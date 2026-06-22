"""Cross-sectional sector-sleeve rotation: rank against a broad context, tilt only the 5 sleeves.

Emits one row per `source_pipeline` (the optimizer join key `SectorName`) with the optimizer-contract
`State` vocabulary {Positive, Neutral, Negative} and a bounded `rotation_multiplier` for the ablation tilt.
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


def build_sector_rotation(
    prices: pd.DataFrame,
    returns: pd.DataFrame,
    *,
    sector_etf_map: dict[str, str],
    rank_universe: list[str],
    windows: list[int],
    weights: list[float],
    ma_days: int,
    slope_lookback: int,
    positive_score_pct: float,
    negative_score_pct: float,
    mult_min: float,
    mult_max: float,
) -> list[dict]:
    # Score the broad context universe (so a strong sleeve in a weak tape gets a moderate percentile).
    universe = sorted(set(rank_universe) | set(sector_etf_map.values()))
    scores = {
        etf: composite_score(_series_col(prices, etf), _returns_for(prices, returns, etf), windows=windows, weights=weights)
        for etf in universe
        if etf in prices.columns
    }
    pcts = percentile_ranks(scores)

    rows: list[dict] = []
    for pipeline, etf in sorted(sector_etf_map.items()):
        score = scores.get(etf)
        pct = pcts.get(etf)
        present = etf in prices.columns and score is not None and pct is not None
        score_value = 0.0 if score is None else float(score)
        pct_value = 0.0 if pct is None else float(pct)
        ts, info = trend_state(_series_col(prices, etf), ma_days=ma_days, slope_lookback=slope_lookback) if etf in prices.columns \
            else ("neutral", {"above_ma": False, "ma_slope": 0.0})

        if not present:
            state, gate, mult = "Neutral", "fail", 1.0
        else:
            if ts == "down":
                state, gate = "Negative", "fail"
            elif pct_value >= positive_score_pct and ts == "up":
                state, gate = "Positive", "pass"
            elif pct_value <= negative_score_pct:
                state, gate = "Negative", "pass"
            else:
                state, gate = "Neutral", "pass"
            mult = mult_min + (mult_max - mult_min) * (pct_value / 100.0)
            if ts == "down":  # never tilt INTO an absolute downtrend
                mult = min(mult, 1.0)

        rows.append({
            "source_pipeline": pipeline,
            "etf": etf,
            "score": round(score_value, 8),
            "score_pct": round(pct_value, 4),
            "state": state,
            "trend_state": ts,
            "trend_gate": gate,
            "above_ma": bool(info.get("above_ma", False)),
            "ma_slope": round(float(info.get("ma_slope", 0.0)), 8),
            "rotation_multiplier": round(float(mult), 6),
            "present_in_panel": bool(present),
        })
    return rows
