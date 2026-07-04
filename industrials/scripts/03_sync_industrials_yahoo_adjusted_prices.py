#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import math
import os
import sys
import time
from contextlib import closing
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, cast

import requests  # type: ignore[reportMissingModuleSource]


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from industrials.core.config import cfg_get, expand_env_vars, load_yaml, resolve_path  # noqa: E402
from industrials.core.db import connect, finish_run, init_db, start_run, utc_now  # noqa: E402
from industrials.core.logging_utils import configure_utc_logging  # noqa: E402
from industrials.core.reports import write_csv_atomic  # noqa: E402
from industrials.core.source_registry import load_source_registry, upsert_source_registry  # noqa: E402
from industrials.core.text_norm import normalize_ticker  # noqa: E402


LOGGER = logging.getLogger("sync_industrials_yahoo_adjusted_prices")
DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
RUN_TYPE = "sync_industrials_yahoo_adjusted_prices"
SOURCE_ID_DEFAULT = "yahoo_finance_adjusted"
FIELDNAMES = [
    "ticker",
    "fetch_ticker",
    "company_name",
    "is_benchmark",
    "alias_routed",
    "source_id",
    "status",
    "bars_upserted",
    "actions_upserted",
    "first_bar_date",
    "last_bar_date",
    "latest_close",
    "latest_adj_close",
    "price_adjustment",
    "is_adjusted",
    "review_reason",
]


@dataclass(frozen=True)
class PriceJob:
    ticker: str
    fetch_ticker: str
    company_name: str
    is_benchmark: bool = False


@dataclass(frozen=True)
class YahooBar:
    ticker: str
    bar_date: str
    source_id: str
    open: float | None
    high: float | None
    low: float | None
    close: float
    adj_close: float | None
    volume: float | None
    dividend_amount: float | None
    split_factor: float | None
    price_adjustment: str
    is_adjusted: int


@dataclass(frozen=True)
class CorporateAction:
    ticker: str
    action_date: str
    source_id: str
    action_type: str
    cash_amount: float | None = None
    split_numerator: float | None = None
    split_denominator: float | None = None
    split_factor: float | None = None
    raw_value: str = ""


@dataclass(frozen=True)
class FetchResult:
    job: PriceJob
    endpoint: str
    query_params: dict[str, Any]
    status_code: int
    payload_text: str
    bars: list[YahooBar]
    actions: list[CorporateAction]
    meta: dict[str, Any]
    error: str = ""
    cache_status: str = "live"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Sync Yahoo adjusted daily OHLCV for an industrials model family.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--model-family", default="", help="Industrials model family to refresh, e.g. defense.")
    parser.add_argument("--start-date", default="")
    parser.add_argument("--asof", default="", help="Fetch bars through this YYYY-MM-DD date. Defaults to today.")
    parser.add_argument("--tickers", default="", help="Optional comma-separated contract ticker subset.")
    parser.add_argument("--max-tickers", type=int, default=0)
    parser.add_argument("--benchmark-tickers", default="", help="Optional comma-separated benchmark ticker override.")
    parser.add_argument("--skip-benchmarks", action="store_true")
    parser.add_argument("--force-refresh", action="store_true")
    parser.add_argument("--allow-partial", action="store_true")
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


def unix_timestamp(day: date) -> int:
    return int(datetime(day.year, day.month, day.day, tzinfo=timezone.utc).timestamp())


def to_float(raw: object) -> float | None:
    if raw is None:
        return None
    try:
        value = float(str(raw).strip())
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def to_int(raw: object) -> int | None:
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return None


def regular_session_bounds(meta: dict[str, Any]) -> tuple[int | None, int | None, int | None]:
    """Return (regular_start, regular_end, regular_market_time) epoch seconds from chart meta."""
    raw_period = meta.get("currentTradingPeriod")
    period = cast(dict[str, Any], raw_period) if isinstance(raw_period, dict) else {}
    raw_regular = period.get("regular")
    regular = cast(dict[str, Any], raw_regular) if isinstance(raw_regular, dict) else {}
    return to_int(regular.get("start")), to_int(regular.get("end")), to_int(meta.get("regularMarketTime"))


def write_text_atomic(path: Path, text: str) -> None:
    """Write text to a temp file and os.replace() it into place so readers never see a truncated file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f"{path.name}.tmp")
    tmp_path.write_text(text, encoding="utf-8")
    os.replace(tmp_path, path)


def cached_payload_stale_reason(payload_text: str) -> str | None:
    """Return why a cached chart payload must be discarded, or None when it is safe to reuse.

    Two invalidation rules:
    - Corrupt (truncated / non-JSON) cache files must be deleted and refetched
      instead of poisoning the ticker on every run.
    - A payload captured while the regular session was still in progress
      (regularMarketTime < currentTradingPeriod.regular.end) must not be served
      once that session has closed, otherwise the completed session's final bar
      is never observed for this (ticker, start, end) cache key.
    """
    try:
        payload = json.loads(payload_text)
    except json.JSONDecodeError as exc:
        return f"json_decode_error: {exc}"
    chart = payload.get("chart") if isinstance(payload, dict) else None
    results = chart.get("result") if isinstance(chart, dict) else None
    first = results[0] if isinstance(results, list) and results and isinstance(results[0], dict) else {}
    raw_meta = first.get("meta") if isinstance(first, dict) else None
    meta = cast(dict[str, Any], raw_meta) if isinstance(raw_meta, dict) else {}
    _, regular_end, market_time = regular_session_bounds(meta)
    if regular_end is None or market_time is None:
        return None
    if market_time < regular_end and int(datetime.now(timezone.utc).timestamp()) >= regular_end:
        return "intraday_payload_for_completed_session"
    return None


def int_set(raw: object, default: set[int]) -> set[int]:
    values = raw if isinstance(raw, list) else list(default)
    out: set[int] = set()
    for value in values:
        try:
            out.add(int(value))
        except (TypeError, ValueError):
            LOGGER.warning("Ignoring invalid retry status code: %r", value)
    return out or set(default)


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


def cache_name(fetch_ticker: str, start_date: date, end_date: date) -> str:
    safe = "".join(ch for ch in normalize_ticker(fetch_ticker) if ch.isalnum() or ch in "._-")
    return f"{safe}_{start_date.isoformat()}_{end_date.isoformat()}.json"


def last_expected_trading_day(day: date) -> date:
    """Weekend-aware expected latest bar date.

    This intentionally avoids a full exchange-calendar dependency. Exchange
    holidays may still re-fetch once, which is acceptable; weekends should not.
    """
    expected = day
    while expected.weekday() >= 5:
        expected -= timedelta(days=1)
    return expected


def resolve_fetch_ticker(conn: Any, ticker: str, asof: date) -> str:
    row = conn.execute(
        """
        SELECT active_ticker
        FROM dim_ticker_alias
        WHERE contract_ticker = ?
          AND effective_date <= ?
          AND verified_flag = 1
        ORDER BY effective_date DESC
        LIMIT 1
        """,
        (ticker, asof.isoformat()),
    ).fetchone()
    return normalize_ticker(row["active_ticker"]) if row is not None else ticker


def load_universe_jobs(
    conn: Any,
    *,
    model_family: str,
    ticker_filter: set[str],
    max_tickers: int,
    asof: date,
) -> list[PriceJob]:
    rows = conn.execute(
        """
        SELECT DISTINCT c.ticker, c.company_name
        FROM dim_company c
        JOIN dim_industrials_taxonomy t
          ON t.ticker = c.ticker
         AND t.model_family = ?
        WHERE c.is_active = 1
        ORDER BY c.ticker
        """,
        (model_family,),
    ).fetchall()
    out: list[PriceJob] = []
    seen: set[str] = set()
    for row in rows:
        ticker = normalize_ticker(row["ticker"])
        if not ticker or ticker in seen:
            continue
        if ticker_filter and ticker not in ticker_filter:
            continue
        fetch_ticker = resolve_fetch_ticker(conn, ticker, asof)
        out.append(PriceJob(ticker=ticker, fetch_ticker=fetch_ticker, company_name=str(row["company_name"] or "")))
        seen.add(ticker)
        if max_tickers > 0 and len(out) >= max_tickers:
            break
    return out


def append_benchmarks(jobs: list[PriceJob], benchmark_tickers: list[str], *, skip_benchmarks: bool) -> list[PriceJob]:
    if skip_benchmarks:
        return jobs
    out = list(jobs)
    seen = {job.ticker for job in out}
    for raw in benchmark_tickers:
        ticker = normalize_ticker(raw)
        if not ticker or ticker in seen:
            continue
        out.append(PriceJob(ticker=ticker, fetch_ticker=ticker, company_name=ticker, is_benchmark=True))
        seen.add(ticker)
    return out


COVERAGE_START_TOLERANCE_DAYS = 7


def existing_coverage_row(
    conn: Any,
    job: PriceJob,
    *,
    source_id: str,
    required_start_date: date,
    required_through_date: date,
) -> dict[str, Any] | None:
    row = conn.execute(
        """
        SELECT
            COUNT(*) AS bars,
            MIN(bar_date) AS first_bar_date,
            MAX(bar_date) AS last_bar_date,
            SUM(CASE WHEN adj_close IS NOT NULL THEN 1 ELSE 0 END) AS adjusted_bars
        FROM fact_price_ohlcv
        WHERE ticker = ?
          AND source_id = ?
        """,
        (job.ticker, source_id),
    ).fetchone()
    if row is None:
        return None
    bars = int(row["bars"] or 0)
    adjusted_bars = int(row["adjusted_bars"] or 0)
    first_bar_date = parse_date(row["first_bar_date"])
    last_bar_date = parse_date(row["last_bar_date"])
    if bars == 0 or adjusted_bars == 0 or first_bar_date is None or last_bar_date is None:
        return None
    if adjusted_bars != bars:
        # Partial adjustment coverage (NULL adj_close bars) is not "current"; refetch.
        return None
    if first_bar_date > required_start_date + timedelta(days=COVERAGE_START_TOLERANCE_DAYS):
        # Stored history starts later than the requested window (beyond a small
        # weekend/holiday tolerance): a prior narrow-window fetch or an explicit
        # earlier --start-date must force a refetch rather than be skipped.
        return None
    if last_bar_date < required_through_date:
        return None
    latest = conn.execute(
        """
        SELECT close, adj_close, price_adjustment, is_adjusted
        FROM fact_price_ohlcv
        WHERE ticker = ?
          AND source_id = ?
          AND bar_date = ?
        LIMIT 1
        """,
        (job.ticker, source_id, last_bar_date.isoformat()),
    ).fetchone()
    return {
        "ticker": job.ticker,
        "fetch_ticker": job.fetch_ticker,
        "company_name": job.company_name,
        "is_benchmark": int(job.is_benchmark),
        "alias_routed": int(job.fetch_ticker != job.ticker),
        "source_id": source_id,
        "status": "already_current",
        "bars_upserted": 0,
        "actions_upserted": 0,
        "first_bar_date": first_bar_date.isoformat(),
        "last_bar_date": last_bar_date.isoformat(),
        "latest_close": latest["close"] if latest is not None else "",
        "latest_adj_close": latest["adj_close"] if latest is not None else "",
        "price_adjustment": latest["price_adjustment"] if latest is not None else "",
        "is_adjusted": latest["is_adjusted"] if latest is not None else 0,
        "review_reason": (
            f"Existing fully adjusted OHLCV covers {first_bar_date.isoformat()}..{last_bar_date.isoformat()} "
            f"for requested window {required_start_date.isoformat()}..{required_through_date.isoformat()}."
        ),
    }


def fetch_chart_payload(
    *,
    endpoint: str,
    query_params: dict[str, Any],
    headers: dict[str, str],
    timeout_sec: float,
    max_retries: int,
    retry_status_codes: set[int],
) -> tuple[int, str]:
    last_error = ""
    for attempt in range(max(1, max_retries) + 1):
        try:
            response = requests.get(endpoint, params=query_params, headers=headers, timeout=timeout_sec)
            text = response.text
            if response.status_code == 200:
                return response.status_code, text
            last_error = f"HTTP {response.status_code}: {text[:300]}"
            if response.status_code not in retry_status_codes:
                return response.status_code, text
        except requests.RequestException as exc:
            last_error = f"{type(exc).__name__}: {exc}"
        if attempt < max_retries:
            time.sleep(min(2.0 * (attempt + 1), 10.0))
    return 0, json.dumps({"error": last_error})


def without_unclosed_trailing_bar(bars: list[YahooBar], last_bar_ts: int | None, meta: dict[str, Any]) -> list[YahooBar]:
    """Drop the trailing in-progress session bar so intraday close/volume is never stored as final OHLCV.

    Yahoo's v8 chart payload includes the current regular session's partial bar
    while the market is open. The bar is kept only once the session is closed
    (meta.regularMarketTime >= meta.currentTradingPeriod.regular.end). When the
    session metadata is missing we cannot prove the session closed, so a
    trailing bar dated today (UTC) is dropped as well; it is re-fetched on the
    next run once the session is verifiably complete.
    """
    if not bars:
        return bars
    regular_start, regular_end, market_time = regular_session_bounds(meta)
    if regular_start is not None and regular_end is not None and market_time is not None:
        session_closed = market_time >= regular_end
        if not session_closed and last_bar_ts is not None and last_bar_ts >= regular_start:
            return bars[:-1]
        return bars
    if bars[-1].bar_date == datetime.now(timezone.utc).date().isoformat():
        return bars[:-1]
    return bars


def parse_chart_result(job: PriceJob, payload_text: str, source_id: str) -> tuple[list[YahooBar], list[CorporateAction], dict[str, Any], str]:
    try:
        payload = json.loads(payload_text)
    except json.JSONDecodeError as exc:
        return [], [], {}, f"json_decode_error: {exc}"
    chart = payload.get("chart") if isinstance(payload, dict) else None
    if not isinstance(chart, dict):
        return [], [], {}, "missing_chart_payload"
    error = chart.get("error")
    if error:
        return [], [], {}, f"yahoo_chart_error: {error}"
    results = chart.get("result")
    if not isinstance(results, list) or not results:
        return [], [], {}, "missing_chart_result"
    if not isinstance(results[0], dict):
        return [], [], {}, "missing_chart_result"
    result = cast(dict[str, Any], results[0])
    raw_meta = result.get("meta")
    meta = cast(dict[str, Any], raw_meta) if isinstance(raw_meta, dict) else {}
    timestamps = result.get("timestamp") or []
    raw_indicators = result.get("indicators")
    indicators = cast(dict[str, Any], raw_indicators) if isinstance(raw_indicators, dict) else {}
    raw_quote = indicators.get("quote")
    raw_adjclose = indicators.get("adjclose")
    quote = raw_quote[0] if isinstance(raw_quote, list) and raw_quote and isinstance(raw_quote[0], dict) else {}
    adjclose = raw_adjclose[0] if isinstance(raw_adjclose, list) and raw_adjclose and isinstance(raw_adjclose[0], dict) else {}
    quote = cast(dict[str, Any], quote)
    adjclose = cast(dict[str, Any], adjclose)
    opens = quote.get("open") or []
    highs = quote.get("high") or []
    lows = quote.get("low") or []
    closes = quote.get("close") or []
    volumes = quote.get("volume") or []
    adjusted = adjclose.get("adjclose") or []

    dividends_by_date: dict[str, float] = {}
    split_by_date: dict[str, float] = {}
    actions: list[CorporateAction] = []
    raw_events = result.get("events")
    events = cast(dict[str, Any], raw_events) if isinstance(raw_events, dict) else {}
    raw_dividends = events.get("dividends")
    dividends = cast(dict[str, Any], raw_dividends) if isinstance(raw_dividends, dict) else {}
    for raw_event in dividends.values():
        if not isinstance(raw_event, dict):
            continue
        action_date = datetime.fromtimestamp(int(raw_event.get("date", 0)), tz=timezone.utc).date().isoformat()
        amount = to_float(raw_event.get("amount"))
        if amount is None:
            continue
        dividends_by_date[action_date] = amount
        actions.append(
            CorporateAction(
                ticker=job.ticker,
                action_date=action_date,
                source_id=source_id,
                action_type="dividend",
                cash_amount=amount,
                raw_value=json.dumps(raw_event, sort_keys=True),
            )
        )
    raw_splits = events.get("splits")
    splits = cast(dict[str, Any], raw_splits) if isinstance(raw_splits, dict) else {}
    for raw_event in splits.values():
        if not isinstance(raw_event, dict):
            continue
        action_date = datetime.fromtimestamp(int(raw_event.get("date", 0)), tz=timezone.utc).date().isoformat()
        numerator = to_float(raw_event.get("numerator"))
        denominator = to_float(raw_event.get("denominator"))
        factor = numerator / denominator if numerator is not None and denominator not in (None, 0) else None
        if factor is None:
            continue
        split_by_date[action_date] = factor
        actions.append(
            CorporateAction(
                ticker=job.ticker,
                action_date=action_date,
                source_id=source_id,
                action_type="split",
                split_numerator=numerator,
                split_denominator=denominator,
                split_factor=factor,
                raw_value=json.dumps(raw_event, sort_keys=True),
            )
        )

    bars: list[YahooBar] = []
    last_bar_ts: int | None = None
    for idx, raw_ts in enumerate(timestamps):
        try:
            bar_ts = int(raw_ts)
            bar_date = datetime.fromtimestamp(bar_ts, tz=timezone.utc).date().isoformat()
        except (TypeError, ValueError, OSError):
            continue
        close = to_float(closes[idx] if idx < len(closes) else None)
        if close is None:
            continue
        adj = to_float(adjusted[idx] if idx < len(adjusted) else None)
        bars.append(
            YahooBar(
                ticker=job.ticker,
                bar_date=bar_date,
                source_id=source_id,
                open=to_float(opens[idx] if idx < len(opens) else None),
                high=to_float(highs[idx] if idx < len(highs) else None),
                low=to_float(lows[idx] if idx < len(lows) else None),
                close=close,
                adj_close=adj,
                volume=to_float(volumes[idx] if idx < len(volumes) else None),
                dividend_amount=dividends_by_date.get(bar_date),
                split_factor=split_by_date.get(bar_date),
                price_adjustment="adjusted_close" if adj is not None else "missing_adjusted_close",
                is_adjusted=1 if adj is not None else 0,
            )
        )
        last_bar_ts = bar_ts
    trimmed = without_unclosed_trailing_bar(bars, last_bar_ts, meta)
    if bars and not trimmed:
        return [], actions, meta, "only_in_progress_session_bar"
    bars = trimmed
    if not bars:
        return [], actions, meta, "no_price_bars"
    return bars, actions, meta, ""


def fetch_job(
    job: PriceJob,
    *,
    chart_url_template: str,
    start_date: date,
    end_date: date,
    source_id: str,
    cache_dir: Path,
    force_refresh: bool,
    user_agent: str,
    timeout_sec: float,
    max_retries: int,
    retry_status_codes: set[int],
    interval: str = "1d",
    events: str = "div,splits",
    include_adjusted_close: bool = True,
) -> FetchResult:
    endpoint = chart_url_template.format(ticker=job.fetch_ticker)
    query_params = {
        "period1": unix_timestamp(start_date),
        "period2": unix_timestamp(end_date + timedelta(days=1)),
        "interval": interval,
        "events": events,
        "includeAdjustedClose": "true" if include_adjusted_close else "false",
    }
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_path = cache_dir / cache_name(job.fetch_ticker, start_date, end_date)
    cache_status = "live"
    payload_text: str | None = None
    status_code = 0
    if cache_path.exists() and not force_refresh:
        cached_text = cache_path.read_text(encoding="utf-8", errors="replace")
        stale_reason = cached_payload_stale_reason(cached_text)
        if stale_reason is None:
            payload_text = cached_text
            status_code = 200
            cache_status = "cache"
        else:
            LOGGER.warning("Invalidating cached Yahoo payload for %s (%s); refetching.", job.fetch_ticker, stale_reason)
            try:
                cache_path.unlink()
            except OSError as exc:
                LOGGER.warning("Could not delete stale cache file %s: %s", cache_path, exc)
    if payload_text is None:
        status_code, payload_text = fetch_chart_payload(
            endpoint=endpoint,
            query_params=query_params,
            headers={"User-Agent": user_agent, "Accept": "application/json,text/plain,*/*"},
            timeout_sec=timeout_sec,
            max_retries=max_retries,
            retry_status_codes=retry_status_codes,
        )
        if status_code == 200:
            write_text_atomic(cache_path, payload_text)
    bars, actions, meta, error = parse_chart_result(job, payload_text, source_id)
    if status_code != 200 and not error:
        error = f"http_status_{status_code}"
    return FetchResult(job, endpoint, query_params, status_code, payload_text, bars, actions, meta, error, cache_status)


def start_ingestion_run(conn: Any, source_id: str) -> int:
    now = utc_now()
    cur = conn.execute(
        """
        INSERT INTO ingestion_runs(source_id, started_at, status, created_at)
        VALUES (?, ?, 'running', ?)
        """,
        (source_id, now, now),
    )
    return int(cur.lastrowid)


def finish_ingestion_run(conn: Any, ingestion_run_id: int, *, status: str, request_count: int, row_count: int, message: str) -> None:
    conn.execute(
        """
        UPDATE ingestion_runs
        SET completed_at = ?, status = ?, request_count = ?, row_count = ?, message = ?
        WHERE ingestion_run_id = ?
        """,
        (utc_now(), status, int(request_count), int(row_count), str(message or ""), int(ingestion_run_id)),
    )


def upsert_result(conn: Any, result: FetchResult, *, source_id: str, ingestion_run_id: int, request_asof: date) -> tuple[int, int]:
    now = utc_now()
    response_hash = hashlib.sha256(result.payload_text.encode("utf-8", errors="replace")).hexdigest()
    conn.execute(
        """
        INSERT INTO raw_api_responses(
            source_id, endpoint, query_params_json, request_time_utc, response_status,
            response_hash, asof_date, payload_text, ingestion_run_id, created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            source_id,
            result.endpoint,
            json.dumps(result.query_params, sort_keys=True),
            now,
            int(result.status_code),
            response_hash,
            request_asof.isoformat(),
            result.payload_text,
            ingestion_run_id,
            now,
        ),
    )
    for bar in result.bars:
        conn.execute(
            """
            INSERT INTO fact_price_ohlcv(
                ticker, bar_date, source_id, open, high, low, close, adj_close, volume,
                dividend, split_coefficient, dividend_amount, split_factor,
                price_adjustment, is_adjusted, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(ticker, bar_date, source_id) DO UPDATE SET
                open = excluded.open,
                high = excluded.high,
                low = excluded.low,
                close = excluded.close,
                adj_close = excluded.adj_close,
                volume = excluded.volume,
                -- Legacy dividend/split_coefficient columns are intentionally not
                -- updated: they are the backfill source for dividend_amount /
                -- split_factor and must not be force-NULLed on refetch.
                dividend_amount = COALESCE(excluded.dividend_amount, fact_price_ohlcv.dividend_amount),
                split_factor = COALESCE(excluded.split_factor, fact_price_ohlcv.split_factor),
                price_adjustment = excluded.price_adjustment,
                is_adjusted = excluded.is_adjusted,
                updated_at = excluded.updated_at
            """,
            (
                bar.ticker,
                bar.bar_date,
                bar.source_id,
                bar.open,
                bar.high,
                bar.low,
                bar.close,
                bar.adj_close,
                bar.volume,
                bar.dividend_amount,
                bar.split_factor,
                bar.price_adjustment,
                bar.is_adjusted,
                now,
                now,
            ),
        )
    for action in result.actions:
        conn.execute(
            """
            INSERT INTO fact_corporate_action(
                ticker, related_ticker, action_type, action_date, source_id,
                cash_amount, split_numerator, split_denominator, split_factor,
                raw_value, reason, notes, created_at, updated_at
            )
            VALUES (?, '', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(ticker, related_ticker, action_type, action_date) DO UPDATE SET
                source_id = excluded.source_id,
                cash_amount = excluded.cash_amount,
                split_numerator = excluded.split_numerator,
                split_denominator = excluded.split_denominator,
                split_factor = excluded.split_factor,
                raw_value = excluded.raw_value,
                reason = excluded.reason,
                notes = excluded.notes,
                updated_at = excluded.updated_at
            """,
            (
                action.ticker,
                action.action_type,
                action.action_date,
                action.source_id,
                action.cash_amount,
                action.split_numerator,
                action.split_denominator,
                action.split_factor,
                action.raw_value,
                "yahoo_chart_event",
                "",
                now,
                now,
            ),
        )
    latest_bar = result.bars[-1] if result.bars else None
    if latest_bar is not None:
        conn.execute(
            """
            INSERT INTO fact_market_snapshot(
                ticker, asof_date, source_id, market_cap, shares_outstanding,
                regular_market_price, currency, quote_type, exchange, source_timestamp,
                payload_json, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(ticker, asof_date, source_id) DO UPDATE SET
                market_cap = excluded.market_cap,
                shares_outstanding = excluded.shares_outstanding,
                regular_market_price = excluded.regular_market_price,
                currency = excluded.currency,
                quote_type = excluded.quote_type,
                exchange = excluded.exchange,
                source_timestamp = excluded.source_timestamp,
                payload_json = excluded.payload_json,
                updated_at = excluded.updated_at
            """,
            (
                result.job.ticker,
                latest_bar.bar_date,
                source_id,
                to_float(result.meta.get("marketCap")),
                to_float(result.meta.get("sharesOutstanding")),
                to_float(result.meta.get("regularMarketPrice")),
                str(result.meta.get("currency") or ""),
                str(result.meta.get("instrumentType") or ""),
                str(result.meta.get("exchangeName") or result.meta.get("fullExchangeName") or ""),
                str(result.meta.get("regularMarketTime") or ""),
                json.dumps(result.meta, sort_keys=True),
                now,
                now,
            ),
        )
    return len(result.bars), len(result.actions)


def add_data_quality_issue(conn: Any, *, ticker: str, source_id: str, issue_type: str, detail: str, severity: str = "warning", model_family: str) -> None:
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
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, ?)
        """,
        (now, severity, RUN_TYPE, model_family, ticker, company_id, source_id, issue_type, detail, now, now),
    )


def main() -> None:
    configure_utc_logging()
    args = parse_args()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    base_dir = config_path.parent
    db_path = args.db.expanduser().resolve() if args.db else resolve_path(cfg_get(config, "paths.database_path"), base_dir=base_dir)
    start = parse_date(args.start_date) or parse_date(cfg_get(config, "yahoo_price_ingestion.start_date")) or date(2010, 1, 1)
    end = parse_date(args.asof) or date.today()
    if end < start:
        raise ValueError(f"asof date {end} is before start date {start}")
    output_csv = args.output_csv.expanduser().resolve() if args.output_csv else resolve_path(cfg_get(config, "yahoo_price_ingestion.output_csv"), base_dir=base_dir)
    cache_dir = resolve_path(cfg_get(config, "yahoo_price_ingestion.cache_dir"), base_dir=base_dir)
    source_id = str(cfg_get(config, "yahoo_price_ingestion.source_id", SOURCE_ID_DEFAULT) or SOURCE_ID_DEFAULT)
    chart_url_template = str(cfg_get(config, "yahoo_price_ingestion.chart_url_template"))
    user_agent = expand_env_vars(cfg_get(config, "yahoo_price_ingestion.user_agent", "JL, Independent Research, jm.357@hotmail.com"))
    timeout_sec = float(cfg_get(config, "yahoo_price_ingestion.timeout_sec", 30.0))
    max_retries = int(cfg_get(config, "yahoo_price_ingestion.max_retries", 3))
    parallel_workers = max(1, int(cfg_get(config, "yahoo_price_ingestion.parallel_workers", 4)))
    retry_status_codes = int_set(cfg_get(config, "yahoo_price_ingestion.retry_status_codes"), {429, 500, 502, 503, 504})
    model_family = str(args.model_family or cfg_get(config, "industrials_universe.initial_subsector", "defense") or "defense").strip()
    if not model_family:
        raise ValueError("model_family cannot be empty")
    benchmark_tickers = parse_ticker_list(args.benchmark_tickers) or parse_ticker_list(cfg_get(config, "industrials_universe.benchmark_tickers", []))
    raw_start_overrides = cfg_get(config, "yahoo_price_ingestion.ticker_start_date_overrides", {}) or {}
    ticker_start_overrides: dict[str, date] = {}
    if isinstance(raw_start_overrides, dict):
        for raw_ticker, raw_date in raw_start_overrides.items():
            ticker = normalize_ticker(raw_ticker)
            override = parse_date(raw_date)
            if ticker and override is not None:
                ticker_start_overrides[ticker] = override
    ticker_filter = {normalize_ticker(x) for x in str(args.tickers or "").split(",") if normalize_ticker(x)}
    sqlite_timeout_sec = float(cfg_get(config, "runtime.sqlite_timeout_sec", 30.0))

    required_through_date = last_expected_trading_day(end)

    with closing(connect(db_path, timeout_sec=sqlite_timeout_sec)) as conn:
        init_db(conn)
        registry_path = resolve_path(cfg_get(config, "source_registry.path"), base_dir=base_dir)
        upsert_source_registry(conn, load_source_registry(registry_path))
        if source_id != SOURCE_ID_DEFAULT:
            raise ValueError(f"This script currently expects source_id={SOURCE_ID_DEFAULT}, got {source_id}")
        jobs = load_universe_jobs(conn, model_family=model_family, ticker_filter=ticker_filter, max_tickers=int(args.max_tickers), asof=end)
        jobs = append_benchmarks(jobs, benchmark_tickers, skip_benchmarks=bool(args.skip_benchmarks))
        if not jobs:
            raise ValueError("No industrials tickers found to fetch.")
        job_start_dates = {
            job.ticker: max(start, ticker_start_overrides.get(job.ticker, start), ticker_start_overrides.get(job.fetch_ticker, start))
            for job in jobs
        }
        skipped_report_rows: list[dict[str, Any]] = []
        if not bool(args.force_refresh):
            jobs_to_fetch: list[PriceJob] = []
            for job in jobs:
                coverage_row = existing_coverage_row(
                    conn,
                    job,
                    source_id=source_id,
                    required_start_date=job_start_dates[job.ticker],
                    required_through_date=required_through_date,
                )
                if coverage_row is None:
                    jobs_to_fetch.append(job)
                else:
                    skipped_report_rows.append(coverage_row)
            jobs = jobs_to_fetch
        run_id = start_run(conn, run_type=RUN_TYPE, input_path=config_path)
        with conn:
            ingestion_run_id = start_ingestion_run(conn, source_id)

    LOGGER.info(
        "Fetching Yahoo adjusted prices for model_family=%s tickers=%d skipped_already_current=%d from %s through %s",
        model_family,
        len(jobs),
        len(skipped_report_rows),
        start,
        end,
    )
    results: list[FetchResult] = []
    report_rows: list[dict[str, Any]] = list(skipped_report_rows)
    total_bars = 0
    total_actions = 0
    failures = 0
    try:
        if jobs:
            with ThreadPoolExecutor(max_workers=parallel_workers) as executor:
                futures = [
                    executor.submit(
                        fetch_job,
                        job,
                        chart_url_template=chart_url_template,
                        start_date=job_start_dates[job.ticker],
                        end_date=end,
                        source_id=source_id,
                        cache_dir=cache_dir,
                        force_refresh=bool(args.force_refresh),
                        user_agent=user_agent,
                        timeout_sec=timeout_sec,
                        max_retries=max_retries,
                        retry_status_codes=retry_status_codes,
                        interval=str(cfg_get(config, "yahoo_price_ingestion.interval", "1d") or "1d"),
                        events=str(cfg_get(config, "yahoo_price_ingestion.events", "div,splits") or "div,splits"),
                        include_adjusted_close=str(cfg_get(config, "yahoo_price_ingestion.include_adjusted_close", True)).lower() in {"1", "true", "yes", "y"},
                    )
                    for job in jobs
                ]
                for idx, future in enumerate(as_completed(futures), start=1):
                    result = future.result()
                    results.append(result)
                    status = "ok" if result.bars and not result.error else f"error={result.error}"
                    LOGGER.info("[%d/%d] %s fetch=%s bars=%d actions=%d %s", idx, len(jobs), result.job.ticker, result.job.fetch_ticker, len(result.bars), len(result.actions), status)

        with closing(connect(db_path, timeout_sec=sqlite_timeout_sec)) as conn:
            init_db(conn)
            with conn:
                processed = sorted({result.job.ticker for result in results})
                if processed:
                    placeholders = ",".join("?" for _ in processed)
                    # SC-12: family-scoped clear so this run never wipes another
                    # family's open issues for the same ticker/stage.
                    conn.execute(
                        f"DELETE FROM data_quality_issues WHERE stage = ? AND model_family = ? AND ticker IN ({placeholders})",
                        (RUN_TYPE, model_family, *processed),
                    )
                for result in sorted(results, key=lambda item: item.job.ticker):
                    bars_upserted, actions_upserted = upsert_result(conn, result, source_id=source_id, ingestion_run_id=ingestion_run_id, request_asof=end)
                    total_bars += bars_upserted
                    total_actions += actions_upserted
                    if result.error:
                        failures += 1
                        add_data_quality_issue(conn, ticker=result.job.ticker, source_id=source_id, issue_type="yahoo_price_fetch_failed", detail=result.error, severity="error", model_family=model_family)
                    elif not result.bars:
                        failures += 1
                        add_data_quality_issue(conn, ticker=result.job.ticker, source_id=source_id, issue_type="no_yahoo_price_bars", detail="Yahoo returned no usable daily bars.", severity="error", model_family=model_family)
                    first_bar = result.bars[0] if result.bars else None
                    latest_bar = result.bars[-1] if result.bars else None
                    report_rows.append(
                        {
                            "ticker": result.job.ticker,
                            "fetch_ticker": result.job.fetch_ticker,
                            "company_name": result.job.company_name,
                            "is_benchmark": int(result.job.is_benchmark),
                            "alias_routed": int(result.job.fetch_ticker != result.job.ticker),
                            "source_id": source_id,
                            "status": "success" if result.bars and not result.error else "failed",
                            "bars_upserted": bars_upserted,
                            "actions_upserted": actions_upserted,
                            "first_bar_date": first_bar.bar_date if first_bar else "",
                            "last_bar_date": latest_bar.bar_date if latest_bar else "",
                            "latest_close": latest_bar.close if latest_bar else "",
                            "latest_adj_close": latest_bar.adj_close if latest_bar else "",
                            "price_adjustment": latest_bar.price_adjustment if latest_bar else "",
                            "is_adjusted": latest_bar.is_adjusted if latest_bar else 0,
                            "review_reason": result.error,
                        }
                    )
                status = "partial" if failures else "success"
                if failures and not bool(args.allow_partial):
                    status = "failed"
                finish_ingestion_run(conn, ingestion_run_id, status=status, request_count=len(results), row_count=total_bars, message=f"bars={total_bars} actions={total_actions} failures={failures}")
                finish_run(conn, run_id=run_id, status=status, row_count=total_bars, message=f"tickers={len(results)} bars={total_bars} actions={total_actions} failures={failures}")
        write_csv_atomic(output_csv, FIELDNAMES, report_rows)
    except BaseException as exc:
        LOGGER.exception("Yahoo price sync failed; finalizing run bookkeeping as failed.")
        failure_message = f"{type(exc).__name__}: {exc}"[:500]
        try:
            with closing(connect(db_path, timeout_sec=sqlite_timeout_sec)) as fail_conn:
                with fail_conn:
                    finish_ingestion_run(fail_conn, ingestion_run_id, status="failed", request_count=len(results), row_count=total_bars, message=failure_message)
                    finish_run(fail_conn, run_id=run_id, status="failed", row_count=total_bars, message=failure_message)
        except Exception:
            LOGGER.exception("Could not record failed status for run_id=%s ingestion_run_id=%s", run_id, ingestion_run_id)
        raise
    LOGGER.info("Wrote Yahoo price coverage report: %s", output_csv)
    LOGGER.info("Yahoo price sync complete: tickers=%d bars=%d actions=%d failures=%d", len(results), total_bars, total_actions, failures)
    if failures and not bool(args.allow_partial):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
