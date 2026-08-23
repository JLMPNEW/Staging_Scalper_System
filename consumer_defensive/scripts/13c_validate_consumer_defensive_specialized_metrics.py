#!/usr/bin/env python3
# ruff: noqa: E402
"""Validate Consumer Defensive Stage 6B and republish coverage artifacts."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = PACKAGE_ROOT.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from consumer_defensive.core.config import cfg_get, load_config, resolve_path
from consumer_defensive.core.db import connect
from consumer_defensive.core.script_runtime import iso_date
from consumer_defensive.core.specialized_metrics import (
    validate_stage6b,
    write_stage6b_reports,
)
from consumer_defensive.core.stage3_runtime import database_path


DEFAULT_CONFIG = PACKAGE_ROOT / 'config.yaml'


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, default=DEFAULT_CONFIG)
    parser.add_argument('--db', type=Path, default=None)
    parser.add_argument('--as-of', type=iso_date, default=date.today().isoformat())
    parser.add_argument('--cache-dir', type=Path, default=None)
    parser.add_argument('--output-dir', type=Path, default=None)
    parser.add_argument('--stage6b-run-id', type=int, default=0)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    bundle = load_config(args.config)
    db_path = database_path(bundle, args.db)
    cache_dir = (
        args.cache_dir.expanduser().resolve()
        if args.cache_dir
        else resolve_path(
            cfg_get(bundle.payload, 'sec_fundamentals.cache_dir'),
            base_dir=bundle.base_dir,
        )
    )
    output_dir = (
        args.output_dir.expanduser().resolve()
        if args.output_dir
        else resolve_path(
            cfg_get(bundle.payload, 'paths.output_dir'),
            base_dir=bundle.base_dir,
        ) / 'stage6b' / args.as_of
    )
    with connect(
        db_path,
        timeout_sec=float(cfg_get(bundle.payload, 'runtime.sqlite_timeout_sec', 30.0)),
    ) as conn:
        validation = validate_stage6b(
            conn,
            bundle,
            as_of=args.as_of,
            cache_dir=cache_dir,
            stage6b_run_id=args.stage6b_run_id or None,
        )
        row = conn.execute(
            '''SELECT stage6b_run_id FROM stage6b_specialized_run
               WHERE stage6b_run_id=COALESCE(?,stage6b_run_id)
                 AND asof_date=? ORDER BY stage6b_run_id DESC LIMIT 1''',
            (args.stage6b_run_id or None, args.as_of),
        ).fetchone()
        if row is not None:
            write_stage6b_reports(
                conn,
                as_of=args.as_of,
                stage6b_run_id=int(row[0]),
                output_dir=output_dir,
                validation=validation,
            )
    print(json.dumps(validation, indent=2, sort_keys=True))
    return 0 if validation['code_status'] == 'PASS' else 1


if __name__ == '__main__':
    raise SystemExit(main())
