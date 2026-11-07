"""OpenAI client: one structured-output call with timeouts, retries and a clear failure type.

`complete_structured` sends `system` + `user` to the Responses API with the pydantic `schema` as a
strict JSON schema and returns the parsed object. Retries with exponential backoff (connection
errors, timeouts, 408/409/429/5xx) are done by the SDK (`max_retries`); everything that still
fails, a missing API key, a refusal, a truncated answer or an answer that fails validation ends
in `LLMUnavailable`, so callers have exactly one exception to handle (template fallback, D16).

`purpose`, `trace_id` and `session_id` are logged now and become Langfuse trace attributes later
(D13: trace = workflow ID, session = mail thread). Prompts are never logged (they may hold
contract text); only sizes, model and token usage are.
"""

import re
import time
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

import openai
import structlog
from pydantic import BaseModel, ValidationError

from finn.config import get_settings

log = structlog.get_logger(__name__)

# Reasoning model families reject `temperature`; for them it is not sent (determinism comes from
# the schema and the review, not from sampling settings).
_NO_TEMPERATURE = re.compile(r"^(o\d|gpt-5|gpt-6)")


class LLMUnavailable(Exception):
    """The LLM could not deliver a valid answer (no key, retries exhausted, refusal, …)."""


class LLMOutputInvalid(LLMUnavailable):
    """The model answered, but the answer is not a valid instance of the schema."""


@dataclass(frozen=True)
class CallUsage:
    purpose: str
    model: str
    input_tokens: int
    cached_input_tokens: int
    output_tokens: int
    reasoning_tokens: int
    seconds: float


_USAGE: list[CallUsage] = []


def usage_log() -> list[CallUsage]:
    """Token usage of every successful call in this process (for cost reports in the CLI)."""
    return list(_USAGE)


@lru_cache(maxsize=4)
def _make_client(api_key: str, timeout: float, max_retries: int) -> openai.OpenAI:
    return openai.OpenAI(api_key=api_key, timeout=timeout, max_retries=max_retries)


def supports_temperature(model: str) -> bool:
    return _NO_TEMPERATURE.match(model) is None


def complete_structured[T: BaseModel](
    *,
    system: str,
    user: str,
    schema: type[T],
    purpose: str,
    trace_id: str | None = None,
    session_id: str | None = None,
    model: str | None = None,
    temperature: float | None = 0,
) -> T:
    """One structured call; returns a validated `schema` instance or raises LLMUnavailable."""
    settings = get_settings()
    model_name = model or settings.openai_model
    bound = log.bind(purpose=purpose, model=model_name, trace_id=trace_id, session_id=session_id)
    key = settings.openai_api_key.get_secret_value() if settings.openai_api_key else ""
    if not key:
        bound.warning("llm_unavailable", reason="no_api_key")
        raise LLMUnavailable("OPENAI_API_KEY is not set")

    client = _make_client(key, settings.openai_timeout_seconds, settings.openai_max_retries)
    kwargs: dict[str, Any] = {
        "model": model_name,
        "instructions": system,
        "input": user,
        "text_format": schema,
        "store": False,  # data minimisation (D21): nothing kept on the provider side
    }
    if temperature is not None and supports_temperature(model_name):
        kwargs["temperature"] = temperature

    bound.info("llm_call", system_chars=len(system), user_chars=len(user), schema=schema.__name__)
    started = time.monotonic()
    try:
        try:
            response = client.responses.parse(**kwargs)
        except openai.BadRequestError as exc:
            # A model that rejects `temperature` despite the name check: retry once without it.
            if "temperature" in kwargs and "temperature" in str(exc).lower():
                kwargs.pop("temperature")
                response = client.responses.parse(**kwargs)
            else:
                raise
    except ValidationError as exc:
        bound.warning("llm_invalid_output", errors=exc.error_count())
        raise LLMOutputInvalid(f"{purpose}: answer does not match {schema.__name__}") from exc
    except openai.OpenAIError as exc:
        bound.warning("llm_unavailable", reason=type(exc).__name__)
        raise LLMUnavailable(f"{purpose}: {type(exc).__name__}: {exc}") from exc

    seconds = time.monotonic() - started
    if getattr(response, "status", None) == "incomplete":
        details = getattr(response, "incomplete_details", None)
        reason = getattr(details, "reason", None) or "unknown"
        bound.warning("llm_incomplete", reason=reason)
        raise LLMUnavailable(f"{purpose}: answer incomplete ({reason})")

    parsed = response.output_parsed
    if parsed is None:
        bound.warning("llm_no_parsed_output", refusal=_refusal(response))
        raise LLMOutputInvalid(f"{purpose}: no structured answer ({_refusal(response) or 'empty'})")

    usage = _usage(purpose, model_name, response, seconds)
    _USAGE.append(usage)
    bound.info(
        "llm_done",
        seconds=round(seconds, 1),
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        reasoning_tokens=usage.reasoning_tokens,
    )
    if not isinstance(parsed, schema):  # defensive: the SDK parses into `text_format`
        raise LLMOutputInvalid(f"{purpose}: unexpected answer type {type(parsed).__name__}")
    return parsed


def _refusal(response: Any) -> str | None:
    for item in getattr(response, "output", None) or []:
        for content in getattr(item, "content", None) or []:
            if getattr(content, "type", None) == "refusal":
                return str(getattr(content, "refusal", "refusal"))
    return None


def _usage(purpose: str, model: str, response: Any, seconds: float) -> CallUsage:
    u = getattr(response, "usage", None)

    def num(obj: Any, name: str) -> int:
        value = getattr(obj, name, 0) if obj is not None else 0
        return value if isinstance(value, int) else 0

    return CallUsage(
        purpose=purpose,
        model=str(getattr(response, "model", None) or model),
        input_tokens=num(u, "input_tokens"),
        cached_input_tokens=num(getattr(u, "input_tokens_details", None), "cached_tokens"),
        output_tokens=num(u, "output_tokens"),
        reasoning_tokens=num(getattr(u, "output_tokens_details", None), "reasoning_tokens"),
        seconds=seconds,
    )
