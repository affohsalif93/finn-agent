"""TEST ONLY: a deterministic stand-in for `client.complete_structured` (`FINN_LLM_FAKE=1`).

Used by the live mail tests and `finn demo` against the local test mail server (compose profile
`testmail`), so a run is reproducible and needs no API key. It never runs unless the setting is
on; production leaves it off (the default) and `complete_structured` logs a warning when it is.

- `intent`: keyword rules on the pseudonymised thread (as the LLM sees it): a workbook or
  covenant words → `covenant_check`, else `other`; the plan year from the thread text.
- `reply_polish`: echoes the paragraphs unchanged (placeholders intact → number check passes).
- anything else (contract extraction): `LLMUnavailable`.
"""

import re

from pydantic import BaseModel

_WORDS = re.compile(
    r"(covenant|prüf|pruef|kennzahl|wirtschaftsplan|planung|\bplan\b|dscr|eigenkapital|"
    r"verschuldung|auswert|berechn|anbei|anhang|kreditvertr|darlehen|excel)",
    re.IGNORECASE,
)
_YEAR = re.compile(r"(?:wirtschafts)?plan(?:jahr|ung)?\D{0,20}?(20\d{2})", re.IGNORECASE)
_FILES = re.compile(r"^Anhänge: (?!keine$)(.+)$", re.MULTILINE)


def complete[T: BaseModel](*, user: str, schema: type[T], purpose: str) -> T:
    from finn.llm.client import LLMUnavailable

    if purpose == "intent":
        return schema.model_validate(_intent(user))
    if purpose == "reply_polish":
        return schema.model_validate(_echo(user))
    raise LLMUnavailable(f"{purpose}: no fake answer (FINN_LLM_FAKE)")


def _intent(user: str) -> dict[str, object]:
    thread = user.partition("Thread:\n")[2] or user
    has_file = bool(_FILES.search(thread))
    covenant = has_file or bool(_WORDS.search(thread))
    year = next((int(m.group(1)) for m in _YEAR.finditer(thread)), None)
    return {
        "intent": "covenant_check" if covenant else "other",
        "plan_year": year,
        "contract_ids": [],
        "confidence": 0.9,
        "assumptions": [],
        "unsupported": [],
    }


def _items(block: str) -> list[str]:
    return [line[2:] for line in block.splitlines() if line.startswith("- ")]


def _echo(user: str) -> dict[str, object]:
    summary, _, notes = user.partition("\n\nHinweise:\n")
    return {"summary": _items(summary.partition("Zusammenfassung:\n")[2]), "notes": _items(notes)}
