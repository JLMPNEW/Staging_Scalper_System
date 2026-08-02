#!/usr/bin/env python3
"""Capture exact quarterly report-date to fiscal-period mappings from Alpha."""

from __future__ import annotations

import argparse
import csv
import math
import sys
import tempfile
import time
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from portfolio_layer.core.config import cfg_get, load_yaml, resolve_path  # noqa: E402
from portfolio_layer.core.contracts import sha256_file, write_csv, write_manifest  # noqa: E402
from portfolio_layer.core.paths import ensure_not_prod_path, resolve_runtime_paths  # noqa: E402
from portfolio_layer.expectations_monitor.monitor_common import (  # noqa: E402
    ACTUAL_OUTCOME_VALUE_FIELDS,
    FISCAL_PERIOD_RESOLUTION_FIELDS,
    append_actual_outcomes,
    append_fiscal_period_resolutions,
    connect_monitor_db,
    writer_lock,
)
from portfolio_layer.expectations_monitor.provider_common import (  # noqa: E402
    ProviderPayloadResult,
    fetch_capability_payload,
    load_entitlements,
)


DEFAULT_CONFIG = PACKAGE_ROOT / 'config.yaml'
DEFAULT_ENTITLEMENTS = Path(__file__).with_name('provider_entitlements.yaml')
CAPTURE_FIELDS = [
    'provider',
    'endpoint_id',
    'symbol',
    'status',
    'http_status',
    'elapsed_ms',
    'provider_rows',
    'normalized_rows',
    'response_sha256',
    'detail',
]
OUTPUT_FIELDS = [
    'resolution_id',
    *FISCAL_PERIOD_RESOLUTION_FIELDS,
    'normalized_sha256',
]
ACTUAL_OUTPUT_FIELDS = [
    'outcome_id',
    'row_sequence',
    'previous_row_sha256',
    'row_sha256',
    *ACTUAL_OUTCOME_VALUE_FIELDS,
    'normalized_sha256',
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, default=DEFAULT_CONFIG)
    parser.add_argument('--entitlements', type=Path, default=DEFAULT_ENTITLEMENTS)
    parser.add_argument('--db', type=Path)
    parser.add_argument('--earnings-history', type=Path)
    parser.add_argument('--symbols-file', type=Path)
    parser.add_argument('--symbols', nargs='*')
    parser.add_argument('--as-of', type=date.fromisoformat)
    parser.add_argument('--retrieval-cycle')
    parser.add_argument('--output-dir', type=Path)
    parser.add_argument('--selftest', action='store_true')
    return parser.parse_args()


def _symbols(args: argparse.Namespace) -> list[str]:
    if args.symbols_file is not None and args.symbols:
        raise ValueError('--symbols-file and --symbols are mutually exclusive')
    if args.symbols_file is not None:
        with args.symbols_file.resolve().open(
            'r', encoding='utf-8-sig', newline=''
        ) as handle:
            reader = csv.DictReader(handle)
            fields = {
                str(value).casefold(): str(value) for value in (reader.fieldnames or [])
            }
            key = fields.get('ticker') or fields.get('symbol')
            if key is None:
                raise ValueError('Symbols file requires ticker or symbol')
            raw = [str(row.get(key, '')) for row in reader]
    else:
        raw = list(args.symbols or [])
    values = list(
        dict.fromkeys(value.strip().upper() for value in raw if value.strip())
    )
    if not values:
        raise ValueError('At least one symbol is required')
    if any(value == 'CASH' or any(char.isspace() for char in value) for value in values):
        raise ValueError('Symbols contain CASH or whitespace')
    return values


def build_resolution_rows(
    result: ProviderPayloadResult,
    *,
    as_of: date,
    retrieval_cycle: str,
    entitlement_version: str,
) -> list[dict[str, Any]]:
    if result.status != 'AVAILABLE' or not isinstance(result.payload, dict):
        return []
    source_rows = result.payload.get('quarterlyEarnings', [])
    if not isinstance(source_rows, list):
        return []
    output: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for source in source_rows:
        if not isinstance(source, dict):
            continue
        report_date = str(source.get('reportedDate', '')).strip()
        fiscal_period_end = str(source.get('fiscalDateEnding', '')).strip()
        try:
            reported_on = date.fromisoformat(report_date)
            period_ended_on = date.fromisoformat(fiscal_period_end)
        except ValueError:
            continue
        if reported_on > as_of or period_ended_on > reported_on:
            continue
        identity = (report_date, fiscal_period_end)
        if identity in seen:
            continue
        seen.add(identity)
        output.append(
            {
                'source_provider': 'alpha_vantage',
                'endpoint_id': result.capability,
                'ticker': result.symbol,
                'report_date': report_date,
                'fiscal_period_end': fiscal_period_end,
                'fiscal_period': 'quarterly',
                'report_time': str(source.get('reportTime', '')).strip(),
                'resolution_status': 'exact_provider_report_date_match',
                'resolution_eligible': 1,
                'ineligibility_reasons': '',
                'fetched_at_utc': result.requested_at_utc,
                'available_at_utc': result.requested_at_utc,
                'retrieval_cycle': retrieval_cycle,
                'response_sha256': result.response_sha256,
                'entitlement_version': entitlement_version,
                'retention_class': 'provisional_user_authorized',
                'coverage_status': 'available',
            }
        )
    return output


def build_alpha_actual_rows(
    result: ProviderPayloadResult,
    *,
    as_of: date,
    retrieval_cycle: str,
    entitlement_version: str,
) -> list[dict[str, Any]]:
    if result.status != 'AVAILABLE' or not isinstance(result.payload, dict):
        return []
    source_rows = result.payload.get('quarterlyEarnings', [])
    if not isinstance(source_rows, list):
        return []
    output: list[dict[str, Any]] = []
    for source in source_rows:
        if not isinstance(source, dict):
            continue
        report_date = str(source.get('reportedDate', '')).strip()
        fiscal_period_end = str(source.get('fiscalDateEnding', '')).strip()
        reported_eps = str(source.get('reportedEPS', '')).strip()
        try:
            reported_on = date.fromisoformat(report_date)
            period_ended_on = date.fromisoformat(fiscal_period_end)
            actual_value = float(reported_eps)
        except ValueError:
            continue
        if (
            reported_on > as_of
            or period_ended_on > reported_on
            or not math.isfinite(actual_value)
        ):
            continue
        output.append(
            {
                'provider': 'alpha_vantage',
                'endpoint_id': result.capability,
                'ticker': result.symbol,
                'report_date': report_date,
                'fiscal_period_end': fiscal_period_end,
                'outcome_period_status': 'exact_provider_report_date_match',
                'metric': 'eps',
                'actual_value': actual_value,
                'reporting_currency': '',
                'metric_basis_id': '',
                'metric_basis_status': 'provider_internal_unverified',
                'provider_updated_at_raw': '',
                'provider_published_at_utc': '',
                'fetched_at_utc': result.requested_at_utc,
                'available_at_utc': result.requested_at_utc,
                'retrieval_cycle': retrieval_cycle,
                'response_sha256': result.response_sha256,
                'entitlement_version': entitlement_version,
                'retention_class': 'provisional_user_authorized',
                'coverage_status': 'available',
                'evaluation_eligible': 0,
                'ineligibility_reasons': (
                    'metric_basis_not_comparison_eligible,'
                    'actual_publication_time_unverified'
                ),
            }
        )
    return output


def calendar_resolution_rows(
    path: Path,
    *,
    symbols: set[str],
    as_of: date,
    retrieval_cycle: str,
    entitlement_version: str,
) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    source_digest = sha256_file(path)
    latest: dict[tuple[str, str, str], dict[str, Any]] = {}
    with path.open('r', encoding='utf-8-sig', newline='') as handle:
        for source in csv.DictReader(handle):
            ticker = str(source.get('ticker', '')).strip().upper()
            report_date = str(source.get('next_earnings_date', '')).strip()
            fiscal_period_end = str(source.get('fiscal_date_ending', '')).strip()
            fetched_at = str(source.get('fetched_at_utc', '')).strip()
            source_name = str(source.get('source', '')).strip()
            if (
                ticker not in symbols
                or source_name != 'alpha_vantage_bulk'
                or not report_date
                or not fiscal_period_end
                or not fetched_at
            ):
                continue
            try:
                reported_on = date.fromisoformat(report_date)
                period_ended_on = date.fromisoformat(fiscal_period_end)
                fetched_on = date.fromisoformat(fetched_at[:10])
            except ValueError:
                continue
            if fetched_on > as_of or period_ended_on > reported_on:
                continue
            row = {
                'source_provider': 'alpha_vantage',
                'endpoint_id': 'earnings_calendar_history',
                'ticker': ticker,
                'report_date': report_date,
                'fiscal_period_end': fiscal_period_end,
                'fiscal_period': 'quarterly',
                'report_time': '',
                'resolution_status': 'exact_alpha_bulk_calendar_match',
                'resolution_eligible': 1,
                'ineligibility_reasons': '',
                'fetched_at_utc': fetched_at,
                'available_at_utc': fetched_at,
                'retrieval_cycle': retrieval_cycle,
                'response_sha256': source_digest,
                'entitlement_version': entitlement_version,
                'retention_class': 'provisional_user_authorized',
                'coverage_status': 'available',
            }
            key = (ticker, report_date, fiscal_period_end)
            prior = latest.get(key)
            if prior is None or fetched_at > str(prior['fetched_at_utc']):
                latest[key] = row
    return sorted(latest.values(), key=lambda row: (row['ticker'], row['report_date']))


def run_selftest() -> None:
    result = ProviderPayloadResult(
        'alpha_vantage',
        'earnings_history',
        'AAA',
        '2026-07-31T22:00:00+00:00',
        'AVAILABLE',
        200,
        1,
        'object.quarterlyEarnings',
        1,
        'fiscalDateEnding,reportedDate,reportedEPS,reportTime',
        'ok',
        'a' * 64,
        {
            'quarterlyEarnings': [
                {
                    'fiscalDateEnding': '2026-06-30',
                    'reportedDate': '2026-07-30',
                    'reportedEPS': '2.5',
                    'reportTime': 'post-market',
                }
            ]
        },
    )
    rows = build_resolution_rows(
        result,
        as_of=date(2026, 7, 31),
        retrieval_cycle='selftest',
        entitlement_version='provider_entitlements_v1:provisional_retention_v1',
    )
    assert len(rows) == 1
    assert rows[0]['fiscal_period_end'] == '2026-06-30'
    actual_rows = build_alpha_actual_rows(
        result,
        as_of=date(2026, 7, 31),
        retrieval_cycle='selftest',
        entitlement_version='provider_entitlements_v1:provisional_retention_v1',
    )
    assert len(actual_rows) == 1
    assert actual_rows[0]['actual_value'] == 2.5
    with tempfile.TemporaryDirectory() as tmp:
        calendar_path = Path(tmp) / 'calendar.csv'
        write_csv(
            calendar_path,
            [
                'ticker',
                'next_earnings_date',
                'fiscal_date_ending',
                'fetched_at_utc',
                'source',
            ],
            [
                {
                    'ticker': 'BBB',
                    'next_earnings_date': '2026-08-10',
                    'fiscal_date_ending': '2026-06-30',
                    'fetched_at_utc': '2026-07-30T20:00:00+00:00',
                    'source': 'alpha_vantage_bulk',
                }
            ],
        )
        calendar_rows = calendar_resolution_rows(
            calendar_path,
            symbols={'BBB'},
            as_of=date(2026, 7, 31),
            retrieval_cycle='selftest',
            entitlement_version='provider_entitlements_v1:provisional_retention_v1',
        )
        assert len(calendar_rows) == 1
        assert calendar_rows[0]['report_date'] == '2026-08-10'
        conn = connect_monitor_db(Path(tmp) / 'monitor.sqlite', timeout_sec=1.0)
        try:
            assert append_fiscal_period_resolutions(conn, rows) == (1, 0)
            assert append_fiscal_period_resolutions(conn, rows) == (0, 1)
            assert append_actual_outcomes(conn, actual_rows) == (1, 0)
            assert append_actual_outcomes(conn, actual_rows) == (0, 1)
        finally:
            conn.close()
    print('fiscal-period resolution selftest: PASS')


def main() -> int:
    args = parse_args()
    if args.selftest:
        run_selftest()
        return 0
    if args.as_of is None:
        raise ValueError('--as-of is required')
    symbols = _symbols(args)
    retrieval_cycle = str(
        args.retrieval_cycle or f'{args.as_of.isoformat()}-fiscal-periods'
    ).strip()
    if not retrieval_cycle or any(char.isspace() for char in retrieval_cycle):
        raise ValueError('Invalid retrieval cycle')

    config_path = args.config.resolve()
    entitlements_path = args.entitlements.resolve()
    config = load_yaml(config_path)
    paths = resolve_runtime_paths(config, config_path)
    monitor_cfg = cfg_get(config, 'expectations_monitor', {})
    if not isinstance(monitor_cfg, dict):
        raise ValueError('expectations_monitor config must be a mapping')
    evaluation_cfg = monitor_cfg.get('forecast_evaluation', {})
    required_policy = {
        'policy_version': 'forecast_evaluation_v1',
        'fiscal_period_resolver_provider': 'alpha_vantage',
        'fiscal_period_resolver_endpoint': 'earnings_history',
    }
    if not isinstance(evaluation_cfg, dict):
        raise ValueError('forecast_evaluation config must be a mapping')
    for field, expected in required_policy.items():
        if evaluation_cfg.get(field) != expected:
            raise ValueError(f'forecast_evaluation.{field} must be {expected!r}')
    retention_cfg = monitor_cfg.get('retention', {})
    if not isinstance(retention_cfg, dict):
        raise ValueError('expectations_monitor.retention must be a mapping')
    if not bool(retention_cfg.get('normalized_snapshots_enabled', False)):
        raise RuntimeError('Normalized provider retention is disabled')
    if bool(retention_cfg.get('raw_payload_retention_enabled', False)):
        raise RuntimeError('Raw payload retention must remain disabled')

    entitlements = load_entitlements(entitlements_path)
    provider_cfg = entitlements['providers']['alpha_vantage']
    capability = provider_cfg['capabilities']['earnings_history']
    probe_cfg = entitlements.get('probe', {})
    max_symbols = int(
        probe_cfg.get('max_symbols_by_provider', {}).get('alpha_vantage', 0)
    )
    if len(symbols) > max_symbols:
        raise ValueError(
            f'Alpha symbol count {len(symbols)} exceeds configured cap {max_symbols}'
        )
    timeout = float(probe_cfg.get('timeout_sec', 30.0))
    max_bytes = int(probe_cfg.get('max_response_bytes', 2_000_000))
    max_retries = int(probe_cfg.get('max_retries', 2))
    pause = float(
        provider_cfg.get('request_pause_sec', probe_cfg.get('request_pause_sec', 0.0))
    )
    entitlement_version = (
        f"{entitlements['schema_version']}:{retention_cfg['policy_version']}"
    )

    capture_rows: list[dict[str, Any]] = []
    earnings_history_path = (
        args.earnings_history.resolve()
        if args.earnings_history is not None
        else paths.output_dir / 'earnings_dates' / 'earnings_calendar_history.csv'
    )
    calendar_rows = calendar_resolution_rows(
        earnings_history_path,
        symbols=set(symbols),
        as_of=args.as_of,
        retrieval_cycle=retrieval_cycle,
        entitlement_version=entitlement_version,
    )
    resolution_rows: list[dict[str, Any]] = list(calendar_rows)
    actual_rows: list[dict[str, Any]] = []
    for symbol in symbols:
        result = fetch_capability_payload(
            provider='alpha_vantage',
            provider_config=provider_cfg,
            capability='earnings_history',
            capability_config=capability,
            symbol=symbol,
            as_of=args.as_of,
            timeout_sec=timeout,
            max_response_bytes=max_bytes,
            max_retries=max_retries,
        )
        normalized = build_resolution_rows(
            result,
            as_of=args.as_of,
            retrieval_cycle=retrieval_cycle,
            entitlement_version=entitlement_version,
        )
        normalized_actuals = build_alpha_actual_rows(
            result,
            as_of=args.as_of,
            retrieval_cycle=retrieval_cycle,
            entitlement_version=entitlement_version,
        )
        status = result.status
        if status == 'AVAILABLE' and not normalized:
            status = 'NORMALIZATION_EMPTY'
        capture_rows.append(
            {
                'provider': 'alpha_vantage',
                'endpoint_id': 'earnings_history',
                'symbol': symbol,
                'status': status,
                'http_status': '' if result.http_status is None else result.http_status,
                'elapsed_ms': result.elapsed_ms,
                'provider_rows': result.row_count,
                'normalized_rows': len(normalized),
                'response_sha256': result.response_sha256,
                'detail': result.detail,
            }
        )
        resolution_rows.extend(normalized)
        actual_rows.extend(normalized_actuals)
        del result
        if pause > 0:
            time.sleep(pause)

    deduplicated: dict[tuple[str, str, str], dict[str, Any]] = {}
    for row in resolution_rows:
        key = (
            str(row['ticker']),
            str(row['report_date']),
            str(row['fiscal_period_end']),
        )
        prior = deduplicated.get(key)
        if prior is None or row['endpoint_id'] == 'earnings_history':
            deduplicated[key] = row
    resolution_rows = sorted(
        deduplicated.values(), key=lambda row: (row['ticker'], row['report_date'])
    )

    db_path = ensure_not_prod_path(
        args.db.resolve()
        if args.db
        else resolve_path(
            monitor_cfg.get('database_path', 'db/expectations_monitor.sqlite'),
            base_dir=config_path.parent,
        ),
        label='expectations monitor database',
    )
    db_timeout = float(monitor_cfg.get('writer_lock_timeout_sec', 30.0))
    lock_path = db_path.with_suffix(db_path.suffix + '.writer.lock')
    with writer_lock(lock_path, timeout_sec=db_timeout):
        conn = connect_monitor_db(db_path, timeout_sec=db_timeout)
        try:
            inserted, duplicates = append_fiscal_period_resolutions(
                conn, resolution_rows
            )
            actual_inserted, actual_duplicates = append_actual_outcomes(
                conn, actual_rows
            )
            stored_rows = [
                dict(row)
                for row in conn.execute(
                    'SELECT * FROM provider_fiscal_period_resolutions '
                    'WHERE retrieval_cycle=? ORDER BY ticker,report_date',
                    (retrieval_cycle,),
                ).fetchall()
            ]
            stored_actuals = [
                dict(row)
                for row in conn.execute(
                    'SELECT * FROM provider_actual_outcomes_v2 '
                    'WHERE provider=\'alpha_vantage\' AND retrieval_cycle=? '
                    'ORDER BY ticker,report_date',
                    (retrieval_cycle,),
                ).fetchall()
            ]
        finally:
            conn.close()

    output_dir = (
        args.output_dir.resolve()
        if args.output_dir
        else paths.output_dir / 'fiscal_period_resolutions' / retrieval_cycle
    )
    capture_path = output_dir / 'fiscal_period_resolution_capture.csv'
    resolution_path = output_dir / 'fiscal_period_resolutions.csv'
    actuals_path = output_dir / 'alpha_eps_actual_outcomes.csv'
    manifest_path = output_dir / 'fiscal_period_resolution_manifest.json'
    write_csv(capture_path, CAPTURE_FIELDS, capture_rows)
    write_csv(resolution_path, OUTPUT_FIELDS, stored_rows)
    write_csv(actuals_path, ACTUAL_OUTPUT_FIELDS, stored_actuals)
    failed = [row for row in capture_rows if row['status'] != 'AVAILABLE']
    acceptance = 'PASS' if not failed else 'FAIL'
    inputs = [
        config_path,
        entitlements_path,
        earnings_history_path,
        Path(__file__).resolve(),
        Path(__file__).with_name('provider_common.py').resolve(),
        Path(__file__).with_name('monitor_common.py').resolve(),
    ]
    if args.symbols_file is not None:
        inputs.append(args.symbols_file.resolve())
    write_manifest(
        manifest_path,
        {
            'schema_version': 'fiscal_period_resolution_manifest_v1',
            'acceptance': acceptance,
            'shadow_only': True,
            'as_of_date': args.as_of.isoformat(),
            'retrieval_cycle': retrieval_cycle,
            'symbol_count': len(symbols),
            'resolution_count': len(stored_rows),
            'calendar_resolution_count': len(calendar_rows),
            'inserted_count': inserted,
            'duplicate_count': duplicates,
            'alpha_eps_actual_count': len(stored_actuals),
            'alpha_eps_actual_inserted_count': actual_inserted,
            'alpha_eps_actual_duplicate_count': actual_duplicates,
            'failed_symbols': [str(row['symbol']) for row in failed],
            'raw_payloads_retained': False,
            'provider_request_fields': ['endpoint', 'ticker', 'authentication'],
            'implementation_or_policy_data_sent': False,
            'inputs_sha256': {str(path): sha256_file(path) for path in inputs},
            'outputs_sha256': {
                capture_path.name: sha256_file(capture_path),
                resolution_path.name: sha256_file(resolution_path),
                actuals_path.name: sha256_file(actuals_path),
            },
            'generated_at_utc': datetime.now(timezone.utc)
            .replace(microsecond=0)
            .isoformat(),
        },
    )
    print(f'FISCAL PERIOD RESOLUTION: {acceptance}')
    print(f'resolutions: {len(stored_rows)} (inserted={inserted}, duplicates={duplicates})')
    print(
        f'alpha EPS actuals: {len(stored_actuals)} '
        f'(inserted={actual_inserted}, duplicates={actual_duplicates})'
    )
    return 1 if acceptance == 'FAIL' else 0


if __name__ == '__main__':
    raise SystemExit(main())
