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
FIELDNAMES = [
    "asof_date",
    "company_id",
    "ticker",
    "company_name",
    "subsector",
    "market_source_id",
    "latest_price_date",
    "latest_close",
    "avg_dollar_volume_60d",
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
    "volatility_risk_score",
    "technical_entry_score",
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


@dataclass(frozen=True)
class Bar:
    ticker: str
    source_id: str
    bar_date: str
    close: float
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
    market_source_id: str = ""
    latest_price_date: str = ""
    latest_close: float | None = None
    avg_dollar_volume_60d: float | None = None
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
    volatility_risk_score: float | None = None
    technical_entry_score: float | None = None
    entry_signal: str = "unavailable"
    data_quality_status: str = "fail"
    missing_fields: list[str] = field(default_factory=list)
    payload: dict[str, Any] = field(default_factory=dict)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build med-device technical entry feature rows.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--output-csv", type=Path, default=None)
    parser.add_argument("--asof", type=str, default="")
    parser.add_argument("--tickers", type=str, default="")
    parser.add_argument("--max-tickers", type=int, default=0)
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
    return max(low, min(high, value))


def load_companies(conn: Any, *, ticker_filter: set[str], max_tickers: int) -> list[Company]:
    rows = conn.execute(
        """
        SELECT company_id, ticker, company_name, subsector
        FROM dim_company
        WHERE is_active = 1
        ORDER BY ticker
        """
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
    if len(values) <= window:
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
    if len(bars) <= window:
        return None
    ranges: list[float] = []
    for prev, cur in zip(bars[-window - 1 : -1], bars[-window:]):
        high = cur.high if cur.high is not None else cur.close
        low = cur.low if cur.low is not None else cur.close
        true_range = max(high - low, abs(high - prev.close), abs(low - prev.close))
        ranges.append(true_range)
    if not ranges:
        return None
    return safe_div(sum(ranges) / len(ranges), bars[-1].close)


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
    pairs.sort(key=lambda item: item[1])
    if len(pairs) == 1:
        return {pairs[0][0]: 50.0}
    out: dict[int, float] = {}
    denominator = len(pairs) - 1
    for rank, (idx, _) in enumerate(pairs):
        pct = 100.0 * rank / denominator
        out[idx] = pct if higher_is_better else 100.0 - pct
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
    row = TechnicalRow(asof.isoformat(), company.company_id, company.ticker, company.company_name, company.subsector)
    if not bars:
        row.missing_fields = ["price_history"]
        return row
    closes = [bar.close for bar in bars]
    row.market_source_id = bars[-1].source_id
    row.latest_price_date = bars[-1].bar_date
    row.latest_close = bars[-1].close
    row.avg_dollar_volume_60d = sum(bar.close * (bar.volume or 0.0) for bar in bars[-60:]) / min(len(bars), 60)
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


def apply_scores(rows: list[TechnicalRow], *, entry_thresholds: dict[str, float]) -> None:
    score_maps = {
        "relative_strength_63d": percentile_scores(rows, "relative_strength_63d", higher_is_better=True),
        "relative_strength_126d": percentile_scores(rows, "relative_strength_126d", higher_is_better=True),
        "avg_dollar_volume_60d": percentile_scores(rows, "avg_dollar_volume_60d", higher_is_better=True),
        "realized_vol_60d": percentile_scores(rows, "realized_vol_60d", higher_is_better=False),
    }
    for idx, row in enumerate(rows):
        if row.latest_close is None or row.sma_200 is None:
            row.technical_entry_score = 0.0
            row.entry_signal = "insufficient_price_history"
            row.data_quality_status = "fail"
            continue
        trend = 50.0
        trend += 15.0 if row.price_vs_sma_200 is not None and row.price_vs_sma_200 > 0 else -20.0
        trend += 10.0 if row.price_vs_sma_50 is not None and row.price_vs_sma_50 > 0 else -10.0
        trend += 10.0 if row.sma_50_vs_200 is not None and row.sma_50_vs_200 > 0 else -10.0
        trend += 10.0 if row.sma_200_slope_63d is not None and row.sma_200_slope_63d > 0 else -5.0
        if row.pct_from_52w_high is not None and row.pct_from_52w_high >= -0.10 and (row.return_126d or 0.0) > 0:
            trend += 5.0
        if row.atr_14_pct is not None and row.atr_14_pct > 0.08:
            trend -= 5.0
        row.trend_quality_score = round(clamp(trend), 2)
        row.relative_strength_score = round(
            clamp(
                0.60 * score_maps["relative_strength_63d"].get(idx, 50.0)
                + 0.40 * score_maps["relative_strength_126d"].get(idx, 50.0)
            ),
            2,
        )
        row.liquidity_score = round(clamp(score_maps["avg_dollar_volume_60d"].get(idx, 50.0)), 2)
        row.volatility_risk_score = round(clamp(score_maps["realized_vol_60d"].get(idx, 50.0)), 2)
        score = (
            0.35 * row.trend_quality_score
            + 0.30 * row.relative_strength_score
            + 0.20 * row.liquidity_score
            + 0.15 * row.volatility_risk_score
        )
        row.technical_entry_score = round(clamp(score), 2)
        if row.technical_entry_score >= entry_thresholds["strong_entry"]:
            row.entry_signal = "strong_entry"
        elif row.technical_entry_score >= entry_thresholds["good_entry"]:
            row.entry_signal = "good_entry"
        elif row.technical_entry_score >= entry_thresholds["watchlist"]:
            row.entry_signal = "watchlist_wait_for_confirmation"
        else:
            row.entry_signal = "weak_entry"
        row.payload["component_scores"] = {
            "trend_quality": row.trend_quality_score,
            "relative_strength": row.relative_strength_score,
            "liquidity": row.liquidity_score,
            "volatility_risk": row.volatility_risk_score,
        }


def upsert_rows(conn: Any, rows: list[TechnicalRow]) -> int:
    if not rows:
        return 0
    now = utc_now()
    conn.executemany(
        """
        INSERT INTO feature_technical_entry(
            asof_date, company_id, ticker, company_name, subsector, market_source_id,
            latest_price_date, latest_close, avg_dollar_volume_60d, return_21d,
            return_63d, return_126d, return_252d, momentum_12_1, relative_strength_63d,
            relative_strength_126d, sma_50, sma_200, price_vs_sma_50, price_vs_sma_200,
            sma_50_vs_200, sma_200_slope_63d, rsi_14, atr_14_pct, realized_vol_60d,
            max_drawdown_252d, pct_from_52w_high, score, trend_quality_score, relative_strength_score,
            liquidity_score, volatility_risk_score, entry_signal, data_quality_status,
            missing_fields, payload_json, created_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(asof_date, company_id) DO UPDATE SET
            ticker = excluded.ticker,
            company_name = excluded.company_name,
            subsector = excluded.subsector,
            market_source_id = excluded.market_source_id,
            latest_price_date = excluded.latest_price_date,
            latest_close = excluded.latest_close,
            avg_dollar_volume_60d = excluded.avg_dollar_volume_60d,
            return_21d = excluded.return_21d,
            return_63d = excluded.return_63d,
            return_126d = excluded.return_126d,
            return_252d = excluded.return_252d,
            momentum_12_1 = excluded.momentum_12_1,
            relative_strength_63d = excluded.relative_strength_63d,
            relative_strength_126d = excluded.relative_strength_126d,
            sma_50 = excluded.sma_50,
            sma_200 = excluded.sma_200,
            price_vs_sma_50 = excluded.price_vs_sma_50,
            price_vs_sma_200 = excluded.price_vs_sma_200,
            sma_50_vs_200 = excluded.sma_50_vs_200,
            sma_200_slope_63d = excluded.sma_200_slope_63d,
            rsi_14 = excluded.rsi_14,
            atr_14_pct = excluded.atr_14_pct,
            realized_vol_60d = excluded.realized_vol_60d,
            max_drawdown_252d = excluded.max_drawdown_252d,
            pct_from_52w_high = excluded.pct_from_52w_high,
            score = excluded.score,
            trend_quality_score = excluded.trend_quality_score,
            relative_strength_score = excluded.relative_strength_score,
            liquidity_score = excluded.liquidity_score,
            volatility_risk_score = excluded.volatility_risk_score,
            entry_signal = excluded.entry_signal,
            data_quality_status = excluded.data_quality_status,
            missing_fields = excluded.missing_fields,
            payload_json = excluded.payload_json,
            updated_at = excluded.updated_at
        """,
        [
            (
                row.asof_date,
                row.company_id,
                row.ticker,
                row.company_name,
                row.subsector,
                row.market_source_id,
                row.latest_price_date,
                row.latest_close,
                row.avg_dollar_volume_60d,
                row.return_21d,
                row.return_63d,
                row.return_126d,
                row.return_252d,
                row.momentum_12_1,
                row.relative_strength_63d,
                row.relative_strength_126d,
                row.sma_50,
                row.sma_200,
                row.price_vs_sma_50,
                row.price_vs_sma_200,
                row.sma_50_vs_200,
                row.sma_200_slope_63d,
                row.rsi_14,
                row.atr_14_pct,
                row.realized_vol_60d,
                row.max_drawdown_252d,
                row.pct_from_52w_high,
                row.technical_entry_score,
                row.trend_quality_score,
                row.relative_strength_score,
                row.liquidity_score,
                row.volatility_risk_score,
                row.entry_signal,
                row.data_quality_status,
                ";".join(row.missing_fields),
                json.dumps(
                    {
                        "source": "technical_entry_builder",
                        "entry_signal": row.entry_signal,
                        "data_quality_status": row.data_quality_status,
                        "missing_fields": row.missing_fields,
                        **row.payload,
                    },
                    ensure_ascii=True,
                    sort_keys=True,
                ),
                now,
                now,
            )
            for row in rows
        ],
    )
    return len(rows)


def row_to_dict(row: TechnicalRow) -> dict[str, Any]:
    out = {field: getattr(row, field) for field in FIELDNAMES if hasattr(row, field)}
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
            companies = load_companies(conn, ticker_filter=ticker_filter, max_tickers=int(args.max_tickers))
            if not companies:
                raise ValueError("No active companies selected")
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
            apply_scores(rows, entry_thresholds=thresholds)
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
