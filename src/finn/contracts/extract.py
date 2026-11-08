"""Contract onboarding (D2–D4): PDF → one LLM extraction on the full text → draft profile, one
correction round, quote repair, a second review call; re-extraction only when the PDF hash or the
prompt version changes."""

import datetime as dt
import hashlib
import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import structlog
from pydantic import BaseModel, ValidationError

from finn.config import get_settings
from finn.contracts import completeness
from finn.contracts.draft import DraftError, ProfileDraft, ReviewResult, to_profile
from finn.contracts.pdf_text import (
    DEFAULT_CACHE_DIR,
    ContractText,
    load_contract_text,
    render_pages,
)
from finn.contracts.validate import ValidationReport, repair_quotes, validate_profile
from finn.llm import client
from finn.models.contract import ContractProfile

log = structlog.get_logger(__name__)

PROMPT_DIR = Path(__file__).parent / "prompts"
PROMPT_VERSION = "v1"
# Repayment schedule entries extracted per loan (plan year and its neighbours).
SCHEDULE_YEARS = (2026, 2027, 2028)

Complete = Callable[..., Any]  # signature of finn.llm.client.complete_structured


def load_prompt(name: str) -> str:
    return (PROMPT_DIR / f"{name}.{PROMPT_VERSION}.md").read_text(encoding="utf-8")


def prompt_version() -> str:
    """`v1-<hash>`: the hash covers both prompts and the draft schema, so any change re-extracts."""
    digest = hashlib.sha256()
    for name in ("extraction", "review"):
        digest.update(load_prompt(name).encode("utf-8"))
    for schema in (ProfileDraft, ReviewResult):
        digest.update(json.dumps(schema.model_json_schema(), sort_keys=True).encode("utf-8"))
    return f"{PROMPT_VERSION}-{digest.hexdigest()[:8]}"


def contract_id_for(pdf: Path) -> str:
    """`Vertrag_4_Konsortialkredit.pdf` → `V4`; other names → the file stem."""
    m = re.match(r"Vertrag_(\d+)", pdf.stem)
    return f"V{m[1]}" if m else pdf.stem


def extraction_system_prompt() -> str:
    years = ", ".join(str(y) for y in SCHEDULE_YEARS)
    return load_prompt("extraction").replace("{schedule_years}", years)


def _user_prompt(contract_id: str, text: ContractText) -> str:
    return (
        f"Contract id: {contract_id}\nFile: {text.file}\nPages: {text.page_count}\n\n"
        f"{render_pages(text.pages)}"
    )


@dataclass
class Extracted:
    profile: ContractProfile
    text: ContractText
    quote_fixes: list[str] = field(default_factory=list)
    retried: bool = False


def extract_contract(
    pdf: Path,
    *,
    contract_id: str | None = None,
    cache_dir: Path | None = DEFAULT_CACHE_DIR,
    model: str | None = None,
    complete: Complete | None = None,
) -> Extracted:
    """Extract one contract into a validated draft ContractProfile (raises on failure)."""
    call = complete or client.complete_structured
    cid = contract_id or contract_id_for(pdf)
    text = load_contract_text(pdf, cache_dir)
    model_name = model or get_settings().openai_model
    system = extraction_system_prompt()
    user = _user_prompt(cid, text)
    meta = {
        "model": model_name,
        "prompt_version": prompt_version(),
        "extracted_at": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
        "source_sha256": text.sha256,
    }
    kwargs: dict[str, Any] = {
        "schema": ProfileDraft,
        "purpose": "contract_extraction",
        "trace_id": f"onboarding-{cid}",
        "model": model_name,
    }

    draft: ProfileDraft = call(system=system, user=user, **kwargs)
    retried = False
    try:
        profile = _convert(draft, cid, text, meta)
    except (DraftError, ValidationError) as exc:
        log.warning("draft_invalid_retry", contract=cid, error=_short(exc))
        retried = True
        correction = (
            f"{user}\n\n<previous_answer>\n{draft.model_dump_json()}\n</previous_answer>\n"
            f"<validation_errors>\n{_short(exc, 4000)}\n</validation_errors>\n"
            "Your previous answer failed validation. Return the complete corrected profile."
        )
        draft = call(system=system, user=correction, **kwargs)
        profile = _convert(draft, cid, text, meta)
    profile, fixes = repair_quotes(profile, text)
    if fixes:
        notes = "\n".join(
            filter(None, [profile.review.notes, *("Zitat korrigiert: " + f for f in fixes)])
        )
        profile = profile.model_copy(
            update={"review": profile.review.model_copy(update={"notes": notes})}
        )
    return Extracted(profile=profile, text=text, quote_fixes=fixes, retried=retried)


def _convert(
    draft: ProfileDraft, cid: str, text: ContractText, meta: dict[str, str]
) -> ContractProfile:
    notes = "\n".join(draft.extraction_notes) or None
    return to_profile(
        draft,
        contract_id=cid,
        file=text.file,
        sha256=text.sha256,
        pages=text.page_count,
        extraction=meta,
        notes=notes,
    )


def review_profile(
    profile: ContractProfile,
    text: ContractText,
    *,
    model: str | None = None,
    complete: Complete | None = None,
) -> ReviewResult:
    """Second LLM pass: full text + draft profile → omissions / modifying clauses."""
    call = complete or client.complete_structured
    user = (
        f"{render_pages(text.pages)}\n\n<draft_profile>\n"
        f"{profile.model_dump_json(indent=1, exclude={'review', 'extraction', 'document'})}\n"
        "</draft_profile>"
    )
    result: ReviewResult = call(
        system=load_prompt("review"),
        user=user,
        schema=ReviewResult,
        purpose="contract_review",
        trace_id=f"onboarding-{profile.contract_id}",
        model=model or get_settings().openai_model,
    )
    return result


# ── Batch (CLI) ──────────────────────────────────────────────────────────────

Status = Literal["extracted", "skipped", "failed"]


@dataclass
class ContractRun:
    contract_id: str
    pdf: Path
    status: Status
    reason: str = ""
    profile: ContractProfile | None = None
    validation: ValidationReport | None = None
    uncovered: list[completeness.Hit] = field(default_factory=list)
    review: ReviewResult | None = None
    quote_fixes: list[str] = field(default_factory=list)


def is_current(path: Path, sha256: str, version: str) -> bool:
    """The profile at `path` was extracted from this PDF content with this prompt version."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    extraction = data.get("extraction") or {}
    return bool(
        extraction.get("source_sha256") == sha256 and extraction.get("prompt_version") == version
    )


def run_extract(
    contracts_dir: Path,
    out_dir: Path,
    *,
    only: list[str] | None = None,
    force: bool = False,
    with_review: bool = True,
    cache_dir: Path | None = DEFAULT_CACHE_DIR,
    model: str | None = None,
    complete: Complete | None = None,
) -> list[ContractRun]:
    """Extract every contract PDF in `contracts_dir` (or `only` these ids) into `out_dir`."""
    runs: list[ContractRun] = []
    version = prompt_version()
    out_dir.mkdir(parents=True, exist_ok=True)
    for pdf in sorted(contracts_dir.glob("*.pdf")):
        cid = contract_id_for(pdf)
        if only and cid not in only:
            continue
        target = out_dir / f"{cid}.json"
        text = load_contract_text(pdf, cache_dir)
        if not force and is_current(target, text.sha256, version):
            runs.append(ContractRun(cid, pdf, "skipped", "PDF und Prompt unverändert"))
            continue
        try:
            extracted = extract_contract(
                pdf, contract_id=cid, cache_dir=cache_dir, model=model, complete=complete
            )
        except (client.LLMUnavailable, DraftError, ValidationError) as exc:
            log.error("extraction_failed", contract=cid, error=_short(exc))
            runs.append(ContractRun(cid, pdf, "failed", _short(exc, 500)))
            continue
        profile = extracted.profile
        run = ContractRun(cid, pdf, "extracted", profile=profile, quote_fixes=extracted.quote_fixes)
        run.validation = validate_profile(profile, extracted.text)
        run.uncovered = completeness.uncovered(profile, extracted.text)
        if with_review:
            try:
                run.review = review_profile(profile, extracted.text, model=model, complete=complete)
            except client.LLMUnavailable as exc:
                log.warning("review_failed", contract=cid, error=_short(exc))
        write_profile(target, profile)
        write_review(out_dir / f"{cid}.review.json", run, version)
        runs.append(run)
    return runs


def write_profile(path: Path, profile: ContractProfile) -> None:
    data = profile.model_dump(mode="json")
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def write_review(path: Path, run: ContractRun, version: str) -> None:
    payload: dict[str, Any] = {
        "contract_id": run.contract_id,
        "prompt_version": version,
        "created_at": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
        "profile_extraction": run.profile.extraction.model_dump()
        if run.profile and run.profile.extraction
        else None,
        "llm_review": _dump(run.review),
        "validation": run.validation.lines() if run.validation else [],
        "quote_fixes": run.quote_fixes,
        "completeness_uncovered": [h.describe() for h in run.uncovered],
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _dump(model: BaseModel | None) -> Any:
    return model.model_dump(mode="json") if model is not None else None


def _short(exc: Exception, limit: int = 300) -> str:
    text = str(exc)
    return text if len(text) <= limit else text[: limit - 1] + "…"
