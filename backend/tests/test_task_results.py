from datetime import timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import app.services as services
from app.models import Asset, Base, Task, User, Wallet
from app.services import get_task_result_access, get_task_result_url, now


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
