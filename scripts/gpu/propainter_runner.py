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
import resource
import shutil
import subprocess
from pathlib import Path


def _raise_open_file_limit() -> None:
    """ProPainter reads long frame/mask sequences and can exceed the default 1024 fd limit."""
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    target = min(max(soft, 65535), hard)
    if soft < target:
        resource.setrlimit(resource.RLIMIT_NOFILE, (target, hard))
        print(f"Raised open file limit from {soft} to {target}", flush=True)


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


def _even_dimension(value: float) -> int:
    dimension = max(2, int(round(value)))
    return dimension if dimension % 2 == 0 else dimension + 1


def _target_video_dimensions(width: int, height: int, params: dict) -> tuple[int, int]:
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


def _processing_dimensions(width: int, height: int, frame_count: int, params: dict) -> tuple[int, int]:
    explicit_width = params.get("propainterWidth")
    explicit_height = params.get("propainterHeight")
    if explicit_width and explicit_height:
        return int(explicit_width), int(explicit_height)

    max_frames = int(params.get("propainterLongVideoFrames") or 900)
    max_short_side = int(params.get("propainterLongVideoShortSide") or 360)
    if frame_count <= max_frames or min(width, height) <= max_short_side:
        return width, height

    scale = max_short_side / min(width, height)
    return _even_dimension(width * scale), _even_dimension(height * scale)


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


def _mask_strategy(params: dict) -> str:
    strategy = str(params.get("maskStrategy") or "").lower()
    if strategy:
        return strategy
    return "subtitle-text" if params.get("removalTarget") == "subtitle" else "rectangle"


def _text_mask_for_frame(frame, regions: list[dict], padding: int, params: dict):
    import cv2
    import numpy as np

    height, width = frame.shape[:2]
    mask = np.zeros((height, width), dtype=np.uint8)
    light_threshold = max(80, min(245, int(params.get("textLightThreshold") or 155)))
    edge_low = max(10, min(200, int(params.get("textEdgeLow") or 45)))
    edge_high = max(edge_low + 10, min(255, int(params.get("textEdgeHigh") or 150)))
    local_padding = max(1, min(16, padding))

    for region in regions:
        x, y, w, h = _normalized_region_to_pixels(region, width, height)
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


def _generate_text_masks(mask_dir: Path, frames_dir: Path, width: int, height: int, regions: list[dict], padding: int, params: dict) -> None:
    import cv2

    mask_dir.mkdir(parents=True, exist_ok=True)
    fallback_mask = None
    for index, frame_path in enumerate(sorted(frames_dir.glob("*.png")), start=1):
        frame = cv2.imread(str(frame_path))
        if frame is None:
            raise RuntimeError(f"Cannot read extracted frame: {frame_path}")
        mask = _text_mask_for_frame(frame, regions, padding, params)
        if mask.max() == 0:
            if fallback_mask is None:
                fallback_dir = mask_dir.parent / "fallback-rect-mask"
                _generate_rectangle_masks(fallback_dir, 1, width, height, regions, padding)
                fallback_mask = cv2.imread(str(fallback_dir / "000001.png"), cv2.IMREAD_GRAYSCALE)
            mask = fallback_mask
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
    _raise_open_file_limit()

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
    target_width, target_height = _target_video_dimensions(width, height, params)
    mask_padding = max(0, min(120, int(params.get("maskPadding") or 8)))
    keep_audio = bool(params.get("keepAudio", True))

    _extract_frames(input_path, frames_dir)
    frame_count = len(sorted(frames_dir.glob("*.png")))
    if frame_count <= 0:
        raise RuntimeError("No frames were extracted from input video.")
    strategy = _mask_strategy(params)
    if strategy in {"subtitle", "subtitle-text", "text", "ocr"}:
        _generate_text_masks(masks_dir, frames_dir, width, height, regions, mask_padding, params)
    else:
        _generate_rectangle_masks(masks_dir, frame_count, width, height, regions, mask_padding)

    precise_mask_strategies = {"subtitle", "subtitle-text", "text", "ocr", "dark-subtitle-line"}
    default_mask_dilation = 1 if strategy in precise_mask_strategies else 5
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
        str(int(params.get("propainterMaskDilation") or default_mask_dilation)),
    ]
    propainter_width, propainter_height = _processing_dimensions(width, height, frame_count, params)
    if (propainter_width, propainter_height) != (width, height):
        print(
            f"Using ProPainter internal size {propainter_width}x{propainter_height} for {frame_count} frames; final output remains {target_width}x{target_height}",
            flush=True,
        )
    if (propainter_width, propainter_height) != (width, height):
        command += ["--height", str(propainter_height), "--width", str(propainter_width)]
    if "propainterNeighborLength" in params:
        command += ["--neighbor_length", str(int(params["propainterNeighborLength"]))]
    elif frame_count > int(params.get("propainterLongVideoFrames") or 900):
        command += ["--neighbor_length", "6"]
    if "propainterRefStride" in params:
        command += ["--ref_stride", str(int(params["propainterRefStride"]))]
    elif frame_count > int(params.get("propainterLongVideoFrames") or 900):
        command += ["--ref_stride", "20"]
    _run(command, cwd=propainter_root)

    inpaint_path = results_dir / frames_dir.name / "inpaint_out.mp4"
    if not inpaint_path.exists():
        raise RuntimeError(f"ProPainter output not found: {inpaint_path}")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    _encode_with_audio(inpaint_path, input_path, output_path, keep_audio, target_width, target_height)
    print(f"ProPainter result saved to {output_path}", flush=True)


if __name__ == "__main__":
    main()
