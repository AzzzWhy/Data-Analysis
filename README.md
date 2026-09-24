# Data Analysis Agent Skill (cuDF-accelerated) — NVIDIA DGX Spark Hackathon

A Skill package that closes the loop from **agent question → skill trigger → GPU compute →
delivered result**. It performs large-scale tabular data analysis with **cuDF (RAPIDS)** on
**NVIDIA GB10 / DGX Spark**, and **falls back to pandas automatically** when no GPU or no
cuDF is available, so it produces a result in any environment.

The headline result, measured on real hardware, is in
[Measured on GB10](#measured-on-gb10) below.

---

## Layout

```
skills/cudf-analytics/
├── SKILL.md                        # The skill: YAML frontmatter + trigger conditions + workflow
└── scripts/
    ├── gpu_analytics.py            # Core engine: cuDF analysis + pandas fallback, emits JSON
    ├── benchmark_cpu_vs_gpu.py     # CPU vs GPU timing, produces the submission tables
    └── smoke_test.py               # 59 self-checks against independently computed truth

benchmark/
├── benchmark_results.md            # The GB10 comparison table (paste straight into a submission)
├── benchmark_results.json          # Same, with full conditions and precision deltas
├── gb10_run.log                    # Raw benchmark output
└── smoke/gb10_smoke_test.log       # Raw log: 59 checks passing on GB10 (cuDF 25.10)

.tools/gb10.ps1                     # Helper to drive GB10 over SSH from Windows without prompts
README.md                           # This file
skill.md                            # Short entry-point note
```

> **Discovery note:** DSH auto-discovers skills only under `.dsh/skills`, `.agents/skills`,
> or `$DSH_HOME/skills`, and it recognises only `<name>/SKILL.md` at one level deep.
> Therefore `skills/cudf-analytics/SKILL.md` is **not** loaded automatically by DSH.
> To make DSH pick it up, copy (or mount) `skills/cudf-analytics/` into one of those
> locations. If your agent registers tools via `skills.py` function-calling instead,
> no such step is needed.

### About `.tools/gb10.ps1`

The `ssh` shipped with Windows cannot accept a password non-interactively, so this helper
drives GB10 through SSH.NET:

```powershell
$env:GB10_PW='<password>'
.\.tools\gb10.ps1 -Cmd "nvidia-smi"                    # run a remote command
.\.tools\gb10.ps1 -Put "<local>" -To "<remote>"         # upload a file
```

The password is read only from an environment variable — never written to disk, never echoed.
GB10 currently uses **password authentication**; switching to public-key auth (append this
machine's `~/.ssh/id_ed25519.pub` to `~/.ssh/authorized_keys` on GB10) is safer and simpler.

The skill's `description` field is the trigger. It is written as "what the user says /
when this must be called", and it explicitly lists the cases that should **not** trigger
(charting, model training, querying a database) to avoid false positives.

---

## Capabilities

| Operation | Description |
| --- | --- |
| `profile` | row/column counts, dtypes, null counts, memory use, first N rows |
| `summary` | per-column count/mean/std/min/Q1/median/Q3/max |
| `groupby` | grouped aggregation supporting `count,size,sum,mean,min,max,std,median,nunique,first,last` |
| `corr` | correlation matrix (pearson / kendall / spearman) |
| `outliers` | IQR outlier detection (`Q1-1.5IQR` … `Q3+1.5IQR`) plus the offending rows |
| `auto` | profile + summary + outliers in a single call |

Supported formats: CSV / Parquet / TSV / JSONL / JSON / Excel.

---

## Running it on GB10 (three steps)

```bash
conda activate rapids-cudf
cd <this directory>

# 1) Self-check: every operation is correct, and GPU and CPU agree numerically
python skills/cudf-analytics/scripts/smoke_test.py

# 2) A real dataset: --op auto returns the summary and outliers in one call
python skills/cudf-analytics/scripts/gpu_analytics.py --input /path/to/data.csv --op auto

# 3) Produce the CPU-vs-GPU comparison for the submission
python skills/cudf-analytics/scripts/benchmark_cpu_vs_gpu.py --rows 1m 5m 20m --repeats 3
```

Step 3 writes `benchmark_results.json` and `benchmark_results.md`, both ready to paste into
a submission.

### Common invocations

```bash
# Group by region, return the top 10 groups
gpu_analytics.py --input sales.csv --op groupby --by region \
  --agg "revenue:sum,mean|quantity:max" --top-k 10

# Analyze selected columns only; restrict what is read to save memory
gpu_analytics.py --input big.csv --op outliers --columns revenue,cost --usecols row_id,region,revenue,cost

# Force the CPU path, to compare against the GPU result
gpu_analytics.py --input sales.csv --op summary --force-cpu
```

---

## Output contract (what an agent parses)

```jsonc
{
  "ok": true,
  "op": "auto",
  "engine": "cudf",              // or "pandas"
  "accelerated": true,           // true only when engine == "cudf"
  "fallback_reason": null,       // explains why, when it fell back
  "rows_scanned": 20000000,      // the whole file, not a sample
  "op_seconds": 1.83,
  "profile": { ... }, "summary": { ... }, "outliers": { ... }
}
```

**Key rule: GPU acceleration may only be claimed when `engine == "cudf"`.**
On fallback the script records the reason in `fallback_reason`, and the agent must tell the
user plainly that it ran on CPU.

Exit codes: `0` success; `2` bad request (missing file, unknown column, malformed `--agg`);
`3` the computation itself failed.

---

## Measured on GB10

### Environment

`NVIDIA GB10`, driver 580.126.09, aarch64, 121 GB unified memory, Linux 6.14.0-1015-nvidia,
20 logical cores. `rapids-cudf` conda env: **cuDF 25.10.00 / pandas 2.3.3 / numpy 2.2.6 /
Python 3.11.16**. `import cudf` plus a smoke computation verified working.

### Verification: `smoke_test.py` — 59 checks, 0 failures

`engine=cudf`, `gpu=NVIDIA GB10`. Every operation's output was compared against ground truth
computed independently in pandas:

- `summary` / `groupby` (including the `median` and `nunique` aliases) / `corr` / `outliers`
  all match the independent values
- **GPU vs CPU cross-check passed**: mean, median and max are identical under cuDF and pandas
- Error handling and fallback behaviour match the contract (all four exit-code-2 bad requests)
- Raw log: `benchmark/smoke/gb10_smoke_test.log`

### Benchmark: three scales, zero numerical difference between engines

Baseline is pandas 2.3.3 with default settings (single-threaded C-level aggregations),
20 logical cores, median of 3 repeats, device synchronized before every timed region,
both engines reading the same file with the page cache warmed.

| Rows | File | Step | pandas | cuDF | Speedup |
| ---: | ---: | :--- | ---: | ---: | ---: |
| 3,000,000 | 564 MB | read CSV | 1.673 s | 0.202 s | **8.30×** |
| 3,000,000 | 564 MB | groupby | 0.056 s | 0.007 s | **8.25×** |
| 3,000,000 | 564 MB | end-to-end | 1.845 s | 0.269 s | **6.86×** |
| 10,000,000 | 1.88 GB | read CSV | 5.843 s | 0.704 s | **8.30×** |
| 10,000,000 | 1.88 GB | groupby | 0.217 s | 0.018 s | **11.89×** |
| 10,000,000 | 1.88 GB | end-to-end | 6.459 s | 0.902 s | **7.16×** |
| 30,000,000 | 5.67 GB | read CSV | 17.464 s | 2.420 s | **7.22×** |
| 30,000,000 | 5.67 GB | groupby | 0.627 s | 0.050 s | **12.44×** |
| 30,000,000 | 5.67 GB | corr | 0.764 s | 0.336 s | **2.27×** |
| 30,000,000 | 5.67 GB | quantile | 0.405 s | 0.175 s | **2.31×** |
| 30,000,000 | 5.67 GB | end-to-end | 19.286 s | 2.988 s | **6.45×** |

**Conclusion: end-to-end analysis of a 30,000,000-row CSV (5.67 GB) drops from 19.3 s to
3.0 s — roughly 6.5× — with zero difference in the results between the two engines**
(`rel_diff=0.00e+00`).

### But how much of that is actually the GPU?

That end-to-end number is **not** a measure of GPU compute: the file read accounts for
81–91% of it. `attribution_test.py` separates the two by pre-loading both engines outside
the timed region, so only computation is measured:

| Rows | Scenario | pandas | cuDF | Speedup |
| ---: | :--- | ---: | ---: | ---: |
| 3,000,000 | read, cold cache | 1.783 s | 0.293 s | **6.09×** |
| 3,000,000 | read, warm cache | — | — | **7.93×** |
| 3,000,000 | **compute, in-memory (all stages)** | — | — | **2.86×** |
| 10,000,000 | read, cold cache | — | — | **6.35×** |
| 10,000,000 | **compute, in-memory (all stages)** | — | — | **3.09×** |

**Only the "compute, in-memory" rows are attributable to the GPU: about 3×.**
Of the ~6–8× read gain, roughly half is compute and the rest is parallel CSV parsing —
which a CPU-only tool such as polars or Dask could also achieve. The pandas baseline also
uses a single core out of 20, so part of every figure above is core count rather than the
GPU. Quote the ~3× compute number alongside any end-to-end figure.

Reproduce: `python scripts/attribution_test.py --rows 3m 10m --repeats 5`

### A correctness bug found only on real data

Real data exposed something synthetic fixtures could not: IQR fences are computed by two
different engines, and floating-point error lands differently in each.

On the 2M-row power dataset, column `Voltage`: pandas computed the lower fence as
`233.14000000000004` and cuDF as `233.14` — a gap of 4e-14. The column contains the exact
value `233.14`, so a bare `s < fence` test counted **304 extra outliers under pandas**:
51,067 vs 50,763 for the same query on the same file.

The fix applies a relative tolerance (`1e-9`) to the fence comparison, so a value sitting
on a fence is treated as a tie and not called an outlier. Both engines now report identical
counts on all seven columns, and any ties are reported explicitly in
`fence_ties_excluded` rather than silently changing the total. Verified by
`agent/verify_fence_fix.py`: all seven columns agree.

This is the kind of defect that only shows up on real data with real precision limits, and
it would have been invisible in a demo — the same question would simply have returned two
different answers on two different runs.

### Caveats

- The speedups above were measured on **synthetic data**. Re-running the benchmark on a real
  dataset will give different numbers (column types, share of string columns and null ratio
  all matter).
- Generating the 5.67 GB benchmark file created 7.6 GB of temporary data on GB10, which has
  been cleaned up.

---

## Submission notes

1. **Form**: an agent application plus skill encapsulation (`SKILL.md` defines the trigger
   conditions and the standard workflow; `scripts/` provides the tool calls). The agent
   understands the request, orchestrates the flow and delivers the result.
2. **GPU compute**: cuDF performs full-file aggregation on GB10 — no sampling. The output
   field `rows_scanned` carries the full row count.
3. **Quantified comparison**: use the measured table above, and always state the conditions
   (pandas defaults, single-threaded, as baseline; 20 logical cores; median of 3; device
   synchronized before timing; page cache warmed).
4. **Numerical correctness**: this is the easiest thing to be challenged on. Answer with
   `rel_diff=0.00e+00` and the 59-check suite.
5. **Hardware advantage**: GB10's unified memory removes the host↔device copy. Scaled test:
   20,000,000 rows → cuDF 2.7 s vs pandas 13.1 s (4.9×); 60,000,000 rows → cuDF 7.0 s vs
   pandas 38.8 s (5.5×). Note the read stage is ~80% of both figures.
6. **Robustness**: automatic fallback to pandas when no GPU / cuDF is available, so a
   reviewer's environment can still run it.

---

## The agent application

The competition's actual target is an **agent that autonomously calls Skills**, not a Skill in
isolation — so the tool-calling loop is the main deliverable, and the Skill is the component
it drives.

```
agent/
├── agent_main.py          # StepFun LLM + the tool-calling loop (the core of the submission)
├── skills.py              # Skill schemas (what the model sees) + implementations (what runs)
├── demo_script.py         # scripted live demo, one step per judging criterion
├── run_criteria_tests.sh  # 7-case suite asserting autonomous selection and robustness
├── env_stepfun.sh         # loads STEPFUN_API_KEY in a non-interactive shell
└── verify_*.py            # independent re-computation of the agent's own claims
```

### Loop shape

```
user question
  -> model (with skill schemas attached)
  -> tool_calls? --no--> final answer (done)
  |                    yes
  -> execute the skill locally on GB10
  -> append the real result to the transcript
  -> model again            (bounded by MAX_TOOL_ROUNDS = 6)
```

Two skills are offered: `analyze_dataset` (wraps `gpu_analytics.py`) and `list_datasets`
(discovers what data exists). The same `SKILL.md` trigger knowledge is carried in the schema
`description`, because that text — not the markdown file — is what the model actually reads
when deciding whether to call.

### Measured results

`step-3.7-flash` supports function calling (verified before building on it), and selects the
right skill from Chinese prompts without being told which one to use:

| Prompt (Chinese) | Skill the agent chose | Result |
| :--- | :--- | :--- |
| 帮我看看这份数据，给我一个整体概览 | `analyze_dataset(operation="auto")` | cuDF, 5,000,000 rows, 3.1 s |
| 按 region 统计 revenue 总和与均值 | `groupby(by="region", agg="revenue:sum,mean", top_k=5)` | cuDF, 1.2 s |
| 哪些数值列之间相关性最强 | `corr(top_k=10)` | cuDF, 1.4 s |
| 单独算 revenue 和 cost 的 IQR 异常值 | `outliers(columns="revenue,cost")` | cuDF, 1.7 s |
| 服务器上有哪些数据文件 | `list_datasets({})` | 0.01 s |
| 解释一下什么是 IQR | *(no tool — correct)* | 5.5 s |

`run_criteria_tests.sh`: **7 / 7 cases pass**, covering autonomous selection, argument
correctness, a missing file, a wrong column name, file discovery, and the negative case where
no tool should be called. Full demo run: **7 steps, ~63 s total**.

### Robustness, demonstrated rather than asserted

Given a Chinese business term that is not a real column (`销售额`), the agent recovers on its
own:

```
round 1 -> groupby(agg="销售额:sum")   FAILED: Column(s) ['销售额'] do not exist
round 2 -> profile                     OK   (learns the real column names)
round 3 -> groupby(agg="revenue:sum")  OK   (retries correctly)
```

Other handled paths: missing file (the agent calls the tool, gets the real error, and explains
it — rather than guessing from the filename), unknown skill name, malformed argument JSON,
API failure, and a runaway tool loop (hard cap, then a forced summary).

### Every number the agent reports was independently re-checked

Reusing a valid earlier result is not fabrication, but it must still be verified. The agent
once answered an outlier question from a previous `auto` result instead of calling the tool
again; recomputing with pandas confirmed all of it (518,394 = 10.37% for `revenue`, 0 for
`quantity`, mean 1,073.83, median 403.67) — zero discrepancies. Separately, `Voltage` on the
power dataset *did* disagree by 304 counts, which is the floating-point bug described above.

### Known limitation

`auto` already computes outliers, so an agent may answer a later outlier question by reusing
that block instead of making a fresh call. The prompt now requires a new call per distinct
analysis request, and the demo questions are chosen so each step needs a genuine computation.

### The GPU speedup is shown inside the answer, per call

A benchmark table can be dismissed as "measured somewhere else". Instead, every tool call
also runs the identical query on the CPU and the measured comparison is reported in the
answer itself:

```
本次分析在 GPU（NVIDIA GB10）上通过 cuDF 完成，全量 20,000,000 行耗时 5.50 秒；
同一计算在 CPU pandas 上耗时 10.91 秒，GPU 快 1.99 倍。
说明：该倍数是端到端耗时对比，其中 CSV 读取阶段两引擎都利用了多核并行 I/O；
CPU 基线 pandas 运行在单线程，因此该倍数不等于纯 GPU 计算内核的理论加速比。
```

Measured per-step speedups on the 20M-row file (3.0 GB), same file and same command:

| Operation | GPU | CPU | Speedup |
| :--- | ---: | ---: | ---: |
| `groupby` (revenue sum/mean by region) | 2.60 s | 10.22 s | **3.93×** |
| `corr` (correlation matrix) | 3.13 s | 11.56 s | **3.69×** |
| `outliers` (IQR, two columns) | 3.37 s | 10.91 s | **3.24×** |
| `summary` (quantiles, std) | 5.28 s | 16.16 s | **2.62×** |
| `auto` (profile + summary + outliers) | 9.45 s | 20.80 s | **2.20×** |

Methodology, chosen so the ratio survives scrutiny:

- the CPU side runs **twice** and the **best** time is used, so CPU jitter cannot flatter the GPU;
- both engines process the **same file** with the **same command**, verified by comparing
  scanned row counts — an earlier bug passed `--limit` and silently compared 5 rows instead
  of 20 million, producing a nonsense "0.1×";
- the model is instructed to quote **GPU time, CPU time and the ratio**, and to repeat the
  caveat that read time and core count are part of the figure.

```bash
python demo_script.py --prewarm   # measure the comparisons before the audience arrives
python demo_script.py             # 7 steps, ~76 s, each showing its speedup
```

`--prewarm` exists because the first run of a given query pays for the CPU measurement. Warming
writes the comparisons to `.gpu_vs_cpu_cache.json`, so live steps show their speedup
immediately. Without it the demo takes ~128 s; with it, ~76 s.

---

## Reproducing the whole thing on GB10

```bash
ssh -p 6060 Developer@106.13.186.155
source ~/.bashrc                        # provides STEPFUN_API_KEY
conda activate rapids-cudf

# 1) Skill self-check (59 assertions, GPU vs CPU cross-validation)
cd ~/cudf-analytics-skill && python scripts/smoke_test.py

# 2) The agent, one question
cd ~/agent && python agent_main.py --ask "分析一下 /home/Developer/sales_demo_small.csv 的异常值"

# 3) The scripted demo (7 steps, ~63 s)
cd ~/agent && python demo_script.py

# 4) The criteria suite (7 cases)
cd ~/agent && bash run_criteria_tests.sh
```

Demo datasets on GB10: `sales_demo_small.csv` (5M rows, 572 MB — use this for live demos) and
`sales_demo.csv` (20M rows, 3.0 GB — use this to show scale).