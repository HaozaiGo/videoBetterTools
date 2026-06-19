import json
import os
import shlex
import shutil
import subprocess
from pathlib import Path
from typing import Any

from app.config import settings
from app.storage import storage


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


def _even_dimension(value: float) -> int:
    dimension = max(2, int(round(value)))
    return dimension if dimension % 2 == 0 else dimension + 1


def target_video_dimensions(width: int, height: int, params: dict[str, Any]) -> tuple[int, int]:
    target_short_side = {
        "720p": 720,
        "1080p": 1080,
        "2k": 1440,
        "4k": 2160,
    }.get(str(params.get("resolution") or "").lower())
    if not target_short_side or width <= 0 or height <= 0:
        return width, height

    if width >= height:
        target_height = target_short_side
        target_width = width * target_height / height
    else:
        target_width = target_short_side
        target_height = height * target_width / width
    return _even_dimension(target_width), _even_dimension(target_height)


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
        hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
        bright = cv2.inRange(gray, light_threshold, 255)
        gold = cv2.inRange(hsv, (8, 45, max(80, light_threshold - 80)), (45, 255, 255))
        edges = cv2.Canny(gray, edge_low, edge_high)

        # 文字水印常带立体暗面和投影：先抓亮色/金色笔画，再沿笔画附近扩出暗部。
        kernel_size = max(3, local_padding | 1)
        kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (kernel_size, kernel_size))
        dark_threshold = max(20, min(120, int(params.get("textDarkThreshold") or 70)))
        dark = cv2.inRange(gray, 0, dark_threshold)
        dark_near_edge = cv2.bitwise_and(dark, cv2.dilate(edges, kernel, iterations=1))
        dark_text = np.zeros_like(dark)
        component_count, labels, stats, _ = cv2.connectedComponentsWithStats(dark_near_edge, 8)
        crop_area = max(1, crop.shape[0] * crop.shape[1])
        for component_index in range(1, component_count):
            area = int(stats[component_index, cv2.CC_STAT_AREA])
            component_width = int(stats[component_index, cv2.CC_STAT_WIDTH])
            component_height = int(stats[component_index, cv2.CC_STAT_HEIGHT])
            if area < 8 or area > crop_area * 0.18:
                continue
            if component_width < 3 or component_height < 3:
                continue
            if component_height > crop.shape[0] * 0.62:
                continue
            dark_text[labels == component_index] = 255
        dark_text = cv2.dilate(dark_text, kernel, iterations=1)

        text_seed = cv2.bitwise_or(cv2.bitwise_or(bright, gold), dark_text)
        bright_area = cv2.dilate(text_seed, kernel, iterations=1)
        edge_near_text = cv2.bitwise_and(cv2.dilate(edges, kernel, iterations=1), cv2.dilate(text_seed, kernel, iterations=2))
        text_mask = cv2.bitwise_or(bright_area, edge_near_text)

        gold_pixels = cv2.countNonZero(gold)
        crop_pixels = max(1, crop.shape[0] * crop.shape[1])
        gold_mode = gold_pixels >= 80 and gold_pixels / crop_pixels >= 0.003
        if gold_mode or params.get("textShadowExpansion"):
            shadow_expansion = max(local_padding + 2, min(64, int(params.get("textShadowExpansion") or round(min(crop.shape[:2]) * 0.16))))
            shadow_kernel_size = max(3, shadow_expansion | 1)
            shadow_kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (shadow_kernel_size, shadow_kernel_size))
            near_text = cv2.dilate(text_seed, shadow_kernel, iterations=1)
            darker_than_text = cv2.inRange(gray, 0, max(80, light_threshold - 35))
            shadow_edges = cv2.bitwise_and(cv2.dilate(edges, kernel, iterations=1), near_text)
            shadow_area = cv2.bitwise_and(cv2.bitwise_or(darker_than_text, shadow_edges), near_text)
            text_mask = cv2.bitwise_or(text_mask, shadow_area)
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


def _is_http_url(value: str) -> bool:
    return value.startswith("http://") or value.startswith("https://")


def _adapter_name(params: dict[str, Any]) -> str:
    return str(params.get("modelAdapter") or params.get("algorithm") or "opencv-inpaint").lower()


def _final_result(output_key: str, output_path: Path) -> dict:
    stored = storage.save_file(output_key, output_path)
    if storage.is_remote:
        storage.delete_local_copy(stored.storage_key)
    return {
        "storage_key": stored.storage_key,
        "url": stored.public_url,
        "mime_type": "video/mp4",
        "size_bytes": stored.size,
    }


def _encode_with_audio(
    video_only_path: Path,
    input_path: Path,
    output_path: Path,
    keep_audio: bool,
    target_width: int | None = None,
    target_height: int | None = None,
) -> None:
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
    if target_width and target_height:
        command += ["-vf", f"scale={target_width}:{target_height}"]
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
    target_width, target_height = target_video_dimensions(width, height, params)
    filters = []
    for region in regions:
        x, y, w, h = normalized_region_to_pixels(region, width, height)
        filters.append(f"delogo=x={x}:y={y}:w={w}:h={h}:show=0")
    if (target_width, target_height) != (width, height):
        filters.append(f"scale={target_width}:{target_height}")

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
    target_width, target_height = target_video_dimensions(width, height, params)

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
    _encode_with_audio(temp_video_path, input_path, output_path, bool(params.get("keepAudio", True)), target_width, target_height)
    temp_video_path.unlink(missing_ok=True)


def process_with_external_model(
    input_path: Path,
    output_path: Path,
    params: dict[str, Any],
    adapter: str,
    input_url: str | None = None,
    result_upload: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    command_template = settings.propainter_command if adapter == "propainter" else settings.e2fgvi_command
    if not command_template:
        raise VideoProcessingError(f"{adapter} command is not configured.")

    work_dir = output_path.with_suffix(f".{adapter}-work")
    work_dir.mkdir(parents=True, exist_ok=True)
    regions_path = work_dir / "regions.json"
    params_path = work_dir / "params.json"
    result_meta_path = work_dir / "result.json"
    regions_path.write_text(json.dumps(params.get("regions") or [], ensure_ascii=False), encoding="utf-8")
    params_path.write_text(json.dumps(params, ensure_ascii=False), encoding="utf-8")

    env = os.environ.copy()
    env["MODEL_PLAZA_INPUT"] = str(input_path)
    env["MODEL_PLAZA_OUTPUT"] = str(output_path)
    env["MODEL_PLAZA_REGIONS"] = str(regions_path)
    env["MODEL_PLAZA_PARAMS"] = str(params_path)
    env["MODEL_PLAZA_WORKDIR"] = str(work_dir)
    env["MODEL_PLAZA_RESULT_META"] = str(result_meta_path)
    env["MODEL_PLAZA_CANCEL_FILE"] = str(settings.upload_path / f"{params.get('taskId') or ''}.cancel")
    env["MODEL_PLAZA_PROVIDER_JOB_ID"] = str(params.get("providerJobId") or "")
    env["MODEL_PLAZA_CALLBACK_URL"] = os.environ.get("MODEL_PLAZA_CALLBACK_URL", "http://127.0.0.1:8010/api/provider/callback")
    if input_url:
        env["MODEL_PLAZA_INPUT_URL"] = input_url
    if result_upload:
        env["MODEL_PLAZA_RESULT_UPLOAD_URL"] = str(result_upload["upload_url"])
        env["MODEL_PLAZA_RESULT_UPLOAD_HEADERS"] = json.dumps(result_upload.get("headers") or {}, ensure_ascii=False)
        env["MODEL_PLAZA_RESULT_STORAGE_KEY"] = str(result_upload["storage_key"])
        env["MODEL_PLAZA_RESULT_URL"] = str(result_upload["url"])
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
    try:
        subprocess.run(command, check=True, capture_output=True, text=True, env=env)
    except subprocess.CalledProcessError as exc:
        detail = "\n".join(part for part in [exc.stdout, exc.stderr] if part).strip()
        tail = detail[-1000:] if detail else str(exc)
        raise VideoProcessingError(f"{adapter} command failed: {tail}") from exc
    if result_meta_path.exists():
        return json.loads(result_meta_path.read_text(encoding="utf-8"))
    return None


def process_with_model_adapter(
    input_path: Path,
    output_path: Path,
    params: dict[str, Any],
    input_url: str | None = None,
    result_upload: dict[str, Any] | None = None,
) -> tuple[str, dict[str, Any] | None]:
    adapter = _adapter_name(params)
    if adapter in {"ffmpeg", "ffmpeg-delogo", "delogo"}:
        process_with_ffmpeg_delogo(input_path, output_path, params)
        return "ffmpeg-delogo", None
    if adapter in {"opencv", "opencv-inpaint", "model", "inpaint"}:
        process_with_opencv_inpaint(input_path, output_path, params)
        return "opencv-inpaint", None
    if adapter in {"propainter", "e2fgvi"}:
        return adapter, process_with_external_model(input_path, output_path, params, adapter, input_url=input_url, result_upload=result_upload)
    raise VideoProcessingError(f"Unsupported video model adapter: {adapter}")


def process_masked_video_removal(input_storage_key: str, task_id: str, params: dict, suffix: str = "watermark-removed") -> dict:
    params = {**params, "taskId": task_id}
    input_url = storage.presign_download(input_storage_key)
    adapter = _adapter_name(params)
    input_path = storage.local_path(input_storage_key)
    input_url_for_adapter = input_url if _is_http_url(input_url) else None
    if not input_path.exists():
        if adapter in {"propainter", "e2fgvi"} and input_url_for_adapter:
            input_path.parent.mkdir(parents=True, exist_ok=True)
        else:
            try:
                input_path = storage.ensure_local(input_storage_key)
            except FileNotFoundError as exc:
                raise VideoProcessingError("Input video file not found.") from exc
    else:
        input_url_for_adapter = None

    regions = params.get("regions") or []
    if not regions:
        raise VideoProcessingError("Please select at least one removal region.")

    # 去水印/去字幕共用同一条 mask 修复管线，由业务工具决定输出命名和前端文案。
    output_key = _output_key(task_id, suffix)
    output_path = settings.upload_path / output_key
    output_path.parent.mkdir(parents=True, exist_ok=True)
    _, remote_result = process_with_model_adapter(
        input_path,
        output_path,
        params,
        input_url=input_url_for_adapter,
        result_upload=None,
    )
    if remote_result:
        return {
            "storage_key": str(remote_result["storage_key"]),
            "url": str(remote_result["url"]),
            "mime_type": str(remote_result.get("mime_type") or "video/mp4"),
            "size_bytes": int(remote_result.get("size_bytes") or 0),
        }
    return _final_result(output_key, output_path)


def process_watermark_removal(input_storage_key: str, task_id: str, params: dict) -> dict:
    # Milestone 2 开始由 adapter 决定具体算法：默认模型修复，必要时可回退 FFmpeg delogo。
    return process_masked_video_removal(input_storage_key, task_id, params, suffix="watermark-removed")


def process_subtitle_removal(input_storage_key: str, task_id: str, params: dict) -> dict:
    return process_masked_video_removal(input_storage_key, task_id, params, suffix="subtitle-removed")
