import json
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
    mask = np.zeros((height, width), dtype=np.uint8)
    for region in regions:
        x, y, w, h = normalized_region_to_pixels(region, width, height)
        left = max(0, x - mask_padding)
        top = max(0, y - mask_padding)
        right = min(width, x + w + mask_padding)
        bottom = min(height, y + h + mask_padding)
        cv2.rectangle(mask, (left, top), (right, bottom), 255, thickness=-1)

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
        writer.write(cv2.inpaint(frame, mask, radius, method))

    cap.release()
    writer.release()

    # OpenCV 只写视频流，最终统一交给 FFmpeg 压制并按需挂回原音频。
    _encode_with_audio(temp_video_path, input_path, output_path, bool(params.get("keepAudio", True)))
    temp_video_path.unlink(missing_ok=True)


def process_with_model_adapter(input_path: Path, output_path: Path, params: dict[str, Any]) -> str:
    adapter = str(params.get("modelAdapter") or params.get("algorithm") or "opencv-inpaint").lower()
    if adapter in {"ffmpeg", "ffmpeg-delogo", "delogo"}:
        process_with_ffmpeg_delogo(input_path, output_path, params)
        return "ffmpeg-delogo"
    if adapter in {"opencv", "opencv-inpaint", "model", "inpaint"}:
        process_with_opencv_inpaint(input_path, output_path, params)
        return "opencv-inpaint"
    raise VideoProcessingError(f"Unsupported video model adapter: {adapter}")


def process_watermark_removal(input_storage_key: str, task_id: str, params: dict) -> dict:
    input_path = settings.upload_path / input_storage_key
    if not input_path.exists():
        raise VideoProcessingError("Input video file not found.")

    regions = params.get("regions") or []
    if not regions:
        raise VideoProcessingError("Please select at least one watermark region.")

    # Milestone 2 开始由 adapter 决定具体算法：默认模型修复，必要时可回退 FFmpeg delogo。
    output_key = _output_key(task_id)
    output_path = settings.upload_path / output_key
    output_path.parent.mkdir(parents=True, exist_ok=True)
    process_with_model_adapter(input_path, output_path, params)
    return _final_result(output_key, output_path)
