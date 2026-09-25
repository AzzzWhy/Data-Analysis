#!/usr/bin/env python3
"""
Analysis plans: named multi-step strategies with built-in progress tracking.

Why this exists
---------------
Multi-step analysis used to depend on the model deciding, step by step, what to look at
next. That works, but it is not reproducible: the same question run three times produced
three different tool paths, one of which ran out of rounds and abandoned the session
mid-way. For a demo that is a liability, and for a judge it looks like luck.

A plan is therefore *state*, not advice:

  * The strategy is a fixed, readable catalog -- no model invention per run.
  * Progress is recorded by the executor, so "what is left" is answered by the system and
    not by the model's memory of the conversation.
  * The model can still deviate. `record()` accepts an off-plan step and marks it as such,
    so the plan guides without becoming a cage.

Each plan step is a concrete engine operation with concrete arguments, sequenced so that
each step narrows the question. That is the whole difference between "ran five queries" and
"investigated something".

The catalog is data, so it can be inspected, tested, and extended without touching the
session worker.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class Step:
    """One concrete engine operation, with the reason it is in the plan."""
    op: str
    reason: str
    args: Dict[str, Any] = field(default_factory=dict)
    done: bool = False
    seconds: Optional[float] = None
    summary: Optional[str] = None
    off_plan: bool = False

    def as_dict(self, index: int) -> dict:
        d = {"step": index, "op": self.op, "reason": self.reason,
             "done": self.done, "args": self.args}
        if self.seconds is not None:
            d["seconds"] = round(self.seconds, 3)
        if self.summary:
            d["found"] = self.summary
        if self.off_plan:
            d["off_plan"] = True
        return d


@dataclass
class Plan:
    """An ordered strategy for one analysis goal."""
    plan_id: str
    kind: str
    goal: str
    steps: List[Step] = field(default_factory=list)

    @property
    def next_step(self) -> Optional[int]:
        for i, s in enumerate(self.steps):
            if not s.done:
                return i
        return None

    @property
    def complete(self) -> bool:
        return self.next_step is None

    def as_dict(self, verbose: bool = True) -> dict:
        nxt = self.next_step
        out = {
            "plan_id": self.plan_id,
            "kind": self.kind,
            "goal": self.goal,
            "total_steps": len(self.steps),
            "completed": sum(1 for s in self.steps if s.done and not s.off_plan),
            "complete": self.complete,
            "next_step": nxt,
        }
        if verbose:
            out["steps"] = [st.as_dict(i) for i, st in enumerate(self.steps)]
        if nxt is not None:
            nxt_step = self.steps[nxt]
            out["do_next"] = (f"operation=\"analyze\", op=\"{nxt_step.op}\""
                              + (f", {_arg_str(nxt_step.args)}" if nxt_step.args else "")
                              + f"  # {nxt_step.reason}")
        else:
            out["do_next"] = 'operation="close"'
        return out


def _arg_str(args: Dict[str, Any]) -> str:
    return ", ".join(f'{k}="{v}"' for k, v in args.items())


# --------------------------------------------------------------------------------------
# The catalog
# --------------------------------------------------------------------------------------
#
# Order matters: each entry is a concrete engine op with concrete arguments.
# `operation` values are the engine's own: profile/summary/groupby/corr/outliers/auto.

CATALOG: Dict[str, Dict[str, Any]] = {
    # "why are these values odd, and where do they come from" -- the drill-down shape.
    "drill_down": {
        "triggers": ["异常", "离群", "outlier", "anomal", "为什么", "原因", "来源",
                     "怎么来", "定位", "下钻", "drill"],
        "why": "先确认范围，再量化异常，再逐维缩小到具体群体，最后看它是否只是指标间的关联",
        "steps": [
            {"op": "profile", "reason": "先确认列名与类型，后面每一步都要用真实列名"},
            {"op": "outliers", "reason": "量化异常规模，后面才知道要解释多大的量",
             "args": {"top_k": 20}},
            {"op": "groupby", "reason": "按第一个维度看异常是否集中在某个群体",
             "args": {"agg": "auto"}},
            {"op": "groupby", "reason": "换第二个维度交叉验证，避免把相关当成原因",
             "args": {"agg": "auto"}},
            {"op": "corr", "reason": "检查这个维度是否只是与其他指标相关，而非独立原因"},
        ],
    },
    # "compare groups on a metric".
    #
    # Note the deliberate absence of a bare "各": as a single character it matches
    # "各指标之间的关系" (a relationships question) and would steal it from that plan. The
    # specific forms below cover the grouping intent without that collision.
    "compare_groups": {
        "triggers": ["对比", "比较", "哪个", "排名", "分组", "按 ", "按每", "各地区", "各类",
                     "compare", "rank", "group", "by ", "versus", " vs "],
        "why": "先看总量差异，再看均值差异，然后检查分布是否可解释，最后看维度间是否独立",
        "steps": [
            {"op": "profile", "reason": "确认分组列与度量列的真实名字"},
            {"op": "groupby", "reason": "先比较各组的规模与平均水平",
             "args": {"agg": "auto"}},
            {"op": "summary", "reason": "看整体分布与离散度，判断组间差异是否有意义"},
            {"op": "corr", "reason": "确认分组维度与度量之间是否存在混淆关系"},
        ],
    },
    # "is this dataset sound"
    "data_quality": {
        "triggers": ["质量", "缺失", "空值", "重复", "脏", "清洗", "quality", "missing",
                     "null", "dup", "check"],
        "why": "先看结构，再看缺失，再看异常，最后看分布形状",
        "steps": [
            {"op": "profile", "reason": "行数、列类型、缺失情况与内存占用"},
            {"op": "summary", "reason": "每个数值列的分布与离散度"},
            {"op": "outliers", "reason": "哪些列的取值超出常规范围"},
        ],
    },
    # "how do these columns relate"
    "relationships": {
        "triggers": ["相关", "关系", "关联", "影响", "因素", "驱动", "correlat", "relation",
                     "influence", "driver", "drives", "driving", "depends", "associat"],
        "why": "先拿到相关矩阵，再回到分布确认不是被极值带偏，最后看组间是否一致",
        "steps": [
            {"op": "profile", "reason": "确定哪些列是数值列，相关分析只对数值列有意义"},
            {"op": "corr", "reason": "直接回答哪两个指标关联最强"},
            {"op": "summary", "reason": "确认强相关不是少数极值造成的"},
            {"op": "groupby", "reason": "检查这个关联在不同群体里是否一致",
             "args": {"agg": "auto"}},
        ],
    },
}


def _score(kind: str, goal: str) -> int:
    g = (goal or "").lower()
    hits = sum(1 for t in CATALOG[kind]["triggers"] if t.lower() in g)
    return hits


def choose_kind(goal: str) -> str:
    """
    Pick the plan kind from the user's own words.

    Deterministic first-match on a scored catalog, rather than asking the model to classify:
    a classification the model can get wrong is a classification that changes the demo. Ties
    resolve by catalog order, so behaviour is stable across runs.
    """
    scored = [(k, _score(k, goal)) for k in CATALOG]
    best = max(scored, key=lambda kv: kv[1])
    if best[1] == 0:
        # Nothing matched: the analysis shape is a good default because it profiles,
        # summarises and flags outliers in one pass.
        return "drill_down"
    return best[0]


def build_plan(plan_id: str, goal: str, kind: Optional[str] = None,
               columns: Optional[str] = None,
               group_cols: Optional[List[str]] = None) -> Plan:
    """Materialise a plan, filling in dataset-specific arguments where known."""
    kind = kind or choose_kind(goal)
    spec = CATALOG.get(kind) or CATALOG["drill_down"]
    group_cols = [c for c in (group_cols or []) if c]

    steps: List[Step] = []
    gi = 0
    for raw in spec["steps"]:
        args = dict(raw.get("args") or {})

        # Fill placeholders with real columns discovered at open time, so the plan the model
        # receives is already executable rather than a template it has to resolve.
        if raw["op"] == "groupby":
            if args.get("by") is None and group_cols:
                args["by"] = group_cols[gi % len(group_cols)]
                gi += 1
            elif args.get("by") is None:
                args.pop("by", None)
            if args.get("agg") == "auto":
                args.pop("agg", None)  # let the engine pick metrics for the group column
        if raw["op"] in ("outliers", "summary") and columns:
            args.setdefault("columns", columns)
        if raw["op"] == "groupby" and "by" not in args and not group_cols:
            # Without a known grouping column this step cannot be built; drop it rather than
            # emit a step that would fail.
            continue
        steps.append(Step(op=raw["op"], reason=raw["reason"], args=args))

    return Plan(plan_id=plan_id, kind=kind, goal=goal, steps=steps)


def record(plan: Plan, op: str, args: Dict[str, Any], seconds: Optional[float],
           summary: Optional[str] = None) -> dict:
    """
    Mark the matching step done, or record an off-plan step.

    Matching is on the operation name and, when the plan step names concrete arguments, on
    those arguments too. An off-plan step is recorded rather than rejected: the model is
    allowed to investigate something the plan did not anticipate, and hiding that would make
    the progress report lie.
    """
    for s in plan.steps:
        if s.done or s.op != op:
            continue
        if any(str(args.get(k)) != str(v) for k, v in s.args.items() if k in args):
            continue
        if any(k in s.args for k in args if k == "by") and s.args.get("by") != args.get("by"):
            continue
        s.done = True
        s.seconds = seconds
        s.summary = summary
        return {"matched": True, "step": plan.steps.index(s),
                "plan": plan.as_dict(verbose=False)}
    return {"matched": False, "off_plan": {"op": op, "args": args},
            "plan": plan.as_dict(verbose=False)}