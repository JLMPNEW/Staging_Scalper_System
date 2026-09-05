from __future__ import annotations

import sqlite3
from datetime import date

from tests.biotech.conftest import load_script_module


def test_calibration_date_validation_ignores_legacy_non_market_snapshots() -> None:
    module = load_script_module(
        "28_calibrate_biotech_opportunity.py",
        "calibration_market_session_grid",
    )
    with sqlite3.connect(":memory:") as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("CREATE TABLE daily_features (asof_date TEXT NOT NULL)")
        conn.execute("CREATE TABLE daily_scores (asof_date TEXT NOT NULL)")
        conn.execute("CREATE TABLE multibagger_scores_daily (asof_date TEXT NOT NULL)")
        conn.execute(
            "CREATE TABLE market_bars_daily (ticker TEXT NOT NULL, bar_date TEXT NOT NULL)"
        )
        dates = [("2021-12-23",), ("2021-12-24",), ("2021-12-27",)]
        conn.executemany("INSERT INTO daily_features(asof_date) VALUES (?)", dates)
        conn.executemany("INSERT INTO daily_scores(asof_date) VALUES (?)", dates)
        conn.executemany("INSERT INTO multibagger_scores_daily(asof_date) VALUES (?)", dates)
        conn.executemany(
            "INSERT INTO market_bars_daily(ticker, bar_date) VALUES ('XBI', ?)",
            [("2021-12-23",), ("2021-12-27",)],
        )

        raw_dates = module.load_snapshot_dates(
            conn,
            start_asof=date(2021, 12, 23),
            end_asof=date(2021, 12, 27),
            fridays_only=False,
            max_snapshots=0,
        )
        snapshot_dates = module.filter_snapshot_dates_to_benchmark_sessions(
            conn,
            raw_dates,
            benchmark_ticker="XBI",
        )

        assert raw_dates == ["2021-12-23", "2021-12-24", "2021-12-27"]
        assert snapshot_dates == ["2021-12-23", "2021-12-27"]
        module.validate_calibration_date_universe(
            conn,
            snapshot_dates=snapshot_dates,
            start_asof=date(2021, 12, 23),
            end_asof=date(2021, 12, 27),
            fridays_only=False,
            max_snapshots=0,
            benchmark_ticker="XBI",
        )
