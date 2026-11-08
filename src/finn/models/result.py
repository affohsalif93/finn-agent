"""Calculation results. The component trace (`Used` / `Excluded` / `AssumedZero`) is the
calculation path shown in the reply; `formula_de` is generated from the same trace."""

import datetime as dt
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from finn.errors import Finding
from finn.models.contract import Source
from finn.models.plan import CellRef, PlanFact, PlanValue


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Used(_Frozen):
    """A component that entered the sum: weight × value = contribution."""

    type: Literal["used"] = "used"
    component: str  # item key, "ref:<definition>" or "loan:<label>"
    label_de: str
    value: Decimal  # T€ (or the example input)
    plan_value: PlanValue | None = None  # building block with its cell; None for refs / loans
    weight: Decimal
    contribution: Decimal
    source_id: str | None = None
    refs: list[CellRef] = Field(default_factory=list)  # cells behind the value
    parts: list["Trace"] = Field(default_factory=list)  # definition / loan breakdown


class Excluded(_Frozen):
    """A component left out by the contract: condition met / not met, or a loan filter."""

    type: Literal["excluded"] = "excluded"
    component: str
    label_de: str
    reason: str
    value: Decimal | None = None  # the plan value that was left out, if known
    evidence: PlanFact | None = None
    refs: list[CellRef] = Field(default_factory=list)
    source_id: str | None = None


class AssumedZero(_Frozen):
    """A component the plan does not show, set to 0 by the profile's `if_missing` policy."""

    type: Literal["assumed_zero"] = "assumed_zero"
    component: str
    label_de: str
    reason: str
    source_id: str | None = None


Trace = Annotated[Used | Excluded | AssumedZero, Field(discriminator="type")]
Used.model_rebuild()


class Components(_Frozen):
    numerator: list[Trace]
    denominator: list[Trace]


class Sensitivity(_Frozen):
    """Rounding-aware break point: the largest drop of `driver` (T€, whole T€) that still meets
    the covenant after contract rounding, everything else unchanged. Negative = an increase
    is needed to meet it."""

    driver: Literal["ebitda", "equity"]
    driver_label_de: str
    base_t_eur: Decimal
    max_drop_t_eur: Decimal
    max_drop_pct: Decimal  # of the base, 1 dp
    value_at_max_drop: Decimal  # rounded covenant value after the drop (still met)
    value_after_next_t_eur: Decimal  # one more T€ → breached
    text_de: str


class Alternative(_Frozen):
    """Result under the opposite assumption for an unclear plan fact (PLAN_FACT_AMBIGUOUS)."""

    assumption_de: str
    value_rounded: Decimal
    status: Literal["met", "breached", "not_computable"]


CovenantStatus = Literal["met", "breached", "not_computable", "manual_review"]


class CovenantResult(_Frozen):
    covenant_id: str
    contract_id: str
    contract_name_de: str
    name_de: str
    metric_type: str
    status: CovenantStatus
    value_raw: Decimal | None = None
    value_rounded: Decimal | None = None
    decimals: int | None = None
    threshold: Decimal | None = None
    operator: Literal[">=", "<="]
    unit: Literal["percent", "ratio", "amount"]
    headroom: Decimal | None = None
    headroom_unit: Literal["pp", "ratio", "T€"]
    headroom_relative: Decimal | None = None  # headroom / threshold
    tight: bool = False
    sensitivity: Sensitivity | None = None
    components: Components | None = None
    numerator_value: Decimal | None = None
    denominator_value: Decimal | None = None
    formula_de: str | None = None
    sub_formulas_de: list[str] = Field(default_factory=list)  # e.g. "EBITDA = 10.700 + …"
    alternatives: list[Alternative] = Field(default_factory=list)
    sources: list[Source] = Field(default_factory=list)
    reason: str | None = None  # why not computable / manual review
    findings: list[Finding] = Field(default_factory=list)


class ContractNote(_Frozen):
    contract_id: str
    kind: Literal["no_covenants", "pricing", "cure", "plan_check", "breach_consequence"]
    text_de: str
    source_id: str | None = None


class CheckedInput(_Frozen):
    file: str | None
    sha256: str
    company: str | None
    plan_year: int
    status: Literal["draft", "approved"]
    version_date: dt.date | None
    reporting_date: dt.date
    profile_hashes: dict[str, str]  # contract_id -> sha256 of the canonical profile JSON


class Report(_Frozen):
    header: CheckedInput | None  # None if the plan could not be read at all
    findings: list[Finding]  # plan + profile + contract–plan findings (affects set)
    results: list[CovenantResult]
    contract_notes: list[ContractNote]
    overall: Literal["all_met", "some_breached", "incomplete"]
    tightest: str | None  # covenant_id
