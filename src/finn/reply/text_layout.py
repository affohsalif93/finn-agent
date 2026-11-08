"""Plain-text layout of the reply: result table and calculation paths."""

import textwrap
from collections.abc import Sequence
from typing import TYPE_CHECKING

from finn.reply.calc_path import EbitdaBlock, InputRow, PathBlock
from finn.text import text_table

if TYPE_CHECKING:
    from finn.reply.render import Row

LABEL_WIDTH = 20  # label column of the calculation path
TEXT_WIDTH = 100


def result_table(headers: list[str], rows: Sequence["Row"]) -> str:
    """The result table of the plain-text mail."""
    cells = [
        [
            f"{r.contract.replace(' · ', ' ', 1)} · {r.covenant}{r.mark}",
            r.value,
            r.threshold,
            r.headroom,
            r.met,
        ]
        for r in rows
    ]
    return "\n".join(text_table(headers, cells, max_width=70))


def _labelled(label: str, text: str) -> list[str]:
    indent = " " * LABEL_WIDTH
    wrapped = textwrap.wrap(text, TEXT_WIDTH - LABEL_WIDTH) or [""]
    return [f"{label:<{LABEL_WIDTH}}{wrapped[0]}", *(indent + w for w in wrapped[1:])]


def _input_lines(rows: Sequence[InputRow]) -> list[str]:
    if not rows:
        return []
    w_label = max(len("Position"), *(len(r.label) for r in rows)) + 2
    w_value = max(len("T€"), *(len(r.value) for r in rows))
    lines = [f"{'Position':<{w_label}}{'T€':>{w_value}}   Zelle"]
    lines += [f"{r.label:<{w_label}}{r.value:>{w_value}}   {r.cell}".rstrip() for r in rows]
    return lines


def text_block(title: str, head: str, rows: list[tuple[str, str | list[InputRow]]]) -> str:
    gap = max(2, TEXT_WIDTH - len(title) - len(head))
    out = [f"{title}{' ' * gap}{head}" if head else title]
    for label, content in rows:
        if isinstance(content, list):
            lines = _input_lines(content)
            for i, line in enumerate(lines):
                out.append(f"{label if i == 0 else '':<{LABEL_WIDTH}}{line}")
        else:
            out += _labelled(label, content)
    return "\n".join(out)


def path_text(b: PathBlock) -> str:
    rows: list[tuple[str, str | list[InputRow]]] = []
    if b.source:
        rows.append(("Quelle im Vertrag", b.source))
    if b.formula:
        rows.append(("Formel", b.formula))
    if b.inputs:
        rows.append(("Eingangswerte", b.inputs))
    rows.append(("Ergebnis", b.result))
    for i, s in enumerate(b.specials):
        rows.append(("Besonderheit" if i == 0 else "", s))
    return text_block(b.title, b.head, rows)


def ebitda_text(b: EbitdaBlock) -> str:
    rows: list[tuple[str, str | list[InputRow]]] = [
        ("Verwendet in", b.used_by),
        ("Eingangswerte", b.inputs),
    ]
    for i, s in enumerate(b.specials):
        rows.append(("Besonderheit" if i == 0 else "", s))
    return text_block(b.title, "", rows)
