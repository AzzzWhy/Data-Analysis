# cudf-analytics — Data Analysis Agent Skill (NVIDIA GB10 / DGX Spark)

Entry point. The **skill itself lives in `skills/cudf-analytics/SKILL.md`**; `README.md` has
the full usage guide, verification results and submission notes.

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
| `skills/cudf-analytics/scripts/smoke_test.py` | 59 self-checks against independently computed truth |
| `benchmark/` | GB10 results: comparison table, JSON, raw logs |
| `README.md` | Full guide: usage, output contract, verification, submission notes |

## How it triggers

The skill's `description` states the situations in which it **must** be called: analyzing
tabular files such as CSV/Parquet, statistical summaries, group-by aggregation, correlation,
and IQR outlier detection — especially at large scale or when the user complains that pandas
is slow. It also lists the cases that must **not** trigger (charting, model training,
querying a database, editing data).

The `description` deliberately carries both English and Chinese trigger vocabulary, because
users ask in either language.

## Status

- Verified on GB10 (cuDF 25.10.00 / pandas 2.3.3): `smoke_test.py` passes all 59 checks, with
  cuDF and pandas agreeing exactly on mean, median and max.
- End-to-end speedup of 6.45× on a 30,000,000-row CSV (5.67 GB): 19.286 s → 2.988 s, with
  zero difference in results between engines. **Note:** only about 3× of this is GPU compute;
  the read stage dominates. See the attribution table in `README.md`.
- Raw evidence: `benchmark/benchmark_results.md`, `benchmark/gb10_run.log`,
  `benchmark/smoke/gb10_smoke_test.log`.
- The agent application (tool-calling loop, scripted demo, 7-case criteria suite) lives in
  `agent/`; see "The agent application" in `README.md`. `run_criteria_tests.sh` passes 7/7.

## Quick start for the agent

```bash
# On the GB10 (substitute your own host/port/user)
ssh -p <port> <user>@<host>
source ~/.bashrc                 # provides STEPFUN_API_KEY
conda activate rapids-cudf
cd agent

python agent_main.py --ask "分析 /path/to/data.csv 的异常值"
python demo_script.py --prewarm && python demo_script.py   # 7-step demo, ~76 s
bash run_criteria_tests.sh       # 7 judging-criteria cases
```

Without a GPU the same code runs on pandas and reports `engine="pandas"` honestly; see the
"Reproducing it" section of `README.md` for the no-GPU path.