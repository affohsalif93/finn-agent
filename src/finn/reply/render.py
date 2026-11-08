"""Report → German reply mail: code fills the Jinja2 templates (result, ask, block, other, error);
the optional LLM polish (`polish.py`) only rewrites lead sentences and notes (D21)."""

import hashlib
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

from jinja2 import Environment, FileSystemLoader, StrictUndefined, select_autoescape

from finn.errors import ErrorCode, Finding, Severity, stopping
from finn.mail.thread import reply_references, reply_subject
from finn.models.mail import InboundMessage, ReplyDraft, ReplyKind
from finn.models.result import CheckedInput, ContractNote, CovenantResult, Report
from finn.models.run import RenderInput
from finn.numbers import number_de, unit_suffix
from finn.reply.calc_path import (
    EbitdaBlock,
    PathBlock,
    anchor,
    ebitda_blocks,
    footnote,
    mark,
    path_block,
    plan_sheets,
    strip_label,
)
from finn.reply.text_layout import ebitda_text, path_text, result_table
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
    paths: list[PathBlock] = field(default_factory=list)
    ebitda: list[EbitdaBlock] = field(default_factory=list)
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
                text = _strip_period(strip_label(f.message_de, r))
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
    sheets = plan_sheets(report.results)
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
                mark=mark(n),
                value=value_de(r),
                threshold=threshold_de(r),
                headroom=minus(headroom_de(r)),
                met=met_de(r),
                status=_status_key(r),
                anchor=anchor(r),
                highlight=r.covenant_id == report.tightest,
            )
        )
        footnotes.append(f"{mark(n)} {footnote(r)}")
    footnotes.append(_plan_footnote(report))
    blocks = [path_block(r, _status_key(r), tight_ratio) for r in report.results]
    ebitda = ebitda_blocks(report.results)
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


def context_for(
    inp: RenderInput, *, run_id: str, tight_ratio: Decimal, wait_days: float
) -> Context:
    """The template context for the reply kind (result, ask, block, other, error)."""
    if inp.kind == "result":
        assert inp.report is not None
        return result_context(
            inp.report,
            inbound=inp.reply_to,
            extra_findings=inp.findings,
            run_id=run_id,
            tight_ratio=tight_ratio,
        )
    if inp.kind == "ask":
        return ask_context(inp.findings, inbound=inp.reply_to, run_id=run_id, wait_days=wait_days)
    if inp.kind == "block":
        return block_context(
            inp.findings,
            inbound=inp.reply_to,
            run_id=run_id,
            file=inp.file,
            header=inp.header,
            wait_days=wait_days,
        )
    if inp.kind == "other":
        return other_context(inbound=inp.reply_to, run_id=run_id, question=inp.question)
    return error_context(inbound=inp.reply_to, run_id=run_id)


_env.globals["text_table"] = result_table
_env.globals["path_text"] = path_text
_env.globals["ebitda_text"] = ebitda_text


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
