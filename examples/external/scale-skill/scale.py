"""Example external command: JSON in, JSON out; no side effects."""
import json
import sys

request = json.load(sys.stdin)
print(json.dumps({"scaled_value": request["value"] * request["factor"]}))
