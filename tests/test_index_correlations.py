from __future__ import annotations

import numpy as np
import pandas as pd

from index_correlations.pipeline import align_prices, compute_daily_log_returns, compute_rolling_correlations


def test_align_prices_uses_common_date_intersection_without_forward_fill() -> None:
    raw = pd.DataFrame(
        {
            "XBI": [10.0, 11.0, 12.0],
            "IHI": [20.0, np.nan, 22.0],
        },
        index=pd.to_datetime(["2024-01-02", "2024-01-03", "2024-01-04"]),
    )
    aligned = align_prices(raw, ("XBI", "IHI"))
    assert list(pd.DatetimeIndex(aligned.index).strftime("%Y-%m-%d")) == [
        "2024-01-02",
        "2024-01-04",
    ]
    assert len(aligned) == 2


def test_daily_log_returns_are_calculated_from_aligned_prices() -> None:
    prices = pd.DataFrame(
        {"XBI": [100.0, 110.0, 99.0], "IHI": [50.0, 55.0, 49.5]},
        index=pd.date_range("2024-01-01", periods=3, freq="D"),
    )
    returns = compute_daily_log_returns(prices)
    np.testing.assert_allclose(returns["XBI"].to_numpy(), [np.log(1.1), np.log(0.9)])


def test_rolling_pearson_and_kendall_require_a_full_window() -> None:
    dates = pd.date_range("2024-01-01", periods=5, freq="D")
    values = pd.DataFrame(
        {
            "CUSTOM_A": np.arange(1.0, 6.0),
            "CUSTOM_B": np.arange(2.0, 7.0),
            "CUSTOM_C": np.arange(3.0, 8.0),
        },
        index=dates,
    )
    panels = compute_rolling_correlations(values, windows=(3,), methods=("pearson", "kendall_tau"))
    assert set(panels[("pearson", 3)].columns) == {
        "CUSTOM_A__CUSTOM_B",
        "CUSTOM_A__CUSTOM_C",
        "CUSTOM_B__CUSTOM_C",
    }
    assert panels[("pearson", 3)]["CUSTOM_A__CUSTOM_B"].iloc[:2].isna().all()
    assert np.isclose(panels[("pearson", 3)]["CUSTOM_A__CUSTOM_B"].iloc[2], 1.0)
    assert np.isclose(panels[("kendall_tau", 3)]["CUSTOM_A__CUSTOM_B"].iloc[2], 1.0)
