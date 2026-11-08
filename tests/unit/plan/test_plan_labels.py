"""labels.py and units: normalisation, synonyms, headers, statements, loan numbers."""

import pytest

from finn.models.plan import ItemKey
from finn.plan.labels import (
    BLOCKS_BY_ITEM,
    LOAN_COLUMNS,
    Statement,
    loan_number,
    normalise,
    parse_year_header,
    statement_for,
)
from finn.plan.workbook import units_in_text


def test_normalise_drops_case_umlauts_enumerations_and_parentheses() -> None:
    assert normalise("B. Sonderposten für Investitions- und Baukostenzuschüsse") == (
        "sonderposten fuer investitions und baukostenzuschuesse"
    )
    assert normalise("  A.  Eigenkapital ") == "eigenkapital"
    assert normalise("Summe Aktiva (Bilanzsumme)") == "summe aktiva"
    expected = "verbindlichkeiten gegenueber kreditinstituten"
    assert normalise("Verb. ggü. Kreditinstituten") == expected


@pytest.mark.parametrize(
    ("item", "label", "score"),
    [
        (ItemKey.EQUITY, "A. Eigenkapital", 3),
        (ItemKey.EQUITY, "Eigenmittel", 3),
        (ItemKey.EQUITY, "Summe Eigenkapital", 3),
        (ItemKey.EQUITY, "Eigenkapitalquote", 0),
        (ItemKey.EQUITY, "Nicht durch Eigenkapital gedeckter Fehlbetrag", 0),
        (ItemKey.BANK_DEBT, "Bankdarlehen", 3),
        (ItemKey.BANK_DEBT, "Verbindlichkeiten ggü. Kreditinstituten", 3),
        (ItemKey.BANK_DEBT, "Kassenbestand und Guthaben bei Kreditinstituten", 0),
        (ItemKey.INCOME_TAXES, "Ergebnis vor Ertragsteuern", 0),
        (ItemKey.INCOME_TAXES, "Ertragsteuern", 3),
        (ItemKey.INTEREST_EXPENSE, "davon aus der Aufzinsung von Pensionsrückstellungen", 0),
        (ItemKey.INTEREST_EXPENSE, "Zinsen und ähnliche Aufwendungen", 3),
        (ItemKey.INTEREST_INCOME, "Zinsen und ähnliche Aufwendungen", 0),
        (ItemKey.DEPRECIATION, "Abschreibungen auf Finanzanlagen", 0),
        (ItemKey.DEPRECIATION, "Abschreibungen auf immaterielle Vermögensgegenstände", 2),
        (ItemKey.SONDERPOSTEN, "B. Sonderposten für Investitions- und Baukostenzuschüsse", 2),
        (ItemKey.NET_INCOME, "Jahresergebnis", 3),
        (ItemKey.NET_INCOME, "Betriebsergebnis", 0),
        (ItemKey.TOTAL_ASSETS, "Kontrolle: Aktiva – Passiva (muss 0 sein)", 0),
    ],
)
def test_block_scores(item: ItemKey, label: str, score: int) -> None:
    assert BLOCKS_BY_ITEM[item].score(label) == score


@pytest.mark.parametrize(
    ("text", "year", "is_plan", "is_actual"),
    [
        ("Plan 2027", 2027, True, False),
        ("31.12.2027 (Plan)", 2027, True, False),
        ("2027 (Plan)", 2027, True, False),
        ("Plan 31.12.2027", 2027, True, False),
        ("31.12.2027", 2027, False, False),
        ("Ist 2025", 2025, False, True),
        ("31.12.2026 (HR)", 2026, False, True),
    ],
)
def test_year_header(text: str, year: int, is_plan: bool, is_actual: bool) -> None:
    header = parse_year_header(text)
    assert header is not None
    assert (header.year, header.is_plan, header.is_actual) == (year, is_plan, is_actual)


@pytest.mark.parametrize("text", ["Position", "2026 vs. 2027", "Darlehen"])
def test_no_year_header(text: str) -> None:
    assert parse_year_header(text) is None


@pytest.mark.parametrize(
    ("text", "statement"),
    [
        ("GuV", Statement.GUV),
        ("Erfolgsplan", Statement.GUV),
        ("Planbilanz", Statement.BILANZ),
        ("Vermögensplan (Planbilanz zum 31.12.) – in T€", Statement.BILANZ),
        ("Darlehensspiegel", Statement.FINANZIERUNG),
        ("Finanzplan", Statement.FINANZIERUNG),
        ("Deckblatt", Statement.COVER),
        ("Notizen", None),
    ],
)
def test_statement_for(text: str, statement: Statement | None) -> None:
    assert statement_for(text) is statement


@pytest.mark.parametrize(
    ("label", "number"),
    [
        ("Investitionsdarlehen Sparkasse Musterstadt (Nr. 4471 203 118)", "4471 203 118"),
        ("Förderdarlehen Förderbank Musterland (Nr. FD-2022-08817)", "FD-2022-08817"),
        ("Investitionsdarlehen Landesbank Mittelland (Nr. LBM-INV-2025-0412)", "LBM-INV-2025-0412"),
        ("Darlehen LBM-INV-2025-0412 Tranche A", "LBM-INV-2025-0412"),
        ("Konsortialkredit (Agent: Weserland Bank AG)", None),
        ("Altdarlehen (Sammelposition, 5 Annuitätendarlehen, Abschluss vor 2019)", None),
    ],
)
def test_loan_number(label: str, number: str | None) -> None:
    assert loan_number(label) == number


@pytest.mark.parametrize(
    ("header", "field"),
    [
        ("Stand 01.01.2027", "opening"),
        ("Auszahlungen 2027", "drawdown"),
        ("Planmäßige Tilgung 2027", "scheduled_repayment"),
        ("Sondertilgung 2027", "special_repayment"),
        ("Stand 31.12.2027", "closing"),
        ("Zinsaufwand 2027", "interest"),
    ],
)
def test_loan_columns(header: str, field: str) -> None:
    assert [c.field for c in LOAN_COLUMNS if c.matches(header)][:1] == [field]


@pytest.mark.parametrize(
    ("text", "units"),
    [
        ("Erfolgsplan – in T€", {"T€"}),
        ("Alle Beträge in T€ (Tausend Euro)", {"T€"}),
        ("Werte in TEUR", {"T€"}),
        ("in Tsd. €", {"T€"}),
        ("Darlehensspiegel – in €", {"€"}),
        ("Angaben in EUR", {"€"}),
        ("in Mio. €", {"Mio. €"}),
        ("Darlehensspiegel (Planwerte)", set()),
        ("in T€, Finanzierung in €", {"T€", "€"}),  # conflicting -> unclear
    ],
)
def test_units_in_text(text: str, units: set[str]) -> None:
    assert units_in_text(text) == units
