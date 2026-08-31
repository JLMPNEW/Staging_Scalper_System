from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from dataclasses import asdict, dataclass
from typing import Any, Iterable

from .calibration_scope import (
    apply_current_production_scope,
    calibration_scope_contract,
)
from .config import ConfigBundle, cfg_get, resolve_path
from .db import utc_now
from .metric_registry import SpecializedMetric, load_metric_registry
from .source_registry import load_source_registry, upsert_source_registry
from .stage5 import bootstrap_stage5
from .stage6a_schema import (
    STAGE6A_MIGRATION_SHA256,
    STAGE6A_SCHEMA_VERSION,
    ensure_stage6a_schema,
)


MODEL_FAMILY = 'consumer_defensive'


@dataclass(frozen=True)
class ComponentSpec:
    name: str
    group: str
    source_table: str
    source_field: str
    direction: str
    rank_requirement: str
    unit: str
    description: str


CORE_COMPONENT_SPECS = (
    ComponentSpec('residual_momentum_63d', 'market', 'feature_market_technical', 'residual_momentum_63d', 'higher', 'required', 'decimal_return', '63-session benchmark-residual momentum.'),
    ComponentSpec('residual_momentum_126d', 'market', 'feature_market_technical', 'residual_momentum_126d', 'higher', 'required', 'decimal_return', '126-session benchmark-residual momentum.'),
    ComponentSpec('realized_volatility_63d', 'market', 'feature_market_technical', 'realized_volatility_63d', 'lower', 'required', 'annualized_decimal', '63-session realized volatility.'),
    ComponentSpec('downside_volatility_63d', 'market', 'feature_market_technical', 'downside_volatility_63d', 'lower', 'required', 'annualized_decimal', '63-session downside volatility.'),
    ComponentSpec('max_drawdown_252d', 'market', 'feature_market_technical', 'max_drawdown_252d', 'higher', 'required', 'decimal_return', '252-session maximum drawdown; less negative is higher quality.'),
    ComponentSpec('avg_dollar_volume_63d', 'market', 'feature_market_technical', 'avg_dollar_volume_63d', 'higher', 'required', 'usd', '63-session average dollar volume.'),
    ComponentSpec('gross_margin', 'financial', 'feature_financial_statement', 'gross_margin', 'higher', 'any_financial', 'ratio', 'Point-in-time gross margin.'),
    ComponentSpec('operating_margin', 'financial', 'feature_financial_statement', 'operating_margin', 'higher', 'any_financial', 'ratio', 'Point-in-time operating margin.'),
    ComponentSpec('free_cash_flow_margin', 'financial', 'feature_financial_statement', 'free_cash_flow_margin', 'higher', 'any_financial', 'ratio', 'Point-in-time free-cash-flow margin.'),
    ComponentSpec('return_on_invested_capital', 'financial', 'feature_financial_statement', 'return_on_invested_capital', 'higher', 'any_financial', 'ratio', 'Point-in-time return on invested capital.'),
    ComponentSpec('net_debt_to_ebitda', 'financial', 'feature_financial_statement', 'net_debt_to_ebitda', 'lower', 'any_financial', 'multiple', 'Point-in-time net debt to EBITDA.'),
    ComponentSpec('inventory_turnover', 'financial', 'feature_financial_statement', 'inventory_turnover', 'higher', 'any_financial', 'multiple', 'Point-in-time inventory turnover.'),
    ComponentSpec('insider_net_buying', 'positioning', 'feature_positioning', 'insider_net_buying', 'higher', 'optional', 'usd', 'Recent accepted insider purchases net of sales.'),
    ComponentSpec('institutional_flow', 'positioning', 'feature_positioning', 'institutional_flow', 'higher', 'required', 'ratio', 'Point-in-time institutional positioning flow.'),
    ComponentSpec('short_float_pct', 'positioning', 'feature_positioning', 'short_float_pct', 'lower', 'any_short', 'ratio', 'Published short interest as a fraction of float.'),
    ComponentSpec('short_days_to_cover', 'positioning', 'feature_positioning', 'short_days_to_cover', 'lower', 'any_short', 'days', 'Published short-interest days to cover.'),
    ComponentSpec('borrow_fee', 'positioning', 'feature_positioning', 'borrow_fee', 'lower', 'optional', 'annualized_rate', 'Observed securities-borrow fee.'),
)


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True)


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode('utf-8')).hexdigest()


def _metric_registry(bundle: ConfigBundle) -> tuple[str, list[SpecializedMetric]]:
    path = resolve_path(
        cfg_get(bundle.payload, 'specialized_metrics.registry_path'),
        base_dir=bundle.base_dir,
    )
    return load_metric_registry(path)


def _specialized_name(metric_id: str) -> str:
    return f'specialized:{metric_id}'


def _specialized_direction(direction_hint: str) -> str:
    lowered = direction_hint.strip().lower()
    if lowered in {'higher', 'positive', 'higher_is_better'}:
        return 'higher'
    if lowered in {'lower', 'negative', 'lower_is_better'}:
        return 'lower'
    return 'none'


def _contract_payload(
    bundle: ConfigBundle,
    *,
    registry_version: str,
    metrics: Iterable[SpecializedMetric],
) -> dict[str, Any]:
    scope_contract = calibration_scope_contract(bundle)
    return {
        'definition_version': cfg_get(bundle.payload, 'scoring_features.definition_version'),
        'minimum_normalization_peer_count': int(
            cfg_get(bundle.payload, 'scoring_features.minimum_normalization_peer_count')
        ),
        'normalize_within_cohort': True,
        'minimum_rank_ready_fraction': float(
            cfg_get(bundle.payload, 'scoring_features.minimum_rank_ready_fraction')
        ),
        'component_weight_default': 0.0,
        'calibration_scope_sha256': scope_contract['payload_sha256'],
        'production_scope_policy': 'reviewed_exclusions_before_normalization',
        'specialized_missing_value_policy': (
            'neutral_zero_contribution_no_weight_redistribution'
        ),
        'specialized_nonapplicable_policy': 'excluded_from_denominator',
        'specialized_weight_activation_policy': (
            'shared_factor_validation_acceptance_required'
        ),
        'core_components': [asdict(spec) for spec in CORE_COMPONENT_SPECS],
        'specialized_registry_version': registry_version,
        'specialized_components': [
            {
                'metric_id': metric.metric_id,
                'cohorts': list(metric.cohorts),
                'applicability_subtypes': list(metric.applicability_subtypes),
                'unit': metric.unit_family,
                'direction': _specialized_direction(metric.direction_hint),
                'production_status': metric.initial_status,
                'production_weight': 0.0,
            }
            for metric in metrics
        ],
    }


def scoring_contract_sha256(bundle: ConfigBundle) -> str:
    registry_version, metrics = _metric_registry(bundle)
    return _sha256(
        _contract_payload(bundle, registry_version=registry_version, metrics=metrics)
    )


def bootstrap_stage6a(conn: sqlite3.Connection, bundle: ConfigBundle) -> str:
    bootstrap_stage5(conn, bundle)
    ensure_stage6a_schema(conn)
    source_id = str(cfg_get(bundle.payload, 'scoring_features.source_id'))
    registry_path = resolve_path(
        cfg_get(bundle.payload, 'source_registry.path'),
        base_dir=bundle.base_dir,
    )
    matches = [row for row in load_source_registry(registry_path) if row.source_id == source_id]
    if len(matches) != 1:
        raise RuntimeError(
            f'Stage 6A source registry must contain exactly one {source_id!r} row.'
        )
    upsert_source_registry(conn, matches)
    return scoring_contract_sha256(bundle)


def _current_universe(conn: sqlite3.Connection, as_of: str) -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in conn.execute(
            '''
            SELECT t.ticker,t.calibration_cohort_id,t.applicability_subtype
            FROM dim_consumer_defensive_taxonomy t
            JOIN dim_universe_membership m
              ON m.ticker=t.ticker AND m.model_family=t.model_family
            WHERE t.model_family='consumer_defensive'
              AND m.live_investable_flag=1
              AND m.start_date<=?
              AND COALESCE(m.end_date,'9999-12-31')>=?
            ORDER BY t.ticker
            ''',
            (as_of, as_of),
        )
    ]


def _rows_by_ticker(
    conn: sqlite3.Connection,
    *,
    table: str,
    as_of: str,
) -> dict[str, sqlite3.Row]:
    rows = list(
        conn.execute(
            f'''
            SELECT * FROM {table}
            WHERE model_family='consumer_defensive' AND asof_date=?
            ORDER BY ticker,source_id
            ''',
            (as_of,),
        )
    )
    result: dict[str, sqlite3.Row] = {}
    for row in rows:
        ticker = str(row['ticker'])
        if ticker in result:
            raise RuntimeError(
                f'Stage 6A requires exactly one {table} row per ticker/as-of; duplicate {ticker}.'
            )
        result[ticker] = row
    return result


def _finite(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _percentile(value: float, peers: list[float], direction: str) -> float:
    lower = sum(peer < value for peer in peers)
    equal = sum(peer == value for peer in peers)
    percentile = 100.0 * (lower + (equal - 1) / 2.0) / (len(peers) - 1)
    return 100.0 - percentile if direction == 'lower' else percentile


def _normalize_components(
    components: list[dict[str, Any]],
    *,
    minimum_peers: int,
) -> None:
    core_names = {spec.name for spec in CORE_COMPONENT_SPECS}
    for name in sorted(core_names):
        named = [
            row for row in components
            if row['component_name'] == name
            and row['raw_value'] is not None
            and row['availability_status'] == 'available'
        ]
        global_peers = [float(row['raw_value']) for row in named]
        for row in named:
            cohort_peers = [
                float(peer['raw_value'])
                for peer in named
                if peer['calibration_cohort_id'] == row['calibration_cohort_id']
            ]
            peers = cohort_peers
            scope = 'cohort'
            if len(peers) < minimum_peers or len(set(peers)) < 2:
                peers = global_peers
                scope = 'universe_fallback'
            if len(peers) < minimum_peers or len(set(peers)) < 2:
                row['normalized_value'] = None
                row['component_score'] = None
                row['exclusion_reason'] = 'insufficient_normalization_peers_or_variance'
                row['normalization_scope'] = 'unavailable'
                continue
            score = _percentile(float(row['raw_value']), peers, str(row['direction']))
            row['normalized_value'] = score
            row['component_score'] = score
            row['normalization_scope'] = scope


COMPONENT_IDENTITY_FIELDS = (
    'ticker', 'asof_date', 'component_name', 'raw_value', 'normalized_value',
    'component_score', 'component_weight', 'availability_status', 'source_asof_date',
    'quality_status', 'component_group', 'direction', 'rank_requirement', 'unit',
    'definition_version', 'contract_sha256', 'source_id', 'source_table',
    'source_field', 'exclusion_reason', 'lineage_json', 'production_status',
)


def component_observation_id(row: dict[str, Any] | sqlite3.Row) -> str:
    return _sha256({field: row[field] for field in COMPONENT_IDENTITY_FIELDS})


INPUT_IDENTITY_FIELDS = (
    'ticker', 'asof_date', 'calibration_cohort_id', 'rank_ready_flag',
    'review_reason', 'source_id', 'feature_status', 'calibration_eligible_flag',
    'core_available_component_count', 'core_missing_component_count',
    'core_data_quality_confidence', 'full_data_quality_confidence',
    'definition_version', 'contract_sha256', 'lineage_json',
)


def input_observation_id(row: dict[str, Any] | sqlite3.Row) -> str:
    return _sha256({field: row[field] for field in INPUT_IDENTITY_FIELDS})


def _specialized_applicable(
    metric: SpecializedMetric,
    *,
    cohort_id: str,
    subtype: str,
) -> bool:
    return cohort_id in metric.cohorts and (
        'all_operating_issuers' in metric.applicability_subtypes
        or subtype in metric.applicability_subtypes
    )


def _contract_rows(
    *,
    metrics: list[SpecializedMetric],
    definition_version: str,
    contract_sha: str,
    now: str,
) -> list[tuple[Any, ...]]:
    rows = [
        (
            spec.name, spec.group, spec.source_table, spec.source_field,
            spec.direction, spec.rank_requirement, spec.unit,
            'research_candidate', definition_version, contract_sha,
            spec.description, now,
        )
        for spec in CORE_COMPONENT_SPECS
    ]
    rows.extend(
        (
            _specialized_name(metric.metric_id), 'specialized',
            'fact_specialized_metric_observation', metric.metric_id,
            _specialized_direction(metric.direction_hint), 'specialized',
            metric.unit_family, metric.initial_status, definition_version,
            contract_sha, metric.purpose, now,
        )
        for metric in metrics
    )
    return rows


def _core_component(
    *,
    ticker: str,
    as_of: str,
    cohort_id: str,
    spec: ComponentSpec,
    upstream: sqlite3.Row | None,
    accepted_statuses: set[str],
    definition_version: str,
    contract_sha: str,
) -> dict[str, Any]:
    quality_column = {
        'market': 'quality_status',
        'financial': 'financial_quality_status',
        'positioning': 'quality_status',
    }[spec.group]
    quality = str(upstream[quality_column] or '') if upstream is not None else 'missing'
    raw = _finite(upstream[spec.source_field]) if upstream is not None else None
    if upstream is None:
        availability = 'missing_upstream_row'
        reason = f'missing_source:{spec.source_table}'
        source_id = None
    elif quality not in accepted_statuses:
        availability = 'quality_rejected'
        reason = f'quality_status:{quality or "blank"}'
        source_id = str(upstream['source_id'])
    elif raw is None:
        availability = 'missing_source_value'
        reason = f'missing_value:{spec.source_field}'
        source_id = str(upstream['source_id'])
    else:
        availability = 'available'
        reason = None
        source_id = str(upstream['source_id'])
    lineage: dict[str, Any] = {
        'upstream_table': spec.source_table,
        'upstream_source_id': source_id,
        'upstream_asof_date': as_of if upstream is not None else None,
        'upstream_quality_status': quality,
        'source_field': spec.source_field,
    }
    if upstream is not None and 'lineage_json' in upstream.keys():
        try:
            lineage['upstream_lineage'] = json.loads(str(upstream['lineage_json'] or '{}'))
        except json.JSONDecodeError:
            lineage['upstream_lineage'] = {'invalid_json_preserved': str(upstream['lineage_json'])}
    return {
        'ticker': ticker,
        'asof_date': as_of,
        'calibration_cohort_id': cohort_id,
        'component_name': spec.name,
        'raw_value': raw,
        'normalized_value': None,
        'component_score': None,
        'component_weight': 0.0,
        'availability_status': availability,
        'source_asof_date': as_of if upstream is not None else None,
        'quality_status': quality,
        'component_group': spec.group,
        'direction': spec.direction,
        'rank_requirement': spec.rank_requirement,
        'unit': spec.unit,
        'definition_version': definition_version,
        'contract_sha256': contract_sha,
        'source_id': source_id,
        'source_table': spec.source_table,
        'source_field': spec.source_field,
        'exclusion_reason': reason,
        'lineage_json': _canonical_json(lineage),
        'production_status': 'research_candidate',
        'normalization_scope': None,
    }


def _specialized_component(
    *,
    ticker: str,
    as_of: str,
    cohort_id: str,
    subtype: str,
    metric: SpecializedMetric,
    registry_version: str,
    definition_version: str,
    contract_sha: str,
) -> dict[str, Any]:
    applicable = _specialized_applicable(
        metric, cohort_id=cohort_id, subtype=subtype
    )
    availability = 'not_loaded' if applicable else 'not_applicable'
    reason = (
        'stage6b_extraction_not_promoted'
        if applicable
        else 'metric_not_applicable_to_cohort_or_subtype'
    )
    lineage = {
        'metric_id': metric.metric_id,
        'registry_version': registry_version,
        'cohorts': list(metric.cohorts),
        'applicability_subtypes': list(metric.applicability_subtypes),
        'ticker_cohort': cohort_id,
        'ticker_subtype': subtype,
        'missing_value_policy': (
            'neutral_zero_contribution_no_weight_redistribution'
        ),
        'nonapplicable_policy': 'excluded_from_denominator',
        'weight_activation_policy': (
            'shared_factor_validation_acceptance_required'
        ),
    }
    return {
        'ticker': ticker,
        'asof_date': as_of,
        'calibration_cohort_id': cohort_id,
        'component_name': _specialized_name(metric.metric_id),
        'raw_value': None,
        'normalized_value': None,
        'component_score': None,
        'component_weight': 0.0,
        'availability_status': availability,
        'source_asof_date': None,
        'quality_status': 'not_loaded' if applicable else 'not_applicable',
        'component_group': 'specialized',
        'direction': _specialized_direction(metric.direction_hint),
        'rank_requirement': 'specialized',
        'unit': metric.unit_family,
        'definition_version': definition_version,
        'contract_sha256': contract_sha,
        'source_id': None,
        'source_table': 'fact_specialized_metric_observation',
        'source_field': metric.metric_id,
        'exclusion_reason': reason,
        'lineage_json': _canonical_json(lineage),
        'production_status': metric.initial_status,
        'normalization_scope': None,
    }


def _rank_readiness(
    rows: list[dict[str, Any]],
) -> tuple[int, list[str]]:
    usable = {
        str(row['component_name'])
        for row in rows
        if row['availability_status'] == 'available'
        and row['normalized_value'] is not None
    }
    reasons = [
        f'missing_required:{spec.name}'
        for spec in CORE_COMPONENT_SPECS
        if spec.rank_requirement == 'required' and spec.name not in usable
    ]
    if not any(
        spec.name in usable
        for spec in CORE_COMPONENT_SPECS
        if spec.rank_requirement == 'any_financial'
    ):
        reasons.append('missing_requirement:any_financial')
    if not any(
        spec.name in usable
        for spec in CORE_COMPONENT_SPECS
        if spec.rank_requirement == 'any_short'
    ):
        reasons.append('missing_requirement:any_short')
    return (0 if reasons else 1), reasons


def build_scoring_features(
    conn: sqlite3.Connection,
    bundle: ConfigBundle,
    *,
    as_of: str,
) -> dict[str, Any]:
    contract_sha = bootstrap_stage6a(conn, bundle)
    definition_version = str(
        cfg_get(bundle.payload, 'scoring_features.definition_version')
    )
    registry_version, metrics = _metric_registry(bundle)
    source_universe = _current_universe(conn, as_of)
    if not source_universe:
        raise RuntimeError(f'Stage 6A has no live PIT universe at {as_of}.')
    universe, scope_summary = apply_current_production_scope(
        source_universe,
        bundle,
    )
    tickers = {str(row['ticker']) for row in universe}
    if conn.execute(
        '''
        SELECT 1 FROM feature_scoring_model_output
        WHERE model_family='consumer_defensive' AND asof_date=?
        LIMIT 1
        ''',
        (as_of,),
    ).fetchone() is not None:
        raise RuntimeError(
            'Stage 6A cannot replace atomic inputs after a model output exists for the as-of date.'
        )

    upstream = {
        'market': _rows_by_ticker(
            conn, table='feature_market_technical', as_of=as_of
        ),
        'financial': _rows_by_ticker(
            conn, table='feature_financial_statement', as_of=as_of
        ),
        'positioning': _rows_by_ticker(
            conn, table='feature_positioning', as_of=as_of
        ),
    }
    accepted_statuses = {
        'market': set(
            cfg_get(bundle.payload, 'scoring_features.accepted_market_quality_statuses')
        ),
        'financial': set(
            cfg_get(bundle.payload, 'scoring_features.accepted_financial_quality_statuses')
        ),
        'positioning': set(
            cfg_get(bundle.payload, 'scoring_features.accepted_positioning_quality_statuses')
        ),
    }
    components: list[dict[str, Any]] = []
    for member in universe:
        ticker = str(member['ticker'])
        cohort_id = str(member['calibration_cohort_id'])
        subtype = str(member['applicability_subtype'] or '')
        components.extend(
            _core_component(
                ticker=ticker,
                as_of=as_of,
                cohort_id=cohort_id,
                spec=spec,
                upstream=upstream[spec.group].get(ticker),
                accepted_statuses=accepted_statuses[spec.group],
                definition_version=definition_version,
                contract_sha=contract_sha,
            )
            for spec in CORE_COMPONENT_SPECS
        )
        components.extend(
            _specialized_component(
                ticker=ticker,
                as_of=as_of,
                cohort_id=cohort_id,
                subtype=subtype,
                metric=metric,
                registry_version=registry_version,
                definition_version=definition_version,
                contract_sha=contract_sha,
            )
            for metric in metrics
        )

    _normalize_components(
        components,
        minimum_peers=int(
            cfg_get(bundle.payload, 'scoring_features.minimum_normalization_peer_count')
        ),
    )
    for row in components:
        lineage = json.loads(str(row['lineage_json']))
        lineage['normalization_scope'] = row.pop('normalization_scope')
        lineage['calibration_scope_sha256'] = scope_summary['contract'][
            'payload_sha256'
        ]
        row['lineage_json'] = _canonical_json(lineage)
        row['component_observation_id'] = component_observation_id(row)

    by_ticker: dict[str, list[dict[str, Any]]] = {
        ticker: [] for ticker in sorted(tickers)
    }
    for row in components:
        by_ticker[str(row['ticker'])].append(row)

    inputs: list[dict[str, Any]] = []
    for member in universe:
        ticker = str(member['ticker'])
        rows = by_ticker[ticker]
        rank_ready, reasons = _rank_readiness(rows)
        core_rows = [row for row in rows if row['component_group'] != 'specialized']
        core_available = sum(
            row['availability_status'] == 'available'
            and row['normalized_value'] is not None
            for row in core_rows
        )
        applicable_specialized = sum(
            row['component_group'] == 'specialized'
            and row['availability_status'] != 'not_applicable'
            for row in rows
        )
        component_ids = sorted(str(row['component_observation_id']) for row in rows)
        lineage_json = _canonical_json(
            {
                'component_observation_ids': component_ids,
                'core_component_count': len(CORE_COMPONENT_SPECS),
                'specialized_component_count': len(metrics),
                'specialized_applicable_count': applicable_specialized,
                'specialized_available_count': 0,
                'specialized_missing_count': applicable_specialized,
                'specialized_missing_value_policy': (
                    'neutral_zero_contribution_no_weight_redistribution'
                ),
                'specialized_nonapplicable_policy': 'excluded_from_denominator',
                'specialized_weight_activation_policy': (
                    'shared_factor_validation_acceptance_required'
                ),
                'calibration_scope_sha256': scope_summary['contract'][
                    'payload_sha256'
                ],
            }
        )
        denominator = len(CORE_COMPONENT_SPECS) + applicable_specialized
        input_row = {
            'ticker': ticker,
            'asof_date': as_of,
            'calibration_cohort_id': str(member['calibration_cohort_id']),
            'rank_ready_flag': rank_ready,
            'review_reason': None if rank_ready else ';'.join(sorted(reasons)),
            'source_id': str(cfg_get(bundle.payload, 'scoring_features.source_id')),
            'feature_status': 'rank_ready' if rank_ready else 'review_required',
            'calibration_eligible_flag': 1,
            'core_available_component_count': core_available,
            'core_missing_component_count': len(CORE_COMPONENT_SPECS) - core_available,
            'core_data_quality_confidence': core_available / len(CORE_COMPONENT_SPECS),
            'full_data_quality_confidence': core_available / denominator,
            'definition_version': definition_version,
            'contract_sha256': contract_sha,
            'lineage_json': lineage_json,
        }
        input_row['input_observation_id'] = input_observation_id(input_row)
        inputs.append(input_row)

    now = utc_now()
    contract_rows = _contract_rows(
        metrics=metrics,
        definition_version=definition_version,
        contract_sha=contract_sha,
        now=now,
    )
    with conn:
        conn.execute('DELETE FROM stage6a_component_contract')
        conn.executemany(
            '''
            INSERT INTO stage6a_component_contract(
                component_name,component_group,source_table,source_field,direction,
                rank_requirement,unit,production_status,definition_version,
                contract_sha256,description,updated_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
            ''',
            contract_rows,
        )
        conn.execute(
            '''
            DELETE FROM feature_scoring_component
            WHERE model_family='consumer_defensive' AND asof_date=?
            ''',
            (as_of,),
        )
        conn.execute(
            '''
            DELETE FROM feature_scoring_input
            WHERE model_family='consumer_defensive' AND asof_date=?
            ''',
            (as_of,),
        )
        conn.executemany(
            '''
            INSERT INTO feature_scoring_component(
                model_family,ticker,asof_date,component_name,raw_value,
                normalized_value,component_score,component_weight,
                availability_status,source_asof_date,quality_status,created_at,
                component_group,direction,rank_requirement,unit,definition_version,
                contract_sha256,source_id,source_table,source_field,exclusion_reason,
                lineage_json,component_observation_id,production_status
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ''',
            [
                (
                    MODEL_FAMILY, row['ticker'], row['asof_date'],
                    row['component_name'], row['raw_value'], row['normalized_value'],
                    row['component_score'], 0.0, row['availability_status'],
                    row['source_asof_date'], row['quality_status'], now,
                    row['component_group'], row['direction'], row['rank_requirement'],
                    row['unit'], row['definition_version'], row['contract_sha256'],
                    row['source_id'], row['source_table'], row['source_field'],
                    row['exclusion_reason'], row['lineage_json'],
                    row['component_observation_id'], row['production_status'],
                )
                for row in components
            ],
        )
        conn.executemany(
            '''
            INSERT INTO feature_scoring_input(
                model_family,ticker,asof_date,calibration_cohort_id,
                rank_ready_flag,review_reason,created_at,source_id,feature_status,
                calibration_eligible_flag,core_available_component_count,
                core_missing_component_count,core_data_quality_confidence,
                full_data_quality_confidence,definition_version,contract_sha256,
                lineage_json,input_observation_id
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ''',
            [
                (
                    MODEL_FAMILY, row['ticker'], row['asof_date'],
                    row['calibration_cohort_id'], row['rank_ready_flag'],
                    row['review_reason'], now, row['source_id'],
                    row['feature_status'], row['calibration_eligible_flag'],
                    row['core_available_component_count'],
                    row['core_missing_component_count'],
                    row['core_data_quality_confidence'],
                    row['full_data_quality_confidence'],
                    row['definition_version'], row['contract_sha256'],
                    row['lineage_json'], row['input_observation_id'],
                )
                for row in inputs
            ],
        )

    rank_ready_count = sum(int(row['rank_ready_flag']) for row in inputs)
    return {
        'status': 'PASS',
        'asof_date': as_of,
        'definition_version': definition_version,
        'contract_sha256': contract_sha,
        'source_live_ticker_count': scope_summary['source_ticker_count'],
        'excluded_ticker_count': scope_summary['observed_excluded_ticker_count'],
        'calibration_scope_sha256': scope_summary['contract']['payload_sha256'],
        'ticker_count': len(inputs),
        'component_count': len(components),
        'core_component_count': len(CORE_COMPONENT_SPECS),
        'specialized_component_count': len(metrics),
        'rank_ready_count': rank_ready_count,
        'review_required_count': len(inputs) - rank_ready_count,
        'rank_ready_fraction': rank_ready_count / len(inputs),
    }


def _check(
    checks: list[dict[str, Any]],
    name: str,
    passed: bool,
    **details: Any,
) -> None:
    checks.append({'check': name, 'passed': bool(passed), **details})


def validate_scoring_features(
    conn: sqlite3.Connection,
    bundle: ConfigBundle,
    *,
    as_of: str,
) -> dict[str, Any]:
    ensure_stage6a_schema(conn)
    checks: list[dict[str, Any]] = []
    registry_version, metrics = _metric_registry(bundle)
    contract_sha = _sha256(
        _contract_payload(bundle, registry_version=registry_version, metrics=metrics)
    )
    definition_version = str(
        cfg_get(bundle.payload, 'scoring_features.definition_version')
    )
    source_universe = _current_universe(conn, as_of)
    universe, scope_summary = apply_current_production_scope(
        source_universe,
        bundle,
    )
    expected_tickers = {str(row['ticker']) for row in universe}
    expected_components = {
        *(spec.name for spec in CORE_COMPONENT_SPECS),
        *(_specialized_name(metric.metric_id) for metric in metrics),
    }
    ledger = conn.execute(
        '''
        SELECT migration_sha256 FROM stage6a_schema_migrations
        WHERE migration_version=?
        ''',
        (STAGE6A_SCHEMA_VERSION,),
    ).fetchone()
    _check(
        checks,
        'stage6a_migration_current',
        ledger is not None and str(ledger[0]) == STAGE6A_MIGRATION_SHA256,
        version=STAGE6A_SCHEMA_VERSION,
    )
    _check(
        checks,
        'live_pit_universe_exact',
        len(source_universe) == int(cfg_get(bundle.payload, 'universe.expected_current_rows')),
        observed=len(source_universe),
        expected=int(cfg_get(bundle.payload, 'universe.expected_current_rows')),
    )
    _check(
        checks,
        'reviewed_production_scope_exact',
        len(expected_tickers)
        == int(scope_summary['contract']['expected_remaining_current_ticker_count'])
        and scope_summary['remaining_tickers_by_cohort']
        == scope_summary['contract']['expected_remaining_current_by_cohort'],
        observed=len(expected_tickers),
        expected=int(
            scope_summary['contract']['expected_remaining_current_ticker_count']
        ),
        calibration_scope_sha256=scope_summary['contract']['payload_sha256'],
    )
    source_id = str(cfg_get(bundle.payload, 'scoring_features.source_id'))
    source = conn.execute(
        'SELECT status FROM source_registry WHERE source_id=?',
        (source_id,),
    ).fetchone()
    _check(
        checks,
        'scoring_source_registered',
        source is not None and str(source[0]) == 'active',
        source_id=source_id,
    )
    contract_rows = list(conn.execute('SELECT * FROM stage6a_component_contract'))
    _check(
        checks,
        'component_contract_exact',
        {str(row['component_name']) for row in contract_rows} == expected_components
        and all(str(row['contract_sha256']) == contract_sha for row in contract_rows),
        observed=len(contract_rows),
        expected=len(expected_components),
    )
    inputs = list(
        conn.execute(
            '''
            SELECT * FROM feature_scoring_input
            WHERE model_family='consumer_defensive' AND asof_date=?
            ORDER BY ticker
            ''',
            (as_of,),
        )
    )
    components = list(
        conn.execute(
            '''
            SELECT * FROM feature_scoring_component
            WHERE model_family='consumer_defensive' AND asof_date=?
            ORDER BY ticker,component_name
            ''',
            (as_of,),
        )
    )
    input_tickers = {str(row['ticker']) for row in inputs}
    _check(
        checks,
        'scoring_inputs_exact_universe',
        input_tickers == expected_tickers and len(inputs) == len(expected_tickers),
        missing=sorted(expected_tickers - input_tickers),
        unexpected=sorted(input_tickers - expected_tickers),
    )
    names_by_ticker: dict[str, set[str]] = {ticker: set() for ticker in expected_tickers}
    for row in components:
        names_by_ticker.setdefault(str(row['ticker']), set()).add(str(row['component_name']))
    matrix_ok = (
        len(components) == len(expected_tickers) * len(expected_components)
        and set(names_by_ticker) == expected_tickers
        and all(names == expected_components for names in names_by_ticker.values())
    )
    _check(
        checks,
        'component_matrix_exact',
        matrix_ok,
        observed=len(components),
        expected=len(expected_tickers) * len(expected_components),
    )
    bad_weights = [
        (str(row['ticker']), str(row['component_name']))
        for row in components
        if float(row['component_weight']) != 0.0
    ]
    _check(checks, 'all_component_weights_zero', not bad_weights, rows=bad_weights[:20])
    invalid_numbers: list[tuple[str, str, str]] = []
    neutral_fills: list[tuple[str, str]] = []
    for row in components:
        for field in ('raw_value', 'normalized_value', 'component_score'):
            value = row[field]
            if value is not None and not math.isfinite(float(value)):
                invalid_numbers.append((str(row['ticker']), str(row['component_name']), field))
        normalized = row['normalized_value']
        if normalized is not None and not 0.0 <= float(normalized) <= 100.0:
            invalid_numbers.append(
                (str(row['ticker']), str(row['component_name']), 'normalized_range')
            )
        if row['raw_value'] is None and (
            row['normalized_value'] is not None or row['component_score'] is not None
        ):
            neutral_fills.append((str(row['ticker']), str(row['component_name'])))
    _check(checks, 'component_values_finite_and_bounded', not invalid_numbers, rows=invalid_numbers[:20])
    _check(checks, 'missing_values_not_neutral_filled', not neutral_fills, rows=neutral_fills[:20])

    identity_errors = [
        (str(row['ticker']), str(row['component_name']))
        for row in components
        if str(row['component_observation_id']) != component_observation_id(dict(row))
    ]
    _check(
        checks,
        'component_observation_ids_exact',
        not identity_errors,
        rows=identity_errors[:20],
    )
    input_identity_errors = [
        str(row['ticker'])
        for row in inputs
        if str(row['input_observation_id']) != input_observation_id(dict(row))
    ]
    _check(
        checks,
        'input_observation_ids_exact',
        not input_identity_errors,
        tickers=input_identity_errors[:20],
    )
    contract_errors = [
        (str(row['ticker']), str(row['component_name']))
        for row in components
        if str(row['definition_version']) != definition_version
        or str(row['contract_sha256']) != contract_sha
    ]
    contract_errors.extend(
        (str(row['ticker']), 'input')
        for row in inputs
        if str(row['definition_version']) != definition_version
        or str(row['contract_sha256']) != contract_sha
        or str(row['source_id']) != source_id
    )
    _check(
        checks,
        'definition_and_contract_hash_exact',
        not contract_errors,
        rows=contract_errors[:20],
    )
    future_sources = [
        (str(row['ticker']), str(row['component_name']), str(row['source_asof_date']))
        for row in components
        if row['source_asof_date'] is not None and str(row['source_asof_date']) > as_of
    ]
    _check(
        checks,
        'component_sources_point_in_time',
        not future_sources,
        rows=future_sources[:20],
    )
    member_by_ticker = {str(row['ticker']): row for row in universe}
    metric_by_id = {metric.metric_id: metric for metric in metrics}
    specialized_errors: list[tuple[str, str, str]] = []
    for row in components:
        if str(row['component_group']) != 'specialized':
            continue
        ticker = str(row['ticker'])
        metric_id = str(row['source_field'])
        metric = metric_by_id.get(metric_id)
        member = member_by_ticker.get(ticker)
        if metric is None or member is None:
            specialized_errors.append((ticker, metric_id, 'unknown_contract_identity'))
            continue
        applicable = _specialized_applicable(
            metric,
            cohort_id=str(member['calibration_cohort_id']),
            subtype=str(member['applicability_subtype'] or ''),
        )
        expected_status = 'not_loaded' if applicable else 'not_applicable'
        measurement_only = (
            applicable
            and str(row['availability_status']) == 'measurement_only'
        )
        measurement_valid = False
        if measurement_only:
            try:
                overlay = json.loads(str(row['lineage_json']))[
                    'stage6b_overlay'
                ]
                observation_sha = str(overlay['observation_sha256'])
            except (json.JSONDecodeError, KeyError, TypeError):
                observation_sha = ''
            observation = conn.execute(
                '''SELECT ticker,metric_id,numeric_value,accepted_at,source_id,
                          production_status,evidence_status
                   FROM fact_specialized_metric_observation
                   WHERE observation_sha256=?''',
                (observation_sha,),
            ).fetchone()
            measurement_valid = (
                observation is not None
                and str(observation['ticker']) == ticker
                and str(observation['metric_id']) == metric_id
                and observation['numeric_value'] is not None
                and row['raw_value'] is not None
                and float(observation['numeric_value']) == float(row['raw_value'])
                and str(observation['accepted_at'])[:10] <= as_of
                and str(observation['source_id']) == str(row['source_id'])
                and str(observation['production_status']) == 'measurement_only'
                and str(observation['evidence_status'])
                == 'accepted_measurement_only'
                and str(row['production_status']) == 'measurement_only'
                and str(row['quality_status']) == 'accepted_measurement_only'
                and row['normalized_value'] is None
                and row['component_score'] is None
                and float(row['component_weight']) == 0.0
            )
        baseline_valid = (
            str(row['availability_status']) == expected_status
            and row['raw_value'] is None
            and row['normalized_value'] is None
            and row['component_score'] is None
            and float(row['component_weight']) == 0.0
        )
        if not (measurement_valid or baseline_valid):
            specialized_errors.append((ticker, metric_id, expected_status))
    _check(
        checks,
        'specialized_metrics_reserved_not_promoted',
        not specialized_errors,
        rows=specialized_errors[:20],
        metric_count=len(metrics),
    )

    components_by_ticker: dict[str, list[dict[str, Any]]] = {
        ticker: [] for ticker in expected_tickers
    }
    for row in components:
        components_by_ticker.setdefault(str(row['ticker']), []).append(dict(row))
    readiness_errors: list[tuple[str, str]] = []
    lineage_errors: list[str] = []
    for row in inputs:
        ticker = str(row['ticker'])
        ticker_components = components_by_ticker.get(ticker, [])
        expected_ready, reasons = _rank_readiness(ticker_components)
        core_available = sum(
            component['component_group'] != 'specialized'
            and component['availability_status'] == 'available'
            and component['normalized_value'] is not None
            for component in ticker_components
        )
        expected_reason = None if expected_ready else ';'.join(sorted(reasons))
        if (
            int(row['rank_ready_flag']) != expected_ready
            or str(row['feature_status'])
            != ('rank_ready' if expected_ready else 'review_required')
            or row['review_reason'] != expected_reason
            or int(row['core_available_component_count']) != core_available
            or int(row['core_missing_component_count'])
            != len(CORE_COMPONENT_SPECS) - core_available
            or int(row['calibration_eligible_flag']) != 1
        ):
            readiness_errors.append((ticker, expected_reason or 'rank_ready'))
        try:
            lineage_ids = json.loads(str(row['lineage_json']))['component_observation_ids']
        except (json.JSONDecodeError, KeyError, TypeError):
            lineage_ids = None
        expected_ids = sorted(
            str(component['component_observation_id'])
            for component in ticker_components
        )
        if lineage_ids != expected_ids:
            lineage_errors.append(ticker)
    _check(
        checks,
        'rank_readiness_recomputes_exactly',
        not readiness_errors,
        rows=readiness_errors[:20],
    )
    _check(
        checks,
        'input_component_lineage_complete',
        not lineage_errors,
        tickers=lineage_errors[:20],
    )
    rank_ready_count = sum(int(row['rank_ready_flag']) for row in inputs)
    rank_ready_fraction = rank_ready_count / len(inputs) if inputs else 0.0
    minimum_fraction = float(
        cfg_get(bundle.payload, 'scoring_features.minimum_rank_ready_fraction')
    )
    _check(
        checks,
        'rank_ready_coverage_sufficient',
        rank_ready_fraction >= minimum_fraction,
        rank_ready_count=rank_ready_count,
        ticker_count=len(inputs),
        observed_fraction=rank_ready_fraction,
        minimum_fraction=minimum_fraction,
    )
    confidence_errors = [
        str(row['ticker'])
        for row in inputs
        if row['core_data_quality_confidence'] is None
        or not 0.0 <= float(row['core_data_quality_confidence']) <= 1.0
        or row['full_data_quality_confidence'] is None
        or not 0.0 <= float(row['full_data_quality_confidence']) <= 1.0
    ]
    _check(
        checks,
        'data_quality_confidence_bounded',
        not confidence_errors,
        tickers=confidence_errors[:20],
    )
    model_output_count = int(
        conn.execute(
            '''
            SELECT COUNT(*) FROM feature_scoring_model_output
            WHERE model_family='consumer_defensive' AND asof_date=?
            ''',
            (as_of,),
        ).fetchone()[0]
    )
    _check(
        checks,
        'production_scores_and_ranks_absent',
        model_output_count == 0,
        rows=model_output_count,
    )
    foreign_key_violation = conn.execute('PRAGMA foreign_key_check').fetchone()
    _check(
        checks,
        'foreign_keys_valid',
        foreign_key_violation is None,
        first_violation=list(foreign_key_violation) if foreign_key_violation else None,
    )
    status = 'PASS' if all(bool(row['passed']) for row in checks) else 'FAIL'
    return {
        'status': status,
        'asof_date': as_of,
        'definition_version': definition_version,
        'contract_sha256': contract_sha,
        'checks': checks,
        'summary': {
            'source_live_ticker_count': len(source_universe),
            'excluded_ticker_count': scope_summary['observed_excluded_ticker_count'],
            'calibration_scope_sha256': scope_summary['contract']['payload_sha256'],
            'ticker_count': len(inputs),
            'component_count': len(components),
            'core_component_count': len(CORE_COMPONENT_SPECS),
            'specialized_component_count': len(metrics),
            'rank_ready_count': rank_ready_count,
            'review_required_count': len(inputs) - rank_ready_count,
            'rank_ready_fraction': rank_ready_fraction,
            'passed_checks': sum(bool(row['passed']) for row in checks),
            'total_checks': len(checks),
        },
    }
