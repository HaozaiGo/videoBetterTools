#!/usr/bin/env python3
"""Submit a video enhancement job to the GPU worker API."""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path


DEFAULT_API_URL = "http://32.196.46.122:18081"
DEFAULT_API_KEY = "model-plaza-dev-gpu-key"


class GpuApiError(RuntimeError):
    pass


class GpuApiRequestError(GpuApiError):
    pass


class GpuJobCancelled(GpuApiError):
    pass


def _api_url(path: str) -> str:
    base = os.environ.get("MODEL_PLAZA_GPU_API_URL", DEFAULT_API_URL).rstrip("/")
    return f"{base}{path}"


def _headers() -> dict[str, str]:
    api_key = os.environ.get("MODEL_PLAZA_GPU_API_KEY", DEFAULT_API_KEY)
    return {"X-API-Key": api_key} if api_key else {}


def _request_json(request: urllib.request.Request, timeout: int = 30) -> dict:
    label = f"{request.get_method()} {request.full_url}"
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise GpuApiError(f"GPU API HTTP {exc.code} for {label}: {body}") from exc
    except (TimeoutError, urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
        raise GpuApiRequestError(f"GPU API request failed for {label} after {timeout}s: {exc}") from exc


def _cancel_requested() -> bool:
    cancel_file = os.environ.get("MODEL_PLAZA_CANCEL_FILE", "").strip()
    return bool(cancel_file and Path(cancel_file).exists())


def _cancel_job(job_id: str) -> None:
    request = urllib.request.Request(_api_url(f"/jobs/{job_id}/cancel"), headers=_headers(), method="POST")
    _request_json(request, timeout=30)


def _cancel_job_safely(job_id: str, reason: str) -> None:
    try:
        _cancel_job(job_id)
        print(f"GPU job {job_id} cancelled after {reason}", flush=True)
    except GpuApiError as exc:
        print(f"Failed to cancel GPU job {job_id} after {reason}: {exc}", flush=True)


def _progress_from_status(status: dict) -> tuple[int, str]:
    state = str(status.get("status") or "queued")
    fallback_percent = {"queued": 2, "processing": 15, "succeeded": 100, "failed": 0, "cancelled": 0}.get(state, 0)
    percent = int(status.get("progress_percent") or fallback_percent)
    stage = str(status.get("progress_stage") or state)
    return max(0, min(100, percent)), stage


def _stall_timeout_seconds() -> int:
    return max(0, int(os.environ.get("MODEL_PLAZA_GPU_STALL_TIMEOUT_SECONDS", "1800")))


def _progress_signature(status: dict) -> tuple[str, int, str]:
    percent, stage = _progress_from_status(status)
    return str(status.get("status") or "queued"), percent, stage


def _sync_progress(job_id: str, status: dict) -> None:
    provider_job_id = os.environ.get("MODEL_PLAZA_PROVIDER_JOB_ID", "").strip()
    callback_url = os.environ.get("MODEL_PLAZA_CALLBACK_URL", "").strip()
    if not provider_job_id or not callback_url:
        return
    percent, stage = _progress_from_status(status)
    payload = json.dumps(
        {
            "providerJobId": provider_job_id,
            "status": "processing",
            "callbackId": f"{provider_job_id}:{job_id}:progress:{percent}",
            "progressPercent": percent,
            "progressStage": stage,
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        callback_url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        _request_json(request, timeout=10)
    except GpuApiError as exc:
        print(f"Progress callback failed: {exc}", flush=True)


def _multipart(fields: dict[str, str], files: dict[str, Path]) -> tuple[bytes, str]:
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
    for name, path in files.items():
        chunks.extend(
            [
                f"--{boundary}\r\n".encode(),
                f'Content-Disposition: form-data; name="{name}"; filename="{path.name}"\r\n'.encode(),
                b"Content-Type: video/mp4\r\n\r\n",
                path.read_bytes(),
                b"\r\n",
            ]
        )
    chunks.append(f"--{boundary}--\r\n".encode())
    return b"".join(chunks), boundary


def _submit_job(
    input_path: Path,
    params_path: Path,
    input_url: str = "",
    result_upload_url: str = "",
    result_upload_headers: str = "{}",
    result_storage_key: str = "",
    result_url: str = "",
) -> str:
    job_type = os.environ.get("MODEL_PLAZA_GPU_JOB_TYPE", "enhance").strip() or "enhance"
    fields = {
        "job_type": job_type,
        "regions": "[]",
        "params": params_path.read_text(encoding="utf-8"),
    }
    files = {}
    if input_url:
        fields["input_url"] = input_url
    else:
        files["input_file"] = input_path
    if result_upload_url:
        fields["result_upload_url"] = result_upload_url
        fields["result_upload_headers"] = result_upload_headers
        fields["result_storage_key"] = result_storage_key
        fields["result_url"] = result_url
    body, boundary = _multipart(fields, files)
    headers = {
        **_headers(),
        "Content-Type": f"multipart/form-data; boundary={boundary}",
        "Content-Length": str(len(body)),
    }
    request = urllib.request.Request(_api_url("/jobs"), data=body, headers=headers, method="POST")
    payload = _request_json(request, timeout=int(os.environ.get("MODEL_PLAZA_GPU_SUBMIT_TIMEOUT", "120")))
    return str(payload["job_id"])


def _poll_job(job_id: str) -> dict:
    job_label = os.environ.get("MODEL_PLAZA_GPU_JOB_LABEL", "enhance").strip() or "enhance"
    interval = max(1, int(os.environ.get("MODEL_PLAZA_GPU_POLL_INTERVAL", "5")))
    timeout = max(interval, int(os.environ.get("MODEL_PLAZA_GPU_POLL_TIMEOUT", "7200")))
    max_status_failures = max(1, int(os.environ.get("MODEL_PLAZA_GPU_STATUS_FAILURES", "12")))
    stall_timeout = _stall_timeout_seconds()
    deadline = time.time() + timeout
    status_failures = 0
    last_progress_signature: tuple[str, int, str] | None = None
    last_progress_change_at = time.time()
    while time.time() < deadline:
        if _cancel_requested():
            _cancel_job(job_id)
            raise GpuJobCancelled(f"GPU {job_label} job cancelled: {job_id}")
        request = urllib.request.Request(_api_url(f"/jobs/{job_id}"), headers=_headers(), method="GET")
        try:
            status = _request_json(request, timeout=30)
        except GpuApiRequestError as exc:
            status_failures += 1
            print(f"GPU {job_label} job {job_id}: status check failed ({status_failures}/{max_status_failures}): {exc}", flush=True)
            if status_failures >= max_status_failures:
                _cancel_job_safely(job_id, "repeated status check failures")
                raise
            time.sleep(interval)
            continue
        status_failures = 0
        state = status.get("status")
        _sync_progress(job_id, status)
        print(f"GPU {job_label} job {job_id}: {state}", flush=True)
        if state == "succeeded":
            return status
        if state == "cancelled":
            raise GpuJobCancelled(f"GPU {job_label} job cancelled: {job_id}")
        if state == "failed":
            raise GpuApiError(f"GPU {job_label} job failed: {status.get('error') or 'unknown error'}")
        signature = _progress_signature(status)
        if signature != last_progress_signature:
            last_progress_signature = signature
            last_progress_change_at = time.time()
        elif state == "processing" and stall_timeout and time.time() - last_progress_change_at >= stall_timeout:
            _cancel_job_safely(job_id, f"stalled progress for {stall_timeout}s")
            percent, stage = _progress_from_status(status)
            raise GpuApiError(f"GPU {job_label} job stalled for {stall_timeout}s at {percent}%: {stage}")
        time.sleep(interval)
    _cancel_job_safely(job_id, f"poll timeout after {timeout}s")
    raise GpuApiError(f"GPU {job_label} job timed out after {timeout}s: {job_id}")


def _download_result(job_id: str, output_path: Path) -> None:
    request = urllib.request.Request(_api_url(f"/jobs/{job_id}/result"), headers=_headers(), method="GET")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    timeout = int(os.environ.get("MODEL_PLAZA_GPU_DOWNLOAD_TIMEOUT", "600"))
    retries = max(1, int(os.environ.get("MODEL_PLAZA_GPU_DOWNLOAD_RETRIES", "3")))
    backoff = max(0, int(os.environ.get("MODEL_PLAZA_GPU_DOWNLOAD_RETRY_BACKOFF_SECONDS", "5")))
    last_error: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                output_path.write_bytes(response.read())
                return
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            last_error = GpuApiError(f"GPU API result HTTP {exc.code} for job {job_id}: {body}")
            if exc.code < 500 or attempt >= retries:
                raise last_error from exc
        except (TimeoutError, urllib.error.URLError, OSError) as exc:
            last_error = GpuApiRequestError(f"GPU API result download failed for job {job_id} on attempt {attempt}/{retries} after {timeout}s: {exc}")
            if attempt >= retries:
                raise last_error from exc
        print(f"GPU API result download retrying for job {job_id} after attempt {attempt}/{retries}: {last_error}", flush=True)
        if backoff:
            time.sleep(backoff)


def _write_result_meta(status: dict, meta_path: Path) -> bool:
    result_url = str(status.get("result_url") or "")
    result_storage_key = str(status.get("result_storage_key") or "")
    if not result_url or not result_storage_key:
        return False
    meta_path.parent.mkdir(parents=True, exist_ok=True)
    meta_path.write_text(
        json.dumps(
            {
                "storage_key": result_storage_key,
                "url": result_url,
                "mime_type": status.get("result_mime_type") or "video/mp4",
                "size_bytes": int(status.get("result_size_bytes") or 0),
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return True


def main() -> None:
    input_path = Path(os.environ["MODEL_PLAZA_INPUT"]).expanduser().resolve()
    output_path = Path(os.environ["MODEL_PLAZA_OUTPUT"]).expanduser().resolve()
    params_path = Path(os.environ["MODEL_PLAZA_PARAMS"]).expanduser().resolve()
    input_url = os.environ.get("MODEL_PLAZA_INPUT_URL", "").strip()
    result_upload_url = os.environ.get("MODEL_PLAZA_RESULT_UPLOAD_URL", "").strip()
    result_upload_headers = os.environ.get("MODEL_PLAZA_RESULT_UPLOAD_HEADERS", "{}").strip() or "{}"
    result_storage_key = os.environ.get("MODEL_PLAZA_RESULT_STORAGE_KEY", "").strip()
    result_url = os.environ.get("MODEL_PLAZA_RESULT_URL", "").strip()
    meta_path_value = os.environ.get("MODEL_PLAZA_RESULT_META", "").strip()
    meta_path = Path(meta_path_value).expanduser().resolve() if meta_path_value else output_path.with_suffix(".result.json")

    job_id = _submit_job(
        input_path,
        params_path,
        input_url=input_url,
        result_upload_url=result_upload_url,
        result_upload_headers=result_upload_headers,
        result_storage_key=result_storage_key,
        result_url=result_url,
    )
    status = _poll_job(job_id)
    if _write_result_meta(status, meta_path):
        print(f"GPU result metadata written to {meta_path}", flush=True)
    else:
        _sync_progress(job_id, {"status": "processing", "progress_percent": 96, "progress_stage": "平台正在拉取远端结果"})
        _download_result(job_id, output_path)
        _sync_progress(job_id, {"status": "processing", "progress_percent": 97, "progress_stage": "结果已拉取，可预览"})
        _sync_progress(job_id, {"status": "processing", "progress_percent": 98, "progress_stage": "平台正在上传对象存储"})
        print(f"Downloaded GPU result to {output_path}", flush=True)


if __name__ == "__main__":
    main()
