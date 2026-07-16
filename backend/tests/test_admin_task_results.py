from datetime import timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.main import admin_task_result_link_endpoint
from app.models import Asset, Base, Task, User, Wallet
from app.services import now


def test_admin_task_result_link_can_target_batch_user() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as db:
        admin = User(id="admin-result-link", email="admin-result@example.com", name="Admin", role="admin", status="active")
        user = User(id="user-result-link", email="user-result@example.com", name="User", role="user", status="active")
        wallet = Wallet(user_id=user.id, credits=100, frozen_credits=0)
        input_asset = Asset(
            id="asset-input-result-link",
            user_id=user.id,
            kind="video",
            original_name="episode.mp4",
            mime_type="video/mp4",
            storage_key="input.mp4",
            url="/uploads/input.mp4",
            size_bytes=10,
            duration_seconds=10,
            expires_at=now() + timedelta(days=1),
        )
        output_asset = Asset(
            id="asset-output-result-link",
            user_id=user.id,
            kind="result",
            original_name="episode-result.mp4",
            mime_type="video/mp4",
            storage_key="result.mp4",
            url="/uploads/result.mp4",
            size_bytes=10,
            duration_seconds=0,
            expires_at=now() + timedelta(days=1),
        )
        task = Task(
            id="task-result-link",
            user_id=user.id,
            tool_slug="subtitle-translate-workflow",
            input_asset_id=input_asset.id,
            output_asset_id=output_asset.id,
            status="succeeded",
            params={"internalBatchId": "batch-result-link", "internalBatchIndex": 1},
            estimated_credits=1,
            frozen_credits=0,
            charged_credits=1,
            provider="mock",
            provider_job_id="provider-result-link",
            progress_percent=100,
            progress_stage="处理完成",
            completed_at=now(),
        )
        db.add_all([admin, user, wallet, input_asset, output_asset, task])
        db.commit()

        response = admin_task_result_link_endpoint(
            task_id="task-result-link",
            user_id="user-result-link",
            db=db,
            _admin=admin,
        )

    assert response["url"].startswith("/api/tasks/task-result-link/result/")
