#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import sys
from datetime import date
from pathlib import Path
from typing import Any


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from med_devices.core.config import cfg_get, load_yaml, resolve_path  # noqa: E402
from med_devices.core.db import connect, init_db  # noqa: E402
from med_devices.core.logging_utils import configure_utc_logging  # noqa: E402


DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
FIELDNAMES = [
    "source_table",
    "source_id",
    "row_count",
    "ticker_count",
    "active_ticker_count",
    "coverage_pct",
    "min_date",
    "max_date",
    "latest_date",
    "latest_ticker_count",
    "latest_coverage_pct",
]


FACT_SOURCES = [
    ("fact_short_interest", "source_id", "settlement_date", "ticker"),
    ("fact_finra_short_volume", "source_id", "trade_date", "ticker"),
    ("fact_ibkr_borrow_snapshot", "source_id", "asof_date", "ticker"),
    ("fact_sec_13f_holding", "source_id", "report_date", "ticker"),
    ("fact_sec_form4_transaction", "source_id", "transaction_date", "ticker"),
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Audit med-device external-positioning source coverage.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--asof", default="")
    parser.add_argument("--output-csv", type=Path, default=None)
    return parser.parse_args()


def table_exists(conn: Any, table: str) -> bool:
    return bool(conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name = ?", (table,)).fetchone())


def active_ticker_count(conn: Any) -> int:
    row = conn.execute("SELECT COUNT(DISTINCT UPPER(ticker)) FROM dim_company WHERE is_active = 1").fetchone()
    return int(row[0] or 0)


def coverage_rows(conn: Any, *, asof: str) -> list[dict[str, Any]]:
    active_count = active_ticker_count(conn)
    out: list[dict[str, Any]] = []
    for table, source_col, date_col, ticker_col in FACT_SOURCES:
        if not table_exists(conn, table):
            out.append(
                {
                    "source_table": table,
                    "source_id": "",
                    "row_count": 0,
                    "ticker_count": 0,
                    "active_ticker_count": active_count,
                    "coverage_pct": 0.0,
                    "min_date": "",
                    "max_date": "",
                    "latest_date": "",
                    "latest_ticker_count": 0,
                    "latest_coverage_pct": 0.0,
                }
            )
            continue
        rows = conn.execute(
            f"""
            SELECT
                COALESCE({source_col}, '') AS source_id,
                COUNT(*) AS row_count,
                COUNT(DISTINCT UPPER({ticker_col})) AS ticker_count,
                MIN({date_col}) AS min_date,
                MAX({date_col}) AS max_date
            FROM {table}
            WHERE COALESCE({date_col}, '') <> ''
              AND {date_col} <= ?
            GROUP BY COALESCE({source_col}, '')
            ORDER BY source_id
            """,
            (asof,),
        ).fetchall()
        for row in rows:
            latest_date = str(row["max_date"] or "")
            latest = conn.execute(
                f"""
                SELECT COUNT(DISTINCT UPPER({ticker_col})) AS latest_ticker_count
                FROM {table}
                WHERE COALESCE({source_col}, '') = ?
                  AND {date_col} = ?
                """,
                (row["source_id"], latest_date),
            ).fetchone()
            ticker_count = int(row["ticker_count"] or 0)
            latest_ticker_count = int(latest["latest_ticker_count"] or 0) if latest else 0
            out.append(
                {
                    "source_table": table,
                    "source_id": str(row["source_id"] or ""),
                    "row_count": int(row["row_count"] or 0),
                    "ticker_count": ticker_count,
                    "active_ticker_count": active_count,
                    "coverage_pct": round(100.0 * ticker_count / active_count, 2) if active_count else 0.0,
                    "min_date": str(row["min_date"] or ""),
                    "max_date": latest_date,
                    "latest_date": latest_date,
                    "latest_ticker_count": latest_ticker_count,
                    "latest_coverage_pct": round(100.0 * latest_ticker_count / active_count, 2) if active_count else 0.0,
                }
            )
    return out


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
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
    asof = args.asof.strip() or date.today().isoformat()
    db_path = args.db.expanduser().resolve() if args.db else resolve_path(cfg_get(config, "paths.database_path"), base_dir=base_dir)
    output_csv = (
        args.output_csv.expanduser().resolve()
        if args.output_csv
        else resolve_path(
            cfg_get(
                config,
                "external_positioning_coverage.output_csv",
                "../output/med_devices_reports/med_device_external_positioning_coverage.csv",
            ),
            base_dir=base_dir,
        )
    )
    with connect(db_path, timeout_sec=float(cfg_get(config, "runtime.sqlite_timeout_sec", 30.0))) as conn:
        init_db(conn)
        rows = coverage_rows(conn, asof=asof)
        write_csv(output_csv, rows)
    print(f"external_positioning_coverage={output_csv} rows={len(rows)} asof={asof}")


if __name__ == "__main__":
    main()
