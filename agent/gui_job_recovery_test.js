/* Real app.js job-state tests using a deterministic DOM/EventSource/fetch harness.
 * node agent/gui_job_recovery_test.js [path/to/app.js]
 * No backend is contacted. These test state/replay contracts, not browser geometry.
 */
"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const sourcePath = process.argv[2] || path.join(__dirname, "gui", "app.js");
const source = fs.readFileSync(sourcePath, "utf8");
const flush = () => new Promise((resolve) => setImmediate(resolve));

class Node {
  constructor(tag = "div") {
    this.tagName = tag.toUpperCase(); this.children = []; this.dataset = {};
    this.attributes = {}; this.hidden = false; this.value = ""; this._text = "";
    this.handlers = {}; this.classList = { add() {}, remove() {} };
  }
  set textContent(value) { this._text = String(value ?? ""); this.children = []; }
  get textContent() { return this._text + this.children.map((child) => child.textContent).join(""); }
  set innerHTML(value) { this.textContent = value; }
  appendChild(child) { this.children.push(child); return child; }
  createTHead() { return this.appendChild(new Node("thead")); }
  insertRow() { return this.appendChild(new Node("tr")); }
  setAttribute(key, value) { this.attributes[key] = String(value); }
  addEventListener(name, handler) { this.handlers[name] = handler; }
  click() { return this.handlers.click && this.handlers.click(); }
  focus() {}
  querySelectorAll(selector) {
    const descendants = this.children.flatMap((child) => [child, ...child.querySelectorAll("*")]);
    return selector === "*" ? descendants : descendants.filter((node) => node.tagName.toLowerCase() === selector);
  }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
}

const pending = () => new Promise(() => {});
const running = (id = "job-1") => ({ id, kind: "ask", label: "test analysis", status: "running",
  done: false, started_at: 100, finished_at: null, sequence: 3, first_sequence: 1 });
const complete = (id = "job-1", status = "succeeded") => ({ ...running(id), status, done: true,
  finished_at: 105, sequence: 5 });
const catalog = (jobs = [], active = null, instance = "backend-A") => ({ instance_id: instance,
  active_job_id: active, busy: Boolean(active), jobs });

async function harness(initial = catalog(), bookmark = null) {
  let data = initial;
  let postReply = { status: 202, data: { instance_id: "backend-A", job_id: "new-job" } };
  const nodes = new Map(), calls = [], sources = [], timers = new Map(), saved = new Map(), reads = new Map();
  let timerId = 0;
  if (bookmark) saved.set("gpu-workbench:last-job:v1", JSON.stringify(bookmark));
  const document = { title: "test", documentElement: new Node("html"), querySelectorAll: () => [],
    createElement: (tag) => new Node(tag), getElementById: (id) => {
      if (!nodes.has(id)) nodes.set(id, new Node());
      return nodes.get(id);
    } };
  class EventSource {
    constructor(url) { this.url = url; this.readyState = 0; this.closed = false; this.listeners = {}; sources.push(this); }
    addEventListener(kind, handler) { (this.listeners[kind] ||= []).push(handler); }
    close() { this.closed = true; this.readyState = 2; }
    emit(kind, body, sequence) {
      const event = { lastEventId: sequence === undefined ? "" : String(sequence) };
      if (body !== undefined) event.data = JSON.stringify(body);
      for (const handler of this.listeners[kind] || []) handler(event);
    }
    open() { this.readyState = 1; this.emit("open"); }
  }
  const context = vm.createContext({ document, console, EventSource,
    window: { matchMedia: () => ({ matches: true }) }, addEventListener() {},
    setTimeout: (fn, delay) => { const id = ++timerId; timers.set(id, { fn, delay }); return id; },
    clearTimeout: (id) => timers.delete(id),
    sessionStorage: { getItem: (key) => saved.get(key) || null,
      setItem: (key, value) => saved.set(key, value), removeItem: (key) => saved.delete(key) },
    fetch: async (url, options = {}) => {
      calls.push({ url, options });
      if (url === "/api/jobs") {
        if (data instanceof Error) throw data;
        const snapshot = typeof data === "function" ? await data() : data;
        return { ok: true, status: 200, json: async () => snapshot };
      }
      if (reads.has(url)) {
        const reader = reads.get(url);
        const payload = typeof reader === "function" ? await reader() : reader;
        return { ok: true, status: 200, json: async () => payload };
      }
      if (url === "/api/ask" || url === "/api/run") {
        if (postReply instanceof Error) throw postReply;
        const reply = typeof postReply === "function" ? await postReply(url, options) : postReply;
        return { ok: reply.status < 400, status: reply.status, json: async () => reply.data };
      }
      return pending();
    },
  });
  vm.runInContext(source, context, { filename: sourcePath });
  await flush();
  const app = vm.runInContext("({ recoverJobs, attach, ask, run, submitJob, renderJobChrome, applyChrome, els })", context);
  return { app, context, nodes, calls, sources, timers, saved,
    read: (expression) => vm.runInContext(expression, context),
    setCatalog: (value) => { data = value; }, setPost: (value) => { postReply = value; },
    setRead: (url, value) => reads.set(url, value),
    source: () => sources[sources.length - 1],
    async nextTimer() { const [id, timer] = timers.entries().next().value || []; if (!timer) return false;
      timers.delete(id); await timer.fn(); await flush(); return true; },
  };
}

let checks = 0;
async function check(name, fn) { await fn(); checks++; console.log("PASS " + name); }

(async () => {
  await check("startup prioritizes the backend active job over a saved completed one", async () => {
    const h = await harness(catalog([running("active"), complete("old")], "active"), { instance_id: "backend-A", job_id: "old" });
    assert.match(h.source().url, /job=active&instance=backend-A&after=0$/);
    assert.equal(h.app.els.ask.disabled, true);
    assert.equal(h.read("currentJob.id"), "active");
  });

  await check("refresh restores same-instance history from zero, never a persisted cursor", async () => {
    const h = await harness(catalog([complete()]), { instance_id: "backend-A", job_id: "job-1", cursor: 999 });
    assert.match(h.source().url, /after=0$/);
    const stored = JSON.parse([...h.saved.values()][0]);
    assert.deepEqual(Object.keys(stored).sort(), ["instance_id", "job_id"]);
    assert.equal(h.app.els.ask.disabled, false);
  });

  await check("old backend bookmark cannot attach to a new backend with the same job id", async () => {
    const h = await harness(catalog([complete()], null, "backend-B"), { instance_id: "backend-A", job_id: "job-1" });
    assert.equal(h.sources.length, 0);
    assert.equal(h.read("jobNotice"), "restarted");
    assert.equal(h.saved.size, 0);
  });

  await check("transport errors neither finish the task nor unlock submission", async () => {
    const h = await harness(catalog([running()], "job-1"));
    h.source().open(); h.source().emit("error");
    assert.equal(h.source().closed, false);
    assert.equal(h.read("jobNotice"), "reconnecting");
    assert.equal(h.app.els.run.disabled, true);
    assert.equal(h.read("currentJob.truncated"), false);
    assert.equal(h.timers.size, 1);
  });

  await check("replayed duplicate event ids render only once", async () => {
    const h = await harness(catalog([running()], "job-1"));
    h.source().emit("trace", { line: "one-line" }, 1);
    h.source().emit("trace", { line: "one-line" }, 1);
    assert.equal((h.app.els.log.textContent.match(/one-line/g) || []).length, 1);
    assert.equal(h.read("currentJob.lastSequence"), 1);
  });

  await check("explicit reconnect resumes from this page's sequence without clearing results", async () => {
    const h = await harness(catalog([running()], "job-1"));
    h.source().emit("trace", { line: "kept-line" }, 3);
    const old = h.source(); old.emit("error");
    await h.app.recoverJobs({ manual: true });
    assert.equal(old.closed, true);
    assert.match(h.source().url, /after=3$/);
    assert.match(h.app.els.log.textContent, /kept-line/);
    h.source().emit("trace", { line: "kept-line" }, 3);
    assert.equal((h.app.els.log.textContent.match(/kept-line/g) || []).length, 1);
  });

  await check("server error is task evidence and waits for an explicit terminal outcome", async () => {
    const h = await harness(catalog([running()], "job-1"));
    h.source().emit("error", { message: "model request failed" }, 1);
    assert.match(h.app.els.log.textContent, /model request failed/);
    assert.equal(h.source().closed, false);
    assert.equal(h.read("currentJob.done"), false);
    assert.equal(h.app.els.ask.disabled, true);
    assert.equal(h.timers.size, 0);
  });

  for (const [name, body, expected] of [
    ["success", { status: "succeeded", success: true }, "succeeded"],
    ["failed", { status: "failed", success: false }, "failed"],
    ["partial", { status: "partial", success: false }, "partial"],
    ["old empty done", {}, "partial"],
    ["inconsistent done", { status: "succeeded", success: false }, "partial"],
    ["success with error", { status: "succeeded", success: true, error: "inconsistent result" }, "partial"],
  ]) {
    await check(name + " done is presented honestly", async () => {
      const h = await harness(catalog([running()], "job-1"));
      h.source().emit("done", body, 5);
      assert.equal(h.read("jobNotice"), expected);
      assert.equal(h.read("currentJob.status"), expected);
      assert.equal(h.source().closed, true);
      assert.equal(h.app.els.ask.disabled, false);
    });
  }

  await check("partial outcome shows its error, reason and failed-tool count", async () => {
    const h = await harness(catalog([running()], "job-1"));
    h.source().emit("done", { status: "partial", success: false, error: "cleanup timed out",
      reason: "cleanup_failed", tool_failures: 2 }, 5);
    const message = h.nodes.get("job-status").textContent;
    assert.match(message, /cleanup timed out/); assert.match(message, /cleanup_failed/); assert.match(message, /2 次失败/);
  });

  await check("an explicit replay gap is visible and never synthesizes missing rows", async () => {
    const h = await harness(catalog([running()], "job-1"));
    h.source().emit("replay_gap", { first_sequence: 21, requested_after: 0 });
    assert.equal(h.read("currentJob.truncated"), true);
    assert.match(h.nodes.get("job-status").textContent, /截断/);
    assert.equal(h.app.els.tables.textContent, "");
  });

  await check("reconnect stops after three failed status checks and offers a manual retry", async () => {
    const h = await harness(catalog([running()], "job-1"));
    h.source().emit("error"); h.setCatalog(new Error("network offline"));
    assert.equal(await h.nextTimer(), true); assert.equal(await h.nextTimer(), true); assert.equal(await h.nextTimer(), true);
    assert.equal(h.timers.size, 0); assert.equal(h.source().closed, true);
    assert.equal(h.read("recoveryChecks"), 3); assert.equal(h.read("jobNotice"), "unavailable");
    assert.equal(h.nodes.get("job-reconnect").hidden, false); assert.equal(h.app.els.ask.disabled, true);
    h.setCatalog(catalog([running()], "job-1"));
    await h.app.recoverJobs({ manual: true });
    assert.equal(h.read("recoveryChecks"), 0); assert.equal(h.source().closed, false);
  });

  await check("a completed task discovered after disconnect replays its missing tail", async () => {
    const h = await harness(catalog([running()], "job-1"));
    h.source().emit("trace", { line: "before" }, 2); h.source().emit("error");
    h.setCatalog(catalog([complete()])); await h.nextTimer();
    assert.match(h.source().url, /after=2$/);
    h.source().emit("trace", { line: "after" }, 4);
    h.source().emit("done", { status: "failed", success: false }, 5);
    assert.match(h.app.els.log.textContent, /before[\s\S]*after/);
    assert.equal(h.read("jobNotice"), "failed");
  });

  await check("restart detaches stale events and scopes recovery to the new backend instance", async () => {
    const h = await harness(catalog([running()], "job-1"));
    const stale = h.source(); stale.emit("trace", { line: "old result" }, 1);
    h.setCatalog(catalog([running()], "job-1", "backend-B"));
    await h.app.recoverJobs({ manual: true });
    assert.equal(stale.closed, true); assert.match(h.source().url, /instance=backend-B&after=0$/);
    stale.emit("done", { status: "succeeded", success: true }, 5);
    assert.equal(h.read("currentJob.instance"), "backend-B");
    assert.equal(h.read("currentJob.done"), false); assert.doesNotMatch(h.app.els.log.textContent, /old result/);
  });

  await check("an expired task is not kept in an endless reconnect loop", async () => {
    const h = await harness(catalog([running()], "job-1"));
    h.source().emit("error"); h.setCatalog(catalog()); await h.nextTimer();
    assert.equal(h.read("currentJob"), null); assert.equal(h.read("jobNotice"), "expired");
    assert.equal(h.timers.size, 0); assert.equal(h.app.els.ask.disabled, false);
  });

  await check("historical replay never resurrects a closed compute session", async () => {
    const h = await harness(catalog([complete()]), { instance_id: "backend-A", job_id: "job-1" });
    h.read('session = "current-real-session"');
    h.source().emit("tool_result", { result: { success: true, session_id: "old-session" } }, 2);
    h.source().emit("session", { sessions: 1, sessions_detail: [{ steps: 1, resident_mb: 900 }], warm_frames: 0 }, 3);
    assert.equal(h.read("session"), "current-real-session");
    assert.notEqual(h.app.els.mb.textContent, "900 MB");
  });

  await check("history viewing neither clears nor replaces the live compute plan", async () => {
    const h = await harness(catalog([complete("old")]));
    h.read('session = "live-session"; renderPlan({ kind: "live-plan", steps: [{ op: "summary" }], completed: 0, total_steps: 1 })');
    h.nodes.get("job-list").children[0].click();
    assert.equal(h.nodes.get("plan-kind").textContent, "live-plan");
    h.source().emit("tool_result", { result: { success: true, session_id: "old-session",
      plan: { kind: "old-plan", steps: [], total_steps: 0 } } }, 2);
    assert.equal(h.nodes.get("plan-kind").textContent, "live-plan");
    assert.equal(h.read("session"), "live-session");
  });

  await check("changing backend clears old dataset, model readiness and memory state immediately", async () => {
    const h = await harness();
    h.read('selected = "/old-data.csv"; canAsk = true; agentReady = true; session = "old-session"');
    h.app.els.card.dataset.state = "active"; h.app.els.mb.textContent = "99 MB";
    h.app.els.files.appendChild(new Node("button"));
    h.setCatalog(catalog([], null, "backend-B")); await h.app.recoverJobs({ manual: true });
    assert.equal(h.read("selected"), null); assert.equal(h.read("canAsk"), false);
    assert.equal(h.read("session"), null); assert.equal(h.app.els.files.children.length, 0);
    assert.equal(h.app.els.card.dataset.state, "unknown"); assert.notEqual(h.app.els.mb.textContent, "99 MB");
    assert.ok(h.calls.filter((call) => call.url === "/api/files").length >= 2);
  });

  await check("late running catalog response cannot roll a received terminal event backwards", async () => {
    const h = await harness(catalog([running()], "job-1"));
    let resolve;
    h.setCatalog(() => new Promise((done) => { resolve = done; }));
    const refresh = h.app.recoverJobs();
    h.source().emit("done", { status: "succeeded", success: true }, 5);
    resolve(catalog([running()], "job-1")); await refresh;
    assert.equal(h.read("currentJob.done"), true); assert.equal(h.read("currentJob.status"), "succeeded");
    assert.equal(h.read("backendBusy"), false); assert.equal(h.app.els.run.disabled, false);
  });

  await check("running analysis prevents switching files and cannot bind its session to another file", async () => {
    const h = await harness(catalog([running()], "job-1"));
    h.setRead("/api/files", { files: [{ path: "/data-b.csv", size_mb: 1 }], count: 1 });
    await h.read("loadFiles()"); h.read('selected = "/data-a.csv"; session = "session-a"');
    const button = h.app.els.files.children[0];
    assert.equal(button.disabled, true); button.click();
    assert.equal(h.read("selected"), "/data-a.csv"); assert.equal(h.read("session"), "session-a");
    h.app.els.tool.value = "dataset_session"; h.app.els.sessionOp.value = "close";
    h.source().emit("tool_result", { name: "dataset_session", args: { operation: "analyze" },
      result: { success: true, session_id: "session-a" } }, 2);
    assert.equal(h.read("session"), "session-a");
  });

  await check("late file listing from the previous backend cannot refill the new sidebar", async () => {
    const h = await harness(); let resolve;
    h.setRead("/api/files", () => new Promise((done) => { resolve = done; }));
    const oldRequest = h.read("loadFiles()");
    h.setRead("/api/files", { files: [{ path: "/new.csv", size_mb: 1 }], count: 1 });
    h.setCatalog(catalog([], null, "backend-B")); await h.app.recoverJobs({ manual: true }); await flush();
    resolve({ files: [{ path: "/old.csv", size_mb: 1 }], count: 1 }); await oldRequest;
    assert.match(h.app.els.files.textContent, /new\.csv/); assert.doesNotMatch(h.app.els.files.textContent, /old\.csv/);
  });

  await check("history view is read-only, escapes its label and disables competing history during a live task", async () => {
    const h = await harness(catalog([complete("old")], null));
    h.nodes.get("job-list").children[0].click();
    assert.match(h.source().url, /job=old/);
    assert.equal(h.calls.filter((call) => call.options.method === "POST").length, 0);
    h.setCatalog(catalog([{ ...running(), label: '<img onerror="bad">' }, complete("old")], "job-1"));
    await h.app.recoverJobs({ manual: true });
    const list = h.nodes.get("job-list");
    assert.match(list.textContent, /<img/); assert.equal(list.querySelectorAll("img").length, 0);
    assert.equal(list.children[1].disabled, true);
    assert.equal(list.children[0].children[1].textContent, "运行中");
  });

  await check("rejected submission preserves the question and cannot be called successful", async () => {
    const h = await harness(); h.read("canAsk = true"); h.app.els.prompt.value = "preserve my question";
    h.setPost({ status: 400, data: { error: "invalid config" } }); await h.app.ask();
    assert.equal(h.app.els.prompt.value, "preserve my question");
    assert.equal(h.read("jobNotice"), "submission_failed");
    assert.equal(h.calls.filter((call) => call.url === "/api/ask").length, 1);
  });

  await check("accepted submission clears only the sent question and stores no private content", async () => {
    const h = await harness(); h.read('canAsk = true; selected = "/private/data.csv"');
    h.app.els.prompt.value = "my confidential question"; await h.app.ask();
    assert.equal(h.app.els.prompt.value, ""); assert.equal(h.read("currentJob.id"), "new-job");
    assert.doesNotMatch([...h.saved.values()].join(), /confidential|private|cursor|sequence/);
    assert.equal(h.app.els.run.disabled, true);
  });

  await check("edits typed while a submission is pending are not cleared", async () => {
    const h = await harness(); h.read("canAsk = true"); h.app.els.prompt.value = "first question";
    h.setPost(async () => { h.app.els.prompt.value = "second draft";
      return { status: 202, data: { instance_id: "backend-A", job_id: "new-job" } }; });
    await h.app.ask(); assert.equal(h.app.els.prompt.value, "second draft");
  });

  await check("409 reconnects to the existing job without resending the preserved question", async () => {
    const h = await harness(); h.read("canAsk = true"); h.app.els.prompt.value = "not yet accepted";
    h.setPost(async () => { h.setCatalog(catalog([running("existing")], "existing"));
      return { status: 409, data: { error: "busy", instance_id: "backend-A", active_job_id: "existing" } }; });
    await h.app.ask();
    assert.equal(h.read("currentJob.id"), "existing"); assert.equal(h.app.els.prompt.value, "not yet accepted");
    assert.equal(h.calls.filter((call) => call.url === "/api/ask").length, 1);
  });

  await check("a lost submission response queries active tasks but never retries the POST", async () => {
    const h = await harness(); h.read("canAsk = true"); h.app.els.prompt.value = "uncertain question";
    h.setPost(async () => { h.setCatalog(catalog([running("accepted-before-disconnect")], "accepted-before-disconnect"));
      throw new Error("connection lost after acceptance"); });
    await h.app.ask();
    assert.equal(h.read("currentJob.id"), "accepted-before-disconnect");
    assert.equal(h.app.els.prompt.value, "uncertain question");
    assert.equal(h.calls.filter((call) => call.url === "/api/ask").length, 1);
  });

  await check("failed startup catalog keeps submission locked until manual verification", async () => {
    const h = await harness(new Error("offline"));
    assert.equal(h.app.els.ask.disabled, true); assert.equal(h.read("jobNotice"), "unavailable");
    h.setCatalog(catalog()); await h.app.recoverJobs({ manual: true });
    assert.equal(h.app.els.ask.disabled, false); assert.equal(h.read("jobNotice"), "ready");
  });

  await check("backend busy without an active job does not invent a task", async () => {
    const h = await harness({ ...catalog(), busy: true });
    assert.equal(h.sources.length, 0); assert.equal(h.read("jobNotice"), "busy");
    assert.equal(h.app.els.ask.disabled, true);
  });

  await check("job history and reconnect states have English labels", async () => {
    const h = await harness(catalog([running()], "job-1"));
    h.read('LANG = "en"'); h.source().emit("error"); h.app.renderJobChrome();
    assert.match(h.nodes.get("job-status").textContent, /Connection lost/);
    assert.equal(h.nodes.get("job-history-title").textContent, "Task history");
    assert.equal(h.nodes.get("job-reconnect").textContent, "Reconnect task");
  });

  console.log(`ALL JOB RECOVERY CHECKS PASSED (${checks} cases; DOM/EventSource harness, not browser layout)`);
})().catch((err) => { console.error(err); process.exitCode = 1; });
