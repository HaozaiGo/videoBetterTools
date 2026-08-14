import logging
import os
import time
from pathlib import Path

from rq import SimpleWorker, Worker, get_current_job

from app.config import settings
from app.database import SessionLocal
from app.models import Asset, Task
from app.queue import enqueue_provider_job, enqueue_result_finalize_job, named_queue, redis_connection, task_queue
from app.services import (
    INPUT_ASSET_REMOTE_MISSING_ERROR_CODE,
    INPUT_ASSET_REMOTE_MISSING_MESSAGE,
    create_internal_batch_zip,
    provider_callback,
)
from app.storage import StorageUploadUnavailableError, storage
from app.video.gpu_api import (
    RemoteGpuError,
    RemoteGpuJobNotFoundError,
    RemoteGpuUnavailableError,
    cancel_remote_video_job,
    download_remote_video_result,
    get_remote_video_job,
)
from app.video.enhance import process_video_enhance
from app.video.gptproto import GptProtoCancelled, GptProtoError, generate_video_redraw
from app.video.translate import process_video_translate
from app.video.watermark import GpuUnavailableError, VideoProcessingError, process_subtitle_removal, process_watermark_removal
from app.video.workflow import process_subtitle_translate_workflow

logger = logging.getLogger("model_plaza.worker")


class RemoteGpuResultNotReady(RemoteGpuUnavailableError):
    def __init__(self, message: str, result: dict, delay_seconds: int | None = None) -> None:
        super().__init__(message)
        self.result = result
        self.delay_seconds = max(1, int(delay_seconds or settings.result_finalize_defer_seconds))


def _input_asset_remote_readable(input_asset: Asset) -> bool:
    if not storage.is_remote:
        return True
    if input_asset.url and not input_asset.url.startswith(("http://", "https://")):
        return True
    return storage.remote_exists(input_asset.storage_key)


def _fail_unreadable_input_asset(db, provider_job_id: str) -> None:
    provider_callback(
        db,
        provider_job_id,
        "failed",
        callback_id=f"{provider_job_id}:input-asset-remote-missing",
        error_code=INPUT_ASSET_REMOTE_MISSING_ERROR_CODE,
        progress_stage=INPUT_ASSET_REMOTE_MISSING_MESSAGE,
    )


def _remember_remote_gpu_job(task: Task, remote_job_id: str, job_type: str = "") -> None:
    normalized_remote_job_id = str(remote_job_id or "").strip()
    if not normalized_remote_job_id:
        return
    params = dict(task.params or {})
    params["remoteGpuJobId"] = normalized_remote_job_id
    if job_type:
        params["remoteGpuJobType"] = str(job_type)
    history = params.get("remoteGpuJobIds")
    if not isinstance(history, list):
        history = []
    normalized_history = [str(item).strip() for item in history if str(item or "").strip()]
    if normalized_remote_job_id not in normalized_history:
        normalized_history.append(normalized_remote_job_id)
    params["remoteGpuJobIds"] = normalized_history[-6:]
    task.params = params


def _remember_remote_gpu_job_for_task(task_id: str, provider_job_id: str, result: dict) -> None:
    with SessionLocal() as db:
        task = db.get(Task, task_id)
        if task is None or task.provider_job_id != provider_job_id or task.status in {"succeeded", "failed", "cancelled"}:
            return
        _remember_remote_gpu_job(task, str(result.get("remote_job_id") or ""), str(result.get("job_type") or ""))
        db.commit()


def prepare_internal_batch_zip(user_id: str, batch_id: str) -> None:
    with SessionLocal() as db:
        try:
            create_internal_batch_zip(db, user_id, batch_id)
        except Exception:
            logger.exception("Failed to auto-prepare internal batch zip %s for user %s", batch_id, user_id)
            raise


def _finalize_result_payload(result: dict) -> dict:
    if result.get("local_path"):
        local_path = Path(str(result["local_path"]))
        storage_key = str(result["storage_key"])
        try:
            stored = storage.save_file(storage_key, local_path)
        except StorageUploadUnavailableError as exc:
            raise RemoteGpuUnavailableError(str(exc)) from exc
        if storage.is_remote:
            storage.delete_local_copy(stored.storage_key)
        return {
            "storage_key": stored.storage_key,
            "url": stored.public_url,
            "mime_type": str(result.get("mime_type") or "video/mp4"),
            "size_bytes": stored.size,
        }
    return {
        "storage_key": str(result["storage_key"]),
        "url": str(result["url"]),
        "mime_type": str(result.get("mime_type") or "video/mp4"),
        "size_bytes": int(result.get("size_bytes") or 0),
    }


def _remote_gpu_failure_error_code(exc: Exception) -> str:
    if not isinstance(exc, RemoteGpuError):
        return "RESULT_UPLOAD_FAILED"
    haystack = str(exc).lower()
    if "result_upload_timeout" in haystack or "result upload exceeded total timeout" in haystack:
        return "RESULT_UPLOAD_TIMEOUT"
    if "cuda_out_of_memory" in haystack or "cuda out of memory" in haystack or "torch.outofmemoryerror" in haystack:
        return "CUDA_OUT_OF_MEMORY"
    if "asr_no_segments" in haystack or "speech recognition returned no subtitle segments" in haystack:
        return "ASR_NO_SEGMENTS"
    if "video_decode_failed" in haystack or "invalid data found" in haystack or "moov atom not found" in haystack:
        return "VIDEO_DECODE_FAILED"
    if "gpu_stalled" in haystack or "stalled progress" in haystack or "stalled_progress" in haystack:
        return "GPU_STALLED"
    if "gpu_runner_failed" in haystack or "remote gpu job failed" in haystack:
        return "REMOTE_GPU_FAILED"
    return "RESULT_UPLOAD_FAILED"


def _remote_gpu_result_upload_timed_out(status: dict) -> bool:
    haystack = "\n".join(
        str(status.get(key) or "")
        for key in ("error", "progress_stage", "progressStage", "detail")
    ).lower()
    return "result_upload_timeout" in haystack or "result upload exceeded total timeout" in haystack


def _cleanup_finalize_intermediate_objects(result: dict) -> None:
    if not storage.is_remote:
        return
    keys = result.get("delete_storage_keys_after_finalize")
    if not isinstance(keys, list):
        return
    for storage_key in [str(item).strip() for item in keys if str(item or "").strip()]:
        try:
            storage.delete_remote(storage_key)
        except Exception:
            logger.warning("Failed to delete intermediate result object %s", storage_key, exc_info=True)


def finalize_provider_job_result(task_id: str, provider_job_id: str, result: dict) -> None:
    try:
        if result.get("workflow") == "subtitle-translate":
            finalized = _finalize_subtitle_translate_workflow_result(task_id, provider_job_id, result)
        else:
            finalized = _finalize_remote_gpu_result(task_id, provider_job_id, result) if result.get("remote_job_id") else _finalize_result_payload(result)
    except RemoteGpuJobNotFoundError as exc:
        logger.warning("Remote GPU job lost for task %s, requeueing provider job: %s", task_id, exc)
        _requeue_provider_job_for_lost_remote_gpu_job(task_id, provider_job_id, getattr(exc, "result", result), str(exc))
        return
    except RemoteGpuResultNotReady as exc:
        delay_seconds = max(1, int(exc.delay_seconds))
        logger.info("Remote GPU result not ready for task %s, deferring finalize by %ss: %s", task_id, delay_seconds, exc)
        _mark_result_finalize_deferred(task_id, provider_job_id, str(exc))
        enqueue_result_finalize_job(task_id, provider_job_id, exc.result, delay_seconds=delay_seconds)
        return
    except RemoteGpuUnavailableError as exc:
        logger.warning("Result finalize temporarily unavailable for task %s, will retry if possible: %s", task_id, exc)
        if _result_finalize_retries_left() > 0:
            _mark_result_finalize_retrying(task_id, provider_job_id, str(exc))
            raise
        logger.exception("Result finalize retries exhausted for task %s", task_id)
        _fail_provider_job(provider_job_id, "RESULT_UPLOAD_FAILED", str(exc))
        raise
    except RemoteGpuError as exc:
        logger.warning("Remote GPU job failed permanently for task %s: %s", task_id, exc)
        _fail_provider_job(provider_job_id, _remote_gpu_failure_error_code(exc), str(exc))
        return
    except Exception as exc:
        logger.exception("Failed to finalize result for task %s", task_id)
        _fail_provider_job(provider_job_id, _remote_gpu_failure_error_code(exc), str(exc))
        raise

    with SessionLocal() as db:
        task = db.get(Task, task_id)
        if task is None or task.provider_job_id != provider_job_id or task.status in {"succeeded", "failed", "cancelled"}:
            return
        provider_callback(
            db,
            provider_job_id,
            "succeeded",
            callback_id=f"{provider_job_id}:succeeded",
            output_url=finalized["url"],
            output_storage_key=finalized["storage_key"],
            output_mime_type=finalized["mime_type"],
            output_size_bytes=finalized["size_bytes"],
        )
    _cleanup_finalize_intermediate_objects(result)


def process_provider_job(task_id: str) -> None:
    with SessionLocal() as db:
        task = db.get(Task, task_id)
        if task is None or task.status != "queued":
            return
        provider_job_id = task.provider_job_id
        provider_callback(db, provider_job_id, "processing", callback_id=f"{provider_job_id}:processing")

    # 已接入真实视频处理能力的工具单独走 GPU/本地处理管线；其他工具仍保留模拟供应商结果。
    if task.tool_slug == "video-redraw":
        _process_gptproto_video_redraw_task(task_id)
        return

    if task.tool_slug in {"remove-watermark", "remove-subtitle", "enhance", "translate", "subtitle-translate-workflow"}:
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


def _process_gptproto_video_redraw_task(task_id: str) -> None:
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
        if not _input_asset_remote_readable(input_asset):
            _fail_unreadable_input_asset(db, task.provider_job_id)
            return
        provider_job_id = task.provider_job_id
        input_url = storage.presign_download(input_asset.storage_key, input_asset.original_name) if storage.is_remote else input_asset.url or ""
        params = dict(task.params or {})

    def progress(percent: int, stage: str) -> None:
        with SessionLocal() as progress_db:
            current = progress_db.get(Task, task_id)
            if current is None or current.provider_job_id != provider_job_id or current.status in {"succeeded", "failed", "cancelled"}:
                return
            provider_callback(
                progress_db,
                provider_job_id,
                "processing",
                callback_id=f"{provider_job_id}:gptproto-progress:{percent}:{hash(stage)}",
                progress_percent=percent,
                progress_stage=stage,
            )

    try:
        result = generate_video_redraw(input_url, task_id, params, progress)
    except GptProtoCancelled:
        return
    except GptProtoError as exc:
        logger.warning("GPTProto video redraw failed for task %s: %s", task_id, exc)
        _fail_provider_job(provider_job_id, "GPTPROTO_VIDEO_REDRAW_FAILED", str(exc))
        return
    except Exception as exc:
        logger.exception("Unexpected GPTProto video redraw error for task %s", task_id)
        _fail_provider_job(provider_job_id, "GPTPROTO_VIDEO_REDRAW_FAILED", str(exc))
        return

    with SessionLocal() as db:
        task = db.get(Task, task_id)
        if task is None or task.provider_job_id != provider_job_id or task.status in {"succeeded", "failed", "cancelled"}:
            return
        params = dict(task.params or {})
        params["gptprotoOperationId"] = result.get("gptproto_operation_id", "")
        params["providerModel"] = result.get("gptproto_model", params.get("providerModel", ""))
        task.params = params
        provider_callback(
            db,
            provider_job_id,
            "processing",
            callback_id=f"{provider_job_id}:result-finalize-queued",
            progress_percent=98,
            progress_stage="视频转绘结果已生成，等待上传对象存储",
        )
    enqueue_result_finalize_job(task_id, provider_job_id, result)


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
        if not _input_asset_remote_readable(input_asset):
            _fail_unreadable_input_asset(db, task.provider_job_id)
            return
        provider_job_id = task.provider_job_id
        params = dict(task.params or {})
        params["providerJobId"] = provider_job_id
        params["_defer_result_upload"] = True
        params["_async_remote_gpu"] = True
        params["_inputAssetName"] = input_asset.original_name
        input_storage_key = input_asset.storage_key
        if input_asset.url and storage.is_remote and input_asset.url != storage.public_url(input_storage_key):
            params["_inputAssetUrl"] = input_asset.url
        tool_slug = task.tool_slug

    # 耗时视频处理放在数据库会话之外，避免长时间占用连接和行锁。
    try:
        if tool_slug == "enhance":
            result = process_video_enhance(input_storage_key, task_id, params)
        elif tool_slug == "translate":
            result = process_video_translate(input_storage_key, task_id, params)
        elif tool_slug == "subtitle-translate-workflow":
            result = process_subtitle_translate_workflow(input_storage_key, task_id, params)
        elif tool_slug == "remove-subtitle":
            result = process_subtitle_removal(input_storage_key, task_id, params)
        else:
            result = process_watermark_removal(input_storage_key, task_id, params)
    except GpuUnavailableError as exc:
        logger.warning("GPU unavailable for task %s, requeueing: %s", task_id, exc)
        _requeue_provider_job_for_gpu_unavailable(task_id, provider_job_id, str(exc))
        return
    except VideoProcessingError as exc:
        logger.warning("Video processing failed for task %s: %s", task_id, exc)
        _fail_provider_job(provider_job_id, "VIDEO_PROCESSING_FAILED", str(exc))
        return
    except Exception as exc:
        logger.exception("Unexpected video processing error for task %s", task_id)
        _fail_provider_job(provider_job_id, "VIDEO_PROCESSING_FAILED", str(exc))
        return

    if result.get("remote_job_id"):
        with SessionLocal() as db:
            task = db.get(Task, task_id)
            if task is None or task.provider_job_id != provider_job_id or task.status in {"succeeded", "failed", "cancelled"}:
                return
            _remember_remote_gpu_job(task, str(result.get("remote_job_id") or ""), str(result.get("job_type") or tool_slug))
            provider_callback(
                db,
                provider_job_id,
                "processing",
                callback_id=f"{provider_job_id}:remote-gpu-submitted",
                progress_percent=10,
                progress_stage="远端 GPU 已提交，等待处理",
            )
        enqueue_result_finalize_job(task_id, provider_job_id, result)
        return

    if result.get("local_path"):
        with SessionLocal() as db:
            task = db.get(Task, task_id)
            if task is None or task.provider_job_id != provider_job_id or task.status in {"succeeded", "failed", "cancelled"}:
                return
            provider_callback(
                db,
                provider_job_id,
                "processing",
                callback_id=f"{provider_job_id}:result-finalize-queued",
                progress_percent=98,
                progress_stage="结果已生成，等待上传对象存储",
            )
        enqueue_result_finalize_job(task_id, provider_job_id, result)
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


def _sync_remote_gpu_progress(task_id: str, provider_job_id: str, remote_job_id: str, status: dict) -> bool:
    with SessionLocal() as db:
        task = db.get(Task, task_id)
        if task is None or task.provider_job_id != provider_job_id or task.status in {"succeeded", "failed", "cancelled"}:
            return False
        _remember_remote_gpu_job(task, remote_job_id, str(status.get("job_type") or status.get("jobType") or ""))
        state = str(status.get("status") or "queued")
        fallback_percent = {"queued": 10, "processing": 15, "uploading": 96, "succeeded": 100, "failed": 0, "cancelled": 0}.get(state, 0)
        percent = int(status.get("progress_percent") or fallback_percent)
        stage = str(status.get("progress_stage") or state)
        if state == "uploading":
            stage = stage if stage != "uploading" else "远端正在上传对象存储"
        provider_callback(
            db,
            provider_job_id,
            "processing",
            callback_id=f"{provider_job_id}:{remote_job_id}:progress:{percent}:{state}",
            progress_percent=percent,
            progress_stage=stage,
        )
        return True


def _finalize_remote_gpu_result(task_id: str, provider_job_id: str, result: dict) -> dict:
    remote_job_id = str(result["remote_job_id"])
    output_key = str(result["storage_key"])
    interval = max(1, int(os.environ.get("MODEL_PLAZA_GPU_POLL_INTERVAL", "5")))
    timeout = max(interval, int(os.environ.get("MODEL_PLAZA_GPU_POLL_TIMEOUT", str(settings.result_finalize_poll_timeout_seconds))))
    deadline = time.time() + timeout
    last_status: dict = {}
    try:
        while time.time() < deadline:
            try:
                status = get_remote_video_job(remote_job_id)
            except RemoteGpuJobNotFoundError as exc:
                setattr(exc, "result", result)
                setattr(exc, "remote_job_id", remote_job_id)
                raise
            except RemoteGpuUnavailableError as exc:
                last_status = {"status": "unavailable", "error": str(exc)}
                logger.warning("Remote GPU status temporarily unavailable for job %s: %s", remote_job_id, exc)
                time.sleep(interval)
                continue
            last_status = status
            if not _sync_remote_gpu_progress(task_id, provider_job_id, remote_job_id, status):
                return {
                    "storage_key": output_key,
                    "url": str(result.get("url") or storage.public_url(output_key)),
                    "mime_type": str(result.get("mime_type") or "video/mp4"),
                    "size_bytes": int(result.get("size_bytes") or 0),
                }
            state = str(status.get("status") or "")
            if state == "succeeded":
                storage_key = str(status.get("result_storage_key") or output_key)
                result_url = str(status.get("result_url") or result.get("url") or storage.public_url(storage_key))
                size_bytes = int(status.get("result_size_bytes") or result.get("size_bytes") or 0)
                if status.get("result_storage_key") and status.get("result_url"):
                    if storage.is_remote and not storage.remote_exists(storage_key):
                        raise RemoteGpuResultNotReady(f"remote GPU result is not visible in storage yet: {storage_key}", result)
                    return {
                        "storage_key": storage_key,
                        "url": result_url,
                        "mime_type": str(status.get("result_mime_type") or result.get("mime_type") or "video/mp4"),
                        "size_bytes": size_bytes,
                    }
                local_path = settings.upload_path / output_key
                download_remote_video_result(remote_job_id, local_path)
                return _finalize_result_payload(
                    {
                        "storage_key": output_key,
                        "local_path": str(local_path),
                        "mime_type": str(result.get("mime_type") or "video/mp4"),
                    }
                )
            if state == "failed":
                if _remote_gpu_result_upload_timed_out(status):
                    with SessionLocal() as recovery_db:
                        provider_callback(
                            recovery_db,
                            provider_job_id,
                            "processing",
                            callback_id=f"{provider_job_id}:{remote_job_id}:recover-result-upload-timeout",
                            progress_percent=98,
                            progress_stage="远端结果已生成，上传超时，正在改由平台拉回",
                        )
                    local_path = settings.upload_path / output_key
                    download_remote_video_result(remote_job_id, local_path)
                    return _finalize_result_payload(
                        {
                            "storage_key": output_key,
                            "local_path": str(local_path),
                            "mime_type": str(result.get("mime_type") or "video/mp4"),
                        }
                    )
                raise RemoteGpuError(f"remote GPU job failed: {status.get('error') or 'unknown error'}")
            if state == "cancelled":
                raise RemoteGpuError("remote GPU job was cancelled")
            time.sleep(interval)
    except RemoteGpuUnavailableError:
        raise
    except Exception:
        try:
            cancel_remote_video_job(remote_job_id)
        except Exception:
            logger.warning("Failed to cancel remote GPU job %s after result finalize error", remote_job_id, exc_info=True)
        raise
    raise RemoteGpuResultNotReady(f"remote GPU job is still pending after {timeout}s: {remote_job_id}; last_status={last_status}", result)


def _finalize_subtitle_translate_workflow_result(task_id: str, provider_job_id: str, result: dict) -> dict:
    intermediate = _finalize_remote_gpu_result(task_id, provider_job_id, result)
    translate_params = dict(result.get("translate_params") or {})
    translate_params["providerJobId"] = provider_job_id
    translate_params["_defer_result_upload"] = True
    translate_params["_async_remote_gpu"] = True
    with SessionLocal() as db:
        task = db.get(Task, task_id)
        if task is None or task.provider_job_id != provider_job_id or task.status in {"succeeded", "failed", "cancelled"}:
            return intermediate
        provider_callback(
            db,
            provider_job_id,
            "processing",
            callback_id=f"{provider_job_id}:subtitle-translate:translate-submit",
            progress_percent=55,
            progress_stage="字幕去除完成，正在提交翻译任务",
        )

    translate_result = process_video_translate(str(intermediate["storage_key"]), task_id, translate_params)
    translate_result["delete_storage_keys_after_finalize"] = [str(intermediate["storage_key"])]
    if translate_result.get("remote_job_id"):
        _remember_remote_gpu_job_for_task(task_id, provider_job_id, translate_result)
        finalized = _finalize_remote_gpu_result(task_id, provider_job_id, translate_result)
    elif translate_result.get("local_path"):
        finalized = _finalize_result_payload(translate_result)
    else:
        finalized = {
            "storage_key": str(translate_result["storage_key"]),
            "url": str(translate_result["url"]),
            "mime_type": str(translate_result.get("mime_type") or "video/mp4"),
            "size_bytes": int(translate_result.get("size_bytes") or 0),
        }
    if storage.is_remote:
        try:
            storage.delete_remote(str(intermediate["storage_key"]))
        except Exception:
            logger.warning("Failed to delete intermediate subtitle workflow object %s", intermediate["storage_key"], exc_info=True)
    return finalized


def _result_finalize_retries_left() -> int:
    job = get_current_job(connection=redis_connection())
    if job is None:
        return 0
    retries_left = getattr(job, "retries_left", None)
    return max(0, int(retries_left or 0))


def _mark_result_finalize_retrying(task_id: str, provider_job_id: str, progress_stage: str) -> None:
    with SessionLocal() as db:
        task = db.get(Task, task_id)
        if task is None or task.provider_job_id != provider_job_id or task.status in {"succeeded", "failed", "cancelled"}:
            return
        task.progress_percent = max(95, min(task.progress_percent, 99))
        task.progress_stage = f"结果回收暂时不可用，等待自动重试：{progress_stage}"[:160]
        db.commit()


def _mark_result_finalize_deferred(task_id: str, provider_job_id: str, progress_stage: str) -> None:
    with SessionLocal() as db:
        task = db.get(Task, task_id)
        if task is None or task.provider_job_id != provider_job_id or task.status in {"succeeded", "failed", "cancelled"}:
            return
        task.progress_stage = f"远端 GPU 仍在处理/等待结果回收，已释放 result worker，稍后自动检查：{progress_stage}"[:160]
        db.commit()


def _is_input_asset_remote_missing(progress_stage: str) -> bool:
    return "input asset is not available in remote storage" in progress_stage.lower()


def _is_remote_gpu_queue_full(progress_stage: str) -> bool:
    return "queue is full" in progress_stage.lower()


def _is_remote_gpu_disk_pressure(progress_stage: str) -> bool:
    lowered = progress_stage.lower()
    return "gpu disk pressure" in lowered or "requires at least" in lowered and "free" in lowered


def _requeue_provider_job_for_gpu_backpressure(task_id: str, provider_job_id: str, progress_stage: str = "") -> None:
    is_disk_pressure = _is_remote_gpu_disk_pressure(progress_stage)
    with SessionLocal() as db:
        task = db.get(Task, task_id)
        if task is None or task.provider_job_id != provider_job_id or task.status in {"succeeded", "failed", "cancelled"}:
            return
        params = dict(task.params or {})
        retries = int(params.get("_gpuQueueFullRetries") or 0) + 1
        params["_gpuQueueFullRetries"] = retries
        params["_gpuQueueFullLastAt"] = int(time.time())
        task.params = params
        max_retries = max(0, int(settings.gpu_queue_full_retry_max))
        if max_retries and retries > max_retries:
            provider_callback(
                db,
                provider_job_id,
                "failed",
                callback_id=f"{provider_job_id}:gpu-queue-full-retries-exhausted",
                error_code="REMOTE_GPU_QUEUE_FULL",
                progress_stage=(progress_stage or "远端 GPU 队列已满，已超过自动重试次数")[:160],
            )
            return
        task.status = "queued"
        task.error_code = None
        task.progress_percent = max(5, task.progress_percent)
        reason = "磁盘空间不足，等待前面任务完成释放空间" if is_disk_pressure else "队列已满"
        if max_retries:
            task.progress_stage = f"远端 GPU {reason}，保持队列顺序等待调度（{retries}/{max_retries}）"
        else:
            task.progress_stage = f"远端 GPU {reason}，保持队列顺序等待调度（第 {retries} 次）"
        db.commit()


def _requeue_provider_job_for_gpu_unavailable(task_id: str, provider_job_id: str, progress_stage: str = "") -> None:
    if _is_remote_gpu_queue_full(progress_stage) or _is_remote_gpu_disk_pressure(progress_stage):
        _requeue_provider_job_for_gpu_backpressure(task_id, provider_job_id, progress_stage)
        return

    should_requeue = False
    with SessionLocal() as db:
        task = db.get(Task, task_id)
        if task is None or task.provider_job_id != provider_job_id or task.status in {"succeeded", "failed", "cancelled"}:
            return
        params = dict(task.params or {})
        retries = int(params.get("_gpuUnavailableRetries") or 0) + 1
        params["_gpuUnavailableRetries"] = retries
        task.params = params
        max_retries = max(0, int(settings.gpu_unavailable_retry_max))
        if retries > max_retries:
            error_code = "INPUT_ASSET_REMOTE_MISSING" if _is_input_asset_remote_missing(progress_stage) else "REMOTE_GPU_UNAVAILABLE"
            provider_callback(
                db,
                provider_job_id,
                "failed",
                callback_id=f"{provider_job_id}:gpu-unavailable-retries-exhausted",
                error_code=error_code,
                progress_stage=(progress_stage or "远端 GPU 暂不可用，已超过自动重试次数")[:160],
            )
            return
        task.status = "queued"
        task.error_code = None
        task.progress_percent = max(5, task.progress_percent)
        task.progress_stage = f"远端 GPU 暂不可用，等待自动重试（{retries}/{max_retries}）"
        db.commit()
        should_requeue = True

    if not should_requeue:
        return

    enqueue_provider_job(task_id, delay_seconds=max(1, int(settings.gpu_unavailable_retry_delay_seconds)))


def _requeue_provider_job_for_lost_remote_gpu_job(task_id: str, provider_job_id: str, result: dict, progress_stage: str = "") -> None:
    remote_job_id = str(result.get("remote_job_id") or "").strip()
    with SessionLocal() as db:
        task = db.get(Task, task_id)
        if task is None or task.provider_job_id != provider_job_id or task.status in {"succeeded", "failed", "cancelled"}:
            return
        params = dict(task.params or {})
        retries = int(params.get("_remoteGpuJobNotFoundRetries") or 0) + 1
        params["_remoteGpuJobNotFoundRetries"] = retries
        if remote_job_id:
            lost_history = params.get("remoteGpuJobNotFoundIds")
            if not isinstance(lost_history, list):
                lost_history = []
            normalized_history = [str(item).strip() for item in lost_history if str(item or "").strip()]
            if remote_job_id not in normalized_history:
                normalized_history.append(remote_job_id)
            params["remoteGpuJobNotFoundIds"] = normalized_history[-10:]
        if params.get("remoteGpuJobId") == remote_job_id:
            params.pop("remoteGpuJobId", None)
            params.pop("remoteGpuJobType", None)
        task.params = params
        max_retries = max(0, int(settings.remote_gpu_job_not_found_retry_max))
        if max_retries and retries > max_retries:
            provider_callback(
                db,
                provider_job_id,
                "failed",
                callback_id=f"{provider_job_id}:remote-gpu-job-not-found",
                error_code="REMOTE_GPU_JOB_NOT_FOUND",
                progress_stage=(progress_stage or "远端 GPU 任务已不存在，超过自动重提次数")[:160],
            )
            return
        task.status = "queued"
        task.error_code = None
        task.progress_percent = max(5, min(task.progress_percent, 10))
        if max_retries:
            task.progress_stage = f"远端 GPU 任务已不存在，已回到平台队列等待重新提交（{retries}/{max_retries}）"
        else:
            task.progress_stage = f"远端 GPU 任务已不存在，已回到平台队列等待重新提交（第 {retries} 次）"
        db.commit()


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
    queue_names = [name.strip() for name in os.environ.get("MODEL_PLAZA_WORKER_QUEUES", "").split(",") if name.strip()]
    queues = [named_queue(name) for name in queue_names] if queue_names else [task_queue()]
    with_scheduler = os.environ.get("MODEL_PLAZA_WORKER_WITH_SCHEDULER", "0").lower() in {"1", "true", "yes"}
    worker = worker_class(queues, connection=redis_connection())
    worker.work(with_scheduler=with_scheduler)


if __name__ == "__main__":
    run_worker()
