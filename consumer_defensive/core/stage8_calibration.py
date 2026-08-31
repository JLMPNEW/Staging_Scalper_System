from __future__ import annotations

import bisect
import csv
import hashlib
import json
import math
import random
import sqlite3
import statistics
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from factor_validation import load_campaign_registry

from consumer_defensive.adapters.factor_validation import (
    validate_consumer_defensive_factor_validation,
)

from .atomic_io import atomic_text_writer
from .calibration_scope import (
    apply_calibration_scope,
    calibration_scope_contract,
)
from .config import ConfigBundle, cfg_get, load_yaml, resolve_path
from .financial_pipeline import (
    build_financial_feature_bundle,
    select_canonical_financial_facts,
)
from .market_data import (
    MarketDataPolicy,
    _aligned_residual_return,
    _annualized_volatilities,
    load_market_policy,
)
from .scoring_features import CORE_COMPONENT_SPECS
from .stage4 import (
    CANONICAL_SOURCE,
    _concept_index,
    _frequency,
    _fx_rate,
    _pit_inline_fallback_required,
)
from .stage6c_panel import validate_stage6c_panel
from .stage7_scoring import (
    stage7_component_weights,
    stage7_contract_sha256,
)


STAGE8_VERSION = 'consumer_defensive_stage8_calibration_v1'
SECTOR_SCOPE = 'consumer_defensive'
RESEARCH_SAMPLE_ROLE = 'deep_replay_research'
PANEL_FILE = 'stage8_historical_core_panel.csv'
CONTRACT_FILE = 'stage8_contract.json'
CANDIDATE_FILE = 'stage8_candidate_registry.json'
SPLIT_FILE = 'stage8_split_manifest.json'
RESULT_FILE = 'stage8_candidate_results.csv'
WALK_FILE = 'stage8_walk_forward_results.csv'
DECISION_FILE = 'stage8_decision.json'
MANIFEST_FILE = 'stage8_artifact_manifest.json'
HORIZONS = (21, 63, 126)

PANEL_FIELDS = (
    'asof_date', 'ticker', 'cohort_id', 'applicability_subtype',
    'sample_role', 'membership_eligible_flag', 'investable_flag',
    'label_status', 'market_regime', 'terminal_event_status',
    'baseline_rank_ready_flag', 'calibration_eligible_flag',
    'review_reason', 'core_score', 'available_weight', 'missing_weight',
    'component_raw_values_json', 'component_scores_json',
    'component_quality_json', 'component_source_hashes_json',
    'specialized_scores_json', 'specialized_applicability_json',
    'forward_xlp_residual_return_21d',
    'forward_xlp_residual_return_63d',
    'forward_xlp_residual_return_126d',
    'row_sha256',
)


@dataclass(frozen=True)
class Candidate:
    candidate_id: str
    scope_id: str
    candidate_kind: str
    core_weights: dict[str, float]
    specialized_weights: dict[str, float]
    parent_candidate_id: str | None
    shrinkage_alpha: float
    evidence_references: tuple[str, ...]
    preregistration_sha256: str


@dataclass(frozen=True)
class ChronologicalSplit:
    train_dates: tuple[str, ...]
    first_embargo_dates: tuple[str, ...]
    validation_dates: tuple[str, ...]
    second_embargo_dates: tuple[str, ...]
    holdout_dates: tuple[str, ...]
    embargo_panel_dates: int


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


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def _finite(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _round_weights(
    weights: Mapping[str, float],
    *,
    target_total: float = 1.0,
) -> dict[str, float]:
    names = sorted(weights)
    rounded = {name: round(float(weights[name]), 12) for name in names}
    difference = round(float(target_total) - sum(rounded.values()), 12)
    if names and difference:
        largest = max(names, key=lambda name: rounded[name])
        rounded[largest] = round(rounded[largest] + difference, 12)
    return rounded


def _candidate_payload(
    *,
    scope_id: str,
    candidate_kind: str,
    core_weights: Mapping[str, float],
    specialized_weights: Mapping[str, float],
    parent_candidate_id: str | None,
    shrinkage_alpha: float,
    evidence_references: Iterable[str],
) -> dict[str, Any]:
    specialized = {
        name: round(float(value), 12)
        for name, value in sorted(specialized_weights.items())
    }
    core_total = 1.0 - sum(specialized.values())
    return {
        'schema_version': 'consumer_defensive_stage8_candidate_v1',
        'scope_id': scope_id,
        'candidate_kind': candidate_kind,
        'core_weights': _round_weights(
            core_weights, target_total=core_total
        ),
        'specialized_weights': specialized,
        'parent_candidate_id': parent_candidate_id,
        'shrinkage_alpha': round(float(shrinkage_alpha), 12),
        'evidence_references': sorted(str(value) for value in evidence_references),
    }


def _make_candidate(**kwargs: Any) -> Candidate:
    payload = _candidate_payload(**kwargs)
    digest = _sha256(payload)
    return Candidate(
        candidate_id=f's8_{digest[:20]}',
        preregistration_sha256=digest,
        **{
            key: (
                tuple(payload[key])
                if key == 'evidence_references'
                else payload[key]
            )
            for key in (
                'scope_id', 'candidate_kind', 'core_weights',
                'specialized_weights', 'parent_candidate_id',
                'shrinkage_alpha', 'evidence_references',
            )
        },
    )


def stage8_contract_payload(
    bundle: ConfigBundle,
    *,
    stage6c_run: Mapping[str, Any],
    factor_campaign_id: str,
    factor_registry_sha256: str,
) -> dict[str, Any]:
    settings = dict(cfg_get(bundle.payload, 'stage8_calibration'))
    methodology_files = [
        Path(__file__).resolve(),
        (bundle.base_dir / 'core' / 'scoring_features.py').resolve(),
        (bundle.base_dir / 'core' / 'stage7_scoring.py').resolve(),
        (bundle.base_dir / 'core' / 'financial_pipeline.py').resolve(),
        (bundle.base_dir / 'core' / 'market_data.py').resolve(),
        (bundle.base_dir / 'core' / 'calibration_scope.py').resolve(),
        bundle.path.resolve(),
    ]
    methodology_hashes = {
        path.name: _file_sha256(path) for path in methodology_files
    }
    return {
        'schema_version': STAGE8_VERSION,
        'model_family': 'consumer_defensive',
        'mode': 'report_only',
        'stage6c_run_id': int(stage6c_run['stage6c_run_id']),
        'stage6c_panel_sha256': str(stage6c_run['panel_sha256']),
        'stage6c_asof_date': str(stage6c_run['asof_date']),
        'stage7_source_id': str(
            cfg_get(bundle.payload, 'stage7_scoring.source_id')
        ),
        'stage7_contract_sha256': stage7_contract_sha256(bundle),
        'factor_validation_campaign_id': factor_campaign_id,
        'factor_validation_registry_sha256': factor_registry_sha256,
        'candidate_policy': settings,
        'calibration_scope': calibration_scope_contract(bundle),
        'methodology_file_sha256s': methodology_hashes,
        'methodology_sha256': _sha256(methodology_hashes),
        'normalization_policy': (
            'point_in_time_cohort_then_universe_fallback'
        ),
        'missing_value_policy': (
            'neutral_score_contribution_no_weight_redistribution'
        ),
        'nonapplicable_policy': (
            'candidate_and_baseline_compared_on_same_applicable_sample'
        ),
        'market_source_policy': (
            'frozen_whole_ticker_stage3_selection_no_date_splicing'
        ),
        'sample_role': RESEARCH_SAMPLE_ROLE,
        'production_promotion_enabled': False,
        'portfolio_write_enabled': False,
        'stage7_weight_mutation_enabled': False,
    }


def chronological_split(
    dates: Sequence[str],
    *,
    minimum_train_dates: int,
    validation_dates: int,
    holdout_dates: int,
    embargo_panel_dates: int,
    maximum_horizon: int = 126,
    evaluation_step_trading_days: int = 21,
) -> ChronologicalSplit:
    ordered = tuple(sorted(set(str(value) for value in dates)))
    if tuple(str(value) for value in dates) != ordered:
        raise ValueError('Stage 8 panel dates must be unique and chronological.')
    minimum_embargo = math.ceil(
        maximum_horizon / evaluation_step_trading_days
    ) + 1
    if embargo_panel_dates < minimum_embargo:
        raise ValueError(
            'Stage 8 embargo is shorter than the maximum forward-label '
            f'isolation requirement: {embargo_panel_dates} < {minimum_embargo}.'
        )
    required = (
        minimum_train_dates + validation_dates + holdout_dates
        + 2 * embargo_panel_dates
    )
    if len(ordered) < required:
        raise ValueError(
            'Insufficient Stage 8 panel dates for train/embargo/validation/'
            f'holdout isolation: observed={len(ordered)} required={required}.'
        )
    holdout_start = len(ordered) - holdout_dates
    second_embargo_start = holdout_start - embargo_panel_dates
    validation_start = second_embargo_start - validation_dates
    first_embargo_start = validation_start - embargo_panel_dates
    if first_embargo_start < minimum_train_dates:
        raise ValueError('Stage 8 training block is shorter than configured.')
    return ChronologicalSplit(
        train_dates=ordered[:first_embargo_start],
        first_embargo_dates=ordered[first_embargo_start:validation_start],
        validation_dates=ordered[validation_start:second_embargo_start],
        second_embargo_dates=ordered[second_embargo_start:holdout_start],
        holdout_dates=ordered[holdout_start:],
        embargo_panel_dates=embargo_panel_dates,
    )


def _core_weight_vectors(bundle: ConfigBundle) -> list[dict[str, float]]:
    baseline = stage7_component_weights(bundle)
    settings = cfg_get(bundle.payload, 'stage8_calibration')
    count = int(settings['candidate_count_per_scope'])
    cap = float(settings['component_weight_cap'])
    l1_cap = float(settings['weight_l1_turnover_cap'])
    scale = float(settings['candidate_perturbation_scale'])
    minimum_breadth = int(settings['minimum_factor_breadth'])
    maximum_breadth = int(settings['maximum_factor_breadth'])
    rng = random.Random(int(settings['candidate_seed']))
    vectors = [_round_weights(baseline)]
    seen = {_canonical_json(vectors[0])}
    attempts = 0
    names = sorted(baseline)
    while len(vectors) < count and attempts < count * 500:
        attempts += 1
        raw = {
            name: baseline[name] * math.exp(scale * (2.0 * rng.random() - 1.0))
            for name in names
        }
        total = sum(raw.values())
        proposal = _round_weights({
            name: value / total for name, value in raw.items()
        })
        breadth = sum(value > 1e-12 for value in proposal.values())
        l1 = sum(abs(proposal[name] - baseline[name]) for name in names)
        if (
            max(proposal.values()) > cap + 1e-12
            or l1 > l1_cap + 1e-12
            or not minimum_breadth <= breadth <= maximum_breadth
        ):
            continue
        identity = _canonical_json(proposal)
        if identity in seen:
            continue
        seen.add(identity)
        vectors.append(proposal)
    if len(vectors) != count:
        raise RuntimeError(
            'Stage 8 could not generate the configured deterministic '
            f'candidate census: generated={len(vectors)} expected={count}.'
        )
    return vectors


def _hierarchical_alpha(
    *,
    median_cross_section: float,
    shrinkage_strength: float,
    maximum_fraction: float,
) -> float:
    raw = median_cross_section / (median_cross_section + shrinkage_strength)
    return min(maximum_fraction, max(0.0, raw))


def build_candidate_registry(
    bundle: ConfigBundle,
    *,
    membership_rows: Sequence[Mapping[str, Any]],
    accepted_factor_cells: Sequence[Mapping[str, Any]],
) -> list[Candidate]:
    baseline = stage7_component_weights(bundle)
    vectors = _core_weight_vectors(bundle)
    by_cohort: dict[str, dict[str, set[str]]] = defaultdict(
        lambda: defaultdict(set)
    )
    for row in membership_rows:
        by_cohort[str(row['cohort_id'])][str(row['asof_date'])].add(
            str(row['ticker'])
        )
    settings = cfg_get(bundle.payload, 'stage8_calibration')
    candidates: list[Candidate] = []

    sector_baseline = _make_candidate(
        scope_id=SECTOR_SCOPE,
        candidate_kind='stage7_core_baseline',
        core_weights=baseline,
        specialized_weights={},
        parent_candidate_id=None,
        shrinkage_alpha=0.0,
        evidence_references=(stage7_contract_sha256(bundle),),
    )
    candidates.append(sector_baseline)
    sector_parents: list[Candidate] = []
    for weights in vectors[1:]:
        candidate = _make_candidate(
            scope_id=SECTOR_SCOPE,
            candidate_kind='sector_core_reweight',
            core_weights=weights,
            specialized_weights={},
            parent_candidate_id=sector_baseline.candidate_id,
            shrinkage_alpha=1.0,
            evidence_references=(stage7_contract_sha256(bundle),),
        )
        candidates.append(candidate)
        sector_parents.append(candidate)

    for cohort_id in sorted(by_cohort):
        counts = [len(value) for value in by_cohort[cohort_id].values()]
        alpha = _hierarchical_alpha(
            median_cross_section=float(statistics.median(counts)),
            shrinkage_strength=float(settings['cohort_shrinkage_strength']),
            maximum_fraction=float(
                settings['maximum_cohort_deviation_fraction']
            ),
        )
        baseline_candidate = _make_candidate(
            scope_id=cohort_id,
            candidate_kind='stage7_core_baseline',
            core_weights=baseline,
            specialized_weights={},
            parent_candidate_id=sector_baseline.candidate_id,
            shrinkage_alpha=0.0,
            evidence_references=(stage7_contract_sha256(bundle),),
        )
        candidates.append(baseline_candidate)
        for parent in sector_parents:
            weights = _round_weights({
                name: (
                    baseline[name]
                    + alpha * (parent.core_weights[name] - baseline[name])
                )
                for name in baseline
            })
            candidates.append(_make_candidate(
                scope_id=cohort_id,
                candidate_kind='cohort_core_reweight_shrunk',
                core_weights=weights,
                specialized_weights={},
                parent_candidate_id=parent.candidate_id,
                shrinkage_alpha=alpha,
                evidence_references=(stage7_contract_sha256(bundle),),
            ))

    accepted_by_scope: dict[str, set[str]] = defaultdict(set)
    evidence_by_scope: dict[str, list[str]] = defaultdict(list)
    for cell in accepted_factor_cells:
        scope_id = str(cell['scope_id'])
        if scope_id == SECTOR_SCOPE or scope_id in by_cohort:
            accepted_by_scope[scope_id].add(str(cell['factor_id']))
            evidence_by_scope[scope_id].append(str(cell['cell_id']))
    specialized_total = float(settings['maximum_specialized_weight'])
    if specialized_total > 0.0:
        for scope_id, factors in sorted(accepted_by_scope.items()):
            if not factors:
                continue
            core = {
                name: value * (1.0 - specialized_total)
                for name, value in baseline.items()
            }
            each = specialized_total / len(factors)
            candidates.append(_make_candidate(
                scope_id=scope_id,
                candidate_kind='core_plus_specialized',
                core_weights=core,
                specialized_weights={factor: each for factor in sorted(factors)},
                parent_candidate_id=(
                    sector_baseline.candidate_id
                    if scope_id == SECTOR_SCOPE
                    else next(
                        candidate.candidate_id
                        for candidate in candidates
                        if candidate.scope_id == scope_id
                        and candidate.candidate_kind == 'stage7_core_baseline'
                    )
                ),
                shrinkage_alpha=0.0,
                evidence_references=tuple(sorted(evidence_by_scope[scope_id])),
            ))

    identities = {candidate.candidate_id for candidate in candidates}
    if len(identities) != len(candidates):
        raise RuntimeError('Stage 8 candidate registry contains duplicate IDs.')
    return sorted(candidates, key=lambda row: (row.scope_id, row.candidate_id))


def verify_stage7_baseline(
    conn: sqlite3.Connection,
    bundle: ConfigBundle,
) -> dict[str, Any]:
    source_id = str(cfg_get(bundle.payload, 'stage7_scoring.source_id'))
    expected_contract = stage7_contract_sha256(bundle)
    contract = conn.execute(
        '''SELECT * FROM stage7_model_contract WHERE source_id=?''',
        (source_id,),
    ).fetchone()
    if contract is None:
        raise RuntimeError('Stage 8 cannot find the frozen Stage 7 contract.')
    if str(contract['contract_sha256']) != expected_contract:
        raise RuntimeError('Stage 7 contract hash differs from configuration.')
    if (
        str(contract['promotion_state']) != 'shadow_monitor'
        or str(contract['factor_validation_campaign_id'])
        != str(cfg_get(
            bundle.payload, 'stage7_scoring.factor_validation_campaign_id'
        ))
    ):
        raise RuntimeError('Stage 7 governance state is not the frozen baseline.')
    weights = list(conn.execute(
        '''SELECT component_name,component_group,component_weight,weight_status
           FROM stage7_component_weight_contract
           WHERE source_id=? ORDER BY component_name''',
        (source_id,),
    ))
    if not weights:
        raise RuntimeError('Stage 7 weight contract is empty.')
    specialized_nonzero = [
        str(row['component_name'])
        for row in weights
        if str(row['component_group']) == 'specialized'
        and abs(float(row['component_weight'])) > 1e-12
    ]
    if specialized_nonzero:
        raise RuntimeError(
            'Stage 8 requires the corrected zero-specialized Stage 7 '
            f'baseline; nonzero={specialized_nonzero}.'
        )
    snapshot = conn.execute(
        '''SELECT * FROM stage7_score_snapshot
           WHERE source_id=? ORDER BY asof_date DESC LIMIT 1''',
        (source_id,),
    ).fetchone()
    if snapshot is None or str(snapshot['status']) != 'shadow_complete':
        raise RuntimeError('Stage 7 frozen score snapshot is incomplete.')
    return {
        'source_id': source_id,
        'contract_sha256': expected_contract,
        'asof_date': str(snapshot['asof_date']),
        'output_manifest_sha256': str(snapshot['output_manifest_sha256']),
        'ticker_count': int(snapshot['ticker_count']),
        'rank_ready_count': int(snapshot['rank_ready_count']),
        'specialized_nonzero_count': 0,
    }


def verify_factor_campaign(
    factor_root: Path,
    *,
    campaign_id: str,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    root = factor_root.expanduser().resolve()
    validation = validate_consumer_defensive_factor_validation(
        root, campaign_id=campaign_id
    )
    if validation['status'] != 'PASS':
        raise RuntimeError(
            'Stage 8 factor campaign failed independent verification: '
            f'{validation["errors"]}'
        )
    registry = load_campaign_registry(
        root / campaign_id / 'campaign_registry.json'
    )
    accepted: list[dict[str, Any]] = []
    for cell in registry.cells:
        state = validation['states'].get(cell.cell_id)
        if state == 'accepted':
            accepted.append({
                'cell_id': cell.cell_id,
                'factor_id': cell.factor_id,
                'scope_id': cell.sector_id,
                'target_name': cell.target_name,
                'horizon_trading_days': cell.horizon_trading_days,
                'factor_direction': cell.factor_direction,
                'state': state,
            })
    return {
        'campaign_id': campaign_id,
        'registry_sha256': registry.registration_sha256,
        'cell_count': len(registry.cells),
        'accepted_cell_count': len(accepted),
        'ledger_entry_count': int(validation['ledger_entry_count']),
        'state_counts': dict(sorted(Counter(
            validation['states'].values()
        ).items())),
    }, accepted


def _stage6c_run(
    conn: sqlite3.Connection,
    *,
    stage6c_run_id: int,
) -> dict[str, Any]:
    validation = validate_stage6c_panel(
        conn, stage6c_run_id=stage6c_run_id
    )
    if validation['status'] != 'PASS':
        raise RuntimeError(
            'Stage 8 requires a valid Stage 6C panel: '
            f'{validation["errors"]}'
        )
    row = conn.execute(
        'SELECT * FROM stage6c_panel_run WHERE stage6c_run_id=?',
        (stage6c_run_id,),
    ).fetchone()
    if row is None or str(row['status']) != 'complete':
        raise RuntimeError('Stage 6C run is missing or incomplete.')
    return dict(row)


def _membership_rows(
    conn: sqlite3.Connection,
    *,
    stage6c_run_id: int,
) -> list[dict[str, Any]]:
    rows = [
        dict(row)
        for row in conn.execute(
            '''SELECT DISTINCT asof_date,ticker,cohort_id,
                              applicability_subtype
               FROM stage6c_specialized_factor_panel
               WHERE stage6c_run_id=?
               ORDER BY asof_date,ticker''',
            (stage6c_run_id,),
        )
    ]
    if not rows:
        raise RuntimeError('Stage 8 membership census is empty.')
    identities = {
        (row['asof_date'], row['ticker']) for row in rows
    }
    if len(identities) != len(rows):
        raise RuntimeError(
            'Stage 6C contains conflicting cohort/subtype membership rows.'
        )
    return rows


def _label_rows(
    conn: sqlite3.Connection,
    *,
    stage6c_run_id: int,
) -> list[dict[str, Any]]:
    fields = (
        'asof_date,ticker,cohort_id,applicability_subtype,'
        'membership_eligible_flag,investable_flag,sample_role,market_regime,'
        'terminal_event_status,'
        'CASE WHEN forward_xlp_residual_return_21d IS NOT NULL '
        'AND forward_xlp_residual_return_63d IS NOT NULL '
        'AND forward_xlp_residual_return_126d IS NOT NULL '
        "THEN 'complete' ELSE 'partial_or_missing' END AS label_status,"
        'forward_xlp_residual_return_21d,'
        'forward_xlp_residual_return_63d,'
        'forward_xlp_residual_return_126d'
    )
    rows = [
        dict(row)
        for row in conn.execute(
            f'''SELECT DISTINCT {fields}
                FROM stage6c_specialized_factor_panel
                WHERE stage6c_run_id=?
                ORDER BY asof_date,ticker''',
            (stage6c_run_id,),
        )
    ]
    identities = {(row['asof_date'], row['ticker']) for row in rows}
    if len(identities) != len(rows):
        raise RuntimeError(
            'Stage 6C forward labels are inconsistent across factor rows.'
        )
    return rows


def _specialized_rows(
    conn: sqlite3.Connection,
    *,
    stage6c_run_id: int,
    factor_ids: set[str],
) -> list[dict[str, Any]]:
    if not factor_ids:
        return []
    placeholders = ','.join('?' for _ in factor_ids)
    return [
        dict(row)
        for row in conn.execute(
            f'''SELECT asof_date,ticker,cohort_id,factor_id,factor_value,
                       availability_status
                FROM stage6c_specialized_factor_panel
                WHERE stage6c_run_id=? AND factor_id IN ({placeholders})
                ORDER BY asof_date,ticker,factor_id''',
            (stage6c_run_id, *sorted(factor_ids)),
        )
    ]


def _price_selection_and_history(
    conn: sqlite3.Connection,
    *,
    tickers: set[str],
    maximum_date: str,
) -> tuple[
    dict[str, str],
    dict[str, list[tuple[str, float, float, float]]],
    str,
]:
    selection: dict[str, str] = {}
    selection_rows: list[dict[str, Any]] = []
    for row in conn.execute(
        '''SELECT ticker,selected_source_id,selection_asof_date,
                  adjustment_basis,selection_reason,coverage_status
           FROM dim_price_series_selection
           WHERE purpose='scoring_return_series'
           ORDER BY ticker'''
    ):
        ticker = str(row['ticker'])
        selection[ticker] = str(row['selected_source_id'])
        selection_rows.append(dict(row))
    required = set(tickers) | {'SPY', 'XLP'}
    missing = sorted(required - set(selection))
    if missing:
        raise RuntimeError(
            f'Stage 8 frozen whole-ticker selections are missing: {missing}.'
        )
    history: dict[str, list[tuple[str, float, float, float]]] = {}
    for ticker in sorted(required):
        rows = conn.execute(
            '''SELECT bar_date,adjusted_close,close,volume
               FROM fact_price_ohlcv
               WHERE ticker=? AND source_id=? AND bar_date<=?
                 AND adjusted_close>0
               ORDER BY bar_date''',
            (ticker, selection[ticker], maximum_date),
        ).fetchall()
        history[ticker] = [
            (
                str(row['bar_date']),
                float(row['adjusted_close']),
                float(row['close'] or row['adjusted_close']),
                float(row['volume'] or 0.0),
            )
            for row in rows
        ]
    return selection, history, _sha256(selection_rows)


def _market_features_for_date(
    *,
    as_of: str,
    tickers: Iterable[str],
    selection: Mapping[str, str],
    history: Mapping[str, list[tuple[str, float, float, float]]],
    policy: MarketDataPolicy,
) -> dict[str, dict[str, Any]]:
    settings = policy.payload['features']
    benchmark = str(settings['benchmark'])
    benchmark_rows = history[benchmark]
    benchmark_stop = bisect.bisect_right(
        [row[0] for row in benchmark_rows], as_of
    )
    benchmark_slice = benchmark_rows[:benchmark_stop]
    benchmark_by_date = {row[0]: row[1] for row in benchmark_slice}
    if not benchmark_by_date:
        raise RuntimeError(f'Stage 8 benchmark history is empty at {as_of}.')
    adv_days = int(settings['adv_days'])
    short_days = int(settings['momentum_short_days'])
    long_days = int(settings['momentum_long_days'])
    vol_days = int(settings['volatility_days'])
    drawdown_days = int(settings['drawdown_days'])
    full_rows = int(policy.payload['selection']['minimum_rows_full'])
    partial_rows = int(policy.payload['selection']['minimum_rows_partial'])
    output: dict[str, dict[str, Any]] = {}
    for ticker in sorted(set(tickers)):
        ticker_rows = history.get(ticker, [])
        stop = bisect.bisect_right([row[0] for row in ticker_rows], as_of)
        rows = ticker_rows[:stop]
        if not rows:
            continue
        adjusted = [row[1] for row in rows]
        dollar_volume = [row[2] * row[3] for row in rows]
        adv = (
            statistics.fmean(dollar_volume[-adv_days:])
            if len(dollar_volume) >= adv_days
            else None
        )
        realized, downside = _annualized_volatilities(adjusted, vol_days)
        peak = 0.0
        max_drawdown = 0.0
        for value in adjusted[-drawdown_days:]:
            peak = max(peak, value)
            if peak > 0.0:
                max_drawdown = min(max_drawdown, value / peak - 1.0)
        quality = (
            'full' if len(rows) >= full_rows
            else 'partial_history' if len(rows) >= partial_rows
            else 'insufficient_history'
        )
        output[ticker] = {
            'residual_momentum_63d': _aligned_residual_return(
                rows, benchmark_by_date, short_days
            ),
            'residual_momentum_126d': _aligned_residual_return(
                rows, benchmark_by_date, long_days
            ),
            'realized_volatility_63d': realized,
            'downside_volatility_63d': downside,
            'max_drawdown_252d': max_drawdown,
            'avg_dollar_volume_63d': adv,
            'quality_status': quality,
            'source_hash': _sha256({
                'source_id': selection[ticker],
                'asof_date': as_of,
                'last_bar_date': rows[-1][0],
                'history_days': len(rows),
            }),
        }
    return output


def _financial_features_for_date(
    conn: sqlite3.Connection,
    bundle: ConfigBundle,
    *,
    as_of: str,
    tickers: set[str],
) -> dict[str, dict[str, Any]]:
    settings = cfg_get(bundle.payload, 'financial_features')
    concept_map = load_yaml(resolve_path(
        settings['concept_map'], base_dir=bundle.base_dir
    ))
    definition_version = str(concept_map['definition_version'])
    concept_index = _concept_index(concept_map)
    concepts = sorted(concept_index)
    concept_placeholders = ','.join('?' for _ in concepts)
    ticker_placeholders = ','.join('?' for _ in tickers)
    cutoff = f'{as_of}T23:59:59Z'
    raw = conn.execute(
        f'''SELECT r.raw_fact_id,r.source_observation_id,r.ticker,
                   r.accession_number,r.taxonomy,r.concept,
                   r.numeric_value,r.unit,r.period_start,r.period_end,
                   r.accepted_at
            FROM fact_sec_xbrl_fact_raw r
            JOIN bridge_sec_filing_company b
              ON b.accession_number=r.accession_number
             AND b.issuer_ticker=r.ticker
            WHERE r.numeric_value IS NOT NULL AND r.accepted_at<=?
              AND r.concept IN ({concept_placeholders})
              AND r.ticker IN ({ticker_placeholders})
              AND COALESCE((SELECT e.event_type
                  FROM sec_filing_company_association_event e
                  WHERE e.accession_number=b.accession_number
                    AND e.issuer_company_id=b.issuer_company_id
                    AND e.effective_asof<=?
                  ORDER BY e.effective_asof DESC,e.event_id DESC LIMIT 1),
                  CASE WHEN b.association_status='active'
                       THEN 'observed' ELSE 'retired' END)
                  IN ('observed','reactivated')''',
        (
            cutoff, *concepts, *sorted(tickers), cutoff,
        ),
    ).fetchall()
    supported = {
        'USD',
        *(str(value).upper() for value in cfg_get(
            bundle.payload, 'fx_rates.supported_currencies', []
        )),
    }
    selected = select_canonical_financial_facts(
        raw,
        concept_index=concept_index,
        supported_currencies=supported,
    )
    by_ticker: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for decision in selected.decisions:
        flow = decision.statement_type != 'balance_sheet'
        rate = _fx_rate(
            conn,
            decision.reported_currency,
            decision.period_start,
            decision.period_end,
            flow,
        )
        by_ticker[decision.ticker].append({
            'canonical_metric': decision.metric,
            'canonical_component': decision.component,
            'accession_number': decision.accession_number,
            'taxonomy': decision.taxonomy,
            'source_concept': decision.source_concept,
            'period_start': decision.period_start,
            'period_end': decision.period_end,
            'accepted_at': decision.accepted_at,
            'frequency': _frequency(
                decision.period_start, decision.period_end, flow
            ),
            'value_usd': (
                decision.normalized_value * rate
                if rate is not None else None
            ),
            'reported_currency': decision.reported_currency,
            'source_raw_fact_id': decision.raw_fact_id,
            'quality_flags_json': json.dumps(
                decision.quality_flags, sort_keys=True
            ),
            'source_observation_id': decision.source_observation_id,
        })
    securities = {
        str(row['ticker']): (
            str(row['listing_start_date'] or '') or None,
            str(row['listing_end_date'] or '') or None,
        )
        for row in conn.execute(
            '''SELECT t.ticker,s.listing_start_date,s.listing_end_date
               FROM dim_consumer_defensive_taxonomy t
               JOIN dim_security s ON s.security_id=t.security_id
               WHERE t.model_family='consumer_defensive' '''
        )
    }
    output: dict[str, dict[str, Any]] = {}
    for ticker in sorted(tickers):
        listing_start, listing_end = securities[ticker]
        fallback = _pit_inline_fallback_required(
            conn,
            ticker=ticker,
            cutoff=cutoff,
            lag_days=int(cfg_get(
                bundle.payload,
                'sec_fundamentals.companyfacts_lag_days',
                120,
            )),
        )
        bundle_row = build_financial_feature_bundle(
            by_ticker.get(ticker, []),
            as_of=as_of,
            listing_start_date=listing_start,
            listing_end_date=listing_end,
            maximum_period_age_days=int(settings['maximum_period_age_days']),
            inline_xbrl_fallback_required=bool(fallback),
        )
        output[ticker] = {
            **bundle_row.values,
            'financial_quality_status': bundle_row.quality_status,
            'source_hash': _sha256({
                'source_id': CANONICAL_SOURCE,
                'definition_version': definition_version,
                'basis_period_end': bundle_row.basis_period_end,
                'lineage': bundle_row.lineage,
            }),
        }
    return output


def _latest_row(
    conn: sqlite3.Connection,
    *,
    table: str,
    ticker: str,
    date_column: str,
    cutoff: str,
    source_id: str,
) -> sqlite3.Row | None:
    return conn.execute(
        f'''SELECT * FROM {table}
            WHERE ticker=? AND source_id=? AND {date_column}<=?
            ORDER BY {date_column} DESC,rowid DESC LIMIT 1''',
        (ticker, source_id, cutoff),
    ).fetchone()


def _positioning_features_for_date(
    conn: sqlite3.Connection,
    bundle: ConfigBundle,
    *,
    as_of: str,
    tickers: set[str],
) -> dict[str, dict[str, Any]]:
    ownership_source = str(
        cfg_get(bundle.payload, 'positioning.ownership_source_id')
    )
    market_source = str(
        cfg_get(bundle.payload, 'positioning.market_positioning_source_id')
    )
    lookback = int(cfg_get(
        bundle.payload, 'positioning.lookback_days.insider', 90
    ))
    births = {
        'form4': str(cfg_get(
            bundle.payload, 'positioning.source_birthdates.sec_form4'
        )),
        '13f': str(cfg_get(
            bundle.payload, 'positioning.source_birthdates.institutional_13f'
        )),
        'short': str(cfg_get(
            bundle.payload, 'positioning.source_birthdates.short_interest'
        )),
        'borrow': str(cfg_get(
            bundle.payload, 'positioning.source_birthdates.borrow'
        )),
    }
    cutoff = f'{as_of}T23:59:59Z'
    window_start = (date.fromisoformat(as_of) - timedelta(
        days=lookback
    )).isoformat()
    output: dict[str, dict[str, Any]] = {}
    for ticker in sorted(tickers):
        ownership = conn.execute(
            '''SELECT SUM(
                       CASE UPPER(COALESCE(acquired_disposed,''))
                         WHEN 'A' THEN 1.0 WHEN 'D' THEN -1.0 ELSE 0.0 END
                       * shares * price
                   ) AS net_value,COUNT(*) AS event_count,
                   MAX(accepted_at) AS latest_accepted
               FROM fact_sec_ownership_transaction
               WHERE ticker=? AND source_id=? AND is_current_truth=1
                 AND accepted_at<=?
                 AND COALESCE(transaction_date,availability_date)>=?
                 AND shares IS NOT NULL AND price IS NOT NULL''',
            (ticker, ownership_source, cutoff, window_start),
        ).fetchone()
        institutional = _latest_row(
            conn, table='fact_13f_positioning', ticker=ticker,
            date_column='publication_date', cutoff=as_of,
            source_id=market_source,
        )
        short = _latest_row(
            conn, table='fact_short_interest', ticker=ticker,
            date_column='publication_date', cutoff=as_of,
            source_id=market_source,
        )
        borrow = _latest_row(
            conn, table='fact_borrow_snapshot', ticker=ticker,
            date_column='asof_date', cutoff=as_of, source_id=market_source,
        )
        insider = _finite(ownership['net_value']) if ownership else None
        institutional_flow = (
            _finite(institutional['institutional_ownership_delta_pct'])
            if institutional else None
        )
        short_pct = _finite(short['short_float_pct']) if short else None
        short_days = _finite(short['days_to_cover']) if short else None
        borrow_fee = _finite(borrow['borrow_fee']) if borrow else None
        short_signal = short_pct if short_pct is not None else short_days
        present = sum(
            value is not None for value in (institutional_flow, short_signal)
        )
        available_sources = [
            key for key in ('13f', 'short') if births[key] <= as_of
        ]
        if not available_sources:
            quality = 'unavailable'
        elif present == 0:
            quality = 'missing'
        elif present < len(available_sources):
            quality = 'partial'
        else:
            quality = 'complete'
        lineage = {
            'asof_date': as_of,
            'ownership_latest_accepted': (
                str(ownership['latest_accepted'] or '') if ownership else ''
            ),
            'ownership_event_count': (
                int(ownership['event_count'] or 0) if ownership else 0
            ),
            'institutional_observation_id': (
                str(institutional['source_observation_id'])
                if institutional else ''
            ),
            'short_observation_id': (
                str(short['source_observation_id']) if short else ''
            ),
            'borrow_observation_id': (
                str(borrow['source_observation_id']) if borrow else ''
            ),
            'source_birthdates': births,
        }
        output[ticker] = {
            'insider_net_buying': insider,
            'institutional_flow': institutional_flow,
            'short_float_pct': short_pct,
            'short_days_to_cover': short_days,
            'borrow_fee': borrow_fee,
            'quality_status': quality,
            'source_hash': _sha256(lineage),
        }
    return output


def _percentile(
    value: float,
    peers: Sequence[float],
    *,
    direction: str,
) -> float:
    lower = sum(peer < value for peer in peers)
    equal = sum(peer == value for peer in peers)
    percentile = 100.0 * (
        lower + (equal - 1) / 2.0
    ) / (len(peers) - 1)
    return 100.0 - percentile if direction == 'lower' else percentile


def _normalize_component_rows(
    rows: list[dict[str, Any]],
    *,
    minimum_peers: int,
) -> None:
    for component_name in sorted({
        str(row['component_name']) for row in rows
    }):
        named = [
            row for row in rows
            if row['component_name'] == component_name
            and row['availability_status'] == 'available'
            and row['raw_value'] is not None
        ]
        global_peers = [float(row['raw_value']) for row in named]
        for row in named:
            cohort_peers = [
                float(peer['raw_value'])
                for peer in named
                if peer['cohort_id'] == row['cohort_id']
            ]
            peers = cohort_peers
            if len(peers) < minimum_peers or len(set(peers)) < 2:
                peers = global_peers
            if len(peers) < minimum_peers or len(set(peers)) < 2:
                row['component_score'] = None
                row['availability_status'] = 'insufficient_peers'
                continue
            row['component_score'] = _percentile(
                float(row['raw_value']),
                peers,
                direction=str(row['direction']),
            )


def _panel_row_hash(row: Mapping[str, Any]) -> str:
    return _sha256({
        field: row.get(field)
        for field in PANEL_FIELDS
        if field != 'row_sha256'
    })


def _rank_requirements(
    components: Mapping[str, Mapping[str, Any]],
) -> tuple[bool, list[str]]:
    usable = {
        name
        for name, row in components.items()
        if row['availability_status'] == 'available'
        and row['component_score'] is not None
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
    return not reasons, reasons


def build_historical_core_panel(
    conn: sqlite3.Connection,
    bundle: ConfigBundle,
    *,
    stage6c_run_id: int,
    membership_rows: Sequence[Mapping[str, Any]],
    accepted_factor_cells: Sequence[Mapping[str, Any]],
    market_policy: MarketDataPolicy,
    progress: Callable[[int, int, str], None] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    all_labels = {
        (str(row['asof_date']), str(row['ticker'])): row
        for row in _label_rows(conn, stage6c_run_id=stage6c_run_id)
    }
    membership_identities = {
        (str(row['asof_date']), str(row['ticker']))
        for row in membership_rows
    }
    labels = {
        key: row for key, row in all_labels.items()
        if key in membership_identities
    }
    if set(labels) != membership_identities:
        raise RuntimeError(
            'Stage 8 membership and forward-label identities do not tie.'
        )
    directions: dict[str, str] = {}
    accepted_ids: set[str] = set()
    for cell in accepted_factor_cells:
        factor_id = str(cell['factor_id'])
        direction = str(cell['factor_direction'])
        if factor_id in directions and directions[factor_id] != direction:
            raise RuntimeError(
                f'Accepted factor direction conflict for {factor_id}.'
            )
        directions[factor_id] = direction
        accepted_ids.add(factor_id)
    specialized_rows = _specialized_rows(
        conn,
        stage6c_run_id=stage6c_run_id,
        factor_ids=accepted_ids,
    )
    specialized_by_key: dict[
        tuple[str, str], dict[str, dict[str, Any]]
    ] = defaultdict(dict)
    for row in specialized_rows:
        specialized_by_key[
            (str(row['asof_date']), str(row['ticker']))
        ][str(row['factor_id'])] = row

    dates = sorted({str(row['asof_date']) for row in membership_rows})
    tickers = {str(row['ticker']) for row in membership_rows}
    members_by_date: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in membership_rows:
        members_by_date[str(row['asof_date'])].append(row)
    selection, price_history, selection_sha = _price_selection_and_history(
        conn,
        tickers=tickers,
        maximum_date=dates[-1],
    )
    accepted_statuses = {
        'market': set(cfg_get(
            bundle.payload,
            'scoring_features.accepted_market_quality_statuses',
        )),
        'financial': set(cfg_get(
            bundle.payload,
            'scoring_features.accepted_financial_quality_statuses',
        )),
        'positioning': set(cfg_get(
            bundle.payload,
            'scoring_features.accepted_positioning_quality_statuses',
        )),
    }
    minimum_peers = int(cfg_get(
        bundle.payload, 'scoring_features.minimum_normalization_peer_count'
    ))
    neutral = float(cfg_get(
        bundle.payload, 'stage7_scoring.neutral_score'
    ))
    baseline_weights = stage7_component_weights(bundle)
    minimum_quality = float(cfg_get(
        bundle.payload,
        'stage7_scoring.minimum_data_quality_confidence',
    ))
    maximum_missing = float(cfg_get(
        bundle.payload,
        'stage7_scoring.maximum_missing_component_weight',
    ))
    panel: list[dict[str, Any]] = []
    financial_quality_counts: Counter[str] = Counter()
    positioning_quality_counts: Counter[str] = Counter()
    market_quality_counts: Counter[str] = Counter()

    for position, as_of in enumerate(dates, start=1):
        members = members_by_date[as_of]
        date_tickers = {str(row['ticker']) for row in members}
        market = _market_features_for_date(
            as_of=as_of,
            tickers=date_tickers,
            selection=selection,
            history=price_history,
            policy=market_policy,
        )
        financial = _financial_features_for_date(
            conn, bundle, as_of=as_of, tickers=date_tickers
        )
        positioning = _positioning_features_for_date(
            conn, bundle, as_of=as_of, tickers=date_tickers
        )
        market_quality_counts.update(
            str(row['quality_status']) for row in market.values()
        )
        financial_quality_counts.update(
            str(row['financial_quality_status'])
            for row in financial.values()
        )
        positioning_quality_counts.update(
            str(row['quality_status']) for row in positioning.values()
        )
        component_rows: list[dict[str, Any]] = []
        for member in members:
            ticker = str(member['ticker'])
            cohort = str(member['cohort_id'])
            upstreams = {
                'market': market.get(ticker),
                'financial': financial.get(ticker),
                'positioning': positioning.get(ticker),
            }
            for spec in CORE_COMPONENT_SPECS:
                upstream = upstreams[spec.group]
                quality_field = (
                    'financial_quality_status'
                    if spec.group == 'financial'
                    else 'quality_status'
                )
                quality = (
                    str(upstream[quality_field])
                    if upstream is not None else 'missing'
                )
                raw = (
                    _finite(upstream.get(spec.source_field))
                    if upstream is not None else None
                )
                available = (
                    upstream is not None
                    and quality in accepted_statuses[spec.group]
                    and raw is not None
                )
                component_rows.append({
                    'ticker': ticker,
                    'cohort_id': cohort,
                    'component_name': spec.name,
                    'raw_value': raw,
                    'component_score': None,
                    'direction': spec.direction,
                    'rank_requirement': spec.rank_requirement,
                    'availability_status': (
                        'available' if available
                        else 'missing_or_quality_rejected'
                    ),
                    'source_hash': (
                        str(upstream['source_hash'])
                        if upstream is not None else ''
                    ),
                })
        _normalize_component_rows(
            component_rows, minimum_peers=minimum_peers
        )
        components_by_ticker: dict[
            str, dict[str, dict[str, Any]]
        ] = defaultdict(dict)
        for row in component_rows:
            components_by_ticker[str(row['ticker'])][
                str(row['component_name'])
            ] = row

        specialized_components: list[dict[str, Any]] = []
        for member in members:
            ticker = str(member['ticker'])
            cohort = str(member['cohort_id'])
            available = specialized_by_key.get((as_of, ticker), {})
            for factor_id in sorted(accepted_ids):
                source = available.get(factor_id)
                if source is None:
                    continue
                raw = (
                    _finite(source['factor_value'])
                    if str(source['availability_status']) == 'available'
                    else None
                )
                specialized_components.append({
                    'ticker': ticker,
                    'cohort_id': cohort,
                    'component_name': factor_id,
                    'raw_value': raw,
                    'component_score': None,
                    'direction': (
                        'higher'
                        if directions[factor_id] == 'higher_is_better'
                        else 'lower'
                    ),
                    'availability_status': (
                        'available' if raw is not None
                        else 'missing_or_quality_rejected'
                    ),
                })
        _normalize_component_rows(
            specialized_components, minimum_peers=minimum_peers
        )
        specialized_scores_by_ticker: dict[str, dict[str, float]] = (
            defaultdict(dict)
        )
        for row in specialized_components:
            score = _finite(row['component_score'])
            if (
                row['availability_status'] == 'available'
                and score is not None
            ):
                specialized_scores_by_ticker[str(row['ticker'])][
                    str(row['component_name'])
                ] = score

        for member in members:
            ticker = str(member['ticker'])
            label = labels[(as_of, ticker)]
            components = components_by_ticker[ticker]
            ready, readiness_reasons = _rank_requirements(components)
            scores: dict[str, float] = {}
            quality: dict[str, float] = {}
            raw_values: dict[str, float | None] = {}
            source_hashes: dict[str, str] = {}
            weighted_score = 0.0
            available_weight = 0.0
            missing_weight = 0.0
            missing_components: list[str] = []
            for spec in CORE_COMPONENT_SPECS:
                component = components[spec.name]
                score = _finite(component['component_score'])
                available = (
                    component['availability_status'] == 'available'
                    and score is not None
                )
                effective = (
                    min(100.0, max(0.0, score)) if available else neutral
                )
                weight = baseline_weights[spec.name]
                scores[spec.name] = effective
                quality[spec.name] = 1.0 if available else 0.0
                raw_values[spec.name] = _finite(component['raw_value'])
                source_hashes[spec.name] = str(component['source_hash'])
                weighted_score += weight * effective
                if available:
                    available_weight += weight
                else:
                    missing_weight += weight
                    if weight > 0.0:
                        missing_components.append(spec.name)
            reasons = list(readiness_reasons)
            if available_weight < minimum_quality:
                reasons.append(f'low_data_quality={available_weight:.6f}')
            if missing_weight > maximum_missing:
                reasons.append(
                    f'missing_component_weight={missing_weight:.6f}:'
                    + ','.join(missing_components)
                )
            rank_ready = int(ready and not (
                available_weight < minimum_quality
                or missing_weight > maximum_missing
            ))
            membership_eligible = int(label['membership_eligible_flag'])
            investable = int(label['investable_flag'])
            specialized_applicable = {
                factor_id: int(
                    factor_id in specialized_by_key.get((as_of, ticker), {})
                )
                for factor_id in sorted(accepted_ids)
            }
            row = {
                'asof_date': as_of,
                'ticker': ticker,
                'cohort_id': str(member['cohort_id']),
                'applicability_subtype': str(
                    member['applicability_subtype']
                ),
                'sample_role': str(label['sample_role']),
                'membership_eligible_flag': membership_eligible,
                'investable_flag': investable,
                'label_status': str(label['label_status']),
                'market_regime': str(label['market_regime'] or ''),
                'terminal_event_status': str(
                    label['terminal_event_status'] or ''
                ),
                'baseline_rank_ready_flag': rank_ready,
                'calibration_eligible_flag': int(
                    rank_ready == 1
                    and membership_eligible == 1
                    and investable == 1
                ),
                'review_reason': ';'.join(sorted(reasons)),
                'core_score': weighted_score,
                'available_weight': available_weight,
                'missing_weight': missing_weight,
                'component_raw_values_json': _canonical_json(raw_values),
                'component_scores_json': _canonical_json(scores),
                'component_quality_json': _canonical_json(quality),
                'component_source_hashes_json': _canonical_json(source_hashes),
                'specialized_scores_json': _canonical_json(
                    specialized_scores_by_ticker.get(ticker, {})
                ),
                'specialized_applicability_json': _canonical_json(
                    specialized_applicable
                ),
                **{
                    f'forward_xlp_residual_return_{horizon}d': _finite(
                        label[f'forward_xlp_residual_return_{horizon}d']
                    )
                    for horizon in HORIZONS
                },
            }
            row['row_sha256'] = _panel_row_hash(row)
            panel.append(row)
        if progress is not None:
            progress(position, len(dates), as_of)

    panel.sort(key=lambda row: (row['asof_date'], row['ticker']))
    panel_sha = _sha256([row['row_sha256'] for row in panel])
    return panel, {
        'panel_sha256': panel_sha,
        'row_count': len(panel),
        'date_count': len(dates),
        'ticker_count': len(tickers),
        'first_date': dates[0],
        'last_date': dates[-1],
        'frozen_price_selection_sha256': selection_sha,
        'accepted_specialized_factor_count': len(accepted_ids),
        'market_quality_counts': dict(sorted(market_quality_counts.items())),
        'financial_quality_counts': dict(
            sorted(financial_quality_counts.items())
        ),
        'positioning_quality_counts': dict(
            sorted(positioning_quality_counts.items())
        ),
        'sample_role': RESEARCH_SAMPLE_ROLE,
    }


def _rank_values(values: Sequence[float]) -> list[float]:
    order = sorted(range(len(values)), key=lambda index: values[index])
    ranks = [0.0] * len(values)
    position = 0
    while position < len(order):
        end = position + 1
        while (
            end < len(order)
            and values[order[end]] == values[order[position]]
        ):
            end += 1
        average_rank = (position + 1 + end) / 2.0
        for offset in range(position, end):
            ranks[order[offset]] = average_rank
        position = end
    return ranks


def _correlation(left: Sequence[float], right: Sequence[float]) -> float | None:
    if len(left) != len(right) or len(left) < 2:
        return None
    left_mean = statistics.fmean(left)
    right_mean = statistics.fmean(right)
    left_delta = [value - left_mean for value in left]
    right_delta = [value - right_mean for value in right]
    denominator = math.sqrt(
        sum(value * value for value in left_delta)
        * sum(value * value for value in right_delta)
    )
    if denominator <= 0.0:
        return None
    return sum(
        first * second
        for first, second in zip(left_delta, right_delta, strict=True)
    ) / denominator


def _spearman(left: Sequence[float], right: Sequence[float]) -> float | None:
    return _correlation(_rank_values(left), _rank_values(right))


def _prepared_panel(
    rows: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    prepared: list[dict[str, Any]] = []
    for source in rows:
        row = dict(source)
        row['_component_scores'] = json.loads(
            str(row['component_scores_json'])
        )
        row['_component_quality'] = json.loads(
            str(row['component_quality_json'])
        )
        row['_specialized_scores'] = json.loads(
            str(row['specialized_scores_json'])
        )
        row['_specialized_applicability'] = json.loads(
            str(row['specialized_applicability_json'])
        )
        prepared.append(row)
    return prepared


def _score_candidate(
    row: Mapping[str, Any],
    candidate: Candidate,
    bundle: ConfigBundle,
) -> tuple[float, float, float, bool]:
    neutral = float(cfg_get(
        bundle.payload, 'stage7_scoring.neutral_score'
    ))
    scores = row['_component_scores']
    quality = row['_component_quality']
    specialized_scores = row['_specialized_scores']
    weighted_score = 0.0
    available_weight = 0.0
    missing_weight = 0.0
    for name, weight in candidate.core_weights.items():
        available = float(quality.get(name, 0.0)) > 0.0
        score = _finite(scores.get(name))
        effective = score if available and score is not None else neutral
        weighted_score += weight * min(100.0, max(0.0, effective))
        if available and score is not None:
            available_weight += weight
        else:
            missing_weight += weight
    for name, weight in candidate.specialized_weights.items():
        score = _finite(specialized_scores.get(name))
        effective = score if score is not None else neutral
        weighted_score += weight * min(100.0, max(0.0, effective))
        if score is not None:
            available_weight += weight
        else:
            missing_weight += weight
    minimum_quality = float(cfg_get(
        bundle.payload,
        'stage7_scoring.minimum_data_quality_confidence',
    ))
    maximum_missing = float(cfg_get(
        bundle.payload,
        'stage7_scoring.maximum_missing_component_weight',
    ))
    eligible = (
        int(row['calibration_eligible_flag']) == 1
        and available_weight >= minimum_quality
        and missing_weight <= maximum_missing
    )
    return weighted_score, available_weight, missing_weight, eligible


def calibration_date_census(
    rows: Sequence[Mapping[str, Any]],
    baseline: Candidate,
    bundle: ConfigBundle,
) -> tuple[list[str], list[dict[str, Any]]]:
    """Select dates without observing outcomes under the frozen baseline."""
    if baseline.scope_id != SECTOR_SCOPE:
        raise ValueError('Stage 8 date census requires the sector baseline.')
    floor = int(cfg_get(
        bundle.payload, 'stage8_calibration.minimum_sector_cross_section'
    ))
    by_date: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in rows:
        by_date[str(row['asof_date'])].append(row)
    census: list[dict[str, Any]] = []
    selected: list[str] = []
    for as_of in sorted(by_date):
        eligible_count = sum(
            int(_score_candidate(row, baseline, bundle)[3])
            for row in by_date[as_of]
        )
        included = eligible_count >= floor
        if included:
            selected.append(as_of)
        census.append({
            'asof_date': as_of,
            'eligible_count': eligible_count,
            'minimum_sector_cross_section': floor,
            'included_flag': int(included),
        })
    return selected, census


def evaluate_candidate(
    rows: Sequence[Mapping[str, Any]],
    dates: Sequence[str],
    candidate: Candidate,
    bundle: ConfigBundle,
    *,
    required_factor_ids: Sequence[str] = (),
) -> dict[str, Any]:
    settings = cfg_get(bundle.payload, 'stage8_calibration')
    scope_rows = [
        row for row in rows
        if (
            candidate.scope_id == SECTOR_SCOPE
            or str(row['cohort_id']) == candidate.scope_id
        )
        and str(row['asof_date']) in set(dates)
        and all(
            int(row['_specialized_applicability'].get(factor_id, 0)) == 1
            for factor_id in required_factor_ids
        )
    ]
    rows_by_date: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in scope_rows:
        rows_by_date[str(row['asof_date'])].append(row)
    minimum_cross_section = int(settings[
        'minimum_sector_cross_section'
        if candidate.scope_id == SECTOR_SCOPE
        else 'minimum_cohort_cross_section'
    ])
    top_quantile = float(settings['top_quantile'])
    minimum_top = int(settings['minimum_top_positions'])
    transaction_cost = float(settings['transaction_cost_bps']) / 10000.0
    ic_values: dict[int, list[float]] = defaultdict(list)
    spread_values: dict[int, list[float]] = defaultdict(list)
    eligible_date_count: dict[int, int] = defaultdict(int)
    turnovers: list[float] = []
    cohort_shares: list[float] = []
    previous_top: set[str] | None = None
    date_details: list[dict[str, Any]] = []

    for as_of in dates:
        scored: list[tuple[float, Mapping[str, Any]]] = []
        for row in rows_by_date.get(str(as_of), []):
            score, _available, _missing, eligible = _score_candidate(
                row, candidate, bundle
            )
            if eligible:
                scored.append((score, row))
        scored.sort(key=lambda item: (-item[0], str(item[1]['ticker'])))
        if len(scored) < minimum_cross_section:
            continue
        top_count = max(
            minimum_top, int(math.ceil(len(scored) * top_quantile))
        )
        if top_count * 2 > len(scored):
            continue
        top = scored[:top_count]
        bottom = scored[-top_count:]
        top_tickers = {str(row['ticker']) for _, row in top}
        turnover = 0.0
        if previous_top is not None:
            denominator = max(len(previous_top), len(top_tickers), 1)
            turnover = 1.0 - len(
                previous_top & top_tickers
            ) / denominator
            turnovers.append(turnover)
        previous_top = top_tickers
        if candidate.scope_id == SECTOR_SCOPE:
            counts = Counter(str(row['cohort_id']) for _, row in top)
            cohort_share = max(counts.values()) / len(top)
        else:
            cohort_share = 1.0
        cohort_shares.append(cohort_share)
        detail: dict[str, Any] = {
            'asof_date': as_of,
            'cross_section': len(scored),
            'top_count': top_count,
            'top_turnover': turnover,
            'top_cohort_share': cohort_share,
        }
        for horizon in HORIZONS:
            target = f'forward_xlp_residual_return_{horizon}d'
            paired = [
                (score, float(row[target]))
                for score, row in scored
                if _finite(row[target]) is not None
            ]
            if len(paired) < minimum_cross_section:
                continue
            ic = _spearman(
                [item[0] for item in paired],
                [item[1] for item in paired],
            )
            top_returns = [
                float(row[target])
                for _, row in top if _finite(row[target]) is not None
            ]
            bottom_returns = [
                float(row[target])
                for _, row in bottom if _finite(row[target]) is not None
            ]
            if (
                ic is None
                or len(top_returns) < minimum_top
                or len(bottom_returns) < minimum_top
            ):
                continue
            spread = (
                statistics.fmean(top_returns)
                - statistics.fmean(bottom_returns)
                - 2.0 * turnover * transaction_cost
            )
            ic_values[horizon].append(ic)
            spread_values[horizon].append(spread)
            eligible_date_count[horizon] += 1
            detail[f'ic_{horizon}d'] = ic
            detail[f'spread_net_{horizon}d'] = spread
        date_details.append(detail)

    minimum_dates = max(3, int(math.ceil(len(dates) * 0.5)))
    complete = all(
        eligible_date_count[horizon] >= minimum_dates
        for horizon in HORIZONS
    )
    horizon_weights = {
        int(key): float(value)
        for key, value in settings['horizon_weights'].items()
    }
    mean_ic = {
        horizon: (
            statistics.fmean(ic_values[horizon])
            if ic_values[horizon] else None
        )
        for horizon in HORIZONS
    }
    mean_spread = {
        horizon: (
            statistics.fmean(spread_values[horizon])
            if spread_values[horizon] else None
        )
        for horizon in HORIZONS
    }
    objective = (
        sum(
            horizon_weights[horizon] * float(mean_ic[horizon])
            for horizon in HORIZONS
        )
        if complete else None
    )
    average_turnover = statistics.fmean(turnovers) if turnovers else 0.0
    average_cohort_share = (
        statistics.fmean(cohort_shares) if cohort_shares else 0.0
    )
    turnover_pass = average_turnover <= float(
        settings['maximum_top_turnover']
    )
    concentration_pass = (
        candidate.scope_id != SECTOR_SCOPE
        or average_cohort_share <= float(
            settings['maximum_top_cohort_share']
        )
    )
    return {
        'candidate_id': candidate.candidate_id,
        'scope_id': candidate.scope_id,
        'candidate_kind': candidate.candidate_kind,
        'status': 'complete' if complete else 'inconclusive',
        'requested_date_count': len(dates),
        'scored_date_count': len(date_details),
        'objective': objective,
        'average_top_turnover': average_turnover,
        'average_top_cohort_share': average_cohort_share,
        'turnover_cap_pass': int(turnover_pass),
        'cohort_concentration_cap_pass': int(concentration_pass),
        'constraint_pass': int(turnover_pass and concentration_pass),
        **{
            f'mean_ic_{horizon}d': mean_ic[horizon]
            for horizon in HORIZONS
        },
        **{
            f'mean_spread_net_{horizon}d': mean_spread[horizon]
            for horizon in HORIZONS
        },
        **{
            f'eligible_date_count_{horizon}d': eligible_date_count[horizon]
            for horizon in HORIZONS
        },
        'date_details': date_details,
    }


def _result_row(
    result: Mapping[str, Any],
    *,
    family_id: str,
    phase: str,
    selected: bool,
    verdict: str = '',
) -> dict[str, Any]:
    fields = {
        key: value for key, value in result.items() if key != 'date_details'
    }
    return {
        'family_id': family_id,
        'phase': phase,
        'selected_flag': int(selected),
        'verdict': verdict,
        **fields,
        'result_sha256': _sha256({
            'family_id': family_id,
            'phase': phase,
            'selected_flag': int(selected),
            'verdict': verdict,
            **fields,
        }),
    }


def _best_candidate(
    results: Sequence[Mapping[str, Any]],
    *,
    baseline_id: str,
) -> Mapping[str, Any] | None:
    candidates = [
        row for row in results
        if row['candidate_id'] != baseline_id
        and row['status'] == 'complete'
        and int(row['constraint_pass']) == 1
        and row['objective'] is not None
    ]
    if not candidates:
        return None
    return max(
        candidates,
        key=lambda row: (float(row['objective']), str(row['candidate_id'])),
    )


def _walk_forward(
    rows: Sequence[Mapping[str, Any]],
    dates: Sequence[str],
    *,
    holdout_start: str,
    candidates: Sequence[Candidate],
    baseline: Candidate,
    bundle: ConfigBundle,
    family_id: str,
    required_factor_ids: Sequence[str],
) -> tuple[list[dict[str, Any]], float, float]:
    settings = cfg_get(bundle.payload, 'stage8_calibration')
    embargo = int(settings['embargo_panel_dates'])
    initial_train = int(settings['walk_forward_initial_train_dates'])
    test_count = int(settings['walk_forward_test_dates'])
    holdout_index = list(dates).index(holdout_start)
    test_start = initial_train + embargo
    folds: list[dict[str, Any]] = []
    wins = 0
    constraints = 0
    while test_start + test_count <= holdout_index:
        train_dates = list(dates[:test_start - embargo])
        test_dates = list(dates[test_start:test_start + test_count])
        train_results = [
            evaluate_candidate(
                rows, train_dates, candidate, bundle,
                required_factor_ids=required_factor_ids,
            )
            for candidate in candidates
        ]
        best = _best_candidate(
            train_results, baseline_id=baseline.candidate_id
        )
        if best is None:
            folds.append({
                'family_id': family_id,
                'fold': len(folds) + 1,
                'train_start': train_dates[0],
                'train_end': train_dates[-1],
                'embargo_start': dates[test_start - embargo],
                'embargo_end': dates[test_start - 1],
                'test_start': test_dates[0],
                'test_end': test_dates[-1],
                'candidate_id': '',
                'candidate_objective': '',
                'baseline_objective': '',
                'objective_improvement': '',
                'constraint_pass': 0,
                'win_flag': 0,
            })
            test_start += test_count
            continue
        selected = next(
            candidate for candidate in candidates
            if candidate.candidate_id == best['candidate_id']
        )
        candidate_test = evaluate_candidate(
            rows, test_dates, selected, bundle,
            required_factor_ids=required_factor_ids,
        )
        baseline_test = evaluate_candidate(
            rows, test_dates, baseline, bundle,
            required_factor_ids=required_factor_ids,
        )
        candidate_objective = _finite(candidate_test['objective'])
        baseline_objective = _finite(baseline_test['objective'])
        improvement = (
            candidate_objective - baseline_objective
            if candidate_objective is not None
            and baseline_objective is not None
            else None
        )
        constraint_pass = int(candidate_test['constraint_pass'])
        win = int(
            improvement is not None
            and improvement > 0.0
            and constraint_pass == 1
        )
        wins += win
        constraints += constraint_pass
        folds.append({
            'family_id': family_id,
            'fold': len(folds) + 1,
            'train_start': train_dates[0],
            'train_end': train_dates[-1],
            'embargo_start': dates[test_start - embargo],
            'embargo_end': dates[test_start - 1],
            'test_start': test_dates[0],
            'test_end': test_dates[-1],
            'candidate_id': selected.candidate_id,
            'candidate_objective': candidate_objective,
            'baseline_objective': baseline_objective,
            'objective_improvement': improvement,
            'constraint_pass': constraint_pass,
            'win_flag': win,
        })
        test_start += test_count
    denominator = len(folds)
    return (
        folds,
        wins / denominator if denominator else 0.0,
        constraints / denominator if denominator else 0.0,
    )


def _run_research_family(
    rows: Sequence[Mapping[str, Any]],
    all_dates: Sequence[str],
    *,
    split: ChronologicalSplit,
    candidates: Sequence[Candidate],
    baseline: Candidate,
    bundle: ConfigBundle,
    family_id: str,
    required_factor_ids: Sequence[str] = (),
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    train_results = [
        evaluate_candidate(
            rows,
            split.train_dates,
            candidate,
            bundle,
            required_factor_ids=required_factor_ids,
        )
        for candidate in candidates
    ]
    best_train = _best_candidate(
        train_results, baseline_id=baseline.candidate_id
    )
    result_rows = [
        _result_row(
            result,
            family_id=family_id,
            phase='train',
            selected=(
                best_train is not None
                and result['candidate_id'] == best_train['candidate_id']
            ),
        )
        for result in train_results
    ]
    if best_train is None:
        return result_rows, [], {
            'family_id': family_id,
            'scope_id': baseline.scope_id,
            'candidate_kind': (
                'core_plus_specialized'
                if required_factor_ids else 'core_reweight'
            ),
            'required_factor_ids': list(required_factor_ids),
            'selected_candidate_id': None,
            'verdict': 'inconclusive',
            'reason': 'no_complete_constraint_feasible_training_candidate',
            'validation_gate_pass': 0,
            'walk_forward_gate_pass': 0,
            'holdout_opened': 0,
            'production_weight_change_allowed': 0,
        }
    selected = next(
        candidate for candidate in candidates
        if candidate.candidate_id == best_train['candidate_id']
    )
    validation_candidate = evaluate_candidate(
        rows,
        split.validation_dates,
        selected,
        bundle,
        required_factor_ids=required_factor_ids,
    )
    validation_baseline = evaluate_candidate(
        rows,
        split.validation_dates,
        baseline,
        bundle,
        required_factor_ids=required_factor_ids,
    )
    validation_candidate_objective = _finite(
        validation_candidate['objective']
    )
    validation_baseline_objective = _finite(
        validation_baseline['objective']
    )
    validation_improvement = (
        validation_candidate_objective - validation_baseline_objective
        if validation_candidate_objective is not None
        and validation_baseline_objective is not None
        else None
    )
    settings = cfg_get(bundle.payload, 'stage8_calibration')
    validation_gate = (
        validation_candidate['status'] == 'complete'
        and validation_baseline['status'] == 'complete'
        and int(validation_candidate['constraint_pass']) == 1
        and validation_improvement is not None
        and validation_improvement >= float(
            settings['minimum_validation_objective_improvement']
        )
        and all(
            float(
                validation_candidate[f'mean_ic_{horizon}d'] or -1.0
            ) > 0.0
            for horizon in (63, 126)
        )
    )
    result_rows.extend((
        _result_row(
            validation_baseline,
            family_id=family_id,
            phase='validation',
            selected=False,
        ),
        _result_row(
            validation_candidate,
            family_id=family_id,
            phase='validation',
            selected=True,
            verdict='pass' if validation_gate else 'reject',
        ),
    ))
    walk_rows, walk_win_fraction, walk_constraint_fraction = _walk_forward(
        rows,
        all_dates,
        holdout_start=split.holdout_dates[0],
        candidates=candidates,
        baseline=baseline,
        bundle=bundle,
        family_id=family_id,
        required_factor_ids=required_factor_ids,
    )
    walk_gate = (
        bool(walk_rows)
        and walk_win_fraction >= float(
            settings['minimum_walk_forward_win_fraction']
        )
        and walk_constraint_fraction >= float(
            settings['minimum_walk_forward_win_fraction']
        )
    )
    if not validation_gate or not walk_gate:
        return result_rows, walk_rows, {
            'family_id': family_id,
            'scope_id': baseline.scope_id,
            'candidate_kind': (
                'core_plus_specialized'
                if required_factor_ids else 'core_reweight'
            ),
            'required_factor_ids': list(required_factor_ids),
            'selected_candidate_id': selected.candidate_id,
            'verdict': 'rejected',
            'reason': (
                'validation_gate_failed'
                if not validation_gate else 'walk_forward_gate_failed'
            ),
            'validation_objective_improvement': validation_improvement,
            'validation_gate_pass': int(validation_gate),
            'walk_forward_win_fraction': walk_win_fraction,
            'walk_forward_constraint_fraction': walk_constraint_fraction,
            'walk_forward_gate_pass': int(walk_gate),
            'holdout_opened': 0,
            'production_weight_change_allowed': 0,
        }
    holdout_candidate = evaluate_candidate(
        rows,
        split.holdout_dates,
        selected,
        bundle,
        required_factor_ids=required_factor_ids,
    )
    holdout_baseline = evaluate_candidate(
        rows,
        split.holdout_dates,
        baseline,
        bundle,
        required_factor_ids=required_factor_ids,
    )
    candidate_objective = _finite(holdout_candidate['objective'])
    baseline_objective = _finite(holdout_baseline['objective'])
    improvement = (
        candidate_objective - baseline_objective
        if candidate_objective is not None
        and baseline_objective is not None
        else None
    )
    ic_thresholds = {
        int(key): float(value)
        for key, value in settings['minimum_holdout_mean_ic'].items()
    }
    holdout_gate = (
        holdout_candidate['status'] == 'complete'
        and holdout_baseline['status'] == 'complete'
        and int(holdout_candidate['constraint_pass']) == 1
        and improvement is not None
        and improvement >= float(
            settings['minimum_holdout_objective_improvement']
        )
        and all(
            float(holdout_candidate[f'mean_ic_{horizon}d'] or -1.0)
            >= threshold
            for horizon, threshold in ic_thresholds.items()
        )
    )
    result_rows.extend((
        _result_row(
            holdout_baseline,
            family_id=family_id,
            phase='holdout',
            selected=False,
        ),
        _result_row(
            holdout_candidate,
            family_id=family_id,
            phase='holdout',
            selected=True,
            verdict='accepted' if holdout_gate else 'rejected',
        ),
    ))
    return result_rows, walk_rows, {
        'family_id': family_id,
        'scope_id': baseline.scope_id,
        'candidate_kind': (
            'core_plus_specialized'
            if required_factor_ids else 'core_reweight'
        ),
        'required_factor_ids': list(required_factor_ids),
        'selected_candidate_id': selected.candidate_id,
        'verdict': 'accepted' if holdout_gate else 'rejected',
        'reason': (
            'all_registered_research_gates_passed'
            if holdout_gate else 'final_holdout_gate_failed'
        ),
        'validation_objective_improvement': validation_improvement,
        'validation_gate_pass': 1,
        'walk_forward_win_fraction': walk_win_fraction,
        'walk_forward_constraint_fraction': walk_constraint_fraction,
        'walk_forward_gate_pass': 1,
        'holdout_opened': 1,
        'holdout_objective_improvement': improvement,
        'holdout_gate_pass': int(holdout_gate),
        'production_weight_change_allowed': 0,
    }


def _immutable_text(path: Path, content: str) -> None:
    encoded = content.encode('utf-8')
    if path.exists():
        if path.is_symlink() or not path.is_file():
            raise ValueError(f'Immutable Stage 8 path is unsafe: {path}')
        if path.read_bytes() != encoded:
            raise FileExistsError(
                f'Immutable Stage 8 artifact content changed: {path}'
            )
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with atomic_text_writer(
        path, encoding='utf-8', newline=''
    ) as handle:
        handle.write(content)


def _immutable_json(path: Path, payload: Any) -> None:
    _immutable_text(path, json.dumps(
        payload, indent=2, sort_keys=True, allow_nan=False
    ) + '\n')


def _csv_text(
    rows: Sequence[Mapping[str, Any]],
    *,
    fieldnames: Sequence[str] | None = None,
) -> str:
    import io

    columns = list(fieldnames or [])
    if not columns:
        for row in rows:
            for key in row:
                if key not in columns:
                    columns.append(str(key))
    handle = io.StringIO(newline='')
    writer = csv.DictWriter(
        handle,
        fieldnames=columns or ['status'],
        extrasaction='ignore',
        lineterminator='\n',
    )
    writer.writeheader()
    writer.writerows(rows)
    return handle.getvalue()


def _candidate_registry_payload(
    *,
    contract_sha256: str,
    candidates: Sequence[Candidate],
) -> dict[str, Any]:
    rows = [asdict(candidate) for candidate in candidates]
    return {
        'schema_version': 'consumer_defensive_stage8_candidate_registry_v1',
        'contract_sha256': contract_sha256,
        'candidate_count': len(rows),
        'candidate_preregistration_sha256s': [
            candidate.preregistration_sha256 for candidate in candidates
        ],
        'registry_sha256': _sha256([
            candidate.preregistration_sha256 for candidate in candidates
        ]),
        'candidates': rows,
        'registered_before_label_evaluation': True,
        'production_promotion_enabled': False,
        'portfolio_write_enabled': False,
    }


def run_stage8_calibration(
    conn: sqlite3.Connection,
    bundle: ConfigBundle,
    *,
    stage6c_run_id: int,
    factor_root: Path,
    market_policy_path: Path,
    output_dir: Path,
    progress: Callable[[int, int, str], None] | None = None,
) -> dict[str, Any]:
    stage6c_run = _stage6c_run(
        conn, stage6c_run_id=stage6c_run_id
    )
    stage7 = verify_stage7_baseline(conn, bundle)
    campaign_id = str(cfg_get(
        bundle.payload, 'stage7_scoring.factor_validation_campaign_id'
    ))
    factor_campaign, accepted_cells = verify_factor_campaign(
        factor_root, campaign_id=campaign_id
    )
    source_membership = _membership_rows(
        conn, stage6c_run_id=stage6c_run_id
    )
    membership, calibration_scope_summary = apply_calibration_scope(
        source_membership, bundle
    )
    settings = cfg_get(bundle.payload, 'stage8_calibration')
    contract = stage8_contract_payload(
        bundle,
        stage6c_run=stage6c_run,
        factor_campaign_id=campaign_id,
        factor_registry_sha256=str(factor_campaign['registry_sha256']),
    )
    contract_sha = _sha256(contract)
    candidates = build_candidate_registry(
        bundle,
        membership_rows=membership,
        accepted_factor_cells=accepted_cells,
    )
    candidate_registry = _candidate_registry_payload(
        contract_sha256=contract_sha,
        candidates=candidates,
    )
    root = output_dir.expanduser().resolve()
    _immutable_json(root / CONTRACT_FILE, {
        **contract, 'contract_sha256': contract_sha
    })
    _immutable_json(root / CANDIDATE_FILE, candidate_registry)

    market_policy = load_market_policy(market_policy_path)
    panel, panel_summary = build_historical_core_panel(
        conn,
        bundle,
        stage6c_run_id=stage6c_run_id,
        membership_rows=membership,
        accepted_factor_cells=accepted_cells,
        market_policy=market_policy,
        progress=progress,
    )
    panel_summary = {
        **panel_summary,
        'calibration_scope': calibration_scope_summary,
    }
    _immutable_text(
        root / PANEL_FILE,
        _csv_text(panel, fieldnames=PANEL_FIELDS),
    )
    prepared = _prepared_panel(panel)
    sector_baseline = next(
        candidate for candidate in candidates
        if candidate.scope_id == SECTOR_SCOPE
        and candidate.candidate_kind == 'stage7_core_baseline'
    )
    dates, date_census = calibration_date_census(
        prepared, sector_baseline, bundle
    )
    split = chronological_split(
        dates,
        minimum_train_dates=int(settings['minimum_train_dates']),
        validation_dates=int(settings['validation_dates']),
        holdout_dates=int(settings['holdout_dates']),
        embargo_panel_dates=int(settings['embargo_panel_dates']),
    )
    panel_dates = sorted({str(row['asof_date']) for row in panel})
    excluded_dates = sorted(set(panel_dates) - set(dates))
    split_payload = {
        'schema_version': 'consumer_defensive_stage8_split_v1',
        'contract_sha256': contract_sha,
        'panel_sha256': panel_summary['panel_sha256'],
        **asdict(split),
        'calibration_date_policy': (
            'frozen_stage7_sector_baseline_rank_ready_cross_section'
        ),
        'calibration_date_census': date_census,
        'excluded_non_calibration_dates': excluded_dates,
        'maximum_label_horizon_trading_days': max(HORIZONS),
        'evaluation_step_trading_days': 21,
        'holdout_sealed_until_static_validation_and_walk_forward_pass': True,
    }
    split_payload['split_sha256'] = _sha256(split_payload)
    _immutable_json(root / SPLIT_FILE, split_payload)

    result_rows: list[dict[str, Any]] = []
    walk_rows: list[dict[str, Any]] = []
    family_decisions: list[dict[str, Any]] = []
    candidates_by_scope: dict[str, list[Candidate]] = defaultdict(list)
    for candidate in candidates:
        candidates_by_scope[candidate.scope_id].append(candidate)
    for scope_id in sorted(candidates_by_scope):
        scoped = candidates_by_scope[scope_id]
        baseline = next(
            candidate for candidate in scoped
            if candidate.candidate_kind == 'stage7_core_baseline'
        )
        core_candidates = [
            candidate for candidate in scoped
            if candidate.candidate_kind != 'core_plus_specialized'
        ]
        family_id = f'{scope_id}__core'
        results, walks, decision = _run_research_family(
            prepared,
            dates,
            split=split,
            candidates=core_candidates,
            baseline=baseline,
            bundle=bundle,
            family_id=family_id,
        )
        result_rows.extend(results)
        walk_rows.extend(walks)
        family_decisions.append(decision)
        for specialized in (
            candidate for candidate in scoped
            if candidate.candidate_kind == 'core_plus_specialized'
        ):
            factor_ids = tuple(sorted(specialized.specialized_weights))
            specialized_family = (
                f'{scope_id}__specialized__{specialized.candidate_id}'
            )
            results, walks, decision = _run_research_family(
                prepared,
                dates,
                split=split,
                candidates=(baseline, specialized),
                baseline=baseline,
                bundle=bundle,
                family_id=specialized_family,
                required_factor_ids=factor_ids,
            )
            result_rows.extend(results)
            walk_rows.extend(walks)
            family_decisions.append(decision)

    accepted = [
        row for row in family_decisions if row['verdict'] == 'accepted'
    ]
    run_id = (
        'cds8_'
        + _sha256({
            'contract_sha256': contract_sha,
            'candidate_registry_sha256': candidate_registry['registry_sha256'],
            'panel_sha256': panel_summary['panel_sha256'],
            'split_sha256': split_payload['split_sha256'],
        })[:24]
    )
    decision = {
        'schema_version': 'consumer_defensive_stage8_decision_v1',
        'stage8_run_id': run_id,
        'asof_date': str(stage6c_run['asof_date']),
        'contract_sha256': contract_sha,
        'candidate_registry_sha256': candidate_registry['registry_sha256'],
        'panel_summary': panel_summary,
        'split_sha256': split_payload['split_sha256'],
        'stage7_baseline': stage7,
        'factor_campaign': factor_campaign,
        'family_decisions': family_decisions,
        'accepted_research_candidate_count': len(accepted),
        'research_verdict': (
            'candidate_evidence_for_stage9'
            if accepted else 'retain_stage7_core_baseline'
        ),
        'action': (
            'retain_stage7_weights_and_test_accepted_candidates_in_stage9'
            if accepted
            else 'retain_stage7_core_baseline'
        ),
        'production_promotion_enabled': False,
        'portfolio_write_enabled': False,
        'stage7_weight_mutation_enabled': False,
        'oos_score_valid_flag': 0,
    }
    decision['decision_sha256'] = _sha256(decision)
    result_rows.sort(key=lambda row: (
        str(row['family_id']), str(row['phase']),
        -int(row['selected_flag']), str(row['candidate_id'])
    ))
    walk_rows.sort(key=lambda row: (
        str(row['family_id']), int(row['fold'])
    ))
    _immutable_text(root / RESULT_FILE, _csv_text(result_rows))
    _immutable_text(root / WALK_FILE, _csv_text(walk_rows))
    _immutable_json(root / DECISION_FILE, decision)
    artifact_names = (
        CONTRACT_FILE, CANDIDATE_FILE, SPLIT_FILE, PANEL_FILE,
        RESULT_FILE, WALK_FILE, DECISION_FILE,
    )
    artifacts = {
        name: {
            'sha256': _file_sha256(root / name),
            'bytes': (root / name).stat().st_size,
        }
        for name in artifact_names
    }
    manifest = {
        'schema_version': 'consumer_defensive_stage8_artifact_manifest_v1',
        'stage8_run_id': run_id,
        'contract_sha256': contract_sha,
        'artifacts': artifacts,
        'production_promotion_enabled': False,
        'portfolio_write_enabled': False,
        'stage7_weight_mutation_enabled': False,
    }
    manifest['manifest_sha256'] = _sha256(manifest)
    _immutable_json(root / MANIFEST_FILE, manifest)
    validation = validate_stage8_artifacts(
        conn,
        bundle,
        output_dir=root,
        factor_root=factor_root,
    )
    if validation['status'] != 'PASS':
        raise RuntimeError(
            f'Stage 8 artifact validation failed: {validation["errors"]}'
        )
    return {
        'stage8_run_id': run_id,
        'output_dir': str(root),
        'decision': decision,
        'validation': validation,
    }


def _read_json(path: Path) -> Any:
    if path.is_symlink() or not path.is_file():
        raise ValueError(f'Stage 8 artifact is not a regular file: {path}')
    return json.loads(path.read_text(encoding='utf-8'))


def _parse_panel_csv_row(row: Mapping[str, str]) -> dict[str, Any]:
    parsed: dict[str, Any] = dict(row)
    for field in (
        'membership_eligible_flag', 'investable_flag',
        'baseline_rank_ready_flag', 'calibration_eligible_flag',
    ):
        parsed[field] = int(row[field])
    for field in (
        'core_score', 'available_weight', 'missing_weight',
        'forward_xlp_residual_return_21d',
        'forward_xlp_residual_return_63d',
        'forward_xlp_residual_return_126d',
    ):
        parsed[field] = None if row[field] == '' else float(row[field])
    return parsed


def validate_stage8_artifacts(
    conn: sqlite3.Connection,
    bundle: ConfigBundle,
    *,
    output_dir: Path,
    factor_root: Path,
) -> dict[str, Any]:
    root = output_dir.expanduser().resolve()
    errors: list[str] = []
    required = {
        CONTRACT_FILE, CANDIDATE_FILE, SPLIT_FILE, PANEL_FILE,
        RESULT_FILE, WALK_FILE, DECISION_FILE, MANIFEST_FILE,
    }
    missing = sorted(
        name for name in required
        if not (root / name).is_file() or (root / name).is_symlink()
    )
    if missing:
        return {
            'status': 'FAIL',
            'errors': [f'missing_or_unsafe_artifacts:{missing}'],
            'check_count': 1,
            'passed_check_count': 0,
        }
    try:
        manifest = _read_json(root / MANIFEST_FILE)
        contract = _read_json(root / CONTRACT_FILE)
        registry = _read_json(root / CANDIDATE_FILE)
        split = _read_json(root / SPLIT_FILE)
        decision = _read_json(root / DECISION_FILE)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return {
            'status': 'FAIL',
            'errors': [f'artifact_parse_failed:{type(exc).__name__}:{exc}'],
            'check_count': 1,
            'passed_check_count': 0,
        }
    checks: list[tuple[str, bool]] = []

    def check(name: str, condition: bool) -> None:
        checks.append((name, bool(condition)))
        if not condition:
            errors.append(name)

    manifest_without_hash = dict(manifest)
    observed_manifest_hash = manifest_without_hash.pop(
        'manifest_sha256', None
    )
    check(
        'manifest_hash_exact',
        observed_manifest_hash == _sha256(manifest_without_hash),
    )
    artifact_map = manifest.get('artifacts')
    artifact_hashes_ok = isinstance(artifact_map, dict)
    if artifact_hashes_ok:
        for name in required - {MANIFEST_FILE}:
            entry = artifact_map.get(name)
            path = root / name
            if (
                not isinstance(entry, dict)
                or entry.get('sha256') != _file_sha256(path)
                or int(entry.get('bytes', -1)) != path.stat().st_size
            ):
                artifact_hashes_ok = False
                break
    check('artifact_hashes_and_sizes_exact', artifact_hashes_ok)
    for payload_name, payload in (
        ('contract', contract),
        ('registry', registry),
        ('manifest', manifest),
        ('decision', decision),
    ):
        check(
            f'{payload_name}_safety_locks',
            payload.get('production_promotion_enabled') is False
            and payload.get('portfolio_write_enabled') is False
            and (
                payload_name == 'registry'
                or payload.get('stage7_weight_mutation_enabled') is False
            ),
        )
    contract_without_hash = dict(contract)
    observed_contract_hash = contract_without_hash.pop(
        'contract_sha256', None
    )
    check(
        'contract_hash_exact',
        observed_contract_hash == _sha256(contract_without_hash),
    )
    expected_scope = calibration_scope_contract(bundle)
    check(
        'calibration_scope_contract_exact',
        contract.get('calibration_scope') == expected_scope,
    )
    methodology = contract.get('methodology_file_sha256s')
    methodology_ok = isinstance(methodology, dict)
    expected_paths = {
        Path(__file__).resolve().name: Path(__file__).resolve(),
        'scoring_features.py': (
            bundle.base_dir / 'core' / 'scoring_features.py'
        ).resolve(),
        'stage7_scoring.py': (
            bundle.base_dir / 'core' / 'stage7_scoring.py'
        ).resolve(),
        'financial_pipeline.py': (
            bundle.base_dir / 'core' / 'financial_pipeline.py'
        ).resolve(),
        'market_data.py': (
            bundle.base_dir / 'core' / 'market_data.py'
        ).resolve(),
        'calibration_scope.py': (
            bundle.base_dir / 'core' / 'calibration_scope.py'
        ).resolve(),
        bundle.path.resolve().name: bundle.path.resolve(),
    }
    if methodology_ok:
        methodology_ok = methodology == {
            name: _file_sha256(path)
            for name, path in expected_paths.items()
        }
    check('methodology_files_unchanged', methodology_ok)
    stage7 = verify_stage7_baseline(conn, bundle)
    check(
        'stage7_contract_and_snapshot_exact',
        contract.get('stage7_contract_sha256')
        == stage7['contract_sha256'],
    )
    stage6c_run_id = int(contract.get('stage6c_run_id', -1))
    stage6c = _stage6c_run(conn, stage6c_run_id=stage6c_run_id)
    check(
        'stage6c_source_exact',
        contract.get('stage6c_panel_sha256') == stage6c['panel_sha256'],
    )
    factor_campaign, accepted_cells = verify_factor_campaign(
        factor_root,
        campaign_id=str(contract.get('factor_validation_campaign_id')),
    )
    check(
        'factor_campaign_exact',
        contract.get('factor_validation_registry_sha256')
        == factor_campaign['registry_sha256'],
    )

    candidate_rows = registry.get('candidates')
    candidate_ok = isinstance(candidate_rows, list)
    candidate_ids: set[str] = set()
    accepted_cell_ids = {str(row['cell_id']) for row in accepted_cells}
    baseline_weights = stage7_component_weights(bundle)
    settings = cfg_get(bundle.payload, 'stage8_calibration')
    cap = float(settings['component_weight_cap'])
    l1_cap = float(settings['weight_l1_turnover_cap'])
    prereg_hashes: list[str] = []
    if candidate_ok:
        for row in candidate_rows:
            try:
                payload = _candidate_payload(
                    scope_id=str(row['scope_id']),
                    candidate_kind=str(row['candidate_kind']),
                    core_weights=dict(row['core_weights']),
                    specialized_weights=dict(row['specialized_weights']),
                    parent_candidate_id=row.get('parent_candidate_id'),
                    shrinkage_alpha=float(row['shrinkage_alpha']),
                    evidence_references=tuple(row['evidence_references']),
                )
                digest = _sha256(payload)
                total = sum(float(value) for value in (
                    list(row['core_weights'].values())
                    + list(row['specialized_weights'].values())
                ))
                l1 = sum(
                    abs(float(row['core_weights'].get(name, 0.0)) - weight)
                    for name, weight in baseline_weights.items()
                )
                evidence_ok = (
                    not row['specialized_weights']
                    or set(row['evidence_references']).issubset(
                        accepted_cell_ids
                    )
                )
                valid = (
                    row['candidate_id'] == f's8_{digest[:20]}'
                    and row['preregistration_sha256'] == digest
                    and abs(total - 1.0) <= 1e-10
                    and max(
                        (float(value) for value in row['core_weights'].values()),
                        default=0.0,
                    ) <= cap + 1e-12
                    and (
                        row['candidate_kind'] == 'core_plus_specialized'
                        or l1 <= l1_cap + 1e-10
                    )
                    and evidence_ok
                )
            except (KeyError, TypeError, ValueError):
                valid = False
                digest = ''
            if not valid or str(row.get('candidate_id')) in candidate_ids:
                candidate_ok = False
            candidate_ids.add(str(row.get('candidate_id')))
            prereg_hashes.append(digest)
    check('candidate_preregistrations_exact', candidate_ok)
    check(
        'candidate_registry_hash_exact',
        registry.get('candidate_count') == len(candidate_ids)
        and registry.get('candidate_preregistration_sha256s')
        == prereg_hashes
        and registry.get('registry_sha256') == _sha256(prereg_hashes)
        and registry.get('registered_before_label_evaluation') is True,
    )

    split_without_hash = dict(split)
    observed_split_hash = split_without_hash.pop('split_sha256', None)
    check(
        'split_hash_exact',
        observed_split_hash == _sha256(split_without_hash),
    )
    split_blocks = [
        tuple(split.get(name, ()))
        for name in (
            'train_dates', 'first_embargo_dates', 'validation_dates',
            'second_embargo_dates', 'holdout_dates',
        )
    ]
    combined = tuple(value for block in split_blocks for value in block)
    excluded_dates = tuple(split.get('excluded_non_calibration_dates', ()))
    split_chronological = (
        combined == tuple(sorted(set(combined)))
        and excluded_dates == tuple(sorted(set(excluded_dates)))
        and set(combined).isdisjoint(excluded_dates)
        and len(split_blocks[1]) == int(split['embargo_panel_dates'])
        and len(split_blocks[3]) == int(split['embargo_panel_dates'])
        and set(split_blocks[0]).isdisjoint(split_blocks[2])
        and set(split_blocks[2]).isdisjoint(split_blocks[4])
    )
    check('chronological_split_and_embargo_exact', split_chronological)

    with (root / PANEL_FILE).open(
        'r', encoding='utf-8', newline=''
    ) as handle:
        panel_rows = [
            _parse_panel_csv_row(row) for row in csv.DictReader(handle)
        ]
    panel_hashes_ok = all(
        row['row_sha256'] == _panel_row_hash(row) for row in panel_rows
    )
    panel_identities = {
        (row['asof_date'], row['ticker']) for row in panel_rows
    }
    excluded_scope_tickers = set(expected_scope['excluded_tickers'])
    panel_tickers = {str(row['ticker']) for row in panel_rows}
    decision_scope = decision.get('panel_summary', {}).get(
        'calibration_scope', {}
    )
    check(
        'calibration_scope_exclusions_enforced',
        panel_tickers.isdisjoint(excluded_scope_tickers)
        and decision_scope.get('contract') == expected_scope
        and decision_scope.get('observed_excluded_tickers')
        == sorted(excluded_scope_tickers),
    )
    panel_dates = tuple(sorted({
        str(row['asof_date']) for row in panel_rows
    }))
    panel_sha = _sha256([row['row_sha256'] for row in panel_rows])
    prepared_panel = _prepared_panel(panel_rows)
    sector_baseline_row = next((
        row for row in candidate_rows
        if row.get('scope_id') == SECTOR_SCOPE
        and row.get('candidate_kind') == 'stage7_core_baseline'
    ), None) if isinstance(candidate_rows, list) else None
    expected_calibration_dates: tuple[str, ...] = ()
    expected_census: list[dict[str, Any]] = []
    if sector_baseline_row is not None:
        baseline_candidate = Candidate(
            candidate_id=str(sector_baseline_row['candidate_id']),
            scope_id=str(sector_baseline_row['scope_id']),
            candidate_kind=str(sector_baseline_row['candidate_kind']),
            core_weights=dict(sector_baseline_row['core_weights']),
            specialized_weights=dict(
                sector_baseline_row['specialized_weights']
            ),
            parent_candidate_id=sector_baseline_row.get(
                'parent_candidate_id'
            ),
            shrinkage_alpha=float(sector_baseline_row['shrinkage_alpha']),
            evidence_references=tuple(
                sector_baseline_row['evidence_references']
            ),
            preregistration_sha256=str(
                sector_baseline_row['preregistration_sha256']
            ),
        )
        selected_dates, expected_census = calibration_date_census(
            prepared_panel, baseline_candidate, bundle
        )
        expected_calibration_dates = tuple(selected_dates)
    check(
        'panel_rows_hash_sealed_and_unique',
        panel_hashes_ok and len(panel_identities) == len(panel_rows),
    )
    check(
        'panel_matches_split_and_decision',
        panel_dates == tuple(sorted(set(combined) | set(excluded_dates)))
        and split.get('panel_sha256') == panel_sha
        and decision.get('panel_summary', {}).get('panel_sha256') == panel_sha,
    )
    check(
        'calibration_dates_recomputed_from_frozen_baseline',
        combined == expected_calibration_dates
        and split.get('calibration_date_census') == expected_census
        and split.get('calibration_date_policy')
        == 'frozen_stage7_sector_baseline_rank_ready_cross_section',
    )
    check(
        'panel_is_research_only',
        all(
            row['sample_role'] == RESEARCH_SAMPLE_ROLE
            and int(row['calibration_eligible_flag']) <= int(
                row['membership_eligible_flag']
            )
            for row in panel_rows
        ),
    )

    with (root / RESULT_FILE).open(
        'r', encoding='utf-8', newline=''
    ) as handle:
        result_rows = list(csv.DictReader(handle))
    result_candidate_ids = {
        str(row['candidate_id']) for row in result_rows
    }
    check(
        'results_reference_only_registered_candidates',
        bool(result_rows)
        and result_candidate_ids.issubset(candidate_ids),
    )
    holdout_families = {
        str(row['family_id']) for row in result_rows
        if row['phase'] == 'holdout'
    }
    decision_families = {
        str(row['family_id']): row
        for row in decision.get('family_decisions', [])
    }
    check(
        'holdout_opening_is_governed',
        all(
            int(decision_families[family]['validation_gate_pass']) == 1
            and int(decision_families[family]['walk_forward_gate_pass']) == 1
            and int(decision_families[family]['holdout_opened']) == 1
            for family in holdout_families
        )
        and all(
            (
                int(row.get('holdout_opened', 0)) == 1
            ) == (family in holdout_families)
            for family, row in decision_families.items()
        ),
    )
    decision_without_hash = dict(decision)
    observed_decision_hash = decision_without_hash.pop(
        'decision_sha256', None
    )
    check(
        'decision_hash_exact',
        observed_decision_hash == _sha256(decision_without_hash),
    )
    check(
        'decision_cannot_self_promote',
        decision.get('oos_score_valid_flag') == 0
        and all(
            int(row.get('production_weight_change_allowed', 1)) == 0
            for row in decision.get('family_decisions', [])
        ),
    )
    check(
        'manifest_run_identity_exact',
        manifest.get('stage8_run_id') == decision.get('stage8_run_id')
        and manifest.get('contract_sha256') == observed_contract_hash
        and decision.get('candidate_registry_sha256')
        == registry.get('registry_sha256'),
    )
    return {
        'status': 'PASS' if not errors else 'FAIL',
        'stage8_run_id': decision.get('stage8_run_id'),
        'check_count': len(checks),
        'passed_check_count': sum(passed for _, passed in checks),
        'checks': [
            {'check': name, 'passed': passed} for name, passed in checks
        ],
        'errors': errors,
        'panel_row_count': len(panel_rows),
        'panel_date_count': len(panel_dates),
        'candidate_count': len(candidate_ids),
        'result_row_count': len(result_rows),
        'accepted_research_candidate_count': int(
            decision.get('accepted_research_candidate_count', 0)
        ),
        'research_verdict': decision.get('research_verdict'),
        'production_promotion_enabled': False,
        'portfolio_write_enabled': False,
        'stage7_weight_mutation_enabled': False,
    }
