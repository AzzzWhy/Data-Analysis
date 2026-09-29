const { JSDOM, VirtualConsole } = require("jsdom");
const http = require("http");
const BASE = process.env.BASE;
const CSV = process.env.CSV;
const errs = [];
const vc = new VirtualConsole();
vc.on("jsdomError", (e) => errs.push(String(e.message || e).split("\n")[0]));
const nf = (u, o) => new Promise((res, rej) => {
  const U = new URL(u, BASE);
  const headers = Object.assign({}, (o && o.headers) || {});
  const body = (o && o.body) || null;
  if (body) headers["Content-Length"] = Buffer.byteLength(body);
  const r = http.request(U, { method: (o && o.method) || "GET", headers }, (x) => {
    let b = ""; x.setEncoding("utf8"); x.on("data", (c) => (b += c));
    x.on("end", () => res({ ok: x.statusCode < 400, status: x.statusCode,
      json: () => Promise.resolve(JSON.parse(b || "{}")), text: () => Promise.resolve(b) }));
  });
  r.on("error", rej); if (body) r.write(body); r.end();
});
const streams = [];
const nap = (ms) => new Promise((r) => setTimeout(r, ms));
const job = async (tool, args) => {
  const j = await nf("/api/run", { method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ tool, args }) }).then((r) => r.json());
  return new Promise((resolve) => {
    nf("/api/events?job=" + encodeURIComponent(j.job_id)).then((res) => res.text()).then((txt) => {
      let name = null, out = null;
      for (const line of txt.split("\n")) {
        if (line.startsWith("event: ")) name = line.slice(7);
        else if (line.startsWith("data: ") && name === "tool_result") {
          const p = JSON.parse(line.slice(6));
          if (p.name === "export_deliverables" || out === null) out = out || p;
          out = p;
        }
      }
      resolve(out);
    });
  });
};
const box = (s) => console.log("  │ " + String(s).replace(/\n/g, "\n  │ "));

(async () => {
  const dom = await JSDOM.fromURL(BASE + "/", { runScripts: "dangerously", resources: "usable",
    pretendToBeVisual: true, virtualConsole: vc,
    beforeParse(w) { w.fetch = nf; w.EventSource = function (u) {
      const self = { url: u, listeners: {}, closed: false, addEventListener(n, f) { (this.listeners[n] = this.listeners[n] || []).push(f); }, close() {} };
      streams.push(self); return self; }; } });
  await nap(2500);
  const w = dom.window, d = w.document, el = (id) => d.getElementById(id);
  const txt = (id) => (el(id).textContent || "").replace(/\s+/g, " ").trim();
  const rows = (card) => Array.from(d.querySelectorAll(card + " .row"))
    .map((r) => (r.querySelector(".k").textContent.trim() + " = " + r.querySelector(".v").textContent.trim()));

  const sess = await job("dataset_session", { operation: "open", file_path: CSV, goal: "按 region 比较 revenue" });
  const exp = await job("export_deliverables", { file_path: CSV, operation: "groupby", by: "region",
    agg: "revenue:sum,mean|profit:sum", out_dir: "/home/Developer/hks-run/fixes5/benchmark/gui-demo/deliv-snap" });
  const st = await nf("/api/state").then((r) => r.json());
  w.renderState(st);
  if (sess && sess.result && sess.result.session_id) w.renderSession({ ...st.session, reused: false });
  w.renderToolResult(exp);
  await nap(400);

  console.log("┌─ 页头 ─────────────────────────────────────────────");
  box(`${txt("ctx-model")}  ·  ${txt("ctx-host")}  ·  ${txt("ctx-file")}   [中文][设置]`);
  console.log("├─ 横幅 ───────────────────────────────────────────");
  box(el("banner").hidden ? "(隐藏)" : txt("banner"));
  console.log("├─ 左栏 aside ───────────────────────────────────────");
  const files = Array.from(el("files").querySelectorAll("button")).slice(0, 3)
    .map((b) => b.textContent.replace(/\s+/g, " ").trim());
  box("数据文件: " + files.join(" | ") + ` …共 ${el("files").querySelectorAll("button").length} 个`);
  box("计划: " + (d.querySelectorAll("#plan-steps li").length ? `${txt("plan-kind")} / ${txt("plan-progress")}` : "(空)"));
  for (const li of Array.from(d.querySelectorAll("#plan-steps li")).slice(0, 6))
    box("   · " + li.textContent.replace(/\s+/g, " ").trim().slice(0, 60) + `  [done=${li.dataset.done || "-" + ""}]`);
  box("会话: " + rows("#sess-card").join("   "));
  box("   " + txt("sess-hint").slice(0, 70));
  console.log("├─ 中栏 pane（提问 + 执行详情） ─────────────────────");
  box("提问框 " + (el("prompt").disabled ? "disabled" : "可用") + "  placeholder: " + (el("prompt").placeholder || "").slice(0, 40));
  box("提示: " + txt("ask-hint").slice(0, 60));
  box("输出（" + el("drawer-toggle").textContent.trim() + "）" + (el("log").hidden ? " 已收起" : ""));
  const log = (el("log").textContent || "").split("\n").filter((l) => l.trim()).slice(-4);
  log.forEach((l) => box("   " + l.trim().slice(0, 92)));
  console.log("├─ 右栏 pane.right（结果） ──────────────────────────");
  box("直接工具调用抽屉: " + (el("tool-drawer").open ? "展开" : "折叠（点标题展开，25 个控件在里面）"));
  box("KPI: " + Array.from(el("kpis").querySelectorAll(".l")).map((n) =>
    n.textContent.trim() + "=" + (n.nextElementSibling ? n.nextElementSibling.textContent.trim() : "?")).join("  "));
  const tabs = Array.from(el("tabs").querySelectorAll(".tab"));
  box("图表标签页: " + (el("tabs").hidden ? "无（这批产物里没有可归类的种类）"
    : tabs.map((t) => (t.getAttribute("aria-selected") === "true" ? "▸" : "·")
      + t.textContent.trim()).join("  ")));
  const imgs = Array.from(el("chart").querySelectorAll("img")).map((i) => i.alt);
  if (imgs.length) box("   当前标签页里的图: " + imgs.join(", ") + "   ← " + txt("chart-cap"));
  box("未归类产物: " + (el("charts").querySelectorAll("img").length
    ? Array.from(el("charts").querySelectorAll("img")).map((i) => i.alt).join(", ") : "无")
    + (el("charts").querySelector("a") ? "  + [打开报告 report.md]" : ""));
  box("结果表: " + (el("tables").querySelectorAll("table").length
    ? Array.from(el("tables").querySelectorAll("h3, .tbl-title, b")).slice(0, 3).map((n) => n.textContent.trim()).join(" | ") || `${el("tables").querySelectorAll("table").length} 张` : "无"));
  console.log("├─ 页脚 + 键位行 ────────────────────────────────────");
  box(`● ${txt("phase")}   ${txt("chip")}   ${txt("elapsed")}   busy=${el("pulse").getAttribute("data-busy")}`);
  box((el("keys") ? txt("keys") : d.querySelector(".keys").textContent.replace(/\s+/g, " ")).trim());
  console.log("└────────────────────────────────────────────────");
  console.log("  加载期错误: " + (errs.length ? errs.slice(0, 3).join(" | ") : "0"));
  process.exit(0);
})().catch((e) => { console.log("异常:", e.message); process.exit(2); });
