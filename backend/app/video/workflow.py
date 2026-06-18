from app.video.translate import process_video_translate
from app.video.watermark import process_subtitle_removal


def process_subtitle_translate_workflow(input_storage_key: str, task_id: str, params: dict) -> dict:
    subtitle_params = {
        **params,
        "mode": "manual",
        "removalTarget": "subtitle",
        "modelAdapter": params.get("modelAdapter") or "propainter",
        "maskStrategy": params.get("maskStrategy") or "subtitle-text",
    }
    intermediate = process_subtitle_removal(input_storage_key, task_id, subtitle_params)
    translate_params = {
        **params,
        "targetLanguage": params.get("targetLanguage") or "en",
        "subtitlePlacement": params.get("subtitlePlacement") or "bottom",
        "keepAudio": params.get("keepAudio", True),
        "priority": params.get("priority") or "standard",
    }
    return process_video_translate(str(intermediate["storage_key"]), task_id, translate_params)
