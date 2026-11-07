"""Gate (ARCHITECTURE §6.1, D12, D20): who may enter. A plain function, headers only.

`check(msg, settings) -> Accept | Reject`. Rejected mail is a security event in the log
(`access_rejected`, `auth_failed`, `loop_ignored`): no reply, attachments never parsed, no workflow.

Sender rules (`ALLOWED_SENDERS`, comma-separated): `*` (anyone, demo mode) · `cfo@x.de` (exact) ·
`@x.de` (domain) · `@*.x.de` (any subdomain of x.de, not x.de itself) · `!rule` (deny).
Precedence: exact deny → exact allow → most specific matching pattern (a deny wins a tie) → reject.
"""

import re
from dataclasses import dataclass, field
from typing import Literal

from finn.config import Settings
from finn.errors import ErrorCode
from finn.models.mail import InboundMessage

SecurityEvent = Literal["access_rejected", "auth_failed", "loop_ignored"]
DEFAULT_TENANT = "default"
FINN_HEADER = "X-Finn-Run"  # set on every mail Finn sends (loop detection)


@dataclass(frozen=True)
class AuthSummary:
    spf: str | None = None
    dkim: str | None = None
    dmarc: str | None = None


@dataclass(frozen=True)
class Accept:
    tenant: str
    auth: AuthSummary = field(default_factory=AuthSummary)


@dataclass(frozen=True)
class Reject:
    code: ErrorCode
    event: SecurityEvent
    reason: str
    auth: AuthSummary = field(default_factory=AuthSummary)


# ── Sender rules ─────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Rule:
    deny: bool
    kind: Literal["any", "exact", "domain", "subdomains"]
    value: str  # address or domain, lower-case

    @property
    def specificity(self) -> int:
        """Higher = more specific. Longer domains beat shorter ones; `@x.de` beats `@*.x.de`."""
        if self.kind == "any":
            return 0
        labels = self.value.count(".") + 1
        return 2 * labels + (1 if self.kind == "domain" else 0)

    def matches(self, addr: str) -> bool:
        domain = addr.rpartition("@")[2]
        if self.kind == "any":
            return True
        if self.kind == "exact":
            return addr == self.value
        if self.kind == "domain":
            return domain == self.value
        return domain.endswith("." + self.value)


def parse_rules(text: str) -> list[Rule]:
    rules: list[Rule] = []
    for raw in (text or "").split(","):
        token = raw.strip().lower()
        deny = token.startswith("!")
        token = token.lstrip("!").strip()
        if not token:
            continue
        if token == "*":
            rules.append(Rule(deny, "any", "*"))
        elif token.startswith("@*."):
            rules.append(Rule(deny, "subdomains", token[3:]))
        elif token.startswith("@"):
            rules.append(Rule(deny, "domain", token[1:]))
        elif "@" in token:
            rules.append(Rule(deny, "exact", token))
        else:  # a bare domain is read as "@domain"
            rules.append(Rule(deny, "domain", token))
    return rules


def sender_allowed(addr: str, rules: list[Rule]) -> bool:
    addr = addr.strip().lower()
    exact = [r for r in rules if r.kind == "exact" and r.matches(addr)]
    if any(r.deny for r in exact):
        return False
    if exact:
        return True
    patterns = [r for r in rules if r.kind != "exact" and r.matches(addr)]
    if not patterns:
        return False
    best = max(r.specificity for r in patterns)
    return not any(r.deny for r in patterns if r.specificity == best)


# ── Authentication-Results ───────────────────────────────────────────────────

_AUTH = {
    name: re.compile(rf"\b{name}\s*=\s*([a-z]+)", re.IGNORECASE)
    for name in ("spf", "dkim", "dmarc")
}


def parse_auth(header: str | None) -> AuthSummary:
    if not header:
        return AuthSummary()

    def get(name: str) -> str | None:
        m = _AUTH[name].search(header)
        return m.group(1).lower() if m else None

    return AuthSummary(spf=get("spf"), dkim=get("dkim"), dmarc=get("dmarc"))


# ── Loops / auto replies ─────────────────────────────────────────────────────

_BULK = {"bulk", "junk", "list", "auto_reply", "auto-reply"}


def loop_reason(msg: InboundMessage, settings: Settings) -> str | None:
    for flag in msg.auto_submitted:
        name, _, value = flag.partition(":")
        name, value = name.strip().lower(), value.strip().lower()
        if name == "auto-submitted" and value and value != "no":
            return f"Auto-Submitted: {value}"
        if name == "precedence" and value in _BULK:
            return f"Precedence: {value}"
        if name in {"x-autoreply", "x-autorespond"}:
            return name
        if name == FINN_HEADER.lower():
            return "eigene Nachricht (X-Finn-Run)"
    sender = msg.from_addr.lower()
    if settings.finn_address and sender == settings.finn_address.lower():
        return "Absender ist Finns eigene Adresse"
    if sender.startswith(("mailer-daemon@", "postmaster@")):
        return "Unzustellbarkeitsmeldung"
    return None


# ── Gate ─────────────────────────────────────────────────────────────────────


def check(msg: InboundMessage, settings: Settings) -> Accept | Reject:
    """Steps (§6.1): tenant · licence · loop · recipient · DMARC · sender rules."""
    auth = parse_auth(msg.auth_results)
    tenant = _tenant(msg, settings)  # 1. tenant by receiving mailbox (stub: one tenant)
    if not _licence_active(tenant):  # 2. licence (stub: always active)
        return Reject(ErrorCode.GATE_SENDER_REJECTED, "access_rejected", "Lizenz inaktiv", auth)
    reason = loop_reason(msg, settings)  # 3. loop / auto reply / own address
    if reason:
        return Reject(ErrorCode.GATE_LOOP, "loop_ignored", reason, auth)
    finn = settings.finn_address.strip().lower()
    if finn and finn not in msg.to_addrs:  # recipient must be Finn's address
        return Reject(
            ErrorCode.GATE_NOT_ADDRESSED, "access_rejected", f"nicht an {finn} adressiert", auth
        )
    if settings.require_dmarc_pass and auth.dmarc != "pass":  # 4. DMARC (logged anyway)
        return Reject(ErrorCode.GATE_AUTH_FAILED, "auth_failed", f"dmarc={auth.dmarc}", auth)
    if not sender_allowed(msg.from_addr, parse_rules(settings.allowed_senders)):  # 5. sender
        return Reject(
            ErrorCode.GATE_SENDER_REJECTED, "access_rejected", "Absender nicht freigegeben", auth
        )
    return Accept(tenant, auth)  # 6. duplicates: Temporal workflow ID = Message-ID (poller)


def _tenant(msg: InboundMessage, settings: Settings) -> str:
    return DEFAULT_TENANT


def _licence_active(tenant: str) -> bool:
    return True
