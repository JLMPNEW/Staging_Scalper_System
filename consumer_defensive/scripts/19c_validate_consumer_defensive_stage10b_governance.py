#!/usr/bin/env python3
# ruff: noqa: E402
'''Validate immutable Consumer Defensive Stage 10B governance artifacts.'''
from __future__ import annotations
import argparse
import json
import sys
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
if str(PACKAGE_ROOT.parent) not in sys.path:
    sys.path.insert(0, str(PACKAGE_ROOT.parent))
from consumer_defensive.core.config import load_config
from consumer_defensive.core.stage10b_governance import validate_stage10b_governance, write_stage10b_validation

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, default=PACKAGE_ROOT / 'config.yaml')
    for flag in ('stage10-root', 'stage9-root', 'stage8-root', 'factor-validation-root', 'output-dir'):
        parser.add_argument(f'--{flag}', type=Path, required=True)
    return parser.parse_args()

def main() -> int:
    args = parse_args()
    result = validate_stage10b_governance(load_config(args.config), stage10_root=args.stage10_root, stage9_root=args.stage9_root, stage8_root=args.stage8_root, factor_root=args.factor_validation_root, output_dir=args.output_dir)
    write_stage10b_validation(args.output_dir, result)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result['status'] == 'PASS' else 1
if __name__ == '__main__':
    raise SystemExit(main())
