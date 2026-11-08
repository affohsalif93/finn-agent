"""Calculation path per covenant for the reply (ARCHITECTURE §6.4, D14), built from the trace.

Four steps per covenant: source in the contract (covenant clause + main definition only; the full
list stays in `results.json`), formula in words (definition names), input values (short labels,
T€, cell; sub-totals), result (with the contract's rounding wording); plus "Besonderheit":
excluded components and items set to 0, with reason, evidence and a short verbatim contract
quote. The alternative value is shown only when it changes the outcome (met / tight / breached).
EBITDA is derived once in a shared block that the covenants refer to.

Pure functions on `CovenantResult`; every number shown comes from the report (or, for the
"with the excluded component" alternative, from the same numbers with contract rounding).
"""

import re
import textwrap
from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import ROUND_DOWN, Decimal

from finn.calc.rounding import effective_decimals, headroom, is_met, is_tight, round_half_up
from finn.errors import ErrorCode
from finn.models.contract import Source
from finn.models.plan import CellRef, FactKey
from finn.models.result import AssumedZero, CovenantResult, Excluded, Trace
from finn.models.result import Used as UsedTrace
from finn.numbers import number_de, unit_suffix
from finn.wording import (
    STATUS_DE,
    alt_status_de,
    headroom_de,
    limit_de,
    met_de,
    minus,
    strip_parenthetical,
    threshold_de,
    value_de,
    zeros_note,
)

# Short position labels per building block (the Excel label goes into the HTML title attribute).
SHORT_LABELS: dict[str, str] = {
    "net_income": "Jahresüberschuss",
    "income_taxes": "Ertragsteuern",
    "interest_expense": "Zinsaufwand",
    "pension_unwinding": "davon Aufzinsung Pensionsrückstellungen",
    "interest_income": "Zinserträge",
    "depreciation": "Abschreibungen",
    "extraordinary_items": "Außerordentliches Ergebnis",
    "total_assets": "Bilanzsumme",
    "total_equity_liabilities": "Summe Passiva",
    "equity": "Eigenkapital",
    "sonderposten": "Sonderposten",
    "bank_debt": "Verbindlichkeiten ggü. Kreditinstituten",
    "shareholder_loan": "Gesellschafterdarlehen",
    "cash": "Kassenbestand, Bankguthaben",
    "called_unpaid_capital": "Ausstehende Einlagen",
    "bonds": "Anleihen",
    "finance_leases": "Leasingverbindlichkeiten",
    "recourse_factoring": "Factoring mit Rückgriff",
    "scheduled_repayments_total": "Planmäßige Tilgungen",
    "special_repayments_total": "Sondertilgungen",
    "total_bank_debt": "Bankverbindlichkeiten (Darlehensspiegel)",
    "interest_on_financial_debt": "Zinsen (Darlehensspiegel)",
}
FACT_REASONS: dict[str, str] = {
    FactKey.QUALIFIED_SUBORDINATION: "qualifizierter Rangrücktritt",
    FactKey.CASH_FREELY_AVAILABLE: "frei verfügbare liquide Mittel",
}
EBITDA_REF = "ref:ebitda"
EBITDA_ANCHOR = "rw-ebitda"
_SUPERSCRIPT = str.maketrans("0123456789", "⁰¹²³⁴⁵⁶⁷⁸⁹")
_EXCLUSION_WORDS = re.compile(
    r"mit Ausnahme|ausgenommen|nicht (?:\w+ )?(?:einzubeziehen|enthalten|zu berücksichtigen)"
    r"|außer Betracht|unberücksichtigt",
    re.IGNORECASE,
)
_NOT_INCLUDED = re.compile(r",?\s*nicht (?:einzubeziehen|enthalten)\s*$")  # said by the sentence
_QUOTE_WORDS = 14  # a trimmed quote keeps about this many words
LABEL_WIDTH = 20  # text part: label column of the calculation path
TEXT_WIDTH = 100


def mark(n: int) -> str:
    """Footnote mark: 1 → ¹, 12 → ¹²."""
    return str(n).translate(_SUPERSCRIPT)


def anchor(r: CovenantResult) -> str:
    return "rw-" + re.sub(r"[^a-z0-9]+", "-", r.covenant_id.lower()).strip("-")


def contract_no(contract_id: str) -> str:
    """ "V4" → "Vertrag 4" (the contracts are numbered in the folder)."""
    m = re.fullmatch(r"V(\d+)", contract_id)
    return f"Vertrag {m.group(1)}" if m else f"Vertrag {contract_id}"


# ── view model ───────────────────────────────────────────────────────────────


@dataclass
class InputRow:
    label: str
    value: str  # T€, German format, "" for a heading
    cell: str
    title: str = ""  # Excel label (HTML title attribute)
    subtotal: bool = False


@dataclass
class PathBlock:
    anchor: str
    title: str  # "V4 Konsortialkredit · Dynamischer Verschuldungsgrad"
    head: str  # "3,37 ≤ 3,50 · Spielraum 0,13 (knapp)"
    status: str  # met | tight | breached | open
    source: str | None
    formula: str | None
    inputs: list[InputRow] = field(default_factory=list)
    result: str = ""
    specials: list[str] = field(default_factory=list)


@dataclass
class EbitdaBlock:
    anchor: str
    title: str
    used_by: str  # "V3, V4"
    inputs: list[InputRow]
    specials: list[str]


# ── sources ──────────────────────────────────────────────────────────────────


_HEAD = re.compile(r"^(§\s*\d+\s*Abs\.\s*\d+|§\s*\d+|Anlage\s*\d+)")


def _section_head(section: str) -> str:
    """ "§ 1 Abs. 1 – EBITDA" → "§ 1 Abs. 1"; "Anlage 3 (6)" → "Anlage 3"."""
    m = _HEAD.match(section.strip())
    return m.group(1) if m else section.split(" – ")[0].strip()


def _pages(pages: Sequence[int]) -> str:
    lo, hi = min(pages), max(pages)
    return f"S. {lo}" if lo == hi else f"S. {lo}–{hi}"


def main_definition(r: CovenantResult) -> Source | None:
    """The definition the covenant points to (source of its first component), else the first
    source naming the metric."""
    if not r.sources:
        return None
    clause = r.sources[0]
    traces = [*(r.components.numerator if r.components else [])]
    ids = [t.source_id for t in traces if t.source_id]
    for sid in ids:
        src = next((s for s in r.sources if s.id == sid), None)
        if src is not None and src.id != clause.id:
            return src
    name = r.name_de.lower()
    return next((s for s in r.sources[1:] if name in s.section.lower()), None)


def _definition(r: CovenantResult) -> tuple[str, str] | None:
    """("§ 1 Abs. 1", "S. 2–3"): the definition paragraph with the pages of all its sources."""
    d = main_definition(r)
    if d is None:
        return None
    head = _section_head(d.section)
    pages = [s.page for s in r.sources if _section_head(s.section) == head] or [d.page]
    return head, _pages(pages)


def source_line(r: CovenantResult) -> str | None:
    if not r.sources:
        return None
    clause = r.sources[0]
    out = f"{clause.section} (S. {clause.page})"
    definition = _definition(r)
    if definition and definition[0] != _section_head(clause.section):
        out += f", Definition {definition[0]} ({definition[1]})"
    return out


def footnote(r: CovenantResult) -> str:
    """ "gem. Vertrag 4, § 1 Abs. 1 S. 2–3; § 14 Abs. 1 lit. b S. 7" (+ why not computable)."""
    parts: list[str] = []
    definition = _definition(r)
    if definition:
        parts.append(f"{definition[0]} {definition[1]}")
    if r.sources:
        parts.append(f"{r.sources[0].section} S. {r.sources[0].page}")
    text = f"gem. {contract_no(r.contract_id)}" + (f", {'; '.join(parts)}" if parts else "")
    if r.status in ("not_computable", "manual_review") and r.reason:
        text += f" – {STATUS_DE[r.status]}: {r.reason}"
    return text


def plan_sheets(results: Sequence[CovenantResult]) -> list[str]:
    """Sheets the values come from, in statement order (GuV, Bilanz, Finanzierung, …)."""
    seen: list[str] = []

    def walk(traces: Sequence[Trace]) -> None:
        for t in traces:
            for ref in t.refs if isinstance(t, (UsedTrace, Excluded)) else []:
                if ref.sheet not in seen:
                    seen.append(ref.sheet)
            if isinstance(t, UsedTrace):
                walk(t.parts)

    for r in results:
        if r.components:
            walk(r.components.numerator)
            walk(r.components.denominator)
    order = ["guv", "bilanz", "finanzierung"]
    return sorted(seen, key=lambda s: (order.index(s.lower()) if s.lower() in order else 9, s))


# ── formula in words ─────────────────────────────────────────────────────────


def _short(t: UsedTrace | Excluded | AssumedZero) -> str:
    return SHORT_LABELS.get(t.component, t.label_de)


def _side_words(traces: Sequence[Trace]) -> str:
    used = [t for t in traces if isinstance(t, UsedTrace)]
    if (
        len(used) == 1
        and used[0].component.startswith("ref:")
        and used[0].component != EBITDA_REF
        and 2 <= len([p for p in used[0].parts if isinstance(p, UsedTrace)]) <= 3
    ):
        used = [p for p in used[0].parts if isinstance(p, UsedTrace)]
    words = [_prefix(t.weight, i == 0) + strip_parenthetical(_short(t)) for i, t in enumerate(used)]
    text = " ".join(words)
    return f"({text})" if len(used) > 1 else text


def formula_words(r: CovenantResult) -> str | None:
    if r.components is None:
        return None
    num = _side_words(r.components.numerator)
    if not r.components.denominator:
        return num
    den = _side_words(r.components.denominator)
    return f"{num} / {den}" + (" × 100" if r.unit == "percent" else "")


# ── input values ─────────────────────────────────────────────────────────────


def _cells(refs: Sequence[CellRef]) -> str:
    if not refs:
        return ""
    if len(refs) == 1:
        return str(refs[0])
    sheets = {r.sheet for r in refs}
    cols = {re.sub(r"\d", "", r.cell) for r in refs}
    if len(sheets) == 1 and len(cols) == 1:
        rows = sorted(int(re.sub(r"\D", "", r.cell)) for r in refs)
        col = next(iter(cols))
        return f"{next(iter(sheets))}!{col}{rows[0]}–{col}{rows[-1]}"
    if len(refs) == 2:
        return f"{refs[0]}, {refs[1]}"
    return f"{len(refs)} Zellen"


def _is_loan_aggregate(t: UsedTrace) -> bool:
    used = [p for p in t.parts if isinstance(p, UsedTrace)]
    return bool(used) and all(p.component.startswith("loan:") for p in used)


def _prefix(w: Decimal, first: bool) -> str:
    if w == 1:
        return "" if first else "+ "
    if w == -1:
        return "− "
    sign = "− " if w < 0 else ("" if first else "+ ")
    return f"{sign}{number_de(abs(w))} × "


def _rows(t: UsedTrace, w: Decimal, first: bool) -> list[InputRow]:
    """A leaf (plan item, loan aggregate, EBITDA) is one row; a definition with several parts
    expands into its parts plus a sub-total row."""
    weight = w * t.weight
    if t.component == EBITDA_REF:
        return [InputRow(f"{_prefix(weight, first)}EBITDA", number_de(t.value),
                         "GuV (Herleitung unten)")]  # fmt: skip
    used = [p for p in t.parts if isinstance(p, UsedTrace)]
    if t.component.startswith("ref:") and used and not _is_loan_aggregate(t):
        if len(used) == 1 and used[0].value == t.value:
            return _rows(used[0], weight, first)
        rows: list[InputRow] = []
        for i, p in enumerate(used):
            rows += _rows(p, Decimal(1), i == 0)
        sign = "− " if weight < 0 else ""
        rows.append(InputRow(f"= {sign}{_short(t)}", number_de(t.value), "", subtotal=True))
        return rows
    title = t.plan_value.label if t.plan_value else t.label_de
    return [InputRow(f"{_prefix(weight, first)}{_short(t)}", number_de(t.value), _cells(t.refs),
                     title=title if title != _short(t) else "")]  # fmt: skip


def input_rows(r: CovenantResult) -> list[InputRow]:
    if r.components is None:
        return []
    rows: list[InputRow] = []
    for side in (r.components.numerator, r.components.denominator):
        used = [t for t in side if isinstance(t, UsedTrace)]
        for i, t in enumerate(used):
            rows += _rows(t, Decimal(1), i == 0)
        if len(used) > 1 and r.components.denominator:
            label = "Zähler" if side is r.components.numerator else "Nenner"
            total = r.numerator_value if label == "Zähler" else r.denominator_value
            if total is not None:
                rows.append(InputRow(f"= {label}", number_de(total), "", subtotal=True))
    return rows


# ── result line ──────────────────────────────────────────────────────────────


def rounding_words(r: CovenantResult) -> str:
    if any(f.code == ErrorCode.CALC_NO_ROUNDING_RULE for f in r.findings):
        return f"ohne Rundungsregel im Vertrag, {effective_decimals(r.decimals)} Nachkommastellen"
    return "kaufmännisch gerundet"


def result_line(r: CovenantResult) -> str:
    if r.status in ("not_computable", "manual_review"):
        word = STATUS_DE[r.status]
        return f"{word}: {r.reason}" if r.reason else word
    if r.value_raw is None or r.value_rounded is None:
        return value_de(r)
    d = effective_decimals(r.decimals)
    raw = r.value_raw.quantize(Decimal(1).scaleb(-(d + 1)), rounding=ROUND_DOWN)
    pct = unit_suffix(r.unit)
    calc = ""
    if r.numerator_value is not None and r.denominator_value is not None:
        times = " × 100" if r.unit == "percent" else ""
        calc = f"{number_de(r.numerator_value)} / {number_de(r.denominator_value)}{times} = "
    if any(f.code == ErrorCode.CALC_NO_ROUNDING_RULE for f in r.findings):
        return f"{calc}{number_de(raw, d + 1)}{pct} → {value_de(r)} ({rounding_words(r)})"
    return f"{calc}{number_de(raw, d + 1)}{pct} → {rounding_words(r)} {value_de(r)}"


def head_line(r: CovenantResult) -> str:
    if r.value_rounded is None:
        return f"{met_de(r)} · Grenze {threshold_de(r)}"
    op = "≤" if r.operator == "<=" else "≥"
    limit = limit_de(r)
    room = minus(headroom_de(r))
    knapp = " (knapp)" if r.status == "met" and r.tight else ""
    word = " · nicht eingehalten" if r.status == "breached" else ""
    return f"{value_de(r)} {op} {limit} · Spielraum {room}{knapp}{word}"


# ── "Besonderheit" ───────────────────────────────────────────────────────────


def _trim_quote(quote: str, term: str) -> str | None:
    """The decisive phrase of a contract quote: a few words around an exclusion phrase that
    names `term` (e.g. "… mit Ausnahme von Nachrangigen Gesellschafterdarlehen.")."""
    stems = [w[:8].lower() for w in re.findall(r"\w{6,}", _EXCLUSION_WORDS.sub(" ", term))]
    if not stems:
        return None
    words = quote.split()
    starts = [len(" ".join(words[:i])) + (1 if i else 0) for i in range(len(words))]
    for m in _EXCLUSION_WORDS.finditer(quote):
        at = max(i for i, s in enumerate(starts) if s <= m.start())
        lo, hi = max(0, at - 6), min(len(words), at + 9)
        window = words[lo:hi]
        if any(s in " ".join(window).lower() for s in stems):
            # start at a clause boundary inside the window, if there is one
            for i in range(at - lo - 1, -1, -1):
                if window[i].endswith((",", ";", ":", ".")):
                    window, lo = window[i + 1 :], lo + i + 1
                    break
            text = " ".join(window).rstrip(",;:")
            return f"„{'… ' if lo else ''}{text}{' …' if hi < len(words) else ''}“"
    return None


def _contract_quote(r: CovenantResult, term: str) -> str | None:
    for s in r.sources:
        trimmed = _trim_quote(s.quote, term)
        if trimmed:
            return f"Vertrag {_section_head(s.section)} S. {s.page}: {trimmed}"
    return None


def _note_ref(e: Excluded) -> str:
    """ "Bilanz Anm. 2" from the plan note the fact was read from."""
    if e.evidence is None or e.evidence.evidence is None:
        return ""
    sheet = e.evidence.evidence.sheet
    m = re.match(r"\s*(\d+)\s*[).]", e.evidence.quote or "")
    return f" ({sheet} Anm. {m.group(1)})" if m else f" ({e.evidence.evidence})"


def _with_excluded(
    r: CovenantResult, e: Excluded, weight: Decimal, in_numerator: bool, tight_ratio: Decimal
) -> str:
    """ "Mit Gesellschafterdarlehen wäre der Wert 3,57 (nicht eingehalten)." if that changes the
    outcome; else ""."""
    if (
        e.value is None
        or r.value_rounded is None
        or r.threshold is None
        or r.numerator_value is None
        or r.denominator_value is None
    ):
        return ""
    num, den = r.numerator_value, r.denominator_value
    if in_numerator:
        num += weight * e.value
    else:
        den += weight * e.value
    if den <= 0:
        return ""
    raw = num / den * (100 if r.unit == "percent" else 1)
    d = effective_decimals(r.decimals)
    alt = round_half_up(raw, d)
    met = is_met(alt, r.operator, r.threshold)
    tight = met and is_tight(headroom(alt, r.operator, r.threshold), r.threshold, tight_ratio)
    was_met = r.status == "met"
    if met == was_met and tight == r.tight:
        return ""
    word = "nicht eingehalten" if not met else "knapp eingehalten" if tight else "eingehalten"
    return f" Einbezogen wäre der Wert {number_de(alt, d)}{unit_suffix(r.unit)} ({word})."


def _walk_specials(
    r: CovenantResult,
    traces: Sequence[Trace],
    w: Decimal,
    in_numerator: bool,
    specials: list[str],
    zeros: list[str],
    tight_ratio: Decimal,
) -> None:
    for t in traces:
        if isinstance(t, UsedTrace):
            if t.component != EBITDA_REF:  # EBITDA: shared derivation block
                _walk_specials(r, t.parts, w * t.weight, in_numerator, specials, zeros, tight_ratio)
        elif isinstance(t, AssumedZero):
            zeros.append(_short(t))
        else:
            value = f"{number_de(t.value)} T€, " if t.value is not None else ""
            cells = _cells(t.refs)
            where = f" ({value}{cells})" if value or cells else ""
            reason = (
                FACT_REASONS.get(t.evidence.key, t.reason) + _note_ref(t)
                if t.evidence is not None and t.evidence.value
                else _NOT_INCLUDED.sub("", t.reason)
            )
            text = f"{_short(t)}{where} nicht einbezogen: {reason}"
            term = t.reason.split(": ", 1)[1] if ": " in t.reason else t.reason
            quote = _contract_quote(r, term)
            text += f"; {quote}" if quote else "."
            if not text.endswith((".", "“")):
                text += "."
            specials.append(text + _with_excluded(r, t, w, in_numerator, tight_ratio))


_SKIP_FINDINGS = {
    ErrorCode.PLAN_ITEM_NOT_IN_PLAN,  # shown as "mit 0 angesetzt"
    ErrorCode.PLAN_SUBITEM_NOT_SHOWN,
    ErrorCode.CALC_NO_ROUNDING_RULE,  # in the result line
    ErrorCode.PLAN_STATUS_REQUIRED,  # a contract note (Hinweise)
    ErrorCode.PLAN_FACT_AMBIGUOUS,  # the alternative below says it
}


def strip_label(text: str, r: CovenantResult) -> str:
    """Drop the "Konsortialkredit · Dynamischer Verschuldungsgrad: " prefix of a finding."""
    for prefix in (f"{r.contract_name_de} · {r.name_de}: ", f"{r.contract_name_de}: "):
        if text.startswith(prefix):
            text = text[len(prefix) :]
    text = re.sub(r"\.\.+$", ".", text.strip())
    text = re.sub(r"^(Hinweis|Achtung):\s*", "", text)
    return text[:1].upper() + text[1:]


def specials(r: CovenantResult, tight_ratio: Decimal) -> list[str]:
    out: list[str] = []
    zeros: list[str] = []
    if r.components is not None:
        _walk_specials(r, r.components.numerator, Decimal(1), True, out, zeros, tight_ratio)
        _walk_specials(r, r.components.denominator, Decimal(1), False, out, zeros, tight_ratio)
    if zeros:
        out.append(zeros_note(zeros))
    for alt in r.alternatives:
        out.append(
            "Im Plan nicht eindeutig belegt, konservativ gerechnet. Alternative "
            f"({alt.assumption_de}): {number_de(alt.value_rounded)}{unit_suffix(r.unit)} "
            f"({alt_status_de(alt.status)})."
        )
    for f in r.findings:
        text = strip_label(f.message_de, r)
        if f.code not in _SKIP_FINDINGS and text not in (r.reason or "") and text not in out:
            out.append(text)
    return out


def path_block(r: CovenantResult, status: str, tight_ratio: Decimal) -> PathBlock:
    return PathBlock(
        anchor=anchor(r),
        title=f"{r.contract_id} {r.contract_name_de} · {r.name_de}",
        head=head_line(r),
        status=status,
        source=source_line(r),
        formula=formula_words(r),
        inputs=input_rows(r),
        result=result_line(r),
        specials=specials(r, tight_ratio),
    )


# ── EBITDA (shared derivation) ───────────────────────────────────────────────


def _find_ebitda(traces: Sequence[Trace]) -> UsedTrace | None:
    for t in traces:
        if isinstance(t, UsedTrace):
            if t.component == EBITDA_REF:
                return t
            found = _find_ebitda(t.parts)
            if found is not None:
                return found
    return None


def ebitda_blocks(results: Sequence[CovenantResult]) -> list[EbitdaBlock]:
    groups: dict[tuple[tuple[str, str], ...], tuple[UsedTrace, list[str]]] = {}
    for r in results:
        if r.components is None:
            continue
        e = _find_ebitda([*r.components.numerator, *r.components.denominator])
        if e is None:
            continue
        key = tuple((p.component, str(getattr(p, "value", ""))) for p in e.parts)
        if key not in groups:
            groups[key] = (e, [])
        if r.contract_id not in groups[key][1]:
            groups[key][1].append(r.contract_id)
    blocks: list[EbitdaBlock] = []
    for n, (e, users) in enumerate(groups.values()):
        rows: list[InputRow] = []
        used = [p for p in e.parts if isinstance(p, UsedTrace)]
        for i, p in enumerate(used):
            rows += _rows(p, Decimal(1), i == 0)
        rows.append(InputRow("= EBITDA", number_de(e.value), "", subtotal=True))
        notes: list[str] = []
        zeros = [_short(p) for p in e.parts if isinstance(p, AssumedZero)]
        if zeros:
            notes.append(zeros_note(zeros))
        for x in e.parts:
            if isinstance(x, Excluded):
                notes.append(f"{_short(x)} nicht einbezogen: {x.reason}.")
        suffix = "" if len(groups) == 1 else f" ({', '.join(users)})"
        blocks.append(
            EbitdaBlock(
                anchor=EBITDA_ANCHOR if n == 0 else f"{EBITDA_ANCHOR}-{n + 1}",
                title=f"EBITDA (Herleitung){suffix}",
                used_by=", ".join(users),
                inputs=rows,
                specials=notes,
            )
        )
    return blocks


# ── plain-text layout ────────────────────────────────────────────────────────


def _labelled(label: str, text: str) -> list[str]:
    indent = " " * LABEL_WIDTH
    wrapped = textwrap.wrap(text, TEXT_WIDTH - LABEL_WIDTH) or [""]
    return [f"{label:<{LABEL_WIDTH}}{wrapped[0]}", *(indent + w for w in wrapped[1:])]


def _input_lines(rows: Sequence[InputRow]) -> list[str]:
    if not rows:
        return []
    w_label = max(len("Position"), *(len(r.label) for r in rows)) + 2
    w_value = max(len("T€"), *(len(r.value) for r in rows))
    lines = [f"{'Position':<{w_label}}{'T€':>{w_value}}   Zelle"]
    lines += [f"{r.label:<{w_label}}{r.value:>{w_value}}   {r.cell}".rstrip() for r in rows]
    return lines


def text_block(title: str, head: str, rows: list[tuple[str, str | list[InputRow]]]) -> str:
    gap = max(2, TEXT_WIDTH - len(title) - len(head))
    out = [f"{title}{' ' * gap}{head}" if head else title]
    for label, content in rows:
        if isinstance(content, list):
            lines = _input_lines(content)
            for i, line in enumerate(lines):
                out.append(f"{label if i == 0 else '':<{LABEL_WIDTH}}{line}")
        else:
            out += _labelled(label, content)
    return "\n".join(out)


def path_text(b: PathBlock) -> str:
    rows: list[tuple[str, str | list[InputRow]]] = []
    if b.source:
        rows.append(("Quelle im Vertrag", b.source))
    if b.formula:
        rows.append(("Formel", b.formula))
    if b.inputs:
        rows.append(("Eingangswerte", b.inputs))
    rows.append(("Ergebnis", b.result))
    for i, s in enumerate(b.specials):
        rows.append(("Besonderheit" if i == 0 else "", s))
    return text_block(b.title, b.head, rows)


def ebitda_text(b: EbitdaBlock) -> str:
    rows: list[tuple[str, str | list[InputRow]]] = [
        ("Verwendet in", b.used_by),
        ("Eingangswerte", b.inputs),
    ]
    for i, s in enumerate(b.specials):
        rows.append(("Besonderheit" if i == 0 else "", s))
    return text_block(b.title, "", rows)
