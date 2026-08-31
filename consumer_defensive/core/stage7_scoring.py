from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from collections import defaultdict
from typing import Any, Iterable

from .calibration_scope import (
    apply_current_production_scope,
    calibration_scope_contract,
)
from .config import ConfigBundle, cfg_get, resolve_path
from .db import utc_now
from .metric_registry import load_metric_registry
from .scoring_features import (
    CORE_COMPONENT_SPECS,
    component_observation_id,
    input_observation_id,
    scoring_contract_sha256,
    validate_scoring_features,
)
from .source_registry import load_source_registry, upsert_source_registry
from .stage6a_schema import ensure_stage6a_schema
from .stage6b_schema import ensure_stage6b_schema
from .stage7_schema import (
    STAGE7_MIGRATION_SHA256,
    STAGE7_SCHEMA_VERSION,
    ensure_stage7_schema,
)


MODEL_FAMILY = 'consumer_defensive'
CORE_COMPONENT_NAMES = tuple(spec.name for spec in CORE_COMPONENT_SPECS)
SPECIALIZED_PREFIX = 'specialized:'

OUTPUT_IDENTITY_FIELDS = (
    'ticker', 'asof_date', 'source_id', 'model_family', 'model_version',
    'baseline_source_id', 'baseline_input_observation_id',
    'calibration_cohort_id', 'core_score', 'final_score', 'final_rank',
    'final_percentile', 'cohort_rank', 'cohort_percentile',
    'component_weights_json', 'component_scores_json',
    'component_quality_json', 'data_quality_confidence',
    'full_data_quality_confidence', 'rank_ready_flag',
    'calibration_eligible_flag', 'model_status', 'review_reason',
    'promotion_state', 'portfolio_candidate_gate', 'oos_score_valid_flag',
    'model_contract_sha256', 'lineage_json',
)


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value, sort_keys=True, separators=(',', ':'), ensure_ascii=True
    )


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode('utf-8')).hexdigest()


def _finite(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _check(
    checks: list[dict[str, Any]],
    name: str,
    passed: bool,
    **details: Any,
) -> None:
    checks.append({'check': name, 'passed': bool(passed), **details})


def stage7_component_weights(bundle: ConfigBundle) -> dict[str, float]:
    raw = cfg_get(bundle.payload, 'stage7_scoring.component_weights')
    if not isinstance(raw, dict):
        raise ValueError('Stage 7 component weights must be a mapping.')
    observed = {str(name) for name in raw}
    expected = set(CORE_COMPONENT_NAMES)
    if observed != expected:
        raise ValueError(
            'Stage 7 component weights must contain exactly the Stage 6A core '
            f'components; missing={sorted(expected - observed)} '
            f'unexpected={sorted(observed - expected)}.'
        )
    weights: dict[str, float] = {}
    for name in CORE_COMPONENT_NAMES:
        value = _finite(raw[name])
        if value is None or not 0.0 <= value <= 1.0:
            raise ValueError(
                f'Stage 7 component weight for {name} must be finite in [0,1].'
            )
        weights[name] = value
    total = sum(weights.values())
    if abs(total - 1.0) > 1e-12:
        raise ValueError(
            f'Stage 7 component weights must sum exactly to 1.0; found {total:.12f}.'
        )
    return weights


def _specialized_component_names(bundle: ConfigBundle) -> tuple[str, ...]:
    path = resolve_path(
        cfg_get(bundle.payload, 'specialized_metrics.registry_path'),
        base_dir=bundle.base_dir,
    )
    _, metrics = load_metric_registry(path)
    return tuple(f'{SPECIALIZED_PREFIX}{metric.metric_id}' for metric in metrics)


def stage7_contract_payload(bundle: ConfigBundle) -> dict[str, Any]:
    weights = stage7_component_weights(bundle)
    scope_contract = calibration_scope_contract(bundle)
    return {
        'schema_version': 'consumer_defensive_stage7_contract_v3',
        'model_family': MODEL_FAMILY,
        'source_id': str(cfg_get(bundle.payload, 'stage7_scoring.source_id')),
        'baseline_source_id': str(
            cfg_get(bundle.payload, 'stage7_scoring.baseline_source_id')
        ),
        'model_version': str(
            cfg_get(bundle.payload, 'stage7_scoring.model_version')
        ),
        'stage6_scoring_contract_sha256': scoring_contract_sha256(bundle),
        'calibration_scope_sha256': scope_contract['payload_sha256'],
        'neutral_score': float(
            cfg_get(bundle.payload, 'stage7_scoring.neutral_score')
        ),
        'minimum_data_quality_confidence': float(
            cfg_get(
                bundle.payload,
                'stage7_scoring.minimum_data_quality_confidence',
            )
        ),
        'maximum_missing_component_weight': float(
            cfg_get(
                bundle.payload,
                'stage7_scoring.maximum_missing_component_weight',
            )
        ),
        'minimum_rank_ready_fraction': float(
            cfg_get(bundle.payload, 'stage7_scoring.minimum_rank_ready_fraction')
        ),
        'normalization_policy': (
            'stage6a_reviewed_scope_then_point_in_time_cohort_then_'
            'scoped_universe_fallback'
        ),
        'missing_value_policy': (
            'neutral_score_contribution_no_weight_redistribution'
        ),
        'rank_tie_break_policy': (
            'score_descending_then_ticker_ascending_ordinal'
        ),
        'nonapplicable_policy': 'excluded_from_denominator',
        'specialized_weight_default': float(
            cfg_get(bundle.payload, 'stage7_scoring.specialized_weight_default')
        ),
        'specialized_weight_policy': str(
            cfg_get(bundle.payload, 'stage7_scoring.specialized_weight_policy')
        ),
        'factor_validation_campaign_id': str(
            cfg_get(bundle.payload, 'stage7_scoring.factor_validation_campaign_id')
        ),
        'factor_validation_verdict': str(
            cfg_get(bundle.payload, 'stage7_scoring.factor_validation_verdict')
        ),
        'factor_validation_evidence_posture': (
            'corrected_campaign_verified_shadow_only'
        ),
        'component_weights': weights,
        'specialized_components': list(_specialized_component_names(bundle)),
        'promotion_state': str(
            cfg_get(bundle.payload, 'stage7_scoring.promotion_state')
        ),
        'portfolio_candidate_gate': int(
            cfg_get(bundle.payload, 'stage7_scoring.portfolio_candidate_gate')
        ),
        'oos_score_valid_flag': int(
            cfg_get(bundle.payload, 'stage7_scoring.oos_score_valid_flag')
        ),
    }


def stage7_contract_sha256(bundle: ConfigBundle) -> str:
    return _sha256(stage7_contract_payload(bundle))


def score_observation_id(row: dict[str, Any] | sqlite3.Row) -> str:
    return _sha256({field: row[field] for field in OUTPUT_IDENTITY_FIELDS})


def bootstrap_stage7(conn: sqlite3.Connection, bundle: ConfigBundle) -> str:
    ensure_stage6a_schema(conn)
    ensure_stage6b_schema(conn)
    ensure_stage7_schema(conn)
    source_id = str(cfg_get(bundle.payload, 'stage7_scoring.source_id'))
    baseline_source_id = str(
        cfg_get(bundle.payload, 'stage7_scoring.baseline_source_id')
    )
    registry_path = resolve_path(
        cfg_get(bundle.payload, 'source_registry.path'), base_dir=bundle.base_dir
    )
    registry = load_source_registry(registry_path)
    selected = [
        row for row in registry
        if row.source_id in {source_id, baseline_source_id}
    ]
    if {row.source_id for row in selected} != {source_id, baseline_source_id}:
        raise RuntimeError(
            'Stage 7 source registry is missing its score or baseline source.'
        )
    if any(row.status != 'active' for row in selected):
        raise RuntimeError('Stage 7 score and baseline sources must be active.')
    upsert_source_registry(conn, selected)
    stage7_component_weights(bundle)
    return stage7_contract_sha256(bundle)


def _current_universe(
    conn: sqlite3.Connection,
    *,
    as_of: str,
) -> list[dict[str, Any]]:
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


def _baseline_inputs(
    conn: sqlite3.Connection,
    *,
    as_of: str,
    baseline_source_id: str,
) -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in conn.execute(
            '''SELECT * FROM feature_scoring_input
               WHERE model_family='consumer_defensive' AND asof_date=?
                 AND source_id=? ORDER BY ticker''',
            (as_of, baseline_source_id),
        )
    ]


def _components(
    conn: sqlite3.Connection,
    *,
    as_of: str,
) -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in conn.execute(
            '''SELECT * FROM feature_scoring_component
               WHERE model_family='consumer_defensive' AND asof_date=?
               ORDER BY ticker,component_name''',
            (as_of,),
        )
    ]


def _stage6_prerequisite_errors(
    conn: sqlite3.Connection,
    bundle: ConfigBundle,
    *,
    as_of: str,
) -> list[str]:
    validation = validate_scoring_features(conn, bundle, as_of=as_of)
    return [
        str(row['check'])
        for row in validation['checks']
        if not bool(row['passed'])
        and str(row['check']) != 'production_scores_and_ranks_absent'
    ]


def _verify_atomic_inputs(
    conn: sqlite3.Connection,
    bundle: ConfigBundle,
    *,
    as_of: str,
    inputs: list[dict[str, Any]],
    components: list[dict[str, Any]],
    require_stage6b_overlay: bool | None = None,
) -> None:
    source_universe = _current_universe(conn, as_of=as_of)
    universe, scope_summary = apply_current_production_scope(
        source_universe,
        bundle,
    )
    expected_tickers = {str(row['ticker']) for row in universe}
    input_tickers = {str(row['ticker']) for row in inputs}
    if not expected_tickers or input_tickers != expected_tickers:
        raise RuntimeError(
            'Stage 7 baseline inputs do not match the reviewed production scope: '
            f'missing={sorted(expected_tickers - input_tickers)} '
            f'unexpected={sorted(input_tickers - expected_tickers)} '
            f'scope_sha256={scope_summary["contract"]["payload_sha256"]}.'
        )
    if len(inputs) != len(input_tickers):
        raise RuntimeError('Stage 7 baseline inputs contain duplicate tickers.')

    input_errors = [
        str(row['ticker']) for row in inputs
        if str(row['input_observation_id']) != input_observation_id(row)
    ]
    component_errors = [
        f"{row['ticker']}:{row['component_name']}"
        for row in components
        if str(row['component_observation_id'])
        != component_observation_id(row)
    ]
    if input_errors or component_errors:
        raise RuntimeError(
            'Stage 7 input identity mismatch: '
            f'inputs={input_errors[:10]} components={component_errors[:10]}.'
        )

    by_ticker: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for component in components:
        by_ticker[str(component['ticker'])].append(component)
    expected_core = set(CORE_COMPONENT_NAMES)
    for ticker in sorted(expected_tickers):
        core = [
            row for row in by_ticker.get(ticker, [])
            if str(row['component_group']) != 'specialized'
        ]
        names = [str(row['component_name']) for row in core]
        if len(names) != len(set(names)) or set(names) != expected_core:
            raise RuntimeError(
                f'Stage 7 core component matrix is invalid for {ticker}: '
                f'{sorted(names)}.'
            )
    unexpected_tickers = set(by_ticker) - expected_tickers
    if unexpected_tickers:
        raise RuntimeError(
            'Stage 7 component matrix contains non-universe tickers: '
            f'{sorted(unexpected_tickers)}.'
        )
    nonzero_specialized = [
        f"{row['ticker']}:{row['component_name']}"
        for row in components
        if str(row['component_group']) == 'specialized'
        and float(row['component_weight']) != 0.0
    ]
    if nonzero_specialized:
        raise RuntimeError(
            'Stage 7 cannot consume nonzero specialized weights: '
            f'{nonzero_specialized[:10]}.'
        )
    future_sources = [
        f"{row['ticker']}:{row['component_name']}:{row['source_asof_date']}"
        for row in components
        if str(row['source_asof_date'] or '')[:10] > as_of
    ]
    if future_sources:
        raise RuntimeError(
            f'Stage 7 component matrix contains future inputs: {future_sources[:10]}.'
        )
    overlay_required = (
        bool(
            cfg_get(
                bundle.payload,
                'stage7_scoring.require_stage6b_measurement_overlay',
            )
        )
        if require_stage6b_overlay is None
        else bool(require_stage6b_overlay)
    )
    if overlay_required:
        table_exists = conn.execute(
            """SELECT 1 FROM sqlite_master
               WHERE type='table' AND name='stage6b_specialized_run'"""
        ).fetchone()
        if table_exists is None:
            raise RuntimeError(
                'Stage 7 requires the accepted Stage 6B overlay, but the '
                'Stage 6B run table is absent.'
            )
        stage6b_run = conn.execute(
            '''SELECT stage6b_run_id FROM stage6b_specialized_run
               WHERE asof_date=? AND status='measurement_only_complete'
               ORDER BY stage6b_run_id DESC LIMIT 1''',
            (as_of,),
        ).fetchone()
        expected_stage6b_run_id = (
            int(stage6b_run['stage6b_run_id']) if stage6b_run else None
        )
        missing_overlay: list[str] = []
        for row in inputs:
            try:
                lineage = json.loads(str(row['lineage_json']))
            except (TypeError, json.JSONDecodeError):
                lineage = {}
            if (
                lineage.get('stage6b_measurement_overlay') is not True
                or expected_stage6b_run_id is None
                or lineage.get('stage6b_run_id') != expected_stage6b_run_id
            ):
                missing_overlay.append(str(row['ticker']))
        if missing_overlay:
            raise RuntimeError(
                'Stage 7 requires the accepted Stage 6B overlay for every '
                f'baseline row: {missing_overlay[:10]}.'
            )


def _percentile(rank: int, count: int) -> float | None:
    if count <= 0:
        return None
    return 100.0 * (count - rank + 0.5) / count


def _assign_ranks(outputs: list[dict[str, Any]]) -> None:
    rankable = sorted(
        [row for row in outputs if int(row['rank_ready_flag']) == 1],
        key=lambda row: (-float(row['final_score']), str(row['ticker'])),
    )
    for rank, row in enumerate(rankable, start=1):
        row['final_rank'] = rank
        row['final_percentile'] = _percentile(rank, len(rankable))
    for row in outputs:
        row.setdefault('final_rank', None)
        row.setdefault('final_percentile', None)

    cohorts: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rankable:
        cohorts[str(row['calibration_cohort_id'])].append(row)
    for rows in cohorts.values():
        rows.sort(key=lambda row: (-float(row['final_score']), str(row['ticker'])))
        for rank, row in enumerate(rows, start=1):
            row['cohort_rank'] = rank
            row['cohort_percentile'] = _percentile(rank, len(rows))
    for row in outputs:
        row.setdefault('cohort_rank', None)
        row.setdefault('cohort_percentile', None)


def _independent_score_tieout_errors(
    rows: Iterable[dict[str, Any]],
    *,
    expected_weights: dict[str, float],
) -> list[str]:
    errors: list[str] = []
    expected_names = set(expected_weights)
    for row in rows:
        ticker = str(row['ticker'])
        try:
            weights = json.loads(str(row['component_weights_json']))
            scores = json.loads(str(row['component_scores_json']))
            quality = json.loads(str(row['component_quality_json']))
        except (TypeError, json.JSONDecodeError):
            errors.append(ticker)
            continue
        if (
            not isinstance(weights, dict)
            or not isinstance(scores, dict)
            or not isinstance(quality, dict)
            or set(weights) != expected_names
            or set(scores) != expected_names
            or set(quality) != expected_names
        ):
            errors.append(ticker)
            continue
        score = 0.0
        available_weight = 0.0
        valid = True
        for name in expected_weights:
            stored_weight = _finite(weights.get(name))
            stored_score = _finite(scores.get(name))
            stored_quality = _finite(quality.get(name))
            if (
                stored_weight is None
                or not math.isclose(
                    stored_weight, expected_weights[name],
                    rel_tol=0.0, abs_tol=1e-12,
                )
                or stored_score is None
                or not 0.0 <= stored_score <= 100.0
                or stored_quality not in {0.0, 1.0}
            ):
                valid = False
                break
            score += stored_weight * stored_score
            available_weight += stored_weight * stored_quality
        if (
            not valid
            or not math.isclose(
                float(row['core_score']), score,
                rel_tol=0.0, abs_tol=1e-10,
            )
            or not math.isclose(
                float(row['final_score']), score,
                rel_tol=0.0, abs_tol=1e-10,
            )
            or not math.isclose(
                float(row['data_quality_confidence']), available_weight,
                rel_tol=0.0, abs_tol=1e-12,
            )
        ):
            errors.append(ticker)
    return errors


def _independent_rank_tieout_errors(
    rows: Iterable[dict[str, Any]],
) -> list[str]:
    materialized = list(rows)
    rankable = sorted(
        [row for row in materialized if int(row['rank_ready_flag']) == 1],
        key=lambda row: (-float(row['final_score']), str(row['ticker'])),
    )
    errors: set[str] = set()

    def verify(scope_rows: list[dict[str, Any]], *, cohort: bool) -> None:
        rank_field = 'cohort_rank' if cohort else 'final_rank'
        percentile_field = (
            'cohort_percentile' if cohort else 'final_percentile'
        )
        for expected_rank, row in enumerate(scope_rows, start=1):
            expected_percentile = _percentile(expected_rank, len(scope_rows))
            observed_rank = row.get(rank_field)
            observed_percentile = _finite(row.get(percentile_field))
            if (
                observed_rank is None
                or int(observed_rank) != expected_rank
                or observed_percentile is None
                or expected_percentile is None
                or not math.isclose(
                    observed_percentile, expected_percentile,
                    rel_tol=0.0, abs_tol=1e-10,
                )
            ):
                errors.add(str(row['ticker']))

    verify(rankable, cohort=False)
    cohorts: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rankable:
        cohorts[str(row['calibration_cohort_id'])].append(row)
    for cohort_rows in cohorts.values():
        cohort_rows.sort(
            key=lambda row: (-float(row['final_score']), str(row['ticker']))
        )
        verify(cohort_rows, cohort=True)
    for row in materialized:
        if int(row['rank_ready_flag']) == 0 and any(
            row.get(field) is not None
            for field in (
                'final_rank', 'final_percentile',
                'cohort_rank', 'cohort_percentile',
            )
        ):
            errors.add(str(row['ticker']))
    return sorted(errors)


def _expected_outputs(
    bundle: ConfigBundle,
    *,
    as_of: str,
    contract_sha: str,
    inputs: list[dict[str, Any]],
    components: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    weights = stage7_component_weights(bundle)
    neutral = float(cfg_get(bundle.payload, 'stage7_scoring.neutral_score'))
    minimum_quality = float(
        cfg_get(bundle.payload, 'stage7_scoring.minimum_data_quality_confidence')
    )
    maximum_missing = float(
        cfg_get(bundle.payload, 'stage7_scoring.maximum_missing_component_weight')
    )
    source_id = str(cfg_get(bundle.payload, 'stage7_scoring.source_id'))
    baseline_source_id = str(
        cfg_get(bundle.payload, 'stage7_scoring.baseline_source_id')
    )
    model_version = str(cfg_get(bundle.payload, 'stage7_scoring.model_version'))
    promotion_state = str(
        cfg_get(bundle.payload, 'stage7_scoring.promotion_state')
    )
    portfolio_gate = int(
        cfg_get(bundle.payload, 'stage7_scoring.portfolio_candidate_gate')
    )
    oos_flag = int(cfg_get(bundle.payload, 'stage7_scoring.oos_score_valid_flag'))
    scope_sha = str(calibration_scope_contract(bundle)['payload_sha256'])
    by_ticker: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
    for component in components:
        by_ticker[str(component['ticker'])][
            str(component['component_name'])
        ] = component

    outputs: list[dict[str, Any]] = []
    for input_row in inputs:
        ticker = str(input_row['ticker'])
        component_scores: dict[str, float] = {}
        component_quality: dict[str, float] = {}
        weighted_score = 0.0
        available_weight = 0.0
        missing_weight = 0.0
        missing_components: list[str] = []
        component_ids: list[str] = []
        for name in CORE_COMPONENT_NAMES:
            component = by_ticker[ticker][name]
            component_ids.append(str(component['component_observation_id']))
            score = _finite(component['component_score'])
            available = (
                str(component['availability_status']) == 'available'
                and score is not None
            )
            effective_score = (
                min(100.0, max(0.0, score))
                if score is not None and available
                else neutral
            )
            component_scores[name] = effective_score
            component_quality[name] = 1.0 if available else 0.0
            weight = weights[name]
            weighted_score += weight * effective_score
            if available:
                available_weight += weight
            else:
                missing_weight += weight
                if weight > 0.0:
                    missing_components.append(name)

        baseline_ready = int(input_row['rank_ready_flag']) == 1
        reasons: list[str] = []
        if not baseline_ready:
            reasons.append(
                'baseline_not_rank_ready:'
                + str(input_row['review_reason'] or 'unspecified')
            )
        if available_weight < minimum_quality:
            reasons.append(f'low_data_quality={available_weight:.6f}')
        if missing_weight > maximum_missing:
            reasons.append(
                f'missing_component_weight={missing_weight:.6f}:'
                + ','.join(missing_components)
            )
        rank_ready = int(not reasons)
        calibration_eligible = int(
            int(input_row['calibration_eligible_flag']) == 1
            and rank_ready == 1
        )
        lineage = {
            'baseline_source_id': baseline_source_id,
            'baseline_input_observation_id': str(
                input_row['input_observation_id']
            ),
            'stage6_contract_sha256': str(input_row['contract_sha256']),
            'calibration_scope_sha256': scope_sha,
            'core_component_observation_ids': sorted(component_ids),
            'missing_components': missing_components,
            'missing_component_weight': missing_weight,
            'missing_value_policy': (
                'neutral_score_contribution_no_weight_redistribution'
            ),
            'rank_tie_break_policy': (
                'score_descending_then_ticker_ascending_ordinal'
            ),
            'normalization_policy': (
                'stage6a_reviewed_scope_then_point_in_time_cohort_then_'
                'scoped_universe_fallback'
            ),
            'specialized_weight': 0.0,
            'specialized_weight_policy': str(
                cfg_get(
                    bundle.payload,
                    'stage7_scoring.specialized_weight_policy',
                )
            ),
            'factor_validation_campaign_id': str(
                cfg_get(
                    bundle.payload,
                    'stage7_scoring.factor_validation_campaign_id',
                )
            ),
            'factor_validation_verdict': str(
                cfg_get(
                    bundle.payload,
                    'stage7_scoring.factor_validation_verdict',
                )
            ),
        }
        output = {
            'ticker': ticker,
            'asof_date': as_of,
            'source_id': source_id,
            'model_family': MODEL_FAMILY,
            'model_version': model_version,
            'baseline_source_id': baseline_source_id,
            'baseline_input_observation_id': str(
                input_row['input_observation_id']
            ),
            'calibration_cohort_id': str(
                input_row['calibration_cohort_id']
            ),
            'core_score': weighted_score,
            'final_score': weighted_score,
            'component_weights_json': _canonical_json(weights),
            'component_scores_json': _canonical_json(component_scores),
            'component_quality_json': _canonical_json(component_quality),
            'data_quality_confidence': available_weight,
            'full_data_quality_confidence': float(
                input_row['full_data_quality_confidence'] or 0.0
            ),
            'rank_ready_flag': rank_ready,
            'calibration_eligible_flag': calibration_eligible,
            'model_status': (
                'shadow_ready' if rank_ready else 'review_required'
            ),
            'review_reason': ';'.join(reasons) if reasons else None,
            'promotion_state': promotion_state,
            'portfolio_candidate_gate': portfolio_gate,
            'oos_score_valid_flag': oos_flag,
            'model_contract_sha256': contract_sha,
            'lineage_json': _canonical_json(lineage),
        }
        outputs.append(output)
    _assign_ranks(outputs)
    for output in outputs:
        output['score_observation_id'] = score_observation_id(output)
    return outputs


def _manifest(values: Iterable[str]) -> str:
    return _sha256(list(values))


def _weight_rows(
    conn: sqlite3.Connection,
    bundle: ConfigBundle,
    *,
    contract_sha: str,
    now: str,
) -> list[tuple[Any, ...]]:
    source_id = str(cfg_get(bundle.payload, 'stage7_scoring.source_id'))
    model_version = str(cfg_get(bundle.payload, 'stage7_scoring.model_version'))
    campaign = str(
        cfg_get(bundle.payload, 'stage7_scoring.factor_validation_campaign_id')
    )
    weights = stage7_component_weights(bundle)
    contract_rows = list(
        conn.execute(
            '''SELECT component_name,component_group
               FROM stage6a_component_contract ORDER BY component_name'''
        )
    )
    observed = {str(row['component_name']) for row in contract_rows}
    expected = set(CORE_COMPONENT_NAMES) | set(_specialized_component_names(bundle))
    if observed != expected:
        raise RuntimeError(
            'Stage 7 weight contract cannot bind an incomplete Stage 6A '
            f'component contract: missing={sorted(expected - observed)} '
            f'unexpected={sorted(observed - expected)}.'
        )
    rows: list[tuple[Any, ...]] = []
    for row in contract_rows:
        name = str(row['component_name'])
        specialized = str(row['component_group']) == 'specialized'
        weight = 0.0 if specialized else weights[name]
        status = (
            'locked_zero_corrected_campaign_no_directionally_accepted_evidence'
            if specialized else 'reviewed_shadow_baseline'
        )
        reference = campaign if specialized else 'stage7_baseline_review_v1'
        rows.append(
            (
                source_id, MODEL_FAMILY, model_version, '*', name,
                str(row['component_group']), weight, status, reference,
                contract_sha, now, now,
            )
        )
    return rows


def _actual_output_rows(
    conn: sqlite3.Connection,
    *,
    source_id: str,
    as_of: str,
) -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in conn.execute(
            '''SELECT * FROM feature_scoring_model_output
               WHERE source_id=? AND model_family='consumer_defensive'
                 AND asof_date=? ORDER BY ticker''',
            (source_id, as_of),
        )
    ]


def _insert_outputs(
    conn: sqlite3.Connection,
    *,
    outputs: list[dict[str, Any]],
    now: str,
) -> None:
    conn.executemany(
        '''INSERT INTO feature_scoring_model_output(
               model_family,ticker,asof_date,source_id,final_score,final_rank,
               promotion_state,portfolio_candidate_gate,oos_score_valid_flag,
               model_version,created_at,baseline_source_id,
               baseline_input_observation_id,calibration_cohort_id,core_score,
               final_percentile,cohort_rank,cohort_percentile,
               data_quality_confidence,full_data_quality_confidence,
               rank_ready_flag,calibration_eligible_flag,model_status,
               review_reason,component_weights_json,component_scores_json,
               component_quality_json,model_contract_sha256,lineage_json,
               score_observation_id,updated_at
           ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
        [
            (
                row['model_family'], row['ticker'], row['asof_date'],
                row['source_id'], row['final_score'], row['final_rank'],
                row['promotion_state'], row['portfolio_candidate_gate'],
                row['oos_score_valid_flag'], row['model_version'], now,
                row['baseline_source_id'],
                row['baseline_input_observation_id'],
                row['calibration_cohort_id'], row['core_score'],
                row['final_percentile'], row['cohort_rank'],
                row['cohort_percentile'], row['data_quality_confidence'],
                row['full_data_quality_confidence'], row['rank_ready_flag'],
                row['calibration_eligible_flag'], row['model_status'],
                row['review_reason'], row['component_weights_json'],
                row['component_scores_json'], row['component_quality_json'],
                row['model_contract_sha256'], row['lineage_json'],
                row['score_observation_id'], now,
            )
            for row in outputs
        ],
    )


def build_stage7_scores(
    conn: sqlite3.Connection,
    bundle: ConfigBundle,
    *,
    as_of: str,
) -> dict[str, Any]:
    contract_sha = bootstrap_stage7(conn, bundle)
    prerequisite_errors = _stage6_prerequisite_errors(
        conn, bundle, as_of=as_of
    )
    if prerequisite_errors:
        raise RuntimeError(
            'Stage 7 requires a passing Stage 6 feature contract; failed='
            f'{prerequisite_errors}.'
        )
    baseline_source_id = str(
        cfg_get(bundle.payload, 'stage7_scoring.baseline_source_id')
    )
    source_id = str(cfg_get(bundle.payload, 'stage7_scoring.source_id'))
    inputs = _baseline_inputs(
        conn, as_of=as_of, baseline_source_id=baseline_source_id
    )
    components = _components(conn, as_of=as_of)
    _verify_atomic_inputs(
        conn,
        bundle,
        as_of=as_of,
        inputs=inputs,
        components=components,
    )
    outputs = _expected_outputs(
        bundle,
        as_of=as_of,
        contract_sha=contract_sha,
        inputs=inputs,
        components=components,
    )
    minimum_fraction = float(
        cfg_get(bundle.payload, 'stage7_scoring.minimum_rank_ready_fraction')
    )
    rank_ready = sum(int(row['rank_ready_flag']) for row in outputs)
    if not outputs or rank_ready / len(outputs) < minimum_fraction:
        raise RuntimeError(
            'Stage 7 rank-ready coverage is below its frozen floor: '
            f'{rank_ready}/{len(outputs)} < {minimum_fraction:.2%}.'
        )
    baseline_manifest = _manifest(
        str(row['input_observation_id']) for row in inputs
    )
    output_manifest = _manifest(
        str(row['score_observation_id']) for row in outputs
    )
    now = utc_now()
    payload = stage7_contract_payload(bundle)
    model_version = str(payload['model_version'])
    weight_rows = _weight_rows(
        conn, bundle, contract_sha=contract_sha, now=now
    )

    existing_outputs = _actual_output_rows(
        conn, source_id=source_id, as_of=as_of
    )
    if existing_outputs:
        expected_ids = [str(row['score_observation_id']) for row in outputs]
        actual_ids = [str(row['score_observation_id']) for row in existing_outputs]
        if actual_ids != expected_ids:
            raise RuntimeError(
                'Stage 7 immutable same-date output conflicts with current inputs.'
            )

    with conn:
        existing_contract = conn.execute(
            'SELECT * FROM stage7_model_contract WHERE source_id=?',
            (source_id,),
        ).fetchone()
        if existing_contract is None:
            conn.execute(
                '''INSERT INTO stage7_model_contract(
                       source_id,model_family,model_version,baseline_source_id,
                       contract_sha256,promotion_state,neutral_score,
                       minimum_data_quality_confidence,
                       maximum_missing_component_weight,
                       minimum_rank_ready_fraction,specialized_weight_policy,
                       factor_validation_campaign_id,factor_validation_verdict,
                       component_weights_json,contract_json,created_at,updated_at
                   ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                (
                    source_id, MODEL_FAMILY, model_version,
                    baseline_source_id, contract_sha,
                    payload['promotion_state'], payload['neutral_score'],
                    payload['minimum_data_quality_confidence'],
                    payload['maximum_missing_component_weight'],
                    payload['minimum_rank_ready_fraction'],
                    payload['specialized_weight_policy'],
                    payload['factor_validation_campaign_id'],
                    payload['factor_validation_verdict'],
                    _canonical_json(payload['component_weights']),
                    _canonical_json(payload), now, now,
                ),
            )
        elif (
            str(existing_contract['contract_sha256']) != contract_sha
            or str(existing_contract['contract_json']) != _canonical_json(payload)
        ):
            raise RuntimeError(
                'Stage 7 model contract is immutable; use a new source/model version.'
            )

        existing_weights = list(
            conn.execute(
                '''SELECT component_name,component_group,component_weight,
                          weight_status,evidence_reference,contract_sha256
                   FROM stage7_component_weight_contract
                   WHERE source_id=? AND model_family=? AND model_version=?
                   ORDER BY component_name''',
                (source_id, MODEL_FAMILY, model_version),
            )
        )
        expected_weights = [
            (row[4], row[5], row[6], row[7], row[8], row[9])
            for row in weight_rows
        ]
        if existing_weights:
            actual_weights = [tuple(row) for row in existing_weights]
            if actual_weights != expected_weights:
                raise RuntimeError('Stage 7 component weight contract drifted.')
        else:
            conn.executemany(
                '''INSERT INTO stage7_component_weight_contract(
                       source_id,model_family,model_version,
                       calibration_cohort_id,component_name,component_group,
                       component_weight,weight_status,evidence_reference,
                       contract_sha256,created_at,updated_at
                   ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)''',
                weight_rows,
            )

        if not existing_outputs:
            _insert_outputs(conn, outputs=outputs, now=now)

        existing_snapshot = conn.execute(
            '''SELECT * FROM stage7_score_snapshot
               WHERE source_id=? AND model_family=? AND asof_date=?''',
            (source_id, MODEL_FAMILY, as_of),
        ).fetchone()
        snapshot_identity = (
            model_version, contract_sha, baseline_manifest, output_manifest,
            len(outputs), rank_ready, len(outputs) - rank_ready,
            'shadow_complete',
        )
        if existing_snapshot is None:
            conn.execute(
                '''INSERT INTO stage7_score_snapshot(
                       source_id,model_family,model_version,asof_date,
                       contract_sha256,baseline_input_manifest_sha256,
                       output_manifest_sha256,ticker_count,rank_ready_count,
                       review_required_count,status,created_at
                   ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)''',
                (
                    source_id, MODEL_FAMILY, model_version, as_of,
                    contract_sha, baseline_manifest, output_manifest,
                    len(outputs), rank_ready, len(outputs) - rank_ready,
                    'shadow_complete', now,
                ),
            )
        else:
            actual_snapshot = (
                str(existing_snapshot['model_version']),
                str(existing_snapshot['contract_sha256']),
                str(existing_snapshot['baseline_input_manifest_sha256']),
                str(existing_snapshot['output_manifest_sha256']),
                int(existing_snapshot['ticker_count']),
                int(existing_snapshot['rank_ready_count']),
                int(existing_snapshot['review_required_count']),
                str(existing_snapshot['status']),
            )
            if actual_snapshot != snapshot_identity:
                raise RuntimeError(
                    'Stage 7 immutable same-date snapshot conflicts with '
                    'current inputs.'
                )

    return {
        'status': 'PASS',
        'asof_date': as_of,
        'source_id': source_id,
        'model_version': model_version,
        'contract_sha256': contract_sha,
        'baseline_input_manifest_sha256': baseline_manifest,
        'output_manifest_sha256': output_manifest,
        'ticker_count': len(outputs),
        'rank_ready_count': rank_ready,
        'review_required_count': len(outputs) - rank_ready,
        'specialized_nonzero_weight_count': 0,
        'promotion_state': payload['promotion_state'],
        'portfolio_candidate_gate': payload['portfolio_candidate_gate'],
        'oos_score_valid_flag': payload['oos_score_valid_flag'],
    }


def validate_stage7_scores(
    conn: sqlite3.Connection,
    bundle: ConfigBundle,
    *,
    as_of: str,
) -> dict[str, Any]:
    contract_sha = bootstrap_stage7(conn, bundle)
    checks: list[dict[str, Any]] = []
    source_id = str(cfg_get(bundle.payload, 'stage7_scoring.source_id'))
    baseline_source_id = str(
        cfg_get(bundle.payload, 'stage7_scoring.baseline_source_id')
    )
    model_version = str(cfg_get(bundle.payload, 'stage7_scoring.model_version'))
    prerequisite_errors = _stage6_prerequisite_errors(
        conn, bundle, as_of=as_of
    )
    _check(
        checks,
        'stage6_feature_contract_ready',
        not prerequisite_errors,
        failed_checks=prerequisite_errors,
    )
    inputs = _baseline_inputs(
        conn, as_of=as_of, baseline_source_id=baseline_source_id
    )
    components = _components(conn, as_of=as_of)
    input_error = ''
    try:
        _verify_atomic_inputs(
            conn,
            bundle,
            as_of=as_of,
            inputs=inputs,
            components=components,
        )
    except RuntimeError as exc:
        input_error = str(exc)
    _check(
        checks,
        'atomic_inputs_and_stage6b_overlay_exact',
        not input_error,
        error=input_error,
    )
    expected = (
        _expected_outputs(
            bundle,
            as_of=as_of,
            contract_sha=contract_sha,
            inputs=inputs,
            components=components,
        )
        if not input_error and inputs
        else []
    )
    actual = _actual_output_rows(conn, source_id=source_id, as_of=as_of)
    expected_by_ticker = {str(row['ticker']): row for row in expected}
    actual_by_ticker = {str(row['ticker']): row for row in actual}
    identity_errors: list[str] = []
    for ticker, row in actual_by_ticker.items():
        expected_row = expected_by_ticker.get(ticker)
        if (
            expected_row is None
            or str(row['score_observation_id'])
            != score_observation_id(row)
            or str(row['score_observation_id'])
            != str(expected_row['score_observation_id'])
        ):
            identity_errors.append(ticker)
    _check(
        checks,
        'score_rows_exact_and_deterministic',
        set(actual_by_ticker) == set(expected_by_ticker)
        and not identity_errors,
        expected_rows=len(expected_by_ticker),
        actual_rows=len(actual_by_ticker),
        identity_errors=identity_errors[:20],
    )

    ledger = conn.execute(
        '''SELECT migration_sha256 FROM stage7_schema_migrations
           WHERE migration_version=?''',
        (STAGE7_SCHEMA_VERSION,),
    ).fetchone()
    _check(
        checks,
        'stage7_migration_current',
        ledger is not None and str(ledger[0]) == STAGE7_MIGRATION_SHA256,
        version=STAGE7_SCHEMA_VERSION,
    )
    contract = conn.execute(
        'SELECT * FROM stage7_model_contract WHERE source_id=?',
        (source_id,),
    ).fetchone()
    payload = stage7_contract_payload(bundle)
    _check(
        checks,
        'model_contract_exact_and_shadow_only',
        contract is not None
        and str(contract['contract_sha256']) == contract_sha
        and str(contract['contract_json']) == _canonical_json(payload)
        and str(contract['promotion_state']) == 'shadow_monitor',
    )
    weight_rows = list(
        conn.execute(
            '''SELECT * FROM stage7_component_weight_contract
               WHERE source_id=? AND model_family=? AND model_version=?
               ORDER BY component_name''',
            (source_id, MODEL_FAMILY, model_version),
        )
    )
    core_weights = {
        str(row['component_name']): float(row['component_weight'])
        for row in weight_rows
        if str(row['component_group']) != 'specialized'
    }
    specialized_rows = [
        row for row in weight_rows
        if str(row['component_group']) == 'specialized'
    ]
    _check(
        checks,
        'weight_contract_exact_core_and_zero_specialized',
        core_weights == stage7_component_weights(bundle)
        and len(specialized_rows) == len(_specialized_component_names(bundle))
        and all(
            float(row['component_weight']) == 0.0
            and str(row['weight_status'])
            == 'locked_zero_corrected_campaign_no_directionally_accepted_evidence'
            for row in specialized_rows
        ),
        core_weight_sum=sum(core_weights.values()),
        specialized_count=len(specialized_rows),
    )
    gate_errors = [
        str(row['ticker']) for row in actual
        if str(row['promotion_state']) != 'shadow_monitor'
        or int(row['portfolio_candidate_gate']) != 0
        or int(row['oos_score_valid_flag']) != 0
    ]
    _check(
        checks,
        'all_outputs_shadow_and_noninvestable',
        not gate_errors,
        tickers=gate_errors[:20],
    )
    score_errors = [
        str(row['ticker']) for row in actual
        if _finite(row['final_score']) is None
        or not 0.0 <= float(row['final_score']) <= 100.0
        or _finite(row['data_quality_confidence']) is None
        or not 0.0 <= float(row['data_quality_confidence']) <= 1.0
        or _finite(row['full_data_quality_confidence']) is None
        or not 0.0 <= float(row['full_data_quality_confidence']) <= 1.0
    ]
    _check(
        checks,
        'scores_and_confidence_bounded',
        not score_errors,
        tickers=score_errors[:20],
    )
    arithmetic_errors = _independent_score_tieout_errors(
        actual,
        expected_weights=stage7_component_weights(bundle),
    )
    _check(
        checks,
        'weighted_score_arithmetic_exact',
        not arithmetic_errors,
        tickers=arithmetic_errors[:20],
    )
    rank_ready = [row for row in actual if int(row['rank_ready_flag']) == 1]
    rank_errors = _independent_rank_tieout_errors(actual)
    _check(
        checks,
        'rank_order_percentiles_and_tie_break_exact',
        not rank_errors,
        rank_ready_count=len(rank_ready),
        tickers=rank_errors[:20],
    )
    review_errors = [
        str(row['ticker']) for row in actual
        if int(row['rank_ready_flag']) == 0
        and not str(row['review_reason'] or '').strip()
    ]
    _check(
        checks,
        'every_unranked_ticker_has_review_reason',
        not review_errors,
        tickers=review_errors[:20],
    )
    minimum_fraction = float(
        cfg_get(bundle.payload, 'stage7_scoring.minimum_rank_ready_fraction')
    )
    rank_fraction = len(rank_ready) / len(actual) if actual else 0.0
    _check(
        checks,
        'rank_ready_coverage_sufficient',
        bool(actual) and rank_fraction >= minimum_fraction,
        rank_ready_count=len(rank_ready),
        ticker_count=len(actual),
        fraction=rank_fraction,
        minimum=minimum_fraction,
    )
    baseline_manifest = _manifest(
        str(row['input_observation_id']) for row in inputs
    )
    output_manifest = _manifest(
        str(row['score_observation_id']) for row in actual
    )
    snapshot = conn.execute(
        '''SELECT * FROM stage7_score_snapshot
           WHERE source_id=? AND model_family=? AND asof_date=?''',
        (source_id, MODEL_FAMILY, as_of),
    ).fetchone()
    _check(
        checks,
        'snapshot_manifests_exact',
        snapshot is not None
        and str(snapshot['contract_sha256']) == contract_sha
        and str(snapshot['baseline_input_manifest_sha256'])
        == baseline_manifest
        and str(snapshot['output_manifest_sha256']) == output_manifest
        and int(snapshot['ticker_count']) == len(actual)
        and int(snapshot['rank_ready_count']) == len(rank_ready)
        and int(snapshot['review_required_count'])
        == len(actual) - len(rank_ready)
        and str(snapshot['status']) == 'shadow_complete',
    )
    source_status = conn.execute(
        'SELECT status FROM source_registry WHERE source_id=?',
        (source_id,),
    ).fetchone()
    _check(
        checks,
        'stage7_source_active',
        source_status is not None and str(source_status[0]) == 'active',
    )
    foreign_key = conn.execute('PRAGMA foreign_key_check').fetchone()
    _check(
        checks,
        'foreign_keys_valid',
        foreign_key is None,
        first_violation=list(foreign_key) if foreign_key else None,
    )
    status = 'PASS' if all(bool(row['passed']) for row in checks) else 'FAIL'
    return {
        'status': status,
        'asof_date': as_of,
        'source_id': source_id,
        'model_version': model_version,
        'contract_sha256': contract_sha,
        'checks': checks,
        'summary': {
            'ticker_count': len(actual),
            'rank_ready_count': len(rank_ready),
            'review_required_count': len(actual) - len(rank_ready),
            'rank_ready_fraction': rank_fraction,
            'specialized_weight_count': len(specialized_rows),
            'specialized_nonzero_weight_count': sum(
                float(row['component_weight']) != 0.0
                for row in specialized_rows
            ),
            'passed_checks': sum(bool(row['passed']) for row in checks),
            'total_checks': len(checks),
        },
    }
