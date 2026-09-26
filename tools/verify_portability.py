#!/usr/bin/env python3
"""Independently grade the installed-skill client run; publish a sanitized trace summary."""
import argparse
import csv
import hashlib
import json
import math
import re
from pathlib import Path


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--workspace', type=Path, required=True)
    p.add_argument('--trace', type=Path, required=True)
    p.add_argument('--out', type=Path, required=True)
    args = p.parse_args()
    fixture = args.workspace / 'fixture.csv'
    with fixture.open(encoding='utf-8-sig', newline='') as f:
        rows = list(csv.DictReader(f))
    sums, counts = {}, {}
    for row in rows:
        key = row['region']
        sums[key] = sums.get(key, 0) + float(row['revenue'])
        counts[key] = counts.get(key, 0) + 1
    with (args.workspace / 'deliverables/groupby.csv').open(encoding='utf-8-sig', newline='') as f:
        actual = list(csv.DictReader(f))
    assert len(actual) == len(sums)
    assert {r['region'] for r in actual} == set(sums)
    for row in actual:
        key = row['region']
        assert math.isclose(float(row['revenue__sum']), sums[key], abs_tol=1e-9)
        assert math.isclose(float(row['revenue__mean']), sums[key] / counts[key], rel_tol=1e-12)
    assert [r['region'] for r in actual] == sorted(sums, key=sums.get, reverse=True)
    events = [json.loads(line) for line in args.trace.read_text(encoding='utf-8-sig').splitlines() if line.strip()]
    commands = [e['item'] for e in events if e.get('type') == 'item.completed'
                and e.get('item', {}).get('type') == 'command_execution']
    success = [i for i in commands if i.get('exit_code') == 0]
    read_skill = any('SKILL.md' in i.get('command', '') and '.agents' in i['command'] for i in success)
    compute = [i for i in success if 'gpu_analytics.py' in i.get('command', '')
               and '--op' in i['command'] and 'groupby' in i['command']]
    assert read_skill and compute, 'no evidence of reading installed skill and running actual aggregation'
    # Grade the actual tool output, not just the model's manually written report JSON.
    tool_result = json.loads(compute[0]['aggregated_output'])
    assert tool_result['ok'] and tool_result['groupby']['rows_scanned'] == len(rows)
    assert tool_result['engine'] == 'pandas' and not tool_result['accelerated']
    for row in tool_result['groupby']['top_k']:
        key = row['region']
        assert math.isclose(row['revenue__sum'], sums[key], abs_tol=1e-9)
        assert math.isclose(row['revenue__mean'], sums[key] / counts[key], rel_tol=1e-12)
    assert len(tool_result['groupby']['top_k']) == len(sums)
    assert any('make_deliverables.py' in i['command'] for i in success)
    assert any('build_html_report.py' in i['command'] for i in success)
    assert not any('agent_main.py' in i['command'] for i in commands)
    page = (args.workspace / 'deliverables/report.html').read_text(encoding='utf-8')
    assert '<svg' in page and '<html' in page.lower()
    assert not re.search(r'(?:src|href)=[\"\x27]https?://', page), 'report loads external resources'
    result = {'passed': True, 'client': 'Codex CLI', 'implicit_skill_selection': True,
        'installed_skill_read': read_skill, 'repository_agent_imported': False,
        'rows_checked': len(rows), 'fixture_sha256': hashlib.sha256(fixture.read_bytes()).hexdigest(),
        'engine': tool_result['engine'], 'engine_version': tool_result.get('engine_version'),
        'independent_numeric_agreement': True, 'ranked_regions': [r['region'] for r in actual],
        'csv_and_actual_tool_output_verified': True, 'inline_svg_count': page.count('<svg'),
        'successful_commands': len(success), 'failed_commands': len(commands) - len(success),
        'artifacts': ['groupby.csv', 'report.html'],
        'limitations': ['One CPU-only fixture on Windows; no remote GPU or Claude client validation.',
            'First execution-policy attempt failed; retry completed with workspace approvals.',
            'WebSocket failed and the CLI fell back to HTTPS; raw traces retained outside the public repo.']}
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, indent=2), encoding='utf-8')
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
