"""Foreign-market ETF rotation evaluator (shadow; applied budget held at 0 until Stage 6).

Emits the optimizer foreign contract {Ticker, MarketName, Score, ScorePct, State} with
State vocabulary {Eligible, Avoid}. Ranked within the foreign universe itself.
"""
from __future__ import annotations

from typing import cast

import pandas as pd

from portfolio_layer.rotation.rotation_timeseries import (
    composite_score,
    percentile_ranks,
    trend_state,
)


def _series_col(df: pd.DataFrame, col: str) -> pd.Series:
    values = df[col]
    if isinstance(values, pd.DataFrame):
        values = values.iloc[:, 0]
    return cast(pd.Series, pd.to_numeric(values, errors="coerce"))


def _returns_for(prices: pd.DataFrame, returns: pd.DataFrame, etf: str) -> pd.Series:
    if etf in returns.columns:
        return _series_col(returns, etf)
    return _series_col(prices, etf).pct_change(fill_method=None)


def build_foreign_rotation(
    prices: pd.DataFrame,
    returns: pd.DataFrame,
    *,
    market_map: dict[str, str],
    windows: list[int],
    weights: list[float],
    ma_days: int,
    slope_lookback: int,
    eligible_score_pct: float,
) -> list[dict]:
    scores = {
        etf: composite_score(_series_col(prices, etf), _returns_for(prices, returns, etf), windows=windows, weights=weights)
        for etf in market_map
        if etf in prices.columns
    }
    pcts = percentile_ranks(scores)

    rows: list[dict] = []
    for etf, market in sorted(market_map.items()):
        score = scores.get(etf)
        pct = pcts.get(etf)
        present = etf in prices.columns and score is not None and pct is not None
        score_value = 0.0 if score is None else float(score)
        pct_value = 0.0 if pct is None else float(pct)
        ts, info = trend_state(_series_col(prices, etf), ma_days=ma_days, slope_lookback=slope_lookback) if etf in prices.columns \
            else ("neutral", {"above_ma": False, "ma_slope": 0.0})
        eligible = bool(present and pct_value >= eligible_score_pct and ts == "up")
        state = "Eligible" if eligible else "Avoid"
        rows.append({
            "ticker": etf,
            "market_name": market,
            "score": round(score_value, 8),
            "score_pct": round(pct_value, 4),
            "state": state,
            "trend_state": ts,
            "eligible": int(eligible),
            "present_in_panel": bool(present),
        })
    return rows
