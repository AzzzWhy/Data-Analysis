/* Result-shape regression tests for the real workbench app.js.
 * Run: node agent/gui_result_contract_test.js [path/to/app.js]
 * Dependency-free DOM harness: tests data contracts and text-only rendering, not browser layout.
 */
"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

class Node {
  constructor(tag = "div") {
    this.tagName = tag.toUpperCase();
    this.children = [];
    this.dataset = {};
    this.attributes = {};
    this.hidden = false;
    this.value = "";
    this._text = "";
    this.classList = { add() {}, remove() {} };
    this.handlers = {};
  }
  set textContent(value) { this._text = String(value ?? ""); this.children = []; }
  get textContent() { return this._text + this.children.map((child) => child.textContent).join(""); }
  set innerHTML(_value) { throw new Error("Result rendering must not assign innerHTML"); }
  appendChild(child) { this.children.push(child); return child; }
  createTHead() { return this.appendChild(new Node("thead")); }
  insertRow() { return this.appendChild(new Node("tr")); }
  setAttribute(key, value) { this.attributes[key] = String(value); }
  addEventListener(name, handler) { this.handlers[name] = handler; }
  querySelectorAll(selector) {
    const descendants = this.children.flatMap((child) => [child, ...child.querySelectorAll("*")]);
    if (selector === "*") return descendants;
    return descendants.filter((node) => node.tagName.toLowerCase() === selector);
  }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
}

const nodes = new Map();
const document = {
  title: "test", documentElement: new Node("html"),
  createElement: (tag) => new Node(tag),
  getElementById: (id) => {
    if (!nodes.has(id)) nodes.set(id, new Node());
    return nodes.get(id);
  },
  querySelectorAll: () => [],
};
const context = vm.createContext({
  document, console,
  window: { matchMedia: () => ({ matches: true }) },
  addEventListener() {}, setTimeout() {}, clearTimeout() {},
  // Never let test startup contact a backend or run a real analysis.
  fetch: () => new Promise(() => {}),
});
const sourcePath = process.argv[2] || path.join(__dirname, "gui", "app.js");
vm.runInContext(fs.readFileSync(sourcePath, "utf8"), context, { filename: sourcePath });
const app = vm.runInContext("({ renderTables, renderKpis, renderToolResult, renderSession, analysisBlocks, resultFailed, trackSession, settingsAppliedMessage, applyChrome, applyReleaseResponse, els })", context);
const tables = () => app.els.tables.querySelectorAll("table");
const tableRows = (table) => table.querySelectorAll("tr").map((row) => row.children.map((cell) => cell.textContent));
const summary = { stats: { power: { count: 2049280, mean: 1.092, std: 1.057,
  min: 0.076, q1: 0.308, median: 0.602, q3: 1.528, max: 11.122, nulls: 25979 } } };
let checks = 0;
function check(name, run) {
  run();
  checks++;
  console.log("PASS " + name);
}

for (const [name, result] of [
  ["direct analysis", { success: true, result: { summary } }],
  ["resident step", { success: true, summary }],
  ["raw worker", { ok: true, summary }],
  ["resident auto", { success: true, auto: { summary } }],
  ["nested auto", { success: true, result: { auto: { summary } } }],
]) {
  check(name + " has the same complete summary", () => {
    app.renderTables(result);
    assert.equal(tables().length, 1);
    const rows = tableRows(tables()[0]);
    assert.deepEqual(rows[0], ["column", "count", "mean", "std", "min", "q1", "median", "q3", "max", "nulls"]);
    assert.equal(rows[1][0], "power");
    assert.equal(rows[1][2], "1.092");
    assert.equal(rows[1][9].replace(/\D/g, ""), "25979");
  });
}

check("profile shows dtypes, true null counts and returned preview", () => {
  app.renderTables({ profile: { rows: 3, columns: ["name", "power"],
    dtypes: { name: "object", power: "float64" }, null_counts: { name: 0, power: 1 },
    preview: [{ name: "one", power: 1.25 }, { name: "two", power: null }] } });
  assert.equal(tables().length, 2);
  assert.deepEqual(tableRows(tables()[0])[2], ["power", "float64", "1"]);
  assert.deepEqual(tableRows(tables()[1])[2], ["two", "—"]);
});

check("outliers display count, percent and bounds without inventing unknowns", () => {
  app.renderTables({ outliers: { results: { power: { count: 4, valid_count: 80, pct: 5,
    lower_bound: -1.25, upper_bound: 2.5 }, all_null: { count: 0 } } } });
  const rows = tableRows(tables()[0]);
  assert.equal(rows[1][1], "4");
  assert.equal(rows[1][3], "5");
  assert.equal(rows[1][7], "-1.25");
  assert.equal(rows[2][2], "—");
});

check("outlier KPI uses real count and includes measured zero", () => {
  app.renderKpis({ outliers: { results: { a: { count: 0 }, b: { count: 0 } } } });
  assert.match(app.els.kpis.textContent, /各列合计.*0/);
  app.renderKpis({ outliers: { results: { a: { count: 2 }, b: { count: 3 } } } });
  assert.match(app.els.kpis.textContent, /各列合计.*5/);
  app.renderKpis({ outliers: { results: { a: {} } } });
  assert.equal(app.els.kpis.textContent, "");
});

check("legacy outlier count remains compatible", () => {
  app.renderKpis({ result: { outliers: { results: { a: { outlier_count: 7 } } } } });
  assert.match(app.els.kpis.textContent, /各列合计.*7/);
});

check("an incomplete outlier count is not represented as a complete total", () => {
  app.renderKpis({ outliers: { results: { a: { count: 2 }, b: {} } } });
  assert.equal(app.els.kpis.textContent, "");
});

check("resident step duration is not dropped or rounded to zero", () => {
  app.renderKpis({ step_seconds: 0.003 });
  assert.match(app.els.kpis.textContent, /0\.003/);
});

check("group rows align sparse fields with a union header", () => {
  app.renderTables({ groupby: { by: "region", top_k: [{ region: "A", sum: 12 }, { region: "B", mean: 6 }] } });
  assert.deepEqual(tableRows(tables()[0]), [
    ["region", "sum", "mean"], ["A", "12", "—"], ["B", "—", "6"],
  ]);
});

check("correlation pairs and matrix both render", () => {
  app.renderTables({ corr: { pairs: [{ a: "a", b: "b", corr: -0.75 }],
    matrix: { a: { a: 1, b: -0.75 }, b: { a: -0.75, b: 1 } } } });
  assert.equal(tables().length, 2);
  assert.deepEqual(tableRows(tables()[0])[1], ["a", "b", "-0.75"]);
  assert.deepEqual(tableRows(tables()[1])[2], ["b", "-0.75", "1"]);
});

check("column named column cannot overwrite matrix row names", () => {
  app.renderTables({ corr: { matrix: { column: { column: 1, x: 0.00000001 }, x: { column: 0.00000001, x: 1 } } } });
  const rows = tableRows(tables()[0]);
  assert.deepEqual(rows[0], ["(column)", "column", "x"]);
  assert.deepEqual(rows[1], ["column", "1", "0.00000001"]);
});

check("untrusted strings stay literal text, not HTML", () => {
  const hostile = '<img src=x onerror="alert(1)">';
  app.renderToolResult({ name: "dataset_session", result: { success: true,
    summary: { stats: { [hostile]: { count: 5, mean: 1.2 } } } } });
  assert.ok(app.els.tables.textContent.includes(hostile));
  assert.equal(app.els.tables.querySelectorAll("img").length, 0);
});

check("metadata and nested payloads are never dumped into tables", () => {
  app.renderTables({ input: "/private/data.csv", token: "secret",
    result: { input: "/another/private.csv", api_key: "secret", summary,
      groupby: { by: "x", top_k: [{ x: "A", metadata: { path: "/hidden/private" } }] } } });
  assert.doesNotMatch(app.els.tables.textContent, /private|secret|\[object Object\]/);
});

check("malformed and null operation entries do not throw", () => {
  app.renderTables({ profile: { columns: [] }, summary: { stats: { a: null, b: [] } },
    groupby: { top_k: [null, 4, [], "bad"] }, outliers: { results: null }, corr: { pairs: [null] } });
  assert.equal(tables().length, 0);
  assert.match(app.els.tables.textContent, /没有返回数据行/);
});

check("an empty summary replaces the previous values", () => {
  app.renderTables({ summary });
  app.renderTables({ summary: { stats: {} } });
  assert.equal(tables().length, 0);
  assert.doesNotMatch(app.els.tables.textContent, /power/);
});

check("report-only and session-close replies remove stale numerical tables", () => {
  app.renderTables({ summary });
  app.renderToolResult({ name: "export_deliverables", result: { success: true,
    report: "/deliverables/report.md", charts: [] } });
  assert.equal(tables().length, 0);
  assert.equal(app.els.charts.children[0].href, "/artifact?path=%2Fdeliverables%2Freport.md");
  app.renderTables({ summary });
  app.renderToolResult({ name: "dataset_session", result: { success: true, closed: 1 } });
  assert.equal(tables().length, 0);
});

check("successful close describes the latest operation without claiming analysis results are missing", () => {
  app.renderTables({ summary });
  app.renderToolResult({ name: "dataset_session", result: { success: true, closed: 1 } });
  assert.equal(app.els.chip.textContent, "会话已关闭");
  assert.match(app.els.chip.title, /最近一次工具操作.*并不表示分析结果缺失/);
  assert.equal(tables().length, 0);
  app.renderToolResult({ result: { success: false, closed: 0, error: "close failed" } });
  assert.equal(app.els.chip.className, "chip bad");
});

check("close supports English and missing engine logs use a dash instead of undefined", () => {
  vm.runInContext('LANG = "en"', context);
  app.els.log.textContent = "";
  app.renderToolResult({ name: "dataset_session", result: { success: true, closed: 1 } });
  assert.equal(app.els.chip.textContent, "Session closed");
  assert.match(app.els.chip.title, /latest tool operation.*not a missing analysis result/);
  assert.match(app.els.log.textContent, /OK engine=—/);
  assert.doesNotMatch(app.els.log.textContent, /undefined/);
  vm.runInContext('LANG = "zh"', context);
});

for (const [name, failure] of [
  ["success=false", { success: false, error: "failed" }],
  ["ok=false", { ok: false, error: "worker failed" }],
  ["error without status", { error: "missing status" }],
  ["nested worker failure", { success: true, result: { ok: false, error: "inner failed" } }],
  ["missing tool payload", undefined],
  ["string tool payload", "invalid"],
]) {
  check(name + " is not labelled successful or left showing old results", () => {
    app.renderToolResult({ result: { success: true, engine: "cudf", summary,
      charts: ["/deliverables/summary_bar.svg"] } });
    app.renderToolResult({ result: failure });
    assert.equal(app.els.chip.className, "chip bad");
    assert.equal(tables().length, 0);
    assert.equal(app.els.kpis.textContent, "");
    assert.equal(app.els.charts.textContent, "");
    assert.equal(app.els.chart.textContent, "");
    assert.match(app.els.tables.textContent, /本次分析失败/);
  });
}

check("a failed session reply cannot replace the working session id", () => {
  app.trackSession({ result: { success: true, session_id: "s1" } });
  app.trackSession({ result: { ok: false, session_id: "bad", error: "failed" } });
  assert.equal(vm.runInContext("session", context), "s1");
});

check("display caps are explicit instead of implying all rows were shown", () => {
  app.renderTables({ groupby: { by: "id", top_k: Array.from({ length: 105 }, (_, id) => ({ id })) } });
  assert.equal(tableRows(tables()[0]).length, 101);
  assert.match(app.els.tables.textContent, /105.*100/);
});

check("runtime-only key is ready and preserves server lifetime warning", () => {
  assert.match(app.settingsAppliedMessage({ config_ready: true, remember_key: false,
    warning: "Runtime only; cleared on restart." }), /提问框已可用.*Runtime only/);
  assert.doesNotMatch(app.settingsAppliedMessage({ config_ready: true, remember_key: false }), /还不可用/);
});

check("missing configuration still reports actual missing fields", () => {
  assert.match(app.settingsAppliedMessage({ config_ready: false, missing_fields: ["api_key"] }), /还不可用.*模型服务访问密钥/);
});

check("chrome refresh never replaces dynamic settings feedback with the initial empty hint", () => {
  const hint = document.getElementById("set-msg");
  document.querySelectorAll = () => [hint];
  hint.textContent = "";
  app.applyChrome();
  assert.equal(hint.dataset.label, undefined);
  const feedback = app.settingsAppliedMessage({ config_ready: true,
    warning: "Runtime only; cleared on restart." });
  hint.textContent = feedback;
  // Also protect pages carrying the stale cached value from the earlier implementation.
  hint.dataset.label = "";
  app.applyChrome();
  assert.equal(hint.textContent, feedback);
  vm.runInContext('LANG = "en"', context);
  app.applyChrome();
  assert.equal(hint.textContent, feedback);
  vm.runInContext('LANG = "zh"', context);
  document.querySelectorAll = () => [];
});

const released = { closed: 1, warm_frames_dropped: 2, bytes_freed_mb: 12.5,
  sessions_after: 0, warm_frames_after: 0, detail: { ok: true } };
for (const [name, status, data] of [
  ["busy HTTP", 409, { ok: false, error: "busy: still analyzing" }],
  ["HTTP failure without error body", 500, released],
  ["body success=false", 200, { ...released, success: false }],
  ["body ok=false", 200, { ...released, ok: false }],
  ["body error", 200, { ...released, error: "worker failed" }],
  ["nested worker failure", 200, { ...released, detail: { ok: false } }],
  ["state verification failure", 200, { ...released, state: { error: "worker unresponsive" } }],
  ["missing body", 200, undefined],
  ["empty body", 200, {}],
  ["sessions remain", 200, { ...released, sessions_after: 1 }],
  ["cache remains", 200, { ...released, warm_frames_after: 1 }],
]) {
  check(name + " preserves the current session and never reports release success", () => {
    app.trackSession({ result: { success: true, session_id: "keep-me" } });
    app.els.card.dataset.state = "active";
    app.els.log.textContent = "";
    assert.equal(app.applyReleaseResponse(status, data), false);
    assert.equal(vm.runInContext("session", context), "keep-me");
    assert.equal(app.els.card.dataset.state, "active");
    assert.match(app.els.log.textContent, /释放失败/);
    assert.doesNotMatch(app.els.log.textContent, /释放：关闭/);
  });
}

check("only a verified release clears the session and its old plan", () => {
  app.trackSession({ result: { success: true, session_id: "release-me" } });
  assert.equal(app.applyReleaseResponse(200, released), true);
  assert.equal(vm.runInContext("session", context), null);
  assert.equal(document.getElementById("plan-kind").textContent, "—");
});

check("a closed session with retained frames is warm, not released", () => {
  app.renderSession({ sessions: 0, sessions_detail: [], warm_frames: 1, warm_cache_mb: 161.9 });
  assert.equal(app.els.card.dataset.state, "warm");
  assert.equal(app.els.state.textContent, "warm（数据已缓存）");
  assert.match(app.els.hint.textContent, /会话已关闭.*热缓存.*重新打开并复用/);
  assert.match(app.els.mb.textContent, /161\.9/);
});

check("warm cache status has an English explanation", () => {
  vm.runInContext('LANG = "en"', context);
  app.renderSession({ sessions: 0, sessions_detail: [], warm_frames: 1, warm_cache_mb: 161.9 });
  assert.equal(app.els.state.textContent, "warm (data cached)");
  assert.match(app.els.hint.textContent, /session is closed.*warm cache.*Reopen/);
  vm.runInContext('LANG = "zh"', context);
});

check("active sessions take precedence over additional cached frames", () => {
  app.renderSession({ sessions: 1, sessions_detail: [{ steps: 2, resident_mb: 161.9 }],
    warm_frames: 1, warm_cache_mb: 25 });
  assert.equal(app.els.card.dataset.state, "active");
  assert.match(app.els.state.textContent, /active/);
});

check("verified zero frames is released while worker failure stays unknown", () => {
  app.renderSession({ sessions: 0, sessions_detail: [], warm_frames: 0, warm_cache_mb: 0 });
  assert.equal(app.els.card.dataset.state, "released");
  assert.equal(app.els.state.textContent, "cold / released");
  app.renderSession({ sessions: 0, sessions_detail: [], warm_frames: 1, warm_cache_mb: 161.9,
    error: "worker unavailable" });
  assert.equal(app.els.card.dataset.state, "unknown");
  assert.equal(app.els.hint.textContent, "worker unavailable");
});

(async () => {
  for (const [name, response] of [
    ["busy", { status: 409, data: { ok: false, error: "busy" } }],
    ["rejected body", { status: 200, data: { ...released, ok: false } }],
    ["network exception", new Error("network unavailable")],
  ]) {
    app.trackSession({ result: { success: true, session_id: "handler-session" } });
    context.post = async () => { if (response instanceof Error) throw response; return response; };
    await app.els.release.handlers.click();
    check("release click handles " + name + " without losing its session or locking the button", () => {
      assert.equal(vm.runInContext("session", context), "handler-session");
      assert.equal(app.els.release.disabled, false);
    });
  }
  console.log(`ALL RESULT CONTRACT CHECKS PASSED (${checks} cases; DOM harness, not a layout test)`);
})().catch((err) => { console.error(err); process.exitCode = 1; });
