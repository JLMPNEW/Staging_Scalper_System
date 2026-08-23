from __future__ import annotations

import csv
import re
import sqlite3
from pathlib import Path
from typing import Any, Iterable

from .market_data import write_csv, write_json
from .specialized_metrics import (
    _run_observations,
    _taxonomy,
    bootstrap_stage6b,
)
from .config import ConfigBundle


MATRIX_VERSION = 'consumer_defensive_reviewed_expectation_matrix_v1'

_LABEL_TO_METRIC = {
    'active customer growth': 'active_customer_growth_pct',
    'active representative growth': 'active_representative_growth_pct',
    'advertising promotion as percent of sales': (
        'advertising_promotion_pct_sales'
    ),
    'agricultural processing margin': 'agricultural_processing_margin',
    'alcohol depletion growth': 'alcohol_depletion_growth_pct',
    'average ticket growth': 'average_ticket_growth_pct',
    'branded sales mix': 'branded_sales_mix_pct',
    'capacity utilization': 'capacity_utilization_pct',
    'case volume growth': 'case_volume_growth_pct',
    'commodity cost impact': 'commodity_cost_impact_bps',
    'comparable sales growth': 'comparable_sales_growth_pct',
    'digital sales mix': 'digital_sales_mix_pct',
    'distribution points growth': 'distribution_points_growth_pct',
    'excise tax impact': 'excise_tax_impact_bps',
    'fixed charge coverage': 'fixed_charge_coverage',
    'gross margin change': 'gross_margin_change_bps',
    'gross profit per case': 'gross_profit_per_case',
    'independent customer mix': 'independent_customer_mix_pct',
    'innovation sales mix': 'innovation_sales_mix_pct',
    'inventory turnover': 'inventory_turnover',
    'lease adjusted net leverage': 'lease_adjusted_net_leverage',
    'livestock feed cost change': 'livestock_feed_cost_change_pct',
    'market share change': 'market_share_change_bps',
    'net debt ebitda': 'net_debt_to_ebitda',
    'net store growth': 'net_store_growth_pct',
    'non alcohol unit case growth': 'non_alcohol_unit_case_growth_pct',
    'organic revenue growth': 'organic_revenue_growth_pct',
    'price mix growth': 'price_mix_growth_pct',
    'private label sales mix': 'private_label_sales_mix_pct',
    'production volume growth': 'production_volume_growth_pct',
    'reduced risk sales mix': 'reduced_risk_sales_mix_pct',
    'revenue per unit growth': 'revenue_per_unit_growth_pct',
    'sales per square foot': 'sales_per_square_foot',
    'shrink change': 'shrink_change_bps',
    'tobacco price mix growth': 'tobacco_price_mix_growth_pct',
    'tobacco shipment volume growth': 'tobacco_shipment_volume_growth_pct',
    'traffic growth': 'traffic_growth_pct',
    'volume growth': 'volume_growth_pct',
}


def _normalized_label(value: str) -> str:
    without_parenthetical = re.sub(r'\([^)]*\)', '', value)
    normalized = re.sub(
        r'[^a-z0-9]+',
        ' ',
        without_parenthetical.casefold().replace('%', ' percent '),
    )
    normalized = re.sub(r'\bderived\b', '', normalized)
    return re.sub(r'\s+', ' ', normalized).strip()


def _metric_from_label(label: str) -> str | None:
    normalized = _normalized_label(label)
    if normalized in _LABEL_TO_METRIC:
        return _LABEL_TO_METRIC[normalized]
    candidates = [
        (len(alias), metric_id)
        for alias, metric_id in _LABEL_TO_METRIC.items()
        if normalized.startswith(alias + ' ')
    ]
    return max(candidates)[1] if candidates else None


def _matrix_column(fieldnames: Iterable[str], prefix: str) -> str:
    matches = [
        value for value in fieldnames
        if str(value).strip().casefold().startswith(prefix.casefold())
    ]
    if len(matches) != 1:
        raise ValueError(
            f'Reviewed expectation matrix requires one {prefix!r} column.'
        )
    return matches[0]


def load_reviewed_expectation_matrix(
    path: Path,
    *,
    cohort_id: str,
) -> list[dict[str, str]]:
    resolved = path.expanduser().resolve(strict=True)
    with resolved.open(encoding='utf-8-sig', newline='') as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames or 'Ticker' not in reader.fieldnames:
            raise ValueError('Reviewed expectation matrix requires Ticker.')
        available_column = _matrix_column(
            reader.fieldnames,
            'Metrics Available via SEC Filings',
        )
        excluded_column = _matrix_column(
            reader.fieldnames,
            'Non-SEC Excluded Metrics',
        )
        rows: list[dict[str, str]] = []
        seen: set[tuple[str, str]] = set()
        for source_row in reader:
            ticker = str(source_row.get('Ticker') or '').strip().upper()
            if not ticker:
                raise ValueError('Reviewed expectation matrix has a blank ticker.')
            available = [
                value.strip()
                for value in str(source_row.get(available_column) or '').split(',')
                if value.strip()
            ]
            excluded_text = str(source_row.get(excluded_column) or '')
            excluded = [
                label
                for label in _LABEL_TO_METRIC
                if re.search(
                    r'(?<![a-z0-9])' + re.escape(label) + r'(?![a-z0-9])',
                    _normalized_label(excluded_text),
                )
            ]
            for raw_label, expectation_class in [
                *((value, 'sec_expected') for value in available),
                *((value, 'non_sec_excluded') for value in excluded),
            ]:
                metric_id = _metric_from_label(raw_label)
                if metric_id is None:
                    raise ValueError(
                        f'Unknown reviewed specialized metric label: {raw_label!r}'
                    )
                key = (ticker, metric_id)
                if key in seen:
                    continue
                seen.add(key)
                rows.append({
                    'matrix_version': MATRIX_VERSION,
                    'source_matrix': str(resolved),
                    'ticker': ticker,
                    'cohort_id': cohort_id,
                    'metric_id': metric_id,
                    'expectation_class': expectation_class,
                    'source_label': raw_label,
                })
    if not rows:
        raise ValueError('Reviewed expectation matrix contains no mapped metrics.')
    return sorted(rows, key=lambda row: (
        row['ticker'], row['metric_id'], row['expectation_class']
    ))


def audit_specialized_recall(
    conn: sqlite3.Connection,
    bundle: ConfigBundle,
    *,
    as_of: str,
    matrices: Iterable[tuple[str, Path]],
    stage6b_run_id: int | None = None,
) -> dict[str, Any]:
    bootstrap_stage6b(conn, bundle)
    taxonomy = _taxonomy(conn)
    matrix_specs = list(matrices)
    normalized: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for cohort_id, path in matrix_specs:
        for row in load_reviewed_expectation_matrix(path, cohort_id=cohort_id):
            ticker = row['ticker']
            if ticker not in taxonomy:
                raise ValueError(
                    f'Reviewed expectation ticker is outside taxonomy: {ticker}'
                )
            if taxonomy[ticker]['cohort_id'] != cohort_id:
                raise ValueError(
                    f'Reviewed expectation cohort mismatch for {ticker}: '
                    f'{cohort_id} != {taxonomy[ticker]["cohort_id"]}'
                )
            key = (ticker, row['metric_id'])
            if key in seen:
                raise ValueError(
                    f'Duplicate reviewed expectation across matrices: {key}'
                )
            seen.add(key)
            normalized.append(row)
    run = conn.execute(
        '''
        SELECT * FROM stage6b_specialized_run
        WHERE stage6b_run_id=COALESCE(?,stage6b_run_id)
          AND asof_date=? AND status='measurement_only_complete'
        ORDER BY stage6b_run_id DESC LIMIT 1
        ''',
        (stage6b_run_id, as_of),
    ).fetchone()
    if run is None:
        raise RuntimeError('Recall audit requires a completed Stage 6B run.')
    measured = {
        (str(row['ticker']), str(row['metric_id']))
        for row in _run_observations(conn, as_of=as_of, run=run)
        if str(row['evidence_status']) == 'accepted_measurement_only'
        and row['numeric_value'] is not None
    }
    inventoried = {
        str(row[0])
        for row in conn.execute(
            '''
            SELECT DISTINCT ticker FROM stage6b_document_inventory
            WHERE asof_date=? AND inventory_status='sealed_current_snapshot'
            ''',
            (as_of,),
        )
    }
    parser_status: dict[tuple[str, str], set[str]] = {}
    if run['parser_run_id'] is not None:
        for row in conn.execute(
            '''
            SELECT e.ticker,e.metric_name,e.candidate_status
            FROM sec_parser_run_metric_evidence r
            JOIN sec_parser_metric_evidence_shadow e
              ON e.evidence_key=r.evidence_key
            WHERE r.run_id=?
            ''',
            (int(run['parser_run_id']),),
        ):
            parser_status.setdefault(
                (str(row['ticker']), str(row['metric_name'])),
                set(),
            ).add(str(row['candidate_status']))
    audited: list[dict[str, Any]] = []
    for row in normalized:
        key = (row['ticker'], row['metric_id'])
        statuses = parser_status.get(key, set())
        if row['expectation_class'] == 'non_sec_excluded':
            result = (
                'unexpected_sec_measurement'
                if key in measured else 'excluded_from_sec_recall_denominator'
            )
        elif key in measured:
            result = 'measured'
        elif 'REVIEW_REQUIRED' in statuses:
            result = 'review_required'
        elif statuses:
            result = 'parser_rejected_or_suppressed'
        elif row['ticker'] not in inventoried:
            result = 'missing_sealed_document'
        else:
            result = 'not_found_in_current_document'
        audited.append({
            **row,
            'stage6b_run_id': int(run['stage6b_run_id']),
            'asof_date': as_of,
            'recall_result': result,
            'parser_statuses': '|'.join(sorted(statuses)),
        })
    expected = [
        row for row in audited
        if row['expectation_class'] == 'sec_expected'
    ]
    measured_expected = [
        row for row in expected if row['recall_result'] == 'measured'
    ]
    return {
        'status': 'PASS',
        'matrix_version': MATRIX_VERSION,
        'stage6b_run_id': int(run['stage6b_run_id']),
        'asof_date': as_of,
        'matrix_count': len(matrix_specs),
        'reviewed_pair_count': len(audited),
        'sec_expected_pair_count': len(expected),
        'measured_expected_pair_count': len(measured_expected),
        'reviewed_sec_recall': (
            len(measured_expected) / len(expected) if expected else None
        ),
        'non_sec_excluded_pair_count': sum(
            row['expectation_class'] == 'non_sec_excluded'
            for row in audited
        ),
        'result_counts': {
            status: sum(row['recall_result'] == status for row in audited)
            for status in sorted({row['recall_result'] for row in audited})
        },
        'rows': audited,
    }


def write_specialized_recall_artifacts(
    result: dict[str, Any],
    *,
    output_dir: Path,
) -> None:
    write_csv(
        output_dir / 'specialized_reviewed_expectation_recall.csv',
        result['rows'],
    )
    write_json(
        output_dir / 'specialized_reviewed_expectation_recall.json',
        {key: value for key, value in result.items() if key != 'rows'},
    )
