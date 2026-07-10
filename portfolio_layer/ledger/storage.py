from __future__ import annotations

import csv
import sqlite3
from pathlib import Path
from typing import Iterable

from portfolio_layer.core.db import utc_now
from portfolio_layer.ledger.ledger_common import (
    CASH_REPORT_FIELDS,
    CASH_TRANSACTION_FIELDS,
    DIVIDEND_FIELDS,
    FEE_FIELDS,
    HOLDING_LOT_FIELDS,
    HOLDING_STATE_FIELDS,
    INSTRUMENT_FIELDS,
    NET_STOCK_POSITION_FIELDS,
    OPEN_POSITION_FIELDS,
    SECURITIES_LENDING_FIELDS,
    STATEMENT_META_FIELDS,
    TRADE_FIELDS,
    TRADE_IDENTITY_FIELDS,
    parse_number,
)


LEDGER_DDL = """
CREATE TABLE IF NOT EXISTS broker_statement_sources (
    source_sha256 TEXT PRIMARY KEY,
    source_file TEXT NOT NULL,
    broker_name TEXT,
    title TEXT,
    period TEXT,
    period_start TEXT,
    period_end TEXT,
    when_generated TEXT,
    account TEXT,
    account_id TEXT,
    accounts_included TEXT,
    base_currency TEXT,
    imported_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS broker_open_positions (
    source_sha256 TEXT NOT NULL,
    statement_end_date TEXT NOT NULL,
    source_row INTEGER NOT NULL,
    asset_category TEXT NOT NULL,
    currency TEXT,
    symbol TEXT NOT NULL,
    quantity REAL,
    multiplier REAL,
    cost_price REAL,
    cost_basis REAL,
    close_price REAL,
    market_value REAL,
    unrealized_pl REAL,
    code TEXT,
    created_at TEXT NOT NULL,
    PRIMARY KEY (source_sha256, asset_category, symbol)
);

CREATE TABLE IF NOT EXISTS broker_net_stock_positions (
    source_sha256 TEXT NOT NULL,
    statement_end_date TEXT NOT NULL,
    source_row INTEGER NOT NULL,
    currency TEXT,
    symbol TEXT NOT NULL,
    description TEXT,
    shares_at_ib REAL,
    shares_borrowed REAL,
    shares_lent REAL,
    net_shares REAL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (source_sha256, symbol)
);

CREATE TABLE IF NOT EXISTS broker_trades (
    trade_key TEXT PRIMARY KEY,
    source_sha256 TEXT NOT NULL,
    statement_end_date TEXT NOT NULL,
    source_row INTEGER NOT NULL,
    asset_category TEXT NOT NULL,
    currency TEXT,
    account TEXT,
    symbol TEXT NOT NULL,
    date_time TEXT,
    trade_date TEXT,
    quantity REAL,
    trade_price REAL,
    close_price REAL,
    proceeds REAL,
    commission_fee REAL,
    basis REAL,
    realized_pl REAL,
    mtm_pl REAL,
    code TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS broker_instruments (
    source_sha256 TEXT NOT NULL,
    statement_end_date TEXT NOT NULL,
    source_row INTEGER NOT NULL,
    asset_category TEXT NOT NULL,
    symbol TEXT NOT NULL,
    description TEXT,
    conid TEXT,
    security_id TEXT,
    underlying TEXT,
    listing_exchange TEXT,
    multiplier REAL,
    instrument_type TEXT,
    expiry TEXT,
    delivery_month TEXT,
    strike REAL,
    code TEXT,
    created_at TEXT NOT NULL,
    PRIMARY KEY (source_sha256, asset_category, symbol, source_row)
);

CREATE TABLE IF NOT EXISTS broker_cash_report (
    source_sha256 TEXT NOT NULL,
    statement_end_date TEXT NOT NULL,
    source_row INTEGER NOT NULL,
    line_item TEXT NOT NULL,
    currency TEXT,
    total REAL,
    securities REAL,
    futures REAL,
    paxos REAL,
    month_to_date REAL,
    year_to_date REAL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (source_sha256, line_item, currency)
);

CREATE TABLE IF NOT EXISTS broker_dividends (
    source_sha256 TEXT NOT NULL,
    statement_end_date TEXT NOT NULL,
    source_row INTEGER NOT NULL,
    currency TEXT,
    date TEXT,
    description TEXT,
    amount REAL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (source_sha256, source_row)
);

CREATE TABLE IF NOT EXISTS broker_cash_transactions (
    source_sha256 TEXT NOT NULL,
    statement_end_date TEXT NOT NULL,
    source_row INTEGER NOT NULL,
    currency TEXT,
    settle_date TEXT,
    description TEXT,
    amount REAL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (source_sha256, source_row)
);

CREATE TABLE IF NOT EXISTS broker_fees (
    source_sha256 TEXT NOT NULL,
    statement_end_date TEXT NOT NULL,
    source_row INTEGER NOT NULL,
    subtitle TEXT,
    currency TEXT,
    date TEXT,
    description TEXT,
    amount REAL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (source_sha256, source_row)
);

CREATE TABLE IF NOT EXISTS broker_securities_lending (
    source_sha256 TEXT NOT NULL,
    statement_end_date TEXT NOT NULL,
    source_row INTEGER NOT NULL,
    section TEXT NOT NULL,
    asset_category TEXT,
    currency TEXT,
    account TEXT,
    symbol TEXT,
    date TEXT,
    activity TEXT,
    transaction_id TEXT,
    quantity REAL,
    rate REAL,
    collateral_amount REAL,
    price REAL,
    value REAL,
    fee_amount REAL,
    interest_amount REAL,
    code TEXT,
    created_at TEXT NOT NULL,
    PRIMARY KEY (source_sha256, section, source_row)
);

CREATE TABLE IF NOT EXISTS holdings_lots (
    run_as_of TEXT NOT NULL,
    asset_category TEXT NOT NULL,
    symbol TEXT NOT NULL,
    lot_id TEXT NOT NULL,
    quantity REAL NOT NULL,
    entry_date TEXT,
    cost_basis REAL,
    cost_price REAL,
    entry_date_unknown INTEGER NOT NULL DEFAULT 0,
    source TEXT,
    provenance TEXT,
    source_sha256 TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (run_as_of, asset_category, symbol, lot_id)
);

CREATE TABLE IF NOT EXISTS holding_state (
    run_as_of TEXT NOT NULL,
    asset_category TEXT NOT NULL,
    currency TEXT,
    symbol TEXT NOT NULL,
    quantity REAL NOT NULL,
    multiplier REAL,
    cost_price REAL,
    cost_basis REAL,
    close_price REAL,
    market_value REAL,
    unrealized_pl REAL,
    source_sha256 TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (run_as_of, asset_category, symbol)
);

CREATE TABLE IF NOT EXISTS broker_reconciliations (
    run_as_of TEXT NOT NULL,
    check_name TEXT NOT NULL,
    status TEXT NOT NULL,
    detail TEXT,
    source_sha256 TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (run_as_of, check_name)
);
"""


TABLE_FIELDS = {
    "broker_statement_sources": STATEMENT_META_FIELDS,
    "broker_open_positions": OPEN_POSITION_FIELDS,
    "broker_net_stock_positions": NET_STOCK_POSITION_FIELDS,
    "broker_trades": TRADE_FIELDS,
    "broker_instruments": INSTRUMENT_FIELDS,
    "broker_cash_report": CASH_REPORT_FIELDS,
    "broker_dividends": DIVIDEND_FIELDS,
    "broker_cash_transactions": CASH_TRANSACTION_FIELDS,
    "broker_fees": FEE_FIELDS,
    "broker_securities_lending": SECURITIES_LENDING_FIELDS,
    "holdings_lots": HOLDING_LOT_FIELDS,
    "holding_state": HOLDING_STATE_FIELDS,
}

NUMERIC_COLUMNS = {
    "source_row",
    "quantity",
    "multiplier",
    "cost_price",
    "cost_basis",
    "close_price",
    "market_value",
    "unrealized_pl",
    "shares_at_ib",
    "shares_borrowed",
    "shares_lent",
    "net_shares",
    "trade_price",
    "proceeds",
    "commission_fee",
    "basis",
    "realized_pl",
    "mtm_pl",
    "total",
    "securities",
    "futures",
    "paxos",
    "month_to_date",
    "year_to_date",
    "amount",
    "rate",
    "collateral_amount",
    "price",
    "value",
    "fee_amount",
    "interest_amount",
    "strike",
    "entry_date_unknown",
}


def init_ledger_tables(conn: sqlite3.Connection) -> None:
    with conn:
        conn.executescript(LEDGER_DDL)


def _to_db_value(key: str, value: str) -> object:
    if key in NUMERIC_COLUMNS:
        number = parse_number(value)
        if number is None:
            return None
        if key in {"source_row", "entry_date_unknown"}:
            return int(number)
        return float(number)
    return value


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def replace_source_rows(conn: sqlite3.Connection, table: str, rows: Iterable[dict[str, str]], source_sha256: str) -> int:
    materialized = list(rows)
    fields = TABLE_FIELDS[table]
    placeholders = ", ".join("?" for _ in [*fields, "created_at"])
    columns = ", ".join([*fields, "created_at"])
    count = 0
    now = utc_now()
    with conn:
        conn.execute(f"DELETE FROM {table} WHERE source_sha256 = ?", (source_sha256,))
        if table == "broker_trades":
            # A later overlapping IB statement replaces earlier copies of the same economic fill.
            # Repeated identical fills in this statement survive because deletion happens once,
            # before its occurrence-ordinal keys are inserted.
            identities = {
                tuple(_to_db_value(field, row.get(field, "")) for field in TRADE_IDENTITY_FIELDS)
                for row in materialized
            }
            where = " AND ".join(f"{field} IS ?" for field in TRADE_IDENTITY_FIELDS)
            for identity in identities:
                conn.execute(f"DELETE FROM broker_trades WHERE {where}", identity)
        insert_verb = "INSERT OR REPLACE" if table == "broker_trades" else "INSERT"
        for row in materialized:
            values = [_to_db_value(field, row.get(field, "")) for field in fields]
            conn.execute(f"{insert_verb} INTO {table} ({columns}) VALUES ({placeholders})", [*values, now])
            count += 1
    return count


def replace_statement_source(conn: sqlite3.Connection, meta: dict[str, str]) -> None:
    fields = STATEMENT_META_FIELDS
    placeholders = ", ".join("?" for _ in [*fields, "imported_at"])
    columns = ", ".join([*fields, "imported_at"])
    values = [meta.get(field, "") for field in fields]
    with conn:
        conn.execute("DELETE FROM broker_statement_sources WHERE source_sha256 = ?", (meta["source_sha256"],))
        conn.execute(f"INSERT INTO broker_statement_sources ({columns}) VALUES ({placeholders})", [*values, utc_now()])


def replace_run_rows(conn: sqlite3.Connection, table: str, rows: Iterable[dict[str, str]], run_as_of: str) -> int:
    fields = TABLE_FIELDS[table]
    placeholders = ", ".join("?" for _ in [*fields, "created_at"])
    columns = ", ".join([*fields, "created_at"])
    count = 0
    now = utc_now()
    with conn:
        conn.execute(f"DELETE FROM {table} WHERE run_as_of = ?", (run_as_of,))
        for row in rows:
            values = [_to_db_value(field, row.get(field, "")) for field in fields]
            conn.execute(f"INSERT INTO {table} ({columns}) VALUES ({placeholders})", [*values, now])
            count += 1
    return count


def replace_reconciliations(
    conn: sqlite3.Connection,
    rows: Iterable[dict[str, str]],
    *,
    run_as_of: str,
    source_sha256: str,
) -> int:
    count = 0
    now = utc_now()
    with conn:
        conn.execute("DELETE FROM broker_reconciliations WHERE run_as_of = ?", (run_as_of,))
        for row in rows:
            conn.execute(
                """
                INSERT INTO broker_reconciliations(run_as_of, check_name, status, detail, source_sha256, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (run_as_of, row.get("check", ""), row.get("status", ""), row.get("detail", ""), source_sha256, now),
            )
            count += 1
    return count


def count_for_source(conn: sqlite3.Connection, table: str, source_sha256: str) -> int:
    row = conn.execute(f"SELECT COUNT(*) AS n FROM {table} WHERE source_sha256 = ?", (source_sha256,)).fetchone()
    return int(row["n"]) if row is not None else 0


def count_for_run(conn: sqlite3.Connection, table: str, run_as_of: str) -> int:
    row = conn.execute(f"SELECT COUNT(*) AS n FROM {table} WHERE run_as_of = ?", (run_as_of,)).fetchone()
    return int(row["n"]) if row is not None else 0


def read_artifact_rows(path: Path) -> list[dict[str, str]]:
    return _read_csv(path)
