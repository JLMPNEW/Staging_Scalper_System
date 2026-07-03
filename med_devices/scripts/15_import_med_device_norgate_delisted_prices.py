#!/usr/bin/env python3
"""Import Norgate total-return prices for med-device historical members."""
from __future__ import annotations

import argparse
import csv
import logging
import os
import sqlite3
import sys
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from med_devices.core.config import cfg_get, load_yaml, resolve_path  # noqa: E402
from med_devices.core.db import connect, init_db  # noqa: E402
from med_devices.core.logging_utils import configure_utc_logging  # noqa: E402
from med_devices.core.source_registry import load_source_registry, upsert_source_registry  # noqa: E402


LOGGER = logging.getLogger("import_med_device_norgate_delisted_prices")
DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
SOURCE_REGISTRY = PACKAGE_ROOT / "data" / "free_source_registry.yaml"
DEFAULT_MEMBERSHIP_CSV = PACKAGE_ROOT / "data" / "med_device_historical_membership.csv"
DEFAULT_OUTPUT = (
    PROJECT_ROOT
    / "output"
    / "med_devices_reports"
    / "market_data"
    / "norgate_delisted_price_import.csv"
)
SOURCE_ID = "norgate_us_equities_total_return"
RUN_TYPE = "med_device_norgate_delisted_price_import"
REPORT_FIELDS = [
    "ticker",
    "exchange_ticker",
    "company_name",
    "membership_start",
    "membership_end",
    "norgate_symbol",
    "norgate_security_name",
    "mapping_reason",
    "status",
    "loaded_rows",
    "first_bar_date",
    "last_bar_date",
    "error",
]


@dataclass(frozen=True)
class HistoricalMember:
    ticker: str
    exchange_ticker: str
    price_source_symbol: str
    company_name: str
    start_date: str
    end_date: str


@dataclass(frozen=True)
class SymbolMatch:
    source_symbol: str | None
    reason: str
    security_name: str = ""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Import Norgate med-device historical-member OHLCV rows.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--membership-csv", type=Path, default=DEFAULT_MEMBERSHIP_CSV)
    parser.add_argument("--start-date", default=None)
    parser.add_argument("--source-id", default=SOURCE_ID)
    parser.add_argument("--output-csv", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def now_utc() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def safe_float(value: Any) -> float | None:
    try:
        if pd.isna(value):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def pandas_index_date(value: object) -> str:
    return str(pd.Timestamp(str(value)))[:10]


def normalize_ticker(raw: object) -> str:
    return str(raw or "").strip().upper().replace(".", "-")


def read_members(path: Path) -> list[HistoricalMember]:
    members: list[HistoricalMember] = []
    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            ticker = normalize_ticker(row.get("ticker") or row.get("internal_ticker"))
            if not ticker:
                continue
            members.append(
                HistoricalMember(
                    ticker=ticker,
                    exchange_ticker=normalize_ticker(row.get("exchange_ticker") or ticker),
                    price_source_symbol=normalize_ticker(row.get("price_source_symbol")),
                    company_name=str(row.get("company_name") or ticker).strip(),
                    start_date=str(row.get("start_date") or "").strip(),
                    end_date=str(row.get("end_date") or "").strip(),
                )
            )
    if not members:
        raise ValueError(f"No historical membership rows found in {path}")
    return members


def security_name(norgatedata: Any, symbol: str) -> str:
    try:
        return str(norgatedata.security_name(symbol) or "")
    except Exception:
        return ""


def choose_symbol(norgatedata: Any, member: HistoricalMember, delisted_symbols: set[str], current_symbols: set[str]) -> SymbolMatch:
    if member.price_source_symbol:
        if member.price_source_symbol not in delisted_symbols and member.price_source_symbol not in current_symbols:
            return SymbolMatch(None, "explicit_price_source_symbol_missing")
        return SymbolMatch(
            member.price_source_symbol,
            "explicit_price_source_symbol",
            security_name(norgatedata, member.price_source_symbol),
        )

    hits = [symbol for symbol in delisted_symbols if symbol == member.exchange_ticker or symbol.startswith(f"{member.exchange_ticker}-")]
    if not hits and member.exchange_ticker in current_symbols:
        hits = [member.exchange_ticker]
    if not hits:
        return SymbolMatch(None, "missing_norgate_symbol")

    target = pd.Timestamp(member.end_date)
    scored: list[tuple[float, str]] = []
    for symbol in hits:
        try:
            last = pd.Timestamp(norgatedata.last_quoted_date(symbol))
        except Exception:
            continue
        scored.append((abs((last - target).days), symbol))
    if not scored:
        return SymbolMatch(None, "symbol_metadata_error")
    scored.sort()
    diff_days, best = scored[0]
    if best in current_symbols:
        return SymbolMatch(best, "matched_current_symbol_by_exact_ticker", security_name(norgatedata, best))
    if diff_days > 45:
        return SymbolMatch(None, "no_symbol_close_to_membership_end")
    return SymbolMatch(best, "matched_by_symbol_and_end_date", security_name(norgatedata, best))


def fetch_prices(norgatedata: Any, symbol: str, start_date: str, end_date: str) -> pd.DataFrame:
    raw = norgatedata.price_timeseries(
        symbol,
        stock_price_adjustment_setting=norgatedata.StockPriceAdjustmentType.NONE,
        start_date=start_date,
        end_date=end_date,
        timeseriesformat="pandas-dataframe",
    )
    adjusted = norgatedata.price_timeseries(
        symbol,
        stock_price_adjustment_setting=norgatedata.StockPriceAdjustmentType.TOTALRETURN,
        start_date=start_date,
        end_date=end_date,
        timeseriesformat="pandas-dataframe",
    )
    if raw is None or len(raw) == 0:
        return pd.DataFrame()

    raw = raw.copy()
    raw.index = pd.to_datetime(raw.index).date
    if adjusted is not None and len(adjusted):
        adjusted = adjusted.copy()
        adjusted.index = pd.to_datetime(adjusted.index).date
        raw["AdjClose"] = adjusted["Close"]
    else:
        raw["AdjClose"] = raw["Close"]
    return raw


def ensure_source(conn: sqlite3.Connection, source_id: str) -> None:
    sources = [row for row in load_source_registry(SOURCE_REGISTRY) if str(row.get("source_id")) == source_id]
    if not sources:
        raise SystemExit(f"source_id {source_id!r} is not defined in {SOURCE_REGISTRY}")
    upsert_source_registry(conn, sources)


def start_ingestion(conn: sqlite3.Connection, source_id: str, timestamp: str) -> int:
    cur = conn.execute(
        """
        INSERT INTO ingestion_runs(source_id, started_at, status, request_count, row_count, message, created_at)
        VALUES (?, ?, 'running', 0, 0, ?, ?)
        """,
        (source_id, timestamp, RUN_TYPE, timestamp),
    )
    if cur.lastrowid is None:
        raise RuntimeError("Unable to create Norgate ingestion run; missing ingestion_run_id.")
    return int(cur.lastrowid)


def finish_ingestion(conn: sqlite3.Connection, run_id: int, status: str, requests: int, rows: int, message: str) -> None:
    conn.execute(
        """
        UPDATE ingestion_runs
        SET completed_at = ?, status = ?, request_count = ?, row_count = ?, message = ?
        WHERE ingestion_run_id = ?
        """,
        (now_utc(), status, requests, rows, message, run_id),
    )


def upsert_price_rows(
    conn: sqlite3.Connection,
    *,
    ticker: str,
    source_id: str,
    source_symbol: str,
    match_reason: str,
    prices: pd.DataFrame,
) -> int:
    inserted = 0
    timestamp = now_utc()
    adjustment = f"norgate_total_return_adj_close;source_symbol={source_symbol};match={match_reason}"
    for idx, row in prices.iterrows():
        close = safe_float(row.get("Close"))
        adj_close = safe_float(row.get("AdjClose"))
        if close is None or adj_close is None:
            continue
        bar_date = pandas_index_date(idx)
        conn.execute(
            """
            INSERT INTO fact_price_ohlcv(
                ticker, bar_date, source_id, open, high, low, close, adj_close, volume,
                dividend_amount, split_factor, price_adjustment, is_adjusted, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, 1, ?, ?)
            ON CONFLICT(ticker, bar_date, source_id) DO UPDATE SET
                open = excluded.open,
                high = excluded.high,
                low = excluded.low,
                close = excluded.close,
                adj_close = excluded.adj_close,
                volume = excluded.volume,
                dividend_amount = excluded.dividend_amount,
                split_factor = COALESCE(excluded.split_factor, fact_price_ohlcv.split_factor),
                price_adjustment = excluded.price_adjustment,
                is_adjusted = excluded.is_adjusted,
                updated_at = excluded.updated_at
            """,
            (
                ticker,
                bar_date,
                source_id,
                safe_float(row.get("Open")),
                safe_float(row.get("High")),
                safe_float(row.get("Low")),
                close,
                adj_close,
                safe_float(row.get("Volume")),
                safe_float(row.get("Dividend")),
                adjustment,
                timestamp,
                timestamp,
            ),
        )
        inserted += 1
    return inserted


def main() -> int:
    configure_utc_logging()
    args = parse_args()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    base_dir = config_path.parent
    db_path = args.db.expanduser().resolve() if args.db else resolve_path(cfg_get(config, "paths.database_path"), base_dir=base_dir)
    membership_csv = args.membership_csv.expanduser().resolve()
    if not membership_csv.exists():
        membership_csv = resolve_path(
            cfg_get(config, "med_devices_universe.historical_membership_csv", "data/med_device_historical_membership.csv"),
            base_dir=base_dir,
        )
    start_date = args.start_date or "2010-01-01"

    if not os.environ.get("NORGATEDATA_ROOT"):
        cache_dir = PROJECT_ROOT / "output" / "norgatedata_cache"
        cache_dir.mkdir(parents=True, exist_ok=True)
        os.environ["NORGATEDATA_ROOT"] = str(cache_dir)

    try:
        import norgatedata  # pyright: ignore[reportMissingImports]
    except ImportError as exc:
        raise SystemExit("norgatedata package is not installed in this Python environment.") from exc

    members = read_members(membership_csv)
    delisted_symbols = set(norgatedata.database_symbols("US Equities Delisted"))
    current_symbols = set(norgatedata.database_symbols("US Equities"))
    output_csv = args.output_csv.expanduser().resolve()
    output_csv.parent.mkdir(parents=True, exist_ok=True)

    report_rows: list[dict[str, Any]] = []
    loaded_rows = 0
    request_count = 0

    run_id: int | None = None
    started = now_utc()
    timeout_sec = float(cfg_get(config, "runtime.sqlite_timeout_sec", 30.0) or 30.0)
    context = nullcontext(None) if args.dry_run else connect(db_path, timeout_sec=timeout_sec)
    with context as conn:
        try:
            if conn is not None:
                init_db(conn)
                ensure_source(conn, args.source_id)
                run_id = start_ingestion(conn, args.source_id, started)

            for member in members:
                match = choose_symbol(norgatedata, member, delisted_symbols, current_symbols)
                status = "skipped"
                rows = 0
                first_bar = ""
                last_bar = ""
                error = ""
                if match.source_symbol is None:
                    status = match.reason
                else:
                    try:
                        prices = fetch_prices(norgatedata, match.source_symbol, start_date, member.end_date)
                        request_count += 2
                        if prices.empty:
                            status = "no_price_bars"
                        else:
                            first_bar = min(pandas_index_date(idx) for idx in prices.index)
                            last_bar = max(pandas_index_date(idx) for idx in prices.index)
                            status = "loaded"
                            rows = len(prices)
                            if conn is not None:
                                rows = upsert_price_rows(
                                    conn,
                                    ticker=member.ticker,
                                    source_id=args.source_id,
                                    source_symbol=match.source_symbol,
                                    match_reason=match.reason,
                                    prices=prices,
                                )
                                loaded_rows += rows
                    except Exception as exc:  # noqa: BLE001
                        status = "error"
                        error = repr(exc)
                report_rows.append(
                    {
                        "ticker": member.ticker,
                        "exchange_ticker": member.exchange_ticker,
                        "company_name": member.company_name,
                        "membership_start": member.start_date,
                        "membership_end": member.end_date,
                        "norgate_symbol": match.source_symbol or "",
                        "norgate_security_name": match.security_name,
                        "mapping_reason": match.reason,
                        "status": status,
                        "loaded_rows": rows if status == "loaded" else 0,
                        "first_bar_date": first_bar,
                        "last_bar_date": last_bar,
                        "error": error,
                    }
                )

            if run_id is not None and conn is not None:
                finish_ingestion(
                    conn,
                    run_id,
                    "completed",
                    request_count,
                    loaded_rows,
                    f"{RUN_TYPE}: loaded_rows={loaded_rows}",
                )
        except Exception:
            if run_id is not None and conn is not None:
                try:
                    finish_ingestion(conn, run_id, "failed", request_count, loaded_rows, f"{RUN_TYPE}: failed")
                    conn.commit()
                except Exception as cleanup_exc:  # noqa: BLE001
                    LOGGER.warning("Failed to mark Norgate import run as failed: %r", cleanup_exc)
            raise

    with output_csv.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=REPORT_FIELDS, extrasaction="ignore", lineterminator="\n")
        writer.writeheader()
        writer.writerows(report_rows)
    status_counts: dict[str, int] = {}
    for row in report_rows:
        status_counts[str(row["status"])] = status_counts.get(str(row["status"]), 0) + 1
    print(f"norgate_report={output_csv} members={len(members)} loaded_rows={loaded_rows} statuses={status_counts}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
