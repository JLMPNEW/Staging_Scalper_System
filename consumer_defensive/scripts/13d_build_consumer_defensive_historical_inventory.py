#!/usr/bin/env python3
# ruff: noqa: E402
"""Plan the reusable chronological Stage 6B SEC filing backfill."""

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
from consumer_defensive.core.historical_filing_inventory import (
    build_historical_filing_inventory,
    write_historical_inventory_artifacts,
)
from consumer_defensive.core.script_runtime import iso_date
from consumer_defensive.core.stage3_runtime import database_path


DEFAULT_CONFIG = PACKAGE_ROOT / 'config.yaml'


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, default=DEFAULT_CONFIG)
    parser.add_argument('--db', type=Path, default=None)
    parser.add_argument('--as-of', type=iso_date, default=date.today().isoformat())
    parser.add_argument('--history-start', type=iso_date, default=None)
    parser.add_argument('--history-end', type=iso_date, default=None)
    parser.add_argument('--maximum-documents-per-issuer', type=int, default=None)
    parser.add_argument('--output-dir', type=Path, default=None)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    bundle = load_config(args.config)
    db_path = database_path(bundle, args.db)
    output_dir = (
        args.output_dir.expanduser().resolve()
        if args.output_dir
        else resolve_path(
            cfg_get(bundle.payload, 'paths.output_dir'),
            base_dir=bundle.base_dir,
        ) / 'stage6b' / args.as_of / 'historical_inventory'
    )
    with connect(
        db_path,
        timeout_sec=float(
            cfg_get(bundle.payload, 'runtime.sqlite_timeout_sec', 30.0)
        ),
    ) as conn:
        run_id = start_run(
            conn,
            run_type='consumer_defensive_stage6b_historical_inventory',
            input_path=bundle.path,
        )
        try:
            result = build_historical_filing_inventory(
                conn,
                bundle,
                generated_asof=args.as_of,
                history_start=args.history_start,
                history_end=args.history_end,
                maximum_documents_per_issuer=(
                    args.maximum_documents_per_issuer
                ),
            )
            write_historical_inventory_artifacts(
                result,
                output_dir=output_dir,
            )
            finish_run(
                conn,
                run_id=run_id,
                status=(
                    'success'
                    if result['status'] == 'PASS'
                    else 'failed'
                ),
                row_count=int(result['target_filing_count']),
                message=json.dumps(
                    {
                        key: value for key, value in result.items()
                        if key not in {'inventory_rows', 'replay_schedule'}
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
    payload = {
        'database': str(db_path),
        'output_dir': str(output_dir),
        **{
            key: value for key, value in result.items()
            if key not in {'inventory_rows', 'replay_schedule'}
        },
    }
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0 if result['status'] == 'PASS' else 1


if __name__ == '__main__':
    raise SystemExit(main())
