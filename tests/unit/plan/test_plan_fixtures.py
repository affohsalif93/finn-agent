"""One test per generated plan variant (scripts/make_plan_fixtures.py)."""

import shutil
from decimal import Decimal
from pathlib import Path

import pytest

from finn.errors import ErrorCode, Finding, Severity, has_blocking
from finn.models.plan import FactKey, ItemKey, Plan
from finn.plan import PlanReadResult, read_plan


def _codes(findings: list[Finding]) -> set[tuple[ErrorCode, Severity]]:
    return {(f.code, f.severity) for f in findings if f.code is not ErrorCode.PLAN_STATUS}


def _values(plan: Plan | None) -> dict[ItemKey, Decimal]:
    assert plan is not None
    return {k: v.value for k, v in plan.values.items()}


def _read(fixtures: dict[str, Path], name: str) -> PlanReadResult:
    return read_plan(fixtures[name])


# ── Same building blocks as the sample plan ──────────────────────────────────


@pytest.mark.parametrize(
    "name", ["plan_renamed_labels.xlsx", "plan_moved_rows.xlsx", "plan_in_eur.xlsx"]
)
def test_variant_reads_like_sample(
    plan_fixtures: dict[str, Path], sample_result: PlanReadResult, name: str
) -> None:
    result = _read(plan_fixtures, name)
    assert _values(result.plan) == _values(sample_result.plan)
    assert not has_blocking(result.findings)
    assert result.plan is not None
    assert sample_result.plan is not None
    facts = {k: (f.value, f.ambiguous) for k, f in result.plan.facts.items()}
    assert facts == {k: (f.value, f.ambiguous) for k, f in sample_result.plan.facts.items()}
    numbers = [ln.loan_number for ln in result.plan.loans]
    assert numbers == [ln.loan_number for ln in sample_result.plan.loans]


def test_renamed_labels_mapped_by_synonyms(plan_fixtures: dict[str, Path]) -> None:
    result = _read(plan_fixtures, "plan_renamed_labels.xlsx")
    assert result.plan is not None
    assert result.plan.values[ItemKey.EQUITY].label == "Eigenmittel"
    assert result.plan.values[ItemKey.BANK_DEBT].label == "Bankdarlehen"
    assert str(result.plan.values[ItemKey.BANK_DEBT].ref) == "Planbilanz!D28"
    assert _codes(result.findings) == set()


def test_moved_rows_new_cells(plan_fixtures: dict[str, Path]) -> None:
    result = _read(plan_fixtures, "plan_moved_rows.xlsx")
    assert result.plan is not None
    values = result.plan.values
    assert str(values[ItemKey.NET_INCOME].ref) == "GuV!B20"  # 2 rows down, column D -> B
    assert str(values[ItemKey.EQUITY].ref) == "Bilanz!D24"
    # saved by openpyxl: no cached values, the totals were recomputed from the inputs
    assert [str(r) for r in values[ItemKey.EQUITY].derived_from] == [
        f"Bilanz!D{r}" for r in range(20, 24)
    ]
    assert _codes(result.findings) == set()


def test_eur_sheet_converted(plan_fixtures: dict[str, Path]) -> None:
    result = _read(plan_fixtures, "plan_in_eur.xlsx")
    assert _codes(result.findings) == {(ErrorCode.PLAN_UNITS_CONVERTED, Severity.INFO)}
    assert result.findings[0].message_de == "Finanzierung in € angegeben, in T€ umgerechnet."
    assert result.plan is not None
    assert result.plan.values[ItemKey.TOTAL_BANK_DEBT].original_unit == "€"
    assert result.plan.loans[0].closing == Decimal(18750)


# ── Error codes ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("plan_2026.xlsx", set()),  # consistent in itself; the request check catches it
        ("plan_unbalanced.xlsx", {(ErrorCode.PLAN_UNBALANCED, Severity.BLOCK)}),
        ("plan_no_units.xlsx", {(ErrorCode.PLAN_UNITS_UNCLEAR, Severity.BLOCK)}),
        ("plan_no_financing.xlsx", {(ErrorCode.PLAN_ITEM_MISSING, Severity.PARTIAL)}),
        ("plan_no_subordination_note.xlsx", {(ErrorCode.PLAN_FACT_AMBIGUOUS, Severity.WARN)}),
        ("plan_ref_error.xlsx", {(ErrorCode.XLSX_ERROR_CELL, Severity.PARTIAL)}),
        ("plan_no_cache.xlsx", {(ErrorCode.XLSX_NO_CACHED_VALUES, Severity.PARTIAL)}),
        ("plan_signs.xlsx", {(ErrorCode.PLAN_SIGNS, Severity.WARN)}),
        ("plan_loans_mismatch.xlsx", {(ErrorCode.PLAN_LOANS_MISMATCH, Severity.BLOCK)}),
        ("plan_interest_off.xlsx", {(ErrorCode.PLAN_INTEREST_TIEOUT, Severity.WARN)}),
        ("plan_opening_off.xlsx", {(ErrorCode.PLAN_OPENING_BALANCE, Severity.WARN)}),
        ("plan_actuals_only.xlsx", {(ErrorCode.PLAN_NO_PLAN_COLUMN, Severity.BLOCK)}),
        ("plan_hidden.xlsx", {(ErrorCode.XLSX_HIDDEN, Severity.INFO)}),
        ("plan_bullet.xlsx", {(ErrorCode.PLAN_BULLET_IN_PLAN_YEAR, Severity.WARN)}),
        ("plan_unwinding_from_note.xlsx", set()),
        (
            "plan_no_unwinding.xlsx",
            {
                (ErrorCode.PLAN_SUBITEM_NOT_SHOWN, Severity.WARN),
                (ErrorCode.PLAN_INTEREST_TIEOUT, Severity.WARN),  # 4.930 + 0 vs. 5.100
            },
        ),
        ("plan_corrupt.xlsx", {(ErrorCode.XLSX_CORRUPT, Severity.ASK)}),
        ("plan_password.xlsx", {(ErrorCode.XLSX_ENCRYPTED, Severity.ASK)}),
        ("plan.xls", {(ErrorCode.ATTACHMENT_XLS_LEGACY, Severity.ASK)}),
    ],
)
def test_variant_findings(
    plan_fixtures: dict[str, Path], name: str, expected: set[tuple[ErrorCode, Severity]]
) -> None:
    assert _codes(_read(plan_fixtures, name).findings) == expected


@pytest.mark.parametrize("name", ["plan_corrupt.xlsx", "plan_password.xlsx", "plan.xls"])
def test_unreadable_files_give_no_plan(plan_fixtures: dict[str, Path], name: str) -> None:
    assert _read(plan_fixtures, name).plan is None


def test_plan_2026_year(plan_fixtures: dict[str, Path]) -> None:
    plan = _read(plan_fixtures, "plan_2026.xlsx").plan
    assert plan is not None
    assert plan.plan_year == 2026
    assert [ln.loan_number for ln in plan.loans][:3] == [
        "4471 203 118", "FD-2022-08817", "LBM-INV-2025-0412"
    ]  # fmt: skip


def test_unbalanced_message(plan_fixtures: dict[str, Path]) -> None:
    (block,) = [f for f in _read(plan_fixtures, "plan_unbalanced.xlsx").findings
                if f.code is ErrorCode.PLAN_UNBALANCED]  # fmt: skip
    assert block.message_de == "Bilanz nicht ausgeglichen: Aktiva 313.200, Passiva 312.900 T€."
    assert block.affects == "all"


def test_no_units_one_block_per_sheet(plan_fixtures: dict[str, Path]) -> None:
    findings = _read(plan_fixtures, "plan_no_units.xlsx").findings
    sheets = [f.message_de.split()[3] for f in findings if f.code is ErrorCode.PLAN_UNITS_UNCLEAR]
    assert sheets == ["GuV", "Bilanz", "Finanzierung"]


def test_no_financing_is_partial(plan_fixtures: dict[str, Path]) -> None:
    result = _read(plan_fixtures, "plan_no_financing.xlsx")
    (missing,) = [f for f in result.findings if f.code is ErrorCode.PLAN_ITEM_MISSING]
    assert set(missing.items) == {
        "scheduled_repayments_total", "special_repayments_total", "total_bank_debt",
        "interest_on_financial_debt",
    }  # fmt: skip
    assert missing.affects == []  # the calc engine maps items -> covenants
    assert result.plan is not None
    assert result.plan.loans == []
    assert result.plan.values[ItemKey.EQUITY].value == 96100  # the rest is still there


def test_no_subordination_note_conservative(plan_fixtures: dict[str, Path]) -> None:
    plan = _read(plan_fixtures, "plan_no_subordination_note.xlsx").plan
    assert plan is not None
    fact = plan.facts[FactKey.QUALIFIED_SUBORDINATION]
    assert (fact.value, fact.ambiguous, fact.evidence) == (False, True, None)


def test_ref_error_items(plan_fixtures: dict[str, Path]) -> None:
    result = _read(plan_fixtures, "plan_ref_error.xlsx")
    errors = {f.items[0]: f.message_de for f in result.findings
              if f.code is ErrorCode.XLSX_ERROR_CELL}  # fmt: skip
    assert errors == {
        "income_taxes": "Zelle GuV!D17 enthält #REF!.",
        "net_income": "Zelle GuV!D18 enthält #REF!.",  # formula depends on D17
    }
    assert not any(f.code is ErrorCode.PLAN_ITEM_MISSING for f in result.findings)


def test_signs_normalised_values_unchanged(
    plan_fixtures: dict[str, Path], sample_result: PlanReadResult
) -> None:
    assert _values(_read(plan_fixtures, "plan_signs.xlsx").plan) == _values(sample_result.plan)


def test_filename_year_differs_from_content(tmp_path: Path) -> None:
    copy = tmp_path / "Wirtschaftsplan_2026.xlsx"
    shutil.copy(Path(__file__).resolve().parents[3] / "material/Wirtschaftsplan_2027.xlsx", copy)
    findings = read_plan(copy).findings
    assert _codes(findings) == {(ErrorCode.PLAN_YEAR_FILENAME, Severity.WARN)}
    assert findings[-1].message_de == "Dateiname nennt 2026, Inhalt ist Plan 2027."


def test_xlsm_macros_info(tmp_path: Path) -> None:
    copy = tmp_path / "plan.xlsm"
    shutil.copy(Path(__file__).resolve().parents[3] / "material/Wirtschaftsplan_2027.xlsx", copy)
    assert _codes(read_plan(copy).findings) == {(ErrorCode.ATTACHMENT_MACROS, Severity.INFO)}


def test_unwinding_derived_from_note_3(plan_fixtures: dict[str, Path]) -> None:
    plan = _read(plan_fixtures, "plan_unwinding_from_note.xlsx").plan
    assert plan is not None
    unwinding = plan.values[ItemKey.PENSION_UNWINDING]
    assert unwinding.value == Decimal(170)  # 5.100 GuV - 4.930 loan schedule
    assert unwinding.ref is None
    assert str(unwinding.derived_from[0]) == "GuV!D13"
    assert len(unwinding.derived_from) == 7  # GuV interest + six loan interest cells


def test_no_unwinding_not_invented(plan_fixtures: dict[str, Path]) -> None:
    plan = _read(plan_fixtures, "plan_no_unwinding.xlsx").plan
    assert plan is not None
    assert ItemKey.PENSION_UNWINDING not in plan.values
