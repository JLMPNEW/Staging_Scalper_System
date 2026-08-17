from __future__ import annotations

import sqlite3

from dedicated_parser.contracts import FilingRef
from technology.core.financial_filing_recovery import (
    RecoveredFact,
    _mapped_metrics,
    _upsert_raw_facts,
)


def test_recovery_rerun_replaces_stale_period_rows_for_accession() -> None:
    conn = sqlite3.connect(":memory:")
    conn.execute(
        """
        CREATE TABLE fact_sec_xbrl_fact_raw(
            fact_key TEXT PRIMARY KEY, ticker TEXT, cik TEXT, source_id TEXT,
            taxonomy TEXT, concept TEXT, unit TEXT, value REAL,
            start_date TEXT, end_date TEXT, fiscal_year INTEGER,
            fiscal_period TEXT, form_type TEXT, filing_date TEXT,
            accession_number TEXT, frame TEXT, period_type TEXT,
            source_detail TEXT, source_accession_url TEXT,
            source_payload_hash TEXT, created_at TEXT, updated_at TEXT
        )
        """
    )
    filing = FilingRef(
        ticker="POET",
        cik="0001437424",
        accession_number="0001171843-26-002167",
        form_type="6-K",
        filing_date="2026-04-01",
        accepted_at="2026-04-01T06:00:00Z",
        report_date="2026-03-31",
        primary_document="f6k_040126.htm",
        source_id="sec_submissions",
    )
    stale = RecoveredFact(
        taxonomy="ifrs-full",
        concept="Revenue",
        unit="USD",
        value=341_202.0,
        start_date="2026-01-01",
        end_date="2026-03-31",
        period_type="duration",
        frame="explicit_table:exh_991.htm:1:1",
        source_document="exh_991.htm",
        source_detail="filing_document_explicit_statement_table",
        content_sha256="a" * 64,
    )
    corrected = RecoveredFact(
        taxonomy="ifrs-full",
        concept="Revenue",
        unit="USD",
        value=341_202.0,
        start_date="2025-10-01",
        end_date="2025-12-31",
        period_type="duration",
        frame="explicit_table:exh_991.htm:1:1",
        source_document="exh_991.htm",
        source_detail="filing_document_explicit_statement_table",
        content_sha256="a" * 64,
    )

    _upsert_raw_facts(conn, filing=filing, source_id="recovery", facts=[stale])
    _upsert_raw_facts(conn, filing=filing, source_id="recovery", facts=[corrected])

    rows = conn.execute("SELECT start_date, end_date FROM fact_sec_xbrl_fact_raw").fetchall()
    assert rows == [("2025-10-01", "2025-12-31")]


def test_metric_mapping_supports_industrials_concept_name_schema() -> None:
    conn = sqlite3.connect(":memory:")
    conn.execute(
        """
        CREATE TABLE dim_xbrl_concept_map(
            taxonomy TEXT,
            concept_name TEXT,
            canonical_metric TEXT,
            active_flag INTEGER
        )
        """
    )
    conn.execute(
        "INSERT INTO dim_xbrl_concept_map VALUES (?, ?, ?, ?)",
        ("ifrs-full", "Revenue", "revenue", 1),
    )
    fact = RecoveredFact(
        taxonomy="ifrs-full",
        concept="Revenue",
        unit="USD",
        value=1.0,
        start_date="2026-01-01",
        end_date="2026-03-31",
        period_type="duration",
        frame="test",
        source_document="test.htm",
        source_detail="test",
        content_sha256="hash",
    )

    assert _mapped_metrics(conn, [fact]) == {"revenue"}


def test_upsert_supports_industrials_raw_fact_schema() -> None:
    conn = sqlite3.connect(":memory:")
    conn.execute(
        """
        CREATE TABLE fact_sec_xbrl_fact_raw(
            raw_fact_id INTEGER PRIMARY KEY,
            fact_key TEXT NOT NULL UNIQUE,
            ticker TEXT NOT NULL,
            cik TEXT,
            source_id TEXT NOT NULL,
            accession_number TEXT,
            form_type TEXT,
            filing_date TEXT,
            accepted_at TEXT,
            fiscal_year INTEGER,
            fiscal_period TEXT,
            period_start TEXT,
            period_end TEXT,
            frame TEXT,
            taxonomy TEXT NOT NULL,
            concept_name TEXT NOT NULL,
            unit TEXT,
            raw_value REAL,
            decimals TEXT,
            source_detail TEXT,
            payload_json TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    filing = FilingRef(
        ticker="MOB",
        cik="0001898643",
        accession_number="0001213900-26-061495",
        form_type="6-K",
        filing_date="2026-05-27",
        accepted_at="2026-05-27T08:00:00Z",
        report_date="2026-05-27",
        primary_document="mobilicom.htm",
        source_id="sec_submissions",
    )
    fact = RecoveredFact(
        taxonomy="ifrs-full",
        concept="Revenue",
        unit="USD",
        value=548_000.0,
        start_date="2026-01-01",
        end_date="2026-03-31",
        period_type="duration",
        frame="test",
        source_document="mobilicom.htm",
        source_detail="filing_document_explicit_financial_prose",
        content_sha256="hash",
    )

    inserted = _upsert_raw_facts(
        conn,
        filing=filing,
        source_id="sec_companyfacts",
        facts=[fact],
    )

    row = conn.execute("SELECT concept_name, raw_value, period_end FROM fact_sec_xbrl_fact_raw").fetchone()
    assert inserted == 1
    assert row == ("Revenue", 548_000.0, "2026-03-31")
