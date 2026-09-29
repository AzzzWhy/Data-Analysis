import hashlib
import tempfile
from pathlib import Path
import unittest

from dataset_inventory import collect


class InventoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.data = b"value\n1\n2\n"
        (self.root / "sample.csv").write_bytes(self.data)

    def test_relative_paths_and_full_hash_without_row_guess(self):
        result = collect(self.root, ["sample.csv"])
        entry = result["files"][0]
        self.assertEqual(entry["path"], "sample.csv")
        self.assertEqual(entry["sha256"], hashlib.sha256(self.data).hexdigest())
        self.assertIsNone(entry["rows"])
        self.assertEqual(result["total_bytes"], len(self.data))
        self.assertNotIn(str(self.root), str(result))

    def test_hash_limit_and_duplicate_inputs(self):
        result = collect(self.root, ["sample.csv", "sample.csv"], 0)
        self.assertEqual(result["file_count"], 1)
        self.assertEqual(result["files"][0]["hash_status"], "skipped_size_limit")
        self.assertIsNone(result["files"][0]["sha256"])

    def test_missing_path_is_explicit(self):
        self.assertEqual(collect(self.root, ["absent"]) ["missing_requested_paths"], ["absent"])

    def test_root_escape_rejected(self):
        with self.assertRaises(ValueError):
            collect(self.root, ["../outside"])


if __name__ == "__main__":
    unittest.main()
