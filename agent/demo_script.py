#!/usr/bin/env python3
"""
Live demo for the DGX Spark data-analysis agent.

Runs a scripted conversation that walks the five judging criteria, printing what the agent
is doing (which skill it chose, on what arguments, on which engine, and on this call how much
faster the GPU was than the CPU) so an audience can see the autonomy and the speedup rather
than take either on faith.

    python demo_script.py              # full run
    python demo_script.py --prewarm    # run once before the audience arrives
    python demo_script.py --list       # show the steps only
    python demo_script.py --only 3     # one step (useful when rehearsing)
    python demo_script.py --fast       # skip the slowest step

Each step prints its wall-clock time, so a live run can be paced and a rehearsal can find
the slow spots.

--prewarm matters for a live demo: the first time a given query is asked, the agent also runs
it on the CPU to measure the speedup, which adds ~15-25s on a 20M-row file. Prewarming
performs that comparison once up front and fills the cache, so every step of the live demo
shows its speedup immediately.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

from openai import OpenAI

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from agent_main import Agent, MODEL_NAME, build_client  # noqa: E402

_HERE = os.path.dirname(os.path.abspath(__file__))

# Dataset for the demo. Defaults to a "demo" directory next to this checkout; override with
# DEMO_DATA or --data. Kept free of machine-specific absolute paths so the script is portable.
_default_data = os.path.join(os.path.dirname(_HERE), "benchmark", "demo", "sales_demo.csv")
DATA = os.environ.get("DEMO_DATA", _default_data)

# (title, what the audience should notice, question)
STEPS = [
    (
        "① 自主发现数据",
        "没给文件路径，Agent 自己决定调用 list_datasets 去找有什么数据",
        "这台服务器上哪些数据文件可以分析？",
    ),
    (
        "② 自然语言 → 全量统计",
        "只说“帮我看看这份数据”，Agent 自己选 analyze_dataset，并对全量数据算统计量",
        "帮我看看 {data} 这份数据，给我一个整体概览",
    ),
    (
        "③ 分组聚合（参数自主生成）",
        "中文业务口径 → Agent 自己拼出 by 和 agg 参数",
        "按 region 统计 {data} 的 revenue 总和与均值，各取前5",
    ),
    (
        "④ 相关性分析",
        "关系型问题 → 自动选 corr；注意 Agent 会不会乱说因果",
        "{data} 里哪些数值列之间相关性最强？这说明什么？",
    ),
    (
        "⑤ 异常值检测（GPU 主力场景）",
        "IQR 异常值检测，全量数据，报告实际使用的引擎；"
        "问的列与②中 auto 已覆盖的不同，强制触发一次新的工具调用",
        "单独帮我算 {data} 里 revenue 和 cost 两列的 IQR 异常值，要数量和占比",
    ),
    (
        "⑥ 异常路径：文件不存在",
        "健壮性：不崩溃、不乱猜，而是明确报告问题（对应评审标准④）",
        "分析一下 {missing_file}",
    ),
    (
        "⑦ 不该调用工具时就不调用",
        "概念性问题不该触发数据分析工具，验证触发边界",
        "解释一下什么是四分位距 IQR，以及它为什么能用来找异常值",
    ),
]


def banner(text: str, char: str = "=") -> None:
    print(f"\n{char * 74}\n{text}\n{char * 74}", flush=True)


def prewarm(data_path: str) -> None:
    """Fill the GPU-vs-CPU comparison cache for the queries the demo is about to ask.

    The cache is keyed on file identity + operation + arguments, so warming the demo's exact
    queries is enough to make every live step show its speedup without a pause.
    """
    import skills

    warmups = [
        ("auto", {}),
        ("groupby", {"by": "region", "agg": "revenue:sum,mean"}),
        ("corr", {}),
        ("outliers", {"columns": "revenue,cost"}),
        ("summary", {}),
    ]
    banner("预热 GPU/CPU 对照缓存（在观众到场前跑）")
    print("首次运行每个查询时会额外在 CPU 上跑一遍用于对比，整体约需 1-2 分钟。")
    print("预热后，现场每一步都能立刻显示加速比。\n")
    for op, kwargs in warmups:
        started = time.perf_counter()
        # Measure and cache the CPU baseline directly. Going through analyze_dataset would
        # also pay the GPU run, and its arguments would have to match the model's exactly.
        measured = skills.warm_comparison(data_path, op, **kwargs)
        elapsed = time.perf_counter() - started
        if not measured:
            print(f"  {op:<9} {elapsed:6.1f}s   (measurement failed, skipped)")
        else:
            print(f"  {op:<9} {elapsed:6.1f}s   CPU baseline "
                  f"{measured['baseline_seconds']:.2f}s")
    print("\n预热完成。")
    print(f"缓存条目数: {len(skills._COMPARISON_CACHE)}")


def main() -> int:
    ap = argparse.ArgumentParser(description="scripted live demo")
    ap.add_argument("--only", type=int, help="run a single step by number")
    ap.add_argument("--list", action="store_true", help="list steps and exit")
    ap.add_argument("--fast", action="store_true", help="skip the slowest step")
    ap.add_argument("--prewarm", action="store_true",
                    help="fill the GPU/CPU comparison cache, then exit")
    ap.add_argument("--data", default=DATA, help=f"dataset path (default {DATA})")
    args = ap.parse_args()

    if args.list:
        for i, (title, notice, q) in enumerate(STEPS, 1):
            print(f"{i}. {title}\n"
                  f"   question: {q.format(data=args.data, missing_file='<nonexistent>.csv')}\n"
                  f"   watch for: {notice}")
        return 0

    # Shadow the module-level default rather than mutating it; a local is enough here and
    # avoids a `global` declaration after DATA has already been read as a default value.
    data_path = args.data
    if not os.path.exists(data_path):
        print(f"[warn] demo dataset not found: {data_path}")
        print("       generate it with memory_ceiling_test.generate(), or pass --data")

    if args.prewarm:
        prewarm(data_path)
        return 0

    client: OpenAI = build_client()
    agent = Agent(client, verbose=True)

    selected = STEPS if args.only is None else [STEPS[args.only - 1]]
    timings = []

    banner(f"DGX Spark 数据分析 Agent — 现场演示  (model={MODEL_NAME})")
    print(f"数据集: {data_path}")
    print(f"共 {len(selected)} 个环节，每个环节的耗时会在结束时汇总")

    for step_no, (title, notice, question) in enumerate(selected, 1):
        banner(f"{title}\n【看这里】{notice}", "-")
        question = question.format(
            data=data_path,
            missing_file=os.path.join(_HERE, "no_such_file_xyz.csv"),
        )
        print(f"用户: {question}\n")
        started = time.perf_counter()
        try:
            answer = agent.run(question)
        except Exception as exc:
            print(f"[演示中断] {type(exc).__name__}: {exc}")
            return 1
        elapsed = time.perf_counter() - started
        timings.append((title, elapsed))
        print(f"--- Agent 回答 ---\n{answer}\n")
        print(f"[本环节耗时 {elapsed:.1f}s]")

    banner("演示结束 — 各环节耗时")
    total = 0.0
    for title, secs in timings:
        total += secs
        print(f"  {secs:7.1f}s   {title}")
    print(f"  {total:7.1f}s   总计")
    return 0


if __name__ == "__main__":
    sys.exit(main())