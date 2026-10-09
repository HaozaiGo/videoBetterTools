from datetime import timedelta
from types import SimpleNamespace

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app import dispatcher
from app.models import Asset, Base, Task, User, Wallet
from app.services import now


def test_dispatcher_enqueues_ready_tasks_and_skips_cooling_down_tasks(monkeypatch) -> None:
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

    assert result == {"dispatched": 2, "taskIds": ["task-1", "task-after-cooldown"]}
    assert enqueued == ["task-1", "task-after-cooldown"]


def test_dispatcher_moves_to_next_batch_when_active_batch_is_cooling(monkeypatch) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    enqueued: list[str] = []
    stored: list[str] = []
    monkeypatch.setattr(dispatcher, "SessionLocal", lambda: Session(engine))
    monkeypatch.setattr(dispatcher, "_queued_rq_task_ids", lambda: set())
    monkeypatch.setattr(dispatcher, "enqueue_provider_job", enqueued.append)
    monkeypatch.setattr(dispatcher, "_select_active_batch_key", lambda _db, _queued: "internal:user-cooling-active:batch-a")
    monkeypatch.setattr(dispatcher, "_clear_active_batch_key", lambda: stored.append(""))
    monkeypatch.setattr(dispatcher, "_store_active_batch_key", stored.append)
    monkeypatch.setattr(dispatcher.settings, "gpu_queue_dispatch_cooldown_seconds", 180)
    monkeypatch.setattr(dispatcher.settings, "gpu_remote_inflight_limit", 0)

    with Session(engine) as db:
        user = User(id="user-cooling-active", email="cooling-active@example.com", name="Cooling Active", role="user", status="active")
        wallet = Wallet(user_id=user.id, credits=100, frozen_credits=0)
        db.add_all([user, wallet])
        for index, (task_id, batch_id, last_full_at) in enumerate(
            [
                ("batch-a-cooling", "batch-a", int(now().timestamp())),
                ("batch-b-ready", "batch-b", 0),
            ],
            start=1,
        ):
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
            params = {"internalBatchId": batch_id, "internalBatchName": batch_id, "internalBatchIndex": 1}
            if last_full_at:
                params["_gpuQueueFullLastAt"] = last_full_at
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
                created_at=now() + timedelta(seconds=index),
            )
            db.add_all([asset, task])
        db.commit()

    result = dispatcher.dispatch_provider_queue_once(limit=2)

    assert result == {"dispatched": 1, "taskIds": ["batch-b-ready"]}
    assert enqueued == ["batch-b-ready"]
    assert stored == ["", "internal:user-cooling-active:batch-b"]


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


def test_dispatcher_drains_same_internal_batch_before_next_batch(monkeypatch) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    enqueued: list[str] = []
    monkeypatch.setattr(dispatcher, "SessionLocal", lambda: Session(engine))
    monkeypatch.setattr(dispatcher, "_queued_rq_task_ids", lambda: set())
    monkeypatch.setattr(dispatcher, "enqueue_provider_job", enqueued.append)

    with Session(engine) as db:
        user = User(id="user-batch-fifo", email="batch-fifo@example.com", name="Batch FIFO", role="user", status="active")
        wallet = Wallet(user_id=user.id, credits=100, frozen_credits=0)
        db.add_all([user, wallet])
        for index, (task_id, batch_id, batch_name, episode) in enumerate(
            [
                ("batch-a-1", "batch-a", "A batch", 1),
                ("batch-b-1", "batch-b", "B batch", 1),
                ("batch-a-2", "batch-a", "A batch", 2),
                ("batch-b-2", "batch-b", "B batch", 2),
            ],
            start=1,
        ):
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
                status="queued",
                params={"internalBatchId": batch_id, "internalBatchName": batch_name, "internalBatchIndex": episode},
                estimated_credits=0,
                frozen_credits=0,
                charged_credits=0,
                provider="mock",
                provider_job_id=f"provider-{task_id}",
                progress_percent=5,
                progress_stage="等待调度",
                created_at=now() + timedelta(seconds=index),
            )
            db.add_all([asset, task])
        db.commit()

    result = dispatcher.dispatch_provider_queue_once(limit=4)

    assert result == {"dispatched": 2, "taskIds": ["batch-a-1", "batch-a-2"]}
    assert enqueued == ["batch-a-1", "batch-a-2"]

    with Session(engine) as db:
        for task in db.query(Task).filter(Task.id.in_(["batch-a-1", "batch-a-2"])):
            task.status = "processing"
        db.commit()

    result = dispatcher.dispatch_provider_queue_once(limit=4)

    assert result == {"dispatched": 2, "taskIds": ["batch-b-1", "batch-b-2"]}
    assert enqueued == ["batch-a-1", "batch-a-2", "batch-b-1", "batch-b-2"]


def test_dispatcher_starts_next_batch_after_active_batch_enters_processing(monkeypatch) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    enqueued: list[str] = []
    monkeypatch.setattr(dispatcher, "SessionLocal", lambda: Session(engine))
    monkeypatch.setattr(dispatcher, "_queued_rq_task_ids", lambda: set())
    monkeypatch.setattr(dispatcher, "enqueue_provider_job", enqueued.append)
    monkeypatch.setattr(dispatcher.settings, "gpu_remote_inflight_limit", 0)

    with Session(engine) as db:
        user = User(id="user-active-lock", email="active-lock@example.com", name="Active Lock", role="user", status="active")
        wallet = Wallet(user_id=user.id, credits=100, frozen_credits=0)
        db.add_all([user, wallet])
        for index, (task_id, batch_id, status) in enumerate(
            [
                ("batch-a-processing", "batch-a", "processing"),
                ("batch-b-queued", "batch-b", "queued"),
            ],
            start=1,
        ):
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
                params={"internalBatchId": batch_id, "internalBatchName": batch_id, "internalBatchIndex": index},
                estimated_credits=0,
                frozen_credits=0,
                charged_credits=0,
                provider="mock",
                provider_job_id=f"provider-{task_id}",
                progress_percent=10 if status == "processing" else 5,
                progress_stage="开始去字幕" if status == "processing" else "等待调度",
                created_at=now() + timedelta(seconds=index),
            )
            db.add_all([asset, task])
        db.commit()

    result = dispatcher.dispatch_provider_queue_once(limit=2)

    assert result == {"dispatched": 1, "taskIds": ["batch-b-queued"]}
    assert enqueued == ["batch-b-queued"]


def test_dispatcher_dispatches_active_batch_outside_created_at_scan_window(monkeypatch) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    enqueued: list[str] = []
    monkeypatch.setattr(dispatcher, "SessionLocal", lambda: Session(engine))
    monkeypatch.setattr(dispatcher, "_queued_rq_task_ids", lambda: set())
    monkeypatch.setattr(dispatcher, "enqueue_provider_job", enqueued.append)
    monkeypatch.setattr(dispatcher.settings, "gpu_remote_inflight_limit", 0)

    with Session(engine) as db:
        user = User(id="user-scan-window", email="scan-window@example.com", name="Scan Window", role="user", status="active")
        wallet = Wallet(user_id=user.id, credits=100, frozen_credits=0)
        db.add_all([user, wallet])
        base_time = now()
        for index in range(1000):
            task_id = f"old-task-{index}"
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
                status="queued",
                params={"internalBatchId": "old-batch", "internalBatchName": "Old batch", "internalBatchIndex": index + 1},
                estimated_credits=0,
                frozen_credits=0,
                charged_credits=0,
                provider="mock",
                provider_job_id=f"provider-{task_id}",
                progress_percent=5,
                progress_stage="等待调度",
                created_at=base_time + timedelta(seconds=index),
            )
            db.add_all([asset, task])

        priority_asset = Asset(
            id="asset-priority-task",
            user_id=user.id,
            kind="video",
            original_name="priority.mp4",
            mime_type="video/mp4",
            storage_key="priority.mp4",
            url="https://cdn.example.test/priority.mp4",
            size_bytes=10,
            duration_seconds=10,
            expires_at=now() + timedelta(days=1),
        )
        priority_task = Task(
            id="priority-task",
            user_id=user.id,
            tool_slug="subtitle-translate-workflow",
            input_asset_id=priority_asset.id,
            status="queued",
            params={
                "internalBatchId": "priority-batch",
                "internalBatchName": "Priority batch",
                "internalBatchIndex": 1,
                "_zipPriorityRank": 1,
            },
            estimated_credits=0,
            frozen_credits=0,
            charged_credits=0,
            provider="mock",
            provider_job_id="provider-priority-task",
            progress_percent=5,
            progress_stage="等待调度",
            created_at=base_time + timedelta(seconds=2000),
        )
        db.add_all([priority_asset, priority_task])
        db.commit()

    result = dispatcher.dispatch_provider_queue_once(limit=1)

    assert result == {"dispatched": 1, "taskIds": ["priority-task"]}
    assert enqueued == ["priority-task"]


def test_dispatcher_reorders_existing_rq_batch_by_episode(monkeypatch) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)

    class FakeConnection:
        def __init__(self, queue) -> None:
            self.queue = queue

        def eval(self, _script, _key_count, _key, *argv):
            selected = set(argv)
            self.queue.job_ids = list(argv) + [job_id for job_id in self.queue.job_ids if job_id not in selected]
            return len(self.queue.job_ids)

    class FakeQueue:
        def __init__(self) -> None:
            self.key = "rq:queue:model-plaza-tasks"
            self.job_ids = ["job-39", "job-57", "job-54"]
            self.connection = FakeConnection(self)

        def fetch_job(self, job_id):
            return SimpleNamespace(args=[job_id.replace("job-", "task-")])

    fake_queue = FakeQueue()
    monkeypatch.setattr(dispatcher, "task_queue", lambda: fake_queue)
    monkeypatch.setattr(dispatcher.settings, "gpu_queue_batch_fifo_enabled", True)
    monkeypatch.setattr(dispatcher.settings, "gpu_queue_batch_fifo_reorder_scan_size", 2000)

    with Session(engine) as db:
        user = User(id="user-rq-fifo", email="rq-fifo@example.com", name="RQ FIFO", role="user", status="active")
        wallet = Wallet(user_id=user.id, credits=100, frozen_credits=0)
        db.add_all([user, wallet])
        for episode in [39, 57, 54]:
            task_id = f"task-{episode}"
            asset = Asset(
                id=f"asset-{task_id}",
                user_id=user.id,
                kind="video",
                original_name=f"{episode}.mp4",
                mime_type="video/mp4",
                storage_key=f"{episode}.mp4",
                url=f"https://cdn.example.test/{episode}.mp4",
                size_bytes=10,
                duration_seconds=10,
                expires_at=now() + timedelta(days=1),
            )
            task = Task(
                id=task_id,
                user_id=user.id,
                tool_slug="subtitle-translate-workflow",
                input_asset_id=asset.id,
                status="queued",
                params={"internalBatchId": "batch-rq", "internalBatchName": "RQ batch", "internalBatchIndex": episode},
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

        reordered = dispatcher._reorder_queued_rq_jobs_batch_fifo(db)

    assert reordered == 3
    assert fake_queue.job_ids == ["job-39", "job-54", "job-57"]


def test_dispatcher_active_batch_prunes_other_batches_from_rq(monkeypatch) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)

    class FakeConnection:
        def __init__(self, queue) -> None:
            self.queue = queue

        def eval(self, _script, _key_count, _key, *argv):
            args = list(argv)
            delimiter = args.index("__SCANNED_JOB_IDS__")
            desired = args[:delimiter]
            scanned = set(args[delimiter + 1:])
            self.queue.job_ids = desired + [job_id for job_id in self.queue.job_ids if job_id not in scanned]
            return len(self.queue.job_ids)

    class FakeQueue:
        def __init__(self) -> None:
            self.key = "rq:queue:model-plaza-tasks"
            self.job_ids = ["job-a-2", "job-b-1", "job-a-1"]
            self.connection = FakeConnection(self)

        def fetch_job(self, job_id):
            return SimpleNamespace(args=[job_id.replace("job-", "task-")])

    fake_queue = FakeQueue()
    monkeypatch.setattr(dispatcher, "task_queue", lambda: fake_queue)
    monkeypatch.setattr(dispatcher.settings, "gpu_queue_batch_fifo_enabled", True)
    monkeypatch.setattr(dispatcher.settings, "gpu_queue_batch_fifo_reorder_scan_size", 10000)

    with Session(engine) as db:
        user = User(id="user-rq-lock", email="rq-lock@example.com", name="RQ Lock", role="user", status="active")
        wallet = Wallet(user_id=user.id, credits=100, frozen_credits=0)
        db.add_all([user, wallet])
        rows = [
            ("task-a-2", "batch-a", 2),
            ("task-b-1", "batch-b", 1),
            ("task-a-1", "batch-a", 1),
        ]
        for task_id, batch_id, episode in rows:
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
                status="queued",
                params={"internalBatchId": batch_id, "internalBatchName": batch_id, "internalBatchIndex": episode},
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

        reordered = dispatcher._reorder_queued_rq_jobs_batch_fifo(db, "internal:user-rq-lock:batch-a")

    assert reordered == 2
    assert fake_queue.job_ids == ["job-a-1", "job-a-2"]


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


def test_dispatcher_ignores_stale_remote_gpu_inflight(monkeypatch) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    enqueued: list[str] = []
    now_seconds = 1_800_000
    monkeypatch.setattr(dispatcher, "SessionLocal", lambda: Session(engine))
    monkeypatch.setattr(dispatcher, "_queued_rq_task_ids", lambda: set())
    monkeypatch.setattr(dispatcher, "enqueue_provider_job", enqueued.append)
    monkeypatch.setattr(dispatcher.settings, "gpu_remote_inflight_limit", 1)
    monkeypatch.setattr(dispatcher.settings, "gpu_remote_inflight_stale_seconds", 1800)
    monkeypatch.setattr(dispatcher.time, "time", lambda: now_seconds)

    with Session(engine) as db:
        user = User(id="user-stale-inflight", email="stale-inflight@example.com", name="Stale", role="user", status="active")
        wallet = Wallet(user_id=user.id, credits=100, frozen_credits=0)
        db.add_all([user, wallet])
        for task_id, status, params in [
            ("stale-remote-processing", "processing", {"remoteGpuJobId": "remote-old", "remoteGpuSubmittedAt": now_seconds - 7200}),
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

    assert result == {"dispatched": 1, "taskIds": ["queued-task"]}
    assert enqueued == ["queued-task"]
