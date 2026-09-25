#!/usr/bin/env python3
"""Test the dataset_session TOOL LAYER (skills.py), not the worker directly.

Covers: tool registration, the open/analyze/close lifecycle through the agent's own code
path, that every step reports full-data row counts, the GPU-vs-CPU workflow figure, and the
failure modes the agent must survive (unknown session, bad op, missing file, double close).

Run on GB10 from the agent directory:  python session_skill_test.py /path/to/data.csv
"""
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import skills  # noqa: E402

DATA = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("DEMO_DATA", "sales_demo.csv")
FAILS = []


def check(label, ok, detail=""):
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}{'  ' + detail if detail else ''}")
    if not ok:
        FAILS.append(label)


def call(**kw):
    raw = skills.dataset_session(**kw)
    assert isinstance(raw, str), f"dataset_session must return a string, got {type(raw)}"
    return json.loads(raw)


print("=== tool registration ===")
names = [d["function"]["name"] for d in skills.load_skill_definitions()]
check("dataset_session is offered to the model", "dataset_session" in names, str(names))
check("analyze_dataset still offered", "analyze_dataset" in names)
check("dataset_session is in the func map", "dataset_session" in skills.skill_func_map)

print("=== open ===")
t0 = time.perf_counter()
r = call(operation="open", file_path=DATA)
print(f"  success={r.get('success')} rows={r.get('rows')} engine={r.get('engine')} "
      f"load={r.get('load_seconds')}s resident={r.get('resident_mb')}MB "
      f"(wall {time.perf_counter() - t0:.2f}s)")
check("open returns success", r.get("success") is True, str(r.get("error"))[:80])
sid = r.get("session_id")
check("a session_id came back", bool(sid))
check("rows reported", r.get("rows") == 20_000_000, f"rows={r.get('rows')}")
check("engine is cudf", r.get("engine") == "cudf")
check("contract text present", "常驻" in str(r.get("contract", "")))

print("=== a realistic multi-step drill-down, all through the tool layer ===")
plan = [
    ("profile", {}),
    ("outliers", {"columns": "revenue"}),
    ("groupby", {"by": "region", "agg": "revenue:sum,mean", "top_k": 5}),
    ("groupby", {"by": "category", "agg": "profit:mean", "top_k": 5}),
    ("corr", {"top_k": 5}),
]
step_times = []
for op, kw in plan:
    r = call(operation="analyze", session_id=sid, op=op, **kw)
    ok = r.get("success") is True
    step_times.append(r.get("step_seconds", 0))
    print(f"  {op:<9} ok={ok} rows={r.get('rows_scanned')} "
          f"step={r.get('step_seconds')}s cum={r.get('cumulative_seconds')}s")
    if not ok:
        print(f"      error: {r.get('error')}")
    check(f"{op} via tool layer", ok)
    check(f"{op} scanned all rows", r.get("rows_scanned") == 20_000_000)

print("=== the result payload is actually usable, not just 'ok' ===")
r = call(operation="analyze", session_id=sid, op="groupby",
         by="region", agg="revenue:sum", top_k=5)
g = r.get("groupby") or {}
# Engine contract: `groups` is the COUNT of groups; the rows live under `top_k`.
check("group count reported", g.get("groups") == 5, f"groups={g.get('groups')}")
rows = g.get("top_k") or []
check("group rows are present", isinstance(rows, list) and len(rows) == 5,
      f"{len(rows) if isinstance(rows, list) else type(rows).__name__} rows")
if isinstance(rows, list) and rows:
    print(f"  first group: {json.dumps(rows[0], ensure_ascii=False)}")
    check("rows carry the aggregated values",
          any(str(k).startswith("revenue") for k in rows[0]), str(sorted(rows[0])))
check("groupby reports its grouping column", g.get("by") == "region")
check("per-step note explains the resident frame", "未重新读盘" in str(r.get("note", "")))

print("=== close reports the workflow-level figure ===")
r = call(operation="close", session_id=sid)
wc = r.get("workflow_comparison") or {}
print(f"  steps={r.get('steps')} total={r.get('total_seconds')}s")
print(f"  session {wc.get('session_total_seconds')}s vs naive CPU "
      f"{wc.get('naive_cpu_seconds')}s = {wc.get('speedup_x')}x")
check("close succeeded", r.get("success") is True)
check("workflow speedup reported", isinstance(wc.get("speedup_x"), (int, float)),
      str(wc.get("speedup_x")))
check("workflow note keeps the claim honest",
      "不等于纯 GPU 计算加速比" in str(wc.get("note", "")))

print("=== failure modes the agent must survive ===")
r = call(operation="analyze", session_id="nonexistent", op="summary")
check("analyze on unknown session fails cleanly",
      r.get("success") is False and "会话不存在" in str(r.get("error")), str(r.get("error"))[:60])
r = call(operation="close", session_id="nonexistent")
check("close on unknown session fails cleanly", r.get("success") is False)
r = call(operation="open", file_path="/nope/missing.csv")
check("open on missing file fails cleanly",
      r.get("success") is False and "不存在" in str(r.get("error")), str(r.get("error"))[:60])
r = call(operation="bogus")
check("bad operation rejected", r.get("success") is False)
r = call(operation="analyze")
check("analyze without session_id rejected",
      r.get("success") is False and "session_id" in str(r.get("error")))
r = call(operation="list")
check("list works with no sessions open", r.get("success") is True, f"count={r.get('count')}")

print("=== a bad op must be rejected by op validation, not by a missing session ===")
# This ordering matters. An earlier version of this test caught the wrong branch: the session
# had already been closed, so the call failed with "session does not exist" and the check
# passed without ever exercising the op validator.
r = call(operation="open", file_path=DATA)
sid_valid = r.get("session_id")
check("reopened for the op-validation check", bool(sid_valid), str(r.get("error"))[:60])
if sid_valid:
    r = call(operation="analyze", session_id=sid_valid, op="not_an_op")
    err = str(r.get("error", ""))
    check("bad op names the valid operations",
          r.get("success") is False and "不支持的操作" in err, err[:70])

print("=== the memory guard must refuse rather than let the box OOM ===")
# Force the guard by demanding an impossible amount of headroom, then confirm the refusal
# carries actionable numbers and that a normal open still works afterwards.
_orig_head = os.environ.get("SESSION_MEM_HEADROOM")
os.environ["SESSION_MEM_HEADROOM"] = "100000"
# The guard reads the environment at import time, so exercise it through the worker's own
# code path instead: call the guard function directly with the inflated setting.
try:
    import importlib

    sys.path.insert(0, os.path.dirname(skills._find_engine()))
    import gpu_session
    importlib.reload(gpu_session)
    r2 = gpu_session.do_open({"path": DATA})
    check("refuses when memory is insufficient",
          r2.get("ok") is False and "显存不足" in str(r2.get("error", "")),
          str(r2.get("error", ""))[:90])
    check("refusal reports the numbers needed to act on",
          all(k in r2 for k in ("file_gb", "free_gb", "need_gb")), str(sorted(r2)))
    if r2.get("file_gb"):
        print(f"  refused: file={r2['file_gb']}GB need={r2['need_gb']}GB free={r2['free_gb']}GB")
finally:
    if _orig_head is None:
        os.environ.pop("SESSION_MEM_HEADROOM", None)
    else:
        os.environ["SESSION_MEM_HEADROOM"] = _orig_head
    importlib.reload(gpu_session)

print("=== after a refusal, the normal path still works ===")
r = call(operation="analyze", session_id=sid_valid, op="summary")
check("session unaffected by the guard test", r.get("success") is True, str(r.get("error"))[:60])
call(operation="close", session_id="all")

print("=== the worker must survive garbage and keep serving ===")
r = call(operation="open", file_path=DATA)
sid2 = r.get("session_id")
check("worker restarts/recovers for a new session", bool(sid2), str(r.get("error"))[:80])
if sid2:
    r = call(operation="analyze", session_id=sid2, op="summary")
    check("worker still serves after the error barrage", r.get("success") is True)
    call(operation="close", session_id="all")

print()
if FAILS:
    print(f"FAILED ({len(FAILS)}): {FAILS}")
    sys.exit(1)
print("ALL dataset_session TOOL-LAYER CHECKS PASSED")