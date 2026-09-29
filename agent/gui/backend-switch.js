"use strict";
// Credentials and the CSRF token are ephemeral: inputs + one POST body only, never URLs/storage/logs.
(() => {
  const header = document.querySelector(".topbar-meta");
  const loginPage = !header && Boolean(document.querySelector("#lang"));
  if (!header && !loginPage) return;
  const trigger = document.createElement("button");
  trigger.type = "button";
  trigger.className = "secondary small backend-connection-trigger" + (loginPage ? " backend-login-trigger" : "");
  trigger.id = "backend-connection-trigger";
  trigger.setAttribute("aria-haspopup", "dialog");
  trigger.setAttribute("aria-controls", "backend-dialog-backdrop");
  if (header) header.insertBefore(trigger, header.firstChild);
  else document.body.appendChild(trigger);
  const backdrop = document.createElement("div");
  backdrop.id = "backend-dialog-backdrop";
  backdrop.className = "backend-dialog-backdrop";
  backdrop.hidden = true;
  // Authored markup only: all runtime values below use textContent or value.
  backdrop.innerHTML = `
    <section class="backend-dialog" role="dialog" aria-modal="true" aria-labelledby="backend-dialog-title">
      <div class="backend-dialog-head">
        <div><p class="backend-eyebrow">COMPUTE CONNECTION</p><h2 id="backend-dialog-title"></h2></div>
        <button type="button" id="backend-dialog-close" class="text-button" aria-label="Close">×</button>
      </div>
      <p id="backend-dialog-intro" class="backend-dialog-copy"></p>
      <div class="backend-current"><span id="backend-current-label"></span><strong id="backend-current-value"></strong></div>
      <dl class="backend-status-list">
        <div><dt id="backend-serving-label"></dt><dd id="backend-serving-value"></dd></div>
        <div><dt id="backend-source-label"></dt><dd id="backend-source-value"></dd></div>
        <div><dt id="backend-ssh-state-label"></dt><dd id="backend-ssh-state-value"></dd></div>
        <div><dt id="backend-api-state-label"></dt><dd id="backend-api-state-value"></dd></div>
      </dl>
      <p id="backend-source-note" class="backend-dialog-copy"></p>
      <div class="backend-dialog-actions">
        <button type="button" id="backend-use-local" class="secondary"></button>
        <button type="button" id="backend-use-remote" class="secondary"></button>
      </div>
      <div class="backend-dialog-divider"></div>
      <label class="backend-field" for="backend-ssh-url"><span id="backend-ssh-label"></span>
        <input id="backend-ssh-url" type="text" autocomplete="off" spellcheck="false" placeholder="ssh://user@host:22"></label>
      <div class="backend-field-pair">
        <label class="backend-field" for="backend-port"><span id="backend-port-label"></span>
          <input id="backend-port" type="number" min="1" max="65535" value="8765"></label>
        <label class="backend-field" for="backend-auth-method"><span id="backend-auth-label"></span>
          <select id="backend-auth-method"><option value="password" id="backend-auth-password"></option>
            <option value="agent" id="backend-auth-agent"></option><option value="key" id="backend-auth-key"></option></select></label>
      </div>
      <label id="backend-password-field" class="backend-field" for="backend-password"><span id="backend-password-label"></span>
        <input id="backend-password" type="password" autocomplete="new-password" spellcheck="false"></label>
      <div id="backend-key-fields" hidden>
        <label class="backend-field" for="backend-key-path"><span id="backend-key-label"></span>
          <input id="backend-key-path" type="text" autocomplete="off" spellcheck="false" placeholder="C:\\Users\\you\\.ssh\\id_ed25519"></label>
        <label class="backend-field" for="backend-passphrase"><span id="backend-passphrase-label"></span>
          <input id="backend-passphrase" type="password" autocomplete="new-password" spellcheck="false"></label>
      </div>
      <p id="backend-auth-note" class="backend-dialog-copy"></p>
      <p id="backend-ssh-note" class="backend-dialog-copy"></p>
      <section id="backend-host-key" class="backend-host-key" hidden aria-labelledby="backend-host-key-title">
        <h3 id="backend-host-key-title"></h3><p id="backend-host-key-note" class="backend-dialog-copy"></p>
        <pre id="backend-host-key-value"></pre>
        <div class="backend-dialog-actions"><button type="button" id="backend-host-accept" class="primary"></button>
          <button type="button" id="backend-host-reject" class="secondary"></button></div>
      </section>
      <div class="backend-dialog-actions"><button type="button" id="backend-connect" class="primary"></button>
        <button type="button" id="backend-disconnect" class="secondary"></button></div>
      <p id="backend-dialog-message" class="backend-dialog-message" role="status" aria-live="polite"></p>
    </section>`;
  document.body.appendChild(backdrop);
  const $ = id => document.getElementById(id);
  const close = $("backend-dialog-close"), local = $("backend-use-local"), remote = $("backend-use-remote");
  const connect = $("backend-connect"), disconnect = $("backend-disconnect"), sshUrl = $("backend-ssh-url");
  const port = $("backend-port"), method = $("backend-auth-method"), password = $("backend-password");
  const keyPath = $("backend-key-path"), passphrase = $("backend-passphrase"), message = $("backend-dialog-message");
  const accept = $("backend-host-accept"), reject = $("backend-host-reject");
  if (!method.value) method.value = "password";
  let info = null, csrfToken = "", busy = false, attempt = null, pollTimer = null, polling = false;
  let dialogGeneration = 0, refreshGeneration = 0, messageKey = "initializing";
  const pendingStates = new Set(["connecting", "awaiting_host_key", "authenticating"]);
  const activeStates = new Set([...pendingStates, "connected"]);
  const failedStates = new Set(["authentication_failed", "host_key_mismatch", "connection_failed"]);
  const words = {
    trigger: ["计算连接", "Compute connection"], title: ["SSH 计算连接", "SSH compute connection"],
    intro: ["选择分析在哪台机器运行，数据留在对应机器上。", "Choose where analysis runs. Data stays on that machine."],
    current: ["当前计算位置", "Current compute location"], thisPc: ["本机", "This PC"], remote: ["远程", "Remote"],
    close: ["关闭连接窗口", "Close connection dialog"], serving: ["当前正在使用", "Currently serving requests"],
    source: ["候选连接来源", "Candidate connection source"], managed: ["网关管理的 SSH", "Gateway-managed SSH"],
    external: ["已有外部隧道", "Existing external tunnel"], none: ["没有隧道", "No tunnel"],
    externalNote: ["已有外部隧道可能仍在提供服务；它可用，不代表本次新 SSH 连接成功。", "An existing external tunnel may still serve requests. Its availability does not prove this new SSH attempt succeeded."],
    managedNote: ["候选连接不会自动取代当前连接。检查成功后，请手动选择“使用远程”。", "The candidate does not automatically replace the current connection. Select Use remote after verification."],
    activeManaged: ["当前已通过这条 SSH 连接访问远端。", "The current workbench is already using this SSH connection."],
    activeExternal: ["当前已通过这条已有外部隧道访问远端；它不是本窗口新建的 SSH 连接。", "The current workbench is using this existing external tunnel; it was not created by this dialog."],
    sshState: ["SSH 尝试／认证状态", "SSH attempt / authentication"], apiState: ["候选远端 API", "Candidate remote API"],
    disconnected: ["尚未连接", "Not connected"], connecting: ["连接中", "Connecting"],
    awaiting_host_key: ["等待确认服务器指纹", "Awaiting fingerprint confirmation"], authenticating: ["正在认证", "Authenticating"],
    connected: ["已连接", "Connected"],
    authentication_failed: ["SSH 认证失败，请检查账号及所选凭据。", "SSH authentication failed. Check the account and selected credential."],
    host_key_mismatch: ["服务器指纹与已知记录不一致，已阻止连接。请先核实服务器，不要直接覆盖原指纹。", "The host fingerprint changed. Connection blocked; verify the server before changing its known-host entry."],
    connection_failed: ["SSH 连接失败，请检查地址、端口、网络及远端 SSH 服务。", "SSH connection failed. Check the host, port, network and remote SSH service."],
    backend_unavailable: ["无法访问候选后端 API，请检查后端端口及服务是否启动。", "Candidate backend API is unreachable. Check its port and service."],
    backend_auth_required: ["远端需要工作台登录；这与 SSH 认证是两回事。可点“使用远程”前往登录。", "Remote workbench login is required separately from SSH authentication. Select Use remote to sign in."],
    incompatible: ["远端服务不是兼容的工作台 API。", "The remote service is not a compatible workbench API."],
    useLocal: ["使用本机", "Use this PC"], useRemote: ["使用远程", "Use remote"],
    sshLabel: ["SSH 地址或简短 SSH 命令", "SSH address or simple SSH command"],
    portLabel: ["远端后端端口", "Remote backend port"], authLabel: ["认证方式", "Authentication method"],
    passwordMethod: ["账号密码", "Password"], agentMethod: ["SSH agent／默认密钥", "SSH agent / default keys"],
    keyMethod: ["指定私钥文件", "Private key file"], passwordLabel: ["SSH 账号密码", "SSH account password"],
    keyLabel: ["本机私钥文件路径", "Private key path on this PC"], passphraseLabel: ["私钥口令（加密私钥时填写）", "Private key passphrase (if encrypted)"],
    passwordNote: ["请求发送后立即清空密码输入；关闭窗口也会清空。", "The password input is cleared when sent and whenever this dialog closes."],
    agentNote: ["使用本机 SSH agent 或默认密钥，仍需通过身份验证，并不等于“不验证就能登录”。", "Uses the local SSH agent or default keys. This still verifies your identity; it is not access without authentication."],
    keyNote: ["本机网关读取指定私钥；加密私钥可输入口令，口令不会被保存。", "The local gateway reads the private key. An encrypted key may require a passphrase; it is not stored."],
    note: ["支持 ssh://用户名@主机:端口 或 ssh -p 端口 用户名@主机。这里只解析地址，不执行命令；不要把密码贴进地址。", "Accepted: ssh://user@host:port or ssh -p PORT user@host. Parsed as an address, never executed. Do not paste a password into the address."],
    connect: ["连接 SSH", "Connect SSH"], disconnect: ["断开网关 SSH", "Disconnect managed SSH"],
    waiting: ["正在检查本次 SSH 连接…不会自动切换计算位置。", "Checking this SSH attempt… No automatic backend switch."],
    timeout: ["自动检查已达到 150 秒并暂停。可重新打开查看状态；网关连接尝试不会因此自动取消。", "Automatic checking paused after 150 seconds. Reopen to inspect status; the gateway attempt was not automatically cancelled."],
    ready: ["本次网关 SSH 及后端接口已就绪，请点击“使用远程”切换计算位置。", "This managed SSH connection and its API are ready. Select Use remote to switch compute location."],
    verifying: ["SSH 认证已完成，仍在确认本次候选后端。", "SSH authentication succeeded; checking this candidate backend."],
    initializing: ["正在读取本机连接设置…", "Loading local connection controls…"],
    login_required: ["请先登录本机工作台，再修改 SSH 连接。", "Sign in to the local workbench before changing its SSH connection."],
    status_unavailable: ["无法读取本机网关状态，请检查服务后重新打开窗口。", "Cannot read the local gateway status. Check its service and reopen this dialog."],
    csrf_failed: ["连接安全令牌已变化，请重新打开窗口后再试；凭据不会被反复重发。", "The security token changed. Reopen the dialog and try again; credentials were not repeatedly resent."],
    rejected: ["网关拒绝了本次请求，请检查选项；凭据不会被自动重试。", "The gateway rejected this request. Check the options; credentials were not automatically retried."],
    ssh_connection_active: ["已有网关 SSH 连接或认证正在进行，请先断开／取消后再创建新连接。", "A managed SSH connection or authentication attempt is already active. Disconnect or cancel it before starting another."],
    dependency_missing: ["本机缺少 SSH 连接运行依赖，请安装所需依赖并重启网关。", "The local SSH runtime dependency is missing. Install the required dependency and restart the gateway."],
    origin_rejected: ["请求来源未获信任，请从本机工作台页面打开连接设置后重试。", "The request origin is not trusted. Open connection settings from the local workbench and try again."],
    request_failed: ["未能确认请求结果，请先查看连接状态再重试；密码没有被重复发送。", "Request outcome is unconfirmed. Inspect the current attempt before trying again; the password was not resent."],
    invalid_address: ["只接受 ssh://用户名@主机:端口 或 ssh -p 端口 用户名@主机，不接受密码、额外参数和 shell 语法。", "Use ssh://user@host:port or ssh -p PORT user@host only. Passwords, extra options and shell syntax are not accepted."],
    invalid_port: ["后端端口和 SSH 端口必须是 1 到 65535 的整数。", "The backend and SSH ports must be integers from 1 to 65535."],
    password_required: ["请输入 SSH 账号密码，或选择其他认证方式。", "Enter the SSH account password or select another authentication method."],
    key_required: ["请输入本机私钥文件路径。", "Enter the private key path on this PC."],
    invalid_method: ["请选择支持的认证方式。", "Select a supported authentication method."],
    hostTitle: ["确认服务器指纹", "Verify the server fingerprint"],
    hostNote: ["请通过可信渠道核对指纹。确认只对本次连接生效，不会自动信任后续连接。", "Verify through a trusted channel. Accepting authorizes only this attempt, not future connections."],
    hostAccept: ["仅本次信任", "Trust for this attempt"], hostReject: ["拒绝连接", "Reject"],
    hostRejected: ["已拒绝该指纹，本次 SSH 连接没有获得信任。", "Fingerprint rejected. This SSH attempt was not approved."],
    disconnected_ok: ["已断开网关 SSH；此按钮不会终止已有外部隧道。", "Managed SSH disconnected. An existing external tunnel is not terminated by this button."],
    resetLocal: ["重置只作用于当前本机后端；远端模式下须先在“计算连接”中选择“使用本机”。", "Reset applies only to this local backend. In remote mode, select Use local in Compute connection first."],
    resetRemote: ["当前使用远端后端，禁止在此重置。请先在“计算连接”中选择“使用本机”。", "Reset is blocked while using a remote backend. Select Use local in Compute connection first."],
  };
  const t = key => (Object.hasOwn(words, key) ? words[key] : words.request_failed)[document.documentElement.lang === "en" ? 1 : 0];
  const errorCode = code => Object.assign(new Error(code), { code });
  const clearSecrets = () => { password.value = ""; passphrase.value = ""; };
  // The gateway permits one managed attempt at a time. Matching candidate/current sources in
  // remote mode therefore identifies the active candidate without needing another backend field.
  const candidateActive = () => info?.mode === "remote" && ["managed", "external"].includes(info?.connection_source)
    && info.connection_source === info.current_connection_source;
  const activeNotice = () => info?.connection_source === "external" ? "activeExternal" : "activeManaged";
  const renderedMessage = () => messageKey ? t(messageKey === "ready" && candidateActive() ? activeNotice() : messageKey) : "";
  const setMessage = key => { messageKey = key; message.textContent = renderedMessage(); };
  function stopPolling() { if (pollTimer !== null) clearTimeout(pollTimer); pollTimer = null; polling = false; }
  function currentAttempt() { return Boolean(attempt && info && info.attempt_id === attempt.id); }
  function render() {
    trigger.textContent = `${t("trigger")} · ${info?.mode === "remote" ? t("remote") : t("thisPc")}`;
    const labels = { "backend-dialog-title": "title", "backend-dialog-intro": "intro", "backend-current-label": "current",
      "backend-serving-label": "serving", "backend-source-label": "source", "backend-ssh-state-label": "sshState", "backend-api-state-label": "apiState",
      "backend-ssh-label": "sshLabel", "backend-port-label": "portLabel", "backend-auth-label": "authLabel",
      "backend-auth-password": "passwordMethod", "backend-auth-agent": "agentMethod", "backend-auth-key": "keyMethod",
      "backend-password-label": "passwordLabel", "backend-key-label": "keyLabel", "backend-passphrase-label": "passphraseLabel",
      "backend-ssh-note": "note", "backend-host-key-title": "hostTitle", "backend-host-key-note": "hostNote" };
    for (const [id, key] of Object.entries(labels)) $(id).textContent = t(key);
    close.setAttribute("aria-label", t("close"));
    $("backend-current-value").textContent = info?.mode === "remote" ? t("remote") : t("thisPc");
    $("backend-serving-value").textContent = info?.mode === "remote" ? t(info.current_connection_source || "none") : t("thisPc");
    $("backend-source-value").textContent = t(info?.connection_source || "none");
    $("backend-source-note").textContent = candidateActive() ? t(activeNotice()) : info?.connection_source === "external" || info?.current_connection_source === "external"
      ? t("externalNote") : info?.connection_source === "managed" ? t("managedNote") : "";
    $("backend-ssh-state-value").textContent = t(info?.ssh_state || "disconnected");
    $("backend-api-state-value").textContent = t(info?.status || "disconnected");
    local.textContent = t("useLocal"); remote.textContent = t("useRemote"); connect.textContent = t("connect"); disconnect.textContent = t("disconnect");
    accept.textContent = t("hostAccept"); reject.textContent = t("hostReject");
    $("backend-password-field").hidden = method.value !== "password";
    $("backend-key-fields").hidden = method.value !== "key";
    $("backend-auth-note").textContent = t(method.value === "agent" ? "agentNote" : method.value === "key" ? "keyNote" : "passwordNote");
    const ready = Boolean(csrfToken);
    local.disabled = busy || !ready || info?.mode === "local";
    // Remote mode may still be served by an older tunnel; selecting a ready candidate is a real
    // action even when the compute-location label already says Remote.
    remote.disabled = busy || !ready || !["connected", "backend_auth_required"].includes(info?.status) || candidateActive();
    connect.disabled = busy || !ready || (info?.ssh_managed && activeStates.has(info?.ssh_state));
    disconnect.disabled = busy || !ready || !info?.ssh_managed;
    const key = info?.host_key;
    const showKey = Boolean(info?.ssh_state === "awaiting_host_key" && key && typeof info.attempt_id === "string");
    $("backend-host-key").hidden = !showKey;
    $("backend-host-key-value").textContent = showKey ? `${String(key.host || "")}:${String(key.port || "")}\n${String(key.algorithm || "")}\n${String(key.fingerprint || "")}` : "";
    accept.disabled = reject.disabled = busy || !ready || !showKey;
    message.textContent = renderedMessage();
    const reset = $("reset-all");
    if (reset && info) {
      reset.dataset.remoteBlocked = String(info.mode === "remote");
      reset.disabled = info.mode === "remote" || reset.dataset.resetBusy === "true" || reset.dataset.taskBusy === "true";
      const note = $("reset-scope-note"), key = info.mode === "remote" ? "resetRemote" : "resetLocal";
      if (note) { note.dataset.label = words[key][0]; note.textContent = t(key); }
    }
  }
  function parseAddress(raw) {
    let value = String(raw).trim();
    if (/[\r\n\x00-\x1f`$;&|<>\\]/.test(value)) throw errorCode("invalid_address");
    if (!value.startsWith("ssh://")) {
      const command = /^ssh\s+(?:-p\s*(\d+)\s+)?([^\s]+)$/.exec(value);
      if (!command) throw errorCode("invalid_address");
      value = "ssh://" + command[2] + (command[1] ? ":" + command[1] : "");
    }
    let url, user;
    try { url = new URL(value); user = decodeURIComponent(url.username); } catch (ignored) { throw errorCode("invalid_address"); }
    if (url.protocol !== "ssh:" || url.password || value.slice(6).split("@")[0].includes(":") || !/^[A-Za-z_][A-Za-z0-9_.-]*$/.test(user)
        || url.search || url.hash || (url.pathname && url.pathname !== "/")
        || !(/^[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?$/.test(url.hostname) || /^\[[0-9a-f:.]+\]$/i.test(url.hostname))) throw errorCode("invalid_address");
    const sshPort = url.port || "22";
    if (!/^\d+$/.test(sshPort) || Number(sshPort) < 1 || Number(sshPort) > 65535) throw errorCode("invalid_port");
    return `ssh://${user}@${url.hostname}:${Number(sshPort)}`;
  }
  async function refresh(timeoutMs = 8000) {
    const generation = ++refreshGeneration;
    const controller = typeof AbortController === "function" ? new AbortController() : null;
    const timer = controller ? setTimeout(() => controller.abort(), Math.max(1, timeoutMs)) : null;
    try {
      const response = await fetch("/api/backend", { cache: "no-store", ...(controller ? { signal: controller.signal } : {}) });
      if (!response.ok) throw errorCode(response.status === 401 ? "login_required" : "status_unavailable");
      const data = await response.json();
      if (!data || typeof data !== "object") throw errorCode("status_unavailable");
      if (generation !== refreshGeneration) return info;
      csrfToken = typeof data.csrf_token === "string" ? data.csrf_token : "";
      const { csrf_token: _token, ...publicInfo } = data;
      info = publicInfo;
      if (info.ssh_url && !sshUrl.value) sshUrl.value = info.ssh_url;
      if (info.backend_port && !port.dataset.edited) port.value = String(info.backend_port);
      if (!csrfToken) setMessage("login_required");
      render(); return info;
    } catch (error) {
      if (generation === refreshGeneration) csrfToken = "";
      throw error.code ? error : errorCode("status_unavailable");
    } finally { if (timer !== null) clearTimeout(timer); }
  }
  async function post(path, body = {}) {
    if (!csrfToken) throw errorCode("login_required");
    for (let tries = 0; tries < 2; tries++) {
      let response, data;
      const controller = typeof AbortController === "function" ? new AbortController() : null;
      const timer = controller ? setTimeout(() => controller.abort(), 15000) : null;
      try {
        response = await fetch(path, { method: "POST", headers: { "Content-Type": "application/json", "X-GWB-CSRF": csrfToken }, body: JSON.stringify(body),
          ...(controller ? { signal: controller.signal } : {}) });
        data = await response.json();
      } catch (ignored) { throw errorCode("request_failed"); }
      finally { if (timer !== null) clearTimeout(timer); }
      const code = data && (data.code || data.error_code);
      // Only this 403 proves the request was rejected before authentication began.
      if (response.status === 403 && code === "csrf_failed" && tries === 0) {
        await refresh(); if (!csrfToken) throw errorCode("csrf_failed"); continue;
      }
      if (!response.ok || !data || !data.ok) throw errorCode(response.status === 401 ? "login_required"
        : Object.hasOwn(words, code) ? code : response.status === 403 ? "rejected" : "request_failed");
      return data;
    }
    throw errorCode("csrf_failed");
  }
  async function action(fn) {
    if (busy) return;
    busy = true; setMessage(""); render();
    try { await fn(); } catch (error) { setMessage(error.code || "request_failed"); }
    finally { busy = false; render(); }
  }
  function inspectAttempt() {
    if (!currentAttempt()) return false;
    if (failedStates.has(info.ssh_state)) { stopPolling(); setMessage(info.ssh_state); return true; }
    if (info.ssh_state === "connected" && info.connection_source === "managed") {
      if (info.status === "connected") { stopPolling(); setMessage("ready"); return true; }
      if (["backend_unavailable", "backend_auth_required", "incompatible"].includes(info.status)) { stopPolling(); setMessage(info.status); return true; }
    }
    setMessage(info.ssh_state === "awaiting_host_key" ? "awaiting_host_key" : info.ssh_state === "connected" ? "verifying" : "waiting");
    return false;
  }
  function startPolling(id) {
    stopPolling(); if (!id || backdrop.hidden) return;
    if (!attempt || attempt.id !== id) attempt = { id, deadline: Date.now() + 150000 };
    if (inspectAttempt()) { render(); return; }
    polling = true;
    const generation = dialogGeneration;
    const tick = async () => {
      pollTimer = null;
      if (!polling || backdrop.hidden || generation !== dialogGeneration) return;
      const remaining = attempt.deadline - Date.now();
      if (remaining <= 0) { stopPolling(); setMessage("timeout"); render(); return; }
      try { await refresh(Math.min(8000, remaining)); }
      catch (error) { stopPolling(); setMessage(error.code || "status_unavailable"); render(); return; }
      if (backdrop.hidden || generation !== dialogGeneration || !polling) return;
      if (Date.now() >= attempt.deadline) { stopPolling(); setMessage("timeout"); render(); return; }
      if (info?.attempt_id && info.attempt_id !== attempt.id) { stopPolling(); setMessage("status_unavailable"); render(); return; }
      if (!inspectAttempt()) pollTimer = setTimeout(tick, Math.min(1000, Math.max(1, attempt.deadline - Date.now())));
      render();
    };
    pollTimer = setTimeout(tick, Math.min(1000, Math.max(1, attempt.deadline - Date.now()))); render();
  }
  function closeDialog() { backdrop.hidden = true; dialogGeneration++; stopPolling(); clearSecrets(); trigger.focus(); }
  trigger.addEventListener("click", async () => {
    backdrop.hidden = false; dialogGeneration++; const generation = dialogGeneration;
    setMessage("initializing"); render();
    try {
      await refresh(); if (backdrop.hidden || generation !== dialogGeneration) return;
      if (csrfToken) setMessage("");
      if (info?.attempt_id && (pendingStates.has(info.ssh_state) || info.ssh_state === "connected")) startPolling(info.attempt_id);
      else if (failedStates.has(info?.ssh_state)) setMessage(info.ssh_state);
    } catch (error) { setMessage(error.code || "status_unavailable"); render(); }
    if (!backdrop.hidden && generation === dialogGeneration) sshUrl.focus();
  });
  close.addEventListener("click", closeDialog);
  backdrop.addEventListener("click", event => { if (event.target === backdrop) closeDialog(); });
  document.addEventListener("keydown", event => {
    if (backdrop.hidden) return;
    if (event.key === "Tab") {
      const items = [...backdrop.querySelectorAll("button:not(:disabled), input:not(:disabled), select:not(:disabled)")].filter(node => !node.closest("[hidden]"));
      const first = items[0], last = items[items.length - 1]; if (!first) return;
      if (event.shiftKey && (document.activeElement === first || !backdrop.contains(document.activeElement))) { event.preventDefault(); last.focus(); }
      else if (!event.shiftKey && (document.activeElement === last || !backdrop.contains(document.activeElement))) { event.preventDefault(); first.focus(); }
    }
    if (event.key === "Escape") { event.stopImmediatePropagation(); closeDialog(); }
  }, true);
  method.addEventListener("change", () => { clearSecrets(); render(); if (method.value === "password") password.focus(); else if (method.value === "key") keyPath.focus(); });
  for (const field of [sshUrl, port, password, keyPath, passphrase]) field.addEventListener("keydown", event => {
    if (event.key === "Enter") { event.preventDefault(); if (!connect.disabled) connect.click(); }
  });
  local.addEventListener("click", () => action(async () => {
    clearSecrets(); await post("/api/backend", { mode: "local" });
    // An explicit mode change must reload even if the dialog was closed during the POST:
    // otherwise the changed routing cookie would leave the old backend's dataset context visible.
    location.reload();
  }));
  remote.addEventListener("click", () => action(async () => {
    clearSecrets(); await post("/api/backend", { mode: "remote" }); location.reload();
  }));
  connect.addEventListener("click", () => action(async () => {
    const address = parseAddress(sshUrl.value), backendPort = String(port.value).trim();
    if (!/^\d+$/.test(backendPort) || Number(backendPort) < 1 || Number(backendPort) > 65535) throw errorCode("invalid_port");
    if (!["password", "agent", "key"].includes(method.value)) throw errorCode("invalid_method");
    if (method.value === "password" && !password.value) throw errorCode("password_required");
    if (method.value === "key" && !keyPath.value.trim()) throw errorCode("key_required");
    const body = { ssh_url: address, backend_port: Number(backendPort), auth_method: method.value };
    if (method.value === "password") body.password = password.value;
    if (method.value === "key") { body.key_path = keyPath.value.trim(); body.passphrase = passphrase.value; }
    clearSecrets(); const generation = dialogGeneration; let reply;
    try { reply = await post("/api/backend/connection", body); }
    finally { if ("password" in body) body.password = ""; if ("passphrase" in body) body.passphrase = ""; }
    if (typeof reply.attempt_id !== "string") throw errorCode("request_failed");
    attempt = { id: reply.attempt_id, deadline: Date.now() + 150000 };
    await refresh();
    if (!backdrop.hidden && generation === dialogGeneration) { setMessage("waiting"); startPolling(attempt.id); }
  }));
  for (const [button, decision] of [[accept, true], [reject, false]]) button.addEventListener("click", () => action(async () => {
    if (info?.ssh_state !== "awaiting_host_key" || !info?.host_key || !info?.attempt_id) throw errorCode("rejected");
    const id = info.attempt_id;
    await post("/api/backend/host-key", { attempt_id: id, accept: decision }); await refresh();
    if (decision) startPolling(id); else { stopPolling(); setMessage("hostRejected"); }
  }));
  disconnect.addEventListener("click", () => {
    clearSecrets(); return action(async () => {
      const previousMode = info?.mode;
      stopPolling(); const reply = await post("/api/backend/disconnect");
      if (typeof reply.mode === "string" && reply.mode !== previousMode) { location.reload(); return; }
      await refresh();
      if (info?.mode !== previousMode) { location.reload(); return; }
      setMessage("disconnected_ok");
    });
  });
  port.addEventListener("input", () => { port.dataset.edited = "true"; });
  new MutationObserver(render).observe(document.documentElement, { attributes: true, attributeFilter: ["lang"] });
  render(); refresh().then(() => { if (csrfToken) setMessage(""); }).catch(error => { setMessage(error.code || "status_unavailable"); render(); });
})();
