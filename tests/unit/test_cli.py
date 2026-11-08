"""The `finn check` and `finn contracts check` commands."""

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from finn.cli import app
from mailkit import GOLD_PROFILES, SAMPLE


@pytest.mark.usefixtures("at_root")
def test_cli_check_table() -> None:
    result = CliRunner().invoke(app, ["check", str(SAMPLE), "--profiles", str(GOLD_PROFILES)])
    assert result.exit_code == 0, result.output
    out = result.output
    for row in ("34,32 %", "≥ 27,5 %", "6,82 PP", "1,56", "30,7 %", "0,7 PP", "3,37", "≤ 3,50"):
        assert row in out
    assert out.count("ja (knapp)") >= 2
    assert "(148.200 − 12.600) / 40.200" in out
    assert "Am engsten: Konsortialkredit · Dynamischer Verschuldungsgrad" in out


@pytest.mark.usefixtures("at_root")
def test_cli_check_json() -> None:
    result = CliRunner().invoke(
        app, ["check", str(SAMPLE), "--profiles", str(GOLD_PROFILES), "--json"]
    )
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["overall"] == "all_met"
    assert data["tightest"] == "V4-DVG"
    assert {r["covenant_id"]: r["value_rounded"] for r in data["results"]} == {
        "V2-WEKQ": "34.32", "V3-DSCR": "1.56", "V4-EKQ": "30.7", "V4-DVG": "3.37"
    }  # fmt: skip


def test_cli_check_without_profiles_prints_hint(tmp_path: Path) -> None:
    result = CliRunner().invoke(app, ["check", str(SAMPLE), "--profiles", str(tmp_path)])
    assert result.exit_code == 2
    assert "--profiles tests/fixtures/profiles_gold" in result.output


@pytest.mark.usefixtures("at_root")
def test_cli_contracts_check_green_on_gold() -> None:
    result = CliRunner().invoke(app, ["contracts", "check", "--profiles", str(GOLD_PROFILES)])
    assert result.exit_code == 0, result.output
    assert "Beispiel V3-DSCR: erwartet 1,54, berechnet 1,54" in result.output
    assert "0 Fehler" in result.output


@pytest.mark.usefixtures("at_root")
def test_cli_contracts_check_fails_on_mismatch(tmp_path: Path) -> None:
    data = json.loads((GOLD_PROFILES / "V3.json").read_text(encoding="utf-8"))
    data["examples"][0]["expected"] = "1.60"
    (tmp_path / "V3.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    result = CliRunner().invoke(app, ["contracts", "check", "--profiles", str(tmp_path)])
    assert result.exit_code == 1
    assert "PROFILE_EXAMPLE_MISMATCH" in result.output
