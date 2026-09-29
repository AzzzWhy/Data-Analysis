#!/usr/bin/env python3
"""One local workbench URL with a selectable local or SSH-tunnelled backend.

The browser always receives this checkout's GUI assets. API, SSE and artifact
requests go to the selected backend. A remote backend must be exposed on a
*local* loopback port by managed SSH or an external SSH client.
SSH credentials are accepted only in authenticated request bodies for one attempt;
they are never saved. No arbitrary HTTP proxy or remote command execution is exposed.
"""
from __future__ import annotations

import argparse
from http.client import HTTPConnection, HTTPException
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import hashlib
import hmac
import ipaddress
from pathlib import Path
import re
import secrets
import shlex
import threading
from urllib.parse import urlsplit

import gui
from managed_ssh import ManagedSSH


ROOT = Path(__file__).resolve().parent / "gui"
MAX_BODY = 1024 * 1024
UPSTREAM_TIMEOUT_SECONDS = 30
ASSETS = {
    "/app.js": ("app.js", "application/javascript; charset=utf-8"),
    "/style.css": ("style.css", "text/css; charset=utf-8"),
    "/login.js": ("login.js", "application/javascript; charset=utf-8"),
    "/login.css": ("login.css", "text/css; charset=utf-8"),
    "/backend-switch.js": ("backend-switch.js", "application/javascript; charset=utf-8"),
    "/backend-switch.css": ("backend-switch.css", "text/css; charset=utf-8"),
}
HOP_HEADERS = {"connection", "transfer-encoding", "keep-alive", "proxy-authenticate",
               "proxy-authorization", "te", "trailer", "upgrade", "content-length",
               "set-cookie", "content-security-policy", "x-content-type-options",
               "content-type", "cache-control"}


def loopback_endpoint(value: str) -> tuple[str, int]:
    """Accept only an explicit loopback HTTP origin, never an internet proxy."""
    parsed = urlsplit(value)
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("remote backend port is invalid") from exc
    if (parsed.scheme != "http" or parsed.hostname not in
            {"127.0.0.1", "localhost", "::1"} or not port or parsed.username or
            parsed.password or parsed.path not in ("", "/") or parsed.query or parsed.fragment):
        raise ValueError("remote backend must be http://127.0.0.1:PORT over an SSH tunnel")
    return parsed.hostname, port


def ssh_endpoint(value: str, backend_port: object) -> tuple[str, int, str, int]:
    """Accept a URI or the common ssh [-p PORT] USER@HOST form, never execute it."""
    if not isinstance(value, str) or len(value) > 2048:
        raise ValueError("SSH address must be text")
    value = value.strip()
    if value.startswith("ssh "):
        # Deliberately not a shell grammar: forwarding, -o, ProxyCommand, remote
        # commands and pasted command separators cannot reach any executor.
        try:
            words = shlex.split(value, posix=True)
        except ValueError:
            raise ValueError("SSH command has invalid quotes") from None
        ssh_port = "22"
        if len(words) == 4 and words[1] == "-p":
            ssh_port, target = words[2:]
        elif len(words) == 3 and words[1].startswith("-p"):
            ssh_port, target = words[1][2:], words[2]
        elif len(words) == 2:
            target = words[1]
        else:
            raise ValueError("only ssh [-p PORT] USER@HOST is supported; use the key field for private keys")
        if not ssh_port.isascii() or not ssh_port.isdecimal() or target.count("@") != 1:
            raise ValueError("SSH command must contain USER@HOST and a numeric port")
        value = f"ssh://{target}:{ssh_port}"
    parsed = urlsplit(value)
    try:
        ssh_port = parsed.port if parsed.port is not None else 22
        username = parsed.username
        if isinstance(backend_port, bool) or not str(backend_port).isascii() or not str(backend_port).isdecimal():
            raise ValueError("invalid backend port")
        port = int(backend_port)
    except (TypeError, ValueError) as exc:
        raise ValueError("SSH or backend port is invalid") from exc
    host = parsed.hostname or ""
    valid_host = bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]*", host))
    if not valid_host:
        try:
            ipaddress.IPv6Address(host)
            valid_host = True
        except ValueError:
            pass
    if (parsed.scheme != "ssh" or not username or not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]*", username)
            or not valid_host
            or parsed.password is not None or parsed.path not in ("", "/")
            or parsed.query or parsed.fragment or not 1 <= ssh_port <= 65535
            or not 1 <= port <= 65535):
        raise ValueError("use ssh://USER@HOST:PORT without a password; backend port must be 1-65535")
    return username, ssh_port, host, port


def loopback_origin(value: str, port: int) -> bool:
    """Canonical localhost aliases are one local UI; paths and userinfo are not origins."""
    try:
        parsed = urlsplit(value)
        return (parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1", "::1"}
                and (parsed.port or 80) == port and parsed.path in ("", "/")
                and not (parsed.username or parsed.password or parsed.query or parsed.fragment))
    except ValueError:
        return False


def portless_loopback_origin(value: str) -> bool:
    """Some embedded browsers omit the local UI port from Origin.

    This exception is usable only alongside an exact Host check, the local gate
    and a session-bound nonce. Explicit origins from other ports remain denied.
    A page on another origin cannot read that nonce (no CORS is enabled).
    """
    try:
        parsed = urlsplit(value)
        return (parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1", "::1"}
                and parsed.port is None and parsed.path in ("", "/")
                and not (parsed.username or parsed.password or parsed.query or parsed.fragment))
    except (TypeError, ValueError):
        return False


def cookies(header: str) -> SimpleCookie:
    try:
        return SimpleCookie(header or "")
    except CookieError:
        return SimpleCookie()


class GatewayHandler(BaseHTTPRequestHandler):
    server: "GatewayServer"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        # Never print query strings (artifact paths or accidental credentials).
        if self.server.verbose:
            print(f"gateway: {self.command} {self.path.split('?', 1)[0]}")

    def mode(self) -> str:
        selected = cookies(self.headers.get("Cookie", "")).get("gwbmode")
        return "remote" if selected and selected.value == "remote" else "local"

    def backend(self, mode: str) -> tuple[str, int] | None:
        return self.server.remote if mode == "remote" else self.server.local

    def backend_cookie(self, mode: str, generation: str | None = None) -> str:
        value = cookies(self.headers.get("Cookie", "")).get(self.backend_cookie_name(mode, generation))
        return "dws=" + value.value if value and value.value else ""

    def backend_cookie_name(self, mode: str, generation: str | None = None) -> str:
        # A token issued by one SSH backend must never be sent to a replacement.
        return "gwb_local" if mode == "local" else "gwb_remote_" + (generation or self.server.remote_generation)

    def csrf_token(self) -> str:
        # Bind the nonce to this local sign-in and gateway lifetime. No SSH secret
        # is used; the token is never persisted by the frontend.
        cookie = self.backend_cookie("local")
        return hmac.new(self.server.csrf_secret, cookie.encode(), hashlib.sha256).hexdigest()

    def allow_backend_change(self) -> bool:
        port = self.server.server_address[1]
        origin = self.headers.get("Origin")
        host_ok = loopback_origin("http://" + self.headers.get("Host", ""), port)
        origin_ok = (origin in (None, "null") or loopback_origin(origin, port)
                     or portless_loopback_origin(origin))
        if not host_ok or not origin_ok:
            self.close_connection = True
            # A small diagnostic, not raw request logging: no body/cookies/keys.
            def safe_origin(value):
                if value in (None, "null"):
                    return value
                try:
                    parsed = urlsplit(value)
                    return {"scheme": parsed.scheme, "host": parsed.hostname, "port": parsed.port}
                except ValueError:
                    return "invalid"
            self.server.origin_rejection = {"origin": safe_origin(origin),
                                            "host": safe_origin("http://" + self.headers.get("Host", ""))}
            self.json_reply(403, {"error": "request origin does not match this local workbench",
                                  "code": "origin_rejected"})
            return False
        if not self.local_authorized():
            self.close_connection = True
            self.json_reply(401, {"error": "sign in to this PC before changing its compute connection"})
            return False
        supplied = self.headers.get("X-GWB-CSRF", "")
        if not supplied.isascii() or not hmac.compare_digest(supplied, self.csrf_token()):
            self.close_connection = True
            self.json_reply(403, {"error": "refresh the connection dialog before trying again",
                                  "code": "csrf_failed"})
            return False
        # Opaque embedded-webview origins and CLI requests still require the
        # authenticated, same-page nonce. Cross-site origins remain rejected.
        return True

    def reply(self, code: int, body: bytes, content_type: str,
              headers: list[tuple[str, str]] = ()) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Security-Policy", gui.CSP)
        self.send_header("X-Content-Type-Options", "nosniff")
        if self.close_connection:
            self.send_header("Connection", "close")
        for key, value in headers:
            self.send_header(key, value)
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def json_reply(self, code: int, payload: dict, headers=()) -> None:
        self.reply(code, json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8", headers)

    def read_body(self) -> bytes | None:
        if self.headers.get("Transfer-Encoding"):
            self.json_reply(400, {"error": "chunked request bodies are not supported"})
            return None
        try:
            length = int(self.headers.get("Content-Length") or "0")
        except ValueError:
            length = -1
        if length < 0 or length > MAX_BODY:
            self.json_reply(413, {"error": "invalid or oversized request body"})
            return None
        return self.rfile.read(length)

    def backend_request(self, mode: str, method: str, path: str,
                        body: bytes | None = None, timeout: float | None = None):
        with self.server.ssh_lock:
            self.server.check_ssh_process()
            endpoint = self.backend(mode)
            generation = self.server.remote_generation
        if endpoint is None:
            raise OSError("remote backend is not configured")
        connection = HTTPConnection(*endpoint, timeout=(
            UPSTREAM_TIMEOUT_SECONDS if timeout is None else timeout))
        headers = {"Accept": self.headers.get("Accept", "*/*"),
                   "User-Agent": "gpu-workbench-gateway", "Connection": "close"}
        if path.startswith("/api/events?") and self.headers.get("Last-Event-ID") is not None:
            # EventSource updates this header on automatic reconnect; dropping it
            # would replay the entire buffer through a remote SSH gateway.
            headers["Last-Event-ID"] = self.headers["Last-Event-ID"]
        if body is not None:
            headers["Content-Type"] = self.headers.get("Content-Type", "application/json")
        auth_cookie = self.backend_cookie(mode, generation)
        connection.gwb_cookie_name = self.backend_cookie_name(mode, generation)
        if auth_cookie:
            headers["Cookie"] = auth_cookie
        try:
            connection.request(method, path, body=body, headers=headers)
            return connection, connection.getresponse()
        except Exception:
            connection.close()
            raise

    def local_authorized(self) -> bool:
        try:
            connection, response = self.backend_request("local", "GET", "/api/auth", timeout=5)
            status = response.status
            response.read()
            connection.close()
            return status == 200
        except (OSError, TimeoutError, HTTPException):
            return False

    def connection_info(self) -> dict:
        self.server.check_ssh_process()
        authorized = self.local_authorized()
        if not authorized:
            return {"mode": "local", "status": "disconnected", "ssh_state": "disconnected",
                    "remote_configured": False, "ssh_managed": False, "connection_source": "none",
                    "error": "local_login_required"}
        with self.server.ssh_lock:
            snapshot = self.server.managed_ssh.snapshot() if self.server.candidate_attempt else {}
            endpoint = self.server.candidate_endpoint()
            attempt = self.server.candidate_attempt
            source = "managed" if self.server.candidate_attempt else (
                "external" if endpoint else "none")
        ssh_state = snapshot.get("ssh_state", "disconnected")
        status = "backend_unavailable" if endpoint is not None else (
            "connecting" if ssh_state in {"connecting", "awaiting_host_key", "authenticating"} else "disconnected")
        if endpoint is not None:
            try:
                connection, response = self.probe_backend(endpoint, attempt=attempt, timeout=1.5)
                code = response.status
                raw = response.read()
                connection.close()
                if code == 401:
                    status = "backend_auth_required"
                elif code == 200:
                    try:
                        state = json.loads(raw)
                    except ValueError:
                        state = {}
                    status = "connected" if isinstance(state, dict) and all(key in state for key in
                        ("engine_ready", "missing_fields", "i18n")) else "incompatible"
            except (OSError, TimeoutError, HTTPException):
                pass
        info = {**snapshot, "mode": self.mode(), "status": status,
                "remote_configured": endpoint is not None,
                "ssh_managed": bool(self.server.candidate_attempt),
                "ssh_state": ssh_state if source == "managed" else source if source == "external" else "disconnected",
                "connection_source": source,
                "current_connection_source": self.server.active_source if self.server.remote else "none",
                "error": snapshot.get("error", ""), "csrf_token": self.csrf_token()}
        info["ssh_url"] = self.server.ssh_url
        info["backend_port"] = self.server.backend_port
        if self.server.origin_rejection:
            info["last_origin_rejection"] = self.server.origin_rejection
        return info

    def probe_backend(self, endpoint, timeout: float = 5, *, attempt=None):
        # A new candidate must not receive an old target's backend login token.
        headers = {"Accept": "application/json", "Connection": "close"}
        with self.server.ssh_lock:
            source = "managed" if attempt else "external"
            if (endpoint == self.server.remote and attempt == self.server.active_attempt
                    and source == self.server.active_source):
                value = self.backend_cookie("remote", self.server.remote_generation)
                if value:
                    headers["Cookie"] = value
        connection = HTTPConnection(*endpoint, timeout=timeout)
        try:
            connection.request("GET", "/api/state", headers=headers)
            return connection, connection.getresponse()
        except Exception:
            connection.close()
            raise

    def forward(self, mode: str, method: str, body: bytes | None = None) -> None:
        # Mode is a user-controlled cookie, not an authorization grant. Check the
        # local gate on every remote request, including artifacts and SSE.
        if mode == "remote" and not self.local_authorized():
            if self.path.split("?", 1)[0] in ("/api/gate", "/api/login", "/api/setup"):
                mode = "local"  # Complete the local gate before any remote sign-in.
            else:
                self.json_reply(401, {"error": "sign in to this PC before using a remote backend"})
                return
        # HTTPConnection must receive an origin-form path, never an absolute URL.
        if not self.path.startswith("/") or self.path.startswith("//"):
            self.json_reply(400, {"error": "invalid request path"})
            return
        if self.path.split("?", 1)[0] == "/api/shutdown":
            self.json_reply(403, {"error": "shutdown is not available through the gateway"})
            return
        if mode == "remote" and self.path.split("?", 1)[0] == "/api/reset":
            self.json_reply(403, {"error": "remote reset is disabled in the gateway"})
            return
        try:
            connection, response = self.backend_request(mode, method, self.path, body)
        except (OSError, TimeoutError, HTTPException) as exc:
            self.json_reply(502, {"error": "selected backend is unavailable",
                                  "mode": mode, "detail": type(exc).__name__})
            return
        try:
            upstream_type = response.getheader("Content-Type", "application/octet-stream")
            extra = [(key, value) for key, value in response.getheaders()
                     if key.lower() not in HOP_HEADERS and "\r" not in value and "\n" not in value]
            for value in response.headers.get_all("Set-Cookie", []):
                upstream_cookie = cookies(value).get("dws")
                if upstream_cookie is not None:
                    name = connection.gwb_cookie_name
                    suffix = "; Path=/; HttpOnly; SameSite=Strict"
                    if "Max-Age=0" in value or not upstream_cookie.value:
                        suffix += "; Max-Age=0"
                    extra.append(("Set-Cookie", f"{name}={upstream_cookie.value}{suffix}"))
            if upstream_type.startswith("text/event-stream"):
                # A healthy analysis can be quiet for longer than the request timeout.
                # Keep the initial connection deadline, then let the live stream wait.
                stream_socket = connection.sock or getattr(
                    getattr(response.fp, "raw", None), "_sock", None)
                if stream_socket is not None:
                    stream_socket.settimeout(None)
                self.send_response(response.status)
                self.send_header("Content-Type", upstream_type)
                self.send_header("Cache-Control", "no-store")
                self.send_header("Connection", "close")
                self.send_header("Content-Security-Policy", gui.CSP)
                for key, value in extra:
                    self.send_header(key, value)
                self.end_headers()
                try:
                    while line := response.fp.readline():
                        self.wfile.write(line)
                        self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError, TimeoutError, OSError):
                    pass
                self.close_connection = True
            else:
                self.reply(response.status, response.read(), upstream_type, extra)
        finally:
            connection.close()

    def static(self, name: str, content_type: str, inject: bool = False, headers=()) -> None:
        path = (ROOT / name).resolve()
        if not path.is_relative_to(ROOT.resolve()) or not path.is_file():
            self.json_reply(404, {"error": "frontend asset is missing"})
            return
        body = path.read_bytes().replace(b"{{BUILD}}", gui.BUILD_TAG.encode("utf-8"))
        if inject:
            body = body.replace(b"</head>",
                b'<link rel="stylesheet" href="/backend-switch.css">\n</head>')
            body = body.replace(b"</body>",
                b'<script src="/backend-switch.js"></script>\n</body>')
        self.reply(200, body, content_type, headers)

    def do_GET(self):
        if not self.path.startswith("/") or self.path.startswith("//"):
            self.json_reply(400, {"error": "invalid request path"})
            return
        path = self.path.split("?", 1)[0]
        mode = self.mode()
        if path == "/api/backend":
            self.json_reply(200, self.connection_info())
            return
        if path in ("/", "/index.html"):
            if mode == "remote" and not self.local_authorized():
                # This is the PC's access gate, not an SSH/backend login. A stale
                # remote-mode cookie must not label its password setup as remote.
                self.static("login.html", "text/html; charset=utf-8", inject=True,
                            headers=[("Set-Cookie", "gwbmode=local; Path=/; HttpOnly; SameSite=Strict")])
                return
            try:
                connection, response = self.backend_request(mode, "GET", "/api/state")
                status = response.status
                response.read()
                connection.close()
            except (OSError, TimeoutError, HTTPException) as exc:
                if mode == "remote":
                    # Keep the selector available when a selected SSH tunnel drops.
                    self.static("index.html", "text/html; charset=utf-8", inject=True)
                    return
                self.json_reply(502, {"error": "selected backend is unavailable",
                                      "mode": mode, "detail": type(exc).__name__})
                return
            if status == 401:
                # The page is always local, including the sign-in gate. Inject the
                # connection dialog so an expired remote login cannot strand users.
                self.static("login.html", "text/html; charset=utf-8", inject=True)
                return
            if status != 200:
                if mode == "remote":
                    self.static("index.html", "text/html; charset=utf-8", inject=True)
                    return
                self.json_reply(502, {"error": "selected backend did not provide a usable state",
                                      "mode": mode, "status": status})
                return
            self.static("index.html", "text/html; charset=utf-8", inject=True)
            return
        if path in ASSETS:
            self.static(*ASSETS[path])
            return
        if path.startswith("/api/") or path == "/artifact":
            self.forward(mode, "GET")
            return
        self.json_reply(404, {"error": "unknown route"})

    def do_POST(self):
        if not self.path.startswith("/") or self.path.startswith("//"):
            self.json_reply(400, {"error": "invalid request path"})
            return
        path = self.path.split("?", 1)[0]
        if path in ("/api/backend", "/api/backend/connection", "/api/backend/disconnect", "/api/backend/host-key", "/api/reset"):
            if not self.allow_backend_change():
                return
        if path == "/api/backend/connection":
            if not self.local_authorized():
                self.json_reply(401, {"error": "sign in to this PC before configuring SSH"})
                return
            body = self.read_body()
            if body is None:
                return
            try:
                config = json.loads(body)
                attempt = self.server.connect_ssh(config)
            except (ValueError, KeyError, TypeError) as exc:
                self.json_reply(400, {"error": str(exc)})
                return
            except RuntimeError:
                self.json_reply(409, {"error": "disconnect the existing managed attempt before connecting again",
                                      "code": "ssh_connection_active"})
                return
            except OSError:
                self.json_reply(502, {"error": "could not start SSH connection", "code": "ssh_connection_failed"})
                return
            self.json_reply(202, {"ok": True, "status": "connecting", "attempt_id": attempt})
            return
        if path == "/api/backend/host-key":
            body = self.read_body()
            if body is None:
                return
            try:
                config = json.loads(body)
                if not isinstance(config, dict) or type(config.get("accept")) is not bool:
                    raise ValueError("host key confirmation requires a boolean decision")
                self.server.managed_ssh.confirm_host_key(config.get("attempt_id"), config["accept"])
            except (ValueError, TypeError, KeyError) as exc:
                self.json_reply(400, {"error": str(exc)})
                return
            self.json_reply(200, {"ok": True})
            return
        if path == "/api/backend/disconnect":
            if not self.local_authorized():
                self.json_reply(401, {"error": "sign in to this PC before disconnecting SSH"})
                return
            if self.server.candidate_attempt is None:
                self.json_reply(409, {"error": "this tunnel was started outside the workbench; "
                                             "close it in its own terminal"})
                return
            with self.server.ssh_lock:
                # Cancelling a candidate must not disrupt an older active tunnel.
                mode = "local" if self.server.active_source == "managed" else self.mode()
                self.server.disconnect_ssh()
            self.json_reply(200, {"ok": True, "mode": mode},
                            [("Set-Cookie", f"gwbmode={mode}; Path=/; HttpOnly; SameSite=Strict")])
            return
        if path == "/api/backend":
            body = self.read_body()
            if body is None:
                return
            try:
                requested = json.loads(body).get("mode")
            except (ValueError, AttributeError):
                requested = None
            if requested not in ("local", "remote"):
                self.json_reply(400, {"error": "mode must be local or remote"})
                return
            if requested == "remote":
                if not self.local_authorized():
                    self.json_reply(401, {"error": "sign in to this PC before using a remote backend"})
                    return
                self.server.check_ssh_process()
                with self.server.ssh_lock:
                    candidate = self.server.candidate_endpoint()
                    attempt = self.server.candidate_attempt
                if not candidate:
                    self.json_reply(409, {"error": "connect to an SSH backend first"})
                    return
                try:
                    connection, response = self.probe_backend(candidate, attempt=attempt, timeout=5)
                    status = response.status
                    payload = response.read()
                    connection.close()
                except (OSError, TimeoutError, HTTPException):
                    self.json_reply(502, {"error": "remote backend is unreachable; mode unchanged"})
                    return
                if status not in (200, 401):
                    self.json_reply(502, {"error": "remote backend is not ready; mode unchanged",
                                          "status": status})
                    return
                if status == 200:
                    try:
                        state = json.loads(payload)
                    except ValueError:
                        state = {}
                    if not isinstance(state, dict) or not all(key in state for key in ("engine_ready", "missing_fields", "i18n")):
                        self.json_reply(409, {"error": "remote backend is too old for this frontend; "
                                                      "update it before switching"})
                        return
                with self.server.ssh_lock:
                    if candidate != self.server.candidate_endpoint() or attempt != self.server.candidate_attempt:
                        self.json_reply(409, {"error": "connection changed while checking; inspect it before switching"})
                        return
                    self.server.activate_remote(candidate, attempt)
            self.json_reply(200, {"ok": True, "mode": requested},
                            [("Set-Cookie", f"gwbmode={requested}; Path=/; HttpOnly; SameSite=Strict")])
            return
        if path.startswith("/api/"):
            body = self.read_body()
            if body is not None:
                self.forward(self.mode(), "POST", body)
            return
        self.json_reply(404, {"error": "unknown route"})

    def do_PATCH(self):
        if not self.path.startswith("/") or self.path.startswith("//"):
            self.json_reply(400, {"error": "invalid request path"})
            return
        if self.path.split("?", 1)[0].startswith("/api/"):
            body = self.read_body()
            if body is not None:
                self.forward(self.mode(), "PATCH", body)
            return
        self.json_reply(404, {"error": "unknown route"})


class GatewayServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, local: tuple[str, int], remote: tuple[str, int] | None,
                 verbose: bool = False):
        super().__init__(address, GatewayHandler)
        self.local = local
        self.remote = remote
        self.external_remote = remote
        self.active_source = "external" if remote else "none"
        self.active_attempt = None
        self.candidate_attempt = None
        self.managed_ssh = ManagedSSH()
        self.remote_generation = secrets.token_hex(8)
        self.csrf_secret = secrets.token_bytes(32)
        self.origin_rejection = None
        self.verbose = verbose
        self.ssh_url = ""
        self.backend_port: int | None = None
        self.ssh_lock = threading.RLock()

    def check_ssh_process(self) -> None:
        with self.ssh_lock:
            state = self.managed_ssh.snapshot()
            if (self.active_source == "managed" and
                    (state.get("ssh_state") != "connected" or state.get("attempt_id") != self.active_attempt)):
                self.remote = None
                self.active_source = "none"
                self.active_attempt = None
                self.remote_generation = secrets.token_hex(8)

    def candidate_endpoint(self):
        with self.ssh_lock:
            if self.candidate_attempt:
                snapshot = self.managed_ssh.snapshot()
                if snapshot.get("attempt_id") != self.candidate_attempt:
                    return None
                return self.managed_ssh.endpoint()
            return self.external_remote

    def activate_remote(self, endpoint, attempt=None):
        """Caller verified the candidate API; selecting it is a separate user action."""
        with self.ssh_lock:
            source = "managed" if attempt else "external"
            if self.remote != endpoint or self.active_attempt != attempt or self.active_source != source:
                self.remote_generation = secrets.token_hex(8)
            self.remote = endpoint
            self.active_attempt = attempt
            self.active_source = source

    def connect_ssh(self, config: dict) -> str:
        if not isinstance(config, dict):
            raise ValueError("connection settings must be an object")
        address, backend_port = config.get("ssh_url"), config.get("backend_port")
        username, port, host, remote_port = ssh_endpoint(address, backend_port)
        auth_method = config.get("auth_method", "agent")
        if auth_method not in {"password", "agent", "key"}:
            raise ValueError("authentication method must be password, agent or key")
        for field in ("password", "passphrase", "key_path"):
            if field in config and (not isinstance(config[field], str) or len(config[field]) > 16384):
                raise ValueError(f"invalid {field} field")
        credentials = {field: config.get(field, "") for field in ("password", "passphrase", "key_path")}
        with self.ssh_lock:
            self.check_ssh_process()
            attempt = self.managed_ssh.connect({"host": host, "port": port, "username": username,
                "backend_port": remote_port, "auth_method": auth_method, **credentials})
            self.candidate_attempt = attempt
            uri_host = f"[{host}]" if ":" in host else host
            self.ssh_url = f"ssh://{username}@{uri_host}:{port}"
            self.backend_port = remote_port
            # The existing selected backend remains intact while this attempt
            # authenticates; no candidate receives the old backend's cookies.
            return attempt

    def disconnect_ssh(self) -> None:
        with self.ssh_lock:
            self.managed_ssh.disconnect()
            self.candidate_attempt = None
            if self.active_source == "managed":
                self.remote = None
                self.active_source = "none"
                self.active_attempt = None
                self.remote_generation = secrets.token_hex(8)
            self.ssh_url = ""
            self.backend_port = None


def start_servers(port: int, remote_url: str | None):
    remote = loopback_endpoint(remote_url) if remote_url else None
    local_httpd, local_workbench = gui.make_server(port=0, host="127.0.0.1")
    # A browser-facing gateway always has a local access gate. In particular,
    # clearing a password must not silently turn a former sign-in into open mode.
    # Direct standalone gui.py keeps its existing optional loopback gate policy.
    local_httpd.require_setup = True
    local_thread = threading.Thread(target=local_httpd.serve_forever, daemon=True)
    local_thread.start()
    try:
        gateway = GatewayServer(("127.0.0.1", port),
                                ("127.0.0.1", local_httpd.server_address[1]), remote)
    except Exception:
        local_httpd.shutdown()
        local_httpd.server_close()
        local_thread.join(timeout=5)
        gui._shutdown(local_workbench)
        raise
    return gateway, local_httpd, local_workbench, local_thread


def main() -> int:
    parser = argparse.ArgumentParser(description="One local frontend, SSH-configurable backend")
    parser.add_argument("--port", type=int, default=8877,
                        help="local frontend port (default: 8877)")
    parser.add_argument("--remote-url", default=None,
                        help="optional existing SSH tunnel; the UI can instead create its own")
    args = parser.parse_args()
    try:
        gateway, local, workbench, thread = start_servers(args.port, args.remote_url)
    except (OSError, ValueError) as exc:
        print(f"cannot start frontend gateway: {exc}")
        return 2
    print(f"Data Workbench frontend: http://127.0.0.1:{gateway.server_address[1]}/")
    print("Local backend: embedded on a private loopback port")
    print("Remote backend: " + (args.remote_url or "not configured"))
    print("Open the computation connection window in the page header. No automatic fallback.")
    try:
        gateway.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        # serve_forever() has already returned on this thread. Calling
        # shutdown() here would wait for itself and hang after Ctrl+C.
        gateway.disconnect_ssh()
        gateway.server_close()
        local.shutdown()
        local.server_close()
        thread.join(timeout=5)
        gui._shutdown(workbench)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
