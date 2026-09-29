"use strict";
// A local connection dialog. SSH credentials are handled by the system ssh client,
// never by this page or a URL containing a password.
(() => {
  const header = document.querySelector(".topbar-meta");
  if (!header) return;
  const trigger = document.createElement("button");
  trigger.type = "button";
  trigger.className = "secondary small backend-connection-trigger";
  trigger.id = "backend-connection-trigger";
  header.insertBefore(trigger, header.firstChild);

  const backdrop = document.createElement("div");
  backdrop.className = "backend-dialog-backdrop";
  backdrop.hidden = true;
  backdrop.innerHTML = `
    <section class="backend-dialog" role="dialog" aria-modal="true" aria-labelledby="backend-dialog-title">
      <div class="backend-dialog-head">
        <div><p class="backend-eyebrow">COMPUTE CONNECTION</p><h2 id="backend-dialog-title"></h2></div>
        <button type="button" id="backend-dialog-close" class="text-button" aria-label="Close">×</button>
      </div>
      <p id="backend-dialog-intro" class="backend-dialog-copy"></p>
      <div class="backend-current">
        <span id="backend-current-label"></span><strong id="backend-current-value"></strong>
      </div>
      <div class="backend-dialog-actions">
        <button type="button" id="backend-use-local" class="secondary"></button>
        <button type="button" id="backend-use-remote" class="secondary"></button>
      </div>
      <div class="backend-dialog-divider"></div>
      <label class="backend-field" for="backend-ssh-url"><span id="backend-ssh-label"></span>
        <input id="backend-ssh-url" type="text" inputmode="url" autocomplete="off"
          spellcheck="false" placeholder="ssh://Developer@host:6060"></label>
      <label class="backend-field" for="backend-port"><span id="backend-port-label"></span>
        <input id="backend-port" type="number" min="1" max="65535" value="8765"></label>
      <p id="backend-ssh-note" class="backend-dialog-copy"></p>
      <div class="backend-dialog-actions">
        <button type="button" id="backend-connect" class="primary"></button>
        <button type="button" id="backend-disconnect" class="secondary"></button>
      </div>
      <p id="backend-dialog-message" class="backend-dialog-message" role="status" aria-live="polite"></p>
    </section>`;
  document.body.appendChild(backdrop);
  const $ = id => document.getElementById(id);
  const close = $("backend-dialog-close");
  const local = $("backend-use-local");
  const remote = $("backend-use-remote");
  const connect = $("backend-connect");
  const disconnect = $("backend-disconnect");
  const sshUrl = $("backend-ssh-url");
  const port = $("backend-port");
  const message = $("backend-dialog-message");
  let info = null;
  let busy = false;
  const s = () => document.documentElement.lang === "en" ? {
    trigger: "Compute connection", title: "Compute connection",
    intro: "Choose where analysis runs. The data stays on the selected machine.",
    current: "Current", thisPc: "This PC", remote: "Remote SSH",
    connecting: "SSH connecting…", unavailable: "Remote unavailable",
    incompatible: "Remote version is incompatible", disconnected: "Not connected",
    useLocal: "Use this PC", useRemote: "Use remote",
    sshLabel: "SSH address", portLabel: "Backend port on remote machine",
    note: "Use ssh://user@host:port. Enter the SSH password in the system SSH window, not here. The address is kept only for this running gateway.",
    connect: "Connect SSH", disconnect: "Disconnect SSH",
    waiting: "Waiting for SSH authentication and backend… Check the SSH window.",
    timeout: "Still waiting. Check the SSH window, then press Use remote.",
    failed: "Connection failed", switchFailed: "Could not switch backend",
  } : {
    trigger: "计算连接", title: "计算连接",
    intro: "选择分析在哪台机器运行。数据仍留在所选机器上。",
    current: "当前", thisPc: "本机", remote: "远程 SSH",
    connecting: "SSH 连接中…", unavailable: "远程不可用",
    incompatible: "远端版本不兼容", disconnected: "尚未连接",
    useLocal: "使用本机", useRemote: "使用远程",
    sshLabel: "SSH 地址", portLabel: "远程机器上的后端端口",
    note: "格式：ssh://用户名@主机:端口。SSH 密码请在系统弹出的 SSH 窗口输入，别填在这里；地址仅在本次网关运行期间保留。",
    connect: "连接 SSH", disconnect: "断开 SSH",
    waiting: "等待 SSH 验证和后端响应…请查看弹出的 SSH 窗口。",
    timeout: "仍在等待。检查 SSH 窗口后，可以点“使用远程”。",
    failed: "连接失败", switchFailed: "切换失败",
  };
  function render() {
    const t = s();
    trigger.textContent = `${t.trigger} · ${info?.mode === "remote" ? t.remote : t.thisPc}`;
    $("backend-dialog-title").textContent = t.title;
    $("backend-dialog-intro").textContent = t.intro;
    $("backend-current-label").textContent = t.current;
    const status = info?.status === "connected" ? t.remote
      : info?.status === "connecting" ? t.connecting
      : info?.status === "incompatible" ? t.incompatible
      : info?.status === "disconnected" ? t.disconnected : t.unavailable;
    $("backend-current-value").textContent = `${info?.mode === "remote" ? t.remote : t.thisPc} · ${status}`;
    local.textContent = t.useLocal;
    remote.textContent = t.useRemote;
    $("backend-ssh-label").textContent = t.sshLabel;
    $("backend-port-label").textContent = t.portLabel;
    $("backend-ssh-note").textContent = t.note;
    connect.textContent = t.connect;
    disconnect.textContent = t.disconnect;
    local.disabled = busy || info?.mode === "local";
    remote.disabled = busy || info?.status !== "connected" || info?.mode === "remote";
    connect.disabled = busy;
    disconnect.disabled = busy || !info?.ssh_managed;
    const reset = $("reset-all");
    if (reset) reset.disabled = info?.mode === "remote";
  }
  async function refresh() {
    const response = await fetch("/api/backend");
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    info = await response.json();
    if (info.ssh_url && !sshUrl.value) sshUrl.value = info.ssh_url;
    if (info.backend_port && !port.dataset.edited) port.value = String(info.backend_port);
    render();
    return info;
  }
  async function post(path, body = {}) {
    const response = await fetch(path, {method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify(body)});
    const data = await response.json();
    if (!response.ok || !data.ok) throw new Error(data.error || s().failed);
    return data;
  }
  function showMessage(value) { message.textContent = value; }
  async function run(action) {
    busy = true;
    showMessage("");
    render();
    try { await action(); }
    catch (error) { showMessage(String(error.message || error)); }
    finally { busy = false; await refresh().catch(() => render()); }
  }
  trigger.addEventListener("click", async () => {
    backdrop.hidden = false;
    showMessage("");
    await refresh().catch(error => showMessage(String(error.message || error)));
    sshUrl.focus();
  });
  close.addEventListener("click", () => { backdrop.hidden = true; trigger.focus(); });
  backdrop.addEventListener("click", event => {
    if (event.target === backdrop) close.click();
  });
  document.addEventListener("keydown", event => {
    if (event.key === "Escape" && !backdrop.hidden) {
      event.stopImmediatePropagation();
      close.click();
    }
  }, true);
  local.addEventListener("click", () => run(async () => {
    await post("/api/backend", {mode: "local"});
    location.reload();
  }));
  remote.addEventListener("click", () => run(async () => {
    await post("/api/backend", {mode: "remote"});
    location.reload();
  }));
  connect.addEventListener("click", () => run(async () => {
    await post("/api/backend/connection", {ssh_url: sshUrl.value.trim(), backend_port: port.value});
    showMessage(s().waiting);
    for (let attempt = 0; attempt < 60; attempt++) {
      await new Promise(resolve => setTimeout(resolve, 1000));
      const state = await refresh();
      if (state.status === "connected") {
        await post("/api/backend", {mode: "remote"});
        location.reload();
        return;
      }
      if (state.status === "disconnected" || state.status === "incompatible")
        throw new Error(state.error || s().failed + ": " + state.status);
    }
    showMessage(s().timeout);
  }));
  disconnect.addEventListener("click", () => run(async () => {
    await post("/api/backend/disconnect");
    location.reload();
  }));
  port.addEventListener("input", () => { port.dataset.edited = "true"; });
  new MutationObserver(render).observe(document.documentElement,
    {attributes: true, attributeFilter: ["lang"]});
  refresh().catch(() => render());
})();
