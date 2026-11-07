"""Deterministic completeness scan (D4, ARCHITECTURE §2.1): covenant signal words in the contract
text must all be accounted for by the profile.

The page texts are split into clauses (sentences, list items). A clause is a **hit** if it names a
financial ratio (Eigenkapitalquote, Verschuldungsgrad, DSCR / Schuldendienstdeckungsgrad, EBITDA,
Finanzkennzahl, Covenant, …) or states a bound on a percentage / ratio ("mindestens",
"nicht mehr als", "nicht weniger als", "höchstens" … + number).

A hit is **covered** when
1. it overlaps a quoted source of the profile on the same page (covenant, definition, condition,
   pricing grid, example, dismissed mention, no-covenant reason: every source is cited by one of
   them), or
2. it only refers back to what the profile already knows: every term it names is a covenant /
   definition of the profile, and every number it states is a threshold or a number quoted in a
   source (e.g. the compliance-certificate template "Eigenkapitalquote (Grenzwert: mind. 30,0 %)").

An uncovered hit is a finding: the extraction may have missed a covenant or a modifying clause,
or a numeric mention that should be dismissed with a reason.
"""

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from difflib import SequenceMatcher

from finn.contracts.pdf_text import ContractText, normalize
from finn.models.contract import ContractProfile
from finn.numbers import to_decimal

# Ratio / covenant vocabulary (case-insensitive, also inside compounds).
_TERMS: dict[str, str] = {
    "eigenkapitalquote": r"eigenkapitalquote",
    "eigenmittelquote": r"eigenmittelquote",
    "verschuldungsgrad": r"verschuldungsgrad",
    "schuldendienstdeckungsgrad": r"schuldendienstdeckungsgrad|kapitaldienstdeckung",
    "dscr": r"\bdscr\b|debt service coverage",
    "zinsdeckungsgrad": r"zinsdeckung",
    "ebitda": r"\bebitda\b",
    "finanzkennzahl": r"finanzkennzahl|kennzahl",
    "covenant": r"covenant",
    "leverage": r"\bleverage\b|\bgearing\b",
}
_TERM_RE = {k: re.compile(v, re.IGNORECASE) for k, v in _TERMS.items()}
_BOUND = re.compile(
    r"(?:mindestens|nicht\s+mehr\s+als|nicht\s+weniger\s+als|höchstens|maximal|mind\.|max\.|"
    r"unterschreit\w*|überschreit\w*|kleiner\s+(?:oder\s+gleich|als)|größer\s+(?:oder\s+gleich|als))"
    r"[^.;:]{0,60}?(?P<num>\d+(?:,\d+)?)\s*(?P<pct>%|Prozent)?",
    re.IGNORECASE,
)
_NUMBER = re.compile(r"\d{1,3}(?:\.\d{3})+(?:,\d+)?|\d+(?:,\d+)?")
# Clause boundaries: after "." / ";" before a capital or a list marker, before "a) " / "(1) ".
_CLAUSE_END = re.compile(r"(?<=[.;])\s+(?=[A-ZÄÖÜ(\"]|[a-z]\)\s)|\s+(?=[a-z]\)\s)|\s+(?=\(\d+\)\s)")
_PAGE_FOOTER = re.compile(r"Seite \d+ von \d+.*$", re.MULTILINE)


@dataclass(frozen=True)
class Hit:
    page: int
    terms: tuple[str, ...]  # matched vocabulary keys; "bound" for a numeric bound
    snippet: str
    numbers: tuple[Decimal, ...]
    covered_by: str | None  # source id, "known" (refers back) or None

    @property
    def covered(self) -> bool:
        return self.covered_by is not None

    def describe(self) -> str:
        text = self.snippet if len(self.snippet) <= 160 else self.snippet[:157] + "…"
        return f"S. {self.page} [{', '.join(self.terms)}]: {text}"


def clauses(page_text: str) -> list[str]:
    text = normalize(_PAGE_FOOTER.sub("", page_text))
    return [c.strip() for c in _CLAUSE_END.split(text) if c.strip()]


def find_hits(text: ContractText) -> list[tuple[int, str, tuple[str, ...]]]:
    """(page, clause, terms) for every clause with a covenant signal."""
    out: list[tuple[int, str, tuple[str, ...]]] = []
    for number, page_text in enumerate(text.pages, 1):
        for clause in clauses(page_text):
            terms = tuple(k for k, rx in _TERM_RE.items() if rx.search(clause))
            if _bound_numbers(clause):
                terms = (*terms, "bound")
            if terms:
                out.append((number, clause, terms))
    return out


def _bound_numbers(clause: str) -> list[Decimal]:
    """Numbers bounded by a comparator that look like a ratio or percentage (not EUR amounts)."""
    found: list[Decimal] = []
    for m in _BOUND.finditer(clause):
        after = clause[m.end() : m.end() + 12]
        if re.match(r"\s*(?:Tage|Bankarbeitstag|Monat|Jahr|Wochen|Mal)", after, re.IGNORECASE):
            continue
        if m["pct"] or "," in m["num"]:
            found.append(to_decimal(m["num"]))
    return found


def scan(profile: ContractProfile, text: ContractText) -> list[Hit]:
    spans = _source_spans(profile, text)
    known_numbers = _known_numbers(profile)
    known_terms = _known_terms(profile)
    hits: list[Hit] = []
    for page, clause, terms in find_hits(text):
        numbers = tuple(_numbers(clause))
        covered = _overlapping_source(page, clause, spans)
        if covered is None and _refers_back(terms, clause, numbers, known_terms, known_numbers):
            covered = "known"
        hits.append(Hit(page, terms, clause, numbers, covered))
    return hits


def uncovered(profile: ContractProfile, text: ContractText) -> list[Hit]:
    return [h for h in scan(profile, text) if not h.covered]


def _source_spans(profile: ContractProfile, text: ContractText) -> dict[int, list[tuple[str, str]]]:
    spans: dict[int, list[tuple[str, str]]] = {}
    for s in profile.sources:
        spans.setdefault(s.page, []).append((s.id, normalize(s.quote)))
    return spans


def _overlapping_source(
    page: int, clause: str, spans: dict[int, list[tuple[str, str]]]
) -> str | None:
    """A source quote on this page that contains the clause's signal part, or vice versa."""
    for source_id, quote in spans.get(page, []):
        if clause in quote or quote in clause or _shared_run(clause, quote) >= 40:
            return source_id
    return None


def _shared_run(a: str, b: str) -> int:
    m = SequenceMatcher(None, a, b, autojunk=False).find_longest_match(0, len(a), 0, len(b))
    return m.size


def _refers_back(
    terms: tuple[str, ...],
    clause: str,
    numbers: tuple[Decimal, ...],
    known_terms: set[str],
    known_numbers: set[Decimal],
) -> bool:
    if not known_terms:
        return False  # no covenants: every signal needs a source (dismissed mention)
    named = [t for t in terms if t != "bound"]
    if any(t not in known_terms for t in named):
        return False
    bounds = _bound_numbers(clause)
    relevant = [n for n in numbers if n in bounds or "%" in clause or n < 100]
    return all(n in known_numbers for n in relevant if n not in _ordinal_like(clause))


def _ordinal_like(clause: str) -> set[Decimal]:
    """Section / paragraph / list numbers and years ("§ 14", "Absatz 1", "2027")."""
    out: set[Decimal] = set()
    for m in re.finditer(
        r"(?:§+|Abs\.|Absatz|Nr\.|Anlage|Ziffer|lit\.)\s*(\d+)|\((\d+)\)|\b(\d{4})\b"
        r"|(?:^|\s)(\d{1,2})\.(?=\s|$)",
        clause,
    ):
        out.add(Decimal(next(g for g in m.groups() if g)))
    return out


def _known_terms(profile: ContractProfile) -> set[str]:
    """Vocabulary keys the profile covers (from covenant names, metric types and definitions)."""
    words = " ".join(
        [c.name_in_contract for c in profile.covenants]
        + [c.metric_type for c in profile.covenants]
        + [d.term or k for k, d in profile.definitions.items()]
    ).lower()
    known = {k for k, rx in _TERM_RE.items() if rx.search(words)}
    metric_terms = {
        "dscr": {"dscr", "schuldendienstdeckungsgrad"},
        "net_debt_to_ebitda": {"verschuldungsgrad", "ebitda", "leverage"},
        "equity_ratio": {"eigenkapitalquote"},
        "economic_equity_ratio": {"eigenkapitalquote"},
    }
    for c in profile.covenants:
        known |= metric_terms.get(c.metric_type, set())
    if profile.covenants:
        known |= {"finanzkennzahl", "covenant"}
    return known


def _known_numbers(profile: ContractProfile) -> set[Decimal]:
    numbers = {t.value for c in profile.covenants for t in c.thresholds}
    for s in profile.sources:
        numbers |= set(_numbers(s.quote))
    if profile.pricing_grid:
        numbers |= {s.upper for s in profile.pricing_grid.steps if s.upper is not None}
    return numbers


def _numbers(text: str) -> list[Decimal]:
    out: list[Decimal] = []
    for m in _NUMBER.finditer(text):
        try:
            out.append(to_decimal(m[0]))
        except (ValueError, InvalidOperation):
            continue
    return out
