#!/usr/bin/env python3
"""HTTP GPU worker for video model jobs.

服务部署在 GPU 服务器上，负责接收本地平台上传的视频和参数，然后异步执行
具体模型 runner。状态写入磁盘，服务重启后仍能查询已完成任务的结果。
"""

from __future__ import annotations

import json
import os
import secrets
import shutil
import subprocess
import threading
import time
import urllib.parse
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from multiprocessing import get_context
from pathlib import Path
from typing import Annotated
from urllib.parse import quote

from fastapi import FastAPI, File, Form, Header, HTTPException, UploadFile
from fastapi.responses import FileResponse


ROOT = Path(os.environ.get("MODEL_PLAZA_VIDEO_ROOT", "/data1/model-plaza-video-worker")).resolve()
JOBS_ROOT = Path(os.environ.get("MODEL_PLAZA_GPU_JOBS_ROOT", str(ROOT / "work" / "api-jobs"))).resolve()
LOGS_ROOT = Path(os.environ.get("MODEL_PLAZA_GPU_LOGS_ROOT", str(ROOT / "logs"))).resolve()
PROPAINTER_RUNNER_PATH = Path(os.environ.get("MODEL_PLAZA_PROPAINTER_RUNNER", str(ROOT / "scripts" / "propainter_runner.py"))).resolve()
ENHANCE_RUNNER_PATH = Path(os.environ.get("MODEL_PLAZA_ENHANCE_RUNNER", str(ROOT / "scripts" / "video_enhance_runner.py"))).resolve()
TRANSLATE_RUNNER_PATH = Path(os.environ.get("MODEL_PLAZA_TRANSLATE_RUNNER", str(ROOT / "scripts" / "video_translate_runner.py"))).resolve()
PYTHON_PATH = os.environ.get("PROPAINTER_PYTHON", "/data1/conda/miniconda3/envs/video-inpaint/bin/python")
API_KEY = os.environ.get("MODEL_PLAZA_GPU_API_KEY", "model-plaza-dev-gpu-key")
MAX_WORKERS = max(1, int(os.environ.get("MODEL_PLAZA_GPU_MAX_WORKERS", "1")))

app = FastAPI(title="片刻修AI GPU Worker")
executor = ThreadPoolExecutor(max_workers=MAX_WORKERS)
running_processes: dict[str, subprocess.Popen] = {}
running_processes_lock = threading.Lock()
recover_lock = threading.Lock()


def _check_auth(api_key: str | None) -> None:
    if API_KEY and not secrets.compare_digest(api_key or "", API_KEY):
        raise HTTPException(status_code=401, detail="invalid api key")


def _job_dir(job_id: str) -> Path:
    if not job_id.replace("-", "").isalnum():
        raise HTTPException(status_code=400, detail="invalid job id")
    return JOBS_ROOT / job_id


def _status_path(job_id: str) -> Path:
    return _job_dir(job_id) / "status.json"


def _progress_path(job_id: str) -> Path:
    return _job_dir(job_id) / "progress.json"


def _write_status(job_id: str, **updates) -> dict:
    status_path = _status_path(job_id)
    current = {}
    if status_path.exists():
        current = json.loads(status_path.read_text(encoding="utf-8"))
    current.update(updates)
    current["updated_at"] = time.time()
    status_path.parent.mkdir(parents=True, exist_ok=True)
    status_path.write_text(json.dumps(current, ensure_ascii=False, indent=2), encoding="utf-8")
    return current


def _read_status(job_id: str) -> dict:
    status_path = _status_path(job_id)
    if not status_path.exists():
        raise HTTPException(status_code=404, detail="job not found")
    status = json.loads(status_path.read_text(encoding="utf-8"))
    progress_path = _progress_path(job_id)
    if progress_path.exists() and status.get("status") not in {"succeeded", "failed", "cancelled"}:
        try:
            progress = json.loads(progress_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            progress = {}
        if "progress_percent" in progress:
            status["progress_percent"] = progress["progress_percent"]
        if "progress_stage" in progress:
            status["progress_stage"] = progress["progress_stage"]
    return status


def _tos_config() -> dict[str, str]:
    ak = os.environ.get("VOLCENGINE_TOS_AK") or os.environ.get("VOLCENGINE_OPENAPI_AK") or ""
    sk = os.environ.get("VOLCENGINE_TOS_SK") or os.environ.get("VOLCENGINE_OPENAPI_SK") or ""
    bucket = os.environ.get("VOLCENGINE_TOS_BUCKET", "")
    endpoint = os.environ.get("VOLCENGINE_TOS_ENDPOINT", "tos-cn-guangzhou.volces.com")
    region = os.environ.get("VOLCENGINE_TOS_REGION", "cn-guangzhou")
    public_base_url = os.environ.get("VOLCENGINE_TOS_PUBLIC_BASE_URL") or (f"https://{bucket}.{endpoint}" if bucket else "")
    return {
        "ak": ak,
        "sk": sk,
        "bucket": bucket,
        "endpoint": endpoint,
        "region": region,
        "public_base_url": public_base_url.rstrip("/"),
    }


def _tos_enabled() -> bool:
    config = _tos_config()
    return all(config[key] for key in ("ak", "sk", "bucket", "endpoint", "region", "public_base_url"))


def _upload_result_to_tos(job_id: str, output_path: Path) -> dict | None:
    if not _tos_enabled():
        return None

    import tos

    config = _tos_config()
    now = datetime.now(timezone.utc)
    object_key = f"model-plaza/output/videos/{now:%Y/%m/%d}/{job_id}.mp4"
    client = tos.TosClientV2(config["ak"], config["sk"], config["endpoint"], config["region"])
    client.put_object_from_file(config["bucket"], object_key, str(output_path))
    encoded_key = quote(object_key, safe="/")
    return {
        "result_storage_key": object_key,
        "result_url": f"{config['public_base_url']}/{encoded_key}",
        "result_mime_type": "video/mp4",
        "result_size_bytes": output_path.stat().st_size,
    }


def _result_upload_config(job_id: str) -> dict:
    config_path = _job_dir(job_id) / "result-upload.json"
    if not config_path.exists():
        return {}
    return json.loads(config_path.read_text(encoding="utf-8"))


def _upload_result_to_presigned_url(job_id: str, output_path: Path) -> dict | None:
    config = _result_upload_config(job_id)
    upload_url = str(config.get("upload_url") or "")
    storage_key = str(config.get("storage_key") or "")
    result_url = str(config.get("url") or "")
    if not upload_url or not storage_key or not result_url:
        return None

    import requests

    headers = dict(config.get("headers") or {})
    headers.setdefault("Content-Type", "video/mp4")
    headers["Content-Length"] = str(output_path.stat().st_size)
    with output_path.open("rb") as file:
        response = requests.put(
            upload_url,
            data=file,
            headers=headers,
            timeout=int(os.environ.get("MODEL_PLAZA_GPU_RESULT_UPLOAD_TIMEOUT", "600")),
        )
    if response.status_code >= 400:
        raise RuntimeError(f"presigned upload failed with HTTP {response.status_code}")
    response.raise_for_status()
    return {
        "result_storage_key": storage_key,
        "result_url": result_url,
        "result_mime_type": "video/mp4",
        "result_size_bytes": output_path.stat().st_size,
    }


def _upload_result(job_id: str, output_path: Path) -> dict:
    """Upload directly to TOS when configured, with backend presigned PUT as compatibility fallback."""
    upload_errors: list[str] = []
    try:
        result_updates = _upload_result_to_tos(job_id, output_path)
        if result_updates:
            return result_updates
    except Exception as exc:
        upload_errors.append(f"tos upload failed: {exc}")

    try:
        result_updates = _upload_result_to_presigned_url(job_id, output_path)
        if result_updates:
            return result_updates
    except Exception as exc:
        upload_errors.append(f"presigned upload failed: {exc}")

    if upload_errors:
        raise RuntimeError("; ".join(upload_errors))
    return {}


def _upload_result_worker(job_id: str, output_path: str, queue) -> None:
    try:
        queue.put({"ok": True, "result": _upload_result(job_id, Path(output_path))})
    except Exception as exc:
        queue.put({"ok": False, "error": str(exc)})


def _upload_result_with_deadline(job_id: str, output_path: Path) -> dict:
    timeout = int(os.environ.get("MODEL_PLAZA_GPU_RESULT_UPLOAD_TOTAL_TIMEOUT", "900"))
    context = get_context("spawn")
    queue = context.Queue()
    process = context.Process(target=_upload_result_worker, args=(job_id, str(output_path), queue))
    process.start()
    process.join(timeout)
    if process.is_alive():
        process.terminate()
        process.join(10)
        raise RuntimeError(f"result upload exceeded total timeout {timeout}s")
    if queue.empty():
        raise RuntimeError("result upload worker exited without a result")
    payload = queue.get()
    if not payload.get("ok"):
        raise RuntimeError(str(payload.get("error") or "result upload failed"))
    return dict(payload.get("result") or {})


def _runner_for_job_type(job_type: str) -> Path:
    if job_type == "propainter":
        return PROPAINTER_RUNNER_PATH
    if job_type == "enhance":
        return ENHANCE_RUNNER_PATH
    if job_type == "translate":
        return TRANSLATE_RUNNER_PATH
    raise RuntimeError(f"Unsupported job type: {job_type}")


def _run_model_job(job_id: str) -> None:
    job_dir = _job_dir(job_id)
    status = _read_status(job_id)
    if status.get("status") == "cancelled":
        return
    job_type = str(status.get("job_type") or "propainter")
    input_path = job_dir / "input.mp4"
    output_path = job_dir / "output.mp4"
    regions_path = job_dir / "regions.json"
    params_path = job_dir / "params.json"
    work_dir = job_dir / "runner-work"
    log_path = LOGS_ROOT / f"{job_type}-api-{job_id}.log"

    _write_status(job_id, status="processing", started_at=time.time(), log_path=str(log_path), progress_percent=8, progress_stage="远端 GPU 已领取任务")
    command = [
        PYTHON_PATH,
        str(_runner_for_job_type(job_type)),
        "--input",
        str(input_path),
        "--output",
        str(output_path),
        "--params",
        str(params_path),
        "--workdir",
        str(work_dir),
    ]
    if job_type in {"propainter", "enhance"}:
        command[6:6] = ["--regions", str(regions_path)]
    LOGS_ROOT.mkdir(parents=True, exist_ok=True)
    try:
        with log_path.open("w", encoding="utf-8") as log_file:
            env = {**os.environ, "MODEL_PLAZA_PROGRESS_FILE": str(_progress_path(job_id))}
            process = subprocess.Popen(command, stdout=log_file, stderr=subprocess.STDOUT, text=True, env=env)
            with running_processes_lock:
                running_processes[job_id] = process
            return_code = process.wait()
            with running_processes_lock:
                running_processes.pop(job_id, None)
            if _read_status(job_id).get("status") == "cancelled":
                return
            if return_code != 0:
                raise subprocess.CalledProcessError(return_code, command)
        if not output_path.exists():
            raise RuntimeError("runner completed but output.mp4 was not created")
        _write_status(job_id, status="uploading", upload_started_at=time.time())
        result_updates = _upload_result_with_deadline(job_id, output_path)
        _write_status(
            job_id,
            status="succeeded",
            completed_at=time.time(),
            result_path=str(output_path),
            progress_percent=100,
            progress_stage="远端处理完成",
            **result_updates,
        )
    except Exception as exc:
        with running_processes_lock:
            running_processes.pop(job_id, None)
        if _read_status(job_id).get("status") == "cancelled":
            return
        _write_status(job_id, status="failed", completed_at=time.time(), error=str(exc), log_path=str(log_path))


def _job_age_seconds(status: dict) -> float:
    timestamp = float(status.get("updated_at") or status.get("created_at") or 0)
    if timestamp <= 0:
        return 0
    return max(0, time.time() - timestamp)


def _recover_finished_job(job_id: str, output_path: Path) -> None:
    try:
        _write_status(job_id, status="uploading", upload_started_at=time.time(), result_path=str(output_path), error="")
        result_updates = _upload_result_with_deadline(job_id, output_path)
        _write_status(
            job_id,
            status="succeeded",
            completed_at=time.time(),
            result_path=str(output_path),
            **result_updates,
        )
    except Exception as exc:
        _write_status(
            job_id,
            status="failed",
            completed_at=time.time(),
            result_path=str(output_path),
            error=f"recovery upload failed: {exc}",
        )


def _recover_stale_jobs() -> None:
    if os.environ.get("MODEL_PLAZA_GPU_RECOVER_INCOMPLETE_JOBS", "1").lower() in {"0", "false", "no"}:
        return
    if not JOBS_ROOT.exists():
        return

    with recover_lock:
        max_age_seconds = int(os.environ.get("MODEL_PLAZA_GPU_RECOVER_MAX_AGE_SECONDS", str(24 * 60 * 60)))
        recoverable_statuses = {"queued", "processing", "uploading"}
        for status_path in sorted(JOBS_ROOT.glob("*/status.json")):
            job_id = status_path.parent.name
            try:
                status = json.loads(status_path.read_text(encoding="utf-8"))
            except Exception:
                continue
            if status.get("status") not in recoverable_statuses:
                continue
            if max_age_seconds > 0 and _job_age_seconds(status) > max_age_seconds:
                _write_status(
                    job_id,
                    status="failed",
                    completed_at=time.time(),
                    error="stale incomplete job was not recovered after restart",
                )
                continue

            output_path = status_path.parent / "output.mp4"
            input_path = status_path.parent / "input.mp4"
            if output_path.exists():
                _recover_finished_job(job_id, output_path)
            elif input_path.exists():
                _write_status(job_id, status="queued", recovered_at=time.time(), error="")
                executor.submit(_run_model_job, job_id)
            else:
                _write_status(
                    job_id,
                    status="failed",
                    completed_at=time.time(),
                    error="incomplete job has no input.mp4 to recover",
                )


def _download_input_url(input_url: str, output_path: Path) -> None:
    parsed = urllib.parse.urlparse(input_url)
    if parsed.scheme not in {"http", "https"}:
        raise HTTPException(status_code=400, detail="input_url must be http or https")
    request = urllib.request.Request(input_url, headers={"User-Agent": "model-plaza-gpu-worker/1.0"})
    with urllib.request.urlopen(request, timeout=int(os.environ.get("MODEL_PLAZA_GPU_INPUT_DOWNLOAD_TIMEOUT", "600"))) as response:
        with output_path.open("wb") as output_file:
            shutil.copyfileobj(response, output_file)


@app.on_event("startup")
def recover_incomplete_jobs_on_startup() -> None:
    # 启动恢复可能包含大文件补传，不能阻塞 /health 和新任务提交。
    threading.Thread(target=_recover_stale_jobs, name="recover-stale-gpu-jobs", daemon=True).start()


@app.get("/health")
def health() -> dict:
    return {"ok": True, "max_workers": MAX_WORKERS}


@app.post("/jobs", status_code=202)
async def create_job(
    regions: Annotated[str, Form()],
    params: Annotated[str, Form()] = "{}",
    job_type: Annotated[str, Form()] = "propainter",
    input_file: Annotated[UploadFile | None, File()] = None,
    input_url: Annotated[str | None, Form()] = None,
    result_upload_url: Annotated[str | None, Form()] = None,
    result_upload_headers: Annotated[str, Form()] = "{}",
    result_storage_key: Annotated[str | None, Form()] = None,
    result_url: Annotated[str | None, Form()] = None,
    x_api_key: Annotated[str | None, Header(alias="X-API-Key")] = None,
) -> dict:
    _check_auth(x_api_key)
    try:
        regions_json = json.loads(regions)
        params_json = json.loads(params)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail="regions/params must be valid JSON") from exc
    job_type = job_type.lower().strip()
    if job_type not in {"propainter", "enhance", "translate"}:
        raise HTTPException(status_code=400, detail="unsupported job type")
    if not isinstance(regions_json, list):
        raise HTTPException(status_code=400, detail="regions must be a JSON array")
    if job_type == "propainter" and not regions_json:
        raise HTTPException(status_code=400, detail="regions must be a non-empty JSON array")
    if not isinstance(params_json, dict):
        raise HTTPException(status_code=400, detail="params must be a JSON object")

    job_id = uuid.uuid4().hex
    job_dir = _job_dir(job_id)
    if job_dir.exists():
        shutil.rmtree(job_dir)
    job_dir.mkdir(parents=True, exist_ok=True)
    input_path = job_dir / "input.mp4"
    if input_url:
        _download_input_url(input_url, input_path)
    elif input_file:
        input_path.write_bytes(await input_file.read())
    else:
        raise HTTPException(status_code=400, detail="input_file or input_url is required")
    if result_upload_url:
        try:
            parsed_headers = json.loads(result_upload_headers or "{}")
        except json.JSONDecodeError as exc:
            raise HTTPException(status_code=400, detail="result_upload_headers must be valid JSON") from exc
        if not isinstance(parsed_headers, dict):
            raise HTTPException(status_code=400, detail="result_upload_headers must be a JSON object")
        (job_dir / "result-upload.json").write_text(
            json.dumps(
                {
                    "upload_url": result_upload_url,
                    "headers": parsed_headers,
                    "storage_key": result_storage_key or "",
                    "url": result_url or "",
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
    (job_dir / "regions.json").write_text(json.dumps(regions_json, ensure_ascii=False), encoding="utf-8")
    (job_dir / "params.json").write_text(json.dumps(params_json, ensure_ascii=False), encoding="utf-8")
    _write_status(job_id, status="queued", job_type=job_type, created_at=time.time(), result_path="", error="", progress_percent=0, progress_stage="远端任务排队中")
    executor.submit(_run_model_job, job_id)
    return {"job_id": job_id, "status": "queued", "status_url": f"/jobs/{job_id}", "result_url": f"/jobs/{job_id}/result"}


@app.get("/jobs/{job_id}")
def get_job(job_id: str, x_api_key: Annotated[str | None, Header(alias="X-API-Key")] = None) -> dict:
    _check_auth(x_api_key)
    return _read_status(job_id)


@app.post("/jobs/{job_id}/cancel")
def cancel_job(job_id: str, x_api_key: Annotated[str | None, Header(alias="X-API-Key")] = None) -> dict:
    _check_auth(x_api_key)
    status = _read_status(job_id)
    if status.get("status") in {"succeeded", "failed", "cancelled"}:
        return status
    cancelled_status = _write_status(job_id, status="cancelled", completed_at=time.time(), error="USER_CANCELLED", progress_percent=0, progress_stage="远端任务已取消")
    with running_processes_lock:
        process = running_processes.get(job_id)
    if process is not None and process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=int(os.environ.get("MODEL_PLAZA_GPU_CANCEL_GRACE_SECONDS", "8")))
        except subprocess.TimeoutExpired:
            process.kill()
    return cancelled_status


@app.get("/jobs/{job_id}/result")
def get_result(job_id: str, x_api_key: Annotated[str | None, Header(alias="X-API-Key")] = None):
    _check_auth(x_api_key)
    status = _read_status(job_id)
    if status.get("status") != "succeeded":
        raise HTTPException(status_code=409, detail="job is not succeeded")
    result_path = Path(status.get("result_path") or "")
    if not result_path.exists():
        raise HTTPException(status_code=404, detail="result not found")
    return FileResponse(result_path, media_type="video/mp4", filename=f"{job_id}.mp4")
