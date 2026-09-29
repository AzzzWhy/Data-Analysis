"""Deterministic workbench lifecycle tests; no server, GPU, real config or API.

Run beside gui.py, or use --agent-dir /path/to/repo/agent for a scratch copy.
Only model/client, persistence, and worker boundaries are replaced; Workbench,
Job, its admission locks and event completion run as product code.
"""
from __future__ import annotations

import argparse
from dataclasses import replace
import importlib
from pathlib import Path
import sys
import types
import unittest
from unittest import mock


parser = argparse.ArgumentParser(add_help=False)
parser.add_argument("--agent-dir", type=Path, default=Path(__file__).resolve().parent)
options, unittest_args = parser.parse_known_args()
sys.path.insert(0, str(options.agent_dir.resolve()))

# Make this regression runnable even without the optional model SDK. No client
# produced by this boundary double can issue a network request.
sdk = types.ModuleType("openai")
sdk.OpenAI = mock.Mock
sdk.APIError = type("APIError", (Exception,), {})
sdk.__version__ = "test"
sys.modules["openai"] = sdk
gui = importlib.import_module("gui")


class FixtureAgent:
    def __init__(self, client, **kwargs):
        self.client = client
        self.model = kwargs.get("model")
        self.reuse_one_shot = kwargs.get("reuse_one_shot", False)
        self.event_sink = None
        self.result_sink = None
        self.messages = [{"role": "assistant", "content": "existing history"}]
        self.run = mock.Mock(return_value="synthetic final answer")

    def safe_error(self, exc):
        return type(exc).__name__


class WorkbenchLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.config = gui.APIConfig(base_url="http://127.0.0.1:1/v1",
                                    api_key="synthetic-regression-key", model="fixture-model")
        self.saved = []
        patches = (
            mock.patch.object(gui, "load_config", side_effect=lambda: replace(self.config)),
            mock.patch.object(gui, "save_config", side_effect=lambda config: self.saved.append(replace(config))),
            mock.patch.object(gui, "build_client", return_value=object()),
            mock.patch.object(gui, "Agent", FixtureAgent),
            mock.patch.object(gui.Workbench, "session_event", return_value={"sessions": 0}),
        )
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        self.wb = gui.Workbench()

    def suspended_ask(self):
        """Capture the thread handoff without nondeterministic scheduling."""
        with mock.patch.object(gui.threading, "Thread") as thread:
            job, reason = self.wb.start_ask("a fixture question", "fixture.csv")
            self.assertEqual(reason, "")
            self.assertIsNotNone(job)
            thread.return_value.start.assert_called_once_with()
            launch = thread.call_args.kwargs
        return job, launch["target"], launch["args"]

    def test_busy_release_reset_and_settings_are_non_mutating(self):
        original = self.wb.agent
        self.wb.busy = True
        with mock.patch.object(self.wb, "_release") as release, \
                mock.patch.object(self.wb, "_reset_all") as reset:
            for response in (self.wb.release(), self.wb.reset_all(),
                             self.wb.apply_settings({"model": "must-not-apply"})):
                self.assertIs(response.get("ok"), False)
                self.assertTrue(response["error"].startswith("busy:"))
                self.assertTrue(self.wb.busy)
            release.assert_not_called()
            reset.assert_not_called()
        self.assertIs(self.wb.agent, original)
        self.assertEqual(self.saved, [])

    def test_release_and_reset_hold_admission_until_their_cleanup_finishes(self):
        for public, implementation in (("release", "_release"), ("reset_all", "_reset_all")):
            with self.subTest(operation=public):
                def cleanup(*_args):
                    self.assertTrue(self.wb.busy)
                    self.assertIsNone(self.wb._acquire("run", "must-not-start"))
                    self.assertFalse(self.wb.apply_settings({"model": "must-not-apply"})["ok"])
                    return {"ok": True}

                with mock.patch.object(self.wb, implementation, side_effect=cleanup):
                    self.assertTrue(getattr(self.wb, public)()["ok"])
                self.assertFalse(self.wb.busy)

    def test_cleanup_exception_releases_its_own_admission(self):
        for public, implementation in (("release", "_release"), ("reset_all", "_reset_all")):
            with self.subTest(operation=public):
                with mock.patch.object(self.wb, implementation, side_effect=RuntimeError("fixture failure")):
                    with self.assertRaises(RuntimeError):
                        getattr(self.wb, public)()
                self.assertFalse(self.wb.busy)

    def test_ask_hands_off_the_captured_agent_and_always_finishes(self):
        captured = self.wb.agent
        job, target, args = self.suspended_ask()
        self.assertIs(args[-1], captured)
        self.assertTrue(self.wb.busy)
        # Simulate a replacement at the handoff boundary. Public settings cannot
        # do this while busy; the thread must still not dereference mutable state.
        replacement = FixtureAgent(object())
        replacement.event_sink = object()
        replacement.result_sink = object()
        replacement_sinks = (replacement.event_sink, replacement.result_sink)
        self.wb.agent = replacement
        target(*args)
        captured.run.assert_called_once()
        replacement.run.assert_not_called()
        self.assertEqual((replacement.event_sink, replacement.result_sink), replacement_sinks)
        self.assertIsNone(captured.event_sink)
        self.assertIsNone(captured.result_sink)
        self.assertTrue(job.done)
        self.assertFalse(self.wb.busy)
        names = [name for _, name, _ in job.events]
        self.assertIn("answer", names)
        self.assertEqual(names[-1], "done")

    def test_model_failure_still_finishes_and_releases_the_job(self):
        agent = self.wb.agent
        agent.run.side_effect = RuntimeError("fixture failure")
        job, target, args = self.suspended_ask()
        target(*args)
        self.assertTrue(job.done)
        self.assertFalse(self.wb.busy)
        self.assertIsNone(agent.event_sink)
        self.assertIsNone(agent.result_sink)
        names = [name for _, name, _ in job.events]
        self.assertIn("error", names)
        self.assertEqual(names[-1], "done")

    def test_failed_client_construction_cannot_keep_the_previous_agent(self):
        with mock.patch.object(gui, "build_client", side_effect=RuntimeError(self.config.api_key)):
            response = self.wb.apply_settings({"model": "new-fixture-model"})
        self.assertFalse(response["config_ready"])
        self.assertFalse(self.wb.config_ready)
        self.assertIsNone(self.wb.agent)
        self.assertNotIn(self.config.api_key, self.wb.last_error)
        with mock.patch.object(gui.threading, "Thread") as thread:
            job, reason = self.wb.start_ask("must not use the old client")
        self.assertIsNone(job)
        self.assertTrue(reason)
        self.assertFalse(self.wb.busy)
        thread.assert_not_called()

    def test_configuration_read_failure_cannot_keep_the_previous_agent(self):
        with mock.patch.object(gui, "load_config", side_effect=OSError("fixture unreadable")):
            self.wb._configure(None)
        self.assertIsNone(self.wb.agent)
        self.assertFalse(self.wb.config_ready)

    def test_language_only_change_preserves_history_and_runtime_key(self):
        original = self.wb.agent
        original_messages = original.messages
        response = self.wb.apply_settings({"language": "en"})
        self.assertTrue(response["ok"])
        self.assertIs(self.wb.agent, original)
        self.assertIs(original.messages, original_messages)
        self.assertEqual(self.wb._config.api_key, self.config.api_key)
        self.assertEqual(self.wb.language, "en")


if __name__ == "__main__":
    unittest.main(argv=[sys.argv[0], *unittest_args])
