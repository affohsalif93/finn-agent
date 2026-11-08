"""Calc engine inputs: contract filter, no readable plan."""

from finn.calc.engine import calculate
from finn.models.contract import ContractProfile
from finn.plan import PlanReadResult
from mailkit import TIGHT_RATIO


def test_contract_filter(
    sample_result: PlanReadResult, gold_profiles: list[ContractProfile]
) -> None:
    report = calculate(
        sample_result.plan, gold_profiles, [], tight_ratio=TIGHT_RATIO, contract_filter={"V3"}
    )
    assert [r.covenant_id for r in report.results] == ["V3-DSCR"]


def test_no_plan(gold_profiles: list[ContractProfile]) -> None:
    report = calculate(None, gold_profiles, [], tight_ratio=TIGHT_RATIO)
    assert (report.header, report.results, report.overall) == (None, [], "incomplete")
