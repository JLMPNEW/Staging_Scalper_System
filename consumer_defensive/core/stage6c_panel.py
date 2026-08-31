from __future__ import annotations

import bisect
import hashlib
import json
import math
import sqlite3
from collections import defaultdict
from datetime import date
from pathlib import Path
from typing import Any, Iterable

from factor_validation import sha256_file

from .config import ConfigBundle, cfg_get
from .db import utc_now
from .market_data import SELECTION_PURPOSE, write_csv, write_json
from .specialized_metrics import (
    _run_observations,
    bootstrap_stage6b,
    stage6b_policy_sha256,
)
from .stage6c_schema import STAGE6C_MIGRATION_SHA256, ensure_stage6c_schema
from .stage3_runtime import DEFAULT_TERMINAL_POLICY
from .terminal_events import load_terminal_event_policy, terminal_horizon_value


MODEL_FAMILY = 'consumer_defensive'
PANEL_VERSION = 'consumer_defensive_stage6c_specialized_pit_panel_v3'
DEFAULT_FRESHNESS_DAYS = 550
DEFAULT_ENTRY_LAG = 1
MINIMUM_EVALUATION_DATES = 12
_HASH_COLUMNS = (
    'asof_date', 'ticker', 'cohort_id', 'applicability_subtype', 'factor_id',
    'factor_value', 'unit', 'direction_hint', 'availability_status',
    'source_accepted_at', 'source_period_end', 'source_age_days',
    'source_observation_sha256', 'source_definition_version',
    'membership_eligible_flag', 'investable_flag', 'sample_role',
    'market_regime', 'input_cost_regime', 'terminal_event_status',
    'forward_total_return_21d', 'forward_total_return_63d',
    'forward_total_return_126d', 'forward_xlp_residual_return_21d',
    'forward_xlp_residual_return_63d',
    'forward_xlp_residual_return_126d',
    'forward_spy_beta_residual_return_21d',
    'forward_spy_beta_residual_return_63d',
    'forward_spy_beta_residual_return_126d',
)
_MANIFEST_HASH_COLUMNS = (
    'factor_id', 'source_availability_class', 'cohorts_json',
    'applicability_subtypes_json', 'unit_family', 'direction_hint',
    'factor_direction', 'production_status', 'definition_versions_json',
    'factor_validation_eligible', 'exclusion_reason',
)


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(',', ':'),
        ensure_ascii=True,
        allow_nan=False,
    )


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode('utf-8')).hexdigest()


def stage6c_config_payload(bundle: ConfigBundle) -> dict[str, Any]:
    return {
        'schema_version': 'consumer_defensive_stage6c_config_v1',
        'requested_history_start': str(cfg_get(
            bundle.payload,
            'historical_contract.requested_snapshot_start',
        )),
        'horizons': sorted(int(value) for value in cfg_get(
            bundle.payload, 'factor_validation.horizons'
        )),
        'strict_oos_start_date': str(cfg_get(
            bundle.payload, 'oos_provenance.strict_oos_start_date', ''
        ) or ''),
        'evaluation_frequency': 'monthly',
        'entry_lag_trading_days': DEFAULT_ENTRY_LAG,
        'terminal_event_policy_sha256': sha256_file(
            DEFAULT_TERMINAL_POLICY
        ),
    }


def stage6c_config_sha256(bundle: ConfigBundle) -> str:
    return _sha256(stage6c_config_payload(bundle))


def _finite(value: Any) -> float | None:
    if value is None:
        return None
    parsed = float(value)
    return parsed if math.isfinite(parsed) else None


def _row_hash(row: dict[str, Any]) -> str:
    return _sha256({column: row.get(column) for column in _HASH_COLUMNS})


def _manifest_hash(row: dict[str, Any]) -> str:
    return _sha256({
        column: row.get(column) for column in _MANIFEST_HASH_COLUMNS
    })


def _metric_applicable(metric: dict[str, Any], *, cohort: str, subtype: str) -> bool:
    cohorts = set(metric['cohorts'])
    subtypes = set(metric['subtypes'])
    return cohort in cohorts and (
        'all_operating_issuers' in subtypes or subtype in subtypes
    )


def _factor_direction(direction_hint: str) -> str | None:
    if direction_hint == 'positive':
        return 'higher_is_better'
    if direction_hint == 'negative':
        return 'lower_is_better'
    return None


def _latest_stage6b_run(conn: sqlite3.Connection, *, as_of: str) -> sqlite3.Row:
    row = conn.execute(
        '''SELECT * FROM stage6b_specialized_run
           WHERE asof_date=? AND status='measurement_only_complete'
           ORDER BY stage6b_run_id DESC LIMIT 1''',
        (as_of,),
    ).fetchone()
    if row is None:
        raise RuntimeError(
            f'Stage 6C requires a completed Stage 6B run for {as_of}.'
        )
    return row


def _metrics(
    conn: sqlite3.Connection,
    *,
    observations: Iterable[sqlite3.Row],
) -> dict[str, dict[str, Any]]:
    definitions: dict[str, set[str]] = defaultdict(set)
    for row in observations:
        if (
            str(row['production_status']) == 'measurement_only'
            and str(row['evidence_status']) == 'accepted_measurement_only'
            and row['numeric_value'] is not None
        ):
            definitions[str(row['metric_id'])].add(
                str(row['definition_version'])
            )
    output: dict[str, dict[str, Any]] = {}
    for row in conn.execute('SELECT * FROM dim_specialized_metric ORDER BY metric_id'):
        metric_id = str(row['metric_id'])
        output[metric_id] = {
            'metric_id': metric_id,
            'cohorts': tuple(json.loads(str(row['cohorts_json']))),
            'subtypes': tuple(json.loads(str(row['applicability_subtypes_json']))),
            'unit_family': str(row['unit_family']),
            'direction_hint': str(row['direction_hint']),
            'source_availability_class': str(row['source_availability_class']),
            'production_status': str(row['production_status']),
            'production_weight': float(row['production_weight']),
            'definition_versions': tuple(sorted(definitions.get(metric_id, set()))),
        }
    if not output:
        raise RuntimeError('Stage 6C metric registry is empty.')
    if any(
        metric['production_weight'] != 0.0
        or metric['production_status'] not in {'research_candidate', 'measurement_only'}
        for metric in output.values()
    ):
        raise RuntimeError('Stage 6C metrics must remain zero-weight research candidates.')
    return output


def _taxonomy(conn: sqlite3.Connection) -> dict[str, dict[str, str]]:
    return {
        str(row['ticker']): {
            'cohort_id': str(row['calibration_cohort_id']),
            'subtype': str(row['applicability_subtype'] or ''),
        }
        for row in conn.execute(
            '''SELECT ticker,calibration_cohort_id,applicability_subtype
               FROM dim_consumer_defensive_taxonomy
               WHERE model_family=? ORDER BY ticker''',
            (MODEL_FAMILY,),
        )
    }


def _memberships(conn: sqlite3.Connection) -> dict[str, list[sqlite3.Row]]:
    output: dict[str, list[sqlite3.Row]] = defaultdict(list)
    for row in conn.execute(
        '''SELECT ticker,start_date,COALESCE(end_date,'9999-12-31') AS end_date,
                  point_in_time_flag,live_investable_flag,
                  historical_calibration_eligible_flag
           FROM dim_universe_membership WHERE model_family=?
           ORDER BY ticker,start_date''',
        (MODEL_FAMILY,),
    ):
        output[str(row['ticker'])].append(row)
    return output


def _membership_on(
    rows: Iterable[sqlite3.Row], *, as_of: str
) -> tuple[bool, bool]:
    eligible = False
    live = False
    for row in rows:
        if str(row['start_date']) <= as_of <= str(row['end_date']):
            if int(row['point_in_time_flag']) != 1:
                continue
            eligible = eligible or bool(
                int(row['historical_calibration_eligible_flag'])
                or int(row['live_investable_flag'])
            )
            live = live or bool(int(row['live_investable_flag']))
    return eligible, live


def _selected_sources(conn: sqlite3.Connection, *, as_of: str) -> dict[str, str]:
    rows = list(conn.execute(
        '''SELECT ticker,selected_source_id,selection_asof_date,coverage_status
           FROM dim_price_series_selection WHERE purpose=? ORDER BY ticker''',
        (SELECTION_PURPOSE,),
    ))
    if not rows:
        raise RuntimeError('Stage 6C requires authoritative Stage 3 price selections.')
    stale = [
        str(row['ticker']) for row in rows
        if str(row['selection_asof_date']) != as_of
    ]
    if stale:
        raise RuntimeError(
            'Stage 6C price selections are not exact-as-of: '
            f'{stale[:10]}'
        )
    rejected = [
        str(row['ticker']) for row in rows
        if str(row['coverage_status']) not in {'complete', 'fallback'}
    ]
    if rejected:
        raise RuntimeError(f'Stage 6C has rejected price selections: {rejected[:10]}')
    output = {
        str(row['ticker']): str(row['selected_source_id']) for row in rows
    }
    if {'SPY', 'XLP'} - set(output):
        raise RuntimeError('Stage 6C requires exact SPY and XLP price selections.')
    return output


def _price_maps(
    conn: sqlite3.Connection,
    *,
    selected_sources: dict[str, str],
    history_start: str,
    as_of: str,
) -> dict[str, dict[str, float]]:
    output: dict[str, dict[str, float]] = {}
    for ticker, source_id in selected_sources.items():
        rows = conn.execute(
            '''SELECT bar_date,adjusted_close FROM fact_price_ohlcv
               WHERE ticker=? AND source_id=? AND bar_date BETWEEN ? AND ?
                 AND adjusted_close>0 ORDER BY bar_date''',
            (ticker, source_id, history_start, as_of),
        )
        output[ticker] = {
            str(row[0]): float(row[1]) for row in rows
            if row[1] is not None and math.isfinite(float(row[1]))
        }
    return output


def _evaluation_dates(
    spy_prices: dict[str, float],
    *,
    history_start: str,
    as_of: str,
    entry_lag: int,
    maximum_horizon: int,
) -> tuple[list[str], list[str]]:
    calendar = sorted(
        value for value in spy_prices if history_start <= value <= as_of
    )
    maximum_index = len(calendar) - 1 - entry_lag - maximum_horizon
    if maximum_index < 0:
        raise RuntimeError('Stage 6C has insufficient SPY history for forward labels.')
    monthly: dict[str, str] = {}
    for value in calendar[:maximum_index + 1]:
        monthly[value[:7]] = value
    dates = sorted(monthly.values())
    if len(dates) < MINIMUM_EVALUATION_DATES:
        raise RuntimeError(
            f'Stage 6C has only {len(dates)} evaluation dates; '
            f'requires {MINIMUM_EVALUATION_DATES}.'
        )
    return calendar, dates


def _trailing_beta(
    ticker_prices: dict[str, float],
    spy_prices: dict[str, float],
    *,
    as_of: str,
    lookback: int = 126,
    minimum: int = 63,
) -> float | None:
    dates = sorted(set(ticker_prices).intersection(spy_prices))
    end = bisect.bisect_right(dates, as_of)
    aligned = dates[max(0, end - lookback - 1):end]
    if len(aligned) < minimum + 1:
        return None
    ticker_returns: list[float] = []
    spy_returns: list[float] = []
    for previous, current in zip(aligned, aligned[1:]):
        ticker_returns.append(ticker_prices[current] / ticker_prices[previous] - 1.0)
        spy_returns.append(spy_prices[current] / spy_prices[previous] - 1.0)
    spy_mean = sum(spy_returns) / len(spy_returns)
    ticker_mean = sum(ticker_returns) / len(ticker_returns)
    variance = sum((value - spy_mean) ** 2 for value in spy_returns)
    if variance <= 0.0:
        return None
    covariance = sum(
        (left - ticker_mean) * (right - spy_mean)
        for left, right in zip(ticker_returns, spy_returns, strict=True)
    )
    beta = covariance / variance
    return beta if math.isfinite(beta) and -10.0 <= beta <= 10.0 else None


def _forward_labels(
    conn: sqlite3.Connection,
    *,
    ticker: str,
    evaluation_date: str,
    calendar: list[str],
    prices: dict[str, dict[str, float]],
    entry_lag: int,
    horizons: tuple[int, ...],
    terminal_policy: Any,
) -> tuple[dict[str, float | None], str, bool]:
    labels: dict[str, float | None] = {}
    evaluation_index = bisect.bisect_left(calendar, evaluation_date)
    entry_index = evaluation_index + entry_lag
    if entry_index >= len(calendar):
        return labels, 'forward_entry_unavailable', False
    entry_date = calendar[entry_index]
    ticker_prices = prices.get(ticker, {})
    entry_price = ticker_prices.get(entry_date)
    if entry_price is None or entry_price <= 0.0:
        return labels, 'forward_entry_unavailable', False
    beta = _trailing_beta(
        ticker_prices,
        prices['SPY'],
        as_of=evaluation_date,
    )
    statuses: set[str] = set()
    for horizon in horizons:
        exit_index = entry_index + horizon
        ticker_return: float | None = None
        xlp_return: float | None = None
        spy_return: float | None = None
        if exit_index < len(calendar):
            exit_date = calendar[exit_index]
            exit_price = ticker_prices.get(exit_date)
            if exit_price is not None and exit_price > 0.0:
                ticker_return = exit_price / entry_price - 1.0
                statuses.add('market_price')
            else:
                terminal = terminal_horizon_value(
                    conn,
                    terminal_policy,
                    ticker=ticker,
                    horizon_date=exit_date,
                )
                terminal_value = _finite(terminal.get('terminal_value'))
                terminal_status = str(terminal.get('calculation_status') or '')
                if terminal_status.startswith('resolved_') and terminal_value is not None:
                    ticker_return = terminal_value / entry_price - 1.0
                    statuses.add('terminal_value_resolved')
                elif terminal_status not in {'terminal_event_missing', 'pre_terminal_event'}:
                    statuses.add('terminal_value_unresolved')
            xlp_entry = prices['XLP'].get(entry_date)
            xlp_exit = prices['XLP'].get(exit_date)
            if xlp_entry and xlp_exit:
                xlp_return = xlp_exit / xlp_entry - 1.0
            spy_entry = prices['SPY'].get(entry_date)
            spy_exit = prices['SPY'].get(exit_date)
            if spy_entry and spy_exit:
                spy_return = spy_exit / spy_entry - 1.0
        labels[f'forward_total_return_{horizon}d'] = ticker_return
        labels[f'forward_xlp_residual_return_{horizon}d'] = (
            None
            if ticker_return is None or xlp_return is None
            else ticker_return - xlp_return
        )
        labels[f'forward_spy_beta_residual_return_{horizon}d'] = (
            None
            if ticker_return is None or spy_return is None or beta is None
            else ticker_return - beta * spy_return
        )
    if 'terminal_value_unresolved' in statuses:
        status = 'terminal_value_unresolved'
    elif 'terminal_value_resolved' in statuses:
        status = 'terminal_value_resolved'
    else:
        status = 'not_crossed'
    return labels, status, True


def _market_regime(
    xlp_prices: dict[str, float], *, as_of: str, lookback: int = 126
) -> str:
    dates = sorted(value for value in xlp_prices if value <= as_of)
    if len(dates) <= lookback:
        return 'insufficient_history'
    value = xlp_prices[dates[-1]] / xlp_prices[dates[-lookback - 1]] - 1.0
    return 'risk_on' if value >= 0.0 else 'risk_off'


def _observation_rows(
    conn: sqlite3.Connection,
    *,
    as_of: str,
    run: sqlite3.Row,
) -> list[sqlite3.Row]:
    return sorted(
        (
            row for row in _run_observations(
                conn, as_of=as_of, run=run
            )
            if row['numeric_value'] is not None
            and str(row['evidence_status']) == 'accepted_measurement_only'
            and str(row['production_status']) == 'measurement_only'
        ),
        key=lambda row: (
            str(row['accepted_at']), int(row['observation_id'])
        ),
    )


def _scope_priority(scope: str) -> int:
    lowered = scope.casefold()
    if 'consolidated' in lowered or lowered in {'company', 'total_company'}:
        return 4
    if 'reported_scope' in lowered:
        return 3
    if 'segment' in lowered:
        return 1
    return 2


def _best_observation(
    rows: Iterable[sqlite3.Row], *, as_of: str
) -> sqlite3.Row | None:
    values = [
        row for row in rows
        if str(row['period_end']) <= as_of
        and str(row['accepted_at'])[:10] <= as_of
    ]
    if not values:
        return None
    return max(
        values,
        key=lambda row: (
            str(row['period_end']),
            str(row['accepted_at']),
            _scope_priority(str(row['scope'] or '')),
            float(row['confidence'] or 0.0),
            int(row['observation_id']),
        ),
    )


def _sample_role(bundle: ConfigBundle, *, as_of: str) -> str:
    strict_start = str(
        cfg_get(bundle.payload, 'oos_provenance.strict_oos_start_date', '') or ''
    )
    if strict_start and as_of >= strict_start:
        return 'strict_oos'
    return 'deep_replay_research'


def _manifest_row(metric: dict[str, Any], *, created_at: str) -> dict[str, Any]:
    direction = _factor_direction(metric['direction_hint'])
    eligible = (
        direction is not None
        and metric['source_availability_class'] not in {
            'non_sec', 'sec_selective',
        }
    )
    reason = None
    if metric['source_availability_class'] == 'non_sec':
        reason = 'non_sec_source_excluded'
    elif metric['source_availability_class'] == 'sec_selective':
        reason = (
            'selective_disclosure_requires_coverage_bias_validation'
        )
    elif direction is None:
        reason = 'context_dependent_direction_requires_registered_policy'
    payload = {
        'factor_id': metric['metric_id'],
        'source_availability_class': metric['source_availability_class'],
        'cohorts_json': _canonical_json(metric['cohorts']),
        'applicability_subtypes_json': _canonical_json(metric['subtypes']),
        'unit_family': metric['unit_family'],
        'direction_hint': metric['direction_hint'],
        'factor_direction': direction,
        'production_status': 'measurement_only',
        'definition_versions_json': _canonical_json(metric['definition_versions']),
        'factor_validation_eligible': int(eligible),
        'exclusion_reason': reason,
    }
    payload['manifest_row_sha256'] = _manifest_hash(payload)
    payload['created_at'] = created_at
    return payload


def _panel_sha(rows: Iterable[dict[str, Any]]) -> str:
    digest = hashlib.sha256()
    for row in rows:
        digest.update((str(row['row_sha256']) + '\n').encode('ascii'))
    return digest.hexdigest()


def build_stage6c_panel(
    conn: sqlite3.Connection,
    bundle: ConfigBundle,
    *,
    as_of: str,
    history_start: str | None = None,
    freshness_days: int = DEFAULT_FRESHNESS_DAYS,
) -> dict[str, Any]:
    """Build the immutable, measurement-only specialized factor panel."""

    date.fromisoformat(as_of)
    history_start = history_start or str(
        cfg_get(bundle.payload, 'historical_contract.requested_snapshot_start')
    )
    date.fromisoformat(history_start)
    if history_start > as_of:
        raise ValueError('Stage 6C history_start cannot exceed as_of.')
    if freshness_days < 1:
        raise ValueError('Stage 6C freshness_days must be positive.')
    horizons = tuple(
        sorted({int(value) for value in cfg_get(bundle.payload, 'factor_validation.horizons')})
    )
    if horizons != (21, 63, 126):
        raise ValueError('Stage 6C horizons must remain exactly 21, 63, and 126.')
    if DEFAULT_ENTRY_LAG != 1:
        raise AssertionError('Stage 6C entry lag contract drifted.')

    bootstrap_stage6b(conn, bundle)
    ensure_stage6c_schema(conn)
    stage6b_run = _latest_stage6b_run(conn, as_of=as_of)
    policy_sha = stage6b_policy_sha256()
    if str(stage6b_run['policy_sha256']) != policy_sha:
        raise RuntimeError('Stage 6B policy drifted before Stage 6C panel construction.')
    config_contract = stage6c_config_payload(bundle)
    config_sha = _sha256(config_contract)
    observations = _observation_rows(
        conn, as_of=as_of, run=stage6b_run
    )
    metrics = _metrics(conn, observations=observations)
    taxonomy = _taxonomy(conn)
    memberships = _memberships(conn)
    selected_sources = _selected_sources(conn, as_of=as_of)
    missing_sources = sorted(set(taxonomy) - set(selected_sources))
    if missing_sources:
        raise RuntimeError(
            f'Stage 6C taxonomy tickers lack selected prices: {missing_sources[:10]}'
        )
    prices = _price_maps(
        conn,
        selected_sources=selected_sources,
        history_start=history_start,
        as_of=as_of,
    )
    calendar, evaluation_dates = _evaluation_dates(
        prices['SPY'],
        history_start=history_start,
        as_of=as_of,
        entry_lag=DEFAULT_ENTRY_LAG,
        maximum_horizon=max(horizons),
    )
    terminal_policy = load_terminal_event_policy(DEFAULT_TERMINAL_POLICY)
    observations_by_key: dict[tuple[str, str], list[sqlite3.Row]] = defaultdict(list)
    pointer = 0
    now = utc_now()
    panel_rows: list[dict[str, Any]] = []
    label_cache: dict[tuple[str, str], tuple[dict[str, float | None], str, bool]] = {}
    for evaluation_date in evaluation_dates:
        cutoff = evaluation_date + 'T23:59:59Z'
        while pointer < len(observations) and str(observations[pointer]['accepted_at']) <= cutoff:
            observed = observations[pointer]
            observations_by_key[
                (str(observed['ticker']), str(observed['metric_id']))
            ].append(observed)
            pointer += 1
        regime = _market_regime(prices['XLP'], as_of=evaluation_date)
        for ticker, member in sorted(taxonomy.items()):
            membership_eligible, _live = _membership_on(
                memberships.get(ticker, ()),
                as_of=evaluation_date,
            )
            if not membership_eligible:
                continue
            labels, terminal_status, entry_available = label_cache.setdefault(
                (evaluation_date, ticker),
                _forward_labels(
                    conn,
                    ticker=ticker,
                    evaluation_date=evaluation_date,
                    calendar=calendar,
                    prices=prices,
                    entry_lag=DEFAULT_ENTRY_LAG,
                    horizons=horizons,
                    terminal_policy=terminal_policy,
                ),
            )
            for metric_id, metric in sorted(metrics.items()):
                if not _metric_applicable(
                    metric,
                    cohort=member['cohort_id'],
                    subtype=member['subtype'],
                ):
                    continue
                observation = _best_observation(
                    observations_by_key.get((ticker, metric_id), ()),
                    as_of=evaluation_date,
                )
                factor_value: float | None = None
                unit: str | None = None
                accepted_at: str | None = None
                period_end: str | None = None
                age_days: int | None = None
                observation_sha: str | None = None
                definition_version: str | None = None
                if metric['source_availability_class'] == 'non_sec':
                    availability = 'structurally_excluded_non_sec'
                elif observation is None:
                    availability = 'missing_no_pit_observation'
                else:
                    accepted_at = str(observation['accepted_at'])
                    period_end = str(observation['period_end'])
                    age_days = (
                        date.fromisoformat(evaluation_date)
                        - date.fromisoformat(period_end)
                    ).days
                    observation_sha = str(observation['observation_sha256'])
                    definition_version = str(observation['definition_version'])
                    unit = str(observation['unit'] or '')
                    if age_days < 0:
                        availability = 'rejected_future_period'
                    elif age_days > freshness_days:
                        availability = 'stale'
                    else:
                        value = _finite(observation['numeric_value'])
                        if value is None:
                            availability = 'rejected_nonfinite'
                        else:
                            availability = 'available'
                            factor_value = value
                row: dict[str, Any] = {
                    'asof_date': evaluation_date,
                    'ticker': ticker,
                    'cohort_id': member['cohort_id'],
                    'applicability_subtype': member['subtype'],
                    'factor_id': metric_id,
                    'factor_value': factor_value,
                    'unit': unit,
                    'direction_hint': metric['direction_hint'],
                    'availability_status': availability,
                    'source_accepted_at': accepted_at,
                    'source_period_end': period_end,
                    'source_age_days': age_days,
                    'source_observation_sha256': observation_sha,
                    'source_definition_version': definition_version,
                    'membership_eligible_flag': 1,
                    'investable_flag': int(entry_available),
                    'sample_role': _sample_role(
                        bundle, as_of=evaluation_date
                    ),
                    'market_regime': regime,
                    'input_cost_regime': 'not_available',
                    'terminal_event_status': terminal_status,
                    **labels,
                }
                row['row_sha256'] = _row_hash(row)
                row['created_at'] = now
                panel_rows.append(row)
    panel_rows.sort(
        key=lambda row: (row['asof_date'], row['ticker'], row['factor_id'])
    )
    panel_sha = _panel_sha(panel_rows)
    manifest_rows = [
        _manifest_row(metric, created_at=now) for metric in metrics.values()
    ]
    horizons_json = _canonical_json(horizons)
    existing = conn.execute(
        '''SELECT * FROM stage6c_panel_run
           WHERE asof_date=? AND history_start=? AND evaluation_frequency='monthly'
             AND entry_lag_trading_days=? AND horizons_json=?
             AND freshness_days=? AND config_sha256=?
             AND metric_policy_sha256=? AND source_stage6b_run_id=?''',
        (
            as_of, history_start, DEFAULT_ENTRY_LAG, horizons_json,
            freshness_days, config_sha, policy_sha,
            int(stage6b_run['stage6b_run_id']),
        ),
    ).fetchone()
    if existing is not None:
        if (
            str(existing['status']) == 'complete'
            and str(existing['panel_sha256']) == panel_sha
        ):
            return validate_stage6c_panel(
                conn,
                stage6c_run_id=int(existing['stage6c_run_id']),
            )
        raise RuntimeError('Stage 6C immutable run identity already exists with different content.')

    manifest = {
        'schema_version': PANEL_VERSION,
        'stage6c_schema_sha256': STAGE6C_MIGRATION_SHA256,
        'asof_date': as_of,
        'history_start': history_start,
        'evaluation_frequency': 'monthly',
        'evaluation_dates': evaluation_dates,
        'entry_lag_trading_days': DEFAULT_ENTRY_LAG,
        'horizons_trading_days': list(horizons),
        'freshness_days': freshness_days,
        'config_sha256': config_sha,
        'stage6c_config_contract': config_contract,
        'metric_policy_sha256': policy_sha,
        'source_stage6b_run_id': int(stage6b_run['stage6b_run_id']),
        'factor_count': len(manifest_rows),
        'row_count': len(panel_rows),
        'numeric_row_count': sum(
            row['factor_value'] is not None for row in panel_rows
        ),
        'panel_sha256': panel_sha,
        'production_promotion_enabled': False,
        'portfolio_write_enabled': False,
    }
    with conn:
        cursor = conn.execute(
            '''INSERT INTO stage6c_panel_run(
                   asof_date,history_start,evaluation_frequency,
                   entry_lag_trading_days,horizons_json,freshness_days,
                   config_sha256,metric_policy_sha256,source_stage6b_run_id,
                   status,evaluation_date_count,panel_row_count,numeric_row_count,
                   panel_sha256,manifest_json,started_at,completed_at
               ) VALUES (?,?,?,?,?,?,?,?,?,'complete',?,?,?,?,?,?,?)''',
            (
                as_of, history_start, 'monthly', DEFAULT_ENTRY_LAG,
                horizons_json, freshness_days, config_sha, policy_sha,
                int(stage6b_run['stage6b_run_id']), len(evaluation_dates),
                len(panel_rows), manifest['numeric_row_count'], panel_sha,
                _canonical_json(manifest), now, now,
            ),
        )
        run_id = int(cursor.lastrowid)
        conn.executemany(
            '''INSERT INTO stage6c_feature_manifest(
                   stage6c_run_id,factor_id,source_availability_class,
                   cohorts_json,applicability_subtypes_json,unit_family,
                   direction_hint,factor_direction,production_status,
                   definition_versions_json,factor_validation_eligible,
                   exclusion_reason,manifest_row_sha256,created_at
               ) VALUES (:stage6c_run_id,:factor_id,:source_availability_class,
                   :cohorts_json,:applicability_subtypes_json,:unit_family,
                   :direction_hint,:factor_direction,:production_status,
                   :definition_versions_json,:factor_validation_eligible,
                   :exclusion_reason,:manifest_row_sha256,:created_at)''',
            ({'stage6c_run_id': run_id, **row} for row in manifest_rows),
        )
        columns = (*_HASH_COLUMNS, 'row_sha256', 'created_at')
        conn.executemany(
            f'''INSERT INTO stage6c_specialized_factor_panel(
                    stage6c_run_id,{','.join(columns)}
                ) VALUES (
                    :stage6c_run_id,{','.join(':' + column for column in columns)}
                )''',
            ({'stage6c_run_id': run_id, **row} for row in panel_rows),
        )
    return validate_stage6c_panel(conn, stage6c_run_id=run_id)


def validate_stage6c_panel(
    conn: sqlite3.Connection, *, stage6c_run_id: int
) -> dict[str, Any]:
    query_only = int(conn.execute('PRAGMA query_only').fetchone()[0]) == 1
    if query_only:
        expected_tables = {
            'stage6c_schema_migrations', 'stage6c_panel_run',
            'stage6c_feature_manifest', 'stage6c_specialized_factor_panel',
        }
        present_tables = {
            str(row[0])
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        if missing := expected_tables - present_tables:
            raise RuntimeError(
                'Read-only Stage 6C validation requires the existing schema; '
                f'missing={sorted(missing)}'
            )
        migration = conn.execute(
            '''SELECT migration_sha256 FROM stage6c_schema_migrations
               ORDER BY migration_version DESC LIMIT 1'''
        ).fetchone()
        if migration is None or str(migration[0]) != STAGE6C_MIGRATION_SHA256:
            raise RuntimeError(
                'Read-only Stage 6C validation found a migration checksum mismatch.'
            )
        if conn.execute('PRAGMA foreign_key_check').fetchone() is not None:
            raise RuntimeError(
                'Read-only Stage 6C validation found a foreign-key violation.'
            )
    else:
        ensure_stage6c_schema(conn)
    run = conn.execute(
        'SELECT * FROM stage6c_panel_run WHERE stage6c_run_id=?',
        (stage6c_run_id,),
    ).fetchone()
    if run is None:
        raise ValueError(f'Unknown Stage 6C run: {stage6c_run_id}')
    rows = [dict(row) for row in conn.execute(
        '''SELECT * FROM stage6c_specialized_factor_panel
           WHERE stage6c_run_id=? ORDER BY asof_date,ticker,factor_id''',
        (stage6c_run_id,),
    )]
    checks: list[dict[str, Any]] = []

    def check(name: str, passed: bool, detail: str) -> None:
        checks.append({
            'check': name,
            'status': 'PASS' if passed else 'FAIL',
            'detail': detail,
        })

    recomputed_hashes = [_row_hash(row) for row in rows]
    mismatches = sum(
        expected != str(row['row_sha256'])
        for expected, row in zip(recomputed_hashes, rows, strict=True)
    )
    panel_sha = hashlib.sha256(
        ''.join(value + '\n' for value in recomputed_hashes).encode('ascii')
    ).hexdigest()
    duplicate_count = int(conn.execute(
        '''SELECT COUNT(*) FROM (
               SELECT asof_date,ticker,factor_id,COUNT(*) AS n
               FROM stage6c_specialized_factor_panel
               WHERE stage6c_run_id=?
               GROUP BY asof_date,ticker,factor_id HAVING n<>1
           )''',
        (stage6c_run_id,),
    ).fetchone()[0])
    future_count = int(conn.execute(
        '''SELECT COUNT(*) FROM stage6c_specialized_factor_panel
           WHERE stage6c_run_id=? AND source_accepted_at IS NOT NULL
             AND substr(source_accepted_at,1,10)>asof_date''',
        (stage6c_run_id,),
    ).fetchone()[0])
    period_age_errors = int(conn.execute(
        '''SELECT COUNT(*) FROM stage6c_specialized_factor_panel
           WHERE stage6c_run_id=? AND source_period_end IS NOT NULL
             AND (
                 source_period_end>asof_date
                 OR source_age_days IS NULL
                 OR source_age_days<0
                 OR source_age_days<>
                    CAST(julianday(asof_date)-julianday(source_period_end)
                         AS INTEGER)
             )''',
        (stage6c_run_id,),
    ).fetchone()[0])
    availability_errors = int(conn.execute(
        '''SELECT COUNT(*) FROM stage6c_specialized_factor_panel
           WHERE stage6c_run_id=? AND (
               (availability_status='available' AND (
                   factor_value IS NULL OR source_accepted_at IS NULL
                   OR source_period_end IS NULL
                   OR source_observation_sha256 IS NULL
                   OR source_definition_version IS NULL
               ))
               OR (availability_status<>'available' AND factor_value IS NOT NULL)
               OR (availability_status='structurally_excluded_non_sec' AND (
                   source_accepted_at IS NOT NULL OR source_period_end IS NOT NULL
                   OR source_observation_sha256 IS NOT NULL
               ))
           )''',
        (stage6c_run_id,),
    ).fetchone()[0])
    weighted = int(conn.execute(
        '''SELECT COUNT(*) FROM dim_specialized_metric WHERE production_weight<>0'''
    ).fetchone()[0])
    manifest_count = int(conn.execute(
        '''SELECT COUNT(*) FROM stage6c_feature_manifest WHERE stage6c_run_id=?''',
        (stage6c_run_id,),
    ).fetchone()[0])
    registry_count = int(conn.execute(
        'SELECT COUNT(*) FROM dim_specialized_metric'
    ).fetchone()[0])
    manifest_rows = [
        dict(row) for row in conn.execute(
            '''SELECT * FROM stage6c_feature_manifest
               WHERE stage6c_run_id=? ORDER BY factor_id''',
            (stage6c_run_id,),
        )
    ]
    manifest_hash_mismatches = sum(
        _manifest_hash(row) != str(row['manifest_row_sha256'])
        for row in manifest_rows
    )
    source_run = conn.execute(
        '''SELECT * FROM stage6b_specialized_run
           WHERE stage6b_run_id=? AND asof_date=?
             AND status='measurement_only_complete' ''',
        (int(run['source_stage6b_run_id']), str(run['asof_date'])),
    ).fetchone()
    source_run_valid = (
        source_run is not None
        and str(source_run['policy_sha256']) == stage6b_policy_sha256()
    )
    source_observations = (
        _observation_rows(
            conn, as_of=str(run['asof_date']), run=source_run
        )
        if source_run_valid and source_run is not None
        else []
    )
    expected_metrics = _metrics(
        conn, observations=source_observations
    )
    expected_manifest = {
        factor_id: _manifest_row(metric, created_at='')
        for factor_id, metric in expected_metrics.items()
    }
    manifest_semantic_mismatches = 0
    for row in manifest_rows:
        expected = expected_manifest.get(str(row['factor_id']))
        if expected is None or any(
            row.get(column) != expected.get(column)
            for column in _MANIFEST_HASH_COLUMNS
        ):
            manifest_semantic_mismatches += 1
    manifest_semantic_mismatches += len(
        set(expected_manifest) - {
            str(row['factor_id']) for row in manifest_rows
        }
    )
    try:
        run_manifest = json.loads(str(run['manifest_json']))
    except (TypeError, json.JSONDecodeError):
        run_manifest = None
    manifest_contract = (
        run_manifest.get('stage6c_config_contract')
        if isinstance(run_manifest, dict) else None
    )
    run_manifest_valid = bool(
        isinstance(run_manifest, dict)
        and isinstance(manifest_contract, dict)
        and _sha256(manifest_contract) == str(run['config_sha256'])
        and run_manifest.get('schema_version') == PANEL_VERSION
        and run_manifest.get('panel_sha256') == str(run['panel_sha256'])
        and run_manifest.get('row_count') == int(run['panel_row_count'])
        and run_manifest.get('numeric_row_count')
        == int(run['numeric_row_count'])
        and run_manifest.get('source_stage6b_run_id')
        == int(run['source_stage6b_run_id'])
    )

    observations_by_key: dict[
        tuple[str, str], list[sqlite3.Row]
    ] = defaultdict(list)
    for observed in source_observations:
        observations_by_key[
            (str(observed['ticker']), str(observed['metric_id']))
        ].append(observed)
    non_sec = {
        factor_id for factor_id, metric in expected_metrics.items()
        if metric['source_availability_class'] == 'non_sec'
    }
    selection_mismatches = 0
    lineage_mismatches = 0
    observations_by_sha = {
        str(observed['observation_sha256']): observed
        for values in observations_by_key.values()
        for observed in values
    }
    for row in rows:
        source_sha = row['source_observation_sha256']
        expected = None
        if str(row['factor_id']) not in non_sec:
            expected = _best_observation(
                observations_by_key.get(
                    (str(row['ticker']), str(row['factor_id'])), ()
                ),
                as_of=str(row['asof_date']),
            )
        expected_sha = (
            None if expected is None
            else str(expected['observation_sha256'])
        )
        if source_sha != expected_sha:
            selection_mismatches += 1
        if source_sha is not None:
            source = observations_by_sha.get(str(source_sha))
            if source is None or any((
                str(source['ticker']) != str(row['ticker']),
                str(source['metric_id']) != str(row['factor_id']),
                str(source['accepted_at']) != str(row['source_accepted_at']),
                str(source['period_end']) != str(row['source_period_end']),
                str(source['unit'] or '') != str(row['unit'] or ''),
                str(source['definition_version'])
                    != str(row['source_definition_version']),
                _finite(source['numeric_value']) != _finite(row['factor_value'])
                    and str(row['availability_status']) == 'available',
            )):
                lineage_mismatches += 1
    check('run_complete', str(run['status']) == 'complete', str(run['status']))
    check('source_stage6b_run_exact', source_run_valid, f'run_id={run["source_stage6b_run_id"]}')
    check('row_count_exact', len(rows) == int(run['panel_row_count']), f'observed={len(rows)} expected={run["panel_row_count"]}')
    check('row_hashes_exact', mismatches == 0, f'mismatches={mismatches}')
    check('panel_hash_exact', panel_sha == str(run['panel_sha256']), f'observed={panel_sha} expected={run["panel_sha256"]}')
    check('panel_primary_keys_unique', duplicate_count == 0, f'duplicates={duplicate_count}')
    check('accepted_at_point_in_time', future_count == 0, f'future_rows={future_count}')
    check('period_end_and_source_age_point_in_time', period_age_errors == 0, f'errors={period_age_errors}')
    check('availability_and_source_fields_coherent', availability_errors == 0, f'errors={availability_errors}')
    check('run_manifest_and_config_contract_exact', run_manifest_valid, f'config_sha256={run["config_sha256"]}')
    check('feature_manifest_complete', manifest_count == registry_count, f'manifest={manifest_count} registry={registry_count}')
    check('feature_manifest_hashes_exact', manifest_hash_mismatches == 0, f'mismatches={manifest_hash_mismatches}')
    check('feature_manifest_registry_bound', manifest_semantic_mismatches == 0, f'mismatches={manifest_semantic_mismatches}')
    check('source_observation_selection_exact', selection_mismatches == 0, f'mismatches={selection_mismatches}')
    check('source_observation_lineage_exact', lineage_mismatches == 0, f'mismatches={lineage_mismatches}')
    check('minimum_evaluation_dates', int(run['evaluation_date_count']) >= MINIMUM_EVALUATION_DATES, f'dates={run["evaluation_date_count"]}')
    check('production_weights_zero', weighted == 0, f'nonzero={weighted}')
    check('foreign_keys_valid', conn.execute('PRAGMA foreign_key_check').fetchone() is None, 'bounded_first_violation_check')
    status = 'PASS' if all(row['status'] == 'PASS' for row in checks) else 'FAIL'
    return {
        'status': status,
        'stage6c_run_id': stage6c_run_id,
        'asof_date': str(run['asof_date']),
        'evaluation_date_count': int(run['evaluation_date_count']),
        'panel_row_count': len(rows),
        'numeric_row_count': sum(row['factor_value'] is not None for row in rows),
        'panel_sha256': str(run['panel_sha256']),
        'checks': checks,
        'production_promotion_enabled': False,
        'portfolio_write_enabled': False,
    }


def write_stage6c_reports(
    conn: sqlite3.Connection,
    *,
    stage6c_run_id: int,
    output_dir: Path,
) -> dict[str, Any]:
    validation = validate_stage6c_panel(conn, stage6c_run_id=stage6c_run_id)
    if validation['status'] != 'PASS':
        failed = [
            row['check'] for row in validation['checks']
            if row['status'] == 'FAIL'
        ]
        raise RuntimeError(
            f'Refusing to export invalid Stage 6C panel: {failed}'
        )
    run = conn.execute(
        'SELECT * FROM stage6c_panel_run WHERE stage6c_run_id=?',
        (stage6c_run_id,),
    ).fetchone()
    if run is None:
        raise ValueError(f'Unknown Stage 6C run: {stage6c_run_id}')
    rows = [dict(row) for row in conn.execute(
        '''SELECT * FROM stage6c_specialized_factor_panel
           WHERE stage6c_run_id=? ORDER BY asof_date,ticker,factor_id''',
        (stage6c_run_id,),
    )]
    manifest_rows = [dict(row) for row in conn.execute(
        '''SELECT * FROM stage6c_feature_manifest
           WHERE stage6c_run_id=? ORDER BY factor_id''',
        (stage6c_run_id,),
    )]
    breadth = [dict(row) for row in conn.execute(
        '''SELECT asof_date,cohort_id,factor_id,
                  COUNT(*) AS applicable_rows,
                  SUM(CASE WHEN availability_status='available' THEN 1 ELSE 0 END)
                      AS available_rows,
                  SUM(CASE WHEN factor_value IS NOT NULL
                            AND investable_flag=1 THEN 1 ELSE 0 END)
                      AS validation_rows
           FROM stage6c_specialized_factor_panel
           WHERE stage6c_run_id=?
           GROUP BY asof_date,cohort_id,factor_id
           ORDER BY asof_date,cohort_id,factor_id''',
        (stage6c_run_id,),
    )]
    output_dir.mkdir(parents=True, exist_ok=True)
    write_csv(output_dir / 'stage6c_specialized_factor_panel.csv', rows)
    write_csv(output_dir / 'stage6c_feature_manifest.csv', manifest_rows)
    write_csv(output_dir / 'stage6c_daily_cohort_breadth.csv', breadth)
    payload = {
        **validation,
        'manifest': json.loads(str(run['manifest_json'])),
        'output_dir': str(output_dir.resolve()),
    }
    write_json(output_dir / 'stage6c_panel_manifest.json', payload)
    return payload
