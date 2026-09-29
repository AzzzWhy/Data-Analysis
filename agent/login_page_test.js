"use strict";

// Dependency-free contract checks for the login page. No backend or credentials.
// Run with: node agent/login_page_test.js
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const html = fs.readFileSync(path.join(__dirname, "gui", "login.html"), "utf8");
const script = fs.readFileSync(path.join(__dirname, "gui", "login.js"), "utf8");
const css = fs.readFileSync(path.join(__dirname, "gui", "login.css"), "utf8");
let checks = 0;
function check(value, expected, label) {
  assert.deepEqual(value, expected, label);
  checks++;
}

function element(textContent = "") {
  return {
    textContent, value: "", hidden: false, disabled: false, dataset: {}, listeners: {},
    addEventListener(event, handler) { this.listeners[event] = handler; },
    setAttribute() {}, focus() {},
  };
}

async function page(gate, { failure = false, status = 200, language = "zh-CN", reduce = true, hidden = false } = {}) {
  const nodes = new Map();
  for (const match of html.matchAll(/\bid="([^"]+)"/g)) nodes.set(match[1], element());
  for (let i = 0; i < 3; i++) nodes.get("live-n" + i).textContent = "—";
  const requests = [], timers = [], redirects = [];
  const frames = new Map(), properties = new Map(), documentListeners = {};
  let nextFrame = 0, canvasDraws = 0, canvasContexts = 0;
  const motion = { matches: reduce, addEventListener(event, handler) { this.onchange = handler; } };
  nodes.get("dust").style = {};
  nodes.get("dust").getContext = () => {
    canvasContexts++;
    return { setTransform() {}, clearRect() { canvasDraws++; }, beginPath() {}, arc() {}, fill() {} };
  };
  let reply = { gate, failure, status };
  const context = vm.createContext({
    document: {
      title: "", hidden, documentElement: { lang: "zh", style: { setProperty: (key, value) => properties.set(key, value) } },
      getElementById: id => nodes.get(id),
      querySelectorAll: () => [],
      addEventListener: (event, handler) => { documentListeners[event] = handler; },
    },
    navigator: { language },
    window: {
      innerWidth: 1280, innerHeight: 800,
      matchMedia: () => motion,
      addEventListener() {},
      location: { replace: destination => redirects.push(destination) },
    },
    setTimeout: (...args) => timers.push(args),
    requestAnimationFrame: callback => { frames.set(++nextFrame, callback); return nextFrame; },
    cancelAnimationFrame: id => frames.delete(id),
    fetch: async (url, options) => {
      requests.push({ url, options });
      if (reply.failure) throw new Error("offline");
      return { ok: reply.status >= 200 && reply.status < 300,
        status: reply.status, json: async () => reply.gate };
    },
  });
  vm.runInContext(script, context, { filename: "login.js" });
  await new Promise(resolve => setImmediate(resolve));
  return { nodes, context, requests, timers, redirects, frames, properties,
    get canvasContexts() { return canvasContexts; },
    get canvasDraws() { return canvasDraws; },
    setHidden(value) { context.document.hidden = value; documentListeners.visibilitychange(); },
    setReduce(value) { motion.matches = value; motion.onchange(); },
    runFrame() {
      const [id, callback] = frames.entries().next().value;
      frames.delete(id);
      callback();
    },
    setReply(next) { reply = { gate: next, failure: false, status: 200 }; } };
}

async function main() {
  check(/data-meter|12,840,221|3\.88×|2\.63s/.test(html), false, "HTML must not contain fabricated measurements");
  check([...html.matchAll(/id="live-n\d">—<\/b>/g)].length, 3, "all static measurement values are unavailable, not zero");
  check(/Math\.random|startMeters|setInterval/.test(script.split("function startAtmosphere()")[0]), false,
    "randomness must be restricted to decorative particles, not measurements");
  const decoration = html.match(/<div class="figures" data-decoration="digits" aria-hidden="true">([\s\S]*?)<\/div>/);
  check(Boolean(decoration), true, "number artwork is hidden from assistive technology");
  const digits = [...decoration[1].matchAll(/<span class="figure f(\d)">([^<]+)<\/span>/g)];
  check(digits.length, 6, "exactly six decorative number groups");
  check(digits.every(match => /^\d+$/.test(match[2])), true, "decorations are only digits, without metric names or units");
  check(/data-fig|data-figure-value|CSV|Parquet|CPU \+ GPU|图表 · 报告/.test(decoration[1]), false,
    "old floating capability text is removed");
  check(/@keyframes digit-breathe\s*\{[\s\S]*?scale\(\.78\)[\s\S]*?scale\(1\.14\)/.test(css), true,
    "size changes use bounded transform scaling");
  check([...css.matchAll(/\.f\d[^\n]*animation-duration:\s*([\d.]+)s/g)].every(match => Number(match[1]) >= 18), true,
    "number animation cycles are slow");
  check(/@media \(max-width: 980px\)\s*\{\s*\.figures\s*\{ display: none;/.test(css), true,
    "narrow layouts keep decoration away from the login form");
  check(/@media \(prefers-reduced-motion: reduce\)[\s\S]*?\.figure[\s\S]*?animation: none !important/.test(css), true,
    "reduced motion disables the CSS number animation");
  check(/\.figure, \.motes i \{ animation-play-state: var\(--atmosphere-play, running\)/.test(css), true,
    "decorative CSS animations pause when the page is hidden");

  const signin = await page({ mode: "signin" });
  check(signin.nodes.get("gate-form").hidden, false, "sign-in form visible");
  check(signin.nodes.get("confirm-field").hidden, true, "sign-in does not offer password creation");
  check(signin.nodes.get("t-sub").textContent.includes("所连后端"), true, "password belongs to selected backend");
  check(signin.nodes.get("metrics-note").textContent.includes("不执行分析"), true, "metric provenance visible in Chinese");
  check(signin.timers.length, 0, "no fake metric timers");
  check(signin.requests.map(request => request.url), ["/api/gate"], "login does not execute analysis");
  signin.nodes.get("lang").listeners.click();
  check(signin.context.document.documentElement.lang, "en", "language can switch to English");
  check(signin.nodes.get("metrics-note").textContent.includes("No analysis runs"), true, "metric provenance visible in English");
  check(signin.nodes.get("product-sub").textContent, "GPU acceleration & data analysis", "brand subtitle translated");
  check(signin.nodes.get("live-k1").textContent, "Speedup", "speedup label translated");
  check(/data-decoration|\.figures|\.figure/.test(script.split("function startAtmosphere()")[0]), false,
    "language and authentication code never rewrite decorative digits");
  for (let i = 0; i < 3; i++) check(signin.nodes.get("live-n" + i).textContent, "—", "translation never invents a metric");
  const dictionaries = vm.runInContext("STRINGS", signin.context);
  check(Object.keys(dictionaries.zh).sort(), Object.keys(dictionaries.en).sort(), "translation keys complete");
  check(Object.values(dictionaries.zh).every(value => typeof value === "string" && value.length > 0), true, "Chinese strings populated");
  check(Object.values(dictionaries.en).every(value => typeof value === "string" && value.length > 0), true, "English strings populated");

  const setup = await page({ mode: "setup", remote: false });
  check(setup.nodes.get("confirm-field").hidden, false, "confirmed first-run setup has confirmation field");
  check(setup.nodes.get("t-foot").textContent.includes("加盐哈希"), true, "storage claim reflects backend implementation");
  setup.nodes.get("lang").listeners.click();
  check(setup.nodes.get("t-foot").textContent.includes("connected backend"), true, "English setup explains backend boundary");

  const waiting = await page({ mode: "setup", remote: true });
  check(waiting.nodes.get("gate-form").hidden, true, "remote uninitialized backend cannot be claimed");
  check(waiting.nodes.get("waiting-note").hidden, false, "remote setup wait note visible");
  check(waiting.nodes.get("gate-retry").hidden, true, "setup wait does not claim connection failure");

  for (const [gate, options] of [
    [null, { failure: true }], [null, { status: 503 }], [null, {}], [{ mode: "unknown" }, {}],
  ]) {
    const unavailable = await page(gate, options);
    check(unavailable.nodes.get("gate-form").hidden, true, "unknown state never offers first-run setup");
    check(unavailable.nodes.get("gate-retry").hidden, false, "unknown state offers explicit retry");
    check(unavailable.nodes.get("waiting-note").hidden, true, "unknown state never claims an operator is setting a password");
    unavailable.nodes.get("lang").listeners.click();
    check(unavailable.nodes.get("t-wait-title").textContent, "Connection not confirmed", "offline message translated");
    unavailable.setReply({ mode: "signin" });
    await unavailable.nodes.get("gate-retry").listeners.click();
    check(unavailable.nodes.get("gate-form").hidden, false, "retry recovers real sign-in mode");
    check(unavailable.nodes.get("confirm-field").hidden, true, "retry honors backend mode");
  }
  const open = await page({ mode: "open" });
  check(open.redirects, ["/"], "open backend still enters workbench");
  check(signin.frames.size, 0, "reduced motion does not start an animation frame loop");
  check(signin.canvasContexts, 0, "reduced motion avoids allocating the canvas renderer");
  const animated = await page({ mode: "signin" }, { reduce: false });
  check(animated.frames.size, 1, "animated page owns only the existing canvas loop");
  check(animated.properties.get("--atmosphere-play"), "running", "visible decorative CSS animation runs");
  animated.runFrame();
  check(animated.canvasDraws, 1, "existing canvas renders normally");
  check(animated.frames.size, 1, "rendering schedules one successor, not a second loop");
  animated.setHidden(true);
  check(animated.frames.size, 0, "backgrounding cancels the pending canvas frame");
  check(animated.properties.get("--atmosphere-play"), "paused", "backgrounding pauses decorative CSS motion");
  animated.setHidden(false);
  animated.setHidden(false);
  check(animated.frames.size, 1, "restoring visibility resumes exactly one loop");
  animated.setReduce(true);
  check(animated.frames.size, 0, "enabling reduced motion live cancels canvas rendering");
  check(animated.properties.get("--atmosphere-play"), "paused", "live reduced motion pauses CSS motion");
  animated.setReduce(false);
  check(animated.frames.size, 1, "disabling reduced motion live resumes a single loop");
  const hiddenPage = await page({ mode: "signin" }, { reduce: false, hidden: true });
  check(hiddenPage.frames.size, 0, "a hidden page never starts a rendering loop");
  check(hiddenPage.canvasContexts, 0, "a hidden page defers canvas initialization");
  hiddenPage.setHidden(false);
  check(hiddenPage.frames.size, 1, "first reveal initializes one loop");
  check(animated.requests.map(request => request.url), ["/api/gate"], "animation creates no backend work");
  console.log(`PASS login page: ${checks} checks (decorative digits, motion lifecycle, honest metrics, bilingual gate)`);
}

main().catch(error => { console.error(error); process.exitCode = 1; });
