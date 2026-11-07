"""`.eml` parsing: headers for the gate, body + attachments for the workflow, thread identity."""

from pathlib import Path

import pytest

from finn.mail.parse import detect_kind, html_to_text, inbound_from_bytes, parse_eml
from finn.mail.thread import (
    candidate_ids,
    message_ids,
    normalise_subject,
    reply_references,
    reply_subject,
    thread_id,
)
from mailkit import (
    CFO,
    FINN,
    SAMPLE,
    XLSX,
    build_mail,
    plan_attachment,
    write_fixtures,
)


@pytest.fixture(scope="module")
def mails(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    return write_fixtures(tmp_path_factory.mktemp("mails"))


def _parse(path: Path, tmp_path: Path):  # type: ignore[no-untyped-def]
    return parse_eml(path, tmp_path / "attachments", max_attachment_bytes=25 * 1024 * 1024)


def test_inbound_headers(mails: dict[str, Path]) -> None:
    msg = inbound_from_bytes(mails["request"].read_bytes(), mailbox=FINN)
    assert msg.message_id == "req-0001@stadtwerke-musterstadt.de"
    assert msg.from_addr == CFO[1]
    assert msg.from_name == CFO[0]
    assert msg.to_addrs == [FINN]
    assert msg.subject == "Covenant-Prüfung Wirtschaftsplan 2027"
    assert msg.date is not None
    assert msg.date.utcoffset() is not None
    assert msg.raw is not None
    assert "raw" not in msg.model_dump()  # never serialised (Temporal payloads)


def test_plain_request_with_sample_plan(mails: dict[str, Path], tmp_path: Path) -> None:
    body = _parse(mails["request"], tmp_path)
    assert "Bitte prüfen Sie die Covenants" in body.body_text
    [att] = body.attachments
    assert att.kind == "xlsx"
    assert att.filename == "Wirtschaftsplan_2027.xlsx"
    assert Path(att.path).read_bytes() == SAMPLE.read_bytes()
    assert att.size == SAMPLE.stat().st_size


def test_no_attachment(mails: dict[str, Path], tmp_path: Path) -> None:
    assert _parse(mails["no_attachment"], tmp_path).attachments == []


def test_two_xlsx(mails: dict[str, Path], tmp_path: Path) -> None:
    names = [a.filename for a in _parse(mails["two_xlsx"], tmp_path).attachments]
    assert names == ["Variante_A.xlsx", "Variante_B.xlsx"]


def test_pdf_instead_of_xlsx(mails: dict[str, Path], tmp_path: Path) -> None:
    [att] = _parse(mails["pdf"], tmp_path).attachments
    assert att.kind == "pdf"


def test_umlaut_filename_kept(mails: dict[str, Path], tmp_path: Path) -> None:
    [att] = _parse(mails["umlaut_filename"], tmp_path).attachments
    assert att.filename == "Wirtschaftsplan_2027_Entwurf_Prüfung.xlsx"
    assert Path(att.path).name == att.filename
    assert att.kind == "xlsx"


def test_forwarded_mail_keeps_attachment_and_text(mails: dict[str, Path], tmp_path: Path) -> None:
    body = _parse(mails["forwarded"], tmp_path)
    assert [a.kind for a in body.attachments] == ["xlsx"]
    assert "finale Entwurf" in body.body_text


def test_tnef_zip_and_link_detected(mails: dict[str, Path], tmp_path: Path) -> None:
    assert [a.kind for a in _parse(mails["tnef"], tmp_path / "t").attachments] == ["tnef"]
    assert [a.kind for a in _parse(mails["zip"], tmp_path / "z").attachments] == ["zip"]
    link = _parse(mails["link"], tmp_path / "l")
    assert link.attachments == []
    assert link.links == ["https://stadtwerke.sharepoint.com/sites/fin/Plan_2027.xlsx"]


@pytest.mark.parametrize(
    ("name", "data", "kind"),
    [
        ("plan.xlsx", SAMPLE.read_bytes(), "xlsx"),
        ("plan", SAMPLE.read_bytes(), "xlsx"),  # renamed: found by magic bytes + zip content
        ("plan.xlsm", b"PK\x03\x04", "xlsm"),
        ("plan.xls", b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1", "xls"),
        ("plan.xlsx", b"%PDF-1.4", "pdf"),  # PDF content wins over a wrong extension
        ("plan.docx", b"PK\x03\x04", "other"),
        ("bild.png", b"\x89PNG", "other"),
        ("archiv.7z", b"7z", "zip"),
    ],
)
def test_detect_kind(name: str, data: bytes, kind: str) -> None:
    assert detect_kind(name, "application/octet-stream", data) == kind


def test_size_limit_marks_attachment(mails: dict[str, Path], tmp_path: Path) -> None:
    body = parse_eml(mails["request"], tmp_path / "a", max_attachment_bytes=1000)
    [att] = body.attachments
    assert att.too_large
    assert not Path(att.path).exists()


def test_html_only_mail(tmp_path: Path) -> None:
    raw = build_mail(body="", html="<p>Bitte <b>prüfen</b></p><blockquote>alt</blockquote>")
    path = tmp_path / "m.eml"
    path.write_bytes(raw)
    assert "prüfen" in html_to_text("<p>Bitte <b>prüfen</b></p>")
    text = _parse(path, tmp_path).body_text  # empty plain part → HTML, quotes dropped
    assert text == "Bitte prüfen"


def test_missing_message_id_gets_stable_id() -> None:
    raw = build_mail().replace(b"Message-ID: <req-0001@stadtwerke-musterstadt.de>\n", b"")
    a = inbound_from_bytes(raw, mailbox=FINN)
    b = inbound_from_bytes(raw, mailbox=FINN)
    assert a.message_id == b.message_id
    assert a.message_id.startswith("no-id.")


def test_thread_identity() -> None:
    root = "req-0001@stadtwerke-musterstadt.de"
    ask = "finn.abc.1@example.com"
    raw = build_mail(
        subject="AW: Covenant-Prüfung Wirtschaftsplan 2027",
        message_id="req-0002@stadtwerke-musterstadt.de",
        in_reply_to=ask,
        references=[root, ask],
        attachments=[plan_attachment()],
    )
    msg = inbound_from_bytes(raw, mailbox=FINN)
    assert msg.references == [root, ask]
    assert thread_id(msg) == root
    assert candidate_ids(msg) == [ask, root]
    assert reply_references(msg) == [root, ask, "req-0002@stadtwerke-musterstadt.de"]
    assert reply_subject(msg.subject) == "AW: Covenant-Prüfung Wirtschaftsplan 2027"
    assert reply_subject("Wirtschaftsplan 2027") == "AW: Wirtschaftsplan 2027"
    assert reply_subject("WG: Plan") == "AW: WG: Plan"
    assert normalise_subject("AW: WG: Re: Plan") == "Plan"
    assert message_ids("<a@b> <c@d> <a@b>") == ["a@b", "c@d"]


def test_subject_fallback_thread_id() -> None:
    raw = build_mail(subject="AW: Plan 2027", message_id="x@y")
    msg = inbound_from_bytes(raw, mailbox=FINN)
    assert thread_id(msg).startswith("subject:")


def test_xlsx_mime_constant() -> None:
    assert XLSX.endswith("spreadsheetml.sheet")
