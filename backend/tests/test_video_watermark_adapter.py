import pytest

from app.video.watermark import VideoProcessingError, normalized_region_to_pixels, process_with_model_adapter


def test_normalized_region_to_pixels_clamps_to_video_bounds() -> None:
    region = {"x": 0.9, "y": 0.8, "width": 0.5, "height": 0.5}

    assert normalized_region_to_pixels(region, 1000, 500) == (900, 400, 100, 100)


def test_unknown_model_adapter_fails_fast(tmp_path) -> None:
    input_path = tmp_path / "input.mp4"
    output_path = tmp_path / "output.mp4"
    input_path.write_bytes(b"not-a-real-video")

    with pytest.raises(VideoProcessingError, match="Unsupported video model adapter"):
        process_with_model_adapter(input_path, output_path, {"modelAdapter": "unknown"})
