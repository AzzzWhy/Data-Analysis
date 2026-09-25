---
name: cudf-analytics
description: >-
  GPU-accelerated statistical analysis for large tabular datasets on NVIDIA GB10 / DGX
  Spark. Use this skill whenever the user asks to analyze, profile, summarize, aggregate,
  correlate, or find outliers in a CSV, Parquet, TSV, JSONL, or Excel dataset, especially
  when the file is large (hundreds of MB to many GB, or millions of rows) or when the user
  mentions GPU, cuDF, RAPIDS, or complains that pandas is too slow or runs out of memory.
  Trigger it for questions like "what is the average sale per category in this CSV", "analyze
  the distribution and outliers in this data", or "how long would 50 million rows take".
  Also trigger it when the user wants something they can keep, such as "make me a chart",
  "give me a report", "export this" or "I need it for a presentation", because this skill
  writes a Markdown report, SVG charts and CSV exports from the GPU-computed aggregates.
  Do NOT trigger for single-number arithmetic, fetching data from a database/API, training
  ML models, or editing the dataset. When in doubt and a data file is involved, prefer this
  skill — it falls back to pandas automatically when no GPU is present, so it is always safe
  to call.
whenToUse: >-
  Analyzing a local tabular data file (CSV/Parquet/TSV/JSONL/Excel) for statistics,
  group-by aggregation, correlation, or outlier detection, especially at million-row scale
  where pandas is slow; and producing a shareable report or charts from those results.
metadata:
  version: "0.1.0"
  author: NVIDIA DGX Spark Hackathon submission
  hardware: NVIDIA GB10 (DGX Spark), ARM64, unified memory, CUDA 13
  engine: cuDF 25.10 (RAPIDS) with automatic pandas fallback
  python: "3.11"
allowed-tools: Read Write Bash
---

# cuDF Analytics — GPU-accelerated dataset analysis

You have a skill that runs real statistical analysis on a data file using the GPU
(cuDF / RAPIDS) instead of asking a language model to reason about the numbers.

**Core rule: never estimate statistics from a sample you read into your context.**
Do not `head` the CSV and guess the mean. Load the file through this skill's script,
which computes exact aggregates on the full dataset on the GPU.

## When to use

Trigger when the user wants **computed facts about a dataset**, and any of these hold:

| Signal | Example user request |
| --- | --- |
| A data file is named or exists in the workspace | "analyze sales.csv", "what's in this parquet file" |
| Summary / distribution question | "show me the statistical summary of this data", "describe the columns" |
| Group-by aggregation | "average price and total units per region" |
| Relationship between columns | "which fields correlate most with churn" |
| Anomaly / outlier detection | "find the outliers in amount", "how many records are out of range" |
| Scale or speed is the point | "50 million rows", "pandas is too slow", "how long will this take" |

**Language:** the skill is driven in English. Its flags, operation names, `--agg` syntax, JSON
keys and trigger vocabulary are English throughout, and so are the example requests above.

Do **not** trigger for: building charts, training models, querying a remote database,
or any task where no tabular file is involved.

## Key concepts

- **Engine**: the script picks `cudf` when a GPU is usable and silently falls back to
  `pandas`. The returned JSON always states which engine ran in `engine` and, on
  fallback, says why in `fallback_reason`. Report the actual engine to the user — do
  not claim GPU speedup if `engine` is `pandas`.
- **`op=auto`** returns profile + summary + outliers in one pass. Start here, then ask
  for `groupby` / `corr` once you know the column names.
- **Numeric columns only**: summarize/groupby/corr/outliers operate on numeric columns.
  The `columns` field inside each operation's result lists what was actually used.
- **Output shape**: metadata (`engine`, `accelerated`, `rows_scanned`, timings…) sits at
  the top level. Each operation's data sits under its own key — `auto` gives
  `profile` / `summary` / `outliers`, and a direct call gives `result["profile"]`,
  `result["groupby"]`, etc.
- **Results are JSON on stdout**, diagnostics go to stderr. Exit code 0 means success;
  2 means a bad request (missing file, unknown column, bad `--agg`); 3 means the compute
  itself failed. Both error paths return `{"error": ...}`.
- **Every row is scanned by default.** Only add `--limit N` when the user explicitly
  asked for a sample, and say so when reporting the numbers.

## Workflow

1. **Find the file.** Verify the path exists and its size before running anything
   (a 4GB CSV is fine on GB10; a 40GB one is not).
2. **Profile first, in one call:**
   ```bash
   python skills/cudf-analytics/scripts/gpu_analytics.py --input sales.csv --op auto
   ```
   Read `profile.numeric_columns`, `profile.dtypes`, and `summary.stats` from the JSON.
3. **Drill down** using real column names from step 2:
   ```bash
   # group-by aggregation
   python skills/cudf-analytics/scripts/gpu_analytics.py --input sales.csv \
     --op groupby --by region --agg "revenue:sum,mean|units:sum|price:median"

   # correlation matrix
   python skills/cudf-analytics/scripts/gpu_analytics.py --input sales.csv \
     --op corr --top-k 20

   # IQR outliers on one column
   python skills/cudf-analytics/scripts/gpu_analytics.py --input sales.csv \
     --op outliers --columns revenue
   ```
4. **Answer in the user's own language**, quoting the exact numbers and stating which
   engine produced them. Keep the raw JSON out of the reply unless asked.

## Operations

| `--op` | What it does | Key flags |
| --- | --- | --- |
| `auto` | profile + summary + outliers together | `--columns` to narrow |
| `profile` | rows, columns, dtypes, null counts, memory, preview | `--preview-n` |
| `summary` | count/mean/std/min/quartiles/max per numeric column | `--columns` |
| `groupby` | grouped aggregates, sorted desc by first metric | `--by --agg --top-k` |
| `corr` | correlation matrix of numeric columns | `--method pearson\|kendall\|spearman --top-k` |
| `outliers` | IQR bounds (`Q1-1.5IQR`, `Q3+1.5IQR`) + offending rows | `--columns --iqr-k --top-k` |

`--agg` syntax: `col:func1,func2|col2:func` — e.g. `revenue:sum,mean|units:max`.
Available funcs: `count, size, sum, mean, min, max, std, median, nunique, first, last`.
All of these were verified working on cuDF 25.10 as plain string aliases. Note that
cuDF rejects a callable such as `pd.Series.nunique` with
`AttributeError: 'Aggregation' object has no attribute 'dtype'`, so function names are
always passed as strings. `std`/`var` on cuDF use `ddof=0` while pandas uses `ddof=1`,
which the result flags in `notes`.
Groups are sorted descending by the first requested metric.

**`--by` takes exactly one column.** A comma-separated pair such as `region,category` is
rejected with `--by column 'region,category' not found; available: [...]`. To break a metric
down by two dimensions, issue two groupby calls.

## Sessions: keeping the data resident across steps

`gpu_analytics.py` is stateless — every invocation re-reads and re-parses the file. That is
fine for one question and wasteful for five, because real analysis is rarely one step:
"find the outliers" is nearly always followed by "where do they come from", "how much do
they matter", "which group drives them".

`gpu_session.py` is a long-lived worker that loads the file **once** into device memory and
then answers many `analyze` requests against that resident copy.

| | 5-step full-data analysis (20M rows, 3.0 GB) |
| :--- | ---: |
| CPU, re-reading every step | 49.7 s |
| GPU, re-reading every step | 10.8 s |
| **GPU with a resident session** | **1.5 s** |

Measured inside one session: load 1.85 s (1,692 MB resident), then `profile` 0.05 s,
`outliers` 0.42 s, `groupby` 0.04 s, a second `groupby` 0.03 s, `corr` 0.53 s.

Protocol (newline-delimited JSON on stdin/stdout):

```bash
{"cmd": "open",    "path": "/data/sales.csv"}
{"cmd": "analyze", "sid": "s1", "op": "groupby", "by": "region", "agg": "revenue:sum"}
{"cmd": "list"}
{"cmd": "close",   "sid": "s1"}        # or "sid": "all"
```

Design points that are load-bearing:

- **Full data, every step.** Because the frame is resident there is no reason to subsample
  for speed, so `rows_scanned` is the whole file on every step and is reported so it can be
  checked.
- **Staleness is refused, not tolerated.** The handle records the file's size and mtime at
  open time and re-checks before every operation. If the file changed underneath, the worker
  refuses rather than computing on a snapshot that no longer matches the file.
- **Memory is admitted, not hidden.** `open` refuses *before* loading when the device cannot
  hold the file plus working room, and says what it needed and what it found. At most 4
  sessions may be open at once.
- **Every failure is JSON.** The worker outlives a bad request; the caller degrades to the
  stateless path.

Note the honest framing when reporting a session speedup: the figure includes the benefit of
not re-reading the file, which is not the same as GPU compute throughput. `close` returns a
`workflow_comparison` block whose `note` says exactly that, and the reported ratio should be
presented with it.

## Plans: multi-step analysis that does not drift

A session can be given a `goal`. The worker then materialises a strategy for that goal from a
fixed catalog (`analysis_plan.py`) and records each step's completion as it runs.

The reason this is state and not a prompt instruction: the same question asked three times
produced three different tool paths — one abandoned the session mid-way, one exhausted the
round budget, one switched tools half-way through. That is not a property a demo can rely on.
With the plan attached, the same question completes its steps and closes cleanly.

```
{"cmd": "open", "path": "/data/sales.csv", "goal": "find the cause of the revenue outliers"}
  -> plan: {kind: "drill_down", total_steps: 5, next_step: 0,
            steps: [profile, outliers, groupby(region), groupby(category), corr],
            do_next: 'operation="analyze", op="profile"'
                     '  # Confirm the real column names and types; later steps need them'}
{"cmd": "analyze", "sid": "s1", "op": "profile"}
  -> plan: {completed: 1, next_step: 1, ...}
```

Design decisions worth stating:

- **Classification is a pure function of the user's words**, by keyword score, not a model
  call. A classifier the model invokes is a classifier that can change between runs; this one
  is asserted stable over 50 consecutive calls in `plan_test.py`.
- **Steps carry real arguments.** Group-by steps are filled with categorical columns read from
  the frame at open time. A step that says "group it" without naming the column is a template.
- **Off-plan work is recorded, not rejected.** The model may investigate something the plan
  did not anticipate; that is marked `off_plan` and does not count toward progress. Hiding it
  would make the progress report lie.
- **Impossible steps are dropped.** With no categorical column known, group-by steps are
  omitted rather than emitted as calls that would immediately fail.

Four catalogued shapes cover the common questions: `drill_down` (anomaly → cause),
`compare_groups`, `data_quality`, `relationships`.

**What a plan does and does not guarantee.** It guarantees that progress is not lost, that the
session is not abandoned mid-way for the slow stateless path, and that memory is released. It
does **not** guarantee the same step count every run: measured over three runs of one
question, coverage was 3/3 for a `relationships` goal but 2/3 for a `drill_down` goal, where
one run closed after three steps instead of five. The answer was valid; it was just less
thorough. That residual variation is the model's judgement, and adding more prompt rules to
suppress it is not obviously an improvement.

Reproducibility is checked by a script in the agent directory rather than asserted:

```bash
bash agent/run_stability_check.sh 3 /path/to/data.csv
```

It gates on staying in the session and closing it, and reports ordering and extra steps as
information rather than failures — exploring one more grouping dimension, or covering the plan
steps in a different order, is legitimate analysis rather than divergence.

## Deliverables: charts and a report

`make_deliverables.py` turns an engine result into files a user can keep: `report.md`, one or
more `.svg` charts, and CSV/JSON of the underlying numbers.

It imports **only the standard library**. That is a deliberate choice over matplotlib:

- matplotlib is not present on GB10 and pulling it onto aarch64 drags in a wheel stack;
- hand-rolled SVG renders identically everywhere — no font discovery, no backend, no DPI
  differences, so a chart looks the same on a reviewer's laptop as in the browser;
- SVG is text, so a chart change is reviewable in a diff.

**State this plainly when reporting it:** chart *rendering* is not GPU-accelerated. The GPU
accelerated the aggregation that produced the plotted values. Claiming a GPU speedup for
drawing a handful of bars would be meaningless, and the generated report says so in its own
text rather than leaving it to the narrator.

Value charts automatically omit surrogate keys (`row_id` and anything matching an id pattern,
or spanning exactly 0..N-1). Without that filter one sequence column with a mean of 10,000,000
stretches the axis and flattens every real column into an invisible sliver — which happened on
the demo data before the filter existed.

Chart types: grouped bar, multi-series line, scatter, histogram, correlation heatmap.

## Performance reporting

Before quoting any number, confirm the harness is green:

```bash
python skills/cudf-analytics/scripts/smoke_test.py
```

That runs every operation against ground truth computed independently in pandas and
exits non-zero on any mismatch. On a GPU host it also checks that cuDF and pandas agree.
Verified on GB10 (cuDF 25.10.00 / pandas 2.3.3): all 76 checks pass, with cuDF and
pandas agreeing exactly on mean, median and max.

To substantiate a CPU vs GPU claim, run the benchmark instead of asserting one:

```bash
python skills/cudf-analytics/scripts/benchmark_cpu_vs_gpu.py --rows 1m 5m 20m --repeats 3
```

This writes `benchmark_results.json` plus a Markdown table you can paste into the
submission. Report measured numbers only; if `cudf` is unavailable the benchmark
will say so rather than inventing a speedup. Use `--format parquet` for I/O-bound
comparisons on GB10, where unified memory removes the host-to-device copy.

Measured on GB10 (pandas 2.3.3 baseline, 20 logical cores, median of 3, device
synchronized before every timed region, engines agreeing to `rel_diff=0.00e+00`):

| Rows | Step | pandas | cuDF | Speedup |
| ---: | :--- | ---: | ---: | ---: |
| 10,000,000 | read CSV | 5.843 s | 0.704 s | 8.30x |
| 10,000,000 | groupby | 0.217 s | 0.018 s | 11.89x |
| 10,000,000 | end-to-end | 6.459 s | 0.902 s | 7.16x |
| 30,000,000 | read CSV | 17.464 s | 2.420 s | 7.22x |
| 30,000,000 | groupby | 0.627 s | 0.050 s | 12.44x |
| 30,000,000 | end-to-end | 19.286 s | 2.988 s | 6.45x |

I/O and groupby show the largest gains (8x-12x); `corr` and `quantile` are only
about 2x because pandas already runs those in vectorized C. Quote the honest spread,
not just the best row.

### When the GPU is not worth using, and the engine says so

The table above times **compute phases with the library already loaded**. Those numbers are real
but they leave out the part that decides small-file cases: the GPU path carries about 1.5 seconds
of fixed cost (0.03 s Python start, 0.55 s `import cudf`, 0.95 s import plus first DataFrame).
On a small file that fixed cost is the whole runtime.

Measured end-to-end, both engines fresh, same file, `--op auto`:

| Rows | GPU | CPU | Ratio | Winner |
| ---: | ---: | ---: | ---: | :--- |
| 10,000 | 1.58 s | 0.20 s | 0.13x | CPU |
| 1,000,000 | 1.83 s | 0.57 s | 0.31x | CPU |
| 5,000,000 | 2.62 s | 2.23 s | 0.85x | CPU |
| 8,000,000 | 3.09 s | 3.44 s | 1.11x | GPU |
| 20,000,000 | 5.36 s | 8.09 s | 1.51x | GPU |

So the honest position is: **below roughly 6.5 million rows the GPU is slower, and the engine
routes to pandas on its own** rather than reporting a speedup that is a slowdown. Use
`--force-gpu` to override when you want the comparison instead of the fastest path.

Note the gap between 7.16x in the table above and 1.51x here: they measure different things, and
both are correct. The 7.16x is compute-only with a 20-core pandas baseline; the 1.51x is the
whole process including CSV parsing and startup, against the single-core pandas path the engine
actually falls back to. Quote whichever one matches the question being asked, and say which it is.

## Failure handling

| Symptom | Action |
| --- | --- |
| `error: input file not found` | Verify the path; try again with the absolute path. |
| `engine` is `pandas` with a `fallback_reason` | Tell the user plainly that it ran on CPU, then fix the GPU env (see below). |
| Out-of-memory on a huge file | Add `--columns` to analyze fewer columns, or `--usecols` to load fewer. |
| Need a CPU-vs-GPU comparison of one command | Add `--force-cpu` and compare against the normal run. |
| `--op corr --method spearman` runs on CPU | Expected: cuDF only does pearson, so that request falls back and reports why. |
| `--by region,category` rejected | `--by` takes one column. The error now names the one-column rule and the valid halves; issue two groupby calls. |
| `unsupported agg func(s)` | The error names an alternative by intent: `corr` → use `op='corr'` (group-by cannot compute per-group correlation), `var` → `std`, `avg` → `mean`, quantiles → `op='summary'`. Do not repeat the same argument. |
| An error inside an open session | Read it, change the argument, retry on the **same session**. Do not `close` first: closing leaves only the slow re-reading path. |
| `memory refused on session open` | Expected guard, raised before loading. Use `analyze_dataset` for a one-off, or pass `columns` to load fewer. |
| Session says the file changed on disk | The file changed under the session, so its answers would be stale. Re-`open` it. |
| Session left open after a run | The agent releases it in a `finally` block; the model is also told to `close`. If a session is ever orphaned, `{"cmd":"close","sid":"all"}` frees it. |
| A chart has one enormous bar and the rest are slivers | A surrogate key is in the chart. `summary_means.svg` filters these; check the omitted-columns note in its subtitle. |

**Restoring the GPU path on GB10:**

```bash
conda activate rapids-cudf
python -c "import cudf; print(cudf.Series([1,2,3]).sum())"   # expect 6
```

If that import fails, the analysis still works — just on CPU. Fix the environment
before making any speed claims.

## Boundaries

- Read-only: this skill never writes to or mutates the user's dataset.
- It computes statistics and renders charts; it does not model or fetch remote data.
- Correlation is not causation — do not present `corr` output as a causal finding.
- `--by` groups by a single column only.
- A session holds the dataset in device memory for its whole lifetime (about 1.7 GB per 20M
  rows), so sessions are capped at 4 and refused outright when memory is short. For one-off
  questions use the stateless path, which holds nothing between calls.
- Chart rendering is CPU-only and cheap. Never report a GPU speedup for producing a chart;
  the GPU speedup belongs to the aggregation behind it.
- Always surface `rows_scanned` so the user knows the numbers came from the whole file,
  not a sample.