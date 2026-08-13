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
import zipfile
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
RESULTS_ROOT = Path(os.environ.get("MODEL_PLAZA_GPU_RESULTS_ROOT", "/data1/model-plaza-results")).resolve()
ZIP_RESULTS_ROOT = RESULTS_ROOT / "internal-batch-zips"
PROPAINTER_RUNNER_PATH = Path(os.environ.get("MODEL_PLAZA_PROPAINTER_RUNNER", str(ROOT / "scripts" / "propainter_runner.py"))).resolve()
ENHANCE_RUNNER_PATH = Path(os.environ.get("MODEL_PLAZA_ENHANCE_RUNNER", str(ROOT / "scripts" / "video_enhance_runner.py"))).resolve()
TRANSLATE_RUNNER_PATH = Path(os.environ.get("MODEL_PLAZA_TRANSLATE_RUNNER", str(ROOT / "scripts" / "video_translate_runner.py"))).resolve()
PYTHON_PATH = os.environ.get("PROPAINTER_PYTHON", "/data1/conda/miniconda3/envs/video-inpaint/bin/python")
API_KEY = os.environ.get("MODEL_PLAZA_GPU_API_KEY", "")
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
GPU_DISK_PREFLIGHT_ENABLED = os.environ.get("MODEL_PLAZA_GPU_DISK_PREFLIGHT_ENABLED", "1").lower() not in {"0", "false", "no"}
GPU_MIN_FREE_BYTES = max(0, _env_int("MODEL_PLAZA_GPU_MIN_FREE_BYTES", 500 * 1024 * 1024 * 1024))
GPU_MIN_FREE_PERCENT = max(0, min(100, _env_int("MODEL_PLAZA_GPU_MIN_FREE_PERCENT", 8)))
RESULT_CACHE_HIGH_WATERMARK_BYTES = max(0, _env_int("MODEL_PLAZA_GPU_RESULT_CACHE_HIGH_WATERMARK_BYTES", 1_000_000_000_000))
RESULT_CACHE_LOW_WATERMARK_BYTES = max(0, min(RESULT_CACHE_HIGH_WATERMARK_BYTES, _env_int("MODEL_PLAZA_GPU_RESULT_CACHE_LOW_WATERMARK_BYTES", 900_000_000_000)))
RESULT_CACHE_MIN_AGE_SECONDS = max(0, _env_int("MODEL_PLAZA_GPU_RESULT_CACHE_MIN_AGE_SECONDS", 60 * 60))
GPU_PREFLIGHT_ENABLED = os.environ.get("MODEL_PLAZA_GPU_PREFLIGHT_ENABLED", "1").lower() not in {"0", "false", "no"}
GPU_STALL_TIMEOUT_SECONDS = max(0, _env_int("MODEL_PLAZA_GPU_STALL_TIMEOUT_SECONDS", 30 * 60))
GPU_WATCHDOG_INTERVAL_SECONDS = max(5, _env_int("MODEL_PLAZA_GPU_WATCHDOG_INTERVAL_SECONDS", 30))
GPU_CANCEL_GRACE_SECONDS = max(1, _env_int("MODEL_PLAZA_GPU_CANCEL_GRACE_SECONDS", 8))
GPU_RECOVER_STALE_PROCESSING_TIMEOUT_SECONDS = max(0, _env_int("MODEL_PLAZA_GPU_RECOVER_STALE_PROCESSING_TIMEOUT_SECONDS", GPU_STALL_TIMEOUT_SECONDS))
GPU_AUTO_EXCLUSIVE_ENABLED = os.environ.get("MODEL_PLAZA_GPU_AUTO_EXCLUSIVE_ENABLED", "1").lower() not in {"0", "false", "no"}
GPU_AUTO_EXCLUSIVE_MIN_DURATION_SECONDS = max(0, _env_int("MODEL_PLAZA_GPU_AUTO_EXCLUSIVE_MIN_DURATION_SECONDS", 80))
GPU_AUTO_EXCLUSIVE_MIN_FRAMES = max(0, _env_int("MODEL_PLAZA_GPU_AUTO_EXCLUSIVE_MIN_FRAMES", 1800))
GPU_AUTO_EXCLUSIVE_MIN_PIXELS = max(0, _env_int("MODEL_PLAZA_GPU_AUTO_EXCLUSIVE_MIN_PIXELS", 1280 * 720))
GPU_AUTO_EXCLUSIVE_MIN_FREE_MEMORY_MIB = max(0, _env_int("MODEL_PLAZA_GPU_AUTO_EXCLUSIVE_MIN_FREE_MEMORY_MIB", 24_000))


def _csv_values(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


GPU_DEVICE_IDS = _csv_values(
    os.environ.get("MODEL_PLAZA_GPU_DEVICE_IDS")
    or os.environ.get("CUDA_VISIBLE_DEVICES")
    or "0"
)
GPU_MONITOR_DEVICE_IDS = _csv_values(os.environ.get("MODEL_PLAZA_GPU_MONITOR_DEVICE_IDS") or ",".join(GPU_DEVICE_IDS))
GPU_WORKERS_PER_DEVICE = max(1, int(os.environ.get("MODEL_PLAZA_GPU_WORKERS_PER_DEVICE", "1")))
def _gpu_slot_capacity_by_device() -> dict[str, int]:
    capacities = {gpu_device: GPU_WORKERS_PER_DEVICE for gpu_device in GPU_DEVICE_IDS}
    for item in _csv_values(os.environ.get("MODEL_PLAZA_GPU_DEVICE_SLOT_CAPACITY", "")):
        gpu_device, separator, capacity = item.partition(":")
        if not separator or gpu_device not in capacities:
            continue
        try:
            capacities[gpu_device] = max(0, int(capacity))
        except ValueError:
            continue
    return capacities


GPU_SLOT_CAPACITY_BY_DEVICE = _gpu_slot_capacity_by_device()
GPU_SLOT_CAPACITY = max(1, sum(GPU_SLOT_CAPACITY_BY_DEVICE.values()))
MAX_WORKERS = max(1, int(os.environ.get("MODEL_PLAZA_GPU_MAX_WORKERS", str(GPU_SLOT_CAPACITY))))

app = FastAPI(title="片刻修AI GPU Worker")
executor = ThreadPoolExecutor(max_workers=MAX_WORKERS)
gpu_slot_condition = threading.Condition()
gpu_slot_usage: dict[str, int] = {gpu_device: 0 for gpu_device in GPU_DEVICE_IDS}
exclusive_gpu_jobs: dict[str, str] = {}
running_processes: dict[str, subprocess.Popen] = {}
running_gpu_devices: dict[str, str] = {}
running_progress_snapshots: dict[str, tuple[tuple[str, int, str], float, float]] = {}
running_processes_lock = threading.Lock()
job_admission_lock = threading.Lock()
recover_lock = threading.Lock()
cleanup_lock = threading.Lock()
zip_locks: dict[str, threading.Lock] = {}
zip_locks_guard = threading.Lock()


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
        "workerSlotsTotal": GPU_SLOT_CAPACITY_BY_DEVICE.get(gpu_device, 0),
    }


def _attach_worker_slots(gpus: list[dict], running_by_gpu: dict[str, int]) -> list[dict]:
    for gpu in gpus:
        gpu_device = str(gpu["index"])
        gpu["workerSlotsUsed"] = running_by_gpu.get(gpu_device, 0)
        gpu["workerSlotsTotal"] = GPU_SLOT_CAPACITY_BY_DEVICE.get(gpu_device, 0)
    return gpus


def _query_configured_gpu_metrics(running_by_gpu: dict[str, int], gpu_device_ids: list[str] | None = None) -> tuple[list[dict], str]:
    gpu_device_ids = gpu_device_ids or GPU_DEVICE_IDS
    query_args = [
        "--query-gpu=index,name,utilization.gpu,utilization.memory,memory.used,memory.total,temperature.gpu,power.draw",
        "--format=csv",
    ]
    try:
        query_output = _run_command(["nvidia-smi", f"--id={','.join(gpu_device_ids)}", *query_args], timeout=5)
        return _attach_worker_slots(_parse_gpu_csv(query_output), running_by_gpu), ""
    except Exception as exc:
        configured_error = str(exc)

    try:
        query_output = _run_command(["nvidia-smi", *query_args], timeout=5)
        visible_gpus = _parse_gpu_csv(query_output)
    except Exception as exc:
        error = f"{configured_error}; fallback without --id failed: {exc}"
        return [_empty_gpu_metric(gpu_device, running_by_gpu) for gpu_device in gpu_device_ids], error

    if len(visible_gpus) == len(gpu_device_ids):
        remapped = []
        for gpu_device, metric in zip(gpu_device_ids, visible_gpus):
            remapped.append({**metric, "index": gpu_device})
        return _attach_worker_slots(remapped, running_by_gpu), configured_error

    visible_by_index = {str(metric.get("index", "")): metric for metric in visible_gpus}
    metrics = []
    for gpu_device in gpu_device_ids:
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


def _active_job_count() -> int:
    return len(_running_jobs_snapshot())


def _slot_usage_snapshot(gpu_device_ids: list[str]) -> dict[str, int]:
    with gpu_slot_condition:
        return {gpu_device: int(gpu_slot_usage.get(gpu_device, 0)) for gpu_device in gpu_device_ids}


def _active_runner_snapshot(gpu_device_ids: list[str]) -> dict[str, int]:
    running_by_gpu = {gpu_device: 0 for gpu_device in gpu_device_ids}
    with running_processes_lock:
        for gpu_device in running_gpu_devices.values():
            running_by_gpu[gpu_device] = running_by_gpu.get(gpu_device, 0) + 1
    return running_by_gpu


def _gpu_metrics() -> dict:
    running_by_gpu = _slot_usage_snapshot(GPU_MONITOR_DEVICE_IDS)
    active_runner_by_gpu = _active_runner_snapshot(GPU_MONITOR_DEVICE_IDS)
    gpus, gpu_metrics_error = _query_configured_gpu_metrics(running_by_gpu, GPU_MONITOR_DEVICE_IDS)
    return {
        "ok": not bool(gpu_metrics_error),
        "timestamp": time.time(),
        "gpuDevices": GPU_DEVICE_IDS,
        "monitorGpuDevices": GPU_MONITOR_DEVICE_IDS,
        "workersPerGpu": GPU_WORKERS_PER_DEVICE,
        "workerSlotCapacityByGpu": GPU_SLOT_CAPACITY_BY_DEVICE,
        "slotCapacity": GPU_SLOT_CAPACITY,
        "runningByGpu": running_by_gpu,
        "activeRunnerByGpu": active_runner_by_gpu,
        "gpus": gpus,
        "runningJobs": _running_jobs_snapshot(),
        **({"gpuMetricsError": gpu_metrics_error} if gpu_metrics_error else {}),
    }


def _truthy(value: object) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _gpu_free_memory_by_device() -> dict[str, float]:
    running_by_gpu = {gpu_device: 0 for gpu_device in GPU_DEVICE_IDS}
    gpus, _ = _query_configured_gpu_metrics(running_by_gpu, GPU_DEVICE_IDS)
    free_by_device: dict[str, float] = {}
    for gpu in gpus:
        gpu_device = str(gpu.get("index") or "")
        total = float(gpu.get("memoryTotalMiB") or 0)
        used = float(gpu.get("memoryUsedMiB") or 0)
        if gpu_device:
            free_by_device[gpu_device] = max(0.0, total - used)
    return free_by_device


def _candidate_gpu_devices(preferred_gpu: str = "", min_free_memory_mib: int = 0) -> list[str]:
    if preferred_gpu and preferred_gpu in GPU_DEVICE_IDS:
        return [preferred_gpu]
    candidates = list(GPU_DEVICE_IDS)
    if min_free_memory_mib <= 0:
        return candidates
    free_by_device = _gpu_free_memory_by_device()
    ranked = sorted(candidates, key=lambda gpu_device: free_by_device.get(gpu_device, 0), reverse=True)
    eligible = [gpu_device for gpu_device in ranked if free_by_device.get(gpu_device, 0) >= min_free_memory_mib]
    return eligible or ranked


def _acquire_gpu_slot(job_id: str, preferred_gpu: str = "", exclusive: bool = False, min_free_memory_mib: int = 0) -> str:
    with gpu_slot_condition:
        while True:
            candidates = _candidate_gpu_devices(preferred_gpu, min_free_memory_mib)
            for gpu_device in candidates:
                slot_capacity = GPU_SLOT_CAPACITY_BY_DEVICE.get(gpu_device, 0)
                if slot_capacity <= 0:
                    continue
                if exclusive:
                    if gpu_slot_usage.get(gpu_device, 0) == 0 and gpu_device not in exclusive_gpu_jobs:
                        gpu_slot_usage[gpu_device] = slot_capacity
                        exclusive_gpu_jobs[gpu_device] = job_id
                        _write_status(job_id, assigned_gpu=gpu_device, exclusive_gpu=True)
                        return gpu_device
                    continue
                if gpu_device in exclusive_gpu_jobs:
                    continue
                if gpu_slot_usage.get(gpu_device, 0) < slot_capacity:
                    gpu_slot_usage[gpu_device] = gpu_slot_usage.get(gpu_device, 0) + 1
                    _write_status(job_id, assigned_gpu=gpu_device, exclusive_gpu=False)
                    return gpu_device
            gpu_slot_condition.wait(timeout=5)


def _probe_video_metadata(path: Path) -> dict[str, float]:
    command = [
        "ffprobe",
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream=width,height,r_frame_rate,nb_frames:format=duration",
        "-of",
        "json",
        str(path),
    ]
    try:
        payload = json.loads(_run_command(command, timeout=15))
    except Exception:
        return {}
    stream = (payload.get("streams") or [{}])[0] or {}
    duration = float((payload.get("format") or {}).get("duration") or 0)
    width = float(stream.get("width") or 0)
    height = float(stream.get("height") or 0)
    frame_count = float(stream.get("nb_frames") or 0)
    if frame_count <= 0 and duration > 0:
        numerator, separator, denominator = str(stream.get("r_frame_rate") or "").partition("/")
        try:
            fps = float(numerator) / float(denominator) if separator and float(denominator) else float(numerator or 0)
        except ValueError:
            fps = 0
        frame_count = duration * fps if fps > 0 else 0
    return {"duration": duration, "width": width, "height": height, "frames": frame_count, "pixels": width * height}


def _auto_exclusive_reason(job_type: str, params: dict, input_path: Path) -> str:
    if not GPU_AUTO_EXCLUSIVE_ENABLED or _truthy(params.get("disableAutoExclusiveGpu")):
        return ""
    if job_type not in {"propainter", "subtitle_translate"}:
        return ""
    metadata = _probe_video_metadata(input_path)
    duration = float(params.get("durationSeconds") or params.get("duration") or metadata.get("duration") or 0)
    frames = float(params.get("frameCount") or metadata.get("frames") or 0)
    pixels = float(metadata.get("pixels") or 0)
    reasons: list[str] = []
    if GPU_AUTO_EXCLUSIVE_MIN_DURATION_SECONDS and duration >= GPU_AUTO_EXCLUSIVE_MIN_DURATION_SECONDS:
        reasons.append(f"duration={duration:.1f}s")
    if GPU_AUTO_EXCLUSIVE_MIN_FRAMES and frames >= GPU_AUTO_EXCLUSIVE_MIN_FRAMES:
        reasons.append(f"frames={frames:.0f}")
    if GPU_AUTO_EXCLUSIVE_MIN_PIXELS and pixels >= GPU_AUTO_EXCLUSIVE_MIN_PIXELS:
        reasons.append(f"pixels={pixels:.0f}")
    return ", ".join(reasons)


def _release_gpu_slot(job_id: str, gpu_device: str | None) -> None:
    if not gpu_device:
        return
    with gpu_slot_condition:
        if exclusive_gpu_jobs.get(gpu_device) == job_id:
            exclusive_gpu_jobs.pop(gpu_device, None)
            gpu_slot_usage[gpu_device] = 0
        else:
            gpu_slot_usage[gpu_device] = max(0, gpu_slot_usage.get(gpu_device, 0) - 1)
        gpu_slot_condition.notify_all()


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


def _atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temp_path.write_text(content, encoding="utf-8")
    os.replace(temp_path, path)


def _read_json_file(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _input_url_path(job_id: str) -> Path:
    return _job_dir(job_id) / "input-url.json"


def _write_status(job_id: str, **updates) -> dict:
    status_path = _status_path(job_id)
    current = {}
    if status_path.exists():
        try:
            current = _read_json_file(status_path)
        except json.JSONDecodeError:
            current = {}
    current.update(updates)
    current["updated_at"] = time.time()
    _atomic_write_text(status_path, json.dumps(current, ensure_ascii=False, indent=2))
    return current


def _read_status(job_id: str) -> dict:
    status_path = _status_path(job_id)
    if not status_path.exists():
        raise HTTPException(status_code=404, detail="job not found")
    try:
        status = _read_json_file(status_path)
    except json.JSONDecodeError:
        if _job_process_pids(job_id):
            status = {
                "job_id": job_id,
                "status": "processing",
                "error": "",
                "progress_percent": 0,
                "progress_stage": "远端任务状态文件暂时不可读，继续等待处理",
            }
        else:
            status = {
                "job_id": job_id,
                "status": "failed",
                "error": "GPU_STATUS_CORRUPTED: job status file is unreadable",
                "progress_percent": 0,
                "progress_stage": "远端任务状态文件损坏，请重试",
                "completed_at": time.time(),
            }
    progress_path = _progress_path(job_id)
    if progress_path.exists() and status.get("status") not in {"succeeded", "failed", "cancelled"}:
        try:
            progress = _read_json_file(progress_path)
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


def _safe_relative_path(value: str) -> Path:
    raw = value.strip().lstrip("/")
    if not raw:
        raise ValueError("storage key is required")
    path = Path(raw)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError("storage key contains an unsafe path")
    return path


def _result_cache_path(storage_key: str) -> Path:
    return RESULTS_ROOT / _safe_relative_path(storage_key)


def _upload_file_to_tos(object_key: str, local_path: Path) -> dict | None:
    if not _tos_enabled():
        return None

    import tos

    config = _tos_config()
    local_size = local_path.stat().st_size
    client = tos.TosClientV2(
        config["ak"],
        config["sk"],
        config["endpoint"],
        config["region"],
        max_retry_count=max(1, _env_int("MODEL_PLAZA_TOS_SDK_RETRIES", 3)),
        request_timeout=max(10, _env_int("MODEL_PLAZA_TOS_REQUEST_TIMEOUT", 60)),
        connection_time=max(3, _env_int("MODEL_PLAZA_TOS_CONNECT_TIMEOUT", 10)),
        socket_timeout=max(10, _env_int("MODEL_PLAZA_TOS_SOCKET_TIMEOUT", 60)),
    )
    threshold = max(1, _env_int("MODEL_PLAZA_TOS_MULTIPART_THRESHOLD_BYTES", 128 * 1024 * 1024))
    attempts = max(1, _env_int("MODEL_PLAZA_TOS_UPLOAD_ATTEMPTS", 3))
    part_size = max(5 * 1024 * 1024, _env_int("MODEL_PLAZA_TOS_UPLOAD_PART_SIZE_BYTES", 64 * 1024 * 1024))
    task_num = max(1, _env_int("MODEL_PLAZA_TOS_UPLOAD_TASK_NUM", 4))
    checkpoint_root = RESULTS_ROOT / ".tos-upload-checkpoints"
    checkpoint_root.mkdir(parents=True, exist_ok=True)
    checkpoint_file = checkpoint_root / f"{_safe_zip_id(object_key)}.checkpoint"
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            try:
                head = client.head_object(config["bucket"], object_key)
                remote_size = int(getattr(head, "content_length", 0) or getattr(head, "contentLength", 0) or 0)
                if remote_size == local_size:
                    checkpoint_file.unlink(missing_ok=True)
                    break
            except Exception:
                pass
            if local_size >= threshold:
                client.upload_file(
                    config["bucket"],
                    object_key,
                    str(local_path),
                    part_size=part_size,
                    task_num=task_num,
                    enable_checkpoint=True,
                    checkpoint_file=str(checkpoint_file),
                )
            else:
                client.put_object_from_file(config["bucket"], object_key, str(local_path))
            head = client.head_object(config["bucket"], object_key)
            remote_size = int(getattr(head, "content_length", 0) or getattr(head, "contentLength", 0) or 0)
            if remote_size and remote_size != local_size:
                raise RuntimeError(f"TOS upload size mismatch: local={local_size} remote={remote_size}")
            checkpoint_file.unlink(missing_ok=True)
            break
        except Exception as exc:
            last_error = exc
            if _is_tos_retryable_upload_error(str(exc)):
                checkpoint_file.unlink(missing_ok=True)
            if attempt >= attempts:
                raise
            time.sleep(min(60, max(1, _env_int("MODEL_PLAZA_TOS_UPLOAD_RETRY_BACKOFF_SECONDS", 5)) * attempt))
    if last_error is not None and attempts <= 0:
        raise last_error
    encoded_key = quote(object_key, safe="/")
    return {
        "storage_key": object_key,
        "url": f"{config['public_base_url']}/{encoded_key}",
        "size_bytes": local_size,
    }


def _is_tos_retryable_upload_error(error_text: str) -> bool:
    return any(
        marker in error_text
        for marker in (
            "CompletingStatusNoExpiration",
            "Competing status not expiration",
            "status_code': 409",
            '"status_code": 409',
        )
    )


def _retry_tos_object_key(object_key: str) -> str:
    suffix = f"-retry-{uuid.uuid4().hex[:8]}"
    if object_key.lower().endswith(".zip"):
        return f"{object_key[:-4]}{suffix}.zip"
    return f"{object_key}{suffix}"


def _upload_file_to_tos_worker(object_key: str, local_path: str, queue) -> None:
    try:
        queue.put({"ok": True, "result": _upload_file_to_tos(object_key, Path(local_path))})
    except Exception as exc:
        queue.put({"ok": False, "error": str(exc)})


def _upload_file_to_tos_with_deadline(object_key: str, local_path: Path, timeout_env: str, default_timeout: int = 900) -> dict | None:
    timeout = int(os.environ.get(timeout_env, str(default_timeout)))
    deadline = time.time() + timeout
    attempts = max(1, _env_int("MODEL_PLAZA_TOS_DEADLINE_UPLOAD_ATTEMPTS", 3))
    context = get_context("spawn")
    last_error = ""
    for attempt in range(1, attempts + 1):
        remaining = max(1, int(deadline - time.time()))
        queue = context.Queue()
        process = context.Process(target=_upload_file_to_tos_worker, args=(object_key, str(local_path), queue))
        process.start()
        process.join(remaining)
        if process.is_alive():
            process.terminate()
            process.join(10)
            raise RuntimeError(f"TOS upload exceeded total timeout {timeout}s")
        if queue.empty():
            last_error = "TOS upload worker exited without a result"
        else:
            payload = queue.get()
            if payload.get("ok"):
                result = payload.get("result")
                return dict(result) if result else None
            last_error = str(payload.get("error") or "TOS upload failed")
        if attempt >= attempts or not _is_tos_retryable_upload_error(last_error):
            raise RuntimeError(last_error)
        sleep_seconds = min(
            max(1, int(deadline - time.time())),
            max(5, _env_int("MODEL_PLAZA_TOS_CONFLICT_RETRY_BACKOFF_SECONDS", 30)) * attempt,
        )
        if sleep_seconds <= 0:
            raise RuntimeError(last_error)
        time.sleep(sleep_seconds)
    raise RuntimeError(last_error or "TOS upload failed")


def _upload_result_to_tos(job_id: str, output_path: Path) -> dict | None:
    now = datetime.now(timezone.utc)
    object_key = f"model-plaza/output/videos/{now:%Y/%m/%d}/{job_id}.mp4"
    uploaded = _upload_file_to_tos(object_key, output_path)
    if not uploaded:
        return None
    return {
        "result_storage_key": uploaded["storage_key"],
        "result_url": uploaded["url"],
        "result_mime_type": "video/mp4",
        "result_size_bytes": uploaded["size_bytes"],
    }


def _persist_result_cache(storage_key: str, source_path: Path) -> Path | None:
    if not storage_key or not source_path.exists():
        return None
    try:
        target = _result_cache_path(storage_key)
    except ValueError:
        return None
    target.parent.mkdir(parents=True, exist_ok=True)
    if source_path.resolve() != target.resolve():
        temp_path = target.with_suffix(target.suffix + f".{uuid.uuid4().hex}.tmp")
        shutil.copyfile(source_path, temp_path)
        temp_path.replace(target)
    os.utime(target, None)
    return target


def _result_upload_config(job_id: str) -> dict:
    config_path = _job_dir(job_id) / "result-upload.json"
    if not config_path.exists():
        return {}
    return json.loads(config_path.read_text(encoding="utf-8"))


def _should_upload_result(job_id: str) -> bool:
    return UPLOAD_RESULTS or bool(_result_upload_config(job_id))


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
    try:
        result_updates = _upload_result_to_tos(job_id, output_path)
        if result_updates:
            return result_updates
    except Exception as exc:
        upload_errors.append(f"tos upload failed: {exc}")

    if _result_upload_config(job_id):
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


def _tail_text(path: Path, max_chars: int = 12000) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return text[-max_chars:]


def _summarize_job_error(exc: Exception, log_path: Path) -> str:
    detail = "\n".join(part for part in [str(exc), _tail_text(log_path)] if part)
    lowered = detail.lower()
    if "cuda out of memory" in lowered or "torch.outofmemoryerror" in lowered:
        return "CUDA_OUT_OF_MEMORY: GPU 显存不足，建议使用单卡独占重跑或降低并发后重试"
    if "speech recognition returned no subtitle segments" in lowered or "asr_no_segments" in lowered:
        return "ASR_NO_SEGMENTS: 未识别到可翻译语音/字幕，可保留去字幕结果或检查音轨"
    if "result upload exceeded total timeout" in lowered:
        timeout = os.environ.get("MODEL_PLAZA_GPU_RESULT_UPLOAD_TOTAL_TIMEOUT", "900")
        return f"RESULT_UPLOAD_TIMEOUT: 结果上传超过 {timeout}s，视频已生成但上传对象存储超时"
    if "presigned upload failed" in lowered or "tos upload failed" in lowered:
        return "RESULT_UPLOAD_FAILED: 结果上传对象存储失败，请检查 TOS/预签名上传链路"
    if "stalled_progress" in lowered or "stalled progress" in lowered:
        return "GPU_STALLED: 远端任务长时间无进度，已被自动熔断"
    if "no input.mp4" in lowered or "input_url" in lowered and "failed" in lowered:
        return "GPU_INPUT_FAILED: GPU 端下载或读取输入视频失败"
    if "invalid data found" in lowered or "could not find codec" in lowered or "moov atom not found" in lowered:
        return "VIDEO_DECODE_FAILED: 视频解码失败，可能是文件损坏或编码不兼容"
    return f"GPU_RUNNER_FAILED: {str(exc)[:240]}"


def _runner_for_job_type(job_type: str) -> Path:
    if job_type == "propainter":
        return PROPAINTER_RUNNER_PATH
    if job_type == "enhance":
        return ENHANCE_RUNNER_PATH
    if job_type == "translate":
        return TRANSLATE_RUNNER_PATH
    raise RuntimeError(f"Unsupported job type: {job_type}")


def _runner_command(runner_path: Path, input_path: Path, output_path: Path, params_path: Path, work_dir: Path, regions_path: Path | None = None) -> list[str]:
    command = [
        PYTHON_PATH,
        str(runner_path),
        "--input",
        str(input_path),
        "--output",
        str(output_path),
        "--params",
        str(params_path),
        "--workdir",
        str(work_dir),
    ]
    if regions_path is not None:
        command[6:6] = ["--regions", str(regions_path)]
    return command


def _run_tracked_process(job_id: str, command: list[str], log_file, assigned_gpu: str, use_progress_file: bool = True) -> None:
    env = {
        **os.environ,
        "CUDA_VISIBLE_DEVICES": assigned_gpu,
        "MODEL_PLAZA_ASSIGNED_GPU": assigned_gpu,
    }
    env.setdefault("PYTORCH_CUDA_ALLOC_CONF", "expandable_segments:True")
    if use_progress_file:
        env["MODEL_PLAZA_PROGRESS_FILE"] = str(_progress_path(job_id))
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


def _run_subtitle_translate_job(job_id: str, input_path: Path, output_path: Path, regions_path: Path, params_path: Path, work_dir: Path, log_file, assigned_gpu: str) -> None:
    intermediate_path = work_dir / "subtitle-removed.mp4"
    intermediate_path.parent.mkdir(parents=True, exist_ok=True)

    _write_status(job_id, progress_percent=10, progress_stage="开始去字幕")
    _run_tracked_process(
        job_id,
        _runner_command(PROPAINTER_RUNNER_PATH, input_path, intermediate_path, params_path, work_dir / "propainter", regions_path),
        log_file,
        assigned_gpu,
    )
    if not intermediate_path.exists():
        raise RuntimeError("subtitle removal runner completed but intermediate video was not created")

    _progress_path(job_id).unlink(missing_ok=True)
    _write_status(job_id, progress_percent=55, progress_stage="字幕去除完成，开始翻译并写入字幕")
    _run_tracked_process(
        job_id,
        _runner_command(TRANSLATE_RUNNER_PATH, intermediate_path, output_path, params_path, work_dir / "translate"),
        log_file,
        assigned_gpu,
        use_progress_file=False,
    )


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
        try:
            params_payload = json.loads(params_path.read_text(encoding="utf-8"))
        except Exception:
            params_payload = {}
        preferred_gpu = str(params_payload.get("preferredGpu") or params_payload.get("preferredGPU") or "").strip()
        exclusive_gpu = _truthy(params_payload.get("forceSingleGpu")) or _truthy(params_payload.get("exclusiveGpu"))
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
        auto_exclusive_reason = "" if exclusive_gpu else _auto_exclusive_reason(job_type, params_payload, input_path)
        if auto_exclusive_reason:
            exclusive_gpu = True
            _write_status(
                job_id,
                auto_exclusive_gpu=True,
                auto_exclusive_reason=auto_exclusive_reason,
                progress_stage=f"检测到高风险视频，等待独占 GPU：{auto_exclusive_reason}",
            )
        min_free_memory_mib = GPU_AUTO_EXCLUSIVE_MIN_FREE_MEMORY_MIB if exclusive_gpu and not preferred_gpu else 0
        assigned_gpu = _acquire_gpu_slot(
            job_id,
            preferred_gpu=preferred_gpu,
            exclusive=exclusive_gpu,
            min_free_memory_mib=min_free_memory_mib,
        )
        if _read_status(job_id).get("status") == "cancelled":
            return

        _write_status(
            job_id,
            status="processing",
            started_at=time.time(),
            log_path=str(log_path),
            assigned_gpu=assigned_gpu,
            progress_percent=8,
            progress_stage=f"远端 GPU {assigned_gpu} {'独占' if exclusive_gpu else ''}已领取任务",
            auto_exclusive_gpu=bool(auto_exclusive_reason),
            auto_exclusive_reason=auto_exclusive_reason,
            min_free_memory_mib=min_free_memory_mib,
        )
        LOGS_ROOT.mkdir(parents=True, exist_ok=True)
        with log_path.open("w", encoding="utf-8") as log_file:
            if job_type == "subtitle_translate":
                _run_subtitle_translate_job(job_id, input_path, output_path, regions_path, params_path, work_dir, log_file, assigned_gpu)
            else:
                _run_tracked_process(
                    job_id,
                    _runner_command(
                        _runner_for_job_type(job_type),
                        input_path,
                        output_path,
                        params_path,
                        work_dir,
                        regions_path if job_type in {"propainter", "enhance"} else None,
                    ),
                    log_file,
                    assigned_gpu,
                )
        if not output_path.exists():
            raise RuntimeError("runner completed but output.mp4 was not created")
        _release_gpu_slot(job_id, assigned_gpu)
        assigned_gpu = None
        _write_status(job_id, gpu_slot_released_at=time.time())
        if not _should_upload_result(job_id):
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
        cached_path = _persist_result_cache(str(result_updates.get("result_storage_key") or ""), output_path)
        _write_status(
            job_id,
            status="succeeded",
            completed_at=time.time(),
            result_path=str(cached_path or output_path),
            progress_percent=100,
            progress_stage="远端处理完成",
            **result_updates,
        )
        _cleanup_result_cache_for_watermark()
    except Exception as exc:
        with running_processes_lock:
            running_processes.pop(job_id, None)
            running_gpu_devices.pop(job_id, None)
            running_progress_snapshots.pop(job_id, None)
        if _read_status(job_id).get("status") in TERMINAL_STATUSES:
            return
        _write_status(job_id, status="failed", completed_at=time.time(), error=_summarize_job_error(exc, log_path), log_path=str(log_path))
    finally:
        _release_gpu_slot(job_id, assigned_gpu)


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


def _remove_tree(path: Path) -> bool:
    try:
        shutil.rmtree(path)
        return True
    except OSError as exc:
        print(f"GPU cleanup failed to remove {path}: {exc}", flush=True)
        return False


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
    if not _remove_tree(runner_work):
        return 0, 0
    return 1, bytes_removed


def _cleanup_expired_terminal_jobs(now: float) -> tuple[int, int, int, int]:
    jobs_removed = 0
    runner_work_removed = 0
    bytes_removed = 0
    runner_work_bytes_removed = 0
    for age_seconds, state, job_dir, _status in _terminal_jobs():
        ttl_seconds = _terminal_ttl_seconds(state)
        if ttl_seconds > 0 and age_seconds >= ttl_seconds:
            job_bytes = _job_directory_size(job_dir)
            if _remove_tree(job_dir):
                bytes_removed += job_bytes
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


def _disk_status() -> dict:
    usage = shutil.disk_usage(JOBS_ROOT if JOBS_ROOT.exists() else ROOT)
    used_percent = (usage.used / usage.total) * 100 if usage.total > 0 else 0
    required_free_bytes = max(GPU_MIN_FREE_BYTES, int(usage.total * GPU_MIN_FREE_PERCENT / 100))
    return {
        "total_bytes": usage.total,
        "used_bytes": usage.used,
        "free_bytes": usage.free,
        "used_percent": round(used_percent, 2),
        "min_free_bytes": required_free_bytes,
        "min_free_percent": GPU_MIN_FREE_PERCENT,
        "disk_preflight_enabled": GPU_DISK_PREFLIGHT_ENABLED,
    }


def _format_gib(value: int) -> str:
    return f"{value / 1024 / 1024 / 1024:.1f} GiB"


def _cleanup_for_disk_pressure() -> tuple[int, int]:
    if _disk_usage_percent() < CLEANUP_DISK_HIGH_WATERMARK_PERCENT:
        return 0, 0
    jobs_removed = 0
    bytes_removed = 0
    for _age_seconds, _state, job_dir, _status in sorted(_terminal_jobs(), key=lambda item: item[0], reverse=True):
        runner_work = job_dir / "runner-work"
        if not runner_work.exists():
            continue
        runner_work_bytes = _job_directory_size(runner_work)
        if not _remove_tree(runner_work):
            continue
        bytes_removed += runner_work_bytes
        if _disk_usage_percent() <= CLEANUP_DISK_LOW_WATERMARK_PERCENT:
            return jobs_removed, bytes_removed
    for age_seconds, _state, job_dir, _status in sorted(_terminal_jobs(), key=lambda item: item[0], reverse=True):
        if age_seconds < CLEANUP_DISK_MIN_AGE_SECONDS:
            continue
        job_bytes = _job_directory_size(job_dir)
        if not _remove_tree(job_dir):
            continue
        bytes_removed += job_bytes
        jobs_removed += 1
        if _disk_usage_percent() <= CLEANUP_DISK_LOW_WATERMARK_PERCENT:
            break
    return jobs_removed, bytes_removed


def _result_cache_files() -> list[tuple[float, Path, int]]:
    if not RESULTS_ROOT.exists():
        return []
    files: list[tuple[float, Path, int]] = []
    for path in RESULTS_ROOT.rglob("*"):
        try:
            if not path.is_file() or path.name.endswith((".tmp", ".lock")):
                continue
            stat = path.stat()
        except OSError:
            continue
        files.append((stat.st_mtime, path, stat.st_size))
    return files


def _cleanup_result_cache_for_watermark() -> dict:
    if RESULT_CACHE_HIGH_WATERMARK_BYTES <= 0 or not RESULTS_ROOT.exists():
        return {"enabled": False}
    files = _result_cache_files()
    used_bytes = sum(size for _mtime, _path, size in files)
    if used_bytes <= RESULT_CACHE_HIGH_WATERMARK_BYTES:
        return {"enabled": True, "removed": 0, "bytes_removed": 0, "used_bytes": used_bytes}

    now = time.time()
    removed = 0
    bytes_removed = 0
    for mtime, path, size in sorted(files, key=lambda item: item[0]):
        if RESULT_CACHE_MIN_AGE_SECONDS > 0 and now - mtime < RESULT_CACHE_MIN_AGE_SECONDS:
            continue
        try:
            path.unlink()
        except OSError:
            continue
        removed += 1
        bytes_removed += size
        used_bytes -= size
        if used_bytes <= RESULT_CACHE_LOW_WATERMARK_BYTES:
            break
    for directory in sorted((path for path in RESULTS_ROOT.rglob("*") if path.is_dir()), key=lambda item: len(item.parts), reverse=True):
        try:
            directory.rmdir()
        except OSError:
            pass
    return {"enabled": True, "removed": removed, "bytes_removed": bytes_removed, "used_bytes": used_bytes}


def _cleanup_once() -> dict:
    if not CLEANUP_ENABLED or not JOBS_ROOT.exists():
        return {"enabled": CLEANUP_ENABLED, "skipped": True}
    with cleanup_lock:
        started_at = time.time()
        expired_jobs, expired_bytes, runner_work_dirs, runner_work_bytes = _cleanup_expired_terminal_jobs(started_at)
        pressure_jobs, pressure_bytes = _cleanup_for_disk_pressure()
        result_cache = _cleanup_result_cache_for_watermark()
        return {
            "enabled": True,
            "expired_jobs_removed": expired_jobs,
            "expired_bytes_removed": expired_bytes,
            "runner_work_dirs_removed": runner_work_dirs,
            "runner_work_bytes_removed": runner_work_bytes,
            "pressure_jobs_removed": pressure_jobs,
            "pressure_bytes_removed": pressure_bytes,
            "result_cache": result_cache,
            "disk_used_percent": round(_disk_usage_percent(), 2),
            "duration_seconds": round(time.time() - started_at, 3),
        }


def _disk_preflight_error(run_cleanup: bool = False) -> str:
    if not GPU_DISK_PREFLIGHT_ENABLED:
        return ""
    if run_cleanup and CLEANUP_ENABLED and _disk_usage_percent() >= CLEANUP_DISK_HIGH_WATERMARK_PERCENT:
        _cleanup_once()
    status = _disk_status()
    if status["free_bytes"] >= status["min_free_bytes"]:
        return ""
    return (
        "GPU disk pressure: "
        f"{_format_gib(status['free_bytes'])} free, "
        f"requires at least {_format_gib(status['min_free_bytes'])}; "
        f"used {status['used_percent']}%"
    )


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
        if not _should_upload_result(job_id):
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
        cached_path = _persist_result_cache(str(result_updates.get("result_storage_key") or ""), output_path)
        _write_status(
            job_id,
            status="succeeded",
            completed_at=time.time(),
            result_path=str(cached_path or output_path),
            **result_updates,
        )
        _cleanup_result_cache_for_watermark()
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


def _zip_lock(zip_id: str) -> threading.Lock:
    with zip_locks_guard:
        lock = zip_locks.get(zip_id)
        if lock is None:
            lock = threading.Lock()
            zip_locks[zip_id] = lock
        return lock


def _download_url_to_cache(download_url: str, target_path: Path) -> None:
    parsed = urllib.parse.urlparse(download_url)
    if parsed.scheme not in {"http", "https"}:
        raise HTTPException(status_code=400, detail="download_url must be http or https")
    target_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = target_path.with_suffix(target_path.suffix + f".{uuid.uuid4().hex}.tmp")
    request = urllib.request.Request(download_url, headers={"User-Agent": "model-plaza-gpu-worker/1.0"})
    try:
        with urllib.request.urlopen(request, timeout=int(os.environ.get("MODEL_PLAZA_GPU_ZIP_SOURCE_DOWNLOAD_TIMEOUT", "900"))) as response:
            with temp_path.open("wb") as output_file:
                shutil.copyfileobj(response, output_file)
        temp_path.replace(target_path)
    finally:
        temp_path.unlink(missing_ok=True)


def _source_path_for_zip_entry(entry: dict) -> Path:
    storage_key = str(entry.get("storage_key") or "")
    download_url = str(entry.get("download_url") or "")
    try:
        source_path = _result_cache_path(storage_key)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if source_path.exists() and source_path.is_file():
        os.utime(source_path, None)
        return source_path
    if not download_url:
        raise HTTPException(status_code=404, detail=f"missing cached result and download_url: {storage_key}")
    _download_url_to_cache(download_url, source_path)
    return source_path


def _safe_zip_id(value: str) -> str:
    safe = Path(value or uuid.uuid4().hex).name
    return "".join(char if char.isalnum() or char in {"-", "_", "."} else "_" for char in safe)[:180] or uuid.uuid4().hex


def _create_internal_batch_zip_on_gpu(payload: dict) -> dict:
    zip_id = _safe_zip_id(str(payload.get("zip_id") or ""))
    filename = Path(str(payload.get("filename") or f"{zip_id}.zip")).name
    local_filename = f"{zip_id}.zip"
    zip_storage_key = str(payload.get("zip_storage_key") or f"model-plaza/output/zips/{zip_id}.zip").strip("/")
    entries = payload.get("entries") or []
    summary = payload.get("summary") or {}
    summary_name = Path(str(payload.get("summary_name") or "_batch-summary.json")).name
    if not isinstance(entries, list) or not entries:
        raise HTTPException(status_code=400, detail="entries must be a non-empty list")
    if not _tos_enabled():
        raise HTTPException(status_code=503, detail="TOS is not configured on GPU worker")

    zip_dir = ZIP_RESULTS_ROOT
    try:
        zip_dir.mkdir(parents=True, exist_ok=True)
    except PermissionError as exc:
        raise HTTPException(status_code=503, detail=f"ZIP result directory is not writable: {zip_dir}") from exc
    zip_path = zip_dir / local_filename
    with _zip_lock(zip_id):
        if not zip_path.exists() or zip_path.stat().st_size <= 0:
            temp_zip_path = zip_path.with_suffix(zip_path.suffix + f".{uuid.uuid4().hex}.tmp")
            try:
                with zipfile.ZipFile(temp_zip_path, "w", compression=zipfile.ZIP_STORED) as archive:
                    archive.writestr(summary_name, json.dumps(summary, ensure_ascii=False, indent=2))
                    for entry in entries:
                        zip_name = Path(str(entry.get("zip_name") or "")).name
                        if not zip_name:
                            raise HTTPException(status_code=400, detail="zip_name is required")
                        archive.write(_source_path_for_zip_entry(entry), zip_name)
                temp_zip_path.replace(zip_path)
            except PermissionError as exc:
                raise HTTPException(status_code=503, detail=f"ZIP result path is not writable: {zip_path}") from exc
            finally:
                temp_zip_path.unlink(missing_ok=True)
        try:
            uploaded = _upload_file_to_tos_with_deadline(zip_storage_key, zip_path, "MODEL_PLAZA_GPU_ZIP_UPLOAD_TOTAL_TIMEOUT", 900)
        except RuntimeError as exc:
            if not _is_tos_retryable_upload_error(str(exc)):
                raise
            uploaded = _upload_file_to_tos_with_deadline(
                _retry_tos_object_key(zip_storage_key),
                zip_path,
                "MODEL_PLAZA_GPU_ZIP_UPLOAD_TOTAL_TIMEOUT",
                900,
            )
        if not uploaded:
            raise HTTPException(status_code=503, detail="TOS upload is not available")
    _cleanup_result_cache_for_watermark()
    return {
        "zip_id": zip_id,
        "filename": filename,
        "storage_key": uploaded["storage_key"],
        "url": uploaded["url"],
        "size_bytes": uploaded["size_bytes"],
        "local_path": str(zip_path),
    }


@app.on_event("startup")
def recover_incomplete_jobs_on_startup() -> None:
    # 启动恢复可能包含大文件补传，不能阻塞 /health 和新任务提交。
    threading.Thread(target=_recover_stale_jobs, name="recover-stale-gpu-jobs", daemon=True).start()
    threading.Thread(target=_cleanup_loop, name="cleanup-gpu-jobs", daemon=True).start()
    threading.Thread(target=_watchdog_loop, name="watchdog-gpu-jobs", daemon=True).start()


@app.get("/health")
def health() -> dict:
    running_by_gpu = _slot_usage_snapshot(GPU_DEVICE_IDS)
    active_runner_by_gpu = _active_runner_snapshot(GPU_DEVICE_IDS)
    gpu_preflight_error = _gpu_preflight_error()
    disk_preflight_error = _disk_preflight_error()
    return {
        "ok": not bool(gpu_preflight_error or disk_preflight_error),
        "gpu_preflight_error": gpu_preflight_error,
        "disk_preflight_error": disk_preflight_error,
        "disk": _disk_status(),
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
        "active_runner_by_gpu": active_runner_by_gpu,
    }


@app.get("/metrics")
def metrics(x_api_key: Annotated[str | None, Header(alias="X-API-Key")] = None) -> dict:
    _check_auth(x_api_key)
    return _gpu_metrics()


@app.post("/maintenance/cleanup")
def run_cleanup(x_api_key: Annotated[str | None, Header(alias="X-API-Key")] = None) -> dict:
    _check_auth(x_api_key)
    return _cleanup_once()


@app.post("/internal-batch-zips")
def create_internal_batch_zip(payload: dict, x_api_key: Annotated[str | None, Header(alias="X-API-Key")] = None) -> dict:
    _check_auth(x_api_key)
    return _create_internal_batch_zip_on_gpu(payload)


@app.get("/internal-batch-zips/{zip_id}/download")
def download_internal_batch_zip(zip_id: str, x_api_key: Annotated[str | None, Header(alias="X-API-Key")] = None):
    _check_auth(x_api_key)
    safe_zip_id = _safe_zip_id(zip_id)
    zip_path = ZIP_RESULTS_ROOT / f"{safe_zip_id}.zip"
    if not zip_path.exists() or not zip_path.is_file() or zip_path.stat().st_size <= 0:
        raise HTTPException(status_code=404, detail="zip not found")
    return FileResponse(zip_path, media_type="application/zip", filename=zip_path.name)


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
    if job_type not in {"propainter", "enhance", "translate", "subtitle_translate"}:
        raise HTTPException(status_code=400, detail="unsupported job type")
    if not isinstance(regions_json, list):
        raise HTTPException(status_code=400, detail="regions must be a JSON array")
    if job_type in {"propainter", "subtitle_translate"} and not regions_json:
        raise HTTPException(status_code=400, detail="regions must be a non-empty JSON array")
    if not isinstance(params_json, dict):
        raise HTTPException(status_code=400, detail="params must be a JSON object")
    with job_admission_lock:
        if _active_job_count() >= MAX_WORKERS:
            raise HTTPException(status_code=503, detail="GPU API queue is full")
        disk_preflight_error = _disk_preflight_error(run_cleanup=True)
        if disk_preflight_error:
            raise HTTPException(status_code=503, detail=disk_preflight_error)

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
