#!/usr/bin/env python3
# ruff: noqa: E402
"""Hydrate and seal the Stage 6B historical SEC primary-document corpus."""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = PACKAGE_ROOT.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from consumer_defensive.core.config import cfg_get, load_config
from consumer_defensive.core.db import connect, finish_run, start_run
from consumer_defensive.core.historical_filing_inventory import (
    hydrate_historical_document_snapshot,
)
from consumer_defensive.core.market_data import write_json
from consumer_defensive.core.script_runtime import (
    cache_only_environment,
    iso_date,
)
from consumer_defensive.core.stage3_runtime import database_path
from consumer_defensive.core.stage4 import bootstrap_stage4


DEFAULT_CONFIG = PACKAGE_ROOT / 'config.yaml'


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, default=DEFAULT_CONFIG)
    parser.add_argument('--db', type=Path, required=True)
    parser.add_argument('--as-of', type=iso_date, default=date.today().isoformat())
    parser.add_argument('--inventory-run-id', type=int, required=True)
    parser.add_argument('--sec-cache-dir', type=Path, required=True)
    parser.add_argument('--output-dir', type=Path, required=True)
    parser.add_argument('--cache-only', action='store_true')
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    bundle = load_config(args.config)
    db_path = database_path(bundle, args.db)
    cache_dir = args.sec_cache_dir.expanduser().resolve()
    output_dir = args.output_dir.expanduser().resolve()
    cache_dir.mkdir(parents=True, exist_ok=True)
    output_dir.mkdir(parents=True, exist_ok=True)
    os.environ['CONSUMER_DEFENSIVE_SEC_CACHE_DIR'] = str(cache_dir)
    with connect(
        db_path,
        timeout_sec=float(cfg_get(bundle.payload, 'runtime.sqlite_timeout_sec', 30.0)),
    ) as conn:
        bootstrap_stage4(conn, bundle)
        run_id = start_run(
            conn,
            run_type='consumer_defensive_stage6b_historical_document_snapshot',
            input_path=bundle.path,
        )

        def progress(payload: dict[str, object]) -> None:
            print(json.dumps(payload, sort_keys=True), flush=True)

        try:
            with cache_only_environment(args.cache_only):
                result = hydrate_historical_document_snapshot(
                    conn,
                    bundle,
                    as_of=args.as_of,
                    inventory_run_id=args.inventory_run_id,
                    cache_dir=cache_dir,
                    cache_only=args.cache_only,
                    progress=progress,
                )
            write_json(
                output_dir / 'consumer_defensive_historical_document_snapshot.json',
                result,
            )
            finish_run(
                conn,
                run_id=run_id,
                status='success',
                row_count=int(result['document_count']),
                message=json.dumps(result, sort_keys=True),
            )
        except BaseException as exc:
            finish_run(
                conn,
                run_id=run_id,
                status='failed',
                message=f'{type(exc).__name__}: {exc}',
            )
            raise
    print(json.dumps({
        'database': str(db_path),
        'sec_cache_dir': str(cache_dir),
        'output_dir': str(output_dir),
        **result,
    }, indent=2, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
