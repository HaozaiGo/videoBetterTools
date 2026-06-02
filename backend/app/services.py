from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

from fastapi import HTTPException, UploadFile
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import hash_password
from app.config import settings
from app.models import Asset, ProcessedCallback, Task, User, Wallet, WalletLedger
from app.pricing import estimate_credits
from app.queue import enqueue_provider_job
from app.storage import object_key_for_upload, safe_storage_name, storage
from app.tool_config import CATEGORIES, TOOLS, get_tool


def now() -> datetime:
    return datetime.now(timezone.utc)


def public_url(storage_key: str) -> str:
    return storage.public_url(storage_key)


def serialize_datetime(value: datetime | None) -> int | None:
    if value is None:
        return None
    return int(value.timestamp() * 1000)


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


def task_to_dict(task: Task) -> dict:
    return {
        "id": task.id,
        "userId": task.user_id,
        "toolSlug": task.tool_slug,
        "inputAssetId": task.input_asset_id,
        "outputAssetId": task.output_asset_id,
        "status": task.status,
        "params": task.params,
        "estimatedCredits": task.estimated_credits,
        "frozenCredits": task.frozen_credits,
        "chargedCredits": task.charged_credits,
        "provider": task.provider,
        "providerJobId": task.provider_job_id,
        "errorCode": task.error_code,
        "createdAt": serialize_datetime(task.created_at),
        "completedAt": serialize_datetime(task.completed_at),
        "outputUrl": task.output_url,
    }


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


def serialize_bootstrap(db: Session, user_id: str | None = None) -> dict:
    ensure_demo_user(db)
    user_id = user_id or settings.demo_user_id
    user = db.get(User, user_id)
    wallet = get_wallet(db, user_id)
    tasks = db.execute(
        select(Task).where(Task.user_id == user_id).order_by(Task.created_at.desc())
    ).scalars()
    ledger = db.execute(
        select(WalletLedger).where(WalletLedger.user_id == user_id).order_by(WalletLedger.created_at.desc())
    ).scalars()
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
        "tasks": [task_to_dict(task) for task in tasks],
        "ledger": [ledger_to_dict(entry) for entry in ledger],
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
        expires_at=now() + timedelta(days=7),
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
        expires_at=now() + timedelta(days=7),
    )
    db.add(asset)
    db.commit()
    db.refresh(asset)
    return asset


def create_task(db: Session, user_id: str, tool_slug: str, input_asset_id: str, params: dict) -> Task:
    tool = get_tool(tool_slug)
    if tool is None or tool["status"] != "online":
        raise HTTPException(status_code=400, detail="tool is not available")

    input_asset = db.get(Asset, input_asset_id)
    if input_asset is None or input_asset.user_id != user_id:
        raise HTTPException(status_code=400, detail="missing uploaded asset")

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
    )
    wallet.frozen_credits += estimate
    db.add(task)
    add_ledger(db, user_id, "freeze", 0, f"{tool['name']} 冻结 {estimate} 积分", task.id)
    db.commit()
    db.refresh(task)
    enqueue_provider_job(task.id)
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

    if status == "processing" and task.status == "queued":
        task.status = "processing"

    if status == "succeeded" and task.status not in {"succeeded", "failed", "cancelled"}:
        charge = min(charged_credits or task.estimated_credits, task.frozen_credits)
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
            expires_at=now() + timedelta(days=7),
        )
        if output_storage_key is None:
            storage.write_text(output_asset.storage_key, f"任务 {task.id} 已完成\n")
        db.add(output_asset)
        task.status = "succeeded"
        task.output_asset_id = output_asset.id
        task.output_url = output_asset.url
        task.charged_credits = charge
        task.completed_at = now()
        wallet.frozen_credits = max(0, wallet.frozen_credits - task.frozen_credits)
        wallet.credits = max(0, wallet.credits - charge)
        add_ledger(db, task.user_id, "charge", -charge, f"{tool['name']} 扣费完成", task.id)

    if status == "failed" and task.status not in {"succeeded", "failed", "cancelled"}:
        task.status = "failed"
        task.error_code = error_code or "PROVIDER_FAILED"
        task.completed_at = now()
        wallet.frozen_credits = max(0, wallet.frozen_credits - task.frozen_credits)
        add_ledger(db, task.user_id, "refund", 0, f"{tool['name']} 失败，释放 {task.frozen_credits} 积分", task.id)

    db.commit()
    db.refresh(task)
    return False, task
