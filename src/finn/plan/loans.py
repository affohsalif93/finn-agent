"""Loan schedule (Finanzierung / Darlehensspiegel) -> LoanLines and the four aggregates."""

from dataclasses import dataclass, field
from decimal import Decimal

from openpyxl.utils.cell import get_column_letter
from openpyxl.worksheet.worksheet import Worksheet

from finn.errors import ErrorCode, Finding, finding
from finn.models.plan import CellRef, ItemKey, LoanKind, LoanLine, PlanValue
from finn.plan.formulas import CellValue, Evaluator
from finn.plan.labels import (
    FINANCING_ITEMS,
    LOAN_COLUMNS,
    LoanField,
    is_notes_heading,
    is_shareholder_label,
    is_total_row,
    loan_number,
    loan_section,
)

_REQUIRED_FIELDS: tuple[LoanField, ...] = ("opening", "scheduled_repayment", "closing", "interest")
_ALL_FIELDS: tuple[LoanField, ...] = (
    "opening", "drawdown", "scheduled_repayment", "special_repayment", "closing", "interest"
)  # fmt: skip
# Which aggregate depends on which loan field (a broken cell makes only that aggregate missing).
_AGGREGATE_FIELD: dict[ItemKey, LoanField] = {
    ItemKey.SCHEDULED_REPAYMENTS_TOTAL: "scheduled_repayment",
    ItemKey.SPECIAL_REPAYMENTS_TOTAL: "special_repayment",
    ItemKey.TOTAL_BANK_DEBT: "closing",
    ItemKey.INTEREST_ON_FINANCIAL_DEBT: "interest",
}


@dataclass
class LoanSchedule:
    loans: list[LoanLine]
    values: dict[ItemKey, PlanValue]
    findings: list[Finding] = field(default_factory=list)
    log: list[str] = field(default_factory=list)
    header_row: int = 0
    label_cells: set[CellRef] = field(default_factory=set)
    used: list[CellRef] = field(default_factory=list)


@dataclass
class _RawLoan:
    label: str
    label_ref: CellRef
    kind: LoanKind
    cells: dict[LoanField, CellValue]


def find_header(ws: Worksheet) -> tuple[int, dict[LoanField, int]] | None:
    """Header row with at least opening, closing and one more loan column."""
    for row in ws.iter_rows(min_row=1, max_row=min(ws.max_row, 30)):
        columns: dict[LoanField, int] = {}
        for cell in row:
            if not isinstance(cell.value, str):
                continue
            for spec in LOAN_COLUMNS:
                if spec.field not in columns and spec.matches(cell.value):
                    columns[spec.field] = cell.column
                    break
        if {"opening", "closing"} <= columns.keys() and len(columns) >= 3:
            return int(row[0].row or 0), columns
    return None


def _label(ws: Worksheet, row: int, before_col: int) -> tuple[str, CellRef] | None:
    for col in range(1, before_col):
        value = ws.cell(row=row, column=col).value
        if isinstance(value, str) and value.strip() and not value.startswith("="):
            return value.strip(), CellRef(sheet=ws.title, cell=f"{get_column_letter(col)}{row}")
    return None


def _raw_loans(
    ws: Worksheet, ev: Evaluator, header_row: int, columns: dict[LoanField, int]
) -> list[_RawLoan]:
    first_col = min(columns.values())
    section: str | None = None
    loans: list[_RawLoan] = []
    for row in range(header_row + 1, ws.max_row + 1):
        found = _label(ws, row, first_col)
        if found is None:
            continue
        label, label_ref = found
        if is_notes_heading(label):
            break
        cells = {f: ev.read(ws.title, f"{get_column_letter(c)}{row}") for f, c in columns.items()}
        has_values = any(not c.is_empty for c in cells.values())
        if not has_values:
            section = loan_section(label) or section
            continue
        if is_total_row(label) or (cells["opening"].is_empty and cells["closing"].is_empty):
            continue
        kind: LoanKind = (
            "shareholder"
            if is_shareholder_label(label)
            else "other"
            if section == "other"
            else "bank"
        )
        loans.append(_RawLoan(label=label, label_ref=label_ref, kind=kind, cells=cells))
    return loans


def read_loan_schedule(ws: Worksheet, ev: Evaluator, factor: Decimal, unit: str) -> LoanSchedule:
    """LoanLines (values in T€, repayments / interest as positive magnitudes) + aggregates."""
    header = find_header(ws)
    if header is None:
        return LoanSchedule(loans=[], values={}, log=[f"{ws.title}: no loan schedule header"])
    header_row, columns = header
    out = LoanSchedule(loans=[], values={}, header_row=header_row)
    missing_cols = [f for f in _REQUIRED_FIELDS if f not in columns]
    if missing_cols:
        out.log.append(f"{ws.title}: loan columns not found: {', '.join(missing_cols)}")
        return out
    raws = _raw_loans(ws, ev, header_row, columns)
    broken = _check_cells(raws, out)
    numbers = _numbers(raws, factor)
    signs_flipped = _normalise_signs(numbers, raws)
    if signs_flipped:
        out.findings.append(
            finding(
                ErrorCode.PLAN_SIGNS,
                cells=", ".join(str(r) for r in signs_flipped),
                refs=signs_flipped,
            )
        )
    for raw, values in zip(raws, numbers, strict=True):
        out.loans.append(_loan_line(raw, values))
        out.label_cells.add(raw.label_ref)
        out.used.extend(c.ref for c in raw.cells.values() if not c.is_empty)
    out.values = _aggregates(out.loans, unit, broken)
    out.log.extend(
        f"{ws.title}!{loan.refs['label'].cell}: loan '{loan.label}' ({loan.kind}, "
        f"Nr. {loan.loan_number or '-'})"
        for loan in out.loans
    )
    return out


def _check_cells(raws: list[_RawLoan], out: LoanSchedule) -> set[LoanField]:
    """Error / uncomputable cells -> findings; returns the loan fields that are unusable."""
    broken: set[LoanField] = set()
    for raw in raws:
        for name, cell in raw.cells.items():
            items = [k for k, f in _AGGREGATE_FIELD.items() if f == name]
            if cell.error:
                broken.add(name)
                out.findings.append(
                    finding(
                        ErrorCode.XLSX_ERROR_CELL, cell=str(cell.ref), error=cell.error,
                        refs=[cell.ref], items=items,
                    )
                )  # fmt: skip
            elif cell.unsupported:
                broken.add(name)
                out.findings.append(
                    finding(
                        ErrorCode.XLSX_NO_CACHED_VALUES, cell=str(cell.ref),
                        refs=[cell.ref], items=items,
                    )
                )  # fmt: skip
    return broken


def _numbers(raws: list[_RawLoan], factor: Decimal) -> list[dict[LoanField, Decimal]]:
    return [
        {f: (raw.cells[f].value or Decimal(0)) * factor if f in raw.cells else Decimal(0)
         for f in _ALL_FIELDS}
        for raw in raws
    ]  # fmt: skip


def _normalise_signs(
    numbers: list[dict[LoanField, Decimal]], raws: list[_RawLoan]
) -> list[CellRef]:
    """Per column: all negative = a convention (flip silently); mixed = flip and report."""
    flipped: list[CellRef] = []
    for name in _ALL_FIELDS:
        signs = [v[name] < 0 for v in numbers if v[name] != 0]
        if not signs:
            continue
        all_negative = all(signs)
        for values, raw in zip(numbers, raws, strict=True):
            if values[name] < 0:
                values[name] = -values[name]
                if not all_negative and name in raw.cells:
                    flipped.append(raw.cells[name].ref)
    return flipped


def _loan_line(raw: _RawLoan, values: dict[LoanField, Decimal]) -> LoanLine:
    refs: dict[str, CellRef] = {n: c.ref for n, c in raw.cells.items() if not c.is_empty}
    refs["label"] = raw.label_ref
    return LoanLine(
        label=raw.label,
        loan_number=loan_number(raw.label),
        kind=raw.kind,
        opening=values["opening"],
        drawdown=values["drawdown"],
        scheduled_repayment=values["scheduled_repayment"],
        special_repayment=values["special_repayment"],
        closing=values["closing"],
        interest=values["interest"],
        refs=refs,
    )


def _aggregates(
    loans: list[LoanLine], unit: str, broken: set[LoanField]
) -> dict[ItemKey, PlanValue]:
    if not loans:
        return {}
    values: dict[ItemKey, PlanValue] = {}
    for item, name in _AGGREGATE_FIELD.items():
        if name in broken:
            continue
        chosen = [ln for ln in loans if item is not ItemKey.TOTAL_BANK_DEBT or ln.kind == "bank"]
        total = sum((getattr(ln, name) for ln in chosen), Decimal(0))
        values[item] = PlanValue(
            item=item,
            value=total,
            ref=None,
            label=FINANCING_ITEMS[item],
            original_unit=unit,
            derived_from=[ln.refs[name] for ln in chosen if name in ln.refs],
        )
    return values
