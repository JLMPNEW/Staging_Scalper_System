#!/usr/bin/env python3
# ruff: noqa: E402
"""Validate and republish one Consumer Defensive Stage 6C panel."""

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
from consumer_defensive.core.db import connect
from consumer_defensive.core.stage3_runtime import database_path
from consumer_defensive.core.stage6c_panel import write_stage6c_reports


DEFAULT_CONFIG = PACKAGE_ROOT / 'config.yaml'


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, default=DEFAULT_CONFIG)
    parser.add_argument('--db', type=Path, default=None)
    parser.add_argument('--stage6c-run-id', type=int, required=True)
    parser.add_argument('--output-dir', type=Path, default=None)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    bundle = load_config(args.config)
    db_path = database_path(bundle, args.db)
    with connect(
        db_path,
        timeout_sec=float(cfg_get(bundle.payload, 'runtime.sqlite_timeout_sec', 30.0)),
    ) as conn:
        row = conn.execute(
            'SELECT asof_date FROM stage6c_panel_run WHERE stage6c_run_id=?',
            (args.stage6c_run_id,),
        ).fetchone()
        if row is None:
            raise ValueError(f'Unknown Stage 6C run: {args.stage6c_run_id}')
        output_dir = (
            args.output_dir.expanduser().resolve()
            if args.output_dir else resolve_path(
                cfg_get(bundle.payload, 'paths.output_dir'),
                base_dir=bundle.base_dir,
            ) / 'stage6c' / str(row[0])
        )
        payload = write_stage6c_reports(
            conn,
            stage6c_run_id=args.stage6c_run_id,
            output_dir=output_dir,
        )
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0 if payload['status'] == 'PASS' else 1


if __name__ == '__main__':
    raise SystemExit(main())
