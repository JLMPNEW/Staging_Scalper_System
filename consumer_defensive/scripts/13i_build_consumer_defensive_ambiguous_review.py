#!/usr/bin/env python3
# ruff: noqa: E402
"""Build an exact evidence-rich review pack for ambiguous Stage 6B pairs."""

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

from consumer_defensive.adapters.dedicated_parser_adapter import ADAPTER_VERSION
from consumer_defensive.core.config import cfg_get, load_config, resolve_path
from consumer_defensive.core.script_runtime import iso_date
from consumer_defensive.core.stage3_runtime import database_path
from dedicated_parser.adjudication import (
    build_ambiguous_adjudication_skeleton,
    build_ocr_adjudication_skeleton,
    write_ambiguous_adjudication_skeleton,
)
from dedicated_parser.atomic_io import atomic_write_text
from dedicated_parser.contracts import file_sha256, stable_hash
from dedicated_parser.storage import connect_database


DEFAULT_CONFIG = PACKAGE_ROOT / 'config.yaml'


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, default=DEFAULT_CONFIG)
    parser.add_argument('--db', type=Path, default=None)
    parser.add_argument('--as-of', type=iso_date, default=date.today().isoformat())
    parser.add_argument('--parser-run-id', type=int, default=0)
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
        ) / 'stage6b' / args.as_of / 'ambiguous_review'
    )
    with connect_database(db_path, readonly=True) as conn:
        run_row = conn.execute(
            '''
            SELECT run_id FROM sec_parser_run
            WHERE run_id=COALESCE(?,run_id)
              AND model_family='consumer_defensive'
              AND asof_date=? AND adapter_version=?
              AND status='COMPLETED' AND failed_work_count=0
            ORDER BY run_id DESC LIMIT 1
            ''',
            (args.parser_run_id or None, args.as_of, ADAPTER_VERSION),
        ).fetchone()
        if run_row is None:
            raise ValueError('No exact completed Consumer Defensive parser run')
        run_id = int(run_row['run_id'])
        rows = build_ambiguous_adjudication_skeleton(conn, run_id=run_id)
        ocr_rows = build_ocr_adjudication_skeleton(conn, run_id=run_id)
    if not rows:
        raise ValueError(f'parser run {run_id} has no ambiguous review evidence')
    output_dir.mkdir(parents=True, exist_ok=True)
    review_path = output_dir / 'consumer_defensive_ambiguous_review.csv'
    write_ambiguous_adjudication_skeleton(review_path, rows)
    ocr_review_path = output_dir / 'consumer_defensive_ocr_review.csv'
    if ocr_rows:
        write_ambiguous_adjudication_skeleton(ocr_review_path, ocr_rows)
    pairs = sorted({
        (str(row['ticker']), str(row['metric_name'])) for row in rows
    })
    payload = {
        'status': 'REVIEW_REQUIRED',
        'model_family': 'consumer_defensive',
        'asof_date': args.as_of,
        'base_parser_run_id': run_id,
        'adapter_version': ADAPTER_VERSION,
        'pair_count': len(pairs),
        'evidence_row_count': len(rows),
        'ticker_count': len({ticker for ticker, _ in pairs}),
        'pair_scope_sha256': stable_hash(pairs),
        'review_csv': str(review_path),
        'review_csv_sha256': file_sha256(review_path),
        'ocr_review_evidence_row_count': len(ocr_rows),
        'ocr_review_pair_count': len({
            (str(row['ticker']), str(row['metric_name'])) for row in ocr_rows
        }),
        'ocr_review_csv': str(ocr_review_path) if ocr_rows else '',
        'ocr_review_csv_sha256': (
            file_sha256(ocr_review_path) if ocr_rows else ''
        ),
        'instructions': (
            'Enable only reviewed exact evidence rows; choose ACCEPTED or '
            'REJECTED_POLICY, complete reviewer/timestamp, and use explicit '
            'period/value/scope overrides only when supported by the source.'
        ),
    }
    manifest_path = output_dir / 'consumer_defensive_ambiguous_review.json'
    atomic_write_text(
        manifest_path,
        json.dumps(payload, indent=2, sort_keys=True) + '\n',
    )
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
