"""All contract profiles × one plan → `Report` (ARCHITECTURE §6.4). Pure code, no LLM, no I/O.

Order per covenant: blocking / partial plan findings on a used building block → not_computable;
invalid or unmapped recipe → manual_review; otherwise evaluate, round, compare, headroom, tight
flag, sensitivity, alternatives for unclear plan facts.
"""

import hashlib
from collections.abc import Collection, Sequence
from dataclasses import dataclass, replace
from decimal import Decimal
from typing import Literal

from finn.calc import rounding
from finn.calc.evaluator import ConditionUse, Evaluation, Inputs, evaluate, loan_fact_key
from finn.calc.formula import formula_de
from finn.calc.recipe import Dependencies, dependencies, iter_components
from finn.calc.sensitivity import break_point, max_whole_drop
from finn.errors import STOPPING, ErrorCode, Finding, Severity, finding
from finn.models.contract import ContractProfile, Covenant, Loan, PricingGrid, Source, Unmapped
from finn.models.plan import FactKey, ItemKey, LoanFactKey, LoanLine, Plan
from finn.models.result import (
    Alternative,
    CheckedInput,
    Components,
    ContractNote,
    CovenantResult,
    CovenantStatus,
    Report,
    Sensitivity,
    Trace,
    Used,
)
from finn.numbers import format_de, format_value, significant_decimals
from finn.plan import checks
from finn.plan.labels import label_de, normalise, normalise_loan_number

AltStatus = Literal["met", "breached", "not_computable"]
Driver = Literal["ebitda", "equity"]
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
            notes.append(_no_covenant_note(profile))
            continue
        for cov in profile.covenants:
            own = [f for f in general if f.affects == "all" or cov.covenant_id in f.affects]
            dep = deps[(profile.contract_id, cov.covenant_id)]
            results.append(_covenant(profile, cov, plan, own, dep, tight_ratio))
        own_results = [r for r in results if r.contract_id == profile.contract_id]
        notes.extend(_contract_notes(profile, own_results))
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
            "sensitivity": _sensitivity(ctx, ev, inputs, decimals, threshold),
            "alternatives": alternatives,
            "findings": findings,
        }
    )


def _rounded(ctx: _Ctx, inputs: Inputs, decimals: int) -> tuple[Decimal | None, Evaluation]:
    ev = evaluate(ctx.profile, ctx.cov, inputs)
    raw = ev.raw(ctx.cov.unit)
    return (None if raw is None else rounding.round_half_up(raw, decimals)), ev


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
        alt_value, _ = _rounded(ctx, replace(inputs, fact_overrides=overrides), decimals)
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
        alt_value, _ = _rounded(ctx, replace(inputs, fact_overrides=overrides), decimals)
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
    section = _cite(src) if src else "Vertrag"
    return [
        finding(
            ErrorCode.PLAN_STATUS_REQUIRED,
            lender=ctx.profile.parties.lender,
            section=section,
            affects=[ctx.cov.covenant_id],
        )
    ]


# ── sensitivity ──────────────────────────────────────────────────────────────


def _driver(deps: Dependencies) -> tuple[Driver, ItemKey] | None:
    if "ebitda" in deps.definitions or ItemKey.NET_INCOME in deps.items:
        return "ebitda", ItemKey.NET_INCOME  # EBITDA falls 1:1 with net income
    if ItemKey.EQUITY in deps.items:
        return "equity", ItemKey.EQUITY
    return None


def _find_ref(traces: Sequence[Trace], ref: str) -> Used | None:
    for t in traces:
        if isinstance(t, Used):
            if t.component == f"ref:{ref}":
                return t
            hit = _find_ref(t.parts, ref)
            if hit is not None:
                return hit
    return None


def _sensitivity(
    ctx: _Ctx, ev: Evaluation, inputs: Inputs, decimals: int, threshold: Decimal
) -> Sensitivity | None:
    driver = _driver(ctx.deps)
    if driver is None or ctx.cov.unit == "amount":
        return None
    name, item = driver
    if item not in ctx.plan.values:
        return None

    def at(d: Decimal) -> tuple[Decimal | None, Evaluation]:
        adjusted = {**inputs.adjustments, item: inputs.adjustments.get(item, Decimal(0)) - d}
        return _rounded(ctx, replace(inputs, adjustments=adjusted), decimals)

    _, ev1 = at(Decimal(1))
    scale = Decimal(100) if ctx.cov.unit == "percent" else Decimal(1)
    estimate = break_point(
        ev.numerator.total,
        ev1.numerator.total - ev.numerator.total,
        ev.denominator.total,
        ev1.denominator.total - ev.denominator.total,
        scale=scale,
        operator=ctx.cov.operator,
        threshold=threshold,
        decimals=decimals,
    )
    if estimate is None:
        return None
    drop = max_whole_drop(lambda d: at(d)[0], ctx.cov.operator, threshold, estimate)
    if drop is None:
        return None
    at_drop, _ = at(drop)
    after, _ = at(drop + 1)
    if at_drop is None or after is None:
        return None
    if name == "ebitda":
        ebitda = _find_ref([*ev.numerator.traces, *ev.denominator.traces], "ebitda")
        base = ebitda.value if ebitda is not None else ctx.plan.values[item].value
        label = "EBITDA"
    else:
        base = ctx.plan.values[item].value
        label = "Eigenkapital"
    pct = rounding.round_half_up(drop / base * 100, 1) if base else Decimal(0)
    return Sensitivity(
        driver=name,
        driver_label_de=label,
        base_t_eur=base,
        max_drop_t_eur=drop,
        max_drop_pct=pct,
        value_at_max_drop=at_drop,
        value_after_next_t_eur=after,
        text_de=_sensitivity_text(ctx, label, base, drop, pct, after, decimals, threshold),
    )


def _sensitivity_text(
    ctx: _Ctx,
    label: str,
    base: Decimal,
    drop: Decimal,
    pct: Decimal,
    after: Decimal,
    decimals: int,
    threshold: Decimal,
) -> str:
    unit, cov = ctx.cov.unit, ctx.cov
    limit = format_value(threshold, unit, rounding.effective_decimals(cov.rounding.decimals))
    if cov.unit == "percent":
        limit = f"{format_de(threshold, max(1, significant_decimals(threshold)))} %"
    verb = "überschreitet" if cov.operator == "<=" else "unterschreitet"
    ceteris = " (übrige Werte unverändert, Bilanzsumme gleich)" if label == "Eigenkapital" else ""
    if drop < 0:
        return (
            f"Damit „{cov.name_in_contract}“ eingehalten wird, müsste das {label} um mindestens "
            f"{format_de(-drop, 0)} T€ ({format_de(-pct, 1)} %) steigen{ceteris}."
        )
    return (
        f"Das {label} ({format_de(base, 0)} T€) darf um bis zu {format_de(drop, 0)} T€ "
        f"({format_de(pct, 1)} %) sinken{ceteris}, bevor „{cov.name_in_contract}“ die Grenze "
        f"von {limit} {verb}; bei {format_de(drop + 1, 0)} T€ weniger: "
        f"{format_value(after, unit, decimals)}."
    )


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


def _cite(src: Source) -> str:
    return f"{src.section}, S. {src.page}"


# ── contract notes ───────────────────────────────────────────────────────────


def _no_covenant_note(profile: ContractProfile) -> ContractNote:
    reason = profile.no_covenant_reason
    assert reason is not None  # model invariant
    return ContractNote(
        contract_id=profile.contract_id,
        kind="no_covenants",
        text_de=f"{profile.name_de}: keine Financial Covenants. {reason.text}",
        source_id=reason.source_id,
    )


_ACTIONS_DE = {
    "talks": "Gespräche über geeignete Maßnahmen",
    "talks_on_request": "auf Verlangen Gespräch über Gegenmaßnahmen",
    "notify_immediately": "unverzügliche Unterrichtung der Bank",
    "present_measures": "Maßnahmen innerhalb von {days} Tagen darlegen",
}


def _contract_notes(profile: ContractProfile, results: list[CovenantResult]) -> list[ContractNote]:
    notes: list[ContractNote] = []
    by_id = {r.covenant_id: r for r in results}
    grid = profile.pricing_grid
    if grid is not None:
        notes.extend(_pricing_note(profile, grid, by_id.get(grid.metric_ref)))
    seen: set[tuple[str, str | None]] = set()
    for cov in profile.covenants:
        pc = cov.plan_check
        if pc.applies and ("plan_check", pc.source_id) not in seen:
            seen.add(("plan_check", pc.source_id))
            notes.append(_plan_check_note(profile, cov))
        if cov.cure is not None and ("cure", cov.cure.source_id) not in seen:
            seen.add(("cure", cov.cure.source_id))
            notes.append(_cure_note(profile, cov))
        result = by_id.get(cov.covenant_id)
        if result is not None and result.status == "breached":
            notes.append(_breach_note(profile, cov))
    return notes


def _section(profile: ContractProfile, source_id: str | None) -> str:
    src = profile.source(source_id)
    return f" ({_cite(src)})" if src else ""


def _pricing_note(
    profile: ContractProfile, grid: PricingGrid, result: CovenantResult | None
) -> list[ContractNote]:
    initial = format_de(grid.initial_margin_pct, 2)
    pricing_only = _section(profile, grid.more_source_ids[0] if grid.more_source_ids else None)
    if result is None or result.value_rounded is None:
        text = (
            f"{profile.name_de}: Margenraster{_section(profile, grid.source_id)} – Stufe "
            f"nicht bestimmbar; Preisbestimmung, kein Covenant{pricing_only}."
        )
        return [
            ContractNote(
                contract_id=profile.contract_id,
                kind="pricing",
                text_de=text,
                source_id=grid.source_id,
            )
        ]
    value = result.value_rounded
    step = grid.step_for(value)
    index = grid.steps.index(step)
    shown = format_de(value, result.decimals or 2)
    if step.upper is None:
        lower = grid.steps[index - 1].upper if index > 0 else None
        bound = f"{shown} > {format_de(lower, 2)}" if lower is not None else shown
    else:
        bound = f"{shown} {'≤' if step.upper_inclusive else '<'} {format_de(step.upper, 2)}"
    text = (
        f"{profile.name_de}: {result.name_de} {bound} → Marge {format_de(step.margin_pct, 2)} % "
        f"p. a. laut Margenraster{_section(profile, grid.source_id)} (anfänglich {initial} %); "
        f"Preisbestimmung, kein Covenant{pricing_only}."
    )
    return [
        ContractNote(
            contract_id=profile.contract_id, kind="pricing", text_de=text, source_id=grid.source_id
        )
    ]


def _plan_check_note(profile: ContractProfile, cov: Covenant) -> ContractNote:
    pc = cov.plan_check
    actions = []
    for a in pc.actions:
        actions.append(
            _ACTIONS_DE[a.type].format(days=a.within_days) + _section(profile, a.source_id)
        )
    if pc.consequence == "early_warning_only":
        lead = "Unterschreitung auf Planbasis ist eine Frühwarnung, kein Kündigungsgrund"
    else:
        lead = (
            "Unterschreitung auf Planbasis ist kein Vertragsbruch (maßgeblich ist der "
            "Jahresabschluss), verlangt aber eine Reaktion"
        )
    text = f"{profile.name_de}: {lead}{_section(profile, pc.source_id)}"
    if actions:
        text += ": " + "; ".join(actions)
    return ContractNote(
        contract_id=profile.contract_id,
        kind="plan_check",
        text_de=text + ".",
        source_id=pc.source_id,
    )


def _cure_note(profile: ContractProfile, cov: Covenant) -> ContractNote:
    cure = cov.cure
    assert cure is not None
    where = _section(profile, cure.source_id)
    if not cure.allowed:
        text = f"{profile.name_de}: keine Heilung durch Eigenkapitalzuführung{where}."
    else:
        limits = []
        if cure.window_days:
            limits.append(f"innerhalb von {cure.window_days} Tagen")
        if cure.max_count:
            limits.append(f"höchstens {cure.max_count}× während der Laufzeit")
        if cure.consecutive_allowed is False:
            limits.append("nicht in aufeinanderfolgenden Geschäftsjahren")
        text = (
            f"{profile.name_de} ({cov.name_in_contract}): Heilung durch Eigenkapitalzuführung "
            f"möglich (rechnerisch dem Zähler hinzugerechnet), {', '.join(limits)}{where}."
        )
    return ContractNote(
        contract_id=profile.contract_id, kind="cure", text_de=text, source_id=cure.source_id
    )


def _breach_note(profile: ContractProfile, cov: Covenant) -> ContractNote:
    bc = cov.breach_consequence
    where = _section(profile, bc.source_id)
    what = {
        "termination": "Kündigungsrecht der Bank",
        "event_of_default": "Kündigungsgrund (Event of Default)",
        "collateral_then_termination": "Bank kann Besicherung verlangen; Kündigung, wenn die "
        f"Sicherheit nicht binnen {bc.window_days} Tagen gestellt wird",
    }[bc.type]
    text = (
        f"{profile.name_de} ({cov.name_in_contract}): würde der Wert so im Jahresabschluss "
        f"erreicht: {what}{where}."
    )
    return ContractNote(
        contract_id=profile.contract_id,
        kind="breach_consequence",
        text_de=text,
        source_id=bc.source_id,
    )


# ── contract ↔ plan checks (loans, company, scale) ──────────────────────────


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
