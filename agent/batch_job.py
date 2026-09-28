"""Run a saved analysis plan without a model, suitable for reports and schedulers."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sys
import time
from uuid import uuid4

import skills


PLAN_FIELDS = {"title", "file_path", "steps", "language", "force_cpu",
               "load_backend", "hybrid_profile"}
STEP_FIELDS = {"op", "by", "agg", "columns", "top_k"}
OPS = {"auto", "profile", "summary", "groupby", "corr", "outliers"}


def load_plan(path: Path) -> dict:
    plan = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(plan, dict) or set(plan) - PLAN_FIELDS:
        raise ValueError("plan must be an object with supported fields only")
    source = plan.get("file_path")
    if not isinstance(source, str) or not source.strip():
        raise ValueError("file_path must be a non-empty path")
    source_path = Path(source).expanduser()
    if not source_path.is_absolute():
        source_path = path.parent / source_path
    source_path = source_path.resolve()
    if not source_path.is_file():
        raise ValueError(f"data file does not exist: {source_path}")
    steps = plan.get("steps")
    if not isinstance(steps, list) or not 1 <= len(steps) <= 8:
        raise ValueError("steps must contain 1 to 8 analyses")
    for index, step in enumerate(steps, 1):
        if (not isinstance(step, dict) or set(step) - STEP_FIELDS or
                step.get("op") not in OPS):
            raise ValueError(f"step {index} has an unsupported operation or field")
        if step["op"] == "groupby" and not step.get("by"):
            raise ValueError(f"step {index} groupby requires by")
        if any(key in step and not isinstance(step[key], str)
               for key in ("by", "agg", "columns")):
            raise ValueError(f"step {index} columns, by and agg must be strings")
        top_k = step.get("top_k")
        if top_k is not None and (isinstance(top_k, bool) or not isinstance(top_k, int)
                                  or not 1 <= top_k <= 100):
            raise ValueError(f"step {index} top_k must be an integer from 1 to 100")
    if plan.get("language", "zh") not in {"zh", "en"}:
        raise ValueError("language must be zh or en")
    if not isinstance(plan.get("force_cpu", False), bool):
        raise ValueError("force_cpu must be a boolean")
    if plan.get("load_backend", "auto") not in {"auto", "native", "cpu_gpu"}:
        raise ValueError("load_backend must be auto, native or cpu_gpu")
    if plan.get("force_cpu") and plan.get("load_backend") == "cpu_gpu":
        raise ValueError("force_cpu conflicts with cpu_gpu")
    profile = plan.get("hybrid_profile")
    if profile is not None:
        if not isinstance(profile, str) or not profile.strip():
            raise ValueError("hybrid_profile must be a non-empty path")
        profile_path = Path(profile).expanduser()
        if not profile_path.is_absolute():
            profile_path = path.parent / profile_path
        profile_path = profile_path.resolve()
        if not profile_path.is_file():
            raise ValueError(f"hybrid profile does not exist: {profile_path}")
        plan["hybrid_profile"] = str(profile_path)
    title = plan.get("title", "Data analysis")
    if not isinstance(title, str) or not title.strip() or len(title) > 160:
        raise ValueError("title must be a non-empty string of at most 160 characters")
    return {**plan, "title": title.strip(), "file_path": str(source_path)}


def _cell(value: object) -> str:
    if value is None:
        return "—"
    if isinstance(value, float):
        value = f"{value:.6g}"
    return str(value).replace("|", "\\|").replace("\r", " ").replace("\n", " ")


def _table(rows: list[dict], fields: list[str]) -> list[str]:
    return ["| " + " | ".join(_cell(field) for field in fields) + " |",
            "| " + " | ".join("---" for _ in fields) + " |",
            *("| " + " | ".join(_cell(row.get(field)) for field in fields) + " |"
              for row in rows)]


def render_report(plan: dict, result: dict, wall_seconds: float) -> str:
    zh = plan.get("language", "zh") == "zh"
    t = (lambda chinese, english: chinese if zh else english)
    lines = [f"# {plan['title']}", "",
             f"{t('数据文件', 'Data file')}: `{plan['file_path']}`  ",
             f"{t('扫描行数', 'Rows scanned')}: {result.get('rows', '—')}  ",
             f"{t('批量计算', 'Batch compute')}: {_cell(result.get('total_seconds'))} s  ",
             f"{t('分析调用耗时（含进程启动）', 'Analysis call wall time (including startup)')}: "
             f"{wall_seconds:.6f} s  ",
             f"{t('实际加载方式', 'Actual load path')}: "
             f"{_cell(result.get('loading', {}).get('actual'))}", "",
             f"## {t('各步骤', 'Steps')}", ""]
    overview = []
    for index, step in enumerate(result["results"], 1):
        overview.append({"#": index, t("操作", "Operation"): step.get("op"),
                         t("计算引擎", "Compute engine"): step.get("engine"),
                         t("扫描行数", "Rows"): step.get("rows_scanned"),
                         t("耗时秒", "Seconds"): step.get("step_seconds")})
    lines.extend(_table(overview, list(overview[0])))
    for index, step in enumerate(result["results"], 1):
        op = step["op"]
        payload = step.get(op, {})
        lines.extend(["", f"## {index}. {op}", ""])
        if op == "summary" and isinstance(payload.get("stats"), dict):
            stats = [{"column": name, **values} for name, values in payload["stats"].items()]
            fields = ["column", "count", "nulls", "mean", "std", "min", "q1",
                      "median", "q3", "max"]
            lines.extend(_table(stats, fields))
        elif op == "groupby" and isinstance(payload.get("top_k"), list):
            rows = payload["top_k"]
            lines.append(f"{t('分组总数', 'Total groups')}: {_cell(payload.get('groups'))}")
            if len(rows) < payload.get("groups", 0):
                lines.append(t("仅展示前 K 组，不可据此推断全部分组的最小值。",
                               "Only top K groups are shown; this is not the global minimum."))
            if rows:
                lines.extend(["", *_table(rows, list(rows[0]))])
        elif op == "profile":
            fields = payload.get("columns", [])
            rows = [{"column": name, "dtype": payload.get("dtypes", {}).get(name),
                     "nulls": payload.get("null_counts", {}).get(name)} for name in fields]
            if rows:
                lines.extend(_table(rows, ["column", "dtype", "nulls"]))
        elif op == "corr" and isinstance(payload.get("pairs"), list):
            lines.append(f"{t('方法', 'Method')}: {_cell(payload.get('method'))}")
            if payload["pairs"]:
                lines.extend(["", *_table(payload["pairs"], ["a", "b", "corr"])])
        elif op == "outliers" and isinstance(payload.get("results"), dict):
            rows = [{"column": name, **values} for name, values in payload["results"].items()]
            if rows:
                lines.extend(_table(rows, ["column", "valid_count", "count", "pct",
                                           "lower_bound", "upper_bound"]))
        else:
            # The full machine-readable result is kept alongside the report. Other
            # operations have different nested shapes and should not be flattened
            # into a misleading table by guessing their meaning.
            lines.append(t("完整数值见 result.json 对应步骤。",
                           "Exact values are in the corresponding result.json step."))
    lines.extend(["", t("每步的实际引擎已列出；本次未运行 CPU/GPU 对照基线。",
                         "Actual engines are listed per step; no CPU/GPU baseline was run."), ""])
    return "\n".join(lines)


def run(plan_path: Path, output_root: Path) -> Path:
    plan = load_plan(plan_path.resolve())
    started = time.perf_counter()
    result = json.loads(skills.analyze_batch(
        plan["file_path"], plan["steps"], force_cpu=plan.get("force_cpu", False),
        load_backend=plan.get("load_backend", "auto"),
        hybrid_profile=plan.get("hybrid_profile")))
    wall_seconds = time.perf_counter() - started
    if not result.get("success"):
        raise RuntimeError(result.get("error", "batch failed"))
    output_root = output_root.expanduser().resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    slug = re.sub(r"[^a-z0-9]+", "-", plan["title"].lower()).strip("-")[:32] or "batch"
    run_dir = output_root / f"{stamp}-{slug}-{uuid4().hex[:8]}"
    run_dir.mkdir(exist_ok=False)
    (run_dir / "result.json").write_text(json.dumps({
        "schema_version": 1, "run_at_utc": stamp, "plan": plan,
        "job_wall_seconds": wall_seconds, "result": result,
    }, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    (run_dir / "report.md").write_text(render_report(plan, result, wall_seconds), encoding="utf-8")
    return run_dir


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run a saved CPU/GPU batch analysis plan")
    parser.add_argument("--plan", type=Path, required=True, help="JSON plan file")
    parser.add_argument("--output-root", type=Path, required=True,
                        help="each run gets its own dated report directory")
    args = parser.parse_args(argv)
    try:
        output = run(args.plan, args.output_root)
    except (OSError, ValueError, RuntimeError, json.JSONDecodeError) as exc:
        print(f"batch job failed: {exc}", file=sys.stderr)
        return 1
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
