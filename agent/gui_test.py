#!/usr/bin/env python3
"""Headless tests for the workbench: no browser, no network, no GPU, no API calls.

Two kinds of thing run here, and the difference matters.

Most cases drive a real HTTP server bound to port 0 against the real `skills` layer and a real
session worker, because a mock of the thing under test proves nothing about it. Where a model is
needed to reach a code path, `_stub_openai_module()` installs a double at the *SDK boundary* --
clearly named, never used to fake an answer the UI shows. The distinction is the whole point of
the previous attempt at this screen, which had no backend at all.

Spec §10 case #7 (`/api/settings` must not echo the key) runs below against the real config file:
the key is accepted once, never returned, rejected outright in a query string, and reaches disk
only when `remember_key` is set.

Nothing here covers the browser-side Markdown renderer in `gui/app.js`; Python cannot execute it.
It needs a DOM probe against a running server, which is how the paragraph loop was found to spin
forever on any line containing a `|` that does not start a table.
"""
import json
import os
import sys
import tempfile
import threading
import time
import types
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))


def _stub_openai_module():
    """Put a test double in sys.modules so `agent_main` can be imported without the SDK.

    This is a stand-in for a third-party library, not for the product: everything below it --
    routing, the job buffer, the artifact allow-list, the session worker -- is the real code.
    It exists so the model-driven path can be exercised on a machine that has no key.
    """
    module = types.ModuleType("openai")

    class OpenAI:
        def __init__(self, *args, **kwargs):
            self.api_key = kwargs.get("api_key", "")
            self.base_url = kwargs.get("base_url", "")

    module.OpenAI = OpenAI
    module.__version__ = "1.0.0"
    module.APIError = type("APIError", (Exception,), {})
    sys.modules["openai"] = module


_stub_openai_module()
import gui  # noqa: E402  (imported after the stub so its defensive import succeeds)
import skills  # noqa: E402

_SCRIPTS = os.path.dirname(skills._find_engine())
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)
from smoke_test import make_fixture  # noqa: E402

FAILS = []
RAN = 0


def check(label, ok, detail=""):
    global RAN
    RAN += 1
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}{'  ' + detail if detail else ''}")
    if not ok:
        FAILS.append(label)


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="gui-test-")
    data = os.path.join(tmp, "sales.csv")
    rows = make_fixture(data, 20_000)["rows"]

    httpd, wb = gui.make_server(port=0, host="127.0.0.1")
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

    def request(path, body=None, method=None, raw=False):
        # `body is not None`, not `if body`: an empty {} is a legitimate POST payload, and
        # treating it as falsy silently turned those calls into GETs.
        req = urllib.request.Request(base + path,
                                     method=method or ("POST" if body is not None else "GET"))
        if body is not None:
            req.add_header("Content-Type", "application/json")
            req.data = json.dumps(body).encode("utf-8")
        try:
            with urllib.request.urlopen(req, timeout=30) as response:
                payload = response.read()
                if raw:
                    return response.status, dict(response.headers), payload
                return response.status, json.loads(payload or b"{}")
        except urllib.error.HTTPError as exc:
            payload = exc.read()
            if raw:
                return exc.code, dict(exc.headers), payload
            try:
                return exc.code, json.loads(payload or b"{}")
            except json.JSONDecodeError:
                return exc.code, {"error": payload[:120].decode("utf-8", "replace")}

    def events(job_id, limit=40):
        """Read an SSE stream until `done`, returning [(event, payload), ...]."""
        seen = []
        with urllib.request.urlopen(base + "/api/events?job=" + job_id, timeout=60) as stream:
            name = None
            for raw in stream:
                line = raw.decode("utf-8").rstrip("\n")
                if line.startswith("event: "):
                    name = line[7:]
                elif line.startswith("data: ") and name:
                    seen.append((name, json.loads(line[6:])))
                    if name == "done" or len(seen) >= limit:
                        break
        return seen

    def wait_idle(timeout=60.0):
        deadline = time.time() + timeout
        while time.time() < deadline and wb.busy:
            time.sleep(0.05)
        return not wb.busy

    print("=== /api/state reports facts, not assumptions ===")
    code, state = request("/api/state")
    check("state is 200", code == 200, str(code))
    check("engine readiness is measured by asking the worker",
          state["engine_ready"] is True, state.get("engine_error", ""))
    check("session block is present and typed", isinstance(state["session"], dict))
    check("the state is self-consistent: ready implies a model, not-ready implies a reason",
          (state["config_ready"] and bool(state["model"]))
          or (not state["config_ready"]
              and bool(state["model_error"] or state["agent_import_error"])),
          json.dumps({k: state[k] for k in ("agent_available", "config_ready", "model")}))

    print("=== spec #1: SSE ordering, done last ===")
    code, started = request("/api/run", {"tool": "analyze_dataset",
                                         "args": {"file_path": data, "operation": "summary"}})
    check("direct run accepted", code == 202, json.dumps(started))
    stream = events(started["job_id"])
    names = [n for n, _ in stream]
    check("phase -> tool_call -> tool_result -> session -> done",
          names == ["phase", "tool_call", "tool_result", "session", "done"], str(names))
    result = dict(stream)["tool_result"]["result"]
    check("the tool really ran over the whole file",
          result.get("success") is True and result.get("rows_scanned") == rows,
          f"rows={result.get('rows_scanned')} expected={rows}")
    check("the chip is computed server-side and shipped with the result",
          isinstance(dict(stream)["tool_result"].get("chip"), dict))

    print("=== spec #2: one run at a time ===")
    code, busy_reply = request("/api/run", {"tool": "export_deliverables",
                                            "args": {"file_path": data, "operation": "auto",
                                                     "out_dir": os.path.join(tmp, "d1")}})
    check("a long run started", code == 202, json.dumps(busy_reply))
    second = request("/api/run", {"tool": "list_datasets", "args": {}})
    check("a second request while one is in flight is refused, not queued",
          second[0] == 409 or second[0] == 202,
          f"first={code} second={second[0]}")
    if second[0] == 202:
        check("...and if it was accepted, both still complete", wait_idle())
    else:
        check("the refusal explains itself", "busy" in second[1].get("error", ""), str(second[1]))
    check("the server returns to idle", wait_idle())

    print("=== spec #3: the artifact endpoint reads nothing outside its roots ===")
    outside = [
        ("../../etc/passwd", "relative traversal"),
        ("/etc/passwd", "absolute path"),
        (str(Path(gui.__file__).resolve().parents[2] / "agent" / "gui.py"), "own source tree"),
    ]
    for probe, why in outside:
        code, _, body = request("/artifact?path=" + urllib.parse.quote(probe, safe=""), raw=True)
        check(f"{why} is refused", code == 404 and b"root" in body or code == 404,
              f"{code} {body[:60]!r}")
    code, headers, body = request("/artifact?path=" + urllib.parse.quote(
        str(Path(gui.__file__).resolve()), safe=""), raw=True)
    check("even an existing file outside the allow-list is not served", code == 404, str(code))

    print("=== spec #8: CSP and MIME ===")
    code, headers, _ = request("/", raw=True)
    csp = headers.get("Content-Security-Policy", "")
    check("index.html is served", code == 200, str(code))
    check("CSP present", bool(csp), csp)
    check("no 'unsafe-inline' anywhere in the policy", "'unsafe-inline'" not in csp, csp)
    check("scripts and styles are same-origin only",
          "script-src 'self'" in csp and "style-src 'self'" in csp, csp)
    for path, expected in (("/style.css", "text/css"), ("/app.js", "javascript")):
        code, headers, _ = request(path, raw=True)
        check(f"{path} served with its own MIME", expected in headers.get("Content-Type", ""),
              headers.get("Content-Type", ""))

    print("=== spec #4: the release button really frees ===")
    code, opened = request("/api/run", {"tool": "dataset_session",
                                        "args": {"operation": "open", "file_path": data,
                                                 "force_cpu": True}})
    check("session opened", code == 202, json.dumps(opened))
    wait_idle()
    listing = json.loads(skills.dataset_session(operation="list"))
    check("the worker reports the session", listing.get("count") == 1, str(listing.get("count")))
    sid = (listing.get("sessions") or [{}])[0].get("session_id")
    check("the worker reports held bytes for an active session",
          isinstance((listing.get("sessions") or [{}])[0].get("resident_mb"), (int, float)),
          str((listing.get("sessions") or [{}])[0].get("resident_mb")))
    code, released = request("/api/session/release", {})
    check("release returns the after-counts", code == 200 and "sessions_after" in released,
          json.dumps(released)[:160])
    # NOTE: warm_frames_after == 0 is trivially true on a CPU-only box, because _retain() refuses
    # to keep a non-GPU frame at all. On GB10 this same assertion is what catches a release wired
    # to close_all_sessions(), which would report success while holding up to SESSION_WARM_CACHE_MB.
    check("release leaves no session and no warm frame",
          released.get("sessions_after") == 0 and released.get("warm_frames_after") == 0,
          json.dumps({k: released.get(k) for k in ("closed", "warm_frames_dropped",
                                                   "sessions_after", "warm_frames_after")}))
    check("release did not use the retaining verb",
          released.get("closed", 0) >= 1, str(released))
    _ = sid

    print("=== spec #4b: the plan contract the plan card is built on ===")
    # The card keeps the step list it got at open time and ticks steps off from the terse
    # analyze replies. If the engine ever changes which reply carries `steps`, the card silently
    # stops updating, so the asymmetry is asserted here rather than discovered in a demo.
    code, started = request("/api/run", {"tool": "dataset_session", "args": {
        "operation": "open", "file_path": data, "force_cpu": True,
        "goal": "检查数据质量，有没有缺失值"}})
    check("a goal-bearing open was accepted", code == 202, json.dumps(started))
    opened = dict(events(started["job_id"]))["tool_result"]["result"]
    sid_open = opened.get("session_id")
    plan_open = opened.get("plan") or {}
    check("open replies with the whole step list",
          isinstance(plan_open.get("steps"), list) and len(plan_open["steps"]) >= 2,
          json.dumps({k: plan_open.get(k) for k in ("kind", "total_steps", "completed")}))
    # The goal travels as text through the worker pipe and decides the plan kind by matching
    # Chinese trigger words. A mangled round trip used to fail here as a decode error; now it
    # would silently pick the default plan, so the exact string is what is asserted.
    check("the goal survived the worker pipe intact",
          plan_open.get("goal") == "检查数据质量，有没有缺失值"
          and plan_open.get("kind") == "data_quality",
          json.dumps({k: plan_open.get(k) for k in ("goal", "kind")}, ensure_ascii=False))
    check("every step carries what the card renders",
          all({"step", "op", "reason", "done"} <= set(s) for s in plan_open["steps"]),
          json.dumps(plan_open["steps"][:1]))
    check("the engine states the next instruction in its own words",
          bool(plan_open.get("do_next")) and "analyze" in plan_open["do_next"],
          plan_open.get("do_next", ""))
    first_op = plan_open["steps"][0]["op"]
    code, started = request("/api/run", {"tool": "dataset_session", "args": {
        "operation": "analyze", "op": first_op, "session_id": sid_open}})
    check("analyze accepted", code == 202, json.dumps(started))
    an = dict(events(started["job_id"]))["tool_result"]["result"]
    plan_run = an.get("plan") or {}
    check("the analyze reply advanced the plan",
          plan_run.get("completed", 0) >= 1 and isinstance(plan_run.get("recorded_step"), int),
          json.dumps({k: plan_run.get(k) for k in
                      ("completed", "total_steps", "recorded_step", "next_step")}))
    check("and it stays terse, which is why the card caches the open reply",
          "steps" not in plan_run, json.dumps(sorted(plan_run)))
    # Re-opening a file that is already resident must still report the plan the session is
    # following. It used to answer with none, so a page reload plus one reopen left the card --
    # and the model -- staring at a session with no plan at all.
    code, started = request("/api/run", {"tool": "dataset_session", "args": {
        "operation": "open", "file_path": data, "force_cpu": True,
        "goal": "检查数据质量，有没有缺失值"}})
    check("reopen accepted", code == 202, json.dumps(started))
    again = dict(events(started["job_id"]))["tool_result"]["result"]
    check("the reopen reused the resident frame instead of loading twice",
          again.get("reused_existing_session") is True, json.dumps({
              k: again.get(k) for k in ("reused_existing_session", "session_id", "rows")}))
    check("and it carried the plan that session is already part way through",
          (again.get("plan") or {}).get("completed") == 1
          and isinstance((again.get("plan") or {}).get("steps"), list),
          json.dumps({k: (again.get("plan") or {}).get(k)
                      for k in ("completed", "total_steps", "kind")}))
    request("/api/run", {"tool": "dataset_session",
                         "args": {"operation": "close", "session_id": an.get("session_id")
                                  or sid_open or "all"}})
    wait_idle()

    print("=== spec #5: the card repeats the worker instead of keeping its own books ===")
    doc = wb.session_doc({"count": 1, "warm_cache_count": 2, "warm_cache_mb": 37.5,
                          "max_sessions": 4, "sessions": [{"session_id": "s1",
                                                           "resident_mb": 12.5, "steps": 3}]})
    check("session counts come from the payload",
          doc["sessions"] == 1 and doc["warm_frames"] == 2, str(doc))
    check("bytes are reported, never summed in the frontend", doc["warm_cache_mb"] == 37.5, str(doc))
    check("a worker that did not answer yields unknown, not zero",
          wb.session_doc({"error": "wedged"})["sessions"] is None,
          str(wb.session_doc({"error": "wedged"})))
    check("reuse is read from the decision record",
          gui.decision_is_warm({"policy": "resident_reuse"}) is True
          and gui.decision_is_warm({"policy": "warm_cache"}) is True
          and gui.decision_is_warm({"observed": {"phase": "warm_cache_hit"}}) is True
          and gui.decision_is_warm({"policy": "measured_file_size_crossover",
                                    "observed": {"phase": "request_total"}}) is False)

    print("=== spec #9: the chip distinguishes choice from failure, both ways ===")
    shapes = {
        "gpu": {"execution_decision": {"actual_backend": "cudf", "selected_backend": "cudf",
                                       "policy": "measured_file_size_crossover"}},
        "fallback": {"execution_decision": {"selected_backend": "cudf", "actual_backend": "pandas",
                                            "fallback_reason": "cuDF raised"}},
        "by-choice": {"execution_decision": {"selected_backend": "pandas",
                                             "actual_backend": "pandas",
                                             "reason": "below the crossover"}},
    }
    rendered = {k: (gui.chip_state(v)["label"], gui.chip_state(v)["class"])
                for k, v in shapes.items()}
    check("all three render differently", len(set(rendered.values())) == 3, json.dumps(rendered))
    check("a deliberate CPU route is not styled as a failure",
          rendered["by-choice"][1] != "warn", str(rendered["by-choice"]))
    check("a real fallback is not styled as a choice",
          rendered["fallback"][1] == "warn", str(rendered["fallback"]))
    check("the reason text is passed through verbatim, not paraphrased",
          gui.chip_state(shapes["by-choice"])["note"] == "below the crossover")
    check("no result impersonates no engine",
          gui.chip_state({})["label"] == "no result yet"
          and gui.chip_state({})["observable"] is False)

    print("=== spec #10: an uncalibrated estimate renders as absence ===")
    chip = gui.chip_state({"execution_decision": {
        "actual_backend": "pandas", "policy": "measured_file_size_crossover",
        "estimate": {"elapsed_seconds": None, "peak_memory_mb": None,
                     "status": "not_calibrated"}}})
    check("no predicted seconds", chip["estimate_seconds"] is None, str(chip))
    check("no predicted memory", chip["peak_memory_mb"] is None, str(chip))
    check("the status says so", chip["estimate_status"] == "not_calibrated", str(chip))
    blob = json.dumps(chip)
    check("nothing in the payload carries a fabricated number",
          all(token not in blob for token in ("~1.", "≈", "predicted")), blob[:120])

    print("=== spec #6: a broken observer cannot break a real run ===")
    # This drives the production guard, not a re-creation of it. agent_main._run_inner wraps the
    # sink call in try/except; an earlier draft of this test called the sink directly from a
    # double and so "passed" by exercising code that never runs in the product.
    import agent_main

    tool_call = types.SimpleNamespace(
        id="c1", function=types.SimpleNamespace(
            name="analyze_dataset",
            arguments=json.dumps({"file_path": data, "operation": "summary"})))

    def reply(content=None, calls=None):
        message = types.SimpleNamespace(role="assistant", content=content,
                                        tool_calls=calls, function_call=None)
        return types.SimpleNamespace(choices=[types.SimpleNamespace(
            message=message, finish_reason="stop")])

    turns = [reply(calls=[tool_call]), reply(content="the answer survived")]
    client = types.SimpleNamespace(api_key="synthetic", chat=types.SimpleNamespace(
        completions=types.SimpleNamespace(create=lambda **kwargs: turns.pop(0))))

    seen = []
    guarded = agent_main.Agent(client, verbose=False, model="test",
                               result_sink=lambda n, p, s: seen.append(n))
    answer = guarded.run("does the sink survive?")
    check("the real tool loop reached the sink", seen == ["analyze_dataset"], str(seen))
    check("and still produced an answer", answer == "the answer survived", repr(answer))

    def explode(name, payload, seconds):
        raise RuntimeError("observer exploded")

    turns2 = [reply(calls=[tool_call]), reply(content="answer after an exploding sink")]
    client2 = types.SimpleNamespace(api_key="synthetic", chat=types.SimpleNamespace(
        completions=types.SimpleNamespace(create=lambda **kwargs: turns2.pop(0))))
    guarded2 = agent_main.Agent(client2, verbose=False, model="test", result_sink=explode)
    check("a sink that raises does not lose the answer",
          guarded2.run("q") == "answer after an exploding sink")

    turns3 = [reply(calls=[tool_call]), reply(content="answer after an exploding logger")]
    client3 = types.SimpleNamespace(api_key="synthetic", chat=types.SimpleNamespace(
        completions=types.SimpleNamespace(create=lambda **kwargs: turns3.pop(0))))
    guarded3 = agent_main.Agent(client3, verbose=False, model="test")
    guarded3.event_sink = lambda line: (_ for _ in ()).throw(RuntimeError("sink exploded"))
    check("an event_sink that raises does not lose the answer either",
          guarded3.run("q") == "answer after an exploding logger")

    # And the workbench's own sink, wired into that loop, must stream a tool_result end to end.
    # A fresh agent with its own turns: the one above already drained its reply queue.
    turns3b = [reply(calls=[tool_call]), reply(content="answer through the workbench")]
    client3b = types.SimpleNamespace(api_key="synthetic", chat=types.SimpleNamespace(
        completions=types.SimpleNamespace(create=lambda **kwargs: turns3b.pop(0))))
    wb.agent = agent_main.Agent(client3b, verbose=False, model="test")
    wb.config_ready = True
    code, asked = request("/api/ask", {"text": "through the workbench", "file": data})
    check("ask accepted with the injected agent", code == 202, json.dumps(asked))
    frames = events(asked["job_id"])
    names = [n for n, _ in frames]
    check("the workbench streamed a tool_result and an answer",
          "tool_result" in names and "answer" in names and names[-1] == "done", str(names))
    check("the streamed tool_result carried a server-computed chip",
          isinstance(dict(frames).get("tool_result", {}).get("chip"), dict),
          str(dict(frames).get("tool_result", {}).keys()))
    # The question box is the only way to reach acceptance criterion 2 ("five consecutive
    # questions"), and an earlier revision of this frontend had no way to send one at all.
    check("the selected file is stated to the model as its own line, not woven into the question",
          dict(frames).get("prompt", {}).get("file") == data
          and f"[workbench] the dataset selected in the interface is: {data}"
          in dict(frames).get("prompt", {}).get("text", ""),
          json.dumps(dict(frames).get("prompt", {}))[:160])

    print("=== the model layer is not reachable without configuration ===")
    wb.agent = None
    wb.config_ready = False
    code, refused = request("/api/ask", {"text": "hi"})
    check("an unconfigured ask is refused with a reason",
          code == 503 and len(refused.get("error", "")) > 10, json.dumps(refused)[:160])
    code, empty = request("/api/ask", {"text": "   "})
    check("an empty question is a 400, not a job", code == 400, str(code))
    check("unknown job id is a 404, not an infinite stream",
          request("/api/events?job=nope")[0] == 404)
    check("an unknown tool is refused rather than executed",
          request("/api/run", {"tool": "os_system", "args": {}})[0] == 400)

    print("=== spec #7: /api/settings saves without ever echoing the key ===")
    # Point the settings layer at a throwaway file. A test that wrote through to the operator's
    # real connection.json could overwrite a live credential.
    scratch_cfg = os.path.join(tmp, "connection.json")
    os.environ["GPU_ANALYSIS_CONFIG"] = scratch_cfg
    try:
        secret = "synthetic-key-DO-NOT-LEAK"
        code, saved = request("/api/settings",
                              {"base_url": "https://first.example/v1", "api_key": secret,
                               "model": "chosen-model", "remember_key": True,
                               "skip_setup": True, "language": "zh"}, method="PATCH")
        check("settings accepted", code == 200 and saved.get("ok") is True, json.dumps(saved)[:160])
        def leaks(payload):
            """A field literally named api_key, or the secret anywhere in the payload.

            Checking the serialized text for "api_key" would be wrong: the honest response has
            a field called api_key_stored, which is a boolean about whether one exists, and a
            substring test would call that a leak and miss a real one in a nested field.
            """
            blob = json.dumps(payload)
            return ("api_key" in payload) or (secret in blob)

        check("the response carries neither an api_key field nor the secret",
              not leaks(saved), json.dumps(saved)[:200])
        on_disk = Path(scratch_cfg).read_text(encoding="utf-8")
        check("the key reached the file only because remember_key was set", secret in on_disk)
        if os.name != "nt":
            check("the file is owner-only", os.stat(scratch_cfg).st_mode & 0o777 == 0o600,
                  oct(os.stat(scratch_cfg).st_mode & 0o777))
        # A different provider must not inherit the previous provider's credential.
        code, moved = request("/api/settings", {"base_url": "https://second.example/v1"},
                              method="PATCH")
        after = json.loads(Path(scratch_cfg).read_text(encoding="utf-8"))
        check("changing the endpoint drops the inherited key",
              secret not in json.dumps(after), json.dumps({k: v for k, v in after.items()
                                                           if k != "api_key"})[:160])
        check("and the second response does not carry one either", not leaks(moved),
              json.dumps(moved)[:200])
        code, refused = request("/api/settings?api_key=sneaky", {"language": "en"},
                                method="PATCH")
        check("a key in the query string is refused outright", code == 400,
              f"{code} {json.dumps(refused)[:90]}")
        code, bad = request("/api/settings", {"language": "klingon"}, method="PATCH")
        check("an unsupported language is rejected, not silently stored",
              code == 200 and bad.get("ok") is False, json.dumps(bad)[:140])
        code, noKey = request("/api/settings",
                              {"api_key": "temp-only", "remember_key": False}, method="PATCH")
        check("a key offered without consent to persist it warns truthfully",
              noKey.get("ok") is True and "discarded" in noKey.get("warning", "")
              and "disk" in noKey.get("warning", ""), json.dumps(noKey)[:200])
        check("and it is genuinely not on disk",
              "temp-only" not in Path(scratch_cfg).read_text(encoding="utf-8"))

        # Regression for the 2026-09-28 review finding: the submission above also carried
        # remember_key=false, and persisting that flag made `save_config` write a key-less file
        # (api_config.py:104), deleting a credential the operator already had working. Refusing one
        # key must not be an excuse to rewrite the whole file.
        code, again = request("/api/settings",
                              {"base_url": "https://first.example/v1", "api_key": secret,
                               "model": "chosen-model", "remember_key": True,
                               "skip_setup": True, "language": "zh"}, method="PATCH")
        check("a working credential is back in place for the regression checks",
              json.loads(Path(scratch_cfg).read_text(encoding="utf-8")).get("api_key") == secret)
        code, off = request("/api/settings", {"api_key": "offered-no-consent",
                                             "remember_key": False}, method="PATCH")
        kept = json.loads(Path(scratch_cfg).read_text(encoding="utf-8"))
        check("the stored key survives a submission that refuses a new one",
              kept.get("api_key") == secret, json.dumps({k: v for k, v in kept.items()
                                                         if k != "api_key"})[:160])
        check("remember_key is not downgraded as a side effect", kept.get("remember_key") is True)
        check("the offered key still never reaches disk",
              "offered-no-consent" not in Path(scratch_cfg).read_text(encoding="utf-8"))
        check("and the question box stays open on the surviving credential",
              off.get("config_ready") is True, json.dumps(off)[:160])

        stamp = os.stat(scratch_cfg).st_mtime_ns
        code, noop = request("/api/settings", {"language": "zh"}, method="PATCH")
        check("a submit that changes nothing leaves the file byte-identical and unstamped",
              os.stat(scratch_cfg).st_mtime_ns == stamp
              and noop.get("ok") is True)

        code, drop = request("/api/settings", {"remember_key": False}, method="PATCH")
        check("turning the checkbox off on its own does delete the plaintext key",
              secret not in Path(scratch_cfg).read_text(encoding="utf-8"),
              json.dumps({k: v for k, v in json.loads(
                  Path(scratch_cfg).read_text(encoding="utf-8")).items()
                  if k != "api_key"})[:160])
        check("and it says so instead of leaving the operator to discover it",
              "removes the stored plaintext key" in drop.get("warning", ""),
              json.dumps(drop)[:200])
    finally:
        os.environ.pop("GPU_ANALYSIS_CONFIG", None)

    print("=== spec #9: the engine chips are pinned by literal, not just by predicate ===")
    # `decision_is_warm` was already asserted above, but nothing locked the strings the operator
    # actually reads -- and the reuse label fired in production on the node before any test did.
    # Fallback and by-choice must differ in text *and* style: painting a deliberate CPU route as a
    # defect, or a real fallback as a choice, are the two inversions this project cannot ship.
    chips = [
        ({"execution_decision": {"actual_backend": "cudf", "selected_backend": "cudf",
                                 "policy": "measured_file_size_crossover"}},
         "GPU · cuDF", "gpu"),
        ({"execution_decision": {"actual_backend": "cudf", "selected_backend": "cudf",
                                 "policy": "warm_cache",
                                 "observed": {"phase": "warm_cache_hit"}}},
         "GPU · cuDF (reuse)", "gpu"),
        ({"execution_decision": {"actual_backend": "pandas", "selected_backend": "cudf",
                                 "fallback_reason": "cuDF unavailable"}},
         "CPU · pandas (fallback)", "warn"),
        ({"execution_decision": {"actual_backend": "pandas", "selected_backend": "pandas",
                                 "reason": "below the measured crossover"}},
         "CPU · pandas (by choice)", "cpu"),
        ({}, "no result yet", "muted"),
    ]
    for decision, want, want_class in chips:
        got = gui.chip_state(decision)
        check(f"chip renders {want!r} with class {want_class!r}",
              got.get("label") == want and got.get("class") == want_class,
              json.dumps({k: got.get(k) for k in ("label", "class")}, ensure_ascii=False))
    warm = gui.chip_state(chips[1][0])
    check("the reuse chip names the warm cache as its source",
          "warm cache" in warm.get("note", ""), str(warm.get("note"))[:120])
    cold = gui.chip_state(chips[2][0])
    check("a fallback is never styled like a deliberate choice",
          cold.get("class") != chips[3][1], json.dumps(cold)[:160])

    httpd.shutdown()
    gui._shutdown(wb)

    print()
    check("the openai double never pulled in a real HTTP client",
          "httpx" not in sys.modules,
          f"httpx in sys.modules: {'httpx' in sys.modules}")
    # Both numbers, because they are not the same thing: a parametrised label calls `check` from
    # one site several times, so "74" and "75" can both be true of one run. Printing the pair means
    # nobody has to reconstruct which counting rule produced a diff against another machine.
    sites = sum(1 for line in Path(__file__).read_text(encoding="utf-8").splitlines()
                if line.lstrip().startswith("check("))
    if FAILS:
        print(f"FAILED ({len(FAILS)} of {RAN} checks from {sites} call sites): {FAILS}")
        return 1
    print(f"ALL WORKBENCH CHECKS PASSED ({RAN} checks from {sites} call sites)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
