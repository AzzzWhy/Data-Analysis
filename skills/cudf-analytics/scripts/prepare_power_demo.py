#!/usr/bin/env python3
"""Reproducible real-data demo: source -> deterministic preparation -> skill -> report.

UCI household power measurements, CC BY 4.0, DOI 10.24432/C58K54. Downloads the official
archive only with --download. Missing measurements stay missing; no sampling or replication.
"""
import argparse
import hashlib
import json
import math
import sys
import urllib.request
import zipfile
from pathlib import Path

SOURCE = 'https://archive.ics.uci.edu/dataset/235/individual+household+electric+power+consumption'
ARCHIVE = 'https://archive.ics.uci.edu/static/public/235/individual%2Bhousehold%2Belectric%2Bpower%2Bconsumption.zip'
NUMERIC = ['Global_active_power', 'Global_reactive_power', 'Voltage', 'Global_intensity',
           'Sub_metering_1', 'Sub_metering_2', 'Sub_metering_3']


def sha(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(8 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--input', type=Path)
    p.add_argument('--download', action='store_true')
    p.add_argument('--out-dir', type=Path, required=True)
    p.add_argument('--engine-dir')
    args = p.parse_args()
    args.out_dir.mkdir(parents=True, exist_ok=True)
    source = args.input
    archive_hash = None
    if args.download:
        archive = args.out_dir / 'uci_power_source.zip'
        if not archive.exists():
            pending = args.out_dir / 'uci_power_source.zip.part'
            with urllib.request.urlopen(ARCHIVE, timeout=120) as response, pending.open('wb') as f:
                import shutil
                shutil.copyfileobj(response, f)
            if not zipfile.is_zipfile(pending):
                raise ValueError('download is not a complete ZIP; partial file preserved')
            pending.replace(archive)
        archive_hash = sha(archive)
        source = args.out_dir / 'household_power_consumption.txt'
        with zipfile.ZipFile(archive) as z:
            source.write_bytes(z.read('household_power_consumption.txt'))
    if source is None:
        p.error('provide --input or --download')
    import pandas as pd
    frame = pd.read_csv(source, sep=';' if source.suffix == '.txt' else ',', na_values=['?'], low_memory=False)
    required = {'Date', 'Time', *NUMERIC}
    if not required <= set(frame.columns):
        raise ValueError(f'missing columns: {required - set(frame.columns)}')
    stamp = pd.to_datetime(frame['Date'] + ' ' + frame['Time'], format='%d/%m/%Y %H:%M:%S', errors='raise')
    for col in NUMERIC:
        frame[col] = pd.to_numeric(frame[col], errors='raise')
    frame['hour'] = 'h' + stamp.dt.hour.astype(str).str.zfill(2)
    frame['month'] = 'm' + stamp.dt.month.astype(str).str.zfill(2)
    frame['day_type'] = stamp.dt.dayofweek.map(lambda n: 'weekend' if n >= 5 else 'weekday')
    prepared = args.out_dir / 'power_demo.csv'
    frame.to_csv(prepared, index=False)
    # Independent full-data pandas reference, separate from the skill's groupby operation.
    hourly = frame.groupby('hour')['Global_active_power'].agg(['mean', 'count']).sort_index()
    peak, low = hourly['mean'].idxmax(), hourly['mean'].idxmin()
    missing = {c: int(frame[c].isna().sum()) for c in NUMERIC}
    if args.engine_dir:
        sys.path.insert(0, args.engine_dir)
    import gpu_analytics as ga
    import make_deliverables as deliver
    import build_html_report as html_report
    engine_request, route = ga.pick_engine_for(str(prepared), 'groupby')
    eng = ga.detect_engine(force_cpu=not engine_request)
    ns = ga.build_parser().parse_args(['--input', str(prepared), '--op', 'groupby', '--by', 'hour',
        '--agg', 'Global_active_power:mean,count', '--top-k', '24'])
    payload, used, elapsed, fallback, rows = ga.execute(eng, lambda e: e.read(str(prepared)), 'groupby', ns)
    records = payload['top_k']
    for record in records:
        reference = hourly.loc[record['hour']]
        if not math.isclose(record['Global_active_power__mean'], reference['mean'], rel_tol=1e-6, abs_tol=1e-9):
            raise AssertionError(f'independent hourly mean mismatch: {record}')
        if record['Global_active_power__count'] != int(reference['count']):
            raise AssertionError(f'independent hourly count mismatch: {record}')
    if len(records) != 24 or rows != len(frame):
        raise AssertionError('incomplete hourly analysis')
    provenance = {'source_page': SOURCE, 'doi': '10.24432/C58K54', 'license': 'CC BY 4.0',
        'creators': ['Georges Hebrail', 'Alice Berard'], 'archive_sha256': archive_hash,
        'source_sha256': sha(source), 'prepared_sha256': sha(prepared), 'rows': len(frame),
        'date_range': [str(stamp.min()), str(stamp.max())], 'missing_measurements': missing,
        'preparation': 'Parse missing measurements as null; add hour/month/day_type; preserve every input row.',
        'sampled': False, 'replicated': False, 'independent_hourly_agreement': True,
        'engine': used.name, 'routing_reason': route,
        'peak_hour': peak, 'peak_kw': float(hourly.loc[peak, 'mean']),
        'lowest_hour': low, 'lowest_kw': float(hourly.loc[low, 'mean']),
        'peak_over_low_ratio': float(hourly.loc[peak, 'mean'] / hourly.loc[low, 'mean'])}
    (args.out_dir / 'provenance.json').write_text(json.dumps(provenance, indent=2), encoding='utf-8')
    result = {'ok': True, 'op': 'groupby', 'input': prepared.name, 'engine': used.name,
              'accelerated': used.is_gpu, 'rows_scanned': rows, 'total_seconds': elapsed,
              'routing_reason': route, 'fallback_reason': fallback if route is None else None,
              'groupby': payload}
    (args.out_dir / 'analysis.json').write_text(json.dumps(result, indent=2), encoding='utf-8')
    deliver.build(result, str(args.out_dir), title='真实家庭用电：什么时候负荷最高？',
                  source_file=prepared.name, lang='zh')
    # Hourly charts must show the whole day in chronological order. Do not mix sample
    # counts with kW on a shared axis or retain the generic bar renderer's top-12 cap.
    chronological = sorted(records, key=lambda r: r['hour'])
    foot = f'computed on {used.name} · full scan {rows:,} rows · chart rendering is not GPU-accelerated'
    for name, svg in (
        ('groupby_bar.svg', deliver.chart_bar(chronological, 'hour', 'Global_active_power__mean',
            'Mean active power by hour (kW)', 'All 24 hours · chronological · missing values excluded',
            foot, top_n=24, width=1200)),
        ('groupby_line.svg', deliver.chart_line(
            [{'hour': r['hour'], 'mean_kW': r['Global_active_power__mean']} for r in chronological],
            'hour', ['mean_kW'],
            'Daily load pattern (kW)', 'All 24 hours · chronological · one household, 2006–2010',
            foot, width=1200))):
        (args.out_dir / name).write_text(svg, encoding='utf-8')
    report = args.out_dir / 'report.md'
    extra = (f'\n## 从真实数据得到的发现\n\n'
        f'全量 {rows:,} 条分钟记录中，{peak[1:]} 时平均有功功率最高：'
        f'{provenance["peak_kw"]:.4f} kW；{low[1:]} 时最低：{provenance["lowest_kw"]:.4f} kW。'
        f'峰谷均值比为 {provenance["peak_over_low_ratio"]:.2f} 倍。\n\n'
        f'下一步可按工作日与周末、月份分别复核，再检查高负荷时段的分表用电。'
        f'这是调查优先级，不代表可直接节省同等比例电费。\n\n'
        f'## 数据来源与边界\n\n'
        f'Georges Hebrail、Alice Berard：Individual Household Electric Power Consumption，'
        f'UCI，DOI 10.24432/C58K54，CC BY 4.0。来源：{SOURCE}\n\n'
        f'仅代表一户家庭在 2006–2010 年的历史记录，不能外推到所有家庭。'
        f'有功功率缺失 {missing["Global_active_power"]:,} 条；均值排除缺失值，逐小时有效样本数随报告提供。'
        f'没有采样、没有复制记录放大数据、没有注入信号。'
        f'相关性不证明因果，IQR 异常不等于故障。本演示证明分析价值；大规模性能另用独立基准证明。\n')
    report.write_text(report.read_text(encoding='utf-8') + extra, encoding='utf-8')
    page = html_report.build_page(str(args.out_dir), '真实家庭用电：什么时候负荷最高？', 'zh')
    (args.out_dir / 'report.html').write_text(page, encoding='utf-8')
    print(json.dumps({k: provenance[k] for k in ('rows', 'engine', 'peak_hour', 'peak_kw',
          'lowest_hour', 'lowest_kw', 'peak_over_low_ratio', 'independent_hourly_agreement')}, ensure_ascii=False))


if __name__ == '__main__':
    main()
