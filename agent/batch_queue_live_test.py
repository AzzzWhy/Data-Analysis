"""Real-device smoke for multi-process plans and a separate warm Agent session."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys
from uuid import uuid4

import batch_queue
import skills

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "benchmark"))
from fast_execution_benchmark import equal, values


STEPS = [
    {"op": "summary", "columns": "revenue"},
    {"op": "groupby", "by": "region", "agg": "revenue:sum", "top_k": 3},
]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True,
                        help="existing numeric Parquet with region and revenue")
    parser.add_argument("--output-root", type=Path, required=True)
    args = parser.parse_args(argv)
    source = args.input.resolve()
    if not source.is_file() or source.suffix.lower() != ".parquet":
        parser.error("input must be an existing Parquet file")
    before = (source.stat().st_size, source.stat().st_mtime_ns)
    output_root = args.output_root.resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    fixture = output_root / f"live-fixture-{uuid4().hex[:8]}"
    fixture.mkdir(parents=True, exist_ok=False)
    names = ("cpu_a", "cpu_b", "hybrid_a", "hybrid_b")
    for name in names:
        (fixture / f"{name}.json").write_text(json.dumps({
            "title": name, "file_path": str(source),
            "force_cpu": name.startswith("cpu_"),
            "load_backend": "cpu_gpu" if name.startswith("hybrid_") else "native",
            "steps": STEPS,
        }), encoding="utf-8")
    manifest = fixture / "queue.json"
    manifest.write_text(json.dumps({"jobs": [
        {"name": name, "plan": f"{name}.json"} for name in names
    ]}), encoding="utf-8")
    # Legacy isolation is still explicitly testable. Optimized reuse and
    # same-file grouping have their own exact-parity benchmark and unit tests.
    run_dir, summary = batch_queue.run(manifest, output_root / "queues", 2, execution_mode="isolated")
    if not summary["ok"]:
        raise AssertionError(summary["jobs"])
    cpu_a, cpu_b, gpu_a, gpu_b = summary["jobs"]
    if len({job["pid"] for job in summary["jobs"]}) != 4:
        raise AssertionError("jobs did not get distinct processes")
    cpu_overlap = (max(cpu_a["started_at_utc"], cpu_b["started_at_utc"]) <
                   min(cpu_a["finished_at_utc"], cpu_b["finished_at_utc"]))
    gpu_serial = (max(cpu_a["finished_at_utc"], cpu_b["finished_at_utc"]) <=
                  gpu_a["started_at_utc"] and
                  gpu_a["finished_at_utc"] <= gpu_b["started_at_utc"])
    if not cpu_overlap or not gpu_serial:
        raise AssertionError("bounded CPU parallelism or GPU exclusivity failed")
    paths = []
    expected_values = None
    expected_rows = None
    for index, job in enumerate(summary["jobs"]):
        result = json.loads((Path(job["report_dir"]) / "result.json").read_text())["result"]
        expected = "cpu" if index < 2 else "cpu_gpu"
        engine = "pandas" if index < 2 else "cudf"
        if result["loading"]["actual"] != expected or any(
                step["engine"] != engine for step in result["results"]):
            raise AssertionError(f"wrong execution path for {job['name']}")
        outputs = [values(step) for step in result["results"]]
        if expected_values is None:
            expected_values = outputs
            expected_rows = result["rows"]
        if not equal(outputs, expected_values) or result["rows"] != expected_rows:
            raise AssertionError(f"CPU/GPU result mismatch for {job['name']}")
        paths.append(result["loading"]["actual"])
    ping = skills._worker_call({"cmd": "ping"})
    if not ping.get("ok"):
        raise AssertionError(f"warm worker startup failed: {ping}")
    # A separate process holds the same host-wide GPU slot. The Agent worker's
    # real open must wait for it, proving queue/Agent arbitration is not merely
    # a local thread lock.
    holder_code = ("from gpu_coordination import gpu_slot\n"
                   "import time\n"
                   "with gpu_slot():\n"
                   " print('locked', flush=True)\n"
                   " time.sleep(1.2)\n")
    holder = subprocess.Popen([sys.executable, "-c", holder_code],
                              cwd=Path(skills._session_script()).parent,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              text=True, encoding="utf-8")
    try:
        if holder.stdout.readline().strip() != "locked":
            raise AssertionError("GPU slot holder did not start")
        opened = skills._worker_call({"cmd": "open", "path": str(source),
                                      "usecols": "region,revenue", "load_backend": "cpu_gpu",
                                      "force_gpu": True, "measure_cpu": False})
        if holder.wait(timeout=5):
            raise AssertionError("GPU slot holder failed")
    finally:
        if holder.poll() is None:
            holder.kill()
            holder.wait(timeout=3)
        holder.stdout.close()
        holder.stderr.close()
    if not opened.get("ok") or opened.get("loading", {}).get("actual") != "cpu_gpu":
        raise AssertionError(f"warm Agent session failed: {opened}")
    slot_wait = opened.get("coordination", {}).get("gpu_slot_wait_seconds", 0)
    if slot_wait < 0.4:
        raise AssertionError(f"warm Agent did not wait for the shared GPU slot: {slot_wait}")
    sid = opened["session_id"]
    try:
        replies = [skills._worker_call({"cmd": "analyze", "sid": sid, **step})
                   for step in STEPS]
        if any(not item.get("ok") or item.get("engine") != "cudf" for item in replies):
            raise AssertionError(f"warm session step failed: {replies}")
        if not equal([values(step) for step in replies], expected_values):
            raise AssertionError("warm session and batch results disagree")
    finally:
        closed = skills._worker_call({"cmd": "close", "sid": sid, "retain": False})
        if not closed.get("ok"):
            raise AssertionError(f"warm session close failed: {closed}")
    state = skills._worker_call({"cmd": "list"})
    if state.get("count") or state.get("warm_cache_count"):
        raise AssertionError(f"warm session leaked data: {state}")
    skills._worker_stop(skills._worker)
    if (source.stat().st_size, source.stat().st_mtime_ns) != before:
        raise AssertionError("input changed")
    evidence = {"ok": True, "rows": expected_rows,
                      "queue_seconds": summary["seconds"],
                      "cpu_processes_overlap": cpu_overlap, "gpu_jobs_serial": gpu_serial,
                      "job_pids": [job["pid"] for job in summary["jobs"]],
                      "load_paths": paths, "warm_session_backend": "cpu_gpu",
                      "warm_gpu_slot_wait_seconds": slot_wait,
                      "warm_session_steps": len(replies), "active_sessions": 0,
                      "results_agree": True, "source_unchanged": True}
    (fixture / "live-evidence.json").write_text(json.dumps(evidence) + "\n", encoding="utf-8")
    print(json.dumps({**evidence, "queue_dir": str(run_dir),
                      "evidence_file": str(fixture / "live-evidence.json")}), flush=True)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    finally:
        skills._worker_stop(skills._worker)
