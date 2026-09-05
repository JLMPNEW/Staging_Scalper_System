from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from visualitation.dashboard_metrics import (
    calculate_index_risk,
    exponentially_weighted_correlation,
    latest_correlation_matrix,
)
from visualitation.dashboard_ui import (
    _benchmark_performance_window,
    _missing_date_ranges,
    _next_business_window,
    _portfolio_window_coverage,
    _portfolio_window_complete,
    _position_next_action,
    _position_state,
    _research_next_action,
)


def _price_series(returns: np.ndarray, start: float = 100.0) -> np.ndarray:
    return start * np.exp(np.r_[0.0, np.cumsum(returns)])


def test_exponentially_weighted_correlation_is_endpoint_sensitive() -> None:
    left = pd.Series(np.linspace(-0.03, 0.03, 160))
    identical = exponentially_weighted_correlation(left, left, minimum_observations=90)
    inverse = exponentially_weighted_correlation(left, -left, minimum_observations=90)
    assert identical is not None and identical > 0.999999
    assert inverse is not None and inverse < -0.999999


def test_exponentially_weighted_correlation_matches_manual_formula() -> None:
    left = pd.Series([0.012, -0.021, 0.007, 0.026, -0.009, 0.018])
    right = pd.Series([-0.004, -0.013, 0.015, 0.009, -0.018, 0.022])
    half_life = 2.5

    actual = exponentially_weighted_correlation(
        left,
        right,
        half_life=half_life,
        minimum_observations=1,
    )

    ages = np.arange(len(left) - 1, -1, -1, dtype=float)
    weights = np.power(0.5, ages / half_life)
    weights /= weights.sum()
    left_values = left.to_numpy(dtype=float)
    right_values = right.to_numpy(dtype=float)
    left_centered = left_values - np.average(left_values, weights=weights)
    right_centered = right_values - np.average(right_values, weights=weights)
    expected = np.sum(weights * left_centered * right_centered) / np.sqrt(
        np.sum(weights * left_centered**2)
        * np.sum(weights * right_centered**2)
    )

    assert actual == pytest.approx(expected)


def test_calculate_index_risk_reports_coverage_and_dominant_sector() -> None:
    observations = 320
    dates = pd.bdate_range("2025-01-02", periods=observations + 1)
    xbi_returns = 0.001 + 0.009 * np.sin(np.arange(observations) / 7.0)
    xlk_returns = 0.0005 + 0.008 * np.cos(np.arange(observations) / 11.0)
    spy_returns = 0.55 * xbi_returns + 0.45 * xlk_returns
    a_returns = 0.92 * xbi_returns + 0.08 * xlk_returns
    b_returns = 0.88 * xlk_returns + 0.12 * xbi_returns
    prices = pd.DataFrame(
        {
            "A": _price_series(a_returns),
            "B": _price_series(b_returns),
            "XBI": _price_series(xbi_returns),
            "XLK": _price_series(xlk_returns),
            "SPY": _price_series(spy_returns),
        },
        index=dates,
    )
    holdings = pd.DataFrame(
        {
            "asset_category": ["Stocks", "Stocks", "Stocks"],
            "symbol": ["A", "B", "MISSING"],
            "market_value": [60.0, 40.0, 25.0],
        }
    )

    benchmark, holding_map, coverage = calculate_index_risk(
        holdings,
        prices,
        benchmark_tickers=("XBI", "XLK", "SPY"),
        benchmark_labels={"XBI": "Biotech", "XLK": "Technology", "SPY": "S&P 500"},
        sector_tickers=("XBI", "XLK"),
    )

    assert coverage.covered_names == 2
    assert coverage.total_names == 3
    assert coverage.covered_gross_value == 100.0
    assert coverage.total_gross_value == 125.0
    assert coverage.market_value_ratio == 0.8
    assert coverage.complete_observations == observations
    assert set(benchmark["benchmark"]) == {"XBI", "XLK", "SPY"}
    assert benchmark["tactical_observations"].eq(observations).all()
    assert benchmark["structural_observations"].eq(250).all()
    assert benchmark["observations"].eq(benchmark["structural_observations"]).all()
    assert holding_map["tactical_observations"].eq(observations).all()
    assert holding_map["structural_observations"].eq(250).all()
    assert holding_map["observations"].eq(holding_map["structural_observations"]).all()
    dominant = holding_map.set_index("ticker")["dominant_benchmark"].to_dict()
    assert dominant == {"A": "XBI", "B": "XLK"}


def test_latest_correlation_matrix_expands_canonical_pairs() -> None:
    rolling = pd.DataFrame(
        {"A__B": [0.25], "A__C": [-0.10], "B__C": [0.70]},
        index=pd.DatetimeIndex(["2026-09-04"], name="date"),
    )

    def pair(left: str, right: str) -> str:
        return "__".join(sorted((left, right)))

    matrix = latest_correlation_matrix(rolling, ("A", "B", "C"), pair)
    assert np.allclose(np.diag(matrix.to_numpy()), 1.0)
    assert matrix.loc["A", "B"] == matrix.loc["B", "A"] == 0.25
    assert matrix.loc["A", "C"] == matrix.loc["C", "A"] == -0.10
    assert matrix.loc["B", "C"] == matrix.loc["C", "B"] == 0.70


def test_performance_period_uses_prior_close_and_requires_complete_ib_chain() -> None:
    dates = pd.to_datetime(["2025-12-31", "2026-01-02", "2026-01-05", "2026-01-06"])
    prices = pd.DataFrame(
        {"SPY": [100.0, 101.0, 102.0, 103.0], "QQQ": [200.0, 198.0, 202.0, 204.0]},
        index=dates,
    )
    benchmark = _benchmark_performance_window(prices, "2026-01-06", "YTD")
    assert benchmark.iloc[0]["sp500_return"] == 0.0
    assert benchmark.iloc[-1]["sp500_return"] == pytest.approx(0.03)
    complete = pd.DataFrame({
        "date": pd.to_datetime(["2026-01-01", "2026-01-02", "2026-01-05", "2026-01-06"])
    })
    incomplete = complete.iloc[1:].copy()
    assert _portfolio_window_complete(complete, benchmark, "2026-01-06", "YTD")
    assert not _portfolio_window_complete(incomplete, benchmark, "2026-01-06", "YTD")
    observed, expected, missing = _portfolio_window_coverage(
        incomplete, "2026-01-06", "YTD"
    )
    assert (observed, expected) == (3, 4)
    assert missing == [pd.Timestamp("2026-01-01")]
    assert _missing_date_ranges(missing) == "Jan 1, 2026"


def test_next_seven_business_dates_excludes_us_federal_holiday() -> None:
    dates = _next_business_window("2026-09-04", 7)
    assert dates[0] == pd.Timestamp("2026-09-08")
    assert dates[-1] == pd.Timestamp("2026-09-16")


def test_prototype_position_state_and_action_policy() -> None:
    base = {
        "internal_state": "stable",
        "action_state": "hold",
        "target_weight": 0.0,
        "market_value": 100.0,
        "unrealized_pl": 5.0,
    }
    assert _position_state("green") == "Stable"
    assert _position_state("deteriorating") == "Deteriorating"
    assert _position_next_action(pd.Series(base)) == "Review fit"
    assert _position_next_action(pd.Series({**base, "unrealized_pl": -10.0})) == "Watch"
    assert _position_next_action(pd.Series({**base, "unrealized_pl": -25.0})) == "Re-underwrite"
    assert _position_next_action(pd.Series({**base, "target_weight": 0.02})) == "Hold"
    assert _position_next_action(pd.Series({**base, "internal_state": "deteriorating"})) == "Suspend adds"


def test_research_action_policy_separates_entry_monitor_and_risk_holds() -> None:
    assert _research_next_action(pd.Series({"weight": 0.02, "internal_state": "stable", "action_state": "hold"})) == "Review entry"
    assert _research_next_action(pd.Series({"weight": 0.0, "internal_state": "stable", "action_state": "hold"})) == "Monitor"
    assert _research_next_action(pd.Series({"weight": 0.02, "internal_state": "watch", "action_state": "hold"})) == "Suspend adds"
