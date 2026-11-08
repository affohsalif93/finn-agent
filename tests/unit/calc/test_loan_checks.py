"""Contract ↔ plan checks: loan matching, repayments, company."""

from decimal import Decimal

from finn.calc.engine import calculate
from finn.calc.loan_checks import match_loans
from finn.errors import ErrorCode
from finn.models.contract import ContractProfile
from finn.models.plan import LoanLine, Plan
from finn.plan import PlanReadResult
from mailkit import TIGHT_RATIO


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
    report = calculate(
        _replace_loans(sample_result.plan, loans), gold_profiles, [], tight_ratio=TIGHT_RATIO
    )
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
    report = calculate(
        _replace_loans(sample_result.plan, loans), gold_profiles, [], tight_ratio=TIGHT_RATIO
    )
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
    report = calculate(plan, gold_profiles, [], tight_ratio=TIGHT_RATIO)
    assert {r.status for r in report.results} == {"not_computable"}
    assert report.findings[0].code is ErrorCode.PLAN_COMPANY_MISMATCH
