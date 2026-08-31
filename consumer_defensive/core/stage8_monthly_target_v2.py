from __future__ import annotations

import math
import statistics
from collections import Counter, defaultdict
from typing import Any, Callable, Mapping, Sequence

from .config import ConfigBundle, cfg_get
from .stage8_calibration import Candidate, SECTOR_SCOPE, _finite, _spearman
from .stage8_independent_evidence_v2 import exact_one_sided_sign_pvalue
from .stage8_validation_v2 import score_candidate_same_sample


STAGE8_MONTHLY_TARGET_V2 = 'consumer_defensive_stage8_monthly_target_v2'
MONTHLY_TARGET_FIELD = 'forward_xlp_residual_return_next_rebalance'


def validate_preregistered_monthly_plan(
    plan: Mapping[str, Any],
) -> dict[str, Any]:
    """Validate the one primary target before any target value is opened."""

    required = {
        'plan_id', 'candidate_registry_sha256', 'target_field',
        'scoring_frequency', 'rebalance_frequency', 'primary_objective',
        'holdout_provenance', 'registered_before_target_access',
        'holdout_sealed', 'legacy_holdout_reuse_allowed',
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
        'candidate_registry_bound': bool(
            str(plan['candidate_registry_sha256'])
        ),
    }
    failed = sorted(name for name, value in checks.items() if not value)
    if failed:
        raise ValueError(
            f'Invalid preregistered monthly target plan: {failed}'
        )
    return {
        **dict(plan),
        'schema_version': STAGE8_MONTHLY_TARGET_V2,
        'plan_validation_pass_flag': 1,
    }


def _total_return(
    prices: Mapping[str, float],
    *,
    entry_date: str,
    exit_date: str,
) -> float | None:
    entry = _finite(prices.get(entry_date))
    exit_value = _finite(prices.get(exit_date))
    if entry is None or exit_value is None or entry <= 0.0 or exit_value <= 0.0:
        return None
    return exit_value / entry - 1.0


def build_next_rebalance_target_panel(
    panel_rows: Sequence[Mapping[str, Any]],
    schedule: Sequence[Mapping[str, Any]],
    *,
    prices_by_ticker: Mapping[str, Mapping[str, float]],
    xlp_prices: Mapping[str, float],
    terminal_total_return_resolver: (
        Callable[[Mapping[str, Any], Mapping[str, Any]], float | None]
        | None
    ) = None,
) -> list[dict[str, Any]]:
    """Attach one non-overlapping next-month-rebalance target to each row."""

    by_date = {str(row['asof_date']): row for row in schedule}
    if len(by_date) != len(schedule):
        raise ValueError('Duplicate monthly schedule dates.')
    output: list[dict[str, Any]] = []
    for source in panel_rows:
        row = dict(source)
        schedule_row = by_date.get(str(row['asof_date']))
        target: float | None = None
        status = 'schedule_missing'
        source_kind = ''
        if schedule_row is not None:
            entry_date = str(schedule_row['entry_date'])
            exit_date = str(schedule_row['exit_date'])
            benchmark = _total_return(
                xlp_prices, entry_date=entry_date, exit_date=exit_date
            )
            ticker_return = _total_return(
                prices_by_ticker.get(str(row['ticker']), {}),
                entry_date=entry_date,
                exit_date=exit_date,
            )
            if ticker_return is not None:
                source_kind = 'adjusted_price_total_return'
            elif terminal_total_return_resolver is not None:
                ticker_return = _finite(
                    terminal_total_return_resolver(row, schedule_row)
                )
                if ticker_return is not None:
                    source_kind = 'reconciled_terminal_total_return'
            if benchmark is None:
                status = 'benchmark_return_missing'
            elif ticker_return is None:
                status = 'ticker_return_missing'
            else:
                target = ticker_return - benchmark
                status = 'complete'
            row['target_entry_date'] = entry_date
            row['target_exit_date'] = exit_date
        row[MONTHLY_TARGET_FIELD] = target
        row['next_rebalance_target_status'] = status
        row['next_rebalance_target_source'] = source_kind
        output.append(row)
    return output


def evaluate_monthly_candidate_same_sample(
    rows: Sequence[Mapping[str, Any]],
    dates: Sequence[str],
    candidate: Candidate,
    bundle: ConfigBundle,
    *,
    required_factor_ids: Sequence[str] = (),
) -> dict[str, Any]:
    """Evaluate the same monthly target used by the investable backtest."""

    settings = cfg_get(bundle.payload, 'stage8_calibration')
    requested = set(dates)
    by_date: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in rows:
        if (
            str(row['asof_date']) in requested
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
    transaction_cost = float(settings['transaction_cost_bps']) / 10000.0
    previous_top: set[str] | None = None
    ics: list[float] = []
    spreads: list[float] = []
    turnovers: list[float] = []
    cohort_shares: list[float] = []
    quality_passes = 0
    quality_count = 0
    details: list[dict[str, Any]] = []
    for as_of in dates:
        scored: list[tuple[float, Mapping[str, Any], Any]] = []
        for row in by_date.get(str(as_of), ()):
            result = score_candidate_same_sample(row, candidate, bundle)
            target = _finite(row.get(MONTHLY_TARGET_FIELD))
            if result.frozen_sample_eligible and target is not None:
                scored.append((result.score, row, result))
                quality_count += 1
                quality_passes += int(result.candidate_quality_gate_pass)
        scored.sort(key=lambda item: (-item[0], str(item[1]['ticker'])))
        if len(scored) < minimum_cross_section:
            continue
        top_count = max(minimum_top, int(math.ceil(
            len(scored) * top_quantile
        )))
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
        spread = (
            statistics.fmean(
                float(row[MONTHLY_TARGET_FIELD]) for _, row, _ in top
            )
            - statistics.fmean(
                float(row[MONTHLY_TARGET_FIELD]) for _, row, _ in bottom
            )
            - 2.0 * turnover * transaction_cost
        )
        if candidate.scope_id == SECTOR_SCOPE:
            counts = Counter(str(row['cohort_id']) for _, row, _ in top)
            cohort_share = max(counts.values()) / len(top)
        else:
            cohort_share = 1.0
        cohort_shares.append(cohort_share)
        ics.append(ic)
        spreads.append(spread)
        details.append({
            'asof_date': as_of,
            'cross_section': len(scored),
            'top_count': top_count,
            'top_turnover': turnover,
            'top_cohort_share': cohort_share,
            'monthly_rank_ic': ic,
            'monthly_top_bottom_spread_net': spread,
        })
    minimum_dates = max(3, int(math.ceil(len(dates) * 0.5)))
    complete = len(ics) >= minimum_dates
    average_turnover = statistics.fmean(turnovers) if turnovers else 0.0
    average_cohort_share = (
        statistics.fmean(cohort_shares) if cohort_shares else 0.0
    )
    quality_fraction = quality_passes / quality_count if quality_count else 0.0
    quality_pass = quality_fraction >= float(cfg_get(
        bundle.payload, 'scoring_features.minimum_rank_ready_fraction'
    ))
    turnover_pass = average_turnover <= float(
        settings['maximum_top_turnover']
    )
    concentration_pass = (
        candidate.scope_id != SECTOR_SCOPE
        or average_cohort_share <= float(settings['maximum_top_cohort_share'])
    )
    positive = sum(value > 0.0 for value in ics)
    return {
        'schema_version': STAGE8_MONTHLY_TARGET_V2,
        'candidate_id': candidate.candidate_id,
        'scope_id': candidate.scope_id,
        'candidate_kind': candidate.candidate_kind,
        'target_field': MONTHLY_TARGET_FIELD,
        'status': 'complete' if complete else 'inconclusive',
        'requested_date_count': len(dates),
        'eligible_date_count': len(ics),
        'objective': statistics.fmean(ics) if complete else None,
        'mean_monthly_rank_ic': statistics.fmean(ics) if ics else None,
        'mean_monthly_top_bottom_spread_net': (
            statistics.fmean(spreads) if spreads else None
        ),
        'positive_ic_date_count': positive,
        'ic_sign_pvalue': (
            exact_one_sided_sign_pvalue(positive, len(ics))
            if ics else None
        ),
        'average_top_turnover': average_turnover,
        'average_top_cohort_share': average_cohort_share,
        'candidate_quality_gate_pass_fraction': quality_fraction,
        'candidate_quality_constraint_pass': int(quality_pass),
        'turnover_cap_pass': int(turnover_pass),
        'cohort_concentration_cap_pass': int(concentration_pass),
        'constraint_pass': int(
            quality_pass and turnover_pass and concentration_pass
        ),
        'sample_policy': 'frozen_baseline_same_sample',
        'date_details': details,
    }


def fail_closed_monthly_absolute_gate(
    evidence: Mapping[str, Any],
    *,
    minimum_dates: int,
    maximum_sign_pvalue: float,
    invariants: Mapping[str, bool],
) -> dict[str, Any]:
    blockers = [
        f'invariant_failed:{name}'
        for name, value in sorted(invariants.items()) if not bool(value)
    ]
    count = int(evidence.get('eligible_date_count') or 0)
    mean_ic = _finite(evidence.get('mean_monthly_rank_ic'))
    mean_spread = _finite(evidence.get(
        'mean_monthly_top_bottom_spread_net'
    ))
    sign_p = _finite(evidence.get('ic_sign_pvalue'))
    if count < minimum_dates:
        blockers.append(f'insufficient_monthly_dates={count}<{minimum_dates}')
    if mean_ic is None or mean_ic <= 0.0:
        blockers.append('nonpositive_monthly_rank_ic')
    if mean_spread is None or mean_spread <= 0.0:
        blockers.append('nonpositive_monthly_top_bottom_spread')
    if sign_p is None or sign_p > maximum_sign_pvalue:
        blockers.append('monthly_sign_test_failed')
    if int(evidence.get('constraint_pass') or 0) != 1:
        blockers.append('portfolio_or_quality_constraint_failed')
    return {
        'schema_version': STAGE8_MONTHLY_TARGET_V2,
        'limited_production_ready_flag': int(not blockers),
        'action': (
            'eligible_for_separately_authorized_limited_production_review'
            if not blockers else 'remain_shadow_fail_closed'
        ),
        'blockers': blockers,
        'portfolio_write_enabled': False,
        'production_promotion_enabled': False,
    }
