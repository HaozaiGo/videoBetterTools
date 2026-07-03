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

    assert payload["page"]["total"] == 1
    item = payload["items"][0]
    assert item["batchId"] == "zip-batch"
    assert item["batchName"] == "后台 ZIP 测试"
    assert item["source"] == "local"
    assert item["sizeBytes"] > 0
    assert item["downloadUrl"] == "/api/admin/internal-batch-zips/zip-batch/download?userId=zip-user&part=1"
