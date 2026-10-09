from datetime import timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app import admin
from app.models import Asset, Base, Task, User, Wallet
from app.services import now


class FakeRqJob:
    def __init__(self, task_id: str) -> None:
        self.args = (task_id,)


class FakeTaskQueue:
    job_ids = ["job-task-2", "job-task-1"]

    def fetch_job(self, job_id: str) -> FakeRqJob | None:
        mapping = {
            "job-task-2": FakeRqJob("task-2"),
            "job-task-1": FakeRqJob("task-1"),
        }
        return mapping.get(job_id)


class EmptyFakeTaskQueue:
    job_ids: list[str] = []

    def fetch_job(self, _job_id: str) -> None:
        return None


def test_queued_gpu_jobs_uses_queue_order_and_precise_names(monkeypatch) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as db:
        user = User(id="user-gpu-queue", email="gpu-queue@example.com", name="GPU Queue", role="user", status="active")
        wallet = Wallet(user_id=user.id, credits=100, frozen_credits=0)
        db.add_all([user, wallet])
        for index in (1, 2):
            asset = Asset(
                id=f"asset-{index}",
                user_id=user.id,
                kind="video",
                original_name=f"episode-{index}.mp4",
                mime_type="video/mp4",
                storage_key=f"episode-{index}.mp4",
                url=f"https://cdn.example.test/episode-{index}.mp4",
                size_bytes=10,
                duration_seconds=10,
                expires_at=now() + timedelta(days=1),
            )
            task = Task(
                id=f"task-{index}",
                user_id=user.id,
                tool_slug="subtitle-translate-workflow",
                input_asset_id=asset.id,
                status="queued",
                params={"internalBatchId": "batch-queue", "internalBatchName": "143.和离后，我停了他的续命香（59集）", "internalBatchIndex": index},
                estimated_credits=0,
                frozen_credits=0,
                charged_credits=0,
                provider="mock",
                provider_job_id=f"provider-{index}",
                progress_percent=5,
                progress_stage="等待 worker 领取任务",
            )
            db.add_all([asset, task])
        db.commit()

        monkeypatch.setattr(admin, "task_queue", lambda: FakeTaskQueue())
        queued_jobs = admin._queued_gpu_jobs(db)

    assert [job["taskId"] for job in queued_jobs] == ["task-2", "task-1"]
    assert queued_jobs[0]["position"] == 1
    assert queued_jobs[0]["queueState"] == "queued"
    assert queued_jobs[1]["position"] == 2
    assert queued_jobs[1]["queueState"] == "queued"
    assert queued_jobs[0]["displayName"] == "143.和离后，我停了他的续命香（59集）"
    assert queued_jobs[0]["displaySubtitle"] == "第 2 集 · episode-2.mp4"
    assert queued_jobs[0]["id"] == "job-task-2"
    assert queued_jobs[0]["providerJobId"] == "provider-2"


def test_queued_gpu_jobs_falls_back_to_planned_dispatch_order(monkeypatch) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as db:
        user = User(id="user-planned-queue", email="planned-queue@example.com", name="Planned Queue", role="user", status="active")
        wallet = Wallet(user_id=user.id, credits=100, frozen_credits=0)
        db.add_all([user, wallet])
        for index in (7, 2, 5):
            asset = Asset(
                id=f"planned-asset-{index}",
                user_id=user.id,
                kind="video",
                original_name=f"episode-{index}.mp4",
                mime_type="video/mp4",
                storage_key=f"episode-{index}.mp4",
                url=f"https://cdn.example.test/episode-{index}.mp4",
                size_bytes=10,
                duration_seconds=10,
                expires_at=now() + timedelta(days=1),
            )
            task = Task(
                id=f"planned-task-{index}",
                user_id=user.id,
                tool_slug="subtitle-translate-workflow",
                input_asset_id=asset.id,
                status="queued",
                params={"internalBatchId": "batch-planned", "internalBatchName": "等待展示批次", "internalBatchIndex": index},
                estimated_credits=0,
                frozen_credits=0,
                charged_credits=0,
                provider="mock",
                provider_job_id=f"planned-provider-{index}",
                progress_percent=5,
                progress_stage="远端 GPU 队列已满，保持队列顺序等待调度",
            )
            db.add_all([asset, task])
        db.commit()

        monkeypatch.setattr(admin, "task_queue", lambda: EmptyFakeTaskQueue())
        queued_jobs = admin._queued_gpu_jobs(db, limit=3)

    assert [job["taskId"] for job in queued_jobs] == ["planned-task-2", "planned-task-5", "planned-task-7"]
    assert [job["queueState"] for job in queued_jobs] == ["waiting", "waiting", "waiting"]
    assert [job["position"] for job in queued_jobs] == [1, 2, 3]
    assert queued_jobs[0]["displayName"] == "等待展示批次"
    assert queued_jobs[0]["displaySubtitle"] == "第 2 集 · episode-2.mp4"
