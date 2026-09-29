/* 在 GB10 上行为化验证 N1（面板回填）与 N2（断流解锁），对着真服务的 /api/* 跑真 app.js。
 * BASE=http://127.0.0.1:8768 CONFIG=<path> node verify-n1n2.js
 */
const { JSDOM, VirtualConsole } = require("jsdom");
const http = require("http");
const BASE = process.env.BASE;
const errs = [];
const vc = new VirtualConsole();
vc.on("jsdomError", (e) => errs.push(String(e.message || e).split("\n")[0]));
// Content-Length 必须手工给：node 的 write() 默认 chunked，而 gui.py 的 _body() 按 Content-Length
// 读体。补丁前那会被当成空补丁并回 ok:true；补丁后是 400 —— 两种都会让这个桥接层骗过测试。
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
const made = [];
let pass = 0, fail = 0;
const ck = (name, ok, note) => { console.log(`  [${ok ? "PASS" : "FAIL"}] ${name}${note ? "  " + note : ""}`); ok ? pass++ : fail++; };

(async () => {
  const dom = await JSDOM.fromURL(BASE + "/", {
    runScripts: "dangerously", resources: "usable", pretendToBeVisual: true, virtualConsole: vc,
    beforeParse(w) {
      w.fetch = nf;
      w.EventSource = function (url) {
        const self = { url, listeners: {}, closed: false,
          addEventListener(n, fn) { (this.listeners[n] = this.listeners[n] || []).push(fn); },
          close() { this.closed = true; } };
        made.push(self); return self;
      };
      w.EventSource.OPEN = 1;
    },
  });
  await new Promise((r) => setTimeout(r, 3000));
  const w = dom.window, d = w.document, el = (id) => d.getElementById(id);

  console.log("== N1 设置面板回填 ==");
  await w.refreshState();
  ck("state 里带 remember_key，勾选框被回填", el("set-remember").checked === true,
     "checked=" + el("set-remember").checked);
  ck("地址/模型同时回填", el("set-url").value.length > 5 && el("set-model").value.length > 3,
     el("set-url").value + " / " + el("set-model").value);
  // 打开面板这个动作本身也要重读（旧代码只在启动与保存后读，跨标签页改过就陈旧）
  el("set-remember").checked = false;
  d.querySelector("#open-settings").dispatchEvent(new w.Event("click", { bubbles: true }));
  await new Promise((r) => setTimeout(r, 1200));
  ck("重开面板会把勾选框恢复成磁盘真相", el("set-remember").checked === true,
     "checked=" + el("set-remember").checked);
  const veil = el("veil");
  const wasOpen = veil ? veil.className.includes("open") : null;
  el("cancel").dispatchEvent(new w.Event("click", { bubbles: true }));
  await new Promise((r) => setTimeout(r, 200));
  ck("取消按钮仍然只负责关面板（我中途差点改坏它）",
     !!veil && wasOpen === true && !veil.className.includes("open"),
     `open before=${wasOpen} after=${veil ? veil.className.includes("open") : "?"}`);

  console.log("");
  console.log("== N2 断流解锁 ==");
  // 直接测我改的那个 handler：不绕 run() 的表单状态。手工锁上按钮 → attach 建流 → 派发 error。
  made.length = 0;
  el("run").disabled = true;
  el("ask").disabled = true;
  if (typeof w.attach !== "function") { console.log("  attach 不是全局函数，无法直测"); process.exit(1); }
  w.attach("j-synthetic");
  await new Promise((r) => setTimeout(r, 150));
  const es = made[made.length - 1];
  ck("attach() 建立了流并留着锁定状态", !!es && el("run").disabled === true,
     "streams=" + made.length + " disabled=" + el("run").disabled);
  if (!es) { console.log("  没有流可测，后面的 N2 检查无从执行"); process.exit(1); }
  es.listeners.error[0]({});                      // 真传输错误：没有 data
  await new Promise((r) => setTimeout(r, 200));
  ck("传输错误后按钮解锁", el("run").disabled === false, "disabled=" + el("run").disabled);
  ck("并且流被关闭（不再无限重连）", es.closed === true, "closed=" + es.closed);
  ck("页脚状态变成可翻译的“连接中断”", el("phase").textContent.trim() === "连接中断",
     JSON.stringify(el("phase").textContent.trim()));
  ck("日志区分出“服务端未给出原因”",
     (el("log").textContent || "").includes("服务端未给出原因"), "");
  es.listeners.error[0]({ data: JSON.stringify({ message: "服务端给的原因" }) });
  await new Promise((r) => setTimeout(r, 120));
  ck("服务端带原因的 error 用原文，不混进默认文案",
     (el("log").textContent || "").includes("服务端给的原因"), "");
  console.log("");
  console.log(`结论: ${pass} PASS / ${fail} FAIL   启动期错误 ${errs.length}${errs.length ? " → " + errs[0] : ""}`);
  process.exit(fail ? 1 : 0);
})().catch((e) => { console.log("验证器异常:", e.message); process.exit(2); });
