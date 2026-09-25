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


GA = _import_engine()

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
    st = os.stat(path)
    return [os.path.abspath(path), st.st_size, int(st.st_mtime)]


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
        return _err("必须提供 path")
    path = os.path.abspath(os.path.expanduser(str(path)))
    if not os.path.isfile(path):
        return _err(
            f"文件不存在: {path}",
            hint=("相对路径按 agent 进程的工作目录解析。请用 list_datasets 取绝对路径后重试；"
                  "如果只是想单次分析，也可以改用 analyze_dataset。"),
        )
    if len(SESSIONS) >= MAX_SESSIONS:
        return _err(
            f"同时打开的会话已达上限 {MAX_SESSIONS}。请先 close 一个不再需要的会话。",
            open_sessions=sorted(SESSIONS),
        )

    # Refuse before loading rather than after: once the parse starts, a failure surfaces as
    # an opaque OOM in the middle of the read.
    size_gb = os.path.getsize(path) / (1024 ** 3)
    free_gb = _free_gpu_gb()
    need_gb = size_gb * MEM_HEADROOM + MEM_FLOOR_GB
    if free_gb is not None and free_gb < need_gb:
        return _err(
            f"显存不足，已拒绝载入：该文件约 {size_gb:.2f}GB，"
            f"预计需要 {need_gb:.1f}GB 空闲显存，当前只有 {free_gb:.1f}GB。"
            f"建议改用 analyze_dataset 单次分析，或先用 columns 参数只读需要的列。",
            file_gb=round(size_gb, 2), free_gb=round(free_gb, 1), need_gb=round(need_gb, 1),
        )

    force_cpu = bool(req.get("force_cpu"))
    try:
        eng = GA.detect_engine(force_cpu=force_cpu)
    except Exception as exc:
        return _err(f"引擎初始化失败: {type(exc).__name__}: {exc}")

    usecols = req.get("usecols")
    cols = None
    if usecols:
        cols = [c.strip() for c in str(usecols).split(",") if c.strip()]

    started = time.perf_counter()
    try:
        frame = eng.read(path, usecols=cols)
    except Exception as exc:
        reason = ("读取失败: " + str(exc)) if eng.is_gpu else None
        if eng.is_gpu:
            # Same fallback rule as the stateless path: if cuDF cannot read it, retry pandas.
            try:
                eng = GA.detect_engine(force_cpu=True)
                frame = eng.read(path, usecols=cols)
            except Exception as exc2:
                return _err(f"读取失败: {type(exc2).__name__}: {exc2}")
        else:
            return _err(f"读取失败: {type(exc).__name__}: {exc}", reason=reason)
    load_seconds = time.perf_counter() - started

    if not hasattr(frame, "columns") or len(frame.columns) == 0:
        return _err(f"无法从 {path} 读出任何列")

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

    return {
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
            f"数据已常驻内存({eng.name})，共 {sess.rows:,} 行。之后每个 analyze 都在全量数据上执行，"
            "不会重新读盘、不会采样。分析完请调用 close 释放内存。"
        ),
    }


def do_analyze(req: dict) -> dict:
    sid = req.get("sid")
    sess = SESSIONS.get(sid)
    if sess is None:
        return _err(f"会话不存在: {sid}。可用会话: {sorted(SESSIONS) or '无'}",
                    open_sessions=sorted(SESSIONS))

    # Staleness guard: answering from a snapshot whose underlying file changed would be a
    # silent wrong answer, which is worse than an error.
    try:
        if _file_identity(sess.path) != sess.identity:
            return _err(
                "文件在会话期间已被修改（大小或修改时间发生变化）。为避免用旧数据算出错误结果，"
                "本会话已失效，请重新 open。"
            )
    except OSError as exc:
        return _err(f"无法校验文件状态，会话失效: {exc}")

    op = str(req.get("op") or "auto")
    if op not in GA.VALID_OPS:
        return _err(f"不支持的操作: {op}。可用: {', '.join(GA.VALID_OPS)}")

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
        return _err(f"{op} 执行失败: {type(exc).__name__}: {exc}")

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
        f"本步在常驻内存的全量 {rows:,} 行上执行，未重新读盘。"
        f"会话累计 {out['cumulative_seconds']:.2f}s（载入 {sess.load_seconds:.2f}s + "
        f"{sess.steps} 步计算 {sess.analysis_seconds:.2f}s）。"
    )
    return out


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
    sid = req.get("sid")
    if sid in (None, "", "all"):
        n = len(SESSIONS)
        SESSIONS.clear()
        return {"ok": True, "closed": n, "note": "已释放所有会话占用的内存。"}
    sess = SESSIONS.pop(sid, None)
    if sess is None:
        return _err(f"会话不存在: {sid}")

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
                f"本次会话 {sess.steps} 步全量分析共 {total:.2f}s；"
                f"若每一步都像传统做法那样用 CPU 重新读盘计算，"
                f"约需 {naive_cpu:.1f}s（单次读盘 {sess.cpu_load_seconds:.2f}s × {sess.steps} 步）。"
                f"注意：该倍数包含「数据已常驻内存」带来的收益，不等于纯 GPU 计算加速比。"
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
        return _err(f"未知命令: {cmd}。可用: ping, {', '.join(HANDLERS)}")
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
            resp = _err(f"请求不是合法 JSON: {exc}")
        else:
            try:
                resp = handle(req)
            except Exception as exc:  # a worker must outlive any single bad request
                resp = _err(f"内部错误: {type(exc).__name__}: {exc}")
        sys.stdout.write(json.dumps(resp, ensure_ascii=False, default=str) + "\n")
        sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())