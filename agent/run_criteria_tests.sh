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

PASS=0; FAIL=0; MATCHED=0
FAILED_CASES=""
ONLY=""
[ "$1" = "--only" ] && ONLY="$2"

# One pattern, used for both the display and the forbid checks, so the two cannot drift apart.
# It is deliberately not anchored to the start of the line: Tui.trace() strips the leading
# indent from every line containing "->" (agent_main.py), so "^  \[round" matches nothing in
# the rendered log -- and a forbid check run against an empty selection passes no matter what
# the agent did. TRACE_TOTAL below is what notices if that ever happens again.
TRACE_RE='\[round [0-9]+\] ->'
TRACE_TOTAL=0

# The header promises the interpreter can be chosen, so honour it: `python` on PATH is whatever
# the machine happens to have, and on a box with several Pythons that is often one without
# pandas or openai -- which fails every case for a reason that has nothing to do with the agent.
PYTHON="${PYTHON:-python}"

run_case() {
  local name="$1" question="$2" expect="$3" forbid="${4:-}"
  if [ -n "$ONLY" ] && [[ "$name" != *"$ONLY"* ]]; then return; fi
  MATCHED=$((MATCHED + 1))

  echo "----------------------------------------------------------------------"
  echo "CASE: $name"
  local out="$WORK/$name.log"

  timeout 900 "$PYTHON" agent_main.py --ask "$question" > "$out" 2>&1
  local code=$?

  echo "ASK : $question"
  # Extracted once and reused below: what the reader sees in the log and what the forbid check
  # matches must be the same lines, or a green assertion and the trace a human trusts can be
  # different things.
  local trace_only trace_n
  trace_only=$(grep -E "$TRACE_RE" "$out")
  trace_n=$([ -n "$trace_only" ] && printf '%s\n' "$trace_only" | grep -c . || echo 0)
  TRACE_TOTAL=$((TRACE_TOTAL + trace_n))

  echo "--- tool trace (${trace_n} call(s)) ---"
  grep -E "$TRACE_RE|^      OK|^      FAILED|\[api error\]|\[warn\]" "$out" | head -8
  [ -s "$out" ] || echo "(no output captured)"

  local ok=1
  if [ $code -ne 0 ]; then
    echo "  !! non-zero exit ($code)"
    ok=0
  fi
  # Judge the evidence, not the transcript. Every run echoes the question back and prints the
  # capability banner ("[agent] skills available to the model: dataset_session, analyze_dataset,
  # list_datasets, export_deliverables"), so a case whose pattern names a tool can be satisfied by
  # prose the agent emitted about itself rather than by a result. Strip exactly those two things:
  # first the box-drawing and padding the frontend adds, then the verbatim question line and the
  # banner line. Nothing else is removed -- an earlier version that skipped whole sections by
  # marker lost the answer too, because rich opens the answer with a corner glyph, not `Agent`.
  local judge="$WORK/$name.judge"
  sed -E 's/[│╭╮╰╯─]+/ /g; s/[[:space:]]+/ /g; s/^ //; s/ $//' "$out" \
    | grep -v -F -x -- "$question" \
    | grep -v -F -e 'skills available to the model:' > "$judge" || true
  if [ -n "$expect" ]; then
    if grep -qE "$expect" "$judge"; then
      echo "  match: $(grep -oE "$expect" "$judge" | head -1)"
    else
      echo "  !! expected pattern not found: $expect (question and capability banner excluded)"
      ok=0
    fi
  fi
  if [ -n "$forbid" ]; then
    # Forbidden patterns are checked against the TOOL TRACE only.
    #
    # Checking the whole transcript produces false failures: a good answer to a conceptual
    # question legitimately ends with "if you give me a data file I can call a tool for
    # that", which names the tool without any tool having been invoked.
    if [ "$trace_n" -eq 0 ]; then
      # A tool-free answer does satisfy a forbid, and for the one conceptual case that is the
      # entire point of the case. It is counted suite-wide at the bottom, so a matcher that has
      # quietly stopped matching cannot keep this branch company for every case at once.
      echo "  no tool invoked, nothing for the forbid to match"
    elif echo "$trace_only" | grep -qE "$forbid"; then
      echo "  !! forbidden tool was invoked: $forbid"
      ok=0
    else
      echo "  ${trace_n} call(s), none forbidden"
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
  "Analyze $DATA and tell me whether it has any outliers" \
  "analyze_dataset" "success.: false"

# (1) argument correctness for groupby (by + agg must both be right)
run_case "groupby-args" \
  "Group $DATA by region and give me the mean and max revenue, top 5 groups only" \
  "groupby"

# (1) relationship question should select corr
run_case "corr-selection" \
  "Which numeric columns in $DATA correlate most strongly with each other" \
  "operation.: .corr"

# (4) missing file must be reported, not crash (path derived so it works anywhere)
run_case "missing-file" \
  "Analyze $AGENT_DIR/this_file_does_not_exist.csv" \
  "not exist|not found|no such|could not find"

# (4) wrong column name must be reported
run_case "bad-column" \
  "Analyze $DATA, outliers in the total_sales column only" \
  "total_sales"

# (4) no path given -> should discover what exists
run_case "discovery" \
  "Which data files on this server can I analyze" \
  "list_datasets"

# (2) no data file involved -> must NOT call an analysis tool
run_case "no-tool-needed" \
  "Explain what the IQR interquartile range is" \
  "IQR|interquartile" "analyze_dataset"

# (1)+(3) a genuinely multi-step question must select the stateful session skill and open the
# file through it, rather than re-loading the file per step.
#
# NOTE: these patterns run under `grep -E`, which is POSIX ERE -- no lookahead. An earlier
# version used "(?=.*x)", which silently never matches and would have failed forever. Keep
# patterns to plain ERE.
run_case "multi-step-session" \
  "First give me an overall picture of $DATA, then the revenue outliers, then a by-region breakdown, all in one session" \
  "\[round [0-9]+\] -> dataset_session.*operation.: .open" \
  "load refused|device memory|out of memory|insufficient memory"

# (4) the session must be released. Two acceptable outcomes, both checked here: the model
# calls close itself, or the agent's cleanup backstop releases it and says so. What must not
# happen is a session left holding ~1.7 GB.
run_case "session-cleanup" \
  "Look at the overall picture of $DATA and its revenue outliers in a session, then release it" \
  "close|releas" \
  "load refused|device memory|out of memory|insufficient memory"

# (2)+(3) a request for something the user can take away must produce files, not just prose.
run_case "deliverables-export" \
  "Analyze revenue in $DATA and give me a report with charts" \
  "export_deliverables|report.md" \
  "load refused|device memory|out of memory|insufficient memory"

# (1) the plan must be attached to the session when a multi-step goal is given, so progress
# is owned by the system rather than recalled by the model.
run_case "plan-attached" \
  "Find where the revenue outliers in $DATA come from, drill down to the cause in a session, then release it" \
  "goal|plan|dataset_session" \
  "load refused|device memory|out of memory|insufficient memory"

echo "==============================================================="
# The case list is derived from this file rather than hard-coded, so adding a 12th case cannot
# silently turn an "11/11" gate into a pass on 11 of 12. Resolved to an absolute path because the
# script has already cd'd into $AGENT_DIR by now, which makes a relative "$0" unopenable.
SELF="$AGENT_DIR/$(basename "${BASH_SOURCE[0]}")"
CASE_COUNT=$(grep -c '^run_case ' "$SELF")
if [ -n "$ONLY" ] && [ "$MATCHED" -eq 0 ]; then
  # A substring that names nothing used to print "ALL CRITERIA CASES PASSED" and exit 0, which
  # makes a typo indistinguishable from a clean run -- and a *partial* run indistinguishable from
  # a full one, since `--only session` also printed 11-case wording.
  echo "!! --only '$ONLY' matched no case. Available:"
  grep -o '^run_case "[^"]*"' "$SELF" | sed 's/^run_case /     /'
  exit 1
fi
echo "PASS=$PASS  FAIL=$FAIL  cases_run=$MATCHED/$CASE_COUNT  tool calls matched across the suite=$TRACE_TOTAL"

# The guard that was missing. Most of these cases load a real file and must produce tool calls,
# so a whole suite with not one matched trace line is not an agent that never reaches for a
# tool -- it is the matcher failing to see them, which turns every forbid assertion above into
# a tautology. That is exactly how five of these cases passed for as long as they have.
# Skipped under --only, where one conceptual case legitimately reports zero calls.
if [ -z "$ONLY" ] && [ "$TRACE_TOTAL" -eq 0 ]; then
  echo "SUITE BROKEN: /$TRACE_RE/ matched nothing in any case."
  echo "Real runs call tools, so this means the rendered trace format changed and every forbid"
  echo "check reported above was vacuously satisfied. Fix the matcher before trusting any"
  echo "green in this output."
  exit 1
fi

if [ -z "$ONLY" ] && [ "$MATCHED" -ne "${CASE_COUNT:-0}" ]; then
  # Only meaningful for a full run: a filtered run is allowed to cover a subset, and now says so.
  echo "!! $MATCHED of $CASE_COUNT cases ran. Something returned early -- not trusting that green."
  exit 1
fi

if [ -n "$FAILED_CASES" ]; then
  echo "failed:$FAILED_CASES"
  exit 1
fi
echo "ALL CRITERIA CASES PASSED"
exit 0