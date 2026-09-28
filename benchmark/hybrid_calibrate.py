"""Explicit full-data three-path parity benchmark; never called by normal analysis."""
import argparse
import json
import os
from pathlib import Path
import statistics
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "agent"))
sys.path.insert(0, str(ROOT / "skills/cudf-analytics/scripts"))
import skills
import gpu_analytics as ga
import hybrid_execution as hybrid
import gpu_session as sessions
from fast_execution_benchmark import equal, values

DEFAULT_STEPS = [{"op": "summary", "columns": "revenue"},
                 {"op": "outliers", "columns": "revenue", "top_k": 3},
                 {"op": "groupby", "by": "region", "agg": "revenue:sum", "top_k": 3}]


def emit(item):
    print(json.dumps(item), flush=True)


def argv_for(path, step, backend):
    argv = ["--input", path, "--op", step["op"], "--load-backend",
            "cpu_gpu" if backend == "cpu_gpu" else "native",
            "--force-cpu" if backend == "cpu" else "--force-gpu"]
    for key in ("columns", "by", "agg", "top_k"):
        if key in step:
            argv.extend(["--" + key.replace("_", "-"), str(step[key])])
    return argv


def single_measure(path, step, backend, context):
    argv = argv_for(path, step, backend)
    start = time.perf_counter()
    if context == "cold":
        proc = subprocess.run([sys.executable, str(ROOT / "skills/cudf-analytics/scripts/gpu_analytics.py"),
                               *argv], capture_output=True, text=True, timeout=1800)
        assert proc.returncode == 0, (proc.stdout, proc.stderr)
        payload = json.loads(proc.stdout)
    else:
        reply = skills._worker_call({"cmd": "oneshot", "argv": argv})
        assert reply["ok"] and reply["exit_code"] == 0, reply
        payload = reply["payload"]
    elapsed = time.perf_counter() - start
    assert payload["ok"] and payload["engine"] == ("pandas" if backend == "cpu" else "cudf"), payload
    assert payload["loading"]["actual"] == {
        "cpu": "cpu", "native": "native_gpu", "cpu_gpu": "cpu_gpu"}[backend], payload
    attempts = payload["loading"]["attempts"]
    cpu_read = sum(item.get("cpu_read_seconds", 0) for item in attempts)
    return payload, {"seconds": elapsed,
        "read_seconds": cpu_read if backend == "cpu_gpu" else payload["phase_timings"]["load_seconds"],
        "compute_seconds": payload["phase_timings"]["compute_seconds"],
        "conversion_seconds": sum(item.get("conversion_seconds", 0) for item in attempts)}


def batch_measure(path, steps, backend, context):
    if context == "cold":
        skills._worker_stop(skills._worker)
        skills._worker = None
    start = time.perf_counter()
    reply = skills._worker_call({"cmd": "batch", "path": path, "steps": steps,
        "load_backend": "cpu_gpu" if backend == "cpu_gpu" else "native",
        "force_cpu": backend == "cpu", "force_gpu": backend != "cpu"})
    elapsed = time.perf_counter() - start
    assert reply["ok"], reply
    expected_engine = "pandas" if backend == "cpu" else "cudf"
    assert reply["engine"] == expected_engine, reply
    assert reply["loading"]["actual"] == {
        "cpu": "cpu", "native": "native_gpu", "cpu_gpu": "cpu_gpu"}[backend], reply
    assert reply["loading"]["execution_context"] == context, reply
    assert all(result["engine"] == expected_engine for result in reply["results"]), reply
    assert all(skills._rows_of(result) == reply["rows"] for result in reply["results"]), reply
    attempts = reply["loading"]["attempts"]
    assert sum(item.get("read_count", 0) for item in attempts) == 1, reply
    assert sum(item.get("conversion_count", 0) for item in attempts) == (1 if backend == "cpu_gpu" else 0), reply
    return reply, {"seconds": elapsed,
        "read_seconds": sum(item.get("cpu_read_seconds", 0) for item in attempts) if backend == "cpu_gpu" else reply["loading"]["load_seconds"],
        "compute_seconds": sum(result["execution_decision"]["observed"]["compute_seconds"] for result in reply["results"]),
        "conversion_seconds": sum(item.get("conversion_seconds", 0) for item in attempts)}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True)
    parser.add_argument("--profile", required=True)
    parser.add_argument("--repeats", type=int, default=5)
    parser.add_argument("--context", choices=["cold", "warm", "both"], default="both")
    parser.add_argument("--mode", choices=["warm", "batch"], default="warm")
    parser.add_argument("--steps-json", help="1-8 known-column steps; default revenue/region suite")
    args = parser.parse_args()
    if not 2 <= args.repeats <= 20:
        parser.error("repeats must be 2-20")
    steps = json.loads(args.steps_json) if args.steps_json else DEFAULT_STEPS
    if not isinstance(steps, list) or not 1 <= len(steps) <= 8:
        parser.error("steps must have 1-8 entries")
    for step in steps:
        if (not isinstance(step, dict) or set(step) - {"op", "columns", "by", "agg", "top_k"}
                or step.get("op") not in ga.VALID_OPS):
            parser.error("invalid step")
    before = hybrid.identity(args.input)
    os.environ["SKILL_SHOW_SPEEDUP"] = "0"
    contexts = ["cold", "warm"] if args.context == "both" else [args.context]
    for context in contexts:
        if context == "warm":
            start = time.perf_counter()
            ping = skills._worker_call({"cmd": "ping"})
            assert ping["engine"] == "cudf", ping
            emit({"stage": "worker_start", "seconds": time.perf_counter()-start,
                  "note": "includes initial imports and CUDA probe; separate from steady-state measurements"})
        for work in ([steps] if args.mode == "batch" else steps):
            measurements = {backend: [] for backend in hybrid.PATHS}
            expected = None
            expected_rows = None
            for repeat in range(args.repeats):
                order = hybrid.PATHS if repeat % 2 == 0 else tuple(reversed(hybrid.PATHS))
                for backend in order:
                    if args.mode == "batch":
                        payload, timing = batch_measure(args.input, work, backend, context)
                        output, rows = [values(result) for result in payload["results"]], payload["rows"]
                    else:
                        payload, timing = single_measure(args.input, work, backend, context)
                        output, rows = values(payload), skills._rows_of(payload)
                    if expected is None:
                        expected, expected_rows = output, rows
                    assert equal(output, expected) and rows == expected_rows, (backend, work, output, expected)
                    measurements[backend].append(timing)
                    emit({"stage": "measurement", "work": work, "mode": args.mode, "context": context,
                          "backend": backend, "rows": rows, **timing, "results_agree": True})
            assert hybrid.identity(args.input) == before, "input changed during benchmark"
            if args.mode == "batch":
                workflow = sessions.batch_workflow(work)
                columns = set()
                for step in work:
                    required = ga.required_read_columns(step["op"], sessions._build_args(step))
                    if required is None:
                        columns = None
                        break
                    columns.update(required)
            else:
                parsed = ga.build_parser().parse_args(argv_for(args.input, work, "native"))
                columns = ga.required_read_columns(work["op"], parsed)
                workflow = hybrid.single_workflow(parsed)
            profile_context = ("batch_" if args.mode == "batch" else "oneshot_") + context
            hybrid.save_measurement(args.profile, args.input, columns, workflow,
                                    profile_context, measurements)
            selected, decision = hybrid.choose(args.profile, args.input, columns, workflow, profile_context)
            emit({"stage": "calibrated", "work": work, "mode": args.mode, "context": context, "selected": selected,
                  "median_seconds": {key: statistics.median(run["seconds"] for run in runs)
                                     for key, runs in measurements.items()}, "decision": decision})
            if context == "cold":
                skills._worker_stop(skills._worker)
                skills._worker = None
            if args.mode == "batch":
                automatic = skills._worker_call({"cmd": "batch", "path": args.input, "steps": work,
                    "load_backend": "auto", "hybrid_profile": args.profile})
                assert automatic["ok"], automatic
                automatic_values = [values(result) for result in automatic["results"]]
            else:
                auto_argv = argv_for(args.input, work, "native")
                auto_argv.remove("--force-gpu")
                auto_argv[auto_argv.index("--load-backend")+1] = "auto"
                auto_argv.extend(["--hybrid-profile", args.profile])
                if context == "cold":
                    proc = subprocess.run([sys.executable, str(ROOT / "skills/cudf-analytics/scripts/gpu_analytics.py"),
                                           *auto_argv], capture_output=True, text=True, timeout=1800)
                    assert proc.returncode == 0, proc.stdout
                    automatic = json.loads(proc.stdout)
                else:
                    reply = skills._worker_call({"cmd": "oneshot", "argv": auto_argv})
                    assert reply["ok"] and reply["exit_code"] == 0, reply
                    automatic = reply["payload"]
                automatic_values = values(automatic)
            assert automatic["loading"]["decision"]["selected"] == selected, automatic
            assert automatic["loading"]["actual"] == {
                "cpu": "cpu", "native": "native_gpu", "cpu_gpu": "cpu_gpu"}[selected], automatic
            assert equal(automatic_values, expected), automatic
            emit({"stage": "auto_verified", "mode": args.mode, "context": context,
                  "selected": selected, "actual": automatic["loading"]["actual"], "results_agree": True})
    state = skills._worker_call({"cmd": "list"}) if skills._worker is not None else {"count": 0, "warm_cache_count": 0}
    assert state["count"] == 0 and state["warm_cache_count"] == 0, state
    emit({"stage": "complete", "sources_unchanged": True, "active_sessions": 0,
          "profile": args.profile, "note": "profile expires in 24h; scoped to exact file, columns, options, host and context"})


if __name__ == "__main__":
    try:
        main()
    finally:
        skills._worker_stop(skills._worker)
