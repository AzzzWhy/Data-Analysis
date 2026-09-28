"""Explicitly calibrate one compatible fixed-report queue for *full cold CLI* latency.

The script benchmarks all eligible paths with fresh queue/worker processes,
checks complete report parity, and saves a machine/file/workflow-bound
reports_cold profile. Normal report requests never run this benchmark.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import statistics
import subprocess
import sys
import time
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "agent"))
sys.path.insert(0, str(ROOT / "skills/cudf-analytics/scripts"))
import batch_job
import batch_queue
from fast_execution_benchmark import equal, values
import gpu_analytics as ga
import gpu_session
import hybrid_execution as hybrid

ROUTES = ("cpu", "native", "cpu_gpu")
ACTUAL = {"cpu": "cpu", "native": "native_gpu", "cpu_gpu": "cpu_gpu"}


def load_group(manifest):
    jobs = batch_queue.load_manifest(manifest)
    plans = [batch_job.load_plan(job.plan_path) for job in jobs]
    sources = {plan["file_path"] for plan in plans}
    if len(sources) != 1:
        raise ValueError("cold calibration requires plans for one file")
    if any(plan.get("force_cpu", False) or plan.get("force_gpu", False)
           or plan.get("load_backend", "auto") != "auto" for plan in plans):
        raise ValueError("cold calibration requires auto plans without forced routes")
    steps = [step for plan in plans for step in plan["steps"]]
    workflow = gpu_session.batch_workflow(steps)
    columns = set()
    for step in steps:
        required = ga.required_read_columns(step["op"], gpu_session._build_args(step))
        if required is None:
            columns = None
            break
        columns.update(required)
    return jobs, plans, next(iter(sources)), columns, workflow


def prepare_manifests(folder, jobs, plans, profile):
    manifests = {}
    for route in (*ROUTES, "auto"):
        entries = []
        route_dir = folder / route
        route_dir.mkdir(parents=True, exist_ok=False)
        for job, plan in zip(jobs, plans):
            variant = {**plan,
                "force_cpu": route == "cpu", "force_gpu": route == "native",
                "load_backend": "cpu_gpu" if route == "cpu_gpu" else "auto" if route == "auto" else "native"}
            if route == "auto":
                variant["hybrid_profile"] = str(profile)
            else:
                variant.pop("hybrid_profile", None)
            plan_file = route_dir / f"{job.index:02d}-{job.name}.json"
            plan_file.write_text(json.dumps(variant, ensure_ascii=False), encoding="utf-8")
            entries.append({"name": job.name, "plan": str(plan_file)})
        manifest = route_dir / "manifest.json"
        manifest.write_text(json.dumps({"jobs": entries}), encoding="utf-8")
        manifests[route] = manifest
    return manifests


def run_cold(manifest, output_root, route):
    started = time.perf_counter()
    proc = subprocess.run([sys.executable, str(ROOT / "agent/batch_queue.py"),
                           "--manifest", str(manifest), "--output-root", str(output_root)],
                          capture_output=True, text=True, encoding="utf-8", timeout=1800)
    seconds = time.perf_counter() - started
    if proc.returncode:
        raise RuntimeError(f"{route} cold queue failed: {proc.stderr[-2000:]}")
    folder = Path(proc.stdout.strip().splitlines()[-1])
    summary = json.loads((folder / "queue-summary.json").read_text(encoding="utf-8"))
    if not summary["ok"] or summary["groups"] != 1:
        raise RuntimeError("calibration requires one successful shared report transaction")
    reports = [json.loads((Path(job["report_dir"]) / "result.json").read_text(encoding="utf-8"))["result"]
               for job in summary["jobs"]]
    if any(report["loading"]["execution_context"] != "cold" for report in reports):
        raise RuntimeError("cold calibration received a warm worker")
    actual = {report["loading"]["actual"] for report in reports}
    if len(actual) != 1 or (route != "auto" and actual != {ACTUAL[route]}):
        raise RuntimeError(f"{route} requested route did not run: {sorted(actual)}")
    outputs = [[values(step) for step in report["results"]] for report in reports]
    first = reports[0]
    attempts = first["loading"]["attempts"]
    read = (sum(item.get("cpu_read_seconds", 0) for item in attempts)
            if route == "cpu_gpu" else first["loading"]["load_seconds"])
    compute = sum(step["execution_decision"]["observed"]["compute_seconds"]
                  for report in reports for step in report["results"])
    timing = {"seconds": seconds, "read_seconds": read, "compute_seconds": compute}
    if any(not isinstance(value, (float, int)) or not math.isfinite(value) or value < 0
           for value in timing.values()):
        raise RuntimeError("non-finite or negative timing")
    return outputs, timing, first["loading"]


def calibrate(manifest, profile, output_root, repeats):
    jobs, plans, source, columns, workflow = load_group(manifest)
    if hybrid.preflight(source, columns, workflow, "cpu_gpu"):
        raise ValueError("CPU-to-GPU path is not eligible for this report schema")
    source_before = hybrid.identity(source)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    folder = output_root / f"reports-cold-{stamp}-{uuid4().hex[:8]}"
    folder.mkdir(parents=True, exist_ok=False)
    manifests = prepare_manifests(folder, jobs, plans, profile)
    samples = {route: [] for route in ROUTES}
    reference = None
    for repeat in range(-1, repeats):
        order = ROUTES if repeat % 2 == 0 else tuple(reversed(ROUTES))
        for route in order:
            outputs, timing, _ = run_cold(manifests[route], folder / "runs" / route, route)
            if reference is None:
                reference = outputs
            elif not equal(outputs, reference):
                raise AssertionError(f"{route} report results disagree")
            if repeat >= 0:
                samples[route].append(timing)
        print(json.dumps({"stage": "cold_calibration_round", "repeat": repeat}), flush=True)
    if hybrid.identity(source) != source_before:
        raise AssertionError("source changed during calibration")
    hybrid.save_measurement(str(profile), source, columns, workflow, "reports_cold", samples)
    selected, decision = hybrid.choose(str(profile), source, columns, workflow, "reports_cold")
    if selected not in ROUTES:
        raise RuntimeError("saved cold profile did not match this machine and report workflow")
    auto_outputs, auto_timing, loading = run_cold(manifests["auto"], folder / "runs" / "auto", "auto")
    if not equal(auto_outputs, reference) or loading["actual"] != ACTUAL[selected]:
        raise AssertionError("calibrated auto result or actual route disagrees")
    if loading["decision"].get("selected") != selected:
        raise AssertionError("auto did not use the cold profile")
    if hybrid.identity(source) != source_before:
        raise AssertionError("source changed during auto verification")
    record = {"repeats": repeats, "reports": len(jobs), "context": "reports_cold",
              "medians": {route: statistics.median(row["seconds"] for row in runs)
                          for route, runs in samples.items()},
              "samples": samples, "selected": selected, "confidence": decision["confidence"],
              "auto_verify_seconds": auto_timing["seconds"], "auto_actual": loading["actual"],
              "results_agree": True, "sources_unchanged": True,
              "profile": str(profile), "calibrated_manifest": str(manifests["auto"]),
              "scope": "full cold queue CLI plus workers and report I/O; OS cache retained; no LLM"}
    evidence = folder / "calibration-result.json"
    evidence.write_text(json.dumps(record, ensure_ascii=False, allow_nan=False) + "\n", encoding="utf-8")
    print(json.dumps({"stage": "calibrated", "selected": selected,
                      "medians": record["medians"], "auto_actual": record["auto_actual"],
                      "manifest": record["calibrated_manifest"], "evidence": str(evidence)}), flush=True)
    return record


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True,
                        help="one compatible auto fixed-report queue manifest")
    parser.add_argument("--profile", type=Path, required=True,
                        help="local calibration file; do not upload it")
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--repeats", type=int, default=5)
    args = parser.parse_args()
    if not 3 <= args.repeats <= 20:
        parser.error("repeats must be 3 to 20")
    args.output_root.mkdir(parents=True, exist_ok=True)
    calibrate(args.manifest.resolve(), args.profile.resolve(), args.output_root.resolve(), args.repeats)


if __name__ == "__main__":
    main()
