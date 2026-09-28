/* DOM-emulation driver for renderer_probe.html, for machines with no Chromium-family browser.
 *
 *   node agent/gui/renderer_probe_jsdom.js [path/to/renderer_probe.html]
 *
 * This exists because a GPU target can have no browser installed and still need to know whether
 * `gui/app.js` behaves. It runs the SAME probe page and the SAME renderer_probe.js assertions in a
 * real DOM implementation, so a regression in the renderer turns this red too (verified by
 * mutation: dropping the escape step fails the injection case, removing a loop advance fails the
 * termination cases).
 *
 * What it does NOT cover, and no report should claim it does:
 *   - Content-Security-Policy. jsdom applies no CSP; gui.py sends `default-src 'self'` and only a
 *     real browser can prove the page respects it.
 *   - Layout, painting, scrolling, focus, and real navigation of /artifact links.
 *   - Chromium's HTML parser edge cases (mXSS through innerHTML round trips in particular).
 *   - The native EventSource reconnect path, which app.js only exercises over a live server.
 * Exit codes mirror renderer_probe.py: 0 all cases passed, 1 something failed,
 * 2 no usable jsdom (skipped, not passed).
 */
"use strict";

const path = require("path");
const fs = require("fs");

let JSDOM;
try {
  ({ JSDOM } = require("jsdom"));
} catch (err) {
  console.log("[jsdom] unavailable: " + err.message);
  console.log("[jsdom] install it with:  npm install jsdom@24   (v24 is the last line that runs on Node 18)");
  process.exit(2);
}

const PROBE = path.resolve(process.argv[2] || path.join(__dirname, "renderer_probe.html"));
if (!fs.existsSync(PROBE)) {
  console.log("[jsdom] probe page missing: " + PROBE);
  process.exit(1);
}

(async () => {
  const dom = await JSDOM.fromFile(PROBE, {
    runScripts: "dangerously",
    resources: "usable",
    pretendToBeVisual: true,
  });
  const doc = dom.window.document;

  // The probe is a synchronous IIFE, but the two <script src> tags load asynchronously.
  const deadline = Date.now() + 20000;
  let settled = false;
  while (Date.now() < deadline) {
    const s = doc.getElementById("summary");
    if (s && s.getAttribute("data-result") !== "NONE") { settled = true; break; }
    await new Promise((r) => setTimeout(r, 50));
  }

  const rows = Array.from(doc.querySelectorAll("#results pre")).map((p) => ({
    result: p.getAttribute("data-result"),
    text: p.textContent.trim(),
  }));
  if (!settled || !rows.length) {
    console.log("[jsdom] FAIL: the probe page never wrote results (renderMarkdown may be unreachable).");
    dom.window.close();
    process.exit(1);
  }
  let failed = 0;
  rows.forEach((r) => {
    if (r.result !== "PASS") failed++;
    console.log(`  [${r.result}] ${r.text}`);
  });
  const sum = doc.getElementById("summary").textContent.trim();
  console.log(`[jsdom] ${sum}  via jsdom ${require("jsdom/package.json").version} / node ${process.version}`);
  console.log("[jsdom] scope: DOM emulation only — CSP, layout, Chromium's parser and EventSource reconnect are NOT covered here.");
  dom.window.close();
  process.exit(failed ? 1 : 0);
})().catch((err) => {
  console.log("[jsdom] FAIL: " + err.message);
  process.exit(1);
});
