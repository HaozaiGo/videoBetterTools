import importlib.util
import subprocess
import sys
import time
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]


def _load_script_module(name: str, relative_path: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative_path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


class FakeClock:
    def __init__(self) -> None:
        self.value = 0.0

    def time(self) -> float:
        return self.value

    def sleep(self, seconds: float) -> None:
        self.value += seconds


def test_propainter_adapter_cancels_stalled_processing_job(monkeypatch) -> None:
    module = _load_script_module("propainter_api_adapter_test", "scripts/gpu/propainter_api_adapter.py")
    clock = FakeClock()
    cancelled: list[str] = []

    monkeypatch.setenv("MODEL_PLAZA_GPU_POLL_INTERVAL", "5")
    monkeypatch.setenv("MODEL_PLAZA_GPU_POLL_TIMEOUT", "120")
    monkeypatch.setenv("MODEL_PLAZA_GPU_STALL_TIMEOUT_SECONDS", "10")
    monkeypatch.setattr(module.time, "time", clock.time)
    monkeypatch.setattr(module.time, "sleep", clock.sleep)
    monkeypatch.setattr(module, "_cancel_requested", lambda: False)
    monkeypatch.setattr(module, "_sync_progress", lambda job_id, status: None)
    monkeypatch.setattr(module, "_request_json", lambda request, timeout=30: {
        "status": "processing",
        "progress_percent": 8,
        "progress_stage": "远端 GPU 4 已领取任务",
    })
    monkeypatch.setattr(module, "_cancel_job_safely", lambda job_id, reason: cancelled.append(reason))

    with pytest.raises(module.GpuApiError, match="stalled"):
        module._poll_job("stuck-job")

    assert cancelled == ["stalled progress for 10s"]


def test_video_enhance_adapter_cancels_stalled_processing_job(monkeypatch) -> None:
    module = _load_script_module("video_enhance_api_adapter_test", "scripts/gpu/video_enhance_api_adapter.py")
    clock = FakeClock()
    cancelled: list[str] = []

    monkeypatch.setenv("MODEL_PLAZA_GPU_JOB_LABEL", "enhance")
    monkeypatch.setenv("MODEL_PLAZA_GPU_POLL_INTERVAL", "5")
    monkeypatch.setenv("MODEL_PLAZA_GPU_POLL_TIMEOUT", "120")
    monkeypatch.setenv("MODEL_PLAZA_GPU_STALL_TIMEOUT_SECONDS", "10")
    monkeypatch.setattr(module.time, "time", clock.time)
    monkeypatch.setattr(module.time, "sleep", clock.sleep)
    monkeypatch.setattr(module, "_cancel_requested", lambda: False)
    monkeypatch.setattr(module, "_sync_progress", lambda job_id, status: None)
    monkeypatch.setattr(module, "_request_json", lambda request, timeout=30: {
        "status": "processing",
        "progress_percent": 8,
        "progress_stage": "远端 GPU 5 已领取任务",
    })
    monkeypatch.setattr(module, "_cancel_job", lambda job_id: cancelled.append(job_id))

    with pytest.raises(module.GpuApiError, match="stalled"):
        module._poll_job("stuck-job")

    assert cancelled == ["stuck-job"]


@pytest.mark.skipif(not Path("/proc").exists(), reason="process tree cleanup uses Linux /proc")
def test_gpu_api_server_terminates_orphan_job_processes() -> None:
    module = _load_script_module("propainter_api_server_test", "scripts/gpu/propainter_api_server.py")
    job_id = "orphanjob123"
    process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import time; time.sleep(60)",
            f"/shared/work/api-jobs/{job_id}/runner-work/chunks/chunk-0001",
        ]
    )
    try:
        deadline = time.time() + 5
        while time.time() < deadline and process.poll() is not None:
            time.sleep(0.1)

        module._terminate_job_processes(job_id, reason="test cleanup")

        deadline = time.time() + 5
        while time.time() < deadline and process.poll() is None:
            time.sleep(0.1)

        assert process.poll() is not None
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)


def test_gpu_api_server_watchdog_treats_log_mtime_as_activity(tmp_path, monkeypatch) -> None:
    module = _load_script_module("propainter_api_server_watchdog_test", "scripts/gpu/propainter_api_server.py")
    job_id = "activejob123"
    job_dir = tmp_path / "jobs" / job_id
    log_dir = tmp_path / "logs"
    log_path = log_dir / f"{job_id}.log"
    job_dir.mkdir(parents=True)
    log_dir.mkdir(parents=True)
    log_path.write_text("still working\n", encoding="utf-8")
    status_path = job_dir / "status.json"
    status_path.write_text(
        '{"status":"processing","progress_percent":8,"progress_stage":"远端 GPU 4 已领取任务","log_path":"%s"}' % log_path,
        encoding="utf-8",
    )

    class FakeProcess:
        def poll(self):
            return None

    monkeypatch.setattr(module, "JOBS_ROOT", tmp_path / "jobs")
    monkeypatch.setattr(module, "GPU_STALL_TIMEOUT_SECONDS", 10)
    monkeypatch.setattr(module.time, "time", lambda: 100.0)
    module.running_processes = {job_id: FakeProcess()}
    module.running_progress_snapshots = {
        job_id: (("processing", 8, "远端 GPU 4 已领取任务"), 0.0, 0.0)
    }

    result = module._watchdog_once()

    assert result["stalled_jobs_cancelled"] == []
    assert module.running_progress_snapshots[job_id][1] == 100.0
