#!/usr/bin/env python3
# ruff: noqa: E402
"""Run Consumer Defensive Stage 6B shadow extraction from its exact manifest."""

from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import date
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = PACKAGE_ROOT.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from consumer_defensive.core.config import cfg_get, load_config, resolve_path
from consumer_defensive.core.script_runtime import iso_date
from consumer_defensive.core.stage3_runtime import database_path
from dedicated_parser.cli import main as parser_main


DEFAULT_CONFIG = PACKAGE_ROOT / 'config.yaml'


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, default=DEFAULT_CONFIG)
    parser.add_argument('--db', type=Path, default=None)
    parser.add_argument('--as-of', type=iso_date, default=date.today().isoformat())
    parser.add_argument('--cache-dir', type=Path, default=None)
    parser.add_argument('--source-manifest', type=Path, default=None)
    parser.add_argument('--output-dir', type=Path, default=None)
    parser.add_argument('--workers', type=int, default=4)
    parser.add_argument('--force', action='store_true')
    parser.add_argument('--plan-only', action='store_true')
    parser.add_argument('--enable-pdf-ocr', action='store_true')
    parser.add_argument('--ocr-max-pages', type=int, default=None)
    parser.add_argument('--ocr-dpi', type=int, default=None)
    parser.add_argument('--ocr-page-timeout-seconds', type=float, default=None)
    parser.add_argument('--ocr-max-pixels-per-page', type=int, default=None)
    parser.add_argument('--ocr-total-timeout-seconds', type=float, default=None)
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
    if not manifest.is_file():
        raise FileNotFoundError(
            f'Stage 6B source manifest is missing; run script 13 first: {manifest}'
        )
    with sqlite3.connect(f'file:{db_path}?mode=ro', uri=True) as conn:
        row = conn.execute(
            '''SELECT ingestion_config_sha256
               FROM consumer_defensive_sec_reconciliation_state
               WHERE asof_date=? AND status='complete'
                 AND trust_state='trusted_current' ''',
            (args.as_of,),
        ).fetchone()
    if row is None:
        raise RuntimeError('Stage 6B shadow run requires a trusted reconciliation.')
    argv = [
        '--db', str(db_path),
        '--cache-dir', str(cache_dir),
        '--asof', args.as_of,
        '--adapter', str(cfg_get(bundle.payload, 'stage6b.adapter_path')),
        '--source-manifest', str(manifest),
        '--workers', str(args.workers),
        '--max-filings-per-ticker', str(
            cfg_get(bundle.payload, 'stage6b.maximum_filings_per_ticker')
        ),
        '--max-documents-per-filing', str(
            cfg_get(bundle.payload, 'stage6b.maximum_documents_per_filing')
        ),
        '--max-pdf-pages', str(
            cfg_get(bundle.payload, 'stage6b.maximum_pdf_pages')
        ),
        '--max-pdf-bytes', str(
            cfg_get(bundle.payload, 'stage6b.maximum_pdf_bytes')
        ),
        '--pdf-extraction-timeout-seconds', str(
            args.ocr_total_timeout_seconds
            if args.ocr_total_timeout_seconds is not None
            else cfg_get(
                bundle.payload, 'stage6b.pdf_extraction_timeout_seconds'
            )
        ),
        '--max-ocr-pages', str(
            args.ocr_max_pages
            if args.ocr_max_pages is not None
            else cfg_get(bundle.payload, 'stage6b.maximum_ocr_pages')
        ),
        '--ocr-dpi', str(
            args.ocr_dpi
            if args.ocr_dpi is not None
            else cfg_get(bundle.payload, 'stage6b.ocr_dpi')
        ),
        '--ocr-page-timeout-seconds', str(
            args.ocr_page_timeout_seconds
            if args.ocr_page_timeout_seconds is not None
            else cfg_get(bundle.payload, 'stage6b.ocr_page_timeout_seconds')
        ),
        '--max-ocr-pixels-per-page', str(
            args.ocr_max_pixels_per_page
            if args.ocr_max_pixels_per_page is not None
            else cfg_get(
                bundle.payload, 'stage6b.maximum_ocr_pixels_per_page'
            )
        ),
        '--expected-ingestion-config-sha256', str(row[0]),
        '--require-complete-cache',
        '--all-metrics',
        '--disable-arelle',
        '--disable-edgartools',
        '--skip-adjudication-skeleton',
        '--output-json', str(output_dir / 'dedicated_parser_run.json'),
        '--cache-gate-output-json', str(output_dir / 'parser_cache_gate.json'),
        '--provider-state-dir', str(output_dir / 'provider_state'),
    ]
    if args.enable_pdf_ocr or bool(
        cfg_get(bundle.payload, 'stage6b.enable_pdf_ocr')
    ):
        argv.append('--enable-pdf-ocr')
    if args.force:
        argv.append('--force')
    if args.plan_only:
        argv.append('--plan-only')
    return parser_main(argv)


if __name__ == '__main__':
    raise SystemExit(main())
