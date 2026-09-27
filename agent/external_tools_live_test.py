"""Real model + real external skill/MCP. Synthetic data; local endpoint only."""
import argparse
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
from urllib.parse import urlsplit

from openai import OpenAI
import openai
if int(openai.__version__.split(".")[0]) >= 3:
    import httpx2 as httpx
else:
    import httpx
from agent_main import Agent


def contains_result(value, field, expected):
    """Match decoded result values, not substrings in schemas/descriptions."""
    if isinstance(value, dict):
        if value.get(field) == expected:
            return True
        if value.get("type") == "text" and isinstance(value.get("text"), str):
            try:
                if contains_result(json.loads(value["text"]), field, expected):
                    return True
            except ValueError:
                pass
        return any(contains_result(child, field, expected) for child in value.values())
    if isinstance(value, list):
        return any(contains_result(child, field, expected) for child in value)
    return False


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:18086/v1")
    parser.add_argument("--model", required=True)
    args = parser.parse_args()
    url = urlsplit(args.base_url)
    if url.hostname not in ("localhost", "127.0.0.1", "::1") or url.username or url.password:
        parser.error("test endpoint must be localhost without credentials")
    root = Path(__file__).resolve().parents[1]
    config = {"skill_roots": [], "timeout_seconds": 12,
        "skills": {"scale": {"path": str(root / "examples/external/scale-skill"),
            "enabled": True, "description": "Scale a numeric value by a factor",
            "command": [sys.executable, "scale.py"], "input_schema": {
                "type": "object", "properties": {"value": {"type": "number"}, "factor": {"type": "number"}},
                "required": ["value", "factor"], "additionalProperties": False}}},
        "mcpServers": {"math": {"enabled": True, "command": sys.executable,
            "args": [str(root / "examples/external/math_mcp.py")], "allowed_tools": ["add"]}}}
    with tempfile.TemporaryDirectory(prefix="gda-external-live-") as folder, \
            OpenAI(base_url=args.base_url, api_key="local-test-no-secret", timeout=90, max_retries=0,
                   http_client=httpx.Client(trust_env=False)) as client:
        path = Path(folder) / "external_tools.json"
        with path.open("w", encoding="utf-8") as stream:
            json.dump(config, stream)
        def create(**kwargs):
            return client.chat.completions.create(**kwargs, max_tokens=1800, temperature=0,
                extra_body={"chat_template_kwargs": {"enable_thinking": False}})
        proxy = SimpleNamespace(api_key="local-test-no-secret", chat=SimpleNamespace(
            completions=SimpleNamespace(create=create)))
        scenarios = [
            ("请自主寻找已可用的外部 skill，把数值7缩放为3倍。先阅读其说明，再执行，报告真实结果。",
             {"discover_external_tools", "read_external_skill", "run_external_skill"}, "scaled_value", 21),
            ("请自主寻找可用的外部 MCP 工具计算19加23，报告实际调用的服务和真实结果。",
             {"discover_external_tools", "call_external_mcp"}, "sum", 42),
            ("请自行寻找可用的外部工具：先把数值7缩放为3倍，再把实际得到的结果加4。"
             "需要真实执行，并告诉我工具调用顺序和最终结果。",
             {"discover_external_tools", "read_external_skill", "run_external_skill", "call_external_mcp"}, "sum", 25)]
        for question, required, field, expected in scenarios:
            agent = Agent(proxy, verbose=False, model=args.model, external_config=path)
            answer = agent.run(question)
            calls = [call["function"]["name"] for message in agent.messages
                     for call in message.get("tool_calls", [])]
            results = [json.loads(message["content"]) for message in agent.messages if message["role"] == "tool"]
            passed = required.issubset(calls) and any(
                result.get("success") and contains_result(result.get("result"), field, expected)
                for result in results)
            if expected == 25:
                run_args = [json.loads(call["function"]["arguments"]) for message in agent.messages
                            for call in message.get("tool_calls", []) if call["function"]["name"] == "call_external_mcp"]
                passed = passed and any(
                    sorted(request.get("arguments", {}).values()) == [4, 21] for request in run_args)
                passed = passed and calls.index("run_external_skill") < calls.index("call_external_mcp")
            print(json.dumps({"question": question, "calls": calls, "answer": answer,
                              "expected": expected, "passed": passed}, ensure_ascii=True), flush=True)
            if not passed:
                raise AssertionError("live external capability chain did not pass")
    print("LIVE_EXTERNAL_SKILL_AND_MCP=PASS", flush=True)


if __name__ == "__main__":
    main()
