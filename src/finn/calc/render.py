"""Plain-text view of a `Report` for `finn check` (German)."""

from collections.abc import Sequence
from decimal import Decimal

from finn.calc.formula import number_de
from finn.models.result import AssumedZero, CovenantResult, Excluded, Report, Trace, Used
from finn.numbers import format_de
from finn.plan.render import text_table

_STATUS_DE = {
    "met": "ja",
    "breached": "nein",
    "not_computable": "nicht berechenbar",
    "manual_review": "manuelle Prüfung",
}


def _decimals(value: Decimal) -> int:
    exponent = value.as_tuple().exponent
    return max(0, -exponent) if isinstance(exponent, int) else 0


def value_de(r: CovenantResult) -> str:
    if r.value_rounded is None:
        return "–"
    text = format_de(r.value_rounded, r.decimals or 0)
    return f"{text} %" if r.unit == "percent" else f"{text} T€" if r.unit == "amount" else text


def threshold_de(r: CovenantResult) -> str:
    if r.threshold is None:
        return "–"
    op = "≥" if r.operator == ">=" else "≤"
    text = format_de(r.threshold, _decimals(r.threshold))
    suffix = " %" if r.unit == "percent" else " T€" if r.unit == "amount" else ""
    return f"{op} {text}{suffix}"


def headroom_de(r: CovenantResult) -> str:
    if r.headroom is None:
        return "–"
    text = format_de(r.headroom, r.decimals or 0)
    return {"pp": f"{text} PP", "T€": f"{text} T€"}.get(r.headroom_unit, text)


def met_de(r: CovenantResult) -> str:
    text = _STATUS_DE[r.status]
    return "ja (knapp)" if r.status == "met" and r.tight else text


def _cells(refs: Sequence[object]) -> str:
    if not refs:
        return ""
    if len(refs) <= 2:
        return f" [{', '.join(str(r) for r in refs)}]"
    return f" [{len(refs)} Zellen]"


def _trace_lines(traces: Sequence[Trace], indent: str) -> list[str]:
    lines: list[str] = []
    for t in traces:
        if isinstance(t, Used):
            weight = (
                ""
                if t.weight == 1
                else "− "
                if t.weight == -1
                else f"{number_de(t.weight).replace('-', '−')} × "
            )
            lines.append(f"{indent}{weight}{t.label_de} {number_de(t.value)}{_cells(t.refs)}")
            lines += _trace_lines(t.parts, indent + "    ")
        elif isinstance(t, Excluded):
            value = f" {number_de(t.value)}" if t.value is not None else ""
            evidence = (
                f"; Beleg {t.evidence.evidence}" if t.evidence and t.evidence.evidence else ""
            )
            lines.append(f"{indent}– {t.label_de}{value}{_cells(t.refs)}: {t.reason}{evidence}")
        elif isinstance(t, AssumedZero):
            lines.append(f"{indent}– {t.label_de}: {t.reason}")
    return lines


def render_report(report: Report) -> str:
    lines: list[str] = []
    h = report.header
    if h is not None:
        version = h.version_date.strftime("%d.%m.%Y") if h.version_date else "-"
        status = "Entwurf" if h.status == "draft" else "beschlossen"
        lines += [
            f"Geprüfte Eingabe: {h.file or '-'} · {h.company or '-'} · Plan {h.plan_year} "
            f"({status} vom {version}) · Stichtag {h.reporting_date:%d.%m.%Y}",
            f"SHA-256: {h.sha256}",
            "",
        ]
    overall = {
        "all_met": "alle Financial Covenants eingehalten",
        "some_breached": "mindestens ein Covenant nicht eingehalten",
        "incomplete": "unvollständig (nicht alle Covenants berechenbar)",
    }[report.overall]
    lines.append(f"Ergebnis auf Planbasis: {overall} (Frühwarnung, kein Vertragsbruch).")
    tight = next((r for r in report.results if r.covenant_id == report.tightest), None)
    if tight is not None:
        lines.append(f"Am engsten: {tight.contract_name_de} · {tight.name_de} ({value_de(tight)}).")
        if tight.sensitivity:
            lines.append(tight.sensitivity.text_de)
    lines.append("")
    year = h.plan_year if h else ""
    rows = [
        [f"{r.contract_id} {r.contract_name_de}", r.name_de, value_de(r), threshold_de(r),
         headroom_de(r), met_de(r)]
        for r in report.results
    ]  # fmt: skip
    headers = ["Vertrag", "Covenant", f"Plan {year}", "Grenze", "Spielraum", "Erfüllt"]
    lines += text_table(headers, rows) if rows else ["(keine Covenants berechnet)"]
    lines += ["", "Rechenweg"]
    for r in report.results:
        lines += ["", *_result_lines(r)]
    if report.contract_notes:
        lines += ["", "Hinweise"]
        lines += [f"- {n.text_de}" for n in report.contract_notes]
    general = [f for f in report.findings if not f.affects]
    if general:
        lines += ["", "Weitere Befunde"]
        lines += [f"- [{f.severity}] {f.code}: {f.message_de}" for f in general]
    lines += ["", "Alle Kennzahlen wurden deterministisch berechnet, nicht vom Sprachmodell."]
    return "\n".join(lines)


def _result_lines(r: CovenantResult) -> list[str]:
    src = next((s for s in r.sources), None)
    cite = f" ({src.section}, S. {src.page})" if src else ""
    out = [f"{r.contract_id} · {r.name_de}{cite}: {met_de(r)}"]
    if r.formula_de:
        raw = number_de(r.value_raw, 4) if r.value_raw is not None else "–"
        suffix = f" = {raw} → gerundet {value_de(r)}" if r.value_rounded is not None else ""
        if r.unit == "percent" and r.value_raw is not None:
            suffix = f" × 100 = {raw} → gerundet {value_de(r)}"
        out.append(f"  {r.formula_de}{suffix}")
        out += [f"  {s}" for s in r.sub_formulas_de]
    if r.reason:
        out.append(f"  Grund: {r.reason}")
    if r.components:
        out.append("  Zähler:")
        out += _trace_lines(r.components.numerator, "    ")
        if r.components.denominator:
            out.append("  Nenner:")
            out += _trace_lines(r.components.denominator, "    ")
    for alt in r.alternatives:
        out.append(f"  Alternative ({alt.assumption_de}): {number_de(alt.value_rounded)}")
    if r.sensitivity:
        out.append(f"  Sensitivität: {r.sensitivity.text_de}")
    for f in r.findings:
        out.append(f"  [{f.severity}] {f.message_de}")
    return out
