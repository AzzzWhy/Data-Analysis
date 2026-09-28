/* The gate page. One page, three doors, chosen by /api/gate:
     signin -- the gate has a password, enter it
     setup  -- first run from the server's own machine, choose one
     setup seen from a remote machine -- a waiting note, no form, no claim
   The password is POSTed once and never appears in a URL; what comes back is a
   session cookie. Everything this page needs lives in this file. */

"use strict";

const STRINGS = {
  zh: {
    pageTitle: "登录 · Data Workbench",
    title: "输入访问密码",
    sub: "这份工作台由访问密码保护，密码来自启动这份服务的一方。",
    label: "访问密码",
    placeholder: "输入访问密码",
    label2: "再输入一次",
    placeholder2: "重复一次，确认无误",
    submit: "进入工作台",
    submitSetup: "保存密码并进入",
    checking: "验证中…",
    saving: "保存中…",
    wrong: "密码不正确，请重试。",
    mismatch: "两次输入不一致。",
    short: "密码至少 8 位，且不能全是空白字符。",
    offline: "连不上服务，请检查网络后重试。",
    offlineCode: "连不上服务（HTTP {code}），请稍后重试。",
    ok: "已解锁，正在进入…",
    saved: "已保存，正在进入…",
    foot: "密码只在请求体中传输 · 会话保留 7 天 · 服务重启后失效",
    footSetup: "密码以加盐 PBKDF2 摘要存储在本机 · 之后每次访问都需要输入",
    waitTitle: "等待初始设置",
    waitSub: "这份服务还没有设置访问密码。请在运行它的那台机器上打开本页面，完成首次设置后，这里即可登录。",
    waitNote: "正在等待管理员设置密码…",
    setupTitle: "设置访问密码",
    setupSub: "首次使用：为这份工作台设置一个访问密码。之后每次打开都需要输入它。",
  },
  en: {
    pageTitle: "Sign in · Data Workbench",
    title: "Enter the access password",
    sub: "This workbench is gated; the password comes from whoever started the server.",
    label: "Access password",
    placeholder: "Enter the password",
    label2: "Type it once more",
    placeholder2: "Repeat it to confirm",
    submit: "Enter the workbench",
    submitSetup: "Save and enter",
    checking: "Checking…",
    saving: "Saving…",
    wrong: "That password is not right. Try again.",
    mismatch: "The two entries do not match.",
    short: "Use at least 8 characters, not all whitespace.",
    offline: "Cannot reach the server. Check the network and retry.",
    offlineCode: "Cannot reach the server (HTTP {code}). Retry in a moment.",
    ok: "Unlocked, entering…",
    saved: "Saved, entering…",
    foot: "Sent in the request body only · the session lasts 7 days · a restart clears it",
    footSetup: "Stored locally as a salted PBKDF2 digest · asked for on every future visit",
    waitTitle: "Waiting for first-run setup",
    waitSub: "This server has no access password yet. Open this page on the machine running it, finish the first-run setup, and signing in works here.",
    waitNote: "Waiting for the operator to set the password…",
    setupTitle: "Set the access password",
    setupSub: "First use: choose an access password for this workbench. Every future visit will ask for it.",
  },
};

let lang = (navigator.language || "zh").toLowerCase().startsWith("zh") ? "zh" : "en";

const panel = document.getElementById("gate-panel");
const form = document.getElementById("gate-form");
const waiting = document.getElementById("gate-waiting");
const input = document.getElementById("password");
const confirmInput = document.getElementById("confirm");
const msg = document.getElementById("gate-msg");
const submit = document.getElementById("gate-submit");
const langButton = document.getElementById("lang");

let mode = "signin";       // signin | setup | waiting | open
let busy = false;

function applyStrings() {
  const s = STRINGS[lang];
  document.title = s.pageTitle;
  document.documentElement.lang = lang;
  document.getElementById("t-title").textContent = mode === "setup" ? s.setupTitle : s.title;
  document.getElementById("t-sub").textContent = mode === "setup" ? s.setupSub : s.sub;
  document.getElementById("t-label").textContent = s.label;
  document.getElementById("t-label2").textContent = s.label2;
  document.getElementById("t-foot").textContent = mode === "setup" ? s.footSetup : s.foot;
  document.getElementById("t-wait-title").textContent = s.waitTitle;
  document.getElementById("t-wait-sub").textContent = s.waitSub;
  document.getElementById("t-wait-note").textContent = s.waitNote;
  input.placeholder = s.placeholder;
  confirmInput.placeholder = s.placeholder2;
  if (!busy) submit.textContent = mode === "setup" ? s.submitSetup : s.submit;
  langButton.textContent = lang === "zh" ? "English" : "中文";
}

langButton.addEventListener("click", () => {
  lang = lang === "zh" ? "en" : "zh";
  applyStrings();
  if (mode !== "waiting") input.focus();
});

function showError(text, armed) {
  msg.textContent = text;
  msg.hidden = false;
  if (armed) {
    panel.dataset.armed = "1";
    panel.dataset.nudge = "1";
    setTimeout(() => { delete panel.dataset.nudge; }, 340);
  }
}

function clearError() {
  msg.hidden = true;
  delete panel.dataset.armed;
}

function nudge() {
  panel.dataset.nudge = "1";
  setTimeout(() => { delete panel.dataset.nudge; }, 340);
}

function setMode(next) {
  mode = next;
  if (next === "waiting") {
    form.hidden = true;
    waiting.hidden = false;
  } else {
    form.hidden = false;
    waiting.hidden = true;
    const setup = next === "setup";
    document.getElementById("t-label2").hidden = !setup;
    confirmInput.hidden = !setup;
    confirmInput.required = setup;
    input.setAttribute("autocomplete", setup ? "new-password" : "current-password");
  }
  applyStrings();
}

async function boot() {
  let gate = null;
  try {
    const response = await fetch("/api/gate");
    gate = response.ok ? await response.json() : null;
  } catch (err) { /* the page itself loaded; show the sign-in form and let it fail honestly */ }
  if (gate && gate.mode === "open") {
    window.location.replace("/");
    return;
  }
  if (gate && gate.mode === "setup" && gate.remote) {
    setMode("waiting");
    return;
  }
  setMode(gate && gate.mode === "setup" ? "setup" : "signin");
  input.focus();
}

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  if (busy) return;
  clearError();
  const password = input.value;
  if (mode === "setup") {
    if (password.length < 8 || !password.trim()) { showError(STRINGS[lang].short, true); input.focus(); return; }
    if (password !== confirmInput.value) { showError(STRINGS[lang].mismatch, true); confirmInput.focus(); nudge(); return; }
  } else if (!password) {
    input.focus();
    return;
  }
  busy = true;
  let succeeded = false;
  submit.disabled = true;
  submit.dataset.busy = "1";
  submit.textContent = mode === "setup" ? STRINGS[lang].saving : STRINGS[lang].checking;
  const endpoint = mode === "setup" ? "/api/setup" : "/api/login";
  try {
    const response = await fetch(endpoint, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ password }),
    });
    if (response.ok) {
      succeeded = true;                 // keep the unlocked state on the button while the page turns
      submit.dataset.busy = "";
      submit.dataset.ok = "1";
      submit.textContent = mode === "setup" ? STRINGS[lang].saved : STRINGS[lang].ok;
      // replace(): the sign-in page is a door, not a place -- Back should skip it.
      window.location.replace("/");
      return;
    }
    const data = await response.json().catch(() => ({}));
    if (response.status === 401) {
      showError(STRINGS[lang].wrong, true);
      input.value = "";
      if (mode === "setup") confirmInput.value = "";
      input.focus();
    } else if (response.status === 409 && mode === "signin") {
      // a password got set somewhere else while this tab was open
      setMode("setup");
      showError((data && data.error) || STRINGS[lang].offline, false);
    } else {
      showError((data && data.error) || STRINGS[lang].offlineCode.replace("{code}", response.status), false);
      input.focus();
    }
  } catch (err) {
    showError(STRINGS[lang].offline, false);
    input.focus();
  } finally {
    busy = false;
    if (!succeeded) {
      submit.disabled = false;
      delete submit.dataset.busy;
      submit.textContent = mode === "setup" ? STRINGS[lang].submitSetup : STRINGS[lang].submit;
    }
  }
});

applyStrings();
boot();
