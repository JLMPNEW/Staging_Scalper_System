#!/usr/bin/env python3
# ruff: noqa: E402
"""Audit Stage 6B recall against reviewed issuer/metric expectation matrices."""

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
from consumer_defensive.core.db import connect
from consumer_defensive.core.script_runtime import iso_date
from consumer_defensive.core.specialized_recall import (
    audit_specialized_recall,
    write_specialized_recall_artifacts,
)
from consumer_defensive.core.stage3_runtime import database_path


DEFAULT_CONFIG = PACKAGE_ROOT / 'config.yaml'


def _matrix_spec(value: str) -> tuple[str, Path]:
    cohort, separator, path = value.partition('=')
    if not separator or not cohort.strip() or not path.strip():
        raise argparse.ArgumentTypeError(
            '--matrix must be COHORT_ID=PATH'
        )
    return cohort.strip(), Path(path.strip()).expanduser().resolve(strict=True)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, default=DEFAULT_CONFIG)
    parser.add_argument('--db', type=Path, default=None)
    parser.add_argument('--as-of', type=iso_date, default=date.today().isoformat())
    parser.add_argument(
        '--matrix',
        type=_matrix_spec,
        action='append',
        required=True,
        help='Reviewed matrix as COHORT_ID=PATH; repeat for each cohort.',
    )
    parser.add_argument('--stage6b-run-id', type=int, default=0)
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
        ) / 'stage6b' / args.as_of / 'recall_benchmark'
    )
    with connect(
        db_path,
        timeout_sec=float(
            cfg_get(bundle.payload, 'runtime.sqlite_timeout_sec', 30.0)
        ),
    ) as conn:
        result = audit_specialized_recall(
            conn,
            bundle,
            as_of=args.as_of,
            matrices=args.matrix,
            stage6b_run_id=args.stage6b_run_id or None,
        )
        write_specialized_recall_artifacts(result, output_dir=output_dir)
    payload = {
        key: value for key, value in result.items() if key != 'rows'
    }
    payload.update({'database': str(db_path), 'output_dir': str(output_dir)})
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
