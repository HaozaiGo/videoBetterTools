from __future__ import annotations

import os
from datetime import timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import app.cleanup as cleanup
from app.models import Asset, Base, Task, User


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


def test_internal_batch_zip_cleanup_defaults_to_twelve_hours(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(cleanup.settings, "upload_dir", str(tmp_path))
    monkeypatch.setattr(cleanup.settings, "internal_batch_zip_retention_hours", 12)

    old_zip = tmp_path / "internal-batch-zips" / "old.zip"
    old_zip.parent.mkdir()
    old_zip.write_bytes(b"zip")
    _age_path(old_zip, 13 * 60 * 60)

    fresh_zip = tmp_path / "internal-batch-zips" / "fresh.zip"
    fresh_zip.write_bytes(b"zip")
    _age_path(fresh_zip, 11 * 60 * 60)

    assert cleanup.cleanup_internal_batch_zips() == 1
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


def test_disk_pressure_cleanup_removes_old_synced_files_but_keeps_active_assets(tmp_path, monkeypatch) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    monkeypatch.setattr(cleanup.settings, "upload_dir", str(tmp_path))
    monkeypatch.setattr(cleanup.settings, "cleanup_disk_high_watermark_percent", 80)
    monkeypatch.setattr(cleanup.settings, "cleanup_disk_low_watermark_percent", 75)
    monkeypatch.setattr(cleanup.settings, "cleanup_disk_min_age_hours", 1)
    monkeypatch.setattr(cleanup, "storage", FakeRemoteStorage({"old.mp4", "active.mp4"}))

    old_file = tmp_path / "old.mp4"
    old_file.write_bytes(b"old")
    _age_path(old_file, 2 * 60 * 60)

    active_file = tmp_path / "active.mp4"
    active_file.write_bytes(b"active")
    _age_path(active_file, 2 * 60 * 60)

    fresh_file = tmp_path / "fresh.mp4"
    fresh_file.write_bytes(b"fresh")
    _age_path(fresh_file, 30 * 60)

    usage_values = iter([85, 74])
    monkeypatch.setattr(cleanup, "_disk_used_percent", lambda path: next(usage_values, 74))

    with Session(engine) as db:
        user = User(id="cleanup-user", email="cleanup@example.com", name="Cleanup User", role="user", status="active")
        active_asset = Asset(
            id="active-asset",
            user_id=user.id,
            kind="video",
            original_name="active.mp4",
            mime_type="video/mp4",
            storage_key="active.mp4",
            url="https://example.com/active.mp4",
            size_bytes=6,
            duration_seconds=1,
            expires_at=None,
        )
        task = Task(
            id="active-task",
            user_id=user.id,
            tool_slug="remove-subtitle",
            input_asset_id=active_asset.id,
            status="processing",
            params={},
            estimated_credits=1,
            frozen_credits=1,
            provider="mock",
            provider_job_id="provider-cleanup",
        )
        db.add_all([user, active_asset, task])
        db.commit()

        assert cleanup.cleanup_disk_pressure(db, cleanup.utc_now()) == 1

    assert not old_file.exists()
    assert active_file.exists()
    assert fresh_file.exists()
