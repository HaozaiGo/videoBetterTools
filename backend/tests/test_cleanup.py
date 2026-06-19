from __future__ import annotations

import os
from datetime import timedelta

import app.cleanup as cleanup


class FakeRemoteStorage:
    is_remote = True

    def __init__(self, remote_keys: set[str]) -> None:
        self.remote_keys = remote_keys

    def remote_exists(self, storage_key: str) -> bool:
        return storage_key in self.remote_keys


def _age_path(path, seconds: int) -> None:
    timestamp = cleanup.utc_now().timestamp() - seconds
    os.utime(path, (timestamp, timestamp))


def test_cleanup_removes_old_multipart_and_batch_zips(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(cleanup.settings, "upload_dir", str(tmp_path))
    cutoff = cleanup.utc_now() - timedelta(hours=72)

    old_upload = tmp_path / ".multipart" / "old-upload"
    old_upload.mkdir(parents=True)
    (old_upload / "manifest.json").write_text("{}", encoding="utf-8")
    _age_path(old_upload, 73 * 60 * 60)

    fresh_upload = tmp_path / ".multipart" / "fresh-upload"
    fresh_upload.mkdir()
    (fresh_upload / "manifest.json").write_text("{}", encoding="utf-8")

    old_zip = tmp_path / "internal-batch-zips" / "old.zip"
    old_zip.parent.mkdir()
    old_zip.write_bytes(b"zip")
    _age_path(old_zip, 73 * 60 * 60)

    fresh_zip = tmp_path / "internal-batch-zips" / "fresh.zip"
    fresh_zip.write_bytes(b"zip")

    assert cleanup.cleanup_multipart(cutoff) == 1
    assert cleanup.cleanup_internal_batch_zips(cutoff) == 1
    assert not old_upload.exists()
    assert fresh_upload.exists()
    assert not old_zip.exists()
    assert fresh_zip.exists()


def test_cleanup_synced_local_files_only_removes_remote_confirmed_files(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(cleanup.settings, "upload_dir", str(tmp_path))
    monkeypatch.setattr(cleanup, "storage", FakeRemoteStorage({"result.mp4"}))
    cutoff = cleanup.utc_now() - timedelta(hours=72)

    synced = tmp_path / "result.mp4"
    synced.write_bytes(b"synced")
    _age_path(synced, 73 * 60 * 60)

    local_only = tmp_path / "local-only.mp4"
    local_only.write_bytes(b"local")
    _age_path(local_only, 73 * 60 * 60)

    skipped_zip = tmp_path / "internal-batch-zips" / "result.mp4"
    skipped_zip.parent.mkdir()
    skipped_zip.write_bytes(b"zip")
    _age_path(skipped_zip, 73 * 60 * 60)

    assert cleanup.cleanup_synced_local_files(cutoff) == 1
    assert not synced.exists()
    assert local_only.exists()
    assert skipped_zip.exists()
