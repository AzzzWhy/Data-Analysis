"""CPU-only contract checks for structured execution decisions.

These also run on a GPU host: force_cpu keeps the resident test deterministic.
"""
import json
import subprocess
import sys
import tempfile
from pathlib import Path

import pandas as pd

import gpu_analytics as ga
import gpu_session as gs

agent_dirs = [parent / "agent" for parent in Path(__file__).resolve().parents]
agent_dir = next((path for path in agent_dirs if (path / "skills.py").is_file()), None)
if agent_dir is None:
    raise RuntimeError("agent/skills.py not found above the test script")
sys.path.insert(0, str(agent_dir))
import skills as agent_skills  # noqa: E402


def run_cli(path, *args):
    proc = subprocess.run(
        [sys.executable, str(Path(ga.__file__)), "--input", str(path), *args],
        capture_output=True, text=True, check=True,
    )
    return json.loads(proc.stdout)


def check_decision(result, selected, actual, phase):
    decision = result["execution_decision"]
    assert decision["schema_version"] == 1
    assert decision["selected_backend"] == selected
    assert decision["actual_backend"] == actual
    assert decision["reason"]
    assert decision["estimate"] == {
        "elapsed_seconds": None,
        "peak_memory_mb": None,
        "status": "not_calibrated",
    }
    assert decision["observed"]["phase"] == phase
    assert decision["observed"]["elapsed_seconds"] >= 0
    return decision


def main():
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / "small.csv"
        path.write_text("region,revenue\nEast,10\nWest,20\nEast,30\n", encoding="utf-8")

        automatic = run_cli(path, "--op", "summary")
        decision = check_decision(automatic, "pandas", "pandas", "request_total")
        assert decision["mode"] == "auto"
        assert decision["policy"] == "measured_file_size_crossover"
        assert decision["signals"]["file_size_bytes"] == path.stat().st_size
        assert decision["signals"]["crossover_bytes"] > path.stat().st_size
        assert decision["fallback_reason"] is None
        assert decision["observed"]["elapsed_seconds"] == automatic["total_seconds"]

        forced = run_cli(path, "--op", "summary", "--force-cpu")
        decision = check_decision(forced, "pandas", "pandas", "request_total")
        assert decision["mode"] == "force_cpu"
        assert decision["policy"] == "caller_override"

        # The Agent should expose the record without burying it inside a compacted result.
        remember = agent_skills._remember_last_file
        agent_skills._remember_last_file = lambda _path: None
        try:
            tool_result = json.loads(agent_skills.analyze_dataset(
                str(path), "summary", force_cpu=True
            ))
        finally:
            agent_skills._remember_last_file = remember
        assert tool_result["success"], tool_result
        assert tool_result["execution_decision"]["mode"] == "force_cpu"
        assert "execution_decision" not in tool_result["result"]

        requested_gpu = run_cli(path, "--op", "summary", "--force-gpu")
        decision = check_decision(
            requested_gpu, "cudf", requested_gpu["engine"], "request_total"
        )
        assert decision["mode"] == "force_gpu"
        if requested_gpu["engine"] == "pandas":
            assert decision["fallback_reason"]

        opened = gs.do_open({"path": str(path), "force_cpu": True})
        assert opened["ok"], opened
        sid = opened["session_id"]
        try:
            decision = check_decision(opened, "pandas", "pandas", "session_open")
            assert decision["signals"]["admission_required_free_gb"] > 0
            assert decision["estimate"]["peak_memory_mb"] is None
            assert opened["routing_reason"] == "CPU forced by the caller"

            reused = gs.do_open({"path": str(path)})
            decision = check_decision(reused, "pandas", "pandas", "session_reuse")
            assert decision["mode"] == "reuse"
            assert reused["session_id"] == sid

            analyzed = gs.do_analyze({"sid": sid, "op": "summary"})
            assert analyzed["ok"], analyzed
            decision = check_decision(analyzed, "pandas", "pandas", "session_step")
            assert decision["mode"] == "resident"
            assert decision["signals"]["resident"] is True
            assert analyzed["routing_reason"] == "CPU forced by the caller"
            assert decision["observed"]["rows_scanned"] == 3
        finally:
            gs.do_close({"sid": sid})

        # Simulate an unsupported GPU operation without requiring CUDA. A fallback must
        # load a real pandas frame, rather than relabel the resident GPU frame as pandas.
        fake_sid = "fallback-test"
        frame = pd.read_csv(path)
        gs.SESSIONS[fake_sid] = gs.Session(
            sid=fake_sid, path=str(path), engine=ga.Engine("cudf", mod=None),
            frame=frame, rows=len(frame), identity=gs._file_identity(str(path)),
            load_seconds=0.0,
        )
        real_execute = ga.execute

        def fallback_execute(_engine, load, _op, _args):
            cpu = ga.detect_engine(force_cpu=True)
            fresh = load(cpu)
            assert isinstance(fresh, pd.DataFrame) and fresh is not frame
            return {"compute_seconds": 0.01, "rows_scanned": len(fresh)}, cpu, 0.01, \
                "simulated unsupported GPU operation", len(fresh)

        ga.execute = fallback_execute
        try:
            fallback_step = gs.do_analyze({"sid": fake_sid, "op": "summary"})
        finally:
            ga.execute = real_execute
            gs.SESSIONS.pop(fake_sid, None)
        assert fallback_step["ok"], fallback_step
        decision = check_decision(fallback_step, "cudf", "pandas", "session_step")
        assert decision["observed"]["reread_for_fallback"] is True
        assert decision["fallback_reason"]
        assert "re-read" in fallback_step["note"]

    print("PASS structured decision for automatic, forced, fallback-capable and resident paths")


if __name__ == "__main__":
    main()
