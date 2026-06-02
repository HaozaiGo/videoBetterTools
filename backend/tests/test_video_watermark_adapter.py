import json

import pytest

from app.video import watermark
from app.video.watermark import VideoProcessingError, _subtitle_text_mask, normalized_region_to_pixels, process_with_external_model, process_with_model_adapter


def test_normalized_region_to_pixels_clamps_to_video_bounds() -> None:
    region = {"x": 0.9, "y": 0.8, "width": 0.5, "height": 0.5}

    assert normalized_region_to_pixels(region, 1000, 500) == (900, 400, 100, 100)


def test_unknown_model_adapter_fails_fast(tmp_path) -> None:
    input_path = tmp_path / "input.mp4"
    output_path = tmp_path / "output.mp4"
    input_path.write_bytes(b"not-a-real-video")

    with pytest.raises(VideoProcessingError, match="Unsupported video model adapter"):
        process_with_model_adapter(input_path, output_path, {"modelAdapter": "unknown"})


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
