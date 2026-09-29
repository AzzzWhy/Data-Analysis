"""Merge regression: real CPU worker through GUI, no model/network or user settings."""
from pathlib import Path
import os
import sys
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
os.environ["GPU_ANALYTICS_SCRIPT"] = str(ROOT / "skills/cudf-analytics/scripts/gpu_analytics.py")
os.environ["GPU_ANALYTICS_PYTHON"] = sys.executable

import gui
import skills


class MainIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.config_patch = mock.patch.object(gui, "load_config", return_value=gui.APIConfig())
        self.config_patch.start()
        self.addCleanup(self.config_patch.stop)
        self.client_patch = mock.patch.object(gui, "build_client", side_effect=AssertionError("no model calls"))
        self.client_patch.start()
        self.addCleanup(self.client_patch.stop)
        self.workbench = gui.Workbench()
        self.addCleanup(self.stop_worker)
        self.path = str(ROOT / "benchmark/portability/fixture.csv")

    @staticmethod
    def stop_worker():
        if skills._worker is not None:
            skills._worker_stop(skills._worker)
            skills._worker = None

    def run_tool(self, **args):
        job = self.workbench._acquire("run", "dataset_session")
        self.assertIsNotNone(job)
        self.workbench._run_direct(job, "dataset_session", args)
        results = [data["result"] for _, kind, data in job.events if kind == "tool_result"]
        self.assertEqual(len(results), 1, list(job.events))
        self.assertTrue(job.done)
        self.assertFalse(self.workbench.busy)
        return job, results[0]

    def test_reuse_keeps_chinese_plan_and_real_cpu_analysis(self):
        job, opened = self.run_tool(operation="open", file_path=self.path, force_cpu=True)
        self.assertEqual(job.status, "succeeded", opened)
        sid = opened["session_id"]
        goal = "分析收入分布，检查异常值"
        job, reused = self.run_tool(operation="open", file_path=self.path,
                                    force_cpu=True, goal=goal)
        self.assertEqual(job.status, "succeeded", reused)
        self.assertEqual(reused["session_id"], sid)
        self.assertTrue(reused["reused_existing_session"])
        self.assertEqual(reused["loading"]["read_count"], 0)
        self.assertEqual(reused["plan"]["goal"], goal)
        job, analyzed = self.run_tool(operation="analyze", session_id=sid,
                                      op="summary", columns="revenue")
        self.assertEqual(job.status, "succeeded", analyzed)
        self.assertEqual(analyzed["engine"], "pandas")
        self.assertFalse(analyzed["accelerated"])
        self.assertIn("plan", analyzed)
        self.assertIn("session", [kind for _, kind, _ in job.events])

    def test_active_cpu_session_rejects_engine_switch_without_losing_plan(self):
        _, opened = self.run_tool(operation="open", file_path=self.path,
                                  force_cpu=True, goal="汇总收入")
        job, refused = self.run_tool(operation="open", file_path=self.path, force_gpu=True)
        self.assertEqual(job.status, "failed", refused)
        self.assertIn("different engine", refused["error"])
        job, reused = self.run_tool(operation="open", file_path=self.path, force_cpu=True)
        self.assertEqual(job.status, "succeeded", reused)
        self.assertEqual(reused["session_id"], opened["session_id"])
        self.assertEqual(reused["plan"], opened["plan"])


if __name__ == "__main__":
    unittest.main()
