from __future__ import annotations

import copy
from pathlib import Path

from consumer_defensive.core.config import ConfigBundle, load_config
from consumer_defensive.core.scoring_features import CORE_COMPONENT_SPECS
from consumer_defensive.core.stage7_scoring import stage7_component_weights
from consumer_defensive.core.stage8_calibration import SECTOR_SCOPE, _make_candidate
from consumer_defensive.core.stage8_monthly_target_v5 import (
    MONTHLY_TARGET_FIELD,
    evaluate_monthly_candidate_same_sample_v5,
)
from consumer_defensive.core.stage8_validation_v5 import (
    evaluate_candidate_same_sample_v5,
)


ROOT = Path(__file__).resolve().parents[2]
CONFIG = ROOT / 'consumer_defensive' / 'config.yaml'


class _ExplodesOnFloat:
    def __float__(self) -> float:
        raise AssertionError('nondecision target was opened')


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
        evidence_references=('validation-v5-test',),
    )


def _row(
    as_of: str,
    ticker: str,
    signal: float,
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


def _gap_panel() -> tuple[list[dict[str, object]], list[str]]:
    dates = ['2024-01-31', '2024-02-29', '2024-03-31']
    rows = [
        _row(
            as_of,
            f'T{index}',
            float(80 - 10 * index),
            (
                _ExplodesOnFloat()
                if as_of == dates[0]
                else float(80 - 10 * index) / 1000.0
            ),
        )
        for as_of in (dates[0], dates[2])
        for index in range(8)
    ]
    return rows, dates


def test_fixed_horizon_gap_reentry_resets_both_sleeve_turnovers() -> None:
    bundle = _bundle()
    rows, dates = _gap_panel()
    result = evaluate_candidate_same_sample_v5(
        rows,
        dates,
        _baseline(bundle),
        bundle,
        decision_dates=[dates[2]],
        liquidate_final_holdings=False,
    )
    detail = result['date_details'][0]
    assert detail['transition_kind'] == 'reentry_after_schedule_gap'
    assert detail['top_turnover'] == 1.0
    assert detail['bottom_turnover'] == 1.0
    assert result['average_top_turnover'] == 1.0
    assert result['turnover_cap_pass'] == 0


def test_monthly_gap_reentry_resets_both_sleeve_turnovers() -> None:
    bundle = _bundle()
    rows, dates = _gap_panel()
    result = evaluate_monthly_candidate_same_sample_v5(
        rows,
        dates,
        _baseline(bundle),
        bundle,
        decision_dates=[dates[2]],
        liquidate_final_holdings=False,
    )
    detail = result['date_details'][0]
    assert detail['transition_kind'] == 'reentry_after_schedule_gap'
    assert detail['top_turnover'] == 1.0
    assert detail['bottom_turnover'] == 1.0
    assert result['average_top_turnover'] == 1.0
    assert result['turnover_cap_pass'] == 0
