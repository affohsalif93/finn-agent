"""Tie-outs and plausibility checks on a read plan → findings (D7, D22)."""

import re
from collections.abc import Collection, Mapping, Sequence
from decimal import Decimal

from finn.errors import ErrorCode, Finding, finding
from finn.models.plan import CellRef, ItemKey, LoanFactKey, Plan
from finn.numbers import format_de
from finn.plan.labels import (
    BLOCKS,
    FINANCING_ITEMS,
    Statement,
    label_de,
    normalise,
    years_in,
)

_STATEMENT_LABELS = {
    Statement.GUV: "Erfolgsplan (GuV)",
    Statement.BILANZ: "Planbilanz",
    Statement.FINANZIERUNG: "Darlehensspiegel (Tilgungen, Zinsen, Darlehensstände)",
}


def _t(value: Decimal) -> str:
    return format_de(value, 0)


def _refs(plan: Plan, *items: ItemKey) -> list[CellRef]:
    refs: list[CellRef] = []
    for item in items:
        pv = plan.values.get(item)
        if pv is not None:
            refs.extend([pv.ref] if pv.ref else pv.derived_from)
    return refs


def check_balance(plan: Plan, recomputed: Mapping[ItemKey, Decimal] | None = None) -> list[Finding]:
    """Assets = equity + liabilities, on totals recomputed from inputs where possible."""
    recomputed = recomputed or {}
    sides = []
    for item in (ItemKey.TOTAL_ASSETS, ItemKey.TOTAL_EQUITY_LIABILITIES):
        pv = plan.values.get(item)
        sides.append(recomputed.get(item, pv.value if pv else None))
    assets, liabilities = sides
    if assets is None or liabilities is None or assets == liabilities:
        return []
    return [
        finding(
            ErrorCode.PLAN_UNBALANCED, assets=_t(assets), liabilities=_t(liabilities),
            refs=_refs(plan, ItemKey.TOTAL_ASSETS, ItemKey.TOTAL_EQUITY_LIABILITIES),
            items=[ItemKey.TOTAL_ASSETS, ItemKey.TOTAL_EQUITY_LIABILITIES],
        )
    ]  # fmt: skip


def check_loans_vs_balance(plan: Plan) -> list[Finding]:
    """Loan schedule closing balances = Bilanz bank liabilities (and shareholder loan)."""
    out: list[Finding] = []
    schedule = plan.values.get(ItemKey.TOTAL_BANK_DEBT)
    balance = plan.values.get(ItemKey.BANK_DEBT)
    if schedule and balance and schedule.value != balance.value:
        out.append(
            finding(
                ErrorCode.PLAN_LOANS_MISMATCH, loans=_t(schedule.value), balance=_t(balance.value),
                label="Verbindlichkeiten gegenüber Kreditinstituten",
                refs=_refs(plan, ItemKey.BANK_DEBT, ItemKey.TOTAL_BANK_DEBT),
                items=[ItemKey.BANK_DEBT, ItemKey.TOTAL_BANK_DEBT],
            )
        )  # fmt: skip
    shareholder_loans = [ln for ln in plan.loans if ln.kind == "shareholder"]
    bilanz_sh = plan.values.get(ItemKey.SHAREHOLDER_LOAN)
    if shareholder_loans and bilanz_sh:
        total = sum((ln.closing for ln in shareholder_loans), Decimal(0))
        if total != bilanz_sh.value:
            out.append(
                finding(
                    ErrorCode.PLAN_LOANS_MISMATCH, loans=_t(total), balance=_t(bilanz_sh.value),
                    label="Gesellschafterdarlehen",
                    refs=[*_refs(plan, ItemKey.SHAREHOLDER_LOAN),
                          *[ln.refs["closing"] for ln in shareholder_loans
                            if "closing" in ln.refs]],
                    items=[ItemKey.SHAREHOLDER_LOAN],
                )
            )  # fmt: skip
    return out


def check_interest_tieout(plan: Plan) -> list[Finding]:
    """Interest per loan schedule + unwinding = GuV interest expense (WARN)."""
    loans = plan.values.get(ItemKey.INTEREST_ON_FINANCIAL_DEBT)
    guv = plan.values.get(ItemKey.INTEREST_EXPENSE)
    if loans is None or guv is None:
        return []
    unwinding = plan.values.get(ItemKey.PENSION_UNWINDING)
    unwinding_value = unwinding.value if unwinding else Decimal(0)
    if loans.value + unwinding_value == guv.value:
        return []
    return [
        finding(
            ErrorCode.PLAN_INTEREST_TIEOUT, loans=_t(loans.value), unwinding=_t(unwinding_value),
            guv=_t(guv.value), refs=_refs(plan, ItemKey.INTEREST_EXPENSE),
            items=[ItemKey.INTEREST_EXPENSE, ItemKey.INTEREST_ON_FINANCIAL_DEBT,
                   ItemKey.PENSION_UNWINDING],
        )
    ]  # fmt: skip


def check_opening_balances(plan: Plan, previous: Mapping[ItemKey, Decimal]) -> list[Finding]:
    """Opening balances in the loan schedule = previous year's Bilanz closing balances (WARN)."""
    out: list[Finding] = []
    groups = (
        (ItemKey.BANK_DEBT, "bank", "Kreditinstitute"),
        (ItemKey.SHAREHOLDER_LOAN, "shareholder", "Gesellschafterdarlehen"),
    )
    for item, kind, label in groups:
        loans = [ln for ln in plan.loans if ln.kind == kind]
        if item not in previous or not loans:
            continue
        opening = sum((ln.opening for ln in loans), Decimal(0))
        if opening != previous[item]:
            out.append(
                finding(
                    ErrorCode.PLAN_OPENING_BALANCE, label=label, opening=_t(opening),
                    previous=_t(previous[item]),
                    refs=[ln.refs["opening"] for ln in loans if "opening" in ln.refs],
                    items=[item],
                )
            )  # fmt: skip
    return out


def check_bullets(plan: Plan) -> list[Finding]:
    """A loan repaid in full in the plan year, and no note rules out refinancing (WARN)."""
    out: list[Finding] = []
    for loan in plan.loans:
        bullet = loan.facts.get(LoanFactKey.BULLET_IN_PLAN_YEAR)
        refinanced = loan.facts.get(LoanFactKey.REFINANCED)
        ruled_out = refinanced is not None and not refinanced.value and not refinanced.ambiguous
        if bullet is not None and bullet.value and not ruled_out:
            out.append(
                finding(
                    ErrorCode.PLAN_BULLET_IN_PLAN_YEAR, label=loan.label, year=plan.plan_year,
                    refs=[r for k, r in loan.refs.items()
                          if k in ("closing", "scheduled_repayment")],
                    items=[ItemKey.SCHEDULED_REPAYMENTS_TOTAL],
                )
            )  # fmt: skip
    return out


def ebitda_estimate(plan: Plan) -> Decimal | None:
    """Rough EBITDA for plausibility only (contracts define their own EBITDA)."""
    keys = (
        ItemKey.NET_INCOME,
        ItemKey.INCOME_TAXES,
        ItemKey.INTEREST_EXPENSE,
        ItemKey.INTEREST_INCOME,
        ItemKey.DEPRECIATION,
    )
    if any(k not in plan.values for k in keys):
        return None
    v = {k: plan.values[k].value for k in keys}
    return (
        v[ItemKey.NET_INCOME]
        + v[ItemKey.INCOME_TAXES]
        + v[ItemKey.INTEREST_EXPENSE]
        - v[ItemKey.INTEREST_INCOME]
        + v[ItemKey.DEPRECIATION]
    )


def check_implausible(plan: Plan) -> list[Finding]:
    """Negative equity, EBITDA <= 0: computed anyway, flagged (the engine decides PARTIAL)."""
    out: list[Finding] = []
    equity = plan.values.get(ItemKey.EQUITY)
    if equity is not None and equity.value < 0:
        out.append(
            finding(
                ErrorCode.PLAN_IMPLAUSIBLE, detail=f"Eigenkapital negativ ({_t(equity.value)} T€)",
                refs=_refs(plan, ItemKey.EQUITY), items=[ItemKey.EQUITY],
            )
        )  # fmt: skip
    ebitda = ebitda_estimate(plan)
    if ebitda is not None and ebitda <= 0:
        out.append(
            finding(
                ErrorCode.PLAN_IMPLAUSIBLE,
                detail=f"EBITDA ≤ 0 ({_t(ebitda)} T€): Verschuldungsgrad nicht sinnvoll definiert",
                refs=_refs(plan, ItemKey.NET_INCOME, ItemKey.DEPRECIATION),
                items=[ItemKey.NET_INCOME, ItemKey.DEPRECIATION],
            )
        )
    return out


def check_status(plan: Plan) -> list[Finding]:
    if plan.status == "approved":
        return []
    if plan.version_date is None:
        return [finding(ErrorCode.PLAN_STATUS, variant="undated")]
    return [finding(ErrorCode.PLAN_STATUS, version_date=plan.version_date.strftime("%d.%m.%Y"))]


def check_plan_year_filename(plan: Plan, filename: str) -> list[Finding]:
    """Year in the file name differs from the content: trust the content (WARN)."""
    years = set(years_in(filename))
    if len(years) != 1 or plan.plan_year in years:
        return []
    return [finding(ErrorCode.PLAN_YEAR_FILENAME, filename_year=years.pop(), found=plan.plan_year)]


def check_missing(
    plan: Plan, *, reported: Collection[str], sheets_present: Collection[Statement]
) -> list[Finding]:
    """Required building blocks not found (PARTIAL; `items` lets the engine find the covenants)."""
    out: list[Finding] = []
    by_statement: dict[Statement, list[ItemKey]] = {
        Statement.GUV: [], Statement.BILANZ: [], Statement.FINANZIERUNG: list(FINANCING_ITEMS)
    }  # fmt: skip
    for spec in BLOCKS:
        if not spec.optional and spec.item is not ItemKey.PENSION_UNWINDING:
            by_statement[spec.statement].append(spec.item)
    for statement, items in by_statement.items():
        missing = [i for i in items if i not in plan.values and i not in reported]
        if not missing:
            continue
        grouped = statement is Statement.FINANZIERUNG or statement not in sheets_present
        if grouped:
            out.append(
                finding(
                    ErrorCode.PLAN_ITEM_MISSING, label=_STATEMENT_LABELS[statement],
                    items=[str(i) for i in missing],
                )
            )  # fmt: skip
        else:
            out.extend(
                finding(ErrorCode.PLAN_ITEM_MISSING, label=label_de(i), items=[i]) for i in missing
            )
    return out


def check_hidden(refs: Sequence[CellRef]) -> list[Finding]:
    if not refs:
        return []
    return [finding(ErrorCode.XLSX_HIDDEN, cells=", ".join(map(str, refs)), refs=list(refs))]


# ── Need the request or the contract profiles (not wired yet) ────────────────

_LEGAL_NOISE = re.compile(r"\b(gmbh|mbh|ag|kg|se|eg|co)\b")


def _company_key(name: str) -> str:
    return _LEGAL_NOISE.sub(" ", normalise(name.replace(".", ""))).replace(" ", "")


def check_company(plan: Plan, expected_borrower: str) -> list[Finding]:
    """Company on the cover = borrower in the contracts (BLOCK)."""
    found = plan.company or "(unbekannt)"
    if plan.company and _company_key(plan.company) == _company_key(expected_borrower):
        return []
    return [finding(ErrorCode.PLAN_COMPANY_MISMATCH, found=found, expected=expected_borrower)]


def check_plan_year(plan: Plan, requested_year: int | None) -> list[Finding]:
    """Year named in the request = plan year in the file (BLOCK)."""
    if requested_year is None or requested_year == plan.plan_year:
        return []
    return [finding(ErrorCode.PLAN_YEAR_MISMATCH, expected=requested_year, found=plan.plan_year)]


def check_scale(plan: Plan, contract_amounts_eur: Sequence[Decimal]) -> list[Finding]:
    """Bank debt (T€) vs. contract loan amounts (EUR): off by ~x1000 -> unit label wrong (WARN)."""
    bank = plan.values.get(ItemKey.BANK_DEBT)
    total_teur = sum(contract_amounts_eur, Decimal(0)) / 1000
    if bank is None or bank.value <= 0 or total_teur <= 0:
        return []
    ratio = bank.value / total_teur
    if Decimal("0.01") < ratio < Decimal(100):
        return []
    return [
        finding(
            ErrorCode.PLAN_SCALE_IMPLAUSIBLE, plan=_t(bank.value), contracts=_t(total_teur),
            refs=_refs(plan, ItemKey.BANK_DEBT), items=[ItemKey.BANK_DEBT],
        )
    ]  # fmt: skip
