import json
import os
import shlex
import subprocess
from pathlib import Path
from typing import Any

from app.config import settings
from app.storage import storage
from app.video.watermark import VideoProcessingError


def _output_key(task_id: str) -> str:
    return f"{task_id}-translated.mp4"


def _is_http_url(value: str) -> bool:
    return value.startswith("http://") or value.startswith("https://")


def _result_upload_target(output_key: str) -> dict[str, Any] | None:
    if not storage.is_remote:
        return None
    presign = storage.presign_upload(kind="result", duration_seconds=0, storage_key=output_key)
    if presign.get("mode") != "tos-put":
        return None
    return {
        "upload_url": presign["uploadUrl"],
        "headers": presign.get("headers") or {},
        "storage_key": output_key,
        "url": storage.public_url(output_key),
    }


def _final_result(output_key: str, output_path: Path) -> dict:
    stored = storage.save_file(output_key, output_path)
    return {
        "storage_key": stored.storage_key,
        "url": stored.public_url,
        "mime_type": "video/mp4",
        "size_bytes": stored.size,
    }


def _run_translate_command(
    input_path: Path,
    output_path: Path,
    params: dict[str, Any],
    input_url: str | None = None,
    result_upload: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    if not settings.translate_command:
        raise VideoProcessingError("translate command is not configured.")

    work_dir = output_path.with_suffix(".translate-work")
    work_dir.mkdir(parents=True, exist_ok=True)
    params_path = work_dir / "params.json"
    result_meta_path = work_dir / "result.json"
    params_path.write_text(json.dumps(params, ensure_ascii=False), encoding="utf-8")

    env = os.environ.copy()
    env["MODEL_PLAZA_INPUT"] = str(input_path)
    env["MODEL_PLAZA_OUTPUT"] = str(output_path)
    env["MODEL_PLAZA_PARAMS"] = str(params_path)
    env["MODEL_PLAZA_WORKDIR"] = str(work_dir)
    env["MODEL_PLAZA_RESULT_META"] = str(result_meta_path)
    env["MODEL_PLAZA_CANCEL_FILE"] = str(settings.upload_path / f"{params.get('taskId') or ''}.cancel")
    env["MODEL_PLAZA_PROVIDER_JOB_ID"] = str(params.get("providerJobId") or "")
    env["MODEL_PLAZA_CALLBACK_URL"] = os.environ.get("MODEL_PLAZA_CALLBACK_URL", "http://127.0.0.1:8010/api/provider/callback")
    if input_url:
        env["MODEL_PLAZA_INPUT_URL"] = input_url
    if result_upload:
        env["MODEL_PLAZA_RESULT_UPLOAD_URL"] = str(result_upload["upload_url"])
        env["MODEL_PLAZA_RESULT_UPLOAD_HEADERS"] = json.dumps(result_upload.get("headers") or {}, ensure_ascii=False)
        env["MODEL_PLAZA_RESULT_STORAGE_KEY"] = str(result_upload["storage_key"])
        env["MODEL_PLAZA_RESULT_URL"] = str(result_upload["url"])

    command = [
        part.format(
            input=str(input_path),
            output=str(output_path),
            params=str(params_path),
            workdir=str(work_dir),
        )
        for part in shlex.split(settings.translate_command)
    ]
    try:
        subprocess.run(command, check=True, capture_output=True, text=True, env=env)
    except subprocess.CalledProcessError as exc:
        detail = "\n".join(part for part in [exc.stdout, exc.stderr] if part).strip()
        tail = detail[-800:] if detail else str(exc)
        raise VideoProcessingError(f"translate command failed: {tail}") from exc
    if result_meta_path.exists():
        return json.loads(result_meta_path.read_text(encoding="utf-8"))
    return None


def process_video_translate(input_storage_key: str, task_id: str, params: dict) -> dict:
    params = {**params, "taskId": task_id}
    input_url = storage.public_url(input_storage_key)
    input_path = storage.local_path(input_storage_key)
    if not input_path.exists():
        if _is_http_url(input_url):
            input_path.parent.mkdir(parents=True, exist_ok=True)
        else:
            try:
                input_path = storage.ensure_local(input_storage_key)
            except FileNotFoundError as exc:
                raise VideoProcessingError("Input video file not found.") from exc

    output_key = _output_key(task_id)
    output_path = settings.upload_path / output_key
    output_path.parent.mkdir(parents=True, exist_ok=True)
    remote_result = _run_translate_command(
        input_path,
        output_path,
        params,
        input_url=input_url if _is_http_url(input_url) else None,
        result_upload=_result_upload_target(output_key),
    )
    if remote_result:
        return {
            "storage_key": str(remote_result["storage_key"]),
            "url": str(remote_result["url"]),
            "mime_type": str(remote_result.get("mime_type") or "video/mp4"),
            "size_bytes": int(remote_result.get("size_bytes") or 0),
        }
    return _final_result(output_key, output_path)
