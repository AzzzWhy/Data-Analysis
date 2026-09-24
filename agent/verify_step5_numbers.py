"""Independently verify the numbers the agent reported in one specific recorded demo run.

Background
----------
In an early rehearsal the agent answered step 5 using the `outliers` block that step 2's
`auto` run had already produced, instead of calling the tool again. Reusing a valid result
is not fabrication, but the numbers still have to be right -- so this script recomputes the
same quantities from scratch with pandas (a code path independent of the cuDF engine that
produced the agent's answer) and compares.

Which dataset these claims belong to
------------------------------------
The recorded claims below come from the run against `sales_demo_small.csv`
(5,000,000 rows, 572 MB). They are *not* valid for the 20M-row `sales_demo.csv` used in the
final demo: outlier counts, means and medians all scale with the row count, so comparing
against the wrong file reports a mismatch that says nothing about correctness.

Therefore the expected values are supplied per dataset, and pointing this script at a file
with no recorded claims is an explicit skip rather than a false failure.

Run:
    python verify_step5_numbers.py                       # uses DEMO_DATA or the default path
    DEMO_DATA=/path/to/sales_demo_small.csv python verify_step5_numbers.py
"""
import os
import sys

import pandas as pd

PATH = os.environ.get("DEMO_DATA", "sales_demo.csv")

# Recorded claims, keyed by dataset basename. Each entry is (rows, outlier counts, extras).
RECORDED = {
    "sales_demo_small.csv": {
        "rows": 5_000_000,
        "outliers": {"revenue": 518_394, "quantity": 0},
        "extras": {
            "revenue mean": 1073.83,
            "revenue median": 403.67,
            "quantity min": 1,
            "quantity max": 499,
            "quantity median": 250,
        },
    },
}

basename = os.path.basename(PATH)
recorded = RECORDED.get(basename)

if not os.path.exists(PATH):
    print(f"[skip] dataset not found: {PATH}")
    sys.exit(0)

if recorded is None:
    print(f"[skip] no recorded agent claims for '{basename}'.")
    print(f"       Recorded datasets: {', '.join(RECORDED)}")
    print("       Point DEMO_DATA at one of those to reproduce the comparison.")
    sys.exit(0)

df = pd.read_csv(PATH)
print(f"rows: {len(df):,}   columns: {list(df.columns)}")
print(f"recorded claims are for {recorded['rows']:,} rows "
      f"-> {'MATCHING dataset' if len(df) == recorded['rows'] else 'DIFFERENT row count'}")
print()

# Same IQR definition as the engine, including the relative fence tolerance that makes the
# GPU and CPU paths agree (see verify_fence_fix.py for why it is needed).
print(f"{'column':<12}{'agent':>10}{'independent':>13}{'match':>8}{'pct':>8}")
print("-" * 52)
bad = 0
for col, claimed_n in recorded["outliers"].items():
    s = df[col].dropna()
    q1, q3 = s.quantile(0.25), s.quantile(0.75)
    iqr = q3 - q1
    lo, hi = q1 - 1.5 * iqr, q3 + 1.5 * iqr
    tol = 1e-9 * max(abs(lo), abs(hi), 1.0)
    mask = ((s < lo) & ((lo - s) > tol)) | ((s > hi) & ((s - hi) > tol))
    n = int(mask.sum())
    pct = 100.0 * n / len(s)
    ok = "OK" if n == claimed_n else "MISMATCH"
    if n != claimed_n:
        bad += 1
    print(f"{col:<12}{claimed_n:>10,}{n:>13,}{ok:>8}{pct:>7.2f}%")

print()
print("Agent's supporting claims, recomputed independently:")
rev, q = df["revenue"], df["quantity"]
actual = {
    "revenue mean": round(float(rev.mean()), 2),
    "revenue median": round(float(rev.median()), 2),
    "quantity min": int(q.min()),
    "quantity max": int(q.max()),
    "quantity median": float(q.median()),
}
for key, claimed in recorded["extras"].items():
    got = actual[key]
    flag = "OK" if abs(float(got) - float(claimed)) <= 0.01 else "MISMATCH"
    if flag == "MISMATCH":
        bad += 1
    print(f"  {key:<18}= {got:<12} (agent said {claimed})  {flag}")

print()
print(f"mismatches: {bad}")
print("PASS: every recorded claim reproduced independently." if bad == 0
      else "FAIL: the agent's numbers did not reproduce.")
sys.exit(0 if bad == 0 else 1)