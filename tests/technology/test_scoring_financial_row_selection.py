from __future__ import annotations

import sqlite3
from datetime import date

from technology.core.scoring_features import FINANCIAL_LATEST_ORDER, latest_row


def test_richer_statement_wins_over_sparse_6k_for_same_period() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE feature_financial_statement(
            ticker TEXT, source_id TEXT, model_family TEXT, asof_date TEXT,
            fiscal_period_end TEXT, accession_number TEXT,
            data_quality_status TEXT, revenue REAL, gross_profit REAL,
            operating_income REAL, net_income REAL, assets REAL, equity REAL,
            cash_and_equivalents REAL, operating_cash_flow REAL,
            free_cash_flow REAL, diluted_shares REAL
        )
        """
    )
    rows = [
        (
            "POET", "sec", "semiconductors", "2026-03-31", "2025-12-31",
            "annual", "review", 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0,
        ),
        (
            "POET", "sec", "semiconductors", "2026-04-01", "2025-12-31",
            "sparse_6k", "review", 1.0, None, None, 1.0, None, None, None, 1.0, None, None,
        ),
    ]
    conn.executemany(
        "INSERT INTO feature_financial_statement VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        rows,
    )

    selected = latest_row(
        conn,
        "feature_financial_statement",
        "POET",
        "sec",
        "semiconductors",
        date(2026, 8, 14),
        FINANCIAL_LATEST_ORDER,
    )

    assert selected is not None
    assert selected["accession_number"] == "annual"
