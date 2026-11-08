"""Completeness scan: every covenant signal in the contract is covered by the profile."""

from finn.contracts import completeness
from finn.contracts.pdf_text import ContractText
from finn.models.contract import ContractProfile
from mailkit import profile_by_id


def test_gold_profiles_cover_every_hit(
    gold_profiles: list[ContractProfile], texts: dict[str, ContractText]
) -> None:
    for p in gold_profiles:
        hits = completeness.scan(p, texts[p.contract_id])
        assert hits, p.contract_id
        assert [h.describe() for h in hits if not h.covered] == [], p.contract_id


def test_scan_finds_the_known_signals(texts: dict[str, ContractText]) -> None:
    pages = {(page, terms) for page, _, terms in completeness.find_hits(texts["V1"])}
    assert (1, ("eigenkapitalquote", "verschuldungsgrad", "ebitda")) in pages  # preamble
    v4_pages = {page for page, _, _ in completeness.find_hits(texts["V4"])}
    assert {2, 4, 6, 7, 13} <= v4_pages  # definitions, margin grid, § 14, certificate


def test_missing_dismissal_is_reported(
    gold_profiles: list[ContractProfile], texts: dict[str, ContractText]
) -> None:
    """V1 without its preamble source: the historical ratios become an uncovered hit."""
    v1 = profile_by_id(gold_profiles, "V1")
    data = v1.model_dump(mode="json")
    data["sources"] = [s for s in data["sources"] if s["id"] != "S_PRAEAMBEL"]
    data["dismissed_mentions"] = [
        m for m in data["dismissed_mentions"] if m["source_id"] != "S_PRAEAMBEL"
    ]
    data["no_covenant_reason"]["source_id"] = "S_TITEL"
    hits = completeness.uncovered(ContractProfile.model_validate(data), texts["V1"])
    assert [h.page for h in hits] == [1]
    assert "28,4 %" in hits[0].snippet


def test_margin_grid_without_pricing_entry_is_reported(
    gold_profiles: list[ContractProfile], texts: dict[str, ContractText]
) -> None:
    v4 = profile_by_id(gold_profiles, "V4")
    data = v4.model_dump(mode="json")
    drop = {"S_MARGE", "S_MARGE_4"}
    data["sources"] = [s for s in data["sources"] if s["id"] not in drop]
    data["dismissed_mentions"] = [
        m for m in data["dismissed_mentions"] if m["source_id"] not in drop
    ]
    data["pricing_grid"] = None
    hits = completeness.uncovered(ContractProfile.model_validate(data), texts["V4"])
    assert hits
    assert {h.page for h in hits} == {4}
