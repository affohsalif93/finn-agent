"""Case catalogue for the live mail tests (`test_mail_cases.py`) and `finn demo send-cases`.

One `Case` per scenario: who sends what (steps, attachments from the plan fixture generator,
.eml variants via `mailkit.build_mail`) and what must come back (reply kinds in order with key
German phrases, error codes from the run folder or the poller log, exactly that many replies).
Every send gets a fresh nonce in its Message-ID (workflow ID = Message-ID), so runs repeat.
"""

from dataclasses import dataclass, field
from pathlib import Path

from finn.models.mail import ReplyKind
from mailkit import SAMPLE, XLSX, build_mail

FINN = "finn@finn.test"  # compose test containers (finn-*-testmail)
FINN_PYTEST = "finn-pytest@finn.test"  # in-process Finn of the pytest run (own mailbox)
DOMAIN = "stadtwerke.test"

# GreenMail users (docker-compose.yml, greenmail.users): address → password.
PASSWORDS = {
    "finn@finn.test": "finn-test",
    "finn-pytest@finn.test": "finn-test",
    "cfo@stadtwerke.test": "cfo-test",
    "blocked@stadtwerke.test": "blocked-test",
    "eve@unbekannt.test": "eve-test",
}
CFO = ("Dr. Petra Schulz", "cfo@stadtwerke.test")
BLOCKED = ("Paul Praktikant", "blocked@stadtwerke.test")
EVE = ("Eve Example", "eve@unbekannt.test")

SUBJECT = "Covenant-Prüfung Wirtschaftsplan 2027"
BODY = (
    "Guten Tag Finn,\n\nanbei der Wirtschaftsplan 2027. Bitte prüfen Sie die Covenants unserer "
    "Kreditverträge.\n\nMit freundlichen Grüßen\nPetra Schulz\nKaufmännische Geschäftsführerin\n"
    "Stadtwerke Teststadt GmbH\nTel. +49 421 555 0100\n"
)
PDF = b"%PDF-1.7\n% Wirtschaftsplan als PDF-Export (Testdatei)\n"


@dataclass(frozen=True)
class Att:
    """`source`: "sample", a plan fixture name (`plan_2026.xlsx`, …), "pdf" or "big:<MB>"."""

    filename: str
    source: str = "sample"


@dataclass(frozen=True)
class Step:
    body: str = BODY
    attachments: tuple[Att, ...] = ()
    subject: str | None = None  # None: the case subject ("AW: …" for a follow-up)
    headers: tuple[tuple[str, str], ...] = ()
    in_thread: bool = False  # answer Finn's latest reply (In-Reply-To / References)
    resend: bool = False  # the previous step's mail again, byte for byte (duplicate)
    after_replies: int = 0  # send only once Finn has replied this many times


@dataclass(frozen=True)
class Reply:
    kind: ReplyKind
    phrases: tuple[str, ...] = ()  # must all occur in the plain-text part


@dataclass(frozen=True)
class Case:
    name: str
    title: str  # German, for the gallery
    steps: tuple[Step, ...]
    replies: tuple[Reply, ...]  # exactly these, in this order; () = no reply at all
    codes: tuple[str, ...] = ()  # must all occur (run folder findings / poller log)
    severity: str = ""  # main severity (FAILURES.md), for the overview
    sender: tuple[str, str] = CFO
    subject: str = SUBJECT
    note: str = ""
    tags: tuple[str, ...] = field(default=())


NO_SUBORDINATION = "plan_no_subordination_note.xlsx"


def plan(name: str = "Wirtschaftsplan_2027.xlsx", source: str = "sample") -> Att:
    return Att(name, source)


CASES: tuple[Case, ...] = (
    Case(
        "happy",
        "Musteranfrage mit Beispielplan",
        (Step(attachments=(plan(),)),),
        (Reply("result", ("3,37", "Konsortialkredit", "Rechenweg")),),
        severity="OK",
    ),
    Case(
        "no_attachment_then_plan",
        "Kein Anhang → Rückfrage, dann Plan im selben Thread",
        (
            Step(),
            Step(
                body="Hallo Finn,\n\nanbei der fehlende Plan.\n\nGruß\nPetra Schulz\n",
                attachments=(plan(),),
                in_thread=True,
                after_replies=1,
            ),
        ),
        (
            Reply("ask", ("Bitte den Wirtschaftsplan als Excel-Datei (.xlsx) anhängen",)),
            Reply("result", ("3,37",)),
        ),
        codes=("ATTACHMENT_MISSING",),
        severity="ASK",
    ),
    Case(
        "pdf",
        "PDF statt Excel",
        (Step(attachments=(Att("Wirtschaftsplan_2027.pdf", "pdf"),)),),
        (Reply("ask", ("Wirtschaftsplan_2027.pdf",)),),
        codes=("ATTACHMENT_WRONG_TYPE",),
        severity="ASK",
    ),
    Case(
        "two_xlsx",
        "Zwei Excel-Dateien",
        (Step(attachments=(plan("Variante_A.xlsx"), plan("Variante_B.xlsx"))),),
        (Reply("ask", ("Variante_A.xlsx", "Variante_B.xlsx")),),
        codes=("ATTACHMENT_MULTIPLE",),
        severity="ASK",
    ),
    Case(
        "xls_legacy",
        "Altes Excel-Format .xls",
        (Step(attachments=(plan("Wirtschaftsplan_2027.xls", "plan.xls"),)),),
        (Reply("ask", (".xls",)),),
        codes=("ATTACHMENT_XLS_LEGACY",),
        severity="ASK",
    ),
    Case(
        "password",
        "Passwortgeschützte Datei",
        (Step(attachments=(plan(source="plan_password.xlsx"),)),),
        (Reply("ask", ("passwortgeschützt",)),),
        codes=("XLSX_ENCRYPTED",),
        severity="ASK",
    ),
    Case(
        "corrupt",
        "Beschädigte Datei",
        (Step(attachments=(plan(source="plan_corrupt.xlsx"),)),),
        (Reply("ask", ("beschädigt",)),),
        codes=("XLSX_CORRUPT",),
        severity="ASK",
    ),
    Case(
        "year_mismatch",
        "Plan 2026, angefragt 2027",
        (Step(attachments=(plan("Wirtschaftsplan_2026.xlsx", "plan_2026.xlsx"),)),),
        (Reply("block", ("2027", "2026")),),
        codes=("PLAN_YEAR_MISMATCH",),
        severity="BLOCK",
    ),
    Case(
        "unbalanced",
        "Bilanz nicht ausgeglichen",
        (Step(attachments=(plan(source="plan_unbalanced.xlsx"),)),),
        (Reply("block", ("313.200", "312.900")),),
        codes=("PLAN_UNBALANCED",),
        severity="BLOCK",
    ),
    Case(
        "ref_error",
        "#REF!-Zelle in der GuV",
        (Step(attachments=(plan(source="plan_ref_error.xlsx"),)),),
        (Reply("result", ("#REF!", "nicht berechenbar")),),
        codes=("XLSX_ERROR_CELL",),
        severity="PARTIAL",
    ),
    Case(
        "in_eur",
        "Finanzierung in € statt T€ (umgerechnet, gleiches Ergebnis)",
        (Step(attachments=(plan(source="plan_in_eur.xlsx"),)),),
        (Reply("result", ("3,37", "umgerechnet")),),
        codes=("PLAN_UNITS_CONVERTED",),
        severity="INFO",
    ),
    Case(
        "renamed_labels",
        "Umbenannte Zeilen und Blätter (gleiches Ergebnis)",
        (Step(attachments=(plan(source="plan_renamed_labels.xlsx"),)),),
        (Reply("result", ("3,37",)),),
        severity="OK",
    ),
    Case(
        "no_financing",
        "Kein Darlehensspiegel (nur DSCR nicht berechenbar)",
        (Step(attachments=(plan(source="plan_no_financing.xlsx"),)),),
        (Reply("result", ("nicht berechenbar", "3,37")),),
        codes=("PLAN_ITEM_MISSING",),
        severity="PARTIAL",
    ),
    Case(
        "no_subordination_note",
        "Ohne Rangrücktritts-Hinweis (konservativ 3,57, alternativ 3,37)",
        (Step(attachments=(plan(source="plan_no_subordination_note.xlsx"),)),),
        (Reply("result", ("3,57", "3,37")),),
        codes=("PLAN_FACT_AMBIGUOUS",),
        severity="WARN",
    ),
    Case(
        "blocked_sender",
        "Gesperrter Absender (ALLOWED_SENDERS !blocked@…) → keine Antwort",
        (Step(attachments=(plan(),)),),
        (),
        codes=("GATE_SENDER_REJECTED",),
        severity="REJECT",
        sender=BLOCKED,
    ),
    Case(
        "unknown_sender",
        "Unbekannte Domain → keine Antwort",
        (Step(attachments=(plan(),)),),
        (),
        codes=("GATE_SENDER_REJECTED",),
        severity="REJECT",
        sender=EVE,
    ),
    Case(
        "auto_reply",
        "Abwesenheitsnotiz (Auto-Submitted) → keine Antwort",
        (
            Step(
                body="Ich bin bis zum 16.11. nicht im Büro. In dringenden Fällen wenden Sie sich "
                "bitte an das Sekretariat.\n",
                subject="Automatische Antwort: Covenant-Prüfung",
                headers=(("Auto-Submitted", "auto-replied"), ("X-Autoreply", "yes")),
            ),
        ),
        (),
        codes=("GATE_LOOP",),
        severity="IGNORE",
    ),
    Case(
        "off_topic",
        "Fachfremde Frage → kurze höfliche Antwort",
        (
            Step(
                body="Hallo Finn,\n\nkönnen Sie mir sagen, wann die Kantine heute geöffnet "
                "hat?\n\nDanke\nPetra Schulz\n",
                subject="Kurze Frage",
            ),
        ),
        (Reply("other", ("Andere Anfragen kann ich nicht bearbeiten",)),),
        severity="INFO",
        subject="Kurze Frage",
    ),
    Case(
        "too_large",
        "Nachricht zu groß (Grenze 2 MB im Testsetup)",
        (Step(attachments=(plan(), Att("Anlagen_Scan.pdf", "big:3"))),),
        (Reply("ask", ("zu groß",)),),
        codes=("MAIL_TOO_LARGE",),
        severity="ASK",
    ),
    Case(
        "duplicate",
        "Dieselbe Message-ID zweimal → genau eine Antwort",
        (Step(attachments=(plan(),)), Step(resend=True)),
        (Reply("result", ("3,37",)),),
        codes=("GATE_DUPLICATE",),
        severity="IGNORE",
    ),
    Case(
        "revised_plan",
        "Überarbeiteter Plan im selben Thread → neues Ergebnis",
        (
            Step(attachments=(plan(),)),
            Step(
                body="Hallo Finn,\n\nhier die überarbeitete Fassung (Anmerkungen entfernt). "
                "Bitte erneut prüfen.\n\nGruß\nPetra Schulz\n",
                attachments=(
                    plan("Wirtschaftsplan_2027_v2.xlsx", "plan_no_subordination_note.xlsx"),
                ),
                in_thread=True,
                after_replies=1,
            ),
        ),
        (Reply("result", ("3,37",)), Reply("result", ("3,57", "Wirtschaftsplan_2027_v2.xlsx"))),
        severity="OK",
    ),
)

BY_NAME = {c.name: c for c in CASES}


# ── building the mails ───────────────────────────────────────────────────────


def attachment_bytes(att: Att, plans: dict[str, Path]) -> tuple[str, bytes, str]:
    if att.source == "sample":
        return (att.filename, SAMPLE.read_bytes(), XLSX)
    if att.source == "pdf":
        return (att.filename, PDF, "application/pdf")
    if att.source.startswith("big:"):
        size = int(float(att.source[4:]) * 1024 * 1024)
        # Incompressible-looking but deterministic filler (base64 grows it by a third anyway).
        block = bytes(range(256)) * 4
        data = PDF + block * (size // len(block))
        return (att.filename, data, "application/pdf")
    data = plans[att.source].read_bytes()
    mime = "application/vnd.ms-excel" if att.filename.endswith(".xls") else XLSX
    return (att.filename, data, mime)


def message_id(case: Case, step: int, nonce: str) -> str:
    return f"{case.name.replace('_', '-')}.{step}.{nonce}@{case.sender[1].rpartition('@')[2]}"


def build_step(
    case: Case,
    index: int,
    *,
    to: str,
    nonce: str,
    plans: dict[str, Path],
    finn_reply: tuple[str, list[str]] | None = None,
) -> bytes:
    """The mail for step `index`; `finn_reply` = (Message-ID, References) of Finn's latest reply
    for a step `in_thread`."""
    step = case.steps[index]
    # The case name in brackets keeps the threads apart in Roundcube (same subject otherwise).
    subject = f"{step.subject or case.subject} [{case.name}]"
    in_reply_to: str | None = None
    references: list[str] = []
    if step.in_thread:
        assert finn_reply is not None, f"{case.name}: step {index} needs Finn's reply first"
        in_reply_to = finn_reply[0]
        references = [*finn_reply[1], finn_reply[0]]
        subject = f"AW: {case.subject} [{case.name}]"
    return build_mail(
        subject=subject,
        body=step.body,
        sender=case.sender,
        to=to,
        message_id=message_id(case, index, nonce),
        in_reply_to=in_reply_to,
        references=references,
        attachments=[attachment_bytes(a, plans) for a in step.attachments],
        headers=dict(step.headers),
        date=_now(),
    )


def _now() -> str:
    from datetime import UTC, datetime
    from email.utils import format_datetime

    return format_datetime(datetime.now(UTC))
