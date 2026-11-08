"""Extraction eval: gold vs. gold is perfect, mutations are detected, answer key reproduces."""

from collections.abc import Callable
from pathlib import Path
from typing import Any

from finn.contracts.eval import Check, answer_key_checks, compare, evaluate_dirs, flatten
from finn.models.contract import ContractProfile
from mailkit import TIGHT_RATIO

SAMPLE = Path(__file__).resolve().parents[3] / "material" / "Wirtschaftsplan_2027.xlsx"


def by_id(profiles: list[ContractProfile], cid: str) -> ContractProfile:
    return next(p for p in profiles if p.contract_id == cid)


def mutate(profile: ContractProfile, change: Callable[[dict[str, Any]], None]) -> ContractProfile:
    data = profile.model_dump(mode="json")
    change(data)
    return ContractProfile.model_validate(data)


def failed(checks: list[Check]) -> list[str]:
    return [c.name for c in checks if not c.ok]


def test_gold_against_gold_is_perfect(gold_profiles: list[ContractProfile]) -> None:
    results = evaluate_dirs(gold_profiles, gold_profiles)
    assert [r.score for r in results] == [1.0] * 4
    assert all(len(r.checks) > 5 for r in results)


def test_flatten_expands_definitions_and_conditions(gold_profiles: list[ContractProfile]) -> None:
    v4 = by_id(gold_profiles, "V4")
    dvg = v4.covenant("V4-DVG")
    assert dvg is not None
    leaves = {k: v.weight for k, v in flatten(v4, dvg.numerator).items()}
    assert leaves[("shareholder_loan", "exclude_if:qualified_subordination")] == 1
    assert leaves[("cash", "only_if:cash_freely_available")] == -1
    v3 = by_id(gold_profiles, "V3")
    dscr = v3.covenant("V3-DSCR")
    assert dscr is not None
    numerator = {k[0]: v.weight for k, v in flatten(v3, dscr.numerator).items()}
    assert "income_taxes" not in numerator  # EBITDA (+taxes) − taxes nets out


def test_shareholder_loan_not_excluded_is_detected(gold_profiles: list[ContractProfile]) -> None:
    v4 = by_id(gold_profiles, "V4")

    def drop_condition(data: dict[str, Any]) -> None:
        for c in data["definitions"]["financial_debt"]["components"]:
            if c["item"] == "shareholder_loan":
                c["exclude_if"] = None

    ev = compare(v4, mutate(v4, drop_condition))
    assert "V4-DVG numerator (Bausteine × Gewicht [Bedingung])" in failed(ev.checks)
    assert "V4-DVG Definition Finanzverbindlichkeiten Bestandteile" in failed(ev.checks)
    assert ev.score < 1


def test_wrong_weight_threshold_and_cure_are_detected(
    gold_profiles: list[ContractProfile],
) -> None:
    v2 = by_id(gold_profiles, "V2")

    def change(data: dict[str, Any]) -> None:
        for c in data["definitions"]["economic_equity"]["components"]:
            if c.get("item") == "sonderposten":
                c["weight"] = "1"
        data["definitions"]["economic_equity"]["exclusions"] = []
        cov = data["covenants"][0]
        cov["thresholds"][0]["value"] = "25.0"
        cov["cure"] = {"allowed": False}

    names = failed(compare(v2, mutate(v2, change)).checks)
    assert "V2-WEKQ Grenzwert" in names
    assert "V2-WEKQ Heilung erlaubt" in names
    assert "V2-WEKQ numerator (Bausteine × Gewicht [Bedingung])" in names
    assert "V2-WEKQ Definition Wirtschaftliches Eigenkapital Ausschlüsse erfasst" in names


def test_v1_with_a_covenant_and_missing_profile(gold_profiles: list[ContractProfile]) -> None:
    v1, v4 = by_id(gold_profiles, "V1"), by_id(gold_profiles, "V4")
    data = v4.model_dump(mode="json")
    data["contract_id"] = "V1"
    names = failed(compare(v1, ContractProfile.model_validate(data)).checks)
    assert "has_financial_covenants" in names
    assert "Covenants (metric_type)" in names
    assert compare(v1, None).score == 0


def test_answer_key_with_gold_profiles(gold_profiles: list[ContractProfile]) -> None:
    checks = answer_key_checks(gold_profiles, SAMPLE, tight_ratio=TIGHT_RATIO)
    assert [c.name for c in checks if not c.ok] == []
    assert len(checks) == 6  # 4 covenants + V1 + V3 example


def test_answer_key_detects_a_wrong_recipe(gold_profiles: list[ContractProfile]) -> None:
    v4 = by_id(gold_profiles, "V4")

    def include_loan(data: dict[str, Any]) -> None:
        for c in data["definitions"]["financial_debt"]["components"]:
            if c["item"] == "shareholder_loan":
                c["exclude_if"] = None

    profiles = [p for p in gold_profiles if p.contract_id != "V4"]
    profiles.append(mutate(v4, include_loan))
    failing = {
        c.name: c.actual
        for c in answer_key_checks(profiles, SAMPLE, tight_ratio=TIGHT_RATIO)
        if not c.ok
    }
    assert list(failing) == ["Antwortschlüssel V4 net_debt_to_ebitda"]
    assert failing["Antwortschlüssel V4 net_debt_to_ebitda"].startswith("3.57")
