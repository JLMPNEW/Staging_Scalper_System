from __future__ import annotations

from typing import Any, Mapping, Sequence

from .config import ConfigBundle
from .stage8_calibration import Candidate
from .stage8_turnover_gate_v6 import apply_symmetric_turnover_gate_v6
from .stage8_validation_v5 import evaluate_candidate_same_sample_v5


STAGE8_VALIDATION_V6 = (
    'consumer_defensive_stage8_same_sample_validation_v6'
)


def evaluate_candidate_same_sample_v6(
    rows: Sequence[Mapping[str, Any]],
    dates: Sequence[str],
    candidate: Candidate,
    bundle: ConfigBundle,
    *,
    required_factor_ids: Sequence[str] = (),
    decision_dates: Sequence[str] | None = None,
    liquidate_final_holdings: bool = True,
) -> dict[str, Any]:
    result = evaluate_candidate_same_sample_v5(
        rows,
        dates,
        candidate,
        bundle,
        required_factor_ids=required_factor_ids,
        decision_dates=decision_dates,
        liquidate_final_holdings=liquidate_final_holdings,
    )
    return {
        **apply_symmetric_turnover_gate_v6(result, bundle),
        'schema_version': STAGE8_VALIDATION_V6,
    }


__all__ = ['evaluate_candidate_same_sample_v6']
