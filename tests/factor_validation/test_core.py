from __future__ import annotations

import json
from datetime import date, timedelta

import pytest

from factor_validation import (
    CONTRACT_VERSION,
    FactorObservation,
    FactorValidationConfig,
    average_ranks,
    hac_lag_for_overlapping_labels,
    infer_evaluation_step_trading_days,
    newey_west_mean_inference,
    quantile_diagnostics,
    spearman_rank_correlation,
    validate_factor,
)


def test_average_ranks_and_spearman_are_tie_aware() -> None:
    assert average_ranks([30.0, 10.0, 10.0, 20.0]) == (4.0, 1.5, 1.5, 3.0)
    assert spearman_rank_correlation([1.0, 1.0, 2.0, 3.0], [10.0, 10.0, 20.0, 30.0]) == pytest.approx(1.0)
    assert spearman_rank_correlation([1.0, 2.0, 3.0], [30.0, 20.0, 10.0]) == pytest.approx(-1.0)
    assert spearman_rank_correlation([1.0, 1.0, 1.0], [10.0, 20.0, 30.0]) is None


def test_quantiles_keep_ties_together_and_charge_cost() -> None:
    factor = [0.0, 0.0, 1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 7.0]
    returns = [float(value) / 100.0 for value in factor]
    result = quantile_diagnostics(
        factor,
        returns,
        quantile_count=5,
        round_trip_cost=0.005,
        two_leg_turnover=1.0,
    )

    assert result.eligible is True
    assert result.bucket_counts == (2, 2, 2, 2, 2)
    assert result.gross_top_minus_bottom == pytest.approx(0.07)
    assert result.net_top_minus_bottom == pytest.approx(0.065)
    assert result.monotonicity == pytest.approx(1.0)


def test_overlap_lag_includes_entry_lag_and_actual_cadence() -> None:
    daily = [date(2026, 1, 5) + timedelta(days=offset) for offset in (0, 1, 2, 3, 4, 7, 8)]
    weekly = [date(2026, 1, 2) + timedelta(days=7 * offset) for offset in range(6)]

    assert infer_evaluation_step_trading_days(daily) == 1
    assert infer_evaluation_step_trading_days(weekly) == 5
    assert hac_lag_for_overlapping_labels(21, 1, entry_lag_trading_days=1) == 21
    assert hac_lag_for_overlapping_labels(21, 5, entry_lag_trading_days=1) == 4


def test_newey_west_inference_is_deterministic_and_overlap_aware() -> None:
    result = newey_west_mean_inference([1.0, 2.0, 3.0, 4.0, 5.0], max_lag=1)

    assert result.mean == pytest.approx(3.0)
    assert result.standard_error == pytest.approx(0.56 ** 0.5)
    assert result.t_stat == pytest.approx(3.0 / (0.56 ** 0.5))
    assert result.requested_max_lag == 1
    assert result.max_lag == 1
    assert result.minimum_recommended_observations == 10
    assert result.small_sample_adequate is False
    assert result.two_sided_p_value is None


def _observations(*, reverse_second_half: bool = False) -> list[FactorObservation]:
    observations: list[FactorObservation] = []
    start = date(2025, 1, 3)
    for date_index in range(12):
        as_of = start + timedelta(days=7 * date_index)
        regime = "heating" if date_index < 6 else "cooling"
        for entity_index in range(10):
            factor = float(entity_index // 2)
            forward = factor
            if reverse_second_half and date_index >= 7:
                forward = -forward
            observations.append(
                FactorObservation(
                    as_of_date=as_of,
                    entity_id=f"T{entity_index:02d}",
                    factor_value=factor,
                    forward_return=forward,
                    regime=regime,
                )
            )
    return observations


def test_validate_factor_builds_complete_deterministic_evidence() -> None:
    config = FactorValidationConfig(
        horizon_trading_days=21,
        min_cross_section=8,
        min_dates=10,
        min_regime_dates=3,
        round_trip_cost=0.001,
    )
    first = validate_factor(_observations(), factor_id="quality", config=config)
    second = validate_factor(reversed(_observations()), factor_id="quality", config=config)

    assert first == second
    assert CONTRACT_VERSION == "factor_validation_v1"
    assert first.contract_version == "factor_validation_v1"
    assert first.ic_date_count == 12
    assert first.mean_ic == pytest.approx(1.0)
    assert first.hit_rate == pytest.approx(1.0)
    assert first.chronological_half_sign_stable is True
    assert first.regime_sign_stable is True
    assert first.mean_rank_persistence == pytest.approx(1.0)
    assert first.mean_top_bucket_turnover == pytest.approx(0.0)
    assert first.hac.evaluation_step_trading_days == 5
    assert first.hac.max_lag == 4
    assert first.independent_window.independent_window_count == 3
    assert first.evidence_eligible is False  # constant IC series has no estimable standard error
    assert first.primary_inference == "independent_window"
    assert first.insufficiency_reasons == ("independent_window_inference_unavailable",)
    encoded = json.dumps(first.to_dict(), sort_keys=True, allow_nan=False)
    assert json.loads(encoded)["contract_version"] == "factor_validation_v1"
    assert encoded == json.dumps(
        second.to_dict(), sort_keys=True, allow_nan=False
    )


def test_validate_factor_detects_half_and_regime_sign_instability() -> None:
    result = validate_factor(
        _observations(reverse_second_half=True),
        factor_id="unstable_factor",
        config=FactorValidationConfig(
            horizon_trading_days=21,
            min_cross_section=8,
            min_dates=10,
            min_regime_dates=3,
        ),
    )

    assert result.half1_mean_ic is not None and result.half1_mean_ic > 0.0
    assert result.half2_mean_ic is not None and result.half2_mean_ic < 0.0
    assert result.mean_ic is not None and result.mean_ic > 0.0
    assert result.chronological_half_sign_stable is False
    assert result.regime_sign_stable is False


def test_validation_fails_closed_on_duplicates_and_conflicting_regimes() -> None:
    duplicate = [
        FactorObservation("2026-01-02", "ABC", 1.0, 0.1),
        FactorObservation("2026-01-02", "abc", 2.0, 0.2),
    ]
    with pytest.raises(ValueError, match="duplicate"):
        validate_factor(
            duplicate,
            factor_id="duplicate",
            config=FactorValidationConfig(horizon_trading_days=21, min_cross_section=4),
        )

    conflicting = [
        FactorObservation("2026-01-02", "A", 1.0, 0.1, "risk_on"),
        FactorObservation("2026-01-02", "B", 2.0, 0.2, "risk_off"),
        FactorObservation("2026-01-02", "C", 3.0, 0.3, "risk_on"),
    ]
    with pytest.raises(ValueError, match="multiple regimes"):
        validate_factor(
            conflicting,
            factor_id="regime_conflict",
            config=FactorValidationConfig(horizon_trading_days=21, min_cross_section=4),
        )
