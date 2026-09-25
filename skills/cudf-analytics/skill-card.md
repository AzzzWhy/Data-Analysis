## Description: <br>
Use when analysing a large local tabular data file (CSV, Parquet, TSV, JSONL, Excel) for statistics, profiling, group-by aggregation, correlation, or outlier detection, especially at million-row to multi-gigabyte scale where pandas is slow or runs out of memory; and when the user wants to keep the result as a report, chart, or data export. <br>

This skill is for research and development only. It is a hackathon submission, not a product. <br>

## Third-Party Community Consideration
This skill is not owned or developed by NVIDIA. This skill has been developed and built to a third-party's requirements for this application and use case; see link to Non-NVIDIA [Data-Analysis Agent contributors Agent Card](https://github.com/AzzzWhy/Data-Analysis). <br>

### License/Terms of Use: <br>
MIT <br>
## Use Case: <br>
Developers and data practitioners who need exact statistics over a dataset too large to load into a language model's context, and who need the analysis to end in a file they can hand to someone else. The skill computes aggregates on the GPU with cuDF (pandas fallback when no GPU is present) rather than letting a model estimate numbers, keeps a dataset resident across multiple analysis steps, attaches a multi-step plan to the session so progress is system state rather than prompt text, and writes a Markdown report with SVG charts and CSV exports. <br>

### Deployment Geography for Use: <br>
Global <br>

## Requirements / Dependencies: <br>
**Requires API Key or External Credential:** [Yes, for the reference agent only] <br>
**Credential Type(s):** [LLM provider API key] <br>

The skill itself requires no credential: it reads a local file and computes. The bundled reference agent (`agent/agent_main.py`) calls a hosted model, so it needs one API key in its own environment. That key is read from the environment and is never written to outputs.

Do not include secrets in prompts/logs/output; use least-privilege credentials; rotate keys as appropriate. <br>

## Known Risks and Mitigations: <br>
Risk: The skill reports GPU speedup figures, and a workflow-level multiplier includes the benefit of the dataset already being resident in memory. Quoted without that context the number overstates GPU compute throughput. <br>
Mitigation: Every measured multiplier carries its caveat in the same payload. Attribution testing in this repository found that only about 2.9x-3.1x of the end-to-end gain comes from in-memory GPU compute, with the rest from parallel parsing that a CPU library could also do; that finding is documented in the README and repeated in the system prompt so the agent restates it to the user. <br>

Risk: A session holds roughly 1.7 GB of device memory per 20M rows, so an unreleased session can exhaust memory across a long conversation. <br>
Mitigation: Sessions are capped at 4, refused before loading when free memory is short, reused rather than duplicated when the same file is opened twice, and released by a `finally` backstop if the model forgets to close. <br>

Risk: Correlation output could be read as a causal claim. <br>
Mitigation: The skill's instructions state that correlation is not causation and the boundary is declared in SKILL.md rather than left to the model's discretion. <br>

Risk: Chart rendering is CPU-only, and a report containing charts could be read as GPU-accelerated end to end. <br>
Mitigation: The generated report states in its own text that rendering is not GPU-accelerated and that the GPU accelerated the aggregation behind the chart. <br>

Risk: Chart axes can be distorted by surrogate key columns, making real columns appear flat. <br>
Mitigation: Identifier-like columns are excluded from value charts by a documented rule; this was found on the demo data, where a `row_id` mean of 10,000,000 flattened every other column. <br>

## Reference(s): <br>
- [Skill instructions](SKILL.md) <br>
- [Benchmark report](BENCHMARK.md) <br>
- [Evaluation dataset](evals/evals.json) <br>
- [Repository README](https://github.com/AzzzWhy/Data-Analysis) <br>

## Skill Output: <br>
**Output Type(s):** [Analysis, Files] <br>
**Output Format:** [JSON, Markdown, SVG, CSV] <br>
**Output Parameters:** [1D] <br>
**Other Properties Related to Output:** [Engine results are JSON on stdout with metadata at the top level and operation data nested under its own key, so a caller can tell whether the GPU path was actually taken. Deliverables are written to a directory and the paths are reported back.] <br>

## Evaluation Agents Used: <br>
- DeepSeek Harness (`dsh`) driving `step-3.7-flash` <br>

## Evaluation Tasks: <br>
Evaluated locally against 13 tasks (5 positive, 3 negative, 5 safety) defined in `evals/evals.json`, using the trace-based runner `evals/run_evals.py`. This is a local harness, not NVSkills-Eval, and no Tier-3 external-profile run has been performed. <br>

## Evaluation Metrics Used: <br>
Reported benchmark dimensions: <br>
- Correctness: Checks whether the agent selects the skill, uses the right operation and arguments, and reports numbers computed over the full dataset rather than estimates. <br>
- Discoverability: Checks whether the agent loads the skill when a data file is involved and avoids it when the question is conceptual or arithmetic. <br>
- Effectiveness: Checks whether the agent completes the multi-step workflow, including releasing the session. <br>
- Efficiency: Checks whether the agent keeps one resident session instead of re-reading a 3 GB file per step, and avoids repeating a rejected call. <br>
- Safety: Checks that a missing file or column is surfaced rather than silently substituted, that no credential reaches the output, and that memory is released. <br>

Underlying evaluation signals used in this run: <br>
- `skill_execution`: which skill ran, with which arguments, read from the tool-call trace. <br>
- `skill_efficiency`: duplicate `open` calls, comma-joined group-by arguments, repeated identical failing calls. <br>
- `accuracy`: whether the reported row counts equal the file's real row count, and whether a failed lookup was followed by fabricated statistics. <br>
- `goal_accuracy`: whether the requested deliverable files exist and their paths are named in the answer. <br>
- `behavior_check`: session closed or explicitly released by the backstop. <br>
- `security`: credential-shaped strings in the transcript, answer, or generated report. <br>

## Evaluation Results: <br>

Local trace-based results, 2026-09-25, on GB10 with a 20M-row / 3.04 GB CSV: <br>
| Dimension | Tasks | Result |
|---|---:|---|
| Positive (skill must be used) | 5 | see BENCHMARK.md |
| Negative (skill must NOT be used) | 3 | 3/3 pass |
| Safety (failure handling) | 5 | see BENCHMARK.md |

Per-check totals and any failing checks are recorded in `BENCHMARK.md`. Dimension percentages in the NVSkills-Eval sense are deliberately **not** reported here, because producing them requires that harness. <br>

## Testing Completed: <br>
**[ ] Agent Red-Teaming** <br>
**[ ] Network Security** <br>
**[ ] Product Security** <br>

## Skill Version(s): <br>
`f24c8ae` (source: git SHA, committed 2026-09-25) <br>