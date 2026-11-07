"""Report → German reply mail (ARCHITECTURE §6.4): code builds the mail, the LLM only polishes.

1. Code fills Jinja2 templates (`templates/`) from the `Report`: checked input, warnings,
   summary, table, calculation path per covenant, notes, footer. This alone is a complete mail.
2. Optional polish (D21): the LLM rewrites only the summary and notes paragraphs, which it
   sees with every number and name replaced by a placeholder (`{{N3}}`, `{{NAME2}}`).
3. Placeholder check + number check (`number_check.py`); any doubt → the template text.

Variants: result (incl. PARTIAL), ask, block, other (off-topic / question), error.
"""

import hashlib
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, StrictUndefined, select_autoescape
from pydantic import BaseModel, ConfigDict

from finn.calc.formula import number_de
from finn.calc.render import headroom_de, met_de, threshold_de, value_de
from finn.errors import ErrorCode, Finding, Severity, finding
from finn.llm import client
from finn.mail.thread import reply_references, reply_subject
from finn.models.mail import InboundMessage, ReplyDraft, ReplyKind
from finn.models.result import AssumedZero, CheckedInput, CovenantResult, Excluded, Report, Trace
from finn.models.result import Used as UsedTrace
from finn.plan.render import text_table
from finn.reply.number_check import allowed_values, unknown_numbers

TEMPLATES = Path(__file__).parent / "templates"
FOOTER_DETERMINISTIC = "Alle Kennzahlen wurden deterministisch berechnet, nicht vom Sprachmodell."
EARLY_WARNING = (
    "Hinweis: Frühwarnung auf Planbasis, kein Vertragsbruch (maßgeblich ist der Jahresabschluss)."
)
_QUOTE_MAX = 280

_env = Environment(
    loader=FileSystemLoader(TEMPLATES),
    autoescape=select_autoescape(enabled_extensions=("html.j2",), default_for_string=False),
    trim_blocks=True,
    lstrip_blocks=True,
    keep_trailing_newline=True,
    undefined=StrictUndefined,
)


def _text_table(headers: list[str], rows: list["Row"]) -> str:
    cells = [[r.contract, r.covenant, r.value, r.threshold, r.headroom, r.met] for r in rows]
    return "\n".join(text_table(headers, cells))


_env.globals["text_table"] = _text_table


# ── Message-ID ───────────────────────────────────────────────────────────────


def reply_message_id(workflow_id: str, seq: int, kind: str, from_addr: str) -> str:
    """Deterministic: the same workflow step always gets the same Message-ID (idempotent send)."""
    digest = hashlib.sha256(f"{workflow_id}|{seq}|{kind}".encode()).hexdigest()[:24]
    domain = from_addr.rpartition("@")[2] or "finn.invalid"
    return f"finn.{digest}.{seq}@{domain}"


# ── View model ───────────────────────────────────────────────────────────────


@dataclass
class Line:
    level: int
    text: str
    muted: bool = False


@dataclass
class PathBlock:
    title: str
    status: str
    source: str | None  # "§ 14 Abs. 1 lit. b, S. 7: „…“"
    more_sources: str | None
    formula: str | None
    sub_formulas: list[str]
    lines: list[Line]
    notes: list[str]


@dataclass
class Row:
    contract: str
    covenant: str
    value: str
    threshold: str
    headroom: str
    met: str
    status: str  # met | tight | breached | open


@dataclass
class Context:
    kind: ReplyKind
    salutation: str
    intro: str
    checked: list[str] = field(default_factory=list)  # "Datei: …"
    input_status: str | None = None
    warnings: list[str] = field(default_factory=list)
    infos: list[str] = field(default_factory=list)
    summary: list[str] = field(default_factory=list)
    early_warning: str | None = None
    table_headers: list[str] = field(default_factory=list)
    rows: list[Row] = field(default_factory=list)
    paths: list[PathBlock] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    items: list[str] = field(default_factory=list)  # ask / block: what is wrong / needed
    closing: list[str] = field(default_factory=list)
    footer: list[str] = field(default_factory=list)
    run_id: str = ""

    def as_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


def salutation(name: str | None) -> str:
    clean = " ".join((name or "").replace('"', "").split())
    if not clean or "@" in clean:
        return "Guten Tag,"
    if "," in clean:  # "Mustermann, Max"
        last, _, first = clean.partition(",")
        clean = f"{first.strip()} {last.strip()}".strip()
    return f"Guten Tag {clean},"


def _footer(run_id: str, deterministic: bool) -> list[str]:
    lines = [FOOTER_DETERMINISTIC] if deterministic else []
    return [*lines, f"Finn · Referenz: {run_id}"]


def _checked_lines(h: CheckedInput) -> list[str]:
    status = "Entwurf" if h.status == "draft" else "beschlossen"
    version = f" vom {h.version_date:%d.%m.%Y}" if h.version_date else ""
    return [
        f"Datei: {h.file or '–'} (SHA-256 {h.sha256[:12]}…)",
        f"Unternehmen: {h.company or '–'} · Plan {h.plan_year} ({status}{version}) · "
        f"Stichtag {h.reporting_date:%d.%m.%Y}",
        f"Vertragsprofile: {', '.join(sorted(h.profile_hashes)) or '–'}",
    ]


def _finding_text(f: Finding) -> str:
    cells = ", ".join(str(r) for r in f.refs[:3])
    return f"{f.message_de} [{cells}]" if cells else f.message_de


def _contract_label(r: CovenantResult) -> str:
    return f"{r.contract_id} {r.contract_name_de}"


def _status_key(r: CovenantResult) -> str:
    if r.status == "met":
        return "tight" if r.tight else "met"
    return "breached" if r.status == "breached" else "open"


def _formula(r: CovenantResult) -> str | None:
    if not r.formula_de:
        return None
    if r.value_raw is None or r.value_rounded is None:
        return r.formula_de
    raw = number_de(r.value_raw, 4)
    times = " × 100" if r.unit == "percent" else ""
    return f"{r.formula_de}{times} = {raw} → gerundet {value_de(r)}"


def _cells(refs: Sequence[object]) -> str:
    if not refs:
        return ""
    if len(refs) <= 3:
        return f" [{', '.join(str(r) for r in refs)}]"
    return f" [{len(refs)} Zellen]"


def _weight(w: Decimal) -> str:
    if w == 1:
        return ""
    if w == -1:
        return "− "
    return f"{number_de(w).replace('-', '−')} × "


def _trace_lines(traces: Sequence[Trace], level: int) -> list[Line]:
    out: list[Line] = []
    for t in traces:
        if isinstance(t, UsedTrace):
            out.append(Line(level, f"{_weight(t.weight)}{t.label_de} {number_de(t.value)}"
                                   f"{_cells(t.refs)}"))  # fmt: skip
            parts = t.parts
            used = [p for p in parts if isinstance(p, UsedTrace)]
            # A definition that is just one plan line ("Eigenkapital" = "A. Eigenkapital"):
            # show the line once, keep the notes about left-out parts.
            if (
                len(used) == 1
                and used[0].value == t.value
                and used[0].weight == 1
                and not used[0].parts
            ):
                parts = [p for p in parts if not isinstance(p, UsedTrace)]
            if parts:
                out += _trace_lines(parts, level + 1)
        elif isinstance(t, Excluded):
            value = f" {number_de(t.value)}" if t.value is not None else ""
            evidence = (
                f" (Beleg {t.evidence.evidence})" if t.evidence and t.evidence.evidence else ""
            )
            out.append(Line(level, f"– {t.label_de}{value}{_cells(t.refs)}: "
                                   f"{t.reason}{evidence}", muted=True))  # fmt: skip
        elif isinstance(t, AssumedZero):
            out.append(Line(level, f"– {t.label_de}: {t.reason}", muted=True))
    return out


def _path(r: CovenantResult) -> PathBlock:
    src = r.sources[0] if r.sources else None
    source = None
    if src is not None:
        quote = src.quote if len(src.quote) <= _QUOTE_MAX else src.quote[: _QUOTE_MAX - 1] + "…"
        source = f"{src.section}, S. {src.page}: „{quote}“"
    seen: list[str] = []
    for s in r.sources[1:]:
        cite = f"{s.section}, S. {s.page}"
        if cite not in seen and (src is None or cite != f"{src.section}, S. {src.page}"):
            seen.append(cite)
    lines: list[Line] = []
    if r.components is not None:
        lines.append(Line(0, "Zähler:"))
        lines += _trace_lines(r.components.numerator, 1)
        if r.components.denominator:
            lines.append(Line(0, "Nenner:"))
            lines += _trace_lines(r.components.denominator, 1)
    notes: list[str] = []
    if r.reason:
        notes.append(f"Grund: {r.reason}")
    for alt in r.alternatives:
        notes.append(f"Alternative ({alt.assumption_de}): {number_de(alt.value_rounded)}")
    if r.sensitivity:
        notes.append(f"Sensitivität: {r.sensitivity.text_de}")
    for f in r.findings:
        prefix = "Achtung: " if f.severity in (Severity.WARN, Severity.PARTIAL) else ""
        notes.append(prefix + _finding_text(f))
    return PathBlock(
        title=f"{_contract_label(r)} · {r.name_de}",
        status=met_de(r),
        source=source,
        more_sources=f"Weitere Fundstellen: {'; '.join(seen)}" if seen else None,
        formula=_formula(r),
        sub_formulas=list(r.sub_formulas_de),
        lines=lines,
        notes=notes,
    )


def _summary(report: Report) -> list[str]:
    results = report.results
    n = len(results)
    out: list[str] = []
    if not results:
        return ["Es konnte kein Financial Covenant berechnet werden (Gründe siehe unten)."]
    breached = [r for r in results if r.status == "breached"]
    open_ = [r for r in results if r.status in ("not_computable", "manual_review")]
    met = [r for r in results if r.status == "met"]
    word = "Financial Covenant" if n == 1 else "Financial Covenants"
    if not breached and not open_:
        out.append(
            f"Auf Basis des Plans {'wird' if n == 1 else 'werden'} "
            f"{'der' if n == 1 else f'alle {n}'} {word} eingehalten."
        )
    else:
        parts = []
        if breached:
            names = ", ".join(f"{r.name_de} ({r.contract_name_de})" for r in breached)
            parts.append(f"nicht eingehalten: {names}")
        if open_:
            names = ", ".join(f"{r.name_de} ({r.contract_name_de})" for r in open_)
            parts.append(f"nicht berechenbar oder manuell zu prüfen: {names}")
        out.append(
            f"Auf Basis des Plans {'wird' if len(met) == 1 else 'werden'} {len(met)} von {n} "
            f"{word} eingehalten; {'; '.join(parts)}."
        )
    tight = next((r for r in results if r.covenant_id == report.tightest), None)
    if tight is not None and tight.threshold is not None:
        bound = "min." if tight.operator == ">=" else "max."
        limit = threshold_de(tight).split(" ", 1)[1]
        out.append(
            f"Am engsten: {tight.name_de} ({tight.contract_name_de}) mit {value_de(tight)} bei "
            f"{bound} {limit}."
        )
        if tight.sensitivity:
            out.append(tight.sensitivity.text_de)
    knapp = [r for r in results if r.status == "met" and r.tight]
    if knapp:
        names = ", ".join(f"{r.name_de} ({r.contract_name_de})" for r in knapp)
        out.append(f"Knapp eingehalten (Spielraum unter 10 % des Grenzwerts): {names}.")
    return out


def result_context(
    report: Report,
    *,
    inbound: InboundMessage,
    extra_findings: Sequence[Finding],
    run_id: str,
    tight_ratio: Decimal = Decimal("0.10"),
) -> Context:
    h = report.header
    general = [f for f in [*extra_findings, *report.findings] if not f.affects]
    warnings = [
        _finding_text(f) for f in general if f.severity in (Severity.WARN, Severity.PARTIAL)
    ]
    infos = [_finding_text(f) for f in general if f.severity == Severity.INFO]
    pct = f"{tight_ratio * 100:.0f}"
    summary = [s.replace("unter 10 %", f"unter {pct} %") for s in _summary(report)]
    year = h.plan_year if h else ""
    rows = [
        Row(_contract_label(r), r.name_de, value_de(r), threshold_de(r), headroom_de(r),
            met_de(r), _status_key(r))
        for r in report.results
    ]  # fmt: skip
    return Context(
        kind="result",
        salutation=salutation(inbound.from_name),
        intro="vielen Dank für Ihre Nachricht. Hier ist das Ergebnis der Covenant-Prüfung.",
        checked=_checked_lines(h) if h else [],
        input_status=(
            "Eingangsprüfungen bestanden (Bilanz ausgeglichen, Darlehensspiegel abgestimmt, "
            "Unternehmen und Planjahr passend)"
            + ("." if not warnings else "; Hinweise siehe unten.")
        ),
        warnings=warnings,
        infos=infos,
        summary=summary,
        early_warning=EARLY_WARNING,
        table_headers=["Vertrag", "Covenant", f"Plan {year}", "Grenze", "Spielraum", "Erfüllt"],
        rows=rows,
        paths=[_path(r) for r in report.results],
        notes=[n.text_de for n in report.contract_notes],
        closing=["Bei Rückfragen antworten Sie einfach auf diese Nachricht."],
        footer=_footer(run_id, deterministic=True),
        run_id=run_id,
    )


def ask_context(
    findings: Sequence[Finding], *, inbound: InboundMessage, run_id: str, wait_days: float
) -> Context:
    return Context(
        kind="ask",
        salutation=salutation(inbound.from_name),
        intro="vielen Dank für Ihre Nachricht. Für die Covenant-Prüfung benötige ich noch "
        "Folgendes:",
        items=[_finding_text(f) for f in findings],
        closing=[
            "Bitte antworten Sie einfach auf diese Nachricht und hängen Sie die Datei an; die "
            f"Prüfung startet dann automatisch. Ich warte {wait_days:g} Tage auf Ihre Antwort."
        ],
        footer=_footer(run_id, deterministic=False),
        run_id=run_id,
    )


def block_context(
    findings: Sequence[Finding],
    *,
    inbound: InboundMessage,
    run_id: str,
    file: str | None,
    header: CheckedInput | None,
    wait_days: float,
) -> Context:
    shown = [f for f in findings if f.severity in (Severity.BLOCK, Severity.ASK)] or list(findings)
    return Context(
        kind="block",
        salutation=salutation(inbound.from_name),
        intro=f"vielen Dank für Ihre Nachricht. Die Datei {file or 'im Anhang'} kann ich so nicht "
        "auswerten. Ein Covenant-Ergebnis bestätige ich erst, wenn die Eingangsprüfungen "
        "bestanden sind:",
        checked=_checked_lines(header) if header else [],
        items=[_finding_text(f) for f in shown],
        closing=[
            "Bitte senden Sie eine korrigierte Fassung als Antwort auf diese Nachricht; die "
            f"Prüfung startet dann automatisch neu. Ich warte {wait_days:g} Tage auf Ihre Antwort."
        ],
        footer=_footer(run_id, deterministic=False),
        run_id=run_id,
    )


def other_context(*, inbound: InboundMessage, run_id: str, question: bool) -> Context:
    if question:
        intro = (
            "vielen Dank für Ihre Nachricht. Rückfragen zu früheren Ergebnissen kann ich noch "
            "nicht beantworten. Den Rechenweg finden Sie in meiner Ergebnis-Mail; für eine neue "
            "Prüfung senden Sie mir bitte den Wirtschaftsplan als Excel-Datei."
        )
    else:
        intro = (
            "vielen Dank für Ihre Nachricht. Ich bin Finn und prüfe die Financial Covenants Ihrer "
            "Kreditverträge anhand des Wirtschaftsplans. Andere Anfragen kann ich nicht "
            "bearbeiten. Für eine Prüfung senden Sie mir bitte den Wirtschaftsplan als "
            "Excel-Datei (.xlsx)."
        )
    return Context(
        kind="other",
        salutation=salutation(inbound.from_name),
        intro=intro,
        footer=_footer(run_id, deterministic=False),
        run_id=run_id,
    )


def error_context(*, inbound: InboundMessage, run_id: str) -> Context:
    return Context(
        kind="error",
        salutation=salutation(inbound.from_name),
        intro="vielen Dank für Ihre Nachricht. Bei der Verarbeitung ist leider ein unerwarteter "
        "Fehler aufgetreten. Das Betriebsteam ist informiert; Sie erhalten kein Ergebnis zu "
        f"dieser Nachricht. Bitte nennen Sie bei Rückfragen die Referenz {run_id}.",
        footer=_footer(run_id, deterministic=False),
        run_id=run_id,
    )


# ── Polish (D21) ─────────────────────────────────────────────────────────────

_NUM = re.compile(r"(?<![\w{])[-−]?\d[\d.,]*\d(?![\w}])|(?<![\w{])\d(?![\w}])")
_PH = re.compile(r"\{\{[A-Z]+\d+\}\}")

POLISH_SYSTEM = """\
Du formulierst Absätze einer geschäftlichen E-Mail auf Deutsch (Sie-Form, sachlich, knapp) um.
Zahlen, Namen und Fundstellen sind durch Platzhalter wie {{N3}} oder {{NAME1}} ersetzt.
Regeln: Jeder Platzhalter muss genau so (mit doppelten geschweiften Klammern) erhalten bleiben,
mindestens einmal. Füge keine Zahlen, Namen, Fakten oder Wertungen hinzu, lass nichts weg.
Ändere nicht, ob ein Covenant eingehalten ist. Gib so viele Absätze bzw. Hinweise zurück, wie du
bekommen hast, in derselben Reihenfolge."""


class PolishAnswer(BaseModel):
    model_config = ConfigDict(extra="forbid")

    summary: list[str]
    notes: list[str]


class Masker:
    """Numbers and names → `{{N1}}` / `{{NAME1}}`; `fill` puts them back."""

    def __init__(self, names: Sequence[str]) -> None:
        self.mapping: dict[str, str] = {}
        self.names = sorted({n for n in names if n and len(n) >= 3}, key=len, reverse=True)

    def _ph(self, prefix: str, value: str) -> str:
        for k, v in self.mapping.items():
            if v == value and k.startswith("{{" + prefix):
                return k
        key = f"{{{{{prefix}{1 + sum(1 for k in self.mapping if k.startswith('{{' + prefix))}}}}}"
        self.mapping[key] = value
        return key

    def mask(self, text: str) -> str:
        for name in self.names:
            text = text.replace(name, self._ph("NAME", name))
        return _NUM.sub(lambda m: self._ph("N", m.group(0)), text)

    def fill(self, text: str) -> str:
        return _PH.sub(lambda m: self.mapping.get(m.group(0), m.group(0)), text)


PolishRecord = Callable[[str, str, Exception | None, BaseModel | None], None]


def polish(
    ctx: Context,
    names: Sequence[str],
    *,
    trace_id: str | None = None,
    session_id: str | None = None,
    record: PolishRecord | None = None,
) -> tuple[Context, list[Finding]]:
    """LLM rewrite of summary + notes; returns the (possibly) polished context and REPLY_* notes.
    Any problem → the template context unchanged."""
    if not ctx.summary:
        return ctx, []
    masker = Masker(names)
    summary = [masker.mask(s) for s in ctx.summary]
    notes = [masker.mask(n) for n in ctx.notes]
    user = "Zusammenfassung:\n" + "\n".join(f"- {s}" for s in summary)
    user += "\n\nHinweise:\n" + "\n".join(f"- {n}" for n in notes)
    try:
        answer = client.complete_structured(
            system=POLISH_SYSTEM,
            user=user,
            schema=PolishAnswer,
            purpose="reply_polish",
            trace_id=trace_id,
            session_id=session_id,
        )
    except client.LLMUnavailable as exc:
        if record:
            record(POLISH_SYSTEM, user, exc, None)
        return ctx, [finding(ErrorCode.REPLY_LLM_UNAVAILABLE)]
    if record:
        record(POLISH_SYSTEM, user, None, answer)
    # Paragraph by paragraph: the same placeholders (facts) in, the same out.
    pairs = zip([*summary, *notes], [*answer.summary, *answer.notes], strict=False)
    if (
        len(answer.summary) != len(summary)
        or len(answer.notes) != len(notes)
        or any(set(_PH.findall(a)) != set(_PH.findall(b)) for a, b in pairs)
    ):
        return ctx, [finding(ErrorCode.REPLY_NUMBER_MISMATCH, numbers="Platzhalter verändert")]
    new_summary = [masker.fill(s) for s in answer.summary]
    new_notes = [masker.fill(n) for n in answer.notes]
    allowed = allowed_values([*ctx.summary, *ctx.notes])
    bad = [t for text in [*new_summary, *new_notes] for t in unknown_numbers(text, allowed)]
    if bad:
        return ctx, [finding(ErrorCode.REPLY_NUMBER_MISMATCH, numbers=", ".join(bad[:5]))]
    polished = Context(**{**ctx.as_dict(), "summary": new_summary, "notes": new_notes})
    return polished, []


# ── Draft ────────────────────────────────────────────────────────────────────


def render(ctx: Context) -> tuple[str, str]:
    """(html, text) from the templates."""
    data = ctx.as_dict()
    html = _env.get_template("mail.html.j2").render(**data)
    text = _env.get_template("mail.txt.j2").render(**data)
    return html, text


def draft(
    ctx: Context,
    *,
    inbound: InboundMessage,
    workflow_id: str,
    seq: int,
    from_addr: str,
    used_template: bool = True,
    placeholders: dict[str, str] | None = None,
    notes: Sequence[Finding] = (),
) -> ReplyDraft:
    html, text = render(ctx)
    return ReplyDraft(
        kind=ctx.kind,
        to=inbound.from_addr,
        to_name=inbound.from_name,
        subject=reply_subject(inbound.subject),
        in_reply_to=inbound.message_id,
        references=reply_references(inbound),
        message_id=reply_message_id(workflow_id, seq, ctx.kind, from_addr),
        html=html,
        text=text,
        placeholders=placeholders or {},
        used_template=used_template,
        notes=[f"{f.code}: {f.message_de}" for f in notes],
        run_id=ctx.run_id,
    )


def names_for_masking(report: Report, inbound: InboundMessage) -> list[str]:
    names: list[str] = []
    if report.header and report.header.company:
        names.append(report.header.company)
    if inbound.from_name:
        names.append(inbound.from_name)
    for r in report.results:
        names += [r.contract_name_de, r.name_de]
    return names
