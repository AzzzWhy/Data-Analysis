"""Isolated Agent outcome contract tests: no SDK, model, worker or real config."""
from __future__ import annotations

import argparse
import importlib
import json
from pathlib import Path
import sys
import types
import unittest
from unittest import mock

parser = argparse.ArgumentParser(add_help=False)
parser.add_argument("--agent-dir", type=Path, default=Path(__file__).resolve().parent)
parser.add_argument("--support-agent-dir", type=Path,
                    help="Optional repository agent/ directory for scratch-copy dependencies")
options, unittest_args = parser.parse_known_args()
if options.support_agent_dir:
    sys.path.insert(0, str(options.support_agent_dir.resolve()))
sys.path.insert(0, str(options.agent_dir.resolve()))
sdk = types.ModuleType("openai")
sdk.OpenAI = mock.Mock
sdk.APIError = type("APIError", (Exception,), {})
sdk.__version__ = "test"
sys.modules["openai"] = sdk
agent_main = importlib.import_module("agent_main")
execution_outcome = importlib.import_module("execution_outcome")

SECRET = "synthetic-outcome-secret"
EXTERNAL_SECRET = "synthetic-external-secret"


class ExternalFixture:
    def __init__(self, _path=None):
        self.secrets = {EXTERNAL_SECRET}

    def redact(self, text):
        for value in self.secrets:
            text = text.replace(value, "[REDACTED]")
        return text

    def sanitize(self, value):
        return value


def reply(text="answer", *, calls=None, empty_choices=False):
    message = types.SimpleNamespace(content=text, tool_calls=calls)
    return types.SimpleNamespace(choices=[] if empty_choices else [types.SimpleNamespace(message=message)])


def tool_call():
    return types.SimpleNamespace(id="fixture-call", function=types.SimpleNamespace(
        name="analyze_dataset", arguments='{"file_path":"fixture.csv","operation":"summary"}'))


class RunOutcomeTests(unittest.TestCase):
    def setUp(self):
        patches = (
            mock.patch.object(agent_main, "ExternalTools", ExternalFixture),
            mock.patch.object(agent_main, "load_skill_definitions", return_value=[]),
            mock.patch.object(agent_main.skills, "remember_request_text"),
            mock.patch.object(agent_main.skills, "close_all_sessions", return_value={"closed": 0}),
        )
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def agent(self, *responses):
        create = mock.Mock(side_effect=list(responses))
        client = types.SimpleNamespace(api_key=SECRET, chat=types.SimpleNamespace(
            completions=types.SimpleNamespace(create=create)))
        agent = agent_main.Agent(client, verbose=False, model="fixture-model")
        # Prevent ambient NO_TOOLS from changing the test's redaction boundary.
        if agent.external_tools is None:
            agent.external_tools = ExternalFixture()
        return agent

    def assert_outcome(self, agent, status, reason=None, failures=0):
        outcome = agent.last_run_outcome
        self.assertEqual(outcome["status"], status)
        self.assertEqual(outcome.get("reason"), reason)
        self.assertEqual(outcome["tool_failures"], failures)
        serialized = json.dumps(outcome)
        self.assertNotIn(SECRET, serialized)
        self.assertNotIn(EXTERNAL_SECRET, serialized)

    def test_api_failure_then_success_resets_status_and_redacts_secrets(self):
        agent = self.agent(RuntimeError(f"denied {SECRET} {EXTERNAL_SECRET}"), reply("recovered"))
        text = agent.run("first")
        self.assertIsInstance(text, str)
        self.assertNotIn(SECRET, text)
        self.assertNotIn(EXTERNAL_SECRET, text)
        self.assert_outcome(agent, "failed", "model_api_error")
        failed_outcome = agent.last_run_outcome
        self.assertEqual(agent.run("second"), "recovered")
        self.assert_outcome(agent, "succeeded")
        self.assertNotIn("error", agent.last_run_outcome)
        self.assertEqual(failed_outcome["status"], "failed")

    def test_missing_configuration_is_a_structured_failure(self):
        agent = self.agent(reply())
        agent.client = None
        self.assertIsInstance(agent.run("question"), str)
        self.assert_outcome(agent, "failed", "not_configured")

    def test_empty_choices_and_empty_final_answers_fail(self):
        cases = ((reply(empty_choices=True), "model_empty_response"),
                 (reply(""), "model_empty_answer"),
                 (reply(" \n\t"), "model_empty_answer"),
                 (reply(None), "model_empty_answer"))
        for response, reason in cases:
            with self.subTest(reason=reason):
                agent = self.agent(response)
                self.assertIsInstance(agent.run("question"), str)
                self.assert_outcome(agent, "failed", reason)

    def test_literal_failure_words_in_a_real_answer_are_not_classified_as_error(self):
        text = "The literal string [model call failed] is only an example."
        agent = self.agent(reply(text))
        self.assertEqual(agent.run("explain that string"), text)
        self.assert_outcome(agent, "succeeded")

    def test_tool_failure_is_partial_even_when_model_claims_success(self):
        agent = self.agent(reply(calls=[tool_call()]), reply("Everything succeeded."), reply("clean next turn"))
        failure = {"success": True, "result": {"ok": False, "error": "fixture failure"}}
        with mock.patch.object(agent_main, "execute_tool", return_value=(json.dumps(failure), 0.001)):
            self.assertEqual(agent.run("first"), "Everything succeeded.")
        self.assert_outcome(agent, "partial", "tool_failure", 1)
        self.assertEqual(agent.run("second"), "clean next turn")
        self.assert_outcome(agent, "succeeded")

    def test_multiple_failures_count_calls_not_nested_failed_steps(self):
        agent = self.agent(reply(calls=[tool_call(), tool_call()]), reply("Some work failed."))
        failure = {"success": True, "results": [{"ok": False}, {"success": False}]}
        with mock.patch.object(agent_main, "execute_tool", return_value=(json.dumps(failure), 0.001)):
            agent.run("question")
        self.assert_outcome(agent, "partial", "tool_failure", 2)

    def test_invalid_tool_json_is_not_reported_as_full_success(self):
        agent = self.agent(reply(calls=[tool_call()]), reply("Received unusable output."))
        with mock.patch.object(agent_main, "execute_tool", return_value=("not JSON", 0.001)):
            agent.run("question")
        self.assert_outcome(agent, "partial", "tool_failure", 1)

    def test_round_limit_with_summary_is_partial(self):
        agent = self.agent(reply(calls=[tool_call()]), reply("Summary of completed work."))
        with mock.patch.object(agent_main, "MAX_TOOL_ROUNDS", 1), \
                mock.patch.object(agent_main, "execute_tool", return_value=('{"success":true}', 0.001)):
            self.assertEqual(agent.run("question"), "Summary of completed work.")
        self.assert_outcome(agent, "partial", "tool_round_limit")

    def test_failed_or_empty_round_limit_summary_is_failed(self):
        for final, reason in ((RuntimeError(SECRET), "model_summary_error"),
                              (reply(empty_choices=True), "model_summary_empty_response"),
                              (reply(" "), "model_summary_empty_answer")):
            with self.subTest(reason=reason):
                agent = self.agent(reply(calls=[tool_call()]), final)
                with mock.patch.object(agent_main, "MAX_TOOL_ROUNDS", 1), \
                        mock.patch.object(agent_main, "execute_tool", return_value=('{"success":true}', 0.001)):
                    text = agent.run("question")
                self.assertNotIn(SECRET, text)
                self.assert_outcome(agent, "failed", reason)

    def test_cleanup_failure_makes_a_useful_answer_partial(self):
        cases = (RuntimeError(SECRET), {"error": SECRET}, {"closed": 1, "detail": {"ok": False}}, None)
        for result in cases:
            with self.subTest(cleanup=type(result).__name__):
                agent = self.agent(reply("useful answer"))
                patch = (mock.patch.object(agent_main.skills, "close_all_sessions", side_effect=result)
                         if isinstance(result, Exception) else
                         mock.patch.object(agent_main.skills, "close_all_sessions", return_value=result))
                with patch:
                    self.assertEqual(agent.run("question"), "useful answer")
                self.assert_outcome(agent, "partial", "cleanup_failed")

    def test_cleanup_failure_preserves_the_primary_exception_and_reason(self):
        agent = self.agent(reply())
        primary = ValueError(f"primary {SECRET}")
        with mock.patch.object(agent, "_run_inner", side_effect=primary), \
                mock.patch.object(agent_main.skills, "close_all_sessions", side_effect=RuntimeError(EXTERNAL_SECRET)):
            with self.assertRaises(ValueError) as captured:
                agent.run("question")
        self.assertIs(captured.exception, primary)
        self.assert_outcome(agent, "failed", "unhandled_exception")
        self.assertIn("ValueError", agent.last_run_outcome["error"])
        self.assertIn("cleanup:", agent.last_run_outcome["error"])

    def test_cleanup_failure_preserves_a_handled_model_failure(self):
        agent = self.agent(RuntimeError(SECRET))
        with mock.patch.object(agent_main.skills, "close_all_sessions", side_effect=RuntimeError(EXTERNAL_SECRET)):
            agent.run("question")
        self.assert_outcome(agent, "failed", "model_api_error")


class ToolEnvelopeTests(unittest.TestCase):
    def test_shared_helper_is_reexported_without_a_second_implementation(self):
        self.assertIs(agent_main.tool_result_failed, execution_outcome.tool_result_failed)

    def test_non_dict_contract_is_left_to_the_caller(self):
        for value in (None, "failed", False, [{"success": False}]):
            self.assertFalse(execution_outcome.tool_result_failed(value))

    def test_failure_markers_and_known_wrappers(self):
        failures = ({"success": False}, {"ok": False}, {"error": "failed"},
                    {"error": {"message": "failed"}}, {"error": ["failed"]},
                    {"success": True, "result": {"isError": True}},
                    {"success": True, "results": [{"ok": True}, {"ok": False}]},
                    {"summary": {"jobs": [{"success": False}]}},
                    {"closed": 1, "detail": {"ok": False}})
        for value in failures:
            with self.subTest(value=value):
                self.assertTrue(agent_main.tool_result_failed(value))

    def test_data_columns_warnings_and_empty_errors_are_not_failures(self):
        values = ({"success": True, "error": ""}, {"ok": True, "error": None},
                  {"success": True, "warning": "CPU fallback"},
                  {"error": 0.2}, {"error": {"mean": 0.2}},
                  {"success": True, "result": {"summary": {"error": {"mean": 1}, "ok": False}}},
                  {"success": True, "data": [{"error": "a row value", "success": False}]},
                  {"success": True, "errors": []})
        for value in values:
            with self.subTest(value=value):
                self.assertFalse(agent_main.tool_result_failed(value))


if __name__ == "__main__":
    unittest.main(argv=[sys.argv[0], *unittest_args])
