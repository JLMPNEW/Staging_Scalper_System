from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, Mapping, Sequence

from .config import ConfigBundle, cfg_get
from .scoring_features import CORE_COMPONENT_SPECS
from .stage8_calibration import (
    Candidate,
    ChronologicalSplit,
    HORIZONS,
    SECTOR_SCOPE,
    _best_candidate,
    _finite,
    _result_row,
)
from .stage8_validation_v2 import evaluate_candidate_same_sample


STAGE8_CALIBRATION_V2 = 'consumer_defensive_stage8_calibration_v2'


@dataclass(frozen=True)
class BaselineEligibilityV2:
    eligible: bool
    rank_ready: bool
    available_weight: float
    missing_weight: float
    structural_missing_weight: float
    reasons: tuple[str, ...]


def complete_month_evaluation_dates(
    spy_prices: Mapping[str, float],
    *,
    history_start: str,
    as_of: str,
    entry_lag: int,
    maximum_horizon: int,
) -> tuple[list[str], list[str]]:
    """Return true month-end sessions whose labels are fully mature.

    Stage 6C v1 truncated the calendar at the label-maturity cutoff and then
    called the last remaining session in a partial month a month-end.  V2 first
    identifies month-ends from the full as-of calendar and only then applies
    the maturity cutoff.
    """

    calendar = sorted(
        value for value in spy_prices if history_start <= value <= as_of
    )
    maximum_index = len(calendar) - 1 - entry_lag - maximum_horizon
    if maximum_index < 0:
        raise RuntimeError('Stage 8 V2 has insufficient SPY label history.')
    true_month_ends: dict[str, str] = {}
    for session in calendar:
        true_month_ends[session[:7]] = session
    mature_cutoff = calendar[maximum_index]
    dates = sorted(
        session
        for session in true_month_ends.values()
        if session <= mature_cutoff
    )
    return calendar, dates


def _usable_components(row: Mapping[str, Any]) -> set[str]:
    scores = row['_component_scores']
    quality = row['_component_quality']
    return {
        spec.name
        for spec in CORE_COMPONENT_SPECS
        if float(quality.get(spec.name, 0.0)) > 0.0
        and _finite(scores.get(spec.name)) is not None
    }


def rank_requirements_era_aware(
    row: Mapping[str, Any],
    *,
    as_of: str,
    short_interest_birthdate: str,
) -> tuple[bool, tuple[str, ...]]:
    """Apply the hard short requirement only after the source exists."""

    usable = _usable_components(row)
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
    if as_of >= short_interest_birthdate and not any(
        spec.name in usable
        for spec in CORE_COMPONENT_SPECS
        if spec.rank_requirement == 'any_short'
    ):
        reasons.append('missing_requirement:any_short')
    return not reasons, tuple(reasons)


def baseline_eligibility_era_aware(
    row: Mapping[str, Any],
    baseline: Candidate,
    bundle: ConfigBundle,
) -> BaselineEligibilityV2:
    """Recompute frozen-baseline eligibility without using any outcome."""

    as_of = str(row['asof_date'])
    birth = str(cfg_get(
        bundle.payload,
        'positioning.source_birthdates.short_interest',
    ))
    ready, requirement_reasons = rank_requirements_era_aware(
        row,
        as_of=as_of,
        short_interest_birthdate=birth,
    )
    reasons = list(requirement_reasons)
    scores = row['_component_scores']
    quality = row['_component_quality']
    usable = _usable_components(row)
    available_weight = sum(
        weight for name, weight in baseline.core_weights.items()
        if name in usable
    )
    missing_weight = sum(baseline.core_weights.values()) - available_weight
    minimum_quality = float(cfg_get(
        bundle.payload,
        'stage7_scoring.minimum_data_quality_confidence',
    ))
    maximum_missing = float(cfg_get(
        bundle.payload,
        'stage7_scoring.maximum_missing_component_weight',
    ))
    if available_weight < minimum_quality:
        reasons.append(f'low_data_quality={available_weight:.6f}')
    if missing_weight > maximum_missing:
        reasons.append(f'missing_component_weight={missing_weight:.6f}')
    rank_ready = ready and not (
        available_weight < minimum_quality or missing_weight > maximum_missing
    )
    structural_missing_weight = 0.0
    if as_of < birth:
        structural_missing_weight = sum(
            baseline.core_weights.get(spec.name, 0.0)
            for spec in CORE_COMPONENT_SPECS
            if spec.rank_requirement == 'any_short'
            and spec.name not in usable
        )
    membership = int(row.get('membership_eligible_flag', 1)) == 1
    investable = int(row.get('investable_flag', 1)) == 1
    del scores, quality
    return BaselineEligibilityV2(
        eligible=rank_ready and membership and investable,
        rank_ready=rank_ready,
        available_weight=available_weight,
        missing_weight=missing_weight,
        structural_missing_weight=structural_missing_weight,
        reasons=tuple(sorted(reasons)),
    )


def prepare_validation_panel_v2(
    rows: Sequence[Mapping[str, Any]],
    baseline: Candidate,
    bundle: ConfigBundle,
    *,
    complete_month_dates: Sequence[str],
) -> list[dict[str, Any]]:
    """Apply complete-month and source-era rules to a prepared panel."""

    allowed = set(complete_month_dates)
    output: list[dict[str, Any]] = []
    for source in rows:
        if str(source['asof_date']) not in allowed:
            continue
        eligibility = baseline_eligibility_era_aware(
            source, baseline, bundle
        )
        row = dict(source)
        row['baseline_rank_ready_flag'] = int(eligibility.rank_ready)
        row['calibration_eligible_flag'] = int(eligibility.eligible)
        row['available_weight'] = eligibility.available_weight
        row['missing_weight'] = eligibility.missing_weight
        row['structural_missing_weight'] = (
            eligibility.structural_missing_weight
        )
        row['validation_v2_review_reason'] = ';'.join(eligibility.reasons)
        output.append(row)
    return output


def calibration_date_census_same_sample(
    rows: Sequence[Mapping[str, Any]],
    baseline: Candidate,
    bundle: ConfigBundle,
) -> tuple[list[str], list[dict[str, Any]]]:
    """Build the label-blind census from the frozen V2 baseline sample."""

    if baseline.scope_id != SECTOR_SCOPE:
        raise ValueError('Stage 8 V2 census requires the sector baseline.')
    floor = int(cfg_get(
        bundle.payload,
        'stage8_calibration.minimum_sector_cross_section',
    ))
    by_date: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in rows:
        by_date[str(row['asof_date'])].append(row)
    selected: list[str] = []
    census: list[dict[str, Any]] = []
    for as_of in sorted(by_date):
        eligible = sum(
            int(row['calibration_eligible_flag']) for row in by_date[as_of]
        )
        included = eligible >= floor
        if included:
            selected.append(as_of)
        census.append({
            'asof_date': as_of,
            'eligible_count': eligible,
            'minimum_sector_cross_section': floor,
            'included_flag': int(included),
            'sample_policy': 'era_aware_frozen_stage7_same_sample_v2',
        })
    return selected, census


def latest_fresh_db_row(
    conn: Any,
    *,
    table: str,
    ticker: str,
    date_column: str,
    as_of: str,
    source_id: str,
    maximum_age_days: int | None,
) -> Any | None:
    """Select the latest PIT row only when it satisfies configured age."""

    if maximum_age_days is not None and maximum_age_days < 0:
        raise ValueError('maximum_age_days cannot be negative')
    lower = (
        None if maximum_age_days is None
        else (date.fromisoformat(as_of) - timedelta(
            days=maximum_age_days
        )).isoformat()
    )
    query = (
        f'SELECT * FROM {table} WHERE ticker=? AND source_id=? '
        f'AND {date_column}<=?'
    )
    parameters: list[Any] = [ticker, source_id, as_of]
    if lower is not None:
        query += f' AND {date_column}>=?'
        parameters.append(lower)
    query += f' ORDER BY {date_column} DESC,rowid DESC LIMIT 1'
    return conn.execute(query, parameters).fetchone()


def positioning_rows_for_date_v2(
    conn: Any,
    bundle: ConfigBundle,
    *,
    as_of: str,
    ticker: str,
) -> dict[str, Any]:
    """Load fresh PIT positioning observations with explicit era states."""

    source_id = str(cfg_get(
        bundle.payload, 'positioning.market_positioning_source_id'
    ))
    specifications = {
        'institutional_13f': (
            'fact_13f_positioning', 'publication_date',
        ),
        'short_interest': (
            'fact_short_interest', 'publication_date',
        ),
        'borrow': ('fact_borrow_snapshot', 'asof_date'),
    }
    result: dict[str, Any] = {}
    states: dict[str, str] = {}
    for source_key, (table, date_column) in specifications.items():
        birth = str(cfg_get(
            bundle.payload, f'positioning.source_birthdates.{source_key}'
        ))
        maximum_age = cfg_get(
            bundle.payload, f'positioning.maximum_age_days.{source_key}'
        )
        if as_of < birth:
            result[source_key] = None
            states[source_key] = 'structurally_unavailable'
            continue
        row = latest_fresh_db_row(
            conn,
            table=table,
            ticker=ticker,
            date_column=date_column,
            as_of=as_of,
            source_id=source_id,
            maximum_age_days=(
                None if maximum_age is None else int(maximum_age)
            ),
        )
        result[source_key] = row
        states[source_key] = 'fresh' if row is not None else 'missing_or_stale'
    result['source_states'] = states
    return result


def _walk_forward_same_sample(
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
            evaluate_candidate_same_sample(
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
                'candidate_id': '',
                'constraint_pass': 0,
                'win_flag': 0,
            })
            test_start += test_count
            continue
        selected = next(
            item for item in candidates
            if item.candidate_id == best['candidate_id']
        )
        candidate_test = evaluate_candidate_same_sample(
            rows, test_dates, selected, bundle,
            required_factor_ids=required_factor_ids,
        )
        baseline_test = evaluate_candidate_same_sample(
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
        constraint = int(candidate_test['constraint_pass'])
        win = int(
            improvement is not None and improvement > 0.0 and constraint == 1
        )
        wins += win
        constraints += constraint
        folds.append({
            'family_id': family_id,
            'fold': len(folds) + 1,
            'train_start': train_dates[0],
            'train_end': train_dates[-1],
            'test_start': test_dates[0],
            'test_end': test_dates[-1],
            'candidate_id': selected.candidate_id,
            'candidate_objective': candidate_objective,
            'baseline_objective': baseline_objective,
            'objective_improvement': improvement,
            'constraint_pass': constraint,
            'win_flag': win,
        })
        test_start += test_count
    denominator = len(folds)
    return (
        folds,
        wins / denominator if denominator else 0.0,
        constraints / denominator if denominator else 0.0,
    )


def run_research_family_same_sample(
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
    """Executable Stage 8 challenger path on one frozen comparison sample."""

    train_results = [
        evaluate_candidate_same_sample(
            rows, split.train_dates, candidate, bundle,
            required_factor_ids=required_factor_ids,
        )
        for candidate in candidates
    ]
    best = _best_candidate(
        train_results, baseline_id=baseline.candidate_id
    )
    results = [
        _result_row(
            result,
            family_id=family_id,
            phase='train',
            selected=(best is not None and result['candidate_id'] == best['candidate_id']),
        )
        for result in train_results
    ]
    if best is None:
        return results, [], {
            'schema_version': STAGE8_CALIBRATION_V2,
            'family_id': family_id,
            'verdict': 'inconclusive',
            'reason': 'no_complete_constraint_feasible_training_candidate',
            'holdout_opened': 0,
            'production_weight_change_allowed': 0,
        }
    selected = next(
        item for item in candidates if item.candidate_id == best['candidate_id']
    )
    validation_candidate = evaluate_candidate_same_sample(
        rows, split.validation_dates, selected, bundle,
        required_factor_ids=required_factor_ids,
    )
    validation_baseline = evaluate_candidate_same_sample(
        rows, split.validation_dates, baseline, bundle,
        required_factor_ids=required_factor_ids,
    )
    candidate_objective = _finite(validation_candidate['objective'])
    baseline_objective = _finite(validation_baseline['objective'])
    improvement = (
        candidate_objective - baseline_objective
        if candidate_objective is not None and baseline_objective is not None
        else None
    )
    settings = cfg_get(bundle.payload, 'stage8_calibration')
    validation_gate = (
        validation_candidate['status'] == 'complete'
        and validation_baseline['status'] == 'complete'
        and int(validation_candidate['constraint_pass']) == 1
        and improvement is not None
        and improvement >= float(
            settings['minimum_validation_objective_improvement']
        )
        and all(
            float(validation_candidate[f'mean_ic_{horizon}d'] or -1.0) > 0.0
            for horizon in (63, 126)
        )
    )
    results.extend((
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
    walk, win_fraction, constraint_fraction = _walk_forward_same_sample(
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
        bool(walk)
        and win_fraction >= float(settings['minimum_walk_forward_win_fraction'])
        and constraint_fraction >= float(
            settings['minimum_walk_forward_win_fraction']
        )
    )
    if not validation_gate or not walk_gate:
        return results, walk, {
            'schema_version': STAGE8_CALIBRATION_V2,
            'family_id': family_id,
            'selected_candidate_id': selected.candidate_id,
            'verdict': 'rejected',
            'reason': (
                'validation_gate_failed'
                if not validation_gate else 'walk_forward_gate_failed'
            ),
            'validation_objective_improvement': improvement,
            'validation_gate_pass': int(validation_gate),
            'walk_forward_gate_pass': int(walk_gate),
            'holdout_opened': 0,
            'production_weight_change_allowed': 0,
            'sample_policy': 'frozen_stage7_calibration_eligible_sample',
        }
    holdout_candidate = evaluate_candidate_same_sample(
        rows, split.holdout_dates, selected, bundle,
        required_factor_ids=required_factor_ids,
    )
    holdout_baseline = evaluate_candidate_same_sample(
        rows, split.holdout_dates, baseline, bundle,
        required_factor_ids=required_factor_ids,
    )
    holdout_candidate_objective = _finite(holdout_candidate['objective'])
    holdout_baseline_objective = _finite(holdout_baseline['objective'])
    holdout_improvement = (
        holdout_candidate_objective - holdout_baseline_objective
        if holdout_candidate_objective is not None
        and holdout_baseline_objective is not None
        else None
    )
    thresholds = {
        int(key): float(value)
        for key, value in settings['minimum_holdout_mean_ic'].items()
    }
    holdout_gate = (
        holdout_candidate['status'] == 'complete'
        and holdout_baseline['status'] == 'complete'
        and int(holdout_candidate['constraint_pass']) == 1
        and holdout_improvement is not None
        and holdout_improvement >= float(
            settings['minimum_holdout_objective_improvement']
        )
        and all(
            float(holdout_candidate[f'mean_ic_{horizon}d'] or -1.0) >= threshold
            for horizon, threshold in thresholds.items()
        )
    )
    results.extend((
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
    return results, walk, {
        'schema_version': STAGE8_CALIBRATION_V2,
        'family_id': family_id,
        'selected_candidate_id': selected.candidate_id,
        'verdict': 'accepted' if holdout_gate else 'rejected',
        'validation_objective_improvement': improvement,
        'validation_gate_pass': 1,
        'walk_forward_gate_pass': 1,
        'holdout_opened': 1,
        'holdout_objective_improvement': holdout_improvement,
        'holdout_gate_pass': int(holdout_gate),
        'production_weight_change_allowed': 0,
        'sample_policy': 'frozen_stage7_calibration_eligible_sample',
    }


def exact_one_sided_sign_pvalue(positive_count: int, count: int) -> float:
    if count <= 0 or not 0 <= positive_count <= count:
        raise ValueError('Invalid sign-test counts.')
    return sum(
        math.comb(count, value) for value in range(positive_count, count + 1)
    ) / (2 ** count)


def absolute_baseline_evidence(
    rows: Sequence[Mapping[str, Any]],
    dates: Sequence[str],
    baseline: Candidate,
    bundle: ConfigBundle,
) -> dict[str, Any]:
    """Evaluate absolute rank efficacy separately from challenger uplift."""

    result = evaluate_candidate_same_sample(rows, dates, baseline, bundle)
    signs: dict[str, Any] = {}
    for horizon in HORIZONS:
        values = [
            _finite(detail.get(f'ic_{horizon}d'))
            for detail in result['date_details']
        ]
        finite = [value for value in values if value is not None]
        positive = sum(value > 0.0 for value in finite)
        signs[f'positive_ic_date_count_{horizon}d'] = positive
        signs[f'ic_date_count_{horizon}d'] = len(finite)
        signs[f'ic_sign_pvalue_{horizon}d'] = (
            exact_one_sided_sign_pvalue(positive, len(finite))
            if finite else None
        )
    return {
        **{key: value for key, value in result.items() if key != 'date_details'},
        **signs,
        'validation_kind': 'absolute_frozen_baseline_efficacy',
        'challenger_incremental_gate_used': 0,
    }


def fail_closed_limited_production_gate(
    evidence: Mapping[str, Any],
    *,
    minimum_holdout_dates: int,
    maximum_sign_pvalue: float,
    invariants: Mapping[str, bool],
) -> dict[str, Any]:
    """Never recommend limited production when provenance is unresolved."""

    required_invariants = (
        'same_sample_correct',
        'positioning_freshness_correct',
        'complete_month_cadence_correct',
        'source_identity_tied',
        'survivorship_correct',
        'holdout_unexposed',
        'strict_oos',
    )
    blockers = [
        f'invariant_failed:{name}'
        for name in required_invariants
        if not bool(invariants.get(name, False))
    ]
    for horizon in HORIZONS:
        count = int(evidence.get(f'ic_date_count_{horizon}d') or 0)
        mean_ic = _finite(evidence.get(f'mean_ic_{horizon}d'))
        mean_spread = _finite(evidence.get(f'mean_spread_net_{horizon}d'))
        sign_p = _finite(evidence.get(f'ic_sign_pvalue_{horizon}d'))
        if count < minimum_holdout_dates:
            blockers.append(f'insufficient_holdout_dates:{horizon}d={count}')
        if mean_ic is None or mean_ic <= 0.0:
            blockers.append(f'nonpositive_holdout_ic:{horizon}d')
        if mean_spread is None or mean_spread <= 0.0:
            blockers.append(f'nonpositive_holdout_spread:{horizon}d')
        if sign_p is None or sign_p > maximum_sign_pvalue:
            blockers.append(f'holdout_sign_test_failed:{horizon}d')
    return {
        'schema_version': STAGE8_CALIBRATION_V2,
        'limited_production_ready_flag': int(not blockers),
        'action': (
            'eligible_for_separately_authorized_limited_production_review'
            if not blockers else 'remain_shadow_fail_closed'
        ),
        'blockers': blockers,
        'portfolio_write_enabled': False,
        'production_promotion_enabled': False,
    }
