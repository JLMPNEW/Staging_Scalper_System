#!/usr/bin/env python3
"""Link PIT provider forecasts to exact-period actuals using a strict pre-report cutoff."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
import tempfile
from collections import defaultdict
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from portfolio_layer.core.config import cfg_get, load_yaml, resolve_path  # noqa: E402
from portfolio_layer.core.contracts import sha256_file, write_csv, write_manifest  # noqa: E402
from portfolio_layer.core.paths import ensure_not_prod_path, resolve_runtime_paths  # noqa: E402
from portfolio_layer.expectations_monitor.estimate_policy import (  # noqa: E402
    canonicalize_snapshot,
)
from portfolio_layer.expectations_monitor.monitor_common import (  # noqa: E402
    append_actual_outcomes,
    append_estimate_snapshots,
    append_fiscal_period_resolutions,
    append_metric_basis_snapshots,
    connect_monitor_db,
    writer_lock,
)


DEFAULT_CONFIG = PACKAGE_ROOT / 'config.yaml'
CANDIDATE_FIELDS = [
    'evaluation_cycle',
    'estimate_provider',
    'ticker',
    'metric',
    'canonical_period',
    'report_date',
    'fiscal_period_end',
    'snapshot_id',
    'outcome_id',
    'resolution_id',
    'basis_snapshot_id',
    'forecast_available_at_utc',
    'outcome_available_at_utc',
    'forecast_lead_days',
    'forecast_value',
    'actual_value',
    'evaluation_status',
    'ineligibility_reasons',
    'error_value',
    'absolute_error',
    'normalized_absolute_error',
]
LINK_FIELDS = [
    'link_id',
    'snapshot_id',
    'outcome_id',
    'resolution_id',
    'evaluation_cycle',
    'basis_snapshot_id',
    'estimate_provider',
    'ticker',
    'metric',
    'canonical_period',
    'report_date',
    'fiscal_period_end',
    'linked_at_utc',
    'forecast_available_at_utc',
    'outcome_available_at_utc',
    'cutoff_policy',
    'forecast_lead_days',
    'forecast_value',
    'actual_value',
    'evaluation_status',
    'ineligibility_reasons',
    'error_value',
    'absolute_error',
    'normalized_absolute_error',
    'normalized_sha256',
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, default=DEFAULT_CONFIG)
    parser.add_argument('--db', type=Path)
    parser.add_argument('--as-of', type=date.fromisoformat)
    parser.add_argument('--evaluation-cycle')
    parser.add_argument('--output-dir', type=Path)
    parser.add_argument('--selftest', action='store_true')
    return parser.parse_args()


def _digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(',', ':')).encode('utf-8')
    ).hexdigest()


def _aware(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if parsed.tzinfo is None:
        raise ValueError(f'PIT timestamp lacks timezone: {value!r}')
    return parsed


def _latest_before(
    rows: Iterable[dict[str, Any]],
    *,
    report_date: date,
    timezone_name: str,
) -> dict[str, Any] | None:
    zone = ZoneInfo(timezone_name)
    eligible = [
        row
        for row in rows
        if _aware(str(row['available_at_utc'])).astimezone(zone).date() < report_date
    ]
    return max(eligible, key=lambda row: str(row['available_at_utc'])) if eligible else None


def _outcome_groups(
    rows: Iterable[dict[str, Any]], *, tolerance: float
) -> dict[tuple[str, str, str, str], tuple[dict[str, Any], bool]]:
    grouped: dict[tuple[str, str, str, str], list[dict[str, Any]]] = defaultdict(
        list
    )
    for row in rows:
        grouped[
            (
                str(row['provider']),
                str(row['ticker']),
                str(row['report_date']),
                str(row['metric']),
            )
        ].append(row)
    output: dict[tuple[str, str, str, str], tuple[dict[str, Any], bool]] = {}
    for key, values in grouped.items():
        ordered = sorted(values, key=lambda row: str(row['available_at_utc']))
        first_value = float(ordered[0]['actual_value'])
        conflict = any(
            not math.isclose(
                float(row['actual_value']),
                first_value,
                rel_tol=tolerance,
                abs_tol=tolerance,
            )
            for row in ordered[1:]
        )
        output[key] = (ordered[0], conflict)
    return output


def build_evaluations(
    conn: Any,
    *,
    as_of: date,
    evaluation_cycle: str,
    policy: dict[str, Any],
    relative_floors: dict[str, float],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    as_of_end = f'{as_of.isoformat()}T23:59:59+00:00'
    timezone_name = str(policy['timezone'])
    maximum_age = int(policy['maximum_forecast_age_days'])
    tolerance = float(policy['actual_conflict_tolerance'])
    cutoff_policy = str(policy['forecast_cutoff_policy'])

    resolutions_raw = [
        dict(row)
        for row in conn.execute(
            'SELECT * FROM provider_fiscal_period_resolutions '
            'WHERE resolution_eligible=1 AND available_at_utc<=? '
            'ORDER BY available_at_utc,resolution_id',
            (as_of_end,),
        ).fetchall()
    ]
    resolutions: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in resolutions_raw:
        resolutions[(str(row['ticker']), str(row['report_date']))].append(row)

    outcome_rows = [
        dict(row)
        for row in conn.execute(
            'SELECT * FROM provider_actual_outcomes_v2 '
            'WHERE coverage_status=\'available\' AND available_at_utc<=? '
            'ORDER BY available_at_utc,outcome_id',
            (as_of_end,),
        ).fetchall()
    ]
    outcomes = _outcome_groups(outcome_rows, tolerance=tolerance)

    snapshots_raw = [
        dict(row)
        for row in conn.execute(
            'SELECT * FROM provider_estimate_snapshots '
            'WHERE coverage_status=\'available\' AND available_at_utc<=?',
            (as_of_end,),
        ).fetchall()
    ]
    snapshots: dict[tuple[str, str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in snapshots_raw:
        canonical = canonicalize_snapshot(row)
        if canonical.canonical_period != 'quarterly':
            continue
        snapshots[
            (
                canonical.provider,
                canonical.ticker,
                canonical.metric,
                canonical.fiscal_period_end,
            )
        ].append(row)

    basis_rows = [
        dict(row)
        for row in conn.execute(
            'SELECT * FROM provider_metric_basis_snapshots '
            'WHERE coverage_status=\'available\' AND available_at_utc<=?',
            (as_of_end,),
        ).fetchall()
    ]
    bases: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in basis_rows:
        bases[
            (str(row['estimate_provider']), str(row['ticker']), str(row['metric']))
        ].append(row)

    candidates: list[dict[str, Any]] = []
    links: list[dict[str, Any]] = []
    for (
        outcome_provider,
        ticker,
        report_text,
        metric,
    ), (outcome, actual_conflict) in sorted(outcomes.items()):
        report_date = date.fromisoformat(report_text)
        resolution_options = resolutions.get((ticker, report_text), [])
        period_ends = {str(row['fiscal_period_end']) for row in resolution_options}
        resolution = resolution_options[0] if len(period_ends) == 1 else None
        provider = outcome_provider
        reasons: list[str] = []
        if not int(outcome.get('evaluation_eligible', 0)):
            raw_reasons = str(outcome.get('ineligibility_reasons', '')).strip()
            reasons.extend(
                reason for reason in raw_reasons.split(',') if reason
            )
            if not raw_reasons:
                reasons.append('actual_outcome_not_evaluation_eligible')
        if actual_conflict:
            reasons.append('actual_value_conflict')
        if not resolution_options:
            reasons.append('exact_fiscal_period_resolution_missing')
        elif len(period_ends) != 1:
            reasons.append('exact_fiscal_period_resolution_conflict')
        fiscal_period_end = str(resolution['fiscal_period_end']) if resolution else ''
        forecast_rows = snapshots.get(
            (provider, ticker, metric, fiscal_period_end), []
        )
        forecast = _latest_before(
            forecast_rows,
            report_date=report_date,
            timezone_name=timezone_name,
        )
        if forecast is None:
            reasons.append('strict_pre_report_forecast_missing')
        canonical = canonicalize_snapshot(forecast) if forecast is not None else None
        if canonical is not None and canonical.quality_status == 'FAIL':
            reasons.extend(canonical.quality_reasons)
        forecast_available = str(forecast['available_at_utc']) if forecast else ''
        zone = ZoneInfo(timezone_name)
        forecast_local_date = (
            _aware(forecast_available).astimezone(zone).date()
            if forecast_available
            else None
        )
        lead_days = (
            float((report_date - forecast_local_date).days)
            if forecast_local_date is not None
            else None
        )
        if lead_days is not None and lead_days > maximum_age:
            reasons.append('forecast_age_exceeds_policy')

        basis = None
        if forecast is not None:
            available_bases = [
                row
                for row in bases.get((provider, ticker, metric), [])
                if str(row['available_at_utc']) <= forecast_available
            ]
            if available_bases:
                basis = max(
                    available_bases, key=lambda row: str(row['available_at_utc'])
                )
        if basis is None:
            reasons.append('metric_basis_missing_at_forecast_cutoff')
        elif not int(basis['comparison_eligible']):
            reasons.append('metric_basis_not_comparison_eligible')
        if (
            basis is not None
            and str(outcome['reporting_currency'])
            and str(basis['reporting_currency'])
            and str(outcome['reporting_currency']) != str(basis['reporting_currency'])
        ):
            reasons.append('actual_forecast_currency_mismatch')

        forecast_value = (
            float(canonical.estimate_average)
            if canonical is not None and canonical.estimate_average is not None
            else None
        )
        actual_value = float(outcome['actual_value'])
        eligible = not reasons and forecast_value is not None
        error_value = None
        if eligible:
            assert forecast_value is not None
            error_value = forecast_value - actual_value
        absolute_error = abs(error_value) if error_value is not None else None
        floor = float(relative_floors[metric])
        normalized_error = (
            absolute_error / max(abs(actual_value), floor)
            if absolute_error is not None
            else None
        )
        status = 'eligible' if eligible else 'ineligible'
        candidate = {
            'evaluation_cycle': evaluation_cycle,
            'estimate_provider': provider,
            'ticker': ticker,
            'metric': metric,
            'canonical_period': 'quarterly',
            'report_date': report_text,
            'fiscal_period_end': fiscal_period_end,
            'snapshot_id': str(forecast['snapshot_id']) if forecast else '',
            'outcome_id': str(outcome['outcome_id']),
            'resolution_id': str(resolution['resolution_id']) if resolution else '',
            'basis_snapshot_id': str(basis['basis_snapshot_id']) if basis else '',
            'forecast_available_at_utc': forecast_available,
            'outcome_available_at_utc': str(outcome['available_at_utc']),
            'forecast_lead_days': '' if lead_days is None else lead_days,
            'forecast_value': '' if forecast_value is None else forecast_value,
            'actual_value': actual_value,
            'evaluation_status': status,
            'ineligibility_reasons': ','.join(dict.fromkeys(reasons)),
            'error_value': '' if error_value is None else error_value,
            'absolute_error': '' if absolute_error is None else absolute_error,
            'normalized_absolute_error': (
                '' if normalized_error is None else normalized_error
            ),
        }
        candidates.append(candidate)
        if (
            forecast is None
            or resolution is None
            or forecast_value is None
            or lead_days is None
        ):
            continue
        linked_at = max(
            value
            for value in (
                forecast_available,
                str(outcome['available_at_utc']),
                str(resolution['available_at_utc']),
                str(basis['available_at_utc']) if basis else '',
            )
            if value
        )
        link = {
            'snapshot_id': str(forecast['snapshot_id']),
            'outcome_id': str(outcome['outcome_id']),
            'resolution_id': str(resolution['resolution_id']),
            'evaluation_cycle': evaluation_cycle,
            'basis_snapshot_id': str(basis['basis_snapshot_id']) if basis else '',
            'estimate_provider': provider,
            'ticker': ticker,
            'metric': metric,
            'canonical_period': 'quarterly',
            'report_date': report_text,
            'fiscal_period_end': fiscal_period_end,
            'linked_at_utc': linked_at,
            'forecast_available_at_utc': forecast_available,
            'outcome_available_at_utc': str(outcome['available_at_utc']),
            'cutoff_policy': cutoff_policy,
            'forecast_lead_days': lead_days,
            'forecast_value': forecast_value,
            'actual_value': actual_value,
            'evaluation_status': status,
            'ineligibility_reasons': candidate['ineligibility_reasons'],
            'error_value': error_value,
            'absolute_error': absolute_error,
            'normalized_absolute_error': normalized_error,
        }
        identity = {
            field: link[field]
            for field in (
                'snapshot_id',
                'outcome_id',
                'resolution_id',
                'evaluation_cycle',
            )
        }
        link['link_id'] = _digest(identity)
        link['normalized_sha256'] = _digest(link)
        links.append(link)
    return candidates, links


def append_links(conn: Any, links: Iterable[dict[str, Any]]) -> tuple[int, int]:
    inserted = 0
    duplicates = 0
    columns = LINK_FIELDS
    placeholders = ','.join('?' for _ in columns)
    with conn:
        for link in links:
            existing = conn.execute(
                'SELECT normalized_sha256 FROM provider_forecast_outcome_links_v3 '
                'WHERE link_id=?',
                (link['link_id'],),
            ).fetchone()
            if existing is not None:
                if str(existing['normalized_sha256']) != str(link['normalized_sha256']):
                    raise RuntimeError('Forecast/outcome link identity collision')
                duplicates += 1
                continue
            conn.execute(
                f"INSERT INTO provider_forecast_outcome_links_v3({','.join(columns)}) "
                f'VALUES ({placeholders})',
                tuple(link[field] for field in columns),
            )
            inserted += 1
    return inserted, duplicates


def evaluation_validation_errors(
    *,
    candidates: list[dict[str, Any]],
    links: list[dict[str, Any]],
    stored_links: list[dict[str, Any]],
    evaluation_cycle: str,
) -> list[str]:
    candidate_keys = [
        (
            row['estimate_provider'],
            row['ticker'],
            row['metric'],
            row['canonical_period'],
            row['report_date'],
            row['fiscal_period_end'],
            row['snapshot_id'],
            row['outcome_id'],
            row['resolution_id'],
        )
        for row in candidates
    ]
    errors: list[str] = []
    if len(candidate_keys) != len(set(candidate_keys)):
        errors.append('duplicate_candidate_identity')
    if any(
        row['evaluation_status'] not in {'eligible', 'ineligible'}
        for row in candidates
    ):
        errors.append('invalid_evaluation_status')

    generated_by_id = {str(row['link_id']): row for row in links}
    stored_by_id = {str(row['link_id']): row for row in stored_links}
    if len(generated_by_id) != len(links):
        errors.append('duplicate_generated_link_identity')
    if len(stored_by_id) != len(stored_links):
        errors.append('duplicate_stored_link_identity')
    if generated_by_id.keys() != stored_by_id.keys():
        errors.append(
            f'link_identity_mismatch:{len(generated_by_id)}:{len(stored_by_id)}'
        )
    elif any(
        str(generated_by_id[link_id]['normalized_sha256'])
        != str(stored_by_id[link_id]['normalized_sha256'])
        for link_id in generated_by_id
    ):
        errors.append('stored_link_payload_mismatch')

    eligible_candidates = sum(
        row['evaluation_status'] == 'eligible' for row in candidates
    )
    eligible_links = sum(row['evaluation_status'] == 'eligible' for row in links)
    stored_eligible_links = sum(
        row['evaluation_status'] == 'eligible' for row in stored_links
    )
    if eligible_candidates != eligible_links or eligible_links != stored_eligible_links:
        errors.append(
            'eligible_link_count_mismatch:'
            f'{eligible_candidates}:{eligible_links}:{stored_eligible_links}'
        )
    if any(
        str(row.get('evaluation_cycle', '')) != evaluation_cycle
        for row in stored_links
    ):
        errors.append('stored_cycle_mismatch')
    return errors


def run_selftest() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        conn = connect_monitor_db(Path(tmp) / 'monitor.sqlite', timeout_sec=1.0)
        try:
            append_estimate_snapshots(
                conn,
                [
                    {
                        'provider': 'alpha_vantage',
                        'endpoint_id': 'earnings_estimates',
                        'ticker': 'AAA',
                        'fiscal_period_end': '2026-06-30',
                        'fiscal_period': 'fiscal quarter',
                        'estimate_type': 'eps_fiscal_quarter',
                        'estimate_average': 2.0,
                        'fetched_at_utc': '2026-07-29T20:00:00+00:00',
                        'available_at_utc': '2026-07-29T20:00:00+00:00',
                        'retrieval_cycle': 'forecast',
                        'response_sha256': 'a' * 64,
                        'entitlement_version': 'test',
                        'retention_class': 'provisional_user_authorized',
                        'coverage_status': 'available',
                    }
                ],
            )
            append_metric_basis_snapshots(
                conn,
                [
                    {
                        'estimate_provider': 'alpha_vantage',
                        'currency_source_provider': 'fmp',
                        'endpoint_id': 'reporting_currency',
                        'ticker': 'AAA',
                        'metric': 'eps',
                        'reporting_currency': 'USD',
                        'statement_period_end': '2025-12-31',
                        'metric_definition': 'diluted_adjusted_eps',
                        'unit_scale': 'currency_per_share',
                        'per_share_basis': 'diluted',
                        'currency_semantics_status': 'verified',
                        'definition_semantics_status': 'verified',
                        'comparison_eligible': 1,
                        'ineligibility_reasons': '',
                        'fetched_at_utc': '2026-07-29T19:00:00+00:00',
                        'available_at_utc': '2026-07-29T19:00:00+00:00',
                        'retrieval_cycle': 'basis',
                        'response_sha256': 'b' * 64,
                        'entitlement_version': 'test',
                        'retention_class': 'provisional_user_authorized',
                        'coverage_status': 'available',
                    }
                ],
            )
            append_fiscal_period_resolutions(
                conn,
                [
                    {
                        'source_provider': 'alpha_vantage',
                        'endpoint_id': 'earnings_history',
                        'ticker': 'AAA',
                        'report_date': '2026-07-30',
                        'fiscal_period_end': '2026-06-30',
                        'fiscal_period': 'quarterly',
                        'report_time': 'post-market',
                        'resolution_status': 'exact_provider_report_date_match',
                        'resolution_eligible': 1,
                        'ineligibility_reasons': '',
                        'fetched_at_utc': '2026-07-31T20:00:00+00:00',
                        'available_at_utc': '2026-07-31T20:00:00+00:00',
                        'retrieval_cycle': 'resolution',
                        'response_sha256': 'c' * 64,
                        'entitlement_version': 'test',
                        'retention_class': 'provisional_user_authorized',
                        'coverage_status': 'available',
                    }
                ],
            )
            append_actual_outcomes(
                conn,
                [
                    {
                        'provider': 'alpha_vantage',
                        'endpoint_id': 'earnings_history',
                        'ticker': 'AAA',
                        'report_date': '2026-07-30',
                        'fiscal_period_end': '',
                        'outcome_period_status': 'report_date_only_unmapped',
                        'metric': 'eps',
                        'actual_value': 2.5,
                        'reporting_currency': 'USD',
                        'metric_basis_id': '',
                        'metric_basis_status': 'fail_closed',
                        'provider_updated_at_raw': '2026-07-30',
                        'provider_published_at_utc': '',
                        'fetched_at_utc': '2026-07-31T21:00:00+00:00',
                        'available_at_utc': '2026-07-31T21:00:00+00:00',
                        'retrieval_cycle': 'actual',
                        'response_sha256': 'd' * 64,
                        'entitlement_version': 'test',
                        'retention_class': 'provisional_user_authorized',
                        'coverage_status': 'available',
                        'evaluation_eligible': 0,
                        'ineligibility_reasons': 'actual_publication_time_unverified',
                    }
                ],
            )
            candidates, links = build_evaluations(
                conn,
                as_of=date(2026, 7, 31),
                evaluation_cycle='selftest',
                policy={
                    'timezone': 'America/New_York',
                    'maximum_forecast_age_days': 180,
                    'actual_conflict_tolerance': 1.0e-9,
                    'forecast_cutoff_policy': (
                        'strictly_before_report_date_us_eastern'
                    ),
                },
                relative_floors={'eps': 0.01, 'revenue': 1.0},
            )
            alpha = next(
                row for row in candidates if row['estimate_provider'] == 'alpha_vantage'
            )
            assert alpha['evaluation_status'] == 'ineligible'
            assert (
                'actual_publication_time_unverified'
                in alpha['ineligibility_reasons']
            )
            assert alpha['error_value'] == ''
            assert len(links) == 1
            assert append_links(conn, links) == (1, 0)
            assert append_links(conn, links) == (0, 1)
            stored_links = [
                dict(row)
                for row in conn.execute(
                    'SELECT * FROM provider_forecast_outcome_links_v3 '
                    'WHERE evaluation_cycle=\'selftest\''
                ).fetchall()
            ]
            assert evaluation_validation_errors(
                candidates=candidates,
                links=links,
                stored_links=stored_links,
                evaluation_cycle='selftest',
            ) == []
        finally:
            conn.close()
    print('forecast/outcome linker selftest: PASS')


def main() -> int:
    args = parse_args()
    if args.selftest:
        run_selftest()
        return 0
    if args.as_of is None:
        raise ValueError('--as-of is required')
    evaluation_cycle = str(
        args.evaluation_cycle or f'{args.as_of.isoformat()}-forecast-evaluation'
    ).strip()
    if not evaluation_cycle or any(char.isspace() for char in evaluation_cycle):
        raise ValueError('Invalid evaluation cycle')
    config_path = args.config.resolve()
    config = load_yaml(config_path)
    paths = resolve_runtime_paths(config, config_path)
    monitor_cfg = cfg_get(config, 'expectations_monitor', {})
    if not isinstance(monitor_cfg, dict):
        raise ValueError('expectations_monitor config must be a mapping')
    policy = monitor_cfg.get('forecast_evaluation', {})
    required_policy = {
        'policy_version': 'forecast_evaluation_v1',
        'fiscal_period_resolver_provider': 'alpha_vantage',
        'fiscal_period_resolver_endpoint': 'earnings_history',
        'forecast_cutoff_policy': 'strictly_before_report_date_us_eastern',
        'timezone': 'America/New_York',
        'require_exact_fiscal_period_end': True,
        'require_eligible_metric_basis': True,
    }
    if not isinstance(policy, dict):
        raise ValueError('forecast_evaluation config must be a mapping')
    for field, expected in required_policy.items():
        if policy.get(field) != expected:
            raise ValueError(f'forecast_evaluation.{field} must be {expected!r}')
    if int(policy.get('maximum_forecast_age_days', 0)) < 1:
        raise ValueError('maximum_forecast_age_days must be positive')
    tolerance = float(policy.get('actual_conflict_tolerance', 0.0))
    if tolerance <= 0:
        raise ValueError('actual_conflict_tolerance must be positive')
    reconciliation = monitor_cfg.get('provider_reconciliation', {})
    if not isinstance(reconciliation, dict):
        raise ValueError('provider_reconciliation config must be a mapping')
    floors = reconciliation.get('relative_difference_floor', {})
    relative_floors = {metric: float(floors[metric]) for metric in ('eps', 'revenue')}
    if any(value <= 0 for value in relative_floors.values()):
        raise ValueError('Provider relative-difference floors must be positive')

    db_path = ensure_not_prod_path(
        args.db.resolve()
        if args.db
        else resolve_path(
            monitor_cfg.get('database_path', 'db/expectations_monitor.sqlite'),
            base_dir=config_path.parent,
        ),
        label='expectations monitor database',
    )
    timeout = float(monitor_cfg.get('writer_lock_timeout_sec', 30.0))
    lock_path = db_path.with_suffix(db_path.suffix + '.writer.lock')
    with writer_lock(lock_path, timeout_sec=timeout):
        conn = connect_monitor_db(db_path, timeout_sec=timeout)
        try:
            candidates, links = build_evaluations(
                conn,
                as_of=args.as_of,
                evaluation_cycle=evaluation_cycle,
                policy=policy,
                relative_floors=relative_floors,
            )
            inserted, duplicates = append_links(conn, links)
            stored_links = [
                dict(row)
                for row in conn.execute(
                    'SELECT * FROM provider_forecast_outcome_links_v3 '
                    'WHERE evaluation_cycle=? ORDER BY ticker,metric,estimate_provider',
                    (evaluation_cycle,),
                ).fetchall()
            ]
        finally:
            conn.close()

    output_dir = (
        args.output_dir.resolve()
        if args.output_dir
        else paths.output_dir / 'provider_forecast_evaluations' / evaluation_cycle
    )
    candidates_path = output_dir / 'forecast_evaluation_candidates.csv'
    links_path = output_dir / 'forecast_outcome_links.csv'
    manifest_path = output_dir / 'forecast_evaluation_manifest.json'
    write_csv(candidates_path, CANDIDATE_FIELDS, candidates)
    write_csv(links_path, LINK_FIELDS, stored_links)
    eligible = sum(row['evaluation_status'] == 'eligible' for row in candidates)
    eligible_links = sum(row['evaluation_status'] == 'eligible' for row in links)
    validation_errors = evaluation_validation_errors(
        candidates=candidates,
        links=links,
        stored_links=stored_links,
        evaluation_cycle=evaluation_cycle,
    )
    acceptance = (
        'FAIL'
        if validation_errors
        else 'PASS'
        if candidates
        else 'PASS_NO_CANDIDATES'
    )
    inputs = [
        config_path,
        Path(__file__).resolve(),
        Path(__file__).with_name('estimate_policy.py').resolve(),
        Path(__file__).with_name('monitor_common.py').resolve(),
    ]
    write_manifest(
        manifest_path,
        {
            'schema_version': 'forecast_evaluation_manifest_v1',
            'acceptance': acceptance,
            'shadow_only': True,
            'as_of_date': args.as_of.isoformat(),
            'evaluation_cycle': evaluation_cycle,
            'candidate_count': len(candidates),
            'linked_count': len(stored_links),
            'eligible_count': eligible,
            'eligible_linked_count': eligible_links,
            'ineligible_linked_count': len(stored_links) - eligible_links,
            'inserted_count': inserted,
            'duplicate_count': duplicates,
            'validation_errors': validation_errors,
            'forecast_cutoff_policy': policy['forecast_cutoff_policy'],
            'same_report_date_forecasts_excluded': True,
            'metric_basis_required': True,
            'inputs_sha256': {str(path): sha256_file(path) for path in inputs},
            'outputs_sha256': {
                candidates_path.name: sha256_file(candidates_path),
                links_path.name: sha256_file(links_path),
            },
            'generated_at_utc': datetime.now(timezone.utc)
            .replace(microsecond=0)
            .isoformat(),
        },
    )
    print(f'FORECAST OUTCOME LINKER: {acceptance}')
    print(
        f'candidates={len(candidates)} links={len(stored_links)} eligible={eligible}'
    )
    return 1 if validation_errors else 0


if __name__ == '__main__':
    raise SystemExit(main())
