"""Full-row public-data parity and measured routing, without touching GUI.

Run on GB10 after prepare_new_public_data.py. Private profiles stay beside data.
No LLM, sampling, cross-request data cache, OS cache clearing or row replication.
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
sys.path.insert(0, str(ROOT / "skills/cudf-analytics/scripts"))
os.environ["SESSION_WARM_CACHE_MB"] = "0"
os.environ.pop("GPU_ANALYSIS_HYBRID_PROFILE", None)
os.environ["GPU_ANALYTICS_SCRIPT"] = str(ROOT / "skills/cudf-analytics/scripts/gpu_analytics.py")
os.environ["GPU_ANALYTICS_PYTHON"] = sys.executable
from optimized_execution_benchmark import Worker
from fast_execution_benchmark import equal, values
import gpu_session
import hybrid_execution as hybrid


def numeric_steps(columns, by, agg):
    return [{"op": "summary", "columns": ",".join(columns)},
            {"op": "corr", "columns": ",".join(columns[:18])},
            {"op": "outliers", "columns": ",".join(columns[:2]), "top_k": 3},
            {"op": "groupby", "by": by, "agg": agg, "top_k": 5}]


def cases():
    gas = [f"feature_{i:03}" for i in range(1, 129)]
    susy = ["lepton_1_pt", "lepton_1_eta", "lepton_1_phi", "lepton_2_pt",
            "lepton_2_eta", "lepton_2_phi", "missing_energy", "missing_energy_phi",
            "MET_rel", "axial_MET", "M_R", "M_TR_2", "R", "MT2", "S_R",
            "M_Delta_R", "dPhi_r_b", "cos_theta_r1"]
    return [("gas_narrow", "gas-drift.parquet", numeric_steps(gas[:2], "gas_class", "feature_001:mean")),
            ("gas_wide", "gas-drift.parquet", numeric_steps(gas, "gas_class", "feature_001:mean")),
            ("retail_numeric", "retail-ii.parquet", numeric_steps(["Quantity", "Price"], "Quantity", "Price:mean")),
            ("retail_country", "retail-ii.parquet", numeric_steps(["Quantity", "Price"], "Country", "Quantity:sum|Price:mean")),
            ("retail_nullable_id", "retail-ii.parquet", numeric_steps(["Quantity", "Price"], "Customer ID", "Quantity:sum")),
            ("retail_timestamp", "retail-ii.parquet", numeric_steps(["Quantity", "Price"], "InvoiceDate", "Quantity:sum")),
            ("susy_narrow", "susy.parquet", numeric_steps([susy[0], "missing_energy"], "class_label", "lepton_1_pt:mean")),
            ("susy_wide", "susy.parquet", numeric_steps(susy, "class_label", "lepton_1_pt:mean"))]


def inspect(reply, rows, expected=None):
    flat = [step for plan in reply["results"] for step in plan["results"]] if "plans" in reply else reply["results"]
    # batch_many has a list of plan envelopes, rather than an explicit plans key.
    if flat and "results" in flat[0]:
        flat = [step for plan in flat for step in plan["results"]]
    scanned = []
    for step in flat:
        if step.get("result_reused"):
            assert step["rows_scanned"] == 0 and step.get("source_rows") == rows, "invalid reuse provenance"
            assert any(previous["op"] == step["op"] and equal(values(previous), values(step))
                       for previous in scanned), "reuse has no matching full-row result"
        else:
            assert step["rows_scanned"] == rows, "incomplete full-row scan"
            scanned.append(step)
    assert all(not step.get("fallback_reason") for step in flat), "unexpected per-step fallback"
    if expected:
        assert all(step["engine"] == expected for step in flat), "requested engine not used"
    return [values(step) for step in flat], flat


def batch(worker, path, steps, route, profile=None, many=False):
    import pyarrow.parquet as pq
    request = {"cmd": "batch_many" if many else "batch", "path": str(path),
               "steps": steps, "force_cpu": route == "cpu", "force_gpu": route in ("native", "cpu_gpu"),
               "load_backend": "cpu_gpu" if route == "cpu_gpu" else "auto" if route == "auto" else "native"}
    if many:
        request["plans"] = [steps, steps]
    if profile:
        request["hybrid_profile"] = str(profile)
    start = time.perf_counter()
    reply = worker.call(request)
    wall = time.perf_counter() - start
    outputs, flat = inspect(reply, pq.ParquetFile(path).metadata.num_rows,
                            None if route == "auto" else "pandas" if route == "cpu" else "cudf")
    loading = reply["loading"]
    if route != "auto":
        assert loading["actual"] == {"cpu": "cpu", "native": "native_gpu", "cpu_gpu": "cpu_gpu"}[route]
    sample = {"seconds": wall, "read_seconds": loading["load_seconds"] if route != "cpu_gpu" else
              sum(a.get("cpu_read_seconds", 0) for a in loading["attempts"]),
              "conversion_seconds": sum(a.get("conversion_seconds", 0) for a in loading["attempts"]),
              "compute_seconds": sum(s["execution_decision"]["observed"]["compute_seconds"] for s in flat),
              "actual": loading["actual"], "reused_steps": sum(bool(s.get("result_reused")) for s in flat)}
    return outputs, sample, loading.get("decision")


def singles(worker, path, steps, route, independent=False):
    import pyarrow.parquet as pq
    outputs, engines = [], []
    start = time.perf_counter()
    for step in steps:
        if independent:
            cmd = [sys.executable, str(ROOT / "skills/cudf-analytics/scripts/gpu_analytics.py"),
                   "--input", str(path), "--force-cpu", "--op", step["op"]]
            for key in ("columns", "by", "agg", "top_k"):
                if key in step:
                    cmd += ["--" + key.replace("_", "-"), str(step[key])]
            proc = subprocess.run(cmd, text=True, capture_output=True, timeout=600)
            reply = json.loads(proc.stdout)
            assert proc.returncode == 0 and reply["ok"], "independent CPU operation failed"
            assert reply.get("rows_scanned", reply[step["op"]].get("rows_scanned")) == pq.ParquetFile(path).metadata.num_rows
            assert reply["engine"] == "pandas" and not reply.get("fallback_reason")
            outputs.append(values(reply))
            engines.append(reply["engine"])
        else:
            result, sample, _ = batch(worker, path, [step], route)
            outputs += result
            engines.append(sample["actual"])
    return outputs, {"seconds": time.perf_counter() - start, "actual": engines}


def run_case(worker, root, output, name, filename, steps, repeats):
    import pyarrow.parquet as pq
    path = root / filename
    before = hybrid.identity(str(path))
    columns = sorted({column for step in steps for column in
                      gpu_session.GA.required_read_columns(step["op"], gpu_session._build_args(step))})
    workflow = gpu_session.batch_workflow(steps)
    record = {"case": name, "rows": pq.ParquetFile(path).metadata.num_rows, "projected_columns": columns,
              "steps": steps, "samples": {}, "excluded": {}}
    reference, first = singles(worker, path, steps, "cpu", independent=True)
    record["samples"]["independent_cpu"] = [first]
    routes = ["cpu", "native", "cpu_gpu"]
    for route in list(routes):
        reason = hybrid.preflight(str(path), columns, workflow, "cpu_gpu" if route == "cpu_gpu" else "native")
        if reason and route != "cpu":
            record["excluded"][route] = reason
            routes.remove(route)
    profiles = output / "profiles"
    profiles.mkdir(exist_ok=True)
    for many, prefix, context in ((False, "batch", "batch_warm"), (True, "reports", "reports_warm")):
        expected = reference * (2 if many else 1)
        measurements = {route: [] for route in routes}
        # One discarded warm-up per path, then interleaved measured rounds.
        for repeat in range(-1, repeats):
            for route in routes if repeat % 2 == 0 else reversed(routes):
                try:
                    actual, sample, _ = batch(worker, path, steps, route, many=many)
                    if not equal(actual, expected):
                        (output / f"private-mismatch-{name}-{prefix}-{route}.json").write_text(
                            json.dumps({"expected": expected, "actual": actual}), encoding="utf-8")
                        raise AssertionError("full results differ from independently executed CPU")
                    if repeat >= 0:
                        measurements[route].append(sample)
                except Exception as exc:
                    record["excluded"][f"{prefix}_{route}"] = str(exc).replace(str(path), filename)[:500]
                    measurements[route] = []
                    routes = [r for r in routes if r != route]
            print(json.dumps({"stage": "round", "case": name, "mode": prefix, "repeat": repeat}), flush=True)
        for route, samples in measurements.items():
            if samples:
                record["samples"][f"{prefix}_{route}"] = samples
        profile = profiles / f"{name}-{context}.json"
        valid = {k: v for k, v in measurements.items() if len(v) >= 2}
        # Do not certify a fallback or mismatch as an eligible GPU measurement.
        for phase in ("untrained", "calibrated"):
            if phase == "calibrated":
                if not {"cpu", "native"} <= valid.keys():
                    continue
                hybrid.save_measurement(str(profile), str(path), columns,
                                        workflow * (2 if many else 1), context, valid)
            auto_samples = []
            for _ in range(repeats):
                actual, sample, decision = batch(worker, path, steps, "auto",
                    profile if phase == "calibrated" else None, many=many)
                assert equal(actual, expected), "auto results differ"
                auto_samples.append(sample)
            record["samples"][f"{prefix}_auto_{phase}"] = auto_samples
            record[f"{prefix}_auto_{phase}_decision"] = decision
    for route in ("cpu", "native"):
        if route not in routes:
            continue
        samples = []
        for _ in range(repeats):
            actual, sample = singles(worker, path, steps, route)
            assert equal(actual, reference), "warm separate steps differ"
            samples.append(sample)
        record["samples"][f"warm_separate_{route}"] = samples
    for _ in range(repeats - 1):
        actual, sample = singles(worker, path, steps, "cpu", independent=True)
        assert equal(actual, reference), "independent CPU is nondeterministic"
        record["samples"]["independent_cpu"].append(sample)
    record["median_seconds"] = {k: statistics.median(s["seconds"] for s in v)
                                 for k, v in record["samples"].items()}
    record["sources_unchanged"] = before == hybrid.identity(str(path))
    assert record["sources_unchanged"]
    record["verified_equal"] = True
    return record


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--cases", default="")
    parser.add_argument("--repeats", type=int, default=3)
    args = parser.parse_args()
    if not 2 <= args.repeats <= 10:
        parser.error("repeats must be 2-10")
    args.output.mkdir(parents=True, exist_ok=True)
    worker = Worker(ROOT)
    result = {"scope": "full data, four operations; reports run two identical four-operation plans; median wall seconds; no LLM; OS file cache retained; worker startup excluded from warm modes; independent CPU includes four Python startups; preparation excluded",
              "worker_startup_seconds": worker.startup, "repeats": args.repeats, "cases": []}
    try:
        selected = set(args.cases.split(",")) if args.cases else None
        for name, filename, steps in cases():
            if selected and name not in selected:
                continue
            try:
                record = run_case(worker, args.root, args.output, name, filename, steps, args.repeats)
            except Exception as exc:
                record = {"case": name, "verified_equal": False, "error": str(exc).replace(str(args.root), "DATA_ROOT")[:500]}
            result["cases"].append(record)
            (args.output / "results.json").write_text(json.dumps(result, indent=2, allow_nan=False) + "\n", encoding="utf-8")
            print(json.dumps({"stage": "case_complete", **{k: v for k, v in record.items()
                  if k in ("case", "verified_equal", "median_seconds", "excluded", "error")}}), flush=True)
        state = worker.call({"cmd": "list"})
        result["cleanup"] = {k: state[k] for k in ("count", "warm_cache_count")}
        assert not state["count"] and not state["warm_cache_count"]
        (args.output / "results.json").write_text(json.dumps(result, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    finally:
        worker.close()


if __name__ == "__main__":
    main()
