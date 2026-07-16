from datetime import timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app import admin
from app.models import Asset, Base, Task, User, Wallet
from app.services import now


class FakeJob:
    def __init__(self, task_id: str) -> None:
        self.args = (task_id,)


class FakeScheduledRegistry:
    def __init__(self, task_ids: list[str]) -> None:
        self.task_ids = task_ids

    def get_job_ids(self) -> list[str]:
        return [f"job-{task_id}" for task_id in self.task_ids]


class FakeQueue:
    def __init__(self, task_ids: list[str], scheduled_task_ids: list[str] | None = None) -> None:
        self.task_ids = task_ids
        self.scheduled_job_registry = FakeScheduledRegistry(scheduled_task_ids or [])

    def get_job_ids(self) -> list[str]:
        return [f"job-{task_id}" for task_id in self.task_ids]

    def fetch_job(self, job_id: str) -> FakeJob | None:
        prefix = "job-"
        if not job_id.startswith(prefix):
            return None
        return FakeJob(job_id[len(prefix):])


def test_queued_gpu_jobs_uses_queue_order_and_precise_names(monkeypatch) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    monkeypatch.setattr(admin, "task_queue", lambda: FakeQueue(["task-2"], ["task-1"]))

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

        queued_jobs = admin._queued_gpu_jobs(db)

    assert [job["taskId"] for job in queued_jobs] == ["task-2", "task-1"]
    assert queued_jobs[0]["position"] == 1
    assert queued_jobs[0]["queueState"] == "queued"
    assert queued_jobs[1]["position"] == 2
    assert queued_jobs[1]["queueState"] == "scheduled"
    assert queued_jobs[0]["displayName"] == "143.和离后，我停了他的续命香（59集）"
    assert queued_jobs[0]["displaySubtitle"] == "第 2 集 · episode-2.mp4"
