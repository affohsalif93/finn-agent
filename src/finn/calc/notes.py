"""Contract notes for the reply: pricing grid, plan-check duties, cure rights, breach effects."""

from finn.models.contract import ContractProfile, Covenant, PricingGrid, Source
from finn.models.result import ContractNote, CovenantResult
from finn.numbers import format_de


def cite(src: Source) -> str:
    return f"{src.section}, S. {src.page}"


def no_covenant_note(profile: ContractProfile) -> ContractNote:
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


def contract_notes(profile: ContractProfile, results: list[CovenantResult]) -> list[ContractNote]:
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
    return f" ({cite(src)})" if src else ""


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
