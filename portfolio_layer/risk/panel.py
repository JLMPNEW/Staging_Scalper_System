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
    # Issuer-level dedup via ticker_aliases: if two contract tickers map to the same issuer_id
    # (e.g. a predecessor and its post-migration active symbol both slip through Stage 1), keeping
    # both would seal two near-identical columns and double the issuer's weight under per-name caps.
    # Keep the alias entry's active_ticker; drop the other.
    by_issuer: dict[str, list[str]] = {}
    aliases = risk_cfg.get("ticker_aliases", {}) or {}
    for t in universe:
        issuer = str((aliases.get(t) or {}).get("issuer_id", "")).strip()
        if issuer:
            by_issuer.setdefault(issuer, []).append(t)
    for issuer, tickers in by_issuer.items():
        if len(tickers) < 2:
            continue
        keep = next(
            (t for t in sorted(tickers)
             if str((aliases.get(t) or {}).get("active_ticker", "")).strip().upper() == t),
            sorted(tickers)[0],
        )
        for t in tickers:
            if t != keep:
                universe.pop(t, None)
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


def coverage_stats(prices: pd.DataFrame, ticker: str, panel_end: str | None = None) -> dict[str, str | int | float]:
    """Coverage stats from the aligned price panel, measured through the panel right edge."""
    panel_end = panel_end or (str(prices.index[-1]) if not prices.empty else "")
    if ticker not in prices.columns:
        return {
            "observation_count": 0,
            "missing_day_count": prices.shape[0],
            "right_edge_missing_day_count": prices.shape[0],
            "missing_day_fraction": 1.0,
            "start_date": "",
            "end_date": "",
        }
    col = prices[ticker]
    obs = int(col.notna().sum())
    if obs == 0:
        return {
            "observation_count": 0,
            "missing_day_count": prices.shape[0],
            "right_edge_missing_day_count": prices.shape[0],
            "missing_day_fraction": 1.0,
            "start_date": "",
            "end_date": "",
        }
    present = col.dropna()
    first, last = str(present.index[0]), str(present.index[-1])
    span = prices.loc[first:panel_end].shape[0] if panel_end else prices.loc[first:].shape[0]
    missing = max(0, span - obs)
    right_edge_missing = max(0, prices.loc[last:panel_end].shape[0] - 1) if panel_end else 0
    return {
        "observation_count": obs,
        "missing_day_count": missing,
        "right_edge_missing_day_count": right_edge_missing,
        "missing_day_fraction": round(missing / span, 4) if span else 0.0,
        "start_date": first,
        "end_date": last,
    }


def classify_coverage(
    stats: dict[str, str | int | float],
    *,
    min_direct: int,
    hard_floor: int,
    max_gap_frac: float,
    max_stale_days: int,
    fetch_status: str = "missing",
) -> tuple[str, int, str]:
    """Return (risk_status, risk_eligible, risk_reason) for one ticker."""
    obs = int(stats["observation_count"])
    gap = float(stats["missing_day_fraction"])
    right_edge_missing = int(stats["right_edge_missing_day_count"])
    last = str(stats["end_date"])
    if obs == 0:
        return "excluded", 0, f"no_price_data:{fetch_status or 'missing'}"
    if right_edge_missing > max_stale_days:
        return "excluded", 0, f"stale_right_edge:{last}"
    if obs < hard_floor:
        return "excluded", 0, "below_hard_floor"
    if obs < min_direct or gap > max_gap_frac:
        return "shrunk", 1, "partial_history" if obs < min_direct else "high_internal_gaps"
    return "direct", 1, "direct"


def to_returns(prices: pd.DataFrame, frequency: str) -> pd.DataFrame:
    """Simple returns on the aligned panel. Missing bars stay NaN — never fabricated as zero."""
    px = prices
    if frequency == "weekly":
        # Take the last observation per W-FRI bin but label each bin with its last ACTUAL trading
        # day, never the bin's nominal Friday — a mid-week as-of must not get a future-dated row.
        grouper = prices.groupby(pd.Grouper(freq="W-FRI"))
        px = grouper.last()
        actual_last = grouper.apply(lambda g: g.index.max() if len(g) else pd.NaT)
        keep = actual_last.notna()
        px = px.loc[keep]
        px.index = pd.DatetimeIndex(actual_last[keep])
        px.index.name = prices.index.name
    returns = px.pct_change(fill_method=None)
    return returns.iloc[1:]
