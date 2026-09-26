#!/usr/bin/env python3
"""Install the self-contained skill into a specified project's client discovery folder."""
import argparse
import shutil
from pathlib import Path


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--project', type=Path, required=True)
    p.add_argument('--client', choices=['codex', 'claude'], default='codex')
    args = p.parse_args()
    source = Path(__file__).resolve().parents[1] / 'skills' / 'cudf-analytics'
    root = args.project.resolve()
    target = root / ('.agents' if args.client == 'codex' else '.claude') / 'skills' / 'cudf-analytics'
    if target.exists():
        p.error(f'existing installation preserved: {target}')
    shutil.copytree(source, target, ignore=shutil.ignore_patterns('__pycache__', '*.pyc', '*.log'))
    print(f'Installed {target}')
    print('No agent/*.py code or API credentials were installed.')


if __name__ == '__main__':
    main()
