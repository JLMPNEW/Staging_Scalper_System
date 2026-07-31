from __future__ import annotations

import sqlite3

from industrials.core.db import init_db
from industrials.machinery.financial_contract import required_metric_names
from industrials.machinery.historical_promotion_materializer import (
    affected_partition_map,
    compact_restored_features,
    restore_validated_sidecar_features,
)


def _sidecar_row(asof_date: str) -> dict[str, str]:
    row = {
        "ticker": "TEST",
        "asof_date": asof_date,
        "market_feature_source_id": "test_market",
        "financial_feature_source_id": "test_financial",
        "positioning_feature_source_id": "test_positioning",
        "latest_adj_close": "42.5",
        "revenue_ttm_usd": "1000000",
        "institutional_ownership_delta_pct": "0.25",
        "accession_number": "0000000000-24-000001",
        "fiscal_period_end": "2023-12-31",
    }
    for metric_name in required_metric_names():
        row[f"{metric_name}_availability_status"] = "NOT_DISCLOSED"
    row["orders_availability_status"] = "REPORTED"
    row["orders_usd"] = "250000"
    return row


def _count(
    conn: sqlite3.Connection,
    table: str,
    *,
    asof_date: str,
) -> int:
    return int(
        conn.execute(
            f"""
            SELECT COUNT(*)
            FROM {table}
            WHERE model_family = 'machinery' AND asof_date = ?
            """,
            (asof_date,),
        ).fetchone()[0]
    )


def test_affected_partition_map_normalizes_tickers() -> None:
    assert affected_partition_map(
        [
            {
                "asof_date": "2024-01-03",
                "affected_tickers": " wab, DOV,WAB ",
            }
        ]
    ) == {"2024-01-03": ("DOV", "WAB")}


def test_restore_and_cleanup_preserve_preexisting_rows() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_db(conn)
    asof_date = "2024-01-03"
    now = "2024-01-03T12:00:00Z"
    conn.executemany(
        """
        INSERT INTO source_registry(
            source_id, stage, source_type, source_name, base_url,
            created_at, updated_at
        )
        VALUES (?, 'test', 'test', ?, 'https://example.test', ?, ?)
        """,
        [
            ("test_market", "test_market", now, now),
            ("test_financial", "test_financial", now, now),
            ("test_positioning", "test_positioning", now, now),
        ],
    )
    conn.execute(
        """
        INSERT INTO feature_market_technical(
            ticker, asof_date, source_id, model_family,
            latest_adj_close, created_at, updated_at
        )
        VALUES (
            'OLD', ?, 'test_market', 'machinery', 7.5, ?, ?
        )
        """,
        (asof_date, now, now),
    )
    conn.commit()

    state = restore_validated_sidecar_features(
        conn,
        asof_date=asof_date,
        rows=[_sidecar_row(asof_date)],
    )
    assert _count(
        conn,
        "feature_market_technical",
        asof_date=asof_date,
    ) == 1
    assert _count(
        conn,
        "feature_financial_statement",
        asof_date=asof_date,
    ) == 1
    assert _count(
        conn,
        "feature_positioning",
        asof_date=asof_date,
    ) == 1
    assert _count(
        conn,
        "feature_financial_metric_availability",
        asof_date=asof_date,
    ) == len(required_metric_names())
    restored_orders = conn.execute(
        """
        SELECT availability_status, metric_value
        FROM feature_financial_metric_availability
        WHERE ticker = 'TEST' AND asof_date = ?
          AND model_family = 'machinery' AND metric_name = 'orders'
        """,
        (asof_date,),
    ).fetchone()
    assert tuple(restored_orders) == ("REPORTED", 250000.0)

    compact_restored_features(
        conn,
        asof_date=asof_date,
        restore_state=state,
    )
    old_row = conn.execute(
        """
        SELECT ticker, latest_adj_close
        FROM feature_market_technical
        WHERE model_family = 'machinery' AND asof_date = ?
        """,
        (asof_date,),
    ).fetchone()
    assert tuple(old_row) == ("OLD", 7.5)
    for table in (
        "feature_financial_statement",
        "feature_positioning",
        "feature_financial_metric_availability",
    ):
        assert _count(conn, table, asof_date=asof_date) == 0
