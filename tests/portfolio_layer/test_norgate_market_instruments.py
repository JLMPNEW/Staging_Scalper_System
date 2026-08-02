from __future__ import annotations

import sqlite3
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

from portfolio_layer.risk.norgate_market_instruments import (
    configured_market_instruments,
    hydrate_market_instruments,
    purge_cached_tickers,
)


class FakeProvider:
    StockPriceAdjustmentType = SimpleNamespace(TOTALRETURN="total_return")

    def __init__(
        self,
        last_date: str = "2026-07-30",
        missing_tickers: set[str] | None = None,
    ) -> None:
        self.last_date = last_date
        self.missing_tickers = missing_tickers or set()

    def price_timeseries(self, ticker: str, **_: object) -> pd.DataFrame:
        if ticker in self.missing_tickers:
            raise ValueError(f"unknown symbol {ticker}")
        return pd.DataFrame(
            {"Close": [100.0, 101.0]},
            index=pd.to_datetime(["2026-07-29", self.last_date]),
        )


def test_configured_market_instruments_are_deduplicated() -> None:
    assert configured_market_instruments(
        {
            "master_calendar_ticker": "SPY",
            "benchmark_tickers": ["SPY", "QQQ"],
            "hedge_rotation_etfs": ["QQQ", "IGV"],
            "sector_etf_map": {"machinery": "XLI"},
        }
    ) == ["IGV", "QQQ", "SPY", "XLI"]


def test_hydration_writes_adjusted_point_in_time_rows(tmp_path: Path) -> None:
    database_path = tmp_path / "market_instruments.sqlite"
    summaries = hydrate_market_instruments(
        FakeProvider(),
        database_path=database_path,
        tickers=["SPY", "XLI"],
        start=date(2026, 7, 1),
        end=date(2026, 7, 30),
        source_id="norgate_us_equities_total_return",
        price_adjustment="total_return_adjusted_close",
    )

    assert [item.ticker for item in summaries] == ["SPY", "XLI"]
    with sqlite3.connect(database_path) as connection:
        rows = connection.execute(
            """
            SELECT ticker, MIN(bar_date), MAX(bar_date), COUNT(*),
                   MIN(is_adjusted), MIN(price_adjustment)
            FROM fact_price_ohlcv
            GROUP BY ticker
            ORDER BY ticker
            """
        ).fetchall()
    assert rows == [
        ("SPY", "2026-07-29", "2026-07-30", 2, 1, "total_return_adjusted_close"),
        ("XLI", "2026-07-29", "2026-07-30", 2, 1, "total_return_adjusted_close"),
    ]


def test_hydration_rejects_stale_market_instrument_history(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="stale for SPY"):
        hydrate_market_instruments(
            FakeProvider(last_date="2026-07-29"),
            database_path=tmp_path / "market_instruments.sqlite",
            tickers=["SPY"],
            start=date(2026, 7, 1),
            end=date(2026, 7, 30),
            source_id="norgate_us_equities_total_return",
            price_adjustment="total_return_adjusted_close",
        )


def test_optional_scored_ticker_hydration_skips_unknown_symbols(
    tmp_path: Path,
) -> None:
    summaries = hydrate_market_instruments(
        FakeProvider(missing_tickers={"MISSING"}),
        database_path=tmp_path / "market_instruments.sqlite",
        tickers=["SPY", "MISSING"],
        start=date(2026, 7, 1),
        end=date(2026, 7, 30),
        source_id="norgate_us_equities_total_return",
        price_adjustment="total_return_adjusted_close",
        allow_missing=True,
    )

    assert [item.ticker for item in summaries] == ["SPY"]


def test_purge_cached_tickers_is_source_scoped(tmp_path: Path) -> None:
    database_path = tmp_path / "market_instruments.sqlite"
    hydrate_market_instruments(
        FakeProvider(),
        database_path=database_path,
        tickers=["P", "SPY"],
        start=date(2026, 7, 1),
        end=date(2026, 7, 30),
        source_id="norgate_us_equities_total_return",
        price_adjustment="total_return_adjusted_close",
    )

    assert purge_cached_tickers(
        database_path,
        tickers={"P"},
        source_id="norgate_us_equities_total_return",
    ) == 2
    with sqlite3.connect(database_path) as connection:
        assert connection.execute(
            "SELECT DISTINCT ticker FROM fact_price_ohlcv ORDER BY ticker"
        ).fetchall() == [("SPY",)]
