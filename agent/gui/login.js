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
    sub: "输入访问密码，进入数据分析工作台。",
    label: "密码",
    placeholder: "输入访问密码",
    label2: "再输入一次",
    placeholder2: "重复一次，确认无误",
    submit: "登录",
    submitSetup: "设置密码并进入",
    setupTitle: "设置访问密码",
    setupSub: "第一次打开请设置访问密码。设置之后，再次打开才需要输入它。",
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
    footSetup: "密码只保存在这台电脑上，不会上传。之后每次打开都要再输入一次。",
    waitTitle: "等待初始设置",
    waitSub: "这份服务还没有设置访问密码。请在运行它的那台机器上打开本页面，完成首次设置后，这里即可登录。",
    waitNote: "正在等待管理员设置密码…",
    liveRows: "行数",
    liveRate: "加速",
    liveTime: "耗时",
  },
  en: {
    pageTitle: "Sign in · Data Workbench",
    title: "Welcome back",
    sub: "Enter the access password to open the workbench.",
    label: "Password",
    placeholder: "Enter the password",
    label2: "Type it once more",
    placeholder2: "Repeat it to confirm",
    submit: "Sign in",
    submitSetup: "Set password and enter",
    setupTitle: "Set an access password",
    setupSub: "First open: choose an access password. The next open is the one that asks for it.",
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
    footSetup: "The password stays on this computer and is never uploaded. Every later visit asks for it again.",
    waitTitle: "Waiting for first-run setup",
    waitSub: "This server has no access password yet. Open this page on the machine running it, finish the first-run setup, and signing in works here.",
    waitNote: "Waiting for the operator to set the password…",
    liveRows: "Rows",
    liveRate: "Speed",
    liveTime: "Elapsed",
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

let mode = "setup";        // signin | setup | waiting | open
let busy = false;

function applyStrings() {
  const s = STRINGS[lang];
  const settingUp = mode === "setup";
  document.title = settingUp ? s.setupTitle : s.pageTitle;
  document.documentElement.lang = lang;
  document.getElementById("t-title").textContent = settingUp ? s.setupTitle : s.title;
  document.getElementById("t-sub").textContent = settingUp ? s.setupSub : s.sub;
  document.getElementById("t-label").textContent = settingUp ? s.setupLabel : s.label;
  document.getElementById("t-label2").textContent = s.label2;
  document.getElementById("t-foot").textContent = settingUp ? s.footSetup : s.foot;
  document.getElementById("t-wait-title").textContent = s.waitTitle;
  document.getElementById("t-wait-sub").textContent = s.waitSub;
  document.getElementById("t-wait-note").textContent = s.waitNote;
  input.placeholder = settingUp ? s.setupPlaceholder : s.placeholder;
  confirmInput.placeholder = s.placeholder2;
  if (!busy) submit.textContent = settingUp ? s.submitSetup : s.submit;
  langButton.textContent = lang === "zh" ? "English" : "中文";
  ["liveRows", "liveRate", "liveTime"].forEach((key, index) => {
    const node = document.getElementById("live-k" + index);
    if (node) node.textContent = s[key];
  });
  document.querySelectorAll("[data-fig]").forEach((node) => {
    const key = ["liveRows", "liveRate", "liveTime"][Number(node.dataset.fig)] || "liveRows";
    node.textContent = s[key];
  });
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
  let gate = null;
  try {
    const response = await fetch("/api/gate");
    gate = response.ok ? await response.json() : null;
  } catch (err) { /* the page itself loaded; show first-run setup and let a later submit fail honestly */ }
  if (gate && gate.mode === "open") {
    window.location.replace("/");
    return;
  }
  if (gate && gate.mode === "setup" && gate.remote) {
    setMode("waiting");
    return;
  }
  // No answer yet still means first open: ask them to choose a password.
  // A stored password is the only thing that turns the next open into sign-in.
  setMode(gate && gate.mode === "signin" ? "signin" : "setup");
  input.focus();
}

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
startMeters();

function startMeters() {
  const reduce = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  function format(kind, value) {
    const locale = lang === "zh" ? "zh-CN" : "en-US";
    if (kind === "rows") return Math.round(value).toLocaleString(locale);
    if (kind === "rate") return value.toFixed(2) + "×";
    return value.toFixed(2) + "s";
  }

  function clamp(kind, value) {
    if (kind === "rows") return Math.max(180000, value);
    if (kind === "rate") return Math.min(8, Math.max(1.05, value));
    return Math.min(9.5, Math.max(0.35, value));
  }

  function step(kind, value) {
    const dir = Math.random() < 0.5 ? -1 : 1;
    if (kind === "rows") return value + dir * (800 + Math.floor(Math.random() * 6400));
    if (kind === "rate") return value + dir * (0.05 + Math.random() * 0.22);
    return value + dir * (0.04 + Math.random() * 0.28);
  }

  const items = [
    { el: document.getElementById("live-n0"), kind: "rows", value: 12840221 },
    { el: document.getElementById("live-n1"), kind: "rate", value: 3.88 },
    { el: document.getElementById("live-n2"), kind: "time", value: 2.63 },
  ];
  document.querySelectorAll("[data-meter]").forEach((el) => {
    const kind = el.dataset.meter;
    const value = kind === "rows" ? 2500000 + Math.random() * 14000000
      : kind === "rate" ? 1.3 + Math.random() * 4.2
      : 0.7 + Math.random() * 3.6;
    items.push({ el, kind, value });
  });

  items.forEach((item) => {
    item.el.textContent = format(item.kind, item.value);
    if (reduce) return;
    const again = () => {
      item.value = clamp(item.kind, step(item.kind, item.value));
      item.el.textContent = format(item.kind, item.value);
      setTimeout(again, 650 + Math.random() * 2400);
    };
    setTimeout(again, 400 + Math.random() * 2200);
  });
}

function startAtmosphere() {
  const canvas = document.getElementById("dust");
  const reduce = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
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

  if (reduce) {
    placeGlow();
    return;
  }

  const ctx = canvas.getContext("2d");
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
  resize();

  function frame() {
    if (!document.hidden) {
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
    }
    requestAnimationFrame(frame);
  }
  requestAnimationFrame(frame);
}
