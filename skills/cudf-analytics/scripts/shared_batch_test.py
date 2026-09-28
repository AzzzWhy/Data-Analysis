"""Exact multi-report reuse, including failure and input-identity boundaries."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import gpu_session as gs


class SharedBatchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "data.csv"
        self.path.write_text("region,revenue\nA,1\nA,2\nB,100\nC,3\n", encoding="utf-8")
        gs.SESSIONS.clear()
        gs.WARM_CACHE.clear()

    def tearDown(self):
        gs.SESSIONS.clear()
        gs.WARM_CACHE.clear()
        self.temp.cleanup()

    def request(self):
        return {"path": str(self.path), "force_cpu": True, "plans": [
            [{"op": "summary", "columns": "revenue"},
             {"op": "outliers", "columns": "revenue", "top_k": k},
             {"op": "groupby", "by": "region", "agg": "revenue:sum", "top_k": k}]
            for k in (1, 3)]}

    def test_same_file_once_and_distinct_top_k_exact(self):
        reply = gs.do_batch_many(self.request())
        self.assertTrue(reply["ok"], reply)
        first, second = reply["results"]
        self.assertEqual(len(first["results"][2]["groupby"]["top_k"]), 1)
        self.assertEqual(len(second["results"][2]["groupby"]["top_k"]), 3)
        self.assertEqual(first["results"][0]["summary"]["stats"],
                         second["results"][0]["summary"]["stats"])
        self.assertTrue(all(step["result_reused"] for step in second["results"]))
        self.assertTrue(all(step["rows_scanned"] == 0 for step in second["results"]))
        self.assertEqual(second["loading"]["read_count"], 0)
        self.assertEqual(gs.SESSIONS, {})
        self.assertEqual(gs.WARM_CACHE, {})

    def test_matches_individually_executed_results(self):
        request = self.request()
        shared = gs.do_batch_many(request)
        for plan, got in zip(request["plans"], shared["results"]):
            separate = gs.do_batch({"path": str(self.path), "force_cpu": True, "steps": plan})
            for step, a, b in zip(plan, got["results"], separate["results"]):
                op = step["op"]
                fields = {"summary": "stats", "outliers": "results", "groupby": "top_k"}
                self.assertEqual(a[op][fields[op]], b[op][fields[op]])

    def test_changed_input_is_not_answered_from_memo(self):
        original = gs.do_analyze
        calls = []
        def analyze(req):
            answer = original(req)
            calls.append(req)
            if len(calls) == 3:
                self.path.write_text("region,revenue\nZ,99999\n", encoding="utf-8")
            return answer
        with patch.object(gs, "do_analyze", side_effect=analyze):
            reply = gs.do_batch_many(self.request())
        self.assertFalse(reply["ok"], reply)
        self.assertIn("changed", reply["error"])
        self.assertEqual(gs.SESSIONS, {})

    def test_bounded_protocol_and_failed_step_cleanup(self):
        self.assertFalse(gs.do_batch_many({**self.request(), "plans": [[]]})["ok"])
        self.assertFalse(gs.do_batch_many({**self.request(), "plans": self.request()["plans"] * 17})["ok"])
        request = self.request()
        request["plans"][1][0]["columns"] = "missing"
        self.assertFalse(gs.do_batch_many(request)["ok"])
        self.assertEqual(gs.SESSIONS, {})


if __name__ == "__main__":
    unittest.main()
