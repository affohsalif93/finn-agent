"""Thread identity (ARCHITECTURE §6.3): `In-Reply-To` / `References`, subject as fallback.

Message IDs are handled without angle brackets (see `models/mail.py`).
"""

import hashlib
import re

from finn.models.mail import InboundMessage

_MSGID = re.compile(r"<([^<>\s]+)>")
# Reply / forward prefixes in German and English mail clients, also stacked ("AW: WG: Re:").
_PREFIX = re.compile(r"^\s*((re|aw|antw|wg|fw|fwd|wtr|sv|vs)\s*(\[\d+\])?\s*:\s*)+", re.IGNORECASE)
_REPLY_PREFIX = re.compile(r"^\s*(re|aw|antw)\s*(\[\d+\])?\s*:", re.IGNORECASE)
_MAX_REFERENCES = 20


def message_ids(header: str | None) -> list[str]:
    """All `<id>` tokens of a header, in order, without brackets (deduplicated)."""
    if not header:
        return []
    out: list[str] = []
    for mid in _MSGID.findall(header):
        if mid not in out:
            out.append(mid)
    if not out and header.strip() and " " not in header.strip():
        out.append(header.strip().strip("<>"))  # bare id without brackets (seen in the wild)
    return out


def normalise_subject(subject: str) -> str:
    """Subject without reply / forward prefixes and surplus whitespace."""
    return " ".join(_PREFIX.sub("", subject or "").split())


def is_reply_subject(subject: str) -> bool:
    return bool(_PREFIX.match(subject or ""))


def thread_id(msg: InboundMessage) -> str:
    """Stable id of the thread: the root of `References`, else `In-Reply-To`, else the mail
    itself. Subject fallback for a reply without reference headers."""
    if msg.references:
        return msg.references[0]
    if msg.in_reply_to:
        return msg.in_reply_to
    if is_reply_subject(msg.subject):
        key = f"{normalise_subject(msg.subject).lower()}|{msg.from_addr}"
        return "subject:" + hashlib.sha256(key.encode()).hexdigest()[:24]
    return msg.message_id


def candidate_ids(msg: InboundMessage) -> list[str]:
    """Message IDs this mail refers to, nearest first (In-Reply-To, then References backwards).
    A waiting workflow has the ID of the mail that started it (§7.1)."""
    out: list[str] = []
    for mid in [msg.in_reply_to, *reversed(msg.references)]:
        if mid and mid not in out and mid != msg.message_id:
            out.append(mid)
    return out


def reply_subject(subject: str) -> str:
    """`AW: <subject>`; an existing reply prefix is kept as it is (no "AW: AW:")."""
    clean = " ".join((subject or "").split())
    if not clean:
        return "AW: Ihre Nachricht an Finn"
    return clean if _REPLY_PREFIX.match(clean) else f"AW: {clean}"


def reply_references(msg: InboundMessage) -> list[str]:
    """`References` for a reply: the mail's references + the mail itself (the root is kept when
    the list is shortened, RFC 5322 §3.6.4)."""
    refs = [m for m in msg.references if m != msg.message_id] + [msg.message_id]
    if len(refs) > _MAX_REFERENCES:
        refs = [refs[0], *refs[-(_MAX_REFERENCES - 1) :]]
    return refs
