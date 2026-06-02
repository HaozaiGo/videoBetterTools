#!/usr/bin/env python3
"""Run ProPainter on one video using Model Plaza region parameters.

这个脚本部署在 GPU 服务器上执行：
1. 把输入视频拆成帧序列，绕开旧版 ProPainter 对 torchvision.read_video 的依赖。
2. 按前端归一化框选区域生成逐帧 mask。
3. 调用 ProPainter 对帧序列做视频修复。
4. 重新封装为 mp4，并按需挂回原视频音频。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
from pathlib import Path


def _run(command: list[str], cwd: Path | None = None) -> None:
    print("+ " + " ".join(command), flush=True)
    subprocess.run(command, cwd=cwd, check=True)


def _load_json(path: Path, fallback):
    if not path or not path.exists():
        return fallback
    return json.loads(path.read_text(encoding="utf-8"))


def _normalized_region_to_pixels(region: dict, width: int, height: int) -> tuple[int, int, int, int]:
    # 前端传 0-1 坐标；这里转换为真实视频像素，并保证不会越界。
    x = max(0.0, min(1.0, float(region.get("x", 0))))
    y = max(0.0, min(1.0, float(region.get("y", 0))))
    w = max(0.01, min(1.0 - x, float(region.get("width", 0.1))))
    h = max(0.01, min(1.0 - y, float(region.get("height", 0.1))))
    px = max(0, round(x * width))
    py = max(0, round(y * height))
    pw = max(2, min(width - px, round(w * width)))
    ph = max(2, min(height - py, round(h * height)))
    return px, py, pw, ph


def _video_meta(input_path: Path) -> tuple[int, int, float]:
    import cv2

    cap = cv2.VideoCapture(str(input_path))
    if not cap.isOpened():
        raise RuntimeError(f"Cannot open input video: {input_path}")
    width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    fps = float(cap.get(cv2.CAP_PROP_FPS) or 25)
    cap.release()
    if width <= 0 or height <= 0:
        raise RuntimeError("Invalid input video dimensions.")
    return width, height, fps


def _extract_frames(input_path: Path, frames_dir: Path) -> None:
    frames_dir.mkdir(parents=True, exist_ok=True)
    _run(
        [
            "ffmpeg",
            "-y",
            "-i",
            str(input_path),
            "-vsync",
            "0",
            str(frames_dir / "%06d.png"),
        ]
    )


def _generate_rectangle_masks(mask_dir: Path, frame_count: int, width: int, height: int, regions: list[dict], padding: int) -> None:
    import cv2
    import numpy as np

    mask_dir.mkdir(parents=True, exist_ok=True)
    mask = np.zeros((height, width), dtype=np.uint8)
    for region in regions:
        x, y, w, h = _normalized_region_to_pixels(region, width, height)
        left = max(0, x - padding)
        top = max(0, y - padding)
        right = min(width, x + w + padding)
        bottom = min(height, y + h + padding)
        cv2.rectangle(mask, (left, top), (right, bottom), 255, thickness=-1)

    # ProPainter 接收逐帧 mask；固定字幕区域先复制同一张 mask，后续可扩展为跟踪 mask。
    for index in range(1, frame_count + 1):
        cv2.imwrite(str(mask_dir / f"{index:06d}.png"), mask)


def _encode_with_audio(video_only_path: Path, input_path: Path, output_path: Path, keep_audio: bool, width: int, height: int) -> None:
    command = [
        "ffmpeg",
        "-y",
        "-i",
        str(video_only_path),
        "-i",
        str(input_path),
        "-map",
        "0:v:0",
        "-vf",
        f"scale={width}:{height}",
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


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", default=os.environ.get("MODEL_PLAZA_INPUT"))
    parser.add_argument("--output", default=os.environ.get("MODEL_PLAZA_OUTPUT"))
    parser.add_argument("--regions", default=os.environ.get("MODEL_PLAZA_REGIONS"))
    parser.add_argument("--params", default=os.environ.get("MODEL_PLAZA_PARAMS"))
    parser.add_argument("--workdir", default=os.environ.get("MODEL_PLAZA_WORKDIR", "/data1/model-plaza-video-worker/work/propainter"))
    parser.add_argument("--propainter-root", default=os.environ.get("PROPAINTER_ROOT", "/data1/model-plaza-video-worker/repos/ProPainter"))
    parser.add_argument("--python", default=os.environ.get("PROPAINTER_PYTHON", "/data1/conda/miniconda3/envs/video-inpaint/bin/python"))
    args = parser.parse_args()

    input_path = Path(args.input).expanduser().resolve()
    output_path = Path(args.output).expanduser().resolve()
    regions_path = Path(args.regions).expanduser().resolve()
    params_path = Path(args.params).expanduser().resolve() if args.params else Path()
    workdir = Path(args.workdir).expanduser().resolve()
    propainter_root = Path(args.propainter_root).expanduser().resolve()

    regions = _load_json(regions_path, [])
    params = _load_json(params_path, {})
    if not regions:
        raise RuntimeError("No removal regions were provided.")

    if workdir.exists():
        shutil.rmtree(workdir)
    frames_dir = workdir / "frames"
    masks_dir = workdir / "masks"
    results_dir = workdir / "propainter-results"
    workdir.mkdir(parents=True, exist_ok=True)

    width, height, fps = _video_meta(input_path)
    mask_padding = max(0, min(120, int(params.get("maskPadding") or 8)))
    keep_audio = bool(params.get("keepAudio", True))

    _extract_frames(input_path, frames_dir)
    frame_count = len(sorted(frames_dir.glob("*.png")))
    if frame_count <= 0:
        raise RuntimeError("No frames were extracted from input video.")
    _generate_rectangle_masks(masks_dir, frame_count, width, height, regions, mask_padding)

    command = [
        args.python,
        "inference_propainter.py",
        "--video",
        str(frames_dir),
        "--mask",
        str(masks_dir),
        "--output",
        str(results_dir),
        "--save_fps",
        str(max(1, round(fps))),
        "--fp16",
        "--subvideo_length",
        str(int(params.get("subvideoLength") or 40)),
        "--mask_dilation",
        str(int(params.get("propainterMaskDilation") or 5)),
    ]
    if params.get("propainterHeight") and params.get("propainterWidth"):
        command += ["--height", str(int(params["propainterHeight"])), "--width", str(int(params["propainterWidth"]))]
    _run(command, cwd=propainter_root)

    inpaint_path = results_dir / frames_dir.name / "inpaint_out.mp4"
    if not inpaint_path.exists():
        raise RuntimeError(f"ProPainter output not found: {inpaint_path}")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    _encode_with_audio(inpaint_path, input_path, output_path, keep_audio, width, height)
    print(f"ProPainter result saved to {output_path}", flush=True)


if __name__ == "__main__":
    main()
