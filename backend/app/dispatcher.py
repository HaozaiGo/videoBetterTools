import time

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


def _remote_gpu_inflight_count(db) -> int:
    tasks = db.execute(
        select(Task)
        .where(Task.status == "processing", Task.tool_slug.in_(DISPATCHABLE_TOOL_SLUGS))
    ).scalars()
    count = 0
    for task in tasks:
        params = task.params if isinstance(task.params, dict) else {}
        if str(params.get("remoteGpuJobId") or "").strip():
            count += 1
    return count


def dispatch_provider_queue_once(limit: int | None = None) -> dict:
    limit = max(1, int(limit or settings.gpu_queue_dispatch_batch_size))
    now_seconds = int(time.time())
    already_enqueued = _queued_rq_task_ids()
    dispatched: list[str] = []

    with SessionLocal() as db:
        remote_inflight_limit = max(0, int(settings.gpu_remote_inflight_limit))
        if remote_inflight_limit and _remote_gpu_inflight_count(db) >= remote_inflight_limit:
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
