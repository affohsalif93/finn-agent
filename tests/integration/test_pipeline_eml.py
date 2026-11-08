"""Direct runner end to end on `.eml` fixtures: no network (LLM mocked, fake mailbox)."""

import json
from email import policy
from email.parser import BytesParser
from pathlib import Path

import pytest
from typer.testing import CliRunner

from finn.mail.connector import build_mime
from finn.pipeline import process_eml
from mailkit import (
    ACCOUNT,
    FakeConnector,
    build_mail,
    fixture_mails,
    settings,
    write_fixtures,
)

pytestmark = pytest.mark.usefixtures("no_llm")


@pytest.fixture
def mails(tmp_path: Path) -> dict[str, Path]:
    return write_fixtures(tmp_path / "mails")


def test_sample_request_end_to_end(tmp_path: Path, mails: dict[str, Path]) -> None:
    s = settings(tmp_path)
    fake = FakeConnector()
    result = process_eml(mails["request"], s, send=True, connector=fake)
    assert result.rejected is None
    [draft] = result.drafts
    assert draft.kind == "result"
    assert result.report is not None
    assert result.report.overall == "all_met"
    values = {r.covenant_id: str(r.value_rounded) for r in result.report.results}
    assert values == {"V2-WEKQ": "34.32", "V3-DSCR": "1.56", "V4-EKQ": "30.7", "V4-DVG": "3.37"}
    [sent] = fake.sent
    assert sent.message_id == draft.message_id
    assert sent.to == "max.mustermann@stadtwerke-musterstadt.de"

    run = result.run_dir
    for name in (
        "incoming.eml",
        "request.json",
        "plan_inputs.json",
        "checks.json",
        "results.json",
        "reply.html",
        "reply.txt",
        "reply-1-result.txt",
        "log.txt",
        "pseudonyms.json",
        "llm_calls.jsonl",
    ):
        assert (run / name).is_file(), name
    assert (run / "attachments" / "Wirtschaftsplan_2027.xlsx").is_file()
    assert not (run / "error.txt").exists()
    assert len(list((run / "sent").glob("*.json"))) == 1
    # signature (name, phone) stripped before the LLM sees the text; the call is logged
    calls = (run / "llm_calls.jsonl").read_text(encoding="utf-8")
    assert "Mustermann" not in calls
    assert "+49 421" not in calls
    assert json.loads(calls.splitlines()[0])["error"].startswith("LLMUnavailable")
    log = (run / "log.txt").read_text(encoding="utf-8")
    assert "[intent] covenant_check (rules" in log
    assert "[calculate] all_met" in log


def test_mime_of_the_reply(tmp_path: Path, mails: dict[str, Path]) -> None:
    from datetime import UTC, datetime

    result = process_eml(mails["request"], settings(tmp_path))
    [draft] = result.drafts
    mime = build_mime(draft, from_addr=ACCOUNT, from_name="Finn (Covenant-Prüfung)",
                      date=datetime(2026, 11, 2, 9, 0, tzinfo=UTC))  # fmt: skip
    parsed = BytesParser(policy=policy.default).parsebytes(mime.as_bytes())
    assert parsed["Message-ID"] == f"<{draft.message_id}>"
    assert parsed["In-Reply-To"] == "<req-0001@stadtwerke-musterstadt.de>"
    assert parsed["References"] == "<req-0001@stadtwerke-musterstadt.de>"
    assert parsed["Auto-Submitted"] == "auto-replied"
    assert parsed["X-Finn-Run"] == draft.run_id
    assert parsed["Subject"] == "AW: Covenant-Prüfung Wirtschaftsplan 2027"
    assert "Finn (Covenant-Prüfung)" in str(parsed["From"])
    assert parsed.get_content_type() == "multipart/alternative"
    plain = parsed.get_body(preferencelist=("plain",))
    html = parsed.get_body(preferencelist=("html",))
    assert plain is not None
    assert html is not None
    assert "3,37" in plain.get_content()
    assert "3,37" in html.get_content()


def test_send_is_idempotent(tmp_path: Path, mails: dict[str, Path]) -> None:
    s = settings(tmp_path)
    fake = FakeConnector()
    first = process_eml(mails["request"], s, send=True, connector=fake)
    second = process_eml(mails["request"], s, send=True, connector=fake)  # same run folder
    assert second.run_dir == first.run_dir
    assert len(fake.sent) == 1
    assert second.sent[0].skipped_already_sent


def test_sent_folder_check_prevents_double_send(tmp_path: Path, mails: dict[str, Path]) -> None:
    """Crash after SMTP, before the local marker: the Sent folder still has the mail."""
    s = settings(tmp_path)
    fake = FakeConnector()
    first = process_eml(mails["request"], s, send=True, connector=fake)
    for marker in (first.run_dir / "sent").glob("*.json"):
        marker.unlink()
    again = process_eml(mails["request"], s, send=True, connector=fake)
    assert len(fake.sent) == 1
    assert again.sent[0].skipped_already_sent


@pytest.mark.parametrize(
    ("name", "kind", "phrase"),
    [
        ("no_attachment", "ask", "Bitte den Wirtschaftsplan als Excel-Datei (.xlsx) anhängen."),
        ("two_xlsx", "ask", "Welche Datei soll ich prüfen"),
        ("pdf", "ask", "Erhalten: Wirtschaftsplan_2027.pdf"),
        ("forwarded", "result", "3,37"),
        ("umlaut_filename", "result", "Wirtschaftsplan_2027_Entwurf_Prüfung.xlsx"),
        ("unknown_sender", "result", "3,37"),  # ALLOWED_SENDERS=* (demo mode)
    ],
)
def test_fixture_mails(tmp_path: Path, mails: dict[str, Path], name: str, kind: str,
                       phrase: str) -> None:  # fmt: skip
    result = process_eml(mails[name], settings(tmp_path))
    [draft] = result.drafts
    assert draft.kind == kind
    assert phrase in draft.text


def test_gate_rejects_before_parsing(tmp_path: Path, mails: dict[str, Path]) -> None:
    s = settings(tmp_path, allowed_senders="@stadtwerke-musterstadt.de")
    result = process_eml(mails["unknown_sender"], s)
    assert result.rejected is not None
    assert result.drafts == []
    assert not result.run_dir.exists()  # nothing saved, attachments never parsed
    loop = process_eml(mails["auto_reply"], settings(tmp_path))
    assert loop.rejected is not None
    assert loop.rejected.event == "loop_ignored"


def test_plan_year_mismatch_blocks(tmp_path: Path) -> None:
    from mailkit import plan_attachment

    raw = build_mail(
        subject="Covenant-Prüfung Wirtschaftsplan 2028",
        body="Bitte den Wirtschaftsplan 2028 prüfen.",
        attachments=[plan_attachment()],
    )
    path = tmp_path / "m.eml"
    path.write_bytes(raw)
    result = process_eml(path, settings(tmp_path))
    [draft] = result.drafts
    assert draft.kind == "block"
    assert "Angefragt: 2028, die Datei enthält Plan 2027." in draft.text
    assert "3,37" not in draft.text  # fail closed: no result


def test_corrupt_workbook_asks(tmp_path: Path) -> None:
    from mailkit import XLSX

    raw = build_mail(attachments=[("Wirtschaftsplan_2027.xlsx", b"not a zip at all", XLSX)])
    path = tmp_path / "m.eml"
    path.write_bytes(raw)
    [draft] = process_eml(path, settings(tmp_path)).drafts
    assert draft.kind == "ask"
    assert "beschädigt" in draft.text


def test_unexpected_error_sends_polite_mail(
    tmp_path: Path, mails: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    import finn.pipeline as pipeline

    def boom(*_: object, **__: object) -> None:
        raise RuntimeError("kaputt")

    monkeypatch.setattr(pipeline, "read_plan", boom)
    result = process_eml(mails["request"], settings(tmp_path))
    [draft] = result.drafts
    assert draft.kind == "error"
    assert result.run_dir.name in draft.text
    assert "RuntimeError: kaputt" in (result.run_dir / "error.txt").read_text(encoding="utf-8")


def test_cli_process_prints_reply(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from finn import cli
    from finn.config import get_settings

    path = tmp_path / "m.eml"
    path.write_bytes(fixture_mails()["request"])
    s = settings(tmp_path)
    monkeypatch.setattr(cli, "get_settings", lambda: s)
    get_settings.cache_clear()
    result = CliRunner().invoke(cli.app, ["process", str(path)])
    assert result.exit_code == 0, result.output
    assert "Betreff: AW: Covenant-Prüfung Wirtschaftsplan 2027" in result.output
    assert "Dynamischer Verschuldungsgrad" in result.output
    assert "3,37" in result.output
