from app import queue


class FakeQueue:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def enqueue(self, *args, **kwargs) -> None:
        self.calls.append({"args": args, "kwargs": kwargs})

    def enqueue_in(self, *args, **kwargs) -> None:
        self.calls.append({"args": args, "kwargs": kwargs})


def test_internal_batch_zip_enqueue_uses_retry_policy(monkeypatch) -> None:
    fake_queue = FakeQueue()
    monkeypatch.setattr(queue, "internal_batch_zip_queue", lambda: fake_queue)
    monkeypatch.setattr(queue.settings, "internal_batch_zip_retry_max", 3)
    monkeypatch.setattr(queue.settings, "internal_batch_zip_retry_interval_seconds", 30)

    queue.enqueue_internal_batch_zip("user-1", "batch-1")

    call = fake_queue.calls[0]
    retry = call["kwargs"]["retry"]
    assert call["args"] == ("app.worker.prepare_internal_batch_zip", "user-1", "batch-1")
    assert call["kwargs"]["failure_ttl"] == 86400
    assert retry.max == 3
    assert retry.intervals == [30, 60, 120]


def test_internal_batch_zip_enqueue_can_disable_retries(monkeypatch) -> None:
    fake_queue = FakeQueue()
    monkeypatch.setattr(queue, "internal_batch_zip_queue", lambda: fake_queue)
    monkeypatch.setattr(queue.settings, "internal_batch_zip_retry_max", 0)

    queue.enqueue_internal_batch_zip("user-1", "batch-1")

    assert fake_queue.calls[0]["kwargs"]["retry"] is None


def test_internal_batch_zip_enqueue_can_prioritize_front(monkeypatch) -> None:
    fake_queue = FakeQueue()
    monkeypatch.setattr(queue, "internal_batch_zip_queue", lambda: fake_queue)

    queue.enqueue_internal_batch_zip("user-1", "batch-1", at_front=True)

    call = fake_queue.calls[0]
    assert call["args"] == ("app.worker.prepare_internal_batch_zip", "user-1", "batch-1")
    assert call["kwargs"]["at_front"] is True


def test_result_finalize_enqueue_targets_result_queue(monkeypatch) -> None:
    fake_queue = FakeQueue()
    monkeypatch.setattr(queue, "result_queue", lambda: fake_queue)

    result = {"storage_key": "task-result.mp4", "local_path": "/tmp/task-result.mp4", "mime_type": "video/mp4"}
    queue.enqueue_result_finalize_job("task-1", "provider-1", result)

    call = fake_queue.calls[0]
    assert call["args"] == ("app.worker.finalize_provider_job_result", "task-1", "provider-1", result)
    assert call["kwargs"]["failure_ttl"] == 86400


def test_provider_enqueue_can_delay_with_scheduler(monkeypatch) -> None:
    fake_queue = FakeQueue()
    monkeypatch.setattr(queue, "task_queue", lambda: fake_queue)

    queue.enqueue_provider_job("task-1", delay_seconds=300)

    call = fake_queue.calls[0]
    delay = call["args"][0]
    assert delay.total_seconds() == 300
    assert call["args"][1:] == ("app.worker.process_provider_job", "task-1")
    assert call["kwargs"]["result_ttl"] == 3600


def test_provider_enqueue_can_prioritize_front(monkeypatch) -> None:
    fake_queue = FakeQueue()
    monkeypatch.setattr(queue, "task_queue", lambda: fake_queue)

    queue.enqueue_provider_job("task-priority", at_front=True)

    call = fake_queue.calls[0]
    assert call["args"] == ("app.worker.process_provider_job", "task-priority")
    assert call["kwargs"]["at_front"] is True
