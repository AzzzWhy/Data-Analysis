"""Isolated HTTP reset/login-gate regressions; never reset a real workbench.

Only temporary configuration files and ephemeral loopback ports are used. The
model and worker boundaries are doubles installed before importing gui. Tests
seed synthetic legacy digests; they never submit /api/setup or real credentials.
"""
from __future__ import annotations

import argparse
from dataclasses import replace
from email.message import Message
import hashlib
import http.client
import importlib
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import types
import unittest
from unittest import mock


parser = argparse.ArgumentParser(add_help=False)
parser.add_argument("--agent-dir", type=Path, default=Path(__file__).resolve().parent)
parser.add_argument("--support-agent-dir", type=Path, action="append", default=[])
options, unittest_args = parser.parse_known_args()
sys.path.insert(0, str(options.agent_dir.resolve()))
for index, directory in enumerate(options.support_agent_dir, start=1):
    sys.path.insert(index, str(directory.resolve()))

skills = types.ModuleType("skills")
skills.release_all_sessions_and_cache = lambda: {
    "closed": 0, "sessions_after": 0, "warm_frames_after": 0, "detail": {"ok": True}}
sys.modules["skills"] = skills
model = types.ModuleType("agent_main")
model.Agent = mock.Mock
model.build_client = mock.Mock(side_effect=AssertionError("model networking is forbidden"))
sys.modules["agent_main"] = model
sdk = types.ModuleType("openai")
sdk.OpenAI = mock.Mock
sys.modules["openai"] = sdk
gui = importlib.import_module("gui")


class ResetGateTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="gui-reset-fixture-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.config_path = self.root / "connection.json"
        self.gate_path = self.root / "gate.json"
        self.config = gui.APIConfig(base_url="http://127.0.0.1:1/v1", model="", api_key="")
        self.env = mock.patch.dict(os.environ, {
            "GPU_ANALYSIS_CONFIG": str(self.config_path), "GPU_GUI_ALLOW_REMOTE": "0",
            "DEMO_DATA_DIR": str(self.root),
        })
        self.env.start()
        self.addCleanup(self.env.stop)
        self.load = mock.patch.object(gui, "load_config", side_effect=lambda: replace(self.config)).start()
        self.addCleanup(mock.patch.stopall)
        # The real writer receives only the synthetic default config at the forced
        # temporary path. Any accidental home-profile fallback fails immediately.
        mock.patch.object(gui, "_config_root", side_effect=AssertionError("real config root forbidden")).start()
        mock.patch.object(gui, "save_config", wraps=gui.save_config).start()
        self.servers = []
        self.addCleanup(self.stop_servers)

    def stop_servers(self):
        while self.servers:
            server, thread = self.servers.pop()
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def seed_legacy_gate(self):
        salt = b"fixture-salt-1234"
        password = "synthetic-fixture-only"
        digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 1)
        payload = {"scheme": "pbkdf2-sha256", "iterations": 1,
                   "salt": salt.hex(), "digest": digest.hex()}
        self.gate_path.write_text(json.dumps(payload), encoding="utf-8")
        return password

    def start(self, *, token=None):
        server, wb = gui.make_server(port=0, token=token)
        thread = threading.Thread(target=server.serve_forever,
                                  kwargs={"poll_interval": .01}, daemon=True)
        thread.start()
        self.servers.append((server, thread))
        return server, wb

    def request(self, server, path, *, data=None, headers=None, cookie=None):
        self.assertNotEqual(path, "/api/setup", "tests must not create a password through setup")
        conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=3)
        given = dict(headers or {})
        if cookie:
            given["Cookie"] = cookie
        if data is not None:
            given.setdefault("Content-Type", "application/json")
        try:
            conn.request("POST" if data is not None else "GET", path,
                         body=json.dumps(data).encode() if data is not None else None,
                         headers=given)
            response = conn.getresponse()
            payload = json.loads(response.read())
            return response.status, payload, dict(response.getheaders())
        finally:
            conn.close()

    def mode(self, server):
        status, payload, _ = self.request(server, "/api/gate")
        self.assertEqual(status, 200)
        return payload["mode"]

    def login(self, server, password):
        status, payload, headers = self.request(server, "/api/login", data={"password": password})
        self.assertEqual(status, 200, payload)
        return headers["Set-Cookie"].split(";", 1)[0]

    def authenticated_server(self):
        password = self.seed_legacy_gate()
        server, wb = self.start()
        cookie = self.login(server, password)
        return server, wb, cookie, password

    def assert_unchanged_gate(self, server, gate_bytes, credential, sessions):
        self.assertEqual(self.gate_path.read_bytes(), gate_bytes)
        self.assertEqual(server.gate_credential, credential)
        self.assertEqual(server.sessions, sessions)
        self.assertFalse(server.require_setup)
        self.assertEqual(self.mode(server), "signin")

    def test_fresh_standalone_loopback_stays_open(self):
        server, _ = self.start()
        self.assertEqual(self.mode(server), "open")
        self.assertFalse(self.gate_path.exists())
        self.assertEqual(self.request(server, "/api/jobs")[0], 200)

    def test_explicit_reset_from_open_requires_setup_and_survives_restart(self):
        server, _ = self.start()
        status, payload, headers = self.request(server, "/api/reset", data={})
        self.assertEqual(status, 200, payload)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["gate_mode"], "setup")
        self.assertIn("Max-Age=0", headers["Set-Cookie"])
        self.assertEqual(json.loads(self.gate_path.read_text()), {"setup_required": True})
        self.assertEqual(self.mode(server), "setup")
        self.assertEqual(self.request(server, "/api/jobs")[0], 401)
        self.stop_servers()
        restarted, _ = self.start()
        self.assertTrue(restarted.require_setup)
        self.assertEqual(self.mode(restarted), "setup")
        self.assertEqual(self.request(restarted, "/api/jobs")[0], 401)

    def test_password_reset_revokes_sessions_and_removes_legacy_digest(self):
        server, wb, cookie, password = self.authenticated_server()
        old_instance = wb.instance_id
        status, payload, _ = self.request(server, "/api/reset", data={}, cookie=cookie)
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["gate_mode"], "setup")
        self.assertIsNone(server.gate_credential)
        self.assertEqual(server.sessions, {})
        self.assertNotEqual(wb.instance_id, old_instance)
        self.assertEqual(self.request(server, "/api/jobs", cookie=cookie)[0], 401)
        self.assertEqual(self.request(server, "/api/login", data={"password": password})[0], 409)
        stored = json.loads(self.gate_path.read_text())
        self.assertEqual(stored, {"setup_required": True})
        self.assertNotIn(password, self.gate_path.read_text())

    def test_token_remains_signin_after_reset_and_ignores_marker_on_restart(self):
        self.gate_path.write_text('{"setup_required": true}', encoding="utf-8")
        token = "synthetic-launch-token"
        server, _ = self.start(token=token)
        self.assertEqual(self.mode(server), "signin")
        cookie = self.login(server, token)
        digest = server.token_digest
        status, payload, _ = self.request(server, "/api/reset", data={}, cookie=cookie)
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["gate_mode"], "signin")
        self.assertEqual(server.token_digest, digest)
        self.assertEqual(server.sessions, {})
        self.assertEqual(self.request(server, "/api/jobs", cookie=cookie)[0], 401)
        self.login(server, token)
        self.stop_servers()
        restarted, _ = self.start(token=token)
        self.assertEqual(self.mode(restarted), "signin")
        self.assertFalse(restarted.require_setup)

    def test_gateway_override_requires_setup_but_credential_takes_priority(self):
        server, _ = self.start()
        server.require_setup = True
        self.assertEqual(self.mode(server), "setup")
        self.assertFalse(self.gate_path.exists())
        self.seed_legacy_gate()
        server.gate_credential = gui.load_gate_credential()
        self.assertEqual(self.mode(server), "signin")

    def test_legacy_digest_without_marker_is_still_compatible(self):
        password = self.seed_legacy_gate()
        server, _ = self.start()
        self.assertFalse(server.require_setup)
        self.assertEqual(self.mode(server), "signin")
        self.login(server, password)
        self.stop_servers()
        # A password record written later replaces, rather than coexists with,
        # the reset marker. This uses a synthetic fixture, not /api/setup.
        self.gate_path.write_text('{"setup_required": true}', encoding="utf-8")
        self.assertTrue(gui.load_gate_setup_required())
        self.seed_legacy_gate()
        restarted, _ = self.start()
        self.assertEqual(self.mode(restarted), "signin")
        self.login(restarted, password)

    def test_malformed_gate_shapes_cannot_be_credentials_or_reset_markers(self):
        for payload in ([], None, True, 12, "fixture", {"salt": []}, {"setup_required": "true"}):
            with self.subTest(payload=payload):
                self.gate_path.write_text(json.dumps(payload), encoding="utf-8")
                self.assertIsNone(gui.load_gate_credential())
                self.assertFalse(gui.load_gate_setup_required())
        self.gate_path.write_text('{"setup_required": true, "salt": "broken"}', encoding="utf-8")
        self.assertIsNone(gui.load_gate_credential())
        self.assertTrue(gui.load_gate_setup_required())

    def test_busy_reset_changes_no_gate_config_or_history(self):
        server, wb, cookie, _ = self.authenticated_server()
        before = self.gate_path.read_bytes(), server.gate_credential, dict(server.sessions)
        wb.busy = True
        job = gui.Job("run", "fixture")
        wb.jobs[job.id] = job
        with mock.patch.object(skills, "release_all_sessions_and_cache") as release:
            status, payload, _ = self.request(server, "/api/reset", data={}, cookie=cookie)
        self.assertEqual(status, 409, payload)
        self.assertFalse(payload["ok"])
        release.assert_not_called()
        gui.save_config.assert_not_called()
        self.assertEqual(list(wb.jobs), [job.id])
        self.assert_unchanged_gate(server, *before)

    def test_worker_error_or_failed_envelope_never_claims_reset_success(self):
        server, wb, cookie, _ = self.authenticated_server()
        before = self.gate_path.read_bytes(), server.gate_credential, dict(server.sessions)
        original_instance = wb.instance_id
        for failure in ({"error": "fixture failure"}, {"success": False}, {"ok": False},
                        {"detail": {"error": "fixture worker failure"}}, {"isError": True}, None):
            with self.subTest(failure=failure):
                with mock.patch.object(skills, "release_all_sessions_and_cache", return_value=failure):
                    status, payload, _ = self.request(server, "/api/reset", data={}, cookie=cookie)
                self.assertEqual(status, 500, payload)
                self.assertFalse(payload["ok"])
                self.assert_unchanged_gate(server, *before)
                self.assertFalse(wb.busy)
                self.assertEqual(wb.instance_id, original_instance)
        gui.save_config.assert_not_called()

    def test_config_failure_keeps_old_gate_and_reports_failure(self):
        server, wb, cookie, _ = self.authenticated_server()
        before = self.gate_path.read_bytes(), server.gate_credential, dict(server.sessions)
        with mock.patch.object(gui, "save_config", side_effect=PermissionError("fixture denied")):
            status, payload, _ = self.request(server, "/api/reset", data={}, cookie=cookie)
        self.assertEqual(status, 500, payload)
        self.assertFalse(payload["ok"])
        self.assert_unchanged_gate(server, *before)
        self.assertFalse(wb.busy)

    def test_atomic_gate_failure_retains_old_credential_and_explains_partial_reset(self):
        server, wb, cookie, _ = self.authenticated_server()
        before = self.gate_path.read_bytes(), server.gate_credential, dict(server.sessions)
        original_replace = gui.os.replace

        def fail_only_gate(source, target):
            if Path(target) == self.gate_path:
                raise PermissionError("fixture denied")
            return original_replace(source, target)

        with mock.patch.object(gui.os, "replace", side_effect=fail_only_gate):
            status, payload, _ = self.request(server, "/api/reset", data={}, cookie=cookie)
        self.assertEqual(status, 500, payload)
        self.assertFalse(payload["ok"])
        self.assertIn("workbench reset ran", payload["error"])
        self.assertTrue(payload["reset"])
        self.assert_unchanged_gate(server, *before)
        self.assertEqual(list(self.root.glob(".gate-*.tmp")), [])
        self.assertFalse(wb.busy)

    def test_unauthenticated_reset_is_rejected_before_worker_or_disk(self):
        server, _, _, _ = self.authenticated_server()
        gate_bytes = self.gate_path.read_bytes()
        with mock.patch.object(skills, "release_all_sessions_and_cache") as release:
            self.assertEqual(self.request(server, "/api/reset", data={})[0], 401)
        release.assert_not_called()
        self.assertEqual(self.gate_path.read_bytes(), gate_bytes)

    def test_foreign_null_missing_port_and_malformed_origins_cannot_reset_open_server(self):
        server, wb = self.start()
        port = server.server_address[1]
        bad_origins = ("https://evil.example", "null", "http://127.0.0.1", "http://127.0.0.1:1",
                       "http://127.0.0.1:notaport", "http://127.0.0.1:99999", "http://127.0.0.1:0",
                       f"https://127.0.0.1:{port}", f"http://evil.example:{port}",
                       f"http://127.0.0.1:{port}/", f"http://user@127.0.0.1:{port}")
        with mock.patch.object(skills, "release_all_sessions_and_cache") as release:
            for origin in bad_origins:
                with self.subTest(origin=origin):
                    status, payload, _ = self.request(server, "/api/reset", data={}, headers={"Origin": origin})
                    self.assertEqual(status, 403, payload)
                    self.assertFalse(payload["ok"])
            release.assert_not_called()
        self.assertFalse(self.gate_path.exists())
        self.assertEqual(self.mode(server), "open")
        self.assertFalse(wb.busy)

    def test_same_origin_reset_allowed_and_does_not_accept_spoofed_loopback_host(self):
        server, _ = self.start()
        port = server.server_address[1]
        with mock.patch.object(skills, "release_all_sessions_and_cache") as release:
            status, _, _ = self.request(server, "/api/reset", data={}, headers={
                "Host": f"evil.example:{port}", "Origin": f"http://evil.example:{port}"})
            self.assertEqual(status, 403)
            release.assert_not_called()
        status, payload, _ = self.request(server, "/api/reset", data={}, headers={
            "Origin": f"http://127.0.0.1:{port}"})
        self.assertEqual(status, 200, payload)
        self.assertEqual(self.mode(server), "setup")

    def test_origin_authority_requires_unique_headers_and_actual_port(self):
        server, _ = self.start()
        host = f"127.0.0.1:{server.server_address[1]}"
        cases = ([('Host', host), ('Host', host)],
                 [('Host', host), ('Origin', 'http://' + host), ('Origin', 'http://' + host)],
                 [('Host', '127.0.0.1')], [('Host', host + '/path')],
                 [('Host', '127.0.0.1:abc')], [('Host', '127.0.0.1:65536')],
                 [('Host', '127.0.0.1:-1')], [('Host', '127.0.0.1:0')],
                 [('Host', host), ('Origin', '')],
                 [('Host', 'user@' + host)], [])
        for pairs in cases:
            headers = Message()
            for name, value in pairs:
                headers[name] = value
            with self.subTest(pairs=pairs):
                self.assertFalse(gui._reset_origin_allowed(server, headers))

    def test_remote_bind_retains_authenticated_same_host_compatibility(self):
        # No remote listener is opened. do_POST still owns gate authorization;
        # this pure check only covers remote-bound Host/Origin compatibility.
        server = types.SimpleNamespace(server_address=("0.0.0.0", 8765))
        headers = Message()
        headers["Host"] = "analysis.example:8765"
        headers["Origin"] = "http://analysis.example:8765"
        self.assertTrue(gui._reset_origin_allowed(server, headers))
        headers.replace_header("Origin", "http://foreign.example:8765")
        self.assertFalse(gui._reset_origin_allowed(server, headers))


if __name__ == "__main__":
    unittest.main(argv=[sys.argv[0], *unittest_args])
