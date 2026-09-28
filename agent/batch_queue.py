"""Run several saved batch plans in isolated processes with bounded concurrency.

CPU-only jobs may run in parallel. Any job which could use the GPU gets an
exclusive lane: it waits for running CPU jobs, and the next CPU group waits for
it. This is deliberately conservative on machines with shared CPU/GPU memory.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import time
from uuid import uuid4

import batch_job


@dataclass(frozen=True)
class Job:
    index: int
    name: str
    plan_path: Path
    lane: str


def load_manifest(path: Path) -> list[Job]:
    path = path.resolve()
    manifest = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(manifest, dict) or set(manifest) != {"jobs"}:
        raise ValueError("manifest must contain only a jobs array")
    entries = manifest["jobs"]
    if not isinstance(entries, list) or not 1 <= len(entries) <= 32:
        raise ValueError("jobs must contain 1 to 32 entries")
    seen = set()
    jobs = []
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict) or set(entry) != {"name", "plan"}:
            raise ValueError(f"job {index + 1} needs only name and plan")
        name, raw_path = entry["name"], entry["plan"]
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", name):
            raise ValueError(f"job {index + 1} name must be 1-40 letters, digits, _ or -")
        if name in seen:
            raise ValueError(f"duplicate job name: {name}")
        if not isinstance(raw_path, str) or not raw_path.strip():
            raise ValueError(f"job {name} plan must be a path")
        plan_path = Path(raw_path).expanduser()
        if not plan_path.is_absolute():
            plan_path = path.parent / plan_path
        plan_path = plan_path.resolve()
        plan = batch_job.load_plan(plan_path)
        # Auto might choose the GPU. Treat it as GPU-risk even if today's
        # calibration would select CPU; calibration and input can change.
        lane = "cpu" if plan.get("force_cpu", False) else "gpu_exclusive"
        jobs.append(Job(index, name, plan_path, lane))
        seen.add(name)
    return jobs


def _stop_tree(proc: subprocess.Popen) -> None:
    failed = None
    if os.name == "nt":
        killed = subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                                capture_output=True, check=False, timeout=5)
        if killed.returncode and proc.poll() is None:
            proc.kill()
            failed = "could not verify termination of the Windows job process tree"
    else:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    try:
        proc.communicate(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=3)
        raise RuntimeError("job stopped but its output pipes remain held by a descendant")
    if failed:
        raise RuntimeError(failed)


def _run_job(job: Job, report_root: Path, timeout_seconds: int) -> dict:
    started = time.perf_counter()
    started_at = datetime.now(timezone.utc).isoformat()
    def finish(record: dict) -> dict:
        return {**record, "started_at_utc": started_at,
                "finished_at_utc": datetime.now(timezone.utc).isoformat(),
                "seconds": time.perf_counter() - started}
    command = [sys.executable, str(Path(batch_job.__file__).resolve()),
               "--plan", str(job.plan_path), "--output-root", str(report_root)]
    options = ({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt"
               else {"start_new_session": True})
    try:
        proc = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, encoding="utf-8", **options)
    except OSError as exc:
        return finish({"name": job.name, "lane": job.lane, "ok": False,
                       "error": f"could not start job: {exc}"})
    try:
        stdout, stderr = proc.communicate(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        _stop_tree(proc)
        return finish({"name": job.name, "lane": job.lane, "ok": False, "pid": proc.pid,
                       "error": f"job exceeded {timeout_seconds} seconds and its process group was stopped"})
    record = {"name": job.name, "lane": job.lane, "ok": proc.returncode == 0,
              "pid": proc.pid, "exit_code": proc.returncode}
    if proc.returncode:
        record["error"] = stderr.strip()[-2000:] or stdout.strip()[-2000:]
        return finish(record)
    try:
        output = Path(stdout.strip().splitlines()[-1]).resolve()
        if not output.is_relative_to(report_root.resolve()) or not (output / "result.json").is_file():
            raise ValueError("job returned a report outside its output directory")
    except (IndexError, ValueError, OSError) as exc:
        return finish({**record, "ok": False, "error": f"invalid job report: {exc}"})
    record["report_dir"] = str(output)
    return finish(record)


def run(manifest_path: Path, output_root: Path, max_cpu_workers: int = 2,
        timeout_seconds: int = 3600) -> tuple[Path, dict]:
    if not 1 <= max_cpu_workers <= 4:
        raise ValueError("max_cpu_workers must be 1 to 4")
    if not 1 <= timeout_seconds <= 86400:
        raise ValueError("timeout_seconds must be 1 to 86400")
    jobs = load_manifest(manifest_path)
    output_root = output_root.expanduser().resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    run_dir = output_root / f"queue-{stamp}-{uuid4().hex[:8]}"
    run_dir.mkdir(exist_ok=False)
    report_root = run_dir / "reports"
    started = time.perf_counter()
    results: list[dict | None] = [None] * len(jobs)
    cursor = 0
    while cursor < len(jobs):
        if jobs[cursor].lane == "gpu_exclusive":
            job = jobs[cursor]
            try:
                results[job.index] = _run_job(job, report_root, timeout_seconds)
            except Exception as exc:
                results[job.index] = {"name": job.name, "lane": job.lane, "ok": False,
                                      "error": f"queue runner failed: {type(exc).__name__}: {exc}"}
            cursor += 1
            continue
        end = cursor
        while end < len(jobs) and jobs[end].lane == "cpu":
            end += 1
        with ThreadPoolExecutor(max_workers=max_cpu_workers) as pool:
            futures = {job.index: pool.submit(_run_job, job, report_root, timeout_seconds)
                       for job in jobs[cursor:end]}
            for index, future in futures.items():
                try:
                    results[index] = future.result()
                except Exception as exc:
                    job = jobs[index]
                    results[index] = {"name": job.name, "lane": job.lane, "ok": False,
                                      "error": f"queue runner failed: {type(exc).__name__}: {exc}"}
        cursor = end
    summary = {"schema_version": 1, "manifest": str(manifest_path.resolve()),
               "started_at_utc": stamp, "seconds": time.perf_counter() - started,
               "max_cpu_workers": max_cpu_workers, "gpu_exclusive": True,
               "jobs": results, "ok": all(item and item["ok"] for item in results)}
    (run_dir / "queue-summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8")
    return run_dir, summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--max-cpu-workers", type=int, default=2)
    parser.add_argument("--timeout-seconds", type=int, default=3600)
    args = parser.parse_args(argv)
    try:
        run_dir, summary = run(args.manifest, args.output_root,
                               args.max_cpu_workers, args.timeout_seconds)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"batch queue failed: {exc}", file=sys.stderr)
        return 1
    print(run_dir)
    return 0 if summary["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
