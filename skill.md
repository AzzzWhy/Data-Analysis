# cudf-analytics — Data Analysis Agent Skill (NVIDIA GB10 / DGX Spark)

Entry point. The **skill itself lives in `skills/cudf-analytics/SKILL.md`**; `README.md` has
the full usage guide, verification results and submission notes.

Submission reviewers: see [project, architecture and actual technology/model usage](docs/HACKATHON_SUBMISSION.md)
and [local model deployment and optimization boundaries](docs/LOCAL_MODEL_DEPLOYMENT.md).
The main branch includes both the Agent core and the final web workbench.

## Quick start (on GB10)

```bash
conda activate rapids-cudf
cd <this directory>

python skills/cudf-analytics/scripts/smoke_test.py            # self-check
python skills/cudf-analytics/scripts/gpu_analytics.py --input data.csv --op auto
python skills/cudf-analytics/scripts/benchmark_cpu_vs_gpu.py --rows 1m 5m 20m --repeats 3
```

## Files

| Path | Purpose |
| --- | --- |
| `skills/cudf-analytics/SKILL.md` | The skill: frontmatter (`name` / `description` trigger conditions) + workflow |
| `skills/cudf-analytics/scripts/gpu_analytics.py` | Core engine: cuDF analysis, pandas fallback, JSON output |
| `skills/cudf-analytics/scripts/benchmark_cpu_vs_gpu.py` | CPU vs GPU timing, produces the submission tables |
| `skills/cudf-analytics/scripts/smoke_test.py` | Core engine self-checks against independently computed truth |
| `agent/frontend_gateway.py`, `agent/gui.py`, `agent/gui/` | Local 8877 workbench and selectable SSH backend; managed SSH needs `requirements-gui.txt` |
| `agent/main_integration_test.py` | Real CPU worker through GUI, Chinese plan reuse and active-engine-switch protection; no model calls |
| `benchmark/` | GB10 results: comparison table, JSON, raw logs |
| `README.md` | Full guide: usage, output contract, verification, submission notes |

## How it triggers

The skill's `description` states the situations in which it **must** be called: analyzing
tabular files such as CSV/Parquet, statistical summaries, group-by aggregation, correlation,
and IQR outlier detection — especially at large scale or when the user complains that pandas
is slow. It also lists the cases that must **not** trigger (charting, model training,
querying a database, editing data).

The definition documents trigger intent and the execution contract. Users can ask in Chinese
or English; tool identifiers, JSON keys and command flags remain stable English names.

## Status

- Verified on GB10 (cuDF 25.10.00 / pandas 2.3.3): `smoke_test.py` exits 0 with no `[FAIL]` line,
  with cuDF and pandas agreeing exactly on mean, median and max. Use `--require-gpu` there to
  make the cuDF/pandas parity check mandatory rather than an explained skip.
- The suite's **check count is deliberately not quoted here**. It has drifted twice already --
  assertions were added and this line was not updated -- and the count also differs by machine,
  because a CPU-only run skips the parity check. The invariant is exit code 0 and no `[FAIL]`;
  read the current count with
  `python skills/cudf-analytics/scripts/smoke_test.py | grep -c '\[PASS\]'`.
  See `benchmark/VERIFICATION.md` for what each machine actually reported.
- Current historical CPU/GPU comparisons and their cold/warm boundaries are indexed in
  [README](README.md#性能证据与测量边界) and [core acceptance](docs/FINAL_CORE_DEPLOYMENT.md).
  They are workload-specific measurements, not fresh performance results for every main commit.
- The Agent application and tool-calling criteria suite live in `agent/`. Historical model
  checks and current CPU/GUI regressions are separate evidence; see
  [main integration](docs/MAIN_INTEGRATION_2026-09-29.md).

## Quick start for the agent

```bash
# On the compute host, with a working RAPIDS environment
conda activate rapids-cudf
cd /path/to/Data-Analysis
python agent/agent_main.py
```

Configure a compatible local or cloud model through the settings entry; no StepFun key or
particular shell configuration is assumed. See [local deployment](docs/LOCAL_MODEL_DEPLOYMENT.md).
For the web interface, run `python agent/frontend_gateway.py --port 8877` with the dependencies
listed in [README](README.md). Without a usable GPU, supported operations run on pandas and
report `engine="pandas"`; an explicit required-GPU check must not silently count as GPU success.
