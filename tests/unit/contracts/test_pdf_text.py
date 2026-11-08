"""Contract PDF text: page split with page numbers, cache by hash, person masking."""

import json
from pathlib import Path

import pytest

from finn.contracts import pdf_text
from finn.contracts.pdf_text import ContractText, mask_persons, render_pages
from mailkit import MATERIAL

V4_PDF = MATERIAL / "Vertrag_4_Konsortialkredit.pdf"


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
