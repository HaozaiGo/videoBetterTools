from redis import Redis
from rq import Queue

from app.config import settings


def redis_connection() -> Redis:
    return Redis.from_url(settings.redis_url)


def task_queue() -> Queue:
    return Queue("model-plaza-tasks", connection=redis_connection())


def enqueue_provider_job(task_id: str) -> None:
    task_queue().enqueue("app.worker.process_provider_job", task_id, job_timeout=settings.task_job_timeout_seconds, result_ttl=3600)
