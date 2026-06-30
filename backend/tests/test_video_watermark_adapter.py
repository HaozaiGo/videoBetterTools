import json

import pytest

from app.video import watermark
from app.video import enhance, translate
from app.video.watermark import (
    GpuUnavailableError,
    VideoProcessingError,
    is_gpu_unavailable_error,
    _subtitle_text_mask,
    normalized_region_to_pixels,
    process_with_external_model,
    process_with_model_adapter,
    process_masked_video_removal,
    target_video_dimensions,
)


class FakeRemoteStorage:
    is_remote = True

    def __init__(self, root):
        self.root = root

    def local_path(self, storage_key: str):
        return self.root / storage_key

    def presign_download(self, storage_key: str, filename: str | None = None) -> str:
        return f"https://tos.example.com/{storage_key}"

    def save_file(self, storage_key: str, local_path):
        raise AssertionError("remote result metadata should avoid local save")

    def delete_local_copy(self, storage_key: str) -> bool:
        return True


def test_normalized_region_to_pixels_clamps_to_video_bounds() -> None:
    region = {"x": 0.9, "y": 0.8, "width": 0.5, "height": 0.5}

    assert normalized_region_to_pixels(region, 1000, 500) == (900, 400, 100, 100)


def test_target_video_dimensions_keep_aspect_ratio() -> None:
    assert target_video_dimensions(3840, 2160, {"resolution": "1080p"}) == (1920, 1080)
    assert target_video_dimensions(1080, 1920, {"resolution": "1080p"}) == (1080, 1920)


def test_unknown_model_adapter_fails_fast(tmp_path) -> None:
    input_path = tmp_path / "input.mp4"
    output_path = tmp_path / "output.mp4"
    input_path.write_bytes(b"not-a-real-video")

    with pytest.raises(VideoProcessingError, match="Unsupported video model adapter"):
        process_with_model_adapter(input_path, output_path, {"modelAdapter": "unknown"})


def test_gpu_unavailable_error_detection_matches_preflight_failures() -> None:
    assert is_gpu_unavailable_error("GPU API HTTP 503: GPU preflight failed: Failed to initialize NVML")
    assert is_gpu_unavailable_error("configured GPU metrics unavailable: 2,4,5")
    assert not is_gpu_unavailable_error("propainter command failed: invalid mask")


def test_external_model_raises_gpu_unavailable_for_preflight_failure(tmp_path, monkeypatch) -> None:
    input_path = tmp_path / "input.mp4"
    output_path = tmp_path / "output.mp4"
    input_path.write_bytes(b"fake-video")

    def fake_run(command, check, capture_output, text, env):
        raise watermark.subprocess.CalledProcessError(
            1,
            command,
            stderr="GPU API HTTP 503: GPU preflight failed: Failed to initialize NVML",
        )

    monkeypatch.setattr(watermark.settings, "propainter_command", "python runner.py")
    monkeypatch.setattr(watermark.subprocess, "run", fake_run)

    with pytest.raises(GpuUnavailableError):
        process_with_external_model(input_path, output_path, {"regions": [{"x": 0, "y": 0, "width": 1, "height": 1}]}, "propainter")


def test_subtitle_text_mask_targets_bright_text_only() -> None:
    import cv2
    import numpy as np

    frame = np.zeros((120, 240, 3), dtype=np.uint8)
    frame[:, :] = (40, 40, 40)
    cv2.putText(frame, "TEXT", (70, 72), cv2.FONT_HERSHEY_SIMPLEX, 1, (255, 255, 255), 2)
    region = {"x": 0.1, "y": 0.35, "width": 0.8, "height": 0.35}

    mask = _subtitle_text_mask(frame, [region], 6, {"textLightThreshold": 150})

    assert mask.max() == 255
    assert mask.sum() < 255 * 240 * 120 * 0.2


def test_subtitle_text_mask_includes_gold_text_shadow() -> None:
    import cv2
    import numpy as np

    frame = np.zeros((180, 360, 3), dtype=np.uint8)
    frame[:, :] = (45, 70, 190)
    cv2.putText(frame, "OK", (105, 105), cv2.FONT_HERSHEY_SIMPLEX, 2.2, (35, 45, 95), 14)
    cv2.putText(frame, "OK", (95, 95), cv2.FONT_HERSHEY_SIMPLEX, 2.2, (55, 190, 235), 10)
    region = {"x": 0.2, "y": 0.25, "width": 0.6, "height": 0.5}

    mask = _subtitle_text_mask(frame, [region], 3, {"textLightThreshold": 155})

    assert mask[105, 110] == 255
    assert mask.sum() < 255 * 360 * 180 * 0.3


def test_subtitle_text_mask_does_not_expand_plain_white_text_shadow() -> None:
    import cv2
    import numpy as np

    frame = np.zeros((180, 360, 3), dtype=np.uint8)
    frame[:, :] = (95, 45, 15)
    cv2.rectangle(frame, (95, 105), (260, 145), (60, 35, 20), thickness=-1)
    cv2.putText(frame, "NTU", (95, 95), cv2.FONT_HERSHEY_SIMPLEX, 1.8, (255, 255, 255), 8)
    region = {"x": 0.18, "y": 0.25, "width": 0.66, "height": 0.56}

    mask = _subtitle_text_mask(frame, [region], 3, {"textLightThreshold": 155})

    assert mask[125, 180] == 0
    assert mask.sum() < 255 * 360 * 180 * 0.12


def test_subtitle_text_mask_targets_black_subtitles() -> None:
    import cv2
    import numpy as np

    frame = np.zeros((220, 420, 3), dtype=np.uint8)
    frame[:, :] = (95, 95, 95)
    cv2.rectangle(frame, (40, 25), (250, 170), (95, 55, 20), thickness=-1)
    cv2.rectangle(frame, (160, 145), (350, 215), (25, 25, 25), thickness=-1)
    cv2.putText(frame, "BLACK", (75, 130), cv2.FONT_HERSHEY_SIMPLEX, 1.7, (5, 5, 5), 12)
    region = {"x": 0.08, "y": 0.36, "width": 0.84, "height": 0.36}

    mask = _subtitle_text_mask(frame, [region], 3, {"textLightThreshold": 155})

    assert mask[120, 120] == 255
    assert mask[185, 250] == 0


def test_external_model_receives_regions_and_full_params(tmp_path, monkeypatch) -> None:
    input_path = tmp_path / "input.mp4"
    output_path = tmp_path / "output.mp4"
    input_path.write_bytes(b"fake-video")
    captured = {}

    def fake_run(command, check, capture_output, text, env):
        captured["command"] = command
        captured["env"] = env

    monkeypatch.setattr(watermark.settings, "propainter_command", "python runner.py --input {input} --regions {regions} --params {params}")
    monkeypatch.setattr(watermark.subprocess, "run", fake_run)

    params = {"regions": [{"x": 0.1, "y": 0.2, "width": 0.3, "height": 0.4}], "maskPadding": 18, "keepAudio": False}
    process_with_external_model(input_path, output_path, params, "propainter")

    params_path = captured["env"]["MODEL_PLAZA_PARAMS"]
    assert "--params" in captured["command"]
    assert json.loads(open(params_path, encoding="utf-8").read()) == params


def test_external_model_can_return_remote_result_metadata(tmp_path, monkeypatch) -> None:
    input_path = tmp_path / "input.mp4"
    output_path = tmp_path / "output.mp4"
    input_path.write_bytes(b"fake-video")

    def fake_run(command, check, capture_output, text, env):
        meta_path = env["MODEL_PLAZA_RESULT_META"]
        with open(meta_path, "w", encoding="utf-8") as file:
            json.dump(
                {
                    "storage_key": "model-plaza/output/videos/job.mp4",
                    "url": "https://tos.example.com/model-plaza/output/videos/job.mp4",
                    "mime_type": "video/mp4",
                    "size_bytes": 123,
                },
                file,
            )

    monkeypatch.setattr(watermark.settings, "propainter_command", "python runner.py")
    monkeypatch.setattr(watermark.subprocess, "run", fake_run)

    result = process_with_external_model(input_path, output_path, {"regions": [{"x": 0, "y": 0, "width": 1, "height": 1}]}, "propainter")

    assert result == {
        "storage_key": "model-plaza/output/videos/job.mp4",
        "url": "https://tos.example.com/model-plaza/output/videos/job.mp4",
        "mime_type": "video/mp4",
        "size_bytes": 123,
    }


def test_remote_storage_prefers_presigned_input_url_even_with_local_cache(tmp_path, monkeypatch) -> None:
    storage_key = "model-plaza/input/videos/input.mp4"
    local_input = tmp_path / storage_key
    local_input.parent.mkdir(parents=True)
    local_input.write_bytes(b"cached-video")
    captured = {}

    def fake_run(command, check, capture_output, text, env):
        captured["env"] = env
        with open(env["MODEL_PLAZA_RESULT_META"], "w", encoding="utf-8") as file:
            json.dump(
                {
                    "storage_key": "model-plaza/output/videos/job.mp4",
                    "url": "https://tos.example.com/model-plaza/output/videos/job.mp4",
                    "mime_type": "video/mp4",
                    "size_bytes": 456,
                },
                file,
            )

    monkeypatch.setattr(watermark, "storage", FakeRemoteStorage(tmp_path))
    monkeypatch.setattr(watermark.settings, "upload_dir", str(tmp_path))
    monkeypatch.setattr(watermark.settings, "propainter_command", "python runner.py")
    monkeypatch.setattr(watermark.subprocess, "run", fake_run)

    result = process_masked_video_removal(
        storage_key,
        "task-1",
        {
            "modelAdapter": "propainter",
            "regions": [{"x": 0, "y": 0, "width": 1, "height": 1}],
        },
    )

    assert captured["env"]["MODEL_PLAZA_INPUT_URL"] == f"https://tos.example.com/{storage_key}"
    assert result["storage_key"] == "model-plaza/output/videos/job.mp4"


@pytest.mark.parametrize(
    ("module", "command_setting", "processor"),
    [
        (enhance, "enhance_command", enhance.process_video_enhance),
        (translate, "translate_command", translate.process_video_translate),
    ],
)
def test_remote_video_processors_prefer_presigned_input_url_with_local_cache(tmp_path, monkeypatch, module, command_setting, processor) -> None:
    storage_key = "model-plaza/input/videos/input.mp4"
    local_input = tmp_path / storage_key
    local_input.parent.mkdir(parents=True)
    local_input.write_bytes(b"cached-video")
    captured = {}

    def fake_run(command, check, capture_output, text, env):
        captured["env"] = env
        with open(env["MODEL_PLAZA_RESULT_META"], "w", encoding="utf-8") as file:
            json.dump(
                {
                    "storage_key": "model-plaza/output/videos/job.mp4",
                    "url": "https://tos.example.com/model-plaza/output/videos/job.mp4",
                    "mime_type": "video/mp4",
                    "size_bytes": 789,
                },
                file,
            )

    monkeypatch.setattr(module, "storage", FakeRemoteStorage(tmp_path))
    monkeypatch.setattr(module.settings, "upload_dir", str(tmp_path))
    monkeypatch.setattr(module.settings, command_setting, "python runner.py")
    monkeypatch.setattr(module.subprocess, "run", fake_run)

    result = processor(storage_key, "task-1", {})

    assert captured["env"]["MODEL_PLAZA_INPUT_URL"] == f"https://tos.example.com/{storage_key}"
    assert result["storage_key"] == "model-plaza/output/videos/job.mp4"
