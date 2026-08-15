from __future__ import annotations

import sqlite3

from industrials.core.financial_filing_lineage import build_financial_filing_lineage


def test_nonfinancial_quarter_end_6k_does_not_supersede_results_filing() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE fact_sec_filing (
            ticker TEXT, cik TEXT, accession_number TEXT, form_type TEXT,
            filing_date TEXT, accepted_at TEXT, report_date TEXT,
            primary_document TEXT, filing_url TEXT
        );
        CREATE TABLE fact_financial_statement_canonical (
            ticker TEXT, model_family TEXT, canonical_metric TEXT,
            accession_number TEXT, accepted_at TEXT, filing_date TEXT
        );
        CREATE TABLE feature_financial_statement (
            ticker TEXT, model_family TEXT, asof_date TEXT
        );
        CREATE TABLE feature_scoring_input (
            ticker TEXT, model_family TEXT, asof_date TEXT,
            financial_feature_asof_date TEXT,
            financial_source_accession TEXT,
            financial_source_fiscal_period_end TEXT,
            financial_source_feature_updated_at TEXT,
            updated_at TEXT
        );
        CREATE TABLE raw_api_responses (
            endpoint TEXT, query_params_json TEXT, request_time_utc TEXT,
            response_status INTEGER, asof_date TEXT
        );
        CREATE TABLE sec_parser_document_catalog (
            accession_number TEXT, source_path TEXT,
            is_full_submission INTEGER, is_primary INTEGER, file_size INTEGER
        );
        """
    )
    conn.executemany(
        "INSERT INTO fact_sec_filing VALUES (?,?,?,?,?,?,?,?,?)",
        [
            (
                "TSM", "0001046179", "results", "6-K", "2026-07-16", "2026-07-16T08:00:00",
                "2026-06-30", "results.htm", "",
            ),
            (
                "TSM", "0001046179", "governance", "6-K", "2026-07-24", "2026-07-24T08:00:00",
                "2026-06-30", "governance.htm", "",
            ),
        ],
    )
    conn.executemany(
        "INSERT INTO fact_financial_statement_canonical VALUES (?,?,?,?,?,?)",
        [
            ("TSM", "semiconductors", metric, "results", "2026-07-16", "2026-07-16")
            for metric in ("revenue", "assets", "operating_income")
        ],
    )
    conn.execute(
        "INSERT INTO raw_api_responses VALUES (?,?,?,?,?)",
        (
            "https://data.sec.gov/submissions/CIK0001046179.json",
            '{"payload_source":"live","response_kind":"root_submissions"}',
            "2026-08-14T09:59:00Z",
            200,
            "2026-08-14",
        ),
    )
    conn.execute(
        "INSERT INTO feature_scoring_input VALUES (?,?,?,?,?,?,?,?)",
        (
            "TSM",
            "semiconductors",
            "2026-08-14",
            "2026-07-16",
            "results",
            "2026-06-30",
            "2026-08-14T10:00:00Z",
            "2026-08-14T10:01:00Z",
        ),
    )

    lineage = build_financial_filing_lineage(
        conn,
        model_family="semiconductors",
        asof="2026-08-14",
        tickers=["TSM"],
    )["TSM"]

    assert lineage["financial_lineage_gate"] == "1"
    assert lineage["latest_material_financial_accession"] == "results"
    assert lineage["incorporated_financial_accession"] == "results"



def test_score_source_accession_and_timestamp_fail_closed() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE fact_sec_filing (
            ticker TEXT, cik TEXT, accession_number TEXT, form_type TEXT,
            filing_date TEXT, accepted_at TEXT, report_date TEXT,
            primary_document TEXT, filing_url TEXT
        );
        CREATE TABLE fact_financial_statement_canonical (
            ticker TEXT, model_family TEXT, canonical_metric TEXT,
            accession_number TEXT, accepted_at TEXT, filing_date TEXT
        );
        CREATE TABLE feature_financial_statement (
            ticker TEXT, model_family TEXT, asof_date TEXT
        );
        CREATE TABLE feature_scoring_input (
            ticker TEXT, model_family TEXT, asof_date TEXT,
            financial_feature_asof_date TEXT,
            financial_source_accession TEXT,
            financial_source_fiscal_period_end TEXT,
            financial_source_feature_updated_at TEXT,
            updated_at TEXT
        );
        CREATE TABLE raw_api_responses (
            endpoint TEXT, query_params_json TEXT, request_time_utc TEXT,
            response_status INTEGER, asof_date TEXT
        );
        CREATE TABLE sec_parser_document_catalog (
            accession_number TEXT, source_path TEXT,
            is_full_submission INTEGER, is_primary INTEGER, file_size INTEGER
        );
        """
    )
    conn.execute(
        "INSERT INTO fact_sec_filing VALUES (?,?,?,?,?,?,?,?,?)",
        (
            "TSM",
            "0001046179",
            "results",
            "6-K",
            "2026-07-16",
            "2026-07-16T08:00:00",
            "2026-06-30",
            "results.htm",
            "",
        ),
    )
    conn.executemany(
        "INSERT INTO fact_financial_statement_canonical VALUES (?,?,?,?,?,?)",
        [
            ("TSM", "semiconductors", metric, "results", "2026-07-16", "2026-07-16")
            for metric in ("revenue", "assets", "operating_income")
        ],
    )
    conn.execute(
        "INSERT INTO raw_api_responses VALUES (?,?,?,?,?)",
        (
            "https://data.sec.gov/submissions/CIK0001046179.json",
            '{"payload_source":"live","response_kind":"root_submissions"}',
            "2026-08-14T09:59:00Z",
            200,
            "2026-08-14",
        ),
    )
    conn.execute(
        "INSERT INTO feature_scoring_input VALUES (?,?,?,?,?,?,?,?)",
        (
            "TSM",
            "semiconductors",
            "2026-08-14",
            "2026-07-16",
            "stale-accession",
            "2026-03-31",
            "2026-08-14T10:00:00Z",
            "2026-08-14T10:01:00Z",
        ),
    )

    mismatch = build_financial_filing_lineage(
        conn,
        model_family="semiconductors",
        asof="2026-08-14",
        tickers=["TSM"],
    )["TSM"]

    assert mismatch["financial_lineage_gate"] == "0"
    assert mismatch["financial_lineage_reason"].startswith(
        "score_input_financial_source_not_latest_material_event"
    )

    conn.execute(
        """
        UPDATE feature_scoring_input
        SET financial_source_accession = 'results',
            financial_source_feature_updated_at = '2026-08-14T10:02:00Z',
            updated_at = '2026-08-14T10:01:00Z'
        """
    )
    out_of_order = build_financial_filing_lineage(
        conn,
        model_family="semiconductors",
        asof="2026-08-14",
        tickers=["TSM"],
    )["TSM"]

    assert out_of_order["financial_lineage_gate"] == "0"
    assert (
        out_of_order["financial_lineage_reason"]
        == "score_input_precedes_selected_financial_feature"
    )
