"""Saved batch plans run without an API key and produce reviewable reports."""
import json
from pathlib import Path
import tempfile
import unittest

import batch_job


class BatchJobTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / "sales.csv").write_text(
            "region,revenue\nEast,10\nWest,20\nEast,30\n", encoding="utf-8")
        self.plan = self.root / "plan.json"
        self.plan.write_text(json.dumps({
            "title": "Daily sales", "file_path": "sales.csv", "language": "en",
            "force_cpu": True,
            "steps": [{"op": "summary", "columns": "revenue"},
                      {"op": "groupby", "by": "region", "agg": "revenue:sum"}],
        }), encoding="utf-8")

    def tearDown(self):
        self.temp.cleanup()

    def test_real_batch_creates_separate_report_for_each_run(self):
        first = batch_job.run(self.plan, self.root / "reports")
        second = batch_job.run(self.plan, self.root / "reports")
        self.assertNotEqual(first, second)
        for folder in (first, second):
            data = json.loads((folder / "result.json").read_text(encoding="utf-8"))
            self.assertTrue(data["result"]["success"])
            self.assertEqual(data["result"]["rows"], 3)
            self.assertEqual([step["engine"] for step in data["result"]["results"]],
                             ["pandas", "pandas"])
            self.assertEqual(data["result"]["results"][0]["summary"]["stats"]
                             ["revenue"]["mean"], 20)
            report = (folder / "report.md").read_text(encoding="utf-8")
            self.assertIn("Daily sales", report)
            self.assertIn("West", report)
            self.assertIn("no CPU/GPU baseline", report)

    def test_invalid_plan_never_writes_report(self):
        self.plan.write_text(json.dumps({"file_path": "sales.csv", "steps": [
            {"op": "summary", "columns": "revenue", "unexpected": True}]}),
            encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "unsupported"):
            batch_job.run(self.plan, self.root / "reports")
        self.assertFalse((self.root / "reports").exists())

    def test_failed_operation_returns_error_without_success_report(self):
        self.plan.write_text(json.dumps({"file_path": "sales.csv", "force_cpu": True,
            "steps": [{"op": "summary", "columns": "missing"}]}), encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "not found"):
            batch_job.run(self.plan, self.root / "reports")
        self.assertFalse((self.root / "reports").exists())


if __name__ == "__main__":
    unittest.main()
