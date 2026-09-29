# Competition-evidence verification

Completed on Windows (pandas 3.0.1) and NVIDIA GB10 (pandas 2.3.3, cuDF 25.10).

Every count below is labelled with the machine it came from, because a suite that needs a device
cannot report the same thing on a laptop. See [How to read these numbers](#how-to-read-these-numbers).

## CPU-only re-verification (2026-09-27, no GPU, no cuDF, no API calls)

Python 3.13.9, pandas 2.3.3, numpy 2.3.5, `import cudf` failing by design. Run from the repo root.

| Suite | Exit | Result | Device-only assertions |
| :--- | :---: | :--- | :--- |
| `skills/cudf-analytics/scripts/smoke_test.py` | 0 | 86 `[PASS]`, 0 failed | 1 cross-check skipped, reason printed |
| `skills/cudf-analytics/scripts/plan_test.py` | 0 | 65 `[PASS]`, 0 failed | none |
| `agent/tool_contract_test.py` | 0 | 11 `[PASS]`, 0 failed | none |
| `agent/session_skill_test.py` | 0 | 55 `[PASS]`, 0 failed | 1 skipped: `engine is cudf` |
| `skills/cudf-analytics/scripts/session_worker_test.py` | 0 | 27 `[PASS]`, 0 failed | 2 skipped: engine identity, `resident_mb` |
| `skills/cudf-analytics/scripts/execution_decision_test.py` | 0 | 1 aggregate PASS: automatic, forced, fallback-capable and resident decision paths | forced-GPU path is exercised against a mocked engine, so it is asserted here rather than skipped |
| `skills/cudf-analytics/scripts/optimization_test.py` | 0 | `Ran 4 tests ... OK` | none |
| `agent/group_coverage_test.py` | 0 | 1 aggregate PASS: 24-group expansion, exact minimum, explicit top-5 | none |
| `agent/tui_test.py` | 0 | SKIP | Textual is the optional layer (`requirements-tui.txt`); it reports SKIP and exits 0 instead of failing a machine that has no full-screen UI |
| `agent/api_config_test.py` | 2 | not run | `openai` is a runtime dependency (`requirements.txt`), not an optional one. It now prints the install command instead of a traceback |

The two session suites generate a small fixture locally when no dataset is supplied, so the whole
lifecycle, the no-re-read accounting, the staleness guard and the memory-guard *logic* are all
asserted on a laptop. The memory guard is tested by fixing the reading it acts on
(`_free_gpu_gb`), which is the same arithmetic that protects a real box; on CPU-only hardware the
real reading is unavailable and the guard would otherwise be inert and unverified.

Skipped assertions are printed as `SKIP` with the reason and are never recorded as PASS. Under
`--require-gpu` each of them turns into a failure naming cuDF as the cause, so a GPU box cannot
quietly lose the device coverage.

## How to read these numbers

Two counts in this file look wrong if you grep the source, and both are correct:

- **11 tool-contract checks**, not 5. `agent/tool_contract_test.py` contains five `check(` call
  sites; two of them sit inside a loop over the four registered tools, so the run prints 8 + 3 = 11.
  `grep -c 'check('` also matches `def check(` and undercounts by one more.
- **30 graded eval checks**, not 26. `skills/cudf-analytics/BENCHMARK.md` lists a per-item
  fraction; its 13 numerators sum to 30. Reading the denominators instead gives the same total.

A suite's *static* `check(` count, its *runtime* PASS count and a committed log from a different
machine are three different quantities. That is why the skill docs quote no fixed number for
`smoke_test.py` and point at `grep -c '[PASS]'` instead.

## Earlier evidence (GB10 and Windows, not reproduced on this laptop)

- Fair engine benchmark: five repeats per engine per input, 20 native-operation samples total;
  floating-point summaries, group aggregates and correlations agree within disclosed tolerance;
  integer row/outlier counts agree exactly. Raw JSON and variability remain visible.
- Real demo: official UCI archive, 2,075,259 untouched rows, source hashes and license;
  all 24 hourly means/counts checked against an independent full-data reference. The corrected
  Agent's 24 displayed values and counts also agree with the CSV (means to displayed precision).
- Independent Codex CLI: implicit installed-skill discovery, actual engine output and exported
  CSV independently checked, CPU routing honestly stated, standalone report has inline charts.
- Tool schema contract: 11 checks passed. Deterministic planning: 65 passed, zero failed.
  Both re-confirmed CPU-only on 2026-09-27, above.
- GB10 `--require-gpu` smoke suite passed; GPU means/medians/max verified. **Still GB10-only** —
  this laptop cannot run it, and the committed `smoke/gb10_smoke_test.log` predates later changes
  to the suite, so treat its 59 as a historical artifact rather than the current count.
- Group coverage regression passed on Windows and GB10: all 24 groups, exact global minimum,
  explicit top-5 preserved with coverage warning. Time-series labels span both endpoints.
- GB10 dataset-session tool-layer suite passed using an absolute dataset path. An initial run
  with a basename failed its direct-worker memory test because the worker does not share the
  tool adapter's parent-directory resolver; it is not counted as a pass.
- Skill frontmatter validator passed after removing unsupported whenToUse and shortening the
  description. Validator dependencies are temporary, not part of the skill or submission.

## Still to re-run on GB10

`--require-gpu` across `smoke_test.py`, `session_skill_test.py` and `session_worker_test.py`
against the 20M-row demo dataset; the real device-memory refusal and warm-frame eviction rather
than their mocked readings; `run_criteria_tests.sh` 11/11 under cuDF; and every speedup figure in
`BENCHMARK.md`, `README.md`, `references/engine-contract.md` and `SKILL.md`.


Limitations remain: default pandas baseline, warm CSV cache, one GB10, selected workloads;
compute variability >10% is flagged. No claim of optimized CPU comparison, Claude execution,
causality, energy savings, or generalization beyond the single household. Initial execution
policy, missing CUDA-header environment, and truncated-group failures were retained privately
and corrected, not silently counted as successful runs.
