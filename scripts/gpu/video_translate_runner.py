#!/usr/bin/env python3
"""Create English hard subtitles for Model Plaza video translation tasks."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import textwrap
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


def _run(command: list[str]) -> None:
    print("+ " + " ".join(command), flush=True)
    subprocess.run(command, check=True)


def _load_json(path: Path) -> dict[str, Any]:
    if not path or not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _ensure_input_file(input_path: Path) -> Path:
    if input_path.exists():
        return input_path
    input_url = os.environ.get("MODEL_PLAZA_INPUT_URL", "").strip()
    if not input_url.startswith(("http://", "https://")):
        return input_path
    input_path.parent.mkdir(parents=True, exist_ok=True)
    print(f"Downloading input video from {input_url}", flush=True)
    with urllib.request.urlopen(input_url, timeout=int(os.environ.get("MODEL_PLAZA_INPUT_DOWNLOAD_TIMEOUT", "600"))) as response:
        with input_path.open("wb") as output_file:
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                output_file.write(chunk)
    return input_path


def _write_progress(percent: int, stage: str) -> None:
    percent = max(0, min(100, percent))
    progress_file = os.environ.get("MODEL_PLAZA_PROGRESS_FILE", "").strip()
    if progress_file:
        path = Path(progress_file).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "progress_percent": percent,
                    "progress_stage": stage,
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

    provider_job_id = os.environ.get("MODEL_PLAZA_PROVIDER_JOB_ID", "").strip()
    callback_url = os.environ.get("MODEL_PLAZA_CALLBACK_URL", "").strip()
    if not provider_job_id or not callback_url:
        return
    payload = json.dumps(
        {
            "providerJobId": provider_job_id,
            "status": "processing",
            "callbackId": f"{provider_job_id}:translate-progress:{percent}",
            "progressPercent": percent,
            "progressStage": stage,
        },
        ensure_ascii=False,
    ).encode("utf-8")
    request = urllib.request.Request(
        callback_url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        urllib.request.urlopen(request, timeout=5).read()
    except Exception as exc:
        print(f"Progress callback failed: {exc}", flush=True)


def _probe_duration(input_path: Path) -> float:
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(input_path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    try:
        return max(1.0, float(result.stdout.strip()))
    except ValueError:
        return 30.0


def _probe_video_size(input_path: Path) -> tuple[int, int]:
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=width,height",
            "-of",
            "csv=p=0:s=x",
            str(input_path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    try:
        width, height = [int(item) for item in result.stdout.strip().split("x", 1)]
    except ValueError:
        return 1920, 1080
    return max(1, width), max(1, height)


def _ass_time(seconds: float) -> str:
    seconds = max(0, seconds)
    centiseconds = int(round(seconds * 100))
    cs = centiseconds % 100
    total_seconds = centiseconds // 100
    s = total_seconds % 60
    total_minutes = total_seconds // 60
    m = total_minutes % 60
    h = total_minutes // 60
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def _escape_ass_text(value: str) -> str:
    return value.replace("\\", "\\\\").replace("{", "\\{").replace("}", "\\}").replace("\n", " ")


def _subtitle_layout(video_width: int, video_height: int) -> dict[str, int]:
    short_side = min(video_width, video_height)
    scale = max(0.62, min(1.0, short_side / 1080))
    font_size = max(26, min(44, round(44 * scale)))
    max_chars = max(24, min(46, round(video_width / max(font_size * 0.82, 1))))
    # 竖屏短视频里，字幕贴底会被播放器控制条和画面边缘挤压；默认把底部字幕放到约 70%-75% 高度区域。
    bottom_margin_ratio = float(os.environ.get("MODEL_PLAZA_SUBTITLE_BOTTOM_MARGIN_RATIO", "0.24"))
    return {
        "font_size": font_size,
        "max_chars": max_chars,
        "outline": max(2, round(font_size * 0.08)),
        "shadow": max(1, round(font_size * 0.04)),
        "margin_v": max(34, round(video_height * 0.052)),
        "bottom_margin_v": max(48, round(video_height * bottom_margin_ratio)),
        "margin_l": max(36, round(video_width * 0.025)),
    }


def _wrap_subtitle_lines(value: str, width: int, max_lines: int = 3) -> list[str]:
    normalized = re.sub(r"\s+", " ", value.strip())
    if not normalized:
        return []
    lines = textwrap.wrap(normalized, width=width, break_long_words=False, break_on_hyphens=False)
    return lines[:max_lines]


def _ass_alignment(placement: str, layout: dict[str, int]) -> tuple[int, int, int]:
    if placement == "top":
        return 8, layout["margin_v"], layout["margin_l"]
    if placement == "middle-lower":
        return 5, layout["margin_v"], layout["margin_l"]
    return 2, layout["bottom_margin_v"], layout["margin_l"]


def _fallback_segments(duration: float) -> list[dict[str, Any]]:
    segment_length = 4.5
    segments: list[dict[str, Any]] = []
    cursor = 0.0
    while cursor < duration:
        end = min(duration, cursor + segment_length)
        segments.append(
            {
                "start": cursor,
                "end": end,
                "text": "English subtitles will appear here after speech recognition and translation.",
            }
        )
        cursor = end
    return segments


def _translate_text(text: str, target_language: str) -> str:
    endpoint = os.environ.get("MODEL_PLAZA_TRANSLATE_API_URL", "").strip()
    api_key = os.environ.get("MODEL_PLAZA_TRANSLATE_API_KEY", "").strip()
    if not endpoint:
        return text
    language_names = {
        "en": "English",
        "eng": "English",
        "english": "English",
        "ja": "Japanese",
        "jp": "Japanese",
        "japanese": "Japanese",
        "ko": "Korean",
        "kr": "Korean",
        "korean": "Korean",
        "es": "Spanish",
        "fr": "French",
        "de": "German",
        "pt": "Portuguese",
        "ru": "Russian",
        "it": "Italian",
        "vi": "Vietnamese",
        "th": "Thai",
        "id": "Indonesian",
        "ar": "Arabic",
        "hi": "Hindi",
    }
    target_name = language_names.get(target_language.lower(), target_language)
    model_name = os.environ.get("MODEL_PLAZA_TRANSLATE_MODEL", "gpt-4.1-mini")
    instruction = (
        f"Translate the subtitle line into {target_name}. Keep it natural, concise, and suitable "
        "for video subtitles. Preserve names, numbers, and units. Return only the translated subtitle text."
    )
    if model_name.startswith("qwen-mt-"):
        messages = [{"role": "user", "content": f"{instruction}\n\n{text}"}]
    else:
        messages = [
            {"role": "system", "content": instruction},
            {"role": "user", "content": text},
        ]
    payload = json.dumps({"model": model_name, "messages": messages, "temperature": 0.2}).encode("utf-8")
    request = urllib.request.Request(
        endpoint,
        data=payload,
        headers={
            "Content-Type": "application/json",
            **({"Authorization": f"Bearer {api_key}"} if api_key else {}),
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=int(os.environ.get("MODEL_PLAZA_TRANSLATE_TIMEOUT", "60"))) as response:
            body = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        error_body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"subtitle translation API failed with HTTP {exc.code}: {error_body[:600]}") from exc
    return str(body["choices"][0]["message"]["content"]).strip()


def _target_is_english(target_language: str) -> bool:
    return target_language.lower() in {"en", "eng", "english", "英语", "英文"}


def _transcribe_segments(input_path: Path, target_language: str) -> list[dict[str, Any]]:
    try:
        from faster_whisper import WhisperModel
    except ImportError as exc:
        raise RuntimeError("faster-whisper is not installed; cannot recognize speech for video translation.") from exc

    model_name = os.environ.get("MODEL_PLAZA_WHISPER_MODEL", "large-v3")
    device = os.environ.get("MODEL_PLAZA_WHISPER_DEVICE", "cuda")
    compute_type = os.environ.get("MODEL_PLAZA_WHISPER_COMPUTE_TYPE", "float16")
    model = WhisperModel(model_name, device=device, compute_type=compute_type)
    source_language = os.environ.get("MODEL_PLAZA_SOURCE_LANGUAGE", "").strip() or None
    task = "translate" if _target_is_english(target_language) else "transcribe"
    print(
        f"Whisper model={model_name} device={device} compute_type={compute_type} task={task} source_language={source_language or 'auto'}",
        flush=True,
    )
    segments, _ = model.transcribe(str(input_path), language=source_language, task=task, vad_filter=True)

    translated: list[dict[str, Any]] = []
    for segment in segments:
        source_text = re.sub(r"\s+", " ", segment.text).strip()
        if not source_text:
            continue
        output_text = source_text if task == "translate" else _translate_text(source_text, target_language)
        translated.append(
            {
                "start": float(segment.start),
                "end": float(segment.end),
                "text": output_text,
                "sourceText": "" if task == "translate" else source_text,
            }
        )
    return translated


def _write_ass(path: Path, segments: list[dict[str, Any]], placement: str, video_width: int, video_height: int) -> None:
    layout = _subtitle_layout(video_width, video_height)
    alignment, margin_v, margin_l = _ass_alignment(placement, layout)
    style = (
        f"Style: Default,Arial,{layout['font_size']},&H00FFFFFF,&H000000FF,&H00000000,&H99000000,"
        f"1,0,0,0,100,100,0,0,1,{layout['outline']},{layout['shadow']},"
        f"{alignment},{margin_l},{margin_l},{margin_v},1"
    )
    events = []
    for segment in segments:
        start = float(segment.get("start") or 0)
        end = max(start + 0.8, float(segment.get("end") or start + 2.5))
        lines = [_escape_ass_text(line) for line in _wrap_subtitle_lines(str(segment.get("text") or ""), layout["max_chars"])]
        if lines:
            text = "\\N".join(lines)
            events.append(f"Dialogue: 0,{_ass_time(start)},{_ass_time(end)},Default,,0,0,0,,{text}")
    content = "\n".join(
        [
            "[Script Info]",
            "ScriptType: v4.00+",
            "ScaledBorderAndShadow: yes",
            f"PlayResX: {video_width}",
            f"PlayResY: {video_height}",
            "",
            "[V4+ Styles]",
            "Format: Name,Fontname,Fontsize,PrimaryColour,SecondaryColour,OutlineColour,BackColour,Bold,Italic,Underline,StrikeOut,ScaleX,ScaleY,Spacing,Angle,BorderStyle,Outline,Shadow,Alignment,MarginL,MarginR,MarginV,Encoding",
            style,
            "",
            "[Events]",
            "Format: Layer,Start,End,Style,Name,MarginL,MarginR,MarginV,Effect,Text",
            *events,
            "",
        ]
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _subtitle_filter_path(path: Path) -> str:
    escaped = str(path).replace("\\", "\\\\").replace(":", "\\:").replace(",", "\\,")
    return f"subtitles=filename={escaped}"


def _ffmpeg_has_filter(name: str) -> bool:
    try:
        result = subprocess.run(["ffmpeg", "-hide_banner", "-filters"], check=True, capture_output=True, text=True)
    except subprocess.SubprocessError:
        return False
    return any(line.split()[1:2] == [name] for line in result.stdout.splitlines() if len(line.split()) >= 2)


def _active_text(segments: list[dict[str, Any]], timestamp: float) -> str:
    for segment in segments:
        start = float(segment.get("start") or 0)
        end = float(segment.get("end") or start + 2.5)
        if start <= timestamp <= end:
            return str(segment.get("text") or "")
    return ""


def _draw_subtitle_frame(frame, text: str, placement: str):
    import cv2

    if not text.strip():
        return frame
    height, width = frame.shape[:2]
    font = cv2.FONT_HERSHEY_SIMPLEX
    layout = _subtitle_layout(width, height)
    lines = _wrap_subtitle_lines(text, layout["max_chars"], max_lines=3)
    if not lines:
        return frame
    scale = max(0.55, min(1.25, layout["font_size"] / 34))
    thickness = max(2, round(layout["font_size"] / 18))
    while scale > 0.5:
        widest = max(cv2.getTextSize(line, font, scale, thickness)[0][0] for line in lines)
        if widest <= width - layout["margin_l"] * 2:
            break
        scale -= 0.05
    line_height = int(layout["font_size"] * 1.18)
    block_height = line_height * len(lines)
    if placement == "top":
        y = layout["margin_v"] + line_height
    elif placement == "middle-lower":
        y = int(height * 0.68)
    else:
        y = height - layout["bottom_margin_v"] - block_height + line_height
    for index, line in enumerate(lines):
        size, _ = cv2.getTextSize(line, font, scale, thickness)
        x = max(layout["margin_l"], (width - size[0]) // 2)
        baseline = y + index * line_height
        cv2.putText(frame, line, (x, baseline), font, scale, (0, 0, 0), thickness + 4, cv2.LINE_AA)
        cv2.putText(frame, line, (x, baseline), font, scale, (255, 255, 255), thickness, cv2.LINE_AA)
    return frame


def _encode_with_opencv_subtitles(input_path: Path, output_path: Path, segments: list[dict[str, Any]], placement: str, keep_audio: bool) -> None:
    try:
        import cv2
    except ImportError as exc:
        raise RuntimeError("FFmpeg subtitles filter is unavailable and OpenCV is not installed.") from exc

    cap = cv2.VideoCapture(str(input_path))
    if not cap.isOpened():
        raise RuntimeError(f"Cannot open input video: {input_path}")
    fps = cap.get(cv2.CAP_PROP_FPS) or 25
    width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    temp_video = output_path.with_suffix(".subtitle-video.mp4")
    writer = cv2.VideoWriter(str(temp_video), cv2.VideoWriter_fourcc(*"mp4v"), fps, (width, height))
    if not writer.isOpened():
        cap.release()
        raise RuntimeError(f"Cannot create output video: {temp_video}")
    frame_index = 0
    while True:
        ok, frame = cap.read()
        if not ok:
            break
        timestamp = frame_index / fps
        writer.write(_draw_subtitle_frame(frame, _active_text(segments, timestamp), placement))
        frame_index += 1
    cap.release()
    writer.release()

    if not keep_audio:
        temp_video.replace(output_path)
        return
    _run(
        [
            "ffmpeg",
            "-y",
            "-i",
            str(temp_video),
            "-i",
            str(input_path),
            "-map",
            "0:v:0",
            "-map",
            "1:a?",
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
    )
    temp_video.unlink(missing_ok=True)


def _encode_with_subtitles(input_path: Path, ass_path: Path, output_path: Path, keep_audio: bool) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if not _ffmpeg_has_filter("subtitles"):
        segments = _load_json(ass_path.with_suffix(".segments.json"))
        placement = str(segments.pop("_placement", "bottom")) if isinstance(segments, dict) else "bottom"
        segment_items = segments.get("items", []) if isinstance(segments, dict) else []
        _encode_with_opencv_subtitles(input_path, output_path, segment_items, placement, keep_audio)
        return
    command = [
        "ffmpeg",
        "-y",
        "-i",
        str(input_path),
        "-vf",
        _subtitle_filter_path(ass_path),
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
    parser.add_argument("--regions", default=None, help="兼容 GPU API 通用任务协议，翻译任务不使用区域参数。")
    parser.add_argument("--params", default=os.environ.get("MODEL_PLAZA_PARAMS"))
    parser.add_argument("--workdir", default=os.environ.get("MODEL_PLAZA_WORKDIR", "/tmp/model-plaza-translate"))
    args = parser.parse_args()

    input_path = _ensure_input_file(Path(args.input).expanduser().resolve())
    output_path = Path(args.output).expanduser().resolve()
    params_path = Path(args.params).expanduser().resolve() if args.params else Path()
    workdir = Path(args.workdir).expanduser().resolve()
    params = _load_json(params_path)
    target_language = str(params.get("targetLanguage") or "en")
    placement = str(params.get("subtitlePlacement") or "bottom")
    keep_audio = bool(params.get("keepAudio", True))

    _write_progress(15, "读取视频信息")
    duration = _probe_duration(input_path)
    video_width, video_height = _probe_video_size(input_path)
    _write_progress(30, "识别语音并翻译英文字幕")
    segments = _transcribe_segments(input_path, target_language)
    if not segments:
        if os.environ.get("MODEL_PLAZA_TRANSLATE_ALLOW_PLACEHOLDER", "").lower() in {"1", "true", "yes"}:
            segments = _fallback_segments(duration)
        else:
            raise RuntimeError("speech recognition returned no subtitle segments.")
    segments_path = workdir / "segments.json"
    segments_path.parent.mkdir(parents=True, exist_ok=True)
    segments_path.write_text(json.dumps(segments, ensure_ascii=False, indent=2), encoding="utf-8")

    _write_progress(70, "生成字幕文件")
    ass_path = workdir / "translated.ass"
    _write_ass(ass_path, segments, placement, video_width, video_height)
    ass_path.with_suffix(".segments.json").write_text(
        json.dumps({"_placement": placement, "items": segments}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    _write_progress(85, "写入英文硬字幕")
    _encode_with_subtitles(input_path, ass_path, output_path, keep_audio)
    _write_progress(100, "视频翻译完成")


if __name__ == "__main__":
    main()
