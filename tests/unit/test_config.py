"""Settings: defaults, environment overrides, `.env` file, validation."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from finn.config import Settings

ENV_KEYS = ("TEMPORAL_ADDRESS", "TEMPORAL_TASK_QUEUE", "POLL_INTERVAL_SECONDS", "LOG_FORMAT")


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Empty cwd (no `.env`) and none of our variables set on the host."""
    monkeypatch.chdir(tmp_path)
    for key in ENV_KEYS:
        monkeypatch.delenv(key, raising=False)


def test_defaults_without_env_file() -> None:
    settings = Settings()

    assert settings.temporal_address == "localhost:7233"
    assert settings.temporal_task_queue == "finn"
    assert settings.poll_interval_seconds == 15
    assert settings.log_format == "console"


def test_env_vars_override_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEMPORAL_ADDRESS", "temporal:7233")
    monkeypatch.setenv("POLL_INTERVAL_SECONDS", "2.5")
    monkeypatch.setenv("LOG_FORMAT", "json")

    settings = Settings()

    assert settings.temporal_address == "temporal:7233"
    assert settings.poll_interval_seconds == 2.5
    assert settings.log_format == "json"


def test_env_file_is_read_and_env_var_wins(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    (tmp_path / ".env").write_text(
        "TEMPORAL_TASK_QUEUE=from-file\nTEMPORAL_ADDRESS=file:7233\n", encoding="utf-8"
    )
    monkeypatch.setenv("TEMPORAL_ADDRESS", "env:7233")

    settings = Settings()

    assert settings.temporal_task_queue == "from-file"
    assert settings.temporal_address == "env:7233"


@pytest.mark.parametrize(
    ("key", "value"),
    [("POLL_INTERVAL_SECONDS", "soon"), ("LOG_FORMAT", "xml")],
)
def test_invalid_value_raises(monkeypatch: pytest.MonkeyPatch, key: str, value: str) -> None:
    monkeypatch.setenv(key, value)

    with pytest.raises(ValidationError, match=key.lower()):
        Settings()
