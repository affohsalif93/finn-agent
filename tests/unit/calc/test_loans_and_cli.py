"""Contract ↔ plan loan checks and the `finn check` / `finn contracts check` commands."""

import json
from decimal import Decimal
from pathlib import Path

import pytest
from typer.testing import CliRunner

from finn.calc.engine import calculate, match_loans
from finn.cli import app
from finn.errors import ErrorCode
from finn.models.contract import ContractProfile
from finn.models.plan import LoanLine, Plan
from finn.plan import PlanReadResult

ROOT = Path(__file__).resolve().parents[3]
SAMPLE = ROOT / "material" / "Wirtschaftsplan_2027.xlsx"
GOLD = ROOT / "tests" / "fixtures" / "profiles_gold"


def _replace_loans(plan: Plan, loans: list[LoanLine]) -> Plan:
    return plan.model_copy(update={"loans": loans})


def test_sample_loans_match_by_number_then_label(
    sample_result: PlanReadResult, gold_profiles: list[ContractProfile]
) -> None:
    assert sample_result.plan is not None
    result = match_loans(sample_result.plan, gold_profiles)
    labels = {key: [ln.label for ln in lines] for key, lines in result.assigned.items()}
    assert labels[("V4", "V4")] == ["Konsortialkredit (Agent: Weserland Bank AG)"]
    assert "4471 203 118" in labels[("V1", "V1")][0]
    assert result.ambiguous == []
    assert [ln.label.split(" (")[0] for ln in result.unmatched] == [
        "Altdarlehen", "Gesellschafterdarlehen Stadt Musterstadt"
    ]  # fmt: skip


def test_loan_matching_without_numbers_and_ambiguity(
    sample_result: PlanReadResult, gold_profiles: list[ContractProfile]
) -> None:
    assert sample_result.plan is not None
    loans = [ln.model_copy(update={"loan_number": None}) for ln in sample_result.plan.loans]
    loans[3] = loans[3].model_copy(update={"label": "Konsortialkredit Sparkasse Musterstadt"})
    report = calculate(_replace_loans(sample_result.plan, loans), gold_profiles, [])
    ambiguous = [f for f in report.findings if f.code is ErrorCode.PLAN_LOAN_AMBIGUOUS]
    assert len(ambiguous) == 1
    assert "V1, V4" in ambiguous[0].message_de


def test_repayment_differs_and_contract_missing(
    sample_result: PlanReadResult, gold_profiles: list[ContractProfile]
) -> None:
    assert sample_result.plan is not None
    loans = list(sample_result.plan.loans)
    loans[1] = loans[1].model_copy(update={"scheduled_repayment": Decimal(1400)})  # V2: 1.500
    del loans[2]  # V3 line
    report = calculate(_replace_loans(sample_result.plan, loans), gold_profiles, [])
    differs = next(f for f in report.findings if f.code is ErrorCode.PLAN_REPAYMENT_DIFFERS)
    assert "1.400" in differs.message_de
    assert "1.500" in differs.message_de
    assert differs.affects == ["V3-DSCR"]  # the DSCR uses the scheduled repayments
    missing = next(f for f in report.findings if f.code is ErrorCode.PLAN_CONTRACT_NO_LOAN)
    assert "LBM-INV-2025-0412" in missing.message_de


def test_company_mismatch_blocks(
    sample_result: PlanReadResult, gold_profiles: list[ContractProfile]
) -> None:
    assert sample_result.plan is not None
    plan = sample_result.plan.model_copy(update={"company": "Stadtwerke Andersstadt GmbH"})
    report = calculate(plan, gold_profiles, [])
    assert {r.status for r in report.results} == {"not_computable"}
    assert report.findings[0].code is ErrorCode.PLAN_COMPANY_MISMATCH


def test_contract_filter(
    sample_result: PlanReadResult, gold_profiles: list[ContractProfile]
) -> None:
    report = calculate(sample_result.plan, gold_profiles, [], contract_filter={"V3"})
    assert [r.covenant_id for r in report.results] == ["V3-DSCR"]


def test_no_plan(gold_profiles: list[ContractProfile]) -> None:
    report = calculate(None, gold_profiles, [])
    assert (report.header, report.results, report.overall) == (None, [], "incomplete")


# ── CLI ──────────────────────────────────────────────────────────────────────


@pytest.fixture
def at_root(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(ROOT)  # default CONTRACTS_DIR / PROFILES_DIR are relative to the repo


@pytest.mark.usefixtures("at_root")
def test_cli_check_table() -> None:
    result = CliRunner().invoke(app, ["check", str(SAMPLE), "--profiles", str(GOLD)])
    assert result.exit_code == 0, result.output
    out = result.output
    for row in ("34,32 %", "≥ 27,5 %", "6,82 PP", "1,56", "30,7 %", "0,7 PP", "3,37", "≤ 3,50"):
        assert row in out
    assert out.count("ja (knapp)") >= 2
    assert "(148.200 − 12.600) / 40.200" in out
    assert "Am engsten: Konsortialkredit · Dynamischer Verschuldungsgrad" in out


@pytest.mark.usefixtures("at_root")
def test_cli_check_json() -> None:
    result = CliRunner().invoke(app, ["check", str(SAMPLE), "--profiles", str(GOLD), "--json"])
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
    result = CliRunner().invoke(app, ["contracts", "check", "--profiles", str(GOLD)])
    assert result.exit_code == 0, result.output
    assert "Beispiel V3-DSCR: erwartet 1,54, berechnet 1,54" in result.output
    assert "0 Fehler" in result.output


@pytest.mark.usefixtures("at_root")
def test_cli_contracts_check_fails_on_mismatch(tmp_path: Path) -> None:
    data = json.loads((GOLD / "V3.json").read_text(encoding="utf-8"))
    data["examples"][0]["expected"] = "1.60"
    (tmp_path / "V3.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    result = CliRunner().invoke(app, ["contracts", "check", "--profiles", str(tmp_path)])
    assert result.exit_code == 1
    assert "PROFILE_EXAMPLE_MISMATCH" in result.output
