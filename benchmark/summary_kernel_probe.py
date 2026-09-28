"""A/B redundant-dropna behavior on the installed GPU; input stays unchanged."""
import argparse
import json
from pathlib import Path
import statistics
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills/cudf-analytics/scripts"))
import gpu_analytics as ga
from fast_execution_benchmark import equal


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", action="append", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    engine = ga.detect_engine()
    assert engine.is_gpu
    original = ga._quartiles
    evidence = []
    for source in args.input:
        frame = engine.read(source, usecols=["revenue"])
        params = ga.build_parser().parse_args(["--input", source, "--op", "summary", "--columns", "revenue"])
        runs = {"double_clean": [], "single_clean": []}
        expected = None
        for repeat in range(-1, 7):
            for mode in runs if repeat % 2 == 0 else reversed(runs):
                ga._quartiles = lambda s, gpu, already_clean=False: original(s, gpu, already_clean=mode == "single_clean")
                ga.sync_device(engine)
                start = time.perf_counter()
                reply = ga.op_summary(frame, engine, params)
                ga.sync_device(engine)
                elapsed = time.perf_counter() - start
                expected = reply if expected is None else expected
                assert equal(reply, expected), "summary result mismatch"
                if repeat >= 0:
                    runs[mode].append(elapsed)
        evidence.append({"rows": len(frame), "samples": runs,
                         "medians": {mode: statistics.median(samples) for mode, samples in runs.items()},
                         "results_agree": True})
        print(json.dumps(evidence[-1]), flush=True)
        del frame
    ga._quartiles = original
    args.output.write_text(json.dumps(evidence) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
