"""Exact example selection contracts; no GPU or external data required."""
import unittest
import os
from unittest.mock import patch

import numpy as np
import pandas as pd
import gpu_analytics as ga


class ExampleTests(unittest.TestCase):
    def check(self, frame, mask, limit):
        columns = list(frame.columns)
        expected = frame[mask].head(limit)
        actual = ga._outlier_examples(frame, mask, columns, int(mask.sum()), limit)
        pd.testing.assert_frame_equal(actual, expected)

    def test_dense_and_empty_matches(self):
        frame = pd.DataFrame({"value": range(10000), "other": range(10000)})
        self.check(frame, frame.value >= 0, 3)
        self.check(frame, frame.value < 0, 3)

    def test_sparse_last_row_and_fewer_than_requested(self):
        frame = pd.DataFrame({"value": range(10000)})
        self.check(frame, frame.value >= 9999, 3)
        self.check(frame, frame.value.isin([0, 5000, 9999]), 8)

    def test_duplicate_nondefault_index_and_nullable_mask(self):
        frame = pd.DataFrame({"value": range(200000)}, index=["duplicate"] * 200000)
        mask = pd.Series(pd.array([None, True, False, True, False] * 40000, dtype="boolean"),
                         index=frame.index)
        self.check(frame, mask, 5)

    def test_exact_original_order_and_projection(self):
        random = np.random.default_rng(9876)
        frame = pd.DataFrame({"a": random.normal(size=200000), "b": range(200000)},
                             index=random.permutation(200000))
        for density in (0.0001, 0.01, 0.5, 1.0):
            mask = pd.Series(random.random(200000) < density, index=frame.index)
            for limit in (1, 3, 100):
                self.check(frame, mask, limit)
        actual = ga._outlier_examples(frame, frame.a > 0, ["a"], int((frame.a > 0).sum()), 3)
        pd.testing.assert_frame_equal(actual, frame[["a"]][frame.a > 0].head(3))

    def test_dense_result_does_not_filter_entire_frame(self):
        frame = pd.DataFrame({"value": range(200000)})
        mask = frame.value >= 0
        sizes = []
        original = pd.DataFrame.__getitem__
        def record(obj, key):
            if isinstance(key, pd.Series):
                sizes.append(len(obj))
            return original(obj, key)
        with patch.object(pd.DataFrame, "__getitem__", record):
            self.check(frame, mask, 3)
        # The first filter is the independent reference, the second the candidate.
        self.assertEqual(sizes, [200000, 1024])

    def test_switch_and_small_input_keep_legacy_path(self):
        frame = pd.DataFrame({"value": range(200000)})
        mask = frame.value >= 0
        with patch.dict(os.environ, {"GPU_ANALYSIS_BOUNDED_OUTLIER_EXAMPLES": "0"}):
            actual = ga._outlier_examples(frame, mask, ["value"], len(frame), 3)
            pd.testing.assert_frame_equal(actual, frame[mask].head(3))
            import hybrid_execution as hybrid
            original = hybrid.fingerprint()
        with patch.dict(os.environ, {"GPU_ANALYSIS_BOUNDED_OUTLIER_EXAMPLES": "1"}):
            self.assertNotEqual(original, hybrid.fingerprint())
        small = frame.head(100000)
        sizes = []
        original_getitem = pd.DataFrame.__getitem__
        def record(obj, key):
            if isinstance(key, pd.Series):
                sizes.append(len(obj))
            return original_getitem(obj, key)
        with patch.object(pd.DataFrame, "__getitem__", record):
            ga._outlier_examples(small, mask.head(len(small)), ["value"], len(small), 3)
        self.assertEqual(sizes, [100000])

    def test_large_sparse_match_at_end(self):
        frame = pd.DataFrame({"value": range(200000)})
        self.check(frame, frame.value >= 199900, 3)


@unittest.skipUnless(os.environ.get("GPU_ANALYSIS_TEST_GPU") == "1", "explicit live GPU opt-in")
class GPUExampleTests(unittest.TestCase):
    def test_gpu_order_null_mask_and_sparse_matches(self):
        import cudf
        frame = cudf.DataFrame({"value": range(200000), "other": range(200000)})
        frame.index = cudf.Index(["duplicate"] * len(frame))
        masks = [frame.value >= 0, frame.value < 0, frame.value >= 199999,
                 frame.value >= 199900, frame.value.isin([0, 100000, 199999]),
                 cudf.Series([None, True, False, True, False] * 40000, index=frame.index)]
        for mask in masks:
            for limit in (1, 3, 100):
                expected = frame[["value"]][mask].head(limit).to_pandas()
                actual = ga._outlier_examples(frame, mask, ["value"], int(mask.sum()), limit).to_pandas()
                pd.testing.assert_frame_equal(actual, expected)


if __name__ == "__main__":
    unittest.main()
