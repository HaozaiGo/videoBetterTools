import json
import time
import urllib.error
import urllib.request
from urllib.parse import urljoin, urlparse

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import Asset, Task, User, Wallet, WalletLedger
from app.services import ledger_to_dict, task_to_dict


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


def admin_tasks(db: Session) -> list[dict]:
    tasks = db.execute(select(Task).order_by(Task.created_at.desc()).limit(200)).scalars()
    return [task_to_dict(task) for task in tasks]


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
