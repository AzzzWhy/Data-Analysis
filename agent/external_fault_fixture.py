"""Synthetic subprocess fixture for external-tool failures; not a production skill."""
import argparse
import json
import os
import sys
import time

parser = argparse.ArgumentParser()
parser.add_argument("mode", choices=["echo-secret", "sleep", "invalid-json", "exit-error",
                                    "oversize", "unicode", "nonfinite"])
mode = parser.parse_args().mode
if mode == "sleep":
    time.sleep(5)
    print("{}")
elif mode == "invalid-json":
    print("this is not JSON")
elif mode == "exit-error":
    print("synthetic private error", file=sys.stderr)
    sys.exit(7)
elif mode == "oversize":
    print(json.dumps({"value": "x" * 70000}))
elif mode == "unicode":
    print(json.dumps({"text": "中文测试：正常"}, ensure_ascii=False))
elif mode == "nonfinite":
    print('{"value": NaN}')
else:
    secret = os.environ.get("PROBE_SECRET", "not-set")
    print(json.dumps({"value": secret, "nested": ["prefix " + secret], "true_value": True,
                      "secret_key": {secret: "value"}}, ensure_ascii=True))
