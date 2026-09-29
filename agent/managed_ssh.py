"""Optional, in-process SSH authentication and loopback-only forwarding.

No remote commands are executed. Unknown host keys require an explicit decision
before Paramiko authenticates. Credentials are never logged or serialized, and
accepted unknown keys are trusted for this attempt only, never written to disk.
"""
from __future__ import annotations

import base64
from dataclasses import dataclass, field
import hashlib
from pathlib import Path
import secrets
import socket
import socketserver
import threading
import time

try:
    import paramiko as _paramiko
except ImportError:  # The local workbench remains usable without the optional SDK.
    _paramiko = None


class _Cancelled(Exception):
    pass


class _HostKeyRejected(Exception):
    pass


class _HostKeyTimeout(Exception):
    pass


@dataclass
class _Attempt:
    identifier: str
    host: str
    username: str
    port: int
    backend_port: int
    auth_method: str
    key_path: str
    credentials: dict = field(repr=False)
    cancelled: threading.Event = field(default_factory=threading.Event, repr=False)
    confirmation: threading.Event = field(default_factory=threading.Event, repr=False)
    resources_lock: threading.RLock = field(default_factory=threading.RLock, repr=False)
    decision: bool | None = None
    client: object = field(default=None, repr=False)
    server: object = field(default=None, repr=False)
    server_thread: object = field(default=None, repr=False)
    connections: set = field(default_factory=set, repr=False)
    forward_endpoint: tuple | None = None
    deadline: float | None = None


def _close_connection(connection):
    try:
        connection.shutdown(socket.SHUT_RDWR)
    except Exception:
        pass
    try:
        connection.close()
    except Exception:
        pass


def _clear_auth_credentials(client):
    """Best-effort cleanup of completed Paramiko authentication references.

    Paramiko keeps AuthHandler for its authenticated flag. Its password field
    and keyboard-interactive callback are no longer needed once connect returns,
    but may otherwise keep the password alive for the entire SSH session.
    """
    try:
        transport = client.get_transport()
        handler = getattr(transport, "auth_handler", None)
        for name in ("password", "interactive_handler"):
            if handler is not None and hasattr(handler, name):
                setattr(handler, name, None)
    except Exception:
        pass  # Optional SDK internals must not prevent orderly client cleanup.


class _ForwardServer(socketserver.ThreadingTCPServer):
    daemon_threads = True
    block_on_close = False
    allow_reuse_address = True

    def handle_error(self, *_args):
        # Socket/channel errors may contain remote text; never print tracebacks.
        pass


class _ForwardHandler(socketserver.BaseRequestHandler):
    def handle(self):
        attempt = self.server.attempt
        channel = None
        with attempt.resources_lock:
            if attempt.cancelled.is_set():
                return
            attempt.connections.add(self.request)
            client = attempt.client
        try:
            transport = client.get_transport() if client else None
            if not transport or not transport.is_authenticated():
                return
            channel = transport.open_channel(
                "direct-tcpip", ("127.0.0.1", attempt.backend_port),
                self.request.getpeername(), timeout=10)
            if channel is None:
                return
            with attempt.resources_lock:
                if attempt.cancelled.is_set():
                    return
                attempt.connections.add(channel)
            self.request.settimeout(1)
            channel.settimeout(1)
            finished = threading.Event()

            def pump(source, destination):
                try:
                    while not attempt.cancelled.is_set() and not finished.is_set():
                        try:
                            data = source.recv(65536)
                        except socket.timeout:
                            continue  # Idle SSE streams are healthy, not timed out.
                        if not data:
                            break
                        destination.sendall(data)
                except Exception:
                    pass
                finally:
                    finished.set()
                    _close_connection(self.request)
                    _close_connection(channel)

            upstream = threading.Thread(target=pump, args=(self.request, channel), daemon=True)
            upstream.start()
            pump(channel, self.request)
            upstream.join(timeout=1.5)
        except Exception:
            pass
        finally:
            with attempt.resources_lock:
                attempt.connections.discard(self.request)
                if channel is not None:
                    attempt.connections.discard(channel)
            _close_connection(self.request)
            if channel is not None:
                _close_connection(channel)


class ManagedSSH:
    ACTIVE_STATES = {"connecting", "awaiting_host_key", "authenticating", "connected"}

    def __init__(self, known_hosts_path=None, *, connect_timeout_seconds=30,
                 host_key_timeout_seconds=120, auth_timeout_seconds=20):
        self._known_hosts = Path(known_hosts_path) if known_hosts_path is not None else Path.home() / ".ssh" / "known_hosts"
        self._connect_timeout = float(connect_timeout_seconds)
        self._host_key_timeout = float(host_key_timeout_seconds)
        self._auth_timeout = float(auth_timeout_seconds)
        if min(self._connect_timeout, self._host_key_timeout, self._auth_timeout) <= 0:
            raise ValueError("invalid_timeout")
        self._lock = threading.RLock()
        self._attempt = None
        self._state = {"attempt_id": "", "ssh_state": "disconnected", "error": "", "auth_method": None}

    def snapshot(self):
        with self._lock:
            result = dict(self._state)
            if "host_key" in result:
                result["host_key"] = dict(result["host_key"])
            return result

    def endpoint(self):
        with self._lock:
            attempt = self._attempt
            if attempt and self._state["ssh_state"] == "connected" and not attempt.cancelled.is_set():
                return attempt.forward_endpoint
            return None

    @staticmethod
    def _validate(config):
        if not isinstance(config, dict):
            raise ValueError("invalid_ssh_config")
        host, username = config.get("host"), config.get("username")
        if (not isinstance(host, str) or not host.strip() or any(c.isspace() or ord(c) < 32 for c in host)
                or not isinstance(username, str) or not username or any(ord(c) < 32 for c in username)):
            raise ValueError("invalid_ssh_address")
        ports = []
        for name, default in (("port", 22), ("backend_port", 8765)):
            value = config.get(name, default)
            if isinstance(value, bool):
                raise ValueError("invalid_ssh_port")
            try:
                port = int(value)
            except (TypeError, ValueError, OverflowError):
                raise ValueError("invalid_ssh_port") from None
            if not 1 <= port <= 65535 or (isinstance(value, float) and value != port):
                raise ValueError("invalid_ssh_port")
            ports.append(port)
        method = config.get("auth_method", "agent")
        if method not in {"password", "agent", "key"}:
            raise ValueError("invalid_auth_method")
        for name in ("password", "key_path", "passphrase"):
            if config.get(name) is not None and not isinstance(config[name], str):
                raise ValueError("invalid_auth_field")
        key_path = config.get("key_path") or ""
        if method == "key" and not key_path.strip():
            raise ValueError("key_path_required")
        credentials = {"password": config.get("password") or "", "passphrase": config.get("passphrase") or ""}
        return _Attempt(secrets.token_hex(16), host, username, *ports, method, key_path, credentials)

    def connect(self, config):
        attempt = self._validate(config)
        with self._lock:
            if self._state["ssh_state"] in self.ACTIVE_STATES:
                attempt.credentials.clear()
                raise RuntimeError("ssh_connection_active")
            old = self._attempt
            self._attempt = attempt
            attempt.deadline = time.monotonic() + self._connect_timeout
            self._state = {"attempt_id": attempt.identifier, "ssh_state": "connecting",
                           "error": "", "auth_method": attempt.auth_method}
        if old:
            self._cancel(old)
        try:
            threading.Thread(target=self._watch_deadline, args=(attempt,), daemon=True).start()
            threading.Thread(target=self._run, args=(attempt,), daemon=True).start()
        except Exception:
            self._fail(attempt, "connection_failed", "ssh_worker_unavailable")
        return attempt.identifier

    def confirm_host_key(self, attempt_id, accept):
        if type(accept) is not bool:
            raise ValueError("invalid_confirmation")
        with self._lock:
            attempt = self._attempt
            if not attempt or attempt.identifier != attempt_id or attempt.cancelled.is_set():
                raise ValueError("stale_attempt")
            if self._state["ssh_state"] != "awaiting_host_key" or attempt.decision is not None:
                raise ValueError("host_key_confirmation_not_pending")
            attempt.decision = accept
            attempt.confirmation.set()

    def disconnect(self):
        with self._lock:
            attempt = self._attempt
            self._state = {"attempt_id": attempt.identifier if attempt else "", "ssh_state": "disconnected",
                           "error": "", "auth_method": attempt.auth_method if attempt else None}
            if attempt:
                attempt.forward_endpoint = None
                attempt.cancelled.set()
                attempt.confirmation.set()
                attempt.credentials.clear()
        if attempt:
            self._cancel(attempt)

    def _publish(self, attempt, state, error="", host_key=None):
        with self._lock:
            if self._attempt is not attempt or attempt.cancelled.is_set():
                return False
            self._state = {"attempt_id": attempt.identifier, "ssh_state": state,
                           "error": error, "auth_method": attempt.auth_method}
            if host_key:
                self._state["host_key"] = host_key
            return True

    def _cancel(self, attempt):
        attempt.cancelled.set()
        attempt.confirmation.set()
        attempt.credentials.clear()
        with attempt.resources_lock:
            connections = list(attempt.connections)
            attempt.connections.clear()
            client, server, thread = attempt.client, attempt.server, attempt.server_thread
            attempt.client = attempt.server = attempt.server_thread = None
            attempt.forward_endpoint = None
        for connection in connections:
            _close_connection(connection)
        if client:
            try:
                client.close()
            except Exception:
                pass
        if server:
            try:
                if thread and thread.is_alive() and thread is not threading.current_thread():
                    server.shutdown()
                server.server_close()
            except Exception:
                pass

    def _fail(self, attempt, state, error):
        self._publish(attempt, state, error)
        self._cancel(attempt)

    def _watch_deadline(self, attempt):
        while not attempt.cancelled.wait(0.1):
            with self._lock:
                if self._attempt is not attempt:
                    return
                deadline = attempt.deadline
                state = self._state["ssh_state"]
            if deadline is not None and time.monotonic() >= deadline:
                code = "host_key_confirmation_timeout" if state == "awaiting_host_key" else "connection_timeout"
                self._fail(attempt, "connection_failed", code)
                return
            if state == "connected":
                return

    def _check(self, attempt):
        with self._lock:
            if self._attempt is not attempt or attempt.cancelled.is_set():
                raise _Cancelled()

    def _connect_socket(self, attempt):
        # A system resolver cannot be interrupted portably. It runs only on the
        # daemon connection thread; cancellation/deadline can finish immediately.
        # Recheck after resolution BEFORE opening any socket, so a late DNS result
        # cannot authenticate a cancelled attempt through Paramiko's internals.
        addresses = socket.getaddrinfo(attempt.host, attempt.port, type=socket.SOCK_STREAM)
        self._check(attempt)
        for family, socktype, protocol, _canonname, address in addresses:
            self._check(attempt)
            connection = socket.socket(family, socktype, protocol)
            with attempt.resources_lock:
                if attempt.cancelled.is_set():
                    connection.close()
                    raise _Cancelled()
                attempt.connections.add(connection)
            try:
                remaining = (attempt.deadline or (time.monotonic() + self._connect_timeout)) - time.monotonic()
                connection.settimeout(max(0.01, min(15, remaining)))
                connection.connect(address)
                self._check(attempt)
                return connection
            except _Cancelled:
                _close_connection(connection)
                raise
            except OSError:
                with attempt.resources_lock:
                    attempt.connections.discard(connection)
                _close_connection(connection)
        raise OSError("ssh_connection_failed")

    def _host_key(self, attempt, key):
        fingerprint = "SHA256:" + base64.b64encode(hashlib.sha256(key.asbytes()).digest()).decode("ascii").rstrip("=")
        challenge = {"host": attempt.host, "port": attempt.port,
                     "algorithm": key.get_name(), "fingerprint": fingerprint}
        with self._lock:
            self._check(attempt)
            attempt.deadline = time.monotonic() + self._host_key_timeout
            self._publish(attempt, "awaiting_host_key", host_key=challenge)
        if not attempt.confirmation.wait(self._host_key_timeout):
            raise _HostKeyTimeout()
        self._check(attempt)
        if not attempt.decision:
            raise _HostKeyRejected()
        with self._lock:
            attempt.deadline = time.monotonic() + self._auth_timeout
            self._publish(attempt, "authenticating")

    def _run(self, attempt):
        kwargs = {}
        try:
            self._check(attempt)
            if _paramiko is None:
                self._fail(attempt, "connection_failed", "dependency_missing")
                return
            manager = self

            class ConfirmUnknown(_paramiko.MissingHostKeyPolicy):
                def missing_host_key(self, _client, _hostname, key):
                    manager._host_key(attempt, key)

            client = _paramiko.SSHClient()
            with attempt.resources_lock:
                self._check(attempt)
                attempt.client = client
            if self._known_hosts.exists():
                client.load_host_keys(str(self._known_hosts))
            client.set_missing_host_key_policy(ConfirmUnknown())
            kwargs = {"hostname": attempt.host, "port": attempt.port, "username": attempt.username,
                      "timeout": min(self._connect_timeout, 15), "banner_timeout": min(self._connect_timeout, 15),
                      "auth_timeout": self._auth_timeout,
                      "allow_agent": attempt.auth_method == "agent", "look_for_keys": attempt.auth_method == "agent"}
            if attempt.auth_method == "password":
                kwargs["password"] = attempt.credentials.get("password", "")
            elif attempt.auth_method == "key":
                kwargs["key_filename"] = str(Path(attempt.key_path).expanduser())
                kwargs["passphrase"] = attempt.credentials.get("passphrase") or None
            self._check(attempt)
            kwargs["sock"] = self._connect_socket(attempt)
            self._check(attempt)
            try:
                client.connect(**kwargs)
            finally:
                kwargs.clear()
                attempt.credentials.clear()
                _clear_auth_credentials(client)
            self._check(attempt)
            transport = client.get_transport()
            if not transport or not transport.is_active() or not transport.is_authenticated():
                self._fail(attempt, "authentication_failed", "authentication_failed")
                return
            transport.set_keepalive(30)
            server = _ForwardServer(("127.0.0.1", 0), _ForwardHandler)
            server.attempt = attempt
            thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True)
            with attempt.resources_lock:
                if attempt.cancelled.is_set():
                    server.server_close()
                    raise _Cancelled()
                attempt.server, attempt.server_thread = server, thread
                thread.start()
                attempt.forward_endpoint = ("127.0.0.1", server.server_address[1])
            with self._lock:
                attempt.deadline = None
                if not self._publish(attempt, "connected"):
                    raise _Cancelled()
            while not attempt.cancelled.wait(0.2):
                if not transport.is_active():
                    self._fail(attempt, "connection_failed", "ssh_transport_closed")
                    return
        except _Cancelled:
            pass
        except _HostKeyRejected:
            self._fail(attempt, "connection_failed", "host_key_rejected")
        except _HostKeyTimeout:
            self._fail(attempt, "connection_failed", "host_key_confirmation_timeout")
        except Exception as exc:
            if _paramiko is not None and isinstance(exc, _paramiko.BadHostKeyException):
                self._fail(attempt, "host_key_mismatch", "host_key_mismatch")
            elif _paramiko is not None and isinstance(exc, _paramiko.AuthenticationException):
                self._fail(attempt, "authentication_failed", "authentication_failed")
            elif _paramiko is not None and isinstance(exc, _paramiko.PasswordRequiredException):
                self._fail(attempt, "authentication_failed", "key_passphrase_required")
            else:
                self._fail(attempt, "connection_failed", "connection_failed")
        finally:
            kwargs.clear()
            attempt.credentials.clear()
            self._cancel(attempt)
