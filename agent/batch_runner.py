"""Persistent queue child: one isolated analysis worker, bounded transactions."""
import json
import os
from pathlib import Path
import sys
import time

import batch_job
import skills


def run_group(paths, output_root, call_worker=skills._worker_call):
    if not isinstance(paths, list) or not 1 <= len(paths) <= 32:
        raise ValueError("group must contain 1-32 plans")
    plans = [batch_job.load_plan(Path(path)) for path in paths]
    route = lambda plan: (plan["file_path"], plan.get("force_cpu", False),
                          plan.get("force_gpu", False), plan.get("load_backend", "auto"),
                          plan.get("hybrid_profile"))
    if any(route(plan) != route(plans[0]) for plan in plans):
        raise ValueError("shared plans must have the same file and route")
    first = plans[0]
    started = time.perf_counter()
    reply = call_worker({"cmd": "batch_many", "path": first["file_path"],
        "plans": [plan["steps"] for plan in plans], "force_cpu": first.get("force_cpu", False),
        "force_gpu": first.get("force_gpu", False), "load_backend": first.get("load_backend", "auto"),
        **({"hybrid_profile": first["hybrid_profile"]} if first.get("hybrid_profile") else {})})
    records = []
    if reply.get("ok"):
        for plan, result in zip(plans, reply["results"]):
            result["success"] = result.pop("ok")
            result["comparison_measured"] = False
            try:
                folder = batch_job.write_result(plan, result, Path(output_root), result["total_seconds"])
                records.append({"ok": True, "report_dir": str(folder),
                                "seconds": result["total_seconds"], "pid": os.getpid(),
                                "shared_transaction": True})
            except Exception as exc:
                records.append({"ok": False, "error": f"report write failed: {exc}",
                                "pid": os.getpid(), "shared_transaction": True})
    else:
        # Keep per-plan failure isolation, and let individually admissible plans
        # run if a projected union is too large or one plan has a bad column.
        for plan in plans:
            job_start = time.perf_counter()
            try:
                result = call_worker({"cmd": "batch", "path": plan["file_path"], "steps": plan["steps"],
                    "force_cpu": plan.get("force_cpu", False), "force_gpu": plan.get("force_gpu", False),
                    "load_backend": plan.get("load_backend", "auto"),
                    **({"hybrid_profile": plan["hybrid_profile"]} if plan.get("hybrid_profile") else {})})
                result["success"] = result.pop("ok", False)
                folder = batch_job.write_result(plan, result, Path(output_root), time.perf_counter() - job_start)
                records.append({"ok": True, "report_dir": str(folder)})
            except TimeoutError:
                raise
            except Exception as exc:
                records.append({"ok": False, "error": f"{type(exc).__name__}: {exc}"})
            records[-1].update(seconds=time.perf_counter() - job_start, pid=os.getpid(),
                               shared_transaction=False, group_fallback=reply.get("error"))
    return {"ok": True, "jobs": records, "seconds": time.perf_counter() - started}


def main():
    try:
        for line in sys.stdin:
            try:
                request = json.loads(line)
                if request.get("cmd") == "close":
                    break
                reply = run_group(request["plans"], request["output_root"])
            except Exception as exc:
                reply = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
            print(json.dumps(reply), flush=True)
    finally:
        skills._worker_stop(skills._worker)
        skills._worker = None


if __name__ == "__main__":
    main()
