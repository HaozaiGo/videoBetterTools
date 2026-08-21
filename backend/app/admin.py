import json
import re
import time
import urllib.error
import urllib.request
from hashlib import sha256
from pathlib import Path
from urllib.parse import quote
from urllib.parse import urljoin, urlparse

from fastapi import HTTPException
from sqlalchemy import case, func, or_, select
from sqlalchemy.orm import Session, selectinload

from app.config import settings
from app.models import Asset, Task, User, Wallet, WalletLedger
from app.queue import enqueue_internal_batch_zip, internal_batch_zip_queue
from app.services import add_ledger, create_task, failure_reason_for_task, get_tool, get_wallet, internal_batch_expected_total_from_name, internal_batch_expected_total_from_tasks, internal_batch_status, normalize_pagination, now, page_info, ledger_to_dict, plan_internal_batch_zip, serialize_datetime, task_result_missing_reason, task_to_dict
from app.storage import safe_storage_name, storage


def _failure_uncleared_filter():
    return Task.params["failureClearedAt"].as_string().is_(None)


def admin_summary(db: Session) -> dict:
    charged = db.execute(
        select(func.coalesce(func.sum(WalletLedger.amount), 0)).where(WalletLedger.type == "charge")
    ).scalar_one()
    failed_tasks = db.execute(
        select(func.count()).select_from(Task).where(Task.status == "failed", _failure_uncleared_filter())
    ).scalar_one()
    cleared_failed_tasks = db.execute(
        select(func.count()).select_from(Task).where(Task.status == "failed", Task.params["failureClearedAt"].as_string().is_not(None))
    ).scalar_one()
    return {
        "users": db.execute(select(func.count()).select_from(User)).scalar_one(),
        "tasks": db.execute(select(func.count()).select_from(Task)).scalar_one(),
        "assets": db.execute(select(func.count()).select_from(Asset)).scalar_one(),
        "creditsCharged": abs(int(charged or 0)),
        "queuedTasks": db.execute(select(func.count()).select_from(Task).where(Task.status == "queued")).scalar_one(),
        "processingTasks": db.execute(select(func.count()).select_from(Task).where(Task.status == "processing")).scalar_one(),
        "failedTasks": failed_tasks,
        "clearedFailedTasks": cleared_failed_tasks,
        "zipJobs": _zip_queue_jobs(db),
    }


def admin_clear_failed_task_count(db: Session) -> dict:
    tasks = list(db.execute(select(Task).where(Task.status == "failed", _failure_uncleared_filter())).scalars())
    cleared_at = serialize_datetime(now())
    for task in tasks:
        params = dict(task.params or {})
        params["failureClearedAt"] = cleared_at
        task.params = params
    db.commit()
    remaining = db.execute(
        select(func.count()).select_from(Task).where(Task.status == "failed", _failure_uncleared_filter())
    ).scalar_one()
    total_cleared = db.execute(
        select(func.count()).select_from(Task).where(Task.status == "failed", Task.params["failureClearedAt"].as_string().is_not(None))
    ).scalar_one()
    return {"cleared": len(tasks), "remainingFailedTasks": remaining, "clearedFailedTasks": total_cleared}


def admin_users(db: Session) -> list[dict]:
    rows = db.execute(select(User, Wallet).join(Wallet, Wallet.user_id == User.id).order_by(User.created_at.desc())).all()
    return [
        {
            "id": user.id,
            "email": user.email,
            "name": user.name,
            "role": user.role,
            "status": user.status,
            "credits": wallet.credits,
            "frozenCredits": wallet.frozen_credits,
            "createdAt": int(user.created_at.timestamp() * 1000),
        }
        for user, wallet in rows
    ]


def admin_tasks(db: Session, page: int = 1, per_page: int = 50) -> dict:
    page, per_page = normalize_pagination(page, per_page)
    total = db.execute(select(func.count()).select_from(Task)).scalar_one()
    tasks = db.execute(
        select(Task)
        .options(selectinload(Task.input_asset))
        .order_by(Task.created_at.desc())
        .offset((page - 1) * per_page)
        .limit(per_page)
    ).scalars()
    return {"items": [task_to_dict(task) for task in tasks], "page": page_info(total, page, per_page)}


def _internal_batch_status_from_counts(batch: dict) -> str:
    if int(batch["processing"]) > 0:
        return "processing"
    if int(batch["failed"]) + int(batch["cancelled"]) > 0:
        return "failed"
    if int(batch["succeeded"]) >= int(batch["total"]):
        return "succeeded"
    return "processing"


def admin_internal_batches(db: Session, page: int = 1, per_page: int = 50, status: str = "all", name: str = "") -> dict:
    page, per_page = normalize_pagination(page, per_page)
    status = status if status in ADMIN_INTERNAL_BATCH_STATUS_FILTERS else "all"
    normalized_name = name.strip()
    batch_id_expr = Task.params["internalBatchId"].as_string()
    batch_name_expr = Task.params["internalBatchName"].as_string()
    batch_total_expr = Task.params["internalBatchTotal"].as_string()
    updated_expr = func.coalesce(Task.completed_at, Task.created_at)
    filters = [
        Task.tool_slug == "subtitle-translate-workflow",
        batch_id_expr.is_not(None),
        batch_id_expr != "",
        Task.params["internalBatchDeletedAt"].as_string().is_(None),
    ]
    if normalized_name:
        filters.append(batch_name_expr.ilike(f"%{normalized_name}%"))
    rows = db.execute(
        select(
            Task.user_id.label("user_id"),
            batch_id_expr.label("batch_id"),
            func.max(batch_name_expr).label("batch_name"),
            func.max(batch_total_expr).label("declared_total"),
            func.count().label("total"),
            func.sum(case((Task.status == "succeeded", 1), else_=0)).label("succeeded"),
            func.sum(case((Task.status == "failed", 1), else_=0)).label("failed"),
            func.sum(case((Task.status == "cancelled", 1), else_=0)).label("cancelled"),
            func.sum(case((Task.status.in_(("queued", "processing")), 1), else_=0)).label("processing"),
            func.min(Task.created_at).label("created_at"),
            func.max(updated_expr).label("updated_at"),
        )
        .where(*filters)
        .group_by(Task.user_id, batch_id_expr)
        .order_by(func.max(updated_expr).desc())
    ).all()
    batches: list[dict] = []
    tab_counts = {"all": 0, "processing": 0, "succeeded": 0, "failed": 0}
    for row in rows:
        created_total = int(row.total or 0)
        batch_name = row.batch_name or row.batch_id or "内部批量任务"
        expected_total = _batch_expected_total(str(batch_name), created_total, row.declared_total)
        missing = max(0, expected_total - created_total)
        active_processing = int(row.processing or 0)
        batch = {
            "userId": row.user_id,
            "batchId": row.batch_id,
            "batchName": batch_name,
            "total": expected_total,
            "created": created_total,
            "succeeded": int(row.succeeded or 0),
            "failed": int(row.failed or 0),
            "cancelled": int(row.cancelled or 0),
            "missing": missing,
            "activeProcessing": active_processing,
            "processing": active_processing + missing,
            "createdAt": int(row.created_at.timestamp() * 1000),
            "updatedAt": int(row.updated_at.timestamp() * 1000),
        }
        batch["status"] = _internal_batch_status_from_counts(batch)
        tab_counts["all"] += 1
        tab_counts[str(batch["status"])] += 1
        if status == "all" or batch["status"] == status:
            batches.append(batch)
    total = len(batches)
    start = (page - 1) * per_page
    return {"items": batches[start : start + per_page], "page": page_info(total, page, per_page), "tabs": tab_counts}


def _batch_id_for_task(task: Task) -> str:
    params = task.params if isinstance(task.params, dict) else {}
    return str(params.get("internalBatchId") or "").strip()


def _batch_name_for_task(task: Task, batch_id: str) -> str:
    params = task.params if isinstance(task.params, dict) else {}
    return str(params.get("internalBatchName") or batch_id or "内部批量任务")


def _zip_part_source(part: dict) -> str:
    path = Path(str(part.get("path") or ""))
    if path.with_suffix(path.suffix + ".remote.json").exists():
        return "tos"
    if path.exists() and path.is_file() and path.stat().st_size > 0:
        return "local"
    return ""


def _zip_base_stem_for_batch(batch: dict) -> str:
    safe_batch_name = safe_storage_name(str(batch.get("batchName") or batch.get("batchId") or "内部批量任务")).removesuffix(".zip")
    return f"{safe_batch_name}-{str(batch['batchId'])[:8]}-{int(batch['succeeded'])}-of-{int(batch['total'])}"


def _read_zip_remote_marker(zip_path: Path) -> dict | None:
    marker_path = zip_path.with_suffix(zip_path.suffix + ".remote.json")
    if not marker_path.exists():
        return None
    try:
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not marker.get("url") or int(marker.get("sizeBytes") or 0) <= 0:
        return None
    return marker


def _zip_storage_names() -> list[str]:
    zip_dir = settings.upload_path / "internal-batch-zips"
    if not zip_dir.exists():
        return []
    return [path.name for path in zip_dir.iterdir()]


def _zip_storage_name_index(zip_names: list[str] | None = None) -> dict[str, list[str]]:
    names = zip_names if zip_names is not None else _zip_storage_names()
    index: dict[str, list[str]] = {}
    for name in names:
        zip_name = name.removesuffix(".remote.json")
        if not zip_name.endswith(".zip"):
            continue
        stem = zip_name.removesuffix(".zip")
        stem = re.sub(r"-part\d+-of\d+$", "", stem)
        index.setdefault(stem, []).append(name)
    return index


def _available_zip_paths_for_batch(batch: dict, zip_names: list[str] | dict[str, list[str]] | None = None) -> list[Path]:
    zip_dir = settings.upload_path / "internal-batch-zips"
    base_stem = _zip_base_stem_for_batch(batch)
    if isinstance(zip_names, dict):
        names = zip_names.get(base_stem, [])
    else:
        names = zip_names if zip_names is not None else _zip_storage_names()
    paths: dict[str, Path] = {}
    single_name = f"{base_stem}.zip"
    for name in names:
        if name == single_name or (name.startswith(f"{base_stem}-part") and name.endswith(".zip")):
            paths[name] = zip_dir / name
        elif name == f"{single_name}.remote.json":
            paths[single_name] = zip_dir / single_name
        elif name.startswith(f"{base_stem}-part") and name.endswith(".zip.remote.json"):
            paths[name.removesuffix(".remote.json")] = zip_dir / name.removesuffix(".remote.json")
    return sorted(paths.values(), key=lambda path: path.name)


def _zip_part_numbers(path: Path) -> tuple[int, int]:
    match = re.search(r"-part(\d+)-of(\d+)\.zip$", path.name)
    if not match:
        return 1, 1
    return int(match.group(1)), int(match.group(2))


def _ready_zip_parts_for_batch(batch: dict, zip_paths: list[Path] | None = None) -> list[dict]:
    parts: list[dict] = []
    safe_batch_name = safe_storage_name(str(batch.get("batchName") or batch.get("batchId") or "内部批量任务")).removesuffix(".zip")
    available_paths = zip_paths if zip_paths is not None else _available_zip_paths_for_batch(batch)
    for zip_path in available_paths:
        part_index, part_count = _zip_part_numbers(zip_path)
        marker = _read_zip_remote_marker(zip_path)
        if marker:
            source = "tos"
            size_bytes = int(marker.get("sizeBytes") or 0)
            storage_key = str(marker.get("storageKey") or "")
        elif zip_path.exists() and zip_path.is_file() and zip_path.stat().st_size > 0:
            source = "local"
            size_bytes = int(zip_path.stat().st_size)
            storage_key = ""
        else:
            continue
        if size_bytes <= 0:
            continue
        filename = f"{safe_batch_name}.zip" if part_count == 1 else f"{safe_batch_name}-part{part_index:02d}-of{part_count:02d}.zip"
        parts.append(
            {
                "index": part_index,
                "partCount": part_count,
                "filename": filename,
                "sizeBytes": size_bytes,
                "estimatedSizeBytes": size_bytes,
                "source": source,
                "storageKey": storage_key,
            }
        )
    return parts


ADMIN_ZIP_STATUS_FILTERS = {"ready", "processing", "failed"}
ADMIN_INTERNAL_BATCH_STATUS_FILTERS = {"all", "processing", "succeeded", "failed"}
SKIPPED_TASK_STATUSES = {"failed", "cancelled", "queued", "processing"}


def _serialize_job_time(value) -> int | None:
    return int(value.timestamp() * 1000) if value else None


def _zip_job_states(include_failed: bool = True) -> dict[tuple[str, str], dict]:
    states: dict[tuple[str, str], dict] = {}
    try:
        queue = internal_batch_zip_queue()
        registry_sets = [
            ("queued", queue.get_job_ids()),
            ("started", queue.started_job_registry.get_job_ids()),
            ("scheduled", queue.scheduled_job_registry.get_job_ids()),
        ]
        if include_failed:
            registry_sets.append(("failed", queue.failed_job_registry.get_job_ids()))
    except Exception:
        return states
    priority = {"queued": 1, "scheduled": 2, "failed": 3, "started": 4}
    for state, job_ids in registry_sets:
        for position, job_id in enumerate(job_ids, start=1):
            try:
                job = queue.fetch_job(job_id)
            except Exception:
                continue
            if not job or len(job.args) < 2:
                continue
            user_id, batch_id = str(job.args[0]), str(job.args[1])
            key = (user_id, batch_id)
            current = states.get(key)
            if current and priority.get(str(current["state"]), 0) >= priority[state]:
                continue
            states[key] = {
                "state": state,
                "position": position if state == "queued" else None,
                "jobId": job_id,
                "retriesLeft": getattr(job, "retries_left", None),
                "createdAt": _serialize_job_time(getattr(job, "created_at", None)),
                "startedAt": _serialize_job_time(getattr(job, "started_at", None)),
                "endedAt": _serialize_job_time(getattr(job, "ended_at", None)),
                "excInfo": str(getattr(job, "exc_info", "") or "")[-800:] if state == "failed" else "",
            }
    return states


def _zip_process_message(batch: dict, zip_status: str, zip_job: dict | None) -> tuple[str, str]:
    processing = int(batch["processing"])
    missing = int(batch.get("missing") or 0)
    active_processing = int(batch.get("activeProcessing") or max(0, processing - missing))
    succeeded = int(batch["succeeded"])
    total = int(batch["total"])
    failed = int(batch["failed"])
    cancelled = int(batch["cancelled"])
    missing_results = int(batch.get("missingResultCount") or 0)
    if missing_results > 0:
        return "failed", f"已有成功任务的结果文件缺失：{missing_results} 个；请先在内部任务里重跑缺失集后再打包"
    if missing > 0 and active_processing <= 0:
        return "failed", f"批次任务数不完整：缺少 {missing} 个任务，当前已创建 {batch.get('created', total - missing)}/{total}；请补传或重新创建完整批次"
    if processing > 0:
        suffix = f"，缺少 {missing} 个未创建任务" if missing else ""
        return "tasks", f"任务还没全部完成：{processing} 个仍未完成，已成功 {succeeded}/{total}{suffix}"
    if zip_job:
        state = str(zip_job.get("state") or "")
        if state == "queued":
            position = int(zip_job.get("position") or 0)
            return "queued", f"等待 ZIP worker：队列前面约 {max(0, position - 1)} 个任务"
        if state == "started":
            return "gpu", "ZIP worker 已接单：GPU 正在打包或上传 TOS"
        if state == "scheduled":
            retries = zip_job.get("retriesLeft")
            suffix = f"，剩余重试 {retries} 次" if retries is not None else ""
            return "retry", f"ZIP 任务等待自动重试{suffix}"
        if state == "failed":
            exc_info = str(zip_job.get("excInfo") or "")
            if "结果文件缺失" in exc_info:
                return "failed", "ZIP 打包失败：存在成功但结果文件已缺失的单集，请查看详情并重跑"
            if "404" in exc_info and ("TOS" in exc_info or "object" in exc_info or "urlopen" in exc_info):
                return "failed", "ZIP 打包失败：结果文件读取 404，请查看详情并重跑缺失集"
            return "failed", "ZIP 打包/上传失败，等待重新入队"
    if zip_status == "failed":
        return "failed", f"批次有失败/取消任务：失败 {failed}，取消 {cancelled}；需重试或补包"
    if succeeded <= 0:
        return "tasks", "还没有成功结果可打包"
    return "waiting", "批次已有成功结果，等待自动入 ZIP 队列"


def _zip_queue_jobs(db: Session, limit: int = 10) -> list[dict]:
    zip_jobs = {
        key: job
        for key, job in _zip_job_states(include_failed=False).items()
        if str(job.get("state") or "") in {"queued", "started", "scheduled"}
    }
    if not zip_jobs:
        return []

    batch_ids = {batch_id for _user_id, batch_id in zip_jobs}
    user_ids = {user_id for user_id, _batch_id in zip_jobs}
    batch_id_expr = Task.params["internalBatchId"].as_string()
    batch_name_expr = Task.params["internalBatchName"].as_string()
    batch_total_expr = Task.params["internalBatchTotal"].as_string()
    updated_expr = func.coalesce(Task.completed_at, Task.created_at)
    rows = db.execute(
        select(
            Task.user_id.label("user_id"),
            batch_id_expr.label("batch_id"),
            func.max(batch_name_expr).label("batch_name"),
            func.max(batch_total_expr).label("declared_total"),
            func.count().label("total"),
            func.sum(case((Task.status == "succeeded", 1), else_=0)).label("succeeded"),
            func.sum(case((Task.status == "failed", 1), else_=0)).label("failed"),
            func.sum(case((Task.status == "cancelled", 1), else_=0)).label("cancelled"),
            func.sum(case((Task.status.in_(("queued", "processing")), 1), else_=0)).label("processing"),
            func.min(Task.created_at).label("created_at"),
            func.max(updated_expr).label("updated_at"),
        )
        .where(
            Task.tool_slug == "subtitle-translate-workflow",
            Task.user_id.in_(user_ids),
            batch_id_expr.in_(batch_ids),
            Task.params["internalBatchDeletedAt"].as_string().is_(None),
        )
        .group_by(Task.user_id, batch_id_expr)
    ).all()

    items: list[dict] = []
    for row in rows:
        key = (str(row.user_id), str(row.batch_id))
        zip_job = zip_jobs.get(key)
        if not zip_job:
            continue
        created_total = int(row.total or 0)
        batch_name = str(row.batch_name or row.batch_id or "内部批量任务")
        expected_total = _batch_expected_total(batch_name, created_total, row.declared_total)
        missing = max(0, expected_total - created_total)
        active_processing = int(row.processing or 0)
        batch = {
            "userId": row.user_id,
            "batchId": row.batch_id,
            "batchName": batch_name,
            "total": expected_total,
            "created": created_total,
            "succeeded": int(row.succeeded or 0),
            "failed": int(row.failed or 0),
            "cancelled": int(row.cancelled or 0),
            "missing": missing,
            "activeProcessing": active_processing,
            "processing": active_processing + missing,
            "createdAt": int(row.created_at.timestamp() * 1000),
            "updatedAt": int(row.updated_at.timestamp() * 1000),
        }
        stage, message = _zip_process_message(batch, "processing", zip_job)
        state = str(zip_job.get("state") or "")
        items.append(
            {
                **batch,
                "zipStage": stage,
                "message": message,
                "state": state,
                "position": zip_job.get("position"),
                "retriesLeft": zip_job.get("retriesLeft"),
                "jobId": zip_job.get("jobId"),
                "createdAt": int(row.created_at.timestamp() * 1000),
                "updatedAt": int(row.updated_at.timestamp() * 1000),
                "startedAt": zip_job.get("startedAt"),
            }
        )

    state_priority = {"started": 0, "queued": 1, "scheduled": 2}
    items.sort(
        key=lambda item: (
            state_priority.get(str(item["state"]), 9),
            int(item["position"] or 999999),
            -int(item["updatedAt"]),
        )
    )
    return items[: max(1, int(limit))]


def _batch_expected_total(batch_name: str, created_total: int, declared_total: object = None) -> int:
    declared = None
    try:
        declared = int(declared_total) if declared_total not in {None, ""} else None
    except (TypeError, ValueError):
        declared = None
    name_total = internal_batch_expected_total_from_name(batch_name)
    return max(value for value in (created_total, declared or 0, name_total or 0) if value >= 0)


def _zip_row_delete_marker_path(user_id: str, batch_id: str, part_index: int) -> Path:
    fingerprint = sha256(f"{user_id}\0{batch_id}\0{part_index}".encode("utf-8")).hexdigest()
    return settings.upload_path / "internal-batch-zips" / ".deleted" / f"{fingerprint}.json"


def _is_zip_row_deleted(user_id: str, batch_id: str, part_index: int) -> bool:
    return _zip_row_delete_marker_path(user_id, batch_id, part_index).exists()


def _mark_zip_row_deleted(user_id: str, batch_id: str, part_index: int) -> None:
    marker_path = _zip_row_delete_marker_path(user_id, batch_id, part_index)
    marker_path.parent.mkdir(parents=True, exist_ok=True)
    marker_path.write_text(
        json.dumps({"userId": user_id, "batchId": batch_id, "partIndex": part_index, "deletedAt": int(time.time() * 1000)}, ensure_ascii=False),
        encoding="utf-8",
    )


def _clear_zip_row_deleted(user_id: str, batch_id: str, part_index: int) -> None:
    _zip_row_delete_marker_path(user_id, batch_id, part_index).unlink(missing_ok=True)


def _task_episode_hint(task: Task, index: int) -> str:
    params = task.params if isinstance(task.params, dict) else {}
    for key in ("episode", "episodeNumber", "episodeIndex", "fileIndex", "index"):
        value = params.get(key)
        if value is None or value == "":
            continue
        try:
            number = int(value)
        except (TypeError, ValueError):
            return str(value)
        return str(number + 1 if key in {"episodeIndex", "fileIndex", "index"} and number <= index else number)
    return str(index)


def _skipped_tasks_for_batch(db: Session, user_id: str, batch_id: str) -> list[dict]:
    batch_id_expr = Task.params["internalBatchId"].as_string()
    tasks = list(db.execute(
        select(Task)
        .where(
            Task.user_id == user_id,
            Task.tool_slug == "subtitle-translate-workflow",
            batch_id_expr == batch_id,
        )
        .options(selectinload(Task.input_asset))
        .order_by(Task.created_at.asc())
    ).scalars())
    skipped_tasks = []
    for index, task in enumerate(tasks, start=1):
        result_missing_reason = task_result_missing_reason(task) if task.status == "succeeded" else ""
        if task.status not in SKIPPED_TASK_STATUSES and not result_missing_reason:
            continue
        skipped_tasks.append(
            {
            "taskId": task.id,
            "episode": _task_episode_hint(task, index),
            "inputAssetName": task.input_asset.original_name if task.input_asset else "",
            "status": task.status,
            "errorCode": task.error_code or "",
            "failureReason": result_missing_reason or failure_reason_for_task(task),
            "resultMissingReason": result_missing_reason,
            "progressStage": task.progress_stage or "",
            "createdAt": int(task.created_at.timestamp() * 1000),
            "completedAt": int(task.completed_at.timestamp() * 1000) if task.completed_at else None,
        }
        )
    return skipped_tasks


def _task_internal_batch_index(task: Task, fallback: int) -> int:
    params = task.params if isinstance(task.params, dict) else {}
    try:
        value = int(params.get("internalBatchIndex") or 0)
    except (TypeError, ValueError):
        value = 0
    return value if value > 0 else fallback


def _template_task_for_missing_episode(tasks: list[Task], episode: int) -> Task:
    indexed = [(_task_internal_batch_index(task, index), task) for index, task in enumerate(tasks, start=1)]
    return min(indexed, key=lambda item: (abs(item[0] - episode), item[0]))[1]


def _batch_name_from_tasks(tasks: list[Task]) -> str:
    for task in tasks:
        params = task.params if isinstance(task.params, dict) else {}
        batch_name = str(params.get("internalBatchName") or "").strip()
        if batch_name:
            return batch_name
    return "内部批量任务"


def _internal_batch_tasks_for_admin(db: Session, user_id: str, batch_id: str) -> list[Task]:
    batch_id_expr = Task.params["internalBatchId"].as_string()
    deleted_expr = Task.params["internalBatchDeletedAt"].as_string()
    return list(
        db.execute(
            select(Task)
            .where(
                Task.user_id == user_id,
                Task.tool_slug == "subtitle-translate-workflow",
                batch_id_expr == batch_id,
                deleted_expr.is_(None),
            )
            .options(selectinload(Task.input_asset))
            .order_by(Task.created_at.asc())
        ).scalars()
    )


def admin_create_internal_batch_missing_task(db: Session, user_id: str, batch_id: str, input_asset_id: str, episode: int, duration_seconds: int = 0) -> dict:
    tasks = _internal_batch_tasks_for_admin(db, user_id, batch_id)
    if not tasks:
        raise HTTPException(status_code=404, detail="batch not found")
    expected_total = internal_batch_expected_total_from_tasks(tasks)
    if episode < 1 or episode > expected_total:
        raise HTTPException(status_code=400, detail=f"episode must be between 1 and {expected_total}")

    existing_indexes = {_task_internal_batch_index(task, index) for index, task in enumerate(tasks, start=1)}
    if episode in existing_indexes:
        raise HTTPException(status_code=409, detail=f"episode {episode} already has a task")

    template_task = _template_task_for_missing_episode(tasks, episode)
    template_params = template_task.params if isinstance(template_task.params, dict) else {}
    params = {
        key: value
        for key, value in template_params.items()
        if key not in {
            "providerJobId",
            "remoteGpuJobId",
            "remoteGpuJobIds",
            "remoteGpuJobType",
            "remoteGpuSubmittedAt",
            "singleGpuRetry",
            "singleGpuRetryAt",
        }
        and not str(key).startswith("_")
    }
    params["internalBatchId"] = batch_id
    params["internalBatchName"] = str(template_params.get("internalBatchName") or _batch_name_from_tasks(tasks))
    params["internalBatchTotal"] = expected_total
    params["internalBatchIndex"] = episode
    if duration_seconds > 0:
        params["duration"] = duration_seconds

    task = create_task(db, user_id, "subtitle-translate-workflow", input_asset_id, params, at_front=True)
    return {"task": task_to_dict(task), "batch": internal_batch_status(db, user_id, batch_id)}


def _empty_zip_batch_item(batch: dict, zip_status: str, archive: dict | None = None, zip_job: dict | None = None, skipped_tasks: list[dict] | None = None) -> dict:
    part = archive["parts"][0] if archive and archive.get("parts") else {}
    batch_id = str(batch["batchId"])
    user_id = str(batch["userId"])
    stage, message = _zip_process_message(batch, zip_status, zip_job)
    return {
        **batch,
        "zipStatus": zip_status,
        "zipStage": stage,
        "zipJob": zip_job,
        "partIndex": int(part.get("index") or 0),
        "partCount": int(archive.get("partCount") or 0) if archive else 0,
        "filename": str(part.get("filename") or ""),
        "sizeBytes": 0,
        "estimatedSizeBytes": int(part.get("estimatedSizeBytes") or 0),
        "source": "",
        "storageKey": str(part.get("storageKey") or ""),
        "downloadUrl": "",
        "message": message,
        "batchId": batch_id,
        "userId": user_id,
        "skippedTasks": skipped_tasks or [],
    }


def _batch_from_zip_row(row) -> dict:
    created_total = int(row.total or 0)
    batch_name = row.batch_name or row.batch_id or "内部批量任务"
    expected_total = _batch_expected_total(str(batch_name), created_total, row.declared_total)
    missing = max(0, expected_total - created_total)
    active_processing = int(row.processing or 0)
    return {
        "userId": row.user_id,
        "batchId": row.batch_id,
        "batchName": batch_name,
        "total": expected_total,
        "created": created_total,
        "succeeded": int(row.succeeded or 0),
        "failed": int(row.failed or 0),
        "cancelled": int(row.cancelled or 0),
        "missing": missing,
        "activeProcessing": active_processing,
        "processing": active_processing + missing,
        "createdAt": int(row.created_at.timestamp() * 1000),
        "updatedAt": int(row.updated_at.timestamp() * 1000),
    }


def _zip_batch_id_prefixes(zip_names: dict[str, list[str]]) -> set[str]:
    prefixes: set[str] = set()
    for stem in zip_names:
        match = re.search(r"-([0-9a-fA-F]{8})-\d+-of-\d+$", stem)
        if match:
            prefixes.add(match.group(1).lower())
    return prefixes


def _admin_internal_batch_ready_zips(db: Session, page: int, per_page: int, name: str) -> dict | None:
    zip_names = _zip_storage_name_index()
    prefixes = _zip_batch_id_prefixes(zip_names)
    if not prefixes:
        if zip_names:
            return None
        return {"items": [], "page": page_info(0, page, per_page), "tabs": {"ready": 0, "processing": 0, "failed": 0}}

    batch_id_expr = Task.params["internalBatchId"].as_string()
    batch_name_expr = Task.params["internalBatchName"].as_string()
    batch_total_expr = Task.params["internalBatchTotal"].as_string()
    updated_expr = func.coalesce(Task.completed_at, Task.created_at)
    filters = [
        Task.tool_slug == "subtitle-translate-workflow",
        batch_id_expr.is_not(None),
        batch_id_expr != "",
        func.substr(func.lower(batch_id_expr), 1, 8).in_(prefixes),
        Task.params["internalBatchDeletedAt"].as_string().is_(None),
    ]
    if name:
        filters.append(batch_name_expr.ilike(f"%{name}%"))

    rows = db.execute(
        select(
            Task.user_id.label("user_id"),
            batch_id_expr.label("batch_id"),
            func.max(batch_name_expr).label("batch_name"),
            func.max(batch_total_expr).label("declared_total"),
            func.count().label("total"),
            func.sum(case((Task.status == "succeeded", 1), else_=0)).label("succeeded"),
            func.sum(case((Task.status == "failed", 1), else_=0)).label("failed"),
            func.sum(case((Task.status == "cancelled", 1), else_=0)).label("cancelled"),
            func.sum(case((Task.status.in_(("queued", "processing")), 1), else_=0)).label("processing"),
            func.min(Task.created_at).label("created_at"),
            func.max(updated_expr).label("updated_at"),
        )
        .where(*filters)
        .group_by(Task.user_id, batch_id_expr)
        .order_by(func.max(updated_expr).desc())
    ).all()

    items: list[dict] = []
    for row in rows:
        batch = _batch_from_zip_row(row)
        user_id = str(batch["userId"])
        batch_id = str(batch["batchId"])
        available_zip_paths = _available_zip_paths_for_batch(batch, zip_names)
        for part in _ready_zip_parts_for_batch(batch, available_zip_paths):
            part_index = int(part["index"])
            if _is_zip_row_deleted(user_id, batch_id, part_index):
                continue
            items.append(
                {
                    **batch,
                    "zipStatus": "ready",
                    "zipStage": "ready",
                    "zipJob": None,
                    "partIndex": part_index,
                    "partCount": int(part.get("partCount") or 1),
                    "filename": part["filename"],
                    "sizeBytes": int(part.get("sizeBytes") or 0),
                    "estimatedSizeBytes": int(part.get("estimatedSizeBytes") or 0),
                    "source": str(part.get("source") or ""),
                    "storageKey": str(part.get("storageKey") or ""),
                    "downloadUrl": f"/api/admin/internal-batch-zips/{quote(batch_id, safe='')}/download?userId={quote(user_id, safe='')}&part={part_index}",
                    "message": "",
                    "skippedTasks": [],
                }
            )

    items.sort(key=lambda item: (int(item["updatedAt"]), str(item["batchId"]), int(item["partIndex"])), reverse=True)
    total = len(items)
    start = (page - 1) * per_page
    paged_items = items[start : start + per_page]
    for item in paged_items:
        unfinished = int(item["failed"]) + int(item["cancelled"]) + int(item["processing"])
        if unfinished > 0:
            item["skippedTasks"] = _skipped_tasks_for_batch(db, str(item["userId"]), str(item["batchId"]))
    return {"items": paged_items, "page": page_info(total, page, per_page), "tabs": {"ready": total, "processing": 0, "failed": 0}}


def admin_internal_batch_zips(db: Session, page: int = 1, per_page: int = 50, status: str = "ready", name: str = "") -> dict:
    page, per_page = normalize_pagination(page, per_page)
    status = status if status in ADMIN_ZIP_STATUS_FILTERS else "ready"
    normalized_name = name.strip()
    if status == "ready":
        ready_payload = _admin_internal_batch_ready_zips(db, page, per_page, normalized_name)
        if ready_payload is not None:
            return ready_payload
    batch_id_expr = Task.params["internalBatchId"].as_string()
    batch_name_expr = Task.params["internalBatchName"].as_string()
    batch_total_expr = Task.params["internalBatchTotal"].as_string()
    updated_expr = func.coalesce(Task.completed_at, Task.created_at)
    filters = [
        Task.tool_slug == "subtitle-translate-workflow",
        batch_id_expr.is_not(None),
        batch_id_expr != "",
        Task.params["internalBatchDeletedAt"].as_string().is_(None),
    ]
    if normalized_name:
        filters.append(batch_name_expr.ilike(f"%{normalized_name}%"))
    rows = db.execute(
        select(
            Task.user_id.label("user_id"),
            batch_id_expr.label("batch_id"),
            func.max(batch_name_expr).label("batch_name"),
            func.max(batch_total_expr).label("declared_total"),
            func.count().label("total"),
            func.sum(case((Task.status == "succeeded", 1), else_=0)).label("succeeded"),
            func.sum(case((Task.status == "failed", 1), else_=0)).label("failed"),
            func.sum(case((Task.status == "cancelled", 1), else_=0)).label("cancelled"),
            func.sum(case((Task.status.in_(("queued", "processing")), 1), else_=0)).label("processing"),
            func.min(Task.created_at).label("created_at"),
            func.max(updated_expr).label("updated_at"),
        )
        .where(*filters)
        .group_by(Task.user_id, batch_id_expr)
        .order_by(func.max(updated_expr).desc())
    ).all()
    batches = []
    for row in rows:
        created_total = int(row.total or 0)
        batch_name = row.batch_name or row.batch_id or "内部批量任务"
        expected_total = _batch_expected_total(str(batch_name), created_total, row.declared_total)
        missing = max(0, expected_total - created_total)
        active_processing = int(row.processing or 0)
        batches.append(
            {
                "userId": row.user_id,
                "batchId": row.batch_id,
                "batchName": batch_name,
                "total": expected_total,
                "created": created_total,
                "succeeded": int(row.succeeded or 0),
                "failed": int(row.failed or 0),
                "cancelled": int(row.cancelled or 0),
                "missing": missing,
                "activeProcessing": active_processing,
                "processing": active_processing + missing,
                "createdAt": int(row.created_at.timestamp() * 1000),
                "updatedAt": int(row.updated_at.timestamp() * 1000),
            }
        )
    items: list[dict] = []
    tab_counts = {"ready": 0, "processing": 0, "failed": 0}
    zip_jobs = _zip_job_states()
    zip_names = _zip_storage_name_index()
    verify_missing_results = status in {"processing", "failed"}
    for batch in batches:
        ready_items: list[dict] = []
        user_id = str(batch["userId"])
        batch_id = str(batch["batchId"])
        can_have_ready_zip = int(batch["succeeded"]) > 0 and int(batch.get("missing") or 0) <= 0 and int(batch.get("activeProcessing") or 0) <= 0
        available_zip_paths = _available_zip_paths_for_batch(batch, zip_names) if can_have_ready_zip else []
        if can_have_ready_zip:
            for part in _ready_zip_parts_for_batch(batch, available_zip_paths):
                part_index = int(part["index"])
                if _is_zip_row_deleted(user_id, batch_id, part_index):
                    continue
                ready_items.append(
                    {
                        **batch,
                        "zipStatus": "ready",
                        "zipStage": "ready",
                        "zipJob": None,
                        "partIndex": part_index,
                        "partCount": int(part.get("partCount") or 1),
                        "filename": part["filename"],
                        "sizeBytes": int(part.get("sizeBytes") or 0),
                        "estimatedSizeBytes": int(part.get("estimatedSizeBytes") or 0),
                        "source": str(part.get("source") or ""),
                        "storageKey": str(part.get("storageKey") or ""),
                        "downloadUrl": f"/api/admin/internal-batch-zips/{quote(batch_id, safe='')}/download?userId={quote(user_id, safe='')}&part={part_index}",
                        "message": "",
                        "skippedTasks": [],
                    }
                )
        if ready_items:
            tab_counts["ready"] += len(ready_items)
            if status == "ready":
                items.extend(ready_items)
            continue
        if available_zip_paths:
            continue
        zip_job = zip_jobs.get((user_id, batch_id))
        missing_result_tasks: list[dict] = []
        if can_have_ready_zip and not zip_job and verify_missing_results:
            missing_result_tasks = [task for task in _skipped_tasks_for_batch(db, user_id, batch_id) if task.get("resultMissingReason")]
            if missing_result_tasks:
                batch = {**batch, "missingResultCount": len(missing_result_tasks)}
        pending_status = "processing"
        if missing_result_tasks:
            pending_status = "failed"
        elif zip_job and zip_job.get("state") == "failed":
            pending_status = "failed"
        elif int(batch.get("missing") or 0) > 0 and int(batch.get("activeProcessing") or 0) <= 0:
            pending_status = "failed"
        elif int(batch["failed"]) + int(batch["cancelled"]) > 0 and int(batch["processing"]) <= 0:
            pending_status = "failed"
        pending_part_index = 1 if can_have_ready_zip else 0
        if _is_zip_row_deleted(user_id, batch_id, pending_part_index):
            continue
        tab_counts[pending_status] += 1
        if status == pending_status:
            items.append(_empty_zip_batch_item(batch, pending_status, None, zip_job, missing_result_tasks))

    items.sort(key=lambda item: (int(item["updatedAt"]), str(item["batchId"]), int(item["partIndex"])), reverse=True)
    total = len(items)
    start = (page - 1) * per_page
    paged_items = items[start : start + per_page]
    for item in paged_items:
        if not item.get("skippedTasks"):
            item["skippedTasks"] = _skipped_tasks_for_batch(db, str(item["userId"]), str(item["batchId"]))
    return {"items": paged_items, "page": page_info(total, page, per_page), "tabs": tab_counts}


def _delete_zip_part_file(part: dict) -> tuple[bool, str]:
    zip_path = Path(str(part.get("path") or ""))
    marker_path = zip_path.with_suffix(zip_path.suffix + ".remote.json")
    deleted = False
    if marker_path.exists():
        marker_storage_key = ""
        try:
            marker = json.loads(marker_path.read_text(encoding="utf-8"))
            marker_storage_key = str(marker.get("storageKey") or "").strip()
        except (OSError, json.JSONDecodeError):
            marker_storage_key = ""
        if marker_storage_key and storage.is_remote and not storage.delete_remote(marker_storage_key):
            return False, "远端 ZIP 删除失败"
        marker_path.unlink(missing_ok=True)
        deleted = True
    if zip_path.exists() and zip_path.is_file():
        zip_path.unlink(missing_ok=True)
        deleted = True
    return deleted, "" if deleted else "ZIP 文件不存在或已删除"


def admin_delete_internal_batch_zips(db: Session, items: list[dict]) -> dict:
    deleted = 0
    missing = 0
    failed: list[dict] = []
    seen: set[tuple[str, str, int]] = set()
    for item in items:
        user_id = str(item.get("userId") or "").strip()
        batch_id = str(item.get("batchId") or "").strip()
        part_index = int(item.get("partIndex") or 0)
        key = (user_id, batch_id, part_index)
        if not user_id or not batch_id or part_index < 0 or key in seen:
            continue
        seen.add(key)
        try:
            try:
                archive = plan_internal_batch_zip(db, user_id, batch_id)
            except Exception:
                archive = None
            if archive and archive.get("parts"):
                if part_index == 0:
                    part_index = int(archive["parts"][0].get("index") or 0)
                    key = (user_id, batch_id, part_index)
                part = next((candidate for candidate in archive["parts"] if int(candidate.get("index") or 0) == part_index), None)
                if part is not None:
                    did_delete, message = _delete_zip_part_file(part)
                    if not did_delete:
                        missing += 1
                        if message and message != "ZIP 文件不存在或已删除":
                            failed.append({"userId": user_id, "batchId": batch_id, "partIndex": part_index, "message": message})
            _mark_zip_row_deleted(user_id, batch_id, part_index)
            deleted += 1
        except Exception as exc:
            failed.append({"userId": user_id, "batchId": batch_id, "partIndex": part_index, "message": str(exc)})
    return {"deleted": deleted, "missing": missing, "failed": failed}


def admin_regenerate_internal_batch_zip(db: Session, user_id: str, batch_id: str) -> dict:
    archive = plan_internal_batch_zip(db, user_id, batch_id)
    deleted = 0
    missing = 0
    failed: list[dict] = []
    for part in archive.get("parts", []):
        part_index = int(part.get("index") or 0)
        if part_index <= 0:
            continue
        _clear_zip_row_deleted(user_id, batch_id, part_index)
        did_delete, message = _delete_zip_part_file(part)
        if did_delete:
            deleted += 1
        elif message == "ZIP 文件不存在或已删除":
            missing += 1
        elif message:
            failed.append({"userId": user_id, "batchId": batch_id, "partIndex": part_index, "message": message})
    if failed:
        return {"queued": False, "deleted": deleted, "missing": missing, "failed": failed, "partCount": int(archive.get("partCount") or 0)}
    enqueue_internal_batch_zip(user_id, batch_id, at_front=True)
    return {"queued": True, "deleted": deleted, "missing": missing, "failed": [], "partCount": int(archive.get("partCount") or 0)}


def _delete_internal_batch_once(db: Session, user_id: str, batch_id: str) -> dict:
    batch_id_expr = Task.params["internalBatchId"].as_string()
    tasks = list(
        db.execute(
            select(Task)
            .where(
                Task.user_id == user_id,
                Task.tool_slug == "subtitle-translate-workflow",
                batch_id_expr == batch_id,
                Task.params["internalBatchDeletedAt"].as_string().is_(None),
            )
            .with_for_update()
        ).scalars()
    )
    if not tasks:
        raise HTTPException(status_code=404, detail="batch not found")

    deleted_at = serialize_datetime(now())
    wallet = get_wallet(db, user_id, lock=True)
    released = 0
    cancelled = 0
    for task in tasks:
        params = dict(task.params or {})
        params["internalBatchDeletedAt"] = deleted_at
        params["taskListDeletedAt"] = deleted_at
        task.params = params
        if task.status in {"queued", "processing"}:
            tool = get_tool(task.tool_slug) or {"name": task.tool_slug}
            released += int(task.frozen_credits or 0)
            cancelled += 1
            task.status = "cancelled"
            task.error_code = "ADMIN_DELETED"
            task.progress_percent = 0
            task.progress_stage = "管理员手动删除内部批次，任务已取消"
            task.completed_at = now()
            add_ledger(db, user_id, "refund", 0, f"{tool['name']} 已删除，释放 {task.frozen_credits} 积分", task.id)
            cancel_marker = settings.upload_path / f"{task.id}.cancel"
            cancel_marker.parent.mkdir(parents=True, exist_ok=True)
            cancel_marker.write_text("cancelled", encoding="utf-8")
    wallet.frozen_credits = max(0, wallet.frozen_credits - released)
    db.commit()
    return {"deleted": len(tasks), "cancelled": cancelled, "releasedCredits": released}


def admin_delete_internal_batch(db: Session, user_id: str, batch_id: str) -> dict:
    return _delete_internal_batch_once(db, user_id, batch_id)


def admin_delete_internal_batches(db: Session, items: list[dict]) -> dict:
    deleted = 0
    cancelled = 0
    released_credits = 0
    failed: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for item in items:
        user_id = str(item.get("userId") or "").strip()
        batch_id = str(item.get("batchId") or "").strip()
        key = (user_id, batch_id)
        if not user_id or not batch_id or key in seen:
            continue
        seen.add(key)
        try:
            payload = _delete_internal_batch_once(db, user_id, batch_id)
            deleted += int(payload.get("deleted") or 0)
            cancelled += int(payload.get("cancelled") or 0)
            released_credits += int(payload.get("releasedCredits") or 0)
        except Exception as exc:
            failed.append({"userId": user_id, "batchId": batch_id, "message": str(exc)})
    return {"deleted": deleted, "cancelled": cancelled, "releasedCredits": released_credits, "failed": failed}


def admin_ledger(db: Session) -> list[dict]:
    ledger = db.execute(select(WalletLedger).order_by(WalletLedger.created_at.desc()).limit(200)).scalars()
    return [ledger_to_dict(entry) for entry in ledger]


def _gpu_job_lookup_keys(job: dict) -> list[str]:
    keys: list[str] = []

    def add(value: object) -> None:
        normalized = str(value or "").strip()
        if normalized and normalized not in keys:
            keys.append(normalized)

    for field in ("id", "providerJobId", "provider_job_id", "localJobId", "local_job_id", "taskProviderJobId"):
        add(job.get(field))
    for container_name in ("params", "metadata"):
        container = job.get(container_name)
        if not isinstance(container, dict):
            continue
        for field in ("providerJobId", "provider_job_id", "remoteGpuJobId", "remote_gpu_job_id"):
            add(container.get(field))
    return keys


def _gpu_job_display_map(db: Session, job_ids: list[str]) -> dict[str, dict]:
    normalized_job_ids = [job_id for job_id in dict.fromkeys(job_ids) if job_id]
    if not normalized_job_ids:
        return {}
    remote_job_id_expr = Task.params["remoteGpuJobId"].as_string()
    rows = db.execute(
        select(Task)
        .where(or_(Task.provider_job_id.in_(normalized_job_ids), remote_job_id_expr.in_(normalized_job_ids)))
        .options(selectinload(Task.input_asset))
    ).scalars()
    display_map: dict[str, dict] = {}
    for task in rows:
        params = task.params if isinstance(task.params, dict) else {}
        display_map[task.provider_job_id] = _gpu_task_display_payload(task)
        remote_job_id = str(params.get("remoteGpuJobId") or "").strip()
        if remote_job_id:
            display_map[remote_job_id] = display_map[task.provider_job_id]
        remote_job_id_items = params.get("remoteGpuJobIds") if isinstance(params.get("remoteGpuJobIds"), list) else []
        for remote_job_id_item in remote_job_id_items:
            remote_job_id_value = str(remote_job_id_item or "").strip()
            if remote_job_id_value:
                display_map[remote_job_id_value] = display_map[task.provider_job_id]
    return display_map


def _gpu_task_display_payload(task: Task) -> dict:
    params = task.params if isinstance(task.params, dict) else {}
    batch_name = str(params.get("internalBatchName") or "").strip()
    input_name = task.input_asset.original_name if task.input_asset else ""
    title = batch_name or input_name or task.tool_slug
    try:
        batch_index = int(params.get("internalBatchIndex") or 0)
    except (TypeError, ValueError):
        batch_index = 0
    subtitle_parts = []
    if batch_index > 0:
        subtitle_parts.append(f"第 {batch_index} 集")
    if input_name:
        subtitle_parts.append(input_name)
    return {
        "taskId": task.id,
        "taskStatus": task.status,
        "toolSlug": task.tool_slug,
        "inputAssetName": input_name,
        "internalBatchId": str(params.get("internalBatchId") or ""),
        "internalBatchName": batch_name,
        "displayName": title,
        "displaySubtitle": " · ".join(subtitle_parts) or task.provider_job_id,
    }


def _queued_gpu_jobs(db: Session, limit: int = 10) -> list[dict]:
    tasks = list(db.execute(
        select(Task)
        .where(Task.status == "queued")
        .options(selectinload(Task.input_asset))
        .order_by(Task.created_at.asc())
        .limit(max(limit * 8, 50))
    ).scalars())
    if not tasks:
        return []

    def priority_key(task: Task) -> tuple[int, int, float]:
        params = task.params if isinstance(task.params, dict) else {}
        return (
            -int(params.get("_manualPriorityBoostCount") or 0),
            -int(params.get("_manualPriorityBoostAt") or 0),
            task.created_at.timestamp(),
        )

    tasks.sort(key=priority_key)
    queued_jobs = []
    for position, task in enumerate(tasks[:limit], start=1):
        payload = _gpu_task_display_payload(task)
        payload.update(
            {
                "id": task.provider_job_id,
                "position": position,
                "queueState": "waiting",
                "progressPercent": task.progress_percent,
                "progressStage": task.progress_stage,
                "createdAt": int(task.created_at.timestamp() * 1000),
            }
        )
        queued_jobs.append(payload)
    return queued_jobs


def admin_gpu_metrics(db: Session) -> dict:
    base_url = settings.model_plaza_gpu_api_url.rstrip("/")
    if not base_url:
        return {
            "ok": False,
            "timestamp": time.time(),
            "error": "GPU API 未配置",
            "gpus": [],
            "runningJobs": [],
            "queuedJobs": _queued_gpu_jobs(db),
        }
    parsed_base_url = urlparse(base_url)
    target = f"{parsed_base_url.scheme}://{parsed_base_url.netloc}" if parsed_base_url.scheme and parsed_base_url.netloc else base_url
    headers = {}
    if settings.model_plaza_gpu_api_key:
        headers["X-API-Key"] = settings.model_plaza_gpu_api_key
    request = urllib.request.Request(urljoin(f"{base_url}/", "metrics"), headers=headers, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=6) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace").strip()
        suffix = f"：{detail[:300]}" if detail else ""
        return {
            "ok": False,
            "timestamp": time.time(),
            "error": f"GPU API HTTP {exc.code} ({target}){suffix}",
            "gpus": [],
            "runningJobs": [],
            "queuedJobs": _queued_gpu_jobs(db),
        }
    except Exception as exc:
        return {
            "ok": False,
            "timestamp": time.time(),
            "error": f"GPU API 请求失败 ({target})：{exc}",
            "gpus": [],
            "runningJobs": [],
            "queuedJobs": _queued_gpu_jobs(db),
        }
    payload.setdefault("ok", True)
    payload.setdefault("gpus", [])
    payload.setdefault("runningJobs", [])
    payload["queuedJobs"] = _queued_gpu_jobs(db)
    running_jobs = payload.get("runningJobs") if isinstance(payload.get("runningJobs"), list) else []
    job_ids = [job_id for job in running_jobs if isinstance(job, dict) for job_id in _gpu_job_lookup_keys(job)]
    display_map = _gpu_job_display_map(db, job_ids)
    for job in running_jobs:
        if isinstance(job, dict):
            for lookup_key in _gpu_job_lookup_keys(job):
                if lookup_key in display_map:
                    job.update(display_map[lookup_key])
                    break
    return payload
