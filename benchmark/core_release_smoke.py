"""Release acceptance on existing data; local profiles remain on the test machine.

Requires optimized_execution_benchmark evidence and matching report profiles.
Does not install/start a model, change user settings, or publish private paths.
"""
import argparse
import json
import os
from pathlib import Path
import queue
import statistics
import subprocess
import sys
import threading
import time
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "agent"))
sys.path.insert(0, str(ROOT / "skills/cudf-analytics/scripts"))
os.environ["GPU_ANALYTICS_SCRIPT"] = str(ROOT / "skills/cudf-analytics/scripts/gpu_analytics.py")
os.environ["GPU_ANALYTICS_PYTHON"] = sys.executable
import hybrid_execution as hybrid
import gpu_session
import skills
from fast_execution_benchmark import equal, values
from optimized_execution_benchmark import STEPS, PLANS, Worker, measured


class ColdWorker(Worker):
    def __init__(self):
        # No ping: the measured first request includes Python/CUDA startup.
        self.proc = subprocess.Popen(
            [sys.executable, str(ROOT / "skills/cudf-analytics/scripts/gpu_session.py")],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, encoding="utf-8", bufsize=1)
        self.responses = queue.Queue()

        def read():
            for line in self.proc.stdout:
                self.responses.put(line)
            self.responses.put(None)

        threading.Thread(target=read, daemon=True).start()


def stop_owned_worker():
    skills._worker_stop(skills._worker)
    skills._worker = None


def check_cleanup():
    state = skills._worker_call({"cmd": "list"})
    if not state.get("ok") or state["count"] or state["warm_cache_count"]:
        raise AssertionError("acceptance left an active session or retained frame")


def inspect_batch(reply, rows):
    if not reply.get("success") or len(reply.get("results", [])) != len(STEPS):
        raise AssertionError("analysis did not return the requested three results")
    if [step["op"] for step in reply["results"]] != [step["op"] for step in STEPS]:
        raise AssertionError("analysis operations differ from the acceptance plan")
    if any(step["rows_scanned"] != rows or step.get("fallback_reason")
           for step in reply["results"]):
        raise AssertionError("unexpected fallback or incomplete full-data scan")
    if reply.get("comparison_measured") is not False:
        raise AssertionError("tool claimed an unmeasured performance comparison")
    loading = reply["loading"]
    decision = loading["decision"]
    routes = {"cpu": "cpu", "native": "native_gpu", "cpu_gpu": "cpu_gpu"}
    if (decision.get("policy") != "matched_hybrid_calibration"
            or routes.get(decision.get("selected")) != loading["actual"]):
        raise AssertionError("auto request did not use a matching calibrated route")
    if any(step["engine"] != ("pandas" if loading["actual"] == "cpu" else "cudf")
           for step in reply["results"]):
        raise AssertionError("reported loader and actual computation engines disagree")
    return {"actual": loading["actual"], "context": loading["execution_context"],
            "decision_policy": loading["decision"].get("policy"),
            "selected": loading["decision"].get("selected"),
            "engines": [step["engine"] for step in reply["results"]],
            "full_data_scanned": True, "comparison_measured": False}


def inspect_examples(reply):
    columns = reply["results"][1]["outliers"]["results"]
    if not isinstance(columns, dict) or not columns:
        raise AssertionError("outlier columns are missing")
    for column in columns.values():
        examples = column["examples"]
        if (not isinstance(examples, list) or (column["count"] > 0 and not examples)
                or any(not isinstance(row, dict) for row in examples)):
            raise AssertionError("outlier examples were truncated into placeholders")
    return {"outlier_examples_preserved": True}


def calibrate_batch(source, rows, args):
    record = json.loads((args.evidence_root / f"optimized-{rows}.json").read_text())
    if not record["results_agree"] or not record["sources_unchanged"]:
        raise ValueError("unverified warm benchmark")
    existing = args.evidence_root / "profiles" / f"{rows}.json"
    workflow = gpu_session.batch_workflow([step for plan in PLANS for step in plan])
    selected, _ = hybrid.choose(str(existing), str(source), ["region", "revenue"],
                                workflow, "reports_warm")
    if selected is None:
        raise ValueError("warm evidence no longer matches the source or runtime")
    samples = {route: record["single_samples"]["after_" + route] for route in hybrid.PATHS}
    hybrid.save_measurement(str(args.profile), str(source), ["region", "revenue"],
                            gpu_session.batch_workflow(STEPS), "batch_warm", samples)
    cold = {route: [] for route in hybrid.PATHS}
    reference = None
    for repeat in range(args.repeats):
        order = hybrid.PATHS if repeat % 2 == 0 else tuple(reversed(hybrid.PATHS))
        for route in order:
            started = time.perf_counter()
            worker = ColdWorker()
            try:
                reply, outputs, sample = measured(worker, source, route)
                if any(step["rows_scanned"] != rows for step in reply["results"]):
                    raise AssertionError("cold calibration did not scan the full dataset")
                reference = outputs if reference is None else reference
                if not equal(outputs, reference):
                    raise AssertionError("cold CPU/GPU results disagree")
            finally:
                worker.close()
            sample["seconds"] = time.perf_counter() - started
            cold[route].append(sample)
        print(json.dumps({"stage": "batch_cold_round", "rows": rows, "repeat": repeat}), flush=True)
    hybrid.save_measurement(str(args.profile), str(source), ["region", "revenue"],
                            gpu_session.batch_workflow(STEPS), "batch_cold", cold)
    decisions = {}
    for context, measurements in (("batch_warm", samples), ("batch_cold", cold)):
        selected, _ = hybrid.choose(str(args.profile), str(source), ["region", "revenue"],
                                    gpu_session.batch_workflow(STEPS), context)
        decisions[context] = {"selected": selected, "medians": {
            route: statistics.median(s["seconds"] for s in runs)
            for route, runs in measurements.items()}}
    return decisions


def real_agent(source, rows, client, model):
    from agent_main import Agent
    external_config = source.parent / "__gda_acceptance_no_external_config__.json"
    if external_config.exists():
        raise ValueError("isolated external configuration fixture unexpectedly exists")
    agent = Agent(client, verbose=False, model=model, reuse_one_shot=True,
                  external_config=str(external_config))
    records = []
    for iteration in range(2):
        offset = len(agent.messages)
        prompt = (f"请实际重新分析本机文件 {source}。列名已确认是 region 和 revenue。"
                  "用一次 analyze_batch 完成这三项独立分析，按此顺序："
                  "summary(columns='revenue')；outliers(columns='revenue', top_k=3)；"
                  "groupby(by='region', agg='revenue:sum', top_k=3)。"
                  "load_backend='auto'，不要强制 CPU/GPU，不要额外读取或 profile。"
                  "简短说明实际引擎和结果。没有基线就不要声称加速倍数。")
        started = time.perf_counter()
        answer = agent.run(prompt)
        seconds = time.perf_counter() - started
        calls = [call for message in agent.messages[offset:]
                 for call in message.get("tool_calls", [])]
        names = [call["function"]["name"] for call in calls]
        tool_results = [json.loads(message["content"]) for message in agent.messages[offset:]
                        if message["role"] == "tool"]
        if names != ["analyze_batch"] or len(tool_results) != 1:
            raise AssertionError(f"model did not use the single-batch plan: {names}")
        arguments = json.loads(calls[0]["function"]["arguments"])
        if (arguments.get("force_cpu") or arguments.get("force_gpu")
                or arguments.get("load_backend", "auto") != "auto"
                or gpu_session.batch_workflow(arguments["steps"]) != gpu_session.batch_workflow(STEPS)):
            raise AssertionError("model changed the tested workflow or bypassed auto routing")
        if not answer.strip() or "[model call failed]" in answer:
            raise AssertionError("model did not produce a final answer")
        check_cleanup()
        records.append({"iteration": iteration, "wall_seconds_including_llm": seconds,
                        "tools": names, "final_answer_present": True,
                        **inspect_batch(tool_results[0], rows), **inspect_examples(tool_results[0])})
        print(json.dumps({"stage": "real_agent", "rows": rows, **records[-1]}), flush=True)
    return records


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, action="append", required=True)
    parser.add_argument("--evidence-root", type=Path, required=True)
    parser.add_argument("--profile", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--local-model-base")
    parser.add_argument("--verify-only", action="store_true",
                        help="reuse previous timing evidence; rerun actual tools/model/cleanup only")
    args = parser.parse_args()
    if not 3 <= args.repeats <= 20:
        parser.error("repeats must be 3 to 20")
    os.environ["SESSION_WARM_CACHE_MB"] = "0"
    os.environ["SKILL_SHOW_SPEEDUP"] = "0"
    os.environ["GPU_ANALYSIS_HYBRID_PROFILE"] = str(args.profile.resolve())
    sources = [source.resolve() for source in args.input]
    identities = {source: hybrid.identity(source) for source in sources}
    previous = json.loads(args.output.read_text()) if args.verify_only else None
    if previous is not None and not previous.get("ok"):
        raise ValueError("previous acceptance did not pass")
    client = model = None
    if args.local_model_base:
        from openai import OpenAI
        with urlopen(args.local_model_base.rstrip("/") + "/models", timeout=10) as response:
            models = json.load(response)["data"]
        if len(models) != 1:
            raise ValueError("acceptance requires an unambiguous existing local model")
        model = models[0]["id"]
        client = OpenAI(base_url=args.local_model_base, api_key="local", timeout=120, max_retries=0)
    import pyarrow.parquet as pq
    record = {"model": model, "cases": [], "retained_frame_cache_disabled_for_test": True}
    for source in sources:
        rows = pq.ParquetFile(source).metadata.num_rows
        decisions = (next(case["calibration"] for case in previous["cases"] if case["rows"] == rows)
                     if previous is not None else calibrate_batch(source, rows, args))
        stop_owned_worker()
        try:
            reply = json.loads(skills.analyze_batch(str(source), STEPS))
            direct = {**inspect_batch(reply, rows), **inspect_examples(reply)}
            check_cleanup()
            stop_owned_worker()
            agent_runs = real_agent(source, rows, client, model) if client else []
            missing = source.parent / "__gda_acceptance_missing_file__.parquet"
            if missing.exists():
                raise ValueError("missing-file fixture unexpectedly exists")
            failed = json.loads(skills.analyze_batch(str(missing), STEPS))
            if failed.get("success") is not False or not failed.get("error"):
                raise AssertionError("missing input was not reported as an error")
            check_cleanup()
            record["cases"].append({"rows": rows, "calibration": decisions, "direct_auto": direct,
                                    "agent_runs": agent_runs, "missing_file_error": True,
                                    "active_sessions": 0, "warm_cache_frames": 0})
        finally:
            stop_owned_worker()
    if any(hybrid.identity(source) != identities[source] for source in sources):
        raise AssertionError("source changed during release acceptance")
    record.update(ok=True, results_agree=True, sources_unchanged=True,
                  output_fix="outlier-example-compaction-v1", timings_reused=args.verify_only,
                  scope="batch_cold includes worker startup/exit; batch_warm excludes startup; model time separately reported")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(record, allow_nan=False) + "\n", encoding="utf-8")
    print(json.dumps({"stage": "complete", "ok": True, "cases": len(record["cases"]), "model": model}), flush=True)


if __name__ == "__main__":
    try:
        main()
    finally:
        stop_owned_worker()
