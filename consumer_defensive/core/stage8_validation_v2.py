from __future__ import annotations

import math
import statistics
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, Iterable, Mapping, Sequence

from .config import ConfigBundle, cfg_get
from .stage8_calibration import Candidate, HORIZONS, SECTOR_SCOPE, _finite, _spearman


STAGE8_VALIDATION_V2 = 'consumer_defensive_stage8_same_sample_validation_v2'


@dataclass(frozen=True)
class CandidateScoreV2:
    score: float
    available_weight: float
    missing_weight: float
    frozen_sample_eligible: bool
    candidate_quality_gate_pass: bool


def score_candidate_same_sample(
    row: Mapping[str, Any],
    candidate: Candidate,
    bundle: ConfigBundle,
) -> CandidateScoreV2:
    """Score on the Stage 7 frozen sample without candidate-induced filtering.

    Stage 8 v1 correctly neutral-filled missing components, but then used the
    candidate's reweighted available/missing weights to decide eligibility.
    That allowed a challenger and its Stage 7 reference to be evaluated on
    different ticker/date samples.  V2 freezes eligibility to the label-blind
    Stage 7 ``calibration_eligible_flag``.  Candidate-weight data quality is
    retained as a diagnostic and may be used as a fail-closed constraint, but
    it never changes the comparison sample.
    """

    neutral = float(cfg_get(bundle.payload, 'stage7_scoring.neutral_score'))
    scores = row['_component_scores']
    quality = row['_component_quality']
    specialized_scores = row['_specialized_scores']
    weighted_score = 0.0
    available_weight = 0.0
    missing_weight = 0.0

    for name, weight in candidate.core_weights.items():
        score = _finite(scores.get(name))
        available = float(quality.get(name, 0.0)) > 0.0 and score is not None
        effective = score if available else neutral
        weighted_score += weight * min(100.0, max(0.0, float(effective)))
        if available:
            available_weight += weight
        else:
            missing_weight += weight

    for name, weight in candidate.specialized_weights.items():
        score = _finite(specialized_scores.get(name))
        effective = score if score is not None else neutral
        weighted_score += weight * min(100.0, max(0.0, float(effective)))
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
    return CandidateScoreV2(
        score=weighted_score,
        available_weight=available_weight,
        missing_weight=missing_weight,
        frozen_sample_eligible=int(row['calibration_eligible_flag']) == 1,
        candidate_quality_gate_pass=(
            available_weight >= minimum_quality
            and missing_weight <= maximum_missing
        ),
    )


def evaluate_candidate_same_sample(
    rows: Sequence[Mapping[str, Any]],
    dates: Sequence[str],
    candidate: Candidate,
    bundle: ConfigBundle,
    *,
    required_factor_ids: Sequence[str] = (),
) -> dict[str, Any]:
    """Evaluate a candidate on one frozen sample shared with its baseline."""

    settings = cfg_get(bundle.payload, 'stage8_calibration')
    requested = set(dates)
    scope_rows = [
        row
        for row in rows
        if (
            candidate.scope_id == SECTOR_SCOPE
            or str(row['cohort_id']) == candidate.scope_id
        )
        and str(row['asof_date']) in requested
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
    quality_passes = 0
    quality_observations = 0
    previous_top: set[str] | None = None
    date_details: list[dict[str, Any]] = []

    for as_of in dates:
        scored: list[tuple[float, Mapping[str, Any], CandidateScoreV2]] = []
        for row in rows_by_date.get(str(as_of), ()):
            result = score_candidate_same_sample(row, candidate, bundle)
            if result.frozen_sample_eligible:
                scored.append((result.score, row, result))
                quality_observations += 1
                quality_passes += int(result.candidate_quality_gate_pass)
        scored.sort(key=lambda item: (-item[0], str(item[1]['ticker'])))
        if len(scored) < minimum_cross_section:
            continue
        top_count = max(
            minimum_top,
            int(math.ceil(len(scored) * top_quantile)),
        )
        if top_count * 2 > len(scored):
            continue
        top = scored[:top_count]
        bottom = scored[-top_count:]
        top_tickers = {str(row['ticker']) for _, row, _ in top}
        turnover = 0.0
        if previous_top is not None:
            denominator = max(len(previous_top), len(top_tickers), 1)
            turnover = 1.0 - len(previous_top & top_tickers) / denominator
            turnovers.append(turnover)
        previous_top = top_tickers
        if candidate.scope_id == SECTOR_SCOPE:
            counts = Counter(str(row['cohort_id']) for _, row, _ in top)
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
            'candidate_quality_gate_pass_fraction': statistics.fmean(
                int(result.candidate_quality_gate_pass)
                for _, _, result in scored
            ),
        }
        for horizon in HORIZONS:
            target = f'forward_xlp_residual_return_{horizon}d'
            paired = [
                (score, float(row[target]))
                for score, row, _ in scored
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
                for _, row, _ in top
                if _finite(row[target]) is not None
            ]
            bottom_returns = [
                float(row[target])
                for _, row, _ in bottom
                if _finite(row[target]) is not None
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
            if ic_values[horizon]
            else None
        )
        for horizon in HORIZONS
    }
    mean_spread = {
        horizon: (
            statistics.fmean(spread_values[horizon])
            if spread_values[horizon]
            else None
        )
        for horizon in HORIZONS
    }
    objective = (
        sum(
            horizon_weights[horizon] * float(mean_ic[horizon])
            for horizon in HORIZONS
        )
        if complete
        else None
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
    quality_pass_fraction = (
        quality_passes / quality_observations
        if quality_observations
        else 0.0
    )
    quality_constraint_pass = quality_pass_fraction >= float(cfg_get(
        bundle.payload, 'scoring_features.minimum_rank_ready_fraction'
    ))
    return {
        'schema_version': STAGE8_VALIDATION_V2,
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
        'candidate_quality_constraint_pass': int(quality_constraint_pass),
        'constraint_pass': int(
            turnover_pass and concentration_pass and quality_constraint_pass
        ),
        'candidate_quality_gate_pass_fraction': quality_pass_fraction,
        'candidate_quality_observation_count': quality_observations,
        'sample_policy': 'frozen_stage7_calibration_eligible_sample',
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


def observation_freshness_status(
    *,
    as_of: str,
    observation_date: str | None,
    maximum_age_days: int | None,
) -> str:
    """Return a deterministic PIT freshness classification."""

    evaluation = date.fromisoformat(as_of)
    if not observation_date:
        return 'missing'
    observed = date.fromisoformat(observation_date[:10])
    if observed > evaluation:
        return 'future'
    if maximum_age_days is None:
        return 'fresh'
    if maximum_age_days < 0:
        raise ValueError('maximum_age_days cannot be negative')
    return (
        'fresh'
        if (evaluation - observed).days <= maximum_age_days
        else 'stale'
    )


def latest_fresh_row(
    rows: Iterable[Mapping[str, Any]],
    *,
    as_of: str,
    date_field: str,
    maximum_age_days: int | None,
) -> Mapping[str, Any] | None:
    """Select the latest row available and fresh at ``as_of``."""

    eligible = [
        row
        for row in rows
        if observation_freshness_status(
            as_of=as_of,
            observation_date=(
                None if row.get(date_field) is None else str(row[date_field])
            ),
            maximum_age_days=maximum_age_days,
        ) == 'fresh'
    ]
    if not eligible:
        return None
    return max(eligible, key=lambda row: str(row[date_field]))


def freshness_window_start(as_of: str, maximum_age_days: int | None) -> str | None:
    """Inclusive SQL lower bound for a configured freshness window."""

    if maximum_age_days is None:
        return None
    if maximum_age_days < 0:
        raise ValueError('maximum_age_days cannot be negative')
    return (date.fromisoformat(as_of) - timedelta(days=maximum_age_days)).isoformat()
