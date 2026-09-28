"""Local JSON-lines report service. Reuse workers, never cache input frames.

No listening network port, automatic installation, or model/API dependency.
Send {"cmd":"run","manifest":"/absolute/queue.json","id":"job-1"} on stdin.
EOF or {"cmd":"close"} releases the owned worker pool.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import signal
import sys
import time

from batch_queue import QueueExecutor, run

MAX_REQUEST_BYTES = 16 * 1024


def respond(stream, payload):
    stream.write(json.dumps(payload, ensure_ascii=False, allow_nan=False) + "\n")
    stream.flush()


def serve(source, destination, output_root, *, max_cpu_workers=2, timeout_seconds=3600):
    if not 1 <= timeout_seconds <= 86400:
        raise ValueError("timeout_seconds must be 1 to 86400")
    with QueueExecutor(max_cpu_workers) as executor:
        respond(destination, {"event": "ready", "protocol_version": 1,
            "process_reuse": True, "cross_request_frame_cache": False})
        for_count = 0
        while True:
            # Reject oversized input and end the stream, avoiding both unbounded
            # allocations and ambiguous resynchronization after a truncated line.
            line = source.readline(MAX_REQUEST_BYTES + 1)
            if not line:
                break
            if len(line.encode("utf-8")) > MAX_REQUEST_BYTES:
                respond(destination, {"ok": False, "error": "request exceeds 16 KiB; stream closed"})
                break
            request_id = None
            try:
                request = json.loads(line)
                if not isinstance(request, dict):
                    raise ValueError("request must be an object")
                request_id = request.get("id")
                if request_id is not None and (not isinstance(request_id, str) or len(request_id) > 80):
                    request_id = None
                    raise ValueError("id must be a string of at most 80 characters")
                cmd = request.get("cmd")
                fields = {"cmd", "id", "manifest"} if cmd == "run" else {"cmd", "id"}
                if set(request) - fields:
                    raise ValueError("unsupported request field")
                if cmd == "close":
                    # Close BEFORE acknowledging; clients may trust this response
                    # as proof that the service no longer owns analysis workers.
                    executor.close()
                    respond(destination, {"id": request_id, "ok": True, "event": "closed"})
                    break
                if cmd == "status":
                    pids = [runner.proc.pid for runner in [executor.gpu, *executor.cpu_runners]
                            if runner.proc and runner.proc.poll() is None]
                    respond(destination, {"id": request_id, "ok": True,
                        "completed_requests": for_count, "worker_pids": pids,
                        "cross_request_frame_cache": False})
                    continue
                if cmd != "run":
                    raise ValueError("cmd must be run, status or close")
                manifest = request.get("manifest")
                if not isinstance(manifest, str) or not manifest.strip():
                    raise ValueError("manifest must be a non-empty local path")
                timer = time.perf_counter()
                folder, summary = run(Path(manifest), Path(output_root), max_cpu_workers,
                    timeout_seconds, executor=executor)
                for_count += 1
                respond(destination, {"id": request_id, "ok": summary["ok"],
                    "run_dir": str(folder), "wall_seconds": time.perf_counter() - timer,
                    "summary": summary})
            except (OSError, ValueError, RuntimeError) as exc:
                respond(destination, {"id": request_id, "ok": False,
                    "error": f"{type(exc).__name__}: {exc}"})


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--max-cpu-workers", type=int, default=2)
    parser.add_argument("--timeout-seconds", type=int, default=3600)
    args = parser.parse_args(argv)
    def terminate(_signum, _frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, terminate)
    try:
        serve(sys.stdin, sys.stdout, args.output_root,
              max_cpu_workers=args.max_cpu_workers, timeout_seconds=args.timeout_seconds)
    except KeyboardInterrupt:
        return 130
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"report service failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
