"""Open a workbook safely; units per sheet; hidden sheets and rows.

The file is loaded twice: `data_only=False` gives inputs and formula text, `data_only=True` the
values Excel cached when the file was last saved (missing after a save by openpyxl).
"""

import hashlib
import io
import re
import warnings
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

import openpyxl
from openpyxl.workbook.workbook import Workbook as OpenpyxlWorkbook
from openpyxl.worksheet.worksheet import Worksheet

from finn.errors import ErrorCode, Finding, finding
from finn.models.plan import CellRef

OLE_MAGIC = bytes.fromhex("D0CF11E0A1B11AE1")
ZIP_MAGIC = b"PK\x03\x04"
# Name of the stream an encrypted OOXML file carries inside its OLE container (UTF-16 LE).
_ENCRYPTION_INFO = "EncryptionInfo".encode("utf-16-le")


@dataclass(frozen=True)
class Workbook:
    """Both views of one workbook plus file metadata."""

    path: Path
    sha256: str
    formulas: OpenpyxlWorkbook
    values: OpenpyxlWorkbook

    @property
    def sheet_names(self) -> list[str]:
        return list(self.formulas.sheetnames)

    def sheet(self, name: str) -> Worksheet:
        return self.formulas[name]

    def resolve_sheet(self, name: str) -> str | None:
        """Excel sheet names are case-insensitive."""
        for candidate in self.formulas.sheetnames:
            if candidate.casefold() == name.casefold():
                return candidate
        return None

    def raw(self, sheet: str, cell: str) -> object:
        """Input value or formula text."""
        return self.formulas[sheet][cell].value

    def cached(self, sheet: str, cell: str) -> object:
        """Value cached by Excel (None for formulas saved without a result)."""
        return self.values[sheet][cell].value

    def is_hidden(self, ref: CellRef) -> bool:
        ws = self.formulas[ref.sheet]
        if ws.sheet_state != "visible":
            return True
        row = int(re.sub(r"\D", "", ref.cell))
        return bool(ws.row_dimensions[row].hidden)


def open_workbook(path: Path) -> tuple[Workbook | None, list[Finding]]:
    """Open `path`, or return None with an ASK finding (encrypted, legacy .xls, corrupt)."""
    data = path.read_bytes()
    file = path.name
    if data.startswith(OLE_MAGIC):
        if _ENCRYPTION_INFO in data:
            return None, [finding(ErrorCode.XLSX_ENCRYPTED, file=file)]
        return None, [finding(ErrorCode.ATTACHMENT_XLS_LEGACY, file=file)]
    if path.suffix.lower() == ".xls":
        return None, [finding(ErrorCode.ATTACHMENT_XLS_LEGACY, file=file)]
    if not data.startswith(ZIP_MAGIC):
        return None, [finding(ErrorCode.XLSX_CORRUPT, file=file)]
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")  # openpyxl warns about unsupported extensions
            formulas = openpyxl.load_workbook(io.BytesIO(data), data_only=False)
            values = openpyxl.load_workbook(io.BytesIO(data), data_only=True)
    except Exception:  # openpyxl raises many types for broken packages
        return None, [finding(ErrorCode.XLSX_CORRUPT, file=file)]
    findings: list[Finding] = []
    if path.suffix.lower() == ".xlsm":
        findings.append(finding(ErrorCode.ATTACHMENT_MACROS, file=file))
    sha = hashlib.sha256(data).hexdigest()
    return Workbook(path=path, sha256=sha, formulas=formulas, values=values), findings


# ── Units (D24) ──────────────────────────────────────────────────────────────

UNIT_TEUR = "T€"
UNIT_EUR = "€"
UNIT_MIO = "Mio. €"
# Multiply a value in this unit by the factor to get T€.
UNIT_FACTORS: dict[str, Decimal] = {
    UNIT_TEUR: Decimal(1),
    UNIT_EUR: Decimal("0.001"),
    UNIT_MIO: Decimal(1000),
}
_UNIT_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    (
        UNIT_TEUR,
        re.compile(
            r"(?<![A-Za-z])(T\s?€|TEUR|T\s?EUR|k€|kEUR|Tsd\.?\s?(€|EUR|Euro)|Tausend\s+Euro)",
            re.IGNORECASE,
        ),
    ),
    (UNIT_MIO, re.compile(r"(Mio\.?\s?(€|EUR|Euro)|Millionen\s+Euro)", re.IGNORECASE)),
    (UNIT_EUR, re.compile(r"(?<![A-Za-z])(€|EUR|Euro)(?![A-Za-z])", re.IGNORECASE)),
]


@dataclass(frozen=True)
class SheetUnit:
    unit: str | None  # None: no or conflicting unit label
    source: CellRef | None
    conflicting: bool = False

    @property
    def factor(self) -> Decimal | None:
        return UNIT_FACTORS[self.unit] if self.unit else None


def units_in_text(text: str) -> set[str]:
    """All units mentioned in `text` ("in T€ (Tausend Euro)" -> {"T€"})."""
    found: set[str] = set()
    rest = text
    for unit, pattern in _UNIT_PATTERNS:
        if pattern.search(rest):
            found.add(unit)
            rest = pattern.sub(" ", rest)
    return found


def detect_unit(ws: Worksheet, last_row: int) -> SheetUnit:
    """Unit label in the title / header area (rows 1..last_row); notes below are ignored."""
    found: dict[str, CellRef] = {}
    for row in ws.iter_rows(min_row=1, max_row=max(last_row, 1)):
        for cell in row:
            if isinstance(cell.value, str) and not cell.value.startswith("="):
                for unit in units_in_text(cell.value):
                    found.setdefault(unit, CellRef(sheet=ws.title, cell=cell.coordinate))
    if len(found) == 1:
        unit, ref = next(iter(found.items()))
        return SheetUnit(unit=unit, source=ref)
    return SheetUnit(unit=None, source=None, conflicting=len(found) > 1)
