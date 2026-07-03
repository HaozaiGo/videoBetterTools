import json
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import quote
from urllib.parse import urljoin, urlparse

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from app.config import settings
from app.models import Asset, Task, User, Wallet, WalletLedger
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


def admin_internal_batch_zips(db: Session, page: int = 1, per_page: int = 50) -> dict:
    page, per_page = normalize_pagination(page, per_page)
    tasks = db.execute(select(Task).order_by(Task.created_at.desc())).scalars().all()
    batches: dict[tuple[str, str], dict] = {}
    for task in tasks:
        batch_id = _batch_id_for_task(task)
        if not batch_id:
            continue
        key = (task.user_id, batch_id)
        batch = batches.get(key)
        created_at_ms = int(task.created_at.timestamp() * 1000)
        completed_at_ms = int(task.completed_at.timestamp() * 1000) if task.completed_at else None
        if batch is None:
            batch = {
                "userId": task.user_id,
                "batchId": batch_id,
                "batchName": _batch_name_for_task(task, batch_id),
                "total": 0,
                "succeeded": 0,
                "failed": 0,
                "cancelled": 0,
                "processing": 0,
                "createdAt": created_at_ms,
                "updatedAt": completed_at_ms or created_at_ms,
            }
            batches[key] = batch
        batch["createdAt"] = min(int(batch["createdAt"]), created_at_ms)
        batch["updatedAt"] = max(int(batch["updatedAt"]), completed_at_ms or created_at_ms)
        batch["total"] += 1
        if task.status == "succeeded":
            batch["succeeded"] += 1
        elif task.status == "failed":
            batch["failed"] += 1
        elif task.status == "cancelled":
            batch["cancelled"] += 1
        elif task.status in {"queued", "processing"}:
            batch["processing"] += 1

    items: list[dict] = []
    for batch in batches.values():
        try:
            archive = plan_internal_batch_zip(db, str(batch["userId"]), str(batch["batchId"]))
        except Exception:
            continue
        for part in archive["parts"]:
            source = _zip_part_source(part)
            size_bytes = int(part.get("sizeBytes") or 0)
            if not source or size_bytes <= 0:
                continue
            part_index = int(part["index"])
            batch_id = str(batch["batchId"])
            user_id = str(batch["userId"])
            items.append(
                {
                    **batch,
                    "partIndex": part_index,
                    "partCount": int(archive["partCount"]),
                    "filename": part["filename"],
                    "sizeBytes": size_bytes,
                    "source": source,
                    "storageKey": str(part.get("storageKey") or ""),
                    "downloadUrl": f"/api/admin/internal-batch-zips/{quote(batch_id, safe='')}/download?userId={quote(user_id, safe='')}&part={part_index}",
                }
            )

    items.sort(key=lambda item: (int(item["updatedAt"]), str(item["batchId"]), int(item["partIndex"])), reverse=True)
    total = len(items)
    start = (page - 1) * per_page
    return {"items": items[start : start + per_page], "page": page_info(total, page, per_page)}


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
