"""Profile validation against the contract text: quotes, repair, thresholds."""

from finn.contracts.pdf_text import ContractText
from finn.contracts.validate import check_quote, repair_quotes, text_findings, validate_profile
from finn.models.contract import ContractProfile
from mailkit import profile_by_id


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
    v4 = profile_by_id(gold_profiles, "V4")
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
