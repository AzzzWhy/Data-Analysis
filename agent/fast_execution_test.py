"""Real CPU worker tests plus deterministic cleanup, parity and transport faults."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

import skills

SCRIPTS = Path(__file__).resolve().parents[1] / "skills/cudf-analytics/scripts"
sys.path.insert(0, str(SCRIPTS))
import gpu_session as gs


class FastExecutionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "fixture.csv"
        self.path.write_text("region,revenue,unused\nA,1,x\nA,3,y\nB,100,z\n", encoding="utf-8")
        gs.do_close({"sid": "all"})

    def tearDown(self):
        gs.do_close({"sid": "all"})
        self.temp.cleanup()

    def test_no_hidden_cpu_baseline_by_default(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertFalse(skills._speedup_enabled())
        with mock.patch.dict(os.environ, {"SKILL_SHOW_SPEEDUP": "1"}):
            self.assertTrue(skills._speedup_enabled())

    def test_real_oneshot_reuses_process_and_matches_cli(self):
        with mock.patch.object(skills, "_remember_last_file"), \
                mock.patch.dict(os.environ, {"GPU_ANALYSIS_PERSISTENT_WORKER": "1"}):
            first = json.loads(skills.analyze_dataset(str(self.path), "summary",
                                                       columns="revenue", force_cpu=True))
            pid = skills._worker.pid
            second = json.loads(skills.analyze_dataset(str(self.path), "summary",
                                                        columns="revenue", force_cpu=True))
            self.assertEqual(pid, skills._worker.pid)
            with mock.patch.dict(os.environ, {"GPU_ANALYSIS_PERSISTENT_WORKER": "0"}):
                cli = json.loads(skills.analyze_dataset(str(self.path), "summary",
                                                        columns="revenue", force_cpu=True))
        for reply in (first, second, cli):
            self.assertTrue(reply["success"], reply)
            self.assertGreaterEqual(reply["phase_timings"]["load_seconds"], 0)
            self.assertGreaterEqual(reply["phase_timings"]["compute_seconds"], 0)
        self.assertEqual(first["result"]["summary"]["stats"], cli["result"]["summary"]["stats"])
        self.assertEqual(first["transport_timings"]["mode"], "persistent_worker")
        self.assertEqual(skills._worker_call({"cmd": "list"})["count"], 0)

    def test_batch_projects_union_and_reuses_statistics(self):
        steps = [{"op": "summary", "columns": "revenue"},
                 {"op": "outliers", "columns": "revenue"},
                 {"op": "groupby", "by": "region", "agg": "revenue:sum"}]
        with mock.patch.object(gs.GA, "_read_table", wraps=gs.GA._read_table) as read, \
                mock.patch.object(gs, "_free_gpu_gb", side_effect=AssertionError("CPU must not probe GPU")):
            reply = gs.do_batch({"path": str(self.path), "steps": steps, "force_cpu": True})
        self.assertTrue(reply["ok"], reply)
        self.assertEqual(read.call_count, 1)
        self.assertEqual(reply["projected_columns"], ["region", "revenue"])
        self.assertEqual(reply["results"][1]["outliers"]["statistics_reused_columns"], ["revenue"])
        self.assertEqual(gs.SESSIONS, {})

    def test_bad_step_stops_and_closes(self):
        reply = gs.do_batch({"path": str(self.path), "force_cpu": True, "steps": [
            {"op": "summary", "columns": "revenue"},
            {"op": "groupby", "by": "region", "agg": "revenue:not_a_function"}]})
        self.assertFalse(reply["ok"])
        self.assertEqual(reply["failed_step"], 1)
        self.assertEqual(reply["completed_steps"], 1)
        self.assertEqual(gs.SESSIONS, {})

    def test_profile_preserves_full_schema(self):
        reply = gs.do_batch({"path": str(self.path), "force_cpu": True,
                              "steps": [{"op": "profile"}]})
        self.assertTrue(reply["ok"], reply)
        self.assertIsNone(reply["projected_columns"])

    def test_default_outlier_example_limit_matches_single(self):
        self.path.write_text("region,revenue\n" + "A,1\n" * 100 + "B,10000\n" * 25,
                             encoding="utf-8")
        single = gs.do_oneshot({"argv": ["--input", str(self.path), "--op", "outliers",
                                        "--columns", "revenue", "--force-cpu"]})
        batch = gs.do_batch({"path": str(self.path), "force_cpu": True,
                            "steps": [{"op": "outliers", "columns": "revenue"}]})
        self.assertEqual(single["payload"]["outliers"]["results"],
                         batch["results"][0]["outliers"]["results"])
        self.assertEqual(len(batch["results"][0]["outliers"]["results"]["revenue"]["examples"]), 20)

    def test_benchmark_mode_avoids_warm_vs_cold_comparison(self):
        with mock.patch.dict(os.environ, {"SKILL_SHOW_SPEEDUP": "1"}), \
                mock.patch.object(skills, "_worker_call") as worker, \
                mock.patch.object(skills, "_remember_last_file"):
            result = json.loads(skills.analyze_dataset(str(self.path), "profile", force_cpu=True))
        self.assertTrue(result["success"])
        worker.assert_not_called()

    def test_file_changed_between_steps_is_refused(self):
        analyze = gs.do_analyze
        def change_after_summary(req):
            reply = analyze(req)
            if req["op"] == "summary":
                with self.path.open("a", encoding="utf-8") as stream:
                    stream.write("A,2,new\n")
            return reply
        with mock.patch.object(gs, "do_analyze", side_effect=change_after_summary):
            reply = gs.do_batch({"path": str(self.path), "force_cpu": True, "steps": [
                {"op": "summary", "columns": "revenue"},
                {"op": "outliers", "columns": "revenue"}]})
        self.assertFalse(reply["ok"])
        self.assertIn("changed while", reply["error"])
        self.assertEqual(gs.SESSIONS, {})

    def test_model_session_is_not_borrowed(self):
        opened = gs.do_open({"path": str(self.path), "force_cpu": True, "measure_cpu": False})
        reply = gs.do_batch({"path": str(self.path), "force_cpu": True,
                              "steps": [{"op": "profile"}]})
        self.assertFalse(reply["ok"])
        self.assertIn(opened["session_id"], gs.SESSIONS)

    def test_invalid_batch_rejected_before_read(self):
        for steps in ([], [{"op": "wrong"}], [{"op": "profile", "command": "bad"}],
                      [{"op": "groupby"}], [{"op": "summary", "top_k": True}],
                      [{"op": "summary", "columns": ["revenue"]}], [{"op": "profile"}] * 9):
            with mock.patch.object(gs, "do_open") as opened:
                self.assertFalse(gs.do_batch({"path": str(self.path), "steps": steps})["ok"])
                opened.assert_not_called()

    def test_worker_timeout_is_bounded_and_restarts(self):
        skills._worker_stop(skills._worker)
        skills._worker = None
        bad = Path(self.temp.name) / "hung.py"
        bad.write_text("import time\ntime.sleep(60)\n", encoding="utf-8")
        with mock.patch.object(skills, "_session_script", return_value=str(bad)):
            reply = skills._worker_call({"cmd": "list"}, timeout=.1)
        self.assertFalse(reply["ok"])
        self.assertIn("timed out", reply["error"])
        self.assertIsNone(skills._worker)
        self.assertTrue(skills._worker_call({"cmd": "list"})["ok"])

    def test_transport_failure_uses_cli_fallback(self):
        with mock.patch.object(skills, "_worker_call", return_value={"ok": False}), \
                mock.patch.object(skills, "_remember_last_file"):
            reply = json.loads(skills.analyze_dataset(str(self.path), "profile", force_cpu=True))
        self.assertTrue(reply["success"])
        self.assertEqual(reply["transport_timings"]["mode"], "subprocess")

    def test_tool_batch_is_registered_and_returns_results(self):
        self.assertIn("analyze_batch", skills.skill_func_map)
        with mock.patch.object(skills, "_remember_last_file"):
            reply = json.loads(skills.analyze_batch(str(self.path),
                              [{"op": "summary", "columns": "revenue"}], force_cpu=True))
        self.assertTrue(reply["success"], reply)
        self.assertEqual(len(reply["results"]), 1)


if __name__ == "__main__":
    try:
        unittest.main()
    finally:
        skills._worker_stop(skills._worker)
