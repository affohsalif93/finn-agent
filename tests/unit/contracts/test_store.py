"""Profile store: schema / invariant / recipe errors, hash check, review status, self-test."""

import json
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from finn.calc.engine import calculate
from finn.contracts.selftest import check_examples
from finn.contracts.store import load_profiles
from finn.errors import ErrorCode
from finn.plan import PlanReadResult
from mailkit import TIGHT_RATIO

ROOT = Path(__file__).resolve().parents[3]
GOLD = ROOT / "tests" / "fixtures" / "profiles_gold"
MATERIAL = ROOT / "material"

Edit = Callable[[dict[str, Any]], None]


def _profiles_dir(tmp_path: Path, contract: str = "V4", edit: Edit | None = None) -> Path:
    out = tmp_path / "profiles"
    out.mkdir()
    data = json.loads((GOLD / f"{contract}.json").read_text(encoding="utf-8"))
    if edit:
        edit(data)
    (out / f"{contract}.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return out


def _codes(findings: list[Any]) -> list[ErrorCode]:
    return [f.code for f in findings]


def test_gold_loads_without_findings() -> None:
    profiles, findings = load_profiles(GOLD, MATERIAL)
    assert len(profiles) == 4
    assert findings == []


def test_unknown_item_is_an_invalid_recipe(tmp_path: Path) -> None:
    def edit(d: dict[str, Any]) -> None:
        d["definitions"]["equity"]["components"][0]["item"] = "goodwill"

    profiles, findings = load_profiles(_profiles_dir(tmp_path, edit=edit), MATERIAL)
    assert profiles == []
    assert ErrorCode.PROFILE_INVALID_RECIPE in _codes(findings)


def test_item_and_ref_together_is_invalid(tmp_path: Path) -> None:
    def edit(d: dict[str, Any]) -> None:
        d["definitions"]["equity"]["components"][0]["ref"] = "ebitda"

    profiles, findings = load_profiles(_profiles_dir(tmp_path, edit=edit), MATERIAL)
    assert profiles == []
    assert _codes(findings)[0] is ErrorCode.PROFILE_INVALID_RECIPE


def test_invariant_and_unknown_source(tmp_path: Path) -> None:
    def no_covenants(d: dict[str, Any]) -> None:
        d["has_financial_covenants"] = False

    profiles, findings = load_profiles(_profiles_dir(tmp_path, edit=no_covenants), MATERIAL)
    assert (profiles, _codes(findings)[:1]) == ([], [ErrorCode.PROFILE_INVARIANT])

    def unknown_source(d: dict[str, Any]) -> None:
        d["covenants"][0]["source_id"] = "S_NOPE"

    other = tmp_path / "b"
    other.mkdir()
    profiles, findings = load_profiles(_profiles_dir(other, edit=unknown_source), MATERIAL)
    assert profiles == []
    assert "S_NOPE" in findings[0].message_de


def test_broken_json_is_profile_missing(tmp_path: Path) -> None:
    (tmp_path / "V9.json").write_text("{not json", encoding="utf-8")
    profiles, findings = load_profiles(tmp_path, None)
    assert (profiles, _codes(findings)) == ([], [ErrorCode.PROFILE_MISSING])


def test_sha_mismatch_and_draft_warn(tmp_path: Path) -> None:
    def edit(d: dict[str, Any]) -> None:
        d["document"]["sha256"] = "0" * 64
        d["review"]["status"] = "draft"

    profiles, findings = load_profiles(_profiles_dir(tmp_path, edit=edit), MATERIAL)
    assert len(profiles) == 1  # still usable, with warnings
    codes = _codes(findings)
    assert ErrorCode.CONTRACT_CHANGED in codes
    assert ErrorCode.PROFILE_NOT_REVIEWED in codes
    assert all(f.severity == "WARN" for f in findings if f.code is not ErrorCode.CONTRACT_NEW_FILE)


def test_contract_without_profile_and_missing_file(tmp_path: Path) -> None:
    contracts = tmp_path / "contracts"
    contracts.mkdir()
    shutil.copy(MATERIAL / "Vertrag_1_Sparkasse_Investitionsdarlehen.pdf", contracts)
    _, findings = load_profiles(_profiles_dir(tmp_path), contracts)
    codes = _codes(findings)
    assert ErrorCode.CONTRACT_NEW_FILE in codes  # V1 PDF without a profile
    assert ErrorCode.CONTRACT_CHANGED in codes  # V4 PDF not in that folder


def test_bad_ref_makes_only_that_covenant_manual_review(
    tmp_path: Path, sample_result: PlanReadResult
) -> None:
    def edit(d: dict[str, Any]) -> None:
        d["covenants"][1]["denominator"] = [{"ref": "ebitda_typo"}]

    profiles, findings = load_profiles(_profiles_dir(tmp_path, edit=edit), MATERIAL)
    assert [f.affects for f in findings if f.code is ErrorCode.PROFILE_INVALID_RECIPE] == [
        ["V4-DVG"]
    ]
    report = calculate(
        sample_result.plan, profiles, [], tight_ratio=TIGHT_RATIO, profile_findings=findings
    )
    status = {r.covenant_id: r.status for r in report.results}
    assert status == {"V4-EKQ": "met", "V4-DVG": "manual_review"}
    assert report.overall == "incomplete"


def test_cycle_is_detected(tmp_path: Path) -> None:
    def edit(d: dict[str, Any]) -> None:
        d["definitions"]["financial_debt"]["components"].append({"ref": "net_financial_debt"})

    _, findings = load_profiles(_profiles_dir(tmp_path, edit=edit), MATERIAL)
    recipe = [f for f in findings if f.code is ErrorCode.PROFILE_INVALID_RECIPE]
    assert recipe
    assert "Zirkelbezug" in recipe[0].message_de


def test_unmapped_manual_review_blocks_the_covenant(
    tmp_path: Path, sample_result: PlanReadResult
) -> None:
    def edit(d: dict[str, Any]) -> None:
        d["definitions"]["financial_debt"]["unmapped_components"][0]["effect"] = "manual_review"

    profiles, findings = load_profiles(_profiles_dir(tmp_path, edit=edit), MATERIAL)
    report = calculate(
        sample_result.plan, profiles, [], tight_ratio=TIGHT_RATIO, profile_findings=findings
    )
    dvg = next(r for r in report.results if r.covenant_id == "V4-DVG")
    assert dvg.status == "manual_review"
    assert any(f.code is ErrorCode.PROFILE_UNMAPPED for f in dvg.findings)


def test_example_mismatch(tmp_path: Path) -> None:
    def edit(d: dict[str, Any]) -> None:
        d["examples"][0]["expected"] = "1.60"

    profiles, _ = load_profiles(_profiles_dir(tmp_path, "V3", edit), MATERIAL)
    (mismatch,) = check_examples(profiles[0])
    assert mismatch.code is ErrorCode.PROFILE_EXAMPLE_MISMATCH
    assert "1,54" in mismatch.message_de


@pytest.mark.parametrize("contract", ["V1", "V2", "V3", "V4"])
def test_roundtrip_json(contract: str) -> None:
    profiles, _ = load_profiles(GOLD, None)
    profile = next(p for p in profiles if p.contract_id == contract)
    again = type(profile).model_validate_json(profile.model_dump_json())
    assert again == profile
