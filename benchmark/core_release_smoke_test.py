"""Pure contract checks for the release harness; no model or GPU required."""
import copy
import unittest

from core_release_smoke import inspect_batch, inspect_examples


def payload(route="cpu", selected="cpu", engine="pandas"):
    return {"success": True, "comparison_measured": False,
            "loading": {"actual": route, "execution_context": "cold",
                        "decision": {"selected": selected, "policy": "matched_hybrid_calibration"}},
            "results": [{"op": op, "rows_scanned": 20000, "engine": engine}
                        for op in ("summary", "outliers", "groupby")]}


class AcceptanceContractTests(unittest.TestCase):
    def test_outlier_examples_are_records(self):
        reply = payload()
        reply["results"][1]["outliers"] = {"results": {"revenue": {
            "count": 899, "examples": [{"revenue": 100.0}]}}}
        self.assertTrue(inspect_examples(reply)["outlier_examples_preserved"])

    def test_reject_truncated_outlier_examples(self):
        for examples in ([], ["<dict truncated: 1 entries>"]):
            reply = payload()
            reply["results"][1]["outliers"] = {"results": {"revenue": {
                "count": 899, "examples": examples}}}
            with self.assertRaises(AssertionError):
                inspect_examples(reply)

    def test_cpu_and_gpu_paths(self):
        for route, selected, engine in (("cpu", "cpu", "pandas"),
                                       ("native_gpu", "native", "cudf"),
                                       ("cpu_gpu", "cpu_gpu", "cudf")):
            self.assertEqual(inspect_batch(payload(route, selected, engine), 20000)["actual"], route)

    def test_reject_false_acceleration_claims(self):
        valid = payload()
        cases = []
        for key, value in (("success", False), ("comparison_measured", True)):
            changed = copy.deepcopy(valid)
            changed[key] = value
            cases.append(changed)
        for key, value in (("rows_scanned", 100), ("fallback_reason", "GPU unavailable"),
                           ("engine", "cudf"), ("op", "profile")):
            changed = copy.deepcopy(valid)
            changed["results"][0][key] = value
            cases.append(changed)
        for key, value in (("policy", "measured_file_size_crossover"), ("selected", "native")):
            changed = copy.deepcopy(valid)
            changed["loading"]["decision"][key] = value
            cases.append(changed)
        changed = copy.deepcopy(valid)
        changed["results"].pop()
        cases.append(changed)
        for changed in cases:
            with self.subTest(payload=changed), self.assertRaises(AssertionError):
                inspect_batch(changed, 20000)


if __name__ == "__main__":
    unittest.main()
