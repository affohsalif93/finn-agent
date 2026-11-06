"""The sample plan (material/Wirtschaftsplan_2027.xlsx) -> exact building blocks in T€."""

from datetime import date
from decimal import Decimal

import pytest

from finn.errors import ErrorCode, Severity, has_blocking
from finn.models.plan import CellRef, FactKey, ItemKey, LoanFactKey, Plan
from finn.plan import PlanReadResult

EXPECTED: dict[ItemKey, tuple[int, str | None]] = {
    ItemKey.NET_INCOME: (10700, "GuV!D18"),
    ItemKey.INCOME_TAXES: (3200, "GuV!D17"),
    ItemKey.INTEREST_EXPENSE: (5100, "GuV!D13"),
    ItemKey.PENSION_UNWINDING: (170, "GuV!D14"),
    ItemKey.INTEREST_INCOME: (300, "GuV!D12"),
    ItemKey.DEPRECIATION: (21500, "GuV!D9"),
    ItemKey.TOTAL_ASSETS: (313200, "Bilanz!D15"),
    ItemKey.TOTAL_EQUITY_LIABILITIES: (313200, "Bilanz!D34"),
    ItemKey.EQUITY: (96100, "Bilanz!D22"),
    ItemKey.SONDERPOSTEN: (22800, "Bilanz!D23"),
    ItemKey.BANK_DEBT: (148200, "Bilanz!D28"),
    ItemKey.SHAREHOLDER_LOAN: (8000, "Bilanz!D29"),
    ItemKey.CASH: (12600, "Bilanz!D12"),
    ItemKey.SCHEDULED_REPAYMENTS_TOTAL: (18750, None),
    ItemKey.SPECIAL_REPAYMENTS_TOTAL: (3000, None),
    ItemKey.TOTAL_BANK_DEBT: (148200, None),
    ItemKey.INTEREST_ON_FINANCIAL_DEBT: (4930, None),
}


@pytest.fixture
def plan(sample_result: PlanReadResult) -> Plan:
    assert sample_result.plan is not None
    return sample_result.plan


@pytest.mark.parametrize(("item", "expected"), EXPECTED.items(), ids=lambda x: str(x))
def test_building_block(plan: Plan, item: ItemKey, expected: tuple[int, str | None]) -> None:
    value, cell = expected
    pv = plan.values[item]
    assert pv.value == Decimal(value)
    assert (str(pv.ref) if pv.ref else None) == cell
    assert pv.original_unit == "T€"


def test_only_expected_blocks(plan: Plan) -> None:
    """Optional blocks (bonds, leasing, factoring, called capital, extraordinary) are absent."""
    assert set(plan.values) == set(EXPECTED)


def test_cover(plan: Plan) -> None:
    assert plan.company == "Stadtwerke Musterstadt GmbH"
    assert plan.plan_year == 2027
    assert plan.reporting_date == date(2027, 12, 31)
    assert plan.status == "draft"
    assert plan.version_date == date(2026, 10, 15)
    assert len(plan.file_sha256) == 64


def test_loans(plan: Plan) -> None:
    numbers = [(ln.loan_number, ln.kind) for ln in plan.loans]
    assert numbers == [
        ("4471 203 118", "bank"),
        ("FD-2022-08817", "bank"),
        ("LBM-INV-2025-0412", "bank"),
        (None, "bank"),  # Konsortialkredit: matched by label / agent later
        (None, "bank"),  # Altdarlehen
        (None, "shareholder"),
    ]
    scheduled = [ln.scheduled_repayment for ln in plan.loans]
    assert scheduled == [Decimal(v) for v in (1250, 1500, 3000, 3000, 9000, 1000)]
    landesbank = plan.loans[2]
    assert (landesbank.opening, landesbank.drawdown, landesbank.closing) == (22000, 15000, 34000)
    assert plan.loans[3].special_repayment == 3000
    assert landesbank.refs["closing"] == CellRef(sheet="Finanzierung", cell="F7")


def test_aggregates_list_every_cell(plan: Plan) -> None:
    scheduled = plan.values[ItemKey.SCHEDULED_REPAYMENTS_TOTAL]
    assert [str(r) for r in scheduled.derived_from] == [
        f"Finanzierung!D{r}" for r in (5, 6, 7, 8, 9, 13)
    ]
    bank = plan.values[ItemKey.TOTAL_BANK_DEBT]  # bank loans only, not the shareholder loan
    assert [str(r) for r in bank.derived_from] == [f"Finanzierung!F{r}" for r in range(5, 10)]


def test_facts(plan: Plan) -> None:
    sub = plan.facts[FactKey.QUALIFIED_SUBORDINATION]
    assert (sub.value, sub.ambiguous, str(sub.evidence)) == (True, False, "Bilanz!A39")
    assert sub.quote is not None
    assert "§ 39 Abs. 2 InsO" in sub.quote
    cash = plan.facts[FactKey.CASH_FREELY_AVAILABLE]
    assert (cash.value, cash.ambiguous, str(cash.evidence)) == (True, False, "Bilanz!A40")


def test_loan_facts_note_4(plan: Plan) -> None:
    old = plan.loans[4]
    assert old.label.startswith("Altdarlehen")
    assert old.facts[LoanFactKey.BULLET_IN_PLAN_YEAR].value is False
    refinanced = old.facts[LoanFactKey.REFINANCED]
    assert (refinanced.value, str(refinanced.evidence)) == (False, "Finanzierung!A24")
    assert all(not ln.facts[LoanFactKey.BULLET_IN_PLAN_YEAR].value for ln in plan.loans)


def test_findings_only_status(sample_result: PlanReadResult) -> None:
    assert not has_blocking(sample_result.findings)
    assert [(f.code, f.severity) for f in sample_result.findings] == [
        (ErrorCode.PLAN_STATUS, Severity.INFO)
    ]
    assert sample_result.findings[0].message_de == (
        "Entwurf vom 15.10.2026, nicht vom Aufsichtsrat genehmigt."
    )


def test_json_round_trip(sample_result: PlanReadResult) -> None:
    dumped = sample_result.model_dump_json()
    assert '"value":"148200"' in dumped  # Decimal as string
    assert type(sample_result).model_validate_json(dumped) == sample_result
