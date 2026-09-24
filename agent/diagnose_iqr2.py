"""Outlier-count mismatch: the fences agree, so compare the comparison itself.

Q1/Q3 are identical across engines and interpolation methods, so the difference must be in
how values are tested against the fence (strict < / > vs <= / >=), or in how NaN handling
lands. Voltage is reported to 3 decimals, so values sitting exactly on the fence are real.
"""

import os
import cudf
import pandas as pd

PATH = os.environ.get("DIAG_DATA", "power_clean.csv")
COL = "Voltage"

pdf = pd.read_csv(PATH, usecols=[COL])
s = pdf[COL].dropna()
gdf = cudf.read_csv(PATH, usecols=[COL])[COL]

q1, q3 = float(s.quantile(0.25)), float(s.quantile(0.75))
iqr = q3 - q1
lo, hi = q1 - 1.5 * iqr, q3 + 1.5 * iqr
print(f"fences: lo={lo!r} hi={hi!r}  iqr={iqr!r}")
print(f"values <  lo : {int((s < lo).sum()):,}")
print(f"values <= lo : {int((s <= lo).sum()):,}")
print(f"values >  hi : {int((s > hi).sum()):,}")
print(f"values >= hi : {int((s >= hi).sum()):,}")
print(f"sum strict   : {int(((s < lo) | (s > hi)).sum()):,}")
print(f"sum inclusive: {int(((s <= lo) | (s >= hi)).sum()):,}")
print()

# How many values sit exactly on each fence?
print(f"exactly == lo : {int((s == lo).sum()):,}   value={lo}")
print(f"exactly == hi : {int((s == hi).sum()):,}   value={hi}")
print(f"unique values near lo: {sorted(s[(s > lo - 0.02) & (s < lo + 0.02)].unique())[:8]}")
print(f"unique values near hi: {sorted(s[(s > hi - 0.02) & (s < hi + 0.02)].unique())[:8]}")
print()

# What does cuDF itself report, with and without dropping nulls first?
g_drop = gdf.dropna()
gq1, gq3 = float(g_drop.quantile(0.25)), float(g_drop.quantile(0.75))
giqr = gq3 - gq1
glo, ghi = gq1 - 1.5 * giqr, gq3 + 1.5 * giqr
print(f"cudf fences: lo={glo!r} hi={ghi!r}")
print(f"cudf mask strict  (<,>) : {int(((g_drop < glo) | (g_drop > ghi)).sum()):,}")
print(f"cudf mask incl (<=,>=)  : {int(((g_drop <= glo) | (g_drop >= ghi)).sum()):,}")
print()

# Is the null handling itself the difference? 25,979 nulls exist in this column.
print(f"total rows          : {len(gdf):,}")
print(f"non-null rows       : {len(s):,}")
print(f"nulls               : {int(gdf.isna().sum()):,}")
print()

# The decisive check: does the engine's own outlier op agree with a strict mask?
import subprocess
import json

out = subprocess.run(
    ["python", os.environ.get("GPU_ANALYTICS_SCRIPT", "gpu_analytics.py"),
     "--input", PATH, "--op", "outliers", "--columns", COL],
    capture_output=True, text=True)
res = json.loads(out.stdout)["outliers"]["results"][COL]
print("engine op result:")
for k in ("count", "pct", "lower_bound", "upper_bound", "q1", "q3", "iqr", "column_min",
          "column_max"):
    if k in res:
        print(f"  {k:<13} {res[k]!r}")
print(f"  (engine says {res['count']:,}; strict pandas mask says "
      f"{int(((s < lo) | (s > hi)).sum()):,})")
