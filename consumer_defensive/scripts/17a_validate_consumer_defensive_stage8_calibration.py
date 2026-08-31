#!/usr/bin/env python3
# ruff: noqa: E402
'''Validate report-only Consumer Defensive Stage 8 artifacts.'''

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = PACKAGE_ROOT.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from consumer_defensive.core.config import cfg_get, load_config
from consumer_defensive.core.db import connect
from consumer_defensive.core.stage3_runtime import database_path
from consumer_defensive.core.stage8_calibration import (
    validate_stage8_artifacts,
)


DEFAULT_CONFIG = PACKAGE_ROOT / 'config.yaml'


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, default=DEFAULT_CONFIG)
    parser.add_argument('--db', type=Path, default=None)
    parser.add_argument('--factor-validation-root', type=Path, required=True)
    parser.add_argument('--output-dir', type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    bundle = load_config(args.config)
    db_path = database_path(bundle, args.db)
    with connect(
        db_path,
        timeout_sec=float(cfg_get(
            bundle.payload, 'runtime.sqlite_timeout_sec', 30.0
        )),
    ) as conn:
        result = validate_stage8_artifacts(
            conn,
            bundle,
            output_dir=args.output_dir,
            factor_root=args.factor_validation_root,
        )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result['status'] == 'PASS' else 1


if __name__ == '__main__':
    raise SystemExit(main())
