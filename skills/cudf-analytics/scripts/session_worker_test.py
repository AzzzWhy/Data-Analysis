#!/usr/bin/env python3
"""Exercise the session worker standalone: open, multi-step analyze, close.

Run on GB10 from the skill's scripts directory:
    python session_worker_test.py /path/to/data.csv
"""
import json
import os
import subprocess
import sys
import time

DATA = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("DEMO_DATA", "sales_demo.csv")
HERE = os.path.dirname(os.path.abspath(__file__))
FAILS = []


def check(label, ok, detail=""):
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}{'  ' + detail if detail else ''}")
    if not ok:
        FAILS.append(label)


p = subprocess.Popen([sys.executable, os.path.join(HERE, "gpu_session.py")],
                     stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                     stderr=subprocess.PIPE, text=True, bufsize=1)


def call(req):
    p.stdin.write(json.dumps(req) + "\n")
    p.stdin.flush()
    line = p.stdout.readline()
    if not line:
        raise RuntimeError("worker died: " + (p.stderr.read() or "")[-500:])
    return json.loads(line)


print("=== ping ===")
r = call({"cmd": "ping"})
print(f"  engine={r.get('engine')} gpu={r.get('gpu')} free={r.get('free_gpu_gb')}GB "
      f"max_sessions={r.get('max_sessions')}")
check("ping responds", r.get("ok") is True)

print("=== open ===")
t0 = time.perf_counter()
r = call({"cmd": "open", "path": DATA})
wall = time.perf_counter() - t0
print(f"  ok={r.get('ok')} rows={r.get('rows')} engine={r.get('engine')} "
      f"load={r.get('load_seconds')}s cpu_load={r.get('cpu_load_seconds')}s "
      f"resident={r.get('resident_mb')}MB (wall {wall:.2f}s)")
check("open succeeded", r.get("ok") is True, r.get("error", ""))
sid = r.get("session_id")
load_seconds = r.get("load_seconds") or 0.0
check("rows match the file", r.get("rows") == 20_000_000, f"rows={r.get('rows')}")
check("engine is cudf", r.get("engine") == "cudf")
check("resident frame measured", isinstance(r.get("resident_mb"), (int, float)))

print("=== multi-step analyze on the resident frame ===")
steps = [
    ("profile", {}),
    ("groupby", {"by": "region", "agg": "revenue:sum,mean", "top_k": 5}),
    ("groupby", {"by": "category", "agg": "profit:mean", "top_k": 3}),
    ("outliers", {"columns": "revenue"}),
    ("summary", {}),
    ("corr", {"top_k": 5}),
]
step_times = []
for op, kw in steps:
    r = call({"cmd": "analyze", "sid": sid, "op": op, **kw})
    ok = r.get("ok") is True
    step_times.append(r.get("step_seconds", 0))
    print(f"  {op:<9} ok={ok} rows_scanned={r.get('rows_scanned')} "
          f"step={r.get('step_seconds')}s cumulative={r.get('cumulative_seconds')}s")
    if not ok:
        print(f"      error: {r.get('error')}")
    check(f"{op} on resident frame", ok)
    check(f"{op} scanned full data", r.get("rows_scanned") == 20_000_000)

print("=== proof that no step re-reads the file ===")
# A wall-clock threshold would be the wrong test: `summary` legitimately costs seconds because
# it computes 3 quantiles x 10 numeric columns (20 quantile passes) on the full 20M rows.
# That is real computation, not I/O. So test the property directly instead:
#   (a) cumulatives are exactly load + sum(steps), and
#   (b) the same op run twice in a row costs the same -- a hidden re-read would add a
#       full load (~1.9s) to the second run.
r = call({"cmd": "analyze", "sid": sid, "op": "groupby", "by": "region",
          "agg": "revenue:sum", "top_k": 5})
t_a = r.get("step_seconds", 0)
r = call({"cmd": "analyze", "sid": sid, "op": "groupby", "by": "region",
          "agg": "revenue:sum", "top_k": 5})
t_b = r.get("step_seconds", 0)
print(f"  identical groupby twice: {t_a:.3f}s then {t_b:.3f}s "
      f"(a re-read would add ~{load_seconds:.2f}s to the second)")
check("repeating an op stays cheap (no re-read)", t_b < load_seconds / 2,
      f"{t_b:.3f}s vs load {load_seconds:.2f}s")
check("repeat cost is stable", abs(t_b - t_a) < 0.5, f"delta={abs(t_b - t_a):.3f}s")

# The accounting must close: cumulative == load + everything else accounted for.
expected = load_seconds + sum(step_times) + t_a + t_b
got = r.get("cumulative_seconds", 0)
check("cumulative == load + sum(steps)", abs(got - expected) < 0.15,
      f"reported={got:.3f}s expected={expected:.3f}s")

print("=== staleness guard: change the file, session must refuse ===")
# Runs against a COPY, never the caller's dataset. An earlier version of this test called
# os.utime on the real file, which rewrote the modification time of a 3 GB dataset the demo
# depends on; appending a byte would have corrupted it outright. A test must not mutate the
# input it is handed.
try:
    import shutil
    import tempfile

    tmpdir = tempfile.mkdtemp(prefix="stale-check-")
    copy_path = os.path.join(tmpdir, "stale_copy.csv")
    shutil.copyfile(DATA, copy_path)

    # The guard compares the identity recorded at open time with the current one. Touching
    # alone used to be enough to defeat it, because the identity truncated mtime to whole
    # seconds and a touch in the same second as the load compared equal -- the guard failed
    # open on exactly the case it exists for. It now uses nanosecond mtime, and this test
    # changes the size too, so it holds even on a coarse-timestamp filesystem.
    r_open = call({"cmd": "open", "path": copy_path})
    copy_sid = r_open.get("session_id")
    check("copy opened for the staleness check", r_open.get("ok") is True, str(r_open)[:120])

    before = os.path.getsize(copy_path)
    with open(copy_path, "ab") as fh:
        fh.write(b"\n")          # harmless to a CSV reader, changes the identity
    os.utime(copy_path, None)

    r = call({"cmd": "analyze", "sid": copy_sid, "op": "summary"})
    refused = (r.get("ok") is False
               and "changed while this session was open" in str(r.get("error", "")))
    check("refuses to answer after the file changed", refused,
          "" if refused else f"got: {r}")
    check("the staleness check saw a size change",
          os.path.getsize(copy_path) == before + 1)
    check("the caller's dataset was not modified", os.path.getsize(DATA) == before,
          f"{os.path.getsize(DATA)} vs {before}")

    call({"cmd": "close", "sid": copy_sid})
    shutil.rmtree(tmpdir, ignore_errors=True)
except Exception as exc:
    check("staleness guard", False, f"{type(exc).__name__}: {exc}")

print("=== list ===")
r = call({"cmd": "list"})
print(f"  count={r.get('count')} {r.get('sessions')}")
check("list reports the open session", r.get("count") == 1)

print("=== close ===")
r = call({"cmd": "close", "sid": sid})
print(f"  steps={r.get('steps')} load={r.get('load_seconds')}s "
      f"analysis={r.get('analysis_seconds')}s total={r.get('total_seconds')}s")
wc = r.get("workflow_comparison") or {}
print(f"  workflow: session {wc.get('session_total_seconds')}s vs "
      f"naive CPU {wc.get('naive_cpu_seconds')}s = {wc.get('speedup_x')}x")
check("close succeeded", r.get("ok") is True)
check("workflow comparison produced", isinstance(wc.get("speedup_x"), (int, float)))

print("=== unknown session must be an explicit error, not a crash ===")
r = call({"cmd": "analyze", "sid": "nope", "op": "summary"})
check("unknown session errors cleanly", r.get("ok") is False, str(r.get("error"))[:60])
r = call({"cmd": "badcmd"})
check("unknown command errors cleanly", r.get("ok") is False, str(r.get("error"))[:60])

p.stdin.close()

print()
if FAILS:
    print(f"FAILED ({len(FAILS)}): {FAILS}")
    sys.exit(1)
print("ALL SESSION WORKER CHECKS PASSED")