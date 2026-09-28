"""Export only timing/validation evidence, excluding host and source identities."""
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills/cudf-analytics/scripts"))
import gpu_session
import hybrid_execution as hybrid
from optimized_execution_benchmark import STEPS


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    smoke = json.loads((args.root / "core-release-smoke.json").read_text())
    if not smoke.get("ok"):
        raise ValueError("release smoke did not pass")
    profile = hybrid.read_profile(str(args.root / "agent-profile.json"))
    for case in smoke["cases"]:
        source = json.loads((args.root / str(case["rows"]) / "auto_0.json").read_text())["file_path"]
        for context in ("batch_cold", "batch_warm"):
            entry = profile["entries"][hybrid.signature(source, ["region", "revenue"],
                                        gpu_session.batch_workflow(STEPS), context)]
            if not entry["verified_equal"]:
                raise ValueError("batch measurements are not verified")
            case["calibration"][context]["samples"] = entry["measurements"]
    record = {"release": "6e287a2", "release_smoke": smoke, "benchmarks": [], "cold_reports": []}
    allowed = ("repeats", "reports", "context", "medians", "samples", "selected", "confidence",
               "auto_verify_seconds", "auto_actual", "results_agree", "sources_unchanged", "scope")
    for case in smoke["cases"]:
        rows = case["rows"]
        record["benchmarks"].append(json.loads((args.root / f"optimized-{rows}.json").read_text()))
        paths = sorted((args.root / f"cold-{rows}").glob("*/calibration-result.json"))
        if len(paths) != 1:
            raise ValueError("ambiguous cold report evidence")
        cold = json.loads(paths[0].read_text())
        record["cold_reports"].append({"rows": rows, **{key: cold[key] for key in allowed}})
    record["report_service"] = json.loads((args.root / "report-service-large.json").read_text())
    encoded = json.dumps(record, allow_nan=False, indent=2) + "\n"
    # Paths, credentials and physical-device identity belong only in local profiles.
    if any(token in encoded for token in (str(args.root.resolve()), "/home/", "/tmp/", "GPU UUID:", "api_key")):
        raise ValueError("private identity unexpectedly present in export")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(encoded, encoding="utf-8")
    print(json.dumps({"ok": True, "cases": len(smoke["cases"]), "bytes": len(encoded.encode())}))


if __name__ == "__main__":
    main()
