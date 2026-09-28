"""Outlier examples survive model-context compaction; lists stay bounded."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import skills


def outliers(count=3):
    return {"results": {"revenue": {"count": 899, "valid_count": 20000,
            "examples": [{"revenue": 100 + index} for index in range(count)]}}}


class CompactionTests(unittest.TestCase):
    def test_operation_keeps_examples_and_totals(self):
        original = outliers()
        self.assertEqual(skills._compact_operation(original, "outliers"), original)

    def test_auto_keeps_examples(self):
        original = {"outliers": outliers(), "summary": {"stats": {"revenue": {"mean": 16.28}}}}
        self.assertEqual(skills._compact_operation(original, "auto"), original)

    def test_list_limit_and_deep_limit_remain(self):
        compacted = skills._compact_operation(outliers(100), "outliers")
        rows = compacted["results"]["revenue"]["examples"]
        self.assertEqual(len(rows), 31)
        self.assertEqual(rows[0], {"revenue": 100})
        self.assertIn("70 more entries omitted", rows[-1])
        self.assertEqual(compacted["results"]["revenue"]["count"], 899)
        original = {"nested": {"nested": {"nested": {"nested": {"deep": [1, 2]}}}}}
        self.assertEqual(skills._compact_operation(original, "summary"), skills._compact(original))

    def test_actual_batch_wrapper(self):
        reply = {"ok": True, "results": [{"op": "outliers", "outliers": outliers()}]}
        with patch.object(skills, "_worker_call", return_value=copy.deepcopy(reply)), \
                patch.object(skills, "_resolve_data_path", return_value="fixture.parquet"), \
                patch.object(skills, "_remember_last_file"):
            result = json.loads(skills.analyze_batch("fixture.parquet", [{"op": "outliers"}]))
        self.assertTrue(result["success"])
        self.assertEqual(result["results"][0]["outliers"], reply["results"][0]["outliers"])

    def test_actual_session_wrapper(self):
        reply = {"ok": True, "op": "outliers", "engine": "pandas", "outliers": outliers()}
        with patch.object(skills, "_worker_call", return_value=copy.deepcopy(reply)):
            result = json.loads(skills.dataset_session("analyze", session_id="fixture", op="outliers"))
        self.assertTrue(result["success"])
        self.assertEqual(result["outliers"], reply["outliers"])

    def test_actual_stateless_wrapper(self):
        reply = {"ok": True, "op": "outliers", "engine": "pandas", "rows_scanned": 20000,
                 "outliers": outliers()}
        import subprocess
        proc = subprocess.CompletedProcess([], 0, json.dumps(reply), "")
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "fixture.csv"
            source.write_text("revenue\n100\n", encoding="utf-8")
            with patch.object(skills, "_run_analysis", return_value=proc), \
                    patch.object(skills, "_find_engine", return_value="fixture.py"), \
                    patch.object(skills, "_remember_last_file"):
                result = json.loads(skills.analyze_dataset(str(source), "outliers", force_cpu=True))
        self.assertTrue(result["success"], result)
        self.assertEqual(result["result"]["outliers"], reply["outliers"])


if __name__ == "__main__":
    unittest.main()
