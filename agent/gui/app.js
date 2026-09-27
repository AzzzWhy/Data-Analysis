/* Browser side of the workbench.
 *
 * Two rules, both there because the previous attempt at this screen broke them:
 *   - Nothing is computed here. Every engine label comes from the server's chip_state() over the
 *     real execution_decision, every memory figure comes from the worker's own list reply, and
 *     every number comes from a tool result. If a field is absent, the row is left empty rather
 *     than filled with something plausible.
 *   - Nothing is polled. The worker's list command prunes its own cache before answering, so a
 *     timer here would expire the frames it was trying to display. The card updates after a run
 *     or an explicit release, and only then.
 *
 * No innerHTML with engine data, and no inline styles: the CSP serves this file with
 * script-src 'self' and style-src 'self', and the escape-first rule the report generator uses
 * applies here too.
 */
"use strict";

const $ = (id) => document.getElementById(id);
const els = {
  banner: $("banner"), model: $("ctx-model"), host: $("ctx-host"), file: $("ctx-file"),
  files: $("files"), card: $("sess-card"), state: $("sess-state"), count: $("sess-count"),
  steps: $("sess-steps"), mb: $("sess-mb"), frames: $("sess-frames"), hint: $("sess-hint"),
  release: $("release"), run: $("run"), log: $("log"), answer: $("answer"),
  charts: $("charts"), tables: $("tables"), phase: $("phase"), chip: $("chip"),
  elapsed: $("elapsed"), tool: $("tool"), sessionOp: $("session-op"), op: $("op"),
  by: $("by"), agg: $("agg"), forceCpu: $("force-cpu"), forceGpu: $("force-gpu"),
};

let selected = null;      // absolute path of the chosen dataset
let session = null;       // last session_id we opened, so "analyze" can reuse it
let stream = null;        // active EventSource

/* ---------------------------------------------------------------- text helpers */

function text(node, value) {
  node.textContent = "";
  return node;
}

function line(container, kind, content) {
  const span = document.createElement("span");
  span.className = kind;
  span.textContent = content + "\n";
  container.appendChild(span);
  container.scrollTop = container.scrollHeight;
  return span;
}

function cell(row, value, numeric) {
  const td = document.createElement("td");
  if (numeric) td.className = "num";
  td.textContent = value === null || value === undefined ? "—" : String(value);
  row.appendChild(td);
  return td;
}

/* -------------------------------------------------------------------- chrome */

function setChip(chip) {
  els.chip.className = "chip " + (chip && chip.class ? chip.class : "muted");
  els.chip.textContent = chip && chip.label ? chip.label : "no result yet";
  els.chip.title = chip && chip.note ? chip.note : "";
}

function renderState(state) {
  els.model.textContent = state.model || "未配置";
  els.host.textContent = location.host;

  const notes = [];
  if (!state.agent_available) {
    notes.push("模型客户端不可用：" + (state.agent_import_error || "未知原因") +
               "\n安装：pip install -r requirements.txt\n" +
               "在此之前，下方“直接工具调用”仍然真实可用，提问区不可用。");
  } else if (!state.config_ready) {
    notes.push("模型客户端已安装，但没有配置 API 地址 / 密钥 / 模型。" +
               (state.model_error ? "\n" + state.model_error : ""));
  }
  if (!state.engine_ready) {
    notes.unshift("分析引擎未响应：" + state.engine_error);
  }

  if (notes.length) {
    els.banner.hidden = false;
    els.banner.dataset.kind = state.engine_ready ? "warn" : "bad";
    els.banner.textContent = notes.join("\n\n");
  } else {
    els.banner.hidden = false;
    els.banner.dataset.kind = "ok";
    els.banner.textContent = "引擎与模型客户端均就绪。";
  }
  renderSession(state.session);
}

/* The card repeats what the worker said. `null` means it did not say, and is shown as such --
   rendering an unknown as 0 would claim the memory was freed. */
function renderSession(doc) {
  if (!doc) return;
  const wedged = Boolean(doc.error);
  const active = (doc.sessions_detail || [])[0] || null;
  els.card.dataset.state = wedged ? "unknown" : (active ? "active" : "released");
  els.state.textContent = wedged ? "worker 未响应"
    : (active ? "active（会话打开中）" : (doc.sessions ? "active" : "cold / released"));
  els.count.textContent = doc.sessions === null ? "未知" : doc.sessions;
  els.steps.textContent = active ? active.steps : "—";
  els.mb.textContent = active
    ? (active.resident_mb === null ? "未知" : active.resident_mb + " MB")
    : (doc.warm_cache_mb === null || doc.warm_cache_mb === undefined ? "—" : doc.warm_cache_mb + " MB（保留帧）");
  els.frames.textContent = doc.warm_frames === null ? "未知"
    : doc.warm_frames + " 帧" + (doc.warm_frames === 0 && !active ? "（本机无 cuDF 时不会累积）" : "");
  els.hint.textContent = wedged ? doc.error
    : (doc.reused ? "上一次结果来自复用，未重新读盘。" : "保留帧只报数量与总字节：worker 的 list 不报每个帧是哪个文件。");
}

/* ------------------------------------------------------------------- results */

function renderToolResult(payload) {
  const result = payload.result || {};
  const chip = payload.chip;
  els.elapsed.textContent = (payload.seconds !== undefined ? payload.seconds.toFixed(2) : "?") + "s";
  line(els.log, "tool", `-> ${payload.name}(${JSON.stringify(payload.args || {})})`);
  if (result.success === false) {
    // "no result yet" would claim the run is still pending when it actually failed. The engine
    // made no statement about hardware here, so the chip says the one true thing instead.
    els.chip.className = "chip bad";
    els.chip.textContent = "运行失败";
    els.chip.title = String(result.error || "");
    line(els.log, "bad", `   FAILED ${result.error || ""}`);
    if (result.hint) line(els.log, "dim", `   hint: ${result.hint}`);
    return;
  }
  setChip(chip);
  const engine = result.engine || (result.execution_decision || {}).actual_backend;
  line(els.log, "ok", `   OK engine=${engine} rows=${result.rows_scanned ?? "?"} ` +
       `${result.seconds ?? payload.seconds ?? "?"}s`);
  if (chip && chip.note) line(els.log, "reason", `   ${chip.label}: ${chip.note}`);
  if (result.plan) renderPlan(result.plan);
  renderArtifacts(result);
  renderTables(result);
}

function renderPlan(plan) {
  line(els.log, "dim", `   plan ${plan.kind} step ${plan.next_step}/${plan.total_steps}`);
}

function renderArtifacts(result) {
  const charts = Array.isArray(result.charts) ? result.charts : [];
  if (!charts.length && !result.report) return;
  text(els.charts);
  charts.forEach((path) => {
    if (!/\.svg$/i.test(path)) return;
    const box = document.createElement("div");
    box.className = "chartbox";
    const img = document.createElement("img");
    img.alt = path.split(/[\\/]/).pop();
    img.src = "/artifact?path=" + encodeURIComponent(path);
    img.addEventListener("error", () => {
      text(box);
      line(box, "bad", `图表无法载入：${img.alt}（不在允许目录内，或文件不存在）`);
    });
    const cap = document.createElement("div");
    cap.className = "cap";
    cap.textContent = img.alt;
    box.appendChild(img);
    box.appendChild(cap);
    els.charts.appendChild(box);
  });
  if (result.report) {
    const a = document.createElement("a");
    a.textContent = "打开报告 report.md";
    a.href = "/artifact?path=" + encodeURIComponent(result.report);
    a.target = "_blank";
    a.rel = "noopener";
    els.charts.appendChild(a);
  }
}

function renderTables(result) {
  const inner = result.result || {};
  const blocks = [];
  if (inner.groupby && Array.isArray(inner.groupby.top_k)) {
    blocks.push(["groupby by " + inner.groupby.by, inner.groupby.top_k]);
  }
  if (inner.summary && inner.summary.stats) {
    const rows = Object.entries(inner.summary.stats).map(([column, s]) => ({
      column, mean: s.mean, median: s.median, min: s.min, max: s.max, nulls: s.nulls,
    }));
    blocks.push(["summary", rows]);
  }
  if (!blocks.length) return;
  text(els.tables);
  blocks.forEach(([title, rows]) => {
    if (!rows.length) return;
    const h = document.createElement("div");
    h.className = "cap";
    h.textContent = title;
    els.tables.appendChild(h);
    const table = document.createElement("table");
    const head = table.createTHead().insertRow();
    Object.keys(rows[0]).forEach((key) => cell(head, key, false));
    rows.forEach((row) => {
      const tr = table.insertRow();
      Object.entries(row).forEach(([key, value]) =>
        cell(tr, typeof value === "number" ? value.toLocaleString(undefined,
              { maximumFractionDigits: 4 }) : value, typeof value === "number"));
    });
    els.tables.appendChild(table);
  });
}

/* --------------------------------------------------------------------- runs */

function buildArgs() {
  const tool = els.tool.value;
  const args = {};
  if (tool === "list_datasets") return args;
  if (selected) args.file_path = selected;
  if (tool === "analyze_dataset") {
    args.operation = els.op.value;
  } else if (tool === "export_deliverables") {
    args.operation = els.op.value === "profile" ? "auto" : els.op.value;
  }
  if (tool === "export_deliverables" || tool === "analyze_dataset") {
    if (els.by.value.trim()) args.by = els.by.value.trim();
    if (els.agg.value.trim()) args.agg = els.agg.value.trim();
    if (tool === "analyze_dataset") args.force_cpu = els.forceCpu.checked;
  }
  if (tool === "dataset_session") {
    const operation = els.sessionOp.value;
    args.operation = operation;
    if (operation === "analyze") {
      args.session_id = session || undefined;
      args.op = els.op.value;
      if (els.by.value.trim()) args.by = els.by.value.trim();
      if (els.agg.value.trim()) args.agg = els.agg.value.trim();
    } else if (operation === "close") {
      args.session_id = session || "all";
    } else if (operation === "open") {
      args.force_cpu = els.forceCpu.checked;
      args.force_gpu = els.forceGpu.checked;
    }
  }
  return args;
}

function trackSession(payload) {
  const result = payload.result || {};
  if (result.session_id) session = result.session_id;
  if ((els.tool.value === "dataset_session" && els.sessionOp.value === "close")
      || result.closed !== undefined) {
    session = null;
  }
}

function attach(jobId) {
  if (stream) stream.close();
  stream = new EventSource("/api/events?job=" + encodeURIComponent(jobId));
  stream.addEventListener("phase", (e) => { els.phase.textContent = JSON.parse(e.data).phase; });
  stream.addEventListener("trace", (e) => {
    const body = JSON.parse(e.data);
    line(els.log, body.line.includes("FAILED") ? "bad" : "dim", body.line);
  });
  stream.addEventListener("tool_call", (e) => {
    const body = JSON.parse(e.data);
    if (body.dropped_args && body.dropped_args.length) {
      line(els.log, "bad", `   已忽略未知参数：${body.dropped_args.join(", ")}`);
    }
  });
  stream.addEventListener("tool_result", (e) => {
    const body = JSON.parse(e.data);
    trackSession(body);
    renderToolResult(body);
  });
  stream.addEventListener("answer", (e) => {
    const body = JSON.parse(e.data);
    els.answer.hidden = false;
    // Plain text, deliberately. The report generator has an escape-first Markdown renderer and
    // the browser copy will mirror it; until then a model answer is shown exactly as authored
    // rather than half-parsed.
    els.answer.textContent = body.text;
  });
  stream.addEventListener("session", (e) => renderSession(JSON.parse(e.data)));
  stream.addEventListener("error", (e) => {
    let message = "连接中断";
    try { message = JSON.parse(e.data).message; } catch (ignored) {}
    line(els.log, "bad", `!! ${message}`);
  });
  stream.addEventListener("done", () => {
    els.phase.textContent = "完成";
    els.run.disabled = false;
    stream.close();
    stream = null;
  });
}

async function post(path, body) {
  const response = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body || {}),
  });
  return { status: response.status, data: await response.json().catch(() => ({})) };
}

async function run() {
  if (!selected && els.tool.value !== "list_datasets") {
    line(els.log, "bad", "!! 先在左侧选择一个数据文件");
    return;
  }
  els.run.disabled = true;
  els.phase.textContent = "提交中";
  const { status, data } = await post("/api/run", { tool: els.tool.value, args: buildArgs() });
  if (status === 409) { els.run.disabled = false; return line(els.log, "bad", "!! 已有一次运行在进行中"); }
  if (status >= 400) { els.run.disabled = false; return line(els.log, "bad", `!! ${data.error || status}`); }
  line(els.log, "dim", `\n— ${data.note || "direct tool call"}: ${els.tool.value}`);
  attach(data.job_id);
}

/* -------------------------------------------------------------------- startup */

async function loadFiles() {
  const response = await fetch("/api/files");
  const payload = await response.json().catch(() => ({ files: [] }));
  text(els.files);
  (payload.files || []).forEach((entry) => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "file";
    button.setAttribute("aria-pressed", "false");
    const name = document.createElement("span");
    name.className = "name";
    name.textContent = entry.path.split(/[\\/]/).pop();
    name.title = entry.path;
    const size = document.createElement("span");
    size.className = "size";
    size.textContent = entry.size_mb + " MB";
    button.appendChild(name);
    button.appendChild(size);
    button.addEventListener("click", () => {
      els.files.querySelectorAll(".file").forEach((x) => x.setAttribute("aria-pressed", "false"));
      button.setAttribute("aria-pressed", "true");
      selected = entry.path;
      session = null;
      els.file.textContent = name.textContent;
    });
    els.files.appendChild(button);
  });
  if (!(payload.files || []).length) {
    line(els.files, "dim", payload.note || "没有可分析的数据文件");
  }
}

els.tool.addEventListener("change", () => {
  els.sessionOp.disabled = els.tool.value !== "dataset_session";
});
$("runner").addEventListener("submit", (event) => { event.preventDefault(); run(); });
els.release.addEventListener("click", async () => {
  const { data } = await post("/api/session/release");
  session = null;
  if (data.error) {
    line(els.log, "bad", `!! 释放失败：${data.error}`);
  } else {
    line(els.log, "ok", `释放：关闭 ${data.closed} 个会话，丢弃 ${data.warm_frames_dropped} 个保留帧；` +
         `之后 ${data.sessions_after} 会话 / ${data.warm_frames_after} 帧`);
  }
  if (data.state) renderSession({ ...data.state, reused: false });
});

loadFiles();
fetch("/api/state").then((r) => r.json()).then(renderState).catch(() => {});
