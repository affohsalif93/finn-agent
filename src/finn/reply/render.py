"""Report → German reply mail (ARCHITECTURE §6.4): code builds the mail, the LLM only polishes.

1. Code fills Jinja2 templates (`templates/`) from the `Report`. Result mail, in this order:
   greeting + "die Covenant-Prüfung … ist abgeschlossen", lead box (overall result, tightest
   covenant with headroom, sensitivity, early-warning note), compact table with a status dot
   and footnote marks, footnotes (contract sources, plan file), Hinweise (input notes and
   contract notes, condensed), links to the calculation paths, one path per covenant
   (`paths.py`), the shared EBITDA derivation, signature. This alone is a complete mail.
2. Optional polish (D21): the LLM rewrites only the lead sentences and the Hinweise, which it
   sees with every number and name replaced by a placeholder (`{{N3}}`, `{{NAME2}}`).
3. Placeholder check + number check (`number_check.py`); any doubt → the template text.

Variants in the same visual style: result (incl. PARTIAL), ask, block, other (off-topic /
question), error: greeting, one-line lead box, what exactly is wrong, "So geht es weiter".
"""

import hashlib
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

from jinja2 import Environment, FileSystemLoader, StrictUndefined, select_autoescape
from pydantic import BaseModel, ConfigDict

from finn.errors import ErrorCode, Finding, Severity, finding, stopping
from finn.llm import client
from finn.mail.thread import reply_references, reply_subject
from finn.models.mail import InboundMessage, ReplyDraft, ReplyKind
from finn.models.result import CheckedInput, ContractNote, CovenantResult, Report
from finn.numbers import number_de, unit_suffix
from finn.reply import paths as rw
from finn.reply.number_check import allowed_values, unknown_numbers
from finn.wording import (
    alt_status_de,
    headroom_de,
    limit_de,
    met_de,
    minus,
    plan_status_de,
    strip_parenthetical,
    threshold_de,
    value_de,
)

TEMPLATES = Path(__file__).parent / "templates"
FOOTER_DETERMINISTIC = (
    "Alle Kennzahlen stammen aus einem deterministischen Rechenkern, nicht aus dem Sprachmodell."
)
EARLY_WARNING = (
    "Frühwarnung auf Planbasis, kein Vertragsbruch (maßgeblich ist der Jahresabschluss)."
)
MAX_NOTES = 6
Tone = Literal["info", "warn", "bad", "neutral"]

_env = Environment(
    loader=FileSystemLoader(TEMPLATES),
    autoescape=select_autoescape(enabled_extensions=("html.j2",), default_for_string=False),
    trim_blocks=True,
    lstrip_blocks=True,
    keep_trailing_newline=True,
    undefined=StrictUndefined,
)


# ── Message-ID ───────────────────────────────────────────────────────────────


def reply_message_id(workflow_id: str, seq: int, kind: str, from_addr: str) -> str:
    """Deterministic: the same workflow step always gets the same Message-ID (idempotent send)."""
    digest = hashlib.sha256(f"{workflow_id}|{seq}|{kind}".encode()).hexdigest()[:24]
    domain = from_addr.rpartition("@")[2] or "finn.invalid"
    return f"finn.{digest}.{seq}@{domain}"


# ── View model ───────────────────────────────────────────────────────────────


@dataclass
class Row:
    contract: str  # "V4 · Konsortialkredit"
    covenant: str  # "Dynamischer Verschuldungsgrad"
    mark: str  # footnote mark "⁴"
    value: str
    threshold: str
    headroom: str
    met: str
    status: str  # met | tight | breached | open
    anchor: str
    highlight: bool = False  # the tightest covenant


@dataclass
class Link:
    anchor: str
    label: str


@dataclass
class Context:
    kind: ReplyKind
    salutation: str
    intro: str
    tone: Tone = "info"
    summary: list[str] = field(default_factory=list)  # lead box (polished by the LLM)
    early_warning: str | None = None
    items: list[str] = field(default_factory=list)  # ask / block: what is wrong / needed
    next_step: str | None = None  # "So geht es weiter: …"
    table_headers: list[str] = field(default_factory=list)
    rows: list[Row] = field(default_factory=list)
    footnotes: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)  # Hinweise (polished by the LLM)
    links: list[Link] = field(default_factory=list)
    paths: list[rw.PathBlock] = field(default_factory=list)
    ebitda: list[rw.EbitdaBlock] = field(default_factory=list)
    signature: list[str] = field(default_factory=list)
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


def _footer(run_id: str) -> list[str]:
    return [f"Referenz: {run_id}"]


def _finding_text(f: Finding) -> str:
    cells = ", ".join(str(r) for r in f.refs[:3])
    return f"{f.message_de} [{cells}]" if cells else f.message_de


def _status_key(r: CovenantResult) -> str:
    if r.status == "met":
        return "tight" if r.tight else "met"
    return "breached" if r.status == "breached" else "open"


def _strip_period(text: str) -> str:
    return text.strip().rstrip(".").strip()


# ── Result: lead box ─────────────────────────────────────────────────────────


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
    if tight is not None and tight.threshold is not None and tight.headroom is not None:
        bound = "min." if tight.operator == ">=" else "max."
        limit = limit_de(tight)
        room = headroom_de(tight).lstrip("-")
        how = f"verfehlt um {room}" if tight.status == "breached" else f"Spielraum {room}"
        out.append(
            f"Am engsten: {tight.name_de} ({tight.contract_name_de}) mit {value_de(tight)} bei "
            f"{bound} {limit} ({how})."
        )
        if tight.sensitivity:
            out.append(tight.sensitivity.text_de)
    for r in results:
        changed = [a for a in r.alternatives if a.status != r.status]
        for alt in changed:
            out.append(
                f"{r.name_de} ({r.contract_name_de}) hängt an einer im Plan nicht eindeutig "
                f"belegten Annahme; {alt.assumption_de}: {number_de(alt.value_rounded)}"
                f"{unit_suffix(r.unit)} ({alt_status_de(alt.status)})."
            )
    return out


def _tone(report: Report) -> Tone:
    if any(r.status == "breached" for r in report.results):
        return "bad"
    if any(r.status in ("not_computable", "manual_review") for r in report.results):
        return "warn"
    return "info"


# ── Result: Hinweise ─────────────────────────────────────────────────────────

_SENTENCE = re.compile(r"(?<=[a-zäöüß)])\.\s+(?=[A-ZÄÖÜ])")


def _note_head(text: str) -> tuple[str, str]:
    """ "Konsortialkredit (DVG): rest" → ("Konsortialkredit", "rest")."""
    head, sep, rest = text.partition(": ")
    if not sep or len(head) > 70:
        return "", text
    return strip_parenthetical(head), rest


def _short_note(note: ContractNote, rest: str) -> str:
    if note.kind == "no_covenants":  # first two sentences: the statement and the main reason
        sentences = _SENTENCE.split(rest)
        return ". ".join(_strip_period(s) for s in sentences[:2])
    return _strip_period(rest)


def _hints(report: Report, general: Sequence[Finding]) -> list[str]:
    out: list[str] = []
    warns = [_strip_period(f.message_de) for f in general
             if f.severity in (Severity.WARN, Severity.PARTIAL)]  # fmt: skip
    infos = [_strip_period(f.message_de) for f in general if f.severity == Severity.INFO]
    if warns:
        out.append("Achtung: " + "; ".join(warns) + ".")
    if infos:
        out.append("Eingabe: " + "; ".join(infos) + ".")
    by_contract: dict[str, tuple[str, list[str]]] = {}
    for note in report.contract_notes:
        head, rest = _note_head(note.text_de)
        name, texts = by_contract.setdefault(note.contract_id, (head, []))
        texts.append(_short_note(note, rest))
    for r in report.results:  # contract requirements on the plan (V2: approved plan)
        for f in r.findings:
            if f.code == ErrorCode.PLAN_STATUS_REQUIRED:
                name, texts = by_contract.setdefault(r.contract_id, (r.contract_name_de, []))
                text = _strip_period(rw.strip_label(f.message_de, r))
                if text not in texts:
                    texts.append(text)
    for cid in sorted(by_contract):
        name, texts = by_contract[cid]
        label = name or cid  # names only: ids like "V4" would reach the LLM (D21)
        out.append(f"{label}: " + "; ".join(t for t in texts if t) + ".")
    if len(out) > MAX_NOTES:  # keep the list short: the rest in one bullet
        out = [*out[: MAX_NOTES - 1], " ".join(out[MAX_NOTES - 1 :])]
    return out


# ── Result context ───────────────────────────────────────────────────────────


def _intro(h: CheckedInput | None) -> str:
    year = f" {h.plan_year}" if h else ""
    return f"die Covenant-Prüfung für den Wirtschaftsplan{year} ist abgeschlossen."


def _plan_footnote(report: Report) -> str:
    h = report.header
    sheets = rw.plan_sheets(report.results)
    text = f"Planwerte: {h.file if h and h.file else 'Wirtschaftsplan'}"
    if sheets:
        text += f" ({', '.join(sheets)})"
    if h is not None:
        status = plan_status_de(h.status)
        version = f" vom {h.version_date:%d.%m.%Y}" if h.version_date else ""
        text += (
            f" · {h.company or '–'} · Plan {h.plan_year} ({status}{version}) · Stichtag "
            f"{h.reporting_date:%d.%m.%Y} · Eingangsprüfungen bestanden"
        )
    return text


def result_context(
    report: Report,
    *,
    inbound: InboundMessage,
    extra_findings: Sequence[Finding],
    run_id: str,
    tight_ratio: Decimal,
) -> Context:
    h = report.header
    general = [f for f in [*extra_findings, *report.findings] if not f.affects]
    year = h.plan_year if h else ""
    rows: list[Row] = []
    footnotes: list[str] = []
    for n, r in enumerate(report.results, 1):
        rows.append(
            Row(
                contract=f"{r.contract_id} · {r.contract_name_de}",
                covenant=r.name_de,
                mark=rw.mark(n),
                value=value_de(r),
                threshold=threshold_de(r),
                headroom=minus(headroom_de(r)),
                met=met_de(r),
                status=_status_key(r),
                anchor=rw.anchor(r),
                highlight=r.covenant_id == report.tightest,
            )
        )
        footnotes.append(f"{rw.mark(n)} {rw.footnote(r)}")
    footnotes.append(_plan_footnote(report))
    blocks = [rw.path_block(r, _status_key(r), tight_ratio) for r in report.results]
    ebitda = rw.ebitda_blocks(report.results)
    links = [Link(b.anchor, f"{b.title}") for b in blocks]
    links += [Link(e.anchor, e.title) for e in ebitda]
    return Context(
        kind="result",
        salutation=salutation(inbound.from_name),
        intro=_intro(h),
        tone=_tone(report),
        summary=_summary(report),
        early_warning=EARLY_WARNING,
        table_headers=["Covenant", f"Plan {year}", "Grenze", "Spielraum", "Erfüllt"],
        rows=rows,
        footnotes=footnotes,
        notes=_hints(report, general),
        links=links,
        paths=blocks,
        ebitda=ebitda,
        next_step="Bei Rückfragen antworten Sie einfach auf diese Nachricht.",
        signature=[FOOTER_DETERMINISTIC],
        footer=_footer(run_id),
        run_id=run_id,
    )


# ── ASK / BLOCK / OTHER / ERROR ──────────────────────────────────────────────

_ASK_LEADS: dict[str, str] = {
    ErrorCode.ATTACHMENT_MULTIPLE: "Ich benötige noch eine Angabe: welche Datei ich prüfen soll.",
    ErrorCode.MAIL_TOO_LARGE: "Ihre Nachricht ist zu groß; ich benötige nur den Wirtschaftsplan.",
    ErrorCode.MAIL_UNPARSEABLE: "Ihre Nachricht konnte ich nicht lesen.",
    ErrorCode.XLSX_ENCRYPTED: "Ich benötige den Wirtschaftsplan ohne Kennwort.",
    ErrorCode.XLSX_CORRUPT: "Ich benötige eine lesbare Excel-Datei des Wirtschaftsplans.",
}
_ASK_DEFAULT = "Ich benötige noch den Wirtschaftsplan als Excel-Datei (.xlsx)."


def ask_context(
    findings: Sequence[Finding], *, inbound: InboundMessage, run_id: str, wait_days: float
) -> Context:
    lead = next((_ASK_LEADS[f.code] for f in findings if f.code in _ASK_LEADS), _ASK_DEFAULT)
    return Context(
        kind="ask",
        salutation=salutation(inbound.from_name),
        intro="vielen Dank für Ihre Nachricht. Für die Covenant-Prüfung fehlt noch etwas.",
        tone="warn",
        summary=[lead],
        items=[_finding_text(f) for f in findings],
        next_step="Antworten Sie einfach auf diese Mail mit dem Wirtschaftsplan (.xlsx) im "
        f"Anhang; die Prüfung startet dann automatisch. Ich warte {wait_days:g} Tage auf Ihre "
        "Antwort.",
        footer=_footer(run_id),
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
    shown = stopping(findings) or list(findings)
    name = file or "im Anhang"
    return Context(
        kind="block",
        salutation=salutation(inbound.from_name),
        intro="vielen Dank für Ihre Nachricht und den Wirtschaftsplan.",
        tone="bad",
        summary=[
            f"Die Datei {name} kann ich so nicht auswerten: Ein Covenant-Ergebnis bestätige ich "
            "erst, wenn die Eingangsprüfungen bestanden sind."
        ],
        items=[_finding_text(f) for f in shown],
        next_step="Antworten Sie einfach auf diese Mail mit dem korrigierten Plan im Anhang; die "
        f"Prüfung startet dann automatisch neu. Ich warte {wait_days:g} Tage auf Ihre Antwort.",
        footer=_footer(run_id),
        run_id=run_id,
    )


def other_context(*, inbound: InboundMessage, run_id: str, question: bool) -> Context:
    if question:
        lead = "Rückfragen zu früheren Ergebnissen kann ich noch nicht beantworten."
        step = (
            "Den Rechenweg finden Sie in meiner Ergebnis-Mail; für eine neue Prüfung senden Sie "
            "mir bitte den Wirtschaftsplan als Excel-Datei (.xlsx)."
        )
    else:
        lead = (
            "Ich bin Finn und prüfe die Financial Covenants Ihrer Kreditverträge anhand des "
            "Wirtschaftsplans. Andere Anfragen kann ich nicht bearbeiten."
        )
        step = "Für eine Prüfung senden Sie mir bitte den Wirtschaftsplan als Excel-Datei (.xlsx)."
    return Context(
        kind="other",
        salutation=salutation(inbound.from_name),
        intro="vielen Dank für Ihre Nachricht.",
        tone="neutral",
        summary=[lead],
        next_step=step,
        footer=_footer(run_id),
        run_id=run_id,
    )


def error_context(*, inbound: InboundMessage, run_id: str) -> Context:
    return Context(
        kind="error",
        salutation=salutation(inbound.from_name),
        intro="vielen Dank für Ihre Nachricht.",
        tone="bad",
        summary=[
            "Bei der Verarbeitung ist leider ein unerwarteter Fehler aufgetreten; zu dieser "
            "Nachricht erhalten Sie kein Ergebnis."
        ],
        next_step="Das Betriebsteam ist informiert. Bitte nennen Sie bei Rückfragen die Referenz "
        f"{run_id}.",
        footer=_footer(run_id),
        run_id=run_id,
    )


# ── Plain-text helpers for the template ──────────────────────────────────────


def _text_table(headers: list[str], rows: list[Row]) -> str:
    from finn.plan.render import text_table

    cells = [
        [
            f"{r.contract.replace(' · ', ' ', 1)} · {r.covenant}{r.mark}",
            r.value,
            r.threshold,
            r.headroom,
            r.met,
        ]
        for r in rows
    ]
    return "\n".join(text_table(headers, cells, max_width=70))


_env.globals["text_table"] = _text_table
_env.globals["path_text"] = rw.path_text
_env.globals["ebitda_text"] = rw.ebitda_text


# ── Polish (D21) ─────────────────────────────────────────────────────────────

_NUM = re.compile(r"(?<![\w{])[-−]?\d[\d.,]*\d(?![\w}])|(?<![\w{])\d(?![\w}])")
_PH = re.compile(r"\{\{[A-Z]+\d+\}\}")

POLISH_SYSTEM = (
    (Path(__file__).parent / "prompts" / "polish.md").read_text(encoding="utf-8").rstrip()
)


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
