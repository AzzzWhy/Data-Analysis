/* Deterministic tests for the real app.js dataset drag/select lifecycle.
 * node agent/gui_dataset_drag_test.js [path/to/app.js]
 * Only synthetic DOM, EventSource and fetch boundaries are used; no real service,
 * filesystem upload, model, analysis, credentials, reset or browser is involved.
 * Synthetic DOM assertions do not verify browser geometry or visual animation.
 */
"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const sourcePath = process.argv[2] || path.join(__dirname, "gui", "app.js");
const source = fs.readFileSync(sourcePath, "utf8");
const TYPE = "application/x-gpu-workbench-dataset";
const flush = () => new Promise((resolve) => setImmediate(resolve));
const pending = () => new Promise(() => {});
const A = { path: "/synthetic/data/first.csv", size_mb: 2.5 };
const B = { path: "/synthetic/data/second.parquet", size_mb: 8 };
const catalog = (instance = "backend-A") => ({ instance_id: instance, jobs: [], active_job_id: null, busy: false });

function event(properties = {}) {
  return { defaultPrevented: false, stopped: false,
    preventDefault() { this.defaultPrevented = true; },
    stopPropagation() { this.stopped = true; }, ...properties };
}
class Transfer {
  constructor(values = {}, files = []) { this.values = new Map(Object.entries(values)); this.files = files; this.reads = 0; }
  get types() { return [...this.values.keys(), ...(this.files.length ? ["Files"] : [])]; }
  clearData() { this.values.clear(); }
  setData(type, value) { this.values.set(type, String(value)); }
  getData(type) { this.reads++; if (this.protected) throw new Error("protected dragover store"); return this.values.get(type) || ""; }
  setDragImage(node, x, y) { this.dragImage = { node, x, y }; }
}
class Node {
  constructor(tag = "div", focus = () => {}) {
    this.tagName = tag.toUpperCase(); this.children = []; this.dataset = {}; this.attributes = {};
    this.hidden = false; this.disabled = false; this.draggable = false; this.value = ""; this._text = "";
    this.handlers = {}; this.classes = new Set(); this.focus = () => focus(this);
    this.captures = new Set(); this.captureLog = []; this.animations = [];
    this.rect = { x: 30, y: 40, left: 30, top: 40, width: 220, height: 60, right: 250, bottom: 100 };
    this.classList = { add: (...names) => names.forEach((name) => this.classes.add(name)),
      remove: (...names) => names.forEach((name) => this.classes.delete(name)), contains: (name) => this.classes.has(name) };
    const styles = new Map();
    this.style = { setProperty: (key, value) => styles.set(key, value), removeProperty: (key) => styles.delete(key),
      getPropertyValue: (key) => styles.get(key) || "" };
  }
  set className(value) { this.classes = new Set(String(value).split(/\s+/).filter(Boolean)); }
  get className() { return [...this.classes].join(" "); }
  set textContent(value) { this._text = String(value ?? ""); this.children = []; }
  get textContent() { return this._text + this.children.map((child) => child.textContent).join(""); }
  set innerHTML(_value) { throw new Error("Dataset tests require text-only rendering"); }
  appendChild(child) { child.parentElement = this; this.children.push(child); return child; }
  remove() { if (this.parentElement) this.parentElement.children = this.parentElement.children.filter((node) => node !== this); this.parentElement = null; }
  createTHead() { return this.appendChild(new Node("thead")); }
  insertRow() { return this.appendChild(new Node("tr")); }
  setAttribute(key, value) { this.attributes[key] = String(value); }
  getAttribute(key) { return this.attributes[key] ?? null; }
  getBoundingClientRect() { return { ...this.rect }; }
  contains(node) { return node === this || this.children.some((child) => child.contains(node)); }
  setPointerCapture(id) { this.captures.add(id); this.captureLog.push(["set", id]); }
  hasPointerCapture(id) { return this.captures.has(id); }
  releasePointerCapture(id) { this.captures.delete(id); this.captureLog.push(["release", id]); }
  animate(keyframes, options) {
    const handlers = {};
    const animation = { keyframes, options, canceled: false, finished: pending(),
      addEventListener(kind, fn) { (handlers[kind] ||= []).push(fn); },
      cancel() { this.canceled = true; this.emit("cancel"); },
      emit(kind) { if (typeof this["on" + kind] === "function") this["on" + kind](); (handlers[kind] || []).forEach((fn) => fn()); } };
    this.animations.push(animation); return animation;
  }
  addEventListener(kind, handler) { (this.handlers[kind] ||= []).push(handler); }
  dispatch(kind, properties = {}) { const e = event({ target: this, ...properties }); for (const handler of this.handlers[kind] || []) handler(e); return e; }
  click() { return this.dispatch("click"); }
  querySelectorAll(selector) {
    const descendants = this.children.flatMap((child) => [child, ...child.querySelectorAll("*")]);
    return selector === "*" ? descendants : descendants.filter((node) => selector.startsWith(".")
      ? node.classList.contains(selector.slice(1)) : node.tagName.toLowerCase() === selector);
  }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
}

async function harness({ files = [A, B], reduced = true, pointers = false } = {}) {
  let listing = { files, count: files.length }, jobs = catalog(), timerId = 0, rafId = 0;
  const nodes = new Map(), calls = [], sources = [], timers = new Map(), frames = new Map(), saved = new Map();
  const globals = {}, mediaHandlers = [], docHandlers = {};
  const document = { title: "test", hidden: false, visibilityState: "visible", activeElement: null,
    documentElement: new Node("html"), body: new Node("body"), querySelectorAll: () => [], querySelector: () => null,
    addEventListener(kind, fn) { (docHandlers[kind] ||= []).push(fn); },
    createElement: (tag) => new Node(tag, (node) => { document.activeElement = node; }),
    getElementById: (id) => {
      if (!nodes.has(id)) {
        const node = document.createElement("div");
        if (id === "result-dropzone") node.rect = { x: 400, y: 120, left: 400, top: 120, width: 560, height: 600, right: 960, bottom: 720 };
        if (id === "dataset-stage") node.rect = { x: 420, y: 220, left: 420, top: 220, width: 320, height: 90, right: 740, bottom: 310 };
        nodes.set(id, node);
      }
      return nodes.get(id);
    },
    elementFromPoint(x, y) {
      const zone = nodes.get("result-dropzone");
      return zone && x >= zone.rect.left && x <= zone.rect.right && y >= zone.rect.top && y <= zone.rect.bottom ? zone : document.body;
    } };
  const media = { matches: reduced, addEventListener(_kind, fn) { mediaHandlers.push(fn); }, addListener(fn) { mediaHandlers.push(fn); } };
  class EventSource {
    constructor(url) { this.url = url; this.listeners = {}; this.closed = false; sources.push(this); }
    addEventListener(kind, handler) { (this.listeners[kind] ||= []).push(handler); }
    close() { this.closed = true; }
    emit(kind, body, sequence = 1) { for (const fn of this.listeners[kind] || []) fn({ data: JSON.stringify(body), lastEventId: String(sequence) }); }
  }
  const context = vm.createContext({ document, console, EventSource, innerWidth: 1200, innerHeight: 800,
    window: { matchMedia: () => media, innerWidth: 1200, innerHeight: 800,
      PointerEvent: pointers ? function PointerEvent() {} : undefined,
      addEventListener(kind, fn) { (globals[kind] ||= []).push(fn); } },
    PointerEvent: pointers ? function PointerEvent() {} : undefined,
    addEventListener(kind, fn) { (globals[kind] ||= []).push(fn); },
    requestAnimationFrame: (fn) => { const id = ++rafId; frames.set(id, fn); return id; },
    cancelAnimationFrame: (id) => frames.delete(id),
    setTimeout: (fn, delay) => { const id = ++timerId; timers.set(id, { fn, delay }); return id; },
    clearTimeout: (id) => timers.delete(id),
    sessionStorage: { getItem: (key) => saved.get(key) || null, setItem: (key, value) => saved.set(key, value), removeItem: (key) => saved.delete(key) },
    fetch: async (url, options = {}) => {
      calls.push({ url, options });
      const data = url === "/api/files" ? (typeof listing === "function" ? await listing() : listing)
        : url === "/api/jobs" ? jobs : url === "/api/ask" ? { job_id: "new-job", instance_id: "backend-A" } : undefined;
      if (data === undefined) return pending();
      return { ok: true, status: url === "/api/ask" ? 202 : 200, json: async () => data };
    },
  });
  vm.runInContext(source, context, { filename: sourcePath });
  await flush(); await flush();
  const app = vm.runInContext("({ loadFiles, setupDatasetDrop, selectDataset, renderDatasetStage, syncJobControls, adoptJobCatalog, attach, ask, applyChrome, els })", context);
  const read = (expression) => vm.runInContext(expression, context);
  return { app, read, nodes, calls, sources, timers, frames, saved, document,
    records: () => read("[...datasetEntries.values()]"),
    setFiles: (value) => { listing = value; }, setJobs: (value) => { jobs = value; },
    global(kind, props = {}) { const e = event(props); for (const fn of globals[kind] || []) fn(e); return e; },
    setReduced(value) { media.matches = value; mediaHandlers.forEach((fn) => fn({ matches: value })); },
    setHidden(value) { document.hidden = value; document.visibilityState = value ? "hidden" : "visible"; (docHandlers.visibilitychange || []).forEach((fn) => fn()); },
    nextFrame() { const [id, fn] = frames.entries().next().value || []; if (!fn) return false; frames.delete(id); fn(); return true; },
    pointer(record = read("[...datasetEntries.values()][0]"), kind = "pointerdown", props = {}) {
      return record.button.dispatch(kind, { pointerId: 1, pointerType: "mouse", isPrimary: true, button: 0, buttons: 1, clientX: 40, clientY: 50, ...props });
    },
    move(props = {}) { const e = event({ pointerId: 1, pointerType: "mouse", isPrimary: true, button: -1, buttons: 1, clientX: 600, clientY: 300, ...props }); for (const fn of globals.pointermove || []) fn(e); return e; },
    up(props = {}) { const e = event({ pointerId: 1, pointerType: "mouse", isPrimary: true, button: 0, buttons: 0, clientX: 600, clientY: 300, ...props }); for (const fn of globals.pointerup || []) fn(e); return e; },
    start(record = read("[...datasetEntries.values()][0]"), transfer = new Transfer(), point = { clientX: 40, clientY: 50 }) {
      return { transfer, event: record.button.dispatch("dragstart", { dataTransfer: transfer, ...point }) }; },
    drop(transfer) { return nodes.get("result-dropzone").dispatch("drop", { dataTransfer: transfer }); },
    writes: () => calls.filter((call) => call.options.method && call.options.method !== "GET"),
  };
}

const pointerHarness = (options = {}) => harness({ ...options, pointers: true });
let checks = 0, failures = 0;
async function check(name, fn) {
  try { await fn(); checks++; console.log("PASS " + name); }
  catch (error) { failures++; console.error("FAIL " + name + "\n" + error.stack); }
}

(async () => {
  await check("drag payload contains only a local list id, no backend path or file bytes", async () => {
    const h = await harness(), { transfer } = h.start();
    assert.deepEqual(transfer.types, [TYPE]);
    assert.equal(transfer.getData(TYPE), h.records()[0].id);
    assert.ok(![...transfer.values.values()].some((value) => value.includes(A.path)));
    assert.equal(h.writes().length, 0);
  });
  await check("protected dragover does not read the DataTransfer store", async () => {
    const h = await harness(), { transfer } = h.start(); transfer.protected = true;
    const e = h.nodes.get("result-dropzone").dispatch("dragover", { dataTransfer: transfer });
    assert.equal(e.defaultPrevented, true); assert.equal(transfer.dropEffect, "copy"); assert.equal(transfer.reads, 0);
  });
  await check("valid drop only stages a card and preserves typed instructions", async () => {
    const h = await harness(); h.app.els.prompt.value = "my own instruction";
    const { transfer } = h.start(); const e = h.drop(transfer);
    assert.equal(e.defaultPrevented, true); assert.equal(e.stopped, true);
    assert.equal(h.read("selected"), A.path); assert.equal(h.nodes.get("dataset-stage").hidden, false);
    assert.equal(h.nodes.get("staged-file-name").textContent, "first.csv");
    assert.equal(h.app.els.prompt.value, "my own instruction"); assert.equal(h.document.activeElement, h.app.els.prompt);
    assert.equal(h.writes().length, 0); assert.equal(h.sources.length, 0);
  });
  await check("only a subsequent explicit ask sends the selected file and original instruction", async () => {
    const h = await harness(); h.app.els.prompt.value = "analyze only when I ask";
    h.drop(h.start().transfer); h.read("canAsk = true"); await h.app.ask();
    assert.equal(h.writes().length, 1); assert.equal(h.writes()[0].url, "/api/ask");
    assert.deepEqual(JSON.parse(h.writes()[0].options.body), { text: "analyze only when I ask", file: A.path });
  });
  for (const [name, condition] of [["unknown", "jobsKnown = false"], ["busy", "backendBusy = true"], ["pending", "submissionPending = true"]]) {
    await check(name + " blocks click and dragstart", async () => {
      const h = await harness(), record = h.records()[0]; h.read(condition); h.app.syncJobControls();
      record.button.click(); const drag = h.start(record);
      assert.equal(h.read("selected"), null); assert.equal(drag.event.defaultPrevented, true);
      assert.equal(record.button.draggable, false); assert.equal(h.writes().length, 0);
    });
    await check(name + " becoming true between dragstart and drop rejects selection", async () => {
      const h = await harness(), { transfer } = h.start(); h.read(condition); h.drop(transfer);
      assert.equal(h.read("selected"), null); assert.equal(h.read("datasetDrag"), null); assert.equal(h.writes().length, 0);
    });
  }
  await check("backend epoch change invalidates an old drag and closes its highlight", async () => {
    const h = await harness(), { transfer } = h.start(); h.app.adoptJobCatalog(catalog("backend-B"));
    await flush(); h.drop(transfer);
    assert.equal(h.read("selected"), null); assert.equal(h.read("datasetDrag"), null);
    assert.equal(h.nodes.get("result-dropzone").classList.contains("drag-ready"), false);
  });
  await check("same-backend file-list refresh invalidates old drag and old button", async () => {
    const h = await harness(), record = h.records()[0], { transfer } = h.start(record);
    h.setFiles({ files: [B], count: 1 }); await h.app.loadFiles(); h.drop(transfer); record.button.click();
    assert.equal(h.read("selected"), null); assert.equal(h.records().length, 1);
  });
  await check("out-of-order file-list responses cannot restore an old allowlist", async () => {
    const h = await harness(); let release; h.setFiles(() => new Promise((resolve) => { release = resolve; }));
    const older = h.app.loadFiles(); h.setFiles({ files: [B], count: 1 }); await h.app.loadFiles();
    release({ files: [A], count: 1 }); await older;
    assert.equal(h.records()[0].path, B.path);
  });
  await check("null or malformed JSON directory payloads clear the allowlist without crashing", async () => {
    const h = await harness(), stale = h.records()[0];
    for (const payload of [null, "unexpected payload", [], {}]) {
      h.setFiles(payload); await h.app.loadFiles();
      assert.equal(h.records().length, 0); assert.equal(h.app.els.files.querySelectorAll("button").length, 0);
      stale.button.click(); assert.equal(h.read("selected"), null);
      assert.ok(h.app.els.files.textContent.length > 0, "missing empty-directory feedback");
    }
    assert.equal(h.writes().length, 0);
  });
  for (const [name, transfer] of [["desktop file", new Transfer({}, [{ name: "private.csv" }])],
    ["external text", new Transfer({ "text/plain": "/synthetic/not-listed.csv" })],
    ["external URI", new Transfer({ "text/uri-list": "file:///synthetic/private.csv" })],
    ["forged custom type", new Transfer({ [TYPE]: "0:1:0" })]]) {
    await check(name + " is refused without selecting, rewriting prompt or uploading", async () => {
      const h = await harness(); h.app.els.prompt.value = "do not replace"; const e = h.drop(transfer);
      assert.equal(e.defaultPrevented, true); assert.equal(h.read("selected"), null);
      assert.equal(h.app.els.prompt.value, "do not replace"); assert.equal(h.writes().length, 0);
    });
  }
  await check("an active drag cannot accept a different offered id", async () => {
    const h = await harness(), { transfer } = h.start(); transfer.setData(TYPE, h.records()[1].id); h.drop(transfer);
    assert.equal(h.read("selected"), null); assert.equal(h.read("datasetDrag"), null);
  });
  await check("desktop drops outside the panel are blocked without an upload", async () => {
    const h = await harness(), transfer = new Transfer({}, [{ name: "private.csv" }]);
    assert.equal(h.global("dragover", { dataTransfer: transfer }).defaultPrevented, true);
    assert.equal(h.global("drop", { dataTransfer: transfer }).defaultPrevented, true);
    assert.equal(h.read("selected"), null); assert.equal(h.writes().length, 0);
  });
  await check("choosing another file clears previous result, plan, session and late history events", async () => {
    const h = await harness(); h.app.attach("old", "backend-A", { summary: { id: "old", status: "succeeded", done: true } });
    const previous = h.sources[0]; h.read("session = 'old-session'");
    for (const id of ["answer", "kpis", "tables", "charts", "chart", "log"]) h.nodes.get(id).textContent = "OLD RESULT";
    h.drop(h.start(h.records()[1]).transfer);
    assert.equal(previous.closed, true); assert.equal(h.read("session"), null); assert.equal(h.read("currentJob"), null);
    for (const id of ["answer", "kpis", "tables", "charts", "chart", "log"]) assert.ok(!h.nodes.get(id).textContent.includes("OLD RESULT"), id);
    previous.emit("trace", { line: "LATE OLD RESULT" });
    assert.ok(!h.app.els.log.textContent.includes("LATE OLD RESULT")); assert.equal(h.saved.size, 0);
    assert.equal(h.read("selected"), B.path); assert.equal(h.writes().length, 0);
  });
  for (const cancel of ["dragend", "Escape", "blur"]) {
    await check(cancel + " cancels selection/highlight and rejects a subsequent drop", async () => {
      const h = await harness(), record = h.records()[0], { transfer } = h.start(record);
      if (cancel === "dragend") record.button.dispatch("dragend");
      else h.global(cancel === "Escape" ? "keydown" : "blur", { key: "Escape" });
      h.drop(transfer); assert.equal(h.read("selected"), null);
      assert.equal(record.button.classList.contains("dataset-dragging"), false);
      assert.equal(h.nodes.get("result-dropzone").classList.contains("drag-over"), false);
    });
  }
  await check("duplicate drop cannot select twice or create a second arrival timer", async () => {
    const h = await harness(), { transfer } = h.start(); h.drop(transfer);
    const timers = h.timers.size; h.document.activeElement = null; h.drop(transfer);
    assert.equal(h.read("selected"), A.path); assert.equal(h.timers.size, timers); assert.equal(h.document.activeElement, null);
    assert.equal(h.writes().length, 0);
  });
  await check("clicking another file clears a previous drop's arrival class and timer", async () => {
    const h = await harness(), { transfer } = h.start(); h.drop(transfer);
    const zone = h.nodes.get("result-dropzone"), timer = h.read("datasetDropTimer");
    assert.equal(zone.classList.contains("drop-received"), true); assert.ok(h.timers.has(timer));
    h.records()[1].button.click();
    assert.equal(h.read("selected"), B.path); assert.equal(zone.classList.contains("drop-received"), false);
    assert.equal(h.read("datasetDropTimer"), null); assert.equal(h.timers.has(timer), false);
    assert.equal(h.writes().length, 0);
  });
  await check("keyboard-compatible file buttons select without moving focus or changing prompt", async () => {
    const h = await harness(); h.app.els.prompt.value = "kept";
    const record = h.records()[0]; record.button.focus(); record.button.click();
    assert.equal(record.button.type, "button"); assert.equal(record.button.getAttribute("aria-pressed"), "true");
    assert.equal(h.read("selected"), A.path); assert.equal(h.document.activeElement, record.button);
    assert.equal(h.app.els.prompt.value, "kept"); assert.equal(h.writes().length, 0);
  });
  await check("hostile filenames remain text-only and unknown sizes are not invented", async () => {
    const name = '<img src=x onerror="attack">.csv';
    const h = await harness({ files: [{ path: "/synthetic/" + name }] }); h.drop(h.start().transfer);
    assert.equal(h.nodes.get("staged-file-name").textContent, name);
    assert.equal(h.nodes.get("staged-file-name").children.length, 0);
    assert.match(h.nodes.get("staged-file-meta").textContent, /未提供|not provided/i);
    assert.match(h.records()[0].button.querySelector(".size").textContent, /未提供|not provided/i);
  });
  await check("reduced motion does not start an animation-frame loop and preserves selection feedback", async () => {
    const h = await harness({ reduced: true }); h.drop(h.start().transfer);
    assert.equal(h.frames.size, 0); assert.equal(h.nodes.get("dataset-stage").hidden, false);
    assert.match(h.nodes.get("drop-hint").textContent, /first\.csv/);
  });
  await check("normal glow becomes idle rather than rendering forever", async () => {
    const h = await harness({ reduced: false });
    h.nextFrame(); assert.equal(h.frames.size, 0);
    h.global("pointermove", { clientX: 700, clientY: 500 });
    h.global("pointermove", { clientX: 710, clientY: 510 });
    assert.equal(h.frames.size, 1, "pointer updates should coalesce");
    let count = 0; while (h.nextFrame() && count++ < 500) {}
    assert.ok(count < 500, "glow never settled"); assert.equal(h.frames.size, 0);
  });
  await check("changing reduced-motion preference cancels and safely resumes the glow", async () => {
    const h = await harness({ reduced: false });
    h.global("pointermove", { clientX: 300, clientY: 400 }); assert.equal(h.frames.size, 1);
    h.setReduced(true); assert.equal(h.frames.size, 0);
    h.global("pointermove", { clientX: 100, clientY: 100 }); assert.equal(h.frames.size, 0);
    h.setReduced(false); assert.equal(h.frames.size, 1);
  });
  await check("hiding the page stops frames and invalidates an active drag", async () => {
    const h = await harness({ reduced: false }), { transfer } = h.start();
    h.setHidden(true); assert.equal(h.frames.size, 0); assert.equal(h.read("datasetDrag"), null);
    assert.equal(h.document.body.classList.contains("document-hidden"), true);
    h.global("pointermove", { clientX: 100, clientY: 100 }); assert.equal(h.frames.size, 0);
    h.drop(transfer); assert.equal(h.read("selected"), null);
    h.setHidden(false); assert.equal(h.document.body.classList.contains("document-hidden"), false);
  });
  await check("stage focus control never submits and stays inert while busy", async () => {
    const h = await harness(); h.app.els.prompt.value = "my own question"; h.drop(h.start().transfer);
    h.document.activeElement = null; h.read("backendBusy = true"); h.app.syncJobControls();
    h.nodes.get("stage-focus").click(); assert.equal(h.document.activeElement, null);
    assert.equal(h.app.els.prompt.value, "my own question"); assert.equal(h.writes().length, 0);
  });
  await check("live ghost replaces the native static screenshot and follows drag coordinates without RAF", async () => {
    const h = await harness(), record = h.records()[0], { transfer } = h.start(record);
    const ghost = h.read("datasetDragGhost"), image = h.read("datasetDragImage");
    assert.equal(transfer.dragImage.node, image); assert.equal(image.parentElement, h.document.body);
    assert.equal(ghost.parentElement, h.document.body); assert.equal(ghost.getAttribute("aria-hidden"), "true");
    assert.equal(image.getAttribute("aria-hidden"), "true"); assert.equal(ghost.style.getPropertyValue("--drag-x"), "58px");
    record.button.dispatch("drag", { clientX: 80, clientY: 90 });
    assert.equal(ghost.style.getPropertyValue("--drag-x"), "98px"); assert.equal(ghost.style.getPropertyValue("--drag-y"), "108px");
    h.global("dragover", { dataTransfer: transfer, clientX: 100, clientY: 120 });
    assert.equal(ghost.style.getPropertyValue("--drag-x"), "118px"); assert.equal(ghost.style.getPropertyValue("--drag-y"), "138px");
    assert.equal(h.frames.size, 0); assert.equal(h.writes().length, 0);
  });
  await check("ghost ignores unavailable and zero drag coordinates and remains inside viewport limits", async () => {
    const h = await harness(), record = h.records()[0]; h.start(record);
    const ghost = h.read("datasetDragGhost");
    for (const point of [{ clientX: 0, clientY: 0 }, { clientX: NaN, clientY: 30 }, {}]) record.button.dispatch("drag", point);
    assert.equal(ghost.style.getPropertyValue("--drag-x"), "58px"); assert.equal(ghost.style.getPropertyValue("--drag-y"), "68px");
    record.button.dispatch("drag", { clientX: 99999, clientY: 99999 });
    assert.equal(ghost.style.getPropertyValue("--drag-x"), "936px"); assert.equal(ghost.style.getPropertyValue("--drag-y"), "692px");
    record.button.dispatch("drag", { clientX: -300, clientY: -300 });
    assert.equal(ghost.style.getPropertyValue("--drag-x"), "8px"); assert.equal(ghost.style.getPropertyValue("--drag-y"), "8px");
  });
  for (const reason of ["drop", "dragend", "blur", "Escape", "hidden", "backend change", "list refresh", "busy"]) {
    await check(reason + " removes ghost and invisible native-image nodes", async () => {
      const h = await harness(), record = h.records()[0], { transfer } = h.start(record);
      const ghost = h.read("datasetDragGhost"), image = h.read("datasetDragImage");
      assert.ok(ghost && image);
      if (reason === "drop") h.drop(transfer);
      else if (reason === "dragend") record.button.dispatch("dragend");
      else if (reason === "blur") h.global("blur");
      else if (reason === "Escape") h.global("keydown", { key: "Escape" });
      else if (reason === "hidden") h.setHidden(true);
      else if (reason === "backend change") { h.app.adoptJobCatalog(catalog("backend-B")); await flush(); }
      else if (reason === "list refresh") await h.app.loadFiles();
      else { h.read("backendBusy = true"); h.app.syncJobControls(); }
      assert.equal(h.read("datasetDragGhost"), null); assert.equal(h.read("datasetDragImage"), null);
      assert.equal(ghost.parentElement, null); assert.equal(image.parentElement, null);
      assert.equal(h.document.body.querySelectorAll(".dataset-drag-ghost").length, 0);
      assert.equal(h.document.body.querySelectorAll(".dataset-drag-image").length, 0);
    });
  }
  for (const unsupported of ["missing", "throws"]) {
    await check("setDragImage " + unsupported + " preserves native drag/drop without ghost leftovers", async () => {
      const h = await harness(), transfer = new Transfer();
      transfer.setDragImage = unsupported === "missing" ? undefined : () => { throw new Error("unsupported drag image"); };
      const started = h.start(h.records()[0], transfer);
      assert.equal(started.event.defaultPrevented, false); assert.equal(h.read("datasetDragGhost"), null);
      assert.equal(h.read("datasetDragImage"), null); assert.equal(h.document.body.children.length, 0);
      h.drop(transfer); assert.equal(h.read("selected"), A.path); assert.equal(h.writes().length, 0);
    });
  }
  await check("a second drag removes its predecessor's ghost before creating another", async () => {
    const h = await harness(); h.start(); const old = h.read("datasetDragGhost"); h.start(h.records()[1]);
    assert.equal(old.parentElement, null); assert.equal(h.document.body.querySelectorAll(".dataset-drag-ghost").length, 1);
    assert.equal(h.document.body.querySelectorAll(".dataset-drag-image").length, 1);
    assert.match(h.read("datasetDragGhost.textContent"), /second\.parquet/);
  });
  await check("ghost uses only a text filename and size, never backend path or executable HTML", async () => {
    const name = '<img src=x onerror="attack">.csv';
    const h = await harness({ files: [{ path: "/synthetic/private/" + name, size_mb: 3 }] }); h.start();
    const ghost = h.read("datasetDragGhost");
    assert.match(ghost.textContent, /<img src=x onerror="attack">\.csv/); assert.match(ghost.textContent, /3 MB/);
    assert.equal(ghost.querySelectorAll("img").length, 0); assert.equal(ghost.textContent.includes("/synthetic/private"), false);
    assert.equal(h.writes().length, 0);
  });
  await check("a press and movement below threshold remains a normal file click", async () => {
    const h = await pointerHarness(), record = h.records()[0]; h.app.els.prompt.value = "keep this instruction";
    h.pointer(record); h.move({ clientX: 42, clientY: 52 });
    assert.equal(h.read("datasetDrag"), null); assert.equal(h.read("datasetDragGhost"), null);
    h.up({ clientX: 42, clientY: 52 }); record.button.click();
    assert.equal(h.read("selected"), A.path); assert.equal(record.button.captures.size, 0);
    assert.equal(h.app.els.prompt.value, "keep this instruction"); assert.equal(h.writes().length, 0);
  });
  await check("mouse pointer drag captures the gesture and stages only on a valid release", async () => {
    const h = await pointerHarness(), record = h.records()[0]; h.app.els.prompt.value = "my own instruction";
    h.pointer(record); h.move();
    assert.ok(record.button.captures.has(1)); assert.ok(h.read("datasetDragGhost"));
    assert.equal(h.read("selected"), null); assert.equal(h.writes().length, 0);
    h.up();
    assert.equal(h.read("selected"), A.path); assert.equal(record.button.captures.size, 0);
    assert.equal(h.app.els.prompt.value, "my own instruction"); assert.equal(h.document.activeElement, h.app.els.prompt);
    assert.equal(h.read("datasetDragGhost"), null); assert.equal(h.frames.size, 0); assert.equal(h.writes().length, 0);
  });
  await check("releasing outside the result zone does not select or trigger the synthetic click", async () => {
    const h = await pointerHarness(), record = h.records()[0];
    h.pointer(record); h.move({ clientX: 300, clientY: 300 }); h.up({ clientX: 300, clientY: 300 });
    record.button.dispatch("click", { detail: 1 });
    assert.equal(h.read("selected"), null); assert.equal(h.read("datasetDragGhost"), null);
    assert.equal(record.button.captures.size, 0); assert.equal(h.writes().length, 0);
  });
  await check("a completed pointer drag does not select twice on its browser-generated click", async () => {
    const h = await pointerHarness(), record = h.records()[0]; h.pointer(record); h.move(); h.up();
    const before = h.read("catalogRequest"); h.document.activeElement = null;
    record.button.dispatch("click", { detail: 1 });
    assert.equal(h.read("catalogRequest"), before); assert.equal(h.document.activeElement, null);
    assert.equal(h.read("selected"), A.path); assert.equal(h.writes().length, 0);
  });
  for (const point of [{ button: 2, buttons: 2 }, { isPrimary: false }]) {
    await check("unsupported pointer " + JSON.stringify(point) + " does not steal scrolling or selection", async () => {
      const h = await pointerHarness(), record = h.records()[0]; const down = h.pointer(record, "pointerdown", point);
      h.move(); h.up();
      assert.equal(down.defaultPrevented, false); assert.equal(h.read("datasetDrag"), null);
      assert.equal(h.read("selected"), null); assert.equal(record.button.captures.size, 0);
      assert.equal(h.writes().length, 0);
    });
  }
  await check("a secondary pointer cannot move or finish the active gesture", async () => {
    const h = await pointerHarness(), record = h.records()[0]; h.pointer(record); h.move();
    h.up({ pointerId: 2 }); assert.equal(h.read("selected"), null); assert.ok(h.read("datasetDragGhost"));
    h.up(); assert.equal(h.read("selected"), A.path); assert.equal(h.writes().length, 0);
  });
  for (const reason of ["pointercancel", "blur", "Escape", "hidden", "backend change", "list refresh", "busy", "pending", "unknown"]) {
    await check("pointer " + reason + " releases capture and prevents late release selection", async () => {
      const h = await pointerHarness(), record = h.records()[0]; h.pointer(record); h.move();
      assert.ok(h.read("datasetDragGhost"));
      if (reason === "pointercancel") h.global("pointercancel", { pointerId: 1 });
      else if (reason === "blur") h.global("blur");
      else if (reason === "Escape") h.global("keydown", { key: "Escape" });
      else if (reason === "hidden") h.setHidden(true);
      else if (reason === "backend change") { h.app.adoptJobCatalog(catalog("backend-B")); await flush(); }
      else if (reason === "list refresh") await h.app.loadFiles();
      else { h.read(reason === "busy" ? "backendBusy = true" : reason === "pending" ? "submissionPending = true" : "jobsKnown = false"); h.app.syncJobControls(); }
      h.up();
      assert.equal(h.read("selected"), null); assert.equal(h.read("datasetDragGhost"), null);
      assert.equal(record.button.captures.size, 0); assert.equal(h.frames.size, 0); assert.equal(h.writes().length, 0);
    });
  }
  await check("native HTML drag still works when pointer events are unsupported", async () => {
    const h = await harness({ pointers: false }), { transfer } = h.start();
    assert.deepEqual(transfer.types, [TYPE]); h.drop(transfer);
    assert.equal(h.read("selected"), A.path); assert.equal(h.writes().length, 0);
  });
  await check("native dragstart cannot create a second drag while a pointer gesture is active", async () => {
    const h = await pointerHarness(), record = h.records()[0]; h.pointer(record); h.move();
    const ghost = h.read("datasetDragGhost"), native = h.start(record);
    assert.equal(native.event.defaultPrevented, true); assert.equal(h.read("datasetDragGhost"), ghost);
    h.up(); assert.equal(h.read("selected"), A.path); assert.equal(h.writes().length, 0);
  });
  await check("pointer bursts use one frame with the latest position and no idle drag loop", async () => {
    const h = await pointerHarness({ reduced: false }), record = h.records()[0]; h.nextFrame();
    assert.equal(h.frames.size, 0); h.pointer(record);
    for (let x = 500; x <= 600; x++) h.move({ clientX: x, clientY: 300 });
    assert.equal(h.frames.size, 1, "drag and ambient motion must not compete while dragging");
    const ghost = h.read("datasetDragGhost"); h.nextFrame();
    assert.equal(ghost.style.getPropertyValue("--drag-x"), "618px");
    assert.equal(ghost.style.getPropertyValue("--drag-y"), "318px");
    assert.equal(h.frames.size, 0, "an unmoving pointer must not schedule another drag frame");
    h.move({ clientX: 610, clientY: 310 }); assert.equal(h.frames.size, 1);
    h.up({ clientX: 620, clientY: 320 });
    assert.equal(ghost.style.getPropertyValue("--drag-x"), "638px", "release coordinates must be flushed before measuring landing");
    assert.equal(h.frames.size, 0); assert.equal(h.read("datasetPointerFrame"), null);
    assert.equal(h.read("datasetPointer"), null); assert.equal(h.read("selected"), A.path);
  });
  await check("reduced motion retains frame-coalesced dragging without tilt or landing animation", async () => {
    const h = await pointerHarness({ reduced: true }), record = h.records()[0]; h.pointer(record); h.move();
    const ghost = h.read("datasetDragGhost"); assert.equal(h.frames.size, 1); h.nextFrame();
    assert.equal(ghost.style.getPropertyValue("--drag-tilt"), "0deg"); assert.equal(h.frames.size, 0);
    h.up(); assert.equal(h.nodes.get("dataset-stage").animations.length, 0);
    assert.equal(h.read("datasetLandingAnimation"), null); assert.equal(h.read("selected"), A.path);
  });
  await check("landing animates from the release rectangle and measures it only at release", async () => {
    const h = await pointerHarness({ reduced: false }), record = h.records()[0]; h.nextFrame();
    h.pointer(record); h.move(); const ghost = h.read("datasetDragGhost"); let reads = 0;
    ghost.getBoundingClientRect = () => { reads++; return { left: 620, top: 330, width: 256, height: 90 }; };
    h.nextFrame(); h.move({ clientX: 620 }); h.nextFrame(); assert.equal(reads, 0);
    h.up(); assert.equal(reads, 1);
    const animation = h.read("datasetLandingAnimation"), card = h.nodes.get("dataset-stage");
    assert.equal(card.animations.length, 1); assert.equal(animation, card.animations[0]);
    assert.equal(animation.keyframes[0].transform, "translate3d(200px, 110px, 0) scale(0.8, 1)");
    assert.equal(animation.keyframes[1].transform, "none");
    assert.equal(animation.options.duration, 380); assert.match(animation.options.easing, /cubic-bezier/);
    animation.emit("finish"); assert.equal(h.read("datasetLandingAnimation"), null);
    assert.equal(h.frames.size, 0); assert.equal(h.writes().length, 0);
  });
  for (const reason of ["new selection", "new drag", "blur", "Escape", "hidden", "reduced motion", "backend change"]) {
    await check("landing " + reason + " cancels and releases its animation handle", async () => {
      const h = await pointerHarness({ reduced: false }), record = h.records()[0]; h.nextFrame();
      h.pointer(record); h.move(); h.up(); const animation = h.read("datasetLandingAnimation"); assert.ok(animation);
      if (reason === "new selection") h.records()[1].button.click();
      else if (reason === "new drag") { h.pointer(h.records()[1]); h.move(); }
      else if (reason === "blur") h.global("blur");
      else if (reason === "Escape") h.global("keydown", { key: "Escape" });
      else if (reason === "hidden") h.setHidden(true);
      else if (reason === "reduced motion") h.setReduced(true);
      else { h.app.adoptJobCatalog(catalog("backend-B")); await flush(); }
      assert.equal(animation.canceled, true); assert.equal(h.read("datasetLandingAnimation"), null);
      assert.equal(h.writes().length, 0);
    });
  }
  for (const unsupported of ["missing", "throws"]) {
    await check("landing animation " + unsupported + " cannot prevent file selection", async () => {
      const h = await pointerHarness({ reduced: false }); h.nextFrame(); const card = h.document.getElementById("dataset-stage");
      card.animate = unsupported === "missing" ? undefined : () => { throw new Error("animation unavailable"); };
      h.pointer(); h.move(); h.up(); assert.equal(h.read("selected"), A.path);
      assert.equal(h.read("datasetLandingAnimation"), null); assert.equal(h.writes().length, 0);
    });
  }
  await check("losing pointer capture cancels the gesture and its pending frame", async () => {
    const h = await pointerHarness(), record = h.records()[0]; h.pointer(record); h.move();
    record.button.dispatch("lostpointercapture", { pointerId: 1 }); h.up();
    assert.equal(h.read("selected"), null); assert.equal(h.read("datasetPointer"), null);
    assert.equal(record.button.captures.size, 0); assert.equal(h.frames.size, 0);
  });
  await check("mouse-button release without pointerup cancels stale dragging", async () => {
    const h = await pointerHarness(), record = h.records()[0]; h.pointer(record); h.move(); h.move({ buttons: 0 });
    h.up(); assert.equal(h.read("selected"), null); assert.equal(h.read("datasetDragGhost"), null);
    assert.equal(record.button.captures.size, 0); assert.equal(h.frames.size, 0);
  });
  await check("capture failure safely abandons dragging without selecting", async () => {
    const h = await pointerHarness(), record = h.records()[0];
    record.button.setPointerCapture = () => { throw new Error("pointer already inactive"); };
    h.pointer(record); h.move(); h.up();
    assert.equal(h.read("selected"), null); assert.equal(h.read("datasetPointer"), null);
    assert.equal(h.read("datasetDragGhost"), null); assert.equal(h.frames.size, 0);
  });
  await check("a dialog covering the result zone cannot receive a drop through its geometry", async () => {
    const h = await pointerHarness(); h.document.elementFromPoint = () => new Node("dialog");
    h.pointer(); h.move(); h.nextFrame();
    assert.equal(h.nodes.get("result-dropzone").classList.contains("drag-over"), false);
    h.up(); assert.equal(h.read("selected"), null); assert.equal(h.writes().length, 0);
  });
  await check("vertical touch movement stays available to native scrolling", async () => {
    const h = await pointerHarness(), record = h.records()[0];
    h.pointer(record, "pointerdown", { pointerType: "touch" }); const move = h.move({ pointerType: "touch", clientX: 42, clientY: 90 });
    h.up({ pointerType: "touch" }); assert.equal(move.defaultPrevented, false);
    assert.equal(h.read("datasetPointer"), null); assert.equal(record.button.captures.size, 0);
    assert.equal(h.read("selected"), null); assert.equal(h.frames.size, 0);
  });
  for (const pointerType of ["pen", "touch"]) {
    await check(pointerType + " can deliberately drag into the analysis area", async () => {
      const h = await pointerHarness(), record = h.records()[0]; h.pointer(record, "pointerdown", { pointerType });
      h.move({ pointerType, clientX: 100, clientY: 52 }); h.move({ pointerType }); h.up({ pointerType });
      assert.equal(h.read("selected"), A.path); assert.equal(record.button.captures.size, 0);
      assert.equal(h.writes().length, 0);
    });
  }
  await check("keyboard activation remains available immediately after dragging", async () => {
    const h = await pointerHarness(), record = h.records()[0]; h.pointer(record); h.move(); h.up({ clientX: 300 });
    record.button.dispatch("click", { detail: 0 });
    assert.equal(h.read("selected"), A.path); assert.equal(h.writes().length, 0);
  });
  await check("removing a staged card clears only its selection and keeps the typed instruction", async () => {
    const h = await pointerHarness(), record = h.records()[0]; h.app.els.prompt.value = "please preserve my draft";
    h.pointer(record); h.move(); h.up();
    h.read("session = 'synthetic-warm-session'; currentJob = { id: 'old-job', instance: 'backend-A' }; rememberJob(currentJob); stream = new EventSource('/synthetic/read-only-events')");
    const stream = h.sources[h.sources.length - 1], before = h.read("catalogRequest");
    for (const id of ["answer", "kpis", "tables", "charts", "chart", "log"]) h.nodes.get(id).textContent = "OLD RESULT";
    h.nodes.get("stage-remove").click();
    assert.equal(h.read("selected"), null); assert.equal(h.read("stagedDataset"), null);
    assert.equal(h.read("session"), null); assert.equal(h.read("currentJob"), null); assert.equal(h.saved.size, 0);
    assert.equal(stream.closed, true); assert.equal(h.read("catalogRequest"), before + 1);
    assert.equal(h.nodes.get("dataset-stage").hidden, true); assert.equal(h.app.els.placeholder.hidden, false);
    assert.equal(h.nodes.get("result-dropzone").classList.contains("is-staged"), false);
    assert.equal(record.button.getAttribute("aria-pressed"), "false");
    assert.equal(h.app.els.prompt.value, "please preserve my draft"); assert.equal(h.document.activeElement, record.button);
    assert.ok(!h.app.els.file.textContent.includes("first.csv"));
    for (const id of ["answer", "kpis", "tables", "charts", "chart", "log"]) assert.ok(!h.nodes.get(id).textContent.includes("OLD RESULT"), id);
    assert.equal(h.writes().length, 0, "removing a card must not delete files or release a remote session");
  });
  for (const [name, condition] of [["unknown", "jobsKnown = false"], ["busy", "backendBusy = true"], ["pending", "submissionPending = true"]]) {
    await check(name + " disables and guards the remove control without mutating a staged dataset", async () => {
      const h = await pointerHarness(); h.pointer(); h.move(); h.up(); h.read(condition); h.app.syncJobControls();
      const remove = h.nodes.get("stage-remove"), before = h.read("catalogRequest"); assert.equal(remove.disabled, true);
      remove.click();
      assert.equal(h.read("selected"), A.path); assert.ok(h.read("stagedDataset"));
      assert.equal(h.nodes.get("dataset-stage").hidden, false); assert.equal(h.read("catalogRequest"), before);
      assert.equal(h.writes().length, 0);
    });
  }
  await check("removing the card twice is idempotent and does not discard a draft", async () => {
    const h = await pointerHarness(); h.app.els.prompt.value = "keep"; h.pointer(); h.move(); h.up();
    const remove = h.nodes.get("stage-remove"); remove.click(); const before = h.read("catalogRequest");
    h.document.activeElement = null; remove.click();
    assert.equal(h.read("catalogRequest"), before); assert.equal(h.read("selected"), null);
    assert.equal(h.app.els.prompt.value, "keep"); assert.equal(h.document.activeElement, null);
    assert.equal(h.writes().length, 0);
  });
  await check("remove without any staged selection is disabled and has no side effects", async () => {
    const h = await pointerHarness(), remove = h.nodes.get("stage-remove"), before = h.read("catalogRequest");
    h.app.els.prompt.value = "unattached draft"; assert.equal(remove.disabled, true); remove.click();
    assert.equal(h.read("selected"), null); assert.equal(h.read("stagedDataset"), null);
    assert.equal(h.read("catalogRequest"), before); assert.equal(h.app.els.prompt.value, "unattached draft");
    assert.equal(h.document.activeElement, null); assert.equal(h.writes().length, 0);
  });
  await check("removing a card stops its landing animation and clears the arrival timer", async () => {
    const h = await pointerHarness({ reduced: false }); h.nextFrame(); h.pointer(); h.move(); h.up();
    const animation = h.read("datasetLandingAnimation"), timer = h.read("datasetDropTimer");
    assert.ok(animation); assert.ok(h.timers.has(timer)); h.nodes.get("stage-remove").click();
    assert.equal(animation.canceled, true); assert.equal(h.read("datasetLandingAnimation"), null);
    assert.equal(h.read("datasetDropTimer"), null); assert.equal(h.timers.has(timer), false);
    assert.equal(h.nodes.get("result-dropzone").classList.contains("drop-received"), false);
    assert.equal(h.writes().length, 0);
  });
  await check("removing the staged file cancels a second in-progress drag and its late release", async () => {
    const h = await pointerHarness(), first = h.records()[0], second = h.records()[1];
    first.button.click(); h.pointer(second); h.move(); assert.ok(h.read("datasetDragGhost"));
    h.nodes.get("stage-remove").click(); h.up();
    assert.equal(h.read("selected"), null); assert.equal(h.read("datasetDragGhost"), null);
    assert.equal(h.read("datasetPointer"), null); assert.equal(second.button.captures.size, 0);
    assert.equal(h.frames.size, 0); assert.equal(h.document.activeElement, first.button);
    assert.equal(h.writes().length, 0);
  });
  await check("removing a file no longer in the source list returns focus to the prompt", async () => {
    const h = await pointerHarness(); h.pointer(); h.move(); h.up();
    h.setFiles({ files: [B], count: 1 }); await h.app.loadFiles(); h.document.activeElement = null;
    h.nodes.get("stage-remove").click();
    assert.equal(h.read("selected"), null); assert.equal(h.document.activeElement, h.app.els.prompt);
    assert.equal(h.writes().length, 0);
  });
  await check("an explicit question after removing a card does not submit the removed file", async () => {
    const h = await pointerHarness(); h.app.els.prompt.value = "answer this without the removed file";
    h.pointer(); h.move(); h.up(); h.nodes.get("stage-remove").click(); assert.equal(h.writes().length, 0);
    h.read("canAsk = true"); await h.app.ask();
    assert.equal(h.writes().length, 1); assert.equal(h.writes()[0].url, "/api/ask");
    assert.deepEqual(JSON.parse(h.writes()[0].options.body), { text: "answer this without the removed file" });
  });
  await check("remove control stays bilingual and is a non-submitting accessible button", async () => {
    const h = await pointerHarness(); h.pointer(); h.move(); h.up();
    const remove = h.nodes.get("stage-remove"), chinese = remove.getAttribute("aria-label");
    assert.ok(chinese && /移除/.test(chinese)); assert.ok(remove.getAttribute("title") || remove.title);
    h.read("LANG = 'en'"); h.app.applyChrome();
    assert.match(remove.getAttribute("aria-label"), /remove/i); assert.notEqual(remove.getAttribute("aria-label"), chinese);
    const html = fs.readFileSync(path.join(path.dirname(sourcePath), "index.html"), "utf8");
    assert.match(html, /id="stage-remove"[^>]*type="button"/);
  });
  await check("drop markup uses a live status and non-submitting focus button", async () => {
    const html = fs.readFileSync(path.join(path.dirname(sourcePath), "index.html"), "utf8");
    assert.match(html, /id="drop-hint"[^>]*role="status"[^>]*aria-live="polite"/);
    assert.match(html, /id="stage-focus"[^>]*type="button"/);
    assert.match(html, /id="dataset-stage"[^>]*hidden/);
  });
  console.log(`${checks} dataset drag checks passed; ${failures} failed`);
  if (failures) process.exitCode = 1;
})().catch((error) => { console.error(error.stack); process.exitCode = 1; });
