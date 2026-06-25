#!/usr/bin/env python3
"""HTTP GPU worker for video model jobs.

服务部署在 GPU 服务器上，负责接收本地平台上传的视频和参数，然后异步执行
具体模型 runner。状态写入磁盘，服务重启后仍能查询已完成任务的结果。
"""

from __future__ import annotations

import json
import os
import secrets
import signal
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
from queue import Queue
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
UPLOAD_RESULTS = os.environ.get("MODEL_PLAZA_GPU_UPLOAD_RESULTS", "0").lower() not in {"0", "false", "no"}
TERMINAL_STATUSES = {"succeeded", "failed", "cancelled"}


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except ValueError:
        return default


CLEANUP_ENABLED = os.environ.get("MODEL_PLAZA_GPU_CLEANUP_ENABLED", "1").lower() not in {"0", "false", "no"}
CLEANUP_INTERVAL_SECONDS = max(60, _env_int("MODEL_PLAZA_GPU_CLEANUP_INTERVAL_SECONDS", 60 * 60))
CLEANUP_SUCCESS_TTL_SECONDS = max(0, _env_int("MODEL_PLAZA_GPU_CLEANUP_SUCCESS_TTL_SECONDS", 24 * 60 * 60))
CLEANUP_FAILED_TTL_SECONDS = max(0, _env_int("MODEL_PLAZA_GPU_CLEANUP_FAILED_TTL_SECONDS", 48 * 60 * 60))
CLEANUP_RUNNER_WORK_TTL_SECONDS = max(0, _env_int("MODEL_PLAZA_GPU_CLEANUP_RUNNER_WORK_TTL_SECONDS", 60 * 60))
CLEANUP_DISK_HIGH_WATERMARK_PERCENT = max(1, min(100, _env_int("MODEL_PLAZA_GPU_CLEANUP_DISK_HIGH_WATERMARK_PERCENT", 80)))
CLEANUP_DISK_LOW_WATERMARK_PERCENT = max(1, min(CLEANUP_DISK_HIGH_WATERMARK_PERCENT, _env_int("MODEL_PLAZA_GPU_CLEANUP_DISK_LOW_WATERMARK_PERCENT", 70)))
CLEANUP_DISK_MIN_AGE_SECONDS = max(0, _env_int("MODEL_PLAZA_GPU_CLEANUP_DISK_MIN_AGE_SECONDS", 60 * 60))
GPU_PREFLIGHT_ENABLED = os.environ.get("MODEL_PLAZA_GPU_PREFLIGHT_ENABLED", "1").lower() not in {"0", "false", "no"}
GPU_STALL_TIMEOUT_SECONDS = max(0, _env_int("MODEL_PLAZA_GPU_STALL_TIMEOUT_SECONDS", 30 * 60))
GPU_WATCHDOG_INTERVAL_SECONDS = max(5, _env_int("MODEL_PLAZA_GPU_WATCHDOG_INTERVAL_SECONDS", 30))
GPU_CANCEL_GRACE_SECONDS = max(1, _env_int("MODEL_PLAZA_GPU_CANCEL_GRACE_SECONDS", 8))
GPU_RECOVER_STALE_PROCESSING_TIMEOUT_SECONDS = max(0, _env_int("MODEL_PLAZA_GPU_RECOVER_STALE_PROCESSING_TIMEOUT_SECONDS", GPU_STALL_TIMEOUT_SECONDS))


def _csv_values(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


GPU_DEVICE_IDS = _csv_values(
    os.environ.get("MODEL_PLAZA_GPU_DEVICE_IDS")
    or os.environ.get("CUDA_VISIBLE_DEVICES")
    or "0"
)
GPU_WORKERS_PER_DEVICE = max(1, int(os.environ.get("MODEL_PLAZA_GPU_WORKERS_PER_DEVICE", "1")))
GPU_SLOT_CAPACITY = max(1, len(GPU_DEVICE_IDS) * GPU_WORKERS_PER_DEVICE)
MAX_WORKERS = max(1, int(os.environ.get("MODEL_PLAZA_GPU_MAX_WORKERS", str(GPU_SLOT_CAPACITY))))

app = FastAPI(title="片刻修AI GPU Worker")
executor = ThreadPoolExecutor(max_workers=MAX_WORKERS)
gpu_slots: Queue[str] = Queue(maxsize=GPU_SLOT_CAPACITY)
for _slot_index in range(GPU_WORKERS_PER_DEVICE):
    for _gpu_device_id in GPU_DEVICE_IDS:
        gpu_slots.put(_gpu_device_id)
running_processes: dict[str, subprocess.Popen] = {}
running_gpu_devices: dict[str, str] = {}
running_progress_snapshots: dict[str, tuple[tuple[str, int, str], float, float]] = {}
running_processes_lock = threading.Lock()
recover_lock = threading.Lock()
cleanup_lock = threading.Lock()


def _run_command(command: list[str], timeout: int = 5) -> str:
    result = subprocess.run(command, check=True, capture_output=True, text=True, timeout=timeout)
    return result.stdout.strip()


def _parse_gpu_csv(output: str) -> list[dict]:
    rows: list[dict] = []
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    if len(lines) <= 1:
        return rows
    headers = [header.strip() for header in lines[0].split(",")]
    for line in lines[1:]:
        values = [value.strip() for value in line.split(",")]
        row = dict(zip(headers, values))
        rows.append(
            {
                "index": row.get("index", ""),
                "name": row.get("name", ""),
                "utilizationGpuPercent": _number_from_smi_value(row.get("utilization.gpu [%]", "")),
                "utilizationMemoryPercent": _number_from_smi_value(row.get("utilization.memory [%]", "")),
                "memoryUsedMiB": _number_from_smi_value(row.get("memory.used [MiB]", "")),
                "memoryTotalMiB": _number_from_smi_value(row.get("memory.total [MiB]", "")),
                "temperatureGpu": _number_from_smi_value(row.get("temperature.gpu", "")),
                "powerDrawW": _number_from_smi_value(row.get("power.draw [W]", "")),
            }
        )
    return rows


def _empty_gpu_metric(gpu_device: str, running_by_gpu: dict[str, int]) -> dict:
    return {
        "index": gpu_device,
        "name": "GPU metrics unavailable",
        "utilizationGpuPercent": 0,
        "utilizationMemoryPercent": 0,
        "memoryUsedMiB": 0,
        "memoryTotalMiB": 0,
        "temperatureGpu": 0,
        "powerDrawW": 0,
        "workerSlotsUsed": running_by_gpu.get(gpu_device, 0),
        "workerSlotsTotal": GPU_WORKERS_PER_DEVICE,
    }


def _attach_worker_slots(gpus: list[dict], running_by_gpu: dict[str, int]) -> list[dict]:
    for gpu in gpus:
        gpu["workerSlotsUsed"] = running_by_gpu.get(str(gpu["index"]), 0)
        gpu["workerSlotsTotal"] = GPU_WORKERS_PER_DEVICE
    return gpus


def _query_configured_gpu_metrics(running_by_gpu: dict[str, int]) -> tuple[list[dict], str]:
    query_args = [
        "--query-gpu=index,name,utilization.gpu,utilization.memory,memory.used,memory.total,temperature.gpu,power.draw",
        "--format=csv",
    ]
    try:
        query_output = _run_command(["nvidia-smi", f"--id={','.join(GPU_DEVICE_IDS)}", *query_args], timeout=5)
        return _attach_worker_slots(_parse_gpu_csv(query_output), running_by_gpu), ""
    except Exception as exc:
        configured_error = str(exc)

    try:
        query_output = _run_command(["nvidia-smi", *query_args], timeout=5)
        visible_gpus = _parse_gpu_csv(query_output)
    except Exception as exc:
        error = f"{configured_error}; fallback without --id failed: {exc}"
        return [_empty_gpu_metric(gpu_device, running_by_gpu) for gpu_device in GPU_DEVICE_IDS], error

    if len(visible_gpus) == len(GPU_DEVICE_IDS):
        remapped = []
        for gpu_device, metric in zip(GPU_DEVICE_IDS, visible_gpus):
            remapped.append({**metric, "index": gpu_device})
        return _attach_worker_slots(remapped, running_by_gpu), configured_error

    visible_by_index = {str(metric.get("index", "")): metric for metric in visible_gpus}
    metrics = []
    for gpu_device in GPU_DEVICE_IDS:
        metric = visible_by_index.get(gpu_device)
        metrics.append({**metric} if metric else _empty_gpu_metric(gpu_device, running_by_gpu))
    return _attach_worker_slots(metrics, running_by_gpu), configured_error


def _number_from_smi_value(value: str) -> float:
    cleaned = value.replace("%", "").replace("MiB", "").replace("W", "").strip()
    try:
        parsed = float(cleaned)
    except ValueError:
        return 0
    return int(parsed) if parsed.is_integer() else parsed


def _gpu_preflight_error() -> str:
    if not GPU_PREFLIGHT_ENABLED:
        return ""
    running_by_gpu = {gpu_device: 0 for gpu_device in GPU_DEVICE_IDS}
    gpus, metrics_error = _query_configured_gpu_metrics(running_by_gpu)
    if metrics_error:
        return metrics_error
    if len(gpus) < len(GPU_DEVICE_IDS):
        return f"only {len(gpus)}/{len(GPU_DEVICE_IDS)} configured GPU metrics are available"
    unavailable = [
        str(gpu.get("index") or "")
        for gpu in gpus
        if str(gpu.get("name") or "") == "GPU metrics unavailable" or float(gpu.get("memoryTotalMiB") or 0) <= 0
    ]
    if unavailable:
        return f"configured GPU metrics unavailable: {','.join(unavailable)}"
    return ""


def _require_gpu_preflight() -> None:
    error = _gpu_preflight_error()
    if error:
        raise RuntimeError(f"GPU preflight failed: {error}")


def _progress_signature_from_status(status: dict) -> tuple[str, int, str]:
    return (
        str(status.get("status") or ""),
        int(status.get("progress_percent") or 0),
        str(status.get("progress_stage") or ""),
    )


def _path_mtime(path: Path | None) -> float:
    if path is None:
        return 0.0
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def _job_activity_heartbeat(job_id: str, status: dict) -> float:
    log_path_value = str(status.get("log_path") or "")
    log_path = Path(log_path_value) if log_path_value else None
    return max(
        float(status.get("updated_at") or 0),
        _path_mtime(_progress_path(job_id)),
        _path_mtime(log_path),
    )


def _status_running_age_seconds(status: dict) -> float:
    timestamp = float(status.get("started_at") or status.get("created_at") or status.get("updated_at") or 0)
    if timestamp <= 0:
        return 0
    return max(0, time.time() - timestamp)


def _process_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _child_pids(parent_pid: int) -> list[int]:
    children_by_parent: dict[int, list[int]] = {}
    try:
        for proc_path in Path("/proc").iterdir():
            if not proc_path.name.isdigit():
                continue
            stat = (proc_path / "stat").read_text(encoding="utf-8", errors="replace")
            after_name = stat.rsplit(")", 1)[-1].strip().split()
            if len(after_name) >= 2:
                pid = int(proc_path.name)
                ppid = int(after_name[1])
                children_by_parent.setdefault(ppid, []).append(pid)
    except Exception:
        return []

    descendants: list[int] = []
    stack = list(children_by_parent.get(parent_pid, []))
    while stack:
        pid = stack.pop()
        descendants.append(pid)
        stack.extend(children_by_parent.get(pid, []))
    return descendants


def _job_process_pids(job_id: str) -> list[int]:
    marker = f"/api-jobs/{job_id}/"
    pids: list[int] = []
    for proc_path in Path("/proc").iterdir():
        if not proc_path.name.isdigit():
            continue
        try:
            cmdline = (proc_path / "cmdline").read_bytes().replace(b"\x00", b" ").decode("utf-8", errors="replace")
        except Exception:
            continue
        if marker in cmdline or job_id in cmdline:
            pids.append(int(proc_path.name))
    return pids


def _terminate_job_processes(job_id: str, process: subprocess.Popen | None = None, reason: str = "") -> None:
    deadline = time.time() + GPU_CANCEL_GRACE_SECONDS
    base_pids = set(_job_process_pids(job_id))
    if process is not None:
        base_pids.add(process.pid)
    all_pids = set(base_pids)
    for pid in list(base_pids):
        all_pids.update(_child_pids(pid))

    if reason:
        print(f"Terminating GPU job {job_id} processes after {reason}: {sorted(all_pids)}", flush=True)

    def send(sig: signal.Signals) -> None:
        for pid in sorted(all_pids, reverse=True):
            try:
                os.killpg(pid, sig)
            except Exception:
                try:
                    os.kill(pid, sig)
                except ProcessLookupError:
                    pass
                except Exception:
                    pass

    send(signal.SIGTERM)
    while time.time() < deadline:
        alive = [pid for pid in all_pids if _process_alive(pid)]
        if not alive:
            break
        time.sleep(0.2)
    if any(_process_alive(pid) for pid in all_pids):
        send(signal.SIGKILL)


def _running_jobs_snapshot() -> list[dict]:
    jobs: list[dict] = []
    now = time.time()
    if not JOBS_ROOT.exists():
        return jobs
    for status_path in sorted(JOBS_ROOT.glob("*/status.json")):
        job_id = status_path.parent.name
        try:
            status = _read_status(job_id)
        except Exception:
            continue
        if status.get("status") not in {"queued", "processing", "uploading"}:
            continue
        started_at = float(status.get("started_at") or status.get("created_at") or now)
        jobs.append(
            {
                "id": job_id,
                "status": status.get("status", ""),
                "jobType": status.get("job_type", ""),
                "assignedGpu": status.get("assigned_gpu", ""),
                "progressPercent": int(status.get("progress_percent") or 0),
                "progressStage": status.get("progress_stage", ""),
                "runningSeconds": max(0, int(now - started_at)),
                "logPath": status.get("log_path", ""),
            }
        )
    return jobs


def _gpu_metrics() -> dict:
    running_by_gpu = {gpu_device: 0 for gpu_device in GPU_DEVICE_IDS}
    with running_processes_lock:
        for gpu_device in running_gpu_devices.values():
            running_by_gpu[gpu_device] = running_by_gpu.get(gpu_device, 0) + 1
    gpus, gpu_metrics_error = _query_configured_gpu_metrics(running_by_gpu)
    return {
        "ok": not bool(gpu_metrics_error),
        "timestamp": time.time(),
        "gpuDevices": GPU_DEVICE_IDS,
        "workersPerGpu": GPU_WORKERS_PER_DEVICE,
        "slotCapacity": GPU_SLOT_CAPACITY,
        "runningByGpu": running_by_gpu,
        "gpus": gpus,
        "runningJobs": _running_jobs_snapshot(),
        **({"gpuMetricsError": gpu_metrics_error} if gpu_metrics_error else {}),
    }


def _acquire_gpu_slot(job_id: str) -> str:
    gpu_device = gpu_slots.get()
    _write_status(job_id, assigned_gpu=gpu_device)
    return gpu_device


def _release_gpu_slot(gpu_device: str | None) -> None:
    if gpu_device:
        gpu_slots.put(gpu_device)


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


def _input_url_path(job_id: str) -> Path:
    return _job_dir(job_id) / "input-url.json"


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
    connect_timeout = int(os.environ.get("MODEL_PLAZA_GPU_RESULT_UPLOAD_CONNECT_TIMEOUT", "20"))
    read_timeout = int(os.environ.get("MODEL_PLAZA_GPU_RESULT_UPLOAD_READ_TIMEOUT", "180"))
    with output_path.open("rb") as file:
        response = requests.put(
            upload_url,
            data=file,
            headers=headers,
            timeout=(connect_timeout, read_timeout),
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
    if _result_upload_config(job_id):
        try:
            result_updates = _upload_result_to_presigned_url(job_id, output_path)
            if result_updates:
                return result_updates
        except Exception as exc:
            upload_errors.append(f"presigned upload failed: {exc}")

    try:
        result_updates = _upload_result_to_tos(job_id, output_path)
        if result_updates:
            return result_updates
    except Exception as exc:
        upload_errors.append(f"tos upload failed: {exc}")

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
    assigned_gpu: str | None = None

    try:
        if not input_path.exists():
            input_url_path = _input_url_path(job_id)
            if not input_url_path.exists():
                raise RuntimeError("job has no input.mp4 or input_url")
            input_payload = json.loads(input_url_path.read_text(encoding="utf-8"))
            input_url = str(input_payload.get("input_url") or "")
            _write_status(job_id, progress_percent=3, progress_stage="远端正在下载输入视频")
            _download_input_url(input_url, input_path)
            _write_status(job_id, progress_percent=5, progress_stage="远端输入视频下载完成")

        _require_gpu_preflight()
        assigned_gpu = _acquire_gpu_slot(job_id)
        if _read_status(job_id).get("status") == "cancelled":
            return

        _write_status(
            job_id,
            status="processing",
            started_at=time.time(),
            log_path=str(log_path),
            assigned_gpu=assigned_gpu,
            progress_percent=8,
            progress_stage=f"远端 GPU {assigned_gpu} 已领取任务",
        )
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
        with log_path.open("w", encoding="utf-8") as log_file:
            env = {
                **os.environ,
                "CUDA_VISIBLE_DEVICES": assigned_gpu,
                "MODEL_PLAZA_ASSIGNED_GPU": assigned_gpu,
                "MODEL_PLAZA_PROGRESS_FILE": str(_progress_path(job_id)),
            }
            process = subprocess.Popen(command, stdout=log_file, stderr=subprocess.STDOUT, text=True, env=env, start_new_session=True)
            with running_processes_lock:
                running_processes[job_id] = process
                running_gpu_devices[job_id] = assigned_gpu
                status = _read_status(job_id)
                running_progress_snapshots[job_id] = (_progress_signature_from_status(status), time.time(), _job_activity_heartbeat(job_id, status))
            return_code = process.wait()
            with running_processes_lock:
                running_processes.pop(job_id, None)
                running_gpu_devices.pop(job_id, None)
                running_progress_snapshots.pop(job_id, None)
            if _read_status(job_id).get("status") in TERMINAL_STATUSES:
                return
            if return_code != 0:
                raise subprocess.CalledProcessError(return_code, command)
        if not output_path.exists():
            raise RuntimeError("runner completed but output.mp4 was not created")
        if not UPLOAD_RESULTS:
            _write_status(
                job_id,
                status="succeeded",
                completed_at=time.time(),
                result_path=str(output_path),
                progress_percent=100,
                progress_stage="远端处理完成，等待平台拉取结果",
            )
            return
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
            running_gpu_devices.pop(job_id, None)
            running_progress_snapshots.pop(job_id, None)
        if _read_status(job_id).get("status") in TERMINAL_STATUSES:
            return
        _write_status(job_id, status="failed", completed_at=time.time(), error=str(exc), log_path=str(log_path))
    finally:
        _release_gpu_slot(assigned_gpu)


def _job_age_seconds(status: dict) -> float:
    timestamp = float(status.get("updated_at") or status.get("created_at") or 0)
    if timestamp <= 0:
        return 0
    return max(0, time.time() - timestamp)


def _job_completed_age_seconds(status: dict) -> float:
    timestamp = float(status.get("completed_at") or status.get("updated_at") or status.get("created_at") or 0)
    if timestamp <= 0:
        return 0
    return max(0, time.time() - timestamp)


def _terminal_ttl_seconds(status: str) -> int:
    if status == "succeeded":
        return CLEANUP_SUCCESS_TTL_SECONDS
    return CLEANUP_FAILED_TTL_SECONDS


def _job_directory_size(path: Path) -> int:
    total = 0
    for item in path.rglob("*"):
        try:
            if item.is_file() or item.is_symlink():
                total += item.stat().st_size
        except FileNotFoundError:
            continue
    return total


def _terminal_jobs() -> list[tuple[float, str, Path, dict]]:
    jobs: list[tuple[float, str, Path, dict]] = []
    if not JOBS_ROOT.exists():
        return jobs
    for status_path in sorted(JOBS_ROOT.glob("*/status.json")):
        job_dir = status_path.parent
        try:
            status = json.loads(status_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        state = str(status.get("status") or "")
        if state not in TERMINAL_STATUSES:
            continue
        jobs.append((_job_completed_age_seconds(status), state, job_dir, status))
    return jobs


def _cleanup_runner_work(job_dir: Path, age_seconds: float) -> tuple[int, int]:
    if CLEANUP_RUNNER_WORK_TTL_SECONDS <= 0 or age_seconds < CLEANUP_RUNNER_WORK_TTL_SECONDS:
        return 0, 0
    runner_work = job_dir / "runner-work"
    if not runner_work.exists():
        return 0, 0
    bytes_removed = _job_directory_size(runner_work)
    shutil.rmtree(runner_work, ignore_errors=True)
    return 1, bytes_removed


def _cleanup_expired_terminal_jobs(now: float) -> tuple[int, int, int, int]:
    jobs_removed = 0
    runner_work_removed = 0
    bytes_removed = 0
    runner_work_bytes_removed = 0
    for age_seconds, state, job_dir, _status in _terminal_jobs():
        ttl_seconds = _terminal_ttl_seconds(state)
        if ttl_seconds > 0 and age_seconds >= ttl_seconds:
            bytes_removed += _job_directory_size(job_dir)
            shutil.rmtree(job_dir, ignore_errors=True)
            jobs_removed += 1
            continue
        removed_count, removed_bytes = _cleanup_runner_work(job_dir, age_seconds)
        runner_work_removed += removed_count
        runner_work_bytes_removed += removed_bytes
    return jobs_removed, bytes_removed, runner_work_removed, runner_work_bytes_removed


def _disk_usage_percent() -> float:
    usage = shutil.disk_usage(JOBS_ROOT if JOBS_ROOT.exists() else ROOT)
    if usage.total <= 0:
        return 0
    return (usage.used / usage.total) * 100


def _cleanup_for_disk_pressure() -> tuple[int, int]:
    if _disk_usage_percent() < CLEANUP_DISK_HIGH_WATERMARK_PERCENT:
        return 0, 0
    jobs_removed = 0
    bytes_removed = 0
    for age_seconds, _state, job_dir, _status in sorted(_terminal_jobs(), key=lambda item: item[0], reverse=True):
        if age_seconds < CLEANUP_DISK_MIN_AGE_SECONDS:
            continue
        bytes_removed += _job_directory_size(job_dir)
        shutil.rmtree(job_dir, ignore_errors=True)
        jobs_removed += 1
        if _disk_usage_percent() <= CLEANUP_DISK_LOW_WATERMARK_PERCENT:
            break
    return jobs_removed, bytes_removed


def _cleanup_once() -> dict:
    if not CLEANUP_ENABLED or not JOBS_ROOT.exists():
        return {"enabled": CLEANUP_ENABLED, "skipped": True}
    with cleanup_lock:
        started_at = time.time()
        expired_jobs, expired_bytes, runner_work_dirs, runner_work_bytes = _cleanup_expired_terminal_jobs(started_at)
        pressure_jobs, pressure_bytes = _cleanup_for_disk_pressure()
        return {
            "enabled": True,
            "expired_jobs_removed": expired_jobs,
            "expired_bytes_removed": expired_bytes,
            "runner_work_dirs_removed": runner_work_dirs,
            "runner_work_bytes_removed": runner_work_bytes,
            "pressure_jobs_removed": pressure_jobs,
            "pressure_bytes_removed": pressure_bytes,
            "disk_used_percent": round(_disk_usage_percent(), 2),
            "duration_seconds": round(time.time() - started_at, 3),
        }


def _cleanup_loop() -> None:
    while True:
        try:
            result = _cleanup_once()
            if not result.get("skipped"):
                print(f"GPU cleanup: {json.dumps(result, ensure_ascii=False)}", flush=True)
        except Exception as exc:
            print(f"GPU cleanup failed: {exc}", flush=True)
        time.sleep(CLEANUP_INTERVAL_SECONDS)


def _watchdog_once() -> dict:
    if GPU_STALL_TIMEOUT_SECONDS <= 0:
        return {"enabled": False}
    now = time.time()
    cancelled: list[str] = []
    with running_processes_lock:
        running_items = list(running_processes.items())

    for job_id, process in running_items:
        if process.poll() is not None:
            with running_processes_lock:
                running_progress_snapshots.pop(job_id, None)
            continue
        try:
            status = _read_status(job_id)
        except Exception:
            continue
        if status.get("status") not in {"processing", "uploading"}:
            with running_processes_lock:
                running_progress_snapshots.pop(job_id, None)
            continue

        signature = _progress_signature_from_status(status)
        heartbeat = _job_activity_heartbeat(job_id, status)
        with running_processes_lock:
            previous = running_progress_snapshots.get(job_id)
            if previous is None or previous[0] != signature or heartbeat > previous[2]:
                running_progress_snapshots[job_id] = (signature, now, heartbeat)
                continue
            last_changed_at = previous[1]

        if now - last_changed_at < GPU_STALL_TIMEOUT_SECONDS:
            continue

        _write_status(
            job_id,
            status="failed",
            completed_at=now,
            error=f"STALLED_PROGRESS: no progress for {GPU_STALL_TIMEOUT_SECONDS}s",
            progress_stage=f"远端任务超过 {GPU_STALL_TIMEOUT_SECONDS // 60} 分钟无进度，已自动熔断",
        )
        _terminate_job_processes(job_id, process, reason=f"stalled progress for {GPU_STALL_TIMEOUT_SECONDS}s")
        cancelled.append(job_id)

    return {"enabled": True, "stalled_jobs_cancelled": cancelled}


def _watchdog_loop() -> None:
    while True:
        try:
            result = _watchdog_once()
            if result.get("stalled_jobs_cancelled"):
                print(f"GPU watchdog: {json.dumps(result, ensure_ascii=False)}", flush=True)
        except Exception as exc:
            print(f"GPU watchdog failed: {exc}", flush=True)
        time.sleep(GPU_WATCHDOG_INTERVAL_SECONDS)


def _recover_finished_job(job_id: str, output_path: Path) -> None:
    try:
        if not UPLOAD_RESULTS:
            _write_status(
                job_id,
                status="succeeded",
                completed_at=time.time(),
                result_path=str(output_path),
                error="",
                progress_percent=100,
                progress_stage="远端处理完成，等待平台拉取结果",
            )
            return
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
            if (
                status.get("status") == "processing"
                and GPU_RECOVER_STALE_PROCESSING_TIMEOUT_SECONDS > 0
                and _status_running_age_seconds(status) >= GPU_RECOVER_STALE_PROCESSING_TIMEOUT_SECONDS
            ):
                _write_status(
                    job_id,
                    status="failed",
                    completed_at=time.time(),
                    error=f"stale processing job exceeded recovery timeout {GPU_RECOVER_STALE_PROCESSING_TIMEOUT_SECONDS}s",
                    progress_stage="远端服务重启后发现任务已超时，已标记失败可重试",
                )
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
    threading.Thread(target=_cleanup_loop, name="cleanup-gpu-jobs", daemon=True).start()
    threading.Thread(target=_watchdog_loop, name="watchdog-gpu-jobs", daemon=True).start()


@app.get("/health")
def health() -> dict:
    running_by_gpu = {gpu_device: 0 for gpu_device in GPU_DEVICE_IDS}
    with running_processes_lock:
        for gpu_device in running_gpu_devices.values():
            running_by_gpu[gpu_device] = running_by_gpu.get(gpu_device, 0) + 1
    gpu_preflight_error = _gpu_preflight_error()
    return {
        "ok": not bool(gpu_preflight_error),
        "gpu_preflight_error": gpu_preflight_error,
        "max_workers": MAX_WORKERS,
        "gpu_devices": GPU_DEVICE_IDS,
        "workers_per_gpu": GPU_WORKERS_PER_DEVICE,
        "slot_capacity": GPU_SLOT_CAPACITY,
        "upload_results": UPLOAD_RESULTS,
        "cleanup_enabled": CLEANUP_ENABLED,
        "cleanup_success_ttl_seconds": CLEANUP_SUCCESS_TTL_SECONDS,
        "cleanup_failed_ttl_seconds": CLEANUP_FAILED_TTL_SECONDS,
        "cleanup_runner_work_ttl_seconds": CLEANUP_RUNNER_WORK_TTL_SECONDS,
        "cleanup_disk_high_watermark_percent": CLEANUP_DISK_HIGH_WATERMARK_PERCENT,
        "cleanup_disk_low_watermark_percent": CLEANUP_DISK_LOW_WATERMARK_PERCENT,
        "gpu_stall_timeout_seconds": GPU_STALL_TIMEOUT_SECONDS,
        "gpu_watchdog_interval_seconds": GPU_WATCHDOG_INTERVAL_SECONDS,
        "gpu_recover_stale_processing_timeout_seconds": GPU_RECOVER_STALE_PROCESSING_TIMEOUT_SECONDS,
        "running_by_gpu": running_by_gpu,
    }


@app.get("/metrics")
def metrics(x_api_key: Annotated[str | None, Header(alias="X-API-Key")] = None) -> dict:
    _check_auth(x_api_key)
    return _gpu_metrics()


@app.post("/maintenance/cleanup")
def run_cleanup(x_api_key: Annotated[str | None, Header(alias="X-API-Key")] = None) -> dict:
    _check_auth(x_api_key)
    return _cleanup_once()


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
    gpu_preflight_error = _gpu_preflight_error()
    if gpu_preflight_error:
        raise HTTPException(status_code=503, detail=f"GPU preflight failed: {gpu_preflight_error}")
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
        _input_url_path(job_id).write_text(json.dumps({"input_url": input_url}, ensure_ascii=False), encoding="utf-8")
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
        running_progress_snapshots.pop(job_id, None)
    _terminate_job_processes(job_id, process, reason="job cancellation")
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
