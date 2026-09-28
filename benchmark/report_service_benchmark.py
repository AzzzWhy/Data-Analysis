"""Same exact manifest: fresh queue CLI vs a persistent local report service."""
import argparse
import json
from pathlib import Path
import queue
import statistics
import subprocess
import sys
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "agent"))
import batch_job
import batch_queue
from fast_execution_benchmark import equal, values


class Service:
    def __init__(self, output):
        started = time.perf_counter()
        self.process = subprocess.Popen([sys.executable, str(ROOT / "agent/report_service.py"),
            "--output-root", str(output), "--timeout-seconds", "90"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=sys.stderr, text=True, encoding="utf-8", bufsize=1)
        self.responses = queue.Queue()
        def read():
            for line in self.process.stdout:
                self.responses.put(line)
            self.responses.put(None)
        threading.Thread(target=read, daemon=True).start()
        ready = self.response()
        if ready.get("event") != "ready":
            raise RuntimeError(ready)
        self.startup = time.perf_counter() - started

    def response(self):
        line = self.responses.get(timeout=120)
        if line is None:
            raise RuntimeError("service exited")
        return json.loads(line)

    def request(self, payload):
        self.process.stdin.write(json.dumps(payload) + "\n")
        self.process.stdin.flush()
        return self.response()

    def close(self):
        try:
            if self.process.poll() is None:
                self.request({"cmd": "close"})
                self.process.stdin.close()
                self.process.wait(timeout=10)
        finally:
            if self.process.poll() is None:
                # Closing stdin requests EOF cleanup; don't silently orphan a
                # service when a benchmark fails its exact-parity assertion.
                self.process.stdin.close()
                self.process.wait(timeout=120)
            self.process.stdout.close()


def outputs(summary):
    actual, engines, reused = [], set(), 0
    for job in summary["jobs"]:
        if not job["ok"]:
            raise RuntimeError(job)
        report = json.loads((Path(job["report_dir"]) / "result.json").read_text(encoding="utf-8"))["result"]
        actual.append([values(step) for step in report["results"]])
        engines.add(report["loading"]["actual"])
        reused += sum(bool(step.get("result_reused")) for step in report["results"])
    return actual, sorted(engines), reused


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repeats", type=int, default=5)
    args = parser.parse_args()
    if not 3 <= args.repeats <= 21:
        parser.error("repeats must be 3 to 21")
    jobs = batch_queue.load_manifest(args.manifest)
    sources = {batch_job.load_plan(job.plan_path)["file_path"] for job in jobs}
    identities = {path: (Path(path).stat().st_size, Path(path).stat().st_mtime_ns) for path in sources}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    folder = args.output.parent / f"service-runs-{time.time_ns()}"
    service = Service(folder / "warm")
    samples = {"fresh_queue_cli": [], "persistent_service": []}
    expected = None
    first_request = None
    status = None
    try:
        for repeat in range(-1, args.repeats):
            for mode in samples if repeat % 2 == 0 else reversed(samples):
                start = time.perf_counter()
                if mode == "fresh_queue_cli":
                    result = subprocess.run([sys.executable, str(ROOT / "agent/batch_queue.py"),
                        "--manifest", str(args.manifest.resolve()), "--output-root", str(folder / "cold")],
                        check=True, capture_output=True, text=True, encoding="utf-8", timeout=120)
                    summary = json.loads((Path(result.stdout.strip()) / "queue-summary.json").read_text())
                else:
                    reply = service.request({"cmd": "run", "manifest": str(args.manifest.resolve())})
                    if not reply.get("ok"):
                        raise RuntimeError(reply)
                    summary = reply["summary"]
                wall = time.perf_counter() - start
                if mode == "persistent_service" and first_request is None:
                    first_request = wall
                result_values, engines, reused = outputs(summary)
                expected = result_values if expected is None else expected
                if not equal(result_values, expected):
                    raise AssertionError("service and fresh queue disagree")
                if repeat >= 0:
                    samples[mode].append({"seconds": wall, "actual_paths": engines, "reused_steps": reused})
        status = service.request({"cmd": "status"})
    finally:
        service.close()
    if any((Path(path).stat().st_size, Path(path).stat().st_mtime_ns) != identity
           for path, identity in identities.items()):
        raise AssertionError("source changed")
    record = {"repeats": args.repeats, "reports": len(jobs), "samples": samples,
        "medians": {mode: statistics.median(r["seconds"] for r in runs) for mode, runs in samples.items()},
        "service_ready_seconds": service.startup, "first_service_request_seconds": first_request,
        "worker_count": len(status["worker_pids"]), "results_agree": True, "sources_unchanged": True,
        "service_exit_code": service.process.returncode,
        "scope": "local machine; full report I/O; fresh CLI includes startup/exit; steady service excludes first startup; no LLM"}
    args.output.write_text(json.dumps(record) + "\n", encoding="utf-8")
    print(json.dumps(record))


if __name__ == "__main__":
    main()
