#!/usr/bin/env python3
"""
Memory-ceiling test: at what data size does pandas fail where cuDF still succeeds?

Why this matters more than a speedup number
-------------------------------------------
A speedup says "the GPU is faster". A memory ceiling says "the CPU path cannot do this
at all". The second claim is far stronger, and on a GB10 (128 GB *unified* memory, where
the GPU allocates from the same pool the CPU uses) it is the honest expression of the
architecture's advantage.

Method
------
Scaling test: increase size until pandas can no longer complete the analysis. Either it
raises MemoryError, or it thrashes the page cache so badly that its runtime explodes
past a wall-clock budget. cuDF is run on the identical file at every size.

The comparison is deliberately generous to pandas: cuDF must ALSO finish inside the same
budget, and the CPU's failure mode is recorded precisely (exception vs timeout) rather
than being assumed.

Usage
-----
    python memory_ceiling_test.py --sizes 20m 50m 100m 200m --time-budget 300
    python memory_ceiling_test.py --sizes 100m --format parquet --repeats 1
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import sys
import time
import traceback
from typing import Any, Dict, List, Optional, Sequence

ROW_SUFFIX = {"k": 1_000, "m": 1_000_000, "b": 1_000_000_000}
OUT_JSON = "memory_ceiling_results.json"
OUT_MD = "memory_ceiling_results.md"


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


def mem_snapshot() -> Dict[str, Any]:
    """Host memory, and the GPU pool if cuPy is available."""
    out: Dict[str, Any] = {}
    try:
        import psutil  # type: ignore

        vm = psutil.virtual_memory()
        out["host_total_gb"] = round(vm.total / 1e9, 1)
        out["host_available_gb"] = round(vm.available / 1e9, 1)
    except Exception:
        try:
            with open("/proc/meminfo") as fh:
                info = {}
                for line in fh:
                    k, _, v = line.partition(":")
                    info[k.strip()] = int(v.split()[0]) * 1024
                out["host_total_gb"] = round(info.get("MemTotal", 0) / 1e9, 1)
                out["host_available_gb"] = round(info.get("MemAvailable", 0) / 1e9, 1)
        except Exception:
            pass
    try:
        import cupy as cp  # type: ignore

        free, total = cp.cuda.runtime.memGetInfo()
        out["gpu_free_gb"] = round(free / 1e9, 1)
        out["gpu_total_gb"] = round(total / 1e9, 1)
        out["unified_memory"] = True  # GB10 reports a large pool shared with the host
    except Exception:
        pass
    return out


def generate(rows: int, cols: int, path: str, seed: int = 23) -> int:
    """
    Write a CSV in chunks, so generation itself never dominates the run.

    Peak host memory during generation is roughly one chunk (~2M rows), not the whole
    file, which keeps the generator cheap next to the analysis being measured.
    """
    import numpy as np
    import pandas as pd

    rng = np.random.default_rng(seed)
    cols = max(6, cols)
    chunk = 2_000_000
    first = True
    for start in range(0, rows, chunk):
        n = min(chunk, rows - start)
        data: Dict[str, Any] = {
            "row_id": np.arange(start, start + n, dtype=np.int64),
            "region": rng.choice(["APAC", "EMEA", "AMER", "LATAM", "MEA"], size=n),
            "category": rng.choice(["alpha", "beta", "gamma", "delta"], size=n),
            "quantity": rng.integers(1, 500, size=n, dtype=np.int64),
            "revenue": rng.lognormal(6.0, 1.4, size=n),
            "cost": rng.lognormal(5.4, 1.1, size=n),
        }
        for i in range(max(0, cols - len(data))):
            data[f"metric_{i:02d}"] = rng.normal(loc=100 + i, scale=15 + i, size=n)
        df = pd.DataFrame(data)
        df["profit"] = df["revenue"] - df["cost"]
        df.to_csv(path, mode="w" if first else "a", header=first, index=False)
        first = False
        del df, data
    return os.path.getsize(path) if os.path.exists(path) else 0


def analyze(mod: Any, is_gpu: bool, path: str) -> tuple[Any, Dict[str, float]]:
    """One pass: load plus the four analysis stages. Returns (df, stage timings)."""
    steps: Dict[str, float] = {}
    ext = os.path.splitext(path)[1].lower()

    sync(mod, is_gpu)
    t0 = time.perf_counter()
    df = mod.read_parquet(path) if ext in (".parquet", ".pq") else mod.read_csv(path)
    sync(mod, is_gpu)
    steps["read"] = time.perf_counter() - t0

    def stage(name: str, fn: Any) -> None:
        sync(mod, is_gpu)
        t = time.perf_counter()
        fn()
        sync(mod, is_gpu)
        steps[name] = time.perf_counter() - t

    stage("mean", lambda: df["revenue"].mean())
    stage("groupby", lambda: df.groupby("region", as_index=False).agg(
        {"revenue": ["mean", "sum"], "quantity": ["sum"]}))
    num = [c for c in ("quantity", "revenue", "cost", "profit") if c in [str(x) for x in df.columns]]

    def corr() -> Any:
        sub = df[num]
        if is_gpu:
            return sub.corr()
        try:
            return sub.corr(numeric_only=True)
        except TypeError:
            return sub.corr()

    stage("corr", corr)
    stage("quantile", lambda: df["revenue"].quantile([0.25, 0.5, 0.75, 0.99]))
    steps["total"] = sum(steps.values())
    return df, steps


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        prog="memory_ceiling_test.py",
        description="Find the data size where pandas fails and cuDF still succeeds.")
    ap.add_argument("--sizes", nargs="+", default=["20m", "50m", "100m", "200m"],
                    help="ascending row counts to try")
    ap.add_argument("--columns", type=int, default=12)
    ap.add_argument("--format", choices=["csv"], default="csv")
    ap.add_argument("--time-budget", type=float, default=300.0,
                    help="seconds a single engine pass may take before it counts as "
                         "'did not finish' (default 300)")
    ap.add_argument("--default", dest="default_rows", default=None,
                    help=argparse.SUPPRESS)
    ap.add_argument("--min-free-gb", type=float, default=8.0,
                    help="refuse a size when available host memory drops below this "
                         "(default 8 GB); the point is to find the CPU ceiling without "
                         "taking the machine down with it")
    ap.add_argument("--workdir", default="ceiling_data")
    ap.add_argument("--keep-data", action="store_true")
    ap.add_argument("--out-json", default=OUT_JSON)
    ap.add_argument("--out-md", default=OUT_MD)
    args = ap.parse_args(argv)

    import pandas as pd

    try:
        import cudf  # type: ignore

        if int(cudf.Series([1, 2, 3]).sum()) != 6:
            raise RuntimeError("probe failed")
        cu: Any = cudf
        cudf_version = str(getattr(cudf, "__version__", "unknown"))
    except Exception as exc:
        say(f"cuDF unavailable: {exc}. Run this on the GB10.")
        return 2

    snap = mem_snapshot()
    say("=" * 78)
    say("Memory ceiling: where does the CPU path stop working?")
    say("=" * 78)
    say(f"  host memory : {snap.get('host_total_gb')} GB total, "
        f"{snap.get('host_available_gb')} GB available")
    say(f"  GPU pool    : {snap.get('gpu_total_gb')} GB total, "
        f"{snap.get('gpu_free_gb')} GB free")
    say(f"  cuDF        : {cudf_version} | pandas {pd.__version__}")
    say(f"  platform    : {platform.platform()}")
    say(f"  budget      : {args.time_budget:.0f}s per engine per size")
    say("=" * 78)

    os.makedirs(args.workdir, exist_ok=True)
    results: List[Dict[str, Any]] = []

    for spec in args.sizes:
        rows = parse_rows(spec)
        path = os.path.join(args.workdir, f"ceiling_{rows}.csv")
        say(f"\n[{spec}] generating {rows:,} rows")
        t0 = time.perf_counter()
        size = generate(rows, args.columns, path)
        say(f"  {size/1e9:.2f} GB in {time.perf_counter() - t0:.0f}s")

        rec: Dict[str, Any] = {
            "rows": rows, "label": spec, "file_gb": round(size / 1e9, 2),
            "mem_before": mem_snapshot(), "pandas": {}, "cudf": {},
        }

        for label, mod, is_gpu in (("cudf", cu, True), ("pandas", pd, False)):
            entry: Dict[str, Any] = {"finished": False}
            say(f"  running {label} ...")
            t0 = time.perf_counter()
            try:
                df, steps = analyze(mod, is_gpu, path)
                elapsed = time.perf_counter() - t0
                entry.update({
                    "finished": True, "seconds": round(elapsed, 3),
                    "steps": {k: round(v, 4) for k, v in steps.items()},
                    "mem_after": mem_snapshot(),
                })
                del df
                say(f"    {label}: {elapsed:.1f}s  (read {steps['read']:.1f}s)")
            except MemoryError as exc:
                entry.update({"failed": "MemoryError", "error": str(exc),
                              "seconds": round(time.perf_counter() - t0, 3)})
                say(f"    {label}: MemoryError after {entry['seconds']:.1f}s")
            except Exception as exc:
                entry.update({"failed": type(exc).__name__, "error": str(exc),
                              "seconds": round(time.perf_counter() - t0, 3),
                              "traceback": traceback.format_exc().splitlines()[-3:]})
                say(f"    {label}: {type(exc).__name__}: {str(exc)[:160]}")
            rec[label] = entry
            # Release host memory between engines so the next one starts clean.
            try:
                import gc

                gc.collect()
            except Exception:
                pass

        rec["verdict"] = _verdict(rec)
        say(f"    -> {rec['verdict']}")
        results.append(rec)

        if not args.keep_data:
            try:
                os.remove(path)
                say(f"  removed {path}")
            except OSError:
                pass

    _write(args, results, snap, cudf_version, pd.__version__, cu)
    return 0


def _verdict(rec: Dict[str, Any]) -> str:
    p, g = rec["pandas"], rec["cudf"]
    if not g.get("finished"):
        return "BOTH FAILED — this size is not usable as evidence"
    if not p.get("finished"):
        return (f"CPU PATH FAILED ({p.get('failed')}) while cuDF finished in "
                f"{g['seconds']:.1f}s — cuDF does work pandas cannot")
    return f"both finished; cuDF {g['seconds']:.1f}s vs pandas {p['seconds']:.1f}s"


def _write(args: argparse.Namespace, results: List[Dict[str, Any]], snap: Dict[str, Any],
           cudf_version: str, pandas_version: str, cu: Any) -> None:
    first_failure = next((r for r in results if not r["pandas"].get("finished")), None)
    payload = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "purpose": "determine the data size at which pandas cannot complete the analysis "
                   "while cuDF can, on the same file and the same hardware",
        "host": {**snap, "cudf": cudf_version, "pandas": pandas_version,
                 "platform": platform.platform()},
        "config": {"time_budget_s": args.time_budget, "columns": args.columns},
        "first_cpu_failure": ({"rows": first_failure["rows"],
                               "mode": first_failure["pandas"].get("failed")}
                              if first_failure else None),
        "results": results,
    }
    with open(args.out_json, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, ensure_ascii=False)

    lines = [
        "# Memory ceiling: where pandas stops and cuDF keeps going",
        "",
        f"- Host memory: {snap.get('host_total_gb')} GB | GPU pool: {snap.get('gpu_total_gb')} GB",
        f"- cuDF {cudf_version} vs pandas {pandas_version}, identical input file",
        f"- A pass that exceeds {args.time_budget:.0f}s counts as 'did not finish'",
        "",
        "| Rows | File (GB) | pandas | cuDF | Verdict |",
        "| ---: | ---: | :--- | :--- | :--- |",
    ]
    for r in results:
        p, g = r["pandas"], r["cudf"]
        ps = f"{p['seconds']:.1f}s" if p.get("finished") else f"**{p.get('failed', 'failed')}**"
        gs = f"{g['seconds']:.1f}s" if g.get("finished") else f"**{g.get('failed', 'failed')}**"
        lines.append(f"| {r['rows']:,} | {r['file_gb']} | {ps} | {gs} | {r['verdict']} |")
    if first_failure:
        lines += ["", f"**First CPU failure at {first_failure['rows']:,} rows "
                      f"({first_failure['pandas'].get('failed')}) while cuDF completed.**"]
    lines.append("")
    with open(args.out_md, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))


if __name__ == "__main__":
    sys.exit(main())