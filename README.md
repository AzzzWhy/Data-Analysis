# Data-Analysis Agent Skill

A tool-calling agent that runs exact statistical analysis over large local datasets on an
NVIDIA GB10 (DGX Spark) with RAPIDS cuDF. The model decides what to compute and writes the
answer; it never reads the data.

Submitted to the third NVIDIA DGX Spark Hackathon, Agent Skills track.

## How it works

A language model cannot analyze a 3 GB CSV on its own. The file does not fit in the context
window, so the model samples it and reports estimates rather than statistics, and any
computation it does run is single-threaded on the CPU.

This skill divides the work. The model interprets the question and chooses the operation; cuDF
computes every statistic over the entire file:

```
              user question
                    |
                    v
     +-------------------------------+
     |  LLM: decides only            |   which skill, which arguments
     |  step-3.7-flash               |   never sees the data
     +--------------+----------------+
                    |  function call
                    v
     +-------------------------------+
     |  skill: runs on this machine  |   full-dataset cuDF, no sampling
     |  analyze_dataset              |   measures the CPU baseline too
     +--------------+----------------+
                    |  real statistics (JSON)
                    v
     +-------------------------------+
     |  LLM: writes the answer only  |   conclusions + measured speedup
     +-------------------------------+
```

Because the data never passes through the model, dataset size is not limited by context
length. A 20,000,000-row, 3.04 GB CSV reaches the model as a few dozen lines of statistics.

## A real run

The transcript below is the original run, kept in its original Chinese. The demo script now
drives the same nine stages with English prompts; the measured behaviour is unchanged.

```console
用户: 按 region 统计 sales_demo.csv 的 revenue 总和与均值，各取前5

  [round 1] -> analyze_dataset({"file_path": "sales_demo.csv",
                                "operation": "groupby", "by": "region",
                                "agg": "revenue:sum,mean", "top_k": 5})
      OK  engine=cudf  rows=20,000,000  2.63s  | GPU 2.63s vs CPU 10.22s = 3.88x
--- Agent 回答 ---
## Revenue 总和排名（Top 5）
| 排名 | 地区  | 总收入            |
|------|-------|-------------------|
| 1    | LATAM | 4,300,004,248.14  |
| 2    | AMER  | 4,299,776,626.08  |
| 3    | APAC  | 4,298,033,716.38  |
| 4    | MEA   | 4,294,925,776.77  |
| 5    | EMEA  | 4,286,931,276.58  |

## 运行情况
本次分析在 GPU（NVIDIA GB10）上通过 cuDF 完成，全量 20,000,000 行耗时 2.63 秒；
同一计算在 CPU pandas 上耗时 10.22 秒，GPU 快 3.88 倍。
> 注意：端到端耗时包含 CSV 读取，读取阶段两引擎都在用多核并行；pandas 基线默认
> 单线程，因此该倍数并不完全等同于纯计算阶段的 GPU 优势。
```

The question did not name an operation; the model chose `groupby` itself. The scan covered all
20,000,000 rows rather than a sample, and the speedup was measured on that question and that
file, during the run.

## How this maps to the judging criteria

| Criterion | What is implemented | Where to check it |
| :--- | :--- | :--- |
| Skill invocation: choosing the right tool and arguments unprompted | The model selects from four tools. Given no path it calls `list_datasets` first. A conceptual question invokes nothing. After a bad column name it calls `profile` to get the real names and retries. A multi-step request switches to the session tool and passes its `goal` to claim a plan. | `agent/run_criteria_tests.sh`, 11 cases. The assertions run against the tool-call trace rather than the prose of the answer. |
| Task completion: natural language in, real results out | All six operations return real statistics. An answer carries conclusions, rankings, tables and key findings, and the run also writes report and chart files the user can keep. | `smoke_test.py`, 59 assertions. `verify_*.py` recomputes the numbers independently. |
| Innovation | A resident-memory session makes multi-step drill-down an order of magnitude cheaper; plans are system state rather than prompt text; deliverables are generated with no third-party dependency; the speedup is measured in the same conversation that reports it. | The scripted drill-down: 1.861 s in session against 37.933 s on CPU, 20.4x. |
| Code usability: deployable, robust, survives bad input | The engine degrades to pandas on its own. No tool ever raises. A session is refused before loading when memory is short. A file that changed underneath a session is refused rather than answered from a stale snapshot. Two-column grouping fails with an explicit message. No GPU, a missing file and a bad column name are all handled gracefully. Memory is released in a `finally` block. | The no-GPU path runs on an ordinary machine. `plan_test.py`, 56 assertions, plus `session_*_test.py`, 41. |
| Demo quality: a smooth end-to-end conversation | A scripted nine-stage demo runs in 176.8 s and shows the measured speedup at every stage, with the deliverables stage and the multi-step drill-down as its two peaks. `--prewarm` removes the first-query wait on stage. | `agent/demo_script.py`. |

### Where the innovation actually is

Using cuDF for data analysis is not novel by itself, so the claims are made where they can be
checked.

- The speedup is measured inside the conversation, on the question being asked and the file being
  analyzed, rather than quoted from a table prepared beforehand.
- The attribution experiment is reported even though it weakens the headline. Around 3x of the
  end-to-end gain comes from GPU compute; the rest comes from parallel CSV parsing, and the
  pandas baseline runs on one core of twenty. The agent repeats those limits to the user instead
  of quoting 6.45x on its own.
- Two engines disagreeing on real data exposed a genuine correctness bug, traced to a 4e-14
  floating-point difference. It is written up below rather than dropped.
- Planning is system state rather than a per-round improvisation, which turns multi-step analysis
  from luck into something reproducible.
- Deliverables import only the standard library, and the generated report states in its own text
  that chart rendering is not GPU-accelerated.

## Measured results

### Test machine

| | |
| :--- | :--- |
| GPU | NVIDIA GB10 |
| Driver | 580.126.09 |
| Architecture | aarch64, Linux 6.14.0-1015-nvidia |
| Memory | 121 GB (117 GB available) |
| CPU | 20 logical cores |
| cuDF | 25.10.00 |
| pandas | 2.3.3 (baseline) |
| Python | 3.11.16 |

### GPU against CPU, 20,000,000 rows / 3.04 GB, same file and command

| Operation | GPU | CPU | Speedup |
| :--- | ---: | ---: | ---: |
| `groupby` grouped aggregation | 2.60 s | 10.22 s | 3.93x |
| `corr` correlation matrix | 3.13 s | 11.56 s | 3.69x |
| `outliers` IQR detection | 3.37 s | 10.91 s | 3.24x |
| `summary` quantiles / std | 5.28 s | 16.16 s | 2.62x |
| `auto` combined profile | 9.45 s | 20.80 s | 2.20x |

The two engines agreed exactly on every value: `rel_diff = 0.00e+00`.

### End-to-end across scales

| Rows | File | Read | groupby | End to end |
| :--- | ---: | ---: | ---: | ---: |
| 3M | 564 MB | 8.30x | 8.25x | 6.86x |
| 10M | 1.88 GB | 8.30x | 11.89x | 7.16x |
| 30M | 5.67 GB | 7.22x | 12.44x | 6.45x |

Raw numbers and logs are in `benchmark/`.

### How much of the speedup is actually the GPU

This figure is easy to overstate, so it was measured on its own:

| Scenario | Speedup | What it includes |
| :--- | ---: | :--- |
| Cold-cache read | 6.09x - 6.35x | includes parallel I/O, which is not GPU-specific |
| Warm-cache read | 7.93x - 8.05x | includes parallel I/O, which is not GPU-specific |
| In-memory compute only | 2.86x - 3.09x | GPU compute on its own |

So roughly 47-49% of the cold-read gain comes from compute. The rest comes from parallel CSV
parsing and from pandas running its operations on one core while the machine has twenty. The
system prompt repeats this, and the agent restates it to the user rather than quoting 6.45x as
a headline figure.

### Multi-step analysis and the resident session

A single query running three times faster is a small gain, because analysis is rarely one step:
finding the outliers is almost always followed by locating their source, sizing their impact,
and comparing the groups they fall into.

A stateless engine re-reads the file on every one of those steps:

| Approach (20,000,000 rows / 3.04 GB) | Five-step full-data analysis |
| :--- | ---: |
| CPU, re-reading every step | 49.7 s |
| GPU, re-reading every step | 10.8 s |
| GPU with a resident session (`dataset_session`) | 1.5 s |

Inside one measured session on the same file:

```
open   load 20,000,000 rows    1.85 s   (1,692 MB resident)
       profile                 0.05 s
       outliers                0.42 s
       groupby by region       0.04 s
       groupby by category     0.03 s
       corr                    0.53 s
close  session total, 9 steps  8.46 s
       vs CPU re-reading per step   ~77 s   ->  19.2x
```

The scripted drill-down in the demo measured between 7.9x and 20.4x depending on how many
operations the model ran in that stage. Even the low end is an order of magnitude above the
3-4x of a single query, because what it saves is re-reading 3 GB per step.

Keeping the dataset resident is only possible because the machine holds 121 GB of device
memory, and it also genuinely runs out. `open` therefore checks free device memory before
loading and refuses rather than thrashing:

```
Not enough free device memory, load refused: the file is about 2.83GB, which needs roughly
12.5GB free and only 4.1GB is available. Use analyze_dataset for a single analysis, or pass
columns to read only the columns you need.
```

At most four sessions may be open at once; a fifth is refused instead of silently exhausting
memory.

Three guards exist because each one was needed in practice:

| Guard | The failure it prevents |
| :--- | :--- |
| File identity check (path, size, mtime) | If the file changes during a session, later steps would answer from a stale snapshot. The worker refuses and asks for a re-`open`. |
| A falsifiable no-re-read assertion | Verified by running the same operation twice and confirming the time does not change, and by checking that cumulative time equals load plus the sum of steps. Not a threshold guess. |
| Forced release at the end | The model does forget to `close` (measured: 1.7 GB left resident). `run()` releases in a `finally` block rather than relying on the prompt. |

## Plans: multi-step analysis that does not drift

Left to itself, a model works out each next step on the fly. That works, but it is not
reproducible: the same question asked three times produced three different paths, one of which
ran out of rounds and one of which abandoned the session halfway. In a live demo that is a
risk, and to a reviewer it looks like luck.

So the plan is system state rather than an instruction in the prompt:

```
dataset_session(operation="open", file_path=..., goal="find the cause of the revenue outliers")
    |
    |  the system immediately returns an ordered plan for that goal,
    |  with real column names filled in
    v
  plan: {kind: "drill_down", total_steps: 5, next_step: 0,
         steps: [profile, outliers, groupby(region), groupby(category), corr],
         do_next: 'operation="analyze", op="profile"'
                  '  # Confirm the real column names and types; later steps need them'}
    |
    |  each executed step is recorded automatically
    v
  plan: {completed: 1, next_step: 1, ...}
```

Three design decisions:

- Classification is a pure function of the goal text, scored by keyword, with no model call.
  The same goal run fifty times gives the same plan. A classifier the model invokes is a
  classifier that can change between runs.
- Steps are executable, not templates. Group-by steps carry column names read out of the data
  at open time. A step that says "group it" without naming the column is a template.
- Steps outside the plan are recorded as `off_plan` rather than rejected. Hiding them would
  make the progress report untrue. The plan guides; it does not confine.

Measured on the same question, "find the revenue outliers and explain them":

| | Rounds | Outcome |
| :--- | :--- | :--- |
| Before, run 1 | 6 (budget exhausted) | abandoned the session mid-way for the stateless path |
| Before, run 2 | 8 (budget exhausted) | completed 9 steps but forgot to `close` (backstop released it) |
| Before, run 3 | 3 | ran out of rounds with no answer |
| After | 6 | followed 4 planned steps and closed, with no retries |

Stage nine of the demo is this scenario: 1.861 s in session against 37.933 s on CPU, 20.4x.

## Deliverables

The brief asks for an agent that finishes work rather than one that only answers. An agent that
produces prose in a chat window has done half of it.

`export_deliverables` runs the full analysis on the GPU and writes three things the user can
take away:

| File | Contents |
| :--- | :--- |
| `report.md` | Source, row count, engine, statistics tables, outlier table, correlation table, embedded charts |
| `*.svg` | Bar, line and heatmap charts generated from the aggregate results |
| `*.csv`, `result.json` | The underlying numbers, for reuse |

The charts are hand-written SVG rather than matplotlib, for three reasons:

1. matplotlib is not installed on GB10 and pulling it onto aarch64 drags in a whole wheel
   stack. This file imports only the standard library.
2. Rendering is deterministic. No font discovery, no backend, no DPI differences, so the same
   file looks the same on a reviewer's laptop, in a browser, and after conversion to PDF.
3. SVG is text, so a chart change is visible in a commit diff.

One caveat is written into the generated report itself: chart rendering is not GPU-accelerated.
The GPU accelerated the aggregation behind the chart. Reporting a speedup for drawing five bars
would be meaningless.

Columns that are only identifiers are excluded from value charts automatically. An
auto-increment `row_id` with a mean of 10,000,000 otherwise compresses every real column into a
line at the axis. The rule is either the column name looks like an ID, or its values are exactly
0..N-1.

## Verification and reproducibility

### Stability

Stability is checked by a script rather than asserted. `agent/run_stability_check.sh` asks the
same multi-step question N times, prints the tool-call sequence for each run, and checks three
things:

1. the session skill was used throughout, with no fallback to the stateless path;
2. the session was closed, leaving no device memory held;
3. each run's operation order is an ordered subsequence of the plan.

```bash
bash run_stability_check.sh 3 /path/to/data.csv
```

Measured on GB10 with 20,000,000 rows:

| Question | Plan kind | Hygiene (session throughout, closed) | Plan coverage |
| :--- | :--- | :---: | :--- |
| Correlation plus group consistency | `relationships` | 3/3 | 3/3, identical call sequence every time |
| Source of revenue outliers | `drill_down` | 3/3 | 2/3 (one run closed after 3 of 5 steps) |

Hygiene means: no fallback to the stateless path, no duplicate `open`, and the session closed.
That column is now 6/6. Before the fixes below, one run exhausted its rounds, forgot to `close`,
and left 1.7 GB resident.

One residual variation is left in place rather than suppressed with more prompt rules. On the
`drill_down` goal, one run covered three steps (profile, outliers, groupby) before closing. The
answer was valid, just less thorough than the plan. That is the model's own judgement varying,
not a defect in the mechanism. The mechanism guarantees that progress is not lost, that memory
is not leaked, and that the run does not stall. It does not guarantee five steps every time.

### Four bugs the verification found

All four were surfaced by the script, not by inspection:

| # | Symptom | Root cause | Fix |
| :--- | :--- | :--- | :--- |
| 1 | `relationships` succeeded only 1 time in 3 | The model asked for `agg="revenue:corr"`, and per-group correlation cannot be expressed as a group-by aggregate. The error listed valid function names, so it assumed a typo, retried three times, and gave up with a failure answer. | The engine now suggests the alternative by intent (`corr` becomes `op='corr'`, `var` becomes `std`, quantiles become `op='summary'`), and the prompt states the order in which to handle an in-session error. |
| 2 | One run forgot to `close`, leaving 1.7 GB | It explored one extra drill-down dimension and used up the round budget. | `MAX_TOOL_ROUNDS` raised from 8 to 10, based on measured need rather than a guess. |
| 3 | One run opened two sessions | The model lost the `session_id` and re-opened, loading the same file twice (about 3.4 GB). | The same file now reuses the existing session and reports its `session_id` explicitly. |
| 4 | The checker reported correct behaviour as failure | It first required byte-identical call sequences across runs, which flagged exploring one extra dimension; then required strict plan order, which flagged a legitimate reordering; and it counted an early cross-check as a fallback to the stateless path. | The gate is now hygiene plus coverage. Ordering and extra steps are reported as information. |

The fourth is the most instructive one. A hand-written assertion is easier to get wrong than the
code it tests, and an assertion that is wrong in either direction, always failing or always
passing, never announces itself.

### Independent recomputation

The numbers the agent reports are not taken on trust. `verify_*.py` recomputes them from scratch
in pandas, which is a separate code path from cuDF, and compares:

```
column           agent  independent   match     pct
----------------------------------------------------
revenue        518,394      518,394      OK  10.37%
quantity             0            0      OK   0.00%

  revenue mean      = 1073.83      (agent said 1073.83)  OK
  revenue median    = 403.67       (agent said 403.67)   OK
  quantity min      = 1            (agent said 1)        OK
  quantity max      = 499          (agent said 499)      OK
  quantity median   = 250.0        (agent said 250)      OK

mismatches: 0
PASS: every recorded claim reproduced independently.
```

### An IQR bug that only real data exposed

On the UCI household power consumption dataset (2,075,259 rows), the IQR outlier count for the
`Voltage` column differed between engines:

```
pandas -> 51,067        cuDF -> 50,763        difference 304
```

Three diagnostic scripts ruled out the obvious causes. Q1 and Q3 were identical across engines
and interpolation methods, so the interpolation assumption was wrong. The real cause was
floating-point accumulation: pandas computed the lower fence as `233.14000000000004` and cuDF
computed `233.14`, a difference of 4e-14, and the data contains exactly `233.14`. A bare
`s < low` comparison therefore treated that tie differently in each engine.

The fix is a `1e-9` relative tolerance plus explicit reporting of `fence_ties_excluded`. All
seven columns now agree:

```
Voltage                 50,763   50,763    OK    fence_ties_excluded=304
Sub_metering_1         169,105  169,105    OK    ties=1,880,175 (IQR=0)
```

### Running the tests yourself

```bash
# test suites (the plan layer needs no GPU)
python skills/cudf-analytics/scripts/plan_test.py            # 56 checks
python skills/cudf-analytics/scripts/smoke_test.py           # 80 checks
bash   agent/run_criteria_tests.sh                           # 11 judging-criteria cases
bash   agent/run_stability_check.sh 3 <your-data.csv>        # reproducibility
```

## The agent application

### The tool-calling loop

```python
messages = [system, user]
for _ in range(MAX_TOOL_ROUNDS):          # 10, to stop a runaway loop
    resp = client.chat.completions.create(model=MODEL, messages=messages,
                                          tools=skill_definitions, temperature=0)
    if not resp.choices[0].message.tool_calls:
        break                             # the model is ready to answer
    for call in resp.choices[0].message.tool_calls:
        result = execute_tool(call)       # never raises; failures are JSON too
        messages.append(tool_result(result))
```

Four tools are registered: `analyze_dataset` (stateless full-dataset operations),
`dataset_session` (resident session, multi-step), `list_datasets` (discovery), and
`export_deliverables` (report, charts and CSV output).

### Behaviour on bad input

| Input | Agent behaviour |
| :--- | :--- |
| File does not exist | Reports the path back with an explicit error. It does not guess or invent a result. |
| Wrong column name | Calls `profile` to get the real column names, then retries. Self-heals within three rounds. |
| A column whose name is not ASCII | Same recovery path. |
| 1,440 distinct dates in the data | Does not treat every date as a group, which would blow up the token budget. |
| No GPU present | Falls back to pandas and states `engine="pandas"` with the reason. |
| API error or no `tool_calls` | Caught and degraded to a text answer. Does not crash. |

### Output contract

The engine puts metadata at the top level and each operation's data under its own key, so the
agent can tell whether the GPU path was actually taken:

```jsonc
{
  "ok": true,
  "op": "groupby",
  "engine": "cudf",              // cuDF succeeded
  "engine_version": "25.10.00",
  "gpu": "NVIDIA GB10",
  "accelerated": true,           // the GPU path really ran
  "fallback_reason": null,       // populated when it did not
  "total_seconds": 2.63,         // end to end, including the read
  "groupby": {
    "by": "region",
    "agg": "revenue:sum,mean",
    "rows_scanned": 20000000,    // note: the row count lives inside the operation block
    "compute_seconds": 1.91,     // compute only, excluding the read
    "groups": 5,                 // number of groups, not the data
    "sorted_by": "revenue__sum",
    "top_k": [ /* the actual group rows, at most top_k of them */ ]
  }
}
```

Exit codes, observed rather than assumed:

| Code | Meaning | Observed cases |
| ---: | :--- | :--- |
| 0 | Analysis completed, result in the `ok` field | Success; also returned when a file reads fine but has no usable columns |
| 2 | Bad request, which the model can correct itself | File missing, unknown `by` column, unknown `columns` entry |
| 3 | Unexpected engine failure | Anything not covered above; reserved for real bugs |

The distinction between 2 and 3 is practical. A 2 means the model passed a bad argument and can
retry; a 3 means the engine broke. No failure mode raises an exception; every one returns
`{"ok": false, "error": "..."}`.

## Repository layout

```
agent/                              the agent application (the submission proper)
  agent_main.py                     the tool-calling loop
  skills.py                         skill registration, GPU/CPU measurement, output trimming
  demo_script.py                    9-stage scripted demo (--prewarm; deliverables and drill-down)
  run_criteria_tests.sh             11 judging-criteria cases
  run_stability_check.sh            reproducibility: N runs, session hygiene and plan coverage
  gpu_vs_cpu_demo.py                side-by-side engine comparison
  session_skill_test.py             session tool-layer assertions
  env_stepfun.sh                    key loading for non-interactive shells
  verify_*.py                       independent recomputation of the agent's numbers
  diagnose_iqr*.py                  root-cause diagnosis of the IQR floating-point bug
  probe_tool_calling.py             confirms the model supports function calling

skills/cudf-analytics/              the skill itself
  SKILL.md                          trigger conditions and workflow
  skill-card.md                     governance card: identity, licence, risks, evaluation
  evals/evals.json                  13 evaluation tasks (5 positive, 3 negative, 5 safety)
  evals/run_evals.py                local trace-based scorer
  references/engine-contract.md     engine CLI contract, JSON field names, session protocol
  scripts/
    gpu_analytics.py                core engine (cuDF and pandas paths, stateless)
    gpu_session.py                  resident session worker
    analysis_plan.py                plan catalog and progress bookkeeping (pure functions)
    make_deliverables.py            zero-dependency SVG charts and Markdown report
    smoke_test.py                   59 self-checks plus GPU/CPU numeric agreement
    plan_test.py                    56 plan-layer assertions
    session_worker_test.py          worker protocol and guard tests
    attribution_test.py             attribution of I/O against compute
    memory_ceiling_test.py          multi-scale stress test
    benchmark_cpu_vs_gpu.py         benchmark harness

benchmark/                          measured GB10 evidence and raw logs
  benchmark_results.json            machine-readable measurements
  benchmark_results.md              measurement tables
  gb10_run.log                      raw run log
  smoke/gb10_smoke_test.log         self-check log from GB10
  charts/                           generated chart samples, readable without any dependency
    groupby_bar.svg                 grouped bar chart
    corr_heatmap.svg                correlation heatmap
    outliers_bar.svg                outlier distribution
    summary_means.svg               column means (identifier columns removed)

skill.md                            entry point, pointing at SKILL.md and this file
INNOVATION_OPTIONS.md               evaluation of five candidate innovations
LICENSE, requirements.txt
```

### How the skill gets triggered

The `description` field in `SKILL.md` is the trigger. It is written as the situations in which
the skill must be called, and it also names the situations in which it must not be.

One discovery detail matters. DSH only auto-discovers skills under `.dsh/skills`,
`.agents/skills` and `$DSH_HOME/skills`, and only one level deep as `<name>/SKILL.md`. So
`skills/cudf-analytics/SKILL.md` is not auto-loaded by DSH; it has to be copied or mounted into
one of those locations. Registering the tools with the model directly, as `skills.py` does here,
makes that step unnecessary.

## Running it

### Without a GPU

The GPU path needs a GB10, but the whole pipeline runs without one: the engine falls back to
pandas and reports `engine: "pandas"` with the reason. Analysis correctness, the agent loop and
error handling can all be verified on an ordinary machine.

```bash
pip install -r requirements.txt
python skills/cudf-analytics/scripts/smoke_test.py        # 80 checks
python skills/cudf-analytics/scripts/gpu_analytics.py --input <any.csv> --op auto
```

Expect `"accelerated": false` and a `fallback_reason` explaining that cuDF is unavailable. No
speedup is claimed on this path.

```bash
export STEPFUN_API_KEY=<your key>
cd agent && python agent_main.py --ask "analyze the outliers in /path/to/data.csv"
```

### On a GB10 (full GPU path)

```bash
ssh -p <port> <user>@<host>
source ~/.bashrc                        # provides STEPFUN_API_KEY
conda activate rapids-cudf              # cuDF 25.10 / pandas 2.3.3 / Python 3.11

# 1) skill self-check: 59 assertions including GPU/CPU numeric agreement
cd skills/cudf-analytics && python scripts/smoke_test.py

# 2) single agent question
cd ../../agent && python agent_main.py --ask "analyze the outliers in /path/to/data.csv"

# 3) scripted demo (9 stages, measured at 176.8 s, showing the measured speedup at each stage)
export DEMO_DATA=/path/to/sales_demo.csv
python demo_script.py --prewarm && python demo_script.py

# 4) judging-criteria suite (11 cases)
bash run_criteria_tests.sh

# 5) evaluation dataset (13 tasks: 5 positive, 3 negative, 5 safety)
python ../skills/cudf-analytics/evals/run_evals.py

# 6) reproducibility: the same question three times, checking hygiene and coverage
bash run_stability_check.sh 3 "$DEMO_DATA"

# 7) plan-layer unit tests (no GPU needed, 56 assertions)
python skills/cudf-analytics/scripts/plan_test.py
```

`--prewarm` matters. On the first run of any given question the agent also runs it on CPU to
measure the speedup, which takes 15-25 seconds. Prewarming writes those baselines to
`.gpu_vs_cpu_cache.json`, so every stage of the live demo can show its speedup immediately, and
it warms the session workflow so stage nine is not left waiting on a comparison measurement.

### Generating the demo data

The repository does not ship data files, since they run to several gigabytes. Generate one:

```bash
python skills/cudf-analytics/scripts/memory_ceiling_test.py --sizes 20m --columns 8
```

Use the 20,000,000-row file for a demo. At 5,000,000 rows the speedup is only 1.4x; at
20,000,000 it is 3.9x. Too small a dataset makes the GPU look useless.

### Conformance with the official skill directory spec

The official directory layout requires five governance artefacts per published skill; a
published skill missing any of them is rejected by the sync pipeline.

| Requirement | This repository | Status |
| :--- | :--- | :--- |
| `SKILL.md` | `skills/cudf-analytics/SKILL.md` | present |
| `skill-card.md` governance card | `skills/cudf-analytics/skill-card.md` | present, filled in section by section from the official Jinja template |
| Tier-3 evaluation dataset | `skills/cudf-analytics/evals/evals.json` | present, 13 tasks in the official schema |
| `BENCHMARK.md` evaluation report | — | not in the repository yet |
| `skill.oms.sig` detached signature | — | not applicable |
| `references/` | `skills/cudf-analytics/references/engine-contract.md` | present |

On the signature: `skill.oms.sig` is NVIDIA's signature over official skills, verified against
`nv-agent-root-cert.pem`. A third-party skill has no NVIDIA private key, so it cannot be signed
and should not be faked. The official material says as much: once the contents change, the old
signature no longer vouches for them. Changes belong either in an adaptation layer or in a
third-party skill. This is a third-party skill, so the signature row is not applicable rather
than missing.

On the evaluation, without overstating it: `evals/run_evals.py` is a local scorer written for
this repository. It grades on the tool-call trace, meaning which skill ran, with which arguments,
and whether the session was closed. It is not NVIDIA's NVSkills-Eval, and it does not produce the
five Security, Correctness, Discoverability, Effectiveness and Efficiency percentages, which
would require the official harness, reference scorers and a multi-agent run. The scorer says so
in its own output.

## Known limitations

- Plans come from a fixed catalog rather than being inferred on the spot. Four shapes
  (`drill_down`, `compare_groups`, `data_quality`, `relationships`) cover the common phrasings;
  a goal outside the catalog falls back to a default shape instead of deriving a new strategy.
  That is the price paid for reproducibility, not a ceiling on capability.
- Plans are not enforced, so step counts vary. The model may take steps the plan did not
  anticipate (recorded as `off_plan`), and as measured above it may close after covering only
  part of the plan. The mechanism guarantees that progress is recorded, memory is released and
  the run does not stall; it does not guarantee a full plan every time.
- Chart types are limited to bar, line, scatter, histogram and heatmap. There is no interactive
  dashboard.
- The CSV path is well tested; Parquet and Excel go through a generic read path.
- Deliverables are written to a local directory. There is no upload or sharing, and several
  datasets cannot be combined into one report.
- The speedup comparison runs an extra CPU pass, adding 15-25 seconds to the first query of a
  session. `--prewarm` avoids this during a demo.
- Answers to conceptual questions are limited by the model, not by the skill.
- Goal-to-plan classification matches English keywords. A goal phrased in another language falls
  back to the default plan shape.

## Third-party components

RAPIDS cuDF (Apache-2.0), pandas (BSD-3-Clause), openai-python (Apache-2.0) and StepFun
step-3.7-flash are dependencies, not bundled components, and the repository ships no binaries.

MIT licensed; see [LICENSE](LICENSE).