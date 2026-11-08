"""German wording of covenant results, shared by `finn check` and the reply mail."""

import re

from finn.models.result import CovenantResult, CovenantStatus
from finn.numbers import format_de, format_value, printed_decimals

STATUS_DE: dict[CovenantStatus, str] = {
    "met": "ja",
    "breached": "nein",
    "not_computable": "nicht berechenbar",
    "manual_review": "manuelle Prüfung",
}
_ALT_STATUS_DE = {"met": "eingehalten", "breached": "nicht eingehalten"}


def alt_status_de(status: str) -> str:
    """Outcome of an alternative calculation: "eingehalten" / "nicht eingehalten" / …"""
    return _ALT_STATUS_DE.get(status, "nicht berechenbar")


def minus(text: str) -> str:
    """Typographic minus sign (−) instead of the hyphen."""
    return text.replace("-", "−")


def value_de(r: CovenantResult) -> str:
    if r.value_rounded is None:
        return "–"
    return format_value(r.value_rounded, r.unit, r.decimals or 0)


def threshold_de(r: CovenantResult) -> str:
    """ "≥ 25,0 %" (operator + limit as written in the contract)."""
    if r.threshold is None:
        return "–"
    op = "≥" if r.operator == ">=" else "≤"
    return f"{op} {format_value(r.threshold, r.unit, printed_decimals(r.threshold))}"


def limit_de(r: CovenantResult) -> str:
    """The limit without the operator: "25,0 %"."""
    return threshold_de(r).split(" ", 1)[1]


def headroom_de(r: CovenantResult) -> str:
    if r.headroom is None:
        return "–"
    text = format_de(r.headroom, r.decimals or 0)
    return {"pp": f"{text} PP", "T€": f"{text} T€"}.get(r.headroom_unit, text)


def met_de(r: CovenantResult) -> str:
    text = STATUS_DE[r.status]
    return "ja (knapp)" if r.status == "met" and r.tight else text


def plan_status_de(status: str) -> str:
    return "Entwurf" if status == "draft" else "beschlossen"


def zeros_note(labels: list[str]) -> str:
    return f"Im Plan nicht ausgewiesen, mit 0 angesetzt: {', '.join(labels)}."


def strip_parenthetical(label: str) -> str:
    """ "Eigenkapital (bereinigt)" → "Eigenkapital"."""
    return re.sub(r"\s*\([^)]*\)\s*$", "", label)
