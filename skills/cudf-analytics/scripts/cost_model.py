"""Conservative per-operation routing learned from local cold-run measurements."""

from __future__ import annotations

import json
import math
import os
from typing import Optional


def record(path: Optional[str], *, op: str, source: str, backend: str,
           size: int, seconds: float) -> None:
    if not path or backend not in ("pandas", "cudf") or size <= 0 or seconds <= 0:
        return
    item = {"schema": 1, "op": op, "format": os.path.splitext(source)[1].lower(),
            "backend": backend, "bytes": size, "seconds": round(seconds, 6)}
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(item, separators=(",", ":")) + "\n")


def _fit(points: list[tuple[int, float]], size: int) -> Optional[dict]:
    # Three measurements at two or more sizes are the minimum for a slope and a sanity check.
    if len(points) < 3 or len({x for x, _ in points}) < 2:
        return None
    xs = [x / 1e9 for x, _ in points]
    ys = [y for _, y in points]
    mean_x, mean_y = sum(xs) / len(xs), sum(ys) / len(ys)
    denom = sum((x - mean_x) ** 2 for x in xs)
    if denom <= 0:
        return None
    slope = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys)) / denom
    intercept = mean_y - slope * mean_x
    if slope <= 0 or intercept < 0:
        return None
    fitted = [intercept + slope * x for x in xs]
    rel_error = sum(abs(a - b) / max(a, 1e-6) for a, b in zip(ys, fitted)) / len(ys)
    if rel_error > 0.25 or not 0.5 * min(xs) <= size / 1e9 <= 2 * max(xs):
        return None
    return {"seconds": round(intercept + slope * size / 1e9, 6),
            "samples": len(points), "mean_relative_error": round(rel_error, 3)}


def predict(path: Optional[str], *, op: str, source: str, size: int) -> Optional[dict]:
    if not path or not os.path.isfile(path):
        return None
    points: dict[str, list[tuple[int, float]]] = {"pandas": [], "cudf": []}
    ext = os.path.splitext(source)[1].lower()
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                try:
                    item = json.loads(line)
                    if (item.get("schema") == 1 and item.get("op") == op and
                            item.get("format") == ext and item.get("backend") in points):
                        x, y = int(item["bytes"]), float(item["seconds"])
                        if x > 0 and math.isfinite(y) and y > 0:
                            points[item["backend"]].append((x, y))
                except (ValueError, TypeError, KeyError):
                    continue
    except OSError:
        return None
    cpu = _fit(points["pandas"][-100:], size)
    gpu = _fit(points["cudf"][-100:], size)
    return {"pandas": cpu, "cudf": gpu} if cpu and gpu else None
