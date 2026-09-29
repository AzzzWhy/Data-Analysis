/* The gate page. One page, three doors, chosen by /api/gate:
     setup  -- first open, from the server's own machine: choose a password
     signin -- every later open: the password is required
     setup seen from a remote machine -- a waiting note, no form, no claim
   The password is POSTed once and never appears in a URL; what comes back is a
   session cookie. Everything this page needs lives in this file. */

"use strict";

const STRINGS = {
  zh: {
    pageTitle: "登录 · Data Workbench",
    title: "欢迎回来",
    sub: "输入当前所连后端的访问密码，进入数据分析工作台。",
    label: "密码",
    placeholder: "输入访问密码",
    label2: "再输入一次",
    placeholder2: "重复一次，确认无误",
    submit: "登录",
    submitSetup: "设置密码并进入",
    setupTitle: "设置访问密码",
    setupSub: "当前所连后端尚未设置访问密码，请先完成设置。此密码不是 SSH 密码。",
    setupLabel: "访问密码",
    setupPlaceholder: "至少 8 位",
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
    footSetup: "密码提交至当前所连后端，仅保存加盐哈希；本机与远程后端的访问密码相互独立。",
    waitTitle: "等待初始设置",
    waitSub: "这份服务还没有设置访问密码。请在运行它的那台机器上打开本页面，完成首次设置后，这里即可登录。",
    waitNote: "正在等待管理员设置密码…",
    unavailableTitle: "暂时无法确认连接",
    unavailableSub: "无法获取当前后端的登录状态。请检查计算连接或重试，不会将连接故障视为首次设置。",
    retry: "重试连接",
    productSub: "GPU 加速与数据分析",
    liveRows: "行数",
    liveRate: "加速比",
    liveTime: "耗时",
    metricsNote: "此页不执行分析；真实指标在任务完成后于工作台展示。",
  },
  en: {
    pageTitle: "Sign in · Data Workbench",
    title: "Welcome back",
    sub: "Enter the connected backend's access password to open the workbench.",
    label: "Password",
    placeholder: "Enter the password",
    label2: "Type it once more",
    placeholder2: "Repeat it to confirm",
    submit: "Sign in",
    submitSetup: "Set password and enter",
    setupTitle: "Set an access password",
    setupSub: "The connected backend has no access password yet. Set one to continue. This is not the SSH password.",
    setupLabel: "Access password",
    setupPlaceholder: "At least 8 characters",
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
    footSetup: "Sent to the connected backend and stored as a salted hash. Local and remote backends have separate access passwords.",
    waitTitle: "Waiting for first-run setup",
    waitSub: "This server has no access password yet. Open this page on the machine running it, finish the first-run setup, and signing in works here.",
    waitNote: "Waiting for the operator to set the password…",
    unavailableTitle: "Connection not confirmed",
    unavailableSub: "Cannot read the backend's sign-in state. Check the compute connection or retry. A connection failure does not mean first-run setup.",
    retry: "Retry connection",
    productSub: "GPU acceleration & data analysis",
    liveRows: "Rows",
    liveRate: "Speedup",
    liveTime: "Elapsed",
    metricsNote: "No analysis runs on this page. Actual metrics appear in the workbench after a task finishes.",
  },
};

let lang = (navigator.language || "zh").toLowerCase().startsWith("zh") ? "zh" : "en";

const panel = document.getElementById("gate-panel");
const form = document.getElementById("gate-form");
const waiting = document.getElementById("gate-waiting");
const input = document.getElementById("password");
const confirmInput = document.getElementById("confirm");
const confirmField = document.getElementById("confirm-field");
const msg = document.getElementById("gate-msg");
const submit = document.getElementById("gate-submit");
const langButton = document.getElementById("lang");
const retryButton = document.getElementById("gate-retry");

let mode = "loading";      // signin | setup | waiting | unavailable | open
let busy = false;

function applyStrings() {
  const s = STRINGS[lang];
  const settingUp = mode === "setup";
  const unavailable = mode === "unavailable";
  document.title = settingUp ? s.setupTitle : s.pageTitle;
  document.documentElement.lang = lang;
  document.getElementById("t-title").textContent = settingUp ? s.setupTitle : s.title;
  document.getElementById("t-sub").textContent = settingUp ? s.setupSub : s.sub;
  document.getElementById("t-label").textContent = settingUp ? s.setupLabel : s.label;
  document.getElementById("t-label2").textContent = s.label2;
  document.getElementById("t-foot").textContent = settingUp ? s.footSetup : s.foot;
  document.getElementById("t-wait-title").textContent = unavailable ? s.unavailableTitle : s.waitTitle;
  document.getElementById("t-wait-sub").textContent = unavailable ? s.unavailableSub : s.waitSub;
  document.getElementById("t-wait-note").textContent = s.waitNote;
  document.getElementById("product-sub").textContent = s.productSub;
  document.getElementById("metrics-note").textContent = s.metricsNote;
  retryButton.textContent = s.retry;
  input.placeholder = settingUp ? s.setupPlaceholder : s.placeholder;
  confirmInput.placeholder = s.placeholder2;
  if (!busy) submit.textContent = settingUp ? s.submitSetup : s.submit;
  langButton.textContent = lang === "zh" ? "English" : "中文";
  ["liveRows", "liveRate", "liveTime"].forEach((key, index) => {
    const node = document.getElementById("live-k" + index);
    if (node) node.textContent = s[key];
  });
}

langButton.addEventListener("click", () => {
  lang = lang === "zh" ? "en" : "zh";
  applyStrings();
  if (mode === "signin" || mode === "setup") input.focus();
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
  if (next === "waiting" || next === "unavailable") {
    form.hidden = true;
    waiting.hidden = false;
    retryButton.hidden = next !== "unavailable";
    document.getElementById("waiting-note").hidden = next === "unavailable";
    applyStrings();
    return;
  }
  const settingUp = next === "setup";
  form.hidden = false;
  waiting.hidden = true;
  confirmField.hidden = !settingUp;
  confirmInput.hidden = !settingUp;
  confirmInput.required = settingUp;
  document.getElementById("t-label2").hidden = !settingUp;
  input.setAttribute("autocomplete", settingUp ? "new-password" : "current-password");
  applyStrings();
}

async function boot() {
  retryButton.disabled = true;
  let gate = null;
  try {
    const response = await fetch("/api/gate");
    gate = response.ok ? await response.json() : null;
  } catch (err) { /* A failed request does not tell us whether a password exists. */ }
  retryButton.disabled = false;
  if (gate && gate.mode === "open") {
    window.location.replace("/");
    return;
  }
  if (gate && gate.mode === "setup" && gate.remote) {
    setMode("waiting");
    return;
  }
  if (!gate || !["signin", "setup"].includes(gate.mode)) {
    setMode("unavailable");
    return;
  }
  setMode(gate.mode);
  input.focus();
}

retryButton.addEventListener("click", boot);

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  if (busy) return;
  clearError();
  const password = input.value;
  const settingUp = mode === "setup";
  if (settingUp) {
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
  submit.textContent = settingUp ? STRINGS[lang].saving : STRINGS[lang].checking;
  const endpoint = settingUp ? "/api/setup" : "/api/login";
  try {
    const response = await fetch(endpoint, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ password }),
    });
    if (response.ok) {
      succeeded = true;
      submit.dataset.busy = "";
      submit.dataset.ok = "1";
      submit.textContent = settingUp ? STRINGS[lang].saved : STRINGS[lang].ok;
      window.location.replace("/");
      return;
    }
    const data = await response.json().catch(() => ({}));
    if (response.status === 401) {
      showError(STRINGS[lang].wrong, true);
      input.value = "";
      confirmInput.value = "";
      input.focus();
    } else if (response.status === 409 && !settingUp) {
      setMode("setup");
      showError((data && data.error) || STRINGS[lang].offline, false);
      input.focus();
    } else if (response.status === 409 && settingUp) {
      setMode("signin");
      showError((data && data.error) || STRINGS[lang].wrong, false);
      input.focus();
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

panel.addEventListener("animationend", (event) => {
  if (event.animationName === "card-in") panel.dataset.entered = "1";
});

applyStrings();
boot();
startAtmosphere();

function startAtmosphere() {
  const canvas = document.getElementById("dust");
  const motion = window.matchMedia("(prefers-reduced-motion: reduce)");
  const root = document.documentElement;
  let width = window.innerWidth;
  let height = window.innerHeight;
  let pointerX = width * 0.5;
  let pointerY = height * 0.42;
  let glowX = pointerX;
  let glowY = pointerY;
  let homeX = pointerX;
  let homeY = pointerY;
  let pointerInside = false;

  function placeGlow() {
    root.style.setProperty("--mx", glowX + "px");
    root.style.setProperty("--my", glowY + "px");
  }

  let ctx = null;
  let frameId = null;
  const specks = [];
  const COUNT = 84;

  function seed() {
    specks.length = 0;
    for (let i = 0; i < COUNT; i++) {
      specks.push({
        x: Math.random() * width,
        y: Math.random() * height,
        r: Math.random() < 0.78 ? 0.8 + Math.random() * 0.9 : 1.8 + Math.random() * 1.4,
        a: 0.16 + Math.random() * 0.28,
        driftX: (Math.random() - 0.5) * 0.12,
        driftY: -0.04 - Math.random() * 0.1,
        vx: 0,
        vy: 0,
        cool: Math.random() < 0.55,
      });
    }
  }

  function resize() {
    width = window.innerWidth;
    height = window.innerHeight;
    homeX = width * 0.5;
    homeY = height * 0.42;
    if (!pointerInside) {
      pointerX = homeX;
      pointerY = homeY;
    }
    if (!ctx) return;
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    canvas.width = Math.floor(width * dpr);
    canvas.height = Math.floor(height * dpr);
    canvas.style.width = width + "px";
    canvas.style.height = height + "px";
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    if (!specks.length) seed();
  }

  window.addEventListener("pointermove", (event) => {
    pointerInside = true;
    pointerX = event.clientX;
    pointerY = event.clientY;
  }, { passive: true });
  window.addEventListener("pointerleave", () => {
    pointerInside = false;
    pointerX = homeX;
    pointerY = homeY;
  });
  window.addEventListener("resize", resize);
  function frame() {
    frameId = null;
    if (!document.hidden && !motion.matches) {
      const aimX = pointerInside ? pointerX : homeX;
      const aimY = pointerInside ? pointerY : homeY;
      glowX += (aimX - glowX) * 0.06;
      glowY += (aimY - glowY) * 0.06;
      placeGlow();

      ctx.clearRect(0, 0, width, height);
      const reach = 128;
      for (const speck of specks) {
        const dx = speck.x - pointerX;
        const dy = speck.y - pointerY;
        const dist2 = dx * dx + dy * dy;
        if (pointerInside && dist2 < reach * reach && dist2 > 0.25) {
          const dist = Math.sqrt(dist2);
          const push = (1 - dist / reach) * 0.42;
          speck.vx += (dx / dist) * push;
          speck.vy += (dy / dist) * push;
        }
        speck.vx *= 0.94;
        speck.vy *= 0.94;
        const speed = Math.hypot(speck.vx, speck.vy);
        if (speed > 1.15) {
          speck.vx = speck.vx / speed * 1.15;
          speck.vy = speck.vy / speed * 1.15;
        }
        speck.x += speck.vx + speck.driftX;
        speck.y += speck.vy + speck.driftY;
        if (speck.x < -8) speck.x = width + 8;
        if (speck.x > width + 8) speck.x = -8;
        if (speck.y < -8) speck.y = height + 8;
        if (speck.y > height + 8) speck.y = -8;
        ctx.beginPath();
        ctx.fillStyle = speck.cool
          ? "rgba(176, 190, 224, " + speck.a + ")"
          : "rgba(168, 160, 206, " + speck.a + ")";
        ctx.arc(speck.x, speck.y, speck.r, 0, Math.PI * 2);
        ctx.fill();
      }
      frameId = requestAnimationFrame(frame);
    }
  }

  function syncMotion() {
    const paused = document.hidden || motion.matches;
    root.style.setProperty("--atmosphere-play", paused ? "paused" : "running");
    if (paused) {
      if (frameId !== null) cancelAnimationFrame(frameId);
      frameId = null;
      placeGlow();
      return;
    }
    if (!ctx) {
      ctx = canvas.getContext("2d");
      if (!ctx) return;
      resize();
    }
    if (frameId === null) frameId = requestAnimationFrame(frame);
  }
  document.addEventListener("visibilitychange", syncMotion);
  motion.addEventListener?.("change", syncMotion);
  syncMotion();
}
