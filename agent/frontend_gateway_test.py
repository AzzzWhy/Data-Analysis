"""Synthetic loopback gateway tests; no real SSH, keys, config, model or GPU.

ManagedSSH has its own real-Paramiko suite. Run beside frontend_gateway.py or use
--support-agent-dir (repeatable) and --asset-dir for a standalone scratch copy.
"""
from __future__ import annotations
import argparse
import http.cookiejar
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib
import json
from pathlib import Path
import sys
import threading
import time
import types
import unittest
from unittest import mock
import urllib.error
import urllib.request

HERE = Path(__file__).resolve().parent
parser = argparse.ArgumentParser(add_help=False)
parser.add_argument("--support-agent-dir", type=Path, action="append", default=[])
parser.add_argument("--asset-dir", type=Path, default=HERE / "gui")
options, unittest_args = parser.parse_known_args()
sys.path.insert(0, str(HERE))
for index, directory in enumerate(options.support_agent_dir, 1):
    sys.path.insert(index, str(directory))
# GatewayServer is the product under test. Avoid importing real skills/config.
gui = types.ModuleType("gui")
gui.CSP = "default-src 'self'; connect-src 'self'; script-src 'self'; style-src 'self'"
gui.BUILD_TAG = "synthetic-test-build"
sys.modules["gui"] = gui
gateway = importlib.import_module("frontend_gateway")


class Backend(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def send(self, status, body, kind="application/json", cookie=None):
        self.send_response(status)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self.server.requests.append((self.path, self.headers.get("Cookie", "")))
        if self.path == "/api/auth":
            self.send(200 if self.headers.get("Cookie") == "dws=local-session" else 401, b"{}")
        elif self.path == "/api/gate":
            self.send(200, b'{"mode":"signin"}')
        elif self.path == "/api/state":
            state = {"old": True} if self.server.old else {"engine_ready": True, "missing_fields": [], "i18n": {}}
            self.send(401 if self.server.force_unauthorized else 200, json.dumps(state).encode())
        elif self.path == "/api/files":
            self.send(200, json.dumps({"backend": self.server.label}).encode())
        elif self.path.startswith("/api/events?"):
            self.server.seen_event_id = self.headers.get("Last-Event-ID", "")
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            self.wfile.write(b'id: 18\nevent: progress\ndata: {"step":1}\n\n')
            self.wfile.flush()
            time.sleep(.3)
            self.wfile.write(b'id: 19\nevent: done\ndata: {"success":true}\n\n')
        elif self.path == "/artifact?path=fake":
            self.send(200, b"remote-artifact", "text/plain")
        else:
            self.send(404, b"{}")

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        self.server.post_requests.append((self.path, body))
        if self.path == "/api/login":
            session = "local-session" if self.server.label == "local" else "remote-session"
            self.send(200, b'{"ok":true}', cookie=f"dws={session}; Path=/; HttpOnly")
        elif self.path.split("?", 1)[0] == "/api/reset":
            # This fixture records routing only: never import or reset real GUI state.
            self.send(200, b'{"ok":true,"fixture":"synthetic-reset-only"}')
        else:
            self.send(404, b"{}")


class FakeManager:
    ACTIVE = {"connecting", "awaiting_host_key", "authenticating", "connected"}

    def __init__(self):
        self.state = {"attempt_id": "", "ssh_state": "disconnected", "error": "", "auth_method": None}
        self.target, self.calls, self.confirmations = None, [], []

    def snapshot(self):
        return dict(self.state)

    def endpoint(self):
        return self.target if self.state["ssh_state"] == "connected" else None

    def connect(self, config):
        if self.state["ssh_state"] in self.ACTIVE:
            raise RuntimeError("ssh_connection_active")
        self.calls.append(dict(config))
        attempt = f"candidate-{len(self.calls)}"
        self.state = {"attempt_id": attempt, "ssh_state": "connecting", "error": "", "auth_method": config["auth_method"]}
        self.target = None
        return attempt

    def succeed(self, endpoint):
        self.target = endpoint
        self.state["ssh_state"] = "connected"

    def await_host_key(self):
        self.state["ssh_state"] = "awaiting_host_key"
        self.state["host_key"] = {"host": "fixture.example", "port": 2222,
                                 "algorithm": "ssh-ed25519", "fingerprint": "SHA256:synthetic"}

    def confirm_host_key(self, attempt, accepted):
        if attempt != self.state["attempt_id"]:
            raise ValueError("stale_attempt")
        self.confirmations.append((attempt, accepted))
        self.state.update(ssh_state="authenticating" if accepted else "connection_failed",
                          error="" if accepted else "host_key_rejected")
        self.state.pop("host_key", None)

    def disconnect(self):
        self.target = None
        self.state["ssh_state"] = "disconnected"


class GatewayTests(unittest.TestCase):
    def setUp(self):
        self.servers = []
        self.addCleanup(self.close_servers)
        self.local, self.remote, self.replacement = [self.backend(name) for name in ("local", "remote", "replacement")]
        self.manager = FakeManager()
        for patch in (mock.patch.object(gateway, "ManagedSSH", return_value=self.manager),
                      mock.patch.object(gateway, "ROOT", options.asset_dir.resolve())):
            patch.start()
            self.addCleanup(patch.stop)
        self.front = gateway.GatewayServer(("127.0.0.1", 0), self.endpoint(self.local), self.endpoint(self.remote))
        self.start(self.front)
        self.base = f"http://127.0.0.1:{self.front.server_address[1]}"
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), urllib.request.HTTPCookieProcessor(self.jar))
        self.assertEqual(self.request("/api/login", "POST", {"password": "synthetic-local"})[0], 200)

    @staticmethod
    def endpoint(server):
        return "127.0.0.1", server.server_address[1]

    def backend(self, label):
        server = ThreadingHTTPServer(("127.0.0.1", 0), Backend)
        server.label, server.old, server.force_unauthorized = label, False, False
        server.requests, server.post_requests, server.seen_event_id = [], [], ""
        self.start(server)
        return server

    def start(self, server):
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .02}, daemon=True)
        thread.start()
        self.servers.append((server, thread))

    def close_servers(self):
        for server, thread in reversed(self.servers):
            if thread.is_alive():
                server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def request(self, path, method="GET", payload=None, headers=None):
        data = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method, headers=headers or {})
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            response = self.opener.open(req, timeout=4)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            return response.status, response.read(), response.headers

    def info(self):
        status, body, headers = self.request("/api/backend")
        self.assertEqual(status, 200)
        self.assertNotIn("Access-Control-Allow-Origin", headers)
        return json.loads(body)

    def mutate(self, path, payload, *, origin="same", nonce=True, headers=None):
        fields = dict(headers or {})
        if nonce:
            fields["X-GWB-CSRF"] = self.info()["csrf_token"] if nonce is True else nonce
        if origin is not None:
            fields["Origin"] = self.base if origin == "same" else origin
        return self.request(path, "POST", payload, fields)

    def connect(self):
        status, body, _ = self.mutate("/api/backend/connection", {
            "ssh_url": "ssh -p 2222 fixture@fixture.example", "backend_port": 8765,
            "auth_method": "password", "password": "synthetic-ssh-secret"})
        self.assertEqual(status, 202, body)
        return json.loads(body)["attempt_id"]

    def use_remote(self):
        status, body, _ = self.mutate("/api/backend", {"mode": "remote"})
        self.assertEqual(status, 200, body)

    def test_origin_aliases_null_and_cli_need_authenticated_nonce(self):
        port = self.front.server_address[1]
        for origin in (self.base, f"http://localhost:{port}", f"http://[::1]:{port}", "null", None):
            with self.subTest(origin=origin):
                self.assertEqual(self.mutate("/api/backend", {"mode": "local"}, origin=origin)[0], 200)
                self.assertEqual(self.mutate("/api/backend", {"mode": "local"}, origin=origin, nonce=False)[0], 403)

    def test_portless_loopback_origins_require_valid_nonce_and_local_login(self):
        for origin in ("http://127.0.0.1", "http://localhost", "http://[::1]"):
            for suffix in ("", "/"):
                with self.subTest(origin=origin + suffix):
                    for nonce in (False, "invalid-nonce"):
                        status, body, _ = self.mutate("/api/backend", {"mode": "local"},
                                                      origin=origin + suffix, nonce=nonce)
                        self.assertEqual(status, 403, body)
                        self.assertEqual(json.loads(body)["code"], "csrf_failed")
                    self.assertEqual(self.mutate("/api/backend", {"mode": "local"},
                                                 origin=origin + suffix)[0], 200)
                    # A real missing local cookie, not a mocked authorization result.
                    token = self.info()["csrf_token"]
                    status, _, _ = self.mutate("/api/backend", {"mode": "local"},
                                               origin=origin + suffix, nonce=token,
                                               headers={"Cookie": ""})
                    self.assertEqual(status, 401)

    def test_portless_origin_does_not_relax_host_port_matching(self):
        for host in ("127.0.0.1", "localhost", "[::1]", "127.0.0.1:80",
                     "localhost:1", "[::1]:1", "foreign.example"):
            with self.subTest(host=host):
                status, body, _ = self.mutate("/api/backend", {"mode": "local"},
                                              origin="http://127.0.0.1", headers={"Host": host})
                self.assertEqual(status, 403, body)
                self.assertEqual(json.loads(body)["code"], "origin_rejected")
        for host in ("127.0.0.1", "localhost", "[::1]"):
            with self.subTest(valid_host=host):
                self.assertEqual(self.mutate("/api/backend", {"mode": "local"},
                                             origin="http://127.0.0.1",
                                             headers={"Host": f"{host}:{self.front.server_address[1]}"})[0], 200)

    def test_foreign_origin_and_foreign_host_rejected_even_with_nonce(self):
        for origin in ("https://foreign.example", "https://127.0.0.1:8877", "http://127.0.0.1:1",
                       "http://127.0.0.1:80", "http://localhost:80", "http://[::1]:80",
                       "http://localhost:1", "http://[::1]:1", "https://localhost",
                       "http://127.0.0.1/other", "http://127.0.0.1?other=1", "http://127.0.0.1#fragment",
                       "http://fixture@127.0.0.1", "http://127.0.0.1.foreign.example",
                       f"http://127.0.0.1:{self.front.server_address[1]}@foreign.example"):
            status, body, _ = self.mutate("/api/backend", {"mode": "local"}, origin=origin)
            self.assertEqual(status, 403)
            self.assertEqual(json.loads(body)["code"], "origin_rejected")
        self.assertEqual(self.mutate("/api/backend", {"mode": "local"}, headers={"Host": "foreign.example"})[0], 403)

    def test_local_reset_requires_origin_login_and_nonce_before_forwarding(self):
        self.local.post_requests.clear()
        self.remote.post_requests.clear()
        payload = {"confirmation": "synthetic fixture only"}
        for path in ("/api/reset", "/api/reset?source=fixture"):
            for nonce in (False, "invalid-nonce"):
                with self.subTest(path=path, nonce=nonce):
                    status, body, _ = self.mutate(path, payload, nonce=nonce)
                    self.assertEqual(status, 403, body)
                    self.assertEqual(json.loads(body)["code"], "csrf_failed")
            self.assertEqual(self.mutate(path, payload, origin="https://foreign.example")[0], 403)
            token = self.info()["csrf_token"]
            self.assertEqual(self.mutate(path, payload, nonce=token, headers={"Cookie": ""})[0], 401)
            self.assertEqual(self.local.post_requests, [])
            self.assertEqual(self.remote.post_requests, [])
        status, body, _ = self.mutate("/api/reset", payload, origin="http://127.0.0.1")
        self.assertEqual(status, 200, body)
        self.assertEqual(json.loads(body)["fixture"], "synthetic-reset-only")
        self.assertEqual(self.local.post_requests, [("/api/reset", json.dumps(payload).encode())])
        self.assertEqual(self.remote.post_requests, [])

    def test_remote_reset_rejected_even_with_valid_origin_login_and_nonce(self):
        self.use_remote()
        self.local.post_requests.clear()
        self.remote.post_requests.clear()
        for origin in ("same", "http://127.0.0.1", "http://localhost", "http://[::1]", "null", None):
            for path in ("/api/reset", "/api/reset?source=fixture"):
                with self.subTest(origin=origin, path=path):
                    status, body, _ = self.mutate(path, {"confirmation": "synthetic fixture only"}, origin=origin)
                    self.assertEqual(status, 403, body)
                    self.assertEqual(json.loads(body)["error"], "remote reset is disabled in the gateway")
        self.assertEqual(self.remote.post_requests, [], "reset reached the remote backend")
        self.assertEqual(self.local.post_requests, [], "remote reset fell back to the local backend")

    def test_start_servers_requires_setup_before_embedded_backend_serves(self):
        started = threading.Event()
        observed_setup = []

        class EmbeddedServer(ThreadingHTTPServer):
            def serve_forever(self, poll_interval=.02):
                observed_setup.append(getattr(self, "require_setup", False))
                started.set()
                super().serve_forever(poll_interval=poll_interval)

        local = EmbeddedServer(("127.0.0.1", 0), Backend)
        workbench = object()  # No real Workbench or user configuration is loaded.
        with mock.patch.object(gui, "make_server", return_value=(local, workbench), create=True) as make_server:
            front, returned_local, returned_workbench, thread = gateway.start_servers(0, None)
        self.servers.append((local, thread))
        self.addCleanup(front.server_close)
        self.assertTrue(started.wait(timeout=1))
        make_server.assert_called_once_with(port=0, host="127.0.0.1")
        self.assertIs(returned_local, local)
        self.assertIs(returned_workbench, workbench)
        self.assertIs(local.require_setup, True)
        self.assertEqual(observed_setup, [True], "embedded backend briefly served before setup was mandatory")

    def test_nonce_bound_to_cookie_gateway_lifetime_and_login(self):
        token = self.info()["csrf_token"]
        self.assertEqual(self.mutate("/api/backend", {"mode": "local"}, nonce="invalid")[0], 403)
        with mock.patch.object(gateway.GatewayHandler, "local_authorized", return_value=True):
            self.assertEqual(self.mutate("/api/backend", {"mode": "local"}, nonce=token,
                                        headers={"Cookie": "gwb_local=another-signin"})[0], 403)
        self.front.csrf_secret = b"another-gateway-lifetime"
        self.assertEqual(self.mutate("/api/backend", {"mode": "local"}, nonce=token)[0], 403)
        token = self.info()["csrf_token"]
        with mock.patch.object(gateway.GatewayHandler, "local_authorized", return_value=False):
            self.assertNotIn("csrf_token", self.info())
            self.assertEqual(self.mutate("/api/backend", {"mode": "local"}, nonce=token)[0], 401)

    def test_local_assets_and_remote_login_recovery(self):
        status, body, _ = self.request("/")
        self.assertEqual(status, 200)
        self.assertIn(b"backend-switch.js", body)
        status, body, _ = self.request("/backend-switch.js")
        self.assertEqual(status, 200)
        self.assertIn(b"ssh_url", body)
        self.use_remote()
        self.remote.force_unauthorized = True
        status, body, _ = self.request("/")
        self.assertEqual(status, 200)
        self.assertIn(b"/login.js", body)
        self.assertIn(b"/backend-switch.js", body)
        self.assertNotIn(b"/app.js", body)

    def test_old_backend_refused_and_unavailable_remote_does_not_fall_back(self):
        self.remote.old = True
        self.assertEqual(self.mutate("/api/backend", {"mode": "remote"})[0], 409)
        self.remote.old = False
        self.use_remote()
        self.remote.shutdown()
        self.remote.server_close()
        self.assertEqual(self.info()["status"], "backend_unavailable")
        status, body, _ = self.request("/api/files")
        self.assertEqual(status, 502)
        self.assertEqual(json.loads(body)["mode"], "remote")
        self.assertEqual(self.mutate("/api/backend", {"mode": "local"})[0], 200)
        self.assertEqual(json.loads(self.request("/api/files")[1])["backend"], "local")

    def test_local_gate_protects_all_remote_forwarding(self):
        self.use_remote()
        with mock.patch.object(gateway.GatewayHandler, "local_authorized", return_value=False):
            for path in ("/api/files", "/artifact?path=fake", "/api/events?job=1"):
                self.assertEqual(self.request(path)[0], 401)
            self.assertEqual(self.request("/api/run", "POST", {"tool": "list_datasets"})[0], 401)
            self.assertIn(b"/login.js", self.request("/")[1])
            self.assertEqual(json.loads(self.request("/api/gate")[1])["mode"], "signin")

    def test_expired_local_login_clears_stale_remote_mode_on_local_gate(self):
        self.use_remote()
        remote_endpoint, generation = self.front.remote, self.front.remote_generation
        # Mimic another browser resetting the PC gate: retain the mode cookie,
        # but remove local authentication. No real reset or settings are invoked.
        for cookie in list(self.jar):
            if cookie.name == "gwb_local":
                self.jar.clear(cookie.domain, cookie.path, cookie.name)
        self.remote.requests.clear()
        for path in ("/", "/index.html"):
            with self.subTest(path=path):
                for cookie in self.jar:
                    if cookie.name == "gwbmode":
                        cookie.value = "remote"
                self.assertEqual(self.info(), {
                    "mode": "local", "status": "disconnected", "ssh_state": "disconnected",
                    "remote_configured": False, "ssh_managed": False,
                    "connection_source": "none", "error": "local_login_required"})
                status, body, headers = self.request(path)
                self.assertEqual(status, 200, body)
                self.assertIn(b"/login.js", body)
                self.assertNotIn(b"/app.js", body)
                self.assertIn("gwbmode=local; Path=/; HttpOnly; SameSite=Strict",
                              headers.get_all("Set-Cookie", []))
                self.assertEqual([cookie.value for cookie in self.jar if cookie.name == "gwbmode"], ["local"])
        self.assertEqual(self.remote.requests, [], "local setup gate contacted a stale remote backend")
        self.assertEqual(self.front.remote, remote_endpoint, "clearing browser mode disconnected another browser")
        self.assertEqual(self.front.remote_generation, generation)

    def test_remote_cookie_generation_idle_sse_and_artifacts(self):
        self.use_remote()
        self.assertEqual(self.request("/api/login", "POST", {"password": "synthetic-remote"})[0], 200)
        self.request("/api/files")
        self.assertEqual(self.remote.requests[-1][1], "dws=remote-session")
        self.front.remote_generation = "replacement-generation"
        self.request("/api/files")
        self.assertEqual(self.remote.requests[-1][1], "")
        with mock.patch.object(gateway, "UPSTREAM_TIMEOUT_SECONDS", .1):
            status, body, _ = self.request("/api/events?job=1", headers={"Last-Event-ID": "17"})
        self.assertEqual(status, 200)
        self.assertIn(b"event: done", body)
        self.assertEqual(self.remote.seen_event_id, "17")
        self.assertEqual(self.request("/artifact?path=fake")[1], b"remote-artifact")
        self.assertEqual(self.request("/api/reset", "POST", {})[0], 403)
        self.assertEqual(self.request("/api/shutdown", "POST", {})[0], 403)

    def test_candidate_connect_and_failure_preserve_but_do_not_masquerade_as_external(self):
        self.use_remote()
        original, generation = self.front.remote, self.front.remote_generation
        attempt = self.connect()
        info = self.info()
        self.assertEqual(info["attempt_id"], attempt)
        self.assertEqual(info["connection_source"], "managed")
        self.assertEqual(info["status"], "connecting")
        self.assertEqual(self.front.remote, original)
        self.assertEqual(self.front.remote_generation, generation)
        self.manager.state.update(ssh_state="authentication_failed", error="authentication_failed")
        info = self.info()
        self.assertEqual(info["ssh_state"], "authentication_failed")
        self.assertNotEqual(info["status"], "connected")
        self.assertFalse(info["remote_configured"])
        self.assertEqual(self.front.remote, original)
        self.assertEqual(self.mutate("/api/backend", {"mode": "remote"})[0], 409)
        self.assertEqual(json.loads(self.request("/api/files")[1])["backend"], "remote")

    def test_connected_candidate_requires_explicit_use(self):
        self.use_remote()
        self.connect()
        self.manager.succeed(self.endpoint(self.replacement))
        self.assertEqual(self.info()["status"], "connected")
        self.assertEqual(json.loads(self.request("/api/files")[1])["backend"], "remote")
        self.use_remote()
        self.assertEqual(json.loads(self.request("/api/files")[1])["backend"], "replacement")
        self.assertEqual(self.front.active_attempt, self.front.candidate_attempt)

    def test_duplicate_connect_409_preserves_original_attempt(self):
        attempt = self.connect()
        status, body, _ = self.mutate("/api/backend/connection", {
            "ssh_url": "ssh://fixture@other.example:22", "backend_port": 8765, "auth_method": "agent"})
        self.assertEqual(status, 409, body)
        self.assertEqual(self.front.candidate_attempt, attempt)
        self.assertEqual(len(self.manager.calls), 1)

    def test_host_key_route_validates_attempt_boolean_and_nonce(self):
        attempt = self.connect()
        self.manager.await_host_key()
        info = self.info()
        self.assertEqual(info["ssh_state"], "awaiting_host_key")
        self.assertEqual(info["host_key"]["fingerprint"], "SHA256:synthetic")
        for payload in ({"attempt_id": "old", "accept": True}, {"attempt_id": attempt, "accept": "true"}):
            self.assertEqual(self.mutate("/api/backend/host-key", payload)[0], 400)
        self.assertEqual(self.mutate("/api/backend/host-key", {"attempt_id": attempt, "accept": True}, nonce=False)[0], 403)
        self.assertEqual(self.manager.confirmations, [])
        self.assertEqual(self.mutate("/api/backend/host-key", {"attempt_id": attempt, "accept": False})[0], 200)
        self.assertEqual(self.manager.confirmations, [(attempt, False)])
        self.assertEqual(self.info()["error"], "host_key_rejected")

    def test_candidate_cookie_isolation_when_loopback_endpoint_is_reused(self):
        self.use_remote()
        self.request("/api/login", "POST", {"password": "synthetic-remote"})
        self.assertEqual(self.info()["status"], "connected")
        self.assertEqual(self.remote.requests[-1][1], "dws=remote-session")
        self.connect()
        self.manager.succeed(self.endpoint(self.remote))  # same port, a new identity
        self.assertEqual(self.info()["status"], "connected")
        self.assertEqual(self.remote.requests[-1][1], "", "candidate probe leaked old target cookie")
        self.use_remote()
        self.assertEqual(self.remote.requests[-1][1], "", "explicit-use probe leaked old target cookie")
        self.request("/api/files")
        self.assertEqual(self.remote.requests[-1][1], "", "new activation reused old target cookie")

    def test_disconnect_managed_does_not_implicitly_restore_external(self):
        self.assertEqual(self.mutate("/api/backend/disconnect", {})[0], 409)
        self.connect()
        self.manager.succeed(self.endpoint(self.replacement))
        self.use_remote()
        self.assertEqual(self.mutate("/api/backend/disconnect", {})[0], 200)
        self.assertIsNone(self.front.remote)
        self.assertIsNone(self.front.candidate_attempt)
        self.assertEqual(self.info()["mode"], "local")
        self.assertEqual(self.front.external_remote, self.endpoint(self.remote))

    def test_command_and_uri_parser_reject_shell_and_unsafe_ports(self):
        expected = ("fixture", 2222, "example.org", 8765)
        for value in ("ssh://fixture@example.org:2222", "ssh -p 2222 fixture@example.org", "ssh -p2222 fixture@example.org"):
            self.assertEqual(gateway.ssh_endpoint(value, 8765), expected)
        self.assertEqual(gateway.ssh_endpoint("ssh fixture@example.org", 8765), ("fixture", 22, "example.org", 8765))
        for value in ("http://fixture@host:22", "ssh://fixture:password@host:22", "ssh://fixture@host:22/run",
                      "ssh://fixture@host:22?x=1", "ssh://-option@host:22", "ssh -o ProxyCommand=calc fixture@host",
                      "ssh fixture@host uname", "ssh fixture@host;calc", "ssh fixture@host && calc",
                      "ssh -L 9999:host:80 fixture@host", "ssh -i private_key fixture@host", "ssh $(calc)@host",
                      "ssh://fixture@host:0"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                gateway.ssh_endpoint(value, 8765)
        for backend_port in (True, 0, 65536, "22.5", "-1", "١٢٣"):
            with self.subTest(backend_port=backend_port), self.assertRaises(ValueError):
                gateway.ssh_endpoint("ssh://fixture@example.org:2222", backend_port)
        for invalid in ("https://127.0.0.1:9000", "http://example.org:9000", "http://127.0.0.1:9000/path", "http://127.0.0.1"):
            with self.assertRaises(ValueError):
                gateway.loopback_endpoint(invalid)

    def test_secrets_not_returned_and_password_in_uri_rejected(self):
        self.connect()
        self.assertEqual(self.manager.calls[-1]["password"], "synthetic-ssh-secret")
        self.assertNotIn("synthetic-ssh-secret", json.dumps(self.info()))
        self.assertNotIn("key_path", self.info())
        status, body, _ = self.mutate("/api/backend/connection", {
            "ssh_url": "ssh://fixture:synthetic-ssh-secret@host:22", "backend_port": 8765})
        self.assertEqual(status, 400)
        self.assertNotIn(b"synthetic-ssh-secret", body)


if __name__ == "__main__":
    unittest.main(argv=[sys.argv[0], *unittest_args])
