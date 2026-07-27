from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable, Mapping

from industrials.machinery.financial_contract import required_metric_names


MODEL_FAMILY = "machinery"
RESTORED_TABLE_SOURCES = {
    "feature_market_technical": "market_feature_source_id",
    "feature_financial_statement": "financial_feature_source_id",
    "feature_positioning": "positioning_feature_source_id",
}
RESTORED_TABLES = (
    "feature_market_technical",
    "feature_financial_statement",
    "feature_financial_metric_availability",
    "feature_positioning",
)


@dataclass(frozen=True)
class RestoredFeatureState:
    asof_date: str
    table_columns: dict[str, tuple[str, ...]]
    previous_rows: dict[str, tuple[tuple[object, ...], ...]]


def _utc_now() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def _table_columns(
    conn: sqlite3.Connection,
    table: str,
) -> list[str]:
    return [
        str(row[1])
        for row in conn.execute(f"PRAGMA table_info({table})")
    ]


def _database_value(value: object) -> object:
    return None if value is None or str(value) == "" else value


def _restore_feature_table(
    conn: sqlite3.Connection,
    *,
    table: str,
    source_field: str,
    asof_date: str,
    rows: list[dict[str, str]],
    now: str,
) -> None:
    table_columns = _table_columns(conn, table)
    common_columns = [
        column
        for column in table_columns
        if column in rows[0]
        and column
        not in {
            "ticker",
            "asof_date",
            "source_id",
            "model_family",
            "created_at",
            "updated_at",
        }
    ]
    insert_columns = [
        "ticker",
        "asof_date",
        "source_id",
        "model_family",
        *common_columns,
        "created_at",
        "updated_at",
    ]
    placeholders = ",".join("?" for _ in insert_columns)
    conn.execute(
        f"""
        DELETE FROM {table}
        WHERE model_family = ? AND asof_date = ?
        """,
        (MODEL_FAMILY, asof_date),
    )
    for row in rows:
        source_id = str(row.get(source_field) or "")
        if not source_id:
            raise ValueError(
                f"{asof_date} {row.get('ticker')}: blank {source_field}"
            )
        values = [
            str(row["ticker"]),
            asof_date,
            source_id,
            MODEL_FAMILY,
            *[_database_value(row.get(column)) for column in common_columns],
            now,
            now,
        ]
        conn.execute(
            f"""
            INSERT INTO {table}({",".join(insert_columns)})
            VALUES ({placeholders})
            """,
            values,
        )


def _metric_value(
    row: Mapping[str, str],
    metric_name: str,
) -> object:
    candidates = (
        metric_name,
        f"{metric_name}_usd",
        "rpo_implied_orders_usd"
        if metric_name == "rpo_implied_orders"
        else "",
    )
    for field in candidates:
        if field and str(row.get(field) or ""):
            return row[field]
    return None


def _restore_availability(
    conn: sqlite3.Connection,
    *,
    asof_date: str,
    rows: list[dict[str, str]],
    now: str,
) -> None:
    conn.execute(
        """
        DELETE FROM feature_financial_metric_availability
        WHERE model_family = ? AND asof_date = ?
        """,
        (MODEL_FAMILY, asof_date),
    )
    values: list[tuple[object, ...]] = []
    for row in rows:
        source_id = str(row.get("financial_feature_source_id") or "")
        for metric_name in required_metric_names():
            values.append(
                (
                    str(row["ticker"]),
                    asof_date,
                    MODEL_FAMILY,
                    metric_name,
                    str(
                        row.get(f"{metric_name}_availability_status")
                        or ""
                    ),
                    _metric_value(row, metric_name),
                    "USD",
                    source_id,
                    str(row.get("accession_number") or ""),
                    "",
                    "",
                    str(row.get("fiscal_period_end") or ""),
                    "historical-sidecar",
                    "HistoricalSidecarRestore",
                    "historical_sidecar_restore",
                    1.0,
                    "temporary_restore_before_targeted_promotion_materialization",
                    "{}",
                    now,
                    now,
                )
            )
    conn.executemany(
        """
        INSERT INTO feature_financial_metric_availability(
            ticker, asof_date, model_family, metric_name,
            availability_status, metric_value, unit, source_id,
            accession_number, filing_date, period_start, period_end,
            taxonomy, concept_name, extraction_method, confidence,
            status_reason, provenance_json, created_at, updated_at
        )
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        values,
    )


def restore_validated_sidecar_features(
    conn: sqlite3.Connection,
    *,
    asof_date: str,
    rows: list[dict[str, str]],
) -> RestoredFeatureState:
    if not rows:
        raise ValueError("Cannot restore features from an empty sidecar")
    if {str(row.get("asof_date") or "") for row in rows} != {asof_date}:
        raise ValueError("Sidecar restore as-of mismatch")
    now = _utc_now()
    table_columns = {
        table: tuple(_table_columns(conn, table))
        for table in RESTORED_TABLES
    }
    previous_rows = {
        table: tuple(
            tuple(row)
            for row in conn.execute(
                f"""
                SELECT {",".join(table_columns[table])}
                FROM {table}
                WHERE model_family = ? AND asof_date = ?
                """,
                (MODEL_FAMILY, asof_date),
            )
        )
        for table in RESTORED_TABLES
    }
    with conn:
        for table, source_field in RESTORED_TABLE_SOURCES.items():
            _restore_feature_table(
                conn,
                table=table,
                source_field=source_field,
                asof_date=asof_date,
                rows=rows,
                now=now,
            )
        _restore_availability(
            conn,
            asof_date=asof_date,
            rows=rows,
            now=now,
        )
    return RestoredFeatureState(
        asof_date=asof_date,
        table_columns=table_columns,
        previous_rows=previous_rows,
    )


def compact_restored_features(
    conn: sqlite3.Connection,
    *,
    asof_date: str,
    restore_state: RestoredFeatureState,
) -> None:
    if restore_state.asof_date != asof_date:
        raise ValueError(
            "Restored feature state does not match cleanup as-of date"
        )
    with conn:
        for table in RESTORED_TABLES:
            conn.execute(
                f"""
                DELETE FROM {table}
                WHERE model_family = ? AND asof_date = ?
                """,
                (MODEL_FAMILY, asof_date),
            )
            columns = restore_state.table_columns[table]
            previous_rows = restore_state.previous_rows[table]
            if previous_rows:
                placeholders = ",".join("?" for _ in columns)
                conn.executemany(
                    f"""
                    INSERT INTO {table}({",".join(columns)})
                    VALUES ({placeholders})
                    """,
                    previous_rows,
                )


def affected_partition_map(
    rows: Iterable[Mapping[str, str]],
) -> dict[str, tuple[str, ...]]:
    output: dict[str, tuple[str, ...]] = {}
    for row in rows:
        asof_date = str(row.get("asof_date") or "")
        tickers = tuple(
            sorted(
                {
                    item.strip().upper()
                    for item in str(
                        row.get("affected_tickers") or ""
                    ).split(",")
                    if item.strip()
                }
            )
        )
        if not asof_date or not tickers:
            raise ValueError("Affected partition row is incomplete")
        if asof_date in output:
            raise ValueError(f"Duplicate affected partition date={asof_date}")
        output[asof_date] = tickers
    return output
