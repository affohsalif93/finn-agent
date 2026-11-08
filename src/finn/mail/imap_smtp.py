"""`ImapSmtpConnector`: IMAP for reading, SMTP for sending (stdlib `imaplib` / `smtplib`).

Gmail specifics (verified Nov 2026), handled generically where possible:
- Finn reads **only** `IMAP_FOLDER` (a Gmail label filled by a filter on the plus alias); never
  INBOX. A missing folder raises `FolderMissingError` (poller: log, retry next cycle).
- Processed = **moved** to `IMAP_PROCESSED_FOLDER` (default `<folder>/Verarbeitet`, created on
  first use) with `UID MOVE` (RFC 6851; COPY + \\Deleted + EXPUNGE without MOVE). On Gmail this
  swaps the label; the mail stays in "All Mail". Custom IMAP keywords are not reliable on Gmail,
  folders are, and the state stays visible to a human in the web UI.
- The Sent folder is found by its `\\Sent` SPECIAL-USE flag (the name is localised), else by
  `IMAP_SENT_FOLDER` or a common name; `was_sent` searches it by Message-ID (`X-GM-RAW
  rfc822msgid:` on Gmail, `HEADER Message-ID` elsewhere).
- Gmail's SMTP files every sent mail in "Gesendet" itself; other servers (GreenMail, many
  hosters) don't, so Finn APPENDs its reply there (`IMAP_APPEND_SENT`, auto = not on Gmail);
  otherwise `was_sent` could never find it.
- Transport: IMAPS and SMTPS / STARTTLS by default; `IMAP_SSL=false` / `SMTP_SECURITY=plain`
  only for the local test server (compose profile `testmail`).
- Bodies are fetched with `BODY.PEEK[]` (no \\Seen flag); mails above the size limit only with
  their header (MAIL_TOO_LARGE).
"""

import contextlib
import imaplib
import re
import smtplib
import ssl
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from email.message import EmailMessage

import structlog

from finn.config import Settings
from finn.mail.connector import (
    FolderMissingError,
    MailAuthError,
    MailConnectionError,
    build_mime,
)
from finn.mail.parse import inbound_from_bytes
from finn.models.mail import InboundMessage, ReplyDraft

log = structlog.get_logger(__name__)

_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
_LIST = re.compile(rb'^\((?P<flags>[^)]*)\)\s+(?P<delim>"[^"]*"|NIL)\s+(?P<name>.+)$')
_FETCH_SIZE = re.compile(rb"RFC822\.SIZE (\d+)")
_TIMEOUT = 60
# Sent folder names for servers without SPECIAL-USE (RFC 6154), in order of preference.
SENT_NAMES = ("Sent", "Sent Items", "Sent Messages", "Gesendet", "Gesendete Elemente", "INBOX.Sent")


def imap_date(d: datetime) -> str:
    """IMAP SEARCH date (`07-Nov-2026`), independent of the process locale."""
    return f"{d.day:02d}-{_MONTHS[d.month - 1]}-{d.year}"


def quote_mailbox(name: str) -> str:
    """Quoted mailbox name in IMAP modified UTF-7 (RFC 3501 §5.1.3)."""
    encoded = _utf7_encode(name)
    return '"' + encoded.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _utf7_encode(name: str) -> str:
    import base64

    out: list[str] = []
    buf: list[str] = []

    def flush() -> None:
        if buf:
            raw = "".join(buf).encode("utf-16-be")
            out.append("&" + base64.b64encode(raw).decode().rstrip("=").replace("/", ",") + "-")
            buf.clear()

    for ch in name:
        if 0x20 <= ord(ch) <= 0x7E:
            flush()
            out.append("&-" if ch == "&" else ch)
        else:
            buf.append(ch)
    flush()
    return "".join(out)


class ImapSmtpConnector:
    """One IMAP connection per instance, opened lazily and reused (the poller keeps one
    instance; each activity creates its own). Not thread-safe."""

    def __init__(self, settings: Settings) -> None:
        self.s = settings
        self._imap: imaplib.IMAP4 | None = None
        self._caps: set[str] = set()
        self._sent_folder: str | None = None

    # ── connection ──────────────────────────────────────────────────────────

    def _conn(self) -> imaplib.IMAP4:
        if self._imap is not None:
            try:
                self._imap.noop()
                return self._imap
            except (imaplib.IMAP4.error, OSError):
                self.close()
        if not self.s.imap_user or self.s.imap_password is None:
            raise MailAuthError("IMAP_USER / IMAP_PASSWORD not set")
        conn: imaplib.IMAP4
        try:
            if self.s.imap_ssl:
                conn = imaplib.IMAP4_SSL(self.s.imap_host, self.s.imap_port, timeout=_TIMEOUT)
            else:  # local test server only (GreenMail)
                conn = imaplib.IMAP4(self.s.imap_host, self.s.imap_port, timeout=_TIMEOUT)
        except (OSError, imaplib.IMAP4.error) as exc:
            raise MailConnectionError(f"IMAP connect {self.s.imap_host}: {exc}") from exc
        try:
            conn.login(self.s.imap_user, self.s.imap_password.get_secret_value())
        except imaplib.IMAP4.error as exc:
            text = str(exc)
            if re.search(r"AUTHENTICATIONFAILED|Invalid credentials|LOGIN failed", text, re.I):
                raise MailAuthError(f"IMAP login rejected for {self.s.imap_host}") from exc
            raise MailConnectionError(f"IMAP login: {text}") from exc
        except OSError as exc:
            raise MailConnectionError(f"IMAP login: {exc}") from exc
        typ, data = conn.capability()
        if typ == "OK" and data and data[0]:
            self._caps = {c.upper() for c in data[0].decode().split()}
        self._imap = conn
        return conn

    def close(self) -> None:
        if self._imap is not None:
            with contextlib.suppress(imaplib.IMAP4.error, OSError):
                self._imap.logout()
        self._imap = None

    @contextmanager
    def _errors(self, what: str) -> Iterator[None]:
        try:
            yield
        except (FolderMissingError, MailAuthError, MailConnectionError):
            raise
        except (TimeoutError, imaplib.IMAP4.abort, OSError, ssl.SSLError) as exc:
            self.close()
            raise MailConnectionError(f"{what}: {exc}") from exc
        except imaplib.IMAP4.error as exc:
            raise MailConnectionError(f"{what}: {exc}") from exc

    def _select(self, folder: str, *, readonly: bool = False) -> int:
        """SELECT; returns UIDVALIDITY. Missing folder → FolderMissingError."""
        conn = self._conn()
        typ, data = conn.select(quote_mailbox(folder), readonly=readonly)
        if typ != "OK":
            detail = b" ".join(d for d in data if isinstance(d, bytes)).decode(errors="replace")
            if re.search(r"NONEXISTENT|Unknown Mailbox|doesn't exist|not exist", detail, re.I):
                raise FolderMissingError(folder)
            raise MailConnectionError(f"SELECT {folder}: {detail}")
        _, validity = conn.response("UIDVALIDITY")
        try:
            return int(validity[0]) if validity and validity[0] else 0
        except (TypeError, ValueError):
            return 0

    def _folders(self) -> list[tuple[set[str], str]]:
        """(flags, raw name as the server spells it) for every folder."""
        typ, data = self._conn().list()
        out: list[tuple[set[str], str]] = []
        if typ != "OK":
            return out
        for item in data:
            line = item[0] + item[1] if isinstance(item, tuple) else item
            if not isinstance(line, bytes):
                continue
            m = _LIST.match(line.strip())
            if not m:
                continue
            flags = {f.decode().lower() for f in m.group("flags").split()}
            name = m.group("name").decode(errors="replace").strip()
            if name.startswith('"') and name.endswith('"'):
                name = name[1:-1].replace('\\"', '"').replace("\\\\", "\\")
            out.append((flags, name))
        return out

    # ── MailConnector ───────────────────────────────────────────────────────

    def fetch_new(self, since: datetime) -> list[InboundMessage]:
        limit = int(self.s.max_mail_mb * 1024 * 1024)
        with self._errors("fetch_new"):
            validity = self._select(self.s.imap_folder)
            conn = self._conn()
            typ, data = conn.uid("SEARCH", "UNDELETED", "SINCE", imap_date(since))
            if typ != "OK":
                raise MailConnectionError(f"SEARCH: {data!r}")
            uids = data[0].split() if data and data[0] else []
            messages: list[InboundMessage] = []
            for uid in uids:
                msg = self._fetch_one(conn, uid.decode(), validity, limit)
                if msg is not None:
                    messages.append(msg)
            return messages

    def _fetch_one(
        self, conn: imaplib.IMAP4, uid: str, validity: int, limit: int
    ) -> InboundMessage | None:
        typ, data = conn.uid("FETCH", uid, "(RFC822.SIZE)")
        size = 0
        for item in data or []:
            line = item[0] if isinstance(item, tuple) else item
            if isinstance(line, bytes) and (m := _FETCH_SIZE.search(line)):
                size = int(m.group(1))
        too_large = size > limit
        section = "BODY.PEEK[HEADER]" if too_large else "BODY.PEEK[]"
        typ, data = conn.uid("FETCH", uid, f"({section})")
        raw = next((i[1] for i in data or [] if isinstance(i, tuple) and len(i) > 1), None)
        if typ != "OK" or not isinstance(raw, bytes):
            log.warning("imap_fetch_empty", uid=uid)  # expunged meanwhile
            return None
        return inbound_from_bytes(
            raw,
            mailbox=self.s.finn_address,
            size=size or len(raw),
            provider_ref=f"{validity}:{uid}",
            too_large=too_large,
        )

    def mark_processed(self, msg: InboundMessage) -> None:
        if not msg.provider_ref:
            return
        validity_text, _, uid = msg.provider_ref.partition(":")
        target = self.s.processed_folder
        with self._errors("mark_processed"):
            self._ensure_folder(target)
            validity = self._select(self.s.imap_folder)
            if str(validity) != validity_text:
                log.warning("imap_uidvalidity_changed", folder=self.s.imap_folder)
                return  # the next fetch sees the mail again; Temporal dedups it
            conn = self._conn()
            if "MOVE" in self._caps:
                typ, data = conn.uid("MOVE", uid, quote_mailbox(target))
            else:
                typ, data = conn.uid("COPY", uid, quote_mailbox(target))
                if typ == "OK":
                    conn.uid("STORE", uid, "+FLAGS.SILENT", r"(\Deleted)")
                    conn.expunge()
            if typ != "OK":
                raise MailConnectionError(f"MOVE {uid} → {target}: {data!r}")

    def _ensure_folder(self, name: str) -> None:
        if any(n == _utf7_encode(name) for _, n in self._folders()):
            return
        typ, data = self._conn().create(quote_mailbox(name))
        if typ != "OK" and b"exists" not in b" ".join(d for d in data if isinstance(d, bytes)):
            raise MailConnectionError(f"CREATE {name}: {data!r}")
        log.info("imap_folder_created", folder=name)

    def sent_folder(self) -> str | None:
        """Raw name of the Sent folder (see `pick_sent_folder`); None if there is none."""
        if self._sent_folder is None:
            with self._errors("list"):
                self._sent_folder = pick_sent_folder(self._folders(), self.s.imap_sent_folder)
        return self._sent_folder

    @property
    def is_gmail(self) -> bool:
        return "X-GM-EXT-1" in self._caps

    def append_sent_enabled(self) -> bool:
        """IMAP_APPEND_SENT: auto = APPEND unless the server is Gmail (files sent mail itself)."""
        if self.s.imap_append_sent == "auto":
            with self._errors("capability"):
                self._conn()
            return not self.is_gmail
        return self.s.imap_append_sent == "true"

    def _append_sent(self, mime: EmailMessage) -> None:
        """Copy the sent reply into the Sent folder (created if missing). A failure is logged,
        not raised: the mail is out, and the local sent marker still prevents a resend."""
        try:
            with self._errors("append_sent"):
                folder = self.sent_folder()
                if folder is None:
                    name = self.s.imap_sent_folder or SENT_NAMES[0]
                    self._ensure_folder(name)
                    folder = self._sent_folder = _utf7_encode(name)
                typ, data = self._conn().append(
                    f'"{folder}"', r"(\Seen)", imaplib.Time2Internaldate(time.time()),
                    mime.as_bytes(),
                )  # fmt: skip
                if typ != "OK":
                    raise MailConnectionError(f"APPEND {folder}: {data!r}")
        except (MailConnectionError, MailAuthError) as exc:
            log.warning("imap_append_sent_failed", error=str(exc))

    def was_sent(self, message_id: str) -> bool:
        mid = message_id.strip("<>")
        folder = self.sent_folder()
        if folder is None:
            log.warning("imap_no_sent_folder")
            return False
        with self._errors("was_sent"):
            conn = self._conn()
            typ, data = conn.select(f'"{folder}"', readonly=True)  # raw name from LIST
            if typ != "OK":
                raise MailConnectionError(f"SELECT {folder}: {data!r}")
            if self.is_gmail:
                typ, data = conn.uid("SEARCH", "X-GM-RAW", f'"rfc822msgid:<{mid}>"')
            else:
                typ, data = conn.uid("SEARCH", "HEADER", "Message-ID", f'"<{mid}>"')
            return typ == "OK" and bool(data and data[0] and data[0].split())

    def send_reply(self, reply: ReplyDraft) -> None:
        if not self.s.smtp_user or self.s.smtp_password is None:
            raise MailAuthError("SMTP_USER / SMTP_PASSWORD not set")
        mime = build_mime(
            reply,
            from_addr=self.s.smtp_user,
            from_name=self.s.smtp_from_name,
            date=datetime.now(UTC),
        )
        password = self.s.smtp_password.get_secret_value()
        security = self.s.smtp_security
        if security == "auto":
            security = "ssl" if self.s.smtp_port == 465 else "starttls"
        try:
            smtp: smtplib.SMTP
            if security == "ssl":
                smtp = smtplib.SMTP_SSL(self.s.smtp_host, self.s.smtp_port, timeout=_TIMEOUT)
            else:
                smtp = smtplib.SMTP(self.s.smtp_host, self.s.smtp_port, timeout=_TIMEOUT)
                if security == "starttls":
                    smtp.starttls(context=ssl.create_default_context())
            with smtp:
                smtp.login(self.s.smtp_user, password)
                smtp.send_message(mime, from_addr=self.s.smtp_user, to_addrs=[reply.to])
        except smtplib.SMTPAuthenticationError as exc:
            raise MailAuthError(f"SMTP login rejected for {self.s.smtp_host}") from exc
        except (smtplib.SMTPException, OSError) as exc:
            raise MailConnectionError(f"SMTP send: {type(exc).__name__}: {exc}") from exc
        log.info("mail_sent", message_id=reply.message_id, kind=reply.kind)
        if self.append_sent_enabled():
            self._append_sent(mime)


def pick_sent_folder(folders: list[tuple[set[str], str]], configured: str = "") -> str | None:
    """The `\\Sent` SPECIAL-USE folder (localised names on Gmail), else IMAP_SENT_FOLDER, else a
    common name (case-insensitive). `folders` = (lower-case flags, raw name) as from LIST."""
    flagged = next((name for flags, name in folders if "\\sent" in flags), None)
    if flagged is not None:
        return flagged
    names = {name.lower(): name for _, name in folders}
    wanted = [_utf7_encode(configured)] if configured else []
    return next((names[c.lower()] for c in [*wanted, *SENT_NAMES] if c.lower() in names), None)
