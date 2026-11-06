"""numbers.py: exact Decimal conversion, German parse and format."""

from decimal import Decimal

import pytest

from finn.numbers import format_de, to_decimal


def test_float_goes_through_str() -> None:
    assert to_decimal(0.1) == Decimal("0.1")
    assert to_decimal(0.1) != Decimal(0.1)  # noqa: RUF032  (the binary float is not 0.1)
    assert to_decimal(0.1) + to_decimal(0.2) == Decimal("0.3")


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (148200, Decimal(148200)),
        (-21500, Decimal(-21500)),
        (34.32, Decimal("34.32")),
        (Decimal("1.5"), Decimal("1.5")),
        ("148.200", Decimal(148200)),
        ("1.234.567", Decimal(1234567)),
        ("34,32", Decimal("34.32")),
        ("1.234,56", Decimal("1234.56")),
        ("-1.250", Decimal(-1250)),
        ("−1.250", Decimal(-1250)),  # Unicode minus
        ("(3.000)", Decimal(-3000)),  # accounting negative
        ("148.200 T€", Decimal(148200)),
        ("34,32 %", Decimal("34.32")),
        ("0.1", Decimal("0.1")),  # dot as decimal point if not a thousands pattern
        (" 12 600 ", Decimal(12600)),
    ],
)
def test_to_decimal(raw: object, expected: Decimal) -> None:
    assert to_decimal(raw) == expected


@pytest.mark.parametrize("raw", [None, True, "abc", "", float("nan"), float("inf"), [1]])
def test_to_decimal_rejects_non_numbers(raw: object) -> None:
    with pytest.raises(ValueError, match="not a"):
        to_decimal(raw)


@pytest.mark.parametrize(
    ("value", "decimals", "unit", "expected"),
    [
        (Decimal(148200), 0, "T€", "148.200 T€"),
        (Decimal("34.3199"), 2, "%", "34,32 %"),
        (Decimal("3.3675"), 2, None, "3,37"),
        (Decimal("3.365"), 2, None, "3,37"),  # ROUND_HALF_UP, not banker's rounding
        (Decimal("2.5"), 0, None, "3"),
        (Decimal("-1234567.891"), 1, None, "-1.234.567,9"),
        (Decimal("-0.001"), 2, None, "0,00"),  # no "-0,00"
        (Decimal(170), 0, None, "170"),
        (Decimal("0.5"), 3, None, "0,500"),
    ],
)
def test_format_de(value: Decimal, decimals: int, unit: str | None, expected: str) -> None:
    assert format_de(value, decimals, unit) == expected


def test_round_trip() -> None:
    assert to_decimal(format_de(Decimal("1234567.89"), 2)) == Decimal("1234567.89")
