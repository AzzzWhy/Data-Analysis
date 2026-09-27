"""Offline security checks plus REAL stdio MCP and executable skill integration."""
import json
import os
from pathlib import Path
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from external_tools import ExternalTools, DEFINITIONS
import agent_main as am

ROOT = Path(__file__).resolve().parents[1]


class ExternalToolsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="gda-external-")
        self.path = Path(self.temp.name) / "config.json"
        self.config = {
            "skill_roots": [str(ROOT / "examples" / "external")],
            "timeout_seconds": 12,
            "skills": {"scale": {
                "path": str(ROOT / "examples" / "external" / "scale-skill"),
                "description": "Scale a value by a factor", "enabled": True,
                "command": [sys.executable, "scale.py"],
                "input_schema": {"type": "object", "properties": {
                    "value": {"type": "number"}, "factor": {"type": "number"}},
                    "required": ["value", "factor"], "additionalProperties": False}}},
            "mcpServers": {"math": {"enabled": True, "command": sys.executable,
                "args": [str(ROOT / "examples" / "external" / "math_mcp.py")],
                "allowed_tools": ["add"]}}}
        self.save()
        self.registry = ExternalTools(self.path)
        self.registry.read_skill("scale")

    def save(self):
        with self.path.open("w", encoding="utf-8") as stream:
            json.dump(self.config, stream)

    def tearDown(self):
        self.temp.cleanup()

    def call(self, name, **arguments):
        return self.registry.execute(name, arguments)

    def test_installed_skill_read_and_real_execution(self):
        catalog = self.call("discover_external_tools", query="scale", kind="skill")
        self.assertTrue(catalog["success"], catalog)
        self.assertEqual(catalog["matches"][0]["skill_id"], "scale")
        self.assertTrue(self.call("read_external_skill", skill_id="scale")["untrusted_external_content"])
        result = self.call("run_external_skill", skill_id="scale", arguments={"value": 7, "factor": 3})
        self.assertTrue(result["success"], result)
        self.assertEqual(result["result"]["scaled_value"], 21)

    def test_real_mcp_discovery_and_call(self):
        result = self.call("discover_external_tools", query="add", kind="mcp")
        self.assertTrue(result["success"], result)
        self.assertEqual(result["errors"], [], result)
        tools = {item["tool_name"]: item for item in result["matches"]}
        self.assertTrue(tools["add"]["authorized"])
        self.assertFalse(tools["restricted_probe"]["authorized"])
        value = self.call("call_external_mcp", server_id="math", tool_name="add", arguments={"a": 19, "b": 23})
        self.assertTrue(value["success"], value)
        self.assertIn("42", json.dumps(value["result"]))

    def test_denied_mcp_never_starts_a_process(self):
        with patch.object(self.registry, "_run") as run:
            result = self.call("call_external_mcp", server_id="math", tool_name="restricted_probe", arguments={})
        self.assertFalse(result["success"])
        run.assert_not_called()

    def test_real_streamable_http_transport(self):
        import socket
        import subprocess
        import time
        from external_tools import clean_env
        with socket.socket() as reservation:
            reservation.bind(("127.0.0.1", 0))
            port = reservation.getsockname()[1]
        env = clean_env()
        env["MCP_DEMO_PORT"] = str(port)
        process = subprocess.Popen([sys.executable, str(ROOT / "examples" / "external" / "math_mcp.py"),
                                    "--http"], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                   creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        try:
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                self.assertIsNone(process.poll(), "HTTP fixture exited early")
                try:
                    with socket.create_connection(("127.0.0.1", port), timeout=.2):
                        break
                except OSError:
                    time.sleep(.05)
            else:
                self.fail("HTTP fixture startup timed out")
            self.config["mcpServers"]["math"] = {
                "enabled": True, "transport": "streamable-http",
                "url": f"http://127.0.0.1:{port}/mcp", "allowed_tools": ["add"]}
            self.save()
            result = self.call("call_external_mcp", server_id="math", tool_name="add", arguments={"a": 2, "b": 5})
            self.assertTrue(result["success"], result)
            self.assertIn("7", json.dumps(result["result"]))
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)

    def test_remote_plain_http_is_rejected(self):
        self.config["mcpServers"]["math"] = {
            "enabled": True, "transport": "streamable-http", "url": "http://example.invalid/mcp",
            "allowed_tools": ["add"]}
        self.save()
        self.assertFalse(self.call("call_external_mcp", server_id="math", tool_name="add", arguments={})["success"])

    def test_disabled_skill_and_mcp_never_execute(self):
        self.config["skills"]["scale"]["enabled"] = False
        self.config["mcpServers"]["math"]["enabled"] = False
        self.save()
        with patch.object(self.registry, "_run") as run:
            self.assertFalse(self.call("run_external_skill", skill_id="scale", arguments={})["success"])
            self.assertFalse(self.call("call_external_mcp", server_id="math", tool_name="add", arguments={})["success"])
        run.assert_not_called()

    def test_bad_skill_arguments_do_not_execute(self):
        with patch.object(self.registry, "_run") as run:
            result = self.call("run_external_skill", skill_id="scale", arguments={"value": "wrong", "factor": 2})
        self.assertFalse(result["success"])
        run.assert_not_called()

    def test_bad_mcp_arguments_rejected_by_real_bridge(self):
        result = self.call("call_external_mcp", server_id="math", tool_name="add", arguments={"a": "wrong"})
        self.assertFalse(result["success"], result)

    def test_remote_schema_references_are_rejected(self):
        self.config["skills"]["scale"]["input_schema"] = {"$ref": "https://example.invalid/schema"}
        self.save()
        with patch.object(self.registry, "_run") as run:
            self.assertFalse(self.call("run_external_skill", skill_id="scale", arguments={})["success"])
        run.assert_not_called()

    def test_config_permissions_refresh_without_restart(self):
        self.config["skills"]["scale"]["enabled"] = False
        self.save()
        self.assertFalse(self.call("run_external_skill", skill_id="scale", arguments={"value": 2, "factor": 2})["success"])
        self.config["skills"]["scale"]["enabled"] = True
        self.save()
        self.assertTrue(self.call("run_external_skill", skill_id="scale", arguments={"value": 2, "factor": 2})["success"])

    def test_commands_and_credentials_cannot_be_set_by_model(self):
        with patch.dict(os.environ, {"GPU_API_KEY": "private-key-do-not-share"}):
            from external_tools import clean_env
            self.assertNotIn("GPU_API_KEY", clean_env())
            result = self.call("call_external_mcp", server_id="math", tool_name="add", arguments={},
                               command="evil", url="https://evil.invalid")
        self.assertFalse(result["success"])

    def test_configured_secret_is_redacted(self):
        self.config["skills"]["scale"]["env_from"] = ["PROBE_SECRET"]
        self.save()
        with patch.dict(os.environ, {"PROBE_SECRET": "private-probe-secret"}):
            env = self.registry._environment(self.config["skills"]["scale"])
            self.assertEqual(env["PROBE_SECRET"], "private-probe-secret")
            self.assertEqual(self.registry.redact("echo private-probe-secret"), "echo [REDACTED]")

    def test_timeout_and_output_limit(self):
        import subprocess
        with patch("external_tools.subprocess.run", side_effect=subprocess.TimeoutExpired("test", 1)):
            self.assertFalse(self.registry._run(["test"], {}, 1, {})["success"])
        with patch("external_tools.subprocess.run", return_value=SimpleNamespace(returncode=0, stdout="x" * 70000)):
            self.assertFalse(self.registry._run(["test"], {}, 1, {})["success"])

    def test_empty_configuration_is_nonfatal(self):
        self.config = {"skill_roots": []}
        self.save()
        result = self.call("discover_external_tools")
        self.assertEqual(result["total"], 0)
        self.assertTrue(result["success"])

    def fault(self, mode):
        self.config["skills"]["scale"]["command"] = [sys.executable, str(Path(__file__).with_name("external_fault_fixture.py")), mode]
        self.save()

    def test_real_unicode_and_json_escaped_secret_redaction(self):
        self.fault("echo-secret")
        self.config["skills"]["scale"]["env_from"] = ["PROBE_SECRET"]
        self.save()
        secret = 'synthetic-"quote"\\slash\n中文'
        with patch.dict(os.environ, {"PROBE_SECRET": secret}):
            result = self.call("run_external_skill", skill_id="scale", arguments={"value": 1, "factor": 2})
        self.assertTrue(result["success"], result)
        self.assertEqual(result["result"]["value"], "[REDACTED]")
        self.assertEqual(result["result"]["nested"], ["prefix [REDACTED]"])
        self.assertEqual(result["result"]["secret_key"], {"[REDACTED]": "value"})
        self.assertIs(result["result"]["true_value"], True)
        self.fault("unicode")
        self.config["skills"]["scale"].pop("env_from")
        self.save()
        self.assertEqual(self.call("run_external_skill", skill_id="scale", arguments={"value": 1, "factor": 2})["result"]["text"], "中文测试：正常")

    def test_redaction_does_not_corrupt_boolean_json(self):
        self.registry.secrets.add("true")
        with patch("external_tools.subprocess.run", return_value=SimpleNamespace(
                returncode=0, stdout='{"value": "true", "flag": true}')):
            result = self.registry._run(["test"], {}, 1, {})
        self.assertEqual(result, {"value": "[REDACTED]", "flag": True})

    def test_real_timeout_and_subsequent_recovery(self):
        original = list(self.config["skills"]["scale"]["command"])
        self.config["timeout_seconds"] = 1
        self.fault("sleep")
        started = time.monotonic()
        result = self.call("run_external_skill", skill_id="scale", arguments={"value": 1, "factor": 2})
        self.assertFalse(result["success"])
        self.assertIn("timed out", result["result"]["error"])
        self.assertLess(time.monotonic() - started, 5)
        self.config["skills"]["scale"]["command"] = original
        self.save()
        recovered = self.call("run_external_skill", skill_id="scale", arguments={"value": 1, "factor": 2})
        self.assertEqual(recovered["result"]["scaled_value"], 2)

    def test_real_invalid_json_nonfinite_and_exit_code(self):
        for mode in ("invalid-json", "nonfinite", "exit-error", "oversize"):
            with self.subTest(mode=mode):
                self.fault(mode)
                result = self.call("run_external_skill", skill_id="scale", arguments={"value": 1, "factor": 2})
                self.assertFalse(result["success"], result)
                self.assertNotIn("synthetic private error", json.dumps(result))

    def test_nonfinite_arguments_never_start_process(self):
        with patch.object(self.registry, "_run", wraps=self.registry._run), \
                patch("external_tools.subprocess.run") as process:
            result = self.call("run_external_skill", skill_id="scale", arguments={"value": float("nan"), "factor": 2})
        self.assertFalse(result["success"])
        process.assert_not_called()

    def test_mcp_permissions_revoked_after_discovery(self):
        found = self.call("discover_external_tools", kind="mcp")
        self.assertTrue(any(item["tool_name"] == "add" and item["authorized"] for item in found["matches"]), found)
        self.config["mcpServers"]["math"]["allowed_tools"] = []
        self.save()
        with patch.object(self.registry, "_run") as run:
            result = self.call("call_external_mcp", server_id="math", tool_name="add", arguments={"a": 1, "b": 2})
        self.assertFalse(result["success"])
        run.assert_not_called()

    def test_real_broken_stdio_server_and_other_discovery_survives(self):
        good = self.config["mcpServers"]["math"]
        self.config["mcpServers"] = {"broken": {"enabled": True, "command": sys.executable,
            "args": [str(Path(__file__).with_name("external_fault_fixture.py")), "exit-error"],
            "allowed_tools": []}, "math": good}
        self.save()
        found = self.call("discover_external_tools", kind="all")
        self.assertTrue(found["success"], found)
        self.assertTrue(any(item.get("tool_name") == "add" for item in found["matches"]), found)
        self.assertTrue(any(item.get("skill_id") == "scale" for item in found["matches"]), found)
        self.assertEqual(found["errors"][0]["server_id"], "broken")

    def test_invalid_configuration_is_nonfatal(self):
        self.config = []
        self.save()
        self.assertFalse(self.call("discover_external_tools")["success"])

    def test_standard_skill_is_discovered_without_command_authorization(self):
        self.config["skills"] = {}
        self.save()
        catalog = self.call("discover_external_tools", kind="skill")
        self.assertEqual(catalog["total"], 1)
        item = catalog["matches"][0]
        self.assertFalse(item["executable"])
        self.assertTrue(self.call("read_external_skill", skill_id=item["skill_id"])["success"])
        with patch.object(self.registry, "_run") as run:
            self.assertFalse(self.call("run_external_skill", skill_id=item["skill_id"], arguments={})["success"])
        run.assert_not_called()

    def test_path_traversal_and_unknown_server_are_not_executable(self):
        with patch.object(self.registry, "_run") as run:
            self.assertFalse(self.call("read_external_skill", skill_id="../../secret")["success"])
            self.assertFalse(self.call("call_external_mcp", server_id="unknown", tool_name="add", arguments={})["success"])
        run.assert_not_called()

    def test_mcp_discovery_is_bounded_and_can_be_narrowed(self):
        with patch("external_tools.time.monotonic", side_effect=[0., 40.]), \
                patch.object(self.registry, "_mcp") as mcp:
            result = self.call("discover_external_tools", kind="mcp")
        mcp.assert_not_called()
        self.assertEqual(result["errors"][0]["server_id"], "math")
        with patch.object(self.registry, "_mcp", return_value={"success": True, "tools": []}) as mcp:
            self.call("discover_external_tools", kind="mcp", server_id="math")
        self.assertEqual(mcp.call_args.args[1], "math")

    def test_external_configuration_is_not_shared_between_agents(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("NO_TOOLS", None)
            first = am.Agent(None, verbose=False, external_config=self.path)
            second = am.Agent(None, verbose=False, external_config=Path(self.temp.name) / "other.json")
        self.assertIsNot(first.external_tools, second.external_tools)
        self.assertNotEqual(first.external_tools.path, second.external_tools.path)

    def test_gateway_roundtrip_in_actual_agent_loop(self):
        steps = iter([
            ("discover_external_tools", {"query": "scale", "kind": "skill"}),
            ("read_external_skill", {"skill_id": "scale"}),
            ("run_external_skill", {"skill_id": "scale", "arguments": {"value": 8, "factor": 4}}),
            (None, {})])
        def completion(**kwargs):
            name, arguments = next(steps)
            names = [tool["function"]["name"] for tool in kwargs["tools"]]
            self.assertIn("discover_external_tools", names)
            calls = [SimpleNamespace(id="test-id", function=SimpleNamespace(name=name,
                      arguments=json.dumps(arguments)))] if name else None
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
                content="32" if name is None else None, tool_calls=calls))])
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=completion)))
        with patch.dict(os.environ, {}, clear=False), patch.object(am.skills, "close_all_sessions", return_value={}):
            os.environ.pop("NO_TOOLS", None)
            agent = am.Agent(client, model="offline", verbose=False, external_config=self.path)
            self.assertEqual(agent.run("scale a value"), "32")
        results = [json.loads(m["content"]) for m in agent.messages if m["role"] == "tool"]
        self.assertEqual(results[-1]["result"]["scaled_value"], 32)

    def test_no_tools_control_stays_no_tools(self):
        with patch.dict(os.environ, {"NO_TOOLS": "1"}):
            agent = am.Agent(None, verbose=False, external_config=self.path)
        self.assertEqual(agent.tools, [])
        self.assertIsNone(agent.external_tools)

    def test_live_result_assertions_match_values_not_schema_text(self):
        from external_tools_live_test import contains_result
        self.assertFalse(contains_result({"inputSchema": {"properties": {
            "sum": {"description": "example result 42"}}}}, "sum", 42))
        self.assertTrue(contains_result({"structuredContent": {"sum": 42.0}}, "sum", 42))
        self.assertTrue(contains_result({"content": [{"type": "text", "text": '{"sum":42}'}]}, "sum", 42))

    def test_unread_skill_is_rejected_before_process_start(self):
        self.registry.read_skills.clear()
        with patch.object(self.registry, "_run") as run:
            result = self.call("run_external_skill", skill_id="scale", arguments={"value": 7, "factor": 3})
        run.assert_not_called()
        self.assertFalse(result["success"])
        self.assertEqual(result["error_code"], "instruction_read_required")
        self.assertEqual(result["required_action"]["tool"], "read_external_skill")

    def test_changed_instructions_require_rereading(self):
        import shutil
        copied = Path(self.temp.name) / "changed-skill"
        shutil.copytree(ROOT / "examples/external/scale-skill", copied)
        self.config["skills"]["scale"]["path"] = str(copied)
        self.save()
        self.registry.read_skill("scale")
        with (copied / "SKILL.md").open("a", encoding="utf-8") as stream:
            stream.write("\nUpdated synthetic test instructions.\n")
        with patch.object(self.registry, "_run") as run:
            result = self.call("run_external_skill", skill_id="scale", arguments={"value": 7, "factor": 3})
        run.assert_not_called()
        self.assertEqual(result["error_code"], "instruction_read_required")
        self.registry.read_skill("scale")
        recovered = self.call("run_external_skill", skill_id="scale", arguments={"value": 7, "factor": 3})
        self.assertEqual(recovered["result"]["scaled_value"], 21)

    def test_undelivered_large_instructions_do_not_enable_execution(self):
        import shutil
        copied = Path(self.temp.name) / "large-skill"
        shutil.copytree(ROOT / "examples/external/scale-skill", copied)
        with (copied / "SKILL.md").open("w", encoding="utf-8") as stream:
            stream.write("中" * 21844)  # <64 KB on disk, >response cap after JSON escaping
        self.config["skills"]["scale"]["path"] = str(copied)
        self.save()
        result = self.call("read_external_skill", skill_id="scale")
        self.assertFalse(result["success"], result)
        with patch.object(self.registry, "_run") as run:
            result = self.call("run_external_skill", skill_id="scale", arguments={"value": 7, "factor": 3})
        run.assert_not_called()
        self.assertEqual(result["error_code"], "instruction_read_required")

    def test_agent_log_redacts_escaped_client_key(self):
        key = 'synthetic-"quoted"\\key\n'
        steps = iter([("run_external_skill", {"skill_id": "scale", "arguments": {
            "value": key, "factor": 2}}), (None, {})])
        def completion(**_kwargs):
            name, args = next(steps)
            calls = [SimpleNamespace(id="log-test", function=SimpleNamespace(name=name,
                arguments=json.dumps(args)))] if name else None
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
                content="done" if not name else None, tool_calls=calls))])
        client = SimpleNamespace(api_key=key, chat=SimpleNamespace(completions=SimpleNamespace(create=completion)))
        logs = []
        with patch.dict(os.environ, {}, clear=False), patch.object(am.skills, "close_all_sessions", return_value={}):
            os.environ.pop("NO_TOOLS", None)
            agent = am.Agent(client, model="offline", verbose=False, external_config=self.path, event_sink=logs.append)
            self.assertEqual(agent.run("synthetic log test"), "done")
        self.assertIn("[REDACTED]", "\n".join(logs))
        self.assertNotIn(key, "\n".join(logs))


if __name__ == "__main__":
    unittest.main()
