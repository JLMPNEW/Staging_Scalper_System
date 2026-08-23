#!/usr/bin/env python3
# ruff: noqa: E402
"""Build the immutable Consumer Defensive Stage 6C specialized PIT panel."""

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
from consumer_defensive.core.db import connect, finish_run, start_run
from consumer_defensive.core.script_runtime import iso_date
from consumer_defensive.core.stage3_runtime import database_path
from consumer_defensive.core.stage6c_panel import (
    build_stage6c_panel,
    write_stage6c_reports,
)


DEFAULT_CONFIG = PACKAGE_ROOT / 'config.yaml'


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, default=DEFAULT_CONFIG)
    parser.add_argument('--db', type=Path, default=None)
    parser.add_argument('--as-of', type=iso_date, default=date.today().isoformat())
    parser.add_argument('--history-start', type=iso_date, default=None)
    parser.add_argument('--freshness-days', type=int, default=550)
    parser.add_argument('--output-dir', type=Path, default=None)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    bundle = load_config(args.config)
    db_path = database_path(bundle, args.db)
    output_dir = (
        args.output_dir.expanduser().resolve()
        if args.output_dir else resolve_path(
            cfg_get(bundle.payload, 'paths.output_dir'),
            base_dir=bundle.base_dir,
        ) / 'stage6c' / args.as_of
    )
    with connect(
        db_path,
        timeout_sec=float(cfg_get(bundle.payload, 'runtime.sqlite_timeout_sec', 30.0)),
    ) as conn:
        run_id = start_run(
            conn,
            run_type='consumer_defensive_stage6c_panel_build',
            input_path=bundle.path,
        )
        try:
            result = build_stage6c_panel(
                conn,
                bundle,
                as_of=args.as_of,
                history_start=args.history_start,
                freshness_days=args.freshness_days,
            )
            payload = write_stage6c_reports(
                conn,
                stage6c_run_id=int(result['stage6c_run_id']),
                output_dir=output_dir,
            )
            finish_run(
                conn,
                run_id=run_id,
                status='success' if payload['status'] == 'PASS' else 'failed',
                row_count=int(payload['panel_row_count']),
                message=json.dumps(
                    {
                        'stage6c_run_id': payload['stage6c_run_id'],
                        'panel_sha256': payload['panel_sha256'],
                    },
                    sort_keys=True,
                ),
            )
        except BaseException as exc:
            finish_run(
                conn,
                run_id=run_id,
                status='failed',
                message=f'{type(exc).__name__}: {exc}',
            )
            raise
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0 if payload['status'] == 'PASS' else 1


if __name__ == '__main__':
    raise SystemExit(main())
