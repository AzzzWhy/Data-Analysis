"""Independent NumPy/pandas contracts for experimental full-frequency stats."""
from pathlib import Path
import sys
import unittest
import os
from unittest.mock import patch
import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills/cudf-analytics/scripts"))
import gpu_analytics as ga
from final_statistics_candidates import frequency_describe, weighted_statistics
from fast_execution_benchmark import equal


class ExactFrequencyTests(unittest.TestCase):
    def compare(self, data):
        series = pd.Series(data)
        trace = {}
        actual = frequency_describe(series, False, ga._describe_stats_native, force=True, trace=trace)
        self.assertTrue(equal(actual, ga._describe_stats_native(series, False)), (actual, data))
        return trace

    def test_counts_quartiles_variance_and_nullable_values(self):
        for data in ([1], [1, 2], [1, 2, 3, 4], [2]*100, [None]*5,
                     [1, None, 2, 2, 10], [-20, -.1, 0, .1, 30]*101,
                     [1e12, 1e12+.01, 1e12+.02]):
            self.compare(data)

    def test_random_exact_values_not_rounded_to_bins(self):
        random = np.random.default_rng(9841)
        for cardinality in (3, 17, 200):
            choices = random.normal(size=cardinality)
            trace = self.compare(random.choice(choices, size=10007))
            self.assertEqual(trace["actual"], "full_frequency")

    def test_nonfinite_ill_conditioned_and_unsafe_integer_fallback(self):
        for data in ([1, float("inf")], [1, float("-inf")],
                     [1e12, 1e12+.01], [2**53, 2**53+1], [1e10, -1e10+1e-5]):
            self.assertNotIn("actual", self.compare(data))

    def test_invalid_weights_and_order(self):
        for values, counts in (([], []), ([1], [0]), ([2, 1], [1, 2]),
                               ([1, 1], [1, 1]), ([1, np.inf], [1, 1])):
            self.assertIsNone(weighted_statistics(np.asarray(values), np.asarray(counts), np))

    def test_float32_and_nullable_integer_contracts(self):
        for data in (pd.Series([.1, .2, 20], dtype="float32"),
                     pd.Series([1, None, 2, 10], dtype="Int64")):
            self.compare(data)

    def test_default_never_uses_candidate_for_small_data(self):
        trace = {}
        series = pd.Series([1, 1, 3])
        actual = frequency_describe(series, False, ga._describe_stats, trace=trace)
        self.assertEqual(trace, {})
        self.assertTrue(equal(actual, ga._describe_stats(series, False)))

    def test_runtime_switch_and_fingerprint_invalidation(self):
        import hybrid_execution as h
        with patch.dict(os.environ, {"GPU_ANALYSIS_FREQUENCY_STATS": "0"}):
            original = h.fingerprint()
            with patch.object(ga, "_describe_stats_native", return_value={"native": True}):
                self.assertEqual(ga._describe_stats(pd.Series([1]), False), {"native": True})
        with patch.dict(os.environ, {"GPU_ANALYSIS_FREQUENCY_STATS": "1"}):
            self.assertNotEqual(original, h.fingerprint())


if __name__ == "__main__":
    unittest.main()
