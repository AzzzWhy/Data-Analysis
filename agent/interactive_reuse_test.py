"""Offline contract tests for interactive, bounded resident one-shot analysis."""

import json
import os
import tempfile
import unittest
from unittest import mock

import skills


class InteractiveReuseTests(unittest.TestCase):
    def test_comparison_cache_rejects_old_full_table_measurements(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "comparisons.json")
            with mock.patch.object(skills, "_COMPARISON_CACHE_PATH", path):
                with open(path, "w", encoding="utf-8") as fh:
                    json.dump({"old-query": {"baseline_seconds": 10}}, fh)
                self.assertEqual(skills._cache_load(), {})
                key = (("file.csv", 100, 123456789), "groupby", "region",
                       "revenue:sum", "None", "3")
                skills._cache_save({key: {"baseline_seconds": 1}})
                self.assertEqual(skills._cache_load(), {key: {"baseline_seconds": 1}})

    def test_parquet_hit_does_not_claim_an_uncached_speedup(self):
        payload = {"engine": "cudf", "total_seconds": .1,
                   "execution_decision": {"observed": {"parquet_cache": {"status": "hit"}}}}
        out = {}
        with mock.patch.object(skills, "_speedup_enabled", return_value=True), \
                mock.patch.object(skills, "_cpu_twin") as cpu:
            skills._attach_speedup(out, "unused.csv", "groupby", "region",
                                  "revenue:sum", None, 3, payload)
        cpu.assert_not_called()
        self.assertNotIn("gpu_vs_cpu", out)
        self.assertIn("gpu_vs_cpu_warning", out)

    def test_reuses_a_warm_frame_without_running_a_cpu_baseline(self):
        replies = [
            {"ok": True, "count": 0},
            {"ok": True, "session_id": "s2", "engine": "cudf", "cache_hit": True,
             "load_seconds": 0.0},
            {"ok": True, "engine": "cudf", "accelerated": True,
             "rows_scanned": 20, "groupby": {"by": "region", "groups": 2,
             "top_k": [{"region": "A", "revenue__sum": 10},
                       {"region": "B", "revenue__sum": 5}]},
             "execution_decision": {"observed": {"compute_seconds": 0.01}}},
            {"ok": True, "closed": 1, "cached": True},
        ]
        with mock.patch.object(skills, "_worker_call", side_effect=replies) as call, \
                mock.patch.object(skills, "_remember_last_file"):
            result = skills._resident_single_analysis("/data/sales.csv", "groupby",
                                                       "region", "revenue:sum", None, 2)
        parsed = json.loads(result)
        self.assertTrue(parsed["success"])
        self.assertTrue(parsed["resident_reuse"])
        self.assertEqual(parsed["rows_scanned"], 20)
        self.assertNotIn("gpu_vs_cpu", parsed)
        self.assertEqual(call.call_args_list[1].args[0]["measure_cpu"], False)
        self.assertEqual(call.call_args_list[-1].args[0],
                         {"cmd": "close", "sid": "s2", "retain": True})

    def test_refusal_returns_to_stateless_path(self):
        replies = [{"ok": True, "count": 0},
                   {"ok": False, "error": "not enough free device memory"}]
        with mock.patch.object(skills, "_worker_call", side_effect=replies):
            result = skills._resident_single_analysis("/data/sales.csv", "summary",
                                                       None, None, None, None)
        self.assertIsNone(result)

    def test_active_model_session_is_not_borrowed(self):
        with mock.patch.object(skills, "_worker_call",
                               return_value={"ok": True, "count": 1}) as call:
            result = skills._resident_single_analysis("/data/sales.csv", "summary",
                                                       None, None, None, None)
        self.assertIsNone(result)
        self.assertEqual(call.call_count, 1)


if __name__ == "__main__":
    unittest.main()
