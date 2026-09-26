# Engine and tool contract

Reference for the exact arguments, output shape, and failure modes of this skill's scripts.
Read this instead of guessing an argument name — several of the constraints below cannot be
inferred from the operation names, and each one is here because it was observed being got
wrong.

## Scripts

| Script | Role | Invocation |
| :--- | :--- | :--- |
| `gpu_analytics.py` | Stateless engine. One operation per process; re-reads the file every time. | `python gpu_analytics.py --input FILE --op OP [...]` |
| `gpu_session.py` | Resident session worker. NDJSON commands on stdin/stdout; loads once, serves many steps. | `python gpu_session.py`, then one JSON command per line |
| `make_deliverables.py` | Turns an engine result into `report.md`, `.svg` charts and CSV/JSON exports. | `python make_deliverables.py --input - --out-dir DIR --source-file FILE [--lang zh\|en\|auto] [--lang-context TEXT]` |
| `build_html_report.py` | Folds the deliverables above into one self-contained `report.html` with every SVG inlined, led by the engine decision. | `python build_html_report.py --out-dir DIR [--lang zh\|en\|auto] [--title "..."]` |
| `analysis_plan.py` | Plan catalog and progress bookkeeping. Imported, not run directly. | `import analysis_plan` |

### Which engine runs, and why it is not always the GPU

`--engine auto` (the default) picks the engine from the file, not from a preference for the GPU.
The GPU path carries a large **fixed** cost, so on small files it is slower, and the engine says
so rather than quietly reporting a "GPU speedup" that is really a slowdown.

Fixed cost, measured on the GB10 by decomposition:

| Step | Seconds |
| :--- | ---: |
| bare Python start | 0.03 |
| `import cudf` | 0.55 |
| `import cudf` + build one DataFrame | 0.95 |

Crossover, measured with both engines fresh and `--op auto`. The deciding axis is **bytes**, and
row width shifts where the crossover sits:

| Shape | Rows | Size | CPU s | GPU s | Ratio | Winner |
| :--- | ---: | ---: | ---: | ---: | ---: | :--- |
| narrow, ~41 B/row | 5,000,000 | 0.217 GB | 2.21 | 2.85 | 0.78× | CPU |
| narrow, ~41 B/row | 8,000,000 | 0.348 GB | 3.35 | 3.25 | 1.03× | GPU (marginal) |
| narrow, ~44 B/row | 20,000,000 | 0.881 GB | 8.19 | 5.40 | 1.52× | GPU |
| narrow, 50 B/row | 50,000,000 | 2.500 GB | 25.23 | 10.59 | 2.38× | GPU |
| wide, 152 B/row | 2,000,000 | 0.301 GB | 2.35 | 2.76 | 0.85× | CPU |
| wide, 152 B/row | 4,000,000 | 0.604 GB | 4.66 | 3.67 | 1.27× | GPU |
| wide, 152 B/row | 20,000,000 | 3.038 GB | 21.61 | 9.92 | 2.18× | GPU |
| wide, 152 B/row | 40,000,000 | 6.082 GB | 44.59 | 16.84 | 2.65× | GPU |

Two corrections this table forced, both of which were wrong in an earlier version of this file:

1. **The axis is bytes, not rows.** The same 20,000,000 rows measured 1.52× at 0.88 GB and 2.18×
   at 3.04 GB. No row-count threshold can express that.
2. **Width still shifts the crossover**, because a 17-digit float column costs the CPU more per
   byte than a short integer column. The measured crossover band is 0.22–0.35 GB for narrow rows
   and 0.30–0.60 GB for wide rows.

So the thresholds are `0.55 GB` for wide rows and `0.40 GB` for narrow rows (under 80 B/row), and
both sit at the HIGH end of their measured band on purpose: routing to the CPU when the GPU would
have been marginally faster costs a few percent, while routing to the GPU when the CPU would have
won pays the full ~1.5 s startup for nothing.

| Flag | Effect |
| :--- | :--- |
| `--engine auto` (default) | Route by file size in bytes, with the width adjustment above |
| `--engine cpu` / `--force-cpu` | Force pandas |
| `--engine gpu` / `--force-gpu` | Force cuDF, even below the crossover |

The result JSON reports both decisions separately and they mean different things:

- `routing_reason` — the CPU was chosen **deliberately**, with the measurement that justified it.
  This is not a failure.
- `fallback_reason` — the GPU was tried and **failed**. This is a failure.

A deliberate routing decision never populates `fallback_reason`, because reporting a conscious
choice as a defect would both mislead the caller and hide a real fallback when one happens.

Row count is **estimated** from a 1 MB prefix, not counted: counting exactly would cost a full
read, which is the thing the engine exists to avoid paying twice. It is now used only to derive
bytes-per-row for the width adjustment; the routing decision itself is made on size, since that is
the quantity that decides. Measured error on the estimate is under 8% on files of 200k rows, and
it is density-independent.

### What happens as data keeps growing

Fitting both engines per byte of CSV, over the measured points:

| Shape | CPU per byte | GPU per byte | CPU/GPU slope | Predicted ceiling |
| :--- | ---: | ---: | ---: | ---: |
| narrow, ~50 B/row | ~10.2 s/GB | ~2.4 s/GB | ~4.3× | ~4.3× |
| wide, ~152 B/row | ~7.2 s/GB | ~1.5 s/GB | ~4.6–8× | ~4.6–8× |

The rate is roughly linear while the file fits comfortably in memory, and the speedup approaches
the slope ratio. Measured 6.08 GB at 2.65× against a model prediction of 2.7×, so the linear range
holds at least to 6 GB.

Do **not** read this as an unbounded trend, and be careful quoting larger extrapolations:

- The 80,000,000-row point (4.00 GB) came in at 2.09×, below the 50,000,000-row point's 2.38×, and
  a best-of-3 re-measurement confirmed 2.09× rather than thermal noise. Per GB the GPU cost rose
  from 4.24 s/GB at 2.5 GB to 5.37 s/GB at 4.0 GB, while the CPU cost stayed flat near 10.3 s/GB.
  The GPU path degrades before the CPU one does, so the ceiling is a ceiling and not a floor that
  keeps rising.
- Memory is the hard limit, not time. cuDF is a device library: a 6 GB CSV plus the frame built
  from it must coexist with the CUDA context, and the resident-session path holds it for the
  session's life. The GB10 has 121 GB of unified memory so this is far off, but on a smaller
  device it binds long before the speedup curve flattens.
- These are single-op end-to-end timings. The multi-step resident-session numbers below are a
  separate effect and are not additive with this.

### Repeated work: residency is not GPU acceleration

The crossover above is the verdict for a **one-off** analysis. It is not the verdict for repeated
work over the same file, and conflating the two got this wrong once. The stateless CPU path
re-reads and re-parses the whole file on every call, while a resident session reads it once. So a
file may benefit from residency on either engine; the historical comparison below is asymmetric.

Measured on the GB10, opening a native GPU session on a small file and running 5 operations
(`profile`, `summary`, `groupby`, `outliers`, `corr`), against the same 5 operations run as 5
stateless CPU calls that each re-read the file:

| Rows | GPU open | GPU per step | CPU, 1 round | CPU, 5 rounds | GPU, 5 rounds | Speedup |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 1,000,000 | 1.499 s | 0.058 s | 2.26 s | 11.17 s | 2.95 s | **3.79×** |
| 2,000,000 | 1.658 s | 0.093 s | 3.45 s | 17.11 s | 3.98 s | **4.29×** |
| 5,000,000 | 2.284 s | 0.187 s | 7.11 s | 35.51 s | 6.95 s | **5.11×** |

Two consequences, and they pull in opposite directions on purpose:

- **One query at 1M rows stays on the CPU.** A single operation pays the ~1.5 s GPU startup
  against 0.57 s of pandas work, so the GPU loses (1.83 s vs 0.57 s). This is what `--engine auto`
  decides, and it is why the default is the CPU.
- **Several queries should avoid rereads on either engine.** New five-repeat fair measurements
  found resident CPU 2.64x faster at 1M rows, and resident GPU 2.92x faster at 20M rows including
  one load (2.46x including startup). The 1.63x compute-only ratio is unstable (GPU CV >10%).
  Raw samples are in `benchmark/resident/README.md` in the repository; default pandas only.

Because of this, `dataset_session` open refuses a small file by default but names the escape
hatch: the refusal carries `suggest_engine`, `route_threshold_rows` and `session_worth_it_if`, and
its hint now recommends `force_cpu=true` for repeated work, reserving `force_gpu=true` for
explicit comparisons. A refusal that does not
say how to proceed is a dead end, and the first version of the guard was one — it told the caller
to switch to `analyze_dataset` even when a session was the better answer.

### Why size, and not memory capacity, is the signal

Two different quantities are easy to confuse here, and only one of them is a routing signal:

- **Data size** — how much work there is. This decides whether the GPU's throughput advantage
  outweighs its fixed cost. It is the axis used above.
- **Memory capacity** — whether the working set fits. This is a *feasibility* constraint, not an
  efficiency curve. It is a cliff: while the data fits, capacity changes nothing, and once it does
  not fit, the fallback is out-of-core chunking with transfers and the advantage collapses. The
  GB10 has 121 GB of unified memory, so nothing measured here came near this cliff.

A controlled experiment confirms which one governs. Holding total bytes at ~600 MB and varying
only the row and cell structure, so that bytes-per-row spans 13x:

| Rows | Cols | B/row | CPU s | GPU s | Speedup |
| ---: | ---: | ---: | ---: | ---: | ---: |
| 12,000,000 | 4 | 66 | 2.73 | 1.97 | **1.39×** |
| 3,000,000 | 12 | 221 | 2.22 | 1.86 | 1.19× |
| 1,500,000 | 24 | 449 | 2.22 | 1.88 | 1.18× |
| 750,000 | 48 | 880 | 2.21 | 1.87 | 1.18× |

At a fixed size the GPU wins by 1.18× to 1.39× across shapes that differ by 13x in row width, and
what shape changes is the *margin*, not the verdict. Also note the GPU total barely moves
(1.97 → 1.86 → 1.88 → 1.87 s) while the CPU gets relatively faster with fewer, longer rows: a wide
row of floats costs the CPU more per byte than a short integer row, which is the small width term
already in the thresholds, and it is a term of a few percent rather than the dominant effect.

### Report language

`make_deliverables.py` writes the report in the caller's language and defaults to **Chinese**.

| Flag | Effect |
| :--- | :--- |
| `--lang zh` / `--lang en` | Force one language, whatever the context says |
| `--lang auto` (default) | Decide from `--lang-context`, then `--title`, then `question`/`goal` in the payload |
| `--lang-context TEXT` | The user's own wording; the best signal for which language they wrote in. `export_deliverables` fills this from the recorded request automatically, so the agent does not re-state the question |
| (no text anywhere) | Chinese, because that is this skill's primary audience |

English and Chinese report structures are asserted to have the same number of sections, so the
two cannot drift apart. A forced `en` report is checked to contain no CJK characters at all.

The long flag is `--input`, not `--file`. Passing `--file` produces an argparse error that
reads like a usage problem rather than a typo, which sends a caller round the same loop.

## Operations

| `--op` | Purpose | Required arguments | Notes |
| :--- | :--- | :--- | :--- |
| `profile` | Row/column counts, dtypes, null counts, distinct counts | — | Always run this first: it is how you learn the real column names |
| `summary` | Per-column statistics (count, mean, std, min, quartiles, max) | — | See the field table below |
| `groupby` | Aggregate by one column | `--by`, `--agg` | `--by` takes **exactly one** column; see below |
| `corr` | Correlation matrix over numeric columns | — | Fails with `Unsupported dtype object` if string columns reach it |
| `outliers` | IQR fence analysis | — | See the field table below |
| `auto` | Several of the above in one pass | — | Its sub-blocks sit at the root of this block rather than one level deeper, and `rows_scanned` appears both at the block root and inside each sub-block |

## Writing a script against the JSON

The stateless tool wraps the engine payload, so operation data is one level down. This shape is
given because guessing it costs a round trip every time:

```
tool result
├── success / engine / rows_scanned / seconds / accelerated     ← wrapper
└── result
    ├── ok / op / engine / accelerated / op_seconds / total_seconds
    └── <op name>                                               ← the operation's own block
```

The block is keyed by the operation name, so `result["summary"]`, `result["outliers"]`,
`result["corr"]`, `result["groupby"]`. Verified field names per block:

| Block | Fields |
| :--- | :--- |
| `summary` | `stats` (**dict**, keyed by column), `columns` (**list** of names), `rows_scanned`, `compute_seconds` |
| `summary.stats[col]` | `count`, `nulls`, `min`, `max`, `mean`, `std`, `median`, `q1`, `q3` |
| `outliers` | `results` (**dict**, keyed by column), `columns` (**list** of names), `rows_scanned`, `compute_seconds` |
| `outliers.results[col]` | `q1`, `q3`, `iqr`, `k`, `lower_bound`, `upper_bound`, `count`, **`pct`**, `examples` |
| `corr` | `pairs` (list), `matrix`, `columns`, `method`, `note`, `rows_scanned`, `compute_seconds` |
| `corr.pairs[i]` | **`a`**, **`b`**, **`corr`**, `abs` |
| `groupby` | `by`, `agg`, `groups` (**int count**), **`top_k`** (the row array), `sorted_by`, `notes`, `rows_scanned`, `compute_seconds` |

Two traps in this table are worth stating twice, because both were got wrong in practice:

- `summary` — `stats` is the dict and `columns` is the list. They are easy to swap, and
  swapping them yields an empty result rather than an error.
- `outliers` — the outlier share is under **`pct`**, and the fences are **`lower_bound`** and
  **`upper_bound`**. `percent`, `lower_fence` and `upper_fence` do not exist.
- `groupby` — `groups` is an **integer count**, not the rows. The rows are in `top_k`.
- `corr` — pair coefficients are under **`corr`**, not `r`.

## `--by` accepts one column

`--by region,category` is rejected. The error names the constraint and the two valid halves:

```
--by column 'region,category' not found; available: [...].
--by takes exactly ONE column, but ['region', 'category'] were joined with a comma.
Run one groupby per column instead: --by region then --by category.
```

Issue one call per dimension. There is no multi-column grouping.

## `--agg` syntax and the functions that exist

Format: `col:func[,func]|col2:func`.

Valid functions: `count`, `size`, `sum`, `mean`, `min`, `max`, `std`, `median`, `nunique`,
`first`, `last`.

Aside worth knowing when working with cuDF directly: aggregation aliases must be **strings** —
passing a callable such as `pd.Series.nunique` is rejected. And cuDF's `std`/`var` use `ddof=0`
where pandas uses `ddof=1`, so cross-engine standard deviations differ by design.

Things callers reasonably ask for that this engine cannot express, and what to do instead:

| Asked for | Why it fails | Do this |
| :--- | :--- | :--- |
| `revenue:corr` (per-group correlation) | group-by has no correlation reducer | `--op corr` for the whole dataset, then `groupby` with `mean`/`std` to compare groups |
| `revenue:var` | not implemented | `std` |
| `revenue:avg` | not implemented | `mean` |
| `revenue:q1` / `quantile` | not implemented in group-by | `--op summary` for quartiles across the dataset |

The error message names the alternative for these cases, so read it rather than re-sending the
same argument.

## Session protocol

One JSON object per line on stdin, one JSON object per line on stdout.

```jsonc
{"cmd": "open",    "path": "/data/sales.csv", "goal": "find the cause of the outliers", "columns": ["a","b"]}
{"cmd": "analyze", "sid": "s1", "op": "groupby", "by": "region", "agg": "revenue:sum"}
{"cmd": "list"}
{"cmd": "close",   "sid": "s1"}          // or {"sid": "all"}
{"cmd": "ping"}
```

Behaviour that matters:

- **`open` on a file that is already open returns the existing session id** rather than loading
  it again, and sets `reused_existing_session: true`. Do not treat a repeated `open` as a fresh
  session; the resident footprint would otherwise double.
- **An error inside a session does not close it.** Change the argument and retry on the same
  `sid`. Closing first leaves only the slow re-reading path.
- **A modified file invalidates the session.** The worker detects this and reports it, because
  answers from a stale frame would be wrong.
- Sessions are capped (`SESSION_MAX`, default 4) and an `open` is refused *before* loading when
  free device memory is short, so an out-of-memory failure never surfaces mid-parse.

Measured on 20M rows / 3.04 GB:

| Quantity | Value |
| :--- | ---: |
| `open` | ~1.9 s |
| Resident | ~1,693 MB |
| `profile` step | ~0.05 s |
| `outliers` step | ~0.4–2.9 s |
| `groupby` step | ~0.03–0.06 s |
| `corr` step | ~0.55 s |
| `summary` step | ~3.4 s (20 quantile passes; the expensive one) |
| Repeat of an already-run op | ~0.027 s (proves no re-read) |

`cumulative_seconds` equals the load time plus the sum of step times exactly.

## Deliverables

`make_deliverables.py` imports only the standard library. It reads an engine result from a file
or `-` for stdin, and writes into `--out-dir`:

- `report.md` — findings, with the "rendering is not GPU-accelerated" caveat in its own text
- `*.svg` — grouped bar, multi-series line, scatter, histogram, correlation heatmap
- `*.csv` / `*.json` — the numbers behind each chart
- a manifest listing what was produced

`build_html_report.py` then reads that directory and writes `report.html`, a single file with the
Markdown rendered and every SVG inlined. Nothing is fetched at view time, so it opens offline, and
the chart markup is inlined rather than linked because a mailed file has no directory beside it for
a relative `<img>` to resolve against. It is also why the page carries the engine decision in a
banner at the top: a reader who receives only this file has no console output to tell them whether
the GPU was used, let alone whether a CPU result was a choice or a fallback.

Identifier-like columns are excluded from value charts, by column name or by the
"min == 0 and mean == max/2" pattern. Without that rule a surrogate key flattens every real
column into an invisible sliver on the axis, which is what happened on the demo data.

A MATLAB-style reverse axis artefact to watch when reading the SVG output: the y-axis is
inverted internally so larger values appear higher. If you are inspecting coordinates by hand,
remember that SVG y grows downward.
