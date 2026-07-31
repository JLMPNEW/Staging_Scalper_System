#!/usr/bin/env python3
from __future__ import annotations

import argparse
import logging
import math
import statistics
import sys
from contextlib import closing
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from industrials.core.config import cfg_get, load_yaml, resolve_path  # noqa: E402
from industrials.core.db import connect, ensure_column, finish_run, init_db, start_run, utc_now  # noqa: E402
from industrials.core.logging_utils import configure_utc_logging  # noqa: E402
from industrials.core.reports import write_csv_atomic  # noqa: E402
from industrials.core.text_norm import normalize_ticker  # noqa: E402


LOGGER = logging.getLogger("build_industrials_market_features")
DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
RUN_TYPE = "build_industrials_market_features"
# Open membership spells are represented with this end_date sentinel so that
# MAX() aggregation prefers an open spell over any closed one (MK-8).
OPEN_MEMBERSHIP_END_SENTINEL = "9999-12-31"
# Generic secondary-benchmark feature columns (DR-3). Slot N holds the
# rel-strength vs the Nth entry (1-based, offset by the primary) of
# market_feature_build.secondary_benchmarks, in config order.
SECONDARY_BENCH_COLUMNS = ["rel_strength_bench2_3m", "rel_strength_bench3_3m", "rel_strength_bench4_3m"]
MAX_SECONDARY_BENCHMARKS = len(SECONDARY_BENCH_COLUMNS)
FIELDNAMES = [
    "ticker",
    "asof_date",
    "source_id",
    "model_family",
    "status",
    "trading_days_available",
    "latest_bar_date",
    "latest_adj_close",
    "ret_3m",
    "ret_12m_ex_1m",
    "rel_strength_bench_3m",
    "avg_dollar_volume_60d",
    "low_liquidity_flag",
    "realized_vol_60d",
    "max_drawdown_12m",
    "distance_from_52w_high",
    "review_reason",
]


@dataclass(frozen=True)
class PriceRow:
    bar_date: date
    close: float
    adj_close: float
    volume: float


@dataclass(frozen=True)
class UniverseMember:
    ticker: str
    start_date: date | None
    end_date: date | None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build market and technical features for an industrials model family.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--model-family", default="", help="Industrials model family to build, e.g. defense.")
    parser.add_argument("--benchmark-tickers", default="", help="Optional comma-separated benchmark ticker override.")
    parser.add_argument("--primary-benchmark", default="", help="Primary benchmark for rel_strength_bench_3m.")
    parser.add_argument("--asof", default="", help="Feature as-of date. Defaults to latest available across universe and benchmarks.")
    parser.add_argument("--source-id", default="", help="Price source override for universe tickers.")
    parser.add_argument("--benchmark-source-id", default="", help="Price source override for benchmarks.")
    parser.add_argument("--include-historical", action="store_true", help="Build features for active and historical/delisted members.")
    parser.add_argument(
        "--membership-status",
        choices=["active", "inactive", "all"],
        default="active",
        help="Universe subset to build. Defaults to active; with --asof, active means point-in-time active on that date.",
    )
    parser.add_argument("--output-csv", type=Path, default=None)
    return parser.parse_args()


def parse_date(raw: object) -> date | None:
    text = str(raw or "").strip()[:10]
    if not text:
        return None
    try:
        return datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError:
        return None


def safe_div(num: float | None, den: float | None) -> float | None:
    if num is None or den is None or den == 0:
        return None
    value = num / den
    return value if math.isfinite(value) else None


def pct_return(rows: list[PriceRow], end_idx: int, lookback: int) -> float | None:
    start_idx = end_idx - lookback
    if start_idx < 0:
        return None
    ratio = safe_div(rows[end_idx].adj_close, rows[start_idx].adj_close)
    return ratio - 1.0 if ratio is not None else None


def pct_return_between(rows: list[PriceRow], start_idx: int, end_idx: int) -> float | None:
    if start_idx < 0 or end_idx < 0 or start_idx >= len(rows) or end_idx >= len(rows) or end_idx <= start_idx:
        return None
    ratio = safe_div(rows[end_idx].adj_close, rows[start_idx].adj_close)
    return ratio - 1.0 if ratio is not None else None


def mean(values: list[float]) -> float | None:
    clean = [value for value in values if math.isfinite(value)]
    return sum(clean) / len(clean) if clean else None


def realized_vol(rows: list[PriceRow], end_idx: int, lookback: int) -> float | None:
    start_idx = max(1, end_idx - lookback + 1)
    if end_idx - start_idx + 1 < max(20, lookback // 2):
        return None
    returns: list[float] = []
    for idx in range(start_idx, end_idx + 1):
        prev = rows[idx - 1].adj_close
        cur = rows[idx].adj_close
        if prev > 0 and cur > 0:
            returns.append(math.log(cur / prev))
    if len(returns) < 20:
        return None
    return statistics.stdev(returns) * math.sqrt(252.0)


def max_drawdown(rows: list[PriceRow], end_idx: int, lookback: int) -> float | None:
    start_idx = max(0, end_idx - lookback + 1)
    window = rows[start_idx : end_idx + 1]
    if len(window) < 20:
        return None
    peak = window[0].adj_close
    worst = 0.0
    for row in window:
        peak = max(peak, row.adj_close)
        if peak > 0:
            worst = min(worst, row.adj_close / peak - 1.0)
    return worst


def moving_average(rows: list[PriceRow], end_idx: int, lookback: int) -> float | None:
    start_idx = end_idx - lookback + 1
    if start_idx < 0:
        return None
    return mean([row.adj_close for row in rows[start_idx : end_idx + 1]])


def window_average(rows: list[PriceRow], end_idx: int, lookback: int, *, dollar: bool) -> float | None:
    start_idx = end_idx - lookback + 1
    if start_idx < 0:
        return None
    if dollar:
        # Traded dollar volume is unadjusted close x volume (MK-21): adj_close
        # is dividend-adjusted while volume is split-only, so mixing them
        # understates ADV for dividend payers.
        return mean([row.close * row.volume for row in rows[start_idx : end_idx + 1]])
    return mean([row.volume for row in rows[start_idx : end_idx + 1]])


def distance_from_high(rows: list[PriceRow], end_idx: int, lookback: int) -> float | None:
    start_idx = max(0, end_idx - lookback + 1)
    window = rows[start_idx : end_idx + 1]
    if len(window) < 20:
        return None
    high = max(row.adj_close for row in window)
    return rows[end_idx].adj_close / high - 1.0 if high > 0 else None


def benchmark_return_asof(rows: list[PriceRow], asof_date: date, lookback: int) -> float | None:
    if not rows:
        return None
    idx = len(rows) - 1
    while idx >= 0 and rows[idx].bar_date > asof_date:
        idx -= 1
    if idx < 0:
        return None
    return pct_return(rows, idx, lookback)


def rel_strength(ticker_ret: float | None, bench_rows: list[PriceRow] | None, asof_date: date, lookback: int) -> float | None:
    if ticker_ret is None or not bench_rows:
        return None
    bench_ret = benchmark_return_asof(bench_rows, asof_date, lookback)
    return ticker_ret - bench_ret if bench_ret is not None else None


def load_price_rows(conn: Any, ticker: str, source_id: str, asof: date | None, start_date: date | None = None) -> list[PriceRow]:
    params: list[Any] = [ticker, source_id]
    asof_clause = ""
    if asof is not None:
        asof_clause = "AND bar_date <= ?"
        params.append(asof.isoformat())
    start_clause = ""
    if start_date is not None:
        start_clause = "AND bar_date >= ?"
        params.append(start_date.isoformat())
    db_rows = conn.execute(
        f"""
        SELECT bar_date, close, adj_close, volume
        FROM fact_price_ohlcv
        WHERE ticker = ? AND source_id = ? AND adj_close IS NOT NULL {asof_clause} {start_clause}
        ORDER BY bar_date
        """,
        tuple(params),
    ).fetchall()
    out: list[PriceRow] = []
    for row in db_rows:
        bar_date = parse_date(row["bar_date"])
        close = row["close"]
        adj_close = row["adj_close"]
        if bar_date is None or close is None or adj_close is None:
            continue
        out.append(PriceRow(bar_date=bar_date, close=float(close), adj_close=float(adj_close), volume=float(row["volume"] or 0.0)))
    return out


def load_benchmark_rows(
    conn: Any,
    source_ids: list[str],
    tickers: list[str],
    asof: date | None,
    *,
    min_bars: int,
) -> dict[str, list[PriceRow]]:
    out: dict[str, list[PriceRow]] = {}
    for ticker in tickers:
        normalized = normalize_ticker(ticker)
        if not normalized:
            continue
        rows, bench_source_id = load_best_available_price_rows(
            conn,
            ticker=normalized,
            source_ids=source_ids,
            asof=asof,
            start_date=None,
            min_bars=min_bars,
        )
        if rows:
            LOGGER.info("Benchmark %s loaded from source %s (%d bars)", normalized, bench_source_id, len(rows))
        out[normalized] = rows
    return out


def parse_ticker_list(raw: object) -> list[str]:
    values = raw if isinstance(raw, list) else str(raw or "").split(",")
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        ticker = normalize_ticker(value)
        if ticker and ticker not in seen:
            out.append(ticker)
            seen.add(ticker)
    return out


def resolve_benchmark_mapping(
    config: dict[str, Any],
    *,
    benchmark_override: object,
    primary_override: object,
) -> tuple[list[str], str, list[str]]:
    explicit = parse_ticker_list(benchmark_override)
    benchmarks = explicit or parse_ticker_list(
        cfg_get(config, "industrials_universe.benchmark_tickers", [])
    )
    primary = normalize_ticker(
        primary_override
        or cfg_get(config, "industrials_universe.benchmark_ticker", "XAR")
        or "XAR"
    )
    if primary and primary not in benchmarks:
        benchmarks.insert(0, primary)
    secondary = (
        [ticker for ticker in benchmarks if ticker != primary]
        if explicit
        else parse_ticker_list(
            cfg_get(config, "market_feature_build.secondary_benchmarks", [])
        )
    )
    if not secondary:
        secondary = [ticker for ticker in benchmarks if ticker != primary]
    return benchmarks, primary, secondary


def parse_source_list(raw: object) -> list[str]:
    values = raw if isinstance(raw, list) else str(raw or "").split(",")
    out: list[str] = []
    for value in values:
        source = str(value or "").strip()
        if source and source not in out:
            out.append(source)
    return out


def source_priority_list(primary_source: str, fallback_sources: list[str]) -> list[str]:
    out: list[str] = []
    for source in [primary_source, *fallback_sources]:
        text = str(source or "").strip()
        if text and text not in out:
            out.append(text)
    return out


def placeholders(values: list[str]) -> str:
    if not values:
        raise ValueError("values cannot be empty")
    return ",".join("?" for _ in values)


def load_universe(
    conn: Any,
    model_family: str,
    *,
    include_historical: bool,
    membership_status: str,
    asof: date | None,
) -> list[UniverseMember]:
    if include_historical or membership_status != "active" or asof is not None:
        status_sql = ""
        if membership_status == "active" and asof is None:
            status_sql = "AND m.is_current_member = 1"
        elif membership_status == "inactive":
            status_sql = "AND m.is_current_member = 0"
        asof_sql = ""
        params: list[Any] = [model_family]
        if asof is not None:
            if include_historical or membership_status in ("all", "inactive"):
                # Historical builds keep spells that ended before asof (MK-7);
                # post-delisting price rows are gated per member via end_date
                # instead (MK-8).
                asof_sql = "AND m.start_date <= ?"
                params.append(asof.isoformat())
            else:
                # With an explicit as-of date, the default "active" universe is
                # point-in-time active, not current active. Delisted calibration
                # members that were live on the snapshot date must be included
                # so source fallback can build historical features.
                asof_sql = f"AND m.start_date <= ? AND COALESCE(m.end_date, '{OPEN_MEMBERSHIP_END_SENTINEL}') >= ?"
                params.extend([asof.isoformat(), asof.isoformat()])
        rows = conn.execute(
            f"""
            SELECT m.ticker, MIN(m.start_date) AS start_date,
                   MAX(COALESCE(m.end_date, '{OPEN_MEMBERSHIP_END_SENTINEL}')) AS end_date
            FROM dim_universe_membership m
            JOIN dim_company c
              ON c.company_id = m.company_id
            WHERE m.model_family = ?
              {status_sql}
              {asof_sql}
            GROUP BY m.ticker
            ORDER BY m.ticker
            """,
            tuple(params),
        ).fetchall()
        members: list[UniverseMember] = []
        for row in rows:
            ticker = normalize_ticker(row["ticker"])
            if not ticker:
                continue
            # The sentinel means at least one spell is still open, i.e. no
            # delisting bound applies (MK-8: '' sorted below every real date,
            # so a closed spell used to beat an open one).
            end_raw = str(row["end_date"] or "")
            end_date = None if end_raw >= OPEN_MEMBERSHIP_END_SENTINEL else parse_date(end_raw)
            members.append(UniverseMember(ticker=ticker, start_date=parse_date(row["start_date"]), end_date=end_date))
        return members
    rows = conn.execute(
        """
        SELECT DISTINCT m.ticker
        FROM dim_universe_membership m
        WHERE m.model_family = ?
          AND m.membership_status = 'active'
          AND m.is_current_member = 1
        ORDER BY m.ticker
        """,
        (model_family,),
    ).fetchall()
    return [UniverseMember(ticker=normalize_ticker(row["ticker"]), start_date=None, end_date=None) for row in rows if normalize_ticker(row["ticker"])]


def load_best_available_price_rows(
    conn: Any,
    *,
    ticker: str,
    source_ids: list[str],
    asof: date | None,
    start_date: date | None,
    min_bars: int,
) -> tuple[list[PriceRow], str]:
    """Pick the source with the best usable coverage instead of the first non-empty one (MK-11).

    Preference order: sources meeting the min_bars coverage floor, then the
    source whose MAX(bar_date) is closest to asof (rows are already capped at
    asof), then bar count, then configured source priority.
    """
    best_rows: list[PriceRow] = []
    best_source = source_ids[0]
    best_key: tuple[int, date, int, int] | None = None
    for priority, source_id in enumerate(source_ids):
        rows = load_price_rows(conn, ticker, source_id, asof, start_date)
        if not rows:
            continue
        key = (int(len(rows) >= min_bars), rows[-1].bar_date, len(rows), -priority)
        if best_key is None or key > best_key:
            best_key = key
            best_rows = rows
            best_source = source_id
    return best_rows, best_source


def build_feature(
    ticker: str,
    rows: list[PriceRow],
    *,
    source_id: str,
    model_family: str,
    asof: date,
    membership_end: date | None,
    max_staleness_days: int,
    min_days: int,
    min_avg_dollar_volume_60d: float,
    windows: dict[str, int],
    bench_rows: dict[str, list[PriceRow]],
    primary_benchmark: str,
    secondary_benchmarks: list[str],
) -> tuple[dict[str, Any], str]:
    if not rows:
        return {
            "ticker": ticker,
            "asof_date": asof.isoformat(),
            "source_id": source_id,
            "model_family": model_family,
            "trading_days_available": 0,
            "low_liquidity_flag": 1,
            "market_data_quality": "missing",
        }, "no_price_bars"

    end_idx = len(rows) - 1
    latest = rows[end_idx]
    stale_days = (asof - latest.bar_date).days
    ret_1m = pct_return(rows, end_idx, windows["one_month_days"])
    ret_3m = pct_return(rows, end_idx, windows["three_month_days"])
    ret_6m = pct_return(rows, end_idx, windows["six_month_days"])
    ret_12m_ex_1m = pct_return_between(rows, end_idx - windows["one_year_days"], end_idx - windows["skip_latest_month_days"])
    ma_50d = moving_average(rows, end_idx, windows["ma_short_days"])
    ma_200d = moving_average(rows, end_idx, windows["ma_long_days"])
    avg_volume_20d = window_average(rows, end_idx, 20, dollar=False)
    avg_volume_60d = window_average(rows, end_idx, 60, dollar=False)
    avg_dollar_volume_20d = window_average(rows, end_idx, 20, dollar=True)
    avg_dollar_volume_60d = window_average(rows, end_idx, 60, dollar=True)
    low_liquidity_flag = int(min_avg_dollar_volume_60d > 0 and (avg_dollar_volume_60d is None or avg_dollar_volume_60d < min_avg_dollar_volume_60d))

    rel_bench_3m = (
        rel_strength(ret_3m, bench_rows.get(primary_benchmark), latest.bar_date, windows["three_month_days"])
        if primary_benchmark
        else None
    )
    secondary_rel: dict[str, float | None] = {}
    missing_benchmarks: list[str] = []
    if primary_benchmark and ret_3m is not None and rel_bench_3m is None:
        missing_benchmarks.append(primary_benchmark)
    for offset, column in enumerate(SECONDARY_BENCH_COLUMNS):
        bench = secondary_benchmarks[offset] if offset < len(secondary_benchmarks) else ""
        value = (
            rel_strength(ret_3m, bench_rows.get(bench), latest.bar_date, windows["three_month_days"])
            if bench
            else None
        )
        secondary_rel[column] = value
        if bench and ret_3m is not None and value is None:
            missing_benchmarks.append(bench)

    reasons: list[str] = []
    if stale_days > max_staleness_days:
        reasons.append(f"stale_{stale_days}d")
    # NOTE: rows are pre-filtered to bar_date <= asof in load_price_rows, so a
    # future-bar sentinel here would be unreachable (MK-13); the real
    # future-bar screen lives in script 06's market-stage validation.
    if membership_end is not None and membership_end < asof:
        reasons.append(f"membership_ended_{membership_end.isoformat()}")
    if len(rows) < min_days:
        reasons.append(f"low_history_{len(rows)}")
    if low_liquidity_flag:
        reasons.append("low_liquidity_60d_missing" if avg_dollar_volume_60d is None else f"low_liquidity_60d_{int(avg_dollar_volume_60d)}")
    if latest.adj_close <= 0:
        reasons.append("bad_latest_adj_close")
    for bench in missing_benchmarks:
        # Missing benchmark data must never leave a silent NULL rel-strength
        # on a row marked 'complete' (MK-9).
        reasons.append(f"missing_benchmark_{bench}")

    quality = "complete" if not reasons else "review"
    feature = {
        "ticker": ticker,
        "asof_date": asof.isoformat(),
        "source_id": source_id,
        "model_family": model_family,
        "latest_close": latest.close,
        "latest_adj_close": latest.adj_close,
        "latest_volume": latest.volume,
        "trading_days_available": len(rows),
        "latest_bar_date": latest.bar_date.isoformat(),
        "stale_days": stale_days,
        "stale_flag": int(stale_days > max_staleness_days),
        "low_history_flag": int(len(rows) < min_days),
        "low_liquidity_flag": low_liquidity_flag,
        "ret_1m": ret_1m,
        "ret_3m": ret_3m,
        "ret_6m": ret_6m,
        "ret_12m_ex_1m": ret_12m_ex_1m,
        "rel_strength_bench_3m": rel_bench_3m,
        **secondary_rel,
        "avg_volume_20d": avg_volume_20d,
        "avg_volume_60d": avg_volume_60d,
        "avg_dollar_volume_20d": avg_dollar_volume_20d,
        "avg_dollar_volume_60d": avg_dollar_volume_60d,
        "realized_vol_60d": realized_vol(rows, end_idx, windows["volatility_days"]),
        "max_drawdown_6m": max_drawdown(rows, end_idx, windows["six_month_days"]),
        "max_drawdown_12m": max_drawdown(rows, end_idx, windows["one_year_days"]),
        "distance_from_52w_high": distance_from_high(rows, end_idx, windows["one_year_days"]),
        "ma_50d": ma_50d,
        "ma_200d": ma_200d,
        "above_ma_50d": int(ma_50d is not None and latest.adj_close > ma_50d),
        "above_ma_200d": int(ma_200d is not None and latest.adj_close > ma_200d),
        "market_data_quality": quality,
    }
    return feature, ";".join(reasons)


def upsert_feature(conn: Any, feature: dict[str, Any]) -> None:
    now = utc_now()
    conn.execute(
        """
        INSERT INTO feature_market_technical(
            ticker, asof_date, source_id, model_family, latest_close, latest_adj_close,
            latest_volume, trading_days_available, latest_bar_date, stale_days, stale_flag,
            low_history_flag, low_liquidity_flag, ret_1m, ret_3m, ret_6m, ret_12m_ex_1m,
            rel_strength_bench_3m, rel_strength_bench2_3m, rel_strength_bench3_3m,
            rel_strength_bench4_3m, avg_volume_20d, avg_volume_60d,
            avg_dollar_volume_20d, avg_dollar_volume_60d, realized_vol_60d,
            max_drawdown_6m, max_drawdown_12m, distance_from_52w_high,
            ma_50d, ma_200d, above_ma_50d, above_ma_200d, market_data_quality,
            created_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(ticker, asof_date, source_id, model_family) DO UPDATE SET
            latest_close = excluded.latest_close,
            latest_adj_close = excluded.latest_adj_close,
            latest_volume = excluded.latest_volume,
            trading_days_available = excluded.trading_days_available,
            latest_bar_date = excluded.latest_bar_date,
            stale_days = excluded.stale_days,
            stale_flag = excluded.stale_flag,
            low_history_flag = excluded.low_history_flag,
            low_liquidity_flag = excluded.low_liquidity_flag,
            ret_1m = excluded.ret_1m,
            ret_3m = excluded.ret_3m,
            ret_6m = excluded.ret_6m,
            ret_12m_ex_1m = excluded.ret_12m_ex_1m,
            rel_strength_bench_3m = excluded.rel_strength_bench_3m,
            rel_strength_bench2_3m = excluded.rel_strength_bench2_3m,
            rel_strength_bench3_3m = excluded.rel_strength_bench3_3m,
            rel_strength_bench4_3m = excluded.rel_strength_bench4_3m,
            avg_volume_20d = excluded.avg_volume_20d,
            avg_volume_60d = excluded.avg_volume_60d,
            avg_dollar_volume_20d = excluded.avg_dollar_volume_20d,
            avg_dollar_volume_60d = excluded.avg_dollar_volume_60d,
            realized_vol_60d = excluded.realized_vol_60d,
            max_drawdown_6m = excluded.max_drawdown_6m,
            max_drawdown_12m = excluded.max_drawdown_12m,
            distance_from_52w_high = excluded.distance_from_52w_high,
            ma_50d = excluded.ma_50d,
            ma_200d = excluded.ma_200d,
            above_ma_50d = excluded.above_ma_50d,
            above_ma_200d = excluded.above_ma_200d,
            market_data_quality = excluded.market_data_quality,
            updated_at = excluded.updated_at
        """,
        (
            feature.get("ticker"),
            feature.get("asof_date"),
            feature.get("source_id"),
            feature.get("model_family"),
            feature.get("latest_close"),
            feature.get("latest_adj_close"),
            feature.get("latest_volume"),
            feature.get("trading_days_available"),
            feature.get("latest_bar_date"),
            feature.get("stale_days"),
            feature.get("stale_flag", 0),
            feature.get("low_history_flag", 0),
            feature.get("low_liquidity_flag", 0),
            feature.get("ret_1m"),
            feature.get("ret_3m"),
            feature.get("ret_6m"),
            feature.get("ret_12m_ex_1m"),
            feature.get("rel_strength_bench_3m"),
            feature.get("rel_strength_bench2_3m"),
            feature.get("rel_strength_bench3_3m"),
            feature.get("rel_strength_bench4_3m"),
            feature.get("avg_volume_20d"),
            feature.get("avg_volume_60d"),
            feature.get("avg_dollar_volume_20d"),
            feature.get("avg_dollar_volume_60d"),
            feature.get("realized_vol_60d"),
            feature.get("max_drawdown_6m"),
            feature.get("max_drawdown_12m"),
            feature.get("distance_from_52w_high"),
            feature.get("ma_50d"),
            feature.get("ma_200d"),
            feature.get("above_ma_50d"),
            feature.get("above_ma_200d"),
            feature.get("market_data_quality"),
            now,
            now,
        ),
    )


def add_issue(conn: Any, *, ticker: str, source_id: str, detail: str, model_family: str) -> None:
    # SC-12: issues are family-scoped; stamp model_family so per-stage clears for
    # one family never wipe another family's open issues.
    now = utc_now()
    row = conn.execute("SELECT company_id FROM dim_company WHERE ticker = ?", (ticker,)).fetchone()
    company_id = int(row["company_id"]) if row is not None else None
    conn.execute(
        """
        INSERT INTO data_quality_issues(
            detected_at, severity, stage, model_family, ticker, company_id, source_id, issue_type,
            issue_detail, resolution_status, created_at, updated_at
        )
        VALUES (?, 'warning', ?, ?, ?, ?, ?, 'market_feature_review', ?, 'open', ?, ?)
        """,
        (now, RUN_TYPE, model_family, ticker, company_id, source_id, detail, now, now),
    )


def write_report(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    write_csv_atomic(path, FIELDNAMES, rows)


def main() -> None:
    configure_utc_logging()
    args = parse_args()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    base_dir = config_path.parent
    db_path = args.db.expanduser().resolve() if args.db else resolve_path(cfg_get(config, "paths.database_path"), base_dir=base_dir)
    output_csv = args.output_csv.expanduser().resolve() if args.output_csv else resolve_path(cfg_get(config, "market_feature_build.output_csv"), base_dir=base_dir)
    source_id = str(args.source_id or cfg_get(config, "market_feature_build.source_id", "yahoo_finance_adjusted") or "yahoo_finance_adjusted")
    fallback_source_ids = parse_source_list(cfg_get(config, "market_data_policy.scoring_fallback_sources", []))
    source_ids = source_priority_list(source_id, fallback_source_ids)
    benchmark_source_id = str(args.benchmark_source_id or source_id)
    model_family = str(args.model_family or cfg_get(config, "industrials_universe.initial_subsector", "defense") or "defense").strip()
    if not model_family:
        raise ValueError("model_family cannot be empty")
    max_staleness_days = int(cfg_get(config, "market_data_policy.max_staleness_days", 7))
    # market_data_policy is the source of truth for full-feature thresholds
    # (CF-5); the market_feature_build copies are legacy fallbacks only.
    min_days = int(
        cfg_get(
            config,
            "market_data_policy.min_trading_days_for_full_features",
            cfg_get(config, "market_feature_build.min_trading_days_for_full_features", 252),
        )
    )
    min_avg_dollar_volume_60d = float(
        cfg_get(
            config,
            "market_data_policy.min_avg_dollar_volume_60d_for_full_features",
            cfg_get(config, "market_feature_build.min_avg_dollar_volume_60d_for_full_features", 0),
        )
        or 0
    )
    min_source_bars = int(cfg_get(config, "market_data_policy.min_source_bars_for_selection", 20))
    benchmark_tickers, primary_benchmark, secondary_benchmarks = (
        resolve_benchmark_mapping(
            config,
            benchmark_override=args.benchmark_tickers,
            primary_override=args.primary_benchmark,
        )
    )
    if len(secondary_benchmarks) > MAX_SECONDARY_BENCHMARKS:
        raise ValueError(
            f"market_feature_build.secondary_benchmarks supports at most {MAX_SECONDARY_BENCHMARKS} entries; got {secondary_benchmarks}."
        )
    for ticker in secondary_benchmarks:
        if ticker not in benchmark_tickers:
            benchmark_tickers.append(ticker)
    LOGGER.info(
        "Benchmark column mapping: rel_strength_bench_3m=%s, %s",
        primary_benchmark or "(none)",
        ", ".join(
            f"{column}={secondary_benchmarks[idx] if idx < len(secondary_benchmarks) else '(unused)'}"
            for idx, column in enumerate(SECONDARY_BENCH_COLUMNS)
        ),
    )
    membership_status = str(args.membership_status)
    if args.include_historical:
        if membership_status == "inactive":
            raise ValueError("--include-historical conflicts with --membership-status inactive; use --membership-status all instead.")
        if membership_status != "all":
            LOGGER.info("--include-historical implies --membership-status all.")
        membership_status = "all"
    windows = {
        "one_month_days": int(cfg_get(config, "market_feature_build.windows.one_month_days", 21)),
        "three_month_days": int(cfg_get(config, "market_feature_build.windows.three_month_days", 63)),
        "six_month_days": int(cfg_get(config, "market_feature_build.windows.six_month_days", 126)),
        "one_year_days": int(cfg_get(config, "market_feature_build.windows.one_year_days", 252)),
        "skip_latest_month_days": int(cfg_get(config, "market_feature_build.windows.skip_latest_month_days", 21)),
        "volatility_days": int(cfg_get(config, "market_feature_build.windows.volatility_days", 60)),
        "ma_short_days": int(cfg_get(config, "market_feature_build.windows.ma_short_days", 50)),
        "ma_long_days": int(cfg_get(config, "market_feature_build.windows.ma_long_days", 200)),
    }
    asof = parse_date(args.asof)

    with closing(connect(db_path, timeout_sec=float(cfg_get(config, "runtime.sqlite_timeout_sec", 30.0)))) as conn:
        init_db(conn)
        with conn:
            for column in SECONDARY_BENCH_COLUMNS:
                ensure_column(conn, "feature_market_technical", column, "REAL")
        run_id = start_run(conn, run_type=RUN_TYPE, input_path=config_path)
        try:
            members = load_universe(
                conn,
                model_family,
                include_historical=bool(args.include_historical),
                membership_status=membership_status,
                asof=asof,
            )
            tickers = [member.ticker for member in members]
            if not tickers:
                raise ValueError(f"No industrials universe tickers found for model_family={model_family}.")
            effective_asof = asof
            if effective_asof is None:
                ph = placeholders(tickers)
                row = conn.execute(
                    f"""
                    SELECT MAX(bar_date) AS max_date
                    FROM fact_price_ohlcv
                    WHERE source_id IN ({placeholders(source_ids)})
                      AND ticker IN ({ph})
                    """,
                    (*source_ids, *tickers),
                ).fetchone()
                effective_asof = parse_date(row["max_date"] if row is not None else "")
            if effective_asof is None:
                raise ValueError(f"No price bars found for source_id={source_id}")

            if asof is None and effective_asof is not None:
                members = load_universe(
                    conn,
                    model_family,
                    include_historical=bool(args.include_historical),
                    membership_status=membership_status,
                    asof=effective_asof,
                )
                tickers = [member.ticker for member in members]
                if not tickers:
                    raise ValueError(f"No as-of eligible industrials universe tickers found for model_family={model_family} asof={effective_asof}.")
            benchmark_source_ids = source_priority_list(benchmark_source_id, fallback_source_ids)
            bench_rows = load_benchmark_rows(
                conn,
                benchmark_source_ids,
                benchmark_tickers,
                effective_asof,
                min_bars=min_source_bars,
            )
            if primary_benchmark and not bench_rows.get(primary_benchmark):
                raise ValueError(
                    f"No price bars found for primary benchmark {primary_benchmark} through asof={effective_asof.isoformat()} "
                    f"in sources {benchmark_source_ids}; rel_strength_bench_3m would be silently NULL panel-wide."
                )
            missing_benchmarks = sorted(ticker for ticker, rows in bench_rows.items() if not rows)
            if missing_benchmarks:
                LOGGER.error(
                    "Missing benchmark price data through asof=%s for: %s; affected rel-strength columns will be NULL and flagged for review.",
                    effective_asof,
                    ",".join(missing_benchmarks),
                )
            report_rows: list[dict[str, Any]] = []
            review_count = 0
            with conn:
                ph_sources = placeholders(source_ids)
                issue_tickers = sorted(
                    {
                        normalize_ticker(row["ticker"])
                        for row in conn.execute(
                            "SELECT ticker FROM dim_industrials_taxonomy WHERE model_family = ?",
                            (model_family,),
                        ).fetchall()
                        if normalize_ticker(row["ticker"])
                    }.union(tickers)
                )
                ph_issue_tickers = placeholders(issue_tickers)
                # A single unconditional DELETE: the previous ticker IN /
                # ticker NOT IN pair covered every ticker between them (SC-8).
                conn.execute(
                    f"""
                    DELETE FROM feature_market_technical
                    WHERE asof_date = ?
                      AND model_family = ?
                      AND source_id IN ({ph_sources})
                    """,
                    (effective_asof.isoformat(), model_family, *source_ids),
                )
                # SC-12: family-scoped clear so this run never wipes another
                # family's open issues for the same ticker/stage.
                conn.execute(
                    f"DELETE FROM data_quality_issues WHERE stage = ? AND model_family = ? AND ticker IN ({ph_issue_tickers})",
                    (RUN_TYPE, model_family, *issue_tickers),
                )
                for member in members:
                    ticker = member.ticker
                    # Delisting gate (MK-8): a closed membership spell caps
                    # price loads at the delisting date so post-delist bars
                    # (e.g. a recycled ticker symbol) never leak into features.
                    member_price_asof = effective_asof
                    if member.end_date is not None and member.end_date < effective_asof:
                        member_price_asof = member.end_date
                    rows, feature_source_id = load_best_available_price_rows(
                        conn,
                        ticker=ticker,
                        source_ids=source_ids,
                        asof=member_price_asof,
                        start_date=member.start_date,
                        min_bars=min_source_bars,
                    )
                    feature, review_reason = build_feature(
                        ticker,
                        rows,
                        source_id=feature_source_id,
                        model_family=model_family,
                        asof=effective_asof,
                        membership_end=member.end_date,
                        max_staleness_days=max_staleness_days,
                        min_days=min_days,
                        min_avg_dollar_volume_60d=min_avg_dollar_volume_60d,
                        windows=windows,
                        bench_rows=bench_rows,
                        primary_benchmark=primary_benchmark,
                        secondary_benchmarks=secondary_benchmarks,
                    )
                    upsert_feature(conn, feature)
                    if review_reason:
                        review_count += 1
                        add_issue(conn, ticker=ticker, source_id=feature_source_id, detail=review_reason, model_family=model_family)
                    report_rows.append(
                        {
                            "ticker": ticker,
                            "asof_date": effective_asof.isoformat(),
                            "source_id": feature_source_id,
                            "model_family": model_family,
                            "status": "review" if review_reason else "success",
                            "trading_days_available": feature.get("trading_days_available", 0),
                            "latest_bar_date": feature.get("latest_bar_date", ""),
                            "latest_adj_close": feature.get("latest_adj_close", ""),
                            "ret_3m": feature.get("ret_3m", ""),
                            "ret_12m_ex_1m": feature.get("ret_12m_ex_1m", ""),
                            "rel_strength_bench_3m": feature.get("rel_strength_bench_3m", ""),
                            "avg_dollar_volume_60d": feature.get("avg_dollar_volume_60d", ""),
                            "low_liquidity_flag": feature.get("low_liquidity_flag", 0),
                            "realized_vol_60d": feature.get("realized_vol_60d", ""),
                            "max_drawdown_12m": feature.get("max_drawdown_12m", ""),
                            "distance_from_52w_high": feature.get("distance_from_52w_high", ""),
                            "review_reason": review_reason,
                        }
                    )
            write_report(output_csv, report_rows)
            finish_run(conn, run_id=run_id, status="success", row_count=len(report_rows), message=f"asof={effective_asof.isoformat()} rows={len(report_rows)} review={review_count} output={output_csv}")
            LOGGER.info("Wrote market feature coverage report: %s", output_csv)
            LOGGER.info("Built market features: asof=%s rows=%d review=%d", effective_asof, len(report_rows), review_count)
        except BaseException as exc:
            finish_run(conn, run_id=run_id, status="failed", row_count=0, message=f"{type(exc).__name__}: {exc}")
            raise


if __name__ == "__main__":
    main()
