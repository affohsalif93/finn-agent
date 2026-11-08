"""Checks that need the request or contract profiles (not wired yet), plus sign / tie-out units."""

from decimal import Decimal

import pytest

from finn.errors import ErrorCode, Severity
from finn.models.plan import ItemKey, Plan, PlanValue
from finn.plan import PlanReadResult, checks


@pytest.fixture
def plan(sample_result: PlanReadResult) -> Plan:
    assert sample_result.plan is not None
    return sample_result.plan


def _with(plan: Plan, item: ItemKey, value: int) -> Plan:
    values = dict(plan.values)
    values[item] = PlanValue(
        item=item, value=Decimal(value), ref=None, label="test", original_unit="T€"
    )
    return plan.model_copy(update={"values": values})


@pytest.mark.parametrize(
    "borrower",
    ["Stadtwerke Musterstadt GmbH", "stadtwerke musterstadt", "Stadtwerke  Musterstadt G.m.b.H."],
)
def test_company_matches(plan: Plan, borrower: str) -> None:
    assert checks.check_company(plan, borrower) == []


def test_company_mismatch_blocks(plan: Plan) -> None:
    (f,) = checks.check_company(plan, "Stadtwerke Nachbarstadt GmbH")
    assert (f.code, f.severity) == (ErrorCode.PLAN_COMPANY_MISMATCH, Severity.BLOCK)
    assert f.affects == "all"
    assert "Stadtwerke Musterstadt GmbH" in f.message_de


@pytest.mark.parametrize("year", [None, 2027])
def test_plan_year_ok(plan: Plan, year: int | None) -> None:
    assert checks.check_plan_year(plan, year) == []


def test_plan_year_mismatch(plan: Plan) -> None:
    (f,) = checks.check_plan_year(plan, 2026)
    assert (f.code, f.severity) == (ErrorCode.PLAN_YEAR_MISMATCH, Severity.BLOCK)
    assert f.message_de == "Angefragt: 2026, die Datei enthält Plan 2027."


def test_bank_debt_off_by_factor_1000_warns(plan: Plan) -> None:
    amounts = [Decimal(20_000_000), Decimal(30_000_000), Decimal(37_000_000), Decimal(64_000_000)]
    assert checks.check_scale(plan, amounts) == []
    (f,) = checks.check_scale(_with(plan, ItemKey.BANK_DEBT, 148), amounts)
    assert (f.code, f.severity) == (ErrorCode.PLAN_SCALE_IMPLAUSIBLE, Severity.WARN)


def test_implausible_negative_equity_and_ebitda(plan: Plan) -> None:
    assert checks.check_implausible(plan) == []
    assert checks.ebitda_estimate(plan) == Decimal(40200)
    bad = _with(_with(plan, ItemKey.EQUITY, -500), ItemKey.NET_INCOME, -50000)
    codes = [(f.code, f.severity) for f in checks.check_implausible(bad)]
    assert codes == [(ErrorCode.PLAN_IMPLAUSIBLE, Severity.WARN)] * 2


def test_balance_uses_recomputed_totals(plan: Plan) -> None:
    assert checks.check_balance(plan) == []
    (f,) = checks.check_balance(plan, {ItemKey.TOTAL_EQUITY_LIABILITIES: Decimal(312900)})
    assert f.code is ErrorCode.PLAN_UNBALANCED


def test_interest_tieout_without_unwinding(plan: Plan) -> None:
    values = {k: v for k, v in plan.values.items() if k is not ItemKey.PENSION_UNWINDING}
    (f,) = checks.check_interest_tieout(plan.model_copy(update={"values": values}))
    assert f.code is ErrorCode.PLAN_INTEREST_TIEOUT


def test_status_approved_is_silent(plan: Plan) -> None:
    assert checks.check_status(plan.model_copy(update={"status": "approved"})) == []
    (f,) = checks.check_status(plan.model_copy(update={"version_date": None}))
    assert f.message_de.startswith("Entwurf (ohne Datum)")
