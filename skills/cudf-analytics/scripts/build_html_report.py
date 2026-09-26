#!/usr/bin/env python3
"""Turn the written deliverable into one self-contained HTML file.

Why this and not a GUI. The deliverable already exists as report.md plus a handful of SVG
charts, and Markdown with relative image links requires a viewer that renders Markdown and can
resolve those links next to it. An HTML file with the SVGs inlined opens by double-click in any
browser, needs no runtime, no server and no network, and can be mailed to someone who was not in
the room. It is a *product*, not a *program*: nothing has to be started, so nothing can fail to
start on stage.

The panels are also ordered to match how the analysis was actually done -- the decision first, then
the evidence, then the numbers -- because the engine choice is the part of this work that is not
obvious.

    python build_html_report.py --out-dir deliverables
"""
from __future__ import annotations

import argparse
import html
import json
import os
import re
import sys

# --------------------------------------------------------------------------------------
# Minimal Markdown -> HTML. Deliberately small: the report uses headings, tables, bold, code
# spans, list items and paragraphs, and nothing else. A general Markdown library would be a
# new dependency for six constructs.
# --------------------------------------------------------------------------------------

_INLINE = (
    (re.compile(r"`([^`]+)`"), r"<code>\1</code>"),
    (re.compile(r"\*\*([^*]+)\*\*"), r"<strong>\1</strong>"),
    (re.compile(r"(?<!\*)\*([^*\n]+)\*(?!\*)"), r"<em>\1</em>"),
)


def _inline(text: str) -> str:
    """Escape first, then apply inline markup, so no tag from the data can survive."""
    out = html.escape(text, quote=False)
    for pattern, repl in _INLINE:
        out = pattern.sub(repl, out)
    return out


def md_to_html(md: str) -> str:
    lines = md.split("\n")
    out: list[str] = []
    i = 0
    in_table = False

    def close_table():
        nonlocal in_table
        if in_table:
            out.append("</tbody></table>")
            in_table = False

    while i < len(lines):
        line = lines[i]

        # Table: a header row followed by a separator row of dashes and pipes.
        if "|" in line and i + 1 < len(lines) and re.match(r"^\s*\|?[\s:|-]+\|[\s:|-]*$", lines[i + 1]):
            close_table()
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            out.append('<table><thead><tr>'
                       + "".join(f"<th>{_inline(c)}</th>" for c in cells)
                       + "</tr></thead><tbody>")
            in_table = True
            i += 2
            continue
        if in_table and "|" in line:
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            out.append("<tr>" + "".join(f"<td>{_inline(c)}</td>" for c in cells) + "</tr>")
            i += 1
            continue
        close_table()

        stripped = line.strip()
        if not stripped:
            i += 1
            continue
        if stripped.startswith("### "):
            out.append(f"<h3>{_inline(stripped[4:])}</h3>")
        elif stripped.startswith("## "):
            out.append(f"<h2>{_inline(stripped[3:])}</h2>")
        elif stripped.startswith("# "):
            out.append(f"<h1>{_inline(stripped[2:])}</h1>")
        elif stripped.startswith(("- ", "* ")):
            out.append(f"<li>{_inline(stripped[2:])}</li>")
        elif re.match(r"^!\[.*\]\(.*\)$", stripped):
            # An image reference. The charts are inlined separately with captions, so drop the
            # Markdown form rather than emit a relative <img> that would 404 in a mailed file.
            i += 1
            continue
        else:
            out.append(f"<p>{_inline(stripped)}</p>")
        i += 1
    close_table()
    return "\n".join(out)


# --------------------------------------------------------------------------------------
# The page
# --------------------------------------------------------------------------------------

STYLE = """
:root { --ink:#1a1a1a; --dim:#5c6370; --line:#e3e6ea; --bg:#ffffff; --accent:#0b6b3a;
        --warn:#8a5a00; --panel:#f7f9fa; }
* { box-sizing:border-box; }
body { margin:0; padding:44px 30px 70px; background:var(--bg); color:var(--ink);
       font:16px/1.65 -apple-system,"Segoe UI",Roboto,"Helvetica Neue",
            "PingFang SC","Microsoft YaHei",sans-serif; }
.wrap { max-width:1180px; margin:0 auto; }
h1 { font-size:31px; line-height:1.25; margin:0 0 6px; letter-spacing:-.4px; }
h2 { font-size:21px; margin:44px 0 12px; padding-top:20px; border-top:1px solid var(--line); }
h3 { font-size:16px; margin:26px 0 8px; color:var(--dim); font-weight:600; }
.sub { color:var(--dim); font-size:14px; margin:0 0 26px; }
.lede { background:var(--panel); border-left:4px solid var(--accent); padding:16px 20px;
        border-radius:0 8px 8px 0; margin:22px 0 8px; }
.lede p { margin:0; }
table { border-collapse:collapse; width:100%; margin:14px 0; font-size:14.5px; }
th,td { text-align:left; padding:9px 13px; border-bottom:1px solid var(--line); }
th { background:var(--panel); font-weight:600; color:var(--dim);
     text-transform:uppercase; font-size:11.5px; letter-spacing:.6px; }
td:nth-child(n+2) { font-variant-numeric:tabular-nums; }
code { background:var(--panel); padding:1.5px 5px; border-radius:4px;
       font:13.5px/1.5 ui-monospace,Consolas,"Courier New",monospace; }
li { margin:5px 0; }
.charts { display:grid; grid-template-columns:repeat(auto-fit,minmax(430px,1fr)); gap:20px; }
.card { border:1px solid var(--line); border-radius:10px; padding:16px; background:#fff; }
.card svg { width:100%; height:auto; display:block; }
.card h3 { margin:0 0 10px; color:var(--ink); font-size:15px; }
.caption { color:var(--dim); font-size:13px; margin:10px 0 0; }
.foot { margin-top:56px; padding-top:18px; border-top:1px solid var(--line);
        color:var(--dim); font-size:13px; }
.caveat { background:#fffbf0; border-left:4px solid var(--warn); padding:14px 18px;
          border-radius:0 8px 8px 0; font-size:14.5px; margin:18px 0; }
"""


def _charts_section(chart_paths, out_dir, title_of) -> tuple[str, int]:
    """Inline each SVG so the page stands alone. Returns (html, count inlined)."""
    cards, inlined, missing = [], 0, []
    for path in chart_paths:
        try:
            with open(path, "r", encoding="utf-8") as fh:
                svg = fh.read()
        except OSError:
            missing.append(os.path.basename(path))
            continue
        # Strip any XML prolog or doctype; a nested <svg> must not carry them.
        svg = re.sub(r"<\?xml[^>]*\?>", "", svg)
        svg = re.sub(r"<!DOCTYPE[^>]*>", "", svg, flags=re.I)
        # Drop width/height so the CSS controls the size while the viewBox keeps the aspect.
        svg = re.sub(r'(<svg[^>]*?)\s+width="[^"]*"', r"\1", svg, count=1)
        svg = re.sub(r'(<svg[^>]*?)\s+height="[^"]*"', r"\1", svg, count=1)
        name = title_of(os.path.basename(path))
        cards.append(f'<div class="card"><h3>{html.escape(name)}</h3>{svg}</div>')
        inlined += 1
    if missing:
        print(f"  note: {len(missing)} chart(s) unreadable, skipped: {', '.join(missing)}")
    if not cards:
        return "", 0
    return ('<h2>Charts</h2>\n<p class="caption">Every value below is an exact aggregate over the '
            'full dataset. Chart rendering is not GPU accelerated &mdash; the charts are drawn from '
            'the aggregated rows, where rendering cost is irrelevant.</p>\n'
            '<div class="charts">' + "".join(cards) + "</div>"), inlined


def build_page(out_dir: str, title: str, lang: str) -> str:
    md_path = os.path.join(out_dir, "report.md")
    json_path = os.path.join(out_dir, "result.json")
    if not os.path.isfile(md_path):
        raise SystemExit(f"no report.md in {out_dir} -- run make_deliverables.py first")

    with open(md_path, "r", encoding="utf-8") as fh:
        md = fh.read()

    meta = {}
    if os.path.isfile(json_path):
        try:
            with open(json_path, "r", encoding="utf-8") as fh:
                meta = json.load(fh)
        except (OSError, json.JSONDecodeError):
            meta = {}

    engine = meta.get("engine") or "unknown"
    rows = meta.get("rows_scanned")
    routing_reason = meta.get("routing_reason")
    fallback_reason = meta.get("fallback_reason")
    source = meta.get("source_file") or meta.get("path") or ""

    # The decision goes first. Whether the GPU was used, and why, is the part of this work that
    # is not self-evident from the numbers -- so it is the first thing the reader sees.
    zh = lang == "zh"
    if engine != "pandas":
        decision = ("在 GPU 上执行 (cuDF)" if zh else "Ran on the GPU (cuDF)")
        why = ("数据量超过实测交叉点,GPU 更快。" if zh
               else "The dataset is above the measured crossover, where the GPU is faster.")
    elif routing_reason:
        decision = ("有意选择在 CPU 上执行" if zh else "Deliberately ran on the CPU")
        why = routing_reason
    elif fallback_reason:
        decision = ("GPU 失败,回退到 CPU" if zh else "The GPU failed; fell back to the CPU")
        why = fallback_reason
    else:
        decision = ("在 CPU 上执行" if zh else "Ran on the CPU")
        why = ""

    lede = f'<div class="lede"><p><strong>{html.escape(decision)}</strong>'
    if rows:
        lede += f' &nbsp;&middot;&nbsp; {rows:,} {"行" if zh else "rows"}'
    lede += f' &nbsp;&middot;&nbsp; {"引擎" if zh else "engine"} <code>{html.escape(str(engine))}</code>'
    if source:
        lede += f' &nbsp;&middot;&nbsp; <code>{html.escape(os.path.basename(str(source)))}</code>'
    lede += "</p>"
    if why:
        lede += f'<p class="caption">{html.escape(str(why))}</p>'
    lede += "</div>"

    # The honest caveat, on the page rather than only in the report body.
    caveat = (""
              if engine != "pandas" else
              '<div class="caveat"><strong>No GPU acceleration is claimed for this run.</strong> '
              'The numbers are exact, but this analysis ran on the CPU &mdash; either because the '
              'dataset was too small for the GPU to pay off, or because the GPU path failed.</div>')

    def title_of(basename: str) -> str:
        return os.path.splitext(basename)[0].replace("_", " ")

    charts_html, n_charts = _charts_section(
        meta.get("charts") or sorted(
            os.path.join(out_dir, f) for f in os.listdir(out_dir) if f.endswith(".svg")),
        out_dir, title_of)

    body = md_to_html(md)

    return f"""<!DOCTYPE html>
<html lang="{'zh' if zh else 'en'}">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(title)}</title>
<style>{STYLE}</style>
</head>
<body>
<div class="wrap">
<h1>{html.escape(title)}</h1>
<p class="sub">{html.escape(os.path.basename(str(source))) if source else ''}
&nbsp;&middot;&nbsp; {'由 cudf-analytics 生成' if zh else 'produced by cudf-analytics'}</p>
{lede}
{caveat}
{charts_html}
{body}
<div class="foot">
{'本页为单文件自包含产物:图表已内联,无需网络、无需服务器,可离线打开或转发。' if zh else
 'Self-contained single file: charts are inlined, so this opens offline with no server and can be forwarded as-is.'}
</div>
</div>
</body>
</html>
"""


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out-dir", default="deliverables")
    ap.add_argument("--title", default=None)
    ap.add_argument("--lang", default="auto", choices=["auto", "zh", "en"])
    args = ap.parse_args()

    if not os.path.isdir(args.out_dir):
        raise SystemExit(f"no such directory: {args.out_dir}")

    md_path = os.path.join(args.out_dir, "report.md")
    title = args.title
    if not title and os.path.isfile(md_path):
        with open(md_path, "r", encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("# "):
                    title = line[2:].strip()
                    break
    title = title or "Data analysis report"

    lang = args.lang
    if lang == "auto":
        has_cjk = bool(re.search(r"[\u4e00-\u9fff]", title))
        lang = "zh" if has_cjk else "en"

    page = build_page(args.out_dir, title, lang)
    out = os.path.join(args.out_dir, "report.html")
    with open(out, "w", encoding="utf-8") as fh:
        fh.write(page)
    size_kb = os.path.getsize(out) / 1024
    print(f"wrote {os.path.abspath(out)}  ({size_kb:.1f} KB, {len(page):,} chars)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())