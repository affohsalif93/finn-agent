"""Extraction with a mocked LLM (canned drafts from the gold profiles), batch run and CLI."""

import json
import shutil
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from finn.cli import app
from finn.contracts import extract
from finn.contracts.draft import ProfileDraft, ReviewIssue, ReviewResult, from_profile
from finn.contracts.store import load_profiles
from finn.llm import client
from finn.models.contract import ContractProfile

ROOT = Path(__file__).resolve().parents[3]
MATERIAL = ROOT / "material"
GOLD = ROOT / "tests" / "fixtures" / "profiles_gold"
V4_PDF = MATERIAL / "Vertrag_4_Konsortialkredit.pdf"


def by_id(profiles: list[ContractProfile], cid: str) -> ContractProfile:
    return next(p for p in profiles if p.contract_id == cid)


class FakeLLM:
    """Stands in for `complete_structured`: returns queued drafts, records the prompts."""

    def __init__(self, drafts: list[ProfileDraft | Exception]) -> None:
        self.drafts = drafts
        self.calls: list[dict[str, Any]] = []

    def __call__(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if kwargs["schema"] is ReviewResult:
            issue = ReviewIssue(
                kind="other",
                severity="low",
                covenant_id=None,
                page=7,
                quote=None,
                description="Hinweis",
                suggested_fix="–",
            )
            return ReviewResult(issues=[issue], summary="Ein Hinweis.")
        draft = self.drafts.pop(0)
        if isinstance(draft, Exception):
            raise draft
        return draft


def comparable(profile: ContractProfile) -> dict[str, Any]:
    data = profile.model_dump(mode="json")
    data.pop("review")
    data.pop("extraction")
    return data


@pytest.fixture
def v4_gold(gold_profiles: list[ContractProfile]) -> ContractProfile:
    return by_id(gold_profiles, "V4")


def test_extract_fills_metadata_and_draft_status(v4_gold: ContractProfile, tmp_path: Path) -> None:
    llm = FakeLLM([from_profile(v4_gold, notes=["Fälligkeit prüfen"])])
    result = extract.extract_contract(V4_PDF, cache_dir=tmp_path, complete=llm)

    p = result.profile
    assert comparable(p) == comparable(v4_gold)
    assert p.review.status == "draft"
    assert p.review.reviewed_by is None
    assert p.review.notes == "Fälligkeit prüfen"
    assert p.extraction is not None
    assert p.extraction.source_sha256 == v4_gold.document.sha256
    assert p.extraction.prompt_version == extract.prompt_version()
    assert p.extraction.model
    assert not result.retried

    (call,) = llm.calls
    assert call["schema"] is ProfileDraft
    assert call["purpose"] == "contract_extraction"
    assert call["trace_id"] == "onboarding-V4"
    assert "Contract id: V4" in call["user"]
    assert call["user"].count("<page number=") == 13
    assert "2026, 2027, 2028" in call["system"]
    assert "{schedule_years}" not in call["system"]


def test_invalid_draft_is_sent_back_once(v4_gold: ContractProfile, tmp_path: Path) -> None:
    good = from_profile(v4_gold)
    bad = good.model_copy(update={"has_financial_covenants": False})  # violates the invariant
    llm = FakeLLM([bad, good])
    result = extract.extract_contract(V4_PDF, cache_dir=tmp_path, complete=llm)
    assert result.retried
    assert len(llm.calls) == 2
    assert "<validation_errors>" in llm.calls[1]["user"]
    assert "PROFILE_INVARIANT" in llm.calls[1]["user"]
    assert comparable(result.profile) == comparable(v4_gold)


def test_near_miss_quote_is_repaired(v4_gold: ContractProfile, tmp_path: Path) -> None:
    draft = from_profile(v4_gold)
    sources = [s.model_copy(update={"page": 6}) if s.id == "S_CURE" else s for s in draft.sources]
    llm = FakeLLM([draft.model_copy(update={"sources": sources})])
    result = extract.extract_contract(V4_PDF, cache_dir=tmp_path, complete=llm)
    assert result.profile.source("S_CURE").page == 7  # type: ignore[union-attr]
    assert len(result.quote_fixes) == 1
    assert "Zitat korrigiert" in (result.profile.review.notes or "")


def test_run_extract_writes_skips_and_forces(v4_gold: ContractProfile, tmp_path: Path) -> None:
    contracts, out, cache = tmp_path / "pdf", tmp_path / "out", tmp_path / "cache"
    contracts.mkdir()
    shutil.copy(V4_PDF, contracts / V4_PDF.name)

    llm = FakeLLM([from_profile(v4_gold), from_profile(v4_gold)])
    (run,) = extract.run_extract(contracts, out, cache_dir=cache, complete=llm)
    assert run.status == "extracted"
    assert run.validation is not None
    assert run.validation.ok
    assert run.uncovered == []
    assert run.review is not None
    review = json.loads((out / "V4.review.json").read_text(encoding="utf-8"))
    assert review["llm_review"]["issues"][0]["description"] == "Hinweis"
    assert review["completeness_uncovered"] == []

    (again,) = extract.run_extract(contracts, out, cache_dir=cache, complete=llm)
    assert again.status == "skipped"
    (forced,) = extract.run_extract(contracts, out, cache_dir=cache, complete=llm, force=True)
    assert forced.status == "extracted"
    assert len(llm.calls) == 4  # 2 × (extraction + review)

    profiles, findings = load_profiles(out, contracts)  # the review file is not a profile
    assert [p.contract_id for p in profiles] == ["V4"]
    assert [f.code for f in findings] == ["PROFILE_NOT_REVIEWED"]


def test_llm_unavailable_marks_contract_failed(tmp_path: Path) -> None:
    contracts, out = tmp_path / "pdf", tmp_path / "out"
    contracts.mkdir()
    shutil.copy(V4_PDF, contracts / V4_PDF.name)
    llm = FakeLLM([client.LLMUnavailable("down")])
    (run,) = extract.run_extract(contracts, out, cache_dir=tmp_path / "c", complete=llm)
    assert run.status == "failed"
    assert "down" in run.reason
    assert not (out / "V4.json").exists()


def test_contract_ids() -> None:
    assert extract.contract_id_for(V4_PDF) == "V4"
    assert extract.contract_id_for(Path("Rahmenvertrag.pdf")) == "Rahmenvertrag"


# ── CLI ──────────────────────────────────────────────────────────────────────


@pytest.fixture
def at_root(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(ROOT)


@pytest.mark.usefixtures("at_root")
def test_cli_extract_with_mocked_llm(
    v4_gold: ContractProfile, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    contracts, out = tmp_path / "pdf", tmp_path / "out"
    contracts.mkdir()
    shutil.copy(V4_PDF, contracts / V4_PDF.name)
    monkeypatch.setattr(client, "complete_structured", FakeLLM([from_profile(v4_gold)]))
    args = [
        "contracts",
        "extract",
        "--only",
        "V4",
        "--contracts",
        str(contracts),
        "--out",
        str(out),
    ]
    result = CliRunner().invoke(app, args)
    assert result.exit_code == 0, result.output
    assert "V4: extrahiert (draft) – V4-EKQ, V4-DVG" in result.output
    assert "Zitate: 31/31" in result.output
    assert json.loads((out / "V4.json").read_text(encoding="utf-8"))["review"]["status"] == "draft"


@pytest.mark.usefixtures("at_root")
def test_cli_approve_sets_reviewed(v4_gold: ContractProfile, tmp_path: Path) -> None:
    draft = v4_gold.model_copy(
        update={
            "review": v4_gold.review.model_copy(
                update={"status": "draft", "reviewed_by": None, "reviewed_at": None}
            )
        }
    )
    extract.write_profile(tmp_path / "V4.json", draft)
    args = ["contracts", "approve", "V4", "--by", "Erika Prüferin", "--profiles", str(tmp_path)]
    result = CliRunner().invoke(app, args)
    assert result.exit_code == 0, result.output
    profiles, findings = load_profiles(tmp_path)
    assert profiles[0].review.status == "reviewed"
    assert profiles[0].review.reviewed_by == "Erika Prüferin"
    assert profiles[0].review.reviewed_at is not None
    assert findings == []


@pytest.mark.usefixtures("at_root")
def test_cli_approve_refuses_broken_quotes(v4_gold: ContractProfile, tmp_path: Path) -> None:
    sources = [
        s.model_copy(update={"quote": s.quote + " (erfunden)"}) if s.id == "S_CURE" else s
        for s in v4_gold.sources
    ]
    extract.write_profile(tmp_path / "V4.json", v4_gold.model_copy(update={"sources": sources}))
    result = CliRunner().invoke(
        app, ["contracts", "approve", "V4", "--by", "X", "--profiles", str(tmp_path)]
    )
    assert result.exit_code == 1
    assert "S_CURE" in result.output


@pytest.mark.usefixtures("at_root")
def test_cli_eval_on_gold() -> None:
    result = CliRunner().invoke(app, ["contracts", "eval", "--profiles", str(GOLD)])
    assert result.exit_code == 0, result.output
    assert "Gesamt:" in result.output
    assert "(100%)" in result.output
    assert "[!!]" not in result.output
