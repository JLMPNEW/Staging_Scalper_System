"""Pure time-series rotation features on the sealed Stage 2 price/return panel.

No fetch, no I/O - deterministic functions over pandas Series so the signal is fully PIT
(every window is contained in the trailing panel whose right edge is <= the run as_of).
"""
from __future__ import annotations

import math
from typing import cast

import numpy as np
import pandas as pd


TRADING_DAYS = 252


def total_return_momentum(prices: pd.Series, window: int) -> float | None:
    """Total return over the last `window` trading days, using only realized history."""
    s = prices.dropna()
    if window <= 0 or len(s) < window + 1:
        return None
    p_now = float(s.iloc[-1])
    p_then = float(s.iloc[-1 - window])
    if not (math.isfinite(p_now) and math.isfinite(p_then)) or p_then <= 0:
        return None
    return p_now / p_then - 1.0


def annualized_vol(returns: pd.Series, window: int) -> float | None:
    r = returns.dropna()
    if len(r) < max(2, window // 2):
        return None
    r = r.iloc[-window:] if len(r) >= window else r
    sd = float(r.std(ddof=1))
    if not math.isfinite(sd) or sd <= 0:
        return None
    return sd * math.sqrt(TRADING_DAYS)


def composite_score(
    prices: pd.Series,
    returns: pd.Series,
    *,
    windows: list[int],
    weights: list[float],
) -> float | None:
    """Volatility-normalized multi-horizon momentum (a Sharpe-like blend).

    For each horizon w: annualize the window momentum and divide by the (longest-window) vol,
    then blend by the configured weights. Returns None if no horizon is computable.
    """
    if not windows:
        return None
    vol = annualized_vol(returns, max(windows))
    if vol is None:
        return None
    num = 0.0
    wsum = 0.0
    for w, wt in zip(windows, weights):
        m = total_return_momentum(prices, w)
        if m is None:
            continue
        ann_m = m * (TRADING_DAYS / w)
        num += float(wt) * (ann_m / vol)
        wsum += float(wt)
    if wsum <= 0:
        return None
    return num / wsum


def trend_state(prices: pd.Series, *, ma_days: int, slope_lookback: int) -> tuple[str, dict]:
    """Absolute-trend classification: up / neutral / down from price-vs-MA and MA slope sign."""
    s = prices.dropna()
    if len(s) < ma_days + slope_lookback:
        return "neutral", {"reason": "insufficient_history", "above_ma": False, "ma_slope": 0.0}
    ma = cast(pd.Series, s.rolling(ma_days).mean())
    ma_last = float(ma.iloc[-1])
    ma_prev = float(ma.iloc[-1 - slope_lookback])
    price = float(s.iloc[-1])
    if not all(math.isfinite(x) for x in (ma_last, ma_prev, price)):
        return "neutral", {"reason": "non_finite", "above_ma": False, "ma_slope": 0.0}
    above = price >= ma_last
    slope = ma_last - ma_prev
    slope_up = slope >= 0
    if above and slope_up:
        state = "up"
    elif (not above) and (not slope_up):
        state = "down"
    else:
        state = "neutral"
    return state, {"price": price, "ma": ma_last, "ma_slope": slope, "above_ma": above}


def percentile_ranks(scores: dict[str, float | None]) -> dict[str, float]:
    """Cross-sectional percentile in [0, 100] with average handling of ties."""
    items = [(t, float(v)) for t, v in scores.items() if v is not None and math.isfinite(float(v))]
    n = len(items)
    if n == 0:
        return {}
    values = np.array([v for _, v in items], dtype=float)
    out: dict[str, float] = {}
    for t, v in items:
        less = float(np.sum(values < v))
        equal = float(np.sum(values == v))
        out[t] = round((less + 0.5 * equal) / n * 100.0, 4)
    return out
