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
  charts: $("charts"), tables: $("tables"), kpis: $("kpis"), phase: $("phase"),
  chip: $("chip"), elapsed: $("elapsed"), tool: $("tool"), sessionOp: $("session-op"),
  op: $("op"),
  by: $("by"), agg: $("agg"), goal: $("goal"),
  forceCpu: $("force-cpu"), forceGpu: $("force-gpu"),
  prompt: $("prompt"), ask: $("ask"), askHint: $("ask-hint"),
};

let selected = null;      // absolute path of the chosen dataset
let session = null;       // last session_id we opened, so "analyze" can reuse it
let stream = null;        // active EventSource
let canAsk = false;       // whether /api/state says a configured client exists
let logVisible = true;    // the execution drawer

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

/* ------------------------------------------------------------------- chrome i18n */

let I18N = {};
let LANG = "zh";

/* Labels only. Model prose and engine values never pass through this table -- translating a
   model's answer in the frontend would be fabricating a translation it did not produce. */
function t(key) {
  return (LANG === "en" && I18N[key]) ? I18N[key] : key;
}

function applyChrome() {
  document.querySelectorAll(".lt, h1, h2, button, .row .k, .kpi .l").forEach((node) => {
    // Guard, not paranoia: assigning textContent destroys child elements. A label that wraps a
    // select used to be rewritten here and the control vanished with it, which silently broke
    // the tool form. Only leaf nodes are ever translated.
    if (node.children.length) return;
    if (node.dataset.label === undefined) node.dataset.label = node.textContent.trim();
    const translated = t(node.dataset.label);
    if (translated !== node.textContent) node.textContent = translated;
  });
  document.documentElement.lang = LANG === "en" ? "en" : "zh";
  $("lang").textContent = LANG === "zh" ? "中文" : "English";
  $("prompt").placeholder = LANG === "en"
    ? "e.g. Compare total and average revenue by region" : "例如：按地区统计 revenue 的总和与均值";
  $("goal").placeholder = t("例如：找出 revenue 离群点的成因");
  $("drawer-toggle").textContent = t("执行详情") + " · "
    + (logVisible ? t("收起") : t("展开"));
  // Plan rows are built once per reply, so the chrome sweep above never reaches them; redraw
  // them from the cached list rather than replaying the reply, which would double the off-plan
  // rows.
  renderPlanRows();
}

/* ------------------------------------------------------------- markdown (escape first) */

const HTML_ESCAPES = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" };
const escapeHtml = (value) => String(value).replace(/[&<>"]/g, (c) => HTML_ESCAPES[c]);

/*
 * Mirrors build_html_report._inline: escape the whole string FIRST, then substitute the inline
 * constructs. The order is the entire safety property. Substituting first would let text from the
 * model open a tag, and innerHTML would then honour an onerror= attribute in it. After escaping,
 * no angle bracket can survive to become a tag, so every tag below is one this file created.
 */
function inlineMarkdown(value) {
  let out = escapeHtml(value);
  out = out.replace(/`([^`]+)`/g, "<code>$1</code>");
  out = out.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
  out = out.replace(/(^|[^*\w])\*([^*\n]+)\*/g, "$1<em>$2</em>");
  return out;
}

const splitRow = (line) => line.trim().replace(/^\|/, "").replace(/\|$/, "").split("|").map((c) => c.trim());
const isTableRule = (line) => /^\s*\|?[\s:|-]*\|[\s:|-]+[-|:\s]*$/.test(line) && line.includes("-");

function renderMarkdown(container, source) {
  container.textContent = "";
  const lines = String(source || "").split("\n");
  let i = 0;
  while (i < lines.length) {
    const line = lines[i];
    if (!line.trim()) { i++; continue; }

    if (line.includes("|") && i + 1 < lines.length && isTableRule(lines[i + 1])) {
      const table = container.appendChild(document.createElement("table"));
      const head = table.createTHead().insertRow();
      splitRow(line).forEach((h) => {
        head.appendChild(document.createElement("th")).innerHTML = inlineMarkdown(h);
      });
      i += 2;
      while (i < lines.length && lines[i].includes("|")) {
        const row = table.insertRow();
        splitRow(lines[i]).forEach((c) => {
          row.appendChild(document.createElement("td")).innerHTML = inlineMarkdown(c);
        });
        i++;
      }
      continue;
    }

    const heading = /^(#{1,3})\s+(.*)$/.exec(line);
    if (heading) {
      container.appendChild(document.createElement("h" + (heading[1].length + 2)))
        .innerHTML = inlineMarkdown(heading[2]);
      i++;
      continue;
    }

    if (/^\s*[-*]\s+/.test(line)) {
      const list = container.appendChild(document.createElement("ul"));
      while (i < lines.length && /^\s*[-*]\s+/.test(lines[i])) {
        list.appendChild(document.createElement("li"))
          .innerHTML = inlineMarkdown(lines[i].replace(/^\s*[-*]\s+/, ""));
        i++;
      }
      continue;
    }

    const paragraph = [];
    // A pipe only ends the paragraph when that line actually starts a table. Excluding every
    // pipe-bearing line wedged the loop: nothing else advanced i, so prose such as
    // "engine=cudf | rows=2075259" emitted empty paragraphs forever.
    while (i < lines.length && lines[i].trim()
           && !(lines[i].includes("|") && i + 1 < lines.length && isTableRule(lines[i + 1]))
           && !/^\s*[-*]\s+/.test(lines[i]) && !/^#{1,3}\s/.test(lines[i])) {
      paragraph.push(lines[i]);
      i++;
    }
    container.appendChild(document.createElement("p"))
      .innerHTML = inlineMarkdown(paragraph.join("\n"));
  }
}

/* -------------------------------------------------------------------- chrome */

function setChip(chip) {
  els.chip.className = "chip " + (chip && chip.class ? chip.class : "muted");
  els.chip.textContent = chip && chip.label ? chip.label : "no result yet";
  els.chip.title = chip && chip.note ? chip.note : "";
}

function renderState(state) {
  I18N = state.i18n || {};
  LANG = state.language === "en" ? "en" : "zh";
  applyChrome();
  els.model.textContent = state.model || t("未配置模型");
  els.host.textContent = location.host;
  if (!selected) els.file.textContent = t("未选择数据文件");

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

  // The question box is the model-driven path, so it opens exactly when a configured client
  // exists and not one moment before. The reason is shown rather than left to be discovered.
  const canAskNow = Boolean(state.agent_available && state.config_ready);
  canAsk = canAskNow;
  els.prompt.disabled = !canAskNow;
  els.ask.disabled = !canAskNow;
  els.askHint.textContent = canAsk
    ? "提问会把选中的文件路径作为一行上下文附在问题后面，日志里会显示模型实际收到的原文。"
    : (!state.agent_available
      ? "提问需要模型客户端（pip install -r requirements.txt）。下面「直接工具调用」不需要它。"
      : "已安装 openai，但还没有配置 API 地址 / 密钥 / 模型。");

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
  // Status text goes through the chrome table like every other label; the worker's own words
  // are quoted verbatim elsewhere and are never translated.
  els.state.textContent = wedged ? t("worker 未响应")
    : (active ? t("active（会话打开中）")
      : (doc.sessions ? t("active（会话打开中）") : t("cold / released")));
  els.count.textContent = doc.sessions === null ? "未知" : doc.sessions;
  els.steps.textContent = active ? active.steps : "—";
  els.mb.textContent = active
    ? (active.resident_mb === null ? "未知" : active.resident_mb + " MB")
    : (doc.warm_cache_mb === null || doc.warm_cache_mb === undefined ? "—" : doc.warm_cache_mb + " MB（保留帧）");
  // Zero retained frames says nothing about whether this machine has cuDF -- a host that served
  // a `GPU · cuDF (reuse)` answer a moment ago has exactly zero of them once they are released,
  // so the old parenthetical contradicted the engine chip on the same screen. `未知` already
  // covers the case where the worker did not answer at all.
  els.frames.textContent = doc.warm_frames === null ? "未知" : doc.warm_frames + " 帧";
  els.hint.textContent = wedged ? doc.error
    : (doc.reused ? "上一次结果来自复用，未重新读盘。" : "保留帧只报数量与总字节：worker 的 list 不报每个帧是哪个文件。");
}

/* ------------------------------------------------------------------- results */

/* KPI tiles are built from fields the engine actually emitted. A field that is not there produces
   no tile -- the strip gets shorter, it never fills a gap with a plausible zero. */
function renderKpis(result) {
  const decision = result.execution_decision || {};
  const observed = decision.observed || {};
  const inner = result.result || {};
  const tiles = [];
  const add = (label, value, unit) => {
    if (value === null || value === undefined || value === "") return;
    tiles.push([label, value, unit || ""]);
  };

  add("扫描行数", typeof result.rows_scanned === "number"
    ? result.rows_scanned.toLocaleString() : result.rows_scanned);
  add("引擎", result.engine || observed.actual_backend);
  if (typeof result.seconds === "number") add("本次耗时", result.seconds.toFixed(2), "s");
  if (typeof observed.compute_seconds === "number"
      && observed.compute_seconds !== result.seconds) {
    add("纯计算", observed.compute_seconds.toFixed(3), "s");
  }
  if (inner.groupby && inner.groupby.groups !== undefined) {
    add("分组数", inner.groupby.groups);
  }
  if (inner.outliers && inner.outliers.results) {
    const total = Object.values(inner.outliers.results)
      .reduce((sum, entry) => sum + (entry && entry.outlier_count || 0), 0);
    if (total) add("离群点", total.toLocaleString());
  }
  if (result.steps_this_session !== undefined) add("步数", result.steps_this_session);
  if (typeof result.resident_mb === "number" && result.resident_mb > 0) {
    add("驻留内存", result.resident_mb, "MB");
  }
  const workflow = result.workflow_comparison || {};
  if (typeof workflow.speedup_x === "number") add("跨轮复用", workflow.speedup_x + "×");

  const strip = els.kpis;
  strip.textContent = "";
  tiles.forEach(([label, value, unit]) => {
    const box = document.createElement("div");
    box.className = "kpi";
    const l = document.createElement("div");
    l.className = "l";
    // Store the untranslated key and let applyChrome() localise it. Baking the translation in
    // here meant a language flip left every tile in the previous language until the next run.
    l.dataset.label = label;
    l.textContent = label;
    const v = document.createElement("div");
    v.className = "v";
    v.textContent = String(value);
    if (unit) {
      const u = document.createElement("span");
      u.className = "u";
      u.textContent = " " + unit;
      v.appendChild(u);
    }
    box.appendChild(l);
    box.appendChild(v);
    strip.appendChild(box);
  });
}

let planSteps = [];   // last step list the engine actually sent, see renderPlan
let lastPlan = null;  // the plan object those steps belong to, kept so a language flip can redraw

function clearPlanCard() {
  planSteps = [];
  lastPlan = null;
  $("plan-kind").textContent = "—";
  $("plan-progress").textContent = "—";
  $("plan-steps").textContent = "";
  $("plan-next").textContent = "";
}

function renderPlan(plan) {
  if (!plan) return;
  if (Array.isArray(plan.steps)) {
    // `open` replies are verbose and carry the whole list.
    planSteps = plan.steps.map((s) => Object.assign({}, s));
  } else if (Number.isInteger(plan.recorded_step) && planSteps[plan.recorded_step]) {
    // Analyze replies are terse -- the same dict is sent to the model every turn, so it drops
    // the step list and keeps only the index it just ticked off. Seconds are deliberately not
    // filled in here: the reply reports the whole round's duration, not that step's.
    planSteps[plan.recorded_step].done = true;
  }
  if (plan.off_plan_step && plan.off_plan_step.op) {
    planSteps.push({ op: plan.off_plan_step.op, note: plan.off_plan_note || "",
                     off_plan: true, done: true });
  }
  lastPlan = plan;
  $("plan-kind").textContent = plan.kind || "—";
  $("plan-progress").textContent = `${plan.completed ?? "—"} / ${plan.total_steps ?? "—"}`;
  renderPlanRows();
  // `do_next` is the engine's own instruction, including the columns it read out of the data.
  $("plan-next").textContent = plan.do_next || "";
}

/* Separate from renderPlan because redrawing must not re-apply the reply: calling renderPlan
   again would push the off-plan row a second time. */
function renderPlanRows() {
  const plan = lastPlan;
  if (!plan) return;
  const list = $("plan-steps");
  list.textContent = "";
  planSteps.forEach((step) => {
    const li = document.createElement("li");
    li.dataset.done = String(Boolean(step.done));
    li.dataset.offplan = String(Boolean(step.off_plan));
    li.dataset.current = String(!step.off_plan && plan.next_step === step.step);
    const dot = document.createElement("span");
    dot.className = "dot";
    dot.textContent = step.off_plan ? "•" : (step.done ? "✓" : (plan.next_step === step.step ? "▶" : "○"));
    const label = document.createElement("span");
    // A planned step's reason is the catalog's own sentence and is shown as written. An off-plan
    // one has no reason -- the engine instead explains its recording policy, which belongs in the
    // tooltip rather than taking three lines of the card.
    label.textContent = step.off_plan
      ? step.op + " — " + t("计划外步骤")
      : step.op + (step.reason ? " — " + step.reason : "");
    label.title = step.off_plan ? (step.note || "") : (step.reason || "");
    li.appendChild(dot);
    li.appendChild(label);
    if (typeof step.seconds === "number") {
      const sec = document.createElement("span");
      sec.className = "sec";
      sec.textContent = step.seconds.toFixed(2) + "s";
      li.appendChild(sec);
    }
    list.appendChild(li);
  });
  if (!planSteps.length) {
    const li = document.createElement("li");
    li.dataset.done = "none";
    li.textContent = t("未设定分析目标，因此没有计划");
    list.appendChild(li);
  }
}

function renderToolResult(payload) {
  const result = payload.result || {};
  const chip = payload.chip;
  els.elapsed.textContent = (payload.seconds !== undefined ? payload.seconds.toFixed(2) : "?") + "s";
  // Direct runs carry their own args here; model-driven runs already printed the call in the
  // trace line, so re-printing it with an empty argument object would be a duplicate that
  // looks like the model called the tool with nothing.
  if (payload.args && Object.keys(payload.args).length) {
    line(els.log, "tool", `-> ${payload.name}(${JSON.stringify(payload.args)})`);
  }
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
  renderKpis(result);
  if (result.plan) renderPlan(result.plan);
  renderArtifacts(result);
  renderTables(result);
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
      // The plan is generated from the goal at open time. Without a goal there is no plan, so
      // the card stays empty rather than inventing steps to look impressive.
      if (els.goal.value.trim()) args.goal = els.goal.value.trim();
    }
  }
  return args;
}

function trackSession(payload) {
  const result = payload.result || {};
  if (result.session_id) {
    session = result.session_id;
    // A fresh session with no goal has no plan. Leaving the previous one on screen would show
    // steps this session never had.
    if (!result.plan) clearPlanCard();
  }
  if ((els.tool.value === "dataset_session" && els.sessionOp.value === "close")
      || result.closed !== undefined) {
    session = null;
    clearPlanCard();
  }
}

function attach(jobId) {
  if (stream) stream.close();
  stream = new EventSource("/api/events?job=" + encodeURIComponent(jobId));
  stream.addEventListener("prompt", (e) => {
    // Echo exactly what the model received, including the appended file line, so the context
    // added on the operator's behalf is visible rather than implied.
    const body = JSON.parse(e.data);
    line(els.log, "dim", `   发给模型的原文：${body.text}`);
  });
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
    renderMarkdown(els.answer, body.text);
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
    els.ask.disabled = !canAsk;
    stream.close();
    stream = null;
  });
}

async function post(path, body, method) {
  const response = await fetch(path, {
    method: method || "POST",
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

/* The model-driven path. Disabled until /api/state says a configured client exists, so it can
   never look available while quietly doing nothing. */
async function ask() {
  const question = els.prompt.value.trim();
  if (!question) return;
  els.prompt.value = "";
  els.ask.disabled = true;
  els.phase.textContent = "提交中";
  const body = { text: question };
  if (selected) body.file = selected;
  const { status, data } = await post("/api/ask", body);
  if (status === 409) { els.ask.disabled = false; return line(els.log, "bad", "!! 已有一次运行在进行中"); }
  if (status >= 400) {
    els.ask.disabled = false;
    return line(els.log, "bad", `!! ${data.error || status}`);
  }
  line(els.log, "dim", `\n— 提问：${question}`);
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
$("asker").addEventListener("submit", (event) => { event.preventDefault(); ask(); });

$("drawer-toggle").addEventListener("click", () => {
  logVisible = !logVisible;
  $("log").hidden = !logVisible;
  $("drawer-toggle").setAttribute("aria-expanded", String(logVisible));
  applyChrome();
});

/* Language flips the chrome only and re-renders the panels from the same payloads. Model prose is
   never re-translated: it stays exactly as the model wrote it, in whatever language that was. */
$("lang").addEventListener("click", async () => {
  const next = LANG === "zh" ? "en" : "zh";
  const { data } = await post("/api/settings", { language: next }, "PATCH");
  if (data && data.ok === false) {
    line(els.log, "bad", `!! ${data.error}`);
    return;
  }
  await refreshState();
  // No re-render needed: every chrome label carries its untranslated key, so applyChrome()
  // inside refreshState() relabels tiles that were rendered before the flip as well.
});

addEventListener("keydown", (event) => {
  if (event.ctrlKey && event.key.toLowerCase() === "l") {
    event.preventDefault();
    $("drawer-toggle").click();
  }
});

/* Settings. The key field starts empty and stays empty after saving: the server never returns
   a credential, so there is nothing to prefill, and an input that cannot show what is stored is
   better than one that pretends to. */
const veil = $("veil");
$("open-settings").addEventListener("click", () => {
  $("set-msg").textContent = "";
  $("set-key").value = "";
  veil.classList.add("open");
});
$("cancel").addEventListener("click", () => veil.classList.remove("open"));
addEventListener("keydown", (event) => { if (event.key === "Escape") veil.classList.remove("open"); });

$("settings").addEventListener("submit", async (event) => {
  event.preventDefault();
  const patch = {
    base_url: $("set-url").value.trim(),
    model: $("set-model").value.trim(),
    remember_key: $("set-remember").checked,
  };
  const key = $("set-key").value.trim();
  if (key) patch.api_key = key;
  const { status, data } = await post("/api/settings", patch, "PATCH");
  if (status >= 400 || data.ok === false) {
    $("set-msg").textContent = "未保存：" + (data.error || data.message || status);
    return;
  }
  $("set-msg").textContent = data.warning
    ? "已保存。" + data.warning
    : (data.config_ready ? "已保存，提问框已可用。" : "已保存，但还不足以启用提问框。");
  $("set-key").value = "";
  await refreshState();
});

async function refreshState() {
  const state = await fetch("/api/state").then((r) => r.json()).catch(() => null);
  if (state) {
    renderState(state);
    $("set-url").value = state.base_url || "";
    $("set-model").value = state.model || "";
  }
  return state;
}
els.release.addEventListener("click", async () => {
  const { data } = await post("/api/session/release");
  session = null;
  if (data.error) {
    line(els.log, "bad", `!! 释放失败：${data.error}`);
  } else {
    const freed = data.bytes_freed_mb ? `，约 ${data.bytes_freed_mb} MB` : "";
    line(els.log, "ok", `释放：关闭 ${data.closed} 个会话，丢弃 ${data.warm_frames_dropped} 个保留帧${freed}；` +
         `之后 ${data.sessions_after} 会话 / ${data.warm_frames_after} 帧`);
  }
  if (data.state) renderSession({ ...data.state, reused: false });
});

loadFiles();
fetch("/api/state").then((r) => r.json()).then(renderState).catch(() => {});
