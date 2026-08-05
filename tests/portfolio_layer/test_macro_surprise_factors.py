from __future__ import annotations

# pyright: reportMissingImports=false

import random
import sys
from datetime import date, timedelta
from pathlib import Path
from statistics import stdev

PROJECT_ROOT = Path(__file__).resolve().parents[2]
MACRO_LAYER_ROOT = PROJECT_ROOT / "portfolio_layer" / "MacroLayer"
if str(MACRO_LAYER_ROOT) not in sys.path:
    sys.path.insert(0, str(MACRO_LAYER_ROOT))

import build_macro_surprise_factors as bmsf  # noqa: E402


def _monthly_periods(n: int, start_year: int = 2015) -> list[str]:
    periods: list[str] = []
    year, month = start_year, 1
    for _ in range(n):
        periods.append(date(year, month, 1).isoformat())
        month += 1
        if month > 12:
            month = 1
            year += 1
    return periods


def _monthly_availability(periods: list[str], lag_days: int = 40) -> list[date]:
    return [date.fromisoformat(p) + timedelta(days=lag_days) for p in periods]


def _ar1_series(n: int, *, intercept: float, slope: float, y0: float, noise_std: float, seed: int = 42) -> list[float]:
    rng = random.Random(seed)
    values = [y0]
    for _ in range(n - 1):
        noise = rng.gauss(0.0, noise_std) if noise_std > 0.0 else 0.0
        values.append(intercept + slope * values[-1] + noise)
    return values


def test_ar1_expectation_recovers_known_coefficients() -> None:
    intercept, slope = 2.0, 0.8
    n = 40
    periods = _monthly_periods(n)
    availability = _monthly_availability(periods)
    values = _ar1_series(n, intercept=intercept, slope=slope, y0=100.0, noise_std=0.0)

    rows = bmsf.build_metric_surprise_rows(
        metric_key="synthetic_ar1",
        periods=periods,
        availability_dates=availability,
        first_prints=values,
        frequency="monthly",
    )

    assert len(rows) == n
    for i in range(bmsf.MIN_OBSERVATIONS_FOR_EXPECTATION):
        assert rows[i].expectation is None
        assert rows[i].expectation_model is None
        assert rows[i].surprise is None
        assert rows[i].surprise_z is None
    for i in range(bmsf.MIN_OBSERVATIONS_FOR_EXPECTATION, n):
        row = rows[i]
        assert row.expectation_model == "ar1"
        assert row.expectation is not None
        # On noiseless AR(1) data the expanding OLS must recover (intercept, slope) exactly,
        # so the one-step expectation equals intercept + slope * previous_value == actual.
        assert abs(row.expectation - (intercept + slope * values[i - 1])) < 1e-6
        assert row.surprise is not None
        assert abs(row.surprise) < 1e-6


def test_surprise_z_uses_only_strictly_earlier_surprises() -> None:
    n = 60
    periods = _monthly_periods(n)
    availability = _monthly_availability(periods)
    values = _ar1_series(n, intercept=1.0, slope=0.6, y0=25.0, noise_std=2.0, seed=7)

    rows = bmsf.build_metric_surprise_rows(
        metric_key="synthetic_noisy",
        periods=periods,
        availability_dates=availability,
        first_prints=values,
        frequency="monthly",
    )

    min_obs = bmsf.MIN_OBSERVATIONS_FOR_EXPECTATION
    min_z = bmsf.MIN_PRIOR_SURPRISES_FOR_Z
    surprises = [row.surprise for row in rows]
    # Surprises begin exactly at min_obs; z stays NULL until min_z strictly-earlier surprises exist.
    for i in range(min_obs, min_obs + min_z):
        assert surprises[i] is not None
        assert rows[i].surprise_z is None
    first_z_index = min_obs + min_z
    row = rows[first_z_index]
    assert row.surprise is not None and row.surprise_z is not None
    prior = [s for s in surprises[min_obs:first_z_index] if s is not None]
    assert len(prior) == min_z
    assert abs(row.surprise_z - row.surprise / stdev(prior)) < 1e-9


def test_no_lookahead_later_data_cannot_change_earlier_rows() -> None:
    n = 60
    periods = _monthly_periods(n)
    availability = _monthly_availability(periods)
    values = _ar1_series(n, intercept=1.0, slope=0.6, y0=25.0, noise_std=2.0, seed=11)

    base = bmsf.build_metric_surprise_rows(
        metric_key="synthetic_noisy",
        periods=periods,
        availability_dates=availability,
        first_prints=values,
        frequency="monthly",
    )
    shocked_values = list(values)
    shocked_values[-1] += 500.0
    shocked = bmsf.build_metric_surprise_rows(
        metric_key="synthetic_noisy",
        periods=periods,
        availability_dates=availability,
        first_prints=shocked_values,
        frequency="monthly",
    )
    # Only the final row's availability changes anything: every earlier row is identical.
    assert base[:-1] == shocked[:-1]
    assert shocked[-1].surprise is not None and base[-1].surprise is not None
    assert shocked[-1].surprise != base[-1].surprise


def test_same_availability_group_is_excluded_from_expectation_window() -> None:
    n = 50
    periods = _monthly_periods(n)
    availability = _monthly_availability(periods)
    availability[41] = availability[40]  # two first prints released at the same vintage date
    values = _ar1_series(n, intercept=1.0, slope=0.6, y0=25.0, noise_std=2.0, seed=13)

    base = bmsf.build_metric_surprise_rows(
        metric_key="synthetic_group",
        periods=periods,
        availability_dates=availability,
        first_prints=values,
        frequency="monthly",
    )
    changed_values = list(values)
    changed_values[41] += 100.0
    changed = bmsf.build_metric_surprise_rows(
        metric_key="synthetic_group",
        periods=periods,
        availability_dates=availability,
        first_prints=changed_values,
        frequency="monthly",
    )
    # The same-date sibling row is untouched, and the changed row's own expectation is
    # unaffected by its own value (expectations only use strictly-earlier availability).
    assert base[40] == changed[40]
    assert base[41].expectation is not None and changed[41].expectation is not None
    assert abs(base[41].expectation - changed[41].expectation) < 1e-12
    assert base[41].surprise is not None and changed[41].surprise is not None
    assert abs((changed[41].surprise - base[41].surprise) - 100.0) < 1e-9


def test_weekly_seasonal_naive_baseline_column() -> None:
    n = 56
    start = date(2020, 1, 4)  # Saturday week-ending periods
    periods = [(start + timedelta(weeks=k)).isoformat() for k in range(n)]
    availability = [date.fromisoformat(p) + timedelta(days=5) for p in periods]
    values = [100.0 + float(k) for k in range(n)]

    rows = bmsf.build_metric_surprise_rows(
        metric_key="synthetic_weekly",
        periods=periods,
        availability_dates=availability,
        first_prints=values,
        frequency="weekly",
    )
    for i in range(52):
        assert rows[i].seasonal_naive_expectation is None
    for i in range(52, n):
        assert rows[i].seasonal_naive_expectation == values[i - 52]


def test_decay_index_halves_after_60_days() -> None:
    d0 = date(2024, 1, 2)
    calendar = [d0 + timedelta(days=k) for k in range(75)]
    impulses = [("growth_now", "m1", d0, 2.0)]
    rows = bmsf.build_surprise_index_daily(impulses, calendar, end_date=d0 + timedelta(days=70))
    by_date = {row[0]: row for row in rows}

    day0 = by_date[d0.isoformat()]
    assert day0[1] == "growth_now"
    assert abs(day0[2] - 2.0) < 1e-12
    assert day0[3] == 1

    day60 = by_date[(d0 + timedelta(days=60)).isoformat()]
    assert abs(day60[2] - 1.0) < 1e-9  # exactly one half-life later


def test_decay_index_sums_metrics_and_keeps_latest_impulse_per_metric() -> None:
    d0 = date(2024, 1, 2)
    d30 = d0 + timedelta(days=30)
    calendar = [d0 + timedelta(days=k) for k in range(40)]
    impulses = [
        ("growth_now", "m1", d0, 2.0),
        ("growth_now", "m2", d0, 1.0),
        ("growth_now", "m1", d30, 4.0),
    ]
    rows = bmsf.build_surprise_index_daily(impulses, calendar, end_date=d0 + timedelta(days=35))
    by_date = {row[0]: row for row in rows}

    assert abs(by_date[d0.isoformat()][2] - 3.0) < 1e-12
    # At d30 m1's latest impulse replaces the old one; m2 has decayed half a half-life.
    expected_d30 = 4.0 + 1.0 * (0.5 ** (30.0 / 60.0))
    assert abs(by_date[d30.isoformat()][2] - expected_d30) < 1e-9
    assert by_date[d30.isoformat()][3] == 2


def test_revision_factor_expanding_mean_abs_z() -> None:
    periods = _monthly_periods(5)
    first_prints = [1.0, 2.0, 3.0, 4.0, 5.0]
    latest_values = [1.0, 2.5, 3.0, 5.0, 5.0]
    rows = bmsf.build_revision_factor_rows(
        metric_key="synthetic_rev",
        periods=periods,
        first_prints=first_prints,
        latest_values=latest_values,
    )

    assert rows[0].mean_abs_revision_z is None  # no std proxy from a single first print
    expected_z: list[float] = []
    for i in range(1, 5):
        scale = stdev(first_prints[: i + 1])
        expected_z.append(abs(first_prints[i] - latest_values[i]) / scale)
        row = rows[i]
        assert row.mean_abs_revision_z is not None
        assert abs(row.mean_abs_revision_z - sum(expected_z) / len(expected_z)) < 1e-12
