# Independent client / installed skill verification

A separate Codex CLI instance ran in an isolated Windows project containing only the installed
`.agents/skills/cudf-analytics`, fixture.csv, and a host Python path. No repository `agent/*.py`
code, expected rankings, API keys or intended answers were supplied. The prompt did not name
the skill; the client selected it automatically, read SKILL.md and executed its installed scripts.

Request: analyze every fixture row, rank regions by total and mean revenue, state the full row
count, and export a shareable chart/report. The actual tool output AND exported CSV were graded
independently using Python's standard-library csv module, not only the model's final answer.

Expected from raw data: West 600 / 200 mean, East 400 / 133.333333 mean, North 200 / 100 mean.
All eight rows were used. CPU pandas 3.0.1 was selected deliberately; this is not GPU validation.
The standalone HTML contains inline SVG and no external web references.

```sh
python tools/install_skill.py --project /path/to/isolated-case
# Supply a valid Python runtime and fixture.csv, then run a separate CLI on that directory.
codex exec --ephemeral --skip-git-repo-check --json -C /path/to/isolated-case \
  'Rank regions by total and mean revenue using all fixture.csv rows; export a chart and report.'
python tools/verify_portability.py --workspace /path/to/isolated-case \
  --trace trace.jsonl --out verification.json
```

Execution requires normal client permissions. The first restrictive-policy attempt failed;
the verified retry used workspace approval mode. WebSocket timed out then HTTPS fallback worked.
Raw traces remain locally outside the public repository because they include machine-specific
paths and client context. [Sanitized verification](verification.json), [fixture](fixture.csv),
[checked CSV](groupby.csv), [HTML report](report.html) are retained here.

Scope: one genuine independent Codex CLI invocation, not every Agent framework. Claude installation
layout is supported by the installer but actual Claude execution remains untested.
