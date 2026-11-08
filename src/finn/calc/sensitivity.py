"""Rounding-aware break points (D15): how far may EBITDA or equity fall before the rounded
covenant value breaks? Solved algebraically ((n0 + a·d) / (m0 + b·d) is linear per block), then
verified in whole T€ with the real evaluator."""

from collections.abc import Callable, Sequence
from dataclasses import replace
from decimal import ROUND_FLOOR, Decimal
from typing import Literal

from finn.calc import rounding
from finn.calc.evaluator import Evaluation, Inputs, evaluate_rounded
from finn.calc.recipe import Dependencies
from finn.calc.rounding import Operator, boundary, is_met
from finn.models.contract import ContractProfile, Covenant
from finn.models.plan import ItemKey, Plan
from finn.models.result import Sensitivity, Trace, Used
from finn.numbers import format_de, format_value, significant_decimals

# Evaluates the covenant after a driver drop of d T€: rounded value, or None if not defined.
RoundedAt = Callable[[Decimal], Decimal | None]

Driver = Literal["ebitda", "equity"]

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


def sensitivity(
    profile: ContractProfile,
    cov: Covenant,
    plan: Plan,
    deps: Dependencies,
    ev: Evaluation,
    inputs: Inputs,
    *,
    decimals: int,
    threshold: Decimal,
) -> Sensitivity | None:
    """How far EBITDA (or equity) may fall before the covenant breaks; None if not applicable."""
    driver = _driver(deps)
    if driver is None or cov.unit == "amount":
        return None
    name, item = driver
    if item not in plan.values:
        return None

    def at(d: Decimal) -> tuple[Decimal | None, Evaluation]:
        adjusted = {**inputs.adjustments, item: inputs.adjustments.get(item, Decimal(0)) - d}
        return evaluate_rounded(profile, cov, replace(inputs, adjustments=adjusted), decimals)

    _, ev1 = at(Decimal(1))
    scale = Decimal(100) if cov.unit == "percent" else Decimal(1)
    estimate = break_point(
        ev.numerator.total,
        ev1.numerator.total - ev.numerator.total,
        ev.denominator.total,
        ev1.denominator.total - ev.denominator.total,
        scale=scale,
        operator=cov.operator,
        threshold=threshold,
        decimals=decimals,
    )
    if estimate is None:
        return None
    drop = max_whole_drop(lambda d: at(d)[0], cov.operator, threshold, estimate)
    if drop is None:
        return None
    at_drop, _ = at(drop)
    after, _ = at(drop + 1)
    if at_drop is None or after is None:
        return None
    if name == "ebitda":
        ebitda = _find_ref([*ev.numerator.traces, *ev.denominator.traces], "ebitda")
        base = ebitda.value if ebitda is not None else plan.values[item].value
        label = "EBITDA"
    else:
        base = plan.values[item].value
        label = "Eigenkapital"
    pct = rounding.round_half_up(drop / base * 100, 1) if base else Decimal(0)
    return Sensitivity(
        driver=name,
        driver_label_de=label,
        base_t_eur=base,
        max_drop_t_eur=drop,
        max_drop_pct=pct,
        value_at_max_drop=at_drop,
        value_after_next_t_eur=after,
        text_de=_text(cov, label, base, drop, pct, after, decimals, threshold),
    )


def _driver(deps: Dependencies) -> tuple[Driver, ItemKey] | None:
    if "ebitda" in deps.definitions or ItemKey.NET_INCOME in deps.items:
        return "ebitda", ItemKey.NET_INCOME  # EBITDA falls 1:1 with net income
    if ItemKey.EQUITY in deps.items:
        return "equity", ItemKey.EQUITY
    return None


def _find_ref(traces: Sequence[Trace], ref: str) -> Used | None:
    for t in traces:
        if isinstance(t, Used):
            if t.component == f"ref:{ref}":
                return t
            hit = _find_ref(t.parts, ref)
            if hit is not None:
                return hit
    return None


def _text(
    cov: Covenant,
    label: str,
    base: Decimal,
    drop: Decimal,
    pct: Decimal,
    after: Decimal,
    decimals: int,
    threshold: Decimal,
) -> str:
    unit = cov.unit
    limit = format_value(threshold, unit, rounding.effective_decimals(cov.rounding.decimals))
    if cov.unit == "percent":
        limit = f"{format_de(threshold, max(1, significant_decimals(threshold)))} %"
    verb = "überschreitet" if cov.operator == "<=" else "unterschreitet"
    ceteris = " (übrige Werte unverändert, Bilanzsumme gleich)" if label == "Eigenkapital" else ""
    if drop < 0:
        return (
            f"Damit „{cov.name_in_contract}“ eingehalten wird, müsste das {label} um mindestens "
            f"{format_de(-drop, 0)} T€ ({format_de(-pct, 1)} %) steigen{ceteris}."
        )
    return (
        f"Das {label} ({format_de(base, 0)} T€) darf um bis zu {format_de(drop, 0)} T€ "
        f"({format_de(pct, 1)} %) sinken{ceteris}, bevor „{cov.name_in_contract}“ die Grenze "
        f"von {limit} {verb}; bei {format_de(drop + 1, 0)} T€ weniger: "
        f"{format_value(after, unit, decimals)}."
    )
