#!/usr/bin/env python3
"""
Smoke test for the cudf-analytics skill.

Generates a small synthetic dataset, runs every operation, and asserts the contract
the Skill depends on. Exits non-zero on the first failure so it can gate a demo.

It works with or without a GPU: when cuDF is present it also cross-checks that the
GPU numbers agree with pandas, which is the check that matters before claiming a
speedup in a submission.

Usage
-----
    python smoke_test.py                 # full test
    python smoke_test.py --keep          # keep the generated fixture
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "gpu_analytics.py")

FAILURES: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    status = "PASS" if condition else "FAIL"
    line = f"  [{status}] {label}"
    if detail and not condition:
        line += f"  <- {detail}"
    print(line, flush=True)
    if not condition:
        FAILURES.append(label)


def run(args: list[str]) -> tuple[int, dict, str]:
    proc = subprocess.run([sys.executable, SCRIPT] + args, capture_output=True, text=True)
    payload: dict = {}
    try:
        payload = json.loads(proc.stdout)
    except Exception:
        payload = {}
    return proc.returncode, payload, proc.stderr


def make_fixture(path: str, rows: int = 20_000) -> dict:
    import numpy as np
    import pandas as pd

    rng = np.random.default_rng(11)
    df = pd.DataFrame({
        "region": rng.choice(["APAC", "EMEA", "AMER"], size=rows),
        "category": rng.choice(["alpha", "beta", "gamma"], size=rows),
        "revenue": rng.lognormal(6.0, 1.2, size=rows),
        "cost": rng.lognormal(5.3, 1.0, size=rows),
        "quantity": rng.integers(1, 200, size=rows),
        "label": rng.choice(["x", "y"], size=rows),
    })
    df["profit"] = df["revenue"] - df["cost"]
    # A deliberately sparse column, to exercise null handling.
    df.loc[df.index[: rows // 5], "cost"] = np.nan
    # A planted outlier, to prove detection works. It goes on quantity (an integer
    # column nothing else asserts on) so it does not distort the revenue/cost
    # correlation checked elsewhere.
    df.loc[0, "quantity"] = 100_000
    # A non-numeric-looking numeric column.
    df["score"] = rng.integers(0, 100, size=rows).astype(str)
    df.to_csv(path, index=False)

    # Ground truth computed independently, so the assertions verify the script
    # against the data rather than against assumed properties of the generator.
    truth = {
        "revenue_mean": float(df["revenue"].mean()),
        "revenue_median": float(df["revenue"].median()),
        "revenue_q1": float(df["revenue"].quantile(0.25)),
        "revenue_q3": float(df["revenue"].quantile(0.75)),
        "cost_count": int(df["cost"].notnull().sum()),
        "corr_revenue_cost": float(df["revenue"].corr(df["cost"])),
        "group_median": {str(k): float(v) for k, v in
                         df.groupby("region")["revenue"].median().items()},
        "group_mean": {str(k): float(v) for k, v in
                       df.groupby("region")["revenue"].mean().items()},
        "group_nunique": {str(k): int(v) for k, v in
                          df.groupby("region")["quantity"].nunique().items()},
    }
    return truth


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rows", type=int, default=20_000)
    ap.add_argument("--keep", action="store_true")
    args = ap.parse_args()

    tmpdir = tempfile.mkdtemp(prefix="cudf-analytics-smoke-")
    csv = os.path.join(tmpdir, "fixture.csv")
    truth = make_fixture(csv, args.rows)
    print(f"fixture: {csv} ({os.path.getsize(csv) / 1e6:.2f} MB, {args.rows:,} rows)\n")

    def close(a, b, rel: float = 1e-6) -> bool:
        if a is None or b is None:
            return False
        return abs(a - b) <= rel * max(abs(b), 1.0)

    try:
        engine = None
        print("engine detection")
        code, out, err = run(["--input", csv, "--op", "profile"])
        check("profile exits 0", code == 0, err.strip()[-400:])
        engine = out.get("engine")
        check("engine reported", engine in ("cudf", "pandas"), f"got {engine!r}")
        check("accelerated flag consistent with engine",
              out.get("accelerated") is (engine == "cudf"))
        print(f"       -> engine={engine} gpu={out.get('gpu')} "
              f"rows={out.get('rows_scanned')} version={out.get('engine_version')}")
        if engine == "pandas":
            print(f"       -> fallback_reason: {out.get('fallback_reason')}")

        print("\nprofile")
        prof = out.get("profile", {})
        check("profile data nested under its op key", bool(prof), str(sorted(out.keys())))
        check("profile reports all rows", prof.get("rows") == args.rows, str(prof.get("rows")))
        check("numeric columns detected", set(prof.get("numeric_columns", [])) >=
              {"revenue", "cost", "quantity", "profit"}, str(prof.get("numeric_columns")))
        check("string column excluded from numeric",
              "region" not in (prof.get("numeric_columns") or []) and
              "label" not in (prof.get("numeric_columns") or []))
        check("null counts present for sparse column",
              int((prof.get("null_counts") or {}).get("cost", 0)) > 0,
              str(prof.get("null_counts")))
        check("preview returned", len(prof.get("preview", [])) > 0)

        print("\nsummary")
        code, out, err = run(["--input", csv, "--op", "summary"])
        check("summary exits 0", code == 0, err.strip()[-400:])
        stats = (out.get("summary") or {}).get("stats", {})
        rev = stats.get("revenue") or {}
        check("summary has revenue stats", bool(rev))
        check("count excludes nulls", rev.get("count") == args.rows, str(rev.get("count")))
        check("mean matches independently computed pandas value",
              close(rev.get("mean"), truth["revenue_mean"]),
              f"got {rev.get('mean')} want {truth['revenue_mean']}")
        check("median matches independent value",
              close(rev.get("median"), truth["revenue_median"]),
              f"got {rev.get('median')} want {truth['revenue_median']}")
        check("q1/q3 match independent values",
              close(rev.get("q1"), truth["revenue_q1"]) and close(rev.get("q3"), truth["revenue_q3"]),
              f"q1={rev.get('q1')}/{truth['revenue_q1']} q3={rev.get('q3')}/{truth['revenue_q3']}")
        check("quartiles ordered q1<=median<=q3",
              rev.get("q1") is not None and rev["q1"] <= rev["median"] <= rev["q3"],
              f"{rev.get('q1')} {rev.get('median')} {rev.get('q3')}")
        check("min<=max", rev.get("min") is not None and rev["min"] <= rev["max"])
        check("sparse column count matches independent value",
              (stats.get("cost") or {}).get("count") == truth["cost_count"],
              f"got {(stats.get('cost') or {}).get('count')} want {truth['cost_count']}")

        print("\ngroupby")
        code, out, err = run(["--input", csv, "--op", "groupby", "--by", "region",
                              "--agg", "revenue:sum,mean|quantity:max", "--top-k", "5"])
        check("groupby exits 0", code == 0, err.strip()[-400:])
        gb = out.get("groupby") or {}
        check("all groups returned", gb.get("groups") == 3, str(gb.get("groups")))
        top = gb.get("top_k") or []
        check("groupby rows present", len(top) == 3, str(len(top)))
        if top:
            cols = set(top[0].keys())
            check("aggregate columns flattened", {"region", "revenue__sum", "revenue__mean",
                                                  "quantity__max"} <= cols, str(sorted(cols)))
            sums = [r["revenue__sum"] for r in top]
            check("sorted descending by first metric", sums == sorted(sums, reverse=True), str(sums))
            means = [r["revenue__mean"] for r in top]
            check("mean <= sum for every group", all(m <= s for m, s in zip(means, sums)))

        print("\ngroupby (median + nunique, the agg aliases cuDF is strict about)")
        code, out, err = run(["--input", csv, "--op", "groupby", "--by", "region",
                              "--agg", "revenue:median|quantity:max,nunique|cost:count",
                              "--top-k", "5"])
        check("median/nunique groupby exits 0", code == 0, err.strip()[-500:])
        med_rows = (out.get("groupby") or {}).get("top_k") or []
        check("median column present", any("revenue__median" in r for r in med_rows),
              str(sorted(med_rows[0].keys()) if med_rows else []))
        check("nunique column present", any("quantity__nunique" in r for r in med_rows),
              str(sorted(med_rows[0].keys()) if med_rows else []))
        check("all groups returned", len(med_rows) == 3, str(len(med_rows)))
        # The emulated median must equal the independently computed group median,
        # aligned by group key (a mis-merged column would pass every other check).
        got = {str(r["region"]): r.get("revenue__median") for r in med_rows}
        check("per-group median matches independent value",
              all(close(got.get(k), v) for k, v in truth["group_median"].items()),
              f"got {got} want {truth['group_median']}")
        got_nu = {str(r["region"]): r.get("quantity__nunique") for r in med_rows}
        check("per-group nunique matches independent value",
              all(close(got_nu.get(k), v, 1e-9) for k, v in truth["group_nunique"].items()),
              f"got {got_nu} want {truth['group_nunique']}")

        print("\ncorr")
        code, out, err = run(["--input", csv, "--op", "corr", "--top-k", "5"])
        check("corr exits 0", code == 0, err.strip()[-400:])
        corr = out.get("corr") or {}
        matrix = corr.get("matrix") or {}
        check("matrix is square over numeric cols",
              len(matrix) >= 4 and all(len(v) >= 4 for v in matrix.values()), str(len(matrix)))
        check("diagonal is 1.0", abs((matrix.get("revenue") or {}).get("revenue", 0) - 1.0) < 1e-9)
        check("symmetric", abs((matrix.get("revenue") or {}).get("cost", 0) -
                               (matrix.get("cost") or {}).get("revenue", 0)) < 1e-9)
        check("revenue/cost correlation matches independent value",
              close((matrix.get("revenue") or {}).get("cost"), truth["corr_revenue_cost"]),
              f"got {(matrix.get('revenue') or {}).get('cost')} want {truth['corr_revenue_cost']}")
        check("pairs ranked by |corr|",
              [p["corr"] for p in (corr.get("pairs") or [])] ==
              sorted([p["corr"] for p in (corr.get("pairs") or [])], key=abs, reverse=True))

        print("\noutliers")
        code, out, err = run(["--input", csv, "--op", "outliers", "--columns", "revenue,quantity",
                              "--top-k", "5"])
        check("outliers exits 0", code == 0, err.strip()[-400:])
        allres = (out.get("outliers") or {}).get("results") or {}
        res = allres.get("revenue") or {}
        check("bounds computed", res.get("lower_bound") is not None and
              res.get("upper_bound") is not None)
        check("bounds ordered", res.get("lower_bound", 0) < res.get("upper_bound", 0))
        check("outlier count is a small share", (res.get("pct") or 0) < 20.0, str(res.get("pct")))
        check("examples returned", len(res.get("examples") or []) >= 1)
        qres = allres.get("quantity") or {}
        check("planted outlier detected in quantity", (qres.get("count") or 0) >= 1,
              str(qres.get("count")))
        check("planted extreme value present in quantity examples",
              any((r.get("quantity") or 0) >= 100_000 for r in (qres.get("examples") or [])),
              str([r.get("quantity") for r in (qres.get("examples") or [])]))
        check("only the requested numeric columns were scanned",
              set(allres.keys()) == {"revenue", "quantity"}, str(sorted(allres.keys())))

        print("\nauto (single-call path the agent uses)")
        code, out, err = run(["--input", csv, "--op", "auto"])
        check("auto exits 0", code == 0, err.strip()[-400:])
        check("auto bundles profile+summary+outliers",
              all(k in out for k in ("profile", "summary", "outliers")), str(sorted(out.keys())))
        check("auto includes timing", isinstance(out.get("compute_seconds"), (int, float)))

        print("\nerror handling")
        code, out, err = run(["--input", os.path.join(tmpdir, "nope.csv"), "--op", "auto"])
        check("missing file exits 2", code == 2, str(code))
        check("missing file returns error payload", "error" in out, str(out)[:200])

        code, out, err = run(["--input", csv, "--op", "groupby"])
        check("groupby without --by exits 2", code == 2, str(code))

        code, out, err = run(["--input", csv, "--op", "groupby", "--by", "region",
                              "--agg", "revenue:bogus"])
        check("bad agg func exits 2", code == 2, str(code))
        check("bad agg func explains the error", "bogus" in json.dumps(out), str(out)[:200])

        code, out, err = run(["--input", csv, "--op", "summary", "--columns", "region"])
        check("non-numeric --columns exits 2", code == 2, str(code))

        print("\n--deliverables: language follows the caller, not a hardcoded default")
        # make_deliverables had no coverage here at all before this section. The language
        # switch is exactly the kind of thing that looks fine and silently regresses, so it is
        # asserted directly: a Chinese caller must get a Chinese report, an English caller an
        # English one, and an English report must contain no Chinese at all.
        try:
            sys.path.insert(0, HERE)
            import make_deliverables as MD

            check("language: Chinese text selects zh",
                  MD.detect_lang("分析这份数据") == "zh", MD.detect_lang("分析这份数据"))
            check("language: English text selects en",
                  MD.detect_lang("analyse this dataset") == "en",
                  MD.detect_lang("analyse this dataset"))
            check("language: no text defaults to zh",
                  MD.detect_lang(None, "") == "zh", MD.detect_lang(None, ""))
            check("language: an explicit value overrides detection",
                  MD.resolve_lang("en", "分析这份数据") == "en"
                  and MD.resolve_lang("zh", "analyse this") == "zh")

            deliv_dir = os.path.join(tmpdir, "deliv")
            payload = {"op": "auto", "engine": "cudf", "rows_scanned": 20000,
                       "profile": {"rows": 20000, "columns": ["region", "revenue"],
                                   "dtypes": {"region": "object", "revenue": "float64"}},
                       "outliers": {"results": {"revenue": {"count": 5, "pct": 0.03,
                                                            "lower_bound": -1.0,
                                                            "upper_bound": 9.0}}}}
            man_zh = MD.build(dict(payload), os.path.join(deliv_dir, "zh"),
                              title="报告", source_file="fixture.csv", lang="zh")
            man_en = MD.build(dict(payload), os.path.join(deliv_dir, "en"),
                              title="Report", source_file="fixture.csv", lang="en")
            md_zh = open(man_zh["report"], encoding="utf-8").read()
            md_en = open(man_en["report"], encoding="utf-8").read()
            check("zh report is Chinese", "分析行数" in md_zh and "数据概况" in md_zh)
            check("en report is English", "Rows analysed" in md_en
                  and "Dataset profile" in md_en)
            check("en report carries no Chinese",
                  not re.search(r"[\u4e00-\u9fff]", md_en),
                  str(re.findall(r"[\u4e00-\u9fff]+", md_en)[:3]))
            check("zh report states the full-scan claim",
                  "全量扫描，非抽样" in md_zh)
            check("zh report does not claim GPU rendering",
                  "图表渲染并未使用 GPU" in md_zh)
            check("both languages produce the same sections",
                  md_zh.count("##") == md_en.count("##"),
                  f"zh={md_zh.count('##')} en={md_en.count('##')}")
            check("charts are produced for the report",
                  man_en["chart_count"] >= 1, str(man_en["chart_count"]))
        except Exception as exc:  # noqa: BLE001 - a smoke test should report, not crash
            check("deliverables section ran", False, f"{type(exc).__name__}: {exc}")

        print("\n--engine routing: small files go to the CPU, because the GPU is slower there")
        # Measured on the GB10: the GPU path carries about 1.5 s of fixed cost (0.03 python
        # start, 0.55 import cudf, 0.95 import+frame), so a 10k-row file costs 1.58 s on the GPU
        # against 0.19 s on the CPU. The crossover is between 5M (0.85x) and 8M (1.11x) rows.
        # These assertions pin that behaviour down, including the case that got it wrong first:
        # a byte threshold sent the 881 MB / 20M-row demo file to the CPU and threw away its
        # speedup, because CSV density varies by more than 2x.
        try:
            import gpu_analytics as GA
            # NOTE: bound locally on purpose. `main()` reuses the name `csv` for the fixture
            # path, so a module-level `import csv` is shadowed here and every csv.writer() call
            # raises AttributeError on a str.
            from csv import writer as csv_writer

            for rows, name in ((10_000, "tiny_dense.csv"), (20_000, "small.csv")):
                p = os.path.join(tmpdir, name)
                with open(p, "w", newline="") as fh:
                    wr = csv_writer(fh)
                    wr.writerow(["row_id", "label", "value"])
                    for i in range(rows):
                        wr.writerow([i, "label-%d" % (i % 97), i * 1.5])

            tiny = os.path.join(tmpdir, "tiny_dense.csv")
            use_gpu, reason = GA.pick_engine_for(tiny, "auto")
            check("routing: a tiny file routes to the CPU", use_gpu is False, str(use_gpu))
            check("routing: the CPU choice carries a reason to report",
                  bool(reason) and "MB" in reason, str(reason)[:80])
            check("routing: forcing the GPU overrides routing",
                  GA.pick_engine_for(tiny, "auto", force_gpu=True)[0] is True)
            check("routing: forcing the CPU overrides routing",
                  GA.pick_engine_for(tiny, "auto", force_cpu=True)[0] is False)

            # Density independence: the same row count in a much wider file must still be
            # judged by rows, not bytes.
            dense = os.path.join(tmpdir, "density_dense.csv")
            sparse = os.path.join(tmpdir, "density_sparse.csv")
            for p, pad in ((dense, 2), (sparse, 60)):
                with open(p, "w", newline="") as fh:
                    wr = csv_writer(fh)
                    wr.writerow(["row_id", "label", "value"])
                    for i in range(200_000):
                        wr.writerow([i, "y" * pad, i * 1.5])
            e1, e2 = GA._estimate_rows(dense), GA._estimate_rows(sparse)
            check("routing: row estimate does not depend on row density",
                  e1 and e2 and abs(e1 - e2) / max(e1, e2) < 0.15,
                  f"dense={e1} sparse={e2}")
            check("routing: row estimate is within 15% of the truth",
                  abs(e1 - 200_000) / 200_000 < 0.15, f"got {e1} want ~200000")
            check("routing: a wide-but-small file still routes to the CPU",
                  GA.pick_engine_for(sparse, "auto")[0] is False)
            # The threshold must stay inside the measured crossover band. 5M rows measured
            # 0.85x (CPU wins) and 8M measured 1.11x (GPU wins), so a threshold outside that
            # band would send files to whichever engine is slower.
            check("routing: the row threshold sits inside the measured crossover band",
                  5_000_000 <= GA.SMALL_ROWS <= 8_000_000, f"SMALL_ROWS={GA.SMALL_ROWS:,}")
            check("routing: the byte shortcut stays far below the crossover",
                  GA.TINY_FILE_BYTES <= 128e6, f"TINY_FILE_BYTES={GA.TINY_FILE_BYTES}")
        except Exception as exc:  # noqa: BLE001
            check("routing section ran", False, f"{type(exc).__name__}: {exc}")

        print("\n--force-cpu parity (CPU numbers must match the default path)")
        code, cpu_out, err = run(["--input", csv, "--op", "summary", "--force-cpu"])
        check("force-cpu exits 0", code == 0, err.strip()[-400:])
        check("force-cpu reports pandas", cpu_out.get("engine") == "pandas",
              str(cpu_out.get("engine")))
        cpu_rev = ((cpu_out.get("summary") or {}).get("stats") or {}).get("revenue") or {}
        check("CPU path mean matches independent value",
              close(cpu_rev.get("mean"), truth["revenue_mean"]),
              f"got {cpu_rev.get('mean')} want {truth['revenue_mean']}")
        if engine == "cudf":
            check("GPU/CPU means agree", close(rev.get("mean"), cpu_rev.get("mean")),
                  f"cudf={rev.get('mean')} pandas={cpu_rev.get('mean')}")
            check("GPU/CPU medians agree", close(rev.get("median"), cpu_rev.get("median")),
                  f"cudf={rev.get('median')} pandas={cpu_rev.get('median')}")
            check("GPU/CPU max agree", close(rev.get("max"), cpu_rev.get("max")),
                  f"cudf={rev.get('max')} pandas={cpu_rev.get('max')}")
        else:
            print("       (skipped: no GPU here, nothing to cross-check)")

    finally:
        if not args.keep:
            for name in ("fixture.csv",):
                try:
                    os.remove(os.path.join(tmpdir, name))
                except OSError:
                    pass
            try:
                os.rmdir(tmpdir)
            except OSError:
                pass
        else:
            print(f"\nfixture kept at {csv}")

    print("\n" + "=" * 60)
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} check(s)")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print("ALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
