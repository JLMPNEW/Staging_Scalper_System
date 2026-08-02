from __future__ import annotations

import sqlite3
from datetime import date
from pathlib import Path

from portfolio_layer.risk.local_prices import (
    load_local_adjusted_price_fallbacks,
)


def _build_price_db(path: Path) -> None:
    with sqlite3.connect(path) as conn:
        conn.execute(
            """
            CREATE TABLE fact_price_ohlcv(
                ticker TEXT NOT NULL,
                bar_date TEXT NOT NULL,
                source_id TEXT NOT NULL,
                adj_close REAL,
                price_adjustment TEXT,
                is_adjusted INTEGER NOT NULL
            )
            """
        )
        conn.executemany(
            "INSERT INTO fact_price_ohlcv VALUES (?, ?, ?, ?, ?, ?)",
            [
                (
                    "GRC",
                    "2026-07-23",
                    "yahoo_finance_adjusted",
                    52.0,
                    "adjusted_close",
                    1,
                ),
                (
                    "GRC",
                    "2026-07-24",
                    "yahoo_finance_adjusted",
                    53.0,
                    "adjusted_close",
                    1,
                ),
                (
                    "GRC",
                    "2026-07-27",
                    "yahoo_finance_adjusted",
                    99.0,
                    "adjusted_close",
                    1,
                ),
                (
                    "LII",
                    "2026-07-24",
                    "unapproved",
                    600.0,
                    "adjusted_close",
                    1,
                ),
                (
                    "BAD",
                    "2026-07-24",
                    "yahoo_finance_adjusted",
                    10.0,
                    "raw_close",
                    1,
                ),
                (
                    "ZERO",
                    "2026-07-24",
                    "yahoo_finance_adjusted",
                    0.0,
                    "adjusted_close",
                    1,
                ),
                (
                    "SPY",
                    "2026-07-24",
                    "yahoo_finance_adjusted",
                    640.0,
                    "adjusted_close",
                    1,
                ),
            ],
        )


def test_local_adjusted_price_fallback_is_point_in_time_and_allowlisted(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "industrials.sqlite"
    _build_price_db(database_path)
    sources = [
        {
            "name": "test_db",
            "database_path": str(database_path),
            "source_pipelines": ["machinery"],
            "source_ids": ["yahoo_finance_adjusted"],
            "accepted_price_adjustments": ["adjusted_close"],
        }
    ]
    universe = [
        {"ticker": "GRC", "source_pipeline": "machinery"},
        {"ticker": "LII", "source_pipeline": "machinery"},
        {"ticker": "BAD", "source_pipeline": "machinery"},
        {"ticker": "ZERO", "source_pipeline": "machinery"},
        {"ticker": "OTHER", "source_pipeline": "biotech"},
    ]

    prices, provenance, summaries = load_local_adjusted_price_fallbacks(
        sources,
        base_dir=tmp_path,
        universe=universe,
        start=date(2026, 7, 1),
        end=date(2026, 7, 24),
    )

    assert prices == {"GRC": [("2026-07-23", 52.0), ("2026-07-24", 53.0)]}
    assert provenance["GRC"].provider == (
        "local_sqlite:test_db:yahoo_finance_adjusted"
    )
    assert provenance["GRC"].last_date == "2026-07-24"
    assert len(provenance["GRC"].extracted_sha256) == 64
    assert summaries[0]["loaded_ticker_count"] == 1
    assert summaries[0]["loaded_row_count"] == 2


def test_local_adjusted_price_fallback_missing_database_is_nonfatal(
    tmp_path: Path,
) -> None:
    prices, provenance, summaries = load_local_adjusted_price_fallbacks(
        [
            {
                "name": "missing",
                "database_path": str(tmp_path / "missing.sqlite"),
                "source_ids": ["yahoo_finance_adjusted"],
            }
        ],
        base_dir=tmp_path,
        universe=[{"ticker": "GRC", "source_pipeline": "machinery"}],
        start=date(2026, 7, 1),
        end=date(2026, 7, 24),
    )

    assert prices == {}
    assert provenance == {}
    assert summaries[0]["database_exists"] is False


def test_local_adjusted_price_fallback_can_include_market_instruments(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "industrials.sqlite"
    _build_price_db(database_path)

    prices, provenance, summaries = load_local_adjusted_price_fallbacks(
        [
            {
                "name": "test_db",
                "database_path": str(database_path),
                "source_pipelines": ["machinery"],
                "include_market_instruments": True,
                "source_ids": ["yahoo_finance_adjusted"],
                "accepted_price_adjustments": ["adjusted_close"],
            }
        ],
        base_dir=tmp_path,
        universe=[
            {
                "ticker": "SPY",
                "role": "market_instrument",
                "source_pipeline": "",
            },
            {"ticker": "OTHER", "role": "scored", "source_pipeline": "biotech"},
        ],
        start=date(2026, 7, 1),
        end=date(2026, 7, 24),
    )

    assert prices == {"SPY": [("2026-07-24", 640.0)]}
    assert provenance["SPY"].provider == (
        "local_sqlite:test_db:yahoo_finance_adjusted"
    )
    assert summaries[0]["include_market_instruments"] is True
    assert summaries[0]["requested_ticker_count"] == 1
