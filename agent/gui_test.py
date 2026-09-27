#!/usr/bin/env python3
"""Headless tests for the workbench: no browser, no network, no GPU, no API calls.

Two kinds of thing run here, and the difference matters.

Most cases drive a real HTTP server bound to port 0 against the real `skills` layer and a real
session worker, because a mock of the thing under test proves nothing about it. Where a model is
needed to reach a code path, `_stub_openai_module()` installs a double at the *SDK boundary* --
clearly named, never used to fake an answer the UI shows. The distinction is the whole point of
the previous attempt at this screen, which had no backend at all.

Spec §10 case #7 (`/api/settings` must not echo the key) is not here because that endpoint is not
built yet; it belongs to the round that wires up a real model client.
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


def check(label, ok, detail=""):
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
    check("an unconfigured model is reported, not hidden",
          (state["agent_available"] and not state["config_ready"])
          or not state["agent_available"], json.dumps({k: state[k] for k in
                                                       ("agent_available", "config_ready")}))

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

    httpd.shutdown()
    gui._shutdown(wb)

    print()
    check("the openai double never pulled in a real HTTP client",
          "httpx" not in sys.modules,
          f"httpx in sys.modules: {'httpx' in sys.modules}")
    if FAILS:
        print(f"FAILED ({len(FAILS)}): {FAILS}")
        return 1
    print("ALL WORKBENCH CHECKS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
