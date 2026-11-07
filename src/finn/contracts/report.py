"""Text output for `finn contracts extract` (per contract: status, validation, completeness,
review issues; token usage and an estimated cost)."""

from collections.abc import Sequence
from decimal import Decimal

from finn.contracts.extract import ContractRun
from finn.llm.client import CallUsage

# USD per 1M tokens (input, cached input, output); developers.openai.com model pages, Nov 2026.
# Only for the cost estimate printed after an extraction run.
PRICES_USD: dict[str, tuple[Decimal, Decimal, Decimal]] = {
    "gpt-6.1-sol": (Decimal("2"), Decimal("0.1"), Decimal("10")),
    "gpt-6.1-terra": (Decimal("1"), Decimal("0.05"), Decimal("5")),
    "gpt-6-luna": (Decimal("0.1"), Decimal("0.01"), Decimal("0.5")),
}


def estimate_cost(usage: Sequence[CallUsage]) -> Decimal | None:
    total = Decimal(0)
    for u in usage:
        price = next((p for m, p in PRICES_USD.items() if u.model.startswith(m)), None)
        if price is None:
            return None
        fresh = u.input_tokens - u.cached_input_tokens
        total += (
            fresh * price[0] + u.cached_input_tokens * price[1] + u.output_tokens * price[2]
        ) / Decimal(1_000_000)
    return total


def render_extract_runs(runs: Sequence[ContractRun], usage: Sequence[CallUsage]) -> str:
    lines: list[str] = []
    for run in runs:
        if run.status != "extracted" or run.profile is None:
            lines.append(f"{run.contract_id}: {run.status} – {run.reason}")
            continue
        p = run.profile
        covenants = ", ".join(c.covenant_id for c in p.covenants) or "keine Covenants"
        lines.append(f"{run.contract_id}: extrahiert (draft) – {covenants}")
        if run.validation is not None:
            lines += [f"  {x}" for x in run.validation.lines()]
        lines += [f"  Zitat korrigiert: {f}" for f in run.quote_fixes]
        hits = run.uncovered
        lines.append(f"  Vollständigkeit: {len(hits)} Kennzahl-Erwähnung(en) ohne Zuordnung")
        lines += [f"    {h.describe()}" for h in hits]
        if run.review is not None:
            lines.append(f"  Review: {len(run.review.issues)} Hinweis(e) – {run.review.summary}")
            for issue in run.review.issues:
                where = f" S. {issue.page}" if issue.page else ""
                lines.append(f"    [{issue.severity}] {issue.kind}{where}: {issue.description}")
    if usage:
        tokens_in = sum(u.input_tokens for u in usage)
        tokens_out = sum(u.output_tokens for u in usage)
        reasoning = sum(u.reasoning_tokens for u in usage)
        cost = estimate_cost(usage)
        shown = f"≈ {cost:.2f} USD" if cost is not None else "Preis unbekannt"
        lines.append(
            f"LLM: {len(usage)} Aufrufe, {tokens_in} Input- / {tokens_out} Output-Tokens "
            f"(davon {reasoning} Reasoning), {shown}"
        )
    return "\n".join(lines)
