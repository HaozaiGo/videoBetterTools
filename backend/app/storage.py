from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote
import shutil
import urllib.request

from app.config import settings


@dataclass(frozen=True)
class StoredObject:
    storage_key: str
    public_url: str
    size: int


def safe_storage_name(name: str) -> str:
    return Path(name or "upload.bin").name.replace("/", "-").replace("\\", "-")


def content_disposition_for_download(filename: str) -> str:
    safe_name = safe_storage_name(filename)
    quoted_name = quote(safe_name)
    ascii_fallback = "".join(char if char.isascii() and char not in {'"', "\\", ";"} else "_" for char in safe_name)
    ascii_fallback = ascii_fallback or "download.bin"
    return f"attachment; filename=\"{ascii_fallback}\"; filename*=UTF-8''{quoted_name}"


def object_key_for_upload(asset_id: str, kind: str, original_name: str) -> str:
    now = datetime.now(timezone.utc)
    folder = "videos" if kind == "video" else "images" if kind == "image" else "files"
    return f"model-plaza/input/{folder}/{now:%Y/%m/%d}/{asset_id}-{safe_storage_name(original_name)}"


class LocalStorage:
    def __init__(self) -> None:
        self.root = settings.upload_path
        self.root.mkdir(parents=True, exist_ok=True)

    @property
    def is_remote(self) -> bool:
        return False

    def local_path(self, storage_key: str) -> Path:
        return self.root / storage_key

    def public_url(self, storage_key: str) -> str:
        return f"{settings.public_upload_prefix}/{storage_key}"

    def presign_download(self, storage_key: str, filename: str | None = None) -> str:
        return self.public_url(storage_key)

    def save_bytes(self, storage_key: str, content: bytes) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        path = self.local_path(storage_key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)

    def save_file(self, storage_key: str, local_path: Path) -> StoredObject:
        target = self.local_path(storage_key)
        target.parent.mkdir(parents=True, exist_ok=True)
        if local_path.resolve() != target.resolve():
            shutil.copyfile(local_path, target)
        return StoredObject(storage_key=storage_key, public_url=self.public_url(storage_key), size=target.stat().st_size)

    def delete_local_copy(self, storage_key: str) -> bool:
        path = self.local_path(storage_key)
        if not path.exists():
            return False
        if path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
            return True
        path.unlink(missing_ok=True)
        return True

    def remote_exists(self, storage_key: str) -> bool:
        return self.local_path(storage_key).exists()

    def delete_remote(self, storage_key: str) -> bool:
        return False

    def write_text(self, storage_key: str, content: str) -> None:
        path = self.local_path(storage_key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    def ensure_local(self, storage_key: str) -> Path:
        path = self.local_path(storage_key)
        if not path.exists():
            raise FileNotFoundError(storage_key)
        return path

    def presign_upload(self, kind: str = "video", duration_seconds: int = 0, storage_key: str | None = None) -> dict:
        return {
            "mode": "local-form",
            "method": "POST",
            "uploadUrl": "/api/assets",
            "fields": {
                "kind": kind,
                "durationSeconds": duration_seconds,
            },
        }


class TosStorage(LocalStorage):
    def __init__(
        self,
        ak: str,
        sk: str,
        endpoint: str,
        region: str,
        bucket: str,
        public_base_url: str,
    ) -> None:
        super().__init__()
        import tos

        self.client = tos.TosClientV2(ak, sk, endpoint, region)
        self.bucket = bucket
        self.public_base_url = public_base_url.rstrip("/")

    @property
    def is_remote(self) -> bool:
        return True

    def public_url(self, storage_key: str) -> str:
        encoded_key = quote(storage_key.strip("/"), safe="/")
        return f"{self.public_base_url}/{encoded_key}"

    def save_bytes(self, storage_key: str, content: bytes) -> None:
        super().save_bytes(storage_key, content)
        self.save_file(storage_key, self.local_path(storage_key))

    def save_file(self, storage_key: str, local_path: Path) -> StoredObject:
        normalized_key = storage_key.strip("/")
        self.client.put_object_from_file(self.bucket, normalized_key, str(local_path))
        return StoredObject(storage_key=normalized_key, public_url=self.public_url(normalized_key), size=local_path.stat().st_size)

    def remote_exists(self, storage_key: str) -> bool:
        try:
            self.client.head_object(self.bucket, storage_key.strip("/"))
        except Exception:
            return False
        return True

    def delete_remote(self, storage_key: str) -> bool:
        try:
            self.client.delete_object(self.bucket, storage_key.strip("/"))
        except Exception:
            return False
        return True

    def write_text(self, storage_key: str, content: str) -> None:
        super().write_text(storage_key, content)
        self.save_file(storage_key, self.local_path(storage_key))

    def ensure_local(self, storage_key: str) -> Path:
        path = self.local_path(storage_key)
        if path.exists():
            return path
        path.parent.mkdir(parents=True, exist_ok=True)
        with urllib.request.urlopen(self.presign_download(storage_key), timeout=600) as response:
            with path.open("wb") as output_file:
                shutil.copyfileobj(response, output_file)
        return path

    def presign_download(self, storage_key: str, filename: str | None = None) -> str:
        from tos.enum import HttpMethodType

        query = None
        if filename:
            query = {"response-content-disposition": content_disposition_for_download(filename)}
        signed = self.client.pre_signed_url(
            HttpMethodType.Http_Method_Get,
            self.bucket,
            storage_key.strip("/"),
            expires=settings.volcengine_tos_presign_expires_seconds,
            query=query,
        )
        return signed.signed_url

    def presign_upload(self, kind: str = "video", duration_seconds: int = 0, storage_key: str | None = None) -> dict:
        if not storage_key:
            return super().presign_upload(kind, duration_seconds)
        from tos.enum import HttpMethodType

        signed = self.client.pre_signed_url(
            HttpMethodType.Http_Method_Put,
            self.bucket,
            storage_key.strip("/"),
            expires=settings.volcengine_tos_presign_expires_seconds,
        )
        headers = {key: value for key, value in signed.signed_header.items() if key.lower() != "host"}
        return {
            "mode": "tos-put",
            "method": "PUT",
            "uploadUrl": signed.signed_url,
            "headers": headers,
            "fields": {
                "kind": kind,
                "durationSeconds": duration_seconds,
                "storageKey": storage_key.strip("/"),
                "publicUrl": self.public_url(storage_key),
            },
        }


def build_storage() -> LocalStorage:
    tos_ak = settings.volcengine_tos_ak or settings.volcengine_openapi_ak
    tos_sk = settings.volcengine_tos_sk or settings.volcengine_openapi_sk
    explicit_tos = settings.storage_backend.lower() == "tos"
    tos_enabled = explicit_tos or bool(settings.volcengine_tos_bucket and tos_ak and tos_sk)
    if not tos_enabled:
        return LocalStorage()
    missing = [
        name
        for name, value in {
            "VOLCENGINE_TOS_BUCKET": settings.volcengine_tos_bucket,
            "VOLCENGINE_TOS_AK or VOLCENGINE_OPENAPI_AK": tos_ak,
            "VOLCENGINE_TOS_SK or VOLCENGINE_OPENAPI_SK": tos_sk,
        }.items()
        if not value
    ]
    if missing:
        raise RuntimeError(f"TOS storage is enabled but missing: {', '.join(missing)}")
    public_base_url = settings.volcengine_tos_public_base_url or f"https://{settings.volcengine_tos_bucket}.{settings.volcengine_tos_endpoint}"
    return TosStorage(
        tos_ak,
        tos_sk,
        settings.volcengine_tos_endpoint,
        settings.volcengine_tos_region,
        settings.volcengine_tos_bucket,
        public_base_url,
    )


storage = build_storage()
