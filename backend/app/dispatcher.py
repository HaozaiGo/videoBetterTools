import time
from datetime import timezone

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.config import settings
from app.database import SessionLocal
from app.models import Task
from app.queue import enqueue_provider_job, task_queue

DISPATCHABLE_TOOL_SLUGS = {"remove-watermark", "remove-subtitle", "enhance", "translate", "subtitle-translate-workflow"}


def _task_cooldown_ready(task: Task, now_seconds: int) -> bool:
    params = task.params if isinstance(task.params, dict) else {}
    last_full_at = int(params.get("_gpuQueueFullLastAt") or 0)
    if last_full_at <= 0:
        return True
    return now_seconds - last_full_at >= max(1, int(settings.gpu_queue_dispatch_cooldown_seconds))


def _task_priority_key(task: Task) -> tuple[int, int, float]:
    params = task.params if isinstance(task.params, dict) else {}
    priority_boost_at = int(params.get("_manualPriorityBoostAt") or 0)
    priority_boost_count = int(params.get("_manualPriorityBoostCount") or 0)
    return (-priority_boost_count, -priority_boost_at, task.created_at.timestamp())


def _queued_rq_task_ids() -> set[str]:
    queue = task_queue()
    task_ids: set[str] = set()
    job_ids = list(queue.get_job_ids())
    started_registry = getattr(queue, "started_job_registry", None)
    if started_registry is not None:
        job_ids.extend(started_registry.get_job_ids())
    for job_id in job_ids:
        job = queue.fetch_job(job_id)
        if job is None or not job.args:
            continue
        task_id = str(job.args[0] or "").strip()
        if task_id:
            task_ids.add(task_id)
    return task_ids


def _task_created_at_seconds(task: Task) -> int:
    created_at = task.created_at
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=timezone.utc)
    return int(created_at.timestamp())


def _remote_gpu_submission_is_fresh(task: Task, now_seconds: int) -> bool:
    stale_seconds = max(1, int(settings.gpu_remote_inflight_stale_seconds))
    params = task.params if isinstance(task.params, dict) else {}
    submitted_at = int(params.get("remoteGpuSubmittedAt") or 0)
    if submitted_at > 0:
        return now_seconds - submitted_at <= stale_seconds
    return now_seconds - _task_created_at_seconds(task) <= stale_seconds


def _remote_gpu_task_occupies_inflight_slot(task: Task, now_seconds: int) -> bool:
    params = task.params if isinstance(task.params, dict) else {}
    if not str(params.get("remoteGpuJobId") or "").strip():
        return False
    stage = task.progress_stage or ""
    if "回传结果" in stage or "结果回收" in stage or "远端处理完成" in stage:
        return False
    if not _remote_gpu_submission_is_fresh(task, now_seconds):
        return False
    return True


def _remote_gpu_inflight_count(db, now_seconds: int | None = None) -> int:
    now_seconds = int(now_seconds or time.time())
    tasks = db.execute(
        select(Task)
        .where(Task.status == "processing", Task.tool_slug.in_(DISPATCHABLE_TOOL_SLUGS))
    ).scalars()
    return sum(1 for task in tasks if _remote_gpu_task_occupies_inflight_slot(task, now_seconds))


def dispatch_provider_queue_once(limit: int | None = None) -> dict:
    limit = max(1, int(limit or settings.gpu_queue_dispatch_batch_size))
    now_seconds = int(time.time())
    already_enqueued = _queued_rq_task_ids()
    dispatched: list[str] = []

    with SessionLocal() as db:
        remote_inflight_limit = max(0, int(settings.gpu_remote_inflight_limit))
        if remote_inflight_limit and _remote_gpu_inflight_count(db, now_seconds) >= remote_inflight_limit:
            return {"dispatched": 0, "taskIds": []}
        tasks = list(
            db.execute(
                select(Task)
                .where(Task.status == "queued", Task.tool_slug.in_(DISPATCHABLE_TOOL_SLUGS))
                .options(selectinload(Task.input_asset))
                .order_by(Task.created_at.asc())
                .limit(max(limit * 8, 50))
            ).scalars()
        )
        tasks.sort(key=_task_priority_key)
        for task in tasks:
            if len(dispatched) >= limit:
                break
            if task.id in already_enqueued:
                continue
            if not _task_cooldown_ready(task, now_seconds):
                break
            enqueue_provider_job(task.id)
            already_enqueued.add(task.id)
            dispatched.append(task.id)
    return {"dispatched": len(dispatched), "taskIds": dispatched}


def run_dispatcher() -> None:
    interval = max(1, int(settings.gpu_queue_dispatch_interval_seconds))
    while True:
        try:
            dispatch_provider_queue_once()
        except Exception as exc:
            print(f"provider queue dispatcher failed: {exc}", flush=True)
        time.sleep(interval)


if __name__ == "__main__":
    run_dispatcher()
