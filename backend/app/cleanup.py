from __future__ import annotations

import argparse
import logging
import shutil
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.config import settings
from app.database import SessionLocal
from app.models import Asset, Task
from app.storage import storage

logger = logging.getLogger("model_plaza.cleanup")

ACTIVE_TASK_STATUSES = {"queued", "processing"}
SKIP_LOCAL_SYNC_DIRS = {".multipart", "internal-batch-zips"}


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _path_mtime(path: Path) -> datetime:
    return datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)


def _is_older_than(path: Path, cutoff: datetime) -> bool:
    try:
        return _path_mtime(path) < cutoff
    except FileNotFoundError:
        return False


def _relative_storage_key(path: Path) -> str | None:
    try:
        return path.relative_to(settings.upload_path).as_posix()
    except ValueError:
        return None


def _remove_path(path: Path, dry_run: bool) -> bool:
    if dry_run:
        return True
    if path.is_dir():
        shutil.rmtree(path, ignore_errors=True)
        return True
    path.unlink(missing_ok=True)
    return True


def cleanup_multipart(cutoff: datetime, dry_run: bool = False) -> int:
    root = settings.upload_path / ".multipart"
    if not root.exists():
        return 0

    removed = 0
    for upload_dir in root.iterdir():
        if not upload_dir.is_dir() or not _is_older_than(upload_dir, cutoff):
            continue
        if _remove_path(upload_dir, dry_run):
            removed += 1
    return removed


def cleanup_internal_batch_zips(cutoff: datetime, dry_run: bool = False) -> int:
    zip_dir = settings.upload_path / "internal-batch-zips"
    if not zip_dir.exists():
        return 0

    removed = 0
    for zip_path in zip_dir.glob("*.zip"):
        if not zip_path.is_file() or not _is_older_than(zip_path, cutoff):
            continue
        if _remove_path(zip_path, dry_run):
            removed += 1
    return removed


def cleanup_synced_local_files(cutoff: datetime, dry_run: bool = False) -> int:
    if not storage.is_remote or not settings.upload_path.exists():
        return 0

    removed = 0
    for path in settings.upload_path.rglob("*"):
        if not path.is_file() or not _is_older_than(path, cutoff):
            continue
        relative_parts = set(path.relative_to(settings.upload_path).parts)
        if relative_parts & SKIP_LOCAL_SYNC_DIRS:
            continue
        storage_key = _relative_storage_key(path)
        if storage_key and storage.remote_exists(storage_key):
            if _remove_path(path, dry_run):
                removed += 1
    return removed


def _asset_is_used_by_active_task(db: Session, asset_id: str) -> bool:
    query = (
        select(Task.id)
        .where(
            Task.status.in_(ACTIVE_TASK_STATUSES),
            or_(Task.input_asset_id == asset_id, Task.output_asset_id == asset_id),
        )
        .limit(1)
    )
    return db.execute(query).first() is not None


def cleanup_expired_assets(db: Session, current_time: datetime, dry_run: bool = False) -> int:
    expired_assets = db.execute(
        select(Asset).where(Asset.expires_at.is_not(None), Asset.expires_at <= current_time)
    ).scalars()

    cleaned = 0
    for asset in expired_assets:
        if _asset_is_used_by_active_task(db, asset.id):
            continue

        changed = False
        if storage.is_remote:
            if storage.remote_exists(asset.storage_key):
                changed = True if dry_run else storage.delete_remote(asset.storage_key) or changed
            if storage.local_path(asset.storage_key).exists():
                changed = True if dry_run else storage.delete_local_copy(asset.storage_key) or changed
        else:
            if storage.local_path(asset.storage_key).exists():
                changed = True if dry_run else storage.delete_local_copy(asset.storage_key)

        if changed:
            cleaned += 1

    return cleaned


def cleanup_once(retention_hours: int | None = None, dry_run: bool = False) -> dict[str, int]:
    retention_hours = retention_hours or settings.cleanup_retention_hours
    current_time = utc_now()
    cutoff = current_time - timedelta(hours=retention_hours)

    stats = {
        "multipart_dirs": cleanup_multipart(cutoff, dry_run=dry_run),
        "internal_batch_zips": cleanup_internal_batch_zips(cutoff, dry_run=dry_run),
        "synced_local_files": cleanup_synced_local_files(cutoff, dry_run=dry_run),
        "expired_assets": 0,
    }
    with SessionLocal() as db:
        stats["expired_assets"] = cleanup_expired_assets(db, current_time, dry_run=dry_run)
    return stats


def run_loop(retention_hours: int | None = None, interval_seconds: int | None = None, dry_run: bool = False) -> None:
    interval_seconds = interval_seconds or settings.cleanup_interval_seconds
    while True:
        try:
            stats = cleanup_once(retention_hours=retention_hours, dry_run=dry_run)
            logger.info("cleanup finished: %s", stats)
        except Exception:
            logger.exception("cleanup failed")
        time.sleep(interval_seconds)


def main() -> None:
    parser = argparse.ArgumentParser(description="Clean expired uploads and local storage cache.")
    parser.add_argument("--loop", action="store_true", help="Run cleanup repeatedly.")
    parser.add_argument("--retention-hours", type=int, default=settings.cleanup_retention_hours)
    parser.add_argument("--interval-seconds", type=int, default=settings.cleanup_interval_seconds)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    if args.loop:
        run_loop(retention_hours=args.retention_hours, interval_seconds=args.interval_seconds, dry_run=args.dry_run)
        return

    stats = cleanup_once(retention_hours=args.retention_hours, dry_run=args.dry_run)
    logger.info("cleanup finished: %s", stats)


if __name__ == "__main__":
    main()
