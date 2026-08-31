from __future__ import annotations

import math
import statistics
from collections import Counter, defaultdict
from typing import Any, Mapping, Sequence

from .config import ConfigBundle, cfg_get
from .portfolio_turnover_v2 import (
    equal_weight_long_short_holdings,
    one_way_leg_turnover,
    trade_notional_turnover,
)
from .stage8_calibration import Candidate, HORIZONS, SECTOR_SCOPE, _finite, _spearman
from .stage8_validation_v2 import (
    CandidateScoreV2,
    score_candidate_same_sample,
)


STAGE8_VALIDATION_V3 = (
    'consumer_defensive_stage8_same_sample_validation_v3'
)


def _validate_schedule(
    dates: Sequence[str],
    decision_dates: Sequence[str] | None,
) -> tuple[list[str], set[str]]:
    schedule = [str(value) for value in dates]
    if schedule != sorted(set(schedule)):
        raise ValueError('Evaluation schedule dates must be sorted and unique.')
    decisions = set(
        schedule if decision_dates is None
        else (str(value) for value in decision_dates)
    )
    if not decisions.issubset(schedule):
        raise ValueError('Decision dates must be a subset of the full schedule.')
    return schedule, decisions


def evaluate_candidate_same_sample_v3(
    rows: Sequence[Mapping[str, Any]],
    dates: Sequence[str],
    candidate: Candidate,
    bundle: ConfigBundle,
    *,
    required_factor_ids: Sequence[str] = (),
    decision_dates: Sequence[str] | None = None,
    liquidate_final_holdings: bool = True,
) -> dict[str, Any]:
    """Evaluate one frozen sample with continuous long/short holdings state.

    ``dates`` is the complete chronological scoring/rebalance schedule used to
    build turnover state. ``decision_dates`` selects the phase whose IC,
    spread, and constraints are summarized. This prevents each phase from
    silently resetting to cash while still keeping target access phase-local.
    """

    schedule, decision_set = _validate_schedule(dates, decision_dates)
    settings = cfg_get(bundle.payload, 'stage8_calibration')
    requested = set(schedule)
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
    transaction_cost_rate = (
        float(settings['transaction_cost_bps']) / 10000.0
    )
    all_details: list[dict[str, Any]] = []
    previous_holdings: dict[str, float] | None = None
    previous_top: set[str] | None = None
    previous_bottom: set[str] | None = None
    last_valid_index: int | None = None
    last_detail: dict[str, Any] | None = None

    for index, as_of in enumerate(schedule):
        scored: list[tuple[float, Mapping[str, Any], CandidateScoreV2]] = []
        for row in rows_by_date.get(as_of, ()):
            result = score_candidate_same_sample(row, candidate, bundle)
            if result.frozen_sample_eligible:
                scored.append((result.score, row, result))
        scored.sort(key=lambda item: (-item[0], str(item[1]['ticker'])))
        top_count = max(
            minimum_top,
            int(math.ceil(len(scored) * top_quantile)),
        )
        valid = (
            len(scored) >= minimum_cross_section
            and top_count * 2 <= len(scored)
        )
        if not valid:
            if previous_holdings is not None and last_detail is not None:
                liquidation = trade_notional_turnover(previous_holdings, None)
                last_detail['gap_liquidation_turnover'] += liquidation
                last_detail['trade_notional_turnover'] += liquidation
                last_detail['transition_out_kind'] = 'liquidated_before_schedule_gap'
            previous_holdings = None
            last_valid_index = None
            continue

        top = scored[:top_count]
        bottom = scored[-top_count:]
        top_tickers = {str(row['ticker']) for _, row, _ in top}
        bottom_tickers = {str(row['ticker']) for _, row, _ in bottom}
        holdings = equal_weight_long_short_holdings(
            top_tickers, bottom_tickers, leg_gross=1.0
        )
        top_turnover = (
            0.0 if previous_top is None
            else one_way_leg_turnover(previous_top, top_tickers)
        )
        bottom_turnover = (
            0.0 if previous_bottom is None
            else one_way_leg_turnover(previous_bottom, bottom_tickers)
        )
        if previous_holdings is None:
            entry_turnover = trade_notional_turnover(None, holdings)
            transition_kind = (
                'initial_entry'
                if last_detail is None else 'reentry_after_schedule_gap'
            )
        elif last_valid_index == index - 1:
            entry_turnover = trade_notional_turnover(
                previous_holdings, holdings
            )
            transition_kind = 'direct_rebalance'
        else:
            # Defensive fallback. A normal gap clears state in the invalid-date
            # branch above, so this path should not be reachable.
            entry_turnover = (
                trade_notional_turnover(previous_holdings, None)
                + trade_notional_turnover(None, holdings)
            )
            transition_kind = 'liquidate_and_reenter_after_untracked_gap'
        if candidate.scope_id == SECTOR_SCOPE:
            counts = Counter(str(row['cohort_id']) for _, row, _ in top)
            cohort_share = max(counts.values()) / len(top)
        else:
            cohort_share = 1.0
        detail: dict[str, Any] = {
            'asof_date': as_of,
            'decision_date_flag': int(as_of in decision_set),
            'cross_section': len(scored),
            'top_count': top_count,
            'top_turnover': top_turnover,
            'bottom_turnover': bottom_turnover,
            'top_cohort_share': cohort_share,
            'candidate_quality_gate_pass_count': sum(
                int(result.candidate_quality_gate_pass)
                for _, _, result in scored
            ),
            'candidate_quality_observation_count': len(scored),
            'candidate_quality_gate_pass_fraction': statistics.fmean(
                int(result.candidate_quality_gate_pass)
                for _, _, result in scored
            ),
            'transition_kind': transition_kind,
            'transition_out_kind': '',
            'entry_rebalance_turnover': entry_turnover,
            'gap_liquidation_turnover': 0.0,
            'final_liquidation_turnover': 0.0,
            'trade_notional_turnover': entry_turnover,
            'long_gross': 1.0,
            'short_gross': 1.0,
        }
        for horizon in HORIZONS:
            target = f'forward_xlp_residual_return_{horizon}d'
            paired = [
                (score, float(row[target]))
                for score, row, _ in scored
                if _finite(row.get(target)) is not None
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
                if _finite(row.get(target)) is not None
            ]
            bottom_returns = [
                float(row[target])
                for _, row, _ in bottom
                if _finite(row.get(target)) is not None
            ]
            if (
                ic is None
                or len(top_returns) < minimum_top
                or len(bottom_returns) < minimum_top
            ):
                continue
            detail[f'ic_{horizon}d'] = ic
            detail[f'spread_gross_{horizon}d'] = (
                statistics.fmean(top_returns)
                - statistics.fmean(bottom_returns)
            )
        all_details.append(detail)
        previous_holdings = holdings
        previous_top = top_tickers
        previous_bottom = bottom_tickers
        last_valid_index = index
        last_detail = detail

    if (
        liquidate_final_holdings
        and previous_holdings is not None
        and last_detail is not None
    ):
        liquidation = trade_notional_turnover(previous_holdings, None)
        last_detail['final_liquidation_turnover'] += liquidation
        last_detail['trade_notional_turnover'] += liquidation
        last_detail['transition_out_kind'] = 'final_liquidation'

    for detail in all_details:
        transaction_cost = (
            float(detail['trade_notional_turnover']) * transaction_cost_rate
        )
        detail['transaction_cost'] = transaction_cost
        for horizon in HORIZONS:
            gross = _finite(detail.get(f'spread_gross_{horizon}d'))
            if gross is not None:
                detail[f'spread_net_{horizon}d'] = gross - transaction_cost

    details = [
        detail for detail in all_details
        if str(detail['asof_date']) in decision_set
    ]
    ic_values: dict[int, list[float]] = defaultdict(list)
    spread_values: dict[int, list[float]] = defaultdict(list)
    for detail in details:
        for horizon in HORIZONS:
            ic = _finite(detail.get(f'ic_{horizon}d'))
            spread = _finite(detail.get(f'spread_net_{horizon}d'))
            if ic is not None and spread is not None:
                ic_values[horizon].append(ic)
                spread_values[horizon].append(spread)
    minimum_dates = max(3, int(math.ceil(len(decision_set) * 0.5)))
    complete = all(
        len(ic_values[horizon]) >= minimum_dates for horizon in HORIZONS
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
    transition_details = [
        detail for detail in details
        if str(detail['transition_kind']) != 'initial_entry'
    ]
    average_top_turnover = (
        statistics.fmean(float(row['top_turnover']) for row in transition_details)
        if transition_details else 0.0
    )
    average_bottom_turnover = (
        statistics.fmean(
            float(row['bottom_turnover']) for row in transition_details
        )
        if transition_details else 0.0
    )
    average_trade_turnover = (
        statistics.fmean(
            float(row['trade_notional_turnover']) for row in details
        )
        if details else 0.0
    )
    average_cohort_share = (
        statistics.fmean(float(row['top_cohort_share']) for row in details)
        if details else 0.0
    )
    quality_passes = sum(
        int(row['candidate_quality_gate_pass_count']) for row in details
    )
    quality_observations = sum(
        int(row['candidate_quality_observation_count']) for row in details
    )
    quality_fraction = (
        quality_passes / quality_observations if quality_observations else 0.0
    )
    quality_constraint_pass = quality_fraction >= float(cfg_get(
        bundle.payload, 'scoring_features.minimum_rank_ready_fraction'
    ))
    turnover_pass = average_top_turnover <= float(
        settings['maximum_top_turnover']
    )
    concentration_pass = (
        candidate.scope_id != SECTOR_SCOPE
        or average_cohort_share <= float(settings['maximum_top_cohort_share'])
    )
    return {
        'schema_version': STAGE8_VALIDATION_V3,
        'candidate_id': candidate.candidate_id,
        'scope_id': candidate.scope_id,
        'candidate_kind': candidate.candidate_kind,
        'status': 'complete' if complete else 'inconclusive',
        'schedule_date_count': len(schedule),
        'requested_date_count': len(decision_set),
        'scored_date_count': len(details),
        'objective': objective,
        'average_top_turnover': average_top_turnover,
        'average_bottom_turnover': average_bottom_turnover,
        'average_trade_notional_turnover': average_trade_turnover,
        'total_transaction_cost': sum(
            float(row['transaction_cost']) for row in details
        ),
        'average_top_cohort_share': average_cohort_share,
        'turnover_cap_pass': int(turnover_pass),
        'cohort_concentration_cap_pass': int(concentration_pass),
        'candidate_quality_constraint_pass': int(quality_constraint_pass),
        'constraint_pass': int(
            turnover_pass and concentration_pass and quality_constraint_pass
        ),
        'candidate_quality_gate_pass_fraction': quality_fraction,
        'candidate_quality_observation_count': quality_observations,
        'sample_policy': 'frozen_stage7_calibration_eligible_sample',
        'turnover_cost_policy': (
            'continuous_full_schedule_signed_long_short_l1_trade_notional'
        ),
        'initial_entry_cost_included': 1,
        'final_liquidation_cost_included': int(liquidate_final_holdings),
        **{
            f'mean_ic_{horizon}d': mean_ic[horizon]
            for horizon in HORIZONS
        },
        **{
            f'mean_spread_net_{horizon}d': mean_spread[horizon]
            for horizon in HORIZONS
        },
        **{
            f'eligible_date_count_{horizon}d': len(ic_values[horizon])
            for horizon in HORIZONS
        },
        'date_details': details,
    }
