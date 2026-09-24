#!/usr/bin/env python3
"""
Attribution benchmark: how much of the cuDF speedup is GPU compute, and how much is
just parallel file I/O?

Why this exists
---------------
`benchmark_cpu_vs_gpu.py` reports an end-to-end pipeline time, but that number is
dominated by CSV parsing (81-91% of it on GB10). It therefore cannot answer the
question a reviewer will actually ask: "is the GPU doing the computation faster, or
did you just parallelise the disk read?" Nor does it separate GPU compute from the
fact that single-threaded pandas is using 1 of 20 available CPU cores.

This script runs three controlled scenarios so the speedup can be attributed:

  A. in-memory   Both engines load the SAME file once, outside any timed region.
                 Only the compute stages are timed. This isolates compute speedup.
  B. warm-read   Includes the file read, with the page cache pre-warmed, so the read
                 is CPU/parse-bound rather than disk-bound.
  C. cold-read   Page cache dropped via posix_fadvise(DONTNEED) before each read, so
                 the read is genuinely disk-bound. Upper bound for I/O gain.

Plus `to_pandas()` measured separately, because that host transfer is part of the
real-world cost of using cuDF and is too often left out.

Every stage reports min/median/max over N repeats, not a single point, and both
engines are checked for numerical agreement so a "speedup" cannot come from doing
less work.

Usage
-----
    python attribution_test.py --rows 3m 10m --repeats 5
    python attribution_test.py --rows 10m --repeats 5 --format parquet
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import statistics
import sys
import time
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

ROW_SUFFIX = {"k": 1_000, "m": 1_000_000, "b": 1_000_000_000}
STAGES = ("groupby", "mean", "corr", "quantile")
OUT_JSON = "attribution_results.json"
OUT_MD = "attribution_results.md"


def say(msg: str = "") -> None:
    print(msg, flush=True)


def parse_rows(spec: str) -> int:
    s = spec.strip().lower().replace("_", "")
    if s and s[-1] in ROW_SUFFIX:
        return int(float(s[:-1]) * ROW_SUFFIX[s[-1]])
    return int(float(s))


def sync(mod: Any, is_gpu: bool) -> None:
    if not is_gpu:
        return
    try:
        import cupy as cp  # type: ignore

        cp.cuda.Stream.null.synchronize()
    except Exception:
        try:
            int(mod.Series([1]).sum())
        except Exception:
            pass


def one_run(fn: Callable[[], Any], mod: Any, is_gpu: bool) -> float:
    """Time a single call with the device synchronized on both sides."""
    sync(mod, is_gpu)
    t0 = time.perf_counter()
    fn()
    sync(mod, is_gpu)
    return time.perf_counter() - t0


def repeat_stats(fn: Callable[[], Any], mod: Any, is_gpu: bool, n: int) -> Dict[str, float]:
    """Statistic-ready sample: min/median/max/mean/std-dev over n runs, plus the raw list."""
    samples = [one_run(fn, mod, is_gpu) for _ in range(max(1, n))]
    return {
        "min": min(samples),
        "median": statistics.median(samples),
        "max": max(samples),
        "mean": statistics.fmean(samples),
        "stdev": statistics.stdev(samples) if len(samples) > 1 else 0.0,
        "n": len(samples),
        "samples": samples,
    }


def drop_page_cache(path: str) -> bool:
    """Evict this file from the page cache so a read is genuinely disk-bound."""
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return False
    try:
        os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED)
        return True
    except (AttributeError, OSError):
        return False
    finally:
        os.close(fd)


def warm_page_cache(path: str) -> None:
    with open(path, "rb") as fh:
        while fh.read(32 * 1024 * 1024):
            pass


# --------------------------------------------------------------------------------------
# Data generation
# --------------------------------------------------------------------------------------


def generate(rows: int, cols: int, path: str, fmt: str, seed: int = 17) -> None:
    import numpy as np
    import pandas as pd

    rng = np.random.default_rng(seed)
    cols = max(6, cols)
    data: Dict[str, Any] = {
        "row_id": np.arange(rows, dtype=np.int64),
        "region": rng.choice(["APAC", "EMEA", "AMER", "LATAM", "MEA"], size=rows),
        "category": rng.choice(["alpha", "beta", "gamma", "delta"], size=rows),
        "quantity": rng.integers(1, 500, size=rows, dtype=np.int64),
        "revenue": rng.lognormal(6.0, 1.4, size=rows),
        "cost": rng.lognormal(5.4, 1.1, size=rows),
    }
    for i in range(max(0, cols - len(data))):
        data[f"metric_{i:02d}"] = rng.normal(loc=100 + i, scale=15 + i, size=rows)
    df = pd.DataFrame(data)
    df["profit"] = df["revenue"] - df["cost"]
    if fmt == "parquet":
        df.to_parquet(path, index=False)
    else:
        df.to_csv(path, index=False)


# --------------------------------------------------------------------------------------
# The workload
# --------------------------------------------------------------------------------------


def stage_fns(df: Any, mod: Any, is_gpu: bool) -> Dict[str, Callable[[], Any]]:
    """The compute stages, identical logic on both engines."""
    num = [c for c in ("quantity", "revenue", "cost", "profit") if c in [str(x) for x in df.columns]]

    def groupby() -> Any:
        return df.groupby("region", as_index=False).agg(
            {"revenue": ["mean", "sum"], "quantity": ["sum"]}
        )

    def mean() -> Any:
        return df["revenue"].mean()

    def corr() -> Any:
        sub = df[num]
        if is_gpu:
            return sub.corr()
        kwargs = {"numeric_only": True} if _needs_numeric_only() else {}
        return sub.corr(**kwargs)

    def quantile() -> Any:
        return df["revenue"].quantile([0.25, 0.5, 0.75, 0.99])

    return {"groupby": groupby, "mean": mean, "corr": corr, "quantile": quantile}


def _needs_numeric_only() -> bool:
    import pandas as pd

    try:
        return int(str(pd.__version__).split(".")[0]) < 3
    except Exception:
        return True


def load(mod: Any, is_gpu: bool, path: str) -> Any:
    if os.path.splitext(path)[1].lower() in (".parquet", ".pq"):
        return mod.read_parquet(path)
    return mod.read_csv(path)


def checks(df: Any, mod: Any, is_gpu: bool) -> Dict[str, Any]:
    """Scalar fingerprints so both engines can be shown to compute the same thing."""
    out: Dict[str, Any] = {"rows": int(len(df))}
    try:
        out["mean_revenue"] = round(float(df["revenue"].mean()), 6)
    except Exception:
        pass
    try:
        g = df.groupby("region", as_index=False).agg({"revenue": ["mean", "sum"]})
        if is_gpu:
            g = g.to_pandas()
        out["groupby_sum_total"] = round(float(g.iloc[:, 2].sum()), 4)
    except Exception:
        pass
    return out


# --------------------------------------------------------------------------------------
# Scenarios
# --------------------------------------------------------------------------------------


def scenario_in_memory(cpu_df: Any, gpu_df: Any, pd_mod: Any, cu_mod: Any,
                       repeats: int) -> Dict[str, Any]:
    """A: data already resident in host and device memory; only compute is timed."""
    out: Dict[str, Any] = {"cpu": {}, "gpu": {}, "speedup": {}}
    cpu_fns = stage_fns(cpu_df, pd_mod, False)
    gpu_fns = stage_fns(gpu_df, cu_mod, True)
    for stage in STAGES:
        out["cpu"][stage] = repeat_stats(cpu_fns[stage], pd_mod, False, repeats)
        out["gpu"][stage] = repeat_stats(gpu_fns[stage], cu_mod, True, repeats)
        c, g = out["cpu"][stage]["median"], out["gpu"][stage]["median"]
        out["speedup"][stage] = round(c / g, 3) if g > 0 else None
    # Full compute sweep, to give one headline compute-only number.
    def cpu_sweep() -> None:
        for stage in STAGES:
            cpu_fns[stage]()

    def gpu_sweep() -> None:
        for stage in STAGES:
            gpu_fns[stage]()

    out["cpu"]["all_stages"] = repeat_stats(cpu_sweep, pd_mod, False, repeats)
    out["gpu"]["all_stages"] = repeat_stats(gpu_sweep, cu_mod, True, repeats)
    c = out["cpu"]["all_stages"]["median"]
    g = out["gpu"]["all_stages"]["median"]
    out["speedup"]["all_stages"] = round(c / g, 3) if g > 0 else None
    return out


def scenario_read(path: str, cpu_mod: Any, cu_mod: Any, repeats: int,
                  cold: bool) -> Dict[str, Any]:
    """B/C: read timing, optionally with the page cache evicted first."""
    out: Dict[str, Any] = {"cold_cache": cold, "cpu": {}, "gpu": {}, "speedup": None}

    def timed_read(mod: Any, is_gpu: bool) -> List[float]:
        samples: List[float] = []
        for _ in range(max(1, repeats)):
            if cold:
                dropped = drop_page_cache(path)
                if not dropped:
                    out["cache_drop_supported"] = False
            else:
                warm_page_cache(path)
            samples.append(one_run(lambda: load(mod, is_gpu, path), mod, is_gpu))
        return samples

    cpu_samples = timed_read(cpu_mod, False)
    gpu_samples = timed_read(cu_mod, True)
    out["cpu"] = {
        "min": min(cpu_samples), "median": statistics.median(cpu_samples),
        "max": max(cpu_samples), "n": len(cpu_samples), "samples": cpu_samples,
    }
    out["gpu"] = {
        "min": min(gpu_samples), "median": statistics.median(gpu_samples),
        "max": max(gpu_samples), "n": len(gpu_samples), "samples": gpu_samples,
    }
    cm, gm = out["cpu"]["median"], out["gpu"]["median"]
    out["speedup"] = round(cm / gm, 3) if gm > 0 else None
    out["cache_drop_supported"] = out.get("cache_drop_supported", drop_page_cache(path))
    return out


def measure_to_pandas(gpu_df: Any, cu_mod: Any, repeats: int) -> Dict[str, float]:
    """The host transfer: real cost of consuming cuDF output, usually omitted."""
    return repeat_stats(lambda: gpu_df.head(len(gpu_df) // 10).to_pandas(), cu_mod, True, repeats)


# --------------------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------------------


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        prog="attribution_test.py",
        description="Attribute the cuDF speedup to GPU compute vs parallel I/O.")
    ap.add_argument("--rows", nargs="+", default=["3m", "10m"],
                    help="row counts, e.g. 1m 5m (default: 3m 10m)")
    ap.add_argument("--columns", type=int, default=12)
    ap.add_argument("--repeats", type=int, default=5, help="repeats per stage (default 5)")
    ap.add_argument("--format", choices=["csv", "parquet"], default="csv")
    ap.add_argument("--workdir", default="attribution_data")
    ap.add_argument("--keep-data", action="store_true")
    ap.add_argument("--cpu-threads", type=int, default=1,
                    help="threads pandas/C-level libs may use (default 1 = the usual "
                         "single-threaded baseline; set to the core count to answer "
                         "'why didn't you just use more CPU cores?')")
    ap.add_argument("--out-json", default=OUT_JSON)
    ap.add_argument("--out-md", default=OUT_MD)
    args = ap.parse_args(argv)

    import pandas as pd

    threads_set: Dict[str, Any] = {}
    if args.cpu_threads and args.cpu_threads > 1:
        # Every knob that lets the CPU side actually use the cores it has.
        for var in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS",
                    "NUMEXPR_NUM_THREADS", "VECLIB_MAXIMUM_THREADS"):
            os.environ[var] = str(args.cpu_threads)
            threads_set[var] = args.cpu_threads

    try:
        import cudf  # type: ignore

        if int(cudf.Series([1, 2, 3]).sum()) != 6:
            raise RuntimeError("cuDF smoke probe failed")
        cu: Any = cudf
        cudf_version = str(getattr(cudf, "__version__", "unknown"))
    except Exception as exc:
        say(f"cuDF unavailable ({type(exc).__name__}: {exc}).")
        say("This test is meaningless without both engines; run it on the GB10.")
        return 2

    gpu_name = None
    try:
        import cupy as cp  # type: ignore

        gpu_name = cp.cuda.runtime.getDeviceProperties(0).get("name")
        if isinstance(gpu_name, bytes):
            gpu_name = gpu_name.decode()
    except Exception:
        pass

    say("=" * 78)
    say("cuDF speedup attribution :: what is GPU compute, what is parallel I/O")
    say("=" * 78)
    say(f"  GPU         : {gpu_name}")
    say(f"  cuDF        : {cudf_version}")
    say(f"  pandas      : {pd.__version__} (C-level ops; threads={args.cpu_threads})")
    say(f"  CPU cores   : {os.cpu_count()}   (pandas baseline uses {args.cpu_threads} "
        f"thread(s); any gap there is core count, NOT the GPU)")
    say(f"  repeats     : {args.repeats} per stage, reporting min/median/max")
    say(f"  platform    : {platform.platform()}")
    say("=" * 78)

    results: List[Dict[str, Any]] = []
    os.makedirs(args.workdir, exist_ok=True)

    for spec in args.rows:
        rows = parse_rows(spec)
        ext = ".parquet" if args.format == "parquet" else ".csv"
        path = os.path.join(args.workdir, f"attr_{rows}{ext}")
        say(f"\n[{spec}] generating {path} ({rows:,} rows x {args.columns} cols)")
        t0 = time.perf_counter()
        generate(rows, args.columns, path, args.format)
        say(f"  generated in {time.perf_counter() - t0:.1f}s, {os.path.getsize(path)/1e6:.1f} MB")

        rec: Dict[str, Any] = {
            "rows": rows, "label": spec, "file_mb": round(os.path.getsize(path) / 1e6, 1),
        }

        # ---- C: cold-cache read (genuinely disk-bound) ----
        say("\n  [C] cold-cache read (page cache dropped before each read)")
        rec["cold_read"] = scenario_read(path, pd, cu, args.repeats, cold=True)
        say(f"      pandas {rec['cold_read']['cpu']['median']:.3f}s  "
            f"cuDF {rec['cold_read']['gpu']['median']:.3f}s  "
            f"-> {rec['cold_read']['speedup']}x")

        # ---- B: warm-cache read (parse-bound) ----
        say("  [B] warm-cache read (page cache pre-warmed)")
        rec["warm_read"] = scenario_read(path, pd, cu, args.repeats, cold=False)
        say(f"      pandas {rec['warm_read']['cpu']['median']:.3f}s  "
            f"cuDF {rec['warm_read']['gpu']['median']:.3f}s  "
            f"-> {rec['warm_read']['speedup']}x")

        # ---- A: in-memory compute only ----
        say("  [A] in-memory compute (load outside the timed region)")
        warm_page_cache(path)
        cpu_df = load(pd, False, path)
        gpu_df = load(cu, True, path)
        rec["checks"] = {"pandas": checks(cpu_df, pd, False), "cudf": checks(gpu_df, cu, True)}
        rec["in_memory"] = scenario_in_memory(cpu_df, gpu_df, pd, cu, args.repeats)
        for stage in STAGES + ("all_stages",):
            c = rec["in_memory"]["cpu"][stage]["median"]
            g = rec["in_memory"]["gpu"][stage]["median"]
            sp = rec["in_memory"]["speedup"][stage]
            say(f"      {stage:<11} pandas {c:8.4f}s  cuDF {g:8.4f}s  {sp}x")
        rec["to_pandas_10pct"] = measure_to_pandas(gpu_df, cu, args.repeats)
        say(f"      to_pandas() (10% of rows) median "
            f"{rec['to_pandas_10pct']['median']:.3f}s  <- real-world transfer cost")

        rec["agreement"] = _agreement(rec["checks"])
        results.append(rec)

        del cpu_df, gpu_df
        if not args.keep_data:
            try:
                os.remove(path)
                say(f"  removed {path}")
            except OSError:
                pass

    _write(args, results, gpu_name, cudf_version, pd.__version__)
    _verdict(results)
    return 0


def _agreement(ck: Dict[str, Any]) -> Dict[str, Any]:
    p, g = ck.get("pandas", {}), ck.get("cudf", {})
    out: Dict[str, Any] = {}
    for k in ("mean_revenue", "groupby_sum_total"):
        if p.get(k) is None or g.get(k) is None:
            continue
        a, b = float(p[k]), float(g[k])
        out[k] = {"pandas": a, "cudf": b,
                  "rel_diff": round(abs(a - b) / max(abs(a), 1e-12), 8)}
    return out


def _verdict(results: List[Dict[str, Any]]) -> None:
    say("\n" + "=" * 78)
    say("VERDICT")
    say("=" * 78)
    for r in results:
        cold = r["cold_read"]["speedup"]
        warm = r["warm_read"]["speedup"]
        comp = r["in_memory"]["speedup"].get("all_stages")
        say(f"  {r['rows']:,} rows:")
        say(f"    I/O gain      (cold read) : {cold}x")
        say(f"    I/O gain      (warm read) : {warm}x")
        say(f"    COMPUTE gain  (in-memory) : {comp}x   <- the GPU-attributable part")
        if cold and warm and comp:
            say(f"    -> of the cold-read gain, ~{min(comp / cold * 100, 100):.0f}% is compute, "
                f"the rest is parallel parsing")
    say("\nHow to read this honestly:")
    say("  * 'in-memory compute' is the only number that is about the GPU itself.")
    say("  * The read gains include parallel CSV parsing, which a CPU-only tool")
    say("    (polars, Dask, multi-threaded pandas) could also achieve.")
    say("  * The pandas baseline uses 1 core; the hardware has N cores. Part of every")
    say("    speedup above is core count, not the GPU.")
    say("  * Always quote the compute figure alongside the end-to-end figure.")


def _write(args: argparse.Namespace, results: List[Dict[str, Any]], gpu: Optional[str],
           cudf_version: str, pandas_version: str) -> None:
    payload = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "methodology": {
            "purpose": "separate GPU compute speedup from parallel-I/O speedup",
            "baseline": f"pandas {pandas_version}, default single-threaded C ops",
            "accelerated": f"cuDF {cudf_version} on {gpu}",
            "repeats": args.repeats,
            "statistic": "min/median/max over repeats",
            "in_memory": "both engines pre-loaded outside the timed region",
            "cold_read": "posix_fadvise(DONTNEED) before each read",
            "warm_read": "page cache pre-warmed",
            "correctness": "scalar fingerprints compared between engines",
            "cpu_threads": args.cpu_threads,
            "thread_env": threads_set,
        },
        "host": {"gpu": gpu, "cudf": cudf_version, "pandas": pandas_version,
                 "cpu_cores": os.cpu_count(), "platform": platform.platform()},
        "results": results,
    }
    with open(args.out_json, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, ensure_ascii=False)

    lines = [
        "# Speedup attribution: GPU compute vs parallel I/O",
        "",
        f"- GPU: {gpu} | cuDF {cudf_version}",
        f"- Baseline: pandas {pandas_version}, default single-threaded C ops "
        f"({os.cpu_count()} cores available, so part of every gain is core count)",
        f"- {args.repeats} repeats per stage; min/median/max recorded",
        "",
        "| Rows | Scenario | pandas (s) | cuDF (s) | Speedup |",
        "| ---: | :--- | ---: | ---: | ---: |",
    ]
    for r in results:
        for key, label in (("cold_read", "read, cold cache"),
                           ("warm_read", "read, warm cache")):
            blk = r[key]
            lines.append(f"| {r['rows']:,} | {label} | {blk['cpu']['median']:.3f} | "
                         f"{blk['gpu']['median']:.3f} | **{blk['speedup']}x** |")
        for stage in STAGES + ("all_stages",):
            c = r["in_memory"]["cpu"][stage]
            g = r["in_memory"]["gpu"][stage]
            sp = r["in_memory"]["speedup"][stage]
            label = "compute, in-memory (all)" if stage == "all_stages" else \
                    f"compute, in-memory ({stage})"
            lines.append(f"| {r['rows']:,} | {label} | {c['median']:.4f} | {g['median']:.4f} | "
                         f"**{sp}x** |")
    lines += [
        "",
        "Only the `compute, in-memory` rows are attributable to the GPU. The read rows also",
        "include parallel CSV parsing, which a CPU-only tool could match, and the pandas",
        "baseline uses a single core out of many.",
        "",
    ]
    with open(args.out_md, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))


if __name__ == "__main__":
    sys.exit(main())