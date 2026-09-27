# Workbench GUI — design spec (rev 2)

**Date:** 2026-09-27
**Status:** Approved in layout review; rev 2 pending written-spec review
**Branch:** `feat/workbench-gui`
**Code baseline:** `origin/main` @ `7d2ef8e` — *"Speed up GPU analytics and add bounded caches and calibrated routing"*
**Mockup:** `../../../../gui-demo/workbench.html` (outside the repo; layout approved 2026-09-27)
**Scope class:** architectural — new subsystem, two small additive backend changes

> **Rebase note.** This branch still sits on the pre-update base `d836c56`; upstream has three
> commits the local tree does not. The branch has one commit (this document), so
> `git rebase origin/main` is a clean replay. All line citations below were taken from
> `origin/main` blobs via `git show`, **not** from the working tree, so they are correct for the
> baseline this spec targets. Run the rebase before implementing.

### What changed between rev 1 and rev 2

Upstream shipped a warm frame cache while this spec was being written. It solves the same
cross-turn-reload problem rev 1's "Change A" solved, differently and better, so **Change A is
deleted** (§5.3 records why). In its place rev 2 adds one small backend helper, because the
upstream `close_all_sessions()` now *retains* frames and therefore cannot implement the GUI's
"release session" button (§5.2, §6.1, §7). Rev 2 also re-points the engine chip at the new
`execution_decision` object instead of the flat fields (§6.3), adds a non-goal about not
re-implementing routing prediction (§2), and updates every line citation.

---

## 1. Problem

The agent speaks through three frontends today: a stateless CLI (`gpu_analytics.py`), an
interactive terminal (`agent/agent_main.py`), and a Textual full-screen app
(`agent/tui_app.py`, 427 lines). All three are text-only and bound to a terminal. For repeated
exploration of one large dataset — the workflow the resident-session engine exists for — a
terminal is the wrong shape: the user wants the chart and the result table visible *beside* the
question, not re-printed under it.

A browser workbench fixes that. The mockup was built and approved before this document, per the
standing rule that interface changes are confirmed visually rather than described.

### Why this does not contradict `build_html_report.py`

That script's docstring argues explicitly against a GUI: a self-contained HTML report needs no
runtime, no server, no network, and "nothing has to be started, so nothing can fail to start on
stage." That argument is correct and remains in force **for the deliverable**. This GUI is not
the deliverable. It is an interactive frontend for the operator, it is optional, and
`report.html` continues to exist unchanged for anyone who needs a file that cannot fail to open.
The two solve different problems and neither replaces the other.

---

## 2. Non-goals

Explicitly out of scope, so nobody builds them by accident:

| Excluded | Reason |
| :--- | :--- |
| Authentication, multi-user, HTTPS | Bound to `127.0.0.1`, single operator, same trust level as the SSH session it rides on |
| Concurrent analyses | The session worker is serialized (§4.1); pretending otherwise would misrepresent the model |
| File upload | Streaming multi-GB CSVs over HTTP contradicts the premise that compute lives next to the data |
| WebSockets | SSE is one-way, survives proxying, needs no reconnect protocol |
| **GUI-side routing, cost prediction, or memory estimation** | `cost_model.py` and the measured-crossover policy own this. The GUI renders `execution_decision.policy` and `estimate.status` as reported and never computes its own. Predicted elapsed time exists only when the local fit validated; `peak_memory_mb` is `null` by design (`gpu_analytics.py:323-348`), and the README is explicit that `admission_required_free_gb` is a safety headroom requirement, not a prediction. A GUI that drew a guessed bar here would break the project's central honesty claim |
| Enabling `--parquet-cache-dir` or `--calibration-file` implicitly | Both have disk side effects — one writes derived copies of the dataset, the other appends measurement rows. Opt-in through the settings screen only, never a default |
| Changes to `gpu_analytics.py`, `cost_model.py`, `parquet_cache.py`, `make_deliverables.py`, `build_html_report.py` | The engine contract is already clean JSON; the GUI is one more consumer of it, not a new kind of consumer |
| Changes to `gpu_session.py` | **One exception, taken during implementation:** `do_list` gained a `resident_mb` field per active session, because §7 requires the card to show held bytes and the worker reported none for an open session (only `warm_cache_mb`, which covers already-closed frames). The helper returns `None` when a frame cannot be measured, so unknown is never rendered as 0. Everything else in the worker is untouched |
| Reworking `tui_app.py` | The TUI stays byte-for-byte compatible, even where it is more fragile — see the regex-on-trace-text note in §5.1 |
| A build step, bundler, or JS framework | See §9 |

---

## 3. Success criteria

1. `python agent/agent_main.py --gui` serves the workbench on `127.0.0.1:8765`, reachable from
   another machine through `ssh -L 8765:127.0.0.1:8765`.
2. Five consecutive questions against one dataset report a warm `execution_decision`
    (`policy`/`observed.phase` per §6.3) from the second turn onward, with per-step
   timings near the ~0.06 s figure in `references/engine-contract.md` — i.e. the file is not
   re-read between turns, **and** this happens with `Agent.run()`'s backstop still executing
   every turn, unchanged.
3. Removing the GUI leaves every existing path working: `agent/tui_test.py` passes unchanged,
   `run_criteria_tests.sh` still reports 11/11, `execution_decision_test.py` and
   `optimization_test.py` still pass, and the engine scripts are untouched.
4. `gui_test.py` runs headless with no API calls and no GPU, matching how `tui_test.py` works.
5. Pressing **释放会话** in the GUI genuinely frees device memory — `gpu_session.py`'s `list`
   afterwards reports zero sessions *and* zero warm frames — which the existing
   `close_all_sessions()` alone does **not** do (§5.2).
6. Terminating the GUI server process releases everything, via the existing `atexit` hook.
7. Zero new third-party dependencies.

---

## 4. Architecture

### 4.1 Process model — why in-process is mandatory

`agent/skills.py:636-637` keeps the session worker in **module-level globals**:

```python
_worker_lock = threading.Lock()
_worker = None
```

`_worker_call()` (`skills.py:676`) holds that lock for the whole request/response round-trip and
reads exactly one NDJSON line back, so calls are serialized. `atexit` registers
`_worker_stop(proc)` (`skills.py:659`), which writes `{"cmd":"close","sid":"all"}` **without**
`retain` before `terminate()` — so process exit already clears the warm cache, free of charge.

Two consequences fix the design:

- The GUI server **must run the agent in-process**. A per-request subprocess model would spawn a
  fresh worker each time and the warm frame (`gpu_session.py:142`) would die with it.
- **One agent instance, one lock.** The server holds a single `Agent` and a `threading.Lock`;
  `ThreadingHTTPServer` threads use them in turn. `busy` is not a queue: a second
  `POST /api/ask` in flight gets `409`, mirroring `tui_app.py:268-269` (`if self.busy: return`).

```
browser (Windows)
   │  http://127.0.0.1:8765   ← ssh -L 8765:127.0.0.1:8765
   ▼
agent/gui.py  ── ThreadingHTTPServer, bind 127.0.0.1
   │  single Agent + agent_lock
   ├─ event_sink  ─┐
   ├─ result_sink ─┴→ job buffer → SSE writer → browser
   │
   └─ agent/skills.py ── subprocess ──→ gpu_session.py worker
                                          ├─ SESSIONS      (active handles)
                                          └─ WARM_CACHE    (bounded, expiring GPU frames)
                                                 │
                                           gpu_analytics.py (cuDF | pandas)
```

### 4.2 New files

| File | Est. lines | Responsibility |
| :--- | ---: | :--- |
| `agent/gui.py` | ~260 | HTTP routing, SSE encoding, job buffer, artifact allow-list. No analysis logic, no session TTL of its own. |
| `agent/gui/index.html` | ~120 | Markup only; structure mirrors the approved mockup. |
| `agent/gui/style.css` | ~200 | Palette taken verbatim from `agent/tui_app.py` CSS. Separate file so the CSP needs no `'unsafe-inline'` (§6.1). |
| `agent/gui/app.js` | ~400 | SSE client, DOM rendering, SVG chart builders, `I18N` chrome table. |
| `agent/gui_test.py` | ~220 | Headless tests with `FakeAgent`. |
| `requirements-gui.txt` | ~8 | Comments only: `# Optional browser workbench. The analytical backend is unchanged.` / `# no dependencies` |

### 4.3 Existing files touched

Two, both additive with unchanged defaults:

- `agent/agent_main.py` — the `result_sink` (§5.1), plus a `--gui` / `--gui-port` pair in `main()`.
- `agent/skills.py` — one ~6-line `release_all_sessions_and_cache()` (§5.2). Nothing else;
  `close_all_sessions()` (`skills.py:761`), `list_datasets()` (`skills.py:1057`) and the worker
  plumbing are called as-is.

Docs updated in the same commit as the implementation, not later: `README.md`, `docs/USAGE.md`,
`skill.md` file table, `references/engine-contract.md` script table.

---

## 5. Backend changes

### 5.1 Change 1: emit tool results as structured events

Inside `_run_inner` (`agent_main.py:554-555`) the raw JSON is in hand but only a text summary
reaches observers:

```python
result, seconds = execute_tool(name, raw)
self.log(f"      {summarize_tool_result(result)}   ({seconds:.2f}s wall)")
```

Add a second sink beside `event_sink` (`agent_main.py:444`, used by `log()` at `460-461`):

```python
def __init__(self, client, verbose=True, event_sink=None, model=None, result_sink=None):
    ...
    self.result_sink = result_sink      # (name: str, parsed: dict, seconds: float) -> None
```

fired after `execute_tool` when the payload parses as JSON. Requirements:

- `event_sink` keeps its current text contract. **`tui_app.py` is not modified.**
- Sink failures must never break a run: wrap the call in a try/except and swallow. Note that the
  guard this line originally pointed at did not exist — `log()` had no exception handling,
  because nothing pluggable into it could fail on its own (Textual's `post_message` is
  thread-safe by design). The guard was added to `log()` in the same change.
- `parsed` passes through unchanged. No renaming, no normalization, no invention of fields the
  engine does not emit.

This hands the GUI `execution_decision` plus `engine`, `accelerated`, `routing_reason`,
`fallback_reason`, `rows_scanned` and `total_seconds` as data rather than as prose to be parsed.

Worth stating plainly: `tui_app.py:338-350` derives the same facts with
`re.search(r'-> ([a-z_]+)\(')` and substring tests for `'engine=cudf'` / `'FALLBACK'`. The GUI
will not inherit that. The TUI is left alone because it works, and unifying the two is a
different change with its own review (§2).

### 5.2 Change 2: a close verb that does not retain

`skills.close_all_sessions()` (`skills.py:761`) now sends `{"cmd":"close","sid":"all","retain":True}`,
and `do_close` in `gpu_session.py:727-738` clears `SESSIONS` while `_retain()`-ing each frame —
`WARM_CACHE` is only cleared when `retain` is absent. So the GUI's **释放会话** button cannot be
wired to `close_all_sessions()`: it would report success while holding up to 4096 MB of GPU
memory. Add the missing verb rather than working around it:

```python
def release_all_sessions_and_cache() -> dict:
    """Close every session AND drop warm frames. Used by the GUI's explicit release."""
    return _worker_call({"cmd": "close", "sid": "all"})
```

`Agent.run()` is **not** touched, so the CLI, the TUI, `demo_script.py` and every existing test
keep their current behaviour byte-for-byte.

### 5.3 Why rev 1's "Change A" was deleted

Rev 1 proposed `run(prompt, keep_sessions=True)` plus a GUI daemon thread with its own 300 s idle
TTL, to stop the per-turn `close_all_sessions()` from throwing away the resident frame. Upstream
solved the same problem in the worker instead, and their design wins on four counts:

| | rev 1 (GUI `keep_sessions`) | upstream `WARM_CACHE` |
| :--- | :--- | :--- |
| Anti-leak guarantee | weakened — deferred to the caller | intact — active handles still close every turn |
| Bound on held memory | time only | `SESSION_WARM_CACHE_MB` (default 4096) **and** `SESSION_WARM_TTL_SECONDS` (default 900), pruned oldest-first (`gpu_session.py:85-86, 165-184`) |
| Which frontends benefit | GUI only | CLI, TUI and GUI alike |
| Where the policy lives | in the new frontend | in the engine, where `list` can report it |

Keeping rev 1's design would have duplicated a weaker version of an existing mechanism and made
the GUI the only frontend with warm reuse. The GUI TTL sweeper is therefore gone, and with it
rev 1's two tests that existed only to police it (its "TTL releases" and its "`keep_sessions`
default"); rev 2's tests #4 and #5 are unrelated checks that happen to reuse those numbers.

---

## 6. Frontend contract

### 6.1 HTTP / SSE

| Method & path | Request | Response |
| :--- | :--- | :--- |
| `GET /` | — | `gui/index.html`; `style.css` and `app.js` as separate same-origin files; CSP `default-src 'self'; style-src 'self'; script-src 'self'; connect-src 'self'; img-src 'self' data:` — no `'unsafe-inline'`, which is why the CSS is not inlined (§4.2) |
| `GET /api/state` | — | `{model, host, dataset, session, plan, busy}`; `session` from the worker's `list` (count + memory use) |
| `POST /api/ask` | `{text, file?}` | `202 {job_id}`; `409` when busy; `400` when `text` empty |
| `GET /api/events?job=ID` | — | `text/event-stream`, `Cache-Control: no-store` |
| `GET /api/files?dir=` | — | parsed `skills.list_datasets(directory)` (`skills.py:1057`) |
| `POST /api/session/release` | — | `release_all_sessions_and_cache()` (§5.2) — **not** `close_all_sessions()`, which retains |
| `PATCH /api/settings` | `{base_url?, model?, api_key?, remember_key?, skip_setup?, language?}` | saved `APIConfig`, **key never echoed back** |
| `GET /artifact?path=` | allow-listed relative path | file bytes with inferred MIME |
| `POST /api/shutdown` | — | `202`, then clean exit (used by tests) |

SSE event names and payloads:

| `event:` | `data:` |
| :--- | :--- |
| `phase` | `{phase: "thinking" \| "tool" \| "summarizing" \| "done" \| "failed"}` |
| `tool_call` | `{round, name, args}` |
| `tool_result` | `{round, name, result, seconds}` — `result` verbatim from `result_sink` |
| `answer` | `{text}` — final model prose, rendered as Markdown |
| `session` | `{sessions, warm_frames, bytes, reused: bool}` |
| `error` | `{message}` |
| `done` | `{seconds}` |

Unknown `job` → `404`, not an infinite stream. A disconnected SSE client must not cancel the
run; the job buffer keeps the last `200` events so a reload can re-attach.

### 6.2 What renders from data, and what stays prose

The i18n boundary was decided during the mockup review and is fixed:

- **Model prose** (`answer`) renders as authored. `/language` does not translate it — the
  documented behaviour at `docs/USAGE.md:41`, unchanged.
- **Numbers, KPI cards and result tables** render from `tool_result` with labels from the chrome
  `I18N` table, so a KPI reads "峰值时均功率 / Peak hourly mean" in the UI language while the
  paragraph under it stays in whatever language the model answered.
- Where the engine emitted no structured field, the GUI shows nothing rather than parsing prose.
  No invented metrics, ever. This includes `estimate.peak_memory_mb`, which upstream always
  reports as `null` (§2).

### 6.3 Engine chip — read the decision record, not the prose

`execution_decision` (`gpu_analytics.py:323-348`, emitted into the envelope at `1127-1133`) is "a
stable, factual decision trace shared by one-off and resident execution" and carries `mode`,
`policy`, `selected_backend`, `actual_backend`, `reason`, `estimate`, `observed`,
`fallback_reason`. That is a better basis than rev 1's flat-field heuristics, and it is the
documented contract, so the chip derives from it:

| Chip | Condition | Style | Note shown |
| :--- | :--- | :--- | :--- |
| `GPU · cuDF` | `actual_backend == "cudf"` and no `fallback_reason` | accent | `policy` (e.g. `measured_file_size_crossover`) |
| `GPU · cuDF (reuse)` | `policy in ("resident_reuse", "warm_cache")` or `observed.phase in ("session_reuse", "warm_cache_hit")` | accent | open cost avoided. **There is no `mode == "resident reuse"`** — that value does not exist in `execution_decision_record`; warmth is recorded in `policy` and `observed.phase`. `resident_reuse` is reachable on the pandas path, `warm_cache` is not (see §12.1) |
| `CPU · pandas (fallback)` | `selected_backend == "cudf"` and `actual_backend == "pandas"` | **warn** | `fallback_reason` verbatim — a GPU was asked for and did not happen |
| `CPU · pandas (by choice)` | `actual_backend == "pandas"` and no `fallback_reason` | neutral | `reason` verbatim — deliberate routing |
| `estimate: not calibrated` | `estimate.elapsed_seconds == null` | muted, on the timing line | shown *only* as the absence of a prediction; never a substituted guess |
| `no result yet` | run started, first `tool_result` not received | muted | never impersonate an engine |

Two inversion risks, both named tests (§10.9):

- Rendering a by-choice CPU run as a fallback contradicts the engine's own comment at
  `gpu_analytics.py:1009-1013` — surfacing a deliberately-routed CPU run as a fallback "would
  violate the public contract and make a healthy decision look broken".
- Rendering a genuine fallback as a choice hides a real problem, and the model would then be told
  a GPU ran.

`selected_backend != actual_backend` is the precise fallback signal; the flat
`fallback_reason != null` heuristic is not equivalent, because the engine also sets it when cuDF
was simply unavailable rather than attempted and failed (`gpu_analytics.py:1087-1088`).

### 6.4 Charts

`app.js` builds SVG from rect/line/text/circle/polyline only — the same primitive budget as
`make_deliverables.py`, which is also why hand-written SVG stays text-diffable. Five types,
matching the deliverable generator: line, bar, heatmap, histogram, scatter. Where
`make_deliverables.py` already produced an SVG for the current dataset, the GUI embeds that
artifact through `/artifact` rather than redrawing it: one renderer per chart, and the workbench
cannot disagree with the exported report.

---

## 7. Session card: report the worker, don't imitate it

The mockup's card implied the GUI owns session state. Under upstream's design it does not, so the
card renders worker-reported facts — the worker's `list` reply already carries count and memory
use — and its states name the two-level reality (`SESSIONS` vs `WARM_CACHE`) precisely:

| State | Meaning | Card |
| :--- | :--- | :--- |
| `cold` | no session, no warm frame; next open pays ~1.5 s | grey |
| `active` | session handle open this turn | green: `steps`, `resident_mb` (added per §2), cumulative seconds |
| `retained` | handle closed at end of turn, frame kept for the next question | green outline + explicit "frame retained, not an open session" — the distinction the release button depends on. **Aggregate only:** `list` reports `warm_cache_count` and `warm_cache_mb`, not which file each frame belongs to, so the card must not name one |
| `released` | explicit release, TTL expiry, or budget eviction | grey + one-line statement of the next-turn cost |

Two limits found by reading the worker rather than this table:

- **`stale` is not a state the card can have.** The worker drops an out-of-date frame silently
  (`_prune_cache`, `gpu_session.py:165-184`) and reports nothing, so there is no field to render.
  Staleness is surfaced only where the worker does say so — when an `analyze` on an open session
  returns "The file changed while this session was open" — and the card relays that verbatim.
- **`list` is not a read-only peek.** `do_list` calls `_prune_cache()` first, so asking the
  question changes the answer: it can expire frames by TTL and evict them by byte budget. The
  card therefore refreshes only after a run or an explicit release. A polling timer would expire
  the very frames it exists to display.

The card also shows the live budget as configured — `SESSION_WARM_CACHE_MB` and
`SESSION_WARM_TTL_SECONDS` — so the operator sees what the engine will actually do rather than
what the GUI assumes. Switching datasets while `active` closes the old session first and says so
in the log drawer.

The GUI runs **no** TTL sweeper of its own (§5.3). Device memory is finite (121 GB on the
reference box) and the 1.7 GB leak that motivated the backstop is still possible, so the card
exists to keep that trade visible rather than hide it behind a second, competing mechanism.

---

## 8. Security

The GUI adds one attack surface the TUI never had: a listening socket. `127.0.0.1` is the default
and the only thing needed for `ssh -L`; binding another interface requires
`GPU_GUI_ALLOW_REMOTE=1`, and without it startup prints a refusal-with-reason.

- **Path traversal.** `/artifact` resolves against a fixed allow-list (the job's out-dir,
  `benchmark/`, `DEMO_DATA_DIR`), then `Path.resolve()` plus `is_relative_to()`. Symlinks that
  escape are rejected. `../../etc/passwd` is a named test case.
- `/api/files?dir=` is **not** a generic file browser; it delegates to `skills.list_datasets()`,
  which applies bounded-depth search roots (`_data_search_roots`, `skills.py:1040`, built on
  `_data_search_dirs()` at `skills.py:1012`). An arbitrary `dir` must still fall inside those.
- **Disk side effects.** `--parquet-cache-dir` writes derived copies of the dataset and
  `--calibration-file` appends measurement rows. Both stay off unless the operator opts in
  through settings, and the chosen directory is echoed back in plain text so the write target is
  unambiguous.
- **Secrets.** Reuse `api_config.save_config()` unchanged: `tempfile` + `os.chmod(0o600)` +
  rename (`api_config.py:96-120`). `APIConfig.api_key` is `field(repr=False)`
  (`api_config.py:16`), so never serialize the dataclass wholesale — build the response dict
  explicitly. The key is never accepted via query string, and SSE frames never carry it.
- **No `innerHTML` from engine output.** `tool_result` values render through `textContent`;
  `answer` Markdown passes an escape-first step, the pattern already set by `_inline` at
  `build_html_report.py:39`.

---

## 9. Dependencies

Zero new runtime dependencies. `gui.py` uses `http.server.ThreadingHTTPServer`, `json`,
`threading`, `queue`, `pathlib`, `mimetypes`, `argparse` — all stdlib. The frontend is plain
ES2019 with no build step.

Rationale, and why not FastAPI: `requirements-tui.txt` frames Textual as an *optional* interface
layer that leaves the analytical backend unchanged, and the engine scripts hand-roll small
renderers rather than adding libraries ("A general Markdown library would be a new dependency for
six constructs", `build_html_report.py:28-29`). Stdlib keeps the GUI on the same axis as the TUI
— optional, degradable, never a reason an analysis cannot run — at a cost of roughly 100 extra
lines of routing. This was offered as option B (FastAPI) against option A, and A was chosen. If
the workbench later grows authentication or concurrency, revisit then rather than pre-paying now.

**Degradation:** a missing `gui.py` or an occupied port must never affect `agent_main.py`,
`tui_app.py` or the engine; `gui.py` is imported only inside the `--gui` branch.

---

## 10. Testing

`agent/gui_test.py` — headless, no network, no GPU, no API calls, temporary CSV fixtures,
following `tui_test.py`'s `FakeAgent` pattern. `gui.py` is plain HTTP, so tests drive it with
`urllib.request` against an ephemeral port (`port=0`), not a browser.

1. **SSE ordering** — `phase → tool_call → tool_result → answer → done`, `done` last.
2. **Busy → 409** on a second `POST /api/ask`.
3. **Path traversal rejected** — `../../etc/passwd`, absolute `/etc/passwd`, and a symlink out of
   the allow-list all return `403`/`404` and read no bytes.
4. **Release really frees** — after `POST /api/session/release`, the worker's `list` reports zero
   sessions **and** zero warm frames. This is the test that catches a `close_all_sessions()`
   wiring mistake (§5.2), which a weaker check would pass.
5. **Warm reuse is reported, not claimed** — with a stubbed worker, assert the card shows
   `retained`/`active` from the payload rather than from GUI bookkeeping, and that the §6.3
   `policy`/`observed.phase` test drives the chip.
6. **`result_sink` cannot break a run** — raise inside the sink, assert the answer still returns.
7. **`/api/settings` does not echo the key** and persists `0o600`.
8. **Artifact MIME and CSP headers present**; no response carries `'unsafe-inline'`.
9. **Chip mapping** — feed `actual_backend="cudf"`; `selected_backend="cudf"` with
   `actual_backend="pandas"`; and `actual_backend="pandas"` with no `fallback_reason`; assert all
   three render differently (§6.3). A test that only checks "GPU shows GPU" would not catch the
   inversion.
10. **Estimate absence** — with `estimate.elapsed_seconds == null`, assert no predicted number or
    bar appears anywhere (§2, §6.2).
11. **Regression gate** — `tui_test.py`, `run_criteria_tests.sh` (11/11),
    `execution_decision_test.py` and `optimization_test.py` all pass with the GUI merged.

Frontend logic gets DOM-probe assertions only — the way the mockup was verified: every chart tab
renders, and the language round-trip leaves model prose untouched. No pixel tests.

---

## 11. Decision log

| # | Decision | Chosen | Rejected | Why |
| :--- | :--- | :--- | :--- | :--- |
| 1 | GUI form factor | localhost web server + `ssh -L` | Electron/Tauri desktop | Compute lives with the data on GB10; a desktop app ships no cuDF and would stream GB-scale CSVs over the network |
| 2 | TUI | untouched | rebuild/unify under GUI | mature, and 6 of the 10 preceding commits polished it; a rewrite buys nothing |
| 3 | Server | stdlib `http.server` | FastAPI, Streamlit | zero new deps, parity with the TUI's optional-layer status |
| 4 | Streaming | SSE | WebSocket, polling | one-way, proxy-safe, no reconnect protocol |
| 5 | Concurrency | single user, `409` | job queue | `_worker_call` holds a lock and reads one line — serialized by construction |
| 6 | Cross-turn warmth | **upstream `WARM_CACHE`** (rev 2) | rev 1's `keep_sessions` + GUI TTL | upstream bounds by bytes *and* time, keeps the anti-leak backstop intact, and serves every frontend (§5.3) |
| 7 | Release verb | new `release_all_sessions_and_cache()` | reusing `close_all_sessions()` | it passes `retain: True`, so it would "succeed" while holding up to 4096 MB (§5.2) |
| 8 | i18n boundary | chrome + numbers translated, prose not | translate everything | matches `docs/USAGE.md:41`; translating model output in the frontend would fabricate a translation |
| 9 | Charts | hand-written SVG / embed the artifact | matplotlib, Chart.js | one renderer, text-diffable, no dependency |
| 10 | Engine state source | `result_sink` → `execution_decision` | regex on trace text; rev 1's flat-field heuristics | the decision record is the documented stable contract and separates choice from fallback exactly (§6.3) |

## 12. Assumptions (confirm or override during spec review)

1. Reference hardware is the documented GB10 (aarch64, driver 580.126.09, 121 GB, cuDF 25.10.00);
   port `8765` is free. **On a no-GPU box, cross-turn warmth does not happen at all.** An earlier
   revision of this document claimed "resident reuse still occurs on pandas frames"; that is not
   what the code does. `_retain()` (`gpu_session.py:186-188`) returns `False` unless
   `sess.engine.is_gpu`, and `WARM_CACHE` is only ever inserted there, so a pandas frame never
   enters the cache. What *is* reachable on the pandas path is `policy == "resident_reuse"` —
   reusing a session handle that is still open within a turn. So the acceptance run on a machine
   without cuDF is: `engine="pandas"` reported honestly, `resident_reuse` observable across
   steps of one session, and the `warm_cache` / `retained` states rendered as explicitly
   unobservable rather than substituted. The ~4× claim does not apply there.
2. Upstream's defaults (`SESSION_WARM_CACHE_MB=4096`, `SESSION_WARM_TTL_SECONDS=900`) are
   acceptable for the workbench; the GUI surfaces them rather than overriding them.
3. One operator, one browser tab. A second tab shares the single `Agent` and can observe events —
   a property, not a feature.
4. The configured model supports Chat Completions with tool calling; being listed by `/models`
   does not establish that (`docs/USAGE.md:116`).
5. `--gui` implies an interactive run; `--ask` and piped stdin stay non-interactive and refuse to
   start a server.
6. The mockup is a throwaway artifact and is not copied into the repo; `gui/index.html` starts
   from its structure but is written against the real `execution_decision` shapes.
7. `git rebase origin/main` has been run before implementation begins.

## 13. Risks

| Risk | Blast radius | Mitigation |
| :--- | :--- | :--- |
| The release button silently retains frames | operator believes 4 GB was freed; every later turn slows | §5.2 helper + test #4 asserting zero warm frames |
| A listening port becomes a real exposure if someone binds `0.0.0.0` | secrets and arbitrary file reads | default `127.0.0.1`, explicit opt-out env var with startup refusal, tests #3/#7/#8 |
| Model prose and rendered numbers drift apart | the "exact, not hallucinated" premise | numbers come only from `tool_result`; nothing is computed in the frontend |
| GUI re-implements routing or estimates | contradicts `cost_model.py`, shows predictions that were never validated | §2 non-goal, §6.2 "shows nothing", test #10 |
| Long tool call stalls the SSE writer | browser looks dead | `phase` heartbeat each 1 s from the run thread; bounded by the engine's own 1800 s worker timeout |
| Upstream keeps moving under this spec | citations and contracts drift again | rebase before implementing (assumption 7); re-verify §5/§6.3/§7 citations if `origin/main` has advanced |

---

## 14. Build order (input to writing-plans)

1. Rebase `feat/workbench-gui` onto `origin/main`; confirm the §5/§6.3/§7 citations still resolve.
2. `result_sink` + test #6 — small, isolated, unblocks the frontend.
3. `release_all_sessions_and_cache()` + test #4.
4. `gui.py` routing, job buffer, SSE — tests #1, #2, #8.
5. `/artifact` allow-list + `/api/files` — test #3.
6. `gui/index.html` + `style.css` + `app.js` from the approved mockup — DOM probes, tests #5, #9, #10.
7. `/api/settings` via `api_config` — test #7.
8. `--gui` / `--gui-port` in `main()`, `requirements-gui.txt`.
9. Docs: `README.md`, `docs/USAGE.md`, `skill.md`, `references/engine-contract.md`.
10. Regression gate #11 on GB10 *and* on a no-GPU machine (the pandas path must report honestly).
