"""Compare four complete cold queue routes on existing all-scale manifests.

Each sample creates and closes its own process pool. The benchmark checks
report parity after timing and never alters the source Parquet files.
"""
import argparse
import json
from pathlib import Path
import statistics
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "agent"))
from fast_execution_benchmark import equal, values

ROUTES = ("cpu", "native", "cpu_gpu", "auto")


def extract_results(folder):
    summary = json.loads((folder / "queue-summary.json").read_text(encoding="utf-8"))
    if not summary["ok"] or len(summary["jobs"]) != 4:
        raise RuntimeError(f"queue did not complete four reports: {summary}")
    results = []
    actual = set()
    for job in summary["jobs"]:
        report = json.loads((Path(job["report_dir"]) / "result.json").read_text(encoding="utf-8"))["result"]
        results.append([values(step) for step in report["results"]])
        actual.add(report["loading"]["actual"])
    return results, sorted(actual)


def source_identity(manifest):
    jobs = json.loads(manifest.read_text(encoding="utf-8"))["jobs"]
    sources = set()
    for job in jobs:
        plan = json.loads(Path(job["plan"]).read_text(encoding="utf-8"))
        sources.add(Path(plan["file_path"]))
    return {str(path): (path.stat().st_size, path.stat().st_mtime_ns) for path in sources}


def suite(result_root, rows, output_root, repeats):
    folder = result_root / str(rows)
    manifests = {route: folder / f"manifest-{route}.json" for route in ROUTES}
    if any(not path.is_file() for path in manifests.values()):
        raise FileNotFoundError(f"missing manifest for {rows}")
    identities = source_identity(manifests["auto"])
    samples = {route: [] for route in ROUTES}
    reference = None
    actual_routes = {route: set() for route in ROUTES}
    for repeat in range(-1, repeats):
        order = ROUTES if repeat % 2 == 0 else tuple(reversed(ROUTES))
        for route in order:
            started = time.perf_counter()
            proc = subprocess.run([sys.executable, str(ROOT / "agent/batch_queue.py"),
                "--manifest", str(manifests[route]), "--output-root", str(output_root / str(rows) / route)],
                capture_output=True, text=True, encoding="utf-8", timeout=180)
            seconds = time.perf_counter() - started
            if proc.returncode:
                raise RuntimeError(f"{rows} {route}: {proc.stderr[-2000:]}")
            report_folder = Path(proc.stdout.strip().splitlines()[-1])
            results, actual = extract_results(report_folder)
            if reference is None:
                reference = results
            elif not equal(results, reference):
                raise AssertionError(f"{rows} {route}: report mismatch")
            if repeat >= 0:
                samples[route].append(seconds)
                actual_routes[route].update(actual)
        print(json.dumps({"stage": "cold_round", "rows": rows, "repeat": repeat}), flush=True)
    if any((Path(path).stat().st_size, Path(path).stat().st_mtime_ns) != identity
           for path, identity in identities.items()):
        raise AssertionError("source changed")
    record = {"rows": rows, "repeats": repeats, "samples": samples,
        "medians": {route: statistics.median(values_) for route, values_ in samples.items()},
        "actual_routes": {route: sorted(actual) for route, actual in actual_routes.items()},
        "results_agree": True, "sources_unchanged": True,
        "scope": "four complete reports; new queue process and worker pool each sample; OS file cache retained; no LLM"}
    print(json.dumps({"stage": "cold_summary", "rows": rows,
        "medians": record["medians"], "actual_routes": record["actual_routes"]}), flush=True)
    return record


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--result-root", type=Path, required=True)
    parser.add_argument("--rows", type=int, action="append", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repeats", type=int, default=5)
    args = parser.parse_args()
    if not 3 <= args.repeats <= 20:
        parser.error("repeats must be 3 to 20")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    records = [suite(args.result_root, rows, args.output.parent / "cold-route-runs",
                     args.repeats) for rows in args.rows]
    args.output.write_text(json.dumps(records) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
