#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from med_devices.core.config import cfg_get, load_yaml, resolve_path  # noqa: E402
from med_devices.core.db import connect, finish_run, init_db, start_run, utc_now  # noqa: E402
from med_devices.core.logging_utils import configure_utc_logging  # noqa: E402


DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
QUARANTINE_TABLE = "security_identity_fact_quarantine"


@dataclass(frozen=True)
class FactIdentitySpec:
    table: str
    date_column: str


FACT_IDENTITY_SPECS = (
    FactIdentitySpec("fact_price_ohlcv", "bar_date"),
    FactIdentitySpec("fact_market_snapshot", "asof_date"),
    FactIdentitySpec("fact_short_interest", "settlement_date"),
    FactIdentitySpec("fact_finra_short_volume", "trade_date"),
    FactIdentitySpec("fact_ibkr_borrow_snapshot", "asof_date"),
    FactIdentitySpec("fact_sec_13f_holding", "report_date"),
    FactIdentitySpec("fact_sec_form4_transaction", "transaction_date"),
)

OUTPUT_FIELDS = (
    "source_table",
    "source_rowid",
    "source_row_hash",
    "company_id",
    "ticker",
    "observation_date",
    "listing_start_date",
    "action",
    "reason",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Quarantine facts that predate the governed identity window of a reused ticker."
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--output-csv", type=Path, default=None)
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def table_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    return [str(row["name"]) for row in conn.execute(f"PRAGMA table_info({table})").fetchall()]


def table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table,),
        ).fetchone()
        is not None
    )


def ensure_quarantine_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {QUARANTINE_TABLE}(
            quarantine_id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_table TEXT NOT NULL,
            source_row_hash TEXT NOT NULL,
            company_id INTEGER,
            ticker TEXT NOT NULL,
            observation_date TEXT NOT NULL,
            listing_start_date TEXT NOT NULL,
            reason TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            quarantined_at TEXT NOT NULL,
            UNIQUE(source_table, source_row_hash)
        )
        """
    )


def violating_rows(conn: sqlite3.Connection, spec: FactIdentitySpec) -> list[dict[str, Any]]:
    if not table_exists(conn, spec.table):
        return []
    columns = table_columns(conn, spec.table)
    if spec.date_column not in columns or "ticker" not in columns:
        return []
    if "company_id" in columns:
        company_join = "c.company_id = f.company_id OR (f.company_id IS NULL AND c.ticker = f.ticker)"
    else:
        company_join = "c.ticker = f.ticker"
    rows = conn.execute(
        f"""
        SELECT
            f.rowid AS source_rowid,
            f.*,
            c.company_id AS identity_company_id,
            c.ticker AS identity_ticker,
            s.listing_start_date AS identity_listing_start_date
        FROM {spec.table} f
        JOIN dim_company c ON ({company_join})
        JOIN dim_security s
          ON s.company_id = c.company_id
         AND COALESCE(s.is_primary_listing, 0) = 1
        WHERE COALESCE(TRIM(s.listing_start_date), '') <> ''
          AND COALESCE(TRIM(f.{spec.date_column}), '') <> ''
          AND f.{spec.date_column} < s.listing_start_date
        ORDER BY c.ticker, f.{spec.date_column}, f.rowid
        """
    ).fetchall()
    out: list[dict[str, Any]] = []
    source_columns = ["source_rowid", *columns]
    for row in rows:
        payload = {column: row[column] for column in source_columns}
        serialized = json.dumps(payload, sort_keys=True, ensure_ascii=True, default=str)
        source_hash = hashlib.sha256(f"{spec.table}|{serialized}".encode("utf-8")).hexdigest()
        out.append(
            {
                "source_table": spec.table,
                "source_rowid": int(row["source_rowid"]),
                "source_row_hash": source_hash,
                "company_id": int(row["identity_company_id"]),
                "ticker": str(row["identity_ticker"] or ""),
                "observation_date": str(row[spec.date_column] or "")[:10],
                "listing_start_date": str(row["identity_listing_start_date"] or "")[:10],
                "reason": "observation_predates_governed_security_identity",
                "payload_json": serialized,
            }
        )
    return out


def repair(conn: sqlite3.Connection, *, dry_run: bool) -> list[dict[str, Any]]:
    violations: list[dict[str, Any]] = []
    for spec in FACT_IDENTITY_SPECS:
        violations.extend(violating_rows(conn, spec))
    if dry_run or not violations:
        return violations

    ensure_quarantine_table(conn)
    quarantined_at = utc_now()
    with conn:
        for row in violations:
            conn.execute(
                f"""
                INSERT INTO {QUARANTINE_TABLE}(
                    source_table, source_row_hash, company_id, ticker, observation_date,
                    listing_start_date, reason, payload_json, quarantined_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(source_table, source_row_hash) DO NOTHING
                """,
                (
                    row["source_table"],
                    row["source_row_hash"],
                    row["company_id"],
                    row["ticker"],
                    row["observation_date"],
                    row["listing_start_date"],
                    row["reason"],
                    row["payload_json"],
                    quarantined_at,
                ),
            )
            conn.execute(
                f"DELETE FROM {row['source_table']} WHERE rowid = ?",
                (row["source_rowid"],),
            )
    return violations


def load_quarantine_report_rows(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    if not table_exists(conn, QUARANTINE_TABLE):
        return []
    return [
        {
            "source_table": row["source_table"],
            "source_rowid": "",
            "source_row_hash": row["source_row_hash"],
            "company_id": row["company_id"],
            "ticker": row["ticker"],
            "observation_date": row["observation_date"],
            "listing_start_date": row["listing_start_date"],
            "reason": row["reason"],
        }
        for row in conn.execute(
            f"""
            SELECT source_table, source_row_hash, company_id, ticker, observation_date,
                   listing_start_date, reason
            FROM {QUARANTINE_TABLE}
            ORDER BY source_table, ticker, observation_date, quarantine_id
            """
        ).fetchall()
    ]


def write_report(path: Path, rows: list[dict[str, Any]], *, dry_run: bool) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=OUTPUT_FIELDS, extrasaction="ignore", lineterminator="\n")
        writer.writeheader()
        for row in rows:
            writer.writerow(
                cast(
                    Any,
                    {
                        **row,
                        "action": "would_quarantine" if dry_run else "quarantined",
                    },
                )
            )


def main() -> None:
    configure_utc_logging()
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
            "../output/med_devices_reports/security_identity/security_identity_fact_quarantine_latest.csv",
            base_dir=base_dir,
        )
    )
    if args.dry_run and args.output_csv is None:
        output_csv = output_csv.with_name(f"{output_csv.stem}_dry_run{output_csv.suffix}")
    with connect(db_path, timeout_sec=float(cfg_get(config, "runtime.sqlite_timeout_sec", 30.0))) as conn:
        init_db(conn)
        run_id = start_run(conn, run_type="repair_med_device_security_identity_facts", input_path=config_path)
        try:
            rows = repair(conn, dry_run=bool(args.dry_run))
            report_rows = rows if args.dry_run else load_quarantine_report_rows(conn)
            write_report(output_csv, report_rows, dry_run=bool(args.dry_run))
            counts: dict[str, int] = {}
            for row in rows:
                table = str(row["source_table"])
                counts[table] = counts.get(table, 0) + 1
            message = f"dry_run={int(args.dry_run)} violations={len(rows)} tables={counts} output={output_csv}"
            finish_run(conn, run_id=run_id, status="success", row_count=len(rows), message=message)
            print(message)
        except BaseException as exc:
            finish_run(conn, run_id=run_id, status="failed", row_count=0, message=f"{type(exc).__name__}: {exc}")
            raise


if __name__ == "__main__":
    raise SystemExit(main())
