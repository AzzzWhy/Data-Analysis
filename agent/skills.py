"""
Skills for the DGX Spark / StepFun data-analysis agent.

Two mechanisms live here, and the distinction matters:

  * `skill_definitions` — OpenAI function-calling schemas. This is what the LLM sees and
    uses to decide *whether and how* to call a skill, so the `description` text is a
    trigger specification, not documentation.
  * `skill_func_map` — name -> implementation. What actually runs, locally, on the GB10.

`analyze_dataset` is the stateless accelerated path: it shells out to
`cudf-analytics/scripts/gpu_analytics.py`, which does full-file aggregation with cuDF on the
GPU and falls back to pandas when no GPU is usable.

`dataset_session` is the stateful path: it loads a file into GPU memory once, via a
long-lived `gpu_session.py` worker, and then runs many analyses against that resident copy.
Measured on a 20M-row, 3.0 GB CSV, that turns a 5-step analysis from 10.8 s (re-reading each
step on the GPU) into 1.5 s. That is what makes multi-step drill-down affordable.

The original `load_csv_dataset` is kept for compatibility with the earlier agent.
"""

import atexit
import json
import os
import subprocess
import sys
import threading

import pandas as pd

# --------------------------------------------------------------------------------------
# Where the analytics engine lives
# --------------------------------------------------------------------------------------

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))

# Candidate directories holding the skill's scripts, tried in order.
#
# This is a list of DIRECTORIES, not files, so that adding a script to the skill needs no
# change here. An earlier version listed full file paths, and a second helper joined a
# filename onto those file paths -- which silently found nothing. One source of truth.
_SKILL_SCRIPT_DIRS = [
    os.path.join(_THIS_DIR, "cudf-analytics-skill", "scripts"),
    os.path.join(_THIS_DIR, "skills", "cudf-analytics", "scripts"),
    os.path.join(_THIS_DIR, "scripts"),
    os.path.join(os.path.dirname(_THIS_DIR), "skills", "cudf-analytics", "scripts"),
    os.path.expanduser("~/cudf-analytics-skill/scripts"),
]

# Standalone deployments can point at a specific engine file instead.
_ENV_ENGINE = os.environ.get("GPU_ANALYTICS_SCRIPT")


def _find_script(basename: str) -> str:
    """Locate a script belonging to the skill, or raise naming everywhere we looked."""
    if _ENV_ENGINE and basename == "gpu_analytics.py" and os.path.isfile(_ENV_ENGINE):
        return _ENV_ENGINE
    tried = []
    for d in _SKILL_SCRIPT_DIRS:
        cand = os.path.join(d, basename)
        tried.append(cand)
        if os.path.isfile(cand):
            return cand
    raise FileNotFoundError(f"{basename} not found. Set GPU_ANALYTICS_SCRIPT or place the "
                            f"skill at one of: {tried}")


def find_engine() -> str:
    """Resolve the analytics engine path, or raise with somewhere useful to look.

    Public entry point: other scripts (e.g. gpu_vs_cpu_demo.py) reuse this rather than
    re-implementing path discovery, so a layout change only has to be handled once.
    """
    return _find_engine()


def _find_engine() -> str:
    """Resolve the analytics engine path, or raise with somewhere useful to look."""
    return _find_script("gpu_analytics.py")


# Interpreter that has pandas/cuDF. Defaults to the running interpreter.
def _python_bin() -> str:
    return os.environ.get("GPU_ANALYTICS_PYTHON", sys.executable)


# --------------------------------------------------------------------------------------
# Skill definitions (what the LLM sees)
# --------------------------------------------------------------------------------------

skill_definitions = [
    {
        "type": "function",
        "function": {
            "name": "dataset_session",
            "description": (
                "Load a data file into device memory once, then analyse it repeatedly in the "
                "same session so no step re-reads it from disk. Use it for multi-step work: find "
                "outliers then trace where they come from, compare several dimensions, move from "
                "an overview into a drill-down."
                "\n\nWhy: with the full dataset resident, each later step drops from seconds to "
                "tens of milliseconds (measured on 20M rows: about 1.9 s to load, then 0.03-0.5 s "
                "per step). Multi-step drill-down becomes cheap, and every step still covers the "
                "full data, with no sampling."
                "\n\nCall it in three steps:"
                "1) operation='open' + file_path: opens the file and returns a session_id; "
                "2) operation='analyze' + session_id + op: repeated analysis, taking the same op "
                "values as analyze_dataset (profile/summary/groupby/corr/outliers/auto), plus "
                "by/agg/columns/top_k; "
                "3) operation='close' + session_id: required when the analysis ends, to release "
                "device memory."
                "\n\nUse it when: the request needs several steps, for example \"find the "
                "outliers and explain why they occur\", \"compare performance across dimensions\", "
                "\"take an overview then drill into one group\"."
                "\n\nDo not use it for: one-step questions (analyze_dataset is enough and holds "
                "less device memory); charting; editing data files. A one-off question in a "
                "session only occupies device memory."
                "\n\nNote: a session keeps holding device memory (about 1.7GB for 20M rows). "
                "close it when the analysis is done. If open is rejected for lack of device "
                "memory, go back to analyze_dataset."
                "\n\nPlanning is hosted: pass the user's goal verbatim in goal at open time and "
                "the system immediately returns a plan for that goal, an ordered set of concrete "
                "steps with what to do next. After each step the response carries the current plan "
                "progress (how many steps are done, how many are left, what comes next). You do "
                "not have to remember where you got to; the system keeps the books. A plan step "
                "that looks wrong for the goal can be skipped, and the system marks it off-plan."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "operation": {
                        "type": "string",
                        "enum": ["open", "analyze", "list", "close"],
                        "description": "open=load the file and return a session_id; "
                                       "analyze=run an analysis on the session; list=show the "
                                       "open sessions; close=release them",
                    },
                    "file_path": {
                        "type": "string",
                        "description": "file to load when operation='open'",
                    },
                    "session_id": {
                        "type": "string",
                        "description": "session id returned by open, used with "
                                       "operation='analyze'/'close', e.g. s1",
                    },
                    "op": {
                        "type": "string",
                        "enum": ["auto", "profile", "summary", "groupby", "corr", "outliers"],
                        "description": "analysis to run when operation='analyze', same meanings "
                                       "as in analyze_dataset",
                    },
                    "by": {"type": "string", "description": "grouping column when op='groupby'"},
                    "agg": {
                        "type": "string",
                        "description": "aggregation when op='groupby', e.g. revenue:sum,mean",
                    },
                    "columns": {
                        "type": "string",
                        "description": "columns to analyse, comma-separated, e.g. revenue,cost",
                    },
                    "top_k": {
                        "type": "integer",
                        "description": "how many rows groupby/corr/outliers return, default 20",
                    },
                    "goal": {
                        "type": "string",
                        "description": "the goal the user wants to reach when operation='open' "
                                       "(their own wording is fine). With it, the system returns "
                                       "a plan for that goal and tracks progress after every "
                                       "step; recommended for multi-step requests.",
                    },
                },
                "required": ["operation"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "analyze_dataset",
            "description": (
                "Run real GPU-accelerated statistics over a local tabular data file and return "
                "exact results. Covers: a profile (rows, columns, types, missing values), summary "
                "statistics (count/mean/std/min/Q1/median/Q3/max), grouped aggregation, a "
                "correlation matrix, and IQR outlier detection."
                "\n\nCall it when: the user asks to analyse, count, summarise, aggregate, "
                "correlate or find outliers in any local data file (CSV, Parquet, TSV, JSONL, "
                "Excel); or mentions a large dataset, millions or tens of millions of rows, says "
                "pandas is too slow, or worries the job will not finish."
                "\n\nHard constraint: do not estimate statistics from a sample you read into the "
                "context. Call this tool so the GPU computes over the full data. rows_scanned in "
                "the result is the full row count."
                "\n\nDo not use it for: charting, training models, querying remote databases, "
                "editing data files, or a single arithmetic operation."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "file_path": {
                        "type": "string",
                        "description": "absolute or relative path to the data file on this "
                                       "machine, e.g. /data/sales.csv"
                    },
                    "operation": {
                        "type": "string",
                        "enum": ["auto", "profile", "summary", "groupby", "corr", "outliers"],
                        "description": (
                            "analysis to run. auto=profile, summary and outliers in one call "
                            "(start with it); "
                            "profile=rows/columns/types/missing/preview; summary=per-column "
                            "statistics; "
                            "groupby=aggregate grouped by a column (needs by and agg); "
                            "corr=correlation matrix; outliers=IQR outlier detection"
                        )
                    },
                    "by": {
                        "type": "string",
                        "description": ("for groupby: column to group by, e.g. region. Needed only "
                                        "with operation=groupby")
                    },
                    "agg": {
                        "type": "string",
                        "description": (
                            "for groupby: aggregation expression, format "
                            "'column:func1,func2|column2:func3', "
                            "e.g. 'revenue:sum,mean|quantity:max'. "
                            "Available functions: "
                            "count,size,sum,mean,min,max,std,median,nunique,first,last"
                        )
                    },
                    "columns": {
                        "type": "string",
                        "description": "optional: analyse only these numeric columns, "
                                       "comma-separated, e.g. revenue,cost. Empty means every "
                                       "numeric column"
                    },
                    "top_k": {
                        "type": "integer",
                        "description": "optional: groupby returns only the top K groups, corr only "
                                       "the K strongest pairs. Worth setting on large data"
                    },
                    "force_cpu": {
                        "type": "boolean",
                        "description": "optional: force CPU (pandas) so the numbers can be "
                                       "compared against the GPU. Default false"
                    }
                },
                "required": ["file_path", "operation"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "list_datasets",
            "description": (
                "List the analysable data files on this machine (CSV/Parquet/TSV/JSONL/Excel) "
                "with their sizes. Call it first when the user gave no file path, or when you are "
                "unsure which data is available."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "directory": {
                        "type": "string",
                        "description": "directory to search, current working directory by default"
                    }
                },
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "export_deliverables",
            "description": (
                "Export the analysis as files the user can take away: charts (SVG) and a Markdown "
                "report. Use it when the user says \"make a chart\", \"plot it\", \"give me a "
                "report\", \"export this\", \"save it\", \"I need this for a briefing / an "
                "email\".\n"
                "It runs the full analysis on the GPU first, then writes: report.md (with tables "
                "and charts), several .svg charts, and CSV/JSON data files.\n"
                "If the user wants charts and named no analysis type, operation=auto is enough.\n"
                "Note: the result carries the file paths, and your answer must give the user those "
                "paths."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "file_path": {
                        "type": "string",
                        "description": "data file to analyse; omit it to reuse the file analysed "
                                       "last"
                    },
                    "operation": {
                        "type": "string",
                        "description": "which analysis to run before charting, auto by default",
                        "enum": ["auto", "profile", "summary", "groupby", "corr", "outliers"]
                    },
                    "by": {"type": "string", "description": "grouping column for groupby (one "
                                                            "column only)"},
                    "agg": {"type": "string", "description": "aggregation form "
                                                             "col:func1,func2|col2:func"},
                    "columns": {"type": "string", "description": "columns to include in the "
                                                                 "analysis, comma-separated"},
                    "top_k": {"type": "integer", "description": "how many rows/groups to return"},
                    "out_dir": {"type": "string", "description": "output directory, "
                                                                 "deliverables/ by default"}
                },
                "required": []
            }
        }
    },
]


# --------------------------------------------------------------------------------------
# Output compaction
# --------------------------------------------------------------------------------------

def _compact(obj, depth: int = 0, max_list: int = 30, max_depth: int = 4):
    """
    Shrink a result payload for the model's context without changing any number.

    A 12x12 correlation matrix or a 100k-row outlier table would otherwise flood the
    conversation. Lists keep their first `max_list` entries (they are already sorted by
    relevance, e.g. strongest correlations) and every list records how many were dropped,
    so a truncation can never be mistaken for the whole result.
    """
    if depth >= max_depth:
        if isinstance(obj, (dict, list)):
            return f"<{type(obj).__name__} truncated: {len(obj)} entries>"
        return obj
    if isinstance(obj, dict):
        # Keep small maps (like a 12-column dtype map) intact, they carry real signal.
        return {k: _compact(v, depth + 1, max_list, max_depth) for k, v in obj.items()}
    if isinstance(obj, list):
        if len(obj) <= max_list:
            return [_compact(v, depth + 1, max_list, max_depth) for v in obj]
        kept = [_compact(v, depth + 1, max_list, max_depth) for v in obj[:max_list]]
        kept.append(f"... {len(obj) - max_list} more entries omitted (total {len(obj)})")
        return kept
    return obj


def _trim_matrix(matrix: dict, max_cols: int = 8) -> dict:
    """Keep a correlation matrix readable: at most max_cols columns per axis."""
    if not isinstance(matrix, dict):
        return matrix
    cols = list(matrix.keys())[:max_cols]
    out = {}
    for r in cols:
        row = matrix.get(r)
        if isinstance(row, dict):
            out[r] = {c: row[c] for c in cols if c in row}
        else:
            out[r] = row
    if len(matrix) > max_cols:
        out[f"<{len(matrix) - max_cols} more columns omitted>"] = f"of {len(matrix)} total"
    return out


# --------------------------------------------------------------------------------------
# GPU vs CPU comparison shown inside the answer
# --------------------------------------------------------------------------------------

# Persisted so `demo_script.py --prewarm` (a separate process) can warm the comparisons that
# a later live run will display. Keys include file size and mtime, so editing the dataset
# invalidates its entries rather than serving a stale ratio.
_COMPARISON_CACHE_PATH = os.path.join(_THIS_DIR, ".gpu_vs_cpu_cache.json")
_LAST_FILE_PATH = os.path.join(_THIS_DIR, ".last_dataset_path")


def _cache_load() -> dict:
    """
    Load persisted comparisons, rebuilding the key structure JSON destroyed.

    JSON has no tuples, so a key like (file_identity_tuple, op, ...) round-trips as
    [[path, size, mtime], op, ...]. Rebuilding only the outer level leaves the inner file
    identity as a *list*, which never equals the tuple produced at lookup time -- so every
    lookup missed and the cache silently did nothing. Both levels are restored here.
    """
    try:
        with open(_COMPARISON_CACHE_PATH, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
        restored = {}
        for key_str, value in raw.items():
            parts = json.loads(key_str)
            if not isinstance(parts, list) or not parts:
                continue
            head = parts[0]
            if isinstance(head, list):
                head = tuple(head)
            restored[(head, *parts[1:])] = value
        return restored
    except Exception:
        return {}


def _cache_save(cache: dict) -> None:
    try:
        with open(_COMPARISON_CACHE_PATH, "w", encoding="utf-8") as fh:
            json.dump({json.dumps(list(k)): v for k, v in cache.items()}, fh, indent=1)
    except Exception:
        # A cache write failure must never break the analysis.
        pass


# Keyed by (file identity, operation, argument signature) -> comparison dict.
_COMPARISON_CACHE: dict[tuple, dict] = _cache_load()


def _file_identity(path: str) -> tuple:
    try:
        st = os.stat(path)
        return (os.path.abspath(path), st.st_size, int(st.st_mtime))
    except OSError:
        return (os.path.abspath(path), None, None)


def _speedup_enabled() -> bool:
    """Enabled by default; set SKILL_SHOW_SPEEDUP=0 to turn the extra CPU run off."""
    return os.environ.get("SKILL_SHOW_SPEEDUP", "1").strip().lower() not in {"0", "false", "no"}


def _rows_of(payload: dict) -> int | None:
    """Row count from either the top level or the operation's own block."""
    rows = payload.get("rows_scanned")
    if isinstance(rows, int) and not isinstance(rows, bool):
        return rows
    for key in ("groupby", "corr", "outliers", "profile", "summary"):
        block = payload.get(key)
        if isinstance(block, dict):
            inner = block.get("rows_scanned")
            if isinstance(inner, int) and not isinstance(inner, bool):
                return inner
    return None


def _cpu_twin(path: str, operation: str, by, agg, columns, top_k) -> dict | None:
    """
    Re-run the same analysis with pandas forced, and report how much slower it was.

    Two deliberate methodology choices, because a naked ratio is easy to attack:
      * the CPU side runs twice and the BEST time is used, so CPU startup jitter cannot
        flatter the GPU;
      * the reported figure is the full analysis time (`total_seconds`), not just the
        compute kernel, so the number is comparable to what the user waited for.

    Returns None when a comparison is not meaningful (GPU was not used).
    """
    try:
        engine = _find_engine()
        cmd = [_python_bin(), engine, "--input", path, "--op", operation, "--force-cpu"]
        if by:
            cmd += ["--by", str(by)]
        if agg:
            cmd += ["--agg", str(agg)]
        if columns:
            cmd += ["--columns", str(columns)]
        if top_k:
            cmd += ["--top-k", str(int(top_k))]

        best = None
        cpu_engine = None
        cpu_rows = None
        for _ in range(2):
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
            if proc.returncode != 0:
                return None
            try:
                payload = json.loads(proc.stdout)
            except json.JSONDecodeError:
                return None
            cpu_engine = payload.get("engine", "pandas")
            cpu_rows = _rows_of(payload)
            secs = payload.get("total_seconds")
            if not isinstance(secs, (int, float)):
                return None
            best = secs if best is None else min(best, secs)
        return {"baseline_engine": cpu_engine, "baseline_seconds": best,
                "rows_scanned": cpu_rows}
    except Exception:
        # A comparison is a nice-to-have; never let it break the actual analysis.
        return None


def warm_comparison(path: str, operation: str, by=None, agg=None, columns=None,
                    top_k=None) -> dict | None:
    """
    Measure the CPU baseline for one query and cache it, including a per-operation default
    entry so a later run with slightly different optional arguments still resolves.

    Returns the cached comparison, or None if it could not be measured.
    """
    try:
        identity = _file_identity(path)
        exact = _cpu_twin(path, str(operation), by, agg, columns, top_k)
        if exact is None:
            return None
        _COMPARISON_CACHE[(identity, str(operation), str(by), str(agg), str(columns),
                           str(top_k))] = exact
        _COMPARISON_CACHE[(identity, str(operation), "default", "", "", "")] = exact
        _cache_save(_COMPARISON_CACHE)
        return exact
    except Exception:
        return None


def _attach_speedup(out: dict, path: str, operation: str, by, agg, columns, top_k,
                    gpu_payload: dict) -> None:
    """Add a same-session GPU-vs-CPU measurement to the tool result.

    The ratio compares like with like: `total_seconds` on the GPU against the best of two
    CPU runs of the identical command on the identical file.
    """
    if not _speedup_enabled() or gpu_payload.get("engine") != "cudf":
        return
    gpu_secs = gpu_payload.get("total_seconds")
    if not isinstance(gpu_secs, (int, float)) or gpu_secs <= 0:
        return

    key = (_file_identity(path), str(operation), str(by), str(agg), str(columns),
           str(top_k))
    # Fallback key: the same operation with default arguments.
    #
    # The comparison cost is driven mostly by the operation and the file, while optional
    # arguments (top_k, a column filter) barely change it. The model does not produce byte
    # identical arguments every run, so an exact-match-only cache missed often and the demo
    # paused for a fresh CPU run. A warmed per-operation entry covers those variations; the
    # precise ratio is still stored on a genuine miss.
    fallback_key = (_file_identity(path), str(operation), "default", "", "", "")
    cached = _COMPARISON_CACHE.get(key) or _COMPARISON_CACHE.get(fallback_key)
    if cached is None:
        cached = _cpu_twin(path, str(operation), by, agg, columns, top_k)
        if cached is None:
            return
        _COMPARISON_CACHE[key] = cached
        _cache_save(_COMPARISON_CACHE)

    cpu_secs = cached["baseline_seconds"]
    if not cpu_secs or cpu_secs <= 0:
        return

    # Guard: a ratio is only meaningful if both engines processed the same amount of data.
    #
    # This is not hypothetical. Passing `--limit` truncated BOTH runs to 5 rows while the
    # rest of the pipeline was unchanged, producing a "GPU is 0.1x" figure that looked like a
    # real measurement. Comparing scanned row counts catches that class of mistake instead of
    # shipping it to an audience.
    gpu_rows = _rows_of(gpu_payload)
    cpu_rows = cached.get("rows_scanned")
    if gpu_rows is not None and cpu_rows is not None and gpu_rows != cpu_rows:
        out["gpu_vs_cpu_warning"] = (
            f"comparison skipped: the two engines scanned different row counts "
            f"(GPU {gpu_rows} / CPU {cpu_rows}), so the comparison is not valid."
        )
        return
    if gpu_rows is not None and gpu_rows < 1000:
        out["gpu_vs_cpu_warning"] = (
            f"comparison skipped: this run scanned only {gpu_rows} rows, too small for a "
            f"meaningful speedup."
        )
        return

    ratio = cpu_secs / gpu_secs
    out["gpu_vs_cpu"] = {
        "gpu_engine": gpu_payload.get("engine"),
        "gpu_seconds": round(gpu_secs, 3),
        "cpu_engine": cached["baseline_engine"],
        "cpu_seconds": round(cpu_secs, 3),
        "speedup_x": round(ratio, 2),
        "rows_compared": gpu_rows,
        "note": (
            "same file, same command, one real run on each engine (the CPU side takes the faster "
            "of two runs). The CPU baseline is default pandas, effectively single-threaded in the "
            "C layer."
        ),
    }
    # Give the model a ready-made, honest sentence, and the caveat it must repeat when the
    # ratio is unimpressive. Small data legitimately shows little or no GPU advantage.
    if ratio < 1.2:
        out["gpu_vs_cpu"]["honest_note"] = (
            f"the GPU was only {ratio:.2f}x faster here, close to no difference, because the "
            "data is small and reading/parsing dominates. Say this plainly in the answer; do not "
            "claim a clear speedup."
        )
    else:
        out["gpu_vs_cpu"]["honest_note"] = (
            f"the GPU was {ratio:.2f}x faster than the CPU. You can say this computation used GPU "
            "acceleration, but add that reading the CSV takes a large share of the end-to-end "
            "time, that both engines use multiple cores for that part, and that the pandas "
            "baseline uses a single core, so the factor is not a pure GPU compute advantage."
        )


# --------------------------------------------------------------------------------------
# Stateful sessions: hold a dataset in GPU memory across several analyses
# --------------------------------------------------------------------------------------

# The user's own wording for the question being answered. Recorded by the agent loop, not
# exposed as a tool parameter: the report should follow the language the user wrote in, and
# asking the model to re-state the question as an argument would only add a way to get it
# wrong. Used solely to choose the report language.
_REQUEST_TEXT = ""


def remember_request_text(text: str) -> None:
    """Record the current question so generated deliverables match its language."""
    global _REQUEST_TEXT
    _REQUEST_TEXT = str(text or "")


def _request_text() -> str:
    return _REQUEST_TEXT


# A worker is one long-lived process holding the resident frames. It is started on first use
# and reused for the rest of the conversation, because its entire value is the state it keeps.
_worker_lock = threading.Lock()
_worker = None


def _session_script() -> str:
    """Locate gpu_session.py next to the engine."""
    engine = _find_engine()
    cand = os.path.join(os.path.dirname(engine), "gpu_session.py")
    if not os.path.exists(cand):
        raise FileNotFoundError(f"gpu_session.py not found (expected at {cand})")
    return cand


def _worker_start():
    """Start the session worker, or return None when it cannot be started."""
    script = _session_script()
    proc = subprocess.Popen(
        [_python_bin(), script],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, bufsize=1,
    )
    # Kill the worker when this process exits, so a crashed agent does not leave a process
    # holding gigabytes of device memory.
    atexit.register(lambda: _worker_stop(proc))
    return proc


def _worker_stop(proc) -> None:
    try:
        if proc and proc.poll() is None:
            try:
                proc.stdin.write(json.dumps({"cmd": "close", "sid": "all"}) + "\n")
                proc.stdin.flush()
            except Exception:
                pass
            proc.terminate()
    except Exception:
        pass


def _worker_call(payload: dict, timeout: float = 1800.0):
    """
    Send one request to the session worker and return its parsed response.

    Returns an error dict rather than raising: a broken worker must degrade the agent to the
    stateless path, never crash the conversation.
    """
    global _worker
    with _worker_lock:
        # Restart on demand: if the worker died (OOM, killed, crashed), the next call brings
        # up a fresh one instead of failing forever.
        if _worker is None or _worker.poll() is not None:
            if _worker is not None:
                _worker = None
            try:
                _worker = _worker_start()
            except Exception as exc:
                return {"ok": False,
                        "error": f"could not start the session process: "
                                 f"{type(exc).__name__}: {exc}"}
        try:
            _worker.stdin.write(json.dumps(payload) + "\n")
            _worker.stdin.flush()
            line = _worker.stdout.readline()
        except (BrokenPipeError, OSError) as exc:
            _worker = None
            return {"ok": False, "error": f"session process communication failed: {exc}. "
                                           f"Use analyze_dataset instead."}
        if not line:
            err = ""
            try:
                err = (_worker.stderr.read() or "")[-400:]
            except Exception:
                pass
            _worker = None
            return {"ok": False,
                    "error": f"the session process exited. {('stderr: ' + err) if err else ''}"
                             f"Use analyze_dataset for a one-off analysis."}
        try:
            return json.loads(line)
        except json.JSONDecodeError:
            return {"ok": False, "error": f"the session process returned unparseable output: "
                                          f"{line[:200]}"}


def _resolve_data_path(file_path: str) -> str:
    """
    Resolve a user-supplied path, searching likely data directories for a bare filename.

    Why this exists: an LLM naturally writes "sales_demo.csv", not the absolute path. The
    agent process runs in `agent/`, while the datasets live in the parent directory, so a
    bare filename used to fail with "file not found" -- and the model would then repeat the
    same relative path instead of switching to the absolute one it had been handed. Prompt
    instructions alone did not fix that (verified), because the model's preferred form is the
    bare filename. Resolving it here removes the failure mode rather than papering over it.

    Only applies when the given path does not exist. An existing path is always honoured.
    """
    raw = str(file_path or "").strip()
    if not raw:
        return ""
    expanded = os.path.expanduser(raw)
    if os.path.exists(expanded):
        return expanded
    if os.path.isabs(expanded):
        return expanded

    # Search the places a dataset plausibly lives, nearest first. Deliberately a short,
    # predictable list: a filesystem-wide search would be slow and unpredictable. The list is
    # shared with list_datasets so discovery and resolution cannot disagree.
    candidates = _data_search_dirs()
    for base in candidates:
        cand = os.path.join(base, raw)
        if os.path.isfile(cand):
            return cand
    # Also try the bare basename, in case the caller passed a stray directory prefix.
    name = os.path.basename(raw)
    if name and name != raw:
        for base in candidates:
            cand = os.path.join(base, name)
            if os.path.isfile(cand):
                return cand
    return expanded


def close_all_sessions() -> dict:
    """
    Release every open session. Called at the end of an agent run.

    Structural backstop, not a substitute for the model calling close: prompt instructions
    to clean up are unreliable (observed: a real run left a 1.7 GB session open), and a
    leaked resident frame degrades every later task on the box. Returns what it released.
    """
    try:
        resp = _worker_call({"cmd": "list"})
        opened = resp.get("count") or 0
        if not opened:
            return {"closed": 0}
        out = _worker_call({"cmd": "close", "sid": "all"})
        return {"closed": opened, "detail": out}
    except Exception as exc:
        return {"closed": 0, "error": f"{type(exc).__name__}: {exc}"}


def dataset_session(operation: str, file_path: str = None, session_id: str = None,
                    op: str = None, by: str = None, agg: str = None,
                    columns: str = None, top_k: int = None,
                    force_cpu: bool = False, goal: str = None) -> str:
    """
    Load a dataset into memory once, then run several analyses against that copy.

    Passing `goal` makes the session plan-bearing: a deterministic strategy for that goal is
    materialised and attached to the session, and every analyze records its own progress
    against it. The model therefore does not have to hold the plan in its head across turns.

    Never raises — failures come back as {"success": false, "error": ...} so the agent can
    fall back to the stateless tool or explain the problem.
    """
    try:
        operation = str(operation or "open").strip().lower()
        if operation not in ("open", "analyze", "list", "close"):
            return _err(f"operation must be open / analyze / list / close, got: {operation}")

        if operation == "open":
            req = {"cmd": "open", "path": _resolve_data_path(file_path),
                   "force_cpu": bool(force_cpu)}
            if goal and str(goal).strip():
                req["goal"] = str(goal).strip()
        elif operation == "analyze":
            req = {"cmd": "analyze", "sid": session_id, "op": op or "auto", "by": by,
                   "agg": agg, "columns": columns, "top_k": top_k}
        elif operation == "close":
            req = {"cmd": "close", "sid": session_id or "all"}
        else:
            req = {"cmd": "list"}

        if operation == "analyze" and not session_id:
            return _err("analyze needs a session_id (open a file first with operation='open')")

        resp = _worker_call(req)
        if not resp.get("ok"):
            # Pass the worker's own guidance through: it is written for the model, and
            # dropping it would leave the agent with an unactionable error.
            return _err(
                resp.get("error") or "session operation failed",
                hint=resp.get("hint") or "you can fall back to analyze_dataset for a one-off "
                                         "stateless analysis.",
                **{k: v for k, v in resp.items()
                   if k in ("open_sessions", "free_gb", "need_gb", "file_gb")},
            )

        out = dict(resp)
        out["success"] = True
        out.pop("ok", None)
        # Keep the payload compact; the same compaction the stateless path uses, so the
        # model sees a consistent shape either way.
        for key in ("profile", "summary", "groupby", "corr", "outliers", "auto"):
            if key in out:
                out[key] = _compact(out[key])
        if operation == "analyze" and out.get("engine") != "cudf":
            out["warning"] = (
                f"this session ran on the CPU (engine={out.get('engine')}); do not claim GPU "
                f"acceleration in the answer."
            )
        return json.dumps(out, ensure_ascii=False, default=str)
    except Exception as exc:  # never let a session failure break the agent loop
        return _err(f"{type(exc).__name__}: {exc}",
                    hint="you can fall back to analyze_dataset for a one-off stateless analysis.")


# --------------------------------------------------------------------------------------
# Skill implementations (what actually runs)
# --------------------------------------------------------------------------------------

def analyze_dataset(file_path: str, operation: str, by: str = None, agg: str = None,
                    columns: str = None, top_k: int = None,
                    force_cpu: bool = False) -> str:
    """
    Run the GPU analytics engine and return a compact JSON result.

    Never raises: every failure comes back as {"success": false, "error": ...} so the
    agent can explain the problem or retry with corrected arguments instead of crashing.
    """
    try:
        if not file_path or not str(file_path).strip():
            return _err("file_path is required")
        path = _resolve_data_path(str(file_path))
        if not os.path.exists(path):
            return _err(
                f"file does not exist: {path}",
                hint=("if this is a relative path or a bare filename, it was resolved against the "
                      "agent process working directory. Call list_datasets to get the absolute "
                      "path of the file, then retry once with that absolute path; do not give up, "
                      "and do not guess a path."),
            )
        if os.path.isdir(path):
            return _err(f"this is a directory, not a file: {path}")

        engine = _find_engine()
        cmd = [_python_bin(), engine, "--input", path, "--op", str(operation or "auto")]
        if by:
            cmd += ["--by", str(by)]
        if agg:
            cmd += ["--agg", str(agg)]
        if columns:
            cmd += ["--columns", str(columns)]
        if top_k:
            cmd += ["--top-k", str(int(top_k))]
        if force_cpu:
            cmd += ["--force-cpu"]

        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)

        # Diagnostics go to stderr; the note about a CPU fallback is worth keeping.
        note = ""
        if proc.stderr:
            note = " | ".join(
                ln.strip() for ln in proc.stderr.strip().splitlines()
                if ln.strip() and not ln.strip().startswith("Traceback")
            )[:400]

        try:
            payload = json.loads(proc.stdout)
        except json.JSONDecodeError:
            return _err(
                f"the analysis engine returned unparseable output (exit={proc.returncode})",
                detail=(proc.stderr or proc.stdout or "")[-600:],
            )

        if not payload.get("ok"):
            return _err(payload.get("error") or "analysis failed", detail=note,
                        exit_code=proc.returncode)

        # Strip the echo of the input path (the model already knows it) and compact the rest.
        result = {k: v for k, v in payload.items() if k != "input"}
        # Remember the target so a follow-up "now chart that" needs no path from the user.
        _remember_last_file(path)
        if isinstance(result.get("corr"), dict) and "matrix" in result["corr"]:
            result["corr"]["matrix"] = _trim_matrix(result["corr"]["matrix"])
        if "note" in result and not result["note"]:
            result.pop("note")

        # `rows_scanned` lives inside the operation's own block (e.g. under "groupby"),
        # so look there too rather than reporting an unknown row count.
        rows_scanned = payload.get("rows_scanned")
        if rows_scanned is None:
            for key in ("groupby", "corr", "outliers", "profile", "summary"):
                block = payload.get(key)
                if isinstance(block, dict) and block.get("rows_scanned") is not None:
                    rows_scanned = block["rows_scanned"]
                    break

        out = {
            "success": True,
            "file": os.path.basename(path),
            "engine": payload.get("engine"),
            "accelerated": payload.get("accelerated"),
            "rows_scanned": rows_scanned,
            "seconds": payload.get("total_seconds"),
            "result": _compact(result),
        }
        # Tell the model plainly when it did NOT get GPU speed, so it cannot overclaim. The two cases
        # must be worded differently, because they mean different things: a deliberate routing choice
        # is normal behaviour with a measured justification, while a fallback means the GPU was tried
        # and failed. Calling the first one "reason: unknown" would have the model report a healthy
        # run as a defect -- which is what happened before this distinction existed.
        if payload.get("engine") != "cudf":
            route = payload.get("routing_reason")
            if route:
                out["engine_note"] = (
                    f"this run used the CPU deliberately (engine={payload.get('engine')}): {route}. "
                    f"This is the faster path at this data size, not a failure. Explain it that way "
                    f"if the user asks about GPU usage; do not claim GPU acceleration."
                )
            else:
                out["warning"] = (
                    f"this run used the CPU (engine={payload.get('engine')}), reason: "
                    f"{payload.get('fallback_reason') or 'unknown'}. Do not claim GPU acceleration "
                    f"in the answer."
                )
        if note:
            # Do not overwrite the routing explanation: that one tells the model the CPU path was
            # a deliberate choice, and losing it would put the run back to reading as a defect.
            out.setdefault("engine_note", note)

        # Attach the measured GPU-vs-CPU comparison for this exact query.
        _attach_speedup(out, path, str(operation or "auto"), by, agg, columns, top_k, payload)
        return json.dumps(out, ensure_ascii=False, default=str)

    except subprocess.TimeoutExpired:
        return _err("analysis timed out (over 30 minutes), narrow the data or name fewer columns",
                    hint="limit the analysed columns with columns, or set top_k")
    except FileNotFoundError as exc:
        return _err(f"analysis engine or interpreter not found: {exc}",
                    hint="check the GPU_ANALYTICS_SCRIPT / GPU_ANALYTICS_PYTHON environment "
                         "variables")
    except Exception as exc:  # last-resort guard: the agent loop must never die here
        return _err(f"{type(exc).__name__}: {exc}")


def _data_search_dirs() -> list:
    """Directories worth looking in for a dataset, nearest first.

    Shared by _resolve_data_path and list_datasets so the two cannot disagree. They did
    disagree, and that asymmetry was a real hole: the resolver searched the parent directory
    (where the demo data lives) while the discovery tool searched only the current directory.
    A caller that sensibly asked "what data is here?" got back nothing but a cache file, then
    invented its own path, failed, and finally asked the user to supply one.
    """
    dirs = []
    env_dir = os.environ.get("DEMO_DATA_DIR")
    if env_dir:
        dirs.append(os.path.expanduser(env_dir))
    dirs += [
        os.getcwd(),
        os.path.dirname(_THIS_DIR),
        os.path.expanduser("~"),
        "/data",
    ]
    seen, out = set(), []
    for d in dirs:
        d = os.path.abspath(os.path.expanduser(str(d)))
        if d not in seen and os.path.isdir(d):
            seen.add(d)
            out.append(d)
    return out


def _data_search_roots() -> list:
    """(directory, max_depth) pairs for dataset discovery.

    Depth matters as much as location. Searching the home directory recursively returned 264
    "datasets", almost all of them irrelevant files inside tool installations, which would bury
    the one 3 GB file the caller wanted. The current directory is small and worth a full walk;
    a parent or home directory is not, so those are bounded.
    """
    roots = []
    cwd = os.path.abspath(os.getcwd())
    roots.append((cwd, None))                       # workspace: walk it all
    for d in _data_search_dirs():
        if d != cwd:
            roots.append((d, 1))                    # parents and home: top level plus one
    return roots


def list_datasets(directory: str = None) -> str:
    """List analysable data files with sizes, so the agent can pick a target.

    With no argument, searches every directory _data_search_dirs() returns rather than only
    the current one, so discovery finds the dataset the resolver would also find. Pass an
    explicit directory to look somewhere else. Deliberately not a machine-specific absolute
    path, so the skill works on any checkout.
    """
    try:
        exts = {".csv", ".tsv", ".parquet", ".pq", ".jsonl", ".json", ".xlsx", ".xls"}
        explicit = str(directory).strip() if directory else ""
        if explicit:
            root = os.path.abspath(os.path.expanduser(explicit))
            if not os.path.isdir(root):
                return _err(f"directory does not exist: {root}")
            roots = [(root, None)]
        else:
            roots = _data_search_roots()
            if not roots:
                return _err("no directory to search; pass directory explicitly")

        found = []
        seen_paths = set()
        for root_dir, max_depth in roots:
            base_depth = root_dir.rstrip(os.sep).count(os.sep)
            for root, dirs, files in os.walk(root_dir):
                if max_depth is not None:
                    depth = root.rstrip(os.sep).count(os.sep) - base_depth
                    if depth >= max_depth:
                        dirs[:] = []       # stop descending; still list this level
                dirs[:] = [d for d in dirs
                           if d not in {"miniforge3", "node_modules", "__pycache__", ".git",
                                        "venv", ".cache"} and not d.startswith(".")]
                for name in files:
                    if os.path.splitext(name)[1].lower() not in exts:
                        continue
                    full = os.path.join(root, name)
                    if full in seen_paths:
                        continue
                    try:
                        size = os.path.getsize(full)
                    except OSError:
                        continue
                    if size < 1024:
                        continue
                    seen_paths.add(full)
                    found.append({"path": full, "size_mb": round(size / 1e6, 1)})
        found.sort(key=lambda r: -r["size_mb"])
        return json.dumps({
            "success": True,
            "count": len(found),
            "searched_dirs": [r[0] for r in roots],
            "files": found[:40],
            "note": ("largest first; bigger data shows GPU acceleration better" if found
                     else f"no analysable data files found under "
                          f"{', '.join(r[0] for r in roots)}"),
        }, ensure_ascii=False)
    except Exception as exc:
        return _err(f"{type(exc).__name__}: {exc}")


# --------------------------------------------------------------------------------------
# Deliverables: charts + report
# --------------------------------------------------------------------------------------

def _deliverables_script() -> str:
    return _find_script("make_deliverables.py")


def _remember_last_file(path: str) -> None:
    """Remember the most recent dataset so a follow-up 'now chart it' needs no path."""
    try:
        with open(_LAST_FILE_PATH, "w", encoding="utf-8") as fh:
            fh.write(os.path.abspath(path))
    except OSError:
        pass


def _last_file() -> str | None:
    try:
        with open(_LAST_FILE_PATH, encoding="utf-8") as fh:
            p = fh.read().strip()
        return p if p and os.path.isfile(p) else None
    except OSError:
        return None


def export_deliverables(file_path: str = None, operation: str = "auto", by: str = None,
                        agg: str = None, columns: str = None, top_k: int = None,
                        out_dir: str = None, lang: str = None,
                        lang_context: str = None) -> str:
    """
    Analyse on the GPU, then write charts, CSV exports and a Markdown report.

    Reuses the exact same engine invocation as `analyze_dataset`, so the numbers in the
    report are the same numbers the agent would otherwise have quoted in chat -- there is no
    second, subtly different code path that could disagree with the answer.

    The report is written in the caller's language. Pass `lang_context` with the user's own
    wording and the report follows the language they wrote in; pass `lang` as "zh" or "en" to
    force one. With neither, the report is Chinese, which is this skill's primary audience.

    Never raises: every failure comes back as a structured error the model can act on.
    """
    try:
        path = _resolve_data_path(file_path) if file_path else (_last_file() or "")
        if not path:
            return _err("no file was given and there is no previous analysis file to reuse.",
                        hint="call list_datasets to find a data file, then pass its path to this "
                             "tool.")
        if not os.path.exists(path):
            return _err(f"file does not exist: {path}",
                        hint="get the absolute path with list_datasets, then retry.")
        if os.path.isdir(path):
            return _err(f"this is a directory, not a file: {path}")

        engine = _find_engine()
        cmd = [_python_bin(), engine, "--input", path, "--op", str(operation or "auto")]
        if by:
            cmd += ["--by", str(by)]
        if agg:
            cmd += ["--agg", str(agg)]
        if columns:
            cmd += ["--columns", str(columns)]
        if top_k:
            cmd += ["--top-k", str(top_k)]

        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "").strip().splitlines()
            return _err(f"analysis failed (exit {proc.returncode}): "
                        f"{detail[-1] if detail else 'unknown error'}",
                        hint="use analyze_dataset first to confirm the arguments (that the column "
                             "names exist, for example), then export again.",
                        exit_code=proc.returncode)
        try:
            payload = json.loads(proc.stdout)
        except json.JSONDecodeError:
            return _err("the analysis script did not return valid JSON.",
                        detail=proc.stdout[:400])

        if payload.get("ok") is False:
            return _err(payload.get("error") or "analysis did not succeed",
                        hint="check the file path and the column names.")

        target = os.path.expanduser(str(out_dir)) if out_dir else os.path.join(
            os.path.dirname(os.path.abspath(path)), "deliverables")
        script = _deliverables_script()

        # One subprocess rather than importing: keeps this module dependency-free of the
        # skill's internals and means a failure here cannot corrupt the agent process.
        proc2 = subprocess.run(
            [_python_bin(), script, "--input", "/dev/stdin", "--out-dir", target,
             "--source-file", path]
            + (["--lang", str(lang)] if lang else [])
            + (["--lang-context", str(lang_context or _request_text())]
               if (lang_context or _request_text()) else []),
            input=proc.stdout, capture_output=True, text=True, timeout=600)
        if proc2.returncode != 0:
            detail = (proc2.stderr or "").strip().splitlines()
            return _err(f"could not generate the deliverables: "
                        f"{detail[-1] if detail else 'unknown error'}",
                        hint="the data itself is still usable, so you can give the conclusion in "
                             "chat first.")

        manifest = json.loads(proc2.stdout)
        _remember_last_file(path)
        out = {
            "success": True,
            "report": manifest["report"],
            "charts": manifest["charts"],
            "data_files": manifest["data_files"],
            "chart_count": manifest["chart_count"],
            "engine": (payload.get("engine") or "unknown"),
            "rows_scanned": manifest.get("rows_scanned"),
            "total_seconds": payload.get("total_seconds"),
            "note": manifest["note"],
            "how_to_answer": (
                "Give the user these file paths verbatim (report.md is the main deliverable), "
                "then summarise the key numbers from the report in your answer. Do not say the "
                "files were saved without giving the paths."
            ),
        }
        return json.dumps(_compact(out), ensure_ascii=False)

    except subprocess.TimeoutExpired:
        return _err("generating the deliverables timed out.",
                    hint="on very large data, limit the columns with columns first, or use "
                         "analyze_dataset instead.")
    except FileNotFoundError as exc:
        return _err(str(exc))
    except Exception as exc:
        return _err(f"{type(exc).__name__}: {exc}")


# --------------------------------------------------------------------------------------
# Legacy skill (unchanged behaviour, kept so the earlier agent still works)
# --------------------------------------------------------------------------------------

def load_csv_dataset(file_path: str) -> str:
    try:
        df = pd.read_csv(file_path)
        info = {
            "rows": len(df),
            "columns": list(df.columns),
            "missing_values": df.isnull().sum().to_dict(),
            "dtypes": df.dtypes.astype(str).to_dict()
        }
        return json.dumps({"success": True, "data": info}, ensure_ascii=False)
    except FileNotFoundError:
        return json.dumps({"success": False, "error": "file does not exist, check the path"},
                          ensure_ascii=False)
    except Exception as e:
        return json.dumps({"success": False, "error": str(e)}, ensure_ascii=False)


def _err(message: str, hint: str = None, detail: str = None, exit_code: int = None,
         **extra) -> str:
    payload = {"success": False, "error": message}
    if hint:
        payload["hint"] = hint
    if detail:
        payload["detail"] = detail
    if exit_code is not None:
        payload["exit_code"] = exit_code
    # Allow callers to attach context (free memory, open session ids, ...) without widening
    # the signature for every new field.
    payload.update(extra)
    return json.dumps(payload, ensure_ascii=False)


# --------------------------------------------------------------------------------------
# Tool map
# --------------------------------------------------------------------------------------

skill_func_map = {
    "analyze_dataset": analyze_dataset,
    "dataset_session": dataset_session,
    "export_deliverables": export_deliverables,
    "list_datasets": list_datasets,
    "load_csv_dataset": load_csv_dataset,
}


def load_skill_definitions(names=None):
    """Return the schemas whose implementations actually exist, so the model is never
    offered a tool that would fail with 'unknown skill'."""
    return [d for d in skill_definitions
            if d["function"]["name"] in skill_func_map
            and (names is None or d["function"]["name"] in names)]