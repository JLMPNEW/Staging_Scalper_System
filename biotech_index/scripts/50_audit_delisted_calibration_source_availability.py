#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import re
import sqlite3
import sys
import time
from collections import Counter
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from biotech_index.core.config import cfg_get, load_yaml, resolve_path  # noqa: E402
from biotech_index.core.db import connect  # noqa: E402
from biotech_index.core.text_norm import normalize_ticker  # noqa: E402


DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
DEFAULT_CANDIDATES = PACKAGE_ROOT / "data" / "delisted_biotech_calibration_candidates.csv"
DEFAULT_OUTPUT_ROOT = PROJECT_ROOT / "output" / "biotech_index_reports" / "delisted_calibration_source_availability"
DEFAULT_13F_CUSIP_MAP = PACKAGE_ROOT / "data" / "delisted_13f_cusip_ticker_map.csv"
FORM4_FEATURE_TABLES = ("form4_events_tier1", "form4_buy_events_v1")

ALLOWED_CALIBRATION_COHORTS = frozenset(
    {
        "commercial_profitable_quality_or_mature",
        "commercial_turnaround_or_unprofitable_growth",
        "late_clinical_pivotal_or_registrational",
        "platform_partnered_modality_pipeline",
        "early_clinical_speculative_or_single_asset_pipeline",
    }
)

AVAILABILITY_FIELDS = [
    "ticker",
    "company_name",
    "proposed_cohort",
    "cik",
    "cusip",
    "share_class_figi",
    "norgate_symbol",
    "price_start_date",
    "price_end_date",
    "verification_status",
    "include_in_audit_scope",
    "scope_exclusion_reasons",
    "price_rows",
    "price_min_date",
    "price_max_date",
    "sec_financial_observation_rows_by_cik",
    "sec_financial_latest_filed_date_by_cik",
    "sec_filing_rows_by_company_match",
    "sec_filing_10k_rows_by_company_match",
    "sec_filing_10q_rows_by_company_match",
    "short_interest_rows",
    "short_interest_min_asof",
    "short_interest_max_asof",
    "ibkr_fee_rows",
    "ibkr_fee_min_asof",
    "ibkr_fee_max_asof",
    "ibkr_shortable_rows",
    "ibkr_shortable_min_asof",
    "ibkr_shortable_max_asof",
    "13f_holdings_rows_by_cusip",
    "13f_holdings_min_filing_date_by_cusip",
    "13f_holdings_max_filing_date_by_cusip",
    "13f_holdings_rows_by_ticker",
    "13f_ownership_snapshot_rows_by_ticker",
    "form4_matching_table_count",
    "form4_matching_row_count",
    "form4_matching_buy_event_rows",
    "form4_matching_min_date",
    "form4_matching_max_date",
    "form4_matching_tables",
    "required_source_gaps",
]

MAP_FIELDS = ["ticker", "cusip", "company_name", "cik", "share_class_figi", "norgate_symbol", "source"]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Audit source coverage for delisted biotech calibration candidates using "
            "point-in-time source keys: issuer CIK for SEC/Form 4, CUSIP for 13F, "
            "and historical ticker/date-window for market/borrow/short feeds."
        )
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--market-positioning-db", type=Path, default=None)
    parser.add_argument("--form4-db", type=Path, default=None)
    parser.add_argument("--candidates", type=Path, default=DEFAULT_CANDIDATES)
    parser.add_argument("--output-dir", type=Path, default=None)
    parser.add_argument("--asof", type=str, default="")
    parser.add_argument(
        "--scope",
        choices=["all", "strict"],
        default="strict",
        help="strict excludes active-stock exceptions, identity conflicts, invalid cohorts, and rows without price/CUSIP keys.",
    )
    parser.add_argument(
        "--write-13f-cusip-map",
        type=Path,
        default=DEFAULT_13F_CUSIP_MAP,
        help="Write a CUSIP->ticker map that the SEC 13F importer can use for delisted candidates.",
    )
    return parser.parse_args()


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_asof(raw: str) -> str:
    text = str(raw or "").strip()
    if not text:
        return date.today().isoformat()
    if len(text) == 8 and text.isdigit():
        return f"{text[:4]}-{text[4:6]}-{text[6:]}"
    return date.fromisoformat(text[:10]).isoformat()


def compact_date(raw: str) -> str:
    return parse_asof(raw).replace("-", "")


def normalize_cik(raw: object) -> str:
    digits = re.sub(r"\D", "", str(raw or ""))
    if not digits:
        return ""
    return (digits.lstrip("0") or "0").zfill(10)


def cik_int(raw: object) -> str:
    normalized = normalize_cik(raw)
    return str(int(normalized)) if normalized else ""


def normalize_cusip(raw: object) -> str:
    return re.sub(r"[^A-Z0-9]", "", str(raw or "").upper())[:9]


def parse_int(raw: object, default: int = 0) -> int:
    try:
        return int(float(str(raw or "").strip()))
    except (TypeError, ValueError):
        return default


def parse_flexible_date(raw: object) -> str:
    text = str(raw or "").strip()
    if not text:
        return ""
    text = text.replace("Z", "")
    candidates = [text, text[:19], text[:10]]
    for candidate in candidates:
        for fmt in ("%Y-%m-%d", "%Y%m%d", "%m/%d/%Y", "%d-%b-%Y", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"):
            try:
                return datetime.strptime(candidate, fmt).date().isoformat()
            except ValueError:
                continue
    match = re.search(r"\d{4}-\d{2}-\d{2}", text)
    if match:
        return match.group(0)
    return ""


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None:
            raise ValueError(f"CSV has no header: {path}")
        rows: list[dict[str, str]] = []
        for row in reader:
            clean = {str(key): str(value or "").strip() for key, value in row.items()}
            clean["ticker"] = normalize_ticker(clean.get("ticker"))
            clean["cik"] = normalize_cik(clean.get("cik"))
            clean["cusip"] = normalize_cusip(clean.get("cusip") or clean.get("cusip_or_figi"))
            rows.append(clean)
    return rows


def write_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore", lineterminator="\n")
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in fieldnames})


def connect_readonly(path: Path | None) -> sqlite3.Connection | None:
    if path is None or not path.exists():
        return None
    conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    return conn


def table_exists(conn: sqlite3.Connection | None, table: str) -> bool:
    if conn is None:
        return False
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone() is not None


def table_columns(conn: sqlite3.Connection | None, table: str) -> set[str]:
    if conn is None or not table_exists(conn, table):
        return set()
    return {str(row["name"]) for row in conn.execute(f"PRAGMA table_info({quote_identifier(table)})").fetchall()}


def quote_identifier(raw: str) -> str:
    return '"' + str(raw).replace('"', '""') + '"'


def scalar(conn: sqlite3.Connection | None, sql: str, params: tuple[Any, ...] = ()) -> Any:
    if conn is None:
        return ""
    try:
        row = conn.execute(sql, params).fetchone()
    except sqlite3.Error:
        return ""
    if row is None:
        return ""
    return row[0]


def count_and_dates(
    conn: sqlite3.Connection | None,
    *,
    table: str,
    where_sql: str,
    params: tuple[Any, ...],
    date_col: str,
    start_date: str = "",
    end_date: str = "",
) -> dict[str, Any]:
    if conn is None or not table_exists(conn, table):
        return {"rows": 0, "min_date": "", "max_date": ""}
    table_sql = quote_identifier(table)
    date_sql = quote_identifier(date_col)
    bounded_where = f"({where_sql})"
    bounded_params: list[Any] = list(params)
    if start_date:
        bounded_where += f" AND substr(CAST({date_sql} AS TEXT), 1, 10) >= ?"
        bounded_params.append(start_date)
    if end_date:
        bounded_where += f" AND substr(CAST({date_sql} AS TEXT), 1, 10) <= ?"
        bounded_params.append(end_date)
    try:
        row = conn.execute(
            f"""
            SELECT COUNT(*) AS n, MIN({date_sql}) AS min_date, MAX({date_sql}) AS max_date
            FROM {table_sql}
            WHERE {bounded_where}
            """,
            tuple(bounded_params),
        ).fetchone()
    except sqlite3.Error:
        return {"rows": 0, "min_date": "", "max_date": ""}
    return {
        "rows": int(row["n"] or 0) if row else 0,
        "min_date": str(row["min_date"] or "") if row else "",
        "max_date": str(row["max_date"] or "") if row else "",
    }


def candidate_scope_reasons(row: dict[str, str]) -> list[str]:
    reasons: list[str] = []
    if not row.get("ticker"):
        reasons.append("missing_ticker")
    if not row.get("cusip"):
        reasons.append("missing_cusip")
    if row.get("proposed_cohort") not in ALLOWED_CALIBRATION_COHORTS:
        reasons.append("invalid_cohort")
    if row.get("verification_status") == "active_stock_exception_do_not_promote":
        reasons.append("active_stock_exception")
    if row.get("verification_status") == "identity_conflict_do_not_promote":
        reasons.append("identity_conflict")
    if not row.get("norgate_symbol"):
        reasons.append("missing_norgate_symbol")
    if parse_int(row.get("norgate_bar_count")) < 252:
        reasons.append("insufficient_norgate_bars")
    return reasons


def company_ids_for_candidate(conn: sqlite3.Connection | None, ticker: str, cik: str) -> list[int]:
    if conn is None or not table_exists(conn, "companies"):
        return []
    parts: list[str] = []
    params: list[Any] = []
    if ticker:
        parts.append("UPPER(ticker) = ?")
        params.append(ticker)
    if cik:
        parts.append("printf('%010d', CAST(cik AS INTEGER)) = ?")
        params.append(cik)
    if not parts:
        return []
    try:
        rows = conn.execute(
            f"SELECT company_id FROM companies WHERE {' OR '.join(parts)}",
            tuple(params),
        ).fetchall()
    except sqlite3.Error:
        return []
    return [int(row["company_id"]) for row in rows if row["company_id"] is not None]


def sec_local_counts(conn: sqlite3.Connection | None, ticker: str, cik: str) -> dict[str, Any]:
    company_ids = company_ids_for_candidate(conn, ticker, cik)
    filing_rows = filing_10k = filing_10q = 0
    if conn is not None and company_ids and table_exists(conn, "sec_filings"):
        placeholders = ",".join("?" for _ in company_ids)
        filing_rows = int(
            scalar(conn, f"SELECT COUNT(*) FROM sec_filings WHERE company_id IN ({placeholders})", tuple(company_ids)) or 0
        )
        filing_10k = int(
            scalar(
                conn,
                f"SELECT COUNT(*) FROM sec_filings WHERE company_id IN ({placeholders}) AND form IN ('10-K','10-K/A','20-F','20-F/A','40-F','40-F/A')",
                tuple(company_ids),
            )
            or 0
        )
        filing_10q = int(
            scalar(
                conn,
                f"SELECT COUNT(*) FROM sec_filings WHERE company_id IN ({placeholders}) AND form IN ('10-Q','10-Q/A')",
                tuple(company_ids),
            )
            or 0
        )
    fact_rows = latest_filed = 0, ""
    if conn is not None and table_exists(conn, "financial_fact_observations") and cik:
        result = count_and_dates(
            conn,
            table="financial_fact_observations",
            where_sql="printf('%010d', CAST(cik AS INTEGER)) = ?",
            params=(cik,),
            date_col="filed_date",
        )
        fact_rows = (int(result["rows"]), result["max_date"])
    return {
        "sec_financial_observation_rows_by_cik": fact_rows[0],
        "sec_financial_latest_filed_date_by_cik": fact_rows[1],
        "sec_filing_rows_by_company_match": filing_rows,
        "sec_filing_10k_rows_by_company_match": filing_10k,
        "sec_filing_10q_rows_by_company_match": filing_10q,
    }


def form4_counts(
    conn: sqlite3.Connection | None,
    ticker: str,
    cik: str,
    *,
    start_date: str = "",
    end_date: str = "",
) -> dict[str, Any]:
    if conn is None:
        return {
            "form4_matching_table_count": 0,
            "form4_matching_row_count": 0,
            "form4_matching_buy_event_rows": 0,
            "form4_matching_min_date": "",
            "form4_matching_max_date": "",
            "form4_matching_tables": "",
        }
    tables = [table for table in FORM4_FEATURE_TABLES if table_exists(conn, table)]
    total_rows = 0
    buy_rows = 0
    table_hits: list[str] = []
    min_date = ""
    max_date = ""
    cik_keys = sorted({key for key in (normalize_cik(cik), cik_int(cik)) if key})
    for table in tables:
        cols = table_columns(conn, table)
        ticker_cols = [col for col in ("issuer_trading_symbol", "issuer_symbol") if col in cols]
        cik_cols = [col for col in ("issuer_cik", "issuer_cik_number") if col in cols]
        if not ticker_cols and not cik_cols:
            continue
        date_col = next(
            (
                col
                for col in (
                    "filing_date_sort",
                    "filing_date",
                    "tradable_date",
                    "accepted_at",
                    "as_of_date",
                    "trans_date",
                    "transaction_date",
                    "period_of_report",
                )
                if col in cols
            ),
            "",
        )
        if not date_col:
            continue
        clauses: list[str] = []
        params: list[Any] = []
        for col in ticker_cols:
            clauses.append(f"UPPER({quote_identifier(col)}) = ?")
            params.append(ticker)
        for col in cik_cols:
            placeholders = ",".join("?" for _ in cik_keys)
            clauses.append(f"CAST({quote_identifier(col)} AS TEXT) IN ({placeholders})")
            params.extend(cik_keys)
        if not clauses:
            continue
        result = count_and_dates(
            conn,
            table=table,
            where_sql=" OR ".join(clauses),
            params=tuple(params),
            date_col=date_col,
            start_date=start_date,
            end_date=end_date,
        )
        count = int(result["rows"])
        if count <= 0:
            continue
        total_rows += count
        table_hits.append(f"{table}:{count}")
        min_date = min([value for value in (min_date, result["min_date"]) if value] or [""])
        max_date = max([value for value in (max_date, result["max_date"]) if value] or [""])
        if "buy" in table.lower() or "transaction_code" in cols or "transaction_type" in cols:
            if "buy" in table.lower():
                buy_rows += count
            elif "transaction_code" in cols:
                buy_where = f"({' OR '.join(clauses)}) AND UPPER(transaction_code) IN ('P','BUY')"
                buy_params = list(params)
                date_sql = quote_identifier(date_col)
                if start_date:
                    buy_where += f" AND substr(CAST({date_sql} AS TEXT), 1, 10) >= ?"
                    buy_params.append(start_date)
                if end_date:
                    buy_where += f" AND substr(CAST({date_sql} AS TEXT), 1, 10) <= ?"
                    buy_params.append(end_date)
                buy_rows += int(
                    scalar(
                        conn,
                        f"SELECT COUNT(*) FROM {quote_identifier(table)} WHERE {buy_where}",
                        tuple(buy_params),
                    )
                    or 0
                )
            elif "transaction_type" in cols:
                buy_where = f"({' OR '.join(clauses)}) AND UPPER(transaction_type) LIKE '%BUY%'"
                buy_params = list(params)
                date_sql = quote_identifier(date_col)
                if start_date:
                    buy_where += f" AND substr(CAST({date_sql} AS TEXT), 1, 10) >= ?"
                    buy_params.append(start_date)
                if end_date:
                    buy_where += f" AND substr(CAST({date_sql} AS TEXT), 1, 10) <= ?"
                    buy_params.append(end_date)
                buy_rows += int(
                    scalar(
                        conn,
                        f"SELECT COUNT(*) FROM {quote_identifier(table)} WHERE {buy_where}",
                        tuple(buy_params),
                    )
                    or 0
                )
    return {
        "form4_matching_table_count": len(table_hits),
        "form4_matching_row_count": total_rows,
        "form4_matching_buy_event_rows": buy_rows,
        "form4_matching_min_date": min_date,
        "form4_matching_max_date": max_date,
        "form4_matching_tables": "|".join(table_hits),
    }


def empty_form4_aggregate() -> dict[str, Any]:
    return {
        "form4_matching_table_count": 0,
        "form4_matching_row_count": 0,
        "form4_matching_buy_event_rows": 0,
        "form4_matching_min_date": "",
        "form4_matching_max_date": "",
        "form4_matching_tables": "",
    }


def preload_form4_counts(conn: sqlite3.Connection | None, candidates: list[dict[str, str]]) -> dict[str, dict[str, Any]]:
    """Bulk-load Form 4 counts by candidate.

    The staging Form 4 database can be large.  Row-by-row COUNT(*) queries are
    too slow for a delisted-candidate audit, so this scans each likely Form 4
    table at most once with all candidate tickers/CIKs in the predicate.
    """
    out = {row["ticker"]: empty_form4_aggregate() for row in candidates if row.get("ticker")}
    if conn is None or not out:
        return out

    tickers = sorted(out)
    ciks = sorted(
        {
            key
            for row in candidates
            for key in (normalize_cik(row.get("cik")), cik_int(row.get("cik")))
            if key
        }
    )
    candidate_windows: dict[str, tuple[str, str]] = {
        row["ticker"]: (parse_flexible_date(row.get("price_start_date")), parse_flexible_date(row.get("price_end_date")))
        for row in candidates
        if row.get("ticker")
    }
    ticker_to_candidates: dict[str, set[str]] = {ticker: {ticker} for ticker in tickers}
    cik_to_candidates: dict[str, set[str]] = {}
    for row in candidates:
        ticker = row.get("ticker", "")
        if not ticker:
            continue
        for key in (normalize_cik(row.get("cik")), cik_int(row.get("cik"))):
            if not key:
                continue
            cik_to_candidates.setdefault(key, set()).add(ticker)

    tables = [table for table in FORM4_FEATURE_TABLES if table_exists(conn, table)]

    for table in tables:
        cols = table_columns(conn, table)
        ticker_cols = [col for col in ("issuer_trading_symbol", "issuer_symbol") if col in cols]
        cik_cols = [col for col in ("issuer_cik", "issuer_cik_number") if col in cols]
        if not ticker_cols and not cik_cols:
            continue
        date_col = next(
            (
                col
                for col in (
                    "filing_date_sort",
                    "filing_date",
                    "tradable_date",
                    "accepted_at",
                    "as_of_date",
                    "trans_date",
                    "transaction_date",
                    "period_of_report",
                )
                if col in cols
            ),
            "",
        )
        if not date_col:
            continue

        select_items = [f"{quote_identifier(date_col)} AS __event_date"]
        for idx, col in enumerate(ticker_cols):
            select_items.append(f"UPPER({quote_identifier(col)}) AS __ticker_{idx}")
        for idx, col in enumerate(cik_cols):
            select_items.append(f"CAST({quote_identifier(col)} AS TEXT) AS __cik_{idx}")
        if "transaction_code" in cols:
            select_items.append("UPPER(transaction_code) AS __transaction_code")
        elif "transaction_type" in cols:
            select_items.append("UPPER(transaction_type) AS __transaction_type")
        else:
            select_items.append("'' AS __transaction_code")

        where_parts: list[str] = []
        params: list[Any] = []
        if ticker_cols and tickers:
            placeholders = ",".join("?" for _ in tickers)
            where_parts.extend(f"UPPER({quote_identifier(col)}) IN ({placeholders})" for col in ticker_cols)
            params.extend(tickers * len(ticker_cols))
        if cik_cols and ciks:
            placeholders = ",".join("?" for _ in ciks)
            where_parts.extend(f"CAST({quote_identifier(col)} AS TEXT) IN ({placeholders})" for col in cik_cols)
            params.extend(ciks * len(cik_cols))
        if not where_parts:
            continue
        match_where = f"({' OR '.join(where_parts)})"

        try:
            rows = conn.execute(
                f"""
                SELECT {", ".join(select_items)}
                FROM {quote_identifier(table)}
                WHERE {match_where}
                """,
                tuple(params),
            ).fetchall()
        except sqlite3.Error:
            continue
        if not rows:
            continue

        per_candidate_table_counts: Counter[str] = Counter()
        for item in rows:
            matched: set[str] = set()
            for idx in range(len(ticker_cols)):
                matched.update(ticker_to_candidates.get(str(item[f"__ticker_{idx}"] or "").strip().upper(), set()))
            for idx in range(len(cik_cols)):
                matched.update(cik_to_candidates.get(str(item[f"__cik_{idx}"] or "").strip(), set()))
            if not matched:
                continue
            event_date = parse_flexible_date(item["__event_date"])
            if not event_date:
                continue
            transaction_marker = str(
                item["__transaction_code"] if "__transaction_code" in item.keys() else item["__transaction_type"]
            ).upper()
            is_buy = "buy" in table.lower() or transaction_marker in {"P", "BUY"} or "BUY" in transaction_marker
            for ticker in matched:
                start_date, end_date = candidate_windows.get(ticker, ("", ""))
                if start_date and event_date < start_date:
                    continue
                if end_date and event_date > end_date:
                    continue
                bucket = out.setdefault(ticker, empty_form4_aggregate())
                bucket["form4_matching_row_count"] = int(bucket["form4_matching_row_count"] or 0) + 1
                if is_buy:
                    bucket["form4_matching_buy_event_rows"] = int(bucket["form4_matching_buy_event_rows"] or 0) + 1
                if event_date:
                    bucket["form4_matching_min_date"] = min(
                        [value for value in (bucket["form4_matching_min_date"], event_date) if value] or [""]
                    )
                    bucket["form4_matching_max_date"] = max(
                        [value for value in (bucket["form4_matching_max_date"], event_date) if value] or [""]
                    )
                per_candidate_table_counts[ticker] += 1
        for ticker, count in per_candidate_table_counts.items():
            bucket = out.setdefault(ticker, empty_form4_aggregate())
            existing = [item for item in str(bucket["form4_matching_tables"] or "").split("|") if item]
            existing.append(f"{table}:{count}")
            bucket["form4_matching_tables"] = "|".join(existing)
            bucket["form4_matching_table_count"] = len(existing)
    return out


def audit_candidate(
    row: dict[str, str],
    *,
    biotech_conn: sqlite3.Connection | None,
    market_conn: sqlite3.Connection | None,
    form4_lookup: dict[str, dict[str, Any]],
    strict_scope: bool,
) -> dict[str, Any]:
    ticker = row.get("ticker", "")
    cik = row.get("cik", "")
    cusip = row.get("cusip", "")
    source_start_date = parse_flexible_date(row.get("price_start_date"))
    source_end_date = parse_flexible_date(row.get("price_end_date"))
    exclusion_reasons = candidate_scope_reasons(row)
    include = not exclusion_reasons if strict_scope else True
    out: dict[str, Any] = {
        "ticker": ticker,
        "company_name": row.get("company_name", ""),
        "proposed_cohort": row.get("proposed_cohort", ""),
        "cik": cik,
        "cusip": cusip,
        "share_class_figi": row.get("share_class_figi", ""),
        "norgate_symbol": row.get("norgate_symbol", ""),
        "price_start_date": source_start_date,
        "price_end_date": source_end_date,
        "verification_status": row.get("verification_status", ""),
        "include_in_audit_scope": 1 if include else 0,
        "scope_exclusion_reasons": "|".join(exclusion_reasons),
    }
    price = count_and_dates(
        biotech_conn,
        table="market_bars_daily",
        where_sql="UPPER(ticker) = ?",
        params=(ticker,),
        date_col="bar_date",
        start_date=source_start_date,
        end_date=source_end_date,
    )
    out.update(
        {
            "price_rows": price["rows"],
            "price_min_date": price["min_date"],
            "price_max_date": price["max_date"],
        }
    )
    out.update(sec_local_counts(biotech_conn, ticker, cik))
    short_interest = count_and_dates(
        market_conn,
        table="short_interest_snapshots",
        where_sql="UPPER(ticker) = ?",
        params=(ticker,),
        date_col="asof_date",
        start_date=source_start_date,
        end_date=source_end_date,
    )
    fee = count_and_dates(
        market_conn,
        table="ibkr_borrow_fee_rate_daily",
        where_sql="UPPER(ticker) = ?",
        params=(ticker,),
        date_col="asof_date",
        start_date=source_start_date,
        end_date=source_end_date,
    )
    shortable = count_and_dates(
        market_conn,
        table="ibkr_shortable_shares_snapshots",
        where_sql="UPPER(ticker) = ?",
        params=(ticker,),
        date_col="asof_date",
        start_date=source_start_date,
        end_date=source_end_date,
    )
    f13_cusip = count_and_dates(
        market_conn,
        table="institutional_13f_holdings",
        where_sql="UPPER(cusip) = ?",
        params=(cusip,),
        date_col="filing_date",
        start_date=source_start_date,
        end_date=source_end_date,
    )
    f13_ticker = count_and_dates(
        market_conn,
        table="institutional_13f_holdings",
        where_sql="UPPER(ticker) = ?",
        params=(ticker,),
        date_col="filing_date",
        start_date=source_start_date,
        end_date=source_end_date,
    )
    f13_snap = count_and_dates(
        market_conn,
        table="institutional_13f_ownership_snapshots",
        where_sql="UPPER(ticker) = ?",
        params=(ticker,),
        date_col="asof_date",
        start_date=source_start_date,
        end_date=source_end_date,
    )
    out.update(
        {
            "short_interest_rows": short_interest["rows"],
            "short_interest_min_asof": short_interest["min_date"],
            "short_interest_max_asof": short_interest["max_date"],
            "ibkr_fee_rows": fee["rows"],
            "ibkr_fee_min_asof": fee["min_date"],
            "ibkr_fee_max_asof": fee["max_date"],
            "ibkr_shortable_rows": shortable["rows"],
            "ibkr_shortable_min_asof": shortable["min_date"],
            "ibkr_shortable_max_asof": shortable["max_date"],
            "13f_holdings_rows_by_cusip": f13_cusip["rows"],
            "13f_holdings_min_filing_date_by_cusip": f13_cusip["min_date"],
            "13f_holdings_max_filing_date_by_cusip": f13_cusip["max_date"],
            "13f_holdings_rows_by_ticker": f13_ticker["rows"],
            "13f_ownership_snapshot_rows_by_ticker": f13_snap["rows"],
        }
    )
    out.update(form4_lookup.get(ticker, empty_form4_aggregate()))

    gaps: list[str] = []
    if include:
        if int(out["price_rows"] or 0) <= 0:
            gaps.append("missing_price_history")
        if int(out["sec_financial_observation_rows_by_cik"] or 0) <= 0 and int(out["sec_filing_rows_by_company_match"] or 0) <= 0:
            gaps.append("missing_local_sec_financial_or_filing_rows")
        if int(out["form4_matching_row_count"] or 0) <= 0:
            gaps.append("missing_form4_rows")
        if int(out["13f_holdings_rows_by_cusip"] or 0) <= 0 and int(out["13f_holdings_rows_by_ticker"] or 0) <= 0:
            gaps.append("missing_13f_rows")
    out["required_source_gaps"] = "|".join(gaps)
    return out


def write_13f_cusip_map(path: Path, rows: list[dict[str, Any]]) -> int:
    map_rows: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for row in rows:
        if not int(row.get("include_in_audit_scope") or 0):
            continue
        ticker = str(row.get("ticker") or "").strip().upper()
        cusip = normalize_cusip(row.get("cusip"))
        if not ticker or not cusip:
            continue
        key = (ticker, cusip)
        if key in seen:
            continue
        seen.add(key)
        map_rows.append(
            {
                "ticker": ticker,
                "cusip": cusip,
                "company_name": row.get("company_name", ""),
                "cik": row.get("cik", ""),
                "share_class_figi": row.get("share_class_figi", ""),
                "norgate_symbol": row.get("norgate_symbol", ""),
                "source": "delisted_biotech_calibration_candidates",
            }
        )
    write_csv(path, sorted(map_rows, key=lambda item: (str(item["ticker"]), str(item["cusip"]))), MAP_FIELDS)
    return len(map_rows)


def main() -> int:
    args = parse_args()
    asof = parse_asof(args.asof)
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    base_dir = config_path.parent
    biotech_db = args.db.expanduser().resolve() if args.db else resolve_path(cfg_get(config, "paths.database_path"), base_dir=base_dir)
    market_db = (
        args.market_positioning_db.expanduser().resolve()
        if args.market_positioning_db
        else resolve_path(cfg_get(config, "market_positioning.database_path"), base_dir=base_dir)
    )
    form4_db = (
        args.form4_db.expanduser().resolve()
        if args.form4_db
        else resolve_path(cfg_get(config, "governance_events.form4_db_path"), base_dir=base_dir)
    )
    output_dir = (
        args.output_dir.expanduser().resolve()
        if args.output_dir
        else DEFAULT_OUTPUT_ROOT / compact_date(asof)
    )
    candidates = read_csv(args.candidates.expanduser().resolve())
    strict_scope = args.scope == "strict"

    biotech_conn = connect_readonly(biotech_db)
    market_conn = connect_readonly(market_db)
    form4_conn = connect_readonly(form4_db)
    try:
        form4_lookup = preload_form4_counts(form4_conn, candidates)
        rows = [
            audit_candidate(
                row,
                biotech_conn=biotech_conn,
                market_conn=market_conn,
                form4_lookup=form4_lookup,
                strict_scope=strict_scope,
            )
            for row in candidates
        ]
    finally:
        for conn in (biotech_conn, market_conn, form4_conn):
            if conn is not None:
                conn.close()

    scoped_rows = [row for row in rows if int(row.get("include_in_audit_scope") or 0)]
    gap_counter: Counter[str] = Counter()
    coverage = {
        "price_rows": 0,
        "sec_local_financial_or_filing": 0,
        "form4_rows": 0,
        "13f_rows": 0,
        "short_interest_rows": 0,
        "ibkr_fee_rows": 0,
        "ibkr_shortable_rows": 0,
    }
    for row in scoped_rows:
        for gap in str(row.get("required_source_gaps") or "").split("|"):
            if gap:
                gap_counter[gap] += 1
        if int(row.get("price_rows") or 0) > 0:
            coverage["price_rows"] += 1
        if int(row.get("sec_financial_observation_rows_by_cik") or 0) > 0 or int(row.get("sec_filing_rows_by_company_match") or 0) > 0:
            coverage["sec_local_financial_or_filing"] += 1
        if int(row.get("form4_matching_row_count") or 0) > 0:
            coverage["form4_rows"] += 1
        if int(row.get("13f_holdings_rows_by_cusip") or 0) > 0 or int(row.get("13f_holdings_rows_by_ticker") or 0) > 0:
            coverage["13f_rows"] += 1
        if int(row.get("short_interest_rows") or 0) > 0:
            coverage["short_interest_rows"] += 1
        if int(row.get("ibkr_fee_rows") or 0) > 0:
            coverage["ibkr_fee_rows"] += 1
        if int(row.get("ibkr_shortable_rows") or 0) > 0:
            coverage["ibkr_shortable_rows"] += 1

    output_dir.mkdir(parents=True, exist_ok=True)
    availability_csv = output_dir / "delisted_source_availability.csv"
    summary_json = output_dir / "delisted_source_availability_summary.json"
    write_csv(availability_csv, rows, AVAILABILITY_FIELDS)
    map_count = 0
    if args.write_13f_cusip_map:
        map_count = write_13f_cusip_map(args.write_13f_cusip_map.expanduser().resolve(), rows)
    summary = {
        "created_at": utc_now(),
        "asof_date": asof,
        "scope": args.scope,
        "candidate_count": len(rows),
        "scoped_candidate_count": len(scoped_rows),
        "biotech_db": str(biotech_db),
        "market_positioning_db": str(market_db),
        "form4_db": str(form4_db),
        "coverage_counts": coverage,
        "required_source_gaps": dict(sorted(gap_counter.items())),
        "availability_csv": str(availability_csv),
        "13f_cusip_map_csv": str(args.write_13f_cusip_map.expanduser().resolve()) if args.write_13f_cusip_map else "",
        "13f_cusip_map_rows": map_count,
    }
    summary_json.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
