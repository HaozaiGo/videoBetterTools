from app.video.gpu_api import RemoteGpuError, RemoteGpuUnavailableError, can_submit_remote_video_job, submit_remote_video_job
from app.video.translate import process_video_translate, translated_output_key
from app.video.watermark import GpuUnavailableError, VideoProcessingError, process_subtitle_removal


def _should_run_subtitle_removal(params: dict) -> bool:
    value = params.get("runSubtitleRemoval", True)
    if isinstance(value, str):
        return value.strip().lower() not in {"0", "false", "no", "off"}
    return value is not False


def process_subtitle_translate_workflow(input_storage_key: str, task_id: str, params: dict) -> dict:
    subtitle_params = {
        **params,
        "_defer_result_upload": True,
        "mode": "manual",
        "removalTarget": "subtitle",
        "modelAdapter": params.get("modelAdapter") or "propainter",
        "maskStrategy": params.get("maskStrategy") or "subtitle-text",
    }
    translate_params = {
        **params,
        "targetLanguage": params.get("targetLanguage") or "en",
        "subtitlePlacement": params.get("subtitlePlacement") or "bottom",
        "keepAudio": params.get("keepAudio", True),
        "priority": params.get("priority") or "standard",
    }
    if not _should_run_subtitle_removal(params):
        return process_video_translate(input_storage_key, task_id, translate_params)

    if params.get("_async_remote_gpu") and can_submit_remote_video_job():
        try:
            return submit_remote_video_job(
                job_type="subtitle_translate",
                input_storage_key=input_storage_key,
                input_url=params.get("_inputAssetUrl"),
                output_key=translated_output_key(task_id, translate_params),
                params={
                    **params,
                    "subtitleParams": subtitle_params,
                    "translateParams": translate_params,
                },
                regions=subtitle_params.get("regions") or [],
            )
        except RemoteGpuUnavailableError as exc:
            raise GpuUnavailableError(str(exc)) from exc
        except RemoteGpuError as exc:
            raise VideoProcessingError(str(exc)) from exc

    intermediate = process_subtitle_removal(input_storage_key, task_id, subtitle_params)
    if intermediate.get("remote_job_id"):
        return {
            **intermediate,
            "workflow": "subtitle-translate",
            "translate_params": translate_params,
        }
    return process_video_translate(str(intermediate["storage_key"]), task_id, translate_params)
