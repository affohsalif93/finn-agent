"""Contract ↔ plan checks: company, scale, loan matching and scheduled repayments."""

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

from finn.errors import ErrorCode, Finding, finding
from finn.models.contract import ContractProfile, Loan
from finn.models.plan import ItemKey, LoanLine, Plan
from finn.numbers import format_de
from finn.plan import checks
from finn.plan.labels import normalise, normalise_loan_number


def contract_plan_checks(plan: Plan, profiles: Sequence[ContractProfile]) -> list[Finding]:
    out: list[Finding] = []
    for borrower in dict.fromkeys(p.parties.borrower for p in profiles):
        out += checks.check_company(plan, borrower)
    out += checks.check_scale(plan, [p.amount_eur for p in profiles])
    out += loan_checks(plan, profiles)
    return out


@dataclass(frozen=True)
class LoanMatchResult:
    assigned: dict[tuple[str, str], list[LoanLine]]  # (contract_id, loan_id) -> plan lines
    ambiguous: list[tuple[LoanLine, list[str]]]
    unmatched: list[LoanLine]


def match_loans(plan: Plan, profiles: Sequence[ContractProfile]) -> LoanMatchResult:
    """Loan number first; then label patterns, only for contract loans without a number hit."""
    contract_loans: list[tuple[ContractProfile, Loan]] = [
        (p, ln) for p in profiles for ln in p.loans
    ]
    assigned: dict[tuple[str, str], list[LoanLine]] = {
        (p.contract_id, ln.loan_id): [] for p, ln in contract_loans
    }
    taken: set[int] = set()
    for p, loan in contract_loans:
        if loan.match.loan_number is None:
            continue
        number = normalise_loan_number(loan.match.loan_number)
        for i, line in enumerate(plan.loans):
            if line.loan_number and normalise_loan_number(line.loan_number) == number:
                assigned[(p.contract_id, loan.loan_id)].append(line)
                taken.add(i)
    open_loans = [(p, ln) for p, ln in contract_loans if not assigned[(p.contract_id, ln.loan_id)]]
    ambiguous: list[tuple[LoanLine, list[str]]] = []
    unmatched: list[LoanLine] = []
    for i, line in enumerate(plan.loans):
        if i in taken:
            continue
        label = normalise(line.label, keep_parens=True)
        hits = [
            (p, ln)
            for p, ln in open_loans
            if any(normalise(pat) in label for pat in ln.match.label_patterns)
        ]
        if len(hits) == 1:
            p, ln = hits[0]
            assigned[(p.contract_id, ln.loan_id)].append(line)
        elif len(hits) > 1:
            ambiguous.append((line, [p.contract_id for p, _ in hits]))
        else:
            unmatched.append(line)
    return LoanMatchResult(assigned=assigned, ambiguous=ambiguous, unmatched=unmatched)


def loan_checks(plan: Plan, profiles: Sequence[ContractProfile]) -> list[Finding]:
    """PLAN_LOAN_AMBIGUOUS, PLAN_CONTRACT_NO_LOAN, PLAN_REPAYMENT_DIFFERS, PLAN_LOAN_NO_CONTRACT."""
    if not plan.loans:
        return []  # no loan schedule: PLAN_ITEM_MISSING already says so
    result = match_loans(plan, profiles)
    out: list[Finding] = []
    ambiguous_contracts = {c for _, cs in result.ambiguous for c in cs}
    for line, contracts in result.ambiguous:
        out.append(
            finding(
                ErrorCode.PLAN_LOAN_AMBIGUOUS,
                label=line.label,
                contracts=", ".join(contracts),
                refs=[line.refs["label"]] if "label" in line.refs else [],
            )
        )
    by_id = {p.contract_id: p for p in profiles}
    for (contract_id, loan_id), lines in result.assigned.items():
        profile = by_id[contract_id]
        loan = next(ln for ln in profile.loans if ln.loan_id == loan_id)
        if not lines:
            if contract_id not in ambiguous_contracts:
                out.append(
                    finding(
                        ErrorCode.PLAN_CONTRACT_NO_LOAN,
                        contract=profile.name_de,
                        loan_number=loan.match.loan_number or "ohne Nummer",
                    )
                )
            continue
        out += _repayment_check(plan, profile, loan, lines)
    for line in result.unmatched:
        if line.kind == "bank":
            out.append(
                finding(
                    ErrorCode.PLAN_LOAN_NO_CONTRACT,
                    label=line.label,
                    refs=[line.refs["label"]] if "label" in line.refs else [],
                )
            )
    return out


def _repayment_check(
    plan: Plan, profile: ContractProfile, loan: Loan, lines: list[LoanLine]
) -> list[Finding]:
    entries = [e for e in loan.repayment_schedule if e.date.year == plan.plan_year]
    if not entries:
        return []  # schedule excerpt does not cover the plan year
    expected = sum((e.amount_eur for e in entries), Decimal(0)) / 1000
    planned = sum((ln.scheduled_repayment for ln in lines), Decimal(0))
    if planned == expected:
        return []
    refs = [ln.refs["scheduled_repayment"] for ln in lines if "scheduled_repayment" in ln.refs]
    return [
        finding(
            ErrorCode.PLAN_REPAYMENT_DIFFERS,
            year=plan.plan_year,
            label=", ".join(ln.label for ln in lines),
            plan=format_de(planned, 0),
            contract=profile.name_de,
            expected=format_de(expected, 0),
            refs=refs,
            items=[ItemKey.SCHEDULED_REPAYMENTS_TOTAL],
        )
    ]
