from __future__ import annotations

import sqlite3

from industrials.core.financial_filing_lineage import build_financial_filing_lineage


def test_nonfinancial_quarter_end_6k_does_not_supersede_results_filing() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE fact_sec_filing (
            ticker TEXT, accession_number TEXT, form_type TEXT,
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
        CREATE TABLE sec_parser_document_catalog (
            accession_number TEXT, source_path TEXT,
            is_full_submission INTEGER, is_primary INTEGER, file_size INTEGER
        );
        """
    )
    conn.executemany(
        "INSERT INTO fact_sec_filing VALUES (?,?,?,?,?,?,?,?)",
        [
            (
                "TSM", "results", "6-K", "2026-07-16", "2026-07-16T08:00:00",
                "2026-06-30", "results.htm", "",
            ),
            (
                "TSM", "governance", "6-K", "2026-07-24", "2026-07-24T08:00:00",
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

    lineage = build_financial_filing_lineage(
        conn,
        model_family="semiconductors",
        asof="2026-08-14",
        tickers=["TSM"],
    )["TSM"]

    assert lineage["financial_lineage_gate"] == "1"
    assert lineage["latest_material_financial_accession"] == "results"
    assert lineage["incorporated_financial_accession"] == "results"
