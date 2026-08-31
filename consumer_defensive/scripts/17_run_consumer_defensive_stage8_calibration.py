#!/usr/bin/env python3
# ruff: noqa: E402
'''Run report-only Consumer Defensive Stage 8 constrained calibration.'''

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
from consumer_defensive.core.stage8_calibration import run_stage8_calibration


DEFAULT_CONFIG = PACKAGE_ROOT / 'config.yaml'
DEFAULT_MARKET_POLICY = (
    PACKAGE_ROOT / 'data' / 'consumer_defensive_market_data_policy.yaml'
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, default=DEFAULT_CONFIG)
    parser.add_argument('--db', type=Path, default=None)
    parser.add_argument('--stage6c-run-id', type=int, required=True)
    parser.add_argument(
        '--factor-validation-root', type=Path, required=True
    )
    parser.add_argument(
        '--market-policy', type=Path, default=DEFAULT_MARKET_POLICY
    )
    parser.add_argument('--output-dir', type=Path, default=None)
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
        stage6c = conn.execute(
            '''SELECT asof_date FROM stage6c_panel_run
               WHERE stage6c_run_id=?''',
            (args.stage6c_run_id,),
        ).fetchone()
        if stage6c is None:
            raise RuntimeError(
                f'Unknown Stage 6C run ID: {args.stage6c_run_id}'
            )
        output_dir = (
            args.output_dir.expanduser().resolve()
            if args.output_dir
            else resolve_path(
                cfg_get(bundle.payload, 'paths.output_dir'),
                base_dir=bundle.base_dir,
            ) / 'stage8' / str(stage6c['asof_date']) / 'v1'
        )

        def progress(position: int, total: int, as_of: str) -> None:
            if position == 1 or position == total or position % 5 == 0:
                print(json.dumps({
                    'stage': 'stage8_historical_core_panel',
                    'completed_dates': position,
                    'total_dates': total,
                    'asof_date': as_of,
                }, sort_keys=True), flush=True)

        result = run_stage8_calibration(
            conn,
            bundle,
            stage6c_run_id=args.stage6c_run_id,
            factor_root=args.factor_validation_root,
            market_policy_path=args.market_policy,
            output_dir=output_dir,
            progress=progress,
        )
    print(json.dumps({
        'database': str(db_path),
        **result,
    }, indent=2, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
