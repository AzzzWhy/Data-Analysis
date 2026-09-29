#!/usr/bin/env python3
"""Localhost workbench -- a browser front end for the analysis agent.

The agent already speaks through a CLI, a plain terminal and a Textual app, all of which print
the answer *below* the question. Repeated exploration of one large dataset wants the chart and
the table beside the question instead, which is what this is for.

Two rules govern everything below:

  1. The server renders facts it was given. Every number, engine label and memory figure comes
     from a tool result or from the session worker's own `list` reply. Nothing here computes a
     routing decision, an estimate or a memory prediction, and nothing invents a value when the
     engine emitted none.
  2. It is optional and degradable. A missing `gui/` folder, an occupied port or an uninstalled
     model client must never affect `agent_main.py`, `tui_app.py` or the analysis engine.

Stdlib only, by the same reasoning that keeps the deliverable generator on the standard library:
this is an interface layer, and it must not be a reason an analysis cannot run.

    python agent/gui.py [--port 8765]          # serve directly
    python agent/agent_main.py --gui           # serve through the agent's own startup
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import inspect
import json
import mimetypes
import os
import secrets
import sys
import tempfile
import threading
import time
from collections import deque
from dataclasses import replace
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import urllib.error
import urllib.request
from urllib.parse import parse_qs, urlparse

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import skills  # noqa: E402
from execution_outcome import tool_result_failed  # noqa: E402

# The model layer is a separate dependency from the analysis layer. Importing it must not be a
# condition for serving a page: on a machine that has not installed requirements.txt the workbench
# still boots, still lists data, still runs tools directly, and says plainly why the question box
# is disabled -- quoting the real ImportError rather than a canned message.
AGENT_IMPORT_ERROR = ""
try:
    from agent_main import Agent, build_client  # noqa: E402
    from api_config import (APIConfig, discover_models, load_config,  # noqa: E402
                            normalize_url, save_config)
except Exception as _exc:  # pragma: no cover - depends on the operator's environment
    Agent = None
    build_client = None
    APIConfig = None
    discover_models = None
    load_config = None
    normalize_url = None
    save_config = None
    AGENT_IMPORT_ERROR = f"{type(_exc).__name__}: {_exc}"

# Chrome vocabulary. Imported from the same module the TUI uses so both frontends translate from
# one table. It is UI labels only: model prose and engine values never pass through it.
try:
    import ui_i18n
except Exception:  # pragma: no cover - the table has no third-party dependencies
    ui_i18n = None


def chrome(language: str) -> dict:
    """The label table for one language. Chinese is the source, so it maps to itself."""
    if ui_i18n is None:
        return {}
    if language == "en":
        return {zh: ui_i18n.tr("en", zh) for zh in ui_i18n.STRINGS}
    return {zh: zh for zh in ui_i18n.STRINGS}

GUI_DIR = HERE / "gui"
DEFAULT_PORT = 8765
EVENT_BUFFER = 200          # a reload re-attaches and replays; it is not a run log
# The one build stamp every page shows ({{BUILD}} in the html files). Bump it per release; a
# tag that says which page you are looking at is worth nothing if it lags the code it names.
BUILD_TAG = "guiv2 · b10"
# How many finished runs stay replayable. Each one holds its whole transcript and result payload,
# and the dict was never trimmed: a workbench left open for a day accumulated one per question.
JOB_HISTORY = 20
# The worker's own read has no deadline: skills._worker_call accepts a `timeout` argument and
# then never uses it, so a wedged worker blocks the readline forever while holding the module
# lock. Every call the workbench makes therefore needs a deadline it owns itself.
WORKER_TIMEOUT_SECONDS = 20.0
SESSION_TTL_SECONDS = 7 * 24 * 3600
GATE_KDF_ITERATIONS = 200_000
CSP = ("default-src 'self'; style-src 'self'; script-src 'self'; "
       "connect-src 'self'; img-src 'self' data:")


def _directory_writable(path: Path) -> bool:
    """True when a file can be created in path. A failed probe leaves nothing behind."""
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".write-probe"
        probe.write_text("", encoding="utf-8")
        probe.unlink(missing_ok=True)
        return True
    except OSError:
        return False


def _config_root() -> Path:
    """Where gate.json and connection.json live.

    XDG_CONFIG_HOME wins. Otherwise ~/.config, unless this profile cannot create it
    (Windows home directories are sometimes read-only for the user). Then AppData.
    Mirrored in api_config.config_path(); keep the two in step.
    """
    forced = os.environ.get("XDG_CONFIG_HOME")
    if forced:
        return Path(forced).expanduser()
    chosen = getattr(_config_root, "chosen", None)
    if chosen is not None:
        return chosen
    home_config = Path.home() / ".config"
    if _directory_writable(home_config):
        chosen = home_config
    else:
        appdata = os.environ.get("APPDATA") or os.environ.get("LOCALAPPDATA")
        chosen = Path(appdata).expanduser() if appdata else home_config
    _config_root.chosen = chosen
    return chosen


def gate_file() -> Path:
    """gate.json sits beside connection.json, resolved by the same rules.

    Mirrored from api_config.config_path() rather than imported, because importing
    api_config pulls in the openai SDK: a machine that has not installed
    requirements.txt still gets a gate that works.
    """
    override = os.environ.get("GPU_ANALYSIS_CONFIG")
    if override:
        return Path(override).expanduser().parent / "gate.json"
    return _config_root() / "gpu-data-analysis" / "gate.json"


def diag_file() -> Path:
    """diag.json: the page's own layout report, beside the config it does not touch."""
    return gate_file().parent / "diag.json"


def load_gate_credential() -> tuple[bytes, int, bytes] | None:
    """(salt, iterations, digest) for the browser-set password, or None when unset.

    The file holds a PBKDF2 digest, never the password. Its shape is checked rather
    than trusted, so a corrupt or hand-edited file reads as "no password set" and the
    gate reopens for first-run setup, instead of crashing or accepting nonsense.
    """
    try:
        data = json.loads(gate_file().read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return None
        if data.get("setup_required") is True:
            return None
        salt = bytes.fromhex(data["salt"])
        digest = bytes.fromhex(data["digest"])
        iterations = int(data.get("iterations", GATE_KDF_ITERATIONS))
    except (OSError, ValueError, TypeError, KeyError, AttributeError, json.JSONDecodeError):
        return None
    if len(salt) < 8 or len(digest) != 32 or iterations < 1:
        return None
    return salt, iterations, digest


def load_gate_setup_required() -> bool:
    """An explicit reset must not silently reopen a loopback gate after restart."""
    try:
        data = json.loads(gate_file().read_text(encoding="utf-8"))
        return isinstance(data, dict) and data.get("setup_required") is True
    except (OSError, ValueError, TypeError):
        return False


def _write_gate_record(payload: dict) -> None:
    """Replace one gate record atomically; failed writes retain the old credential."""
    target = gate_file()
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=target.parent,
                                         prefix=".gate-", suffix=".tmp", delete=False) as output:
            temporary = Path(output.name)
            json.dump(payload, output, indent=2)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, target)
        temporary = None
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


def _is_loopback(ip: str) -> bool:
    return ip in ("127.0.0.1", "::1", "::ffff:127.0.0.1")


def _gate_mode(server) -> str:
    """One of 'signin', 'setup' or 'open', computed from live server state.

    A launch-time --token or a stored password makes the gate 'signin'. An explicit
    reset or a gateway-required gate makes it 'setup' until a password is created.
    Otherwise a loopback binding stays 'open' (the workbench you started for yourself), while a
    remote binding becomes 'setup': nothing unauthenticated is served, the first visit
    from the server's own machine claims the password, and everyone else waits.
    """
    if getattr(server, "token_digest", None) is not None:
        return "signin"
    if getattr(server, "gate_credential", None) is not None:
        return "signin"
    if getattr(server, "require_setup", False):
        return "setup"
    if server.server_address[0] in ("127.0.0.1", "::1"):
        return "open"
    if os.environ.get("GPU_GUI_ALLOW_REMOTE") == "1":
        return "open"
    return "setup"


def _reset_origin_allowed(server, headers) -> bool:
    """Reject cross-site reset posts, including simple form posts to loopback.

    No Origin remains useful to a CLI and the nonce-checked local gateway. A
    browser Origin must match the literal HTTP authority and actual bound port;
    unlike the gateway, this endpoint has no nonce to justify repairing one.
    """
    def authority(value: str, *, origin: bool):
        if not value or any(character.isspace() for character in value) or "\\" in value:
            return None
        try:
            parsed = urlparse(value if origin else "http://" + value)
            if (parsed.scheme != "http" or not parsed.hostname or parsed.username is not None
                    or parsed.password is not None or parsed.path or parsed.params
                    or parsed.query or parsed.fragment):
                return None
            return parsed.hostname.lower(), parsed.port if parsed.port is not None else 80
        except ValueError:
            return None

    hosts = headers.get_all("Host")
    if not hosts or len(hosts) != 1:
        return False
    target = authority(hosts[0], origin=False)
    if target is None or target[1] != server.server_address[1]:
        return False
    if server.server_address[0] in ("127.0.0.1", "::1"):
        if target[0] != "localhost" and not _is_loopback(target[0]):
            return False
    origins = headers.get_all("Origin")
    return origins is None or (len(origins) == 1 and authority(origins[0], origin=True) == target)


def _probe_config_dir() -> bool:
    """True when the config directory can actually hold a file.

    A hardened user profile can leave the default ~/.config uncreatable for a normal
    user; a browser-set gate password (and every saved connection setting) then fails
    at the worst possible moment. The server says so at startup instead, naming the
    way out: XDG_CONFIG_HOME pointed at a writable directory.
    """
    try:
        gate_file().parent.mkdir(parents=True, exist_ok=True)
        probe = gate_file().parent / ".write-probe"
        probe.write_text("", encoding="utf-8")
        probe.unlink(missing_ok=True)
        return True
    except OSError:
        return False

# Tools the workbench may invoke directly. Kept explicit: an HTTP body must never become a
# call to something chosen by the caller.
RUNNABLE_TOOLS = ("analyze_dataset", "dataset_session", "export_deliverables", "list_datasets")

# There is no `mode == "resident reuse"`. Warmth is recorded in `policy` and in `observed.phase`,
# and `resident_reuse` is reachable on the pandas path while `warm_cache` is not, so both
# spellings have to count.
WARM_POLICIES = ("resident_reuse", "warm_cache")
WARM_PHASES = ("session_reuse", "warm_cache_hit")


def call_with_timeout(fn, timeout: float = WORKER_TIMEOUT_SECONDS, *args, **kwargs):
    """Run `fn` off-thread and say so when it did not finish, instead of hanging the server.

    A wedged worker still holds the memory, so reporting success would be a lie; the caller gets
    an `error` key and renders that.
    """
    box: dict = {"finished": threading.Event()}

    def target():
        try:
            box["value"] = fn(*args, **kwargs)
        except Exception as exc:
            box["error"] = f"{type(exc).__name__}: {exc}"
        finally:
            box["finished"].set()

    threading.Thread(target=target, daemon=True).start()
    if not box["finished"].wait(timeout):
        return {"error": f"worker did not answer within {timeout}s; it may still be holding "
                         f"memory -- nothing was confirmed released"}
    if "error" in box:
        return {"error": box["error"]}
    value = box.get("value")
    return value if isinstance(value, dict) else {
        "error": f"worker returned {type(value).__name__}, expected an object"}


def decision_is_warm(decision) -> bool:
    if not isinstance(decision, dict):
        return False
    return (decision.get("policy") in WARM_POLICIES
            or (decision.get("observed") or {}).get("phase") in WARM_PHASES)


def chip_state(payload: dict) -> dict:
    """Engine chip text, style class and note, read from the decision record.

    `selected_backend` versus `actual_backend` is the precise fallback signal. The flat
    `fallback_reason != null` heuristic is not equivalent: the engine also sets it when cuDF was
    never attempted, so keying off it alone would paint a deliberate CPU route as a defect --
    the exact inversion this project exists to avoid. Fallback is tested before choice for the
    same reason.

    Returns `observed_on_this_machine: false` for states the engine can produce but a CPU-only
    box cannot, so the frontend labels them as unobservable instead of substituting something.
    """
    decision = payload.get("execution_decision") or {}
    selected = decision.get("selected_backend")
    actual = decision.get("actual_backend")
    policy = decision.get("policy")
    fallback = decision.get("fallback_reason") or payload.get("fallback_reason")
    reason = decision.get("reason")
    estimate = decision.get("estimate") or {}
    elapsed = estimate.get("elapsed_seconds")

    if actual == "cudf" and not fallback:
        warm = decision_is_warm(decision)
        label, cls = ("GPU · cuDF (reuse)" if warm else "GPU · cuDF"), "gpu"
        if policy == "warm_cache":
            note = "frame came from the bounded warm cache; the file was not re-read"
        elif warm:
            note = "resident session reused; the file was not re-read"
        else:
            note = policy or ""
        observable = True
    elif selected == "cudf" and actual == "pandas":
        label, cls, note = "CPU · pandas (fallback)", "warn", str(fallback or "")
        observable = True
    elif actual == "pandas":
        label = "CPU · pandas (by choice)" if reason else "CPU · pandas"
        cls, note = "cpu", str(reason or payload.get("gpu") or "")
        observable = True
    else:
        label, cls, note = "no result yet", "muted", ""
        observable = False

    return {
        "label": label,
        "class": cls,
        "note": note,
        "policy": policy,
        "mode": decision.get("mode"),
        # An admission headroom check is a safety threshold, not a prediction of peak memory, so
        # peak_memory_mb is always None by design. Absence is rendered as absence.
        "estimate_status": estimate.get("status"),
        "estimate_seconds": elapsed,
        "peak_memory_mb": estimate.get("peak_memory_mb"),
        "observable": observable,
    }


class Job:
    """One run's event stream, buffered so a disconnected viewer can re-attach and replay.

    Events carry a monotonic sequence number rather than a position in the buffer: the buffer is
    bounded, so a long run would shift indices under a reconnecting client and replay the wrong
    slice.
    """

    def __init__(self, kind: str, label: str):
        self.id = "j" + secrets.token_hex(16)
        self.kind = kind            # "ask" (model-driven) or "run" (direct tool call)
        self.label = label
        self.events: deque = deque(maxlen=EVENT_BUFFER)
        self.sequence = 0
        self.rounds = 0
        self.done = False
        self.status = "running"
        self.started_at = time.time()
        self.finished_at = None
        self.tool_failures = 0
        self.condition = threading.Condition()
        self.started = time.perf_counter()

    def emit(self, name: str, payload) -> None:
        with self.condition:
            self.sequence += 1
            self.events.append((self.sequence, name, payload))
            self.condition.notify_all()

    def finish(self, status: str = "succeeded") -> None:
        with self.condition:
            self.status = status
            self.done = True
            self.finished_at = time.time()
            self.condition.notify_all()

    def summary(self) -> dict:
        with self.condition:
            return {"id": self.id, "kind": self.kind, "label": self.label,
                    "status": self.status, "done": self.done,
                    "started_at": self.started_at, "finished_at": self.finished_at,
                    "sequence": self.sequence,
                    "first_sequence": self.events[0][0] if self.events else 0}

    def since(self, cursor: int):
        """(events after cursor, finished). Blocks briefly instead of busy-polling."""
        with self.condition:
            newest = self.events[-1][0] if self.events else 0
            if newest <= cursor and not self.done:
                self.condition.wait(1.0)
            return [e for e in self.events if e[0] > cursor], self.done


def _model_ids(base_url: str, api_key: str) -> list[str]:
    """Read /models from one OpenAI-compatible address. The key stays in the request."""
    endpoint = base_url.rstrip("/") + "/models"
    request = urllib.request.Request(
        endpoint,
        headers={
            "Authorization": "Bearer " + api_key,
            "Accept": "application/json",
            "User-Agent": "gpu-workbench",
        },
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        raise ValueError(f"无法读取模型列表（HTTP {exc.code}）。检查地址和密钥。") from None
    except Exception as exc:
        raise ValueError(f"无法读取模型列表（{type(exc).__name__}）。检查地址、密钥或网络。") from None
    try:
        payload = json.loads(raw.decode("utf-8"))
    except Exception:
        raise ValueError("这个地址返回的模型列表不是 JSON。") from None
    rows = None
    if isinstance(payload, dict):
        rows = payload.get("data")
        if not isinstance(rows, list):
            rows = payload.get("models")
    if not isinstance(rows, list):
        raise ValueError("这个地址没有返回模型列表。")
    names = []
    for item in rows:
        if isinstance(item, str) and item.strip():
            names.append(item.strip())
        elif isinstance(item, dict):
            name = item.get("id") or item.get("name") or item.get("model")
            if isinstance(name, str) and name.strip():
                names.append(name.strip())
    return sorted(set(names))


class Workbench:
    """Holds the single agent, the single session worker behind it, and the job registry.

    One agent and one lock are not a simplification: `skills._worker_call` holds a module-level
    lock for a whole request/response round trip against one long-lived worker process, so
    concurrent analyses were never on the table. A second request is refused with 409 rather
    than queued, because a queue would look like a hang from the browser.
    """

    def __init__(self, model_override: str | None = None):
        self.lock = threading.RLock()
        self.busy = False
        self.jobs: dict[str, Job] = {}
        self.instance_id = secrets.token_hex(16)
        self.agent = None
        self.config_ready = False
        self.missing_fields: list[str] = []      # filled once a config file could be read
        self.remember_key = False
        self._config = None  # Runtime-only credentials; never serialized in state/jobs.
        self.model = None
        # `--model` is a launch-time override and the only thing allowed to outrank the file. It has
        # to be remembered separately: `apply_settings` used to reconfigure with `self.model` -- the
        # model the screen was showing *before* the edit -- so saving a new model ID wrote it to disk
        # and then immediately overrode it in memory. Measured on the node: disk had beta-model while
        # /api/state still reported alpha-model, and the running agent kept using the old one.
        self.model_override: str | None = None
        self.base_url = ""
        self.language = "zh"
        self.last_error = ""
        self.httpd = None
        # The most recent decision record, kept so the session card can say whether the last
        # answer was served from memory. It is read from a tool result, never inferred.
        self.last_decision: dict = {}
        # Artifact roots grow as runs report their own output directories. export_deliverables
        # defaults to <dataset dir>/deliverables, which is not under the job dir or benchmark/,
        # so a fixed list would reject every real chart.
        self.artifact_roots: set[Path] = set()
        for env_dir in (os.environ.get("DEMO_DATA_DIR"),):
            if env_dir:
                self._add_root(env_dir)
        self._add_root(HERE.parent / "benchmark")
        self.model_override = model_override
        self._configure(model_override)

    # -- configuration and honest state ------------------------------------------------

    def session_event(self) -> dict:
        """Payload for one `session` SSE frame: the worker's own numbers, plus the reuse flag."""
        status = self.session_status()
        return {**self.session_doc(status), "reused": decision_is_warm(self.last_decision)}

    def _configure(self, model_override: str | None, config=None) -> None:
        self.agent = None
        self.config_ready = False
        self.last_error = ""
        if Agent is None or load_config is None:
            return
        try:
            config = replace(config) if config is not None else load_config()
        except Exception as exc:
            self.last_error = f"connection settings unreadable: {type(exc).__name__}"
            return
        self._config = replace(config)
        if model_override:
            config.model = model_override
        self.config_ready = bool(config.ready)
        # Which of the three readiness conditions is actually missing, by name only -- never a value,
        # and never a guess. `api_config.APIConfig.ready` is a conjunction (api_config.py:23), so a
        # bare `false` cannot tell the operator whether to type a key or pick a model, and the UI
        # had one fixed sentence covering all three cases. Absent when the file could not be read:
        # then nothing is known, which is a different claim from nothing being configured.
        self.missing_fields = [name for name, value in (("base_url", config.base_url),
                                                         ("model", config.model),
                                                         ("api_key", config.api_key))
                               if not str(value or "").strip()]
        self.model = config.model or None
        self.base_url = config.base_url
        self.language = getattr(config, "language", "zh")
        # The settings form needs to show the truth about the checkbox it is about to submit.
        # `remember_key` is a persisted preference, not a secret; the key itself stays out of
        # every payload this server emits.
        self.remember_key = bool(getattr(config, "remember_key", False))
        if self.config_ready:
            try:
                self.agent = Agent(build_client(config), verbose=False, model=config.model,
                                   reuse_one_shot=True)
            except Exception as exc:
                self.config_ready = False
                self.last_error = f"model client could not start: {type(exc).__name__}"

    def apply_settings(self, patch: dict) -> dict:
        # Do not replace the Agent while its in-flight turn is emitting events.
        with self.lock:
            if self.busy:
                return {"ok": False, "error": "busy: wait for the current analysis before changing settings"}
            return self._apply_settings(patch)

    def _apply_settings(self, patch: dict) -> dict:
        """Write connection settings through api_config, then rebuild the agent.

        The key is accepted once and never returned. The response reports whether one is stored,
        as a boolean -- a server that echoes a credential can end up with that credential in a
        browser history entry, a screenshot or a log file, and this screen is designed to be put
        on camera.
        """
        if load_config is None or save_config is None:
            return {"ok": False, "error": f"settings layer unavailable: {AGENT_IMPORT_ERROR}"}
        if not isinstance(patch, dict):
            return {"ok": False, "error": "settings must be an object"}
        for field in ("base_url", "api_key", "model", "language"):
            if field in patch and not isinstance(patch[field], str):
                return {"ok": False, "error": f"{field} must be text"}
        for field in ("remember_key", "skip_setup"):
            if field in patch and not isinstance(patch[field], bool):
                return {"ok": False, "error": f"{field} must be a boolean"}
        try:
            config = replace(self._config) if self._config is not None else load_config()
        except Exception as exc:
            return {"ok": False, "error": f"could not read current settings: {exc}"}
        was = (config.base_url, config.model, config.api_key, config.skip_setup,
               config.remember_key, config.language)

        warning = ""
        new_base = str(patch.get("base_url") or "").strip()
        if new_base:
            try:
                target = normalize_url(new_base)
            except Exception as exc:
                # api_config authors its refusals in Chinese, and those exact sentences are already
                # in the label table -- so the reason can be shown in either language instead of
                # leaking one Chinese sentence into an English screen. The English wrapper stays: it
                # names which field was rejected, which is not something the table holds.
                reason = ui_i18n.tr(self.language, str(exc)) if ui_i18n else str(exc)
                return {"ok": False, "error": f"base_url rejected: {reason}"}
            if target != config.base_url:
                # A different provider must not inherit the previous provider's key, and a model
                # id from the old provider is meaningless at the new one.
                config.api_key = ""
                config.model = ""
            config.base_url = target
        for field in ("model", "skip_setup", "remember_key", "language"):
            if field in patch:
                setattr(config, field, patch[field])
        if patch.get("api_key", "").strip():
            config.api_key = patch["api_key"].strip()
        config.model = config.model.strip()
        if config.api_key and not config.remember_key:
            warning = ("密钥仅在当前后端进程内存中使用，不保存到磁盘；后端重启后需重新输入。"
                       if config.language == "zh" else
                       "The key is used in backend process memory only, not on disk; enter it again after a backend restart.")

        try:
            if (config.base_url, config.model, config.api_key, config.skip_setup,
                    config.remember_key, config.language) != was:
                # A no-op settings submit used to rewrite the operator's connection.json (its mtime
                # moved even when every field was identical), and a refused key rewrote it too.
                save_config(config)
        except Exception as exc:
            # save_config validates the language and refuses anything but zh/en.
            return {"ok": False, "error": f"settings not saved: {type(exc).__name__}: {exc}"}

        previous = self._config
        if (self.agent is not None and previous is not None
                and (previous.base_url, previous.model, previous.api_key) ==
                    (config.base_url, config.model, config.api_key)):
            # Presentation/persistence preferences must not erase conversation history.
            self._config = replace(config)
            self.language = config.language
            self.remember_key = config.remember_key
        else:
            self._configure(self.model_override, config=config)
        response = {
            "ok": True,
            "base_url": config.base_url,
            "model": config.model or None,
            "language": config.language,
            "config_ready": self.config_ready,
            # The settings form's closing line is now derived from this, not from a fixed sentence
            # that claimed all three fields were missing when only one was.
            "missing_fields": list(self.missing_fields),
            # A key can be live in memory without being on disk -- that is the documented default.
            # `save_config` writes the field only when remember_key is true, so disk truth is that
            # flag plus a non-empty key, not merely a non-empty key.
            "api_key_stored": bool(config.remember_key and config.api_key),
            "warning": warning,
        }
        return response

    def list_models(self, body: dict) -> dict:
        """Ask the address in the form for its model ids. The key is never returned.

        A key typed into this request is used only for that one /models call. If the
        field is empty and the address matches the saved one, the stored key is used.
        A different address never inherits the previous provider's key.
        """
        if load_config is None or normalize_url is None or APIConfig is None or discover_models is None:
            return {"ok": False, "error": f"settings layer unavailable: {AGENT_IMPORT_ERROR}"}
        try:
            config = replace(self._config) if self._config is not None else load_config()
        except Exception as exc:
            return {"ok": False, "error": f"could not read current settings: {exc}"}
        raw = str(body.get("base_url") or "").strip() or config.base_url
        try:
            target = normalize_url(raw)
            stored = normalize_url(config.base_url) if str(config.base_url or "").strip() else ""
        except Exception as exc:
            reason = ui_i18n.tr(self.language, str(exc)) if ui_i18n else str(exc)
            return {"ok": False, "error": reason}
        key = str(body.get("api_key") or "").strip()
        if not key and target == stored:
            key = config.api_key
        if not key:
            return {"ok": False, "error": "请先填写 API 密钥，再读取模型。本地无鉴权服务可填 local。"}
        try:
            models = _model_ids(target, key)
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        if not models:
            return {"ok": False, "error": "这个地址没有返回可选模型。检查地址和密钥后再试。"}
        return {"ok": True, "models": models}

    def session_status(self) -> dict:
        """Ask the worker what it holds, with a deadline this module owns.

        Two things to know before changing this. `skills.dataset_session` returns a JSON
        *string*, not a dict. And the worker's `list` command runs `_prune_cache()` first, so it
        is not a read-only peek: it can expire frames by TTL and evict them by byte budget, which
        changes the very numbers it reports. That is why nothing here polls -- the card refreshes
        only after a run or an explicit release.
        """
        def ask():
            try:
                return json.loads(skills.dataset_session(operation="list"))
            except Exception as exc:
                return {"error": f"{type(exc).__name__}: {exc}"}
        return call_with_timeout(ask)

    def session_doc(self, status: dict) -> dict:
        """Shape the worker's reply for the card. Unknown stays None; it never becomes 0.

        A 0 reads as "nothing is held", which is a different claim from "the worker did not
        answer" -- and the second one is the case where memory may still be held.
        """
        error = status.get("error")
        if error:
            return {"sessions": None, "warm_frames": None, "warm_cache_mb": None,
                    "max_sessions": None, "sessions_detail": None, "error": error}
        return {
            "sessions": status.get("count"),
            "warm_frames": status.get("warm_cache_count"),
            "warm_cache_mb": status.get("warm_cache_mb"),
            "max_sessions": status.get("max_sessions"),
            "sessions_detail": status.get("sessions", []),
            "error": "",
        }

    def engine_state(self) -> dict:
        """What the analysis layer can prove right now, by asking it -- never by assuming."""
        status = self.session_status()
        doc = self.session_doc(status)
        return {
            "engine_ready": not status.get("error"),
            "engine_error": status.get("error", ""),
            "agent_available": Agent is not None,
            "agent_import_error": AGENT_IMPORT_ERROR,
            "config_ready": self.config_ready,
            "missing_fields": list(self.missing_fields),
            "model": self.model,
            # The address is not a secret and the settings form needs to show what is configured;
            # the key itself is never part of this payload.
            "base_url": self.base_url,
            "language": self.language,
            "remember_key": self.remember_key,
            "i18n": chrome(self.language),
            "model_error": self.last_error,
            "busy": self.busy,
            "session": {**doc, "reused": decision_is_warm(self.last_decision)},
        }

    # -- artifact allow-list -----------------------------------------------------------

    def _add_root(self, path) -> None:
        try:
            resolved = Path(path).expanduser().resolve()
        except Exception:
            return
        if resolved.is_dir():
            self.artifact_roots.add(resolved)

    def note_artifact_root(self, path) -> None:
        """Register a directory a tool result just reported as its own output location."""
        if path:
            self._add_root(path)

    def resolve_artifact(self, raw: str):
        """Return an allowed file path, or None. Traversal and escaping symlinks are refused.

        parse_qs has already decoded the query, so this must not decode again: a second pass
        would turn a literal "%252e%252e/" into "../" after the check that is supposed to stop it.
        """
        if not raw:
            return None
        try:
            candidate = Path(str(raw)).expanduser()
            candidate = (HERE if not candidate.is_absolute() else candidate).resolve()
        except Exception:
            return None
        if not candidate.is_file():
            return None
        return candidate if any(candidate.is_relative_to(root)
                                for root in self.artifact_roots) else None

    # -- running things ----------------------------------------------------------------

    def job_catalog(self, job_id: str | None = None) -> dict:
        # Reading history never starts/touches the analysis worker or executes a tool.
        with self.lock:
            if job_id is not None:
                job = self.jobs.get(job_id)
                return {"instance_id": self.instance_id,
                        "job": job.summary() if job else None}
            jobs = [job.summary() for job in reversed(list(self.jobs.values()))]
            return {"instance_id": self.instance_id, "busy": self.busy,
                    "active_job_id": next((job["id"] for job in jobs if not job["done"]), None),
                    "jobs": jobs}

    def _complete_job(self, job: Job, status: str, **payload) -> None:
        with self.lock:
            job.emit("done", {**payload, "status": status, "success": status == "succeeded"})
            job.finish(status)
            self.busy = False
            self._prune_jobs()

    def _acquire(self, kind: str, label: str):
        if self.busy:
            return None
        with self.lock:
            if self.busy:
                return None
            self.busy = True
            job = Job(kind, label)
            self.jobs[job.id] = job
            self._prune_jobs()
            return job

    def _prune_jobs(self) -> None:
        """Trim the replay cache to the newest JOB_HISTORY entries. Caller holds self.lock.

        Only finished runs are eligible: dropping the live one would hand a reconnecting viewer a
        404 for the run it is watching, which is the exact failure this buffer exists to prevent.
        Insertion order is kept, so "oldest finished" is a plain scan from the front.
        """
        overflow = len(self.jobs) - JOB_HISTORY
        if overflow <= 0:
            return
        for job_id in [jid for jid, job in self.jobs.items() if job.done][:overflow]:
            del self.jobs[job_id]

    def release(self, timeout: float = WORKER_TIMEOUT_SECONDS) -> dict:
        with self.lock:
            if self.busy:
                return {"ok": False, "error": "busy: wait for the current analysis before releasing sessions"}
            self.busy = True
        try:
            return self._release(timeout)
        finally:
            with self.lock:
                self.busy = False

    def _release(self, timeout: float) -> dict:
        """Close sessions AND drop warm frames, off-thread, with our own deadline.

        skills.release_all_sessions_and_cache() already re-reads the after-counts; what it cannot
        do is time out, because the underlying worker read has no deadline. A worker that never
        answers still holds the memory, so this reports the wedge instead of showing "released".
        """
        result = call_with_timeout(skills.release_all_sessions_and_cache, timeout)
        if not result.get("error"):
            result["state"] = self.session_doc(self.session_status())
        return result

    def reset_all(self) -> dict:
        with self.lock:
            if self.busy:
                return {"ok": False, "error": "busy: wait for the current analysis before resetting"}
            self.busy = True
        try:
            return self._reset_all()
        finally:
            with self.lock:
                self.busy = False

    def _reset_all(self) -> dict:
        """Return this machine's workbench state to out-of-the-box, and say exactly what
        that means.

        Reset here: resident sessions and the warm cache, the job history, tracked
        artifact roots, and the saved connection settings (address, key, model,
        language) -- plus, from the HTTP layer that calls this, the gate password and
        every sign-in session id. Deliberately NOT reset: dataset files and generated
        reports. They are inputs and outputs the operator chose; a button that deletes
        files a request can name is a footgun wearing a feature's name, and the UI
        copy says so rather than implying a wiped disk.
        """
        result = call_with_timeout(skills.release_all_sessions_and_cache, WORKER_TIMEOUT_SECONDS)
        if not isinstance(result, dict) or tool_result_failed(result):
            error = result.get("error") if isinstance(result, dict) else None
            return {"ok": False, "error": error if isinstance(error, str) and error.strip()
                    else "could not release resident sessions and warm cache"}
        done = ["resident sessions and warm cache released"]
        self.jobs.clear()
        self.instance_id = secrets.token_hex(16)
        self.artifact_roots = set()
        for env_dir in (os.environ.get("DEMO_DATA_DIR"),):
            if env_dir:
                self._add_root(env_dir)
        self._add_root(HERE.parent / "benchmark")
        done.append("job history and artifact tracking cleared")
        if save_config is not None and APIConfig is not None:
            try:
                save_config(APIConfig())
                self.agent = None
                self.last_error = ""
                self._configure(self.model_override)
                done.append("connection settings restored to defaults "
                            "(address, key, model, language)")
            except OSError as exc:
                return {"ok": False, "error": f"could not rewrite the config file: {exc}"}
        else:
            done.append("connection settings left alone: api_config is unavailable")
        return {"ok": True, "reset": done}

    def start_direct_run(self, tool: str, args: dict):
        if tool not in RUNNABLE_TOOLS:
            return None, f"tool is not runnable from the workbench: {tool}"
        job = self._acquire("run", tool)
        if job is None:
            return None, "busy"
        thread = threading.Thread(target=self._run_direct, args=(job, tool, args), daemon=True)
        thread.start()
        return job, ""

    def _run_direct(self, job: Job, tool: str, args: dict) -> None:
        """Call the real skill function through the same event path a model-driven turn uses.

        Argument names are filtered against the target's own signature, so an HTTP body cannot
        reach a parameter the tool does not declare.
        """
        started = time.perf_counter()
        status = "failed"
        try:
            fn = getattr(skills, tool)
            accepted = inspect.signature(fn).parameters
            clean = {k: v for k, v in (args or {}).items() if k in accepted}
            dropped = sorted(set((args or {}).keys()) - set(clean))
            job.emit("phase", {"phase": "tool"})
            job.rounds += 1
            job.emit("tool_call", {"round": job.rounds, "name": tool, "args": clean,
                                   "dropped_args": dropped, "source": "direct"})
            tool_started = time.perf_counter()
            raw = fn(**clean)
            seconds = time.perf_counter() - tool_started
            try:
                parsed = json.loads(raw)
            except (TypeError, json.JSONDecodeError):
                parsed = {"success": False, "error": "tool returned a non-JSON payload"}
            if not isinstance(parsed, dict):
                parsed = {"success": False, "error": "tool returned a non-object payload"}
            job.emit("tool_result", {"round": job.rounds, "name": tool, "args": clean,
                                     "result": parsed, "chip": chip_state(parsed),
                                     "seconds": round(seconds, 3)})
            self._absorb_result(parsed)
            job.emit("session", self.session_event())
            status = "failed" if tool_result_failed(parsed) else "succeeded"
        except Exception as exc:
            job.emit("error", {"message": f"{type(exc).__name__}: {exc}"})
        finally:
            self._complete_job(job, status, seconds=round(time.perf_counter() - started, 3),
                               source="direct")

    def start_ask(self, text: str, file: str | None = None):
        """Begin a model-driven turn. `file`, when given, is stated to the model as context.

        The path is appended as its own labelled line rather than being woven into the question,
        so what the model actually received can be shown verbatim in the log and nothing is
        silently rewritten.
        """
        if Agent is None:
            return None, (f"model client is not available: {AGENT_IMPORT_ERROR}. "
                          f"Install it with: pip install -r requirements.txt")
        prompt = text
        if file:
            resolved = file
            prompt = (f"{text}\n\n"
                      f"[workbench] the dataset selected in the interface is: {resolved}")
        with self.lock:
            if self.agent is None:
                return None, (self.last_error or "no API base URL, key and model are configured. "
                                                 "Set them in Connection settings.")
            job = self._acquire("ask", prompt[:120])
            if job is None:
                return None, "busy"
            agent = self.agent
        job.emit("prompt", {"text": prompt, "file": file or ""})
        thread = threading.Thread(target=self._run_ask, args=(job, prompt, agent), daemon=True)
        thread.start()
        return job, ""

    def _run_ask(self, job: Job, text: str, agent) -> None:
        """Drive one real Agent turn, streaming text to `event` and tool payloads to `result`."""
        started = time.perf_counter()
        status = "failed"
        reason = ""
        outcome_error = ""
        try:
            agent.event_sink = lambda line: job.emit("trace", {"line": line})
            agent.result_sink = self._make_result_sink(job)
            job.emit("phase", {"phase": "thinking"})
            answer = agent.run(text)
            outcome = getattr(agent, "last_run_outcome", None)
            # Legacy adapters without an outcome are unknown, not proven successful.
            if isinstance(outcome, dict) and outcome.get("status") in {"succeeded", "failed", "partial"}:
                status = outcome["status"]
                reason = outcome.get("reason", "")
                if isinstance(outcome.get("error"), str):
                    outcome_error = outcome["error"]  # Agent has already redacted secrets.
                if type(outcome.get("tool_failures")) is int:
                    job.tool_failures = max(job.tool_failures, outcome["tool_failures"])
            else:
                status, reason = "partial", "unknown_outcome"
            if job.tool_failures and status == "succeeded":
                status, reason = "partial", "tool_failures"
            job.emit("answer", {"text": answer})
            job.emit("session", self.session_event())
        except Exception as exc:
            # safe_error strips the API key out of the message; an exception text that echoes a
            # request header must never reach the browser or the event log.
            job.emit("error", {"message": agent.safe_error(exc)})
            status, reason = "failed", "exception"
        finally:
            agent.event_sink = None
            agent.result_sink = None
            self._complete_job(job, status, seconds=round(time.perf_counter() - started, 3),
                               reason=reason, error=outcome_error, tool_failures=job.tool_failures)

    def _make_result_sink(self, job: Job):
        def sink(name: str, payload: dict, seconds: float) -> None:
            job.rounds += 1
            if tool_result_failed(payload):
                job.tool_failures += 1
            # The chip is computed here, once, from the real decision record. The browser renders
            # what it is handed and never re-derives an engine label -- two implementations of
            # "was this the GPU, and was that on purpose" is how the two views drift apart.
            job.emit("tool_result", {"round": job.rounds, "name": name,
                                     "result": payload, "chip": chip_state(payload),
                                     "seconds": round(seconds, 3)})
            self._absorb_result(payload)
            job.emit("session", self.session_event())
        return sink

    def _absorb_result(self, payload) -> None:
        """Take what a tool result tells us: its output dir, and the decision it just made."""
        if not isinstance(payload, dict):
            return
        if isinstance(payload.get("execution_decision"), dict):
            self.last_decision = payload["execution_decision"]
        for key in ("out_dir", "output_dir"):
            self.note_artifact_root(payload.get(key))
        report = payload.get("report")
        if isinstance(report, str) and report:
            self.note_artifact_root(Path(report).parent)

    def shutdown(self) -> None:
        """Stop accepting connections; the caller's finally block releases sessions."""
        if self.httpd is not None:
            self.httpd.shutdown()


class Handler(BaseHTTPRequestHandler):
    server_version = "GPUWorkbench/1"
    workbench: Workbench = None        # set on the class before serve()

    # -- plumbing ----------------------------------------------------------------------

    def log_message(self, fmt, *args):    # keep the console for analysis, not access noise
        if os.environ.get("GPU_GUI_VERBOSE"):
            super().log_message(fmt, *args)

    def _authorized(self) -> bool:
        """True when this request carries a live session, or the gate is open.

        The password itself is sent exactly once: POST /api/login (or /api/setup on
        first run) trades it for a random session id in an HttpOnly, SameSite=Strict
        cookie, which the browser then attaches to fetch() and EventSource() alike.
        That is why the gate moved off HTTP Basic -- its credentials are something
        EventSource is not required to send, and the browser's own dialog is not this
        workbench's design. The session table lives on the server instance, not the
        Handler class, so a second server in one process (the headless tests run
        several) never inherits the first one's gate.
        """
        if _gate_mode(self.server) == "open":
            return True
        try:
            jar = SimpleCookie(self.headers.get("Cookie") or "")
        except CookieError:
            return False
        morsel = jar.get("dws")
        if morsel is None or not morsel.value:
            return False
        sessions = self.server.sessions
        expiry = sessions.get(morsel.value)
        if expiry is None:
            return False
        if expiry < time.time():
            sessions.pop(morsel.value, None)
            return False
        return True

    def _issue_session(self) -> None:
        """Answer 200 with a fresh session cookie. The caller has verified the password."""
        now = time.time()
        for stale, expiry in list(self.server.sessions.items()):
            if expiry < now:
                self.server.sessions.pop(stale, None)
        sid = secrets.token_urlsafe(32)
        self.server.sessions[sid] = now + SESSION_TTL_SECONDS
        self._json(200, {"ok": True, "expires_in": SESSION_TTL_SECONDS},
                   {"Set-Cookie": f"dws={sid}; Path=/; Max-Age={SESSION_TTL_SECONDS}; "
                                  f"HttpOnly; SameSite=Strict"})

    def _gate_check(self, offered: str) -> bool:
        """Constant-time check against whichever credential is active."""
        runtime = getattr(self.server, "token_digest", None)
        if runtime is not None:
            return hmac.compare_digest(hashlib.sha256(offered.encode("utf-8")).digest(),
                                       runtime)
        cred = getattr(self.server, "gate_credential", None)
        if cred is None:
            return False
        salt, iterations, digest = cred
        return hmac.compare_digest(
            hashlib.pbkdf2_hmac("sha256", offered.encode("utf-8"), salt, iterations), digest)

    def _login(self, url) -> None:
        """Trade the access password for a session cookie.

        The password is accepted only in the request body -- the same rule the API key
        follows -- because a query string lands in access logs, proxies and history.
        """
        if "password" in parse_qs(url.query) or "token" in parse_qs(url.query):
            return self._json(400, {"error": "the password must be sent in the request body, "
                                             "never in the query string"})
        mode = _gate_mode(self.server)
        if mode == "setup":
            return self._json(409, {"error": "no password is set yet; "
                                             "the page will offer to create one"})
        if mode == "open":
            return self._json(404, {"error": "this server has no gate; "
                                             "there is nothing to sign in to"})
        body = self._body()
        if body is None:
            return
        offered = str(body.get("password") or "")
        if not self._gate_check(offered):
            # A wrong guess costs the caller a delay, not the server an outage.
            time.sleep(0.8)
            return self._json(401, {"error": "wrong password"})
        self._issue_session()

    def _setup(self, url) -> None:
        """First-run password creation: loopback only, one shot, then the gate is live.

        The browser-set password is stored as a salted PBKDF2 digest in gate.json
        beside connection.json. Only a loopback client may claim an uninitialized gate,
        so binding 0.0.0.0 before the password exists never hands the gate to whoever
        happens to click first on the network.
        """
        if "password" in parse_qs(url.query):
            return self._json(400, {"error": "the password must be sent in the request body, "
                                             "never in the query string"})
        if _gate_mode(self.server) != "setup":
            return self._json(409, {"error": "the gate already has a password. Clear it with "
                                             "--reset-token at the server, or 数据初始化 in "
                                             "the settings panel."})
        if not _is_loopback(self.client_address[0]):
            return self._json(403, {"error": "the first password may only be set from the "
                                             "machine running this server"})
        body = self._body()
        if body is None:
            return
        password = str(body.get("password") or "")
        if not (8 <= len(password) <= 128) or not password.strip():
            return self._json(400, {"error": "the password must be 8-128 characters, "
                                             "and not only whitespace"})
        salt = secrets.token_bytes(16)
        digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt,
                                     GATE_KDF_ITERATIONS)
        payload = {"scheme": "pbkdf2-sha256", "iterations": GATE_KDF_ITERATIONS,
                   "salt": salt.hex(), "digest": digest.hex(),
                   "created": time.strftime("%Y-%m-%dT%H:%M:%S")}
        try:
            _write_gate_record(payload)
        except OSError as exc:
            return self._json(500, {"error": f"could not store the password digest in "
                                             f"{gate_file()}: {exc}. The config directory "
                                             f"must be writable; point XDG_CONFIG_HOME (or "
                                             f"GPU_ANALYSIS_CONFIG) at one that is, and "
                                             f"restart."})
        self.server.gate_credential = (salt, GATE_KDF_ITERATIONS, digest)
        self.server.require_setup = False
        self._issue_session()

    def _reset(self) -> None:
        """Erase this machine's workbench state; Workbench.reset_all states the boundary."""
        if not _reset_origin_allowed(self.server, self.headers):
            return self._json(403, {"ok": False, "error": "reset request origin is not trusted"})
        # Keep admission closed through the gate write, not merely while the
        # worker/config reset runs. reset_all uses the same reentrant lock.
        with self.workbench.lock:
            result = self.workbench.reset_all()
            if not result.get("ok"):
                return self._json(409 if result.get("error", "").startswith("busy:") else 500, result)
            cleared = list(result.get("reset", []))
            if getattr(self.server, "token_digest", None) is not None:
                cleared.append("gate stays on: its password was given at launch (--token), "
                               "not in the browser")
            else:
                try:
                    _write_gate_record({"setup_required": True})
                except OSError as exc:
                    return self._json(500, {"ok": False, "reset": cleared,
                        "error": "workbench reset ran, but the password gate was not changed: "
                                 f"could not persist first-run setup ({type(exc).__name__})"})
                self.server.gate_credential = None
                self.server.require_setup = True
                cleared.append("gate password cleared; first-run setup is required, including after restart")
            self.server.sessions = {}
            cleared.append("sign-in sessions invalidated")
            self._json(200, {"ok": True, "reset": cleared, "gate_mode": _gate_mode(self.server)},
                       {"Set-Cookie": "dws=; Path=/; Max-Age=0; HttpOnly; SameSite=Strict"})

    def _logout(self) -> None:
        """Retire the session named by the caller's cookie and expire the cookie.

        Reachable without a session on purpose: leaving through a door that is
        already open is still leaving, and the browser ends up somewhere truthful.
        """
        try:
            jar = SimpleCookie(self.headers.get("Cookie") or "")
        except CookieError:
            jar = SimpleCookie()
        morsel = jar.get("dws")
        if morsel is not None and morsel.value:
            self.server.sessions.pop(morsel.value, None)
        self._json(200, {"ok": True},
                   {"Set-Cookie": "dws=; Path=/; Max-Age=0; HttpOnly; SameSite=Strict"})

    def _send(self, code: int, body: bytes, content: str, extra=None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Security-Policy", CSP)
        self.send_header("X-Content-Type-Options", "nosniff")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, code: int, payload, extra=None) -> None:
        self._send(code, json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8", extra)

    def _body(self) -> dict | None:
        """Return the parsed JSON object, or answer the client and return None.

        This used to be one `except: return {}`, which made four different situations look alike:
        a chunked body this HTTP/1.0 handler cannot read at all, a garbage Content-Length, JSON that
        is not an object, and JSON that does not parse. All four arrived as an empty patch, and an
        empty PATCH is a *legal* no-op -- so the server answered `ok: true` about a body it had
        never seen. Observed from the jsdom harness: a chunked PATCH {"language":"en"} came back
        successful with the language unchanged. Nothing was written; nothing was wrong on screen.
        """
        transfer = (self.headers.get("Transfer-Encoding") or "").strip().lower()
        if "chunked" in transfer:
            # Reading it would need real chunk framing; pretending the body was empty is worse,
            # because the client is handed a success it did not earn.
            self._json(400, {"error": "chunked request bodies are not accepted: this server reads "
                                      "the body by Content-Length (HTTP/1.0). Resend the JSON with "
                                      "a Content-Length header."})
            return None
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            self._json(400, {"error": f"unreadable Content-Length: "
                                      f"{self.headers.get('Content-Length')!r}"})
            return None
        if length < 0:
            self._json(400, {"error": f"negative Content-Length: {length}"})
            return None
        if length == 0:
            return {}                      # a genuinely absent body stays the legal empty patch
        try:
            raw = self.rfile.read(length).decode("utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            self._json(400, {"error": f"could not read the request body: {exc}"})
            return None
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            self._json(400, {"error": f"request body is not valid JSON: {exc}"})
            return None
        if not isinstance(parsed, dict):
            self._json(400, {"error": f"request body must be a JSON object, not "
                                      f"{type(parsed).__name__}"})
            return None
        return parsed

    # -- routes ------------------------------------------------------------------------

    def do_GET(self):                      # noqa: N802
        url = urlparse(self.path)
        # The gate's own two assets: markup aside, they contain nothing, and the
        # sign-in page could not render without them.
        if url.path in ("/login.css", "/login.js"):
            kind = "text/css; charset=utf-8" if url.path.endswith(".css") else \
                "application/javascript; charset=utf-8"
            return self._static(url.path.lstrip("/"), kind)
        if url.path == "/api/gate":
            return self._json(200, {"mode": _gate_mode(self.server),
                                    "auth_required": _gate_mode(self.server) != "open",
                                    "remote": not _is_loopback(self.client_address[0])})
        if not self._authorized():
            if url.path in ("/", "/index.html"):
                return self._static("login.html", "text/html; charset=utf-8")
            return self._json(401, {"error": "a password is configured; sign in at / first"})
        wb = self.workbench
        if url.path in ("/", "/index.html"):
            return self._static("index.html", "text/html; charset=utf-8")
        if url.path in ("/style.css", "/app.js"):
            kind = "text/css; charset=utf-8" if url.path.endswith(".css") else \
                "application/javascript; charset=utf-8"
            return self._static(url.path.lstrip("/"), kind)
        if url.path == "/api/auth":
            return self._json(200, {"authorized": True})
        if url.path == "/api/jobs":
            query = parse_qs(url.query, keep_blank_values=True)
            job_id = query.get("job", [None])[0]
            catalog = wb.job_catalog(job_id)
            if job_id is not None and catalog["job"] is None:
                return self._json(404, {"error": "job is no longer available",
                                        "instance_id": wb.instance_id})
            return self._json(200, catalog)
        if url.path == "/api/state":
            payload = wb.engine_state()
            payload["auth_required"] = _gate_mode(self.server) != "open"
            return self._json(200, payload)
        if url.path == "/api/files":
            directory = (parse_qs(url.query).get("dir") or [None])[0]
            try:
                payload = json.loads(skills.list_datasets(directory=directory))
            except Exception as exc:
                payload = {"success": False, "error": f"{type(exc).__name__}: {exc}"}
            return self._json(200 if payload.get("success") else 400, payload)
        if url.path == "/api/events":
            query = parse_qs(url.query, keep_blank_values=True)
            return self._events(query.get("job", [""])[0],
                                query.get("after", ["0"])[0], query.get("instance", [None])[0])
        if url.path == "/artifact":
            target = wb.resolve_artifact((parse_qs(url.query).get("path") or [""])[0])
            if target is None:
                return self._json(404, {"error": "not found, or outside the allowed roots"})
            mime = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
            return self._send(200, target.read_bytes(), mime)
        return self._json(404, {"error": f"no such route: {url.path}"})

    def do_POST(self):                     # noqa: N802
        wb = self.workbench
        url = urlparse(self.path)
        # The door routes are the only ones reachable without a session: /api/gate says
        # what state the door is in, /api/login opens it, /api/setup builds it.
        if url.path == "/api/gate":
            return self._json(200, {"mode": _gate_mode(self.server),
                                    "auth_required": _gate_mode(self.server) != "open",
                                    "remote": not _is_loopback(self.client_address[0])})
        if url.path == "/api/login":
            return self._login(url)
        if url.path == "/api/setup":
            return self._setup(url)
        if url.path == "/api/logout":
            return self._logout()
        if not self._authorized():
            return self._json(401, {"error": "a password is configured; sign in at / first"})
        if url.path == "/api/reset":
            return self._reset()
        if url.path == "/api/session/release":
            result = wb.release()
            return self._json(409 if result.get("error", "").startswith("busy:") else 200, result)
        if url.path == "/api/shutdown":
            self._json(202, {"ok": True})
            threading.Thread(target=self.workbench.shutdown, daemon=True).start()
            return
        if url.path == "/api/ask":
            body = self._body()
            if body is None:
                return                      # the reason is already on the wire
            text = str(body.get("text") or "").strip()
            if not text:
                return self._json(400, {"error": "text is required"})
            job, problem = wb.start_ask(text, body.get("file"))
            if job is None:
                code = 409 if problem == "busy" else 503
                return self._json(code, {"error": problem, "instance_id": wb.instance_id,
                                        "active_job_id": wb.job_catalog()["active_job_id"]})
            return self._json(202, {"job_id": job.id, "instance_id": wb.instance_id})
        if url.path == "/api/run":
            body = self._body()
            if body is None:
                return
            job, problem = wb.start_direct_run(str(body.get("tool") or ""),
                                               body.get("args") or {})
            if job is None:
                code = 409 if problem == "busy" else 400
                return self._json(code, {"error": problem, "instance_id": wb.instance_id,
                                        "active_job_id": wb.job_catalog()["active_job_id"]})
            return self._json(202, {"job_id": job.id, "instance_id": wb.instance_id,
                                    "note": "direct tool call, no model involved"})
        if url.path == "/api/models":
            if "api_key" in parse_qs(url.query):
                return self._json(400, {"error": "api_key must be sent in the request body, "
                                                 "never in the query string"})
            body = self._body()
            if body is None:
                return
            return self._json(200, wb.list_models(body))
        if url.path == "/api/diag":
            # The page measures its own layout once per load and reports here; the report is
            # geometry only, lands in diag.json beside connection.json, and exists so that
            # "the composer is not pinned" can be answered with numbers from the browser that
            # actually rendered it, instead of with theories about browsers nobody measured.
            body = self._body()
            if body is None:
                return
            if not isinstance(body, dict) or len(json.dumps(body)) > 4000:
                return self._json(400, {"error": "the layout report must be a small JSON object"})
            body["reported_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
            try:
                diag_file().write_text(json.dumps(body, ensure_ascii=False, indent=2),
                                       encoding="utf-8")
            except OSError as exc:
                return self._json(500, {"error": f"could not write the layout report: {exc}"})
            return self._json(200, {"ok": True})
        return self._json(404, {"error": f"no such route: {url.path}"})

    def do_PATCH(self):                    # noqa: N802
        url = urlparse(self.path)
        if not self._authorized():
            return self._json(401, {"error": "a password is configured; sign in at / first"})
        if url.path != "/api/settings":
            return self._json(404, {"error": f"no such route: {url.path}"})
        # A credential in a query string lands in access logs, proxy logs and browser history.
        # It is only ever accepted in the request body.
        if "api_key" in parse_qs(url.query):
            return self._json(400, {"error": "api_key must be sent in the request body, "
                                             "never in the query string"})
        patch = self._body()
        if patch is None:
            return
        result = self.workbench.apply_settings(patch)
        return self._json(409 if result.get("error", "").startswith("busy:") else 200, result)

    def _static(self, name: str, content: str) -> None:
        root = GUI_DIR.resolve()
        path = (GUI_DIR / name).resolve()
        if not path.is_relative_to(root) or not path.is_file():
            return self._json(404, {"error": f"missing frontend file: {name}"})
        body = path.read_bytes()
        if b"{{BUILD}}" in body:
            # The pages carry one build stamp, filled from one constant, so a page can never
            # claim to be a release it is not -- and a stale tab is identifiable at a glance.
            body = body.replace(b"{{BUILD}}", BUILD_TAG.encode("utf-8"))
        self._send(200, body, content)

    def _events(self, job_id: str, after: str = "0", instance: str | None = None) -> None:
        with self.workbench.lock:
            if instance is not None and instance != self.workbench.instance_id:
                return self._json(409, {"error": "backend instance changed; reload task history"})
            job = self.workbench.jobs.get(job_id)
        if job is None:
            return self._json(404, {"error": f"no such job: {job_id}"})
        raw_cursor = self.headers.get("Last-Event-ID", after)
        if not raw_cursor.isascii() or not raw_cursor.isdecimal() or len(raw_cursor) > 20:
            return self._json(400, {"error": "event cursor must be a nonnegative integer"})
        cursor = int(raw_cursor)
        if cursor > job.sequence:
            return self._json(400, {"error": "event cursor is ahead of this job"})
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        try:
            while True:
                fresh, finished = job.since(cursor)
                if fresh and fresh[0][0] > cursor + 1:
                    notice = {"first_sequence": fresh[0][0], "requested_after": cursor}
                    self.wfile.write(("event: replay_gap\ndata: " + json.dumps(notice) + "\n\n").encode())
                for sequence, name, payload in fresh:
                    cursor = sequence
                    frame = f"id: {sequence}\nevent: {name}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
                    self.wfile.write(frame.encode("utf-8"))
                    self.wfile.flush()
                if not fresh:
                    self.wfile.write(b": keep-alive\n\n")
                    self.wfile.flush()
                if finished and cursor >= job.sequence:
                    break
        except (BrokenPipeError, ConnectionResetError, OSError):
            # A closed viewer must never cancel the run it was watching.
            return


def _shutdown(wb: Workbench) -> None:
    """Release whatever the worker still holds before the process goes.

    The GUI can leave a session open between questions on purpose -- that is where the resident
    speedup comes from -- so this is the backstop for an operator who closes the tab instead of
    pressing release. skills already registers an atexit hook for the worker process itself.
    """
    try:
        skills.release_all_sessions_and_cache()
    except Exception:
        pass


def make_server(port: int = DEFAULT_PORT, host: str = "127.0.0.1",
                model: str | None = None, token: str | None = None):
    """Build the server and its workbench without entering the loop.

    Split out so the headless tests can bind port 0, drive real HTTP against a real Workbench
    and real skills, and shut it down -- rather than testing a mock of the thing under test.
    A token, when given, is stored only as its sha256 digest, on the server instance.
    """
    workbench = Workbench(model_override=model)
    # Separate registries even when several backends share one Python process.
    bound_handler = type("WorkbenchHandler", (Handler,), {"workbench": workbench})
    httpd = ThreadingHTTPServer((host, port), bound_handler)
    httpd.daemon_threads = True
    # Two credential sources: a launch-time --token (sha256, in memory only) and the
    # browser-set gate.json password (salted PBKDF2, on disk). Either arms the gate;
    # neither is ever stored in plaintext, and both live per-server-instance so the
    # headless tests can run gated and ungated servers side by side.
    httpd.token_digest = hashlib.sha256(token.encode("utf-8")).digest() if token else None
    httpd.gate_credential = load_gate_credential() if token is None else None
    httpd.require_setup = load_gate_setup_required() if token is None else False
    httpd.sessions = {}                # sid -> expiry; survives exactly as long as the process
    workbench.httpd = httpd
    return httpd, workbench


def serve(port: int = DEFAULT_PORT, host: str = "127.0.0.1",
          model: str | None = None, token: str | None = None) -> int:
    remote = host != "127.0.0.1"
    if remote and os.environ.get("GPU_GUI_ALLOW_REMOTE") != "1" and token is None \
            and load_gate_credential() is None:
        # No refusal: with no password anywhere, a remote bind arms first-run setup
        # instead of exposing anything. Unauthenticated visitors get a waiting page,
        # and only a loopback client can claim the gate. GPU_GUI_ALLOW_REMOTE=1 plus
        # no password remains the explicit opt-in to a genuinely open interface.
        print(f"binding {host} with no password set: first-run setup mode. The first visit "
              f"from the server's own machine sets the access password; every other "
              f"visitor sees a waiting page until then. (Set --token, or "
              f"GPU_GUI_ALLOW_REMOTE=1 for no gate at all.)")
    try:
        httpd, state = make_server(port, host, model, token)
    except OSError as exc:
        print(f"cannot bind {host}:{port}: {exc}\n"
              f"The workbench is optional; nothing else was affected. "
              f"Try --port with a free port.")
        return 2
    status = state.engine_state()
    if Agent is None:
        model_state = f"unavailable -- {AGENT_IMPORT_ERROR}"
    elif state.config_ready:
        model_state = f"configured ({state.model})"
    else:
        model_state = "installed but no base URL / key / model configured"
    print(f"GPU加速与数据分析 · workbench on http://{host}:{port}")
    mode = _gate_mode(httpd)
    if mode == "signin":
        print("  access: password required -- the sign-in page comes up once per browser; "
              "the session cookie lasts 7 days or until this process restarts")
    elif mode == "setup":
        print("  access: first-run setup -- the first loopback visit sets the password; "
              "remote visitors wait")
    elif remote:
        print("  access: NO password and GPU_GUI_ALLOW_REMOTE=1 is on -- anyone who can "
              "reach this address can read files through /artifact. Bind a password "
              "before leaving a trusted network.")
    print(f"  engine: {'ready' if status['engine_ready'] else 'not reachable: ' + status['engine_error']}")
    print(f"  model client: {model_state}")
    if not _probe_config_dir():
        print(f"  config dir NOT writable: {gate_file().parent}")
        print("  a browser-set gate password and saved settings will fail until it is; "
              "set XDG_CONFIG_HOME to a writable directory and restart")
    print("  Ctrl+C to stop; any resident session is released on the way out.")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopping...")
    finally:
        httpd.shutdown()
        _shutdown(state)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Localhost workbench for the analysis agent")
    ap.add_argument("--port", type=int, default=int(os.environ.get("GPU_GUI_PORT", DEFAULT_PORT)))
    ap.add_argument("--host", default="127.0.0.1",
                    help="loopback by default; a non-loopback host arms first-run setup when "
                         "no password is set, so nothing unauthenticated is ever served")
    ap.add_argument("--token", default=os.environ.get("GPU_GUI_TOKEN"),
                    help="set the gate password here instead of the browser's first-run "
                         "setup (sha256, in memory only, never written to disk). "
                         "GPU_GUI_TOKEN is the same thing as an environment variable, and "
                         "keeps the secret off the process command line.")
    ap.add_argument("--reset-token", action="store_true",
                    help="clear the stored gate password before serving; the next loopback "
                         "visit offers first-run setup again")
    ap.add_argument("--model", default=None, help="override the configured model ID for this run")
    args = ap.parse_args()
    if args.reset_token:
        try:
            gate_file().unlink(missing_ok=True)
            print("gate password cleared; the next loopback visit sets a new one")
        except OSError as exc:
            print(f"could not remove {gate_file()}: {exc}")
            return 2
    return serve(args.port, args.host, args.model, args.token)


if __name__ == "__main__":
    raise SystemExit(main())
