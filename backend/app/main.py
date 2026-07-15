import logging

from fastapi import BackgroundTasks, Depends, FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy.orm import Session

from app.admin import admin_create_internal_batch_missing_task, admin_delete_internal_batch_zips, admin_gpu_metrics, admin_internal_batches, admin_internal_batch_zips, admin_ledger, admin_regenerate_internal_batch_zip, admin_summary, admin_tasks, admin_users
from app.auth import admin_user, create_token, current_user, find_user_by_email, verify_password
from app.config import settings
from app.database import SessionLocal, get_db
from app.models import User
from app.schemas import AdminInternalBatchZipDeleteRequest, AssetComplete, LoginRequest, MultipartUploadInit, ProviderCallback, RechargeCreate, TaskBulkDeleteRequest, TaskCreate, UserCreate, UserRecharge
from app.services import (
    asset_to_dict,
    cancel_task,
    complete_multipart_upload,
    complete_uploaded_asset,
    create_multipart_upload,
    create_internal_batch_zip,
    create_presigned_asset_upload,
    create_user,
    create_task,
    delete_tasks_from_list,
    get_task_result_access,
    get_task_result_url,
    internal_batch_status,
    get_multipart_upload,
    paginated_ledger,
    paginated_tasks,
    plan_internal_batch_zip,
    provider_callback,
    recharge_wallet,
    retry_failed_task_single_gpu,
    retry_internal_batch_task_with_replacement_asset,
    retry_internal_batch_tasks,
    save_upload,
    save_multipart_chunk,
    serialize_bootstrap,
    task_to_dict,
)

logger = logging.getLogger("model_plaza")

app = FastAPI(title="片刻修AI API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

settings.upload_path.mkdir(parents=True, exist_ok=True)
app.mount(settings.public_upload_prefix, StaticFiles(directory=settings.upload_path), name="uploads")


@app.middleware("http")
async def log_errors(request: Request, call_next):
    try:
        return await call_next(request)
    except Exception:
        logger.exception("Unhandled error while processing %s %s", request.method, request.url.path)
        raise


@app.get("/api/health")
def health() -> dict:
    return {"ok": True}


@app.post("/api/auth/login")
def login(payload: LoginRequest, db: Session = Depends(get_db)) -> dict:
    user = find_user_by_email(db, payload.email)
    if user is None or not verify_password(payload.password, user.password_hash):
        from fastapi import HTTPException

        raise HTTPException(status_code=401, detail="invalid credentials")
    token = create_token(user.id)
    return {
        "token": token,
        "user": {
            "id": user.id,
            "email": user.email,
            "name": user.name,
            "role": user.role,
        },
    }


@app.post("/api/auth/register", status_code=201)
def register(payload: UserCreate, db: Session = Depends(get_db)) -> dict:
    user = create_user(db, payload.email, payload.password, payload.name, "user", 0)
    token = create_token(user.id)
    return {
        "token": token,
        "user": {
            "id": user.id,
            "email": user.email,
            "name": user.name,
            "role": user.role,
        },
    }


@app.get("/api/auth/me")
def me(user: User = Depends(current_user)) -> dict:
    return {"id": user.id, "email": user.email, "name": user.name, "role": user.role}


@app.get("/api/bootstrap")
def bootstrap(db: Session = Depends(get_db), user: User = Depends(current_user)) -> dict:
    return serialize_bootstrap(db, user.id)


@app.get("/api/tasks")
def list_tasks(
    page: int = Query(1, ge=1),
    per_page: int = Query(50, alias="perPage", ge=1, le=100),
    status: str | None = Query(None),
    completed_from: str | None = Query(None, alias="completedFrom"),
    completed_to: str | None = Query(None, alias="completedTo"),
    batch_name: str | None = Query(None, alias="batchName"),
    internal_batch_only: bool = Query(False, alias="internalBatchOnly"),
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> dict:
    return paginated_tasks(
        db,
        user.id,
        page=page,
        per_page=per_page,
        status=status,
        completed_from=completed_from,
        completed_to=completed_to,
        batch_name=batch_name,
        internal_batch_only=internal_batch_only,
    )


@app.get("/api/ledger")
def list_ledger(
    page: int = Query(1, ge=1),
    per_page: int = Query(50, alias="perPage", ge=1, le=100),
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> dict:
    return paginated_ledger(db, user.id, page=page, per_page=per_page)


@app.post("/api/assets/presign")
def presign_asset(
    kind: str = "video",
    durationSeconds: int = 0,
    originalName: str = "upload.bin",
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> dict:
    return create_presigned_asset_upload(db, user.id, kind=kind, duration_seconds=durationSeconds, original_name=originalName)


@app.post("/api/assets", status_code=201)
async def upload_asset(
    file: UploadFile = File(...),
    kind: str = Form("video"),
    durationSeconds: int = Form(0),
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> dict:
    asset = await save_upload(db, user.id, file, kind=kind, duration_seconds=durationSeconds)
    return {"asset": asset_to_dict(asset)}


@app.post("/api/assets/complete", status_code=201)
def complete_asset_upload(payload: AssetComplete, db: Session = Depends(get_db), user: User = Depends(current_user)) -> dict:
    asset = complete_uploaded_asset(
        db,
        user.id,
        asset_id=payload.assetId,
        kind=payload.kind,
        original_name=payload.originalName,
        mime_type=payload.mimeType,
        storage_key=payload.storageKey,
        size_bytes=payload.sizeBytes,
        duration_seconds=payload.durationSeconds,
    )
    return {"asset": asset_to_dict(asset)}


@app.post("/api/assets/multipart/init", status_code=201)
def init_multipart_upload(payload: MultipartUploadInit, db: Session = Depends(get_db), user: User = Depends(current_user)) -> dict:
    return create_multipart_upload(
        db,
        user.id,
        kind=payload.kind,
        original_name=payload.originalName,
        mime_type=payload.mimeType,
        size_bytes=payload.sizeBytes,
        duration_seconds=payload.durationSeconds,
        chunk_size=payload.chunkSize,
    )


@app.get("/api/assets/multipart/{upload_id}")
def multipart_upload_status(upload_id: str, user: User = Depends(current_user)) -> dict:
    return get_multipart_upload(user.id, upload_id)


@app.post("/api/assets/multipart/{upload_id}/chunks/{chunk_index}")
async def upload_multipart_chunk(
    upload_id: str,
    chunk_index: int,
    file: UploadFile = File(...),
    user: User = Depends(current_user),
) -> dict:
    return await save_multipart_chunk(user.id, upload_id, chunk_index, file)


@app.post("/api/assets/multipart/{upload_id}/complete", status_code=201)
def complete_multipart_upload_endpoint(upload_id: str, db: Session = Depends(get_db), user: User = Depends(current_user)) -> dict:
    asset = complete_multipart_upload(db, user.id, upload_id)
    return {"asset": asset_to_dict(asset)}


@app.post("/api/tasks", status_code=201)
def create_task_endpoint(payload: TaskCreate, db: Session = Depends(get_db), user: User = Depends(current_user)) -> dict:
    task = create_task(db, user.id, payload.toolSlug, payload.inputAssetId, payload.params)
    return {"task": task_to_dict(task), "state": serialize_bootstrap(db, user.id)}


@app.post("/api/tasks/{task_id}/cancel")
def cancel_task_endpoint(task_id: str, db: Session = Depends(get_db), user: User = Depends(current_user)) -> dict:
    task = cancel_task(db, user.id, task_id)
    return {"task": task_to_dict(task), "state": serialize_bootstrap(db, user.id)}


@app.delete("/api/tasks")
def delete_tasks_endpoint(payload: TaskBulkDeleteRequest, db: Session = Depends(get_db), user: User = Depends(current_user)) -> dict:
    result = delete_tasks_from_list(db, user.id, payload.taskIds)
    return {**result, "state": serialize_bootstrap(db, user.id)}


@app.post("/api/tasks/{task_id}/retry-single-gpu")
def retry_task_single_gpu_endpoint(task_id: str, db: Session = Depends(get_db), user: User = Depends(current_user)) -> dict:
    task = retry_failed_task_single_gpu(db, user.id, task_id)
    return {"task": task_to_dict(task), "state": serialize_bootstrap(db, user.id)}


@app.get("/api/tasks/{task_id}/preview-result")
def preview_task_result(task_id: str, db: Session = Depends(get_db), user: User = Depends(current_user)):
    access = get_task_result_access(db, user.id, task_id)
    db.close()
    if access["mode"] == "redirect":
        return RedirectResponse(str(access["url"]), status_code=302)
    preview_path = access["path"]
    return FileResponse(preview_path, media_type=access.get("mime_type", "video/mp4"), filename=access["filename"], content_disposition_type="inline")


@app.get("/api/tasks/{task_id}/result/{filename}")
def task_result_file(task_id: str, filename: str, db: Session = Depends(get_db), user: User = Depends(current_user)):
    access = get_task_result_access(db, user.id, task_id)
    db.close()
    if access["mode"] == "redirect":
        return RedirectResponse(str(access["url"]), status_code=302)
    return FileResponse(access["path"], media_type=access.get("mime_type", "video/mp4"), filename=access["filename"], content_disposition_type="inline")


@app.get("/api/tasks/{task_id}/result-link")
def task_result_link(task_id: str, db: Session = Depends(get_db), user: User = Depends(current_user)) -> dict:
    return {"url": get_task_result_url(db, user.id, task_id)}


@app.get("/api/internal/batches/{batch_id}")
def internal_batch_status_endpoint(batch_id: str, db: Session = Depends(get_db), user: User = Depends(current_user)) -> dict:
    return internal_batch_status(db, user.id, batch_id)


@app.get("/api/internal/batches/{batch_id}/download")
def internal_batch_download_endpoint(batch_id: str, part: int = Query(1, ge=1), db: Session = Depends(get_db), user: User = Depends(current_user)):
    archive = create_internal_batch_zip(db, user.id, batch_id, part=part)
    parts = archive.get("parts") or [archive]
    if part > len(parts):
        raise HTTPException(status_code=404, detail="download part not found")
    selected = parts[part - 1]
    db.close()
    if selected.get("remoteUrl"):
        return RedirectResponse(str(selected["remoteUrl"]), status_code=302)
    return FileResponse(selected["path"], media_type="application/zip", filename=selected["filename"], content_disposition_type="attachment")


def _prepare_internal_batch_zip_background(user_id: str, batch_id: str, part: int) -> None:
    db = SessionLocal()
    try:
        create_internal_batch_zip(db, user_id, batch_id, part=part)
    except Exception:
        logger.exception("Failed to prepare internal batch zip %s part %s", batch_id, part)
    finally:
        db.close()


@app.post("/api/internal/batches/{batch_id}/download/prepare")
def internal_batch_download_prepare_endpoint(
    batch_id: str,
    background_tasks: BackgroundTasks,
    part: int = Query(1, ge=1),
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> dict:
    archive = plan_internal_batch_zip(db, user.id, batch_id)
    parts = archive.get("parts") or [archive]
    if part > len(parts):
        raise HTTPException(status_code=404, detail="download part not found")
    selected = parts[part - 1]
    status = "ready" if selected["sizeBytes"] > 0 else "preparing"
    if status == "preparing":
        background_tasks.add_task(_prepare_internal_batch_zip_background, user.id, batch_id, part)
    return {
        "status": status,
        "partCount": len(parts),
        "part": {
            "index": selected["index"],
            "filename": selected["filename"],
            "sizeBytes": selected["sizeBytes"],
            "estimatedSizeBytes": selected.get("estimatedSizeBytes", 0),
            "url": f"/api/internal/batches/{batch_id}/download?part={selected['index']}",
        },
    }


@app.post("/api/internal/batches/{batch_id}/download-manifest")
def internal_batch_download_manifest_endpoint(batch_id: str, db: Session = Depends(get_db), user: User = Depends(current_user)) -> dict:
    archive = plan_internal_batch_zip(db, user.id, batch_id)
    parts = archive.get("parts") or [archive]
    return {
        "partCount": len(parts),
        "parts": [
            {
                "index": part["index"],
                "filename": part["filename"],
                "sizeBytes": part["sizeBytes"],
                "estimatedSizeBytes": part.get("estimatedSizeBytes", 0),
                "url": f"/api/internal/batches/{batch_id}/download?part={part['index']}",
            }
            for part in parts
        ],
    }


@app.post("/api/internal/batches/{batch_id}/retry")
def internal_batch_retry_endpoint(batch_id: str, db: Session = Depends(get_db), user: User = Depends(current_user)) -> dict:
    result = retry_internal_batch_tasks(db, user.id, batch_id)
    return {**result, "state": serialize_bootstrap(db, user.id)}


@app.post("/api/provider/callback")
def provider_callback_endpoint(payload: ProviderCallback, db: Session = Depends(get_db)) -> dict:
    duplicated, _task = provider_callback(
        db,
        provider_job_id=payload.providerJobId,
        status=payload.status,
        callback_id=payload.callbackId,
        output_url=payload.outputUrl,
        output_storage_key=payload.outputStorageKey,
        output_mime_type=payload.outputMimeType,
        output_size_bytes=payload.outputSizeBytes,
        charged_credits=payload.chargedCredits,
        error_code=payload.errorCode,
        progress_percent=payload.progressPercent,
        progress_stage=payload.progressStage,
    )
    return {"duplicated": duplicated, "task": task_to_dict(_task)}


@app.post("/api/recharge")
def recharge(payload: RechargeCreate, db: Session = Depends(get_db), user: User = Depends(current_user)) -> dict:
    recharge_wallet(db, user.id, payload.credits)
    return {"state": serialize_bootstrap(db, user.id)}


@app.get("/api/admin/summary")
def admin_summary_endpoint(db: Session = Depends(get_db), _admin: User = Depends(admin_user)) -> dict:
    return admin_summary(db)


@app.get("/api/admin/users")
def admin_users_endpoint(db: Session = Depends(get_db), _admin: User = Depends(admin_user)) -> list[dict]:
    return admin_users(db)


@app.post("/api/admin/users", status_code=201)
def admin_create_user_endpoint(payload: UserCreate, db: Session = Depends(get_db), _admin: User = Depends(admin_user)) -> dict:
    user = create_user(db, payload.email, payload.password, payload.name, payload.role, payload.initialCredits)
    return {"id": user.id, "email": user.email, "name": user.name, "role": user.role}


@app.post("/api/admin/users/{user_id}/recharge")
def admin_recharge_user_endpoint(user_id: str, payload: UserRecharge, db: Session = Depends(get_db), _admin: User = Depends(admin_user)) -> dict:
    recharge_wallet(db, user_id, payload.credits)
    return {"ok": True}


@app.get("/api/admin/tasks")
def admin_tasks_endpoint(
    page: int = Query(1, ge=1),
    per_page: int = Query(50, alias="perPage", ge=1, le=100),
    db: Session = Depends(get_db),
    _admin: User = Depends(admin_user),
) -> dict:
    return admin_tasks(db, page=page, per_page=per_page)


@app.get("/api/admin/internal-batches")
def admin_internal_batches_endpoint(
    page: int = Query(1, ge=1),
    per_page: int = Query(50, alias="perPage", ge=1, le=100),
    status: str = Query("all"),
    name: str = Query(""),
    db: Session = Depends(get_db),
    _admin: User = Depends(admin_user),
) -> dict:
    return admin_internal_batches(db, page=page, per_page=per_page, status=status, name=name)


@app.get("/api/admin/internal-batches/{batch_id}")
def admin_internal_batch_detail_endpoint(
    batch_id: str,
    user_id: str = Query(..., alias="userId"),
    db: Session = Depends(get_db),
    _admin: User = Depends(admin_user),
) -> dict:
    return internal_batch_status(db, user_id, batch_id)


@app.post("/api/admin/internal-batches/{batch_id}/retry")
def admin_internal_batch_retry_endpoint(
    batch_id: str,
    user_id: str = Query(..., alias="userId"),
    db: Session = Depends(get_db),
    _admin: User = Depends(admin_user),
) -> dict:
    return retry_internal_batch_tasks(db, user_id, batch_id, at_front=True)


@app.post("/api/admin/internal-batches/{batch_id}/zip/regenerate")
def admin_internal_batch_zip_regenerate_endpoint(
    batch_id: str,
    user_id: str = Query(..., alias="userId"),
    db: Session = Depends(get_db),
    _admin: User = Depends(admin_user),
) -> dict:
    return admin_regenerate_internal_batch_zip(db, user_id, batch_id)


@app.post("/api/admin/internal-batches/{batch_id}/missing")
async def admin_internal_batch_missing_upload_endpoint(
    batch_id: str,
    user_id: str = Form(..., alias="userId"),
    episode: int = Form(..., ge=1),
    duration_seconds: int = Form(0, alias="durationSeconds"),
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    _admin: User = Depends(admin_user),
) -> dict:
    asset = await save_upload(db, user_id, file, kind="video", duration_seconds=duration_seconds)
    payload = admin_create_internal_batch_missing_task(db, user_id, batch_id, asset.id, episode, duration_seconds=duration_seconds)
    return {"asset": asset_to_dict(asset), **payload}


@app.post("/api/admin/internal-batches/{batch_id}/tasks/{task_id}/upload-retry")
async def admin_internal_batch_task_upload_retry_endpoint(
    batch_id: str,
    task_id: str,
    user_id: str = Form(..., alias="userId"),
    duration_seconds: int = Form(0, alias="durationSeconds"),
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    _admin: User = Depends(admin_user),
) -> dict:
    asset = await save_upload(db, user_id, file, kind="video", duration_seconds=duration_seconds)
    payload = retry_internal_batch_task_with_replacement_asset(db, user_id, batch_id, task_id, asset.id, duration_seconds=duration_seconds, at_front=True)
    return {"asset": asset_to_dict(asset), **payload}


@app.get("/api/admin/internal-batch-zips")
def admin_internal_batch_zips_endpoint(
    page: int = Query(1, ge=1),
    per_page: int = Query(50, alias="perPage", ge=1, le=100),
    status: str = Query("ready"),
    name: str = Query(""),
    db: Session = Depends(get_db),
    _admin: User = Depends(admin_user),
) -> dict:
    return admin_internal_batch_zips(db, page=page, per_page=per_page, status=status, name=name)


@app.delete("/api/admin/internal-batch-zips")
def admin_delete_internal_batch_zips_endpoint(
    payload: AdminInternalBatchZipDeleteRequest,
    db: Session = Depends(get_db),
    _admin: User = Depends(admin_user),
) -> dict:
    return admin_delete_internal_batch_zips(db, [item.model_dump() for item in payload.items])


@app.get("/api/admin/internal-batch-zips/{batch_id}/download")
def admin_internal_batch_zip_download_endpoint(
    batch_id: str,
    user_id: str = Query(..., alias="userId"),
    part: int = Query(1, ge=1),
    db: Session = Depends(get_db),
    _admin: User = Depends(admin_user),
):
    archive = create_internal_batch_zip(db, user_id, batch_id, part=part)
    selected = next((item for item in archive["parts"] if item["index"] == part), None)
    if selected is None:
        raise HTTPException(status_code=404, detail="download part not found")
    remote_url = selected.get("remoteUrl")
    if remote_url:
        return RedirectResponse(str(remote_url), status_code=302)
    return FileResponse(selected["path"], media_type="application/zip", filename=selected["filename"], content_disposition_type="attachment")


@app.get("/api/admin/gpu")
def admin_gpu_endpoint(db: Session = Depends(get_db), _admin: User = Depends(admin_user)) -> dict:
    return admin_gpu_metrics(db)


@app.get("/api/admin/ledger")
def admin_ledger_endpoint(db: Session = Depends(get_db), _admin: User = Depends(admin_user)) -> list[dict]:
    return admin_ledger(db)
