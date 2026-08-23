#!/usr/bin/env python3
# ruff: noqa: E402
"""Verify a Consumer Defensive shared factor-validation campaign."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = PACKAGE_ROOT.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from consumer_defensive.adapters.factor_validation import (
    validate_consumer_defensive_factor_validation,
)
from consumer_defensive.core.config import cfg_get, load_config, resolve_path


DEFAULT_CONFIG = PACKAGE_ROOT / 'config.yaml'


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, default=DEFAULT_CONFIG)
    parser.add_argument('--campaign-id', required=True)
    parser.add_argument('--output-root', type=Path, default=None)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    bundle = load_config(args.config)
    output_root = (
        args.output_root.expanduser().resolve()
        if args.output_root else resolve_path(
            cfg_get(bundle.payload, 'paths.output_dir'),
            base_dir=bundle.base_dir,
        ) / 'factor_validation'
    )
    payload = validate_consumer_defensive_factor_validation(
        output_root,
        campaign_id=args.campaign_id,
    )
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0 if payload['status'] == 'PASS' else 1


if __name__ == '__main__':
    raise SystemExit(main())
