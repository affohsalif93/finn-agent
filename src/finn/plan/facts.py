"""Plan facts by pattern rules on note texts (ARCHITECTURE §4.1). Code decides, never the LLM.

Unclear facts are set to the conservative value (`False`: shareholder loan counts as debt, cash
is not netted) with `ambiguous=True` and a `PLAN_FACT_AMBIGUOUS` warning.
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

from finn.errors import ErrorCode, Finding, finding
from finn.models.plan import CellRef, FactKey, LoanFactKey, LoanLine, PlanFact
from finn.plan.labels import normalise, normalise_loan_number


@dataclass(frozen=True)
class TextCell:
    ref: CellRef
    text: str

    @property
    def norm(self) -> str:
        return normalise(self.text, keep_parens=True)


_NEGATIONS = frozenset({"kein", "keine", "keinen", "keiner", "keinerlei", "nicht", "ohne"})


def _negated(norm: str, start: int) -> bool:
    """A negation word among the three words before position `start`."""
    before = norm[:start].split()[-3:]
    return any(word in _NEGATIONS for word in before)


@dataclass(frozen=True)
class _Hit:
    cell: TextCell
    positive: bool
    strength: int  # higher = clearer evidence


def _decide(
    key: FactKey | LoanFactKey, hits: Sequence[_Hit], *, required: bool
) -> tuple[PlanFact, bool]:
    """Fact from hits; returns (fact, needs_warning)."""
    pos = sorted((h for h in hits if h.positive), key=lambda h: -h.strength)
    neg = [h for h in hits if not h.positive]
    strong = [h for h in pos if h.strength >= 2]
    if strong and not neg:
        return _fact(key, True, strong[0].cell), False
    if neg and not pos:
        return _fact(key, False, neg[0].cell), False
    if hits:
        return _fact(key, False, (pos or neg)[0].cell, ambiguous=True), True
    return PlanFact(key=key, value=False, evidence=None, quote=None, ambiguous=required), required


def _fact(
    key: FactKey | LoanFactKey, value: bool, cell: TextCell, *, ambiguous: bool = False
) -> PlanFact:
    return PlanFact(key=key, value=value, evidence=cell.ref, quote=cell.text, ambiguous=ambiguous)


# ── qualified_subordination ──────────────────────────────────────────────────

_RANGRUECKTRITT = re.compile(r"rangruecktritt")
_NACHRANG = re.compile(r"\bnachrangig")
_QUALIFIED = re.compile(r"qualifizier")
_INSO_39 = re.compile(r"§\s*39\s*abs\s*2\s*inso")


def _subordination_hits(texts: Sequence[TextCell]) -> list[_Hit]:
    hits: list[_Hit] = []
    for cell in texts:
        norm = cell.norm
        match = _RANGRUECKTRITT.search(norm)
        if match:
            if _negated(norm, match.start()):
                hits.append(_Hit(cell, positive=False, strength=2))
                continue
            strength = 3 if _INSO_39.search(norm) else 2 if _QUALIFIED.search(norm) else 1
            hits.append(_Hit(cell, positive=True, strength=strength))
        elif _NACHRANG.search(norm):
            hits.append(_Hit(cell, positive=True, strength=1))
    return hits


def subordination_fact(
    texts: Sequence[TextCell], *, has_shareholder_loan: bool
) -> tuple[PlanFact, list[Finding]]:
    """`qualified_subordination` of the shareholder loan (Bilanz note 2 in the sample)."""
    fact, warn = _decide(
        FactKey.QUALIFIED_SUBORDINATION,
        _subordination_hits(texts),
        required=has_shareholder_loan,
    )
    if not warn:
        return fact, []
    return fact, [
        finding(
            ErrorCode.PLAN_FACT_AMBIGUOUS,
            label="Qualifizierter Rangrücktritt des Gesellschafterdarlehens",
            assumption="kein qualifizierter Rangrücktritt (Darlehen zählt als Verbindlichkeit)",
            refs=[fact.evidence] if fact.evidence else [],
            items=[FactKey.QUALIFIED_SUBORDINATION],
        )
    ]


# ── cash_freely_available ────────────────────────────────────────────────────

_CASH_CONTEXT = re.compile(
    r"\b(liquide mittel|fluessige mittel|kassenbestand|guthaben|bankguthaben|zahlungsmittel"
    r"|liquiditaet|kasse)"
)
_FREE = re.compile(r"\b(frei|uneingeschraenkt) verfuegbar")
_RESTRICTED = re.compile(r"(verpfaend|gesperrt|sperrkonto|verfuegungsbeschraenk|sicherheitsabtret)")


def _cash_hits(texts: Sequence[TextCell]) -> list[_Hit]:
    hits: list[_Hit] = []
    for cell in texts:
        norm = cell.norm
        if not _CASH_CONTEXT.search(norm):
            continue
        for match in _FREE.finditer(norm):
            hits.append(_Hit(cell, positive=not _negated(norm, match.start()), strength=2))
        for match in _RESTRICTED.finditer(norm):
            if not _negated(norm, match.start()):
                hits.append(_Hit(cell, positive=False, strength=2))
    return hits


def cash_fact(texts: Sequence[TextCell], *, has_cash: bool) -> tuple[PlanFact, list[Finding]]:
    """`cash_freely_available` ("frei verfügbar", no unnegated "verpfändet" / "gesperrt")."""
    fact, warn = _decide(FactKey.CASH_FREELY_AVAILABLE, _cash_hits(texts), required=has_cash)
    if not warn:
        return fact, []
    return fact, [
        finding(
            ErrorCode.PLAN_FACT_AMBIGUOUS,
            label="Freie Verfügbarkeit der liquiden Mittel",
            assumption="liquide Mittel nicht frei verfügbar (nicht abgezogen)",
            refs=[fact.evidence] if fact.evidence else [],
            items=[FactKey.CASH_FREELY_AVAILABLE],
        )
    ]


# ── per loan: bullet_in_plan_year, refinanced ────────────────────────────────

_BULLET = re.compile(r"endfaellig")
_REFINANCED = re.compile(r"refinanz")
_NOTE_PREFIX = re.compile(r"^\s*\d+\s*[).]\s*")


def notes_for_loan(loan: LoanLine, notes: Sequence[TextCell]) -> list[TextCell]:
    """Notes that name this loan: loan number, label head, or the note's subject."""
    head = normalise(loan.label.split("(")[0])
    number = normalise_loan_number(loan.loan_number) if loan.loan_number else None
    label_norm = normalise(loan.label, keep_parens=True)
    related: list[TextCell] = []
    for note in notes:
        compact = normalise_loan_number(note.text)
        subject = normalise(_NOTE_PREFIX.sub("", note.text).split(":")[0])
        if (
            (number and number in compact)
            or (len(head) >= 6 and head in note.norm)
            or (":" in note.text and len(subject) >= 6 and subject in label_norm)
        ):
            related.append(note)
    return related


def _note_hits(notes: Sequence[TextCell], pattern: re.Pattern[str]) -> list[_Hit]:
    hits: list[_Hit] = []
    for cell in notes:
        norm = cell.norm
        for match in pattern.finditer(norm):
            hits.append(_Hit(cell, positive=not _negated(norm, match.start()), strength=2))
    return hits


def loan_facts(loan: LoanLine, notes: Sequence[TextCell]) -> dict[LoanFactKey, PlanFact]:
    """Bullet from the schedule (closing 0 after a repayment), refinancing from notes only."""
    related = notes_for_loan(loan, notes)
    facts: dict[LoanFactKey, PlanFact] = {}
    repaid = loan.scheduled_repayment + loan.special_repayment
    bullet = loan.closing == Decimal(0) and repaid > 0
    note_hits = _note_hits(related, _BULLET)
    agreeing = [h for h in note_hits if h.positive == bullet]
    if agreeing:
        facts[LoanFactKey.BULLET_IN_PLAN_YEAR] = _fact(
            LoanFactKey.BULLET_IN_PLAN_YEAR, bullet, agreeing[0].cell,
            ambiguous=len(agreeing) != len(note_hits),
        )  # fmt: skip
    else:
        facts[LoanFactKey.BULLET_IN_PLAN_YEAR] = PlanFact(
            key=LoanFactKey.BULLET_IN_PLAN_YEAR,
            value=bullet,
            evidence=loan.refs.get("closing"),
            quote=None,
            ambiguous=bool(note_hits),
        )
    refi_hits = _note_hits(related, _REFINANCED)
    if refi_hits:
        fact, _ = _decide(LoanFactKey.REFINANCED, refi_hits, required=False)
        facts[LoanFactKey.REFINANCED] = fact
    return facts
