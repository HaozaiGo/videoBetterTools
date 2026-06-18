from datetime import datetime, timedelta, timezone
from pathlib import Path
import shutil
from urllib.parse import quote
from uuid import uuid4

from fastapi import HTTPException, UploadFile
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from app.auth import hash_password
from app.config import settings
from app.models import Asset, ProcessedCallback, Task, User, Wallet, WalletLedger
from app.pricing import estimate_credits
from app.queue import enqueue_provider_job
from app.storage import object_key_for_upload, safe_storage_name, storage
from app.tool_config import CATEGORIES, TOOLS, get_tool

DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 100


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
    preview_path = task_preview_path(task)
    input_asset = getattr(task, "input_asset", None)
    has_result_access = bool(preview_path.exists() or task.output_asset_id)
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
        "progressPercent": task.progress_percent,
        "progressStage": task.progress_stage,
        "createdAt": serialize_datetime(task.created_at),
        "completedAt": serialize_datetime(task.completed_at),
        "outputUrl": task.output_url,
        "previewUrl": f"/api/tasks/{task.id}/preview-result" if has_result_access else "",
    }


def task_result_output_key(task: Task) -> str:
    suffix_by_tool = {
        "remove-watermark": "watermark-removed",
        "remove-subtitle": "subtitle-removed",
        "enhance": "enhanced",
        "translate": "translated",
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


def paginated_tasks(db: Session, user_id: str, page: int = 1, per_page: int = DEFAULT_PAGE_SIZE) -> dict:
    page, per_page = normalize_pagination(page, per_page)
    total = db.execute(select(func.count()).select_from(Task).where(Task.user_id == user_id)).scalar_one()
    tasks = db.execute(
        select(Task)
        .where(Task.user_id == user_id)
        .options(selectinload(Task.input_asset))
        .order_by(Task.created_at.desc())
        .offset((page - 1) * per_page)
        .limit(per_page)
    ).scalars()
    return {"items": [task_to_dict(task) for task in tasks], "page": page_info(total, page, per_page)}


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
        expires_at=now() + timedelta(days=7),
    )
    db.add(asset)
    db.commit()
    db.refresh(asset)
    shutil.rmtree(upload_dir, ignore_errors=True)
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
        progress_percent=0,
        progress_stage="等待 worker 领取任务",
    )
    wallet.frozen_credits += estimate
    db.add(task)
    add_ledger(db, user_id, "freeze", 0, f"{tool['name']} 冻结 {estimate} 积分", task.id)
    db.commit()
    db.refresh(task)
    enqueue_provider_job(task.id)
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
        task.progress_percent = 100
        task.progress_stage = "处理完成，结果已入库"
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
        if progress_stage is None:
            task.progress_stage = "处理失败，积分已释放"
        task.completed_at = now()
        wallet.frozen_credits = max(0, wallet.frozen_credits - task.frozen_credits)
        add_ledger(db, task.user_id, "refund", 0, f"{tool['name']} 失败，释放 {task.frozen_credits} 积分", task.id)

    db.commit()
    db.refresh(task)
    return False, task
