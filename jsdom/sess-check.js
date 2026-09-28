const http = require("http");
const BASE = process.env.BASE;
const CSV = process.env.CSV;
const nf = (u, o) => new Promise((res, rej) => {
  const U = new URL(u, BASE);
  const headers = Object.assign({}, (o && o.headers) || {});
  const body = (o && o.body) || null;
  if (body) headers["Content-Length"] = Buffer.byteLength(body);
  const r = http.request(U, { method: (o && o.method) || "GET", headers }, (x) => {
    let b = ""; x.setEncoding("utf8"); x.on("data", (c) => (b += c));
    x.on("end", () => res({ status: x.statusCode, json: () => Promise.resolve(JSON.parse(b || "{}")), text: () => b }));
  });
  r.on("error", rej); if (body) r.write(body); r.end();
});
const sse = async (jid) => {
  const t = await nf("/api/events?job=" + encodeURIComponent(jid)).then((r) => r.text());
  let name = null, out = [];
  for (const line of t.split("\n")) {
    if (line.startsWith("event: ")) name = line.slice(7);
    else if (line.startsWith("data: ") && name) out.push([name, JSON.parse(line.slice(6))]);
  }
  return out;
};
(async () => {
  for (const step of ["open", "analyze", "list"]) {
    const args = { operation: step, file_path: CSV };
    if (step === "open") args.goal = "按 region 比较 revenue";
    if (step === "analyze") { args.op = "summary"; }
    const j = await nf("/api/run", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ tool: "dataset_session", args }) }).then((r) => r.json());
    const frames = await sse(j.job_id);
    const tr = frames.find(([n]) => n === "tool_result");
    const res = tr && tr[1] && tr[1].result;
    console.log(step.padEnd(8), "→", JSON.stringify({
      ok: res && res.success !== false ? true : false,
      sid: res && res.session_id, engine: res && res.engine, rows: res && res.rows_scanned,
      resident_mb: res && res.resident_mb, warm: res && res.execution_decision && res.execution_decision.observed && res.execution_decision.observed.phase,
      error: res && res.error || (tr && tr[1] && tr[1].chip && tr[1].chip.note) }).slice(0, 260));
    const sessFrame = frames.find(([n]) => n === "session");
    if (sessFrame) console.log("         session 帧:", JSON.stringify(sessFrame[1]).slice(0, 220));
  }
  const st = await nf("/api/state").then((r) => r.json());
  console.log("最后 /api/state.session:", JSON.stringify(st.session).slice(0, 300));
})();
