#!/usr/bin/env python3
# ruff: noqa: E402
"""Build measurement-only Stage 6B observations and coverage diagnostics."""

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
    apply_stage6b_measurement_overlays,
    build_stage6b_coverage,
    promote_stage6b_measurements,
    validate_stage6b,
    write_stage6b_reports,
)
from consumer_defensive.core.stage3_runtime import database_path


DEFAULT_CONFIG = PACKAGE_ROOT / 'config.yaml'


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, default=DEFAULT_CONFIG)
    parser.add_argument('--db', type=Path, default=None)
    parser.add_argument('--as-of', type=iso_date, default=date.today().isoformat())
    parser.add_argument('--cache-dir', type=Path, default=None)
    parser.add_argument('--source-manifest', type=Path, default=None)
    parser.add_argument('--output-dir', type=Path, default=None)
    parser.add_argument('--parser-run-id', type=int, default=0)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
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
    manifest = (
        args.source_manifest.expanduser().resolve()
        if args.source_manifest
        else output_dir / 'consumer_defensive_stage6b_source_manifest.csv'
    )
    with connect(
        db_path,
        timeout_sec=float(cfg_get(bundle.payload, 'runtime.sqlite_timeout_sec', 30.0)),
    ) as conn:
        run_id = start_run(
            conn,
            run_type='consumer_defensive_stage6b_measurement_build',
            input_path=bundle.path,
        )
        try:
            promotion = promote_stage6b_measurements(
                conn,
                bundle,
                as_of=args.as_of,
                source_manifest_path=manifest,
                parser_run_id=args.parser_run_id or None,
                minimum_confidence=float(
                    cfg_get(bundle.payload, 'stage6b.minimum_parser_confidence')
                ),
            )
            coverage = build_stage6b_coverage(
                conn,
                bundle,
                as_of=args.as_of,
                stage6b_run_id=int(promotion['stage6b_run_id']),
            )
            overlay = apply_stage6b_measurement_overlays(conn, as_of=args.as_of)
            validation = validate_stage6b(
                conn,
                bundle,
                as_of=args.as_of,
                cache_dir=cache_dir,
                stage6b_run_id=int(promotion['stage6b_run_id']),
            )
            status = 'success' if validation['code_status'] == 'PASS' else 'failed'
            finish_run(
                conn,
                run_id=run_id,
                status=status,
                row_count=int(promotion['accepted_observation_count']),
                message=json.dumps({
                    'promotion': promotion,
                    'overlay': overlay,
                    'validation': validation['summary'],
                }, sort_keys=True),
            )
            write_stage6b_reports(
                conn,
                as_of=args.as_of,
                stage6b_run_id=int(promotion['stage6b_run_id']),
                output_dir=output_dir,
                validation=validation,
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
        'promotion': promotion,
        'coverage_summary': {
            key: value for key, value in coverage.items()
            if key not in {'coverage_rows', 'overall_rows'}
        },
        'overlay': overlay,
        'validation': validation,
    }
    write_json(output_dir / 'stage6b_measurement_build.json', payload)
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0 if validation['code_status'] == 'PASS' else 1


if __name__ == '__main__':
    raise SystemExit(main())
