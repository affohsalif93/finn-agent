"""LLM client: settings, missing key, SDK retries / timeouts, exception mapping (no network)."""

from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx2
import openai
import pytest
from pydantic import BaseModel

from finn.config import DEFAULT_OPENAI_MODEL, Settings, get_settings
from finn.llm import client
from finn.llm.client import LLMOutputInvalid, LLMUnavailable, complete_structured


class Answer(BaseModel):
    value: int


@pytest.fixture(autouse=True)
def isolated_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[None]:
    """No `.env` (the repo root has a real key), no host variables, fresh settings cache."""
    monkeypatch.chdir(tmp_path)
    for key in ("OPENAI_API_KEY", "OPENAI_MODEL", "OPENAI_MAX_RETRIES", "OPENAI_TIMEOUT_SECONDS"):
        monkeypatch.delenv(key, raising=False)
    get_settings.cache_clear()
    client._make_client.cache_clear()
    yield
    get_settings.cache_clear()


class FakeResponses:
    def __init__(self, outcomes: list[Any]) -> None:
        self.outcomes = outcomes
        self.calls: list[dict[str, Any]] = []

    def parse(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def fake_client(monkeypatch: pytest.MonkeyPatch, outcomes: list[Any]) -> FakeResponses:
    responses = FakeResponses(outcomes)
    fake = SimpleNamespace(responses=responses)
    monkeypatch.setattr(client, "_make_client", lambda *a: fake)
    return responses


def parsed(value: int | None, *, status: str = "completed", refusal: str | None = None) -> Any:
    content = [SimpleNamespace(type="refusal", refusal=refusal)] if refusal else []
    usage = SimpleNamespace(
        input_tokens=100,
        output_tokens=20,
        input_tokens_details=SimpleNamespace(cached_tokens=10),
        output_tokens_details=SimpleNamespace(reasoning_tokens=5),
    )
    return SimpleNamespace(
        status=status,
        incomplete_details=SimpleNamespace(reason="max_output_tokens"),
        output_parsed=Answer(value=value) if value is not None else None,
        output=[SimpleNamespace(content=content)],
        usage=usage,
        model="gpt-6.1-sol-2026-10-01",
    )


def call(**kwargs: Any) -> Answer:
    return complete_structured(system="s", user="u", schema=Answer, purpose="test", **kwargs)


REQUEST = httpx2.Request("POST", "https://api.openai.com/v1/responses")


def status_error(cls: type[openai.APIStatusError], code: int, message: str) -> Exception:
    return cls(message, response=httpx2.Response(code, request=REQUEST), body=None)


def test_empty_model_falls_back_to_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_MODEL", "")
    assert Settings().openai_model == DEFAULT_OPENAI_MODEL
    monkeypatch.setenv("OPENAI_MODEL", "  gpt-6-luna ")
    assert Settings().openai_model == "gpt-6-luna"


def test_empty_key_in_env_file_is_none(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("OPENAI_API_KEY=\nOPENAI_MODEL=\n", encoding="utf-8")
    settings = Settings()
    assert settings.openai_api_key is None
    assert settings.openai_model == DEFAULT_OPENAI_MODEL


def test_missing_key_raises_without_calling(monkeypatch: pytest.MonkeyPatch) -> None:
    responses = fake_client(monkeypatch, [])
    with pytest.raises(LLMUnavailable, match="OPENAI_API_KEY"):
        call()
    assert responses.calls == []


def test_success_returns_parsed_and_records_usage(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    responses = fake_client(monkeypatch, [parsed(7)])
    before = len(client.usage_log())

    assert call(trace_id="wf-1", session_id="thread-1").value == 7

    sent = responses.calls[0]
    assert sent["model"] == DEFAULT_OPENAI_MODEL
    assert sent["text_format"] is Answer
    assert sent["instructions"] == "s"
    assert sent["input"] == "u"
    assert sent["store"] is False
    assert "temperature" not in sent  # reasoning model family: not sent
    usage = client.usage_log()[before:]
    assert [(u.input_tokens, u.cached_input_tokens, u.output_tokens) for u in usage] == [
        (100, 10, 20)
    ]


def test_temperature_sent_for_non_reasoning_model(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    responses = fake_client(monkeypatch, [parsed(1)])
    call(model="gpt-4.1-mini", temperature=0)
    assert responses.calls[0]["temperature"] == 0


def test_temperature_rejected_is_retried_without(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    error = status_error(openai.BadRequestError, 400, "Unsupported parameter: 'temperature'")
    responses = fake_client(monkeypatch, [error, parsed(3)])
    assert call(model="gpt-4.1-mini").value == 3
    assert "temperature" in responses.calls[0]
    assert "temperature" not in responses.calls[1]


def test_sdk_gets_timeout_and_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("OPENAI_MAX_RETRIES", "5")
    monkeypatch.setenv("OPENAI_TIMEOUT_SECONDS", "42")
    seen: dict[str, Any] = {}

    class FakeOpenAI:
        def __init__(self, **kwargs: Any) -> None:
            seen.update(kwargs)
            self.responses = FakeResponses([parsed(1)])

    monkeypatch.setattr(client.openai, "OpenAI", FakeOpenAI)
    call()
    assert seen == {"api_key": "sk-test", "timeout": 42.0, "max_retries": 5}


@pytest.mark.parametrize(
    "error",
    [
        openai.APIConnectionError(request=REQUEST),
        openai.APITimeoutError(request=REQUEST),
        status_error(openai.RateLimitError, 429, "rate limited"),
        status_error(openai.InternalServerError, 500, "boom"),
        status_error(openai.AuthenticationError, 401, "bad key"),
        status_error(openai.NotFoundError, 404, "model not found"),
    ],
    ids=["connection", "timeout", "rate_limit", "server", "auth", "not_found"],
)
def test_errors_after_sdk_retries_map_to_unavailable(
    monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    fake_client(monkeypatch, [error])
    with pytest.raises(LLMUnavailable) as info:
        call()
    assert info.value.__cause__ is error
    assert "sk-test" not in str(info.value)


def test_refusal_and_incomplete(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    fake_client(monkeypatch, [parsed(None, refusal="I can't"), parsed(1, status="incomplete")])
    with pytest.raises(LLMOutputInvalid, match="I can't"):
        call()
    with pytest.raises(LLMUnavailable, match="max_output_tokens"):
        call()


def test_invalid_answer_maps_to_output_invalid(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    try:
        Answer.model_validate({"value": "x"})
    except Exception as exc:  # pydantic.ValidationError, as raised by the SDK parser
        validation_error = exc
    fake_client(monkeypatch, [validation_error])
    with pytest.raises(LLMOutputInvalid):
        call()
