"""Gate (§6.1): loops, recipient, DMARC, sender rules with precedence."""

from pathlib import Path

import pytest

from finn.errors import ErrorCode
from finn.mail import gate
from finn.mail.gate import parse_rules, sender_allowed
from finn.mail.parse import inbound_from_bytes
from mailkit import ACCOUNT, FINN, build_mail, fixture_mails, settings


def _msg(raw: bytes):  # type: ignore[no-untyped-def]
    return inbound_from_bytes(raw, mailbox=FINN)


@pytest.mark.parametrize(
    ("rules", "addr", "allowed"),
    [
        ("*", "anyone@example.org", True),
        ("", "cfo@stadtwerke-x.de", False),
        ("cfo@stadtwerke-x.de", "cfo@stadtwerke-x.de", True),
        ("cfo@stadtwerke-x.de", "CFO@Stadtwerke-X.de", True),
        ("cfo@stadtwerke-x.de", "ceo@stadtwerke-x.de", False),
        ("@stadtwerke-x.de", "ceo@stadtwerke-x.de", True),
        ("@stadtwerke-x.de", "ceo@it.stadtwerke-x.de", False),
        ("@*.stadtwerke-x.de", "ceo@it.stadtwerke-x.de", True),
        ("@*.stadtwerke-x.de", "ceo@stadtwerke-x.de", False),
        # deny → exact → most specific pattern → reject
        ("@stadtwerke-x.de, !praktikant@stadtwerke-x.de", "praktikant@stadtwerke-x.de", False),
        ("!@stadtwerke-x.de, cfo@stadtwerke-x.de", "cfo@stadtwerke-x.de", True),
        ("!@stadtwerke-x.de, cfo@stadtwerke-x.de", "ceo@stadtwerke-x.de", False),
        ("*, !@gmail.com", "someone@gmail.com", False),
        ("*, !@gmail.com", "someone@web.de", True),
        ("!@*.stadtwerke-x.de, @it.stadtwerke-x.de", "a@it.stadtwerke-x.de", True),
        ("!@*.stadtwerke-x.de, @it.stadtwerke-x.de", "a@hr.stadtwerke-x.de", False),
        ("@x.de, !@x.de", "a@x.de", False),  # tie: deny wins
        ("cfo@x.de, !cfo@x.de", "cfo@x.de", False),  # exact deny beats exact allow
    ],
)
def test_sender_rules(rules: str, addr: str, allowed: bool) -> None:
    assert sender_allowed(addr, parse_rules(rules)) is allowed


def test_accepts_plain_request(tmp_path: Path) -> None:
    decision = gate.check(_msg(fixture_mails()["request"]), settings(tmp_path))
    assert isinstance(decision, gate.Accept)
    assert decision.tenant == gate.DEFAULT_TENANT


def test_auto_reply_is_loop(tmp_path: Path) -> None:
    decision = gate.check(_msg(fixture_mails()["auto_reply"]), settings(tmp_path))
    assert isinstance(decision, gate.Reject)
    assert decision.code == ErrorCode.GATE_LOOP
    assert decision.event == "loop_ignored"


@pytest.mark.parametrize(
    "headers",
    [
        {"Precedence": "bulk"},
        {"X-Autoreply": "yes"},
        {"X-Finn-Run": "20261102T081500Z_abc"},  # Finn's own reply
    ],
)
def test_loop_headers(tmp_path: Path, headers: dict[str, str]) -> None:
    decision = gate.check(_msg(build_mail(headers=headers)), settings(tmp_path))
    assert isinstance(decision, gate.Reject)
    assert decision.code == ErrorCode.GATE_LOOP


def test_auto_submitted_no_is_fine(tmp_path: Path) -> None:
    decision = gate.check(_msg(build_mail(headers={"Auto-Submitted": "no"})), settings(tmp_path))
    assert isinstance(decision, gate.Accept)


def test_own_address_is_loop(tmp_path: Path) -> None:
    raw = build_mail(sender=("Finn", FINN))
    decision = gate.check(_msg(raw), settings(tmp_path))
    assert isinstance(decision, gate.Reject)
    assert decision.code == ErrorCode.GATE_LOOP


def test_account_owner_may_write_to_the_alias(tmp_path: Path) -> None:
    """The mailbox account itself (not the alias) is a normal sender (live test, demo)."""
    raw = build_mail(sender=("Konto", ACCOUNT))
    assert isinstance(gate.check(_msg(raw), settings(tmp_path)), gate.Accept)


def test_recipient_must_be_finn(tmp_path: Path) -> None:
    raw = build_mail(to="someone.else@example.com")
    decision = gate.check(_msg(raw), settings(tmp_path))
    assert isinstance(decision, gate.Reject)
    assert decision.code == ErrorCode.GATE_NOT_ADDRESSED


def test_unknown_sender_with_allowlist(tmp_path: Path) -> None:
    s = settings(tmp_path, allowed_senders="@stadtwerke-musterstadt.de")
    assert isinstance(gate.check(_msg(fixture_mails()["request"]), s), gate.Accept)
    decision = gate.check(_msg(fixture_mails()["unknown_sender"]), s)
    assert isinstance(decision, gate.Reject)
    assert decision.code == ErrorCode.GATE_SENDER_REJECTED
    assert decision.event == "access_rejected"


def test_dmarc(tmp_path: Path) -> None:
    fail = build_mail(
        headers={"Authentication-Results": "mx.google.com; spf=fail; dkim=none; dmarc=fail"}
    )
    ok = build_mail(
        headers={"Authentication-Results": "mx.google.com; spf=pass; dkim=pass; dmarc=pass"}
    )
    relaxed = settings(tmp_path)
    strict = settings(tmp_path, require_dmarc_pass=True)
    assert isinstance(gate.check(_msg(fail), relaxed), gate.Accept)  # logged only
    decision = gate.check(_msg(fail), strict)
    assert isinstance(decision, gate.Reject)
    assert decision.code == ErrorCode.GATE_AUTH_FAILED
    assert decision.auth.dmarc == "fail"
    assert isinstance(gate.check(_msg(ok), strict), gate.Accept)
    assert isinstance(gate.check(_msg(build_mail()), strict), gate.Reject)  # no header
