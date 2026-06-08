import logging
import os
import time

from rq import SimpleWorker, Worker

from app.database import SessionLocal
from app.models import Asset, Task
from app.queue import redis_connection, task_queue
from app.services import provider_callback
from app.video.enhance import process_video_enhance
from app.video.translate import process_video_translate
from app.video.watermark import VideoProcessingError, process_subtitle_removal, process_watermark_removal

logger = logging.getLogger("model_plaza.worker")


def process_provider_job(task_id: str) -> None:
    with SessionLocal() as db:
        task = db.get(Task, task_id)
        if task is None:
            return
        provider_job_id = task.provider_job_id
        provider_callback(db, provider_job_id, "processing", callback_id=f"{provider_job_id}:processing")

    # 已接入真实视频处理能力的工具单独走 GPU/本地处理管线；其他工具仍保留模拟供应商结果。
    if task.tool_slug in {"remove-watermark", "remove-subtitle", "enhance", "translate"}:
        _process_real_video_task(task_id)
        return

    time.sleep(8)

    with SessionLocal() as db:
        task = db.get(Task, task_id)
        if task is None or task.status in {"succeeded", "failed", "cancelled"}:
            return
        provider_callback(
            db,
            task.provider_job_id,
            "succeeded",
            callback_id=f"{task.provider_job_id}:succeeded",
        )


def _process_real_video_task(task_id: str) -> None:
    with SessionLocal() as db:
        task = db.get(Task, task_id)
        if task is None or task.status in {"succeeded", "failed", "cancelled"}:
            return
        input_asset = db.get(Asset, task.input_asset_id)
        if input_asset is None:
            provider_callback(
                db,
                task.provider_job_id,
                "failed",
                callback_id=f"{task.provider_job_id}:missing-input",
                error_code="INPUT_ASSET_NOT_FOUND",
            )
            return
        provider_job_id = task.provider_job_id
        params = dict(task.params or {})
        params["providerJobId"] = provider_job_id
        input_storage_key = input_asset.storage_key
        tool_slug = task.tool_slug

    # 耗时视频处理放在数据库会话之外，避免长时间占用连接和行锁。
    try:
        if tool_slug == "enhance":
            result = process_video_enhance(input_storage_key, task_id, params)
        elif tool_slug == "translate":
            result = process_video_translate(input_storage_key, task_id, params)
        elif tool_slug == "remove-subtitle":
            result = process_subtitle_removal(input_storage_key, task_id, params)
        else:
            result = process_watermark_removal(input_storage_key, task_id, params)
    except VideoProcessingError as exc:
        logger.warning("Video processing failed for task %s: %s", task_id, exc)
        _fail_provider_job(provider_job_id, "VIDEO_PROCESSING_FAILED", str(exc))
        return
    except Exception as exc:
        logger.exception("Unexpected video processing error for task %s", task_id)
        _fail_provider_job(provider_job_id, "VIDEO_PROCESSING_FAILED", str(exc))
        return

    with SessionLocal() as db:
        task = db.get(Task, task_id)
        if task is None or task.status in {"succeeded", "failed", "cancelled"}:
            return
        # 复用供应商回调入口完成扣费、产物入库和任务状态流转，后续替换真实供应商时账务逻辑不分叉。
        provider_callback(
            db,
            task.provider_job_id,
            "succeeded",
            callback_id=f"{task.provider_job_id}:succeeded",
            output_url=result["url"],
            output_storage_key=result["storage_key"],
            output_mime_type=result["mime_type"],
            output_size_bytes=result["size_bytes"],
        )


def _fail_provider_job(provider_job_id: str, error_code: str, progress_stage: str = "") -> None:
    with SessionLocal() as db:
        provider_callback(
            db,
            provider_job_id,
            "failed",
            callback_id=f"{provider_job_id}:failed",
            error_code=error_code,
            progress_stage=progress_stage[:160] if progress_stage else None,
        )


def run_worker() -> None:
    worker_class = SimpleWorker if os.environ.get("MODEL_PLAZA_WORKER_MODE") == "simple" else Worker
    worker = worker_class([task_queue()], connection=redis_connection())
    worker.work()


if __name__ == "__main__":
    run_worker()
