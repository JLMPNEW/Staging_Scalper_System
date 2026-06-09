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
from urllib.parse import urlencode

import requests  # type: ignore[reportMissingModuleSource]


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from technology.core.config import cfg_get, expand_env_vars, load_yaml, resolve_path  # noqa: E402
from technology.core.db import connect, finish_run, init_db, start_run, utc_now  # noqa: E402
from technology.core.logging_utils import configure_utc_logging  # noqa: E402
from technology.core.source_registry import load_source_registry, upsert_source_registry  # noqa: E402


LOGGER = logging.getLogger("sync_technology_fx_rates")
DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
RUN_TYPE = "sync_technology_fx_rates"
CSV_FIELDS = ["currency", "symbol", "status", "rates_upserted", "first_rate_date", "last_rate_date", "review_reason"]


@dataclass(frozen=True)
class FxRate:
    base_currency: str
    quote_currency: str
    rate_date: str
    fx_rate: float


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Sync USD FX rates for technology financial statement conversion.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--currencies", default="")
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


def safe_float(raw: object) -> float | None:
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def int_set(raw: object, default: set[int]) -> set[int]:
    if raw is None:
        return default
    if isinstance(raw, list):
        return {int(x) for x in raw}
    return {int(x.strip()) for x in str(raw).split(",") if x.strip()}


def cache_name(symbol: str, start: date, end: date) -> str:
    safe = "".join(ch if ch.isalnum() else "_" for ch in symbol)
    return f"{safe}_{start.isoformat()}_{end.isoformat()}.json"


def load_currencies(conn: Any, configured: object, cli: str, facts_source: str) -> list[str]:
    if cli.strip():
        currencies = {x.strip().upper() for x in cli.split(",") if x.strip()}
    elif isinstance(configured, list):
        currencies = {str(x).strip().upper() for x in configured if str(x).strip()}
    elif str(configured or "").strip().lower() != "auto":
        currencies = {x.strip().upper() for x in str(configured or "").split(",") if x.strip()}
    else:
        rows = conn.execute(
            """
            SELECT DISTINCT UPPER(reported_currency) AS currency
            FROM fact_financial_statement_canonical
            WHERE source_id = ?
              AND COALESCE(reported_currency, '') <> ''
            ORDER BY currency
            """,
            (facts_source,),
        ).fetchall()
        currencies = {str(row["currency"] or "").upper() for row in rows if str(row["currency"] or "")}
    currencies.add("USD")
    return sorted(currency for currency in currencies if len(currency) == 3 and currency.isalpha())


def fx_symbol_candidates(base_currency: str) -> list[tuple[str, bool]]:
    base_currency = base_currency.upper()
    if base_currency in {"USD", "USN", "USS"}:
        return [("USDUSD=X", False)]
    return [
        (f"{base_currency}USD=X", False),
        (f"USD{base_currency}=X", True),
        (f"{base_currency}=X", True),
    ]


def fetch_payload(
    symbol: str,
    *,
    chart_url_template: str,
    start: date,
    end: date,
    cache_dir: Path,
    force_refresh: bool,
    headers: dict[str, str],
    timeout_sec: float,
    retries: int,
    retry_status_codes: set[int],
    sleep_sec: float,
) -> tuple[int, str, str, dict[str, Any]]:
    endpoint = chart_url_template.format(ticker=symbol)
    params = {
        "period1": unix_timestamp(start),
        "period2": unix_timestamp(end + timedelta(days=1)),
        "interval": "1d",
    }
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_path = cache_dir / cache_name(symbol, start, end)
    if cache_path.exists() and not force_refresh:
        text = cache_path.read_text(encoding="utf-8", errors="replace")
        return 200, endpoint + "?" + urlencode(params), text, json.loads(text)
    last_exc: Exception | None = None
    for attempt in range(max(1, retries)):
        try:
            response = requests.get(endpoint, params=params, headers=headers, timeout=timeout_sec)
            text = response.text
            if response.status_code in retry_status_codes and attempt + 1 < retries:
                time.sleep(sleep_sec * (attempt + 1))
                continue
            if response.status_code == 200:
                cache_path.write_text(text, encoding="utf-8")
            return response.status_code, response.url, text, response.json() if text else {}
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            if attempt + 1 < retries:
                time.sleep(sleep_sec * (attempt + 1))
    raise RuntimeError(f"FX request failed for {symbol}: {last_exc}")


def parse_rates(payload: dict[str, Any], *, base_currency: str, quote_currency: str, invert: bool) -> list[FxRate]:
    chart = payload.get("chart", {}) if isinstance(payload, dict) else {}
    results = chart.get("result") if isinstance(chart, dict) else None
    if not results:
        return []
    result = results[0]
    timestamps = result.get("timestamp") or []
    quote = ((result.get("indicators") or {}).get("quote") or [{}])[0]
    closes = quote.get("close") or []
    rates: list[FxRate] = []
    for ts, close in zip(timestamps, closes):
        value = safe_float(close)
        if value is None or value <= 0:
            continue
        rate = 1.0 / value if invert else value
        rate_date = datetime.fromtimestamp(int(ts), tz=timezone.utc).date().isoformat()
        rates.append(FxRate(base_currency=base_currency, quote_currency=quote_currency, rate_date=rate_date, fx_rate=rate))
    return rates


def record_raw_response(conn: Any, *, source_id: str, endpoint: str, status: int, text: str) -> None:
    now = utc_now()
    digest = hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()
    conn.execute(
        """
        INSERT INTO raw_api_responses(
            source_id, endpoint, query_params_json, request_time_utc, response_status,
            response_hash, asof_date, payload_text, ingestion_run_id, created_at
        )
        VALUES (?, ?, '', ?, ?, ?, ?, ?, NULL, ?)
        """,
        (source_id, endpoint, now, int(status), digest, date.today().isoformat(), text, now),
    )


def upsert_rates(conn: Any, rates: list[FxRate], *, source_id: str) -> int:
    now = utc_now()
    count = 0
    for rate in rates:
        conn.execute(
            """
            INSERT INTO fact_fx_rate(
                base_currency, quote_currency, rate_date, source_id, rate_type,
                fx_rate, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, 'close', ?, ?, ?)
            ON CONFLICT(base_currency, quote_currency, rate_date, source_id, rate_type) DO UPDATE SET
                fx_rate = excluded.fx_rate,
                updated_at = excluded.updated_at
            """,
            (rate.base_currency, rate.quote_currency, rate.rate_date, source_id, rate.fx_rate, now, now),
        )
        count += 1
    return count


def identity_usd_rates(conn: Any, *, currency: str, start: date, end: date) -> list[FxRate]:
    rows = conn.execute(
        """
        SELECT DISTINCT bar_date
        FROM fact_price_ohlcv
        WHERE bar_date BETWEEN ? AND ?
        ORDER BY bar_date
        """,
        (start.isoformat(), end.isoformat()),
    ).fetchall()
    if rows:
        dates = [str(row["bar_date"]) for row in rows]
    else:
        dates = [(start + timedelta(days=offset)).isoformat() for offset in range((end - start).days + 1)]
    return [FxRate(base_currency=currency, quote_currency="USD", rate_date=day, fx_rate=1.0) for day in dates]


def write_report(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS, extrasaction="ignore", lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    configure_utc_logging()
    args = parse_args()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    base_dir = config_path.parent
    db_path = args.db.expanduser().resolve() if args.db else resolve_path(cfg_get(config, "paths.database_path"), base_dir=base_dir)
    output_csv = args.output_csv.expanduser().resolve() if args.output_csv else resolve_path(cfg_get(config, "fx_rates.output_csv"), base_dir=base_dir)
    registry_path = resolve_path(cfg_get(config, "source_registry.path"), base_dir=base_dir)
    source_id = str(cfg_get(config, "fx_rates.source_id", "yahoo_fx_rates"))
    facts_source = str(cfg_get(config, "sec_fundamentals.companyfacts_source_id", "sec_companyfacts"))
    start = parse_date(cfg_get(config, "fx_rates.start_date", "2015-01-01")) or date(2015, 1, 1)
    end = date.today()
    chart_url_template = str(cfg_get(config, "fx_rates.chart_url_template"))
    cache_dir = resolve_path(cfg_get(config, "fx_rates.cache_dir"), base_dir=base_dir)
    quote_currency = str(cfg_get(config, "fx_rates.quote_currency", "USD") or "USD").upper()
    user_agent = expand_env_vars(cfg_get(config, "fx_rates.user_agent", "JL, Independent Research, jm.357@hotmail.com"))
    timeout_sec = float(cfg_get(config, "fx_rates.timeout_sec", 30.0))
    retries = int(cfg_get(config, "fx_rates.max_retries", 3))
    sleep_sec = float(cfg_get(config, "fx_rates.request_sleep_sec", 0.05))
    retry_status_codes = int_set(cfg_get(config, "fx_rates.retry_status_codes"), {429, 500, 502, 503, 504})
    headers = {"User-Agent": user_agent, "Accept": "application/json,text/plain,*/*"}
    report_rows: list[dict[str, Any]] = []
    failures = 0
    total_rates = 0
    with connect(db_path, timeout_sec=float(cfg_get(config, "runtime.sqlite_timeout_sec", 30.0))) as conn:
        init_db(conn)
        upsert_source_registry(conn, load_source_registry(registry_path))
        currencies = load_currencies(conn, cfg_get(config, "fx_rates.currencies", "auto"), args.currencies, facts_source)
        run_id = start_run(conn, run_type=RUN_TYPE, input_path=config_path)
        try:
            with conn:
                for currency in currencies:
                    symbol_used = ""
                    rates: list[FxRate] = []
                    reason = ""
                    if currency in {"USD", "USN", "USS"}:
                        symbol_used = "USDUSD=X"
                        rates = identity_usd_rates(conn, currency=currency, start=start, end=end)
                    else:
                        for symbol, invert in fx_symbol_candidates(currency):
                            status, endpoint, text, payload = fetch_payload(
                                symbol,
                                chart_url_template=chart_url_template,
                                start=start,
                                end=end,
                                cache_dir=cache_dir,
                                force_refresh=args.force_refresh,
                                headers=headers,
                                timeout_sec=timeout_sec,
                                retries=retries,
                                retry_status_codes=retry_status_codes,
                                sleep_sec=sleep_sec,
                            )
                            record_raw_response(conn, source_id=source_id, endpoint=endpoint, status=status, text=text)
                            if status != 200:
                                reason = f"{symbol}_http_{status}"
                                continue
                            rates = parse_rates(payload, base_currency=currency, quote_currency=quote_currency, invert=invert)
                            if rates:
                                symbol_used = symbol
                                break
                            reason = f"{symbol}_no_rates"
                    upserted = upsert_rates(conn, rates, source_id=source_id)
                    total_rates += upserted
                    if not rates:
                        failures += 1
                    report_rows.append(
                        {
                            "currency": currency,
                            "symbol": symbol_used,
                            "status": "success" if rates else "failed",
                            "rates_upserted": upserted,
                            "first_rate_date": rates[0].rate_date if rates else "",
                            "last_rate_date": rates[-1].rate_date if rates else "",
                            "review_reason": reason,
                        }
                    )
                    LOGGER.info("%s symbol=%s rates=%d status=%s", currency, symbol_used or "none", upserted, "success" if rates else reason)
            write_report(output_csv, report_rows)
            status = "success" if failures == 0 else ("partial" if args.allow_partial else "failed")
            finish_run(conn, run_id=run_id, status=status, row_count=total_rates, message=f"currencies={len(currencies)} failures={failures} output={output_csv}")
            if failures and not args.allow_partial:
                raise SystemExit(1)
        except BaseException as exc:
            if not isinstance(exc, SystemExit):
                finish_run(conn, run_id=run_id, status="failed", row_count=0, message=f"{type(exc).__name__}: {exc}")
            raise


if __name__ == "__main__":
    main()
