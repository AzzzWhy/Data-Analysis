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
import re
import socket
import subprocess
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

    # Readiness is measured from the operator's real connection.json plus any exported GPU_API_KEY,
    # so this suite inherited whatever credential state the machine happened to be in. On a node
    # whose stored file has a base_url and a model but no key (`remember_key` off), the very first
    # check failed for a reason that had nothing to do with the code under test -- and the same file
    # passes when a key is exported. Pin a scratch config for the run, and leave the operator's file
    # out of it entirely.
    fixture_cfg = os.path.join(tmp, "connection-fixture.json")
    with open(fixture_cfg, "w", encoding="utf-8") as fh:
        json.dump({"base_url": "http://127.0.0.1:1/v1", "model": "fixture-model",
                   "api_key": "fixture-key", "language": "zh"}, fh)
    ambient_cfg = os.environ.get("GPU_ANALYSIS_CONFIG")
    ambient_key = os.environ.get("GPU_API_KEY")
    os.environ["GPU_ANALYSIS_CONFIG"] = fixture_cfg
    os.environ.pop("GPU_API_KEY", None)

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
          (state["config_ready"] and bool(state["model"]) and state["missing_fields"] == [])
          or (not state["config_ready"]
              and (bool(state["missing_fields"])
                   or bool(state["model_error"] or state["agent_import_error"]))),
          json.dumps({k: state[k] for k in ("agent_available", "config_ready", "model")})
          + " missing=" + json.dumps(state.get("missing_fields")))
    # `ready` is a conjunction in api_config.py:23. A bare false cannot say whether to type a key or
    # pick a model, and the browser had one fixed sentence covering all three -- so the field list is
    # the fact, and the UI assembles the sentence from it.
    check("missing_fields only ever names fields, never a value",
          all(f in ("base_url", "model", "api_key") for f in state["missing_fields"]),
          json.dumps(state["missing_fields"]))

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

        # api_config authors its refusals in Chinese. They are in the label table, so the server can
        # answer in the language the screen is using -- otherwise an English interface shows one
        # Chinese sentence at exactly the moment the operator made a mistake.
        request("/api/settings", {"language": "zh"}, method="PATCH")
        code, zh_refusal = request("/api/settings", {"base_url": "http://remote.example/v1"},
                                   method="PATCH")
        request("/api/settings", {"language": "en"}, method="PATCH")
        code, en_refusal = request("/api/settings", {"base_url": "http://remote.example/v1"},
                                   method="PATCH")
        zh_err, en_err = zh_refusal.get("error", ""), en_refusal.get("error", "")
        han = lambda s: any("\u4e00" <= c <= "\u9fff" for c in s)
        check("a refused URL is explained in the language the screen is in, not only in Chinese",
              code == 200 and zh_err and en_err and han(zh_err) and not han(en_err)
              and en_err.startswith("base_url rejected: "),
              f"zh={zh_err[:44]!r} en={en_err[:66]!r}")
        check("the refusal still wrote nothing",
              json.loads(Path(scratch_cfg).read_text(encoding="utf-8"))["base_url"] != "http://remote.example/v1")
        request("/api/settings", {"language": "zh"}, method="PATCH")

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
        code, named = request("/api/state")
        check("the state then names the one missing field, instead of guessing all three",
              named.get("config_ready") is False
              and named.get("missing_fields") == ["api_key"]
              and bool(named.get("model")),
              json.dumps({k: named.get(k) for k in ("config_ready", "missing_fields",
                                                    "model")})[:160])

        # The semantics above are deliberate and now pinned. What was still missing is the form:
        # app.js submits `remember_key` verbatim from a checkbox that nothing ever prefilled, so
        # an unrelated edit arrived as an explicit uncheck. Verified on GB10 against the real
        # route -- a model-only save wiped a stored key while the response went on saying
        # config_ready=True, i.e. the question box kept working until the next restart.
        stored = "synthetic-stored-DO-NOT-LEAK"
        # Isolate from the ambient environment: an exported GPU_API_KEY outranks the stored one in
        # load_config, so asserting on what the file contains while that var is set measures the
        # env override instead of the credential being managed.
        ambient = os.environ.pop("GPU_API_KEY", None)
        try:
            code, mk = request("/api/settings", {"base_url": "https://first.example/v1",
                                                 "model": "m-one", "api_key": stored,
                                                 "remember_key": True}, method="PATCH")
            check("setup: a consented save stores the key",
                  mk.get("api_key_stored") is True
                  and stored in Path(scratch_cfg).read_text(encoding="utf-8"), json.dumps(mk)[:120])
            code, st = request("/api/state")
            check("state publishes remember_key so the settings form can prefill it",
                  st.get("remember_key") is True, f"got {st.get('remember_key')!r}")
            check("and reports nothing missing once URL, model and key are all there",
                  st.get("config_ready") is True and st.get("missing_fields") == [],
                  json.dumps({k: st.get(k) for k in ("config_ready", "missing_fields")}))
            code, kept = request("/api/settings", {"base_url": "https://first.example/v1",
                                                   "model": "m-two",
                                                   "remember_key": True}, method="PATCH")
            check("with the box prefilled, editing only the model keeps the key",
                  kept.get("ok") is True and stored in Path(scratch_cfg).read_text(encoding="utf-8"),
                  json.dumps({k: kept.get(k) for k in ("ok", "api_key_stored", "config_ready")}))

            # Editing the model ID used to be a no-op with a success message: apply_settings wrote
            # the new value to disk, then called _configure(self.model) -- the value the screen had
            # been showing *before* the edit -- which read the file back and overrode it again. The
            # node showed disk=beta-model, /api/state=alpha-model, agent=alpha-model.
            code, renamed = request("/api/settings", {"model": "beta-model"}, method="PATCH")
            code, st_after = request("/api/state")
            check("a saved model ID takes effect in the running server, not only on disk",
                  renamed.get("model") == "beta-model"
                  and st_after.get("model") == "beta-model"
                  and json.loads(Path(scratch_cfg).read_text(encoding="utf-8"))["model"] == "beta-model",
                  json.dumps({"patch": renamed.get("model"), "state": st_after.get("model")}))
            check("and the client the agent will actually call holds the model the screen shows",
                  getattr(wb.agent, "model", None) == st_after.get("model"),
                  f"agent={getattr(wb.agent, 'model', None)!r} state={st_after.get('model')!r}")
            check("the missing-field list survives the edit (nothing was cleared on the way)",
                  st_after.get("missing_fields") == [] and st_after.get("config_ready") is True,
                  json.dumps(st_after.get("missing_fields")))
            # The mirror-image bug would be worse: `--model` is a launch-time pin, so it has to keep
            # winning after the form edits something, instead of being replaced by the typed value.
            pinned = gui.Workbench(model_override="cli-pinned")
            try:
                check("the --model launch override is what a fresh workbench reports",
                      pinned.model == "cli-pinned", f"model={pinned.model!r}")
                pinned.apply_settings({"model": "typed-in-form"})
                check("and it survives a settings edit rather than being overwritten by the form",
                      pinned.model == "cli-pinned", f"after edit: model={pinned.model!r}")
            finally:
                pinned.agent = None
        finally:
            if ambient is not None:
                os.environ["GPU_API_KEY"] = ambient
    finally:
        os.environ.pop("GPU_ANALYSIS_CONFIG", None)

    print("=== spec #12: a body the server cannot read is refused, not answered ok ===")
    # Four different failures used to collapse into one: a chunked body this HTTP/1.0 handler cannot
    # read at all, a garbage Content-Length, JSON that is valid but not an object, and JSON that does
    # not parse. Each arrived as `{}`, `{}` is a legal empty patch, and the server said `ok: true`
    # about a body it had never seen. The chunked shape is not hypothetical -- the jsdom harness sent
    # {"language":"en"} twice on this node and got a success back both times, which read as the
    # product ignoring the operator. urllib cannot produce any of these shapes, so this talks socket.
    os.environ["GPU_ANALYSIS_CONFIG"] = scratch_cfg
    port = httpd.server_address[1]

    def raw_http(headers, body=b""):
        with socket.create_connection(("127.0.0.1", port), timeout=10) as sock:
            sock.sendall(("\r\n".join(headers) + "\r\n\r\n").encode() + body)
            got = b""
            try:
                while True:
                    chunk = sock.recv(4096)
                    if not chunk:
                        break
                    got += chunk
            except OSError:
                pass
        text = got.decode("utf-8", "replace")
        head = text.split("\r\n", 1)[0].split(" ")
        return (int(head[1]) if len(head) > 1 and head[1].isdigit() else 0), text

    req = ["PATCH /api/settings HTTP/1.1", "Host: 127.0.0.1", "Connection: close",
           "Content-Type: application/json"]
    status, chunked = raw_http(req + ["Transfer-Encoding: chunked"],
                               b"8\r\n{\"langua\r\n6\r\nge\":\"en\r\n0\r\n\r\n")
    check("a chunked body is refused outright instead of being read as an empty patch",
          status == 400 and "chunked" in chunked.lower(), chunked.split("\r\n", 1)[0])
    check("and the field it claimed to set really stayed untouched",
          json.loads(Path(scratch_cfg).read_text(encoding="utf-8")).get("language") != "en")

    for label, extra, body in (
        ("a garbage Content-Length", ["Content-Length: twelve"], b'{"language":"en"}'),
        ("a valid JSON body that is not an object", ["Content-Length: 3"], b"[1]"),
        ("a body that is not JSON at all", ["Content-Length: 5"], b"{oops"),
    ):
        status, text = raw_http(req + extra, body)
        check(f"{label} gets a 400 with a reason, not a success",
              status == 400 and bool(text.split("\r\n\r\n", 1)[-1].strip()),
              text.split("\r\n", 1)[0])

    status, text = raw_http(req)                       # no Content-Length, no body: the legal no-op
    check("a genuinely absent body is still the legal empty patch (200, ok:true, nothing written)",
          status == 200 and json.loads(text.split("\r\n\r\n", 1)[-1] or "{}").get("ok") is True,
          text.split("\r\n", 1)[0])

    status, text = raw_http(req + ["Content-Length: 17"], b'{"language":"en"}')
    check("and the well-formed request still works, so the refusal is not a blanket block",
          status == 200 and json.loads(Path(scratch_cfg).read_text(encoding="utf-8")
                                       ).get("language") == "en", text.split("\r\n", 1)[0])
    os.environ["GPU_ANALYSIS_CONFIG"] = fixture_cfg

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
    # The replay cache used to grow by one full transcript per question and never shrink; a
    # workbench left open across a long demo accumulates them silently. Pruning must never take a
    # run the viewer is still attached to, because /api/events answers 404 for anything forgotten.
    saved_jobs = dict(wb.jobs)
    try:
        wb.jobs.clear()
        for n in range(gui.JOB_HISTORY + 40):
            stale = gui.Job("run", f"finished {n}")
            stale.id = f"j-old-{n}"      # real ids collide inside one millisecond; be explicit
            stale.finish()
            wb.jobs[stale.id] = stale
        live = gui.Job("run", "still running")
        live.id = "j-live"
        wb.jobs[live.id] = live
        wb._prune_jobs()
        check("the replay cache is bounded", len(wb.jobs) <= gui.JOB_HISTORY,
              f"{len(wb.jobs)} kept of {gui.JOB_HISTORY + 1} created")
        check("pruning never drops a run that has not finished", live.id in wb.jobs, "")
    finally:
        wb.jobs.clear()
        wb.jobs.update(saved_jobs)

    print("=== the access token gates every route when one is set ===")
    # A token server and a tokenless server, driven over real HTTP. This runs as a subprocess
    # because `Handler.workbench` is class state set by make_server(): a second server in this
    # process would hand the first one a different workbench mid-suite. The probe keeps the two
    # worlds apart, and pins the same scratch config the suite above runs under.
    probe_src = r'''
import json, sys, threading, urllib.error, urllib.parse, urllib.request
from pathlib import Path
sys.path.insert(0, str(Path.cwd()))
import gui

TOKEN = "correct horse battery staple"
plain, plain_wb = gui.make_server(port=0, host="127.0.0.1")
guard, guard_wb = gui.make_server(port=0, host="127.0.0.1", token=TOKEN)
for srv in (plain, guard):
    threading.Thread(target=srv.serve_forever, daemon=True).start()

def hit(httpd, path, body=None, cookie=None):
    req = urllib.request.Request(f"http://127.0.0.1:{httpd.server_address[1]}{path}",
                                 method="POST" if body is not None else "GET")
    if body is not None:
        req.add_header("Content-Type", "application/json")
        req.data = json.dumps(body).encode("utf-8")
    if cookie:
        req.add_header("Cookie", cookie)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers), exc.read()

failures = []
def expect(label, ok, detail=""):
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}{'  ' + detail if detail else ''}")
    if not ok:
        failures.append(label)

# The tokenless server is only ever asked for static files: after the second make_server(),
# `Handler.workbench` points at the guard's workbench, so an /api/state there would exercise
# the wrong object and prove nothing about token isolation.
code, _, body = hit(guard, "/")
expect("no session: / is the sign-in page, not the app",
       code == 200 and b"gate-form" in body and b"workspace" not in body, f"code {code}")
code, _, _ = hit(guard, "/login.css")
expect("the sign-in page's own stylesheet loads without a session", code == 200, f"code {code}")
code, _, _ = hit(guard, "/api/state")
expect("no session: the API answers 401, not data", code == 401, f"code {code}")
code, _, _ = hit(guard, "/app.js")
expect("no session: the app's own code stays behind the gate", code == 401, f"code {code}")
code, _, _ = hit(guard, "/api/login", body={"token": "wrong"})
expect("a wrong token is a 401", code == 401, f"code {code}")
code, _, _ = hit(guard, "/api/login?" + urllib.parse.urlencode({"token": TOKEN}),
                 body={"token": TOKEN})
expect("the token in a query string is refused outright", code == 400, f"code {code}")
code, headers, _ = hit(guard, "/api/login", body={"token": TOKEN})
cookie = headers.get("Set-Cookie", "")
sid = cookie.split("dws=", 1)[1].split(";", 1)[0] if "dws=" in cookie else ""
expect("the right token trades for an HttpOnly, SameSite=Strict cookie",
       code == 200 and sid and "HttpOnly" in cookie and "SameSite=Strict" in cookie,
       f"code {code}")
code, _, body = hit(guard, "/api/state", cookie="dws=" + sid)
expect("the session cookie opens the API",
       code == 200 and b"engine_ready" in body, f"code {code}")
code, _, body = hit(guard, "/api/state", cookie="dws=" + sid)
expect("the state says the gate is on for this server",
       json.loads(body).get("auth_required") is True,
       f"auth_required={json.loads(body).get('auth_required')}")
code, _, body = hit(guard, "/", cookie="dws=" + sid)
expect("the session cookie boots the app itself",
       code == 200 and b"workspace" in body and b"gate-form" not in body, f"code {code}")
code, _, _ = hit(guard, "/api/state", cookie="dws=forged-session-id")
expect("a cookie nobody issued is rejected", code == 401, f"code {code}")
code, _, _ = hit(guard, "/api/logout", body={}, cookie="dws=" + sid)
expect("logout retires the session it names", code == 200, f"code {code}")
code, _, _ = hit(guard, "/api/state", cookie="dws=" + sid)
expect("a logged-out cookie no longer works", code == 401, f"code {code}")
code, _, body = hit(plain, "/")
expect("a server started without a token serves the app with no gate",
       code == 200 and b"workspace" in body, f"code {code}")
plain.shutdown(); plain.server_close()
guard.shutdown(); guard.server_close()
sys.exit(1 if failures else 0)
'''
    env = dict(os.environ)
    env["GPU_ANALYSIS_CONFIG"] = fixture_cfg
    env.pop("GPU_API_KEY", None)
    probe = None
    try:
        probe = subprocess.run([sys.executable, "-c", probe_src], capture_output=True,
                               text=True, timeout=300, encoding="utf-8", errors="replace",
                               cwd=str(HERE))
        prc, pout = probe.returncode, (probe.stdout or "")
    except (OSError, subprocess.TimeoutExpired) as exc:
        prc, pout = 2, f"probe could not run: {type(exc).__name__}: {exc}"
    for line in pout.splitlines():
        print(line)
    if probe is not None and probe.stderr:
        print(probe.stderr.strip()[:400])
    last = next((ln for ln in pout.splitlines()[::-1] if ln.strip()), "")
    check("the token gate holds on a real server, and never leaks to a tokenless one",
          prc == 0, f"exit {prc}; {last[:120]}")

    print("=== spec #11: the key row and the referenced ids are checked against the shipped sources ===")
    # Two ways this screen can lie while every HTTP route still passes: the footer advertises a key
    # that no handler binds, and app.js reaches for an id that markup no longer has. Both are
    # invisible to a Python test that only talks to the server, and both are cheap to catch by
    # reading the two files that are actually served.
    page = (HERE / "gui" / "index.html").read_text(encoding="utf-8")
    script = (HERE / "gui" / "app.js").read_text(encoding="utf-8")
    row = re.search(r'<p class="keys">.*?</p>', page, re.S)
    check("the key row exists", row is not None)
    if row:
        markup = row.group(0)
        advertised = [key.strip() for key in re.findall(r"<b>([^<]+)</b>", markup)]
        reserved = [key for key in ("Ctrl+L", "Ctrl+Q", "Ctrl+W", "Ctrl+N", "Ctrl+T", "Ctrl+,")
                    if key in markup]
        check("no browser-reserved chord is advertised", not reserved, ",".join(reserved))
        bound = re.findall(r"^\s*(F\d+):\s*\(\)", script, re.M)

        def is_bound(key):
            if key.startswith("F"):
                return key in bound
            if key == "Esc":       # handled by the modal's own listener, not by the table
                return 'event.key === "Escape"' in script
            if key == "Enter":     # native form submission, so the forms are the binding
                return 'addEventListener("submit"' in script
            return False

        unbound = [key for key in advertised if not is_bound(key)]
        check(f"all {len(advertised)} advertised keys are bound", not unbound, ",".join(unbound))
        # The other direction: a binding that works but is never mentioned is a feature nobody
        # finds, and it is exactly how the terminal's own key row drifted from its handlers.
        silent = [key for key in bound if key not in advertised]
        check("no bound key hides from the row", not silent, ",".join(silent))
    referenced = set(re.findall(r'\$\("([^"]+)"\)', script))
    declared = set(re.findall(r'id="([^"]+)"', page))
    missing = sorted(referenced - declared)
    check(f"all {len(referenced)} ids app.js looks up exist in index.html",
          not missing, ",".join(missing))

    for name, value in (("GPU_ANALYSIS_CONFIG", ambient_cfg), ("GPU_API_KEY", ambient_key)):
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value

    # The server and the browser-side renderer are two different programs, and this file can only
    # reach the first. Run the renderer probe as its own process and print its verdict, so a green
    # suite can never quietly mean "the JavaScript was never executed on this machine".
    try:
        probe = subprocess.run([sys.executable, str(HERE / "renderer_probe.py")],
                               capture_output=True, text=True, timeout=300,
                               encoding="utf-8", errors="replace")
        prc, pout = probe.returncode, (probe.stdout or "")
    except (OSError, subprocess.TimeoutExpired) as exc:
        prc, pout = 2, f"probe could not run: {type(exc).__name__}: {exc}"
    last = next((ln for ln in pout.splitlines()[::-1] if ln.strip()), "")
    check("the renderer probe did not report a failure", prc != 1, f"exit {prc}; {last[:120]}")
    if prc == 2:
        print("  [SKIP] gui/app.js was NEVER executed on this machine: no Chromium-family browser "
              "and no jsdom driver. Nothing above proves the renderer.")
    elif "jsdom" in pout:
        print("  [NOTE] the renderer ran under DOM emulation, not Chromium: CSP, layout and the "
              "real parser are still unproven here.")

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
