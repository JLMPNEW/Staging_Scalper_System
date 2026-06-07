from __future__ import annotations

import csv
from datetime import date

from market_positioning.core import (
    connect,
    export_positioning_features,
    ingest_13f_csv,
    ingest_short_interest_csv,
    init_db,
    latest_borrow_availability_rows,
    parse_date,
)


def write_rows(path, fieldnames: list[str], rows: list[dict[str, object]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def test_parse_sec_13f_date_format() -> None:
    assert parse_date("31-JAN-2025").isoformat() == "2025-01-31"  # type: ignore[union-attr]


def test_market_positioning_ingest_and_export_point_in_time(tmp_path) -> None:
    db_path = tmp_path / "market_positioning.sqlite"
    short_csv = tmp_path / "short.csv"
    holdings_csv = tmp_path / "holdings.csv"
    out_dir = tmp_path / "exports"
    write_rows(
        short_csv,
        ["ticker", "settlement_date", "publication_date", "short_interest_pct_float", "days_to_cover", "source"],
        [
            {
                "ticker": "AAA",
                "settlement_date": "2025-01-15",
                "publication_date": "2025-01-24",
                "short_interest_pct_float": "12",
                "days_to_cover": "4",
                "source": "unit",
            },
            {
                "ticker": "AAA",
                "settlement_date": "2025-02-15",
                "publication_date": "2025-02-24",
                "short_interest_pct_float": "8",
                "days_to_cover": "2",
                "source": "unit",
            },
        ],
    )
    write_rows(
        holdings_csv,
        [
            "ticker",
            "manager_cik",
            "manager_name",
            "period_of_report",
            "filing_date",
            "shares",
            "market_value",
            "cusip",
            "accession_number",
            "source",
        ],
        [
            {
                "ticker": "AAA",
                "manager_cik": "1",
                "manager_name": "Fund A",
                "period_of_report": "2024-12-31",
                "filing_date": "2025-02-14",
                "shares": "100",
                "market_value": "1000",
                "cusip": "000AAA",
                "accession_number": "A1",
                "source": "unit",
            },
            {
                "ticker": "AAA",
                "manager_cik": "1",
                "manager_name": "Fund A",
                "period_of_report": "2025-03-31",
                "filing_date": "2025-05-14",
                "shares": "150",
                "market_value": "1800",
                "cusip": "000AAA",
                "accession_number": "A2",
                "source": "unit",
            },
        ],
    )
    with connect(db_path) as conn:
        init_db(conn)
        assert ingest_short_interest_csv(conn, short_csv, history_start_date=date(2019, 1, 1), source="unit") == 2
        filings, rows = ingest_13f_csv(conn, holdings_csv, history_start_date=date(2019, 1, 1), source="unit")
        assert filings == 2
        assert rows == 2
        conn.executemany(
            """
            INSERT INTO ibkr_borrow_fee_rate_daily(
                ticker, asof_date, con_id, borrow_fee_rate, source, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            [
                ("AAA", "2025-03-01", 111, 0.02, "interactive_brokers", "now", "now"),
                ("AAA", "2025-05-15", 111, 0.08, "interactive_brokers", "now", "now"),
                ("AAA", "2025-05-30", 111, 0.10, "interactive_brokers", "now", "now"),
            ],
        )
        conn.execute(
            """
            INSERT INTO ibkr_shortable_shares_snapshots(
                ticker, asof_date, asof_datetime, con_id, shortable_shares,
                market_data_type, source, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            ("AAA", "2025-06-01", "2025-06-01T15:00:00Z", 111, 40_000.0, 1, "interactive_brokers", "now", "now"),
        )
        short_path, ownership_path, borrow_path, short_count, ownership_count, borrow_count = export_positioning_features(
            conn,
            asof_date=date(2025, 6, 1),
            output_dir=out_dir,
        )

    assert short_count == 1
    assert ownership_count == 1
    assert borrow_count == 1
    with short_path.open("r", encoding="utf-8") as handle:
        short_rows = list(csv.DictReader(handle))
    with ownership_path.open("r", encoding="utf-8") as handle:
        ownership_rows = list(csv.DictReader(handle))
    with borrow_path.open("r", encoding="utf-8") as handle:
        borrow_rows = list(csv.DictReader(handle))

    assert short_rows[0]["ticker"] == "AAA"
    assert float(short_rows[0]["short_interest_pct_float"]) == 0.08
    assert ownership_rows[0]["ticker"] == "AAA"
    assert float(ownership_rows[0]["institutional_ownership_delta_pct"]) == 0.5
    assert borrow_rows[0]["ticker"] == "AAA"
    assert float(borrow_rows[0]["borrow_rate_current"]) == 0.10
    assert float(borrow_rows[0]["borrow_rate_30d_avg"]) == 0.09
    assert float(borrow_rows[0]["shortable_shares"]) == 40_000.0
    assert float(borrow_rows[0]["hard_to_borrow_flag"]) == 1.0


def test_borrow_export_staleness_guard_and_threshold(tmp_path) -> None:
    db_path = tmp_path / "market_positioning.sqlite"
    with connect(db_path) as conn:
        init_db(conn)
        conn.executemany(
            """
            INSERT INTO ibkr_borrow_fee_rate_daily(
                ticker, asof_date, con_id, borrow_fee_rate, source, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            [
                ("AAA", "2025-01-01", 111, 0.50, "interactive_brokers", "now", "now"),
                ("BBB", "2025-05-31", 222, 0.08, "interactive_brokers", "now", "now"),
            ],
        )
        conn.executemany(
            """
            INSERT INTO ibkr_shortable_shares_snapshots(
                ticker, asof_date, asof_datetime, con_id, shortable_shares,
                market_data_type, source, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                ("AAA", "2025-01-01", "2025-01-01T15:00:00Z", 111, 1_000.0, 1, "interactive_brokers", "now", "now"),
                ("BBB", "2025-06-01", "2025-06-01T15:00:00Z", 222, 75_000.0, 1, "interactive_brokers", "now", "now"),
            ],
        )
        rows = latest_borrow_availability_rows(
            conn,
            date(2025, 6, 2),
            {"AAA", "BBB"},
            max_fee_staleness_days=10,
            max_snapshot_staleness_days=7,
            hard_to_borrow_shares=100_000.0,
        )

    by_ticker = {row["ticker"]: row for row in rows}
    assert by_ticker["AAA"]["borrow_rate_current"] == ""
    assert by_ticker["AAA"]["shortable_shares"] == ""
    assert by_ticker["AAA"]["borrow_fee_stale_flag"] == 1.0
    assert by_ticker["AAA"]["shortable_stale_flag"] == 1.0
    assert by_ticker["BBB"]["borrow_rate_current"] == 0.08
    assert by_ticker["BBB"]["hard_to_borrow_flag"] == 1.0
