"""Shared Stage 2 panel logic: universe assembly, calendar alignment, returns."""
from __future__ import annotations

from typing import Any

import pandas as pd

from portfolio_layer.core.contracts import read_csv


def build_universe(scores_path: Any, risk_cfg: dict[str, Any]) -> list[dict[str, str]]:
    """Eligible scored names (investable_eligible=1) + benchmarks/hedges/sector-ETF targets.

    One row per ticker; scored names carry source_pipeline, market instruments carry role only.
    """
    universe: dict[str, dict[str, str]] = {}
    for r in read_csv(scores_path):
        if str(r.get("investable_eligible", "")).strip() == "1":
            t = str(r.get("ticker", "")).strip().upper()
            if t:
                universe[t] = {"ticker": t, "role": "scored", "source_pipeline": str(r.get("source_pipeline", ""))}
    instruments: list[str] = []
    instruments += [str(x).upper() for x in risk_cfg.get("benchmark_tickers", [])]
    instruments += [str(x).upper() for x in risk_cfg.get("hedge_rotation_etfs", [])]
    instruments += [str(x).upper() for x in (risk_cfg.get("sector_etf_map", {}) or {}).values()]
    for t in instruments:
        if t and t not in universe:
            universe[t] = {"ticker": t, "role": "market_instrument", "source_pipeline": ""}
    return list(universe.values())


def master_calendar(spy_series: dict[str, float], run_as_of: str, lookback: int) -> list[str]:
    """Last `lookback` SPY trading days at or before run_as_of (the US master calendar)."""
    days = sorted(d for d in spy_series if d <= run_as_of)
    return days[-lookback:] if len(days) > lookback else days


def assemble_prices(series_by_ticker: dict[str, dict[str, float]], calendar: list[str]) -> pd.DataFrame:
    """Wide adjusted-close frame: rows = master calendar, cols = tickers, NaN where a ticker has no bar."""
    index = pd.DatetimeIndex([pd.Timestamp(d) for d in calendar])
    data = {
        ticker: pd.Series({pd.Timestamp(d): v for d, v in series.items()})
        for ticker, series in series_by_ticker.items()
    }
    frame = pd.DataFrame(data).reindex(index)
    frame.index.name = "date"
    return frame


def to_returns(prices: pd.DataFrame, frequency: str) -> pd.DataFrame:
    """Simple returns on the aligned panel. Missing bars stay NaN — never fabricated as zero."""
    px = prices
    if frequency == "weekly":
        px = prices.resample("W-FRI").last()
    returns = px.pct_change(fill_method=None)
    return returns.iloc[1:]
