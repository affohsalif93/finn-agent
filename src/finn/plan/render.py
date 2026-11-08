"""Plain-text view of a `PlanReadResult` for `finn plan read` (no extra dependencies)."""

from finn.models.run import PlanReadResult
from finn.numbers import format_de
from finn.text import text_table


def render_text(result: PlanReadResult) -> str:
    lines: list[str] = []
    plan = result.plan
    if plan is not None:
        version = plan.version_date.strftime("%d.%m.%Y") if plan.version_date else "-"
        lines += [
            f"Unternehmen: {plan.company or '-'}",
            f"Planjahr:    {plan.plan_year} (Stichtag {plan.reporting_date:%d.%m.%Y})",
            f"Status:      {plan.status} (Stand {version})",
            f"SHA-256:     {plan.file_sha256}",
            "",
            "Bausteine (T€)",
        ]
        rows = [
            [
                item.value,
                format_de(pv.value, 0),
                str(pv.ref) if pv.ref else f"Summe aus {len(pv.derived_from)} Zellen",
                pv.label,
            ]
            for item, pv in plan.values.items()
        ]
        lines += text_table(["Baustein", "Wert", "Zelle", "Bezeichnung"], rows)
        lines += ["", "Darlehen (T€)"]
        loan_rows = [
            [ln.label, ln.loan_number or "-", ln.kind, *(format_de(v, 0) for v in (
                ln.opening, ln.drawdown, ln.scheduled_repayment, ln.special_repayment,
                ln.closing, ln.interest))]
            for ln in plan.loans
        ]  # fmt: skip
        headers = ["Darlehen", "Nr.", "Art", "Anfang", "Ausz.", "Tilg.", "Sondertilg.", "Ende"]
        lines += text_table([*headers, "Zins"], loan_rows, max_width=40)
        lines += ["", "Fakten"]
        fact_rows = [
            [key.value, str(f.value), str(f.evidence or "-"), "ja" if f.ambiguous else "nein"]
            for key, f in plan.facts.items()
        ]
        lines += text_table(["Fakt", "Wert", "Beleg", "unklar"], fact_rows)
    lines += ["", "Befunde"]
    if result.findings:
        finding_rows = [[f.severity, f.code, f.message_de] for f in result.findings]
        lines += text_table(["Stufe", "Code", "Meldung"], finding_rows, max_width=120)
    else:
        lines.append("(keine)")
    return "\n".join(lines)
