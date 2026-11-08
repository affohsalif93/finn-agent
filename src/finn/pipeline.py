"""Every step of a mail run as a plain function (ARCHITECTURE §12, D27).

The direct runner (`process_eml`, CLI `finn process`) and the Temporal activities call the same
functions; Temporal adds durability, not logic. Steps write their audit files to the run folder.
"""

import traceback
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import structlog
from pydantic import BaseModel

from finn import runs
from finn.calc import engine
from finn.config import Settings
from finn.contracts.store import load_profiles
from finn.errors import ErrorCode, Finding, Severity, stopping
from finn.llm.client import Recorder
from finn.llm.redact import Pseudonyms, clean_body
from finn.mail import gate
from finn.mail.connector import MailConnector
from finn.mail.parse import inbound_from_bytes, parse_eml
from finn.mail.thread import thread_id
from finn.models.mail import (
    Classified,
    InputCheck,
    ParsedMessage,
    ReplyDraft,
    ReplyKind,
    SentRecord,
    ThreadMessage,
)
from finn.models.result import Report
from finn.models.run import (
    CalcInput,
    ClassifyInput,
    FailureInput,
    ParseInput,
    PlanInput,
    PlanReadResult,
    RenderInput,
    SendInput,
)
from finn.plan import read_plan
from finn.plan.checks import check_plan_year
from finn.reply import render
from finn.reply.polish import names_for_masking, polish
from finn.request import intent

log = structlog.get_logger(__name__)

_FORWARD = ("wg:", "fw:", "fwd:", "wtr:")


# ── helpers ──────────────────────────────────────────────────────────────────


def _recorder(run_dir: str) -> Recorder:
    """LLM call log (`llm_calls.jsonl`): prompts are already pseudonymised / masked."""

    def record(system: str, user: str, error: Exception | None, answer: BaseModel | None) -> None:
        runs.append_jsonl(
            run_dir,
            "llm_calls.jsonl",
            {
                "at": datetime.now(UTC).isoformat(),
                "system": system,
                "user": user,
                "answer": answer.model_dump() if answer else None,
                "error": f"{type(error).__name__}: {error}" if error else None,
            },
        )

    return record


def contract_infos(settings: Settings) -> list[intent.ContractInfo]:
    profiles, _ = load_profiles(settings.profiles_dir, None)
    return [intent.ContractInfo(p.contract_id, p.name_de, bool(p.covenants)) for p in profiles]


# ── steps ────────────────────────────────────────────────────────────────────


def parse_message(inp: ParseInput, settings: Settings) -> ParsedMessage:
    """Saved `.eml` → body (cleaned, pseudonymised) + attachments in the mail's folder."""
    inbound = inp.inbound
    if inbound.raw_eml_path is None:
        raise ValueError("raw_eml_path missing (the poller saves the mail before the start)")
    eml = Path(inbound.raw_eml_path)
    if inbound.too_large:
        body = None
    else:
        body = parse_eml(
            eml,
            eml.parent / "attachments",
            max_attachment_bytes=int(settings.max_mail_mb * 1024 * 1024),
        )
    store = Pseudonyms(runs.read_json(inp.run_dir, "pseudonyms.json"))
    forwarded = inbound.subject.lower().startswith(_FORWARD)
    cleaned = clean_body(body.body_text, forwarded=forwarded) if body else ""
    names = [inbound.from_name or ""]
    text = store.pseudonymise(cleaned, names=names)
    subject = store.pseudonymise(inbound.subject, names=names)
    runs.write_json(inp.run_dir, "pseudonyms.json", store.mapping)
    attachments = body.attachments if body else []
    parsed = ParsedMessage(
        inbound=inbound.model_copy(update={"attachment_paths": [a.path for a in attachments]}),
        body_text=text,
        thread=[
            ThreadMessage(
                message_id=inbound.message_id,
                date=inbound.date,
                subject=subject,
                body_text=text,
                attachments=[f"{a.filename} ({a.kind})" for a in attachments],
            )
        ],
        attachments=attachments,
        links=body.links if body else [],
        unparseable=body.unparseable if body else False,
        run_dir=inp.run_dir,
    )
    files = ", ".join(f"{a.filename} [{a.kind}]" for a in attachments) or "keine"
    runs.log_line(inp.run_dir, "parse", f"{inbound.message_id}: Anhänge {files}")
    return parsed


def classify_intent(inp: ClassifyInput, settings: Settings) -> Classified:
    """LLM intent on the pseudonymised thread (rule fallback) + the intent's input contract."""
    contracts = contract_infos(settings)
    req = intent.classify(
        inp.thread,
        contracts,
        trace_id=inp.workflow_id,
        session_id=inp.session_id,
        record=_recorder(inp.run_dir),
    )
    has_plan = any(a.kind in intent.PLAN_KINDS for m in inp.thread for a in m.attachments)
    if req.intent == "other" and has_plan:  # Finn has one job; a workbook means: check it
        req = req.model_copy(
            update={"intent": "covenant_check", "confidence": min(req.confidence, 0.5)}
        )
    check = (
        intent.check_input(inp.thread, settings.max_mail_mb)
        if req.intent in ("covenant_check", "follow_up_input")
        else InputCheck(status="ok")
    )
    notes = intent.intent_findings(req, contracts)
    check = check.model_copy(update={"findings": [*check.findings, *notes]})
    runs.write_json(
        inp.run_dir,
        "request.json",
        {"request": req.model_dump(mode="json"), "input": check.model_dump(mode="json")},
    )
    runs.log_line(
        inp.run_dir,
        "intent",
        f"{req.intent} ({req.source}, {req.confidence:.2f}); Eingabe {check.status}"
        + (f"; Plan {check.plan.filename}" if check.plan else ""),
    )
    return Classified(request=req, input=check)


def load_and_validate_plan(inp: PlanInput) -> PlanReadResult:
    """Plan reader + checks (§4.4) + plan year vs. request (PLAN_YEAR_MISMATCH)."""
    result = read_plan(Path(inp.attachment.path))
    plan = result.plan
    if plan is not None and (mismatch := check_plan_year(plan, inp.request.plan_year)):
        result = result.model_copy(update={"findings": [*result.findings, *mismatch]})
    runs.write_json(inp.run_dir, "plan_inputs.json", plan.model_dump(mode="json") if plan else {})
    runs.write_json(
        inp.run_dir, "checks.json", [f.model_dump(mode="json") for f in result.findings]
    )
    stops = stopping(result.findings)
    runs.log_line(
        inp.run_dir,
        "plan",
        f"{inp.attachment.filename}: {len(result.findings)} Befunde, "
        + (f"{len(stops)} blockierend" if stops else "keine blockierenden"),
    )
    return result


def calculate(inp: CalcInput, settings: Settings) -> Report:
    profiles, profile_findings = load_profiles(settings.profiles_dir, settings.contracts_dir)
    report = engine.calculate(
        inp.plan_result.plan,
        profiles,
        inp.plan_result.findings,
        tight_ratio=settings.tight_headroom_ratio,
        profile_findings=profile_findings,
        file=inp.file,
        contract_filter=inp.request.contract_filter,
    )
    runs.write_json(inp.run_dir, "results.json", report)
    summary = ", ".join(
        f"{r.covenant_id}={r.value_rounded if r.value_rounded is not None else '–'} {r.status}"
        for r in report.results
    )
    runs.log_line(inp.run_dir, "calculate", f"{report.overall}: {summary}")
    return report


def render_reply(inp: RenderInput, settings: Settings) -> ReplyDraft:
    run_id = runs.run_id(inp.run_dir)
    notes: list[Finding] = []
    used_template = True
    ctx = render.context_for(
        inp,
        run_id=run_id,
        tight_ratio=settings.tight_headroom_ratio,
        wait_days=settings.wait_for_input_days,
    )
    if inp.kind == "result" and settings.reply_llm_polish:
        assert inp.report is not None
        polished, notes = polish(
            ctx,
            names_for_masking(inp.report, inp.reply_to),
            trace_id=inp.workflow_id,
            session_id=inp.session_id,
            record=_recorder(inp.run_dir),
        )
        used_template = polished is ctx
        ctx = polished
    draft = render.draft(
        ctx,
        inbound=inp.reply_to,
        workflow_id=inp.workflow_id,
        seq=inp.seq,
        from_addr=settings.smtp_user or settings.imap_user or "finn@finn.invalid",
        used_template=used_template,
        notes=notes,
    )
    runs.write_reply(inp.run_dir, inp.seq, inp.kind, draft.html, draft.text)
    runs.log_line(
        inp.run_dir,
        "render",
        f"{inp.kind} #{inp.seq} ({'Vorlage' if used_template else 'LLM-Text geprüft'})"
        + (f"; {notes[0].code}" if notes else ""),
    )
    return draft


def send_reply(inp: SendInput, connector: MailConnector) -> SentRecord:
    """Idempotent (§7.3): local sent marker → Sent folder (`was_sent`) → send → marker."""
    draft = inp.draft
    marker = runs.sent_marker(inp.run_dir, draft.message_id)
    if marker.is_file():
        return SentRecord.model_validate_json(marker.read_text(encoding="utf-8")).model_copy(
            update={"skipped_already_sent": True}
        )
    if connector.was_sent(draft.message_id):
        record = SentRecord(
            message_id=draft.message_id, sent_at=datetime.now(UTC), skipped_already_sent=True
        )
        runs.log_line(inp.run_dir, "send", f"{ErrorCode.SEND_ALREADY_DONE}: {draft.message_id}")
    else:
        connector.send_reply(draft)
        record = SentRecord(message_id=draft.message_id, sent_at=datetime.now(UTC))
        runs.log_line(inp.run_dir, "send", f"{draft.kind} an {draft.to}: {draft.message_id}")
    runs.write_sent_marker(inp.run_dir, record)
    return record


def record_failure(inp: FailureInput) -> None:
    runs.write_error(inp.run_dir, inp.error)
    runs.log_line(inp.run_dir, "error", inp.error.splitlines()[0] if inp.error else "Fehler")


# ── direct runner (§7.5) ─────────────────────────────────────────────────────


@dataclass
class DirectResult:
    run_dir: Path
    rejected: gate.Reject | None = None
    drafts: list[ReplyDraft] = field(default_factory=list)
    sent: list[SentRecord] = field(default_factory=list)
    report: Report | None = None


def process_eml(
    path: Path,
    settings: Settings,
    *,
    send: bool = False,
    connector: MailConnector | None = None,
    check_gate: bool = True,
) -> DirectResult:
    """Run one `.eml` through all steps without Temporal. Waiting (ASK / BLOCK) ends the run
    after the question; a follow-up is a new `finn process` call."""
    raw = path.read_bytes()
    inbound = inbound_from_bytes(raw, mailbox=settings.finn_address, keep_raw=False)
    run_dir = runs.run_dir_for(settings.runs_dir, inbound.message_id, inbound.date)
    if check_gate:
        decision = gate.check(inbound, settings)
        if isinstance(decision, gate.Reject):
            return DirectResult(run_dir=run_dir, rejected=decision)
    eml = runs.save_incoming(run_dir, raw)
    inbound = inbound.model_copy(update={"raw_eml_path": str(eml)})
    workflow_id = inbound.message_id
    session = thread_id(inbound)
    result = DirectResult(run_dir=run_dir)

    def finish(kind: ReplyKind, **fields: Any) -> None:
        inp = RenderInput(
            kind=kind,
            reply_to=inbound,
            run_dir=str(run_dir),
            workflow_id=workflow_id,
            session_id=session,
            seq=1,
            **fields,
        )
        draft = render_reply(inp, settings)
        result.drafts.append(draft)
        if send:
            if connector is None:
                raise ValueError("send=True needs a connector")
            result.sent.append(send_reply(SendInput(draft=draft, run_dir=str(run_dir)), connector))

    try:
        parsed = parse_message(ParseInput(inbound=inbound, run_dir=str(run_dir)), settings)
        classified = classify_intent(
            ClassifyInput(
                thread=[parsed], run_dir=str(run_dir), workflow_id=workflow_id, session_id=session
            ),
            settings,
        )
        req = classified.request
        if req.intent in ("other", "question_about_result"):
            finish("other", question=req.intent != "other")
            return result
        plan_file = classified.input.plan
        if classified.input.status == "ask" or plan_file is None:
            finish("ask", findings=stopping(classified.input.findings))
            return result
        plan_result = load_and_validate_plan(
            PlanInput(attachment=plan_file, request=req, run_dir=str(run_dir))
        )
        stops = stopping(plan_result.findings)
        if stops:
            blocked = any(f.severity == Severity.BLOCK for f in stops)
            finish("block" if blocked else "ask", findings=stops, file=plan_file.filename)
            return result
        report = calculate(
            CalcInput(
                plan_result=plan_result, request=req, file=plan_file.filename, run_dir=str(run_dir)
            ),
            settings,
        )
        result.report = report
        finish("result", report=report, findings=classified.input.findings)
    except Exception:
        record_failure(FailureInput(run_dir=str(run_dir), error=traceback.format_exc()))
        finish("error")
    return result
