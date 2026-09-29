const { JSDOM } = require("jsdom");
const path = require("path");
const FILE = process.env.PAGE;
const dom = new JSDOM(require("fs").readFileSync(FILE, "utf-8"));
const d = dom.window.document;
const LABEL = (el) => {
  const t = (el.getAttribute("aria-label") || el.textContent || "").replace(/\s+/g, " ").trim();
  return t.slice(0, 34);
};
function walk(node, depth, out) {
  for (const el of node.children || []) {
    const tag = el.tagName.toLowerCase();
    if (["script", "meta", "link", "head", "html", "body"].includes(tag)) { walk(el, depth, out); continue; }
    const id = el.id ? "#" + el.id : "";
    const cls = el.className && typeof el.className === "string"
      ? "." + el.className.trim().split(/\s+/).join(".") : "";
    const bits = [tag + id + cls];
    if (el.hasAttribute("hidden")) bits.push("[hidden]");
    if (el.matches("details")) bits.push("[open=" + el.hasAttribute("open") + "]");
    if (el.matches("input,select,button")) bits.push("(" + (el.type || tag) + (el.disabled ? ",disabled" : "") + ")");
    const text = LABEL(el);
    const isLeaf = !el.children.length;
    out.push("  ".repeat(depth) + bits.join(" ") + (isLeaf && text ? "  «" + text + "»" : ""));
    if (!isLeaf && depth < 4) walk(el, depth + 1, out);
    else if (!isLeaf) {
      const kids = el.querySelectorAll("*").length;
      out.push("  ".repeat(depth + 1) + `…${kids} 个子元素`);
    }
  }
}
const out = [];
walk(d.body, 0, out);
console.log(out.join("\n"));
console.log("\n控件计数：button=" + d.querySelectorAll("button").length
  + " input=" + d.querySelectorAll("input").length
  + " select=" + d.querySelectorAll("select").length
  + " option=" + d.querySelectorAll("option").length
  + " label=" + d.querySelectorAll("label").length
  + " 可交互合计=" + d.querySelectorAll("button,input,select").length);
