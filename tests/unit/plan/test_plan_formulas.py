"""Formula evaluator for cells without cached values."""

from decimal import Decimal
from pathlib import Path

import openpyxl
import pytest

from finn.plan.formulas import Evaluator
from finn.plan.workbook import Workbook, open_workbook


def _workbook(tmp_path: Path, cells: dict[str, object], other: dict[str, object]) -> Workbook:
    wb = openpyxl.Workbook()
    ws = wb.active
    assert ws is not None
    ws.title = "S"
    for coord, value in cells.items():
        ws[coord] = value
    ws2 = wb.create_sheet("Other Sheet")
    for coord, value in other.items():
        ws2[coord] = value
    path = tmp_path / "f.xlsx"
    wb.save(path)  # openpyxl writes no cached values
    loaded, findings = open_workbook(path)
    assert loaded is not None
    assert findings == []
    return loaded


@pytest.fixture
def ev(tmp_path: Path) -> Evaluator:
    cells: dict[str, object] = {
        "A1": 10, "A2": 2.5, "A3": -4, "A4": "Text", "A5": "#REF!",
        "B1": "=SUM(A1:A3)", "B2": "=A1+A2*2-(A3)", "B3": "=-'Other Sheet'!A1",
        "B4": "=SUM(A1:A4)", "B5": "=A1+A5", "B6": "=VLOOKUP(A1,A1:A3,1)",
        "B7": "=A1/0", "B8": "=B9", "B9": "=B8", "B10": "=SUMME(A1;A2)", "B11": "=A1+A4",
        "B12": "=B1+B2", "B13": "=[1]Sheet1!A1", "B14": "=Missing!A1",
    }  # fmt: skip
    return Evaluator(_workbook(tmp_path, cells, {"A1": 7}))


@pytest.mark.parametrize(
    ("cell", "value"),
    [
        ("B1", Decimal("8.5")),
        ("B2", Decimal(19)),
        ("B3", Decimal(-7)),
        ("B4", Decimal("8.5")),  # SUM ignores text
        ("B10", Decimal("12.5")),
        ("B12", Decimal("27.5")),  # nested formulas
    ],
)
def test_simple_formulas_are_recomputed(ev: Evaluator, cell: str, value: Decimal) -> None:
    result = ev.read("S", cell)
    assert (result.value, result.recomputed) == (value, True)


def test_inputs_are_leaf_cells(ev: Evaluator) -> None:
    assert [str(r) for r in ev.read("S", "B12").inputs] == ["S!A1", "S!A2", "S!A3"]


@pytest.mark.parametrize(("cell", "error"), [("B5", "#REF!"), ("B7", "#DIV/0!"), ("B14", "#REF!")])
def test_errors_propagate(ev: Evaluator, cell: str, error: str) -> None:
    assert ev.read("S", cell).error == error


@pytest.mark.parametrize("cell", ["B6", "B8", "B11", "B13"])
def test_unsupported_formulas_give_no_value(ev: Evaluator, cell: str) -> None:
    result = ev.read("S", cell)
    assert result.unsupported
    assert result.value is None


def test_input_values(ev: Evaluator) -> None:
    assert ev.read("S", "A2").value == Decimal("2.5")
    assert ev.read("S", "A4").text == "Text"
    assert ev.read("S", "A5").error == "#REF!"
    assert ev.read("S", "Z99").is_empty


def test_cached_value_preferred(tmp_path: Path) -> None:
    sample = Path(__file__).resolve().parents[3] / "material" / "Wirtschaftsplan_2027.xlsx"
    wb, _ = open_workbook(sample)
    assert wb is not None
    ev = Evaluator(wb)
    cached = ev.read("Bilanz", "D22")
    assert (cached.value, cached.recomputed, cached.formula) == (96100, False, "=SUM(D18:D21)")
    assert ev.recompute("Bilanz", "D34") == Decimal(313200)
