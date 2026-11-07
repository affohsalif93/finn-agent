"""Rounding-aware break points (ARCHITECTURE §5.1, D15).

How far may the driver (EBITDA or equity) fall, everything else unchanged, before the **rounded**
covenant value breaks the threshold? The recipe is linear in each building block, so the
covenant value after a drop d is (n0 + a·d) / (m0 + b·d) (× 100 for percent): solve for the
rounding boundary algebraically, then verify and adjust to whole T€ with the real evaluator.
"""

from collections.abc import Callable
from decimal import ROUND_FLOOR, Decimal

from finn.calc.rounding import Operator, boundary, is_met

# Evaluates the covenant after a driver drop of d T€: rounded value, or None if not defined.
RoundedAt = Callable[[Decimal], Decimal | None]

_MAX_STEPS = 50


def break_point(
    n0: Decimal,
    a: Decimal,
    m0: Decimal,
    b: Decimal,
    *,
    scale: Decimal,
    operator: Operator,
    threshold: Decimal,
    decimals: int,
) -> Decimal | None:
    """Exact drop d* at which the raw value reaches the rounding boundary; None = never."""
    limit, _ = boundary(operator, threshold, decimals)
    denom = scale * a - limit * b
    if denom == 0:
        return None
    return (limit * m0 - scale * n0) / denom


def max_whole_drop(
    rounded_at: RoundedAt, operator: Operator, threshold: Decimal, estimate: Decimal
) -> Decimal | None:
    """Largest whole-T€ drop d with the covenant still met (negative: an increase is needed)."""

    def met(d: Decimal) -> bool:
        value = rounded_at(d)
        return value is not None and is_met(value, operator, threshold)

    d = estimate.to_integral_value(rounding=ROUND_FLOOR)
    for _ in range(_MAX_STEPS):  # the algebra is exact; this only absorbs the boundary
        if not met(d):
            d -= 1
        elif met(d + 1):
            d += 1
        else:
            return d
    return None
