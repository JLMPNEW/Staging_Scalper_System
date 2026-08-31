from __future__ import annotations

import statistics
from typing import Any, Mapping, Sequence

from .config import ConfigBundle, cfg_get
from .stage8_calibration import Candidate
from .stage8_monthly_target_v2 import (
    MONTHLY_TARGET_FIELD,
    build_next_rebalance_target_panel,
    fail_closed_monthly_absolute_gate,
)
from .stage8_monthly_target_v3 import (
    monthly_plan_sha256,
    validate_preregistered_monthly_plan_v3,
)
from .stage8_monthly_target_v4 import (
    evaluate_monthly_candidate_same_sample_v4,
)


STAGE8_MONTHLY_TARGET_V5 = 'consumer_defensive_stage8_monthly_target_v5'


def evaluate_monthly_candidate_same_sample_v5(
    rows: Sequence[Mapping[str, Any]],
    dates: Sequence[str],
    candidate: Candidate,
    bundle: ConfigBundle,
    *,
    required_factor_ids: Sequence[str] = (),
    decision_dates: Sequence[str] | None = None,
    liquidate_final_holdings: bool = True,
) -> dict[str, Any]:
    result = evaluate_monthly_candidate_same_sample_v4(
        rows,
        dates,
        candidate,
        bundle,
        required_factor_ids=required_factor_ids,
        decision_dates=decision_dates,
        liquidate_final_holdings=liquidate_final_holdings,
    )
    details = list(result['date_details'])
    for detail in details:
        if str(detail.get('transition_kind')) == 'reentry_after_schedule_gap':
            detail['top_turnover'] = 1.0
            detail['bottom_turnover'] = 1.0
            detail['turnover_diagnostic_kind'] = 'full_sleeve_reentry_after_cash_gap'
    transitions = [
        row for row in details
        if str(row.get('transition_kind')) != 'initial_entry'
    ]
    average_top = (
        statistics.fmean(float(row['top_turnover']) for row in transitions)
        if transitions else 0.0
    )
    average_bottom = (
        statistics.fmean(float(row['bottom_turnover']) for row in transitions)
        if transitions else 0.0
    )
    turnover_pass = average_top <= float(cfg_get(
        bundle.payload, 'stage8_calibration.maximum_top_turnover'
    ))
    quality_pass = int(result['candidate_quality_constraint_pass']) == 1
    concentration_pass = int(result['cohort_concentration_cap_pass']) == 1
    return {
        **result,
        'schema_version': STAGE8_MONTHLY_TARGET_V5,
        'average_top_turnover': average_top,
        'average_bottom_turnover': average_bottom,
        'turnover_cap_pass': int(turnover_pass),
        'constraint_pass': int(
            turnover_pass and quality_pass and concentration_pass
        ),
        'gap_reentry_turnover_policy': 'reset_both_sleeves_to_cash_then_full_entry',
        'date_details': details,
    }


__all__ = [
    'MONTHLY_TARGET_FIELD',
    'build_next_rebalance_target_panel',
    'evaluate_monthly_candidate_same_sample_v5',
    'fail_closed_monthly_absolute_gate',
    'monthly_plan_sha256',
    'validate_preregistered_monthly_plan_v3',
]
