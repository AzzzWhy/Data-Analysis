/* 缺陷 #2/#3 的浏览器那一半：状态点名"缺哪一项"之后，界面文案是否真的跟着变。
 * BASE=http://127.0.0.1:8792 node verify-readiness.js
 * 起点用 remember_key=false 把密钥撤掉（这是产品语义里唯一撤密钥的路径），
 * 于是 config_ready=false 且 missing_fields=["api_key"]，正好是要演的状态。
 */
const { JSDOM, VirtualConsole } = require("jsdom");
const http = require("http");
const BASE = process.env.BASE;
if (!BASE) { console.log("需要 BASE=http://127.0.0.1:PORT"); process.exit(2); }

const errs = [];
const vc = new VirtualConsole();
vc.on("jsdomError", (e) => errs.push(String((e && e.message) || e).split("\n")[0]));
vc.on("error", (...a) => errs.push("console.error: " + a.map(String).join(" ").slice(0, 160)));
// 与 verify-demo 同样的教训：node 的 write() 走 chunked，而这个服务按 Content-Length 读体。
// 补丁前它会被当成空补丁并回 ok:true；补丁后是 400 —— 桥接层必须自己给 Content-Length。
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
let pass = 0, fail = 0;
const ck = (n, ok, note) => { console.log(`  [${ok ? "PASS" : "FAIL"}] ${n}${note ? "  " + note : ""}`); ok ? pass++ : fail++; };
const nap = (ms) => new Promise((r) => setTimeout(r, ms));
const patch = (obj) => nf("/api/settings", { method: "PATCH",
  headers: { "Content-Type": "application/json" }, body: JSON.stringify(obj) }).then((r) => r.json());

(async () => {
  const dom = await JSDOM.fromURL(BASE + "/", {
    runScripts: "dangerously", resources: "usable", pretendToBeVisual: true, virtualConsole: vc,
    beforeParse(w) { w.fetch = nf; w.EventSource = function () {
      return { addEventListener() {}, close() {} }; }; },
  });
  await nap(2500);
  const w = dom.window, d = w.document, el = (id) => d.getElementById(id);

  console.log("== 撤掉密钥：界面必须点名 api_key，而不是把三项一起报缺 ==");
  const dropped = await patch({ remember_key: false });
  await w.refreshState(); await nap(200);
  ck("PATCH 的响应本身就带着 missing_fields（表单不用二次猜测）",
     dropped.ok === true && dropped.config_ready === false
     && JSON.stringify(dropped.missing_fields) === '["api_key"]', JSON.stringify(dropped).slice(0, 160));
  ck("撤密钥之后响应里没有密钥值", !("api_key" in dropped)
     && !/synthetic|local"/.test(JSON.stringify(dropped)), Object.keys(dropped).join(","));
  const hint = el("ask-hint").textContent;
  const banner = el("banner").textContent;
  ck("提问框按事实关掉", el("ask").disabled === true && el("prompt").disabled === true);
  ck("提示行点名缺的是密钥", /密钥/.test(hint) && !/地址/.test(hint), hint);
  ck("横幅与提示行来自同一个事实源（同一句 notReadyText，不再各写各的）",
     /但还缺少：API 密钥/.test(banner) && /但还缺少：API 密钥/.test(hint),
     banner.slice(0, 80) + " || " + hint.slice(0, 80));
  ck("旧的三合一文案不再出现在屏幕上",
     !/没有配置 API 地址 \/ 密钥 \/ 模型/.test(hint + banner), (hint + banner).slice(0, 120));

  console.log("");
  console.log("== 切到英文：同一份事实，两种说法 ==");
  el("lang").click(); await nap(900);
  const hintEn = el("ask-hint").textContent;
  ck("英文提示里是 API key，且没有中文残留",
     /API key/.test(hintEn) && !/[\u4e00-\u9fff]/.test(hintEn), hintEn);
  el("lang").click(); await nap(900);

  console.log("");
  console.log("== 保存表单时的收尾话术也要跟着事实 ==");
  el("set-url").value = "http://127.0.0.1:8000/v1";
  el("set-model").value = "beta-model";
  el("set-remember").checked = false;
  el("set-key").value = "";
  el("settings").dispatchEvent(new w.Event("submit", { bubbles: true, cancelable: true }));
  await nap(900);
  ck("只填地址与模型、没给密钥 → 明说还缺密钥",
     /缺少/.test(el("set-msg").textContent) && /密钥/.test(el("set-msg").textContent),
     el("set-msg").textContent);

  console.log("");
  console.log("== 保存被拒时的话术也必须走同一张表（英文界面下不留中文） ==");
  el("lang").click(); await nap(900);                 // → en
  el("set-url").value = "http://example.com/v1";       // 远程 http，服务端必拒
  el("settings").dispatchEvent(new w.Event("submit", { bubbles: true, cancelable: true }));
  await nap(900);
  const msgEn = el("set-msg").textContent;
  ck("英文界面下被拒的话术是 Not saved:，且整句没有中文",
     /^Not saved:/.test(msgEn) && !/[\u4e00-\u9fff]/.test(msgEn), msgEn.slice(0, 90));
  const stRefused = await nf("/api/state").then((r) => r.json());
  ck("被拒的那次没有把地址写坏（state 里仍是本机服务）",
     stRefused.base_url === "http://127.0.0.1:8000/v1", stRefused.base_url);
  el("lang").click(); await nap(900);                  // → zh

  console.log("");
  console.log("== 把密钥装回去：所有话术必须一起翻正 ==");
  const restored = await patch({ api_key: "local", remember_key: true });
  await w.refreshState(); await nap(300);
  ck("config_ready 回到 true 且 missing_fields 为空",
     restored.ok === true && restored.config_ready === true
     && JSON.stringify(restored.missing_fields) === "[]", JSON.stringify(restored).slice(0, 160));
  ck("提问框开着，提示行不再是缺项话术",
     el("ask").disabled === false && !/缺少|没有配置/.test(el("ask-hint").textContent),
     el("ask-hint").textContent.slice(0, 60));
  ck("横幅报就绪", /就绪/.test(el("banner").textContent), el("banner").textContent.slice(0, 60));

  console.log("");
  console.log(`结论: ${pass} PASS / ${fail} FAIL   加载期错误 ${errs.length}${errs.length ? " → " + errs.slice(0, 3).join(" | ") : ""}`);
  console.log(`driver: jsdom ${require("jsdom/package.json").version} / node ${process.version} —— DOM 模拟，非 Chromium`);
  process.exit(fail ? 1 : 0);
})().catch((e) => { console.log("验证器异常:", e && e.message); process.exit(2); });
