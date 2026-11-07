"""Contract profile schema v2 (ARCHITECTURE §3). Reviewed JSON, one file per contract.

Amounts are exact decimals (JSON strings): contract amounts in EUR, plan values in T€.
The model validates types and the profile-wide invariants; the recipe structure (refs resolve,
no cycles, conditions exist) is checked per covenant by `finn.calc.recipe`, so one bad recipe
makes only that covenant `manual_review` instead of rejecting the whole contract.

Validation errors that the store maps to an error code carry the code as a message prefix
("PROFILE_INVARIANT: …", "PROFILE_INVALID_RECIPE: …").
"""

import datetime as dt
from collections.abc import Iterator
from decimal import Decimal
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from finn.models.plan import FactKey, ItemKey


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


# ── Document and parties ─────────────────────────────────────────────────────


class Document(_Frozen):
    file: str  # file name in the contracts folder (material/)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    pages: int = Field(gt=0)
    date: dt.date | None = None


class Parties(_Frozen):
    lender: str
    lender_members: list[str] = Field(default_factory=list)  # syndicates: every lender
    borrower: str


class AccountingBasis(_Frozen):
    standard: Literal["HGB"] = "HGB"
    entity: Literal["single", "group"] = "single"
    frozen_methods_source_id: str | None = None


class Source(_Frozen):
    """Verbatim contract text (whitespace normalised only) on a stated page."""

    id: str
    page: int = Field(gt=0)
    section: str  # e.g. "§ 14 Abs. 1 lit. b", "Anlage 3 (4)"
    quote: str = Field(min_length=10)


# ── Loans ────────────────────────────────────────────────────────────────────


class LoanMatch(_Frozen):
    loan_number: str | None = None  # matched first
    label_patterns: list[str] = Field(default_factory=list)  # case-insensitive substrings


class Tranche(_Frozen):
    tranche_id: str
    amount_eur: Decimal
    source_id: str | None = None


class RepaymentEntry(_Frozen):
    date: dt.date
    amount_eur: Decimal
    tranche: str | None = None


class Loan(_Frozen):
    loan_id: str
    match: LoanMatch
    tranches: list[Tranche] = Field(default_factory=list)
    repayment_schedule: list[RepaymentEntry] = Field(default_factory=list)
    source_id: str | None = None


# ── Conditions, definitions, components ──────────────────────────────────────


class Condition(_Frozen):
    """Contract-defined condition, decided by a plan fact (e.g. qualified subordination)."""

    label_de: str  # e.g. "qualifizierter Rangrücktritt des Gesellschafterdarlehens"
    criteria: list[str] = Field(min_length=1)
    plan_fact: FactKey
    source_id: str
    more_source_ids: list[str] = Field(default_factory=list)  # clause continued on the next page


IfMissing = Literal["partial", "assume_zero_warn", "assume_zero_info"]
LoanFilter = Literal[
    "exclude_special_repayments", "exclude_refinancing", "exclude_bullet_if_refinanced"
]


class Component(_Frozen):
    """One weighted term: a building block (`item`) or a definition of this contract (`ref`)."""

    item: ItemKey | None = None
    ref: str | None = None
    weight: Decimal = Decimal(1)
    exclude_if: str | None = None  # condition id
    only_if: str | None = None  # condition id
    if_missing: IfMissing = "partial"
    loan_filters: list[LoanFilter] = Field(default_factory=list)
    source_id: str | None = None

    @model_validator(mode="after")
    def _shape(self) -> Self:
        if (self.item is None) == (self.ref is None):
            raise ValueError("PROFILE_INVALID_RECIPE: component needs exactly one of item / ref")
        if self.exclude_if and self.only_if:
            raise ValueError("PROFILE_INVALID_RECIPE: component has both exclude_if and only_if")
        if self.loan_filters and self.item is not ItemKey.SCHEDULED_REPAYMENTS_TOTAL:
            raise ValueError(
                "PROFILE_INVALID_RECIPE: loan_filters only apply to scheduled_repayments_total"
            )
        if not self.weight.is_finite() or self.weight == 0:
            raise ValueError("PROFILE_INVALID_RECIPE: weight must be a non-zero decimal")
        return self

    @property
    def key(self) -> str:
        return self.item.value if self.item is not None else f"ref:{self.ref}"


class TextNote(_Frozen):
    text: str
    source_id: str | None = None


class Unmapped(_Frozen):
    """A part of a definition the recipe cannot express.

    `effect = "assumption"`: the plan cannot show it either (e.g. V4 (e) "sonstige Geschäfte mit
    der wirtschaftlichen Wirkung einer Kreditaufnahme"); the covenant is computed and the
    `handling` text is reported as an assumption. `manual_review`: the covenant is not computed.
    """

    text: str
    handling: str
    effect: Literal["assumption", "manual_review"] = "manual_review"
    source_id: str | None = None


class Definition(_Frozen):
    term: str | None = None  # the contract's defined term, e.g. "Finanzverbindlichkeiten"
    components: list[Component] = Field(min_length=1)
    exclusions: list[TextNote] = Field(default_factory=list)
    clarifications: list[TextNote] = Field(default_factory=list)
    unmapped_components: list[Unmapped] = Field(default_factory=list)
    source_id: str | None = None


# ── Covenants ────────────────────────────────────────────────────────────────


MetricType = Literal[
    "equity_ratio", "economic_equity_ratio", "net_debt_to_ebitda", "dscr", "other"
]  # fmt: skip


class Threshold(_Frozen):
    from_year: int
    value: Decimal  # percent as "27.5", ratio as "1.30", amount in EUR


class Rounding(_Frozen):
    decimals: int | None = Field(default=None, ge=0, le=6)  # None: contract names no rounding
    method: Literal["half_up"] = "half_up"
    source_id: str | None = None


class Test(_Frozen):
    frequency: Literal["annual"] = "annual"
    date: str = Field(pattern=r"^\d{2}-\d{2}$")  # "12-31"
    basis: Literal["annual_accounts"] = "annual_accounts"
    first_test: dt.date | None = None
    source_id: str | None = None


class PlanAction(_Frozen):
    type: Literal["talks", "talks_on_request", "notify_immediately", "present_measures"]
    by: str | None = None  # "parties", "borrower", "majority_lenders"
    within_days: int | None = None
    source_id: str | None = None


class PlanCheck(_Frozen):
    applies: bool = True
    consequence: Literal["early_warning_only", "measures_required"]
    plan_basis: list[str] = Field(default_factory=list)  # "planbilanz", "erfolgsplan", …
    plan_status_required: Literal["draft", "approved"] | None = None
    deadline: str | None = None  # "MM-DD" of the year before the plan year
    actions: list[PlanAction] = Field(default_factory=list)
    source_id: str | None = None  # the plan-shortfall clause
    duty_source_id: str | None = None  # the duty to deliver the plan (basis, status, deadline)


class BreachConsequence(_Frozen):
    type: Literal["termination", "event_of_default", "collateral_then_termination"]
    window_days: int | None = None
    source_id: str | None = None
    more_source_ids: list[str] = Field(default_factory=list)


class Cure(_Frozen):
    allowed: bool
    mechanism: Literal["add_to_numerator"] | None = None
    window_days: int | None = None
    max_count: int | None = None
    consecutive_allowed: bool | None = None
    source_id: str | None = None


class Covenant(_Frozen):
    covenant_id: str
    metric_type: MetricType
    name_in_contract: str
    numerator: list[Component] = Field(min_length=1)
    denominator: list[Component] = Field(default_factory=list)  # empty only for unit "amount"
    operator: Literal[">=", "<="]
    thresholds: list[Threshold] = Field(min_length=1)
    unit: Literal["percent", "ratio", "amount"]
    rounding: Rounding
    test: Test
    plan_check: PlanCheck
    breach_consequence: BreachConsequence
    cure: Cure | None = None  # None: the contract says nothing about a cure
    source_id: str
    unmapped_components: list[Unmapped] = Field(default_factory=list)

    @model_validator(mode="after")
    def _ratio_has_denominator(self) -> Self:
        if self.unit != "amount" and not self.denominator:
            raise ValueError("PROFILE_INVALID_RECIPE: ratio / percent covenant needs a denominator")
        return self

    def threshold_for(self, year: int) -> Decimal | None:
        valid = [t for t in self.thresholds if t.from_year <= year]
        return max(valid, key=lambda t: t.from_year).value if valid else None


class PricingStep(_Frozen):
    upper: Decimal | None  # None: no upper bound (last step)
    upper_inclusive: bool = True
    margin_pct: Decimal


class PricingGrid(_Frozen):
    metric_ref: str  # covenant_id whose value sets the step
    is_covenant: Literal[False] = False
    initial_margin_pct: Decimal
    effective_from: str
    steps: list[PricingStep] = Field(min_length=1)
    source_id: str
    more_source_ids: list[str] = Field(default_factory=list)

    def step_for(self, value: Decimal) -> PricingStep:
        for step in self.steps:
            if step.upper is None:
                return step
            if value < step.upper or (step.upper_inclusive and value == step.upper):
                return step
        return self.steps[-1]


class Example(_Frozen):
    """A sample calculation printed in the contract; inputs keyed by item or definition id."""

    covenant_id: str
    inputs: dict[str, Decimal]
    expected: Decimal
    source_id: str


class DismissedMention(_Frozen):
    source_id: str
    category: Literal[
        "historical_figure", "general_undertaking", "pricing", "collateral_right",
        "drawdown_condition", "cross_default", "negative_pledge", "other",
    ]  # fmt: skip
    reason: str


class Review(_Frozen):
    status: Literal["draft", "reviewed"]
    reviewed_by: str | None = None
    reviewed_at: dt.date | None = None
    notes: str | None = None


class Extraction(_Frozen):
    model: str
    prompt_version: str
    extracted_at: str
    source_sha256: str


class NoCovenantReason(_Frozen):
    text: str
    source_id: str


# ── Profile ──────────────────────────────────────────────────────────────────


class ContractProfile(_Frozen):
    contract_id: str
    schema_version: Literal[2] = 2
    name_de: str  # short German name for the reply, e.g. "Konsortialkredit"
    document: Document
    parties: Parties
    amount_eur: Decimal
    accounting_basis: AccountingBasis = Field(default_factory=AccountingBasis)
    has_financial_covenants: bool
    no_covenant_reason: NoCovenantReason | None = None
    sources: list[Source] = Field(min_length=1)
    loans: list[Loan] = Field(default_factory=list)
    conditions: dict[str, Condition] = Field(default_factory=dict)
    definitions: dict[str, Definition] = Field(default_factory=dict)
    covenants: list[Covenant] = Field(default_factory=list)
    pricing_grid: PricingGrid | None = None
    examples: list[Example] = Field(default_factory=list)
    dismissed_mentions: list[DismissedMention] = Field(default_factory=list)
    label_hints: dict[ItemKey, list[str]] = Field(default_factory=dict)
    review: Review
    extraction: Extraction | None = None

    @model_validator(mode="after")
    def _invariants(self) -> Self:
        if self.has_financial_covenants != bool(self.covenants):
            raise ValueError(
                "PROFILE_INVARIANT: has_financial_covenants does not match covenants[]"
            )
        if not self.has_financial_covenants and self.no_covenant_reason is None:
            raise ValueError("PROFILE_INVARIANT: no_covenant_reason (with source_id) required")
        ids = [c.covenant_id for c in self.covenants]
        if len(ids) != len(set(ids)):
            raise ValueError("PROFILE_INVARIANT: duplicate covenant_id")
        source_ids = [s.id for s in self.sources]
        if len(source_ids) != len(set(source_ids)):
            raise ValueError("PROFILE_INVARIANT: duplicate source id")
        unknown = sorted({sid for sid in _source_refs(self) if sid not in set(source_ids)})
        if unknown:
            raise ValueError(f"PROFILE_INVARIANT: unknown source_id {', '.join(unknown)}")
        if self.pricing_grid and self.pricing_grid.metric_ref not in ids:
            raise ValueError("PROFILE_INVARIANT: pricing_grid.metric_ref is not a covenant")
        bad_examples = [e.covenant_id for e in self.examples if e.covenant_id not in ids]
        if bad_examples:
            raise ValueError(f"PROFILE_INVARIANT: example for unknown covenant {bad_examples}")
        return self

    def source(self, source_id: str | None) -> Source | None:
        return next((s for s in self.sources if s.id == source_id), None)

    def covenant(self, covenant_id: str) -> Covenant | None:
        return next((c for c in self.covenants if c.covenant_id == covenant_id), None)


def _source_refs(model: BaseModel) -> Iterator[str]:
    """Every `*source_id` / `*source_ids` value anywhere in the model tree."""
    for name in type(model).model_fields:
        value = getattr(model, name)
        if name.endswith("source_id") and isinstance(value, str):
            yield value
        elif name.endswith("source_ids") and isinstance(value, list):
            yield from (v for v in value if isinstance(v, str))
        else:
            yield from _walk(value)


def _walk(value: object) -> Iterator[str]:
    if isinstance(value, BaseModel):
        yield from _source_refs(value)
    elif isinstance(value, list | tuple):
        for v in value:
            yield from _walk(v)
    elif isinstance(value, dict):
        for v in value.values():
            yield from _walk(v)
