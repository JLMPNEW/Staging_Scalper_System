from __future__ import annotations

import hashlib
import json
import math
import re
import statistics
from collections import Counter, defaultdict
from datetime import date
from typing import Any, Mapping, Sequence

from .config import ConfigBundle, cfg_get
from .portfolio_turnover_v2 import (
    equal_weight_long_short_holdings,
    one_way_leg_turnover,
    trade_notional_turnover,
)
from .stage8_calibration import Candidate, SECTOR_SCOPE, _finite, _spearman
from .stage8_independent_evidence_v2 import exact_one_sided_sign_pvalue
from .stage8_monthly_target_v2 import (
    MONTHLY_TARGET_FIELD,
    build_next_rebalance_target_panel,
    fail_closed_monthly_absolute_gate,
)
from .stage8_validation_v2 import score_candidate_same_sample


STAGE8_MONTHLY_TARGET_V3 = 'consumer_defensive_stage8_monthly_target_v3'


def monthly_plan_sha256(plan: Mapping[str, Any]) -> str:
    payload = {
        str(key): value for key, value in plan.items()
        if str(key) != 'plan_sha256'
    }
    canonical = json.dumps(
        payload,
        sort_keys=True,
        separators=(',', ':'),
        ensure_ascii=True,
        allow_nan=False,
    )
    return hashlib.sha256(canonical.encode('utf-8')).hexdigest()


def _is_sha256(value: Any) -> bool:
    return re.fullmatch(r'[0-9a-fA-F]{64}', str(value)) is not None


def _split_dates_are_bound(plan: Mapping[str, Any]) -> bool:
    names = (
        'train_dates',
        'first_embargo_dates',
        'validation_dates',
        'second_embargo_dates',
        'holdout_dates',
    )
    partitions: list[list[str]] = []
    try:
        for name in names:
            raw = plan[name]
            if not isinstance(raw, (list, tuple)):
                return False
            values = [str(value) for value in raw]
            if name in {'train_dates', 'validation_dates', 'holdout_dates'}:
                if not values:
                    return False
            if values != sorted(set(values)):
                return False
            for value in values:
                date.fromisoformat(value)
            partitions.append(values)
    except (KeyError, TypeError, ValueError):
        return False
    flattened = [value for partition in partitions for value in partition]
    return flattened == sorted(set(flattened))


def validate_preregistered_monthly_plan_v3(
    plan: Mapping[str, Any],
) -> dict[str, Any]:
    required = {
        'plan_id', 'candidate_registry_sha256', 'split_manifest_sha256',
        'plan_sha256', 'target_field', 'scoring_frequency',
        'rebalance_frequency', 'primary_objective', 'holdout_provenance',
        'registered_before_target_access', 'holdout_sealed',
        'legacy_holdout_reuse_allowed', 'train_dates',
        'first_embargo_dates', 'validation_dates',
        'second_embargo_dates', 'holdout_dates',
    }
    missing = sorted(required - set(plan))
    if missing:
        raise ValueError(f'Monthly target plan missing fields: {missing}')
    checks = {
        'registered_before_target_access': (
            plan['registered_before_target_access'] is True
        ),
        'target_field': str(plan['target_field']) == MONTHLY_TARGET_FIELD,
        'scoring_frequency': str(plan['scoring_frequency']) == 'monthly',
        'rebalance_frequency': str(plan['rebalance_frequency']) == 'monthly',
        'primary_objective': str(plan['primary_objective']) == 'mean_rank_ic',
        'holdout_provenance': str(plan['holdout_provenance']) in {
            'fresh_forward_oos', 'new_outer_holdout'
        },
        'holdout_sealed': plan['holdout_sealed'] is True,
        'legacy_holdout_reuse_prohibited': (
            plan['legacy_holdout_reuse_allowed'] is False
        ),
        'candidate_registry_bound': _is_sha256(
            plan['candidate_registry_sha256']
        ),
        'split_manifest_bound': _is_sha256(plan['split_manifest_sha256']),
        'split_dates_bound': _split_dates_are_bound(plan),
        'plan_self_hash_bound': (
            _is_sha256(plan['plan_sha256'])
            and str(plan['plan_sha256']).lower() == monthly_plan_sha256(plan)
        ),
    }
    failed = sorted(name for name, value in checks.items() if not value)
    if failed:
        raise ValueError(
            f'Invalid preregistered monthly target plan: {failed}'
        )
    return {
        **dict(plan),
        'schema_version': STAGE8_MONTHLY_TARGET_V3,
        'plan_validation_pass_flag': 1,
    }


def evaluate_monthly_candidate_same_sample_v3(
    rows: Sequence[Mapping[str, Any]],
    dates: Sequence[str],
    candidate: Candidate,
    bundle: ConfigBundle,
    *,
    required_factor_ids: Sequence[str] = (),
    decision_dates: Sequence[str] | None = None,
    liquidate_final_holdings: bool = True,
) -> dict[str, Any]:
    """Evaluate monthly targets using full-schedule long/short turnover state."""

    schedule = [str(value) for value in dates]
    if schedule != sorted(set(schedule)):
        raise ValueError('Evaluation schedule dates must be sorted and unique.')
    decisions = set(
        schedule if decision_dates is None
        else (str(value) for value in decision_dates)
    )
    if not decisions.issubset(schedule):
        raise ValueError('Decision dates must be a subset of the full schedule.')
    settings = cfg_get(bundle.payload, 'stage8_calibration')
    by_date: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in rows:
        if (
            str(row['asof_date']) in set(schedule)
            and (
                candidate.scope_id == SECTOR_SCOPE
                or str(row['cohort_id']) == candidate.scope_id
            )
            and all(
                int(row['_specialized_applicability'].get(factor_id, 0)) == 1
                for factor_id in required_factor_ids
            )
        ):
            by_date[str(row['asof_date'])].append(row)
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
        scored: list[tuple[float, Mapping[str, Any], Any]] = []
        for row in by_date.get(as_of, ()):
            result = score_candidate_same_sample(row, candidate, bundle)
            target = _finite(row.get(MONTHLY_TARGET_FIELD))
            if result.frozen_sample_eligible and target is not None:
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
            entry_turnover = (
                trade_notional_turnover(previous_holdings, None)
                + trade_notional_turnover(None, holdings)
            )
            transition_kind = 'liquidate_and_reenter_after_untracked_gap'
        paired = [
            (score, float(row[MONTHLY_TARGET_FIELD]))
            for score, row, _ in scored
        ]
        ic = _spearman(
            [value[0] for value in paired],
            [value[1] for value in paired],
        )
        if ic is None:
            continue
        gross_spread = (
            statistics.fmean(
                float(row[MONTHLY_TARGET_FIELD]) for _, row, _ in top
            )
            - statistics.fmean(
                float(row[MONTHLY_TARGET_FIELD]) for _, row, _ in bottom
            )
        )
        if candidate.scope_id == SECTOR_SCOPE:
            counts = Counter(str(row['cohort_id']) for _, row, _ in top)
            cohort_share = max(counts.values()) / len(top)
        else:
            cohort_share = 1.0
        detail = {
            'asof_date': as_of,
            'decision_date_flag': int(as_of in decisions),
            'cross_section': len(scored),
            'top_count': top_count,
            'top_turnover': top_turnover,
            'bottom_turnover': bottom_turnover,
            'top_cohort_share': cohort_share,
            'monthly_rank_ic': ic,
            'monthly_top_bottom_spread_gross': gross_spread,
            'candidate_quality_gate_pass_count': sum(
                int(result.candidate_quality_gate_pass)
                for _, _, result in scored
            ),
            'candidate_quality_observation_count': len(scored),
            'transition_kind': transition_kind,
            'transition_out_kind': '',
            'entry_rebalance_turnover': entry_turnover,
            'gap_liquidation_turnover': 0.0,
            'final_liquidation_turnover': 0.0,
            'trade_notional_turnover': entry_turnover,
            'long_gross': 1.0,
            'short_gross': 1.0,
        }
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
        cost = float(detail['trade_notional_turnover']) * transaction_cost_rate
        detail['transaction_cost'] = cost
        detail['monthly_top_bottom_spread_net'] = (
            float(detail['monthly_top_bottom_spread_gross']) - cost
        )
    details = [
        detail for detail in all_details
        if str(detail['asof_date']) in decisions
    ]
    minimum_dates = max(3, int(math.ceil(len(decisions) * 0.5)))
    complete = len(details) >= minimum_dates
    transitions = [
        row for row in details
        if str(row['transition_kind']) != 'initial_entry'
    ]
    average_top_turnover = (
        statistics.fmean(float(row['top_turnover']) for row in transitions)
        if transitions else 0.0
    )
    average_bottom_turnover = (
        statistics.fmean(float(row['bottom_turnover']) for row in transitions)
        if transitions else 0.0
    )
    average_cohort_share = (
        statistics.fmean(float(row['top_cohort_share']) for row in details)
        if details else 0.0
    )
    quality_passes = sum(
        int(row['candidate_quality_gate_pass_count']) for row in details
    )
    quality_count = sum(
        int(row['candidate_quality_observation_count']) for row in details
    )
    quality_fraction = quality_passes / quality_count if quality_count else 0.0
    quality_pass = quality_fraction >= float(cfg_get(
        bundle.payload, 'scoring_features.minimum_rank_ready_fraction'
    ))
    turnover_pass = average_top_turnover <= float(
        settings['maximum_top_turnover']
    )
    concentration_pass = (
        candidate.scope_id != SECTOR_SCOPE
        or average_cohort_share <= float(settings['maximum_top_cohort_share'])
    )
    ics = [float(row['monthly_rank_ic']) for row in details]
    spreads = [
        float(row['monthly_top_bottom_spread_net']) for row in details
    ]
    positive = sum(value > 0.0 for value in ics)
    return {
        'schema_version': STAGE8_MONTHLY_TARGET_V3,
        'candidate_id': candidate.candidate_id,
        'scope_id': candidate.scope_id,
        'candidate_kind': candidate.candidate_kind,
        'target_field': MONTHLY_TARGET_FIELD,
        'status': 'complete' if complete else 'inconclusive',
        'schedule_date_count': len(schedule),
        'requested_date_count': len(decisions),
        'eligible_date_count': len(details),
        'objective': statistics.fmean(ics) if complete else None,
        'mean_monthly_rank_ic': statistics.fmean(ics) if ics else None,
        'mean_monthly_top_bottom_spread_net': (
            statistics.fmean(spreads) if spreads else None
        ),
        'positive_ic_date_count': positive,
        'ic_sign_pvalue': (
            exact_one_sided_sign_pvalue(positive, len(ics)) if ics else None
        ),
        'average_top_turnover': average_top_turnover,
        'average_bottom_turnover': average_bottom_turnover,
        'average_trade_notional_turnover': (
            statistics.fmean(
                float(row['trade_notional_turnover']) for row in details
            ) if details else 0.0
        ),
        'total_transaction_cost': sum(
            float(row['transaction_cost']) for row in details
        ),
        'average_top_cohort_share': average_cohort_share,
        'candidate_quality_gate_pass_fraction': quality_fraction,
        'candidate_quality_constraint_pass': int(quality_pass),
        'turnover_cap_pass': int(turnover_pass),
        'cohort_concentration_cap_pass': int(concentration_pass),
        'constraint_pass': int(
            quality_pass and turnover_pass and concentration_pass
        ),
        'sample_policy': 'frozen_baseline_same_sample',
        'turnover_cost_policy': (
            'continuous_full_schedule_signed_long_short_l1_trade_notional'
        ),
        'initial_entry_cost_included': 1,
        'final_liquidation_cost_included': int(liquidate_final_holdings),
        'date_details': details,
    }


__all__ = [
    'MONTHLY_TARGET_FIELD',
    'build_next_rebalance_target_panel',
    'evaluate_monthly_candidate_same_sample_v3',
    'fail_closed_monthly_absolute_gate',
    'monthly_plan_sha256',
    'validate_preregistered_monthly_plan_v3',
]
