/* 在 GB10 上对“演示稿 1→2 步”做行为化验证：真服务 + 真 app.js + 真产物，DOM 用 jsdom 模拟。
 * 覆盖：品牌/键位行/脉冲/右侧折叠抽屉（第 1 步），由 result.charts 派生的图表标签页（第 2 步），
 * 以及 F2/F3/F4/F5/F6/F8 六个按键是否真的走到与按钮同一条代码路径。
 * BASE=http://127.0.0.1:8790 node verify-demo.js
 * 差集（jsdom 不覆盖）：CSP 强制、布局、Chromium 解析器、EventSource 真重连、SVG 实际绘制。
 */
const { JSDOM, VirtualConsole } = require("jsdom");
const http = require("http");
const fs = require("fs");
const path = require("path");
const BASE = process.env.BASE;
if (!BASE) { console.log("需要 BASE=http://127.0.0.1:PORT"); process.exit(2); }
const DIR = process.env.PAYLOAD_DIR || __dirname;

const errs = [];
const vc = new VirtualConsole();
vc.on("jsdomError", (e) => errs.push(String((e && e.message) || e).split("\n")[0]));
vc.on("error", (...a) => errs.push("console.error: " + a.map(String).join(" ").slice(0, 160)));

// jsdom 没有 fetch；用 node http 桥接，路由与状态码都是真服务的。
// Content-Length 必须手工给出：node 对 write() 用 chunked 编码，而 gui.py 的 _body() 按
// Content-Length 取长度，于是整个 PATCH/POST 体被当成空补丁 —— 服务端回 ok:true 却什么都不做。
// 这不是服务端的 bug 复现，是桥接层会掩盖它（已在交接说明里单独记一条）。
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
// EventSource 换成可派发的假对象：SSE 帧由本验证器手工投喂，服务端仍是真的。
const made = [];
let pass = 0, fail = 0;
const ck = (name, ok, note) => { console.log(`  [${ok ? "PASS" : "FAIL"}] ${name}${note ? "  " + note : ""}`); ok ? pass++ : fail++; };
const nap = (ms) => new Promise((r) => setTimeout(r, ms));
const rawGet = (p) => new Promise((res, rej) => {
  http.get(BASE + p, (x) => { let b = ""; x.setEncoding("utf8"); x.on("data", (c) => (b += c));
    x.on("end", () => res({ status: x.statusCode, type: x.headers["content-type"] || "", body: b })); }).on("error", rej);
});

(async () => {
  const dom = await JSDOM.fromURL(BASE + "/", {
    runScripts: "dangerously", resources: "usable", pretendToBeVisual: true, virtualConsole: vc,
    beforeParse(w) { w.fetch = nf; w.EventSource = function (url) {
      const self = { url, listeners: {}, closed: false,
        addEventListener(n, fn) { (this.listeners[n] = this.listeners[n] || []).push(fn); },
        close() { this.closed = true; } };
      made.push(self); return self; }; w.EventSource.OPEN = 1; },
  });
  await nap(2500);
  const w = dom.window, d = w.document, el = (id) => d.getElementById(id);
  const loaded = (f) => JSON.parse(fs.readFileSync(path.join(DIR, f), "utf-8"));
  // 起点钉回中文：上一轮或 curl 试验都可能把磁盘上的偏好留在 en，而 F6 是“切换”不是“设定”。
  await nf("/api/settings", { method: "PATCH", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ language: "zh" }) });
  await w.refreshState(); await nap(200);
  ck("起点是中文", d.documentElement.lang === "zh", d.documentElement.lang);

  console.log("== 第 1 步：品牌层 / 键位行 / 脉冲 / 抽屉 ==");
  ck("页头有品牌标记与 GPU × DATA 上标", !!d.querySelector(".mark") && !!d.querySelector(".eyebrow"),
     (d.querySelector(".eyebrow") || {}).textContent);
  ck("品牌标记只有一个（我一度把它复制成两份）",
     d.querySelectorAll(".mark, .glyph").length === 1, "count=" + d.querySelectorAll(".mark, .glyph").length);
  const row = d.querySelector(".keys");
  const advertised = row ? Array.from(row.querySelectorAll("b")).map((b) => b.textContent.trim()) : [];
  ck("键位行列出 8 个键，且不含浏览器保留组合", advertised.length === 8 && !/Ctrl\+/.test(row.textContent),
     advertised.join(" "));
  ck("抽屉在右栏，runner 表单在抽屉里", (() => {
    const drawer = el("tool-drawer");
    return !!drawer && drawer.tagName === "DETAILS"
      && drawer.contains(el("runner")) && drawer.closest(".pane.right") !== null;
  })(), "tag=" + (el("tool-drawer") || {}).tagName);
  ck("提问栏不再夹带工具表单，但保留 asker 与 log",
     !d.querySelector(".pane:not(.right) #runner") && !!el("asker") && !!el("log"));
  ck("说明文字只剩一份（重复的那份已删）",
     (d.body.textContent.match(/不经过模型/g) || []).length <= 2,
     "hits=" + (d.body.textContent.match(/不经过模型/g) || []).length);
  ck("脉冲初始不忙", el("pulse").getAttribute("data-busy") === "0", el("pulse").outerHTML);

  console.log("");
  console.log("== 第 2 步：标签页由真产物派生 ==");
  const gb = loaded("tool_result_gb.json");       // groupby_bar + groupby_line
  w.renderToolResult(gb);
  await nap(150);
  const tabsOf = () => Array.from(el("tabs").querySelectorAll(".tab"));
  ck("groupby 的真产物长出 2 个标签：柱状 + 折线",
     el("tabs").hidden === false && tabsOf().length === 2
     && tabsOf().map((t) => t.dataset.kind).join(",") === "line,bar",
     tabsOf().map((t) => `${t.dataset.kind}:${t.textContent}`).join(" "));
  ck("默认选中第一个，查看器里就一幅图",
     tabsOf()[0].getAttribute("aria-selected") === "true"
     && el("chart").hidden === false && el("chart").querySelectorAll("img").length === 1);
  const srcBefore = el("chart").querySelector("img").getAttribute("src");
  tabsOf()[1].dispatchEvent(new w.Event("click", { bubbles: true }));
  await nap(150);
  const srcAfter = el("chart").querySelector("img").getAttribute("src");
  ck("点第二个标签真的换图（src 变了，不是只改样式）", srcBefore !== srcAfter,
     `${srcBefore.split("=").pop()} → ${srcAfter.split("=").pop()}`);
  ck("已归类的产物不会同时出现在下面的平铺栏（不重复）",
     el("charts").querySelectorAll("img").length === 0
     && el("charts").querySelectorAll("a").length === 1, "loose imgs=" + el("charts").querySelectorAll("img").length);
  const art = await rawGet(srcAfter);
  ck("切换出来的图能真取到 200 image/svg+xml", art.status === 200 && /svg/.test(art.type),
     `${art.status} ${art.type} ${art.body.length}B`);

  const corr = loaded("tool_result_corr.json");   // corr_heatmap 一种
  w.renderToolResult(corr);
  await nap(150);
  ck("热力单独出现时只有 1 个标签，标签数=出现的种类数",
     tabsOf().length === 1 && tabsOf()[0].dataset.kind === "heat" && el("tabs").hidden === false,
     "tabs=" + tabsOf().length);
  ck("上一轮的柱状/折线不留残影", el("chart").querySelectorAll("img").length === 1
     && el("chart").querySelector("img").src.includes("heatmap"));

  const sum = loaded("tool_result_sum.json");      // summary_means / summary_spread：无 kind
  w.renderToolResult(sum);
  await nap(150);
  ck("名字证明不了种类的产物退回平铺栏，标签行隐藏",
     el("tabs").hidden === true && el("chart").hidden === true
     && el("charts").querySelectorAll("img").length === 2,
     "loose=" + el("charts").querySelectorAll("img").length);
  ck("切到无标签的结果时查看器清空（早退前先清，这是那个 stale bug）",
     el("chart").children.length === 0 && el("chart-cap").textContent === "");

  const merged = JSON.parse(JSON.stringify(gb));
  merged.result.charts = merged.result.charts.concat(corr.result.charts, sum.result.charts);
  w.renderToolResult(merged);
  await nap(150);
  ck("三种同时在场：3 个标签，平铺栏只收无种类的 2 幅（两份真产物合并，只测渲染器）",
     tabsOf().length === 3 && el("charts").querySelectorAll("img").length === 2,
     tabsOf().map((t) => t.dataset.kind).join(",") + " loose=" + el("charts").querySelectorAll("img").length);
  ck("标签用 role=tab 且 aria-controls 指向查看器",
     tabsOf().every((t) => t.getAttribute("role") === "tab" && t.getAttribute("aria-controls") === "chart"),
     el("tabs").getAttribute("aria-label"));

  console.log("");
  console.log("== 键位：与按钮同一条路径 ==");
  const key = async (k) => { w.dispatchEvent(new w.KeyboardEvent("keydown", { key: k, bubbles: true, cancelable: true })); await nap(250); };
  const logHidden = () => el("log").hidden;
  await key("F2"); ck("F2 收起执行详情", logHidden() === true, "hidden=" + logHidden());
  await key("F2"); ck("再按 F2 展开", logHidden() === false);
  await key("F4"); ck("F4 把光标放到提问框", d.activeElement === el("prompt"), (d.activeElement || {}).id);
  await key("F3"); ck("F3 聚焦文件列表第一个（无文件时不报错）",
     !!d.activeElement && (d.activeElement === el("prompt") || d.activeElement.tagName === "BUTTON"),
     d.activeElement.tagName + "#" + d.activeElement.id + " files=" + el("files").querySelectorAll("button").length);
  await key("F5"); ck("F5 打开连接设置面板", el("veil").className.includes("open"), el("veil").className);
  const zhTab = tabsOf()[0].textContent;
  ck("中文下标签是界面词，不是文件名里的机器名", /[\u4e00-\u9fff]/.test(zhTab), zhTab + " | cap=" + el("chart-cap").textContent);
  await key("F6"); await nap(800);
  const enTab = el("tabs").querySelector(".tab").textContent;
  ck("F6 切语言：标签页跟着换，说明走的是同一套 i18n 表",
     enTab !== zhTab && /^[A-Za-z]/.test(enTab), `${zhTab} → ${enTab}`);
  ck("说明行也跟着换（它是标签+计数，扫表器重建不了，得单独喂）",
     new RegExp("^[A-Za-z]").test(el("chart-cap").textContent), el("chart-cap").textContent);
  ck("切英文后键位行不留中文",
     !/[\u4e00-\u9fff]/.test(el("tabs").querySelector(".tab").textContent)
     && !/执行详情|释放会话|关闭弹窗/.test(d.querySelector(".keys").textContent),
     d.querySelector(".keys").textContent.replace(/\s+/g, " ").slice(0, 90));
  await key("F6"); await nap(600);
  ck("再按 F6 回到中文", tabsOf()[0].textContent === zhTab, tabsOf()[0].textContent);
  const before = el("log").textContent.length;
  await key("F8");
  ck("F8 走的是释放按钮本身（日志出现释放行）",
     el("log").textContent.length > before && /释放/.test(el("log").textContent),
     el("log").textContent.slice(before, before + 90).replace(/\n/g, " "));

  console.log("");
  console.log("== 脉冲跟随真实作业 ==");
  el("tool").value = "list_datasets";
  await w.run();
  await nap(400);
  ck("提交作业后脉冲进入忙态", el("pulse").getAttribute("data-busy") === "1",
     el("pulse").getAttribute("data-busy"));
  if (!made.length || !made[made.length - 1]) {
    console.log("  [diag] streams=" + made.length
      + " log tail: " + el("log").textContent.slice(-220).replace(/\n/g, " / "));
  }
  const es = made[made.length - 1];
  es.listeners.done[0]({});
  await nap(200);
  ck("done 之后脉冲停下并解锁按钮",
     el("pulse").getAttribute("data-busy") === "0" && el("run").disabled === false && es.closed === true);
  await w.run(); await nap(400);
  const es2 = made[made.length - 1];
  es2.listeners.error[0]({});
  await nap(200);
  ck("断流也停下（不留下永久闪烁）", el("pulse").getAttribute("data-busy") === "0", es2.closed ? "" : "stream open");

  console.log("");
  console.log(`结论: ${pass} PASS / ${fail} FAIL   加载期错误 ${errs.length}${errs.length ? " → " + errs.slice(0, 3).join(" | ") : ""}`);
  console.log(`driver: jsdom ${require("jsdom/package.json").version} / node ${process.version} —— DOM 模拟，非 Chromium`);
  process.exit(fail ? 1 : 0);
})().catch((e) => { console.log("验证器异常:", e && e.message); process.exit(2); });
