from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import json
import logging
from pathlib import Path
import re
import shutil
import threading
import urllib.error
import urllib.request
from urllib.parse import quote
from uuid import uuid4
import zipfile

try:
    import fcntl
except ImportError:  # pragma: no cover - production runs on Linux, tests may run elsewhere.
    fcntl = None

from fastapi import HTTPException, UploadFile
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from app.auth import hash_password
from app.config import settings
from app.models import Asset, ProcessedCallback, Task, User, Wallet, WalletLedger
from app.pricing import estimate_credits
from app.queue import enqueue_internal_batch_zip, enqueue_provider_job
from app.storage import object_key_for_upload, safe_storage_name, storage
from app.tool_config import CATEGORIES, TOOLS, get_tool
from app.video.gptproto import VIDEO_REDRAW_MODELS, normalize_video_redraw_model

DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 100
INTERNAL_BATCH_ZIP_SUMMARY_NAME = "_batch-summary.json"
_INTERNAL_BATCH_ZIP_LOCKS: dict[str, threading.Lock] = {}
_INTERNAL_BATCH_ZIP_LOCKS_GUARD = threading.Lock()
logger = logging.getLogger("model_plaza.services")
INPUT_ASSET_REMOTE_MISSING_ERROR_CODE = "INPUT_ASSET_REMOTE_MISSING"
INPUT_ASSET_REMOTE_MISSING_MESSAGE = "输入视频对象存储不可读，请重新上传后重试"
EPISODE_TOTAL_PATTERN = re.compile(r"[（(]?\s*(\d{1,4})\s*集\s*[）)]?")


def now() -> datetime:
    return datetime.now(timezone.utc)


def asset_expires_at() -> datetime:
    return now() + timedelta(hours=settings.asset_retention_hours)


def is_expired(value: datetime | None) -> bool:
    if value is None:
        return False
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value <= now()


def input_asset_is_remote_readable(input_asset: Asset) -> bool:
    if not storage.is_remote:
        return True
    if input_asset.url and not input_asset.url.startswith(("http://", "https://")):
        return True
    return storage.remote_exists(input_asset.storage_key)


def ensure_input_asset_remote_readable(input_asset: Asset) -> None:
    if not input_asset_is_remote_readable(input_asset):
        raise HTTPException(status_code=400, detail=INPUT_ASSET_REMOTE_MISSING_MESSAGE)


def public_url(storage_key: str) -> str:
    return storage.public_url(storage_key)


def serialize_datetime(value: datetime | None) -> int | None:
    if value is None:
        return None
    return int(value.timestamp() * 1000)


def _positive_int(value: object) -> int | None:
    try:
        parsed = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def internal_batch_expected_total_from_name(batch_name: str) -> int | None:
    match = EPISODE_TOTAL_PATTERN.search(batch_name or "")
    return _positive_int(match.group(1)) if match else None


def internal_batch_expected_total_from_tasks(tasks: list[Task]) -> int:
    declared_totals: list[int] = []
    for task in tasks:
        params = task.params if isinstance(task.params, dict) else {}
        value = _positive_int(params.get("internalBatchTotal"))
        if value is not None:
            declared_totals.append(value)
        name_total = internal_batch_expected_total_from_name(str(params.get("internalBatchName") or ""))
        if name_total is not None:
            declared_totals.append(name_total)
    return max([len(tasks), *declared_totals])


def ensure_demo_user(db: Session) -> None:
    user = db.get(User, settings.demo_user_id)
    if user is None:
        db.add(User(id=settings.demo_user_id, email="demo@modelplaza.local", name="演示用户", role="admin", password_hash=hash_password(settings.demo_user_password)))
        db.add(Wallet(user_id=settings.demo_user_id, credits=180, frozen_credits=0))
        db.commit()
    else:
        changed = False
        if user.role != "admin":
            user.role = "admin"
            changed = True
        if not user.password_hash:
            user.password_hash = hash_password(settings.demo_user_password)
            changed = True
        if changed:
            db.commit()


def create_user(db: Session, email: str, password: str, name: str, role: str = "user", initial_credits: int = 0) -> User:
    existing = db.execute(select(User).where(User.email == email)).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(status_code=409, detail="email already exists")
    user = User(
        id=str(uuid4()),
        email=email,
        name=name,
        role=role,
        password_hash=hash_password(password),
    )
    db.add(user)
    db.add(Wallet(user_id=user.id, credits=max(0, initial_credits), frozen_credits=0))
    if initial_credits > 0:
        db.add(
            WalletLedger(
                id=str(uuid4()),
                user_id=user.id,
                type="recharge",
                amount=initial_credits,
                title="初始积分",
                task_id=None,
            )
        )
    db.commit()
    db.refresh(user)
    return user


def get_wallet(db: Session, user_id: str, lock: bool = False) -> Wallet:
    stmt = select(Wallet).where(Wallet.user_id == user_id)
    if lock:
        stmt = stmt.with_for_update()
    wallet = db.execute(stmt).scalar_one_or_none()
    if wallet is None:
        raise HTTPException(status_code=404, detail="wallet not found")
    return wallet


def add_ledger(db: Session, user_id: str, ledger_type: str, amount: int, title: str, task_id: str | None = None) -> None:
    db.add(
        WalletLedger(
            id=str(uuid4()),
            user_id=user_id,
            type=ledger_type,
            amount=amount,
            title=title,
            task_id=task_id,
        )
    )


def asset_to_dict(asset: Asset) -> dict:
    return {
        "id": asset.id,
        "userId": asset.user_id,
        "kind": asset.kind,
        "originalName": asset.original_name,
        "mimeType": asset.mime_type,
        "storageKey": asset.storage_key,
        "url": asset.url,
        "sizeBytes": asset.size_bytes,
        "durationSeconds": asset.duration_seconds,
        "width": asset.width,
        "height": asset.height,
        "expiresAt": serialize_datetime(asset.expires_at),
        "createdAt": serialize_datetime(asset.created_at),
    }


def task_result_missing_reason(task: Task) -> str:
    if task.status != "succeeded":
        return ""
    preview_path = task_preview_path(task)
    if preview_path.exists() and preview_path.is_file():
        return ""
    output_asset = getattr(task, "output_asset", None)
    if output_asset is None:
        return "结果 Asset 记录缺失"
    if not output_asset.storage_key:
        return "结果对象存储 key 缺失"
    if getattr(storage, "is_remote", False):
        remote_exists = getattr(storage, "remote_exists", None)
        if not callable(remote_exists) or remote_exists(output_asset.storage_key):
            return ""
        return "结果对象存储文件不存在，可能已过期清理"
    local_path_for_key = getattr(storage, "local_path", None)
    if not callable(local_path_for_key):
        return ""
    local_path = local_path_for_key(output_asset.storage_key)
    return "" if local_path.exists() and local_path.is_file() else "结果本地文件不存在，可能已过期清理"


def task_to_dict(task: Task, verify_result: bool = False) -> dict:
    preview_path = task_preview_path(task)
    input_asset = getattr(task, "input_asset", None)
    result_missing_reason = task_result_missing_reason(task) if verify_result else ""
    has_result_access = bool((preview_path.exists() and preview_path.is_file()) or (task.output_asset_id and not result_missing_reason))
    return {
        "id": task.id,
        "userId": task.user_id,
        "toolSlug": task.tool_slug,
        "inputAssetId": task.input_asset_id,
        "inputAssetName": input_asset.original_name if input_asset else "",
        "outputAssetId": task.output_asset_id,
        "status": task.status,
        "params": task.params,
        "estimatedCredits": task.estimated_credits,
        "frozenCredits": task.frozen_credits,
        "chargedCredits": task.charged_credits,
        "provider": task.provider,
        "providerJobId": task.provider_job_id,
        "errorCode": task.error_code,
        "failureReason": failure_reason_for_task(task),
        "progressPercent": task.progress_percent,
        "progressStage": task.progress_stage,
        "createdAt": serialize_datetime(task.created_at),
        "completedAt": serialize_datetime(task.completed_at),
        "outputUrl": task.output_url,
        "previewUrl": f"/api/tasks/{task.id}/preview-result" if has_result_access else "",
        "resultMissing": bool(result_missing_reason),
        "resultMissingReason": result_missing_reason,
    }


def failure_reason_for_task(task: Task) -> str:
    if task.status != "failed":
        return ""
    stage = task.progress_stage or ""
    haystack = f"{task.error_code or ''}\n{stage}".lower()
    if "input_asset_not_found" in haystack or "input video file not found" in haystack:
        return "输入文件不存在或已过期，请重新上传后再试。"
    if "cuda_out_of_memory" in haystack or "cuda out of memory" in haystack or "outofmemoryerror" in haystack:
        return "GPU 显存不足导致模型退出。建议点击“单卡重跑”，或降低并发后重试。"
    if "result_upload_timeout" in haystack or "result upload exceeded total timeout" in haystack:
        return "结果文件已生成，但上传对象存储超时。可以直接重跑；若频繁出现，需要放宽上传超时或检查 TOS 上传链路。"
    if "result_upload_failed" in haystack or "tos upload failed" in haystack or "presigned upload failed" in haystack:
        return "结果上传对象存储失败，请检查 TOS/预签名上传链路后重试。"
    if "gpu_stalled" in haystack or "stalled_progress" in haystack or "stalled progress" in haystack:
        return "远端 GPU 任务长时间无进度，已被自动熔断，可重跑。"
    if "video_decode_failed" in haystack or "moov atom not found" in haystack or "invalid data found" in haystack:
        return "视频解码失败，可能是素材损坏或编码不兼容。"
    if "propainter command failed" in haystack:
        return "远端 ProPainter 去字幕模型执行失败。建议优先使用“单卡重跑”；如果仍失败，再降低并发或调整模型参数。"
    if "translate command failed" in haystack:
        return "远端翻译/字幕生成链路失败。可重跑；如果多次失败，需检查翻译模型或上传链路日志。"
    if task.error_code == "GPTPROTO_VIDEO_REDRAW_FAILED":
        if "video height should not be less than 700" in haystack or "height should not be less than 700" in haystack:
            return "Pike 视频转绘要求输入视频高度在 700-2160px 之间。当前素材分辨率过低，请先转成 720p 或更高清晰度后再提交。"
        if "aspect ratio must be specified" in haystack:
            return "Pike 视频转绘缺少输出画幅参数，请选择 16:9、9:16 或 1:1 后重试。"
        if "video can not be null" in haystack or "video cannot be null" in haystack:
            return "Pike 视频转绘没有收到有效视频地址，请重新上传视频后再试。"
        if "content policy" in haystack or "input may not meet the guidelines" in haystack:
            return "Pike 供应商安全审核未通过，请调整视频内容或提示词后重试。"
        if "handshake operation timed out" in haystack or "timed out" in haystack or "timeout" in haystack:
            return "本地连接 GPTProto 查询结果超时，供应商任务可能仍在生成。请稍后重试；如果频繁出现，需要检查本地网络或代理到 GPTProto 的连接稳定性。"
        if "no channel found" in haystack or "channel configuration" in haystack:
            return "GPTProto 未给当前账号/模型配置可用通道。请确认使用文档中的模型名，或联系 GPTProto 管理员开通对应模型通道后再试。"
        if "http 429" in haystack:
            return "GPTProto 视频转绘触发 429 限流或通道不可用。请稍后重试，或联系 GPTProto 确认该模型通道状态。"
        return "Pike 视频转绘失败。请检查视频是否可访问、分辨率/时长是否符合要求，或查看供应商返回的原始错误后重试。"
    if task.error_code == "VIDEO_PROCESSING_FAILED":
        return "远端视频处理失败，可能是模型报错、显存不足、视频编码不兼容或网络传输中断。"
    if task.error_code == "PROVIDER_FAILED":
        return "供应商返回失败。"
    if task.error_code == "MANUAL_TEST_FAILED":
        return "手动触发的失败回调，用于验证退款流程。"
    return "任务失败，系统已释放冻结积分。"


def task_result_output_key(task: Task) -> str:
    suffix_by_tool = {
        "remove-watermark": "watermark-removed",
        "remove-subtitle": "subtitle-removed",
        "enhance": "enhanced",
        "translate": "translated",
        "video-redraw": "redraw",
        "subtitle-translate-workflow": "translated",
    }
    suffix = suffix_by_tool.get(task.tool_slug, "result")
    return f"{task.id}-{suffix}.mp4"


def task_result_download_name(task: Task) -> str:
    input_asset = getattr(task, "input_asset", None)
    original_name = input_asset.original_name if input_asset else task.id
    base_name = Path(original_name or task.id).stem or task.id
    return safe_storage_name(f"{base_name}-{task.id[:8]}.mp4")


def task_preview_path(task: Task) -> Path:
    return settings.upload_path / task_result_output_key(task)


def get_task_preview_path(db: Session, user_id: str, task_id: str) -> Path:
    task = db.execute(select(Task).where(Task.id == task_id, Task.user_id == user_id)).scalar_one_or_none()
    if task is None:
        raise HTTPException(status_code=404, detail="task not found")
    preview_path = task_preview_path(task)
    if not preview_path.exists() or not preview_path.is_file():
        raise HTTPException(status_code=404, detail="preview result not ready")
    return preview_path


def get_task_result_access(db: Session, user_id: str, task_id: str) -> dict:
    task = db.execute(select(Task).where(Task.id == task_id, Task.user_id == user_id)).scalar_one_or_none()
    if task is None:
        raise HTTPException(status_code=404, detail="task not found")
    filename = task_result_download_name(task)
    preview_path = task_preview_path(task)
    if preview_path.exists() and preview_path.is_file():
        return {"mode": "file", "path": preview_path, "filename": filename}
    if task.output_asset_id:
        output_asset = db.get(Asset, task.output_asset_id)
        if output_asset is not None and output_asset.storage_key:
            if not storage.is_remote:
                try:
                    output_path = storage.ensure_local(output_asset.storage_key)
                except FileNotFoundError as exc:
                    raise HTTPException(status_code=404, detail="result not ready") from exc
                return {
                    "mode": "file",
                    "path": output_path,
                    "filename": filename,
                    "mime_type": output_asset.mime_type or "application/octet-stream",
                }
            return {"mode": "redirect", "url": storage.presign_download(output_asset.storage_key, filename), "filename": filename}
    raise HTTPException(status_code=404, detail="preview result not ready")


def get_task_result_url(db: Session, user_id: str, task_id: str) -> str:
    task = db.execute(select(Task).where(Task.id == task_id, Task.user_id == user_id)).scalar_one_or_none()
    if task is None:
        raise HTTPException(status_code=404, detail="task not found")
    preview_path = task_preview_path(task)
    if preview_path.exists() and preview_path.is_file():
        return f"/api/tasks/{task.id}/result/{quote(task_result_download_name(task))}"
    if task.output_asset_id:
        output_asset = db.get(Asset, task.output_asset_id)
        if output_asset is not None and output_asset.storage_key:
            return f"/api/tasks/{task.id}/result/{quote(task_result_download_name(task))}"
    raise HTTPException(status_code=404, detail="result not ready")


def _internal_batch_tasks(db: Session, user_id: str, batch_id: str) -> list[Task]:
    batch_id_expr = Task.params["internalBatchId"].as_string()
    return list(db.execute(
        select(Task)
        .where(
            Task.user_id == user_id,
            Task.tool_slug == "subtitle-translate-workflow",
            batch_id_expr == batch_id,
        )
        .options(selectinload(Task.input_asset), selectinload(Task.output_asset))
        .order_by(Task.created_at.asc())
    ).scalars())


def internal_batch_status(db: Session, user_id: str, batch_id: str) -> dict:
    tasks = _internal_batch_tasks(db, user_id, batch_id)
    if not tasks:
        raise HTTPException(status_code=404, detail="batch not found")
    batch_name = str((tasks[0].params or {}).get("internalBatchName") or "内部批量任务")
    created = len(tasks)
    total = internal_batch_expected_total_from_tasks(tasks)
    missing = max(0, total - created)
    succeeded = sum(1 for task in tasks if task.status == "succeeded")
    failed = sum(1 for task in tasks if task.status == "failed")
    cancelled = sum(1 for task in tasks if task.status == "cancelled")
    active_processing = sum(1 for task in tasks if task.status in {"queued", "processing"})
    processing = active_processing + missing
    return {
        "id": batch_id,
        "name": batch_name,
        "total": total,
        "created": created,
        "succeeded": succeeded,
        "failed": failed,
        "cancelled": cancelled,
        "missing": missing,
        "processing": processing,
        "downloadReady": succeeded > 0 and missing == 0 and active_processing == 0,
        "tasks": [task_to_dict(task, verify_result=True) for task in tasks],
    }


def _internal_batch_zip_entries(db: Session, user_id: str, batch_id: str) -> tuple[dict, list[dict], list[dict]]:
    batch = internal_batch_status(db, user_id, batch_id)
    if not batch["downloadReady"]:
        raise HTTPException(status_code=409, detail=f"batch is not complete: {batch['succeeded']}/{batch['total']} succeeded, {batch['processing']} pending")

    tasks = _internal_batch_tasks(db, user_id, batch_id)
    succeeded_tasks = [task for task in tasks if task.status == "succeeded"]
    safe_batch_name = safe_storage_name(str(batch["name"]) or batch_id).removesuffix(".zip")
    zip_dir = settings.upload_path / "internal-batch-zips"
    zip_dir.mkdir(parents=True, exist_ok=True)
    batch["_safeZipName"] = safe_batch_name
    batch["_zipDir"] = zip_dir

    used_names: set[str] = set()
    entries: list[dict] = []
    for index, task in enumerate(succeeded_tasks, start=1):
        preview_path = task_preview_path(task)
        if preview_path.exists() and preview_path.is_file():
            source_path = preview_path
            entry_size = source_path.stat().st_size
            storage_key = None
        elif task.output_asset_id:
            output_asset = db.get(Asset, task.output_asset_id)
            if output_asset is None:
                raise HTTPException(status_code=404, detail=f"task result not found: {task.id}")
            source_path = None
            entry_size = int(output_asset.size_bytes or 0)
            storage_key = output_asset.storage_key
        else:
            raise HTTPException(status_code=404, detail=f"task result not ready: {task.id}")

        base_name = safe_storage_name(task_result_download_name(task))
        zip_name = f"{index:03d}-{base_name}"
        duplicate_index = 1
        while zip_name in used_names:
            zip_name = f"{index:03d}-{task.id[:8]}-{duplicate_index}-{base_name}"
            duplicate_index += 1
        used_names.add(zip_name)
        entries.append(
            {
                "task_id": task.id,
                "source_path": source_path,
                "storage_key": storage_key,
                "zip_name": zip_name,
                "size": entry_size,
            }
        )
    task_summaries = [
        {
            "id": task.id,
            "status": task.status,
            "errorCode": task.error_code,
            "progressPercent": task.progress_percent,
            "progressStage": task.progress_stage,
        }
        for task in tasks
    ]
    return batch, task_summaries, entries


def _internal_batch_zip_parts(batch: dict, entries: list[dict]) -> list[dict]:
    max_part_bytes = max(1, int(settings.internal_batch_zip_part_max_bytes))
    max_part_files = max(1, int(settings.internal_batch_zip_part_max_files))
    entry_parts: list[list[dict]] = []
    current_part: list[dict] = []
    current_size = 0
    for entry in entries:
        entry_size = int(entry["size"])
        if current_part and (current_size + entry_size > max_part_bytes or len(current_part) >= max_part_files):
            entry_parts.append(current_part)
            current_part = []
            current_size = 0
        current_part.append(entry)
        current_size += entry_size
    if current_part:
        entry_parts.append(current_part)

    safe_batch_name = str(batch["_safeZipName"])
    zip_dir = Path(batch["_zipDir"])
    base_zip_stem = f"{safe_batch_name}-{str(batch['id'])[:8]}-{batch['succeeded']}-of-{batch['total']}"
    part_count = max(1, len(entry_parts))
    parts: list[dict] = []

    for part_index, part_entries in enumerate(entry_parts, start=1):
        zip_filename = f"{base_zip_stem}.zip" if part_count == 1 else f"{base_zip_stem}-part{part_index:02d}-of{part_count:02d}.zip"
        zip_path = zip_dir / zip_filename
        download_filename = f"{safe_batch_name}.zip" if part_count == 1 else f"{safe_batch_name}-part{part_index:02d}-of{part_count:02d}.zip"
        parts.append(
            {
                "path": zip_path,
                "filename": download_filename,
                "index": part_index,
                "sizeBytes": _internal_batch_zip_size_bytes(zip_path),
                "estimatedSizeBytes": sum(int(entry["size"]) for entry in part_entries),
                "entries": part_entries,
                "partCount": part_count,
            }
        )
    return parts


def _internal_batch_zip_exists(zip_path: Path) -> bool:
    return zip_path.exists() and zip_path.is_file() and zip_path.stat().st_size > 0


def _internal_batch_zip_remote_marker_path(zip_path: Path) -> Path:
    return zip_path.with_suffix(zip_path.suffix + ".remote.json")


def _remote_storage_object_exists(storage_key: str) -> bool:
    if not storage.is_remote or not storage_key:
        return True
    remote_exists = getattr(storage, "remote_exists", None)
    if not callable(remote_exists):
        return True
    return bool(remote_exists(storage_key))


def _internal_batch_zip_remote_object_exists(marker: dict) -> bool:
    storage_key = str(marker.get("storageKey") or "").strip()
    return _remote_storage_object_exists(storage_key)


def _read_internal_batch_zip_remote_marker(zip_path: Path, verify_remote: bool = False) -> dict | None:
    marker_path = _internal_batch_zip_remote_marker_path(zip_path)
    if not marker_path.exists():
        return None
    try:
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not marker.get("url") or int(marker.get("sizeBytes") or 0) <= 0:
        return None
    if verify_remote and not _internal_batch_zip_remote_object_exists(marker):
        marker_path.unlink(missing_ok=True)
        return None
    return marker


def _internal_batch_zip_size_bytes(zip_path: Path) -> int:
    if zip_path.exists() and zip_path.is_file():
        return zip_path.stat().st_size
    marker = _read_internal_batch_zip_remote_marker(zip_path)
    return int(marker.get("sizeBytes") or 0) if marker else 0


def _internal_batch_zip_remote_download_url(marker: dict, filename: str) -> str:
    storage_key = str(marker.get("storageKey") or "").strip()
    if storage_key and storage.is_remote:
        return storage.presign_download(storage_key, filename)
    return str(marker.get("url") or "")


def _internal_batch_zip_process_lock(lock_path: Path) -> threading.Lock:
    lock_key = str(lock_path)
    with _INTERNAL_BATCH_ZIP_LOCKS_GUARD:
        lock = _INTERNAL_BATCH_ZIP_LOCKS.get(lock_key)
        if lock is None:
            lock = threading.Lock()
            _INTERNAL_BATCH_ZIP_LOCKS[lock_key] = lock
        return lock


@contextmanager
def _locked_internal_batch_zip(zip_path: Path):
    lock_path = zip_path.with_suffix(zip_path.suffix + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    process_lock = _internal_batch_zip_process_lock(lock_path)
    with process_lock:
        with lock_path.open("a+", encoding="utf-8") as lock_file:
            if fcntl is not None:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                if fcntl is not None:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def plan_internal_batch_zip(db: Session, user_id: str, batch_id: str) -> dict:
    batch, _tasks, entries = _internal_batch_zip_entries(db, user_id, batch_id)
    parts = _internal_batch_zip_parts(batch, entries)
    return {"parts": parts, "partCount": len(parts)}


def _remote_internal_batch_zip_enabled() -> bool:
    return bool(settings.internal_batch_zip_gpu_enabled and settings.model_plaza_gpu_api_url and storage.is_remote)


def _completed_internal_batch_id_for_auto_zip(db: Session, task: Task) -> str | None:
    if not settings.internal_batch_zip_auto_prepare_enabled or not _remote_internal_batch_zip_enabled():
        return None
    if task.tool_slug != "subtitle-translate-workflow" or not isinstance(task.params, dict):
        return None
    batch_id = str(task.params.get("internalBatchId") or "").strip()
    if not batch_id:
        return None
    tasks = _internal_batch_tasks(db, task.user_id, batch_id)
    if not tasks or not any(item.status == "succeeded" for item in tasks):
        return None
    if len(tasks) < internal_batch_expected_total_from_tasks(tasks):
        return None
    terminal_statuses = {"succeeded", "failed", "cancelled"}
    if any(item.status not in terminal_statuses for item in tasks):
        return None
    return batch_id


def _enqueue_internal_batch_zip_after_commit(user_id: str, batch_id: str) -> None:
    try:
        enqueue_internal_batch_zip(user_id, batch_id)
    except Exception:
        logger.exception("Failed to enqueue internal batch zip %s for user %s", batch_id, user_id)


def _internal_batch_zip_summary(batch: dict, task_summaries: list[dict], selected_part: dict) -> dict:
    return {
        "id": batch["id"],
        "name": batch["name"],
        "total": batch["total"],
        "created": batch.get("created", batch["total"]),
        "succeeded": batch["succeeded"],
        "failed": batch["failed"],
        "cancelled": batch["cancelled"],
        "missing": batch.get("missing", 0),
        "processing": batch["processing"],
        "partIndex": selected_part["index"],
        "partCount": selected_part["partCount"],
        "partMaxBytes": max(1, int(settings.internal_batch_zip_part_max_bytes)),
        "partMaxFiles": max(1, int(settings.internal_batch_zip_part_max_files)),
        "includedTaskIds": [entry["task_id"] for entry in selected_part["entries"]],
        "skippedTasks": [task for task in task_summaries if task["status"] != "succeeded"],
    }


def _remote_internal_batch_zip_payload(batch: dict, task_summaries: list[dict], selected_part: dict) -> dict | None:
    entries = []
    for entry in selected_part["entries"]:
        storage_key = entry.get("storage_key")
        if not storage_key:
            return None
        entries.append(
            {
                "task_id": entry["task_id"],
                "storage_key": storage_key,
                "zip_name": entry["zip_name"],
                "size": int(entry.get("size") or 0),
                "download_url": storage.presign_download(storage_key, entry["zip_name"]),
            }
        )
    zip_path = Path(selected_part["path"])
    return {
        "zip_id": zip_path.stem,
        "filename": selected_part["filename"],
        "zip_storage_key": f"model-plaza/output/zips/{str(batch['id'])[:8]}/{zip_path.name}",
        "summary_name": INTERNAL_BATCH_ZIP_SUMMARY_NAME,
        "summary": _internal_batch_zip_summary(batch, task_summaries, selected_part),
        "entries": entries,
    }


def _request_remote_internal_batch_zip(payload: dict) -> dict:
    base_url = settings.model_plaza_gpu_api_url.rstrip("/")
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {"Content-Type": "application/json", "Content-Length": str(len(body))}
    if settings.model_plaza_gpu_api_key:
        headers["X-API-Key"] = settings.model_plaza_gpu_api_key
    request = urllib.request.Request(f"{base_url}/internal-batch-zips", data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=settings.internal_batch_zip_gpu_timeout_seconds) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise HTTPException(status_code=502, detail=f"GPU zip failed: HTTP {exc.code}: {detail}") from exc
    except (TimeoutError, urllib.error.URLError, OSError) as exc:
        raise HTTPException(status_code=502, detail=f"GPU zip failed: {exc}") from exc


def _download_remote_internal_batch_zip_from_gpu(zip_id: str, target_path: Path) -> Path:
    base_url = settings.model_plaza_gpu_api_url.rstrip("/")
    url = f"{base_url}/internal-batch-zips/{quote(zip_id, safe='')}/download"
    headers = {}
    if settings.model_plaza_gpu_api_key:
        headers["X-API-Key"] = settings.model_plaza_gpu_api_key
    request = urllib.request.Request(url, headers=headers, method="GET")
    target_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with urllib.request.urlopen(request, timeout=settings.internal_batch_zip_gpu_timeout_seconds) as response:
            with target_path.open("wb") as output_file:
                shutil.copyfileobj(response, output_file)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise HTTPException(status_code=502, detail=f"GPU zip download failed: HTTP {exc.code}: {detail}") from exc
    except (TimeoutError, urllib.error.URLError, OSError) as exc:
        raise HTTPException(status_code=502, detail=f"GPU zip download failed: {exc}") from exc
    if not _internal_batch_zip_exists(target_path):
        raise HTTPException(status_code=502, detail="GPU zip download returned an empty file")
    return target_path


def _restore_internal_batch_zip_to_current_tos(zip_id: str, storage_key: str, filename: str, zip_path: Path) -> dict | None:
    if not storage.is_remote or not storage_key or _remote_storage_object_exists(storage_key):
        return None
    temp_path = zip_path.with_suffix(f".{uuid4().hex}.gpu-download.tmp")
    try:
        downloaded_path = _download_remote_internal_batch_zip_from_gpu(zip_id, temp_path)
        stored = storage.save_file(storage_key, downloaded_path)
        return {
            "url": stored.public_url,
            "storageKey": stored.storage_key,
            "sizeBytes": stored.size,
            "filename": filename,
        }
    finally:
        temp_path.unlink(missing_ok=True)


def _create_remote_internal_batch_zip(batch: dict, task_summaries: list[dict], selected_part: dict) -> bool:
    if not _remote_internal_batch_zip_enabled():
        return False
    zip_path = Path(selected_part["path"])
    if _read_internal_batch_zip_remote_marker(zip_path, verify_remote=True):
        return True
    payload = _remote_internal_batch_zip_payload(batch, task_summaries, selected_part)
    if payload is None:
        return False
    with _locked_internal_batch_zip(zip_path):
        if _read_internal_batch_zip_remote_marker(zip_path, verify_remote=True) or _internal_batch_zip_exists(zip_path):
            return True
        result = _request_remote_internal_batch_zip(payload)
        marker = {
            "url": str(result.get("url") or ""),
            "storageKey": str(result.get("storage_key") or payload["zip_storage_key"]),
            "sizeBytes": int(result.get("size_bytes") or 0),
            "filename": selected_part["filename"],
        }
        restored_marker = _restore_internal_batch_zip_to_current_tos(zip_path.stem, marker["storageKey"], selected_part["filename"], zip_path)
        if restored_marker:
            marker = restored_marker
        if not marker["url"] or marker["sizeBytes"] <= 0:
            raise HTTPException(status_code=502, detail="GPU zip returned an invalid result")
        marker_path = _internal_batch_zip_remote_marker_path(zip_path)
        marker_path.parent.mkdir(parents=True, exist_ok=True)
        marker_path.write_text(json.dumps(marker, ensure_ascii=False, indent=2), encoding="utf-8")
        selected_part["sizeBytes"] = marker["sizeBytes"]
        selected_part["storageKey"] = marker["storageKey"]
        selected_part["remoteUrl"] = _internal_batch_zip_remote_download_url(marker, selected_part["filename"])
        return True


def create_internal_batch_zip(db: Session, user_id: str, batch_id: str, part: int | None = None) -> dict:
    batch, task_summaries, entries = _internal_batch_zip_entries(db, user_id, batch_id)
    parts = _internal_batch_zip_parts(batch, entries)
    if part is not None and (part < 1 or part > len(parts)):
        raise HTTPException(status_code=404, detail="download part not found")

    selected_parts = parts if part is None else [parts[part - 1]]
    db.close()
    for selected_part in selected_parts:
        zip_path = selected_part["path"]
        if not _internal_batch_zip_exists(zip_path) and not _read_internal_batch_zip_remote_marker(zip_path, verify_remote=True):
            if _create_remote_internal_batch_zip(batch, task_summaries, selected_part):
                continue
            with _locked_internal_batch_zip(zip_path):
                if not _internal_batch_zip_exists(zip_path):
                    summary = _internal_batch_zip_summary(batch, task_summaries, selected_part)
                    temp_zip_path = zip_path.with_suffix(f".{uuid4().hex}.tmp")
                    try:
                        with zipfile.ZipFile(temp_zip_path, "w", compression=zipfile.ZIP_STORED) as archive:
                            archive.writestr(INTERNAL_BATCH_ZIP_SUMMARY_NAME, json.dumps(summary, ensure_ascii=False, indent=2))
                            for entry in selected_part["entries"]:
                                source_path = entry.get("source_path")
                                if source_path is None:
                                    try:
                                        source_path = storage.ensure_local(entry["storage_key"])
                                    except FileNotFoundError as exc:
                                        raise HTTPException(status_code=404, detail=f"task result not ready: {entry['task_id']}") from exc
                                archive.write(source_path, entry["zip_name"])
                        temp_zip_path.replace(zip_path)
                    finally:
                        if temp_zip_path.exists():
                            temp_zip_path.unlink()
        marker = _read_internal_batch_zip_remote_marker(zip_path, verify_remote=True)
        if marker:
            selected_part["sizeBytes"] = int(marker["sizeBytes"])
            selected_part["storageKey"] = str(marker.get("storageKey") or "")
            selected_part["remoteUrl"] = _internal_batch_zip_remote_download_url(marker, selected_part["filename"])
        else:
            selected_part["sizeBytes"] = zip_path.stat().st_size

    first_part = selected_parts[0]
    return {"path": first_part["path"], "filename": first_part["filename"], "parts": parts, "partCount": len(parts)}


def retry_internal_batch_tasks(db: Session, user_id: str, batch_id: str, at_front: bool = False) -> dict:
    tasks = _internal_batch_tasks(db, user_id, batch_id)
    if not tasks:
        raise HTTPException(status_code=404, detail="batch not found")

    retryable_tasks = [task for task in tasks if task.status in {"failed", "cancelled"}]
    if not retryable_tasks:
        raise HTTPException(status_code=409, detail="batch has no failed or cancelled tasks to retry")

    wallet = get_wallet(db, user_id, lock=True)
    retry_specs: list[tuple[Task, dict, int]] = []
    total_estimate = 0
    for task in retryable_tasks:
        tool = get_tool(task.tool_slug)
        if tool is None or tool["status"] != "online":
            raise HTTPException(status_code=400, detail=f"tool is not available: {task.tool_slug}")
        input_asset = db.get(Asset, task.input_asset_id)
        if input_asset is None or input_asset.user_id != user_id:
            raise HTTPException(status_code=400, detail=f"missing uploaded asset for task: {task.id}")
        ensure_input_asset_remote_readable(input_asset)
        params = dict(task.params or {})
        estimate = estimate_credits(tool, {**params, "duration": params.get("duration") or input_asset.duration_seconds or 30})
        retry_specs.append((task, tool, estimate))
        total_estimate += estimate

    if wallet.credits - wallet.frozen_credits < total_estimate:
        raise HTTPException(status_code=402, detail="insufficient credits")

    retried_ids: list[str] = []
    for task, tool, estimate in retry_specs:
        task.status = "queued"
        task.provider_job_id = f"mock_{uuid4()}"
        task.estimated_credits = estimate
        task.frozen_credits = estimate
        task.charged_credits = 0
        task.error_code = None
        task.progress_percent = 0
        task.progress_stage = "等待 worker 领取任务"
        task.output_asset_id = None
        task.output_url = ""
        task.completed_at = None
        wallet.frozen_credits += estimate
        add_ledger(db, user_id, "freeze", 0, f"{tool['name']} 重新生成，冻结 {estimate} 积分", task.id)
        cancel_marker = settings.upload_path / f"{task.id}.cancel"
        cancel_marker.unlink(missing_ok=True)
        retried_ids.append(task.id)

    db.commit()
    for task_id in retried_ids:
        if at_front:
            enqueue_provider_job(task_id, at_front=True)
        else:
            enqueue_provider_job(task_id)

    return {"retried": len(retried_ids), "taskIds": retried_ids, "batch": internal_batch_status(db, user_id, batch_id)}


def retry_internal_batch_task_with_replacement_asset(
    db: Session,
    user_id: str,
    batch_id: str,
    task_id: str,
    input_asset_id: str,
    duration_seconds: int = 0,
    at_front: bool = True,
) -> dict:
    task = db.execute(
        select(Task)
        .where(Task.id == task_id, Task.user_id == user_id)
        .options(selectinload(Task.input_asset))
        .with_for_update()
    ).scalar_one_or_none()
    if task is None:
        raise HTTPException(status_code=404, detail="task not found")
    params = dict(task.params or {})
    if task.tool_slug != "subtitle-translate-workflow" or str(params.get("internalBatchId") or "") != batch_id:
        raise HTTPException(status_code=400, detail="task does not belong to this internal batch")
    missing_reason = task_result_missing_reason(task) if task.status == "succeeded" else ""
    is_missing_result_retry = task.status == "succeeded" and bool(missing_reason)
    if task.status not in {"failed", "cancelled"} and not is_missing_result_retry:
        raise HTTPException(status_code=409, detail="only failed, cancelled, or missing-result tasks can be replaced and retried")

    replacement_asset = db.get(Asset, input_asset_id)
    if replacement_asset is None or replacement_asset.user_id != user_id:
        raise HTTPException(status_code=400, detail="missing uploaded replacement asset")
    ensure_input_asset_remote_readable(replacement_asset)

    tool = get_tool(task.tool_slug)
    if tool is None or tool["status"] != "online":
        raise HTTPException(status_code=400, detail=f"tool is not available: {task.tool_slug}")
    if duration_seconds > 0:
        params["duration"] = duration_seconds
    elif replacement_asset.duration_seconds:
        params["duration"] = replacement_asset.duration_seconds
    if is_missing_result_retry:
        params["_noChargeRetry"] = True
        params["_missingResultRetryAt"] = int(now().timestamp() * 1000)
        params["_missingResultReason"] = missing_reason
    estimate = estimate_credits(tool, {**params, "duration": params.get("duration") or replacement_asset.duration_seconds or 30})
    wallet = get_wallet(db, user_id, lock=True)
    if not is_missing_result_retry and wallet.credits - wallet.frozen_credits < estimate:
        raise HTTPException(status_code=402, detail="insufficient credits")

    previous_charged = task.charged_credits
    task.input_asset_id = replacement_asset.id
    task.params = params
    task.status = "queued"
    task.provider_job_id = f"mock_{uuid4()}"
    task.estimated_credits = estimate
    task.frozen_credits = 0 if is_missing_result_retry else estimate
    task.charged_credits = previous_charged if is_missing_result_retry else 0
    task.error_code = None
    task.progress_percent = 0
    task.progress_stage = "结果文件缺失，补传视频后插队重跑，等待 worker 领取任务" if is_missing_result_retry else "补传视频后插队重跑，等待 worker 领取任务"
    task.output_asset_id = None
    task.output_url = ""
    task.completed_at = None
    if not is_missing_result_retry:
        wallet.frozen_credits += estimate
        add_ledger(db, user_id, "freeze", 0, f"{tool['name']} 补传重跑，冻结 {estimate} 积分", task.id)
    cancel_marker = settings.upload_path / f"{task.id}.cancel"
    cancel_marker.unlink(missing_ok=True)

    db.commit()
    db.refresh(task)
    enqueue_provider_job(task.id, at_front=at_front)
    return {"task": task_to_dict(task), "batch": internal_batch_status(db, user_id, batch_id)}


def retry_internal_batch_missing_result_task(
    db: Session,
    user_id: str,
    batch_id: str,
    task_id: str,
    at_front: bool = True,
) -> dict:
    task = db.execute(
        select(Task)
        .where(Task.id == task_id, Task.user_id == user_id)
        .options(selectinload(Task.input_asset), selectinload(Task.output_asset))
        .with_for_update()
    ).scalar_one_or_none()
    if task is None:
        raise HTTPException(status_code=404, detail="task not found")
    params = dict(task.params or {})
    if task.tool_slug != "subtitle-translate-workflow" or str(params.get("internalBatchId") or "") != batch_id:
        raise HTTPException(status_code=400, detail="task does not belong to this internal batch")
    if task.status != "succeeded":
        raise HTTPException(status_code=409, detail="only succeeded tasks can use missing-result retry")

    missing_reason = task_result_missing_reason(task)
    if not missing_reason:
        raise HTTPException(status_code=409, detail="task result object still exists; no retry needed")
    input_asset = getattr(task, "input_asset", None) or db.get(Asset, task.input_asset_id)
    if input_asset is None or input_asset.user_id != user_id:
        raise HTTPException(status_code=400, detail="missing uploaded asset for task")
    ensure_input_asset_remote_readable(input_asset)

    tool = get_tool(task.tool_slug)
    if tool is None or tool["status"] != "online":
        raise HTTPException(status_code=400, detail=f"tool is not available: {task.tool_slug}")

    params["_noChargeRetry"] = True
    params["_missingResultRetryAt"] = int(now().timestamp() * 1000)
    params["_missingResultReason"] = missing_reason
    task.params = params
    task.status = "queued"
    task.provider_job_id = f"mock_{uuid4()}"
    task.frozen_credits = 0
    task.error_code = None
    task.progress_percent = 0
    task.progress_stage = "结果文件缺失，已插队重跑，等待 worker 领取任务"
    task.output_asset_id = None
    task.output_url = ""
    task.completed_at = None
    cancel_marker = settings.upload_path / f"{task.id}.cancel"
    cancel_marker.unlink(missing_ok=True)

    db.commit()
    db.refresh(task)
    enqueue_provider_job(task.id, at_front=at_front)
    return {"task": task_to_dict(task), "batch": internal_batch_status(db, user_id, batch_id)}


def prioritize_internal_batch_queued_task(
    db: Session,
    user_id: str,
    batch_id: str,
    task_id: str,
    at_front: bool = True,
) -> dict:
    task = db.execute(
        select(Task)
        .where(Task.id == task_id, Task.user_id == user_id)
        .options(selectinload(Task.input_asset))
        .with_for_update()
    ).scalar_one_or_none()
    if task is None:
        raise HTTPException(status_code=404, detail="task not found")
    params = dict(task.params or {})
    if task.tool_slug != "subtitle-translate-workflow" or str(params.get("internalBatchId") or "") != batch_id:
        raise HTTPException(status_code=400, detail="task does not belong to this internal batch")
    if task.status != "queued":
        raise HTTPException(status_code=409, detail="only queued tasks can be prioritized")
    input_asset = getattr(task, "input_asset", None) or db.get(Asset, task.input_asset_id)
    if input_asset is None or input_asset.user_id != user_id:
        raise HTTPException(status_code=400, detail="missing uploaded asset for task")
    ensure_input_asset_remote_readable(input_asset)

    params["_manualPriorityBoostCount"] = int(params.get("_manualPriorityBoostCount") or 0) + 1
    params["_manualPriorityBoostAt"] = int(now().timestamp() * 1000)
    task.params = params
    task.error_code = None
    task.progress_percent = max(5, task.progress_percent)
    task.progress_stage = "已插队到最高优先级，等待 worker 领取任务"
    cancel_marker = settings.upload_path / f"{task.id}.cancel"
    cancel_marker.unlink(missing_ok=True)

    db.commit()
    db.refresh(task)
    enqueue_provider_job(task.id, at_front=at_front)
    return {"task": task_to_dict(task), "batch": internal_batch_status(db, user_id, batch_id)}


def retry_failed_task_single_gpu(db: Session, user_id: str, task_id: str) -> Task:
    task = db.execute(
        select(Task)
        .where(Task.id == task_id, Task.user_id == user_id)
        .options(selectinload(Task.input_asset))
        .with_for_update()
    ).scalar_one_or_none()
    if task is None:
        raise HTTPException(status_code=404, detail="task not found")
    if task.status != "failed":
        raise HTTPException(status_code=409, detail="only failed tasks can be retried")

    tool = get_tool(task.tool_slug)
    if tool is None or tool["status"] != "online":
        raise HTTPException(status_code=400, detail=f"tool is not available: {task.tool_slug}")
    input_asset = db.get(Asset, task.input_asset_id)
    if input_asset is None or input_asset.user_id != user_id:
        raise HTTPException(status_code=400, detail="missing uploaded asset for task")
    if is_expired(input_asset.expires_at):
        raise HTTPException(status_code=400, detail="uploaded asset has expired")
    ensure_input_asset_remote_readable(input_asset)

    params = dict(task.params or {})
    params.update(
        {
            "forceSingleGpu": True,
            "exclusiveGpu": True,
            "singleGpuRetry": True,
            "singleGpuRetryAt": int(now().timestamp() * 1000),
        }
    )
    estimate = estimate_credits(tool, {**params, "duration": params.get("duration") or input_asset.duration_seconds or 30})
    wallet = get_wallet(db, user_id, lock=True)
    if wallet.credits - wallet.frozen_credits < estimate:
        raise HTTPException(status_code=402, detail="insufficient credits")

    task.status = "queued"
    task.provider_job_id = f"mock_{uuid4()}"
    task.params = params
    task.estimated_credits = estimate
    task.frozen_credits = estimate
    task.charged_credits = 0
    task.error_code = None
    task.progress_percent = 0
    task.progress_stage = "等待 worker 领取任务（单卡独占重跑）"
    task.output_asset_id = None
    task.output_url = ""
    task.completed_at = None
    wallet.frozen_credits += estimate
    add_ledger(db, user_id, "freeze", 0, f"{tool['name']} 单卡重跑，冻结 {estimate} 积分", task.id)
    cancel_marker = settings.upload_path / f"{task.id}.cancel"
    cancel_marker.unlink(missing_ok=True)

    db.commit()
    db.refresh(task)
    enqueue_provider_job(task.id)
    return task


def ledger_to_dict(entry: WalletLedger) -> dict:
    return {
        "id": entry.id,
        "userId": entry.user_id,
        "type": entry.type,
        "amount": entry.amount,
        "title": entry.title,
        "taskId": entry.task_id,
        "createdAt": serialize_datetime(entry.created_at),
    }


def normalize_pagination(page: int = 1, per_page: int = DEFAULT_PAGE_SIZE) -> tuple[int, int]:
    page = max(1, page)
    per_page = max(1, min(per_page, MAX_PAGE_SIZE))
    return page, per_page


def page_info(total: int, page: int, per_page: int) -> dict:
    total_pages = max(1, (total + per_page - 1) // per_page)
    return {
        "page": page,
        "perPage": per_page,
        "total": total,
        "totalPages": total_pages,
        "hasNext": page < total_pages,
        "hasPrevious": page > 1,
    }


TASK_STATUS_FILTERS = {"queued", "processing", "succeeded", "failed", "cancelled"}


def parse_task_filter_datetime(value: str | None, field_name: str) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"invalid {field_name}") from exc
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def paginated_tasks(
    db: Session,
    user_id: str,
    page: int = 1,
    per_page: int = DEFAULT_PAGE_SIZE,
    status: str | None = None,
    completed_from: str | None = None,
    completed_to: str | None = None,
    batch_name: str | None = None,
    internal_batch_only: bool = False,
) -> dict:
    page, per_page = normalize_pagination(page, per_page)
    deleted_expr = Task.params["taskListDeletedAt"].as_string()
    filters = [Task.user_id == user_id, deleted_expr.is_(None)]
    if internal_batch_only:
        batch_id_expr = Task.params["internalBatchId"].as_string()
        filters.extend(
            [
                Task.tool_slug == "subtitle-translate-workflow",
                batch_id_expr.is_not(None),
                batch_id_expr != "",
            ]
        )
    if status:
        if status not in TASK_STATUS_FILTERS:
            raise HTTPException(status_code=400, detail="invalid task status filter")
        filters.append(Task.status == status)
    completed_from_datetime = parse_task_filter_datetime(completed_from, "completedFrom")
    completed_to_datetime = parse_task_filter_datetime(completed_to, "completedTo")
    if completed_from_datetime is not None:
        filters.append(Task.completed_at >= completed_from_datetime)
    if completed_to_datetime is not None:
        filters.append(Task.completed_at < completed_to_datetime)
    normalized_batch_name = (batch_name or "").strip()
    if normalized_batch_name:
        filters.append(Task.params["internalBatchName"].as_string().ilike(f"%{normalized_batch_name}%"))
    total = db.execute(select(func.count()).select_from(Task).where(*filters)).scalar_one()
    tasks = db.execute(
        select(Task)
        .where(*filters)
        .options(selectinload(Task.input_asset))
        .order_by(Task.created_at.desc())
        .offset((page - 1) * per_page)
        .limit(per_page)
    ).scalars()
    return {"items": [task_to_dict(task) for task in tasks], "page": page_info(total, page, per_page)}


def delete_tasks_from_list(db: Session, user_id: str, task_ids: list[str]) -> dict:
    unique_task_ids = [task_id for task_id in dict.fromkeys(task_ids) if task_id]
    if not unique_task_ids:
        return {"deleted": 0, "missing": 0, "taskIds": []}
    tasks = list(db.execute(select(Task).where(Task.user_id == user_id, Task.id.in_(unique_task_ids))).scalars())
    found_ids = {task.id for task in tasks}
    deleted_at = serialize_datetime(now())
    deleted_ids: list[str] = []
    for task in tasks:
        params = dict(task.params or {})
        if params.get("taskListDeletedAt"):
            continue
        params["taskListDeletedAt"] = deleted_at
        task.params = params
        deleted_ids.append(task.id)
    db.commit()
    return {"deleted": len(deleted_ids), "missing": len(unique_task_ids) - len(found_ids), "taskIds": deleted_ids}


def paginated_ledger(db: Session, user_id: str, page: int = 1, per_page: int = DEFAULT_PAGE_SIZE) -> dict:
    page, per_page = normalize_pagination(page, per_page)
    total = db.execute(select(func.count()).select_from(WalletLedger).where(WalletLedger.user_id == user_id)).scalar_one()
    ledger = db.execute(
        select(WalletLedger)
        .where(WalletLedger.user_id == user_id)
        .order_by(WalletLedger.created_at.desc())
        .offset((page - 1) * per_page)
        .limit(per_page)
    ).scalars()
    return {"items": [ledger_to_dict(entry) for entry in ledger], "page": page_info(total, page, per_page)}


def serialize_bootstrap(db: Session, user_id: str | None = None) -> dict:
    ensure_demo_user(db)
    user_id = user_id or settings.demo_user_id
    user = db.get(User, user_id)
    wallet = get_wallet(db, user_id)
    task_page = paginated_tasks(db, user_id)
    ledger_page = paginated_ledger(db, user_id)
    return {
        "account": {
            "id": user.id,
            "name": user.name,
            "email": user.email,
            "credits": wallet.credits,
            "frozenCredits": wallet.frozen_credits,
            "availableCredits": wallet.credits - wallet.frozen_credits,
            "role": user.role,
        },
        "tools": TOOLS,
        "categories": CATEGORIES,
        "tasks": task_page["items"],
        "taskPage": task_page["page"],
        "ledger": ledger_page["items"],
        "ledgerPage": ledger_page["page"],
    }


async def save_upload(db: Session, user_id: str, file: UploadFile, kind: str, duration_seconds: int = 0) -> Asset:
    settings.upload_path.mkdir(parents=True, exist_ok=True)
    asset_id = str(uuid4())
    original_name = safe_storage_name(file.filename or "upload.bin")
    storage_key = object_key_for_upload(asset_id, kind, original_name) if storage.is_remote else f"{asset_id}-{original_name}"
    content = await file.read()
    storage.save_bytes(storage_key, content)

    asset = Asset(
        id=asset_id,
        user_id=user_id,
        kind=kind,
        original_name=original_name,
        mime_type=file.content_type or "application/octet-stream",
        storage_key=storage_key,
        url=public_url(storage_key),
        size_bytes=len(content),
        duration_seconds=duration_seconds,
        expires_at=asset_expires_at(),
    )
    db.add(asset)
    db.commit()
    db.refresh(asset)
    return asset


def create_presigned_asset_upload(db: Session, user_id: str, kind: str, duration_seconds: int = 0, original_name: str = "upload.bin") -> dict:
    asset_id = str(uuid4())
    storage_key = object_key_for_upload(asset_id, kind, original_name)
    presign = storage.presign_upload(kind=kind, duration_seconds=duration_seconds, storage_key=storage_key)
    presign["assetId"] = asset_id
    presign["storageKey"] = storage_key
    presign["publicUrl"] = public_url(storage_key)
    return presign


def complete_uploaded_asset(
    db: Session,
    user_id: str,
    asset_id: str,
    kind: str,
    original_name: str,
    mime_type: str,
    storage_key: str,
    size_bytes: int = 0,
    duration_seconds: int = 0,
) -> Asset:
    if db.get(Asset, asset_id) is not None:
        raise HTTPException(status_code=409, detail="asset already exists")
    normalized_key = storage_key.strip("/")
    if asset_id not in normalized_key:
        raise HTTPException(status_code=400, detail="storage key does not match asset")
    asset = Asset(
        id=asset_id,
        user_id=user_id,
        kind=kind,
        original_name=safe_storage_name(original_name),
        mime_type=mime_type or "application/octet-stream",
        storage_key=normalized_key,
        url=public_url(normalized_key),
        size_bytes=max(0, size_bytes),
        duration_seconds=duration_seconds,
        expires_at=asset_expires_at(),
    )
    db.add(asset)
    db.commit()
    db.refresh(asset)
    return asset


def _multipart_root() -> Path:
    root = settings.upload_path / ".multipart"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _safe_upload_id(upload_id: str) -> str:
    cleaned = upload_id.replace("-", "")
    if not cleaned.isalnum():
        raise HTTPException(status_code=400, detail="invalid upload id")
    return upload_id


def _multipart_dir(upload_id: str) -> Path:
    return _multipart_root() / _safe_upload_id(upload_id)


def _multipart_manifest_path(upload_id: str) -> Path:
    return _multipart_dir(upload_id) / "manifest.json"


def _read_multipart_manifest(upload_id: str) -> dict:
    path = _multipart_manifest_path(upload_id)
    if not path.exists():
        raise HTTPException(status_code=404, detail="multipart upload not found")
    import json

    return json.loads(path.read_text(encoding="utf-8"))


def _write_multipart_manifest(upload_id: str, manifest: dict) -> None:
    import json

    path = _multipart_manifest_path(upload_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")


def _uploaded_chunk_indexes(upload_id: str) -> list[int]:
    chunks_dir = _multipart_dir(upload_id) / "chunks"
    if not chunks_dir.exists():
        return []
    indexes: list[int] = []
    for path in chunks_dir.glob("*.part"):
        try:
            indexes.append(int(path.stem))
        except ValueError:
            continue
    return sorted(indexes)


def create_multipart_upload(
    db: Session,
    user_id: str,
    kind: str,
    original_name: str,
    mime_type: str,
    size_bytes: int,
    duration_seconds: int = 0,
    chunk_size: int = 8 * 1024 * 1024,
) -> dict:
    if size_bytes < 1:
        raise HTTPException(status_code=400, detail="sizeBytes must be positive")
    chunk_size = max(1024 * 1024, min(chunk_size, 64 * 1024 * 1024))
    asset_id = str(uuid4())
    upload_id = str(uuid4())
    safe_name = safe_storage_name(original_name)
    storage_key = object_key_for_upload(asset_id, kind, safe_name) if storage.is_remote else f"{asset_id}-{safe_name}"
    total_chunks = (size_bytes + chunk_size - 1) // chunk_size
    manifest = {
        "uploadId": upload_id,
        "assetId": asset_id,
        "userId": user_id,
        "kind": kind,
        "originalName": safe_name,
        "mimeType": mime_type or "application/octet-stream",
        "storageKey": storage_key,
        "sizeBytes": size_bytes,
        "durationSeconds": duration_seconds,
        "chunkSize": chunk_size,
        "totalChunks": total_chunks,
        "createdAt": int(now().timestamp() * 1000),
    }
    _write_multipart_manifest(upload_id, manifest)
    (_multipart_dir(upload_id) / "chunks").mkdir(parents=True, exist_ok=True)
    return {**manifest, "uploadedChunks": []}


def get_multipart_upload(user_id: str, upload_id: str) -> dict:
    manifest = _read_multipart_manifest(upload_id)
    if manifest.get("userId") != user_id:
        raise HTTPException(status_code=404, detail="multipart upload not found")
    return {**manifest, "uploadedChunks": _uploaded_chunk_indexes(upload_id)}


async def save_multipart_chunk(user_id: str, upload_id: str, chunk_index: int, file: UploadFile) -> dict:
    manifest = get_multipart_upload(user_id, upload_id)
    total_chunks = int(manifest["totalChunks"])
    if chunk_index < 0 or chunk_index >= total_chunks:
        raise HTTPException(status_code=400, detail="invalid chunk index")
    chunk_size = int(manifest["chunkSize"])
    expected_size = chunk_size
    if chunk_index == total_chunks - 1:
        expected_size = int(manifest["sizeBytes"]) - chunk_size * (total_chunks - 1)
    content = await file.read()
    if len(content) != expected_size:
        raise HTTPException(status_code=400, detail="chunk size mismatch")
    chunks_dir = _multipart_dir(upload_id) / "chunks"
    chunks_dir.mkdir(parents=True, exist_ok=True)
    part_path = chunks_dir / f"{chunk_index:06d}.part"
    temp_path = part_path.with_suffix(".tmp")
    temp_path.write_bytes(content)
    temp_path.replace(part_path)
    uploaded = _uploaded_chunk_indexes(upload_id)
    return {
        "uploadId": upload_id,
        "chunkIndex": chunk_index,
        "uploadedChunks": uploaded,
        "progressPercent": int(len(uploaded) / total_chunks * 100),
    }


def complete_multipart_upload(db: Session, user_id: str, upload_id: str) -> Asset:
    manifest = get_multipart_upload(user_id, upload_id)
    total_chunks = int(manifest["totalChunks"])
    uploaded = set(_uploaded_chunk_indexes(upload_id))
    missing = [index for index in range(total_chunks) if index not in uploaded]
    if missing:
        raise HTTPException(status_code=400, detail={"message": "missing chunks", "missingChunks": missing[:200]})
    if db.get(Asset, manifest["assetId"]) is not None:
        raise HTTPException(status_code=409, detail="asset already exists")

    upload_dir = _multipart_dir(upload_id)
    assembled_path = upload_dir / "assembled.bin"
    with assembled_path.open("wb") as output_file:
        for index in range(total_chunks):
            output_file.write((upload_dir / "chunks" / f"{index:06d}.part").read_bytes())
    size_bytes = assembled_path.stat().st_size
    if size_bytes != int(manifest["sizeBytes"]):
        raise HTTPException(status_code=400, detail="assembled file size mismatch")

    stored = storage.save_file(str(manifest["storageKey"]), assembled_path)
    asset = Asset(
        id=str(manifest["assetId"]),
        user_id=user_id,
        kind=str(manifest["kind"]),
        original_name=str(manifest["originalName"]),
        mime_type=str(manifest["mimeType"]),
        storage_key=stored.storage_key,
        url=stored.public_url,
        size_bytes=stored.size,
        duration_seconds=int(manifest["durationSeconds"]),
        expires_at=asset_expires_at(),
    )
    db.add(asset)
    db.commit()
    db.refresh(asset)
    shutil.rmtree(upload_dir, ignore_errors=True)
    return asset


def create_task(db: Session, user_id: str, tool_slug: str, input_asset_id: str, params: dict, at_front: bool = False) -> Task:
    tool = get_tool(tool_slug)
    if tool is None or tool["status"] != "online":
        raise HTTPException(status_code=400, detail="tool is not available")

    input_asset = db.get(Asset, input_asset_id)
    if input_asset is None or input_asset.user_id != user_id:
        raise HTTPException(status_code=400, detail="missing uploaded asset")
    ensure_input_asset_remote_readable(input_asset)
    if tool_slug == "video-redraw":
        if input_asset.kind != "video":
            raise HTTPException(status_code=400, detail="video redraw requires a video asset")
        prompt = str((params or {}).get("videoPrompt") or (params or {}).get("prompt") or "").strip()
        if not prompt:
            raise HTTPException(status_code=400, detail="请输入视频转绘提示词")
        provider_model = normalize_video_redraw_model(str((params or {}).get("providerModel") or "veo-3.1-generate-preview"))
        if provider_model not in VIDEO_REDRAW_MODELS:
            raise HTTPException(status_code=400, detail="不支持的视频转绘模型")
        params = {**(params or {}), "providerModel": provider_model}
        if not (input_asset.url or "").startswith(("http://", "https://")):
            raise HTTPException(status_code=400, detail="GPTProto 转绘需要公网可访问的视频地址，请启用 TOS/S3 等远程存储")

    estimate = estimate_credits(tool, {**params, "duration": params.get("duration") or input_asset.duration_seconds or 30})
    wallet = get_wallet(db, user_id, lock=True)
    if wallet.credits - wallet.frozen_credits < estimate:
        raise HTTPException(status_code=402, detail="insufficient credits")

    task = Task(
        id=str(uuid4()),
        user_id=user_id,
        tool_slug=tool["slug"],
        input_asset_id=input_asset.id,
        status="queued",
        params=params,
        estimated_credits=estimate,
        frozen_credits=estimate,
        provider=tool["provider"],
        provider_job_id=f"mock_{uuid4()}",
        progress_percent=0,
        progress_stage="等待 worker 领取任务",
    )
    wallet.frozen_credits += estimate
    db.add(task)
    add_ledger(db, user_id, "freeze", 0, f"{tool['name']} 冻结 {estimate} 积分", task.id)
    db.commit()
    db.refresh(task)
    enqueue_provider_job(task.id, at_front=at_front)
    return task


def cancel_task(db: Session, user_id: str, task_id: str) -> Task:
    task = db.execute(
        select(Task).where(Task.id == task_id, Task.user_id == user_id).with_for_update()
    ).scalar_one_or_none()
    if task is None:
        raise HTTPException(status_code=404, detail="task not found")
    if task.status in {"succeeded", "failed", "cancelled"}:
        raise HTTPException(status_code=409, detail="task is already finished")

    wallet = get_wallet(db, user_id, lock=True)
    tool = get_tool(task.tool_slug) or {"name": task.tool_slug}
    task.status = "cancelled"
    task.error_code = "USER_CANCELLED"
    task.progress_percent = 0
    task.progress_stage = "用户已取消，积分已释放"
    task.completed_at = now()
    wallet.frozen_credits = max(0, wallet.frozen_credits - task.frozen_credits)
    add_ledger(db, user_id, "refund", 0, f"{tool['name']} 已取消，释放 {task.frozen_credits} 积分", task.id)

    # 正在运行的视频 worker 会轮询这个标记，并把取消请求转发到远端 GPU Worker。
    cancel_marker = settings.upload_path / f"{task.id}.cancel"
    cancel_marker.parent.mkdir(parents=True, exist_ok=True)
    cancel_marker.write_text("cancelled", encoding="utf-8")

    db.commit()
    db.refresh(task)
    return task


def recharge_wallet(db: Session, user_id: str, credits: int) -> None:
    if credits < 1:
        raise HTTPException(status_code=400, detail="credits must be positive")
    wallet = get_wallet(db, user_id, lock=True)
    wallet.credits += min(credits, 100000)
    add_ledger(db, user_id, "recharge", credits, "模拟充值")
    db.commit()


def provider_callback(
    db: Session,
    provider_job_id: str,
    status: str,
    callback_id: str | None = None,
    output_url: str | None = None,
    output_storage_key: str | None = None,
    output_mime_type: str | None = None,
    output_size_bytes: int | None = None,
    charged_credits: int | None = None,
    error_code: str | None = None,
    progress_percent: int | None = None,
    progress_stage: str | None = None,
) -> tuple[bool, Task]:
    callback_id = callback_id or f"{provider_job_id}:{status}"
    if db.get(ProcessedCallback, callback_id) is not None:
        task = db.execute(select(Task).where(Task.provider_job_id == provider_job_id)).scalar_one()
        return True, task

    task = db.execute(select(Task).where(Task.provider_job_id == provider_job_id).with_for_update()).scalar_one_or_none()
    if task is None:
        raise HTTPException(status_code=404, detail="task not found")

    db.add(ProcessedCallback(callback_id=callback_id, provider_job_id=provider_job_id))
    wallet = get_wallet(db, task.user_id, lock=True)
    tool = get_tool(task.tool_slug) or {"name": task.tool_slug}
    auto_zip_batch_id: str | None = None

    if progress_percent is not None:
        normalized_progress = max(0, min(100, progress_percent))
        if status == "processing":
            # 远端模型完成后还需要上传结果、平台入库和扣费，处理中最多展示到 95%。
            normalized_progress = min(normalized_progress, 95)
        task.progress_percent = normalized_progress
    if progress_stage is not None:
        normalized_stage = progress_stage
        if status == "processing" and progress_percent is not None and progress_percent >= 100:
            # 避免用户看到“100% + 处理中”的矛盾状态。
            normalized_stage = "远端处理完成，正在回传结果"
        task.progress_stage = normalized_stage[:160]

    if status == "processing" and task.status in {"queued", "processing"}:
        task.status = "processing"
        if progress_percent is None and task.progress_percent < 5:
            task.progress_percent = 5
        if progress_stage is None and (
            not task.progress_stage or task.progress_stage == "等待 worker 领取任务"
        ):
            task.progress_stage = "worker 已领取，准备提交远端任务"

    if status == "succeeded" and task.status not in {"succeeded", "failed", "cancelled"}:
        no_charge_retry = bool((task.params or {}).get("_noChargeRetry"))
        previous_charged = int(task.charged_credits or 0)
        charge = 0 if no_charge_retry else min(charged_credits or task.estimated_credits, task.frozen_credits)
        # 本地 worker 或真实供应商都可以传入结果文件；未传时用文本占位，方便其他 mock 工具继续跑通。
        storage_key = output_storage_key or f"{task.id}-result.txt"
        result_url = output_url or public_url(storage_key)
        output_asset = Asset(
            id=str(uuid4()),
            user_id=task.user_id,
            kind="result",
            original_name=Path(storage_key).name,
            mime_type=output_mime_type or "text/plain",
            storage_key=storage_key,
            url=result_url,
            size_bytes=output_size_bytes or 0,
            expires_at=asset_expires_at(),
        )
        if output_storage_key is None:
            storage.write_text(output_asset.storage_key, f"任务 {task.id} 已完成\n")
        db.add(output_asset)
        task.status = "succeeded"
        task.progress_percent = 100
        task.progress_stage = "处理完成，结果已入库"
        task.output_asset_id = output_asset.id
        task.output_url = output_asset.url
        task.charged_credits = previous_charged if no_charge_retry else charge
        task.completed_at = now()
        wallet.frozen_credits = max(0, wallet.frozen_credits - task.frozen_credits)
        wallet.credits = max(0, wallet.credits - charge)
        if charge:
            add_ledger(db, task.user_id, "charge", -charge, f"{tool['name']} 扣费完成", task.id)
        auto_zip_batch_id = _completed_internal_batch_id_for_auto_zip(db, task)

    if status == "failed" and task.status not in {"succeeded", "failed", "cancelled"}:
        task.status = "failed"
        task.error_code = error_code or "PROVIDER_FAILED"
        if progress_stage is None:
            task.progress_stage = "处理失败，积分已释放"
        task.completed_at = now()
        wallet.frozen_credits = max(0, wallet.frozen_credits - task.frozen_credits)
        add_ledger(db, task.user_id, "refund", 0, f"{tool['name']} 失败，释放 {task.frozen_credits} 积分", task.id)

    db.commit()
    db.refresh(task)
    if auto_zip_batch_id:
        _enqueue_internal_batch_zip_after_commit(task.user_id, auto_zip_batch_id)
    return False, task
