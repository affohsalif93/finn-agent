"""Mail data contracts (ARCHITECTURE §2.3, §6). No logic.

Message IDs are stored **without** angle brackets (`abc@mail.gmail.com`); the brackets are added
only when a header is written. Paths are strings so the models serialise cleanly into Temporal
payloads (paths, never file contents, §7.2).
"""

import datetime as dt
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from finn.errors import Finding

AttachmentKind = Literal["xlsx", "xlsm", "xls", "pdf", "zip", "tnef", "other"]
Intent = Literal["covenant_check", "follow_up_input", "question_about_result", "other"]
ReplyKind = Literal["result", "ask", "block", "other", "error"]


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class InboundMessage(_Model):
    """Normalised, provider-independent view of one received mail (headers only)."""

    message_id: str
    in_reply_to: str | None = None
    references: list[str] = Field(default_factory=list)
    from_addr: str  # lower-case address of the verified `From`
    from_name: str | None = None
    to_addrs: list[str] = Field(default_factory=list)  # To + Cc + Delivered-To, lower-case
    reply_to: str | None = None  # logged only; replies always go to `from_addr` (§6.1)
    subject: str = ""
    date: dt.datetime | None = None
    auth_results: str | None = None  # raw Authentication-Results header(s)
    auto_submitted: list[str] = Field(default_factory=list)  # loop indicators, "Header: value"
    mailbox: str = ""  # receiving address (→ tenant)
    size: int = 0  # bytes as reported by the server
    too_large: bool = False  # body not downloaded (MAIL_TOO_LARGE)
    raw_eml_path: str | None = None  # set by the poller after the gate accepted the mail
    attachment_paths: list[str] = Field(default_factory=list)
    provider_ref: str | None = None  # IMAP "<uidvalidity>:<uid>" (for mark_processed)
    # Raw bytes while the mail is in the poller's memory; never serialised (Temporal, JSON).
    raw: bytes | None = Field(default=None, exclude=True, repr=False)


class Attachment(_Model):
    path: str
    filename: str
    content_type: str
    size: int
    sha256: str
    kind: AttachmentKind
    too_large: bool = False


class ThreadMessage(_Model):
    """One mail of the thread as the intent step sees it (pseudonymised body)."""

    message_id: str
    date: dt.datetime | None = None
    subject: str
    body_text: str  # quoted history and signature stripped, pseudonymised (§8.1)
    attachments: list[str] = Field(default_factory=list)  # "Wirtschaftsplan_2027.xlsx (xlsx)"


class ParsedMessage(_Model):
    inbound: InboundMessage
    body_text: str  # pseudonymised
    thread: list[ThreadMessage]  # this mail; the workflow concatenates the thread
    attachments: list[Attachment]
    links: list[str] = Field(default_factory=list)  # file-sharing links (ATTACHMENT_LINK_ONLY)
    unparseable: bool = False  # MAIL_UNPARSEABLE: raw mail kept, polite reply
    run_dir: str


class Request(_Model):
    """Intent schema (§6.2): the LLM (or the rule fallback) classifies, code decides."""

    intent: Intent
    plan_year: int | None = None
    contract_filter: list[str] | None = None  # contract ids, e.g. ["V3"]; None = all
    confidence: float = Field(default=1.0, ge=0, le=1)
    assumptions: list[str] = Field(default_factory=list)
    unsupported: list[str] = Field(default_factory=list)
    source: Literal["llm", "rules"] = "llm"


class InputCheck(_Model):
    """Result of the intent's input contract (FAILURES "Intent input contracts")."""

    status: Literal["ok", "ask"]
    plan: Attachment | None = None
    findings: list[Finding] = Field(default_factory=list)  # ASK + INFO (intent notes)


class Classified(_Model):
    request: Request
    input: InputCheck


class ReplyDraft(_Model):
    kind: ReplyKind
    to: str
    to_name: str | None = None
    subject: str
    in_reply_to: str | None
    references: list[str]
    message_id: str  # deterministic (workflow / run id + seq + kind)
    html: str
    text: str
    placeholders: dict[str, str] = Field(default_factory=dict)
    used_template: bool = True
    notes: list[str] = Field(default_factory=list)  # REPLY_* findings, for the run log
    run_id: str


class SentRecord(_Model):
    message_id: str
    sent_at: dt.datetime
    skipped_already_sent: bool = False
    dry_run: bool = False
