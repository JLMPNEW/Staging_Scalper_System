#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import logging
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from med_devices.core.config import cfg_get, load_yaml, resolve_path  # noqa: E402
from med_devices.core.db import connect, finish_run, init_db, start_run, utc_now  # noqa: E402
from med_devices.core.logging_utils import configure_utc_logging  # noqa: E402
from med_devices.core.text_norm import normalize_ticker  # noqa: E402


LOGGER = logging.getLogger("sync_med_device_finra_short_volume")
DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
FIELDNAMES = [
    "ticker",
    "trade_date",
    "source_id",
    "company_id",
    "short_volume",
    "short_exempt_volume",
    "total_volume",
    "short_volume_ratio",
    "market",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Sync FINRA Reg SHO short-volume facts for med-device tickers.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--start-date", default="")
    parser.add_argument("--end-date", default="")
    parser.add_argument("--input-csv", type=Path, default=None, help="Optional local FINRA-format or normalized CSV.")
    parser.add_argument("--output-csv", type=Path, default=None)
    parser.add_argument("--tickers", default="")
    parser.add_argument(
        "--max-days",
        type=int,
        default=None,
        help="Override finra_short_volume_ingestion.max_days_per_run; use 0 for no cap.",
    )
    return parser.parse_args()


def to_float(raw: object) -> float | None:
    try:
        value = float(str(raw).strip())
    except (TypeError, ValueError):
        return None
    return value if value == value else None


def parse_date(raw: str) -> date:
    return datetime.strptime(str(raw), "%Y-%m-%d").date()


def date_range(start: date, end: date) -> list[date]:
    days: list[date] = []
    cur = start
    while cur <= end:
        days.append(cur)
        cur += timedelta(days=1)
    return days


def ensure_source(conn: Any, source_id: str) -> None:
    now = utc_now()
    conn.execute(
        """
        INSERT INTO source_registry(
            source_id, stage, source_name, source_type, base_url,
            authentication_required, free_key_required, priority, status, created_at, updated_at
        )
        VALUES (?, 'stage_1', 'FINRA Reg SHO daily short volume', 'txt',
                'https://cdn.finra.org/equity/regsho/daily/', 0, 0, 62, 'planned', ?, ?)
        ON CONFLICT(source_id) DO UPDATE SET updated_at = excluded.updated_at
        """,
        (source_id, now, now),
    )


def load_company_map(conn: Any) -> dict[str, dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT company_id, ticker
        FROM dim_company
        WHERE is_active = 1
        """
    ).fetchall()
    return {normalize_ticker(row["ticker"]): dict(row) for row in rows}


def parse_finra_lines(text: str, *, source_id: str, company_by_ticker: dict[str, dict[str, Any]], tickers: set[str]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    reader = csv.DictReader(text.splitlines(), delimiter="|")
    for raw in reader:
        ticker = normalize_ticker(raw.get("Symbol") or raw.get("ticker") or raw.get("symbol"))
        if not ticker or ticker not in company_by_ticker or (tickers and ticker not in tickers):
            continue
        short_volume = to_float(raw.get("ShortVolume") or raw.get("short_volume"))
        short_exempt = to_float(raw.get("ShortExemptVolume") or raw.get("short_exempt_volume")) or 0.0
        total_volume = to_float(raw.get("TotalVolume") or raw.get("total_volume"))
        ratio = short_volume / total_volume if short_volume is not None and total_volume and total_volume > 0 else None
        trade_date = str(raw.get("Date") or raw.get("trade_date") or "")
        if len(trade_date) == 8 and trade_date.isdigit():
            trade_date = f"{trade_date[:4]}-{trade_date[4:6]}-{trade_date[6:]}"
        company = company_by_ticker[ticker]
        rows.append(
            {
                "ticker": ticker,
                "trade_date": trade_date,
                "source_id": str(raw.get("source_id") or source_id),
                "company_id": int(company["company_id"]),
                "short_volume": short_volume,
                "short_exempt_volume": short_exempt,
                "total_volume": total_volume,
                "short_volume_ratio": ratio,
                "market": str(raw.get("Market") or raw.get("market") or ""),
                "payload_json": json.dumps(raw, sort_keys=True, ensure_ascii=True),
            }
        )
    return rows


def fetch_finra_file(day: date, *, config: dict[str, Any]) -> str | None:
    template = str(cfg_get(config, "finra_short_volume_ingestion.base_url_template"))
    url = template.format(yyyymmdd=day.strftime("%Y%m%d"))
    request = Request(url, headers={"User-Agent": "med-devices-research/1.0"})
    retries = int(cfg_get(config, "finra_short_volume_ingestion.download_retries", 3))
    retry_sleep_sec = float(cfg_get(config, "finra_short_volume_ingestion.retry_sleep_sec", 2.0))
    for attempt in range(max(1, retries)):
        try:
            with urlopen(request, timeout=30) as response:
                return response.read().decode("utf-8", errors="replace")
        except HTTPError as exc:
            if exc.code in {403, 404}:
                return None
            if 500 <= exc.code < 600 and attempt + 1 < retries:
                time.sleep(retry_sleep_sec * (attempt + 1))
                continue
            LOGGER.warning("Skipping FINRA short-volume file after HTTP %s: %s", exc.code, url)
            return None
        except URLError as exc:
            if attempt + 1 < retries:
                time.sleep(retry_sleep_sec * (attempt + 1))
                continue
            LOGGER.warning("Skipping FINRA short-volume file after network error: url=%s error=%s", url, exc)
            return None
    return None


def load_rows(
    *,
    input_csv: Path | None,
    start: date,
    end: date,
    source_id: str,
    company_by_ticker: dict[str, dict[str, Any]],
    tickers: set[str],
    config: dict[str, Any],
    max_days_override: int | None,
) -> list[dict[str, Any]]:
    if input_csv is not None:
        return parse_finra_lines(input_csv.read_text(encoding="utf-8-sig"), source_id=source_id, company_by_ticker=company_by_ticker, tickers=tickers)
    rows: list[dict[str, Any]] = []
    max_days = (
        int(max_days_override)
        if max_days_override is not None
        else int(cfg_get(config, "finra_short_volume_ingestion.max_days_per_run", 30))
    )
    sleep_sec = float(cfg_get(config, "finra_short_volume_ingestion.request_sleep_sec", 0.25))
    days = date_range(start, end)
    if max_days > 0:
        days = days[:max_days]
    fetched_days = 0
    missing_days = 0
    for day in days:
        if day.weekday() >= 5:
            continue
        text = fetch_finra_file(day, config=config)
        if text:
            fetched_days += 1
            rows.extend(parse_finra_lines(text, source_id=source_id, company_by_ticker=company_by_ticker, tickers=tickers))
        else:
            missing_days += 1
        time.sleep(sleep_sec)
    LOGGER.info(
        "FINRA short-volume range processed: start=%s end=%s calendar_days=%d fetched_days=%d missing_weekdays=%d rows=%d",
        start,
        end,
        len(days),
        fetched_days,
        missing_days,
        len(rows),
    )
    return rows


def upsert_rows(conn: Any, rows: list[dict[str, Any]]) -> int:
    now = utc_now()
    conn.executemany(
        """
        INSERT INTO fact_finra_short_volume(
            ticker, trade_date, source_id, company_id, short_volume, short_exempt_volume,
            total_volume, short_volume_ratio, market, payload_json, created_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(ticker, trade_date, source_id) DO UPDATE SET
            company_id = excluded.company_id,
            short_volume = excluded.short_volume,
            short_exempt_volume = excluded.short_exempt_volume,
            total_volume = excluded.total_volume,
            short_volume_ratio = excluded.short_volume_ratio,
            market = excluded.market,
            payload_json = excluded.payload_json,
            updated_at = excluded.updated_at
        """,
        [
            (
                row["ticker"],
                row["trade_date"],
                row["source_id"],
                row["company_id"],
                row.get("short_volume"),
                row.get("short_exempt_volume"),
                row.get("total_volume"),
                row.get("short_volume_ratio"),
                row.get("market", ""),
                row.get("payload_json", "{}"),
                now,
                now,
            )
            for row in rows
        ],
    )
    return len(rows)


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDNAMES, extrasaction="ignore", lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    configure_utc_logging()
    args = parse_args()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    base_dir = config_path.parent
    db_path = args.db.expanduser().resolve() if args.db else resolve_path(cfg_get(config, "paths.database_path"), base_dir=base_dir)
    source_id = str(cfg_get(config, "finra_short_volume_ingestion.source_id", "finra_regsho_short_volume"))
    end = parse_date(args.end_date) if args.end_date else date.today()
    start = parse_date(args.start_date) if args.start_date else end
    output_csv = (
        args.output_csv.expanduser().resolve()
        if args.output_csv
        else resolve_path(cfg_get(config, "finra_short_volume_ingestion.output_csv"), base_dir=base_dir)
    )
    ticker_filter = {normalize_ticker(value) for value in str(args.tickers or "").split(",") if normalize_ticker(value)}
    with connect(db_path, timeout_sec=float(cfg_get(config, "runtime.sqlite_timeout_sec", 30.0))) as conn:
        init_db(conn)
        ensure_source(conn, source_id)
        run_id = start_run(conn, run_type="sync_med_device_finra_short_volume", input_path=config_path)
        try:
            company_by_ticker = load_company_map(conn)
            rows = load_rows(
                input_csv=args.input_csv.expanduser().resolve() if args.input_csv else None,
                start=start,
                end=end,
                source_id=source_id,
                company_by_ticker=company_by_ticker,
                tickers=ticker_filter,
                config=config,
                max_days_override=args.max_days,
            )
            count = upsert_rows(conn, rows)
            write_csv(output_csv, rows)
            finish_run(conn, run_id=run_id, status="success", row_count=count, message=f"start={start} end={end} rows={count}")
            LOGGER.info("FINRA short-volume sync complete: rows=%d", count)
        except BaseException as exc:
            finish_run(conn, run_id=run_id, status="failed", row_count=0, message=f"{type(exc).__name__}: {exc}")
            raise


if __name__ == "__main__":
    main()
