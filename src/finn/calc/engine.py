"""All contract profiles × one plan → `Report`; pure code, no LLM, no I/O (D1). Per covenant:
stopping plan findings → not_computable; invalid or unmapped recipe → manual_review; otherwise
evaluate, round, compare, headroom, tight flag, sensitivity, alternatives."""

import hashlib
from collections.abc import Collection, Sequence
from dataclasses import dataclass, replace
from decimal import Decimal
from typing import Literal

from finn.calc import rounding
from finn.calc.evaluator import (
    ConditionUse,
    Evaluation,
    Inputs,
    evaluate,
    evaluate_rounded,
    loan_fact_key,
)
from finn.calc.formula import formula_de
from finn.calc.loan_checks import contract_plan_checks
from finn.calc.notes import cite, contract_notes, no_covenant_note
from finn.calc.recipe import Dependencies, dependencies, iter_components
from finn.calc.sensitivity import sensitivity
from finn.errors import STOPPING, ErrorCode, Finding, Severity, finding
from finn.models.contract import ContractProfile, Covenant, Source, Unmapped
from finn.models.plan import FactKey, ItemKey, LoanFactKey, Plan
from finn.models.result import (
    Alternative,
    CheckedInput,
    Components,
    ContractNote,
    CovenantResult,
    CovenantStatus,
    Report,
)
from finn.numbers import format_de, format_value
from finn.plan.labels import label_de

AltStatus = Literal["met", "breached", "not_computable"]
_NOT_COMPUTABLE = STOPPING | {Severity.PARTIAL}  # findings that stop one covenant


def calculate(
    plan: Plan | None,
    profiles: Sequence[ContractProfile],
    plan_findings: Sequence[Finding],
    *,
    tight_ratio: Decimal,
    profile_findings: Sequence[Finding] = (),
    file: str | None = None,
    contract_filter: Collection[str] | None = None,
) -> Report:
    """Evaluate every covenant of every profile against the plan; `tight_ratio`: headroom below
    this share of the limit counts as tight."""
    chosen = [p for p in profiles if not contract_filter or p.contract_id in contract_filter]
    if plan is None:
        findings = [*plan_findings, *profile_findings]
        return Report(
            header=None,
            findings=findings,
            results=[],
            contract_notes=[],
            overall="incomplete",
            tightest=None,
        )
    general = [*plan_findings, *profile_findings, *contract_plan_checks(plan, chosen)]
    deps = {(p.contract_id, c.covenant_id): dependencies(p, c) for p in chosen for c in p.covenants}
    general = [_with_affects(f, deps) for f in general]
    results: list[CovenantResult] = []
    notes: list[ContractNote] = []
    for profile in chosen:
        if not profile.covenants:
            notes.append(no_covenant_note(profile))
            continue
        for cov in profile.covenants:
            own = [f for f in general if f.affects == "all" or cov.covenant_id in f.affects]
            dep = deps[(profile.contract_id, cov.covenant_id)]
            results.append(_covenant(profile, cov, plan, own, dep, tight_ratio))
        own_results = [r for r in results if r.contract_id == profile.contract_id]
        notes.extend(contract_notes(profile, own_results))
    header = CheckedInput(
        file=file,
        sha256=plan.file_sha256,
        company=plan.company,
        plan_year=plan.plan_year,
        status=plan.status,
        version_date=plan.version_date,
        reporting_date=plan.reporting_date,
        profile_hashes={p.contract_id: profile_hash(p) for p in chosen},
    )
    return Report(
        header=header,
        findings=general,
        results=results,
        contract_notes=notes,
        overall=_overall(results, general),
        tightest=_tightest(results),
    )


def profile_hash(profile: ContractProfile) -> str:
    return hashlib.sha256(profile.model_dump_json().encode("utf-8")).hexdigest()


def _with_affects(f: Finding, deps: dict[tuple[str, str], Dependencies]) -> Finding:
    if f.affects == "all" or f.affects:
        return f
    if f.severity in STOPPING:
        return f.model_copy(update={"affects": "all"})
    hit = [cov for (_, cov), d in deps.items() if d.keys & set(f.items)]
    return f.model_copy(update={"affects": hit}) if hit else f


# ── one covenant ─────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class _Ctx:
    profile: ContractProfile
    cov: Covenant
    plan: Plan
    deps: Dependencies
    tight_ratio: Decimal

    @property
    def name(self) -> str:
        return f"{self.profile.name_de} · {self.cov.name_in_contract}"


def _covenant(
    profile: ContractProfile,
    cov: Covenant,
    plan: Plan,
    own: list[Finding],
    deps: Dependencies,
    tight_ratio: Decimal,
) -> CovenantResult:
    ctx = _Ctx(profile, cov, plan, deps, tight_ratio)
    base = CovenantResult(
        covenant_id=cov.covenant_id,
        contract_id=profile.contract_id,
        contract_name_de=profile.name_de,
        name_de=cov.name_in_contract,
        metric_type=cov.metric_type,
        status="not_computable",
        operator=cov.operator,
        unit=cov.unit,
        headroom_unit=rounding.headroom_unit(cov.unit),
        threshold=_threshold(cov, plan.plan_year),
        decimals=rounding.effective_decimals(cov.rounding.decimals),
        sources=_sources(profile, cov),
        findings=own,
    )
    invalid = [f for f in own if f.code is ErrorCode.PROFILE_INVALID_RECIPE]
    if invalid:
        return _stop(base, "manual_review", invalid)
    unmapped = _unmapped(profile, cov)
    blockers = [u for u in unmapped if u.effect == "manual_review"]
    if blockers:
        f = finding(
            ErrorCode.PROFILE_UNMAPPED,
            covenant=ctx.name,
            parts="; ".join(u.text for u in blockers),
            affects=[cov.covenant_id],
        )
        return _stop(base, "manual_review", [f])
    stops = [f for f in own if f.severity in _NOT_COMPUTABLE]
    if stops:
        return _stop(base, "not_computable", stops)
    if base.threshold is None:
        return base.model_copy(update={"reason": f"kein Grenzwert für {plan.plan_year}"})
    return _evaluate(ctx, base)


def _stop(base: CovenantResult, status: CovenantStatus, why: list[Finding]) -> CovenantResult:
    reason = " ".join(dict.fromkeys(f.message_de for f in why))
    extra = [f for f in why if f not in base.findings]
    return base.model_copy(
        update={"status": status, "reason": reason, "findings": [*base.findings, *extra]}
    )


def _threshold(cov: Covenant, year: int) -> Decimal | None:
    value = cov.threshold_for(year)
    if value is None:
        return None
    return value / 1000 if cov.unit == "amount" else value  # EUR → T€


def _evaluate(ctx: _Ctx, base: CovenantResult) -> CovenantResult:
    cov, plan = ctx.cov, ctx.plan
    inputs = Inputs.from_plan(plan)
    ev = evaluate(ctx.profile, cov, inputs)
    formula, subs = formula_de(ev.numerator.traces, ev.denominator.traces)
    findings = list(base.findings)
    findings += _assumption_findings(ctx, ev)
    traced = base.model_copy(
        update={
            "components": Components(
                numerator=ev.numerator.traces, denominator=ev.denominator.traces
            ),
            "numerator_value": ev.numerator.total,
            "denominator_value": ev.denominator.total if cov.denominator else None,
            "formula_de": formula,
            "sub_formulas_de": subs,
        }
    )
    if ev.missing:
        labels = ", ".join(label_de(i) for i in dict.fromkeys(ev.missing))
        f = finding(
            ErrorCode.PLAN_ITEM_MISSING,
            variant="covenant",
            covenant=ctx.name,
            labels=labels,
            items=ev.missing,
            affects=[cov.covenant_id],
        )
        return _stop(traced.model_copy(update={"findings": findings}), "not_computable", [f])
    raw = ev.raw(cov.unit)
    if raw is None:
        value = format_de(ev.denominator.total, 0)
        if cov.metric_type == "net_debt_to_ebitda":
            f = finding(
                ErrorCode.PLAN_IMPLAUSIBLE,
                variant="ebitda",
                severity=Severity.PARTIAL,
                covenant=ctx.name,
                value=value,
                affects=[cov.covenant_id],
            )
        else:
            f = finding(
                ErrorCode.CALC_DENOMINATOR,
                covenant=ctx.name,
                value=value,
                affects=[cov.covenant_id],
            )
        return _stop(traced.model_copy(update={"findings": findings}), "not_computable", [f])
    if cov.rounding.decimals is None:
        findings.append(
            finding(ErrorCode.CALC_NO_ROUNDING_RULE, covenant=ctx.name, affects=[cov.covenant_id])
        )
    assert base.threshold is not None
    assert base.decimals is not None
    threshold, decimals = base.threshold, base.decimals
    rounded = rounding.round_half_up(raw, decimals)
    met = rounding.is_met(rounded, cov.operator, threshold)
    head = rounding.headroom(rounded, cov.operator, threshold)
    if head == 0:
        findings.append(
            finding(
                ErrorCode.CALC_AT_THRESHOLD,
                covenant=ctx.name,
                value=format_value(rounded, cov.unit, decimals),
                affects=[cov.covenant_id],
            )
        )
    alternatives, alt_findings = _alternatives(ctx, ev, inputs, rounded, decimals, threshold)
    findings += alt_findings
    findings += _plan_status_findings(ctx)
    return traced.model_copy(
        update={
            "status": "met" if met else "breached",
            "value_raw": raw,
            "value_rounded": rounded,
            "headroom": head,
            "headroom_relative": head / abs(threshold) if threshold else None,
            "tight": rounding.is_tight(head, threshold, ctx.tight_ratio),
            "sensitivity": sensitivity(
                ctx.profile,
                ctx.cov,
                plan,
                ctx.deps,
                ev,
                inputs,
                decimals=decimals,
                threshold=threshold,
            ),
            "alternatives": alternatives,
            "findings": findings,
        }
    )


def _status(value: Decimal | None, ctx: _Ctx, threshold: Decimal) -> AltStatus:
    if value is None:
        return "not_computable"
    return "met" if rounding.is_met(value, ctx.cov.operator, threshold) else "breached"


# ── findings derived during evaluation ───────────────────────────────────────


def _unmapped(profile: ContractProfile, cov: Covenant) -> list[Unmapped]:
    defs = {c.ref for c in iter_components(profile, [*cov.numerator, *cov.denominator]) if c.ref}
    out = list(cov.unmapped_components)
    for d in sorted(defs):
        if d in profile.definitions:
            out.extend(profile.definitions[d].unmapped_components)
    return out


def _assumption_findings(ctx: _Ctx, ev: Evaluation) -> list[Finding]:
    out: list[Finding] = []
    for severity, policy in (
        (Severity.WARN, "assume_zero_warn"),
        (Severity.INFO, "assume_zero_info"),
    ):
        labels = list(dict.fromkeys(a.label_de for a, p in ev.assumed if p == policy))
        if labels:
            out.append(
                finding(
                    ErrorCode.PLAN_ITEM_NOT_IN_PLAN,
                    severity=severity,
                    covenant=ctx.name,
                    labels=", ".join(labels),
                    affects=[ctx.cov.covenant_id],
                )
            )
    for u in dict.fromkeys(u for u in ev.unmapped if u.effect == "assumption"):
        out.append(
            finding(
                ErrorCode.PROFILE_UNMAPPED,
                variant="assumption",
                severity=Severity.INFO,
                covenant=ctx.name,
                text=u.text,
                handling=u.handling,
                affects=[ctx.cov.covenant_id],
            )
        )
    return out


def _cases(use: ConditionUse) -> tuple[str, str]:
    """Wording for (fact true, fact false), e.g. ("ohne Gesellschafterdarlehen", "mit …")."""
    label = use.component_label
    if use.mode == "exclude_if":
        return f"ohne {label}", f"mit {label}"
    return f"mit Abzug {label}", f"ohne Abzug {label}"


def _alternatives(
    ctx: _Ctx, ev: Evaluation, inputs: Inputs, rounded: Decimal, decimals: int, threshold: Decimal
) -> tuple[list[Alternative], list[Finding]]:
    alternatives: list[Alternative] = []
    findings: list[Finding] = []
    unit = ctx.cov.unit
    seen: set[FactKey] = set()
    for use in ev.conditions:
        if not use.ambiguous or use.fact_key in seen:
            continue
        seen.add(use.fact_key)
        overrides = {**inputs.fact_overrides, use.fact_key.value: not use.value}
        alt_value, _ = evaluate_rounded(
            ctx.profile, ctx.cov, replace(inputs, fact_overrides=overrides), decimals
        )
        if alt_value is None:
            continue
        when_true, when_false = _cases(use)
        chosen = when_true if use.value else when_false
        other = when_false if use.value else when_true
        alternatives.append(
            Alternative(
                assumption_de=other,
                value_rounded=alt_value,
                status=_status(alt_value, ctx, threshold),
            )
        )
        by_case = {chosen: rounded, other: alt_value}
        values = " / ".join(format_de(by_case[c], decimals) for c in (when_true, when_false))
        cond = ctx.profile.conditions[use.condition_id]
        findings.append(
            finding(
                ErrorCode.PLAN_FACT_AMBIGUOUS,
                variant="alternative",
                covenant=ctx.name,
                label=cond.label_de,
                chosen=f"{chosen} ({format_de(rounded, decimals)})",
                cases=f"{when_true} / {when_false}",
                values=values,
                items=[use.fact_key],
                affects=[ctx.cov.covenant_id],
            )
        )
    for loan in {ln.label: ln for ln in ev.uncertain_bullets}.values():
        key = loan_fact_key(loan, LoanFactKey.REFINANCED)
        overrides = {**inputs.fact_overrides, key: True}
        alt_value, _ = evaluate_rounded(
            ctx.profile, ctx.cov, replace(inputs, fact_overrides=overrides), decimals
        )
        if alt_value is None:
            continue
        alternatives.append(
            Alternative(
                assumption_de=f"Endfälligkeit {loan.label} refinanziert",
                value_rounded=alt_value,
                status=_status(alt_value, ctx, threshold),
            )
        )
        findings.append(
            finding(
                ErrorCode.PLAN_BULLET_IN_PLAN_YEAR,
                variant="alternative",
                covenant=ctx.name,
                label=loan.label,
                year=ctx.plan.plan_year,
                value=format_value(alt_value, unit, decimals),
                items=[ItemKey.SCHEDULED_REPAYMENTS_TOTAL],
                affects=[ctx.cov.covenant_id],
            )
        )
    return alternatives, findings


def _plan_status_findings(ctx: _Ctx) -> list[Finding]:
    required = ctx.cov.plan_check.plan_status_required
    if required != "approved" or ctx.plan.status == "approved":
        return []
    pc = ctx.cov.plan_check
    src = ctx.profile.source(pc.duty_source_id or pc.source_id)
    section = cite(src) if src else "Vertrag"
    return [
        finding(
            ErrorCode.PLAN_STATUS_REQUIRED,
            lender=ctx.profile.parties.lender,
            section=section,
            affects=[ctx.cov.covenant_id],
        )
    ]


# ── report-level ─────────────────────────────────────────────────────────────


def _overall(
    results: list[CovenantResult], findings: list[Finding]
) -> Literal["all_met", "some_breached", "incomplete"]:
    if any(r.status == "breached" for r in results):
        return "some_breached"
    incomplete = any(r.status in ("not_computable", "manual_review") for r in results)
    lost = any(f.code in (ErrorCode.PROFILE_MISSING, ErrorCode.PROFILE_INVARIANT) for f in findings)
    if incomplete or lost or (not results and any(f.severity in _NOT_COMPUTABLE for f in findings)):
        return "incomplete"
    return "all_met"


def _tightest(results: list[CovenantResult]) -> str | None:
    """Smallest buffer: with sensitivities for all, the smallest earnings / equity drop in T€
    (one scale across ratio types); otherwise the smallest headroom relative to the threshold."""
    computed = [r for r in results if r.status in ("met", "breached")]
    if not computed:
        return None
    drops = [(r.sensitivity.max_drop_t_eur, r.covenant_id) for r in computed if r.sensitivity]
    if len(drops) == len(computed):
        return min(drops)[1]
    heads = [
        (r.headroom_relative, r.covenant_id) for r in computed if r.headroom_relative is not None
    ]
    return min(heads)[1] if heads else None


def _sources(profile: ContractProfile, cov: Covenant) -> list[Source]:
    ids: list[str | None] = [cov.source_id, cov.rounding.source_id, cov.test.source_id]
    comps = list(iter_components(profile, [*cov.numerator, *cov.denominator]))
    for c in comps:
        ids.append(c.source_id)
        if c.ref and c.ref in profile.definitions:
            d = profile.definitions[c.ref]
            ids += [d.source_id, *(n.source_id for n in (*d.exclusions, *d.clarifications))]
        for cond_id in (c.exclude_if, c.only_if):
            if cond_id and cond_id in profile.conditions:
                cond = profile.conditions[cond_id]
                ids += [cond.source_id, *cond.more_source_ids]
    ids += [cov.plan_check.source_id, *(a.source_id for a in cov.plan_check.actions)]
    ids += [cov.breach_consequence.source_id, *cov.breach_consequence.more_source_ids]
    ids.append(cov.cure.source_id if cov.cure else None)
    out = [profile.source(i) for i in dict.fromkeys(i for i in ids if i)]
    return [s for s in out if s is not None]
