"""Quantify the floating-point fence tie: how many values sit exactly on a fence?

If the answer is "a non-trivial number", then an IQR outlier count is not reproducible
across engines, and the fix is a relative tolerance on the fence comparison.
"""
import cudf
import pandas as pd

PATH = "/home/Developer/power_clean.csv"

COLS = ["Global_active_power", "Global_reactive_power", "Voltage", "Global_intensity",
        "Sub_metering_1", "Sub_metering_2", "Sub_metering_3"]

pdf = pd.read_csv(PATH)
gdf = cudf.read_csv(PATH)

print(f"{'column':<24}{'pandas':>9}{'cudf':>9}{'diff':>8}{'==lo':>7}{'==hi':>7}")
print("-" * 66)
total_ties = 0
for col in COLS:
    s = pdf[col].dropna()
    q1, q3 = float(s.quantile(0.25)), float(s.quantile(0.75))
    iqr = q3 - q1
    lo, hi = q1 - 1.5 * iqr, q3 + 1.5 * iqr
    p_count = int(((s < lo) | (s > hi)).sum())

    gs = gdf[col].dropna()
    gq1, gq3 = float(gs.quantile(0.25)), float(gs.quantile(0.75))
    giqr = gq3 - gq1
    glo, ghi = gq1 - 1.5 * giqr, gq3 + 1.5 * giqr
    g_count = int(((gs < glo) | (gs > ghi)).sum())

    lo_ties = int((s == lo).sum())
    hi_ties = int((s == hi).sum())
    total_ties += lo_ties + hi_ties
    flag = "" if p_count == g_count else "   <-- DISAGREE"
    print(f"{col:<24}{p_count:>9,}{g_count:>9,}{p_count-g_count:>8,}"
          f"{lo_ties:>7,}{hi_ties:>7,}{flag}")

print()
print(f"values sitting exactly on a fence, across all columns: {total_ties:,}")
print()
print("Relative-tolerance fix preview (1e-9 relative), Voltage:")
s = pdf["Voltage"].dropna()
lo, hi = 233.14000000000004, 248.73999999999995
tol = 1e-9 * max(abs(lo), abs(hi))
strict = int(((s < lo) | (s > hi)).sum())
tolerant = int((((s < lo) & (abs(s - lo) > tol)) | ((s > hi) & (abs(s - hi) > tol))).sum())
print(f"  strict   : {strict:,}")
print(f"  tolerant : {tolerant:,}   (drops fence ties)")
print(f"  matches cuDF's {50763:,}? {tolerant == 50763}")