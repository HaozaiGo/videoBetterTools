import asyncio

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import app.services as services
from app.models import Base, User


class FakeUploadFile:
    filename = "clip.mp4"
    content_type = "video/mp4"

    async def read(self) -> bytes:
        return b"video"


class AssertNoOpenTransactionStorage:
    is_remote = True

    def __init__(self, db: Session) -> None:
        self.db = db

    def save_bytes(self, storage_key: str, content: bytes) -> None:
        assert not self.db.in_transaction()

    def public_url(self, storage_key: str) -> str:
        return f"https://tos.example.test/{storage_key}"


def test_save_upload_releases_db_transaction_before_slow_storage_write(monkeypatch) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as db:
        user = User(id="user-upload", email="upload@example.com", name="Upload User", role="user", status="active")
        db.add(user)
        db.commit()
        db.get(User, user.id)
        assert db.in_transaction()

        monkeypatch.setattr(services, "storage", AssertNoOpenTransactionStorage(db))

        asset = asyncio.run(services.save_upload(db, user.id, FakeUploadFile(), kind="video", duration_seconds=12))

        assert asset.user_id == user.id
        assert asset.size_bytes == 5
