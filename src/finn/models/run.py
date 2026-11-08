"""Inputs / outputs of the pipeline steps = Temporal activity payloads (ARCHITECTURE §2.3, §7).

Paths, never file contents (§7.2). Decimals serialise as strings (pydantic data converter).
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from finn.errors import Finding
from finn.models.mail import (
    Attachment,
    InboundMessage,
    ParsedMessage,
    ReplyDraft,
    ReplyKind,
    Request,
    SentRecord,
)
from finn.models.plan import Plan
from finn.models.result import CheckedInput, Report


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class PlanReadResult(BaseModel):
    """What the plan reader returns: the plan (None if unreadable) and its findings."""

    model_config = ConfigDict(frozen=True)

    plan: Plan | None
    findings: list[Finding]
    mapping_log: list[str]


class StartRun(_Model):
    """Workflow input from the poller: the gate-approved mail, saved to its run folder."""

    inbound: InboundMessage
    run_dir: str
    wait_for_input_days: float = 7  # thread case B: how long to wait for a follow-up


class WorkflowState(_Model):
    """Query result: the poller signals only a workflow that is waiting for input."""

    waiting_for_input: bool
    run_dir: str
    seen_message_ids: list[str] = Field(default_factory=list)


class ParseInput(_Model):
    inbound: InboundMessage
    run_dir: str  # the workflow's run folder (pseudonyms are shared across the thread)


class ClassifyInput(_Model):
    thread: list[ParsedMessage]
    run_dir: str
    workflow_id: str
    session_id: str


class PlanInput(_Model):
    attachment: Attachment
    request: Request
    run_dir: str


class CalcInput(_Model):
    plan_result: PlanReadResult
    request: Request
    file: str
    run_dir: str


class RenderInput(_Model):
    kind: ReplyKind
    reply_to: InboundMessage  # the latest mail of the thread (verified From)
    run_dir: str
    workflow_id: str
    session_id: str
    seq: int
    report: Report | None = None
    findings: list[Finding] = Field(default_factory=list)
    file: str | None = None
    header: CheckedInput | None = None
    question: bool = False


class SendInput(_Model):
    draft: ReplyDraft
    run_dir: str


class FailureInput(_Model):
    run_dir: str
    error: str


class RunOutcome(_Model):
    status: Literal["replied", "wait_timeout", "error"]
    replies: list[str] = Field(default_factory=list)  # kinds in order, e.g. ["ask", "result"]
    sent: list[SentRecord] = Field(default_factory=list)
    run_id: str
