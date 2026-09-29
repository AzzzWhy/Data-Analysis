#!/usr/bin/env python3
"""One local workbench URL with a selectable local or SSH-tunnelled backend.

The browser always receives this checkout's GUI assets. API, SSE and artifact
requests go to the selected backend. A remote backend must be exposed on a
*local* loopback port by an independently managed SSH tunnel; this process
never handles an SSH password or exposes an arbitrary proxy target.
"""
from __future__ import annotations

import argparse
from http import HTTPStatus
from http.client import HTTPConnection
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import threading
from urllib.parse import urlsplit

import gui


ROOT = Path(__file__).resolve().parent / "gui"
MAX_BODY = 1024 * 1024
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
    """Parse an SSH address, never a URL containing a password or remote command."""
    parsed = urlsplit(value)
    try:
        ssh_port = parsed.port or 22
        username = parsed.username
        port = int(backend_port)
    except (TypeError, ValueError) as exc:
        raise ValueError("SSH or backend port is invalid") from exc
    host = parsed.hostname or ""
    if (parsed.scheme != "ssh" or not username or not re.fullmatch(r"[A-Za-z0-9_.-]+", username)
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]*", host)
            or parsed.password is not None or parsed.path not in ("", "/")
            or parsed.query or parsed.fragment or not 1 <= ssh_port <= 65535
            or not 1 <= port <= 65535):
        raise ValueError("use ssh://USER@HOST:PORT without a password; backend port must be 1-65535")
    return username, ssh_port, host, port


def available_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


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
            print("gateway: " + fmt % args)

    def mode(self) -> str:
        selected = cookies(self.headers.get("Cookie", "")).get("gwbmode")
        return "remote" if selected and selected.value == "remote" else "local"

    def backend(self, mode: str) -> tuple[str, int] | None:
        return self.server.remote if mode == "remote" else self.server.local

    def backend_cookie(self, mode: str) -> str:
        value = cookies(self.headers.get("Cookie", "")).get("gwb_" + mode)
        return "dws=" + value.value if value and value.value else ""

    def reply(self, code: int, body: bytes, content_type: str,
              headers: list[tuple[str, str]] = ()) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Security-Policy", gui.CSP)
        self.send_header("X-Content-Type-Options", "nosniff")
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
                        body: bytes | None = None, timeout: float = 30):
        endpoint = self.backend(mode)
        if endpoint is None:
            raise OSError("remote backend is not configured")
        connection = HTTPConnection(*endpoint, timeout=timeout)
        headers = {"Accept": self.headers.get("Accept", "*/*"),
                   "User-Agent": "gpu-workbench-gateway", "Connection": "close"}
        if body is not None:
            headers["Content-Type"] = self.headers.get("Content-Type", "application/json")
        auth_cookie = self.backend_cookie(mode)
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
            connection, response = self.backend_request("local", "GET", "/api/state", timeout=5)
            status = response.status
            response.read()
            connection.close()
            return status == 200
        except (OSError, TimeoutError):
            return False

    def connection_info(self) -> dict:
        self.server.check_ssh_process()
        endpoint = self.server.remote
        status = "disconnected" if endpoint is None else "connecting"
        if endpoint is not None:
            try:
                connection, response = self.backend_request("remote", "GET", "/api/state",
                                                            timeout=1.5)
                code = response.status
                raw = response.read()
                connection.close()
                if code == 401:
                    status = "connected"
                elif code == 200:
                    try:
                        state = json.loads(raw)
                    except ValueError:
                        state = {}
                    status = "connected" if all(key in state for key in
                        ("engine_ready", "missing_fields", "i18n")) else "incompatible"
            except (OSError, TimeoutError):
                pass
        info = {"mode": self.mode(), "status": status,
                "remote_configured": endpoint is not None,
                "ssh_managed": self.server.ssh_process is not None,
                "error": self.server.ssh_error}
        if self.local_authorized():
            info["ssh_url"] = self.server.ssh_url
            info["backend_port"] = self.server.backend_port
        return info

    def forward(self, mode: str, method: str, body: bytes | None = None) -> None:
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
        except (OSError, TimeoutError) as exc:
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
                    name = "gwb_" + mode
                    suffix = "; Path=/; HttpOnly; SameSite=Strict"
                    if "Max-Age=0" in value or not upstream_cookie.value:
                        suffix += "; Max-Age=0"
                    extra.append(("Set-Cookie", f"{name}={upstream_cookie.value}{suffix}"))
            if upstream_type.startswith("text/event-stream"):
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

    def static(self, name: str, content_type: str, inject: bool = False) -> None:
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
        self.reply(200, body, content_type)

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
            try:
                connection, response = self.backend_request(mode, "GET", "/api/state")
                status = response.status
                response.read()
                connection.close()
            except (OSError, TimeoutError) as exc:
                if mode == "remote":
                    # Keep the selector available when a selected SSH tunnel drops.
                    self.static("index.html", "text/html; charset=utf-8", inject=True)
                    return
                self.json_reply(502, {"error": "selected backend is unavailable",
                                      "mode": mode, "detail": type(exc).__name__})
                return
            if status == 401:
                self.forward(mode, "GET")  # backend renders its own sign-in page
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
        if path in ("/api/backend", "/api/backend/connection", "/api/backend/disconnect"):
            origin = self.headers.get("Origin")
            if origin and origin != f"http://{self.headers.get('Host')}":
                self.json_reply(403, {"error": "cross-origin backend changes are refused"})
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
                self.server.connect_ssh(config["ssh_url"], config["backend_port"])
            except (ValueError, KeyError, TypeError) as exc:
                self.json_reply(400, {"error": str(exc)})
                return
            except OSError as exc:
                self.json_reply(502, {"error": f"could not launch SSH: {exc}"})
                return
            self.json_reply(202, {"ok": True, "status": "connecting"})
            return
        if path == "/api/backend/disconnect":
            if not self.local_authorized():
                self.json_reply(401, {"error": "sign in to this PC before disconnecting SSH"})
                return
            if self.server.ssh_process is None:
                self.json_reply(409, {"error": "this tunnel was started outside the workbench; "
                                             "close it in its own terminal"})
                return
            self.server.disconnect_ssh()
            self.json_reply(200, {"ok": True, "mode": "local"},
                            [("Set-Cookie", "gwbmode=local; Path=/; HttpOnly; SameSite=Strict")])
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
                self.server.check_ssh_process()
                if not self.server.remote:
                    self.json_reply(409, {"error": "connect to an SSH backend first"})
                    return
                try:
                    connection, response = self.backend_request("remote", "GET", "/api/state",
                                                                timeout=5)
                    status = response.status
                    payload = response.read()
                    connection.close()
                except (OSError, TimeoutError):
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
                    if not all(key in state for key in ("engine_ready", "missing_fields", "i18n")):
                        self.json_reply(409, {"error": "remote backend is too old for this frontend; "
                                                      "update it before switching"})
                        return
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
        self.verbose = verbose
        self.ssh_process: subprocess.Popen | None = None
        self.ssh_url = ""
        self.backend_port: int | None = None
        self.ssh_error = ""
        self.ssh_lock = threading.RLock()

    def check_ssh_process(self) -> None:
        with self.ssh_lock:
            if self.ssh_process is not None and self.ssh_process.poll() is not None:
                self.ssh_error = f"SSH exited ({self.ssh_process.returncode}); check its terminal"
                self.ssh_process = None
                self.remote = None

    def connect_ssh(self, address: str, backend_port: object) -> None:
        username, port, host, remote_port = ssh_endpoint(address, backend_port)
        ssh_executable = shutil.which("ssh")
        if ssh_executable is None:
            raise OSError("OpenSSH client (ssh) is not installed")
        with self.ssh_lock:
            self.check_ssh_process()
            if self.ssh_process is not None:
                raise ValueError("disconnect the current SSH tunnel before changing its address")
            local_port = available_loopback_port()
            command = [ssh_executable, "-N", "-o", "ExitOnForwardFailure=yes",
                       "-o", "ServerAliveInterval=30", "-p", str(port),
                       "-L", f"127.0.0.1:{local_port}:127.0.0.1:{remote_port}",
                       f"{username}@{host}"]
            if os.name == "nt":
                process = subprocess.Popen(command, creationflags=subprocess.CREATE_NEW_CONSOLE)
            else:
                # On headless hosts, rely on a configured SSH key or agent.
                command.insert(1, "-o")
                command.insert(2, "BatchMode=yes")
                process = subprocess.Popen(command, stdin=subprocess.DEVNULL,
                                           stdout=subprocess.DEVNULL,
                                           stderr=subprocess.DEVNULL)
            self.ssh_process = process
            self.ssh_url = address
            self.backend_port = remote_port
            self.remote = ("127.0.0.1", local_port)
            self.ssh_error = ""

    def disconnect_ssh(self) -> None:
        with self.ssh_lock:
            process = self.ssh_process
            self.ssh_process = None
            self.remote = None
            self.ssh_error = ""
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


def start_servers(port: int, remote_url: str | None):
    remote = loopback_endpoint(remote_url) if remote_url else None
    local_httpd, local_workbench = gui.make_server(port=0, host="127.0.0.1")
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
        gateway.shutdown()
        gateway.disconnect_ssh()
        gateway.server_close()
        local.shutdown()
        local.server_close()
        thread.join(timeout=5)
        gui._shutdown(workbench)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
