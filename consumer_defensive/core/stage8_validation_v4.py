from __future__ import annotations

from typing import Any, Mapping, Sequence

from .config import ConfigBundle
from .stage8_calibration import Candidate, HORIZONS
from .stage8_validation_v3 import evaluate_candidate_same_sample_v3


STAGE8_VALIDATION_V4 = (
    'consumer_defensive_stage8_same_sample_validation_v4'
)


def evaluate_candidate_same_sample_v4(
    rows: Sequence[Mapping[str, Any]],
    dates: Sequence[str],
    candidate: Candidate,
    bundle: ConfigBundle,
    *,
    required_factor_ids: Sequence[str] = (),
    decision_dates: Sequence[str] | None = None,
    liquidate_final_holdings: bool = True,
) -> dict[str, Any]:
    """Run V3 turnover while opening labels only for the decision dates."""

    decisions = set(
        str(value) for value in (
            dates if decision_dates is None else decision_dates
        )
    )
    sanitized: list[dict[str, Any]] = []
    for source in rows:
        row = dict(source)
        if str(row['asof_date']) not in decisions:
            for horizon in HORIZONS:
                row[f'forward_xlp_residual_return_{horizon}d'] = None
        sanitized.append(row)
    result = evaluate_candidate_same_sample_v3(
        sanitized,
        dates,
        candidate,
        bundle,
        required_factor_ids=required_factor_ids,
        decision_dates=decision_dates,
        liquidate_final_holdings=liquidate_final_holdings,
    )
    return {
        **result,
        'schema_version': STAGE8_VALIDATION_V4,
        'target_access_policy': (
            'nondecision_schedule_labels_redacted_before_evaluation'
        ),
    }
