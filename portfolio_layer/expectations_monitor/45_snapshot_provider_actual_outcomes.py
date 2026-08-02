#!/usr/bin/env python3
'''Append normalized FMP earnings actuals to a tamper-evident outcome ledger.'''

from __future__ import annotations

import argparse
import csv
import sys
import tempfile
import time
from datetime import date, datetime
from pathlib import Path
from typing import Any


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from portfolio_layer.core.config import cfg_get, load_yaml, resolve_path  # noqa: E402
from portfolio_layer.core.contracts import (  # noqa: E402
    read_csv,
    sha256_file,
    write_csv,
    write_manifest,
)
from portfolio_layer.core.paths import ensure_not_prod_path, resolve_runtime_paths  # noqa: E402
from portfolio_layer.expectations_monitor.monitor_common import (  # noqa: E402
    append_actual_outcomes,
    connect_monitor_db,
    verify_actual_outcome_chain,
    writer_lock,
)
from portfolio_layer.expectations_monitor.provider_common import (  # noqa: E402
    ProviderPayloadResult,
    fetch_capability_payload,
    load_entitlements,
)


DEFAULT_CONFIG = PACKAGE_ROOT / 'config.yaml'
DEFAULT_ENTITLEMENTS = Path(__file__).with_name('provider_entitlements.yaml')
REPORT_FIELDS = [
    'ticker',
    'status',
    'http_status',
    'provider_rows',
    'normalized_outcomes',
    'evaluation_eligible_outcomes',
    'response_sha256',
    'detail',
]
OUTCOME_FIELDS = [
    'provider',
    'endpoint_id',
    'ticker',
    'report_date',
    'fiscal_period_end',
    'outcome_period_status',
    'metric',
    'actual_value',
    'reporting_currency',
    'metric_basis_id',
    'metric_basis_status',
    'provider_updated_at_raw',
    'provider_published_at_utc',
    'fetched_at_utc',
    'available_at_utc',
    'retrieval_cycle',
    'response_sha256',
    'entitlement_version',
    'retention_class',
    'coverage_status',
    'evaluation_eligible',
    'ineligibility_reasons',
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, default=DEFAULT_CONFIG)
    parser.add_argument('--entitlements', type=Path, default=DEFAULT_ENTITLEMENTS)
    parser.add_argument('--db', type=Path)
    parser.add_argument('--symbols-file', type=Path)
    parser.add_argument('--symbols', nargs='*')
    parser.add_argument('--as-of', type=date.fromisoformat)
    parser.add_argument('--retrieval-cycle')
    parser.add_argument('--earnings-history', type=Path)
    parser.add_argument('--output-dir', type=Path)
    parser.add_argument('--selftest', action='store_true')
    return parser.parse_args()


def _symbols(args: argparse.Namespace) -> list[str]:
    if args.symbols_file is not None and args.symbols:
        raise ValueError('--symbols-file and --symbols are mutually exclusive')
    if args.symbols_file is not None:
        with args.symbols_file.resolve().open('r', encoding='utf-8-sig', newline='') as handle:
            reader = csv.DictReader(handle)
            fields = {str(value).casefold(): str(value) for value in (reader.fieldnames or [])}
            key = fields.get('ticker') or fields.get('symbol')
            if key is None:
                raise ValueError('Symbols file requires ticker or symbol')
            raw = [str(row.get(key, '')) for row in reader]
    else:
        raw = list(args.symbols or [])
    values = list(
        dict.fromkeys(
            value.strip().upper()
            for value in raw
            if value.strip()
            and value.strip().upper() != 'CASH'
            and not any(character.isspace() for character in value.strip())
        )
    )
    if not values:
        raise ValueError('At least one symbol is required')
    return values


def _trusted_published_utc(raw: Any) -> str:
    value = str(raw if raw is not None else '').strip()
    if not value:
        return ''
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        return ''
    if parsed.tzinfo is None:
        return ''
    return parsed.isoformat()


def _number(value: Any) -> float | None:
    text = str(value if value is not None else '').strip()
    if not text or text.casefold() in {'none', 'null', 'n/a'}:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _fiscal_period_map(path: Path, *, as_of: date) -> dict[str, dict[str, str]]:
    if not path.exists():
        return {}
    output: dict[str, dict[str, tuple[str, str]]] = {}
    for row in read_csv(path):
        ticker = str(row.get('ticker', '')).strip().upper()
        report_date = str(row.get('next_earnings_date', '')).strip()
        fiscal_end = str(row.get('fiscal_date_ending', '')).strip()
        fetched = str(row.get('fetched_at_utc', '')).strip()
        if not ticker or not report_date or not fiscal_end or not fetched:
            continue
        try:
            if date.fromisoformat(fetched[:10]) > as_of:
                continue
            date.fromisoformat(report_date)
            date.fromisoformat(fiscal_end)
        except ValueError:
            continue
        ticker_map = output.setdefault(ticker, {})
        prior = ticker_map.get(report_date)
        if prior is None or fetched > prior[0]:
            ticker_map[report_date] = (fetched, fiscal_end)
    return {
        ticker: {report_date: value[1] for report_date, value in rows.items()}
        for ticker, rows in output.items()
    }
def build_actual_rows(
    result: ProviderPayloadResult,
    *,
    as_of: date,
    retrieval_cycle: str,
    entitlement_version: str,
    basis_by_metric: dict[str, dict[str, Any]],
    fiscal_period_by_report_date: dict[str, str],
) -> list[dict[str, Any]]:
    if result.status != 'AVAILABLE':
        return []
    source_rows = result.payload if isinstance(result.payload, list) else []
    output: list[dict[str, Any]] = []
    for source in source_rows:
        if not isinstance(source, dict):
            continue
        report_date = str(source.get('date', '')).strip()
        try:
            reported_on = date.fromisoformat(report_date)
        except ValueError:
            continue
        if reported_on > as_of:
            continue
        updated_raw = str(source.get('lastUpdated', '')).strip()
        published_utc = _trusted_published_utc(updated_raw)
        fiscal_period_end = fiscal_period_by_report_date.get(report_date, '')
        for metric, field in (('eps', 'epsActual'), ('revenue', 'revenueActual')):
            actual = _number(source.get(field))
            if actual is None:
                continue
            basis = basis_by_metric.get(metric, {})
            basis_eligible = bool(basis) and bool(
                int(basis.get('comparison_eligible', 0))
            )
            reasons: list[str] = []
            if not basis:
                reasons.append('metric_basis_missing')
            elif not basis_eligible:
                reasons.append('metric_basis_not_comparison_eligible')
            if not published_utc:
                reasons.append('actual_publication_time_unverified')
            if not fiscal_period_end:
                reasons.append('fiscal_period_end_unresolved')
            output.append(
                {
                    'provider': 'fmp',
                    'endpoint_id': result.capability,
                    'ticker': result.symbol,
                    'report_date': report_date,
                    'fiscal_period_end': fiscal_period_end,
                    'outcome_period_status': (
                        'exact_earnings_calendar_match'
                        if fiscal_period_end
                        else 'report_date_only_unmapped'
                    ),
                    'metric': metric,
                    'actual_value': actual,
                    'reporting_currency': str(basis.get('reporting_currency', '')),
                    'metric_basis_id': str(basis.get('basis_snapshot_id', '')),
                    'metric_basis_status': (
                        'eligible' if basis_eligible else 'fail_closed'
                    ),
                    'provider_updated_at_raw': updated_raw,
                    'provider_published_at_utc': published_utc,
                    'fetched_at_utc': result.response_received_at_utc,
                    'available_at_utc': result.response_received_at_utc,
                    'retrieval_cycle': retrieval_cycle,
                    'response_sha256': result.response_sha256,
                    'entitlement_version': entitlement_version,
                    'retention_class': 'provisional_user_authorized',
                    'coverage_status': 'available',
                    'evaluation_eligible': int(not reasons),
                    'ineligibility_reasons': ','.join(reasons),
                }
            )
    return output


def run_selftest() -> None:
    result = ProviderPayloadResult(
        'fmp',
        'earnings_report',
        'AAA',
        '2026-07-31T22:00:00+00:00',
        '2026-07-31T22:00:01+00:00',
        'AVAILABLE',
        200,
        1,
        'list',
        1,
        'date,epsActual,lastUpdated,revenueActual',
        'ok',
        'a' * 64,
        [
            {
                'date': '2026-06-30',
                'epsActual': '2.5',
                'revenueActual': '100',
                'lastUpdated': '2026-07-20',
            }
        ],
    )
    rows = build_actual_rows(
        result,
        as_of=date(2026, 7, 31),
        retrieval_cycle='test',
        entitlement_version='provider_entitlements_v1:provisional_retention_v1',
        basis_by_metric={},
        fiscal_period_by_report_date={},
    )
    assert len(rows) == 2
    assert not any(row['evaluation_eligible'] for row in rows)
    assert all('actual_publication_time_unverified' in row['ineligibility_reasons'] for row in rows)
    with tempfile.TemporaryDirectory() as tmp:
        conn = connect_monitor_db(Path(tmp) / 'monitor.sqlite', timeout_sec=1.0)
        try:
            assert append_actual_outcomes(conn, rows) == (2, 0)
            assert append_actual_outcomes(conn, rows) == (0, 2)
            assert verify_actual_outcome_chain(conn) == []
            with conn:
                conn.execute(
                    'UPDATE provider_actual_outcomes_v2 SET normalized_sha256=? '
                    'WHERE row_sequence=1',
                    ('f' * 64,),
                )
            assert verify_actual_outcome_chain(conn)
        finally:
            conn.close()
    print('provider actual-outcome ledger selftest: PASS')


def main() -> int:
    args = parse_args()
    if args.selftest:
        run_selftest()
        return 0
    if args.as_of is None:
        raise ValueError('--as-of is required')
    symbols = _symbols(args)
    retrieval_cycle = str(
        args.retrieval_cycle or f'{args.as_of.isoformat()}-actuals'
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
    outcome_cfg = monitor_cfg.get('provider_outcomes', {})
    if not isinstance(outcome_cfg, dict) or outcome_cfg.get('policy_version') != 'provider_outcomes_v1':
        raise ValueError('provider_outcomes_v1 config is required')
    required_outcome_policy = {
        'provider': 'fmp',
        'endpoint': 'earnings_report',
        'require_trusted_release_timestamp': True,
        'require_eligible_metric_basis': True,
        'forecast_linking': 'fail_closed',
    }
    for field, expected in required_outcome_policy.items():
        if outcome_cfg.get(field) != expected:
            raise ValueError(f'provider_outcomes.{field} must be {expected!r}')
    retention_cfg = monitor_cfg.get('retention', {})
    if not isinstance(retention_cfg, dict):
        raise ValueError('expectations_monitor.retention must be a mapping')
    if not bool(retention_cfg.get('normalized_snapshots_enabled', False)):
        raise RuntimeError('Normalized provider retention is disabled')
    if bool(retention_cfg.get('raw_payload_retention_enabled', False)):
        raise RuntimeError('Raw payload retention must remain disabled')

    entitlements = load_entitlements(entitlements_path)
    max_symbols = int(
        entitlements.get('probe', {}).get('max_symbols_by_provider', {}).get('fmp', 0)
    )
    if len(symbols) > max_symbols:
        raise ValueError(f'FMP symbol count {len(symbols)} exceeds configured cap {max_symbols}')
    fmp_cfg = entitlements['providers']['fmp']
    capability = fmp_cfg['capabilities']['earnings_report']
    probe_cfg = entitlements.get('probe', {})
    timeout = float(probe_cfg.get('timeout_sec', 30.0))
    max_bytes = int(probe_cfg.get('max_response_bytes', 2_000_000))
    max_retries = int(probe_cfg.get('max_retries', 1))
    pause = float(fmp_cfg.get('request_pause_sec', probe_cfg.get('request_pause_sec', 0.0)))
    entitlement_version = ':'.join(
        (str(entitlements['schema_version']), str(retention_cfg['policy_version']))
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
    earnings_history = (
        args.earnings_history.resolve()
        if args.earnings_history is not None
        else paths.output_dir / 'earnings_dates' / 'earnings_calendar_history.csv'
    )
    fiscal_periods = _fiscal_period_map(earnings_history, as_of=args.as_of)
    conn = connect_monitor_db(db_path, timeout_sec=db_timeout)
    try:
        basis_by_ticker: dict[str, dict[str, dict[str, Any]]] = {}
        basis_rows = conn.execute(
            'SELECT * FROM provider_metric_basis_snapshots '
            'WHERE estimate_provider=\'fmp\' ORDER BY fetched_at_utc DESC'
        ).fetchall()
        for row in basis_rows:
            ticker_map = basis_by_ticker.setdefault(str(row['ticker']), {})
            ticker_map.setdefault(str(row['metric']), dict(row))
    finally:
        conn.close()

    outcome_rows: list[dict[str, Any]] = []
    reports: list[dict[str, Any]] = []
    hard_errors = 0
    for symbol in symbols:
        result = fetch_capability_payload(
            provider='fmp',
            provider_config=fmp_cfg,
            capability='earnings_report',
            capability_config=capability,
            symbol=symbol,
            as_of=args.as_of,
            timeout_sec=timeout,
            max_response_bytes=max_bytes,
            max_retries=max_retries,
        )
        rows = build_actual_rows(
            result,
            as_of=args.as_of,
            retrieval_cycle=retrieval_cycle,
            entitlement_version=entitlement_version,
            basis_by_metric=basis_by_ticker.get(symbol, {}),
            fiscal_period_by_report_date=fiscal_periods.get(symbol, {}),
        )
        status = result.status if rows or result.status != 'AVAILABLE' else 'NORMALIZATION_EMPTY'
        if status not in {'AVAILABLE', 'EMPTY'}:
            hard_errors += 1
        reports.append(
            {
                'ticker': symbol,
                'status': status,
                'http_status': '' if result.http_status is None else result.http_status,
                'provider_rows': result.row_count,
                'normalized_outcomes': len(rows),
                'evaluation_eligible_outcomes': sum(
                    int(row['evaluation_eligible']) for row in rows
                ),
                'response_sha256': result.response_sha256,
                'detail': result.detail,
            }
        )
        outcome_rows.extend(rows)
        del result
        if pause > 0:
            time.sleep(pause)

    lock_path = db_path.with_suffix(db_path.suffix + '.writer.lock')
    with writer_lock(lock_path, timeout_sec=db_timeout):
        conn = connect_monitor_db(db_path, timeout_sec=db_timeout)
        try:
            inserted, duplicates = append_actual_outcomes(conn, outcome_rows)
            chain_errors = verify_actual_outcome_chain(conn)
        finally:
            conn.close()
    if chain_errors:
        raise RuntimeError(f'Actual-outcome chain verification failed: {chain_errors}')

    output_dir = (
        args.output_dir.resolve()
        if args.output_dir
        else paths.output_dir / 'provider_actual_outcomes' / retrieval_cycle
    )
    report_path = output_dir / 'provider_actual_capture.csv'
    outcomes_path = output_dir / 'provider_actual_outcomes.csv'
    manifest_path = output_dir / 'provider_actual_outcomes_manifest.json'
    write_csv(report_path, REPORT_FIELDS, reports)
    write_csv(outcomes_path, OUTCOME_FIELDS, outcome_rows)
    if hard_errors:
        acceptance = 'FAIL'
    elif outcome_rows:
        acceptance = 'PASS'
    else:
        acceptance = 'PASS_NO_NEW_OUTCOMES'
    inputs = [
        config_path,
        entitlements_path,
        Path(__file__).resolve(),
        Path(__file__).with_name('provider_common.py').resolve(),
        Path(__file__).with_name('monitor_common.py').resolve(),
    ]
    if args.symbols_file is not None:
        inputs.append(args.symbols_file.resolve())
    if earnings_history.exists():
        inputs.append(earnings_history)
    write_manifest(
        manifest_path,
        {
            'schema_version': 'provider_actual_outcomes_manifest_v2',
            'acceptance': acceptance,
            'as_of_date': args.as_of.isoformat(),
            'retrieval_cycle': retrieval_cycle,
            'policy_version': outcome_cfg['policy_version'],
            'symbol_count': len(symbols),
            'normalized_outcome_count': len(outcome_rows),
            'evaluation_eligible_count': sum(
                int(row['evaluation_eligible']) for row in outcome_rows
            ),
            'inserted_count': inserted,
            'idempotent_duplicate_count': duplicates,
            'hard_error_count': hard_errors,
            'chain_verified': True,
            'forecast_links_created': 0,
            'forecast_linking_status': (
                'pending_fiscal_period_basis_and_release_time_contracts'
            ),
            'raw_payloads_retained': False,
            'earnings_calendar_mapping_count': sum(
                len(rows) for rows in fiscal_periods.values()
            ),
            'shadow_only': True,
            'inputs_sha256': {str(path): sha256_file(path) for path in inputs},
            'outputs_sha256': {
                report_path.name: sha256_file(report_path),
                outcomes_path.name: sha256_file(outcomes_path),
            },
        },
    )
    eligible_count = sum(int(row['evaluation_eligible']) for row in outcome_rows)
    print(f'PROVIDER ACTUAL OUTCOMES: {acceptance}')
    print(
        f'outcomes={len(outcome_rows)}; eligible='
        f'{eligible_count}; '
        f'inserted={inserted}; duplicates={duplicates}'
    )
    return 1 if acceptance == 'FAIL' else 0


if __name__ == '__main__':
    raise SystemExit(main())
