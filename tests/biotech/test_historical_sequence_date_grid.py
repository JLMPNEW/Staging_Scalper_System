from __future__ import annotations

import sqlite3
from pathlib import Path

from tests.biotech.conftest import load_script_module


def _history_db(path: Path) -> Path:
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE daily_scores (asof_date TEXT NOT NULL)")
        conn.executemany(
            "INSERT INTO daily_scores(asof_date) VALUES (?)",
            [("2024-01-02",), ("2024-01-05",), ("2024-01-08",)],
        )
        conn.execute(
            "CREATE TABLE market_bars_daily (ticker TEXT NOT NULL, bar_date TEXT NOT NULL)"
        )
        conn.executemany(
            "INSERT INTO market_bars_daily(ticker, bar_date) VALUES ('XBI', ?)",
            [("2024-01-02",), ("2024-01-03",), ("2024-01-04",), ("2024-01-05",), ("2024-01-08",)],
        )
    return path


def test_explicit_bounds_preserve_daily_source_dates(tmp_path: Path) -> None:
    module = load_script_module("45_run_biotech_clean_historical_sequence.py", "historical_daily_grid")
    db_path = _history_db(tmp_path / "history.sqlite")
    start, end, dates = module.resolve_history_dates(
        db_path,
        source_table="daily_scores",
        start_asof="2024-01-02",
        end_asof="2024-01-05",
        target_weekly_date_count=0,
        snap_weekly_to_market_days=True,
        fridays_only=False,
    )
    assert (start, end) == ("2024-01-02", "2024-01-05")
    assert dates == ["2024-01-02", "2024-01-05"]


def test_friday_filter_is_explicit(tmp_path: Path) -> None:
    module = load_script_module("45_run_biotech_clean_historical_sequence.py", "historical_friday_grid")
    db_path = _history_db(tmp_path / "history.sqlite")
    start, end, dates = module.resolve_history_dates(
        db_path,
        source_table="daily_scores",
        start_asof="2024-01-02",
        end_asof="2024-01-08",
        target_weekly_date_count=0,
        snap_weekly_to_market_days=True,
        fridays_only=True,
    )
    assert (start, end) == ("2024-01-05", "2024-01-05")
    assert dates == ["2024-01-05"]

def test_market_daily_explicit_bounds_do_not_require_existing_score_dates(tmp_path: Path) -> None:
    module = load_script_module("45_run_biotech_clean_historical_sequence.py", "historical_market_daily_empty_scores")
    db_path = tmp_path / "history.sqlite"
    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE daily_scores (asof_date TEXT NOT NULL)")
        conn.execute("CREATE TABLE market_bars_daily (ticker TEXT NOT NULL, bar_date TEXT NOT NULL)")
        conn.execute(
            "INSERT INTO market_bars_daily(ticker, bar_date) VALUES ('XBI', '2024-01-03')"
        )

    start, end, dates = module.resolve_history_dates(
        db_path,
        source_table="daily_scores",
        start_asof="2024-01-03",
        end_asof="2024-01-03",
        target_weekly_date_count=0,
        snap_weekly_to_market_days=True,
        fridays_only=False,
        date_frequency="market_daily",
        benchmark_ticker="XBI",
    )

    assert (start, end) == ("2024-01-03", "2024-01-03")
    assert dates == ["2024-01-03"]


def test_market_daily_frequency_fills_sessions_missing_from_prior_scores(tmp_path: Path) -> None:
    module = load_script_module("45_run_biotech_clean_historical_sequence.py", "historical_market_daily_grid")
    db_path = _history_db(tmp_path / "history.sqlite")
    start, end, dates = module.resolve_history_dates(
        db_path,
        source_table="daily_scores",
        start_asof="2024-01-02",
        end_asof="2024-01-05",
        target_weekly_date_count=0,
        snap_weekly_to_market_days=True,
        fridays_only=False,
        date_frequency="market_daily",
        benchmark_ticker="XBI",
    )
    assert (start, end) == ("2024-01-02", "2024-01-05")
    assert dates == ["2024-01-02", "2024-01-03", "2024-01-04", "2024-01-05"]



def test_core_score_history_mode_includes_all_calibration_score_layers() -> None:
    source = Path("biotech_index/scripts/45_run_biotech_clean_historical_sequence.py").read_text(
        encoding="utf-8"
    )
    core_block = source.split("if args.core_score_history_only:", maxsplit=1)[1].split(
        "if (", maxsplit=1
    )[0]
    assert '"biotech_scores"' in core_block
    assert '"multibagger_features"' in core_block
    assert '"multibagger_scores"' in core_block
