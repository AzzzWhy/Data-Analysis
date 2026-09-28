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
import inspect
import json
import mimetypes
import os
import sys
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import skills  # noqa: E402

# The model layer is a separate dependency from the analysis layer. Importing it must not be a
# condition for serving a page: on a machine that has not installed requirements.txt the workbench
# still boots, still lists data, still runs tools directly, and says plainly why the question box
# is disabled -- quoting the real ImportError rather than a canned message.
AGENT_IMPORT_ERROR = ""
try:
    from agent_main import Agent, build_client  # noqa: E402
    from api_config import load_config, normalize_url, save_config  # noqa: E402
except Exception as _exc:  # pragma: no cover - depends on the operator's environment
    Agent = None
    build_client = None
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
# How many finished runs stay replayable. Each one holds its whole transcript and result payload,
# and the dict was never trimmed: a workbench left open for a day accumulated one per question.
JOB_HISTORY = 20
# The worker's own read has no deadline: skills._worker_call accepts a `timeout` argument and
# then never uses it, so a wedged worker blocks the readline forever while holding the module
# lock. Every call the workbench makes therefore needs a deadline it owns itself.
WORKER_TIMEOUT_SECONDS = 20.0
CSP = ("default-src 'self'; style-src 'self'; script-src 'self'; "
       "connect-src 'self'; img-src 'self' data:")

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
        self.id = f"j{int(time.time() * 1000):x}-{threading.get_ident():x}"
        self.kind = kind            # "ask" (model-driven) or "run" (direct tool call)
        self.label = label
        self.events: deque = deque(maxlen=EVENT_BUFFER)
        self.sequence = 0
        self.rounds = 0
        self.done = False
        self.condition = threading.Condition()
        self.started = time.perf_counter()

    def emit(self, name: str, payload) -> None:
        with self.condition:
            self.sequence += 1
            self.events.append((self.sequence, name, payload))

    def finish(self) -> None:
        with self.condition:
            self.done = True
            self.condition.notify_all()

    def since(self, cursor: int):
        """(events after cursor, finished). Blocks briefly instead of busy-polling."""
        with self.condition:
            newest = self.events[-1][0] if self.events else 0
            if newest <= cursor and not self.done:
                self.condition.wait(1.0)
            return [e for e in self.events if e[0] > cursor], self.done


class Workbench:
    """Holds the single agent, the single session worker behind it, and the job registry.

    One agent and one lock are not a simplification: `skills._worker_call` holds a module-level
    lock for a whole request/response round trip against one long-lived worker process, so
    concurrent analyses were never on the table. A second request is refused with 409 rather
    than queued, because a queue would look like a hang from the browser.
    """

    def __init__(self, model_override: str | None = None):
        self.lock = threading.Lock()
        self.busy = False
        self.jobs: dict[str, Job] = {}
        self.agent = None
        self.config_ready = False
        self.missing_fields: list[str] = []      # filled once a config file could be read
        self.remember_key = False
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

    def _configure(self, model_override: str | None) -> None:
        if Agent is None or load_config is None:
            return
        try:
            config = load_config()
        except Exception as exc:
            self.last_error = f"connection settings unreadable: {exc}"
            return
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
                self.agent = Agent(build_client(config), verbose=False, model=config.model)
            except Exception as exc:
                self.last_error = f"model client could not start: {type(exc).__name__}: {exc}"

    def apply_settings(self, patch: dict) -> dict:
        """Write connection settings through api_config, then rebuild the agent.

        The key is accepted once and never returned. The response reports whether one is stored,
        as a boolean -- a server that echoes a credential can end up with that credential in a
        browser history entry, a screenshot or a log file, and this screen is designed to be put
        on camera.
        """
        if load_config is None or save_config is None:
            return {"ok": False, "error": f"settings layer unavailable: {AGENT_IMPORT_ERROR}"}
        try:
            config = load_config()
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
        if patch.get("api_key") and not config.remember_key:
            # The workbench rebuilds its client from disk (`_configure` calls `load_config`), so a
            # key that is not persisted cannot be used here at all -- say that, rather than
            # implying a memory-only session the GUI does not have. And do not let this submission
            # downgrade remember_key either: `save_config` writes the key only while the flag is
            # true (api_config.py:104), so persisting the flag as false would rewrite
            # connection.json with no key in it and delete a credential that was already working.
            warning = ("api_key was discarded: without 记住密钥 / Remember key nothing is written, "
                       "and the workbench reads its credential from disk, so an unpersisted key "
                       "cannot be used here. To replace the stored key, submit again with Remember "
                       "key ticked; to stop keeping the stored plaintext key, change that checkbox "
                       "on its own.")
            if config.base_url == was[0]:
                config.remember_key = was[4]
                config.api_key = was[2]
        elif patch.get("api_key"):
            config.api_key = str(patch["api_key"])
        elif was[4] and not config.remember_key:
            # Turning the checkbox off really does delete the stored plaintext key, which is the
            # safe reading of "don't remember" but must not surprise the operator at the next ask.
            warning = ("remember_key off removes the stored plaintext key from connection.json: "
                       "the question box closes until a key is saved again or GPU_API_KEY was "
                       "exported before this server started.")

        try:
            if (config.base_url, config.model, config.api_key, config.skip_setup,
                    config.remember_key, config.language) != was:
                # A no-op settings submit used to rewrite the operator's connection.json (its mtime
                # moved even when every field was identical), and a refused key rewrote it too.
                save_config(config)
        except Exception as exc:
            # save_config validates the language and refuses anything but zh/en.
            return {"ok": False, "error": f"settings not saved: {type(exc).__name__}: {exc}"}

        self._configure(self.model_override)
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
        """Close sessions AND drop warm frames, off-thread, with our own deadline.

        skills.release_all_sessions_and_cache() already re-reads the after-counts; what it cannot
        do is time out, because the underlying worker read has no deadline. A worker that never
        answers still holds the memory, so this reports the wedge instead of showing "released".
        """
        result = call_with_timeout(skills.release_all_sessions_and_cache, timeout)
        if not result.get("error"):
            self.busy = False
            result["state"] = self.session_doc(self.session_status())
        return result

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
        try:
            fn = getattr(skills, tool)
            accepted = inspect.signature(fn).parameters
            clean = {k: v for k, v in (args or {}).items() if k in accepted}
            dropped = sorted(set((args or {}).keys()) - set(clean))
            job.emit("phase", {"phase": "tool"})
            job.rounds += 1
            job.emit("tool_call", {"round": job.rounds, "name": tool, "args": clean,
                                   "dropped_args": dropped, "source": "direct"})
            started = time.perf_counter()
            raw = fn(**clean)
            seconds = time.perf_counter() - started
            try:
                parsed = json.loads(raw)
            except (TypeError, json.JSONDecodeError):
                parsed = {"success": False, "error": "tool returned a non-JSON payload"}
            job.emit("tool_result", {"round": job.rounds, "name": tool, "args": clean,
                                     "result": parsed, "chip": chip_state(parsed),
                                     "seconds": round(seconds, 3)})
            self._absorb_result(parsed)
            job.emit("session", self.session_event())
            job.emit("done", {"seconds": round(seconds, 3), "source": "direct"})
        except Exception as exc:
            job.emit("error", {"message": f"{type(exc).__name__}: {exc}"})
            job.emit("done", {"seconds": 0.0})
        finally:
            job.finish()
            self.busy = False

    def start_ask(self, text: str, file: str | None = None):
        """Begin a model-driven turn. `file`, when given, is stated to the model as context.

        The path is appended as its own labelled line rather than being woven into the question,
        so what the model actually received can be shown verbatim in the log and nothing is
        silently rewritten.
        """
        if Agent is None:
            return None, (f"model client is not available: {AGENT_IMPORT_ERROR}. "
                          f"Install it with: pip install -r requirements.txt")
        if self.agent is None:
            return None, (self.last_error or "no API base URL, key and model are configured. "
                                             "Set them with agent_main.py --configure or the "
                                             "GPU_API_* environment variables.")
        prompt = text
        if file:
            resolved = file
            prompt = (f"{text}\n\n"
                      f"[workbench] the dataset selected in the interface is: {resolved}")
        job = self._acquire("ask", prompt[:120])
        if job is None:
            return None, "busy"
        job.emit("prompt", {"text": prompt, "file": file or ""})
        thread = threading.Thread(target=self._run_ask, args=(job, prompt), daemon=True)
        thread.start()
        return job, ""

    def _run_ask(self, job: Job, text: str) -> None:
        """Drive one real Agent turn, streaming text to `event` and tool payloads to `result`."""
        started = time.perf_counter()
        try:
            self.agent.event_sink = lambda line: job.emit("trace", {"line": line})
            self.agent.result_sink = self._make_result_sink(job)
            job.emit("phase", {"phase": "thinking"})
            answer = self.agent.run(text)
            job.emit("answer", {"text": answer})
            job.emit("session", self.session_event())
            job.emit("done", {"seconds": round(time.perf_counter() - started, 3)})
        except Exception as exc:
            # safe_error strips the API key out of the message; an exception text that echoes a
            # request header must never reach the browser or the event log.
            job.emit("error", {"message": self.agent.safe_error(exc)})
            job.emit("done", {"seconds": round(time.perf_counter() - started, 3)})
        finally:
            self.agent.event_sink = None
            self.agent.result_sink = None
            job.finish()
            self.busy = False

    def _make_result_sink(self, job: Job):
        def sink(name: str, payload: dict, seconds: float) -> None:
            job.rounds += 1
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

    def _json(self, code: int, payload) -> None:
        self._send(code, json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

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
        wb = self.workbench
        if url.path in ("/", "/index.html"):
            return self._static("index.html", "text/html; charset=utf-8")
        if url.path in ("/style.css", "/app.js"):
            kind = "text/css; charset=utf-8" if url.path.endswith(".css") else \
                "application/javascript; charset=utf-8"
            return self._static(url.path.lstrip("/"), kind)
        if url.path == "/api/state":
            return self._json(200, wb.engine_state())
        if url.path == "/api/files":
            directory = (parse_qs(url.query).get("dir") or [None])[0]
            try:
                payload = json.loads(skills.list_datasets(directory=directory))
            except Exception as exc:
                payload = {"success": False, "error": f"{type(exc).__name__}: {exc}"}
            return self._json(200 if payload.get("success") else 400, payload)
        if url.path == "/api/events":
            return self._events(parse_qs(url.query).get("job", [""])[0])
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
        if url.path == "/api/session/release":
            return self._json(200, wb.release())
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
                return self._json(code, {"error": problem})
            return self._json(202, {"job_id": job.id})
        if url.path == "/api/run":
            body = self._body()
            if body is None:
                return
            job, problem = wb.start_direct_run(str(body.get("tool") or ""),
                                               body.get("args") or {})
            if job is None:
                code = 409 if problem == "busy" else 400
                return self._json(code, {"error": problem})
            return self._json(202, {"job_id": job.id,
                                    "note": "direct tool call, no model involved"})
        return self._json(404, {"error": f"no such route: {url.path}"})

    def do_PATCH(self):                    # noqa: N802
        url = urlparse(self.path)
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
        return self._json(200, self.workbench.apply_settings(patch))

    def _static(self, name: str, content: str) -> None:
        root = GUI_DIR.resolve()
        path = (GUI_DIR / name).resolve()
        if not path.is_relative_to(root) or not path.is_file():
            return self._json(404, {"error": f"missing frontend file: {name}"})
        self._send(200, path.read_bytes(), content)

    def _events(self, job_id: str) -> None:
        job = self.workbench.jobs.get(job_id)
        if job is None:
            return self._json(404, {"error": f"no such job: {job_id}"})
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        cursor = 0
        try:
            while True:
                fresh, finished = job.since(cursor)
                for sequence, name, payload in fresh:
                    cursor = sequence
                    frame = f"event: {name}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
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
                model: str | None = None):
    """Build the server and its workbench without entering the loop.

    Split out so the headless tests can bind port 0, drive real HTTP against a real Workbench
    and real skills, and shut it down -- rather than testing a mock of the thing under test.
    """
    workbench = Workbench(model_override=model)
    Handler.workbench = workbench
    httpd = ThreadingHTTPServer((host, port), Handler)
    httpd.daemon_threads = True
    workbench.httpd = httpd
    return httpd, workbench


def serve(port: int = DEFAULT_PORT, host: str = "127.0.0.1",
          model: str | None = None) -> int:
    if host != "127.0.0.1" and os.environ.get("GPU_GUI_ALLOW_REMOTE") != "1":
        print(f"refusing to bind {host}: the workbench has no authentication, and 8888/9000 on "
              f"the Spark node are public ports reachable by anyone who knows the address.\n"
              f"Tunnel instead, which keeps the service on loopback:  "
              f"ssh -p <port> -L {port}:127.0.0.1:{port} Developer@<jump-host>\n"
              f"If you genuinely want an open interface, set GPU_GUI_ALLOW_REMOTE=1.")
        return 2
    try:
        httpd, state = make_server(port, host, model)
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
    print(f"  engine: {'ready' if status['engine_ready'] else 'not reachable: ' + status['engine_error']}")
    print(f"  model client: {model_state}")
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
                    help="loopback only unless GPU_GUI_ALLOW_REMOTE=1")
    ap.add_argument("--model", default=None, help="override the configured model ID for this run")
    args = ap.parse_args()
    return serve(args.port, args.host, args.model)


if __name__ == "__main__":
    raise SystemExit(main())
