#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import logging
import math
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import requests  # type: ignore[reportMissingModuleSource]


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from technology.core.config import cfg_get, expand_env_vars, load_yaml, resolve_path  # noqa: E402
from technology.core.db import connect, finish_run, init_db, start_run, utc_now  # noqa: E402
from technology.core.logging_utils import configure_utc_logging  # noqa: E402
from technology.core.source_registry import load_source_registry, upsert_source_registry  # noqa: E402
from technology.core.text_norm import normalize_ticker  # noqa: E402


LOGGER = logging.getLogger("sync_technology_yahoo_adjusted_prices")
DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
RUN_TYPE = "sync_technology_yahoo_adjusted_prices"
SOURCE_ID_DEFAULT = "yahoo_finance_adjusted"
FIELDNAMES = [
    "ticker",
    "company_name",
    "is_benchmark",
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
    parser = argparse.ArgumentParser(description="Sync Yahoo adjusted daily OHLCV for the technology universe.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--start-date", default="")
    parser.add_argument("--asof", default="", help="Fetch bars through this YYYY-MM-DD date. Defaults to today.")
    parser.add_argument("--tickers", default="", help="Optional comma-separated ticker subset.")
    parser.add_argument("--max-tickers", type=int, default=0)
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


def to_int(raw: object, default: int = 0) -> int:
    try:
        return int(raw)
    except (TypeError, ValueError):
        return int(default)


def int_set(raw: object, default: set[int]) -> set[int]:
    values = raw if isinstance(raw, list) else list(default)
    out: set[int] = set()
    for value in values:
        try:
            out.add(int(value))
        except (TypeError, ValueError):
            LOGGER.warning("Ignoring invalid retry status code: %r", value)
    return out or set(default)


def cache_name(ticker: str, start_date: date, end_date: date) -> str:
    safe = "".join(ch for ch in normalize_ticker(ticker) if ch.isalnum() or ch in "._-")
    return f"{safe}_{start_date.isoformat()}_{end_date.isoformat()}.json"


def load_universe_jobs(conn: Any, *, ticker_filter: set[str], max_tickers: int) -> list[PriceJob]:
    rows = conn.execute(
        """
        SELECT ticker, company_name
        FROM dim_company
        WHERE is_active = 1
        ORDER BY ticker
        """
    ).fetchall()
    out: list[PriceJob] = []
    seen: set[str] = set()
    for row in rows:
        ticker = normalize_ticker(row["ticker"])
        if not ticker or ticker in seen:
            continue
        if ticker_filter and ticker not in ticker_filter:
            continue
        out.append(PriceJob(ticker=ticker, company_name=str(row["company_name"] or "")))
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
        out.append(PriceJob(ticker=ticker, company_name=ticker, is_benchmark=True))
        seen.add(ticker)
    return out


def fetch_chart_payload(
    job: PriceJob,
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
    result = results[0] if isinstance(results[0], dict) else {}
    meta = result.get("meta") if isinstance(result.get("meta"), dict) else {}
    timestamps = result.get("timestamp") or []
    indicators = result.get("indicators") if isinstance(result.get("indicators"), dict) else {}
    quote = (indicators.get("quote") or [{}])[0]
    adjclose = (indicators.get("adjclose") or [{}])[0]
    if not isinstance(quote, dict):
        quote = {}
    if not isinstance(adjclose, dict):
        adjclose = {}
    opens = quote.get("open") or []
    highs = quote.get("high") or []
    lows = quote.get("low") or []
    closes = quote.get("close") or []
    volumes = quote.get("volume") or []
    adjusted = adjclose.get("adjclose") or []

    dividends_by_date: dict[str, float] = {}
    split_by_date: dict[str, float] = {}
    actions: list[CorporateAction] = []
    events = result.get("events") if isinstance(result.get("events"), dict) else {}
    dividends = events.get("dividends") if isinstance(events.get("dividends"), dict) else {}
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
    splits = events.get("splits") if isinstance(events.get("splits"), dict) else {}
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
    for idx, raw_ts in enumerate(timestamps):
        try:
            bar_date = datetime.fromtimestamp(int(raw_ts), tz=timezone.utc).date().isoformat()
        except (TypeError, ValueError, OSError):
            continue
        close = to_float(closes[idx] if idx < len(closes) else None)
        if close is None:
            continue
        adj = to_float(adjusted[idx] if idx < len(adjusted) else None)
        price_adjustment = "adjusted_close" if adj is not None else "missing_adjusted_close"
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
                price_adjustment=price_adjustment,
                is_adjusted=1 if adj is not None else 0,
            )
        )
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
    endpoint = chart_url_template.format(ticker=job.ticker)
    query_params = {
        "period1": unix_timestamp(start_date),
        "period2": unix_timestamp(end_date + timedelta(days=1)),
        "interval": interval,
        "events": events,
        "includeAdjustedClose": "true" if include_adjusted_close else "false",
    }
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_path = cache_dir / cache_name(job.ticker, start_date, end_date)
    cache_status = "live"
    if cache_path.exists() and not force_refresh:
        payload_text = cache_path.read_text(encoding="utf-8", errors="replace")
        status_code = 200
        cache_status = "cache"
    else:
        status_code, payload_text = fetch_chart_payload(
            job,
            endpoint=endpoint,
            query_params=query_params,
            headers={"User-Agent": user_agent, "Accept": "application/json,text/plain,*/*"},
            timeout_sec=timeout_sec,
            max_retries=max_retries,
            retry_status_codes=retry_status_codes,
        )
        if status_code == 200:
            cache_path.write_text(payload_text, encoding="utf-8")
    bars, actions, meta, error = parse_chart_result(job, payload_text, source_id)
    if status_code != 200 and not error:
        error = f"http_status_{status_code}"
    return FetchResult(
        job=job,
        endpoint=endpoint,
        query_params=query_params,
        status_code=status_code,
        payload_text=payload_text,
        bars=bars,
        actions=actions,
        meta=meta,
        error=error,
        cache_status=cache_status,
    )


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


def upsert_result(conn: Any, result: FetchResult, *, ingestion_run_id: int) -> tuple[int, int]:
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
            SOURCE_ID_DEFAULT,
            result.endpoint,
            json.dumps(result.query_params, sort_keys=True),
            now,
            int(result.status_code),
            response_hash,
            date.today().isoformat(),
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
                dividend_amount, split_factor, price_adjustment, is_adjusted, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(ticker, bar_date, source_id) DO UPDATE SET
                open = excluded.open,
                high = excluded.high,
                low = excluded.low,
                close = excluded.close,
                adj_close = excluded.adj_close,
                volume = excluded.volume,
                dividend_amount = excluded.dividend_amount,
                split_factor = excluded.split_factor,
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
                ticker, action_date, source_id, action_type, cash_amount, split_numerator,
                split_denominator, split_factor, raw_value, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(ticker, action_date, source_id, action_type) DO UPDATE SET
                cash_amount = excluded.cash_amount,
                split_numerator = excluded.split_numerator,
                split_denominator = excluded.split_denominator,
                split_factor = excluded.split_factor,
                raw_value = excluded.raw_value,
                updated_at = excluded.updated_at
            """,
            (
                action.ticker,
                action.action_date,
                action.source_id,
                action.action_type,
                action.cash_amount,
                action.split_numerator,
                action.split_denominator,
                action.split_factor,
                action.raw_value,
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
                SOURCE_ID_DEFAULT,
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


def write_report(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDNAMES, extrasaction="ignore", lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def add_data_quality_issue(conn: Any, ticker: str, issue_type: str, detail: str, severity: str = "warning") -> None:
    now = utc_now()
    row = conn.execute("SELECT company_id FROM dim_company WHERE ticker = ?", (ticker,)).fetchone()
    company_id = int(row["company_id"]) if row is not None else None
    conn.execute(
        """
        INSERT INTO data_quality_issues(
            detected_at, severity, stage, ticker, company_id, source_id, issue_type,
            issue_detail, resolution_status, created_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, ?)
        """,
        (now, severity, RUN_TYPE, ticker, company_id, SOURCE_ID_DEFAULT, issue_type, detail, now, now),
    )


def main() -> None:
    configure_utc_logging()
    args = parse_args()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    base_dir = config_path.parent
    db_path = args.db.expanduser().resolve() if args.db else resolve_path(cfg_get(config, "paths.database_path"), base_dir=base_dir)
    start = parse_date(args.start_date) or parse_date(cfg_get(config, "yahoo_price_ingestion.start_date")) or date(2016, 1, 1)
    end = parse_date(args.asof) or date.today()
    if end < start:
        raise ValueError(f"asof date {end} is before start date {start}")
    output_csv = (
        args.output_csv.expanduser().resolve()
        if args.output_csv
        else resolve_path(cfg_get(config, "yahoo_price_ingestion.output_csv"), base_dir=base_dir)
    )
    cache_dir = resolve_path(cfg_get(config, "yahoo_price_ingestion.cache_dir"), base_dir=base_dir)
    source_id = str(cfg_get(config, "yahoo_price_ingestion.source_id", SOURCE_ID_DEFAULT) or SOURCE_ID_DEFAULT)
    chart_url_template = str(cfg_get(config, "yahoo_price_ingestion.chart_url_template"))
    user_agent = expand_env_vars(cfg_get(config, "yahoo_price_ingestion.user_agent", "JL, Independent Research, jm.357@hotmail.com"))
    timeout_sec = float(cfg_get(config, "yahoo_price_ingestion.timeout_sec", 30.0))
    max_retries = int(cfg_get(config, "yahoo_price_ingestion.max_retries", 3))
    parallel_workers = max(1, int(cfg_get(config, "yahoo_price_ingestion.parallel_workers", 4)))
    retry_status_codes = int_set(cfg_get(config, "yahoo_price_ingestion.retry_status_codes"), {429, 500, 502, 503, 504})
    benchmark_tickers = [str(x) for x in cfg_get(config, "technology_universe.benchmark_tickers", [])]
    ticker_filter = {normalize_ticker(x) for x in str(args.tickers or "").split(",") if normalize_ticker(x)}

    with connect(db_path, timeout_sec=float(cfg_get(config, "runtime.sqlite_timeout_sec", 30.0))) as conn:
        init_db(conn)
        source_registry_path = resolve_path(cfg_get(config, "source_registry.path"), base_dir=base_dir)
        upsert_source_registry(conn, load_source_registry(source_registry_path))
        jobs = load_universe_jobs(conn, ticker_filter=ticker_filter, max_tickers=int(args.max_tickers))
        jobs = append_benchmarks(jobs, benchmark_tickers, skip_benchmarks=bool(args.skip_benchmarks))
        if source_id != SOURCE_ID_DEFAULT:
            raise ValueError(f"This script currently expects source_id={SOURCE_ID_DEFAULT}, got {source_id}")
        if not jobs:
            raise ValueError("No technology tickers found to fetch.")
        run_id = start_run(conn, run_type=RUN_TYPE, input_path=config_path)
        ingestion_run_id = start_ingestion_run(conn, SOURCE_ID_DEFAULT)

    LOGGER.info("Fetching Yahoo adjusted prices for %d tickers from %s through %s", len(jobs), start, end)
    results: list[FetchResult] = []
    with ThreadPoolExecutor(max_workers=parallel_workers) as executor:
        futures = [
            executor.submit(
                fetch_job,
                job,
                chart_url_template=chart_url_template,
                start_date=start,
                end_date=end,
                source_id=SOURCE_ID_DEFAULT,
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
            LOGGER.info("[%d/%d] %s bars=%d actions=%d %s", idx, len(jobs), result.job.ticker, len(result.bars), len(result.actions), status)

    report_rows: list[dict[str, Any]] = []
    total_bars = 0
    total_actions = 0
    failures = 0
    with connect(db_path, timeout_sec=float(cfg_get(config, "runtime.sqlite_timeout_sec", 30.0))) as conn:
        init_db(conn)
        with conn:
            processed = sorted({result.job.ticker for result in results})
            if ticker_filter or int(args.max_tickers) > 0:
                # Subset run: only clear issues for the tickers actually refreshed.
                placeholders = ",".join("?" for _ in processed)
                conn.execute(f"DELETE FROM data_quality_issues WHERE stage = ? AND ticker IN ({placeholders})", (RUN_TYPE, *processed))
            else:
                conn.execute("DELETE FROM data_quality_issues WHERE stage = ?", (RUN_TYPE,))
            for result in sorted(results, key=lambda item: item.job.ticker):
                bars_upserted, actions_upserted = upsert_result(conn, result, ingestion_run_id=ingestion_run_id)
                total_bars += bars_upserted
                total_actions += actions_upserted
                if result.error:
                    failures += 1
                    add_data_quality_issue(conn, result.job.ticker, "yahoo_price_fetch_failed", result.error, severity="error")
                elif not result.bars:
                    failures += 1
                    add_data_quality_issue(conn, result.job.ticker, "no_yahoo_price_bars", "Yahoo returned no usable daily bars.", severity="error")
                first_bar = result.bars[0] if result.bars else None
                latest_bar = result.bars[-1] if result.bars else None
                report_rows.append(
                    {
                        "ticker": result.job.ticker,
                        "company_name": result.job.company_name,
                        "is_benchmark": int(result.job.is_benchmark),
                        "source_id": SOURCE_ID_DEFAULT,
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
            finish_ingestion_run(
                conn,
                ingestion_run_id,
                status=status,
                request_count=len(results),
                row_count=total_bars,
                message=f"bars={total_bars} actions={total_actions} failures={failures}",
            )
            finish_run(
                conn,
                run_id=run_id,
                status=status,
                row_count=total_bars,
                message=f"tickers={len(results)} bars={total_bars} actions={total_actions} failures={failures}",
            )
    write_report(output_csv, report_rows)
    LOGGER.info("Wrote Yahoo price coverage report: %s", output_csv)
    LOGGER.info("Yahoo price sync complete: tickers=%d bars=%d actions=%d failures=%d", len(results), total_bars, total_actions, failures)
    if failures and not bool(args.allow_partial):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
