"""Optional, explicitly located CSV-to-Parquet cache for repeated full-file analyses."""

from __future__ import annotations

import hashlib
import os
import time
import uuid
from typing import Any, Optional, Tuple


def _identity(path: str) -> tuple:
    st = os.stat(path)
    return os.path.realpath(path), st.st_size, st.st_mtime_ns


def read(engine: Any, path: str, cache_dir: Optional[str], *, usecols=None, nrows=None
         ) -> Tuple[Any, dict]:
    """Return frame and trace. The first conversion is charged to this request."""
    if not cache_dir or os.path.splitext(path)[1].lower() != ".csv" or nrows:
        return engine.read(path, usecols=usecols, nrows=nrows), {"status": "disabled"}
    started = time.perf_counter()
    before = _identity(path)
    # Do not share a converted schema across different CSV parsers or engine versions.
    engine_id = (getattr(engine, "name", type(engine).__name__),
                 getattr(engine, "version", "unknown"))
    key = hashlib.sha256(repr((before, engine_id)).encode("utf-8")).hexdigest()[:32]
    try:
        os.makedirs(cache_dir, exist_ok=True)
    except OSError as exc:
        return engine.read(path, usecols=usecols), {"status": "unavailable",
                                   "reason": f"cache directory: {type(exc).__name__}: {exc}"}
    cached = os.path.join(cache_dir, f"gda-{key}.parquet")
    if os.path.isfile(cached):
        try:
            frame = engine.read(cached, usecols=usecols)
            if _identity(path) == before:
                return frame, {"status": "hit", "elapsed_seconds": round(
                    time.perf_counter() - started, 6), "cache_bytes": os.path.getsize(cached),
                    "projected_columns": list(usecols) if usecols else None}
        except Exception:
            # A partial/corrupt cache must never make the source unreadable.
            pass

    # Conversion must contain the full source schema.  Project only the returned
    # frame; subsequent requests can select *different* columns from one cache.
    frame = engine.read(path)
    if _identity(path) != before:
        raise RuntimeError("CSV changed during cache conversion; retry against the current file")
    tmp = os.path.join(cache_dir, f"gda-{key}-{uuid.uuid4().hex}.tmp.parquet")
    try:
        frame.to_parquet(tmp, index=False)
        if _identity(path) != before:
            raise RuntimeError("CSV changed during cache conversion; retry against the current file")
        os.replace(tmp, cached)
    except RuntimeError:
        raise
    except Exception as exc:
        return (frame[list(usecols)] if usecols else frame), {
                      "status": "unavailable", "reason": f"{type(exc).__name__}: {exc}",
                       "elapsed_seconds": round(time.perf_counter() - started, 6)}
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    return (frame[list(usecols)] if usecols else frame), {
        "status": "built", "elapsed_seconds": round(time.perf_counter() - started, 6),
        "cache_bytes": os.path.getsize(cached),
        "projected_columns": list(usecols) if usecols else None}
