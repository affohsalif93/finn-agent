"""Contract self-test: run each profile's printed sample calculations (`examples`) through the
evaluator (FAILURES.md PROFILE_EXAMPLE_MISMATCH; V3 Anlage 3 (8): 1.54)."""

from decimal import Decimal

from finn.calc import rounding
from finn.calc.evaluator import Inputs, evaluate
from finn.errors import ErrorCode, Finding, finding
from finn.models.contract import ContractProfile, Example
from finn.models.plan import ItemKey, PlanValue
from finn.numbers import format_de
from finn.plan.labels import label_de


def example_value(profile: ContractProfile, example: Example) -> Decimal | None:
    """Rounded covenant value for the example inputs (None: not computable)."""
    cov = profile.covenant(example.covenant_id)
    if cov is None:
        return None
    items = {i.value: i for i in ItemKey}
    values = {
        items[k]: PlanValue(item=items[k], value=v, ref=None, label=label_de(items[k]),
                            original_unit="T€")
        for k, v in example.inputs.items()
        if k in items
    }  # fmt: skip
    definitions = {k: v for k, v in example.inputs.items() if k not in items}
    ev = evaluate(profile, cov, Inputs(values=values, definition_values=definitions))
    raw = ev.raw(cov.unit)
    if raw is None:
        return None
    return rounding.round_half_up(raw, rounding.effective_decimals(cov.rounding.decimals))


def check_examples(profile: ContractProfile) -> list[Finding]:
    out: list[Finding] = []
    for example in profile.examples:
        actual = example_value(profile, example)
        if actual is not None and actual == example.expected:
            continue
        shown = "nicht berechenbar" if actual is None else format_de(actual, 2)
        out.append(
            finding(
                ErrorCode.PROFILE_EXAMPLE_MISMATCH,
                contract=profile.contract_id,
                covenant=example.covenant_id,
                expected=format_de(example.expected, 2),
                actual=shown,
                affects=[example.covenant_id],
            )
        )
    return out
