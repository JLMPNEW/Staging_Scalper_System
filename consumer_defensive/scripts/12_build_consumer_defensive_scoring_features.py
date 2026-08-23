#!/usr/bin/env python3
# ruff: noqa: E402
"""Build Consumer Defensive Stage 6A PIT atomic scoring inputs."""

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
from consumer_defensive.core.market_data import write_csv, write_json
from consumer_defensive.core.scoring_features import (
    bootstrap_stage6a,
    build_scoring_features,
    validate_scoring_features,
)
from consumer_defensive.core.script_runtime import iso_date
from consumer_defensive.core.stage3_runtime import database_path

DEFAULT_CONFIG = PACKAGE_ROOT / 'config.yaml'


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, default=DEFAULT_CONFIG)
    parser.add_argument('--db', type=Path, default=None)
    parser.add_argument('--as-of', type=iso_date, default=date.today().isoformat())
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
            cfg_get(bundle.payload, 'paths.output_dir'), base_dir=bundle.base_dir
        ) / 'stage6a' / args.as_of
    )
    with connect(
        db_path,
        timeout_sec=float(cfg_get(bundle.payload, 'runtime.sqlite_timeout_sec', 30.0)),
    ) as conn:
        bootstrap_stage6a(conn, bundle)
        run_id = start_run(
            conn,
            run_type='consumer_defensive_stage6a_scoring_feature_build',
            input_path=bundle.path,
        )
        try:
            build = build_scoring_features(conn, bundle, as_of=args.as_of)
            validation = validate_scoring_features(conn, bundle, as_of=args.as_of)
            input_rows = [
                dict(row)
                for row in conn.execute(
                    """SELECT * FROM feature_scoring_input
                       WHERE model_family='consumer_defensive' AND asof_date=?
                       ORDER BY ticker""",
                    (args.as_of,),
                )
            ]
            component_rows = [
                dict(row)
                for row in conn.execute(
                    """SELECT * FROM feature_scoring_component
                       WHERE model_family='consumer_defensive' AND asof_date=?
                       ORDER BY ticker,component_name""",
                    (args.as_of,),
                )
            ]
            status = 'success' if validation['status'] == 'PASS' else 'failed'
            finish_run(
                conn,
                run_id=run_id,
                status=status,
                row_count=len(component_rows),
                message=json.dumps(
                    {'build': build, 'validation': validation['summary']},
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
        'build': build,
        'validation': validation,
    }
    write_csv(output_dir / 'scoring_feature_inputs.csv', input_rows)
    write_csv(output_dir / 'scoring_feature_components.csv', component_rows)
    write_json(output_dir / 'scoring_feature_build.json', payload)
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0 if validation['status'] == 'PASS' else 1


if __name__ == '__main__':
    raise SystemExit(main())
