/* The gate page: its one job is to trade a token for a session cookie, and to say
   exactly what happened when that fails. Everything it needs lives in this file;
   the workbench's app.js stays behind the gate. */

"use strict";

const STRINGS = {
  zh: {
    pageTitle: "登录 · Data Workbench",
    title: "输入访问口令",
    sub: "这份工作台由访问口令保护，口令来自启动这份服务的一方。",
    label: "访问口令",
    placeholder: "粘贴访问口令",
    submit: "进入工作台",
    checking: "验证中…",
    wrong: "口令不正确，请重试。",
    offline: "连不上服务，请检查网络后重试。",
    foot: "口令只在请求体中传输 · 会话保留 7 天 · 服务重启后失效",
  },
  en: {
    pageTitle: "Sign in · Data Workbench",
    title: "Enter the access token",
    sub: "This workbench is gated; the token comes from whoever started the server.",
    label: "Access token",
    placeholder: "Paste the access token",
    submit: "Enter the workbench",
    checking: "Checking…",
    wrong: "That token is not right. Try again.",
    offline: "Cannot reach the server. Check the network and retry.",
    foot: "Sent in the request body only · the session lasts 7 days · a server restart clears it",
  },
};

let lang = (navigator.language || "zh").toLowerCase().startsWith("zh") ? "zh" : "en";

const panel = document.getElementById("gate-panel");
const form = document.getElementById("gate-form");
const input = document.getElementById("token");
const msg = document.getElementById("gate-msg");
const submit = document.getElementById("gate-submit");
const langButton = document.getElementById("lang");

function applyStrings() {
  const s = STRINGS[lang];
  document.title = s.pageTitle;
  document.documentElement.lang = lang;
  document.getElementById("t-title").textContent = s.title;
  document.getElementById("t-sub").textContent = s.sub;
  document.getElementById("t-label").textContent = s.label;
  document.getElementById("t-foot").textContent = s.foot;
  input.placeholder = s.placeholder;
  if (!submit.disabled) submit.textContent = s.submit;
  langButton.textContent = lang === "zh" ? "English" : "中文";
}

langButton.addEventListener("click", () => {
  lang = lang === "zh" ? "en" : "zh";
  applyStrings();
  input.focus();
});

function nudge() {
  panel.dataset.nudge = "1";
  setTimeout(() => { delete panel.dataset.nudge; }, 240);
}

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  const token = input.value.trim();
  if (!token) { input.focus(); return; }
  msg.hidden = true;
  submit.disabled = true;
  submit.textContent = STRINGS[lang].checking;
  try {
    const response = await fetch("/api/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ token }),
    });
    if (response.ok) {
      // replace(): the sign-in page is a door, not a place -- Back should skip it.
      window.location.replace("/");
      return;
    }
    if (response.status === 401) {
      msg.textContent = STRINGS[lang].wrong;
      input.value = "";
      nudge();
    } else {
      msg.textContent = `${STRINGS[lang].offline} (HTTP ${response.status})`;
    }
    input.focus();
  } catch (err) {
    msg.textContent = STRINGS[lang].offline;
    input.focus();
  } finally {
    submit.disabled = false;
    submit.textContent = STRINGS[lang].submit;
  }
});

applyStrings();
input.focus();
