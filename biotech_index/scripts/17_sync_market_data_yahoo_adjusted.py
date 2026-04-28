#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import logging
import math
import sqlite3
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime, time as dt_time, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from biotech_index.core.config import cfg_get, load_yaml, resolve_path
from biotech_index.core.db import connect, finish_run, init_db, start_run, utc_now


LOGGER = logging.getLogger("sync_market_data_yahoo_adjusted")
DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
DEFAULT_SOURCE = "yahoo_adjusted"
DEFAULT_BENCHMARK = "XBI"
CSV_FIELDNAMES = [
    "asof_date",
    "source",
    "ticker",
    "company_name",
    "close_price",
    "market_cap",
    "shares_outstanding",
    "avg_dollar_volume_20d",
    "return_3m_pct",
    "relative_strength_3m_vs_xbi",
    "price_vs_200d_pct",
    "distance_from_52w_high_pct",
    "price_adjustment",
    "is_adjusted",
    "is_provisional",
    "first_bar_date",
    "last_bar_date",
    "bar_count",
    "expected_bar_count",
    "missing_bar_count",
    "continuity_status",
    "market_data_quality",
]


@dataclass(frozen=True)
class Company:
    company_id: int
    ticker: str
    company_name: str
    currency: str


@dataclass(frozen=True)
class AsofDecision:
    requested_asof: date
    effective_asof: date
    guard_applied: bool
    provisional_asof: bool
    reason: str


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Sync Yahoo adjusted daily prices into the biotech index database.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--asof", type=str, default="", help="Snapshot date in YYYY-MM-DD. Defaults to the configured market timezone today.")
    parser.add_argument("--tickers", type=str, default="", help="Optional comma-separated ticker subset.")
    parser.add_argument("--max-tickers", type=int, default=0, help="Smoke-test limit. 0 means all.")
    parser.add_argument("--start-date", type=str, default="", help="Reload Yahoo adjusted history on or after this YYYY-MM-DD date.")
    parser.add_argument("--refetch-days", type=int, default=-1, help="Rolling refetch window. 0 means reload from configured start date.")
    return parser.parse_args()


def configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%SZ",
    )
    for handler in logging.getLogger().handlers:
        if handler.formatter is not None:
            handler.formatter.converter = time.gmtime
    logging.getLogger("yfinance").setLevel(logging.WARNING)


def parse_date(raw: object) -> date | None:
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        return datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError:
        return None


def to_float(raw: object) -> float | None:
    if raw is None:
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def as_bool(raw: object) -> bool:
    return str(raw or "").strip().lower() in {"1", "true", "yes", "y"}


def parse_clock_time(raw: object, default: str = "16:15") -> dt_time:
    text = str(raw or default).strip()
    for fmt in ("%H:%M:%S", "%H:%M"):
        try:
            return datetime.strptime(text, fmt).time()
        except ValueError:
            continue
    raise ValueError(f"Invalid market close time: {raw}")


def previous_business_day(day: date) -> date:
    out = day - timedelta(days=1)
    while out.weekday() >= 5:
        out -= timedelta(days=1)
    return out


def resolve_effective_asof(
    requested_asof: date,
    *,
    now: datetime | None = None,
    guard_enabled: bool,
    market_timezone: str,
    market_close_time: dt_time,
) -> AsofDecision:
    tz = ZoneInfo(market_timezone)
    now_local = (now or datetime.now(timezone.utc)).astimezone(tz)
    local_today = now_local.date()
    before_close = now_local.time() < market_close_time
    effective_asof = requested_asof
    guard_applied = False
    provisional = False
    reason = ""
    if guard_enabled and requested_asof > local_today:
        effective_asof = previous_business_day(local_today) if local_today.weekday() >= 5 or before_close else local_today
        guard_applied = True
        reason = "future_asof_clamped"
    elif guard_enabled and requested_asof >= local_today and local_today.weekday() >= 5:
        effective_asof = previous_business_day(local_today)
        guard_applied = True
        reason = "market_closed_weekend"
    elif guard_enabled and requested_asof >= local_today and before_close:
        effective_asof = previous_business_day(local_today)
        guard_applied = True
        reason = "before_market_close"
    else:
        provisional = requested_asof >= local_today and before_close
        reason = "current_session_provisional" if provisional else ""
    if effective_asof.weekday() >= 5:
        effective_asof = previous_business_day(effective_asof)
        guard_applied = True
        provisional = False
        reason = reason or "weekend_asof"
    return AsofDecision(
        requested_asof=requested_asof,
        effective_asof=effective_asof,
        guard_applied=guard_applied,
        provisional_asof=provisional,
        reason=reason,
    )


def normalize_ticker(raw: object) -> str:
    return str(raw or "").strip().upper().replace(".", "-")


def read_scoring_tickers(path: Path) -> set[str]:
    if not path.exists():
        raise FileNotFoundError(f"Final scoring universe CSV not found: {path}")
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        out: set[str] = set()
        for row in reader:
            ticker = normalize_ticker(row.get("ticker"))
            if ticker and str(row.get("final_status") or "").strip().lower() == "keep" and as_bool(row.get("scoring_include")):
                out.add(ticker)
    if not out:
        raise ValueError(f"Final scoring universe CSV contains no scoring tickers: {path}")
    return out


def load_companies(conn: sqlite3.Connection, *, scoring_tickers: set[str], ticker_filter: set[str], max_tickers: int) -> list[Company]:
    rows = conn.execute(
        """
        SELECT company_id, ticker, company_name, currency
        FROM companies
        WHERE is_active = 1
        ORDER BY ticker
        """
    ).fetchall()
    out: list[Company] = []
    for row in rows:
        ticker = normalize_ticker(row["ticker"])
        if scoring_tickers and ticker not in scoring_tickers:
            continue
        if ticker_filter and ticker not in ticker_filter:
            continue
        out.append(Company(int(row["company_id"]), ticker, str(row["company_name"] or ""), str(row["currency"] or "USD") or "USD"))
        if max_tickers > 0 and len(out) >= max_tickers:
            break
    return out


def load_latest_shares(conn: sqlite3.Connection, company_id: int, asof_date: date) -> float | None:
    row = conn.execute(
        """
        SELECT shares_outstanding
        FROM company_facts_quarterly
        WHERE company_id = ?
          AND period_end <= ?
          AND (filed_date IS NULL OR filed_date = '' OR filed_date <= ?)
          AND shares_outstanding IS NOT NULL
        ORDER BY period_end DESC, filed_date DESC
        LIMIT 1
        """,
        (company_id, asof_date.isoformat(), asof_date.isoformat()),
    ).fetchone()
    return to_float(row["shares_outstanding"]) if row else None


def load_latest_bar_dates(conn: sqlite3.Connection, *, tickers: list[str], source: str) -> dict[str, date]:
    if not tickers:
        return {}
    placeholders = ",".join("?" for _ in tickers)
    rows = conn.execute(
        f"""
        SELECT ticker, MAX(bar_date) AS latest_bar_date
        FROM market_bars_daily
        WHERE source = ? AND ticker IN ({placeholders})
        GROUP BY ticker
        """,
        (source, *tickers),
    ).fetchall()
    out: dict[str, date] = {}
    for row in rows:
        latest = parse_date(row["latest_bar_date"])
        if latest is not None:
            out[normalize_ticker(row["ticker"])] = latest
    return out


def load_existing_bars(
    conn: sqlite3.Connection,
    *,
    ticker: str,
    source: str,
    start_date: date,
    asof_date: date,
) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT *
        FROM market_bars_daily
        WHERE ticker = ? AND source = ? AND bar_date >= ? AND bar_date <= ?
        ORDER BY bar_date
        """,
        (ticker, source, start_date.isoformat(), asof_date.isoformat()),
    ).fetchall()
    return [dict(row) for row in rows]


def compute_fetch_start(default_start: date, asof_date: date, latest_bar_date: date | None, refetch_days: int) -> date:
    if refetch_days <= 0 or latest_bar_date is None:
        return default_start
    rolling_start = asof_date - timedelta(days=refetch_days)
    overlap_start = latest_bar_date - timedelta(days=min(7, refetch_days))
    return max(default_start, min(rolling_start, overlap_start))


def yahoo_column(row: Any, name: str) -> Any:
    try:
        return row.get(name)
    except AttributeError:
        return None


def build_yahoo_bar(
    ticker: str,
    bar_day: date,
    row: Any,
    *,
    provisional_asof: bool,
    asof_date: date,
) -> dict[str, Any] | None:
    raw_open = to_float(yahoo_column(row, "Open"))
    raw_high = to_float(yahoo_column(row, "High"))
    raw_low = to_float(yahoo_column(row, "Low"))
    raw_close = to_float(yahoo_column(row, "Close"))
    adj_close = to_float(yahoo_column(row, "Adj Close"))
    volume = to_float(yahoo_column(row, "Volume"))
    dividend_amount = to_float(yahoo_column(row, "Dividends")) or 0.0
    split_factor = to_float(yahoo_column(row, "Stock Splits")) or 0.0
    if raw_close is None or raw_close <= 0:
        return None
    if adj_close is None or adj_close <= 0:
        adj_close = raw_close
    factor = adj_close / raw_close if raw_close else None
    if factor is None or factor <= 0 or not math.isfinite(factor):
        return None

    def adjusted(value: float | None) -> float | None:
        return value * factor if value is not None else None

    return {
        "ticker": ticker,
        "bar_date": bar_day.isoformat(),
        "source": DEFAULT_SOURCE,
        "open": adjusted(raw_open),
        "high": adjusted(raw_high),
        "low": adjusted(raw_low),
        "close": adj_close,
        "volume": volume,
        "wap": None,
        "price_adjustment": "adjusted",
        "raw_open": raw_open,
        "raw_high": raw_high,
        "raw_low": raw_low,
        "raw_close": raw_close,
        "adj_close": adj_close,
        "adjustment_factor": factor,
        "dividend_amount": dividend_amount,
        "split_factor": split_factor,
        "corporate_action_source": "yahoo",
        "is_adjusted": 1,
        "is_provisional": 1 if provisional_asof and bar_day == asof_date else 0,
        "data_quality": "high",
    }


def fetch_yahoo_bars(
    ticker: str,
    *,
    start_date: date,
    asof_date: date,
    source: str,
    provisional_asof: bool,
) -> list[dict[str, Any]]:
    try:
        import yfinance as yf  # type: ignore
    except Exception as exc:
        raise RuntimeError("yfinance is required for Yahoo adjusted market data sync. Install package 'yfinance'.") from exc

    end_date = asof_date + timedelta(days=1)
    frame = yf.Ticker(ticker).history(
        start=start_date.isoformat(),
        end=end_date.isoformat(),
        interval="1d",
        auto_adjust=False,
        actions=True,
    )
    if frame is None or frame.empty:
        return []
    out: list[dict[str, Any]] = []
    for idx, row in frame.iterrows():
        try:
            bar_day = idx.date()
        except AttributeError:
            bar_day = parse_date(str(idx)[:10])
        if bar_day is None or bar_day < start_date or bar_day > asof_date:
            continue
        bar = build_yahoo_bar(ticker, bar_day, row, provisional_asof=provisional_asof, asof_date=asof_date)
        if bar is None:
            continue
        bar["source"] = source
        out.append(bar)
    return out


def parse_bar_day(row: dict[str, Any]) -> date | None:
    return parse_date(str(row.get("bar_date") or "")[:10])


def sorted_bar_rows(bars: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted((row for row in bars if parse_bar_day(row) is not None), key=lambda row: str(row.get("bar_date") or ""))


def merge_bars(existing: list[dict[str, Any]], fetched: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_date = {str(row.get("bar_date") or ""): row for row in existing if row.get("bar_date")}
    for row in fetched:
        if row.get("bar_date"):
            by_date[str(row["bar_date"])] = row
    return sorted_bar_rows(list(by_date.values()))


def reference_dates_from_bars(bars: list[dict[str, Any]], asof_date: date) -> list[date]:
    return sorted({day for row in bars if (day := parse_bar_day(row)) is not None and day <= asof_date})


def weekday_dates(start: date, end: date) -> list[date]:
    out: list[date] = []
    current = start
    while current <= end:
        if current.weekday() < 5:
            out.append(current)
        current += timedelta(days=1)
    return out


def continuity_report(
    bars: list[dict[str, Any]],
    *,
    reference_dates: list[date],
    asof_date: date,
    max_missing_days: int,
) -> dict[str, Any]:
    observed_dates = sorted({day for row in bars if (day := parse_bar_day(row)) is not None and day <= asof_date})
    if not observed_dates:
        return {
            "first_bar_date": "",
            "last_bar_date": "",
            "bar_count": 0,
            "expected_bar_count": 0,
            "missing_bar_count": 0,
            "missing_bar_dates_sample": [],
            "continuity_status": "no_bars",
        }
    first_date = observed_dates[0]
    expected_pool = [day for day in reference_dates if first_date <= day <= asof_date]
    if not expected_pool:
        expected_pool = weekday_dates(first_date, asof_date)
    observed_set = set(observed_dates)
    missing_dates = [day for day in expected_pool if day not in observed_set]
    latest_expected = expected_pool[-1] if expected_pool else None
    latest_observed = observed_dates[-1]
    if not missing_dates:
        status = "complete"
    elif latest_expected is not None and latest_observed < latest_expected:
        status = "stale"
    elif len(missing_dates) <= max_missing_days:
        status = "minor_gaps"
    else:
        status = "gaps"
    return {
        "first_bar_date": first_date.isoformat(),
        "last_bar_date": latest_observed.isoformat(),
        "bar_count": len(observed_dates),
        "expected_bar_count": len(expected_pool),
        "missing_bar_count": len(missing_dates),
        "missing_bar_dates_sample": [day.isoformat() for day in missing_dates[:10]],
        "continuity_status": status,
    }


def pct_return(values: list[float], days: int) -> float | None:
    if len(values) <= days or values[-days - 1] == 0:
        return None
    return (values[-1] / values[-days - 1]) - 1.0


def score_liquidity(avg_dollar_volume_20d: float | None) -> float:
    if avg_dollar_volume_20d is None:
        return 0.0
    if avg_dollar_volume_20d >= 20_000_000:
        return 100.0
    if avg_dollar_volume_20d >= 10_000_000:
        return 85.0
    if avg_dollar_volume_20d >= 2_000_000:
        return 65.0
    if avg_dollar_volume_20d >= 1_000_000:
        return 45.0
    return 15.0


def min_quality(*values: str) -> str:
    ranks = {"low": 0, "medium": 1, "high": 2}
    lowest = min((ranks.get(str(value or "").lower(), 0) for value in values), default=0)
    return {rank: quality for quality, rank in ranks.items()}[lowest]


def continuity_quality(status: str) -> str:
    if status == "complete":
        return "high"
    if status == "minor_gaps":
        return "medium"
    return "low"


def common_value(rows: list[dict[str, Any]], key: str, default: Any = None) -> Any:
    values = {row.get(key) for row in rows if row.get(key) not in {None, ""}}
    if len(values) == 1:
        return next(iter(values))
    if len(values) > 1:
        return "mixed"
    return default


def build_market_rows(
    company: Company,
    bars: list[dict[str, Any]],
    xbi_closes: list[float],
    shares: float | None,
    asof_date: date,
    *,
    source: str,
    reference_dates: list[date],
    continuity_max_missing_days: int,
) -> tuple[dict[str, Any], dict[str, Any]]:
    ordered_bars = sorted_bar_rows(bars)
    closes = [value for value in (to_float(row.get("close")) for row in ordered_bars) if value is not None and value > 0]
    volumes = [to_float(row.get("volume")) or 0.0 for row in ordered_bars if to_float(row.get("close")) is not None]
    if not closes:
        raise ValueError(f"No usable adjusted close prices for {company.ticker}")
    continuity = continuity_report(
        ordered_bars,
        reference_dates=reference_dates,
        asof_date=asof_date,
        max_missing_days=continuity_max_missing_days,
    )
    close = closes[-1]
    dollar_volumes = [closes[idx] * volumes[idx] for idx in range(min(len(closes), len(volumes)))]
    avg_volume_20d = sum(volumes[-20:]) / min(20, len(volumes)) if volumes else None
    avg_dollar_volume_20d = sum(dollar_volumes[-20:]) / min(20, len(dollar_volumes)) if dollar_volumes else None
    high_52w = max((to_float(row.get("high")) or 0.0 for row in ordered_bars[-260:]), default=None)
    low_52w = min((to_float(row.get("low")) or close for row in ordered_bars[-260:]), default=None)
    market_cap = close * shares if shares and shares > 0 else None
    sma_200 = sum(closes[-200:]) / min(200, len(closes)) if closes else None
    return_1m = pct_return(closes, 21)
    return_3m = pct_return(closes, 63)
    xbi_return_3m = pct_return(xbi_closes, 63) if xbi_closes else None
    relative_strength = return_3m - xbi_return_3m if return_3m is not None and xbi_return_3m is not None else None
    price_vs_200d = (close / sma_200 - 1.0) if sma_200 else None
    distance_52w = (close / high_52w - 1.0) if high_52w else None
    length_quality = "high" if len(closes) >= 200 else "medium" if len(closes) >= 63 else "low"
    quality = min_quality(length_quality, continuity_quality(str(continuity["continuity_status"])))
    is_provisional = 1 if any(int(row.get("is_provisional") or 0) for row in ordered_bars) else 0
    payload = {
        "bar_count": len(closes),
        "market_cap_source": "yahoo_adjusted_close_x_sec_shares" if market_cap else "missing_sec_shares",
        "price_adjustment": "adjusted",
        "is_adjusted": True,
        "is_provisional": bool(is_provisional),
        "adjustment_factor_latest": common_value(ordered_bars[-1:], "adjustment_factor"),
        "continuity": continuity,
    }
    snapshot = {
        "asof_date": asof_date.isoformat(),
        "company_id": company.company_id,
        "ticker": company.ticker,
        "source": source,
        "last_price": close,
        "close_price": close,
        "market_cap": market_cap,
        "shares_outstanding": shares,
        "avg_volume_20d": avg_volume_20d,
        "avg_dollar_volume_20d": avg_dollar_volume_20d,
        "fifty_two_week_high": high_52w,
        "fifty_two_week_low": low_52w,
        "currency": company.currency,
        "price_adjustment": "adjusted",
        "is_adjusted": 1,
        "is_provisional": is_provisional,
        "first_bar_date": continuity["first_bar_date"],
        "last_bar_date": continuity["last_bar_date"],
        "bar_count": continuity["bar_count"],
        "expected_bar_count": continuity["expected_bar_count"],
        "missing_bar_count": continuity["missing_bar_count"],
        "continuity_status": continuity["continuity_status"],
        "data_quality": quality,
        "payload_json": json.dumps(payload, sort_keys=True),
    }
    features = {
        "asof_date": asof_date.isoformat(),
        "company_id": company.company_id,
        "ticker": company.ticker,
        "source": source,
        "close_price": close,
        "market_cap": market_cap,
        "shares_outstanding": shares,
        "price_vs_200d_pct": price_vs_200d,
        "return_1m_pct": return_1m,
        "return_3m_pct": return_3m,
        "xbi_return_3m_pct": xbi_return_3m,
        "relative_strength_3m_vs_xbi": relative_strength,
        "distance_from_52w_high_pct": distance_52w,
        "avg_dollar_volume_20d": avg_dollar_volume_20d,
        "liquidity_score": score_liquidity(avg_dollar_volume_20d),
        "price_adjustment": "adjusted",
        "is_adjusted": 1,
        "is_provisional": is_provisional,
        "first_bar_date": continuity["first_bar_date"],
        "last_bar_date": continuity["last_bar_date"],
        "bar_count": continuity["bar_count"],
        "expected_bar_count": continuity["expected_bar_count"],
        "missing_bar_count": continuity["missing_bar_count"],
        "continuity_status": continuity["continuity_status"],
        "market_data_quality": quality,
        "payload_json": snapshot["payload_json"],
    }
    return snapshot, features


def upsert_market_rows(conn: sqlite3.Connection, *, bars: list[dict[str, Any]], snapshots: list[dict[str, Any]], features: list[dict[str, Any]]) -> None:
    now = utc_now()
    with conn:
        for row in bars:
            conn.execute(
                """
                INSERT INTO market_bars_daily(
                    ticker, bar_date, source, open, high, low, close, volume, wap,
                    price_adjustment, raw_open, raw_high, raw_low, raw_close, adj_close,
                    adjustment_factor, dividend_amount, split_factor, corporate_action_source,
                    is_adjusted, is_provisional, data_quality, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(ticker, bar_date, source) DO UPDATE SET
                    open = excluded.open, high = excluded.high, low = excluded.low, close = excluded.close,
                    volume = excluded.volume, wap = excluded.wap,
                    price_adjustment = excluded.price_adjustment,
                    raw_open = excluded.raw_open,
                    raw_high = excluded.raw_high,
                    raw_low = excluded.raw_low,
                    raw_close = excluded.raw_close,
                    adj_close = excluded.adj_close,
                    adjustment_factor = excluded.adjustment_factor,
                    dividend_amount = excluded.dividend_amount,
                    split_factor = excluded.split_factor,
                    corporate_action_source = excluded.corporate_action_source,
                    is_adjusted = excluded.is_adjusted,
                    is_provisional = excluded.is_provisional,
                    data_quality = excluded.data_quality,
                    updated_at = excluded.updated_at
                """,
                (
                    row["ticker"],
                    row["bar_date"],
                    row["source"],
                    row["open"],
                    row["high"],
                    row["low"],
                    row["close"],
                    row["volume"],
                    row["wap"],
                    row.get("price_adjustment"),
                    row.get("raw_open"),
                    row.get("raw_high"),
                    row.get("raw_low"),
                    row.get("raw_close"),
                    row.get("adj_close"),
                    row.get("adjustment_factor"),
                    row.get("dividend_amount"),
                    row.get("split_factor"),
                    row.get("corporate_action_source"),
                    int(row.get("is_adjusted") or 0),
                    int(row.get("is_provisional") or 0),
                    row["data_quality"],
                    now,
                    now,
                ),
            )
        for row in snapshots:
            conn.execute(
                """
                INSERT INTO market_snapshots_daily(
                    asof_date, company_id, ticker, source, last_price, close_price, market_cap, shares_outstanding,
                    avg_volume_20d, avg_dollar_volume_20d, fifty_two_week_high, fifty_two_week_low, currency,
                    price_adjustment, is_adjusted, is_provisional, first_bar_date, last_bar_date, bar_count,
                    expected_bar_count, missing_bar_count, continuity_status, data_quality, payload_json, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(asof_date, company_id, source) DO UPDATE SET
                    last_price = excluded.last_price, close_price = excluded.close_price, market_cap = excluded.market_cap,
                    shares_outstanding = excluded.shares_outstanding, avg_volume_20d = excluded.avg_volume_20d,
                    avg_dollar_volume_20d = excluded.avg_dollar_volume_20d, fifty_two_week_high = excluded.fifty_two_week_high,
                    fifty_two_week_low = excluded.fifty_two_week_low, currency = excluded.currency,
                    price_adjustment = excluded.price_adjustment,
                    is_adjusted = excluded.is_adjusted,
                    is_provisional = excluded.is_provisional,
                    first_bar_date = excluded.first_bar_date,
                    last_bar_date = excluded.last_bar_date,
                    bar_count = excluded.bar_count,
                    expected_bar_count = excluded.expected_bar_count,
                    missing_bar_count = excluded.missing_bar_count,
                    continuity_status = excluded.continuity_status,
                    data_quality = excluded.data_quality,
                    payload_json = excluded.payload_json, updated_at = excluded.updated_at
                """,
                tuple(
                    row.get(field)
                    for field in [
                        "asof_date",
                        "company_id",
                        "ticker",
                        "source",
                        "last_price",
                        "close_price",
                        "market_cap",
                        "shares_outstanding",
                        "avg_volume_20d",
                        "avg_dollar_volume_20d",
                        "fifty_two_week_high",
                        "fifty_two_week_low",
                        "currency",
                        "price_adjustment",
                        "is_adjusted",
                        "is_provisional",
                        "first_bar_date",
                        "last_bar_date",
                        "bar_count",
                        "expected_bar_count",
                        "missing_bar_count",
                        "continuity_status",
                        "data_quality",
                        "payload_json",
                    ]
                )
                + (now, now),
            )
        for row in features:
            conn.execute(
                """
                INSERT INTO market_features_daily(
                    asof_date, company_id, ticker, source, close_price, market_cap, shares_outstanding, price_vs_200d_pct,
                    return_1m_pct, return_3m_pct, xbi_return_3m_pct, relative_strength_3m_vs_xbi,
                    distance_from_52w_high_pct, avg_dollar_volume_20d, liquidity_score, market_data_quality,
                    price_adjustment, is_adjusted, is_provisional, first_bar_date, last_bar_date, bar_count,
                    expected_bar_count, missing_bar_count, continuity_status, payload_json, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(asof_date, company_id, source) DO UPDATE SET
                    close_price = excluded.close_price, market_cap = excluded.market_cap, shares_outstanding = excluded.shares_outstanding,
                    price_vs_200d_pct = excluded.price_vs_200d_pct, return_1m_pct = excluded.return_1m_pct,
                    return_3m_pct = excluded.return_3m_pct, xbi_return_3m_pct = excluded.xbi_return_3m_pct,
                    relative_strength_3m_vs_xbi = excluded.relative_strength_3m_vs_xbi,
                    distance_from_52w_high_pct = excluded.distance_from_52w_high_pct,
                    avg_dollar_volume_20d = excluded.avg_dollar_volume_20d, liquidity_score = excluded.liquidity_score,
                    price_adjustment = excluded.price_adjustment,
                    is_adjusted = excluded.is_adjusted,
                    is_provisional = excluded.is_provisional,
                    first_bar_date = excluded.first_bar_date,
                    last_bar_date = excluded.last_bar_date,
                    bar_count = excluded.bar_count,
                    expected_bar_count = excluded.expected_bar_count,
                    missing_bar_count = excluded.missing_bar_count,
                    continuity_status = excluded.continuity_status,
                    market_data_quality = excluded.market_data_quality,
                    payload_json = excluded.payload_json,
                    updated_at = excluded.updated_at
                """,
                tuple(
                    row.get(field)
                    for field in [
                        "asof_date",
                        "company_id",
                        "ticker",
                        "source",
                        "close_price",
                        "market_cap",
                        "shares_outstanding",
                        "price_vs_200d_pct",
                        "return_1m_pct",
                        "return_3m_pct",
                        "xbi_return_3m_pct",
                        "relative_strength_3m_vs_xbi",
                        "distance_from_52w_high_pct",
                        "avg_dollar_volume_20d",
                        "liquidity_score",
                        "market_data_quality",
                        "price_adjustment",
                        "is_adjusted",
                        "is_provisional",
                        "first_bar_date",
                        "last_bar_date",
                        "bar_count",
                        "expected_bar_count",
                        "missing_bar_count",
                        "continuity_status",
                        "payload_json",
                    ]
                )
                + (now, now),
            )


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_FIELDNAMES, lineterminator="\n")
        writer.writeheader()
        writer.writerows([{field: row.get(field, "") for field in CSV_FIELDNAMES} for row in rows])


def load_current_feature_csv_rows(conn: sqlite3.Connection, companies: list[Company], asof_date: date, *, source: str) -> list[dict[str, Any]]:
    if not companies:
        return []
    company_names = {company.company_id: company.company_name for company in companies}
    company_order = {company.company_id: idx for idx, company in enumerate(companies)}
    placeholders = ",".join("?" for _ in company_names)
    fields = [field for field in CSV_FIELDNAMES if field != "company_name"]
    rows = conn.execute(
        f"""
        SELECT {", ".join(fields)}, company_id
        FROM market_features_daily
        WHERE source = ? AND asof_date = ? AND company_id IN ({placeholders})
        """,
        (source, asof_date.isoformat(), *company_names.keys()),
    ).fetchall()
    out: list[dict[str, Any]] = []
    for row in rows:
        data = dict(row)
        data["company_name"] = company_names.get(int(data["company_id"]), "")
        out.append(data)
    out.sort(key=lambda row: (company_order.get(int(row.get("company_id") or 0), len(company_order)), str(row.get("ticker") or "")))
    return out


def main() -> None:
    configure_logging()
    args = parse_args()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    base_dir = config_path.parent
    db_path = args.db.expanduser().resolve() if args.db else resolve_path(cfg_get(config, "paths.database_path"), base_dir=base_dir)
    final_universe_csv = resolve_path(
        cfg_get(config, "yahoo_market_data.final_scoring_universe_csv", cfg_get(config, "ib_market_data.final_scoring_universe_csv")),
        base_dir=base_dir,
    )
    output_csv = resolve_path(
        cfg_get(config, "yahoo_market_data.output_csv", "../output/biotech_index_reports/yahoo_adjusted_market_features.csv"),
        base_dir=base_dir,
    )
    source = str(cfg_get(config, "yahoo_market_data.source", DEFAULT_SOURCE) or DEFAULT_SOURCE).strip() or DEFAULT_SOURCE
    benchmark_ticker = normalize_ticker(cfg_get(config, "yahoo_market_data.benchmark_ticker", DEFAULT_BENCHMARK))
    requested_asof_arg = parse_date(args.asof) if args.asof else None
    if args.asof and requested_asof_arg is None:
        raise ValueError(f"Invalid --asof date: {args.asof}")
    explicit_start_date = parse_date(args.start_date) if args.start_date else None
    if args.start_date and explicit_start_date is None:
        raise ValueError(f"Invalid --start-date: {args.start_date}")
    configured_start_date = parse_date(cfg_get(config, "yahoo_market_data.start_date", "2023-04-24"))
    if configured_start_date is None:
        raise ValueError("Invalid yahoo_market_data.start_date config value")
    market_timezone = str(cfg_get(config, "yahoo_market_data.market_timezone", cfg_get(config, "ib_market_data.market_timezone", "America/New_York")))
    market_close_time = parse_clock_time(cfg_get(config, "yahoo_market_data.market_close_time", cfg_get(config, "ib_market_data.market_close_time", "16:15")))
    guard_enabled = as_bool(cfg_get(config, "yahoo_market_data.market_close_guard", cfg_get(config, "ib_market_data.market_close_guard", True)))
    requested_asof = requested_asof_arg or datetime.now(ZoneInfo(market_timezone)).date()
    asof_decision = resolve_effective_asof(
        requested_asof,
        guard_enabled=guard_enabled,
        market_timezone=market_timezone,
        market_close_time=market_close_time,
    )
    if asof_decision.guard_applied:
        LOGGER.info(
            "Market-close guard adjusted Yahoo as-of date: requested=%s effective=%s reason=%s",
            asof_decision.requested_asof.isoformat(),
            asof_decision.effective_asof.isoformat(),
            asof_decision.reason,
        )
    asof_date = asof_decision.effective_asof
    if configured_start_date > asof_date:
        raise ValueError(f"yahoo_market_data.start_date {configured_start_date.isoformat()} is after effective as-of date {asof_date.isoformat()}")
    if explicit_start_date is not None and explicit_start_date > asof_date:
        raise ValueError(f"--start-date {explicit_start_date.isoformat()} is after effective as-of date {asof_date.isoformat()}")
    refetch_days = args.refetch_days if args.refetch_days >= 0 else int(cfg_get(config, "yahoo_market_data.refetch_days", 0))
    continuity_max_missing_days = int(cfg_get(config, "yahoo_market_data.continuity_max_missing_days", cfg_get(config, "ib_market_data.continuity_max_missing_days", 2)))
    sqlite_timeout_sec = float(cfg_get(config, "runtime.sqlite_timeout_sec", 30.0))
    ticker_filter = {normalize_ticker(value) for value in args.tickers.split(",") if value.strip()}

    with connect(db_path, timeout_sec=sqlite_timeout_sec) as conn:
        init_db(conn)
        run_id = start_run(conn, run_type="sync_market_data_yahoo_adjusted", input_path=db_path)
        try:
            scoring_tickers = read_scoring_tickers(final_universe_csv)
            companies = load_companies(conn, scoring_tickers=scoring_tickers, ticker_filter=ticker_filter, max_tickers=args.max_tickers)
            LOGGER.info("Loaded %d company job(s) for Yahoo adjusted market sync", len(companies))
            all_symbols = [benchmark_ticker, *[company.ticker for company in companies]]
            latest_bar_dates = load_latest_bar_dates(conn, tickers=all_symbols, source=source)
            benchmark_start = explicit_start_date or compute_fetch_start(
                configured_start_date,
                asof_date,
                latest_bar_dates.get(benchmark_ticker),
                refetch_days,
            )
            try:
                benchmark_fetched = fetch_yahoo_bars(
                    benchmark_ticker,
                    start_date=benchmark_start,
                    asof_date=asof_date,
                    source=source,
                    provisional_asof=asof_decision.provisional_asof,
                )
            except Exception as exc:
                benchmark_fetched = []
                LOGGER.warning("Yahoo adjusted benchmark fetch failed for %s; trying existing DB bars: %s", benchmark_ticker, exc)
            benchmark_existing = load_existing_bars(
                conn,
                ticker=benchmark_ticker,
                source=source,
                start_date=configured_start_date,
                asof_date=asof_date,
            )
            benchmark_bars = merge_bars(benchmark_existing, benchmark_fetched)
            benchmark_ordered = sorted_bar_rows(benchmark_bars)
            reference_dates = reference_dates_from_bars(benchmark_ordered, asof_date)
            xbi_closes = [value for value in (to_float(row.get("close")) for row in benchmark_ordered) if value is not None and value > 0]
            if not xbi_closes:
                raise RuntimeError(f"Yahoo adjusted benchmark fetch produced no usable bars for {benchmark_ticker}")

            bars_to_upsert: list[dict[str, Any]] = list(benchmark_fetched)
            snapshots: list[dict[str, Any]] = []
            features: list[dict[str, Any]] = []
            failed_tickers: list[str] = []
            reused_existing_tickers: list[str] = []
            for idx, company in enumerate(companies, start=1):
                try:
                    fetch_start = explicit_start_date or compute_fetch_start(
                        configured_start_date,
                        asof_date,
                        latest_bar_dates.get(company.ticker),
                        refetch_days,
                    )
                    fetch_error: Exception | None = None
                    try:
                        fetched = fetch_yahoo_bars(
                            company.ticker,
                            start_date=fetch_start,
                            asof_date=asof_date,
                            source=source,
                            provisional_asof=asof_decision.provisional_asof,
                        )
                    except Exception as exc:
                        fetched = []
                        fetch_error = exc
                        LOGGER.warning("Yahoo adjusted fetch failed for %s; trying existing DB bars: %s", company.ticker, exc)
                    existing = load_existing_bars(
                        conn,
                        ticker=company.ticker,
                        source=source,
                        start_date=configured_start_date,
                        asof_date=asof_date,
                    )
                    bars = merge_bars(existing, fetched)
                    if not bars and fetch_error is not None:
                        raise fetch_error
                    if not fetched and existing:
                        reused_existing_tickers.append(company.ticker)
                    shares = load_latest_shares(conn, company.company_id, asof_date)
                    snapshot, feature = build_market_rows(
                        company,
                        bars,
                        xbi_closes,
                        shares,
                        asof_date,
                        source=source,
                        reference_dates=reference_dates,
                        continuity_max_missing_days=continuity_max_missing_days,
                    )
                    bars_to_upsert.extend(fetched)
                    snapshots.append(snapshot)
                    features.append(feature)
                    LOGGER.info(
                        "[%d/%d] %s fetched=%d bars=%d continuity=%s quality=%s",
                        idx,
                        len(companies),
                        company.ticker,
                        len(fetched),
                        len(bars),
                        feature.get("continuity_status"),
                        feature.get("market_data_quality"),
                    )
                except Exception as exc:
                    failed_tickers.append(company.ticker)
                    LOGGER.warning("Yahoo adjusted market sync failed for %s: %s", company.ticker, exc)
            if companies and not features:
                raise RuntimeError(f"Yahoo adjusted market sync produced no company feature rows; failed_tickers={','.join(failed_tickers)}")
            upsert_market_rows(conn, bars=bars_to_upsert, snapshots=snapshots, features=features)
            csv_rows = load_current_feature_csv_rows(conn, companies, asof_date, source=source)
            csv_tickers = {str(row.get("ticker") or "").upper() for row in csv_rows}
            missing_csv_tickers = sorted({company.ticker for company in companies} - csv_tickers)
            write_csv(output_csv, csv_rows)
            status = "partial" if failed_tickers else "success"
            message = (
                f"requested_asof={asof_decision.requested_asof.isoformat()} "
                f"effective_asof={asof_date.isoformat()} source={source} "
                f"start_date={explicit_start_date.isoformat() if explicit_start_date else configured_start_date.isoformat()} "
                f"refetch_days={refetch_days} output={output_csv}"
            )
            if failed_tickers:
                message += f" failed_tickers={','.join(failed_tickers)}"
            if reused_existing_tickers:
                message += f" reused_existing_tickers={','.join(reused_existing_tickers)}"
            if missing_csv_tickers:
                message += f" missing_output_tickers={','.join(missing_csv_tickers)}"
            finish_run(conn, run_id=run_id, status=status, row_count=len(features), message=message)
        except Exception as exc:
            finish_run(conn, run_id=run_id, status="failed", row_count=0, message=f"{type(exc).__name__}: {exc}")
            raise
    LOGGER.info("Yahoo adjusted market sync complete: rows=%d output=%s", len(features), output_csv)


if __name__ == "__main__":
    main()
