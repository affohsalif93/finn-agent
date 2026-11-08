"""Shared fixtures."""

import importlib.util
from collections.abc import AsyncIterator
from pathlib import Path
from types import ModuleType

import pytest
import pytest_asyncio
from temporalio.testing import WorkflowEnvironment

from finn.contracts.store import load_profiles
from finn.models.contract import ContractProfile
from finn.plan import PlanReadResult, read_plan

ROOT = Path(__file__).resolve().parents[1]
MATERIAL = ROOT / "material"
SAMPLE = MATERIAL / "Wirtschaftsplan_2027.xlsx"
GOLD_PROFILES = ROOT / "tests" / "fixtures" / "profiles_gold"


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def temporal_env() -> AsyncIterator[WorkflowEnvironment]:
    """One local Temporal dev server (in-memory SQLite) for the whole test session.

    The first run downloads the Temporal CLI into the temp dir and reuses it afterwards.
    """
    async with await WorkflowEnvironment.start_local() as env:
        yield env


def _load_script() -> ModuleType:
    path = ROOT / "scripts" / "make_plan_fixtures.py"
    spec = importlib.util.spec_from_file_location("make_plan_fixtures", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="session")
def plan_fixtures(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    """Plan variants, generated once per session from the sample plan into a temp dir.

    Nothing binary is committed; `scripts/make_plan_fixtures.py` documents every variant in code.
    """
    built: dict[str, Path] = _load_script().build_all(tmp_path_factory.mktemp("plans"), SAMPLE)
    return built


@pytest.fixture(scope="session")
def sample_result() -> PlanReadResult:
    return read_plan(SAMPLE)


@pytest.fixture(scope="session")
def gold_profiles() -> list[ContractProfile]:
    profiles, findings = load_profiles(GOLD_PROFILES, MATERIAL)
    assert findings == [], findings
    return profiles


def pytest_addoption(parser: pytest.Parser) -> None:
    """Options of the live mail tests (`tests/live`, marker `mailserver`)."""
    group = parser.getgroup("finn-live", "Finn live mail tests (pytest -m mailserver)")
    group.addoption(
        "--real-llm",
        action="store_true",
        help="use OpenAI (OPENAI_API_KEY from .env) instead of the deterministic stand-in",
    )
    group.addoption(
        "--mail-driver",
        choices=("inprocess", "compose"),
        default="inprocess",
        help="inprocess: worker + poller in pytest (Temporal test server); "
        "compose: the finn-*-testmail containers",
    )
    group.addoption("--case", action="append", default=[], help="run only these cases (repeatable)")
