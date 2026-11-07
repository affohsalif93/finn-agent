"""CovenantCheckWorkflow + poller against the local Temporal dev server (`temporal_env`).

Fake mailbox, LLM mocked (unavailable → rule fallback + template reply).
"""

import asyncio
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest
from temporalio.client import Client, WorkflowHandle
from temporalio.common import WorkflowIDReusePolicy
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.exceptions import WorkflowAlreadyStartedError
from temporalio.service import RPCError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from finn.config import Settings
from finn.llm import client as llm_client
from finn.mail.connector import FolderMissingError
from finn.mail.parse import inbound_from_bytes
from finn.models.run import RunOutcome, StartRun
from finn.poller import _backoff, poll_once
from finn.reply.render import reply_message_id
from finn.workflows.activities import FinnActivities
from finn.workflows.covenant_check import CovenantCheckWorkflow
from mailkit import (
    ACCOUNT,
    FakeConnector,
    build_mail,
    fixture_mails,
    llm_unavailable,
    plan_attachment,
    settings,
)

pytestmark = pytest.mark.asyncio(loop_scope="session")

REQUEST_ID = "req-0001@stadtwerke-musterstadt.de"


@pytest.fixture(autouse=True)
def _no_llm(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(llm_client, "complete_structured", llm_unavailable)


@asynccontextmanager
async def finn_worker(
    env: WorkflowEnvironment, tmp_path: Path, fake: FakeConnector, **overrides: Any
) -> AsyncIterator[tuple[Client, Settings]]:
    config = env.client.config()
    config["data_converter"] = pydantic_data_converter
    client = Client(**config)
    s = settings(tmp_path, temporal_task_queue=f"test-{uuid.uuid4()}", **overrides)
    acts = FinnActivities(s, lambda: fake)
    with ThreadPoolExecutor(max_workers=8) as executor:
        async with Worker(
            client,
            task_queue=s.temporal_task_queue,
            workflows=[CovenantCheckWorkflow],
            activities=acts.all(),
            activity_executor=executor,
        ):
            yield client, s


def _handle(client: Client, wid: str) -> WorkflowHandle[CovenantCheckWorkflow, RunOutcome]:
    return client.get_workflow_handle_for(CovenantCheckWorkflow.run, wid)


async def _until(pred: Callable[[], Awaitable[bool]], timeout: float = 60) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not await pred():
        if asyncio.get_running_loop().time() > deadline:
            raise TimeoutError("condition not reached")
        await asyncio.sleep(0.2)


def _unique(raw: bytes, mid: str) -> bytes:
    """Each test needs its own Message-ID = workflow ID (the dev server is shared)."""
    return raw.replace(REQUEST_ID.encode(), mid.encode())


async def test_happy_path_via_poller(temporal_env: WorkflowEnvironment, tmp_path: Path) -> None:
    mid = f"happy-{uuid.uuid4()}@stadtwerke-musterstadt.de"
    fake = FakeConnector([_unique(fixture_mails()["request"], mid)])
    async with finn_worker(temporal_env, tmp_path, fake) as (client, s):
        stats = await poll_once(client, fake, s)
        assert (stats.fetched, stats.started) == (1, 1)
        assert fake.processed == ["1:1"]
        outcome = await _handle(client, mid).result()
    assert outcome.status == "replied"
    assert outcome.replies == ["result"]
    [sent] = fake.sent
    assert sent.kind == "result"
    assert "3,37" in sent.text
    assert sent.in_reply_to == mid
    assert sent.message_id == reply_message_id(mid, 1, "result", ACCOUNT)
    run_dir = next((tmp_path / "runs").iterdir())
    assert (run_dir / "results.json").is_file()


async def test_missing_attachment_ask_then_follow_up(
    temporal_env: WorkflowEnvironment, tmp_path: Path
) -> None:
    mid = f"ask-{uuid.uuid4()}@stadtwerke-musterstadt.de"
    first = build_mail(message_id=mid)  # no attachment
    fake = FakeConnector([first])
    async with finn_worker(temporal_env, tmp_path, fake) as (client, s):
        assert (await poll_once(client, fake, s)).started == 1
        handle = _handle(client, mid)

        async def waiting() -> bool:
            return (await handle.query(CovenantCheckWorkflow.state)).waiting_for_input

        await _until(waiting)
        [ask] = fake.sent
        assert ask.kind == "ask"
        assert "Bitte den Wirtschaftsplan als Excel-Datei (.xlsx) anhängen." in ask.text

        follow_id = f"follow-{uuid.uuid4()}@stadtwerke-musterstadt.de"
        fake.deliver(
            build_mail(
                subject=ask.subject,
                body="Anbei der Plan.",
                message_id=follow_id,
                in_reply_to=ask.message_id,
                references=[mid, ask.message_id],
                attachments=[plan_attachment()],
            )
        )
        stats = await poll_once(client, fake, s)
        assert (stats.signalled, stats.started) == (1, 0)
        outcome = await handle.result()
    assert outcome.replies == ["ask", "result"]
    _ask_mail, result_mail = fake.sent
    assert result_mail.kind == "result"
    assert result_mail.in_reply_to == follow_id  # reply to the latest mail in the thread
    assert result_mail.references[0] == mid
    assert "3,37" in result_mail.text
    run_dir = next((tmp_path / "runs").iterdir())
    followups = list(run_dir.glob("followup_*/incoming.eml"))
    assert len(followups) == 1
    assert fake.processed == ["1:1", "1:2"]


async def test_duplicate_start_is_ignored(
    temporal_env: WorkflowEnvironment, tmp_path: Path
) -> None:
    mid = f"dup-{uuid.uuid4()}@stadtwerke-musterstadt.de"
    fake = FakeConnector([_unique(fixture_mails()["request"], mid)])
    async with finn_worker(temporal_env, tmp_path, fake) as (client, s):
        assert (await poll_once(client, fake, s)).started == 1
        fake.processed.clear()  # crash before mark_processed: the mail is fetched again
        stats = await poll_once(client, fake, s)
        assert (stats.duplicate, stats.started) == (1, 0)
        await _handle(client, mid).result()
        start = StartRun(
            inbound=inbound_from_bytes(fake.folder["1"], mailbox=s.finn_address, keep_raw=False),
            run_dir=str(tmp_path),
        )
        with pytest.raises(WorkflowAlreadyStartedError):
            await client.start_workflow(
                CovenantCheckWorkflow.run,
                start,
                id=mid,
                task_queue=s.temporal_task_queue,
                id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
            )
    assert len(fake.sent) == 1


async def test_send_retried_once_and_sent_exactly_once(
    temporal_env: WorkflowEnvironment, tmp_path: Path
) -> None:
    mid = f"retry-{uuid.uuid4()}@stadtwerke-musterstadt.de"
    fake = FakeConnector([_unique(fixture_mails()["request"], mid)])
    fake.fail_sends = 1  # SMTP down on the first attempt
    async with finn_worker(temporal_env, tmp_path, fake) as (client, s):
        await poll_once(client, fake, s)
        outcome = await _handle(client, mid).result()
    assert fake.send_attempts == 2
    assert len(fake.sent) == 1
    assert outcome.sent[0].skipped_already_sent is False


async def test_already_in_sent_folder_is_not_sent_again(
    temporal_env: WorkflowEnvironment, tmp_path: Path
) -> None:
    """Crash after SMTP before Temporal recorded it: `was_sent` finds the Message-ID."""
    mid = f"sent-{uuid.uuid4()}@stadtwerke-musterstadt.de"
    fake = FakeConnector([_unique(fixture_mails()["request"], mid)])
    fake.sent_folder.add(reply_message_id(mid, 1, "result", ACCOUNT))
    async with finn_worker(temporal_env, tmp_path, fake) as (client, s):
        await poll_once(client, fake, s)
        outcome = await _handle(client, mid).result()
    assert fake.sent == []
    assert outcome.sent[0].skipped_already_sent


async def test_rejected_sender_creates_no_workflow(
    temporal_env: WorkflowEnvironment, tmp_path: Path
) -> None:
    raw = fixture_mails()["unknown_sender"]
    fake = FakeConnector([raw])
    async with finn_worker(
        temporal_env, tmp_path, fake, allowed_senders="@stadtwerke-musterstadt.de"
    ) as (client, s):
        stats = await poll_once(client, fake, s)
        assert (stats.rejected, stats.started) == (1, 0)
        assert fake.processed == ["1:1"]
        with pytest.raises(RPCError):
            await _handle(client, "req-0005@unbekannt.example").describe()
    assert fake.sent == []
    assert not (tmp_path / "runs").exists()


async def test_wait_timeout_ends_the_run(temporal_env: WorkflowEnvironment, tmp_path: Path) -> None:
    mid = f"timeout-{uuid.uuid4()}@stadtwerke-musterstadt.de"
    fake = FakeConnector([build_mail(message_id=mid)])
    async with finn_worker(temporal_env, tmp_path, fake, wait_for_input_days=2 / 86400) as (
        client,
        s,
    ):
        await poll_once(client, fake, s)
        outcome = await _handle(client, mid).result()
    assert outcome.status == "wait_timeout"
    assert outcome.replies == ["ask"]


async def test_folder_missing_propagates_to_the_loop(tmp_path: Path) -> None:
    class NoFolder(FakeConnector):
        def fetch_new(self, since: Any) -> Any:
            raise FolderMissingError("Finn")

    with pytest.raises(FolderMissingError):
        await poll_once(None, NoFolder(), settings(tmp_path))  # type: ignore[arg-type]
    s = settings(tmp_path, poll_interval_seconds=15)
    assert [_backoff(s, n) for n in (1, 2, 3, 6)] == [30, 60, 120, 300]
