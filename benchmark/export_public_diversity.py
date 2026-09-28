"""Export only public provenance and sanitized aggregate benchmark evidence."""
import argparse
import hashlib
import json
from pathlib import Path
import platform
import re
import zipfile


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--results", type=Path, required=True)
    parser.add_argument("--archive", type=Path, required=True)
    args = parser.parse_args()
    result = json.loads((args.results / "results.json").read_text())
    assert len(result["cases"]) == 8 and all(c["verified_equal"] for c in result["cases"])
    assert result["cleanup"] == {"count": 0, "warm_cache_count": 0}
    sources = {name: json.loads((args.root / f"source-{name}.json").read_text())
               for name in ("gas", "retail", "susy")}
    assert all(s["round_trip_exact"] and s["source_rows_verified"] for s in sources.values())
    root = Path(__file__).resolve().parents[1]
    files = ["skills/cudf-analytics/scripts/gpu_analytics.py",
             "skills/cudf-analytics/scripts/gpu_session.py",
             "skills/cudf-analytics/scripts/hybrid_execution.py",
             "benchmark/prepare_new_public_data.py", "benchmark/public_diversity_benchmark.py"]
    evidence = {"architecture": platform.machine(), "runtime_sha256": {
        name: hashlib.sha256((root / name).read_bytes()).hexdigest() for name in files}}
    payloads = {"results.json": result, "runtime.json": evidence,
                **{f"source-{name}.json": record for name, record in sources.items()}}
    encoded = {name: json.dumps(record, indent=2, ensure_ascii=False, allow_nan=False) + "\n"
               for name, record in payloads.items()}
    for body in encoded.values():
        assert not re.search(r"/home/|/tmp/|[A-Z]:\\|\b(?:\d{1,3}\.){3}\d{1,3}\b", body), "private location found"
    if args.archive.exists():
        raise FileExistsError("refusing to overwrite an existing evidence archive")
    with zipfile.ZipFile(args.archive, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, body in encoded.items():
            archive.writestr(name, body)
    for case in result["cases"]:
        m = case["median_seconds"]
        auto = m["batch_auto_calibrated"]
        print(json.dumps({"case": case["case"], "rows": case["rows"], "batch_cpu": m["batch_cpu"],
              "batch_gpu": m.get("batch_native"), "batch_mixed": m.get("batch_cpu_gpu"), "auto": auto,
              "gpu_vs_cpu": m["batch_cpu"] / m["batch_native"] if "batch_native" in m else None,
              "auto_vs_cpu": m["batch_cpu"] / auto,
              "auto_vs_independent_cpu": m["independent_cpu"] / auto,
              "batch_auto_route": case["samples"]["batch_auto_calibrated"][0]["actual"],
              "reports_auto_route": case["samples"]["reports_auto_calibrated"][0]["actual"]}), flush=True)
    print(json.dumps({"verified_cases": len(result["cases"]), "archive_bytes": args.archive.stat().st_size}), flush=True)


if __name__ == "__main__":
    main()
