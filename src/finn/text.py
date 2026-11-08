"""Plain-text table for CLI output and the text part of the mail."""


def text_table(headers: list[str], rows: list[list[str]], max_width: int = 60) -> list[str]:
    """Left-aligned columns, header underlined; cells longer than `max_width` are cut with "…"."""
    cells = [[c if len(c) <= max_width else c[: max_width - 1] + "…" for c in r] for r in rows]
    widths = [max(len(h), *(len(r[i]) for r in cells)) for i, h in enumerate(headers)]
    line = "  ".join(h.ljust(w) for h, w in zip(headers, widths, strict=True))
    out = [line.rstrip(), "  ".join("-" * w for w in widths)]
    for r in cells:
        out.append("  ".join(c.ljust(w) for c, w in zip(r, widths, strict=True)).rstrip())
    return out
