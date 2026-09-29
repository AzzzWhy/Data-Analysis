/* Browser side of the workbench.
 *
 * Two rules, both there because the previous attempt at this screen broke them:
 *   - Nothing is computed here. Every engine label comes from the server's chip_state() over the
 *     real execution_decision, every memory figure comes from the worker's own list reply, and
 *     every number comes from a tool result. If a field is absent, the row is left empty rather
 *     than filled with something plausible.
 *   - Worker cache state is not polled. Bounded job-status checks recover a dropped event stream;
 *     they query the in-memory job registry, never the worker's cache-pruning list command.
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
let backendInstance = null;
let activeJobId = null;
let currentJob = null;
let jobSummaries = [];
let jobsKnown = false;
let backendBusy = false;
let submissionPending = false;
let jobNotice = "checking";
let jobPhase = "checking";
let reconnectTimer = null;
let recoveryChecks = 0;
let catalogRequest = 0;
let backendContextEpoch = 0;
const JOB_BOOKMARK_KEY = "gpu-workbench:last-job:v1";
const JOB_TERMINAL = new Set(["succeeded", "failed", "partial"]);

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
// The frontend may connect to an older remote backend. Keep only the new labels here as
// fallbacks so these safety explanations stay bilingual without deploying remote code.
const LOCAL_I18N = {
  "模型连接设置": "Model connection settings",
  "模型服务地址": "Model service URL",
  "模型服务访问密钥": "Model service access key",
  "密钥不会回显到浏览器。更换模型服务地址会清除上一服务商的密钥与模型。": "Keys are never returned to the browser. Changing the model service URL clears the previous provider key and model.",
  "本地部署：地址由当前计算后端访问。选择远端后，127.0.0.1 指远端机器，不是这台电脑。SSH 连接不是模型接口。": "Local deployment: the selected compute backend accesses this URL. When remote is selected, 127.0.0.1 means the remote machine, not this PC. An SSH connection is not a model API.",
  "使用 OpenAI 兼容的 /v1 地址，例如 Ollama：http://127.0.0.1:11434/v1；vLLM：http://127.0.0.1:8000/v1。按实际部署修改主机与端口。": "Use an OpenAI-compatible /v1 URL, e.g. Ollama: http://127.0.0.1:11434/v1; vLLM: http://127.0.0.1:8000/v1. Adjust the host and port to match your deployment.",
  "无鉴权的本地模型服务可填 local 作为密钥占位，无需云服务密钥；启用了鉴权则填写该模型服务的访问密钥。": "For a local model service without authentication, enter local as a placeholder; no cloud key is needed. If authentication is enabled, use that model service access key.",
  "重置后将进入设置新访问密码页。退出登录不会清除配置，不需要重置。": "Reset opens the page for setting a new access password. Signing out keeps your configuration and does not require a reset.",
  "重置只作用于当前本机后端；远端模式下须先在“计算连接”中选择“使用本机”。": "Reset applies only to this local backend. In remote mode, select Use local in Compute connection first.",
  "当前使用远端后端，禁止在此重置。请先在“计算连接”中选择“使用本机”。": "Reset is blocked while using a remote backend. Select Use local in Compute connection first.",
  "请再次确认重置。重置后将进入设置新访问密码页；只想退出登录请使用“退出”。": "Confirm reset again. You will be asked to set a new access password. To sign out only, use Sign out.",
  "正在核对计算位置与重置权限…": "Checking compute location and reset authorization…",
  "无法核对重置权限，未发送重置请求。请检查连接或重新登录后再试。": "Cannot verify reset authorization; no reset request was sent. Check the connection or sign in again before retrying.",
  "正在重置，请勿重复提交…": "Resetting. Do not submit again…",
  "重置未确认成功（HTTP {status}），未自动重试。请刷新检查；若仍需重置，请重新确认。": "Reset was not confirmed successful (HTTP {status}); it was not retried. Refresh to check, then confirm again only if a reset is still needed.",
  "无法确认重置结果，未自动重试。请先刷新检查，不要立即重复重置。": "The reset outcome is unknown and was not retried. Refresh to check before attempting another reset.",
};
let LANG = "zh";
// The title is the one piece of chrome that lives outside the DOM tree, so the sweep below can
// never reach it. Capture the authored value once and re-localise from that, not from whatever
// the title currently says, or a zh↔en round trip would compound.
const ORIGINAL_TITLE = document.title;

/* Labels only. Model prose and engine values never pass through this table -- translating a
   model's answer in the frontend would be fabricating a translation it did not produce. */
function t(key) {
  return LANG === "en" ? (I18N[key] || LOCAL_I18N[key] || key) : key;
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
    // This hint is a dynamic server response, not a label. Its initial empty text must never
    // become a cached label that erases settings success/errors on the following state refresh.
    if (node === $("set-msg") || node.dataset.dynamic !== undefined) return;
    if (node.closest && node.closest("#set-model")) return;
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
  $("set-key").placeholder = t("留空沿用当前密钥；不记住时仅本次服务有效");
  $("drawer-toggle").textContent = logVisible ? t("收起") : t("展开");
  const modelPlaceholder = $("set-model").querySelector("option[value='']");
  if (modelPlaceholder) modelPlaceholder.textContent = t("请选择模型");
  // Attributes are outside the text sweep, so the tablist's accessible name is localised here --
  // leaving it in the HTML would strand a Chinese label under language=en.
  els.tabs.setAttribute("aria-label", t("图表类型"));
  // The caption is a label plus a count, so the sweep above cannot rebuild it from textContent.
  if (chartCap.label) els.chartCap.textContent = `${t(chartCap.label)} (${chartCap.count})`;
  // Plan rows are built once per reply, so the chrome sweep above never reaches them; redraw
  // them from the cached list rather than replaying the reply, which would double the off-plan
  // rows.
  renderPlanRows();
  renderJobChrome();
  renderResetControls();
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
const FIELD_LABELS = { base_url: "模型服务地址", model: "模型", api_key: "模型服务访问密钥" };

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

/* True when a style on the given root makes it a containing block for fixed-position
   descendants, which unpins the composer. Nothing on this page sets such a style; what
   does is a browser extension (Dark Reader and friends), and the page cannot undo it --
   only name it. */
function rootBlocksFixed(node) {
  const style = getComputedStyle(node || document.documentElement);
  const backdrop = style.backdropFilter;
  return style.transform !== "none" || style.filter !== "none" ||
         style.perspective !== "none" || String(style.willChange || "").includes("transform") ||
         (backdrop !== undefined && backdrop !== "none" && backdrop !== "");
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
  // A transform, filter or backdrop-filter on the root -- Dark Reader and friends inject
  // exactly that -- silently turns every position:fixed element into a child of the page:
  // the composer stops being pinned, and no CSS inside the page can undo it. The page can,
  // however, say so, in the banner, at the moment it starts.
  if (rootBlocksFixed() || rootBlocksFixed(document.body)) {
    notes.unshift(t("浏览器扩展改写了页面根样式（transform/filter），底部输入栏将不固定：请用无痕窗口（Ctrl+Shift+N）打开本页验证。"));
  }
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
  const hasActive = Boolean(active) || doc.sessions > 0;
  const warm = !hasActive && doc.sessions === 0 && doc.warm_frames > 0;
  els.card.dataset.state = wedged ? "unknown" : (hasActive ? "active" : (warm ? "warm" : "released"));
  // Status text goes through the chrome table like every other label; the worker's own words
  // are quoted verbatim elsewhere and are never translated.
  els.state.textContent = wedged ? t("worker 未响应")
    : (hasActive ? t("active（会话打开中）")
      : (warm ? (LANG === "en" ? "warm (data cached)" : "warm（数据已缓存）") : t("cold / released")));
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
    : (warm ? (LANG === "en"
      ? "The session is closed, but its data remains in the warm cache. Reopen it to reuse the cached data."
      : "会话已关闭，数据仍在热缓存中；继续分析可重新打开并复用。")
      : (doc.reused ? "上一次结果来自复用，未重新读盘。" : "保留帧只报数量与总字节：worker 的 list 不报每个帧是哪个文件。"));
}

/* ------------------------------------------------------------------- results */

const isRecord = (value) => value !== null && typeof value === "object" && !Array.isArray(value);

/* Direct analysis wraps operations in `result`; resident steps return them at the top level,
   and resident auto wraps them in `auto`. Read only known operation blocks, never recursively
   dump the reply: transport metadata, paths and credentials are not analysis table cells. */
function analysisBlocks(result) {
  if (!isRecord(result)) return {};
  const inner = isRecord(result.result) ? result.result : {};
  const sources = [result, result.auto, inner, inner.auto].filter(isRecord);
  const blocks = {};
  for (const source of sources) {
    for (const key of ["profile", "summary", "groupby", "outliers", "corr"]) {
      if (isRecord(source[key])) blocks[key] = source[key];
    }
  }
  return blocks;
}

function resultFailed(result) {
  if (!isRecord(result)) return true;
  const inner = isRecord(result.result) ? result.result : {};
  return [result, inner].some((item) => item.success === false || item.ok === false || Boolean(item.error));
}

/* KPI tiles are built from fields the engine actually emitted. A field that is not there produces
   no tile -- the strip gets shorter, it never fills a gap with a plausible zero. */
function renderKpis(result) {
  const decision = result.execution_decision || {};
  const observed = decision.observed || {};
  const inner = analysisBlocks(result);
  const tiles = [];
  const add = (label, value, unit) => {
    if (value === null || value === undefined || value === "") return;
    tiles.push([label, value, unit || ""]);
  };

  add("扫描行数", typeof result.rows_scanned === "number"
    ? result.rows_scanned.toLocaleString() : result.rows_scanned);
  add("引擎", result.engine || observed.actual_backend);
  const elapsed = result.seconds ?? result.step_seconds;
  if (typeof elapsed === "number") add("本次耗时", elapsed.toFixed(3), "s");
  if (typeof observed.compute_seconds === "number"
      && observed.compute_seconds !== result.seconds) {
    add("纯计算", observed.compute_seconds.toFixed(3), "s");
  }
  if (inner.groupby && inner.groupby.groups !== undefined) {
    add("分组数", inner.groupby.groups);
  }
  if (inner.outliers && isRecord(inner.outliers.results)) {
    const entries = Object.values(inner.outliers.results);
    const counts = entries.filter(isRecord).map((entry) => entry.count ?? entry.outlier_count)
      .filter((count) => typeof count === "number" && Number.isFinite(count) && count >= 0);
    // Counts are per-column observations, not distinct rows: a row may appear in two columns.
    if (counts.length && counts.length === entries.length) {
      add("离群值（各列合计）", counts.reduce((sum, count) => sum + count, 0).toLocaleString());
    }
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

function renderToolResult(payload, { historical = false } = {}) {
  const result = isRecord(payload.result) ? payload.result : null;
  let chip = payload.chip;
  els.elapsed.textContent = (typeof payload.seconds === "number" ? payload.seconds.toFixed(3) : "?") + "s";
  // Direct runs carry their own args here; model-driven runs already printed the call in the
  // trace line, so re-printing it with an empty argument object would be a duplicate that
  // looks like the model called the tool with nothing.
  if (payload.args && Object.keys(payload.args).length) {
    line(els.log, "tool", `-> ${payload.name}(${JSON.stringify(payload.args)})`);
  }
  if (resultFailed(result)) {
    // "no result yet" would claim the run is still pending when it actually failed. The engine
    // made no statement about hardware here, so the chip says the one true thing instead.
    els.chip.className = "chip bad";
    els.chip.textContent = "运行失败";
    const failure = result || {};
    const inner = isRecord(failure.result) ? failure.result : {};
    els.chip.title = String(failure.error || inner.error || "");
    line(els.log, "bad", `   FAILED ${failure.error || inner.error || "invalid tool result"}`);
    if (failure.hint) line(els.log, "dim", `   hint: ${failure.hint}`);
    // Old success tables must never look like the outcome of this failed request.
    text(els.kpis);
    text(els.tables);
    text(els.answer);
    els.answer.hidden = true;
    renderArtifacts({});
    if (els.placeholder) els.placeholder.hidden = true;
    line(els.tables, "bad", LANG === "en" ? "The analysis failed; no current result is available."
      : "本次分析失败，没有可展示的新结果。");
    return;
  }
  if (els.placeholder) els.placeholder.hidden = true;
  if (result.closed !== undefined) {
    chip = { class: "muted", label: LANG === "en" ? "Session closed" : "会话已关闭",
      note: LANG === "en"
        ? "This describes the latest tool operation, not a missing analysis result."
        : "这里显示的是最近一次工具操作，并不表示分析结果缺失。" };
  }
  setChip(chip);
  const engine = result.engine || (result.execution_decision || {}).actual_backend || "—";
  line(els.log, "ok", `   OK engine=${engine} rows=${result.rows_scanned ?? "?"} ` +
       `${result.seconds ?? result.step_seconds ?? payload.seconds ?? "?"}s`);
  if (chip && chip.note) line(els.log, "reason", `   ${chip.label}: ${chip.note}`);
  renderKpis(result);
  if (result.plan && !historical) renderPlan(result.plan);
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
  const charts = Array.isArray(result.charts) ? result.charts.filter((path) => typeof path === "string") : [];
  const loose = renderChartTabs(charts);
  // Cleared before the early return, not after: a second result whose charts all found a tab used
  // to leave the first result's artifacts parked down here, so the same SVG showed twice.
  text(els.charts);
  if (!loose.length && !result.report) return;
  loose.forEach((path) => els.charts.appendChild(svgBox(path)));
  if (typeof result.report === "string" && result.report) {
    const a = document.createElement("a");
    a.textContent = "打开报告 report.md";
    a.href = "/artifact?path=" + encodeURIComponent(result.report);
    a.target = "_blank";
    a.rel = "noopener";
    els.charts.appendChild(a);
  }
}

function renderTables(result) {
  const inner = analysisBlocks(result);
  const blocks = [];
  const fields = (value, keys) => Object.fromEntries(keys.map((key) => [key, value[key]]));
  const entries = (value) => isRecord(value) ? Object.entries(value).filter(([, row]) => isRecord(row)) : [];
  if (inner.profile) {
    const profile = inner.profile;
    const names = Array.isArray(profile.columns) ? profile.columns
      : Object.keys(isRecord(profile.dtypes) ? profile.dtypes : {});
    blocks.push(["profile", names.map((column) => ({ column,
      dtype: (profile.dtypes || {})[column], nulls: (profile.null_counts || {})[column],
    }))]);
    if (Array.isArray(profile.preview)) blocks.push(["preview", profile.preview]);
  }
  if (inner.groupby && Array.isArray(inner.groupby.top_k)) {
    blocks.push(["groupby by " + inner.groupby.by, inner.groupby.top_k]);
  }
  if (inner.summary) {
    const rows = entries(inner.summary.stats).map(([column, s]) => ({
      column, ...fields(s, ["count", "mean", "std", "min", "q1", "median", "q3", "max", "nulls"]),
    }));
    blocks.push(["summary", rows]);
  }
  if (inner.outliers) {
    blocks.push(["outliers", entries(inner.outliers.results).map(([column, s]) => ({
      column, count: s.count ?? s.outlier_count,
      ...fields(s, ["valid_count", "pct", "q1", "q3", "iqr", "lower_bound", "upper_bound", "fence_ties_excluded"]),
    }))]);
  }
  if (inner.corr) {
    if (Array.isArray(inner.corr.pairs)) blocks.push(["correlation pairs", inner.corr.pairs
      .filter(isRecord).map((row) => fields(row, ["a", "b", "corr"]))]);
    if (isRecord(inner.corr.matrix)) {
      const matrixRows = entries(inner.corr.matrix);
      const names = new Set(matrixRows.flatMap(([, values]) => Object.keys(values)));
      let rowLabel = "column";
      while (names.has(rowLabel)) rowLabel = "(" + rowLabel + ")";
      blocks.push(["correlation matrix",
        matrixRows.map(([column, values]) => ({ [rowLabel]: column, ...values }))]);
    }
  }
  // Clear even when this response has no tables; otherwise a report/close/failed result can
  // inherit numerical tables belonging to a different query or dataset.
  text(els.tables);
  blocks.forEach(([title, values]) => {
    const rows = values.filter(isRecord);
    const h = document.createElement("div");
    h.className = "cap";
    h.textContent = title;
    els.tables.appendChild(h);
    if (!rows.length) {
      line(els.tables, "dim", LANG === "en" ? "No rows were returned for this operation."
        : "本次操作没有返回数据行。");
      return;
    }
    // The union keeps later optional fields aligned with their header, even when an earlier
    // row omits them. Bound rendering, not computation; tell users if the view was truncated.
    const keys = [...new Set(rows.flatMap((row) => Object.keys(row)))].slice(0, 40);
    const visible = rows.slice(0, 100);
    if (rows.length > visible.length || rows.some((row) => Object.keys(row).some((key) => !keys.includes(key)))) {
      line(els.tables, "dim", LANG === "en"
        ? `Display limited to ${visible.length} of ${rows.length} returned rows and ${keys.length} columns.`
        : `仅展示返回结果 ${rows.length} 行中的 ${visible.length} 行，最多 ${keys.length} 列。`);
    }
    const table = document.createElement("table");
    const head = table.createTHead().insertRow();
    keys.forEach((key) => {
      const th = document.createElement("th");
      th.scope = "col";
      th.textContent = key;
      head.appendChild(th);
    });
    visible.forEach((row) => {
      const tr = table.insertRow();
      keys.forEach((key) => {
        const value = row[key];
        // Never stringify arbitrary nested objects; they can contain transport-only details.
        if (value !== null && typeof value === "object") return cell(tr, undefined, false);
        cell(tr, typeof value === "number" ? value.toLocaleString(undefined,
              { maximumSignificantDigits: 12 }) : value, typeof value === "number");
      });
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
  if (resultFailed(result)) return;
  if (result.session_id) {
    session = result.session_id;
    // A fresh session with no goal has no plan. Leaving the previous one on screen would show
    // steps this session never had.
    if (!result.plan) clearPlanCard();
  }
  if ((payload.name === "dataset_session" && payload.args && payload.args.operation === "close")
      || result.closed !== undefined) {
    session = null;
    clearPlanCard();
  }
}

/* ----------------------------------------------------- recoverable job viewer */

const jobText = (zh, en) => LANG === "en" ? en : zh;
const JOB_LABELS = {
  checking: ["正在检查任务状态…", "Checking task status…"],
  ready: ["任务状态已确认，可以开始分析。", "Task state verified. Ready to analyze."],
  running: ["任务运行中，正在接收执行记录。", "Task running; receiving events."],
  submitting: ["正在提交；请勿重复发送。", "Submitting; please do not send again."],
  submission_failed: ["本次提交未成功确认，输入已保留；没有自动重复发送。", "Submission was not confirmed. Your input is preserved; it was not resent."],
  reconnecting: ["连接中断，正在重连；后台任务可能仍在运行，不会自动重跑。", "Connection lost. Reconnecting; the task may still be running. It will not be rerun."],
  unavailable: ["暂时无法确认任务状态。请重新连接；不会自动重复执行。", "Task status is unavailable. Reconnect to verify it; no automatic rerun."],
  busy: ["后端正忙，等待现有操作完成后刷新状态。", "Backend busy. Refresh after its current operation finishes."],
  succeeded: ["任务成功完成。", "Task succeeded."],
  failed: ["任务失败，请查看执行记录。", "Task failed. See the execution log."],
  partial: ["任务部分完成或结果未完全确认，请查看执行记录。", "Task partially completed or its outcome is unconfirmed. See the execution log."],
  expired: ["任务历史已失效或被清理，不能恢复旧结果。", "This task is no longer retained. Its old results cannot be restored."],
  restarted: ["后端已重启或切换，没有恢复旧后端的任务。", "Backend restarted or changed. Tasks from the old backend were not restored."],
};

function jobLabel(status) {
  const pair = JOB_LABELS[status] || JOB_LABELS.partial;
  return jobText(pair[0], pair[1]);
}

function shortJobLabel(status) {
  const labels = { running: ["运行中", "Running"], succeeded: ["成功", "Succeeded"],
    failed: ["失败", "Failed"], partial: ["部分", "Partial"] };
  const pair = labels[status] || ["未确认", "Unknown"];
  return jobText(pair[0], pair[1]);
}

function readJobBookmark() {
  try {
    const saved = JSON.parse(sessionStorage.getItem(JOB_BOOKMARK_KEY));
    return saved && typeof saved.instance_id === "string" && typeof saved.job_id === "string"
      ? { instance_id: saved.instance_id, job_id: saved.job_id } : null;
  } catch (ignored) { return null; }
}

function rememberJob(job) {
  // No question, file path, credential or event body is persisted in the browser.
  try {
    if (job) sessionStorage.setItem(JOB_BOOKMARK_KEY, JSON.stringify({ instance_id: job.instance, job_id: job.id }));
    else sessionStorage.removeItem(JOB_BOOKMARK_KEY);
  } catch (ignored) { /* storage-disabled browsers still work for the current page */ }
}

function cancelJobReconnect() {
  if (reconnectTimer !== null) clearTimeout(reconnectTimer);
  reconnectTimer = null;
}

function closeJobStream() {
  cancelJobReconnect();
  if (stream) stream.close();
  stream = null;
}

function syncJobControls() {
  const blocked = submissionPending || !jobsKnown || backendBusy;
  els.run.disabled = blocked;
  els.ask.disabled = blocked; // When idle, an unconfigured model still opens its settings form.
  els.files.querySelectorAll("button").forEach((button) => { button.disabled = blocked; });
  const reset = $("reset-all");
  if (reset) reset.dataset.taskBusy = String(submissionPending || backendBusy);
  renderResetControls();
  setBusy(submissionPending || backendBusy);
}

function renderJobChrome() {
  const status = $("job-status");
  if (status) {
    let message = jobLabel(jobNotice);
    if (currentJob && currentJob.truncated) message += jobText(
      " 较早的执行记录已截断，当前仅显示保留下来的结果。",
      " Earlier events were truncated; only retained results are shown.");
    if (currentJob && currentJob.outcomeDetail) message += " " + currentJob.outcomeDetail;
    status.textContent = message;
  }
  const reconnect = $("job-reconnect");
  if (reconnect) {
    reconnect.textContent = jobText("重新连接任务", "Reconnect task");
    reconnect.hidden = !["unavailable", "reconnecting", "busy"].includes(jobNotice);
  }
  if (jobPhase) {
    const labels = {
      checking: ["检查中", "Checking"], ready: ["待命", "Ready"], running: ["执行中", "Running"],
      submitting: ["提交中", "Submitting"], reconnecting: ["重连中", "Reconnecting"],
      submission_failed: ["提交未确认", "Submission unconfirmed"],
      unavailable: ["状态未知", "Status unknown"], busy: ["后端忙", "Backend busy"],
      succeeded: ["成功", "Succeeded"], failed: ["失败", "Failed"], partial: ["部分完成", "Partial"],
      expired: ["历史已失效", "History expired"], restarted: ["后端已更换", "Backend changed"],
    };
    const pair = labels[jobPhase] || labels.partial;
    els.phase.textContent = jobText(pair[0], pair[1]);
    els.phase.dataset.label = els.phase.textContent;
  }
  if ($("job-history-title")) $("job-history-title").textContent = jobText("任务历史", "Task history");
  if ($("job-history-note")) $("job-history-note").textContent = jobText(
    "仅保留当前后端进程内最近 20 项任务，重启后清空。历史回放不会重新执行分析。",
    "The current backend retains at most 20 tasks in memory. Restart clears them. Viewing history never reruns analysis.");
  if ($("refresh-jobs")) $("refresh-jobs").textContent = jobText("刷新历史", "Refresh history");
  renderJobHistory();
}

function showJobNotice(notice) {
  jobNotice = notice;
  jobPhase = notice;
  renderJobChrome();
}

function renderJobHistory() {
  const list = $("job-list");
  if (!list) return;
  text(list);
  for (const job of jobSummaries) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "file";
    button.dataset.dynamic = "";
    button.setAttribute("aria-pressed", String(Boolean(currentJob && currentJob.id === job.id)));
    button.disabled = submissionPending || Boolean(activeJobId && activeJobId !== job.id);
    const name = document.createElement("span");
    name.className = "name";
    name.textContent = String(job.label || job.kind || job.id);
    const state = document.createElement("span");
    state.className = "size";
    state.textContent = shortJobLabel(job.status);
    state.title = jobLabel(job.status);
    button.appendChild(name);
    button.appendChild(state);
    const instance = backendInstance;
    button.addEventListener("click", () => {
      if (instance !== backendInstance || submissionPending || (activeJobId && activeJobId !== job.id)) return;
      attach(job.id, instance, { summary: job });
    });
    list.appendChild(button);
  }
  if (!jobSummaries.length) line(list, "dim", jobText("暂无可恢复的任务记录。", "No retained tasks to restore."));
}

function resetJobView() {
  text(els.log);
  text(els.answer);
  els.answer.hidden = true;
  text(els.kpis);
  text(els.tables);
  renderArtifacts({});
  // The plan belongs to the live compute session, not to whichever historical job is viewed.
  setChip(null);
  els.elapsed.textContent = "—";
  if (els.placeholder) els.placeholder.hidden = false;
}

async function readJobCatalog() {
  const controller = typeof AbortController === "function" ? new AbortController() : null;
  const timer = controller ? setTimeout(() => controller.abort(), 8000) : null;
  try {
    const response = await fetch("/api/jobs", { cache: "no-store", ...(controller ? { signal: controller.signal } : {}) });
    const data = await response.json();
    if (!response.ok || !isRecord(data) || typeof data.instance_id !== "string" || !Array.isArray(data.jobs)) {
      throw new Error(`Task status HTTP ${response.status}`);
    }
    return data;
  } finally { if (timer !== null) clearTimeout(timer); }
}

function adoptJobCatalog(data) {
  const changed = Boolean(backendInstance && backendInstance !== data.instance_id);
  if (changed) {
    closeJobStream();
    currentJob = null;
    session = null;
    selected = null;
    canAsk = false;
    agentReady = false;
    backendContextEpoch++;
    text(els.files);
    els.file.textContent = t("未选择数据文件");
    els.model.textContent = "—";
    clearPlanCard();
    renderSession({ sessions: null, warm_frames: null, warm_cache_mb: null,
      error: jobText("后端已更换，正在读取当前会话状态。", "Backend changed. Reading its current session state.") });
    rememberJob(null);
    resetJobView();
    loadFiles().catch(() => {});
    refreshState().catch(() => {});
  }
  backendInstance = data.instance_id;
  jobSummaries = data.jobs.filter((job) => isRecord(job) && typeof job.id === "string");
  activeJobId = typeof data.active_job_id === "string" ? data.active_job_id : null;
  backendBusy = Boolean(data.busy || activeJobId);
  jobsKnown = true;
  syncJobControls();
  renderJobHistory();
  return changed;
}

function finishJob(status, job = currentJob) {
  if (!job || currentJob !== job) return;
  ++catalogRequest; // A previously requested running snapshot cannot roll this terminal state back.
  job.status = JOB_TERMINAL.has(status) ? status : "partial";
  job.done = true;
  closeJobStream();
  if (activeJobId === job.id) activeJobId = null;
  backendBusy = Boolean(activeJobId);
  jobsKnown = true;
  jobSummaries = jobSummaries.map((item) => item.id === job.id ? { ...item, status: job.status, done: true } : item);
  showJobNotice(job.status);
  syncJobControls();
}

/* A fresh page always replays from zero: persisting the cursor but not the result DOM would
   produce a blank screen. Live reconnections reuse only this page's in-memory sequence. */
async function recoverJobs({ restoreSaved = true, manual = false } = {}) {
  const request = ++catalogRequest;
  const bookmark = restoreSaved ? readJobBookmark() : null;
  if (manual) recoveryChecks = 0;
  try {
    const data = await readJobCatalog();
    if (request !== catalogRequest) return false;
    const changed = adoptJobCatalog(data);
    let candidate = jobSummaries.find((job) => job.id === activeJobId);
    if (!candidate && currentJob && currentJob.instance === backendInstance) {
      candidate = jobSummaries.find((job) => job.id === currentJob.id);
    }
    if (!candidate && bookmark && bookmark.instance_id === backendInstance) {
      candidate = jobSummaries.find((job) => job.id === bookmark.job_id);
    }
    if (candidate) {
      const same = currentJob && currentJob.id === candidate.id && currentJob.instance === backendInstance;
      if (same && candidate.done && currentJob.lastSequence >= Number(candidate.sequence || 0)) {
        finishJob(candidate.status);
      } else if (!same || manual || !stream || stream.readyState === 2 || jobNotice !== "running") {
        attach(candidate.id, backendInstance, { summary: candidate, reset: !same });
      }
    } else {
      const stale = currentJob || bookmark;
      closeJobStream();
      currentJob = null;
      rememberJob(null);
      if (stale) resetJobView();
      showJobNotice(backendBusy ? "busy" : (changed || (bookmark && bookmark.instance_id !== backendInstance)
        ? "restarted" : (stale ? "expired" : "ready")));
      syncJobControls();
    }
    return true;
  } catch (err) {
    if (request !== catalogRequest) return false;
    jobsKnown = false;
    showJobNotice("unavailable");
    syncJobControls();
    return false;
  }
}

function scheduleJobRecovery(job) {
  if (reconnectTimer !== null || currentJob !== job) return;
  if (recoveryChecks >= 3) {
    closeJobStream();
    showJobNotice("unavailable");
    syncJobControls();
    return;
  }
  reconnectTimer = setTimeout(async () => {
    reconnectTimer = null;
    if (currentJob !== job) return;
    recoveryChecks++;
    await recoverJobs({ restoreSaved: false });
    if (currentJob === job && ["unavailable", "reconnecting"].includes(jobNotice)) scheduleJobRecovery(job);
  }, 1000 * (2 ** recoveryChecks));
}

function attach(jobId, instance = backendInstance, { summary = null, reset = true } = {}) {
  if (!jobId || !instance) { jobsKnown = false; showJobNotice("unavailable"); syncJobControls(); return; }
  const previous = currentJob;
  closeJobStream();
  ++catalogRequest; // A status request started for an older viewer must not overwrite this one.
  const same = !reset && previous && previous.id === jobId && previous.instance === instance;
  const job = same ? previous : { id: jobId, instance, lastSequence: 0, truncated: false };
  if (!same) {
    resetJobView();
    recoveryChecks = 0;
  }
  job.status = summary ? summary.status : "running";
  job.done = Boolean(summary && summary.done);
  job.historical = Boolean(summary && summary.done);
  currentJob = job;
  rememberJob(job);
  if (!job.done) { activeJobId = job.id; backendBusy = true; }
  showJobNotice(job.done ? job.status : "running");
  syncJobControls();
  const url = "/api/events?job=" + encodeURIComponent(jobId) + "&instance=" + encodeURIComponent(instance)
    + "&after=" + job.lastSequence;
  const me = new EventSource(url);
  stream = me;
  const valid = () => stream === me && currentJob === job;
  const receive = (kind, handler) => me.addEventListener(kind, (event) => {
    if (!valid()) return;
    if (kind === "error" && !(typeof event.data === "string" && event.data.trim())) return;
    const sequence = Number(event.lastEventId);
    if (Number.isInteger(sequence) && sequence > 0 && sequence <= job.lastSequence) return;
    let body;
    try {
      body = JSON.parse(event.data);
      if (!isRecord(body)) throw new Error("invalid event payload");
    }
    catch (err) {
      line(els.log, "bad", jobText("无法读取一条任务事件，结果可能不完整。", "A task event could not be read; results may be incomplete."));
      job.truncated = true;
      renderJobChrome();
      return;
    }
    if (Number.isInteger(sequence) && sequence > 0) job.lastSequence = sequence;
    recoveryChecks = 0;
    handler(body, event);
  });
  me.addEventListener("open", () => {
    if (!valid()) return;
    cancelJobReconnect();
    jobsKnown = true;
    showJobNotice(job.done ? job.status : "running");
    syncJobControls();
  });
  receive("prompt", (body) => line(els.log, "dim", `   ${jobText("发给模型的原文", "Prompt sent to model")}: ${body.text}`));
  receive("phase", (body) => { if (!job.done) { jobPhase = null; setPhase(body.phase); } });
  receive("trace", (body) => line(els.log, String(body.line).includes("FAILED") ? "bad" : "dim", String(body.line || "")));
  receive("tool_call", (body) => {
    if (Array.isArray(body.dropped_args) && body.dropped_args.length) {
      line(els.log, "bad", `   ${jobText("已忽略未知参数", "Unknown arguments ignored")}: ${body.dropped_args.join(", ")}`);
    }
  });
  receive("tool_result", (body) => {
    if (!job.historical) trackSession(body);
    renderToolResult(body, { historical: job.historical });
  });
  receive("answer", (body) => {
    if (els.placeholder) els.placeholder.hidden = true;
    els.answer.hidden = false;
    renderMarkdown(els.answer, body.text);
  });
  receive("session", (body) => { if (!job.historical) renderSession(body); });
  // replay_gap is a control event, not a replayed task event; its id may equal the prior cursor.
  me.addEventListener("replay_gap", (event) => {
    if (!valid()) return;
    job.truncated = true;
    renderJobChrome();
    line(els.log, "bad", jobText("较早执行记录已截断；未收到的结果不会被补造。", "Earlier events were truncated; missing results cannot be reconstructed."));
  });
  receive("error", (body) => {
    // Server errors are task evidence, not a broken transport. Only done/catalog ends the task.
    line(els.log, "bad", `!! ${body.message || body.error || jobText("任务发生错误", "Task error")}`);
  });
  me.addEventListener("error", (event) => {
    if (!valid() || (typeof event.data === "string" && event.data.trim())) return;
    jobsKnown = false;
    showJobNotice("reconnecting");
    syncJobControls();
    // Keep EventSource open: it reconnects with Last-Event-ID. Status checks are bounded and
    // never submit a new task. A later successful event is de-duplicated by its sequence.
    scheduleJobRecovery(job);
  });
  receive("done", (body) => {
    const status = body.status === "succeeded" && body.success === true && !body.error ? "succeeded"
      : (body.status === "failed" ? "failed" : "partial");
    const detail = [];
    if (typeof body.error === "string" && body.error) detail.push(body.error);
    if (Number.isInteger(body.tool_failures) && body.tool_failures > 0) detail.push(jobText(
      `本次含 ${body.tool_failures} 次失败的工具调用；即使后续恢复，也不记为完全成功。`,
      `This run included ${body.tool_failures} failed tool call(s); recovery does not make it a fully successful run.`));
    if (typeof body.reason === "string" && body.reason) detail.push(jobText("原因：", "Reason: ") + body.reason);
    job.outcomeDetail = detail.join(" ");
    if (job.outcomeDetail) line(els.log, status === "succeeded" ? "dim" : "bad", job.outcomeDetail);
    finishJob(status, job);
  });
}

async function post(path, body, method, timeoutMs = 0) {
  const controller = timeoutMs && typeof AbortController === "function" ? new AbortController() : null;
  const timer = controller ? setTimeout(() => controller.abort(), timeoutMs) : null;
  try {
    const response = await fetch(path, {
      method: method || "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body || {}),
      ...(controller ? { signal: controller.signal } : {}),
    });
    return { status: response.status, data: await response.json().catch(() => ({})) };
  } finally { if (timer !== null) clearTimeout(timer); }
}

async function submitJob(path, body) {
  if (submissionPending) return false;
  if (!jobsKnown || backendBusy) {
    await recoverJobs({ restoreSaved: false });
    if (!jobsKnown || backendBusy) return false;
  }
  submissionPending = true;
  showJobNotice("submitting");
  syncJobControls();
  try {
    const { status, data } = await post(path, body, "POST", 15000);
    if (status === 202 && isRecord(data) && typeof data.job_id === "string" && typeof data.instance_id === "string") {
      if (backendInstance && backendInstance !== data.instance_id) {
        adoptJobCatalog({ instance_id: data.instance_id, jobs: [], active_job_id: null, busy: false });
      }
      backendInstance = data.instance_id;
      activeJobId = data.job_id;
      backendBusy = true;
      jobsKnown = true;
      const summary = { id: data.job_id, kind: path === "/api/ask" ? "agent" : "tool",
        label: body.text || body.tool, status: "running", done: false };
      jobSummaries = [summary, ...jobSummaries.filter((job) => job.id !== summary.id)].slice(0, 20);
      attach(data.job_id, data.instance_id, { summary });
      return true;
    }
    line(els.log, "bad", `!! ${isRecord(data) && data.error ? data.error : `HTTP ${status}`}`);
    // Rejected or uncertain submissions leave the question intact. A 409 reconnects to the
    // existing task; it never retries the POST or silently queues the question a second time.
    await recoverJobs({ restoreSaved: false });
    if (jobsKnown && !backendBusy) showJobNotice("submission_failed");
    return false;
  } catch (err) {
    jobsKnown = false;
    line(els.log, "bad", jobText(
      "提交结果未确认；正在查询后端现有任务，不会自动重复发送。问题仍保留在输入框。",
      "Submission was not confirmed. Checking existing backend tasks without resending; your question is preserved."));
    await recoverJobs({ restoreSaved: false });
    if (jobsKnown && !backendBusy) showJobNotice("submission_failed");
    return false;
  } finally {
    submissionPending = false;
    syncJobControls();
    renderJobHistory();
  }
}

async function run() {
  if (!selected && els.tool.value !== "list_datasets") {
    line(els.log, "bad", "!! 先在左侧选择一个数据文件");
    return;
  }
  await submitJob("/api/run", { tool: els.tool.value, args: buildArgs() });
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
  const body = { text: question };
  if (selected) body.file = selected;
  const accepted = await submitJob("/api/ask", body);
  if (accepted && els.prompt.value.trim() === question) els.prompt.value = "";
}

/* -------------------------------------------------------------------- startup */

async function loadFiles() {
  const epoch = backendContextEpoch;
  const response = await fetch("/api/files");
  const payload = await response.json().catch(() => ({ files: [] }));
  if (epoch !== backendContextEpoch) return;
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
      if (epoch !== backendContextEpoch || submissionPending || !jobsKnown || backendBusy) return;
      els.files.querySelectorAll(".file").forEach((x) => x.setAttribute("aria-pressed", "false"));
      button.setAttribute("aria-pressed", "true");
      selected = entry.path;
      session = null;
      clearPlanCard();
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
  syncJobControls();
}

els.tool.addEventListener("change", () => {
  els.sessionOp.disabled = els.tool.value !== "dataset_session";
});
$("runner").addEventListener("submit", (event) => { event.preventDefault(); run(); });
$("asker").addEventListener("submit", (event) => { event.preventDefault(); ask(); });
$("refresh-jobs").addEventListener("click", () => recoverJobs({ manual: true }));
$("job-reconnect").addEventListener("click", () => recoverJobs({ manual: true }));

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
let resetMessageKey = "";
let resetMessageValues = {};
function renderResetControls() {
  const button = $("reset-all"), message = $("reset-msg");
  if (button) {
    // The connection dialog and the reset request own separate locks. Neither may release
    // the other's lock while polling, switching language, or finishing a failed request.
    button.disabled = button.dataset.remoteBlocked === "true" || button.dataset.resetBusy === "true"
      || button.dataset.taskBusy === "true";
    button.textContent = t(button.dataset.resetArmed === "true" ? "再点一次确认擦除" : "恢复初始状态");
  }
  if (message) {
    let value = t(resetMessageKey);
    for (const [key, replacement] of Object.entries(resetMessageValues)) value = value.replace(`{${key}}`, String(replacement));
    message.textContent = value;
  }
}
function resetFeedback(key, values = {}) {
  resetMessageKey = key;
  resetMessageValues = values;
  renderResetControls();
}
async function resetRequest(path, options = {}) {
  const controller = typeof AbortController === "function" ? new AbortController() : null;
  const timeout = controller ? setTimeout(() => controller.abort(), 15000) : null;
  try {
    const response = await fetch(path, { ...options, cache: "no-store", ...(controller ? { signal: controller.signal } : {}) });
    return { status: response.status, data: await response.json().catch(() => null) };
  } finally { if (timeout !== null) clearTimeout(timeout); }
}
(() => {
  const button = $("reset-all");
  let timer = null;
  button.addEventListener("click", async () => {
    if (button.disabled || button.dataset.resetBusy === "true") return;
    if (timer === null) {
      button.classList.add("armed");
      button.dataset.resetArmed = "true";
      resetFeedback("请再次确认重置。重置后将进入设置新访问密码页；只想退出登录请使用“退出”。");
      timer = setTimeout(() => {
        timer = null;
        button.classList.remove("armed");
        button.dataset.resetArmed = "false";
        resetFeedback("");
      }, 4000);
      return;
    }
    clearTimeout(timer);
    timer = null;
    button.dataset.resetArmed = "false";
    button.dataset.resetBusy = "true";
    button.classList.remove("armed");
    let sent = false;
    resetFeedback("正在核对计算位置与重置权限…");
    try {
      const probe = await resetRequest("/api/backend");
      const headers = { "Content-Type": "application/json" };
      // Only a confirmed 404 identifies a standalone backend. A failed/malformed gateway
      // probe must never degrade into an unguarded destructive request.
      if (probe.status !== 404) {
        if (probe.status !== 200 || !probe.data || !["local", "remote"].includes(probe.data.mode)) {
          resetFeedback("无法核对重置权限，未发送重置请求。请检查连接或重新登录后再试。");
          return;
        }
        button.dataset.remoteBlocked = String(probe.data.mode === "remote");
        if (probe.data.mode === "remote") {
          resetFeedback("当前使用远端后端，禁止在此重置。请先在“计算连接”中选择“使用本机”。");
          return;
        }
        if (typeof probe.data.csrf_token !== "string" || !probe.data.csrf_token.trim()) {
          resetFeedback("无法核对重置权限，未发送重置请求。请检查连接或重新登录后再试。");
          return;
        }
        headers["X-GWB-CSRF"] = probe.data.csrf_token;
      }
      resetFeedback("正在重置，请勿重复提交…");
      sent = true;
      // No automatic retry, even on CSRF rejection: this action destroys configuration.
      const { status, data } = await resetRequest("/api/reset", { method: "POST", headers, body: "{}" });
      if (status === 200 && data && data.ok === true) {
        window.location.replace("/");
        return;
      }
      resetFeedback("重置未确认成功（HTTP {status}），未自动重试。请刷新检查；若仍需重置，请重新确认。", { status });
    } catch (error) {
      resetFeedback(sent
        ? "无法确认重置结果，未自动重试。请先刷新检查，不要立即重复重置。"
        : "无法核对重置权限，未发送重置请求。请检查连接或重新登录后再试。");
    } finally {
      button.dataset.resetBusy = "false";
      renderResetControls();
    }
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
$("open-settings").addEventListener("click", async () => {
  $("set-msg").textContent = "";
  $("set-key").value = "";
  // Re-read from the server on every open, so the form cannot show a stale address or a stale
  // `记住密钥 / Remember key` box: the checkbox's value is submitted back verbatim, and a stale
  // unchecked box is what used to delete an already-stored key during an unrelated edit.
  veil.classList.add("open");
  await refreshState();
  loadModels();
});
$("cancel").addEventListener("click", () => veil.classList.remove("open"));
addEventListener("keydown", (event) => { if (event.key === "Escape") veil.classList.remove("open"); });

function settingsAppliedMessage(data) {
  const names = missingFieldNames(data);
  const status = data.config_ready ? t("设置已应用，提问框已可用。")
    : t("设置已应用，但提问框还不可用，缺少：{fields}").replace("{fields}", names || t("未知项"));
  // Runtime-only credentials are usable without being persisted. Preserve the server's warning
  // about their lifetime while still reporting the actual readiness, not the checkbox state.
  return status + (data.warning ? " " + data.warning : "");
}

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
  $("set-msg").textContent = settingsAppliedMessage(data);
  $("set-key").value = "";
  await refreshState();
});

let modelFetchGen = 0;

function fillModels(models) {
  const select = $("set-model");
  const current = select.dataset.current || "";
  const known = Array.isArray(models) ? models : [];
  select.replaceChildren();
  const placeholder = document.createElement("option");
  placeholder.value = "";
  placeholder.textContent = t("请选择模型");
  select.appendChild(placeholder);
  for (const name of known) {
    const option = document.createElement("option");
    option.value = name;
    option.textContent = name;
    select.appendChild(option);
  }
  if (known.length && current && known.includes(current)) select.value = current;
  else if (!known.length && current) {
    const kept = document.createElement("option");
    kept.value = current;
    kept.textContent = current;
    select.appendChild(kept);
    select.value = current;
  } else select.value = "";
  select.dataset.current = select.value;
}

async function loadModels() {
  const gen = ++modelFetchGen;
  const url = $("set-url").value.trim();
  if (!url) {
    fillModels([]);
    return;
  }
  $("set-msg").textContent = t("正在读取模型列表…（不会发送分析数据）");
  const body = { base_url: url };
  const key = $("set-key").value.trim();
  if (key) body.api_key = key;
  const { status, data } = await post("/api/models", body);
  if (gen !== modelFetchGen) return;
  const models = data && Array.isArray(data.models) ? data.models : [];
  if (status === 404) {
    fillModels([]);
    $("set-msg").textContent = t("工作台进程还是旧的，请先重启它，再读取模型列表。");
    return;
  }
  if (status !== 200 || !data || data.ok !== true || !models.length) {
    fillModels([]);
    $("set-msg").textContent = (data && data.error) || t("读取失败：请检查地址、密钥或网络；服务不支持 /models 时可手填模型名。");
    return;
  }
  fillModels(models);
  $("set-msg").textContent = t("已读取 {count} 个模型，请选择一个。").replace("{count}", models.length);
}

$("fetch-models").addEventListener("click", () => loadModels());
$("set-url").addEventListener("change", () => {
  if ($("set-url").value.trim() !== ($("set-model").dataset.url || "")) {
    $("set-model").dataset.current = "";
  }
  loadModels();
});
$("set-key").addEventListener("change", () => loadModels());
$("set-model").addEventListener("change", () => {
  $("set-model").dataset.current = $("set-model").value;
});

async function refreshState() {
  const epoch = backendContextEpoch;
  const state = await fetch("/api/state").then((r) => r.json()).catch(() => null);
  if (epoch !== backendContextEpoch) return null;
  if (state) {
    renderState(state);
    $("set-url").value = state.base_url || "";
    $("set-model").dataset.url = state.base_url || "";
    $("set-model").dataset.current = state.model || "";
    // The checkbox used to open unchecked no matter what was on disk, while the submit below
    // always sends `remember_key` -- so editing only the model silently deleted a stored key.
    // Prefill it, and let an explicit uncheck be the only thing that can remove one.
    $("set-remember").checked = Boolean(state.remember_key);
  }
  return state;
}
function applyReleaseResponse(status, data) {
  const doc = isRecord(data) ? data : {};
  const counters = ["closed", "warm_frames_dropped", "sessions_after", "warm_frames_after"];
  const verified = counters.every((key) => Number.isInteger(doc[key]) && doc[key] >= 0)
    && doc.sessions_after === 0 && doc.warm_frames_after === 0;
  if (status < 200 || status >= 300 || resultFailed(data)
      || (isRecord(doc.detail) && resultFailed(doc.detail))
      || (isRecord(doc.state) && Boolean(doc.state.error)) || !verified) {
    const reason = doc.error || (doc.detail || {}).error || (doc.state || {}).error
      || (status < 200 || status >= 300 ? `HTTP ${status}` : "未确认全部会话与缓存已释放");
    line(els.log, "bad", `!! 释放失败：${reason}`);
    return false;
  }
  // Only an acknowledged release may discard the id. A busy 409 or a failed worker call leaves
  // the resident frame alive; clearing its id here would strand it and break the next analysis.
  session = null;
  clearPlanCard();
  const freed = doc.bytes_freed_mb ? `，约 ${doc.bytes_freed_mb} MB` : "";
  line(els.log, "ok", `释放：关闭 ${doc.closed} 个会话，丢弃 ${doc.warm_frames_dropped} 个保留帧${freed}；` +
       `之后 ${doc.sessions_after} 会话 / ${doc.warm_frames_after} 帧`);
  if (isRecord(doc.state)) renderSession({ ...doc.state, reused: false });
  return true;
}

els.release.addEventListener("click", async () => {
  els.release.disabled = true;
  try {
    const { status, data } = await post("/api/session/release");
    applyReleaseResponse(status, data);
  } catch (err) {
    line(els.log, "bad", `!! 释放失败：${err.message || err}`);
  } finally {
    els.release.disabled = false;
  }
});

followGlow();
syncJobControls();
renderJobChrome();
recoverJobs();
loadFiles().catch(() => {});
refreshState()
  .then(() => setTimeout(layoutSelfTest, 900))
  .catch(() => {});

/* The page measures its own composer once per load and posts the numbers to /api/diag:
   geometry only -- position, scroll displacement, viewport, pixel ratio, ancestors -- stored
   beside the config as diag.json. "The input bar is not pinned" then gets answered with a
   measurement from the very browser that rendered the page, not with theories about browsers
   nobody measured. The verdict is also spoken out loud, right here in the log. */
let layoutSelfTestRan = false;
function layoutSelfTest() {
  if (layoutSelfTestRan) return;
  layoutSelfTestRan = true;
  const dock = document.querySelector(".composer-dock");
  const build = document.getElementById("build");
  if (!dock) {
    reportLayout({ build: build ? build.textContent : "", dock: "absent", pinned: false });
    return;
  }
  const cs = getComputedStyle(dock);
  const chain = [];
  for (let node = dock.parentElement; node; node = node.parentElement) {
    const s = getComputedStyle(node);
    const poison = (s.transform !== "none" ? "transform" : "") +
                   (s.filter !== "none" ? " filter" : "") +
                   (s.perspective !== "none" ? " perspective" : "") +
                   (String(s.willChange || "").includes("transform") ? " will-change" : "") +
                   (((s.backdropFilter || "none") !== "none" && s.backdropFilter) ? " backdrop" : "");
    chain.push(node.tagName.toLowerCase() + (poison ? ":" + poison.trim().replace(/ /g, "+") : ""));
  }
  const from = window.scrollY;
  const before = dock.getBoundingClientRect().top;
  window.scrollTo(0, from + 250);
  const reached = window.scrollY;
  const after = dock.getBoundingClientRect().top;
  window.scrollTo(0, from);
  const moved = Math.abs(after - before);
  const payload = {
    build: build ? build.textContent : "",
    position: cs.position,
    pinned: moved < 1,
    moved_px: Math.round(moved),
    scroll_from_px: Math.round(from),
    scroll_to_px: Math.round(reached),
    page_height_px: document.documentElement.scrollHeight,
    viewport: `${innerWidth}x${innerHeight}`,
    screen: `${screen.width}x${screen.height}`,
    outer: `${outerWidth}x${outerHeight}`,
    device_pixel_ratio: window.devicePixelRatio,
    ancestors: chain.join(" "),
    agent: navigator.userAgent.slice(0, 160),
  };
  reportLayout(payload);
  line(els.log, payload.pinned ? "dim" : "bad",
       (payload.pinned ? t("布局自检：输入栏固定在视口底部（滚动位移 {m}px）。")
                      : t("布局自检：输入栏未固定！滚动位移 {m}px，报告已写入服务器。"))
         .replace("{m}", payload.moved_px));
  if (!payload.pinned) {
    els.banner.hidden = false;
    els.banner.dataset.kind = "bad";
    els.banner.textContent = t("底部输入栏未固定：当前浏览器的布局自检已检出，详情在执行记录与 diag.json。");
  }
}

function followGlow() {
  if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) return;
  const root = document.documentElement;
  let x = innerWidth * 0.62;
  let y = innerHeight * 0.28;
  let tx = x;
  let ty = y;
  addEventListener("pointermove", (event) => {
    tx = event.clientX;
    ty = event.clientY;
  }, { passive: true });
  const frame = () => {
    x += (tx - x) * 0.07;
    y += (ty - y) * 0.07;
    root.style.setProperty("--mx", x + "px");
    root.style.setProperty("--my", y + "px");
    requestAnimationFrame(frame);
  };
  requestAnimationFrame(frame);
}

function reportLayout(payload) {
  fetch("/api/diag", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  }).catch(() => { /* a report that cannot be delivered still told the operator on screen */ });
}
