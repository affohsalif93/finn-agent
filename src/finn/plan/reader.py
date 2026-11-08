"""Step 1 of the plan reader (D6): deterministic mapping by row label x plan-year column.

`read_plan(path)` is pure: file in, models out, no network. Values are normalised to T€ and to
positive magnitudes for expenses / liabilities; every value keeps its cell reference.
"""

import re
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from pathlib import Path

from openpyxl.utils.cell import get_column_letter
from openpyxl.worksheet.worksheet import Worksheet

from finn.errors import ErrorCode, Finding, finding
from finn.models.plan import CellRef, FactKey, ItemKey, Plan, PlanFact, PlanValue
from finn.models.run import PlanReadResult
from finn.plan import checks
from finn.plan.facts import TextCell, cash_fact, loan_facts, subordination_fact
from finn.plan.formulas import CellErrorValue, CellValue, Evaluator, UnsupportedFormula
from finn.plan.labels import (
    BLOCKS,
    BlockSpec,
    Section,
    Statement,
    YearHeader,
    is_notes_heading,
    normalise,
    parse_year_header,
    statement_for,
)
from finn.plan.loans import LoanSchedule, find_header, read_loan_schedule
from finn.plan.workbook import (
    UNIT_FACTORS,
    UNIT_TEUR,
    Workbook,
    detect_unit,
    open_workbook,
    units_in_text,
)


@dataclass
class _Ctx:
    wb: Workbook
    ev: Evaluator
    findings: list[Finding] = field(default_factory=list)
    log: list[str] = field(default_factory=list)
    used: list[CellRef] = field(default_factory=list)
    reported: set[str] = field(default_factory=set)  # items that already have a finding
    cover_unit: str | None = None  # "Alle Beträge in T€" on the cover = default per sheet

    def add(self, f: Finding) -> None:
        self.findings.append(f)
        self.reported.update(f.items)


@dataclass(frozen=True)
class _Row:
    row: int
    label: str
    label_ref: CellRef
    cell: CellValue
    section: Section | None


@dataclass(frozen=True)
class _Column:
    sheet: str
    header_row: int
    col: int
    header: YearHeader
    text: str


@dataclass(frozen=True)
class _Cover:
    company: str | None = None
    year: int | None = None
    status: str = "draft"
    version_date: date | None = None
    unit: str | None = None


def read_plan(path: Path) -> PlanReadResult:
    """Read a business plan workbook into a `Plan` plus findings (FAILURES.md D/E/F)."""
    wb, open_findings = open_workbook(path)
    if wb is None:
        return PlanReadResult(plan=None, findings=open_findings, mapping_log=[])
    ctx = _Ctx(wb=wb, ev=Evaluator(wb), findings=list(open_findings))
    plan = _read(ctx, path)
    return PlanReadResult(plan=plan, findings=ctx.findings, mapping_log=ctx.log)


def _read(ctx: _Ctx, path: Path) -> Plan | None:
    sheets = _classify_sheets(ctx)
    cover = _read_cover(ctx, sheets.get(Statement.COVER))
    ctx.cover_unit = cover.unit
    columns = _plan_columns(ctx, sheets, cover.year)
    plan_year = cover.year or max((c.header.year for c in columns.values()), default=None)
    if plan_year is None:
        if not any(f.code is ErrorCode.PLAN_NO_PLAN_COLUMN for f in ctx.findings):
            ctx.add(finding(ErrorCode.PLAN_NO_PLAN_COLUMN, sheet="(alle)"))
        return None
    values: dict[ItemKey, PlanValue] = {}
    previous: dict[ItemKey, Decimal] = {}
    recomputed: dict[ItemKey, Decimal] = {}
    for statement in (Statement.GUV, Statement.BILANZ):
        if statement in columns:
            _read_statement(ctx, columns[statement], statement, values, previous, recomputed)
    schedule = _read_financing(ctx, sheets.get(Statement.FINANZIERUNG))
    values.update(schedule.values)
    _derive_unwinding(ctx, values, schedule, sheets.get(Statement.FINANZIERUNG))
    texts = _text_cells(ctx.wb)
    facts = _plan_facts(ctx, texts, values, schedule)
    notes = [t for t in texts if t.ref.sheet == sheets.get(Statement.FINANZIERUNG)]
    notes = [t for t in notes if t.ref not in schedule.label_cells]
    loans = [ln.model_copy(update={"facts": loan_facts(ln, notes)}) for ln in schedule.loans]
    plan = Plan(
        company=cover.company,
        plan_year=plan_year,
        reporting_date=_reporting_date(columns.get(Statement.BILANZ), plan_year),
        status="approved" if cover.status == "approved" else "draft",
        version_date=cover.version_date,
        file_sha256=ctx.wb.sha256,
        values=values,
        loans=loans,
        facts=facts,
    )
    _run_checks(ctx, plan, path, previous, recomputed, sheets)
    return plan


# ── Sheets and cover ─────────────────────────────────────────────────────────


def _title(ws: Worksheet) -> str:
    for row in ws.iter_rows(min_row=1, max_row=3):
        for cell in row:
            if isinstance(cell.value, str) and cell.value.strip():
                return cell.value
    return ""


def _classify_sheets(ctx: _Ctx) -> dict[Statement, str]:
    found: dict[Statement, str] = {}
    for name in ctx.wb.sheet_names:
        ws = ctx.wb.sheet(name)
        statement = statement_for(name) or statement_for(_title(ws), allow_cover=False)
        if statement and statement not in found:
            found[statement] = name
            ctx.log.append(f"sheet '{name}' -> {statement}")
    if Statement.COVER not in found and ctx.wb.sheet_names:
        first = ctx.wb.sheet_names[0]
        if first not in found.values():
            found[Statement.COVER] = first
            ctx.log.append(f"sheet '{first}' -> cover (first sheet)")
    return found


_LEGAL_FORM = re.compile(
    r"\b(GmbH|mbH|AG|KG|SE|eG|AöR|e\.\s?V\.|Eigenbetrieb|Zweckverband|Stadtwerke)\b"
)
_COVER_YEAR = re.compile(r"\b(?:wirtschaftsplan|plan|budget|erfolgsplan)\s*((?:19|20)\d{2})\b")
_DRAFT = re.compile(r"\b(entwurf|nicht beschlossen|nicht genehmigt|vorlaeufig)\b")
_APPROVED = re.compile(r"\b(beschlossen|genehmigt|festgestellt|verabschiedet)\b")
_DATE = re.compile(r"(\d{1,2})\.(\d{1,2})\.((?:19|20)\d{2})")
_VERSION = re.compile(r"\b(stand|version|fassung)\b\s*:?\s*(\d{1,2})\.(\d{1,2})\.(\d{4})", re.I)
_UNIT_HINT = re.compile(r"\b(betraege|angaben|werte|einheit|zahlen)\b")


def _sheet_texts(ws: Worksheet) -> list[str]:
    return [
        c.value
        for row in ws.iter_rows()
        for c in row
        if isinstance(c.value, str) and c.value.strip() and not c.value.startswith("=")
    ]


def _read_cover(ctx: _Ctx, sheet: str | None) -> _Cover:
    if sheet is None:
        ctx.log.append("no cover sheet")
        return _Cover()
    texts = _sheet_texts(ctx.wb.sheet(sheet))
    norm = " | ".join(normalise(t, keep_parens=True) for t in texts)
    company = next((t.strip() for t in texts if _LEGAL_FORM.search(t)), texts[0] if texts else None)
    year_match = _COVER_YEAR.search(norm)
    status = "draft" if _DRAFT.search(norm) else "approved" if _APPROVED.search(norm) else None
    if status is None:
        ctx.log.append("cover: plan status not stated, assumed draft")
    version = next((m for t in texts if (m := _VERSION.search(t))), None)
    version_date = (
        _safe_date(version.group(4), version.group(3), version.group(2)) if version else None
    )
    units = {u for t in texts if _UNIT_HINT.search(normalise(t)) for u in units_in_text(t)}
    cover = _Cover(
        company=company,
        year=int(year_match.group(1)) if year_match else None,
        status=status or "draft",
        version_date=version_date,
        unit=units.pop() if len(units) == 1 else None,
    )
    ctx.log.append(
        f"cover '{sheet}': company={cover.company!r} year={cover.year} status={cover.status} "
        f"version={cover.version_date} unit={cover.unit}"
    )
    return cover


def _safe_date(year: str, month: str, day: str) -> date | None:
    try:
        return date(int(year), int(month), int(day))
    except ValueError:
        return None


def _reporting_date(column: _Column | None, plan_year: int) -> date:
    if column is not None:
        match = _DATE.search(column.text)
        if match and int(match.group(3)) == plan_year:
            parsed = _safe_date(match.group(3), match.group(2), match.group(1))
            if parsed:
                return parsed
    return date(plan_year, 12, 31)


# ── Plan-year columns ────────────────────────────────────────────────────────


def _year_headers(ws: Worksheet) -> list[_Column]:
    found: list[_Column] = []
    for row in ws.iter_rows(min_row=1, max_row=min(ws.max_row, 15)):
        for cell in row:
            if isinstance(cell.value, str) and not cell.value.startswith("="):
                header = parse_year_header(cell.value)
                if header and cell.column > 1:
                    found.append(_Column(ws.title, cell.row, cell.column, header, cell.value))
    return found


def _choose_column(headers: list[_Column], year: int | None) -> _Column | None:
    plan = [h for h in headers if h.header.is_plan and not h.header.is_actual]
    if year is not None:
        same_year = [h for h in plan if h.header.year == year]
        if same_year:
            return same_year[0]
        unmarked = [h for h in headers if h.header.year == year and not h.header.is_actual]
        if unmarked:
            return unmarked[0]
    return max(plan, key=lambda h: h.header.year, default=None)


def _plan_columns(
    ctx: _Ctx, sheets: dict[Statement, str], cover_year: int | None
) -> dict[Statement, _Column]:
    columns: dict[Statement, _Column] = {}
    for statement in (Statement.GUV, Statement.BILANZ):
        sheet = sheets.get(statement)
        if sheet is None:
            continue
        column = _choose_column(_year_headers(ctx.wb.sheet(sheet)), cover_year)
        if column is None:
            ctx.add(
                finding(
                    ErrorCode.PLAN_NO_PLAN_COLUMN, sheet=sheet, items=_statement_items(statement)
                )
            )
            continue
        cell = f"{get_column_letter(column.col)}{column.header_row}"
        ctx.log.append(f"{sheet}: plan column {cell} '{column.text}'")
        if cover_year is not None and column.header.year != cover_year:
            ctx.add(
                finding(
                    ErrorCode.PLAN_YEAR_MISMATCH, variant="cover", expected=cover_year,
                    found=column.header.year, sheet=sheet, refs=[CellRef(sheet=sheet, cell=cell)],
                )
            )  # fmt: skip
        columns[statement] = column
    return columns


def _statement_items(statement: Statement) -> list[str]:
    return [str(b.item) for b in BLOCKS if b.statement is statement and not b.optional]


# ── Units ────────────────────────────────────────────────────────────────────


def _sheet_unit(ctx: _Ctx, sheet: str, header_row: int) -> str:
    """Unit of a sheet, else the cover's; unclear -> BLOCK and assume T€."""
    detected = detect_unit(ctx.wb.sheet(sheet), header_row)
    unit = detected.unit or (None if detected.conflicting else ctx.cover_unit)
    if unit is None:
        ctx.add(finding(ErrorCode.PLAN_UNITS_UNCLEAR, sheet=sheet))
        ctx.log.append(f"{sheet}: unit unclear, assumed T€")
        unit = UNIT_TEUR
    else:
        source = detected.source or "cover"
        ctx.log.append(f"{sheet}: unit {unit} (from {source})")
        if unit != UNIT_TEUR:
            ctx.add(finding(ErrorCode.PLAN_UNITS_CONVERTED, sheet=sheet, unit=unit))
    return unit


# ── GuV / Bilanz ─────────────────────────────────────────────────────────────


def _rows(ctx: _Ctx, column: _Column) -> list[_Row]:
    """Rows below the header with a label and a non-empty, non-text plan cell."""
    ws = ctx.wb.sheet(column.sheet)
    rows: list[_Row] = []
    section: Section | None = None
    letter = get_column_letter(column.col)
    for r in range(column.header_row + 1, ws.max_row + 1):
        found = _row_label(ws, r, column.col)
        if found is None:
            continue
        label, label_ref = found
        if is_notes_heading(label):
            break
        cell = ctx.ev.read(column.sheet, f"{letter}{r}")
        norm = normalise(label)
        if cell.is_empty and norm in ("aktiva", "passiva"):
            section = "aktiva" if norm == "aktiva" else "passiva"
            continue
        if cell.is_empty or (cell.text is not None and not cell.formula):
            continue
        rows.append(_Row(r, label, label_ref, cell, section))
    return rows


def _row_label(ws: Worksheet, row: int, before_col: int) -> tuple[str, CellRef] | None:
    for col in range(1, before_col):
        value = ws.cell(row=row, column=col).value
        if isinstance(value, str) and value.strip() and not value.startswith("="):
            return value.strip(), CellRef(sheet=ws.title, cell=f"{get_column_letter(col)}{row}")
    return None


def _match(ctx: _Ctx, spec: BlockSpec, rows: list[_Row]) -> _Row | None:
    scored = [
        (spec.score(r.label), r)
        for r in rows
        if spec.section is None or r.section is None or r.section == spec.section
    ]
    best = max((s for s, _ in scored), default=0)
    if best == 0:
        return None
    candidates = [r for s, r in scored if s == best]
    differ = len({r.cell.value for r in candidates}) > 1 or any(r.cell.error for r in candidates)
    if len(candidates) > 1 and differ:
        ctx.add(
            finding(
                ErrorCode.PLAN_LABEL_AMBIGUOUS, label=spec.label_de,
                cells=", ".join(str(r.cell.ref) for r in candidates),
                refs=[r.cell.ref for r in candidates], items=[spec.item],
            )
        )  # fmt: skip
        return None
    kind = {3: "exact", 2: "prefix", 1: "contains"}[best]
    ctx.log.append(f"{spec.item} <- {candidates[0].cell.ref} '{candidates[0].label}' ({kind})")
    return candidates[0]


def _cell_number(ctx: _Ctx, cell: CellValue, items: list[str]) -> Decimal | None:
    """Number of a needed cell, or a finding (error value, formula we can't evaluate)."""
    if cell.error:
        ctx.add(
            finding(
                ErrorCode.XLSX_ERROR_CELL, cell=str(cell.ref), error=cell.error,
                refs=[cell.ref], items=items,
            )
        )  # fmt: skip
        return None
    if cell.unsupported or cell.value is None:
        ctx.add(
            finding(
                ErrorCode.XLSX_NO_CACHED_VALUES, cell=str(cell.ref), refs=[cell.ref], items=items
            )
        )
        return None
    if cell.external:
        ctx.add(finding(ErrorCode.XLSX_EXTERNAL_LINK, cell=str(cell.ref), refs=[cell.ref]))
    if cell.recomputed:
        ctx.log.append(f"{cell.ref}: no cached value, recomputed from {len(cell.inputs)} cells")
    ctx.used.append(cell.ref)
    return cell.value


def _read_statement(
    ctx: _Ctx,
    column: _Column,
    statement: Statement,
    values: dict[ItemKey, PlanValue],
    previous: dict[ItemKey, Decimal],
    recomputed: dict[ItemKey, Decimal],
) -> None:
    unit = _sheet_unit(ctx, column.sheet, column.header_row)
    factor = UNIT_FACTORS[unit]
    rows = _rows(ctx, column)
    raw: dict[ItemKey, tuple[Decimal, _Row]] = {}
    for spec in (b for b in BLOCKS if b.statement is statement):
        row = _match(ctx, spec, rows)
        if row is None:
            continue
        number = _cell_number(ctx, row.cell, [spec.item])
        if number is not None:
            raw[spec.item] = (number * factor, row)
    signed = _normalise_signs(ctx, statement, {k: v for k, (v, _) in raw.items()}, raw)
    for item, value in signed.items():
        row = raw[item][1]
        values[item] = PlanValue(
            item=item, value=value, ref=row.cell.ref, label=row.label, original_unit=unit,
            derived_from=list(row.cell.inputs) if row.cell.recomputed else [],
        )  # fmt: skip
    if statement is Statement.BILANZ:
        previous.update(_previous_year(ctx, column, raw, factor))
        recomputed.update(_recomputed_totals(ctx, raw, factor, signed))


# ── Signs ────────────────────────────────────────────────────────────────────


def _normalise_signs(
    ctx: _Ctx,
    statement: Statement,
    raw: dict[ItemKey, Decimal],
    rows: dict[ItemKey, tuple[Decimal, _Row]],
) -> dict[ItemKey, Decimal]:
    """Expenses / liabilities as positive magnitudes; mixed signs -> PLAN_SIGNS."""
    specs = {b.item: b for b in BLOCKS}
    if statement is Statement.GUV:
        expenses = [v for k, v in raw.items() if specs[k].sign == "expense" and v != 0]
        negative_convention = bool(expenses) and sum(v < 0 for v in expenses) * 2 >= len(expenses)
        passiva_sign = Decimal(1)
    else:
        negative_convention = False
        total = raw.get(ItemKey.TOTAL_EQUITY_LIABILITIES)
        passiva_sign = Decimal(-1) if total is not None and total < 0 else Decimal(1)
    out: dict[ItemKey, Decimal] = {}
    flipped: list[CellRef] = []
    for item, value in raw.items():
        kind = specs[item].sign
        if kind == "expense":
            expected_negative = negative_convention
            if value != 0 and (value < 0) != expected_negative:
                flipped.append(rows[item][1].cell.ref)
            out[item] = abs(value)
        elif kind in ("liability", "equity"):
            v = value * passiva_sign
            if kind == "liability" and v < 0:
                flipped.append(rows[item][1].cell.ref)
            out[item] = abs(v) if kind == "liability" else v
        elif kind in ("asset", "income"):
            if value < 0:
                flipped.append(rows[item][1].cell.ref)
            out[item] = abs(value)
        else:
            out[item] = value
    if flipped:
        ctx.add(finding(ErrorCode.PLAN_SIGNS, cells=", ".join(map(str, flipped)), refs=flipped))
    return out


# ── Bilanz extras: previous year, recomputed totals ──────────────────────────


def _previous_year(
    ctx: _Ctx, column: _Column, raw: dict[ItemKey, tuple[Decimal, _Row]], factor: Decimal
) -> dict[ItemKey, Decimal]:
    """Previous-year closing balances (for the opening-balance check), if that column exists."""
    prev = next(
        (
            h
            for h in _year_headers(ctx.wb.sheet(column.sheet))
            if h.header_row == column.header_row and h.header.year == column.header.year - 1
        ),
        None,
    )
    if prev is None:
        return {}
    out: dict[ItemKey, Decimal] = {}
    letter = get_column_letter(prev.col)
    for item in (ItemKey.BANK_DEBT, ItemKey.SHAREHOLDER_LOAN):
        if item in raw:
            cell = ctx.ev.read(column.sheet, f"{letter}{raw[item][1].row}")
            if cell.value is not None:
                out[item] = abs(cell.value * factor)
    return out


def _recomputed_totals(
    ctx: _Ctx,
    raw: dict[ItemKey, tuple[Decimal, _Row]],
    factor: Decimal,
    signed: dict[ItemKey, Decimal],
) -> dict[ItemKey, Decimal]:
    """Balance-sheet totals recomputed from input cells, ignoring cached sub-totals (D22)."""
    out: dict[ItemKey, Decimal] = {}
    for item in (ItemKey.TOTAL_ASSETS, ItemKey.TOTAL_EQUITY_LIABILITIES):
        if item not in raw or item not in signed:
            continue
        ref = raw[item][1].cell.ref
        try:
            value = ctx.ev.recompute(ref.sheet, ref.cell) * factor
        except (CellErrorValue, UnsupportedFormula):
            continue
        out[item] = abs(value)
        if out[item] != signed[item]:
            ctx.log.append(f"{ref}: cached {signed[item]} != recomputed {out[item]}")
    return out


# ── Finanzierung ─────────────────────────────────────────────────────────────


def _read_financing(ctx: _Ctx, sheet: str | None) -> LoanSchedule:
    if sheet is None:
        ctx.log.append("no loan schedule sheet")
        return LoanSchedule(loans=[], values={})
    ws = ctx.wb.sheet(sheet)
    header = find_header(ws)
    unit = _sheet_unit(ctx, sheet, header[0] if header else 3)
    schedule = read_loan_schedule(ws, ctx.ev, UNIT_FACTORS[unit], unit)
    for f in schedule.findings:
        ctx.add(f)
    ctx.log.extend(schedule.log)
    ctx.used.extend(schedule.used)
    return schedule


_UNWINDING_NOTE = re.compile(r"differenz.*aufzinsung|aufzinsung.*differenz")


def _derive_unwinding(
    ctx: _Ctx, values: dict[ItemKey, PlanValue], schedule: LoanSchedule, sheet: str | None
) -> None:
    """No "davon Aufzinsung" line: GuV interest - loan interest, if a note says so."""
    if ItemKey.PENSION_UNWINDING in values:
        return
    guv = values.get(ItemKey.INTEREST_EXPENSE)
    loans = values.get(ItemKey.INTEREST_ON_FINANCIAL_DEBT)
    note = None
    if sheet is not None:
        note = next(
            (t for t in _sheet_texts(ctx.wb.sheet(sheet)) if _UNWINDING_NOTE.search(normalise(t))),
            None,
        )
    if guv is None or loans is None or note is None:
        if guv is not None:
            ctx.add(
                finding(
                    ErrorCode.PLAN_SUBITEM_NOT_SHOWN, label="Aufzinsung Pensionsrückstellungen",
                    refs=[guv.ref] if guv.ref else [], items=[ItemKey.PENSION_UNWINDING],
                )
            )  # fmt: skip
        return
    derived = [*([guv.ref] if guv.ref else []), *loans.derived_from]
    values[ItemKey.PENSION_UNWINDING] = PlanValue(
        item=ItemKey.PENSION_UNWINDING,
        value=guv.value - loans.value,
        ref=None,
        label="Aufzinsung (GuV-Zinsaufwand abzüglich Zinsen laut Darlehensspiegel)",
        original_unit=guv.original_unit,
        derived_from=derived,
    )
    ctx.log.append("pension_unwinding derived as GuV interest - loan interest (note)")


# ── Facts ────────────────────────────────────────────────────────────────────


def _text_cells(wb: Workbook) -> list[TextCell]:
    return [
        TextCell(CellRef(sheet=ws.title, cell=c.coordinate), c.value)
        for ws in wb.formulas.worksheets
        for row in ws.iter_rows()
        for c in row
        if isinstance(c.value, str) and c.value.strip() and not c.value.startswith("=")
    ]


def _plan_facts(
    ctx: _Ctx, texts: list[TextCell], values: dict[ItemKey, PlanValue], schedule: LoanSchedule
) -> dict[FactKey, PlanFact]:
    shareholder = values.get(ItemKey.SHAREHOLDER_LOAN)
    has_shareholder = (shareholder is not None and shareholder.value != 0) or any(
        ln.kind == "shareholder" for ln in schedule.loans
    )
    cash = values.get(ItemKey.CASH)
    subordination, f1 = subordination_fact(texts, has_shareholder_loan=has_shareholder)
    cash_free, f2 = cash_fact(texts, has_cash=cash is not None and cash.value > 0)
    for f in (*f1, *f2):
        ctx.add(f)
    facts = {
        FactKey.QUALIFIED_SUBORDINATION: subordination,
        FactKey.CASH_FREELY_AVAILABLE: cash_free,
    }
    for fact in facts.values():
        ctx.log.append(f"fact {fact.key}={fact.value} ambiguous={fact.ambiguous} ({fact.evidence})")
    return facts


# ── Checks ───────────────────────────────────────────────────────────────────


def _run_checks(
    ctx: _Ctx,
    plan: Plan,
    path: Path,
    previous: dict[ItemKey, Decimal],
    recomputed: dict[ItemKey, Decimal],
    sheets: dict[Statement, str],
) -> None:
    results = [
        *checks.check_balance(plan, recomputed),
        *checks.check_loans_vs_balance(plan),
        *checks.check_interest_tieout(plan),
        *checks.check_opening_balances(plan, previous),
        *checks.check_bullets(plan),
        *checks.check_implausible(plan),
        *checks.check_status(plan),
        *checks.check_plan_year_filename(plan, path.name),
        *checks.check_missing(plan, reported=ctx.reported, sheets_present=set(sheets)),
        *checks.check_hidden([r for r in dict.fromkeys(ctx.used) if ctx.wb.is_hidden(r)]),
    ]
    for f in results:
        ctx.add(f)
