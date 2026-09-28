"""Process and scheduling tests for the bounded multi-plan queue."""
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import batch_queue


class BatchQueueTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / "sales.csv").write_text(
            "region,revenue\nEast,10\nWest,20\nEast,30\n", encoding="utf-8")

    def tearDown(self):
        self.temp.cleanup()

    def plan(self, name, *, force_cpu=True, missing=False):
        path = self.root / f"{name}.json"
        path.write_text(json.dumps({
            "title": name, "file_path": "sales.csv", "force_cpu": force_cpu,
            "steps": [{"op": "summary", "columns": "missing" if missing else "revenue"}],
        }), encoding="utf-8")
        return path

    def manifest(self, names):
        path = self.root / "queue.json"
        path.write_text(json.dumps({"jobs": [
            {"name": name, "plan": f"{name}.json"} for name in names
        ]}), encoding="utf-8")
        return path

    def test_real_cpu_jobs_use_distinct_processes_and_reports(self):
        self.plan("first")
        self.plan("second")
        run_dir, summary = batch_queue.run(self.manifest(["first", "second"]),
                                           self.root / "out")
        self.assertTrue(summary["ok"])
        self.assertEqual(len({job["pid"] for job in summary["jobs"]}), 2)
        self.assertLess(max(job["started_at_utc"] for job in summary["jobs"]),
                        min(job["finished_at_utc"] for job in summary["jobs"]))
        for job in summary["jobs"]:
            self.assertEqual(job["lane"], "cpu")
            data = json.loads((Path(job["report_dir"]) / "result.json").read_text())
            self.assertTrue(data["result"]["success"])
            self.assertEqual(data["result"]["rows"], 3)
        self.assertTrue((run_dir / "queue-summary.json").is_file())

    def test_cpu_jobs_overlap_but_gpu_risk_job_is_exclusive(self):
        self.plan("cpu_a")
        self.plan("cpu_b")
        self.plan("gpu", force_cpu=False)
        self.plan("cpu_c")
        barrier = threading.Barrier(2)
        events = []
        lock = threading.Lock()

        def fake(job, _root, _timeout):
            with lock:
                events.append(("start", job.name))
            if job.name in {"cpu_a", "cpu_b"}:
                barrier.wait(timeout=3)
            time.sleep(0.01)
            with lock:
                events.append(("end", job.name))
            return {"name": job.name, "lane": job.lane, "ok": True}

        with patch.object(batch_queue, "_run_job", side_effect=fake):
            _, summary = batch_queue.run(
                self.manifest(["cpu_a", "cpu_b", "gpu", "cpu_c"]),
                self.root / "out", max_cpu_workers=2)
        self.assertTrue(summary["ok"])
        self.assertEqual(summary["jobs"][2]["lane"], "gpu_exclusive")
        self.assertLess(events.index(("start", "cpu_a")), events.index(("end", "cpu_b")))
        self.assertLess(events.index(("start", "cpu_b")), events.index(("end", "cpu_a")))
        self.assertLess(events.index(("end", "cpu_a")), events.index(("start", "gpu")))
        self.assertLess(events.index(("end", "cpu_b")), events.index(("start", "gpu")))
        self.assertLess(events.index(("end", "gpu")), events.index(("start", "cpu_c")))

    def test_failed_job_keeps_other_result_and_returns_failure(self):
        self.plan("good")
        self.plan("bad", missing=True)
        _, summary = batch_queue.run(self.manifest(["good", "bad"]), self.root / "out")
        self.assertFalse(summary["ok"])
        self.assertTrue(summary["jobs"][0]["ok"])
        self.assertFalse(summary["jobs"][1]["ok"])
        self.assertIn("not found", summary["jobs"][1]["error"])

    def test_rejects_duplicate_names_and_unbounded_concurrency(self):
        self.plan("same")
        with self.assertRaisesRegex(ValueError, "duplicate"):
            batch_queue.load_manifest(self.manifest(["same", "same"]))
        with self.assertRaisesRegex(ValueError, "max_cpu_workers"):
            batch_queue.run(self.manifest(["same"]), self.root / "out", 5)

    def test_timeout_stops_job_process(self):
        slow = self.root / "slow.py"
        slow.write_text("import time\ntime.sleep(30)\n", encoding="utf-8")
        self.plan("slow")
        job = batch_queue.load_manifest(self.manifest(["slow"]))[0]
        with patch.object(batch_queue.batch_job, "__file__", str(slow)):
            try:
                result = batch_queue._run_job(job, self.root / "out", timeout_seconds=0.1)
            except RuntimeError as exc:
                # A sandbox may deny the OS process-tree terminator. It must
                # report that limitation instead of falsely claiming cleanup.
                self.assertIn("could not verify termination", str(exc))
                return
        self.assertFalse(result["ok"])
        self.assertIn("process group was stopped", result["error"])
        self.assertLess(result["seconds"], 5)


if __name__ == "__main__":
    unittest.main()
