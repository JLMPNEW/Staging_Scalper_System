#!/usr/bin/env python3
"""Apply the Pure Storage/Everpure ticker-lineage migration.

This is a same-company ticker change: CIK 0001474432 moved from PSTG to P.
The migration preserves the existing company_id and all historical facts by
re-keying ticker-bearing technology rows to the new active ticker. PSTG is
kept as an exchange-ticker alias so upstream positioning imports can still map
legacy PSTG records into the active P lineage.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any


PACKAGE_ROOT = Path(__file__).resolve().parents[2]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from technology.core.config import cfg_get, load_yaml, resolve_path  # noqa: E402
from technology.core.db import connect, init_db, utc_now  # noqa: E402
from technology.core.text_norm import normalize_org_name  # noqa: E402


DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
OLD_TICKER = "PSTG"
NEW_TICKER = "P"
CIK = "0001474432"
NEW_COMPANY_NAME = "Everpure, Inc."
OLD_COMPANY_NAME = "Pure Storage, Inc."
SOURCE_ID = "technology_hardware_ticker_seed"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Apply PSTG -> P same-CIK ticker lineage migration.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def quote_ident(name: str) -> str:
    return '"' + str(name).replace('"', '""') + '"'


def ticker_tables(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND sql LIKE '%ticker%' ORDER BY name"
    ).fetchall()
    out: list[str] = []
    for row in rows:
        table = str(row["name"])
        cols = [str(col["name"]) for col in conn.execute(f"PRAGMA table_info({quote_ident(table)})")]
        if "ticker" in cols:
            out.append(table)
    return out


def count_ticker(conn: sqlite3.Connection, table: str, ticker: str) -> int:
    row = conn.execute(
        f"SELECT COUNT(*) AS n FROM {quote_ident(table)} WHERE ticker = ?",
        (ticker,),
    ).fetchone()
    return int(row["n"] or 0) if row is not None else 0


def source_id_or_none(conn: sqlite3.Connection, source_id: str) -> str | None:
    row = conn.execute("SELECT 1 FROM source_registry WHERE source_id = ? LIMIT 1", (source_id,)).fetchone()
    return source_id if row is not None else None


def insert_alias(conn: sqlite3.Connection, *, company_id: int, alias_raw: str, source_id: str | None) -> None:
    alias_raw = str(alias_raw or "").strip()
    alias_norm = normalize_org_name(alias_raw)
    if not alias_raw or not alias_norm:
        return
    exists = conn.execute(
        """
        SELECT 1
        FROM dim_company_alias
        WHERE company_id = ? AND alias_raw = ? AND alias_norm = ?
        LIMIT 1
        """,
        (company_id, alias_raw, alias_norm),
    ).fetchone()
    if exists:
        return
    now = utc_now()
    conn.execute(
        """
        INSERT INTO dim_company_alias(
            company_id, alias_raw, alias_norm, source_id, confidence, is_manual, created_at, updated_at
        )
        VALUES (?, ?, ?, ?, 1.0, 0, ?, ?)
        """,
        (company_id, alias_raw, alias_norm, source_id, now, now),
    )


def insert_identifier(
    conn: sqlite3.Connection,
    *,
    company_id: int,
    identifier_type: str,
    identifier_value: str,
    source_id: str | None,
    confidence: float,
) -> None:
    if not identifier_value:
        return
    now = utc_now()
    conn.execute(
        """
        INSERT INTO dim_identifier(
            company_id, identifier_type, identifier_value, source_id, confidence, created_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(identifier_type, identifier_value) DO UPDATE SET
            company_id = excluded.company_id,
            source_id = COALESCE(excluded.source_id, dim_identifier.source_id),
            confidence = excluded.confidence,
            updated_at = excluded.updated_at
        """,
        (company_id, identifier_type, identifier_value, source_id, float(confidence), now, now),
    )


def apply_migration(conn: sqlite3.Connection, *, dry_run: bool) -> dict[str, Any]:
    old_company = conn.execute(
        "SELECT * FROM dim_company WHERE ticker = ? OR cik = ? ORDER BY CASE WHEN ticker = ? THEN 0 ELSE 1 END LIMIT 1",
        (OLD_TICKER, CIK, OLD_TICKER),
    ).fetchone()
    new_company = conn.execute("SELECT * FROM dim_company WHERE ticker = ?", (NEW_TICKER,)).fetchone()
    if old_company is None and new_company is None:
        raise RuntimeError(f"No company row found for {OLD_TICKER}, {NEW_TICKER}, or CIK {CIK}.")
    if old_company is not None and new_company is not None and int(old_company["company_id"]) != int(new_company["company_id"]):
        raise RuntimeError(
            f"Refusing to merge separate company rows: {OLD_TICKER} company_id={old_company['company_id']} "
            f"{NEW_TICKER} company_id={new_company['company_id']}"
        )

    company_row = new_company if old_company is None else old_company
    company_id = int(company_row["company_id"])
    source_id = source_id_or_none(conn, SOURCE_ID)
    tables = ticker_tables(conn)
    before_counts = {
        table: {
            OLD_TICKER: count_ticker(conn, table, OLD_TICKER),
            NEW_TICKER: count_ticker(conn, table, NEW_TICKER),
        }
        for table in tables
    }
    conflicts = [
        table
        for table, counts in before_counts.items()
        if counts[OLD_TICKER] > 0 and counts[NEW_TICKER] > 0
    ]
    if conflicts:
        raise RuntimeError(f"Refusing ticker re-key because both {OLD_TICKER} and {NEW_TICKER} rows exist in: {conflicts}")

    update_counts: dict[str, int] = {}
    if not dry_run:
        now = utc_now()
        with conn:
            for table in tables:
                before = conn.total_changes
                conn.execute(
                    f"UPDATE {quote_ident(table)} SET ticker = ? WHERE ticker = ?",
                    (NEW_TICKER, OLD_TICKER),
                )
                changed = conn.total_changes - before
                if changed:
                    update_counts[table] = changed

            conn.execute(
                """
                UPDATE dim_company
                SET company_name = ?, cik = ?, universe_status = 'keep', is_active = 1,
                    data_quality_status = 'complete', updated_at = ?
                WHERE company_id = ?
                """,
                (NEW_COMPANY_NAME, CIK, now, company_id),
            )
            conn.execute(
                """
                UPDATE dim_security
                SET ticker = ?, listing_status = 'active', updated_at = ?
                WHERE company_id = ? AND ticker = ?
                """,
                (NEW_TICKER, now, company_id, OLD_TICKER),
            )
            insert_alias(conn, company_id=company_id, alias_raw=NEW_TICKER, source_id=source_id)
            insert_alias(conn, company_id=company_id, alias_raw=OLD_TICKER, source_id=source_id)
            insert_alias(conn, company_id=company_id, alias_raw=NEW_COMPANY_NAME, source_id=source_id)
            insert_alias(conn, company_id=company_id, alias_raw=OLD_COMPANY_NAME, source_id=source_id)
            insert_identifier(
                conn,
                company_id=company_id,
                identifier_type="CIK",
                identifier_value=CIK,
                source_id=source_id_or_none(conn, "sec_company_tickers"),
                confidence=1.0,
            )
            insert_identifier(
                conn,
                company_id=company_id,
                identifier_type="EXCHANGE_TICKER",
                identifier_value=OLD_TICKER,
                source_id=source_id,
                confidence=0.95,
            )
            insert_identifier(
                conn,
                company_id=company_id,
                identifier_type="HISTORICAL_EXCHANGE_TICKER",
                identifier_value=f"{NEW_TICKER}:{OLD_TICKER}",
                source_id=source_id,
                confidence=0.95,
            )
    else:
        update_counts = {
            table: counts[OLD_TICKER]
            for table, counts in before_counts.items()
            if counts[OLD_TICKER] > 0
        }

    after_counts = {
        table: {
            OLD_TICKER: count_ticker(conn, table, OLD_TICKER),
            NEW_TICKER: count_ticker(conn, table, NEW_TICKER),
        }
        for table in tables
    }
    return {
        "dry_run": int(dry_run),
        "company_id": company_id,
        "old_ticker": OLD_TICKER,
        "new_ticker": NEW_TICKER,
        "cik": CIK,
        "planned_or_applied_updates": update_counts,
        "before_counts": before_counts,
        "after_counts": after_counts,
    }


def main() -> int:
    args = parse_args()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    base_dir = config_path.parent
    db_path = args.db.expanduser().resolve() if args.db else resolve_path(cfg_get(config, "paths.database_path"), base_dir=base_dir)
    with connect(db_path, timeout_sec=float(cfg_get(config, "runtime.sqlite_timeout_sec", 120.0))) as conn:
        init_db(conn)
        result = apply_migration(conn, dry_run=bool(args.dry_run))
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
