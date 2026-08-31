from __future__ import annotations

import bisect
import csv
import gzip
import hashlib
import json
import math
import statistics
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .stage8_calibration import Candidate


STAGE9_BACKTEST_V2 = 'consumer_defensive_stage9_monthly_backtest_v2'
EVIDENCE_ROLES = frozenset({'validation', 'holdout'})
NON_DECISION_ROLES = frozenset({'train', 'embargo_1', 'embargo_2'})


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


def _finite(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _split_roles(split: Mapping[str, Any]) -> dict[str, str]:
    output: dict[str, str] = {}
    for key, role in (
        ('train_dates', 'train'),
        ('first_embargo_dates', 'embargo_1'),
        ('validation_dates', 'validation'),
        ('second_embargo_dates', 'embargo_2'),
        ('holdout_dates', 'holdout'),
    ):
        for raw in split.get(key, []):
            value = str(raw)
            if value in output:
                raise RuntimeError(f'Duplicate split date: {value}')
            output[value] = role
    return output


def build_monthly_rebalance_schedule(
    split: Mapping[str, Any],
    calendar: Sequence[str],
    *,
    entry_lag: int,
) -> list[dict[str, Any]]:
    """Use every complete monthly signal until the next monthly rebalance.

    This replaces the fixed-21-session greedy schedule that discarded months.
    The last signal exits at the first session after the following true
    month-end, provided that session exists in the supplied frozen calendar.
    """

    if entry_lag < 0:
        raise ValueError('entry_lag cannot be negative')
    sessions = list(calendar)
    if sessions != sorted(set(sessions)):
        raise ValueError('calendar must be sorted and unique')
    roles = _split_roles(split)
    census = [
        str(row['asof_date'])
        for row in split.get('calibration_date_census', [])
        if int(row.get('included_flag') or 0) == 1
    ]
    if census != sorted(roles):
        raise RuntimeError('Calibration census and chronological split disagree.')
    month_ends: dict[str, str] = {}
    for session in sessions:
        month_ends[session[:7]] = session
    for as_of in census:
        if month_ends.get(as_of[:7]) != as_of:
            raise RuntimeError(
                f'Incomplete month-end signal is prohibited: {as_of}'
            )
    all_month_ends = sorted(month_ends.values())
    output: list[dict[str, Any]] = []
    for as_of in census:
        evaluation_index = bisect.bisect_left(sessions, as_of)
        entry_index = evaluation_index + entry_lag
        if entry_index >= len(sessions):
            raise RuntimeError(f'Entry unavailable for {as_of}')
        month_end_index = bisect.bisect_right(all_month_ends, as_of)
        if month_end_index >= len(all_month_ends):
            raise RuntimeError(f'Next monthly rebalance unavailable for {as_of}')
        next_month_end = all_month_ends[month_end_index]
        next_evaluation_index = bisect.bisect_left(sessions, next_month_end)
        exit_index = next_evaluation_index + entry_lag
        if exit_index >= len(sessions):
            raise RuntimeError(f'Next monthly entry unavailable for {as_of}')
        row = {
            'asof_date': as_of,
            'split_role': roles[as_of],
            'entry_date': sessions[entry_index],
            'exit_date': sessions[exit_index],
            'selected_rebalance_flag': 1,
            'holding_session_count': exit_index - entry_index,
            'return_target': 'next_rebalance_total_return',
        }
        row['schedule_row_sha256'] = _sha256(row)
        output.append(row)
    return output


def next_rebalance_return(
    prices: Mapping[str, float],
    *,
    entry_date: str,
    exit_date: str,
) -> float:
    entry = _finite(prices.get(entry_date))
    exit_value = _finite(prices.get(exit_date))
    if entry is None or exit_value is None or entry <= 0.0 or exit_value <= 0.0:
        raise ValueError('Positive entry and exit prices are required.')
    return exit_value / entry - 1.0


def next_rebalance_xlp_relative_return(
    ticker_prices: Mapping[str, float],
    xlp_prices: Mapping[str, float],
    *,
    entry_date: str,
    exit_date: str,
) -> float:
    return next_rebalance_return(
        ticker_prices, entry_date=entry_date, exit_date=exit_date
    ) - next_rebalance_return(
        xlp_prices, entry_date=entry_date, exit_date=exit_date
    )


def validate_primary_target_contract(
    *,
    stage8_primary_target: str,
    stage9_return_target: str,
    scoring_frequency: str,
    rebalance_frequency: str,
) -> dict[str, Any]:
    """Fail target/cadence identity instead of comparing unlike objectives."""

    target_match = stage8_primary_target == stage9_return_target
    cadence_match = (
        scoring_frequency == rebalance_frequency == 'monthly'
    )
    return {
        'schema_version': STAGE9_BACKTEST_V2,
        'stage8_primary_target': stage8_primary_target,
        'stage9_return_target': stage9_return_target,
        'target_match_flag': int(target_match),
        'scoring_frequency': scoring_frequency,
        'rebalance_frequency': rebalance_frequency,
        'cadence_match_flag': int(cadence_match),
        'pass_flag': int(target_match and cadence_match),
    }


def _registry_candidates(
    registry: Mapping[str, Any],
) -> list[Mapping[str, Any]]:
    raw = registry.get('candidates')
    if not isinstance(raw, list):
        raise ValueError('Stage 8 candidate registry is missing candidates.')
    return [row for row in raw if isinstance(row, Mapping)]


def allowed_holdout_candidate_ids(
    stage8_decision: Mapping[str, Any],
    registry: Mapping[str, Any],
    *,
    baseline_absolute_plan: Mapping[str, Any] | None = None,
) -> set[str]:
    """Return only candidates whose sealed holdout was explicitly opened."""

    candidates = _registry_candidates(registry)
    baseline_by_scope = {
        str(row['scope_id']): str(row['candidate_id'])
        for row in candidates
        if str(row.get('candidate_kind')) == 'stage7_core_baseline'
    }
    allowed: set[str] = set()
    for family in stage8_decision.get('family_decisions', []):
        if not isinstance(family, Mapping):
            continue
        if int(family.get('holdout_opened') or 0) != 1:
            continue
        selected = str(family.get('selected_candidate_id') or '')
        scope = str(family.get('scope_id') or '')
        if selected:
            allowed.add(selected)
        baseline = baseline_by_scope.get(scope)
        if baseline:
            allowed.add(baseline)
    if baseline_absolute_plan is not None:
        if (
            baseline_absolute_plan.get('registered_before_holdout_access')
            is not True
        ):
            raise ValueError(
                'Absolute-baseline holdout plan was not preregistered.'
            )
        for candidate_id in baseline_absolute_plan.get(
            'authorized_baseline_candidate_ids', []
        ):
            allowed.add(str(candidate_id))
    return allowed


def holdout_permission_violations(
    period_rows: Iterable[Mapping[str, Any]],
    *,
    allowed_candidate_ids: set[str],
) -> list[dict[str, Any]]:
    grouped: dict[str, int] = defaultdict(int)
    for row in period_rows:
        if str(row.get('split_role')) != 'holdout':
            continue
        candidate_id = str(row.get('candidate_id') or '')
        if candidate_id not in allowed_candidate_ids:
            grouped[candidate_id] += 1
    return [
        {'candidate_id': candidate_id, 'holdout_period_row_count': count}
        for candidate_id, count in sorted(grouped.items())
    ]


def enforce_holdout_permissions(
    period_rows: Iterable[Mapping[str, Any]],
    *,
    stage8_decision: Mapping[str, Any],
    registry: Mapping[str, Any],
    baseline_absolute_plan: Mapping[str, Any] | None = None,
) -> None:
    allowed = allowed_holdout_candidate_ids(
        stage8_decision,
        registry,
        baseline_absolute_plan=baseline_absolute_plan,
    )
    violations = holdout_permission_violations(
        period_rows, allowed_candidate_ids=allowed
    )
    if violations:
        raise RuntimeError(
            'Stage 9 V2 rejected unopened holdout access for '
            f'{len(violations)} candidates.'
        )


def candidate_schedule_v2(
    schedule: Sequence[Mapping[str, Any]],
    candidate: Candidate,
    *,
    allowed_holdout_ids: set[str],
) -> list[dict[str, Any]]:
    """Reject, rather than silently score, an unopened candidate holdout."""

    if any(
        str(row['split_role']) == 'holdout' for row in schedule
    ) and candidate.candidate_id not in allowed_holdout_ids:
        raise RuntimeError(
            f'Candidate {candidate.candidate_id} is not authorized for holdout.'
        )
    return [dict(row) for row in schedule]


def phase_summary_rows(
    period_rows: Sequence[Mapping[str, Any]],
    *,
    decision_roles: Sequence[str] = ('validation', 'holdout'),
    return_field: str = 'net_xlp_relative_return_observed_cost',
) -> list[dict[str, Any]]:
    """Emit phase-specific evidence; never blend embargo/train into decisions."""

    requested = set(decision_roles)
    invalid = requested - EVIDENCE_ROLES
    if invalid:
        raise ValueError(
            f'Promotion summaries cannot use non-evidence roles: {sorted(invalid)}'
        )
    keys = (
        'candidate_id', 'scope_id', 'candidate_kind', 'portfolio_name',
        'weight_method', 'exposure_mode', 'split_role',
    )
    grouped: dict[tuple[str, ...], list[float]] = defaultdict(list)
    for row in period_rows:
        role = str(row.get('split_role'))
        if role not in requested:
            continue
        value = _finite(row.get(return_field))
        if value is None:
            continue
        grouped[tuple(str(row.get(key) or '') for key in keys)].append(value)
    output: list[dict[str, Any]] = []
    for identity, values in sorted(grouped.items()):
        compounded = math.prod(1.0 + value for value in values) - 1.0
        positive = sum(value > 0.0 for value in values)
        row = {
            **dict(zip(keys, identity, strict=True)),
            'evaluation_slice': identity[-1],
            'return_field': return_field,
            'period_count': len(values),
            'mean_period_return': statistics.fmean(values),
            'compounded_return': compounded,
            'positive_period_count': positive,
            'positive_period_fraction': positive / len(values),
            'aggregate_train_embargo_mixed_flag': 0,
        }
        row['summary_row_sha256'] = _sha256(row)
        output.append(row)
    return output


def price_selection_rows(conn: Any) -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in conn.execute(
            '''SELECT ticker,selected_source_id,selection_asof_date,
                      adjustment_basis,selection_reason,coverage_status
               FROM dim_price_series_selection
               WHERE purpose='scoring_return_series'
               ORDER BY ticker'''
        )
    ]


def price_selection_sha256(conn: Any) -> str:
    return _sha256(price_selection_rows(conn))


def enforce_frozen_price_selection(
    conn: Any,
    *,
    expected_sha256: str,
) -> str:
    actual = price_selection_sha256(conn)
    if actual != expected_sha256:
        raise RuntimeError(
            'Stage 9 price selection differs from the frozen Stage 8 selection.'
        )
    return actual


def decision_from_bound_stage8(
    stage8_decision: Mapping[str, Any],
    *,
    absolute_baseline_gate: Mapping[str, Any],
    holdout_violation_count: int,
    target_contract_pass: bool,
) -> dict[str, Any]:
    """Derive decision state from evidence and remain fail-closed."""

    accepted = int(stage8_decision.get(
        'accepted_research_candidate_count', 0
    ))
    blockers: list[str] = []
    if holdout_violation_count:
        blockers.append(
            f'unopened_holdout_candidate_count={holdout_violation_count}'
        )
    if not target_contract_pass:
        blockers.append('primary_target_or_cadence_mismatch')
    if int(absolute_baseline_gate.get(
        'limited_production_ready_flag', 0
    )) != 1:
        blockers.extend(
            str(value) for value in absolute_baseline_gate.get('blockers', [])
        )
    payload = {
        'schema_version': STAGE9_BACKTEST_V2,
        'stage8_candidate_promotion_count': accepted,
        'stage8_action': str(stage8_decision.get('action') or ''),
        'limited_production_ready_flag': int(not blockers),
        'decision_readiness': (
            'ready_for_separately_authorized_limited_production_review'
            if not blockers else 'blocked_fail_closed'
        ),
        'blockers': sorted(set(blockers)),
        'portfolio_write_enabled': False,
        'production_promotion_enabled': False,
    }
    return {**payload, 'decision_sha256': _sha256(payload)}


def _read_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(payload, dict):
        raise ValueError(f'Expected JSON object: {path}')
    return payload


def _read_period_rows(path: Path) -> list[dict[str, str]]:
    if path.suffix == '.gz':
        with gzip.open(path, 'rt', encoding='utf-8', newline='') as handle:
            return list(csv.DictReader(handle))
    with path.open('r', encoding='utf-8', newline='') as handle:
        return list(csv.DictReader(handle))


def audit_existing_holdout_access(
    *,
    stage8_root: Path,
    stage9_root: Path,
) -> dict[str, Any]:
    """Executable audit for legacy Stage 9 holdout-governance breaches."""

    decision = _read_json(stage8_root / 'stage8_decision.json')
    registry = _read_json(stage8_root / 'stage8_candidate_registry.json')
    periods = _read_period_rows(
        stage9_root / 'stage9_period_results.csv.gz'
    )
    allowed = allowed_holdout_candidate_ids(decision, registry)
    violations = holdout_permission_violations(
        periods, allowed_candidate_ids=allowed
    )
    holdout_ids = {
        str(row.get('candidate_id') or '')
        for row in periods if str(row.get('split_role')) == 'holdout'
    }
    return {
        'schema_version': STAGE9_BACKTEST_V2,
        'allowed_holdout_candidate_count': len(allowed),
        'observed_holdout_candidate_count': len(holdout_ids),
        'unauthorized_holdout_candidate_count': len(violations),
        'unauthorized_holdout_period_row_count': sum(
            int(row['holdout_period_row_count']) for row in violations
        ),
        'holdout_unexposed_flag': int(not violations),
        'violations': violations,
    }
