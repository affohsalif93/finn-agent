"""Contract PDF → page texts with page numbers (ARCHITECTURE §2.1), cached by file hash.

`read_pages` extracts every page with pdfplumber once per PDF content (sha256) and keeps the
raw texts in `data/.cache/pdf_text/<sha256>.json` (git-ignored). `mask_persons` replaces the
names of natural persons before the text goes to the LLM (D21, ARCHITECTURE §8.1): signatory
names in signature blocks, "Herr/Frau …", "gez. …", "Dr. …". Lenders, the borrower, amounts and
clauses stay unchanged: they are the content. The rules are deterministic and deliberately
narrow; organisation and role words are never masked.

Quote validation and the completeness scan run on the **masked** page texts, i.e. on exactly the
text the LLM saw.
"""

import hashlib
import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import pdfplumber

# Bump when the extraction settings change (invalidates the cache).
EXTRACTOR_VERSION = "pdfplumber-default-1"
DEFAULT_CACHE_DIR = Path("data/.cache/pdf_text")


@dataclass(frozen=True)
class ContractText:
    file: str
    sha256: str
    pages: list[str]  # index 0 = page 1, masked
    masked: dict[str, str] = field(default_factory=dict)  # placeholder → original (stays local)

    def page(self, number: int) -> str:
        return self.pages[number - 1]

    @property
    def page_count(self) -> int:
        return len(self.pages)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def read_pages(pdf: Path, cache_dir: Path | None = DEFAULT_CACHE_DIR) -> tuple[str, list[str]]:
    """(sha256, raw page texts) for `pdf`; uses / fills the cache when `cache_dir` is set."""
    data = pdf.read_bytes()
    digest = sha256_bytes(data)
    cache = cache_dir / f"{digest}.json" if cache_dir is not None else None
    if cache is not None and cache.is_file():
        try:
            cached = json.loads(cache.read_text(encoding="utf-8"))
            if cached.get("extractor") == EXTRACTOR_VERSION:
                return digest, [str(p) for p in cached["pages"]]
        except (OSError, ValueError, KeyError):
            pass  # unreadable cache: extract again
    with pdfplumber.open(pdf) as doc:
        pages = [page.extract_text() or "" for page in doc.pages]
    if cache is not None:
        cache.parent.mkdir(parents=True, exist_ok=True)
        payload = {"extractor": EXTRACTOR_VERSION, "file": pdf.name, "pages": pages}
        cache.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    return digest, pages


def load_contract_text(pdf: Path, cache_dir: Path | None = DEFAULT_CACHE_DIR) -> ContractText:
    digest, pages = read_pages(pdf, cache_dir)
    masked_pages, mapping = mask_persons(pages)
    return ContractText(file=pdf.name, sha256=digest, pages=masked_pages, masked=mapping)


def render_pages(pages: list[str]) -> str:
    """Page-tagged text for the prompt: `<page number="n">…</page>`."""
    return "\n".join(
        f'<page number="{i}">\n{text.strip()}\n</page>' for i, text in enumerate(pages, 1)
    )


# Typographic quotes, dashes, special spaces, ligatures → plain ASCII (by code point, so the
# source stays free of look-alike characters).
_DQ, _SQ, _DASH, _SPACE = '"', "'", "-", " "
_CHAR_MAP = str.maketrans(
    {
        **dict.fromkeys(map(chr, (0x201E, 0x201C, 0x201D, 0x00AB, 0x00BB)), _DQ),
        **dict.fromkeys(map(chr, (0x201A, 0x2018, 0x2019)), _SQ),
        **dict.fromkeys(map(chr, (0x2013, 0x2014, 0x2212)), _DASH),
        **dict.fromkeys(map(chr, (0x00A0, 0x202F)), _SPACE),
        chr(0x00AD): "",
        chr(0xFB01): "fi",
        chr(0xFB02): "fl",
    }
)
_WS = re.compile(r"\s+")


def normalize(text: str) -> str:
    """Whitespace collapsed, typographic quotes / dashes unified (what "verbatim" means here)."""
    return _WS.sub(" ", text.translate(_CHAR_MAP)).strip()


# ── Masking (D21) ────────────────────────────────────────────────────────────

_NAME_WORD = r"[A-ZÄÖÜ][a-zäöüß]+(?:-[A-ZÄÖÜ][a-zäöüß]+)?"
_TITLE = r"(?:(?:Dr|Prof|Dipl\.-\w+)\.?\s+)"
# "Dr. Anna Beispiel", "Prof. Dr. Max Mustermann" anywhere in the text.
_TITLED_NAME = re.compile(rf"(?:{_TITLE})+(?:{_NAME_WORD}\s+)?{_NAME_WORD}")
# "Herrn Max Mustermann", "Frau Beispiel", "gez. Max Mustermann", "i. V. Anna Beispiel".
_INTRO_NAME = re.compile(
    rf"(?P<intro>\b(?:Herrn?|Frau|gez\.|i\.\s?V\.|i\.\s?A\.|ppa\.)\s+)"
    rf"(?P<name>(?:{_TITLE})*{_NAME_WORD}(?:\s+{_NAME_WORD})?)"
)
_UNDERSCORES = re.compile(r"_{5,}")
_WORD = re.compile(rf"(?<![\wÄÖÜäöüß-]){_NAME_WORD}(?![\wÄÖÜäöüß])")

# Words of organisations and roles: never part of a person's name in a signature block.
_NOT_NAME = {
    "Aufsichtsrat", "Abteilungsleiter", "Abteilungsleiterin", "Bank", "Bereichsleiter",
    "Bereichsleiterin", "Bereichsleitung", "Direktor", "Direktorin", "Finanzen", "Firmenkunden",
    "Geschäftsführer", "Geschäftsführerin", "Geschäftsführung", "Gesellschaft", "Kaufmännische",
    "Kaufmännischer", "Kreditgeber", "Kreditnehmerin", "Leitung", "Mitglied", "Prokurist",
    "Prokuristin", "Sparkasse", "Stadtwerke", "Technische", "Technischer", "Vorstand",
    "Vorsitzender", "Vorsitzende", "Vizedirektor", "Vizedirektorin", "Agent",
}  # fmt: skip


def mask_persons(pages: list[str]) -> tuple[list[str], dict[str, str]]:
    """Replace person names by `[PERSON_n]` (same name → same placeholder, across pages).

    1. names introduced by "Herr/Frau/gez./i. V." or a title, anywhere;
    2. word runs in signature blocks that are neither body words nor org / role words;
    3. every name found so far, wherever else it occurs.
    """
    mapping: dict[str, str] = {}  # original → placeholder

    def placeholder(name: str) -> str:
        key = " ".join(name.split())
        if key not in mapping:
            mapping[key] = f"[PERSON_{len(mapping) + 1}]"
        return mapping[key]

    out = [
        _TITLED_NAME.sub(
            lambda m: placeholder(m[0]),
            _INTRO_NAME.sub(lambda m: m["intro"] + placeholder(m["name"]), text),
        )
        for text in pages
    ]
    vocabulary = _body_vocabulary(out)
    out = [_mask_signature_blocks(text, vocabulary, placeholder) for text in out]
    for name in sorted(mapping, key=len, reverse=True):
        pattern = re.compile(r"\s+".join(re.escape(part) for part in name.split()))
        out = [pattern.sub(mapping[name], text) for text in out]
    return out, {v: k for k, v in mapping.items()}


def _mask_signature_blocks(
    text: str, vocabulary: set[str], placeholder: Callable[[str], str]
) -> str:
    """Lines right after a signature line (`_____`): mask runs of two or more name words.

    A word is a name candidate only if it does not occur in the contract body (outside signature
    blocks) and is no organisation / role word: "Stadtwerke Musterstadt" and "Kaufmännische
    Geschäftsführerin" stay, "Max Mustermann" goes (also in "Geschäftsführer Max Mustermann").
    """
    lines = text.split("\n")
    for i, line in enumerate(lines):
        if not _UNDERSCORES.search(line):
            continue
        for j in range(i, min(i + 4, len(lines))):
            if j > i and _UNDERSCORES.search(lines[j]):
                break
            lines[j] = _mask_runs(lines[j], vocabulary, placeholder)
    return "\n".join(lines)


def _mask_runs(line: str, vocabulary: set[str], placeholder: Callable[[str], str]) -> str:
    runs: list[list[re.Match[str]]] = []
    current: list[re.Match[str]] = []
    for m in _WORD.finditer(line):
        candidate = m[0] not in _NOT_NAME and m[0] not in vocabulary
        adjacent = bool(current) and not line[current[-1].end() : m.start()].strip()
        if candidate and (adjacent or not current):
            current.append(m)
            continue
        if len(current) >= 2:
            runs.append(current)
        current = [m] if candidate else []
    if len(current) >= 2:
        runs.append(current)
    for run in reversed(runs):
        start, end = run[0].start(), run[-1].end()
        line = line[:start] + placeholder(line[start:end]) + line[end:]
    return line


def _body_vocabulary(pages: list[str]) -> set[str]:
    """Capitalised words outside signature blocks (names of signatories rarely occur there)."""
    words: set[str] = set()
    for text in pages:
        skip = 0
        for line in text.split("\n"):
            if _UNDERSCORES.search(line):
                skip = 3
                continue
            if skip:
                skip -= 1
                continue
            words.update(re.findall(_NAME_WORD, line))
    return words
