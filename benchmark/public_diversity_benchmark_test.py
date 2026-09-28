"""Pure benchmark contract checks; do not require a GPU or Arrow runtime."""
import copy
import unittest
from public_diversity_benchmark import cases, inspect


def step():
    return {"op": "summary", "engine": "pandas", "rows_scanned": 100,
            "summary": {"stats": {"x": {"mean": 2.0}}}}


class DiversityContractTests(unittest.TestCase):
    def test_full_scan(self):
        outputs, flat = inspect({"results": [step()]}, 100, "pandas")
        self.assertEqual(len(outputs), 1)
        self.assertEqual(flat[0]["rows_scanned"], 100)

    def test_valid_transaction_reuse(self):
        original = step()
        reused = {**copy.deepcopy(original), "rows_scanned": 0,
                  "source_rows": 100, "result_reused": True}
        outputs, _ = inspect({"results": [{"results": [original]}, {"results": [reused]}]}, 100, "pandas")
        self.assertEqual(outputs[0], outputs[1])

    def test_reject_partial_scan_fallback_and_wrong_engine(self):
        for change in ({"rows_scanned": 99}, {"fallback_reason": "GPU unavailable"}, {"engine": "cudf"}):
            with self.subTest(change=change), self.assertRaises(AssertionError):
                inspect({"results": [{**step(), **change}]}, 100, "pandas")

    def test_reject_unproven_or_wrong_reuse(self):
        reused = {**step(), "rows_scanned": 0, "source_rows": 100, "result_reused": True}
        with self.assertRaises(AssertionError):
            inspect({"results": [reused]}, 100)
        for change in ({"source_rows": 99}, {"rows_scanned": 10},
                       {"summary": {"stats": {"x": {"mean": 3.0}}}}):
            with self.subTest(change=change), self.assertRaises(AssertionError):
                inspect({"results": [step(), {**reused, **change}]}, 100)

    def test_workloads_use_all_wide_features_and_valid_aggregation(self):
        plans = {name: steps for name, _, steps in cases()}
        self.assertEqual(len(plans["gas_wide"][0]["columns"].split(",")), 128)
        self.assertEqual(len(plans["susy_wide"][0]["columns"].split(",")), 18)
        self.assertEqual(plans["retail_country"][-1]["agg"], "Quantity:sum|Price:mean")


if __name__ == "__main__":
    unittest.main()
