"""Offline tests for bounded, file-validated and engine-isolated quartile reuse."""
import os
import json
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import pandas as pd
import gpu_analytics as ga
import gpu_session as gs


class SessionStatisticsTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.root.name, "data.csv")
        self.frame = pd.DataFrame({"a": [1., 2., 3., 4., 100., None],
                                   "b": [float("nan")] * 6})
        self.frame.to_csv(self.path, index=False)
        self.sessions = mock.patch.dict(gs.SESSIONS, {}, clear=True)
        self.sessions.start()
        self.warm = mock.patch.dict(gs.WARM_CACHE, {}, clear=True)
        self.warm.start()
        self.session = gs.Session("test", self.path, ga.Engine("pandas", pd),
                                  self.frame, len(self.frame), gs._file_identity(self.path), 0.)
        gs.SESSIONS["test"] = self.session

    def tearDown(self):
        self.warm.stop()
        self.sessions.stop()
        self.root.cleanup()

    def analyze(self, op, **kwargs):
        result = gs.do_analyze({"sid": "test", "op": op, **kwargs})
        self.assertTrue(result["ok"], result)
        return result

    def test_summary_then_outliers_reuses_exact_quartiles_not_results(self):
        self.analyze("summary", columns="a,b")
        args = gs._build_args({"columns": "a,b", "iqr_k": 2})
        expected = ga.op_outliers(self.frame, self.session.engine, args)
        with mock.patch.object(ga, "_quartiles", side_effect=AssertionError("recomputed")):
            result = self.analyze("outliers", columns="a,b", iqr_k=2)
        self.assertEqual(result["outliers"]["results"], expected["results"])
        self.assertEqual(result["outliers"]["statistics_reused_columns"], ["a", "b"])
        self.assertEqual(result["rows_scanned"], len(self.frame))
        self.assertEqual(result["execution_decision"]["observed"]["rows_scanned"], 6)

    def test_changed_file_invalidates_statistics_and_session(self):
        self.analyze("summary")
        self.frame.iloc[:2].to_csv(self.path, index=False)
        with mock.patch.object(ga, "execute") as execute:
            result = gs.do_analyze({"sid": "test", "op": "outliers"})
        self.assertFalse(result["ok"])
        self.assertNotIn("test", gs.SESSIONS)
        execute.assert_not_called()

    def test_non_null_count_reuse_avoids_another_scan(self):
        self.analyze("summary", columns="a")
        with mock.patch.object(pd.Series, "notnull", side_effect=AssertionError("extra scan")) as scan:
            result = self.analyze("outliers", columns="a")
        scan.assert_not_called()
        values = result["outliers"]["results"]["a"]
        self.assertEqual(values["valid_count"], 5)
        self.assertEqual(values["pct"], 20.)
        self.assertEqual(result["execution_decision"]["observed"]["valid_count_reused_columns"], ["a"])

    def test_outlier_count_is_not_cached_as_non_null_count(self):
        first = self.analyze("outliers", columns="a")
        second = self.analyze("outliers", columns="a")
        self.assertEqual(first["outliers"]["results"], second["outliers"]["results"])
        self.assertEqual(second["outliers"]["results"]["a"]["valid_count"], 5)
        self.assertEqual(second["outliers"]["valid_count_reused_columns"], ["a"])

    def test_engine_version_isolation_and_snapshot_copies(self):
        self.analyze("summary", columns="a")
        cache = self.session.statistics
        snapshot = cache.for_engine(self.session.engine)
        snapshot["a"]["valid_count"] = 0
        self.assertEqual(cache.for_engine(self.session.engine)["a"]["valid_count"], 5)
        self.assertEqual(cache.for_engine(ga.Engine("pandas", pd, version="different")), {})

    def test_disabling_cache_ignores_existing_entries(self):
        self.analyze("summary", columns="a")
        with mock.patch.object(gs, "STATISTICS_CACHE_COLUMNS", 0):
            result = self.analyze("outliers", columns="a")
        self.assertEqual(result["outliers"]["statistics_reused_columns"], [])
        self.assertEqual(result["outliers"]["valid_count_reused_columns"], [])
        self.assertEqual(len(self.session.statistics), 0)

    def test_fence_ties_preserve_original_tolerance_rule(self):
        frame = pd.DataFrame({"a": [0., 1., 2., 3., 4., 10., float("nan")]})
        args = gs._build_args({"columns": "a", "iqr_k": .5})
        result = ga.op_outliers(frame, self.session.engine, args)["results"]["a"]
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["valid_count"], 6)
        self.assertEqual(result["fence_ties_excluded"], 1)
        self.assertEqual(result["examples"], [{"a": 10.}])

    def test_actual_worker_protocol_loads_the_new_cache_module(self):
        requests = [
            {"cmd": "open", "path": self.path, "force_cpu": True, "measure_cpu": False},
            {"cmd": "analyze", "sid": "s1", "op": "summary", "columns": "a"},
            {"cmd": "analyze", "sid": "s1", "op": "outliers", "columns": "a"},
            {"cmd": "close", "sid": "all"},
        ]
        env = dict(os.environ, SESSION_STATISTICS_CACHE_COLUMNS="128", SESSION_MAX="4")
        run = subprocess.run([sys.executable, gs.__file__],
                             input="\n".join(json.dumps(req) for req in requests) + "\n",
                             capture_output=True, text=True, timeout=30, env=env)
        self.assertEqual(run.returncode, 0, run.stderr)
        replies = [json.loads(line) for line in run.stdout.splitlines()]
        self.assertEqual(len(replies), 4)
        self.assertTrue(all(reply["ok"] for reply in replies), replies)
        self.assertEqual(replies[2]["outliers"]["valid_count_reused_columns"], ["a"])
        self.assertEqual(replies[2]["outliers"]["results"]["a"]["pct"], 20.)

    def test_cache_limit_and_disable(self):
        with mock.patch.object(gs, "STATISTICS_CACHE_COLUMNS", 1):
            self.analyze("summary")
        self.assertEqual(len(self.session.statistics), 1)
        self.session.statistics.clear()
        with mock.patch.object(gs, "STATISTICS_CACHE_COLUMNS", 0):
            self.analyze("summary")
            result = self.analyze("outliers")
        self.assertEqual(len(self.session.statistics), 0)
        self.assertEqual(result["outliers"]["statistics_reused_columns"], [])

    def test_other_engine_statistics_are_not_used(self):
        self.session.statistics.record("summary", {"stats": {
            "a": {"q1": -999., "q3": 999., "count": 6}}}, ga.Engine("cudf", pd), 128)
        with mock.patch.object(ga, "_quartiles", wraps=ga._quartiles) as quartiles:
            result = self.analyze("outliers", columns="a")
        self.assertEqual(quartiles.call_count, 1)
        self.assertEqual(result["outliers"]["statistics_reused_columns"], [])
        self.assertEqual(result["outliers"]["results"]["a"]["count"], 1)

    def test_fallback_requests_statistics_for_actual_engine(self):
        args = gs._build_args({"columns": "a"})
        gpu = ga.Engine("cudf", pd)
        original = ga.op_outliers
        attempts = []

        def operation(frame, engine, args, **kwargs):
            attempts.append((engine.name, kwargs.get("summary_stats")))
            if engine.is_gpu:
                raise ga.OpNotSupported("test fallback")
            return original(frame, engine, args, **kwargs)

        with mock.patch.object(ga, "op_outliers", side_effect=operation), \
                mock.patch.object(ga, "detect_engine", return_value=self.session.engine):
            result, used, _, fallback, _ = ga.execute(
                gpu, lambda _engine: self.frame, "outliers", args,
                statistics_for=lambda engine: {"a": {"q1": -999., "q3": 999.}}
                if engine.is_gpu else {})
        self.assertEqual(used.name, "pandas")
        self.assertTrue(fallback)
        self.assertEqual(attempts[1], ("pandas", {}))
        self.assertEqual(result["statistics_reused_columns"], [])
        self.assertEqual(result["results"]["a"]["count"], 1)

    def test_statistics_follow_retained_frame_and_are_released_on_close_all(self):
        # Fake a GPU-labelled pandas frame only to exercise lifecycle, not GPU speed.
        self.session.engine = ga.Engine("cudf", pd)
        self.analyze("summary", columns="a")
        closed = gs.do_close({"sid": "test", "retain": True})
        self.assertTrue(closed["cached"])
        opened = gs.do_open({"path": self.path, "measure_cpu": False})
        self.assertTrue(opened["cache_hit"])
        with mock.patch.object(ga, "_quartiles", side_effect=AssertionError("recomputed")):
            result = gs.do_analyze({"sid": opened["session_id"], "op": "outliers",
                                    "columns": "a"})
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["outliers"]["statistics_reused_columns"], ["a"])
        gs.do_close({"sid": "all"})
        self.assertEqual(gs.SESSIONS, {})
        self.assertEqual(gs.WARM_CACHE, {})


if __name__ == "__main__":
    unittest.main()
