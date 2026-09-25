#!/usr/bin/env python3
"""
Tests for the plan layer and its integration with the session worker.

Run:
    python plan_test.py

What this covers, and why each part matters:

1. **Classification is deterministic.** The same goal must always select the same strategy.
   This is the entire reason plans exist: three consecutive runs of one question used to
   produce three different tool paths, one of which abandoned the session. A classifier the
   model invokes is a classifier that can change mid-demo, so classification is a pure
   function of the user's words and is asserted to be stable over repeated calls.

2. **A plan is executable.** Every step carries a concrete operation and the arguments to
   run it with, including real group-by columns taken from the frame. A plan that says
   "investigate by group" without naming the group is a template, not a plan.

3. **Progress is owned by the executor.** Steps are marked done by the code that ran them,
   and off-plan work is recorded as off-plan rather than hidden. This is what lets the
   model ask "what is left" and get a truthful answer instead of recalling the conversation.

4. **Degenerate input drops steps instead of emitting broken ones.** With no known grouping
   column the group-by steps are omitted, so the model never receives a step that would
   immediately fail.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import analysis_plan as AP  # noqa: E402

PASSED = 0
FAILED: list = []


def check(name: str, cond: bool, extra: str = "") -> None:
    global PASSED
    if cond:
        PASSED += 1
        print(f"  [PASS] {name}" + (f"  {extra}" if extra else ""))
    else:
        FAILED.append(name)
        print(f"  [FAIL] {name}" + (f"  {extra}" if extra else ""))


def main() -> int:
    print("=== classification is deterministic and covers the four shapes ===")
    cases = [
        ("find the outliers in revenue and explain why", "drill_down"),
        ("where do these outliers come from", "drill_down"),
        ("why are these outliers so large", "drill_down"),
        ("compare total revenue by region", "compare_groups"),
        ("which category ranks highest", "compare_groups"),
        ("how clean is this dataset, are there missing values?", "data_quality"),
        ("check for nulls and duplicates", "data_quality"),
        ("how strongly do the metrics correlate with one another", "relationships"),
        ("what drives revenue", "relationships"),
        # Guards against a regression where a broad trigger steals this: neither "each" nor
        # "every" belongs to any plan, but "the relationship between each metric" is a
        # relationships question.
        ("what is the relationship between each of these metrics", "relationships"),
        # Chinese goals are the primary input language for this skill -- SKILL.md lists Chinese
        # trigger words, the demo script asks its questions in Chinese, and the criteria suite
        # does too. These cases were lost when the repository was translated to English, and
        # because the tests went with them the loss was invisible: the suite kept passing in
        # English while every Chinese goal silently fell through to the default shape. Keep
        # them here so that cannot happen again.
        ("找出 revenue 的异常值并解释原因", "drill_down"),
        ("这些异常是怎么来的", "drill_down"),
        ("为什么这些离群值这么大", "drill_down"),
        ("按 region 分组对比各地区的销售额", "compare_groups"),
        ("哪个类别的排名最高", "compare_groups"),
        ("这份数据的质量怎么样，有没有缺失", "data_quality"),
        ("检查重复和空值", "data_quality"),
        ("哪些指标之间相关性最强", "relationships"),
        ("各指标之间的关系如何", "relationships"),
    ]
    for goal, want in cases:
        got = AP.choose_kind(goal)
        check(f"classify {goal[:22]!r}", got == want, f"-> {got}")

    print()
    print("=== the same goal always yields the same plan, not a varying one ===")
    seen = {AP.choose_kind("why does revenue have outliers") for _ in range(50)}
    check("stable over 50 calls", len(seen) == 1, str(seen))
    p_again = AP.build_plan("x", "why does revenue have outliers", group_cols=["region"])
    ops_a = [s.op for s in p_again.steps]
    p_again2 = AP.build_plan("x", "why does revenue have outliers", group_cols=["region"])
    check("plan steps identical across builds", ops_a == [s.op for s in p_again2.steps],
          str(ops_a))

    print()
    print("=== a plan is executable, with real column names ===")
    p = AP.build_plan("p1", "why does revenue have outliers",
                      group_cols=["region", "category"])
    check("drill_down yields 5 steps", len(p.steps) == 5, f"n={len(p.steps)}")
    check("starts with profile (need real column names first)", p.steps[0].op == "profile")
    gbs = [s for s in p.steps if s.op == "groupby"]
    check("every groupby step names a real grouping column",
          all(s.args.get("by") in ("region", "category") for s in gbs),
          str([s.args.get("by") for s in gbs]))
    check("two groupby steps use different axes",
          len({s.args.get("by") for s in gbs}) == len(gbs),
          "avoids confirming a finding with the same axis twice")
    check("every step has a reason", all(s.reason.strip() for s in p.steps))
    check("every step has an operation", all(s.op.strip() for s in p.steps))
    d = p.as_dict()
    check("as_dict reports the next action to take",
          d["next_step"] == 0 and "do_next" in d, d["do_next"])
    check("as_dict counts completed steps", d["completed"] == 0)

    print()
    print("=== progress is recorded by the executor ===")
    r = AP.record(p, "profile", {}, 0.05)
    check("on-plan step is matched", r["matched"] is True, f"step={r.get('step')}")
    check("completed advances", r["plan"]["completed"] == 1)
    check("next_step advances", r["plan"]["next_step"] == 1)
    check("do_next now names the second step",
          'op="outliers"' in r["plan"]["do_next"], r["plan"]["do_next"])

    print()
    print("=== off-plan work is recorded, not hidden ===")
    off = AP.record(p, "nonexistent_op", {"foo": 1}, 0.01)
    check("unmatched step is reported as off-plan", off["matched"] is False)
    check("off-plan does not inflate progress", off["plan"]["completed"] == 1,
          str(off["plan"]["completed"]))
    check("the off-plan step is carried in the response", "off_plan" in off)

    print()
    print("=== a plan can complete, and says so ===")
    for s in p.steps:
        if not s.done:
            AP.record(p, s.op, s.args, 0.1)
    check("plan reports complete", p.complete is True)
    check("no next step remains", p.next_step is None)
    check("do_next becomes close", p.as_dict()["do_next"] == 'operation="close"',
          p.as_dict()["do_next"])
    check("completed equals total", p.as_dict()["completed"] == p.as_dict()["total_steps"])

    print()
    print("=== degenerate input drops steps instead of emitting broken ones ===")
    p2 = AP.build_plan("p2", "compare revenue by region", "compare_groups", group_cols=[])
    check("groupby steps dropped with no known group column",
          all(s.op != "groupby" for s in p2.steps), str([s.op for s in p2.steps]))
    check("remaining plan is still useful", len(p2.steps) >= 3, f"n={len(p2.steps)}")
    check("no step carries an empty by argument",
          all(s.args.get("by") for s in p2.steps if s.op == "groupby"))

    print()
    print("=== an unrecognised goal still gets a usable plan ===")
    p3 = AP.build_plan("p3", "just take a quick look at this data")
    check("fallback plan is non-empty", len(p3.steps) >= 3,
          f"kind={p3.kind} n={len(p3.steps)}")
    check("fallback kind is a catalog entry", p3.kind in AP.CATALOG)

    print()
    print("=== every catalog entry is well-formed ===")
    for kind, spec in AP.CATALOG.items():
        check(f"{kind} has triggers", bool(spec.get("triggers")))
        check(f"{kind} has steps", len(spec.get("steps") or []) >= 3)
        check(f"{kind} declares why the order works", bool(spec.get("why")))
        check(f"{kind} steps all have op+reason",
              all(s.get("op") and s.get("reason") for s in spec["steps"]))
        check(f"{kind} only uses operations the engine has",
              all(s["op"] in ("profile", "summary", "groupby", "corr", "outliers")
                  for s in spec["steps"]),
              str([s["op"] for s in spec["steps"]]))

    print()
    print("=" * 60)
    print(f"PASSED={PASSED}  FAILED={len(FAILED)}")
    if FAILED:
        print("failed: " + ", ".join(FAILED))
        return 1
    print("ALL PLAN CHECKS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())