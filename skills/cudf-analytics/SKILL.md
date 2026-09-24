---
name: cudf-analytics
description: >-
  GPU-accelerated statistical analysis for large tabular datasets on NVIDIA GB10 / DGX
  Spark. Use this skill whenever the user asks to analyze, profile, summarize, aggregate,
  correlate, or find outliers/异常值 in a CSV, Parquet, TSV, JSONL, or Excel dataset —
  especially when the file is large (hundreds of MB to many GB, or millions of rows) or
  when the user mentions 大数据集, 统计摘要, 分组聚合, 相关性, 异常值, 数据分析, 算力加速,
  GPU, cuDF, RAPIDS, or complains that pandas is too slow or runs out of memory. Trigger it
  for questions like "这个 CSV 里各品类的平均销售额是多少", "帮我分析这份数据的分布和异常值",
  "算了 5000 万行要多久". Do NOT trigger for single-number arithmetic, fetching data from a
  database/API, building charts or dashboards, training ML models, or editing the dataset.
  When in doubt and a data file is involved, prefer this skill — it falls back to pandas
  automatically when no GPU is present, so it is always safe to call.
whenToUse: >-
  Analyzing a local tabular data file (CSV/Parquet/TSV/JSONL/Excel) for statistics,
  group-by aggregation, correlation, or outlier detection, especially at million-row scale
  where pandas is slow.
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

**Language:** the table above shows English phrasings, but this skill is language-agnostic.
Users commonly ask in Chinese (e.g. 「帮我分析这份数据的统计摘要」,「按 region 分组算平均销量」),
because the frontmatter `description` deliberately carries both English and Chinese trigger
vocabulary. Match the user's language when answering; the script's flags stay English.

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

## Performance reporting

Before quoting any number, confirm the harness is green:

```bash
python skills/cudf-analytics/scripts/smoke_test.py
```

That runs every operation against ground truth computed independently in pandas and
exits non-zero on any mismatch. On a GPU host it also checks that cuDF and pandas agree.
Verified on GB10 (cuDF 25.10.00 / pandas 2.3.3): all 59 checks pass, with cuDF and
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

## Failure handling

| Symptom | Action |
| --- | --- |
| `error: input file not found` | Verify the path; try again with the absolute path. |
| `engine` is `pandas` with a `fallback_reason` | Tell the user plainly that it ran on CPU, then fix the GPU env (see below). |
| Out-of-memory on a huge file | Add `--columns` to analyze fewer columns, or `--usecols` to load fewer. |
| Need a CPU-vs-GPU comparison of one command | Add `--force-cpu` and compare against the normal run. |
| `--op corr --method spearman` runs on CPU | Expected: cuDF only does pearson, so that request falls back and reports why. |

**Restoring the GPU path on GB10:**

```bash
conda activate rapids-cudf
python -c "import cudf; print(cudf.Series([1,2,3]).sum())"   # expect 6
```

If that import fails, the analysis still works — just on CPU. Fix the environment
before making any speed claims.

## Boundaries

- Read-only: this skill never writes to or mutates the user's dataset.
- It computes statistics; it does not plot, model, or fetch remote data.
- Correlation is not causation — do not present `corr` output as a causal finding.
- Always surface `rows_scanned` so the user knows the numbers came from the whole file,
  not a sample.