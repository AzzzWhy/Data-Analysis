"""Confirm the fence-tie fix makes GPU and CPU agree on the real dataset.

Before the fix: Voltage reported 51,067 outliers on pandas and 50,763 on cuDF (a 304-count
disagreement caused by 4e-14 of floating-point difference in the IQR fence).

After the fix both engines must report the same counts, and any values sitting on a fence
must be reported in `fence_ties_excluded` rather than silently changing the total.
"""
import json
import subprocess
import sys

ENGINE = "/home/Developer/cudf-analytics-skill/scripts/gpu_analytics.py"
PATH = "/home/Developer/power_clean.csv"

COLS = ["Global_active_power", "Global_reactive_power", "Voltage", "Global_intensity",
        "Sub_metering_1", "Sub_metering_2", "Sub_metering_3"]


def run(force_cpu: bool) -> dict:
    cmd = [sys.executable, ENGINE, "--input", PATH, "--op", "outliers",
           "--columns", ",".join(COLS)]
    if force_cpu:
        cmd.append("--force-cpu")
    out = subprocess.run(cmd, capture_output=True, text=True)
    if out.returncode != 0:
        print(f"engine failed (exit {out.returncode}): {out.stderr[-500:]}")
        sys.exit(2)
    return json.loads(out.stdout)


gpu = run(force_cpu=False)
cpu = run(force_cpu=True)
# rows_scanned lives inside the op's own block, not at the top level.
g_rows = gpu["outliers"].get("rows_scanned", gpu.get("rows_scanned"))
print(f"GPU engine: {gpu['engine']}  accelerated={gpu['accelerated']}  "
      f"rows={g_rows:,}")
print(f"CPU engine: {cpu['engine']}  accelerated={cpu['accelerated']}")
print()

print(f"{'column':<24}{'GPU':>9}{'CPU':>9}{'agree':>7}{'ties':>9}")
print("-" * 60)
failures = 0
for col in COLS:
    g = gpu["outliers"]["results"][col]
    c = cpu["outliers"]["results"][col]
    ties = g.get("fence_ties_excluded", 0)
    same = g["count"] == c["count"]
    if not same:
        failures += 1
    print(f"{col:<24}{g['count']:>9,}{c['count']:>9,}"
          f"{('OK' if same else 'DIFFER'):>7}{ties:>9,}")

print()
if failures:
    print(f"FAIL: {failures} column(s) still disagree between engines")
    sys.exit(1)

print("PASS: GPU and CPU report identical outlier counts on real data.")
v = gpu["outliers"]["results"]["Voltage"]
print(f"  Voltage was the failing case before: now {v['count']:,} on both engines.")
if v.get("fence_ties_excluded"):
    print(f"  fence_ties_excluded={v['fence_ties_excluded']:,}")
    print(f"  note: {v.get('note')}")
sm = gpu["outliers"]["results"]["Sub_metering_1"]
print(f"  Sub_metering_1 (IQR=0 case): {sm['count']:,} both engines, "
      f"ties={sm.get('fence_ties_excluded', 0):,}")