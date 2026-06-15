#!/usr/bin/env python3
"""One-time Norgate price import for software-infrastructure historical members."""
from __future__ import annotations

import argparse
import csv
import json
import logging
import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd


PACKAGE_ROOT = Path(__file__).resolve().parents[2]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from technology.core.config import cfg_get, load_yaml, resolve_path  # noqa: E402
from technology.core.logging_utils import configure_utc_logging  # noqa: E402


LOGGER = logging.getLogger("import_software_infrastructure_norgate_delisted_prices")
DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
DEFAULT_MEMBERSHIP_CSV = PACKAGE_ROOT / "software_infrastructure" / "data" / "software_infrastructure_historical_membership.csv"
DEFAULT_OUTPUT = (
    PROJECT_ROOT
    / "output"
    / "technology_reports"
    / "software_infrastructure"
    / "market_data"
    / "norgate_delisted_price_import.csv"
)
SOURCE_ID = "norgate_us_equities_total_return"
RUN_TYPE = "software_infrastructure_norgate_delisted_price_import"
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
    parser = argparse.ArgumentParser(description="Import Norgate delisted software-infrastructure OHLCV rows.")
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


def read_members(path: Path) -> list[HistoricalMember]:
    members: list[HistoricalMember] = []
    with path.open(newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            ticker = str(row.get("ticker") or row.get("internal_ticker") or "").strip().upper()
            if not ticker:
                continue
            members.append(
                HistoricalMember(
                    ticker=ticker,
                    exchange_ticker=str(row.get("exchange_ticker") or ticker).strip().upper(),
                    price_source_symbol=str(row.get("price_source_symbol") or "").strip().upper(),
                    company_name=str(row["company_name"]).strip(),
                    start_date=str(row["start_date"]).strip(),
                    end_date=str(row["end_date"]).strip(),
                )
            )
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
    if not hits:
        return SymbolMatch(None, "missing_norgate_symbol")

    target = pd.Timestamp(member.end_date)
    scored: list[tuple[int, str]] = []
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


def ensure_source(conn: sqlite3.Connection, source_id: str, timestamp: str) -> None:
    conn.execute(
        """
        INSERT INTO source_registry(
            source_id, stage, source_name, source_owner, source_type, base_url, documentation_url,
            authentication_required, free_key_required, api_key_env, rate_limit_notes, refresh_frequency,
            terms_url, data_owner, raw_schema, canonical_tables, feature_stages, subsector_scope,
            priority, status, notes, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(source_id) DO UPDATE SET
            stage = excluded.stage,
            source_name = excluded.source_name,
            source_owner = excluded.source_owner,
            source_type = excluded.source_type,
            base_url = excluded.base_url,
            documentation_url = excluded.documentation_url,
            authentication_required = excluded.authentication_required,
            free_key_required = excluded.free_key_required,
            rate_limit_notes = excluded.rate_limit_notes,
            refresh_frequency = excluded.refresh_frequency,
            terms_url = excluded.terms_url,
            data_owner = excluded.data_owner,
            raw_schema = excluded.raw_schema,
            canonical_tables = excluded.canonical_tables,
            feature_stages = excluded.feature_stages,
            subsector_scope = excluded.subsector_scope,
            priority = excluded.priority,
            status = excluded.status,
            notes = excluded.notes,
            updated_at = excluded.updated_at
        """,
        (
            source_id,
            "survivorship_backfill",
            "Norgate Data US Equities",
            "Norgate Data",
            "licensed_local_database",
            "https://norgatedata.com/",
            "https://norgatedata.com/python.php",
            1,
            0,
            "",
            "Local licensed Windows database; no HTTP rate limit during local reads.",
            "one_time_backfill_then_manual_refresh",
            "https://norgatedata.com/eula.php",
            "Norgate Data",
            "norgatedata.price_timeseries raw OHLCV plus total-return adjusted close",
            "fact_price_ohlcv",
            "software_infrastructure_calibration,software_infrastructure_backtest,survivorship_backfill",
            "technology",
            15,
            "active",
            "Historical/delisted software-infrastructure price backfill. Rows preserve internal historical ticker keys and record source_symbol in price_adjustment.",
            timestamp,
            timestamp,
        ),
    )


def start_ingestion(conn: sqlite3.Connection, source_id: str, timestamp: str) -> int:
    cur = conn.execute(
        """
        INSERT INTO ingestion_runs(source_id, started_at, status, request_count, row_count, message, created_at)
        VALUES (?, ?, 'running', 0, 0, ?, ?)
        """,
        (source_id, timestamp, RUN_TYPE, timestamp),
    )
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
                split_factor = excluded.split_factor,
                price_adjustment = excluded.price_adjustment,
                is_adjusted = excluded.is_adjusted,
                updated_at = excluded.updated_at
            """,
            (
                ticker,
                idx.isoformat(),
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
    config = load_yaml(args.config)
    db_path = args.db or resolve_path(cfg_get(config, "paths.database_path"), base_dir=PACKAGE_ROOT)
    membership_csv = args.membership_csv.expanduser().resolve()
    start_date = args.start_date or str(cfg_get(config, "technology_universe.optimization_start_date", "2010-01-01"))

    try:
        import norgatedata
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

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    run_id: int | None = None
    started = now_utc()
    try:
        if not args.dry_run:
            ensure_source(conn, args.source_id, started)
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
                        first_bar = min(idx.isoformat() for idx in prices.index)
                        last_bar = max(idx.isoformat() for idx in prices.index)
                        status = "loaded"
                        rows = len(prices)
                        if not args.dry_run:
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

        if run_id is not None:
            finish_ingestion(
                conn,
                run_id,
                "completed",
                request_count,
                loaded_rows,
                f"{RUN_TYPE}: loaded_rows={loaded_rows}",
            )
        if not args.dry_run:
            conn.commit()
    except Exception:
        if run_id is not None:
            finish_ingestion(conn, run_id, "failed", request_count, loaded_rows, f"{RUN_TYPE}: failed")
            conn.commit()
        raise
    finally:
        conn.close()

    with output_csv.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=REPORT_FIELDS, extrasaction="ignore", lineterminator="\n")
        writer.writeheader()
        writer.writerows(report_rows)

    summary = {
        "dry_run": args.dry_run,
        "db": str(db_path),
        "source_id": args.source_id,
        "historical_tickers": len(members),
        "loaded_tickers": sum(1 for row in report_rows if row["status"] == "loaded"),
        "loaded_rows": loaded_rows if not args.dry_run else sum(int(row["loaded_rows"]) for row in report_rows),
        "request_count": request_count,
        "report": str(output_csv),
        "not_loaded": [row for row in report_rows if row["status"] != "loaded"],
    }
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
