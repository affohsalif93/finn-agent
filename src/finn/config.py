"""Runtime settings, read from environment variables and an optional `.env` file."""

from decimal import Decimal
from functools import lru_cache
from pathlib import Path
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
    contracts_dir: Path = Path("material")
    profiles_dir: Path = Path("data/contract_profiles")
    # Headroom below this share of the threshold is flagged as "tight" (ARCHITECTURE §5.1).
    tight_headroom_ratio: Decimal = Decimal("0.10")


@lru_cache
def get_settings() -> Settings:
    return Settings()
