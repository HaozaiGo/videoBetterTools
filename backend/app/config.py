from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    database_url: str = "postgresql+psycopg://jason@127.0.0.1:5432/model_plaza"
    database_pool_size: int = 20
    database_max_overflow: int = 20
    database_pool_timeout_seconds: int = 10
    database_pool_recycle_seconds: int = 1800
    redis_url: str = "redis://127.0.0.1:6379/0"
    task_job_timeout_seconds: int = 7200
    upload_dir: str = "../data/uploads"
    public_upload_prefix: str = "/uploads"
    demo_user_id: str = "demo-user"
    demo_user_password: str = "demo123456"
    auth_secret: str = "change-me-in-production"
    auth_token_ttl_seconds: int = 7 * 24 * 60 * 60
    allow_demo_without_auth: bool = True
    propainter_command: str = ""
    enhance_command: str = ""
    translate_command: str = ""
    e2fgvi_command: str = ""
    model_plaza_gpu_api_url: str = ""
    model_plaza_gpu_api_key: str = ""
    gptproto_api_key: str = ""
    gptproto_query_token: str = ""
    gptproto_base_url: str = ""
    gptproto_poll_interval_seconds: int = 10
    gptproto_timeout_seconds: int = 1800
    seedreambest_gptproto_api_key: str = ""
    seedreambest_gptproto_query_token: str = ""
    seedreambest_gptproto_base_url: str = ""
    storage_backend: str = "local"
    volcengine_openapi_ak: str = ""
    volcengine_openapi_sk: str = ""
    volcengine_tos_endpoint: str = "tos-cn-guangzhou.volces.com"
    volcengine_tos_region: str = "cn-guangzhou"
    volcengine_tos_bucket: str = ""
    volcengine_tos_public_base_url: str = ""
    volcengine_tos_ak: str = ""
    volcengine_tos_sk: str = ""
    volcengine_tos_presign_expires_seconds: int = 6 * 60 * 60
    remote_storage_ready_timeout_seconds: int = 120
    remote_storage_ready_poll_seconds: int = 3
    asset_retention_hours: int = 14 * 24
    asset_input_cleanup_grace_hours: int = 14 * 24
    cleanup_retention_hours: int = 48
    internal_batch_zip_retention_hours: int = 12
    internal_batch_zip_part_max_bytes: int = 10 * 1024 * 1024 * 1024
    internal_batch_zip_part_max_files: int = 200
    internal_batch_zip_gpu_enabled: bool = False
    internal_batch_zip_gpu_timeout_seconds: int = 7200
    internal_batch_zip_auto_prepare_enabled: bool = False
    internal_batch_zip_retry_max: int = 5
    internal_batch_zip_retry_interval_seconds: int = 60
    result_finalize_retry_max: int = 6
    result_finalize_retry_interval_seconds: int = 60
    result_finalize_job_timeout_seconds: int = 900
    result_finalize_poll_timeout_seconds: int = 30
    result_finalize_defer_seconds: int = 60
    remote_gpu_job_not_found_retry_max: int = 3
    remote_gpu_result_download_timeout_seconds: int = 300
    volcengine_tos_upload_timeout_seconds: int = 600
    volcengine_tos_upload_retry_max: int = 2
    volcengine_tos_upload_retry_interval_seconds: int = 10
    cleanup_disk_high_watermark_percent: int = 80
    cleanup_disk_low_watermark_percent: int = 75
    cleanup_disk_min_age_hours: int = 6
    cleanup_interval_seconds: int = 3600
    gpu_unavailable_retry_max: int = 3
    gpu_unavailable_retry_delay_seconds: int = 60
    gpu_queue_full_retry_delay_seconds: int = 300
    gpu_queue_full_retry_max: int = 0
    gpu_queue_dispatch_interval_seconds: int = 30
    gpu_queue_dispatch_batch_size: int = 8
    gpu_queue_dispatch_cooldown_seconds: int = 180
    gpu_remote_inflight_limit: int = 16

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    @property
    def upload_path(self) -> Path:
        return Path(self.upload_dir).resolve()


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
