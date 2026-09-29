"use strict";

// Dependency-free contract checks for the login page. No backend or credentials.
// Run with: node agent/login_page_test.js
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const html = fs.readFileSync(path.join(__dirname, "gui", "login.html"), "utf8");
const script = fs.readFileSync(path.join(__dirname, "gui", "login.js"), "utf8");
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

async function page(gate, { failure = false, status = 200, language = "zh-CN" } = {}) {
  const nodes = new Map();
  for (const match of html.matchAll(/\bid="([^"]+)"/g)) nodes.set(match[1], element());
  for (let i = 0; i < 3; i++) nodes.get("live-n" + i).textContent = "—";
  const figures = [...html.matchAll(/data-fig="(\d)"/g)].map(match => {
    const node = element();
    node.dataset.fig = match[1];
    return node;
  });
  const figureValues = [...html.matchAll(/data-figure-value="(\d)"/g)].map(match => {
    const node = element();
    node.dataset.figureValue = match[1];
    return node;
  });
  const requests = [], timers = [], redirects = [];
  let reply = { gate, failure, status };
  const context = vm.createContext({
    document: {
      title: "", documentElement: { lang: "zh", style: { setProperty() {} } },
      getElementById: id => nodes.get(id),
      querySelectorAll: selector => selector === "[data-fig]" ? figures
        : selector === "[data-figure-value]" ? figureValues : [],
    },
    navigator: { language },
    window: {
      innerWidth: 1280, innerHeight: 800,
      matchMedia: () => ({ matches: true }),
      location: { replace: destination => redirects.push(destination) },
    },
    setTimeout: (...args) => timers.push(args),
    fetch: async (url, options) => {
      requests.push({ url, options });
      if (reply.failure) throw new Error("offline");
      return { ok: reply.status >= 200 && reply.status < 300,
        status: reply.status, json: async () => reply.gate };
    },
  });
  vm.runInContext(script, context, { filename: "login.js" });
  await new Promise(resolve => setImmediate(resolve));
  return { nodes, context, requests, timers, redirects, figures, figureValues,
    setReply(next) { reply = { gate: next, failure: false, status: 200 }; } };
}

async function main() {
  check(/data-meter|12,840,221|3\.88×|2\.63s/.test(html), false, "HTML must not contain fabricated measurements");
  check([...html.matchAll(/id="live-n\d">—<\/b>/g)].length, 3, "all static measurement values are unavailable, not zero");
  check(/Math\.random|startMeters|setInterval/.test(script.split("function startAtmosphere()")[0]), false,
    "randomness must be restricted to decorative particles, not measurements");

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
  check(signin.figureValues[2].textContent, "Charts · Reports", "background capability labels translated");
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
  console.log(`PASS login page: ${checks} checks (honest metrics, bilingual copy, gate failure and recovery)`);
}

main().catch(error => { console.error(error); process.exitCode = 1; });
