"""CPU-only real Arrow tests and simulated GPU conversion/admission contracts."""
import contextlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import hybrid_execution as hybrid
import gpu_analytics as ga


class HybridTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.temp.name) / "data.parquet")
        self.profile = str(Path(self.temp.name) / "profile.json")
        pq.write_table(pa.table({"region": [1, 2, 1], "revenue": [1., None, 100.],
                                "ignored": ["a", "b", "c"]}), self.path)
        self.workflow = [{"op": "summary", "columns": "revenue"}]
        self.columns = ["region", "revenue"]
        self.engine = SimpleNamespace(is_gpu=True, name="cudf", mod=SimpleNamespace(
            DataFrame=SimpleNamespace(from_arrow=lambda table: table.to_pandas())))
        self.sync = SimpleNamespace(deviceSynchronize=lambda: None)
        self.cuda = SimpleNamespace(cuda=SimpleNamespace(runtime=self.sync))
        self.large_memory = patch.object(hybrid, "memory_available", return_value=(100*1024**3, 100*1024**3))
        self.large_memory.start()
        self.fake_cuda = patch.dict(sys.modules, {"cupy": self.cuda})
        self.fake_cuda.start()

    def tearDown(self):
        self.fake_cuda.stop()
        self.large_memory.stop()
        self.temp.cleanup()

    def measures(self, hybrid_seconds=2.):
        return {name: [{"seconds": total, "read_seconds": read, "compute_seconds": compute}]*2
                for name, total, read, compute in (
                    ("cpu", 6., .8, 5.), ("native", 3., 1., 1.5),
                    ("cpu_gpu", hybrid_seconds, .3, 1.5))}

    def save(self, measurements=None, context="oneshot_warm"):
        hybrid.save_measurement(self.profile, self.path, self.columns, self.workflow,
                                context, measurements or self.measures())

    def select(self, **changes):
        return hybrid.choose(self.profile, self.path, changes.get("columns", self.columns),
                             changes.get("workflow", self.workflow), changes.get("context", "oneshot_warm"))[0]

    def test_real_arrow_projection_conversion_once(self):
        trace = {}
        with patch.object(pq, "read_table", wraps=pq.read_table) as read:
            frame = hybrid.read_gpu(self.engine, self.path, self.columns, trace)
        self.assertEqual(list(frame.columns), self.columns)
        self.assertEqual(len(frame), 3)
        self.assertTrue(pd.isna(frame.revenue.iloc[1]))
        self.assertEqual(read.call_count, 1)
        self.assertEqual((trace["read_count"], trace["conversion_count"]), (1, 1))
        self.assertGreaterEqual(trace["conversion_seconds"], 0)

    def test_memory_refused_before_read(self):
        with patch.object(hybrid, "memory_available", return_value=(0, 100*1024**3)), \
                patch.object(pq, "read_table") as read:
            with self.assertRaises(hybrid.HybridRefused):
                hybrid.read_gpu(self.engine, self.path, self.columns, {})
        read.assert_not_called()

    def test_host_memory_also_guarded(self):
        with patch.object(hybrid, "memory_available", return_value=(100*1024**3, 0)):
            with self.assertRaises(hybrid.HybridRefused):
                hybrid.read_gpu(self.engine, self.path, self.columns, {})

    def test_unsupported_column_refused_before_read(self):
        with patch.object(pq, "read_table") as read:
            with self.assertRaises(hybrid.HybridRefused):
                hybrid.read_gpu(self.engine, self.path, None, {})
        read.assert_not_called()

    def test_input_change_refused(self):
        with patch.object(hybrid, "identity", side_effect=[[self.path, 1, 1], [self.path, 2, 2]]):
            with self.assertRaises(hybrid.HybridRefused):
                hybrid.read_gpu(self.engine, self.path, self.columns, {})

    def test_conversion_failure_reported(self):
        engine = SimpleNamespace(is_gpu=True, name="cudf", mod=SimpleNamespace(
            DataFrame=SimpleNamespace(from_arrow=lambda table: (_ for _ in ()).throw(RuntimeError("conversion failed")))))
        trace = {}
        with self.assertRaises(hybrid.HybridRefused):
            hybrid.read_gpu(engine, self.path, self.columns, trace)
        self.assertEqual(trace["read_count"], 1)
        self.assertIn("conversion failed", trace["failure"])

    def test_selects_hybrid_only_when_all_margins_pass(self):
        self.save()
        self.assertEqual(self.select(), "cpu_gpu")

    def test_conversion_overhead_can_select_native(self):
        self.save(self.measures(4.))
        self.assertEqual(self.select(), "native")

    def test_native_calibration_without_hybrid_candidate(self):
        measures = self.measures()
        del measures["cpu_gpu"]
        self.save(measures)
        self.assertEqual(self.select(), "native")

    def test_unsupported_hybrid_schema_keeps_valid_native_candidate(self):
        self.columns = ["revenue", "ignored"]
        self.save()
        self.assertEqual(self.select(), "native")

    def test_cpu_can_win_even_when_cpu_read_is_faster(self):
        measurements = self.measures()
        measurements["cpu"] = [{"seconds": 1., "read_seconds": .2, "compute_seconds": .5}]*2
        self.save(measurements)
        self.assertEqual(self.select(), "cpu")

    def test_missing_calibration_does_not_guess(self):
        self.assertIsNone(self.select())

    def test_columns_query_and_context_must_match(self):
        self.save()
        self.assertIsNone(self.select(columns=["revenue"]))
        self.assertIsNone(self.select(workflow=[{"op": "outliers"}]))
        self.assertIsNone(self.select(context="oneshot_cold"))

    def test_file_mutation_invalidates_calibration(self):
        self.save()
        os.utime(self.path, ns=(time.time_ns(), time.time_ns()+1_000_000))
        self.assertIsNone(self.select())

    def test_expired_and_nonfinite_profiles_ignored(self):
        self.save()
        with patch.object(hybrid.time, "time", return_value=time.time()+hybrid.MAX_AGE_SECONDS+10):
            self.assertIsNone(self.select())
        with open(self.profile, encoding="utf-8") as source:
            profile = json.load(source)
        entry = next(iter(profile["entries"].values()))
        entry["measurements"]["cpu_gpu"][0]["seconds"] = float("nan")
        with open(self.profile, "w", encoding="utf-8") as target:
            json.dump(profile, target)
        self.assertIsNone(self.select())

    def test_different_host_profile_ignored(self):
        self.save()
        with patch.object(hybrid, "fingerprint", return_value={"host": "another-host"}):
            self.assertIsNone(self.select())

    def test_forced_cpu_cannot_be_overridden(self):
        output = io.StringIO()
        with patch.object(hybrid, "choose") as select, contextlib.redirect_stdout(output):
            code = ga.main(["--input", self.path, "--op", "summary", "--columns", "revenue", "--force-cpu"])
        self.assertEqual(code, 0)
        select.assert_not_called()
        self.assertEqual(json.loads(output.getvalue())["loading"]["actual"], "cpu")

    def test_conflicting_force_flags_rejected(self):
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(ga.main(["--input", self.path, "--force-cpu", "--load-backend", "cpu_gpu"]), 2)


if __name__ == "__main__":
    unittest.main()
