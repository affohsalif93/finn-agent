"""Test helpers: build `.eml` fixtures in code (nothing binary is committed) and test settings.

The fixture set mirrors real clients (Outlook, Gmail, Apple Mail): plain request with the sample
plan, no attachment, two workbooks, PDF instead of Excel, auto-reply, unknown sender, forwarded
mail, umlaut file name, TNEF, zip, link only, follow-up in the thread.
"""

from collections.abc import Callable, Iterable
from datetime import datetime
from email.headerregistry import Address
from email.message import EmailMessage
from email.utils import format_datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from finn.config import Settings
from finn.llm import client
from finn.mail.connector import MailConnectionError
from finn.mail.parse import inbound_from_bytes
from finn.models.contract import ContractProfile
from finn.models.mail import InboundMessage, ReplyDraft

ROOT = Path(__file__).resolve().parents[1]
MATERIAL = ROOT / "material"
SAMPLE = MATERIAL / "Wirtschaftsplan_2027.xlsx"
GOLD_PROFILES = ROOT / "tests" / "fixtures" / "profiles_gold"
TIGHT_RATIO = Settings.model_fields["tight_headroom_ratio"].default


def profile_by_id(profiles: list[ContractProfile], cid: str) -> ContractProfile:
    return next(p for p in profiles if p.contract_id == cid)


FINN = "finn.box+finn@example.com"
ACCOUNT = "finn.box@example.com"
CFO = ("Dr. Max Mustermann", "max.mustermann@stadtwerke-musterstadt.de")
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
DATE = "Mon, 02 Nov 2026 09:15:00 +0100"


def settings(tmp_path: Path, **overrides: Any) -> Settings:
    """Settings that never read `.env` (no real keys in tests)."""
    values: dict[str, Any] = {
        "finn_address": FINN,
        "imap_user": ACCOUNT,
        "smtp_user": ACCOUNT,
        "allowed_senders": "*",
        "runs_dir": tmp_path / "runs",
        "profiles_dir": GOLD_PROFILES,
        "contracts_dir": MATERIAL,
        "openai_api_key": None,
        "reply_llm_polish": False,
        "max_mail_mb": 25,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)  # type: ignore[call-arg]


def build_mail(
    *,
    subject: str = "Covenant-Prüfung Wirtschaftsplan 2027",
    body: str = "Guten Tag Finn,\n\nanbei der Wirtschaftsplan 2027. Bitte prüfen Sie die "
    "Covenants.\n\nMit freundlichen Grüßen\nMax Mustermann\nTel. +49 421 1234567\n",
    sender: tuple[str, str] = CFO,
    to: str = FINN,
    message_id: str = "req-0001@stadtwerke-musterstadt.de",
    in_reply_to: str | None = None,
    references: Iterable[str] = (),
    attachments: Iterable[tuple[str, bytes, str]] = (),
    headers: dict[str, str] | None = None,
    html: str | None = None,
    date: str = DATE,
) -> bytes:
    msg = EmailMessage()
    msg["From"] = Address(display_name=sender[0], addr_spec=sender[1])
    msg["To"] = to
    msg["Subject"] = subject
    msg["Date"] = date
    msg["Message-ID"] = f"<{message_id}>"
    if in_reply_to:
        msg["In-Reply-To"] = f"<{in_reply_to}>"
    refs = list(references)
    if refs:
        msg["References"] = " ".join(f"<{r}>" for r in refs)
    for k, v in (headers or {}).items():
        msg[k] = v
    msg.set_content(body)
    if html is not None:
        msg.add_alternative(html, subtype="html")
    for filename, data, mime in attachments:
        maintype, _, subtype = mime.partition("/")
        msg.add_attachment(data, maintype=maintype, subtype=subtype, filename=filename)
    return msg.as_bytes()


def plan_attachment(name: str = "Wirtschaftsplan_2027.xlsx") -> tuple[str, bytes, str]:
    return (name, SAMPLE.read_bytes(), XLSX)


def fixture_mails() -> dict[str, bytes]:
    """All named fixtures (FAILURES.md fixture names without the `mail_` prefix)."""
    pdf = b"%PDF-1.7\n% fake plan\n"
    return {
        "request": build_mail(attachments=[plan_attachment()]),
        "no_attachment": build_mail(message_id="req-0002@stadtwerke-musterstadt.de"),
        "two_xlsx": build_mail(
            message_id="req-0003@stadtwerke-musterstadt.de",
            attachments=[plan_attachment("Variante_A.xlsx"), plan_attachment("Variante_B.xlsx")],
        ),
        "pdf": build_mail(
            message_id="req-0004@stadtwerke-musterstadt.de",
            attachments=[("Wirtschaftsplan_2027.pdf", pdf, "application/pdf")],
        ),
        "auto_reply": build_mail(
            subject="Automatische Antwort: Covenant-Prüfung",
            body="Ich bin bis 10.11. nicht im Büro.",
            message_id="ooo-0001@stadtwerke-musterstadt.de",
            headers={"Auto-Submitted": "auto-replied", "X-Autoreply": "yes"},
        ),
        "unknown_sender": build_mail(
            sender=("Eve Example", "eve@unbekannt.example"),
            message_id="req-0005@unbekannt.example",
            attachments=[plan_attachment()],
        ),
        "forwarded": build_mail(
            subject="WG: Wirtschaftsplan 2027",
            body="Finn, bitte prüfen.\n\n---------- Forwarded message ---------\nVon: Controlling "
            "<controlling@stadtwerke-musterstadt.de>\nDatum: Fr., 30. Okt. 2026\nBetreff: "
            "Wirtschaftsplan 2027\n\nAnbei der finale Entwurf.\n",
            message_id="req-0006@stadtwerke-musterstadt.de",
            attachments=[plan_attachment()],
        ),
        "umlaut_filename": build_mail(
            message_id="req-0007@stadtwerke-musterstadt.de",
            attachments=[plan_attachment("Wirtschaftsplan_2027_Entwurf_Prüfung.xlsx")],
        ),
        "tnef": build_mail(
            message_id="req-0008@stadtwerke-musterstadt.de",
            attachments=[("winmail.dat", b"\x78\x9f\x3e\x22" + b"\0" * 20, "application/ms-tnef")],
        ),
        "zip": build_mail(
            message_id="req-0009@stadtwerke-musterstadt.de",
            attachments=[("Plan.zip", b"PK\x03\x04" + b"\0" * 30, "application/zip")],
        ),
        "link": build_mail(
            message_id="req-0010@stadtwerke-musterstadt.de",
            body="Hier der Plan: https://stadtwerke.sharepoint.com/sites/fin/Plan_2027.xlsx\n",
        ),
    }


def write_fixtures(directory: Path) -> dict[str, Path]:
    directory.mkdir(parents=True, exist_ok=True)
    out: dict[str, Path] = {}
    for name, data in fixture_mails().items():
        path = directory / f"mail_{name}.eml"
        path.write_bytes(data)
        out[name] = path
    return out


def llm_unavailable(**_: Any) -> BaseModel:
    raise client.LLMUnavailable("mocked: no LLM in tests")


def llm_returning(answers: dict[str, BaseModel]) -> Callable[..., BaseModel]:
    """Fake `complete_structured`: the answer for `purpose`, else LLMUnavailable."""

    def fake(**kwargs: Any) -> BaseModel:
        purpose = kwargs["purpose"]
        if purpose not in answers:
            raise client.LLMUnavailable(f"mocked: no answer for {purpose}")
        answer = answers[purpose]
        return answer(kwargs) if callable(answer) else answer  # type: ignore[operator]

    return fake


class FakeConnector:
    """In-memory mailbox with the `MailConnector` protocol (thread-safe enough for tests)."""

    def __init__(self, mails: Iterable[bytes] = ()) -> None:
        import threading

        self.lock = threading.Lock()
        self.folder: dict[str, bytes] = {}
        self.processed: list[str] = []
        self.sent: list[ReplyDraft] = []
        self.sent_folder: set[str] = set()
        self.fail_sends = 0  # raise MailConnectionError this many times first
        self.send_attempts = 0
        for raw in mails:
            self.deliver(raw)

    def deliver(self, raw: bytes) -> None:
        with self.lock:
            self.folder[str(len(self.folder) + 1)] = raw

    def fetch_new(self, since: datetime) -> list[InboundMessage]:
        with self.lock:
            return [
                inbound_from_bytes(raw, mailbox=FINN, provider_ref=f"1:{uid}")
                for uid, raw in self.folder.items()
                if f"1:{uid}" not in self.processed
            ]

    def mark_processed(self, msg: InboundMessage) -> None:
        with self.lock:
            if msg.provider_ref and msg.provider_ref not in self.processed:
                self.processed.append(msg.provider_ref)

    def send_reply(self, reply: ReplyDraft) -> None:
        with self.lock:
            self.send_attempts += 1
            if self.fail_sends > 0:
                self.fail_sends -= 1
                raise MailConnectionError("SMTP down (test)")
            self.sent.append(reply)
            self.sent_folder.add(reply.message_id)

    def was_sent(self, message_id: str) -> bool:
        with self.lock:
            return message_id in self.sent_folder


__all__ = ["DATE", "format_datetime"]
