"""Isolated baseline/candidate A/B; no change to production algorithm defaults."""
import argparse
import json
from pathlib import Path
import statistics
import sys
import time
from contextlib import nullcontext

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills/cudf-analytics/scripts"))
import gpu_analytics as ga
import hybrid_execution
from gpu_coordination import gpu_slot
from fast_execution_benchmark import equal
from final_statistics_candidates import frequency_describe


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", action="append", required=True)
    parser.add_argument("--column", default="revenue")
    parser.add_argument("--cpu", action="store_true")
    parser.add_argument("--force-candidate", action="store_true")
    parser.add_argument("--repeats", type=int, default=7)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not 3 <= args.repeats <= 21:
        parser.error("repeats must be 3 to 21")
    eng = ga.detect_engine(force_cpu=args.cpu)
    if not args.cpu and not eng.is_gpu:
        raise RuntimeError("GPU requested but unavailable")
    with (gpu_slot() if eng.is_gpu else nullcontext()):
        evaluate(args, eng)


def evaluate(args, eng):
    records = []
    original = getattr(ga, "_describe_stats_native", ga._describe_stats)
    for source in args.input:
        identity = hybrid_execution.identity(source)
        frame = eng.read(source, usecols=[args.column])
        column = frame[args.column]
        reference = original(column, eng.is_gpu)
        samples = {"baseline": [], "frequency_candidate": []}
        trace = {}
        for repeat in range(-1, args.repeats):
            for method in samples if repeat % 2 == 0 else reversed(samples):
                ga.sync_device(eng)
                started = time.perf_counter()
                result = (original(column, eng.is_gpu) if method == "baseline" else
                          frequency_describe(column, eng.is_gpu, original,
                                             force=args.force_candidate, trace=trace))
                ga.sync_device(eng)
                elapsed = time.perf_counter() - started
                if not equal(result, reference):
                    raise AssertionError(f"candidate precision mismatch: {result} != {reference}")
                if repeat >= 0:
                    samples[method].append(elapsed)
        if hybrid_execution.identity(source) != identity:
            raise AssertionError("input changed")
        record = {"rows": len(frame), "engine": eng.name, "samples": samples,
            "medians": {k: statistics.median(v) for k, v in samples.items()},
            "candidate_trace": trace, "results_agree": True, "source_unchanged": True,
            "scope": "resident single-column exact statistics; excludes read/startup/LLM"}
        records.append(record)
        print(json.dumps(record), flush=True)
        del column, frame
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(records) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
