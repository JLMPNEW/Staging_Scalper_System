from __future__ import annotations

import bisect
import math
import statistics
from typing import Any, Mapping, Sequence

from .config import ConfigBundle
from .stage8_calibration import Candidate, HORIZONS, _finite
from .stage8_validation_v3 import evaluate_candidate_same_sample_v3


STAGE8_INDEPENDENT_EVIDENCE_V3 = (
    'consumer_defensive_stage8_independent_evidence_v3'
)


def exact_one_sided_sign_pvalue(positive_count: int, count: int) -> float:
    if count <= 0 or not 0 <= positive_count <= count:
        raise ValueError('Invalid sign-test counts.')
    return sum(
        math.comb(count, value) for value in range(positive_count, count + 1)
    ) / (2 ** count)


def horizon_interval(
    calendar: Sequence[str],
    *,
    as_of: str,
    entry_lag: int,
    horizon_sessions: int,
) -> tuple[int, int, str, str]:
    if list(calendar) != sorted(set(calendar)):
        raise ValueError('calendar must be sorted and unique')
    evaluation_index = bisect.bisect_left(calendar, as_of)
    if (
        evaluation_index >= len(calendar)
        or str(calendar[evaluation_index]) != as_of
    ):
        raise ValueError(f'Evaluation date is not a calendar session: {as_of}')
    entry_index = evaluation_index + entry_lag
    exit_index = entry_index + horizon_sessions
    if entry_index < 0 or exit_index >= len(calendar):
        raise ValueError(f'Horizon endpoint unavailable for {as_of}')
    return (
        entry_index,
        exit_index,
        str(calendar[entry_index]),
        str(calendar[exit_index]),
    )


def thin_nonoverlapping_horizon_observations_v3(
    date_details: Sequence[Mapping[str, Any]],
    calendar: Sequence[str],
    *,
    entry_lag: int,
    horizon_sessions: int,
) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    ic_field = f'ic_{horizon_sessions}d'
    spread_field = f'spread_net_{horizon_sessions}d'
    for detail in date_details:
        ic = _finite(detail.get(ic_field))
        spread = _finite(detail.get(spread_field))
        if ic is None or spread is None:
            continue
        as_of = str(detail['asof_date'])
        entry_index, exit_index, entry_date, exit_date = horizon_interval(
            calendar,
            as_of=as_of,
            entry_lag=entry_lag,
            horizon_sessions=horizon_sessions,
        )
        candidates.append({
            'asof_date': as_of,
            'entry_index': entry_index,
            'exit_index': exit_index,
            'entry_date': entry_date,
            'exit_date': exit_date,
            'ic': ic,
            'spread_net': spread,
        })
    candidates.sort(key=lambda row: (
        int(row['entry_index']), str(row['asof_date'])
    ))
    selected: list[dict[str, Any]] = []
    previous_exit: int | None = None
    for row in candidates:
        if previous_exit is None or int(row['entry_index']) >= previous_exit:
            selected.append(row)
            previous_exit = int(row['exit_index'])
    return selected


def independent_horizon_evidence_v3(
    evaluation: Mapping[str, Any],
    calendar: Sequence[str],
    *,
    entry_lag: int,
) -> dict[str, Any]:
    details = evaluation.get('date_details')
    if not isinstance(details, list):
        raise ValueError('Evaluation date_details are required.')
    output: dict[str, Any] = {
        'schema_version': STAGE8_INDEPENDENT_EVIDENCE_V3,
        'raw_summary_overlap_sensitive_flag': 1,
    }
    materialized = [row for row in details if isinstance(row, Mapping)]
    for horizon in HORIZONS:
        raw_pairs = [
            (
                _finite(row.get(f'ic_{horizon}d')),
                _finite(row.get(f'spread_net_{horizon}d')),
            )
            for row in materialized
        ]
        raw_values = [
            (ic, spread)
            for ic, spread in raw_pairs
            if ic is not None and spread is not None
        ]
        raw_positive = sum(ic > 0.0 for ic, _ in raw_values)
        independent = thin_nonoverlapping_horizon_observations_v3(
            materialized,
            calendar,
            entry_lag=entry_lag,
            horizon_sessions=horizon,
        )
        independent_positive = sum(
            float(row['ic']) > 0.0 for row in independent
        )
        output.update({
            f'raw_date_count_{horizon}d': len(raw_values),
            f'raw_mean_ic_{horizon}d': (
                statistics.fmean(ic for ic, _ in raw_values)
                if raw_values else None
            ),
            f'raw_mean_spread_net_{horizon}d': (
                statistics.fmean(spread for _, spread in raw_values)
                if raw_values else None
            ),
            f'raw_positive_ic_count_{horizon}d': raw_positive,
            f'raw_sign_pvalue_{horizon}d': (
                exact_one_sided_sign_pvalue(raw_positive, len(raw_values))
                if raw_values else None
            ),
            f'raw_overlap_sensitive_flag_{horizon}d': int(
                len(independent) < len(raw_values)
            ),
            f'effective_independent_date_count_{horizon}d': len(independent),
            f'effective_mean_ic_{horizon}d': (
                statistics.fmean(float(row['ic']) for row in independent)
                if independent else None
            ),
            f'effective_mean_spread_net_{horizon}d': (
                statistics.fmean(
                    float(row['spread_net']) for row in independent
                ) if independent else None
            ),
            f'effective_positive_ic_count_{horizon}d': independent_positive,
            f'effective_sign_pvalue_{horizon}d': (
                exact_one_sided_sign_pvalue(
                    independent_positive, len(independent)
                ) if independent else None
            ),
            f'effective_asof_dates_{horizon}d': [
                str(row['asof_date']) for row in independent
            ],
            f'effective_intervals_{horizon}d': [
                {
                    'entry_date': row['entry_date'],
                    'exit_date': row['exit_date'],
                }
                for row in independent
            ],
        })
    return output


def absolute_baseline_independent_evidence_v3(
    rows: Sequence[Mapping[str, Any]],
    dates: Sequence[str],
    baseline: Candidate,
    bundle: ConfigBundle,
    *,
    calendar: Sequence[str],
    entry_lag: int,
    schedule_dates: Sequence[str] | None = None,
) -> dict[str, Any]:
    evaluation = evaluate_candidate_same_sample_v3(
        rows,
        list(schedule_dates) if schedule_dates is not None else dates,
        baseline,
        bundle,
        decision_dates=dates,
        liquidate_final_holdings=True,
    )
    independent = independent_horizon_evidence_v3(
        evaluation, calendar, entry_lag=entry_lag
    )
    return {
        **{
            key: value for key, value in evaluation.items()
            if key != 'date_details'
        },
        **independent,
        'validation_kind': (
            'absolute_frozen_baseline_calendar_independent_efficacy_v3'
        ),
    }


def independent_evidence_gate_v3(
    evidence: Mapping[str, Any],
    *,
    minimum_independent_dates: Mapping[int, int],
    maximum_sign_pvalue: float,
    invariants: Mapping[str, bool],
) -> dict[str, Any]:
    blockers = [
        f'invariant_failed:{name}'
        for name, value in sorted(invariants.items()) if not bool(value)
    ]
    for horizon in HORIZONS:
        required = int(minimum_independent_dates[horizon])
        count = int(evidence.get(
            f'effective_independent_date_count_{horizon}d', 0
        ))
        mean_ic = _finite(evidence.get(f'effective_mean_ic_{horizon}d'))
        mean_spread = _finite(evidence.get(
            f'effective_mean_spread_net_{horizon}d'
        ))
        sign_p = _finite(evidence.get(
            f'effective_sign_pvalue_{horizon}d'
        ))
        if count < required:
            blockers.append(
                f'insufficient_independent_dates:{horizon}d={count}<{required}'
            )
        if mean_ic is None or mean_ic <= 0.0:
            blockers.append(f'nonpositive_independent_mean_ic:{horizon}d')
        if mean_spread is None or mean_spread <= 0.0:
            blockers.append(
                f'nonpositive_independent_mean_spread_net:{horizon}d'
            )
        if sign_p is None or sign_p > maximum_sign_pvalue:
            blockers.append(f'independent_sign_test_failed:{horizon}d')
    return {
        'schema_version': STAGE8_INDEPENDENT_EVIDENCE_V3,
        'pass_flag': int(not blockers),
        'blockers': blockers,
    }
