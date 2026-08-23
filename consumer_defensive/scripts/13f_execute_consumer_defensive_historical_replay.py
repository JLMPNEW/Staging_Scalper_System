#!/usr/bin/env python3
# ruff: noqa: E402
"""Execute a proven Consumer Defensive SEC history plan in cutoff order."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = PACKAGE_ROOT.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from consumer_defensive.core.config import cfg_get, load_config, resolve_path
from consumer_defensive.core.db import connect, finish_run, start_run
from consumer_defensive.core.historical_filing_inventory import (
    execute_historical_filing_replay,
    load_historical_replay_plan,
)
from consumer_defensive.core.script_runtime import (
    assert_stage4_universe_ready,
    cache_only_environment,
)
from consumer_defensive.core.stage3_runtime import database_path
from consumer_defensive.core.stage4 import (
    apply_applicability,
    bootstrap_stage4,
    sync_sec_fundamentals,
)


DEFAULT_CONFIG = PACKAGE_ROOT / 'config.yaml'


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, default=DEFAULT_CONFIG)
    parser.add_argument(
        '--db', type=Path, required=True,
        help='Explicit fresh or resumable Stage 3 foundation database.',
    )
    parser.add_argument('--inventory-csv', type=Path, required=True)
    parser.add_argument('--schedule-csv', type=Path, required=True)
    parser.add_argument('--output-dir', type=Path, required=True)
    parser.add_argument(
        '--sec-cache-dir', type=Path, required=True,
        help='Explicit dedicated SEC cache root for this chronological replay.',
    )
    parser.add_argument('--cache-only', action='store_true')
    parser.add_argument('--stop-after-sequence', type=int, default=None)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    sec_cache_dir = args.sec_cache_dir.expanduser().resolve()
    sec_cache_dir.mkdir(parents=True, exist_ok=True)
    os.environ['CONSUMER_DEFENSIVE_SEC_CACHE_DIR'] = str(sec_cache_dir)
    bundle = load_config(args.config)
    db_path = database_path(bundle, args.db)
    output_dir = args.output_dir.expanduser().resolve()
    plan = load_historical_replay_plan(
        inventory_path=args.inventory_csv,
        schedule_path=args.schedule_csv,
    )
    applicability_path = resolve_path(
        cfg_get(
            bundle.payload,
            'specialized_disclosure_census.applicability_csv',
        ),
        base_dir=bundle.base_dir,
    )
    timeout = float(cfg_get(bundle.payload, 'runtime.sqlite_timeout_sec', 30.0))
    with connect(db_path, timeout_sec=timeout) as conn:
        bootstrap_stage4(conn, bundle)
        run_id = start_run(
            conn,
            run_type='consumer_defensive_stage6b_historical_replay',
            input_path=plan.schedule_path,
        )
        try:
            readiness = assert_stage4_universe_ready(conn, bundle)
            applicability = apply_applicability(conn, applicability_path)

            def sync_step(**kwargs):
                return sync_sec_fundamentals(conn, bundle, **kwargs)

            with cache_only_environment(args.cache_only):
                result = execute_historical_filing_replay(
                    conn,
                    plan,
                    output_dir=output_dir,
                    sync_step=sync_step,
                    stop_after_sequence=args.stop_after_sequence,
                )
            finish_run(
                conn,
                run_id=run_id,
                status='success' if result['status'] == 'PASS' else 'partial',
                row_count=int(result['verified_target_count']),
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
        'output_dir': str(output_dir),
        'sec_cache_dir': str(sec_cache_dir),
        'cache_only': args.cache_only,
        'readiness': readiness,
        'applicability': applicability,
        **result,
    }, indent=2, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
