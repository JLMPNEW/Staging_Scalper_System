#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import logging
import sys
import time
from datetime import date, datetime
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


LOGGER = logging.getLogger("sync_med_device_exchange_short_interest")
DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
DEFAULT_NASDAQ_SHORT_INTEREST_URL = "https://www.nasdaqtrader.com/dynamic/symdir/shortinterest.txt"
FIELDNAMES = [
    "ticker",
    "settlement_date",
    "source_id",
    "company_id",
    "short_interest",
    "avg_daily_volume",
    "days_to_cover",
    "float_shares",
    "short_interest_pct_float",
    "publication_date",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Sync true exchange/Nasdaq short-interest snapshots for med-device tickers."
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--asof", default="")
    parser.add_argument("--settlement-date", default="")
    parser.add_argument("--publication-date", default="")
    parser.add_argument("--input-csv", type=Path, default=None, help="Optional normalized or Nasdaq-style short-interest CSV/TXT.")
    parser.add_argument("--input-url", default="", help="Optional current snapshot URL. Defaults to config/current Nasdaq file.")
    parser.add_argument("--output-csv", type=Path, default=None)
    parser.add_argument("--tickers", default="")
    return parser.parse_args()


def to_float(raw: object) -> float | None:
    text = str(raw or "").replace(",", "").replace("%", "").strip()
    if not text or text.lower() in {"nan", "none", "null", "n/a", "na"}:
        return None
    try:
        value = float(text)
    except ValueError:
        return None
    return value if value == value else None


def parse_date_text(raw: object) -> str:
    text = str(raw or "").strip()
    if not text:
        return ""
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%Y%m%d"):
        try:
            return datetime.strptime(text[:10], fmt).date().isoformat()
        except ValueError:
            continue
    return text[:10]


def normalize_pct(raw: float | None, source_text: object = "") -> float | None:
    if raw is None:
        return None
    value = float(raw)
    if "%" in str(source_text) or abs(value) > 1.0:
        value /= 100.0
    return value


def header_key(raw: object) -> str:
    return "".join(ch for ch in str(raw or "").strip().lower() if ch.isalnum())


def first_value(row: dict[str, Any], aliases: list[str]) -> object:
    normalized = {header_key(key): value for key, value in row.items()}
    for alias in aliases:
        key = header_key(alias)
        if key in normalized and str(normalized[key] or "").strip() != "":
            return normalized[key]
    return ""


def ensure_source(conn: Any, source_id: str) -> None:
    now = utc_now()
    conn.execute(
        """
        INSERT INTO source_registry(
            source_id, stage, source_name, source_type, base_url,
            authentication_required, free_key_required, priority, status, created_at, updated_at
        )
        VALUES (?, 'stage_1', 'Exchange reported short interest snapshots', 'txt_or_csv',
                'https://www.nasdaqtrader.com/dynamic/symdir/shortinterest.txt', 0, 0, 63, 'planned', ?, ?)
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


def detect_delimiter(text: str) -> str:
    first_line = next((line for line in text.splitlines() if line.strip()), "")
    return "|" if first_line.count("|") >= first_line.count(",") else ","


def parse_short_interest_text(
    text: str,
    *,
    source_id: str,
    company_by_ticker: dict[str, dict[str, Any]],
    tickers: set[str],
    default_settlement_date: str,
    default_publication_date: str,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    reader = csv.DictReader(text.splitlines(), delimiter=detect_delimiter(text))
    for raw in reader:
        if raw is None:
            continue
        ticker = normalize_ticker(
            first_value(raw, ["ticker", "symbol", "issue_symbol", "issue symbol", "Symbol"])
        )
        if not ticker or ticker not in company_by_ticker or (tickers and ticker not in tickers):
            continue
        short_interest_raw = first_value(
            raw,
            [
                "short_interest",
                "short_interest_shares",
                "current_short_interest",
                "current short interest",
                "current short",
                "short interest",
            ],
        )
        short_interest = to_float(short_interest_raw)
        if short_interest is None:
            continue
        avg_daily_volume = to_float(
            first_value(
                raw,
                [
                    "avg_daily_volume",
                    "average_daily_volume",
                    "average daily share volume",
                    "average daily volume",
                    "adv",
                ],
            )
        )
        days_to_cover = to_float(first_value(raw, ["days_to_cover", "days to cover", "dtc"]))
        float_shares = to_float(first_value(raw, ["float_shares", "float shares", "public_float", "shares_float"]))
        pct_raw = first_value(
            raw,
            [
                "short_interest_pct_float",
                "short interest pct float",
                "short_interest_percent_float",
                "percent_of_float",
                "pct_float",
            ],
        )
        pct_float = normalize_pct(to_float(pct_raw), pct_raw)
        if pct_float is None and float_shares and float_shares > 0:
            pct_float = short_interest / float_shares
        settlement_date = parse_date_text(
            first_value(raw, ["settlement_date", "settlement date", "asof_date", "as of date"])
        ) or default_settlement_date
        publication_date = parse_date_text(
            first_value(raw, ["publication_date", "publication date", "dissemination_date", "date"])
        ) or default_publication_date
        company = company_by_ticker[ticker]
        rows.append(
            {
                "ticker": ticker,
                "settlement_date": settlement_date,
                "source_id": source_id,
                "company_id": int(company["company_id"]),
                "short_interest": short_interest,
                "avg_daily_volume": avg_daily_volume,
                "days_to_cover": days_to_cover,
                "float_shares": float_shares,
                "short_interest_pct_float": pct_float,
                "publication_date": publication_date,
                "payload_json": json.dumps(raw, sort_keys=True, ensure_ascii=True),
            }
        )
    return rows


def fetch_text(url: str, *, user_agent: str, timeout_sec: float, retries: int, sleep_sec: float) -> str:
    request = Request(url, headers={"User-Agent": user_agent})
    last_error: Exception | None = None
    for attempt in range(max(1, retries)):
        try:
            with urlopen(request, timeout=timeout_sec) as response:
                return response.read().decode("utf-8", errors="replace")
        except (HTTPError, URLError, TimeoutError) as exc:
            last_error = exc
            if attempt + 1 < retries:
                time.sleep(sleep_sec * (attempt + 1))
                continue
    raise RuntimeError(f"Failed to fetch short-interest snapshot: url={url} error={last_error}")


def upsert_rows(conn: Any, rows: list[dict[str, Any]]) -> int:
    if not rows:
        return 0
    now = utc_now()
    conn.executemany(
        """
        INSERT INTO fact_short_interest(
            ticker, settlement_date, source_id, company_id, short_interest, avg_daily_volume,
            days_to_cover, float_shares, short_interest_pct_float, publication_date,
            payload_json, created_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(ticker, settlement_date, source_id) DO UPDATE SET
            company_id = excluded.company_id,
            short_interest = excluded.short_interest,
            avg_daily_volume = excluded.avg_daily_volume,
            days_to_cover = excluded.days_to_cover,
            float_shares = excluded.float_shares,
            short_interest_pct_float = excluded.short_interest_pct_float,
            publication_date = excluded.publication_date,
            payload_json = excluded.payload_json,
            updated_at = excluded.updated_at
        """,
        [
            (
                row["ticker"],
                row["settlement_date"],
                row["source_id"],
                row["company_id"],
                row.get("short_interest"),
                row.get("avg_daily_volume"),
                row.get("days_to_cover"),
                row.get("float_shares"),
                row.get("short_interest_pct_float"),
                row.get("publication_date"),
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
    asof = args.asof.strip() or date.today().isoformat()
    settlement_date = parse_date_text(args.settlement_date) or asof
    publication_date = parse_date_text(args.publication_date) or asof
    db_path = args.db.expanduser().resolve() if args.db else resolve_path(cfg_get(config, "paths.database_path"), base_dir=base_dir)
    source_id = str(cfg_get(config, "short_interest_ingestion.source_id", "exchange_short_interest"))
    output_csv = (
        args.output_csv.expanduser().resolve()
        if args.output_csv
        else resolve_path(
            cfg_get(config, "short_interest_ingestion.output_csv", "../output/med_devices_reports/med_device_exchange_short_interest.csv"),
            base_dir=base_dir,
        )
    )
    ticker_filter = {normalize_ticker(value) for value in str(args.tickers or "").split(",") if normalize_ticker(value)}
    with connect(db_path, timeout_sec=float(cfg_get(config, "runtime.sqlite_timeout_sec", 30.0))) as conn:
        init_db(conn)
        ensure_source(conn, source_id)
        run_id = start_run(conn, run_type="sync_med_device_exchange_short_interest", input_path=config_path)
        try:
            company_by_ticker = load_company_map(conn)
            if args.input_csv:
                text = args.input_csv.expanduser().resolve().read_text(encoding="utf-8-sig")
            else:
                input_url = args.input_url.strip() or str(
                    cfg_get(config, "short_interest_ingestion.current_snapshot_url", DEFAULT_NASDAQ_SHORT_INTEREST_URL)
                )
                text = fetch_text(
                    input_url,
                    user_agent=str(cfg_get(config, "short_interest_ingestion.user_agent", "med-devices-research/1.0")),
                    timeout_sec=float(cfg_get(config, "short_interest_ingestion.timeout_sec", 30.0)),
                    retries=int(cfg_get(config, "short_interest_ingestion.download_retries", 3)),
                    sleep_sec=float(cfg_get(config, "short_interest_ingestion.retry_sleep_sec", 2.0)),
                )
            rows = parse_short_interest_text(
                text,
                source_id=source_id,
                company_by_ticker=company_by_ticker,
                tickers=ticker_filter,
                default_settlement_date=settlement_date,
                default_publication_date=publication_date,
            )
            count = upsert_rows(conn, rows)
            write_csv(output_csv, rows)
            finish_run(conn, run_id=run_id, status="success", row_count=count, message=f"asof={asof} rows={count}")
            LOGGER.info("Exchange short-interest sync complete: rows=%d output=%s", count, output_csv)
        except BaseException as exc:
            finish_run(conn, run_id=run_id, status="failed", row_count=0, message=f"{type(exc).__name__}: {exc}")
            raise


if __name__ == "__main__":
    main()
