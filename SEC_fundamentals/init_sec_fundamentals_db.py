#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import sqlite3
from pathlib import Path

from sec_fundamentals_config import cfg_get, load_sec_fundamentals_config

DEFAULT_DB_PATH = Path(r"C:\Users\josel\Documents\PROD\DB\sec_fundamentals.sqlite")


def default_db_path() -> Path:
    return Path(os.getenv("SEC_FUNDAMENTALS_DB_PATH", str(DEFAULT_DB_PATH)))


DDL_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS sec_entity_universe (
        cik                     TEXT PRIMARY KEY,
        ticker                  TEXT,
        company_name            TEXT,
        universe_source         TEXT,
        active                  INTEGER NOT NULL DEFAULT 1,
        added_at_utc            TEXT NOT NULL,
        updated_at_utc          TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS sec_entity_profile (
        cik                                     TEXT PRIMARY KEY,
        entity_name                             TEXT,
        sic                                     TEXT,
        sic_description                         TEXT,
        category                                TEXT,
        fiscal_year_end                         TEXT,
        state_of_incorporation                  TEXT,
        state_of_incorporation_description       TEXT,
        phone                                   TEXT,
        website                                 TEXT,
        investor_website                        TEXT,
        description                             TEXT,
        insider_transaction_for_owner_exists    INTEGER,
        insider_transaction_for_issuer_exists   INTEGER,
        former_names_json                       TEXT,
        last_submissions_fetched_utc            TEXT,
        last_companyfacts_fetched_utc           TEXT,
        updated_at_utc                          TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS sec_entity_ticker_history (
        cik                     TEXT NOT NULL,
        ticker                  TEXT NOT NULL,
        exchange                TEXT NOT NULL DEFAULT '',
        is_current              INTEGER NOT NULL DEFAULT 0,
        as_of_date              TEXT NOT NULL DEFAULT '',
        source                  TEXT,
        updated_at_utc          TEXT NOT NULL,
        PRIMARY KEY (cik, ticker, exchange, as_of_date)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS sec_filing_index (
        accession_number        TEXT PRIMARY KEY,
        cik                     TEXT NOT NULL,
        company_name            TEXT,
        form_type               TEXT,
        filing_date             TEXT,
        acceptance_datetime     TEXT,
        report_period_end       TEXT,
        fiscal_year_focus       TEXT,
        fiscal_period_focus     TEXT,
        is_amendment            INTEGER NOT NULL DEFAULT 0,
        amendment_description   TEXT,
        primary_document        TEXT,
        primary_doc_description TEXT,
        items                   TEXT,
        film_number             TEXT,
        file_number             TEXT,
        size_bytes              INTEGER,
        is_xbrl                 INTEGER,
        is_inline_xbrl          INTEGER,
        source_json_page        TEXT,
        source_url              TEXT,
        created_at_utc          TEXT NOT NULL,
        updated_at_utc          TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS sec_dei_facts (
        accession_number                        TEXT PRIMARY KEY,
        cik                                     TEXT NOT NULL,
        trading_symbol                          TEXT,
        security_exchange_name                  TEXT,
        entity_common_stock_shares_outstanding  REAL,
        public_float                            REAL,
        filer_category                          TEXT,
        well_known_seasoned_issuer              TEXT,
        small_business_issuer                   TEXT,
        period_end_date                         TEXT,
        filed_date                              TEXT,
        acceptance_datetime                     TEXT,
        source_tags_json                        TEXT,
        updated_at_utc                          TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS sec_xbrl_facts_raw (
        fact_id                  INTEGER PRIMARY KEY AUTOINCREMENT,
        cik                      TEXT NOT NULL,
        accession_number         TEXT NOT NULL DEFAULT '',
        taxonomy                 TEXT NOT NULL,
        tag                      TEXT NOT NULL,
        label                    TEXT,
        unit                     TEXT NOT NULL DEFAULT '',
        value_text               TEXT NOT NULL DEFAULT '',
        value_num                REAL,
        frame                    TEXT NOT NULL DEFAULT '',
        form_type                TEXT NOT NULL DEFAULT '',
        fiscal_year              INTEGER,
        fiscal_period            TEXT NOT NULL DEFAULT '',
        period_start             TEXT NOT NULL DEFAULT '',
        period_end               TEXT NOT NULL DEFAULT '',
        filed_date               TEXT NOT NULL DEFAULT '',
        report_period_end        TEXT NOT NULL DEFAULT '',
        is_amendment             INTEGER NOT NULL DEFAULT 0,
        source                   TEXT NOT NULL DEFAULT 'companyfacts',
        loaded_at_utc            TEXT NOT NULL,
        UNIQUE (
            cik,
            accession_number,
            taxonomy,
            tag,
            unit,
            period_start,
            period_end,
            filed_date,
            frame,
            form_type,
            value_text
        )
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS sec_entity_sync_state (
        cik                                 TEXT PRIMARY KEY,
        last_submission_acceptance_datetime TEXT,
        last_submissions_fetch_utc          TEXT,
        last_companyfacts_fetch_utc         TEXT,
        last_filing_date_seen               TEXT,
        last_success_utc                    TEXT,
        last_error_utc                      TEXT,
        last_error_text                     TEXT,
        last_run_mode                       TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS sec_ingest_run_log (
        run_id                  TEXT PRIMARY KEY,
        mode                    TEXT NOT NULL,
        started_utc             TEXT NOT NULL,
        finished_utc            TEXT,
        status                  TEXT NOT NULL,
        cik_total               INTEGER NOT NULL DEFAULT 0,
        cik_processed           INTEGER NOT NULL DEFAULT 0,
        filing_rows_added       INTEGER NOT NULL DEFAULT 0,
        fact_rows_added         INTEGER NOT NULL DEFAULT 0,
        error_text              TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS sec_fundamental_period_t1 (
        period_sk                            INTEGER PRIMARY KEY AUTOINCREMENT,
        cik                                  TEXT NOT NULL,
        ticker                               TEXT,
        company_name                         TEXT,
        accession_number                     TEXT,
        form_type                            TEXT,
        report_period_end                    TEXT NOT NULL,
        fiscal_year                          INTEGER,
        fiscal_period                        TEXT,
        filing_date                          TEXT,
        acceptance_datetime                  TEXT,
        revenue                              REAL,
        cogs                                 REAL,
        gross_profit                         REAL,
        sga                                  REAL,
        r_and_d                              REAL,
        depreciation_and_amortization        REAL,
        operating_income                     REAL,
        interest_expense                     REAL,
        pretax_income                        REAL,
        tax_expense                          REAL,
        net_income                           REAL,
        ebitda                               REAL,
        eps_basic                            REAL,
        eps_diluted                          REAL,
        weighted_avg_shares_basic            REAL,
        weighted_avg_shares_diluted          REAL,
        stock_based_compensation             REAL,
        impairment_charges                   REAL,
        restructuring_charges                REAL,
        cash_and_equivalents                 REAL,
        short_term_investments               REAL,
        accounts_receivable                  REAL,
        inventory                            REAL,
        prepaid_other_current_assets         REAL,
        total_current_assets                 REAL,
        ppe_net                              REAL,
        goodwill                             REAL,
        intangibles                          REAL,
        total_assets                         REAL,
        accounts_payable                     REAL,
        accrued_liabilities                  REAL,
        contract_liabilities_current         REAL,
        contract_liabilities_noncurrent      REAL,
        short_term_borrowings                REAL,
        current_portion_long_term_debt       REAL,
        long_term_debt                       REAL,
        lease_liabilities                    REAL,
        total_liabilities                    REAL,
        total_equity                         REAL,
        shares_outstanding_period_end        REAL,
        public_float                         REAL,
        operating_cash_flow                  REAL,
        capex                                REAL,
        acquisitions                         REAL,
        cash_from_investing                  REAL,
        cash_from_financing                  REAL,
        dividends_paid                       REAL,
        share_repurchases                    REAL,
        share_issuance                       REAL,
        debt_issuance                        REAL,
        debt_repayment                       REAL,
        free_cash_flow                       REAL,
        taxes_payable                        REAL,
        taxes_receivable                     REAL,
        allowance_credit_losses              REAL,
        accruals_ratio                       REAL,
        gross_margin                         REAL,
        operating_margin                     REAL,
        cfo_to_net_income                    REAL,
        net_debt                             REAL,
        net_debt_to_assets                   REAL,
        sbc_to_revenue                       REAL,
        dilution_rate                        REAL,
        market_cap_proxy                     REAL,
        revenue_yoy_growth                   REAL,
        eps_yoy_growth                       REAL,
        revenue_acceleration                 REAL,
        earnings_acceleration                REAL,
        sue                                  REAL,
        earnings_release_8k_item202_30d      INTEGER,
        insider_buy_score_20bd               REAL,
        insider_sell_score_20bd              REAL,
        insider_net_score                    REAL,
        insider_data_present                 INTEGER,
        consensus_proxy_score                REAL,
        recommendation_proxy                 TEXT,
        data_quality_flags_json              TEXT,
        as_of_date                           TEXT NOT NULL,
        updated_at_utc                       TEXT NOT NULL,
        UNIQUE (cik, report_period_end, form_type, accession_number, as_of_date)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS sec_fundamental_period_metadata_t1 (
        period_sk                            INTEGER PRIMARY KEY AUTOINCREMENT,
        cik                                  TEXT NOT NULL,
        ticker                               TEXT,
        company_name                         TEXT,
        accession_number                     TEXT,
        form_type                            TEXT,
        report_period_end                    TEXT NOT NULL,
        fiscal_year                          INTEGER,
        fiscal_period                        TEXT,
        filing_date                          TEXT,
        acceptance_datetime                  TEXT,
        revenue                              REAL,
        cogs                                 REAL,
        gross_profit                         REAL,
        sga                                  REAL,
        r_and_d                              REAL,
        depreciation_and_amortization        REAL,
        operating_income                     REAL,
        interest_expense                     REAL,
        pretax_income                        REAL,
        tax_expense                          REAL,
        net_income                           REAL,
        ebitda                               REAL,
        eps_basic                            REAL,
        eps_diluted                          REAL,
        weighted_avg_shares_basic            REAL,
        weighted_avg_shares_diluted          REAL,
        stock_based_compensation             REAL,
        impairment_charges                   REAL,
        restructuring_charges                REAL,
        cash_and_equivalents                 REAL,
        short_term_investments               REAL,
        accounts_receivable                  REAL,
        inventory                            REAL,
        prepaid_other_current_assets         REAL,
        total_current_assets                 REAL,
        ppe_net                              REAL,
        goodwill                             REAL,
        intangibles                          REAL,
        total_assets                         REAL,
        accounts_payable                     REAL,
        accrued_liabilities                  REAL,
        contract_liabilities_current         REAL,
        contract_liabilities_noncurrent      REAL,
        short_term_borrowings                REAL,
        current_portion_long_term_debt       REAL,
        long_term_debt                       REAL,
        lease_liabilities                    REAL,
        total_liabilities                    REAL,
        total_equity                         REAL,
        shares_outstanding_period_end        REAL,
        public_float                         REAL,
        operating_cash_flow                  REAL,
        capex                                REAL,
        acquisitions                         REAL,
        cash_from_investing                  REAL,
        cash_from_financing                  REAL,
        dividends_paid                       REAL,
        share_repurchases                    REAL,
        share_issuance                       REAL,
        debt_issuance                        REAL,
        debt_repayment                       REAL,
        free_cash_flow                       REAL,
        taxes_payable                        REAL,
        taxes_receivable                     REAL,
        allowance_credit_losses              REAL,
        accruals_ratio                       REAL,
        gross_margin                         REAL,
        operating_margin                     REAL,
        cfo_to_net_income                    REAL,
        net_debt                             REAL,
        net_debt_to_assets                   REAL,
        sbc_to_revenue                       REAL,
        dilution_rate                        REAL,
        market_cap_proxy                     REAL,
        revenue_yoy_growth                   REAL,
        eps_yoy_growth                       REAL,
        revenue_acceleration                 REAL,
        earnings_acceleration                REAL,
        sue                                  REAL,
        earnings_release_8k_item202_30d      INTEGER,
        insider_buy_score_20bd               REAL,
        insider_sell_score_20bd              REAL,
        insider_net_score                    REAL,
        insider_data_present                 INTEGER,
        consensus_proxy_score                REAL,
        recommendation_proxy                 TEXT,
        data_quality_flags_json              TEXT,
        feature_status_json                  TEXT,
        feature_applicability_json           TEXT,
        effective_missing_feature_count      INTEGER,
        effective_any_feature_missing        INTEGER,
        core_nonnull_count                   INTEGER,
        metadata_marker_count                INTEGER,
        is_metadata_only                     INTEGER,
        is_scoring_eligible                  INTEGER,
        metadata_only_reason                 TEXT,
        as_of_date                           TEXT NOT NULL,
        updated_at_utc                       TEXT NOT NULL,
        UNIQUE (cik, report_period_end, form_type, accession_number, as_of_date)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS sec_cutover_tolerance_result (
        run_id                  TEXT NOT NULL,
        as_of_date              TEXT NOT NULL,
        metric_name             TEXT NOT NULL,
        metric_value            REAL,
        threshold_value         REAL,
        comparator              TEXT,
        pass_flag               INTEGER NOT NULL,
        details                 TEXT,
        created_at_utc          TEXT NOT NULL,
        PRIMARY KEY (run_id, metric_name)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_universe_ticker ON sec_entity_universe(ticker)",
    "CREATE INDEX IF NOT EXISTS idx_filing_cik_date ON sec_filing_index(cik, filing_date)",
    "CREATE INDEX IF NOT EXISTS idx_filing_form_date ON sec_filing_index(form_type, filing_date)",
    "CREATE INDEX IF NOT EXISTS idx_filing_acceptance ON sec_filing_index(cik, acceptance_datetime)",
    "CREATE INDEX IF NOT EXISTS idx_facts_lookup ON sec_xbrl_facts_raw(cik, taxonomy, tag, form_type, filed_date)",
    "CREATE INDEX IF NOT EXISTS idx_facts_cik_taxonomy_tag_date ON sec_xbrl_facts_raw(cik, taxonomy, tag, filed_date)",
    "CREATE INDEX IF NOT EXISTS idx_facts_period ON sec_xbrl_facts_raw(cik, report_period_end, accession_number)",
    "CREATE INDEX IF NOT EXISTS idx_facts_period_cik_int ON sec_xbrl_facts_raw(CAST(cik AS INTEGER), report_period_end, accession_number)",
    "CREATE INDEX IF NOT EXISTS idx_period_t1_key ON sec_fundamental_period_t1(cik, report_period_end, as_of_date)",
    "CREATE INDEX IF NOT EXISTS idx_period_t1_key_cik_int ON sec_fundamental_period_t1(CAST(cik AS INTEGER), report_period_end, as_of_date)",
    "CREATE INDEX IF NOT EXISTS idx_period_t1_ticker ON sec_fundamental_period_t1(ticker, as_of_date)",
    "CREATE INDEX IF NOT EXISTS idx_period_t1_ticker_accession_asof ON sec_fundamental_period_t1(ticker, accession_number, as_of_date)",
    "CREATE INDEX IF NOT EXISTS idx_period_metadata_t1_key ON sec_fundamental_period_metadata_t1(cik, report_period_end, as_of_date)",
    "CREATE INDEX IF NOT EXISTS idx_period_metadata_t1_ticker ON sec_fundamental_period_metadata_t1(ticker, as_of_date)",
    "CREATE INDEX IF NOT EXISTS idx_period_metadata_t1_ticker_accession_asof ON sec_fundamental_period_metadata_t1(ticker, accession_number, as_of_date)",
)


def init_db(conn: sqlite3.Connection) -> None:
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    # Legacy table removed in enhanced-only mode.
    conn.execute("DROP TABLE IF EXISTS sec_signal_proxy_snapshot_t1")
    for ddl in DDL_STATEMENTS:
        conn.execute(ddl)
    conn.commit()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Initialize sec_fundamentals.sqlite schema.")
    parser.add_argument("--config", type=Path, default=None, help="Path to fundamentals YAML config.")
    parser.add_argument("--db-path", type=Path, default=None, help="Override SQLite DB path.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    _, cfg = load_sec_fundamentals_config(args.config)
    db_path = Path(
        args.db_path if args.db_path is not None else cfg_get(cfg, "db_path", default=str(default_db_path()))
    ).expanduser().resolve()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    try:
        init_db(conn)
    finally:
        conn.close()
    print(f"Initialized fundamentals DB at: {db_path}")


if __name__ == "__main__":
    main()
