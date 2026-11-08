"""The TEST-ONLY LLM stand-in (`FINN_LLM_FAKE`, llm/fake.py) and its guard in the client."""

from collections.abc import Iterator
from pathlib import Path

import pytest

from finn.config import get_settings
from finn.llm import client
from finn.reply.render import PolishAnswer
from finn.request.intent import IntentAnswer


@pytest.fixture
def fake_on(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("FINN_LLM_FAKE", "1")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _intent(thread: str) -> IntentAnswer:
    return client.complete_structured(
        system="s", user=f"Verträge:\n- V1\n\nThread:\n{thread}", schema=IntentAnswer,
        purpose="intent",
    )  # fmt: skip


def test_intent_rules(fake_on: None) -> None:
    plan = _intent("--- Nachricht 1 ---\nBetreff: Plan\nAnhänge: W.xlsx (xlsx)\n\nanbei")
    assert plan.intent == "covenant_check"
    year = _intent("Betreff: Covenant-Prüfung Wirtschaftsplan 2027\nAnhänge: keine\n\nHallo")
    assert (year.intent, year.plan_year) == ("covenant_check", 2027)
    other = _intent("Betreff: Kurze Frage\nAnhänge: keine\n\nWann öffnet die Kantine?")
    assert other.intent == "other"


def test_polish_echoes_paragraphs(fake_on: None) -> None:
    answer = client.complete_structured(
        system="s", user="Zusammenfassung:\n- A {{N1}}\n- B\n\nHinweise:\n- C {{NAME1}}",
        schema=PolishAnswer, purpose="reply_polish",
    )  # fmt: skip
    assert answer.summary == ["A {{N1}}", "B"]
    assert answer.notes == ["C {{NAME1}}"]


def test_other_purposes_unavailable(fake_on: None) -> None:
    with pytest.raises(client.LLMUnavailable):
        client.complete_structured(system="s", user="u", schema=PolishAnswer, purpose="extract")


def test_off_by_default(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)  # no .env
    monkeypatch.delenv("FINN_LLM_FAKE", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    get_settings.cache_clear()
    try:
        assert get_settings().finn_llm_fake is False
        with pytest.raises(client.LLMUnavailable, match="OPENAI_API_KEY"):
            _intent("Anhänge: keine\n\nx")
    finally:
        get_settings.cache_clear()
