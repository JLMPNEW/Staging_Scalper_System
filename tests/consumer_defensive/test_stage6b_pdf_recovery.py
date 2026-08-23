from __future__ import annotations

import csv
import sqlite3
from pathlib import Path

import pytest

from consumer_defensive.core.pdf_recovery import build_pdf_recovery_manifest
from dedicated_parser.contracts import file_sha256


FIELDS = (
    'ticker', 'accession_number', 'document_name', 'content_sha256',
    'cache_status', 'local_path', 'primary_document', 'company_currency',
    'cik', 'form_type', 'filing_date', 'accepted_at', 'report_date',
    'source_id', 'is_primary', 'is_full_submission', 'source_kind',
    'requested_metric_ids',
)


def _write_manifest(path: Path) -> None:
    rows = [
        {
            'ticker': 'KO', 'accession_number': 'a1',
            'document_name': 'earnings.pdf', 'content_sha256': 'a' * 64,
            'cache_status': 'CACHED_HASHED', 'local_path': 'sealed-a',
            'primary_document': 'filing.htm', 'company_currency': 'USD',
            'cik': '0000000001', 'form_type': '8-K',
            'filing_date': '2026-02-01',
            'accepted_at': '2026-02-01T12:00:00Z',
            'report_date': '2025-12-31', 'source_id': 'sec_submissions',
            'is_primary': '0', 'is_full_submission': '0',
            'source_kind': 'stage6b_event_sealed_cas',
            'requested_metric_ids': (
                'organic_revenue_growth_pct|price_mix_growth_pct'
            ),
        },
        {
            'ticker': 'KO', 'accession_number': 'a1',
            'document_name': 'filing.htm', 'content_sha256': 'b' * 64,
            'cache_status': 'CACHED_HASHED', 'local_path': 'sealed-b',
            'primary_document': 'filing.htm', 'company_currency': 'USD',
            'cik': '0000000001', 'form_type': '8-K',
            'filing_date': '2026-02-01',
            'accepted_at': '2026-02-01T12:00:00Z',
            'report_date': '2025-12-31', 'source_id': 'sec_submissions',
            'is_primary': '1', 'is_full_submission': '0',
            'source_kind': 'stage6b_event_sealed_cas',
            'requested_metric_ids': 'organic_revenue_growth_pct',
        },
        {
            'ticker': 'PEP', 'accession_number': 'a2',
            'document_name': 'slides.pdf', 'content_sha256': 'c' * 64,
            'cache_status': 'CACHED_HASHED', 'local_path': 'sealed-c',
            'primary_document': 'filing.htm', 'company_currency': 'USD',
            'cik': '0000000002', 'form_type': '8-K',
            'filing_date': '2026-02-02',
            'accepted_at': '2026-02-02T12:00:00Z',
            'report_date': '2025-12-31', 'source_id': 'sec_submissions',
            'is_primary': '0', 'is_full_submission': '0',
            'source_kind': 'stage6b_event_sealed_cas',
            'requested_metric_ids': 'organic_revenue_growth_pct',
        },
    ]
    with path.open('w', encoding='utf-8', newline='') as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)


def _connection(source_sha: str) -> sqlite3.Connection:
    conn = sqlite3.connect(':memory:')
    conn.row_factory = sqlite3.Row
    conn.executescript(
        '''CREATE TABLE stage6b_specialized_run(
               stage6b_run_id INTEGER PRIMARY KEY,
               asof_date TEXT NOT NULL,
               adapter_version TEXT NOT NULL,
               source_manifest_sha256 TEXT NOT NULL,
               status TEXT NOT NULL
           );
           CREATE TABLE stage6b_metric_coverage_status(
               stage6b_run_id INTEGER NOT NULL,
               scope_name TEXT NOT NULL,
               cohort_id TEXT NOT NULL,
               applicability_subtype TEXT NOT NULL,
               metric_id TEXT NOT NULL,
               evidence_state TEXT NOT NULL,
               issuer_count INTEGER NOT NULL,
               tickers_json TEXT NOT NULL
           );'''
    )
    conn.execute(
        'INSERT INTO stage6b_specialized_run VALUES(1,?,?,?,?)',
        (
            '2026-08-14', 'consumer_defensive_specialized_metrics_v3.17',
            source_sha, 'PASS',
        ),
    )
    conn.executemany(
        '''INSERT INTO stage6b_metric_coverage_status
           VALUES(1,?,?,?,?,?,?,?)''',
        [
            (
                'all_taxonomy', '*', '*', 'price_mix_growth_pct',
                'parser_failure', 1, '["KO"]',
            ),
            (
                'current_live', 'beverages', 'non_alcohol',
                'price_mix_growth_pct', 'parser_failure', 1, '["KO"]',
            ),
        ],
    )
    return conn


def test_pdf_recovery_is_sealed_failure_only_and_metric_narrowed(
    tmp_path: Path,
) -> None:
    source = tmp_path / 'source.csv'
    output = tmp_path / 'recovery.csv'
    _write_manifest(source)
    conn = _connection(file_sha256(source))
    try:
        result = build_pdf_recovery_manifest(
            conn,
            as_of='2026-08-14',
            source_manifest_path=source,
            output_path=output,
        )
    finally:
        conn.close()
    assert result['document_count'] == 1
    assert result['metric_pairs'] == [['KO', 'price_mix_growth_pct']]
    with output.open('r', encoding='utf-8', newline='') as handle:
        rows = list(csv.DictReader(handle))
    assert len(rows) == 1
    assert rows[0]['document_name'] == 'earnings.pdf'
    assert rows[0]['requested_metric_ids'] == 'price_mix_growth_pct'


def test_pdf_recovery_rejects_manifest_not_bound_to_coverage_run(
    tmp_path: Path,
) -> None:
    source = tmp_path / 'source.csv'
    _write_manifest(source)
    conn = _connection('f' * 64)
    try:
        with pytest.raises(RuntimeError, match='does not match'):
            build_pdf_recovery_manifest(
                conn,
                as_of='2026-08-14',
                source_manifest_path=source,
                output_path=tmp_path / 'recovery.csv',
            )
    finally:
        conn.close()
