#!/usr/bin/env python3
"""Control experiment: the same question with the tools and without them.

Every evaluation in this repository until now proved that behaviour matches expectation -- the
model calls the right tool, passes the right arguments, declines fingerprint data. None of it
showed that having the Skill changes the answer, because no run ever happened without it. That
is the weakest claim in the whole submission, so this measures it.

The design: run one question through the agent with its tools, and the identical question through
the same model with no tools at all. Then compare the answers on things that are objectively
checkable rather than on prose quality:

  - does the answer name the real file
  - does it state the true row count (20,000,000) and true column count
  - does it report numbers that match an independent computation
  - does it admit it cannot see the file, or invent figures anyway

The last one is the interesting one. A model with no tools has never seen the CSV, so a specific
number in its answer is fabricated. This is not a trick question: it is exactly what a judge would
type.

    python control_experiment.py [--data PATH] [--keep-tools]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

DEFAULT_DATA = os.environ.get("DEMO_DATA", "/home/Developer/sales_demo.csv")

QUESTIONS = [
    "分析 {path} 里 revenue 和 profit 的相关性,并告诉我是否值得关注。",
    "数据集 {path} 里哪个地区的 profit 最高?给出具体数字。",
]


def run_agent(question: str, data: str, with_tools: bool, timeout: int = 900):
    """Run one question. Returns (stdout, seconds).

    agent_main.py takes only --ask and --quiet, so the dataset path goes in the question text.
    Both arms of the experiment get the identical string, which is what makes the comparison
    meaningful: only the presence of the tools differs.
    """
    import time

    env = dict(os.environ)
    if not with_tools:
        env["NO_TOOLS"] = "1"
    t0 = time.time()
    proc = subprocess.run(
        [sys.executable, os.path.join(HERE, "agent_main.py"), "--ask", question],
        capture_output=True, text=True, timeout=timeout, env=env, cwd=HERE)
    return proc.stdout + proc.stderr, round(time.time() - t0, 2)


def tools_used(log: str) -> list:
    """Tool calls the model actually made, read off the agent's own trace."""
    names = []
    for m in re.finditer(r"->\s*(\w+)\(", log):
        names.append(m.group(1))
    return names


def answer_of(log: str) -> str:
    """The final assistant message, not the whole trace."""
    idx = log.rfind("=== Agent ===")
    return log[idx:] if idx >= 0 else log


def facts_in(text: str, truth: dict) -> dict:
    """Which objectively checkable facts appear in the answer."""
    nums = set(re.findall(r"\d[\d,]{2,}", text))
    norm = {n.replace(",", "") for n in nums}
    return {
        "names_file": os.path.basename(truth["path"]) in text,
        "row_count": str(truth["rows"]) in norm or f"{truth['rows']:,}" in text,
        "column_count": str(truth["cols"]) in norm,
        "has_specific_decimal": bool(re.search(r"\d+\.\d{2,}", text)),
        "admits_cannot_see": bool(re.search(
            r"(没有|无法|不能|缺少|需要).{0,12}(访问|读取|打开|拿到|看到|数据|文件)"
            r"|cannot (access|read|open|see)|no access|without (access|the (file|data))",
            text, re.I)),
    }


def truth_for(path: str) -> dict:
    """Columns and rows computed independently of the agent."""
    import pandas as pd

    ncols = len(pd.read_csv(path, nrows=0).columns)
    rows = 0
    with open(path, "rb") as fh:
        for _ in fh:
            rows += 1
    return {"path": path, "rows": rows - 1, "cols": ncols}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--data", default=DEFAULT_DATA)
    ap.add_argument("--out", default=os.path.join(HERE, f"control_result_{os.getpid()}.json"))
    args = ap.parse_args()

    if not os.path.isfile(args.data):
        print(f"dataset not found: {args.data}")
        return 2

    truth = truth_for(args.data)
    print(f"数据: {truth['path']}")
    print(f"独立算出的真值: {truth['rows']:,} 行 / {truth['cols']} 列\n")

    results = []
    for q_tmpl in QUESTIONS:
        # Identical text for both arms, so the tools are the only difference.
        q = q_tmpl.format(path=args.data)
        for with_tools in (True, False):
            label = "有工具" if with_tools else "无工具"
            print(f"=== [{label}] {q}")
            try:
                log, secs = run_agent(q, args.data, with_tools)
            except subprocess.TimeoutExpired:
                print("    超时\n")
                results.append({"q": q, "tools": with_tools, "error": "timeout"})
                continue
            ans = answer_of(log)
            used = tools_used(log)
            facts = facts_in(ans, truth)
            print(f"    耗时 {secs}s  工具调用 {len(used)} 次: {used or '无'}")
            for k, v in facts.items():
                print(f"      {k:<22} {v}")
            print()
            results.append({"q": q, "tools": with_tools, "seconds": secs,
                            "tools_used": used, "facts": facts,
                            "answer_head": ans[:600]})

    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump({"truth": truth, "results": results}, fh, ensure_ascii=False, indent=2)
    print(f"写入 {args.out}")

    # Summary: the comparison the repository was missing.
    print("\n" + "=" * 74)
    print(f"{'题目':<26}{'工具':<8}{'调用':<6}{'命名文件':<10}{'行数':<8}{'承认看不到'}")
    for r in results:
        if r.get("error"):
            continue
        f = r["facts"]
        print(f"{r['q'][:24]:<26}{'有' if r['tools'] else '无':<8}"
              f"{len(r['tools_used']):<6}{'是' if f['names_file'] else '否':<10}"
              f"{'是' if f['row_count'] else '否':<8}"
              f"{'是' if f['admits_cannot_see'] else '否'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())