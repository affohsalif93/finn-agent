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
    flat = " ".join(text.split())  # the calculation path wraps long lines
    assert text.startswith("Guten Tag Dr. Max Mustermann,")
    for expected in (
        "die Covenant-Prüfung für den Wirtschaftsplan 2027 ist abgeschlossen.",
        # lead box
        "Auf Basis des Plans werden alle 4 Financial Covenants eingehalten.",
        "Am engsten: Dynamischer Verschuldungsgrad (Konsortialkredit) mit 3,37 bei max. 3,50 "
        "(Spielraum 0,13).",
        "darf um bis zu 1.512 T€ (3,8 %) sinken",
        "Frühwarnung auf Planbasis, kein Vertragsbruch",
        # compact table with footnote marks, footnotes
        "V4 Konsortialkredit · Dynamischer Verschuldungsgrad⁴",
        "34,32 %",
        "1,56",
        "30,7 %",
        "ja (knapp)",
        "⁴ gem. Vertrag 4, § 1 Abs. 1 S. 2–3; § 14 Abs. 1 lit. b S. 7",
        "Planwerte: Wirtschaftsplan_2027.xlsx (GuV, Bilanz, Finanzierung)",
        "Stadtwerke Musterstadt GmbH · Plan 2027 (Entwurf vom 15.10.2026) · Stichtag 31.12.2027",
        # Hinweise
        "Sparkasse: keine Financial Covenants",
        "Marge 1,25 % p. a.",
        "Rechenweg und Quellen",
        # calculation path, four steps
        "Quelle im Vertrag   § 14 Abs. 1 lit. b (S. 7), Definition § 1 Abs. 1 (S. 2–3)",
        "Formel              (Finanzverbindlichkeiten − Kassenbestand, Bankguthaben) / EBITDA",
        "Verbindlichkeiten ggü. Kreditinstituten  148.200   Bilanz!D28",
        "= Nettofinanzverbindlichkeiten",
        "GuV (Herleitung unten)",
        "135.600 / 40.200 = 3,373 → kaufmännisch gerundet 3,37",
        "Gesellschafterdarlehen (8.000 T€, Bilanz!D29) nicht einbezogen: qualifizierter",
        "mit Ausnahme von Nachrangigen Gesellschafterdarlehen.“",
        "EBITDA (Herleitung)",
        "Alle Kennzahlen stammen aus einem deterministischen Rechenkern, nicht aus dem "
        "Sprachmodell.",
        f"Referenz: {RUN_ID}",
    ):
        assert expected in text or expected in flat, expected
    # the V4 trap: the shareholder loan stays out of the result; 3,57 appears only as the
    # decision-relevant alternative in the calculation path ("Einbezogen wäre der Wert 3,57")
    assert "Einbezogen wäre der Wert 3,57 (nicht eingehalten)" in flat
    assert text.count("3,57") == 1
    head = text.split("Rechenweg und Quellen")[0]
    assert "3,57" not in head
    # order: lead → table → footnotes → Hinweise → links → paths → signature
    order = ["ist abgeschlossen", "Am engsten", "Plan 2027  Grenze", "¹ gem.", "Hinweise",
             "Rechenweg und Quellen", "Quelle im Vertrag", "EBITDA (Herleitung)",
             "Mit freundlichen Grüßen"]  # fmt: skip
    positions = [text.index(o) for o in order]
    assert positions == sorted(positions)
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
    assert "deterministischen Rechenkern" in html
    assert "&lt;" not in html.split("<body")[1][:200]  # no double escaping of the layout
    assert 'href="#rw-v4-dvg"' in html
    assert 'name="rw-v4-dvg"' in html
    assert 'href="#rw-ebitda"' in html
    assert "&#9679;" in html  # status dots are characters, not images
    assert "border-left:3px solid #0F6CBD" in html  # lead box
    assert 'title="Verbindlichkeiten gegenüber Kreditinstituten"' in html  # Excel label
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


def test_ask_variant(inbound) -> None:  # type: ignore[no-untyped-def]
    ask = render.ask_context(
        [finding(ErrorCode.ATTACHMENT_MISSING)], inbound=inbound, run_id=RUN_ID, wait_days=7
    )
    html, text = render.render(ask)
    assert text.startswith("Guten Tag Dr. Max Mustermann,")
    assert "Ich benötige noch den Wirtschaftsplan als Excel-Datei (.xlsx)." in text
    assert "- Bitte den Wirtschaftsplan als Excel-Datei (.xlsx) anhängen." in text
    assert "So geht es weiter: Antworten Sie einfach auf diese Mail" in text
    assert "Ich warte 7 Tage" in text
    assert "deterministisch" not in text
    assert f"Referenz: {RUN_ID}" in text
    assert "border-left:3px solid #D99A1E" in html  # amber lead box
    multiple = render.ask_context(
        [finding(ErrorCode.ATTACHMENT_MULTIPLE, files="A.xlsx oder B.xlsx")],
        inbound=inbound, run_id=RUN_ID, wait_days=7,
    )  # fmt: skip
    assert "welche Datei ich prüfen soll" in render.render(multiple)[1]


def test_block_variant(inbound) -> None:  # type: ignore[no-untyped-def]
    block = render.block_context(
        [finding(ErrorCode.PLAN_UNBALANCED, assets="313.200", liabilities="312.900")],
        inbound=inbound, run_id=RUN_ID, file="Plan.xlsx", header=None, wait_days=7,
    )  # fmt: skip
    html, text = render.render(block)
    assert "Die Datei Plan.xlsx kann ich so nicht auswerten" in text
    assert "- Bilanz nicht ausgeglichen: Aktiva 313.200, Passiva 312.900 T€." in text
    assert "mit dem korrigierten Plan im Anhang" in text
    assert "border-left:3px solid #C62828" in html  # red lead box
    assert "<b>So geht es weiter:</b>" in html


def test_other_and_error_variants(inbound) -> None:  # type: ignore[no-untyped-def]
    html, text = render.render(render.other_context(inbound=inbound, run_id=RUN_ID, question=False))
    assert "prüfe die Financial Covenants" in text
    assert "Andere Anfragen kann ich nicht bearbeiten." in text
    assert "So geht es weiter: Für eine Prüfung senden Sie mir bitte den Wirtschaftsplan" in text
    assert "border-left:3px solid #8A8A8A" in html
    _, text = render.render(render.other_context(inbound=inbound, run_id=RUN_ID, question=True))
    assert "Rückfragen zu früheren Ergebnissen" in text
    _, text = render.render(render.error_context(inbound=inbound, run_id=RUN_ID))
    assert "unerwarteter Fehler" in text
    assert f"Referenz {RUN_ID}" in text


def test_partial_variant(report: Report, inbound) -> None:  # type: ignore[no-untyped-def]
    """PARTIAL keeps the full result mail; the row is grey with the reason in the footnote and
    a calculation path that says why."""
    dscr = next(r for r in report.results if r.covenant_id == "V3-DSCR")
    reason = "Darlehensspiegel (Tilgungen) fehlt."
    broken = dscr.model_copy(
        update={"status": "not_computable", "value_raw": None, "value_rounded": None,
                "headroom": None, "components": None, "reason": reason,
                "sensitivity": None, "tight": False}
    )  # fmt: skip
    partial = report.model_copy(
        update={"results": [broken if r is dscr else r for r in report.results],
                "overall": "incomplete"}
    )  # fmt: skip
    ctx = render.result_context(partial, inbound=inbound, extra_findings=[], run_id=RUN_ID)
    html, text = render.render(ctx)
    assert "3 von 4 Financial Covenants eingehalten; nicht berechenbar" in text
    assert f"² gem. Vertrag 3, § 13 Abs. 1 S. 5 – nicht berechenbar: {reason}" in text
    assert f"Ergebnis            nicht berechenbar: {reason}" in text
    row = next(r for r in ctx.rows if r.covenant == dscr.name_de)
    assert (row.status, row.value, row.met) == ("open", "–", "nicht berechenbar")
    assert "color:#8A8A8A" in html
    assert "border-left:3px solid #D99A1E" in html  # amber lead: incomplete


def test_alternative_assumption_in_lead(report: Report, inbound) -> None:  # type: ignore[no-untyped-def]
    """An ambiguous plan fact that flips the outcome is named in the lead box."""
    from finn.models.result import Alternative

    dvg = next(r for r in report.results if r.covenant_id == "V4-DVG")
    alt = Alternative(assumption_de="ohne Gesellschafterdarlehen", value_rounded=dvg.value_rounded,
                      status="met")  # fmt: skip
    breached = dvg.model_copy(update={"status": "breached", "alternatives": [alt]})
    rep = report.model_copy(
        update={"results": [breached if r is dvg else r for r in report.results]}
    )
    ctx = render.result_context(rep, inbound=inbound, extra_findings=[], run_id=RUN_ID)
    lead = " ".join(ctx.summary)
    assert "hängt an einer im Plan nicht eindeutig belegten Annahme" in lead
    assert "ohne Gesellschafterdarlehen: 3,37 (eingehalten)" in lead
    assert ctx.tone == "bad"


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
