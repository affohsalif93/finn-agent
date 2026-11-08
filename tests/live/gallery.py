"""Gallery of a live run: `out/replies/index.html` (git-ignored, contains test mail only).

Summary table on top, then per case: the request (from, subject, body, attachments), expected
vs. actual (pass / fail, problems), and each of Finn's replies as a mail client shows it (HTML
part in a sandboxed iframe via `srcdoc`, plain-text part next to it).
"""

import html
from datetime import UTC, datetime
from pathlib import Path

from driver import CaseResult

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "out" / "replies"

_CSS = """
:root{--bg:#f6f7f9;--card:#fff;--ink:#1d2330;--muted:#5d6675;--line:#dde1e7;--ok:#1a7f37;
--bad:#c62828;--chip:#eef1f5}
@media (prefers-color-scheme: dark){:root{--bg:#14171c;--card:#1d2128;--ink:#e6e9ee;
--muted:#9aa3b1;--line:#30363f;--ok:#4ac26b;--bad:#ff6b6b;--chip:#272c35}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);
font:14px/1.45 system-ui,Segoe UI,Roboto,sans-serif}
main{max-width:1200px;margin:0 auto;padding:24px 16px}h1{font-size:22px;margin:0 0 4px}
.meta{color:var(--muted);margin-bottom:20px}
table.sum{width:100%;border-collapse:collapse;background:var(--card);margin-bottom:28px}
.sum th,.sum td{border-bottom:1px solid var(--line);padding:6px 8px;text-align:left;
vertical-align:top}.sum th{color:var(--muted);font-weight:600}
.pass{color:var(--ok);font-weight:700}.fail{color:var(--bad);font-weight:700}
section.case{background:var(--card);border:1px solid var(--line);border-radius:8px;
padding:16px;margin-bottom:24px}section.case h2{font-size:17px;margin:0 0 8px}
.chip{display:inline-block;background:var(--chip);border-radius:10px;padding:1px 8px;
margin:0 4px 4px 0;font-size:12px}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:12px}
@media (max-width:800px){.grid{grid-template-columns:1fr}}
pre{white-space:pre-wrap;word-break:break-word;background:var(--chip);padding:8px;
border-radius:6px;margin:0;font:12px/1.4 ui-monospace,Consolas,monospace;max-height:520px;
overflow:auto}
iframe{width:100%;height:520px;border:1px solid var(--line);border-radius:6px;background:#fff}
.req{margin-bottom:10px}.req dt{color:var(--muted);float:left;width:90px}
.req dd{margin:0 0 2px 90px}
h3{font-size:14px;margin:14px 0 6px}ul.problems{color:var(--bad);margin:4px 0}
"""


def _e(text: object) -> str:
    return html.escape(str(text))


def _expected(r: CaseResult) -> str:
    if not r.case.replies:
        return "keine Antwort"
    return " → ".join(x.kind for x in r.case.replies)


def _actual(r: CaseResult) -> str:
    return " → ".join(x.kind for x in r.replies) or "keine Antwort"


def write(results: list[CaseResult], *, mode: str, out_dir: Path = OUT) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    passed = sum(r.passed for r in results)
    rows = []
    for r in results:
        verdict = '<span class="pass">ok</span>' if r.passed else '<span class="fail">FEHLER</span>'
        rows.append(
            f'<tr><td><a href="#{_e(r.case.name)}">{_e(r.case.name)}</a></td>'
            f"<td>{_e(r.case.title)}</td><td>{_e(r.case.severity)}</td>"
            f"<td>{_e(_expected(r))}</td><td>{_e(_actual(r))}</td>"
            f"<td>{_e(', '.join(r.codes) or '–')}</td><td>{r.seconds:.0f} s</td>"
            f"<td>{verdict}</td></tr>"
        )
    sections = [_section(r) for r in results]
    stamp = datetime.now(UTC).strftime("%d.%m.%Y %H:%M UTC")
    page = f"""<!doctype html><html lang="de"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Finn Testmail-Galerie</title><style>{_CSS}</style></head><body><main>
<h1>Finn · Testfälle gegen lokalen Mailserver</h1>
<div class="meta">{_e(stamp)} · Modus: {_e(mode)} · {passed}/{len(results)} bestanden</div>
<table class="sum"><thead><tr><th>Fall</th><th>Beschreibung</th><th>Schwere</th>
<th>Erwartet</th><th>Erhalten</th><th>Codes</th><th>Dauer</th><th>Ergebnis</th></tr></thead>
<tbody>{"".join(rows)}</tbody></table>
{"".join(sections)}
</main></body></html>"""
    path = out_dir / "index.html"
    path.write_text(page, encoding="utf-8")
    return path


def _section(r: CaseResult) -> str:
    verdict = (
        '<span class="pass">bestanden</span>' if r.passed else '<span class="fail">FEHLER</span>'
    )
    parts = [
        f'<section class="case" id="{_e(r.case.name)}"><h2>{_e(r.case.name)} · '
        f"{_e(r.case.title)} · {verdict}</h2>",
        f'<div><span class="chip">erwartet: {_e(_expected(r))}</span>'
        f'<span class="chip">erhalten: {_e(_actual(r))}</span>'
        + "".join(f'<span class="chip">{_e(c)}</span>' for c in r.codes)
        + "</div>",
    ]
    if r.problems:
        parts.append(
            '<ul class="problems">' + "".join(f"<li>{_e(p)}</li>" for p in r.problems) + "</ul>"
        )
    for n, s in enumerate(r.sent, 1):
        files = ", ".join(f"{name} ({size / 1024:.0f} KB)" for name, size in s.attachments)
        parts.append(
            f"<h3>Anfrage {n}{' (dieselbe Mail erneut)' if s.resend else ''}</h3>"
            f'<dl class="req"><dt>Von</dt><dd>{_e(s.sender)}</dd><dt>An</dt><dd>{_e(s.to)}</dd>'
            f"<dt>Betreff</dt><dd>{_e(s.subject)}</dd><dt>Anhänge</dt>"
            f"<dd>{_e(files or 'keine')}</dd><dt>Message-ID</dt><dd>{_e(s.message_id)}</dd></dl>"
            f"<pre>{_e(s.body)}</pre>"
        )
    for n, reply in enumerate(r.replies, 1):
        parts.append(
            f"<h3>Antwort {n} von Finn · {_e(reply.kind)} · {_e(reply.subject)}</h3>"
            f'<div class="meta">{_e(reply.from_)} → {_e(reply.to)} · {_e(reply.date)} · '
            f"Referenz {_e(reply.run_id)}</div>"
            f'<div class="grid"><iframe sandbox="" srcdoc="{_e(reply.html)}" '
            f'title="HTML-Teil"></iframe><pre>{_e(reply.text)}</pre></div>'
        )
    if not r.replies:
        parts.append('<p class="meta">Keine Antwort von Finn (erwartet: '
                     f"{_e(_expected(r))}).</p>")  # fmt: skip
    parts.append("</section>")
    return "".join(parts)
