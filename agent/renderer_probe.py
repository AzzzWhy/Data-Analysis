#!/usr/bin/env python3
"""Run the browser-side Markdown probe headless and report pass/fail.

`gui_test.py` covers the server; it cannot execute `gui/app.js`, and a Python reimplementation of
a JavaScript parser tests the Python, not the parser. So this driver launches a real browser
against `gui/renderer_probe.html` over file:// and reads the verdict back out of the DOM.

    python3 agent/renderer_probe.py            # auto-detect Edge or Chrome
    GPU_GUI_BROWSER="C:/path/to/chrome.exe" python3 agent/renderer_probe.py

Exit codes: 0 all cases passed, 1 something failed, 2 no browser found (skipped, not passed --
a probe that silently no-ops is worse than one that says it did not run).
"""
from __future__ import annotations

import html
import os
import re
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROBE = HERE / "gui" / "renderer_probe.html"

# Program Files on the left, x86 on the right: Edge installs 32-bit even on 64-bit Windows.
CANDIDATES = (
    os.environ.get("GPU_GUI_BROWSER"),
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/usr/bin/chromium",
    "/usr/bin/google-chrome",
)

# --dump-dom gives the DOM after the probe has written its results, with no server, no route in
# gui.py and no new CSP allowance. The budget lets a synchronous script finish before the dump.
FLAGS = ("--headless=new", "--disable-gpu", "--no-first-run", "--no-default-browser-check",
         "--allow-file-access-from-files", "--virtual-time-budget=5000", "--dump-dom")


def browser() -> str | None:
    for candidate in CANDIDATES:
        if candidate and Path(candidate).is_file():
            return candidate
    return None


def main() -> int:
    # Chinese case names on a GBK console would otherwise raise UnicodeEncodeError, and a probe
    # that dies while printing its own verdict is worse useless than useless misleading.
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass
    if not PROBE.is_file():
        print(f"[probe] the probe page is missing: {PROBE}")
        return 1
    found = browser()
    if found is None:
        print("[probe] SKIPPED: no Edge or Chrome found. Set GPU_GUI_BROWSER to the executable.")
        print(f"[probe] alternatively open this file in a browser and read the summary: {PROBE}")
        return 2
    target = PROBE.as_uri()
    try:
        # A dedicated profile keeps this from touching the operator's real browser state, and
        # without it a running browser instance can swallow the invocation entirely.
        out = subprocess.run([found, *FLAGS, f"--user-data-dir={Path(temp_dir()) / 'probe-profile'}",
                              target], capture_output=True, text=True, timeout=120,
                             encoding="utf-8", errors="replace")
    except subprocess.TimeoutExpired:
        print(f"[probe] FAIL: {found} did not return within 120 s")
        return 1
    doc = out.stdout or ""

    rows = re.findall(r"<pre data-result=\"(PASS|FAIL)\">(.*?)</pre>", doc, re.S)
    summary = re.search(r'id="summary"[^>]*data-result="(PASS|FAIL|NONE)">(.*?)<', doc, re.S)
    for status, text in rows:
        print(f"  [{status}] {html.unescape(text).strip()}")
    if not rows:
        print("[probe] FAIL: the browser returned no results. The probe page did not execute.")
        print("        first 400 chars of stdout: " + doc[:400].replace("\n", " "))
        if out.stderr:
            print("        stderr: " + out.stderr.strip()[:400])
        return 1
    print("  " + html.unescape(summary.group(2)).strip() if summary else "  no summary line")
    verdict = (summary.group(1) if summary else "FAIL") == "PASS"
    print(f"[probe] {'PASS' if verdict else 'FAIL'}  using {Path(found).name}")
    return 0 if verdict else 1


def temp_dir() -> str:
    import tempfile
    return tempfile.mkdtemp(prefix="hks-probe-")


if __name__ == "__main__":
    sys.exit(main())
