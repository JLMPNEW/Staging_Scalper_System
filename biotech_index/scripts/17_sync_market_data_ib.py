#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import logging
import math
import sqlite3
import sys
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
from biotech_index.core.logging_utils import configure_utc_logging
from biotech_index.core.pipeline_guards import (
    normalize_ticker,
    read_final_scoring_tickers,
    subset_mode_enabled,
    subset_output_path,
    validate_full_universe_coverage,
    validate_nonempty_selection,
    validate_requested_tickers,
)


LOGGER = logging.getLogger("sync_market_data_ib")
DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
SOURCE = "interactive_brokers"
SQLITE_PARAM_CHUNK_SIZE = 800


def chunked(values: list[Any] | tuple[Any, ...], size: int = SQLITE_PARAM_CHUNK_SIZE) -> list[list[Any]]:
    step = max(1, int(size))
    return [list(values[start : start + step]) for start in range(0, len(values), step)]


CSV_FIELDNAMES = [
    "asof_date",
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
    parser = argparse.ArgumentParser(description="Sync IB market prices/bars into the biotech index database.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--asof", type=str, default="", help="Snapshot date in YYYY-MM-DD. Defaults to the configured market timezone today.")
    parser.add_argument("--tickers", type=str, default="", help="Optional comma-separated ticker subset.")
    parser.add_argument("--max-tickers", type=int, default=0, help="Smoke-test limit. 0 means all.")
    parser.add_argument("--duration", type=str, default="", help="Override IB duration string, for example '4 Y'.")
    parser.add_argument("--start-date", type=str, default="", help="Only store bars on or after this YYYY-MM-DD date.")
    parser.add_argument("--full-refresh", action="store_true", help="Refetch the configured duration for every ticker instead of using incremental DB-backed refresh.")
    parser.add_argument("--repair-window-days", type=int, default=None, help="Refetch at least this many recent calendar days for every ticker in incremental mode.")
    parser.add_argument("--allow-partial", action="store_true", help="Return success even if one or more ticker updates fail.")
    parser.add_argument(
        "--offline-existing-bars",
        action="store_true",
        help="Build historical market snapshots/features only from existing market_bars_daily rows without connecting to IB.",
    )
    return parser.parse_args()


def configure_logging() -> None:
    configure_utc_logging()
    logging.getLogger("ib_insync.wrapper").setLevel(logging.WARNING)
    logging.getLogger("ib_insync.client").setLevel(logging.WARNING)
    logging.getLogger("ib_insync.ib").setLevel(logging.WARNING)


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


def read_scoring_tickers(path: Path) -> set[str]:
    return read_final_scoring_tickers(path)


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


def ib_end_datetime(asof_date: date) -> str:
    return f"{asof_date.strftime('%Y%m%d')} 23:59:59 US/Eastern"


def price_adjustment_for_what_to_show(what_to_show: str) -> tuple[str, int]:
    mode = str(what_to_show or "").strip().upper()
    if mode == "ADJUSTED_LAST":
        return "adjusted", 1
    return "raw", 0


def request_ib_bars(
    ib: Any,
    contract: Any,
    *,
    duration: str,
    asof_date: date,
    what_to_show: str,
    use_rth: bool,
) -> list[Any]:
    return list(
        ib.reqHistoricalData(
            contract,
            endDateTime=ib_end_datetime(asof_date),
            durationStr=duration,
            barSizeSetting="1 day",
            whatToShow=what_to_show,
            useRTH=use_rth,
            formatDate=1,
            keepUpToDate=False,
        )
        or []
    )


def fetch_bars(
    ib: Any,
    ticker: str,
    *,
    currency: str,
    duration: str,
    sleep_sec: float,
    asof_date: date,
    what_to_show: str,
    fallback_what_to_show: str,
    use_rth: bool,
    provisional_asof: bool,
) -> list[dict[str, Any]]:
    from ib_insync import Stock  # type: ignore

    contract = Stock(ticker, "SMART", currency or "USD")
    try:
        qualified = ib.qualifyContracts(contract)
        if not qualified:
            raise ValueError(f"IB could not qualify contract for {ticker}")
        attempts = [str(what_to_show or "ADJUSTED_LAST").strip().upper()]
        fallback = str(fallback_what_to_show or "").strip().upper()
        if fallback and fallback not in attempts:
            attempts.append(fallback)
        last_error: Exception | None = None
        bars: list[Any] = []
        used_what_to_show = attempts[0]
        for attempt in attempts:
            try:
                bars = request_ib_bars(
                    ib,
                    qualified[0],
                    duration=duration,
                    asof_date=asof_date,
                    what_to_show=attempt,
                    use_rth=use_rth,
                )
                if bars:
                    used_what_to_show = attempt
                    break
                last_error = ValueError(f"IB returned no bars for {ticker} using {attempt}")
            except Exception as exc:
                last_error = exc
                continue
        if not bars and last_error is not None:
            raise last_error
    finally:
        ib.sleep(sleep_sec)
    price_adjustment, is_adjusted = price_adjustment_for_what_to_show(used_what_to_show)
    out: list[dict[str, Any]] = []
    for bar in bars:
        bar_date = bar.date.isoformat() if hasattr(bar.date, "isoformat") else str(bar.date)
        bar_day = parse_date(bar_date[:10])
        out.append(
            {
                "ticker": ticker,
                "bar_date": bar_date[:10],
                "source": SOURCE,
                "open": to_float(bar.open),
                "high": to_float(bar.high),
                "low": to_float(bar.low),
                "close": to_float(bar.close),
                "volume": to_float(bar.volume),
                "wap": to_float(getattr(bar, "average", None)),
                "price_adjustment": price_adjustment,
                "is_adjusted": is_adjusted,
                "is_provisional": 1 if provisional_asof and bar_day == asof_date else 0,
                "what_to_show": used_what_to_show,
                "data_quality": "high",
            }
        )
    return out


def parse_bar_day(row: dict[str, Any]) -> date | None:
    return parse_date(str(row.get("bar_date") or "")[:10])


def sorted_bar_rows(bars: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted((row for row in bars if parse_bar_day(row) is not None), key=lambda row: str(row.get("bar_date") or ""))


def filter_bars_from_start(bars: list[dict[str, Any]], start_date: date | None) -> list[dict[str, Any]]:
    if start_date is None:
        return bars
    return [row for row in bars if (day := parse_bar_day(row)) is not None and day >= start_date]


def load_bars_from_db(conn: sqlite3.Connection, ticker: str, *, start_date: date, asof_date: date) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT ticker, bar_date, source, open, high, low, close, volume, wap,
               price_adjustment, is_adjusted, is_provisional, data_quality
        FROM market_bars_daily
        WHERE source = ?
          AND ticker = ?
          AND bar_date >= ?
          AND bar_date <= ?
        ORDER BY bar_date
        """,
        (SOURCE, ticker, start_date.isoformat(), asof_date.isoformat()),
    ).fetchall()
    out: list[dict[str, Any]] = []
    for row in rows:
        is_adjusted = int(row["is_adjusted"] or 0)
        out.append(
            {
                "ticker": str(row["ticker"] or ticker),
                "bar_date": str(row["bar_date"] or ""),
                "source": str(row["source"] or SOURCE),
                "open": to_float(row["open"]),
                "high": to_float(row["high"]),
                "low": to_float(row["low"]),
                "close": to_float(row["close"]),
                "volume": to_float(row["volume"]),
                "wap": to_float(row["wap"]),
                "price_adjustment": str(row["price_adjustment"] or ("adjusted" if is_adjusted else "raw")),
                "is_adjusted": is_adjusted,
                "is_provisional": int(row["is_provisional"] or 0),
                "what_to_show": "ADJUSTED_LAST" if is_adjusted else "",
                "data_quality": str(row["data_quality"] or "medium"),
            }
        )
    return out


def latest_bar_date(conn: sqlite3.Connection, ticker: str, *, asof_date: date) -> date | None:
    row = conn.execute(
        """
        SELECT MAX(bar_date) AS max_date
        FROM market_bars_daily
        WHERE source = ?
          AND ticker = ?
          AND bar_date <= ?
        """,
        (SOURCE, ticker, asof_date.isoformat()),
    ).fetchone()
    return parse_date(row["max_date"]) if row and row["max_date"] else None


def merge_bar_rows(*groups: list[dict[str, Any]], asof_date: date) -> list[dict[str, Any]]:
    merged: dict[tuple[str, str, str], dict[str, Any]] = {}
    for group in groups:
        for row in group:
            bar_day = parse_bar_day(row)
            if bar_day is None or bar_day > asof_date:
                continue
            ticker = str(row.get("ticker") or "").upper().replace(".", "-")
            bar_date = str(row.get("bar_date") or "")[:10]
            source = str(row.get("source") or SOURCE)
            if not ticker or not bar_date:
                continue
            merged[(ticker, bar_date, source)] = row
    return sorted(merged.values(), key=lambda row: (str(row.get("ticker") or ""), str(row.get("bar_date") or "")))


def incremental_duration(latest_date: date | None, asof_date: date, *, default_duration: str, repair_window_days: int) -> str:
    if latest_date is None:
        return default_duration
    missing_days = max(1, (asof_date - latest_date).days + 3)
    days = max(missing_days, repair_window_days)
    if days <= 7:
        return "1 W"
    if days <= 31:
        return "1 M"
    if days <= 93:
        return "3 M"
    if days <= 186:
        return "6 M"
    return default_duration


def should_fetch_incremental(latest_date: date | None, asof_date: date, *, repair_window_days: int) -> bool:
    if latest_date is None or latest_date < asof_date:
        return True
    return repair_window_days > 0


def reference_dates_from_bars(bars: list[dict[str, Any]], asof_date: date) -> list[date]:
    out = sorted({day for row in bars if (day := parse_bar_day(row)) is not None and day <= asof_date})
    return out


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
    reference_dates: list[date],
    continuity_max_missing_days: int,
) -> tuple[dict[str, Any], dict[str, Any]]:
    ordered_bars = sorted_bar_rows(bars)
    closes = [value for value in (to_float(row.get("close")) for row in ordered_bars) if value is not None and value > 0]
    volumes = [to_float(row.get("volume")) or 0.0 for row in ordered_bars if to_float(row.get("close")) is not None]
    if not closes:
        raise ValueError(f"No usable close prices for {company.ticker}")
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
    price_adjustment = str(common_value(ordered_bars, "price_adjustment", "raw"))
    is_adjusted = 1 if common_value(ordered_bars, "is_adjusted", 0) == 1 else 0
    is_provisional = 1 if any(int(row.get("is_provisional") or 0) for row in ordered_bars) else 0
    what_to_show = str(common_value(ordered_bars, "what_to_show", ""))
    payload = {
        "bar_count": len(closes),
        "market_cap_source": "ib_close_x_sec_shares" if market_cap else "missing_sec_shares",
        "price_adjustment": price_adjustment,
        "is_adjusted": bool(is_adjusted),
        "is_provisional": bool(is_provisional),
        "what_to_show": what_to_show,
        "continuity": continuity,
    }
    snapshot = {
        "asof_date": asof_date.isoformat(),
        "company_id": company.company_id,
        "ticker": company.ticker,
        "source": SOURCE,
        "last_price": close,
        "close_price": close,
        "market_cap": market_cap,
        "shares_outstanding": shares,
        "avg_volume_20d": avg_volume_20d,
        "avg_dollar_volume_20d": avg_dollar_volume_20d,
        "fifty_two_week_high": high_52w,
        "fifty_two_week_low": low_52w,
        "currency": company.currency,
        "price_adjustment": price_adjustment,
        "is_adjusted": is_adjusted,
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
        "source": SOURCE,
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
        "price_adjustment": price_adjustment,
        "is_adjusted": is_adjusted,
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
                    price_adjustment, is_adjusted, is_provisional, data_quality, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(ticker, bar_date, source) DO UPDATE SET
                    open = excluded.open, high = excluded.high, low = excluded.low, close = excluded.close,
                    volume = excluded.volume, wap = excluded.wap,
                    price_adjustment = excluded.price_adjustment,
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


def delete_market_rows_for_companies(
    conn: sqlite3.Connection,
    *,
    company_ids: set[int],
    asof_date: date,
    source: str,
) -> None:
    if not company_ids:
        return
    with conn:
        for company_chunk in chunked(sorted(company_ids)):
            placeholders = ",".join("?" for _ in company_chunk)
            params = (asof_date.isoformat(), source, *company_chunk)
            conn.execute(
                f"DELETE FROM market_snapshots_daily WHERE asof_date = ? AND source = ? AND company_id IN ({placeholders})",
                params,
            )
            conn.execute(
                f"DELETE FROM market_features_daily WHERE asof_date = ? AND source = ? AND company_id IN ({placeholders})",
                params,
            )


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_FIELDNAMES, lineterminator="\n")
        writer.writeheader()
        writer.writerows([{field: row.get(field, "") for field in CSV_FIELDNAMES} for row in rows])


def load_current_feature_csv_rows(conn: sqlite3.Connection, companies: list[Company], asof_date: date) -> list[dict[str, Any]]:
    if not companies:
        return []
    if len(companies) > SQLITE_PARAM_CHUNK_SIZE:
        rows: list[dict[str, Any]] = []
        company_order = {company.company_id: idx for idx, company in enumerate(companies)}
        for company_chunk in chunked(companies):
            rows.extend(load_current_feature_csv_rows(conn, [company for company in company_chunk], asof_date))
        rows.sort(key=lambda row: (company_order.get(int(row.get("company_id") or 0), len(company_order)), str(row.get("ticker") or "")))
        return rows
    company_names = {company.company_id: company.company_name for company in companies}
    company_order = {company.company_id: idx for idx, company in enumerate(companies)}
    placeholders = ",".join("?" for _ in company_names)
    rows = conn.execute(
        f"""
        SELECT {", ".join(field for field in CSV_FIELDNAMES if field != "company_name")}, company_id
        FROM market_features_daily
        WHERE source = ? AND asof_date = ? AND company_id IN ({placeholders})
        """,
        (SOURCE, asof_date.isoformat(), *company_names.keys()),
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
    output_csv = resolve_path(cfg_get(config, "ib_market_data.output_csv"), base_dir=base_dir)
    final_universe_csv = resolve_path(cfg_get(config, "ib_market_data.final_scoring_universe_csv"), base_dir=base_dir)
    requested_asof_arg = parse_date(args.asof) if args.asof else None
    if args.asof and requested_asof_arg is None:
        raise ValueError(f"Invalid --asof date: {args.asof}")
    start_date = parse_date(args.start_date) if args.start_date else None
    if args.start_date and start_date is None:
        raise ValueError(f"Invalid --start-date: {args.start_date}")
    ticker_filter = {normalize_ticker(value) for value in args.tickers.split(",") if normalize_ticker(value)}

    host = str(cfg_get(config, "ib_market_data.host", "127.0.0.1"))
    port = int(cfg_get(config, "ib_market_data.port", 7497))
    client_id = int(cfg_get(config, "ib_market_data.client_id", 7717))
    duration_override = str(args.duration or "").strip()
    what_to_show = str(cfg_get(config, "ib_market_data.what_to_show", "ADJUSTED_LAST")).strip().upper()
    fallback_what_to_show = str(cfg_get(config, "ib_market_data.fallback_what_to_show", "TRADES")).strip().upper()
    use_rth = as_bool(cfg_get(config, "ib_market_data.use_rth", True))
    market_timezone = str(cfg_get(config, "ib_market_data.market_timezone", "America/New_York"))
    market_close_time = parse_clock_time(cfg_get(config, "ib_market_data.market_close_time", "16:15"))
    guard_enabled = as_bool(cfg_get(config, "ib_market_data.market_close_guard", True))
    requested_asof = requested_asof_arg or datetime.now(ZoneInfo(market_timezone)).date()
    asof_decision = resolve_effective_asof(
        requested_asof,
        guard_enabled=guard_enabled,
        market_timezone=market_timezone,
        market_close_time=market_close_time,
    )
    if asof_decision.guard_applied:
        LOGGER.info(
            "Market-close guard adjusted IB as-of date: requested=%s effective=%s reason=%s",
            asof_decision.requested_asof.isoformat(),
            asof_decision.effective_asof.isoformat(),
            asof_decision.reason,
        )
    asof_date = asof_decision.effective_asof
    if start_date is not None and start_date > asof_date:
        raise ValueError(f"--start-date {start_date.isoformat()} is after effective as-of date {asof_date.isoformat()}")
    duration = duration_override or str(cfg_get(config, "ib_market_data.duration", "1 Y"))
    full_refresh = bool(args.full_refresh or duration_override or start_date is not None)
    offline_existing_bars = bool(args.offline_existing_bars)
    if offline_existing_bars and full_refresh:
        raise ValueError("--offline-existing-bars cannot be combined with --full-refresh, --duration, or --start-date")
    repair_window_days = (
        max(0, int(args.repair_window_days))
        if args.repair_window_days is not None
        else max(0, int(cfg_get(config, "ib_market_data.repair_window_days", 0)))
    )
    incremental_lookback_days = max(365, int(cfg_get(config, "ib_market_data.incremental_lookback_days", 400)))
    history_start = start_date or (asof_date - timedelta(days=incremental_lookback_days))
    sleep_sec = float(cfg_get(config, "ib_market_data.sleep_sec", 0.15))
    continuity_max_missing_days = int(cfg_get(config, "ib_market_data.continuity_max_missing_days", 2))
    sqlite_timeout_sec = float(cfg_get(config, "runtime.sqlite_timeout_sec", 30.0))

    if offline_existing_bars:
        IB = None
    else:
        try:
            from ib_insync import IB  # type: ignore
        except Exception as exc:
            raise RuntimeError("ib_insync is required for IB market data sync. Install package 'ib_insync'.") from exc

    with connect(db_path, timeout_sec=sqlite_timeout_sec) as conn:
        init_db(conn)
        run_id = start_run(conn, run_type="sync_market_data_ib", input_path=db_path)
        ib = IB() if IB is not None else None
        try:
            scoring_tickers = read_scoring_tickers(final_universe_csv)
            companies = load_companies(conn, scoring_tickers=scoring_tickers, ticker_filter=ticker_filter, max_tickers=args.max_tickers)
            subset_mode = subset_mode_enabled(ticker_filter=ticker_filter, max_count=int(args.max_tickers))
            output_csv = subset_output_path(output_csv, subset_mode=subset_mode)
            validate_nonempty_selection(count=len(companies), context="IB market sync", subset_mode=subset_mode)
            loaded_tickers = [company.ticker for company in companies]
            validate_requested_tickers(requested_tickers=ticker_filter, loaded_tickers=loaded_tickers, context="IB market sync")
            validate_full_universe_coverage(
                expected_tickers=scoring_tickers,
                observed_tickers=loaded_tickers,
                context="IB market sync",
                subset_mode=subset_mode,
            )
            LOGGER.info(
                "Loaded %d company job(s) for IB market sync mode=%s",
                len(companies),
                "offline_existing_bars" if offline_existing_bars else ("full_refresh" if full_refresh else "incremental"),
            )

            def ensure_ib_connected() -> None:
                if ib is None:
                    raise RuntimeError("IB connection requested while --offline-existing-bars is enabled")
                if not ib.isConnected():
                    ib.connect(
                        host,
                        port,
                        clientId=client_id,
                        timeout=float(cfg_get(config, "ib_market_data.connect_timeout_sec", 15.0)),
                    )

            all_bars: list[dict[str, Any]] = []
            fetched_tickers: list[str] = []
            skipped_tickers: list[str] = []
            stale_feature_tickers: list[str] = []
            failed_tickers: list[str] = []

            xbi_existing = [] if full_refresh else load_bars_from_db(conn, "XBI", start_date=history_start, asof_date=asof_date)
            xbi_latest = None if full_refresh else latest_bar_date(conn, "XBI", asof_date=asof_date)
            xbi_fetch_needed = False if offline_existing_bars else (
                full_refresh or should_fetch_incremental(
                    xbi_latest,
                    asof_date,
                    repair_window_days=repair_window_days,
                )
            )
            xbi_fetched: list[dict[str, Any]] = []
            benchmark_refresh_failed = False
            if xbi_fetch_needed:
                try:
                    ensure_ib_connected()
                    xbi_fetch_duration = (
                        duration
                        if full_refresh
                        else incremental_duration(
                            xbi_latest,
                            asof_date,
                            default_duration=duration,
                            repair_window_days=repair_window_days,
                        )
                    )
                    xbi_fetched = fetch_bars(
                        ib,
                        "XBI",
                        currency="USD",
                        duration=xbi_fetch_duration,
                        sleep_sec=sleep_sec,
                        asof_date=asof_date,
                        what_to_show=what_to_show,
                        fallback_what_to_show=fallback_what_to_show,
                        use_rth=use_rth,
                        provisional_asof=asof_decision.provisional_asof,
                    )
                    xbi_fetched = filter_bars_from_start(xbi_fetched, start_date)
                    all_bars.extend(xbi_fetched)
                    LOGGER.info("XBI benchmark fetched bars=%d duration=%s", len(xbi_fetched), xbi_fetch_duration)
                except Exception as exc:
                    if not xbi_existing:
                        raise
                    benchmark_refresh_failed = True
                    LOGGER.warning("XBI benchmark refresh failed; using existing bars: %s", exc)
            elif offline_existing_bars:
                LOGGER.info("XBI benchmark using existing DB bars for offline snapshot through %s", asof_date.isoformat())
            else:
                LOGGER.info("XBI benchmark already current through %s; using DB bars", asof_date.isoformat())
            xbi_bars = xbi_fetched if full_refresh else merge_bar_rows(xbi_existing, xbi_fetched, asof_date=asof_date)
            xbi_ordered = sorted_bar_rows(xbi_bars)
            reference_dates = reference_dates_from_bars(xbi_ordered, asof_date)
            xbi_closes = [value for value in (to_float(row.get("close")) for row in xbi_ordered) if value is not None and value > 0]
            if not xbi_closes:
                raise RuntimeError("IB market sync has no usable XBI benchmark bars")
            snapshots: list[dict[str, Any]] = []
            features: list[dict[str, Any]] = []
            csv_rows: list[dict[str, Any]] = []
            for idx, company in enumerate(companies, start=1):
                try:
                    existing_bars = [] if full_refresh else load_bars_from_db(
                        conn,
                        company.ticker,
                        start_date=history_start,
                        asof_date=asof_date,
                    )
                    latest_date = None if full_refresh else latest_bar_date(conn, company.ticker, asof_date=asof_date)
                    fetch_needed = False if offline_existing_bars else (
                        full_refresh or should_fetch_incremental(
                            latest_date,
                            asof_date,
                            repair_window_days=repair_window_days,
                        )
                    )
                    fetched_bars: list[dict[str, Any]] = []
                    fetch_error: Exception | None = None
                    action = "offline_existing" if offline_existing_bars else "skipped"
                    if offline_existing_bars and not existing_bars:
                        failed_tickers.append(company.ticker)
                        LOGGER.warning("IB market offline snapshot has no existing bars for %s", company.ticker)
                        continue
                    if fetch_needed:
                        fetch_duration = (
                            duration
                            if full_refresh
                            else incremental_duration(
                                latest_date,
                                asof_date,
                                default_duration=duration,
                                repair_window_days=repair_window_days,
                            )
                        )
                        action = "fetched"
                        try:
                            ensure_ib_connected()
                            fetched_bars = fetch_bars(
                                ib,
                                company.ticker,
                                currency=company.currency or "USD",
                                duration=fetch_duration,
                                sleep_sec=sleep_sec,
                                asof_date=asof_date,
                                what_to_show=what_to_show,
                                fallback_what_to_show=fallback_what_to_show,
                                use_rth=use_rth,
                                provisional_asof=asof_decision.provisional_asof,
                            )
                            fetched_bars = filter_bars_from_start(fetched_bars, start_date)
                            all_bars.extend(fetched_bars)
                            fetched_tickers.append(company.ticker)
                        except Exception as exc:
                            fetch_error = exc
                            failed_tickers.append(company.ticker)
                            LOGGER.warning("IB market sync failed for %s: %s", company.ticker, exc)
                    else:
                        skipped_tickers.append(company.ticker)

                    if fetch_error is not None and (full_refresh or not existing_bars):
                        continue
                    bars = fetched_bars if full_refresh else merge_bar_rows(existing_bars, fetched_bars, asof_date=asof_date)
                    if fetch_error is not None:
                        stale_feature_tickers.append(company.ticker)
                        action = "stale_existing"
                    shares = load_latest_shares(conn, company.company_id, asof_date)
                    snapshot, feature = build_market_rows(
                        company,
                        bars,
                        xbi_closes,
                        shares,
                        asof_date,
                        reference_dates=reference_dates,
                        continuity_max_missing_days=continuity_max_missing_days,
                    )
                    snapshots.append(snapshot)
                    features.append(feature)
                    csv_rows.append({"company_name": company.company_name, **feature})
                    LOGGER.info(
                        "[%d/%d] %s action=%s bars=%d fetched=%d continuity=%s quality=%s adjustment=%s",
                        idx,
                        len(companies),
                        company.ticker,
                        action,
                        len(bars),
                        len(fetched_bars),
                        feature.get("continuity_status"),
                        feature.get("market_data_quality"),
                        feature.get("price_adjustment"),
                    )
                except Exception as exc:
                    failed_tickers.append(company.ticker)
                    LOGGER.warning("IB market sync failed for %s: %s", company.ticker, exc)
            if companies and not features:
                raise RuntimeError(f"IB market sync produced no company feature rows; failed_tickers={','.join(failed_tickers)}")
            upsert_market_rows(conn, bars=all_bars, snapshots=snapshots, features=features)
            unique_failed_tickers = list(dict.fromkeys(failed_tickers))
            failed_company_ids = {company.company_id for company in companies if company.ticker in set(unique_failed_tickers)}
            delete_market_rows_for_companies(conn, company_ids=failed_company_ids, asof_date=asof_date, source=SOURCE)
            successful_companies = [company for company in companies if company.company_id not in failed_company_ids]
            csv_rows = load_current_feature_csv_rows(conn, successful_companies, asof_date)
            csv_tickers = {str(row.get("ticker") or "").upper() for row in csv_rows}
            missing_csv_tickers = sorted({company.ticker for company in companies} - csv_tickers)
            write_csv(output_csv, csv_rows)
            status = "partial" if benchmark_refresh_failed or unique_failed_tickers or missing_csv_tickers else "success"
            mode_label = "offline_existing_bars" if offline_existing_bars else ("full_refresh" if full_refresh else "incremental")
            message = (
                f"requested_asof={asof_decision.requested_asof.isoformat()} "
                f"effective_asof={asof_date.isoformat()} mode={mode_label} "
                f"duration={duration} start_date={start_date.isoformat() if start_date else ''} "
                f"repair_window_days={repair_window_days if not full_refresh else ''} "
                f"fetched={len(fetched_tickers)} skipped={len(skipped_tickers)} stale_existing={len(stale_feature_tickers)} "
                f"bars_upserted={len(all_bars)} adjustment={what_to_show} output={output_csv}"
            )
            if unique_failed_tickers:
                message += f" failed_tickers={','.join(unique_failed_tickers)}"
            if missing_csv_tickers:
                message += f" missing_output_tickers={','.join(missing_csv_tickers)}"
            if benchmark_refresh_failed:
                message += " benchmark_refresh_failed=1"
            finish_run(conn, run_id=run_id, status=status, row_count=len(features), message=message)
            LOGGER.info("IB market sync complete: rows=%d output=%s", len(features), output_csv)
            if status != "success" and not args.allow_partial:
                raise SystemExit(2)
        except BaseException as exc:
            finish_run(conn, run_id=run_id, status="failed", row_count=0, message=f"{type(exc).__name__}: {exc}")
            raise
        finally:
            if ib is not None and ib.isConnected():
                ib.disconnect()


if __name__ == "__main__":
    main()
