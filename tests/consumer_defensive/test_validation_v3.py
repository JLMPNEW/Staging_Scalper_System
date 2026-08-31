from __future__ import annotations

import copy
from datetime import date, timedelta
from pathlib import Path

import pytest

from consumer_defensive.core.config import ConfigBundle, load_config
from consumer_defensive.core.portfolio_turnover_v2 import (
    equal_weight_long_short_holdings,
    one_way_leg_turnover,
    trade_notional_turnover,
)
from consumer_defensive.core.scoring_features import CORE_COMPONENT_SPECS
from consumer_defensive.core.stage7_scoring import stage7_component_weights
from consumer_defensive.core.stage8_calibration import SECTOR_SCOPE, _make_candidate
from consumer_defensive.core.stage8_independent_evidence_v3 import (
    independent_evidence_gate_v3,
    independent_horizon_evidence_v3,
)
from consumer_defensive.core.stage8_monthly_target_v3 import (
    MONTHLY_TARGET_FIELD,
    evaluate_monthly_candidate_same_sample_v3,
    monthly_plan_sha256,
    validate_preregistered_monthly_plan_v3,
)
from consumer_defensive.core.stage8_validation_v3 import (
    evaluate_candidate_same_sample_v3,
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
        evidence_references=('validation-v3-test',),
    )


def _row(as_of: str, ticker: str, signal: float) -> dict[str, object]:
    return {
        'asof_date': as_of,
        'ticker': ticker,
        'cohort_id': 'beverages',
        'calibration_eligible_flag': 1,
        '_component_scores': {
            spec.name: signal for spec in CORE_COMPONENT_SPECS
        },
        '_component_quality': {
            spec.name: 1.0 for spec in CORE_COMPONENT_SPECS
        },
        '_specialized_scores': {},
        '_specialized_applicability': {},
        MONTHLY_TARGET_FIELD: signal / 1000.0,
        'forward_xlp_residual_return_21d': signal / 1000.0,
        'forward_xlp_residual_return_63d': signal / 1000.0,
        'forward_xlp_residual_return_126d': signal / 1000.0,
    }


def _three_date_panel() -> tuple[list[dict[str, object]], list[str]]:
    dates = ['2024-01-31', '2024-02-29', '2024-03-31']
    first = [80.0, 70.0, 60.0, 50.0, 40.0, 30.0, 20.0, 10.0]
    second = [80.0, 70.0, 60.0, 50.0, 25.0, 40.0, 20.0, 10.0]
    rows = [
        _row(as_of, f'T{index}', signal)
        for as_of, signals in zip(
            dates, (first, second, second), strict=True
        )
        for index, signal in enumerate(signals)
    ]
    return rows, dates


def test_signed_long_short_turnover_tracks_each_sleeve() -> None:
    first = equal_weight_long_short_holdings(
        {'A', 'B'}, {'C', 'D'}
    )
    second = equal_weight_long_short_holdings(
        {'A', 'B'}, {'C', 'E'}
    )
    assert trade_notional_turnover(None, first) == pytest.approx(2.0)
    assert trade_notional_turnover(first, second) == pytest.approx(1.0)
    assert one_way_leg_turnover({'A', 'B'}, {'A', 'B'}) == 0.0
    assert one_way_leg_turnover({'C', 'D'}, {'C', 'E'}) == pytest.approx(0.5)


def test_monthly_costs_include_initial_bottom_transition_and_final_exit() -> None:
    bundle = _bundle(minimum_sector_cross_section=8)
    rows, dates = _three_date_panel()
    result = evaluate_monthly_candidate_same_sample_v3(
        rows, dates, _baseline(bundle), bundle
    )
    details = result['date_details']
    assert details[0]['entry_rebalance_turnover'] == pytest.approx(2.0)
    assert details[1]['top_turnover'] == 0.0
    assert details[1]['bottom_turnover'] == pytest.approx(1.0 / 3.0)
    assert details[1]['trade_notional_turnover'] == pytest.approx(2.0 / 3.0)
    assert details[-1]['final_liquidation_turnover'] == pytest.approx(2.0)
    assert result['total_transaction_cost'] == pytest.approx(
        (2.0 + 2.0 / 3.0 + 2.0) * 0.002
    )


def test_monthly_decision_slice_inherits_prior_holdings_state() -> None:
    bundle = _bundle(minimum_sector_cross_section=8)
    rows, dates = _three_date_panel()
    full_schedule = evaluate_monthly_candidate_same_sample_v3(
        rows,
        dates,
        _baseline(bundle),
        bundle,
        decision_dates=[dates[1]],
        liquidate_final_holdings=False,
    )
    reset = evaluate_monthly_candidate_same_sample_v3(
        rows,
        [dates[1]],
        _baseline(bundle),
        bundle,
        liquidate_final_holdings=False,
    )
    assert full_schedule['schedule_date_count'] == 3
    assert full_schedule['date_details'][0]['transition_kind'] == 'direct_rebalance'
    assert full_schedule['total_transaction_cost'] == pytest.approx(
        (2.0 / 3.0) * 0.002
    )
    assert reset['total_transaction_cost'] == pytest.approx(2.0 * 0.002)


def test_fixed_horizon_v3_uses_same_full_long_short_cost_contract() -> None:
    bundle = _bundle(minimum_sector_cross_section=8)
    rows, dates = _three_date_panel()
    result = evaluate_candidate_same_sample_v3(
        rows, dates, _baseline(bundle), bundle
    )
    details = result['date_details']
    assert details[0]['entry_rebalance_turnover'] == pytest.approx(2.0)
    assert details[1]['top_turnover'] == 0.0
    assert details[1]['bottom_turnover'] == pytest.approx(1.0 / 3.0)
    assert details[1]['trade_notional_turnover'] == pytest.approx(2.0 / 3.0)
    assert details[-1]['final_liquidation_turnover'] == pytest.approx(2.0)


def _valid_plan() -> dict[str, object]:
    plan: dict[str, object] = {
        'plan_id': 'future-monthly-v3',
        'candidate_registry_sha256': 'a' * 64,
        'split_manifest_sha256': 'b' * 64,
        'target_field': MONTHLY_TARGET_FIELD,
        'scoring_frequency': 'monthly',
        'rebalance_frequency': 'monthly',
        'primary_objective': 'mean_rank_ic',
        'holdout_provenance': 'fresh_forward_oos',
        'registered_before_target_access': True,
        'holdout_sealed': True,
        'legacy_holdout_reuse_allowed': False,
        'train_dates': ['2024-01-31', '2024-02-29'],
        'first_embargo_dates': ['2024-03-31'],
        'validation_dates': ['2024-04-30'],
        'second_embargo_dates': ['2024-05-31'],
        'holdout_dates': ['2024-06-30'],
    }
    plan['plan_sha256'] = monthly_plan_sha256(plan)
    return plan


def test_monthly_plan_binds_registry_split_dates_and_self_hash() -> None:
    plan = _valid_plan()
    assert validate_preregistered_monthly_plan_v3(plan)[
        'plan_validation_pass_flag'
    ] == 1
    invalid_registry = {**plan, 'candidate_registry_sha256': 'abc123'}
    invalid_registry['plan_sha256'] = monthly_plan_sha256(invalid_registry)
    with pytest.raises(ValueError, match='candidate_registry_bound'):
        validate_preregistered_monthly_plan_v3(invalid_registry)
    tampered = {**plan, 'holdout_dates': ['2024-07-31']}
    with pytest.raises(ValueError, match='plan_self_hash_bound'):
        validate_preregistered_monthly_plan_v3(tampered)


def test_independent_gate_uses_independent_mean_ic_and_spread() -> None:
    start = date(2024, 1, 1)
    calendar = [
        (start + timedelta(days=index)).isoformat() for index in range(500)
    ]
    details = [
        {
            'asof_date': calendar[index],
            'ic_21d': 0.1,
            'ic_63d': 0.1,
            'ic_126d': 0.1,
            'spread_net_21d': 0.01,
            'spread_net_63d': 0.01,
            'spread_net_126d': -0.01,
        }
        for index in (0, 126, 252)
    ]
    evidence = independent_horizon_evidence_v3(
        {'date_details': details}, calendar, entry_lag=1
    )
    assert evidence['effective_mean_ic_126d'] == pytest.approx(0.1)
    assert evidence['effective_mean_spread_net_126d'] == pytest.approx(-0.01)
    gate = independent_evidence_gate_v3(
        evidence,
        minimum_independent_dates={21: 1, 63: 1, 126: 1},
        maximum_sign_pvalue=1.0,
        invariants={'test': True},
    )
    assert gate['pass_flag'] == 0
    assert 'nonpositive_independent_mean_spread_net:126d' in gate['blockers']
