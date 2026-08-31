from __future__ import annotations

import bisect
import math
from typing import Any, Mapping, Sequence

from .config import ConfigBundle
from .stage8_calibration import Candidate, HORIZONS, _finite
from .stage8_validation_v2 import evaluate_candidate_same_sample


STAGE8_INDEPENDENT_EVIDENCE_V2 = (
    'consumer_defensive_stage8_independent_evidence_v2'
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
    """Resolve an exact entry/endpoint interval on the frozen calendar."""

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


def thin_nonoverlapping_horizon_observations(
    date_details: Sequence[Mapping[str, Any]],
    calendar: Sequence[str],
    *,
    entry_lag: int,
    horizon_sessions: int,
) -> list[dict[str, Any]]:
    """Greedily retain exact, chronologically non-overlapping endpoints."""

    candidates: list[dict[str, Any]] = []
    field = f'ic_{horizon_sessions}d'
    for detail in date_details:
        value = _finite(detail.get(field))
        if value is None:
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
            'ic': value,
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


def independent_horizon_evidence(
    evaluation: Mapping[str, Any],
    calendar: Sequence[str],
    *,
    entry_lag: int,
) -> dict[str, Any]:
    """Separate overlap-sensitive raw statistics from independent evidence."""

    details = evaluation.get('date_details')
    if not isinstance(details, list):
        raise ValueError('Evaluation date_details are required.')
    output: dict[str, Any] = {
        'schema_version': STAGE8_INDEPENDENT_EVIDENCE_V2,
    }
    for horizon in HORIZONS:
        raw = [
            _finite(row.get(f'ic_{horizon}d'))
            for row in details if isinstance(row, Mapping)
        ]
        raw_values = [value for value in raw if value is not None]
        raw_positive = sum(value > 0.0 for value in raw_values)
        independent = thin_nonoverlapping_horizon_observations(
            [row for row in details if isinstance(row, Mapping)],
            calendar,
            entry_lag=entry_lag,
            horizon_sessions=horizon,
        )
        independent_positive = sum(
            float(row['ic']) > 0.0 for row in independent
        )
        output.update({
            f'raw_date_count_{horizon}d': len(raw_values),
            f'raw_positive_ic_count_{horizon}d': raw_positive,
            f'raw_sign_pvalue_{horizon}d': (
                exact_one_sided_sign_pvalue(raw_positive, len(raw_values))
                if raw_values else None
            ),
            f'raw_overlap_sensitive_flag_{horizon}d': int(
                len(independent) < len(raw_values)
            ),
            f'effective_independent_date_count_{horizon}d': len(independent),
            f'effective_positive_ic_count_{horizon}d': independent_positive,
            f'effective_sign_pvalue_{horizon}d': (
                exact_one_sided_sign_pvalue(
                    independent_positive, len(independent)
                )
                if independent else None
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


def absolute_baseline_independent_evidence(
    rows: Sequence[Mapping[str, Any]],
    dates: Sequence[str],
    baseline: Candidate,
    bundle: ConfigBundle,
    *,
    calendar: Sequence[str],
    entry_lag: int,
) -> dict[str, Any]:
    evaluation = evaluate_candidate_same_sample(
        rows, dates, baseline, bundle
    )
    independent = independent_horizon_evidence(
        evaluation, calendar, entry_lag=entry_lag
    )
    return {
        **{
            key: value for key, value in evaluation.items()
            if key != 'date_details'
        },
        **independent,
        'validation_kind': (
            'absolute_frozen_baseline_calendar_independent_efficacy'
        ),
    }


def independent_evidence_gate(
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
        sign_p = _finite(evidence.get(
            f'effective_sign_pvalue_{horizon}d'
        ))
        if count < required:
            blockers.append(
                f'insufficient_independent_dates:{horizon}d={count}<{required}'
            )
        if sign_p is None or sign_p > maximum_sign_pvalue:
            blockers.append(f'independent_sign_test_failed:{horizon}d')
    return {
        'schema_version': STAGE8_INDEPENDENT_EVIDENCE_V2,
        'pass_flag': int(not blockers),
        'blockers': blockers,
    }
