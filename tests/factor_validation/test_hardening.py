from __future__ import annotations

import json
from datetime import date, timedelta

import numpy as np
import pytest

from factor_validation import (
    FactorObservation,
    FactorValidationConfig,
    evaluation_cadence,
    independent_window_mean_inference,
    newey_west_mean_inference,
    quantile_diagnostics,
    validate_factor,
)
from factor_validation.core import _json_safe


def test_strict_dates_and_numpy_json_values_fail_closed() -> None:
    for malformed in ("2026-01-02|AAPL", "2026-01-02T12:00:00", " 2026-01-02"):
        with pytest.raises(ValueError, match="ISO"):
            FactorObservation(malformed, "ABC", 1.0, 0.1)

    safe = _json_safe({"integer": np.int64(3), "floating": np.float64(0.25)})
    assert json.loads(json.dumps(safe, allow_nan=False)) == {"integer": 3, "floating": 0.25}
    with pytest.raises(ValueError, match="non-finite"):
        _json_safe(np.float64(np.inf))


def test_tied_extremes_and_two_bucket_monotonicity_are_valid() -> None:
    tied = quantile_diagnostics(
        [0, 0, 1, 2, 3, 4, 5, 5, 5, 5, 5, 5],
        list(range(12)),
        quantile_count=5,
    )
    assert tied.eligible is True
    assert tied.bucket_counts[0] == 2 and tied.bucket_counts[-1] == 6
    assert tied.gross_top_minus_bottom == pytest.approx(8.0)
    assert tied.monotonicity == pytest.approx(1.0)

    sparse = quantile_diagnostics(
        [0, 1, 2, 3, 4, 5, 5, 5, 5, 5],
        list(range(10)),
        quantile_count=5,
    )
    assert sparse.eligible is False
    assert sparse.failure_reason == "sparse_extreme_bucket"

    two_bucket = quantile_diagnostics(list(range(10)), list(range(10)), quantile_count=2)
    assert two_bucket.monotonicity == pytest.approx(1.0)

    constant = quantile_diagnostics([1.0] * 10, list(range(10)), quantile_count=5)
    assert constant.eligible is False
    assert constant.failure_reason == "constant_factor"


def test_cadence_uses_minimum_business_gap_and_records_distribution() -> None:
    every_other_trading_day = [
        date(2026, 1, 5),
        date(2026, 1, 7),
        date(2026, 1, 9),
        date(2026, 1, 13),
        date(2026, 1, 15),
    ]
    cadence = evaluation_cadence(every_other_trading_day)
    assert cadence.minimum_step_trading_days == 2
    assert cadence.median_step_trading_days == pytest.approx(2.0)
    assert cadence.gap_distribution == ((2, 4),)

    mixed = [date(2026, 1, 2), date(2026, 2, 2), date(2026, 3, 2), date(2026, 3, 3), date(2026, 4, 2)]
    mixed_cadence = evaluation_cadence(mixed)
    assert mixed_cadence.minimum_step_trading_days == 1
    assert mixed_cadence.maximum_step_trading_days > 10
    assert sum(count for _gap, count in mixed_cadence.gap_distribution) == 4


def test_hac_truncation_and_small_samples_never_emit_gating_p_values() -> None:
    result = newey_west_mean_inference([0.1, -0.1, 0.2, -0.2, 0.1, -0.1], max_lag=10)

    assert result.requested_max_lag == 10
    assert result.max_lag == 4
    assert result.lag_truncated is True
    assert result.small_sample_adequate is False
    assert result.two_sided_p_value is None


def test_independent_window_null_rejection_is_bounded() -> None:
    rng = np.random.default_rng(20260807)
    dates = [date(2026, 1, 2) + timedelta(days=7 * index) for index in range(12)]
    rejections = 0
    simulations = 1_000
    for _simulation in range(simulations):
        innovations = rng.normal(size=12)
        values = np.empty(12)
        values[0] = innovations[0] / np.sqrt(1.0 - 0.8**2)
        for index in range(1, 12):
            values[index] = 0.8 * values[index - 1] + innovations[index]
        inference = independent_window_mean_inference(
            values.tolist(),
            dates,
            horizon_trading_days=21,
            entry_lag_trading_days=1,
        )
        rejections += inference.two_sided_p_value is not None and inference.two_sided_p_value < 0.05

    assert rejections / simulations <= 0.08


def _transition_observations() -> list[FactorObservation]:
    rows: list[FactorObservation] = []
    dates = [date(2026, 1, 5), date(2026, 1, 6), date(2026, 1, 20)]
    for date_index, as_of in enumerate(dates):
        for entity_index in range(10):
            factor = float(entity_index if date_index == 0 else 9 - entity_index)
            rows.append(
                FactorObservation(
                    as_of,
                    f"T{entity_index:02d}",
                    factor,
                    factor / 100.0,
                )
            )
    return rows


def test_turnover_costs_use_both_legs_and_persistence_skips_dropped_gaps() -> None:
    result = validate_factor(
        _transition_observations(),
        factor_id="turnover_factor",
        config=FactorValidationConfig(
            horizon_trading_days=1,
            min_cross_section=8,
            min_dates=3,
            min_independent_windows=2,
            round_trip_cost=0.01,
        ),
    )

    first, second, after_gap = result.per_date
    assert first.net_top_minus_bottom is None
    assert second.top_bucket_turnover == pytest.approx(1.0)
    assert second.two_leg_turnover == pytest.approx(2.0)
    assert second.gross_top_minus_bottom is not None
    assert second.net_top_minus_bottom == pytest.approx(second.gross_top_minus_bottom - 0.02)
    assert after_gap.top_bucket_turnover is None
    assert after_gap.two_leg_turnover is None
    assert result.mean_top_bucket_turnover == pytest.approx(1.0)
    assert result.mean_two_leg_turnover == pytest.approx(2.0)
    assert result.mean_rank_persistence == pytest.approx(-1.0)


def test_rejected_quantile_dates_do_not_create_turnover() -> None:
    rows: list[FactorObservation] = []
    for date_index, count in enumerate((10, 6, 10)):
        as_of = date(2026, 2, 2) + timedelta(days=date_index)
        for entity_index in range(count):
            rows.append(FactorObservation(as_of, f"T{entity_index:02d}", entity_index, entity_index / 100.0))
    result = validate_factor(
        rows,
        factor_id="coverage_factor",
        config=FactorValidationConfig(
            horizon_trading_days=1,
            min_cross_section=3,
            min_dates=3,
            min_independent_windows=2,
        ),
    )

    assert result.per_date[1].quantile_eligible is False
    assert result.per_date[1].quantile_failure_reason == "insufficient_quantile_observations"
    assert result.mean_top_bucket_turnover is None
    assert result.mean_two_leg_turnover is None


def test_non_finite_intermediate_portfolio_statistics_are_rejected() -> None:
    with pytest.raises(ValueError, match="non-finite"):
        quantile_diagnostics(
            list(range(10)),
            [-1e308] * 5 + [1e308] * 5,
            quantile_count=5,
        )

    assert FactorValidationConfig(horizon_trading_days=21).primary_inference == "independent_window"
    with pytest.raises(ValueError, match="diagnostic-only"):
        FactorValidationConfig(horizon_trading_days=21, primary_inference="hac")  # type: ignore[arg-type]


def _monthly_signal_observations(
    *,
    month_count: int = 30,
    entity_count: int = 12,
    daily_cluster: int = 0,
    drop_wednesday: bool = False,
) -> list[FactorObservation]:
    dates: list[date] = [
        date(2023 + month // 12, month % 12 + 1, 15) for month in range(month_count)
    ]
    if daily_cluster:
        anchor = date(2026, 3, 2)
        dates += [anchor + timedelta(days=offset) for offset in range(daily_cluster)]
    if drop_wednesday:
        dates = [item for item in dates if item != date(2026, 3, 4)]
    observations: list[FactorObservation] = []
    for date_index, as_of in enumerate(sorted(dates)):
        for entity in range(entity_count):
            factor = float((entity * 7 + date_index * 3) % entity_count)
            disorder = float((entity * 13 + date_index * 5) % 7 - 3) * 0.3
            observations.append(
                FactorObservation(
                    as_of,
                    f"E{entity:02d}",
                    factor,
                    factor * 0.01 + disorder * 0.02,
                    regime="expansion" if date_index % 2 == 0 else "contraction",
                )
            )
    return observations


def test_happy_path_yields_eligible_evidence_with_gating_p_value() -> None:
    config = FactorValidationConfig(horizon_trading_days=21, round_trip_cost=0.001)
    result = validate_factor(_monthly_signal_observations(), factor_id="HAPPY", config=config)

    assert result.evidence_eligible is True
    assert result.insufficiency_reasons == ()
    assert result.primary_inference == "independent_window"
    assert result.primary_p_value is not None and result.primary_p_value < 0.05
    assert result.mean_ic is not None and result.mean_ic > 0.5
    assert result.independent_window.independent_window_count >= 3
    # Matched-set identity: gross(matched) - net = cost x mean two-leg turnover.
    assert result.mean_gross_top_minus_bottom_matched is not None
    assert result.mean_net_top_minus_bottom is not None
    assert result.mean_two_leg_turnover is not None
    assert result.mean_two_leg_turnover > 0.0
    assert result.mean_gross_top_minus_bottom_matched - result.mean_net_top_minus_bottom == pytest.approx(
        config.round_trip_cost * result.mean_two_leg_turnover
    )
    json.dumps(result.to_dict(), sort_keys=True, allow_nan=False)


def test_canonical_iso_dates_and_datetimes_are_accepted() -> None:
    from datetime import datetime

    accepted = FactorObservation("2026-01-02", "AAPL", 1.0, 0.5)
    assert accepted.as_of_date == date(2026, 1, 2)
    assert FactorObservation(datetime(2026, 1, 2, 15, 30), "AAPL", 1.0, 0.5).as_of_date == date(2026, 1, 2)
    assert FactorObservation(date(2026, 1, 2), "AAPL", 1.0, 0.5).as_of_date == date(2026, 1, 2)


def test_bucket_assignment_mirrors_under_negation() -> None:
    from factor_validation.core import _bucket_assignments

    for quantile_count, values in ((4, [0.0, 1.0, 2.0, 3.0]), (5, [0.0, 1.0, 2.0, 3.0])):
        forward = _bucket_assignments(values, quantile_count)
        mirrored = _bucket_assignments([-value for value in values], quantile_count)
        assert mirrored == tuple(quantile_count - 1 - bucket for bucket in forward)

    # Midpoint half-tie groups (positions 0.5 and 1.5 on the 0..2 bucket scale)
    # must break toward the center AND stay mirror-consistent; plain half-up
    # rounding fails this exact case.
    tie_case = _bucket_assignments([0.0, 1.0, 2.0, 3.0, 4.0], 3)
    assert tie_case == (0, 1, 1, 1, 2)
    tie_mirrored = _bucket_assignments([0.0, -1.0, -2.0, -3.0, -4.0], 3)
    assert tie_mirrored == tuple(2 - bucket for bucket in tie_case)


def test_hac_lag_cannot_be_undercut_by_config() -> None:
    from factor_validation import hac_lag_for_overlapping_labels

    config = FactorValidationConfig(horizon_trading_days=21, hac_max_lag=0)
    result = validate_factor(_monthly_signal_observations(), factor_id="FLOOR", config=config)
    implied = hac_lag_for_overlapping_labels(
        21,
        result.evaluation_cadence.minimum_step_trading_days,
        entry_lag_trading_days=config.entry_lag_trading_days,
    )
    assert result.hac.requested_max_lag == implied


def test_declared_holidays_refine_business_day_gaps() -> None:
    without = evaluation_cadence(["2026-07-02", "2026-07-06"])
    with_holiday = evaluation_cadence(
        ["2026-07-02", "2026-07-06"], holidays=(date(2026, 7, 3),)
    )
    assert without.minimum_step_trading_days == 2
    assert with_holiday.minimum_step_trading_days == 1


def test_mixed_cadence_keeps_sparse_transitions_and_min_lag() -> None:
    result = validate_factor(
        _monthly_signal_observations(daily_cluster=5),
        factor_id="MIXED",
        config=FactorValidationConfig(horizon_trading_days=21, round_trip_cost=0.001),
    )
    assert result.evaluation_cadence.minimum_step_trading_days == 1
    measured = [item for item in result.per_date if item.two_leg_turnover is not None]
    # Median-based adjacency keeps the monthly transitions measurable; the old
    # minimum-based rule collapsed this to the daily cluster only.
    assert len(measured) > 25


def test_single_dropped_daily_date_is_not_spanned() -> None:
    weekdays = [date(2026, 1, 5) + timedelta(days=offset) for offset in range(9) if (date(2026, 1, 5) + timedelta(days=offset)).weekday() < 5]
    kept = [item for item in weekdays if item != date(2026, 1, 7)]
    observations = []
    for date_index, as_of in enumerate(kept):
        for entity in range(10):
            factor = float((entity * 3 + date_index) % 10)
            observations.append(
                FactorObservation(as_of, f"E{entity:02d}", factor, factor * 0.01)
            )
    result = validate_factor(
        observations,
        factor_id="GAP",
        config=FactorValidationConfig(horizon_trading_days=21, min_dates=3, round_trip_cost=0.001),
    )
    by_date = {item.as_of_date: item for item in result.per_date}
    assert by_date[date(2026, 1, 8)].two_leg_turnover is None
    assert by_date[date(2026, 1, 9)].two_leg_turnover is not None


def test_mixed_cadence_does_not_span_a_dropped_daily_cluster_date() -> None:
    result = validate_factor(
        _monthly_signal_observations(daily_cluster=5, drop_wednesday=True),
        factor_id="MIXED_GAP",
        config=FactorValidationConfig(horizon_trading_days=21, round_trip_cost=0.001),
    )
    by_date = {item.as_of_date: item for item in result.per_date}
    assert result.evaluation_cadence.median_step_trading_days > 10
    assert by_date[date(2026, 3, 5)].two_leg_turnover is None
    assert by_date[date(2026, 3, 6)].two_leg_turnover is not None


def test_primary_p_value_fails_closed_below_configured_evidence_minimums() -> None:
    rng = np.random.default_rng(8)
    observations: list[FactorObservation] = []
    for date_index in range(8):
        as_of = date(2026, 1, 5) + timedelta(days=7 * date_index)
        factors = np.arange(20, dtype=float)
        returns = (0.2 + 0.1 * date_index) * factors + rng.normal(0.0, 5.0, size=20)
        for entity_index in range(20):
            observations.append(
                FactorObservation(as_of, f"E{entity_index}", factors[entity_index], returns[entity_index])
            )
    result = validate_factor(
        observations,
        factor_id="BELOW_MINIMUM",
        config=FactorValidationConfig(
            horizon_trading_days=21,
            min_dates=3,
            min_independent_windows=3,
        ),
    )
    assert result.independent_window.independent_window_count == 2
    assert result.independent_window.two_sided_p_value is not None
    assert result.evidence_eligible is False
    assert result.primary_p_value is None


@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        ("horizon_trading_days", 21.5),
        ("horizon_trading_days", np.nan),
        ("quantile_count", 5.5),
        ("hac_max_lag", np.nan),
    ],
)
def test_configuration_rejects_non_integer_count_fields(field_name: str, value: float) -> None:
    kwargs = {"horizon_trading_days": 21, field_name: value}
    with pytest.raises(TypeError, match="integer"):
        FactorValidationConfig(**kwargs)  # type: ignore[arg-type]


def test_exported_inference_helpers_reject_invalid_window_parameters() -> None:
    dates = [date(2026, 1, 2), date(2026, 1, 9)]
    with pytest.raises(ValueError, match="positive"):
        independent_window_mean_inference(
            [0.1, 0.2], dates, horizon_trading_days=0, entry_lag_trading_days=0
        )
    with pytest.raises(ValueError, match="at least 0"):
        independent_window_mean_inference(
            [0.1, 0.2], dates, horizon_trading_days=1, entry_lag_trading_days=-1
        )
    with pytest.raises(ValueError, match="positive"):
        newey_west_mean_inference(
            [0.1, 0.2, 0.3], max_lag=0, evaluation_step_trading_days=0
        )


def test_zero_dimensional_numpy_arrays_serialize_or_fail_cleanly() -> None:
    assert _json_safe(np.array(1.5)) == 1.5
    assert _json_safe(np.array(3, dtype=np.int64)) == 3
    with pytest.raises(ValueError, match="non-finite"):
        _json_safe(np.array(np.nan))
