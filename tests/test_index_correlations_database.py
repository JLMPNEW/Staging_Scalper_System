from __future__ import annotations

import json
import sqlite3
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from index_correlations.dashboard_data import (
    DashboardArtifactError,
    latest_publication_dir,
    load_verified_manifest,
    load_verified_rolling,
)
from index_correlations.pipeline import (
    DatabaseCoverageError,
    DatabasePriceSpec,
    PublicationError,
    SOURCE_ID,
    SOURCE_PRICE_ADJUSTMENT,
    SourceContractError,
    _series_sha256,
    build_artifacts,
    load_prices_from_databases,
)


def _create_price_db(path) -> None:
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TABLE bars (ticker TEXT, bar_date TEXT, source TEXT, adjusted REAL)"
        )
        connection.executemany(
            "INSERT INTO bars VALUES (?, ?, ?, ?)",
            [
                ("XBI", "2024-01-02", "canonical", 10.0),
                ("XBI", "2024-01-03", "canonical", 11.0),
                ("IHI", "2024-01-02", "canonical", 20.0),
                ("IHI", "2024-01-03", "canonical", 21.0),
            ],
        )


def _create_stage2_contract(
    root: Path,
    *,
    drop_last_for: str | None = None,
    drop_date_for: tuple[str, str] | None = None,
) -> tuple[Path, Path, list[str]]:
    database_path = root / "norgate_market_instruments.sqlite"
    manifest_path = root / "norgate_market_instruments_manifest.json"
    dates = [
        "2024-01-02",
        "2024-01-03",
        "2024-01-04",
        "2024-01-05",
        "2024-01-08",
        "2024-01-09",
    ]
    values = {
        "AAA": [100.0, 101.0, 99.0, 102.0, 104.0, 103.0],
        "BBB": [50.0, 49.0, 50.0, 51.0, 50.0, 52.0],
    }
    source_rows: dict[str, list[tuple[str, float]]] = {}
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            CREATE TABLE fact_price_ohlcv(
                ticker TEXT NOT NULL,
                bar_date TEXT NOT NULL,
                source_id TEXT NOT NULL,
                adj_close REAL NOT NULL,
                price_adjustment TEXT NOT NULL,
                is_adjusted INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY(ticker, bar_date, source_id)
            )
            """
        )
        for ticker, ticker_values in values.items():
            rows = list(zip(dates, ticker_values, strict=True))
            if ticker == drop_last_for:
                rows = rows[:-1]
            if drop_date_for and ticker == drop_date_for[0]:
                rows = [row for row in rows if row[0] != drop_date_for[1]]
            source_rows[ticker] = rows
            connection.executemany(
                "INSERT INTO fact_price_ohlcv VALUES (?, ?, ?, ?, ?, 1, ?, ?)",
                [
                    (
                        ticker,
                        bar_date,
                        SOURCE_ID,
                        value,
                        SOURCE_PRICE_ADJUSTMENT,
                        "2024-01-10T00:00:00Z",
                        "2024-01-10T00:00:00Z",
                    )
                    for bar_date, value in rows
                ],
            )
    instruments = [
        {
            "ticker": ticker,
            "row_count": len(rows),
            "first_date": rows[0][0],
            "last_date": rows[-1][0],
            "extracted_sha256": _series_sha256(rows),
        }
        for ticker, rows in source_rows.items()
    ]
    manifest_path.write_text(
        json.dumps(
            {
                "acceptance": "PASS",
                "as_of": dates[-1],
                "database_path": str(database_path.resolve()),
                "instruments": instruments,
            }
        ),
        encoding="utf-8",
    )
    return database_path, manifest_path, dates


def test_database_reader_uses_only_the_declared_sqlite_source(tmp_path) -> None:
    db_path = tmp_path / "prices.sqlite"
    _create_price_db(db_path)
    specs = {
        ticker: DatabasePriceSpec("prices.sqlite", "bars", "source", "canonical", "bar_date", "adjusted")
        for ticker in ("XBI", "IHI")
    }

    prices = load_prices_from_databases(tmp_path, ("XBI", "IHI"), specs=specs)

    assert list(prices.columns) == ["XBI", "IHI"]
    assert list(pd.DatetimeIndex(prices.index).strftime("%Y-%m-%d")) == [
        "2024-01-02",
        "2024-01-03",
    ]
    assert prices.loc["2024-01-03", "IHI"] == 21.0


def test_database_reader_fails_closed_when_a_required_ticker_is_missing(tmp_path) -> None:
    db_path = tmp_path / "prices.sqlite"
    _create_price_db(db_path)
    specs = {
        ticker: DatabasePriceSpec("prices.sqlite", "bars", "source", "canonical", "bar_date", "adjusted")
        for ticker in ("XBI", "IHI")
    }

    with pytest.raises(DatabaseCoverageError, match="SOXX"):
        load_prices_from_databases(tmp_path, ("XBI", "SOXX"), specs=specs)


def test_database_reader_rejects_duplicate_dates(tmp_path) -> None:
    db_path = tmp_path / "prices.sqlite"
    _create_price_db(db_path)
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "INSERT INTO bars VALUES (?, ?, ?, ?)",
            ("XBI", "2024-01-03", "canonical", 12.0),
        )
    specs = {
        ticker: DatabasePriceSpec(
            "prices.sqlite", "bars", "source", "canonical", "bar_date", "adjusted"
        )
        for ticker in ("XBI", "IHI")
    }
    with pytest.raises(DatabaseCoverageError, match="duplicate price dates"):
        load_prices_from_databases(tmp_path, ("XBI", "IHI"), specs=specs)


def test_stage2_contract_publishes_only_derived_as_of_outputs(tmp_path) -> None:
    database, source_manifest, dates = _create_stage2_contract(tmp_path)
    output_root = tmp_path / "output"
    requested = date.fromisoformat(dates[3])

    manifest = build_artifacts(
        requested,
        output_root=output_root,
        source_database=database,
        source_manifest=source_manifest,
        tickers=("AAA", "BBB"),
        windows=(3,),
        methods=("pearson", "kendall_tau"),
    )

    output_dir = output_root / requested.isoformat()
    assert manifest["acceptance"] == "PASS"
    assert manifest["external_requests"] == 0
    assert manifest["raw_price_or_return_artifacts_published"] is False
    assert manifest["source"]["manifest_as_of"] == dates[-1]
    assert {item["as_of_end_date"] for item in manifest["source_series"]} == {
        requested.isoformat()
    }
    assert {path.name for path in output_dir.iterdir()} == {
        "correlation_manifest.json",
        "correlation_validation.csv",
        "latest_correlations.csv",
        "rolling_kendall_tau_3.csv",
        "rolling_pearson_3.csv",
        "source_coverage.csv",
    }
    assert not any(
        token in path.name
        for path in output_dir.iterdir()
        for token in ("price", "return")
    )

    second = build_artifacts(
        requested,
        output_root=output_root,
        source_database=database,
        source_manifest=source_manifest,
        tickers=("AAA", "BBB"),
        windows=(3,),
        methods=("pearson", "kendall_tau"),
    )
    assert second == manifest

    assert latest_publication_dir(output_root) == output_dir
    dashboard_manifest = load_verified_manifest(
        output_dir,
        tickers=("AAA", "BBB"),
        windows=(3,),
        methods=("pearson", "kendall_tau"),
    )
    assert dashboard_manifest["as_of"] == requested.isoformat()
    rolling = load_verified_rolling(
        output_dir,
        "pearson",
        3,
        tickers=("AAA", "BBB"),
        windows=(3,),
        methods=("pearson", "kendall_tau"),
    )
    assert list(rolling.columns) == ["AAA__BBB"]
    assert str(rolling.index[-1])[:10] == requested.isoformat()


def test_stage2_contract_rejects_an_etf_stale_inside_current_manifest(tmp_path) -> None:
    database, source_manifest, dates = _create_stage2_contract(
        tmp_path, drop_last_for="BBB"
    )
    with pytest.raises(SourceContractError, match="do not reach"):
        build_artifacts(
            date.fromisoformat(dates[-1]),
            output_root=tmp_path / "output",
            source_database=database,
            source_manifest=source_manifest,
            tickers=("AAA", "BBB"),
            windows=(3,),
            methods=("pearson",),
        )


def test_stage2_contract_rejects_missing_requested_session(tmp_path) -> None:
    database, source_manifest, dates = _create_stage2_contract(
        tmp_path, drop_date_for=("BBB", "2024-01-05")
    )
    with pytest.raises(SourceContractError, match="exact as-of price missing"):
        build_artifacts(
            date.fromisoformat("2024-01-05"),
            output_root=tmp_path / "output",
            source_database=database,
            source_manifest=source_manifest,
            tickers=("AAA", "BBB"),
            windows=(2,),
            methods=("pearson",),
        )


def test_stage2_contract_rejects_database_restatement(tmp_path) -> None:
    database, source_manifest, dates = _create_stage2_contract(tmp_path)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE fact_price_ohlcv SET adj_close = 999 WHERE ticker = 'AAA' "
            "AND bar_date = '2024-01-04'"
        )
    with pytest.raises(SourceContractError, match="source series hash mismatch"):
        build_artifacts(
            date.fromisoformat(dates[-1]),
            output_root=tmp_path / "output",
            source_database=database,
            source_manifest=source_manifest,
            tickers=("AAA", "BBB"),
            windows=(3,),
            methods=("pearson",),
        )


def test_same_date_publication_detects_tampering_and_force_repairs(tmp_path) -> None:
    database, source_manifest, dates = _create_stage2_contract(tmp_path)
    output_root = tmp_path / "output"
    requested = date.fromisoformat(dates[-1])
    kwargs = {
        "output_root": output_root,
        "source_database": database,
        "source_manifest": source_manifest,
        "tickers": ("AAA", "BBB"),
        "windows": (3,),
        "methods": ("pearson",),
    }
    build_artifacts(requested, **kwargs)
    latest = output_root / requested.isoformat() / "latest_correlations.csv"
    latest.write_bytes(latest.read_bytes() + b"tampered")

    with pytest.raises(PublicationError, match="hash mismatch"):
        build_artifacts(requested, **kwargs)
    repaired = build_artifacts(requested, force=True, **kwargs)
    assert repaired["acceptance"] == "PASS"
    assert b"tampered" not in latest.read_bytes()


def test_dashboard_reader_rejects_unexpected_duplicate_data_file(tmp_path) -> None:
    database, source_manifest, dates = _create_stage2_contract(tmp_path)
    output_root = tmp_path / "output"
    requested = date.fromisoformat(dates[-1])
    build_artifacts(
        requested,
        output_root=output_root,
        source_database=database,
        source_manifest=source_manifest,
        tickers=("AAA", "BBB"),
        windows=(3,),
        methods=("pearson",),
    )
    output_dir = output_root / requested.isoformat()
    (output_dir / "raw_adjusted_close.csv").write_text(
        "date,AAA\n", encoding="utf-8"
    )

    with pytest.raises(DashboardArtifactError, match="unexpected files"):
        load_verified_manifest(
            output_dir,
            tickers=("AAA", "BBB"),
            windows=(3,),
            methods=("pearson",),
        )
