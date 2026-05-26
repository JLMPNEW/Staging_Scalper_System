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
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import requests


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from med_devices.core.config import cfg_get, load_yaml, resolve_path  # noqa: E402
from med_devices.core.db import connect, finish_run, init_db, start_run, utc_now  # noqa: E402
from med_devices.core.logging_utils import configure_utc_logging  # noqa: E402
from med_devices.core.source_registry import load_source_registry, upsert_source_registry  # noqa: E402
from med_devices.core.text_norm import normalize_ticker  # noqa: E402


LOGGER = logging.getLogger("sync_med_device_yahoo_adjusted_prices")
DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
DEFAULT_YAHOO_CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
DEFAULT_SOURCE_ID = "yahoo_finance_backup"
DEFAULT_USER_AGENT = "JL, Independent Research, jm.357@hotmail.com"
DEFAULT_RETRY_STATUS_CODES = {429, 500, 502, 503, 504}
FIELDNAMES = [
    "ticker",
    "company_name",
    "source_id",
    "status",
    "bars_upserted",
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
    adj_close: float
    volume: float | None
    dividend_amount: float | None
    split_factor: float | None
    price_adjustment: str
    is_adjusted: int


@dataclass(frozen=True)
class YahooPolicy:
    source_id: str
    chart_url_template: str
    interval: str
    events: str
    include_adjusted_close: bool
    timeout_sec: float
    max_retries: int
    sleep_sec: float
    user_agent: str
    retry_status_codes: set[int]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Sync Yahoo adjusted daily prices for the med-devices universe.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--input", type=Path, default=None)
    parser.add_argument("--output-csv", type=Path, default=None)
    parser.add_argument("--asof", type=str, default="", help="Fetch bars through this YYYY-MM-DD date. Defaults to today.")
    parser.add_argument("--start-date", type=str, default="", help="Override configured historical start date.")
    parser.add_argument("--tickers", type=str, default="", help="Optional comma-separated ticker subset.")
    parser.add_argument("--max-tickers", type=int, default=0, help="Smoke-test limit. 0 means all.")
    parser.add_argument("--allow-partial", action="store_true", help="Exit 0 even when some tickers fail.")
    parser.add_argument("--skip-benchmarks", action="store_true", help="Do not append configured benchmark tickers.")
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
    if raw is None:
        return None
    try:
        value = float(str(raw).strip())
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def as_bool(raw: object, *, default: bool = False) -> bool:
    if raw is None:
        return default
    text = str(raw).strip().lower()
    if text in {"1", "true", "yes", "y", "on"}:
        return True
    if text in {"0", "false", "no", "n", "off"}:
        return False
    return default


def int_set(raw: object, default: set[int]) -> set[int]:
    values = raw if isinstance(raw, list) else list(default)
    out: set[int] = set()
    for value in values:
        try:
            out.add(int(value))
        except (TypeError, ValueError):
            LOGGER.warning("Ignoring invalid Yahoo retry status code: %r", value)
    return out or set(default)


def unix_timestamp(day: date) -> int:
    return int(datetime(day.year, day.month, day.day, tzinfo=timezone.utc).timestamp())


def read_csv_flexible(path: Path) -> list[dict[str, str]]:
    last_error: Exception | None = None
    for encoding in ("utf-8-sig", "utf-8", "cp1252"):
        try:
            with path.open("r", encoding=encoding, newline="") as handle:
                reader = csv.DictReader(handle)
                if reader.fieldnames is None:
                    raise ValueError(f"CSV has no header: {path}")
                return [{str(key): str(value or "") for key, value in row.items()} for row in reader]
        except UnicodeDecodeError as exc:
            last_error = exc
            continue
    raise ValueError(f"Could not decode CSV {path}: {last_error}")


def row_get(row: dict[str, str], *keys: str) -> str:
    lowered = {str(key).strip().lower(): str(value or "") for key, value in row.items()}
    for key in keys:
        value = row.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
        value = lowered.get(key.lower())
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def read_jobs(input_csv: Path, *, ticker_filter: set[str], max_tickers: int) -> list[PriceJob]:
    rows = read_csv_flexible(input_csv)
    out: list[PriceJob] = []
    seen: set[str] = set()
    for row in rows:
        ticker = normalize_ticker(row_get(row, "Name", "Ticker", "ticker", "MatchedTicker"))
        if not ticker or ticker in seen:
            continue
        if ticker_filter and ticker not in ticker_filter:
            continue
        out.append(PriceJob(ticker=ticker, company_name=row_get(row, "Company_Name", "CompanyName")))
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


def yahoo_policy(config: dict[str, Any]) -> YahooPolicy:
    return YahooPolicy(
        source_id=str(cfg_get(config, "yahoo_price_ingestion.source_id", DEFAULT_SOURCE_ID) or DEFAULT_SOURCE_ID).strip(),
        chart_url_template=str(
            cfg_get(config, "yahoo_price_ingestion.chart_url_template", DEFAULT_YAHOO_CHART_URL)
            or DEFAULT_YAHOO_CHART_URL
        ),
        interval=str(cfg_get(config, "yahoo_price_ingestion.interval", "1d") or "1d"),
        events=str(cfg_get(config, "yahoo_price_ingestion.events", "div,splits") or "div,splits"),
        include_adjusted_close=as_bool(
            cfg_get(config, "yahoo_price_ingestion.include_adjusted_close", True),
            default=True,
        ),
        timeout_sec=float(cfg_get(config, "yahoo_price_ingestion.timeout_sec", 30.0)),
        max_retries=int(cfg_get(config, "yahoo_price_ingestion.max_retries", 3)),
        sleep_sec=float(cfg_get(config, "yahoo_price_ingestion.request_sleep_sec", 0.15)),
        user_agent=str(cfg_get(config, "yahoo_price_ingestion.user_agent", DEFAULT_USER_AGENT) or DEFAULT_USER_AGENT),
        retry_status_codes=int_set(
            cfg_get(config, "yahoo_price_ingestion.retry_status_codes", list(DEFAULT_RETRY_STATUS_CODES)),
            DEFAULT_RETRY_STATUS_CODES,
        ),
    )


def fetch_chart_payload(
    ticker: str,
    *,
    start_date: date,
    asof_date: date,
    policy: YahooPolicy,
) -> tuple[int, str, dict[str, Any]]:
    params = {
        "period1": unix_timestamp(start_date),
        "period2": unix_timestamp(asof_date + timedelta(days=1)),
        "interval": policy.interval,
        "events": policy.events,
        "includeAdjustedClose": str(policy.include_adjusted_close).lower(),
    }
    url = policy.chart_url_template.format(ticker=ticker)
    last_status = 0
    last_text = ""
    last_payload: dict[str, Any] = {}
    for attempt in range(max(1, policy.max_retries)):
        response = requests.get(
            url,
            params=params,
            timeout=policy.timeout_sec,
            headers={"User-Agent": policy.user_agent, "Accept": "application/json,text/plain,*/*"},
        )
        last_status = int(response.status_code)
        last_text = response.text
        try:
            last_payload = response.json()
        except ValueError:
            last_payload = {}
        if response.status_code == 200:
            return last_status, last_text, last_payload
        if response.status_code in policy.retry_status_codes and attempt < policy.max_retries - 1:
            time.sleep(max(0.1, policy.sleep_sec) * (attempt + 1) * 2)
            continue
        return last_status, last_text, last_payload
    return last_status, last_text, last_payload


def event_date(raw_ts: object) -> str:
    try:
        ts = int(str(raw_ts))
    except (TypeError, ValueError):
        return ""
    return datetime.fromtimestamp(ts, timezone.utc).date().isoformat()


def parse_events(result: dict[str, Any]) -> tuple[dict[str, float], dict[str, float]]:
    raw_events = result.get("events")
    events: dict[str, Any] = raw_events if isinstance(raw_events, dict) else {}
    dividends: dict[str, float] = {}
    splits: dict[str, float] = {}
    raw_dividends = events.get("dividends")
    dividend_events: dict[str, Any] = raw_dividends if isinstance(raw_dividends, dict) else {}
    for raw_ts, event in dividend_events.items():
        event_payload = event if isinstance(event, dict) else {}
        day = event_date(raw_ts)
        amount = to_float(event_payload.get("amount"))
        if day and amount is not None:
            dividends[day] = amount
    raw_splits = events.get("splits")
    split_events: dict[str, Any] = raw_splits if isinstance(raw_splits, dict) else {}
    for raw_ts, event in split_events.items():
        event_payload = event if isinstance(event, dict) else {}
        day = event_date(raw_ts)
        numerator = to_float(event_payload.get("numerator"))
        denominator = to_float(event_payload.get("denominator"))
        ratio = (numerator / denominator) if numerator and denominator else None
        if day and ratio is not None:
            splits[day] = ratio
    return dividends, splits


def parse_bars(ticker: str, payload: dict[str, Any], *, source_id: str) -> list[YahooBar]:
    raw_chart = payload.get("chart")
    chart: dict[str, Any] = raw_chart if isinstance(raw_chart, dict) else {}
    raw_results = chart.get("result")
    results: list[Any] = raw_results if isinstance(raw_results, list) else []
    if not results:
        return []
    first_result = results[0]
    if not isinstance(first_result, dict):
        return []
    result: dict[str, Any] = first_result
    raw_timestamps = result.get("timestamp")
    timestamps: list[Any] = raw_timestamps if isinstance(raw_timestamps, list) else []
    raw_indicators = result.get("indicators")
    indicators: dict[str, Any] = raw_indicators if isinstance(raw_indicators, dict) else {}
    raw_quote_rows = indicators.get("quote")
    quote_rows: list[Any] = raw_quote_rows if isinstance(raw_quote_rows, list) else []
    quote: dict[str, Any] = quote_rows[0] if quote_rows and isinstance(quote_rows[0], dict) else {}
    raw_adj_rows = indicators.get("adjclose")
    adj_rows: list[Any] = raw_adj_rows if isinstance(raw_adj_rows, list) else []
    adj: dict[str, Any] = adj_rows[0] if adj_rows and isinstance(adj_rows[0], dict) else {}
    raw_adj_closes = adj.get("adjclose")
    adj_closes: list[Any] = raw_adj_closes if isinstance(raw_adj_closes, list) else []
    dividends, splits = parse_events(result)

    def list_field(mapping: dict[str, Any], field: str) -> list[Any]:
        raw_values = mapping.get(field)
        return raw_values if isinstance(raw_values, list) else []

    close_values = list_field(quote, "close")

    out: list[YahooBar] = []
    for idx, raw_ts in enumerate(timestamps):
        try:
            bar_day = datetime.fromtimestamp(int(raw_ts), timezone.utc).date().isoformat()
        except (TypeError, ValueError, OSError):
            continue
        raw_close = to_float(close_values[idx] if idx < len(close_values) else None)
        adj_close = to_float(adj_closes[idx] if idx < len(adj_closes) else None)
        if raw_close is None or raw_close <= 0:
            continue
        price_adjustment = "adjusted" if adj_close is not None else "raw"
        final_close = adj_close if adj_close is not None else raw_close
        if final_close <= 0:
            continue
        factor = final_close / raw_close if raw_close else 1.0

        def adjusted_value(field: str) -> float | None:
            values = list_field(quote, field)
            raw_value = to_float(values[idx] if idx < len(values) else None)
            return raw_value * factor if raw_value is not None else None

        volume_values = list_field(quote, "volume")
        out.append(
            YahooBar(
                ticker=ticker,
                bar_date=bar_day,
                source_id=source_id,
                open=adjusted_value("open"),
                high=adjusted_value("high"),
                low=adjusted_value("low"),
                close=final_close,
                adj_close=final_close,
                volume=to_float(volume_values[idx] if idx < len(volume_values) else None),
                dividend_amount=dividends.get(bar_day),
                split_factor=splits.get(bar_day),
                price_adjustment=price_adjustment,
                is_adjusted=1 if price_adjustment == "adjusted" else 0,
            )
        )
    return out


def ensure_source_registry(conn: Any, config: dict[str, Any], base_dir: Path, source_id: str) -> None:
    row = conn.execute("SELECT 1 FROM source_registry WHERE source_id = ? LIMIT 1", (source_id,)).fetchone()
    if row is not None:
        return
    registry_path = resolve_path(cfg_get(config, "source_registry.path"), base_dir=base_dir)
    upsert_source_registry(conn, load_source_registry(registry_path))
    row = conn.execute("SELECT 1 FROM source_registry WHERE source_id = ? LIMIT 1", (source_id,)).fetchone()
    if row is None:
        raise ValueError(f"Source registry did not include required source_id: {source_id}")


def start_ingestion_run(conn: Any, source_id: str) -> int:
    now = utc_now()
    cur = conn.execute(
        """
        INSERT INTO ingestion_runs(source_id, started_at, status, created_at)
        VALUES (?, ?, 'running', ?)
        """,
        (source_id, now, now),
    )
    if cur.lastrowid is None:
        raise RuntimeError("Could not create ingestion_runs row")
    return int(cur.lastrowid)


def finish_ingestion_run(
    conn: Any,
    *,
    ingestion_run_id: int,
    status: str,
    request_count: int,
    row_count: int,
    message: str,
) -> None:
    conn.execute(
        """
        UPDATE ingestion_runs
        SET completed_at = ?, status = ?, request_count = ?, row_count = ?, message = ?
        WHERE ingestion_run_id = ?
        """,
        (utc_now(), status, request_count, row_count, message, ingestion_run_id),
    )


def store_raw_response(
    conn: Any,
    *,
    source_id: str,
    endpoint: str,
    ticker: str,
    query_params: dict[str, Any],
    response_status: int,
    payload_text: str,
    asof_date: str,
    ingestion_run_id: int,
) -> None:
    now = utc_now()
    response_hash = hashlib.sha256(payload_text.encode("utf-8", errors="replace")).hexdigest()
    conn.execute(
        """
        INSERT OR IGNORE INTO raw_api_responses(
            source_id, endpoint, query_params_json, request_time_utc, response_status,
            response_hash, asof_date, payload_text, ingestion_run_id, created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            source_id,
            endpoint,
            json.dumps(query_params, ensure_ascii=True, sort_keys=True),
            now,
            response_status,
            response_hash,
            asof_date,
            payload_text,
            ingestion_run_id,
            now,
        ),
    )


def upsert_price_bars(conn: Any, bars: list[YahooBar]) -> int:
    if not bars:
        return 0
    now = utc_now()
    fields = [
        "ticker",
        "bar_date",
        "source_id",
        "open",
        "high",
        "low",
        "close",
        "adj_close",
        "volume",
        "dividend_amount",
        "split_factor",
        "price_adjustment",
        "is_adjusted",
    ]
    conn.executemany(
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
        [tuple(getattr(bar, field) for field in fields) + (now, now) for bar in bars],
    )
    return len(bars)


def coverage_row(job: PriceJob, source_id: str, status: str, bars: list[YahooBar], review_reason: str = "") -> dict[str, Any]:
    if not bars:
        return {
            "ticker": job.ticker,
            "company_name": job.company_name,
            "source_id": source_id,
            "status": status,
            "bars_upserted": 0,
            "review_reason": review_reason,
        }
    ordered = sorted(bars, key=lambda bar: bar.bar_date)
    latest = ordered[-1]
    return {
        "ticker": job.ticker,
        "company_name": job.company_name,
        "source_id": source_id,
        "status": status,
        "bars_upserted": len(ordered),
        "first_bar_date": ordered[0].bar_date,
        "last_bar_date": latest.bar_date,
        "latest_close": latest.close,
        "latest_adj_close": latest.adj_close,
        "price_adjustment": latest.price_adjustment,
        "is_adjusted": latest.is_adjusted,
        "review_reason": review_reason,
    }


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDNAMES, extrasaction="ignore", lineterminator="\n")
        writer.writeheader()
        writer.writerows([{field: row.get(field, "") for field in FIELDNAMES} for row in rows])


def main() -> None:
    configure_utc_logging()
    args = parse_args()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    base_dir = config_path.parent
    db_path = args.db.expanduser().resolve() if args.db else resolve_path(cfg_get(config, "paths.database_path"), base_dir=base_dir)
    policy = yahoo_policy(config)
    input_csv = (
        args.input.expanduser().resolve()
        if args.input
        else resolve_path(
            cfg_get(config, "yahoo_price_ingestion.input_csv", cfg_get(config, "med_devices_universe.seed_csv")),
            base_dir=base_dir,
        )
    )
    output_csv = (
        args.output_csv.expanduser().resolve()
        if args.output_csv
        else resolve_path(
            cfg_get(config, "yahoo_price_ingestion.output_csv", "../output/med_devices_reports/med_device_yahoo_adjusted_price_coverage.csv"),
            base_dir=base_dir,
        )
    )
    configured_start = parse_date(args.start_date or cfg_get(config, "yahoo_price_ingestion.start_date", "2000-01-01"))
    if configured_start is None:
        raise ValueError("Invalid Yahoo start date")
    asof_date = parse_date(args.asof) if args.asof else datetime.now(timezone.utc).date()
    if asof_date is None:
        raise ValueError(f"Invalid --asof date: {args.asof}")
    commit_every = max(1, int(cfg_get(config, "yahoo_price_ingestion.commit_every_tickers", 25)))
    include_benchmarks = str(cfg_get(config, "yahoo_price_ingestion.include_benchmark_tickers", True)).strip().lower() not in {
        "0",
        "false",
        "no",
    }
    ticker_filter = {normalize_ticker(value) for value in str(args.tickers or "").split(",") if normalize_ticker(value)}
    jobs = read_jobs(input_csv, ticker_filter=ticker_filter, max_tickers=int(args.max_tickers))
    benchmark_tickers = list(cfg_get(config, "med_devices_universe.benchmark_tickers", []) or [])
    jobs = append_benchmarks(jobs, benchmark_tickers if include_benchmarks else [], skip_benchmarks=args.skip_benchmarks)
    if not jobs:
        raise ValueError(f"No tickers selected from {input_csv}")

    LOGGER.info(
        "Yahoo adjusted price sync starting: db=%s input=%s jobs=%d source=%s start=%s asof=%s",
        db_path,
        input_csv,
        len(jobs),
        policy.source_id,
        configured_start.isoformat(),
        asof_date.isoformat(),
    )
    coverage_rows: list[dict[str, Any]] = []
    total_bars = 0
    failed_tickers: list[str] = []
    request_count = 0

    with connect(db_path, timeout_sec=float(cfg_get(config, "runtime.sqlite_timeout_sec", 30.0))) as conn:
        init_db(conn)
        ensure_source_registry(conn, config, base_dir, policy.source_id)
        run_id = start_run(conn, run_type="sync_med_device_yahoo_adjusted_prices", input_path=input_csv)
        ingestion_run_id = start_ingestion_run(conn, policy.source_id)
        try:
            for idx, job in enumerate(jobs, start=1):
                query_params = {
                    "period1": unix_timestamp(configured_start),
                    "period2": unix_timestamp(asof_date + timedelta(days=1)),
                    "interval": policy.interval,
                    "events": policy.events,
                    "includeAdjustedClose": str(policy.include_adjusted_close).lower(),
                }
                endpoint = policy.chart_url_template.format(ticker=job.ticker)
                try:
                    status_code, payload_text, payload = fetch_chart_payload(
                        job.ticker,
                        start_date=configured_start,
                        asof_date=asof_date,
                        policy=policy,
                    )
                    request_count += 1
                    store_raw_response(
                        conn,
                        source_id=policy.source_id,
                        endpoint=endpoint,
                        ticker=job.ticker,
                        query_params=query_params,
                        response_status=status_code,
                        payload_text=payload_text,
                        asof_date=asof_date.isoformat(),
                        ingestion_run_id=ingestion_run_id,
                    )
                    if status_code != 200:
                        failed_tickers.append(job.ticker)
                        coverage_rows.append(
                            coverage_row(job, policy.source_id, "failed", [], f"http_status_{status_code}")
                        )
                        LOGGER.warning("[%d/%d] %s failed: http_status=%s", idx, len(jobs), job.ticker, status_code)
                        if idx % commit_every == 0:
                            conn.commit()
                            LOGGER.info("Committed Yahoo price sync progress: %d/%d", idx, len(jobs))
                        time.sleep(max(0.0, policy.sleep_sec))
                        continue
                    bars = parse_bars(job.ticker, payload, source_id=policy.source_id)
                    if not bars:
                        failed_tickers.append(job.ticker)
                        coverage_rows.append(coverage_row(job, policy.source_id, "failed", [], "no_usable_bars"))
                        LOGGER.warning("[%d/%d] %s failed: no usable bars", idx, len(jobs), job.ticker)
                        if idx % commit_every == 0:
                            conn.commit()
                            LOGGER.info("Committed Yahoo price sync progress: %d/%d", idx, len(jobs))
                        time.sleep(max(0.0, policy.sleep_sec))
                        continue
                    upserted = upsert_price_bars(conn, bars)
                    total_bars += upserted
                    coverage_rows.append(coverage_row(job, policy.source_id, "success", bars))
                    LOGGER.info(
                        "[%d/%d] %s bars=%d first=%s last=%s",
                        idx,
                        len(jobs),
                        job.ticker,
                        upserted,
                        bars[0].bar_date,
                        bars[-1].bar_date,
                    )
                    if idx % commit_every == 0:
                        conn.commit()
                        LOGGER.info("Committed Yahoo price sync progress: %d/%d", idx, len(jobs))
                    time.sleep(max(0.0, policy.sleep_sec))
                except Exception as exc:
                    failed_tickers.append(job.ticker)
                    coverage_rows.append(coverage_row(job, policy.source_id, "failed", [], f"{type(exc).__name__}: {exc}"))
                    LOGGER.warning("[%d/%d] %s failed: %s", idx, len(jobs), job.ticker, exc)
                    if idx % commit_every == 0:
                        conn.commit()
                        LOGGER.info("Committed Yahoo price sync progress: %d/%d", idx, len(jobs))
            status = "partial" if failed_tickers else "success"
            message = f"jobs={len(jobs)} bars={total_bars} output={output_csv}"
            if failed_tickers:
                message += " failed_tickers=" + ",".join(failed_tickers)
            finish_ingestion_run(
                conn,
                ingestion_run_id=ingestion_run_id,
                status=status,
                request_count=request_count,
                row_count=total_bars,
                message=message,
            )
            finish_run(conn, run_id=run_id, status=status, row_count=total_bars, message=message)
        except BaseException as exc:
            finish_ingestion_run(
                conn,
                ingestion_run_id=ingestion_run_id,
                status="failed",
                request_count=request_count,
                row_count=total_bars,
                message=f"{type(exc).__name__}: {exc}",
            )
            finish_run(conn, run_id=run_id, status="failed", row_count=total_bars, message=f"{type(exc).__name__}: {exc}")
            raise

    write_csv(output_csv, coverage_rows)
    LOGGER.info("Yahoo adjusted price sync complete: bars=%d output=%s failed=%d", total_bars, output_csv, len(failed_tickers))
    if failed_tickers and not args.allow_partial:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
