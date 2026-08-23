#!/usr/bin/env python3
# ruff: noqa: E402
'''Build a sealed, parser-failure-only Stage 6B PDF recovery manifest.'''

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
from consumer_defensive.core.market_data import write_json
from consumer_defensive.core.pdf_recovery import build_pdf_recovery_manifest
from consumer_defensive.core.script_runtime import iso_date
from consumer_defensive.core.stage3_runtime import database_path


DEFAULT_CONFIG = PACKAGE_ROOT / 'config.yaml'


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, default=DEFAULT_CONFIG)
    parser.add_argument('--db', type=Path, default=None)
    parser.add_argument('--as-of', type=iso_date, default=date.today().isoformat())
    parser.add_argument('--stage6b-run-id', type=int, default=0)
    parser.add_argument('--source-manifest', type=Path, default=None)
    parser.add_argument('--output-dir', type=Path, default=None)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    bundle = load_config(args.config)
    db_path = database_path(bundle, args.db)
    output_dir = (
        args.output_dir.expanduser().resolve()
        if args.output_dir
        else resolve_path(
            cfg_get(bundle.payload, 'paths.output_dir'),
            base_dir=bundle.base_dir,
        ) / 'stage6b' / args.as_of / 'pdf_recovery'
    )
    source_manifest = (
        args.source_manifest.expanduser().resolve()
        if args.source_manifest
        else output_dir.parent / 'consumer_defensive_stage6b_source_manifest.csv'
    )
    recovery_manifest = output_dir / 'stage6b_pdf_recovery_manifest.csv'
    with connect(
        db_path,
        timeout_sec=float(cfg_get(bundle.payload, 'runtime.sqlite_timeout_sec')),
    ) as conn:
        result = build_pdf_recovery_manifest(
            conn,
            as_of=args.as_of,
            source_manifest_path=source_manifest,
            output_path=recovery_manifest,
            stage6b_run_id=args.stage6b_run_id or None,
        )
    result['database'] = str(db_path)
    write_json(output_dir / 'stage6b_pdf_recovery_manifest.json', result)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
