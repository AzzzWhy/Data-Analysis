#!/usr/bin/env python3
"""
Long-lived session worker: load a dataset into GPU memory ONCE, then run any number of
analyses against that resident copy.

Why this exists
---------------
The stateless path re-reads and re-parses the file on every call. Measured on a 20,000,000
row, 3.0 GB CSV on GB10:

    load once                    1.29 s
    each subsequent operation   0.015 - 0.146 s
    5-step full-data analysis    1.54 s total

    the same 5 steps, re-reading every time
        GPU                      10.8 s
        CPU pandas               49.7 s

So session reuse does not make a single query a little faster; it makes a multi-step
workflow roughly an order of magnitude faster than that. That is what makes deliberate
drill-down affordable, and it is the reason this skill exists.

Two properties the design protects
----------------------------------
1. **Full data, every step.** The frame is already resident, so there is no reason to
   subsample for speed. Every operation scans all rows. `rows_scanned` is reported per step
   so the claim is checkable rather than asserted.
2. **No stale answers.** The handle records the file's identity (path, size, mtime) at open
   time and re-checks it on every operation. If the file changed underneath, the worker
   refuses to answer instead of silently computing on the old snapshot.

Protocol
--------
Newline-delimited JSON on stdin, one response per request on stdout. Requests:

    {"cmd": "open",    "path": "...", "usecols": null, "force_cpu": false}
    {"cmd": "analyze", "sid": "s1", "op": "groupby", "by": "region", "agg": "revenue:sum"}
    {"cmd": "list"}
    {"cmd": "close",   "sid": "s1"}
    {"cmd": "ping"}

Never raises: every failure is returned as {"ok": false, "error": ...} so the caller can
degrade to the stateless path.

Run standalone for a manual check:
    echo '{"cmd":"ping"}' | python gpu_session.py
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

# Reuse the verified engine rather than reimplementing any statistics. The operation
# functions, the engine probe and the fallback logic all come from gpu_analytics.
_HERE = os.path.dirname(os.path.abspath(__file__))


def _import_engine():
    if _HERE not in sys.path:
        sys.path.insert(0, _HERE)
    import gpu_analytics as ga  # noqa: E402
    return ga


def _import_plans():
    if _HERE not in sys.path:
        sys.path.insert(0, _HERE)
    import analysis_plan as ap  # noqa: E402
    return ap


GA = _import_engine()
PLANS_MODULE = _import_plans()
from statistics_cache import ExactStatisticsCache

# Sessions hold a full dataset in GPU memory, so they are capped. Each open session on a
# 20M-row file costs roughly 1-2 GB of device memory; refusing the 5th is better than
# letting an agent work the box into an OOM it cannot diagnose.
MAX_SESSIONS = int(os.environ.get("SESSION_MAX", "4"))
WARM_CACHE_MB = max(0, int(os.environ.get("SESSION_WARM_CACHE_MB", "4096")))
WARM_TTL_SECONDS = max(0, int(os.environ.get("SESSION_WARM_TTL_SECONDS", "900")))
STATISTICS_CACHE_COLUMNS = max(0, int(os.environ.get("SESSION_STATISTICS_CACHE_COLUMNS", "128")))

# Refuse an open when the device would be left too tight to finish the work.
# The multiplier covers the parsed frame plus transient intermediates and result copies.
MEM_HEADROOM = float(os.environ.get("SESSION_MEM_HEADROOM", "3.0"))
MEM_FLOOR_GB = float(os.environ.get("SESSION_MEM_FLOOR_GB", "4.0"))


def _free_gpu_gb() -> Optional[float]:
    """Free device memory in GB, or None when it cannot be determined (CPU-only run)."""
    try:
        import cupy  # type: ignore

        free, _total = cupy.cuda.runtime.memGetInfo()
        return free / (1024 ** 3)
    except Exception:
        return None


def _file_identity(path: str) -> list:
    """Identity of a file for staleness detection: absolute path, size, modification time.

    Nanosecond mtime, not `int(st_mtime)`. Truncating to whole seconds made the guard fail
    open on a real scenario: the guard compares the identity recorded at open time against the
    current one, so a file touched within the same second as the load compared equal and the
    session happily answered from a stale snapshot. That is exactly the silent wrong answer the
    guard exists to prevent. `st_mtime_ns` is available in Python 3.3+, and where a filesystem
    has coarser granularity the size component still catches most real edits.
    """
    st = os.stat(path)
    mtime_ns = getattr(st, "st_mtime_ns", None)
    return [os.path.abspath(path), st.st_size,
            int(mtime_ns) if mtime_ns is not None else int(st.st_mtime * 1e9)]


@dataclass
class Session:
    sid: str
    path: str
    engine: Any
    frame: Any
    rows: int
    identity: list
    load_seconds: float
    opened_at: float = field(default_factory=time.perf_counter)
    steps: int = 0
    analysis_seconds: float = 0.0
    cpu_load_seconds: Optional[float] = None
    reason: Optional[str] = None
    usecols: Optional[tuple] = None
    # The strategy this session is following, if a goal was supplied at open time. Held on
    # the session so progress survives across analyze calls without the model tracking it.
    plan: Any = None
    # Only scalar quartiles, bounded per session; never retain masks or result frames.
    # Engine is part of the key so a pandas fallback cannot inherit cuDF quartiles.
    statistics: ExactStatisticsCache = field(default_factory=ExactStatisticsCache)
    loading: dict = field(default_factory=dict)


SESSIONS: Dict[str, Session] = {}
WARM_CACHE: Dict[tuple, Session] = {}
_COUNTER = {"n": 0}


def _cache_key(path: str, usecols: Optional[tuple]) -> tuple:
    return path, usecols


def _frame_bytes(sess: Session) -> int:
    try:
        return int(sess.frame.memory_usage(deep=True).sum())
    except Exception:
        return 0


def _release_unused_gpu_blocks() -> None:
    if GA._ENGINE is None or not GA._ENGINE.is_gpu:
        return  # a CPU-only request must not initialize CUDA just for cleanup
    try:
        import cupy  # type: ignore
        cupy.get_default_memory_pool().free_all_blocks()
    except Exception:
        pass


def _prune_cache() -> None:
    now = time.monotonic()
    evicted = False
    for key, sess in list(WARM_CACHE.items()):
        try:
            valid = _file_identity(sess.path) == sess.identity
        except OSError:
            valid = False
        if not valid or now - sess.opened_at > WARM_TTL_SECONDS:
            WARM_CACHE.pop(key, None)
            evicted = True
    budget = WARM_CACHE_MB * 1024 * 1024
    while WARM_CACHE and sum(_frame_bytes(s) for s in WARM_CACHE.values()) > budget:
        oldest = min(WARM_CACHE, key=lambda k: WARM_CACHE[k].opened_at)
        WARM_CACHE.pop(oldest, None)
        evicted = True
    if evicted:
        sess = None
        _release_unused_gpu_blocks()


def _retain(sess: Session) -> bool:
    """Retain only bounded GPU frames; never retain a plan or an active session handle."""
    if not sess.engine.is_gpu or not WARM_CACHE_MB or not WARM_TTL_SECONDS:
        return False
    size = _frame_bytes(sess)
    if not size or size > WARM_CACHE_MB * 1024 * 1024:
        return False
    sess.plan = None
    sess.opened_at = time.monotonic()
    WARM_CACHE[_cache_key(sess.path, sess.usecols)] = sess
    _prune_cache()
    return _cache_key(sess.path, sess.usecols) in WARM_CACHE


def _default_args() -> argparse.Namespace:
    """
    A namespace carrying every default the operation functions expect.

    Taken from the engine's own parser so the two can never drift apart: a new option in
    gpu_analytics.py needs no change here.
    """
    return GA.build_parser().parse_args(["--input", "/dev/null"])


def _build_args(req: dict) -> argparse.Namespace:
    args = _default_args()
    # `columns` in the tool schema maps onto the engine's `--columns`/args.select. Keeping
    # this translation in one place is what lets the schema stay natural for the model.
    mapping = {
        "op": "op",
        "columns": "select",
        "by": "by",
        "agg": "agg",
        "top_k": "top_k",
        "method": "method",
        "iqr_k": "iqr_k",
        "preview_n": "preview_n",
        "auto_columns": "auto_columns",
        "max_columns": "max_columns",
    }
    for key, attr in mapping.items():
        if req.get(key) not in (None, ""):
            setattr(args, attr, req[key])
    try:
        if args.top_k is not None:
            args.top_k = int(args.top_k)
    except (TypeError, ValueError):
        return args
    return args


def _err(msg: str, **extra) -> dict:
    return {"ok": False, "error": msg, **extra}


def do_open(req: dict) -> dict:
    request_started = time.perf_counter()
    path = req.get("path")
    if not path or not str(path).strip():
        return _err("path is required")
    path = os.path.abspath(os.path.expanduser(str(path)))
    if not os.path.isfile(path):
        return _err(
            f"file not found: {path}",
            hint=("Relative paths resolve against the working directory of the agent process. "
                  "Call list_datasets, take the absolute path and retry; if you only need a "
                  "single analysis, analyze_dataset is the other option."),
        )
    if len(SESSIONS) >= MAX_SESSIONS:
        return _err(
            f"session limit reached, {MAX_SESSIONS} are already open. Close one first.",
            open_sessions=sorted(SESSIONS),
        )

    usecols = req.get("usecols")
    cols = tuple(c.strip() for c in str(usecols).split(",") if c.strip()) if usecols else None
    requested_backend = req.get("load_backend", req.get("_batch_load_backend", "native"))
    if requested_backend not in {"auto", "native", "cpu_gpu"}:
        return _err("load_backend must be auto, native or cpu_gpu")
    requested_force_cpu = bool(req.get("force_cpu"))
    requested_force_gpu = bool(req.get("force_gpu"))
    if requested_force_cpu and requested_force_gpu:
        return _err("force_cpu and force_gpu are mutually exclusive")
    if requested_force_cpu and requested_backend == "cpu_gpu":
        return _err("force_cpu conflicts with CPU-to-GPU loading")
    # Adaptive conversation steps are unknown at open time. An uncalibrated session
    # keeps the native reader; a planned batch has an exact-workflow calibration.
    load_backend = "native" if requested_backend == "auto" else requested_backend
    eligibility = GA.hybrid_execution.preflight(str(path), cols, [], load_backend)
    if eligibility and requested_backend == "cpu_gpu":
        return _err("CPU-to-GPU loading unavailable: " + eligibility)
    if eligibility and not req.get("force_cpu"):
        req = {**req, "force_cpu": True, "force_gpu": False}
    _prune_cache()

    # Reuse an existing session for the same file instead of loading the dataset again.
    #
    # Why: a caller that loses track of the session id it was handed opens a second session
    # on the same file. Measured on 20M rows, that doubled the resident footprint (~3.4 GB)
    # and left the first session stranded until a cleanup backstop ran -- real device memory
    # held for no benefit. Returning the existing handle costs nothing and cannot be worse
    # than a second full load, so it is the right default rather than an optimisation.
    invalidated = False
    for existing in list(SESSIONS.values()):
        if existing.path != path:
            continue
        if _file_identity(path) != existing.identity:
            SESSIONS.pop(existing.sid, None)
            invalidated = True
            continue
        if existing.usecols == cols:
            if ((req.get("force_cpu") and existing.engine.is_gpu) or
                    (req.get("force_gpu") and not existing.engine.is_gpu) or
                    (requested_backend == "cpu_gpu" and
                     existing.loading.get("actual") != "cpu_gpu") or
                    (requested_backend == "native" and
                     existing.loading.get("actual") == "cpu_gpu")):
                return _err("file already has an active session with a different engine or "
                            "load backend; close it before changing the route",
                            session_id=existing.sid)
            return {
                "ok": True,
                "session_id": existing.sid,
                "file": os.path.basename(path),
                "rows": existing.rows,
                "columns": [str(c) for c in existing.frame.columns],
                "engine": existing.engine.name,
                "gpu": existing.engine.gpu_name,
                "accelerated": existing.engine.is_gpu,
                "load_seconds": round(existing.load_seconds, 3),
                "loading": {"requested": requested_backend,
                            "actual": existing.loading.get("actual", "native_gpu" if
                                       existing.engine.is_gpu else "cpu"),
                            "read_count": 0, "conversion_count": 0,
                            "session_reuse": True},
                "reused_existing_session": True,
                "already_loaded": True,
                "execution_decision": GA.execution_decision_record(
                    mode="reuse", policy="resident_reuse",
                    selected_backend=existing.engine.name,
                    actual_backend=existing.engine.name,
                    reason="reused the existing resident frame; no file was reloaded",
                    signals={"operation": "open", "file_size_bytes": os.path.getsize(path),
                             "session_id": existing.sid},
                    observed={"phase": "session_reuse", "elapsed_seconds": round(
                        time.perf_counter() - request_started, 6), "rows_scanned": 0},
                ),
                "note": (
                    f"This file is already loaded, so session {existing.sid} is reused and no "
                    "second copy took device memory. **Use session_id="
                    f"{existing.sid}** for every later call, and release it with close."
                ),
            }
    existing = None
    if invalidated:
        _release_unused_gpu_blocks()

    cached = None if req.get("_fresh_batch") else WARM_CACHE.pop(_cache_key(path, cols), None)
    cache_backend_matches = (requested_backend == "auto" or cached is not None and
                             ((load_backend == "cpu_gpu" and
                               cached.loading.get("actual") == "cpu_gpu") or
                              (load_backend == "native" and
                               cached.loading.get("actual") != "cpu_gpu")))
    if cached is not None and not req.get("force_cpu") and cache_backend_matches:
        _COUNTER["n"] += 1
        cached.sid = f"s{_COUNTER['n']}"
        cached.opened_at = time.perf_counter()
        cached.steps = 0
        cached.analysis_seconds = 0.0
        SESSIONS[cached.sid] = cached
        out = {
            "ok": True, "session_id": cached.sid, "file": os.path.basename(path),
            "rows": cached.rows, "columns": [str(c) for c in cached.frame.columns],
            "engine": cached.engine.name, "gpu": cached.engine.gpu_name,
            "accelerated": cached.engine.is_gpu, "load_seconds": 0.0,
            "loading": {"requested": requested_backend,
                        "actual": cached.loading.get("actual", "native_gpu"),
                        "read_count": 0, "conversion_count": 0,
                        "cache_reuse": True},
            "cache_hit": True, "reused_existing_session": False,
            "already_loaded": True, "resident_mb": round(_frame_bytes(cached) / 1024**2, 1),
            "execution_decision": GA.execution_decision_record(
                mode="reuse", policy="warm_cache", selected_backend=cached.engine.name,
                actual_backend=cached.engine.name,
                reason="reused a bounded, file-validated GPU frame from a previous question",
                signals={"operation": "open", "file_size_bytes": cached.identity[1],
                         "session_id": cached.sid},
                observed={"phase": "warm_cache_hit", "elapsed_seconds": round(
                    time.perf_counter() - request_started, 6), "rows_scanned": 0},
            ),
        }
        if req.get("goal") or req.get("plan"):
            cached.plan = PLANS_MODULE.build_plan(
                plan_id=f"p{cached.sid}", goal=str(req.get("goal") or ""),
                kind=req.get("plan_kind"), group_cols=_pick_group_columns(cached.frame),
            )
            out["plan"] = cached.plan.as_dict()
        return out
    if cached is not None:
        WARM_CACHE[_cache_key(path, cols)] = cached

    # Refuse before loading rather than after: once the parse starts, a failure surfaces as
    # an opaque OOM in the middle of the read.
    size_gb = os.path.getsize(path) / (1024 ** 3)
    free_gb = None if req.get("force_cpu") else _free_gpu_gb()
    need_gb = size_gb * MEM_HEADROOM + MEM_FLOOR_GB
    while free_gb is not None and free_gb < need_gb and WARM_CACHE:
        oldest = min(WARM_CACHE, key=lambda k: WARM_CACHE[k].opened_at)
        WARM_CACHE.pop(oldest, None)
        _release_unused_gpu_blocks()
        free_gb = _free_gpu_gb()
    if free_gb is not None and free_gb < need_gb:
        return _err(
            f"Not enough free device memory, load refused: the file is about {size_gb:.2f}GB, "
            f"which needs roughly {need_gb:.1f}GB free and only {free_gb:.1f}GB is available. "
            f"Use analyze_dataset for a single analysis, or pass columns to read only the "
            "columns you need.",
            file_gb=round(size_gb, 2), free_gb=round(free_gb, 1), need_gb=round(need_gb, 1),
        )

    force_cpu = bool(req.get("force_cpu"))
    force_gpu = bool(req.get("force_gpu")) or load_backend == "cpu_gpu"
    decision_mode = ("force_cpu" if requested_force_cpu else
                     "force_gpu" if requested_force_gpu or requested_backend == "cpu_gpu" else
                     "auto")
    route_reason = req.get("_route_reason") or (
        "CPU forced by the caller" if requested_force_cpu else None)
    route_details: Dict[str, Any] = {}
    if eligibility:
        force_cpu, force_gpu = True, False
        route_reason = route_reason or "preflight selected CPU: " + eligibility
        route_details["eligibility"] = eligibility
    # Preserve the single-query heuristic. Both engines can keep data resident:
    # resident GPU versus stateless CPU is not a fair acceleration comparison.
    if not force_cpu and not force_gpu:
        use_gpu, route_reason = GA.pick_engine_for(
            str(path), "session", details=route_details
        )
        if not use_gpu:
            return _err(
                "no GPU session opened: " + str(route_reason),
                hint=("for a dataset this size the CPU path is faster for a ONE-OFF analysis, and "
                      "a session would hold device memory for nothing. Call analyze_dataset or "
                      "export_deliverables instead; they will use pandas. If you are going to run "
                      "SEVERAL analyses over this same file, reopening it each time is the cost "
                      "you are paying -- retry open with force_cpu=true for a resident CPU "
                      "dataframe. Use force_gpu=true for an explicit comparison only; repeated "
                      "work alone does not establish GPU superiority."),
                route_threshold_rows=GA.SMALL_ROWS,
                suggest_engine="pandas",
                session_worth_it_if="several analyses over the same file, not one",
            )
    selected_backend = "pandas" if force_cpu else "cudf"
    init_started = time.perf_counter()
    try:
        eng = GA.detect_engine(force_cpu=force_cpu)
    except Exception as exc:
        return _err(f"engine init failed: {type(exc).__name__}: {exc}")
    init_seconds = time.perf_counter() - init_started

    started = time.perf_counter()
    read_fallback = None
    cache_trace = {"status": "disabled"}
    load_attempts = []

    def read_frame(engine):
        nonlocal cache_trace
        trace = {"load_engine": engine.name, "compute_engine": engine.name,
                 "read_count": 1, "conversion_count": 0}
        load_attempts.append(trace)
        if load_backend == "cpu_gpu" and engine.is_gpu:
            return GA.hybrid_execution.read_gpu(engine, path, cols, trace)
        frame, cache_trace = GA.parquet_cache.read(
            engine, path, None if req.get("_fresh_batch") else
            os.environ.get("GPU_ANALYSIS_PARQUET_CACHE_DIR"), usecols=cols)
        if getattr(engine, "last_read", None):
            trace.update(engine.last_read)
        return frame

    try:
        frame = read_frame(eng)
    except Exception as exc:
        if isinstance(exc, (GA.hybrid_execution.HybridMemoryRefused, GA.hybrid_execution.HybridInputChanged)):
            return _err(str(exc), load_refused=True, loading_attempts=load_attempts)
        read_fallback = ("read failed: " + str(exc)) if eng.is_gpu else None
        if eng.is_gpu:
            # Same fallback rule as the stateless path: if cuDF cannot read it, retry pandas.
            try:
                eng = GA.detect_engine(force_cpu=True)
                frame = read_frame(eng)
            except Exception as exc2:
                return _err(f"read failed: {type(exc2).__name__}: {exc2}")
        else:
            return _err(f"read failed: {type(exc).__name__}: {exc}", reason=read_fallback)
    load_seconds = time.perf_counter() - started

    if not hasattr(frame, "columns") or len(frame.columns) == 0:
        return _err(f"no columns could be read from {path}")

    _COUNTER["n"] += 1
    sid = f"s{_COUNTER['n']}"
    sess = Session(
        sid=sid, path=path, engine=eng, frame=frame, rows=int(len(frame)),
        identity=_file_identity(path), load_seconds=load_seconds,
        reason=route_reason if selected_backend == "pandas" else None, usecols=cols,
        loading={"requested": requested_backend,
                 "actual": "cpu_gpu" if eng.is_gpu and (load_backend == "cpu_gpu" or
                 any(a.get("conversion_count") for a in load_attempts)) else
                 "native_gpu" if eng.is_gpu else "cpu", "attempts": load_attempts,
                 "engine_init_seconds": init_seconds, "load_seconds": load_seconds},
    )
    # Explicit multi-step demonstrations still measure the CPU read baseline.
    # Interactive one-shot reuse does not: an unseen extra full CPU pass would
    # dominate the first answer while contributing nothing to its result.
    if req.get("measure_cpu", True):
        try:
            cpu_eng = GA.detect_engine(force_cpu=True)
            t0 = time.perf_counter()
            cpu_eng.read(path, usecols=cols)
            sess.cpu_load_seconds = time.perf_counter() - t0
        except Exception:
            sess.cpu_load_seconds = None

    SESSIONS[sid] = sess

    # Report how much memory the resident copy actually takes, so the cost of holding a
    # session is visible rather than implied.
    resident_mb = None
    if eng.is_gpu:
        try:
            resident_mb = round(float(sess.frame.memory_usage(deep=True).sum()) / (1024 ** 2), 1)
        except Exception:
            resident_mb = None

    out = {
        "ok": True,
        "session_id": sid,
        "file": os.path.basename(path),
        "rows": sess.rows,
        "columns": [str(c) for c in frame.columns],
        "engine": eng.name,
        "gpu": eng.gpu_name,
        "accelerated": eng.is_gpu,
        "load_seconds": round(load_seconds, 3),
        "loading": sess.loading,
        "cpu_load_seconds": round(sess.cpu_load_seconds, 3) if sess.cpu_load_seconds else None,
        "resident_mb": resident_mb,
        "execution_decision": GA.execution_decision_record(
            mode=decision_mode,
            policy="capability_preflight" if eligibility and not requested_force_cpu else
                   "caller_override" if decision_mode != "auto" else
                   "measured_file_size_crossover",
            selected_backend=selected_backend,
            actual_backend=eng.name,
            reason=route_reason or (
                "GPU forced by the caller" if decision_mode == "force_gpu" else
                "the measured file-size crossover policy selected GPU; this is not an "
                "operation-specific cost prediction"
            ),
            signals={"operation": "open", "file_size_bytes": os.path.getsize(path),
                     "admission_required_free_gb": round(need_gb, 3),
                     "free_device_gb": round(free_gb, 3) if free_gb is not None else None,
                     **route_details},
            observed={"phase": "session_open", "elapsed_seconds": round(
                time.perf_counter() - request_started, 6),
                "load_seconds": round(load_seconds, 6), "resident_mb": resident_mb,
                "parquet_cache": cache_trace,
                "rows_scanned": sess.rows},
            fallback_reason=read_fallback or (
                eng.reason if selected_backend == "cudf" and not eng.is_gpu else None
            ),
        ),
        "contract": (
            f"The frame is resident in memory ({eng.name}), {sess.rows:,} rows. Every later "
            "analyze runs on all of it: nothing is re-read from disk and nothing is sampled. "
            "Call close when the analysis is done."
        ),
    }
    if sess.reason:
        out["routing_reason"] = sess.reason
    if out["execution_decision"]["fallback_reason"]:
        out["fallback_reason"] = out["execution_decision"]["fallback_reason"]

    # Build a plan when a goal was given, or when the caller asked for one. The plan is
    # filled in with real column names discovered right here, so the model receives an
    # executable sequence instead of a template.
    want_plan = req.get("goal") or req.get("plan")
    if want_plan:
        group_cols = _pick_group_columns(frame)
        plan = PLANS_MODULE.build_plan(
            plan_id=f"p{sid}", goal=str(req.get("goal") or ""),
            kind=req.get("plan_kind"), group_cols=group_cols,
        )
        sess.plan = plan
        out["plan"] = plan.as_dict()
        out["plan_note"] = (
            "The plan is session state: the system records each step as it runs, so you can "
            "always continue from the next one without tracking progress yourself. If a step "
            "looks wrong you can skip it, and the system marks skipped steps."
        )
    out["execution_decision"]["observed"]["elapsed_seconds"] = round(
        time.perf_counter() - request_started, 6
    )
    return out


def _pick_group_columns(frame) -> list:
    """The frame's categorical columns, for use as the group-by axes of a plan."""
    names = [str(c) for c in frame.columns]
    dtypes = {}
    try:
        for c in frame.columns:
            dtypes[str(c)] = str(frame[c].dtype)
    except Exception:
        dtypes = {}
    out = []
    for c in names:
        dt = dtypes.get(c, "")
        if dt in ("object", "category", "string") or "str" in dt:
            out.append(c)
    return out


def _sid_from(req: dict) -> Any:
    """Read the session handle, accepting both spellings this protocol has used.

    `open` answers with "session_id" while `analyze` and `close` read "sid". The agent layer
    translates between them, which is why the mismatch went unnoticed -- but anything calling
    the worker directly follows the answer it was given and gets "no such session: None".
    Accepting either key removes a trap rather than documenting it.
    """
    return req.get("sid") if req.get("sid") is not None else req.get("session_id")


def do_analyze(req: dict) -> dict:
    sid = _sid_from(req)
    sess = SESSIONS.get(sid)
    if sess is None:
        return _err(f"no such session: {sid}. Open sessions: {sorted(SESSIONS) or 'none'}",
                    open_sessions=sorted(SESSIONS))

    # Staleness guard: answering from a snapshot whose underlying file changed would be a
    # silent wrong answer, which is worse than an error.
    try:
        if _file_identity(sess.path) != sess.identity:
            SESSIONS.pop(sid, None)
            sess = None
            _release_unused_gpu_blocks()
            return _err(
                "The file changed while this session was open (size or modification time). "
                "Rather than answer from the old snapshot, the session is invalidated: open "
                "the file again."
            )
    except OSError as exc:
        SESSIONS.pop(sid, None)
        sess = None
        _release_unused_gpu_blocks()
        return _err(f"could not verify the file state, session invalidated: {exc}")

    op = str(req.get("op") or "auto")
    if op not in GA.VALID_OPS:
        return _err(f"unsupported op: {op}. Valid ops: {', '.join(GA.VALID_OPS)}")

    args = _build_args(req)
    args.input = sess.path

    started = time.perf_counter()
    reread_for_fallback = False

    def load_for(engine):
        nonlocal reread_for_fallback
        if engine.name == sess.engine.name:
            return sess.frame
        # A cuDF operation can be unsupported. The pandas retry needs a pandas frame,
        # not the resident cuDF frame handed to it under a different engine label.
        reread_for_fallback = True
        return engine.read(sess.path, usecols=list(sess.frame.columns))

    try:
        payload, used, _elapsed, fallback, rows = GA.execute(
            sess.engine, load_for, op, args,
            statistics_for=(sess.statistics.for_engine if STATISTICS_CACHE_COLUMNS else
                            lambda _engine: {}),
        )
    except Exception as exc:
        return _err(f"{op} failed: {type(exc).__name__}: {exc}")

    step_seconds = time.perf_counter() - started
    # Commit statistics only after a successful operation. The file identity guard
    # above protects both the frame and these exact statistics on every request.
    sess.statistics.record(op, payload, used, STATISTICS_CACHE_COLUMNS)
    sess.steps += 1
    sess.analysis_seconds += step_seconds

    out = {
        "ok": True,
        "session_id": sess.sid,
        "op": op,
        "engine": used.name,
        "gpu": used.gpu_name,
        "accelerated": used.is_gpu,
        "rows_scanned": rows,
        "step_seconds": round(step_seconds, 3),
        "cumulative_seconds": round(sess.load_seconds + sess.analysis_seconds, 3),
        "steps_this_session": sess.steps,
        "execution_decision": GA.execution_decision_record(
            mode="resident", policy="resident_reuse",
            selected_backend=sess.engine.name, actual_backend=used.name,
            reason=(f"reused the resident {sess.engine.name} frame; no file was reloaded"
                    if not reread_for_fallback else
                    f"the resident {sess.engine.name} operation fell back to {used.name}; "
                    "the file was reloaded for the fallback"),
            signals={"operation": op, "file_size_bytes": sess.identity[1],
                     "session_id": sess.sid, "resident": True},
            observed={"phase": "session_step", "elapsed_seconds": round(step_seconds, 6),
                      "compute_seconds": payload.get("compute_seconds"),
                      "rows_scanned": rows,
                      "statistics_reused_columns": payload.get("statistics_reused_columns", []),
                      "valid_count_reused_columns": payload.get("valid_count_reused_columns", []),
                      "statistics_cache_entries": len(sess.statistics),
                      "reread_for_fallback": reread_for_fallback},
            fallback_reason=fallback,
        ),
        op: payload,
    }
    if sess.reason:
        out["routing_reason"] = sess.reason
    if fallback:
        out["fallback_reason"] = fallback
    # Restate the invariant every time: the model should never drift into describing a
    # session step as a sample.
    data_path_note = ("The GPU operation fell back to pandas, which re-read the full file. "
                      if reread_for_fallback else
                      f"This step ran on all {rows:,} resident rows, with no read from disk. ")
    out["note"] = (
        data_path_note +
        f"Session total {out['cumulative_seconds']:.2f}s (load {sess.load_seconds:.2f}s + "
        f"{sess.steps} steps of analysis {sess.analysis_seconds:.2f}s)."
    )

    # --- plan bookkeeping -------------------------------------------------------------
    # The executor records progress, not the model. This is what turns a plan from a
    # suggestion in the prompt into state the system owns: "what is left" is answered from
    # the session, so it cannot drift as the conversation grows.
    if sess.plan is not None:
        hit = PLANS_MODULE.record(sess.plan, op, req, step_seconds,
                                 summary=_step_summary(op, payload))
        out["plan"] = hit["plan"]
        if hit.get("matched"):
            out["plan"]["recorded_step"] = hit["step"]
        elif hit.get("off_plan"):
            out["plan"]["off_plan_step"] = hit["off_plan"]
            out["plan"]["off_plan_note"] = (
                "This step is not in the plan. It is recorded but does not count towards plan "
                "progress; if it was worth more than the planned next step, keep going with "
                "your own judgement."
            )
    return out


def _step_summary(op: str, payload: Any) -> Optional[str]:
    """
    One short, factual line about what a step found.

    Deliberately terse and derived only from the payload: this is what the plan's completed
    steps display, so it must be checkable rather than a narration of the step.
    """
    try:
        if op == "outliers" and isinstance(payload, dict):
            res = payload.get("results") or {}
            worst = None
            for name, info in res.items():
                if isinstance(info, dict) and isinstance(info.get("count"), (int, float)):
                    if worst is None or info["count"] > worst[1]:
                        worst = (name, info["count"], info.get("pct"))
            if worst:
                pct = f" ({worst[2]}%)" if worst[2] is not None else ""
                return f"{len(res)} columns have outliers, worst {worst[0]}: {worst[1]:,}{pct}"
        if op == "groupby" and isinstance(payload, dict):
            rows = payload.get("top_k") or []
            if rows:
                by = payload.get("by")
                first = rows[0]
                metric = next((k for k in first if k != by), None)
                if metric:
                    return (f"grouped by {by} into {payload.get('groups')} groups, "
                            f"highest {metric}: {first.get(by)}={first.get(metric)}")
        if op == "corr" and isinstance(payload, dict):
            pairs = payload.get("pairs") or []
            if pairs:
                p = pairs[0]
                return f"strongest correlation: {p.get('a')} ~ {p.get('b')} r={p.get('corr')}"
        if op == "summary" and isinstance(payload, dict):
            st = payload.get("stats") or {}
            return f"distribution stats for {len(st)} numeric columns"
        if op == "profile" and isinstance(payload, dict):
            return f"{payload.get('rows')} rows x {payload.get('columns_count')} columns"
        if op == "auto" and isinstance(payload, dict):
            return f"overview complete, {payload.get('rows_scanned')} rows scanned"
    except Exception:
        return None
    return None


def do_list(_req: dict) -> dict:
    _prune_cache()
    return {
        "ok": True,
        "count": len(SESSIONS),
        "max_sessions": MAX_SESSIONS,
        "warm_cache_count": len(WARM_CACHE),
        "warm_cache_mb": round(sum(_frame_bytes(s) for s in WARM_CACHE.values()) / 1024**2, 1),
        "sessions": [
            {
                "session_id": s.sid,
                "file": os.path.basename(s.path),
                "rows": s.rows,
                "engine": s.engine.name,
                "steps": s.steps,
                "idle_seconds": round(time.perf_counter() - s.opened_at, 1),
                "cumulative_seconds": round(s.load_seconds + s.analysis_seconds, 3),
            }
            for s in SESSIONS.values()
        ],
    }


def do_close(req: dict) -> dict:
    sid = _sid_from(req)
    if sid in (None, "", "all"):
        n = len(SESSIONS)
        retained = sum(_retain(sess) for sess in list(SESSIONS.values())) if req.get("retain") else 0
        SESSIONS.clear()
        if not req.get("retain"):
            WARM_CACHE.clear()
            _release_unused_gpu_blocks()
        return {"ok": True, "closed": n, "cached": retained,
                "note": ("retained bounded GPU frames for later questions" if retained else
                         "released the memory held by all sessions")}
    sess = SESSIONS.pop(sid, None)
    if sess is None:
        return _err(f"no such session: {sid}")

    retained = _retain(sess) if req.get("retain") else False

    total = sess.load_seconds + sess.analysis_seconds
    out = {
        "ok": True,
        "session_id": sid,
        "closed": 1,
        "cached": bool(retained),
        "steps": sess.steps,
        "load_seconds": round(sess.load_seconds, 3),
        "analysis_seconds": round(sess.analysis_seconds, 3),
        "total_seconds": round(total, 3),
    }
    # Workflow-level comparison: this session's whole multi-step analysis against what the
    # same number of steps costs when every step re-reads the file from disk on the CPU.
    if sess.cpu_load_seconds and sess.steps:
        naive_cpu = sess.cpu_load_seconds * sess.steps
        out["workflow_comparison"] = {
            "session_total_seconds": round(total, 3),
            "naive_cpu_seconds": round(naive_cpu, 3),
            "speedup_x": round(naive_cpu / total, 2) if total > 0 else None,
            "note": (
                f"{sess.steps} full-data steps in this session took {total:.2f}s. Re-reading "
                f"the file from disk on the CPU at every step (read time only, no compute), "
                f"would take about {naive_cpu:.1f}s (one read {sess.cpu_load_seconds:.2f}s x "
                f"{sess.steps} steps). The factor includes the gain from keeping the frame "
                f"resident, so it is not a pure GPU compute speedup."
            ),
        }
    # Free device memory deterministically rather than waiting for the next collection.
    try:
        if not retained:
            del sess.frame
        if sess.engine.is_gpu and not retained:
            import cupy  # type: ignore

            cupy.get_default_memory_pool().free_all_blocks()
    except Exception:
        pass
    return out


def do_oneshot(req: dict) -> dict:
    """Keep imports/CUDA context warm, but release this request's frame."""
    argv = req.get("argv")
    if not isinstance(argv, list) or not all(isinstance(v, str) for v in argv):
        return _err("argv must be a list of strings")
    output = io.StringIO()
    try:
        with contextlib.redirect_stdout(output):
            context = "warm" if GA._ENGINE is not None and GA._ENGINE.is_gpu else "cold"
            code = GA.main(argv, execution_context=context)
        payload = json.loads(output.getvalue())
        return {"ok": True, "payload": payload, "exit_code": code}
    except SystemExit as exc:
        return _err(f"invalid analysis arguments (exit {exc.code})")
    finally:
        _release_unused_gpu_blocks()


def batch_workflow(steps):
    return [GA.hybrid_execution.single_workflow(_build_args({
        **({"top_k": 30} if step["op"] == "groupby" else {}), **step}))[0] for step in steps]


def do_batch(req: dict) -> dict:
    """Execute an independent bounded plan on one projected resident frame."""
    request_started = time.perf_counter()
    steps = req.get("steps")
    allowed = {"op", "by", "agg", "columns", "top_k"}
    if not isinstance(steps, list) or not 1 <= len(steps) <= 8:
        return _err("steps must contain 1 to 8 analyses")
    for step in steps:
        if (not isinstance(step, dict) or set(step) - allowed
                or step.get("op") not in GA.VALID_OPS):
            return _err("invalid batch step; use op/by/agg/columns/top_k only")
        if step.get("op") == "groupby" and not step.get("by"):
            return _err("groupby step requires by")
        if any(step.get(key) is not None and not isinstance(step[key], str)
               for key in ("by", "agg", "columns")):
            return _err("batch by/agg/columns must be strings")
        if step.get("top_k") is not None and (isinstance(step["top_k"], bool)
                or not isinstance(step["top_k"], int) or not 1 <= step["top_k"] <= 100):
            return _err("batch top_k must be an integer from 1 to 100")
    path = req.get("path")
    if not isinstance(path, str) or not os.path.isfile(path):
        return _err("batch path must be an existing file")
    path = os.path.abspath(path)
    # Never borrow/close a model-managed session, including a same-path handle.
    if any(s.path == path for s in SESSIONS.values()):
        return _err("file already has an active session; analyze on that session instead")
    columns = set()
    for step in steps:
        required = GA.required_read_columns(step["op"], _build_args(step))
        if required is None:
            columns = None
            break
        columns.update(required)
    force_cpu = bool(req.get("force_cpu"))
    force_gpu = bool(req.get("force_gpu"))
    backend = req.get("load_backend", os.environ.get("GPU_ANALYSIS_LOAD_BACKEND", "auto"))
    if backend not in {"auto", "native", "cpu_gpu"}:
        return _err("load_backend must be auto, native or cpu_gpu")
    if force_cpu and backend == "cpu_gpu":
        return _err("force_cpu conflicts with hybrid request")
    if force_cpu and force_gpu:
        return _err("force_cpu and force_gpu are mutually exclusive")
    context = "warm" if GA._ENGINE is not None and GA._ENGINE.is_gpu else "cold"
    decision = {"selected": None, "reason": "explicit/native request"}
    selected = None
    eligibility = GA.hybrid_execution.preflight(path, columns, batch_workflow(steps), backend)
    if eligibility and backend == "cpu_gpu":
        return _err("CPU-to-GPU loading unavailable: " + eligibility)
    if eligibility and not force_cpu:
        force_cpu, force_gpu = True, False
        decision = {"selected": "cpu", "policy": "capability_preflight",
                    "reason": "preflight selected CPU: " + eligibility}
    if backend == "auto" and not eligibility and not force_cpu:
        selected, decision = GA.hybrid_execution.choose(
            req.get("hybrid_profile", os.environ.get("GPU_ANALYSIS_HYBRID_PROFILE")), path,
            columns, batch_workflow(steps), "batch_" + context)
        if selected == "cpu_gpu":
            backend = "cpu_gpu"
        elif selected:
            backend = "native"
    if eligibility:
        pass  # CPU choice already made, before CUDA initialization or reading.
    elif backend == "cpu_gpu":
        force_gpu = True
    elif not force_cpu and not force_gpu and selected:
        force_cpu = selected == "cpu"
        force_gpu = not force_cpu
    elif not force_cpu and not force_gpu:
        use_gpu, _reason = GA.pick_engine_for(path, "session")
        force_cpu = not use_gpu
    started = time.perf_counter()
    opened = do_open({"path": path, "usecols": ",".join(sorted(columns)) if columns else None,
                      "force_cpu": force_cpu, "force_gpu": force_gpu,
                      "measure_cpu": False, "_fresh_batch": True,
                      "_route_reason": decision.get("reason") if eligibility else None,
                      "_batch_load_backend": backend})
    if not opened.get("ok"):
        return opened
    sid = opened["session_id"]
    results = []
    try:
        for index, step in enumerate(steps):
            defaults = {"top_k": 30} if step["op"] == "groupby" else {}
            reply = do_analyze({**defaults, **step, "sid": sid})
            group = reply.get("groupby") or {}
            if len(group.get("top_k", [])) < group.get("groups", 0):
                reply["group_coverage_warning"] = (
                    "Truncated top-K, not all groups; do not infer a global minimum.")
            results.append(reply)
            if not reply.get("ok"):
                return _err(reply.get("error", "batch step failed"), failed_step=index,
                            results=results, completed_steps=index)
        return {"ok": True, "results": results, "rows": opened["rows"],
                "engine": opened["engine"], "load_seconds": opened["load_seconds"],
                "total_seconds": round(time.perf_counter() - request_started, 6),
                "projected_columns": sorted(columns) if columns else None,
                "loading": {**opened.get("loading", {}), "requested": req.get("load_backend", "auto"),
                            "decision": decision, "execution_context": context,
                            "scope": "one fresh load/conversion per batch; no cross-batch data cache"},
                "note": "One load, exact full-data operations; no CPU baseline or sampling. "
                        "Any per-step fallback is reported in that step."}
    finally:
        do_close({"sid": sid})


HANDLERS = {
    "oneshot": do_oneshot,
    "batch": do_batch,
    "open": do_open,
    "analyze": do_analyze,
    "list": do_list,
    "close": do_close,
}


def handle(req: dict) -> dict:
    cmd = req.get("cmd")
    if cmd == "ping":
        eng = GA.detect_engine()
        free = _free_gpu_gb()
        return {
            "ok": True,
            "pong": True,
            "engine": eng.name,
            "gpu": eng.gpu_name,
            "free_gpu_gb": round(free, 1) if free is not None else None,
            "max_sessions": MAX_SESSIONS,
        }
    fn = HANDLERS.get(cmd)
    if fn is None:
        return _err(f"unknown command: {cmd}. Available: ping, {', '.join(HANDLERS)}")
    return fn(req)


def main() -> int:
    # Line-buffered so the parent sees each response as soon as it is produced.
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError as exc:
            resp = _err(f"request is not valid JSON: {exc}")
        else:
            try:
                resp = handle(req)
            except Exception as exc:  # a worker must outlive any single bad request
                resp = _err(f"internal error: {type(exc).__name__}: {exc}")
        sys.stdout.write(json.dumps(resp, ensure_ascii=False, default=str) + "\n")
        sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
