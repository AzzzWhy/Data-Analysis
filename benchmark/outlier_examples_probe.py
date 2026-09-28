"""Interleaved old/new exact outlier operation on real data; no input mutation."""
import argparse
from contextlib import nullcontext
import json
from pathlib import Path
import statistics
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills/cudf-analytics/scripts"))
import gpu_analytics as ga
import hybrid_execution as hybrid
from gpu_coordination import gpu_slot
from fast_execution_benchmark import equal
from fast_execution_benchmark import values
import gpu_session


def legacy(frame, mask, columns, count, limit):
    return frame[list(columns)][mask].head(limit)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", action="append", required=True)
    parser.add_argument("--column", default="revenue")
    parser.add_argument("--cpu", action="store_true")
    parser.add_argument("--workflow", action="store_true",
                        help="include fresh projected read, summary, outliers and region groupby")
    parser.add_argument("--load-backend", choices=("native", "cpu_gpu"), default="native")
    parser.add_argument("--repeats", type=int, default=7)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not 3 <= args.repeats <= 21:
        parser.error("repeats must be 3 to 21")
    if args.cpu and args.load_backend == "cpu_gpu":
        parser.error("CPU-only cannot use cpu_gpu")
    engine = ga.detect_engine(force_cpu=args.cpu)
    if not args.cpu and not engine.is_gpu:
        raise RuntimeError("GPU requested but unavailable")
    candidate = ga._outlier_examples
    records = []
    try:
        with gpu_slot() if engine.is_gpu else nullcontext():
            for source in args.input:
                identity = hybrid.identity(source)
                frame = None if args.workflow else engine.read(source, usecols=[args.column])
                params = ga.build_parser().parse_args(["--input", source, "--op", "outliers",
                                                       "--columns", args.column, "--top-k", "3"])
                prior = None if args.workflow else ga.op_summary(frame, engine, params)["stats"]
                samples = {"legacy_full_filter": [], "bounded_examples": []}
                reference = None
                for repeat in range(-1, args.repeats):
                    for mode in samples if repeat % 2 == 0 else reversed(samples):
                        ga._outlier_examples = legacy if mode == "legacy_full_filter" else candidate
                        ga.sync_device(engine)
                        start = time.perf_counter()
                        if args.workflow:
                            reply = gpu_session.do_batch({"path": source,
                                "steps": [{"op": "summary", "columns": args.column},
                                          {"op": "outliers", "columns": args.column, "top_k": 3},
                                          {"op": "groupby", "by": "region",
                                           "agg": args.column + ":sum", "top_k": 3}],
                                "force_cpu": args.cpu, "force_gpu": not args.cpu,
                                "load_backend": args.load_backend})
                            if not reply.get("ok") or any(step.get("fallback_reason")
                                or step["engine"] != engine.name for step in reply["results"]):
                                raise AssertionError(reply)
                            actual = "cpu" if args.cpu else "cpu_gpu" if args.load_backend == "cpu_gpu" else "native_gpu"
                            if reply["loading"]["actual"] != actual:
                                raise AssertionError("requested loader did not run")
                            result = [values(step) for step in reply["results"]]
                        else:
                            reply = ga.op_outliers(frame, engine, params, summary_stats=prior)
                            result = reply
                        ga.sync_device(engine)
                        seconds = time.perf_counter() - start
                        reference = result if reference is None else reference
                        if not equal(reference, result):
                            raise AssertionError("outlier results disagree")
                        if repeat >= 0:
                            samples[mode].append(seconds)
                if hybrid.identity(source) != identity:
                    raise AssertionError("source changed")
                outliers = reply["results"][1]["outliers"] if args.workflow else reply
                record = {"rows": reply["rows"] if args.workflow else len(frame),
                          "engine": engine.name, "load_backend": args.load_backend, "samples": samples,
                          "medians": {k: statistics.median(v) for k, v in samples.items()},
                          "outlier_count": outliers["results"][args.column]["count"],
                          "results_agree": True, "source_unchanged": True,
                          "scope": "fresh projected read plus exact summary/outliers/groupby; hot process; no LLM"
                                   if args.workflow else "full IQR mask/count/ties/examples; resident one column; "
                                   "exact quartiles reused; excludes load/startup/LLM"}
                records.append(record)
                print(json.dumps(record), flush=True)
                del frame
    finally:
        ga._outlier_examples = candidate
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(records) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
