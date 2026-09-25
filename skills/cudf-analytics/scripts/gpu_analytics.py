#!/usr/bin/env python3
"""
GPU-accelerated dataset analytics for the `cudf-analytics` Agent Skill.

Runs statistical analysis on a local tabular file using cuDF (RAPIDS) on the GPU,
with an automatic, transparent fallback to pandas when no GPU / cuDF is available.

Design rules
------------
* Read-only. The user's dataset is never modified or written to.
* Exact aggregates over the whole file (optionally limited with --limit), never a
  sample-based estimate.
* The engine actually used is always reported in the JSON output. A caller must
  never claim GPU acceleration unless ``engine == "cudf"``.
* Any cuDF op that is unimplemented or fails falls back to pandas locally instead
  of failing the whole request.

Usage
-----
    python gpu_analytics.py --input sales.csv --op auto
    python gpu_analytics.py --input sales.csv --op groupby --by region \
        --agg "revenue:sum,mean|units:sum" --top-k 10
    python gpu_analytics.py --input sales.csv --op corr --method spearman
    python gpu_analytics.py --input sales.csv --op outliers --columns revenue

Exit codes: 0 success, 2 usage error, 3 unexpected failure.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
import traceback
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

# --------------------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------------------

SUPPORTED_EXTS = {".csv", ".txt", ".tsv", ".parquet", ".pq", ".jsonl", ".ndjson", ".json", ".xlsx", ".xls"}
AGG_FUNCS = {"count", "size", "sum", "mean", "min", "max", "std", "median", "nunique", "first", "last"}
VARIANCE_FUNCS = {"std", "var"}
EXACT_CORR_METHODS = {"pearson", "kendall", "spearman"}
VALID_OPS = ("auto", "profile", "summary", "groupby", "corr", "outliers")

_IQR_TEXT = "IQR"


def _log(msg: str) -> None:
    """Diagnostics go to stderr so stdout stays pure JSON."""
    print(f"[cudf-analytics] {msg}", file=sys.stderr, flush=True)


class OpNotSupported(Exception):
    """The current engine cannot do this; caller should retry on pandas."""


# --------------------------------------------------------------------------------------
# Engine detection
# --------------------------------------------------------------------------------------


@dataclass
class Engine:
    name: str                    # "cudf" or "pandas"
    mod: Any
    version: str = "unknown"
    gpu_name: Optional[str] = None
    reason: Optional[str] = None  # why we are not on the GPU

    @property
    def is_gpu(self) -> bool:
        return self.name == "cudf"

    def read(self, path: str, usecols: Optional[Sequence[str]] = None,
             nrows: Optional[int] = None) -> Any:
        return _read_table(self.mod, path, usecols=usecols, nrows=nrows)


_ENGINE: Optional[Engine] = None

# Where the GPU stops losing. The deciding quantity is the file's SIZE IN BYTES, not its row
# count, because the cost being amortised is dominated by parsing bytes. Row count was the wrong
# axis: the same 20M rows measured 1.57x at 0.88 GB and 2.18x at 3.04 GB, which no row-based
# threshold can express.
#
# Width still matters, because a wide row of 17-digit floats costs the CPU more per byte than a
# short integer row does. Measured crossover, both engines fresh, --op auto:
#
#   narrow  50 B/row: CPU wins at 0.217 GB (0.78x), GPU wins at 0.348 GB (1.03x) and 0.881 GB (1.52x)
#   wide   152 B/row: CPU wins at 0.301 GB (0.85x), GPU wins at 0.604 GB (1.27x)
#
# So ~0.40 GB is the highest crossover seen and ~0.22 GB the lowest, and the threshold is placed
# at the HIGH end on purpose: routing to the CPU when the GPU would have been marginally faster
# costs a few percent, while routing to the GPU when the CPU would have won pays the full ~1.5 s
# startup for nothing. Narrow rows get the conservative value because their CPU/GPU slope ratio
# is the least favourable (CPU costs only ~4.3x the GPU per byte there, against ~10x for wide rows).
CROSSOVER_BYTES = int(0.55e9)        # wide rows: crossover measured between 0.30 and 0.60 GB
CROSSOVER_BYTES_NARROW = int(0.40e9)  # narrow rows: measured between 0.22 and 0.35 GB
NARROW_BYTES_PER_ROW = 80.0          # below this, take the conservative threshold

# Kept for the row estimate's own sanity check and for callers that ask about rows; the routing
# decision above no longer depends on it.
SMALL_ROWS = 6_500_000


def _read_table(mod: Any, path: str, usecols: Optional[Sequence[str]] = None,
                nrows: Optional[int] = None) -> Any:
    ext = os.path.splitext(path)[1].lower()
    kwargs: Dict[str, Any] = {}
    if usecols:
        kwargs["usecols"] = list(usecols)
    if nrows:
        kwargs["nrows"] = nrows

    if ext in (".parquet", ".pq"):
        return mod.read_parquet(path, columns=list(usecols) if usecols else None)
    if ext in (".jsonl", ".ndjson"):
        # cuDF has no read_json; JSONL/JSON loading always goes through pandas.
        import pandas as _pd
        return _pd.read_json(path, lines=True, **kwargs)
    if ext == ".json":
        import pandas as _pd
        return _pd.read_json(path, **kwargs)
    if ext in (".xlsx", ".xls"):
        import pandas as _pd
        return _pd.read_excel(path, **kwargs)
    if ext in (".tsv", ".txt"):
        kwargs["sep"] = "\t"

    try:
        return mod.read_csv(path, **kwargs)
    except UnicodeDecodeError:
        return mod.read_csv(path, encoding="latin-1", **kwargs)


def _probe_gpu_name() -> Optional[str]:
    """Ask the driver for the GPU model. Best effort, never fatal."""
    try:
        import cupy as cp  # type: ignore
        name = cp.cuda.runtime.getDeviceProperties(0).get("name")
        if isinstance(name, bytes):
            name = name.decode("utf-8", "replace")
        return str(name) if name else None
    except Exception:
        pass
    try:
        import pynvml  # type: ignore
        pynvml.nvmlInit()
        h = pynvml.nvmlDeviceGetHandleByIndex(0)
        name = pynvml.nvmlDeviceGetName(h)
        return name.decode() if isinstance(name, bytes) else str(name)
    except Exception:
        return None


def detect_engine(force_cpu: bool = False, verbose: bool = False) -> Engine:
    """Pick cuDF when a GPU is genuinely usable, otherwise pandas. Cached."""
    global _ENGINE
    if _ENGINE is not None and not force_cpu:
        return _ENGINE

    import pandas as pd

    reason: Optional[str] = None
    engine: Optional[Engine] = None

    if not force_cpu:
        try:
            import cudf  # type: ignore

            # A real smoke op: proves the CUDA context works, not just that the
            # package imports.
            probe = cudf.Series([1, 2, 3]).sum()
            if int(probe) != 6:
                reason = f"cuDF smoke probe returned {probe!r}, expected 6"
            else:
                engine = Engine(
                    name="cudf",
                    mod=cudf,
                    version=str(getattr(cudf, "__version__", "unknown")),
                    gpu_name=_probe_gpu_name(),
                )
        except ImportError as exc:
            reason = f"cuDF is not installed in this Python environment ({exc})"
        except Exception as exc:  # CUDA driver mismatch, no device, OOM, ...
            reason = f"cuDF is installed but unusable: {type(exc).__name__}: {exc}"

    if engine is None:
        engine = Engine(
            name="pandas",
            mod=pd,
            version=str(getattr(pd, "__version__", "unknown")),
            reason=reason or ("CPU forced by --force-cpu" if force_cpu else "GPU unavailable"),
        )

    if verbose:
        _log(f"engine={engine.name} version={engine.version} gpu={engine.gpu_name} "
             f"{'reason=' + engine.reason if engine.reason else ''}")
    if not force_cpu:
        _ENGINE = engine
    return engine


def _fmt_size(nbytes: float) -> str:
    """Format a byte count so it stays readable at both ends of the range.

    Fixed GB formatting rendered a 0.4 MB file as "0.00 GB", which is useless to the model
    reading the reason and to a user reading the report.
    """
    if nbytes < 1e6:
        return f"{nbytes / 1e3:.0f} KB"
    if nbytes < 1e9:
        return f"{nbytes / 1e6:.1f} MB"
    return f"{nbytes / 1e9:.2f} GB"


def pick_engine_for(path: str, op: str, force_cpu: bool = False,
                    force_gpu: bool = False, verbose: bool = False) -> Tuple[bool, Optional[str]]:
    """Decide whether the GPU is worth using for this file, from measured numbers.

    Returns (use_gpu, reason). reason is set when the CPU was chosen deliberately, so the
    caller can explain the choice instead of appearing to have silently given up on the GPU.

    Why this exists: on the GPU path a large part of the runtime is fixed cost, not compute.
    Measured on the GB10 by decomposing it: 0.03 s bare Python start, 0.55 s to import cudf,
    0.95 s to import cudf and build one DataFrame. A 10,000-row file therefore costs 1.58 s on
    the GPU against 0.19 s on the CPU -- 8x SLOWER, and the ratio is entirely startup.

    Measured, both engines fresh, --op auto. Size in bytes is the deciding axis:

        file                    rows        size     CPU s    GPU s   ratio
        narrow  50 B/row     5,000,000   0.217 GB    2.21     2.85   0.78x  CPU
        narrow  50 B/row     8,000,000   0.348 GB    3.35     3.25   1.03x  GPU
        narrow  50 B/row    20,000,000   0.881 GB    8.19     5.40   1.52x  GPU
        wide   152 B/row     2,000,000   0.301 GB    2.35     2.76   0.85x  CPU
        wide   152 B/row     4,000,000   0.604 GB    4.66     3.67   1.27x  GPU
        wide   150 B/row    20,000,000   3.038 GB   21.61     9.92   2.18x  GPU
        wide   152 B/row    40,000,000   6.082 GB   44.59    16.84   2.65x  GPU

    Two things this table says that an earlier row-based threshold got wrong:

    1. The axis is BYTES. The same 20M rows measured 1.52x at 0.88 GB and 2.18x at 3.04 GB, so
       row count cannot express the decision on its own.
    2. Width still shifts the crossover, because a 17-digit float column costs the CPU more per
       byte than a short integer column. Measured crossover is 0.22-0.35 GB for narrow rows and
       0.30-0.60 GB for wide ones. Narrow rows therefore get the more conservative threshold,
       their CPU/GPU slope ratio being the least favourable (~4.3x per byte against ~10x).

    Thresholds sit at the HIGH end of each measured band on purpose: routing to the CPU when the
    GPU would have been marginally faster costs a few percent, while routing to the GPU when the
    CPU would have won pays the full ~1.5 s startup for nothing.
    """
    if force_cpu:
        return False, "CPU forced by --force-cpu"
    if force_gpu:
        return True, None

    size = None
    try:
        size = os.path.getsize(path)
    except OSError:
        pass
    if size is None:
        return True, None

    rows = _estimate_rows(path)
    per_row = (size / rows) if (rows and rows > 0) else None
    narrow = per_row is not None and per_row < NARROW_BYTES_PER_ROW
    threshold = CROSSOVER_BYTES_NARROW if narrow else CROSSOVER_BYTES

    if size < threshold:
        shape = (f"about {per_row:.0f} bytes per row over roughly {rows:,} rows"
                 if per_row is not None else "this file")
        return False, (
            f"file is {_fmt_size(size)} ({shape}), below the {_fmt_size(threshold)} crossover "
            f"measured on this hardware. The GPU is slower at this size because most of its "
            f"runtime is fixed startup cost (~1.5 s) rather than compute, and the CPU path wins "
            f"on bytes parsed per second. Pass force_gpu=true to compare anyway."
        )
    if verbose:
        _log(f"{_fmt_size(size)} >= {_fmt_size(threshold)} crossover"
             f"{f' at {per_row:.0f} B/row' if per_row else ''}; using the GPU")
    return True, None


def _estimate_rows(path: str, sample_bytes: int = 1 << 20) -> Optional[int]:
    """Estimate a CSV's row count from a prefix of the file.

    Deliberately an estimate: counting rows exactly would cost a full read, which is the very
    thing the engine is trying to avoid paying twice. A prefix is enough to place a file on
    either side of the crossover, and file size is checked first as a cheaper and more reliable
    signal.
    """
    try:
        with open(path, "rb") as fh:
            head = fh.read(sample_bytes)
            if not head:
                return 0
            total = os.fstat(fh.fileno()).st_size
    except OSError:
        return None

    newlines = head.count(b"\n")
    if newlines < 2:
        return None                     # single-line or exotic file; let the size check decide
    # Lines in the prefix, minus the header, extrapolated by byte ratio.
    mean_line = len(head) / newlines
    if mean_line <= 0:
        return None
    return int((total / mean_line) - 1)


def sync_device(eng: Engine) -> None:
    """Make sure queued GPU work has finished before reading a wall-clock time."""
    if not eng.is_gpu:
        return
    try:
        import cupy as cp  # type: ignore

        cp.cuda.Stream.null.synchronize()
        return
    except Exception:
        pass
    try:
        import numba.cuda  # type: ignore

        numba.cuda.synchronize()
    except Exception:
        # Last resort: force a device->host copy, which serializes the stream.
        try:
            int(eng.mod.Series([1]).sum())
        except Exception:
            pass


# --------------------------------------------------------------------------------------
# Small strict helpers (cudf / pandas compatible)
# --------------------------------------------------------------------------------------


def _is_num(dt: Any) -> bool:
    """True for numeric dtypes, excluding bools (which are ints to numpy)."""
    try:
        import pandas as pd

        if isinstance(dt, str):
            low = dt.lower()
            return any(tok in low for tok in ("int", "float", "double", "decimal"))
        if pd.api.types.is_bool_dtype(dt):
            return False
        return bool(pd.api.types.is_numeric_dtype(dt))
    except Exception:
        return False


def _numeric_cols(df: Any) -> List[str]:
    out: List[str] = []
    for c in df.columns:
        try:
            if _is_num(df[c].dtype):
                out.append(str(c))
        except Exception:
            continue
    return out


def _col(df: Any, name: str) -> Any:
    """Column access that is strict about exactly-equal matches."""
    if name not in [str(c) for c in df.columns]:
        raise KeyError(f"column {name!r} not found; available: {[str(c) for c in df.columns]}")
    return df[name]


def _nan_to_none(v: Any) -> Any:
    if v is None:
        return None
    if isinstance(v, float):
        return None if (math.isnan(v) or math.isinf(v)) else v
    return v


def _native_scalar(v: Any) -> Any:
    if v is None:
        return None
    try:
        import pandas as pd

        if v is pd.NaT:
            return None
    except Exception:
        pass
    if isinstance(v, bool):
        return v
    if isinstance(v, float):
        return _nan_to_none(v)
    if isinstance(v, int):
        return int(v)
    if isinstance(v, str):
        return v
    for attr in ("item", "to_numpy"):
        if hasattr(v, attr):
            try:
                got = getattr(v, attr)()
                if hasattr(got, "tolist"):
                    got = got.tolist()
                return _native_scalar(got)
            except Exception:
                continue
    return str(v)


def _col_list(series: Any, limit: Optional[int] = None) -> List[Any]:
    """A column (or index) as plain Python values. cudf honors .head() before .to_numpy()."""
    if limit is not None:
        try:
            series = series.head(limit)
        except Exception:
            pass
    try:
        vals = series.to_numpy()
    except Exception:
        vals = series
    try:
        out = vals.tolist()
    except Exception:
        out = list(vals)
    return [_native_scalar(x) for x in out]


def _quantile(s: Any, q: float, is_gpu: bool) -> Optional[float]:
    if is_gpu:
        try:
            s2 = s.dropna()
            if len(s2) == 0:
                return None
            method = s2.quantile([q])
            val = method.iloc[0] if hasattr(method, "iloc") else method[0]
            return _native_scalar(val)
        except Exception as exc:
            _log(f"cuDF quantile failed for q={q} ({type(exc).__name__}), using pandas")
    s2 = s.dropna()
    if len(s2) == 0:
        return None
    method = s2.quantile(q)
    return _native_scalar(method)


def _to_dict(rec: Any) -> Dict[str, Any]:
    """One result row -> JSON-safe dict, regardless of engine."""
    if hasattr(rec, "to_pandas"):
        rec = rec.to_pandas()
    if hasattr(rec, "to_dict"):
        try:
            return {str(k): _nan_to_none(v) for k, v in rec.to_dict().items()}
        except Exception:
            pass
    return {str(k): _native_scalar(v) for k, v in dict(rec).items()}


def _frame_records(df: Any, limit: int) -> List[Dict[str, Any]]:
    try:
        head = df.head(limit)
    except Exception:
        head = df
    if hasattr(head, "to_pandas"):
        head = head.to_pandas()
    try:
        rows = head.to_dict(orient="records")
    except Exception:
        rows = []
    return [{str(k): _nan_to_none(v) for k, v in r.items()} for r in rows]


def _make_timer(eng: Engine) -> Callable[[], float]:
    """Return a callable giving elapsed seconds with the device synchronized."""
    t0 = time.perf_counter()
    sync_device(eng)
    t_start = time.perf_counter()

    def elapsed() -> float:
        sync_device(eng)
        return time.perf_counter() - t_start

    return elapsed


# --------------------------------------------------------------------------------------
# Operations
# --------------------------------------------------------------------------------------


def op_profile(df: Any, eng: Engine, args: argparse.Namespace) -> Dict[str, Any]:
    n_rows = int(len(df))
    n_cols = int(len(df.columns))
    info = {
        "rows": n_rows,
        "columns_count": n_cols,
        "columns": [str(c) for c in df.columns],
        "dtypes": {str(c): str(df[c].dtype) for c in df.columns},
        "numeric_columns": _numeric_cols(df),
        "memory_usage_bytes": int(df.memory_usage(deep=False).sum()) if n_cols else 0,
    }
    # null counts: cuDF supports isnull().sum() but not .isna() historically.
    try:
        nulls = df.isnull().sum()
        info["null_counts"] = {str(k): int(v) for k, v in _to_dict(nulls).items()}
    except Exception as exc:
        _log(f"null counts unavailable ({type(exc).__name__})")
        info["null_counts"] = {}
    info["preview"] = _frame_records(df, int(args.preview_n))
    return info


def _describe_stats(s: Any, is_gpu: bool) -> Dict[str, Any]:
    """count/mean/std/min/q1/median/q3/max computed without relying on describe()."""
    import pandas as pd

    stats: Dict[str, Any] = {"count": 0, "mean": None, "std": None, "min": None,
                             "q1": None, "median": None, "q3": None, "max": None}
    clean = s.dropna()
    n = int(len(clean))
    stats["count"] = n
    if n == 0:
        return stats
    for label, fn in (
        ("mean", lambda: _native_scalar(clean.mean())),
        ("std", lambda: _native_scalar(clean.std())),
        ("min", lambda: _native_scalar(clean.min())),
        ("max", lambda: _native_scalar(clean.max())),
    ):
        try:
            stats[label] = _nan_to_none(fn())
        except Exception as exc:
            if is_gpu:
                raise OpNotSupported(f"cuDF cannot compute {label}: {exc}") from exc
            _log(f"{label} failed: {exc}")
    stats["q1"] = _quantile(clean, 0.25, is_gpu)
    stats["median"] = _quantile(clean, 0.50, is_gpu)
    stats["q3"] = _quantile(clean, 0.75, is_gpu)
    return stats


def op_summary(df: Any, eng: Engine, args: argparse.Namespace) -> Dict[str, Any]:
    numeric = _numeric_cols(df)
    wanted = _resolve_columns(args, numeric)
    if not wanted:
        return {"columns": [], "stats": {}, "note": "no numeric columns to summarize"}
    out: Dict[str, Any] = {}
    for name in wanted:
        out[name] = _describe_stats(_col(df, name), eng.is_gpu)
        out[name]["nulls"] = int(df[name].isnull().sum())
    return {"columns": wanted, "stats": out}


def op_groupby(df: Any, eng: Engine, args: argparse.Namespace) -> Dict[str, Any]:
    import pandas as pd

    by = args.by
    if not by:
        raise ValueError("--op groupby requires --by")
    if by not in [str(c) for c in df.columns]:
        # Name the one-column limitation explicitly. Without it the caller sees a list of
        # real columns that contains both halves of what it asked for, concludes it must have
        # made a typo, and retries the same comma-joined value. Observed repeatedly.
        hint = ""
        if "," in str(by):
            parts = [p.strip() for p in str(by).split(",") if p.strip()]
            known = [p for p in parts if p in [str(c) for c in df.columns]]
            if len(known) >= 2:
                hint = (f" --by takes exactly ONE column, but {known} were joined with a comma."
                        f" Run one groupby per column instead: "
                        + " then ".join(f'--by {k}' for k in known) + ".")
            elif known:
                hint = (f" --by takes exactly ONE column; {known} is valid but "
                        f"{[p for p in parts if p not in known]} is not.")
        raise ValueError(
            f"--by column {by!r} not found; available: {[str(c) for c in df.columns]}.{hint}")
    spec = _parse_agg(args.agg, _numeric_cols(df))

    g = df.groupby(by, dropna=False, as_index=False)
    notes: List[str] = []

    # Function names are passed as strings. Verified on cuDF 25.10: every documented
    # alias (count, size, sum, mean, min, max, std, median, nunique, first, last)
    # works directly, so - importantly - do NOT pass pd.Series.nunique as a callable:
    # cuDF raises "AttributeError: 'Aggregation' object has no attribute 'dtype'".
    agg_map = {col: list(funcs) for col, funcs in spec.items()}
    try:
        res = g.agg(agg_map)
    except Exception as exc:
        if eng.is_gpu:
            raise OpNotSupported(f"cuDF groupby.agg failed: {type(exc).__name__}: {exc}") from exc
        try:
            res = g.agg({col: list(funcs) for col, funcs in spec.items()})
        except Exception as exc2:
            raise ValueError(f"groupby aggregation failed: {exc2}") from exc2

    if eng.is_gpu and VARIANCE_FUNCS & {f for fs in spec.values() for f in fs}:
        # cuDF computes std/var with ddof=0; pandas defaults to ddof=1.
        notes.append("std/var on cuDF use ddof=0, pandas uses ddof=1 (small numeric difference)")
    res = _flatten_columns(res)

    # Sort by the first aggregated metric, skipping the group-key column, so
    # "top_k" means the biggest group by the leading metric.
    metric_cols = [c for c in res.columns if str(c) != by]
    sort_col = metric_cols[0] if metric_cols else next(iter(res.columns), None)
    if sort_col is not None:
        try:
            res = res.sort_values(sort_col, ascending=False, kind="stable")
        except Exception:
            try:
                res = res.sort_values(sort_col, ascending=False)
            except Exception:
                pass
    top = _frame_records(res, int(args.top_k))
    return {
        "by": by,
        "agg": args.agg,
        "groups": int(len(res)),
        "sorted_by": sort_col,
        "top_k": top,
        "notes": notes,
    }


def _flatten_columns(res: Any) -> Any:
    """collapsed (col, func) MultiIndex -> col__func."""
    cols = getattr(res, "columns", None)
    if cols is None:
        return res
    flat: List[str] = []
    changed = False
    for c in cols:
        if isinstance(c, tuple):
            parts = [str(p) for p in c if p != ""]
            flat.append("__".join(parts))
            changed = True
        else:
            flat.append(str(c))
    if changed:
        res = res.copy()
        res.columns = flat
    else:
        res.columns = flat
    return res


def _parse_agg(agg: Optional[str], numeric: Sequence[str]) -> Dict[str, List[str]]:
    """`col:sum,mean|col2:max` -> {'col': ['sum','mean'], 'col2': ['max']}."""
    if not agg or agg == _IQR_TEXT:
        if not numeric:
            raise ValueError("no numeric columns available for aggregation")
        return {numeric[0]: ["mean", "sum"]}
    spec: Dict[str, List[str]] = {}
    for chunk in agg.split("|"):
        chunk = chunk.strip()
        if not chunk:
            continue
        if ":" not in chunk:
            raise ValueError(f"bad --agg chunk {chunk!r}; expected 'column:func[,func]'")
        col, funcs = chunk.split(":", 1)
        col = col.strip()
        fns = [f.strip().lower() for f in funcs.split(",") if f.strip()]
        bad = [f for f in fns if f not in AGG_FUNCS]
        if bad:
            # Name the offending functions precisely, and say what to do instead when the
            # caller is reaching for something group-by cannot express. Without the second
            # part a caller sees only a list of valid function names, concludes it mis-typed,
            # and repeats the same request -- observed: a caller asked for revenue:corr three
            # times across two tools, then exhausted its turn budget with no answer.
            hint = ""
            if any(b in ("corr", "correlation", "pearson", "spearman", "kendall")
                   for b in bad):
                hint = (" Per-group correlation is not something groupby can compute. Use "
                        "op='corr' for the correlation across the whole dataset, and use "
                        "groupby with mean/std to compare how columns differ between groups.")
            elif any(b in ("var", "variance") for b in bad):
                hint = " 'var' is not available; use 'std'."
            elif any(b in ("avg", "average") for b in bad):
                hint = " 'avg' is not available; use 'mean'."
            elif any(b in ("q1", "q3", "quantile", "percentile") for b in bad):
                hint = (" Quantiles are not available in groupby; use op='summary' for "
                        "quartiles across the dataset.")
            raise ValueError(
                f"unsupported agg func(s) {sorted(set(bad))} for column(s) "
                f"{sorted({c for c in spec} | {col})}; "
                f"choose from {sorted(AGG_FUNCS)}. Syntax is col:func[,func]|col2:func -- "
                f"the function comes after the colon.{hint}")
        if not fns:
            raise ValueError(f"no functions given for column {col!r}")
        spec[col] = fns
    if not spec:
        raise ValueError("--agg produced an empty specification")
    return spec


def op_corr(df: Any, eng: Engine, args: argparse.Namespace) -> Dict[str, Any]:
    import pandas as pd

    method = (args.method or "pearson").lower()
    if method not in EXACT_CORR_METHODS:
        raise ValueError(f"--method must be one of {sorted(EXACT_CORR_METHODS)}")

    numeric = _numeric_cols(df)
    wanted = _resolve_columns(args, numeric)
    if len(wanted) < 2:
        return {"columns": wanted, "matrix": {}, "pairs": [],
                "note": "need at least 2 numeric columns for a correlation matrix"}

    note: Optional[str] = None
    if method != "pearson":
        if eng.is_gpu:
            raise OpNotSupported(f"cuDF only computes pearson correlation, not {method}")
        note = "pandas is O(n^2)-heavy for rank correlations; large files may be slow"

    sub = df[wanted]
    if eng.is_gpu:
        if method != "pearson":
            raise OpNotSupported(f"cuDF only computes pearson correlation, not {method}")
        try:
            cm = sub.corr()
        except Exception as exc:
            raise OpNotSupported(f"cuDF DataFrame.corr failed: {exc}") from exc
    else:
        kwargs = {"method": method}
        if _pandas_uses_numeric_only():
            kwargs["numeric_only"] = True
        cm = sub.corr(**kwargs)

    cols = [str(c) for c in cm.columns]
    matrix = {str(r): {str(c): _nan_to_none(v) for c, v in row.items()}
              for r, row in _to_dict(cm).items()}

    pairs: List[Dict[str, Any]] = []
    for i, a in enumerate(cols):
        for b in cols[i + 1:]:
            v = matrix.get(a, {}).get(b)
            if v is not None:
                pairs.append({"a": a, "b": b, "corr": v, "abs": abs(v)})
    pairs.sort(key=lambda p: p["abs"], reverse=True)

    return {
        "method": method,
        "columns": cols,
        "matrix": matrix,
        "pairs": pairs[: int(args.top_k)],
        "note": note,
    }


def _pandas_uses_numeric_only() -> bool:
    import pandas as pd

    try:
        major = int(str(pd.__version__).split(".")[0])
    except Exception:
        return True
    return major < 3


def op_outliers(df: Any, eng: Engine, args: argparse.Namespace,
                max_columns: Optional[int] = None) -> Dict[str, Any]:
    import pandas as pd

    k = float(args.iqr_k)
    numeric = _numeric_cols(df)
    wanted = _resolve_columns(args, numeric, limit=max_columns)
    if not wanted:
        return {"columns": [], "results": {}, "note": "no numeric columns to scan"}

    results: Dict[str, Any] = {}
    for name in wanted:
        s = df[name]
        q1 = _quantile(s, 0.25, eng.is_gpu)
        q3 = _quantile(s, 0.75, eng.is_gpu)
        if q1 is None or q3 is None:
            results[name] = {"note": "column is entirely null", "count": 0}
            continue
        iqr = float(q3) - float(q1)
        low = float(q1) - k * iqr
        high = float(q3) + k * iqr
        # Fence-tie tolerance.
        #
        # Q1/Q3 are computed by cuDF on the GPU and by pandas on the CPU, and the two
        # accumulate floating-point error differently. Measured on a real dataset
        # (Voltage, 2M rows): pandas produced low = 233.14000000000004 and cuDF produced
        # 233.14, a gap of 4e-14 -- yet the column contains the exact value 233.14. A bare
        # `s < low` therefore counted 304 extra outliers under pandas, so the same query
        # returned a different answer depending on which engine ran it.
        #
        # Values within a relative tolerance of a fence are ties: the comparison cannot
        # reliably decide them, so they are not called outliers. This makes the result
        # reproducible across engines instead of depending on rounding luck.
        tol = 1e-9 * max(abs(low), abs(high), 1.0)
        val = pd.to_numeric(s, errors="coerce")
        try:
            delta_low = low - val
            delta_high = val - high
            below = (val < low) & (delta_low > tol)
            above = (val > high) & (delta_high > tol)
            mask = below | above
            sub = df[mask]
            count = int(len(sub))
            top = sub[list(wanted)].head(int(args.top_k)) if len(wanted) > 1 \
                else sub[[name]].head(int(args.top_k))
            records = _frame_records(top, int(args.top_k))
        except Exception as exc:
            if eng.is_gpu:
                raise OpNotSupported(f"cuDF outlier filtering failed: {exc}") from exc
            raise
        try:
            n_valid = int(val.notnull().sum())
        except Exception:
            n_valid = int(len(s)) - count
        # Report how many values landed on a fence, so a boundary-heavy column is visible.
        try:
            n_ties = int((val.sub(low).abs().le(tol) | val.sub(high).abs().le(tol)).sum())
        except Exception:
            n_ties = 0
        results[name] = {
            "q1": float(q1),
            "q3": float(q3),
            "iqr": iqr,
            "k": k,
            "lower_bound": low,
            "upper_bound": high,
            "count": count,
            "pct": round(100.0 * count / max(n_valid, 1), 4),
            "examples": records,
        }
        if n_ties:
            # Boundary ties are excluded from `count`; say so rather than hiding it.
            results[name]["fence_ties_excluded"] = n_ties
            results[name]["note"] = (
                f"{n_ties} value(s) sit on an IQR fence within a relative tolerance of "
                f"{tol:.3g} and are not counted as outliers, so the count does not depend "
                f"on engine rounding."
            )
    return {"columns": wanted, "results": results}


def _resolve_columns(args: argparse.Namespace, numeric: Sequence[str],
                     limit: Optional[int] = None) -> List[str]:
    """Honor --columns when given, else take every numeric column (optionally capped)."""
    cap = int(limit if limit is not None else args.max_columns)
    if args.select:
        wanted = [c.strip() for c in str(args.select).split(",") if c.strip()]
        missing = [c for c in wanted if c not in numeric]
        if missing:
            _log(f"ignoring non-numeric or missing --columns entries: {missing}")
        wanted = [c for c in wanted if c in numeric]
        if not wanted:
            raise ValueError(f"none of the requested columns are numeric; numeric columns: {list(numeric)}")
        return wanted[:cap]
    return list(numeric)[:cap]


# --------------------------------------------------------------------------------------
# Dispatch with GPU -> CPU fallback
# --------------------------------------------------------------------------------------

Ops = Dict[str, Callable[..., Dict[str, Any]]]
OPS: Ops = {
    "profile": op_profile,
    "summary": op_summary,
    "groupby": op_groupby,
    "corr": op_corr,
    "outliers": op_outliers,
}


def execute(eng: Engine, load: Callable[[Engine], Any], op: str,
            args: argparse.Namespace) -> Tuple[Dict[str, Any], Engine, float, Optional[str], int]:
    """
    Load the table and run `op`, retrying on pandas if cuDF cannot do it.

    Returns (payload, engine_used, elapsed_s, fallback_reason, rows).
    """
    started = time.perf_counter()

    def attempt(engine: Engine) -> Dict[str, Any]:
        df = load(engine)
        t_elapsed = _make_timer(engine)
        if op == "auto":
            payload = {
                "profile": op_profile(df, engine, args),
                "summary": op_summary(df, engine, args),
                "outliers": op_outliers(df, engine, args, max_columns=args.auto_columns),
            }
        else:
            payload = OPS[op](df, engine, args)
        dt = t_elapsed()
        payload["rows_scanned"] = int(len(df))
        payload["compute_seconds"] = round(dt, 6)
        return payload

    try:
        payload = attempt(eng)
        return payload, eng, time.perf_counter() - started, None, payload["rows_scanned"]
    except OpNotSupported as exc:
        if not eng.is_gpu:
            raise
        reason = f"{op} not supported by cuDF ({exc}); re-ran on pandas"
        _log(reason)
        cpu = detect_engine(force_cpu=True)
        payload = attempt(cpu)
        return payload, cpu, time.perf_counter() - started, reason, payload["rows_scanned"]


# --------------------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="gpu_analytics.py",
        description="GPU-accelerated (cuDF) dataset analytics with automatic pandas fallback.",
    )
    p.add_argument("--input", required=True, help="path to a CSV/Parquet/TSV/JSONL/Excel file")
    p.add_argument("--op", default="auto", choices=VALID_OPS,
                   help="analysis to run (default: auto = profile + summary + outliers)")
    p.add_argument("--columns", dest="select", default=None,
                   help="comma-separated numeric columns to analyze (default: all numeric)")
    p.add_argument("--max-columns", type=int, default=200,
                   help="cap on numeric columns analyzed per op (default: 200)")
    p.add_argument("--auto-columns", type=int, default=12,
                   help="cap on columns scanned for outliers under --op auto (default: 12)")
    p.add_argument("--by", default=None, help="grouping column for --op groupby")
    p.add_argument("--agg", default=None,
                   help="aggregations for --op groupby, e.g. 'revenue:sum,mean|units:max'")
    p.add_argument("--method", default="pearson", help="correlation method for --op corr")
    p.add_argument("--iqr-k", type=float, default=1.5,
                   help="IQR multiplier for --op outliers (default 1.5)")
    p.add_argument("--top-k", type=int, default=20,
                   help="rows/pairs to return for groupby, corr and outliers (default: 20)")
    p.add_argument("--preview-n", type=int, default=5, help="preview rows for profile (default: 5)")
    p.add_argument("--limit", type=int, default=None,
                   help="read at most N rows (use only when the user asked for a sample)")
    p.add_argument("--usecols", default=None, help="comma-separated columns to read from the file")
    p.add_argument("--force-cpu", action="store_true", help="ignore the GPU (for A/B comparison)")
    p.add_argument("--force-gpu", action="store_true",
                   help="use the GPU even below the measured crossover (for A/B comparison)")
    p.add_argument("--engine", default="auto", choices=["auto", "cpu", "gpu"],
                   help="auto (default) routes small files to the CPU, where the GPU's fixed "
                        "startup cost makes it slower; cpu/gpu force one path")
    p.add_argument("--pretty", action="store_true", help="pretty-print the JSON output")
    p.add_argument("--verbose", action="store_true", help="log engine/fallback decisions to stderr")
    return p


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    t_start = time.perf_counter()

    path = os.path.abspath(args.input)
    if not os.path.isfile(path):
        print(json.dumps({"error": f"input file not found: {path}", "input": path}), flush=True)
        return 2

    ext = os.path.splitext(path)[1].lower()
    if ext and ext not in SUPPORTED_EXTS:
        _log(f"unrecognized extension {ext!r}; attempting to read as CSV")

    usecols = [c.strip() for c in args.usecols.split(",") if c.strip()] if args.usecols else None
    nrows = int(args.limit) if args.limit else None

    def load(engine: Engine) -> Any:
        df = engine.read(path, usecols=usecols, nrows=nrows)
        if not hasattr(df, "columns") or len(df.columns) == 0:
            raise ValueError(f"no columns could be read from {path}")
        return df

    try:
        force_cpu = bool(args.force_cpu) or (args.engine == "cpu")
        force_gpu = bool(args.force_gpu) or (args.engine == "gpu")
        route_reason: Optional[str] = None
        if not force_cpu and not force_gpu:
            use_gpu, route_reason = pick_engine_for(path, args.op, verbose=args.verbose)
            force_cpu = not use_gpu
            if args.verbose and route_reason:
                _log(f"CPU chosen: {route_reason}")
        elif force_cpu:
            route_reason = "CPU forced by the caller"
        eng = detect_engine(force_cpu=force_cpu, verbose=args.verbose)
        payload, used, elapsed, fallback, rows = execute(eng, load, args.op, args)
        # A deliberate CPU choice is not a fallback. Keeping them separate matters: a fallback
        # means the GPU failed, and reporting a routing decision as a failure would both
        # confuse the caller and make an intentional choice look like a defect.
        if route_reason and not used.is_gpu:
            fallback = None
    except (ValueError, KeyError) as exc:
        print(json.dumps({"error": str(exc), "input": path, "op": args.op}), flush=True)
        return 2
    except Exception as exc:
        print(json.dumps({
            "error": f"{type(exc).__name__}: {exc}",
            "input": path,
            "op": args.op,
            "traceback": traceback.format_exc().splitlines()[-6:],
        }), flush=True)
        return 3

    result: Dict[str, Any] = {
        "ok": True,
        "op": args.op,
        "input": path,
        "file_size_bytes": os.path.getsize(path),
        "engine": used.name,
        "engine_version": used.version,
        "gpu": used.gpu_name or ("none (CPU run)" if not used.is_gpu else "CUDA device"),
        "accelerated": used.is_gpu,
        "fallback_reason": fallback or (None if used.is_gpu else used.reason),
        "routing_reason": route_reason,
        "rows_scanned": rows,
        "op_seconds": round(elapsed, 6),
        "total_seconds": round(time.perf_counter() - t_start, 6),
    }
    result.update(payload)

    # Consistent contract: each op's data lives under its own key so a caller can
    # read result[op] without caring which op ran ("auto" already nests that way).
    if args.op != "auto":
        result = {**{k: v for k, v in result.items() if k not in payload}, args.op: payload}

    print(json.dumps(result, indent=2 if args.pretty else None, default=str, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())