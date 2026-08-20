from pathlib import Path
from datetime import timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app import worker
from app.models import Asset, Base, Task, User, Wallet
from app.services import now


class FakeStorage:
    is_remote = True

    def __init__(self) -> None:
        self.saved: list[tuple[str, Path]] = []
        self.deleted: list[str] = []

    def save_file(self, storage_key: str, local_path: Path):
        self.saved.append((storage_key, local_path))

        class Stored:
            size = local_path.stat().st_size
            public_url = f"https://cdn.example.test/{storage_key}"

            def __init__(self, key: str) -> None:
                self.storage_key = key

        return Stored(storage_key)

    def delete_local_copy(self, storage_key: str) -> bool:
        self.deleted.append(storage_key)
        return True

    def remote_exists(self, storage_key: str) -> bool:
        return True


class MissingRemoteStorage(FakeStorage):
    def remote_exists(self, storage_key: str) -> bool:
        return False


def test_finalize_result_payload_uploads_local_file(monkeypatch, tmp_path) -> None:
    output = tmp_path / "result.mp4"
    output.write_bytes(b"video")
    fake_storage = FakeStorage()
    monkeypatch.setattr(worker, "storage", fake_storage)

    finalized = worker._finalize_result_payload(
        {
            "storage_key": "model-plaza/output/videos/result.mp4",
            "local_path": str(output),
            "mime_type": "video/mp4",
        }
    )

    assert finalized == {
        "storage_key": "model-plaza/output/videos/result.mp4",
        "url": "https://cdn.example.test/model-plaza/output/videos/result.mp4",
        "mime_type": "video/mp4",
        "size_bytes": 5,
    }
    assert fake_storage.saved == [("model-plaza/output/videos/result.mp4", output)]
    assert fake_storage.deleted == ["model-plaza/output/videos/result.mp4"]


def test_real_video_task_fails_before_gpu_when_input_remote_missing(monkeypatch) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as db:
        user = User(id="user-worker-missing", email="worker-missing@example.com", name="Worker Missing", role="user", status="active")
        wallet = Wallet(user_id=user.id, credits=100, frozen_credits=10)
        asset = Asset(
            id="asset-worker-missing",
            user_id=user.id,
            kind="video",
            original_name="missing.mp4",
            mime_type="video/mp4",
            storage_key="missing.mp4",
            url="https://tos.example.test/missing.mp4",
            size_bytes=1024,
            duration_seconds=70,
            expires_at=now() + timedelta(days=1),
        )
        task = Task(
            id="task-worker-missing",
            user_id=user.id,
            tool_slug="remove-subtitle",
            input_asset_id=asset.id,
            status="queued",
            params={"modelAdapter": "propainter"},
            estimated_credits=10,
            frozen_credits=10,
            provider="mock",
            provider_job_id="provider-worker-missing",
            progress_percent=5,
            progress_stage="worker 已领取，准备提交远端任务",
        )
        db.add_all([user, wallet, asset, task])
        db.commit()

    session_factory = lambda: Session(engine)
    monkeypatch.setattr(worker, "SessionLocal", session_factory)
    monkeypatch.setattr(worker, "storage", MissingRemoteStorage())
    monkeypatch.setattr(worker, "process_subtitle_removal", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("GPU should not be called")))

    worker._process_real_video_task("task-worker-missing")

    with Session(engine) as db:
        task = db.get(Task, "task-worker-missing")
        wallet = db.get(Wallet, "user-worker-missing")
        assert task is not None
        assert wallet is not None
        assert task.status == "failed"
        assert task.error_code == worker.INPUT_ASSET_REMOTE_MISSING_ERROR_CODE
        assert task.progress_stage == worker.INPUT_ASSET_REMOTE_MISSING_MESSAGE
        assert wallet.frozen_credits == 0


def test_real_video_task_skips_duplicate_job_after_task_is_processing(monkeypatch) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as db:
        user = User(id="user-worker-processing", email="worker-processing@example.com", name="Worker Processing", role="user", status="active")
        wallet = Wallet(user_id=user.id, credits=100, frozen_credits=10)
        asset = Asset(
            id="asset-worker-processing",
            user_id=user.id,
            kind="video",
            original_name="processing.mp4",
            mime_type="video/mp4",
            storage_key="processing.mp4",
            url="https://cdn.example.test/processing.mp4",
            size_bytes=1024,
            duration_seconds=70,
            expires_at=now() + timedelta(days=1),
        )
        task = Task(
            id="task-worker-processing",
            user_id=user.id,
            tool_slug="translate",
            input_asset_id=asset.id,
            status="processing",
            params={},
            estimated_credits=10,
            frozen_credits=10,
            provider="mock",
            provider_job_id="provider-worker-processing",
            progress_percent=10,
            progress_stage="远端 GPU 已提交，等待处理",
        )
        db.add_all([user, wallet, asset, task])
        db.commit()

    session_factory = lambda: Session(engine)
    monkeypatch.setattr(worker, "SessionLocal", session_factory)
    monkeypatch.setattr(worker, "process_video_translate", lambda *_args, **_kwargs: pytest.fail("processing duplicate should not submit again"))

    worker.process_provider_job("task-worker-processing")

    with Session(engine) as db:
        task = db.get(Task, "task-worker-processing")
        assert task is not None
        assert task.status == "processing"
        assert task.progress_stage == "远端 GPU 已提交，等待处理"


def test_claim_provider_job_marks_queued_task_processing(monkeypatch) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as db:
        user = User(id="user-claim", email="claim@example.com", name="Claim", role="user", status="active")
        wallet = Wallet(user_id=user.id, credits=100, frozen_credits=10)
        asset = Asset(
            id="asset-claim",
            user_id=user.id,
            kind="video",
            original_name="claim.mp4",
            mime_type="video/mp4",
            storage_key="claim.mp4",
            url="https://cdn.example.test/claim.mp4",
            size_bytes=1024,
            duration_seconds=70,
            expires_at=now() + timedelta(days=1),
        )
        task = Task(
            id="task-claim",
            user_id=user.id,
            tool_slug="translate",
            input_asset_id=asset.id,
            status="queued",
            params={},
            estimated_credits=10,
            frozen_credits=10,
            provider="mock",
            provider_job_id="provider-claim",
            progress_percent=0,
            progress_stage="等待 worker 领取任务",
        )
        db.add_all([user, wallet, asset, task])
        db.commit()

    monkeypatch.setattr(worker, "SessionLocal", lambda: Session(engine))

    assert worker._claim_provider_job("task-claim") == ("provider-claim", "translate")
    assert worker._claim_provider_job("task-claim") is None

    with Session(engine) as db:
        task = db.get(Task, "task-claim")
        assert task is not None
        assert task.status == "processing"
        assert task.progress_stage == "worker 已领取，准备提交远端任务"


def test_finalize_remote_gpu_result_uses_direct_upload_metadata(monkeypatch) -> None:
    monkeypatch.setattr(worker, "storage", FakeStorage())
    monkeypatch.setattr(worker, "_sync_remote_gpu_progress", lambda *args, **kwargs: True)
    monkeypatch.setattr(
        worker,
        "get_remote_video_job",
        lambda job_id: {
            "status": "succeeded",
            "result_storage_key": "model-plaza/output/videos/result.mp4",
            "result_url": "https://cdn.example.test/model-plaza/output/videos/result.mp4",
            "result_mime_type": "video/mp4",
            "result_size_bytes": 123,
        },
    )

    finalized = worker._finalize_remote_gpu_result(
        "task-1",
        "provider-1",
        {
            "remote_job_id": "remote-1",
            "storage_key": "model-plaza/output/videos/result.mp4",
            "url": "https://cdn.example.test/model-plaza/output/videos/result.mp4",
        },
    )

    assert finalized == {
        "storage_key": "model-plaza/output/videos/result.mp4",
        "url": "https://cdn.example.test/model-plaza/output/videos/result.mp4",
        "mime_type": "video/mp4",
        "size_bytes": 123,
    }


def test_finalize_remote_gpu_result_waits_for_uploaded_object_visibility(monkeypatch) -> None:
    class InvisibleStorage:
        is_remote = True

        def remote_exists(self, storage_key: str) -> bool:
            return False

    monkeypatch.setattr(worker, "storage", InvisibleStorage())
    monkeypatch.setattr(worker, "_sync_remote_gpu_progress", lambda *args, **kwargs: True)
    monkeypatch.setattr(
        worker,
        "get_remote_video_job",
        lambda job_id: {
            "status": "succeeded",
            "result_storage_key": "model-plaza/output/videos/result.mp4",
            "result_url": "https://cdn.example.test/model-plaza/output/videos/result.mp4",
            "result_mime_type": "video/mp4",
            "result_size_bytes": 123,
        },
    )
    monkeypatch.setattr(worker, "cancel_remote_video_job", lambda job_id: None)

    with pytest.raises(worker.RemoteGpuUnavailableError, match="not visible in storage yet"):
        worker._finalize_remote_gpu_result(
            "task-1",
            "provider-1",
            {
                "remote_job_id": "remote-1",
                "storage_key": "model-plaza/output/videos/result.mp4",
                "url": "https://cdn.example.test/model-plaza/output/videos/result.mp4",
            },
        )


def test_finalize_remote_gpu_result_recovers_upload_timeout_from_gpu_cache(monkeypatch, tmp_path) -> None:
    fake_storage = FakeStorage()
    callbacks: list[dict] = []

    class DummySession:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

    def fake_provider_callback(db, provider_job_id: str, status: str, **kwargs):
        callbacks.append({"provider_job_id": provider_job_id, "status": status, **kwargs})

    def fake_download(job_id: str, output_path: Path) -> None:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(b"recovered-video")

    monkeypatch.setattr(worker.settings, "upload_dir", str(tmp_path))
    monkeypatch.setattr(worker, "storage", fake_storage)
    monkeypatch.setattr(worker, "SessionLocal", lambda: DummySession())
    monkeypatch.setattr(worker, "provider_callback", fake_provider_callback)
    monkeypatch.setattr(worker, "_sync_remote_gpu_progress", lambda *args, **kwargs: True)
    monkeypatch.setattr(worker, "download_remote_video_result", fake_download)
    monkeypatch.setattr(
        worker,
        "get_remote_video_job",
        lambda job_id: {
            "status": "failed",
            "error": "RESULT_UPLOAD_TIMEOUT: 结果上传超过 1800s，视频已生成但上传对象存储超时",
        },
    )

    finalized = worker._finalize_remote_gpu_result(
        "task-1",
        "provider-1",
        {
            "remote_job_id": "remote-1",
            "storage_key": "model-plaza/output/videos/result.mp4",
            "url": "https://cdn.example.test/model-plaza/output/videos/result.mp4",
        },
    )

    assert finalized["storage_key"] == "model-plaza/output/videos/result.mp4"
    assert finalized["size_bytes"] == len(b"recovered-video")
    assert fake_storage.saved == [(finalized["storage_key"], tmp_path / finalized["storage_key"])]
    assert callbacks == [
        {
            "provider_job_id": "provider-1",
            "status": "processing",
            "callback_id": "provider-1:remote-1:recover-result-upload-timeout",
            "progress_percent": 98,
            "progress_stage": "远端结果已生成，上传超时，正在改由平台拉回",
        }
    ]


def test_remote_gpu_failure_error_code_preserves_runner_failures() -> None:
    assert worker._remote_gpu_failure_error_code(worker.RemoteGpuError("remote GPU job failed: CUDA_OUT_OF_MEMORY")) == "CUDA_OUT_OF_MEMORY"
    assert worker._remote_gpu_failure_error_code(
        worker.RemoteGpuError("remote GPU job failed: ASR_NO_SEGMENTS: speech recognition returned no subtitle segments")
    ) == "ASR_NO_SEGMENTS"
    assert worker._remote_gpu_failure_error_code(worker.RemoteGpuError("remote GPU job failed: VIDEO_DECODE_FAILED")) == "VIDEO_DECODE_FAILED"
    assert worker._remote_gpu_failure_error_code(worker.RemoteGpuError("remote GPU job failed: RESULT_UPLOAD_TIMEOUT")) == "RESULT_UPLOAD_TIMEOUT"
    assert worker._remote_gpu_failure_error_code(RuntimeError("tos upload failed")) == "RESULT_UPLOAD_FAILED"


def test_remember_remote_gpu_job_stores_latest_and_history() -> None:
    task = Task(
        id="task-remote-memory",
        user_id="user-remote-memory",
        tool_slug="subtitle-translate-workflow",
        input_asset_id="asset-remote-memory",
        status="processing",
        params={"remoteGpuJobIds": ["remote-old"]},
        estimated_credits=1,
        frozen_credits=1,
        provider="mock",
        provider_job_id="provider-remote-memory",
    )

    worker._remember_remote_gpu_job(task, "remote-new", "subtitle_translate")

    assert task.params["remoteGpuJobId"] == "remote-new"
    assert task.params["remoteGpuJobType"] == "subtitle_translate"
    assert task.params["remoteGpuJobIds"] == ["remote-old", "remote-new"]


def test_finalize_result_not_ready_defers_without_failing(monkeypatch) -> None:
    result = {"remote_job_id": "remote-pending", "storage_key": "model-plaza/output/videos/result.mp4"}
    deferred: list[tuple[str, str, str]] = []
    enqueued: list[tuple[str, str, dict, int]] = []

    def fake_finalize(task_id: str, provider_job_id: str, payload: dict) -> dict:
        raise worker.RemoteGpuResultNotReady("remote GPU job is still queued", payload, delay_seconds=45)

    monkeypatch.setattr(worker, "_should_skip_result_finalize", lambda task_id, provider_job_id: False)
    monkeypatch.setattr(worker, "_finalize_remote_gpu_result", fake_finalize)
    monkeypatch.setattr(worker, "_mark_result_finalize_deferred", lambda task_id, provider_job_id, stage: deferred.append((task_id, provider_job_id, stage)))
    monkeypatch.setattr(
        worker,
        "enqueue_result_finalize_job",
        lambda task_id, provider_job_id, payload, delay_seconds=0: enqueued.append((task_id, provider_job_id, payload, delay_seconds)),
    )
    monkeypatch.setattr(worker, "_fail_provider_job", lambda *args, **kwargs: pytest.fail("not-ready result should not fail task"))

    worker.finalize_provider_job_result("task-pending", "provider-pending", result)

    assert deferred == [("task-pending", "provider-pending", "remote GPU job is still queued")]
    assert enqueued == [("task-pending", "provider-pending", result, 45)]


def test_permanent_remote_gpu_failure_marks_failed_without_retry(monkeypatch) -> None:
    result = {"remote_job_id": "remote-failed", "storage_key": "model-plaza/output/videos/result.mp4"}
    failures: list[tuple[str, str, str]] = []

    def fake_finalize(task_id: str, provider_job_id: str, payload: dict) -> dict:
        raise worker.RemoteGpuError("remote GPU job failed: CUDA_OUT_OF_MEMORY")

    monkeypatch.setattr(worker, "_should_skip_result_finalize", lambda task_id, provider_job_id: False)
    monkeypatch.setattr(worker, "_finalize_remote_gpu_result", fake_finalize)
    monkeypatch.setattr(
        worker,
        "_fail_provider_job",
        lambda provider_job_id, error_code, message: failures.append((provider_job_id, error_code, message)),
    )

    worker.finalize_provider_job_result("task-failed", "provider-failed", result)

    assert failures == [("provider-failed", "CUDA_OUT_OF_MEMORY", "remote GPU job failed: CUDA_OUT_OF_MEMORY")]


def test_finalize_skips_terminal_or_stale_tasks(monkeypatch) -> None:
    monkeypatch.setattr(worker, "_should_skip_result_finalize", lambda task_id, provider_job_id: True)
    monkeypatch.setattr(worker, "_finalize_remote_gpu_result", lambda *args, **kwargs: pytest.fail("terminal task should skip finalize"))
    monkeypatch.setattr(worker, "_finalize_result_payload", lambda *args, **kwargs: pytest.fail("terminal task should skip finalize"))

    worker.finalize_provider_job_result(
        "task-terminal",
        "provider-terminal",
        {"remote_job_id": "remote-terminal", "storage_key": "model-plaza/output/videos/result.mp4"},
    )


def test_lost_remote_gpu_job_requeues_platform_task(monkeypatch) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as db:
        user = User(id="user-lost-gpu", email="lost-gpu@example.com", name="Lost GPU", role="user", status="active")
        wallet = Wallet(user_id=user.id, credits=100, frozen_credits=10)
        asset = Asset(
            id="asset-lost-gpu",
            user_id=user.id,
            kind="input",
            original_name="input.mp4",
            mime_type="video/mp4",
            storage_key="input.mp4",
            url="https://cdn.example.test/input.mp4",
            size_bytes=1024,
            duration_seconds=70,
            expires_at=now() + timedelta(days=1),
        )
        task = Task(
            id="task-lost-gpu",
            user_id=user.id,
            tool_slug="subtitle-translate-workflow",
            input_asset_id=asset.id,
            status="processing",
            params={"remoteGpuJobId": "remote-lost", "remoteGpuJobType": "subtitle_translate"},
            estimated_credits=10,
            frozen_credits=10,
            provider="mock",
            provider_job_id="provider-lost-gpu",
            progress_percent=10,
            progress_stage="远端 GPU 已提交，等待处理",
        )
        db.add_all([user, wallet, asset, task])
        db.commit()

    monkeypatch.setattr(worker, "SessionLocal", lambda: Session(engine))
    monkeypatch.setattr(worker.settings, "remote_gpu_job_not_found_retry_max", 3)

    worker._requeue_provider_job_for_lost_remote_gpu_job(
        "task-lost-gpu",
        "provider-lost-gpu",
        {"remote_job_id": "remote-lost"},
        'GPU API HTTP 404: {"detail":"job not found"}',
    )

    with Session(engine) as db:
        task = db.get(Task, "task-lost-gpu")
        wallet = db.get(Wallet, "user-lost-gpu")
        assert task is not None
        assert wallet is not None
        assert task.status == "queued"
        assert task.error_code is None
        assert task.params["_remoteGpuJobNotFoundRetries"] == 1
        assert task.params["remoteGpuJobNotFoundIds"] == ["remote-lost"]
        assert "remoteGpuJobId" not in task.params
        assert "等待重新提交" in task.progress_stage
        assert wallet.frozen_credits == 10


def test_gpu_unavailable_retries_exhaust_to_failed(monkeypatch) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as db:
        user = User(id="user-gpu-retry", email="gpu-retry@example.com", name="GPU Retry", role="user", status="active")
        wallet = Wallet(user_id=user.id, credits=100, frozen_credits=10)
        asset = Asset(
            id="asset-gpu-retry",
            user_id=user.id,
            kind="input",
            original_name="input.mp4",
            mime_type="video/mp4",
            storage_key="missing.mp4",
            url="https://cdn.example.test/missing.mp4",
            size_bytes=1024,
            duration_seconds=70,
            expires_at=now() + timedelta(days=1),
        )
        task = Task(
            id="task-gpu-retry",
            user_id=user.id,
            tool_slug="translate",
            input_asset_id=asset.id,
            status="queued",
            params={"_gpuUnavailableRetries": 1},
            estimated_credits=10,
            frozen_credits=10,
            provider="mock",
            provider_job_id="provider-gpu-retry",
            progress_percent=5,
            progress_stage="远端 GPU 暂不可用，等待自动重试",
        )
        db.add_all([user, wallet, asset, task])
        db.commit()

    session_factory = lambda: Session(engine)
    enqueued: list[str] = []
    monkeypatch.setattr(worker, "SessionLocal", session_factory)
    monkeypatch.setattr(worker.settings, "gpu_unavailable_retry_max", 1)
    monkeypatch.setattr(worker, "enqueue_provider_job", lambda task_id: enqueued.append(task_id))

    worker._requeue_provider_job_for_gpu_unavailable(
        "task-gpu-retry",
        "provider-gpu-retry",
        "input asset is not available in remote storage after waiting 120s",
    )

    with Session(engine) as db:
        task = db.get(Task, "task-gpu-retry")
        wallet = db.get(Wallet, "user-gpu-retry")
        assert task is not None
        assert wallet is not None
        assert task.status == "failed"
        assert task.error_code == "INPUT_ASSET_REMOTE_MISSING"
        assert task.params["_gpuUnavailableRetries"] == 2
        assert wallet.frozen_credits == 0
    assert enqueued == []


def test_gpu_queue_full_does_not_requeue_bound_remote_job(monkeypatch) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as db:
        user = User(id="user-bound-remote", email="bound-remote@example.com", name="Bound Remote", role="user", status="active")
        wallet = Wallet(user_id=user.id, credits=100, frozen_credits=10)
        asset = Asset(
            id="asset-bound-remote",
            user_id=user.id,
            kind="video",
            original_name="bound.mp4",
            mime_type="video/mp4",
            storage_key="bound.mp4",
            url="https://cdn.example.test/bound.mp4",
            size_bytes=1024,
            duration_seconds=70,
            expires_at=now() + timedelta(days=1),
        )
        task = Task(
            id="task-bound-remote",
            user_id=user.id,
            tool_slug="subtitle-translate-workflow",
            input_asset_id=asset.id,
            status="processing",
            params={"remoteGpuJobId": "remote-running", "_gpuQueueFullRetries": 2},
            estimated_credits=10,
            frozen_credits=10,
            provider="mock",
            provider_job_id="provider-bound-remote",
            progress_percent=10,
            progress_stage="远端 GPU 已提交，等待处理",
        )
        db.add_all([user, wallet, asset, task])
        db.commit()

    monkeypatch.setattr(worker, "SessionLocal", lambda: Session(engine))

    worker._requeue_provider_job_for_gpu_unavailable(
        "task-bound-remote",
        "provider-bound-remote",
        'GPU API HTTP 503: {"detail":"GPU API queue is full"}',
    )

    with Session(engine) as db:
        task = db.get(Task, "task-bound-remote")
        assert task is not None
        assert task.status == "processing"
        assert task.params["_gpuQueueFullRetries"] == 2
        assert task.params["remoteGpuJobId"] == "remote-running"
        assert task.progress_stage == "远端 GPU 已提交，等待处理"


def test_gpu_disk_pressure_requeues_as_backpressure(monkeypatch) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as db:
        user = User(id="user-gpu-disk", email="gpu-disk@example.com", name="GPU Disk", role="user", status="active")
        wallet = Wallet(user_id=user.id, credits=100, frozen_credits=10)
        asset = Asset(
            id="asset-gpu-disk",
            user_id=user.id,
            kind="input",
            original_name="input.mp4",
            mime_type="video/mp4",
            storage_key="input.mp4",
            url="https://cdn.example.test/input.mp4",
            size_bytes=1024,
            duration_seconds=70,
            expires_at=now() + timedelta(days=1),
        )
        task = Task(
            id="task-gpu-disk",
            user_id=user.id,
            tool_slug="translate",
            input_asset_id=asset.id,
            status="queued",
            params={"_gpuUnavailableRetries": 2},
            estimated_credits=10,
            frozen_credits=10,
            provider="mock",
            provider_job_id="provider-gpu-disk",
            progress_percent=5,
            progress_stage="远端 GPU 暂不可用，等待自动重试",
        )
        db.add_all([user, wallet, asset, task])
        db.commit()

    session_factory = lambda: Session(engine)
    enqueued: list[str] = []
    monkeypatch.setattr(worker, "SessionLocal", session_factory)
    monkeypatch.setattr(worker.settings, "gpu_unavailable_retry_max", 1)
    monkeypatch.setattr(worker.settings, "gpu_queue_full_retry_max", 0)
    monkeypatch.setattr(worker, "enqueue_provider_job", lambda task_id, delay_seconds=0: enqueued.append(f"{task_id}:{delay_seconds}"))

    worker._requeue_provider_job_for_gpu_unavailable(
        "task-gpu-disk",
        "provider-gpu-disk",
        'GPU API HTTP 503: {"detail":"GPU disk pressure: 363.7 GiB free, requires at least 500.0 GiB; used 84.47%"}',
    )

    with Session(engine) as db:
        task = db.get(Task, "task-gpu-disk")
        wallet = db.get(Wallet, "user-gpu-disk")
        assert task is not None
        assert wallet is not None
        assert task.status == "queued"
        assert task.error_code is None
        assert task.params["_gpuUnavailableRetries"] == 2
        assert task.params["_gpuQueueFullRetries"] == 1
        assert "磁盘空间不足" in task.progress_stage
        assert wallet.frozen_credits == 10
    assert enqueued == ["task-gpu-disk:300"]


def test_gpu_unavailable_requeues_with_delay(monkeypatch) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as db:
        user = User(id="user-gpu-delay", email="gpu-delay@example.com", name="GPU Delay", role="user", status="active")
        wallet = Wallet(user_id=user.id, credits=100, frozen_credits=10)
        asset = Asset(
            id="asset-gpu-delay",
            user_id=user.id,
            kind="input",
            original_name="input.mp4",
            mime_type="video/mp4",
            storage_key="input.mp4",
            url="https://cdn.example.test/input.mp4",
            size_bytes=1024,
            duration_seconds=70,
            expires_at=now() + timedelta(days=1),
        )
        task = Task(
            id="task-gpu-delay",
            user_id=user.id,
            tool_slug="translate",
            input_asset_id=asset.id,
            status="queued",
            params={"_gpuUnavailableRetries": 0},
            estimated_credits=10,
            frozen_credits=10,
            provider="mock",
            provider_job_id="provider-gpu-delay",
            progress_percent=5,
            progress_stage="远端 GPU 暂不可用，等待自动重试",
        )
        db.add_all([user, wallet, asset, task])
        db.commit()

    session_factory = lambda: Session(engine)
    enqueued: list[tuple[str, int]] = []
    monkeypatch.setattr(worker, "SessionLocal", session_factory)
    monkeypatch.setattr(worker.settings, "gpu_unavailable_retry_max", 3)
    monkeypatch.setattr(worker.settings, "gpu_unavailable_retry_delay_seconds", 120)
    monkeypatch.setattr(worker, "enqueue_provider_job", lambda task_id, delay_seconds=0: enqueued.append((task_id, delay_seconds)))

    worker._requeue_provider_job_for_gpu_unavailable(
        "task-gpu-delay",
        "provider-gpu-delay",
        "temporary GPU API unavailable",
    )

    with Session(engine) as db:
        task = db.get(Task, "task-gpu-delay")
        wallet = db.get(Wallet, "user-gpu-delay")
        assert task is not None
        assert wallet is not None
        assert task.status == "queued"
        assert task.error_code is None
        assert task.params["_gpuUnavailableRetries"] == 1
        assert task.progress_stage == "远端 GPU 暂不可用，等待自动重试（1/3）"
        assert wallet.frozen_credits == 10
    assert enqueued == [("task-gpu-delay", 120)]


def test_gpu_queue_full_requeues_without_exhausting_unavailable_retries(monkeypatch) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as db:
        user = User(id="user-gpu-full", email="gpu-full@example.com", name="GPU Full", role="user", status="active")
        wallet = Wallet(user_id=user.id, credits=100, frozen_credits=10)
        asset = Asset(
            id="asset-gpu-full",
            user_id=user.id,
            kind="input",
            original_name="input.mp4",
            mime_type="video/mp4",
            storage_key="input.mp4",
            url="https://cdn.example.test/input.mp4",
            size_bytes=1024,
            duration_seconds=70,
            expires_at=now() + timedelta(days=1),
        )
        task = Task(
            id="task-gpu-full",
            user_id=user.id,
            tool_slug="translate",
            input_asset_id=asset.id,
            status="queued",
            params={"_gpuUnavailableRetries": 3, "_gpuQueueFullRetries": 7},
            estimated_credits=10,
            frozen_credits=10,
            provider="mock",
            provider_job_id="provider-gpu-full",
            progress_percent=5,
            progress_stage="远端 GPU 暂不可用，等待自动重试",
        )
        db.add_all([user, wallet, asset, task])
        db.commit()

    session_factory = lambda: Session(engine)
    enqueued: list[str] = []
    monkeypatch.setattr(worker, "SessionLocal", session_factory)
    monkeypatch.setattr(worker.settings, "gpu_unavailable_retry_max", 1)
    monkeypatch.setattr(worker.settings, "gpu_queue_full_retry_max", 0)
    monkeypatch.setattr(worker.settings, "gpu_queue_full_retry_delay_seconds", 300)
    monkeypatch.setattr(worker, "enqueue_provider_job", lambda task_id, delay_seconds=0: enqueued.append(f"{task_id}:{delay_seconds}"))

    worker._requeue_provider_job_for_gpu_unavailable(
        "task-gpu-full",
        "provider-gpu-full",
        'GPU API HTTP 503: {"detail":"GPU API queue is full"}',
    )

    with Session(engine) as db:
        task = db.get(Task, "task-gpu-full")
        wallet = db.get(Wallet, "user-gpu-full")
        assert task is not None
        assert wallet is not None
        assert task.status == "queued"
        assert task.error_code is None
        assert task.params["_gpuUnavailableRetries"] == 3
        assert task.params["_gpuQueueFullRetries"] == 8
        assert task.progress_stage == "远端 GPU 队列已满，保持队列顺序等待调度（第 8 次）"
        assert wallet.frozen_credits == 10
    assert enqueued == ["task-gpu-full:300"]
