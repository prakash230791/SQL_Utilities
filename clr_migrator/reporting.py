"""CSV / JSON / self-contained HTML report writers."""
from __future__ import annotations

import csv
import html
import json
from pathlib import Path


def write_csv(path: Path, rows: list[dict], fields: list[str] | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if fields is None:
        fields = list(rows[0].keys()) if rows else ["no_rows"]
    # utf-8-sig so Excel opens it with the right encoding
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def write_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2, default=str)


_TONES = {"PASS": "ok", "READY": "ok", "LOW": "ok", "NONE": "muted", "NO_ACTION": "muted",
          "INFO": "muted", "SKIP": "muted", "WARN": "warn", "MEDIUM": "warn",
          "READY_WITH_WARNINGS": "warn", "FAIL": "bad", "FAILED": "bad", "HIGH": "bad",
          "MANUAL_REVIEW": "bad"}

_CSS = """
:root{--bg:#f7f7f5;--fg:#1d1d1b;--card:#fff;--line:#deddd8;--muted:#6b6a64;
--ok:#1f7a4d;--warn:#9a6700;--bad:#b42318;--okb:#e7f4ec;--warnb:#fff4d6;--badb:#fdecea}
@media (prefers-color-scheme:dark){:root{--bg:#161614;--fg:#ecebe6;--card:#20201d;--line:#34332f;
--muted:#9c9a92;--ok:#6fcf97;--warn:#f2c94c;--bad:#ff8a80;--okb:#1d3326;--warnb:#3a3016;--badb:#3d1f1c}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);
font:14px/1.5 -apple-system,Segoe UI,Roboto,sans-serif}
main{max-width:1280px;margin:0 auto;padding:24px}
h1{font-size:22px;margin:0 0 4px}h2{font-size:16px;margin:32px 0 8px}
.sub{color:var(--muted);margin-bottom:20px}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px}
.card{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:12px 14px}
.card b{display:block;font-size:22px}.card span{color:var(--muted);font-size:12px}
.card.ok b{color:var(--ok)}.card.warn b{color:var(--warn)}.card.bad b{color:var(--bad)}
.scroll{overflow-x:auto;background:var(--card);border:1px solid var(--line);border-radius:8px}
table{border-collapse:collapse;width:100%;font-size:12.5px}
th,td{padding:6px 10px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}
th{position:sticky;top:0;background:var(--card);font-weight:600}
td code{font:12px ui-monospace,Consolas,monospace;white-space:pre-wrap;word-break:break-word}
.t{padding:1px 7px;border-radius:10px;font-size:11.5px;font-weight:600;white-space:nowrap}
.t.ok{background:var(--okb);color:var(--ok)}.t.warn{background:var(--warnb);color:var(--warn)}
.t.bad{background:var(--badb);color:var(--bad)}.t.muted{color:var(--muted)}
ul.notes{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:12px 28px}
"""


def _cell(v, code_cols: set, col: str) -> str:
    s = "" if v is None else str(v)
    tone = _TONES.get(s)
    if tone:
        return f'<span class="t {tone}">{html.escape(s)}</span>'
    if col in code_cols and s:
        return f"<code>{html.escape(s)}</code>"
    return html.escape(s)


def write_html(path: Path, title: str, subtitle: str, cards: list[tuple[str, object, str]],
               sections: list[tuple[str, list[dict], list[str]]], notes: list[str] | None = None,
               code_cols: set | None = None) -> None:
    code_cols = code_cols or set()
    parts = [f"<!doctype html><html><head><meta charset='utf-8'>"
             f"<meta name='viewport' content='width=device-width, initial-scale=1'>"
             f"<title>{html.escape(title)}</title><style>{_CSS}</style></head><body><main>",
             f"<h1>{html.escape(title)}</h1><div class='sub'>{html.escape(subtitle)}</div>",
             "<div class='cards'>"]
    for label, value, tone in cards:
        parts.append(f"<div class='card {tone}'><b>{html.escape(str(value))}</b>"
                     f"<span>{html.escape(label)}</span></div>")
    parts.append("</div>")
    if notes:
        parts.append("<h2>Read before deploying</h2><ul class='notes'>")
        parts += [f"<li>{html.escape(n)}</li>" for n in notes]
        parts.append("</ul>")
    for heading, rows, cols in sections:
        parts.append(f"<h2>{html.escape(heading)} <span class='sub'>({len(rows)})</span></h2>")
        if not rows:
            parts.append("<div class='sub'>None.</div>")
            continue
        parts.append("<div class='scroll'><table><thead><tr>")
        parts += [f"<th>{html.escape(c)}</th>" for c in cols]
        parts.append("</tr></thead><tbody>")
        for r in rows:
            parts.append("<tr>" + "".join(f"<td>{_cell(r.get(c), code_cols, c)}</td>" for c in cols) + "</tr>")
        parts.append("</tbody></table></div>")
    parts.append("</main></body></html>")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(parts), encoding="utf-8")
