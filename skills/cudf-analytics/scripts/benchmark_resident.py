#!/usr/bin/env python3
"""Compare pandas and cuDF with BOTH dataframes loaded once, in fresh processes.

Reports import/init, one load, five full-data operations, and process wall time separately.
Alternates engine order; keeps every sample and checks numerical agreement. Warm-cache
CSV, default pandas (not an optimized multithreaded CPU library), no stateless CPU strawman.
"""
import argparse
import hashlib
import json
import math
import os
import platform
import statistics
import subprocess
import sys
import time
from pathlib import Path


def digest(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(8 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


def synchronize(gpu):
    if gpu:
        import cupy
        cupy.cuda.runtime.deviceSynchronize()


def worker(args):
    start = time.perf_counter()
    if args.engine_dir:
        sys.path.insert(0, args.engine_dir)
    import gpu_analytics as ga
    eng = ga.detect_engine(force_cpu=args.worker == 'cpu')
    if args.worker == 'gpu' and not eng.is_gpu:
        raise RuntimeError(f'GPU unavailable: {eng.reason}')
    synchronize(eng.is_gpu)
    init_seconds = time.perf_counter() - start
    start = time.perf_counter()
    frame = eng.read(args.input)
    synchronize(eng.is_gpu)
    load_seconds = time.perf_counter() - start
    checks = {'rows': len(frame)}
    steps = {}
    # Same selected numeric columns and aggregation on both engines; retain full rows.
    for op in ('profile', 'summary', 'groupby', 'corr', 'outliers'):
        ns = ga.build_parser().parse_args(['--input', args.input, '--op', op,
            '--columns', args.columns, '--by', args.by,
            '--agg', args.agg, '--top-k', '20'])
        synchronize(eng.is_gpu)
        start = time.perf_counter()
        # Dispatch the native operation directly: a resident benchmark must never reload or
        # fall back to the other engine, which would invalidate the comparison.
        payload = ga.OPS[op](frame, eng, ns)
        rows = len(frame)
        synchronize(eng.is_gpu)
        steps[op] = time.perf_counter() - start
        if rows != len(frame):
            raise AssertionError(f'{op} changed the full-scan row count')
        if op == 'summary':
            checks['summary'] = {c: {k: s[k] for k in ('count', 'mean', 'median', 'min', 'max', 'q1', 'q3')}
                                 for c, s in payload['stats'].items()}
        elif op == 'groupby':
            checks['groupby'] = sorted(payload['top_k'], key=lambda r: str(r[args.by]))
        elif op == 'corr':
            checks['corr'] = payload['matrix']
        elif op == 'outliers':
            checks['outlier_counts'] = {c: s['count'] for c, s in payload['results'].items()}
    compute = sum(steps.values())
    print(json.dumps({'engine': eng.name, 'version': eng.version, 'init_seconds': init_seconds,
        'load_seconds': load_seconds, 'steps_seconds': steps, 'compute_seconds': compute,
        'resident_workflow_seconds': load_seconds + compute, 'checks': checks}, allow_nan=False))


def compare(a, b, path='result'):
    if isinstance(a, dict):
        if not isinstance(b, dict) or set(a) != set(b):
            raise AssertionError(f'{path}: keys differ')
        for k in a:
            compare(a[k], b[k], f'{path}.{k}')
    elif isinstance(a, list):
        if len(a) != len(b):
            raise AssertionError(f'{path}: lengths differ')
        for i, (x, y) in enumerate(zip(a, b)):
            compare(x, y, f'{path}[{i}]')
    elif isinstance(a, int) and isinstance(b, int):
        if a != b:
            raise AssertionError(f'{path}: integer counts differ: {a} != {b}')
    elif isinstance(a, (int, float)):
        if not math.isclose(a, b, rel_tol=1e-6, abs_tol=1e-8):
            raise AssertionError(f'{path}: {a} != {b}')
    elif a != b:
        raise AssertionError(f'{path}: values differ')


def summarize(samples, metric):
    values = [s[metric] for s in samples]
    mean = statistics.mean(values)
    return {'samples': values, 'median': statistics.median(values),
            'mean': mean, 'sample_stdev': statistics.stdev(values),
            'cv_percent': statistics.stdev(values) / mean * 100 if mean else 0}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--input', required=True)
    p.add_argument('--repeats', type=int, default=5)
    p.add_argument('--out', default='resident_benchmark.json')
    p.add_argument('--engine-dir')
    p.add_argument('--worker', choices=['cpu', 'gpu'])
    p.add_argument('--columns', default='revenue,cost,quantity')
    p.add_argument('--by', default='region')
    p.add_argument('--agg', default='revenue:sum,mean|quantity:sum')
    args = p.parse_args()
    if args.worker:
        worker(args)
        return
    if args.repeats < 3:
        p.error('at least three repeats are required')
    before = digest(args.input)  # Also warms the page cache equally before the first arm.
    raw = {'cpu': [], 'gpu': []}
    for i in range(args.repeats):
        for engine in (('cpu', 'gpu') if i % 2 == 0 else ('gpu', 'cpu')):
            command = [sys.executable, __file__, '--input', args.input, '--worker', engine,
                       '--columns', args.columns, '--by', args.by, '--agg', args.agg]
            if args.engine_dir:
                command += ['--engine-dir', args.engine_dir]
            start = time.perf_counter()
            result = subprocess.run(command, capture_output=True, text=True, timeout=600)
            wall = time.perf_counter() - start
            if result.returncode:
                raise RuntimeError(result.stderr[-4000:])
            record = json.loads(result.stdout)
            record['process_wall_seconds'] = wall
            record['repeat'] = i + 1
            record['execution_order'] = sum(map(len, raw.values())) + 1
            raw[engine].append(record)
            print(f'repeat={i+1} {engine} load={record["load_seconds"]:.3f}s '
                  f'compute={record["compute_seconds"]:.3f}s wall={wall:.3f}s', flush=True)
    for group in raw.values():
        for record in group:
            compare(raw['cpu'][0]['checks'], record['checks'])
    if before != digest(args.input):
        raise RuntimeError('input changed during measurement')
    summary = {engine: {key: summarize(group, key) for key in (
        'init_seconds', 'load_seconds', 'compute_seconds', 'resident_workflow_seconds',
        'process_wall_seconds')} for engine, group in raw.items()}
    ratios = {k: summary['cpu'][k]['median'] / summary['gpu'][k]['median']
              for k in summary['cpu']}
    output = {'schema_version': 1, 'created_utc': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
        'methodology': 'fresh process per sample; one load per engine; identical five full-data operations; '
                       'alternating arm order; warm page cache; synchronized GPU; default pandas',
        'input': {'name': Path(args.input).name, 'size_bytes': os.path.getsize(args.input), 'sha256': before},
        'host': {'platform': platform.platform(), 'logical_cores': os.cpu_count(),
                 'cuda_path': os.environ.get('CUDA_PATH')},
        'agreement': True, 'repeats': args.repeats, 'raw_samples': raw, 'summary': summary,
        'operations': {'sequence': ['profile', 'summary', 'groupby', 'corr', 'outliers'],
                       'columns': args.columns, 'by': args.by, 'agg': args.agg},
        'cpu_over_gpu_ratio': ratios,
        'unstable_metrics': [f'{e}.{k}' for e in summary for k, v in summary[e].items() if v['cv_percent'] > 10],
        'limitations': ['Warm page cache, CSV, this machine and these selected operations only.',
                       'Default pandas baseline; not compared with Polars, DuckDB or tuned CPU parallelism.',
                       'CV >10% is flagged; do not present flagged metrics as stable measurements.']}
    Path(args.out).write_text(json.dumps(output, indent=2), encoding='utf-8')
    print(json.dumps({'ratios': ratios, 'unstable_metrics': output['unstable_metrics']}), flush=True)


if __name__ == '__main__':
    main()
