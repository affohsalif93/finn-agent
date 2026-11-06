"""Generate plan variants from the sample plan (docs/FAILURES.md fixtures), reproducibly.

    uv run python scripts/make_plan_fixtures.py [OUT_DIR]   # default: tests/fixtures/plans/

The tests call `build_all(tmp_dir)` instead, so no generated binaries are committed. Each variant
documents in code what it changes. Files saved by openpyxl lose Excel's cached formula results;
the plan reader recomputes simple formulas (SUM, + - * /), which these variants rely on.
"""

import re
import sys
from collections.abc import Callable
from copy import copy
from datetime import UTC, datetime
from pathlib import Path

import openpyxl
from openpyxl.utils.cell import column_index_from_string, get_column_letter
from openpyxl.workbook.workbook import Workbook

ROOT = Path(__file__).resolve().parents[1]
SAMPLE = ROOT / "material" / "Wirtschaftsplan_2027.xlsx"
DEFAULT_OUT = ROOT / "tests" / "fixtures" / "plans"
FIXED_TIME = datetime(2026, 11, 1, tzinfo=UTC).replace(tzinfo=None)

_REF = re.compile(r"(?P<prefix>(?:'[^']+'|[A-Za-z0-9_]+)!)?(?P<c>\$?[A-Z]{1,3})(?P<r>\$?\d+)\b")
_YEAR = re.compile(r"(?<![\d-])(20[1-3]\d)(?![\d-])")

CellMap = Callable[[int, int], tuple[int, int]]  # (col, row) -> (col, row)


# ── Helpers ──────────────────────────────────────────────────────────────────


def _rewrite_refs(wb: Workbook, sheet: str, fn: CellMap) -> None:
    """Rewrite every formula reference that points into `sheet` with `fn`."""
    for ws in wb.worksheets:
        for row in ws.iter_rows():
            for cell in row:
                if isinstance(cell.value, str) and cell.value.startswith("="):
                    cell.value = _REF.sub(lambda m, ws=ws: _map_ref(m, ws.title, sheet, fn),
                                          cell.value)  # fmt: skip


def _map_ref(m: re.Match[str], own_sheet: str, sheet: str, fn: CellMap) -> str:
    prefix = m.group("prefix") or ""
    target = prefix[:-1].strip("'") if prefix else own_sheet
    if target != sheet:
        return m.group(0)
    col_abs = "$" if m.group("c").startswith("$") else ""
    row_abs = "$" if m.group("r").startswith("$") else ""
    col = column_index_from_string(m.group("c").lstrip("$"))
    new_col, new_row = fn(col, int(m.group("r").lstrip("$")))
    return f"{prefix}{col_abs}{get_column_letter(new_col)}{row_abs}{new_row}"


def _move_cells(wb: Workbook, sheet: str, fn: CellMap) -> None:
    """Move all cells of `sheet` with `fn` (values and styles); references are rewritten first."""
    _rewrite_refs(wb, sheet, fn)
    ws = wb[sheet]
    cells = [(c.column, c.row, c.value, copy(c._style)) for row in ws.iter_rows() for c in row]
    for col, row, _, _ in cells:
        ws.cell(row=row, column=col).value = None
    for col, row, value, style in cells:
        new_col, new_row = fn(col, row)
        target = ws.cell(row=new_row, column=new_col)
        target.value = value
        target._style = style


def insert_rows(wb: Workbook, sheet: str, at: int, n: int) -> None:
    """Insert `n` empty rows before row `at`, adjusting formulas (openpyxl does not)."""
    _move_cells(wb, sheet, lambda c, r: (c, r + n if r >= at else r))


def swap_columns(wb: Workbook, sheet: str, a: str, b: str) -> None:
    ia, ib = column_index_from_string(a), column_index_from_string(b)
    _move_cells(wb, sheet, lambda c, r: (ib if c == ia else ia if c == ib else c, r))


def rename_sheet(wb: Workbook, old: str, new: str) -> None:
    wb[old].title = new
    for ws in wb.worksheets:
        for row in ws.iter_rows():
            for cell in row:
                if isinstance(cell.value, str) and cell.value.startswith("="):
                    cell.value = re.sub(rf"\b{re.escape(old)}!", f"{new}!", cell.value)


def map_strings(wb: Workbook, fn: Callable[[str], str]) -> None:
    for ws in wb.worksheets:
        for row in ws.iter_rows():
            for cell in row:
                if isinstance(cell.value, str) and not cell.value.startswith("="):
                    cell.value = fn(cell.value)


def find_cell(wb: Workbook, sheet: str, startswith: str) -> str:
    """Coordinate of the first cell in column A whose text starts with `startswith`."""
    for (cell,) in wb[sheet].iter_rows(min_col=1, max_col=1):
        if isinstance(cell.value, str) and cell.value.startswith(startswith):
            return cell.coordinate
    raise KeyError(f"{sheet}: no label starting with {startswith!r}")


# ── Variants ─────────────────────────────────────────────────────────────────


def plan_2026(wb: Workbook) -> None:
    """Every year one earlier: content is Plan 2026 (loan numbers untouched)."""
    map_strings(wb, lambda s: _YEAR.sub(lambda m: str(int(m.group(1)) - 1), s))


def plan_unbalanced(wb: Workbook) -> None:
    """Sonstige Rückstellungen 7.000 -> 6.700: Passiva 312.900 vs. Aktiva 313.200."""
    wb["Bilanz"]["D26"] = 6700


def plan_renamed_labels(wb: Workbook) -> None:
    """Synonyms for sheet names, row labels and plan-year headers."""
    for old, new in (("GuV", "Erfolgsplan"), ("Bilanz", "Planbilanz"),
                     ("Finanzierung", "Darlehensspiegel")):  # fmt: skip
        rename_sheet(wb, old, new)
    guv, bil = wb["Erfolgsplan"], wb["Planbilanz"]
    guv["D3"] = "2027 (Plan)"
    guv["A9"] = "Abschreibungen"
    guv["A12"] = "Zinserträge"
    guv["A13"] = "Zinsaufwand"
    guv["A14"] = "davon Aufzinsung Pensionsrückstellungen"
    guv["A17"] = "Ertragsteuern"
    guv["A18"] = "Jahresergebnis"
    bil["D3"] = "Plan 31.12.2027"
    bil["A12"] = "Flüssige Mittel"
    bil["A15"] = "Bilanzsumme"
    bil["A22"] = "Eigenmittel"
    bil["A23"] = "Sonderposten für Zuschüsse"
    bil["A28"] = "Bankdarlehen"
    bil["A29"] = "Gesellschafterdarlehen (nachrangig)"


def plan_moved_rows(wb: Workbook) -> None:
    """Rows inserted in all statements; in the GuV the plan column moves from D to B."""
    insert_rows(wb, "GuV", 2, 2)
    wb["GuV"]["A2"] = "Version 3 (Arbeitsstand Controlling)"
    swap_columns(wb, "GuV", "B", "D")
    insert_rows(wb, "Bilanz", 18, 2)
    wb["Bilanz"]["A18"] = "Ausstehende Einlagen (nicht eingefordert)"
    wb["Bilanz"]["D18"] = 0
    insert_rows(wb, "Finanzierung", 4, 1)


def plan_in_eur(wb: Workbook) -> None:
    """Finanzierung in € (inputs x1000, title "in €"); other sheets stay in T€."""
    ws = wb["Finanzierung"]
    ws["A1"] = ws["A1"].value.replace("in T€", "in €")
    for row in ws.iter_rows(min_row=4):
        for cell in row:
            if isinstance(cell.value, int | float) and not isinstance(cell.value, bool):
                cell.value = cell.value * 1000


def plan_no_units(wb: Workbook) -> None:
    """No unit on any sheet title and none on the cover."""
    for name in ("GuV", "Bilanz", "Finanzierung"):
        ws = wb[name]
        ws["A1"] = ws["A1"].value.replace(" – in T€", "").replace(" in T€", "")
    wb["Deckblatt"]["A6"] = "Alle Beträge soweit nicht anders angegeben."


def plan_no_financing(wb: Workbook) -> None:
    """Loan schedule sheet removed."""
    del wb["Finanzierung"]


def plan_no_subordination_note(wb: Workbook) -> None:
    """Note 2 removed and no "Rangrücktritt" left in any label."""
    bil = wb["Bilanz"]
    bil[find_cell(wb, "Bilanz", "2) Gesellschafterdarlehen")] = None
    bil["A29"] = "Verbindlichkeiten gegenüber Gesellschafter (Darlehen Stadt Musterstadt)"
    wb["Finanzierung"]["A13"] = "Gesellschafterdarlehen Stadt Musterstadt"


def plan_ref_error(wb: Workbook) -> None:
    """Income taxes = #REF! (Jahresüberschuss depends on it)."""
    wb["GuV"]["D17"] = "#REF!"


def plan_no_cache(wb: Workbook) -> None:
    """Equity as a SUBTOTAL formula: not evaluable without Excel's cached value."""
    wb["Bilanz"]["D22"] = "=SUBTOTAL(9,D18:D21)"


def plan_signs(wb: Workbook) -> None:
    """Taxes entered positive and subtracted, while other expenses are negative."""
    wb["GuV"]["D17"] = 3200
    wb["GuV"]["D18"] = "=D16-D17"


def plan_loans_mismatch(wb: Workbook) -> None:
    """Bilanz bank debt 150.000 (still balanced via other liabilities)."""
    wb["Bilanz"]["D28"] = 150000
    wb["Bilanz"]["D31"] = 3400


def plan_interest_off(wb: Workbook) -> None:
    """Altdarlehen interest 820 -> 900: loan interest + unwinding != GuV interest."""
    wb["Finanzierung"]["G9"] = 900


def plan_opening_off(wb: Workbook) -> None:
    """Bilanz 31.12.2026 bank debt 154.000 vs. loan schedule opening 153.950."""
    wb["Bilanz"]["C28"] = 154000


def plan_actuals_only(wb: Workbook) -> None:
    """Plan columns relabelled as actuals."""
    wb["GuV"]["D3"] = "Ist 2027"
    wb["Bilanz"]["D3"] = "31.12.2027 (Ist)"


def plan_hidden(wb: Workbook) -> None:
    """Sonderposten row hidden."""
    wb["Bilanz"].row_dimensions[23].hidden = True


def _drop_unwinding_line(wb: Workbook) -> None:
    for col in "ABCD":
        wb["GuV"][f"{col}14"] = None


def plan_unwinding_from_note(wb: Workbook) -> None:
    """No "davon Aufzinsung" line; Finanzierung note 3 says the difference is the unwinding."""
    _drop_unwinding_line(wb)


def plan_no_unwinding(wb: Workbook) -> None:
    """No "davon Aufzinsung" line and no note explaining the interest difference."""
    _drop_unwinding_line(wb)
    wb["Finanzierung"][find_cell(wb, "Finanzierung", "3) Zinsaufwand")] = None


def plan_bullet(wb: Workbook) -> None:
    """Sparkasse loan repaid in full in 2027 (closing 0), no note on refinancing.

    Bilanz kept consistent: bank debt 148.200 - 18.750, Sachanlagen reduced by the same amount.
    """
    wb["Finanzierung"]["D5"] = 20000
    wb["Bilanz"]["D28"] = 148200 - 18750
    wb["Bilanz"]["D6"] = 258500 - 18750


VARIANTS: dict[str, Callable[[Workbook], None]] = {
    "plan_2026.xlsx": plan_2026,
    "plan_unbalanced.xlsx": plan_unbalanced,
    "plan_renamed_labels.xlsx": plan_renamed_labels,
    "plan_moved_rows.xlsx": plan_moved_rows,
    "plan_in_eur.xlsx": plan_in_eur,
    "plan_no_units.xlsx": plan_no_units,
    "plan_no_financing.xlsx": plan_no_financing,
    "plan_no_subordination_note.xlsx": plan_no_subordination_note,
    "plan_ref_error.xlsx": plan_ref_error,
    "plan_no_cache.xlsx": plan_no_cache,
    "plan_signs.xlsx": plan_signs,
    "plan_loans_mismatch.xlsx": plan_loans_mismatch,
    "plan_interest_off.xlsx": plan_interest_off,
    "plan_opening_off.xlsx": plan_opening_off,
    "plan_actuals_only.xlsx": plan_actuals_only,
    "plan_hidden.xlsx": plan_hidden,
    "plan_bullet.xlsx": plan_bullet,
    "plan_unwinding_from_note.xlsx": plan_unwinding_from_note,
    "plan_no_unwinding.xlsx": plan_no_unwinding,
}

# Not workbooks: only the first bytes matter for the detection.
RAW_FILES: dict[str, bytes] = {
    "plan_corrupt.xlsx": b"This is not a spreadsheet.\x00\x01\x02" * 8,
    # An encrypted OOXML file is an OLE container with an "EncryptionInfo" stream.
    "plan_password.xlsx": bytes.fromhex("D0CF11E0A1B11AE1") + b"\x00" * 504
    + "EncryptionInfo".encode("utf-16-le") + "EncryptedPackage".encode("utf-16-le"),
    # A legacy .xls is an OLE container without it.
    "plan.xls": bytes.fromhex("D0CF11E0A1B11AE1") + b"\x00" * 504
    + "Workbook".encode("utf-16-le"),
}  # fmt: skip


def build(name: str, out_dir: Path, sample: Path = SAMPLE) -> Path:
    target = out_dir / name
    if name in RAW_FILES:
        target.write_bytes(RAW_FILES[name])
        return target
    wb = openpyxl.load_workbook(sample)
    VARIANTS[name](wb)
    wb.properties.created = FIXED_TIME
    wb.properties.modified = FIXED_TIME
    wb.save(target)
    return target


def build_all(out_dir: Path, sample: Path = SAMPLE) -> dict[str, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    return {name: build(name, out_dir, sample) for name in [*VARIANTS, *RAW_FILES]}


def main() -> None:
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_OUT
    for name, path in build_all(out).items():
        print(f"{name:36} {path.stat().st_size:>7} bytes")


if __name__ == "__main__":
    main()
