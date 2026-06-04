from __future__ import annotations

import csv
from datetime import date

from market_positioning.core import connect, export_positioning_features, ingest_13f_csv, ingest_short_interest_csv, init_db, parse_date


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
        short_path, ownership_path, short_count, ownership_count = export_positioning_features(
            conn,
            asof_date=date(2025, 6, 1),
            output_dir=out_dir,
        )

    assert short_count == 1
    assert ownership_count == 1
    with short_path.open("r", encoding="utf-8") as handle:
        short_rows = list(csv.DictReader(handle))
    with ownership_path.open("r", encoding="utf-8") as handle:
        ownership_rows = list(csv.DictReader(handle))

    assert short_rows[0]["ticker"] == "AAA"
    assert float(short_rows[0]["short_interest_pct_float"]) == 0.08
    assert ownership_rows[0]["ticker"] == "AAA"
    assert float(ownership_rows[0]["institutional_ownership_delta_pct"]) == 0.5
