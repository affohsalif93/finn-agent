"""Number check (D21): every number in a polished text must come from the template text.

Numbers glued to letters (cells such as `D28`, ids such as `V4`) are not counted."""

import re
from collections.abc import Iterable
from decimal import Decimal, InvalidOperation

# German numbers: "148.200", "34,32", "3,3731", "-1.512", plain "2027"; not part of a word.
_NUMBER = re.compile(
    r"(?<![\w.,])[-−]?\d{1,3}(?:\.\d{3})+(?:,\d+)?(?![\w])|(?<![\w.,])[-−]?\d+(?:,\d+)?(?![\w])"
)


def numbers_in(text: str) -> list[str]:
    return [m.group(0) for m in _NUMBER.finditer(text or "")]


def to_value(token: str) -> Decimal | None:
    t = token.replace("−", "-").replace(".", "").replace(",", ".")
    try:
        return Decimal(t).normalize()
    except InvalidOperation:
        return None


def allowed_values(texts: Iterable[str]) -> set[Decimal]:
    out: set[Decimal] = set()
    for text in texts:
        for token in numbers_in(text):
            value = to_value(token)
            if value is not None:
                out.add(value)
                out.add(abs(value))
    return out


def unknown_numbers(text: str, allowed: set[Decimal]) -> list[str]:
    """Tokens in `text` whose value is not in `allowed` (empty list = text passes)."""
    bad: list[str] = []
    for token in numbers_in(text):
        value = to_value(token)
        if value is None or (value not in allowed and abs(value) not in allowed):
            bad.append(token)
    return bad
