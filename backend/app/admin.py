import json
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import quote
from urllib.parse import urljoin, urlparse

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session, selectinload

from app.config import settings
from app.models import Asset, Task, User, Wallet, WalletLedger
from app.queue import internal_batch_zip_queue
from app.services import normalize_pagination, page_info, ledger_to_dict, plan_internal_batch_zip, task_to_dict


def admin_summary(db: Session) -> dict:
    charged = db.execute(
        select(func.coalesce(func.sum(WalletLedger.amount), 0)).where(WalletLedger.type == "charge")
    ).scalar_one()
    return {
        "users": db.execute(select(func.count()).select_from(User)).scalar_one(),
        "tasks": db.execute(select(func.count()).select_from(Task)).scalar_one(),
        "assets": db.execute(select(func.count()).select_from(Asset)).scalar_one(),
        "creditsCharged": abs(int(charged or 0)),
        "queuedTasks": db.execute(select(func.count()).select_from(Task).where(Task.status == "queued")).scalar_one(),
        "processingTasks": db.execute(select(func.count()).select_from(Task).where(Task.status == "processing")).scalar_one(),
        "failedTasks": db.execute(select(func.count()).select_from(Task).where(Task.status == "failed")).scalar_one(),
    }


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


ADMIN_ZIP_STATUS_FILTERS = {"ready", "processing", "failed"}


def _serialize_job_time(value) -> int | None:
    return int(value.timestamp() * 1000) if value else None


def _zip_job_states() -> dict[tuple[str, str], dict]:
    states: dict[tuple[str, str], dict] = {}
    try:
        queue = internal_batch_zip_queue()
        registry_sets = [
            ("queued", queue.get_job_ids()),
            ("started", queue.started_job_registry.get_job_ids()),
            ("scheduled", queue.scheduled_job_registry.get_job_ids()),
            ("failed", queue.failed_job_registry.get_job_ids()),
        ]
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
            }
    return states


def _zip_process_message(batch: dict, zip_status: str, zip_job: dict | None) -> tuple[str, str]:
    processing = int(batch["processing"])
    succeeded = int(batch["succeeded"])
    total = int(batch["total"])
    failed = int(batch["failed"])
    cancelled = int(batch["cancelled"])
    if processing > 0:
        return "tasks", f"任务还没全部完成：{processing} 个仍在生成，已成功 {succeeded}/{total}"
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
            return "failed", "ZIP 打包/上传失败，等待重新入队"
    if zip_status == "failed":
        return "failed", f"批次有失败/取消任务：失败 {failed}，取消 {cancelled}；需重试或补包"
    if succeeded <= 0:
        return "tasks", "还没有成功结果可打包"
    return "waiting", "批次已有成功结果，等待自动入 ZIP 队列"


def _empty_zip_batch_item(batch: dict, zip_status: str, archive: dict | None = None, zip_job: dict | None = None) -> dict:
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
    }


def admin_internal_batch_zips(db: Session, page: int = 1, per_page: int = 50, status: str = "ready") -> dict:
    page, per_page = normalize_pagination(page, per_page)
    status = status if status in ADMIN_ZIP_STATUS_FILTERS else "ready"
    batch_id_expr = Task.params["internalBatchId"].as_string()
    batch_name_expr = Task.params["internalBatchName"].as_string()
    updated_expr = func.coalesce(Task.completed_at, Task.created_at)
    rows = db.execute(
        select(
            Task.user_id.label("user_id"),
            batch_id_expr.label("batch_id"),
            func.max(batch_name_expr).label("batch_name"),
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
            batch_id_expr.is_not(None),
            batch_id_expr != "",
        )
        .group_by(Task.user_id, batch_id_expr)
        .order_by(func.max(updated_expr).desc())
    ).all()
    batches = [
        {
            "userId": row.user_id,
            "batchId": row.batch_id,
            "batchName": row.batch_name or row.batch_id or "内部批量任务",
            "total": int(row.total or 0),
            "succeeded": int(row.succeeded or 0),
            "failed": int(row.failed or 0),
            "cancelled": int(row.cancelled or 0),
            "processing": int(row.processing or 0),
            "createdAt": int(row.created_at.timestamp() * 1000),
            "updatedAt": int(row.updated_at.timestamp() * 1000),
        }
        for row in rows
    ]
    items: list[dict] = []
    tab_counts = {"ready": 0, "processing": 0, "failed": 0}
    zip_jobs = _zip_job_states()
    for batch in batches:
        archive = None
        ready_items: list[dict] = []
        try:
            archive = plan_internal_batch_zip(db, str(batch["userId"]), str(batch["batchId"]))
        except Exception:
            archive = None
        if archive is not None:
            for part in archive["parts"]:
                source = _zip_part_source(part)
                size_bytes = int(part.get("sizeBytes") or 0)
                if not source or size_bytes <= 0:
                    continue
                part_index = int(part["index"])
                batch_id = str(batch["batchId"])
                user_id = str(batch["userId"])
                ready_items.append(
                    {
                        **batch,
                        "zipStatus": "ready",
                        "zipStage": "ready",
                        "zipJob": None,
                        "partIndex": part_index,
                        "partCount": int(archive["partCount"]),
                        "filename": part["filename"],
                        "sizeBytes": size_bytes,
                        "estimatedSizeBytes": int(part.get("estimatedSizeBytes") or 0),
                        "source": source,
                        "storageKey": str(part.get("storageKey") or ""),
                        "downloadUrl": f"/api/admin/internal-batch-zips/{quote(batch_id, safe='')}/download?userId={quote(user_id, safe='')}&part={part_index}",
                        "message": "",
                    }
                )
        if ready_items:
            tab_counts["ready"] += len(ready_items)
            if status == "ready":
                items.extend(ready_items)
            continue
        zip_job = zip_jobs.get((str(batch["userId"]), str(batch["batchId"])))
        pending_status = "processing"
        if zip_job and zip_job.get("state") == "failed":
            pending_status = "failed"
        elif int(batch["failed"]) + int(batch["cancelled"]) > 0 and int(batch["processing"]) <= 0:
            pending_status = "failed"
        tab_counts[pending_status] += 1
        if status == pending_status:
            items.append(_empty_zip_batch_item(batch, pending_status, archive, zip_job))

    items.sort(key=lambda item: (int(item["updatedAt"]), str(item["batchId"]), int(item["partIndex"])), reverse=True)
    total = len(items)
    start = (page - 1) * per_page
    return {"items": items[start : start + per_page], "page": page_info(total, page, per_page), "tabs": tab_counts}


def admin_ledger(db: Session) -> list[dict]:
    ledger = db.execute(select(WalletLedger).order_by(WalletLedger.created_at.desc()).limit(200)).scalars()
    return [ledger_to_dict(entry) for entry in ledger]


def admin_gpu_metrics() -> dict:
    base_url = settings.model_plaza_gpu_api_url.rstrip("/")
    if not base_url:
        return {
            "ok": False,
            "timestamp": time.time(),
            "error": "GPU API 未配置",
            "gpus": [],
            "runningJobs": [],
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
        }
    except Exception as exc:
        return {
            "ok": False,
            "timestamp": time.time(),
            "error": f"GPU API 请求失败 ({target})：{exc}",
            "gpus": [],
            "runningJobs": [],
        }
    payload.setdefault("ok", True)
    payload.setdefault("gpus", [])
    payload.setdefault("runningJobs", [])
    return payload
