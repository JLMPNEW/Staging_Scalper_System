#!/usr/bin/env python3
# ruff: noqa: E402
'''Validate Consumer Defensive Stage 7 shadow baseline scores.'''

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = PACKAGE_ROOT.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from consumer_defensive.core.config import cfg_get, load_config, resolve_path
from consumer_defensive.core.db import connect, init_db
from consumer_defensive.core.market_data import write_csv, write_json
from consumer_defensive.core.script_runtime import iso_date
from consumer_defensive.core.stage3_runtime import database_path
from consumer_defensive.core.stage7_scoring import validate_stage7_scores


DEFAULT_CONFIG = PACKAGE_ROOT / 'config.yaml'


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, default=DEFAULT_CONFIG)
    parser.add_argument('--db', type=Path, default=None)
    parser.add_argument('--as-of', type=iso_date, required=True)
    parser.add_argument('--output-dir', type=Path, default=None)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    bundle = load_config(args.config)
    db_path = database_path(bundle, args.db)
    source_id = str(cfg_get(bundle.payload, 'stage7_scoring.source_id'))
    output_dir = (
        args.output_dir.expanduser().resolve()
        if args.output_dir
        else resolve_path(
            cfg_get(bundle.payload, 'paths.output_dir'),
            base_dir=bundle.base_dir,
        ) / 'stage7' / source_id / args.as_of
    )
    with connect(
        db_path,
        timeout_sec=float(
            cfg_get(bundle.payload, 'runtime.sqlite_timeout_sec', 30.0)
        ),
    ) as conn:
        init_db(conn)
        result = validate_stage7_scores(conn, bundle, as_of=args.as_of)
    write_csv(output_dir / 'stage7_validation_checks.csv', result['checks'])
    write_json(output_dir / 'stage7_validation.json', result)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result['status'] == 'PASS' else 1


if __name__ == '__main__':
    raise SystemExit(main())
