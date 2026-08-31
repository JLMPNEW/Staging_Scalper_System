#!/usr/bin/env python3
# ruff: noqa: E402
'''Run report-only Consumer Defensive Stage 9 portfolio backtests.'''

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from urllib.parse import quote


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = PACKAGE_ROOT.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from consumer_defensive.core.config import cfg_get, load_config, resolve_path
from consumer_defensive.core.stage3_runtime import database_path
from consumer_defensive.core.stage9_backtest import run_stage9_backtest


DEFAULT_CONFIG = PACKAGE_ROOT / 'config.yaml'


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, default=DEFAULT_CONFIG)
    parser.add_argument('--db', type=Path, default=None)
    parser.add_argument('--stage8-root', type=Path, required=True)
    parser.add_argument('--factor-validation-root', type=Path, required=True)
    parser.add_argument('--output-dir', type=Path, default=None)
    return parser.parse_args()


def _connect_read_only(path: Path, *, timeout_sec: float) -> sqlite3.Connection:
    resolved = path.expanduser().resolve()
    encoded = quote(resolved.as_posix(), safe='/:')
    conn = sqlite3.connect(
        f'file:{encoded}?mode=ro',
        uri=True,
        timeout=timeout_sec,
    )
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA query_only=ON')
    conn.execute('PRAGMA foreign_keys=ON')
    return conn


def main() -> int:
    args = parse_args()
    bundle = load_config(args.config)
    db_path = database_path(bundle, args.db)
    stage8_root = args.stage8_root.expanduser().resolve()
    stage8_contract = json.loads(
        (stage8_root / 'stage8_contract.json').read_text(encoding='utf-8')
    )
    output_dir = (
        args.output_dir.expanduser().resolve()
        if args.output_dir
        else resolve_path(
            cfg_get(bundle.payload, 'paths.output_dir'),
            base_dir=bundle.base_dir,
        ) / 'stage9' / str(stage8_contract['stage6c_asof_date']) / 'v1'
    )

    def progress(position: int, total: int, candidate_id: str) -> None:
        if position == 1 or position == total or position % 10 == 0:
            print(json.dumps({
                'stage': 'stage9_portfolio_backtest',
                'completed_candidates': position,
                'total_candidates': total,
                'candidate_id': candidate_id,
            }, sort_keys=True), flush=True)

    with _connect_read_only(
        db_path,
        timeout_sec=float(cfg_get(
            bundle.payload, 'runtime.sqlite_timeout_sec', 30.0
        )),
    ) as conn:
        result = run_stage9_backtest(
            conn,
            bundle,
            stage8_root=stage8_root,
            factor_root=args.factor_validation_root,
            output_dir=output_dir,
            progress=progress,
        )
    print(json.dumps({
        'database': str(db_path),
        'database_access_mode': 'read_only',
        **result,
    }, indent=2, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
