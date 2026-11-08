"""Plan data contracts. All amounts are `Decimal` in T€ after unit and sign normalisation."""

from datetime import date
from decimal import Decimal
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ItemKey(StrEnum):
    """Contract-neutral building blocks (§4.1)."""

    # GuV
    NET_INCOME = "net_income"
    INCOME_TAXES = "income_taxes"
    INTEREST_EXPENSE = "interest_expense"
    PENSION_UNWINDING = "pension_unwinding"
    INTEREST_INCOME = "interest_income"
    DEPRECIATION = "depreciation"
    EXTRAORDINARY_ITEMS = "extraordinary_items"
    # Bilanz
    TOTAL_ASSETS = "total_assets"
    TOTAL_EQUITY_LIABILITIES = "total_equity_liabilities"
    EQUITY = "equity"
    SONDERPOSTEN = "sonderposten"
    BANK_DEBT = "bank_debt"
    SHAREHOLDER_LOAN = "shareholder_loan"
    CASH = "cash"
    CALLED_UNPAID_CAPITAL = "called_unpaid_capital"
    BONDS = "bonds"
    FINANCE_LEASES = "finance_leases"
    RECOURSE_FACTORING = "recourse_factoring"
    # Finanzierung (aggregates over the loan schedule)
    SCHEDULED_REPAYMENTS_TOTAL = "scheduled_repayments_total"
    SPECIAL_REPAYMENTS_TOTAL = "special_repayments_total"
    TOTAL_BANK_DEBT = "total_bank_debt"
    INTEREST_ON_FINANCIAL_DEBT = "interest_on_financial_debt"


class FactKey(StrEnum):
    """Plan-wide facts decided by pattern rules on note texts."""

    QUALIFIED_SUBORDINATION = "qualified_subordination"
    CASH_FREELY_AVAILABLE = "cash_freely_available"


class LoanFactKey(StrEnum):
    """Per-loan facts (stored on `LoanLine.facts`)."""

    BULLET_IN_PLAN_YEAR = "bullet_in_plan_year"
    REFINANCED = "refinanced"


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class CellRef(_Frozen):
    sheet: str
    cell: str

    def __str__(self) -> str:
        return f"{self.sheet}!{self.cell}"


class PlanValue(_Frozen):
    item: ItemKey
    value: Decimal
    ref: CellRef | None  # None if derived (e.g. sums over loans)
    label: str
    original_unit: str  # unit as declared on the sheet, e.g. "T€", "€"
    derived_from: list[CellRef] = Field(default_factory=list)


class PlanFact(_Frozen):
    key: FactKey | LoanFactKey
    value: bool
    evidence: CellRef | None
    quote: str | None
    ambiguous: bool = False


LoanKind = Literal["bank", "shareholder", "other"]


class LoanLine(_Frozen):
    label: str
    loan_number: str | None
    kind: LoanKind
    opening: Decimal
    drawdown: Decimal
    scheduled_repayment: Decimal
    special_repayment: Decimal
    closing: Decimal
    interest: Decimal
    refs: dict[str, CellRef]  # field name -> cell
    facts: dict[LoanFactKey, PlanFact] = Field(default_factory=dict)


class Plan(_Frozen):
    company: str | None
    plan_year: int
    reporting_date: date
    status: Literal["draft", "approved"]
    version_date: date | None
    file_sha256: str
    values: dict[ItemKey, PlanValue]
    loans: list[LoanLine]
    facts: dict[FactKey, PlanFact]
