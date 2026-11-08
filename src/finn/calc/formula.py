"""`formula_de` from the component trace: what is shown = what was computed. A definition ref is
expanded when it has at most `EXPAND_MAX` non-zero terms, else it stays a named sub-total."""

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

from finn.models.result import Trace, Used
from finn.numbers import number_de

EXPAND_MAX = 3


@dataclass(frozen=True)
class Term:
    weight: Decimal
    value: Decimal


def _leaves(trace: Used, weight: Decimal) -> list[Term]:
    """A ref expands into its used parts (recursively); anything else is one term."""
    if trace.component.startswith("ref:") and trace.parts:
        return _parts(trace.parts, weight * trace.weight)
    return [Term(weight * trace.weight, trace.value)]


def _parts(parts: Sequence[Trace], weight: Decimal) -> list[Term]:
    out: list[Term] = []
    for part in parts:
        if isinstance(part, Used):
            out.extend(_leaves(part, weight))
    return out


def side_terms(traces: Sequence[Trace]) -> tuple[list[Term], list[Used]]:
    """Terms of one side, plus the refs kept collapsed (for sub-formulas)."""
    terms: list[Term] = []
    collapsed: list[Used] = []
    for t in traces:
        if not isinstance(t, Used):
            continue
        if t.component.startswith("ref:"):
            leaves = [x for x in _leaves(t, Decimal(1)) if x.value != 0]
            if len(leaves) <= EXPAND_MAX:
                terms.extend(leaves)
                continue
            collapsed.append(t)
        terms.append(Term(t.weight, t.value))
    return terms, collapsed


def render_terms(terms: Sequence[Term]) -> str:
    if not terms:
        return "0"
    out: list[str] = []
    for i, term in enumerate(terms):
        negative = term.weight < 0
        factor = abs(term.weight)
        value = number_de(term.value) if term.value >= 0 else f"({number_de(term.value)})"
        body = value if factor == 1 else f"{number_de(factor)} × {value}"
        if i == 0:
            out.append(f"−{body}" if negative else body)
        else:
            out.append(f"{'−' if negative else '+'} {body}")
    return " ".join(out)


def _wrapped(terms: Sequence[Term]) -> str:
    text = render_terms(terms)
    return f"({text})" if len(terms) > 1 else text


def formula_de(numerator: Sequence[Trace], denominator: Sequence[Trace]) -> tuple[str, list[str]]:
    """("(148.200 − 12.600) / 40.200", ["EBITDA = 10.700 + … = 40.200"])."""
    num_terms, num_collapsed = side_terms(numerator)
    den_terms, den_collapsed = side_terms(denominator)
    subs = [sub_formula(t) for t in (*num_collapsed, *den_collapsed)]
    return f"{_wrapped(num_terms)} / {_wrapped(den_terms)}", subs


def sub_formula(trace: Used) -> str:
    leaves = [x for x in _parts(trace.parts, Decimal(1)) if x.value != 0]
    return f"{trace.label_de} = {render_terms(leaves)} = {number_de(trace.value)}"
