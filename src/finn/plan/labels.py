"""Label knowledge: synonyms per building block, plan-year headers, statement detection.

Normalised matching; score exact 3 > starts with 2 > contains 1; `excludes` disqualify a label."""

import re
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Literal

from finn.models.plan import ItemKey


class Statement(StrEnum):
    COVER = "cover"
    GUV = "guv"
    BILANZ = "bilanz"
    FINANZIERUNG = "finanzierung"


Section = Literal["aktiva", "passiva"]
# How a block's sign is normalised: magnitudes become positive, signed blocks keep their sign.
SignKind = Literal["expense", "income", "signed", "asset", "liability", "equity"]

_UMLAUTS = str.maketrans({"ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss"})
_ENUM_PREFIX = re.compile(r"^\s*((?:[a-h]|[ivx]+|\d+)\s*[.)]\s*)+")
_PARENS = re.compile(r"\([^)]*\)")
# Applied after punctuation is removed ("ggü." -> "ggue").
_ABBREVIATIONS = [
    (re.compile(r"\bgg(ue|u)?\b"), "gegenueber"),
    (re.compile(r"\bverb\b"), "verbindlichkeiten"),
]


def normalise(text: str, *, keep_parens: bool = False) -> str:
    """'B. Sonderposten für Investitions- und …' -> 'sonderposten fuer investitions und …'."""
    s = text.casefold().translate(_UMLAUTS)
    s = _ENUM_PREFIX.sub("", s)
    if not keep_parens:
        s = _PARENS.sub(" ", s)
    s = re.sub(r"[^\w§]+", " ", s)
    for pattern, replacement in _ABBREVIATIONS:
        s = pattern.sub(replacement, s)
    return re.sub(r"\s+", " ", s).strip()


@dataclass(frozen=True)
class BlockSpec:
    item: ItemKey
    statement: Statement
    label_de: str  # used in German messages
    synonyms: tuple[str, ...]
    sign: SignKind
    excludes: tuple[str, ...] = ()
    section: Section | None = None
    optional: bool = False  # usually absent; the recipe decides (if_missing)
    norm_synonyms: tuple[str, ...] = field(init=False)
    norm_excludes: tuple[str, ...] = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "norm_synonyms", tuple(normalise(s) for s in self.synonyms))
        object.__setattr__(self, "norm_excludes", tuple(normalise(s) for s in self.excludes))

    def score(self, label: str) -> int:
        """0 = no match; 3 exact, 2 prefix, 1 contained."""
        norm = normalise(label)
        full = normalise(label, keep_parens=True)
        if any(re.search(rf"\b{re.escape(x)}\b", full) for x in self.norm_excludes):
            return 0
        best = 0
        for syn in self.norm_synonyms:
            if norm == syn:
                return 3
            if norm.startswith(syn + " "):
                best = max(best, 2)
            elif re.search(rf"\b{re.escape(syn)}\b", norm):
                best = max(best, 1)
        return best


G, B = Statement.GUV, Statement.BILANZ

BLOCKS: tuple[BlockSpec, ...] = (
    # ── GuV ──
    BlockSpec(
        ItemKey.NET_INCOME, G, "Jahresüberschuss",
        ("Jahresüberschuss", "Jahresfehlbetrag", "Jahresüberschuss/-fehlbetrag",
         "Jahresüberschuss Jahresfehlbetrag", "Jahresergebnis", "Ergebnis nach Steuern",
         "Periodenergebnis"),
        "signed", excludes=("vor",),
    ),
    BlockSpec(
        ItemKey.INCOME_TAXES, G, "Steuern vom Einkommen und vom Ertrag",
        ("Steuern vom Einkommen und vom Ertrag", "Steuern vom Einkommen und Ertrag",
         "Ertragsteuern", "Ertragssteuern", "Ertragsteueraufwand"),
        "expense", excludes=("vor", "ergebnis", "nach", "sonstige steuern"),
    ),
    BlockSpec(
        ItemKey.INTEREST_EXPENSE, G, "Zinsen und ähnliche Aufwendungen",
        ("Zinsen und ähnliche Aufwendungen", "Zinsaufwand", "Zinsaufwendungen",
         "Zinsen und ähnliche Aufwand"),
        "expense", excludes=("davon", "ertraege", "ertrag", "aufzinsung"),
    ),
    BlockSpec(
        ItemKey.PENSION_UNWINDING, G, "Aufzinsung Pensionsrückstellungen",
        ("davon aus der Aufzinsung von Pensionsrückstellungen", "Aufzinsung von Rückstellungen",
         "Aufzinsung Pensionsrückstellungen", "Aufzinsung von Pensionsrückstellungen",
         "Aufzinsung"),
        "expense",
    ),
    BlockSpec(
        ItemKey.INTEREST_INCOME, G, "Zinserträge",
        ("Sonstige Zinsen und ähnliche Erträge", "Zinsen und ähnliche Erträge", "Zinserträge",
         "Zinsertrag"),
        "income", excludes=("aufwendungen", "aufwand", "davon"),
    ),
    BlockSpec(
        ItemKey.DEPRECIATION, G, "Abschreibungen",
        ("Abschreibungen", "Abschreibungen auf immaterielle Vermögensgegenstände und Sachanlagen",
         "Abschreibungen auf Sachanlagen", "AfA", "planmäßige Abschreibungen"),
        "expense", excludes=("finanzanlagen", "wertpapiere", "umlaufvermoegen", "davon"),
    ),
    BlockSpec(
        ItemKey.EXTRAORDINARY_ITEMS, G, "Außerordentliches Ergebnis",
        ("Außerordentliches Ergebnis", "außerordentliche Erträge und Aufwendungen",
         "außergewöhnliche Posten"),
        "signed", optional=True,
    ),
    # ── Bilanz, Aktiva ──
    BlockSpec(
        ItemKey.TOTAL_ASSETS, B, "Summe Aktiva",
        ("Summe Aktiva", "Bilanzsumme", "Aktiva gesamt", "Summe der Aktiva", "Aktiva insgesamt",
         "Summe Vermögen"),
        "asset", section="aktiva",
    ),
    BlockSpec(
        ItemKey.CASH, B, "Kassenbestand und Guthaben bei Kreditinstituten",
        ("Kassenbestand und Guthaben bei Kreditinstituten", "Kassenbestand, Guthaben bei "
         "Kreditinstituten", "Flüssige Mittel", "Liquide Mittel", "Zahlungsmittel",
         "Guthaben bei Kreditinstituten", "Kasse und Bank", "Bankguthaben", "Kassenbestand"),
        "asset", section="aktiva",
    ),
    BlockSpec(
        ItemKey.CALLED_UNPAID_CAPITAL, B, "Eingeforderte, nicht eingezahlte Einlagen",
        ("eingeforderte noch nicht eingezahlte Einlagen", "eingefordertes nicht eingezahltes "
         "Kapital", "eingeforderte ausstehende Einlagen"),
        "asset", optional=True,
    ),
    # ── Bilanz, Passiva ──
    BlockSpec(
        ItemKey.TOTAL_EQUITY_LIABILITIES, B, "Summe Passiva",
        ("Summe Passiva", "Passiva gesamt", "Summe der Passiva", "Passiva insgesamt",
         "Bilanzsumme", "Summe Kapital"),
        "liability", section="passiva",
    ),
    BlockSpec(
        ItemKey.EQUITY, B, "Eigenkapital",
        ("Eigenkapital", "Summe Eigenkapital", "Eigenkapital gesamt", "Eigenmittel",
         "Summe Eigenmittel"),
        "equity", section="passiva",
        excludes=("quote", "eigenkapitalquote", "fehlbetrag", "davon"),
    ),
    BlockSpec(
        ItemKey.SONDERPOSTEN, B, "Sonderposten",
        ("Sonderposten", "Sonderposten für Investitionszuschüsse", "Sonderposten für Zuschüsse",
         "Baukostenzuschüsse", "Ertragszuschüsse", "Netzanschlusskostenbeiträge",
         "Investitionszuschüsse"),
        "liability", section="passiva", excludes=("ruecklageanteil",),
    ),
    BlockSpec(
        ItemKey.BANK_DEBT, B, "Verbindlichkeiten gegenüber Kreditinstituten",
        ("Verbindlichkeiten gegenüber Kreditinstituten", "Verbindlichkeiten gegenüber Banken",
         "Bankverbindlichkeiten", "Bankdarlehen", "Kreditinstitute", "Darlehen von Banken",
         "Bankkredite"),
        "liability", section="passiva", excludes=("guthaben",),
    ),
    BlockSpec(
        ItemKey.SHAREHOLDER_LOAN, B, "Gesellschafterdarlehen",
        ("Verbindlichkeiten gegenüber Gesellschafter", "Verbindlichkeiten gegenüber "
         "Gesellschaftern", "Gesellschafterdarlehen", "Darlehen Gesellschafter",
         "Darlehen des Gesellschafters"),
        "liability", section="passiva",
    ),
    BlockSpec(
        ItemKey.BONDS, B, "Anleihen",
        ("Anleihen", "Schuldverschreibungen", "Schuldscheindarlehen"),
        "liability", section="passiva", optional=True,
    ),
    BlockSpec(
        ItemKey.FINANCE_LEASES, B, "Leasingverbindlichkeiten",
        ("Leasingverbindlichkeiten", "Verbindlichkeiten aus Finanzierungsleasing",
         "Finanzierungsleasing", "Verbindlichkeiten aus Leasing"),
        "liability", section="passiva", optional=True,
    ),
    BlockSpec(
        ItemKey.RECOURSE_FACTORING, B, "Factoring mit Rückgriff",
        ("Factoring mit Rückgriff", "unechtes Factoring", "Verbindlichkeiten aus Factoring"),
        "liability", section="passiva", optional=True,
    ),
)  # fmt: skip

BLOCKS_BY_ITEM: dict[ItemKey, BlockSpec] = {b.item: b for b in BLOCKS}

# Finanzierung aggregates (from LoanLines) with their German labels.
FINANCING_ITEMS: dict[ItemKey, str] = {
    ItemKey.SCHEDULED_REPAYMENTS_TOTAL: "Planmäßige Tilgungen (Darlehensspiegel)",
    ItemKey.SPECIAL_REPAYMENTS_TOTAL: "Sondertilgungen (Darlehensspiegel)",
    ItemKey.TOTAL_BANK_DEBT: "Darlehen Kreditinstitute, Stand Jahresende (Darlehensspiegel)",
    ItemKey.INTEREST_ON_FINANCIAL_DEBT: "Zinsaufwand je Darlehen (Darlehensspiegel)",
}


def label_de(item: ItemKey) -> str:
    if item in FINANCING_ITEMS:
        return FINANCING_ITEMS[item]
    return BLOCKS_BY_ITEM[item].label_de


# ── Statements ───────────────────────────────────────────────────────────────

_STATEMENT_NAMES: list[tuple[Statement, tuple[str, ...]]] = [
    (Statement.COVER, ("deckblatt", "cover", "titel", "titelblatt", "uebersicht")),
    (Statement.FINANZIERUNG, ("finanzierung", "darlehensspiegel", "finanzplan",
                              "finanzierungsplan", "darlehen", "kreditspiegel")),
    (Statement.GUV, ("guv", "erfolgsplan", "gewinn und verlust", "gewinn und verlustrechnung",
                     "erfolgsrechnung", "ergebnisplan")),
    (Statement.BILANZ, ("bilanz", "planbilanz", "vermoegensplan", "vermoegensuebersicht")),
]  # fmt: skip


def statement_for(text: str, *, allow_cover: bool = True) -> Statement | None:
    """Statement named by a sheet name or a sheet title ("Planbilanz" -> BILANZ)."""
    norm = normalise(text, keep_parens=True)
    for statement, names in _STATEMENT_NAMES:
        if statement is Statement.COVER and not allow_cover:
            continue
        if any(re.search(rf"\b{re.escape(n)}", norm) for n in names):
            return statement
    return None


# ── Column headers ───────────────────────────────────────────────────────────

_YEAR = re.compile(r"(?<!\d)((?:19|20)\d{2})(?!\d)")
_PLAN_MARKERS = re.compile(r"\b(plan|planwerte?|wp|budget|soll)\b")
_ACTUAL_MARKERS = re.compile(
    r"\b(ist|hr|hochrechnung|forecast|fc|vorjahr|vj|erwartet|testiert|vorschau)\b"
)


@dataclass(frozen=True)
class YearHeader:
    year: int
    is_plan: bool  # "Plan 2027", "31.12.2027 (Plan)", "2027 (Plan)"
    is_actual: bool  # "Ist 2025", "31.12.2026 (HR)"


def parse_year_header(text: str) -> YearHeader | None:
    """Year column header, or None if the text names no single year."""
    norm = normalise(text, keep_parens=True)
    years = {int(y) for y in _YEAR.findall(norm)}
    if len(years) != 1:
        return None
    return YearHeader(
        year=years.pop(),
        is_plan=bool(_PLAN_MARKERS.search(norm)),
        is_actual=bool(_ACTUAL_MARKERS.search(norm)),
    )


def years_in(text: str) -> list[int]:
    return [int(y) for y in _YEAR.findall(text)]


# ── Loan schedule columns (Finanzierung) ─────────────────────────────────────

LoanField = Literal[
    "opening", "drawdown", "scheduled_repayment", "special_repayment", "closing", "interest"
]


@dataclass(frozen=True)
class ColumnSpec:
    field: LoanField
    patterns: tuple[re.Pattern[str], ...]
    excludes: tuple[re.Pattern[str], ...] = ()

    def matches(self, header: str) -> bool:
        norm = normalise(header, keep_parens=True)
        if any(x.search(norm) for x in self.excludes):
            return False
        return any(p.search(norm) for p in self.patterns)


def _rx(*patterns: str) -> tuple[re.Pattern[str], ...]:
    return tuple(re.compile(p) for p in patterns)


LOAN_COLUMNS: tuple[ColumnSpec, ...] = (
    ColumnSpec("opening", _rx(r"\bstand 0?1 0?1\b", r"\banfangs(bestand|stand|saldo)\b",
                              r"\bstand (jahres)?(anfang|beginn)\b", r"\bsaldo 0?1 0?1\b",
                              r"\bvortrag\b")),
    ColumnSpec("drawdown", _rx(r"\bauszahlung", r"\bneuaufnahme", r"\bvalutierung",
                               r"\bzugang", r"\bziehung")),
    ColumnSpec("special_repayment", _rx(r"\bsondertilgung", r"\bausserplanmaessige tilgung",
                                        r"\bvorzeitige tilgung", r"\bvorfaelligkeit")),
    ColumnSpec("scheduled_repayment", _rx(r"\bplanmaessige tilgung", r"\bregeltilgung",
                                          r"\btilgung"),
               excludes=_rx(r"sonder", r"ausserplanmaessig", r"vorzeitig")),
    ColumnSpec("closing", _rx(r"\bstand 31 12\b", r"\bend(bestand|stand|saldo)\b",
                              r"\bstand (jahres)?ende\b", r"\bsaldo 31 12\b")),
    ColumnSpec("interest", _rx(r"\bzins(aufwand|aufwendungen|en)?\b"),
               excludes=_rx(r"\bzinssatz\b", r"\bp a\b", r"%")),
)  # fmt: skip

_TOTAL_ROW = re.compile(r"\b(summe|gesamt|insgesamt|total|abgleich|kontrolle|zwischensumme)\b")
_NOTES_START = re.compile(r"^(erlaeuterungen|hinweise?|anmerkungen|fussnoten)\b")
_SHAREHOLDER = re.compile(r"\bgesellschafter")
_BANK_SECTION = re.compile(r"\b(kreditinstitut|banken|bankdarlehen)")
_OTHER_SECTION = re.compile(r"\b(sonstige darlehen|sonstige finanzierung|gesellschafter|uebrige)")


def is_total_row(label: str) -> bool:
    return bool(_TOTAL_ROW.search(normalise(label, keep_parens=True)))


def is_notes_heading(label: str) -> bool:
    return bool(_NOTES_START.match(normalise(label)))


def loan_section(label: str) -> Literal["bank", "other"] | None:
    """Section heading in the loan schedule ("Verbindlichkeiten gegenüber Kreditinstituten")."""
    norm = normalise(label)
    if _OTHER_SECTION.search(norm):
        return "other"
    if _BANK_SECTION.search(norm):
        return "bank"
    return None


def is_shareholder_label(label: str) -> bool:
    return bool(_SHAREHOLDER.search(normalise(label, keep_parens=True)))


# Loan numbers: "Nr. 4471 203 118", "Nr. FD-2022-08817", "LBM-INV-2025-0412".
_LOAN_NR = re.compile(
    r"\b(?:Nr|Nummer|Konto(?:-?Nr)?|Darlehensnr|Vertragsnr)\.?\s*[:.]?\s*"
    r"([A-Z0-9](?:[A-Z0-9./-]|\s(?=\d))*[A-Z0-9])",
    re.IGNORECASE,
)
_LOAN_ID = re.compile(r"\b([A-Z]{2,}(?:-[A-Z0-9]+)+)\b")


def loan_number(label: str) -> str | None:
    """Loan number from a loan-schedule label, if any."""
    for pattern in (_LOAN_NR, _LOAN_ID):
        for match in pattern.finditer(label):
            candidate = match.group(1).strip()
            if any(ch.isdigit() for ch in candidate):
                return candidate
    return None


def normalise_loan_number(number: str) -> str:
    return re.sub(r"[\s./-]", "", number).upper()
