#!/usr/bin/env python3
"""Import Norgate adjusted price history for defense delisted calibration rows."""
from __future__ import annotations

import argparse
import csv
import json
import logging
import sqlite3
import sys
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

import pandas as pd


PACKAGE_ROOT = Path(__file__).resolve().parents[2]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from industrials.core.config import cfg_get, load_yaml, resolve_path  # noqa: E402
from industrials.core.db import connect, finish_run, init_db, start_run, utc_now  # noqa: E402
from industrials.core.logging_utils import configure_utc_logging  # noqa: E402
from industrials.core.reports import write_csv_atomic  # noqa: E402
from industrials.core.text_norm import normalize_ticker  # noqa: E402


LOGGER = logging.getLogger("import_defense_norgate_delisted_prices")
DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
SOURCE_ID = "norgate_us_equities_total_return"
RUN_TYPE = "import_defense_norgate_delisted_prices"
REPORT_FIELDS = [
    "ticker",
    "internal_ticker",
    "company_name",
    "cohort",
    "exit_year",
    "norgate_symbol",
    "norgate_database",
    "price_adjustment_mode",
    "norgate_security_name",
    "norgate_last_quoted_date",
    "mapping_reason",
    "status",
    "loaded_rows",
    "first_bar_date",
    "last_bar_date",
    "last_raw_close",
    "last_adjusted_close",
    "error",
]


@dataclass(frozen=True)
class DelistedMember:
    ticker: str
    internal_ticker: str
    company_name: str
    cohort: str
    exit_year: int | None
    start_date: str
    end_date: str


@dataclass(frozen=True)
class SymbolOverride:
    ticker: str
    norgate_symbol: str
    source_database: str
    override_start_date: str
    override_end_date: str
    mapping_reason: str
    review_status: str
    notes: str


@dataclass(frozen=True)
class SymbolMatch:
    source_symbol: str | None
    reason: str
    security_name: str = ""
    last_quoted_date: str = ""
    source_database: str = ""
    start_date: str = ""
    end_date: str = ""
    loadable: bool = True


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Import Norgate delisted defense OHLCV rows.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--source-id", default="", help="Overrides config norgate_delisted_import.source_id.")
    parser.add_argument("--start-date", default="", help="Fallback start date. Defaults to config delisted_default_start_date.")
    parser.add_argument("--output-csv", type=Path, default=None)
    parser.add_argument("--price-adjustment-mode", default="", help="Norgate StockPriceAdjustmentType for adj_close. Defaults to config norgate_delisted_import.price_adjustment_mode.")
    parser.add_argument("--no-purge-existing-range", action="store_true", help="Keep stale bars outside the current loaded date range.")
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def parse_date(raw: object) -> str:
    text = str(raw or "").strip()[:10]
    if not text:
        return ""
    try:
        return datetime.strptime(text, "%Y-%m-%d").date().isoformat()
    except ValueError:
        return ""


def safe_float(value: Any) -> float | None:
    try:
        if pd.isna(value):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def pandas_index_date(value: object) -> str:
    return str(pd.Timestamp(str(value)))[:10]


def name_similarity(left: str, right: str) -> float:
    left_clean = " ".join(str(left or "").upper().replace(",", " ").split())
    right_clean = " ".join(str(right or "").upper().replace(",", " ").split())
    if not left_clean or not right_clean:
        return 0.0
    return SequenceMatcher(None, left_clean, right_clean).ratio()


def security_name(norgatedata: Any, symbol: str) -> str:
    try:
        return str(norgatedata.security_name(symbol) or "")
    except Exception:
        return ""


def last_quoted_date(norgatedata: Any, symbol: str) -> str:
    try:
        return parse_date(norgatedata.last_quoted_date(symbol))
    except Exception:
        return ""


def load_members(conn: sqlite3.Connection, *, model_family: str, default_start_date: str) -> list[DelistedMember]:
    rows = conn.execute(
        """
        SELECT d.ticker, d.internal_ticker, d.company_name, d.calibration_cohort_id,
               d.exit_year, m.start_date, m.end_date
        FROM dim_delisted_calibration_seed d
        LEFT JOIN dim_universe_membership m
          ON m.ticker = d.internal_ticker
         AND m.model_family = d.model_family
         AND m.membership_basis = 'delisted_calibration_seed'
        WHERE d.model_family = ?
        ORDER BY d.ticker
        """,
        (model_family,),
    ).fetchall()
    members: list[DelistedMember] = []
    for row in rows:
        ticker = normalize_ticker(row["ticker"])
        internal_ticker = normalize_ticker(row["internal_ticker"])
        if not ticker or not internal_ticker:
            continue
        exit_year = int(row["exit_year"]) if str(row["exit_year"] or "").strip().isdigit() else None
        start_date = parse_date(row["start_date"]) or default_start_date
        end_date = parse_date(row["end_date"])
        if not end_date and exit_year:
            end_date = f"{exit_year}-12-31"
        members.append(
            DelistedMember(
                ticker=ticker,
                internal_ticker=internal_ticker,
                company_name=str(row["company_name"] or ticker),
                cohort=str(row["calibration_cohort_id"] or ""),
                exit_year=exit_year,
                start_date=start_date,
                end_date=end_date or "2100-01-01",
            )
        )
    return members


def csv_value(row: dict[str, str], key: str) -> str:
    return str(row.get(key) or "").strip()


def load_symbol_overrides(path: Path) -> dict[str, SymbolOverride]:
    if not path.exists():
        return {}
    overrides: dict[str, SymbolOverride] = {}
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        for line_number, row in enumerate(reader, start=2):
            ticker = normalize_ticker(csv_value(row, "ticker"))
            symbol = normalize_ticker(csv_value(row, "norgate_symbol"))
            if not ticker and not symbol:
                continue
            if not ticker:
                raise ValueError(f"{path}:{line_number} missing ticker")
            if not symbol:
                raise ValueError(f"{path}:{line_number} missing or invalid norgate_symbol for ticker={ticker}")
            if ticker in overrides:
                raise ValueError(f"{path}:{line_number} duplicate override ticker={ticker}")
            review_status = csv_value(row, "review_status").lower()
            if review_status not in {"reviewed", "approved"}:
                raise ValueError(f"{path}:{line_number} ticker={ticker} review_status must be reviewed or approved")
            overrides[ticker] = SymbolOverride(
                ticker=ticker,
                norgate_symbol=symbol,
                source_database=csv_value(row, "source_database"),
                override_start_date=parse_date(csv_value(row, "override_start_date")),
                override_end_date=parse_date(csv_value(row, "override_end_date")),
                mapping_reason=csv_value(row, "mapping_reason") or "reviewed_manual_mapping",
                review_status=review_status,
                notes=csv_value(row, "notes"),
            )
    return overrides


def norgate_database_for_symbol(
    symbol: str,
    *,
    delisted_symbols: set[str],
    current_symbols: set[str],
) -> str:
    if symbol in delisted_symbols:
        return "US Equities Delisted"
    if symbol in current_symbols:
        return "US Equities"
    return ""


def choose_symbol(
    norgatedata: Any,
    member: DelistedMember,
    *,
    delisted_symbols: set[str],
    current_symbols: set[str],
    overrides: dict[str, SymbolOverride],
) -> SymbolMatch:
    override = overrides.get(member.ticker)
    if override is not None:
        database = norgate_database_for_symbol(
            override.norgate_symbol,
            delisted_symbols=delisted_symbols,
            current_symbols=current_symbols,
        )
        if not database:
            return SymbolMatch(
                override.norgate_symbol,
                "override_symbol_not_found",
                source_database=override.source_database,
                start_date=override.override_start_date,
                end_date=override.override_end_date,
                loadable=False,
            )
        expected_database = override.source_database.strip()
        if expected_database and expected_database != database:
            return SymbolMatch(
                override.norgate_symbol,
                f"override_database_mismatch_expected_{expected_database}",
                source_database=database,
                security_name=security_name(norgatedata, override.norgate_symbol),
                last_quoted_date=last_quoted_date(norgatedata, override.norgate_symbol),
                start_date=override.override_start_date,
                end_date=override.override_end_date,
                loadable=False,
            )
        return SymbolMatch(
            override.norgate_symbol,
            override.mapping_reason,
            security_name(norgatedata, override.norgate_symbol),
            last_quoted_date(norgatedata, override.norgate_symbol),
            source_database=database,
            start_date=override.override_start_date,
            end_date=override.override_end_date,
        )

    candidates = {
        symbol
        for symbol in delisted_symbols
        if symbol == member.ticker
        or symbol.startswith(f"{member.ticker}-")
        or symbol.startswith(f"{member.ticker}Q-")
        or symbol.startswith(f"{member.ticker}D-")
    }
    if member.ticker in current_symbols and member.ticker not in candidates:
        candidates.add(member.ticker)
    if not candidates:
        return SymbolMatch(None, "missing_norgate_symbol")

    scored: list[tuple[float, str, str, str]] = []
    for symbol in sorted(candidates):
        name = security_name(norgatedata, symbol)
        last = last_quoted_date(norgatedata, symbol)
        if member.exit_year and last:
            try:
                year_gap = abs(int(last[:4]) - member.exit_year)
            except ValueError:
                year_gap = 20
        else:
            year_gap = 20
        similarity = name_similarity(member.company_name, name)
        current_penalty = 0.75 if symbol in current_symbols and symbol not in delisted_symbols else 0.0
        score = similarity - year_gap * 0.08 - current_penalty
        scored.append((score, symbol, name, last))
    scored.sort(reverse=True)
    best_score, best_symbol, best_name, best_last = scored[0]
    if best_score < -0.35:
        return SymbolMatch(None, "no_plausible_norgate_symbol")
    return SymbolMatch(
        best_symbol,
        "matched_by_symbol_name_and_exit_year",
        best_name,
        best_last,
        source_database=norgate_database_for_symbol(
            best_symbol,
            delisted_symbols=delisted_symbols,
            current_symbols=current_symbols,
        ),
    )


def resolve_adjustment_type(norgatedata: Any, mode: str) -> Any:
    mode_clean = str(mode or "").strip().upper()
    if not mode_clean:
        mode_clean = "CAPITAL"
    try:
        return getattr(norgatedata.StockPriceAdjustmentType, mode_clean)
    except AttributeError as exc:
        valid = [name for name in dir(norgatedata.StockPriceAdjustmentType) if name.isupper()]
        raise ValueError(f"Unsupported Norgate price_adjustment_mode={mode_clean!r}; valid={valid}") from exc


def readonly_connection(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"{db_path.as_uri()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def fetch_prices(norgatedata: Any, symbol: str, start_date: str, end_date: str, *, adjustment_mode: str) -> pd.DataFrame:
    raw = norgatedata.price_timeseries(
        symbol,
        stock_price_adjustment_setting=norgatedata.StockPriceAdjustmentType.NONE,
        start_date=start_date,
        end_date=end_date,
        timeseriesformat="pandas-dataframe",
    )
    if raw is None or len(raw) == 0:
        return pd.DataFrame()
    raw = raw.copy()
    raw.index = pd.to_datetime(raw.index).date
    raw = raw.sort_index()
    if adjustment_mode.strip().upper() == "NONE":
        raw["AdjClose"] = raw["Close"]
        return raw
    adjusted = norgatedata.price_timeseries(
        symbol,
        stock_price_adjustment_setting=resolve_adjustment_type(norgatedata, adjustment_mode),
        start_date=start_date,
        end_date=end_date,
        timeseriesformat="pandas-dataframe",
    )
    if adjusted is not None and len(adjusted):
        adjusted = adjusted.copy()
        adjusted.index = pd.to_datetime(adjusted.index).date
        adjusted = adjusted.sort_index()
        raw["AdjClose"] = adjusted["Close"]
    else:
        raw["AdjClose"] = raw["Close"]
    return raw


def update_source_status(conn: sqlite3.Connection, *, source_id: str) -> None:
    conn.execute(
        """
        UPDATE source_registry
        SET status = 'active',
            notes = COALESCE(notes, '') || CASE WHEN COALESCE(notes, '') LIKE '%Defense delisted import enabled.%' THEN '' ELSE ' Defense delisted import enabled.' END,
            updated_at = ?
        WHERE source_id = ?
        """,
        (utc_now(), source_id),
    )


def add_issue(
    conn: sqlite3.Connection,
    *,
    member: DelistedMember,
    source_id: str,
    issue_type: str,
    detail: str,
    severity: str = "warning",
    model_family: str,
) -> None:
    # SC-12: issues are family-scoped; stamp model_family so per-stage clears for
    # one family never wipe another family's open issues.
    now = utc_now()
    row = conn.execute("SELECT company_id FROM dim_company WHERE ticker = ? LIMIT 1", (member.internal_ticker,)).fetchone()
    company_id = int(row["company_id"]) if row is not None else None
    conn.execute(
        """
        INSERT INTO data_quality_issues(
            detected_at, severity, stage, model_family, ticker, company_id, source_id, issue_type,
            issue_detail, resolution_status, created_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, ?)
        """,
        (now, severity, RUN_TYPE, model_family, member.internal_ticker, company_id, source_id, issue_type, detail, now, now),
    )


def upsert_price_rows(
    conn: sqlite3.Connection,
    *,
    member: DelistedMember,
    source_id: str,
    source_symbol: str,
    match_reason: str,
    adjustment_mode: str,
    prices: pd.DataFrame,
) -> int:
    now = now_utc()
    inserted = 0
    mode_clean = adjustment_mode.strip().upper() or "CAPITAL"
    adjustment = f"norgate_{mode_clean.lower()}_adj_close;source_symbol={source_symbol};match={match_reason};original_ticker={member.ticker}"
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
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, 1, ?, ?)
            ON CONFLICT(ticker, bar_date, source_id) DO UPDATE SET
                open = excluded.open,
                high = excluded.high,
                low = excluded.low,
                close = excluded.close,
                adj_close = excluded.adj_close,
                volume = excluded.volume,
                dividend_amount = excluded.dividend_amount,
                price_adjustment = excluded.price_adjustment,
                is_adjusted = excluded.is_adjusted,
                updated_at = excluded.updated_at
            """,
            (
                member.internal_ticker,
                pandas_index_date(idx),
                source_id,
                safe_float(row.get("Open")),
                safe_float(row.get("High")),
                safe_float(row.get("Low")),
                close,
                adj_close,
                safe_float(row.get("Volume")),
                safe_float(row.get("Dividend")),
                adjustment,
                now,
                now,
            ),
        )
        inserted += 1
    return inserted


def upsert_snapshot(
    conn: sqlite3.Connection,
    *,
    member: DelistedMember,
    source_id: str,
    source_symbol: str,
    adjustment_mode: str,
    prices: pd.DataFrame,
) -> None:
    if prices.empty:
        return
    prices = prices.sort_index()
    last_idx = prices.index[-1]
    last_row = prices.loc[last_idx]
    asof_date = pandas_index_date(last_idx)
    price = safe_float(last_row.get("AdjClose")) or safe_float(last_row.get("Close"))
    payload = json.dumps({"source_symbol": source_symbol, "original_ticker": member.ticker, "price_adjustment_mode": adjustment_mode.strip().upper() or "CAPITAL"}, sort_keys=True)
    now = now_utc()
    conn.execute(
        """
        INSERT INTO fact_market_snapshot(
            ticker, asof_date, source_id, market_cap, shares_outstanding,
            regular_market_price, currency, quote_type, exchange, source_timestamp,
            payload_json, created_at, updated_at
        )
        VALUES (?, ?, ?, NULL, NULL, ?, 'USD', 'delisted_equity', 'historical_delisted', ?, ?, ?, ?)
        ON CONFLICT(ticker, asof_date, source_id) DO UPDATE SET
            regular_market_price = excluded.regular_market_price,
            quote_type = excluded.quote_type,
            exchange = excluded.exchange,
            source_timestamp = excluded.source_timestamp,
            payload_json = excluded.payload_json,
            updated_at = excluded.updated_at
        """,
        (member.internal_ticker, asof_date, source_id, price, now, payload, now, now),
    )


def purge_existing_range(
    conn: sqlite3.Connection,
    *,
    member: DelistedMember,
    source_id: str,
    first_bar: str,
    last_bar: str,
) -> None:
    conn.execute(
        """
        DELETE FROM fact_price_ohlcv
        WHERE ticker = ?
          AND source_id = ?
          AND (bar_date < ? OR bar_date > ?)
        """,
        (member.internal_ticker, source_id, first_bar, last_bar),
    )
    conn.execute(
        """
        DELETE FROM fact_market_snapshot
        WHERE ticker = ?
          AND source_id = ?
          AND asof_date <> ?
        """,
        (member.internal_ticker, source_id, last_bar),
    )


def main() -> int:
    configure_utc_logging()
    args = parse_args()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    base_dir = config_path.parent
    db_path = args.db.expanduser().resolve() if args.db else resolve_path(cfg_get(config, "paths.database_path"), base_dir=base_dir)
    source_id = str(args.source_id or cfg_get(config, "norgate_delisted_import.source_id", SOURCE_ID) or SOURCE_ID)
    model_family = str(cfg_get(config, "industrials_universe.initial_subsector", "defense") or "defense")
    default_start_date = parse_date(args.start_date) or str(cfg_get(config, "industrials_universe.delisted_default_start_date", "2000-01-01"))
    output_csv = (
        args.output_csv.expanduser().resolve()
        if args.output_csv
        else resolve_path(
            cfg_get(config, "norgate_delisted_import.output_csv", "../output/industrials/defense/stage3/norgate_delisted_price_import.csv"),
            base_dir=base_dir,
        )
    )
    overrides_raw = str(cfg_get(config, "norgate_delisted_import.symbol_overrides_csv", "") or "").strip()
    overrides_path = resolve_path(overrides_raw, base_dir=base_dir) if overrides_raw else None
    symbol_overrides = load_symbol_overrides(overrides_path) if overrides_path is not None else {}
    adjustment_mode = str(
        args.price_adjustment_mode
        or cfg_get(config, "norgate_delisted_import.price_adjustment_mode", "CAPITAL")
        or "CAPITAL"
    ).strip().upper()
    purge_config = str(cfg_get(config, "norgate_delisted_import.purge_existing_range", True)).strip().lower()
    purge_ranges = not args.no_purge_existing_range and purge_config not in {"0", "false", "no", "off"}

    try:
        import norgatedata  # pyright: ignore[reportMissingImports]
    except ImportError as exc:
        raise SystemExit("norgatedata package is not installed in this Python environment.") from exc
    resolve_adjustment_type(norgatedata, adjustment_mode)

    delisted_symbols = set(norgatedata.database_symbols("US Equities Delisted"))
    current_symbols = set(norgatedata.database_symbols("US Equities"))
    report_rows: list[dict[str, Any]] = []
    loaded_rows = 0
    request_count = 0

    conn_obj = readonly_connection(db_path) if args.dry_run else connect(db_path, timeout_sec=float(cfg_get(config, "runtime.sqlite_timeout_sec", 120.0)))
    with closing(conn_obj) as conn:
        if not args.dry_run:
            init_db(conn)
        members = load_members(conn, model_family=model_family, default_start_date=default_start_date)
        if not members:
            raise ValueError(f"No delisted members loaded for model_family={model_family}")
        run_id = 0 if args.dry_run else start_run(conn, run_type=RUN_TYPE, input_path=config_path)
        try:
            if not args.dry_run:
                with conn:
                    update_source_status(conn, source_id=source_id)
                    # SC-12: family-scoped clear so this import never wipes
                    # another family's open issues for the same stage.
                    conn.execute(
                        "DELETE FROM data_quality_issues WHERE stage = ? AND model_family = ?",
                        (RUN_TYPE, model_family),
                    )
            for member in members:
                match = choose_symbol(
                    norgatedata,
                    member,
                    delisted_symbols=delisted_symbols,
                    current_symbols=current_symbols,
                    overrides=symbol_overrides,
                )
                status = "skipped"
                first_bar = ""
                last_bar = ""
                last_raw_close: float | None = None
                last_adjusted_close: float | None = None
                row_count = 0
                error = ""
                if match.source_symbol is None or not match.loadable:
                    status = match.reason
                    if not args.dry_run:
                        with conn:
                            add_issue(
                                conn,
                                member=member,
                                source_id=source_id,
                                issue_type="norgate_delisted_symbol_unresolved",
                                detail=f"{match.reason}; symbol={match.source_symbol or ''}; database={match.source_database}",
                                model_family=model_family,
                            )
                else:
                    try:
                        start_date = match.start_date or member.start_date
                        end_date = match.end_date or member.end_date
                        prices = fetch_prices(norgatedata, match.source_symbol, start_date, end_date, adjustment_mode=adjustment_mode)
                        request_count += 1 + int(adjustment_mode != "NONE")
                        if prices.empty:
                            status = "no_price_bars"
                            if not args.dry_run:
                                with conn:
                                    add_issue(
                                        conn,
                                        member=member,
                                        source_id=source_id,
                                        issue_type="norgate_delisted_no_price_bars",
                                        detail=f"symbol={match.source_symbol} start={start_date} end={end_date}",
                                        model_family=model_family,
                                    )
                        else:
                            prices = prices.sort_index()
                            first_bar = min(pandas_index_date(idx) for idx in prices.index)
                            last_bar = max(pandas_index_date(idx) for idx in prices.index)
                            last_row = prices.loc[prices.index[-1]]
                            last_raw_close = safe_float(last_row.get("Close"))
                            last_adjusted_close = safe_float(last_row.get("AdjClose"))
                            row_count = len(prices)
                            status = "dry_run_loadable" if args.dry_run else "loaded"
                            if not args.dry_run:
                                with conn:
                                    if purge_ranges:
                                        purge_existing_range(
                                            conn,
                                            member=member,
                                            source_id=source_id,
                                            first_bar=first_bar,
                                            last_bar=last_bar,
                                        )
                                    row_count = upsert_price_rows(
                                        conn,
                                        member=member,
                                        source_id=source_id,
                                        source_symbol=match.source_symbol,
                                        match_reason=match.reason,
                                        adjustment_mode=adjustment_mode,
                                        prices=prices,
                                    )
                                    upsert_snapshot(
                                        conn,
                                        member=member,
                                        source_id=source_id,
                                        source_symbol=match.source_symbol,
                                        adjustment_mode=adjustment_mode,
                                        prices=prices,
                                    )
                                loaded_rows += row_count
                    except Exception as exc:  # noqa: BLE001
                        status = "error"
                        error = repr(exc)
                        if not args.dry_run:
                            with conn:
                                add_issue(
                                    conn,
                                    member=member,
                                    source_id=source_id,
                                    issue_type="norgate_delisted_import_error",
                                    detail=error,
                                    severity="error",
                                    model_family=model_family,
                                )
                report_rows.append(
                    {
                        "ticker": member.ticker,
                        "internal_ticker": member.internal_ticker,
                        "company_name": member.company_name,
                        "cohort": member.cohort,
                        "exit_year": "" if member.exit_year is None else member.exit_year,
                        "norgate_symbol": match.source_symbol or "",
                        "norgate_database": match.source_database,
                        "price_adjustment_mode": adjustment_mode,
                        "norgate_security_name": match.security_name,
                        "norgate_last_quoted_date": match.last_quoted_date,
                        "mapping_reason": match.reason,
                        "status": status,
                        "loaded_rows": row_count if status == "loaded" else 0,
                        "first_bar_date": first_bar,
                        "last_bar_date": last_bar,
                        "last_raw_close": "" if last_raw_close is None else round(last_raw_close, 6),
                        "last_adjusted_close": "" if last_adjusted_close is None else round(last_adjusted_close, 6),
                        "error": error,
                    }
                )
            status = "success" if all(row["status"] in {"loaded", "dry_run_loadable"} for row in report_rows) else "partial"
            if not args.dry_run:
                finish_run(
                    conn,
                    run_id=run_id,
                    status=status,
                    row_count=loaded_rows,
                    message=f"members={len(members)} loaded={sum(1 for row in report_rows if row['status'] == 'loaded')} rows={loaded_rows} requests={request_count}",
                )
        except BaseException as exc:
            if not args.dry_run:
                finish_run(conn, run_id=run_id, status="failed", row_count=loaded_rows, message=f"{type(exc).__name__}: {exc}")
            raise

    write_csv_atomic(output_csv, REPORT_FIELDS, report_rows)
    status_counts: dict[str, int] = {}
    for row in report_rows:
        status_counts[str(row["status"])] = status_counts.get(str(row["status"]), 0) + 1
    LOGGER.info("Wrote Norgate delisted price import report: %s", output_csv)
    LOGGER.info("Norgate delisted import complete: members=%d loaded_rows=%d statuses=%s", len(report_rows), loaded_rows, status_counts)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
