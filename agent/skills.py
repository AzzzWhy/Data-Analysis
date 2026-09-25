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
                "把数据文件一次性载入显存，之后在同一次会话里反复分析，避免每一步都重新读盘。"
                "适合【多步分析】：先找异常再定位来源、按多个维度对比、由概览逐步下钻。"
                "\n\n为什么用它：全量数据常驻内存后，后续每一步从数秒降到几十毫秒"
                "（2000 万行实测：载入约 1.9 秒，之后每步 0.03~0.5 秒）。"
                "因此多步下钻变得便宜，且每一步仍然是全量数据，不采样。"
                "\n\n调用方式（三步）："
                "1) operation='open' + file_path —— 打开文件，返回 session_id；"
                "2) operation='analyze' + session_id + op —— 反复分析，op 与 analyze_dataset 相同"
                "(profile/summary/groupby/corr/outliers/auto)，可带 by/agg/columns/top_k；"
                "3) operation='close' + session_id —— 分析结束必须关闭以释放显存。"
                "\n\n必须使用的场景：用户的需求需要多步完成，例如「找出异常并分析原因」"
                "「对比几个维度的表现」「先概览再深入某一组」。"
                "\n\n不要用于：只有一步的简单问题（直接用 analyze_dataset 更省显存）；"
                "画图；修改数据文件。单次问题用 session 只会白占显存。"
                "\n\n注意：会话会持续占用显存（2000 万行约 1.7GB）。分析完请 close。"
                "如果 open 因显存不足被拒绝，改回 analyze_dataset。"
                "\n\n**规划托管**：open 时把用户的目标原文传进 goal，系统会立刻返回一份"
                "针对这个目标的计划（plan），里面是排好序的具体步骤和「下一步该做什么」。"
                "每一步执行完后，返回里都会带上最新的 plan 进度（已完成几步、还剩几步、"
                "下一步做什么）。你不必自己记住做到哪里——进度由系统记账。"
                "计划里的步骤如果判断不合适，可以跳过，系统会把它标成 off-plan。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "operation": {
                        "type": "string",
                        "enum": ["open", "analyze", "list", "close"],
                        "description": "open=载入文件并返回 session_id；analyze=在会话上执行分析；"
                                       "list=查看当前会话；close=释放",
                    },
                    "file_path": {
                        "type": "string",
                        "description": "operation='open' 时要载入的文件路径",
                    },
                    "session_id": {
                        "type": "string",
                        "description": "operation='analyze'/'close' 时 open 返回的会话编号，例如 s1",
                    },
                    "op": {
                        "type": "string",
                        "enum": ["auto", "profile", "summary", "groupby", "corr", "outliers"],
                        "description": "operation='analyze' 时要执行的分析，含义与 analyze_dataset 一致",
                    },
                    "by": {"type": "string", "description": "op='groupby' 时的分组列"},
                    "agg": {
                        "type": "string",
                        "description": "op='groupby' 时的聚合方式，例如 revenue:sum,mean",
                    },
                    "columns": {
                        "type": "string",
                        "description": "要分析的列，逗号分隔，例如 revenue,cost",
                    },
                    "top_k": {
                        "type": "integer",
                        "description": "groupby/corr/outliers 返回多少条，默认 20",
                    },
                    "goal": {
                        "type": "string",
                        "description": "operation='open' 时用户想达成的目标（原文即可）。"
                                       "传入后系统会返回一份针对该目标的分解计划并在每一步"
                                       "自动记账，强烈建议多步需求都传。",
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
                "对本地表格数据文件做 GPU 加速的真实统计计算，返回精确结果。"
                "覆盖：整体画像(行列数/类型/缺失)、统计摘要(count/mean/std/min/Q1/中位数/Q3/max)、"
                "分组聚合、相关性矩阵、IQR 异常值检测。"
                "\n\n必须在以下场景调用：用户要求分析/统计/汇总/聚合/相关性/找异常值任何本地数据文件"
                "(CSV、Parquet、TSV、JSONL、Excel)；或用户提到数据量很大、百万行/千万行、"
                "说 pandas 太慢、担心跑不动。"
                "\n\n关键约束：不要用你自己读到上下文的样本来估算统计量，必须调用本工具让 GPU 在全量数据上计算。"
                "返回结果里的 rows_scanned 是全量行数。"
                "\n\n不要用于：画图、训练模型、查远程数据库、修改数据文件、单次算术运算。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "file_path": {
                        "type": "string",
                        "description": "服务器上数据文件的绝对或相对路径，例如 /data/sales.csv"
                    },
                    "operation": {
                        "type": "string",
                        "enum": ["auto", "profile", "summary", "groupby", "corr", "outliers"],
                        "description": (
                            "要做的分析。auto=一次返回画像+摘要+异常值(推荐先用它); "
                            "profile=行列数/类型/缺失/预览; summary=每列统计量; "
                            "groupby=按某列分组聚合(需要配合 by 和 agg); "
                            "corr=相关性矩阵; outliers=IQR 异常值检测"
                        )
                    },
                    "by": {
                        "type": "string",
                        "description": "groupby 用：分组依据的列名，例如 region。仅 operation=groupby 时需要"
                    },
                    "agg": {
                        "type": "string",
                        "description": (
                            "groupby 用：聚合表达式，格式 '列名:函数1,函数2|列名2:函数3'，"
                            "例如 'revenue:sum,mean|quantity:max'。"
                            "可用函数：count,size,sum,mean,min,max,std,median,nunique,first,last"
                        )
                    },
                    "columns": {
                        "type": "string",
                        "description": "可选：只分析这些数值列，逗号分隔，例如 revenue,cost。留空则分析全部数值列"
                    },
                    "top_k": {
                        "type": "integer",
                        "description": "可选：groupby 只返回前 K 个分组、corr 只返回关联最强的 K 对。数据量大时建议设置"
                    },
                    "force_cpu": {
                        "type": "boolean",
                        "description": "可选：强制用 CPU(pandas) 计算，用于与 GPU 结果对照。默认 false"
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
                "列出服务器上可分析的数据文件(CSV/Parquet/TSV/JSONL/Excel)及其大小。"
                "当用户没有给出具体文件路径，或你不确定有哪些数据可用时，先调用这个。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "directory": {
                        "type": "string",
                        "description": "要搜索的目录，默认当前工作目录"
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
                "把分析结果导出成可以直接交付的文件:图表(SVG)和一份 Markdown 报告。"
                "当用户说「出个图表」「画一下」「给我一份报告」「导出」「保存下来」"
                "「我要拿去汇报/发邮件」时使用。\n"
                "会先在 GPU 上做一次全量分析，然后写出:report.md(带表格与图表)、"
                "若干 .svg 图、以及 CSV/JSON 数据文件。\n"
                "如果用户要的是「图表」而没指定分析类型，用 operation=auto 即可。\n"
                "注意:返回里会给出文件路径，回答时必须把这些路径告诉用户。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "file_path": {
                        "type": "string",
                        "description": "要分析的数据文件路径；省略则用最近一次分析过的文件"
                    },
                    "operation": {
                        "type": "string",
                        "description": "先做哪种分析再出图，默认 auto",
                        "enum": ["auto", "profile", "summary", "groupby", "corr", "outliers"]
                    },
                    "by": {"type": "string", "description": "groupby 的分组列(只能一列)"},
                    "agg": {"type": "string", "description": "聚合写法 col:func1,func2|col2:func"},
                    "columns": {"type": "string", "description": "限定参与分析的列，逗号分隔"},
                    "top_k": {"type": "integer", "description": "返回前多少行/组"},
                    "out_dir": {"type": "string", "description": "输出目录，默认 deliverables/"}
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
            f"对照已跳过：两个引擎扫描的行数不同（GPU {gpu_rows} 行 / CPU {cpu_rows} 行），"
            "该对比无效。"
        )
        return
    if gpu_rows is not None and gpu_rows < 1000:
        out["gpu_vs_cpu_warning"] = (
            f"对照已跳过：本次只扫描了 {gpu_rows} 行，规模太小，加速比没有意义。"
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
            "同一文件、同一命令，GPU 与 CPU 各实际运行一次所得（CPU 侧取两次中最快的一次）。"
            "CPU 基线是 pandas 默认（C 层基本单线程）。"
        ),
    }
    # Give the model a ready-made, honest sentence, and the caveat it must repeat when the
    # ratio is unimpressive. Small data legitimately shows little or no GPU advantage.
    if ratio < 1.2:
        out["gpu_vs_cpu"]["honest_note"] = (
            f"这次 GPU 只快 {ratio:.2f}x，几乎无差别。原因是数据规模偏小、"
            "读取/解析占了大头。回答时必须如实说明，不要宣称明显加速。"
        )
    else:
        out["gpu_vs_cpu"]["honest_note"] = (
            f"GPU 比 CPU 快 {ratio:.2f}x。可以在回答里说明这次计算用了 GPU 加速；"
            "但要补充：端到端时间里读取 CSV 占比很大，这部分两引擎都在用多核并行，"
            "加上 pandas 基线只用单核，所以该倍数不等于纯 GPU 计算的优势。"
        )


# --------------------------------------------------------------------------------------
# Stateful sessions: hold a dataset in GPU memory across several analyses
# --------------------------------------------------------------------------------------

# A worker is one long-lived process holding the resident frames. It is started on first use
# and reused for the rest of the conversation, because its entire value is the state it keeps.
_worker_lock = threading.Lock()
_worker = None


def _session_script() -> str:
    """Locate gpu_session.py next to the engine."""
    engine = _find_engine()
    cand = os.path.join(os.path.dirname(engine), "gpu_session.py")
    if not os.path.exists(cand):
        raise FileNotFoundError(f"找不到 gpu_session.py（期望在 {cand}）")
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
                        "error": f"无法启动会话进程: {type(exc).__name__}: {exc}"}
        try:
            _worker.stdin.write(json.dumps(payload) + "\n")
            _worker.stdin.flush()
            line = _worker.stdout.readline()
        except (BrokenPipeError, OSError) as exc:
            _worker = None
            return {"ok": False, "error": f"会话进程通信失败: {exc}。请改用 analyze_dataset。"}
        if not line:
            err = ""
            try:
                err = (_worker.stderr.read() or "")[-400:]
            except Exception:
                pass
            _worker = None
            return {"ok": False,
                    "error": f"会话进程已退出。{('stderr: ' + err) if err else ''}"
                             f"请改用 analyze_dataset 做单次分析。"}
        try:
            return json.loads(line)
        except json.JSONDecodeError:
            return {"ok": False, "error": f"会话进程返回了无法解析的输出: {line[:200]}"}


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
    # predictable list: a filesystem-wide search would be slow and unpredictable.
    candidates = []
    env_dir = os.environ.get("DEMO_DATA_DIR")
    if env_dir:
        candidates.append(env_dir)
    candidates += [
        os.getcwd(),
        os.path.dirname(_THIS_DIR),
        os.path.expanduser("~"),
        "/data",
    ]
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
            return _err(f"operation 必须是 open / analyze / list / close，收到: {operation}")

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
            return _err("analyze 需要 session_id（先用 operation='open' 打开文件）")

        resp = _worker_call(req)
        if not resp.get("ok"):
            # Pass the worker's own guidance through: it is written for the model, and
            # dropping it would leave the agent with an unactionable error.
            return _err(
                resp.get("error") or "会话操作失败",
                hint=resp.get("hint") or "可以改用 analyze_dataset 做单次无状态分析。",
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
                f"本会话运行在 CPU 上 (engine={out.get('engine')})，回答时不要声称用了 GPU 加速。"
            )
        return json.dumps(out, ensure_ascii=False, default=str)
    except Exception as exc:  # never let a session failure break the agent loop
        return _err(f"{type(exc).__name__}: {exc}",
                    hint="可以改用 analyze_dataset 做单次无状态分析。")


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
            return _err("必须提供 file_path")
        path = _resolve_data_path(str(file_path))
        if not os.path.exists(path):
            return _err(
                f"文件不存在: {path}",
                hint=("如果这是相对路径或文件名，它相对于 agent 进程的工作目录解析。"
                      "请调用 list_datasets 拿到该文件的绝对路径，然后用绝对路径重试一次；"
                      "不要直接放弃，也不要凭空猜测路径。"),
            )
        if os.path.isdir(path):
            return _err(f"这是一个目录而不是文件: {path}")

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
                f"分析引擎返回了无法解析的输出 (exit={proc.returncode})",
                detail=(proc.stderr or proc.stdout or "")[-600:],
            )

        if not payload.get("ok"):
            return _err(payload.get("error") or "分析失败", detail=note,
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
        # Tell the model plainly when it did NOT get GPU speed, so it cannot overclaim.
        if payload.get("engine") != "cudf":
            out["warning"] = (
                f"本次在 CPU 上运行 (engine={payload.get('engine')})，原因: "
                f"{payload.get('fallback_reason') or '未知'}。回答时不要声称使用了 GPU 加速。"
            )
        if note:
            out["engine_note"] = note

        # Attach the measured GPU-vs-CPU comparison for this exact query.
        _attach_speedup(out, path, str(operation or "auto"), by, agg, columns, top_k, payload)
        return json.dumps(out, ensure_ascii=False, default=str)

    except subprocess.TimeoutExpired:
        return _err("分析超时(超过 30 分钟)，请缩小数据范围或指定更少的列",
                    hint="用 columns 参数限制分析列，或加 top_k")
    except FileNotFoundError as exc:
        return _err(f"找不到分析引擎或解释器: {exc}",
                    hint="检查 GPU_ANALYTICS_SCRIPT / GPU_ANALYTICS_PYTHON 环境变量")
    except Exception as exc:  # last-resort guard: the agent loop must never die here
        return _err(f"{type(exc).__name__}: {exc}")


def list_datasets(directory: str = None) -> str:
    """List analysable data files with sizes, so the agent can pick a target.

    Defaults to $DEMO_DATA_DIR, then the current working directory. Deliberately not a
    machine-specific absolute path, so the skill works on any checkout.
    """
    try:
        default_dir = os.environ.get("DEMO_DATA_DIR") or os.getcwd()
        directory = os.path.expanduser(str(directory or default_dir))
        if not os.path.isdir(directory):
            return _err(f"目录不存在: {directory}")
        exts = {".csv", ".tsv", ".parquet", ".pq", ".jsonl", ".json", ".xlsx", ".xls"}
        found = []
        for root, dirs, files in os.walk(directory):
            dirs[:] = [d for d in dirs
                       if d not in {"miniforge3", "node_modules", "__pycache__", ".git",
                                    "venv", ".cache"} and not d.startswith(".")]
            for name in files:
                if os.path.splitext(name)[1].lower() in exts:
                    full = os.path.join(root, name)
                    try:
                        size = os.path.getsize(full)
                    except OSError:
                        continue
                    if size < 1024:
                        continue
                    found.append({"path": full, "size_mb": round(size / 1e6, 1)})
        found.sort(key=lambda r: -r["size_mb"])
        return json.dumps({
            "success": True,
            "count": len(found),
            "files": found[:40],
            "note": ("按大小倒序；数据量越大越能体现 GPU 加速" if found
                     else f"{directory} 下没有找到可分析的数据文件"),
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
                        out_dir: str = None) -> str:
    """
    Analyse on the GPU, then write charts, CSV exports and a Markdown report.

    Reuses the exact same engine invocation as `analyze_dataset`, so the numbers in the
    report are the same numbers the agent would otherwise have quoted in chat -- there is no
    second, subtly different code path that could disagree with the answer.

    Never raises: every failure comes back as a structured error the model can act on.
    """
    try:
        path = _resolve_data_path(file_path) if file_path else (_last_file() or "")
        if not path:
            return _err("没有指定文件，也没有可复用的上一次分析文件。",
                        hint="先调用 list_datasets 找到数据文件，再把路径传给本工具。")
        if not os.path.exists(path):
            return _err(f"文件不存在: {path}",
                        hint="用 list_datasets 取绝对路径后重试。")
        if os.path.isdir(path):
            return _err(f"这是一个目录而不是文件: {path}")

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
            return _err(f"分析失败(exit {proc.returncode}): {detail[-1] if detail else '未知错误'}",
                        hint="先用 analyze_dataset 确认参数正确(例如列名是否存在)，再重试导出。",
                        exit_code=proc.returncode)
        try:
            payload = json.loads(proc.stdout)
        except json.JSONDecodeError:
            return _err("分析脚本没有返回合法 JSON。", detail=proc.stdout[:400])

        if payload.get("ok") is False:
            return _err(payload.get("error") or "分析未成功",
                        hint="检查文件路径与列名。")

        target = os.path.expanduser(str(out_dir)) if out_dir else os.path.join(
            os.path.dirname(os.path.abspath(path)), "deliverables")
        script = _deliverables_script()

        # One subprocess rather than importing: keeps this module dependency-free of the
        # skill's internals and means a failure here cannot corrupt the agent process.
        proc2 = subprocess.run(
            [_python_bin(), script, "--input", "/dev/stdin", "--out-dir", target,
             "--source-file", path],
            input=proc.stdout, capture_output=True, text=True, timeout=600)
        if proc2.returncode != 0:
            detail = (proc2.stderr or "").strip().splitlines()
            return _err(f"生成交付物失败: {detail[-1] if detail else '未知错误'}",
                        hint="数据本身仍是可用的，可以先在对话里给出结论。")

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
                "把这些文件路径原样告诉用户(report.md 是主交付物)，"
                "再在回答里概括报告里的关键数字。不要说文件已保存却不给路径。"
            ),
        }
        return json.dumps(_compact(out), ensure_ascii=False)

    except subprocess.TimeoutExpired:
        return _err("生成交付物超时。",
                    hint="数据量过大时可以先用 columns 限定列，或改用 analyze_dataset。")
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
        return json.dumps({"success": False, "error": "文件不存在，请检查路径"}, ensure_ascii=False)
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