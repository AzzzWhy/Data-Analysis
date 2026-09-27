"""Same exact queries: subprocess vs warm worker vs one-load batch on GB10."""
import argparse
import json
import math
import os
from pathlib import Path
import statistics
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "agent"))
import skills


def values(reply):
    op = reply["op"]
    block = reply[op]
    if op == "groupby":
        return block["top_k"]
    if op == "summary":
        return block["stats"]
    return block["results"]


def equal(a, b):
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(equal(a[k], b[k]) for k in a)
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(equal(x, y) for x, y in zip(a, b))
    if isinstance(a, (float, int)) and isinstance(b, (float, int)):
        return math.isclose(a, b, rel_tol=1e-9, abs_tol=1e-9)
    return a == b


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--small", required=True)
    parser.add_argument("--large", required=True)
    opts = parser.parse_args()
    identities = {p: skills._file_identity(p) for p in (opts.small, opts.large)}
    os.environ["SKILL_SHOW_SPEEDUP"] = "0"
    startup = time.perf_counter()
    ping = skills._worker_call({"cmd": "ping"})
    assert ping["ok"] and ping["engine"] == "cudf", ping
    print(json.dumps({"stage": "worker_start", "seconds": time.perf_counter() - startup,
                      "engine": ping["engine"]}), flush=True)
    with tempfile.TemporaryDirectory() as folder:
        tiny = Path(folder) / "tiny.csv"
        tiny.write_text("region,revenue\n" + "A,1\nB,3\n" * 10000, encoding="utf-8")
        for label, path, gpu in (("20k", str(tiny), False),
                                 ("5M", opts.small, True), ("20M", opts.large, True)):
            steps = [{"op": "summary", "columns": "revenue"},
                     {"op": "outliers", "columns": "revenue", "top_k": 3},
                     {"op": "groupby", "by": "region", "agg": "revenue:sum", "top_k": 3}]
            times = {m: [] for m in ("subprocess", "warm_worker", "batch")}
            expected = None
            traces = {}
            for mode in ("subprocess", "warm_worker", "batch", "batch", "warm_worker", "subprocess"):
                os.environ["GPU_ANALYSIS_PERSISTENT_WORKER"] = "0" if mode == "subprocess" else "1"
                start = time.perf_counter()
                if mode == "batch":
                    reply = skills._worker_call({"cmd": "batch", "path": path, "steps": steps,
                                                "force_cpu": not gpu, "force_gpu": gpu})
                    if not reply["ok"]:
                        print(json.dumps({"case": label, "mode": mode, "refused": reply}), flush=True)
                        continue
                    outputs = [values(r) for r in reply["results"]]
                    traces[mode] = {"load_seconds": reply["load_seconds"],
                        "statistics_reused": reply["results"][1]["outliers"].get("statistics_reused_columns")}
                else:
                    outputs = []
                    phase_list = []
                    for step in steps:
                        cmd = [sys.executable, str(ROOT / "skills/cudf-analytics/scripts/gpu_analytics.py"),
                               "--input", path, "--op", step["op"],
                               "--force-gpu" if gpu else "--force-cpu"]
                        for k in ("columns", "by", "agg", "top_k"):
                            if k in step:
                                cmd.extend(["--" + k.replace("_", "-"), str(step[k])])
                        proc = skills._run_analysis(cmd)
                        payload = json.loads(proc.stdout)
                        assert proc.returncode == 0 and payload["ok"], payload
                        assert payload["engine"] == ("cudf" if gpu else "pandas"), payload
                        outputs.append(values(payload))
                        phase_list.append(payload.get("phase_timings"))
                    traces[mode] = phase_list
                times[mode].append(time.perf_counter() - start)
                if expected is None:
                    expected = outputs
                assert equal(outputs, expected), f"results disagree: {label}/{mode}"
            medians = {mode: statistics.median(runs) for mode, runs in times.items() if runs}
            print(json.dumps({"case": label, "engine": "cudf" if gpu else "pandas",
                "times": times, "median_seconds": medians, "traces": traces,
                "subprocess_over_worker": medians["subprocess"] / medians["warm_worker"],
                "subprocess_over_batch": medians["subprocess"] / medians["batch"] if "batch" in medians else None,
                "results_agree": True, "scope": "3 analyses; worker first startup reported separately; no LLM"}), flush=True)
    state = skills._worker_call({"cmd": "list"})
    assert state["count"] == 0 and state["warm_cache_count"] == 0, state
    assert all(skills._file_identity(p) == identity for p, identity in identities.items())
    print(json.dumps({"stage": "complete", "sources_unchanged": True, "active_sessions": 0}), flush=True)


if __name__ == "__main__":
    try:
        main()
    finally:
        skills._worker_stop(skills._worker)
