"""Cross-process GPU slot and worker routing contracts."""
from contextlib import contextmanager
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import gpu_coordination as coordination
import gpu_session as session
import gpu_analytics as analytics


class GpuCoordinationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.temp.name) / "gpu.lock")
        self.env = patch.dict(os.environ, {"GPU_ANALYSIS_GPU_SLOT_FILE": self.path})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def test_another_process_holds_slot_and_timeout_then_release(self):
        code = ("from gpu_coordination import gpu_slot\n"
                "import time\n"
                "with gpu_slot():\n"
                " print('locked', flush=True)\n"
                " time.sleep(0.4)\n")
        child = subprocess.Popen([sys.executable, "-c", code], cwd=Path(__file__).parent,
                                 env=os.environ.copy(), stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, text=True)
        try:
            self.assertEqual(child.stdout.readline().strip(), "locked")
            with self.assertRaisesRegex(TimeoutError, "GPU compute slot busy"):
                with coordination.gpu_slot(timeout_seconds=0.1):
                    pass
            self.assertEqual(child.wait(timeout=3), 0)
            with coordination.gpu_slot(timeout_seconds=0.1):
                pass
        finally:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=3)
            child.stdout.close()
            child.stderr.close()

    def test_exception_releases_slot(self):
        with self.assertRaisesRegex(RuntimeError, "boom"):
            with coordination.gpu_slot():
                raise RuntimeError("boom")
        with coordination.gpu_slot(timeout_seconds=0.1):
            pass

    def test_nested_worker_cli_acquisition_does_not_deadlock(self):
        with coordination.gpu_slot():
            with coordination.gpu_slot(timeout_seconds=0.1) as waited:
                self.assertEqual(waited, 0)

    def test_cli_and_cpu_forced_paths(self):
        calls = []

        @contextmanager
        def fake_slot():
            calls.append("slot")
            yield 0

        with patch.object(coordination, "gpu_slot", fake_slot), patch.object(
                analytics, "_uncoordinated_main", return_value=0):
            self.assertEqual(analytics.main(["--force-cpu"]), 0)
            self.assertEqual(calls, [])
            self.assertEqual(analytics.main(["--engine", "cpu"]), 0)
            self.assertEqual(calls, [])
            self.assertEqual(analytics.main(["--engine", "gpu"]), 0)
            self.assertEqual(calls, ["slot"])

    def test_worker_skips_cpu_and_locks_gpu_batch(self):
        calls = []

        @contextmanager
        def fake_slot():
            calls.append("enter")
            yield 0.125
            calls.append("exit")

        with patch.object(session, "gpu_slot", fake_slot), patch.dict(
                session.HANDLERS, {"batch": lambda _req: {"ok": True}}):
            self.assertEqual(session.handle({"cmd": "batch", "force_cpu": True}),
                             {"ok": True})
            self.assertEqual(calls, [])
            result = session.handle({"cmd": "batch", "force_cpu": False})
            self.assertEqual(calls, ["enter", "exit"])
            self.assertEqual(result["coordination"]["gpu_slot_wait_seconds"], 0.125)


if __name__ == "__main__":
    unittest.main()
