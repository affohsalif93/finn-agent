"""Gold profiles: every quote is verbatim on its stated page; examples reproduce."""

import re
from pathlib import Path

import pdfplumber
import pytest

from finn.calc.recipe import recipe_issues
from finn.contracts.selftest import check_examples, example_value
from finn.models.contract import ContractProfile

MATERIAL = Path(__file__).resolve().parents[3] / "material"


def _norm(text: str) -> str:
    """Whitespace only: line breaks, double spaces (nothing else is normalised)."""
    return re.sub(r"\s+", " ", text).strip()


@pytest.fixture(scope="session")
def page_texts() -> dict[str, list[str]]:
    texts: dict[str, list[str]] = {}
    for pdf_path in sorted(MATERIAL.glob("*.pdf")):
        with pdfplumber.open(pdf_path) as pdf:
            texts[pdf_path.name] = [_norm(page.extract_text() or "") for page in pdf.pages]
    return texts


def test_four_gold_profiles(gold_profiles: list[ContractProfile]) -> None:
    assert [p.contract_id for p in gold_profiles] == ["V1", "V2", "V3", "V4"]
    assert all(p.review.status == "reviewed" for p in gold_profiles)
    assert [len(p.covenants) for p in gold_profiles] == [0, 1, 1, 2]


def test_quotes_are_verbatim_on_the_stated_page(
    gold_profiles: list[ContractProfile], page_texts: dict[str, list[str]]
) -> None:
    missing = []
    for profile in gold_profiles:
        pages = page_texts[profile.document.file]
        assert profile.document.pages == len(pages)
        for source in profile.sources:
            if _norm(source.quote) not in pages[source.page - 1]:
                missing.append(f"{profile.contract_id} {source.id} (S. {source.page})")
    assert missing == []


def test_recipes_are_valid(gold_profiles: list[ContractProfile]) -> None:
    assert {p.contract_id: recipe_issues(p) for p in gold_profiles} == {
        "V1": [], "V2": [], "V3": [], "V4": []
    }  # fmt: skip


def test_v3_contract_example_gives_154(gold_profiles: list[ContractProfile]) -> None:
    v3 = next(p for p in gold_profiles if p.contract_id == "V3")
    (example,) = v3.examples
    assert str(example_value(v3, example)) == "1.54"
    assert check_examples(v3) == []


def test_profile_facts(gold_profiles: list[ContractProfile]) -> None:
    v1, v2, v3, v4 = gold_profiles
    assert v1.has_financial_covenants is False
    assert v1.no_covenant_reason is not None
    assert {m.category for m in v1.dismissed_mentions} >= {
        "historical_figure", "general_undertaking", "collateral_right"
    }  # fmt: skip
    assert v2.covenants[0].rounding.decimals is None
    assert v2.covenants[0].plan_check.plan_status_required == "approved"
    assert v3.loans[0].match.loan_number == "LBM-INV-2025-0412"
    assert [t.tranche_id for t in v3.loans[0].tranches] == ["A", "B"]
    assert v3.covenants[0].cure is not None
    assert v3.covenants[0].cure.max_count == 2
    assert "Sparkasse Musterstadt" in v4.parties.lender_members
    assert v4.pricing_grid is not None
    assert [str(s.margin_pct) for s in v4.pricing_grid.steps] == ["0.95", "1.10", "1.25"]
    for cov in v4.covenants:
        assert cov.cure is not None
        assert cov.cure.allowed is False
    unmapped = v4.definitions["financial_debt"].unmapped_components
    assert [u.effect for u in unmapped] == ["assumption"]
