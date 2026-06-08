#!/usr/bin/env python3
"""Run Real-ESRGAN video enhancement for Model Plaza."""

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


def _load_json(path: Path) -> dict:
    if not path or not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _write_progress(percent: int, stage: str) -> None:
    progress_file = os.environ.get("MODEL_PLAZA_PROGRESS_FILE", "").strip()
    if not progress_file:
        return
    path = Path(progress_file).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "progress_percent": max(0, min(100, percent)),
                "progress_stage": stage,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def _even_dimension(value: float) -> int:
    dimension = max(2, int(round(value)))
    return dimension if dimension % 2 == 0 else dimension + 1


def _target_video_dimensions(width: int, height: int, params: dict) -> tuple[int, int]:
    target_short_side = {
        "720p": 720,
        "1080p": 1080,
        "2k": 1440,
        "4k": 2160,
    }.get(str(params.get("resolution") or "1080p").lower())
    if not target_short_side or width <= 0 or height <= 0:
        return width, height
    if width >= height:
        target_height = target_short_side
        target_width = width * target_height / height
    else:
        target_width = target_short_side
        target_height = height * target_width / width
    return _even_dimension(target_width), _even_dimension(target_height)


def _int_param(params: dict, name: str, default: int) -> int:
    value = params.get(name)
    if value is None or value == "":
        env_value = os.environ.get(f"REALESRGAN_{name.upper()}", "").strip()
        value = env_value if env_value else default
    return int(value)


def _default_tile(width: int, height: int, target_width: int, target_height: int) -> int:
    pixels = max(width * height, target_width * target_height)
    if pixels <= 2560 * 1440:
        return 0
    if pixels <= 3840 * 2160:
        return 512
    return 256


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


def _find_realesrgan_output(results_dir: Path, input_path: Path, suffix: str) -> Path:
    expected = results_dir / f"{input_path.stem}_{suffix}.mp4"
    if expected.exists():
        return expected
    matches = sorted(results_dir.glob(f"*_{suffix}.mp4"))
    if matches:
        return matches[0]
    matches = sorted(results_dir.glob("*.mp4"))
    if matches:
        return matches[0]
    raise RuntimeError(f"Real-ESRGAN output was not found in {results_dir}")


def _encode_final(video_path: Path, output_path: Path, width: int, height: int) -> None:
    _run(
        [
            "ffmpeg",
            "-y",
            "-i",
            str(video_path),
            "-map",
            "0:v:0",
            "-map",
            "0:a?",
            "-vf",
            f"scale={width}:{height}",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "18",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "copy",
            "-shortest",
            "-movflags",
            "+faststart",
            str(output_path),
        ]
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", default=os.environ.get("MODEL_PLAZA_INPUT"))
    parser.add_argument("--output", default=os.environ.get("MODEL_PLAZA_OUTPUT"))
    parser.add_argument("--regions", default=None, help="兼容 GPU API 通用任务协议，超分任务不使用区域参数。")
    parser.add_argument("--params", default=os.environ.get("MODEL_PLAZA_PARAMS"))
    parser.add_argument("--workdir", default=os.environ.get("MODEL_PLAZA_WORKDIR", "/data1/model-plaza-video-worker/work/enhance"))
    parser.add_argument("--realesrgan-root", default=os.environ.get("REALESRGAN_ROOT", "/data1/model-plaza-video-worker/repos/Real-ESRGAN"))
    parser.add_argument("--python", default=os.environ.get("REALESRGAN_PYTHON", "/data1/conda/miniconda3/envs/video-inpaint/bin/python"))
    args = parser.parse_args()

    input_path = Path(args.input).expanduser().resolve()
    output_path = Path(args.output).expanduser().resolve()
    params_path = Path(args.params).expanduser().resolve() if args.params else Path()
    workdir = Path(args.workdir).expanduser().resolve()
    realesrgan_root = Path(args.realesrgan_root).expanduser().resolve()
    params = _load_json(params_path)
    _write_progress(10, "读取视频信息")

    if workdir.exists():
        shutil.rmtree(workdir)
    results_dir = workdir / "realesrgan-results"
    results_dir.mkdir(parents=True, exist_ok=True)

    width, height, fps = _video_meta(input_path)
    target_width, target_height = _target_video_dimensions(width, height, params)
    _write_progress(15, f"准备超分到 {target_width}x{target_height}")
    outscale = max(target_width / width, target_height / height)
    outscale = max(1.0, min(4.0, outscale))

    mode = str(params.get("enhanceMode") or "quality").lower()
    model_name = "RealESRNet_x4plus" if mode == "natural" else "RealESRGAN_x4plus"
    suffix = "enhanced"
    tile = max(0, _int_param(params, "tile", _default_tile(width, height, target_width, target_height)))

    command = [
        args.python,
        "inference_realesrgan_video.py",
        "-i",
        str(input_path),
        "-o",
        str(results_dir),
        "-n",
        str(params.get("realesrganModel") or model_name),
        "-s",
        f"{outscale:.4f}",
        "--suffix",
        suffix,
        "-t",
        str(tile),
        "--tile_pad",
        str(max(0, _int_param(params, "tilePad", 10))),
        "--fps",
        f"{fps:.4f}",
        "--ffmpeg_bin",
        "ffmpeg",
        "--num_process_per_gpu",
        str(int(params.get("numProcessPerGpu") or 1)),
    ]
    if params.get("fp32"):
        command.append("--fp32")
    _write_progress(25, "Real-ESRGAN 正在超分视频帧")
    _run(command, cwd=realesrgan_root)

    enhanced_path = _find_realesrgan_output(results_dir, input_path, suffix)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    _write_progress(90, "合成目标清晰度视频")
    _encode_final(enhanced_path, output_path, target_width, target_height)
    _write_progress(100, "视频超分完成")
    print(f"Enhanced video saved to {output_path}", flush=True)


if __name__ == "__main__":
    main()
