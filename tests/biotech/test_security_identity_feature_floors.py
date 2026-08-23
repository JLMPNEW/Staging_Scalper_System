from __future__ import annotations

import sqlite3
from datetime import date

from biotech_index.core.security_identity import SecurityIdentityRule
from tests.biotech.conftest import load_script_module


def make_rule(ticker: str, start: date, *, cik: str = "0000000002", historical_ciks: tuple[str, ...] = ()) -> SecurityIdentityRule:
    return SecurityIdentityRule(
        ticker=ticker,
        company_name=ticker,
        cik=cik,
        historical_ciks=historical_ciks,
        calibration_cohort="platform_partnered_modality_pipeline",
        membership_start_date=start,
        membership_end_date=None,
        historical_price_ticker=ticker,
        institutional_13f_issuer_aliases=(),
        cusip="",
        source_reference="test",
    )


def test_financial_and_sec_feature_loaders_exclude_pre_identity_rows() -> None:
    survival = load_script_module("16_build_financial_survival_features.py", "survival_identity_floor")
    features = load_script_module("10_build_biotech_features.py", "feature_identity_floor")
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE company_facts_quarterly(company_id INTEGER, period_end TEXT, filed_date TEXT, fiscal_period TEXT);
        INSERT INTO company_facts_quarterly VALUES (1, '2023-06-30', '2023-08-01', 'Q2');
        INSERT INTO company_facts_quarterly VALUES (1, '2023-12-31', '2024-02-15', 'FY');
        CREATE TABLE sec_filings(company_id INTEGER, form TEXT, filing_date TEXT);
        INSERT INTO sec_filings VALUES (1, '10-Q', '2023-08-01');
        INSERT INTO sec_filings VALUES (1, '8-K', '2023-11-01');
        CREATE TABLE sec_events(
            event_id INTEGER PRIMARY KEY,
            company_id INTEGER,
            filing_date TEXT,
            form TEXT,
            event_type TEXT,
            event_date TEXT,
            event_value TEXT,
            polarity TEXT,
            confidence REAL,
            extracted_text TEXT,
            accession_nodash TEXT
        );
        INSERT INTO sec_events VALUES (1, 1, '2023-08-01', '10-Q', 'public_offering', '', '', 'neutral', 0.9, 'completed public offering', 'old');
        INSERT INTO sec_events VALUES (2, 1, '2023-11-01', '8-K', 'public_offering', '', '', 'neutral', 0.9, 'completed public offering', 'new');
        """
    )
    floors = {1: date(2023, 10, 31)}
    facts = survival.load_fact_rows_bulk(conn, [1], date(2024, 3, 1), identity_start_dates=floors)
    filings = features.load_recent_sec_filing_summary(conn, date(2024, 3, 1), identity_start_dates=floors)
    events = features.load_recent_sec_event_summary(conn, date(2024, 3, 1), identity_start_dates=floors)

    assert [row["period_end"] for row in facts[1]] == ["2023-12-31"]
    assert filings[1]["recent_sec_filing_count_2y"] == 1
    assert filings[1]["recent_current_report_count_2y"] == 1
    assert events[1]["counts"] == {"public_offering": 1}


def test_form4_loader_maps_reviewed_historical_cik_and_applies_identity_floor() -> None:
    governance = load_script_module("20_build_governance_event_features.py", "form4_historical_cik_floor")
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE form4_events_tier1(
            issuer_trading_symbol TEXT,
            issuer_cik TEXT,
            trans_date TEXT,
            filing_date TEXT,
            is_current_truth INTEGER
        );
        INSERT INTO form4_events_tier1 VALUES ('ATAI', '1840904', '2021-07-01', '2021-07-02', 1);
        INSERT INTO form4_events_tier1 VALUES ('ATAI', '1840904', '2021-05-01', '2021-05-02', 1);
        """
    )
    companies = [{"company_id": 1, "ticker": "ATAI", "cik": "0002081043"}]
    rules = {"ATAI": make_rule("ATAI", date(2021, 6, 18), cik="0002081043", historical_ciks=("0001840904",))}

    rows, error = governance.load_form4_rows_bulk(
        conn,
        table="form4_events_tier1",
        companies=companies,
        start_date=date(2021, 1, 1),
        asof_date=date(2021, 12, 31),
        security_identity_rules=rules,
    )

    assert error == ""
    assert [row["trans_date"] for row in rows[1]] == ["2021-07-01"]


def test_sec_governance_loader_applies_floor_before_document_rank_limit() -> None:
    governance = load_script_module("20_build_governance_event_features.py", "sec_governance_identity_floor")
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE sec_filings(company_id INTEGER, accession_nodash TEXT, form TEXT, filing_date TEXT);
        CREATE TABLE sec_filing_latest_document(accession_nodash TEXT, document_url TEXT, document_type TEXT, text_hash TEXT);
        CREATE TABLE sec_filing_documents(document_id INTEGER, accession_nodash TEXT, document_url TEXT, document_type TEXT, text_hash TEXT, text_length INTEGER, text_content TEXT, fetched_at TEXT, updated_at TEXT, created_at TEXT);
        INSERT INTO sec_filings VALUES (1, '000000000123000001', '8-K', '2023-10-01');
        INSERT INTO sec_filings VALUES (1, '000000000123000002', '8-K', '2023-11-01');
        INSERT INTO sec_filing_latest_document VALUES ('000000000123000001', 'old', '8-K', 'oldhash');
        INSERT INTO sec_filing_latest_document VALUES ('000000000123000002', 'new', '8-K', 'newhash');
        """
    )

    docs, _ = governance.load_sec_governance_inputs_bulk(
        conn,
        company_ids=[1],
        asof_date=date(2023, 12, 1),
        config={"governance_events": {"sec_event_lookback_days": 365, "sec_document_max_per_company": 1}},
        identity_start_dates={1: date(2023, 10, 31)},
    )

    assert [row["document_url"] for row in docs[1]] == ["new"]
