"""Rounding, comparison, headroom, edge cases and the sensitivity solver."""

from decimal import Decimal

import pytest

from finn.calc import rounding
from finn.calc.engine import calculate
from finn.calc.formula import Term, render_terms
from finn.calc.sensitivity import break_point, max_whole_drop
from finn.errors import ErrorCode
from finn.models.contract import ContractProfile
from finn.models.plan import ItemKey, Plan
from finn.models.result import CovenantResult
from finn.numbers import number_de
from finn.plan import PlanReadResult
from mailkit import TIGHT_RATIO

D = Decimal


@pytest.mark.parametrize(
    ("raw", "decimals", "expected"),
    [
        ("30.65", 1, "30.7"),
        ("30.649", 1, "30.6"),
        ("3.375", 2, "3.38"),
        ("3.3749", 2, "3.37"),
        ("34.32311", 2, "34.32"),
        ("1.5625", 2, "1.56"),
        ("-0.05", 1, "-0.1"),  # half up = away from zero
    ],
)
def test_round_half_up(raw: str, decimals: int, expected: str) -> None:
    assert rounding.round_half_up(D(raw), decimals) == D(expected)


def test_no_rounding_rule_means_two_decimals() -> None:
    assert rounding.effective_decimals(None) == 2
    assert rounding.effective_decimals(1) == 1


def test_compare_headroom_tight() -> None:
    assert rounding.is_met(D("30.0"), ">=", D("30.0"))
    assert not rounding.is_met(D("3.51"), "<=", D("3.50"))
    assert rounding.headroom(D("3.37"), "<=", D("3.50")) == D("0.13")
    assert rounding.headroom(D("29.9"), ">=", D("30.0")) == D("-0.1")
    assert rounding.is_tight(D("0.7"), D("30.0"), D("0.10"))
    assert not rounding.is_tight(D("0.26"), D("1.30"), D("0.10"))
    assert not rounding.is_tight(D("-0.1"), D("30.0"), D("0.10"))  # breached, not "tight"
    assert rounding.headroom_unit("percent") == "pp"
    assert rounding.boundary(">=", D("30.0"), 1) == (D("29.95"), True)
    assert rounding.boundary("<=", D("3.50"), 2) == (D("3.505"), False)


# ── engine edges on a modified sample plan ───────────────────────────────────


def _set(plan: Plan, **values: str) -> Plan:
    new = dict(plan.values)
    for key, value in values.items():
        item = ItemKey(key)
        new[item] = new[item].model_copy(update={"value": D(value)})
    return plan.model_copy(update={"values": new})


def _result(plan: Plan, profiles: list[ContractProfile], covenant: str) -> CovenantResult:
    report = calculate(plan, profiles, [], tight_ratio=TIGHT_RATIO)
    return next(r for r in report.results if r.covenant_id == covenant)


@pytest.mark.parametrize(
    ("equity", "value", "status", "headroom"),
    [
        ("30650", "30.7", "met", "0.7"),  # 30.65 % → 30.7
        ("30649", "30.6", "met", "0.6"),  # 30.649 % → 30.6
        ("29950", "30.0", "met", "0.0"),  # 29.95 % rounds up onto the threshold: met, no headroom
        ("29949", "29.9", "breached", "-0.1"),  # 29.949 % → 29.9
    ],
)
def test_equity_ratio_rounding_edges(
    sample_result: PlanReadResult,
    gold_profiles: list[ContractProfile],
    equity: str,
    value: str,
    status: str,
    headroom: str,
) -> None:
    assert sample_result.plan is not None
    plan = _set(sample_result.plan, equity=equity, total_assets="100000")
    r = _result(plan, gold_profiles, "V4-EKQ")
    assert (r.value_rounded, r.status) == (D(value), status)
    assert r.headroom == D(headroom)
    at_threshold = [f for f in r.findings if f.code is ErrorCode.CALC_AT_THRESHOLD]
    assert bool(at_threshold) is (equity == "29950")


def test_denominator_zero_is_not_computable(
    sample_result: PlanReadResult, gold_profiles: list[ContractProfile]
) -> None:
    assert sample_result.plan is not None
    plan = _set(sample_result.plan, total_assets="0")
    r = _result(plan, gold_profiles, "V4-EKQ")
    assert r.status == "not_computable"
    assert r.value_rounded is None
    assert any(f.code is ErrorCode.CALC_DENOMINATOR for f in r.findings)
    assert r.formula_de == "96.100 / 0"  # the calculation path is still shown


def test_ebitda_not_positive_leverage_not_meaningful(
    sample_result: PlanReadResult, gold_profiles: list[ContractProfile]
) -> None:
    assert sample_result.plan is not None
    plan = _set(sample_result.plan, net_income="-40000")  # EBITDA = -500
    r = _result(plan, gold_profiles, "V4-DVG")
    assert r.status == "not_computable"
    f = next(f for f in r.findings if f.code is ErrorCode.PLAN_IMPLAUSIBLE)
    assert "nicht sinnvoll definiert" in f.message_de
    assert r.reason is not None


def test_missing_block_with_partial_policy(
    sample_result: PlanReadResult, gold_profiles: list[ContractProfile]
) -> None:
    assert sample_result.plan is not None
    values = {k: v for k, v in sample_result.plan.values.items() if k is not ItemKey.SONDERPOSTEN}
    plan = sample_result.plan.model_copy(update={"values": values})
    r = _result(plan, gold_profiles, "V2-WEKQ")
    assert r.status == "not_computable"
    assert any(f.code is ErrorCode.PLAN_ITEM_MISSING for f in r.findings)
    assert _result(plan, gold_profiles, "V4-EKQ").status == "met"  # V4 does not use it


# ── sensitivity solver ───────────────────────────────────────────────────────


def test_break_point_matches_algebra() -> None:
    # leverage 135,600 / (40,200 - d) <= 3.50 after rounding → raw < 3.505
    d = break_point(D(135600), D(0), D(40200), D(-1), scale=D(1), operator="<=",
                    threshold=D("3.50"), decimals=2)  # fmt: skip
    assert d is not None
    assert D("1512.40") < d < D("1512.42")
    # equity ratio 96,100 - d over 313,200 >= 30.0 % after rounding → raw >= 29.95
    e = break_point(D(96100), D(-1), D(313200), D(0), scale=D(100), operator=">=",
                    threshold=D("30.0"), decimals=1)  # fmt: skip
    assert e == D("2296.6")


def test_max_whole_drop_is_rounding_aware() -> None:
    def rounded_at(d: Decimal) -> Decimal | None:
        den = D(40200) - d
        return None if den <= 0 else rounding.round_half_up(D(135600) / den, 2)

    assert max_whole_drop(rounded_at, "<=", D("3.50"), D("1512.41")) == D(1512)
    # a bad estimate is corrected by stepping
    assert max_whole_drop(rounded_at, "<=", D("3.50"), D("1500")) == D(1512)
    assert max_whole_drop(rounded_at, "<=", D("3.50"), D("1530")) == D(1512)


def test_sensitivity_negative_when_already_breached(
    sample_result: PlanReadResult, gold_profiles: list[ContractProfile]
) -> None:
    assert sample_result.plan is not None
    plan = _set(sample_result.plan, equity="90000")  # 28.7 % < 30.0 %
    r = _result(plan, gold_profiles, "V4-EKQ")
    assert r.status == "breached"
    assert r.sensitivity is not None
    assert r.sensitivity.max_drop_t_eur < 0
    assert "steigen" in r.sensitivity.text_de


def test_number_format() -> None:
    assert number_de(D("148200")) == "148.200"
    assert number_de(D("0.5")) == "0,5"
    assert number_de(D("1.30")) == "1,3"
    assert render_terms([Term(D(1), D(148200)), Term(D(-1), D(12600))]) == "148.200 − 12.600"
    assert render_terms([Term(D(-1), D(5))]) == "−5"
