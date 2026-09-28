"""Guarded exact full-frequency statistics for repetitive numeric columns.

Counts include every input value. No rounding, bins, sampling of answers, or
approximate quantiles. Small/high-cardinality/ill-conditioned inputs fall back.
The small probe only chooses an algorithm; it never supplies reported values.
"""
import math

MIN_ROWS = 1_000_000
MAX_DISTINCT = 262_144


def weighted_statistics(values, counts, xp):
    """Exact-order-statistic quartiles and stable weighted sample variance.

    values must be sorted distinct finite float64 values; counts positive int64.
    Returns None when numeric conditions are unsuitable for this candidate.
    """
    if len(values) == 0 or len(values) != len(counts) or counts.dtype.kind not in "iu":
        return None
    if not bool(xp.all(xp.isfinite(values))) or not bool(xp.all(counts > 0)):
        return None
    if bool(xp.any(values[1:] <= values[:-1])):
        return None
    total = int(counts.sum())
    if total <= 0 or total > 2**53 - 1:
        return None
    if bool(xp.any(xp.abs(values) > 2**53 - 1)):
        return None
    span = float(values[-1] - values[0])
    scale = max(abs(float(values[0])), abs(float(values[-1])))
    if not math.isfinite(span) or (scale and 0 < span / scale < 1e-8):
        return None  # preserve the baseline for large-offset, narrow-range data
    cumulative = xp.cumsum(counts, dtype=xp.int64)
    ranks = (total - 1) * xp.asarray([.25, .5, .75], dtype=xp.float64)
    low = xp.floor(ranks).astype(xp.int64)
    high = xp.ceil(ranks).astype(xp.int64)
    left = values[xp.searchsorted(cumulative, low, side="right")]
    right = values[xp.searchsorted(cumulative, high, side="right")]
    fraction = ranks - low
    quartiles = left + (right - left) * fraction
    centered = values - values[0]
    delta = xp.sum(centered * counts) / total
    mean = values[0] + delta
    if scale > 1e6 and abs(float(mean)) < scale * 1e-8:
        return None  # near-cancelling huge signed values retain native reductions
    std = (xp.sqrt(xp.sum(counts * (centered - delta)**2) / (total - 1))
           if total > 1 else xp.asarray(xp.nan))
    return xp.concatenate((xp.stack((mean, std, values[0], values[-1])), quartiles))


def frequency_describe(series, is_gpu, baseline, *, force=False, trace=None):
    if len(series) < MIN_ROWS and not force:
        return baseline(series, is_gpu)
    kind = getattr(series.dtype, "kind", "")
    if kind not in {"i", "u", "f"} or not len(series):
        return baseline(series, is_gpu)
    if kind == "f" and series.dtype.itemsize != 8:
        # Don't silently replace the baseline's float32 accumulation semantics.
        return baseline(series, is_gpu)
    probe = series.iloc[::max(1, len(series) // 4096)].head(4096).dropna()
    if int(probe.nunique()) > (1024 if is_gpu else 256) and not force:
        return baseline(series, is_gpu)
    if len(probe):
        low, high = float(probe.min()), float(probe.max())
        scale = max(abs(low), abs(high))
        if scale and 0 < (high - low) / scale < 1e-8:
            # A sample can conservatively REJECT this algorithm before its
            # expensive full grouping; full values still validate acceptance.
            return baseline(series, is_gpu)
    if is_gpu:
        import cupy as xp
        import hybrid_execution
        # Includes the worst-case distinct hash table and temporary buffers,
        # not merely the optimistic small result seen in a sample.
        if min(hybrid_execution.memory_available()) < len(series) * 64 + 4 * 1024**3:
            return baseline(series, is_gpu)
    else:
        import numpy as xp
    clean = series.dropna()
    if not len(clean):
        return baseline(series, is_gpu)
    frequencies = clean.value_counts(sort=False)
    if int(frequencies.sum()) != len(clean):
        return baseline(series, is_gpu)
    if len(frequencies) > MAX_DISTINCT:
        if trace is not None:
            trace["fallback"] = "actual cardinality exceeds candidate limit"
        return baseline(series, is_gpu)
    frequencies = frequencies.sort_index()
    if is_gpu:
        values = frequencies.index.values.astype(xp.float64)
        counts = frequencies.values.astype(xp.int64)
    else:
        values = frequencies.index.to_numpy(dtype="float64")
        counts = frequencies.to_numpy(dtype="int64")
    result = weighted_statistics(values, counts, xp)
    if result is None:
        return baseline(series, is_gpu)
    if is_gpu:
        result = xp.asnumpy(result)
    if trace is not None:
        trace.update(actual="full_frequency", rows=len(clean), distinct=len(frequencies))
    labels = ("mean", "std", "min", "max", "q1", "median", "q3")
    return {"count": len(clean), **{name: (float(v) if math.isfinite(float(v)) else None)
                                  for name, v in zip(labels, result)}}
