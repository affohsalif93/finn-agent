"""Live failure-case tests against the local test mail server (compose profile `testmail`).

    uv run pytest -m mailserver                         in-process Finn, LLM stand-in
    uv run pytest -m mailserver --real-llm              in-process Finn, OpenAI (key from .env)
    uv run pytest -m mailserver --mail-driver compose   the finn-*-testmail containers
    uv run pytest -m mailserver --case happy --case pdf

Every case of `cases.py` is sent via SMTP from the test sender to Finn; the real poller
(`poll_once`, gate, IMAP connector) and the real worker (CovenantCheckWorkflow + activities, SMTP
send + Sent APPEND) process it; Finn's replies are fetched via IMAP from the sender's mailbox and
compared with the expectation, including "exactly this many replies" after a settle time.

in-process (default): worker + poller loop run in this process on the Temporal test server
(`temporal_env`, own SQLite, nothing shared with the compose Temporal), Finn's mailbox is
`finn-pytest@finn.test`, so the compose test containers (finn@finn.test) never see these mails;
the code under test is the working tree. GreenMail is started if it is not running.
compose: `docker compose --profile testmail up -d --build --no-deps …` (rebuilds finn:testmail
from the working tree), Finn = the containers on queue `finn-testmail`.

Writes the gallery `out/replies/index.html`.
"""

import os
import shutil
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import gallery
import pytest
import pytest_asyncio
from cases import CASES, FINN, FINN_PYTEST
from driver import (
    COMPOSE_SERVICES,
    ROOT,
    CaseResult,
    Env,
    compose_gate_codes,
    compose_up,
    inprocess_finn,
    inprocess_settings,
    port_open,
    run_cases,
)
from temporalio.client import Client
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.testing import WorkflowEnvironment

from finn.config import get_settings

pytestmark = [pytest.mark.mailserver, pytest.mark.asyncio(loop_scope="session")]


def _selected(config: pytest.Config) -> list[str]:
    only: list[str] = config.getoption("--case")
    unknown = [n for n in only if n not in {c.name for c in CASES}]
    if unknown:
        raise pytest.UsageError(f"unknown case(s): {unknown}")
    return only or [c.name for c in CASES]


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def live_results(
    request: pytest.FixtureRequest,
    plan_fixtures: dict[str, Path],
    temporal_env: WorkflowEnvironment,
) -> AsyncIterator[dict[str, CaseResult]]:
    config = request.config
    real_llm: bool = config.getoption("--real-llm")
    driver: str = config.getoption("--mail-driver")
    cases = [c for c in CASES if c.name in _selected(config)]
    key = None
    if real_llm:
        get_settings.cache_clear()
        secret = get_settings().openai_api_key
        if secret is None:
            pytest.fail("--real-llm needs OPENAI_API_KEY (environment or .env)")
        key = secret.get_secret_value()
    timeout = 400 if real_llm else 120
    mode = f"{driver}, LLM: {'OpenAI' if real_llm else 'Stand-in (FINN_LLM_FAKE)'}"

    if driver == "compose":
        since = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        compose_up(COMPOSE_SERVICES, real_llm_key=key)
        env = Env(
            finn=FINN,
            runs_dir=ROOT / "runs-testmail",
            plans=plan_fixtures,
            gate_codes=compose_gate_codes(since),
            timeout=timeout,
            settle=6,
            poll=1,
        )
        results = await run_cases(cases, env)
    else:
        if not port_open(3143):
            compose_up(("greenmail",))
        runs_dir = gallery.OUT / "runs"
        shutil.rmtree(runs_dir, ignore_errors=True)
        saved = os.environ.get("FINN_LLM_FAKE")
        if real_llm:
            os.environ.pop("FINN_LLM_FAKE", None)
        else:  # the guarded stand-in in llm/client.py (TEST ONLY)
            os.environ["FINN_LLM_FAKE"] = "1"
        get_settings.cache_clear()
        settings = inprocess_settings(FINN_PYTEST, runs_dir, f"finn-live-{uuid.uuid4().hex[:8]}")
        cfg = temporal_env.client.config()
        cfg["data_converter"] = pydantic_data_converter
        client = Client(**cfg)
        try:
            async with inprocess_finn(client, settings) as events:
                env = Env(
                    finn=FINN_PYTEST,
                    runs_dir=runs_dir,
                    plans=plan_fixtures,
                    gate_codes=lambda mid: events.get(mid, set()),
                    timeout=timeout,
                )
                results = await run_cases(cases, env)
        finally:
            if saved is None:
                os.environ.pop("FINN_LLM_FAKE", None)
            else:
                os.environ["FINN_LLM_FAKE"] = saved
            get_settings.cache_clear()
    path = gallery.write(results, mode=mode)
    print(f"\nGalerie: {path}")
    yield {r.case.name: r for r in results}


@pytest.mark.parametrize("name", [c.name for c in CASES])
async def test_mail_case_gets_expected_replies(
    name: str, live_results: dict[str, CaseResult]
) -> None:
    if name not in live_results:
        pytest.skip("not selected (--case)")
    result = live_results[name]
    assert result.passed, "\n".join(result.problems)
