"""Bounded Arrow CPU loading and exact-query, opt-in three-path calibration.

Profiles are local runtime artifacts, not model guesses or hardware-independent rules.
No imports of CUDA/cuDF are needed to inspect a profile or a Parquet footer.
"""
from __future__ import annotations

import hashlib
import importlib.metadata
import json
import math
import os
import platform
import statistics
import tempfile
import time

PATHS = ("cpu", "native", "cpu_gpu")
MAX_PROFILE_BYTES = 8 * 1024**2
MAX_AGE_SECONDS = 24 * 3600


class HybridRefused(RuntimeError):
    pass


class HybridMemoryRefused(HybridRefused):
    pass


class HybridInputChanged(HybridRefused):
    pass


def identity(path):
    stat = os.stat(path)
    return [os.path.realpath(path), stat.st_size, stat.st_mtime_ns]


def fingerprint():
    packages = {}
    for package in ("pyarrow", "pandas", "cudf", "cudf-cu12", "cupy", "cupy-cuda12x"):
        try:
            packages[package] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            pass
    devices = []
    try:
        for device in os.scandir("/proc/driver/nvidia/gpus"):
            with open(os.path.join(device.path, "information"), encoding="utf-8") as source:
                devices.append([line.strip() for line in source
                                if line.startswith(("Model:", "GPU UUID:"))])
    except OSError:
        pass
    affinity = sorted(os.sched_getaffinity(0)) if hasattr(os, "sched_getaffinity") else os.cpu_count()
    return {"host": platform.node(), "arch": platform.machine(), "devices": sorted(devices),
            "python": platform.python_version(), "packages": packages,
            "cpu_affinity": affinity, "omp_threads": os.environ.get("OMP_NUM_THREADS"),
            "arrow_threads_env": os.environ.get("ARROW_NUM_THREADS"),
            "visible_devices": os.environ.get("CUDA_VISIBLE_DEVICES", "default")}


def signature(path, columns, workflow, context):
    value = {"file": identity(path), "columns": sorted(columns) if columns else None,
             "workflow": workflow, "context": context}
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def single_workflow(args):
    # Include every option which can change the work or its returned result.
    return [{key: getattr(args, key, None) for key in (
        "op", "select", "by", "agg", "method", "iqr_k", "top_k", "max_columns",
        "auto_columns", "preview_n", "limit", "usecols", "no_auto_usecols")}]


def read_profile(path):
    if not path or os.path.getsize(path) > MAX_PROFILE_BYTES:
        raise ValueError("missing or oversized hybrid profile")
    with open(path, encoding="utf-8") as stream:
        profile = json.load(stream)
    if profile.get("version") != 1 or profile.get("fingerprint") != fingerprint():
        raise ValueError("hybrid profile hardware/software identity does not match")
    if not isinstance(profile.get("entries"), dict) or len(profile["entries"]) > 1000:
        raise ValueError("invalid hybrid profile entries")
    return profile


def choose(profile_path, path, columns, workflow, context):
    """None preserves native routing. Hybrid must beat BOTH complete alternatives."""
    note = {"policy": "matched_hybrid_calibration", "context": context,
            "selected": None, "reason": "no matching, verified calibration"}
    if os.path.splitext(path)[1].lower() not in (".parquet", ".pq"):
        return None, {**note, "reason": "hybrid supports Parquet only"}
    try:
        profile = read_profile(profile_path)
        entry = profile["entries"][signature(path, columns, workflow, context)]
        if (not isinstance(entry["created"], (int, float)) or isinstance(entry["created"], bool)
                or not math.isfinite(entry["created"])):
            raise ValueError("invalid calibration timestamp")
        age = time.time() - entry["created"]
        if age < 0 or age > MAX_AGE_SECONDS or entry.get("verified_equal") is not True:
            raise ValueError("expired or unverified calibration")
        estimates = {}
        for backend in PATHS:
            runs = entry["measurements"][backend]
            if not isinstance(runs, list) or not 2 <= len(runs) <= 20:
                raise ValueError("calibration requires 2-20 runs per path")
            for run in runs:
                for key in ("seconds", "read_seconds", "compute_seconds"):
                    if (not isinstance(run[key], (float, int)) or isinstance(run[key], bool)
                            or not math.isfinite(run[key]) or run[key] < 0):
                        raise ValueError("non-finite or negative calibration")
                if run["seconds"] <= 0:
                    raise ValueError("zero wall time")
            estimates[backend] = {key: statistics.median(run[key] for run in runs)
                                  for key in ("seconds", "read_seconds", "compute_seconds")}
        cpu, gpu, hybrid = (estimates[backend] for backend in PATHS)
        hybrid_wins = (hybrid["read_seconds"] < .95 * gpu["read_seconds"]
                       and hybrid["compute_seconds"] < .90 * cpu["compute_seconds"]
                       and hybrid["seconds"] < .90 * min(cpu["seconds"], gpu["seconds"]))
        selected = "cpu_gpu" if hybrid_wins else (
            "native" if gpu["seconds"] < .90 * cpu["seconds"] else "cpu")
        return selected, {**note, "selected": selected, "estimated": estimates,
                          "reason": "hybrid clears read, compute and full-cost margins" if hybrid_wins else
                          "hybrid did not beat both full-cost alternatives; choose CPU/native",
                          "calibration_age_seconds": round(age, 1)}
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return None, note


def save_measurement(profile_path, path, columns, workflow, context, measurements):
    """Only the explicit parity-checking benchmark calls this function."""
    try:
        profile = read_profile(profile_path)
    except (OSError, ValueError, TypeError, AttributeError):
        profile = {"version": 1, "fingerprint": fingerprint(), "entries": {}}
    profile["entries"][signature(path, columns, workflow, context)] = {
        "created": time.time(), "verified_equal": True, "measurements": measurements}
    directory = os.path.dirname(os.path.abspath(profile_path))
    os.makedirs(directory, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".hybrid-", suffix=".json", dir=directory)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(profile, stream, allow_nan=False)
        if os.path.getsize(temporary) > MAX_PROFILE_BYTES or len(profile["entries"]) > 1000:
            raise ValueError("hybrid calibration budget exceeded")
        os.replace(temporary, profile_path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def inspect_parquet(path, columns):
    import pyarrow as pa
    import pyarrow.parquet as pq
    metadata = pq.read_metadata(path)
    schema = metadata.schema.to_arrow_schema()
    fields = [schema.field(name) for name in columns] if columns else list(schema)
    if not fields or any(not (pa.types.is_integer(field.type) or pa.types.is_floating(field.type)
                             or pa.types.is_boolean(field.type)) for field in fields):
        raise HybridRefused("initial hybrid loader supports flat numeric/bool Parquet columns only")
    # Includes validity masks, simultaneous Arrow/cuDF copies and compute temporaries.
    frame_bytes = metadata.num_rows * sum(max(1, field.type.bit_width // 8) + 1 for field in fields)
    required = max(frame_bytes * 6 + 4 * 1024**3, os.path.getsize(path) * 3 + 4 * 1024**3)
    return metadata.num_rows, required


def memory_available():
    import cupy
    if os.path.isfile("/proc/meminfo"):
        with open("/proc/meminfo", encoding="ascii") as source:
            entries = dict(line.split(":", 1) for line in source)
        available = int(entries["MemAvailable"].split()[0]) * 1024
    else:
        import psutil
        available = int(psutil.virtual_memory().available)
    return int(cupy.cuda.runtime.memGetInfo()[0]), available


def read_gpu(engine, path, columns, trace):
    """A single full CPU read, a single conversion, then release Arrow immediately."""
    trace.update({"requested": "cpu_gpu", "load_engine": "pyarrow_cpu",
                  "compute_engine": engine.name, "read_count": 0, "conversion_count": 0})
    table = frame = None
    try:
        import pyarrow as pa
        import pyarrow.parquet as pq
        if not engine.is_gpu or os.path.splitext(path)[1].lower() not in (".parquet", ".pq"):
            raise HybridRefused("CPU-to-GPU loading requires cuDF and a Parquet file")
        before = identity(path)
        rows, required = inspect_parquet(path, columns)
        device_free, host_available = memory_available()
        trace["admission"] = {"required_bytes": required, "device_free_bytes": device_free,
                              "host_available_bytes": host_available}
        if min(device_free, host_available) < required:
            raise HybridMemoryRefused("hybrid memory budget refused before CPU read")
        start = time.perf_counter()
        table = pq.read_table(path, columns=list(columns) if columns else None, use_threads=True)
        trace.update(cpu_read_seconds=time.perf_counter() - start, read_count=1,
                     arrow_bytes=table.nbytes, cpu_threads=pa.cpu_count())
        if table.num_rows != rows or identity(path) != before:
            raise HybridInputChanged("Parquet changed during CPU read")
        start = time.perf_counter()
        frame = engine.mod.DataFrame.from_arrow(table)
        # Synchronize conversion itself; do not hide it in later compute timing.
        import cupy
        cupy.cuda.runtime.deviceSynchronize()
        trace.update(conversion_seconds=time.perf_counter() - start, conversion_count=1)
        if len(frame) != rows or identity(path) != before:
            raise HybridInputChanged("conversion row count or input identity changed")
        return frame
    except Exception as exc:
        frame = None
        trace["failure"] = f"{type(exc).__name__}: {exc}"
        if isinstance(exc, (HybridMemoryRefused, HybridInputChanged)):
            raise
        raise HybridRefused(trace["failure"]) from exc
    finally:
        table = None
