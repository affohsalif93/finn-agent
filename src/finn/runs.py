"""Run folder `runs/<ts>_<msgid-hash>/`: the audit trail of one mail (D13); git-ignored."""

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from finn.models.mail import SentRecord


def msgid_hash(message_id: str, n: int = 12) -> str:
    return hashlib.sha256(message_id.strip("<>").encode()).hexdigest()[:n]


def run_dir_for(runs_dir: Path, message_id: str, received: datetime | None = None) -> Path:
    """The run folder of a mail; an existing folder for the same Message-ID is reused, so a
    poller restart does not create a second one."""
    suffix = msgid_hash(message_id)
    if runs_dir.is_dir():
        existing = sorted(runs_dir.glob(f"*_{suffix}"))
        if existing:
            return existing[0]
    ts = (received or datetime.now(UTC)).astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    return runs_dir / f"{ts}_{suffix}"


def run_id(run_dir: Path | str) -> str:
    return Path(run_dir).name


def save_incoming(run_dir: Path, raw: bytes) -> Path:
    run_dir.mkdir(parents=True, exist_ok=True)
    path = run_dir / "incoming.eml"
    if not path.exists():
        path.write_bytes(raw)
    return path


def write_json(
    run_dir: Path | str, name: str, data: BaseModel | dict[str, Any] | list[Any]
) -> Path:
    path = Path(run_dir) / name
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(data, BaseModel):
        text = data.model_dump_json(indent=2)
    else:
        text = json.dumps(data, ensure_ascii=False, indent=2, default=str)
    path.write_text(text, encoding="utf-8")
    return path


def read_json(run_dir: Path | str, name: str) -> Any:
    path = Path(run_dir) / name
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None


def append_jsonl(run_dir: Path | str, name: str, record: dict[str, Any]) -> None:
    path = Path(run_dir) / name
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")


def log_line(run_dir: Path | str, step: str, text: str) -> None:
    """Readable step log (`log.txt`)."""
    path = Path(run_dir) / "log.txt"
    path.parent.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    with path.open("a", encoding="utf-8") as f:
        f.write(f"{stamp} [{step}] {text}\n")


def write_error(run_dir: Path | str, text: str) -> None:
    path = Path(run_dir) / "error.txt"
    path.parent.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    with path.open("a", encoding="utf-8") as f:
        f.write(f"--- {stamp}\n{text}\n")


def write_reply(run_dir: Path | str, seq: int, kind: str, html: str, text: str) -> None:
    """`reply-<seq>-<kind>.html|txt` per reply, `reply.html|txt` = the latest."""
    base = Path(run_dir)
    for name in (f"reply-{seq}-{kind}", "reply"):
        (base / f"{name}.html").write_text(html, encoding="utf-8")
        (base / f"{name}.txt").write_text(text, encoding="utf-8")


def sent_marker(run_dir: Path | str, message_id: str) -> Path:
    return Path(run_dir) / "sent" / f"{msgid_hash(message_id, 24)}.json"


def write_sent_marker(run_dir: Path | str, record: SentRecord) -> Path:
    marker = sent_marker(run_dir, record.message_id)
    return write_json(marker.parent, marker.name, record)
