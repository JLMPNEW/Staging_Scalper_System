from __future__ import annotations

# pyright: reportMissingImports=false

import logging
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
MACRO_LAYER_ROOT = PROJECT_ROOT / "portfolio_layer" / "MacroLayer"
if str(MACRO_LAYER_ROOT) not in sys.path:
    sys.path.insert(0, str(MACRO_LAYER_ROOT))

import run_macro_industry_ablation as ablation  # noqa: E402


# --------------------------------------------------------------------------------------
# Weight construction: softplus + 0.20 cap via macro_allocation.bounded_normalize.
# --------------------------------------------------------------------------------------
def test_tilt_weights_match_softplus_proportions_when_cap_slack() -> None:
    scores = pd.Series(
        [0.5, 0.0, -0.5, -1.0, 0.25, -0.25, 0.1, -0.1],
        index=[f"IND_{i}" for i in range(8)],
    )
    weights = ablation.tilt_weights(scores)
    softplus = np.logaddexp(0.0, scores.to_numpy())
    expected = softplus / softplus.sum()
    assert float(weights.sum()) == pytest.approx(1.0)
    assert np.allclose(weights.to_numpy(), expected, atol=1e-9)
    assert float(weights.max()) <= ablation.TILT_INDUSTRY_CAP + 1e-9


def test_tilt_weights_cap_binds_and_residual_redistributes() -> None:
    scores = pd.Series([3.0, 0.0, 0.0, 0.0, 0.0, 0.0], index=list("ABCDEF"))
    weights = ablation.tilt_weights(scores)
    # softplus(3) / sum would be ~0.47 > cap, so A pins to the 0.20 cap and the
    # residual 0.80 spreads equally over the five identical remaining scores.
    assert float(weights.sum()) == pytest.approx(1.0)
    assert float(weights["A"]) == pytest.approx(0.20)
    for name in "BCDEF":
        assert float(weights[name]) == pytest.approx(0.16)


def test_tilt_weights_infeasible_cap_leaves_cash_residual() -> None:
    # 3 industries x 0.20 cap can only hold 0.60: allow_partial keeps caps honest
    # and the ablation carries the remaining 0.40 as cash at zero return.
    scores = pd.Series([1.0, 0.5, -0.5], index=list("ABC"))
    weights = ablation.tilt_weights(scores)
    assert float(weights.sum()) == pytest.approx(0.60)
    assert np.allclose(weights.to_numpy(), ablation.TILT_INDUSTRY_CAP)


def test_neutral_weights_equal_and_fully_invested() -> None:
    weights = ablation.neutral_weights(pd.Index(list("ABCDE")))
    assert float(weights.sum()) == pytest.approx(1.0)
    assert np.allclose(weights.to_numpy(), 0.2)
    assert ablation.neutral_weights(pd.Index([])).empty


# --------------------------------------------------------------------------------------
# Turnover / cost math.
# --------------------------------------------------------------------------------------
def test_turnover_and_cost_math() -> None:
    weights = pd.DataFrame(
        [
            {"A": 0.5, "B": 0.5},
            {"A": 0.6, "B": 0.4},
            {"B": 1.0},  # A drops out of the book: NaN must count as weight 0.
        ],
        index=pd.to_datetime(["2024-01-05", "2024-01-12", "2024-01-19"]),
    )
    turnover = ablation.turnover_series(weights)
    # Week 1 enters from all cash: 0.5 * (0.5 + 0.5); week 2: 0.5 * (0.1 + 0.1);
    # week 3: 0.5 * (0.6 + 0.6).
    assert np.allclose(turnover.to_numpy(), [0.5, 0.1, 0.6])
    costs = ablation.cost_series(turnover, round_trip_cost_bps=10.0)
    assert np.allclose(costs.to_numpy(), [0.5e-3, 0.1e-3, 0.6e-3])


def test_cost_series_rejects_negative_bps() -> None:
    with pytest.raises(ValueError):
        ablation.cost_series(pd.Series([0.1]), round_trip_cost_bps=-1.0)


# --------------------------------------------------------------------------------------
# Moving-block bootstrap CI (deterministic seed).
# --------------------------------------------------------------------------------------
def test_block_bootstrap_is_deterministic_and_centered() -> None:
    rng = np.random.default_rng(7)
    diffs = rng.normal(loc=0.001, scale=0.01, size=120)
    first = ablation.moving_block_bootstrap_ci(diffs)
    second = ablation.moving_block_bootstrap_ci(diffs)
    assert first == second
    assert first["mean_weekly_diff"] == pytest.approx(float(diffs.mean()))
    assert first["ci_low"] <= first["mean_weekly_diff"] <= first["ci_high"]
    assert first["block_weeks"] == 8
    assert first["resamples"] == 1000
    assert first["seed"] == 20260804
    assert first["n_weeks"] == 120


def test_block_bootstrap_constant_series_has_degenerate_ci() -> None:
    out = ablation.moving_block_bootstrap_ci(np.full(30, 0.002))
    assert out["ci_low"] == pytest.approx(0.002)
    assert out["ci_high"] == pytest.approx(0.002)
    assert out["prob_diff_positive"] == pytest.approx(1.0)


def test_block_bootstrap_clamps_block_to_short_series() -> None:
    out = ablation.moving_block_bootstrap_ci(np.array([0.01, -0.02, 0.03]), block_weeks=8)
    assert out["block_weeks"] == 3
    assert out["n_weeks"] == 3
    assert out["ci_low"] <= out["ci_high"]


def test_block_bootstrap_rejects_empty_input() -> None:
    with pytest.raises(ValueError):
        ablation.moving_block_bootstrap_ci(np.array([np.nan]))


# --------------------------------------------------------------------------------------
# Member-price data-artifact guards (price floor + return winsorization).
# --------------------------------------------------------------------------------------
def _one_week_guard_fixture(
    prices: dict[str, list[float]],
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DatetimeIndex]:
    """One evaluable signal week (entry 03-10, exit 03-17), all tickers in one industry."""
    signal_dates = pd.DatetimeIndex(["2025-03-07", "2025-03-14"])
    price_frame = pd.DataFrame(prices, index=pd.to_datetime(["2025-03-10", "2025-03-17"]))
    membership = pd.DataFrame(
        {
            "Date": [pd.Timestamp("2025-03-07")] * len(prices),
            "Ticker": list(prices.keys()),
            "industry_key": ["Tech||Software||software"] * len(prices),
        }
    )
    return membership, price_frame, signal_dates


def test_sub_floor_member_excluded_and_basket_renormalized(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # LLNW_EGIO-style sentinel print (0.000001) would be a fake +29,900% weekly return;
    # the floor guard must drop it and the basket must renormalize over the survivors.
    membership, prices, signal_dates = _one_week_guard_fixture(
        {
            "AAA": [10.0, 11.0],  # +10%
            "BBB": [20.0, 19.0],  # -5%
            "LLNW_EGIO": [0.000001, 0.0003],
        }
    )
    with caplog.at_level(logging.WARNING, logger=ablation.logger.name):
        out = ablation.compute_industry_week_returns(
            membership=membership,
            prices=prices,
            signal_dates=signal_dates,
            min_member_price=0.01,
        )
    assert len(out) == 1
    row = out.iloc[0]
    assert int(row["priced_members"]) == 2
    assert float(row["industry_return"]) == pytest.approx((0.10 - 0.05) / 2.0)
    floor_warnings = [r for r in caplog.records if "price-floor" in r.getMessage()]
    assert len(floor_warnings) == 1
    message = floor_warnings[0].getMessage()
    assert "LLNW_EGIO" in message
    assert "2025-03-07" in message


def test_extreme_member_return_winsorized_at_plus_minus_200pct(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # DDD clears the price floor but returns +2400%; it must be clamped to +200% and
    # averaged with the untouched member, with a warning naming ticker and week.
    membership, prices, signal_dates = _one_week_guard_fixture(
        {
            "AAA": [10.0, 11.0],  # +10%, untouched
            "DDD": [0.02, 0.50],  # +2400% -> winsorized to +200%
        }
    )
    with caplog.at_level(logging.WARNING, logger=ablation.logger.name):
        out = ablation.compute_industry_week_returns(
            membership=membership,
            prices=prices,
            signal_dates=signal_dates,
        )
    assert len(out) == 1
    row = out.iloc[0]
    assert int(row["priced_members"]) == 2
    assert float(row["industry_return"]) == pytest.approx(
        (0.10 + ablation.MEMBER_RETURN_WINSOR_LIMIT) / 2.0
    )
    winsor_warnings = [r for r in caplog.records if "winsor guard" in r.getMessage()]
    assert len(winsor_warnings) == 1
    message = winsor_warnings[0].getMessage()
    assert "DDD" in message
    assert "2025-03-07" in message
