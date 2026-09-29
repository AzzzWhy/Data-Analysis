/* No-network tests of the real SSH connection dialog. Run: node agent/backend_switch_test.js.
 * This deterministic DOM/fetch harness checks behavior and credential handling, not visual layout.
 */
"use strict";
const assert = require("node:assert/strict"), fs = require("node:fs"), path = require("node:path"), vm = require("node:vm");
const source = fs.readFileSync(process.argv[2] || path.join(__dirname, "gui", "backend-switch.js"), "utf8");
const flush = () => new Promise(resolve => setImmediate(resolve));
const base = extra => ({ csrf_token: "csrf-test-token", mode: "local", connection_source: "none",
  current_connection_source: "none", ssh_state: "disconnected", status: "disconnected", ssh_managed: false, ...extra });

async function harness(initial = base()) {
  const nodes = new Map(), timers = new Map(), calls = [], docHandlers = {}, observers = [];
  let now = 0, timerId = 0, reloads = 0, state = initial, postHandler = null, storageTouches = 0;
  let document;
  class Node {
    constructor(tag = "div") { this.tagName = tag.toUpperCase(); this.children = []; this.parentElement = null;
      this.dataset = {}; this.attrs = {}; this.handlers = {}; this.value = ""; this._text = ""; this.hidden = false; this.disabled = false; }
    set id(value) { this._id = value; nodes.set(value, this); } get id() { return this._id; }
    set textContent(value) { this._text = String(value ?? ""); this.children = []; }
    get textContent() { return this._text + this.children.map(child => child.textContent).join(""); }
    set innerHTML(value) {
      this.children = []; const stack = [this];
      for (const match of value.matchAll(/<\/?([a-z][a-z0-9-]*)([^>]*)>/gi)) {
        const tag = match[1].toLowerCase();
        if (match[0].startsWith("</")) { if (stack.length > 1) stack.pop(); continue; }
        const node = new Node(tag);
        for (const attr of match[2].matchAll(/([\w:-]+)(?:="([^"]*)")?/g)) node.setAttribute(attr[1], attr[2] ?? "");
        stack[stack.length - 1].appendChild(node);
        if (!["input", "br", "hr", "img", "meta", "link"].includes(tag)) stack.push(node);
      }
    }
    setAttribute(name, value) { this.attrs[name] = String(value); if (name === "id") this.id = value;
      else if (name === "value") this.value = value; else if (name === "hidden") this.hidden = true; else if (name === "class") this.className = value; }
    getAttribute(name) { return this.attrs[name]; }
    appendChild(node) { node.parentElement = this; this.children.push(node); return node; }
    insertBefore(node) { node.parentElement = this; this.children.unshift(node); return node; }
    get firstChild() { return this.children[0] || null; }
    addEventListener(name, callback) { (this.handlers[name] ||= []).push(callback); }
    async emit(name, event = {}) { for (const callback of this.handlers[name] || []) await callback(event); }
    click() { return this.disabled ? Promise.resolve() : this.emit("click", { target: this }); }
    focus() { document.activeElement = this; }
    closest(selector) { for (let node = this; node; node = node.parentElement) if (selector === "[hidden]" && node.hidden) return node; return null; }
    contains(other) { for (let node = other; node; node = node.parentElement) if (node === this) return true; return false; }
    querySelectorAll(selector) { const all = this.children.flatMap(child => [child, ...child.querySelectorAll("*")]);
      return selector === "*" ? all : all.filter(node => ["BUTTON", "INPUT", "SELECT"].includes(node.tagName) && !node.disabled); }
  }
  const header = new Node();
  document = { body: new Node("body"), documentElement: { lang: "zh" }, activeElement: null,
    createElement: tag => new Node(tag), getElementById: id => nodes.get(id) || null,
    querySelector: selector => selector === ".topbar-meta" ? header : selector.startsWith("#") ? nodes.get(selector.slice(1)) : null,
    addEventListener: (event, fn) => (docHandlers[event] ||= []).push(fn) };
  for (const id of ["reset-all", "reset-scope-note"]) {
    const node = new Node(id === "reset-all" ? "button" : "p"); node.id = id; document.body.appendChild(node);
  }
  const context = vm.createContext({ document, URL,
    Date: { now: () => now }, MutationObserver: class { constructor(fn) { observers.push(fn); } observe() {} },
    location: { reload: () => reloads++ }, console: { log: () => { throw new Error("No credential logging"); } },
    setTimeout: (fn, delay) => { const id = ++timerId; timers.set(id, { fn, delay }); return id; },
    clearTimeout: id => timers.delete(id),
    localStorage: { setItem: () => storageTouches++, getItem: () => storageTouches++ },
    sessionStorage: { setItem: () => storageTouches++, getItem: () => storageTouches++ },
    fetch: async (url, options = {}) => {
      calls.push({ url, options });
      let response;
      if (options.method === "POST") {
        response = postHandler ? await postHandler(url, JSON.parse(options.body), options) : { status: 202, data: { ok: true, attempt_id: "attempt-1" } };
      } else {
        const value = typeof state === "function" ? await state() : state;
        if (value instanceof Error) throw value;
        response = value && value.httpStatus ? { status: value.httpStatus, data: value.data || {} } : { status: 200, data: value };
      }
      return { ok: response.status >= 200 && response.status < 300, status: response.status, json: async () => response.data };
    },
  });
  vm.runInContext(source, context, { filename: "backend-switch.js" }); await flush();
  const $ = id => nodes.get(id);
  return { $, nodes, calls, timers, document, setState: value => { state = value; }, setPost: fn => { postHandler = fn; },
    reloads: () => reloads, stored: () => storageTouches, setTime: value => { now = value; },
    open: () => $("backend-connection-trigger").click(),
    async nextTimer() { const entry = timers.entries().next().value; if (!entry) return false;
      const [id, timer] = entry; timers.delete(id); now += timer.delay; await timer.fn(); await flush(); return true; },
    async key(event) { for (const handler of docHandlers.keydown || []) await handler(event); },
    language: lang => { document.documentElement.lang = lang; observers.forEach(fn => fn()); },
    postCalls: () => calls.filter(call => call.options.method === "POST"),
  };
}

async function fill(h, method = "password") {
  await h.open(); h.$("backend-ssh-url").value = "ssh -p 6060 Developer@example.test";
  h.$("backend-auth-method").value = method; await h.$("backend-auth-method").emit("change");
  h.$("backend-password").value = "test-password-only";
  h.$("backend-key-path").value = "C:\\keys\\test_key";
  h.$("backend-passphrase").value = "test-passphrase-only";
}
let count = 0;
async function check(name, fn) { await fn(); count++; console.log("PASS " + name); }
(async () => {
  await check("remote reset block has visible bilingual instructions, not only a disabled button", async () => {
    const h = await harness(base({ mode: "remote" }));
    assert.equal(h.$("reset-all").disabled, true); assert.equal(h.$("reset-all").dataset.remoteBlocked, "true");
    assert.match(h.$("reset-scope-note").textContent, /禁止.*重置.*使用本机/);
    h.language("en"); assert.match(h.$("reset-scope-note").textContent, /Reset is blocked.*Use local/);
  });
  for (const lock of ["resetBusy", "taskBusy"]) {
    await check("connection rendering cannot release the " + lock + " reset lock", async () => {
      const h = await harness(); h.$("reset-all").dataset[lock] = "true";
      h.language("en"); assert.equal(h.$("reset-all").disabled, true);
      h.$("reset-all").dataset[lock] = "false"; h.language("zh"); assert.equal(h.$("reset-all").disabled, false);
    });
  }
  await check("switching the reset scope to local clears only the remote lock", async () => {
    const h = await harness(base({ mode: "remote" })); h.$("reset-all").dataset.resetBusy = "true";
    h.setState(base()); await h.open(); assert.equal(h.$("reset-all").dataset.remoteBlocked, "false");
    assert.equal(h.$("reset-all").disabled, true); assert.match(h.$("reset-scope-note").textContent, /只作用于当前本机/);
  });
  await check("CSRF initialization gates all write controls and never renders or stores the token", async () => {
    const h = await harness(base({ csrf_token: undefined }));
    assert.equal(h.$("backend-connect").disabled, true); assert.match(h.$("backend-dialog-message").textContent, /登录/);
    h.setState(base()); await h.open(); assert.equal(h.$("backend-connect").disabled, false);
    assert.doesNotMatch(h.document.body.textContent, /csrf-test-token/); assert.equal(h.stored(), 0);
  });
  await check("simple SSH command is parsed into an address and password exists only in its POST body", async () => {
    const h = await harness(); await fill(h);
    h.setPost((_url, body) => {
      assert.equal(h.$("backend-password").value, ""); assert.equal(h.$("backend-passphrase").value, "");
      assert.equal(body.ssh_url, "ssh://Developer@example.test:6060"); assert.equal(body.password, "test-password-only");
      assert.equal(body.passphrase, undefined); return { status: 202, data: { ok: true, attempt_id: "attempt-1" } };
    });
    await h.$("backend-connect").click();
    assert.equal(h.postCalls().length, 1); assert.equal(h.postCalls()[0].options.headers["X-GWB-CSRF"], "csrf-test-token");
    assert.ok(h.calls.every(call => !/test-password|test-passphrase/.test(call.url)));
    assert.doesNotMatch(h.document.body.textContent, /test-password|test-passphrase/); assert.equal(h.stored(), 0);
  });
  await check("key authentication sends only its private key path and ephemeral passphrase", async () => {
    const h = await harness(); await fill(h, "key"); await h.$("backend-connect").click();
    const body = JSON.parse(h.postCalls()[0].options.body);
    assert.equal(body.auth_method, "key"); assert.equal(body.key_path, "C:\\keys\\test_key");
    assert.equal(body.passphrase, "test-passphrase-only"); assert.equal(body.password, undefined);
    assert.equal(h.$("backend-passphrase").value, ""); assert.equal(h.stored(), 0);
  });
  await check("agent authentication is clearly described and sends no hidden credentials", async () => {
    const h = await harness(); await fill(h, "agent");
    assert.match(h.$("backend-auth-note").textContent, /仍需通过身份验证/);
    await h.$("backend-connect").click(); const body = JSON.parse(h.postCalls()[0].options.body);
    assert.equal(body.auth_method, "agent"); assert.equal(body.password, undefined); assert.equal(body.passphrase, undefined);
    assert.equal(h.$("backend-password-field").hidden, true); assert.equal(h.$("backend-key-fields").hidden, true);
  });
  for (const address of ["ssh -p 22 user@host; touch x", "ssh user@host -o ProxyCommand=evil", "ssh://user:secret@host:22",
    "ssh://user:@host:22", "ssh://user@host:22/path", "ssh://user@host?password=x", "ssh $(bad)@host", "ssh user@host\nwhoami"]) {
    await check("unsafe address rejected without a POST: " + address.split(/[;?\n]/)[0].replace(/secret/g, "<secret>"), async () => {
      const h = await harness(); await fill(h); h.$("backend-ssh-url").value = address;
      await h.$("backend-connect").click(); assert.equal(h.postCalls().length, 0); assert.match(h.$("backend-dialog-message").textContent, /只接受/);
    });
  }
  await check("SSH URI and IPv6 address are normalized without shell execution", async () => {
    const h = await harness(); await fill(h); h.$("backend-ssh-url").value = "ssh://user@[::1]:2222";
    await h.$("backend-connect").click(); assert.equal(JSON.parse(h.postCalls()[0].options.body).ssh_url, "ssh://user@[::1]:2222");
  });
  await check("compact -pPORT command matches the backend address parser", async () => {
    const h = await harness(); await fill(h); h.$("backend-ssh-url").value = "ssh -p6060 Developer@example.test";
    await h.$("backend-connect").click(); assert.equal(JSON.parse(h.postCalls()[0].options.body).ssh_url, "ssh://Developer@example.test:6060");
  });
  await check("closing and changing auth method clear both secret inputs", async () => {
    const h = await harness(); await fill(h); await h.$("backend-dialog-close").click();
    assert.equal(h.$("backend-password").value, ""); assert.equal(h.$("backend-passphrase").value, "");
    assert.equal(h.document.activeElement, h.$("backend-connection-trigger"));
    await fill(h); h.$("backend-auth-method").value = "key"; await h.$("backend-auth-method").emit("change");
    assert.equal(h.$("backend-password").value, ""); assert.equal(h.$("backend-passphrase").value, "");
  });
  await check("disconnect clears typed secrets even when the request is rejected", async () => {
    const h = await harness(base({ ssh_managed: true })); await fill(h);
    h.setPost(() => ({ status: 403, data: { ok: false, code: "origin_rejected", error: "raw internal detail" } }));
    await h.$("backend-disconnect").click();
    assert.equal(h.$("backend-password").value, ""); assert.equal(h.$("backend-passphrase").value, "");
    assert.doesNotMatch(h.$("backend-dialog-message").textContent, /raw internal detail/);
  });
  await check("confirmed CSRF rejection refreshes the token and retries only once", async () => {
    const h = await harness(); await fill(h); let posted = 0;
    h.setPost((_url, body) => { posted++; assert.equal(body.password, "test-password-only");
      if (posted === 1) { h.setState(base({ csrf_token: "renewed-token" })); return { status: 403, data: { ok: false, code: "csrf_failed" } }; }
      return { status: 202, data: { ok: true, attempt_id: "attempt-1" } };
    });
    await h.$("backend-connect").click(); assert.equal(posted, 2);
    assert.equal(h.postCalls()[1].options.headers["X-GWB-CSRF"], "renewed-token"); assert.equal(h.$("backend-password").value, "");
  });
  for (const code of ["origin_rejected", "unclassified_forbidden", "csrf_failed"]) {
    await check("403 " + code + " never causes unbounded credential retries", async () => {
      const h = await harness(); await fill(h);
      h.setPost(() => ({ status: 403, data: { ok: false, code, error: "test-password-only raw error" } }));
      await h.$("backend-connect").click(); assert.equal(h.postCalls().length, code === "csrf_failed" ? 2 : 1);
      assert.doesNotMatch(h.$("backend-dialog-message").textContent, /test-password|raw error/);
    });
  }
  await check("network failure never retries a password and keeps only a classified warning", async () => {
    const h = await harness(); await fill(h); h.setPost(() => { throw new Error("secret request echo"); });
    await h.$("backend-connect").click(); assert.equal(h.postCalls().length, 1);
    assert.match(h.$("backend-dialog-message").textContent, /未能确认/); assert.doesNotMatch(h.document.body.textContent, /secret request echo/);
  });
  await check("unknown host fingerprint requires explicit one-attempt confirmation", async () => {
    const key = { host: "test-host", port: 22, algorithm: "ssh-ed25519", fingerprint: "SHA256:test-fingerprint" };
    const h = await harness(base({ attempt_id: "attempt-1", ssh_state: "awaiting_host_key", ssh_managed: true, connection_source: "managed", host_key: key }));
    await h.open(); assert.equal(h.$("backend-host-key").hidden, false); assert.equal(h.postCalls().length, 0);
    assert.match(h.$("backend-host-key-value").textContent, /SHA256:test-fingerprint/);
    await h.$("backend-host-accept").click();
    assert.equal(h.postCalls()[0].url, "/api/backend/host-key");
    assert.deepEqual(JSON.parse(h.postCalls()[0].options.body), { attempt_id: "attempt-1", accept: true });
  });
  await check("host-key mismatch is blocked and cannot use the unknown-host accept button", async () => {
    const h = await harness(base({ attempt_id: "attempt-1", ssh_state: "host_key_mismatch", host_key: { fingerprint: "changed" } }));
    await h.open(); assert.equal(h.$("backend-host-key").hidden, true); assert.equal(h.$("backend-host-accept").disabled, true);
    await h.$("backend-host-accept").click(); assert.equal(h.postCalls().length, 0); assert.match(h.$("backend-dialog-message").textContent, /不要直接覆盖/);
  });
  await check("external service availability is not presented as success of a new candidate", async () => {
    const h = await harness(base({ mode: "remote", current_connection_source: "external", connection_source: "managed",
      attempt_id: "new-attempt", ssh_state: "authentication_failed", status: "backend_unavailable", ssh_managed: true }));
    await h.open(); assert.equal(h.$("backend-serving-value").textContent, "已有外部隧道");
    assert.equal(h.$("backend-source-value").textContent, "网关管理的 SSH");
    assert.match(h.$("backend-dialog-message").textContent, /认证失败/); assert.match(h.$("backend-source-note").textContent, /不代表/);
    assert.equal(h.reloads(), 0);
  });
  await check("ready candidate can be selected while an old remote tunnel still serves requests", async () => {
    const h = await harness(base({ mode: "remote", current_connection_source: "external", connection_source: "managed",
      attempt_id: "new-attempt", ssh_state: "connected", status: "connected", ssh_managed: true }));
    await h.open(); assert.equal(h.$("backend-use-remote").disabled, false); assert.equal(h.reloads(), 0);
    await h.$("backend-use-remote").click(); assert.equal(h.reloads(), 1);
    assert.equal(h.postCalls()[0].options.headers["X-GWB-CSRF"], "csrf-test-token");
  });
  await check("an activated managed candidate says it is already in use instead of requesting another switch", async () => {
    const h = await harness(base({ mode: "remote", current_connection_source: "managed", connection_source: "managed",
      attempt_id: "active-attempt", ssh_state: "connected", status: "connected", ssh_managed: true }));
    await h.open(); assert.equal(h.$("backend-use-remote").disabled, true);
    assert.match(h.$("backend-source-note").textContent, /当前已通过这条 SSH/);
    assert.match(h.$("backend-dialog-message").textContent, /当前已通过这条 SSH/);
    assert.doesNotMatch(h.$("backend-dialog-message").textContent, /请点击|切换计算位置/);
    h.language("en"); assert.match(h.$("backend-dialog-message").textContent, /already using/);
  });
  await check("an activated external tunnel is not called a newly created SSH connection", async () => {
    const h = await harness(base({ mode: "remote", current_connection_source: "external", connection_source: "external", status: "connected" }));
    await h.open(); assert.equal(h.$("backend-use-remote").disabled, true);
    assert.match(h.$("backend-source-note").textContent, /已有外部隧道.*不是本窗口新建/);
  });
  await check("active managed connection disables creating a duplicate connection", async () => {
    const h = await harness(base({ ssh_managed: true, connection_source: "managed", ssh_state: "connected", status: "connected" }));
    await h.open(); assert.equal(h.$("backend-connect").disabled, true);
    await h.$("backend-connect").click(); assert.equal(h.postCalls().length, 0);
  });
  await check("explicit backend switch reloads after acknowledgement even if its dialog was closed", async () => {
    const h = await harness(base({ status: "connected", connection_source: "external" })); await h.open(); let resolve;
    h.setPost(() => new Promise(done => { resolve = done; }));
    const request = h.$("backend-use-remote").click(); await flush(); await h.$("backend-dialog-close").click();
    resolve({ status: 200, data: { ok: true, mode: "remote" } }); await request; assert.equal(h.reloads(), 1);
  });
  await check("disconnect changing compute mode reloads while cancelling an unused candidate does not", async () => {
    const h = await harness(base({ mode: "remote", ssh_managed: true, connection_source: "managed", current_connection_source: "managed" }));
    await h.open(); h.setPost(() => ({ status: 200, data: { ok: true, mode: "local" } }));
    await h.$("backend-disconnect").click(); assert.equal(h.reloads(), 1);
    const other = await harness(base({ mode: "remote", ssh_managed: true, connection_source: "managed", current_connection_source: "external" }));
    await other.open(); other.setPost(() => ({ status: 200, data: { ok: true, mode: "remote" } }));
    await other.$("backend-disconnect").click(); assert.equal(other.reloads(), 0);
  });
  for (const code of ["ssh_connection_active", "dependency_missing", "origin_rejected"]) {
    await check(code + " has a specific bilingual error message", async () => {
      const h = await harness(); await fill(h); h.setPost(() => ({ status: 409, data: { ok: false, code } }));
      await h.$("backend-connect").click();
      const zh = h.$("backend-dialog-message").textContent;
      h.language("en"); const en = h.$("backend-dialog-message").textContent;
      assert.ok(zh.length > 10 && en.length > 10); assert.notEqual(zh, en); assert.ok(!zh.includes(code) && !en.includes(code));
    });
  }
  await check("backend login requirement is distinct from SSH failure and allows explicit remote login", async () => {
    const h = await harness(base({ attempt_id: "a", connection_source: "managed", ssh_state: "connected", status: "backend_auth_required" }));
    await h.open(); assert.equal(h.$("backend-use-remote").disabled, false); assert.match(h.$("backend-dialog-message").textContent, /两回事/);
  });
  await check("closing an in-flight connection clears secrets and never navigates when it completes", async () => {
    const h = await harness(); await fill(h); let resolve;
    h.setPost(() => new Promise(done => { resolve = done; }));
    const request = h.$("backend-connect").click(); await flush(); await h.$("backend-dialog-close").click();
    h.setState(base({ attempt_id: "attempt-1", connection_source: "managed", ssh_state: "connected", status: "connected" }));
    resolve({ status: 202, data: { ok: true, attempt_id: "attempt-1" } }); await request;
    assert.equal(h.$("backend-password").value, ""); assert.equal(h.reloads(), 0); assert.equal(h.timers.size, 0);
  });
  await check("connection checking stops at 150 seconds and never implicitly selects remote", async () => {
    const h = await harness(base({ attempt_id: "attempt-1", ssh_state: "authenticating", status: "connecting", connection_source: "managed" }));
    await h.open(); h.setTime(150000); await h.nextTimer();
    assert.equal(h.timers.size, 0); assert.match(h.$("backend-dialog-message").textContent, /150 秒/);
    assert.equal(h.postCalls().length, 0); assert.equal(h.reloads(), 0);
  });
  await check("Escape clears secrets and restores keyboard focus; labels switch to English", async () => {
    const h = await harness(); await fill(h); h.language("en");
    assert.equal(h.$("backend-auth-label").textContent, "Authentication method");
    assert.equal(h.$("backend-password-label").textContent, "SSH account password");
    await h.key({ key: "Escape", stopImmediatePropagation() {} });
    assert.equal(h.$("backend-password").value, ""); assert.equal(h.$("backend-dialog-backdrop").hidden, true);
    assert.equal(h.document.activeElement, h.$("backend-connection-trigger"));
  });
  console.log(`ALL SSH DIALOG CHECKS PASSED (${count} cases; deterministic DOM harness, not browser layout)`);
})().catch(error => { console.error(error); process.exitCode = 1; });
