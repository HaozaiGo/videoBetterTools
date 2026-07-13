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
        assert task.progress_stage == "远端 GPU 队列已满，等待空位自动重试（第 8 次）"
        assert wallet.frozen_credits == 10
    assert enqueued == ["task-gpu-full:300"]
