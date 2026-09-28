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
import csv
from functools import lru_cache
import json
import math
import os
import sys
import time
import traceback
import cost_model
import parquet_cache
import hybrid_execution
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
    last_read: Dict[str, Any] = field(default_factory=dict)

    @property
    def is_gpu(self) -> bool:
        return self.name == "cudf"

    def read(self, path: str, usecols: Optional[Sequence[str]] = None,
             nrows: Optional[int] = None) -> Any:
        self.last_read = {}
        ext = os.path.splitext(path)[1].lower()
        if self.is_gpu and ext in (".xlsx", ".xls"):
            try:
                return hybrid_execution.read_gpu(self, path, usecols, self.last_read, nrows=nrows)
            except (hybrid_execution.HybridMemoryRefused, hybrid_execution.HybridInputChanged):
                raise
            except hybrid_execution.HybridRefused as exc:
                raise OpNotSupported(str(exc)) from exc
        if self.is_gpu and ext in (".json", ".jsonl", ".ndjson"):
            raise OpNotSupported("JSON reader is CPU-only; select pandas before loading")
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


def csv_separator(path: str) -> str:
    """Bounded, quote-aware sniff; never scan a large CSV to choose its parser."""
    return _csv_separator_cached(*hybrid_execution.identity(path))


@lru_cache(maxsize=128)
def _csv_separator_cached(path: str, size: int, modified_ns: int) -> str:
    with open(path, "r", encoding="utf-8-sig", errors="replace", newline="") as source:
        sample = source.read(65536)
    try:
        return csv.Sniffer().sniff(sample, delimiters=",;\t|").delimiter
    except csv.Error:
        return "\t" if os.path.splitext(path)[1].lower() in (".tsv", ".txt") else ","


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
        frame = _pd.read_json(path, lines=True, **({"nrows": nrows} if nrows else {}))
        return frame[list(usecols)] if usecols else frame
    if ext == ".json":
        import pandas as _pd
        frame = _pd.read_json(path)
        if nrows:
            frame = frame.head(nrows)
        return frame[list(usecols)] if usecols else frame
    if ext in (".xlsx", ".xls"):
        import pandas as _pd
        return _pd.read_excel(path, **kwargs)
    kwargs["sep"] = csv_separator(path)

    try:
        return mod.read_csv(path, **kwargs)
    except UnicodeDecodeError:
        return mod.read_csv(path, encoding="latin-1", **kwargs)


def required_read_columns(op: str, args: argparse.Namespace) -> Optional[List[str]]:
    """Project only columns that can be proven sufficient for this operation.

    A profile or an unspecified numeric-column request needs the full schema.  In
    particular, ``--columns`` selects numeric *analysis* columns, whereas a
    groupby also needs its key.  Keep the caller's explicit --usecols authoritative.
    """
    if getattr(args, "usecols", None):
        names = str(args.usecols).split(",")
    elif getattr(args, "no_auto_usecols", False):
        return None
    elif op == "groupby" and args.by and args.agg and "," not in str(args.by):
        names = [args.by]
        names.extend(chunk.partition(":")[0] for chunk in str(args.agg).split("|")
                     if ":" in chunk)
        if len(names) == 1:
            return None
    elif op in ("summary", "corr", "outliers") and args.select:
        names = str(args.select).split(",")
    else:
        return None
    return list(dict.fromkeys(name.strip() for name in names if name.strip())) or None


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
                    force_gpu: bool = False, verbose: bool = False,
                    details: Optional[Dict[str, Any]] = None,
                    calibration_file: Optional[str] = None,
                    read_columns=None) -> Tuple[bool, Optional[str]]:
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
    if details is not None:
        details.update({"policy": "measured_file_size_crossover", "operation": op})
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

    calibrated = cost_model.predict(
        (os.environ.get("GPU_ANALYSIS_CALIBRATION_FILE") if calibration_file is None
         else calibration_file),
        op=op, source=path, size=size,
    )
    if calibrated:
        cpu_s = calibrated["pandas"]["seconds"]
        gpu_s = calibrated["cudf"]["seconds"]
        # A 10% guard avoids paying GPU startup on a statistical tie.
        use_gpu = gpu_s < 0.90 * cpu_s
        if details is not None:
            details.update({"policy": "calibrated_cost_model", "file_size_bytes": size,
                            "estimated_seconds": {"pandas": cpu_s, "cudf": gpu_s},
                            "calibration_samples": {k: v["samples"] for k, v in calibrated.items()}})
        return use_gpu, (None if use_gpu else
                         f"calibrated {op} estimate: CPU {cpu_s:.2f}s, GPU {gpu_s:.2f}s; "
                         "GPU must beat CPU by at least 10% to offset model uncertainty")

    rows = _estimate_rows(path)
    # Hot Parquet operations scan decoded values, not compressed file bytes.
    if op == "session" and _ENGINE is not None and _ENGINE.is_gpu and rows:
        try:
            if os.path.splitext(path)[1].lower() in (".parquet", ".pq"):
                decoded = _decoded_numeric_bytes(path, read_columns)
                if decoded is not None and decoded >= CROSSOVER_BYTES_NARROW:
                    if details is not None:
                        details.update(policy="warm_parquet_decoded_size", estimated_rows=rows,
                                       decoded_numeric_bytes=decoded, file_size_bytes=size)
                    return True, None
        except (OSError, ValueError, TypeError, AttributeError, ImportError):
            pass
    per_row = (size / rows) if (rows and rows > 0) else None
    narrow = per_row is not None and per_row < NARROW_BYTES_PER_ROW
    threshold = CROSSOVER_BYTES_NARROW if narrow else CROSSOVER_BYTES
    if details is not None:
        details.update({
            "file_size_bytes": size,
            "estimated_rows": rows,
            "estimated_bytes_per_row": round(per_row, 2) if per_row is not None else None,
            "crossover_bytes": threshold,
        })

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


def execution_decision_record(*, mode: str, policy: str, selected_backend: str,
                              actual_backend: str, reason: str, signals: Dict[str, Any],
                              observed: Dict[str, Any], fallback_reason: Optional[str] = None,
                              estimated_seconds: Optional[float] = None
                              ) -> Dict[str, Any]:
    """A stable, factual decision trace shared by one-off and resident execution.

    Elapsed predictions appear only with a validated local per-operation fit. An admission
    headroom check is a safety threshold, not an estimate of peak memory usage.
    """
    return {
        "schema_version": 1,
        "mode": mode,
        "policy": policy,
        "selected_backend": selected_backend,
        "actual_backend": actual_backend,
        "reason": reason,
        "signals": signals,
        "estimate": {
            "elapsed_seconds": estimated_seconds,
            "peak_memory_mb": None,
            "status": "calibrated" if estimated_seconds is not None else "not_calibrated",
        },
        "observed": observed,
        "fallback_reason": fallback_reason,
    }


def _decoded_numeric_bytes(path, columns=None):
    """Cheap fixed-width Parquet footprint; unknown types stay conservative."""
    if os.path.splitext(path)[1].lower() not in (".parquet", ".pq"):
        return None
    try:
        import pyarrow.parquet as pq
        parquet = pq.ParquetFile(path)
        schema = parquet.schema_arrow
        fields = [schema.field(name) for name in columns] if columns else list(schema)
        widths = [getattr(field.type, "bit_width", 0) for field in fields]
        return parquet.metadata.num_rows * sum(widths) / 8 if widths and all(widths) else None
    except (OSError, ValueError, TypeError, AttributeError, ImportError, KeyError):
        return None


def _estimate_rows(path: str, sample_bytes: int = 1 << 20) -> Optional[int]:
    """Estimate a CSV's row count from a prefix of the file.

    Deliberately an estimate: counting rows exactly would cost a full read, which is the very
    thing the engine is trying to avoid paying twice. A prefix is enough to place a file on
    either side of the crossover, and file size is checked first as a cheaper and more reliable
    signal.
    """
    extension = os.path.splitext(path)[1].lower()
    if extension in (".parquet", ".pq"):
        try:
            import pyarrow.parquet as pq
            return pq.ParquetFile(path).metadata.num_rows
        except (OSError, ValueError, ImportError):
            return None
    if extension not in (".csv", ".tsv", ".txt"):
        return None
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


def _quartiles(s: Any, is_gpu: bool, *, already_clean: bool = False) -> Tuple[Optional[float], Optional[float], Optional[float]]:
    """Compute all three quartiles in one reduction instead of three full scans."""
    clean = s if already_clean else s.dropna()
    if len(clean) == 0:
        return None, None, None
    try:
        values = clean.quantile([0.25, 0.50, 0.75])
        return tuple(_nan_to_none(_native_scalar(values.iloc[i])) for i in range(3))
    except Exception as exc:
        if is_gpu:
            raise OpNotSupported(f"cuDF quartiles failed: {exc}") from exc
        raise


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
    # Full-frequency reductions preserve exact order statistics; the selector
    # never uses sampled answers. An environment switch retains the old path
    # for reproducible A/B tests and operational rollback.
    if os.environ.get("GPU_ANALYSIS_FREQUENCY_STATS", "1") == "0":
        return _describe_stats_native(s, is_gpu)
    from exact_statistics import frequency_describe
    try:
        return frequency_describe(s, is_gpu, _describe_stats_native)
    except hybrid_execution.HybridMemoryRefused:
        raise
    except Exception as exc:
        if is_gpu and "out of memory" in str(exc).lower():
            raise hybrid_execution.HybridMemoryRefused("frequency statistics temporary memory refused") from exc
        _log(f"frequency statistics unavailable ({type(exc).__name__}); retained native exact statistics")
        return _describe_stats_native(s, is_gpu)


def _describe_stats_native(s: Any, is_gpu: bool) -> Dict[str, Any]:
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
    stats["q1"], stats["median"], stats["q3"] = _quartiles(clean, is_gpu, already_clean=True)
    return stats


def op_summary(df: Any, eng: Engine, args: argparse.Namespace) -> Dict[str, Any]:
    numeric = _numeric_cols(df)
    wanted = _resolve_columns(args, numeric)
    if not wanted:
        return {"columns": [], "stats": {}, "note": "no numeric columns to summarize"}
    out: Dict[str, Any] = {}
    for name in wanted:
        out[name] = _describe_stats(_col(df, name), eng.is_gpu)
        # _describe_stats already counted non-null values; avoid another full
        # column scan just to derive the complement.
        out[name]["nulls"] = int(len(df) - out[name]["count"])
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


def pairwise_pearson(sub: Any, xp: Any) -> Any:
    """Exact pairwise-complete, finite Pearson; only p-by-p results leave GPU.

    Two-pass centering per pair avoids cancellation in raw moment formulas.
    Never drop a row globally or fill a missing observation with zero.
    xp is CuPy in production, NumPy in independent CPU contract tests.
    """
    arrays = []
    for c in sub.columns:
        series = sub[c].astype("float64")
        arrays.append(series.to_cupy(na_value=float("nan")) if hasattr(series, "to_cupy")
                      else xp.asarray(series.fillna(float("nan"))))
    masks = [xp.isfinite(a) for a in arrays]
    if len(sub) >= 2:
        complete = all(bool(xp.all(m)) for m in masks)
        # Shared validity is common in instrument datasets. ONLY after proving
        # every pair has exactly the same mask may we use one matrix reduction.
        same_mask = complete or all(bool(xp.all(m == masks[0])) for m in masks[1:])
        if same_mask:
            selected = arrays if complete else [a[masks[0]] for a in arrays]
            if selected[0].size >= 2:
                centered = xp.stack([a - a[0] for a in selected])
                return xp.corrcoef(centered)
    result = xp.full((len(arrays), len(arrays)), xp.nan, dtype=xp.float64)
    for i, left in enumerate(arrays):
        for j in range(i, len(arrays)):
            right = arrays[j]
            valid = masks[i] & masks[j]
            x, y = left[valid], right[valid]
            if x.size < 2:
                continue
            # Subtract an anchor before averaging for large-offset narrow data.
            x = x - x[0]
            y = y - y[0]
            x = x - x.mean()
            y = y - y.mean()
            xx, yy = xp.sum(x * x), xp.sum(y * y)
            denominator = xp.sqrt(xx) * xp.sqrt(yy)
            value = xp.sum(x * y) / xp.where(denominator > 0, denominator, xp.nan)
            result[i, j] = result[j, i] = xp.clip(value, -1.0, 1.0)
    return result


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
            import cupy as cp
            free, available = hybrid_execution.memory_available()
            # Covers float arrays, masks, filtered arrays, stacking and covariance
            # copies simultaneously; the existing frame is already in use.
            required = len(sub) * (len(wanted) * 48 + 96) + 4 * 1024**3
            if min(free, available) < required:
                raise hybrid_execution.HybridMemoryRefused("Pearson GPU temporary memory budget refused")
            # cuDF corr refuses nulls. Use the same finite pairwise semantics for
            # complete data too, including constants, NaNs and infinities.
            values = pairwise_pearson(sub, cp)
            cm = pd.DataFrame(cp.asnumpy(values), index=wanted, columns=wanted)
        except hybrid_execution.HybridMemoryRefused:
            raise
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
                max_columns: Optional[int] = None,
                summary_stats: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    import pandas as pd

    k = float(args.iqr_k)
    numeric = _numeric_cols(df)
    wanted = _resolve_columns(args, numeric, limit=max_columns)
    if not wanted:
        return {"columns": [], "results": {}, "note": "no numeric columns to scan"}

    results: Dict[str, Any] = {}
    count_reused = []
    for name in wanted:
        s = df[name]
        prior = (summary_stats or {}).get(name)
        if prior is not None:
            q1, q3 = prior["q1"], prior["q3"]
        else:
            q1, _median, q3 = _quartiles(s, eng.is_gpu)
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
            count = int(mask.sum())
            # Filtering the full (potentially very wide) frame would materialize
            # columns that are discarded immediately. Filter only the columns
            # returned as examples; the mask still scans every source row.
            example_cols = list(wanted) if len(wanted) > 1 else [name]
            top = df[example_cols][mask].head(int(args.top_k))
            records = _frame_records(top, int(args.top_k))
        except Exception as exc:
            if eng.is_gpu:
                raise OpNotSupported(f"cuDF outlier filtering failed: {exc}") from exc
            raise
        prior_count = (prior or {}).get("valid_count", (prior or {}).get("count"))
        if isinstance(prior_count, int) and not isinstance(prior_count, bool) and 0 <= prior_count <= len(s):
            n_valid = prior_count
            count_reused.append(name)
        else:
            try:
                n_valid = int(val.notnull().sum())
            except Exception:
                n_valid = int(len(s)) - count
        # Report how many values landed on a fence, so a boundary-heavy column is visible.
        try:
            # Distances were already computed for the anomaly mask above. Reuse
            # them instead of allocating two more full-column differences.
            n_ties = int((delta_low.abs().le(tol) | delta_high.abs().le(tol)).sum())
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
            "valid_count": n_valid,
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
    return {"columns": wanted, "results": results,
            "valid_count_reused_columns": count_reused}


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
            args: argparse.Namespace,
            statistics_for: Optional[Callable[[Engine], Dict[str, Any]]] = None,
            ) -> Tuple[Dict[str, Any], Engine, float, Optional[str], int]:
    """
    Load the table and run `op`, retrying on pandas if cuDF cannot do it.

    Returns (payload, engine_used, elapsed_s, fallback_reason, rows).
    """
    started = time.perf_counter()
    attempts = []

    def attempt(engine: Engine) -> Dict[str, Any]:
        load_started = time.perf_counter()
        df = load(engine)
        sync_device(engine)
        phases = {"engine": engine.name, "load_seconds": round(
            time.perf_counter() - load_started, 6)}
        attempts.append(phases)
        t_elapsed = _make_timer(engine)
        if op == "auto":
            summary = op_summary(df, engine, args)
            payload = {
                "profile": op_profile(df, engine, args),
                "summary": summary,
                "outliers": op_outliers(df, engine, args, max_columns=args.auto_columns,
                                         summary_stats=summary["stats"]),
            }
        elif op == "outliers" and statistics_for is not None:
            prior = statistics_for(engine)
            payload = op_outliers(df, engine, args, summary_stats=prior)
            payload["statistics_reused_columns"] = [
                name for name in payload.get("columns", []) if name in prior
            ]
        else:
            payload = OPS[op](df, engine, args)
        dt = t_elapsed()
        payload["rows_scanned"] = int(len(df))
        payload["compute_seconds"] = round(dt, 6)
        phases["compute_seconds"] = round(dt, 6)
        payload["phase_timings"] = {"attempts": attempts,
            "load_seconds": round(sum(a["load_seconds"] for a in attempts), 6),
            "compute_seconds": round(dt, 6)}
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
    p.add_argument("--no-auto-usecols", action="store_true",
                   help="read the full schema even when this operation names all required columns")
    p.add_argument("--force-cpu", action="store_true", help="ignore the GPU (for A/B comparison)")
    p.add_argument("--force-gpu", action="store_true",
                   help="use the GPU even below the measured crossover (for A/B comparison)")
    p.add_argument("--engine", default="auto", choices=["auto", "cpu", "gpu"],
                   help="auto (default) routes small files to the CPU, where the GPU's fixed "
                        "startup cost makes it slower; cpu/gpu force one path")
    p.add_argument("--parquet-cache-dir", default=os.environ.get("GPU_ANALYSIS_PARQUET_CACHE_DIR"),
                   help="optional directory for reusable full-file CSV to Parquet conversions")
    p.add_argument("--calibration-file", default=os.environ.get("GPU_ANALYSIS_CALIBRATION_FILE"),
                   help="optional JSONL measurements used for per-operation CPU/GPU routing")
    p.add_argument("--pretty", action="store_true", help="pretty-print the JSON output")
    p.add_argument("--load-backend", choices=["auto", "native", "cpu_gpu"],
                   default=os.environ.get("GPU_ANALYSIS_LOAD_BACKEND", "auto"),
                   help="native reader, explicit CPU Arrow -> GPU, or matched calibration")
    p.add_argument("--hybrid-profile", default=os.environ.get("GPU_ANALYSIS_HYBRID_PROFILE"),
                   help="local verified three-path calibration; never runs baselines implicitly")
    p.add_argument("--verbose", action="store_true", help="log engine/fallback decisions to stderr")
    return p


def _uncoordinated_main(argv: Optional[Sequence[str]] = None, *, execution_context: str = "cold") -> int:
    args = build_parser().parse_args(argv)
    t_start = time.perf_counter()

    path = os.path.abspath(args.input)
    if not os.path.isfile(path):
        print(json.dumps({"error": f"input file not found: {path}", "input": path}), flush=True)
        return 2

    ext = os.path.splitext(path)[1].lower()
    if ext and ext not in SUPPORTED_EXTS:
        _log(f"unrecognized extension {ext!r}; attempting to read as CSV")

    usecols = required_read_columns(args.op, args)
    nrows = int(args.limit) if args.limit else None

    cache_trace: Dict[str, Any] = {"status": "disabled"}
    load_attempts = []
    load_backend = args.load_backend
    hybrid_route = {"selected": None, "reason": "explicit/native request"}
    if load_backend == "cpu_gpu" and args.limit:
        print(json.dumps({"error": "hybrid loading does not support --limit", "input": path}), flush=True)
        return 2

    def load(engine: Engine) -> Any:
        nonlocal cache_trace
        trace = {"load_engine": engine.name, "compute_engine": engine.name,
                 "read_count": 1, "conversion_count": 0}
        load_attempts.append(trace)
        if load_backend == "cpu_gpu" and engine.is_gpu:
            try:
                df = hybrid_execution.read_gpu(engine, path, usecols, trace)
            except hybrid_execution.HybridRefused as exc:
                if isinstance(exc, (hybrid_execution.HybridMemoryRefused, hybrid_execution.HybridInputChanged)):
                    raise ValueError(str(exc)) from exc  # never bypass admission via fallback
                raise OpNotSupported(f"hybrid load/conversion failed: {exc}") from exc
        else:
            df, cache_trace = parquet_cache.read(
                engine, path, args.parquet_cache_dir, usecols=usecols, nrows=nrows)
            if getattr(engine, "last_read", None):
                trace.update(engine.last_read)
        if not hasattr(df, "columns") or len(df.columns) == 0:
            raise ValueError(f"no columns could be read from {path}")
        return df

    try:
        force_cpu = bool(args.force_cpu) or (args.engine == "cpu")
        force_gpu = bool(args.force_gpu) or (args.engine == "gpu")
        if force_cpu and (force_gpu or load_backend == "cpu_gpu"):
            raise ValueError("force_cpu conflicts with GPU/hybrid request")
        eligibility = hybrid_execution.preflight(
            path, usecols, hybrid_execution.single_workflow(args), load_backend)
        matched = None
        if load_backend == "auto" and not eligibility and not force_cpu and not nrows and not args.parquet_cache_dir:
            matched, hybrid_route = hybrid_execution.choose(
                args.hybrid_profile, path, usecols, hybrid_execution.single_workflow(args),
                "oneshot_" + execution_context)
            if matched == "cpu_gpu":
                load_backend = "cpu_gpu"
            elif matched:
                load_backend = "native"
        route_reason: Optional[str] = None
        route_details: Dict[str, Any] = {}
        mode = "force_cpu" if force_cpu else "force_gpu" if force_gpu else "auto"
        if eligibility and not force_cpu:
            force_cpu = True
            route_reason = "preflight selected CPU: " + eligibility
            route_details["eligibility"] = eligibility
            route_details["policy"] = "capability_preflight"
        elif load_backend == "cpu_gpu":
            force_gpu = True
            route_reason = None
            route_details["hybrid_calibration"] = hybrid_route
            if matched:
                route_details["policy"] = "matched_hybrid_calibration"
        elif not force_cpu and not force_gpu and matched:
            force_cpu = matched == "cpu"
            route_reason = hybrid_route["reason"] if force_cpu else None
            route_details["policy"] = "matched_hybrid_calibration"
            route_details["hybrid_calibration"] = hybrid_route
        elif not force_cpu and not force_gpu:
            use_gpu, route_reason = pick_engine_for(
                path, args.op, verbose=args.verbose, details=route_details,
                calibration_file="" if args.parquet_cache_dir or usecols or execution_context != "cold"
                                 else args.calibration_file,
            )
            force_cpu = not use_gpu
            if args.verbose and route_reason:
                _log(f"CPU chosen: {route_reason}")
        elif force_cpu:
            route_reason = "CPU forced by the caller"
        selected_backend = "pandas" if force_cpu else "cudf"
        init_started = time.perf_counter()
        eng = detect_engine(force_cpu=force_cpu, verbose=args.verbose)
        init_seconds = time.perf_counter() - init_started
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

    # `used.reason` explains why detect_engine returned pandas. It is a fallback reason only
    # when auto-routing had selected the GPU and the GPU was unavailable or failed. When
    # `route_reason` is present, pandas was chosen deliberately, so surfacing `used.reason`
    # as a fallback would violate the public contract and make a healthy decision look broken.
    fallback_reason = fallback
    if fallback_reason is None and not used.is_gpu and route_reason is None:
        fallback_reason = used.reason

    total_seconds = round(time.perf_counter() - t_start, 6)
    if execution_context == "cold" and not fallback_reason and load_backend != "cpu_gpu" and cache_trace["status"] == "disabled" and not nrows and not usecols:
        try:
            cost_model.record(args.calibration_file, op=args.op, source=path,
                              backend=used.name, size=os.path.getsize(path),
                              seconds=total_seconds)
        except OSError as exc:
            _log(f"calibration measurement not saved: {exc}")
    decision_reason = route_reason or (
        hybrid_route["reason"] if matched else
        "CPU Arrow loading and GPU compute explicitly requested" if load_backend == "cpu_gpu" else
        "GPU forced by the caller" if mode == "force_gpu" else
        "the calibrated operation model selected GPU" if
        route_details.get("policy") == "calibrated_cost_model" else
        "the measured file-size crossover policy selected GPU; this is not an "
        "operation-specific cost prediction"
    )
    decision = execution_decision_record(
        mode=mode,
        policy="caller_override" if mode != "auto" else
               route_details.get("policy", "measured_file_size_crossover"),
        selected_backend=selected_backend,
        actual_backend=used.name,
        reason=decision_reason,
        signals={"operation": args.op, "file_size_bytes": os.path.getsize(path),
                 "read_columns": usecols,
                 **route_details},
        observed={"phase": "request_total", "elapsed_seconds": total_seconds,
                  "compute_seconds": payload.get("compute_seconds"), "rows_scanned": rows,
                  "parquet_cache": cache_trace},
        fallback_reason=fallback_reason,
        estimated_seconds=(route_details.get("estimated_seconds") or {}).get(selected_backend),
    )

    result: Dict[str, Any] = {
        "ok": True,
        "op": args.op,
        "input": path,
        "file_size_bytes": os.path.getsize(path),
        "engine": used.name,
        "engine_version": used.version,
        "gpu": used.gpu_name or ("none (CPU run)" if not used.is_gpu else "CUDA device"),
        "accelerated": used.is_gpu,
        "fallback_reason": fallback_reason,
        "routing_reason": route_reason,
        "execution_decision": decision,
        "rows_scanned": rows,
        "op_seconds": round(elapsed, 6),
        "total_seconds": total_seconds,
        "phase_timings": {**payload.get("phase_timings", {}),
                          "engine_init_seconds": round(init_seconds, 6),
                          "request_seconds": total_seconds},
    }
    result.update(payload)

    # Consistent contract: each op's data lives under its own key so a caller can
    # read result[op] without caring which op ran ("auto" already nests that way).
    if args.op != "auto":
        result = {**{k: v for k, v in result.items() if k not in payload}, args.op: payload}
    result["phase_timings"] = {**payload.get("phase_timings", {}),
                              "engine_init_seconds": round(init_seconds, 6),
                              "request_seconds": total_seconds}
    result["loading"] = {"requested": args.load_backend,
        "actual": "cpu_gpu" if used.is_gpu and (load_backend == "cpu_gpu" or
                  any(a.get("conversion_count") for a in load_attempts)) else
                  "native_gpu" if used.is_gpu else "cpu",
        "execution_context": execution_context, "decision": hybrid_route,
        "attempts": load_attempts}

    print(json.dumps(result, indent=2 if args.pretty else None, default=str, ensure_ascii=False), flush=True)
    return 0


def main(argv: Optional[Sequence[str]] = None, *, execution_context: str = "cold") -> int:
    # Stateless CLI calls (including transport fallback from the Agent) must
    # obey the same slot as resident/batch workers. CPU-forced CLI stays parallel.
    from gpu_coordination import gpu_slot

    tokens = list(sys.argv[1:] if argv is None else argv)
    cpu_forced = "--force-cpu" in tokens or "--engine=cpu" in tokens or any(
        token == "--engine" and index + 1 < len(tokens) and tokens[index + 1] == "cpu"
        for index, token in enumerate(tokens))
    if cpu_forced:
        return _uncoordinated_main(tokens, execution_context=execution_context)
    try:
        with gpu_slot():
            return _uncoordinated_main(tokens, execution_context=execution_context)
    except (TimeoutError, PermissionError, ValueError) as exc:
        print(json.dumps({"ok": False, "error": str(exc),
                          "error_code": "GPU_COORDINATION_REFUSED"}), flush=True)
        return 1


if __name__ == "__main__":
    sys.exit(main())
