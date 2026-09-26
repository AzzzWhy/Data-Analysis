#!/usr/bin/env python3
"""Regression: complete 24-hour tables and explicit top-K coverage warnings."""
import csv
import json
import os
import tempfile
from pathlib import Path

import skills


def main():
    with tempfile.TemporaryDirectory(prefix='group-coverage-') as folder:
        path = Path(folder) / 'hours.csv'
        with path.open('w', newline='', encoding='utf-8') as f:
            writer = csv.writer(f)
            writer.writerow(['hour', 'power'])
            writer.writerows((f'h{i:02d}', i + 0.5) for i in range(24))
        full = json.loads(skills.analyze_dataset(str(path), 'groupby', by='hour',
                                                agg='power:mean,count', force_cpu=True))
        assert full['success'], full
        group = full['result']['groupby']
        assert group['groups'] == 24 and len(group['top_k']) == 24
        assert min(r['power__mean'] for r in group['top_k']) == 0.5
        short = json.loads(skills.analyze_dataset(str(path), 'groupby', by='hour',
                            agg='power:mean,count', top_k=5, force_cpu=True))
        assert short['success'], short
        assert len(short['result']['groupby']['top_k']) == 5
        assert 'group_coverage_warning' in short['result']
        print('PASS: default 24-group expansion; exact minimum; explicit top-5 preserved and flagged')


if __name__ == '__main__':
    main()
