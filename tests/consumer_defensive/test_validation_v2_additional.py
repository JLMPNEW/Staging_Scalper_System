from __future__ import annotations

import copy
from datetime import date, timedelta
from pathlib import Path

import pytest

from consumer_defensive.core.config import ConfigBundle, load_config
from consumer_defensive.core.scoring_features import CORE_COMPONENT_SPECS
from consumer_defensive.core.stage7_scoring import stage7_component_weights
from consumer_defensive.core.stage8_calibration import SECTOR_SCOPE, _make_candidate
from consumer_defensive.core.stage8_era_quality_v2 import (
    era_adjusted_baseline_eligibility,
    prepare_era_adjusted_panel,
)
from consumer_defensive.core.stage8_independent_evidence_v2 import (
    independent_horizon_evidence,
)
from consumer_defensive.core.stage8_monthly_target_v2 import (
    MONTHLY_TARGET_FIELD,
    build_next_rebalance_target_panel,
    evaluate_monthly_candidate_same_sample,
    fail_closed_monthly_absolute_gate,
    validate_preregistered_monthly_plan,
)


ROOT = Path(__file__).resolve().parents[2]
CONFIG = ROOT / 'consumer_defensive' / 'config.yaml'


def _bundle(**stage8_overrides: object) -> ConfigBundle:
    original = load_config(CONFIG)
    payload = copy.deepcopy(original.payload)
    payload['stage8_calibration'].update(stage8_overrides)
    return ConfigBundle(original.path, original.base_dir, payload)


def _baseline(bundle: ConfigBundle):
    return _make_candidate(
        scope_id=SECTOR_SCOPE,
        candidate_kind='stage7_core_baseline',
        core_weights=stage7_component_weights(bundle),
        specialized_weights={},
        parent_candidate_id=None,
        shrinkage_alpha=0.0,
        evidence_references=('v2-additional-test',),
    )


def _row(
    *,
    as_of: str,
    ticker: str = 'T00',
    signal: float = 50.0,
    missing: set[str] | None = None,
) -> dict[str, object]:
    missing = missing or set()
    return {
        'asof_date': as_of,
        'ticker': ticker,
        'cohort_id': 'beverages',
        'membership_eligible_flag': 1,
        'investable_flag': 1,
        'calibration_eligible_flag': 1,
        '_component_scores': {
            spec.name: signal for spec in CORE_COMPONENT_SPECS
        },
        '_component_quality': {
            spec.name: (0.0 if spec.name in missing else 1.0)
            for spec in CORE_COMPONENT_SPECS
        },
        '_specialized_scores': {},
        '_specialized_applicability': {},
    }


def test_source_era_adjusts_quality_denominator_without_reweighting_score() -> None:
    bundle = _bundle()
    baseline = _baseline(bundle)
    missing_financial = {
        'gross_margin', 'operating_margin',
        'free_cash_flow_margin', 'return_on_invested_capital',
    }
    row = _row(
        as_of='2020-06-30',
        signal=80.0,
        missing=missing_financial | {
            'short_float_pct', 'short_days_to_cover'
        },
    )
    result = era_adjusted_baseline_eligibility(row, baseline, bundle)
    assert result.absolute_available_weight == pytest.approx(0.61)
    assert result.absolute_missing_weight == pytest.approx(0.39)
    assert result.structural_missing_weight == pytest.approx(0.07)
    assert result.observable_available_fraction == pytest.approx(0.61 / 0.93)
    assert result.observable_missing_fraction == pytest.approx(0.32 / 0.93)
    assert result.eligible

    prepared = prepare_era_adjusted_panel([row], baseline, bundle)[0]
    assert prepared['_component_scores']['short_float_pct'] == 50.0
    assert prepared['_component_scores']['short_days_to_cover'] == 50.0
    assert prepared['_component_quality']['short_float_pct'] == 0.0
    assert prepared['calibration_eligible_flag'] == 1


def test_source_era_does_not_waive_missing_short_after_birth() -> None:
    bundle = _bundle()
    baseline = _baseline(bundle)
    row = _row(
        as_of='2022-06-30',
        missing={'short_float_pct', 'short_days_to_cover'},
    )
    result = era_adjusted_baseline_eligibility(row, baseline, bundle)
    assert not result.eligible
    assert 'missing_requirement:any_short' in result.reasons
    assert result.structural_missing_weight == 0.0


def test_exact_calendar_thinning_separates_raw_and_independent_counts() -> None:
    start = date(2024, 1, 1)
    calendar = [
        (start + timedelta(days=index)).isoformat() for index in range(240)
    ]
    asof_dates = [calendar[index] for index in (0, 21, 42, 63, 84, 105)]
    evaluation = {
        'date_details': [
            {
                'asof_date': as_of,
                'ic_21d': 0.10,
                'ic_63d': 0.10,
                'ic_126d': 0.10,
            }
            for as_of in asof_dates
        ]
    }
    evidence = independent_horizon_evidence(
        evaluation, calendar, entry_lag=1
    )
    assert evidence['raw_date_count_21d'] == 6
    assert evidence['effective_independent_date_count_21d'] == 6
    assert evidence['effective_independent_date_count_63d'] == 2
    assert evidence['effective_independent_date_count_126d'] == 1
    assert evidence['raw_overlap_sensitive_flag_63d'] == 1
    assert evidence['raw_overlap_sensitive_flag_126d'] == 1
    assert evidence['effective_sign_pvalue_126d'] == pytest.approx(0.5)


def _plan(**overrides: object) -> dict[str, object]:
    plan: dict[str, object] = {
        'plan_id': 'new-monthly-plan',
        'candidate_registry_sha256': 'abc123',
        'target_field': MONTHLY_TARGET_FIELD,
        'scoring_frequency': 'monthly',
        'rebalance_frequency': 'monthly',
        'primary_objective': 'mean_rank_ic',
        'holdout_provenance': 'fresh_forward_oos',
        'registered_before_target_access': True,
        'holdout_sealed': True,
        'legacy_holdout_reuse_allowed': False,
    }
    plan.update(overrides)
    return plan


def test_monthly_target_plan_prohibits_burned_holdout_reuse() -> None:
    assert validate_preregistered_monthly_plan(_plan())[
        'plan_validation_pass_flag'
    ] == 1
    with pytest.raises(ValueError, match='legacy_holdout_reuse'):
        validate_preregistered_monthly_plan(
            _plan(legacy_holdout_reuse_allowed=True)
        )


def test_monthly_target_builder_and_evaluator_use_same_return() -> None:
    bundle = _bundle(minimum_sector_cross_section=6)
    baseline = _baseline(bundle)
    dates = ('2024-01-31', '2024-02-29', '2024-03-31')
    schedule = [
        {
            'asof_date': as_of,
            'entry_date': f'2024-0{index + 2}-01',
            'exit_date': f'2024-0{index + 3}-01',
        }
        for index, as_of in enumerate(dates)
    ]
    rows = [
        _row(as_of=as_of, ticker=f'T{index:02d}', signal=float(index))
        for as_of in dates for index in range(6)
    ]
    all_price_dates = {
        value for item in schedule
        for value in (item['entry_date'], item['exit_date'])
    }
    ordered_price_dates = sorted(all_price_dates)
    xlp = {value: 100.0 for value in ordered_price_dates}
    prices = {
        f'T{index:02d}': {
            value: 100.0 * (1.0 + 0.01 * index) ** step
            for step, value in enumerate(ordered_price_dates)
        }
        for index in range(6)
    }
    targeted = build_next_rebalance_target_panel(
        rows,
        schedule,
        prices_by_ticker=prices,
        xlp_prices=xlp,
    )
    assert all(
        row['next_rebalance_target_status'] == 'complete'
        for row in targeted
    )
    result = evaluate_monthly_candidate_same_sample(
        targeted, dates, baseline, bundle
    )
    assert result['status'] == 'complete'
    assert result['target_field'] == MONTHLY_TARGET_FIELD
    assert result['mean_monthly_rank_ic'] == pytest.approx(1.0)
    assert result['mean_monthly_top_bottom_spread_net'] > 0.0

    gate = fail_closed_monthly_absolute_gate(
        result,
        minimum_dates=3,
        maximum_sign_pvalue=0.20,
        invariants={
            'holdout_unexposed': False,
            'strict_oos': True,
            'survivorship_correct': True,
        },
    )
    assert gate['limited_production_ready_flag'] == 0
    assert 'invariant_failed:holdout_unexposed' in gate['blockers']
