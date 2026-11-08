"""Poller (D20): a stateless loop outside Temporal. Fetch → gate → save .eml → signal the waiting
workflow or start one (ID = Message-ID) → mark processed only after that, so mail is never lost
or processed twice; outages back off, auth failures are loud."""

import asyncio
import contextlib
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

import structlog
from temporalio.client import Client, WorkflowExecutionStatus, WorkflowHandle
from temporalio.common import WorkflowIDConflictPolicy, WorkflowIDReusePolicy
from temporalio.exceptions import WorkflowAlreadyStartedError
from temporalio.service import RPCError, RPCStatusCode

from finn import runs
from finn.config import Settings
from finn.errors import ErrorCode
from finn.mail import gate
from finn.mail.connector import (
    FolderMissingError,
    MailAuthError,
    MailConnectionError,
    MailConnector,
)
from finn.mail.thread import candidate_ids, thread_id
from finn.models.mail import InboundMessage
from finn.models.run import StartRun
from finn.runtime import connect, install_stop_handlers
from finn.workflows.covenant_check import CovenantCheckWorkflow

log = structlog.get_logger(__name__)

POLL_MAX_BACKOFF_SECONDS = 300.0
AUTH_PAUSE_SECONDS = 900.0
Outcome = Literal["started", "signalled", "duplicate", "rejected"]


@dataclass
class PollStats:
    fetched: int = 0
    started: int = 0
    signalled: int = 0
    duplicate: int = 0
    rejected: int = 0
    failed: int = 0


async def run_poller(settings: Settings) -> None:
    from finn.mail.imap_smtp import ImapSmtpConnector

    stop = install_stop_handlers()
    client = await connect(settings, stop)
    if client is None:
        return
    connector = ImapSmtpConnector(settings)
    log.info(
        "poller started",
        interval_s=settings.poll_interval_seconds,
        folder=settings.imap_folder,
        processed_folder=settings.processed_folder,
    )
    failures = 0
    try:
        while not stop.is_set():
            delay = settings.poll_interval_seconds
            try:
                stats = await poll_once(client, connector, settings)
                if stats.fetched:
                    log.info("poll cycle", **stats.__dict__)
                failures = 0
            except MailAuthError as exc:
                log.critical(
                    "mailbox_auth_failed",
                    code=ErrorCode.INFRA_MAILBOX_AUTH,
                    error=str(exc),
                    action="IMAP_USER / IMAP_PASSWORD (App-Passwort) prüfen",
                    retry_in_s=AUTH_PAUSE_SECONDS,
                )
                delay = AUTH_PAUSE_SECONDS
            except FolderMissingError as exc:
                failures += 1
                delay = _backoff(settings, failures)
                log.error(
                    "imap_folder_missing",
                    code=ErrorCode.INFRA_IMAP_FOLDER_MISSING,
                    folder=str(exc),
                    action="Gmail: Label anlegen und Filter auf FINN_ADDRESS einrichten",
                    retry_in_s=delay,
                )
            except (MailConnectionError, RPCError, OSError) as exc:
                failures += 1
                delay = _backoff(settings, failures)
                log.warning(
                    "poll_failed", code=ErrorCode.INFRA_IMAP_DOWN, error=str(exc), retry_in_s=delay
                )
            except Exception:
                failures += 1
                delay = _backoff(settings, failures)
                log.exception("poll_unexpected", code=ErrorCode.INFRA_UNEXPECTED, retry_in_s=delay)
            with contextlib.suppress(TimeoutError):  # timeout = next cycle, stop = leave the loop
                await asyncio.wait_for(stop.wait(), timeout=delay)
    finally:
        connector.close()
    log.info("poller stopped")


def _backoff(settings: Settings, failures: int) -> float:
    return float(min(settings.poll_interval_seconds * 2**failures, POLL_MAX_BACKOFF_SECONDS))


async def poll_once(
    client: Client,
    connector: MailConnector,
    settings: Settings,
    *,
    now: datetime | None = None,
) -> PollStats:
    """One cycle. Connector calls are blocking → run in a thread (keeps the client responsive).
    A failure on one mail leaves it unmarked (retried next cycle) and does not stop the others;
    connection-level errors propagate to the loop's backoff."""
    since = (now or datetime.now(UTC)) - timedelta(days=settings.lookback_days)
    messages = await asyncio.to_thread(connector.fetch_new, since)
    stats = PollStats(fetched=len(messages))
    for msg in messages:
        try:
            outcome = await handle_message(client, connector, settings, msg)
        except (MailAuthError, MailConnectionError, FolderMissingError):
            raise
        except Exception:
            stats.failed += 1
            log.exception("message_failed", message_id=msg.message_id)
            continue
        setattr(stats, outcome, getattr(stats, outcome) + 1)
    return stats


async def handle_message(
    client: Client, connector: MailConnector, settings: Settings, msg: InboundMessage
) -> Outcome:
    decision = gate.check(msg, settings)
    if isinstance(decision, gate.Reject):
        # Security event: no reply, attachments never parsed, no workflow (D12).
        log.warning(
            decision.event,
            code=decision.code,
            reason=decision.reason,
            sender=msg.from_addr,
            message_id=msg.message_id,
            dmarc=decision.auth.dmarc,
        )
        await asyncio.to_thread(connector.mark_processed, msg)
        return "rejected"
    if msg.raw is None:
        raise ValueError(f"{msg.message_id}: raw bytes missing")

    if await _exists(client, msg.message_id):
        log.info("duplicate", code=ErrorCode.GATE_DUPLICATE, message_id=msg.message_id)
        await asyncio.to_thread(connector.mark_processed, msg)
        return "duplicate"

    outcome: Outcome
    target = await find_waiting_workflow(client, msg)
    if target is not None:
        handle, run_dir = target
        folder = Path(run_dir) / f"followup_{runs.msgid_hash(msg.message_id)}"
        inbound = _saved(msg, folder)
        await handle.signal(CovenantCheckWorkflow.new_message, inbound)
        log.info("signalled", workflow_id=handle.id, message_id=msg.message_id)
        outcome = "signalled"
    else:
        folder = runs.run_dir_for(settings.runs_dir, msg.message_id, msg.date)
        inbound = _saved(msg, folder)
        try:
            await client.start_workflow(
                CovenantCheckWorkflow.run,
                StartRun(
                    inbound=inbound,
                    run_dir=str(folder),
                    wait_for_input_days=settings.wait_for_input_days,
                ),
                id=msg.message_id,
                task_queue=settings.temporal_task_queue,
                id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
                id_conflict_policy=WorkflowIDConflictPolicy.FAIL,
            )
        except WorkflowAlreadyStartedError:
            log.info("duplicate", code=ErrorCode.GATE_DUPLICATE, message_id=msg.message_id)
            outcome = "duplicate"
        else:
            log.info("started", workflow_id=msg.message_id, thread=thread_id(msg), run=folder.name)
            outcome = "started"
    await asyncio.to_thread(connector.mark_processed, msg)  # only after start / signal
    return outcome


def _saved(msg: InboundMessage, folder: Path) -> InboundMessage:
    assert msg.raw is not None
    eml = runs.save_incoming(folder, msg.raw)
    return msg.model_copy(update={"raw": None, "raw_eml_path": str(eml)})


async def _exists(client: Client, workflow_id: str) -> bool:
    try:
        await client.get_workflow_handle(workflow_id).describe()
    except RPCError as exc:
        if exc.status == RPCStatusCode.NOT_FOUND:
            return False
        raise
    return True


async def find_waiting_workflow(
    client: Client, msg: InboundMessage
) -> tuple[WorkflowHandle[CovenantCheckWorkflow, object], str] | None:
    """The running workflow of this thread that waits for input (IDs from In-Reply-To /
    References, nearest first). A thread without one starts a new run (thread case C / late
    follow-up, FAILURES `WAIT_LATE_FOLLOW_UP`)."""
    for mid in candidate_ids(msg):
        handle = client.get_workflow_handle_for(CovenantCheckWorkflow.run, mid)
        try:
            desc = await handle.describe()
        except RPCError as exc:
            if exc.status == RPCStatusCode.NOT_FOUND:
                continue
            raise
        if desc.status != WorkflowExecutionStatus.RUNNING:
            continue
        state = await handle.query(CovenantCheckWorkflow.state)
        if state.waiting_for_input:
            return handle, state.run_dir
    return None
