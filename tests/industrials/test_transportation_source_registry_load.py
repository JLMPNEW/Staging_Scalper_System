from __future__ import annotations

import sqlite3

from industrials.transportation.source_registry_load import (
    apply_source_registry_load,
    plan_source_registry_load,
)


def _database() -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.executescript(
        """
        CREATE TABLE fact_sec_filing(
            ticker TEXT NOT NULL,
            cik TEXT,
            source_id TEXT NOT NULL,
            accession_number TEXT NOT NULL,
            form_type TEXT NOT NULL,
            filing_date TEXT NOT NULL,
            accepted_at TEXT,
            report_date TEXT,
            fiscal_year INTEGER,
            fiscal_period TEXT,
            primary_document TEXT,
            filing_url TEXT,
            reporting_standard TEXT,
            taxonomy TEXT,
            source_detail TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY(ticker, accession_number, source_id)
        );
        """
    )
    return connection


def test_registry_load_is_append_only_and_idempotent() -> None:
    connection = _database()
    filing = {
        "ticker": "TST",
        "cik": "1",
        "accession_number": "0000000001-24-000001",
        "form_type": "8-K",
        "filing_date": "2024-02-01",
        "accepted_at": "2024-02-01T16:00:00Z",
        "report_date": "2023-12-31",
        "primary_document": "test.htm",
        "submissions_source_file": "CIK0000000001.json",
    }
    planned, errors = plan_source_registry_load(
        connection,
        filing_rows=[filing],
        source_id="sec_submissions",
    )
    assert errors == []
    assert planned[0]["load_action"] == "INSERT_MISSING"
    assert apply_source_registry_load(
        connection,
        planned_rows=planned,
    ) == 1
    second, second_errors = plan_source_registry_load(
        connection,
        filing_rows=[filing],
        source_id="sec_submissions",
    )
    assert second_errors == []
    assert second[0]["load_action"] == "KEEP_LOADER_INSERT"
    assert second[0]["managed_by_loader"] == 1
    assert apply_source_registry_load(
        connection,
        planned_rows=second,
    ) == 0
    row = connection.execute(
        """
        SELECT form_type, primary_document, source_detail
        FROM fact_sec_filing
        """
    ).fetchone()
    assert tuple(row) == (
        "8-K",
        "test.htm",
        "sec_submissions_source_exhaustion:CIK0000000001.json",
    )
