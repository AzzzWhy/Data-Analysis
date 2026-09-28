"""Simulated device contracts; actual device parity is tested by hybrid_calibrate."""
from contextlib import ExitStack
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import pandas as pd
import gpu_analytics as ga
import gpu_session as gs
import hybrid_execution as hybrid


class BatchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.temp.name) / "data.parquet")
        pd.DataFrame({"region": [1]*19+[2], "revenue": [1.]*19+[100.],
                      "ignored": ["not read"]*20}).to_parquet(self.path, index=False)
        self.steps = [{"op": "summary", "columns": "revenue"},
                      {"op": "outliers", "columns": "revenue", "top_k": 3},
                      {"op": "groupby", "by": "region", "agg": "revenue:sum", "top_k": 3}]
        self.stack = ExitStack()
        self.gpu = ga.Engine("cudf", pd)
        self.cpu = ga.Engine("pandas", pd)
        self.stack.enter_context(patch.object(ga, "_ENGINE", self.gpu))
        self.stack.enter_context(patch.object(ga, "detect_engine", side_effect=lambda force_cpu=False, **kw:
                                             self.cpu if force_cpu else self.gpu))
        self.stack.enter_context(patch.object(gs, "_free_gpu_gb", return_value=100.))
        self.stack.enter_context(patch.object(ga, "sync_device"))
        self.stack.enter_context(patch.object(gs, "_release_unused_gpu_blocks"))
        self.reads = []
        def read(engine, path, columns, trace):
            self.reads.append(columns)
            trace.update(read_count=1, conversion_count=1, cpu_read_seconds=.01, conversion_seconds=.01)
            return pd.read_parquet(path, columns=list(columns))
        self.reader = self.stack.enter_context(patch.object(hybrid, "read_gpu", side_effect=read))
        gs.SESSIONS.clear()
        gs.WARM_CACHE.clear()

    def tearDown(self):
        gs.SESSIONS.clear()
        gs.WARM_CACHE.clear()
        self.stack.close()
        self.temp.cleanup()

    def batch(self, **kwargs):
        return gs.do_batch({"path": self.path, "steps": self.steps, "load_backend": "cpu_gpu", **kwargs})

    def test_union_one_read_conversion_and_exact_statistics_reuse(self):
        reply = self.batch()
        self.assertTrue(reply["ok"], reply)
        self.assertEqual(set(self.reads[0]), {"region", "revenue"})
        self.assertEqual(self.reader.call_count, 1)
        self.assertEqual(reply["loading"]["actual"], "cpu_gpu")
        self.assertEqual(reply["results"][1]["outliers"]["statistics_reused_columns"], ["revenue"])
        self.assertTrue(all(result["rows_scanned"] == 20 for result in reply["results"]))
        self.assertEqual(gs.SESSIONS, {})
        self.assertEqual(gs.WARM_CACHE, {})

    def test_batches_reload_instead_of_caching_frames(self):
        self.assertTrue(self.batch()["ok"])
        self.assertTrue(self.batch()["ok"])
        self.assertEqual(self.reader.call_count, 2)

    def test_memory_refusal_does_not_retry_cpu_or_create_session(self):
        self.reader.side_effect = hybrid.HybridMemoryRefused("budget refused")
        reply = self.batch()
        self.assertFalse(reply["ok"])
        self.assertTrue(reply["load_refused"])
        self.assertEqual(self.reader.call_count, 1)
        self.assertEqual(gs.SESSIONS, {})

    def test_conversion_failure_falls_back_and_reports_cpu(self):
        self.reader.side_effect = hybrid.HybridRefused("conversion failed")
        reply = self.batch()
        self.assertTrue(reply["ok"], reply)
        self.assertEqual(reply["loading"]["actual"], "cpu")
        self.assertTrue(all(result["engine"] == "pandas" for result in reply["results"]))
        self.assertEqual(gs.SESSIONS, {})

    def test_step_failure_still_closes_session(self):
        with patch.object(gs, "do_analyze", return_value={"ok": False, "error": "step failed"}):
            reply = self.batch()
        self.assertFalse(reply["ok"])
        self.assertEqual(reply["failed_step"], 0)
        self.assertEqual(gs.SESSIONS, {})

    def test_active_session_is_not_borrowed_or_closed(self):
        gs.SESSIONS["sExisting"] = gs.Session("sExisting", self.path, self.gpu,
            pd.read_parquet(self.path), 20, gs._file_identity(self.path), 0.)
        self.assertFalse(self.batch()["ok"])
        self.assertIn("sExisting", gs.SESSIONS)
        self.reader.assert_not_called()

    def test_auto_uses_exact_batch_decision(self):
        with patch.object(hybrid, "choose", return_value=("cpu_gpu", {"selected": "cpu_gpu"})) as choose:
            reply = self.batch(load_backend="auto")
        self.assertTrue(reply["ok"], reply)
        self.assertEqual(choose.call_args.args[-1], "batch_warm")
        self.assertEqual(choose.call_args.args[-2], gs.batch_workflow(self.steps))
        self.assertEqual(reply["loading"]["decision"]["selected"], "cpu_gpu")

    def test_explicit_cpu_wins_and_conflicts_rejected(self):
        with patch.object(hybrid, "choose") as choose:
            reply = self.batch(load_backend="auto", force_cpu=True)
        self.assertTrue(reply["ok"], reply)
        choose.assert_not_called()
        self.reader.assert_not_called()
        self.assertFalse(self.batch(force_cpu=True)["ok"])

    def test_interactive_session_can_keep_cpu_loaded_gpu_frame(self):
        opened = gs.do_open({"path": self.path, "usecols": "region,revenue",
                             "load_backend": "cpu_gpu", "measure_cpu": False})
        self.assertTrue(opened["ok"], opened)
        self.assertEqual(opened["engine"], "cudf")
        self.assertEqual(opened["loading"]["actual"], "cpu_gpu")
        step = gs.do_analyze({"sid": opened["session_id"], "op": "summary",
                              "columns": "revenue"})
        self.assertTrue(step["ok"], step)
        self.assertEqual(step["rows_scanned"], 20)
        self.assertEqual(self.reader.call_count, 1)
        gs.do_close({"sid": opened["session_id"]})

    def test_interactive_hybrid_preflight_rejects_before_gpu_probe(self):
        with patch.object(gs, "_free_gpu_gb", side_effect=AssertionError("GPU probed")):
            opened = gs.do_open({"path": self.path, "load_backend": "cpu_gpu",
                                 "measure_cpu": False})
        self.assertFalse(opened["ok"])
        self.assertIn("numeric", opened["error"])
        self.reader.assert_not_called()

    def test_cached_hybrid_is_not_reused_for_explicit_native_load(self):
        first = gs.do_open({"path": self.path, "usecols": "region,revenue",
                            "load_backend": "cpu_gpu", "measure_cpu": False})
        self.assertTrue(first["ok"], first)
        gs.do_close({"sid": first["session_id"], "retain": True})
        native = gs.do_open({"path": self.path, "usecols": "region,revenue",
                             "load_backend": "native", "force_gpu": True,
                             "measure_cpu": False})
        self.assertTrue(native["ok"], native)
        self.assertFalse(native.get("cache_hit", False))
        self.assertEqual(native["loading"]["actual"], "native_gpu")
        self.assertEqual(self.reader.call_count, 1)
        gs.do_close({"sid": native["session_id"]})

    def test_active_session_cannot_silently_change_loader(self):
        first = gs.do_open({"path": self.path, "usecols": "region,revenue",
                            "load_backend": "cpu_gpu", "measure_cpu": False})
        self.assertTrue(first["ok"], first)
        second = gs.do_open({"path": self.path, "usecols": "region,revenue",
                             "load_backend": "native", "force_gpu": True,
                             "measure_cpu": False})
        self.assertFalse(second["ok"])
        self.assertEqual(second["session_id"], first["session_id"])
        self.assertIn(first["session_id"], gs.SESSIONS)

    def test_preflight_cpu_route_is_not_labelled_caller_override(self):
        json_path = Path(self.temp.name) / "data.jsonl"
        json_path.write_text('{"region": 1, "revenue": 2}\n', encoding="utf-8")
        with patch.object(gs, "_free_gpu_gb", side_effect=AssertionError("GPU probed")):
            opened = gs.do_open({"path": str(json_path), "load_backend": "auto",
                                 "measure_cpu": False})
        self.assertTrue(opened["ok"], opened)
        self.assertEqual(opened["engine"], "pandas")
        self.assertEqual(opened["execution_decision"]["mode"], "auto")
        self.assertEqual(opened["execution_decision"]["policy"], "capability_preflight")

    def test_uncalibrated_batch_reports_the_actual_cpu_route_reason(self):
        with patch.object(hybrid, "choose", return_value=(None, {
                "reason": "no matching, verified calibration"})), \
                patch.object(ga, "pick_engine_for", return_value=(False,
                    "measured file-size crossover selected CPU")):
            reply = gs.do_batch({"path": self.path, "steps": self.steps,
                                 "load_backend": "auto"})
        self.assertTrue(reply["ok"], reply)
        self.assertEqual(reply["loading"]["decision"]["selected"], "cpu")
        self.assertEqual(reply["loading"]["decision"]["policy"],
                         "measured_file_size_crossover")
        self.assertEqual(reply["results"][0]["routing_reason"],
                         "measured file-size crossover selected CPU")
        self.assertEqual(reply["loading"]["open_decision"]["mode"], "auto")


if __name__ == "__main__":
    unittest.main()
