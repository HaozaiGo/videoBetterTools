import logging
import time

from rq import Worker

from app.database import SessionLocal
from app.models import Asset, Task
from app.queue import redis_connection, task_queue
from app.services import provider_callback
from app.video.watermark import VideoProcessingError, process_watermark_removal

logger = logging.getLogger("model_plaza.worker")


def process_provider_job(task_id: str) -> None:
    with SessionLocal() as db:
        task = db.get(Task, task_id)
        if task is None:
            return
        provider_job_id = task.provider_job_id
        provider_callback(db, provider_job_id, "processing", callback_id=f"{provider_job_id}:processing")

    if task.tool_slug == "remove-watermark":
        _process_remove_watermark(task_id)
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


def _process_remove_watermark(task_id: str) -> None:
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
        input_storage_key = input_asset.storage_key

    try:
        result = process_watermark_removal(input_storage_key, task_id, params)
    except VideoProcessingError as exc:
        logger.warning("Video processing failed for task %s: %s", task_id, exc)
        _fail_provider_job(provider_job_id, "VIDEO_PROCESSING_FAILED")
        return
    except Exception:
        logger.exception("Unexpected video processing error for task %s", task_id)
        _fail_provider_job(provider_job_id, "VIDEO_PROCESSING_FAILED")
        return

    with SessionLocal() as db:
        task = db.get(Task, task_id)
        if task is None or task.status in {"succeeded", "failed", "cancelled"}:
            return
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


def _fail_provider_job(provider_job_id: str, error_code: str) -> None:
    with SessionLocal() as db:
        provider_callback(
            db,
            provider_job_id,
            "failed",
            callback_id=f"{provider_job_id}:failed",
            error_code=error_code,
        )


def run_worker() -> None:
    worker = Worker([task_queue()], connection=redis_connection())
    worker.work()


if __name__ == "__main__":
    run_worker()
