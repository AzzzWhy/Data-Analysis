"""Reader, capability and exact Pearson contracts; --gpu verifies real cuDF."""
import contextlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import numpy as np
import pandas as pd
import gpu_analytics as GA
import hybrid_execution as H

GPU = "--gpu" in sys.argv
if GPU:
    sys.argv.remove("--gpu")


class DiversityContracts(unittest.TestCase):
    def test_pairwise_finite_semantics(self):
        cases = [
            pd.DataFrame({"a": [1, 2, None, 4, 5], "b": [2, None, 3, 5, 1],
                          "constant": [1]*5, "empty": [np.nan]*5}),
            pd.DataFrame({"a": [1, np.inf, 3, 4], "b": [4, 3, -np.inf, 2]}),
            pd.DataFrame({"a": [1e12+1, 1e12+2, 1e12+3],
                          "b": [1e12+3, 1e12+2, 1e12+1]}),
            pd.DataFrame({"a": [np.nan, 1], "b": [1, np.nan]}),
            pd.DataFrame({"a": [], "b": []}),
        ]
        for frame in cases:
            with np.errstate(invalid="ignore", divide="ignore"):
                actual = GA.pairwise_pearson(frame, np)
            np.testing.assert_allclose(actual, frame.corr().to_numpy(),
                                       atol=1e-9, rtol=1e-9, equal_nan=True)
            if GPU:
                import cudf
                import cupy as cp
                actual = GA.pairwise_pearson(cudf.from_pandas(frame), cp)
                np.testing.assert_allclose(cp.asnumpy(actual), frame.corr().to_numpy(),
                                           atol=1e-9, rtol=1e-9, equal_nan=True)

    def test_csv_semicolon_quoted_bom_and_tab(self):
        with tempfile.TemporaryDirectory() as tmp:
            for sep in (";", ",", "\t", "|"):
                path = Path(tmp) / "data.csv"
                path.write_text(f'label{sep}value\n"with;comma,inside"{sep}3\nplain{sep}5\n',
                                encoding="utf-8-sig")
                self.assertEqual(GA.csv_separator(str(path)), sep)
                got = GA.Engine("pandas", pd).read(str(path), usecols=["value"])
                self.assertEqual(got.value.tolist(), [3, 5])
                if GPU:
                    import cudf
                    got = GA.Engine("cudf", cudf).read(str(path), usecols=["value"])
                    self.assertEqual(got.to_pandas().value.tolist(), [3, 5])

    def test_preflight_no_cuda_or_read(self):
        self.assertIn("CPU-only", H.preflight("none.parquet", None,
                       [{"op": "corr", "method": "spearman"}], "native"))
        self.assertIn("Parquet", H.preflight("none.csv", None, [], "cpu_gpu"))
        self.assertIn("CPU-only", H.preflight("none.json", None, [], "native"))
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "strings.parquet"
            pd.DataFrame({"key": ["a", "b"], "value": [1, 2]}).to_parquet(path)
            self.assertIsNone(H.preflight(str(path), ["value"], [], "cpu_gpu"))
            self.assertIn("numeric", H.preflight(str(path), None, [], "cpu_gpu"))

    def test_known_cpu_only_routes_before_gpu_detection(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "d.csv"
            path.write_text("a;b\n1;2\n2;1\n")
            output = io.StringIO()
            with patch.object(GA, "detect_engine", return_value=GA.Engine("pandas", pd)) as detect:
                with contextlib.redirect_stdout(output):
                    rc = GA.main(["--input", str(path), "--op", "corr", "--method", "spearman", "--force-gpu"])
                self.assertEqual(rc, 0)
                self.assertTrue(detect.call_args.kwargs["force_cpu"])
            result = json.loads(output.getvalue())
            self.assertEqual(result["engine"], "pandas")
            self.assertIsNone(result["fallback_reason"])
            self.assertEqual(len(result["phase_timings"]["attempts"]), 1)

    @unittest.skipUnless(GPU, "real GPU bridge needs cuDF and openpyxl")
    def test_excel_real_bridge_and_admission(self):
        # Read a pre-existing regression fixture. No authored workbook is needed.
        fixture = os.environ.get("GDA_EXCEL_FIXTURE")
        if not fixture:
            self.fail("set GDA_EXCEL_FIXTURE to the existing public Online Retail workbook")
        import cudf
        eng = GA.Engine("cudf", cudf)
        got = eng.read(fixture, ["Quantity", "UnitPrice"], nrows=50)
        expected = pd.read_excel(fixture, usecols=["Quantity", "UnitPrice"], nrows=50)
        pd.testing.assert_frame_equal(got.to_pandas(), expected)
        self.assertEqual(eng.last_read["read_count"], 1)
        self.assertEqual(eng.last_read["conversion_count"], 1)
        with patch.object(H, "memory_available", return_value=(1, 1)):
            with patch.object(pd, "read_excel") as read:
                with self.assertRaises(H.HybridMemoryRefused):
                    eng.read(fixture, ["Quantity"])
                read.assert_not_called()


if __name__ == "__main__":
    unittest.main()
