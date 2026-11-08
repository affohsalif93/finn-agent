"""Plan facts by pattern rules on note texts."""

from decimal import Decimal

import pytest

from finn.errors import ErrorCode
from finn.models.plan import CellRef, LoanFactKey, LoanLine
from finn.plan.facts import TextCell, cash_fact, loan_facts, subordination_fact


def _texts(*texts: str) -> list[TextCell]:
    return [TextCell(CellRef(sheet="Bilanz", cell=f"A{i}"), t) for i, t in enumerate(texts, 1)]


@pytest.mark.parametrize(
    ("text", "value", "ambiguous"),
    [
        ("mit qualifiziertem Rangrücktritt im Sinne von § 39 Abs. 2 InsO", True, False),
        ("Gesellschafterdarlehen (nachrangig, qualifizierter Rangrücktritt)", True, False),
        ("Gesellschafterdarlehen ohne Rangrücktritt", False, False),
        ("Gesellschafterdarlehen mit Rangrücktritt", False, True),  # not stated as qualified
        ("Gesellschafterdarlehen, nachrangig", False, True),
        ("Darlehen der Stadt", False, True),  # nothing said, shareholder loan exists
    ],
)
def test_subordination_from_note(text: str, value: bool, ambiguous: bool) -> None:
    fact, findings = subordination_fact(_texts(text), has_shareholder_loan=True)
    assert (fact.value, fact.ambiguous) == (value, ambiguous)
    assert [f.code for f in findings] == ([ErrorCode.PLAN_FACT_AMBIGUOUS] if ambiguous else [])


def test_subordination_conflict_is_ambiguous() -> None:
    texts = _texts("qualifizierter Rangrücktritt (§ 39 Abs. 2 InsO)", "kein Rangrücktritt")
    fact, findings = subordination_fact(texts, has_shareholder_loan=True)
    assert (fact.value, fact.ambiguous, len(findings)) == (False, True, 1)


def test_no_shareholder_loan_no_warning() -> None:
    fact, findings = subordination_fact(_texts("Sonstiges"), has_shareholder_loan=False)
    assert (fact.value, fact.ambiguous, findings) == (False, False, [])


@pytest.mark.parametrize(
    ("text", "value", "ambiguous"),
    [
        ("Liquide Mittel: frei verfügbar, keine Verpfändung, keine Cash-Pooling-…", True, False),
        ("Flüssige Mittel frei verfügbar", True, False),
        ("Liquide Mittel: frei verfügbar; 2.000 T€ an die Bank verpfändet", False, True),
        ("Guthaben auf Sperrkonto (Kaution)", False, False),
        ("Liquide Mittel sind nicht frei verfügbar", False, False),
        ("Kreditlinie frei verfügbar", False, True),  # not about cash: no evidence
    ],
)
def test_cash_availability_from_note(text: str, value: bool, ambiguous: bool) -> None:
    fact, _ = cash_fact(_texts(text), has_cash=True)
    assert (fact.value, fact.ambiguous) == (value, ambiguous)


def _loan(label: str, closing: int, repaid: int) -> LoanLine:
    return LoanLine(
        label=label, loan_number=None, kind="bank", opening=Decimal(closing + repaid),
        drawdown=Decimal(0), scheduled_repayment=Decimal(repaid), special_repayment=Decimal(0),
        closing=Decimal(closing), interest=Decimal(1),
        refs={"closing": CellRef(sheet="Finanzierung", cell="F5")},
    )  # fmt: skip


def test_bullet_from_schedule() -> None:
    facts = loan_facts(_loan("Investitionsdarlehen Sparkasse", 0, 20000), [])
    assert facts[LoanFactKey.BULLET_IN_PLAN_YEAR].value is True
    assert LoanFactKey.REFINANCED not in facts  # unknown: no note


def test_refinancing_note() -> None:
    text = "5) Investitionsdarlehen Sparkasse: Anschlussfinanzierung, refinanziert."
    notes = [TextCell(CellRef(sheet="Finanzierung", cell="A30"), text)]
    facts = loan_facts(_loan("Investitionsdarlehen Sparkasse (Nr. 1)", 0, 20000), notes)
    assert facts[LoanFactKey.REFINANCED].value is True
