"""Independently verify the numbers the agent reported in demo step 5.

The agent answered without calling a tool, reusing the `outliers` block from step 2's
`auto` run. Reusing a valid result is not fabrication, but the numbers must still be
correct -- so recompute them from scratch with pandas and compare.
"""
import pandas as pd

PATH = "/home/Developer/sales_demo_small.csv"

df = pd.read_csv(PATH)
print(f"rows: {len(df):,}   columns: {list(df.columns)}")
print()

claims = {
    "revenue": (518394, 10.37),
    "quantity": (0, 0.00),
}

print(f"{'column':<12}{'agent':>10}{'independent':>13}{'match':>8}{'pct':>8}")
print("-" * 52)
bad = 0
for col, (claimed_n, claimed_pct) in claims.items():
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
print("Agent's supporting claims:")
rev = df["revenue"]
print(f"  revenue mean   = {rev.mean():.2f}   (agent said 1,073.83)")
print(f"  revenue median = {rev.median():.2f}   (agent said 403.67)")
q = df["quantity"]
print(f"  quantity range = {q.min()} .. {q.max()}   (agent said 1~499)")
print(f"  quantity median= {q.median()}   (agent said 250)")
print(f"  quantity IQR   = {q.quantile(0.75)-q.quantile(0.25)}")
print()
print(f"mismatches: {bad}")