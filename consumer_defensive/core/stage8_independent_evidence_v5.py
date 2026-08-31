from __future__ import annotations

from typing import Any, Mapping, Sequence

from .config import ConfigBundle
from .stage8_calibration import Candidate
from .stage8_independent_evidence_v3 import (
    independent_evidence_gate_v3,
    independent_horizon_evidence_v3,
)
from .stage8_validation_v5 import evaluate_candidate_same_sample_v5


STAGE8_INDEPENDENT_EVIDENCE_V5 = (
    'consumer_defensive_stage8_independent_evidence_v5'
)


def absolute_baseline_independent_evidence_v5(
    rows: Sequence[Mapping[str, Any]],
    dates: Sequence[str],
    baseline: Candidate,
    bundle: ConfigBundle,
    *,
    calendar: Sequence[str],
    entry_lag: int,
    schedule_dates: Sequence[str] | None = None,
) -> dict[str, Any]:
    evaluation = evaluate_candidate_same_sample_v5(
        rows,
        list(schedule_dates) if schedule_dates is not None else dates,
        baseline,
        bundle,
        decision_dates=dates,
        liquidate_final_holdings=True,
    )
    independent = independent_horizon_evidence_v3(
        evaluation, calendar, entry_lag=entry_lag
    )
    return {
        **{
            key: value for key, value in evaluation.items()
            if key != 'date_details'
        },
        **independent,
        'schema_version': STAGE8_INDEPENDENT_EVIDENCE_V5,
        'validation_kind': (
            'absolute_frozen_baseline_calendar_independent_efficacy_v5'
        ),
    }


def independent_evidence_gate_v5(
    evidence: Mapping[str, Any],
    *,
    minimum_independent_dates: Mapping[int, int],
    maximum_sign_pvalue: float,
    invariants: Mapping[str, bool],
) -> dict[str, Any]:
    result = independent_evidence_gate_v3(
        evidence,
        minimum_independent_dates=minimum_independent_dates,
        maximum_sign_pvalue=maximum_sign_pvalue,
        invariants=invariants,
    )
    return {
        **result,
        'schema_version': STAGE8_INDEPENDENT_EVIDENCE_V5,
    }
