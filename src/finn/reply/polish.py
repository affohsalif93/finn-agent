"""Optional LLM polish of the result mail (D21): the model sees placeholders, never numbers."""

from collections.abc import Sequence
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from finn.errors import ErrorCode, Finding, finding
from finn.llm import client
from finn.llm.redact import Masker, placeholders
from finn.models.mail import InboundMessage
from finn.models.result import Report
from finn.reply.number_check import allowed_values, unknown_numbers
from finn.reply.render import Context

POLISH_SYSTEM = (
    (Path(__file__).parent / "prompts" / "polish.md").read_text(encoding="utf-8").rstrip()
)


class PolishAnswer(BaseModel):
    model_config = ConfigDict(extra="forbid")

    summary: list[str]
    notes: list[str]


def polish(
    ctx: Context,
    names: Sequence[str],
    *,
    trace_id: str | None = None,
    session_id: str | None = None,
    record: client.Recorder | None = None,
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
        or any(placeholders(a) != placeholders(b) for a, b in pairs)
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


def names_for_masking(report: Report, inbound: InboundMessage) -> list[str]:
    names: list[str] = []
    if report.header and report.header.company:
        names.append(report.header.company)
    if inbound.from_name:
        names.append(inbound.from_name)
    for r in report.results:
        names += [r.contract_name_de, r.name_de]
    return names
