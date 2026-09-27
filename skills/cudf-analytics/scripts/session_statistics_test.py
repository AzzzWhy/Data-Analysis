"""Offline tests for bounded, file-validated and engine-isolated quartile reuse."""
import os
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

    def test_cache_limit_and_disable(self):
        with mock.patch.object(gs, "STATISTICS_CACHE_COLUMNS", 1):
            self.analyze("summary")
        self.assertEqual(len(self.session.statistics), 1)
        self.session.statistics.clear()
        with mock.patch.object(gs, "STATISTICS_CACHE_COLUMNS", 0):
            self.analyze("summary")
            result = self.analyze("outliers")
        self.assertEqual(self.session.statistics, {})
        self.assertEqual(result["outliers"]["statistics_reused_columns"], [])

    def test_other_engine_statistics_are_not_used(self):
        self.session.statistics[("cudf", "a")] = {"q1": -999., "q3": 999.}
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
