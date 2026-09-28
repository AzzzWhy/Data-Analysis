"""Reproduce full-data diversity timings and verified automatic routing.

Use prepared, unmodified UCI files listed in docs/PUBLIC_DATA_REPAIR.md.
Never downloads, cleans, samples or rewrites an input. No model/API is invoked.
"""
import argparse
import json
import os
from pathlib import Path
import statistics
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "agent"), str(ROOT / "skills/cudf-analytics/scripts")]
import skills
import gpu_analytics as ga
import hybrid_execution as hybrid
from fast_execution_benchmark import equal, values


def workloads():
    terrain = "Elevation,Aspect,Slope,Horizontal_Distance_To_Hydrology,Vertical_Distance_To_Hydrology,Horizontal_Distance_To_Roadways,Hillshade_9am,Hillshade_Noon,Hillshade_3pm,Horizontal_Distance_To_Fire_Points"
    wine = [{"op": "summary", "columns": "alcohol"},
            {"op": "outliers", "columns": "alcohol", "top_k": 3},
            {"op": "groupby", "by": "quality", "agg": "alcohol:mean", "top_k": 3}]
    forest_group = {"op": "groupby", "by": "Cover_Type", "agg": "Elevation:mean|Slope:mean", "top_k": 3}
    retail = [{"op": "summary", "columns": "Quantity,UnitPrice"},
              {"op": "outliers", "columns": "UnitPrice", "top_k": 3}]
    all_forest = terrain + "," + ",".join([f"Wilderness_{i}" for i in range(1, 5)] +
                      [f"Soil_{i}" for i in range(1, 41)] + ["Cover_Type"])
    return {
        "wine_numeric": ("wine.parquet", 4898, wine),
        "wine_comma_csv": ("wine-comma.csv", 4898, wine),
        "wine_semicolon_csv": ("wine-original.csv", 4898, wine),
        "forest_wide": ("forest.parquet", 581012, [
            {"op": "summary", "columns": terrain}, {"op": "corr", "columns": terrain}, forest_group]),
        "forest_all55": ("forest.parquet", 581012, [
            {"op": "summary", "columns": all_forest},
            {"op": "outliers", "columns": "Elevation", "top_k": 3}, forest_group]),
        "power_numeric": ("power.parquet", 2075259, [
            {"op": "summary", "columns": "Global_active_power,Global_reactive_power,Voltage"},
            {"op": "outliers", "columns": "Global_active_power", "top_k": 3},
            {"op": "corr", "columns": "Global_active_power,Global_reactive_power,Voltage"}]),
        "power_date_string": ("power.parquet", 2075259, [
            {"op": "summary", "columns": "Global_active_power"},
            {"op": "outliers", "columns": "Global_active_power", "top_k": 3},
            {"op": "groupby", "by": "Date", "agg": "Global_active_power:sum", "top_k": 3}]),
        "retail_numeric": ("retail.parquet", 541909, retail + [
            {"op": "groupby", "by": "CustomerID", "agg": "Quantity:sum", "top_k": 3}]),
        "retail_country": ("retail.parquet", 541909, retail + [
            {"op": "groupby", "by": "Country", "agg": "Quantity:sum|UnitPrice:mean", "top_k": 3}]),
        "retail_timestamp": ("retail.parquet", 541909, retail + [
            {"op": "groupby", "by": "InvoiceDate", "agg": "Quantity:sum", "top_k": 3}]),
        "retail_excel": ("retail-original.xlsx", 541909, retail + [
            {"op": "groupby", "by": "CustomerID", "agg": "Quantity:sum", "top_k": 3}]),
    }


def emit(item):
    print(json.dumps(item, allow_nan=False), flush=True)


def argv_for(path, step, backend, profile):
    argv = ["--input", str(path), "--op", step["op"], "--load-backend",
            "auto" if backend == "auto" else "cpu_gpu" if backend == "cpu_gpu" else "native"]
    if backend != "auto":
        argv.append("--force-cpu" if backend == "cpu" else "--force-gpu")
    elif profile:
        argv += ["--hybrid-profile", str(profile)]
    for key in ("columns", "by", "agg", "top_k", "method"):
        if key in step:
            argv += ["--" + key.replace("_", "-"), str(step[key])]
    return argv


def run_case(opts):
    filename, rows, steps = workloads()[opts.case]
    path = Path(opts.dataset_root).resolve() / filename
    before = hybrid.identity(path)
    os.environ["SKILL_SHOW_SPEEDUP"] = "0"
    os.environ.pop("GPU_ANALYSIS_HYBRID_PROFILE", None)
    records = {backend: [] for backend in hybrid.PATHS}
    expected = None

    def run(backend):
        started = time.perf_counter()
        if opts.mode == "batch":
            request = {"cmd": "batch", "path": str(path), "steps": steps,
                       "load_backend": "auto" if backend == "auto" else
                                       "cpu_gpu" if backend == "cpu_gpu" else "native"}
            if backend != "auto":
                request.update(force_cpu=backend == "cpu", force_gpu=backend != "cpu")
            elif opts.profile:
                request["hybrid_profile"] = opts.profile
            reply = skills._worker_call(request)
            assert reply["ok"], reply
            results = reply["results"]
            load = reply["loading"]
            attempts = load["attempts"]
            samples = [{"seconds": time.perf_counter() - started,
                        "read_seconds": sum(a.get("cpu_read_seconds", 0) for a in attempts)
                            if load["actual"] == "cpu_gpu" else load["load_seconds"],
                        "compute_seconds": sum(r["execution_decision"]["observed"]["compute_seconds"] for r in results)}]
            loads, decisions = [load["actual"]], [load.get("decision")]
            counts = [{"reads": sum(a.get("read_count", 0) for a in attempts),
                       "conversions": sum(a.get("conversion_count", 0) for a in attempts)}]
        else:
            results, samples, loads, decisions, counts = [], [], [], [], []
            for step in steps:
                begin = time.perf_counter()
                reply = skills._worker_call({"cmd": "oneshot", "argv": argv_for(path, step, backend, opts.profile)})
                assert reply["ok"] and reply["exit_code"] == 0, reply
                result = reply["payload"]
                load = result["loading"]
                attempts = load["attempts"]
                samples.append({"seconds": time.perf_counter() - begin,
                    "read_seconds": sum(a.get("cpu_read_seconds", 0) for a in attempts)
                        if load["actual"] == "cpu_gpu" else result["phase_timings"]["load_seconds"],
                    "compute_seconds": result["phase_timings"]["compute_seconds"]})
                loads.append(load["actual"])
                decisions.append(result["execution_decision"]["policy"])
                counts.append({"reads": sum(a.get("read_count", 0) for a in attempts),
                               "conversions": sum(a.get("conversion_count", 0) for a in attempts)})
                results.append(result)
        assert all(r.get("ok") and skills._rows_of(r) == rows for r in results), results
        return [values(r) for r in results], {
            "seconds": time.perf_counter() - started,
            "engines": [r["engine"] for r in results], "loading": loads,
            "counts": counts, "samples": samples, "decisions": decisions,
            "fallbacks": [r.get("fallback_reason") or r.get("execution_decision", {}).get("fallback_reason") for r in results]}

    try:
        start = time.perf_counter()
        ping = skills._worker_call({"cmd": "ping"})
        assert ping["ok"] and ping["engine"] == "cudf", ping
        emit({"stage": "startup", "case": opts.case, "mode": opts.mode,
              "seconds": time.perf_counter() - start})
        for repeat in range(-1, opts.repeats):
            for backend in (hybrid.PATHS if repeat % 2 == 0 else tuple(reversed(hybrid.PATHS))):
                output, timing = run(backend)
                expected = output if expected is None else expected
                assert equal(output, expected), (opts.case, backend, "result mismatch", output, expected)
                emit({"stage": "warmup" if repeat < 0 else "measurement", "case": opts.case,
                      "mode": opts.mode, "backend": backend, "repeat": repeat,
                      "rows": rows, "results_agree": True, **timing})
                if repeat >= 0:
                    records[backend].append(timing)
        calibrated = []
        if opts.profile and path.suffix == ".parquet" and opts.repeats >= 2:
            workflows = [steps] if opts.mode == "batch" else [[step] for step in steps]
            for index, workflow in enumerate(workflows):
                measurements = {}
                for backend in hybrid.PATHS:
                    runs = records[backend]
                    indices = range(len(steps)) if opts.mode == "batch" else [index]
                    expected_engine = "pandas" if backend == "cpu" else "cudf"
                    expected_load = {"cpu": "cpu", "native": "native_gpu", "cpu_gpu": "cpu_gpu"}[backend]
                    load_index = 0 if opts.mode == "batch" else index
                    if all(all(r["engines"][i] == expected_engine for i in indices)
                           and r["loading"][load_index] == expected_load
                           and not any(r["fallbacks"][i] for i in indices) for r in runs):
                        measurements[backend] = [r["samples"][load_index] for r in runs]
                if not {"cpu", "native"} <= measurements.keys():
                    continue
                if opts.mode == "batch":
                    import gpu_session as session
                    signature = session.batch_workflow(steps)
                    columns = set()
                    for step in steps:
                        needed = ga.required_read_columns(step["op"], session._build_args(step))
                        if needed is None:
                            columns = None
                            break
                        columns.update(needed)
                else:
                    args = ga.build_parser().parse_args(argv_for(path, workflow[0], "cpu", None))
                    signature = hybrid.single_workflow(args)
                    columns = ga.required_read_columns(args.op, args)
                context = "batch_warm" if opts.mode == "batch" else "oneshot_warm"
                hybrid.save_measurement(opts.profile, str(path), columns, signature, context, measurements)
                selected, note = hybrid.choose(opts.profile, str(path), columns, signature, context)
                assert selected, note
                calibrated.append({"step": index, "selected": selected, "candidates": sorted(measurements)})
        output, automatic = run("auto")
        assert equal(output, expected), "automatic route changed results"
        for item in calibrated:
            index = item["step"]
            actual = automatic["loading"][0 if opts.mode == "batch" else index]
            assert actual == {"cpu": "cpu", "native": "native_gpu", "cpu_gpu": "cpu_gpu"}[item["selected"]], (item, actual)
        state = skills._worker_call({"cmd": "list"})
        assert state["count"] == 0 and state["warm_cache_count"] == 0, state
        assert hybrid.identity(path) == before, "source changed"
        emit({"stage": "summary", "case": opts.case, "mode": opts.mode, "rows": rows,
              "median_seconds": {k: statistics.median(r["seconds"] for r in runs) for k, runs in records.items()},
              "engines": {k: runs[0]["engines"] for k, runs in records.items()},
              "loading": {k: runs[0]["loading"] for k, runs in records.items()},
              "automatic": automatic, "calibrated": calibrated,
              "source_unchanged": True, "results_agree": True, "active_sessions": 0})
    finally:
        skills._worker_stop(skills._worker)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset-root", required=True)
    parser.add_argument("--mode", choices=["warm", "batch"], required=True)
    parser.add_argument("--case", choices=list(workloads()))
    parser.add_argument("--include-excel", action="store_true", help="full XLSX parsing makes repeated warm tests slow")
    parser.add_argument("--repeats", type=int, default=5)
    parser.add_argument("--profile", help="optional local calibration output; never commit this file")
    opts = parser.parse_args()
    if not 1 <= opts.repeats <= 20:
        parser.error("repeats must be 1-20")
    if opts.case:
        run_case(opts)
        return
    failures = []
    cases = [name for name in workloads() if opts.include_excel or name != "retail_excel"]
    for name in cases:
        result = subprocess.run([sys.executable, __file__, *sys.argv[1:], "--case", name], timeout=3600)
        if result.returncode:
            failures.append(name)
    emit({"stage": "suite_complete", "mode": opts.mode, "cases": len(cases),
          "repeats": opts.repeats, "failures": failures})
    if failures:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
