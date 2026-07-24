#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import date
from pathlib import Path
from typing import Any


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from industrials.core.config import cfg_get, load_yaml, resolve_path  # noqa: E402
from industrials.core.db import connect  # noqa: E402
from industrials.machinery.scoring import write_json_atomic  # noqa: E402


DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
PROMOTED_TABLES = (
    "fact_financial_statement_canonical",
    "dim_issuer_reporting_profile_history",
)
PRIMARY_KEYS = {
    "fact_financial_statement_canonical": (
        "ticker",
        "source_id",
        "model_family",
        "canonical_metric",
        "period_end",
        "accession_number",
        "unit",
    ),
    "dim_issuer_reporting_profile_history": (
        "ticker",
        "model_family",
        "profile_asof_date",
    ),
}
FEATURE_TABLES = (
    "feature_market_technical",
    "feature_financial_statement",
    "feature_financial_metric_availability",
    "feature_positioning",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Promote accepted machinery historical/delisted canonical data from an "
            "isolated backfill database into production."
        )
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--source-db", type=Path, required=True)
    parser.add_argument("--target-db", type=Path, default=None)
    parser.add_argument("--start-date", default="2019-01-02")
    parser.add_argument("--end-date", default=date.today().isoformat())
    parser.add_argument("--report", type=Path, default=None)
    parser.add_argument(
        "--compact-target-features",
        action="store_true",
        help="Remove non-current machinery feature snapshots after promotion.",
    )
    parser.add_argument(
        "--preserve-asof",
        default="",
        help="Current machinery feature date retained when compaction is requested.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Required to modify the target database. Without it, run validation only.",
    )
    return parser.parse_args()


def _table_columns(conn: sqlite3.Connection, table: str) -> tuple[str, ...]:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return tuple(str(row["name"]) for row in rows)


def _eligible_tickers(
    conn: sqlite3.Connection,
    *,
    start_date: str,
    end_date: str,
) -> list[str]:
    rows = conn.execute(
        """
        SELECT DISTINCT membership.ticker
        FROM dim_universe_membership membership
        JOIN dim_delisted_calibration_seed seed
          ON seed.model_family = 'machinery'
         AND (
              seed.internal_ticker = membership.ticker
              OR seed.ticker = membership.ticker
         )
        WHERE membership.model_family = 'machinery'
          AND membership.point_in_time_flag = 1
          AND membership.start_date <= ?
          AND COALESCE(membership.end_date, '9999-12-31') >= ?
        ORDER BY membership.ticker
        """,
        (end_date, start_date),
    ).fetchall()
    return [str(row["ticker"]) for row in rows]


def _load_rows(
    conn: sqlite3.Connection,
    *,
    table: str,
    tickers: list[str],
) -> tuple[tuple[str, ...], list[sqlite3.Row]]:
    columns = _table_columns(conn, table)
    if not tickers:
        return columns, []
    placeholders = ",".join("?" for _ in tickers)
    rows = conn.execute(
        f"""
        SELECT {','.join(columns)}
        FROM {table}
        WHERE model_family = 'machinery'
          AND ticker IN ({placeholders})
        """,
        tickers,
    ).fetchall()
    return columns, rows


def _row_key(row: sqlite3.Row, columns: tuple[str, ...]) -> tuple[Any, ...]:
    return tuple(row[column] for column in columns)


def _business_values(
    row: sqlite3.Row,
    columns: tuple[str, ...],
) -> tuple[Any, ...]:
    return tuple(
        row[column]
        for column in columns
        if column not in {"created_at", "updated_at"}
    )


def _validate_target_compatibility(
    source: sqlite3.Connection,
    target: sqlite3.Connection,
) -> None:
    for table in PROMOTED_TABLES:
        source_columns = _table_columns(source, table)
        target_columns = _table_columns(target, table)
        if source_columns != target_columns:
            raise RuntimeError(
                f"Schema mismatch for {table}: source={source_columns}, target={target_columns}"
            )


def _validate_source_ids(
    target: sqlite3.Connection,
    source_rows: dict[str, list[sqlite3.Row]],
) -> None:
    required = {
        str(row["source_id"])
        for rows in source_rows.values()
        for row in rows
        if row["source_id"]
    }
    if not required:
        return
    placeholders = ",".join("?" for _ in required)
    available = {
        str(row["source_id"])
        for row in target.execute(
            f"SELECT source_id FROM source_registry WHERE source_id IN ({placeholders})",
            sorted(required),
        ).fetchall()
    }
    missing = sorted(required - available)
    if missing:
        raise RuntimeError(f"Target source_registry is missing source IDs: {missing}")


def _promote_table(
    target: sqlite3.Connection,
    *,
    table: str,
    columns: tuple[str, ...],
    source_rows: list[sqlite3.Row],
    tickers: list[str],
) -> dict[str, int]:
    primary_key = PRIMARY_KEYS[table]
    existing_rows: list[sqlite3.Row] = []
    if tickers:
        placeholders = ",".join("?" for _ in tickers)
        existing_rows = target.execute(
            f"""
            SELECT {','.join(columns)}
            FROM {table}
            WHERE model_family = 'machinery'
              AND ticker IN ({placeholders})
            """,
            tickers,
        ).fetchall()
    existing = {_row_key(row, primary_key): row for row in existing_rows}
    conflicts = [
        _row_key(row, primary_key)
        for row in source_rows
        if _row_key(row, primary_key) in existing
        and _business_values(row, columns)
        != _business_values(existing[_row_key(row, primary_key)], columns)
    ]
    if conflicts:
        raise RuntimeError(
            f"{table} has {len(conflicts)} immutable-key conflicts; first={conflicts[0]}"
        )

    source_keys = {_row_key(row, primary_key) for row in source_rows}
    preexisting = len(source_keys & set(existing))
    if source_rows:
        placeholders = ",".join("?" for _ in columns)
        target.executemany(
            f"INSERT OR IGNORE INTO {table} ({','.join(columns)}) VALUES ({placeholders})",
            [tuple(row[column] for column in columns) for row in source_rows],
        )

    post_rows: list[sqlite3.Row] = []
    if tickers:
        placeholders = ",".join("?" for _ in tickers)
        post_rows = target.execute(
            f"""
            SELECT {','.join(primary_key)}
            FROM {table}
            WHERE model_family = 'machinery'
              AND ticker IN ({placeholders})
            """,
            tickers,
        ).fetchall()
    post_keys = {_row_key(row, primary_key) for row in post_rows}
    missing = source_keys - post_keys
    if missing:
        raise RuntimeError(
            f"{table} promotion verification failed for {len(missing)} rows; first={next(iter(missing))}"
        )
    return {
        "source_rows": len(source_rows),
        "preexisting_rows": preexisting,
        "inserted_rows": len(source_keys) - preexisting,
        "verified_rows": len(source_keys),
    }


def promote_historical_data(
    source: sqlite3.Connection,
    target: sqlite3.Connection,
    *,
    start_date: str,
    end_date: str,
    compact_target_features: bool,
    preserve_asof: str,
    commit: bool,
) -> dict[str, Any]:
    if end_date < start_date:
        raise ValueError("end_date must be on or after start_date")
    if compact_target_features and not preserve_asof:
        raise ValueError("preserve_asof is required when compacting target features")

    _validate_target_compatibility(source, target)
    tickers = _eligible_tickers(source, start_date=start_date, end_date=end_date)
    if not tickers:
        raise RuntimeError("No resolved in-scope machinery delisted tickers were found")

    source_data: dict[str, tuple[tuple[str, ...], list[sqlite3.Row]]] = {
        table: _load_rows(source, table=table, tickers=tickers)
        for table in PROMOTED_TABLES
    }
    source_rows = {table: rows for table, (_, rows) in source_data.items()}
    _validate_source_ids(target, source_rows)

    target.execute("BEGIN IMMEDIATE")
    try:
        table_results = {
            table: _promote_table(
                target,
                table=table,
                columns=columns,
                source_rows=rows,
                tickers=tickers,
            )
            for table, (columns, rows) in source_data.items()
        }
        compacted_rows: dict[str, int] = {}
        if compact_target_features:
            for table in FEATURE_TABLES:
                before = target.total_changes
                target.execute(
                    f"""
                    DELETE FROM {table}
                    WHERE model_family = 'machinery'
                      AND asof_date <> ?
                    """,
                    (preserve_asof,),
                )
                compacted_rows[table] = target.total_changes - before
        if commit:
            target.commit()
        else:
            target.rollback()
    except Exception:
        target.rollback()
        raise

    return {
        "acceptance": "PASS",
        "mode": "COMMIT" if commit else "VALIDATION_ONLY",
        "model_family": "machinery",
        "start_date": start_date,
        "end_date": end_date,
        "resolved_in_scope_ticker_count": len(tickers),
        "resolved_in_scope_tickers": tickers,
        "tables": table_results,
        "compacted_feature_rows": compacted_rows,
        "preserved_feature_asof": preserve_asof if compact_target_features else "",
    }


def main() -> int:
    args = parse_args()
    config_path = args.config.resolve()
    config = load_yaml(config_path)
    base_dir = config_path.parent
    target_path = (
        args.target_db.resolve()
        if args.target_db
        else resolve_path(cfg_get(config, "paths.database_path"), base_dir=base_dir)
    )
    source_path = args.source_db.resolve()
    if source_path == target_path:
        raise ValueError("source-db and target-db must be different files")
    if not source_path.exists():
        raise FileNotFoundError(source_path)
    if not target_path.exists():
        raise FileNotFoundError(target_path)

    report_path = (
        args.report.resolve()
        if args.report
        else PROJECT_ROOT
        / "output"
        / "industrials"
        / "machinery"
        / "historical_backfill"
        / "machinery_historical_data_promotion.json"
    )
    with connect(source_path) as source, connect(target_path) as target:
        report = promote_historical_data(
            source,
            target,
            start_date=args.start_date,
            end_date=args.end_date,
            compact_target_features=args.compact_target_features,
            preserve_asof=args.preserve_asof,
            commit=args.force,
        )
    report.update(
        {
            "source_db": str(source_path),
            "target_db": str(target_path),
            "report_path": str(report_path),
        }
    )
    write_json_atomic(report_path, report)
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
