/* No-network reset and model-help contracts for the actual app.js.
 * Run: node agent/gui_reset_test.js. Never calls a live gateway or backend.
 */
"use strict";
const assert = require("node:assert/strict"), fs = require("node:fs"), path = require("node:path"), vm = require("node:vm");
const root = path.join(__dirname, "gui");
const source = fs.readFileSync(path.join(root, "app.js"), "utf8");
const html = fs.readFileSync(path.join(root, "index.html"), "utf8");
const flush = () => new Promise(resolve => setImmediate(resolve));
const reply = (status, data) => ({ status, data });
const local = () => reply(200, { mode: "local", csrf_token: "test-csrf-token" });

function harness() {
  const nodes = new Map(), calls = [], timers = new Map(), redirects = [], labels = [];
  let timerId = 0, probe = local(), posted = reply(200, { ok: true });
  class Node {
    constructor(tag = "div") { this.tagName = tag.toUpperCase(); this.children = []; this.dataset = {}; this.handlers = {};
      this._text = ""; this.value = ""; this.disabled = false; this.hidden = false; this.attributes = {};
      this.classList = { add() {}, remove() {} }; }
    set textContent(value) { this._text = String(value ?? ""); this.children = []; }
    get textContent() { return this._text + this.children.map(node => node.textContent).join(""); }
    set innerHTML(value) { throw new Error("No HTML injection permitted: " + value); }
    appendChild(node) { this.children.push(node); return node; }
    setAttribute(key, value) { this.attributes[key] = String(value); }
    addEventListener(name, fn) { this.handlers[name] = fn; }
    click() { return this.disabled ? Promise.resolve() : this.handlers.click?.(); }
    focus() {}
    querySelector() { return null; }
    querySelectorAll() { return []; }
  }
  const $ = id => { if (!nodes.has(id)) nodes.set(id, new Node()); return nodes.get(id); };
  for (const match of html.matchAll(/<(p|h2|span)\b([^>]*)>([^<]+)<\/\1>/g)) {
    if (match[1] === "span" && !match[2].includes('class="lt"')) continue;
    const id = /id="([^"]+)"/.exec(match[2]);
    const node = id ? $(id[1]) : new Node(match[1]); node.textContent = match[3]; labels.push(node);
  }
  $("reset-msg").dataset.dynamic = ""; $("reset-all").dataset.dynamic = "";
  const document = { title: "test", documentElement: new Node("html"), getElementById: $,
    createElement: tag => new Node(tag), querySelectorAll: () => [...labels, $("reset-msg"), $("reset-all")] };
  const context = vm.createContext({ document, console, AbortController,
    window: { location: { replace: url => redirects.push(url) }, matchMedia: () => ({ matches: true }) },
    addEventListener() {},
    setTimeout: (fn, delay) => { const id = ++timerId; timers.set(id, { fn, delay }); return id; },
    clearTimeout: id => timers.delete(id),
    fetch: async (url, options = {}) => {
      calls.push({ url, options });
      if (!["/api/backend", "/api/reset", "/api/logout"].includes(url)) return new Promise(() => {});
      const reader = url === "/api/backend" ? probe : url === "/api/reset" ? posted : reply(200, { ok: true });
      const value = typeof reader === "function" ? await reader(options) : reader;
      if (value instanceof Error) throw value;
      return { status: value.status, json: async () => {
        if (value.jsonError) throw new Error("invalid JSON"); return value.data;
      } };
    },
  });
  vm.runInContext(source, context, { filename: "app.js" });
  return { $, nodes, labels, timers, redirects, context,
    setProbe: value => { probe = value; }, setPost: value => { posted = value; },
    requests: () => calls.filter(call => call.url === "/api/backend" || call.url === "/api/reset"),
    posts: () => calls.filter(call => call.options.method === "POST"),
    read: expression => vm.runInContext(expression, context),
    async reset() { await $("reset-all").click(); await $("reset-all").click(); },
  };
}
let count = 0;
async function check(name, fn) { await fn(); count++; console.log("PASS " + name); }
(async () => {
  await check("arming is visible and expires without sending any request", async () => {
    const h = harness(); await h.$("reset-all").click(); assert.equal(h.requests().length, 0);
    assert.match(h.$("reset-msg").textContent, /设置新访问密码.*退出/);
    const [id, timer] = [...h.timers].find(([, value]) => value.delay === 4000); h.timers.delete(id); timer.fn();
    assert.equal(h.$("reset-all").dataset.resetArmed, "false"); assert.equal(h.requests().length, 0);
  });
  await check("local gateway reset preflights and sends its CSRF token once", async () => {
    const h = harness(); await h.reset();
    assert.deepEqual(h.requests().map(call => call.url), ["/api/backend", "/api/reset"]);
    assert.equal(h.posts()[0].options.headers["X-GWB-CSRF"], "test-csrf-token");
    assert.equal(h.posts()[0].options.body, "{}"); assert.deepEqual(h.redirects, ["/"]);
    assert.doesNotMatch([...h.nodes.values()].map(node => node.textContent).join(""), /test-csrf-token/);
  });
  await check("only a confirmed standalone 404 can reset without a CSRF header", async () => {
    const h = harness(); h.setProbe({ status: 404, jsonError: true }); await h.reset();
    assert.equal(h.posts().length, 1); assert.equal(h.posts()[0].options.headers["X-GWB-CSRF"], undefined);
    assert.deepEqual(h.redirects, ["/"]);
  });
  await check("a fresh remote preflight refuses reset even if an old page enabled its button", async () => {
    const h = harness(); h.setProbe(reply(200, { mode: "remote", csrf_token: "test-csrf-token" })); await h.reset();
    assert.equal(h.posts().length, 0); assert.equal(h.$("reset-all").disabled, true);
    assert.match(h.$("reset-msg").textContent, /远端.*禁止.*使用本机/);
    assert.equal(h.$("reset-all").dataset.resetBusy, "false");
  });
  for (const [name, value] of [
    ["network error", new Error("private transport text")], ["unauthorized", reply(401, {})],
    ["server error", reply(500, {})], ["bad JSON", { status: 200, jsonError: true }],
    ["unknown mode", reply(200, { mode: "other", csrf_token: "token" })],
    ["missing token", reply(200, { mode: "local" })], ["blank token", reply(200, { mode: "local", csrf_token: " " })],
  ]) {
    await check("preflight " + name + " fails closed and releases the request lock", async () => {
      const h = harness(); h.setProbe(value); await h.reset(); assert.equal(h.posts().length, 0);
      assert.equal(h.$("reset-all").disabled, false); assert.match(h.$("reset-msg").textContent, /未发送重置/);
      assert.doesNotMatch(h.$("reset-msg").textContent, /private transport/);
      assert.equal([...h.timers.values()].filter(timer => [4000, 15000].includes(timer.delay)).length, 0);
    });
  }
  for (const [name, response] of [
    ["csrf rejection", reply(403, { ok: false, code: "csrf_failed" })],
    ["busy", reply(409, { ok: false })], ["failure body", reply(200, { ok: false, error: "secret detail" })],
    ["invalid success body", { status: 200, jsonError: true }], ["network exception", new Error("secret detail")],
  ]) {
    await check("POST " + name + " is not retried and cannot strand a disabled reset button", async () => {
      const h = harness(); h.setPost(response); await h.reset();
      assert.equal(h.posts().length, 1); assert.equal(h.requests().length, 2); assert.equal(h.redirects.length, 0);
      assert.equal(h.$("reset-all").disabled, false); assert.match(h.$("reset-msg").textContent, /未自动重试/);
      assert.doesNotMatch(h.$("reset-msg").textContent, /secret detail/);
      assert.equal([...h.timers.values()].filter(timer => [4000, 15000].includes(timer.delay)).length, 0);
    });
  }
  await check("timeout aborts an uncertain reset without replaying it", async () => {
    const h = harness(); h.setPost(options => new Promise((resolve, reject) => options.signal.addEventListener("abort", () => reject(new Error("timeout")))));
    const running = h.reset(); await flush(); assert.equal(h.$("reset-all").disabled, true);
    assert.equal(h.posts().length, 1); const timer = [...h.timers.values()].find(item => item.delay === 15000); timer.fn(); await running;
    assert.equal(h.posts().length, 1); assert.equal(h.$("reset-all").disabled, false);
    assert.match(h.$("reset-msg").textContent, /无法确认重置结果/);
  });
  await check("reset completion does not unlock an independent remote or task-busy lock", async () => {
    for (const lock of ["remoteBlocked", "taskBusy"]) {
      const h = harness(); h.setPost(() => { h.$("reset-all").dataset[lock] = "true"; throw new Error("offline"); }); await h.reset();
      assert.equal(h.$("reset-all").disabled, true); assert.equal(h.$("reset-all").dataset.resetBusy, "false");
    }
  });
  await check("job changes lock reset and never release an in-flight reset", async () => {
    const h = harness(); h.read("backendBusy = true; syncJobControls()"); assert.equal(h.$("reset-all").disabled, true);
    h.$("reset-all").dataset.resetBusy = "true"; h.read("backendBusy = false; syncJobControls()");
    assert.equal(h.$("reset-all").disabled, true);
  });
  await check("old remote translation tables still show English help and reset feedback", async () => {
    const h = harness(); h.read("LANG = 'en'; I18N = {}; applyChrome()");
    assert.match(h.$("model-local-help").textContent, /127\.0\.0\.1 means the remote machine.*SSH.*not a model API/);
    assert.match(h.$("model-address-help").textContent, /OpenAI-compatible.*11434\/v1.*8000\/v1/);
    assert.match(h.$("model-key-help").textContent, /local as a placeholder.*no cloud key/);
    assert.match(h.$("reset-scope-note").textContent, /Reset applies only.*Use local/);
    assert.ok(h.labels.some(node => node.textContent === "Model service URL"));
    assert.ok(h.labels.some(node => node.textContent === "Model service access key"));
    h.setProbe(new Error("offline")); await h.reset(); assert.match(h.$("reset-msg").textContent, /no reset request was sent/);
    h.read("applyChrome()"); assert.match(h.$("reset-msg").textContent, /no reset request was sent/);
    h.read("LANG = 'zh'; applyChrome()"); assert.match(h.$("reset-msg").textContent, /未发送重置/);
    assert.match(h.$("model-local-help").textContent, /选择远端后/);
  });
  await check("server translations take precedence over the new local fallback", async () => {
    const h = harness(); h.read("LANG = 'en'; I18N = { '模型服务地址': 'Server-provided model label' }; applyChrome()");
    assert.ok(h.labels.some(node => node.textContent === "Server-provided model label"));
  });
  await check("sign out does not call reset or modify settings", async () => {
    const h = harness(); await h.$("logout").click();
    assert.deepEqual(h.posts().map(call => call.url), ["/api/logout"]); assert.deepEqual(h.redirects, ["/"]);
  });
  await check("model help does not mutate settings, passwords or model-service addresses", async () => {
    const h = harness(); h.$("set-key").value = "do-not-submit"; h.$("set-url").value = "http://chosen-model.test/v1";
    h.read("LANG = 'en'; applyChrome()"); assert.equal(h.$("set-key").value, "do-not-submit");
    assert.equal(h.$("set-url").value, "http://chosen-model.test/v1"); assert.equal(h.posts().length, 0);
  });
  console.log(`ALL RESET AND MODEL-HELP CHECKS PASSED (${count} cases; no real reset or network)`);
})().catch(error => { console.error(error); process.exitCode = 1; });
