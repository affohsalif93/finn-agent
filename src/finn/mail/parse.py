"""`.eml` → `InboundMessage` (headers, for the gate) and → body + attachments (for the workflow).

Stdlib `email` with `policy.default`, defensive: a broken part never stops the others, sizes are
limited, the raw `.eml` is always kept (§6.1 rule 6). Attachment kinds are detected by extension
**and** magic bytes (a renamed `.xlsx` is still found, a `.pdf` named `.xlsx` is not trusted).
"""

import hashlib
import html
import re
import zipfile
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email import policy
from email.message import EmailMessage, Message
from email.parser import BytesHeaderParser, BytesParser
from email.utils import getaddresses, parseaddr, parsedate_to_datetime
from pathlib import Path

from finn.mail.thread import message_ids
from finn.models.mail import Attachment, AttachmentKind, InboundMessage

LOOP_HEADERS = ("Auto-Submitted", "Precedence", "X-Autoreply", "X-Autorespond", "X-Finn-Run")
_RECIPIENT_HEADERS = ("To", "Cc", "Delivered-To", "X-Original-To")
_LINK = re.compile(
    r"https?://[^\s<>\"')]*(sharepoint\.com|onedrive\.live\.com|1drv\.ms|dropbox\.com|"
    r"drive\.google\.com|docs\.google\.com|wetransfer\.com|we\.tl|box\.com|icloud\.com)"
    r"[^\s<>\"')]*",
    re.IGNORECASE,
)
_UNSAFE = re.compile(r'[\x00-\x1f<>:"/\\|?*]')
_MAX_FILENAME = 120

OLE_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
ZIP_MAGIC = b"PK\x03\x04"
PDF_MAGIC = b"%PDF"
TNEF_MAGIC = b"\x78\x9f\x3e\x22"


# ── Headers → InboundMessage ─────────────────────────────────────────────────


def inbound_from_bytes(
    raw: bytes,
    *,
    mailbox: str,
    size: int | None = None,
    provider_ref: str | None = None,
    too_large: bool = False,
    keep_raw: bool = True,
) -> InboundMessage:
    """Headers only (the gate must not touch attachments)."""
    msg = BytesHeaderParser(policy=policy.default).parsebytes(raw)
    message_id = _first_id(_header(msg, "Message-ID"))
    if not message_id:  # rare, but must still be stable for dedup
        message_id = f"no-id.{hashlib.sha256(raw).hexdigest()[:32]}@finn.invalid"
    name, addr = parseaddr(_header(msg, "From"))
    recipients: list[str] = []
    for header in _RECIPIENT_HEADERS:
        for _, a in getaddresses(_all(msg, header)):
            if a and a.lower() not in recipients:
                recipients.append(a.lower())
    flags = [f"{h}: {v}" for h in LOOP_HEADERS for v in _all(msg, h)]
    auth = "\n".join(_all(msg, "Authentication-Results")) or None
    reply_to = parseaddr(_header(msg, "Reply-To"))[1].lower() or None
    return InboundMessage(
        message_id=message_id,
        in_reply_to=_first_id(_header(msg, "In-Reply-To")),
        references=message_ids(_header(msg, "References")),
        from_addr=addr.lower(),
        from_name=name.strip() or None,
        to_addrs=recipients,
        reply_to=reply_to if reply_to != addr.lower() else None,
        subject=" ".join(_header(msg, "Subject").split()),
        date=_date(_header(msg, "Date")),
        auth_results=auth,
        auto_submitted=flags,
        mailbox=mailbox.lower(),
        size=size if size is not None else len(raw),
        too_large=too_large,
        provider_ref=provider_ref,
        raw=raw if keep_raw else None,
    )


def _header(msg: Message, name: str) -> str:
    try:
        value = msg.get(name)
    except Exception:  # malformed header: treat as missing
        return ""
    return str(value) if value is not None else ""


def _all(msg: Message, name: str) -> list[str]:
    try:
        return [str(v) for v in msg.get_all(name) or []]
    except Exception:
        return []


def _first_id(header: str) -> str | None:
    ids = message_ids(header)
    return ids[0] if ids else None


def _date(value: str) -> datetime | None:
    if not value:
        return None
    try:
        d = parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        return None
    return d if d.tzinfo else d.replace(tzinfo=UTC)


# ── Body + attachments ───────────────────────────────────────────────────────


@dataclass
class ParsedBody:
    body_text: str
    attachments: list[Attachment] = field(default_factory=list)
    links: list[str] = field(default_factory=list)
    unparseable: bool = False
    defects: list[str] = field(default_factory=list)


def parse_eml(raw_path: Path, attachments_dir: Path, *, max_attachment_bytes: int) -> ParsedBody:
    """Body text (plain preferred, HTML as fallback) and attachments saved to `attachments_dir`."""
    try:
        msg = BytesParser(policy=policy.default).parsebytes(raw_path.read_bytes())
    except Exception as exc:  # parser bugs on broken MIME: keep the raw mail, reply politely
        return ParsedBody("", unparseable=True, defects=[type(exc).__name__])
    assert isinstance(msg, EmailMessage)
    defects = [type(d).__name__ for d in msg.defects]
    body = _body_text(msg)
    attachments: list[Attachment] = []
    used_names: set[str] = set()
    for part in msg.walk():
        if part.is_multipart() or not _is_attachment(part):
            continue
        try:
            payload = part.get_payload(decode=True)
        except Exception:
            defects.append("PayloadDecodeError")
            continue
        if not isinstance(payload, bytes):
            continue
        filename = _filename(part, len(attachments))
        safe = _unique(_safe_name(filename), used_names)
        too_large = len(payload) > max_attachment_bytes
        path = attachments_dir / safe
        if not too_large:
            attachments_dir.mkdir(parents=True, exist_ok=True)
            path.write_bytes(payload)
        attachments.append(
            Attachment(
                path=str(path),
                filename=filename,
                content_type=part.get_content_type(),
                size=len(payload),
                sha256=hashlib.sha256(payload).hexdigest(),
                kind=detect_kind(filename, part.get_content_type(), payload),
                too_large=too_large,
            )
        )
    links = sorted({m.group(0) for m in _LINK.finditer(body)})
    if not body and not attachments and defects:
        return ParsedBody("", unparseable=True, defects=defects)
    return ParsedBody(body, attachments, links, defects=defects)


def _is_attachment(part: Message) -> bool:
    if part.get_content_type() in ("application/ms-tnef", "application/vnd.ms-tnef"):
        return True
    disposition = (part.get_content_disposition() or "").lower()
    if disposition == "attachment":
        return True
    if part.get_filename():
        # inline images with a name (signature logos) are not plan candidates, but keep files
        return part.get_content_maintype() != "image" or disposition != "inline"
    return False


def _filename(part: Message, index: int) -> str:
    try:
        name = part.get_filename()
    except Exception:
        name = None
    if name:
        return " ".join(str(name).split())
    if part.get_content_type() in ("application/ms-tnef", "application/vnd.ms-tnef"):
        return "winmail.dat"
    ext = {"application/pdf": ".pdf", "application/zip": ".zip"}.get(part.get_content_type(), "")
    return f"anhang_{index + 1}{ext}"


def _safe_name(name: str) -> str:
    clean = _UNSAFE.sub("_", name).strip(" .") or "anhang"
    if len(clean) > _MAX_FILENAME:
        stem, dot, ext = clean.rpartition(".")
        clean = (stem[: _MAX_FILENAME - len(ext) - 1] + dot + ext) if dot else clean[:_MAX_FILENAME]
    return clean


def _unique(name: str, used: set[str]) -> str:
    candidate, n = name, 1
    while candidate.lower() in used:
        n += 1
        stem, dot, ext = name.rpartition(".")
        candidate = f"{stem}_{n}.{ext}" if dot else f"{name}_{n}"
    used.add(candidate.lower())
    return candidate


def detect_kind(filename: str, content_type: str, data: bytes) -> AttachmentKind:
    """Extension + magic bytes. Office extensions win (the plan reader reports encrypted /
    corrupt workbooks precisely); a PDF or TNEF payload is recognised under any name."""
    ext = filename.lower().rpartition(".")[2] if "." in filename else ""
    head = data[:8]
    if content_type in ("application/ms-tnef", "application/vnd.ms-tnef") or head.startswith(
        TNEF_MAGIC
    ):
        return "tnef"
    if filename.lower() == "winmail.dat":
        return "tnef"
    if head.startswith(PDF_MAGIC):
        return "pdf"
    if ext in ("xlsx", "xlsm"):
        return "xlsx" if ext == "xlsx" else "xlsm"
    if ext == "xls":
        return "xls"
    if ext == "pdf":
        return "pdf"
    if ext in ("zip", "7z", "rar", "gz", "tgz"):
        return "zip"
    if head.startswith(ZIP_MAGIC):
        names = _zip_names(data)
        if "xl/workbook.xml" in names:
            return "xlsm" if "xl/vbaProject.bin" in names else "xlsx"
        if not ext or ext not in ("docx", "pptx", "odt", "ods", "odp"):
            return "zip"
    if head.startswith(OLE_MAGIC) and not ext:
        return "xls"
    return "other"


def _zip_names(data: bytes) -> set[str]:
    import io

    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            return set(zf.namelist())
    except (zipfile.BadZipFile, OSError, ValueError):
        return set()


def _body_text(msg: EmailMessage) -> str:
    """Plain part; the HTML part if there is no (or only an empty) plain part."""
    for preference in (("plain",), ("html",)):
        try:
            part = msg.get_body(preferencelist=preference)
        except Exception:
            part = None
        if part is None:
            continue
        try:
            content = part.get_content()
        except Exception:  # unknown charset etc.
            payload = part.get_payload(decode=True)
            content = payload.decode("utf-8", "replace") if isinstance(payload, bytes) else ""
        text = str(content)
        if part.get_content_subtype() == "html":
            text = html_to_text(text)
        text = text.replace("\r\n", "\n").strip()
        if text:
            return text
    return ""


_TAG_BREAK = re.compile(r"<\s*(br|/p|/div|/tr|/li|/h\d)\b[^>]*>", re.IGNORECASE)
_TAG = re.compile(r"<[^>]+>")
_HIDDEN = re.compile(r"<(style|script|head)\b.*?</\1\s*>", re.IGNORECASE | re.DOTALL)
_BLOCKQUOTE = re.compile(r"<blockquote\b.*?</blockquote\s*>", re.IGNORECASE | re.DOTALL)


def html_to_text(markup: str) -> str:
    """Rough HTML → text for HTML-only mails; quoted blocks are dropped."""
    text = _HIDDEN.sub("", markup)
    text = _BLOCKQUOTE.sub("", text)
    text = _TAG_BREAK.sub("\n", text)
    text = html.unescape(_TAG.sub("", text))
    lines = [" ".join(line.split()) for line in text.split("\n")]
    return "\n".join(line for line in lines).strip()
