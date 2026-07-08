from pathlib import Path

from app import worker


class FakeStorage:
    is_remote = True

    def __init__(self) -> None:
        self.saved: list[tuple[str, Path]] = []
        self.deleted: list[str] = []

    def save_file(self, storage_key: str, local_path: Path):
        self.saved.append((storage_key, local_path))

        class Stored:
            size = local_path.stat().st_size
            public_url = f"https://cdn.example.test/{storage_key}"

            def __init__(self, key: str) -> None:
                self.storage_key = key

        return Stored(storage_key)

    def delete_local_copy(self, storage_key: str) -> bool:
        self.deleted.append(storage_key)
        return True


def test_finalize_result_payload_uploads_local_file(monkeypatch, tmp_path) -> None:
    output = tmp_path / "result.mp4"
    output.write_bytes(b"video")
    fake_storage = FakeStorage()
    monkeypatch.setattr(worker, "storage", fake_storage)

    finalized = worker._finalize_result_payload(
        {
            "storage_key": "model-plaza/output/videos/result.mp4",
            "local_path": str(output),
            "mime_type": "video/mp4",
        }
    )

    assert finalized == {
        "storage_key": "model-plaza/output/videos/result.mp4",
        "url": "https://cdn.example.test/model-plaza/output/videos/result.mp4",
        "mime_type": "video/mp4",
        "size_bytes": 5,
    }
    assert fake_storage.saved == [("model-plaza/output/videos/result.mp4", output)]
    assert fake_storage.deleted == ["model-plaza/output/videos/result.mp4"]


def test_finalize_remote_gpu_result_uses_direct_upload_metadata(monkeypatch) -> None:
    monkeypatch.setattr(worker, "_sync_remote_gpu_progress", lambda *args, **kwargs: True)
    monkeypatch.setattr(
        worker,
        "get_remote_video_job",
        lambda job_id: {
            "status": "succeeded",
            "result_storage_key": "model-plaza/output/videos/result.mp4",
            "result_url": "https://cdn.example.test/model-plaza/output/videos/result.mp4",
            "result_mime_type": "video/mp4",
            "result_size_bytes": 123,
        },
    )

    finalized = worker._finalize_remote_gpu_result(
        "task-1",
        "provider-1",
        {
            "remote_job_id": "remote-1",
            "storage_key": "model-plaza/output/videos/result.mp4",
            "url": "https://cdn.example.test/model-plaza/output/videos/result.mp4",
        },
    )

    assert finalized == {
        "storage_key": "model-plaza/output/videos/result.mp4",
        "url": "https://cdn.example.test/model-plaza/output/videos/result.mp4",
        "mime_type": "video/mp4",
        "size_bytes": 123,
    }
