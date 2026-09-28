"""Small, engine-isolated scalar cache owned by a file-validated resident session.

File validation remains the session owner's responsibility. This module deliberately
does not cache result tables, masks, model answers or dataframe references.
"""
from dataclasses import dataclass, field
from typing import Any


@dataclass
class ExactStatisticsCache:
    _entries: dict = field(default_factory=dict)

    def __len__(self):
        return len(self._entries)

    def clear(self):
        self._entries.clear()

    def for_engine(self, engine: Any) -> dict:
        # Include version: even a same-name engine upgrade must not mix statistics.
        identity = (engine.name, engine.version)
        return {column: dict(values) for (name, version, column), values in
                self._entries.items() if (name, version) == identity}

    def record(self, operation: str, payload: dict, engine: Any, limit: int):
        if limit <= 0:
            self.clear()
            return
        if operation == "summary":
            stats = payload.get("stats", {})
        elif operation == "auto":
            stats = payload.get("summary", {}).get("stats", {})
        elif operation == "outliers":
            stats = payload.get("results", {})
        else:
            stats = {}
        for column, values in stats.items():
            if "q1" not in values or "q3" not in values:
                continue
            entry = {"q1": values["q1"], "q3": values["q3"]}
            # Outlier count is NOT non-null count. Never substitute it here.
            count = values.get("valid_count" if operation == "outliers" else "count")
            if isinstance(count, int) and not isinstance(count, bool) and count >= 0:
                entry["valid_count"] = count
            key = (engine.name, engine.version, column)
            self._entries.pop(key, None)
            self._entries[key] = entry
        while len(self._entries) > limit:
            self._entries.pop(next(iter(self._entries)))
