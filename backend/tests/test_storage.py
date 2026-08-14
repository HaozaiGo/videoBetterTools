from pathlib import Path

import pytest

from app.storage import StorageUploadUnavailableError, StoredObject, TosStorage


class FakeHeadResult:
    content_length = 5


class FakeTosClient:
    def __init__(self) -> None:
        self.head_calls: list[tuple[str, str]] = []

    def put_object_from_file(self, bucket: str, key: str, local_path: str) -> None:
        raise RuntimeError("AccessDenied: Access Denied because object protected by object lock.")

    def head_object(self, bucket: str, key: str) -> FakeHeadResult:
        self.head_calls.append((bucket, key))
        return FakeHeadResult()


class TimeoutTosClient:
    def __init__(self) -> None:
        self.put_calls = 0

    def put_object_from_file(self, bucket: str, key: str, local_path: str) -> None:
        self.put_calls += 1
        raise TimeoutError("upload stalled")


def test_tos_save_file_treats_existing_locked_same_size_object_as_success(tmp_path: Path) -> None:
    local_path = tmp_path / "clip.mp4"
    local_path.write_bytes(b"video")

    storage = object.__new__(TosStorage)
    storage.client = FakeTosClient()
    storage.bucket = "bucket"
    storage.public_base_url = "https://bucket.example.test"

    stored = storage.save_file("model-plaza/input/videos/clip.mp4", local_path)

    assert stored == StoredObject(
        storage_key="model-plaza/input/videos/clip.mp4",
        public_url="https://bucket.example.test/model-plaza/input/videos/clip.mp4",
        size=5,
    )
    assert storage.client.head_calls == [("bucket", "model-plaza/input/videos/clip.mp4")]


def test_tos_save_file_wraps_upload_timeouts_as_transient(tmp_path: Path, monkeypatch) -> None:
    local_path = tmp_path / "clip.mp4"
    local_path.write_bytes(b"video")

    storage = object.__new__(TosStorage)
    storage.client = TimeoutTosClient()
    storage.bucket = "bucket"
    storage.public_base_url = "https://bucket.example.test"

    monkeypatch.setattr("app.storage.settings.volcengine_tos_upload_timeout_seconds", 1)
    monkeypatch.setattr("app.storage.settings.volcengine_tos_upload_retry_max", 2)
    monkeypatch.setattr("app.storage.settings.volcengine_tos_upload_retry_interval_seconds", 1)
    monkeypatch.setattr("app.storage.time.sleep", lambda seconds: None)

    with pytest.raises(StorageUploadUnavailableError, match="TOS upload timed out"):
        storage.save_file("model-plaza/input/videos/clip.mp4", local_path)

    assert storage.client.put_calls == 2
