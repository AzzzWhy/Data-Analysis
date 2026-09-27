# Workbench GUI — design spec

**Date:** 2026-09-27
**Status:** Approved in layout review; pending written-spec review
**Branch:** `feat/workbench-gui`
**Mockup:** `../../../../gui-demo/workbench.html` (outside the repo; layout approved 2026-09-27)
**Scope class:** architectural — new subsystem, two additive changes to existing interfaces

---

## 1. Problem

The agent today speaks through three frontends: a stateless CLI (`gpu_analytics.py`), an
interactive terminal (`agent/agent_main.py`), and a Textual full-screen app
(`agent/tui_app.py`, 427 lines). All three are text-only and all three are bound to a
terminal. For repeated exploration of one large dataset — the workflow the resident-session
engine was built for — a terminal is the wrong shape: the user wants the chart and the
result table visible next to the question, not re-printed below it.

A browser workbench fixes that. The mockup was built first and approved before this document,
per the standing rule that interface changes are confirmed visually rather than described.

### Why this does not contradict `build_html_report.py`

That script's docstring argues explicitly against a GUI: a self-contained HTML report needs
no runtime, no server, no network, and "nothing has to be started, so nothing can fail to
start on stage." That argument is correct and remains in force **for the deliverable**. This
GUI is not the deliverable. It is an interactive frontend for the operator, it is optional,
and `report.html` continues to exist unchanged for anyone who needs a file that cannot fail
to open. The two solve different problems and neither replaces the other.

---

## 2. Non-goals

Explicitly out of scope, so nobody builds them by accident:

| Excluded | Reason |
| :--- | :--- |
| Authentication, multi-user, HTTPS | Bound to `127.0.0.1`, single operator, same trust level as the SSH session it rides on |
| Concurrent analyses | The session worker is serialized (§4.1); pretending otherwise would corrupt the model we sell |
| File upload | Streaming multi-GB CSVs over HTTP contradicts the premise that compute lives next to the data |
| WebSockets | SSE is one-way, survives proxying, and needs no reconnect protocol |
| Changes to `gpu_analytics.py`, `gpu_session.py`, `make_deliverables.py`, `build_html_report.py` | The engine contract is already clean JSON; the GUI is one more consumer of it, not a new kind of consumer |
| Reworking `tui_app.py` | The TUI stays byte-for-byte compatible. It is not folded into this design even where it is more fragile — see the regex-on-trace-text note in §5.2 |
| A build step, bundler, or JS framework | See §9 |

---

## 3. Success criteria

1. `python agent/agent_main.py --gui` serves the workbench on `127.0.0.1:8765` and a browser
   on another machine reaches it through `ssh -L`.
2. Five consecutive questions against one dataset report `engine="cudf"` and per-step timings
   near the resident figure already documented in `references/engine-contract.md` (~0.06 s per
   step), i.e. the file is **not** re-read between turns.
3. Removing the GUI leaves every existing path working: `agent/tui_test.py` passes unchanged,
   `run_criteria_tests.sh` still reports 11/11, and `gpu_analytics.py` is untouched.
4. `gui_test.py` runs headless with no API calls and no GPU, matching how `tui_test.py` works
   today.
5. Terminating the GUI server process releases any resident session, proven by
   `gpu_session.py` reporting zero open sessions afterwards.
6. Zero new third-party dependencies.

---

## 4. Architecture

### 4.1 Process model — why in-process is mandatory

`agent/skills.py:636-637` keeps the session worker in **module-level globals**:

```python
_worker_lock = threading.Lock()
_worker = None
```

`_worker_call()` (`skills.py:676`) holds that lock for the whole request/response round-trip
and reads exactly one NDJSON line back, so calls are serialized. `atexit` registers
`_worker_stop(proc)` (`skills.py:659`), which writes `{"cmd":"close","sid":"all"}` before
`terminate()`.

Two consequences fix the design:

- The GUI server **must run the agent in-process**. A per-request subprocess model would spawn
  a fresh worker each time, and the resident session — the source of the 3.79×–4.29× speedup
  in `engine-contract.md` — would evaporate.
- **One agent instance, one lock.** The server holds a single `Agent` and a `threading.Lock`;
  `ThreadingHTTPServer` threads use them in turn. `busy` is not a queue: a second `POST /api/ask`
  while a run is in flight gets `409`, mirroring `tui_app.py:268-269` (`if self.busy: return`).

```
browser (Windows)
   │  http://127.0.0.1:8765   ← ssh -L 8765:127.0.0.1:8765
   ▼
agent/gui.py  ── ThreadingHTTPServer, bind 127.0.0.1
   │  single Agent + agent_lock
   ├─ event_sink  ─┐
   ├─ result_sink ─┴→ job queue → SSE writer thread → browser
   │
   └─ agent/skills.py ── subprocess ──→ gpu_session.py worker (NDJSON, resident frames)
                                              │
                                        gpu_analytics.py (cuDF | pandas)
```

### 4.2 New files

| File | Est. lines | Responsibility |
| :--- | ---: | :--- |
| `agent/gui.py` | ~280 | HTTP routing, SSE encoding, job state machine, artifact whitelist, TTL sweeper. No analysis logic. |
| `agent/gui/index.html` | ~120 | Markup only. Structure mirrors the approved mockup. |
| `agent/gui/style.css` | ~200 | Palette taken verbatim from `agent/tui_app.py` CSS. Separate file so the CSP needs no `'unsafe-inline'` (§6.1). |
| `agent/gui/app.js` | ~380 | SSE client, DOM rendering, SVG chart builders, `I18N` chrome table. |
| `agent/gui_test.py` | ~200 | Headless tests with `FakeAgent`. |
| `requirements-gui.txt` | ~8 | Comments only: `# Optional browser workbench. The analytical backend is unchanged.` / `# no dependencies` |

### 4.3 Existing files touched

Only two, and both changes are additive with unchanged defaults:

- `agent/agent_main.py` — change A (§5.1) and change B (§5.2), plus a `--gui` flag and
  `--gui-port` in `main()` (`agent_main.py:581`).
- `agent/skills.py` — no code change; `close_all_sessions()` (`skills.py:761`) and
  `list_datasets()` (`skills.py:1054`) are called as-is.

Docs updated in the same commit as the implementation, not later: `README.md`, `docs/USAGE.md`,
`skill.md` file table, `references/engine-contract.md` script table.

---

## 5. The two backend changes

### 5.1 Change A — make the session backstop defeasible

`agent_main.py:485-499`:

```python
def run(self, user_query: str) -> str:
    """Answer one question. Any session left open is released on the way out."""
    ...
    finally:
        # Structural guarantee rather than a prompt request. ... Observed once in
        # practice, so this is enforced in code.
        released = skills.close_all_sessions()
```

The backstop is deliberate and the comment records a real 1.7 GB leak. It must not be deleted.
It becomes conditional:

```python
def run(self, user_query: str, keep_sessions: bool = False) -> str:
```

with the `finally` block skipped when `keep_sessions` is true, and the log line replaced by one
stating that release was deferred to the caller. Defaults preserve current behaviour exactly,
so the CLI, the TUI, `demo_script.py` and the criteria suite are unaffected.

**Leak prevention moves to the GUI**, with three independent triggers, any of which releases:

1. `POST /api/session/release` — the button in the mockup.
2. Idle TTL — a daemon thread releases when no turn has run for `GPU_GUI_SESSION_TTL` seconds
   (default `300`). Setting it to `0` disables auto-release, and then `gui.py` must log a
   loud startup warning saying the operator now owns session cleanup entirely.
3. Process exit — already free via `atexit` in `skills.py:659`.

The GUI owns `keep_sessions=True` and therefore owns the cleanup obligation.

### 5.2 Change B — emit tool results as structured events

Inside `_run_inner` (`agent_main.py:555-556`) the raw JSON is in hand but only a text summary
reaches observers:

```python
result, seconds = execute_tool(name, raw)
self.log(f"      {summarize_tool_result(result)}   ({seconds:.2f}s wall)")
```

Add a second sink beside `event_sink` (`agent_main.py:444`, used by `log()` at `460-461`):

```python
def __init__(self, client, verbose=True, event_sink=None, model=None, result_sink=None):
    ...
    self.result_sink = result_sink          # (name: str, parsed: dict, seconds: float) -> None
```

fired after `execute_tool` when the payload parses as JSON. Requirements:

- `event_sink` keeps its current text contract. **`tui_app.py` is not modified.**
- `result_sink` failures must never break a run: wrap the call in the same try/except that
  protects `log()`, and swallow.
- `parsed` is passed through unchanged. No field renaming, no normalization, no invention of
  fields the engine does not emit.

This gives the GUI `engine`, `accelerated`, `routing_reason`, `fallback_reason`, `rows_scanned`
and `total_seconds` as data instead of as prose to be parsed.

---

## 6. Frontend contract

### 6.1 HTTP / SSE

| Method & path | Request | Response |
| :--- | :--- | :--- |
| `GET /` | — | `gui/index.html`, with `style.css` and `app.js` served as separate same-origin files and CSP `default-src 'self'; style-src 'self'; script-src 'self'; connect-src 'self'; img-src 'self' data:` — no `'unsafe-inline'`, which is why the CSS is not inlined (§4.2) |
| `GET /api/state` | — | `{model, host, dataset, session: {open, turns, bytes}, plan, busy}` |
| `POST /api/ask` | `{text, file?}` | `202 {job_id}`; `409` when busy; `400` when `text` empty |
| `GET /api/events?job=ID` | — | `text/event-stream`, `Cache-Control: no-store` |
| `GET /api/files?dir=` | — | parsed `skills.list_datasets(directory)` |
| `POST /api/session/release` | — | `close_all_sessions()` verbatim |
| `PATCH /api/settings` | `{base_url?, model?, api_key?, remember_key?, skip_setup?, language?}` | saved `APIConfig`, **key never echoed back** |
| `GET /artifact?path=` | allow-listed relative path | file bytes with inferred MIME |
| `POST /api/shutdown` | — | `202`, then clean exit (used by tests) |

SSE event names and payloads:

| `event:` | `data:` |
| :--- | :--- |
| `phase` | `{phase: "thinking" \| "tool" \| "summarizing" \| "done" \| "failed"}` |
| `tool_call` | `{round, name, args}` |
| `tool_result` | `{round, name, result, seconds}` |
| `answer` | `{text}` — final model prose, rendered as Markdown |
| `session` | `{open, sid, bytes, turns, ttl_seconds}` |
| `error` | `{message}` |
| `done` | `{seconds}` |

Unknown `job` → `404`, not an infinite stream. A disconnected SSE client must not cancel the
run; the job buffer keeps the last `200` events so a reload can re-attach.

### 6.2 What renders from data, and what stays prose

The i18n boundary was decided during the mockup review and is now fixed:

- **Model prose** (`answer`) is rendered as authored. `/language` does not translate it — this
  is the documented behaviour in `docs/USAGE.md:41` and stays true.
- **Numbers, KPI cards and result tables** are rendered by `app.js` from `tool_result`, with
  labels taken from the chrome `I18N` table. So a KPI reads "峰值时均功率 / Peak hourly mean"
  in the user's UI language while the paragraph beneath it stays in whatever language the model
  answered.
- Where the engine emitted no structured field, the GUI shows nothing rather than parsing prose.
  No invented metrics, ever.

### 6.3 Engine chip — five states, never a lie

The three fields come from the result envelope (`gpu_analytics.py:1023-1028`) and are **not**
interchangeable. The engine's own comment at `gpu_analytics.py:1009-1013` states that surfacing
a deliberately-routed CPU run as a fallback "would make a healthy decision look broken", so the
chip must key off `fallback_reason` first and `routing_reason` second:

| Chip | Condition | Style | Note shown |
| :--- | :--- | :--- | :--- |
| `GPU · cuDF` | `accelerated == true` | accent | — |
| `CPU · pandas (fallback)` | `fallback_reason != null` | **warn** | `fallback_reason` verbatim — cuDF was unavailable or the attempt failed |
| `CPU · pandas (by choice)` | `routing_reason != null` | neutral | `routing_reason` verbatim — deliberate byte-size routing |
| `CPU · pandas` | none of the above, `engine == "pandas"` | neutral | `gpu` field (e.g. `none (CPU run)`) |
| `no result yet` | run started, first `tool_result` not received | muted | never impersonate an engine |

Formatting a `routing_reason` run as an error would contradict the project's central honesty
claim, and formatting a `fallback_reason` run as a choice would hide a real problem. Both
directions are named test cases (§10.10).

### 6.4 Charts

`app.js` builds SVG with rect/line/text/circle/polyline only — the same primitive budget as
`make_deliverables.py`, which is also why hand-written SVG stays text-diffable. Five chart
types, matching the deliverable generator: line, bar, heatmap, histogram, scatter. Where
`make_deliverables.py` already produced an SVG for the current dataset, the GUI embeds that
artifact through `/artifact` rather than redrawing it, so there is one renderer per chart and
the workbench cannot disagree with the exported report.

---

## 7. Session lifecycle policy

The workbench card (approved in the mockup) is the operator's only view of device memory, so
its states are named precisely:

| State | Meaning | Card |
| :--- | :--- | :--- |
| `cold` | no session; next open pays ~1.5 s | grey |
| `warm` | session open for this dataset | green, shows open cost / per-step / turns / held bytes |
| `stale` | dataset changed since open | amber + "re-open" action; never silent |
| `released` | explicitly or by TTL | grey + one-line explanation of the next-turn cost |

Switching datasets while warm releases the old session first and says so in the log drawer.
Device memory is finite (121 GB on the reference box) and the leak the original backstop was
written for is still possible; the card exists to keep that trade visible rather than to hide
it behind automation.

---

## 8. Security

The GUI adds one attack surface the TUI never had: a listening socket. `127.0.0.1` is the
default and the only thing needed for `ssh -L`; binding any other interface requires
`GPU_GUI_ALLOW_REMOTE=1` **and** prints a refusal-with-reason at startup if that is absent.

- **Path traversal.** `/artifact` resolves against a fixed allow-list of roots (the job's
  out-dir, `benchmark/`, and `DEMO_DATA_DIR`), then `Path.resolve()` and a `is_relative_to()`
  check. Symlinks that escape are rejected. `../../etc/passwd` is a named test case (§10).
- `/api/files?dir=` is **not** a generic file browser; it delegates to `skills.list_datasets()`,
  which applies its own bounded-depth search roots (`skills.py:1037-1048`, built on
  `_data_search_dirs()` at `skills.py:1009`). An arbitrary `dir` must still be inside
  `_data_search_dirs()`.
- **Secrets.** Reuse `api_config.save_config()` unchanged: `tempfile` + `os.chmod(0o600)` +
  rename (`api_config.py:96-120`). `APIConfig.api_key` is already `field(repr=False)`
  (`api_config.py:16`), so never serialize the dataclass wholesale — build the response dict
  explicitly. The key is never accepted in a query string, and SSE frames never carry it.
- No `eval`/`innerHTML` from engine output: `tool_result` values are rendered through
  `textContent`, and `answer` Markdown is passed through a small escaping step before
  insertion — `build_html_report.py:39` (`_inline`) already establishes that escape-first
  pattern in this codebase.

---

## 9. Dependencies

Zero new runtime dependencies. `gui.py` uses `http.server.ThreadingHTTPServer`, `json`,
`threading`, `queue`, `pathlib`, `mime`, `argparse` — all stdlib. Frontend is plain ES2019 with
no build step.

Rationale, and why not FastAPI: `requirements-tui.txt` frames Textual as an *optional* interface
layer that leaves the analytical backend unchanged, and `make_deliverables.py` / 
`build_html_report.py` hand-roll small renderers rather than adding libraries ("A general
Markdown library would be the new dependency for six constructs", `build_html_report.py:28-30`).
Choosing stdlib keeps the GUI on the same axis as the TUI — optional, degradable, never a
reason the analysis cannot run — and costs roughly 100 extra lines of routing. This decision was
offered as option B (FastAPI) and option A was selected; if the workbench later grows
authentication or concurrency, revisit it then rather than pre-paying now.
("A general Markdown library would be a new dependency for six constructs",
`build_html_report.py:28-29`.)

**Degradation:** missing `gui.py` or an occupied port must never affect `agent_main.py`,
`tui_app.py` or the engine. Import of `gui.py` happens only inside the `--gui` branch.

---

## 10. Testing

`agent/gui_test.py`, headless, no network, no GPU, no API calls, temporary CSV fixtures — the
pattern set by `tui_test.py` (`FakeAgent` with a `release` event, `run_test(size=…)`). Because
`gui.py` is plain HTTP, tests drive it with `urllib.request` against an ephemeral port
(`port=0`), not a browser.

Required cases:

1. **SSE ordering** — `phase → tool_call → tool_result → answer → done`, with `done` last.
2. **Busy → 409** on a second `POST /api/ask`.
3. **Path traversal rejected** — `../../etc/passwd`, absolute `/etc/passwd`, and a symlink out
   of the allow-list all return `403`/`404` and never read bytes.
4. **TTL releases** — advance a monotonic clock seam (no `sleep(300)`), assert
   `close_all_sessions()` fired.
5. **`keep_sessions` default** — `Agent.run(q)` with no argument still releases; the CLI
   behaviour is unchanged.
6. **`result_sink` cannot break a run** — raise inside the sink, assert the answer still returns.
7. **`/api/settings` does not echo the key** and persists `0o600`.
8. **Artifact MIME + CSP headers present.**
9. **Regression gate** — `tui_test.py` and `run_criteria_tests.sh` (11/11) pass with the GUI
   merged.
10. **Chip mapping** — feed `tool_result` payloads with `fallback_reason` set, with
    `routing_reason` set, and with neither, and assert the rendered chip text and style class
    differ (§6.3). A test that only checks "GPU shows GPU" would not catch the inversion.

Frontend logic gets DOM-probe assertions only (the mockup was verified this way: every chart
tab renders, the language round-trip leaves model prose untouched). No pixel tests.

---

## 11. Decision log

| # | Decision | Chosen | Rejected | Why |
| :--- | :--- | :--- | :--- | :--- |
| 1 | GUI form factor | localhost web server + `ssh -L` | Electron/Tauri desktop | Compute lives with the data on GB10; a desktop app would ship no cuDF and would stream GB-scale CSVs across the network |
| 2 | TUI | untouched | rebuild/unify under GUI | mature, 6 of the last 10 commits polished it; a rewrite buys nothing |
| 3 | Server | stdlib `http.server` | FastAPI, Streamlit | zero new deps, parity with TUI's optional-layer status |
| 4 | Streaming | SSE | WebSocket, polling | one-way, proxy-safe, no reconnect protocol |
| 5 | Concurrency | single user, `409` | job queue | `_worker_call` holds a lock and reads one line — serialized by construction |
| 6 | Session backstop | conditional + 3-way cleanup | delete, or keep unconditional | keeps the anti-leak guarantee, recovers the ~4× resident speedup |
| 7 | i18n boundary | chrome + numbers translated, prose not | translate everything | matches `docs/USAGE.md:41`; translating model output in the frontend would be fabricating a translation |
| 8 | Charts | hand-written SVG / embed the artifact | matplotlib, Chart.js | one renderer, text-diffable, no dependency |
| 9 | Engine state source | `result_sink` structured JSON | regex on trace text | `tui_app.py:338-350` parses prose with regex; the GUI will not inherit that fragility |

## 12. Assumptions (confirm or override during spec review)

1. Reference hardware is the documented GB10 (aarch64, driver 580.126.09, 121 GB, cuDF 25.10.00);
   port `8765` is free.
2. One operator, one browser tab. A second tab shares the single `Agent` and can observe events
   — this is a property, not a feature.
3. The model configured supports Chat Completions with tool calling. Being listed by `/models`
   does not establish that (`docs/USAGE.md:116`).
4. `--gui` implies an interactive run; `--ask` and piped stdin stay non-interactive and must
   refuse to start a server.
5. The mockup file is a throwaway artifact and is not copied into the repo; `gui/index.html`
   starts from its structure but is written for the real data shapes.

## 13. Risks

| Risk | Blast radius | Mitigation |
| :--- | :--- | :--- |
| Deferred release leaks device memory | Every later question on the box slows down | three independent release triggers + a live memory card + test #4 |
| A listening port becomes a real exposure if someone binds `0.0.0.0` | Secrets and arbitrary file reads | default `127.0.0.1`, explicit opt-out env var with a startup refusal, tests #3/#7 |
| Model prose and rendered numbers drift apart | credibility of the whole "exact, not hallucinated" claim | numbers come only from `tool_result`; nothing is computed in the frontend |
| Long tool call stalls the SSE writer | browser shows a dead UI | `phase` heartbeat every 1 s from the run thread; `Retry-After`-free but bounded by the engine's own 1800 s worker timeout |
| Scope creep into auth/multi-user | never ships | §2 non-goals; any reversal needs a new spec |

---

## 14. Build order (input to writing-plans)

1. Change A + test #5 (default unchanged) — small, isolated, unblocks everything.
2. Change B + test #6.
3. `gui.py` routing, job state machine, SSE — tests #1, #2, #8.
4. `/artifact` allow-list + `/api/files` — tests #3.
5. Session TTL + `/api/session/release` — test #4.
6. `gui/index.html` + `app.js` from the approved mockup — DOM probes.
7. `/api/settings` via `api_config` — test #7.
8. `--gui` / `--gui-port` in `main()`, `requirements-gui.txt`.
9. Docs: `README.md`, `docs/USAGE.md`, `skill.md`, `references/engine-contract.md`.
10. Regression gate #9 on GB10 *and* on a no-GPU machine (pandas path must report honestly).
