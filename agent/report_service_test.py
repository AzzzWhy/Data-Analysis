"""Real CPU subprocess tests for the local persistent report protocol."""
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import report_service


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.data = self.root / "input.csv"
        self.data.write_text("region,revenue\nA,1\nB,3\n", encoding="utf-8")
        self.plan = self.root / "plan.json"
        self.plan.write_text(json.dumps({"file_path": str(self.data), "force_cpu": True,
            "steps": [{"op": "summary", "columns": "revenue"}]}), encoding="utf-8")
        self.manifest = self.root / "manifest.json"
        self.manifest.write_text(json.dumps({"jobs": [{"name": "report", "plan": str(self.plan)}]}), encoding="utf-8")

    def tearDown(self):
        self.temp.cleanup()

    def run_service(self, requests):
        output = io.StringIO()
        source = io.StringIO("\n".join(json.dumps(r) for r in requests) + "\n")
        report_service.serve(source, output, self.root / "out")
        return [json.loads(line) for line in output.getvalue().splitlines()]

    def test_reuses_process_but_observes_changed_file(self):
        request = {"cmd": "run", "manifest": str(self.manifest), "id": "first"}
        original = report_service.respond
        def change_after_first(stream, response):
            original(stream, response)
            if response.get("id") == "first":
                self.data.write_text("region,revenue\nA,20\nB,40\nC,60\n", encoding="utf-8")
        with patch.object(report_service, "respond", side_effect=change_after_first):
            replies = self.run_service([request, {**request, "id": "second"},
                                        {"cmd": "status"}, {"cmd": "close"}])
        first, second = replies[1:3]
        self.assertTrue(first["ok"] and second["ok"])
        self.assertEqual(first["summary"]["jobs"][0]["pid"], second["summary"]["jobs"][0]["pid"])
        reports = [json.loads((Path(r["summary"]["jobs"][0]["report_dir"]) / "result.json").read_text())
                   for r in (first, second)]
        self.assertEqual(reports[0]["result"]["rows"], 2)
        self.assertEqual(reports[1]["result"]["rows"], 3)
        self.assertEqual(reports[1]["result"]["results"][0]["summary"]["stats"]["revenue"]["mean"], 40)
        self.assertEqual(replies[3]["completed_requests"], 2)
        self.assertEqual(replies[-1]["event"], "closed")

    def test_bad_requests_do_not_kill_service(self):
        replies = self.run_service([[], {"cmd": "run", "manifest": "missing.json"},
            {"cmd": "status", "inject": 1}, {"cmd": "status", "id": 7}, {"cmd": "status"},
            {"cmd": "close"}])
        self.assertTrue(all(not reply["ok"] for reply in replies[1:5]))
        self.assertTrue(replies[5]["ok"])
        self.assertEqual(replies[5]["worker_pids"], [])

    def test_oversized_and_invalid_json(self):
        for content in ("x" * (report_service.MAX_REQUEST_BYTES + 1), "not json\n"):
            output = io.StringIO()
            report_service.serve(io.StringIO(content), output, self.root / "out")
            self.assertFalse(json.loads(output.getvalue().splitlines()[1])["ok"])

    def test_eof_closes_owned_pool(self):
        from queue_runtime import QueueExecutor
        pool = QueueExecutor(2)
        with patch.object(report_service, "QueueExecutor", return_value=pool):
            self.run_service([{"cmd": "run", "manifest": str(self.manifest)}])
        self.assertTrue(pool.closed)
        self.assertTrue(all(r.proc is None for r in pool.cpu_runners))

    def test_invalid_limits(self):
        for workers, timeout in ((0, 10), (5, 10), (2, 0), (2, 86401)):
            with self.assertRaises(ValueError):
                report_service.serve(io.StringIO(), io.StringIO(), self.root / "out",
                                     max_cpu_workers=workers, timeout_seconds=timeout)


if __name__ == "__main__":
    unittest.main()
