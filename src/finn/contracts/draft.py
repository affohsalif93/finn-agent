"""Extraction-specific draft schema (LLM structured output) and its conversion to ContractProfile.

Why not `ContractProfile` itself: OpenAI strict JSON schema requires every property to be
present (`required` = all, `additionalProperties: false`) and has no free-form maps. The profile
uses dicts (`conditions`, `definitions`, `label_hints`, `examples[].inputs`), defaults everywhere
and decimals. The draft therefore
- uses lists with an `id` / `key` field instead of dicts,
- has no defaults: every field is required, "not stated" is an explicit `null`,
- carries decimals as strings ("30.0", "0.5") that code parses,
- leaves out everything code knows better: contract id, file, sha256, page count, schema version,
  review and extraction metadata.
`to_profile` converts and validates (pydantic invariants + store-level checks happen later).
"""

import datetime as dt
from decimal import Decimal, InvalidOperation
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from finn.models.contract import (
    ContractProfile,
    DismissedMention,
    IfMissing,
    LoanFilter,
    MetricType,
    PlanAction,
)
from finn.models.plan import FactKey, ItemKey


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DSource(_Strict):
    id: str
    page: int
    section: str
    quote: str


class DParties(_Strict):
    lender: str
    lender_members: list[str]
    borrower: str


class DLoanMatch(_Strict):
    loan_number: str | None
    label_patterns: list[str]


class DTranche(_Strict):
    tranche_id: str
    amount_eur: str
    source_id: str | None


class DRepayment(_Strict):
    date: str  # YYYY-MM-DD
    amount_eur: str
    tranche: str | None


class DLoan(_Strict):
    loan_id: str
    match: DLoanMatch
    tranches: list[DTranche]
    repayment_schedule: list[DRepayment]
    source_id: str | None


class DCondition(_Strict):
    id: str
    label_de: str
    criteria: list[str]
    plan_fact: FactKey
    source_id: str
    more_source_ids: list[str]


class DComponent(_Strict):
    item: ItemKey | None
    ref: str | None
    weight: str
    exclude_if: str | None
    only_if: str | None
    if_missing: IfMissing
    loan_filters: list[LoanFilter]
    source_id: str | None


class DTextNote(_Strict):
    text: str
    source_id: str | None


class DUnmapped(_Strict):
    text: str
    handling: str
    effect: Literal["assumption", "manual_review"]
    source_id: str | None


class DDefinition(_Strict):
    id: str
    term: str | None
    components: list[DComponent]
    exclusions: list[DTextNote]
    clarifications: list[DTextNote]
    unmapped_components: list[DUnmapped]
    source_id: str | None


class DThreshold(_Strict):
    from_year: int
    value: str


class DRounding(_Strict):
    decimals: int | None
    source_id: str | None


class DTest(_Strict):
    date: str  # MM-DD
    first_test: str | None  # YYYY-MM-DD
    source_id: str | None


class DPlanAction(_Strict):
    type: Literal["talks", "talks_on_request", "notify_immediately", "present_measures"]
    by: str | None
    within_days: int | None
    source_id: str | None


class DPlanCheck(_Strict):
    applies: bool
    consequence: Literal["early_warning_only", "measures_required"]
    plan_basis: list[Literal["planbilanz", "erfolgsplan", "finanzplan", "darlehensspiegel"]]
    plan_status_required: Literal["approved"] | None
    deadline: str | None  # MM-DD
    actions: list[DPlanAction]
    source_id: str | None
    duty_source_id: str | None


class DBreach(_Strict):
    type: Literal["termination", "event_of_default", "collateral_then_termination"]
    window_days: int | None
    source_id: str | None
    more_source_ids: list[str]


class DCure(_Strict):
    allowed: bool
    mechanism: Literal["add_to_numerator"] | None
    window_days: int | None
    max_count: int | None
    consecutive_allowed: bool | None
    source_id: str | None


class DCovenant(_Strict):
    covenant_id: str
    metric_type: MetricType
    name_in_contract: str
    numerator: list[DComponent]
    denominator: list[DComponent]
    operator: Literal[">=", "<="]
    thresholds: list[DThreshold]
    unit: Literal["percent", "ratio", "amount"]
    rounding: DRounding
    test: DTest
    plan_check: DPlanCheck
    breach_consequence: DBreach
    cure: DCure | None
    source_id: str
    unmapped_components: list[DUnmapped]


class DPricingStep(_Strict):
    upper: str | None
    upper_inclusive: bool
    margin_pct: str


class DPricingGrid(_Strict):
    metric_ref: str
    initial_margin_pct: str
    effective_from: str
    steps: list[DPricingStep]
    source_id: str
    more_source_ids: list[str]


class DExampleInput(_Strict):
    key: str  # building block (ItemKey) or definition id
    value: str


class DExample(_Strict):
    covenant_id: str
    inputs: list[DExampleInput]
    expected: str
    source_id: str


DismissedCategory = Literal[
    "historical_figure", "general_undertaking", "pricing", "collateral_right",
    "drawdown_condition", "cross_default", "negative_pledge", "other",
]  # fmt: skip


class DDismissed(_Strict):
    source_id: str
    category: DismissedCategory
    reason: str


class DLabelHint(_Strict):
    item: ItemKey
    labels: list[str]


class DNoCovenantReason(_Strict):
    text: str
    source_id: str


class ProfileDraft(_Strict):
    """What the extraction LLM returns (see the module docstring)."""

    name_de: str
    contract_date: str | None  # YYYY-MM-DD
    parties: DParties
    amount_eur: str
    accounting_entity: Literal["single", "group"]
    frozen_methods_source_id: str | None
    has_financial_covenants: bool
    no_covenant_reason: DNoCovenantReason | None
    sources: list[DSource]
    loans: list[DLoan]
    conditions: list[DCondition]
    definitions: list[DDefinition]
    covenants: list[DCovenant]
    pricing_grid: DPricingGrid | None
    examples: list[DExample]
    dismissed_mentions: list[DDismissed]
    label_hints: list[DLabelHint]
    extraction_notes: list[str]  # uncertainties for the human reviewer


# ── Conversion ───────────────────────────────────────────────────────────────


class DraftError(ValueError):
    """The draft cannot be turned into a valid ContractProfile."""


def to_profile_data(
    draft: ProfileDraft,
    *,
    contract_id: str,
    file: str,
    sha256: str,
    pages: int,
    extraction: dict[str, str],
    notes: str | None = None,
) -> dict[str, Any]:
    """Plain JSON-able dict in ContractProfile shape (decimals as strings, like the gold files)."""
    d = draft
    return {
        "contract_id": contract_id,
        "schema_version": 2,
        "name_de": d.name_de,
        "document": {"file": file, "sha256": sha256, "pages": pages, "date": d.contract_date},
        "parties": d.parties.model_dump(),
        "amount_eur": _dec(d.amount_eur, "amount_eur"),
        "accounting_basis": {
            "standard": "HGB",
            "entity": d.accounting_entity,
            "frozen_methods_source_id": d.frozen_methods_source_id,
        },
        "has_financial_covenants": d.has_financial_covenants,
        "no_covenant_reason": d.no_covenant_reason.model_dump() if d.no_covenant_reason else None,
        "sources": [s.model_dump() for s in d.sources],
        "loans": [_loan(x) for x in d.loans],
        "conditions": {
            c.id: {
                "label_de": c.label_de,
                "criteria": c.criteria,
                "plan_fact": c.plan_fact.value,
                "source_id": c.source_id,
                "more_source_ids": c.more_source_ids,
            }
            for c in d.conditions
        },
        "definitions": {x.id: _definition(x) for x in d.definitions},
        "covenants": [_covenant(c) for c in d.covenants],
        "pricing_grid": _pricing(d.pricing_grid) if d.pricing_grid else None,
        "examples": [
            {
                "covenant_id": e.covenant_id,
                "inputs": {i.key: _dec(i.value, f"example {i.key}") for i in e.inputs},
                "expected": _dec(e.expected, "example expected"),
                "source_id": e.source_id,
            }
            for e in d.examples
        ],
        "dismissed_mentions": [
            DismissedMention.model_validate(m.model_dump()).model_dump(mode="json")
            for m in d.dismissed_mentions
        ],
        "label_hints": {h.item.value: h.labels for h in d.label_hints},
        "review": {"status": "draft", "reviewed_by": None, "reviewed_at": None, "notes": notes},
        "extraction": extraction,
    }


def to_profile(draft: ProfileDraft, **kwargs: Any) -> ContractProfile:
    return ContractProfile.model_validate(to_profile_data(draft, **kwargs))


def _dec(text: str, what: str) -> str:
    cleaned = text.strip().replace(" ", "")
    if "," in cleaned:  # tolerate German notation
        cleaned = cleaned.replace(".", "").replace(",", ".")
    try:
        value = Decimal(cleaned)
    except InvalidOperation as exc:
        raise DraftError(f"{what}: '{text}' ist keine Zahl") from exc
    if not value.is_finite():
        raise DraftError(f"{what}: '{text}' ist keine Zahl")
    return str(value)


def _date(text: str | None, what: str) -> str | None:
    if text is None:
        return None
    try:
        return dt.date.fromisoformat(text.strip()).isoformat()
    except ValueError as exc:
        raise DraftError(f"{what}: '{text}' ist kein Datum (YYYY-MM-DD)") from exc


def _component(c: DComponent) -> dict[str, Any]:
    return {
        "item": c.item.value if c.item else None,
        "ref": c.ref,
        "weight": _dec(c.weight, "weight"),
        "exclude_if": c.exclude_if,
        "only_if": c.only_if,
        "if_missing": c.if_missing,
        "loan_filters": list(c.loan_filters),
        "source_id": c.source_id,
    }


def _definition(x: DDefinition) -> dict[str, Any]:
    return {
        "term": x.term,
        "components": [_component(c) for c in x.components],
        "exclusions": [n.model_dump() for n in x.exclusions],
        "clarifications": [n.model_dump() for n in x.clarifications],
        "unmapped_components": [u.model_dump() for u in x.unmapped_components],
        "source_id": x.source_id,
    }


def _loan(x: DLoan) -> dict[str, Any]:
    return {
        "loan_id": x.loan_id,
        "match": x.match.model_dump(),
        "tranches": [
            {
                "tranche_id": t.tranche_id,
                "amount_eur": _dec(t.amount_eur, "tranche"),
                "source_id": t.source_id,
            }
            for t in x.tranches
        ],
        "repayment_schedule": [
            {
                "date": _date(r.date, "repayment date"),
                "amount_eur": _dec(r.amount_eur, "repayment"),
                "tranche": r.tranche,
            }
            for r in x.repayment_schedule
        ],
        "source_id": x.source_id,
    }


def _covenant(c: DCovenant) -> dict[str, Any]:
    pc = c.plan_check
    return {
        "covenant_id": c.covenant_id,
        "metric_type": c.metric_type,
        "name_in_contract": c.name_in_contract,
        "numerator": [_component(x) for x in c.numerator],
        "denominator": [_component(x) for x in c.denominator],
        "operator": c.operator,
        "thresholds": [
            {"from_year": t.from_year, "value": _dec(t.value, "threshold")} for t in c.thresholds
        ],
        "unit": c.unit,
        "rounding": {
            "decimals": c.rounding.decimals,
            "method": "half_up",
            "source_id": c.rounding.source_id,
        },
        "test": {
            "frequency": "annual",
            "date": c.test.date,
            "basis": "annual_accounts",
            "first_test": _date(c.test.first_test, "first_test"),
            "source_id": c.test.source_id,
        },
        "plan_check": {
            "applies": pc.applies,
            "consequence": pc.consequence,
            "plan_basis": list(pc.plan_basis),
            "plan_status_required": pc.plan_status_required,
            "deadline": pc.deadline,
            "actions": [PlanAction.model_validate(a.model_dump()).model_dump() for a in pc.actions],
            "source_id": pc.source_id,
            "duty_source_id": pc.duty_source_id,
        },
        "breach_consequence": c.breach_consequence.model_dump(),
        "cure": c.cure.model_dump() if c.cure else None,
        "source_id": c.source_id,
        "unmapped_components": [u.model_dump() for u in c.unmapped_components],
    }


def _pricing(g: DPricingGrid) -> dict[str, Any]:
    return {
        "metric_ref": g.metric_ref,
        "is_covenant": False,
        "initial_margin_pct": _dec(g.initial_margin_pct, "initial_margin_pct"),
        "effective_from": g.effective_from,
        "steps": [
            {
                "upper": _dec(s.upper, "upper") if s.upper is not None else None,
                "upper_inclusive": s.upper_inclusive,
                "margin_pct": _dec(s.margin_pct, "margin"),
            }
            for s in g.steps
        ],
        "source_id": g.source_id,
        "more_source_ids": g.more_source_ids,
    }


# ── Review pass (second LLM call) ────────────────────────────────────────────


class ReviewIssue(_Strict):
    kind: Literal[
        "missing_covenant", "not_a_covenant", "wrong_value", "missing_component",
        "wrong_component", "missing_exclusion", "modifying_clause", "missing_dismissed_mention",
        "wrong_quote", "other",
    ]  # fmt: skip
    severity: Literal["high", "medium", "low"]
    covenant_id: str | None
    page: int | None
    quote: str | None  # verbatim contract text the issue is about
    description: str
    suggested_fix: str


class ReviewResult(_Strict):
    issues: list[ReviewIssue]
    summary: str


# ── Inverse (tests, canned drafts) ───────────────────────────────────────────


def from_profile(profile: ContractProfile, notes: list[str] | None = None) -> ProfileDraft:
    """The draft an ideal extraction would return for `profile` (round trip: to_profile)."""
    data = profile.model_dump(mode="json")
    covenants = []
    for c in data["covenants"]:
        c["rounding"].pop("method")
        c["test"] = {k: c["test"][k] for k in ("date", "first_test", "source_id")}
        covenants.append(c)
    grid = data["pricing_grid"]
    if grid is not None:
        grid.pop("is_covenant")
    return ProfileDraft.model_validate(
        {
            "name_de": data["name_de"],
            "contract_date": data["document"]["date"],
            "parties": data["parties"],
            "amount_eur": data["amount_eur"],
            "accounting_entity": data["accounting_basis"]["entity"],
            "frozen_methods_source_id": data["accounting_basis"]["frozen_methods_source_id"],
            "has_financial_covenants": data["has_financial_covenants"],
            "no_covenant_reason": data["no_covenant_reason"],
            "sources": data["sources"],
            "loans": data["loans"],
            "conditions": [{"id": k, **v} for k, v in data["conditions"].items()],
            "definitions": [{"id": k, **v} for k, v in data["definitions"].items()],
            "covenants": covenants,
            "pricing_grid": grid,
            "examples": [
                {**e, "inputs": [{"key": k, "value": v} for k, v in e["inputs"].items()]}
                for e in data["examples"]
            ],
            "dismissed_mentions": data["dismissed_mentions"],
            "label_hints": [{"item": k, "labels": v} for k, v in data["label_hints"].items()],
            "extraction_notes": notes or [],
        }
    )
