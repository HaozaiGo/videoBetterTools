import json
import shutil
import subprocess
from pathlib import Path

from app.config import settings


class VideoProcessingError(RuntimeError):
    pass


def _binary(name: str) -> str:
    path = shutil.which(name)
    if path is None:
        raise VideoProcessingError(f"{name} is required. Install FFmpeg first.")
    return path


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
    x = max(0, min(1, float(region.get("x", 0))))
    y = max(0, min(1, float(region.get("y", 0))))
    w = max(0.01, min(1 - x, float(region.get("width", 0.1))))
    h = max(0.01, min(1 - y, float(region.get("height", 0.1))))
    px = max(0, int(x * width))
    py = max(0, int(y * height))
    pw = max(2, min(width - px, int(w * width)))
    ph = max(2, min(height - py, int(h * height)))
    return px, py, pw, ph


def process_watermark_removal(input_storage_key: str, task_id: str, params: dict) -> dict:
    ffmpeg = _binary("ffmpeg")
    input_path = settings.upload_path / input_storage_key
    if not input_path.exists():
        raise VideoProcessingError("Input video file not found.")

    regions = params.get("regions") or []
    if not regions:
        raise VideoProcessingError("Please select at least one watermark region.")

    stream = probe_video(input_path)
    width = int(stream["width"])
    height = int(stream["height"])
    x, y, w, h = normalized_region_to_pixels(regions[0], width, height)

    output_key = f"{task_id}-watermark-removed.mp4"
    output_path = settings.upload_path / output_key
    output_path.parent.mkdir(parents=True, exist_ok=True)

    keep_audio = bool(params.get("keepAudio", True))
    command = [
        ffmpeg,
        "-y",
        "-i",
        str(input_path),
        "-vf",
        f"delogo=x={x}:y={y}:w={w}:h={h}:show=0",
        "-map",
        "0:v:0",
    ]
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
    subprocess.run(command, check=True, capture_output=True, text=True)
    return {
        "storage_key": output_key,
        "url": f"{settings.public_upload_prefix}/{output_key}",
        "mime_type": "video/mp4",
        "size_bytes": output_path.stat().st_size,
    }
