#!/usr/bin/env python3
"""Run the browser-side Markdown probe headless and report pass/fail.

`gui_test.py` covers the server; it cannot execute `gui/app.js`, and a Python reimplementation of
a JavaScript parser tests the Python, not the parser. So this driver launches a real browser
against `gui/renderer_probe.html` over file:// and reads the verdict back out of the DOM.

    python3 agent/renderer_probe.py            # auto-detect Edge or Chrome
    GPU_GUI_BROWSER="C:/path/to/chrome.exe" python3 agent/renderer_probe.py

A machine with no Chromium-family browser -- which is the normal state of a GPU node -- falls back
to `gui/renderer_probe_jsdom.js`, which runs the same probe page under a DOM implementation. The
fallback is loud about what it cannot see (CSP, layout, Chromium's parser), because "the renderer
assertions ran" and "the page was verified in a browser" are different claims.

Exit codes: 0 all cases passed, 1 something failed, 2 nothing could run (skipped, not passed --
a probe that silently no-ops is worse than one that says it did not run).
"""
from __future__ import annotations

import html
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROBE = HERE / "gui" / "renderer_probe.html"
JSDOM_DRIVER = HERE / "gui" / "renderer_probe_jsdom.js"

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


def run_jsdom() -> int:
    """Drive the same probe page through jsdom when no browser exists on this machine.

    Returns this driver's own exit-code vocabulary, so the caller never has to guess whether an
    emulation pass is a browser pass. It is not: the driver prints what it cannot see.
    """
    node = shutil.which("node") or shutil.which("nodejs")
    if node is None or not JSDOM_DRIVER.is_file():
        print("[probe] no browser here and no node driver to fall back on.")
        return 2
    try:
        out = subprocess.run([node, str(JSDOM_DRIVER), str(PROBE)], capture_output=True,
                             text=True, timeout=180, encoding="utf-8", errors="replace")
    except subprocess.TimeoutExpired:
        print("[probe] FAIL: the jsdom driver did not return within 180 s")
        return 1
    for line in (out.stdout or "").splitlines():
        print(line)
    if out.returncode == 2:
        print("[probe] SKIPPED: jsdom is not installed, so the renderer assertions never ran.")
        return 2
    if out.returncode != 0:
        print("[probe] FAIL under DOM emulation (see the cases above).")
        if out.stderr:
            print("        stderr: " + out.stderr.strip()[:300])
        return 1
    print("[probe] PASS under jsdom emulation -- NOT a Chromium run.")
    return 0


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
        print("[probe] no Edge or Chrome found here. Set GPU_GUI_BROWSER to the executable, "
              "or fall back to DOM emulation:")
        print(f"[probe]   npm install jsdom@24 && node {JSDOM_DRIVER.name} {PROBE}")
        print(f"[probe] alternatively open this file in a browser and read the summary: {PROBE}")
        return run_jsdom()
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
