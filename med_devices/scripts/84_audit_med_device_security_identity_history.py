#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import sqlite3
import sys
from pathlib import Path
from typing import Any, cast


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from med_devices.core.config import cfg_get, load_yaml, resolve_path  # noqa: E402
from med_devices.core.db import connect, init_db  # noqa: E402
from med_devices.core.security_identity import load_primary_security_identity_windows  # noqa: E402
from med_devices.core.text_norm import normalize_ticker  # noqa: E402


DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
POSITIONING_FACTS = (
    ("fact_short_interest", "settlement_date"),
    ("fact_finra_short_volume", "trade_date"),
    ("fact_ibkr_borrow_snapshot", "asof_date"),
    ("fact_sec_13f_holding", "report_date"),
    ("fact_sec_form4_transaction", "transaction_date"),
)
FIELDS = (
    "ticker",
    "company_id",
    "listing_start_date",
    "price_row_count",
    "price_start_date",
    "price_end_date",
    "financial_history_start_date",
    "financial_row_count",
    "financial_period_start_date",
    "financial_period_end_date",
    "financial_data_quality_status",
    "financial_missing_fields",
    "short_interest_row_count",
    "short_interest_latest_date",
    "short_volume_row_count",
    "short_volume_latest_date",
    "borrow_row_count",
    "borrow_latest_date",
    "sec_13f_row_count",
    "sec_13f_latest_date",
    "form4_row_count",
    "form4_latest_date",
    "prelisting_fact_row_count",
    "availability_status",
    "issues",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Audit governed med-device issuer history, identity boundaries, and source availability."
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--asof", default="")
    parser.add_argument("--tickers", default="")
    parser.add_argument("--output-csv", type=Path, default=None)
    return parser.parse_args()


def table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table,),
        ).fetchone()
        is not None
    )


def table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(row["name"]) for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def min_max_count(
    conn: sqlite3.Connection,
    *,
    table: str,
    date_column: str,
    asof: str,
    ticker: str,
    company_id: int,
) -> tuple[int, str, str]:
    if not table_exists(conn, table) or date_column not in table_columns(conn, table):
        return 0, "", ""
    columns = table_columns(conn, table)
    if "company_id" in columns:
        identity_clause = "company_id = ?"
        identity_param: Any = company_id
    elif "ticker" in columns:
        identity_clause = "ticker = ?"
        identity_param = ticker
    else:
        return 0, "", ""
    row = conn.execute(
        f"""
        SELECT COUNT(*) AS n, MIN({date_column}) AS min_date, MAX({date_column}) AS max_date
        FROM {table}
        WHERE {identity_clause}
          AND {date_column} <= ?
        """,
        (identity_param, asof),
    ).fetchone()
    return int(row["n"] or 0), str(row["min_date"] or ""), str(row["max_date"] or "")


def current_financial_quality(
    conn: sqlite3.Connection,
    *,
    company_id: int,
    asof: str,
) -> tuple[str, str]:
    if not table_exists(conn, "feature_financial_valuation"):
        return "", ""
    row = conn.execute(
        """
        SELECT data_quality_status, missing_fields
        FROM feature_financial_valuation
        WHERE company_id = ?
          AND asof_date <= ?
        ORDER BY asof_date DESC
        LIMIT 1
        """,
        (company_id, asof),
    ).fetchone()
    if row is None:
        return "", ""
    return str(row["data_quality_status"] or ""), str(row["missing_fields"] or "")


def prelisting_count(
    conn: sqlite3.Connection,
    *,
    ticker: str,
    company_id: int,
    listing_start: str,
) -> int:
    total = 0
    facts = (("fact_price_ohlcv", "bar_date"), *POSITIONING_FACTS)
    for table, date_column in facts:
        if not table_exists(conn, table):
            continue
        columns = table_columns(conn, table)
        if date_column not in columns:
            continue
        if "company_id" in columns:
            clause = "company_id = ?"
            identity: Any = company_id
        elif "ticker" in columns:
            clause = "ticker = ?"
            identity = ticker
        else:
            continue
        row = conn.execute(
            f"SELECT COUNT(*) AS n FROM {table} WHERE {clause} AND {date_column} < ?",
            (identity, listing_start),
        ).fetchone()
        total += int(row["n"] or 0)
    return total


def build_rows(
    conn: sqlite3.Connection,
    *,
    config: dict[str, Any],
    asof: str,
    ticker_filter: set[str],
) -> list[dict[str, Any]]:
    windows = load_primary_security_identity_windows(conn, active_only=True)
    configured = cfg_get(config, "universe_validation.security_identity_overrides", {})
    governed = {normalize_ticker(value) for value in configured} if isinstance(configured, dict) else set()
    financial_starts = cfg_get(config, "financial_features.financial_history_start_by_ticker", {})
    financial_starts = financial_starts if isinstance(financial_starts, dict) else {}
    expected_missing = cfg_get(config, "financial_features.expected_precommercial_missing_revenue", {})
    expected_missing = expected_missing if isinstance(expected_missing, dict) else {}
    targets = sorted((governed & set(windows)) if not ticker_filter else (ticker_filter & set(windows)))
    rows: list[dict[str, Any]] = []
    for ticker in targets:
        window = windows[ticker]
        listing_start = window.listing_start_date.isoformat() if window.listing_start_date else ""
        price_n, price_start, price_end = min_max_count(
            conn,
            table="fact_price_ohlcv",
            date_column="bar_date",
            asof=asof,
            ticker=ticker,
            company_id=window.company_id,
        )
        financial_start = str(financial_starts.get(ticker) or "")[:10]
        fin_row = conn.execute(
            """
            SELECT COUNT(*) AS n, MIN(period_end) AS min_date, MAX(period_end) AS max_date
            FROM fact_financial_statement
            WHERE company_id = ?
              AND period_end >= ?
              AND period_end <= ?
              AND filed_date <= ?
            """,
            (window.company_id, financial_start or "0001-01-01", asof, asof),
        ).fetchone()
        quality_status, missing_fields = current_financial_quality(
            conn,
            company_id=window.company_id,
            asof=asof,
        )
        positioning: dict[str, tuple[int, str, str]] = {}
        for table, date_column in POSITIONING_FACTS:
            positioning[table] = min_max_count(
                conn,
                table=table,
                date_column=date_column,
                asof=asof,
                ticker=ticker,
                company_id=window.company_id,
            )
        prelisting = prelisting_count(
            conn,
            ticker=ticker,
            company_id=window.company_id,
            listing_start=listing_start,
        )
        issues: list[str] = []
        if not listing_start:
            issues.append("missing_listing_start")
        if prelisting:
            issues.append(f"prelisting_facts={prelisting}")
        if price_n == 0:
            issues.append("missing_price_history")
        expected_financial_reason = str(expected_missing.get(ticker) or "")
        expected_revenue_gap = expected_financial_reason and "revenue_ttm" in missing_fields.split(";")
        if int(fin_row["n"] or 0) == 0 and not expected_financial_reason:
            issues.append("missing_financial_history")
        elif int(fin_row["n"] or 0) == 0 or expected_revenue_gap:
            issues.append(f"expected_financial_gap={expected_financial_reason}")
        if quality_status == "review":
            issues.append("financial_feature_review")
        elif quality_status == "fail" and not expected_revenue_gap:
            issues.append("financial_feature_fail")
        missing_sources = [
            table.removeprefix("fact_")
            for table, values in positioning.items()
            if values[0] == 0
        ]
        if missing_sources:
            issues.append(f"unavailable_optional_sources={','.join(missing_sources)}")
        hard_issues = [item for item in issues if item.startswith(("missing_listing", "prelisting", "missing_price"))]
        status = "fail" if hard_issues else "review" if issues else "pass"
        rows.append(
            {
                "ticker": ticker,
                "company_id": window.company_id,
                "listing_start_date": listing_start,
                "price_row_count": price_n,
                "price_start_date": price_start,
                "price_end_date": price_end,
                "financial_history_start_date": financial_start,
                "financial_row_count": int(fin_row["n"] or 0),
                "financial_period_start_date": str(fin_row["min_date"] or ""),
                "financial_period_end_date": str(fin_row["max_date"] or ""),
                "financial_data_quality_status": quality_status,
                "financial_missing_fields": missing_fields,
                "short_interest_row_count": positioning["fact_short_interest"][0],
                "short_interest_latest_date": positioning["fact_short_interest"][2],
                "short_volume_row_count": positioning["fact_finra_short_volume"][0],
                "short_volume_latest_date": positioning["fact_finra_short_volume"][2],
                "borrow_row_count": positioning["fact_ibkr_borrow_snapshot"][0],
                "borrow_latest_date": positioning["fact_ibkr_borrow_snapshot"][2],
                "sec_13f_row_count": positioning["fact_sec_13f_holding"][0],
                "sec_13f_latest_date": positioning["fact_sec_13f_holding"][2],
                "form4_row_count": positioning["fact_sec_form4_transaction"][0],
                "form4_latest_date": positioning["fact_sec_form4_transaction"][2],
                "prelisting_fact_row_count": prelisting,
                "availability_status": status,
                "issues": ";".join(issues),
            }
        )
    return rows


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS, extrasaction="ignore", lineterminator="\n")
        writer.writeheader()
        writer.writerows(cast(Any, rows))


def main() -> None:
    args = parse_args()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    base_dir = config_path.parent
    db_path = (
        args.db.expanduser().resolve()
        if args.db
        else resolve_path(cfg_get(config, "paths.database_path"), base_dir=base_dir)
    )
    output_csv = (
        args.output_csv.expanduser().resolve()
        if args.output_csv
        else resolve_path(
            "../output/med_devices_reports/security_identity/security_identity_history_audit_latest.csv",
            base_dir=base_dir,
        )
    )
    ticker_filter = {
        normalize_ticker(value)
        for value in str(args.tickers or "").split(",")
        if normalize_ticker(value)
    }
    with connect(db_path, timeout_sec=float(cfg_get(config, "runtime.sqlite_timeout_sec", 30.0))) as conn:
        init_db(conn)
        asof = str(args.asof or "").strip()
        if not asof:
            row = conn.execute("SELECT MAX(bar_date) AS asof FROM fact_price_ohlcv").fetchone()
            asof = str(row["asof"] or "")
        if not asof:
            raise ValueError("Unable to resolve an audit as-of date")
        rows = build_rows(
            conn,
            config=config,
            asof=asof,
            ticker_filter=ticker_filter,
        )
    write_csv(output_csv, rows)
    status_counts: dict[str, int] = {}
    for row in rows:
        status = str(row["availability_status"])
        status_counts[status] = status_counts.get(status, 0) + 1
    print(f"security_identity_history_audit={output_csv} asof={asof} rows={len(rows)} statuses={status_counts}")


if __name__ == "__main__":
    raise SystemExit(main())
