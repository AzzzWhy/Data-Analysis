"""Exact old/new single work and cold/hot fused reporting on existing TLC data."""
import argparse
import json
import os
from pathlib import Path
import queue
import statistics
import subprocess
import sys
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "agent"))
sys.path.insert(0, str(ROOT / "skills/cudf-analytics/scripts"))
from fast_execution_benchmark import equal, values
import batch_queue
import gpu_session
import hybrid_execution

STEPS = [{"op": "summary", "columns": "revenue"},
         {"op": "outliers", "columns": "revenue", "top_k": 3},
         {"op": "groupby", "by": "region", "agg": "revenue:sum", "top_k": 3}]
PLANS = [[{**step, **({"top_k": k} if step["op"] in ("outliers", "groupby") else {})}
          for step in STEPS] for k in (1, 2, 3, 4)]


class Worker:
    def __init__(self, root):
        self.proc = subprocess.Popen([sys.executable, str(root / "skills/cudf-analytics/scripts/gpu_session.py")],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, encoding="utf-8", bufsize=1)
        self.responses = queue.Queue()
        def read():
            for line in self.proc.stdout:
                self.responses.put(line)
            self.responses.put(None)
        threading.Thread(target=read, daemon=True).start()
        start = time.perf_counter()
        self.call({"cmd": "ping"})
        self.startup = time.perf_counter() - start

    def call(self, request):
        self.proc.stdin.write(json.dumps(request) + "\n")
        self.proc.stdin.flush()
        line = self.responses.get(timeout=1800)
        if not line:
            raise RuntimeError("worker exited")
        reply = json.loads(line)
        assert reply.get("ok"), reply
        return reply

    def close(self):
        if self.proc.poll() is None:
            self.call({"cmd": "close", "sid": "all", "retain": False})
            self.proc.stdin.close()
            self.proc.wait(timeout=5)


def measured(worker, source, backend, *, many=False):
    start = time.perf_counter()
    reply = worker.call({"cmd": "batch_many" if many else "batch", "path": str(source),
        "plans": PLANS, "steps": STEPS, "force_cpu": backend == "cpu",
        "force_gpu": backend != "cpu", "load_backend": "cpu_gpu" if backend == "cpu_gpu" else "native"})
    wall = time.perf_counter() - start
    steps = [step for plan in reply["results"] for step in plan["results"]] if many else reply["results"]
    assert all(step["engine"] == ("pandas" if backend == "cpu" else "cudf")
               and not step.get("fallback_reason") for step in steps), "unexpected engine fallback"
    actual = reply["loading"]["actual"]
    assert actual == {"cpu": "cpu", "native": "native_gpu", "cpu_gpu": "cpu_gpu"}[backend], actual
    loading = reply["loading"]
    read = (sum(a.get("cpu_read_seconds", 0) for a in loading["attempts"])
            if backend == "cpu_gpu" else loading["load_seconds"])
    sample = {"seconds": wall, "read_seconds": read,
        "conversion_seconds": sum(a.get("conversion_seconds", 0) for a in loading["attempts"]),
        "compute_seconds": sum(step["execution_decision"]["observed"]["compute_seconds"] for step in steps),
        "result_reused_steps": sum(bool(step.get("result_reused")) for step in steps)}
    return reply, [values(step) for step in steps], sample


def suite(args, source):
    import pyarrow.parquet as pq
    rows = pq.ParquetFile(source).metadata.num_rows
    identity = hybrid_execution.identity(source)
    folder = args.output / str(rows)
    folder.mkdir(parents=True, exist_ok=True)
    before, current = Worker(args.before_root), Worker(ROOT)
    singles = {f"{version}_{backend}": [] for version in ("before", "after") for backend in hybrid_execution.PATHS}
    parity = None
    try:
        for repeat in range(-1, args.repeats):
            methods = [(version, backend) for version in ("before", "after") for backend in hybrid_execution.PATHS]
            for version, backend in methods if repeat % 2 == 0 else reversed(methods):
                reply, outputs, sample = measured(before if version == "before" else current, source, backend)
                parity = outputs if parity is None else parity
                assert equal(parity, outputs), (rows, version, backend, "single mismatch")
                assert all(step["rows_scanned"] == rows for step in reply["results"])
                if repeat >= 0:
                    singles[f"{version}_{backend}"].append(sample)
            print(json.dumps({"stage": "single_round", "rows": rows, "repeat": repeat}), flush=True)
        calibration = {backend: [] for backend in hybrid_execution.PATHS}
        shared_parity = None
        for repeat in range(-1, 2):
            for backend in hybrid_execution.PATHS:
                reply, outputs, sample = measured(current, source, backend, many=True)
                shared_parity = outputs if shared_parity is None else shared_parity
                assert equal(outputs, shared_parity), (rows, backend, "shared mismatch")
                assert sample["result_reused_steps"] == 9
                if repeat >= 0:
                    calibration[backend].append(sample)
        # Verify every report against a separately executed CPU plan, not merely
        # against another backend using the same fused/memoized implementation.
        independent = []
        for plan in PLANS:
            reply = before.call({"cmd": "batch", "path": str(source), "steps": plan, "force_cpu": True})
            independent.extend(values(step) for step in reply["results"])
        assert equal(independent, shared_parity), "fused versus independent results mismatch"
        profiles = args.output / "profiles"
        profiles.mkdir(exist_ok=True)
        profile = profiles / f"{rows}.json"
        workflow = gpu_session.batch_workflow([step for plan in PLANS for step in plan])
        hybrid_execution.save_measurement(str(profile), str(source), ["region", "revenue"],
                                          workflow, "reports_warm", calibration)
        selected, selection = hybrid_execution.choose(str(profile), str(source), ["region", "revenue"],
                                                      workflow, "reports_warm")
        manifests = {}
        for backend in (*hybrid_execution.PATHS, "auto"):
            jobs = []
            for index, steps in enumerate(PLANS):
                name = f"{backend}_{index}"
                plan_file = folder / f"{name}.json"
                plan_file.write_text(json.dumps({"file_path": str(source), "title": name, "steps": steps,
                    "force_cpu": backend == "cpu", "force_gpu": backend == "native",
                    "load_backend": "cpu_gpu" if backend == "cpu_gpu" else "auto",
                    **({"hybrid_profile": str(profile)} if backend == "auto" else {})}), encoding="utf-8")
                jobs.append({"name": name, "plan": str(plan_file)})
            manifest = folder / f"manifest-{backend}.json"
            manifest.write_text(json.dumps({"jobs": jobs}), encoding="utf-8")
            manifests[backend] = manifest
        queue_samples = {backend: [] for backend in (*hybrid_execution.PATHS, "auto", "cold_auto")}
        def queue_run(backend, executor=None):
            start = time.perf_counter()
            _, summary = batch_queue.run(manifests[backend], folder / "reports", executor=executor)
            wall = time.perf_counter() - start
            assert summary["ok"], summary
            results = [json.loads((Path(job["report_dir"]) / "result.json").read_text())["result"] for job in summary["jobs"]]
            outputs = [values(step) for result in results for step in result["results"]]
            assert equal(outputs, independent), (rows, backend, "queue mismatch")
            assert summary["groups"] == 1
            return {"seconds": wall, "actual": results[0]["loading"]["actual"],
                    "reused_steps": sum(bool(step.get("result_reused")) for result in results for step in result["results"])}
        with batch_queue.QueueExecutor(2) as executor:
            for repeat in range(-1, args.repeats):
                methods = list(manifests)
                for backend in methods if repeat % 2 == 0 else reversed(methods):
                    sample = queue_run(backend, executor)
                    if repeat >= 0:
                        queue_samples[backend].append(sample)
                print(json.dumps({"stage": "hot_queue_round", "rows": rows, "repeat": repeat}), flush=True)
        for _ in range(args.repeats):
            queue_samples["cold_auto"].append(queue_run("auto"))
        assert hybrid_execution.identity(source) == identity, "source changed"
        for worker in (before, current):
            state = worker.call({"cmd": "list"})
            assert state["count"] == state["warm_cache_count"] == 0, state
        evidence = {"rows": rows, "repeats": args.repeats, "results_agree": True,
            "independent_cpu_parity": True, "sources_unchanged": True, "active_sessions": 0,
            "single_samples": singles,
            "single_medians": {name: statistics.median(x["seconds"] for x in samples) for name, samples in singles.items()},
            "queue_samples": queue_samples,
            "queue_medians": {name: statistics.median(x["seconds"] for x in samples) for name, samples in queue_samples.items()},
            "calibration_samples": calibration, "selected": selected,
            "selection_reason": selection["reason"],
            "scope": "single=3 ops warm worker; queue=4 complete reports; cold_auto includes process start/teardown; no LLM"}
        (args.output / f"optimized-{rows}.json").write_text(json.dumps(evidence) + "\n", encoding="utf-8")
        print(json.dumps({"stage": "summary", "rows": rows, "single": evidence["single_medians"],
                          "queue": evidence["queue_medians"], "selected": selected}), flush=True)
    finally:
        before.close()
        current.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, action="append", required=True)
    parser.add_argument("--before-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repeats", type=int, default=5)
    args = parser.parse_args()
    assert 2 <= args.repeats <= 20
    args.output = args.output.resolve()
    args.output.mkdir(parents=True, exist_ok=True)
    os.environ["SESSION_WARM_CACHE_MB"] = "0"
    for key in ("GPU_ANALYSIS_HYBRID_PROFILE", "GPU_ANALYSIS_CALIBRATION_FILE"):
        os.environ.pop(key, None)
    for source in args.input:
        suite(args, source.resolve())


if __name__ == "__main__":
    main()
