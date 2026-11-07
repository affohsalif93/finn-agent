"""Data minimisation before an LLM call (ARCHITECTURE §8.1, D21).

- `clean_body`: strip quoted history, signatures and mobile footers from a mail body.
- `Pseudonyms`: replace emails, phone numbers, IBANs and given person names with placeholders
  (`[EMAIL_1]`, `[PHONE_1]`, `[IBAN_1]`, `[PERSON_1]`) and restore them afterwards. The same
  original always gets the same placeholder; the mapping stays local (run folder
  `pseudonyms.json`), never in a prompt.
"""

import re
from collections.abc import Iterable

_EMAIL = re.compile(r"[\w.!#$%&'*+/=?^`{|}~-]+@[\w-]+(?:\.[\w-]+)+", re.UNICODE)
# IBAN: country code, check digits, 11–30 alphanumerics, optionally grouped by 4 with spaces.
_IBAN = re.compile(r"\b[A-Z]{2}\d{2}(?:[ ]?[A-Z0-9]{4}){2,7}(?:[ ]?[A-Z0-9]{1,3})?\b")
# Phone: starts with +, 00 or 0 and has at least 8 digits overall; separators space / - ( ).
_PHONE = re.compile(r"(?<![\w.,])(?:\+|00|0)\d[\d\s/()\-]{6,}\d(?![\w.,]?\d)")
_PLACEHOLDER = re.compile(r"\[(EMAIL|PHONE|IBAN|PERSON)_\d+\]")
_TITLES = {"dr", "dr.", "prof", "prof.", "dipl.-kfm.", "dipl.-ing.", "herr", "frau", "mag."}

# Quoted history: everything from the first reply header on is dropped.
_QUOTE_HEADERS = [
    re.compile(
        r"^\s*-{2,}\s*(original message|ursprüngliche nachricht|original-nachricht)\b", re.I
    ),
    re.compile(r"^\s*(am|on)\s.{4,200}\s(schrieb|wrote)\b.*:\s*$", re.I),
    re.compile(r"^\s*_{10,}\s*$"),  # Outlook separator line before the header block
]
_OUTLOOK_FROM = re.compile(r"^\s*\**(von|from)\s*:\**\s", re.I)
_OUTLOOK_NEXT = re.compile(r"^\s*\**(gesendet|sent|datum|date|an|to)\s*:", re.I)
_FORWARD = re.compile(
    r"(forwarded message|weitergeleitete nachricht|begin forwarded message|"
    r"anfang der weitergeleiteten)",
    re.I,
)
_GREETING = re.compile(
    r"^\s*(mit freundlichen grüßen|mit freundlichem gruß|freundliche grüße|viele grüße|"
    r"beste grüße|herzliche grüße|liebe grüße|gruß|grüße|mfg|best regards|kind regards|"
    r"regards|best|cheers)\s*[,!.]?\s*$",
    re.I,
)
_MOBILE = re.compile(r"^\s*(von meinem \w+ gesendet|sent from my \w+|gesendet von)", re.I)
_SIGNATURE_WINDOW = 15


def clean_body(text: str, *, forwarded: bool = False) -> str:
    """Body without quoted history (kept for forwards: the forwarded text *is* the request),
    `> ` quotes, signature and mobile footer."""
    lines = (text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    out: list[str] = []
    for i, line in enumerate(lines):
        if not forwarded and _is_quote_start(lines, i):
            break
        if line.lstrip().startswith(">"):
            continue
        if line.rstrip() in ("--", "-- ") or _MOBILE.match(line):
            break
        out.append(line.rstrip())
    out = _strip_greeting(out)
    return "\n".join(out).strip()


def _is_quote_start(lines: list[str], i: int) -> bool:
    line = lines[i]
    if any(p.match(line) for p in _QUOTE_HEADERS):
        return True
    if _FORWARD.search(line):
        return False
    if _OUTLOOK_FROM.match(line):
        following = lines[i + 1 : i + 4]
        return any(_OUTLOOK_NEXT.match(n) for n in following)
    return False


def _strip_greeting(lines: list[str]) -> list[str]:
    non_empty = [i for i, line in enumerate(lines) if line.strip()]
    window = set(non_empty[-_SIGNATURE_WINDOW:])
    for i in reversed(non_empty):
        if i in window and _GREETING.match(lines[i]):
            return lines[:i]
    return lines


class Pseudonyms:
    """Placeholder ↔ original mapping for one run."""

    def __init__(self, mapping: dict[str, str] | None = None) -> None:
        self.mapping: dict[str, str] = dict(mapping or {})  # placeholder -> original

    def _token(self, kind: str, original: str) -> str:
        for placeholder, value in self.mapping.items():
            if value == original and placeholder.startswith(f"[{kind}_"):
                return placeholder
        n = 1 + sum(1 for p in self.mapping if p.startswith(f"[{kind}_"))
        placeholder = f"[{kind}_{n}]"
        self.mapping[placeholder] = original
        return placeholder

    def pseudonymise(self, text: str, *, names: Iterable[str] = ()) -> str:
        """Replace emails, IBANs, phone numbers, then the given person names."""
        text = _EMAIL.sub(lambda m: self._token("EMAIL", m.group(0)), text)
        text = _IBAN.sub(lambda m: self._token("IBAN", m.group(0)), text)
        text = _PHONE.sub(lambda m: self._phone(m.group(0)), text)
        for name in _name_variants(names):
            pattern = re.compile(rf"(?<!\w){re.escape(name)}(?!\w)", re.IGNORECASE)
            if pattern.search(text):  # a placeholder only for names that occur
                text = pattern.sub(self._token("PERSON", name).replace("\\", "\\\\"), text)
        return text

    def _phone(self, match: str) -> str:
        digits = re.sub(r"\D", "", match)
        if len(digits) < 8:
            return match
        return self._token("PHONE", match.strip())

    def restore(self, text: str) -> str:
        return _PLACEHOLDER.sub(lambda m: self.mapping.get(m.group(0), m.group(0)), text)


def _name_variants(names: Iterable[str]) -> list[str]:
    """Full names first, then their parts (≥ 3 letters, no titles), longest first."""
    variants: list[str] = []
    for name in names:
        clean = " ".join((name or "").replace('"', " ").split())
        if not clean or "@" in clean:
            continue
        if "," in clean:  # "Mustermann, Max" → also "Max Mustermann"
            last, _, first = clean.partition(",")
            clean = f"{first.strip()} {last.strip()}".strip()
        parts = [p for p in clean.split() if p.lower() not in _TITLES]
        full = " ".join(parts)
        for v in [full, *parts]:
            if len(v) >= 3 and v not in variants:
                variants.append(v)
    return sorted(variants, key=len, reverse=True)
