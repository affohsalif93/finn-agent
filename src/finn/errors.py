"""Error codes (docs/FAILURES.md), severities, `Finding` and the German message templates.

Only the codes that are raised by code that exists are listed; later increments add theirs.
"""

from collections.abc import Sequence
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from finn.models.plan import CellRef


class Severity(StrEnum):
    REJECT = "REJECT"
    IGNORE = "IGNORE"
    ASK = "ASK"
    BLOCK = "BLOCK"
    PARTIAL = "PARTIAL"
    WARN = "WARN"
    INFO = "INFO"


class ErrorCode(StrEnum):
    # D. Attachment
    ATTACHMENT_XLS_LEGACY = "ATTACHMENT_XLS_LEGACY"
    ATTACHMENT_MACROS = "ATTACHMENT_MACROS"
    # E. Workbook
    XLSX_ENCRYPTED = "XLSX_ENCRYPTED"
    XLSX_CORRUPT = "XLSX_CORRUPT"
    XLSX_NO_CACHED_VALUES = "XLSX_NO_CACHED_VALUES"
    XLSX_ERROR_CELL = "XLSX_ERROR_CELL"
    XLSX_EXTERNAL_LINK = "XLSX_EXTERNAL_LINK"
    XLSX_HIDDEN = "XLSX_HIDDEN"
    # F. Plan content
    PLAN_YEAR_MISMATCH = "PLAN_YEAR_MISMATCH"
    PLAN_YEAR_FILENAME = "PLAN_YEAR_FILENAME"
    PLAN_NO_PLAN_COLUMN = "PLAN_NO_PLAN_COLUMN"
    PLAN_COMPANY_MISMATCH = "PLAN_COMPANY_MISMATCH"
    PLAN_UNITS_UNCLEAR = "PLAN_UNITS_UNCLEAR"
    PLAN_UNITS_CONVERTED = "PLAN_UNITS_CONVERTED"
    PLAN_SCALE_IMPLAUSIBLE = "PLAN_SCALE_IMPLAUSIBLE"
    PLAN_ITEM_MISSING = "PLAN_ITEM_MISSING"
    PLAN_SUBITEM_NOT_SHOWN = "PLAN_SUBITEM_NOT_SHOWN"
    PLAN_FACT_AMBIGUOUS = "PLAN_FACT_AMBIGUOUS"
    PLAN_LABEL_AMBIGUOUS = "PLAN_LABEL_AMBIGUOUS"
    PLAN_UNBALANCED = "PLAN_UNBALANCED"
    PLAN_LOANS_MISMATCH = "PLAN_LOANS_MISMATCH"
    PLAN_INTEREST_TIEOUT = "PLAN_INTEREST_TIEOUT"
    PLAN_OPENING_BALANCE = "PLAN_OPENING_BALANCE"
    PLAN_LOAN_NO_CONTRACT = "PLAN_LOAN_NO_CONTRACT"
    PLAN_CONTRACT_NO_LOAN = "PLAN_CONTRACT_NO_LOAN"
    PLAN_BULLET_IN_PLAN_YEAR = "PLAN_BULLET_IN_PLAN_YEAR"
    PLAN_SIGNS = "PLAN_SIGNS"
    PLAN_IMPLAUSIBLE = "PLAN_IMPLAUSIBLE"
    PLAN_STATUS = "PLAN_STATUS"
    PLAN_STATUS_REQUIRED = "PLAN_STATUS_REQUIRED"
    PLAN_ITEM_NOT_IN_PLAN = "PLAN_ITEM_NOT_IN_PLAN"
    PLAN_REPAYMENT_DIFFERS = "PLAN_REPAYMENT_DIFFERS"
    PLAN_LOAN_AMBIGUOUS = "PLAN_LOAN_AMBIGUOUS"
    # G. Contracts and profiles
    CONTRACT_CHANGED = "CONTRACT_CHANGED"
    CONTRACT_NEW_FILE = "CONTRACT_NEW_FILE"
    PROFILE_MISSING = "PROFILE_MISSING"
    PROFILE_INVALID_RECIPE = "PROFILE_INVALID_RECIPE"
    PROFILE_EXAMPLE_MISMATCH = "PROFILE_EXAMPLE_MISMATCH"
    PROFILE_INVARIANT = "PROFILE_INVARIANT"
    PROFILE_NOT_REVIEWED = "PROFILE_NOT_REVIEWED"
    PROFILE_UNMAPPED = "PROFILE_UNMAPPED"
    # H. Calculation
    CALC_DENOMINATOR = "CALC_DENOMINATOR"
    CALC_AT_THRESHOLD = "CALC_AT_THRESHOLD"
    CALC_NO_ROUNDING_RULE = "CALC_NO_ROUNDING_RULE"


# Default severity per code (FAILURES.md "Reaction"); a caller may override it.
SEVERITY: dict[ErrorCode, Severity] = {
    ErrorCode.ATTACHMENT_XLS_LEGACY: Severity.ASK,
    ErrorCode.ATTACHMENT_MACROS: Severity.INFO,
    ErrorCode.XLSX_ENCRYPTED: Severity.ASK,
    ErrorCode.XLSX_CORRUPT: Severity.ASK,
    ErrorCode.XLSX_NO_CACHED_VALUES: Severity.PARTIAL,
    ErrorCode.XLSX_ERROR_CELL: Severity.PARTIAL,
    ErrorCode.XLSX_EXTERNAL_LINK: Severity.WARN,
    ErrorCode.XLSX_HIDDEN: Severity.INFO,
    ErrorCode.PLAN_YEAR_MISMATCH: Severity.BLOCK,
    ErrorCode.PLAN_YEAR_FILENAME: Severity.WARN,
    ErrorCode.PLAN_NO_PLAN_COLUMN: Severity.BLOCK,
    ErrorCode.PLAN_COMPANY_MISMATCH: Severity.BLOCK,
    ErrorCode.PLAN_UNITS_UNCLEAR: Severity.BLOCK,
    ErrorCode.PLAN_UNITS_CONVERTED: Severity.INFO,
    ErrorCode.PLAN_SCALE_IMPLAUSIBLE: Severity.WARN,
    ErrorCode.PLAN_ITEM_MISSING: Severity.PARTIAL,
    ErrorCode.PLAN_SUBITEM_NOT_SHOWN: Severity.WARN,
    ErrorCode.PLAN_FACT_AMBIGUOUS: Severity.WARN,
    ErrorCode.PLAN_LABEL_AMBIGUOUS: Severity.ASK,
    ErrorCode.PLAN_UNBALANCED: Severity.BLOCK,
    ErrorCode.PLAN_LOANS_MISMATCH: Severity.BLOCK,
    ErrorCode.PLAN_INTEREST_TIEOUT: Severity.WARN,
    ErrorCode.PLAN_OPENING_BALANCE: Severity.WARN,
    ErrorCode.PLAN_LOAN_NO_CONTRACT: Severity.INFO,
    ErrorCode.PLAN_CONTRACT_NO_LOAN: Severity.WARN,
    ErrorCode.PLAN_BULLET_IN_PLAN_YEAR: Severity.WARN,
    ErrorCode.PLAN_SIGNS: Severity.WARN,
    ErrorCode.PLAN_IMPLAUSIBLE: Severity.WARN,
    ErrorCode.PLAN_STATUS: Severity.INFO,
    ErrorCode.PLAN_STATUS_REQUIRED: Severity.INFO,
    ErrorCode.PLAN_ITEM_NOT_IN_PLAN: Severity.WARN,
    ErrorCode.PLAN_REPAYMENT_DIFFERS: Severity.WARN,
    ErrorCode.PLAN_LOAN_AMBIGUOUS: Severity.WARN,
    ErrorCode.CONTRACT_CHANGED: Severity.WARN,
    ErrorCode.CONTRACT_NEW_FILE: Severity.WARN,
    ErrorCode.PROFILE_MISSING: Severity.PARTIAL,
    ErrorCode.PROFILE_INVALID_RECIPE: Severity.PARTIAL,
    ErrorCode.PROFILE_EXAMPLE_MISMATCH: Severity.WARN,
    ErrorCode.PROFILE_INVARIANT: Severity.PARTIAL,
    ErrorCode.PROFILE_NOT_REVIEWED: Severity.WARN,
    ErrorCode.PROFILE_UNMAPPED: Severity.PARTIAL,
    ErrorCode.CALC_DENOMINATOR: Severity.PARTIAL,
    ErrorCode.CALC_AT_THRESHOLD: Severity.WARN,
    ErrorCode.CALC_NO_ROUNDING_RULE: Severity.INFO,
}

# German reply templates, keyed by code (or "CODE.variant"); filled with str.format.
MESSAGES_DE: dict[str, str] = {
    "ATTACHMENT_XLS_LEGACY": "Die Datei {file} ist im alten Format .xls. Bitte im Format .xlsx "
    "speichern und erneut senden.",
    "ATTACHMENT_MACROS": "Die Datei {file} enthält Makros. Makros wurden nicht ausgeführt, "
    "nur die Daten wurden gelesen.",
    "XLSX_ENCRYPTED": "Die Datei {file} ist passwortgeschützt. Bitte ohne Kennwort senden.",
    "XLSX_CORRUPT": "Die Datei {file} ist beschädigt oder keine Excel-Datei.",
    "XLSX_NO_CACHED_VALUES": "Zelle {cell} enthält eine Formel ohne berechneten Wert. "
    "Bitte die Datei in Excel öffnen, speichern und erneut senden.",
    "XLSX_ERROR_CELL": "Zelle {cell} enthält {error}.",
    "XLSX_EXTERNAL_LINK": "Externer Bezug in {cell}; verwendet: gespeicherter Wert.",
    "XLSX_HIDDEN": "Ausgeblendete Zeilen oder Blätter berücksichtigt: {cells}.",
    "PLAN_YEAR_MISMATCH": "Angefragt: {expected}, die Datei enthält Plan {found}.",
    "PLAN_YEAR_MISMATCH.cover": "Das Deckblatt nennt den Wirtschaftsplan {expected}, das Blatt "
    "{sheet} enthält jedoch Werte für {found}.",
    "PLAN_YEAR_FILENAME": "Dateiname nennt {filename_year}, Inhalt ist Plan {found}.",
    "PLAN_NO_PLAN_COLUMN": "Keine Planspalte gefunden auf Blatt {sheet} (nur Ist-Werte?).",
    "PLAN_COMPANY_MISMATCH": "Der Plan gehört zu {found}, die Verträge zu {expected}.",
    "PLAN_UNITS_UNCLEAR": "Einheit auf Blatt {sheet} nicht erkennbar. Bitte angeben (T€ / €).",
    "PLAN_UNITS_CONVERTED": "{sheet} in {unit} angegeben, in T€ umgerechnet.",
    "PLAN_SCALE_IMPLAUSIBLE": "Größenordnung auffällig: Bankverbindlichkeiten {plan} T€ vs. "
    "Darlehen {contracts} T€. Einheit prüfen.",
    "PLAN_ITEM_MISSING": "Position nicht gefunden: {label}.",
    "PLAN_SUBITEM_NOT_SHOWN": "{label} nicht separat ausgewiesen.",
    "PLAN_FACT_AMBIGUOUS": "{label} nicht eindeutig; angenommen: {assumption}.",
    "PLAN_LABEL_AMBIGUOUS": "{label} kommt mehrfach vor ({cells}). Welcher Wert gilt?",
    "PLAN_UNBALANCED": "Bilanz nicht ausgeglichen: Aktiva {assets}, Passiva {liabilities} T€.",
    "PLAN_LOANS_MISMATCH": "Darlehensspiegel {loans} ≠ Bilanz {balance} T€ ({label}).",
    "PLAN_INTEREST_TIEOUT": "Zinsen laut Darlehensspiegel ({loans} T€) zuzüglich Aufzinsung "
    "({unwinding} T€) weichen von der GuV ab ({guv} T€).",
    "PLAN_OPENING_BALANCE": "Anfangsbestand {label} im Darlehensspiegel ({opening} T€) weicht "
    "vom Vorjahresendbestand der Bilanz ab ({previous} T€).",
    "PLAN_LOAN_NO_CONTRACT": "Darlehen {label} ohne Vertrag im Vertragsordner.",
    "PLAN_CONTRACT_NO_LOAN": "Vertrag {contract} (Nr. {loan_number}) fehlt im Darlehensspiegel.",
    "PLAN_BULLET_IN_PLAN_YEAR": "Darlehen {label} endfällig {year}; falls refinanziert, ist die "
    "Tilgung nicht einzubeziehen.",
    "PLAN_SIGNS": "Vorzeichen vereinheitlicht: {cells}.",
    "PLAN_IMPLAUSIBLE": "Auffälliger Wert: {detail}.",
    "PLAN_STATUS": "Entwurf vom {version_date}, nicht vom Aufsichtsrat genehmigt.",
    "PLAN_STATUS.undated": "Entwurf (ohne Datum), nicht vom Aufsichtsrat genehmigt.",
    "PLAN_STATUS_REQUIRED": "Hinweis: {lender} verlangt den beschlossenen Wirtschaftsplan "
    "({section}); der vorliegende Plan ist ein Entwurf.",
    "PLAN_ITEM_NOT_IN_PLAN": "{covenant}: im Plan nicht ausgewiesen, mit 0 angesetzt: {labels}.",
    "PLAN_REPAYMENT_DIFFERS": "Planmäßige Tilgung {year} {label}: Plan {plan} T€, laut Vertrag "
    "{contract} {expected} T€.",
    "PLAN_LOAN_AMBIGUOUS": "Darlehenszeile {label} nicht eindeutig einem Vertrag zuzuordnen "
    "({contracts}); Tilgungsabgleich übersprungen.",
    "PLAN_FACT_AMBIGUOUS.alternative": "{covenant}: {label} nicht eindeutig; angesetzt wird "
    "konservativ {chosen}. Ergebnis {cases}: {values}.",
    "PLAN_BULLET_IN_PLAN_YEAR.alternative": "{covenant}: Darlehen {label} endfällig {year}; "
    "falls refinanziert, ist die Tilgung nicht einzubeziehen (dann {value}).",
    "PLAN_IMPLAUSIBLE.ebitda": "{covenant}: EBITDA ≤ 0 ({value} T€), Verschuldungsgrad nicht "
    "sinnvoll definiert.",
    "PLAN_ITEM_MISSING.covenant": "{covenant} nicht berechenbar: {labels} fehlt im Plan.",
    "CONTRACT_CHANGED": "Vertrag {contract} ({file}) wurde seit der Profilprüfung geändert; "
    "Profil nicht neu geprüft.",
    "CONTRACT_CHANGED.missing": "Vertragsdatei {file} zu {contract} nicht gefunden; Hash nicht "
    "geprüft.",
    "CONTRACT_NEW_FILE": "Neuer Vertrag ohne geprüftes Profil: {file}.",
    "PROFILE_MISSING": "Vertragsprofil {file} konnte nicht geladen werden ({detail}); Vertrag "
    "nicht geprüft.",
    "PROFILE_INVALID_RECIPE": "Covenant {covenant} ({contract}) kann nicht automatisch berechnet "
    "werden: {detail}.",
    "PROFILE_INVALID_RECIPE.profile": "Vertragsprofil {file} enthält eine ungültige "
    "Rechenvorschrift ({detail}); Vertrag nicht geprüft.",
    "PROFILE_EXAMPLE_MISMATCH": "Beispielrechnung {contract} {covenant}: laut Vertrag {expected}, "
    "berechnet {actual}.",
    "PROFILE_INVARIANT": "Vertragsprofil {file} ist widersprüchlich ({detail}); Vertrag nicht "
    "geprüft.",
    "PROFILE_NOT_REVIEWED": "Vertragsprofil {contract} ist noch nicht fachlich geprüft.",
    "PROFILE_UNMAPPED": "Covenant {covenant} enthält Bestandteile, die ich nicht automatisch "
    "abbilde: {parts}.",
    "PROFILE_UNMAPPED.assumption": "{covenant}: {text} – im Plan nicht abbildbar; Annahme: "
    "{handling}.",
    "CALC_DENOMINATOR": "{covenant}: Nenner = {value} ≤ 0, Kennzahl nicht definiert.",
    "CALC_AT_THRESHOLD": "{covenant}: eingehalten, kein Spielraum ({value} = Grenzwert).",
    "CALC_NO_ROUNDING_RULE": "{covenant}: Vertrag nennt keine Rundung; gerechnet mit "
    "2 Nachkommastellen (kaufmännisch).",
}


class Finding(BaseModel):
    """The one error type for everything (reply header, run folder, tests)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    code: ErrorCode
    severity: Severity
    message_de: str
    refs: list[CellRef] = Field(default_factory=list)
    # Covenant IDs; the calc engine fills this from `items` (the reader knows no covenants).
    affects: list[str] | Literal["all"] = Field(default_factory=list)
    items: list[str] = Field(default_factory=list)  # building blocks / facts concerned


def finding(
    code: ErrorCode,
    *,
    variant: str | None = None,
    severity: Severity | None = None,
    refs: list[CellRef] | None = None,
    items: Sequence[str] | None = None,
    affects: list[str] | Literal["all"] | None = None,
    **params: object,
) -> Finding:
    """Build a Finding with the German template for `code` (or `code.variant`)."""
    key = f"{code}.{variant}" if variant else str(code)
    sev = severity or SEVERITY[code]
    if affects is None:
        affects = "all" if sev in (Severity.BLOCK, Severity.ASK) else []
    return Finding(
        code=code,
        severity=sev,
        message_de=MESSAGES_DE[key].format(**params),
        refs=refs or [],
        affects=affects,
        items=[str(i) for i in items or ()],
    )


def has_blocking(findings: list[Finding]) -> bool:
    """True if no covenant result may be confirmed (D7)."""
    return any(f.severity in (Severity.BLOCK, Severity.ASK) for f in findings)
