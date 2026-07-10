from datetime import timedelta

from redis import Redis
from rq import Queue, Retry

from app.config import settings


def redis_connection() -> Redis:
    return Redis.from_url(settings.redis_url)


def named_queue(name: str) -> Queue:
    return Queue(name, connection=redis_connection())


def task_queue() -> Queue:
    return named_queue("model-plaza-tasks")


def internal_batch_zip_queue() -> Queue:
    return named_queue("model-plaza-zips")


def result_queue() -> Queue:
    return named_queue("model-plaza-results")


def enqueue_provider_job(task_id: str, delay_seconds: int = 0) -> None:
    queue = task_queue()
    kwargs = {"job_timeout": settings.task_job_timeout_seconds, "result_ttl": 3600}
    if delay_seconds > 0:
        queue.enqueue_in(timedelta(seconds=delay_seconds), "app.worker.process_provider_job", task_id, **kwargs)
        return
    queue.enqueue("app.worker.process_provider_job", task_id, **kwargs)


def _internal_batch_zip_retry() -> Retry | None:
    max_retries = max(0, int(settings.internal_batch_zip_retry_max))
    if max_retries <= 0:
        return None
    interval = max(1, int(settings.internal_batch_zip_retry_interval_seconds))
    return Retry(max=max_retries, interval=[interval * (2**attempt) for attempt in range(max_retries)])


def _result_finalize_retry() -> Retry | None:
    max_retries = max(0, int(settings.result_finalize_retry_max))
    if max_retries <= 0:
        return None
    interval = max(1, int(settings.result_finalize_retry_interval_seconds))
    return Retry(max=max_retries, interval=[interval * (2**attempt) for attempt in range(max_retries)])


def enqueue_internal_batch_zip(user_id: str, batch_id: str) -> None:
    internal_batch_zip_queue().enqueue(
        "app.worker.prepare_internal_batch_zip",
        user_id,
        batch_id,
        job_timeout=settings.internal_batch_zip_gpu_timeout_seconds,
        result_ttl=3600,
        failure_ttl=86400,
        retry=_internal_batch_zip_retry(),
    )


def enqueue_result_finalize_job(task_id: str, provider_job_id: str, result: dict) -> None:
    result_queue().enqueue(
        "app.worker.finalize_provider_job_result",
        task_id,
        provider_job_id,
        result,
        job_timeout=settings.task_job_timeout_seconds,
        result_ttl=3600,
        failure_ttl=86400,
        retry=_result_finalize_retry(),
    )
