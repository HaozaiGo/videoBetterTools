import json
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable
from uuid import uuid4

from app.config import settings


class GptProtoError(RuntimeError):
    pass


class GptProtoCancelled(RuntimeError):
    pass


ProgressCallback = Callable[[int, str], None]

VIDEO_REDRAW_MODELS = {
    "veo-3.1-generate-preview": {
        "label": "Veo 3.1",
        "api": "google-v1beta",
    },
    "kling-video-o1-std": {
        "label": "Kling Video O1 Standard",
        "api": "gptproto-v3",
        "path": "/api/v3/kwaivgi/kling-video-o1-std/video-to-video",
    },
    "kling-video-o1-pro": {
        "label": "Kling Video O1 Pro",
        "api": "gptproto-v3",
        "path": "/api/v3/kwaivgi/kling-video-o1-pro/video-to-video",
    },
}

VIDEO_REDRAW_MODEL_ALIASES = {
    "kling-v3-omni-std": "kling-video-o1-std",
}


def _api_key() -> str:
    return settings.gptproto_api_key or settings.seedreambest_gptproto_api_key


def _query_token() -> str:
    return settings.gptproto_query_token or settings.seedreambest_gptproto_query_token or _api_key()


def _base_url() -> str:
    return (settings.gptproto_base_url or settings.seedreambest_gptproto_base_url or "https://gptproto.com").rstrip("/")


def normalize_video_redraw_model(model: str) -> str:
    value = (model or "veo-3.1-generate-preview").strip()
    return VIDEO_REDRAW_MODEL_ALIASES.get(value, value)


def _request_json(url: str, payload: dict | None = None, method: str = "POST", token: str | None = None) -> dict:
    body = json.dumps(payload or {}).encode("utf-8") if payload is not None else None
    api_token = token or _api_key()
    headers = {
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0 ModelPlaza/0.1",
        "x-goog-api-key": api_token,
    }
    if _api_key():
        headers["Authorization"] = _api_key()
    request = urllib.request.Request(
        url,
        data=body,
        method=method,
        headers=headers,
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            data = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise GptProtoError(f"GPTProto request failed: HTTP {exc.code} {detail[:500]}") from exc
    except urllib.error.URLError as exc:
        raise GptProtoError(f"GPTProto request failed: {exc}") from exc

    try:
        return json.loads(data or "{}")
    except json.JSONDecodeError as exc:
        raise GptProtoError(f"GPTProto returned invalid JSON: {data[:500]}") from exc


def _operation_id(operation: dict) -> str:
    name = str(operation.get("name") or operation.get("operation") or operation.get("id") or "").strip()
    if not name:
        raise GptProtoError("GPTProto did not return an operation id")
    return name.rsplit("/", 1)[-1]


def _operation_error(operation: dict) -> str:
    error = operation.get("error")
    if isinstance(error, dict):
        message = error.get("message") or error.get("details") or error
        return json.dumps(message, ensure_ascii=False) if not isinstance(message, str) else message
    if error:
        return str(error)
    return "GPTProto generation failed"


def _find_video_url(payload: object) -> str:
    if isinstance(payload, dict):
        for key in ("uri", "url", "videoUri", "videoUrl"):
            value = payload.get(key)
            if isinstance(value, str) and value.startswith(("http://", "https://")) and _looks_like_video_url(value):
                return value
        for key in ("video", "file", "output", "generatedVideo", "result"):
            if key in payload:
                found = _find_video_url(payload[key])
                if found:
                    return found
        for value in payload.values():
            found = _find_video_url(value)
            if found:
                return found
    if isinstance(payload, list):
        for item in payload:
            found = _find_video_url(item)
            if found:
                return found
    if isinstance(payload, str) and payload.startswith(("http://", "https://")) and _looks_like_video_url(payload):
        return payload
    return ""


def _looks_like_video_url(url: str) -> bool:
    lowered = url.lower().split("?", 1)[0]
    return lowered.endswith((".mp4", ".mov", ".webm", ".m4v")) or "/video" in lowered


def _metadata_progress(operation: dict) -> int | None:
    metadata = operation.get("metadata")
    if not isinstance(metadata, dict):
        return None
    for key in ("progressPercentage", "progressPercent", "percent"):
        value = metadata.get(key)
        if isinstance(value, (int, float)):
            return max(0, min(100, int(value)))
    return None


def _prediction_data(payload: dict) -> dict:
    data = payload.get("data")
    return data if isinstance(data, dict) else payload


def _prediction_id(payload: dict) -> str:
    data = _prediction_data(payload)
    prediction_id = str(data.get("id") or payload.get("id") or "").strip()
    if prediction_id:
        return prediction_id
    urls = data.get("urls")
    if isinstance(urls, dict):
        get_url = str(urls.get("get") or "")
        if get_url:
            return get_url.rstrip("/").rsplit("/", 2)[-2] if get_url.rstrip("/").endswith("/result") else get_url.rstrip("/").rsplit("/", 1)[-1]
    raise GptProtoError(f"GPTProto did not return a prediction id: {json.dumps(payload)[:800]}")


def _prediction_result_url(base_url: str, payload: dict, prediction_id: str) -> str:
    data = _prediction_data(payload)
    urls = data.get("urls")
    if isinstance(urls, dict) and isinstance(urls.get("get"), str):
        return urls["get"]
    return f"{base_url}/api/v3/predictions/{prediction_id}/result"


def _prediction_error(payload: dict) -> str:
    data = _prediction_data(payload)
    error = data.get("error") or payload.get("error")
    if isinstance(error, str):
        return error
    if isinstance(error, dict):
        message = error.get("message") or error
        return json.dumps(message, ensure_ascii=False) if not isinstance(message, str) else message
    message = payload.get("message")
    if isinstance(message, str):
        return message
    return "GPTProto generation failed"


def _download_result(video_url: str, task_id: str, model: str, operation_id: str) -> dict:
    local_path = _download_video(video_url, task_id)
    now = datetime.now(timezone.utc)
    return {
        "local_path": str(local_path),
        "storage_key": f"model-plaza/output/videos/{now:%Y/%m/%d}/{task_id}-video-redraw.mp4",
        "url": video_url,
        "mime_type": "video/mp4",
        "size_bytes": local_path.stat().st_size,
        "gptproto_operation_id": operation_id,
        "gptproto_model": model,
    }


def _download_video(url: str, task_id: str) -> Path:
    temp_dir = settings.upload_path / "gptproto-results"
    temp_dir.mkdir(parents=True, exist_ok=True)
    output_path = temp_dir / f"{task_id}-{uuid4().hex}.mp4"
    try:
        request = urllib.request.Request(
            url,
            headers={"User-Agent": "Mozilla/5.0 ModelPlaza/0.1"},
        )
        with urllib.request.urlopen(request, timeout=600) as response:
            with output_path.open("wb") as output:
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    output.write(chunk)
    except Exception as exc:
        output_path.unlink(missing_ok=True)
        raise GptProtoError(f"Failed to download GPTProto result: {exc}") from exc
    return output_path


def generate_video_redraw(
    input_video_url: str,
    task_id: str,
    params: dict,
    progress: ProgressCallback,
) -> dict:
    if not input_video_url.startswith(("http://", "https://")):
        raise GptProtoError("GPTProto video-to-video requires a public input video URL")

    prompt = str(params.get("videoPrompt") or params.get("prompt") or "").strip()
    if not prompt:
        raise GptProtoError("videoPrompt is required")
    resolution = str(params.get("resolution") or "720p")
    if resolution not in {"720p", "1080p"}:
        resolution = "720p"
    aspect_ratio = str(params.get("aspectRatio") or "16:9")
    if aspect_ratio not in {"16:9", "9:16", "1:1"}:
        aspect_ratio = "16:9"

    model = normalize_video_redraw_model(str(params.get("providerModel") or "veo-3.1-generate-preview"))
    if model not in VIDEO_REDRAW_MODELS:
        raise GptProtoError(f"Unsupported GPTProto video redraw model: {model}")
    model_config = VIDEO_REDRAW_MODELS[model]
    model_label = str(model_config["label"])

    base_url = _base_url()
    interval = max(2, settings.gptproto_poll_interval_seconds)
    timeout = max(interval, settings.gptproto_timeout_seconds)
    deadline = time.time() + timeout

    if model_config["api"] == "gptproto-v3":
        api_token = _api_key()
        if not api_token:
            raise GptProtoError("GPTPROTO_API_KEY is not configured")

        submit_url = f"{base_url}{model_config['path']}"
        progress(8, f"正在提交 GPTProto {model_label} 视频转绘任务")
        operation = _request_json(
            submit_url,
            {
                "prompt": prompt,
                "video": input_video_url,
                "aspect_ratio": aspect_ratio,
                "duration": max(3, min(10, int(params.get("duration") or 5))),
                "keep_original_sound": bool(params.get("keepAudio", True)),
            },
            token=api_token,
        )
        operation_id = _prediction_id(operation)
        result_url = _prediction_result_url(base_url, operation, operation_id)
        progress(12, f"GPTProto 已接单，prediction: {operation_id}")

        last_operation = operation
        while time.time() < deadline:
            cancel_marker = settings.upload_path / f"{task_id}.cancel"
            if cancel_marker.exists():
                raise GptProtoCancelled("task was cancelled")

            polled = _request_json(result_url, method="GET", token=api_token)
            last_operation = polled
            data = _prediction_data(polled)
            status = str(data.get("status") or polled.get("status") or "").lower()
            if status in {"failed", "error", "canceled", "cancelled"}:
                raise GptProtoError(_prediction_error(polled))
            video_url = _find_video_url(data.get("outputs") or data.get("output") or data.get("result") or data)
            if status in {"completed", "succeeded", "succeed", "success"} or video_url:
                if not video_url:
                    raise GptProtoError(f"GPTProto result did not contain a video URL: {json.dumps(polled)[:800]}")
                progress(92, "GPTProto 生成完成，正在下载结果")
                result = _download_result(video_url, task_id, model, operation_id)
                progress(98, "结果已下载，等待上传对象存储")
                return result

            progress(20, f"GPTProto {model_label} 正在生成视频转绘结果")
            time.sleep(interval)

        raise GptProtoError(f"GPTProto operation timed out: {json.dumps(last_operation)[:800]}")

    video_token = _query_token()
    if not video_token:
        raise GptProtoError("GPTPROTO_QUERY_TOKEN or GPTPROTO_API_KEY is not configured")

    submit_url = f"{base_url}/v1beta/models/{model}:predictLongRunning"
    operation_url = f"{base_url}/v1beta/models/{model}/operations"
    progress(8, f"正在提交 GPTProto {model_label} 视频转绘任务")
    operation = _request_json(
        submit_url,
        {
            "instances": [
                {
                    "prompt": prompt,
                    "video": {"uri": input_video_url},
                }
            ],
            "parameters": {
                "resolution": resolution,
                "aspectRatio": aspect_ratio,
            },
        },
        token=video_token,
    )
    operation_id = _operation_id(operation)
    progress(12, f"GPTProto 已接单，operation: {operation_id}")

    last_operation = operation
    while time.time() < deadline:
        cancel_marker = settings.upload_path / f"{task_id}.cancel"
        if cancel_marker.exists():
            raise GptProtoCancelled("task was cancelled")

        polled = _request_json(f"{operation_url}/{operation_id}", token=video_token)
        last_operation = polled
        if polled.get("done") is True:
            if polled.get("error"):
                raise GptProtoError(_operation_error(polled))
            video_url = _find_video_url(polled.get("response") or polled)
            if not video_url:
                raise GptProtoError(f"GPTProto result did not contain a video URL: {json.dumps(polled)[:800]}")
            progress(92, "GPTProto 生成完成，正在下载结果")
            result = _download_result(video_url, task_id, model, operation_id)
            progress(98, "结果已下载，等待上传对象存储")
            return result

        metadata_percent = _metadata_progress(polled)
        percent = 12 + int((metadata_percent or 0) * 0.75)
        progress(max(14, min(88, percent)), f"GPTProto {model_label} 正在生成视频转绘结果")
        time.sleep(interval)

    raise GptProtoError(f"GPTProto operation timed out: {json.dumps(last_operation)[:800]}")
