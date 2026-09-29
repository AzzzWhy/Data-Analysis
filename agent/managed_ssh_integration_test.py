"""Real loopback SSH authentication and forwarding tests; no user credentials.

Requires Paramiko. Run beside managed_ssh.py, or pass --agent-dir for a scratch
module. Every key and known-hosts file is generated inside TemporaryDirectory;
the SSH fixture and HTTP fixture bind only 127.0.0.1 on ephemeral ports.
"""
from __future__ import annotations

import argparse
import base64
from contextlib import suppress
import hashlib
import importlib
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import select
import socket
import sys
import tempfile
import threading
import time
import unittest
from urllib.request import ProxyHandler, build_opener

import paramiko


parser = argparse.ArgumentParser(add_help=False)
parser.add_argument("--agent-dir", type=Path, default=Path(__file__).resolve().parent)
options, unittest_args = parser.parse_known_args()
sys.path.insert(0, str(options.agent_dir.resolve()))
managed_ssh = importlib.import_module("managed_ssh")

USERNAME = "ssh-integration-fixture"
PASSWORD = "synthetic-test-password-only"


def wait_until(predicate, timeout=4, description="condition"):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(.01)
    raise AssertionError(f"Timed out waiting for {description}")


class BackendHandler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def do_GET(self):
        if self.path == "/events":
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Connection", "close")
            self.end_headers()
            try:
                self.wfile.write(b'id: 1\nevent: progress\ndata: {"stage":"started"}\n\n')
                self.wfile.flush()
                # Prove that an idle SSH forwarding connection survives, not just
                # one eager HTTP response. This delay is fixture behavior only.
                time.sleep(1.2)
                self.wfile.write(b'id: 2\nevent: done\ndata: {"success":true}\n\n')
                self.wfile.flush()
            except OSError:
                pass
            return
        data = json.dumps({"source": "synthetic-loopback-backend", "path": self.path}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        with suppress(OSError):
            self.wfile.write(data)


class AuthServer(paramiko.ServerInterface):
    def __init__(self, fixture):
        self.fixture = fixture
        self.destinations = {}

    def get_allowed_auths(self, username):
        return "password,publickey"

    def check_auth_none(self, username):
        self.fixture.record_auth("none")
        return paramiko.AUTH_FAILED

    def check_auth_password(self, username, password):
        self.fixture.record_auth("password")
        return paramiko.AUTH_SUCCESSFUL if username == USERNAME and password == PASSWORD else paramiko.AUTH_FAILED

    def check_auth_publickey(self, username, key):
        self.fixture.record_auth("publickey")
        trusted = self.fixture.client_key
        return paramiko.AUTH_SUCCESSFUL if (username == USERNAME and trusted is not None
            and key.get_name() == trusted.get_name() and key.get_base64() == trusted.get_base64()) else paramiko.AUTH_FAILED

    def check_channel_request(self, kind, channel_id):
        return paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED

    def check_channel_direct_tcpip_request(self, channel_id, origin, destination):
        # Never allow a fixture request to leave this computer.
        if destination[0] != "127.0.0.1":
            return paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED
        self.destinations[channel_id] = destination
        self.fixture.forward_destinations.append(destination)
        return paramiko.OPEN_SUCCEEDED


class SSHFixture:
    def __init__(self, host_key, client_key=None):
        self.host_key = host_key
        self.client_key = client_key
        self.auth_methods = []
        self.forward_destinations = []
        self.transports = []
        self.workers = []
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen()
        self.listener.settimeout(.1)
        self.port = self.listener.getsockname()[1]
        self.thread = threading.Thread(target=self.accept_connections, daemon=True)
        self.thread.start()

    def record_auth(self, method):
        with self.lock:
            self.auth_methods.append(method)

    def accept_connections(self):
        while not self.stop.is_set():
            try:
                connection, _address = self.listener.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            worker = threading.Thread(target=self.serve_connection, args=(connection,), daemon=True)
            self.workers.append(worker)
            worker.start()

    def serve_connection(self, connection):
        transport = paramiko.Transport(connection)
        self.transports.append(transport)
        interface = AuthServer(self)
        try:
            transport.add_server_key(self.host_key)
            transport.start_server(server=interface)
            while not self.stop.is_set() and transport.is_active():
                channel = transport.accept(timeout=.1)
                if channel is None:
                    continue
                destination = interface.destinations.get(channel.get_id())
                if destination is None:
                    channel.close()
                    continue
                worker = threading.Thread(target=self.forward, args=(channel, destination), daemon=True)
                self.workers.append(worker)
                worker.start()
        except (EOFError, OSError, paramiko.SSHException):
            pass
        finally:
            transport.close()

    def forward(self, channel, destination):
        try:
            with socket.create_connection(destination, timeout=1) as target:
                while not self.stop.is_set() and not channel.closed:
                    ready, _, _ = select.select([channel, target], [], [], .1)
                    for reader in ready:
                        data = reader.recv(65536)
                        if not data:
                            return
                        (target if reader is channel else channel).sendall(data)
        except (OSError, EOFError, paramiko.SSHException):
            pass
        finally:
            with suppress(EOFError, OSError, paramiko.SSHException):
                channel.close()

    def close(self):
        self.stop.set()
        self.listener.close()
        for transport in self.transports:
            transport.close()
        self.thread.join(timeout=2)
        for worker in self.workers:
            worker.join(timeout=2)


class ManagedSSHIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.host_key = paramiko.RSAKey.generate(2048)
        cls.other_host_key = paramiko.RSAKey.generate(2048)
        cls.client_key = paramiko.RSAKey.generate(2048)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="managed-ssh-fixture-")
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.known_hosts = self.directory / "known_hosts"
        self.private_key = self.directory / "client_rsa"
        self.client_key.write_private_key_file(str(self.private_key))
        self.backend = ThreadingHTTPServer(("127.0.0.1", 0), BackendHandler)
        self.backend.daemon_threads = True
        self.backend_thread = threading.Thread(target=self.backend.serve_forever,
                                               kwargs={"poll_interval": .02}, daemon=True)
        self.backend_thread.start()
        self.addCleanup(self.close_backend)
        self.ssh = SSHFixture(self.host_key, self.client_key)
        self.addCleanup(self.ssh.close)
        self.manager = managed_ssh.ManagedSSH(known_hosts_path=self.known_hosts,
                                            connect_timeout_seconds=3,
                                            host_key_timeout_seconds=4,
                                            auth_timeout_seconds=2)
        self.addCleanup(self.manager.disconnect)
        self.http = build_opener(ProxyHandler({}))

    def close_backend(self):
        self.backend.shutdown()
        self.backend.server_close()
        self.backend_thread.join(timeout=2)

    def config(self, **overrides):
        return {"host": "127.0.0.1", "port": self.ssh.port, "username": USERNAME,
                "backend_port": self.backend.server_address[1], "auth_method": "password",
                "password": PASSWORD, **overrides}

    def trust_host(self, key=None):
        hosts = paramiko.HostKeys()
        hosts.add(f"[127.0.0.1]:{self.ssh.port}", (key or self.host_key).get_name(), key or self.host_key)
        hosts.save(str(self.known_hosts))

    def connect(self, **overrides):
        attempt = self.manager.connect(self.config(**overrides))
        self.assertIsInstance(attempt, str)
        return attempt

    def await_connected(self):
        snapshot = wait_until(lambda: (state if (state := self.manager.snapshot()).get("ssh_state") == "connected" else None),
                              description="SSH connected")
        endpoint = self.manager.endpoint()
        self.assertIsNotNone(endpoint)
        self.assertEqual(endpoint[0], "127.0.0.1")
        self.assertGreater(endpoint[1], 0)
        self.assertNotIn(PASSWORD, json.dumps(snapshot))
        return endpoint

    def await_failed(self):
        failures = {"connection_failed", "authentication_failed", "host_key_mismatch"}
        state = wait_until(lambda: (state if (state := self.manager.snapshot()).get("ssh_state") in failures else None),
                           description="SSH failed")
        self.assertIsNone(self.manager.endpoint())
        self.assertNotIn(PASSWORD, json.dumps(state))
        return state

    def await_host_key(self):
        state = wait_until(lambda: (state if (state := self.manager.snapshot()).get("host_key") else None),
                           description="host key confirmation")
        self.assertIsNone(self.manager.endpoint())
        return state

    def read_http(self, endpoint, path="/health"):
        with self.http.open(f"http://{endpoint[0]}:{endpoint[1]}{path}", timeout=3) as response:
            self.assertEqual(response.status, 200)
            return response.read().decode("utf8")

    def test_unknown_host_never_authenticates_before_confirmation_then_password_works(self):
        attempt = self.connect()
        pending = self.await_host_key()
        self.assertEqual(pending["attempt_id"], attempt)
        self.assertEqual(pending["host_key"]["host"], "127.0.0.1")
        self.assertEqual(pending["host_key"]["port"], self.ssh.port)
        self.assertEqual(pending["host_key"]["algorithm"], self.host_key.get_name())
        fingerprint = "SHA256:" + base64.b64encode(hashlib.sha256(self.host_key.asbytes()).digest()).decode().rstrip("=")
        self.assertEqual(pending["host_key"]["fingerprint"], fingerprint)
        self.assertEqual(self.ssh.auth_methods, [], "secret must not be sent to an untrusted host")
        self.manager.confirm_host_key(attempt, True)
        endpoint = self.await_connected()
        self.assertIn("password", self.ssh.auth_methods)
        self.assertEqual(json.loads(self.read_http(endpoint))["source"], "synthetic-loopback-backend")

    def test_declined_unknown_host_never_authenticates(self):
        attempt = self.connect()
        self.await_host_key()
        self.assertEqual(self.ssh.auth_methods, [])
        self.manager.confirm_host_key(attempt, False)
        failed = self.await_failed()
        self.assertEqual(failed["error"], "host_key_rejected")
        self.assertEqual(self.ssh.auth_methods, [])
        self.assertFalse(self.known_hosts.exists(), "rejected key must not become trusted")

    def test_known_host_password_connects_without_confirmation(self):
        self.trust_host()
        self.connect()
        endpoint = self.await_connected()
        self.assertEqual(json.loads(self.read_http(endpoint, "/fixture"))["path"], "/fixture")
        self.assertIn("password", self.ssh.auth_methods)

    def test_wrong_password_fails_and_is_not_exposed(self):
        self.trust_host()
        wrong = "synthetic-wrong-password-never-log"
        self.connect(password=wrong)
        failure = self.await_failed()
        self.assertTrue(failure.get("error"))
        self.assertNotIn(wrong, json.dumps(failure))
        self.assertEqual(self.ssh.auth_methods, ["password"])

    def test_explicit_private_key_authenticates_without_password(self):
        self.trust_host()
        self.connect(auth_method="key", key_path=str(self.private_key), password="")
        endpoint = self.await_connected()
        self.assertEqual(self.ssh.auth_methods, ["publickey"])
        self.assertIn("synthetic-loopback-backend", self.read_http(endpoint))
        self.assertNotIn("PRIVATE KEY", json.dumps(self.manager.snapshot()))

    def test_encrypted_private_key_uses_only_its_passphrase(self):
        self.trust_host()
        encrypted = self.directory / "client_rsa_encrypted"
        phrase = "synthetic-key-passphrase"
        self.client_key.write_private_key_file(str(encrypted), password=phrase)
        self.connect(auth_method="key", key_path=str(encrypted), passphrase=phrase, password="")
        self.await_connected()
        self.assertEqual(self.ssh.auth_methods, ["publickey"])
        self.assertNotIn(phrase, json.dumps(self.manager.snapshot()))

    def test_known_host_mismatch_fails_before_auth_and_does_not_overwrite_trust(self):
        self.trust_host(self.other_host_key)
        original = self.known_hosts.read_bytes()
        self.connect()
        failed = self.await_failed()
        self.assertEqual(failed["error"], "host_key_mismatch")
        self.assertEqual(self.ssh.auth_methods, [])
        self.assertEqual(self.known_hosts.read_bytes(), original)

    def test_ssh_connected_does_not_claim_unavailable_http_backend_is_healthy(self):
        self.trust_host()
        # Reserve then release a loopback port; no process is listening there.
        with socket.socket() as unused:
            unused.bind(("127.0.0.1", 0))
            closed_port = unused.getsockname()[1]
        self.connect(backend_port=closed_port)
        endpoint = self.await_connected()
        with self.assertRaises(Exception):
            self.read_http(endpoint)
        self.assertEqual(self.manager.snapshot()["ssh_state"], "connected")
        self.assertEqual(self.manager.endpoint(), endpoint)
        self.assertNotEqual(self.manager.snapshot().get("backend_status"), "available")

    def test_direct_tcpip_forwarding_preserves_idle_sse(self):
        self.trust_host()
        self.connect()
        endpoint = self.await_connected()
        events = self.read_http(endpoint, "/events")
        self.assertIn("event: progress", events)
        self.assertIn("event: done", events)
        self.assertIn('"success":true', events)
        self.assertTrue(self.ssh.forward_destinations)
        self.assertTrue(all(destination == ("127.0.0.1", self.backend.server_address[1])
                            for destination in self.ssh.forward_destinations))

    def test_disconnect_closes_listener_and_does_not_reattempt_authentication(self):
        self.trust_host()
        self.connect()
        endpoint = self.await_connected()
        self.read_http(endpoint)
        before = list(self.ssh.auth_methods)
        self.manager.disconnect()
        self.assertIsNone(self.manager.endpoint())
        self.assertNotEqual(self.manager.snapshot()["ssh_state"], "connected")
        with self.assertRaises(OSError):
            socket.create_connection(endpoint, timeout=.5)
        time.sleep(.15)
        self.assertEqual(self.ssh.auth_methods, before)
        self.assertIsNone(self.manager.endpoint())

    def test_disconnect_closes_an_already_open_sse_socket(self):
        self.trust_host()
        self.connect()
        endpoint = self.await_connected()
        with self.http.open(f"http://{endpoint[0]}:{endpoint[1]}/events", timeout=3) as stream:
            first = []
            while True:
                line = stream.readline()
                first.append(line)
                if not line.strip():
                    break
            self.assertIn(b"event: progress", b"".join(first))
            started = time.monotonic()
            self.manager.disconnect()
            self.assertEqual(stream.read(), b"")
            self.assertLess(time.monotonic() - started, 1, "disconnect must close active streams without waiting for backend output")
        self.assertIsNone(self.manager.endpoint())

    def test_unpersisted_host_acceptance_requires_confirmation_on_next_attempt(self):
        attempt = self.connect()
        self.await_host_key()
        self.manager.confirm_host_key(attempt, True)
        self.await_connected()
        self.manager.disconnect()
        before = list(self.ssh.auth_methods)
        self.assertFalse(self.known_hosts.exists(), "confirmation must not silently persist trust")
        next_attempt = self.connect()
        pending = self.await_host_key()
        self.assertEqual(pending["attempt_id"], next_attempt)
        self.assertEqual(self.ssh.auth_methods, before)
        self.manager.confirm_host_key(next_attempt, False)
        self.await_failed()

    def test_stale_confirmation_cannot_authorize_a_new_attempt(self):
        old_attempt = self.connect()
        self.await_host_key()
        self.manager.disconnect()
        new_attempt = self.connect()
        self.await_host_key()
        self.assertNotEqual(old_attempt, new_attempt)
        with self.assertRaisesRegex(ValueError, "stale_attempt"):
            self.manager.confirm_host_key(old_attempt, True)
        self.assertEqual(self.ssh.auth_methods, [])
        self.manager.confirm_host_key(new_attempt, False)
        self.await_failed()
        self.assertEqual(self.ssh.auth_methods, [])


if __name__ == "__main__":
    unittest.main(argv=[sys.argv[0], *unittest_args])
