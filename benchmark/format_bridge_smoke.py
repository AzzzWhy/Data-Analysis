"""Full original XLSX workflow parity against its verified Parquet representation.

One integration run, not a repeated speed benchmark. Inputs remain read-only.
"""
import argparse
import json
import os
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "agent"), str(ROOT / "skills/cudf-analytics/scripts")]
import skills
import hybrid_execution as hybrid
from public_data_regression import argv_for, workloads
from fast_execution_benchmark import equal, values


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset-root", required=True)
    parser.add_argument("--mode", choices=["warm", "batch"], required=True)
    opts = parser.parse_args()
    folder = Path(opts.dataset_root).resolve()
    original, reference = folder / "retail-original.xlsx", folder / "retail.parquet"
    identities = [hybrid.identity(p) for p in (original, reference)]
    _, rows, steps = workloads()["retail_excel"]
    os.environ["SKILL_SHOW_SPEEDUP"] = "0"
    os.environ.pop("GPU_ANALYSIS_HYBRID_PROFILE", None)
    try:
        baseline = []
        for step in steps:
            reply = skills._worker_call({"cmd": "oneshot", "argv": argv_for(reference, step, "cpu", None)})
            assert reply["ok"] and reply["exit_code"] == 0, reply
            assert skills._rows_of(reply["payload"]) == rows
            baseline.append(values(reply["payload"]))
        start = time.perf_counter()
        if opts.mode == "batch":
            reply = skills._worker_call({"cmd": "batch", "path": str(original), "steps": steps,
                                         "force_gpu": True, "load_backend": "cpu_gpu"})
            assert reply["ok"], reply
            results, loads = reply["results"], [reply["loading"]]
        else:
            results, loads = [], []
            for step in steps:
                reply = skills._worker_call({"cmd": "oneshot", "argv": argv_for(original, step, "cpu_gpu", None)})
                assert reply["ok"] and reply["exit_code"] == 0, reply
                results.append(reply["payload"])
                loads.append(reply["payload"]["loading"])
        elapsed = time.perf_counter() - start
        assert equal([values(r) for r in results], baseline), "Excel workflow differs from verified reference"
        assert all(r["engine"] == "cudf" and skills._rows_of(r) == rows for r in results)
        for load in loads:
            assert load["actual"] == "cpu_gpu", load
            assert sum(a["read_count"] for a in load["attempts"]) == 1, load
            assert sum(a["conversion_count"] for a in load["attempts"]) == 1, load
        state = skills._worker_call({"cmd": "list"})
        assert state["count"] == 0 and state["warm_cache_count"] == 0, state
        assert identities == [hybrid.identity(p) for p in (original, reference)]
        print(json.dumps({"mode": opts.mode, "rows": rows, "steps": 3,
                          "results_agree": True, "engines": [r["engine"] for r in results],
                          "reads": len(loads), "conversions": len(loads),
                          "seconds_single_run": elapsed,
                          "sources_unchanged": True, "active_sessions": 0,
                          "scope": "full original XLSX integration, not a speedup claim"}), flush=True)
    finally:
        skills._worker_stop(skills._worker)


if __name__ == "__main__":
    main()
