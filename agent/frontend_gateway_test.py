"""HTTP smoke test for the frontend gateway without touching the real config."""
from __future__ import annotations

import http.cookiejar
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
from unittest import mock
import urllib.error
import urllib.request

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from gui_test import gui  # noqa: E402; installs a model SDK test double
import frontend_gateway as gateway  # noqa: E402


class Remote(BaseHTTPRequestHandler):
    old = False
    seen_cookie = ""

    def log_message(self, *_args):
        pass

    def send(self, status, payload, kind="application/json", cookie=None):
        self.send_response(status)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(payload)))
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        if self.path == "/api/state":
            value = {"engine_ready": True, "missing_fields": [], "i18n": {}}
            if self.old:
                value = {"old": True}
            self.send(200, json.dumps(value).encode())
        elif self.path == "/api/files":
            Remote.seen_cookie = self.headers.get("Cookie", "")
            self.send(200, b'{"backend":"remote"}')
        elif self.path == "/api/events?job=1":
            self.send(200, b'event: done\ndata: {"ok":true}\n\n', "text/event-stream")
        elif self.path == "/artifact?path=fake":
            self.send(200, b"remote-artifact", "text/plain")
        else:
            self.send(404, b"{}")

    def do_POST(self):
        if self.path == "/api/login":
            self.send(200, b'{"ok":true}', cookie="dws=remote-session; Path=/; HttpOnly")
        else:
            self.send(404, b"{}")


def main():
    with tempfile.TemporaryDirectory(prefix="gwb-test-") as scratch:
        os.environ["GPU_ANALYSIS_CONFIG"] = str(Path(scratch) / "connection.json")
        os.environ.pop("GPU_API_KEY", None)
        remote = ThreadingHTTPServer(("127.0.0.1", 0), Remote)
        remote_thread = threading.Thread(target=remote.serve_forever, daemon=True)
        remote_thread.start()
        remote_url = f"http://127.0.0.1:{remote.server_address[1]}"
        front, local, workbench, local_thread = gateway.start_servers(0, remote_url)
        front_thread = threading.Thread(target=front.serve_forever, daemon=True)
        front_thread.start()
        base = f"http://127.0.0.1:{front.server_address[1]}"
        opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

        def request(path, method="GET", payload=None):
            data = json.dumps(payload).encode() if payload is not None else None
            req = urllib.request.Request(base + path, data=data, method=method)
            if data is not None:
                req.add_header("Content-Type", "application/json")
            try:
                with opener.open(req, timeout=30) as reply:
                    return reply.status, reply.read(), reply.headers
            except urllib.error.HTTPError as error:
                return error.code, error.read(), error.headers

        try:
            code, body, _ = request("/api/backend")
            assert code == 200 and json.loads(body)["mode"] == "local"
            code, body, _ = request("/")
            assert code == 200 and b"backend-switch.js" in body
            code, body, _ = request("/backend-switch.js")
            assert code == 200 and b"ssh_url" in body
            code, body, _ = request("/api/backend/connection", "POST",
                                    {"ssh_url": "http://example.com", "backend_port": 8765})
            assert code == 400
            code, body, _ = request("/api/state")
            assert code == 200 and "engine_ready" in json.loads(body)
            Remote.old = True
            code, _, _ = request("/api/backend", "POST", {"mode": "remote"})
            assert code == 409, f"old backend was accepted: {code}"
            Remote.old = False
            code, body, _ = request("/api/backend", "POST", {"mode": "remote"})
            assert code == 200 and json.loads(body)["mode"] == "remote"
            code, body, _ = request("/api/files")
            assert code == 200 and json.loads(body)["backend"] == "remote"
            code, body, _ = request("/api/login", "POST", {"password": "test"})
            assert code == 200
            code, body, _ = request("/api/files")
            assert code == 200 and Remote.seen_cookie == "dws=remote-session"
            code, body, _ = request("/api/events?job=1")
            assert code == 200 and b"event: done" in body
            code, body, _ = request("/artifact?path=fake")
            assert code == 200 and body == b"remote-artifact"
            code, _, _ = request("/api/reset", "POST", {})
            assert code == 403
            code, _, _ = request("/api/shutdown", "POST", {})
            assert code == 403
            remote.shutdown()
            remote.server_close()
            remote_thread.join(timeout=5)
            code, body, _ = request("/api/files")
            assert code == 502 and json.loads(body)["mode"] == "remote"
            code, body, _ = request("/api/backend", "POST", {"mode": "local"})
            assert code == 200
            code, body, _ = request("/api/state")
            assert code == 200 and "engine_ready" in json.loads(body)
            code, body, _ = request("/api/backend/disconnect", "POST", {})
            assert code == 409 and b"outside the workbench" in body
            for invalid in ("https://127.0.0.1:9000", "http://example.com:9000",
                            "http://127.0.0.1:9000/path", "http://127.0.0.1"):
                try:
                    gateway.loopback_endpoint(invalid)
                except ValueError:
                    pass
                else:
                    raise AssertionError(f"accepted invalid remote endpoint: {invalid}")
            for invalid in ("http://user@host:22", "ssh://user:password@host:22",
                            "ssh://user@host:22/run", "ssh://user@host:22?x=1"):
                try:
                    gateway.ssh_endpoint(invalid, 8765)
                except ValueError:
                    pass
                else:
                    raise AssertionError(f"accepted invalid SSH endpoint: {invalid}")
            assert gateway.ssh_endpoint("ssh://Developer@example.org:6060", 8765) == (
                "Developer", 6060, "example.org", 8765)

            class FakeSSH:
                returncode = None
                terminated = False

                def poll(self):
                    return self.returncode

                def terminate(self):
                    self.terminated = True
                    self.returncode = 0

                def wait(self, timeout=None):
                    return 0

            fake = FakeSSH()
            commands = []
            def fake_popen(command, **kwargs):
                commands.append(command)
                return fake
            with mock.patch.object(gateway.shutil, "which", return_value="ssh"), \
                 mock.patch.object(gateway.subprocess, "Popen", side_effect=fake_popen):
                front.connect_ssh("ssh://Developer@example.org:6060", 8765)
                assert front.ssh_process is fake and front.remote[0] == "127.0.0.1"
                assert commands[0][-1] == "Developer@example.org"
                assert "8765" in commands[0][-2]
                front.disconnect_ssh()
                assert fake.terminated and front.remote is None
            print("PASS: local/remote switching, stale-version rejection, cookies, SSE, artifact, no fallback")
        finally:
            front.shutdown()
            front.server_close()
            front_thread.join(timeout=5)
            local.shutdown()
            local.server_close()
            local_thread.join(timeout=5)
            gui._shutdown(workbench)
            if remote_thread.is_alive():
                remote.shutdown()
                remote.server_close()
                remote_thread.join(timeout=5)


if __name__ == "__main__":
    main()
