"""Actual Agent.run -> Workbench Job outcome bridge, with no network or workers."""
from __future__ import annotations

import argparse
from dataclasses import replace
import importlib
import json
from pathlib import Path
import sys
import types
import unittest
from unittest import mock

parser = argparse.ArgumentParser(add_help=False)
parser.add_argument("--agent-dir", type=Path, default=Path(__file__).resolve().parent)
parser.add_argument("--support-agent-dir", type=Path)
options, unittest_args = parser.parse_known_args()
if options.support_agent_dir:
    sys.path.insert(0, str(options.support_agent_dir.resolve()))
sys.path.insert(0, str(options.agent_dir.resolve()))
sdk = types.ModuleType("openai")
sdk.OpenAI = mock.Mock
sdk.APIError = type("APIError", (Exception,), {})
sdk.__version__ = "test"
sys.modules["openai"] = sdk
gui = importlib.import_module("gui")
agent_main = importlib.import_module("agent_main")

SECRET = "synthetic-bridge-secret"


class ExternalFixture:
    def __init__(self, _path=None):
        self.secrets = set()

    def sanitize(self, value):
        return value

    def redact(self, text):
        for secret in self.secrets:
            text = text.replace(secret, "[REDACTED]")
        return text


def reply(text="fixture answer", calls=None):
    return types.SimpleNamespace(choices=[types.SimpleNamespace(
        message=types.SimpleNamespace(content=text, tool_calls=calls))])


class OutcomeBridgeTests(unittest.TestCase):
    def setUp(self):
        self.create = mock.Mock()
        self.client = types.SimpleNamespace(api_key=SECRET, chat=types.SimpleNamespace(
            completions=types.SimpleNamespace(create=self.create)))
        self.config = gui.APIConfig(base_url="http://127.0.0.1:1/v1", model="fixture", api_key=SECRET)
        patches = (
            mock.patch.object(gui, "load_config", side_effect=lambda: replace(self.config)),
            mock.patch.object(gui, "build_client", return_value=self.client),
            mock.patch.object(gui, "save_config"),
            mock.patch.object(agent_main, "ExternalTools", ExternalFixture),
            mock.patch.object(agent_main, "load_skill_definitions", return_value=[]),
            mock.patch.object(agent_main.skills, "remember_request_text"),
            mock.patch.object(agent_main.skills, "close_all_sessions", return_value={"closed": 0}),
            mock.patch.object(gui.Workbench, "session_event", return_value={"sessions": 0}),
        )
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        self.workbench = gui.Workbench()
        self.assertIsInstance(self.workbench.agent, agent_main.Agent)

    def run_bridge(self):
        with mock.patch.object(gui.threading, "Thread") as thread:
            job, reason = self.workbench.start_ask("fixture question", "fixture.csv")
            self.assertEqual(reason, "")
            launch = thread.call_args.kwargs
        launch["target"](*launch["args"])
        self.assertTrue(job.done)
        self.assertFalse(self.workbench.busy)
        frames = list(job.events)
        terminal = [payload for _, name, payload in frames if name == "done"]
        self.assertEqual(len(terminal), 1)
        self.assertEqual(frames[-1][1], "done")
        self.assertEqual(job.status, terminal[0]["status"])
        self.assertNotIn(SECRET, json.dumps(frames))
        return terminal[0], frames

    def test_actual_model_api_failure_is_failed_with_safe_done_error(self):
        self.create.side_effect = RuntimeError(f"fixture denial {SECRET}")
        done, frames = self.run_bridge()
        self.assertEqual(done["status"], "failed")
        self.assertIs(done["success"], False)
        self.assertEqual(done["reason"], "model_api_error")
        self.assertIn("RuntimeError", done["error"])
        self.assertIn("[REDACTED]", done["error"])
        self.assertEqual(done["tool_failures"], 0)
        self.assertTrue(any(name == "answer" for _, name, _ in frames))

    def test_non_json_tool_failure_count_survives_absence_of_result_sink_event(self):
        call = types.SimpleNamespace(id="fixture-call", function=types.SimpleNamespace(
            name="analyze_dataset", arguments='{"file_path":"fixture.csv","operation":"summary"}'))
        self.create.side_effect = [reply(calls=[call]), reply("A tool failed.")]
        with mock.patch.object(agent_main, "execute_tool", return_value=("not JSON", 0.001)):
            done, frames = self.run_bridge()
        self.assertEqual(done["status"], "partial")
        self.assertIs(done["success"], False)
        self.assertEqual(done["reason"], "tool_failure")
        self.assertEqual(done["tool_failures"], 1)
        self.assertTrue(done["error"])
        self.assertFalse(any(name == "tool_result" for _, name, _ in frames))

    def test_cleanup_failure_is_partial_with_the_actual_reason_in_done(self):
        self.create.return_value = reply("A useful answer exists.")
        with mock.patch.object(agent_main.skills, "close_all_sessions", return_value={"error": SECRET}):
            done, _ = self.run_bridge()
        self.assertEqual(done["status"], "partial")
        self.assertEqual(done["reason"], "cleanup_failed")
        self.assertIs(done["success"], False)
        self.assertIn("[REDACTED]", done["error"])

    def test_followup_success_does_not_inherit_failed_done_state(self):
        self.create.side_effect = [RuntimeError("first failure"), reply("Recovered.")]
        first, _ = self.run_bridge()
        second, _ = self.run_bridge()
        self.assertEqual(first["status"], "failed")
        self.assertEqual(second["status"], "succeeded")
        self.assertIs(second["success"], True)
        self.assertFalse(second["error"])
        self.assertFalse(second["reason"])
        self.assertEqual(second["tool_failures"], 0)


if __name__ == "__main__":
    unittest.main(argv=[sys.argv[0], *unittest_args])
