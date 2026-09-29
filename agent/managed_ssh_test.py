"""Deterministic managed SSH lifecycle tests at the Paramiko boundary.

No real account, host key, credential file or remote connection is used. A
connected fixture may bind a temporary loopback listener, closed in tearDown.
"""
from __future__ import annotations

from collections import deque
import json
from pathlib import Path
import sys
import threading
import time
import types
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import managed_ssh

SECRET = "synthetic-managed-ssh-secret"


class AuthenticationException(Exception):
    pass


class BadHostKeyException(Exception):
    pass


class PasswordRequiredException(Exception):
    pass


class FakeKey:
    def get_name(self):
        return "ssh-ed25519"

    def asbytes(self):
        return b"synthetic-host-key"


class FakeTransport:
    def __init__(self):
        self.active = True
        self.authenticated = False
        self.auth_handler = types.SimpleNamespace(password=SECRET, interactive_handler=lambda *_: SECRET)

    def is_active(self):
        return self.active

    def is_authenticated(self):
        return self.authenticated

    def set_keepalive(self, value):
        self.keepalive = value


class FakeClient:
    def __init__(self, behavior):
        self.behavior = behavior
        self.policy = None
        self.closed = threading.Event()
        self.auth_started = threading.Event()
        self.connect_entered = threading.Event()
        self.transport = FakeTransport()
        self.flags = {}
        self.loaded = None

    def load_host_keys(self, path):
        self.loaded = path

    def set_missing_host_key_policy(self, policy):
        self.policy = policy

    def connect(self, **kwargs):
        self.connect_entered.set()
        self.flags = {key: value for key, value in kwargs.items() if key not in ("password", "passphrase")}
        if self.behavior == "mismatch":
            raise BadHostKeyException(SECRET)
        if self.behavior == "blocked":
            self.closed.wait(3)
            raise OSError(SECRET)
        if self.behavior in ("unknown", "bad_password"):
            self.policy.missing_host_key(self, kwargs["hostname"], FakeKey())
        self.auth_started.set()
        if self.behavior == "bad_password":
            raise AuthenticationException(SECRET)
        self.transport.authenticated = True

    def get_transport(self):
        return self.transport

    def close(self):
        self.transport.active = False
        self.closed.set()


class ManagedSSHTests(unittest.TestCase):
    def setUp(self):
        self.clients = []
        self.behaviors = deque(["known"])

        def create():
            client = FakeClient(self.behaviors.popleft())
            self.clients.append(client)
            return client

        boundary = types.SimpleNamespace(SSHClient=create, MissingHostKeyPolicy=object,
                                         AuthenticationException=AuthenticationException,
                                         BadHostKeyException=BadHostKeyException,
                                         PasswordRequiredException=PasswordRequiredException)
        patch = mock.patch.object(managed_ssh, "_paramiko", boundary)
        patch.start()
        self.addCleanup(patch.stop)
        self.manager = managed_ssh.ManagedSSH(known_hosts_path=Path("__synthetic_missing_known_hosts__"),
                                               connect_timeout_seconds=2, host_key_timeout_seconds=2,
                                               auth_timeout_seconds=2)
        self.addCleanup(self.manager.disconnect)
        self.socket_patch = mock.patch.object(self.manager, "_connect_socket", return_value=object())
        self.socket_patch.start()
        self.addCleanup(self.socket_patch.stop)
        self.config = {"host": "example.invalid", "username": "fixture", "port": 2200,
                       "backend_port": 8765, "auth_method": "password", "password": SECRET}

    def wait_state(self, expected, timeout=3):
        limit = time.monotonic() + timeout
        while time.monotonic() < limit:
            snapshot = self.manager.snapshot()
            if snapshot["ssh_state"] == expected:
                self.assertNotIn(SECRET, json.dumps(snapshot))
                return snapshot
            time.sleep(0.005)
        self.fail(f"expected {expected}, got {self.manager.snapshot()}")

    def test_unknown_key_requires_confirmation_before_auth_and_snapshot_has_no_secret(self):
        self.behaviors = deque(["unknown"])
        identifier = self.manager.connect(self.config)
        snapshot = self.wait_state("awaiting_host_key")
        self.assertEqual(snapshot["attempt_id"], identifier)
        self.assertFalse(self.clients[0].auth_started.is_set())
        self.assertIsNone(self.manager.endpoint())
        self.assertEqual(snapshot["host_key"]["algorithm"], "ssh-ed25519")
        self.assertTrue(snapshot["host_key"]["fingerprint"].startswith("SHA256:"))
        self.manager.confirm_host_key(identifier, True)
        self.wait_state("connected")
        self.assertTrue(self.clients[0].auth_started.is_set())
        self.assertEqual(self.manager.endpoint()[0], "127.0.0.1")
        self.assertEqual(self.manager._attempt.credentials, {})
        self.assertIsNone(self.clients[0].transport.auth_handler.password)
        self.assertIsNone(self.clients[0].transport.auth_handler.interactive_handler)
        self.assertIs(self.clients[0].flags["allow_agent"], False)
        self.assertIs(self.clients[0].flags["look_for_keys"], False)

    def test_rejected_unknown_key_never_authenticates(self):
        self.behaviors = deque(["unknown"])
        identifier = self.manager.connect(self.config)
        self.wait_state("awaiting_host_key")
        self.manager.confirm_host_key(identifier, False)
        snapshot = self.wait_state("connection_failed")
        self.assertEqual(snapshot["error"], "host_key_rejected")
        self.assertFalse(self.clients[0].auth_started.is_set())
        self.assertIsNone(self.manager.endpoint())

    def test_known_mismatch_fails_without_confirmation_or_authentication(self):
        self.behaviors = deque(["mismatch"])
        with mock.patch.object(Path, "exists", return_value=True):
            self.manager.connect(self.config)
            snapshot = self.wait_state("host_key_mismatch")
        self.assertEqual(snapshot["error"], "host_key_mismatch")
        self.assertNotIn("host_key", snapshot)
        self.assertFalse(self.clients[0].auth_started.is_set())
        self.assertTrue(self.clients[0].loaded)
        self.assertIsNone(self.manager.endpoint())

    def test_password_failure_never_echoes_exception_or_retains_credentials(self):
        self.behaviors = deque(["bad_password"])
        identifier = self.manager.connect(self.config)
        self.wait_state("awaiting_host_key")
        self.manager.confirm_host_key(identifier, True)
        snapshot = self.wait_state("authentication_failed")
        self.assertEqual(snapshot["error"], "authentication_failed")
        self.assertEqual(self.manager._attempt.credentials, {})
        self.assertIsNone(self.manager.endpoint())

    def test_cancel_pending_confirmation_clears_credentials_without_authentication(self):
        self.behaviors = deque(["unknown"])
        identifier = self.manager.connect(self.config)
        self.wait_state("awaiting_host_key")
        old = self.manager._attempt
        self.manager.disconnect()
        self.assertEqual(self.manager.snapshot()["ssh_state"], "disconnected")
        self.assertTrue(old.cancelled.is_set())
        self.assertEqual(old.credentials, {})
        self.assertFalse(self.clients[0].auth_started.is_set())
        with self.assertRaisesRegex(ValueError, "stale_attempt"):
            self.manager.confirm_host_key(identifier, True)

    def test_old_connect_error_cannot_overwrite_a_new_attempt(self):
        self.behaviors = deque(["blocked", "known"])
        old_id = self.manager.connect(self.config)
        limit = time.monotonic() + 2
        while not self.clients and time.monotonic() < limit:
            time.sleep(0.005)
        self.assertTrue(self.clients[0].connect_entered.wait(1))
        self.manager.disconnect()
        new_id = self.manager.connect(self.config)
        self.assertNotEqual(new_id, old_id)
        snapshot = self.wait_state("connected")
        self.assertEqual(snapshot["attempt_id"], new_id)
        self.assertEqual(snapshot["error"], "")
        self.assertTrue(self.clients[0].closed.wait(1))

    def test_active_connection_rejects_duplicate_connect(self):
        self.manager.connect(self.config)
        self.wait_state("connected")
        with self.assertRaisesRegex(RuntimeError, "ssh_connection_active"):
            self.manager.connect(self.config)
        self.assertEqual(len(self.clients), 1)

    def test_auth_methods_explicitly_control_agent_and_key_search(self):
        for method in ("agent", "key"):
            with self.subTest(method=method):
                self.behaviors = deque(["known"])
                self.manager.connect({**self.config, "auth_method": method,
                                      "key_path": "fixture-key", "passphrase": SECRET})
                self.wait_state("connected")
                flags = self.clients[-1].flags
                self.assertEqual(flags["allow_agent"], method == "agent")
                self.assertEqual(flags["look_for_keys"], method == "agent")
                self.assertEqual("key_filename" in flags, method == "key")
                self.assertEqual(self.manager._attempt.credentials, {})
                self.manager.disconnect()

    def test_dependency_missing_is_an_async_classified_failure(self):
        with mock.patch.object(managed_ssh, "_paramiko", None):
            identifier = self.manager.connect(self.config)
            snapshot = self.wait_state("connection_failed")
        self.assertEqual(snapshot["attempt_id"], identifier)
        self.assertEqual(snapshot["error"], "dependency_missing")
        self.assertEqual(self.manager._attempt.credentials, {})

    def test_pending_host_key_deadline_is_bounded(self):
        self.behaviors = deque(["unknown"])
        self.manager._host_key_timeout = 0.1
        self.manager.connect(self.config)
        snapshot = self.wait_state("connection_failed")
        self.assertEqual(snapshot["error"], "host_key_confirmation_timeout")
        self.assertFalse(self.clients[0].auth_started.is_set())

    def test_blocked_connect_deadline_closes_client(self):
        self.behaviors = deque(["blocked"])
        self.manager._connect_timeout = 0.1
        self.manager.connect(self.config)
        snapshot = self.wait_state("connection_failed")
        self.assertEqual(snapshot["error"], "connection_timeout")
        self.assertTrue(self.clients[0].closed.wait(1))

    def test_disconnect_closes_forwarding_and_is_idempotent(self):
        self.manager.connect(self.config)
        self.wait_state("connected")
        old = self.manager._attempt
        server = old.server
        self.manager.disconnect()
        self.manager.disconnect()
        self.assertIsNone(self.manager.endpoint())
        self.assertEqual(self.manager.snapshot()["ssh_state"], "disconnected")
        self.assertEqual(server.socket.fileno(), -1)
        self.assertTrue(self.clients[0].closed.is_set())

    def test_bad_configuration_does_not_start_a_thread(self):
        for update in ({"port": True}, {"port": 1.2}, {"port": 0}, {"auth_method": "none"},
                       {"auth_method": "key", "key_path": ""}, {"password": {"bad": True}},
                       {"host": "bad\nhost"}):
            with self.subTest(update=update), self.assertRaises(ValueError):
                self.manager.connect({**self.config, **update})
        self.assertEqual(self.clients, [])

    def test_thread_start_failure_is_classified_and_clears_credentials(self):
        with mock.patch.object(managed_ssh.threading, "Thread") as thread:
            thread.return_value.start.side_effect = RuntimeError(SECRET)
            identifier = self.manager.connect(self.config)
        snapshot = self.manager.snapshot()
        self.assertEqual(snapshot["attempt_id"], identifier)
        self.assertEqual(snapshot["ssh_state"], "connection_failed")
        self.assertEqual(snapshot["error"], "ssh_worker_unavailable")
        self.assertEqual(self.manager._attempt.credentials, {})
        self.assertNotIn(SECRET, json.dumps(snapshot))

    def test_late_dns_result_cannot_open_socket_or_authenticate_after_cancel(self):
        self.socket_patch.stop()
        resolving, release_dns, completed = threading.Event(), threading.Event(), threading.Event()
        connect_socket = self.manager._connect_socket

        def delayed_resolution(*_args, **_kwargs):
            resolving.set()
            release_dns.wait(2)
            return [(2, 1, 6, "", ("127.0.0.1", 2200))]

        def observed_connect(attempt):
            try:
                return connect_socket(attempt)
            finally:
                completed.set()

        with mock.patch.object(managed_ssh.socket, "getaddrinfo", side_effect=delayed_resolution), \
                mock.patch.object(self.manager, "_connect_socket", side_effect=observed_connect), \
                mock.patch.object(managed_ssh.socket, "socket") as open_socket:
            self.manager.connect(self.config)
            self.assertTrue(resolving.wait(1))
            self.manager.disconnect()
            release_dns.set()
            self.assertTrue(completed.wait(1))
            # The cancellation barrier is checked synchronously before socket().
            self.assertFalse(self.clients[0].auth_started.is_set())
            open_socket.assert_not_called()
        self.assertEqual(self.manager.snapshot()["ssh_state"], "disconnected")


if __name__ == "__main__":
    unittest.main()
