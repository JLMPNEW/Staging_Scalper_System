"""Symmetric long/short turnover constraints for Stage 8 evidence."""

from __future__ import annotations

from typing import Any, Mapping

from .config import ConfigBundle, cfg_get


def apply_symmetric_turnover_gate_v6(
    result: Mapping[str, Any], bundle: ConfigBundle
) -> dict[str, Any]:
    cap = float(cfg_get(
        bundle.payload, 'stage8_calibration.maximum_top_turnover'
    ))
    average_top = float(result['average_top_turnover'])
    average_bottom = float(result['average_bottom_turnover'])
    average_trade = float(result['average_trade_notional_turnover'])
    top_pass = average_top <= cap
    bottom_pass = average_bottom <= cap
    aggregate_sleeve = average_top + average_bottom
    aggregate_pass = aggregate_sleeve <= 2.0 * cap
    trade_pass = average_trade <= 2.0 * cap
    turnover_pass = top_pass and bottom_pass and aggregate_pass and trade_pass
    quality_pass = int(result['candidate_quality_constraint_pass']) == 1
    concentration_pass = int(result['cohort_concentration_cap_pass']) == 1
    return {
        **dict(result),
        'maximum_turnover_per_sleeve': cap,
        'maximum_trade_notional_turnover': 2.0 * cap,
        'average_aggregate_sleeve_turnover': aggregate_sleeve,
        'top_turnover_cap_pass': int(top_pass),
        'bottom_turnover_cap_pass': int(bottom_pass),
        'aggregate_sleeve_turnover_cap_pass': int(aggregate_pass),
        'trade_notional_turnover_cap_pass': int(trade_pass),
        'turnover_cap_pass': int(turnover_pass),
        'constraint_pass': int(
            turnover_pass and quality_pass and concentration_pass
        ),
        'turnover_constraint_policy': (
            'symmetric_top_bottom_aggregate_and_costed_l1_trade_notional'
        ),
    }


__all__ = ['apply_symmetric_turnover_gate_v6']
