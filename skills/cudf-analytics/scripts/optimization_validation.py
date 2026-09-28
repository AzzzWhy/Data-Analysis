"""Validate local optimizations on a RAPIDS host, without model/API calls.

The source file is read-only. Derived Parquet files live in a temporary directory
and are deleted after the test. Timings include first conversion where applicable.
"""
import argparse
import importlib.util
import json
import math
import os
import statistics
import sys
import tempfile
import time

import gpu_analytics as ga
import gpu_session as gs
import parquet_cache
import skills


def equal(a, b):
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(equal(a[k], b[k]) for k in a)
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(equal(x, y) for x, y in zip(a, b))
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return math.isclose(a, b, rel_tol=1e-9, abs_tol=1e-9)
    return a == b


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", required=True)
    parser.add_argument("--baseline", required=True)
    parser.add_argument("--resident-source",
                        help="optional smaller GPU-routed source when the host is memory-constrained")
    args = parser.parse_args()
    spec = importlib.util.spec_from_file_location("original_analytics", args.baseline)
    original = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = original
    spec.loader.exec_module(original)
    gpu = ga.detect_engine()
    assert gpu.is_gpu, gpu.reason
    before = gs._file_identity(args.source)
    frame = gpu.read(args.source)
    query = ga.build_parser().parse_args([
        "--input", args.source, "--op", "groupby", "--by", "region",
        "--agg", "revenue:sum,mean", "--top-k", "3"])
    projected = gpu.read(args.source, usecols=ga.required_read_columns("groupby", query))
    assert equal(ga.op_groupby(frame, gpu, query), ga.op_groupby(projected, gpu, query))
    del projected
    query.select = "revenue"
    times = {"original": [], "optimized": []}
    outputs = {}
    for label in ("original", "optimized", "optimized", "original", "original", "optimized"):
        ga.sync_device(gpu)
        started = time.perf_counter()
        module = original if label == "original" else ga
        outputs[label] = module.op_outliers(frame, gpu, query)
        ga.sync_device(gpu)
        times[label].append(time.perf_counter() - started)
    assert equal(outputs["original"], outputs["optimized"]), "outlier results changed"
    print(json.dumps({"stage": "outliers_full_operation", "rows": len(frame),
                      "seconds": times, "medians": {k: statistics.median(v)
                      for k, v in times.items()}, "results_agree": True}), flush=True)
    del frame

    with tempfile.TemporaryDirectory(prefix="gda-parquet-validation-") as cache:
        first, built = parquet_cache.read(gpu, args.source, cache, usecols=["region", "revenue"])
        second, hit = parquet_cache.read(gpu, args.source, cache, usecols=["region", "revenue"])
        assert (built["status"], hit["status"]) == ("built", "hit")
        assert equal(ga.op_groupby(first, gpu, query), ga.op_groupby(second, gpu, query))
        print(json.dumps({"stage": "parquet", "first_conversion": built,
                          "projected_hit": hit, "results_agree": True}), flush=True)
        del first, second

    # Exercise the actual agent helper against the updated worker in-process.
    # No source files are deployed and no model credentials are used.
    def call(req):
        return gs.handle(req)
    skills._worker_call = call
    skills._remember_last_file = lambda _path: None
    out = []
    resident_source = args.resident_source or args.source
    try:
        for _ in range(2):
            response = skills._resident_single_analysis(
                resident_source, "groupby", "region", "revenue:sum,mean", None, 3)
            if response is None:
                raise RuntimeError("resident open refused; choose --resident-source with "
                                   "sufficient memory headroom (stateless fallback remains available)")
            out.append(json.loads(response))
        assert all(item["success"] and item["engine"] == "cudf" for item in out)
        assert not out[0]["resident_reuse"] and out[1]["resident_reuse"]
        # Timing fields describe each execution, not the statistical answer.
        stats = [{k: value for k, value in item["result"]["groupby"].items()
                  if k != "compute_seconds"} for item in out]
        assert equal(stats[0], stats[1])
        assert gs.do_list({})["count"] == 0
        print(json.dumps({"stage": "interactive_reuse", "seconds": [x["seconds"] for x in out],
                          "warm_hits": [x["resident_reuse"] for x in out],
                          "results_agree": True, "active_sessions": 0}), flush=True)
    finally:
        gs.do_close({"sid": "all"})
    assert gs._file_identity(args.source) == before, "source changed"
    print("PASS: projection, full-operation statistics, Parquet and cross-question reuse", flush=True)


if __name__ == "__main__":
    main()
