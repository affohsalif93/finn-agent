"""Drive the case catalogue against the local test mail server (compose profile `testmail`).

    send each step via SMTP (test sender → Finn) → Finn (in-process or the compose containers)
    polls, gates, runs the workflow, replies → fetch the replies via IMAP from the sender's
    mailbox → compare with the case's expectation (kinds, phrases, codes, exactly-once).

Used by `test_mail_cases.py` (pytest -m mailserver) and `finn demo send-cases`.
"""

import asyncio
import contextlib
import imaplib
import re
import smtplib
import socket
import subprocess
import time
import uuid
from collections.abc import AsyncIterator, Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from pathlib import Path
from typing import Any

from cases import PASSWORDS, Case, build_step

from finn import runs
from finn.config import Settings
from finn.mail.thread import message_ids

ROOT = Path(__file__).resolve().parents[2]
HOST = "127.0.0.1"
SMTP_PORT = 3025
IMAP_PORT = 3143
PROCESSED = "Verarbeitet"
COMPOSE_SERVICES = ("greenmail", "roundcube", "finn-worker-testmail", "finn-poller-testmail")


# ── infrastructure ───────────────────────────────────────────────────────────


def port_open(port: int, host: str = HOST) -> bool:
    with contextlib.suppress(OSError), socket.create_connection((host, port), timeout=1):
        return True
    return False


def wait_ports(timeout: float = 60) -> None:
    deadline = time.monotonic() + timeout
    while not (port_open(SMTP_PORT) and port_open(IMAP_PORT)):
        if time.monotonic() > deadline:
            raise TimeoutError("GreenMail (127.0.0.1:3025/3143) not reachable")
        time.sleep(1)


def compose_up(services: tuple[str, ...], *, real_llm_key: str | None = None) -> None:
    """`docker compose --profile testmail up -d --build --no-deps <services>`: never touches the
    core services (temporal, finn-worker, finn-poller). The LLM mode of the test containers is
    set here (TESTMAIL_LLM_FAKE / TESTMAIL_OPENAI_API_KEY, see docker-compose.yml); the key is
    passed in the subprocess environment only, never written or printed."""
    import os

    env = dict(os.environ)
    env["TESTMAIL_LLM_FAKE"] = "false" if real_llm_key else "true"
    env["TESTMAIL_OPENAI_API_KEY"] = real_llm_key or ""
    cmd = ["docker", "compose", "--profile", "testmail", "up", "-d", "--no-deps"]
    if any(s.startswith("finn-") for s in services):
        cmd.append("--build")
    subprocess.run([*cmd, *services], cwd=ROOT, env=env, check=True, capture_output=True)
    wait_ports()


def compose_gate_codes(since: str) -> Callable[[str], set[str]]:
    """Codes the compose poller logged per Message-ID (console log: `code=… message_id=…`)."""

    def codes(mid: str) -> set[str]:
        out = subprocess.run(
            ["docker", "compose", "logs", "--no-color", "--since", since, "finn-poller-testmail"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        ).stdout
        return {
            m.group(1)
            for line in out.splitlines()
            if f"message_id={mid}" in line
            for m in [re.search(r"code=(\w+)", line)]
            if m
        }

    return codes


# ── mail I/O ─────────────────────────────────────────────────────────────────


def smtp_send(raw: bytes, sender: str, to: str) -> None:
    with smtplib.SMTP(HOST, SMTP_PORT, timeout=30) as smtp:
        smtp.sendmail(sender, [to], raw)


def _login(addr: str) -> imaplib.IMAP4:
    conn = imaplib.IMAP4(HOST, IMAP_PORT, timeout=30)
    conn.login(addr, PASSWORDS.get(addr, addr))  # auto-created mailboxes: password = address
    return conn


def _search(conn: imaplib.IMAP4, folder: str, header: str, value: str) -> list[bytes]:
    typ, _ = conn.select(f'"{folder}"', readonly=True)
    if typ != "OK":
        return []
    typ, data = conn.uid("SEARCH", "HEADER", header, f'"<{value}>"')
    return data[0].split() if typ == "OK" and data and data[0] else []


@dataclass
class Received:
    uid: int
    message_id: str
    subject: str
    from_: str
    to: str
    in_reply_to: str | None
    references: list[str]
    run_id: str
    date: str
    text: str
    html: str
    kind: str = "?"
    codes: list[str] = field(default_factory=list)


def fetch_replies(addr: str, root_ids: list[str]) -> list[Received]:
    """Finn's mails in `addr`'s INBOX whose References / In-Reply-To name one of `root_ids`."""
    try:
        conn = _login(addr)
    except imaplib.IMAP4.error:
        return []  # mailbox never created = no mail ever delivered to it
    try:
        uids: set[bytes] = set()
        for mid in root_ids:
            uids |= set(_search(conn, "INBOX", "References", mid))
            uids |= set(_search(conn, "INBOX", "In-Reply-To", mid))
        out: list[Received] = []
        for uid in sorted(uids, key=int):
            typ, data = conn.uid("FETCH", uid, "(BODY.PEEK[])")
            raw = next((i[1] for i in data or [] if isinstance(i, tuple)), None)
            if typ == "OK" and isinstance(raw, bytes):
                out.append(_parse(int(uid), raw))
        return out
    finally:
        with contextlib.suppress(Exception):
            conn.logout()


def _parse(uid: int, raw: bytes) -> Received:
    msg = BytesParser(policy=policy.default).parsebytes(raw)
    assert isinstance(msg, EmailMessage)
    text_part = msg.get_body(("plain",))
    html_part = msg.get_body(("html",))
    return Received(
        uid=uid,
        message_id=(message_ids(str(msg["Message-ID"] or "")) or [""])[0],
        subject=str(msg["Subject"] or ""),
        from_=str(msg["From"] or ""),
        to=str(msg["To"] or ""),
        in_reply_to=(message_ids(str(msg["In-Reply-To"] or "")) or [None])[0],
        references=message_ids(str(msg["References"] or "")),
        run_id=str(msg["X-Finn-Run"] or ""),
        date=str(msg["Date"] or ""),
        text=text_part.get_content() if isinstance(text_part, EmailMessage) else "",
        html=html_part.get_content() if isinstance(html_part, EmailMessage) else "",
    )


def processed(finn: str, mid: str) -> bool:
    """Finn moved the mail out of its INBOX into the processed folder (poller is done)."""
    conn = _login(finn)
    try:
        return bool(_search(conn, PROCESSED, "Message-ID", mid)) and not _search(
            conn, "INBOX", "Message-ID", mid
        )
    finally:
        with contextlib.suppress(Exception):
            conn.logout()


# ── run folder → kind + codes ────────────────────────────────────────────────


def _codes(run_dir: Path) -> list[str]:
    found: list[str] = []
    data: list[Any] = []
    for name in ("checks.json", "results.json", "request.json"):
        value = runs.read_json(run_dir, name)
        if isinstance(value, list):
            data += value
        elif isinstance(value, dict):
            data += value.get("findings", [])
            data += (value.get("input") or {}).get("findings", [])
    for f in data:
        if isinstance(f, dict) and f.get("code") and f["code"] not in found:
            found.append(f["code"])
    return found


def annotate(reply: Received, runs_dir: Path) -> None:
    run_dir = runs_dir / reply.run_id
    seq = reply.message_id.partition("@")[0].rpartition(".")[2]
    files = sorted(run_dir.glob(f"reply-{seq}-*.txt"))
    if files:
        reply.kind = files[0].stem.split("-", 2)[2]
    reply.codes = _codes(run_dir)


# ── one case ─────────────────────────────────────────────────────────────────


@dataclass
class Sent:
    message_id: str
    sender: str
    to: str
    subject: str
    body: str
    attachments: list[tuple[str, int]]
    resend: bool = False


@dataclass
class CaseResult:
    case: Case
    sent: list[Sent] = field(default_factory=list)
    replies: list[Received] = field(default_factory=list)
    gate_codes: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    seconds: float = 0.0

    @property
    def passed(self) -> bool:
        return not self.problems

    @property
    def codes(self) -> list[str]:
        out = list(self.gate_codes)
        for r in self.replies:
            out += [c for c in r.codes if c not in out]
        return out


@dataclass
class Env:
    finn: str  # Finn's address (the test's mailbox)
    runs_dir: Path
    plans: dict[str, Path]
    gate_codes: Callable[[str], set[str]]
    timeout: float = 90
    settle: float = 4
    poll: float = 0.5
    nonce: str = field(default_factory=lambda: uuid.uuid4().hex[:10])


async def _until(pred: Callable[[], bool], timeout: float, poll: float) -> bool:
    deadline = time.monotonic() + timeout
    while True:
        if await asyncio.to_thread(pred):
            return True
        if time.monotonic() > deadline:
            return False
        await asyncio.sleep(poll)


async def run_case(case: Case, env: Env) -> CaseResult:
    started = time.monotonic()
    result = CaseResult(case)
    sender = case.sender[1]
    roots: list[str] = []
    last_raw = b""
    replies: list[Received] = []

    seen: dict[str, Received] = {}

    def refresh() -> list[Received]:
        """Replies so far; each one is annotated (kind, codes) from its run folder when it is
        first seen, i.e. before the next step of the case changes that folder."""
        nonlocal replies
        fresh = fetch_replies(sender, roots)
        for r in fresh:
            if r.message_id not in seen:
                annotate(r, env.runs_dir)
                seen[r.message_id] = r
            else:
                r.kind, r.codes = seen[r.message_id].kind, seen[r.message_id].codes
        replies = fresh
        return replies

    for i, step in enumerate(case.steps):
        if step.after_replies:
            ok = await _until(lambda n=step.after_replies: len(refresh()) >= n, env.timeout, 1)
            if not ok:
                result.problems.append(f"Schritt {i + 1}: keine {step.after_replies}. Antwort")
                break
        if step.resend:
            raw = last_raw
        else:
            latest = replies[-1] if replies else None
            raw = build_step(
                case,
                i,
                to=env.finn,
                nonce=env.nonce,
                plans=env.plans,
                finn_reply=(latest.message_id, latest.references) if latest else None,
            )
        msg = BytesParser(policy=policy.default).parsebytes(raw)
        assert isinstance(msg, EmailMessage)
        mid = message_ids(str(msg["Message-ID"]))[0]
        if mid not in roots:
            roots.append(mid)
        body = msg.get_body(("plain",))
        result.sent.append(
            Sent(
                message_id=mid,
                sender=str(msg["From"]),
                to=str(msg["To"]),
                subject=str(msg["Subject"]),
                body=body.get_content() if isinstance(body, EmailMessage) else "",
                attachments=[
                    (p.get_filename() or "?", len(p.get_payload(decode=True) or b""))
                    for p in msg.iter_attachments()
                ],
                resend=step.resend,
            )
        )
        await asyncio.to_thread(smtp_send, raw, sender, env.finn)
        last_raw = raw

    want = len(case.replies)

    def done() -> bool:
        return len(refresh()) >= want and all(processed(env.finn, m) for m in roots)

    if not await _until(done, env.timeout, env.poll):
        result.problems.append("Zeitüberschreitung: Finn hat nicht alles verarbeitet")
    await asyncio.sleep(env.settle)  # exactly once: nothing else may arrive afterwards
    await asyncio.to_thread(refresh)
    result.replies = replies
    for mid in roots:
        result.gate_codes += [c for c in sorted(env.gate_codes(mid)) if c not in result.gate_codes]
    result.problems += evaluate(case, result, roots[0] if roots else "")
    result.seconds = time.monotonic() - started
    return result


def evaluate(case: Case, result: CaseResult, root: str) -> list[str]:
    problems: list[str] = []
    got = result.replies
    if len(got) != len(case.replies):
        problems.append(f"{len(case.replies)} Antwort(en) erwartet, {len(got)} erhalten")
    ids = [r.message_id for r in got]
    if len(set(ids)) != len(ids):
        problems.append("dieselbe Antwort mehrfach zugestellt")
    for n, (want, reply) in enumerate(zip(case.replies, got, strict=False), 1):
        if reply.kind != want.kind:
            problems.append(f"Antwort {n}: Art {reply.kind}, erwartet {want.kind}")
        missing = [p for p in want.phrases if p.lower() not in reply.text.lower()]
        if missing:
            problems.append(f"Antwort {n}: fehlt im Text: {missing}")
        if root and (not reply.references or reply.references[0] != root):
            problems.append(f"Antwort {n}: nicht im Thread der Anfrage")
        if not reply.subject.startswith("AW:"):
            problems.append(f"Antwort {n}: Betreff ohne 'AW:'")
    codes = result.codes
    missing_codes = [c for c in case.codes if c not in codes]
    if missing_codes:
        problems.append(f"Codes fehlen: {missing_codes} (gefunden: {codes})")
    return problems


async def run_cases(cases: list[Case], env: Env) -> list[CaseResult]:
    return list(await asyncio.gather(*(run_case(c, env) for c in cases)))


# ── Finn in-process (pytest default) ─────────────────────────────────────────


def inprocess_settings(finn: str, runs_dir: Path, task_queue: str) -> Settings:
    values: dict[str, Any] = {
        "imap_host": HOST,
        "imap_port": IMAP_PORT,
        "imap_ssl": False,
        "imap_user": finn,
        "imap_password": PASSWORDS[finn],
        "imap_folder": "INBOX",
        "imap_processed_folder": PROCESSED,
        "imap_sent_folder": "Sent",
        "imap_append_sent": "true",
        "smtp_host": HOST,
        "smtp_port": SMTP_PORT,
        "smtp_security": "plain",
        "smtp_user": finn,
        "smtp_password": PASSWORDS[finn],
        "finn_address": finn,
        "allowed_senders": "@stadtwerke.test,!blocked@stadtwerke.test",
        "require_dmarc_pass": False,
        "runs_dir": runs_dir,
        "profiles_dir": ROOT / "data" / "contract_profiles",
        "contracts_dir": ROOT / "material",
        "max_mail_mb": 2,
        "poll_interval_seconds": 0.5,
        "temporal_task_queue": task_queue,
        "reply_llm_polish": True,
        "openai_api_key": None,
    }
    return Settings(_env_file=None, **values)  # type: ignore[call-arg]


@asynccontextmanager
async def inprocess_finn(client: Any, settings: Settings) -> AsyncIterator[dict[str, set[str]]]:
    """Worker + poller loop in this process (the real `poll_once` / activities / IMAP+SMTP
    connector). Yields the gate / duplicate codes per Message-ID, recorded from the real
    decisions (`gate.check`, `handle_message` outcome)."""
    from temporalio.worker import Worker

    import finn.poller as poller
    from finn.errors import ErrorCode
    from finn.mail import gate
    from finn.mail.imap_smtp import ImapSmtpConnector
    from finn.workflows.activities import FinnActivities
    from finn.workflows.covenant_check import CovenantCheckWorkflow

    events: dict[str, set[str]] = {}
    real_check, real_handle = gate.check, poller.handle_message

    def check(msg: Any, s: Settings) -> Any:
        decision = real_check(msg, s)
        if isinstance(decision, gate.Reject):
            events.setdefault(msg.message_id, set()).add(str(decision.code))
        return decision

    async def handle(c: Any, conn: Any, s: Settings, msg: Any) -> Any:
        outcome = await real_handle(c, conn, s, msg)
        if outcome == "duplicate":
            events.setdefault(msg.message_id, set()).add(str(ErrorCode.GATE_DUPLICATE))
        return outcome

    gate.check = check  # type: ignore[assignment]
    poller.handle_message = handle  # type: ignore[assignment]
    stop = asyncio.Event()
    connector = ImapSmtpConnector(settings)

    async def loop() -> None:
        while not stop.is_set():
            with contextlib.suppress(Exception):
                await poller.poll_once(client, connector, settings)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), settings.poll_interval_seconds)

    try:
        with ThreadPoolExecutor(max_workers=16) as executor:
            async with Worker(
                client,
                task_queue=settings.temporal_task_queue,
                workflows=[CovenantCheckWorkflow],
                activities=FinnActivities(settings).all(),
                activity_executor=executor,
            ):
                task = asyncio.create_task(loop())
                try:
                    yield events
                finally:
                    stop.set()
                    await task
    finally:
        gate.check = real_check  # type: ignore[assignment]
        poller.handle_message = real_handle  # type: ignore[assignment]
        connector.close()
