#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from typing import Any


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from med_devices.core.config import cfg_get, load_yaml, resolve_path  # noqa: E402
from med_devices.core.db import connect, finish_run, init_db, start_run, utc_now  # noqa: E402
from med_devices.core.logging_utils import configure_utc_logging  # noqa: E402
from med_devices.core.text_norm import normalize_ticker  # noqa: E402


DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
FIELDNAMES = ["source_table", "rows_imported"]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Import med-device external positioning facts from local staging DBs.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--market-positioning-db", type=Path, default=None)
    parser.add_argument("--sec-form4-db", type=Path, default=None)
    parser.add_argument("--history-start", default="")
    parser.add_argument("--asof", default="")
    parser.add_argument("--output-csv", type=Path, default=None)
    return parser.parse_args()


def to_float(raw: object) -> float | None:
    try:
        value = float(str(raw).replace(",", "").strip())
    except (TypeError, ValueError):
        return None
    return value if value == value else None


def int_flag(raw: object) -> int:
    text = str(raw or "").strip().lower()
    return 1 if text in {"1", "true", "yes", "y"} else 0


def parse_sec_date(raw: object) -> str:
    text = str(raw or "").strip()
    if not text:
        return ""
    for fmt in ("%Y-%m-%d", "%d-%b-%Y", "%d-%b-%y"):
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            continue
    return text


def ensure_source(conn: Any, source_id: str, *, name: str, source_type: str = "local_staging_db") -> None:
    now = utc_now()
    conn.execute(
        """
        INSERT INTO source_registry(
            source_id, stage, source_name, source_type, base_url,
            authentication_required, free_key_required, priority, status, created_at, updated_at
        )
        VALUES (?, 'stage_1', ?, ?, 'local_staging_db', 0, 0, 65, 'planned', ?, ?)
        ON CONFLICT(source_id) DO UPDATE SET updated_at = excluded.updated_at
        """,
        (source_id, name, source_type, now, now),
    )


def company_map(conn: Any) -> dict[str, dict[str, Any]]:
    rows = conn.execute("SELECT company_id, ticker FROM dim_company WHERE is_active = 1").fetchall()
    return {normalize_ticker(row["ticker"]): dict(row) for row in rows}


def qmarks(values: list[str]) -> str:
    return ",".join("?" for _ in values)


def import_short_interest(conn: Any, mp_conn: sqlite3.Connection, *, companies: dict[str, dict[str, Any]], start: str, asof: str) -> int:
    source_id = "finra_equity_short_interest"
    ensure_source(conn, source_id, name="FINRA equity short interest snapshots")
    tickers = sorted(companies)
    rows = mp_conn.execute(
        f"""
        SELECT *
        FROM short_interest_snapshots
        WHERE ticker IN ({qmarks(tickers)})
          AND asof_date >= ?
          AND asof_date <= ?
        """,
        (*tickers, start, asof),
    ).fetchall()
    now = utc_now()
    conn.executemany(
        """
        INSERT INTO fact_short_interest(
            ticker, settlement_date, source_id, company_id, short_interest, avg_daily_volume,
            days_to_cover, float_shares, short_interest_pct_float, publication_date,
            payload_json, created_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(ticker, settlement_date, source_id) DO UPDATE SET
            company_id = excluded.company_id,
            short_interest = excluded.short_interest,
            days_to_cover = excluded.days_to_cover,
            float_shares = excluded.float_shares,
            short_interest_pct_float = excluded.short_interest_pct_float,
            publication_date = excluded.publication_date,
            payload_json = excluded.payload_json,
            updated_at = excluded.updated_at
        """,
        [
            (
                normalize_ticker(row["ticker"]),
                row["settlement_date"],
                source_id,
                int(companies[normalize_ticker(row["ticker"])]["company_id"]),
                to_float(row["short_interest_shares"]),
                to_float(row["days_to_cover"]),
                to_float(row["float_shares"]),
                to_float(row["short_interest_pct_float"]),
                row["publication_date"],
                json.dumps(dict(row), sort_keys=True, ensure_ascii=True),
                now,
                now,
            )
            for row in rows
        ],
    )
    return len(rows)


def import_borrow(conn: Any, mp_conn: sqlite3.Connection, *, companies: dict[str, dict[str, Any]], start: str, asof: str) -> int:
    source_id = "ibkr_borrow"
    ensure_source(conn, source_id, name="Interactive Brokers shortable shares and borrow fee", source_type="broker_api")
    tickers = sorted(companies)
    share_rows = mp_conn.execute(
        f"""
        SELECT *
        FROM ibkr_shortable_shares_snapshots
        WHERE ticker IN ({qmarks(tickers)})
          AND asof_date >= ?
          AND asof_date <= ?
        """,
        (*tickers, start, asof),
    ).fetchall()
    fee_rows = mp_conn.execute(
        f"""
        SELECT *
        FROM ibkr_borrow_fee_rate_daily
        WHERE ticker IN ({qmarks(tickers)})
          AND asof_date >= ?
          AND asof_date <= ?
        """,
        (*tickers, start, asof),
    ).fetchall()
    now = utc_now()
    conn.executemany(
        """
        INSERT INTO fact_ibkr_borrow_snapshot(
            ticker, asof_date, source_id, company_id, shortable_status, shortable_shares,
            borrow_fee_rate, source_timestamp, payload_json, created_at, updated_at
        )
        VALUES (?, ?, ?, ?, NULL, ?, NULL, ?, ?, ?, ?)
        ON CONFLICT(ticker, asof_date, source_id) DO UPDATE SET
            company_id = excluded.company_id,
            shortable_shares = COALESCE(excluded.shortable_shares, fact_ibkr_borrow_snapshot.shortable_shares),
            source_timestamp = COALESCE(NULLIF(excluded.source_timestamp, ''), fact_ibkr_borrow_snapshot.source_timestamp),
            payload_json = excluded.payload_json,
            updated_at = excluded.updated_at
        """,
        [
            (
                normalize_ticker(row["ticker"]),
                row["asof_date"],
                source_id,
                int(companies[normalize_ticker(row["ticker"])]["company_id"]),
                to_float(row["shortable_shares"]),
                row["asof_datetime"],
                json.dumps(dict(row), sort_keys=True, ensure_ascii=True),
                now,
                now,
            )
            for row in share_rows
        ],
    )
    conn.executemany(
        """
        INSERT INTO fact_ibkr_borrow_snapshot(
            ticker, asof_date, source_id, company_id, shortable_status, shortable_shares,
            borrow_fee_rate, source_timestamp, payload_json, created_at, updated_at
        )
        VALUES (?, ?, ?, ?, NULL, NULL, ?, '', ?, ?, ?)
        ON CONFLICT(ticker, asof_date, source_id) DO UPDATE SET
            company_id = excluded.company_id,
            borrow_fee_rate = COALESCE(excluded.borrow_fee_rate, fact_ibkr_borrow_snapshot.borrow_fee_rate),
            payload_json = excluded.payload_json,
            updated_at = excluded.updated_at
        """,
        [
            (
                normalize_ticker(row["ticker"]),
                row["asof_date"],
                source_id,
                int(companies[normalize_ticker(row["ticker"])]["company_id"]),
                to_float(row["borrow_fee_rate"]),
                json.dumps(dict(row), sort_keys=True, ensure_ascii=True),
                now,
                now,
            )
            for row in fee_rows
        ],
    )
    return len(share_rows) + len(fee_rows)


def import_13f_snapshots(conn: Any, mp_conn: sqlite3.Connection, *, companies: dict[str, dict[str, Any]], start: str, asof: str) -> int:
    source_id = "sec_13f_edgar"
    ensure_source(conn, source_id, name="SEC Form 13F institutional holdings")
    tickers = sorted(companies)
    raw_rows = mp_conn.execute(
        f"""
        WITH ranked AS (
            SELECT
                UPPER(ticker) AS ticker,
                period_of_report,
                COALESCE(NULLIF(manager_cik, ''), manager_name) AS manager_key,
                COALESCE(cusip, '') AS cusip,
                filing_key,
                filing_date,
                accepted_at,
                COALESCE(shares, 0.0) AS shares,
                COALESCE(market_value, 0.0) AS market_value,
                ROW_NUMBER() OVER (
                    PARTITION BY UPPER(ticker), period_of_report, COALESCE(NULLIF(manager_cik, ''), manager_name), COALESCE(cusip, '')
                    ORDER BY filing_date DESC, accepted_at DESC, filing_key DESC
                ) AS rn
            FROM institutional_13f_holdings
            WHERE UPPER(ticker) IN ({qmarks(tickers)})
              AND period_of_report >= ?
              AND period_of_report <= ?
              AND filing_date <= ?
              AND COALESCE(period_of_report, '') <> ''
              AND UPPER(COALESCE(share_type, '')) IN ('', 'SH')
              AND COALESCE(put_call, '') = ''
        )
        SELECT
            ticker,
            period_of_report,
            SUM(shares) AS institutional_shares,
            SUM(market_value) AS institutional_value,
            COUNT(DISTINCT manager_key) AS manager_count
        FROM ranked
        WHERE rn = 1
        GROUP BY ticker, period_of_report
        ORDER BY ticker, period_of_report
        """,
        (*tickers, start, asof, asof),
    ).fetchall()
    rows: list[dict[str, Any]] = []
    prior_by_ticker: dict[str, float] = {}
    for row in raw_rows:
        ticker = normalize_ticker(row["ticker"])
        shares = to_float(row["institutional_shares"]) or 0.0
        prior_shares = prior_by_ticker.get(ticker)
        delta = (shares - prior_shares) / prior_shares if prior_shares and prior_shares > 0.0 else 0.0
        prior_by_ticker[ticker] = shares
        rows.append(
            {
                "ticker": ticker,
                "period_of_report": row["period_of_report"],
                "institutional_shares": shares,
                "institutional_value": to_float(row["institutional_value"]) or 0.0,
                "manager_count": to_float(row["manager_count"]) or 0.0,
                "institutional_ownership_delta_pct": delta,
            }
        )
    now = utc_now()
    conn.execute(
        """
        DELETE FROM fact_sec_13f_holding
        WHERE source_id = ?
          AND manager_name = 'aggregate_13f_snapshot'
          AND report_date >= ?
          AND report_date <= ?
        """,
        (source_id, start, asof),
    )
    conn.executemany(
        """
        INSERT INTO fact_sec_13f_holding(
            accession_nodash, report_date, source_id, manager_cik, manager_name, ticker,
            company_id, cusip, shares, market_value_usd, manager_count, institutional_ownership_pct,
            institutional_ownership_delta_pct, put_call, investment_discretion, voting_authority_json,
            payload_json, created_at, updated_at
        )
        VALUES (?, ?, ?, '', 'aggregate_13f_snapshot', ?, ?, '', ?, ?, ?, NULL, ?, '', '', '', ?, ?, ?)
        ON CONFLICT DO UPDATE SET
            shares = excluded.shares,
            market_value_usd = excluded.market_value_usd,
            manager_count = excluded.manager_count,
            institutional_ownership_delta_pct = excluded.institutional_ownership_delta_pct,
            payload_json = excluded.payload_json,
            updated_at = excluded.updated_at
        """,
        [
            (
                f"aggregate_{normalize_ticker(row['ticker'])}_{str(row['period_of_report']).replace('-', '')}",
                row["period_of_report"],
                source_id,
                normalize_ticker(row["ticker"]),
                int(companies[normalize_ticker(row["ticker"])]["company_id"]),
                to_float(row["institutional_shares"]),
                to_float(row["institutional_value"]),
                to_float(row["manager_count"]),
                to_float(row["institutional_ownership_delta_pct"]),
                json.dumps(dict(row), sort_keys=True, ensure_ascii=True),
                now,
                now,
            )
            for row in rows
        ],
    )
    return len(rows)


def import_form4(conn: Any, form4_conn: sqlite3.Connection, *, companies: dict[str, dict[str, Any]], start: str, asof: str) -> int:
    source_id = "sec_form4_edgar"
    ensure_source(conn, source_id, name="SEC Form 4 insider transactions")
    tickers = sorted(companies)
    rows = form4_conn.execute(
        f"""
        SELECT
            s.accession_number,
            t.nonderiv_trans_sk AS transaction_id,
            s.issuer_cik,
            s.issuer_trading_symbol AS ticker,
            o.rptowner_cik,
            o.rptowner_name,
            o.officer_title,
            o.is_director,
            o.is_officer,
            o.is_ten_percent_owner,
            t.transaction_date,
            t.transaction_code,
            t.transaction_shares,
            t.transaction_price_per_share,
            t.shares_owned_following_transaction,
            t.direct_or_indirect_ownership,
            t.transaction_acquired_disposed_code,
            s.source_dataset_id
        FROM sec_ownership_submission s
        JOIN sec_ownership_nonderiv_trans t ON t.accession_number = s.accession_number
        LEFT JOIN sec_ownership_reporting_owner o ON o.accession_number = s.accession_number
        WHERE UPPER(s.issuer_trading_symbol) IN ({qmarks(tickers)})
          AND t.transaction_code IN ('P', 'S')
        """,
        tickers,
    ).fetchall()
    now = utc_now()
    filtered = []
    for row in rows:
        trade_date = parse_sec_date(row["transaction_date"])
        if not trade_date or trade_date < start or trade_date > asof:
            continue
        ticker = normalize_ticker(row["ticker"])
        shares = to_float(row["transaction_shares"])
        price = to_float(row["transaction_price_per_share"])
        value = shares * price if shares is not None and price is not None else None
        filtered.append(
            (
                str(row["accession_number"]).replace("-", ""),
                str(row["transaction_id"]),
                source_id,
                int(companies[ticker]["company_id"]),
                ticker,
                str(row["issuer_cik"] or ""),
                str(row["rptowner_cik"] or ""),
                str(row["rptowner_name"] or ""),
                str(row["officer_title"] or ""),
                int_flag(row["is_director"]),
                int_flag(row["is_officer"]),
                int_flag(row["is_ten_percent_owner"]),
                trade_date,
                str(row["transaction_code"] or "").upper(),
                shares,
                price,
                value,
                str(row["direct_or_indirect_ownership"] or ""),
                to_float(row["shares_owned_following_transaction"]),
                0,
                json.dumps(dict(row), sort_keys=True, ensure_ascii=True),
                now,
                now,
            )
        )
    conn.executemany(
        """
        INSERT INTO fact_sec_form4_transaction(
            accession_nodash, transaction_id, source_id, company_id, ticker, issuer_cik,
            reporting_owner_cik, reporting_owner_name, officer_title, is_director, is_officer,
            is_ten_percent_owner, transaction_date, transaction_code, transaction_shares,
            transaction_price, transaction_value_usd, direct_or_indirect, post_transaction_shares,
            derivative_flag, payload_json, created_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(accession_nodash, transaction_id, source_id) DO UPDATE SET
            company_id = excluded.company_id,
            ticker = excluded.ticker,
            reporting_owner_cik = excluded.reporting_owner_cik,
            reporting_owner_name = excluded.reporting_owner_name,
            officer_title = excluded.officer_title,
            is_director = excluded.is_director,
            is_officer = excluded.is_officer,
            is_ten_percent_owner = excluded.is_ten_percent_owner,
            transaction_date = excluded.transaction_date,
            transaction_code = excluded.transaction_code,
            transaction_shares = excluded.transaction_shares,
            transaction_price = excluded.transaction_price,
            transaction_value_usd = excluded.transaction_value_usd,
            direct_or_indirect = excluded.direct_or_indirect,
            post_transaction_shares = excluded.post_transaction_shares,
            payload_json = excluded.payload_json,
            updated_at = excluded.updated_at
        """,
        filtered,
    )
    return len(filtered)


def write_summary(path: Path, rows: list[dict[str, Any]]) -> None:
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
    market_db = (
        args.market_positioning_db.expanduser().resolve()
        if args.market_positioning_db
        else Path(str(cfg_get(config, "external_positioning_import.market_positioning_db_path"))).expanduser().resolve()
    )
    form4_db = (
        args.sec_form4_db.expanduser().resolve()
        if args.sec_form4_db
        else Path(str(cfg_get(config, "external_positioning_import.sec_form4_db_path"))).expanduser().resolve()
    )
    start = args.history_start.strip() or str(cfg_get(config, "external_positioning_import.history_start", "2019-01-01"))
    asof = args.asof.strip() or datetime.now().date().isoformat()
    output_csv = (
        args.output_csv.expanduser().resolve()
        if args.output_csv
        else resolve_path(cfg_get(config, "external_positioning_import.output_csv"), base_dir=base_dir)
    )
    if not market_db.exists():
        raise FileNotFoundError(f"market_positioning DB not found: {market_db}")
    if not form4_db.exists():
        raise FileNotFoundError(f"SEC Form 4 DB not found: {form4_db}")

    with connect(db_path, timeout_sec=float(cfg_get(config, "runtime.sqlite_timeout_sec", 30.0))) as conn:
        init_db(conn)
        run_id = start_run(conn, run_type="import_med_device_external_positioning_facts", input_path=config_path)
        try:
            companies = company_map(conn)
            mp_conn = sqlite3.connect(str(market_db))
            mp_conn.row_factory = sqlite3.Row
            form4_conn = sqlite3.connect(str(form4_db))
            form4_conn.row_factory = sqlite3.Row
            rows = [
                {
                    "source_table": "short_interest_snapshots",
                    "rows_imported": import_short_interest(conn, mp_conn, companies=companies, start=start, asof=asof),
                },
                {
                    "source_table": "ibkr_borrow",
                    "rows_imported": import_borrow(conn, mp_conn, companies=companies, start=start, asof=asof),
                },
                {
                    "source_table": "institutional_13f_ownership_snapshots",
                    "rows_imported": import_13f_snapshots(conn, mp_conn, companies=companies, start=start, asof=asof),
                },
                {
                    "source_table": "sec_ownership_nonderiv_trans",
                    "rows_imported": import_form4(conn, form4_conn, companies=companies, start=start, asof=asof),
                },
            ]
            write_summary(output_csv, rows)
            total = sum(int(row["rows_imported"]) for row in rows)
            finish_run(conn, run_id=run_id, status="success", row_count=total, message=f"start={start} asof={asof} rows={total}")
            print(f"imported_rows={total} output={output_csv}")
        except BaseException as exc:
            finish_run(conn, run_id=run_id, status="failed", row_count=0, message=f"{type(exc).__name__}: {exc}")
            raise


if __name__ == "__main__":
    main()
