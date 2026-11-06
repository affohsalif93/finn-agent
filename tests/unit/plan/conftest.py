"""Plan fixtures: generated once per session from the sample plan into a temp dir.

Nothing binary is committed; `scripts/make_plan_fixtures.py` documents every variant in code.
"""

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest

from finn.plan import PlanReadResult, read_plan

ROOT = Path(__file__).resolve().parents[3]
SAMPLE = ROOT / "material" / "Wirtschaftsplan_2027.xlsx"


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
    built: dict[str, Path] = _load_script().build_all(tmp_path_factory.mktemp("plans"), SAMPLE)
    return built


@pytest.fixture(scope="session")
def sample_result() -> PlanReadResult:
    return read_plan(SAMPLE)
