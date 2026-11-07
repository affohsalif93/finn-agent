"""Reply rendering: golden German result mail for the sample plan, variants, number check, polish.

Regenerate the golden file after an intended change:
`UPDATE_GOLDEN=1 uv run pytest tests/unit/reply`.
"""

import os
import re
from pathlib import Path
from typing import Any

import pytest

from finn.calc.engine import calculate
from finn.contracts.store import load_profiles
from finn.errors import ErrorCode, finding
from finn.llm import client
from finn.mail.parse import inbound_from_bytes
from finn.models.result import Report
from finn.plan import read_plan
from finn.reply import render
from finn.reply.number_check import allowed_values, numbers_in, unknown_numbers
from finn.reply.render import PolishAnswer
from mailkit import (
    FINN,
    GOLD_PROFILES,
    MATERIAL,
    SAMPLE,
    fixture_mails,
    llm_returning,
    llm_unavailable,
)

GOLDEN = Path(__file__).resolve().parents[2] / "fixtures" / "replies" / "result_sample.txt"
RUN_ID = "20261102T081500Z_7d364a2056c1"


@pytest.fixture(scope="module")
def report() -> Report:
    profiles, findings = load_profiles(GOLD_PROFILES, MATERIAL)
    plan = read_plan(SAMPLE)
    return calculate(
        plan.plan, profiles, plan.findings, profile_findings=findings, file=SAMPLE.name
    )


@pytest.fixture
def inbound():  # type: ignore[no-untyped-def]
    return inbound_from_bytes(fixture_mails()["request"], mailbox=FINN, keep_raw=False)


@pytest.fixture(autouse=True)
def _no_llm(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(client, "complete_structured", llm_unavailable)


def _result(report: Report, inbound) -> tuple[render.Context, Any]:  # type: ignore[no-untyped-def]
    ctx = render.result_context(report, inbound=inbound, extra_findings=[], run_id=RUN_ID)
    d = render.draft(ctx, inbound=inbound, workflow_id=inbound.message_id, seq=1,
                     from_addr="finn.box@example.com")  # fmt: skip
    return ctx, d


def test_golden_result_mail(report: Report, inbound) -> None:  # type: ignore[no-untyped-def]
    _, d = _result(report, inbound)
    if os.environ.get("UPDATE_GOLDEN"):
        GOLDEN.parent.mkdir(parents=True, exist_ok=True)
        GOLDEN.write_text(d.text, encoding="utf-8", newline="\n")
    assert d.text == GOLDEN.read_text(encoding="utf-8")


def test_result_mail_content(report: Report, inbound) -> None:  # type: ignore[no-untyped-def]
    _, d = _result(report, inbound)
    text = d.text
    assert text.startswith("Guten Tag Dr. Max Mustermann,")
    for expected in (
        "Wirtschaftsplan_2027.xlsx",
        "Stadtwerke Musterstadt GmbH · Plan 2027 (Entwurf vom 15.10.2026) · Stichtag 31.12.2027",
        "Auf Basis des Plans werden alle 4 Financial Covenants eingehalten.",
        "Am engsten: Dynamischer Verschuldungsgrad (Konsortialkredit) mit 3,37 bei max. 3,50.",
        "darf um bis zu 1.512 T€ (3,8 %) sinken",
        "Frühwarnung auf Planbasis, kein Vertragsbruch",
        "34,32 %",
        "1,56",
        "30,7 %",
        "ja (knapp)",
        "(148.200 − 12.600) / 40.200 = 3,3731 → gerundet 3,37",
        "§ 14 Abs. 1 lit. b, S. 7: „der Dynamische Verschuldungsgrad",
        "[Bilanz!D28]",
        "qualifizierter Rangrücktritt",
        "Sparkasse: keine Financial Covenants",
        "Marge 1,25 % p. a.",
        "Alle Kennzahlen wurden deterministisch berechnet, nicht vom Sprachmodell.",
        f"Finn · Referenz: {RUN_ID}",
    ):
        assert expected in text, expected
    assert "3,57" not in text  # the V4 trap: shareholder loan must stay out
    assert d.subject == "AW: Covenant-Prüfung Wirtschaftsplan 2027"
    assert d.in_reply_to == inbound.message_id
    assert d.references == [inbound.message_id]
    assert d.to == inbound.from_addr


def test_html_part_is_outlook_safe(report: Report, inbound) -> None:  # type: ignore[no-untyped-def]
    _, d = _result(report, inbound)
    html = d.html
    assert "<script" not in html.lower()
    assert "<img" not in html.lower()
    assert "<style" not in html.lower()  # inline CSS only
    assert "3,37" in html
    assert "Alle Kennzahlen wurden deterministisch berechnet" in html
    assert "&lt;" not in html.split("<body")[1][:200]  # no double escaping of the layout
    # same numbers in both parts
    plain_numbers = set(numbers_in(d.text))
    html_text = re.sub(r"<[^>]+>", " ", html)
    assert {"34,32", "1,56", "30,7", "3,37", "148.200"} <= plain_numbers & set(
        numbers_in(html_text)
    )


def test_message_id_is_deterministic(inbound) -> None:  # type: ignore[no-untyped-def]
    a = render.reply_message_id("wf-1", 1, "result", "finn.box@example.com")
    assert a == render.reply_message_id("wf-1", 1, "result", "finn.box@example.com")
    assert a != render.reply_message_id("wf-1", 2, "result", "finn.box@example.com")
    assert a.endswith("@example.com")


def test_ask_block_other_error_variants(inbound) -> None:  # type: ignore[no-untyped-def]
    ask = render.ask_context(
        [finding(ErrorCode.ATTACHMENT_MISSING)], inbound=inbound, run_id=RUN_ID, wait_days=7
    )
    _, text = render.render(ask)
    assert "Bitte den Wirtschaftsplan als Excel-Datei (.xlsx) anhängen." in text
    assert "Ich warte 7 Tage" in text
    assert "deterministisch" not in text
    block = render.block_context(
        [finding(ErrorCode.PLAN_UNBALANCED, assets="313.200", liabilities="312.900")],
        inbound=inbound, run_id=RUN_ID, file="Plan.xlsx", header=None, wait_days=7,
    )  # fmt: skip
    _, text = render.render(block)
    assert "Bilanz nicht ausgeglichen: Aktiva 313.200, Passiva 312.900 T€." in text
    assert "Die Datei Plan.xlsx kann ich so nicht auswerten" in text
    _, text = render.render(render.other_context(inbound=inbound, run_id=RUN_ID, question=False))
    assert "prüfe die Financial Covenants" in text
    _, text = render.render(render.error_context(inbound=inbound, run_id=RUN_ID))
    assert f"Referenz {RUN_ID}" in text


def test_number_check() -> None:
    allowed = allowed_values(["Am engsten: 3,37 bei max. 3,50; EBITDA 40.200 T€ (§ 14 Abs. 1)."])
    assert unknown_numbers("Der Wert 3,37 liegt unter 3,50.", allowed) == []
    assert unknown_numbers("Nach § 14 Abs. 1 sind es 40.200 T€.", allowed) == []
    assert unknown_numbers("Der Wert 3,38 liegt unter 3,50.", allowed) == ["3,38"]
    assert unknown_numbers("rund 40 Mio.", allowed) == ["40"]
    assert unknown_numbers("Zelle Bilanz!D28, Vertrag V4", allowed) == []  # glued to letters


def _polish_with(monkeypatch: pytest.MonkeyPatch, rewrite: Any) -> None:
    def fake(kwargs: dict[str, Any]) -> PolishAnswer:
        user = kwargs["user"]
        summary_block, notes_block = user.split("\n\nHinweise:\n")
        summary = [line[2:] for line in summary_block.splitlines()[1:]]
        notes = [line[2:] for line in notes_block.splitlines()]
        return rewrite(summary, notes)

    monkeypatch.setattr(client, "complete_structured", llm_returning({"reply_polish": fake}))


def test_polish_accepted(report: Report, inbound, monkeypatch: pytest.MonkeyPatch) -> None:  # type: ignore[no-untyped-def]
    seen: list[str] = []

    def rewrite(summary: list[str], notes: list[str]) -> PolishAnswer:
        seen.extend(summary + notes)
        return PolishAnswer(summary=[f"Kurz: {s}" for s in summary], notes=notes)

    _polish_with(monkeypatch, rewrite)
    ctx, _ = _result(report, inbound)
    names = render.names_for_masking(report, inbound)
    polished, notes = render.polish(ctx, names)
    assert notes == []
    assert polished is not ctx
    assert polished.summary[0].startswith("Kurz: Auf Basis des Plans werden alle 4")
    assert "3,37" in polished.summary[1]
    # the LLM saw placeholders only: no numbers, no names
    prompt = "\n".join(seen)
    assert not re.search(r"\d", re.sub(r"\{\{[A-Z]+\d+\}\}", "", prompt))
    assert "Konsortialkredit" not in prompt
    assert "Dynamischer Verschuldungsgrad" not in prompt


def test_polish_with_new_number_falls_back(  # type: ignore[no-untyped-def]
    report: Report, inbound, monkeypatch: pytest.MonkeyPatch
) -> None:
    def rewrite(summary: list[str], notes: list[str]) -> PolishAnswer:
        return PolishAnswer(
            summary=[*summary[:-1], summary[-1] + " Rund 4,2 % Puffer."], notes=notes
        )

    _polish_with(monkeypatch, rewrite)
    ctx, _ = _result(report, inbound)
    polished, notes = render.polish(ctx, render.names_for_masking(report, inbound))
    assert polished is ctx
    assert [n.code for n in notes] == [ErrorCode.REPLY_NUMBER_MISMATCH]
    assert "4,2" in notes[0].message_de


def test_polish_dropping_a_placeholder_falls_back(  # type: ignore[no-untyped-def]
    report: Report, inbound, monkeypatch: pytest.MonkeyPatch
) -> None:
    def rewrite(summary: list[str], notes: list[str]) -> PolishAnswer:
        return PolishAnswer(summary=["Alles gut.", *summary[1:]], notes=notes)

    _polish_with(monkeypatch, rewrite)
    ctx, _ = _result(report, inbound)
    polished, notes = render.polish(ctx, render.names_for_masking(report, inbound))
    assert polished is ctx
    assert [n.code for n in notes] == [ErrorCode.REPLY_NUMBER_MISMATCH]


def test_polish_llm_unavailable(report: Report, inbound) -> None:  # type: ignore[no-untyped-def]
    ctx, _ = _result(report, inbound)
    polished, notes = render.polish(ctx, render.names_for_masking(report, inbound))
    assert polished is ctx
    assert [n.code for n in notes] == [ErrorCode.REPLY_LLM_UNAVAILABLE]
