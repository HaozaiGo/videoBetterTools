from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import json
import time
import zipfile

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import app.services as services
from app.models import Asset, Base, Task, User, Wallet
from app.storage import StoredObject
from app.services import create_internal_batch_zip, create_task, delete_tasks_from_list, get_task_result_access, get_task_result_url, internal_batch_status, now, paginated_tasks, plan_internal_batch_zip, prioritize_internal_batch_queued_task, retry_failed_task_single_gpu, retry_internal_batch_missing_result_task, retry_internal_batch_task_with_replacement_asset, retry_internal_batch_tasks, task_to_dict


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


class FakeRemoteStorage:
    is_remote = True

    def __init__(self, existing_keys: set[str] | None = None) -> None:
        self.existing_keys = existing_keys or set()

    def remote_exists(self, storage_key: str) -> bool:
        return storage_key in self.existing_keys

    def public_url(self, storage_key: str) -> str:
        return f"https://tos.example.test/{storage_key}"


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


def test_create_task_rejects_unreadable_remote_input(monkeypatch) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    monkeypatch.setattr(services, "storage", FakeRemoteStorage())
    enqueued: list[str] = []
    monkeypatch.setattr(services, "enqueue_provider_job", enqueued.append)

    with Session(engine) as db:
        user = User(id="user-create-missing", email="create-missing@example.com", name="Create Missing", role="user", status="active")
        wallet = Wallet(user_id=user.id, credits=100, frozen_credits=0)
        asset = Asset(
            id="asset-create-missing",
            user_id=user.id,
            kind="video",
            original_name="missing.mp4",
            mime_type="video/mp4",
            storage_key="missing.mp4",
            url="https://tos.example.test/missing.mp4",
            size_bytes=10,
            duration_seconds=10,
            expires_at=now() + timedelta(days=1),
        )
        db.add_all([user, wallet, asset])
        db.commit()

        with pytest.raises(HTTPException) as exc:
            create_task(db, user.id, "remove-subtitle", asset.id, {"modelAdapter": "propainter"})
        wallet_after = db.get(Wallet, user.id)
        task_count = db.query(Task).count()

    assert exc.value.status_code == 400
    assert exc.value.detail == services.INPUT_ASSET_REMOTE_MISSING_MESSAGE
    assert enqueued == []
    assert task_count == 0
    assert wallet_after.frozen_credits == 0


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
        with pytest.raises(HTTPException) as exc:
            create_internal_batch_zip(db, user.id, batch_id)
        tasks[2].status = "cancelled"
        tasks[2].error_code = "USER_CANCELLED"
        tasks[2].completed_at = now()
        db.commit()
        ready_status = internal_batch_status(db, user.id, batch_id)
        archive = create_internal_batch_zip(db, user.id, batch_id)

    assert exc.value.status_code == 409
    assert status["downloadReady"] is False
    assert status["succeeded"] == 1
    assert status["total"] == 3
    assert status["processing"] == 1
    assert ready_status["downloadReady"] is True

    with zipfile.ZipFile(archive["path"]) as zip_file:
        names = zip_file.namelist()
        summary = json.loads(zip_file.read("_batch-summary.json"))
        video_names = [name for name in names if name.endswith(".mp4")]

    assert len(video_names) == 1
    assert video_names[0].startswith("001-clip-1-task-1")
    assert summary["succeeded"] == 1
    assert summary["failed"] == 1
    assert summary["cancelled"] == 1
    assert summary["processing"] == 0
    assert summary["includedTaskIds"] == ["task-1"]
    assert {task["id"] for task in summary["skippedTasks"]} == {"task-2", "task-3"}


def test_internal_batch_zip_waits_for_declared_episode_total(tmp_path, monkeypatch) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    monkeypatch.setattr(services.settings, "upload_dir", str(tmp_path))

    batch_id = "batch-65"
    batch_name = "91.闺蜜误良缘（65集）"

    with Session(engine) as db:
        user = User(id="user-batch-65", email="batch65@example.com", name="Batch 65 User", role="user", status="active")
        wallet = Wallet(user_id=user.id, credits=100, frozen_credits=0)
        db.add_all([user, wallet])

        for index in range(1, 24):
            asset = Asset(
                id=f"asset-65-{index}",
                user_id=user.id,
                kind="video",
                original_name=f"clip-{index}.mp4",
                mime_type="video/mp4",
                storage_key=f"input-65-{index}.mp4",
                url=f"/uploads/input-65-{index}.mp4",
                size_bytes=10,
                duration_seconds=10,
                expires_at=now() + timedelta(days=1),
            )
            task = Task(
                id=f"task-65-{index}",
                user_id=user.id,
                tool_slug="subtitle-translate-workflow",
                input_asset_id=asset.id,
                output_asset_id=None,
                status="succeeded",
                params={"internalBatchId": batch_id, "internalBatchName": batch_name},
                estimated_credits=1,
                frozen_credits=0,
                charged_credits=1,
                provider="mock",
                provider_job_id=f"provider-65-{index}",
                output_url="",
                progress_percent=100,
                progress_stage="处理完成",
            )
            db.add_all([asset, task])
        db.commit()

        status = internal_batch_status(db, user.id, batch_id)
        with pytest.raises(HTTPException) as exc:
            create_internal_batch_zip(db, user.id, batch_id)

    assert status["total"] == 65
    assert status["created"] == 23
    assert status["succeeded"] == 23
    assert status["missing"] == 42
    assert status["processing"] == 42
    assert status["downloadReady"] is False
    assert exc.value.status_code == 409


def test_internal_batch_zip_releases_db_before_materializing_remote_files(tmp_path, monkeypatch) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    monkeypatch.setattr(services.settings, "upload_dir", str(tmp_path))

    source_path = tmp_path / "remote-result.mp4"
    source_path.write_bytes(b"remote-video")

    with Session(engine) as db:
        class AssertClosedStorage:
            def ensure_local(self, storage_key: str):
                assert storage_key == "remote-result.mp4"
                assert not db.in_transaction()
                return source_path

        monkeypatch.setattr(services, "storage", AssertClosedStorage())

        user = User(id="user-remote-zip", email="remote-zip@example.com", name="Remote Zip User", role="user", status="active")
        wallet = Wallet(user_id=user.id, credits=100, frozen_credits=0)
        input_asset = Asset(
            id="remote-input-asset",
            user_id=user.id,
            kind="video",
            original_name="remote.mp4",
            mime_type="video/mp4",
            storage_key="remote-input.mp4",
            url="/uploads/remote-input.mp4",
            size_bytes=10,
            duration_seconds=10,
            expires_at=now() + timedelta(days=1),
        )
        output_asset = Asset(
            id="remote-output-asset",
            user_id=user.id,
            kind="video",
            original_name="remote-result.mp4",
            mime_type="video/mp4",
            storage_key="remote-result.mp4",
            url="https://example.test/remote-result.mp4",
            size_bytes=12,
            duration_seconds=10,
            expires_at=now() + timedelta(days=1),
        )
        task = Task(
            id="remote-zip-task",
            user_id=user.id,
            tool_slug="subtitle-translate-workflow",
            input_asset_id=input_asset.id,
            output_asset_id=output_asset.id,
            status="succeeded",
            params={"internalBatchId": "remote-zip-batch", "internalBatchName": "remote zip"},
            estimated_credits=0,
            frozen_credits=0,
            charged_credits=0,
            provider="mock",
            provider_job_id="remote-provider",
            output_url="",
            progress_percent=100,
            progress_stage="处理完成",
        )
        db.add_all([user, wallet, input_asset, output_asset, task])
        db.commit()

        archive = create_internal_batch_zip(db, user.id, "remote-zip-batch")

    with zipfile.ZipFile(archive["path"]) as zip_file:
        assert zip_file.read("001-remote-remote-z.mp4") == b"remote-video"


def test_internal_batch_zip_can_be_materialized_on_gpu_and_marked_ready(tmp_path, monkeypatch) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    monkeypatch.setattr(services.settings, "upload_dir", str(tmp_path))
    monkeypatch.setattr(services.settings, "internal_batch_zip_gpu_enabled", True)
    monkeypatch.setattr(services.settings, "model_plaza_gpu_api_url", "https://gpu.example.test")

    class FakeRemoteStorage:
        is_remote = True

        def presign_download(self, storage_key: str, filename: str | None = None) -> str:
            return f"https://tos.example.test/{storage_key}?filename={filename or ''}"

    requested_payloads: list[dict] = []

    def fake_remote_zip(payload: dict) -> dict:
        requested_payloads.append(payload)
        return {
            "url": "https://tos.example.test/model-plaza/output/zips/batch/remote.zip",
            "storage_key": payload["zip_storage_key"],
            "size_bytes": 1234,
        }

    monkeypatch.setattr(services, "storage", FakeRemoteStorage())
    monkeypatch.setattr(services, "_request_remote_internal_batch_zip", fake_remote_zip)
    output_storage_key = "model-plaza/output/videos/2026/07/02/result.mp4"

    with Session(engine) as db:
        user = User(id="user-gpu-zip", email="gpu-zip@example.com", name="GPU Zip User", role="user", status="active")
        wallet = Wallet(user_id=user.id, credits=100, frozen_credits=0)
        input_asset = Asset(
            id="gpu-zip-input",
            user_id=user.id,
            kind="video",
            original_name="clip.mp4",
            mime_type="video/mp4",
            storage_key="input.mp4",
            url="https://tos.example.test/input.mp4",
            size_bytes=10,
            duration_seconds=10,
            expires_at=now() + timedelta(days=1),
        )
        output_asset = Asset(
            id="gpu-zip-output",
            user_id=user.id,
            kind="video",
            original_name="clip-result.mp4",
            mime_type="video/mp4",
            storage_key=output_storage_key,
            url="https://tos.example.test/model-plaza/output/videos/2026/07/02/result.mp4",
            size_bytes=12,
            duration_seconds=10,
            expires_at=now() + timedelta(days=1),
        )
        task = Task(
            id="gpu-zip-task",
            user_id=user.id,
            tool_slug="subtitle-translate-workflow",
            input_asset_id=input_asset.id,
            output_asset_id=output_asset.id,
            status="succeeded",
            params={"internalBatchId": "gpu-zip-batch", "internalBatchName": "gpu zip"},
            estimated_credits=0,
            frozen_credits=0,
            charged_credits=0,
            provider="mock",
            provider_job_id="gpu-zip-provider",
            output_url=output_asset.url,
            progress_percent=100,
            progress_stage="处理完成",
        )
        db.add_all([user, wallet, input_asset, output_asset, task])
        db.commit()

        archive = create_internal_batch_zip(db, user.id, "gpu-zip-batch", part=1)

        assert archive["parts"][0]["sizeBytes"] == 1234
        assert archive["parts"][0]["remoteUrl"].startswith("https://tos.example.test/model-plaza/output/zips/")
        assert archive["parts"][0]["remoteUrl"].endswith("?filename=gpu zip.zip")
        manifest = plan_internal_batch_zip(db, user.id, "gpu-zip-batch")

    assert requested_payloads[0]["entries"][0]["storage_key"] == output_storage_key
    assert requested_payloads[0]["entries"][0]["download_url"].startswith("https://tos.example.test/model-plaza/output/videos/")
    assert requested_payloads[0]["summary"]["includedTaskIds"] == ["gpu-zip-task"]
    assert manifest["parts"][0]["sizeBytes"] == 1234


def test_internal_batch_zip_recreates_stale_remote_marker(tmp_path, monkeypatch) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    monkeypatch.setattr(services.settings, "upload_dir", str(tmp_path))
    monkeypatch.setattr(services.settings, "internal_batch_zip_gpu_enabled", True)
    monkeypatch.setattr(services.settings, "model_plaza_gpu_api_url", "https://gpu.example.test")

    class FakeRemoteStorage:
        is_remote = True

        def __init__(self) -> None:
            self.existing_keys: set[str] = set()

        def presign_download(self, storage_key: str, filename: str | None = None) -> str:
            return f"https://tos.example.test/{storage_key}?filename={filename or ''}"

        def remote_exists(self, storage_key: str) -> bool:
            return storage_key in self.existing_keys

    fake_storage = FakeRemoteStorage()
    requested_payloads: list[dict] = []

    def fake_remote_zip(payload: dict) -> dict:
        requested_payloads.append(payload)
        fake_storage.existing_keys.add(payload["zip_storage_key"])
        return {
            "url": f"https://tos.example.test/{payload['zip_storage_key']}",
            "storage_key": payload["zip_storage_key"],
            "size_bytes": 4321,
        }

    monkeypatch.setattr(services, "storage", fake_storage)
    monkeypatch.setattr(services, "_request_remote_internal_batch_zip", fake_remote_zip)

    with Session(engine) as db:
        user = User(id="user-stale-gpu-zip", email="stale-gpu-zip@example.com", name="Stale GPU Zip User", role="user", status="active")
        wallet = Wallet(user_id=user.id, credits=100, frozen_credits=0)
        input_asset = Asset(
            id="stale-gpu-zip-input",
            user_id=user.id,
            kind="video",
            original_name="clip.mp4",
            mime_type="video/mp4",
            storage_key="input.mp4",
            url="https://tos.example.test/input.mp4",
            size_bytes=10,
            duration_seconds=10,
            expires_at=now() + timedelta(days=1),
        )
        output_asset = Asset(
            id="stale-gpu-zip-output",
            user_id=user.id,
            kind="video",
            original_name="clip-result.mp4",
            mime_type="video/mp4",
            storage_key="model-plaza/output/videos/2026/07/02/stale-result.mp4",
            url="https://tos.example.test/model-plaza/output/videos/2026/07/02/stale-result.mp4",
            size_bytes=12,
            duration_seconds=10,
            expires_at=now() + timedelta(days=1),
        )
        task = Task(
            id="stale-gpu-zip-task",
            user_id=user.id,
            tool_slug="subtitle-translate-workflow",
            input_asset_id=input_asset.id,
            output_asset_id=output_asset.id,
            status="succeeded",
            params={"internalBatchId": "stale-gpu-zip-batch", "internalBatchName": "stale gpu zip"},
            estimated_credits=0,
            frozen_credits=0,
            charged_credits=0,
            provider="mock",
            provider_job_id="stale-gpu-zip-provider",
            output_url=output_asset.url,
            progress_percent=100,
            progress_stage="处理完成",
        )
        db.add_all([user, wallet, input_asset, output_asset, task])
        db.commit()

        manifest = plan_internal_batch_zip(db, user.id, "stale-gpu-zip-batch")
        zip_path = manifest["parts"][0]["path"]
        stale_marker = zip_path.with_suffix(zip_path.suffix + ".remote.json")
        stale_marker.write_text(
            json.dumps(
                {
                    "url": "https://tos.example.test/model-plaza/output/zips/stale/missing.zip",
                    "storageKey": "model-plaza/output/zips/stale/missing.zip",
                    "sizeBytes": 9999,
                    "filename": "stale gpu zip.zip",
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

        archive = create_internal_batch_zip(db, user.id, "stale-gpu-zip-batch", part=1)

    assert len(requested_payloads) == 1
    assert archive["parts"][0]["sizeBytes"] == 4321
    assert archive["parts"][0]["storageKey"] == requested_payloads[0]["zip_storage_key"]
    assert archive["parts"][0]["remoteUrl"].startswith("https://tos.example.test/model-plaza/output/zips/")
    marker = json.loads(stale_marker.read_text(encoding="utf-8"))
    assert marker["storageKey"] == requested_payloads[0]["zip_storage_key"]


def test_internal_batch_zip_restores_missing_tos_object_from_gpu_local_zip(tmp_path, monkeypatch) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    monkeypatch.setattr(services.settings, "upload_dir", str(tmp_path))
    monkeypatch.setattr(services.settings, "internal_batch_zip_gpu_enabled", True)
    monkeypatch.setattr(services.settings, "model_plaza_gpu_api_url", "https://gpu.example.test")

    class FakeRemoteStorage:
        is_remote = True

        def __init__(self) -> None:
            self.saved_files: list[tuple[str, bytes]] = []

        def presign_download(self, storage_key: str, filename: str | None = None) -> str:
            return f"https://upload-mmm.example.test/{storage_key}?filename={filename or ''}"

        def remote_exists(self, storage_key: str) -> bool:
            return any(saved_key == storage_key for saved_key, _content in self.saved_files)

        def save_file(self, storage_key: str, local_path) -> StoredObject:
            content = local_path.read_bytes()
            self.saved_files.append((storage_key, content))
            return StoredObject(storage_key=storage_key, public_url=f"https://upload-mmm.example.test/{storage_key}", size=len(content))

    fake_storage = FakeRemoteStorage()
    requested_payloads: list[dict] = []
    downloaded_zip_ids: list[str] = []

    def fake_remote_zip(payload: dict) -> dict:
        requested_payloads.append(payload)
        return {
            "url": f"https://jkx-data.example.test/{payload['zip_storage_key']}",
            "storage_key": payload["zip_storage_key"],
            "size_bytes": 9999,
        }

    def fake_download_from_gpu(zip_id: str, target_path):
        downloaded_zip_ids.append(zip_id)
        target_path.write_bytes(b"gpu-local-zip")
        return target_path

    monkeypatch.setattr(services, "storage", fake_storage)
    monkeypatch.setattr(services, "_request_remote_internal_batch_zip", fake_remote_zip)
    monkeypatch.setattr(services, "_download_remote_internal_batch_zip_from_gpu", fake_download_from_gpu)

    with Session(engine) as db:
        user = User(id="user-restore-gpu-zip", email="restore-gpu-zip@example.com", name="Restore GPU Zip User", role="user", status="active")
        wallet = Wallet(user_id=user.id, credits=100, frozen_credits=0)
        input_asset = Asset(
            id="restore-gpu-zip-input",
            user_id=user.id,
            kind="video",
            original_name="clip.mp4",
            mime_type="video/mp4",
            storage_key="input.mp4",
            url="https://upload-mmm.example.test/input.mp4",
            size_bytes=10,
            duration_seconds=10,
            expires_at=now() + timedelta(days=1),
        )
        output_asset = Asset(
            id="restore-gpu-zip-output",
            user_id=user.id,
            kind="video",
            original_name="clip-result.mp4",
            mime_type="video/mp4",
            storage_key="model-plaza/output/videos/2026/07/02/restore-result.mp4",
            url="https://upload-mmm.example.test/model-plaza/output/videos/2026/07/02/restore-result.mp4",
            size_bytes=12,
            duration_seconds=10,
            expires_at=now() + timedelta(days=1),
        )
        task = Task(
            id="restore-gpu-zip-task",
            user_id=user.id,
            tool_slug="subtitle-translate-workflow",
            input_asset_id=input_asset.id,
            output_asset_id=output_asset.id,
            status="succeeded",
            params={"internalBatchId": "restore-gpu-zip-batch", "internalBatchName": "restore gpu zip"},
            estimated_credits=0,
            frozen_credits=0,
            charged_credits=0,
            provider="mock",
            provider_job_id="restore-gpu-zip-provider",
            output_url=output_asset.url,
            progress_percent=100,
            progress_stage="处理完成",
        )
        db.add_all([user, wallet, input_asset, output_asset, task])
        db.commit()

        archive = create_internal_batch_zip(db, user.id, "restore-gpu-zip-batch", part=1)

    assert len(requested_payloads) == 1
    assert downloaded_zip_ids == [requested_payloads[0]["zip_id"]]
    assert fake_storage.saved_files == [(requested_payloads[0]["zip_storage_key"], b"gpu-local-zip")]
    assert archive["parts"][0]["sizeBytes"] == len(b"gpu-local-zip")
    assert archive["parts"][0]["remoteUrl"].startswith("https://upload-mmm.example.test/model-plaza/output/zips/")


def test_completed_internal_batch_auto_enqueues_gpu_zip_prepare(tmp_path, monkeypatch) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    monkeypatch.setattr(services.settings, "upload_dir", str(tmp_path))
    monkeypatch.setattr(services.settings, "internal_batch_zip_auto_prepare_enabled", True)
    monkeypatch.setattr(services.settings, "internal_batch_zip_gpu_enabled", True)
    monkeypatch.setattr(services.settings, "model_plaza_gpu_api_url", "https://gpu.example.test")

    class FakeRemoteStorage:
        is_remote = True

    enqueued: list[tuple[str, str]] = []
    monkeypatch.setattr(services, "storage", FakeRemoteStorage())
    monkeypatch.setattr(services, "enqueue_internal_batch_zip", lambda user_id, batch_id: enqueued.append((user_id, batch_id)))

    batch_id = "auto-zip-batch"
    with Session(engine) as db:
        user = User(id="user-auto-zip", email="auto-zip@example.com", name="Auto Zip User", role="user", status="active")
        wallet = Wallet(user_id=user.id, credits=100, frozen_credits=1)
        db.add_all([user, wallet])

        done_input = Asset(
            id="auto-zip-input-1",
            user_id=user.id,
            kind="video",
            original_name="done.mp4",
            mime_type="video/mp4",
            storage_key="input-done.mp4",
            url="https://tos.example.test/input-done.mp4",
            size_bytes=10,
            duration_seconds=10,
            expires_at=now() + timedelta(days=1),
        )
        done_output = Asset(
            id="auto-zip-output-1",
            user_id=user.id,
            kind="result",
            original_name="done-result.mp4",
            mime_type="video/mp4",
            storage_key="model-plaza/output/videos/done.mp4",
            url="https://tos.example.test/done.mp4",
            size_bytes=10,
            expires_at=now() + timedelta(days=1),
        )
        pending_input = Asset(
            id="auto-zip-input-2",
            user_id=user.id,
            kind="video",
            original_name="pending.mp4",
            mime_type="video/mp4",
            storage_key="input-pending.mp4",
            url="https://tos.example.test/input-pending.mp4",
            size_bytes=10,
            duration_seconds=10,
            expires_at=now() + timedelta(days=1),
        )
        done_task = Task(
            id="auto-zip-task-1",
            user_id=user.id,
            tool_slug="subtitle-translate-workflow",
            input_asset_id=done_input.id,
            output_asset_id=done_output.id,
            status="succeeded",
            params={"internalBatchId": batch_id, "internalBatchName": "auto zip"},
            estimated_credits=1,
            frozen_credits=0,
            charged_credits=1,
            provider="mock",
            provider_job_id="auto-zip-provider-1",
            output_url=done_output.url,
            progress_percent=100,
            progress_stage="处理完成",
        )
        pending_task = Task(
            id="auto-zip-task-2",
            user_id=user.id,
            tool_slug="subtitle-translate-workflow",
            input_asset_id=pending_input.id,
            status="processing",
            params={"internalBatchId": batch_id, "internalBatchName": "auto zip"},
            estimated_credits=1,
            frozen_credits=1,
            charged_credits=0,
            provider="mock",
            provider_job_id="auto-zip-provider-2",
            progress_percent=95,
            progress_stage="远端处理完成",
        )
        db.add_all([done_input, done_output, pending_input, done_task, pending_task])
        db.commit()

        duplicated, task = services.provider_callback(
            db,
            "auto-zip-provider-2",
            "succeeded",
            callback_id="auto-zip-provider-2:succeeded",
            output_url="https://tos.example.test/pending-result.mp4",
            output_storage_key="model-plaza/output/videos/pending-result.mp4",
            output_mime_type="video/mp4",
            output_size_bytes=10,
        )

    assert duplicated is False
    assert task.status == "succeeded"
    assert enqueued == [("user-auto-zip", batch_id)]


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


def test_internal_batch_retry_can_enqueue_at_front(tmp_path, monkeypatch) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    monkeypatch.setattr(services.settings, "upload_dir", str(tmp_path))
    enqueued: list[tuple[str, bool]] = []

    def fake_enqueue(task_id: str, at_front: bool = False) -> None:
        enqueued.append((task_id, at_front))

    monkeypatch.setattr(services, "enqueue_provider_job", fake_enqueue)

    with Session(engine) as db:
        user = User(id="user-retry-front", email="retry-front@example.com", name="Retry Front User", role="user", status="active")
        wallet = Wallet(user_id=user.id, credits=100, frozen_credits=0)
        asset = Asset(
            id="retry-front-asset",
            user_id=user.id,
            kind="video",
            original_name="retry-front.mp4",
            mime_type="video/mp4",
            storage_key="retry-front-input.mp4",
            url="/uploads/retry-front-input.mp4",
            size_bytes=10,
            duration_seconds=10,
            expires_at=now() + timedelta(days=1),
        )
        task = Task(
            id="retry-front-task",
            user_id=user.id,
            tool_slug="subtitle-translate-workflow",
            input_asset_id=asset.id,
            output_asset_id=None,
            status="failed",
            params={"internalBatchId": "batch-retry-front", "internalBatchName": "retry front batch"},
            estimated_credits=0,
            frozen_credits=0,
            charged_credits=0,
            provider="mock",
            provider_job_id="old-retry-front-provider",
            error_code="PROVIDER_FAILED",
            output_url="",
            progress_percent=0,
            progress_stage="failed",
            completed_at=now(),
        )
        db.add_all([user, wallet, asset, task])
        db.commit()

        result = retry_internal_batch_tasks(db, user.id, "batch-retry-front", at_front=True)

    assert result["retried"] == 1
    assert enqueued == [("retry-front-task", True)]


def test_internal_batch_retry_can_replace_unreadable_input_and_enqueue_front(tmp_path, monkeypatch) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    monkeypatch.setattr(services.settings, "upload_dir", str(tmp_path))
    monkeypatch.setattr(services, "storage", FakeRemoteStorage(existing_keys={"replacement-input.mp4"}))
    enqueued: list[tuple[str, bool]] = []

    def fake_enqueue(task_id: str, at_front: bool = False) -> None:
        enqueued.append((task_id, at_front))

    monkeypatch.setattr(services, "enqueue_provider_job", fake_enqueue)

    with Session(engine) as db:
        user = User(id="user-replace-input", email="replace-input@example.com", name="Replace Input", role="user", status="active")
        wallet = Wallet(user_id=user.id, credits=100, frozen_credits=0)
        old_asset = Asset(
            id="old-input",
            user_id=user.id,
            kind="video",
            original_name="old.mp4",
            mime_type="video/mp4",
            storage_key="missing-input.mp4",
            url="https://tos.example.test/missing-input.mp4",
            size_bytes=10,
            duration_seconds=10,
            expires_at=now() + timedelta(days=1),
        )
        replacement_asset = Asset(
            id="replacement-input",
            user_id=user.id,
            kind="video",
            original_name="replacement.mp4",
            mime_type="video/mp4",
            storage_key="replacement-input.mp4",
            url="https://tos.example.test/replacement-input.mp4",
            size_bytes=10,
            duration_seconds=12,
            expires_at=now() + timedelta(days=1),
        )
        task = Task(
            id="replace-input-task",
            user_id=user.id,
            tool_slug="subtitle-translate-workflow",
            input_asset_id=old_asset.id,
            output_asset_id=None,
            status="failed",
            params={"internalBatchId": "batch-replace-input", "internalBatchName": "replace input batch", "internalBatchIndex": 2},
            estimated_credits=0,
            frozen_credits=0,
            charged_credits=0,
            provider="mock",
            provider_job_id="old-replace-provider",
            error_code="INPUT_ASSET_REMOTE_MISSING",
            output_url="",
            progress_percent=55,
            progress_stage="输入视频对象存储不可读，请重新上传后重试",
            completed_at=now(),
        )
        db.add_all([user, wallet, old_asset, replacement_asset, task])
        db.commit()

        result = retry_internal_batch_task_with_replacement_asset(
            db,
            user.id,
            "batch-replace-input",
            task.id,
            replacement_asset.id,
            duration_seconds=12,
            at_front=True,
        )
        retried = db.get(Task, task.id)
        assert retried is not None
        assert result["task"]["id"] == "replace-input-task"
        assert retried.input_asset_id == "replacement-input"
        assert retried.status == "queued"
        assert retried.params["duration"] == 12
        assert retried.error_code is None
        assert retried.completed_at is None

    assert enqueued == [("replace-input-task", True)]


def test_internal_batch_status_flags_succeeded_task_with_missing_result(monkeypatch) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    monkeypatch.setattr(services, "storage", FakeRemoteStorage(existing_keys={"input-ok.mp4"}))

    with Session(engine) as db:
        user = User(id="user-missing-result", email="missing-result@example.com", name="Missing Result", role="user", status="active")
        wallet = Wallet(user_id=user.id, credits=90, frozen_credits=0)
        input_asset = Asset(
            id="missing-result-input",
            user_id=user.id,
            kind="video",
            original_name="episode-1.mp4",
            mime_type="video/mp4",
            storage_key="input-ok.mp4",
            url="https://tos.example.test/input-ok.mp4",
            size_bytes=10,
            duration_seconds=10,
            expires_at=now() + timedelta(days=1),
        )
        output_asset = Asset(
            id="missing-result-output",
            user_id=user.id,
            kind="result",
            original_name="episode-1-result.mp4",
            mime_type="video/mp4",
            storage_key="missing-result.mp4",
            url="https://tos.example.test/missing-result.mp4",
            size_bytes=10,
            duration_seconds=0,
            expires_at=now() + timedelta(days=1),
        )
        task = Task(
            id="missing-result-task",
            user_id=user.id,
            tool_slug="subtitle-translate-workflow",
            input_asset_id=input_asset.id,
            output_asset_id=output_asset.id,
            status="succeeded",
            params={"internalBatchId": "batch-missing-result", "internalBatchName": "missing result batch", "internalBatchTotal": 1},
            estimated_credits=10,
            frozen_credits=0,
            charged_credits=10,
            provider="mock",
            provider_job_id="missing-result-provider",
            output_url="https://tos.example.test/missing-result.mp4",
            progress_percent=100,
            progress_stage="处理完成，结果已入库",
            completed_at=now(),
        )
        db.add_all([user, wallet, input_asset, output_asset, task])
        db.commit()

        status = internal_batch_status(db, user.id, "batch-missing-result")

    assert status["tasks"][0]["resultMissing"] is True
    assert "对象存储文件不存在" in status["tasks"][0]["resultMissingReason"]
    assert status["tasks"][0]["previewUrl"] == ""


def test_missing_result_retry_fronts_queue_without_double_charge(monkeypatch) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    monkeypatch.setattr(services, "storage", FakeRemoteStorage(existing_keys={"input-ok.mp4"}))
    enqueued: list[tuple[str, bool]] = []

    def fake_enqueue(task_id: str, at_front: bool = False) -> None:
        enqueued.append((task_id, at_front))

    monkeypatch.setattr(services, "enqueue_provider_job", fake_enqueue)

    with Session(engine) as db:
        user = User(id="user-missing-result-retry", email="missing-result-retry@example.com", name="Missing Result Retry", role="user", status="active")
        wallet = Wallet(user_id=user.id, credits=90, frozen_credits=0)
        input_asset = Asset(
            id="missing-result-retry-input",
            user_id=user.id,
            kind="video",
            original_name="episode-1.mp4",
            mime_type="video/mp4",
            storage_key="input-ok.mp4",
            url="https://tos.example.test/input-ok.mp4",
            size_bytes=10,
            duration_seconds=10,
            expires_at=now() + timedelta(days=1),
        )
        output_asset = Asset(
            id="missing-result-retry-output",
            user_id=user.id,
            kind="result",
            original_name="episode-1-result.mp4",
            mime_type="video/mp4",
            storage_key="missing-result.mp4",
            url="https://tos.example.test/missing-result.mp4",
            size_bytes=10,
            duration_seconds=0,
            expires_at=now() + timedelta(days=1),
        )
        task = Task(
            id="missing-result-retry-task",
            user_id=user.id,
            tool_slug="subtitle-translate-workflow",
            input_asset_id=input_asset.id,
            output_asset_id=output_asset.id,
            status="succeeded",
            params={"internalBatchId": "batch-missing-result-retry", "internalBatchName": "missing result retry", "internalBatchTotal": 1},
            estimated_credits=10,
            frozen_credits=0,
            charged_credits=10,
            provider="mock",
            provider_job_id="old-missing-result-retry",
            output_url="https://tos.example.test/missing-result.mp4",
            progress_percent=100,
            progress_stage="处理完成，结果已入库",
            completed_at=now(),
        )
        db.add_all([user, wallet, input_asset, output_asset, task])
        db.commit()

        result = retry_internal_batch_missing_result_task(db, user.id, "batch-missing-result-retry", task.id, at_front=True)
        retried = db.get(Task, task.id)
        provider_job_id = retried.provider_job_id
        wallet_after_retry = db.get(Wallet, user.id)

        assert result["task"]["id"] == task.id
        assert retried.status == "queued"
        assert retried.output_asset_id is None
        assert retried.charged_credits == 10
        assert retried.frozen_credits == 0
        assert retried.params["_noChargeRetry"] is True
        assert wallet_after_retry.credits == 90
        assert wallet_after_retry.frozen_credits == 0

        services.provider_callback(
            db,
            provider_job_id,
            "succeeded",
            output_storage_key="new-result.mp4",
            output_url="https://tos.example.test/new-result.mp4",
            output_mime_type="video/mp4",
            output_size_bytes=10,
        )
        completed = db.get(Task, task.id)
        wallet_after_callback = db.get(Wallet, user.id)

    assert enqueued == [("missing-result-retry-task", True)]
    assert completed.status == "succeeded"
    assert completed.charged_credits == 10
    assert wallet_after_callback.credits == 90
    assert wallet_after_callback.frozen_credits == 0


def test_missing_result_upload_retry_replaces_input_without_double_charge(monkeypatch) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    monkeypatch.setattr(services, "storage", FakeRemoteStorage(existing_keys={"replacement-ok.mp4"}))
    enqueued: list[tuple[str, bool]] = []

    def fake_enqueue(task_id: str, at_front: bool = False) -> None:
        enqueued.append((task_id, at_front))

    monkeypatch.setattr(services, "enqueue_provider_job", fake_enqueue)

    with Session(engine) as db:
        user = User(id="user-missing-result-upload", email="missing-result-upload@example.com", name="Missing Result Upload", role="user", status="active")
        wallet = Wallet(user_id=user.id, credits=90, frozen_credits=0)
        old_input = Asset(
            id="missing-result-upload-old-input",
            user_id=user.id,
            kind="video",
            original_name="old-episode-1.mp4",
            mime_type="video/mp4",
            storage_key="old-input-expired.mp4",
            url="https://tos.example.test/old-input-expired.mp4",
            size_bytes=10,
            duration_seconds=10,
            expires_at=now() - timedelta(days=1),
        )
        replacement_asset = Asset(
            id="missing-result-upload-replacement",
            user_id=user.id,
            kind="video",
            original_name="replacement-episode-1.mp4",
            mime_type="video/mp4",
            storage_key="replacement-ok.mp4",
            url="https://tos.example.test/replacement-ok.mp4",
            size_bytes=10,
            duration_seconds=12,
            expires_at=now() + timedelta(days=1),
        )
        output_asset = Asset(
            id="missing-result-upload-output",
            user_id=user.id,
            kind="result",
            original_name="episode-1-result.mp4",
            mime_type="video/mp4",
            storage_key="missing-output-expired.mp4",
            url="https://tos.example.test/missing-output-expired.mp4",
            size_bytes=10,
            duration_seconds=0,
            expires_at=now() - timedelta(days=1),
        )
        task = Task(
            id="missing-result-upload-task",
            user_id=user.id,
            tool_slug="subtitle-translate-workflow",
            input_asset_id=old_input.id,
            output_asset_id=output_asset.id,
            status="succeeded",
            params={"internalBatchId": "batch-missing-result-upload", "internalBatchName": "missing result upload", "internalBatchTotal": 1},
            estimated_credits=10,
            frozen_credits=0,
            charged_credits=10,
            provider="mock",
            provider_job_id="old-missing-result-upload",
            output_url="https://tos.example.test/missing-output-expired.mp4",
            progress_percent=100,
            progress_stage="处理完成，结果已入库",
            completed_at=now(),
        )
        db.add_all([user, wallet, old_input, replacement_asset, output_asset, task])
        db.commit()

        result = retry_internal_batch_task_with_replacement_asset(
            db,
            user.id,
            "batch-missing-result-upload",
            task.id,
            replacement_asset.id,
            duration_seconds=12,
            at_front=True,
        )
        retried = db.get(Task, task.id)
        wallet_after_retry = db.get(Wallet, user.id)

    assert result["task"]["id"] == "missing-result-upload-task"
    assert retried.input_asset_id == "missing-result-upload-replacement"
    assert retried.status == "queued"
    assert retried.output_asset_id is None
    assert retried.params["_noChargeRetry"] is True
    assert retried.params["_missingResultReason"] == "结果对象存储文件不存在，可能已过期清理"
    assert retried.charged_credits == 10
    assert retried.frozen_credits == 0
    assert wallet_after_retry.credits == 90
    assert wallet_after_retry.frozen_credits == 0
    assert enqueued == [("missing-result-upload-task", True)]


def test_prioritize_internal_batch_queued_task_enqueues_front_without_refreeze(monkeypatch) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    monkeypatch.setattr(services, "storage", FakeRemoteStorage(existing_keys={"priority-input.mp4"}))
    enqueued: list[tuple[str, bool]] = []

    def fake_enqueue(task_id: str, at_front: bool = False) -> None:
        enqueued.append((task_id, at_front))

    monkeypatch.setattr(services, "enqueue_provider_job", fake_enqueue)

    with Session(engine) as db:
        user = User(id="user-priority", email="priority@example.com", name="Priority User", role="user", status="active")
        wallet = Wallet(user_id=user.id, credits=100, frozen_credits=32)
        asset = Asset(
            id="priority-input",
            user_id=user.id,
            kind="video",
            original_name="priority.mp4",
            mime_type="video/mp4",
            storage_key="priority-input.mp4",
            url="https://tos.example.test/priority-input.mp4",
            size_bytes=10,
            duration_seconds=10,
            expires_at=now() + timedelta(days=1),
        )
        task = Task(
            id="priority-task",
            user_id=user.id,
            tool_slug="subtitle-translate-workflow",
            input_asset_id=asset.id,
            output_asset_id=None,
            status="queued",
            params={"internalBatchId": "batch-priority", "internalBatchName": "priority batch", "internalBatchTotal": 1},
            estimated_credits=32,
            frozen_credits=32,
            charged_credits=0,
            provider="mock",
            provider_job_id="priority-provider",
            output_url="",
            progress_percent=5,
            progress_stage="远端 GPU 队列已满，等待空位自动重试（第 813 次）",
        )
        db.add_all([user, wallet, asset, task])
        db.commit()

        result = prioritize_internal_batch_queued_task(db, user.id, "batch-priority", task.id, at_front=True)
        prioritized = db.get(Task, task.id)
        wallet_after = db.get(Wallet, user.id)

    assert result["task"]["id"] == "priority-task"
    assert prioritized.status == "queued"
    assert prioritized.frozen_credits == 32
    assert prioritized.params["_manualPriorityBoostCount"] == 1
    assert prioritized.progress_stage == "已插队到最高优先级，等待 worker 领取任务"
    assert wallet_after.frozen_credits == 32
    assert enqueued == [("priority-task", True)]


def test_internal_batch_retry_rejects_unreadable_remote_input(tmp_path, monkeypatch) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    monkeypatch.setattr(services.settings, "upload_dir", str(tmp_path))
    monkeypatch.setattr(services, "storage", FakeRemoteStorage())
    enqueued: list[str] = []
    monkeypatch.setattr(services, "enqueue_provider_job", enqueued.append)

    batch_id = "batch-retry-missing"

    with Session(engine) as db:
        user = User(id="user-retry-missing", email="retry-missing@example.com", name="Retry Missing", role="user", status="active")
        wallet = Wallet(user_id=user.id, credits=100, frozen_credits=0)
        asset = Asset(
            id="retry-missing-asset",
            user_id=user.id,
            kind="video",
            original_name="retry-missing.mp4",
            mime_type="video/mp4",
            storage_key="missing-input.mp4",
            url="https://tos.example.test/missing-input.mp4",
            size_bytes=10,
            duration_seconds=10,
            expires_at=now() + timedelta(days=1),
        )
        task = Task(
            id="retry-missing-task",
            user_id=user.id,
            tool_slug="subtitle-translate-workflow",
            input_asset_id=asset.id,
            status="failed",
            params={"internalBatchId": batch_id, "internalBatchName": "retry missing"},
            estimated_credits=0,
            frozen_credits=0,
            charged_credits=0,
            provider="mock",
            provider_job_id="old-retry-missing",
            error_code="INPUT_ASSET_REMOTE_MISSING",
            progress_percent=0,
            progress_stage="input missing",
            completed_at=now(),
        )
        db.add_all([user, wallet, asset, task])
        db.commit()

        with pytest.raises(HTTPException) as exc:
            retry_internal_batch_tasks(db, user.id, batch_id)
        task_after = db.get(Task, task.id)
        wallet_after = db.get(Wallet, user.id)

    assert exc.value.status_code == 400
    assert exc.value.detail == services.INPUT_ASSET_REMOTE_MISSING_MESSAGE
    assert enqueued == []
    assert task_after.status == "failed"
    assert task_after.provider_job_id == "old-retry-missing"
    assert wallet_after.frozen_credits == 0


def test_failed_task_serializes_specific_failure_reason() -> None:
    task = Task(
        id="oom-task",
        user_id="user-oom",
        tool_slug="remove-subtitle",
        input_asset_id="asset-oom",
        status="failed",
        params={},
        estimated_credits=1,
        frozen_credits=0,
        charged_credits=0,
        provider="mock",
        provider_job_id="provider-oom",
        error_code="VIDEO_PROCESSING_FAILED",
        progress_stage="CUDA_OUT_OF_MEMORY: GPU 显存不足",
    )

    payload = task_to_dict(task)

    assert payload["failureReason"] == "GPU 显存不足导致模型退出。建议点击“单卡重跑”，或降低并发后重试。"


def test_paginated_tasks_filters_by_status() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as db:
        user = User(id="user-filter", email="filter@example.com", name="Filter User", role="user", status="active")
        wallet = Wallet(user_id=user.id, credits=100, frozen_credits=0)
        db.add_all([user, wallet])

        for index, status in enumerate(["failed", "succeeded", "failed"], start=1):
            completed_at = None
            batch_name = "普通批次"
            if index == 2:
                completed_at = now() - timedelta(hours=2)
                batch_name = "64.仙王开局威压诸天万古（60集）AI短剧"
            asset = Asset(
                id=f"filter-asset-{index}",
                user_id=user.id,
                kind="video",
                original_name=f"filter-{index}.mp4",
                mime_type="video/mp4",
                storage_key=f"filter-{index}.mp4",
                url=f"/uploads/filter-{index}.mp4",
                size_bytes=10,
                duration_seconds=10,
                expires_at=now() + timedelta(days=1),
            )
            task = Task(
                id=f"filter-task-{index}",
                user_id=user.id,
                tool_slug="remove-subtitle",
                input_asset_id=asset.id,
                status=status,
                params={"internalBatchName": batch_name},
                estimated_credits=1,
                frozen_credits=0,
                charged_credits=0,
                provider="mock",
                provider_job_id=f"filter-provider-{index}",
                error_code="VIDEO_PROCESSING_FAILED" if status == "failed" else None,
                progress_stage="CUDA_OUT_OF_MEMORY" if status == "failed" else "处理完成",
                completed_at=completed_at,
            )
            db.add_all([asset, task])
        db.commit()

        page = paginated_tasks(db, user.id, status="failed")
        completed_page = paginated_tasks(
            db,
            user.id,
            status="succeeded",
            completed_from=(now() - timedelta(days=1)).isoformat(),
            completed_to=(now() + timedelta(days=1)).isoformat(),
            batch_name="仙王开局",
        )

    assert page["page"]["total"] == 2
    assert {task["id"] for task in page["items"]} == {"filter-task-1", "filter-task-3"}
    assert {task["status"] for task in page["items"]} == {"failed"}
    assert completed_page["page"]["total"] == 1
    assert [task["id"] for task in completed_page["items"]] == ["filter-task-2"]


def test_paginated_tasks_filters_internal_batch_queue() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as db:
        user = User(id="user-internal-filter", email="internal-filter@example.com", name="Internal Filter User", role="user", status="active")
        wallet = Wallet(user_id=user.id, credits=100, frozen_credits=0)
        db.add_all([user, wallet])

        rows = [
            ("internal-task", "subtitle-translate-workflow", {"internalBatchId": "batch-1", "internalBatchName": "内部批次"}),
            ("missing-batch-id", "subtitle-translate-workflow", {"internalBatchName": "内部批次"}),
            ("other-tool", "remove-subtitle", {"internalBatchId": "batch-2", "internalBatchName": "其它工具批次"}),
        ]
        for index, (task_id, tool_slug, params) in enumerate(rows, start=1):
            asset = Asset(
                id=f"internal-filter-asset-{index}",
                user_id=user.id,
                kind="video",
                original_name=f"internal-filter-{index}.mp4",
                mime_type="video/mp4",
                storage_key=f"internal-filter-{index}.mp4",
                url=f"/uploads/internal-filter-{index}.mp4",
                size_bytes=10,
                duration_seconds=10,
                expires_at=now() + timedelta(days=1),
            )
            task = Task(
                id=task_id,
                user_id=user.id,
                tool_slug=tool_slug,
                input_asset_id=asset.id,
                status="queued",
                params=params,
                estimated_credits=1,
                frozen_credits=1,
                charged_credits=0,
                provider="mock",
                provider_job_id=f"internal-provider-{index}",
                progress_stage="等待处理",
            )
            db.add_all([asset, task])
        db.commit()

        page = paginated_tasks(db, user.id, internal_batch_only=True)

    assert page["page"]["total"] == 1
    assert [task["id"] for task in page["items"]] == ["internal-task"]


def test_delete_tasks_from_list_hides_selected_tasks() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as db:
        user = User(id="user-delete", email="delete@example.com", name="Delete User", role="user", status="active")
        wallet = Wallet(user_id=user.id, credits=100, frozen_credits=0)
        db.add_all([user, wallet])

        for index in range(1, 4):
            asset = Asset(
                id=f"delete-asset-{index}",
                user_id=user.id,
                kind="video",
                original_name=f"delete-{index}.mp4",
                mime_type="video/mp4",
                storage_key=f"delete-{index}.mp4",
                url=f"/uploads/delete-{index}.mp4",
                size_bytes=10,
                duration_seconds=10,
                expires_at=now() + timedelta(days=1),
            )
            task = Task(
                id=f"delete-task-{index}",
                user_id=user.id,
                tool_slug="remove-subtitle",
                input_asset_id=asset.id,
                status="succeeded",
                params={},
                estimated_credits=1,
                frozen_credits=0,
                charged_credits=0,
                provider="mock",
                provider_job_id=f"delete-provider-{index}",
                progress_stage="处理完成",
                completed_at=now(),
            )
            db.add_all([asset, task])
        db.commit()

        delete_payload = delete_tasks_from_list(db, user.id, ["delete-task-1", "delete-task-2", "missing-task"])
        page = paginated_tasks(db, user.id)
        second_delete_payload = delete_tasks_from_list(db, user.id, ["delete-task-1"])

    assert delete_payload["deleted"] == 2
    assert delete_payload["missing"] == 1
    assert set(delete_payload["taskIds"]) == {"delete-task-1", "delete-task-2"}
    assert page["page"]["total"] == 1
    assert [task["id"] for task in page["items"]] == ["delete-task-3"]
    assert second_delete_payload["deleted"] == 0
    assert second_delete_payload["missing"] == 0


def test_retry_failed_task_single_gpu_marks_exclusive_retry(tmp_path, monkeypatch) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    monkeypatch.setattr(services.settings, "upload_dir", str(tmp_path))
    enqueued: list[str] = []
    monkeypatch.setattr(services, "enqueue_provider_job", enqueued.append)

    with Session(engine) as db:
        user = User(id="user-single-gpu", email="single-gpu@example.com", name="Single GPU User", role="user", status="active")
        wallet = Wallet(user_id=user.id, credits=100, frozen_credits=0)
        asset = Asset(
            id="asset-single-gpu",
            user_id=user.id,
            kind="video",
            original_name="single.mp4",
            mime_type="video/mp4",
            storage_key="single.mp4",
            url="/uploads/single.mp4",
            size_bytes=10,
            duration_seconds=10,
            expires_at=now() + timedelta(days=1),
        )
        task = Task(
            id="task-single-gpu",
            user_id=user.id,
            tool_slug="remove-subtitle",
            input_asset_id=asset.id,
            status="failed",
            params={"modelAdapter": "propainter"},
            estimated_credits=1,
            frozen_credits=0,
            charged_credits=0,
            provider="mock",
            provider_job_id="old-provider-single-gpu",
            error_code="VIDEO_PROCESSING_FAILED",
            output_url="",
            progress_percent=0,
            progress_stage="CUDA_OUT_OF_MEMORY",
            completed_at=now(),
        )
        db.add_all([user, wallet, asset, task])
        db.commit()

        retried = retry_failed_task_single_gpu(db, user.id, task.id)
        wallet_after = db.get(Wallet, user.id)

    assert retried.status == "queued"
    assert retried.provider_job_id != "old-provider-single-gpu"
    assert retried.error_code is None
    assert retried.params["forceSingleGpu"] is True
    assert retried.params["exclusiveGpu"] is True
    assert retried.progress_stage == "等待 worker 领取任务（单卡独占重跑）"
    assert enqueued == ["task-single-gpu"]
    assert wallet_after.frozen_credits == retried.frozen_credits


def test_retry_failed_task_single_gpu_rejects_unreadable_remote_input(tmp_path, monkeypatch) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    monkeypatch.setattr(services.settings, "upload_dir", str(tmp_path))
    monkeypatch.setattr(services, "storage", FakeRemoteStorage())
    enqueued: list[str] = []
    monkeypatch.setattr(services, "enqueue_provider_job", enqueued.append)

    with Session(engine) as db:
        user = User(id="user-single-missing", email="single-missing@example.com", name="Single Missing", role="user", status="active")
        wallet = Wallet(user_id=user.id, credits=100, frozen_credits=0)
        asset = Asset(
            id="asset-single-missing",
            user_id=user.id,
            kind="video",
            original_name="single-missing.mp4",
            mime_type="video/mp4",
            storage_key="single-missing.mp4",
            url="https://tos.example.test/single-missing.mp4",
            size_bytes=10,
            duration_seconds=10,
            expires_at=now() + timedelta(days=1),
        )
        task = Task(
            id="task-single-missing",
            user_id=user.id,
            tool_slug="remove-subtitle",
            input_asset_id=asset.id,
            status="failed",
            params={"modelAdapter": "propainter"},
            estimated_credits=1,
            frozen_credits=0,
            charged_credits=0,
            provider="mock",
            provider_job_id="old-single-missing",
            error_code="VIDEO_PROCESSING_FAILED",
            progress_percent=0,
            progress_stage="failed",
            completed_at=now(),
        )
        db.add_all([user, wallet, asset, task])
        db.commit()

        with pytest.raises(HTTPException) as exc:
            retry_failed_task_single_gpu(db, user.id, task.id)
        task_after = db.get(Task, task.id)
        wallet_after = db.get(Wallet, user.id)

    assert exc.value.status_code == 400
    assert exc.value.detail == services.INPUT_ASSET_REMOTE_MISSING_MESSAGE
    assert enqueued == []
    assert task_after.status == "failed"
    assert task_after.provider_job_id == "old-single-missing"
    assert wallet_after.frozen_credits == 0


def test_internal_batch_zip_splits_large_batches_into_parts(tmp_path, monkeypatch) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    monkeypatch.setattr(services.settings, "upload_dir", str(tmp_path))
    monkeypatch.setattr(services.settings, "internal_batch_zip_part_max_bytes", 25)

    batch_id = "batch-split"
    batch_name = "split batch"

    with Session(engine) as db:
        user = User(id="user-split", email="split@example.com", name="Split User", role="user", status="active")
        wallet = Wallet(user_id=user.id, credits=100, frozen_credits=0)
        db.add_all([user, wallet])

        for index in range(1, 4):
            asset = Asset(
                id=f"split-asset-{index}",
                user_id=user.id,
                kind="video",
                original_name=f"split-{index}.mp4",
                mime_type="video/mp4",
                storage_key=f"split-input-{index}.mp4",
                url=f"/uploads/split-input-{index}.mp4",
                size_bytes=20,
                duration_seconds=10,
                expires_at=now() + timedelta(days=1),
            )
            task = Task(
                id=f"split-task-{index}",
                user_id=user.id,
                tool_slug="subtitle-translate-workflow",
                input_asset_id=asset.id,
                output_asset_id=None,
                status="succeeded",
                params={"internalBatchId": batch_id, "internalBatchName": batch_name},
                estimated_credits=0,
                frozen_credits=0,
                charged_credits=0,
                provider="mock",
                provider_job_id=f"split-provider-{index}",
                output_url="",
                progress_percent=100,
                progress_stage="处理完成",
            )
            db.add_all([asset, task])
            db.flush()
            (tmp_path / services.task_result_output_key(task)).write_bytes(bytes([index]) * 20)
        db.commit()

        archive = create_internal_batch_zip(db, user.id, batch_id)

    assert archive["partCount"] == 3
    assert len(archive["parts"]) == 3
    assert [part["filename"] for part in archive["parts"]] == [
        "split batch-part01-of03.zip",
        "split batch-part02-of03.zip",
        "split batch-part03-of03.zip",
    ]

    for index, part in enumerate(archive["parts"], start=1):
        with zipfile.ZipFile(part["path"]) as zip_file:
            summary = json.loads(zip_file.read("_batch-summary.json"))
            video_names = [name for name in zip_file.namelist() if name.endswith(".mp4")]
            compression_types = {item.compress_type for item in zip_file.infolist()}
        assert summary["partIndex"] == index
        assert summary["partCount"] == 3
        assert summary["includedTaskIds"] == [f"split-task-{index}"]
        assert len(video_names) == 1
        assert compression_types == {zipfile.ZIP_STORED}


def test_internal_batch_zip_lock_prevents_duplicate_tmp_generation(tmp_path, monkeypatch) -> None:
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'batch-lock.db'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    monkeypatch.setattr(services.settings, "upload_dir", str(tmp_path))

    batch_id = "batch-lock"
    batch_name = "lock batch"

    with Session(engine) as db:
        user = User(id="user-lock", email="lock@example.com", name="Lock User", role="user", status="active")
        wallet = Wallet(user_id=user.id, credits=100, frozen_credits=0)
        asset = Asset(
            id="lock-asset",
            user_id=user.id,
            kind="video",
            original_name="lock.mp4",
            mime_type="video/mp4",
            storage_key="lock-input.mp4",
            url="/uploads/lock-input.mp4",
            size_bytes=20,
            duration_seconds=10,
            expires_at=now() + timedelta(days=1),
        )
        task = Task(
            id="lock-task",
            user_id=user.id,
            tool_slug="subtitle-translate-workflow",
            input_asset_id=asset.id,
            output_asset_id=None,
            status="succeeded",
            params={"internalBatchId": batch_id, "internalBatchName": batch_name},
            estimated_credits=0,
            frozen_credits=0,
            charged_credits=0,
            provider="mock",
            provider_job_id="lock-provider",
            output_url="",
            progress_percent=100,
            progress_stage="处理完成",
        )
        db.add_all([user, wallet, asset, task])
        db.flush()
        (tmp_path / services.task_result_output_key(task)).write_bytes(b"fake-video")
        db.commit()

    original_zip_file = zipfile.ZipFile
    zip_creations = 0

    class SlowZipFile(original_zip_file):
        def __init__(self, *args, **kwargs):
            nonlocal zip_creations
            if len(args) > 1 and args[1] == "w":
                zip_creations += 1
                time.sleep(0.2)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(services.zipfile, "ZipFile", SlowZipFile)

    def build_zip() -> dict:
        with Session(engine) as thread_db:
            return create_internal_batch_zip(thread_db, "user-lock", batch_id, part=1)

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: build_zip(), range(2)))

    zip_paths = sorted((tmp_path / "internal-batch-zips").glob("*.zip"))
    tmp_paths = sorted((tmp_path / "internal-batch-zips").glob("*.tmp"))

    assert len(zip_paths) == 1
    assert not tmp_paths
    assert zip_creations == 1
    assert {result["path"] for result in results} == {zip_paths[0]}


def test_internal_batch_zip_manifest_plans_parts_without_creating_archives(tmp_path, monkeypatch) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    monkeypatch.setattr(services.settings, "upload_dir", str(tmp_path))
    monkeypatch.setattr(services.settings, "internal_batch_zip_part_max_bytes", 1_000_000)
    monkeypatch.setattr(services.settings, "internal_batch_zip_part_max_files", 2)

    batch_id = "batch-plan"
    batch_name = "plan batch"

    with Session(engine) as db:
        user = User(id="user-plan", email="plan@example.com", name="Plan User", role="user", status="active")
        wallet = Wallet(user_id=user.id, credits=100, frozen_credits=0)
        db.add_all([user, wallet])

        for index in range(1, 6):
            asset = Asset(
                id=f"plan-asset-{index}",
                user_id=user.id,
                kind="video",
                original_name=f"plan-{index}.mp4",
                mime_type="video/mp4",
                storage_key=f"plan-input-{index}.mp4",
                url=f"/uploads/plan-input-{index}.mp4",
                size_bytes=20,
                duration_seconds=10,
                expires_at=now() + timedelta(days=1),
            )
            task = Task(
                id=f"plan-task-{index}",
                user_id=user.id,
                tool_slug="subtitle-translate-workflow",
                input_asset_id=asset.id,
                output_asset_id=None,
                status="succeeded",
                params={"internalBatchId": batch_id, "internalBatchName": batch_name},
                estimated_credits=0,
                frozen_credits=0,
                charged_credits=0,
                provider="mock",
                provider_job_id=f"plan-provider-{index}",
                output_url="",
                progress_percent=100,
                progress_stage="处理完成",
            )
            db.add_all([asset, task])
            db.flush()
            (tmp_path / services.task_result_output_key(task)).write_bytes(bytes([index]) * 20)
        db.commit()

        manifest = plan_internal_batch_zip(db, user.id, batch_id)

        assert manifest["partCount"] == 3
        assert len(manifest["parts"]) == 3
        assert all(part["sizeBytes"] == 0 for part in manifest["parts"])
        assert not list((tmp_path / "internal-batch-zips").glob("*.zip"))

        archive = create_internal_batch_zip(db, user.id, batch_id, part=2)

    zip_paths = sorted((tmp_path / "internal-batch-zips").glob("*.zip"))
    assert len(zip_paths) == 1
    assert archive["partCount"] == 3
    assert archive["filename"] == "plan batch-part02-of03.zip"
    with zipfile.ZipFile(zip_paths[0]) as zip_file:
        summary = json.loads(zip_file.read("_batch-summary.json"))
        video_names = [name for name in zip_file.namelist() if name.endswith(".mp4")]
    assert summary["partIndex"] == 2
    assert summary["partCount"] == 3
    assert summary["partMaxFiles"] == 2
    assert summary["includedTaskIds"] == ["plan-task-3", "plan-task-4"]
    assert len(video_names) == 2
