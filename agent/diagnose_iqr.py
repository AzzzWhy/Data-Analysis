"""Pin down why the cuDF path and pandas disagree on Voltage's IQR outlier count.

Hypothesis: the two engines interpolate quantiles differently, which moves the IQR fence
and therefore the outlier count. pandas defaults to linear interpolation; if the cuDF path
does not request linear explicitly, it may use nearest, giving a different Q1/Q3.
"""

import cudf
import pandas as pd

PATH = "/home/Developer/power_clean.csv"
COL = "Voltage"

pdf = pd.read_csv(PATH, usecols=[COL])
s = pdf[COL].dropna()
gdf = cudf.read_csv(PATH, usecols=[COL])[COL].dropna()

print("=== Q1 / Q3 under different interpolation settings ===")
p_lin = (float(s.quantile(0.25)), float(s.quantile(0.75)))
print(f"pandas linear          Q1={p_lin[0]:.12f}  Q3={p_lin[1]:.12f}")
try:
    p_near = (float(s.quantile(0.25, interpolation="nearest")),
              float(s.quantile(0.75, interpolation="nearest")))
    print(f"pandas nearest         Q1={p_near[0]:.12f}  Q3={p_near[1]:.12f}")
except Exception as exc:
    p_near = None
    print(f"pandas nearest         ERROR {exc}")
try:
    p_low = (float(s.quantile(0.25, interpolation="lower")),
             float(s.quantile(0.75, interpolation="lower")))
    print(f"pandas lower           Q1={p_low[0]:.12f}  Q3={p_low[1]:.12f}")
except Exception as exc:
    p_low = None
    print(f"pandas lower           ERROR {exc}")

g_def = (float(gdf.quantile(0.25)), float(gdf.quantile(0.75)))
print(f"cudf   default         Q1={g_def[0]:.12f}  Q3={g_def[1]:.12f}")
for how in ("linear", "nearest", "lower", "higher", "midpoint"):
    try:
        q = (float(gdf.quantile(0.25, interpolation=how)),
             float(gdf.quantile(0.75, interpolation=how)))
        print(f"cudf   {how:<15} Q1={q[0]:.12f}  Q3={q[1]:.12f}")
    except Exception as exc:
        print(f"cudf   {how:<15} ERROR {type(exc).__name__}: {str(exc)[:70]}")


def count_out(q1: float, q3: float) -> int:
    iqr = q3 - q1
    lo, hi = q1 - 1.5 * iqr, q3 + 1.5 * iqr
    return int(((s < lo) | (s > hi)).sum())


print()
print("=== resulting outlier counts (computed on the same pandas series) ===")
print(f"  from pandas linear : {count_out(*p_lin):,}")
if p_near:
    print(f"  from pandas nearest: {count_out(*p_near):,}")
if p_low:
    print(f"  from pandas lower  : {count_out(*p_low):,}")
print(f"  from cudf default  : {count_out(*g_def):,}   <- what the agent reported")
print()
print(f"Agent reported : 50,763")
print(f"pandas linear  : {count_out(*p_lin):,}")