from __future__ import annotations

import runpy
import sqlite3
from datetime import date
from pathlib import Path

from med_devices.core.security_identity import (
    date_within_listing_identity,
    load_primary_security_identity_windows,
)


ROOT = Path(__file__).resolve().parents[2]


def memory_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE dim_company(
            company_id INTEGER PRIMARY KEY,
            ticker TEXT NOT NULL,
            is_active INTEGER NOT NULL DEFAULT 1
        );
        CREATE TABLE dim_security(
            security_id INTEGER PRIMARY KEY,
            company_id INTEGER NOT NULL,
            ticker TEXT NOT NULL,
            listing_start_date TEXT,
            is_primary_listing INTEGER NOT NULL DEFAULT 1
        );
        INSERT INTO dim_company(company_id, ticker, is_active) VALUES (1, 'SI', 1);
        INSERT INTO dim_security(
            security_id, company_id, ticker, listing_start_date, is_primary_listing
        ) VALUES (1, 1, 'SI', '2025-07-31', 1);
        """
    )
    return conn


def test_shared_identity_window_rejects_predecessor_dates() -> None:
    conn = memory_db()
    window = load_primary_security_identity_windows(conn)["SI"]
    assert not date_within_listing_identity(window, "2023-04-28")
    assert date_within_listing_identity(window, "2025-07-31")


def test_short_interest_and_short_volume_loaders_ignore_prelisting_rows() -> None:
    module = runpy.run_path(
        str(ROOT / "med_devices" / "scripts" / "56_build_med_device_short_interest_features.py")
    )
    conn = memory_db()
    conn.executescript(
        """
        CREATE TABLE fact_short_interest(
            ticker TEXT, settlement_date TEXT, source_id TEXT, company_id INTEGER,
            short_interest REAL, avg_daily_volume REAL, days_to_cover REAL,
            float_shares REAL, short_interest_pct_float REAL, publication_date TEXT
        );
        INSERT INTO fact_short_interest VALUES
          ('SI', '2023-04-28', 'exchange_short_interest', 1, 100, 10, 10, 1000, 0.10, '2023-05-08'),
          ('SI', '2025-08-15', 'exchange_short_interest', 1, 200, 40, 5, 2000, 0.10, '2025-08-25');

        CREATE TABLE fact_finra_short_volume(
            ticker TEXT, trade_date TEXT, source_id TEXT, company_id INTEGER,
            short_volume_ratio REAL
        );
        INSERT INTO fact_finra_short_volume VALUES
          ('SI', '2023-04-28', 'finra_regsho_short_volume', 1, 0.99),
          ('SI', '2025-08-15', 'finra_regsho_short_volume', 1, 0.40);
        """
    )
    config = {
        "short_interest_features": {
            "publication_lag_days": 8,
            "short_interest_source_ids": ["exchange_short_interest"],
            "short_volume_source_ids": ["finra_regsho_short_volume"],
            "short_volume_publication_lag_days": 1,
        }
    }
    short_interest = module["load_short_interest"](conn, asof="2025-09-01", config=config)
    assert [row["settlement_date"] for row in short_interest[1]] == ["2025-08-15"]
    short_volume = module["load_short_volume_stats"](
        conn,
        asof="2025-09-01",
        lookback_days=30,
        config=config,
    )
    assert short_volume[1]["short_volume_ratio_20d"] == 0.40


def test_borrow_and_form4_loaders_ignore_prelisting_rows() -> None:
    borrow_module = runpy.run_path(
        str(ROOT / "med_devices" / "scripts" / "54_build_med_device_borrow_features.py")
    )
    form4_module = runpy.run_path(
        str(ROOT / "med_devices" / "scripts" / "60_build_med_device_insider_activity_features.py")
    )
    conn = memory_db()
    conn.executescript(
        """
        CREATE TABLE fact_ibkr_borrow_snapshot(
            ticker TEXT, asof_date TEXT, source_id TEXT, company_id INTEGER,
            shortable_status REAL, shortable_shares REAL, borrow_fee_rate REAL
        );
        INSERT INTO fact_ibkr_borrow_snapshot VALUES
          ('SI', '2023-04-28', 'ibkr_borrow', 1, 3, 999999, 0.01),
          ('SI', '2025-08-15', 'ibkr_borrow', 1, 2, 1000, 0.05);

        CREATE TABLE fact_sec_form4_transaction(
            company_id INTEGER, ticker TEXT, transaction_date TEXT, derivative_flag INTEGER,
            transaction_code TEXT, filed_date TEXT, accepted_at TEXT
        );
        INSERT INTO fact_sec_form4_transaction VALUES
          (1, 'SI', '2023-04-28', 0, 'P', '2023-05-01', '2023-05-01T12:00:00Z'),
          (1, 'SI', '2025-08-15', 0, 'P', '2025-08-18', '2025-08-18T12:00:00Z');
        """
    )
    snapshots = borrow_module["load_latest_snapshots"](conn, asof="2025-09-01")
    assert snapshots[1]["asof_date"] == "2025-08-15"
    transactions = form4_module["load_transactions"](
        conn,
        asof="2025-09-01",
        lookback_days=1000,
        config={"insider_activity_features": {"filing_lag_days": 2}},
    )
    assert [row["transaction_date"] for row in transactions[1]] == ["2025-08-15"]


def test_prpo_financial_history_boundary_excludes_predecessor_company_rows() -> None:
    module = runpy.run_path(
        str(ROOT / "med_devices" / "scripts" / "06_build_med_device_financial_features.py")
    )
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE fact_financial_statement(
            company_id INTEGER, accession_nodash TEXT, period_end TEXT, fiscal_year INTEGER,
            fiscal_period TEXT, form TEXT, filed_date TEXT, revenue REAL, gross_profit REAL,
            operating_income REAL, net_income REAL, operating_cash_flow REAL,
            capital_expenditures REAL, free_cash_flow REAL, research_and_development REAL,
            interest_expense REAL, cash_and_investments REAL, total_debt REAL, total_assets REAL,
            stockholders_equity REAL, shares_outstanding REAL, payload_json TEXT
        );
        INSERT INTO fact_financial_statement VALUES
          (1, 'old', '2016-12-31', 2016, 'FY', '10-K', '2017-03-01', 100, 50, 1, 1, 1, 1, 0, 1, 0, 1, 1, 2, 1, 10, '{}'),
          (1, 'new', '2017-09-30', 2017, 'Q3', '10-Q', '2017-11-01', 10, 5, 1, 1, 1, 1, 0, 1, 0, 1, 1, 2, 1, 10, '{}');
        """
    )
    company = module["Company"](1, "PRPO", "Precipio", "diagnostics_clinical_tests")
    rows = module["load_financial_rows"](
        conn,
        [company],
        asof=date(2018, 1, 1),
        history_start_by_ticker={"PRPO": "2017-06-30"},
    )
    assert [row.accession_nodash for row in rows[1]] == ["new"]


def test_identity_repair_quarantines_before_deleting() -> None:
    module = runpy.run_path(
        str(ROOT / "med_devices" / "scripts" / "83_repair_med_device_security_identity_facts.py")
    )
    conn = memory_db()
    conn.executescript(
        """
        CREATE TABLE fact_short_interest(
            ticker TEXT, settlement_date TEXT, source_id TEXT, company_id INTEGER
        );
        INSERT INTO fact_short_interest VALUES
          ('SI', '2023-04-28', 'exchange_short_interest', 1),
          ('SI', '2025-08-15', 'exchange_short_interest', 1);
        """
    )
    violations = module["repair"](conn, dry_run=False)
    assert len(violations) == 1
    remaining = conn.execute(
        "SELECT settlement_date FROM fact_short_interest ORDER BY settlement_date"
    ).fetchall()
    assert [row["settlement_date"] for row in remaining] == ["2025-08-15"]
    quarantined = conn.execute(
        "SELECT observation_date FROM security_identity_fact_quarantine"
    ).fetchall()
    assert [row["observation_date"] for row in quarantined] == ["2023-04-28"]
