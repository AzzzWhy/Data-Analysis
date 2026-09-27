"""Local model chooses the real batch tool for independent, known-column analyses."""
import argparse
import json
import re
from pathlib import Path
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
import skills


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8000/v1")
    parser.add_argument("--model", required=True)
    args = parser.parse_args()
    url = urlsplit(args.base_url)
    if url.hostname not in ("localhost", "127.0.0.1", "::1") or url.username or url.password:
        parser.error("test endpoint must be localhost without credentials")
    with tempfile.TemporaryDirectory(prefix="gda-batch-live-") as folder, \
            OpenAI(base_url=args.base_url, api_key="local-test-no-secret", timeout=90,
                   max_retries=0, http_client=httpx.Client(trust_env=False)) as client:
        path = Path(folder) / "fixture.csv"
        path.write_text("region,revenue\nA,1\nA,3\nB,10\n", encoding="utf-8")
        config = Path(folder) / "external.json"
        config.write_text('{"skill_roots": []}', encoding="utf-8")
        def create(**kwargs):
            return client.chat.completions.create(**kwargs, max_tokens=1400, temperature=0,
                extra_body={"chat_template_kwargs": {"enable_thinking": False}})
        proxy = SimpleNamespace(api_key="local-test-no-secret", chat=SimpleNamespace(
            completions=SimpleNamespace(create=create)))
        agent = Agent(proxy, verbose=False, model=args.model, external_config=config)
        answer = agent.run(f"请分析文件 {path}，已确认列名为 region 和 revenue。"
            "给出 revenue 的摘要、异常值，以及按 region 的收入总和前三名。"
            "三项都是独立分析，不需要下钻。不要生成文件。")
        calls = [c["function"]["name"] for m in agent.messages for c in m.get("tool_calls", [])]
        # Some SDKs omit name in tool messages; associate by tool_call_id instead.
        ids = {c["id"] for m in agent.messages for c in m.get("tool_calls", [])
               if c["function"]["name"] == "analyze_batch"}
        results = [json.loads(m["content"]) for m in agent.messages
                   if m["role"] == "tool" and m.get("tool_call_id") in ids]
        passed = False
        for reply in results:
            ops = {r.get("op") for r in reply.get("results", [])}
            groups = [r["groupby"]["top_k"] for r in reply.get("results", []) if r.get("op") == "groupby"]
            passed = (reply.get("success") and {"summary", "outliers", "groupby"} <= ops
                      and bool(groups) and groups[0][0].get("revenue__sum") == 10)
            if passed:
                break
        passed = passed and not re.search(r"GPU was\s+[\d.]+\s*x\s*faster", answer, re.I)
        print(json.dumps({"calls": calls, "passed": bool(passed), "answer": answer}, ensure_ascii=True), flush=True)
        assert passed, "local model did not complete the real batch chain"
        state = skills._worker_call({"cmd": "list"})
        assert state["count"] == 0, state
    print("LIVE_BATCH=PASS", flush=True)


if __name__ == "__main__":
    try:
        main()
    finally:
        skills._worker_stop(skills._worker)
