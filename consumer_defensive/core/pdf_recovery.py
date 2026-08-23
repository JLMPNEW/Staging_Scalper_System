'''Failure-only Stage 6B PDF/OCR recovery manifest construction.'''

from __future__ import annotations

import csv
import json
import sqlite3
from collections import defaultdict
from pathlib import Path
from typing import Any

from dedicated_parser.contracts import file_sha256

from .atomic_io import atomic_text_writer


_REQUIRED_COLUMNS = {
    'ticker', 'accession_number', 'document_name', 'content_sha256',
    'cache_status', 'local_path', 'requested_metric_ids',
}


def _failed_metric_scope(
    conn: sqlite3.Connection,
    *,
    stage6b_run_id: int,
) -> dict[str, set[str]]:
    failed: dict[str, set[str]] = defaultdict(set)
    for row in conn.execute(
        '''SELECT metric_id,tickers_json
           FROM stage6b_metric_coverage_status
           WHERE stage6b_run_id=? AND evidence_state='parser_failure'
             AND issuer_count>0
           ORDER BY metric_id,scope_name,cohort_id,applicability_subtype''',
        (stage6b_run_id,),
    ):
        raw_tickers = json.loads(str(row['tickers_json']))
        if not isinstance(raw_tickers, list):
            raise RuntimeError('Stage 6B parser-failure tickers are malformed.')
        for ticker in raw_tickers:
            normalized = str(ticker).strip().upper()
            if normalized:
                failed[normalized].add(str(row['metric_id']))
    return dict(failed)


def build_pdf_recovery_manifest(
    conn: sqlite3.Connection,
    *,
    as_of: str,
    source_manifest_path: Path,
    output_path: Path,
    stage6b_run_id: int | None = None,
) -> dict[str, Any]:
    '''Write the exact PDF subset for unresolved parser-failure pairs.

    This path never discovers new files and never reads mutable SEC aliases.
    It narrows an already sealed Stage 6B manifest to PDF documents whose
    ticker/metric pairs were explicitly classified as parser failures.
    '''

    source = source_manifest_path.expanduser().resolve()
    if not source.is_file():
        raise FileNotFoundError(f'Stage 6B source manifest is missing: {source}')
    run = conn.execute(
        '''SELECT stage6b_run_id,adapter_version,source_manifest_sha256,status
           FROM stage6b_specialized_run
           WHERE asof_date=? AND stage6b_run_id=COALESCE(?,stage6b_run_id)
             AND status='PASS'
           ORDER BY stage6b_run_id DESC LIMIT 1''',
        (as_of, stage6b_run_id),
    ).fetchone()
    if run is None:
        raise RuntimeError('No passing Stage 6B coverage run exists for recovery.')
    source_sha = file_sha256(source)
    if source_sha != str(run['source_manifest_sha256']):
        raise RuntimeError(
            'PDF recovery source manifest does not match the selected '
            'Stage 6B run seal.'
        )
    failed = _failed_metric_scope(
        conn, stage6b_run_id=int(run['stage6b_run_id'])
    )
    if not failed:
        raise RuntimeError('Selected Stage 6B run has no parser-failure pairs.')

    selected: list[dict[str, str]] = []
    with source.open('r', encoding='utf-8-sig', newline='') as handle:
        reader = csv.DictReader(handle)
        fieldnames = list(reader.fieldnames or ())
        missing = sorted(_REQUIRED_COLUMNS - set(fieldnames))
        if missing:
            raise RuntimeError(
                f'Stage 6B source manifest lacks recovery columns: {missing}'
            )
        for raw in reader:
            row = {key: str(value or '') for key, value in raw.items()}
            ticker = row['ticker'].strip().upper()
            if ticker not in failed:
                continue
            document = row['document_name'].strip()
            if Path(document).suffix.casefold() != '.pdf':
                continue
            requested = {
                value.strip()
                for value in row['requested_metric_ids'].split('|')
                if value.strip()
            }
            target = sorted(requested & failed[ticker])
            if not target:
                continue
            row['requested_metric_ids'] = '|'.join(target)
            selected.append(row)
    if not selected:
        raise RuntimeError(
            'Parser-failure pairs have no sealed PDF documents in the '
            'selected source manifest.'
        )
    selected.sort(key=lambda row: (
        row['ticker'], row['accession_number'], row['document_name'].casefold(),
    ))
    with atomic_text_writer(
        output_path, encoding='utf-8', newline='',
    ) as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(selected)
    pairs = sorted({
        (row['ticker'], metric_id)
        for row in selected
        for metric_id in row['requested_metric_ids'].split('|')
    })
    return {
        'status': 'PASS',
        'asof_date': as_of,
        'source_stage6b_run_id': int(run['stage6b_run_id']),
        'source_adapter_version': str(run['adapter_version']),
        'source_manifest': str(source),
        'source_manifest_sha256': source_sha,
        'recovery_manifest': str(output_path.resolve()),
        'recovery_manifest_sha256': file_sha256(output_path),
        'document_count': len(selected),
        'ticker_count': len({row['ticker'] for row in selected}),
        'metric_pair_count': len(pairs),
        'metric_pairs': [list(pair) for pair in pairs],
        'ocr_policy': 'bounded_failure_only_review_required',
    }
