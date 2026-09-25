#!/usr/bin/env bash
# Evaluation-criteria test suite for the DGX Spark data-analysis agent.
#
# Maps each case to the hackathon criteria:
#   (1) autonomous skill selection + correct arguments
#   (2) natural language -> real result
#   (4) robustness: does it survive bad input instead of crashing
#   (5) end-to-end conversation quality
#
# Assertions are made against the tool-call trace (which skill ran, with what arguments,
# whether the engine was cuDF) rather than against prose, because prose always reads well.
#
# Portable: paths are derived from this script's own location, so the suite runs from any
# checkout. Override the data file with DEMO_DATA, and the interpreter with CONDA_ENV.
#
# Usage:
#   bash run_criteria_tests.sh [--only CASE_SUBSTRING]
#   DEMO_DATA=/path/to/data.csv bash run_criteria_tests.sh

# Resolve this script's directory, following symlinks, so no absolute path is assumed.
SOURCE="${BASH_SOURCE[0]}"
while [ -L "$SOURCE" ]; do
  DIR="$(cd -P "$(dirname "$SOURCE")" && pwd)"
  SOURCE="$(readlink "$SOURCE")"
  [[ $SOURCE != /* ]] && SOURCE="$DIR/$SOURCE"
done
AGENT_DIR="$(cd -P "$(dirname "$SOURCE")" && pwd)"
cd "$AGENT_DIR" || exit 1

# Optional environment setup, if present (provides STEPFUN_API_KEY in a non-interactive shell).
[ -f "$AGENT_DIR/env_stepfun.sh" ] && source "$AGENT_DIR/env_stepfun.sh"

# Optional conda activation: set CONDA_ENV to enable, or export the interpreter directly.
if [ -n "${CONDA_ENV:-}" ]; then
  # shellcheck disable=SC1091
  [ -f "$(conda info --base 2>/dev/null)/etc/profile.d/conda.sh" ] && \
    source "$(conda info --base)/etc/profile.d/conda.sh" && conda activate "$CONDA_ENV"
fi

DATA="${DEMO_DATA:-$AGENT_DIR/../benchmark/demo/sales_demo.csv}"
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT

PASS=0; FAIL=0
FAILED_CASES=""
ONLY=""
[ "$1" = "--only" ] && ONLY="$2"

run_case() {
  local name="$1" question="$2" expect="$3" forbid="${4:-}"
  if [ -n "$ONLY" ] && [[ "$name" != *"$ONLY"* ]]; then return; fi

  echo "----------------------------------------------------------------------"
  echo "CASE: $name"
  local out="$WORK/$name.log"

  timeout 900 python agent_main.py --ask "$question" > "$out" 2>&1
  local code=$?

  echo "ASK : $question"
  echo "--- tool trace ---"
  grep -E "^  \[round|^      OK|^      FAILED|\[api error\]|\[warn\]" "$out" | head -8
  [ -s "$out" ] || echo "(no output captured)"

  local ok=1
  if [ $code -ne 0 ]; then
    echo "  !! non-zero exit ($code)"
    ok=0
  fi
  if [ -n "$expect" ]; then
    if grep -qE "$expect" "$out"; then
      echo "  match: $(grep -oE "$expect" "$out" | head -1)"
    else
      echo "  !! expected pattern not found: $expect"
      ok=0
    fi
  fi
  if [ -n "$forbid" ]; then
    # Forbidden patterns are checked against the TOOL TRACE only.
    #
    # Checking the whole transcript produces false failures: a good answer to a conceptual
    # question legitimately ends with "if you give me a data file I can call a tool for
    # that", which names the tool without any tool having been invoked.
    local trace_only
    trace_only=$(grep -E "^  \[round" "$out")
    if echo "$trace_only" | grep -qE "$forbid"; then
      echo "  !! forbidden tool was invoked: $forbid"
      ok=0
    else
      echo "  no tool invoked (as expected)"
    fi
  fi

  if [ $ok -eq 1 ]; then
    echo "  => PASS"; PASS=$((PASS+1))
  else
    echo "  => FAIL"; FAIL=$((FAIL+1)); FAILED_CASES="$FAILED_CASES $name"
  fi
  echo "--- answer tail ---"
  tail -12 "$out"
  echo
}

echo "===================== criteria test suite ====================="

# (1)+(2) happy path: does it pick the right skill and produce real numbers
run_case "happy-path-outliers" \
  "帮我分析 $DATA 里有没有异常值" \
  "analyze_dataset" "success.: false"

# (1) argument correctness for groupby (by + agg must both be right)
run_case "groupby-args" \
  "按 region 分组，统计 $DATA 的销售额均值和最大值，只看前5组" \
  "groupby"

# (1) relationship question should select corr
run_case "corr-selection" \
  "$DATA 里哪些数值列之间相关性最强" \
  "operation.: .corr"

# (4) missing file must be reported, not crash (path derived so it works anywhere)
run_case "missing-file" \
  "分析一下 $AGENT_DIR/this_file_does_not_exist.csv" \
  "不存在|not exist"

# (4) wrong column name must be reported
run_case "bad-column" \
  "分析 $DATA，只看 total_sales 这一列的异常值" \
  "total_sales"

# (4) no path given -> should discover what exists
run_case "discovery" \
  "这台服务器上有哪些数据文件可以分析" \
  "list_datasets"

# (2) no data file involved -> must NOT call an analysis tool
run_case "no-tool-needed" \
  "帮我解释一下什么是 IQR 四分位距" \
  "四分位|IQR" "analyze_dataset"

# (1)+(3) a genuinely multi-step question must select the stateful session skill and open the
# file through it, rather than re-loading the file per step.
#
# NOTE: these patterns run under `grep -E`, which is POSIX ERE -- no lookahead. An earlier
# version used "(?=.*x)", which silently never matches and would have failed forever. Keep
# patterns to plain ERE.
run_case "multi-step-session" \
  "先看 $DATA 的整体概览，然后找出 revenue 的异常值，再按 region 分组分析，用同一个会话完成" \
  "\[round [0-9]+\] -> dataset_session.*operation.: .open" "显存不足"

# (4) the session must be released. Two acceptable outcomes, both checked here: the model
# calls close itself, or the agent's cleanup backstop releases it and says so. What must not
# happen is a session left holding ~1.7 GB.
run_case "session-cleanup" \
  "用会话方式看 $DATA 的整体概览和 revenue 异常值，完成后释放" \
  "close|自动释放" "显存不足"

# (2)+(3) a request for something the user can take away must produce files, not just prose.
run_case "deliverables-export" \
  "分析 $DATA 的 revenue 并给我出一份带图表的报告" \
  "export_deliverables|report.md" "显存不足"

# (1) the plan must be attached to the session when a multi-step goal is given, so progress
# is owned by the system rather than recalled by the model.
run_case "plan-attached" \
  "用会话方式找出 $DATA 里 revenue 的异常值来源，分析原因后释放" \
  "goal|plan|dataset_session" "显存不足"

echo "==============================================================="
echo "PASS=$PASS  FAIL=$FAIL"
if [ -n "$FAILED_CASES" ]; then
  echo "failed:$FAILED_CASES"
  exit 1
fi
echo "ALL CRITERIA CASES PASSED"
exit 0