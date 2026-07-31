from __future__ import annotations

import sqlite3
from pathlib import Path

from industrials.transportation.required_metric_repair import (
    REPAIR_SCOPE_VERSION,
    build_accession_manifest,
    build_repair_contract,
    read_scope,
)


def _connection() -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.executescript(
        """
        CREATE TABLE feature_financial_metric_availability (
            ticker TEXT, model_family TEXT, asof_date TEXT,
            metric_name TEXT, availability_status TEXT,
            metric_value TEXT, status_reason TEXT
        );
        CREATE TABLE feature_financial_statement (
            ticker TEXT, model_family TEXT, asof_date TEXT,
            fiscal_period_end TEXT, source_id TEXT,
            operating_margin REAL, fcf_margin REAL,
            cash_runway_years REAL, capital_raise_dependence REAL
        );
        CREATE TABLE fact_financial_statement_canonical (
            ticker TEXT, model_family TEXT, canonical_metric TEXT,
            period_end TEXT, filing_date TEXT, accession_number TEXT,
            form_type TEXT, taxonomy TEXT, concept_name TEXT,
            source_id TEXT, source_priority INTEGER
        );
        CREATE TABLE fact_price_ohlcv (
            ticker TEXT, bar_date TEXT, adj_close REAL
        );
        CREATE TABLE fact_sec_filing (
            ticker TEXT, cik TEXT, source_id TEXT,
            accession_number TEXT, form_type TEXT, filing_date TEXT,
            accepted_at TEXT, report_date TEXT, primary_document TEXT
        );
        """
    )
    return connection


def test_scope_is_exactly_19_tickers_and_32_pairs() -> None:
    path = (
        Path(__file__).resolve().parents[2]
        / "industrials"
        / "transportation"
        / "review_policies"
        / "transportation_required_metric_repair_scope.csv"
    )
    rows = read_scope(path)

    assert len(rows) == 32
    assert len({row["ticker"] for row in rows}) == 19
    assert {
        row["ticker"] for row in rows if row["source_type"] == "market"
    } == {"RUBI"}


def test_rubi_is_market_only_and_never_enters_filing_manifest() -> None:
    connection = _connection()
    connection.executemany(
        "INSERT INTO fact_price_ohlcv VALUES ('RUBI', ?, 10.0)",
        [(f"2026-01-{day:02d}",) for day in range(1, 11)],
    )
    scope = [
        {
            "scope_version": REPAIR_SCOPE_VERSION,
            "ticker": "RUBI",
            "metric_name": "maximum_drawdown_12m",
            "source_type": "market",
            "required_dependencies": "adjusted_price_history",
            "repair_objective": "wait_for_252_valid_adjusted_bars",
            "include_in_filing_pass": "0",
            "notes": "market only",
        }
    ]

    pairs, dependencies = build_repair_contract(
        connection, scope_rows=scope, asof_date="2026-07-30"
    )
    accessions = build_accession_manifest(
        connection, pair_rows=pairs, asof_date="2026-07-30"
    )

    assert dependencies == []
    assert pairs[0]["repair_classification"] == "INSUFFICIENT_MARKET_HISTORY"
    assert pairs[0]["include_in_filing_pass"] == 0
    assert accessions == []


def test_source_manifest_excludes_future_report_periods() -> None:
    connection = _connection()
    connection.executemany(
        """
        INSERT INTO fact_sec_filing
        VALUES ('AAA', '1', 'sec', ?, '10-Q', ?, ?, ?, ?)
        """,
        [
            (
                "0001-26-000001",
                "2026-07-29",
                "2026-07-29",
                "2026-06-30",
                "q2.htm",
            ),
            (
                "0001-26-000002",
                "2026-07-29",
                "2026-07-29",
                "2026-07-31",
                "future.htm",
            ),
        ],
    )
    pair = {
        "ticker": "AAA",
        "metric_name": "fcf_margin",
        "source_type": "financial",
        "required_dependencies": "revenue|operating_cash_flow|capex",
        "include_in_filing_pass": 1,
        "repair_classification": "SOURCE_OR_PERIOD_GAP",
    }

    rows = build_accession_manifest(
        connection,
        pair_rows=[pair],
        asof_date="2026-07-30",
        annual_limit=1,
        interim_limit=8,
    )

    assert [row["accession_number"] for row in rows] == [
        "0001-26-000001"
    ]
