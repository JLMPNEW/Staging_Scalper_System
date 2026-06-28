#!/usr/bin/env python3
"""Import Norgate total-return prices for delisted biotech calibration candidates."""
from __future__ import annotations

import argparse
import csv
import json
import logging
import sqlite3
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

import pandas as pd


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from biotech_index.core.config import cfg_get, load_yaml, resolve_path  # noqa: E402
from biotech_index.core.db import connect, init_db  # noqa: E402
from biotech_index.core.text_norm import normalize_ticker  # noqa: E402


LOGGER = logging.getLogger("import_biotech_norgate_delisted_prices")
DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
DEFAULT_CANDIDATES = PACKAGE_ROOT / "data" / "delisted_biotech_calibration_candidates.csv"
DEFAULT_OUTPUT = (
    PROJECT_ROOT
    / "output"
    / "biotech_index_reports"
    / "market_data"
    / "norgate_delisted_biotech_price_import.csv"
)
SOURCE_ID = "norgate_us_equities_total_return"
DEFAULT_START_DATE = "1990-01-01"

REPORT_FIELDS = [
    "ticker",
    "company_name",
    "proposed_cohort",
    "exchange_ticker",
    "norgate_symbol",
    "norgate_security_name",
    "mapping_reason",
    "status",
    "loaded_rows",
    "first_bar_date",
    "last_bar_date",
    "last_raw_close",
    "last_adjusted_close",
    "error",
]

TERMINAL_CONSIDERATION_OVERRIDES = {
    "DNA": "$95.00 cash per share",
    "CELG": "$50.00 cash plus 1.0 BMY share plus one CVR per share",
    "GENZ": "$74.00 cash plus CVR per share",
    "CBST": "$102.00 cash per share",
    "IMCL": "$70.00 cash per share",
    "MEDI": "$58.00 cash per share",
    "PCYC": "$261.25 cash/stock mix per share",
    "CEPH": "$81.50 cash per share",
    "DNDN": "bankruptcy/asset-sale exit; common terminal recovery requires bankruptcy-specific handling",
    "HGSI": "$14.25 cash per share",
    "CLVS": "Chapter 11 liquidation; common terminal recovery requires bankruptcy-specific handling",
    "AUXL": "$33.25 cash/stock mix per share",
    "KERX": "stock-for-stock merger into Akebia; exchange-ratio terminal return requires successor-share handling",
    "VVUS": "bankruptcy/go-private restructuring; common terminal recovery requires bankruptcy-specific handling",
    "PGNX": "Lantheus acquisition; stock/CVR terminal return requires successor-share handling",
    "VRUS": "$137.00 cash per share",
    "LOXO": "$235.00 cash per share",
    "TSRO": "$75.00 cash per share",
    "RCPT": "$232.00 cash per share",
    "MDCO": "$85.00 cash per share",
    "ARIA": "$24.00 cash per share",
    "ANAC": "$99.25 cash per share",
    "ZSPH": "$90.00 cash per share",
    "KITE": "$180.00 cash per share",
    "JUNO": "$87.00 cash per share",
    "AVXS": "$218.00 cash per share",
    "ONCE": "$114.50 cash per share",
    "DRNA": "$38.25 cash per share",
    "TBIO": "$38.00 cash per share",
    "IMMU": "$88.00 cash per share",
    "THOR": "identity conflict: supplied identifiers map to Thoratec; not biotech calibration eligible",
    "BOLD": "$60.00 cash per share",
    "TRIL": "$18.50 cash per share",
    "INHX": "$26.00 cash per share",
    "FPRX": "$38.00 cash per share",
    "CASC": "$10.00 cash per share",
    "RXDX": "$200.00 cash per share",
    "TPTX": "$76.00 cash per share",
    "AKAO": "Chapter 11 asset-sale exit; likely common wipeout pending plan-level confirmation",
    "MLNT": "Chapter 11 reorganization; old common extinguished with zero recovery",
    "INSY": "Chapter 11/liability-driven bankruptcy; likely common wipeout pending plan-level confirmation",
    "TTPH": "Distressed acquisition with cash plus CVR; exact common recovery pending final terms",
    "GNCA": "Liquidating plan; common equity cancelled without distribution",
    "APTX": "Wind-down/delisting after clinical failures; likely common wipeout pending terminal confirmation",
    "HGEN": "Bankruptcy exit; likely common wipeout pending plan-level confirmation",
    "GRTS": "Chapter 11/restructuring exit; likely common wipeout pending plan-level confirmation",
    "CDAK": "Chapter 11 section 363 asset sale; likely common wipeout pending plan-level confirmation",
    "ATHX": "Chapter 11 asset-sale exit; likely common wipeout pending plan-level confirmation",
    "BLUE": "$3.00 cash plus contingent CVR per share",
    "SYBX": "Wind-down after clinical failure; likely common wipeout pending terminal confirmation",
    "OMGA": "Bankruptcy/wind-down exit; likely common wipeout pending plan-level confirmation",
    "EIGR": "Bankruptcy asset-sale exit; likely common wipeout pending plan-level confirmation",
}


@dataclass(frozen=True)
class SymbolMatch:
    source_symbol: str | None
    reason: str
    security_name: str = ""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Import Norgate delisted biotech candidate OHLCV rows.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--candidates", type=Path, default=DEFAULT_CANDIDATES)
    parser.add_argument("--output-csv", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--source-id", type=str, default=SOURCE_ID)
    parser.add_argument("--start-date", type=str, default=DEFAULT_START_DATE)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--no-update-candidates",
        action="store_true",
        help="Do not rewrite the candidate CSV with Norgate mapping/date metadata.",
    )
    return parser.parse_args()


def configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%SZ",
    )
    for handler in logging.getLogger().handlers:
        if handler.formatter is not None:
            handler.formatter.converter = time.gmtime


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


def name_similarity(left: str, right: str) -> float:
    left_clean = " ".join(str(left or "").upper().replace(",", " ").split())
    right_clean = " ".join(str(right or "").upper().replace(",", " ").split())
    if not left_clean or not right_clean:
        return 0.0
    return SequenceMatcher(None, left_clean, right_clean).ratio()


def read_candidates(path: Path) -> tuple[list[dict[str, str]], list[str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None:
            raise ValueError(f"Candidate CSV has no header: {path}")
        fieldnames = [str(field or "").strip() for field in reader.fieldnames]
        rows = [{field: str(row.get(field) or "").strip() for field in fieldnames} for row in reader]
    extra_fields = [
        "norgate_symbol",
        "norgate_security_name",
        "norgate_mapping_reason",
        "norgate_first_bar_date",
        "norgate_last_bar_date",
        "norgate_bar_count",
    ]
    for field in extra_fields:
        if field not in fieldnames:
            insert_after = "share_class_figi" if field == "norgate_symbol" else fieldnames[-1]
            if insert_after in fieldnames and field == "norgate_symbol":
                idx = fieldnames.index(insert_after) + 1
                fieldnames.insert(idx, field)
            else:
                fieldnames.append(field)
    return rows, fieldnames


def write_candidates(path: Path, rows: list[dict[str, str]], fieldnames: list[str]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore", lineterminator="\n")
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in fieldnames})


def write_report(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=REPORT_FIELDS, extrasaction="ignore", lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def security_name(norgatedata: Any, symbol: str) -> str:
    try:
        return str(norgatedata.security_name(symbol) or "")
    except Exception:
        return ""


def last_quoted_date(norgatedata: Any, symbol: str) -> str:
    try:
        return str(norgatedata.last_quoted_date(symbol) or "")
    except Exception:
        return ""


def choose_symbol(
    norgatedata: Any,
    row: dict[str, str],
    *,
    delisted_symbols: set[str],
    current_symbols: set[str],
) -> SymbolMatch:
    explicit = normalize_ticker(row.get("norgate_symbol"))
    if explicit:
        if explicit not in delisted_symbols and explicit not in current_symbols:
            return SymbolMatch(None, "explicit_norgate_symbol_missing")
        return SymbolMatch(explicit, "explicit_norgate_symbol", security_name(norgatedata, explicit))

    ticker = normalize_ticker(row.get("ticker"))
    if not ticker:
        return SymbolMatch(None, "missing_ticker")
    candidates = {
        symbol
        for symbol in delisted_symbols
        if symbol == ticker
        or symbol.startswith(f"{ticker}-")
        or symbol.startswith(f"{ticker}Q-")
        or symbol.startswith(f"{ticker}D-")
    }
    if ticker in current_symbols:
        candidates.add(ticker)
    if not candidates:
        return SymbolMatch(None, "missing_norgate_symbol")

    exit_year = str(row.get("exit_year") or "").strip()
    try:
        target_year = int(exit_year)
    except ValueError:
        target_year = 0
    company = str(row.get("company_name") or "")
    scored: list[tuple[float, str, str, str]] = []
    for symbol in sorted(candidates):
        name = security_name(norgatedata, symbol)
        last = last_quoted_date(norgatedata, symbol)
        try:
            year_gap = abs(int(last[:4]) - target_year) if target_year and len(last) >= 4 else 20
        except ValueError:
            year_gap = 20
        similarity = name_similarity(company, name)
        current_penalty = 0.75 if symbol in current_symbols and symbol not in delisted_symbols else 0.0
        score = similarity - (year_gap * 0.08) - current_penalty
        scored.append((score, symbol, name, last))
    scored.sort(reverse=True)
    best_score, best_symbol, best_name, _last = scored[0]
    if best_score < -0.40:
        return SymbolMatch(None, "no_plausible_norgate_symbol")
    return SymbolMatch(best_symbol, "matched_by_symbol_name_and_exit_year", best_name)


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


def upsert_market_bars(
    conn: sqlite3.Connection,
    *,
    ticker: str,
    source_id: str,
    source_symbol: str,
    match_reason: str,
    prices: pd.DataFrame,
) -> int:
    timestamp = now_utc()
    inserted = 0
    for idx, row in prices.iterrows():
        close = safe_float(row.get("Close"))
        adj_close = safe_float(row.get("AdjClose"))
        if close is None or adj_close is None:
            continue
        raw_close = safe_float(row.get("Unadjusted Close")) or close
        adjustment = json.dumps(
            {
                "adjustment": "norgate_total_return_adj_close",
                "source_symbol": source_symbol,
                "match_reason": match_reason,
            },
            sort_keys=True,
        )
        conn.execute(
            """
            INSERT INTO market_bars_daily(
                ticker, bar_date, source, open, high, low, close, volume, wap,
                price_adjustment, raw_open, raw_high, raw_low, raw_close, adj_close,
                adjustment_factor, dividend_amount, split_factor, corporate_action_source,
                is_adjusted, is_provisional, data_quality, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, 0, 'high', ?, ?)
            ON CONFLICT(ticker, bar_date, source) DO UPDATE SET
                open = excluded.open,
                high = excluded.high,
                low = excluded.low,
                close = excluded.close,
                volume = excluded.volume,
                wap = excluded.wap,
                price_adjustment = excluded.price_adjustment,
                raw_open = excluded.raw_open,
                raw_high = excluded.raw_high,
                raw_low = excluded.raw_low,
                raw_close = excluded.raw_close,
                adj_close = excluded.adj_close,
                adjustment_factor = excluded.adjustment_factor,
                dividend_amount = excluded.dividend_amount,
                split_factor = excluded.split_factor,
                corporate_action_source = excluded.corporate_action_source,
                is_adjusted = excluded.is_adjusted,
                is_provisional = excluded.is_provisional,
                data_quality = excluded.data_quality,
                updated_at = excluded.updated_at
            """,
            (
                ticker,
                pandas_index_date(idx),
                source_id,
                safe_float(row.get("Open")),
                safe_float(row.get("High")),
                safe_float(row.get("Low")),
                adj_close,
                safe_float(row.get("Volume")),
                None,
                adjustment,
                safe_float(row.get("Open")),
                safe_float(row.get("High")),
                safe_float(row.get("Low")),
                raw_close,
                adj_close,
                (adj_close / raw_close) if raw_close else None,
                safe_float(row.get("Dividend")),
                None,
                "norgatedata_total_return",
                timestamp,
                timestamp,
            ),
        )
        inserted += 1
    return inserted


def main() -> int:
    configure_logging()
    args = parse_args()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    base_dir = config_path.parent
    db_path = args.db.expanduser().resolve() if args.db else resolve_path(cfg_get(config, "paths.database_path"), base_dir=base_dir)
    candidates_path = args.candidates.expanduser().resolve()
    output_csv = args.output_csv.expanduser().resolve()

    try:
        import norgatedata  # pyright: ignore[reportMissingImports]
    except ImportError as exc:
        raise SystemExit("norgatedata package is not installed in this Python environment.") from exc

    rows, fieldnames = read_candidates(candidates_path)
    delisted_symbols = set(norgatedata.database_symbols("US Equities Delisted"))
    current_symbols = set(norgatedata.database_symbols("US Equities"))
    report_rows: list[dict[str, Any]] = []
    total_loaded = 0

    with connect(db_path, timeout_sec=float(cfg_get(config, "sqlite_timeout_sec", 30.0) or 30.0)) as conn:
        init_db(conn)
        for row in rows:
            ticker = normalize_ticker(row.get("ticker"))
            if not ticker:
                continue
            match = choose_symbol(norgatedata, row, delisted_symbols=delisted_symbols, current_symbols=current_symbols)
            status = match.reason if match.source_symbol is None else "mapped"
            loaded_rows = 0
            first_bar = ""
            last_bar = ""
            last_raw_close: float | None = None
            last_adjusted_close: float | None = None
            error = ""
            if match.source_symbol:
                end_date = last_quoted_date(norgatedata, match.source_symbol)
                try:
                    prices = fetch_prices(norgatedata, match.source_symbol, args.start_date, end_date or "2100-01-01")
                    if prices.empty:
                        status = "no_price_bars"
                    else:
                        first_bar = min(pandas_index_date(idx) for idx in prices.index)
                        last_bar = max(pandas_index_date(idx) for idx in prices.index)
                        last_row = prices.iloc[-1]
                        last_raw_close = safe_float(last_row.get("Unadjusted Close")) or safe_float(last_row.get("Close"))
                        last_adjusted_close = safe_float(last_row.get("AdjClose"))
                        loaded_rows = len(prices)
                        if not args.dry_run:
                            loaded_rows = upsert_market_bars(
                                conn,
                                ticker=ticker,
                                source_id=args.source_id,
                                source_symbol=match.source_symbol,
                                match_reason=match.reason,
                                prices=prices,
                            )
                            total_loaded += loaded_rows
                        status = "loaded"
                        row["norgate_symbol"] = match.source_symbol
                        row["norgate_security_name"] = match.security_name
                        row["norgate_mapping_reason"] = match.reason
                        row["norgate_first_bar_date"] = first_bar
                        row["norgate_last_bar_date"] = last_bar
                        row["norgate_bar_count"] = str(loaded_rows)
                        row["delisting_date"] = row.get("delisting_date") or last_bar
                        row["price_start_date"] = row.get("price_start_date") or first_bar
                        row["price_end_date"] = row.get("price_end_date") or last_bar
                        row["terminal_consideration"] = row.get("terminal_consideration") or TERMINAL_CONSIDERATION_OVERRIDES.get(ticker, "")
                        if row.get("verification_status") == "pending_norgate_sec_price_identity":
                            row["verification_status"] = "norgate_price_identity_mapped_pending_final_review"
                except Exception as exc:  # noqa: BLE001
                    status = "error"
                    error = repr(exc)
            report_rows.append(
                {
                    "ticker": ticker,
                    "company_name": row.get("company_name", ""),
                    "proposed_cohort": row.get("proposed_cohort", ""),
                    "exchange_ticker": ticker,
                    "norgate_symbol": match.source_symbol or "",
                    "norgate_security_name": match.security_name,
                    "mapping_reason": match.reason,
                    "status": status,
                    "loaded_rows": loaded_rows,
                    "first_bar_date": first_bar,
                    "last_bar_date": last_bar,
                    "last_raw_close": "" if last_raw_close is None else round(last_raw_close, 6),
                    "last_adjusted_close": "" if last_adjusted_close is None else round(last_adjusted_close, 6),
                    "error": error,
                }
            )
        if not args.dry_run:
            conn.commit()

    if not args.dry_run and not args.no_update_candidates:
        write_candidates(candidates_path, rows, fieldnames)
    write_report(output_csv, report_rows)
    summary = {
        "candidate_count": len(rows),
        "loaded_count": sum(1 for row in report_rows if row["status"] == "loaded"),
        "loaded_rows": total_loaded if not args.dry_run else sum(int(row["loaded_rows"] or 0) for row in report_rows),
        "output_csv": str(output_csv),
        "candidate_csv_updated": bool(not args.dry_run and not args.no_update_candidates),
    }
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
