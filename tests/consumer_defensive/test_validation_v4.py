from __future__ import annotations

import copy
from pathlib import Path

import pytest

from consumer_defensive.core.config import ConfigBundle, load_config
from consumer_defensive.core.scoring_features import CORE_COMPONENT_SPECS
from consumer_defensive.core.stage7_scoring import stage7_component_weights
from consumer_defensive.core.stage8_calibration import SECTOR_SCOPE, _make_candidate
from consumer_defensive.core.stage8_monthly_target_v4 import (
    MONTHLY_TARGET_FIELD,
    evaluate_monthly_candidate_same_sample_v4,
)
from consumer_defensive.core.stage8_validation_v4 import (
    evaluate_candidate_same_sample_v4,
)


ROOT = Path(__file__).resolve().parents[2]
CONFIG = ROOT / 'consumer_defensive' / 'config.yaml'


class _ExplodesOnFloat:
    def __float__(self) -> float:
        raise AssertionError('nondecision target value was opened')


def _bundle() -> ConfigBundle:
    original = load_config(CONFIG)
    payload = copy.deepcopy(original.payload)
    payload['stage8_calibration']['minimum_sector_cross_section'] = 8
    return ConfigBundle(original.path, original.base_dir, payload)


def _baseline(bundle: ConfigBundle):
    return _make_candidate(
        scope_id=SECTOR_SCOPE,
        candidate_kind='stage7_core_baseline',
        core_weights=stage7_component_weights(bundle),
        specialized_weights={},
        parent_candidate_id=None,
        shrinkage_alpha=0.0,
        evidence_references=('validation-v4-test',),
    )


def _row(
    as_of: str,
    ticker: str,
    signal: float,
    *,
    target: object,
) -> dict[str, object]:
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
        MONTHLY_TARGET_FIELD: target,
        'forward_xlp_residual_return_21d': target,
        'forward_xlp_residual_return_63d': target,
        'forward_xlp_residual_return_126d': target,
    }


def _panel() -> tuple[list[dict[str, object]], list[str]]:
    dates = ['2024-01-31', '2024-02-29', '2024-03-31']
    rows: list[dict[str, object]] = []
    for as_of in dates:
        for index in range(8):
            signal = float(80 - index * 10)
            target: object = (
                signal / 1000.0
                if as_of == dates[1] else _ExplodesOnFloat()
            )
            rows.append(_row(
                as_of, f'T{index}', signal, target=target
            ))
    return rows, dates


def test_fixed_horizon_v4_never_opens_nondecision_labels() -> None:
    bundle = _bundle()
    rows, dates = _panel()
    result = evaluate_candidate_same_sample_v4(
        rows,
        dates,
        _baseline(bundle),
        bundle,
        decision_dates=[dates[1]],
        liquidate_final_holdings=False,
    )
    assert result['target_access_policy'].startswith('nondecision')
    assert result['date_details'][0]['asof_date'] == dates[1]
    assert result['date_details'][0]['transition_kind'] == 'direct_rebalance'


def test_monthly_v4_never_opens_nondecision_labels_and_keeps_state() -> None:
    bundle = _bundle()
    rows, dates = _panel()
    result = evaluate_monthly_candidate_same_sample_v4(
        rows,
        dates,
        _baseline(bundle),
        bundle,
        decision_dates=[dates[1]],
        liquidate_final_holdings=False,
    )
    assert result['target_access_policy'] == 'decision_dates_only'
    assert result['date_details'][0]['transition_kind'] == 'direct_rebalance'
    assert result['date_details'][0]['entry_rebalance_turnover'] == pytest.approx(0.0)
    assert result['total_transaction_cost'] == pytest.approx(0.0)
