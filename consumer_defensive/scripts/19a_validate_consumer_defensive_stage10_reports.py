#!/usr/bin/env python3
# ruff: noqa: E402
'''Validate Stage 10 reports and update latest only after a full pass.'''

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

from consumer_defensive.core.config import cfg_get, load_config
from consumer_defensive.core.stage3_runtime import database_path
from consumer_defensive.core.stage10_publishing import (
    stage10_policy,
    validate_stage10_artifacts,
    write_stage10_validation,
)


DEFAULT_CONFIG = PACKAGE_ROOT / 'config.yaml'


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, default=DEFAULT_CONFIG)
    parser.add_argument('--db', type=Path, default=None)
    parser.add_argument('--as-of', required=True)
    parser.add_argument('--stage9-root', type=Path, required=True)
    parser.add_argument('--stage8-root', type=Path, required=True)
    parser.add_argument('--factor-validation-root', type=Path, required=True)
    parser.add_argument('--output-root', type=Path, required=True)
    return parser.parse_args()


def _connect_read_only(path: Path, *, timeout_sec: float) -> sqlite3.Connection:
    resolved = path.expanduser().resolve()
    encoded = quote(resolved.as_posix(), safe='/:')
    conn = sqlite3.connect(
        f'file:{encoded}?mode=ro', uri=True, timeout=timeout_sec,
    )
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA query_only=ON')
    conn.execute('PRAGMA foreign_keys=ON')
    return conn


def main() -> int:
    args = parse_args()
    bundle = load_config(args.config)
    db_path = database_path(bundle, args.db).expanduser().resolve()
    output_root = args.output_root.expanduser().resolve()
    output_dir = (
        output_root / args.as_of / stage10_policy(bundle)['output_version']
    )
    with _connect_read_only(
        db_path,
        timeout_sec=float(cfg_get(
            bundle.payload, 'runtime.sqlite_timeout_sec', 30.0
        )),
    ) as conn:
        result = validate_stage10_artifacts(
            conn, bundle, asof_date=args.as_of, database_path=db_path,
            stage9_root=args.stage9_root, stage8_root=args.stage8_root,
            factor_root=args.factor_validation_root, output_dir=output_dir,
        )
    latest = write_stage10_validation(output_dir, output_root, result)
    result = {**result, **latest}
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result['status'] == 'PASS' else 1


if __name__ == '__main__':
    raise SystemExit(main())
