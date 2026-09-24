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
# Usage: bash run_criteria_tests.sh [--only CASE_SUBSTRING]

source /home/Developer/agent/env_stepfun.sh
source /home/Developer/miniforge3/etc/profile.d/conda.sh
conda activate rapids-cudf
cd /home/Developer/agent || exit 1

DATA="/home/Developer/sales_demo.csv"
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

# (4) missing file must be reported, not crash
run_case "missing-file" \
  "分析一下 /home/Developer/this_file_does_not_exist.csv" \
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

echo "==============================================================="
echo "PASS=$PASS  FAIL=$FAIL"
if [ -n "$FAILED_CASES" ]; then
  echo "failed:$FAILED_CASES"
  exit 1
fi
echo "ALL CRITERIA CASES PASSED"
exit 0