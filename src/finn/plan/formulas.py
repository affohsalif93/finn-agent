"""Cell values, with a small evaluator for simple formulas (D22 level 2).

`openpyxl` cannot evaluate formulas and files saved by it carry no cached results. Sub-totals in
plans are simple (`=SUM(D5:D9)`, `=D8+D13+D14`, `=-GuV!D13`), so we recompute those from the
input cells: numbers, cell / range references (also on other sheets), `+ - * /`, parentheses,
`SUM`. Anything else (other functions, external links) is reported as unsupported.
"""

import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from decimal import Decimal, DivisionByZero, InvalidOperation

from openpyxl.utils.cell import column_index_from_string, get_column_letter

from finn.models.plan import CellRef
from finn.numbers import to_decimal
from finn.plan.workbook import Workbook

ERROR_VALUES = frozenset(
    {"#REF!", "#DIV/0!", "#N/A", "#NV", "#NAME?", "#NUM!", "#NULL!", "#VALUE!", "#WERT!",
     "#BEZUG!", "#ZAHL!", "#NAME", "#GETTING_DATA", "#SPILL!", "#CALC!"}
)  # fmt: skip
_SUM_NAMES = frozenset({"SUM", "SUMME"})

_TOKEN = re.compile(
    r"""\s*(?:
      (?P<num>\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)
    | (?P<func>[A-Za-z][A-Za-z0-9.]*)\(
    | (?P<ref>(?:(?:'(?P<qsheet>[^']+)'|(?P<sheet>[^\s'!()+\-*/,;:]+))!)?
              \$?(?P<c1>[A-Za-z]{1,3})\$?(?P<r1>\d+)
              (?::\$?(?P<c2>[A-Za-z]{1,3})\$?(?P<r2>\d+))?)
    | (?P<err>\#[A-Z0-9/!?]+)
    | (?P<op>[-+*/(),;])
    )""",
    re.VERBOSE,
)


class UnsupportedFormula(Exception):
    """Formula we cannot evaluate (function, external link, cycle, text operand)."""


class CellErrorValue(Exception):
    """The cell (or a cell it depends on) holds an Excel error such as #REF!."""

    def __init__(self, error: str, ref: CellRef) -> None:
        super().__init__(f"{ref}: {error}")
        self.error = error
        self.ref = ref


@dataclass(frozen=True)
class CellValue:
    """What one cell holds, resolved."""

    ref: CellRef
    value: Decimal | None = None  # number (None: empty, text, error or unsupported)
    text: str | None = None
    error: str | None = None  # e.g. "#REF!" (also when inherited from a referenced cell)
    formula: str | None = None
    recomputed: bool = False  # formula without cached value, evaluated by us
    unsupported: bool = False  # formula without cached value we could not evaluate
    inputs: tuple[CellRef, ...] = ()  # input cells used when recomputed
    external: bool = False  # formula refers to another workbook

    @property
    def is_number(self) -> bool:
        return self.value is not None

    @property
    def is_empty(self) -> bool:
        return self.value is None and self.text is None and self.error is None and not self.formula


@dataclass
class Evaluator:
    """Reads cells of one workbook; recomputes formulas that have no cached value."""

    wb: Workbook
    _memo: dict[tuple[str, str, bool], tuple[Decimal, frozenset[CellRef]]] = field(
        default_factory=dict
    )

    def read(self, sheet: str, cell: str) -> CellValue:
        """Cell value: input, cached formula result, or recomputed result."""
        ref = CellRef(sheet=sheet, cell=cell)
        raw = self.wb.raw(sheet, cell)
        if raw is None:
            return CellValue(ref=ref)
        if isinstance(raw, str) and raw.startswith("="):
            return self._read_formula(ref, raw)
        return _input_value(ref, raw)

    def recompute(self, sheet: str, cell: str) -> Decimal:
        """Evaluate from input cells, ignoring all cached values (balance check)."""
        value, _ = self._eval_cell(sheet, cell, use_cache=False, stack=())
        return value

    def _read_formula(self, ref: CellRef, formula: str) -> CellValue:
        external = "[" in formula
        cached = self.wb.cached(ref.sheet, ref.cell)
        if isinstance(cached, str) and cached.strip() in ERROR_VALUES:
            return CellValue(ref=ref, error=cached.strip(), formula=formula, external=external)
        if cached is not None and not isinstance(cached, str | bool):
            return CellValue(ref=ref, value=to_decimal(cached), formula=formula, external=external)
        if cached is not None:
            return CellValue(ref=ref, text=str(cached), formula=formula, external=external)
        try:
            value, inputs = self._eval_cell(ref.sheet, ref.cell, use_cache=True, stack=())
        except CellErrorValue as exc:
            return CellValue(ref=ref, error=exc.error, formula=formula)
        except UnsupportedFormula:
            return CellValue(ref=ref, formula=formula, unsupported=True, external=external)
        ordered = tuple(sorted(inputs, key=_ref_sort_key))
        return CellValue(ref=ref, value=value, formula=formula, recomputed=True, inputs=ordered)

    def _eval_cell(
        self, sheet: str, cell: str, *, use_cache: bool, stack: tuple[tuple[str, str], ...]
    ) -> tuple[Decimal, frozenset[CellRef]]:
        key = (sheet, cell, use_cache)
        if key in self._memo:
            return self._memo[key]
        if (sheet, cell) in stack:
            raise UnsupportedFormula(f"circular reference at {sheet}!{cell}")
        ref = CellRef(sheet=sheet, cell=cell)
        raw = self.wb.raw(sheet, cell)
        if isinstance(raw, str) and raw.startswith("="):
            result = self._eval_formula_cell(ref, raw, use_cache, (*stack, (sheet, cell)))
        else:
            result = (_leaf_number(ref, raw), frozenset({ref}))
        self._memo[key] = result
        return result

    def _eval_formula_cell(
        self, ref: CellRef, formula: str, use_cache: bool, stack: tuple[tuple[str, str], ...]
    ) -> tuple[Decimal, frozenset[CellRef]]:
        if use_cache:
            cached = self.wb.cached(ref.sheet, ref.cell)
            if isinstance(cached, str) and cached.strip() in ERROR_VALUES:
                raise CellErrorValue(cached.strip(), ref)
            if isinstance(cached, int | float) and not isinstance(cached, bool):
                return to_decimal(cached), frozenset({ref})
        if "[" in formula:
            raise UnsupportedFormula("external reference")
        parser = _Parser(self, ref, use_cache, stack)
        return parser.parse(formula[1:])

    def range_cells(self, sheet: str, c1: str, r1: int, c2: str, r2: int) -> Iterator[str]:
        lo_c, hi_c = sorted((column_index_from_string(c1), column_index_from_string(c2)))
        lo_r, hi_r = sorted((r1, r2))
        for col in range(lo_c, hi_c + 1):
            for row in range(lo_r, hi_r + 1):
                yield f"{get_column_letter(col)}{row}"


def _input_value(ref: CellRef, raw: object) -> CellValue:
    if isinstance(raw, str):
        stripped = raw.strip()
        if stripped in ERROR_VALUES:
            return CellValue(ref=ref, error=stripped)
        try:
            return CellValue(ref=ref, value=to_decimal(stripped))
        except ValueError:
            return CellValue(ref=ref, text=raw)
    if isinstance(raw, int | float | Decimal) and not isinstance(raw, bool):
        return CellValue(ref=ref, value=to_decimal(raw))
    return CellValue(ref=ref, text=str(raw))


def _leaf_number(ref: CellRef, raw: object) -> Decimal:
    if raw is None:
        return Decimal(0)
    value = _input_value(ref, raw)
    if value.error:
        raise CellErrorValue(value.error, ref)
    if value.value is None:
        raise UnsupportedFormula(f"text operand at {ref}")
    return value.value


def _ref_sort_key(ref: CellRef) -> tuple[str, int, str]:
    letters = re.sub(r"\d", "", ref.cell)
    digits = re.sub(r"\D", "", ref.cell)
    return ref.sheet, int(digits), letters


class _Parser:
    """Recursive descent over `+ - * /`, unary minus, parentheses, refs, ranges in SUM."""

    def __init__(
        self,
        ev: Evaluator,
        ref: CellRef,
        use_cache: bool,
        stack: tuple[tuple[str, str], ...],
    ) -> None:
        self.ev = ev
        self.ref = ref
        self.use_cache = use_cache
        self.stack = stack
        self.tokens: list[re.Match[str]] = []
        self.pos = 0
        self.inputs: set[CellRef] = set()

    def parse(self, text: str) -> tuple[Decimal, frozenset[CellRef]]:
        self.tokens = _tokenize(text)
        value = self._expr()
        if self.pos != len(self.tokens):
            raise UnsupportedFormula(f"unexpected token in {text!r}")
        return value, frozenset(self.inputs)

    def _peek(self) -> re.Match[str] | None:
        return self.tokens[self.pos] if self.pos < len(self.tokens) else None

    def _next(self) -> re.Match[str]:
        tok = self._peek()
        if tok is None:
            raise UnsupportedFormula("unexpected end of formula")
        self.pos += 1
        return tok

    def _peek_op(self, *ops: str) -> str | None:
        tok = self._peek()
        if tok is not None and tok.group("op") in ops:
            return tok.group("op")
        return None

    def _expr(self) -> Decimal:
        value = self._term()
        while op := self._peek_op("+", "-"):
            self.pos += 1
            rhs = self._term()
            value = value + rhs if op == "+" else value - rhs
        return value

    def _term(self) -> Decimal:
        value = self._unary()
        while op := self._peek_op("*", "/"):
            self.pos += 1
            rhs = self._unary()
            if op == "*":
                value = value * rhs
            else:
                try:
                    value = value / rhs
                except (DivisionByZero, InvalidOperation) as exc:
                    raise CellErrorValue("#DIV/0!", self.ref) from exc
        return value

    def _unary(self) -> Decimal:
        if op := self._peek_op("+", "-"):
            self.pos += 1
            value = self._unary()
            return -value if op == "-" else value
        return self._primary()

    def _primary(self) -> Decimal:
        tok = self._next()
        if tok.group("num"):
            return Decimal(tok.group("num"))
        if tok.group("err"):
            raise CellErrorValue(tok.group("err"), self.ref)
        if tok.group("op") == "(":
            value = self._expr()
            self._expect(")")
            return value
        if tok.group("func"):
            return self._function(tok.group("func"))
        if tok.group("ref"):
            if tok.group("c2"):
                raise UnsupportedFormula("range outside SUM")
            return self._cell(tok)
        raise UnsupportedFormula(f"unexpected token {tok.group(0)!r}")

    def _expect(self, op: str) -> None:
        if self._next().group("op") != op:
            raise UnsupportedFormula(f"expected {op!r}")

    def _function(self, name: str) -> Decimal:
        if name.upper() not in _SUM_NAMES:
            raise UnsupportedFormula(f"function {name}")
        total = Decimal(0)
        if self._peek_op(")"):
            self.pos += 1
            return total
        while True:
            total += self._sum_arg()
            sep = self._next().group("op")
            if sep == ")":
                return total
            if sep not in (",", ";"):
                raise UnsupportedFormula("bad SUM arguments")

    def _sum_arg(self) -> Decimal:
        tok = self._peek()
        if tok is not None and tok.group("ref") and tok.group("c2"):
            self.pos += 1
            return self._range_sum(tok)
        return self._expr()

    def _sheet_of(self, tok: re.Match[str]) -> str:
        name = tok.group("qsheet") or tok.group("sheet")
        if not name:
            return self.ref.sheet
        resolved = self.ev.wb.resolve_sheet(name)
        if resolved is None:
            raise CellErrorValue("#REF!", self.ref)
        return resolved

    def _cell(self, tok: re.Match[str]) -> Decimal:
        sheet = self._sheet_of(tok)
        coord = f"{tok.group('c1').upper()}{tok.group('r1')}"
        value, inputs = self.ev._eval_cell(sheet, coord, use_cache=self.use_cache, stack=self.stack)
        self.inputs |= inputs
        return value

    def _range_sum(self, tok: re.Match[str]) -> Decimal:
        sheet = self._sheet_of(tok)
        total = Decimal(0)
        cells = self.ev.range_cells(
            sheet, tok.group("c1"), int(tok.group("r1")), tok.group("c2"), int(tok.group("r2"))
        )
        for coord in cells:
            raw = self.ev.wb.raw(sheet, coord)
            if isinstance(raw, str) and not raw.startswith("="):
                if raw.strip() in ERROR_VALUES:
                    raise CellErrorValue(raw.strip(), CellRef(sheet=sheet, cell=coord))
                continue  # SUM ignores text
            if raw is None:
                continue
            value, inputs = self.ev._eval_cell(
                sheet, coord, use_cache=self.use_cache, stack=self.stack
            )
            self.inputs |= inputs
            total += value
        return total


def _tokenize(text: str) -> list[re.Match[str]]:
    tokens: list[re.Match[str]] = []
    pos = 0
    text = text.rstrip()
    while pos < len(text):
        match = _TOKEN.match(text, pos)
        if match is None or match.end() == pos:
            raise UnsupportedFormula(f"cannot tokenize {text[pos:]!r}")
        tokens.append(match)
        pos = match.end()
    return tokens
