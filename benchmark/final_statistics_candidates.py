"""Compatibility import for isolated frequency-statistics candidate probes."""
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills/cudf-analytics/scripts"))
from exact_statistics import frequency_describe, weighted_statistics
