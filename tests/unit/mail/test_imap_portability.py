"""Connector portability (Gmail vs. GreenMail / plain IMAP servers), without a server."""

from __future__ import annotations  # FakeImap.list shadows the builtin in annotations

from email.message import EmailMessage
from pathlib import Path
from typing import Any

import pytest

from finn.mail import imap_smtp
from finn.mail.imap_smtp import ImapSmtpConnector, pick_sent_folder
from finn.models.mail import ReplyDraft
from mailkit import settings


@pytest.mark.parametrize(
    ("folders", "configured", "expected"),
    [
        # Gmail: localised name, found by the SPECIAL-USE flag
        ([({"\\hasnochildren"}, "INBOX"), ({"\\sent", "\\hasnochildren"}, "[Gmail]/Gesendet")],
         "", "[Gmail]/Gesendet"),
        # no SPECIAL-USE (GreenMail): the configured name, case-insensitive
        ([(set(), "INBOX"), (set(), "Postausgang"), (set(), "Sent")], "postausgang", "Postausgang"),
        # no flag, nothing configured: a common name
        ([(set(), "INBOX"), (set(), "Gesendete Elemente")], "", "Gesendete Elemente"),
        ([(set(), "INBOX"), (set(), "Verarbeitet")], "", None),
    ],
)  # fmt: skip
def test_pick_sent_folder(
    folders: list[tuple[set[str], str]], configured: str, expected: str | None
) -> None:
    assert pick_sent_folder(folders, configured) == expected


class FakeImap:
    """Just enough of imaplib.IMAP4 for login / LIST / CREATE / APPEND."""

    def __init__(self, caps: str, folders: list[bytes]) -> None:
        self.caps = caps
        self.folders = folders
        self.appended: list[tuple[str, bytes]] = []
        self.created: list[str] = []

    def login(self, user: str, password: str) -> tuple[str, list[bytes]]:
        return "OK", [b"LOGIN completed"]

    def capability(self) -> tuple[str, list[bytes]]:
        return "OK", [self.caps.encode()]

    def noop(self) -> tuple[str, list[bytes]]:
        return "OK", []

    def list(self) -> tuple[str, list[bytes]]:
        return "OK", list(self.folders)

    def create(self, name: str) -> tuple[str, list[bytes]]:
        self.created.append(name)
        self.folders.append(b'() "." ' + name.encode())
        return "OK", [b"CREATE completed"]

    def append(self, folder: str, flags: str, date: str, data: bytes) -> tuple[str, list[bytes]]:
        self.appended.append((folder, data))
        return "OK", [b"APPEND completed"]

    def logout(self) -> None:
        pass


def _connector(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake: FakeImap, **kw: Any
) -> ImapSmtpConnector:
    s = settings(tmp_path, imap_password="x", imap_ssl=False, **kw)
    monkeypatch.setattr(imap_smtp.imaplib, "IMAP4", lambda *a, **k: fake)
    return ImapSmtpConnector(s)


def _mime() -> EmailMessage:
    msg = EmailMessage()
    msg["Message-ID"] = "<finn.abc.1@finn.test>"
    msg.set_content("x")
    return msg


def test_plain_imap_creates_sent_and_appends(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeImap("IMAP4rev1 MOVE UIDPLUS", [b'() "." "INBOX"'])
    conn = _connector(tmp_path, monkeypatch, fake, imap_sent_folder="Sent")
    assert conn.append_sent_enabled()  # auto: not Gmail
    conn._append_sent(_mime())
    assert fake.created == ['"Sent"']
    assert [folder for folder, _ in fake.appended] == ['"Sent"']
    assert conn.sent_folder() == "Sent"


def test_gmail_never_appends(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeImap("IMAP4rev1 X-GM-EXT-1 MOVE", [b'(\\Sent) "/" "[Gmail]/Gesendet"'])
    conn = _connector(tmp_path, monkeypatch, fake)
    assert not conn.append_sent_enabled()  # auto + X-GM-EXT-1: Gmail files sent mail itself
    assert conn.is_gmail
    assert conn.sent_folder() == "[Gmail]/Gesendet"


def test_append_sent_setting_overrides_auto(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeImap("IMAP4rev1", [])
    off = _connector(tmp_path, monkeypatch, fake, imap_append_sent="false")
    on = _connector(tmp_path, monkeypatch, fake, imap_append_sent="true")
    assert not off.append_sent_enabled()
    assert on.append_sent_enabled()


@pytest.mark.parametrize(
    ("security", "port", "expected"),
    [
        ("auto", 465, ["ssl"]),
        ("auto", 587, ["plain", "starttls"]),
        ("starttls", 2525, ["plain", "starttls"]),
        ("plain", 3025, ["plain"]),
        ("ssl", 2465, ["ssl"]),
    ],
)
def test_smtp_security(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    security: str,
    port: int,
    expected: list[str],
) -> None:
    calls: list[str] = []

    class Smtp:
        kind = "plain"

        def __init__(self, *a: Any, **k: Any) -> None:
            calls.append(self.kind)

        def starttls(self, **k: Any) -> None:
            calls.append("starttls")

        def __enter__(self) -> Smtp:
            return self

        def __exit__(self, *a: Any) -> None:
            pass

        def login(self, *a: Any) -> None:
            pass

        def send_message(self, *a: Any, **k: Any) -> None:
            pass

    class SmtpSsl(Smtp):
        kind = "ssl"

    monkeypatch.setattr(imap_smtp.smtplib, "SMTP", Smtp)
    monkeypatch.setattr(imap_smtp.smtplib, "SMTP_SSL", SmtpSsl)
    s = settings(
        tmp_path, smtp_password="x", smtp_port=port, smtp_security=security,
        imap_append_sent="false",
    )  # fmt: skip
    draft = ReplyDraft(
        kind="other", to="cfo@stadtwerke.test", subject="AW: x", message_id="finn.a.1@finn.test",
        html="<p>x</p>", text="x", run_id="r", in_reply_to="req@x", references=["req@x"],
    )  # fmt: skip
    ImapSmtpConnector(s).send_reply(draft)
    assert calls == expected
