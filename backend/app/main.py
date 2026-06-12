import logging

from fastapi import Depends, FastAPI, File, Form, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy.orm import Session

from app.admin import admin_gpu_metrics, admin_ledger, admin_summary, admin_tasks, admin_users
from app.auth import admin_user, create_token, current_user, find_user_by_email, verify_password
from app.config import settings
from app.database import get_db
from app.models import User
from app.schemas import AssetComplete, LoginRequest, MultipartUploadInit, ProviderCallback, RechargeCreate, TaskCreate, UserCreate, UserRecharge
from app.services import (
    asset_to_dict,
    cancel_task,
    complete_multipart_upload,
    complete_uploaded_asset,
    create_multipart_upload,
    create_presigned_asset_upload,
    create_user,
    create_task,
    get_task_result_access,
    get_task_result_url,
    get_multipart_upload,
    provider_callback,
    recharge_wallet,
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


@app.get("/api/tasks/{task_id}/preview-result")
def preview_task_result(task_id: str, db: Session = Depends(get_db), user: User = Depends(current_user)):
    access = get_task_result_access(db, user.id, task_id)
    if access["mode"] == "redirect":
        return RedirectResponse(str(access["url"]), status_code=302)
    preview_path = access["path"]
    return FileResponse(preview_path, media_type="video/mp4", filename=preview_path.name)


@app.get("/api/tasks/{task_id}/result-link")
def task_result_link(task_id: str, db: Session = Depends(get_db), user: User = Depends(current_user)) -> dict:
    return {"url": get_task_result_url(db, user.id, task_id)}


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
    return {"duplicated": duplicated, "state": serialize_bootstrap(db, _task.user_id)}


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
def admin_tasks_endpoint(db: Session = Depends(get_db), _admin: User = Depends(admin_user)) -> list[dict]:
    return admin_tasks(db)


@app.get("/api/admin/gpu")
def admin_gpu_endpoint(_admin: User = Depends(admin_user)) -> dict:
    return admin_gpu_metrics()


@app.get("/api/admin/ledger")
def admin_ledger_endpoint(db: Session = Depends(get_db), _admin: User = Depends(admin_user)) -> list[dict]:
    return admin_ledger(db)
