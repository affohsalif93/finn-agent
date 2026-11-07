"""Onboarding on the contract text: page split + cache, masking, quote validation, completeness."""

import json
from pathlib import Path

import pytest

from finn.contracts import completeness, pdf_text
from finn.contracts.pdf_text import ContractText, load_contract_text, mask_persons, render_pages
from finn.contracts.validate import check_quote, repair_quotes, text_findings, validate_profile
from finn.models.contract import ContractProfile

ROOT = Path(__file__).resolve().parents[3]
MATERIAL = ROOT / "material"
V4_PDF = MATERIAL / "Vertrag_4_Konsortialkredit.pdf"


@pytest.fixture(scope="module")
def texts(tmp_path_factory: pytest.TempPathFactory) -> dict[str, ContractText]:
    cache = tmp_path_factory.mktemp("cache")
    return {
        f"V{pdf.name.split('_')[1]}": load_contract_text(pdf, cache)
        for pdf in sorted(MATERIAL.glob("Vertrag_*.pdf"))
    }


def by_id(profiles: list[ContractProfile], cid: str) -> ContractProfile:
    return next(p for p in profiles if p.contract_id == cid)


# ── Page texts ───────────────────────────────────────────────────────────────


def test_pages_split_with_numbers(texts: dict[str, ContractText]) -> None:
    v4 = texts["V4"]
    assert v4.page_count == 13
    assert "§ 14 Finanzkennzahlen" in v4.page(6)
    assert "Eine Heilung durch Eigenkapitalzuführung" in v4.page(7)
    assert "Seite 7 von 13" in v4.page(7)
    rendered = render_pages(v4.pages)
    assert rendered.count("<page number=") == 13
    assert '<page number="13">' in rendered


def test_cache_by_hash_is_reused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    digest, pages = pdf_text.read_pages(V4_PDF, tmp_path)
    cached = tmp_path / f"{digest}.json"
    assert cached.is_file()
    assert json.loads(cached.read_text(encoding="utf-8"))["pages"] == pages

    def no_pdfplumber(*_: object) -> None:
        raise AssertionError("cache not used")

    monkeypatch.setattr(pdf_text.pdfplumber, "open", no_pdfplumber)
    assert pdf_text.read_pages(V4_PDF, tmp_path) == (digest, pages)


def test_masks_person_names_keeps_parties_and_roles() -> None:
    pages = [
        "Die Sparkasse Musterstadt, vertreten durch Herrn Max Mustermann, gewährt EUR "
        "25.000.000,00.\nDr. Erika Beispiel bestätigt § 14.",
        "Musterstadt, den 1.1.2025\n______________ ______________\n"
        "Sparkasse Musterstadt Stadtwerke Musterstadt GmbH\n"
        "Geschäftsführer Max Mustermann Kaufmännische Geschäftsführerin\n"
        "Johanna Schmidt-Weber Prokurist\nSeite 2 von 2",
    ]
    masked, mapping = mask_persons(pages)
    text = "\n".join(masked)
    for name in ("Max Mustermann", "Erika Beispiel", "Johanna Schmidt-Weber"):
        assert name not in text
    for kept in (
        "Sparkasse Musterstadt",
        "Stadtwerke Musterstadt GmbH",
        "EUR 25.000.000,00",
        "Kaufmännische Geschäftsführerin",
        "Prokurist",
        "§ 14",
    ):
        assert kept in text
    assert "Herrn [PERSON_1]" in masked[0]
    assert "Geschäftsführer [PERSON_1]" in masked[1]  # same name, same placeholder
    assert set(mapping.values()) == {
        "Max Mustermann",
        "Dr. Erika Beispiel",
        "Johanna Schmidt-Weber",
    }


def test_real_contracts_have_no_person_names(texts: dict[str, ContractText]) -> None:
    """The signature blocks carry roles only; nothing to mask, nothing lost."""
    assert all(t.masked == {} for t in texts.values())


# ── Quotes ───────────────────────────────────────────────────────────────────


def test_quote_exact_and_whitespace(texts: dict[str, ContractText]) -> None:
    v4 = texts["V4"]
    exact = "Eine Heilung durch Eigenkapitalzuführung nach dem Prüfungsstichtag ist ausgeschlossen."
    assert check_quote("S", exact, 7, v4).status == "ok"
    spaced = (
        "Die Eigenkapitalquote wird auf eine\n  Nachkommastelle, der Dynamische  Verschuldungsgrad"
    )
    assert check_quote("S", spaced, 7, v4).status == "ok"
    straight_quotes = '"Bilanzsumme" bezeichnet die Summe der Aktiva der Kreditnehmerin'
    assert check_quote("S", straight_quotes, 1, v4).status == "ok"


def test_quote_near_misses_and_missing(texts: dict[str, ContractText]) -> None:
    v4 = texts["V4"]
    wrong_page = check_quote("S", "Eine Heilung durch Eigenkapitalzuführung", 6, v4)
    assert (wrong_page.status, wrong_page.found_page) == ("wrong_page", 7)
    punct = check_quote(
        "S", "eine heilung durch eigenkapitalzuführung, nach dem Prüfungsstichtag", 7, v4
    )
    assert punct.status == "punctuation"
    assert punct.exact_text == "Eine Heilung durch Eigenkapitalzuführung nach dem Prüfungsstichtag"
    missing = check_quote("S", "Eine Heilung durch Eigenkapitalzuführung ist zulässig.", 7, v4)
    assert missing.status == "missing"
    assert missing.found_page == 7
    assert 0.5 < missing.similarity < 1


def test_gold_profiles_validate_against_the_pdfs(
    gold_profiles: list[ContractProfile], texts: dict[str, ContractText]
) -> None:
    for p in gold_profiles:
        report = validate_profile(p, texts[p.contract_id])
        assert report.ok, report.lines()
        assert text_findings(p, texts[p.contract_id]) == []


def test_repair_and_threshold_check(
    gold_profiles: list[ContractProfile], texts: dict[str, ContractText]
) -> None:
    v4 = by_id(gold_profiles, "V4")
    sources = [s.model_copy(update={"page": 6}) if s.id == "S_CURE" else s for s in v4.sources]
    broken = v4.model_copy(update={"sources": sources})
    report = validate_profile(broken, texts["V4"])
    assert [q.source_id for q in report.quote_errors] == ["S_CURE"]
    repaired, fixes = repair_quotes(broken, texts["V4"])
    assert len(fixes) == 1
    assert repaired.source("S_CURE").page == 7  # type: ignore[union-attr]

    cov = v4.covenants[0]
    wrong = cov.model_copy(
        update={
            "thresholds": [
                cov.thresholds[0].model_copy(update={"value": cov.thresholds[0].value + 1})
            ]
        }
    )
    report = validate_profile(
        v4.model_copy(update={"covenants": [wrong, v4.covenants[1]]}), texts["V4"]
    )
    assert report.thresholds
    assert "31.0" in report.thresholds[0]


# ── Completeness ─────────────────────────────────────────────────────────────


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
    v1 = by_id(gold_profiles, "V1")
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
    v4 = by_id(gold_profiles, "V4")
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
