import json
import time
import urllib.request
from collections import defaultdict
from datetime import timezone
from urllib.parse import urljoin

from sqlalchemy import select

from app.config import settings
from app.database import SessionLocal
from app.models import Task
from app.queue import enqueue_provider_job, task_queue

DISPATCHABLE_TOOL_SLUGS = {"remove-watermark", "remove-subtitle", "enhance", "translate", "subtitle-translate-workflow"}
ACTIVE_BATCH_REDIS_KEY = "model-plaza:gpu-queue:active-batch"


def _task_cooldown_ready(task: Task, now_seconds: int) -> bool:
    params = task.params if isinstance(task.params, dict) else {}
    last_full_at = int(params.get("_gpuQueueFullLastAt") or 0)
    if last_full_at <= 0:
        return True
    return now_seconds - last_full_at >= max(1, int(settings.gpu_queue_dispatch_cooldown_seconds))


def _task_priority_key(task: Task) -> tuple[int, int, int, int, int, float]:
    params = task.params if isinstance(task.params, dict) else {}
    zip_priority_rank = int(params.get("_zipPriorityRank") or 0)
    zip_priority_boost_at = int(params.get("_zipPriorityBoostAt") or 0)
    priority_boost_at = int(params.get("_manualPriorityBoostAt") or 0)
    priority_boost_count = int(params.get("_manualPriorityBoostCount") or 0)
    return (
        0 if zip_priority_rank > 0 else 1,
        zip_priority_rank if zip_priority_rank > 0 else 999_999,
        -zip_priority_boost_at,
        -priority_boost_count,
        -priority_boost_at,
        task.created_at.timestamp(),
    )


def _task_batch_member_key(task: Task) -> tuple[int, int, int, int, int, int, float]:
    params = task.params if isinstance(task.params, dict) else {}
    zip_priority_rank = int(params.get("_zipPriorityRank") or 0)
    zip_priority_boost_at = int(params.get("_zipPriorityBoostAt") or 0)
    priority_boost_at = int(params.get("_manualPriorityBoostAt") or 0)
    priority_boost_count = int(params.get("_manualPriorityBoostCount") or 0)
    episode, created_at = _task_episode_key(task)
    return (
        0 if zip_priority_rank > 0 else 1,
        zip_priority_rank if zip_priority_rank > 0 else 999_999,
        -zip_priority_boost_at,
        -priority_boost_count,
        -priority_boost_at,
        episode,
        created_at,
    )


def _task_batch_key(task: Task) -> str:
    params = task.params if isinstance(task.params, dict) else {}
    batch_id = str(params.get("internalBatchId") or "").strip()
    if batch_id:
        return f"internal:{task.user_id}:{batch_id}"
    return f"task:{task.id}"


def _task_episode_key(task: Task) -> tuple[int, float]:
    params = task.params if isinstance(task.params, dict) else {}
    try:
        episode = int(params.get("internalBatchIndex") or 0)
    except (TypeError, ValueError):
        episode = 0
    return (episode if episode > 0 else 999_999, task.created_at.timestamp())


def _batch_fifo_ordered_tasks(tasks: list[Task]) -> list[Task]:
    if not settings.gpu_queue_batch_fifo_enabled:
        return sorted(tasks, key=_task_priority_key)
    grouped: dict[str, list[Task]] = defaultdict(list)
    for task in tasks:
        grouped[_task_batch_key(task)].append(task)
    batches = list(grouped.values())
    for batch_tasks in batches:
        batch_tasks.sort(key=_task_batch_member_key)
    batches.sort(key=lambda batch_tasks: min(_task_priority_key(task) for task in batch_tasks))
    ordered: list[Task] = []
    for batch_tasks in batches:
        ordered.extend(batch_tasks)
    return ordered


def _queued_rq_task_ids() -> set[str]:
    queue = task_queue()
    task_ids: set[str] = set()
    job_ids = list(queue.get_job_ids())
    for registry_name in ("started_job_registry", "scheduled_job_registry", "deferred_job_registry"):
        registry = getattr(queue, registry_name, None)
        if registry is not None:
            job_ids.extend(registry.get_job_ids())
    for job_id in job_ids:
        job = queue.fetch_job(job_id)
        if job is None or not job.args:
            continue
        task_id = str(job.args[0] or "").strip()
        if task_id:
            task_ids.add(task_id)
    return task_ids


def _stored_active_batch_key() -> str:
    try:
        value = task_queue().connection.get(ACTIVE_BATCH_REDIS_KEY)
    except Exception:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="ignore")
    return str(value or "")


def _store_active_batch_key(batch_key: str) -> None:
    try:
        task_queue().connection.set(ACTIVE_BATCH_REDIS_KEY, batch_key)
    except Exception:
        return


def _clear_active_batch_key() -> None:
    try:
        task_queue().connection.delete(ACTIVE_BATCH_REDIS_KEY)
    except Exception:
        return


def _queued_dispatchable_tasks(db) -> list[Task]:
    return list(
        db.execute(
            select(Task)
            .where(Task.status == "queued", Task.tool_slug.in_(DISPATCHABLE_TOOL_SLUGS))
            .order_by(Task.created_at.asc())
        ).scalars()
    )


def _select_active_batch_key(db, queued_tasks: list[Task]) -> str:
    if not settings.gpu_queue_batch_fifo_enabled:
        return ""
    stored_key = _stored_active_batch_key().strip()
    queued_batch_keys = {_task_batch_key(task) for task in queued_tasks}
    ordered_tasks = _batch_fifo_ordered_tasks(queued_tasks)
    preferred_key = _task_batch_key(ordered_tasks[0]) if ordered_tasks else ""
    if stored_key and stored_key in queued_batch_keys:
        stored_priority = min(_task_priority_key(task) for task in queued_tasks if _task_batch_key(task) == stored_key)
        preferred_priority = min(_task_priority_key(task) for task in ordered_tasks if _task_batch_key(task) == preferred_key)
        if preferred_key and preferred_key != stored_key and preferred_priority < stored_priority:
            _store_active_batch_key(preferred_key)
            return preferred_key
        return stored_key
    if stored_key:
        _clear_active_batch_key()
    if not queued_tasks:
        return ""
    _store_active_batch_key(preferred_key)
    return preferred_key


def _reorder_queued_rq_jobs_batch_fifo(db, active_batch_key: str = "") -> int:
    if not settings.gpu_queue_batch_fifo_enabled:
        return 0
    try:
        queue = task_queue()
        job_ids = list(queue.job_ids)
    except Exception:
        return 0
    scan_size = max(0, int(settings.gpu_queue_batch_fifo_reorder_scan_size))
    if scan_size:
        job_ids = job_ids[:scan_size]
    if len(job_ids) <= 1:
        return 0

    task_id_by_job_id: dict[str, str] = {}
    for job_id in job_ids:
        try:
            job = queue.fetch_job(job_id)
        except Exception:
            continue
        if job is None or not job.args:
            continue
        task_id = str(job.args[0] or "").strip()
        if not task_id:
            continue
        normalized_job_id = str(job_id)
        task_id_by_job_id[normalized_job_id] = task_id
    if not task_id_by_job_id:
        return 0

    tasks = list(
        db.execute(
            select(Task)
            .where(Task.id.in_(set(task_id_by_job_id.values())))
        ).scalars()
    )
    task_by_id = {task.id: task for task in tasks}
    batch_first_position: dict[str, int] = {}
    batch_job_ids: dict[str, list[str]] = defaultdict(list)
    passthrough_index = 0
    for position, job_id in enumerate(job_ids):
        normalized_job_id = str(job_id)
        task_id = task_id_by_job_id.get(normalized_job_id)
        task = task_by_id.get(task_id or "")
        if task is None:
            batch_key = f"passthrough:{passthrough_index}"
            passthrough_index += 1
        else:
            batch_key = _task_batch_key(task)
        batch_first_position.setdefault(batch_key, position)
        batch_job_ids[batch_key].append(normalized_job_id)

    reordered_prefix: list[str] = []
    sorted_batch_keys = sorted(batch_job_ids, key=lambda key: batch_first_position[key])
    if active_batch_key:
        sorted_batch_keys = [key for key in sorted_batch_keys if key == active_batch_key]
    for batch_key in sorted_batch_keys:
        reordered_prefix.extend(
            sorted(
                batch_job_ids[batch_key],
                key=lambda job_id: _task_batch_member_key(task_by_id[task_id_by_job_id[job_id]])
                if job_id in task_id_by_job_id and task_id_by_job_id[job_id] in task_by_id
                else (1, 999_999, 0, 0, 0, 999_999, float(batch_first_position[batch_key])),
            )
        )
    current_prefix = [str(job_id) for job_id in job_ids]
    if not active_batch_key and reordered_prefix == current_prefix:
        return 0

    if active_batch_key:
        script = """
local key = KEYS[1]
local current = redis.call('LRANGE', key, 0, -1)
local current_set = {}
for i = 1, #current do
  current_set[current[i]] = true
end
local desired = {}
local scanned = {}
local after_delimiter = false
for i = 1, #ARGV do
  local id = ARGV[i]
  if id == '__SCANNED_JOB_IDS__' then
    after_delimiter = true
  elseif after_delimiter then
    scanned[id] = true
  else
    table.insert(desired, id)
    scanned[id] = true
  end
end
local next_items = {}
for i = 1, #desired do
  local id = desired[i]
  if current_set[id] then
    table.insert(next_items, id)
  end
end
for i = 1, #current do
  local id = current[i]
  if not scanned[id] then
    table.insert(next_items, id)
  end
end
redis.call('DEL', key)
if #next_items > 0 then
  redis.call('RPUSH', key, unpack(next_items))
end
return #next_items
"""
        queue.connection.eval(script, 1, queue.key, *reordered_prefix, "__SCANNED_JOB_IDS__", *current_prefix)
        return len(reordered_prefix)

    script = """
local key = KEYS[1]
local current = redis.call('LRANGE', key, 0, -1)
local current_set = {}
for i = 1, #current do
  current_set[current[i]] = true
end
local selected = {}
local next_items = {}
for i = 1, #ARGV do
  local id = ARGV[i]
  selected[id] = true
  if current_set[id] then
    table.insert(next_items, id)
  end
end
for i = 1, #current do
  local id = current[i]
  if not selected[id] then
    table.insert(next_items, id)
  end
end
redis.call('DEL', key)
if #next_items > 0 then
  redis.call('RPUSH', key, unpack(next_items))
end
return #next_items
"""
    queue.connection.eval(script, 1, queue.key, *reordered_prefix)
    return len(reordered_prefix)


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


def _remote_gpu_available_slots() -> int | None:
    base_url = settings.model_plaza_gpu_api_url.rstrip("/")
    if not base_url:
        return None
    headers = {"X-API-Key": settings.model_plaza_gpu_api_key} if settings.model_plaza_gpu_api_key else {}
    request = urllib.request.Request(urljoin(f"{base_url}/", "metrics"), headers=headers, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=6) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except Exception:
        return None

    try:
        capacity = int(payload.get("slotCapacity") or 0)
    except (TypeError, ValueError):
        capacity = 0
    running_jobs = payload.get("runningJobs")
    if isinstance(running_jobs, list):
        running = len(running_jobs)
    else:
        running_by_gpu = payload.get("runningByGpu")
        if isinstance(running_by_gpu, dict):
            running = sum(int(value or 0) for value in running_by_gpu.values())
        else:
            running = 0
    if capacity <= 0:
        gpus = payload.get("gpus")
        if isinstance(gpus, list):
            for gpu in gpus:
                if not isinstance(gpu, dict):
                    continue
                try:
                    capacity += int(gpu.get("workerSlotsTotal") or 0)
                except (TypeError, ValueError):
                    continue
    if capacity <= 0:
        return None
    return max(0, capacity - running)


def dispatch_provider_queue_once(limit: int | None = None) -> dict:
    limit = max(1, int(limit or settings.gpu_queue_dispatch_batch_size))
    now_seconds = int(time.time())
    dispatched: list[str] = []

    with SessionLocal() as db:
        queued_tasks = _queued_dispatchable_tasks(db)
        active_batch_key = _select_active_batch_key(db, queued_tasks)
        _reorder_queued_rq_jobs_batch_fifo(db, active_batch_key)
        already_enqueued = _queued_rq_task_ids()
        available_slots = _remote_gpu_available_slots()
        use_cooldown = available_slots is None
        if available_slots is None:
            remote_inflight_limit = max(0, int(settings.gpu_remote_inflight_limit))
            if remote_inflight_limit and _remote_gpu_inflight_count(db, now_seconds) >= remote_inflight_limit:
                return {"dispatched": 0, "taskIds": []}
        elif available_slots <= 0:
            return {"dispatched": 0, "taskIds": []}
        else:
            limit = min(limit, available_slots)
        tasks = _batch_fifo_ordered_tasks(queued_tasks)
        if active_batch_key:
            tasks = [task for task in tasks if _task_batch_key(task) == active_batch_key]
        for task in tasks:
            if len(dispatched) >= limit:
                break
            if task.id in already_enqueued:
                continue
            if use_cooldown and not _task_cooldown_ready(task, now_seconds):
                continue
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
