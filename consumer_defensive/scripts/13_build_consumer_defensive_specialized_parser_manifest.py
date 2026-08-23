#!/usr/bin/env python3
# ruff: noqa: E402
"""Build the exact-seal Consumer Defensive Stage 6B parser manifest."""

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
from consumer_defensive.core.market_data import write_json
from consumer_defensive.core.script_runtime import iso_date
from consumer_defensive.core.specialized_metrics import (
    build_stage6b_source_manifest,
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
    manifest_path = output_dir / 'consumer_defensive_stage6b_source_manifest.csv'
    with connect(
        db_path,
        timeout_sec=float(cfg_get(bundle.payload, 'runtime.sqlite_timeout_sec', 30.0)),
    ) as conn:
        run_id = start_run(
            conn,
            run_type='consumer_defensive_stage6b_manifest_build',
            input_path=bundle.path,
        )
        try:
            result = build_stage6b_source_manifest(
                conn,
                bundle,
                as_of=args.as_of,
                cache_dir=cache_dir,
                output_path=manifest_path,
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
    payload = {'database': str(db_path), 'cache_dir': str(cache_dir), **result}
    write_json(output_dir / 'stage6b_manifest_build.json', payload)
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
