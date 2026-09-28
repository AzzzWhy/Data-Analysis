"""A bounded cross-process compute slot shared by Agent and batch workers.

The lock covers one GPU operation, not the lifetime of a resident frame. This
lets a warm conversation stay open while scheduled work makes progress between
its questions. Existing memory-admission checks still govern each frame.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
import getpass
import os
from pathlib import Path
import tempfile
import time


_HELD_PATHS = ContextVar("gpu_compute_slots", default=frozenset())


def _lock_path() -> Path:
    configured = os.environ.get("GPU_ANALYSIS_GPU_SLOT_FILE")
    if configured:
        path = Path(configured).expanduser()
        if not path.is_absolute():
            raise ValueError("GPU_ANALYSIS_GPU_SLOT_FILE must be an absolute path")
        return path
    user_key = str(os.getuid()) if hasattr(os, "getuid") else getpass.getuser()
    return Path(tempfile.gettempdir()) / f"gpu-data-analysis-{user_key}" / "gpu-compute.lock"


@contextmanager
def gpu_slot(timeout_seconds: float | None = None):
    """Wait for the host-wide GPU compute slot and release it on every exit."""
    if timeout_seconds is None:
        timeout_seconds = float(os.environ.get("GPU_ANALYSIS_GPU_SLOT_TIMEOUT", "300"))
    if not 0 < timeout_seconds <= 1800:
        raise ValueError("GPU slot timeout must be in (0, 1800] seconds")
    path = _lock_path()
    key = str(path.resolve())
    held = _HELD_PATHS.get()
    if key in held:
        yield 0.0
        return
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name != "nt" and not os.environ.get("GPU_ANALYSIS_GPU_SLOT_FILE"):
        parent_stat = path.parent.stat()
        if parent_stat.st_uid != os.getuid() or parent_stat.st_mode & 0o077:
            raise PermissionError("GPU slot directory must be private to the current user")
    started = time.monotonic()
    with path.open("a+b") as file:
        file.seek(0, os.SEEK_END)
        if file.tell() == 0:
            file.write(b"\0")
            file.flush()
        acquired = False
        try:
            while not acquired:
                try:
                    if os.name == "nt":
                        import msvcrt

                        file.seek(0)
                        msvcrt.locking(file.fileno(), msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl

                        fcntl.flock(file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    acquired = True
                except (BlockingIOError, OSError):
                    if time.monotonic() - started >= timeout_seconds:
                        raise TimeoutError(f"GPU compute slot busy for {timeout_seconds:g} seconds")
                    time.sleep(0.05)
            token = _HELD_PATHS.set(held | {key})
            try:
                yield time.monotonic() - started
            finally:
                _HELD_PATHS.reset(token)
        finally:
            if acquired:
                if os.name == "nt":
                    import msvcrt

                    file.seek(0)
                    msvcrt.locking(file.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(file.fileno(), fcntl.LOCK_UN)
