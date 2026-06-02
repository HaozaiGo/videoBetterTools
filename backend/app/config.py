from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    database_url: str = "postgresql+psycopg://jason@127.0.0.1:5432/model_plaza"
    redis_url: str = "redis://127.0.0.1:6379/0"
    upload_dir: str = "../data/uploads"
    public_upload_prefix: str = "/uploads"
    demo_user_id: str = "demo-user"
    demo_user_password: str = "demo123456"
    auth_secret: str = "change-me-in-production"
    auth_token_ttl_seconds: int = 7 * 24 * 60 * 60
    allow_demo_without_auth: bool = True
    propainter_command: str = ""
    e2fgvi_command: str = ""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    @property
    def upload_path(self) -> Path:
        return Path(self.upload_dir).resolve()


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
