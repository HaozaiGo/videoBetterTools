import io
import urllib.error
import urllib.request

import pytest

from app.video import workflow
from app.video import gpu_api


class FakeRemoteStorage:
    is_remote = True

    def __init__(self, visible_after_checks: int) -> None:
        self.visible_after_checks = visible_after_checks
        self.checks = 0

    def remote_exists(self, storage_key: str) -> bool:
        self.checks += 1
        return self.checks >= self.visible_after_checks

    def presign_upload(self, kind: str, storage_key: str) -> dict:
        return {
            "mode": "tos-put",
            "uploadUrl": f"https://upload.example.test/{storage_key}",
            "headers": {"Content-Type": "video/mp4"},
        }

    def presign_download(self, storage_key: str) -> str:
        return f"https://download.example.test/{storage_key}"

    def public_url(self, storage_key: str) -> str:
        return f"https://cdn.example.test/{storage_key}"


def test_submit_remote_video_job_waits_for_input_storage_to_be_visible(monkeypatch) -> None:
    storage = FakeRemoteStorage(visible_after_checks=3)
    sleep_calls: list[float] = []
    requests: list[object] = []

    monkeypatch.setattr(gpu_api, "storage", storage)
    monkeypatch.setattr(gpu_api.settings, "model_plaza_gpu_api_url", "https://gpu.example.test")
    monkeypatch.setattr(gpu_api.settings, "remote_storage_ready_timeout_seconds", 20)
    monkeypatch.setattr(gpu_api.settings, "remote_storage_ready_poll_seconds", 2)
    monkeypatch.setattr(gpu_api.time, "sleep", lambda seconds: sleep_calls.append(seconds))
    monkeypatch.setattr(gpu_api, "_request_json", lambda request, timeout=30: requests.append(request) or {"job_id": "remote-job-1"})

    result = gpu_api.submit_remote_video_job(
        job_type="translate",
        input_storage_key="model-plaza/input/videos/input.mp4",
        output_key="model-plaza/output/videos/output.mp4",
        params={"taskId": "task-1"},
    )

    assert storage.checks == 3
    assert sleep_calls == [2, 2]
    assert len(requests) == 1
    assert result["remote_job_id"] == "remote-job-1"


def test_submit_remote_video_job_fails_after_storage_visibility_timeout(monkeypatch) -> None:
    storage = FakeRemoteStorage(visible_after_checks=99)
    sleep_calls: list[float] = []

    monkeypatch.setattr(gpu_api, "storage", storage)
    monkeypatch.setattr(gpu_api.settings, "model_plaza_gpu_api_url", "https://gpu.example.test")
    monkeypatch.setattr(gpu_api.settings, "remote_storage_ready_timeout_seconds", 0)
    monkeypatch.setattr(gpu_api.settings, "remote_storage_ready_poll_seconds", 2)
    monkeypatch.setattr(gpu_api.time, "sleep", lambda seconds: sleep_calls.append(seconds))

    with pytest.raises(gpu_api.RemoteGpuUnavailableError, match="after waiting 0s"):
        gpu_api.submit_remote_video_job(
            job_type="translate",
            input_storage_key="model-plaza/input/videos/input.mp4",
            output_key="model-plaza/output/videos/output.mp4",
            params={"taskId": "task-1"},
        )

    assert storage.checks == 1
    assert sleep_calls == []


def test_gpu_api_job_not_found_is_treated_as_unavailable(monkeypatch) -> None:
    def raise_job_not_found(*_args, **_kwargs):
        raise urllib.error.HTTPError(
            url="https://gpu.example.test/jobs/missing",
            code=404,
            msg="Not Found",
            hdrs={},
            fp=io.BytesIO(b'{"detail":"job not found"}'),
        )

    monkeypatch.setattr(gpu_api.urllib.request, "urlopen", raise_job_not_found)
    request = urllib.request.Request("https://gpu.example.test/jobs/missing")

    with pytest.raises(gpu_api.RemoteGpuJobNotFoundError, match="job not found"):
        gpu_api._request_json(request)


def test_gpu_api_regular_404_remains_remote_error(monkeypatch) -> None:
    def raise_regular_404(*_args, **_kwargs):
        raise urllib.error.HTTPError(
            url="https://gpu.example.test/other",
            code=404,
            msg="Not Found",
            hdrs={},
            fp=io.BytesIO(b'{"detail":"zip not found"}'),
        )

    monkeypatch.setattr(gpu_api.urllib.request, "urlopen", raise_regular_404)
    request = urllib.request.Request("https://gpu.example.test/other")

    with pytest.raises(gpu_api.RemoteGpuError, match="zip not found"):
        gpu_api._request_json(request)


def test_subtitle_translate_workflow_submits_single_remote_gpu_job(monkeypatch) -> None:
    submitted: list[dict] = []

    monkeypatch.setattr(workflow, "can_submit_remote_video_job", lambda: True)
    monkeypatch.setattr(
        workflow,
        "submit_remote_video_job",
        lambda **kwargs: submitted.append(kwargs)
        or {
            "remote_job_id": "remote-workflow-1",
            "storage_key": kwargs["output_key"],
            "url": f"https://cdn.example.test/{kwargs['output_key']}",
            "mime_type": "video/mp4",
            "size_bytes": 0,
        },
    )
    monkeypatch.setattr(workflow, "process_subtitle_removal", lambda *args, **kwargs: pytest.fail("intermediate subtitle task should stay on GPU"))
    monkeypatch.setattr(workflow, "process_video_translate", lambda *args, **kwargs: pytest.fail("translate task should stay on GPU"))

    result = workflow.process_subtitle_translate_workflow(
        "model-plaza/input/videos/input.mp4",
        "task-workflow",
        {
            "_async_remote_gpu": True,
            "regions": [{"x": 0, "y": 0.75, "width": 1, "height": 0.08}],
            "targetLanguage": "en",
            "subtitlePlacement": "bottom",
            "keepAudio": True,
        },
    )

    assert result["remote_job_id"] == "remote-workflow-1"
    assert len(submitted) == 1
    assert submitted[0]["job_type"] == "subtitle_translate"
    assert submitted[0]["input_storage_key"] == "model-plaza/input/videos/input.mp4"
    assert submitted[0]["output_key"] == "task-workflow-translated.mp4"
    assert submitted[0]["regions"] == [{"x": 0, "y": 0.75, "width": 1, "height": 0.08}]
    assert submitted[0]["params"]["subtitleParams"]["removalTarget"] == "subtitle"
    assert submitted[0]["params"]["translateParams"]["targetLanguage"] == "en"
