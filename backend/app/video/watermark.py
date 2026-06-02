import json
import os
import shlex
import shutil
import subprocess
from pathlib import Path
from typing import Any

from app.config import settings


class VideoProcessingError(RuntimeError):
    pass


def _binary(name: str) -> str:
    path = shutil.which(name)
    if path is None:
        raise VideoProcessingError(f"{name} is required. Install FFmpeg first.")
    return path


def _run(command: list[str]) -> None:
    subprocess.run(command, check=True, capture_output=True, text=True)


def probe_video(input_path: Path) -> dict:
    ffprobe = _binary("ffprobe")
    command = [
        ffprobe,
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream=width,height,duration",
        "-of",
        "json",
        str(input_path),
    ]
    result = subprocess.run(command, check=True, capture_output=True, text=True)
    streams = json.loads(result.stdout).get("streams", [])
    if not streams:
        raise VideoProcessingError("No video stream found.")
    return streams[0]


def normalized_region_to_pixels(region: dict, width: int, height: int) -> tuple[int, int, int, int]:
    # 前端传 0-1 的归一化坐标，后端按实际视频分辨率转换成 FFmpeg 需要的像素矩形。
    x = max(0, min(1, float(region.get("x", 0))))
    y = max(0, min(1, float(region.get("y", 0))))
    w = max(0.01, min(1 - x, float(region.get("width", 0.1))))
    h = max(0.01, min(1 - y, float(region.get("height", 0.1))))
    px = max(0, round(x * width))
    py = max(0, round(y * height))
    pw = max(2, min(width - px, round(w * width)))
    ph = max(2, min(height - py, round(h * height)))
    return px, py, pw, ph


def _mask_strategy(params: dict[str, Any]) -> str:
    strategy = str(params.get("maskStrategy") or "").lower()
    if strategy:
        return strategy
    return "subtitle-text" if params.get("removalTarget") == "subtitle" else "rectangle"


def _rect_mask(regions: list[dict], width: int, height: int, padding: int):
    import cv2
    import numpy as np

    mask = np.zeros((height, width), dtype=np.uint8)
    for region in regions:
        x, y, w, h = normalized_region_to_pixels(region, width, height)
        left = max(0, x - padding)
        top = max(0, y - padding)
        right = min(width, x + w + padding)
        bottom = min(height, y + h + padding)
        cv2.rectangle(mask, (left, top), (right, bottom), 255, thickness=-1)
    return mask


def _subtitle_text_mask(frame, regions: list[dict], padding: int, params: dict[str, Any]):
    import cv2
    import numpy as np

    height, width = frame.shape[:2]
    mask = np.zeros((height, width), dtype=np.uint8)
    light_threshold = max(80, min(245, int(params.get("textLightThreshold") or 155)))
    edge_low = max(10, min(200, int(params.get("textEdgeLow") or 45)))
    edge_high = max(edge_low + 10, min(255, int(params.get("textEdgeHigh") or 150)))
    local_padding = max(1, min(16, padding))

    for region in regions:
        x, y, w, h = normalized_region_to_pixels(region, width, height)
        left = max(0, x - local_padding)
        top = max(0, y - local_padding)
        right = min(width, x + w + local_padding)
        bottom = min(height, y + h + local_padding)
        crop = frame[top:bottom, left:right]
        if crop.size == 0:
            continue

        gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
        bright = cv2.inRange(gray, light_threshold, 255)
        edges = cv2.Canny(gray, edge_low, edge_high)

        # 字幕通常是亮色字配黑描边：先抓亮色笔画，再膨胀覆盖描边，避免整条矩形背景被修掉。
        kernel_size = max(3, local_padding | 1)
        kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (kernel_size, kernel_size))
        bright_area = cv2.dilate(bright, kernel, iterations=1)
        edge_near_text = cv2.bitwise_and(cv2.dilate(edges, kernel, iterations=1), cv2.dilate(bright, kernel, iterations=2))
        text_mask = cv2.bitwise_or(bright_area, edge_near_text)
        text_mask = cv2.morphologyEx(text_mask, cv2.MORPH_CLOSE, kernel, iterations=1)

        mask[top:bottom, left:right] = cv2.bitwise_or(mask[top:bottom, left:right], text_mask)

    return mask


def _frame_mask(frame, regions: list[dict], padding: int, params: dict[str, Any], static_mask=None):
    strategy = _mask_strategy(params)
    if strategy in {"subtitle", "subtitle-text", "text", "ocr"}:
        text_mask = _subtitle_text_mask(frame, regions, padding, params)
        if text_mask.max() > 0:
            return text_mask
    if static_mask is not None:
        return static_mask
    height, width = frame.shape[:2]
    return _rect_mask(regions, width, height, padding)


def _output_key(task_id: str, suffix: str = "watermark-removed") -> str:
    return f"{task_id}-{suffix}.mp4"


def _final_result(output_key: str, output_path: Path) -> dict:
    return {
        "storage_key": output_key,
        "url": f"{settings.public_upload_prefix}/{output_key}",
        "mime_type": "video/mp4",
        "size_bytes": output_path.stat().st_size,
    }


def _encode_with_audio(video_only_path: Path, input_path: Path, output_path: Path, keep_audio: bool) -> None:
    ffmpeg = _binary("ffmpeg")
    command = [
        ffmpeg,
        "-y",
        "-i",
        str(video_only_path),
        "-i",
        str(input_path),
        "-map",
        "0:v:0",
    ]
    if keep_audio:
        command += ["-map", "1:a?"]
    command += [
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "20",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "copy",
        "-shortest",
        "-movflags",
        "+faststart",
        str(output_path),
    ]
    _run(command)


def process_with_ffmpeg_delogo(input_path: Path, output_path: Path, params: dict[str, Any]) -> None:
    ffmpeg = _binary("ffmpeg")
    regions = params.get("regions") or []
    stream = probe_video(input_path)
    width = int(stream["width"])
    height = int(stream["height"])
    filters = []
    for region in regions:
        x, y, w, h = normalized_region_to_pixels(region, width, height)
        filters.append(f"delogo=x={x}:y={y}:w={w}:h={h}:show=0")

    keep_audio = bool(params.get("keepAudio", True))
    command = [ffmpeg, "-y", "-i", str(input_path), "-vf", ",".join(filters), "-map", "0:v:0"]
    if keep_audio:
        command += ["-map", "0:a?"]
    command += [
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "23",
        "-c:a",
        "copy",
        "-movflags",
        "+faststart",
        str(output_path),
    ]
    _run(command)


def process_with_opencv_inpaint(input_path: Path, output_path: Path, params: dict[str, Any]) -> None:
    try:
        import cv2
        import numpy as np
    except ImportError as exc:
        raise VideoProcessingError("OpenCV model adapter is not installed.") from exc

    cap = cv2.VideoCapture(str(input_path))
    if not cap.isOpened():
        raise VideoProcessingError("Input video cannot be opened.")

    fps = cap.get(cv2.CAP_PROP_FPS) or 25
    width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    if width <= 0 or height <= 0:
        cap.release()
        raise VideoProcessingError("Invalid video dimensions.")

    regions = params.get("regions") or []
    mask_padding = int(params.get("maskPadding") or 8)
    static_mask = _rect_mask(regions, width, height, mask_padding)

    radius = max(1, min(32, int(params.get("inpaintRadius") or 5)))
    method_name = str(params.get("inpaintMethod") or "telea").lower()
    method = cv2.INPAINT_NS if method_name == "ns" else cv2.INPAINT_TELEA

    temp_video_path = output_path.with_suffix(".model-video.mp4")
    writer = cv2.VideoWriter(
        str(temp_video_path),
        cv2.VideoWriter_fourcc(*"mp4v"),
        fps,
        (width, height),
    )
    if not writer.isOpened():
        cap.release()
        raise VideoProcessingError("Output video cannot be created.")

    while True:
        ok, frame = cap.read()
        if not ok:
            break
        mask = _frame_mask(frame, regions, mask_padding, params, static_mask)
        writer.write(cv2.inpaint(frame, mask, radius, method))

    cap.release()
    writer.release()

    # OpenCV 只写视频流，最终统一交给 FFmpeg 压制并按需挂回原音频。
    _encode_with_audio(temp_video_path, input_path, output_path, bool(params.get("keepAudio", True)))
    temp_video_path.unlink(missing_ok=True)


def process_with_external_model(input_path: Path, output_path: Path, params: dict[str, Any], adapter: str) -> None:
    command_template = settings.propainter_command if adapter == "propainter" else settings.e2fgvi_command
    if not command_template:
        raise VideoProcessingError(f"{adapter} command is not configured.")

    work_dir = output_path.with_suffix(f".{adapter}-work")
    work_dir.mkdir(parents=True, exist_ok=True)
    regions_path = work_dir / "regions.json"
    params_path = work_dir / "params.json"
    regions_path.write_text(json.dumps(params.get("regions") or [], ensure_ascii=False), encoding="utf-8")
    params_path.write_text(json.dumps(params, ensure_ascii=False), encoding="utf-8")

    env = os.environ.copy()
    env["MODEL_PLAZA_INPUT"] = str(input_path)
    env["MODEL_PLAZA_OUTPUT"] = str(output_path)
    env["MODEL_PLAZA_REGIONS"] = str(regions_path)
    env["MODEL_PLAZA_PARAMS"] = str(params_path)
    env["MODEL_PLAZA_WORKDIR"] = str(work_dir)
    command = [
        part.format(
            input=str(input_path),
            output=str(output_path),
            regions=str(regions_path),
            params=str(params_path),
            workdir=str(work_dir),
        )
        for part in shlex.split(command_template)
    ]
    subprocess.run(command, check=True, capture_output=True, text=True, env=env)


def process_with_model_adapter(input_path: Path, output_path: Path, params: dict[str, Any]) -> str:
    adapter = str(params.get("modelAdapter") or params.get("algorithm") or "opencv-inpaint").lower()
    if adapter in {"ffmpeg", "ffmpeg-delogo", "delogo"}:
        process_with_ffmpeg_delogo(input_path, output_path, params)
        return "ffmpeg-delogo"
    if adapter in {"opencv", "opencv-inpaint", "model", "inpaint"}:
        process_with_opencv_inpaint(input_path, output_path, params)
        return "opencv-inpaint"
    if adapter in {"propainter", "e2fgvi"}:
        process_with_external_model(input_path, output_path, params, adapter)
        return adapter
    raise VideoProcessingError(f"Unsupported video model adapter: {adapter}")


def process_masked_video_removal(input_storage_key: str, task_id: str, params: dict, suffix: str = "watermark-removed") -> dict:
    input_path = settings.upload_path / input_storage_key
    if not input_path.exists():
        raise VideoProcessingError("Input video file not found.")

    regions = params.get("regions") or []
    if not regions:
        raise VideoProcessingError("Please select at least one removal region.")

    # 去水印/去字幕共用同一条 mask 修复管线，由业务工具决定输出命名和前端文案。
    output_key = _output_key(task_id, suffix)
    output_path = settings.upload_path / output_key
    output_path.parent.mkdir(parents=True, exist_ok=True)
    process_with_model_adapter(input_path, output_path, params)
    return _final_result(output_key, output_path)


def process_watermark_removal(input_storage_key: str, task_id: str, params: dict) -> dict:
    # Milestone 2 开始由 adapter 决定具体算法：默认模型修复，必要时可回退 FFmpeg delogo。
    return process_masked_video_removal(input_storage_key, task_id, params, suffix="watermark-removed")


def process_subtitle_removal(input_storage_key: str, task_id: str, params: dict) -> dict:
    return process_masked_video_removal(input_storage_key, task_id, params, suffix="subtitle-removed")
