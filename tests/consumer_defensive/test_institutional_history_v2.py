from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from consumer_defensive.core.institutional_history_v2 import (
    derive_institutional_history_v2,
)


def _database(path: Path) -> None:
    connection = sqlite3.connect(path)
    connection.execute(
        """CREATE TABLE institutional_13f_holdings(
               filing_key TEXT, manager_cik TEXT, manager_name TEXT,
               ticker TEXT, period_of_report TEXT, filing_date TEXT,
               accepted_at TEXT, shares REAL, market_value REAL,
               share_type TEXT, put_call TEXT)"""
    )
    rows = [
        ("f0", "m1", "", "AAA", "2018-12-31", "2019-02-10", "2019-02-10T10:00:00Z", 100, 1000, "SH", ""),
        ("f1", "m1", "", "AAA", "2019-03-31", "2019-05-10", "2019-05-10T10:00:00Z", 60, 600, "SH", ""),
        ("f1", "m1", "", "AAA", "2019-03-31", "2019-05-10", "2019-05-10T10:00:00Z", 50, 500, "SH", ""),
        ("f2", "m2", "", "AAA", "2019-03-31", "2019-05-15", "2019-05-15T10:00:00Z", 50, 500, "SH", ""),
        # This later amendment must not enter a May 31 cutoff.
        ("f3", "m1", "", "AAA", "2019-03-31", "2019-06-10", "2019-06-10T10:00:00Z", 999, 9999, "SH", ""),
        ("b0", "m9", "", "BBB", "2019-03-31", "2019-05-14", "2019-05-14T10:00:00Z", 20, 200, "SH", ""),
        # Options are not common-share ownership and must be excluded.
        ("o0", "m8", "", "AAA", "2019-03-31", "2019-05-12", "2019-05-12T10:00:00Z", 500, 5000, "SH", "PUT"),
    ]
    connection.executemany("INSERT INTO institutional_13f_holdings VALUES(?,?,?,?,?,?,?,?,?,?,?)", rows)
    connection.commit()
    connection.close()


def test_read_only_derivation_recovers_period_history_without_future_amendment(tmp_path: Path) -> None:
    database = tmp_path / "positioning.sqlite"
    _database(database)
    before = database.read_bytes()
    history, summary = derive_institutional_history_v2(
        database,
        tickers={"AAA", "BBB"},
        history_start="2019-01-01",
        maximum_date="2019-05-31",
    )
    assert len(history["AAA"]) == 2
    first, second = history["AAA"]
    assert first["institutional_ownership_delta_pct"] is None
    assert second["publication_date"] == "2019-05-20"
    assert second["institutional_shares"] == pytest.approx(160.0)
    assert second["manager_count"] == 2
    assert second["institutional_ownership_delta_pct"] == pytest.approx(0.6)
    assert summary["snapshot_row_count"] == 3
    assert summary["mutation_performed"] is False
    assert database.read_bytes() == before


def test_derivation_is_deterministic_under_ticker_order(tmp_path: Path) -> None:
    database = tmp_path / "positioning.sqlite"
    _database(database)
    left, left_summary = derive_institutional_history_v2(
        database,
        tickers=["BBB", "AAA"],
        history_start="2019-01-01",
        maximum_date="2019-05-31",
    )
    right, right_summary = derive_institutional_history_v2(
        database,
        tickers=["AAA", "BBB"],
        history_start="2019-01-01",
        maximum_date="2019-05-31",
    )
    assert left == right
    assert left_summary == right_summary


