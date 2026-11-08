"""Mail connector interface (D11, D19): one implementation per provider; `build_mime` turns a
`ReplyDraft` into the MIME message every connector sends."""

from datetime import datetime
from email.headerregistry import Address
from email.message import EmailMessage
from email.utils import format_datetime
from typing import Protocol

from finn.mail.gate import FINN_HEADER
from finn.models.mail import InboundMessage, ReplyDraft


class MailError(Exception):
    """Base class for connector failures."""


class MailAuthError(MailError):
    """Login rejected: loud, not retried in a loop (INFRA_MAILBOX_AUTH)."""


class MailConnectionError(MailError):
    """Server unreachable / connection dropped / throttled: retry next cycle (INFRA_IMAP_DOWN)."""


class FolderMissingError(MailError):
    """The configured IMAP folder (Gmail label) does not exist (INFRA_IMAP_FOLDER_MISSING)."""


class MailConnector(Protocol):
    def fetch_new(self, since: datetime) -> list[InboundMessage]:
        """Mail in Finn's folder not yet processed, received since `since` (with raw bytes)."""
        ...

    def mark_processed(self, msg: InboundMessage) -> None:
        """Move / tag the mail so `fetch_new` no longer returns it (after workflow start)."""
        ...

    def send_reply(self, reply: ReplyDraft) -> None:
        """Send in the thread (`In-Reply-To` / `References` from the draft)."""
        ...

    def was_sent(self, message_id: str) -> bool:
        """True if a mail with this Message-ID is already in the Sent folder (idempotent send)."""
        ...


def _bracket(mid: str) -> str:
    return mid if mid.startswith("<") else f"<{mid}>"


def build_mime(
    reply: ReplyDraft, *, from_addr: str, from_name: str, date: datetime
) -> EmailMessage:
    """multipart/alternative (plain text + HTML), reply headers, loop protection headers."""
    msg = EmailMessage()
    msg["From"] = Address(display_name=from_name, addr_spec=from_addr)
    msg["To"] = (
        Address(display_name=reply.to_name, addr_spec=reply.to) if reply.to_name else reply.to
    )
    msg["Subject"] = reply.subject
    msg["Date"] = format_datetime(date)
    msg["Message-ID"] = _bracket(reply.message_id)
    if reply.in_reply_to:
        msg["In-Reply-To"] = _bracket(reply.in_reply_to)
    if reply.references:
        msg["References"] = " ".join(_bracket(r) for r in reply.references)
    # RFC 3834: automatic reply; suppress out-of-office answers (Exchange) → no mail loops.
    msg["Auto-Submitted"] = "auto-replied"
    msg["X-Auto-Response-Suppress"] = "All"
    msg[FINN_HEADER] = reply.run_id
    msg["Content-Language"] = "de-DE"
    msg.set_content(reply.text, subtype="plain", charset="utf-8")
    msg.add_alternative(reply.html, subtype="html", charset="utf-8")
    return msg
