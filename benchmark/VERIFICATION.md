# Competition-evidence verification

Completed on Windows (pandas 3.0.1) and NVIDIA GB10 (pandas 2.3.3, cuDF 25.10).

- Fair engine benchmark: five repeats per engine per input, 20 native-operation samples total;
  floating-point summaries, group aggregates and correlations agree within disclosed tolerance;
  integer row/outlier counts agree exactly. Raw JSON and variability remain visible.
- Real demo: official UCI archive, 2,075,259 untouched rows, source hashes and license;
  all 24 hourly means/counts checked against an independent full-data reference. The corrected
  Agent's 24 displayed values and counts also agree with the CSV (means to displayed precision).
- Independent Codex CLI: implicit installed-skill discovery, actual engine output and exported
  CSV independently checked, CPU routing honestly stated, standalone report has inline charts.
- Tool schema contract: 11 checks passed. Deterministic planning: 65 passed, zero failed.
- CPU smoke suite and GB10 `--require-gpu` smoke suite passed; GPU means/medians/max verified.
- Group coverage regression passed on Windows and GB10: all 24 groups, exact global minimum,
  explicit top-5 preserved with coverage warning. Time-series labels span both endpoints.
- GB10 dataset-session tool-layer suite passed using an absolute dataset path. An initial run
  with a basename failed its direct-worker memory test because the worker does not share the
  tool adapter's parent-directory resolver; it is not counted as a pass.
- Skill frontmatter validator passed after removing unsupported whenToUse and shortening the
  description. Validator dependencies are temporary, not part of the skill or submission.

Limitations remain: default pandas baseline, warm CSV cache, one GB10, selected workloads;
compute variability >10% is flagged. No claim of optimized CPU comparison, Claude execution,
causality, energy savings, or generalization beyond the single household. Initial execution
policy, missing CUDA-header environment, and truncated-group failures were retained privately
and corrected, not silently counted as successful runs.
