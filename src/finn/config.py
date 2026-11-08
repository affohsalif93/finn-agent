"""Runtime settings, read from environment variables and an optional `.env` file."""

from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Default OpenAI model: balanced GPT-6.1 tier, structured outputs, 1M context.
DEFAULT_OPENAI_MODEL = "gpt-6.1-sol"


class Settings(BaseSettings):
    """All of Finn's settings; `.env.example` shows the common keys."""

    # A missing .env file is fine: pydantic-settings simply skips it.
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    temporal_address: str = "localhost:7233"
    temporal_task_queue: str = "finn"
    poll_interval_seconds: float = 15
    log_format: Literal["console", "json"] = "console"
    contracts_dir: Path = Path("material")
    profiles_dir: Path = Path("data/contract_profiles")
    # Headroom below this share of the threshold is flagged as "tight".
    tight_headroom_ratio: Decimal = Decimal("0.10")
    # LLM. An empty `OPENAI_API_KEY=` means "no key"; an empty
    # `OPENAI_MODEL=` falls back to the default.
    openai_api_key: SecretStr | None = None
    openai_model: str = DEFAULT_OPENAI_MODEL
    openai_timeout_seconds: float = 300
    openai_max_retries: int = 3
    # Mailbox. Finn reads ONLY `imap_folder`; processed mail is moved to
    # `imap_processed_folder` (default "<imap_folder>/Verarbeitet", a nested Gmail label).
    imap_host: str = "imap.gmail.com"
    imap_port: int = 993
    # TLS on connect (IMAPS). `false` = plain IMAP, only for a local test server (GreenMail).
    imap_ssl: bool = True
    imap_user: str = ""
    imap_password: SecretStr | None = None
    imap_folder: str = "Finn"
    imap_processed_folder: str = ""
    # Sent folder: empty = the folder with the `\Sent` SPECIAL-USE flag, else a common name
    # ("Sent", "Gesendet", …). Servers without SPECIAL-USE (GreenMail) need no setting.
    imap_sent_folder: str = ""
    # Copy each sent reply into the Sent folder via IMAP APPEND (idempotency check `was_sent`).
    # auto = only if the server is not Gmail (Gmail's SMTP files sent mail itself).
    imap_append_sent: Literal["auto", "true", "false"] = "auto"
    smtp_host: str = "smtp.gmail.com"
    smtp_port: int = 465
    # auto = implicit TLS on port 465, else STARTTLS; `plain` only for a local test server.
    smtp_security: Literal["auto", "ssl", "starttls", "plain"] = "auto"
    smtp_user: str = ""
    smtp_password: SecretStr | None = None
    smtp_from_name: str = "Finn (Covenant-Prüfung)"
    # Gate (D12): Finn's receiving address (may be a plus alias of `imap_user`), sender rules.
    finn_address: str = ""
    allowed_senders: str = "*"
    require_dmarc_pass: bool = False
    runs_dir: Path = Path("runs")
    lookback_days: int = 3
    max_mail_mb: float = 25
    # Waiting for a follow-up mail (thread case B, FAILURES §J).
    wait_for_input_days: float = 7
    # Reply: let the LLM rephrase summary + notes (placeholders only, D21); off → template.
    reply_llm_polish: bool = True
    # TEST ONLY (live mail tests / `finn demo`): a deterministic stand-in replaces every LLM
    # call (`llm/fake.py`). Never set in production; the worker logs a warning when it is on.
    finn_llm_fake: bool = False

    @field_validator("imap_password", "smtp_password", mode="before")
    @classmethod
    def _empty_password_is_none(cls, value: object) -> object:
        if value is None or (isinstance(value, str) and not value.strip()):
            return None
        return value

    @property
    def processed_folder(self) -> str:
        return self.imap_processed_folder or f"{self.imap_folder}/Verarbeitet"

    @field_validator("openai_api_key", mode="before")
    @classmethod
    def _empty_key_is_none(cls, value: object) -> object:
        if value is None or (isinstance(value, str) and not value.strip()):
            return None
        return value

    @field_validator("openai_model", mode="before")
    @classmethod
    def _empty_model_is_default(cls, value: object) -> object:
        if value is None or (isinstance(value, str) and not value.strip()):
            return DEFAULT_OPENAI_MODEL
        return value.strip() if isinstance(value, str) else value


@lru_cache
def get_settings() -> Settings:
    return Settings()
