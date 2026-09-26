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

# Sessions hold a full dataset in GPU memory, so they are capped. Each open session on a
# 20M-row file costs roughly 1-2 GB of device memory; refusing the 5th is better than
# letting an agent work the box into an OOM it cannot diagnose.
MAX_SESSIONS = int(os.environ.get("SESSION_MAX", "4"))

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
    # The strategy this session is following, if a goal was supplied at open time. Held on
    # the session so progress survives across analyze calls without the model tracking it.
    plan: Any = None


SESSIONS: Dict[str, Session] = {}
_COUNTER = {"n": 0}


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

    # Reuse an existing session for the same file instead of loading the dataset again.
    #
    # Why: a caller that loses track of the session id it was handed opens a second session
    # on the same file. Measured on 20M rows, that doubled the resident footprint (~3.4 GB)
    # and left the first session stranded until a cleanup backstop ran -- real device memory
    # held for no benefit. Returning the existing handle costs nothing and cannot be worse
    # than a second full load, so it is the right default rather than an optimisation.
    for existing in SESSIONS.values():
        if existing.path == path:
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
                "reused_existing_session": True,
                "already_loaded": True,
                "note": (
                    f"This file is already loaded, so session {existing.sid} is reused and no "
                    "second copy took device memory. **Use session_id="
                    f"{existing.sid}** for every later call, and release it with close."
                ),
            }

    # Refuse before loading rather than after: once the parse starts, a failure surfaces as
    # an opaque OOM in the middle of the read.
    size_gb = os.path.getsize(path) / (1024 ** 3)
    free_gb = _free_gpu_gb()
    need_gb = size_gb * MEM_HEADROOM + MEM_FLOOR_GB
    if free_gb is not None and free_gb < need_gb:
        return _err(
            f"Not enough free device memory, load refused: the file is about {size_gb:.2f}GB, "
            f"which needs roughly {need_gb:.1f}GB free and only {free_gb:.1f}GB is available. "
            f"Use analyze_dataset for a single analysis, or pass columns to read only the "
            "columns you need.",
            file_gb=round(size_gb, 2), free_gb=round(free_gb, 1), need_gb=round(need_gb, 1),
        )

    force_cpu = bool(req.get("force_cpu"))
    force_gpu = bool(req.get("force_gpu"))
    # Preserve the single-query heuristic. Both engines can keep data resident:
    # resident GPU versus stateless CPU is not a fair acceleration comparison.
    if not force_cpu and not force_gpu:
        use_gpu, route_reason = GA.pick_engine_for(str(path), "session")
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
    try:
        eng = GA.detect_engine(force_cpu=force_cpu)
    except Exception as exc:
        return _err(f"engine init failed: {type(exc).__name__}: {exc}")

    usecols = req.get("usecols")
    cols = None
    if usecols:
        cols = [c.strip() for c in str(usecols).split(",") if c.strip()]

    started = time.perf_counter()
    try:
        frame = eng.read(path, usecols=cols)
    except Exception as exc:
        reason = ("read failed: " + str(exc)) if eng.is_gpu else None
        if eng.is_gpu:
            # Same fallback rule as the stateless path: if cuDF cannot read it, retry pandas.
            try:
                eng = GA.detect_engine(force_cpu=True)
                frame = eng.read(path, usecols=cols)
            except Exception as exc2:
                return _err(f"read failed: {type(exc2).__name__}: {exc2}")
        else:
            return _err(f"read failed: {type(exc).__name__}: {exc}", reason=reason)
    load_seconds = time.perf_counter() - started

    if not hasattr(frame, "columns") or len(frame.columns) == 0:
        return _err(f"no columns could be read from {path}")

    _COUNTER["n"] += 1
    sid = f"s{_COUNTER['n']}"
    sess = Session(
        sid=sid, path=path, engine=eng, frame=frame, rows=int(len(frame)),
        identity=_file_identity(path), load_seconds=load_seconds,
    )
    # A CPU-resident frame is the honest baseline for this same file, and it is measured
    # without touching the GPU. Failures here must not fail the open.
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
        "cpu_load_seconds": round(sess.cpu_load_seconds, 3) if sess.cpu_load_seconds else None,
        "resident_mb": resident_mb,
        "contract": (
            f"The frame is resident in memory ({eng.name}), {sess.rows:,} rows. Every later "
            "analyze runs on all of it: nothing is re-read from disk and nothing is sampled. "
            "Call close when the analysis is done."
        ),
    }

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
            return _err(
                "The file changed while this session was open (size or modification time). "
                "Rather than answer from the old snapshot, the session is invalidated: open "
                "the file again."
            )
    except OSError as exc:
        return _err(f"could not verify the file state, session invalidated: {exc}")

    op = str(req.get("op") or "auto")
    if op not in GA.VALID_OPS:
        return _err(f"unsupported op: {op}. Valid ops: {', '.join(GA.VALID_OPS)}")

    args = _build_args(req)
    args.input = sess.path

    started = time.perf_counter()
    try:
        # The whole point: `load` hands back the resident frame, so `execute` never reads
        # the file. Every other behaviour (ops, fallback, timing) is the shared code path.
        payload, used, _elapsed, fallback, rows = GA.execute(
            sess.engine, lambda _eng: sess.frame, op, args
        )
    except Exception as exc:
        return _err(f"{op} failed: {type(exc).__name__}: {exc}")

    step_seconds = time.perf_counter() - started
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
        op: payload,
    }
    if fallback:
        out["fallback_reason"] = fallback
    # Restate the invariant every time: the model should never drift into describing a
    # session step as a sample.
    out["note"] = (
        f"This step ran on all {rows:,} resident rows, with no read from disk. "
        f"Session total {out['cumulative_seconds']:.2f}s (load {sess.load_seconds:.2f}s + "
        f"{sess.steps} steps of compute {sess.analysis_seconds:.2f}s)."
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
    return {
        "ok": True,
        "count": len(SESSIONS),
        "max_sessions": MAX_SESSIONS,
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
        SESSIONS.clear()
        return {"ok": True, "closed": n, "note": "released the memory held by all sessions."}
    sess = SESSIONS.pop(sid, None)
    if sess is None:
        return _err(f"no such session: {sid}")

    total = sess.load_seconds + sess.analysis_seconds
    out = {
        "ok": True,
        "session_id": sid,
        "closed": 1,
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
        del sess.frame
        if sess.engine.is_gpu:
            import cupy  # type: ignore

            cupy.get_default_memory_pool().free_all_blocks()
    except Exception:
        pass
    return out


HANDLERS = {
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
