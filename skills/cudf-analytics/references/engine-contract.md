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
| `analysis_plan.py` | Plan catalog and progress bookkeeping. Imported, not run directly. | `import analysis_plan` |

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

Identifier-like columns are excluded from value charts, by column name or by the
"min == 0 and mean == max/2" pattern. Without that rule a surrogate key flattens every real
column into an invisible sliver on the axis, which is what happened on the demo data.

A MATLAB-style reverse axis artefact to watch when reading the SVG output: the y-axis is
inverted internally so larger values appear higher. If you are inspecting coordinates by hand,
remember that SVG y grows downward.