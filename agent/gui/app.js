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
  pulse: $("pulse"), tabs: $("tabs"), chart: $("chart"), chartCap: $("chart-cap"),
  placeholder: $("result-placeholder"), logout: $("logout"),
};

let selected = null;      // absolute path of the chosen dataset
let session = null;       // last session_id we opened, so "analyze" can reuse it
let stream = null;        // active EventSource
let canAsk = false;       // whether /api/state says a configured client exists
let agentReady = false;   // whether the model client import itself worked
let notReadyHint = "";    // why sending is gated, in the words renderState already chose
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
// The title is the one piece of chrome that lives outside the DOM tree, so the sweep below can
// never reach it. Capture the authored value once and re-localise from that, not from whatever
// the title currently says, or a zh↔en round trip would compound.
const ORIGINAL_TITLE = document.title;

/* Labels only. Model prose and engine values never pass through this table -- translating a
   model's answer in the frontend would be fabricating a translation it did not produce. */
function t(key) {
  return (LANG === "en" && I18N[key]) ? I18N[key] : key;
}

function applyChrome() {
  // `#phase` and `<option>` joined the sweep: the footer state and the tool picker were the last
  // two places where an English setting still produced Chinese text (and vice versa). Both are
  // leaf nodes, and `<option>` carries its behaviour in `value`, never in its label.
  document.querySelectorAll(".lt, h1, h2, button, .row .k, .kpi .l, .hint, #phase, option").forEach((node) => {
    // Guard, not paranoia: assigning textContent destroys child elements. A label that wraps a
    // select used to be rewritten here and the control vanished with it, which silently broke
    // the tool form. Only leaf nodes are ever translated.
    if (node.children.length) return;
    if (node.dataset.label === undefined) node.dataset.label = node.textContent.trim();
    const translated = t(node.dataset.label);
    if (translated !== node.textContent) node.textContent = translated;
  });
  document.documentElement.lang = LANG === "en" ? "en" : "zh";
  document.title = t(ORIGINAL_TITLE);
  $("lang").textContent = LANG === "zh" ? "中文" : "English";
  $("prompt").placeholder = LANG === "en"
    ? "e.g. Compare total and average revenue by region" : "例如：按地区统计 revenue 的总和与均值";
  $("goal").placeholder = t("例如：找出 revenue 离群点的成因");
  $("drawer-toggle").textContent = logVisible ? t("收起") : t("展开");
  // Attributes are outside the text sweep, so the tablist's accessible name is localised here --
  // leaving it in the HTML would strand a Chinese label under language=en.
  els.tabs.setAttribute("aria-label", t("图表类型"));
  // The caption is a label plus a count, so the sweep above cannot rebuild it from textContent.
  if (chartCap.label) els.chartCap.textContent = `${t(chartCap.label)} (${chartCap.count})`;
  // Plan rows are built once per reply, so the chrome sweep above never reaches them; redraw
  // them from the cached list rather than replaying the reply, which would double the off-plan
  // rows.
  renderPlanRows();
}

/* The engine sends one of a closed set of state tokens. They go through the same table as every
   other label, and `dataset.label` is written together with the text so the chrome sweep and this
   assignment can never disagree about what the footer currently means. The two raw tokens used to
   surface as untranslated English in a Chinese interface. */
const PHASE_TOKENS = { tool: "执行工具", thinking: "正在思考" };

function setPhase(token) {
  const label = PHASE_TOKENS[token] || token;
  els.phase.dataset.label = label;
  els.phase.textContent = t(label);
}

/* The one live-work indicator. It follows the same edges as the submit buttons -- set when a job is
   accepted, cleared when the stream says it is over (including a dropped tunnel) -- so it can never
   pulse on a run that already ended. */
function setBusy(on) {
  els.pulse.dataset.busy = on ? "1" : "0";
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

/* Which piece of the credential is missing is a fact the server knows. `APIConfig.ready` is a
   three-way conjunction (api_config.py:23), so `config_ready: false` alone cannot tell the operator
   whether to type a key or pick a model -- and the browser covered all three cases with one fixed
   sentence, in two nearly identical copies. Field names only, never values. */
const FIELD_LABELS = { base_url: "API 地址", model: "模型", api_key: "API 密钥" };

function missingFieldNames(state) {
  const fields = Array.isArray(state.missing_fields) ? state.missing_fields : [];
  return fields.map((field) => t(FIELD_LABELS[field] || field)).join(" / ");
}

function notReadyText(state) {
  const names = missingFieldNames(state);
  if (!names) {
    // No names to work with: the file could not be read, which is a different claim from nothing
    // being configured, so the general sentence stands and `model_error` carries the real reason.
    return t("模型客户端已安装，但没有配置 API 地址 / 密钥 / 模型。");
  }
  return t("模型客户端已安装，但还缺少：{fields}").replace("{fields}", names);
}

function renderState(state) {
  I18N = state.i18n || {};
  LANG = state.language === "en" ? "en" : "zh";
  applyChrome();
  els.model.textContent = state.model || t("未配置模型");
  els.host.textContent = location.host;
  els.logout.hidden = !state.auth_required;
  if (!selected) els.file.textContent = t("未选择数据文件");

  const notes = [];
  if (!state.agent_available) {
    notes.push(t("模型客户端不可用：") + (state.agent_import_error || t("未知原因")) +
               "\n" + t("安装：pip install -r requirements.txt\n" +
                 "在此之前，下方“直接工具调用”仍然真实可用，提问区不可用。"));
  } else if (!state.config_ready) {
    notes.push(notReadyText(state) + (state.model_error ? "\n" + state.model_error : ""));
  }
  if (!state.engine_ready) {
    notes.unshift(t("分析引擎未响应：") + state.engine_error);
  }

  // The question box is the model-driven path, so its submit opens exactly when a configured
  // client exists. The input itself never locks: a question can be typed ahead of the config,
  // and sending before that walks straight to the form that fixes it. A grey input that would
  // not take focus used to read as "broken"; that was the button's job dressed up as the
  // field's.
  const canAskNow = Boolean(state.agent_available && state.config_ready);
  canAsk = canAskNow;
  agentReady = Boolean(state.agent_available);
  notReadyHint = agentReady
    ? notReadyText(state)
    : t("提问需要模型客户端（pip install -r requirements.txt）。下面「直接工具调用」不需要它。");
  els.ask.dataset.pending = canAsk ? "" : "1";
  els.askHint.textContent = canAsk
    ? t("提问会把选中的文件路径作为一行上下文附在问题后面，日志里会显示模型实际收到的原文。")
    : notReadyHint;

  if (notes.length) {
    els.banner.hidden = false;
    els.banner.dataset.kind = state.engine_ready ? "warn" : "bad";
    els.banner.textContent = notes.join("\n\n");
  } else {
    els.banner.hidden = false;
    els.banner.dataset.kind = "ok";
    els.banner.textContent = t("引擎与模型客户端均就绪。");
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
  if (els.placeholder) els.placeholder.hidden = true;
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

/* Chart tabs are derived, never declared. The five kinds below are what the report generator can
   actually emit (make_deliverables.py writes groupby_bar/groupby_line/corr_heatmap/outliers_bar/
   summary_*); a kind with no artifact on disk gets no tab, so the row can't advertise a chart that
   isn't there. Anything whose name doesn't prove a kind stays in the flat #charts list rather than
   being guessed into a tab. */
const CHART_KINDS = [
  ["line", "折线"], ["bar", "柱状"], ["heat", "热力"], ["hist", "直方"], ["scatter", "散点"],
];
// The second column is the label that goes into the i18n table; the first is only a filename token.
// Handing the token to t() would look a machine name up in a vocabulary of interface labels and,
// finding nothing, show the token itself.
const kindLabel = (kind) => (CHART_KINDS.find(([token]) => token === kind) || [])[1] || kind;

function chartKind(path) {
  const name = String(path).split(/[\\/]/).pop().toLowerCase();
  if (!/\.svg$/i.test(name)) return "";
  const hit = CHART_KINDS.find(([token]) => name.includes(token));
  return hit ? hit[0] : "";
}

function svgBox(path) {
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
  return box;
}

let chartByKind = {};   // kind -> [artifact path], rebuilt from result.charts on every render
let chartCap = { label: "", count: 0 };   // what the caption is made of, so a language flip can rebuild it

function showKind(kind) {
  const items = chartByKind[kind] || [];
  text(els.chart);
  items.forEach((path) => els.chart.appendChild(svgBox(path)));
  els.chart.hidden = false;
  // Kind plus a count, and only that: each chart below already carries its own filename caption, so
  // repeating the path here would be a second place for the two to disagree.
  chartCap = { label: kindLabel(kind), count: items.length };
  els.chartCap.textContent = `${t(chartCap.label)} (${chartCap.count})`;
  els.tabs.querySelectorAll(".tab").forEach((btn) => {
    btn.setAttribute("aria-selected", String(btn.dataset.kind === kind));
  });
}

/* Returns the artifacts that no kind claimed, so the caller can render them as before and nothing
   is dropped between the two views. */
function renderChartTabs(charts) {
  chartByKind = {};
  charts.forEach((path) => {
    const kind = chartKind(path);
    if (kind) (chartByKind[kind] = chartByKind[kind] || []).push(path);
  });
  const order = CHART_KINDS.map(([kind]) => kind).filter((kind) => chartByKind[kind]);
  text(els.tabs);
  text(els.chart);
  els.chartCap.textContent = "";
  if (!order.length) {
    els.tabs.hidden = true;
    els.chart.hidden = true;
    chartCap = { label: "", count: 0 };
    els.chartCap.textContent = "";
    return charts;
  }
  order.forEach((kind) => {
    const btn = document.createElement("button");
    btn.className = "tab";
    btn.type = "button";
    btn.setAttribute("role", "tab");
    btn.setAttribute("aria-controls", "chart");
    // dataset.label holds the untranslated key so a language flip relabels a tab built under the
    // other language; writing only textContent would strand it in whichever language it was born in.
    btn.dataset.label = kindLabel(kind);
    btn.dataset.kind = kind;
    btn.textContent = t(kindLabel(kind));
    btn.addEventListener("click", () => showKind(kind));
    els.tabs.appendChild(btn);
  });
  els.tabs.hidden = false;
  showKind(order[0]);
  return charts.filter((path) => !chartKind(path));
}

function renderArtifacts(result) {
  const charts = Array.isArray(result.charts) ? result.charts : [];
  const loose = renderChartTabs(charts);
  // Cleared before the early return, not after: a second result whose charts all found a tab used
  // to leave the first result's artifacts parked down here, so the same SVG showed twice.
  text(els.charts);
  if (!loose.length && !result.report) return;
  loose.forEach((path) => els.charts.appendChild(svgBox(path)));
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
  // Handlers must act on the stream they were registered for, not on the global. A late event on
  // an already-finished stream would otherwise close the *next* run's connection, or -- once the
  // global had been nulled -- throw inside the handler and leave the buttons locked.
  const me = stream;
  stream.addEventListener("prompt", (e) => {
    // Echo exactly what the model received, including the appended file line, so the context
    // added on the operator's behalf is visible rather than implied.
    const body = JSON.parse(e.data);
    line(els.log, "dim", `   发给模型的原文：${body.text}`);
  });
  stream.addEventListener("phase", (e) => { setPhase(JSON.parse(e.data).phase); });
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
    if (els.placeholder) els.placeholder.hidden = true;
    els.answer.hidden = false;
    renderMarkdown(els.answer, body.text);
  });
  stream.addEventListener("session", (e) => renderSession(JSON.parse(e.data)));
  stream.addEventListener("error", (e) => {
    // A transport failure carries no `data`, so the parse below only ever yields the server's own
    // error event; collapsing the two into one line hid which half broke. Either way the run is
    // over as far as this viewer can tell, and `done` may never arrive -- without the unlock and
    // the close here, one dropped tunnel locked both submit buttons until a page reload, and the
    // browser kept re-attaching to a job that had finished.
    let reason = null;
    try { reason = JSON.parse(e.data).message; } catch (ignored) {}
    line(els.log, "bad", reason ? `!! ${reason}` : "!! 连接中断（服务端未给出原因）");
    setPhase("连接中断");
    me.close();
    if (stream === me) stream = null;
    setBusy(false);
    els.run.disabled = false;
    els.ask.disabled = false;   // the pending look carries the config state; busy no longer does
  });
  stream.addEventListener("done", () => {
    setPhase("完成");
    setBusy(false);
    els.run.disabled = false;
    els.ask.disabled = false;
    me.close();
    if (stream === me) stream = null;
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
  setPhase("提交中");
  const { status, data } = await post("/api/run", { tool: els.tool.value, args: buildArgs() });
  if (status === 409) { els.run.disabled = false; return line(els.log, "bad", "!! 已有一次运行在进行中"); }
  if (status >= 400) { els.run.disabled = false; return line(els.log, "bad", `!! ${data.error || status}`); }
  line(els.log, "dim", `\n— ${data.note || "direct tool call"}: ${els.tool.value}`);
  setBusy(true);
  attach(data.job_id);
}

/* The model-driven path. The submit can arrive before a client is configured -- the input is
   always typable -- and that click is spent guiding the operator to the form that fixes it,
   with the question left in the box. */
async function ask() {
  if (!canAsk) {
    if (agentReady) {
      $("open-settings").click();
      $("set-url").focus();
    }
    els.askHint.textContent = notReadyHint;
    return;
  }
  const question = els.prompt.value.trim();
  if (!question) return;
  els.prompt.value = "";
  els.ask.disabled = true;
  setPhase("提交中");
  const body = { text: question };
  if (selected) body.file = selected;
  const { status, data } = await post("/api/ask", body);
  if (status === 409) { els.ask.disabled = false; return line(els.log, "bad", "!! 已有一次运行在进行中"); }
  if (status >= 400) {
    els.ask.disabled = false;
    return line(els.log, "bad", `!! ${data.error || status}`);
  }
  line(els.log, "dim", `\n— 提问：${question}`);
  setBusy(true);
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
  // The listing is a bounded walk with a capped payload, so "these are the files on the machine"
  // has to say how much of it it is. Before this the panel showed 40 buttons while the server
  // reported 196 and the difference was invisible -- the one thing a bounded view must never imply.
  const total = Number(payload.count || 0);
  const shown = (payload.files || []).length;
  if (shown) {
    line(els.files, "dim", (total > shown
      ? t("共 {total} 个可分析文件，这里按大小列出前 {shown} 个")
          .replace("{total}", total).replace("{shown}", shown)
      : t("共 {total} 个可分析文件，按大小排序").replace("{total}", total))
      + (payload.note ? " · " + payload.note : ""));
  }
  if (!(payload.files || []).length) {
    line(els.files, "dim", payload.note || t("没有可分析的数据文件"));
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

/* The way out of a token-gated workbench. Visible only when /api/state says the gate is on,
   so a loopback server gains no button that would lead nowhere. The server forgets the
   session; the browser lands back on the sign-in page. */
$("logout").addEventListener("click", async () => {
  els.logout.disabled = true;
  try { await fetch("/api/logout", { method: "POST" }); }
  catch (err) { /* the cookie expiry is already set; the door is behind us either way */ }
  window.location.replace("/");
});

/* 数据初始化: back to out-of-box. One click arms it for four seconds and changes the
   button into its armed state -- a second click inside that window is intent, anything
   else is not. On success the gate password and this session are already gone, so the
   redirect lands on first-run setup rather than on a stale screen. */
(() => {
  const button = $("reset-all");
  let timer = null;
  button.addEventListener("click", async () => {
    if (timer === null) {
      button.classList.add("armed");
      button.textContent = t("再点一次确认擦除");
      timer = setTimeout(() => {
        timer = null;
        button.classList.remove("armed");
        button.textContent = t("恢复初始状态");
      }, 4000);
      return;
    }
    clearTimeout(timer);
    timer = null;
    button.disabled = true;
    const { status, data } = await post("/api/reset", {});
    if (status === 200 && data && data.ok) {
      window.location.replace("/");
      return;
    }
    button.disabled = false;
    button.classList.remove("armed");
    button.textContent = t("恢复初始状态");
    line(els.log, "bad", `!! ${(data && data.error) || `HTTP ${status}`}`);
  });
})();

/* Keyboard: the terminal's bindings, not the browser's. Ctrl+Q and Ctrl+, never reach the page --
   Chrome and Firefox handle them first -- so advertising a chord that cannot fire would be a
   shortcut that silently does nothing. F5 is the browser's own reload, hence the preventDefault.
   Each binding dispatches the button's click rather than duplicating its body: one code path per
   action, so the key and the button can never disagree about what they do. */
const KEY_BINDINGS = {
  F2: () => $("drawer-toggle").click(),
  F3: () => { const first = els.files.querySelector("button"); if (first) first.focus(); },
  F4: () => els.prompt.focus(),
  F5: () => $("open-settings").click(),
  F6: () => $("lang").click(),
  F8: () => els.release.click(),
};

addEventListener("keydown", (event) => {
  if (event.ctrlKey || event.metaKey || event.altKey) return;
  const action = KEY_BINDINGS[event.key];
  if (!action) return;
  event.preventDefault();
  action();
});

/* Settings. The key field starts empty and stays empty after saving: the server never returns
   a credential, so there is nothing to prefill, and an input that cannot show what is stored is
   better than one that pretends to. */
const veil = $("veil");
$("open-settings").addEventListener("click", () => {
  $("set-msg").textContent = "";
  $("set-key").value = "";
  // Re-read from the server on every open, so the form cannot show a stale address or a stale
  // `记住密钥 / Remember key` box: the checkbox's value is submitted back verbatim, and a stale
  // unchecked box is what used to delete an already-stored key during an unrelated edit.
  refreshState();
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
    // The three endings of this one submit are now translated as a set; leaving this line as raw
    // Chinese while its neighbours go through the table would be the same residue, one line apart.
    $("set-msg").textContent = t("未保存：") + (data.error || data.message || status);
    return;
  }
  const names = missingFieldNames(data);
  $("set-msg").textContent = data.warning
    ? t("已保存。") + data.warning
    : (data.config_ready ? t("已保存，提问框已可用。")
       : t("已保存，但提问框还不可用，缺少：{fields}").replace("{fields}", names || t("未知项")));
  $("set-key").value = "";
  await refreshState();
});

async function refreshState() {
  const state = await fetch("/api/state").then((r) => r.json()).catch(() => null);
  if (state) {
    renderState(state);
    $("set-url").value = state.base_url || "";
    $("set-model").value = state.model || "";
    // The checkbox used to open unchecked no matter what was on disk, while the submit below
    // always sends `remember_key` -- so editing only the model silently deleted a stored key.
    // Prefill it, and let an explicit uncheck be the only thing that can remove one.
    $("set-remember").checked = Boolean(state.remember_key);
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
