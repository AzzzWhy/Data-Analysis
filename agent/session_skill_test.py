#!/usr/bin/env python3
"""Test the dataset_session TOOL LAYER (skills.py), not the worker directly.

Covers: tool registration, the open/analyze/close lifecycle through the agent's own code
path, that every step reports full-data row counts, the GPU-vs-CPU workflow figure, the memory
guard's refusal logic, and the failure modes the agent must survive (unknown session, bad op,
missing file, double close).

Run with no arguments on any machine: a small fixture is generated locally so every assertion
that is really about logic -- not about a device -- still runs. On a machine with cuDF the same
command exercises the GPU path for real.

    python session_skill_test.py                     # generate a fixture, run everything testable
    python session_skill_test.py /path/to/data.csv   # against an existing dataset
    python session_skill_test.py --require-gpu       # fail unless cuDF actually ran

Assertions that need a device are not silently dropped: the cuDF branch is additionally asserted
against a mocked engine (the convention optimization_test.py already uses), and anything left over
prints SKIP with its reason. A guard path that no machine runs is a guard path that can rot.
"""
import argparse
import atexit
import json
import os
import shutil
import sys
import tempfile
import time
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import skills  # noqa: E402

try:
    _SCRIPTS = os.path.dirname(skills._find_engine())
except Exception as _exc:
    _SCRIPTS = ""
    print(f"engine script not found: {_exc}")
    sys.exit(2)
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)

try:
    import pandas as pd
    import gpu_analytics as ga
    import gpu_session as gs
    from smoke_test import make_fixture
    _ENGINE_IMPORT_OK = True
    _ENGINE_IMPORT_ERR = ""
except Exception as _exc:  # keeps the tool-layer checks runnable without pandas
    pd = ga = gs = make_fixture = None
    _ENGINE_IMPORT_OK = False
    _ENGINE_IMPORT_ERR = f"{type(_exc).__name__}: {_exc}"

_ap = argparse.ArgumentParser(description="dataset_session tool-layer assertions")
_ap.add_argument("data", nargs="?", default=None,
                 help="dataset to open; generated locally when omitted")
_ap.add_argument("--require-gpu", action="store_true",
                 help="fail instead of SKIP when the cuDF path could not be run for real")
_ap.add_argument("--fixture-rows", type=int, default=200_000,
                 help="rows in the locally generated fixture")
args = _ap.parse_args()

FAILS = []
_GENERATED = None


def check(label, ok, detail=""):
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}{'  ' + detail if detail else ''}")
    if not ok:
        FAILS.append(label)


def skip(label, why):
    """A device-only assertion that could not run. Recorded, never reported as PASS."""
    print(f"  [SKIP] {label}  {why}")


def gpu_state():
    """(available, reason). Reads the engine's own probe, so it cannot drift from the engine."""
    if not _ENGINE_IMPORT_OK:
        return False, _ENGINE_IMPORT_ERR
    try:
        eng = ga.detect_engine()
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"
    return bool(eng.is_gpu), (eng.reason or f"engine={eng.name}")


def count_data_rows(path):
    """Independent row count, so 'scanned all rows' is checked against the file, not the engine."""
    n = 0
    with open(path, "rb") as fh:
        for _ in fh:
            n += 1
    return max(n - 1, 0)


def scanned(r):
    """rows_scanned sits at the top level for some ops and inside the op block for others
    (gpu_analytics moves it under result[op] unless op == "auto"). Same digging skills.py does."""
    value = r.get("rows_scanned")
    if value is not None:
        return value
    for key in ("auto", "groupby", "corr", "outliers", "profile", "summary"):
        block = r.get(key)
        if isinstance(block, dict) and block.get("rows_scanned") is not None:
            return block["rows_scanned"]
    return None


if args.data:
    DATA = args.data
    EXPECTED_GROUPS = None
    if not os.path.exists(DATA):
        print(f"no such file: {DATA}")
        sys.exit(2)
    EXPECTED_ROWS = count_data_rows(DATA)
else:
    # Generated inside the current directory on purpose: the discovery section asserts that
    # list_datasets finds the target by basename, and a fixture in /tmp is under no search root.
    if make_fixture is None:
        print(f"cannot generate a fixture and no dataset was given ({_ENGINE_IMPORT_ERR}).\n"
              f"Install the analysis dependencies, or pass a path: "
              f"python session_skill_test.py /path/to/data.csv")
        sys.exit(2)
    DATA = os.path.abspath(f"session_fixture_{os.getpid()}.csv")
    _truth = make_fixture(DATA, args.fixture_rows)
    EXPECTED_ROWS = _truth["rows"]
    EXPECTED_GROUPS = len(_truth["group_mean"])
    _GENERATED = DATA
    atexit.register(lambda: os.path.exists(DATA) and os.remove(DATA))
    print(f"generated fixture: {DATA} ({EXPECTED_ROWS:,} rows, "
          f"{EXPECTED_GROUPS} regions, {os.path.getsize(DATA) / 1e6:.2f} MB)  (removed on exit)")

if EXPECTED_ROWS < 1:
    print("fixture has no data rows")
    sys.exit(2)


def call(**kw):
    raw = skills.dataset_session(**kw)
    assert isinstance(raw, str), f"dataset_session must return a string, got {type(raw)}"
    return json.loads(raw)


def call_discovery(directory=None):
    raw = skills.list_datasets(directory=directory) if directory else skills.list_datasets()
    assert isinstance(raw, str), f"list_datasets must return a string, got {type(raw)}"
    d = json.loads(raw)
    # The payload key has moved once; accept either so this test cannot silently pass on an
    # empty list just because the key was renamed.
    for key in ("files", "datasets", "found"):
        if key in d:
            d["files"] = d[key]
            break
    return d


print("=== tool registration ===")
names = [d["function"]["name"] for d in skills.load_skill_definitions()]
check("dataset_session is offered to the model", "dataset_session" in names, str(names))
check("analyze_dataset still offered", "analyze_dataset" in names)
check("dataset_session is in the func map", "dataset_session" in skills.skill_func_map)

_gpu_there, _gpu_why = gpu_state()
print(f"=== engine on this machine: {'cuDF available' if _gpu_there else 'no cuDF'} "
      f"({_gpu_why}) ===")

# A session is a promise to reuse a loaded frame, and the worker refuses to make that promise to
# an engine it has not been told to keep: below the measured crossover a plain open is rejected
# with a hint to choose deliberately. So the lifecycle is driven on whichever engine this machine
# can actually hold resident, which is exactly what the tool tells the model to do.
OPEN_KW = {"force_gpu": True} if _gpu_there else {"force_cpu": True}


def open_session(path):
    return call(operation="open", file_path=path, **OPEN_KW)


print("=== open ===")
t0 = time.perf_counter()
r = open_session(DATA)
print(f"  success={r.get('success')} rows={r.get('rows')} engine={r.get('engine')} "
      f"load={r.get('load_seconds')}s resident={r.get('resident_mb')}MB "
      f"(wall {time.perf_counter() - t0:.2f}s)")
check("open returns success", r.get("success") is True, str(r.get("error"))[:80])
sid = r.get("session_id")
check("a session_id came back", bool(sid))
check("rows reported", r.get("rows") == EXPECTED_ROWS,
      f"rows={r.get('rows')} file has {EXPECTED_ROWS:,}")
# The engine choice is reported honestly on either path; which path is *right* is the routing
# layer's business and is covered by execution_decision_test.py, not asserted as a constant here.
check("engine reported honestly", r.get("engine") in ("cudf", "pandas"), f"got {r.get('engine')!r}")
if _gpu_there:
    check("engine is cudf on a GPU box", r.get("engine") == "cudf", f"got {r.get('engine')!r}")
elif args.require_gpu:
    check("engine is cudf (--require-gpu)", False, _gpu_why)
else:
    skip("engine is cudf", f"no cuDF here ({_gpu_why}); the cudf branch is asserted under "
                          f"mock below, and --require-gpu turns this into a failure")
check("contract text present", "resident" in str(r.get("contract", "")))

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
    print(f"  {op:<9} ok={ok} rows={scanned(r)} "
          f"step={r.get('step_seconds')}s cum={r.get('cumulative_seconds')}s")
    if not ok:
        print(f"      error: {r.get('error')}")
    check(f"{op} via tool layer", ok)
    check(f"{op} scanned all rows", scanned(r) == EXPECTED_ROWS,
          f"got {scanned(r)} expected {EXPECTED_ROWS:,}")

print("=== the result payload is actually usable, not just 'ok' ===")
r = call(operation="analyze", session_id=sid, op="groupby",
         by="region", agg="revenue:sum", top_k=5)
g = r.get("groupby") or {}
# Engine contract: `groups` is the COUNT of groups; the rows live under `top_k`.
if EXPECTED_GROUPS is None:
    # Against someone else's dataset the absolute group count is not knowable here, and the
    # independent value is already checked by smoke_test against pandas ground truth. Assert the
    # contract instead: every group is returned when top_k covers them all.
    check("group count reported", isinstance(g.get("groups"), int) and g.get("groups") >= 2,
          f"groups={g.get('groups')}")
    check("group rows match the reported group count",
          isinstance(g.get("top_k"), list) and len(g.get("top_k") or []) == min(5, g.get("groups") or 0),
          f"{len(g.get('top_k') or [])} rows for groups={g.get('groups')}")
else:
    check("group count reported", g.get("groups") == EXPECTED_GROUPS,
          f"groups={g.get('groups')} fixture has {EXPECTED_GROUPS}")
    rows = g.get("top_k") or []
    check("group rows are present", isinstance(rows, list) and len(rows) == EXPECTED_GROUPS,
          f"{len(rows) if isinstance(rows, list) else type(rows).__name__} rows")
rows = g.get("top_k") or []
if isinstance(rows, list) and rows:
    print(f"  first group: {json.dumps(rows[0], ensure_ascii=False)}")
    check("rows carry the aggregated values",
          any(str(k).startswith("revenue") for k in rows[0]), str(sorted(rows[0])))
check("groupby reports its grouping column", g.get("by") == "region")
check("per-step note explains the resident frame",
      any(k in str(r.get("note", "")) for k in ("resident", "in memory", "re-read")))

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
      any(k in str(wc.get("note", "")) for k in ("not a pure", "not purely", "does not equal")))

print("=== the cuDF branch is asserted under mock, not skipped ===")
# Without this block a GPU-less machine would verify zero of the cudf code path. The engine is
# faked the way optimization_test.py does it, so what is tested is the worker's own reporting and
# decision record -- not whether cuDF happens to be installed.
if not _ENGINE_IMPORT_OK:
    skip("mocked cuDF open", f"engine modules unavailable ({_ENGINE_IMPORT_ERR})")
else:
    try:
        with tempfile.TemporaryDirectory() as _root:
            _src = os.path.join(_root, "mock_gpu.csv")
            make_fixture(_src, 2_000)
            gs.SESSIONS.clear()
            gs.WARM_CACHE.clear()
            _fake = ga.Engine("cudf", pd, version="mock", gpu_name="mock device")
            with mock.patch.object(gs.GA, "detect_engine", return_value=_fake), \
                    mock.patch.object(gs, "_free_gpu_gb", return_value=None):
                opened = gs.do_open({"path": _src, "force_gpu": True})
                check("mocked cuDF open succeeds", opened.get("ok") is True,
                      str(opened.get("error"))[:80])
                check("mocked cuDF open reports engine=cudf", opened.get("engine") == "cudf",
                      f"got {opened.get('engine')!r}")
                check("mocked cuDF open reports accelerated", opened.get("accelerated") is True)
                dec = opened.get("execution_decision") or {}
                check("decision record shows cudf selected AND delivered",
                      dec.get("selected_backend") == "cudf"
                      and dec.get("actual_backend") == "cudf",
                      f"selected={dec.get('selected_backend')} actual={dec.get('actual_backend')}")
                closed = gs.do_close({"sid": opened.get("session_id"), "retain": True})
                check("a GPU frame is retained for the next question",
                      closed.get("cached") is True, f"retained={closed.get('retained')}")
                reused = gs.do_open({"path": _src, "force_gpu": True})
                check("the retained frame is reused instead of re-parsing",
                      reused.get("cache_hit") is True,
                      f"cache_hit={reused.get('cache_hit')}")
                dec2 = reused.get("execution_decision") or {}
                check("reuse is reported as reuse, not as a fresh load",
                      dec2.get("policy") == "warm_cache", f"policy={dec2.get('policy')!r}")
                gs.do_close({"sid": "all"})
        # _retain() requires engine.is_gpu, so on a CPU-only box nothing is ever cached. That is
        # the engine's design; state it instead of pretending the pandas path reuses frames.
        if not _gpu_there:
            print("  note: without cuDF the warm cache never fills, so cross-turn reuse does not "
                  "happen on this machine. The mock above is the only cudf-branch evidence here.")
    except Exception as _exc:
        check("mocked cuDF open runs", False, f"{type(_exc).__name__}: {_exc}")

print("=== failure modes the agent must survive ===")
r = call(operation="analyze", session_id="nonexistent", op="summary")
check("analyze on unknown session fails cleanly",
      r.get("success") is False and
      any(k in str(r.get("error")) for k in ("does not exist", "no such", "unknown", "not found")),
      str(r.get("error"))[:60])
r = call(operation="close", session_id="nonexistent")
check("close on unknown session fails cleanly", r.get("success") is False)
r = call(operation="open", file_path="/nope/missing.csv")
check("open on missing file fails cleanly",
      r.get("success") is False and
      any(k in str(r.get("error")) for k in ("does not exist", "no such", "not found")),
      str(r.get("error"))[:60])
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
r = open_session(DATA)
sid_valid = r.get("session_id")
check("reopened for the op-validation check", bool(sid_valid), str(r.get("error"))[:60])
if sid_valid:
    r = call(operation="analyze", session_id=sid_valid, op="not_an_op")
    err = str(r.get("error", ""))
    check("bad op names the valid operations",
          r.get("success") is False and
          any(k in err for k in ("unsupported", "invalid", "unknown")), err[:70])

print("=== the memory guard refuses rather than letting the box OOM ===")
# The guard reads _free_gpu_gb(), which returns None when there is no device to ask -- so with a
# real CPU-only reading the guard is inert by design and every load passes. Testing it therefore
# means fixing the READING, not needing the device: the arithmetic below is the same arithmetic
# that protects a real box, and it runs here.
if not _ENGINE_IMPORT_OK:
    skip("memory guard", f"engine modules unavailable ({_ENGINE_IMPORT_ERR})")
else:
    _orig_free = gs._free_gpu_gb
    try:
        gs._free_gpu_gb = lambda: 0.0
        r2 = gs.do_open({"path": DATA, "force_gpu": True})
        check("guard logic refuses when free memory is short",
              r2.get("ok") is False and
              any(k in str(r2.get("error", "")).lower()
                  for k in ("not enough", "insufficient", "refused", "out of memory")),
              str(r2.get("error", ""))[:90])
        check("refusal reports the numbers needed to act on",
              all(k in r2 for k in ("file_gb", "free_gb", "need_gb")), str(sorted(r2)))
        if r2.get("file_gb"):
            print(f"  refused: file={r2['file_gb']}GB need={r2['need_gb']}GB free={r2['free_gb']}GB")
    finally:
        gs._free_gpu_gb = _orig_free

    # The refusal is a last step; the loop before it evicts warm frames first, which is the part
    # that can quietly fail to run. Seed the cache and assert it was drained on the way to no.
    _orig_free = gs._free_gpu_gb
    try:
        _ghost = os.path.join(os.getcwd(), "ghost.csv")
        gs.WARM_CACHE.clear()
        gs.WARM_CACHE[(_ghost, None)] = gs.Session(
            sid="ghost", path=_ghost, engine=ga.Engine("cudf", pd),
            frame=pd.DataFrame({"a": [1, 2, 3]}), rows=3,
            identity=[_ghost, 10, 0.0], load_seconds=0.0)
        gs._free_gpu_gb = lambda: 0.0
        r3 = gs.do_open({"path": DATA, "force_gpu": True})
        check("warm frames are evicted before the load is refused",
              not gs.WARM_CACHE and r3.get("ok") is False,
              f"cache={len(gs.WARM_CACHE)} ok={r3.get('ok')}")
    finally:
        gs._free_gpu_gb = _orig_free
        gs.WARM_CACHE.clear()

print("=== after a refusal, the normal path still works ===")
r = call(operation="analyze", session_id=sid_valid, op="summary")
check("session unaffected by the guard test", r.get("success") is True, str(r.get("error"))[:60])
call(operation="close", session_id="all")

print("=== the worker must survive garbage and keep serving ===")
r = open_session(DATA)
sid2 = r.get("session_id")
check("worker restarts/recovers for a new session", bool(sid2), str(r.get("error"))[:80])
if sid2:
    r = call(operation="analyze", session_id=sid2, op="summary")
    check("worker still serves after the error barrage", r.get("success") is True)
    call(operation="close", session_id="all")

print("=== dataset discovery must find what the resolver can find ===")
# This had no coverage at all, and the gap hid a real hole: _resolve_data_path searched the
# parent directory while list_datasets searched only the current one, so discovery returned
# nothing useful for a dataset that resolution could handle fine. The first version of this
# fix then walked the whole home directory and returned 264 files, burying the target.
_disc = call_discovery()
_home = os.path.abspath(os.path.expanduser("~"))
_basename = os.path.basename(DATA)
_paths = [f.get("path", "") for f in _disc.get("files", []) or _disc.get("datasets", [])]
check("list_datasets finds the target dataset by basename",
      any(os.path.basename(p) == _basename for p in _paths),
      f"{len(_paths)} files listed")
check("discovery searches the roots it declares, not just the current directory",
      sorted(_disc.get("searched_dirs", [])) == sorted(r[0] for r in skills._data_search_roots()),
      f"reported={_disc.get('searched_dirs')}")
check("the home directory is a listing root at depth 0 only",
      any(os.path.abspath(d) == _home and depth == 0 for d, depth in skills._data_search_roots()),
      f"roots={skills._data_search_roots()}")

print("=== discovery depth is a real bound, not a payload truncation ===")
# "len(files) <= 40" could never fail: list_datasets truncates its payload to 40 regardless of
# how far it walked. A bound is only proven by a file that sits past it and is not found, so
# markers are planted at each side of the limit and the untruncated `count` is what is asserted.
_outside = [p for p in _paths
            if p.startswith(_home + os.sep)
            and not p.startswith(os.path.abspath(os.getcwd()) + os.sep)]
check("discovery does not descend into the home directory", not _outside,
      f"{len(_outside)} path(s) from a home subdirectory, e.g. {_outside[:1]}")

if not pd:
    skip("depth markers", "pandas unavailable")
else:
    _probe = os.path.join(os.getcwd(), f"_probe_{os.getpid()}")
    try:
        blob = b"a" * 2048
        os.makedirs(os.path.join(_probe, "a", "b"), exist_ok=True)
        near = os.path.join(_probe, "a", "marker_near.csv")       # one level below the root
        deep = os.path.join(_probe, "a", "b", "c", "marker_deep.csv")
        os.makedirs(os.path.join(_probe, "a", "b", "c"), exist_ok=True)
        for _p in (near, deep):
            with open(_p, "wb") as fh:
                fh.write(b"x\n" + blob)
        _d2 = call_discovery()
        _p2 = [f.get("path", "") for f in _d2.get("files", [])]
        check("a file just inside the depth bound is listed",
              os.path.basename(near) in {os.path.basename(p) for p in _p2},
              f"{len(_p2)} files listed, count={_d2.get('count')}")
        check("a file past the depth bound is NOT found",
              os.path.basename(deep) not in {os.path.basename(p) for p in _p2},
              f"marker_deep visible in {_p2[:2]}")
        check("the untruncated count reports the whole walk",
              isinstance(_d2.get("count"), int) and _d2["count"] >= len(_p2),
              f"count={_d2.get('count')} files={len(_p2)}")
    finally:
        import shutil
        shutil.rmtree(_probe, ignore_errors=True)

_r_explicit = call_discovery(directory="/definitely/not/a/directory")
check("explicit bad directory is reported, not silently ignored",
      _r_explicit.get("success") is False, str(_r_explicit.get("error"))[:70])

print()
if FAILS:
    print(f"FAILED ({len(FAILS)}): {FAILS}")
    sys.exit(1)
print("ALL dataset_session TOOL-LAYER CHECKS PASSED")
