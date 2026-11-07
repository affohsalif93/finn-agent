"""Onboarding validation of a (draft) profile against the contract text (ARCHITECTURE §2.1, D4).

- every `sources[]` quote exists **verbatim** on its stated page (whitespace normalised; typographic
  quotes and dashes unified). Near misses are reported with the page where the text was found
  (`wrong_page`) or as `punctuation` when only punctuation / case differ;
- recipe whitelist and structure (`calc/recipe.py`, the same check the store runs at load time);
- thresholds parse: every threshold value appears as a number in the covenant's source quote;
- page numbers within the document, the contract's printed examples reproduce (selftest).

`repair_quotes` turns near misses into exact quotes (page corrected, text copied from the page),
so a draft never carries a quote that is not in the contract.
"""

import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from difflib import SequenceMatcher
from typing import Literal

from finn.calc.recipe import recipe_issues
from finn.contracts import completeness
from finn.contracts.pdf_text import ContractText, normalize
from finn.contracts.selftest import check_examples
from finn.errors import ErrorCode, Finding, finding
from finn.models.contract import ContractProfile, Source
from finn.numbers import to_decimal

QuoteStatus = Literal["ok", "wrong_page", "punctuation", "missing"]

_NUMBER = re.compile(r"\d{1,3}(?:\.\d{3})+(?:,\d+)?|\d+(?:,\d+)?")


def _loose_with_map(text: str) -> tuple[str, list[int]]:
    """Letters and digits only, casefolded, with the index of each kept char in `text`."""
    chars: list[str] = []
    index: list[int] = []
    for i, ch in enumerate(text):
        if ch.isalnum():
            folded = ch.casefold()
            chars.append(folded)
            index.extend([i] * len(folded))
    return "".join(chars), index


@dataclass(frozen=True)
class QuoteCheck:
    source_id: str
    page: int
    status: QuoteStatus
    found_page: int | None = None
    similarity: float = 0.0  # best fuzzy ratio on any page (for `missing`)
    exact_text: str | None = None  # the page text matching a near miss

    @property
    def ok(self) -> bool:
        return self.status == "ok"

    def describe(self) -> str:
        if self.status == "ok":
            return f"{self.source_id}: ok (S. {self.page})"
        if self.status == "wrong_page":
            return f"{self.source_id}: steht auf S. {self.found_page}, nicht S. {self.page}"
        if self.status == "punctuation":
            return (
                f"{self.source_id}: nur Zeichensetzung/Schreibweise abweichend "
                f"(S. {self.found_page})"
            )
        where = f" (ähnlichster Text S. {self.found_page})" if self.found_page else ""
        return (
            f"{self.source_id}: Zitat nicht im Vertrag gefunden, "
            f"Ähnlichkeit {self.similarity:.0%}{where}"
        )


def check_quote(source_id: str, quote: str, page: int, text: ContractText) -> QuoteCheck:
    q = normalize(quote)
    pages = [normalize(p) for p in text.pages]
    order = [page, *[n for n in range(1, len(pages) + 1) if n != page]]
    order = [n for n in order if 1 <= n <= len(pages)]
    for n in order:
        if q and q in pages[n - 1]:
            status: QuoteStatus = "ok" if n == page else "wrong_page"
            return QuoteCheck(source_id, page, status, found_page=n, similarity=1.0, exact_text=q)
    loose_q, _ = _loose_with_map(q)
    for n in order:
        loose_p, index = _loose_with_map(pages[n - 1])
        pos = loose_p.find(loose_q) if loose_q else -1
        if pos >= 0:
            start, end = index[pos], index[pos + len(loose_q) - 1] + 1
            exact = _extend_to_punctuation(pages[n - 1], start, end, q)
            return QuoteCheck(source_id, page, "punctuation", n, 1.0, exact_text=exact)
    best_ratio, best_page = 0.0, None
    for n in order:
        ratio = _best_window_ratio(q, pages[n - 1])
        if ratio > best_ratio:
            best_ratio, best_page = ratio, n
    return QuoteCheck(source_id, page, "missing", found_page=best_page, similarity=best_ratio)


def _extend_to_punctuation(page: str, start: int, end: int, quote: str) -> str:
    """Keep trailing / leading punctuation the quote had (e.g. a closing '“' or '.')."""
    if quote and not quote[-1].isalnum() and end < len(page) and not page[end].isalnum():
        end += 1
    if quote and not quote[0].isalnum() and start > 0 and not page[start - 1].isalnum():
        start -= 1
    return page[start:end].strip()


def _best_window_ratio(quote: str, page: str) -> float:
    if not quote or not page:
        return 0.0
    matcher = SequenceMatcher(None, page, quote, autojunk=False)
    match = matcher.find_longest_match(0, len(page), 0, len(quote))
    lo = max(0, match.a - match.b)
    window = page[lo : lo + len(quote) + 20]
    return SequenceMatcher(None, window, quote, autojunk=False).ratio()


def check_quotes(profile: ContractProfile, text: ContractText) -> list[QuoteCheck]:
    return [check_quote(s.id, s.quote, s.page, text) for s in profile.sources]


def repair_quotes(
    profile: ContractProfile, text: ContractText
) -> tuple[ContractProfile, list[str]]:
    """Fix near misses (wrong page, punctuation only) with the exact page text; list the fixes."""
    fixes: list[str] = []
    sources: list[Source] = []
    for source, check in zip(profile.sources, check_quotes(profile, text), strict=True):
        if check.status in ("wrong_page", "punctuation") and check.exact_text:
            fixes.append(check.describe())
            sources.append(
                source.model_copy(update={"page": check.found_page, "quote": check.exact_text})
            )
        else:
            sources.append(source)
    if not fixes:
        return profile, []
    return profile.model_copy(update={"sources": sources}), fixes


# ── Whole-profile validation ─────────────────────────────────────────────────


@dataclass
class ValidationReport:
    contract_id: str
    quotes: list[QuoteCheck] = field(default_factory=list)
    recipe: list[str] = field(default_factory=list)
    thresholds: list[str] = field(default_factory=list)
    pages: list[str] = field(default_factory=list)
    examples: list[str] = field(default_factory=list)

    @property
    def quote_errors(self) -> list[QuoteCheck]:
        return [q for q in self.quotes if not q.ok]

    @property
    def ok(self) -> bool:
        return not (
            self.quote_errors or self.recipe or self.thresholds or self.pages or self.examples
        )

    def lines(self) -> list[str]:
        ok_quotes = len(self.quotes) - len(self.quote_errors)
        out = [f"Zitate: {ok_quotes}/{len(self.quotes)} wörtlich auf der angegebenen Seite"]
        out += [f"  Zitat {q.describe()}" for q in self.quote_errors]
        out += [f"  Rechenvorschrift: {m}" for m in self.recipe]
        out += [f"  Grenzwert: {m}" for m in self.thresholds]
        out += [f"  Seite: {m}" for m in self.pages]
        out += [f"  Beispiel: {m}" for m in self.examples]
        return out


def validate_profile(profile: ContractProfile, text: ContractText) -> ValidationReport:
    report = ValidationReport(profile.contract_id)
    report.quotes = check_quotes(profile, text)
    report.recipe = [f"{i.covenant_id or '(Profil)'}: {i.message}" for i in recipe_issues(profile)]
    report.pages = [
        f"{s.id}: Seite {s.page} > {text.page_count}"
        for s in profile.sources
        if s.page > text.page_count
    ]
    if profile.document.pages != text.page_count:
        report.pages.append(
            f"document.pages = {profile.document.pages}, PDF hat {text.page_count} Seiten"
        )
    for cov in profile.covenants:
        source = profile.source(cov.source_id)
        numbers = _numbers(source.quote) if source else set()
        for t in cov.thresholds:
            if t.value not in numbers:
                report.thresholds.append(
                    f"{cov.covenant_id}: Grenzwert {t.value} steht nicht im Zitat {cov.source_id}"
                )
    report.examples = [f.message_de for f in check_examples(profile)]
    return report


def _numbers(text: str) -> set[Decimal]:
    out: set[Decimal] = set()
    for m in _NUMBER.finditer(text):
        try:
            out.add(to_decimal(m[0]))
        except (ValueError, InvalidOperation):
            continue
    return out


def text_findings(profile: ContractProfile, text: ContractText) -> list[Finding]:
    """Onboarding findings against the contract text: quotes (D4) and the completeness scan."""
    report = validate_profile(profile, text)
    out = [
        finding(ErrorCode.PROFILE_QUOTE_MISMATCH, contract=profile.contract_id, detail=q.describe())
        for q in report.quote_errors
    ]
    out += [
        finding(ErrorCode.PROFILE_INCOMPLETE, contract=profile.contract_id, detail=h.describe())
        for h in completeness.uncovered(profile, text)
    ]
    return out
