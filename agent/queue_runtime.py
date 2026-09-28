"""Bounded reusable child processes; CPU and GPU pools remain disjoint."""
from concurrent.futures import ThreadPoolExecutor
import atexit
import json
import os
from pathlib import Path
import queue
import threading
import time


class AnalysisRunner:
    """Own a dedicated analysis worker; write reports in the queue parent.

    Do not use the Agent's global worker or borrow its active conversations.
    Removing a relay process preserves isolation of the actual GPU frame.
    """
    def __init__(self):
        self.proc = None

    def request(self, request):
        import skills
        if self.proc is None or self.proc.poll() is not None:
            self.proc = skills._worker_start(register_cleanup=False)
        self.proc.stdin.write(json.dumps(request) + "\n")
        self.proc.stdin.flush()
        try:
            line = self.proc.responses.get(timeout=max(.001, self.deadline - time.monotonic()))
        except queue.Empty:
            self.close()
            raise TimeoutError("analysis worker timed out and was stopped")
        if line is None:
            self.close()
            raise RuntimeError("analysis worker exited")
        return json.loads(line)

    def call(self, paths, output_root, timeout):
        import batch_runner
        self.deadline = time.monotonic() + timeout
        try:
            reply = batch_runner.run_group(paths, output_root, call_worker=self.request)
            if len(reply["jobs"]) != len(paths):
                raise RuntimeError("wrong number of job results")
            for job in reply["jobs"]:
                if job.get("ok"):
                    folder = Path(job["report_dir"]).resolve()
                    if not folder.is_relative_to(Path(output_root).resolve()) or not (folder / "result.json").is_file():
                        raise RuntimeError("invalid report directory")
                job.update(report_io_pid=os.getpid(), pid=self.proc.pid if self.proc else None)
            return reply
        except Exception:
            self.close()
            raise

    def close(self):
        import skills
        skills._worker_stop(self.proc)
        if self.proc:
            for stream in (self.proc.stdin, self.proc.stdout, self.proc.stderr):
                stream.close()
        self.proc = None


class QueueExecutor:
    """Keep this context open across submissions to amortize first startup."""
    def __init__(self, max_cpu_workers=2):
        if not 1 <= max_cpu_workers <= 4:
            raise ValueError("max_cpu_workers must be 1 to 4")
        self.max_cpu_workers = max_cpu_workers
        self.pool = ThreadPoolExecutor(max_workers=max_cpu_workers)
        self.gpu = AnalysisRunner()
        self.local = threading.local()
        self.cpu_runners = []
        self.lock = threading.Lock()
        self.submission_lock = threading.Lock()
        self.closed = False
        self.shutdown_hook = self.close
        atexit.register(self.shutdown_hook)

    def cpu(self, paths, root, timeout):
        if not hasattr(self.local, "runner"):
            self.local.runner = AnalysisRunner()
            with self.lock:
                self.cpu_runners.append(self.local.runner)
        return self.local.runner.call(paths, root, timeout)

    def close(self):
        with self.submission_lock:
            if self.closed:
                return
            self.pool.shutdown(wait=True)
            errors = []
            for runner in [self.gpu, *self.cpu_runners]:
                try:
                    runner.close()
                except Exception as exc:
                    errors.append(str(exc))
            self.closed = True
            atexit.unregister(self.shutdown_hook)
            if errors:
                raise RuntimeError("; ".join(errors))

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
