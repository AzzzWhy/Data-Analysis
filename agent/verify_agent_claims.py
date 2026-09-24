"""Verify the agent's outlier claims against an independent computation.

The agent reported per-column outlier counts for power_clean.csv. This recomputes them
from scratch with pandas (an implementation independent of the cuDF path that produced
the agent's numbers) and prints both. If they disagree, the agent's answer was wrong and
that is a serious problem for a tool whose whole purpose is accurate statistics.
"""
import os
import sys

import pandas as pd

PATH = os.environ.get("DIAG_DATA", "power_clean.csv")

df = pd.read_csv(PATH)
print(f"rows read: {len(df):,}")
print()

claims = {
    "Global_active_power": 94907,
    "Global_reactive_power": 40420,
    "Voltage": 50763,
    "Global_intensity": 100961,
    "Sub_metering_1": 169105,
    "Sub_metering_2": 77151,
    "Sub_metering_3": 0,
}

print(f"{'column':<24}{'agent':>9}{'independent':>13}{'match':>8}{'pct':>8}")
print("-" * 62)
bad = 0
for col, claimed in claims.items():
    s = df[col].dropna()
    q1, q3 = s.quantile(0.25), s.quantile(0.75)
    iqr = q3 - q1
    lo, hi = q1 - 1.5 * iqr, q3 + 1.5 * iqr
    actual = int(((s < lo) | (s > hi)).sum())
    ok = "OK" if actual == claimed else "MISMATCH"
    if actual != claimed:
        bad += 1
    print(f"{col:<24}{claimed:>9,}{actual:>13,}{ok:>8}{100*actual/len(s):>7.2f}%")

print()
print(f"mismatches: {bad}")
# The interesting structural claim the agent made about Sub_metering_1
s = df["Sub_metering_1"].dropna()
print(f"Sub_metering_1: Q1={s.quantile(0.25)}, Q3={s.quantile(0.75)}, "
      f"IQR={s.quantile(0.75)-s.quantile(0.25)}  (agent claimed Q1=Q3=0, IQR=0)")
print(f"Sub_metering_1 zeros: {int((s == 0).sum()):,} of {len(s):,} "
      f"({100*(s==0).mean():.1f}%)")
sys.exit(1 if bad else 0)
