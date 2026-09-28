"""CPU-only contract tests for the GPU optimization paths."""

import json
import os
import tempfile
import time
import unittest
from unittest import mock

import pandas as pd

import cost_model
import gpu_analytics as ga
import gpu_session as gs
import parquet_cache


class OptimizationTests(unittest.TestCase):
    def test_clean_quartiles_do_not_allocate_another_dropna(self):
        clean = pd.Series([1.0, 2.0, 3.0, 4.0])
        with mock.patch.object(clean, "dropna", side_effect=AssertionError("duplicate cleaning")):
            self.assertEqual(ga._quartiles(clean, False, already_clean=True), (1.75, 2.5, 3.25))

    def test_binary_formats_do_not_use_csv_newline_estimator(self):
        with tempfile.TemporaryDirectory() as root:
            path = os.path.join(root, "data.json")
            with open(path, "w", encoding="utf-8") as stream:
                stream.write('{"x":1}\n' * 100)
            self.assertIsNone(ga._estimate_rows(path))

    def test_json_projection_does_not_pass_csv_only_arguments(self):
        with tempfile.TemporaryDirectory() as root:
            source = pd.DataFrame({"a": [1, 2], "b": [3, 4]})
            for ext, lines in ((".json", False), (".jsonl", True)):
                path = os.path.join(root, "data" + ext)
                source.to_json(path, orient="records", lines=lines)
                frame = ga._read_table(pd, path, usecols=["a"], nrows=1)
                self.assertEqual(frame.to_dict("records"), [{"a": 1}])

    def test_operation_column_projection_is_conservative(self):
        parser = ga.build_parser()
        group = parser.parse_args(["--input", "unused.csv", "--op", "groupby",
                                   "--by", "region", "--agg", "revenue:sum,mean|units:max"])
        self.assertEqual(ga.required_read_columns("groupby", group),
                         ["region", "revenue", "units"])
        group.no_auto_usecols = True
        self.assertIsNone(ga.required_read_columns("groupby", group))
        group.usecols = "region,revenue"
        self.assertEqual(ga.required_read_columns("groupby", group),
                         ["region", "revenue"])
        for operation in ("summary", "corr", "outliers"):
            args = parser.parse_args(["--input", "unused.csv", "--op", operation,
                                      "--columns", "revenue,cost"])
            self.assertEqual(ga.required_read_columns(operation, args),
                             ["revenue", "cost"])
        for operation in ("profile", "auto"):
            args = parser.parse_args(["--input", "unused.csv", "--op", operation,
                                      "--columns", "revenue"])
            self.assertIsNone(ga.required_read_columns(operation, args))

    def test_summary_values_and_null_count(self):
        result = ga._describe_stats(pd.Series([1.0, 2.0, 3.0, None, 20.0]), False)
        self.assertEqual(result["count"], 4)
        self.assertEqual(result["mean"], 6.5)
        self.assertEqual(result["min"], 1.0)
        self.assertEqual(result["max"], 20.0)
        self.assertEqual(result["median"], 2.5)
        frame = pd.DataFrame({"value": [1.0, 2.0, 3.0, None, 20.0]})
        args = ga.build_parser().parse_args(["--input", "unused.csv", "--op", "summary"])
        self.assertEqual(ga.op_summary(frame, ga.Engine("pandas", pd), args)
                         ["stats"]["value"]["nulls"], 1)

    def test_outlier_examples_only_materialize_requested_columns(self):
        frame = pd.DataFrame({"value": [1.0, 1.0, 1.0, 1.0, 100.0],
                              "unused": ["a", "b", "c", "d", "e"]})
        args = ga.build_parser().parse_args(["--input", "unused.csv", "--op", "outliers",
                                             "--columns", "value"])
        result = ga.op_outliers(frame, ga.Engine("pandas", pd), args)
        self.assertEqual(result["results"]["value"]["count"], 1)
        self.assertEqual(result["results"]["value"]["examples"], [{"value": 100.0}])

    def test_auto_reuses_summary_quartiles(self):
        frame = pd.DataFrame({"a": [1, 2, 3, 20], "b": [4, 5, 6, 7]})
        args = ga.build_parser().parse_args(["--input", "unused.csv", "--op", "auto"])
        original = ga._quartiles
        with mock.patch.object(ga, "_quartiles", wraps=original) as quartiles:
            result, _, _, _, _ = ga.execute(
                ga.Engine("pandas", pd), lambda _eng: frame, "auto", args)
        self.assertEqual(quartiles.call_count, 2)
        self.assertEqual(result["outliers"]["results"]["a"]["q1"],
                         result["summary"]["stats"]["a"]["q1"])

    def test_calibrated_routing_and_uncalibrated_fallback(self):
        with tempfile.TemporaryDirectory() as root:
            source = os.path.join(root, "data.csv")
            with open(source, "wb") as fh:
                fh.write(b"x\n" + b"1\n" * 49)
            samples = os.path.join(root, "observations.jsonl")
            for size in (80, 90, 100, 110):
                cost_model.record(samples, op="summary", source=source,
                                  backend="pandas", size=size, seconds=1 + size / 100)
                cost_model.record(samples, op="summary", source=source,
                                  backend="cudf", size=size, seconds=0.4 + size / 200)
            details = {}
            use_gpu, reason = ga.pick_engine_for(
                source, "summary", details=details, calibration_file=samples)
            self.assertTrue(use_gpu)
            self.assertIsNone(reason)
            self.assertEqual(details["policy"], "calibrated_cost_model")
            self.assertGreater(details["estimated_seconds"]["pandas"],
                               details["estimated_seconds"]["cudf"])
            other = {}
            ga.pick_engine_for(source, "corr", details=other, calibration_file=samples)
            self.assertEqual(other["policy"], "measured_file_size_crossover")

    def test_parquet_cache_hit_and_invalidation(self):
        with tempfile.TemporaryDirectory() as root:
            source = os.path.join(root, "data.csv")
            cache = os.path.join(root, "cache")
            with open(source, "w", encoding="utf-8") as fh:
                fh.write("x\n1\n")

            class Frame:
                def to_parquet(self, target, index=False):
                    with open(target, "w", encoding="utf-8") as fh:
                        fh.write("parquet-shaped test payload")

            class Engine:
                def __init__(self):
                    self.read_paths = []

                def read(self, path, **_kwargs):
                    self.read_paths.append(path)
                    return Frame()

            engine = Engine()
            _, first = parquet_cache.read(engine, source, cache)
            _, second = parquet_cache.read(engine, source, cache)
            self.assertEqual((first["status"], second["status"]), ("built", "hit"))
            self.assertEqual(engine.read_paths[0], source)
            self.assertTrue(engine.read_paths[1].endswith(".parquet"))
            with open(source, "a", encoding="utf-8") as fh:
                fh.write("2\n")
            _, third = parquet_cache.read(engine, source, cache)
            self.assertEqual(third["status"], "built")

    def test_parquet_cache_projects_different_columns_after_one_conversion(self):
        with tempfile.TemporaryDirectory() as root:
            source = os.path.join(root, "data.csv")
            cache = os.path.join(root, "cache")
            with open(source, "w", encoding="utf-8") as fh:
                fh.write("x,y\n1,2\n")

            class Frame:
                def __init__(self, columns):
                    self.columns = columns

                def __getitem__(self, names):
                    return Frame(names)

                def to_parquet(self, target, index=False):
                    with open(target, "w", encoding="utf-8") as fh:
                        fh.write("cached")

            class Engine:
                name = "pandas"
                version = "test"

                def __init__(self):
                    self.reads = []

                def read(self, path, usecols=None, **_kwargs):
                    self.reads.append((path, usecols))
                    return Frame(list(usecols or ["x", "y"]))

            engine = Engine()
            first, built = parquet_cache.read(engine, source, cache, usecols=["x"])
            second, hit = parquet_cache.read(engine, source, cache, usecols=["y"])
            self.assertEqual((built["status"], hit["status"]), ("built", "hit"))
            self.assertEqual((first.columns, second.columns), (["x"], ["y"]))
            self.assertEqual(engine.reads[0], (source, None))
            self.assertTrue(engine.reads[1][0].endswith(".parquet"))
            self.assertEqual(engine.reads[1][1], ["y"])

    def test_cross_question_warm_cache_and_stale_source(self):
        with tempfile.TemporaryDirectory() as root:
            source = os.path.join(root, "data.csv")
            with open(source, "w", encoding="utf-8") as fh:
                fh.write("a\n1\n2\n")
            gpu = ga.Engine("cudf", pd)
            cpu = ga.Engine("pandas", pd)
            gs.SESSIONS.clear()
            gs.WARM_CACHE.clear()
            with mock.patch.object(gs.GA, "detect_engine", side_effect=lambda force_cpu=False: cpu if force_cpu else gpu), \
                    mock.patch.object(gs, "_free_gpu_gb", return_value=None):
                opened = gs.do_open({"path": source, "force_gpu": True})
                self.assertTrue(opened["ok"], opened)
                closed = gs.do_close({"sid": opened["session_id"], "retain": True})
                self.assertTrue(closed["cached"])
                reused = gs.do_open({"path": source, "force_gpu": True})
                self.assertTrue(reused["cache_hit"])
                self.assertNotEqual(reused["session_id"], opened["session_id"])
                gs.do_close({"sid": reused["session_id"], "retain": True})
                with open(source, "a", encoding="utf-8") as fh:
                    fh.write("3\n")
                fresh = gs.do_open({"path": source, "force_gpu": True})
                self.assertFalse(fresh.get("cache_hit", False))
                self.assertEqual(fresh["rows"], 3)
            gs.do_close({"sid": "all"})


if __name__ == "__main__":
    unittest.main()
