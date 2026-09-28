"""Synthetic local CPU candidate gate; NOT GB10 or GPU performance evidence."""
import argparse
import json
from pathlib import Path
import statistics
import sys
import time

import numpy as np
import pandas as pd
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills/cudf-analytics/scripts"))
import gpu_analytics as ga
from fast_execution_benchmark import equal
from final_statistics_candidates import frequency_describe


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rows", type=int, default=2_000_000)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not 1_000_000 <= args.rows <= 10_000_000:
        parser.error("rows must be 1,000,000 to 10,000,000")
    rng = np.random.default_rng(7315)
    records = []
    baseline = getattr(ga, "_describe_stats_native", ga._describe_stats)
    for kind in ("repeated_float64", "continuous_float64", "large_offset_narrow"):
        series = pd.Series(rng.integers(0, 200, args.rows).astype("float64") / 100
            if kind == "repeated_float64" else rng.normal(size=args.rows)
            if kind == "continuous_float64" else 1e12 + rng.integers(0, 20, args.rows) / 100)
        expected = baseline(series, False)
        samples = {"baseline": [], "frequency_candidate": []}
        trace = {}
        for repeat in range(-1, 5):
            for method in samples if repeat % 2 == 0 else reversed(samples):
                start = time.perf_counter()
                got = (baseline(series, False) if method == "baseline" else
                       frequency_describe(series, False, baseline, trace=trace))
                elapsed = time.perf_counter() - start
                if not equal(got, expected):
                    raise AssertionError((kind, expected, got))
                if repeat >= 0:
                    samples[method].append(elapsed)
        record = {"input_kind": kind, "rows": len(series), "samples": samples,
            "medians": {k: statistics.median(v) for k, v in samples.items()},
            "candidate_trace": trace, "results_agree": True,
            "scope": "synthetic local CPU resident stats; no disk, no GPU, no LLM"}
        print(json.dumps(record), flush=True)
        records.append(record)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(records) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
