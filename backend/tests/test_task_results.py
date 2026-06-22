from datetime import timedelta
import json
import zipfile

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import app.services as services
from app.models import Asset, Base, Task, User, Wallet
from app.services import create_internal_batch_zip, get_task_result_access, get_task_result_url, internal_batch_status, now, retry_internal_batch_tasks


class FakeLocalStorage:
    is_remote = False

    def __init__(self, root):
        self.root = root

    def ensure_local(self, storage_key: str):
        path = self.root / storage_key
        if not path.exists():
            raise FileNotFoundError(storage_key)
        return path

    def presign_download(self, storage_key: str, filename: str | None = None) -> str:
        raise AssertionError("local result should be served through the API")


def test_local_output_asset_result_is_served_through_api(tmp_path, monkeypatch) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    monkeypatch.setattr(services, "storage", FakeLocalStorage(tmp_path))

    result_path = tmp_path / "result.mp4"
    result_path.write_bytes(b"fake-video")

    with Session(engine) as db:
        user = User(id="user-result", email="result@example.com", name="Result User", role="user", status="active")
        wallet = Wallet(user_id=user.id, credits=100, frozen_credits=0)
        input_asset = Asset(
            id="asset-input",
            user_id=user.id,
            kind="video",
            original_name="clip.mp4",
            mime_type="video/mp4",
            storage_key="input.mp4",
            url="/uploads/input.mp4",
            size_bytes=10,
            duration_seconds=10,
            expires_at=now() + timedelta(days=1),
        )
        output_asset = Asset(
            id="asset-output",
            user_id=user.id,
            kind="result",
            original_name="result.mp4",
            mime_type="video/mp4",
            storage_key="result.mp4",
            url="/uploads/result.mp4",
            size_bytes=10,
            duration_seconds=0,
            expires_at=now() + timedelta(days=1),
        )
        task = Task(
            id="task-result",
            user_id=user.id,
            tool_slug="remove-subtitle",
            input_asset_id=input_asset.id,
            output_asset_id=output_asset.id,
            status="succeeded",
            params={},
            estimated_credits=1,
            frozen_credits=0,
            charged_credits=1,
            provider="mock",
            provider_job_id="provider-result",
            output_url="/uploads/result.mp4",
            progress_percent=100,
            progress_stage="处理完成",
        )
        db.add_all([user, wallet, input_asset, output_asset, task])
        db.commit()

        assert get_task_result_url(db, user.id, task.id).startswith("/api/tasks/task-result/result/")
        access = get_task_result_access(db, user.id, task.id)

    assert access["mode"] == "file"
    assert access["path"] == result_path
    assert access["mime_type"] == "video/mp4"


def test_internal_batch_zip_includes_succeeded_tasks_when_batch_is_partial(tmp_path, monkeypatch) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    monkeypatch.setattr(services.settings, "upload_dir", str(tmp_path))

    batch_id = "batch-partial"
    batch_name = "partial batch"

    with Session(engine) as db:
        user = User(id="user-batch", email="batch@example.com", name="Batch User", role="user", status="active")
        wallet = Wallet(user_id=user.id, credits=100, frozen_credits=0)
        db.add_all([user, wallet])

        tasks = []
        for index, status in enumerate(["succeeded", "failed", "queued"], start=1):
            asset = Asset(
                id=f"asset-{index}",
                user_id=user.id,
                kind="video",
                original_name=f"clip-{index}.mp4",
                mime_type="video/mp4",
                storage_key=f"input-{index}.mp4",
                url=f"/uploads/input-{index}.mp4",
                size_bytes=10,
                duration_seconds=10,
                expires_at=now() + timedelta(days=1),
            )
            task = Task(
                id=f"task-{index}",
                user_id=user.id,
                tool_slug="subtitle-translate-workflow",
                input_asset_id=asset.id,
                output_asset_id=None,
                status=status,
                params={"internalBatchId": batch_id, "internalBatchName": batch_name},
                estimated_credits=1,
                frozen_credits=0,
                charged_credits=1 if status == "succeeded" else 0,
                provider="mock",
                provider_job_id=f"provider-{index}",
                error_code="PROVIDER_FAILED" if status == "failed" else None,
                output_url="",
                progress_percent=100 if status == "succeeded" else 0,
                progress_stage="处理完成" if status == "succeeded" else "",
            )
            db.add_all([asset, task])
            tasks.append(task)
        db.commit()

        succeeded_task = tasks[0]
        result_path = tmp_path / services.task_result_output_key(succeeded_task)
        result_path.write_bytes(b"succeeded-video")

        status = internal_batch_status(db, user.id, batch_id)
        archive = create_internal_batch_zip(db, user.id, batch_id)

    assert status["downloadReady"] is True
    assert status["succeeded"] == 1
    assert status["total"] == 3

    with zipfile.ZipFile(archive["path"]) as zip_file:
        names = zip_file.namelist()
        summary = json.loads(zip_file.read("_batch-summary.json"))
        video_names = [name for name in names if name.endswith(".mp4")]

    assert len(video_names) == 1
    assert video_names[0].startswith("001-clip-1-task-1")
    assert summary["succeeded"] == 1
    assert summary["failed"] == 1
    assert summary["processing"] == 1
    assert summary["includedTaskIds"] == ["task-1"]
    assert {task["id"] for task in summary["skippedTasks"]} == {"task-2", "task-3"}


def test_internal_batch_retry_resets_failed_and_cancelled_tasks(tmp_path, monkeypatch) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    monkeypatch.setattr(services.settings, "upload_dir", str(tmp_path))
    enqueued: list[str] = []
    monkeypatch.setattr(services, "enqueue_provider_job", enqueued.append)

    batch_id = "batch-retry"
    old_provider_ids: dict[str, str] = {}

    with Session(engine) as db:
        user = User(id="user-retry", email="retry@example.com", name="Retry User", role="user", status="active")
        wallet = Wallet(user_id=user.id, credits=100, frozen_credits=0)
        db.add_all([user, wallet])

        for index, status in enumerate(["succeeded", "failed", "cancelled"], start=1):
            asset = Asset(
                id=f"retry-asset-{index}",
                user_id=user.id,
                kind="video",
                original_name=f"retry-{index}.mp4",
                mime_type="video/mp4",
                storage_key=f"retry-input-{index}.mp4",
                url=f"/uploads/retry-input-{index}.mp4",
                size_bytes=10,
                duration_seconds=10,
                expires_at=now() + timedelta(days=1),
            )
            task = Task(
                id=f"retry-task-{index}",
                user_id=user.id,
                tool_slug="subtitle-translate-workflow",
                input_asset_id=asset.id,
                output_asset_id=None,
                status=status,
                params={"internalBatchId": batch_id, "internalBatchName": "retry batch"},
                estimated_credits=0,
                frozen_credits=0,
                charged_credits=0,
                provider="mock",
                provider_job_id=f"old-provider-{index}",
                error_code="USER_CANCELLED" if status == "cancelled" else "PROVIDER_FAILED" if status == "failed" else None,
                output_url="",
                progress_percent=100 if status == "succeeded" else 0,
                progress_stage="",
                completed_at=now() if status != "succeeded" else None,
            )
            db.add_all([asset, task])
            old_provider_ids[task.id] = task.provider_job_id
        db.commit()

        cancel_marker = tmp_path / "retry-task-3.cancel"
        cancel_marker.write_text("cancelled", encoding="utf-8")

        result = retry_internal_batch_tasks(db, user.id, batch_id)
        status = internal_batch_status(db, user.id, batch_id)
        retried_tasks = {task_id: db.get(Task, task_id) for task_id in result["taskIds"]}
        wallet_after = db.get(Wallet, user.id)

    assert result["retried"] == 2
    assert set(result["taskIds"]) == {"retry-task-2", "retry-task-3"}
    assert enqueued == ["retry-task-2", "retry-task-3"]
    assert status["total"] == 3
    assert status["succeeded"] == 1
    assert status["failed"] == 0
    assert status["cancelled"] == 0
    assert status["processing"] == 2
    assert wallet_after.frozen_credits == 0
    assert not cancel_marker.exists()
    for task_id, task in retried_tasks.items():
        assert task.status == "queued"
        assert task.provider_job_id != old_provider_ids[task_id]
        assert task.error_code is None
        assert task.progress_stage == "等待 worker 领取任务"
