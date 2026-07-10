from __future__ import annotations

import http.client
import json
import logging
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any

from app.config import settings
from app.storage import storage

logger = logging.getLogger("model_plaza.gpu_api")


class RemoteGpuError(RuntimeError):
    pass


class RemoteGpuUnavailableError(RemoteGpuError):
    pass


def _api_url(path: str) -> str:
    return f"{settings.model_plaza_gpu_api_url.rstrip('/')}{path}"


def _headers() -> dict[str, str]:
    return {"X-API-Key": settings.model_plaza_gpu_api_key} if settings.model_plaza_gpu_api_key else {}


def _request_json(request: urllib.request.Request, timeout: int = 30) -> dict:
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        if exc.code in {408, 425, 429, 500, 502, 503, 504}:
            raise RemoteGpuUnavailableError(f"GPU API HTTP {exc.code}: {body}") from exc
        raise RemoteGpuError(f"GPU API HTTP {exc.code}: {body}") from exc
    except (TimeoutError, urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
        raise RemoteGpuUnavailableError(f"GPU API request failed: {exc}") from exc


def _multipart(fields: dict[str, str]) -> tuple[bytes, str]:
    boundary = f"modelplaza-{uuid.uuid4().hex}"
    chunks: list[bytes] = []
    for name, value in fields.items():
        chunks.extend(
            [
                f"--{boundary}\r\n".encode(),
                f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode(),
                value.encode("utf-8"),
                b"\r\n",
            ]
        )
    chunks.append(f"--{boundary}--\r\n".encode())
    return b"".join(chunks), boundary


def can_submit_remote_video_job() -> bool:
    return bool(settings.model_plaza_gpu_api_url and storage.is_remote)


def wait_for_remote_storage(storage_key: str) -> None:
    timeout = max(0, int(settings.remote_storage_ready_timeout_seconds))
    interval = max(1, int(settings.remote_storage_ready_poll_seconds))
    deadline = time.monotonic() + timeout
    attempts = 0

    while True:
        attempts += 1
        if storage.remote_exists(storage_key):
            if attempts > 1:
                logger.info("Remote storage object became visible after %s checks: %s", attempts, storage_key)
            return
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise RemoteGpuUnavailableError(f"input asset is not available in remote storage after waiting {timeout}s")
        time.sleep(min(interval, remaining))


def submit_remote_video_job(
    *,
    job_type: str,
    input_storage_key: str,
    output_key: str,
    params: dict[str, Any],
    regions: list[dict] | None = None,
) -> dict:
    if not can_submit_remote_video_job():
        raise RemoteGpuError("remote GPU async submit is not configured")
    wait_for_remote_storage(input_storage_key)

    result_upload = storage.presign_upload(kind="video", storage_key=output_key)
    if result_upload.get("mode") != "tos-put":
        raise RemoteGpuError("remote GPU async submit requires TOS presigned PUT storage")

    fields = {
        "job_type": job_type,
        "regions": json.dumps(regions or [], ensure_ascii=False),
        "params": json.dumps(params, ensure_ascii=False),
        "input_url": storage.presign_download(input_storage_key),
        "result_upload_url": str(result_upload["uploadUrl"]),
        "result_upload_headers": json.dumps(result_upload.get("headers") or {}, ensure_ascii=False),
        "result_storage_key": output_key,
        "result_url": storage.public_url(output_key),
    }
    body, boundary = _multipart(fields)
    request = urllib.request.Request(
        _api_url("/jobs"),
        data=body,
        headers={
            **_headers(),
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "Content-Length": str(len(body)),
        },
        method="POST",
    )
    payload = _request_json(request, timeout=120)
    remote_job_id = str(payload["job_id"])
    return {
        "remote_job_id": remote_job_id,
        "job_type": job_type,
        "storage_key": output_key,
        "url": storage.public_url(output_key),
        "mime_type": "video/mp4",
        "size_bytes": 0,
    }


def get_remote_video_job(job_id: str) -> dict:
    request = urllib.request.Request(_api_url(f"/jobs/{job_id}"), headers=_headers(), method="GET")
    return _request_json(request, timeout=30)


def cancel_remote_video_job(job_id: str) -> None:
    request = urllib.request.Request(_api_url(f"/jobs/{job_id}/cancel"), headers=_headers(), method="POST")
    _request_json(request, timeout=30)


def download_remote_video_result(job_id: str, output_path: Path) -> None:
    request = urllib.request.Request(_api_url(f"/jobs/{job_id}/result"), headers=_headers(), method="GET")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    last_error: Exception | None = None
    for attempt in range(1, 4):
        try:
            with urllib.request.urlopen(request, timeout=600) as response:
                output_path.write_bytes(response.read())
                return
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            if exc.code >= 500 or exc.code in {408, 425, 429}:
                last_error = RemoteGpuUnavailableError(f"GPU API result HTTP {exc.code} on attempt {attempt}/3: {body}")
            else:
                last_error = RemoteGpuError(f"GPU API result HTTP {exc.code}: {body}")
            if not isinstance(last_error, RemoteGpuUnavailableError) or attempt >= 3:
                raise last_error from exc
        except (TimeoutError, urllib.error.URLError, OSError, http.client.IncompleteRead) as exc:
            last_error = RemoteGpuUnavailableError(f"GPU API result download failed on attempt {attempt}/3: {exc}")
            if attempt >= 3:
                raise last_error from exc
        time.sleep(5)
