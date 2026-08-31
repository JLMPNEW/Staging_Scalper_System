from __future__ import annotations

import csv
import importlib.util
import sqlite3
from datetime import date
from pathlib import Path
from types import ModuleType

from market_positioning.api_collectors import (
    ibkr_fee_history_bounds,
    ibkr_fee_history_left_edge_missing,
)
from market_positioning.core import connect, init_db


ROOT = Path(__file__).resolve().parents[2]


def load_upstream() -> ModuleType:
    path = ROOT / "technology" / "scripts" / "13_sync_technology_positioning_upstream.py"
    spec = importlib.util.spec_from_file_location("technology_positioning_cusip_pit", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_configured_cusip_map_is_scoped_and_supports_lineages(tmp_path: Path) -> None:
    upstream = load_upstream()
    universe = tmp_path / "universe.csv"
    with universe.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["ticker", "company_name"])
        writer.writeheader()
        writer.writerows(
            [
                {"ticker": "AEVA", "company_name": "Aeva Technologies"},
                {"ticker": "OTHER", "company_name": "Other"},
            ]
        )
    output = tmp_path / "cusips.csv"
    result = upstream.write_configured_13f_cusip_map_csv(
        {
            "positioning_import": {
                "sec_13f_cusip_aliases": {
                    "AEVA": ["00835Q103", "00835Q202"],
                    "AIP": ["04302A104"],
                }
            }
        },
        universe,
        output,
    )

    assert result == output
    with output.open("r", encoding="utf-8", newline="") as handle:
        assert list(csv.DictReader(handle)) == [
            {"ticker": "AEVA", "cusip": "00835Q103"},
            {"ticker": "AEVA", "cusip": "00835Q202"},
        ]


def test_13f_pit_aggregation_excludes_years_late_amendments() -> None:
    upstream = load_upstream()
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    upstream.init_market_positioning_db(conn)
    conn.executemany(
        """
        INSERT INTO institutional_13f_holdings(
            filing_key, manager_cik, manager_name, ticker, cusip,
            period_of_report, filing_date, shares, market_value,
            share_type, put_call, source, created_at, updated_at
        ) VALUES (?, 'M1', 'Manager', 'TEST', '123456789', ?, ?, ?, 1000.0,
                  'SH', '', 'sec_13f_data_sets', '', '')
        """,
        [
            ("TIMELY_Q4", "2023-12-31", "2024-02-14", 100.0),
            ("LATE_AMENDMENT", "2023-12-31", "2026-02-01", 999.0),
            ("TIMELY_Q1", "2024-03-31", "2024-05-14", 110.0),
        ],
    )

    count = upstream.aggregate_13f_ownership_for_tickers(conn, ["TEST"])
    rows = conn.execute(
        """
        SELECT period_of_report, asof_date, institutional_shares
        FROM institutional_13f_ownership_snapshots
        WHERE ticker = 'TEST'
        ORDER BY period_of_report
        """
    ).fetchall()

    assert count == 2
    assert [tuple(row) for row in rows] == [
        ("2023-12-31", "2024-02-14", 100.0),
        ("2024-03-31", "2024-05-14", 110.0),
    ]


def test_ibkr_fee_bounds_expose_missing_left_edge(tmp_path: Path) -> None:
    with connect(tmp_path / "market_positioning.sqlite") as conn:
        init_db(conn)
        conn.executemany(
            """
            INSERT INTO ibkr_borrow_fee_rate_daily(
                ticker, asof_date, con_id, borrow_fee_rate, source, created_at, updated_at
            ) VALUES ('AEVA', ?, 1, 0.01, 'interactive_brokers', '', '')
            """,
            [("2026-07-13",), ("2026-08-25",)],
        )
        earliest, latest = ibkr_fee_history_bounds(conn, "AEVA")
        missing_earliest, missing_latest = ibkr_fee_history_bounds(conn, "MISSING")

    assert earliest is not None and earliest.isoformat() == "2026-07-13"
    assert latest is not None and latest.isoformat() == "2026-08-25"
    assert missing_earliest is None
    assert missing_latest is None
    assert ibkr_fee_history_left_edge_missing(earliest, date(2021, 3, 15))
    assert not ibkr_fee_history_left_edge_missing(earliest, date(2026, 7, 1))
    assert ibkr_fee_history_left_edge_missing(None, date(2026, 7, 1))


def test_ibkr_duration_uses_years_for_long_windows() -> None:
    upstream = load_upstream()

    assert upstream.ibkr_borrow_duration(date(2026, 8, 1), date(2026, 8, 25), full_history=False) == "24 D"
    assert upstream.ibkr_borrow_duration(date(2021, 3, 15), date(2026, 8, 25), full_history=False) == "6 Y"
    assert upstream.ibkr_borrow_duration(date(2013, 1, 1), date(2026, 8, 25), full_history=True) == "7 Y"
