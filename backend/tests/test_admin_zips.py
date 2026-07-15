from datetime import timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import app.admin as admin
import app.services as services
from app.models import Asset, Base, Task, User, Wallet
from app.services import create_internal_batch_zip, now


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
        raise AssertionError("local storage should not presign downloads")


def test_admin_internal_batches_groups_batch_progress() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as db:
        user = User(id="batch-admin-user", email="batch-admin@example.com", name="Batch Admin User", role="user", status="active")
        wallet = Wallet(user_id=user.id, credits=100, frozen_credits=0)
        db.add_all([user, wallet])

        for index, status in enumerate(["succeeded", "succeeded", "failed"], start=1):
            asset = Asset(
                id=f"batch-admin-asset-{index}",
                user_id=user.id,
                kind="video",
                original_name=f"episode-{index}.mp4",
                mime_type="video/mp4",
                storage_key=f"episode-{index}.mp4",
                url=f"/uploads/episode-{index}.mp4",
                size_bytes=10,
                duration_seconds=10,
                expires_at=now() + timedelta(days=1),
            )
            task = Task(
                id=f"batch-admin-task-{index}",
                user_id=user.id,
                tool_slug="subtitle-translate-workflow",
                input_asset_id=asset.id,
                status=status,
                params={"internalBatchId": "batch-admin", "internalBatchName": "101.批次聚合测试（4集）"},
                estimated_credits=1,
                frozen_credits=0,
                charged_credits=1 if status == "succeeded" else 0,
                provider="mock",
                provider_job_id=f"batch-admin-provider-{index}",
                progress_percent=100 if status != "queued" else 5,
                progress_stage="done" if status == "succeeded" else "failed",
                completed_at=now(),
            )
            db.add_all([asset, task])
        db.commit()

        payload = admin.admin_internal_batches(db)
        processing_payload = admin.admin_internal_batches(db, status="processing")

    assert payload["tabs"] == {"all": 1, "processing": 1, "succeeded": 0, "failed": 0}
    item = payload["items"][0]
    assert item["batchName"] == "101.批次聚合测试（4集）"
    assert item["total"] == 4
    assert item["created"] == 3
    assert item["succeeded"] == 2
    assert item["failed"] == 1
    assert item["missing"] == 1
    assert item["processing"] == 1
    assert item["status"] == "processing"
    assert processing_payload["page"]["total"] == 1


def test_admin_internal_batch_zips_lists_ready_local_zip(tmp_path, monkeypatch) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    monkeypatch.setattr(services.settings, "upload_dir", str(tmp_path / "uploads"))
    monkeypatch.setattr(services, "storage", FakeLocalStorage(tmp_path))
    monkeypatch.setattr(admin.settings, "upload_dir", str(tmp_path / "uploads"))

    result_path = tmp_path / "result.mp4"
    result_path.write_bytes(b"fake-video")

    with Session(engine) as db:
        user = User(id="zip-user", email="zip@example.com", name="Zip User", role="user", status="active")
        wallet = Wallet(user_id=user.id, credits=100, frozen_credits=0)
        input_asset = Asset(
            id="zip-input",
            user_id=user.id,
            kind="video",
            original_name="input.mp4",
            mime_type="video/mp4",
            storage_key="input.mp4",
            url="/uploads/input.mp4",
            size_bytes=10,
            duration_seconds=10,
            expires_at=now() + timedelta(days=1),
        )
        output_asset = Asset(
            id="zip-output",
            user_id=user.id,
            kind="result",
            original_name="result.mp4",
            mime_type="video/mp4",
            storage_key="result.mp4",
            url="/uploads/result.mp4",
            size_bytes=result_path.stat().st_size,
            duration_seconds=0,
            expires_at=now() + timedelta(days=1),
        )
        task = Task(
            id="zip-task",
            user_id=user.id,
            tool_slug="subtitle-translate-workflow",
            input_asset_id=input_asset.id,
            output_asset_id=output_asset.id,
            status="succeeded",
            params={"internalBatchId": "zip-batch", "internalBatchName": "后台 ZIP 测试"},
            estimated_credits=1,
            frozen_credits=0,
            charged_credits=1,
            provider="mock",
            provider_job_id="zip-provider",
            output_url="/uploads/result.mp4",
            progress_percent=100,
            progress_stage="done",
            completed_at=now(),
        )
        db.add_all([user, wallet, input_asset, output_asset, task])
        db.commit()

        create_internal_batch_zip(db, user.id, "zip-batch")
        payload = admin.admin_internal_batch_zips(db)
        search_payload = admin.admin_internal_batch_zips(db, name="后台 ZIP")
        empty_search_payload = admin.admin_internal_batch_zips(db, name="不存在的批次")

    assert payload["page"]["total"] == 1
    item = payload["items"][0]
    assert item["batchId"] == "zip-batch"
    assert item["batchName"] == "后台 ZIP 测试"
    assert item["source"] == "local"
    assert item["sizeBytes"] > 0
    assert item["downloadUrl"] == "/api/admin/internal-batch-zips/zip-batch/download?userId=zip-user&part=1"
    assert search_payload["page"]["total"] == 1
    assert empty_search_payload["page"]["total"] == 0


def test_admin_regenerate_internal_batch_zip_deletes_old_zip_and_prioritizes_queue(tmp_path, monkeypatch) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    monkeypatch.setattr(services.settings, "upload_dir", str(tmp_path / "uploads"))
    monkeypatch.setattr(services, "storage", FakeLocalStorage(tmp_path))
    monkeypatch.setattr(admin.settings, "upload_dir", str(tmp_path / "uploads"))
    enqueued: list[tuple[str, str, bool]] = []
    monkeypatch.setattr(admin, "enqueue_internal_batch_zip", lambda user_id, batch_id, at_front=False: enqueued.append((user_id, batch_id, at_front)))

    result_path = tmp_path / "episode-1.mp4"
    result_path.write_bytes(b"episode-one")

    with Session(engine) as db:
        user = User(id="zip-user", email="zip@example.com", name="Zip User", role="user", status="active")
        wallet = Wallet(user_id=user.id, credits=100, frozen_credits=0)
        input_asset = Asset(
            id="zip-input",
            user_id=user.id,
            kind="video",
            original_name="episode-1.mp4",
            mime_type="video/mp4",
            storage_key="episode-1-input.mp4",
            url="/uploads/episode-1-input.mp4",
            size_bytes=10,
            duration_seconds=10,
            expires_at=now() + timedelta(days=1),
        )
        output_asset = Asset(
            id="zip-output",
            user_id=user.id,
            kind="result",
            original_name="episode-1.mp4",
            mime_type="video/mp4",
            storage_key="episode-1.mp4",
            url="/uploads/episode-1.mp4",
            size_bytes=result_path.stat().st_size,
            duration_seconds=0,
            expires_at=now() + timedelta(days=1),
        )
        task = Task(
            id="zip-task",
            user_id=user.id,
            tool_slug="subtitle-translate-workflow",
            input_asset_id=input_asset.id,
            output_asset_id=output_asset.id,
            status="succeeded",
            params={"internalBatchId": "zip-batch", "internalBatchName": "后台 ZIP 测试", "internalBatchTotal": 1},
            estimated_credits=1,
            frozen_credits=0,
            charged_credits=1,
            provider="mock",
            provider_job_id="zip-provider",
            output_url="/uploads/result.mp4",
            progress_percent=100,
            progress_stage="done",
            completed_at=now(),
        )
        db.add_all([user, wallet, input_asset, output_asset, task])
        db.commit()

        archive = create_internal_batch_zip(db, user.id, "zip-batch")
        zip_path = archive["path"]
        assert zip_path.exists()

        payload = admin.admin_regenerate_internal_batch_zip(db, user.id, "zip-batch")

    assert payload == {"queued": True, "deleted": 1, "missing": 0, "failed": [], "partCount": 1}
    assert not zip_path.exists()
    assert enqueued == [("zip-user", "zip-batch", True)]


def test_admin_internal_batch_zips_reports_skipped_tasks_and_deletes_zip(tmp_path, monkeypatch) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    monkeypatch.setattr(services.settings, "upload_dir", str(tmp_path / "uploads"))
    monkeypatch.setattr(services, "storage", FakeLocalStorage(tmp_path))
    monkeypatch.setattr(admin.settings, "upload_dir", str(tmp_path / "uploads"))

    result_path = tmp_path / "episode-1.mp4"
    result_path.write_bytes(b"episode-one")

    with Session(engine) as db:
        user = User(id="zip-user", email="zip@example.com", name="Zip User", role="user", status="active")
        wallet = Wallet(user_id=user.id, credits=100, frozen_credits=0)
        input_one = Asset(
            id="zip-input-1",
            user_id=user.id,
            kind="video",
            original_name="episode-1.mp4",
            mime_type="video/mp4",
            storage_key="episode-1-input.mp4",
            url="/uploads/episode-1-input.mp4",
            size_bytes=10,
            duration_seconds=10,
            expires_at=now() + timedelta(days=1),
        )
        input_two = Asset(
            id="zip-input-2",
            user_id=user.id,
            kind="video",
            original_name="episode-2.mp4",
            mime_type="video/mp4",
            storage_key="episode-2-input.mp4",
            url="/uploads/episode-2-input.mp4",
            size_bytes=10,
            duration_seconds=10,
            expires_at=now() + timedelta(days=1),
        )
        output_asset = Asset(
            id="zip-output-1",
            user_id=user.id,
            kind="result",
            original_name="episode-1.mp4",
            mime_type="video/mp4",
            storage_key="episode-1.mp4",
            url="/uploads/episode-1.mp4",
            size_bytes=result_path.stat().st_size,
            duration_seconds=0,
            expires_at=now() + timedelta(days=1),
        )
        succeeded_task = Task(
            id="zip-task-1",
            user_id=user.id,
            tool_slug="subtitle-translate-workflow",
            input_asset_id=input_one.id,
            output_asset_id=output_asset.id,
            status="succeeded",
            params={"internalBatchId": "zip-batch", "internalBatchName": "后台 ZIP 测试"},
            estimated_credits=1,
            frozen_credits=0,
            charged_credits=1,
            provider="mock",
            provider_job_id="zip-provider-1",
            output_url="/uploads/episode-1.mp4",
            progress_percent=100,
            progress_stage="done",
            completed_at=now(),
        )
        failed_task = Task(
            id="zip-task-2",
            user_id=user.id,
            tool_slug="subtitle-translate-workflow",
            input_asset_id=input_two.id,
            output_asset_id=None,
            status="failed",
            params={"internalBatchId": "zip-batch", "internalBatchName": "后台 ZIP 测试"},
            estimated_credits=1,
            frozen_credits=0,
            charged_credits=0,
            provider="mock",
            provider_job_id="zip-provider-2",
            error_code="GPU_ERROR",
            output_url="",
            progress_percent=30,
            progress_stage="GPU 任务失败",
            completed_at=now(),
        )
        db.add_all([user, wallet, input_one, input_two, output_asset, succeeded_task, failed_task])
        db.commit()

        archive = create_internal_batch_zip(db, user.id, "zip-batch")
        zip_path = archive["path"]
        payload = admin.admin_internal_batch_zips(db)
        delete_payload = admin.admin_delete_internal_batch_zips(db, [{"userId": user.id, "batchId": "zip-batch", "partIndex": 1}])
        ready_after_delete = admin.admin_internal_batch_zips(db)
        failed_after_delete = admin.admin_internal_batch_zips(db, status="failed")
        processing_after_delete = admin.admin_internal_batch_zips(db, status="processing")

    item = payload["items"][0]
    assert item["succeeded"] == 1
    assert item["failed"] == 1
    assert len(item["skippedTasks"]) == 1
    skipped = item["skippedTasks"][0]
    assert skipped["taskId"] == "zip-task-2"
    assert skipped["episode"] == "2"
    assert skipped["inputAssetName"] == "episode-2.mp4"
    assert skipped["status"] == "failed"
    assert skipped["errorCode"] == "GPU_ERROR"
    assert skipped["progressStage"] == "GPU 任务失败"
    assert delete_payload == {"deleted": 1, "missing": 0, "failed": []}
    assert not zip_path.exists()
    assert ready_after_delete["page"]["total"] == 0
    assert failed_after_delete["page"]["total"] == 0
    assert processing_after_delete["page"]["total"] == 0


def test_gpu_job_display_map_matches_remote_gpu_job_id() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as db:
        user = User(id="gpu-user", email="gpu@example.com", name="GPU User", role="user", status="active")
        wallet = Wallet(user_id=user.id, credits=100, frozen_credits=0)
        input_asset = Asset(
            id="gpu-input",
            user_id=user.id,
            kind="video",
            original_name="第01集.mp4",
            mime_type="video/mp4",
            storage_key="episode-1.mp4",
            url="/uploads/episode-1.mp4",
            size_bytes=10,
            duration_seconds=10,
            expires_at=now() + timedelta(days=1),
        )
        task = Task(
            id="gpu-task",
            user_id=user.id,
            tool_slug="subtitle-translate-workflow",
            input_asset_id=input_asset.id,
            status="processing",
            params={
                "internalBatchId": "gpu-batch",
                "internalBatchName": "测试短剧（2集）",
                "remoteGpuJobId": "remote-gpu-1",
                "remoteGpuJobIds": ["remote-gpu-0", "remote-gpu-1"],
            },
            estimated_credits=1,
            frozen_credits=1,
            provider="mock",
            provider_job_id="provider-gpu-1",
            progress_percent=55,
            progress_stage="远端处理中",
        )
        db.add_all([user, wallet, input_asset, task])
        db.commit()

        display_map = admin._gpu_job_display_map(db, ["remote-gpu-1", "provider-gpu-1", "remote-gpu-0"])

    assert display_map["remote-gpu-1"]["displayName"] == "测试短剧（2集）"
    assert display_map["remote-gpu-1"]["displaySubtitle"] == "第01集.mp4"
    assert display_map["provider-gpu-1"]["inputAssetName"] == "第01集.mp4"
    assert display_map["remote-gpu-0"]["taskId"] == "gpu-task"


def test_admin_create_internal_batch_missing_task_uses_template_params(tmp_path, monkeypatch) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    monkeypatch.setattr(services.settings, "upload_dir", str(tmp_path / "uploads"))
    monkeypatch.setattr(services, "storage", FakeLocalStorage(tmp_path))
    enqueued: list[str] = []
    monkeypatch.setattr(services, "enqueue_provider_job", lambda task_id: enqueued.append(task_id))

    with Session(engine) as db:
        user = User(id="batch-user", email="batch@example.com", name="Batch User", role="user", status="active")
        wallet = Wallet(user_id=user.id, credits=100, frozen_credits=0)
        existing_one = Asset(
            id="input-1",
            user_id=user.id,
            kind="video",
            original_name="01.mp4",
            mime_type="video/mp4",
            storage_key="01.mp4",
            url="/uploads/01.mp4",
            size_bytes=10,
            duration_seconds=10,
            expires_at=now() + timedelta(days=1),
        )
        existing_three = Asset(
            id="input-3",
            user_id=user.id,
            kind="video",
            original_name="03.mp4",
            mime_type="video/mp4",
            storage_key="03.mp4",
            url="/uploads/03.mp4",
            size_bytes=10,
            duration_seconds=10,
            expires_at=now() + timedelta(days=1),
        )
        missing_asset = Asset(
            id="input-2",
            user_id=user.id,
            kind="video",
            original_name="02.mp4",
            mime_type="video/mp4",
            storage_key="02.mp4",
            url="/uploads/02.mp4",
            size_bytes=10,
            duration_seconds=11,
            expires_at=now() + timedelta(days=1),
        )
        base_params = {
            "duration": 10,
            "mode": "manual",
            "regions": [{"x": 1, "y": 2, "width": 3, "height": 4}],
            "targetLanguage": "en",
            "subtitlePlacement": "bottom",
            "internalBatchId": "batch-missing",
            "internalBatchName": "补传测试（3集）",
            "internalBatchTotal": 3,
        }
        task_one = Task(
            id="task-1",
            user_id=user.id,
            tool_slug="subtitle-translate-workflow",
            input_asset_id=existing_one.id,
            status="succeeded",
            params={**base_params, "internalBatchIndex": 1, "remoteGpuJobId": "remote-old"},
            estimated_credits=1,
            frozen_credits=0,
            charged_credits=1,
            provider="mock",
            provider_job_id="provider-1",
            progress_percent=100,
            progress_stage="done",
            completed_at=now(),
        )
        task_three = Task(
            id="task-3",
            user_id=user.id,
            tool_slug="subtitle-translate-workflow",
            input_asset_id=existing_three.id,
            status="succeeded",
            params={**base_params, "internalBatchIndex": 3},
            estimated_credits=1,
            frozen_credits=0,
            charged_credits=1,
            provider="mock",
            provider_job_id="provider-3",
            progress_percent=100,
            progress_stage="done",
            completed_at=now(),
        )
        db.add_all([user, wallet, existing_one, existing_three, missing_asset, task_one, task_three])
        db.commit()

        payload = admin.admin_create_internal_batch_missing_task(db, user.id, "batch-missing", missing_asset.id, 2, duration_seconds=11)
        created_task = db.get(Task, payload["task"]["id"])
        assert created_task is not None
        created_task_id = created_task.id
        assert created_task.input_asset_id == "input-2"
        assert created_task.status == "queued"
        assert created_task.params["internalBatchIndex"] == 2
        assert created_task.params["internalBatchTotal"] == 3
        assert created_task.params["duration"] == 11
        assert created_task.params["targetLanguage"] == "en"
        assert created_task.params["regions"] == [{"x": 1, "y": 2, "width": 3, "height": 4}]
        assert "remoteGpuJobId" not in created_task.params
        assert payload["batch"]["created"] == 3

    assert enqueued == [created_task_id]


def test_admin_internal_batch_zips_deletes_processing_row_without_zip(tmp_path, monkeypatch) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    monkeypatch.setattr(services.settings, "upload_dir", str(tmp_path / "uploads"))
    monkeypatch.setattr(services, "storage", FakeLocalStorage(tmp_path))
    monkeypatch.setattr(admin.settings, "upload_dir", str(tmp_path / "uploads"))

    with Session(engine) as db:
        user = User(id="zip-user", email="zip@example.com", name="Zip User", role="user", status="active")
        wallet = Wallet(user_id=user.id, credits=100, frozen_credits=0)
        input_asset = Asset(
            id="zip-input",
            user_id=user.id,
            kind="video",
            original_name="waiting.mp4",
            mime_type="video/mp4",
            storage_key="waiting-input.mp4",
            url="/uploads/waiting-input.mp4",
            size_bytes=10,
            duration_seconds=10,
            expires_at=now() + timedelta(days=1),
        )
        task = Task(
            id="zip-task",
            user_id=user.id,
            tool_slug="subtitle-translate-workflow",
            input_asset_id=input_asset.id,
            output_asset_id=None,
            status="processing",
            params={"internalBatchId": "waiting-batch", "internalBatchName": "等待打包测试"},
            estimated_credits=1,
            frozen_credits=1,
            charged_credits=0,
            provider="mock",
            provider_job_id="zip-provider-waiting",
            output_url="",
            progress_percent=60,
            progress_stage="生成中",
            completed_at=None,
        )
        db.add_all([user, wallet, input_asset, task])
        db.commit()

        payload = admin.admin_internal_batch_zips(db, status="processing")
        delete_payload = admin.admin_delete_internal_batch_zips(db, [{"userId": user.id, "batchId": "waiting-batch", "partIndex": 0}])
        after_delete = admin.admin_internal_batch_zips(db, status="processing")

    assert payload["page"]["total"] == 1
    assert payload["items"][0]["partIndex"] == 0
    assert delete_payload == {"deleted": 1, "missing": 0, "failed": []}
    assert after_delete["page"]["total"] == 0
