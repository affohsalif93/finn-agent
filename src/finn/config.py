"""Runtime settings, read from environment variables and an optional `.env` file."""

from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Only the settings needed so far; the full key list lives in `.env.example`."""

    # A missing .env file is fine: pydantic-settings simply skips it.
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    temporal_address: str = "localhost:7233"
    temporal_task_queue: str = "finn"
    poll_interval_seconds: float = 15
    log_format: Literal["console", "json"] = "console"


@lru_cache
def get_settings() -> Settings:
    return Settings()
