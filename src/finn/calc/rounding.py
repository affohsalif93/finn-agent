"""Contract rounding, comparison, headroom and the tight flag (D24).

ROUND_HALF_UP ("kaufmännisch"); the **rounded** value is compared; no rule → 2 decimals."""

from decimal import ROUND_HALF_UP, Decimal
from typing import Literal

DEFAULT_DECIMALS = 2

Operator = Literal[">=", "<="]
Unit = Literal["percent", "ratio", "amount"]


def effective_decimals(decimals: int | None) -> int:
    return DEFAULT_DECIMALS if decimals is None else decimals


def round_half_up(value: Decimal, decimals: int) -> Decimal:
    return value.quantize(Decimal(1).scaleb(-decimals), rounding=ROUND_HALF_UP)


def is_met(rounded: Decimal, operator: Operator, threshold: Decimal) -> bool:
    return rounded >= threshold if operator == ">=" else rounded <= threshold


def headroom(rounded: Decimal, operator: Operator, threshold: Decimal) -> Decimal:
    """Distance to the threshold in the covenant's unit; negative = breached."""
    return rounded - threshold if operator == ">=" else threshold - rounded


def headroom_unit(unit: Unit) -> Literal["pp", "ratio", "T€"]:
    if unit == "percent":
        return "pp"
    if unit == "amount":
        return "T€"
    return "ratio"


def is_tight(head: Decimal, threshold: Decimal, ratio: Decimal) -> bool:
    """Met, but headroom below `ratio` × threshold (default 10 %)."""
    return head >= 0 and head < ratio * abs(threshold)


def boundary(operator: Operator, threshold: Decimal, decimals: int) -> tuple[Decimal, bool]:
    """Raw-value boundary of the rounded comparison: (value, inclusive).

    `>= T` holds after rounding iff raw >= T − ½ unit (inclusive); `<= T` iff raw < T + ½ unit.
    """
    half = Decimal(5).scaleb(-(decimals + 1))
    if operator == ">=":
        return threshold - half, True
    return threshold + half, False
