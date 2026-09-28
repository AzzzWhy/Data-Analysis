"""Compare one-load conversation sessions with one-load batches on the same work."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import statistics
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "agent"))
from fast_execution_benchmark import equal, values
import skills


def _run_session(path: str, steps: list[dict], columns: str, backend: str) -> tuple[list, dict]:
    opened = skills._worker_call({"cmd": "open", "path": path, "usecols": columns,
        "force_cpu": backend == "cpu", "force_gpu": backend == "native",
        "load_backend": "cpu_gpu" if backend == "cpu_gpu" else "native",
        "measure_cpu": False})
    if not opened.get("ok"):
        raise RuntimeError(f"session open failed: {opened}")
    sid = opened["session_id"]
    replies = []
    try:
        for step in steps:
            result = skills._worker_call({"cmd": "analyze", "sid": sid, **step})
            if not result.get("ok"):
                raise RuntimeError(f"session step failed: {result}")
            replies.append(result)
    finally:
        closed = skills._worker_call({"cmd": "close", "sid": sid, "retain": False})
        if not closed.get("ok"):
            raise RuntimeError(f"session close failed: {closed}")
    return replies, opened


def _run_batch(path: str, steps: list[dict], backend: str) -> tuple[list, dict]:
    reply = skills._worker_call({"cmd": "batch", "path": path, "steps": steps,
        "force_cpu": backend == "cpu", "force_gpu": backend == "native",
        "load_backend": "cpu_gpu" if backend == "cpu_gpu" else "native"})
    if not reply.get("ok"):
        raise RuntimeError(f"batch failed: {reply}")
    return reply["results"], reply


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True)
    parser.add_argument("--columns", required=True,
                        help="columns to retain; must cover every step")
    parser.add_argument("--steps-json", required=True,
                        help="1-8 already known analyses, JSON array")
    parser.add_argument("--backend", choices=["cpu", "native", "cpu_gpu"], required=True)
    parser.add_argument("--repeats", type=int, default=5)
    parser.add_argument("--output", type=Path, help="optional JSON evidence file")
    opts = parser.parse_args(argv)
    if not 2 <= opts.repeats <= 20:
        parser.error("repeats must be 2-20")
    path = str(Path(opts.input).resolve())
    if not Path(path).is_file():
        parser.error("input file does not exist")
    steps = json.loads(opts.steps_json)
    if not isinstance(steps, list) or not 1 <= len(steps) <= 8:
        parser.error("steps must contain 1-8 analyses")
    columns = ",".join(dict.fromkeys(c.strip() for c in opts.columns.split(",") if c.strip()))
    if not columns:
        parser.error("columns cannot be empty")
    before = skills._file_identity(path)
    # A resident-cache hit would give only the session path an unearned second-run
    # advantage. Disable it before starting the worker; both paths then load once/run.
    os.environ["SESSION_WARM_CACHE_MB"] = "0"
    startup = time.perf_counter()
    ping = skills._worker_call({"cmd": "ping"})
    if not ping.get("ok"):
        raise RuntimeError(f"worker startup failed: {ping}")
    startup_seconds = time.perf_counter() - startup
    times = {"session": [], "batch": []}
    expected = None
    actual_paths = {"session": set(), "batch": set()}
    try:
        for repeat in range(opts.repeats):
            order = ("session", "batch") if repeat % 2 == 0 else ("batch", "session")
            for mode in order:
                started = time.perf_counter()
                replies, info = (_run_session(path, steps, columns, opts.backend) if
                                 mode == "session" else _run_batch(path, steps, opts.backend))
                times[mode].append(time.perf_counter() - started)
                outputs = [values(reply) for reply in replies]
                if expected is None:
                    expected = outputs
                if not equal(outputs, expected):
                    raise AssertionError(f"result mismatch in {mode} repetition {repeat + 1}")
                if any(reply.get("engine") != ("pandas" if opts.backend == "cpu" else "cudf")
                       for reply in replies):
                    raise AssertionError(f"unexpected compute engine in {mode}")
                actual_paths[mode].add(info.get("loading", {}).get("actual"))
                state = skills._worker_call({"cmd": "list"})
                if state.get("count") or state.get("warm_cache_count"):
                    raise AssertionError(f"worker retained frames after {mode}: {state}")
        if skills._file_identity(path) != before:
            raise AssertionError("input file changed during benchmark")
        medians = {key: statistics.median(run) for key, run in times.items()}
        evidence = {"rows": replies[0]["rows_scanned"], "backend": opts.backend,
              "steps": steps, "startup_seconds": startup_seconds,
              "times": times, "median_seconds": medians,
              "session_over_batch": medians["session"] / medians["batch"],
              "actual_load_paths": {key: sorted(value) for key, value in actual_paths.items()},
              "results_agree": True, "source_unchanged": True,
              "scope": f"same worker, one read per workflow, {opts.repeats} alternating repetitions; no LLM"}
        serialized = json.dumps(evidence, ensure_ascii=False)
        if opts.output:
            opts.output.write_text(serialized + "\n", encoding="utf-8")
        print(serialized, flush=True)
        return 0
    finally:
        skills._worker_stop(skills._worker)


if __name__ == "__main__":
    raise SystemExit(main())
