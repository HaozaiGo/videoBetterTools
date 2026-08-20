import importlib.util
import json
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
    monkeypatch.setattr(module, "_cancel_job_safely", lambda job_id, reason: cancelled.append(reason))

    with pytest.raises(module.GpuApiError, match="stalled"):
        module._poll_job("stuck-job")

    assert cancelled == ["stalled progress for 10s"]


def test_video_enhance_adapter_retries_transient_status_failures(monkeypatch) -> None:
    module = _load_script_module("video_enhance_api_adapter_retry_test", "scripts/gpu/video_enhance_api_adapter.py")
    clock = FakeClock()
    calls = {"count": 0}
    cancelled: list[str] = []

    monkeypatch.setenv("MODEL_PLAZA_GPU_JOB_LABEL", "translate")
    monkeypatch.setenv("MODEL_PLAZA_GPU_POLL_INTERVAL", "5")
    monkeypatch.setenv("MODEL_PLAZA_GPU_POLL_TIMEOUT", "120")
    monkeypatch.setenv("MODEL_PLAZA_GPU_STATUS_FAILURES", "3")
    monkeypatch.setattr(module.time, "time", clock.time)
    monkeypatch.setattr(module.time, "sleep", clock.sleep)
    monkeypatch.setattr(module, "_cancel_requested", lambda: False)
    monkeypatch.setattr(module, "_sync_progress", lambda job_id, status: None)
    monkeypatch.setattr(module, "_cancel_job_safely", lambda job_id, reason: cancelled.append(reason))

    def flaky_status(request, timeout=30):
        calls["count"] += 1
        if calls["count"] < 3:
            raise module.GpuApiRequestError("temporary timeout")
        return {"status": "succeeded"}

    monkeypatch.setattr(module, "_request_json", flaky_status)

    assert module._poll_job("retry-job") == {"status": "succeeded"}
    assert calls["count"] == 3
    assert cancelled == []


def test_gpu_api_auto_exclusive_detects_high_risk_video(monkeypatch, tmp_path) -> None:
    module = _load_script_module("propainter_api_auto_exclusive_test", "scripts/gpu/propainter_api_server.py")
    monkeypatch.setattr(module, "GPU_AUTO_EXCLUSIVE_ENABLED", True)
    monkeypatch.setattr(module, "GPU_AUTO_EXCLUSIVE_MIN_DURATION_SECONDS", 80)
    monkeypatch.setattr(module, "GPU_AUTO_EXCLUSIVE_MIN_FRAMES", 1800)
    monkeypatch.setattr(module, "GPU_AUTO_EXCLUSIVE_MIN_PIXELS", 1280 * 720)
    monkeypatch.setattr(
        module,
        "_probe_video_metadata",
        lambda path: {"duration": 95.0, "frames": 2400.0, "pixels": 1280.0 * 720.0},
    )

    reason = module._auto_exclusive_reason("subtitle_translate", {}, tmp_path / "input.mp4")

    assert "duration=95.0s" in reason
    assert "frames=2400" in reason
    assert "pixels=921600" in reason


def test_gpu_api_candidate_devices_prefers_free_memory(monkeypatch) -> None:
    module = _load_script_module("propainter_api_candidate_gpu_test", "scripts/gpu/propainter_api_server.py")
    monkeypatch.setattr(module, "GPU_DEVICE_IDS", ["2", "3", "5"])
    monkeypatch.setattr(module, "_gpu_free_memory_by_device", lambda: {"2": 12000, "3": 64000, "5": 32000})

    assert module._candidate_gpu_devices(min_free_memory_mib=24000) == ["3", "5"]
    assert module._candidate_gpu_devices(preferred_gpu="2", min_free_memory_mib=24000) == ["2"]


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


def test_gpu_api_server_metrics_can_monitor_non_worker_gpus(monkeypatch) -> None:
    module = _load_script_module("propainter_api_server_monitor_test", "scripts/gpu/propainter_api_server.py")
    monkeypatch.setattr(module, "GPU_DEVICE_IDS", ["3", "6", "7"])
    monkeypatch.setattr(module, "GPU_MONITOR_DEVICE_IDS", ["0", "1", "3", "6", "7"])
    monkeypatch.setattr(module, "GPU_WORKERS_PER_DEVICE", 2)
    monkeypatch.setattr(module, "GPU_SLOT_CAPACITY_BY_DEVICE", {"3": 1, "6": 2, "7": 2})
    monkeypatch.setattr(module, "GPU_SLOT_CAPACITY", 5)

    def fake_run_command(command, timeout=5):
        assert command[1] == "--id=0,1,3,6,7"
        return "\n".join(
            [
                "index, name, utilization.gpu [%], utilization.memory [%], memory.used [MiB], memory.total [MiB], temperature.gpu, power.draw [W]",
                "0, NVIDIA RTX PRO 6000 Blackwell Server Edition, 1 %, 0 %, 100 MiB, 97894 MiB, 40, 80 W",
                "1, NVIDIA RTX PRO 6000 Blackwell Server Edition, 2 %, 0 %, 200 MiB, 97894 MiB, 41, 82 W",
                "3, NVIDIA RTX PRO 6000 Blackwell Server Edition, 5 %, 1 %, 250 MiB, 97894 MiB, 42, 83 W",
                "6, NVIDIA RTX PRO 6000 Blackwell Server Edition, 3 %, 1 %, 300 MiB, 97894 MiB, 42, 84 W",
                "7, NVIDIA RTX PRO 6000 Blackwell Server Edition, 4 %, 1 %, 400 MiB, 97894 MiB, 43, 86 W",
            ]
        )

    monkeypatch.setattr(module, "_run_command", fake_run_command)
    module.gpu_slot_usage = {"3": 0, "6": 0, "7": 2}
    module.running_gpu_devices = {"job-7": "7"}

    payload = module._gpu_metrics()

    assert payload["gpuDevices"] == ["3", "6", "7"]
    assert payload["monitorGpuDevices"] == ["0", "1", "3", "6", "7"]
    assert payload["workerSlotCapacityByGpu"] == {"3": 1, "6": 2, "7": 2}
    assert [gpu["index"] for gpu in payload["gpus"]] == ["0", "1", "3", "6", "7"]
    assert [gpu["workerSlotsTotal"] for gpu in payload["gpus"]] == [0, 0, 1, 2, 2]
    assert [gpu["workerSlotsUsed"] for gpu in payload["gpus"]] == [0, 0, 0, 0, 2]
    assert payload["runningByGpu"] == {"0": 0, "1": 0, "3": 0, "6": 0, "7": 2}
    assert payload["activeRunnerByGpu"] == {"0": 0, "1": 0, "3": 0, "6": 0, "7": 1}


def test_gpu_api_server_running_jobs_include_display_metadata(tmp_path, monkeypatch) -> None:
    module = _load_script_module("propainter_api_server_running_display_test", "scripts/gpu/propainter_api_server.py")
    job_id = "remote-display-job"
    job_dir = tmp_path / "jobs" / job_id
    job_dir.mkdir(parents=True)
    (job_dir / "status.json").write_text(
        '{"status":"processing","job_type":"subtitle_translate","created_at":100,"started_at":110,'
        '"assigned_gpu":"3","progress_percent":10,"progress_stage":"开始去字幕"}',
        encoding="utf-8",
    )
    (job_dir / "params.json").write_text(
        '{"providerJobId":"provider-display","internalBatchId":"batch-display",'
        '"internalBatchName":"恰好的意外（53集）擦边剧","internalBatchIndex":46,"_inputAssetName":"48.mp4"}',
        encoding="utf-8",
    )
    monkeypatch.setattr(module, "JOBS_ROOT", tmp_path / "jobs")
    monkeypatch.setattr(module.time, "time", lambda: 140)

    jobs = module._running_jobs_snapshot()

    assert jobs == [
        {
            "id": job_id,
            "status": "processing",
            "jobType": "subtitle_translate",
            "providerJobId": "provider-display",
            "inputAssetName": "48.mp4",
            "internalBatchId": "batch-display",
            "internalBatchName": "恰好的意外（53集）擦边剧",
            "displayName": "恰好的意外（53集）擦边剧",
            "displaySubtitle": "第 46 集 · 48.mp4",
            "assignedGpu": "3",
            "progressPercent": 10,
            "progressStage": "开始去字幕",
            "runningSeconds": 30,
            "logPath": "",
        }
    ]


def test_gpu_api_server_rejects_unsafe_result_cache_paths() -> None:
    module = _load_script_module("propainter_api_server_cache_path_test", "scripts/gpu/propainter_api_server.py")

    assert module._safe_relative_path("model-plaza/output/videos/result.mp4").parts == (
        "model-plaza",
        "output",
        "videos",
        "result.mp4",
    )
    with pytest.raises(ValueError):
        module._safe_relative_path("../result.mp4")


def test_gpu_api_server_subtitle_translate_runs_locally_and_uploads_once(tmp_path, monkeypatch) -> None:
    module = _load_script_module("propainter_api_server_workflow_test", "scripts/gpu/propainter_api_server.py")
    job_id = "workflowjob123"
    job_dir = tmp_path / "jobs" / job_id
    job_dir.mkdir(parents=True)
    (job_dir / "input.mp4").write_bytes(b"input-video")
    (job_dir / "regions.json").write_text('[{"x":0,"y":0.75,"width":1,"height":0.08}]', encoding="utf-8")
    (job_dir / "params.json").write_text('{"targetLanguage":"en","subtitlePlacement":"bottom"}', encoding="utf-8")
    (job_dir / "status.json").write_text('{"status":"queued","job_type":"subtitle_translate","created_at":1}', encoding="utf-8")

    commands: list[list[str]] = []
    uploaded: list[Path] = []
    events: list[tuple[str, str | Path | None]] = []

    class FakeProcess:
        def __init__(self, command, **kwargs) -> None:
            self.command = list(command)
            commands.append(self.command)

        def wait(self) -> int:
            output_path = Path(self.command[self.command.index("--output") + 1])
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_bytes(b"output-video")
            return 0

        def poll(self):
            return 0

    monkeypatch.setattr(module, "JOBS_ROOT", tmp_path / "jobs")
    monkeypatch.setattr(module, "LOGS_ROOT", tmp_path / "logs")
    monkeypatch.setattr(module, "PROPAINTER_RUNNER_PATH", tmp_path / "propainter_runner.py")
    monkeypatch.setattr(module, "TRANSLATE_RUNNER_PATH", tmp_path / "video_translate_runner.py")
    monkeypatch.setattr(module, "PYTHON_PATH", sys.executable)
    monkeypatch.setattr(module, "UPLOAD_RESULTS", True)
    monkeypatch.setattr(module, "_require_gpu_preflight", lambda: None)
    monkeypatch.setattr(module, "_auto_exclusive_reason", lambda *args, **kwargs: "")
    monkeypatch.setattr(module, "_acquire_gpu_slot", lambda *args, **kwargs: "0")
    monkeypatch.setattr(module, "_release_gpu_slot", lambda job_id, gpu_device: events.append(("release", gpu_device)))
    monkeypatch.setattr(module.subprocess, "Popen", lambda command, **kwargs: FakeProcess(command, **kwargs))
    monkeypatch.setattr(module, "_upload_result_with_deadline", lambda job_id, output_path: events.append(("upload", output_path)) or uploaded.append(output_path) or {
        "result_storage_key": "model-plaza/output/videos/final.mp4",
        "result_url": "https://cdn.example.test/final.mp4",
        "result_size_bytes": output_path.stat().st_size,
    })
    monkeypatch.setattr(module, "_persist_result_cache", lambda storage_key, output_path: output_path)
    monkeypatch.setattr(module, "_cleanup_result_cache_for_watermark", lambda: None)

    module._run_model_job(job_id)

    assert len(commands) == 2
    assert commands[0][1].endswith("propainter_runner.py")
    assert commands[0][commands[0].index("--input") + 1].endswith("input.mp4")
    assert commands[0][commands[0].index("--status-path") + 1].endswith("status.json")
    intermediate_output = commands[0][commands[0].index("--output") + 1]
    assert intermediate_output.endswith("subtitle-removed.mp4")
    assert commands[1][1].endswith("video_translate_runner.py")
    assert commands[1][commands[1].index("--input") + 1] == intermediate_output
    assert len(uploaded) == 1
    assert uploaded[0] == job_dir / "output.mp4"
    assert events[:2] == [("release", "0"), ("upload", job_dir / "output.mp4")]
    status = module._read_status(job_id)
    assert status["status"] == "succeeded"
    assert status["result_storage_key"] == "model-plaza/output/videos/final.mp4"


def test_propainter_runner_updates_status_for_chunks(tmp_path, monkeypatch) -> None:
    module = _load_script_module("propainter_runner_status_test", "scripts/gpu/propainter_runner.py")
    frames_dir = tmp_path / "frames"
    masks_dir = tmp_path / "masks"
    frames_dir.mkdir()
    masks_dir.mkdir()
    for index in range(1, 6):
        (frames_dir / f"{index:06d}.png").write_bytes(b"frame")
        (masks_dir / f"{index:06d}.png").write_bytes(b"mask")
    status_path = tmp_path / "status.json"
    status_path.write_text('{"status":"processing","progress_percent":10,"progress_stage":"开始去字幕"}', encoding="utf-8")

    def fake_run_propainter_command(command, cwd=None):
        output_dir = Path(command[command.index("--output") + 1])
        frames_name = Path(command[command.index("--video") + 1]).name
        result = output_dir / frames_name / "inpaint_out.mp4"
        result.parent.mkdir(parents=True, exist_ok=True)
        result.write_bytes(b"chunk")

    monkeypatch.setattr(module, "_run_propainter_command", fake_run_propainter_command)
    monkeypatch.setattr(module, "_concat_videos", lambda parts, output_path, workdir: output_path.write_bytes(b"merged"))

    merged = module._run_propainter_once(
        sys.executable,
        tmp_path,
        frames_dir,
        masks_dir,
        tmp_path / "results",
        25,
        360,
        640,
        5,
        {"propainterChunkFrames": 2, "propainterLongVideoFrames": 2},
        "subtitle-text",
        tmp_path / "work",
        status_path,
    )

    assert merged.exists()
    status = json.loads(status_path.read_text(encoding="utf-8"))
    assert status["progress_percent"] == 52
    assert status["progress_stage"] == "去字幕 3/3 完成，正在合并分片"
