from pathlib import Path

from app.config import settings


class LocalStorage:
    def __init__(self) -> None:
        self.root = settings.upload_path
        self.root.mkdir(parents=True, exist_ok=True)

    def public_url(self, storage_key: str) -> str:
        return f"{settings.public_upload_prefix}/{storage_key}"

    def save_bytes(self, storage_key: str, content: bytes) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / storage_key).write_bytes(content)

    def write_text(self, storage_key: str, content: str) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / storage_key).write_text(content, encoding="utf-8")

    def presign_upload(self, kind: str = "video", duration_seconds: int = 0) -> dict:
        return {
            "mode": "local-form",
            "method": "POST",
            "uploadUrl": "/api/assets",
            "fields": {
                "kind": kind,
                "durationSeconds": duration_seconds,
            },
        }


storage = LocalStorage()
