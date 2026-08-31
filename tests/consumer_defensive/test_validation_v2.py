from __future__ import annotations

import copy
import sqlite3
from datetime import date, timedelta
from pathlib import Path

import pytest

from consumer_defensive.core.config import ConfigBundle, load_config
from consumer_defensive.core.scoring_features import CORE_COMPONENT_SPECS
from consumer_defensive.core.stage7_scoring import stage7_component_weights
from consumer_defensive.core.stage8_calibration import (
    SECTOR_SCOPE,
    _make_candidate,
    _score_candidate,
)
from consumer_defensive.core.stage8_calibration_v2 import (
    absolute_baseline_evidence,
    baseline_eligibility_era_aware,
    complete_month_evaluation_dates,
    fail_closed_limited_production_gate,
    latest_fresh_db_row,
    rank_requirements_era_aware,
)
from consumer_defensive.core.stage8_validation_v2 import (
    evaluate_candidate_same_sample,
    score_candidate_same_sample,
)
from consumer_defensive.core.stage9_backtest_v2 import (
    allowed_holdout_candidate_ids,
    build_monthly_rebalance_schedule,
    decision_from_bound_stage8,
    enforce_holdout_permissions,
    holdout_permission_violations,
    phase_summary_rows,
    validate_primary_target_contract,
)


ROOT = Path(__file__).resolve().parents[2]
CONFIG = ROOT / 'consumer_defensive' / 'config.yaml'


def _bundle(**stage8_overrides: object) -> ConfigBundle:
    original = load_config(CONFIG)
    payload = copy.deepcopy(original.payload)
    payload['stage8_calibration'].update(stage8_overrides)
    return ConfigBundle(original.path, original.base_dir, payload)


def _candidate(
    bundle: ConfigBundle,
    *,
    candidate_kind: str = 'stage7_core_baseline',
    weights: dict[str, float] | None = None,
    scope_id: str = SECTOR_SCOPE,
):
    return _make_candidate(
        scope_id=scope_id,
        candidate_kind=candidate_kind,
        core_weights=weights or stage7_component_weights(bundle),
        specialized_weights={},
        parent_candidate_id=None,
        shrinkage_alpha=0.0,
        evidence_references=('v2-test',),
    )


def _prepared_row(
    *,
    as_of: str,
    ticker: str = 'T00',
    missing: set[str] | None = None,
    signal: float = 50.0,
) -> dict[str, object]:
    missing = missing or set()
    scores = {spec.name: signal for spec in CORE_COMPONENT_SPECS}
    quality = {
        spec.name: (0.0 if spec.name in missing else 1.0)
        for spec in CORE_COMPONENT_SPECS
    }
    return {
        'asof_date': as_of,
        'ticker': ticker,
        'cohort_id': 'beverages',
        'membership_eligible_flag': 1,
        'investable_flag': 1,
        'calibration_eligible_flag': 1,
        '_component_scores': scores,
        '_component_quality': quality,
        '_specialized_scores': {},
        '_specialized_applicability': {},
        'forward_xlp_residual_return_21d': signal,
        'forward_xlp_residual_return_63d': signal,
        'forward_xlp_residual_return_126d': signal,
    }


def test_candidate_reweight_cannot_change_frozen_comparison_sample() -> None:
    bundle = _bundle(minimum_sector_cross_section=6)
    weights = {
        spec.name: (
            0.60 if spec.name == 'gross_margin'
            else 0.40 if spec.name == 'residual_momentum_63d'
            else 0.0
        )
        for spec in CORE_COMPONENT_SPECS
    }
    candidate = _candidate(
        bundle,
        candidate_kind='sector_core_reweight',
        weights=weights,
    )
    row = _prepared_row(
        as_of='2024-01-31', missing={'gross_margin'}, signal=80.0
    )
    assert _score_candidate(row, candidate, bundle)[3] is False
    v2 = score_candidate_same_sample(row, candidate, bundle)
    assert v2.frozen_sample_eligible is True
    assert v2.candidate_quality_gate_pass is False

    rows = [
        _prepared_row(
            as_of=as_of,
            ticker=f'T{index:02d}',
            missing={'gross_margin'},
            signal=float(index),
        )
        for as_of in ('2024-01-31', '2024-02-29', '2024-03-28')
        for index in range(6)
    ]
    result = evaluate_candidate_same_sample(
        rows,
        ['2024-01-31', '2024-02-29', '2024-03-28'],
        candidate,
        bundle,
    )
    assert result['status'] == 'complete'
    assert result['candidate_quality_observation_count'] == 18
    assert result['candidate_quality_gate_pass_fraction'] == 0.0


def test_short_requirement_is_waived_only_before_source_birth() -> None:
    bundle = _bundle()
    baseline = _candidate(bundle)
    missing = {'short_float_pct', 'short_days_to_cover'}
    pre_birth = _prepared_row(
        as_of='2020-06-30', missing=missing
    )
    ready, reasons = rank_requirements_era_aware(
        pre_birth,
        as_of='2020-06-30',
        short_interest_birthdate='2021-07-01',
    )
    assert ready
    assert 'missing_requirement:any_short' not in reasons
    eligibility = baseline_eligibility_era_aware(
        pre_birth, baseline, bundle
    )
    assert eligibility.eligible
    assert eligibility.structural_missing_weight > 0.0

    post_birth = _prepared_row(
        as_of='2022-06-30', missing=missing
    )
    ready, reasons = rank_requirements_era_aware(
        post_birth,
        as_of='2022-06-30',
        short_interest_birthdate='2021-07-01',
    )
    assert not ready
    assert 'missing_requirement:any_short' in reasons


def test_complete_month_dates_exclude_partial_maturity_tail() -> None:
    sessions = (
        '2026-01-30', '2026-02-27', '2026-03-02', '2026-03-13',
        '2026-03-31', '2026-04-01', '2026-04-30',
    )
    _calendar, dates = complete_month_evaluation_dates(
        {value: 100.0 for value in sessions},
        history_start='2026-01-01',
        as_of='2026-04-30',
        entry_lag=1,
        maximum_horizon=2,
    )
    assert dates == ['2026-01-30', '2026-02-27']
    assert '2026-03-13' not in dates


def test_fresh_db_selection_enforces_inclusive_age_boundary() -> None:
    conn = sqlite3.connect(':memory:')
    conn.row_factory = sqlite3.Row
    conn.execute(
        'CREATE TABLE facts('
        'ticker TEXT,source_id TEXT,publication_date TEXT,value REAL)'
    )
    as_of = date(2026, 8, 14)
    stale = (as_of - timedelta(days=151)).isoformat()
    fresh = (as_of - timedelta(days=150)).isoformat()
    conn.executemany(
        'INSERT INTO facts VALUES (?,?,?,?)',
        [('KO', 'source', stale, 1.0), ('PEP', 'source', fresh, 2.0)],
    )
    assert latest_fresh_db_row(
        conn,
        table='facts',
        ticker='KO',
        date_column='publication_date',
        as_of=as_of.isoformat(),
        source_id='source',
        maximum_age_days=150,
    ) is None
    row = latest_fresh_db_row(
        conn,
        table='facts',
        ticker='PEP',
        date_column='publication_date',
        as_of=as_of.isoformat(),
        source_id='source',
        maximum_age_days=150,
    )
    assert row is not None and row['value'] == 2.0


def _split() -> dict[str, object]:
    return {
        'train_dates': ['2024-01-31'],
        'first_embargo_dates': [],
        'validation_dates': ['2024-02-29'],
        'second_embargo_dates': [],
        'holdout_dates': [],
        'calibration_date_census': [
            {'asof_date': '2024-01-31', 'included_flag': 1},
            {'asof_date': '2024-02-29', 'included_flag': 1},
        ],
    }


def test_monthly_schedule_uses_every_signal_and_next_rebalance_exit() -> None:
    calendar = (
        '2024-01-30', '2024-01-31', '2024-02-01', '2024-02-28',
        '2024-02-29', '2024-03-01', '2024-03-28', '2024-04-01',
    )
    schedule = build_monthly_rebalance_schedule(
        _split(), calendar, entry_lag=1
    )
    assert len(schedule) == 2
    assert all(row['selected_rebalance_flag'] == 1 for row in schedule)
    assert schedule[0]['entry_date'] == '2024-02-01'
    assert schedule[0]['exit_date'] == '2024-03-01'
    assert schedule[1]['entry_date'] == '2024-03-01'
    assert schedule[1]['exit_date'] == '2024-04-01'


def test_monthly_schedule_rejects_partial_month_signal() -> None:
    split = {
        'train_dates': ['2026-02-11'],
        'first_embargo_dates': [],
        'validation_dates': [],
        'second_embargo_dates': [],
        'holdout_dates': [],
        'calibration_date_census': [
            {'asof_date': '2026-02-11', 'included_flag': 1}
        ],
    }
    with pytest.raises(RuntimeError, match='Incomplete month-end'):
        build_monthly_rebalance_schedule(
            split,
            ('2026-02-11', '2026-02-27', '2026-03-02', '2026-03-31'),
            entry_lag=1,
        )


def _holdout_registry() -> dict[str, object]:
    return {
        'candidates': [
            {
                'candidate_id': 'sector_base',
                'scope_id': 'consumer_defensive',
                'candidate_kind': 'stage7_core_baseline',
            },
            {
                'candidate_id': 'bev_base',
                'scope_id': 'beverages',
                'candidate_kind': 'stage7_core_baseline',
            },
            {
                'candidate_id': 'bev_challenger',
                'scope_id': 'beverages',
                'candidate_kind': 'cohort_core_reweight_shrunk',
            },
        ]
    }


def _stage8_decision() -> dict[str, object]:
    return {
        'accepted_research_candidate_count': 0,
        'action': 'retain_stage7_core_baseline',
        'family_decisions': [
            {
                'scope_id': 'beverages',
                'selected_candidate_id': 'bev_challenger',
                'holdout_opened': 1,
            },
            {
                'scope_id': 'consumer_defensive',
                'selected_candidate_id': 'sector_other',
                'holdout_opened': 0,
            },
        ],
    }


def test_stage9_rejects_unopened_holdout_candidates() -> None:
    registry = _holdout_registry()
    decision = _stage8_decision()
    allowed = allowed_holdout_candidate_ids(decision, registry)
    assert allowed == {'bev_base', 'bev_challenger'}
    periods = [
        {'candidate_id': 'bev_challenger', 'split_role': 'holdout'},
        {'candidate_id': 'sector_base', 'split_role': 'holdout'},
    ]
    violations = holdout_permission_violations(
        periods, allowed_candidate_ids=allowed
    )
    assert violations == [
        {'candidate_id': 'sector_base', 'holdout_period_row_count': 1}
    ]
    with pytest.raises(RuntimeError, match='unopened holdout'):
        enforce_holdout_permissions(
            periods, stage8_decision=decision, registry=registry
        )


def test_phase_summary_never_mixes_train_or_embargo_into_decision() -> None:
    periods = [
        {
            'candidate_id': 'base', 'scope_id': 'consumer_defensive',
            'candidate_kind': 'stage7_core_baseline',
            'portfolio_name': 'long_short', 'weight_method': 'equal_weight',
            'exposure_mode': 'long_short', 'split_role': role,
            'net_xlp_relative_return_observed_cost': value,
        }
        for role, value in (
            ('train', -0.90), ('embargo_1', -0.80),
            ('validation', 0.10), ('holdout', 0.20),
        )
    ]
    rows = phase_summary_rows(periods)
    assert {row['evaluation_slice'] for row in rows} == {
        'validation', 'holdout'
    }
    assert sorted(row['compounded_return'] for row in rows) == pytest.approx(
        [0.10, 0.20]
    )
    with pytest.raises(ValueError, match='non-evidence roles'):
        phase_summary_rows(periods, decision_roles=('train',))


def test_primary_target_and_cadence_must_match() -> None:
    mismatch = validate_primary_target_contract(
        stage8_primary_target='weighted_21_63_126_ic',
        stage9_return_target='fixed_21_session_return',
        scoring_frequency='monthly',
        rebalance_frequency='21_sessions',
    )
    assert mismatch['pass_flag'] == 0
    matched = validate_primary_target_contract(
        stage8_primary_target='next_rebalance_xlp_relative_return',
        stage9_return_target='next_rebalance_xlp_relative_return',
        scoring_frequency='monthly',
        rebalance_frequency='monthly',
    )
    assert matched['pass_flag'] == 1


def test_absolute_baseline_gate_is_independent_and_fail_closed() -> None:
    bundle = _bundle(minimum_sector_cross_section=6)
    baseline = _candidate(bundle)
    dates = ('2024-01-31', '2024-02-29', '2024-03-28')
    rows = [
        _prepared_row(as_of=as_of, ticker=f'T{index:02d}', signal=float(index))
        for as_of in dates for index in range(6)
    ]
    evidence = absolute_baseline_evidence(rows, dates, baseline, bundle)
    assert evidence['validation_kind'] == 'absolute_frozen_baseline_efficacy'
    assert evidence['challenger_incremental_gate_used'] == 0
    gate = fail_closed_limited_production_gate(
        evidence,
        minimum_holdout_dates=3,
        maximum_sign_pvalue=0.20,
        invariants={
            'same_sample_correct': True,
            'positioning_freshness_correct': True,
            'complete_month_cadence_correct': True,
            'source_identity_tied': True,
            'survivorship_correct': False,
            'holdout_unexposed': True,
            'strict_oos': True,
        },
    )
    assert gate['limited_production_ready_flag'] == 0
    assert 'invariant_failed:survivorship_correct' in gate['blockers']


def test_stage9_decision_is_derived_from_bound_stage8_decision() -> None:
    stage8 = _stage8_decision()
    stage8['accepted_research_candidate_count'] = 2
    gate = {
        'limited_production_ready_flag': 0,
        'blockers': ['invariant_failed:strict_oos'],
    }
    decision = decision_from_bound_stage8(
        stage8,
        absolute_baseline_gate=gate,
        holdout_violation_count=0,
        target_contract_pass=True,
    )
    assert decision['stage8_candidate_promotion_count'] == 2
    assert decision['limited_production_ready_flag'] == 0
    assert decision['portfolio_write_enabled'] is False
