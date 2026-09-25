"""Probe whether the configured StepFun model actually supports function calling.

If this fails, the whole agent design has to change (e.g. prompt-based tool routing),
so it is worth checking before building anything on top of it.
"""
import json
import os
import sys

from openai import OpenAI

key = os.environ.get("STEPFUN_API_KEY")
if not key:
    print("NO_KEY")
    sys.exit(2)

client = OpenAI(api_key=key, base_url=os.environ.get(
    "STEPFUN_BASE_URL", "https://api.stepfun.com/step_plan/v1"))

TOOLS = [{
    "type": "function",
    "function": {
        "name": "analyze_dataset",
        "description": "Run statistical analysis and outlier detection on a local data file.",
        "parameters": {
            "type": "object",
            "properties": {
                "file_path": {"type": "string", "description": "path to the data file"},
                "operation": {"type": "string",
                              "enum": ["auto", "summary", "groupby", "corr", "outliers"]},
            },
            "required": ["file_path", "operation"],
        },
    },
}]

for model in [os.environ.get("STEPFUN_MODEL", "step-3.7-flash")]:
    print(f"=== model: {model} ===")
    try:
        r = client.chat.completions.create(
            model=model,
            messages=[{"role": "user",
                       "content": "Analyze /data/sales.csv and tell me whether it has outliers"}],
            tools=TOOLS,
            tool_choice="auto",
        )
        msg = r.choices[0].message
        tcs = getattr(msg, "tool_calls", None)
        if tcs:
            print("TOOL_CALLING=YES")
            for tc in tcs:
                print(f"  fn={tc.function.name}")
                print(f"  args={tc.function.arguments}")
        else:
            print("TOOL_CALLING=NO (model answered directly)")
            print(f"  content[:200]={(msg.content or '')[:200]!r}")
    except Exception as exc:
        print(f"TOOL_CALLING=ERROR {type(exc).__name__}: {str(exc)[:300]}")