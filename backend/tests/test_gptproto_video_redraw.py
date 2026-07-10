from pathlib import Path

import pytest

from app.video import gptproto


def test_video_redraw_uses_kling_v3_gptproto_endpoint(monkeypatch, tmp_path) -> None:
    requests: list[tuple[str, str, dict | None]] = []
    output = tmp_path / "result.mp4"
    output.write_bytes(b"fake mp4")

    def fake_request_json(url: str, payload=None, method="POST", token=None):
        requests.append((method, url, payload))
        if url.endswith("/video-to-video"):
            return {"data": {"id": "kling-prediction-1", "urls": {"get": "https://gptproto.example.test/api/v3/predictions/kling-prediction-1/result"}, "status": "created"}}
        return {"data": {"status": "completed", "outputs": ["https://cdn.example.test/result.mp4"]}}

    monkeypatch.setattr(gptproto, "_api_key", lambda: "sk-test")
    monkeypatch.setattr(gptproto, "_base_url", lambda: "https://gptproto.example.test")
    monkeypatch.setattr(gptproto, "_request_json", fake_request_json)
    monkeypatch.setattr(gptproto, "_download_video", lambda url, task_id: output)

    result = gptproto.generate_video_redraw(
        "https://cdn.example.test/input.mp4",
        "task-1",
        {
            "videoPrompt": "anime style",
            "resolution": "720p",
            "aspectRatio": "9:16",
            "providerModel": "kling-video-o1-std",
        },
        lambda percent, stage: None,
    )

    assert requests == [
        (
            "POST",
            "https://gptproto.example.test/api/v3/kwaivgi/kling-video-o1-std/video-to-video",
            {
                "prompt": "anime style",
                "video": "https://cdn.example.test/input.mp4",
                "aspect_ratio": "9:16",
                "duration": 5,
                "keep_original_sound": True,
            },
        ),
        (
            "GET",
            "https://gptproto.example.test/api/v3/predictions/kling-prediction-1/result",
            None,
        ),
    ]
    assert result["gptproto_model"] == "kling-video-o1-std"
    assert Path(result["local_path"]) == output


def test_video_redraw_uses_kling_pro_gptproto_endpoint(monkeypatch, tmp_path) -> None:
    requested_urls: list[str] = []
    output = tmp_path / "result.mp4"
    output.write_bytes(b"fake mp4")

    def fake_request_json(url: str, payload=None, method="POST", token=None):
        requested_urls.append(url)
        if url.endswith("/video-to-video"):
            return {"data": {"id": "kling-pro-prediction-1", "urls": {"get": "https://gptproto.example.test/api/v3/predictions/kling-pro-prediction-1/result"}}}
        return {"data": {"status": "completed", "outputs": ["https://cdn.example.test/result.mp4"]}}

    monkeypatch.setattr(gptproto, "_api_key", lambda: "sk-test")
    monkeypatch.setattr(gptproto, "_base_url", lambda: "https://gptproto.example.test")
    monkeypatch.setattr(gptproto, "_request_json", fake_request_json)
    monkeypatch.setattr(gptproto, "_download_video", lambda url, task_id: output)

    result = gptproto.generate_video_redraw(
        "https://cdn.example.test/input.mp4",
        "task-1",
        {"videoPrompt": "anime style", "providerModel": "kling-video-o1-pro"},
        lambda percent, stage: None,
    )

    assert requested_urls[0] == "https://gptproto.example.test/api/v3/kwaivgi/kling-video-o1-pro/video-to-video"
    assert result["gptproto_model"] == "kling-video-o1-pro"


def test_video_redraw_maps_legacy_kling_omni_name(monkeypatch, tmp_path) -> None:
    requested_urls: list[str] = []
    output = tmp_path / "result.mp4"
    output.write_bytes(b"fake mp4")

    def fake_request_json(url: str, payload=None, method="POST", token=None):
        requested_urls.append(url)
        if url.endswith("/video-to-video"):
            return {"data": {"id": "kling-prediction-1", "urls": {"get": "https://gptproto.example.test/api/v3/predictions/kling-prediction-1/result"}}}
        return {"data": {"status": "completed", "outputs": ["https://cdn.example.test/result.mp4"]}}

    monkeypatch.setattr(gptproto, "_api_key", lambda: "sk-test")
    monkeypatch.setattr(gptproto, "_base_url", lambda: "https://gptproto.example.test")
    monkeypatch.setattr(gptproto, "_request_json", fake_request_json)
    monkeypatch.setattr(gptproto, "_download_video", lambda url, task_id: output)

    result = gptproto.generate_video_redraw(
        "https://cdn.example.test/input.mp4",
        "task-1",
        {"videoPrompt": "anime style", "providerModel": "kling-v3-omni-std"},
        lambda percent, stage: None,
    )

    assert requested_urls[0] == "https://gptproto.example.test/api/v3/kwaivgi/kling-video-o1-std/video-to-video"
    assert result["gptproto_model"] == "kling-video-o1-std"


def test_video_redraw_defaults_to_veo_model(monkeypatch, tmp_path) -> None:
    requested_urls: list[str] = []
    output = tmp_path / "result.mp4"
    output.write_bytes(b"fake mp4")

    def fake_request_json(url: str, payload=None, method="POST", token=None):
        requested_urls.append(url)
        if url.endswith(":predictLongRunning"):
            return {"name": "operations/veo-operation-1"}
        return {"done": True, "response": {"video": {"uri": "https://cdn.example.test/result.mp4"}}}

    monkeypatch.setattr(gptproto, "_query_token", lambda: "at-test")
    monkeypatch.setattr(gptproto, "_base_url", lambda: "https://gptproto.example.test")
    monkeypatch.setattr(gptproto, "_request_json", fake_request_json)
    monkeypatch.setattr(gptproto, "_download_video", lambda url, task_id: output)

    result = gptproto.generate_video_redraw(
        "https://cdn.example.test/input.mp4",
        "task-1",
        {"videoPrompt": "anime style", "resolution": "720p", "aspectRatio": "16:9"},
        lambda percent, stage: None,
    )

    assert requested_urls[0] == "https://gptproto.example.test/v1beta/models/veo-3.1-generate-preview:predictLongRunning"
    assert result["gptproto_model"] == "veo-3.1-generate-preview"


def test_video_redraw_rejects_unknown_model(monkeypatch) -> None:
    monkeypatch.setattr(gptproto, "_query_token", lambda: "at-test")

    with pytest.raises(gptproto.GptProtoError, match="Unsupported GPTProto video redraw model"):
        gptproto.generate_video_redraw(
            "https://cdn.example.test/input.mp4",
            "task-1",
            {"videoPrompt": "anime style", "providerModel": "unknown-model"},
            lambda percent, stage: None,
        )
