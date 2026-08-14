from datetime import timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app import dispatcher
from app.models import Asset, Base, Task, User, Wallet
from app.services import now


def test_dispatcher_enqueues_stable_queued_tasks_and_skips_cooldown(monkeypatch) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    enqueued: list[str] = []
    monkeypatch.setattr(dispatcher, "SessionLocal", lambda: Session(engine))
    monkeypatch.setattr(dispatcher, "_queued_rq_task_ids", lambda: {"already-enqueued"})
    monkeypatch.setattr(dispatcher, "enqueue_provider_job", enqueued.append)
    monkeypatch.setattr(dispatcher.settings, "gpu_queue_dispatch_cooldown_seconds", 180)

    with Session(engine) as db:
        user = User(id="user-dispatch", email="dispatch@example.com", name="Dispatch", role="user", status="active")
        wallet = Wallet(user_id=user.id, credits=100, frozen_credits=0)
        db.add_all([user, wallet])
        for index, task_id in enumerate(["task-1", "task-cooldown", "task-after-cooldown", "already-enqueued"], start=1):
            asset = Asset(
                id=f"asset-{task_id}",
                user_id=user.id,
                kind="video",
                original_name=f"{task_id}.mp4",
                mime_type="video/mp4",
                storage_key=f"{task_id}.mp4",
                url=f"https://cdn.example.test/{task_id}.mp4",
                size_bytes=10,
                duration_seconds=10,
                expires_at=now() + timedelta(days=1),
            )
            params = {"internalBatchId": "batch-dispatch", "internalBatchName": "dispatch batch", "internalBatchIndex": index}
            if task_id == "task-cooldown":
                params["_gpuQueueFullLastAt"] = int(now().timestamp())
            task = Task(
                id=task_id,
                user_id=user.id,
                tool_slug="subtitle-translate-workflow",
                input_asset_id=asset.id,
                status="queued",
                params=params,
                estimated_credits=0,
                frozen_credits=0,
                charged_credits=0,
                provider="mock",
                provider_job_id=f"provider-{task_id}",
                progress_percent=5,
                progress_stage="等待调度",
            )
            db.add_all([asset, task])
        db.commit()

    result = dispatcher.dispatch_provider_queue_once(limit=3)

    assert result == {"dispatched": 1, "taskIds": ["task-1"]}
    assert enqueued == ["task-1"]


def test_dispatcher_keeps_manual_priority_ahead(monkeypatch) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    enqueued: list[str] = []
    monkeypatch.setattr(dispatcher, "SessionLocal", lambda: Session(engine))
    monkeypatch.setattr(dispatcher, "_queued_rq_task_ids", lambda: set())
    monkeypatch.setattr(dispatcher, "enqueue_provider_job", enqueued.append)

    with Session(engine) as db:
        user = User(id="user-priority", email="priority@example.com", name="Priority", role="user", status="active")
        wallet = Wallet(user_id=user.id, credits=100, frozen_credits=0)
        db.add_all([user, wallet])
        for index, task_id in enumerate(["normal-task", "boosted-task"], start=1):
            asset = Asset(
                id=f"asset-{task_id}",
                user_id=user.id,
                kind="video",
                original_name=f"{task_id}.mp4",
                mime_type="video/mp4",
                storage_key=f"{task_id}.mp4",
                url=f"https://cdn.example.test/{task_id}.mp4",
                size_bytes=10,
                duration_seconds=10,
                expires_at=now() + timedelta(days=1),
            )
            params = {"internalBatchId": "batch-priority", "internalBatchName": "priority batch", "internalBatchIndex": index}
            if task_id == "boosted-task":
                params["_manualPriorityBoostAt"] = int(now().timestamp())
                params["_manualPriorityBoostCount"] = 1
            task = Task(
                id=task_id,
                user_id=user.id,
                tool_slug="subtitle-translate-workflow",
                input_asset_id=asset.id,
                status="queued",
                params=params,
                estimated_credits=0,
                frozen_credits=0,
                charged_credits=0,
                provider="mock",
                provider_job_id=f"provider-{task_id}",
                progress_percent=5,
                progress_stage="等待调度",
            )
            db.add_all([asset, task])
        db.commit()

    result = dispatcher.dispatch_provider_queue_once(limit=2)

    assert result == {"dispatched": 2, "taskIds": ["boosted-task", "normal-task"]}
    assert enqueued == ["boosted-task", "normal-task"]


def test_dispatcher_stops_when_remote_gpu_inflight_limit_is_reached(monkeypatch) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    enqueued: list[str] = []
    monkeypatch.setattr(dispatcher, "SessionLocal", lambda: Session(engine))
    monkeypatch.setattr(dispatcher, "_queued_rq_task_ids", lambda: set())
    monkeypatch.setattr(dispatcher, "enqueue_provider_job", enqueued.append)
    monkeypatch.setattr(dispatcher.settings, "gpu_remote_inflight_limit", 1)

    with Session(engine) as db:
        user = User(id="user-inflight", email="inflight@example.com", name="Inflight", role="user", status="active")
        wallet = Wallet(user_id=user.id, credits=100, frozen_credits=0)
        db.add_all([user, wallet])
        for task_id, status, params in [
            ("remote-processing", "processing", {"remoteGpuJobId": "remote-1"}),
            ("queued-task", "queued", {}),
        ]:
            asset = Asset(
                id=f"asset-{task_id}",
                user_id=user.id,
                kind="video",
                original_name=f"{task_id}.mp4",
                mime_type="video/mp4",
                storage_key=f"{task_id}.mp4",
                url=f"https://cdn.example.test/{task_id}.mp4",
                size_bytes=10,
                duration_seconds=10,
                expires_at=now() + timedelta(days=1),
            )
            task = Task(
                id=task_id,
                user_id=user.id,
                tool_slug="subtitle-translate-workflow",
                input_asset_id=asset.id,
                status=status,
                params=params,
                estimated_credits=0,
                frozen_credits=0,
                charged_credits=0,
                provider="mock",
                provider_job_id=f"provider-{task_id}",
                progress_percent=10 if status == "processing" else 5,
                progress_stage="远端 GPU 已提交，等待处理" if status == "processing" else "等待调度",
            )
            db.add_all([asset, task])
        db.commit()

    result = dispatcher.dispatch_provider_queue_once(limit=2)

    assert result == {"dispatched": 0, "taskIds": []}
    assert enqueued == []
