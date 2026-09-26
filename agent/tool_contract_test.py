#!/usr/bin/env python3
"""Fast checks that the model-visible tool schemas match their implementations."""

import inspect
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import skills  # noqa: E402


FAILURES = []


def check(label, condition, detail=""):
    print(f"  [{'PASS' if condition else 'FAIL'}] {label}"
          f"{'  <- ' + detail if detail and not condition else ''}")
    if not condition:
        FAILURES.append(label)


definitions = {item["function"]["name"]: item["function"]
               for item in skills.load_skill_definitions()}

for name, definition in definitions.items():
    implementation = skills.skill_func_map[name]
    parameters = set(inspect.signature(implementation).parameters)
    properties = set(definition.get("parameters", {}).get("properties", {}))
    required = set(definition.get("parameters", {}).get("required", []))

    unknown = sorted(properties - parameters)
    check(f"{name}: every schema property is accepted by the function",
          not unknown, f"unknown properties: {unknown}")
    check(f"{name}: every required property exists in the schema",
          required <= properties, f"missing properties: {sorted(required - properties)}")

session_properties = set(definitions["dataset_session"]["parameters"]["properties"])
analysis_properties = set(definitions["analyze_dataset"]["parameters"]["properties"])
check("dataset_session exposes the force_gpu escape hatch", "force_gpu" in session_properties)
check("dataset_session exposes force_cpu for validation", "force_cpu" in session_properties)
check("analyze_dataset does not advertise the session-only force_gpu argument",
      "force_gpu" not in analysis_properties)

if FAILURES:
    print(f"FAIL: {len(FAILURES)} tool-contract checks failed")
    raise SystemExit(1)

print("ALL TOOL CONTRACT CHECKS PASSED")
