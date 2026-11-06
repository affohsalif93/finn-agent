"""`finn plan read <path> [--json]`."""

import json
from pathlib import Path

from typer.testing import CliRunner

from finn.cli import app

SAMPLE = Path(__file__).resolve().parents[3] / "material" / "Wirtschaftsplan_2027.xlsx"


def test_plan_read_text() -> None:
    result = CliRunner().invoke(app, ["plan", "read", str(SAMPLE)])
    assert result.exit_code == 0, result.output
    assert "Stadtwerke Musterstadt GmbH" in result.output
    assert "148.200" in result.output
    assert "Bilanz!D28" in result.output
    assert "PLAN_STATUS" in result.output


def test_plan_read_json() -> None:
    result = CliRunner().invoke(app, ["plan", "read", str(SAMPLE), "--json"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["plan"]["values"]["equity"]["value"] == "96100"
    assert data["findings"][0]["code"] == "PLAN_STATUS"


def test_plan_read_missing_file(tmp_path: Path) -> None:
    result = CliRunner().invoke(app, ["plan", "read", str(tmp_path / "nope.xlsx")])
    assert result.exit_code != 0
