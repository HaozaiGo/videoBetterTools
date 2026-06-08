from datetime import timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.models import Asset, Base, Task, User, Wallet
from app.services import now, provider_callback


def test_processing_progress_is_capped_before_result_is_saved() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as db:
        user = User(id="user-progress", email="progress@example.com", name="Progress User", role="user", status="active")
        wallet = Wallet(user_id=user.id, credits=100, frozen_credits=42)
        asset = Asset(
            id="asset-progress",
            user_id=user.id,
            kind="input",
            original_name="input.mp4",
            mime_type="video/mp4",
            storage_key="input.mp4",
            url="https://example.com/input.mp4",
            size_bytes=1024,
            duration_seconds=70,
            expires_at=now() + timedelta(days=1),
        )
        task = Task(
            id="task-progress",
            user_id=user.id,
            tool_slug="translate",
            input_asset_id=asset.id,
            status="processing",
            params={},
            estimated_credits=42,
            frozen_credits=42,
            provider="mock",
            provider_job_id="provider-progress",
            progress_percent=30,
            progress_stage="视频翻译中",
        )
        db.add_all([user, wallet, asset, task])
        db.commit()

        provider_callback(
            db,
            provider_job_id="provider-progress",
            status="processing",
            callback_id="provider-progress:progress:100",
            progress_percent=100,
            progress_stage="视频翻译完成",
        )

        db.refresh(task)
        assert task.status == "processing"
        assert task.progress_percent == 95
        assert task.progress_stage == "远端处理完成，正在回传结果"
