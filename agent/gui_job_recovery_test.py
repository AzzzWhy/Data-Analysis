"""HTTP/job recovery contract tests with isolated skill and model boundaries.

Run: python -B agent/gui_job_recovery_test.py
Scratch: python -B gui_job_recovery_test.py --agent-dir /path/to/repo/agent

Only loopback ephemeral ports are used. No real credentials, configuration, GPU,
datasets or model endpoints are read. Job, Workbench, HTTP authentication and SSE
replay are product code; external execution boundaries are deterministic doubles.
"""
from __future__ import annotations

import argparse
from dataclasses import replace
import importlib
import json
from pathlib import Path
import sys
import threading
import time
import types
import unittest
from unittest import mock
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


parser = argparse.ArgumentParser(add_help=False)
parser.add_argument("--agent-dir", type=Path, default=Path(__file__).resolve().parent)
parser.add_argument("--support-agent-dir", type=Path, action="append", default=[],
                    help="optional support module directory for scratch gui.py; may be repeated")
options, unittest_args = parser.parse_known_args()
sys.path.insert(0, str(options.agent_dir.resolve()))
for index, directory in enumerate(options.support_agent_dir, start=1):
    sys.path.insert(index, str(directory.resolve()))

# Install boundary doubles BEFORE importing gui. This intentionally prevents the
# real skills module's worker hooks or the real Agent's imports from running.
skills = types.ModuleType("skills")
skills.analyze_dataset = lambda file_path="": json.dumps({"success": True, "rows": 3})
skills.dataset_session = lambda operation="list": json.dumps({"success": True, "sessions": []})
skills.release_all_sessions_and_cache = lambda: {"success": True}
sys.modules["skills"] = skills
sdk = types.ModuleType("openai")
sdk.OpenAI = mock.Mock
sdk.APIError = type("APIError", (Exception,), {})
sys.modules["openai"] = sdk


class FixtureAgent:
    def __init__(self, client=None, **kwargs):
        self.client = client
        self.model = kwargs.get("model")
        self.event_sink = None
        self.result_sink = None
        self.last_run_outcome = {"status": "succeeded", "reason": "fixture"}
        self.run = mock.Mock(return_value="fixture answer")

    def safe_error(self, error):
        return type(error).__name__


model = types.ModuleType("agent_main")
model.Agent = FixtureAgent
model.build_client = lambda config: object()
sys.modules["agent_main"] = model
gui = importlib.import_module("gui")


def parse_sse(raw):
    """Preserve absent ids: a replay-gap notice is not a stored job event."""
    events = []
    for block in raw.replace("\r\n", "\n").split("\n\n"):
        fields = {}
        for line in block.splitlines():
            if line.startswith(":") or ":" not in line:
                continue
            key, value = line.split(":", 1)
            fields[key] = value.lstrip(" ")
        if "data" in fields:
            events.append({"name": fields.get("event", "message"),
                           "id": fields.get("id"), "data": json.loads(fields["data"])})
    return events


class JobRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.config = gui.APIConfig(base_url="http://127.0.0.1:1/v1",
                                    api_key="synthetic-never-sent-secret", model="fixture")
        self.patches = (
            mock.patch.object(gui, "load_config", side_effect=lambda: replace(self.config)),
            mock.patch.object(gui, "save_config", side_effect=AssertionError("must not persist configuration")),
            mock.patch.object(gui, "load_gate_credential", side_effect=AssertionError("must not read gate files")),
            mock.patch.object(gui.Workbench, "session_event", return_value={"sessions": 0}),
        )
        for patch in self.patches:
            patch.start()
            self.addCleanup(patch.stop)
        self.servers = []
        self.addCleanup(self.stop_servers)
        self.httpd, self.wb, self.base, self.cookie = self.new_server()

    def stop_servers(self):
        for server, thread in reversed(self.servers):
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def new_server(self):
        server, wb = gui.make_server(port=0, token="synthetic-access-password")
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .02}, daemon=True)
        thread.start()
        self.servers.append((server, thread))
        base = f"http://127.0.0.1:{server.server_address[1]}"
        request = Request(base + "/api/login", data=json.dumps({"password": "synthetic-access-password"}).encode(),
                          headers={"Content-Type": "application/json"}, method="POST")
        with urlopen(request, timeout=3) as response:
            self.assertEqual(response.status, 200)
            cookie = response.headers["Set-Cookie"].split(";", 1)[0]
        return server, wb, base, cookie

    def request(self, path, *, cookie=True, headers=None, base=None, data=None):
        given = dict(headers or {})
        if cookie:
            given["Cookie"] = cookie if isinstance(cookie, str) else self.cookie
        if data is not None:
            given["Content-Type"] = "application/json"
        request = Request((base or self.base) + path, headers=given,
                          data=None if data is None else json.dumps(data).encode())
        try:
            response = urlopen(request, timeout=3)
        except HTTPError as error:
            response = error
        with response:
            body = response.read().decode("utf8")
            content_type = response.headers.get("Content-Type", "")
            return response.status, json.loads(body) if "application/json" in content_type else body

    def jobs(self, **kwargs):
        status, payload = self.request("/api/jobs", **kwargs)
        self.assertEqual(status, 200, payload)
        self.assertIsInstance(payload.get("instance_id"), str)
        self.assertTrue(payload["instance_id"])
        return payload

    def detail(self, job):
        status, payload = self.request("/api/jobs?" + urlencode({"job": job.id}))
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["job"]["id"], job.id)
        return payload["job"]

    def events(self, job, *, after=0, instance=None, headers=None):
        if instance is None:
            instance = self.jobs()["instance_id"]
        query = urlencode({"job": job.id, "instance": instance, "after": after})
        status, body = self.request("/api/events?" + query, headers=headers)
        self.assertEqual(status, 200, body)
        return parse_sse(body)

    def run_direct(self, result=None, error=None):
        job = self.wb._acquire("run", "analyze_dataset")
        self.assertIsNotNone(job)

        def run(file_path=""):
            if error is not None:
                raise error
            return result if isinstance(result, str) else json.dumps(result or {"success": True, "rows": 3})

        with mock.patch.object(skills, "analyze_dataset", side_effect=run) as call:
            self.wb._run_direct(job, "analyze_dataset", {"file_path": "fixture.csv"})
            call.assert_called_once()
        self.assertTrue(job.done)
        self.assertFalse(self.wb.busy)
        return job

    def assert_done(self, job, status, success):
        detail = self.detail(job)
        self.assertEqual(detail["status"], status)
        self.assertIs(detail["done"], True)
        done = [event for event in self.events(job) if event["name"] == "done"]
        self.assertEqual(len(done), 1)
        self.assertEqual(done[0]["data"]["status"], status)
        self.assertIs(done[0]["data"]["success"], success)
        self.assertIsInstance(done[0]["data"]["seconds"], (int, float))
        self.assertGreaterEqual(done[0]["data"]["seconds"], 0)

    def test_authentication_precedes_job_and_instance_disclosure(self):
        job = self.run_direct()
        for path in ("/api/jobs", "/api/jobs?job=" + job.id,
                     "/api/events?" + urlencode({"job": job.id, "instance": "wrong", "after": "bad"})):
            with self.subTest(path=path):
                status, data = self.request(path, cookie=False)
                self.assertEqual(status, 401)
                self.assertNotIn(job.id, json.dumps(data))
                self.assertNotIn("instance_id", data)
        status, _ = self.request("/api/jobs", cookie="dws=missing-session")
        self.assertEqual(status, 401)
        self.assertEqual(len(self.jobs()["jobs"]), 1)

    def test_ids_unique_even_in_same_thread_same_millisecond(self):
        with mock.patch.object(gui.time, "time", return_value=1_700_000_000.123):
            jobs = [gui.Job("run", "fixture") for _ in range(500)]
        self.assertEqual(len({job.id for job in jobs}), len(jobs))

    def test_job_metadata_tracks_running_then_finished_state(self):
        job = self.wb._acquire("run", "analyze_dataset")
        running = self.jobs()
        self.assertEqual(running["active_job_id"], job.id)
        before = self.detail(job)
        self.assertEqual(before["status"], "running")
        self.assertIs(before["done"], False)
        self.assertEqual(before["kind"], "run")
        self.assertEqual(before["label"], "analyze_dataset")
        self.assertIsInstance(before["started_at"], (int, float))
        self.assertLess(abs(before["started_at"] - time.time()), 10)
        self.assertIsNone(before["finished_at"])
        self.assertEqual(before["sequence"], 0)
        with mock.patch.object(skills, "analyze_dataset", return_value='{"success": true}'):
            self.wb._run_direct(job, "analyze_dataset", {})
        after = self.detail(job)
        self.assertGreaterEqual(after["finished_at"], before["started_at"])
        self.assertEqual(after["sequence"], job.sequence)
        self.assertEqual(after["first_sequence"], job.events[0][0])
        self.assertIsNone(self.jobs()["active_job_id"])
        self.assert_done(job, "succeeded", True)

    def test_bounded_history_keeps_twenty_total_including_active_newest_first(self):
        completed = [self.run_direct() for _ in range(gui.JOB_HISTORY + 4)]
        active = self.wb._acquire("run", "still-running")
        listing = self.jobs()
        self.assertEqual(listing["active_job_id"], active.id)
        self.assertEqual([item["id"] for item in listing["jobs"]],
                         [active.id] + [job.id for job in reversed(completed[-(gui.JOB_HISTORY - 1):])])
        for oldest in completed[:-(gui.JOB_HISTORY - 1)]:
            self.assertEqual(self.request("/api/jobs?job=" + oldest.id)[0], 404)

    def test_replay_cursor_emits_monotonic_ids_and_does_not_execute(self):
        job = self.run_direct()
        with mock.patch.object(self.wb, "start_direct_run", side_effect=AssertionError("replay must not rerun")), \
                mock.patch.object(self.wb, "start_ask", side_effect=AssertionError("replay must not ask")), \
                mock.patch.object(skills, "analyze_dataset", side_effect=AssertionError("replay must not execute")):
            full = self.events(job)
            ids = [int(event["id"]) for event in full]
            self.assertEqual(ids, sorted(set(ids)))
            self.assertEqual(ids, [item[0] for item in job.events])
            cursor = ids[1]
            replay = self.events(job, after=cursor)
            self.assertEqual([int(event["id"]) for event in replay], [item for item in ids if item > cursor])
            self.assertEqual(self.events(job, after=job.sequence), [])
            self.assertEqual(self.events(job), full)
        self.assertEqual(len(self.wb.jobs), 1)

    def test_disconnected_stream_does_not_cancel_or_restart_running_tool(self):
        entered = threading.Event()
        release = threading.Event()

        def slow_tool(file_path=""):
            entered.set()
            if not release.wait(timeout=3):
                raise TimeoutError("test did not release fixture")
            return '{"success": true, "rows": 3}'

        with mock.patch.object(skills, "analyze_dataset", side_effect=slow_tool) as call:
            try:
                status, payload = self.request("/api/run", data={"tool": "analyze_dataset", "args": {}})
                self.assertEqual(status, 202, payload)
                self.assertTrue(entered.wait(timeout=2))
                job = self.wb.jobs[payload["job_id"]]
                query = urlencode({"job": job.id, "instance": self.jobs()["instance_id"], "after": 0})
                request = Request(self.base + "/api/events?" + query, headers={"Cookie": self.cookie})
                with urlopen(request, timeout=3) as stream:
                    first_frame = []
                    while True:
                        line = stream.readline().decode("utf8")
                        if not line.strip():
                            break
                        first_frame.append(line)
                    initial = parse_sse("".join(first_frame))
                    self.assertEqual(len(initial), 1)
                    cursor = int(initial[0]["id"])
                self.assertTrue(self.wb.busy)
                self.assertFalse(job.done)
            finally:
                release.set()
            with job.condition:
                self.assertTrue(job.condition.wait_for(lambda: job.done, timeout=2))
            replay = self.events(job, after=cursor)
            self.assertTrue(all(int(event["id"]) > cursor for event in replay))
            self.assertEqual(replay[-1]["name"], "done")
            self.assertIs(replay[-1]["data"]["success"], True)
            self.assertFalse(self.wb.busy)
            call.assert_called_once()

    def test_last_event_id_header_wins_over_query_cursor(self):
        job = self.run_direct()
        ids = [item[0] for item in job.events]
        after = ids[1]
        replay = self.events(job, after=0, headers={"Last-Event-ID": str(after)})
        self.assertEqual([int(event["id"]) for event in replay], [item for item in ids if item > after])
        replay = self.events(job, after="bad-query-ignored", headers={"Last-Event-ID": str(after)})
        self.assertEqual([int(event["id"]) for event in replay], [item for item in ids if item > after])

    def test_invalid_cursor_and_instance_mismatch_are_explicit_http_errors(self):
        job = self.run_direct()
        instance = self.jobs()["instance_id"]
        for value in ("-1", "abc", "1.5", "", "NaN"):
            with self.subTest(after=value):
                status, _ = self.request("/api/events?" + urlencode({"job": job.id, "instance": instance, "after": value}))
                self.assertEqual(status, 400)
        status, _ = self.request("/api/events?" + urlencode({"job": job.id, "instance": instance, "after": 0}),
                                 headers={"Last-Event-ID": "invalid"})
        self.assertEqual(status, 400)
        status, _ = self.request("/api/events?" + urlencode({"job": job.id, "instance": "stale-backend", "after": 0}))
        self.assertEqual(status, 409)
        self.assertEqual(self.request("/api/jobs?job=missing")[0], 404)
        self.assertEqual(self.request("/api/events?" + urlencode({"job": "missing", "instance": instance, "after": 0}))[0], 404)

    def test_truncated_replay_warns_and_delivers_retained_suffix_once(self):
        job = self.wb._acquire("run", "buffered")
        for index in range(gui.EVENT_BUFFER + 20):
            job.emit("trace", {"line": str(index)})
        with mock.patch.object(skills, "analyze_dataset", return_value='{"success": true}'):
            self.wb._run_direct(job, "analyze_dataset", {})
        first = job.events[0][0]
        self.assertGreater(first, 1)
        replay = self.events(job, after=0)
        self.assertEqual(replay[0]["name"], "replay_gap")
        self.assertEqual(replay[0]["data"]["first_sequence"], first)
        self.assertEqual(replay[0]["data"]["requested_after"], 0)
        stored = [event for event in replay if event["name"] != "replay_gap"]
        self.assertEqual([int(event["id"]) for event in stored], [row[0] for row in job.events])
        self.assertEqual(len(stored), gui.EVENT_BUFFER)
        no_gap = self.events(job, after=first - 1)
        self.assertNotIn("replay_gap", [event["name"] for event in no_gap])
        self.assertEqual(self.detail(job)["first_sequence"], first)

    def test_tool_failure_and_invalid_payload_never_report_success(self):
        for payload in ({"success": False, "error": "fixture tool rejected"},
                        {"ok": False}, {"success": True, "result": {"success": False}},
                        {"success": True, "result": {"detail": {"error": "nested failure"}}},
                        {"isError": True},
                        "not-json", "[]", "null"):
            with self.subTest(payload=payload):
                self.assert_done(self.run_direct(result=payload), "failed", False)
        job = self.run_direct(error=RuntimeError("fixture tool failure"))
        self.assert_done(job, "failed", False)
        names = [event["name"] for event in self.events(job)]
        self.assertIn("error", names)
        self.assertNotIn("transporterror", names)

    def test_statistics_columns_named_error_or_success_are_not_failures(self):
        payload = {"success": True, "result": {"summary": {
            "error": {"count": 4, "mean": .25, "min": 0, "max": 1},
            "success": {"count": 4, "mean": .75},
            "ok": {"count": 4, "mean": .75},
        }, "records": [{"error": "a user data value", "success": False}]}}
        self.assert_done(self.run_direct(result=payload), "succeeded", True)

    def test_model_exception_is_failed_application_error_not_transport_error(self):
        agent = self.wb.agent
        agent.run.side_effect = RuntimeError("synthetic-never-sent-secret")
        job = self.wb._acquire("ask", "fixture prompt")
        self.wb._run_ask(job, "fixture prompt", agent)
        self.assert_done(job, "failed", False)
        events = self.events(job)
        self.assertIn("error", [event["name"] for event in events])
        self.assertNotIn("transporterror", [event["name"] for event in events])
        self.assertNotIn(self.config.api_key, json.dumps(events))
        self.assertIsNone(agent.event_sink)
        self.assertIsNone(agent.result_sink)
        self.assertFalse(self.wb.busy)

    def test_caught_model_failure_outcome_is_not_a_successful_answer(self):
        agent = self.wb.agent
        agent.last_run_outcome = {"status": "failed", "reason": "model_error"}
        agent.run.return_value = "The model call failed."
        job = self.wb._acquire("ask", "fixture prompt")
        self.wb._run_ask(job, "fixture prompt", agent)
        self.assert_done(job, "failed", False)

    def test_legacy_adapter_unknown_outcome_is_not_asserted_successful(self):
        agent = self.wb.agent
        del agent.last_run_outcome
        job = self.wb._acquire("ask", "fixture prompt")
        self.wb._run_ask(job, "fixture prompt", agent)
        self.assert_done(job, "partial", False)

    def test_successful_model_outcome_stays_successful_without_tool_failures(self):
        agent = self.wb.agent
        job = self.wb._acquire("ask", "fixture prompt")
        self.wb._run_ask(job, "fixture prompt", agent)
        self.assert_done(job, "succeeded", True)

    def test_answer_with_failed_tool_is_partial_not_success(self):
        agent = self.wb.agent

        def answer(_text):
            agent.result_sink("analyze_dataset", {"success": True, "rows": 3}, .01)
            agent.result_sink("analyze_dataset", {"success": False, "error": "fixture"}, .01)
            return "One analysis succeeded; one failed."

        agent.run.side_effect = answer
        job = self.wb._acquire("ask", "fixture prompt")
        self.wb._run_ask(job, "fixture prompt", agent)
        self.assert_done(job, "partial", False)

    def test_instances_do_not_share_history_and_old_instance_is_rejected(self):
        job = self.run_direct()
        before = self.jobs()
        _server, _wb, base, cookie = self.new_server()
        after = self.jobs(base=base, cookie=cookie)
        self.assertNotEqual(after["instance_id"], before["instance_id"])
        self.assertEqual(after["jobs"], [])
        self.assertEqual(self.jobs()["instance_id"], before["instance_id"], "starting a server must not replace another Handler's workbench")
        self.assertEqual(self.jobs()["jobs"][0]["id"], job.id)
        status, _ = self.request("/api/events?" + urlencode({"job": job.id, "instance": before["instance_id"], "after": 0}),
                                 base=base, cookie=cookie)
        self.assertEqual(status, 409, "instance mismatch must explain restart rather than generic missing job")


if __name__ == "__main__":
    unittest.main(argv=[sys.argv[0], *unittest_args])
