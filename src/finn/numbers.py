"""Exact decimals and German number formatting, in one place (D24).

Floats from Excel go through `Decimal(str(x))`, never `Decimal(x)`. Rounding for covenants is
the calc engine's job; `format_de` only formats a value with a given number of decimals.
"""

import re
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

_THOUSANDS_ONLY = re.compile(r"^\d{1,3}(\.\d{3})+$")
_STRIP = re.compile(r"(T€|TEUR|EUR|€|%|\s|\u00a0|\u202f)")  # incl. (narrow) no-break spaces


def to_decimal(x: object) -> Decimal:
    """Convert an int, float, Decimal or (German or plain) number string to an exact Decimal.

    Raises `ValueError` for anything that is not a number (bool, None, text, NaN).
    """
    if isinstance(x, bool) or x is None:
        raise ValueError(f"not a number: {x!r}")
    if isinstance(x, Decimal):
        result = x
    elif isinstance(x, int | float):
        result = Decimal(str(x))
    elif isinstance(x, str):
        result = _parse_str(x)
    else:
        raise ValueError(f"not a number: {x!r}")
    if not result.is_finite():
        raise ValueError(f"not a finite number: {x!r}")
    return result


def _parse_str(text: str) -> Decimal:
    s = _STRIP.sub("", text).replace("\u2212", "-")  # Unicode minus sign
    negative = s.startswith("(") and s.endswith(")")
    if negative:
        s = s[1:-1]
    if "," in s:
        s = s.replace(".", "").replace(",", ".")
    elif _THOUSANDS_ONLY.match(s.lstrip("+-")):
        s = s.replace(".", "")
    try:
        value = Decimal(s)
    except InvalidOperation as exc:
        raise ValueError(f"not a number: {text!r}") from exc
    return -value if negative else value


def format_de(value: Decimal, decimals: int = 0, unit: str | None = None) -> str:
    """German format: `.` thousands, `,` decimals, ROUND_HALF_UP; e.g. "148.200 T€", "34,32 %"."""
    exponent = Decimal(1).scaleb(-decimals)
    quantized = value.quantize(exponent, rounding=ROUND_HALF_UP)
    if quantized == 0:
        quantized = abs(quantized)  # no "-0"
    text = f"{quantized:,.{decimals}f}".replace(",", "\x00").replace(".", ",")
    text = text.replace("\x00", ".")
    return f"{text} {unit}" if unit else text
