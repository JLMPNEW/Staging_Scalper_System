#!/usr/bin/env python3
'''Capture issuer reporting currency and seal fail-closed estimate-basis contracts.'''

from __future__ import annotations

import argparse
import csv
import sys
import time
from datetime import date
from pathlib import Path
from typing import Any, Mapping


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from portfolio_layer.core.config import cfg_get, load_yaml, resolve_path  # noqa: E402
from portfolio_layer.core.contracts import sha256_file, write_csv, write_manifest  # noqa: E402
from portfolio_layer.core.paths import ensure_not_prod_path, resolve_runtime_paths  # noqa: E402
from portfolio_layer.expectations_monitor.monitor_common import (  # noqa: E402
    append_metric_basis_snapshots,
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
REPORT_FIELDS = [
    'ticker',
    'status',
    'http_status',
    'reporting_currency',
    'statement_period_end',
    'contract_rows',
    'eligible_rows',
    'response_sha256',
    'detail',
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
    symbols = list(dict.fromkeys(value.strip().upper() for value in raw if value.strip()))
    if not symbols:
        raise ValueError('At least one symbol is required')
    return symbols


def _statement_currency(
    result: ProviderPayloadResult, *, as_of: date
) -> tuple[str, str]:
    rows = result.payload if isinstance(result.payload, list) else []
    candidates: list[tuple[str, str]] = []
    for raw in rows:
        if not isinstance(raw, dict):
            continue
        period_end = str(raw.get('date', '')).strip()
        currency = str(raw.get('reportedCurrency', '')).strip().upper()
        try:
            period_date = date.fromisoformat(period_end)
        except ValueError:
            continue
        if period_date <= as_of and len(currency) == 3 and currency.isalpha():
            candidates.append((period_end, currency))
    if not candidates:
        return '', ''
    return max(candidates)


def build_basis_rows(
    result: ProviderPayloadResult,
    *,
    as_of: date,
    retrieval_cycle: str,
    entitlement_version: str,
    semantics: Mapping[str, Any],
) -> list[dict[str, Any]]:
    if result.status != 'AVAILABLE':
        return []
    statement_period_end, reporting_currency = _statement_currency(result, as_of=as_of)
    if not reporting_currency:
        return []
    rows: list[dict[str, Any]] = []
    for estimate_provider in ('alpha_vantage', 'fmp'):
        provider_semantics = semantics.get(estimate_provider, {})
        if not isinstance(provider_semantics, Mapping):
            raise ValueError(f'Invalid semantics config for {estimate_provider}')
        for metric in ('eps', 'revenue'):
            metric_semantics = provider_semantics.get(metric, {})
            if not isinstance(metric_semantics, Mapping):
                raise ValueError(f'Invalid {estimate_provider}/{metric} semantics')
            currency_status = str(
                metric_semantics.get('currency_semantics_status', 'unverified')
            )
            definition_status = str(
                metric_semantics.get('definition_semantics_status', 'unverified')
            )
            reasons: list[str] = []
            if currency_status != 'verified':
                reasons.append('provider_currency_semantics_unverified')
            if definition_status != 'verified':
                reasons.append('provider_metric_definition_unverified')
            rows.append(
                {
                    'estimate_provider': estimate_provider,
                    'currency_source_provider': 'fmp',
                    'endpoint_id': result.capability,
                    'ticker': result.symbol,
                    'metric': metric,
                    'reporting_currency': reporting_currency,
                    'statement_period_end': statement_period_end,
                    'metric_definition': str(metric_semantics.get('metric_definition', 'unknown')),
                    'unit_scale': str(metric_semantics.get('unit_scale', 'unknown')),
                    'per_share_basis': str(metric_semantics.get('per_share_basis', 'not_applicable')),
                    'currency_semantics_status': currency_status,
                    'definition_semantics_status': definition_status,
                    'comparison_eligible': int(not reasons),
                    'ineligibility_reasons': ','.join(reasons),
                    'fetched_at_utc': result.requested_at_utc,
                    'available_at_utc': result.requested_at_utc,
                    'retrieval_cycle': retrieval_cycle,
                    'response_sha256': result.response_sha256,
                    'entitlement_version': entitlement_version,
                    'retention_class': 'provisional_user_authorized',
                    'coverage_status': 'available',
                }
            )
    return rows


def run_selftest() -> None:
    result = ProviderPayloadResult(
        'fmp',
        'reporting_currency',
        'AAA',
        '2026-07-31T22:00:00+00:00',
        'AVAILABLE',
        200,
        1,
        'list',
        1,
        'date,reportedCurrency',
        'ok',
        'a' * 64,
        [{'date': '2025-12-31', 'reportedCurrency': 'USD'}],
    )
    semantics = {
        provider: {
            'eps': {
                'currency_semantics_status': 'unverified',
                'definition_semantics_status': 'unverified',
                'metric_definition': 'provider_consensus_eps_unknown_gaap_adjusted',
                'unit_scale': 'currency_per_share',
                'per_share_basis': 'provider_defined_unknown',
            },
            'revenue': {
                'currency_semantics_status': 'unverified',
                'definition_semantics_status': 'unverified',
                'metric_definition': 'provider_consensus_total_revenue',
                'unit_scale': 'currency_units',
                'per_share_basis': 'not_applicable',
            },
        }
        for provider in ('alpha_vantage', 'fmp')
    }
    rows = build_basis_rows(
        result,
        as_of=date(2026, 7, 31),
        retrieval_cycle='test',
        entitlement_version='provider_entitlements_v1:provisional_retention_v1',
        semantics=semantics,
    )
    assert len(rows) == 4
    assert {row['reporting_currency'] for row in rows} == {'USD'}
    assert not any(row['comparison_eligible'] for row in rows)
    assert all(row['ineligibility_reasons'] for row in rows)
    print('estimate basis contract selftest: PASS')


def main() -> int:
    args = parse_args()
    if args.selftest:
        run_selftest()
        return 0
    if args.as_of is None:
        raise ValueError('--as-of is required')
    symbols = _symbols(args)
    retrieval_cycle = str(
        args.retrieval_cycle or f'{args.as_of.isoformat()}-basis'
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
    basis_cfg = monitor_cfg.get('estimate_basis', {})
    if not isinstance(basis_cfg, dict) or basis_cfg.get('policy_version') != 'estimate_basis_v1':
        raise ValueError('estimate_basis_v1 config is required')
    if basis_cfg.get('reporting_currency_source') != 'fmp_income_statement':
        raise ValueError('estimate_basis reporting currency source must be fmp_income_statement')
    semantics = basis_cfg.get('provider_semantics', {})
    if not isinstance(semantics, dict):
        raise ValueError('estimate_basis.provider_semantics must be a mapping')
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
    capability = fmp_cfg['capabilities']['reporting_currency']
    probe_cfg = entitlements.get('probe', {})
    timeout = float(probe_cfg.get('timeout_sec', 30.0))
    max_bytes = int(probe_cfg.get('max_response_bytes', 2_000_000))
    max_retries = int(probe_cfg.get('max_retries', 1))
    pause = float(fmp_cfg.get('request_pause_sec', probe_cfg.get('request_pause_sec', 0.0)))
    entitlement_version = ':'.join(
        (str(entitlements['schema_version']), str(retention_cfg['policy_version']))
    )

    contract_rows: list[dict[str, Any]] = []
    reports: list[dict[str, Any]] = []
    hard_errors = 0
    for symbol in symbols:
        result = fetch_capability_payload(
            provider='fmp',
            provider_config=fmp_cfg,
            capability='reporting_currency',
            capability_config=capability,
            symbol=symbol,
            as_of=args.as_of,
            timeout_sec=timeout,
            max_response_bytes=max_bytes,
            max_retries=max_retries,
        )
        rows = build_basis_rows(
            result,
            as_of=args.as_of,
            retrieval_cycle=retrieval_cycle,
            entitlement_version=entitlement_version,
            semantics=semantics,
        )
        period_end, currency = _statement_currency(result, as_of=args.as_of)
        status = result.status if rows or result.status != 'AVAILABLE' else 'NORMALIZATION_EMPTY'
        if status not in {'AVAILABLE', 'EMPTY'}:
            hard_errors += 1
        reports.append(
            {
                'ticker': symbol,
                'status': status,
                'http_status': '' if result.http_status is None else result.http_status,
                'reporting_currency': currency,
                'statement_period_end': period_end,
                'contract_rows': len(rows),
                'eligible_rows': sum(int(row['comparison_eligible']) for row in rows),
                'response_sha256': result.response_sha256,
                'detail': result.detail,
            }
        )
        contract_rows.extend(rows)
        del result
        if pause > 0:
            time.sleep(pause)

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
            inserted, duplicates = append_metric_basis_snapshots(conn, contract_rows)
        finally:
            conn.close()

    output_dir = (
        args.output_dir.resolve()
        if args.output_dir
        else paths.output_dir / 'estimate_basis' / retrieval_cycle
    )
    report_path = output_dir / 'estimate_basis_capture.csv'
    contract_path = output_dir / 'estimate_metric_basis_contract.csv'
    manifest_path = output_dir / 'estimate_metric_basis_manifest.json'
    write_csv(report_path, REPORT_FIELDS, reports)
    contract_fields = [
        'estimate_provider',
        'currency_source_provider',
        'endpoint_id',
        'ticker',
        'metric',
        'reporting_currency',
        'statement_period_end',
        'metric_definition',
        'unit_scale',
        'per_share_basis',
        'currency_semantics_status',
        'definition_semantics_status',
        'comparison_eligible',
        'ineligibility_reasons',
        'fetched_at_utc',
        'available_at_utc',
        'retrieval_cycle',
        'response_sha256',
        'entitlement_version',
        'retention_class',
        'coverage_status',
    ]
    write_csv(contract_path, contract_fields, contract_rows)
    acceptance = 'PASS' if contract_rows and hard_errors == 0 else 'FAIL'
    inputs = [
        config_path,
        entitlements_path,
        Path(__file__).resolve(),
        Path(__file__).with_name('provider_common.py').resolve(),
        Path(__file__).with_name('monitor_common.py').resolve(),
    ]
    if args.symbols_file is not None:
        inputs.append(args.symbols_file.resolve())
    write_manifest(
        manifest_path,
        {
            'schema_version': 'estimate_metric_basis_manifest_v1',
            'acceptance': acceptance,
            'as_of_date': args.as_of.isoformat(),
            'retrieval_cycle': retrieval_cycle,
            'policy_version': basis_cfg['policy_version'],
            'symbol_count': len(symbols),
            'contract_row_count': len(contract_rows),
            'comparison_eligible_count': sum(
                int(row['comparison_eligible']) for row in contract_rows
            ),
            'inserted_count': inserted,
            'idempotent_duplicate_count': duplicates,
            'hard_error_count': hard_errors,
            'raw_payloads_retained': False,
            'shadow_only': True,
            'inputs_sha256': {str(path): sha256_file(path) for path in inputs},
            'outputs_sha256': {
                report_path.name: sha256_file(report_path),
                contract_path.name: sha256_file(contract_path),
            },
        },
    )
    eligible_count = sum(int(row['comparison_eligible']) for row in contract_rows)
    print(f'ESTIMATE METRIC BASIS: {acceptance}')
    print(
        f'contracts={len(contract_rows)}; eligible='
        f'{eligible_count}; '
        f'inserted={inserted}'
    )
    return 0 if acceptance == 'PASS' else 1


if __name__ == '__main__':
    raise SystemExit(main())
