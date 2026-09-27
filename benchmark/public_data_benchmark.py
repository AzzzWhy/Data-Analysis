"""Matched CPU/GPU execution benchmarks on a verified full public dataset."""
import argparse
import json
import os
from pathlib import Path
import statistics
import sys
import time

import pyarrow.parquet as pq

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "agent"))
import skills
from fast_execution_benchmark import equal, values


def emit(value):
    print(json.dumps(value, ensure_ascii=False), flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True)
    parser.add_argument("--min-rows", type=int, default=100_000_000)
    args = parser.parse_args()
    rows = pq.read_metadata(args.input).num_rows
    assert rows >= args.min_rows, (rows, args.min_rows)
    identity = skills._file_identity(args.input)
    os.environ["SKILL_SHOW_SPEEDUP"] = "0"
    start = time.perf_counter()
    ping = skills._worker_call({"cmd": "ping"})
    assert ping["ok"] and ping["engine"] == "cudf", ping
    # Conservative experimental budget, separate from the production file guard.
    budget_gib = rows * 16 * 6 / 1024**3 + 4
    assert ping["free_gpu_gb"] > budget_gib, (ping, budget_gib)
    emit({"stage": "start", "rows": rows, "file_bytes": Path(args.input).stat().st_size,
          "worker_start_seconds": time.perf_counter() - start, "memory_budget_gib": budget_gib,
          "cuda_free_gib": ping["free_gpu_gb"],
          "scope": "Full rows, same two-column Parquet and three queries; no LLM; no cache flush"})
    steps = [{"op": "summary", "columns": "revenue"},
             {"op": "outliers", "columns": "revenue", "top_k": 3},
             {"op": "groupby", "by": "region", "agg": "revenue:sum", "top_k": 3}]
    modes = ("subprocess", "warm_worker", "batch")
    times = {(engine, mode): [] for engine in ("pandas", "cudf") for mode in modes}
    expected = None
    # Reverse the second repetition to reduce simple ordering bias.
    cases = [(engine, mode) for engine in ("pandas", "cudf") for mode in modes]
    for engine, mode in cases + list(reversed(cases)):
        gpu = engine == "cudf"
        os.environ["GPU_ANALYSIS_PERSISTENT_WORKER"] = "0" if mode == "subprocess" else "1"
        start = time.perf_counter()
        if mode == "batch":
            reply = skills._worker_call({"cmd": "batch", "path": args.input, "steps": steps,
                                        "force_cpu": not gpu, "force_gpu": gpu})
            assert reply["ok"] and reply["engine"] == engine and reply["rows"] == rows, reply
            payloads = reply["results"]
            trace = {"load_seconds": reply["load_seconds"], "total_seconds": reply["total_seconds"],
                     "statistics_reused": payloads[1]["outliers"].get("statistics_reused_columns")}
        else:
            payloads = []
            for step in steps:
                cmd = [sys.executable, str(ROOT / "skills/cudf-analytics/scripts/gpu_analytics.py"),
                       "--input", args.input, "--op", step["op"],
                       "--force-gpu" if gpu else "--force-cpu"]
                for key in ("columns", "by", "agg", "top_k"):
                    if key in step:
                        cmd.extend(["--" + key.replace("_", "-"), str(step[key])])
                proc = skills._run_analysis(cmd)
                assert proc.returncode == 0, proc.stderr
                payloads.append(json.loads(proc.stdout))
            trace = [payload.get("phase_timings") for payload in payloads]
        elapsed = time.perf_counter() - start
        for payload in payloads:
            assert payload["ok"] and payload["engine"] == engine, payload
            assert skills._rows_of(payload) == rows, payload
        outputs = [values(payload) for payload in payloads]
        if expected is None:
            expected = outputs
        if not equal(outputs, expected):
            emit({"stage": "mismatch", "engine": engine, "mode": mode,
                  "expected": expected, "actual": outputs})
            raise AssertionError("Matched full-data results differ; no speedup claim allowed")
        times[(engine, mode)].append(elapsed)
        emit({"stage": "measurement", "engine": engine, "mode": mode,
              "seconds": elapsed, "rows": rows, "results_agree": True, "trace": trace})
    medians = {engine: {mode: statistics.median(times[(engine, mode)]) for mode in modes}
               for engine in ("pandas", "cudf")}
    state = skills._worker_call({"cmd": "list"})
    assert state["count"] == 0 and state["warm_cache_count"] == 0, state
    assert skills._file_identity(args.input) == identity
    emit({"stage": "complete", "rows": rows, "median_seconds": medians,
          "cpu_over_gpu": {mode: medians["pandas"][mode] / medians["cudf"][mode] for mode in modes},
          "gpu_subprocess_over_batch": medians["cudf"]["subprocess"] / medians["cudf"]["batch"],
          "results_agree": True, "sources_unchanged": True, "active_sessions": 0,
          "reference_results": expected})


if __name__ == "__main__":
    try:
        main()
    finally:
        skills._worker_stop(skills._worker)
