#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import logging
import math
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from statistics import stdev
from typing import Any


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from med_devices.core.config import cfg_get, load_yaml, resolve_path  # noqa: E402
from med_devices.core.db import connect, finish_run, init_db, start_run, utc_now  # noqa: E402
from med_devices.core.logging_utils import configure_utc_logging  # noqa: E402
from med_devices.core.market_policy import scoring_market_sources  # noqa: E402
from med_devices.core.text_norm import normalize_ticker  # noqa: E402


LOGGER = logging.getLogger("build_med_device_technical_features")
DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
TECH_WINSOR_LOW_PCT = 0.05
TECH_WINSOR_HIGH_PCT = 0.95
TECHNICAL_MODE_LEGACY = "legacy"
TECHNICAL_MODE_TREND_FOLLOWING = "trend_following"
TECHNICAL_MODE_PULLBACK_MEAN_REVERSION = "pullback_mean_reversion"
TECHNICAL_MODE_NEUTRAL_OVERLAY = "neutral_overlay"
TECHNICAL_SIGNAL_MODES = {
    TECHNICAL_MODE_LEGACY,
    TECHNICAL_MODE_TREND_FOLLOWING,
    TECHNICAL_MODE_PULLBACK_MEAN_REVERSION,
    TECHNICAL_MODE_NEUTRAL_OVERLAY,
}
FIELDNAMES = [
    "asof_date",
    "company_id",
    "ticker",
    "company_name",
    "subsector",
    "calibration_cohort",
    "market_source_id",
    "latest_price_date",
    "latest_close",
    "avg_dollar_volume_60d",
    "volume_trend_ratio",
    "return_21d",
    "return_63d",
    "return_126d",
    "return_252d",
    "momentum_12_1",
    "relative_strength_63d",
    "relative_strength_126d",
    "sma_50",
    "sma_200",
    "price_vs_sma_50",
    "price_vs_sma_200",
    "sma_50_vs_200",
    "sma_200_slope_63d",
    "rsi_14",
    "atr_14_pct",
    "realized_vol_60d",
    "max_drawdown_252d",
    "pct_from_52w_high",
    "trend_quality_score",
    "relative_strength_score",
    "liquidity_score",
    "volume_breakout_score",
    "volatility_risk_score",
    "technical_score",
    "technical_entry_score",
    "technical_setup_score",
    "technical_core_score",
    "technical_alpha_score",
    "technical_pullback_score",
    "technical_overextension_score",
    "technical_breakdown_flag",
    "technical_liquidity_gate_flag",
    "technical_signal_mode",
    "technical_signal_direction",
    "technical_signal_reliability",
    "technical_policy_reason",
    "entry_signal",
    "data_quality_status",
    "missing_fields",
]


@dataclass(frozen=True)
class Company:
    company_id: int
    ticker: str
    company_name: str
    subsector: str
    calibration_cohort: str = ""


@dataclass(frozen=True)
class Bar:
    ticker: str
    source_id: str
    bar_date: str
    close: float
    raw_close: float | None
    high: float | None
    low: float | None
    volume: float | None


@dataclass
class TechnicalRow:
    asof_date: str
    company_id: int
    ticker: str
    company_name: str
    subsector: str
    calibration_cohort: str = ""
    market_source_id: str = ""
    latest_price_date: str = ""
    latest_close: float | None = None
    avg_dollar_volume_60d: float | None = None
    volume_trend_ratio: float | None = None
    return_21d: float | None = None
    return_63d: float | None = None
    return_126d: float | None = None
    return_252d: float | None = None
    momentum_12_1: float | None = None
    relative_strength_63d: float | None = None
    relative_strength_126d: float | None = None
    sma_50: float | None = None
    sma_200: float | None = None
    price_vs_sma_50: float | None = None
    price_vs_sma_200: float | None = None
    sma_50_vs_200: float | None = None
    sma_200_slope_63d: float | None = None
    rsi_14: float | None = None
    atr_14_pct: float | None = None
    realized_vol_60d: float | None = None
    max_drawdown_252d: float | None = None
    pct_from_52w_high: float | None = None
    trend_quality_score: float | None = None
    relative_strength_score: float | None = None
    liquidity_score: float | None = None
    volume_breakout_score: float | None = None
    volatility_risk_score: float | None = None
    technical_entry_score: float | None = None
    technical_setup_score: float | None = None
    technical_core_score: float | None = None
    technical_alpha_score: float | None = None
    technical_pullback_score: float | None = None
    technical_overextension_score: float | None = None
    technical_breakdown_flag: int = 0
    technical_liquidity_gate_flag: int = 0
    technical_signal_mode: str = TECHNICAL_MODE_LEGACY
    technical_signal_direction: str = "positive"
    technical_signal_reliability: float = 1.0
    technical_policy_reason: str = ""
    entry_signal: str = "unavailable"
    data_quality_status: str = "fail"
    missing_fields: list[str] = field(default_factory=list)
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class TechnicalSignalProfile:
    mode: str = TECHNICAL_MODE_LEGACY
    reliability: float = 1.0
    trend_weight: float = 0.50
    relative_strength_weight: float = 0.45
    volume_weight: float = 0.05
    volatility_weight: float = 0.0
    breakdown_floor: float = 35.0
    breakdown_cap: float = 45.0
    overextension_penalty_max: float = 0.0
    min_avg_dollar_volume_60d: float = 1_000_000.0
    rationale: str = ""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build med-device technical entry feature rows.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--output-csv", type=Path, default=None)
    parser.add_argument("--asof", type=str, default="")
    parser.add_argument("--tickers", type=str, default="")
    parser.add_argument("--max-tickers", type=int, default=0)
    parser.add_argument("--include-historical-members", action="store_true")
    return parser.parse_args()


def parse_date(raw: object) -> date | None:
    text = str(raw or "").strip()[:10]
    if not text:
        return None
    try:
        return datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError:
        return None


def to_float(raw: object) -> float | None:
    try:
        value = float(str(raw).strip())
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def safe_div(numerator: float | None, denominator: float | None) -> float | None:
    if numerator is None or denominator is None or abs(denominator) < 1e-12:
        return None
    value = numerator / denominator
    return value if math.isfinite(value) else None


def clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    if not math.isfinite(value):
        return low
    return max(low, min(high, value))


def bool_from_raw(raw: object, default: bool) -> bool:
    if raw is None:
        return default
    text = str(raw).strip().lower()
    if text in {"1", "true", "yes", "y", "on"}:
        return True
    if text in {"0", "false", "no", "n", "off"}:
        return False
    return default


def table_exists(conn: Any, table_name: str) -> bool:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table_name,),
    ).fetchone()
    return row is not None


def parse_float_config(raw: object, default: float) -> float:
    value = to_float(raw)
    return default if value is None else value


def normalize_weights(*weights: float) -> list[float]:
    clean = [max(0.0, weight) for weight in weights]
    total = sum(clean)
    if total <= 1e-12:
        return [1.0 / len(clean) for _ in clean]
    return [weight / total for weight in clean]


def parse_signal_profile(raw: object, *, default: TechnicalSignalProfile, context: str) -> TechnicalSignalProfile:
    if raw is None:
        return default
    if not isinstance(raw, dict):
        raise ValueError(f"{context} must be a mapping")
    mode = str(raw.get("mode", default.mode)).strip().lower()
    if mode not in TECHNICAL_SIGNAL_MODES:
        raise ValueError(f"{context}.mode must be one of {sorted(TECHNICAL_SIGNAL_MODES)}, got {mode!r}")
    return TechnicalSignalProfile(
        mode=mode,
        reliability=clamp(parse_float_config(raw.get("reliability"), default.reliability), 0.0, 1.0),
        trend_weight=parse_float_config(raw.get("trend_weight"), default.trend_weight),
        relative_strength_weight=parse_float_config(raw.get("relative_strength_weight"), default.relative_strength_weight),
        volume_weight=parse_float_config(raw.get("volume_weight"), default.volume_weight),
        volatility_weight=parse_float_config(raw.get("volatility_weight"), default.volatility_weight),
        breakdown_floor=parse_float_config(raw.get("breakdown_floor"), default.breakdown_floor),
        breakdown_cap=parse_float_config(raw.get("breakdown_cap"), default.breakdown_cap),
        overextension_penalty_max=max(
            0.0,
            parse_float_config(raw.get("overextension_penalty_max"), default.overextension_penalty_max),
        ),
        min_avg_dollar_volume_60d=max(
            0.0,
            parse_float_config(raw.get("min_avg_dollar_volume_60d"), default.min_avg_dollar_volume_60d),
        ),
        rationale=str(raw.get("rationale", default.rationale) or "").strip(),
    )


def load_signal_profiles(config: dict[str, Any]) -> tuple[TechnicalSignalProfile, dict[str, TechnicalSignalProfile], int]:
    default_profile = parse_signal_profile(
        cfg_get(config, "technical_features.default_signal_profile", None),
        default=TechnicalSignalProfile(rationale="default_legacy_setup_score"),
        context="technical_features.default_signal_profile",
    )
    min_n = int(parse_float_config(cfg_get(config, "technical_features.cohort_rank_min_n", 8), 8.0))
    raw_profiles = cfg_get(config, "technical_features.cohort_signal_profiles", {}) or {}
    if not isinstance(raw_profiles, dict):
        raise ValueError("technical_features.cohort_signal_profiles must be a mapping when provided")
    profiles: dict[str, TechnicalSignalProfile] = {}
    for cohort, raw_profile in raw_profiles.items():
        if not isinstance(raw_profile, dict):
            continue
        if not bool_from_raw(raw_profile.get("enabled"), True):
            continue
        profiles[str(cohort)] = parse_signal_profile(
            raw_profile,
            default=default_profile,
            context=f"technical_features.cohort_signal_profiles.{cohort}",
        )
    return default_profile, profiles, max(2, min_n)


def load_companies(
    conn: Any,
    *,
    asof: date,
    ticker_filter: set[str],
    max_tickers: int,
    include_historical_members: bool,
) -> list[Company]:
    if table_exists(conn, "dim_company_model_taxonomy"):
        rows = conn.execute(
            """
            SELECT c.company_id, c.ticker, c.company_name, c.subsector, t.calibration_cohort
            FROM dim_company c
            LEFT JOIN dim_company_model_taxonomy t ON t.company_id = c.company_id
            WHERE c.is_active = 1
               OR (? = 1 AND EXISTS (
                    SELECT 1
                    FROM dim_universe_membership m
                    WHERE m.company_id = c.company_id
                      AND m.model_family = 'med_devices'
                      AND m.point_in_time_flag = 1
                      AND m.start_date <= ?
                      AND (m.end_date IS NULL OR m.end_date >= ?)
               ))
            ORDER BY c.ticker
            """,
            (1 if include_historical_members else 0, asof.isoformat(), asof.isoformat()),
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT company_id, ticker, company_name, subsector, '' AS calibration_cohort
            FROM dim_company c
            WHERE c.is_active = 1
               OR (? = 1 AND EXISTS (
                    SELECT 1
                    FROM dim_universe_membership m
                    WHERE m.company_id = c.company_id
                      AND m.model_family = 'med_devices'
                      AND m.point_in_time_flag = 1
                      AND m.start_date <= ?
                      AND (m.end_date IS NULL OR m.end_date >= ?)
               ))
            ORDER BY ticker
            """,
            (1 if include_historical_members else 0, asof.isoformat(), asof.isoformat()),
        ).fetchall()
    out: list[Company] = []
    for row in rows:
        ticker = normalize_ticker(row["ticker"])
        if ticker_filter and ticker not in ticker_filter:
            continue
        out.append(
            Company(
                int(row["company_id"]),
                ticker,
                str(row["company_name"] or ""),
                str(row["subsector"] or ""),
                str(row["calibration_cohort"] or ""),
            )
        )
        if max_tickers > 0 and len(out) >= max_tickers:
            break
    return out


def latest_market_asof(conn: Any, sources: list[str]) -> str:
    placeholders = ",".join("?" for _ in sources)
    row = conn.execute(
        f"""
        SELECT MAX(bar_date) AS max_bar_date
        FROM fact_price_ohlcv
        WHERE source_id IN ({placeholders})
        """,
        sources,
    ).fetchone()
    asof = str(row["max_bar_date"] or "") if row is not None else ""
    if not asof:
        raise ValueError("No market bars found for configured scoring sources")
    return asof


def latest_financial_asof(conn: Any) -> str:
    row = conn.execute("SELECT MAX(asof_date) AS asof_date FROM feature_financial_valuation").fetchone()
    return str(row["asof_date"] or "") if row is not None else ""


def configured_benchmark(config: dict[str, Any]) -> str:
    ticker = normalize_ticker(cfg_get(config, "technical_features.benchmark_ticker", ""))
    if ticker:
        return ticker
    benchmarks = cfg_get(config, "med_devices_universe.benchmark_tickers", []) or []
    if isinstance(benchmarks, list) and benchmarks:
        return normalize_ticker(benchmarks[0])
    return "IHI"


def entry_signal_thresholds(config: dict[str, Any]) -> dict[str, float]:
    return {
        "strong_entry": float(cfg_get(config, "technical_features.entry_signal_thresholds.strong_entry", 80.0)),
        "good_entry": float(cfg_get(config, "technical_features.entry_signal_thresholds.good_entry", 65.0)),
        "watchlist": float(cfg_get(config, "technical_features.entry_signal_thresholds.watchlist", 50.0)),
    }


def load_price_history(
    conn: Any,
    *,
    tickers: list[str],
    sources: list[str],
    asof: date,
    lookback_days: int,
) -> dict[str, list[Bar]]:
    if not tickers:
        return {}
    ticker_clause = ",".join("?" for _ in tickers)
    source_clause = ",".join("?" for _ in sources)
    start = (asof - timedelta(days=lookback_days)).isoformat()
    rows = conn.execute(
        f"""
        SELECT ticker, source_id, bar_date, high, low, close, adj_close, volume
        FROM fact_price_ohlcv
        WHERE ticker IN ({ticker_clause})
          AND source_id IN ({source_clause})
          AND bar_date BETWEEN ? AND ?
        ORDER BY ticker, bar_date, source_id
        """,
        [*tickers, *sources, start, asof.isoformat()],
    ).fetchall()
    priority = {source: idx for idx, source in enumerate(sources)}
    best: dict[tuple[str, str], Bar] = {}
    for row in rows:
        ticker = normalize_ticker(row["ticker"])
        bar_date = str(row["bar_date"] or "")
        adjusted_close = to_float(row["adj_close"])
        raw_close = to_float(row["close"])
        close = adjusted_close if adjusted_close is not None else raw_close
        if not ticker or not bar_date or close is None or close <= 0:
            continue
        candidate = Bar(
            ticker=ticker,
            source_id=str(row["source_id"] or ""),
            bar_date=bar_date,
            close=close,
            raw_close=raw_close,
            high=to_float(row["high"]),
            low=to_float(row["low"]),
            volume=to_float(row["volume"]),
        )
        key = (ticker, bar_date)
        existing = best.get(key)
        if existing is None or priority.get(candidate.source_id, 999) < priority.get(existing.source_id, 999):
            best[key] = candidate
    out: dict[str, list[Bar]] = {}
    for bar in best.values():
        out.setdefault(bar.ticker, []).append(bar)
    for bars in out.values():
        bars.sort(key=lambda item: item.bar_date)
    return out


def moving_average(values: list[float], window: int) -> float | None:
    if len(values) < window:
        return None
    return sum(values[-window:]) / window


def trailing_return(values: list[float], periods: int) -> float | None:
    if len(values) <= periods:
        return None
    return safe_div(values[-1] - values[-periods - 1], values[-periods - 1])


def momentum_12_1(values: list[float]) -> float | None:
    if len(values) < 253:
        return None
    return safe_div(values[-22] - values[-253], values[-253])


def rsi(values: list[float], window: int = 14) -> float | None:
    if len(values) < window + 2:
        return None
    deltas = [cur - prev for prev, cur in zip(values[:-1], values[1:])]
    if len(deltas) < window:
        return None
    seed = deltas[:window]
    avg_gain = sum(max(0.0, value) for value in seed) / window
    avg_loss = sum(max(0.0, -value) for value in seed) / window
    for diff in deltas[window:]:
        gain = max(0.0, diff)
        loss = max(0.0, -diff)
        avg_gain = ((avg_gain * (window - 1)) + gain) / window
        avg_loss = ((avg_loss * (window - 1)) + loss) / window
    if avg_loss <= 1e-12:
        return 100.0
    return 100.0 - (100.0 / (1.0 + avg_gain / avg_loss))


def realized_vol(values: list[float], window: int) -> float | None:
    if len(values) <= window:
        return None
    returns = [safe_div(cur - prev, prev) for prev, cur in zip(values[-window - 1 : -1], values[-window:])]
    clean = [value for value in returns if value is not None]
    if len(clean) < 2:
        return None
    return stdev(clean) * math.sqrt(252.0)


def max_drawdown(values: list[float], window: int) -> float | None:
    if len(values) < 2:
        return None
    subset = values[-window:] if len(values) >= window else values
    peak = subset[0]
    worst = 0.0
    for value in subset:
        peak = max(peak, value)
        worst = min(worst, safe_div(value - peak, peak) or 0.0)
    return worst


def atr_pct(bars: list[Bar], window: int = 14) -> float | None:
    if len(bars) < window + 2:
        return None
    lookback = bars[-(window * 2 + 1) :]
    ranges: list[float] = []
    for prev, cur in zip(lookback[:-1], lookback[1:]):
        high = cur.high if cur.high is not None else cur.close
        low = cur.low if cur.low is not None else cur.close
        true_range = max(high - low, abs(high - prev.close), abs(low - prev.close))
        ranges.append(true_range)
    if len(ranges) < window:
        return None
    atr = sum(ranges[:window]) / window
    for true_range in ranges[window:]:
        atr = ((atr * (window - 1)) + true_range) / window
    return safe_div(atr, bars[-1].close)


def staleness_days(latest_price_date: str, asof: date) -> int | None:
    parsed = parse_date(latest_price_date)
    if parsed is None:
        return None
    return (asof - parsed).days


def percentile_scores(rows: list[TechnicalRow], field_name: str, *, higher_is_better: bool) -> dict[int, float]:
    pairs: list[tuple[int, float]] = []
    for idx, row in enumerate(rows):
        value = to_float(getattr(row, field_name))
        if value is not None:
            pairs.append((idx, value))
    if not pairs:
        return {}
    if len(pairs) >= 4:
        sorted_values = sorted(value for _, value in pairs)
        low_bound = sorted_values[max(0, min(len(sorted_values) - 1, math.ceil(TECH_WINSOR_LOW_PCT * len(sorted_values)) - 1))]
        high_bound = sorted_values[max(0, min(len(sorted_values) - 1, math.ceil(TECH_WINSOR_HIGH_PCT * len(sorted_values)) - 1))]
        if low_bound > high_bound:
            low_bound, high_bound = high_bound, low_bound
        pairs = [(idx, max(low_bound, min(high_bound, value))) for idx, value in pairs]
    pairs.sort(key=lambda item: item[1])
    if len(pairs) == 1:
        return {pairs[0][0]: 50.0}
    out: dict[int, float] = {}
    denominator = len(pairs) - 1
    for rank, (idx, _) in enumerate(pairs):
        pct = 100.0 * rank / denominator
        out[idx] = pct if higher_is_better else 100.0 - pct
    return out


def percentile_scores_for_indices(
    rows: list[TechnicalRow],
    field_name: str,
    indices: list[int],
    *,
    higher_is_better: bool,
) -> dict[int, float]:
    subset = [rows[idx] for idx in indices]
    local_scores = percentile_scores(subset, field_name, higher_is_better=higher_is_better)
    return {indices[local_idx]: score for local_idx, score in local_scores.items()}


def grouped_percentile_scores(
    rows: list[TechnicalRow],
    field_name: str,
    *,
    higher_is_better: bool,
    min_group_n: int,
) -> dict[int, float]:
    global_scores = percentile_scores(rows, field_name, higher_is_better=higher_is_better)
    grouped: dict[str, list[int]] = {}
    for idx, row in enumerate(rows):
        value = to_float(getattr(row, field_name))
        if value is None:
            continue
        grouped.setdefault(row.calibration_cohort or row.subsector or "unknown", []).append(idx)
    out: dict[int, float] = {}
    for indices in grouped.values():
        if len(indices) >= min_group_n:
            out.update(
                percentile_scores_for_indices(
                    rows,
                    field_name,
                    indices,
                    higher_is_better=higher_is_better,
                )
            )
        else:
            for idx in indices:
                if idx in global_scores:
                    out[idx] = global_scores[idx]
    return out


def build_raw_row(
    company: Company,
    bars: list[Bar],
    benchmark: dict[str, float | None],
    *,
    asof: date,
    min_trading_bars: int,
    max_staleness_days: int,
) -> TechnicalRow:
    row = TechnicalRow(
        asof.isoformat(),
        company.company_id,
        company.ticker,
        company.company_name,
        company.subsector,
        company.calibration_cohort,
    )
    if not bars:
        row.missing_fields = ["price_history"]
        return row
    closes = [bar.close for bar in bars]
    row.market_source_id = bars[-1].source_id
    row.latest_price_date = bars[-1].bar_date
    row.latest_close = bars[-1].close
    recent_bars = bars[-60:]
    row.avg_dollar_volume_60d = sum(bar.close * (bar.volume or 0.0) for bar in recent_bars) / max(1, len(recent_bars))
    recent_volumes = [float(bar.volume) for bar in bars[-5:] if bar.volume is not None]
    historical_volumes = [float(bar.volume) for bar in bars[-60:-5] if bar.volume is not None]
    if len(recent_volumes) >= 3 and len(historical_volumes) >= 40:
        row.volume_trend_ratio = safe_div(
            sum(recent_volumes) / len(recent_volumes),
            sum(historical_volumes) / len(historical_volumes),
        )
    row.return_21d = trailing_return(closes, 21)
    row.return_63d = trailing_return(closes, 63)
    row.return_126d = trailing_return(closes, 126)
    row.return_252d = trailing_return(closes, 252)
    row.momentum_12_1 = momentum_12_1(closes)
    row.relative_strength_63d = row.return_63d - benchmark["return_63d"] if row.return_63d is not None and benchmark["return_63d"] is not None else None
    row.relative_strength_126d = row.return_126d - benchmark["return_126d"] if row.return_126d is not None and benchmark["return_126d"] is not None else None
    row.sma_50 = moving_average(closes, 50)
    row.sma_200 = moving_average(closes, 200)
    row.price_vs_sma_50 = safe_div(row.latest_close - row.sma_50, row.sma_50) if row.sma_50 else None
    row.price_vs_sma_200 = safe_div(row.latest_close - row.sma_200, row.sma_200) if row.sma_200 else None
    row.sma_50_vs_200 = safe_div(row.sma_50 - row.sma_200, row.sma_200) if row.sma_50 and row.sma_200 else None
    prior_sma_200 = moving_average(closes[:-63], 200) if len(closes) >= 263 else None
    row.sma_200_slope_63d = safe_div(row.sma_200 - prior_sma_200, prior_sma_200) if row.sma_200 and prior_sma_200 else None
    row.rsi_14 = rsi(closes, 14)
    row.atr_14_pct = atr_pct(bars, 14)
    row.realized_vol_60d = realized_vol(closes, 60)
    row.max_drawdown_252d = max_drawdown(closes, 252)
    high_252 = max(closes[-252:]) if len(closes) >= 252 else max(closes)
    row.pct_from_52w_high = safe_div(row.latest_close - high_252, high_252)
    row.missing_fields = [field for field in ["sma_200", "rsi_14", "atr_14_pct", "momentum_12_1"] if getattr(row, field) is None]
    stale_days = staleness_days(row.latest_price_date, asof)
    row.data_quality_status = (
        "pass"
        if len(bars) >= min_trading_bars and stale_days is not None and 0 <= stale_days <= max_staleness_days
        else "review"
    )
    row.payload = {"bar_count": len(bars), "benchmark": benchmark}
    return row


def cross_rank_trend_quality(rows: list[TechnicalRow], *, min_group_n: int) -> None:
    score_map = grouped_percentile_scores(
        rows,
        "trend_quality_score",
        higher_is_better=True,
        min_group_n=min_group_n,
    )
    for idx, score in score_map.items():
        rows[idx].trend_quality_score = round(score, 2)


def overextension_score(row: TechnicalRow) -> float:
    score = 0.0
    if row.rsi_14 is not None and row.rsi_14 > 65.0:
        score += min(30.0, max(0.0, (row.rsi_14 - 65.0) * 1.5))
    if row.price_vs_sma_50 is not None and row.price_vs_sma_50 > 0.10:
        score += min(30.0, max(0.0, (row.price_vs_sma_50 - 0.10) * 150.0))
    if row.atr_14_pct is not None and row.atr_14_pct > 1e-6 and row.price_vs_sma_50 is not None:
        atr_distance = row.price_vs_sma_50 / row.atr_14_pct
        if atr_distance > 3.0:
            score += min(25.0, (atr_distance - 3.0) * 5.0)
    if row.pct_from_52w_high is not None and row.pct_from_52w_high >= -0.05 and (row.return_126d or 0.0) > 0.20:
        score += 15.0
    return round(clamp(score), 2)


def technical_breakdown_flag(row: TechnicalRow, profile: TechnicalSignalProfile, core_score: float) -> int:
    if core_score < profile.breakdown_floor:
        return 1
    if row.max_drawdown_252d is not None and row.max_drawdown_252d <= -0.50:
        return 1
    if (
        row.price_vs_sma_200 is not None
        and row.price_vs_sma_200 <= -0.20
        and row.sma_50_vs_200 is not None
        and row.sma_50_vs_200 < 0
    ):
        return 1
    return 0


TECHNICAL_PROFILE_COHORT_ALIASES = {
    "capital_equipment_procedure_platforms": "capital_equipment_imaging_monitoring",
    "home_chronic_care_devices_dme_drug_delivery": "diabetes_wearables_drug_delivery",
    "healthcare_services_cro_lab_services": "healthcare_services_cro_other",
    "hospital_supplies_surgical_consumables_oem": "hospital_supplies_consumables_dme",
    "orthopedics_spine_sports_implants": "orthopedics_spine_dental",
    "surgical_robotics_platforms": "capital_equipment_procedure_platforms",
}


def profile_for_row(
    row: TechnicalRow,
    default_profile: TechnicalSignalProfile,
    profiles: dict[str, TechnicalSignalProfile],
) -> TechnicalSignalProfile:
    cohort = str(row.calibration_cohort or "")
    if cohort in profiles:
        return profiles[cohort]
    alias = TECHNICAL_PROFILE_COHORT_ALIASES.get(cohort)
    if alias and alias in profiles:
        return profiles[alias]
    return default_profile


def apply_alpha_score(row: TechnicalRow, profile: TechnicalSignalProfile) -> None:
    trend_score = row.trend_quality_score if row.trend_quality_score is not None else 50.0
    relative_strength_score = row.relative_strength_score if row.relative_strength_score is not None else 50.0
    volume_score = row.volume_breakout_score if row.volume_breakout_score is not None else 50.0
    volatility_score = row.volatility_risk_score if row.volatility_risk_score is not None else 50.0
    trend_w, rs_w, volume_w, volatility_w = normalize_weights(
        profile.trend_weight,
        profile.relative_strength_weight,
        profile.volume_weight,
        profile.volatility_weight,
    )
    core_score = clamp(
        trend_w * trend_score
        + rs_w * relative_strength_score
        + volume_w * volume_score
        + volatility_w * volatility_score
    )
    pullback_score = 100.0 - core_score
    overextended = overextension_score(row)
    breakdown = technical_breakdown_flag(row, profile, core_score)
    setup_score = row.technical_setup_score if row.technical_setup_score is not None else 50.0
    if profile.mode == TECHNICAL_MODE_NEUTRAL_OVERLAY:
        alpha = 50.0
        direction = "neutral"
    elif profile.mode == TECHNICAL_MODE_PULLBACK_MEAN_REVERSION:
        alpha = 50.0 + profile.reliability * (pullback_score - 50.0)
        direction = "inverse"
        if breakdown:
            alpha = min(alpha, profile.breakdown_cap)
    elif profile.mode == TECHNICAL_MODE_TREND_FOLLOWING:
        alpha = 50.0 + profile.reliability * (core_score - 50.0)
        alpha -= (overextended / 100.0) * profile.overextension_penalty_max
        direction = "positive"
        if breakdown:
            alpha = min(alpha, profile.breakdown_cap)
    else:
        alpha = setup_score
        direction = "positive"

    row.technical_core_score = round(core_score, 2)
    row.technical_pullback_score = round(pullback_score, 2)
    row.technical_overextension_score = overextended
    row.technical_breakdown_flag = breakdown
    row.technical_liquidity_gate_flag = int(
        row.avg_dollar_volume_60d is not None and row.avg_dollar_volume_60d >= profile.min_avg_dollar_volume_60d
    )
    row.technical_alpha_score = round(clamp(alpha), 2)
    row.technical_signal_mode = profile.mode
    row.technical_signal_direction = direction
    row.technical_signal_reliability = round(profile.reliability, 4)
    row.technical_policy_reason = profile.rationale


def apply_scores(
    rows: list[TechnicalRow],
    *,
    entry_thresholds: dict[str, float],
    default_profile: TechnicalSignalProfile,
    profiles: dict[str, TechnicalSignalProfile],
    cohort_rank_min_n: int,
) -> None:
    score_maps = {
        "relative_strength_63d": grouped_percentile_scores(
            rows,
            "relative_strength_63d",
            higher_is_better=True,
            min_group_n=cohort_rank_min_n,
        ),
        "relative_strength_126d": grouped_percentile_scores(
            rows,
            "relative_strength_126d",
            higher_is_better=True,
            min_group_n=cohort_rank_min_n,
        ),
        "avg_dollar_volume_60d": grouped_percentile_scores(
            rows,
            "avg_dollar_volume_60d",
            higher_is_better=True,
            min_group_n=cohort_rank_min_n,
        ),
        "volume_trend_ratio": grouped_percentile_scores(
            rows,
            "volume_trend_ratio",
            higher_is_better=True,
            min_group_n=cohort_rank_min_n,
        ),
        "realized_vol_60d": grouped_percentile_scores(
            rows,
            "realized_vol_60d",
            higher_is_better=False,
            min_group_n=cohort_rank_min_n,
        ),
    }
    failed_indices: set[int] = set()
    for idx, row in enumerate(rows):
        if row.latest_close is None or row.sma_50 is None:
            row.technical_entry_score = 0.0
            row.technical_setup_score = 0.0
            row.technical_core_score = 50.0
            row.technical_alpha_score = 50.0
            row.technical_pullback_score = 50.0
            row.technical_overextension_score = 0.0
            row.technical_breakdown_flag = 0
            row.technical_liquidity_gate_flag = 0
            row.technical_signal_mode = TECHNICAL_MODE_NEUTRAL_OVERLAY
            row.technical_signal_direction = "neutral"
            row.technical_signal_reliability = 0.0
            row.technical_policy_reason = "neutral_alpha_for_missing_price_history"
            row.entry_signal = "insufficient_price_history"
            row.data_quality_status = "fail"
            failed_indices.add(idx)
            continue
        trend = 50.0
        trend += 10.0 if row.price_vs_sma_50 is not None and row.price_vs_sma_50 > 0 else -10.0
        if row.sma_200 is not None:
            trend += 15.0 if row.price_vs_sma_200 is not None and row.price_vs_sma_200 > 0 else -20.0
            trend += 10.0 if row.sma_50_vs_200 is not None and row.sma_50_vs_200 > 0 else -10.0
            trend += 10.0 if row.sma_200_slope_63d is not None and row.sma_200_slope_63d > 0 else -5.0
        else:
            trend -= 5.0
        if row.pct_from_52w_high is not None and row.pct_from_52w_high >= -0.10 and (row.return_126d or 0.0) > 0:
            trend += 5.0
        if row.atr_14_pct is not None and row.atr_14_pct > 0.08:
            trend -= 5.0
        row.trend_quality_score = round(clamp(trend), 2)

    cross_rank_trend_quality(rows, min_group_n=cohort_rank_min_n)

    for idx, row in enumerate(rows):
        if idx in failed_indices:
            continue
        row.relative_strength_score = round(
            clamp(
                0.60 * score_maps["relative_strength_63d"].get(idx, 50.0)
                + 0.40 * score_maps["relative_strength_126d"].get(idx, 50.0)
            ),
            2,
        )
        row.liquidity_score = round(clamp(score_maps["avg_dollar_volume_60d"].get(idx, 50.0)), 2)
        row.volume_breakout_score = round(clamp(score_maps["volume_trend_ratio"].get(idx, 50.0)), 2)
        row.volatility_risk_score = round(clamp(score_maps["realized_vol_60d"].get(idx, 50.0)), 2)
        trend_quality_score = row.trend_quality_score if row.trend_quality_score is not None else 50.0
        score = (
            0.35 * trend_quality_score
            + 0.30 * row.relative_strength_score
            + 0.15 * row.liquidity_score
            + 0.10 * row.volume_breakout_score
            + 0.10 * row.volatility_risk_score
        )
        row.technical_entry_score = round(clamp(score), 2)
        row.technical_setup_score = row.technical_entry_score
        apply_alpha_score(row, profile_for_row(row, default_profile, profiles))
        if row.technical_entry_score >= entry_thresholds["strong_entry"]:
            row.entry_signal = "strong_entry"
        elif row.technical_entry_score >= entry_thresholds["good_entry"]:
            row.entry_signal = "good_entry"
        elif row.technical_entry_score >= entry_thresholds["watchlist"]:
            row.entry_signal = "watchlist_wait_for_confirmation"
        else:
            row.entry_signal = "weak_entry"
        if row.missing_fields and row.entry_signal in {"strong_entry", "good_entry"}:
            row.entry_signal = "watchlist_wait_for_history"
        row.payload["component_scores"] = {
            "trend_quality": row.trend_quality_score,
            "relative_strength": row.relative_strength_score,
            "liquidity": row.liquidity_score,
            "volume_breakout": row.volume_breakout_score,
            "volatility_risk": row.volatility_risk_score,
            "setup": row.technical_setup_score,
            "core": row.technical_core_score,
            "alpha": row.technical_alpha_score,
        }
        row.payload["technical_signal_profile"] = {
            "mode": row.technical_signal_mode,
            "direction": row.technical_signal_direction,
            "reliability": row.technical_signal_reliability,
            "reason": row.technical_policy_reason,
            "breakdown_flag": row.technical_breakdown_flag,
            "liquidity_gate_flag": row.technical_liquidity_gate_flag,
        }


def upsert_rows(conn: Any, rows: list[TechnicalRow]) -> int:
    if not rows:
        return 0
    now = utc_now()
    fields = [
        "asof_date",
        "company_id",
        "ticker",
        "company_name",
        "subsector",
        "calibration_cohort",
        "market_source_id",
        "latest_price_date",
        "latest_close",
        "avg_dollar_volume_60d",
        "volume_trend_ratio",
        "return_21d",
        "return_63d",
        "return_126d",
        "return_252d",
        "momentum_12_1",
        "relative_strength_63d",
        "relative_strength_126d",
        "sma_50",
        "sma_200",
        "price_vs_sma_50",
        "price_vs_sma_200",
        "sma_50_vs_200",
        "sma_200_slope_63d",
        "rsi_14",
        "atr_14_pct",
        "realized_vol_60d",
        "max_drawdown_252d",
        "pct_from_52w_high",
        "technical_score",
        "technical_setup_score",
        "technical_core_score",
        "technical_alpha_score",
        "technical_pullback_score",
        "technical_overextension_score",
        "technical_breakdown_flag",
        "technical_liquidity_gate_flag",
        "technical_signal_mode",
        "technical_signal_direction",
        "technical_signal_reliability",
        "technical_policy_reason",
        "trend_quality_score",
        "relative_strength_score",
        "liquidity_score",
        "volume_breakout_score",
        "volatility_risk_score",
        "entry_signal",
        "data_quality_status",
        "missing_fields",
        "payload_json",
    ]
    payload_rows: list[tuple[Any, ...]] = []
    for row in rows:
        values = {field: getattr(row, field, None) for field in fields}
        values["technical_score"] = row.technical_entry_score
        values["technical_setup_score"] = row.technical_setup_score
        values["technical_alpha_score"] = row.technical_alpha_score
        values["missing_fields"] = ";".join(row.missing_fields)
        values["payload_json"] = json.dumps(
            {
                "source": "technical_entry_builder",
                "entry_signal": row.entry_signal,
                "data_quality_status": row.data_quality_status,
                "missing_fields": row.missing_fields,
                **row.payload,
            },
            ensure_ascii=True,
            sort_keys=True,
        )
        payload_rows.append(tuple(values[field] for field in fields) + (now, now))
    field_sql = ", ".join(fields)
    placeholder_sql = ", ".join("?" for _ in fields)
    update_sql = ",\n            ".join(
        f"{field} = excluded.{field}" for field in fields if field not in {"asof_date", "company_id"}
    )
    conn.executemany(
        f"""
        INSERT INTO feature_technical_entry(
            {field_sql}, created_at, updated_at
        )
        VALUES ({placeholder_sql}, ?, ?)
        ON CONFLICT(asof_date, company_id) DO UPDATE SET
            {update_sql},
            updated_at = excluded.updated_at
        """,
        payload_rows,
    )
    return len(rows)


def row_to_dict(row: TechnicalRow) -> dict[str, Any]:
    out = {field: getattr(row, field) for field in FIELDNAMES if hasattr(row, field)}
    out["technical_score"] = row.technical_entry_score
    out["missing_fields"] = ";".join(row.missing_fields)
    return out


def write_csv(path: Path, rows: list[TechnicalRow]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDNAMES, extrasaction="ignore", lineterminator="\n")
        writer.writeheader()
        writer.writerows(row_to_dict(row) for row in rows)


def main() -> None:
    configure_utc_logging()
    args = parse_args()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    base_dir = config_path.parent
    db_path = args.db.expanduser().resolve() if args.db else resolve_path(cfg_get(config, "paths.database_path"), base_dir=base_dir)
    output_csv = (
        args.output_csv.expanduser().resolve()
        if args.output_csv
        else resolve_path(
            cfg_get(config, "technical_features.output_csv", "../output/med_devices_reports/med_device_technical_entry_features.csv"),
            base_dir=base_dir,
        )
    )
    sources = scoring_market_sources(config)
    lookback_days = int(cfg_get(config, "technical_features.lookback_days", 460))
    min_trading_bars = int(cfg_get(config, "technical_features.min_trading_bars", 220))
    max_staleness_days = int(cfg_get(config, "technical_features.max_staleness_days", 7))
    thresholds = entry_signal_thresholds(config)
    default_profile, signal_profiles, cohort_rank_min_n = load_signal_profiles(config)
    benchmark_ticker = configured_benchmark(config)
    ticker_filter = {normalize_ticker(value) for value in str(args.tickers or "").split(",") if normalize_ticker(value)}
    with connect(db_path, timeout_sec=float(cfg_get(config, "runtime.sqlite_timeout_sec", 30.0))) as conn:
        init_db(conn)
        run_id = start_run(conn, run_type="build_med_device_technical_features", input_path=config_path)
        try:
            asof_text = args.asof.strip() if args.asof else (latest_financial_asof(conn) or latest_market_asof(conn, sources))
            asof = parse_date(asof_text)
            if asof is None:
                raise ValueError(f"Invalid as-of date: {asof_text}")
            companies = load_companies(
                conn,
                asof=asof,
                ticker_filter=ticker_filter,
                max_tickers=int(args.max_tickers),
                include_historical_members=bool(args.include_historical_members),
            )
            if not companies:
                raise ValueError("No active or point-in-time historical companies selected")
            tickers = sorted({company.ticker for company in companies} | {benchmark_ticker})
            histories = load_price_history(conn, tickers=tickers, sources=sources, asof=asof, lookback_days=lookback_days)
            benchmark_closes = [bar.close for bar in histories.get(benchmark_ticker, [])]
            benchmark = {
                "ticker": benchmark_ticker,
                "return_63d": trailing_return(benchmark_closes, 63),
                "return_126d": trailing_return(benchmark_closes, 126),
            }
            rows = [
                build_raw_row(
                    company,
                    histories.get(company.ticker, []),
                    benchmark,
                    asof=asof,
                    min_trading_bars=min_trading_bars,
                    max_staleness_days=max_staleness_days,
                )
                for company in companies
            ]
            apply_scores(
                rows,
                entry_thresholds=thresholds,
                default_profile=default_profile,
                profiles=signal_profiles,
                cohort_rank_min_n=cohort_rank_min_n,
            )
            upserted = upsert_rows(conn, rows)
            write_csv(output_csv, rows)
            message = f"asof={asof.isoformat()} rows={upserted} output={output_csv}"
            finish_run(conn, run_id=run_id, status="success", row_count=upserted, message=message)
            LOGGER.info("Technical features complete: %s", message)
        except BaseException as exc:
            finish_run(conn, run_id=run_id, status="failed", row_count=0, message=f"{type(exc).__name__}: {exc}")
            raise


if __name__ == "__main__":
    main()
