from __future__ import annotations

import copy
from pathlib import Path

from consumer_defensive.core.config import ConfigBundle, load_config
from consumer_defensive.core.stage8_monthly_target_v6 import (
    evaluate_monthly_candidate_same_sample_v6,
)
from consumer_defensive.core.stage8_validation_v6 import (
    evaluate_candidate_same_sample_v6,
)


ROOT = Path(__file__).resolve().parents[2]
CONFIG = ROOT / 'consumer_defensive' / 'config.yaml'


def _bundle() -> ConfigBundle:
    original = load_config(CONFIG)
    payload = copy.deepcopy(original.payload)
    payload['stage8_calibration']['maximum_top_turnover'] = 0.60
    return ConfigBundle(original.path, original.base_dir, payload)


def _underlying_result() -> dict[str, object]:
    return {
        'average_top_turnover': 0.10,
        'average_bottom_turnover': 0.80,
        'average_trade_notional_turnover': 0.90,
        'candidate_quality_constraint_pass': 1,
        'cohort_concentration_cap_pass': 1,
        'constraint_pass': 1,
        'turnover_cap_pass': 1,
        'date_details': [],
    }


def test_fixed_horizon_rejects_bottom_sleeve_breach(monkeypatch) -> None:
    monkeypatch.setattr(
        'consumer_defensive.core.stage8_validation_v6.'
        'evaluate_candidate_same_sample_v5',
        lambda *_args, **_kwargs: _underlying_result(),
    )
    result = evaluate_candidate_same_sample_v6(
        [], [], object(), _bundle()
    )
    assert result['top_turnover_cap_pass'] == 1
    assert result['bottom_turnover_cap_pass'] == 0
    assert result['turnover_cap_pass'] == 0
    assert result['constraint_pass'] == 0


def test_monthly_rejects_bottom_sleeve_breach(monkeypatch) -> None:
    monkeypatch.setattr(
        'consumer_defensive.core.stage8_monthly_target_v6.'
        'evaluate_monthly_candidate_same_sample_v5',
        lambda *_args, **_kwargs: _underlying_result(),
    )
    result = evaluate_monthly_candidate_same_sample_v6(
        [], [], object(), _bundle()
    )
    assert result['top_turnover_cap_pass'] == 1
    assert result['bottom_turnover_cap_pass'] == 0
    assert result['trade_notional_turnover_cap_pass'] == 1
    assert result['turnover_cap_pass'] == 0
    assert result['constraint_pass'] == 0


def test_costed_trade_notional_breach_fails_even_when_sleeves_pass(
    monkeypatch,
) -> None:
    payload = _underlying_result()
    payload.update({
        'average_top_turnover': 0.55,
        'average_bottom_turnover': 0.55,
        'average_trade_notional_turnover': 1.30,
    })
    monkeypatch.setattr(
        'consumer_defensive.core.stage8_validation_v6.'
        'evaluate_candidate_same_sample_v5',
        lambda *_args, **_kwargs: payload,
    )
    result = evaluate_candidate_same_sample_v6(
        [], [], object(), _bundle()
    )
    assert result['top_turnover_cap_pass'] == 1
    assert result['bottom_turnover_cap_pass'] == 1
    assert result['aggregate_sleeve_turnover_cap_pass'] == 1
    assert result['trade_notional_turnover_cap_pass'] == 0
    assert result['constraint_pass'] == 0
