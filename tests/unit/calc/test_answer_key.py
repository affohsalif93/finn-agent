"""Answer key (SCOPE.md §3) with the hand-written gold profiles and the sample plan."""

import json
from decimal import Decimal
from pathlib import Path

import pytest

from finn.calc.engine import calculate
from finn.errors import ErrorCode
from finn.models.contract import ContractProfile
from finn.models.plan import FactKey, ItemKey, Plan
from finn.models.result import CovenantResult, Excluded, Report, Used
from finn.plan import PlanReadResult, read_plan
from mailkit import TIGHT_RATIO

GOLD = Path(__file__).resolve().parents[2] / "fixtures" / "profiles_gold"
D = Decimal


@pytest.fixture(scope="module")
def report(sample_result: PlanReadResult, gold_profiles: list[ContractProfile]) -> Report:
    return calculate(
        sample_result.plan, gold_profiles, sample_result.findings, tight_ratio=TIGHT_RATIO
    )


def by_id(report: Report) -> dict[str, CovenantResult]:
    return {r.covenant_id: r for r in report.results}


@pytest.mark.parametrize(
    ("covenant", "value", "headroom", "unit", "tight"),
    [
        ("V2-WEKQ", "34.32", "6.82", "pp", False),
        ("V3-DSCR", "1.56", "0.26", "ratio", False),
        ("V4-EKQ", "30.7", "0.7", "pp", True),
        ("V4-DVG", "3.37", "0.13", "ratio", True),
    ],
)
def test_answer_key(
    report: Report, covenant: str, value: str, headroom: str, unit: str, tight: bool
) -> None:
    r = by_id(report)[covenant]
    assert r.status == "met"
    assert r.value_rounded == D(value)
    assert str(r.value_rounded) == value  # contract decimals kept (30.7, not 30.70)
    assert r.headroom == D(headroom)
    assert r.headroom_unit == unit
    assert r.tight is tight


def test_overall_tightest_and_v1(report: Report) -> None:
    assert report.overall == "all_met"
    assert report.tightest == "V4-DVG"
    assert [r.contract_id for r in report.results] == ["V2", "V3", "V4", "V4"]
    v1 = [n for n in report.contract_notes if n.contract_id == "V1"]
    assert [n.kind for n in v1] == ["no_covenants"]
    assert "31.12.2019" in v1[0].text_de


def test_intermediate_values(report: Report) -> None:
    r = by_id(report)
    assert r["V2-WEKQ"].numerator_value == D("107500.0")
    assert r["V2-WEKQ"].denominator_value == D(313200)
    assert r["V3-DSCR"].numerator_value == D(37000)
    assert r["V3-DSCR"].denominator_value == D(23680)
    assert r["V4-DVG"].numerator_value == D(135600)
    assert r["V4-DVG"].denominator_value == D(40200)
    assert r["V4-EKQ"].numerator_value == D(96100)
    debt_service = r["V3-DSCR"].components.denominator[0]  # type: ignore[union-attr]
    assert isinstance(debt_service, Used)
    parts = {p.component: p for p in debt_service.parts if isinstance(p, Used)}
    assert parts["interest_on_financial_debt"].value == D(4930)
    assert parts["scheduled_repayments_total"].value == D(18750)


def test_formula_de(report: Report) -> None:
    r = by_id(report)
    assert r["V2-WEKQ"].formula_de == "(96.100 + 0,5 × 22.800) / 313.200"
    assert r["V3-DSCR"].formula_de == "(40.200 − 3.200) / (4.930 + 18.750)"
    assert r["V4-EKQ"].formula_de == "96.100 / 313.200"
    assert r["V4-DVG"].formula_de == "(148.200 − 12.600) / 40.200"
    ebitda = "EBITDA = 10.700 + 3.200 + 5.100 − 300 + 21.500 = 40.200"
    assert r["V4-DVG"].sub_formulas_de == [ebitda]
    assert r["V3-DSCR"].sub_formulas_de == [ebitda]


def _walk(traces: list) -> list:  # type: ignore[type-arg]
    out = []
    for t in traces:
        out.append(t)
        if isinstance(t, Used):
            out += _walk(t.parts)
    return out


def test_v3_includes_shareholder_loan_and_excludes_special_repayment(report: Report) -> None:
    r = by_id(report)["V3-DSCR"]
    traces = _walk(r.components.denominator)  # type: ignore[union-attr]
    loans = {t.label_de: t for t in traces if t.component.startswith("loan:")}
    shareholder = next(t for label, t in loans.items() if "Gesellschafterdarlehen" in label)
    assert isinstance(shareholder, Used)
    assert shareholder.value == D(1000)
    special = [t for t in traces if isinstance(t, Excluded) and t.component.startswith("special:")]
    assert [t.value for t in special] == [D(3000)]
    interest = next(t for t in traces if t.component == "interest_on_financial_debt")
    assert interest.plan_value is not None
    shareholder_interest_cell = "G13"  # Finanzierung: interest of the shareholder loan (220)
    assert any(ref.cell == shareholder_interest_cell for ref in interest.plan_value.derived_from)


def test_v4_excludes_subordinated_shareholder_loan_with_evidence(report: Report) -> None:
    r = by_id(report)["V4-DVG"]
    excluded = [t for t in _walk(r.components.numerator) if isinstance(t, Excluded)]  # type: ignore[union-attr]
    assert len(excluded) == 1
    assert excluded[0].component == "shareholder_loan"
    assert excluded[0].value == D(8000)
    assert excluded[0].evidence is not None
    assert excluded[0].evidence.key is FactKey.QUALIFIED_SUBORDINATION


def test_findings_and_notes(report: Report) -> None:
    r = by_id(report)
    v2_codes = [f.code for f in r["V2-WEKQ"].findings]
    assert ErrorCode.CALC_NO_ROUNDING_RULE in v2_codes
    assert ErrorCode.PLAN_STATUS_REQUIRED in v2_codes  # approved plan required, draft given
    v4 = [f for f in r["V4-DVG"].findings]
    assumed = next(f for f in v4 if f.code is ErrorCode.PLAN_ITEM_NOT_IN_PLAN)
    assert "Anleihen" in assumed.message_de
    unmapped = next(f for f in v4 if f.code is ErrorCode.PROFILE_UNMAPPED)
    assert unmapped.severity == "INFO"  # (e) is an assumption, not a blocker
    assert r["V4-DVG"].status == "met"
    notes = {(n.contract_id, n.kind): n.text_de for n in report.contract_notes}
    pricing = notes[("V4", "pricing")]
    assert "3,37 > 3,00" in pricing
    assert "1,25 %" in pricing
    assert "kein Covenant" in pricing
    assert "keine Heilung" in notes[("V4", "cure")]
    assert "höchstens 2×" in notes[("V3", "cure")]
    assert "Frühwarnung, kein Kündigungsgrund" in notes[("V4", "plan_check")]
    # no loan-schedule mismatches for the four contracts (V1: 1.250 = Anlage 1 for 2027)
    codes = [f.code for f in report.findings]
    assert ErrorCode.PLAN_REPAYMENT_DIFFERS not in codes
    assert ErrorCode.PLAN_CONTRACT_NO_LOAN not in codes
    assert ErrorCode.PLAN_LOAN_AMBIGUOUS not in codes


def test_sensitivity_values(report: Report) -> None:
    r = by_id(report)
    dvg = r["V4-DVG"].sensitivity
    ekq = r["V4-EKQ"].sensitivity
    assert dvg is not None
    assert ekq is not None
    assert (dvg.driver, dvg.max_drop_t_eur, dvg.max_drop_pct) == ("ebitda", D(1512), D("3.8"))
    assert (dvg.value_at_max_drop, dvg.value_after_next_t_eur) == (D("3.50"), D("3.51"))
    assert (ekq.driver, ekq.max_drop_t_eur, ekq.max_drop_pct) == ("equity", D(2296), D("2.4"))
    assert (ekq.value_at_max_drop, ekq.value_after_next_t_eur) == (D("30.0"), D("29.9"))
    assert r["V3-DSCR"].sensitivity.max_drop_t_eur == D(6334)  # type: ignore[union-attr]
    assert r["V2-WEKQ"].sensitivity.max_drop_t_eur == D(21385)  # type: ignore[union-attr]


def _with(plan: Plan, **changes: int) -> Plan:
    values = dict(plan.values)
    for key, delta in changes.items():
        item = ItemKey(key)
        values[item] = values[item].model_copy(update={"value": values[item].value + delta})
    return plan.model_copy(update={"values": values})


@pytest.mark.parametrize(
    ("covenant", "item", "drop", "expected", "status"),
    [
        ("V4-DVG", "net_income", 1512, "3.50", "met"),
        ("V4-DVG", "net_income", 1513, "3.51", "breached"),
        ("V4-EKQ", "equity", 2296, "30.0", "met"),
        ("V4-EKQ", "equity", 2297, "29.9", "breached"),
    ],
)
def test_sensitivity_verified_at_the_boundary(
    sample_result: PlanReadResult,
    gold_profiles: list[ContractProfile],
    covenant: str,
    item: str,
    drop: int,
    expected: str,
    status: str,
) -> None:
    assert sample_result.plan is not None
    plan = _with(sample_result.plan, **{item: -drop})
    r = by_id(calculate(plan, gold_profiles, [], tight_ratio=TIGHT_RATIO))[covenant]
    assert (r.value_rounded, r.status) == (D(expected), status)


# ── the trap: the subordination condition drives 3.37 vs. 3.57 ─────────────


def _v4_with_shareholder_loan() -> ContractProfile:
    data = json.loads((GOLD / "V4.json").read_text(encoding="utf-8"))
    for c in data["definitions"]["financial_debt"]["components"]:
        if c["item"] == "shareholder_loan":
            del c["exclude_if"]
    return ContractProfile.model_validate(data)


def test_trap_profile_with_shareholder_loan_gives_false_breach(
    sample_result: PlanReadResult,
) -> None:
    report = calculate(
        sample_result.plan, [_v4_with_shareholder_loan()], [], tight_ratio=TIGHT_RATIO
    )
    r = by_id(report)["V4-DVG"]
    assert (r.value_rounded, r.status) == (D("3.57"), "breached")


def test_trap_fact_false_includes_shareholder_loan(
    sample_result: PlanReadResult, gold_profiles: list[ContractProfile]
) -> None:
    plan = sample_result.plan
    assert plan is not None
    fact = plan.facts[FactKey.QUALIFIED_SUBORDINATION].model_copy(update={"value": False})
    plan = plan.model_copy(update={"facts": {**plan.facts, FactKey.QUALIFIED_SUBORDINATION: fact}})
    r = by_id(calculate(plan, gold_profiles, [], tight_ratio=TIGHT_RATIO))["V4-DVG"]
    assert (r.value_rounded, r.status) == (D("3.57"), "breached")
    assert r.alternatives == []  # the fact is clear (not ambiguous): no alternative


def test_gold_profile_gives_337(report: Report) -> None:
    assert by_id(report)["V4-DVG"].value_rounded == D("3.37")


# ── plan variants (scripts/make_plan_fixtures.py) ────────────────────────────


def _run(path: Path, profiles: list[ContractProfile]) -> Report:
    result = read_plan(path)
    return calculate(result.plan, profiles, result.findings, tight_ratio=TIGHT_RATIO)


def test_plan_no_financing_only_dscr_not_computable(
    plan_fixtures: dict[str, Path], gold_profiles: list[ContractProfile]
) -> None:
    report = _run(plan_fixtures["plan_no_financing.xlsx"], gold_profiles)
    status = {r.covenant_id: r.status for r in report.results}
    assert status == {
        "V2-WEKQ": "met", "V3-DSCR": "not_computable", "V4-EKQ": "met", "V4-DVG": "met"
    }  # fmt: skip
    dscr = by_id(report)["V3-DSCR"]
    assert dscr.reason is not None
    assert "Darlehensspiegel" in dscr.reason
    assert report.overall == "incomplete"


def test_plan_no_subordination_note_conservative_with_alternative(
    plan_fixtures: dict[str, Path], gold_profiles: list[ContractProfile]
) -> None:
    report = _run(plan_fixtures["plan_no_subordination_note.xlsx"], gold_profiles)
    r = by_id(report)
    dvg = r["V4-DVG"]
    assert (dvg.value_rounded, dvg.status) == (D("3.57"), "breached")
    assert [(a.assumption_de, a.value_rounded, a.status) for a in dvg.alternatives] == [
        ("ohne Gesellschafterdarlehen", D("3.37"), "met")
    ]
    ambiguous = [f for f in dvg.findings if f.code is ErrorCode.PLAN_FACT_AMBIGUOUS]
    assert any("3,37 / 3,57" in f.message_de for f in ambiguous)
    assert r["V3-DSCR"].value_rounded == D("1.56")  # V3 includes the loan regardless
    assert r["V4-EKQ"].value_rounded == D("30.7")
    assert report.overall == "some_breached"


@pytest.mark.parametrize("name", ["plan_in_eur.xlsx", "plan_renamed_labels.xlsx"])
def test_variants_keep_the_answer_key(
    plan_fixtures: dict[str, Path], gold_profiles: list[ContractProfile], name: str
) -> None:
    report = _run(plan_fixtures[name], gold_profiles)
    values = {r.covenant_id: r.value_rounded for r in report.results}
    assert values == {
        "V2-WEKQ": D("34.32"), "V3-DSCR": D("1.56"), "V4-EKQ": D("30.7"), "V4-DVG": D("3.37")
    }  # fmt: skip
    assert report.overall == "all_met"


def test_plan_bullet_states_refinancing_alternative(
    plan_fixtures: dict[str, Path], gold_profiles: list[ContractProfile]
) -> None:
    dscr = by_id(_run(plan_fixtures["plan_bullet.xlsx"], gold_profiles))["V3-DSCR"]
    assert dscr.alternatives
    assert dscr.alternatives[0].value_rounded > dscr.value_rounded  # type: ignore[operator]
    assert any(f.code is ErrorCode.PLAN_BULLET_IN_PLAN_YEAR for f in dscr.findings)


def test_unbalanced_plan_blocks_everything(
    plan_fixtures: dict[str, Path], gold_profiles: list[ContractProfile]
) -> None:
    report = _run(plan_fixtures["plan_unbalanced.xlsx"], gold_profiles)
    assert {r.status for r in report.results} == {"not_computable"}
    assert report.overall == "incomplete"
    assert report.tightest is None
