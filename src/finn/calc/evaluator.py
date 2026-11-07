"""Recipe evaluator (ARCHITECTURE §5.1, D5, D23): weighted sums, one division, nothing else.

Pure: profile + building blocks + plan facts in, a full component trace out. `ref` components
resolve to the contract's **own** definitions (recursively); conditions are decided by plan
facts; `if_missing` and `loan_filters` apply per component. Recipes are assumed structurally
valid (`finn.calc.recipe.recipe_issues` is empty for the covenant).
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Literal

from finn.models.contract import Component, ContractProfile, Covenant, IfMissing, Unmapped
from finn.models.plan import (
    CellRef,
    FactKey,
    ItemKey,
    LoanFactKey,
    LoanLine,
    Plan,
    PlanFact,
    PlanValue,
)
from finn.models.result import AssumedZero, Excluded, Trace, Used
from finn.plan.labels import label_de

ZERO = Decimal(0)


@dataclass(frozen=True)
class Inputs:
    """What the evaluator reads. Overrides serve examples, alternatives and sensitivity."""

    values: Mapping[ItemKey, PlanValue]
    facts: Mapping[FactKey, PlanFact] = field(default_factory=dict)
    loans: Sequence[LoanLine] = ()
    definition_values: Mapping[str, Decimal] = field(default_factory=dict)  # examples
    fact_overrides: Mapping[str, bool] = field(default_factory=dict)  # FactKey / loan fact key
    adjustments: Mapping[ItemKey, Decimal] = field(default_factory=dict)  # added to a block

    @classmethod
    def from_plan(cls, plan: Plan) -> "Inputs":
        return cls(values=plan.values, facts=plan.facts, loans=plan.loans)


def loan_fact_key(loan: LoanLine, key: LoanFactKey) -> str:
    return f"loan:{loan.label}:{key.value}"


@dataclass(frozen=True)
class ConditionUse:
    condition_id: str
    fact_key: FactKey
    fact: PlanFact | None
    value: bool  # value used (after overrides)
    mode: Literal["exclude_if", "only_if"]
    component_label: str

    @property
    def ambiguous(self) -> bool:
        return self.fact is None or self.fact.ambiguous


@dataclass
class Side:
    total: Decimal
    traces: list[Trace]


@dataclass
class Evaluation:
    numerator: Side
    denominator: Side
    missing: list[ItemKey] = field(default_factory=list)  # if_missing = partial, not in plan
    assumed: list[tuple[AssumedZero, IfMissing]] = field(default_factory=list)
    conditions: list[ConditionUse] = field(default_factory=list)
    uncertain_bullets: list[LoanLine] = field(default_factory=list)
    unmapped: list[Unmapped] = field(default_factory=list)
    definitions_used: list[str] = field(default_factory=list)

    def raw(self, unit: str) -> Decimal | None:
        """numerator / denominator (× 100 for percent); None if the denominator is ≤ 0."""
        if self.missing:
            return None
        if unit == "amount":  # absolute covenant (e.g. equity >= EUR 80 M), T€
            return self.numerator.total
        if self.denominator.total <= 0:
            return None
        ratio = self.numerator.total / self.denominator.total
        return ratio * 100 if unit == "percent" else ratio


def evaluate(profile: ContractProfile, cov: Covenant, inputs: Inputs) -> Evaluation:
    ev = _Evaluator(profile, inputs)
    num = ev.side(cov.numerator)
    den = ev.side(cov.denominator)
    out = ev.out
    out.numerator, out.denominator = num, den
    out.unmapped = [*out.unmapped, *cov.unmapped_components]
    return out


class _Evaluator:
    def __init__(self, profile: ContractProfile, inputs: Inputs) -> None:
        self.profile = profile
        self.inputs = inputs
        self.out = Evaluation(numerator=Side(ZERO, []), denominator=Side(ZERO, []))

    def side(self, components: Sequence[Component]) -> Side:
        total, traces = ZERO, []
        for c in components:
            trace = self.component(c)
            if isinstance(trace, Used):
                total += trace.contribution
            traces.append(trace)
        return Side(total, traces)

    # ── one component ────────────────────────────────────────────────────────

    def component(self, c: Component) -> Trace:
        label = self.label(c)
        excluded = self.condition(c, label)
        if excluded is not None:
            return excluded
        if c.ref is not None:
            return self.ref(c, label)
        assert c.item is not None
        if c.loan_filters and self.inputs.loans:
            return self.loan_repayments(c, label)
        pv = self.inputs.values.get(c.item)
        if pv is None:
            if c.if_missing == "partial":
                self.out.missing.append(c.item)
                return AssumedZero(
                    component=c.key, label_de=label, reason="fehlt im Plan", source_id=c.source_id
                )
            assumed = AssumedZero(
                component=c.key,
                label_de=label,
                reason="im Plan nicht ausgewiesen, mit 0 angesetzt",
                source_id=c.source_id,
            )
            self.out.assumed.append((assumed, c.if_missing))
            return assumed
        value = pv.value + self.inputs.adjustments.get(c.item, ZERO)
        trace = self.used(c, label, value, pv, [pv.ref] if pv.ref else list(pv.derived_from))
        if c.loan_filters:  # aggregate fallback (no loan lines, e.g. contract examples)
            parts = self.special_repayments_excluded(c)
            trace = trace.model_copy(update={"parts": parts})
        return trace

    def used(
        self,
        c: Component,
        label: str,
        value: Decimal,
        pv: PlanValue | None,
        refs: list[CellRef],
        parts: list[Trace] | None = None,
    ) -> Used:
        return Used(
            component=c.key,
            label_de=label,
            value=value,
            plan_value=pv,
            weight=c.weight,
            contribution=c.weight * value,
            source_id=c.source_id,
            refs=refs,
            parts=parts or [],
        )

    def label(self, c: Component) -> str:
        if c.ref is not None:
            definition = self.profile.definitions.get(c.ref)
            return (definition.term if definition and definition.term else None) or c.ref
        assert c.item is not None
        pv = self.inputs.values.get(c.item)
        return pv.label if pv is not None else label_de(c.item)

    def condition(self, c: Component, label: str) -> Excluded | None:
        cond_id = c.exclude_if or c.only_if
        if cond_id is None:
            return None
        cond = self.profile.conditions[cond_id]
        fact = self.inputs.facts.get(cond.plan_fact)
        value = self.inputs.fact_overrides.get(
            cond.plan_fact.value, fact.value if fact is not None else False
        )
        mode: Literal["exclude_if", "only_if"] = "exclude_if" if c.exclude_if else "only_if"
        short = label_de(c.item) if c.item is not None else label  # "Gesellschafterdarlehen"
        self.out.conditions.append(
            ConditionUse(cond_id, cond.plan_fact, fact, value, mode, component_label=short)
        )
        if mode == "exclude_if" and value:
            reason = f"nicht enthalten: {cond.label_de}"
        elif mode == "only_if" and not value:
            reason = f"nicht abgezogen: {cond.label_de} nicht belegt"
        else:
            return None
        pv = self.inputs.values.get(c.item) if c.item is not None else None
        return Excluded(
            component=c.key,
            label_de=label,
            reason=reason,
            value=pv.value if pv is not None else None,
            evidence=fact,
            refs=[pv.ref] if pv is not None and pv.ref else [],
            source_id=cond.source_id,
        )

    def ref(self, c: Component, label: str) -> Used:
        assert c.ref is not None
        if c.ref in self.inputs.definition_values:
            return self.used(c, label, self.inputs.definition_values[c.ref], None, [])
        definition = self.profile.definitions[c.ref]
        self.out.definitions_used.append(c.ref)
        self.out.unmapped.extend(definition.unmapped_components)
        sub = self.side(definition.components)
        refs = [r for t in sub.traces if isinstance(t, Used) for r in t.refs]
        source = c.source_id or definition.source_id
        used = self.used(c, label, sub.total, None, refs, parts=sub.traces)
        return used.model_copy(update={"source_id": source})

    # ── loan schedule (scheduled repayments with contract filters) ───────────

    def loan_fact(self, loan: LoanLine, key: LoanFactKey) -> tuple[bool | None, bool]:
        """(value, ambiguous); value None = no evidence."""
        override = self.inputs.fact_overrides.get(loan_fact_key(loan, key))
        if override is not None:
            return override, False
        fact = loan.facts.get(key)
        if fact is None:
            return None, False
        return fact.value, fact.ambiguous

    def loan_repayments(self, c: Component, label: str) -> Used:
        parts: list[Trace] = []
        total = ZERO
        refs: list[CellRef] = []
        for loan in self.inputs.loans:
            name = f"loan:{loan.label}"
            ref = loan.refs.get("scheduled_repayment")
            refinanced, refi_unclear = self.loan_fact(loan, LoanFactKey.REFINANCED)
            bullet, _ = self.loan_fact(loan, LoanFactKey.BULLET_IN_PLAN_YEAR)
            if refinanced and (
                "exclude_refinancing" in c.loan_filters
                or (bullet and "exclude_bullet_if_refinanced" in c.loan_filters)
            ):
                reason = (
                    "refinanzierte Endfälligkeit, nicht einzubeziehen"
                    if bullet
                    else "Rückzahlung aus Refinanzierung, nicht einzubeziehen"
                )
                parts.append(
                    Excluded(
                        component=name,
                        label_de=loan.label,
                        reason=reason,
                        value=loan.scheduled_repayment,
                        refs=[ref] if ref else [],
                        source_id=c.source_id,
                    )
                )
                continue
            if (
                "exclude_bullet_if_refinanced" in c.loan_filters
                and bullet
                and (refinanced is None or refi_unclear)
            ):
                self.out.uncertain_bullets.append(loan)
            total += loan.scheduled_repayment
            if ref:
                refs.append(ref)
            parts.append(
                Used(
                    component=name,
                    label_de=loan.label,
                    value=loan.scheduled_repayment,
                    weight=Decimal(1),
                    contribution=loan.scheduled_repayment,
                    refs=[ref] if ref else [],
                    source_id=c.source_id,
                )
            )
        parts.extend(self.special_repayments_excluded(c))
        adjustment = self.inputs.adjustments.get(ItemKey.SCHEDULED_REPAYMENTS_TOTAL, ZERO)
        return self.used(c, label, total + adjustment, None, refs, parts=parts)

    def special_repayments_excluded(self, c: Component) -> list[Trace]:
        if "exclude_special_repayments" not in c.loan_filters:
            return []
        reason = "freiwillige Sondertilgungen, nicht einzubeziehen"
        if self.inputs.loans:
            out: list[Trace] = []
            for loan in self.inputs.loans:
                if loan.special_repayment > 0:
                    ref = loan.refs.get("special_repayment")
                    out.append(
                        Excluded(
                            component=f"special:{loan.label}",
                            label_de=loan.label,
                            reason=reason,
                            value=loan.special_repayment,
                            refs=[ref] if ref else [],
                            source_id=c.source_id,
                        )
                    )
            return out
        key = ItemKey.SPECIAL_REPAYMENTS_TOTAL
        pv = self.inputs.values.get(key)
        if pv is None or pv.value == 0:
            return []
        return [
            Excluded(
                component=key.value,
                label_de=pv.label,
                reason=reason,
                value=pv.value,
                refs=[pv.ref] if pv.ref else list(pv.derived_from),
                source_id=c.source_id,
            )
        ]
