from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional


SCHEMA_SQL = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS runs (
    run_id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_type TEXT NOT NULL,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    status TEXT NOT NULL,
    input_path TEXT,
    row_count INTEGER,
    message TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS companies (
    company_id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL UNIQUE,
    cik TEXT,
    company_name TEXT NOT NULL,
    exchange TEXT,
    sector TEXT,
    industry TEXT,
    industry_aggregate TEXT,
    security_type TEXT,
    is_primary_listing TEXT,
    listing_status TEXT,
    country TEXT,
    currency TEXT,
    manual_include TEXT,
    manual_exclude TEXT,
    manual_review TEXT,
    notes TEXT,
    universe_status TEXT NOT NULL,
    is_active INTEGER NOT NULL DEFAULT 0,
    source_screen_decision TEXT,
    reason_codes TEXT,
    first_seen_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS company_aliases (
    alias_id INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id INTEGER NOT NULL,
    alias_raw TEXT NOT NULL,
    alias_norm TEXT NOT NULL,
    source TEXT NOT NULL,
    confidence REAL NOT NULL DEFAULT 1.0,
    is_manual INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (company_id) REFERENCES companies(company_id) ON DELETE CASCADE,
    UNIQUE(company_id, alias_norm, source)
);

CREATE TABLE IF NOT EXISTS company_universe_history (
    history_id INTEGER PRIMARY KEY AUTOINCREMENT,
    asof_date TEXT NOT NULL,
    company_id INTEGER NOT NULL,
    ticker TEXT NOT NULL,
    universe_status TEXT NOT NULL,
    reason_codes TEXT,
    source_file TEXT NOT NULL,
    run_id INTEGER,
    created_at TEXT NOT NULL,
    FOREIGN KEY (company_id) REFERENCES companies(company_id) ON DELETE CASCADE,
    FOREIGN KEY (run_id) REFERENCES runs(run_id) ON DELETE SET NULL,
    UNIQUE(asof_date, company_id, run_id)
);

CREATE TABLE IF NOT EXISTS manual_overrides (
    override_id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL,
    override_type TEXT NOT NULL,
    override_value TEXT NOT NULL,
    reason TEXT,
    is_active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS company_alias_review (
    review_id INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id INTEGER,
    candidate_alias TEXT NOT NULL,
    candidate_alias_norm TEXT NOT NULL,
    source TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    notes TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (company_id) REFERENCES companies(company_id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS trials (
    nct_id TEXT PRIMARY KEY,
    brief_title TEXT,
    study_type TEXT,
    phase_text TEXT,
    overall_status TEXT,
    lead_sponsor TEXT,
    last_update_post_date TEXT,
    has_results INTEGER NOT NULL DEFAULT 0,
    raw_json TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS trial_sponsors (
    sponsor_id INTEGER PRIMARY KEY AUTOINCREMENT,
    nct_id TEXT NOT NULL,
    sponsor_name TEXT NOT NULL,
    sponsor_name_norm TEXT NOT NULL,
    sponsor_role TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (nct_id) REFERENCES trials(nct_id) ON DELETE CASCADE,
    UNIQUE(nct_id, sponsor_name_norm, sponsor_role)
);

CREATE TABLE IF NOT EXISTS trial_company_links (
    nct_id TEXT NOT NULL,
    company_id INTEGER NOT NULL,
    match_role TEXT NOT NULL,
    match_method TEXT NOT NULL,
    confidence REAL NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (nct_id, company_id, match_role),
    FOREIGN KEY (nct_id) REFERENCES trials(nct_id) ON DELETE CASCADE,
    FOREIGN KEY (company_id) REFERENCES companies(company_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS ctgov_query_hits (
    company_id INTEGER NOT NULL,
    nct_id TEXT NOT NULL,
    search_term TEXT NOT NULL,
    query_field TEXT NOT NULL,
    source TEXT,
    confidence REAL NOT NULL DEFAULT 0.75,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (company_id, nct_id, search_term, query_field),
    FOREIGN KEY (company_id) REFERENCES companies(company_id) ON DELETE CASCADE,
    FOREIGN KEY (nct_id) REFERENCES trials(nct_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS trial_snapshot_daily (
    asof_date TEXT NOT NULL,
    nct_id TEXT NOT NULL,
    overall_status TEXT,
    phase_text TEXT,
    has_results INTEGER NOT NULL DEFAULT 0,
    primary_completion_date TEXT,
    enrollment_count INTEGER,
    raw_hash TEXT,
    created_at TEXT NOT NULL,
    PRIMARY KEY (asof_date, nct_id),
    FOREIGN KEY (nct_id) REFERENCES trials(nct_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS ctgov_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    asof_date TEXT NOT NULL,
    nct_id TEXT NOT NULL,
    company_id INTEGER NOT NULL,
    event_type TEXT NOT NULL,
    event_value TEXT,
    source_payload TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY (nct_id) REFERENCES trials(nct_id) ON DELETE CASCADE,
    FOREIGN KEY (company_id) REFERENCES companies(company_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS sec_filings (
    accession_nodash TEXT PRIMARY KEY,
    company_id INTEGER NOT NULL,
    form TEXT NOT NULL,
    filing_date TEXT NOT NULL,
    primary_document TEXT,
    archive_url TEXT,
    text_hash TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (company_id) REFERENCES companies(company_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS sec_filing_documents (
    document_id INTEGER PRIMARY KEY AUTOINCREMENT,
    accession_nodash TEXT NOT NULL,
    document_url TEXT NOT NULL,
    document_type TEXT NOT NULL,
    text_content TEXT,
    text_hash TEXT,
    text_length INTEGER NOT NULL DEFAULT 0,
    fetched_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (accession_nodash) REFERENCES sec_filings(accession_nodash) ON DELETE CASCADE,
    UNIQUE(accession_nodash, document_url)
);

CREATE TABLE IF NOT EXISTS sec_filing_latest_document (
    accession_nodash TEXT PRIMARY KEY,
    document_id INTEGER NOT NULL,
    document_url TEXT NOT NULL,
    document_type TEXT NOT NULL,
    text_hash TEXT NOT NULL,
    text_length INTEGER NOT NULL DEFAULT 0,
    fetched_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (accession_nodash) REFERENCES sec_filings(accession_nodash) ON DELETE CASCADE,
    FOREIGN KEY (document_id) REFERENCES sec_filing_documents(document_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS sec_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id INTEGER NOT NULL,
    accession_nodash TEXT NOT NULL,
    filing_date TEXT NOT NULL,
    form TEXT NOT NULL,
    event_type TEXT NOT NULL,
    event_date TEXT,
    event_value TEXT,
    polarity TEXT,
    confidence REAL NOT NULL DEFAULT 0.0,
    extracted_text TEXT,
    source_payload TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT,
    FOREIGN KEY (company_id) REFERENCES companies(company_id) ON DELETE CASCADE,
    FOREIGN KEY (accession_nodash) REFERENCES sec_filings(accession_nodash) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS sec_event_parse_state (
    accession_nodash TEXT PRIMARY KEY,
    text_hash TEXT,
    parser_signature TEXT,
    parsed_at TEXT NOT NULL,
    event_count INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (accession_nodash) REFERENCES sec_filings(accession_nodash) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS company_facts_daily (
    asof_date TEXT NOT NULL,
    company_id INTEGER NOT NULL,
    cash REAL,
    rd_expense REAL,
    op_cash_flow REAL,
    runway_months REAL,
    confidence TEXT,
    fact_json TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (asof_date, company_id),
    FOREIGN KEY (company_id) REFERENCES companies(company_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS financial_fact_observations (
    observation_id INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id INTEGER NOT NULL,
    cik TEXT,
    taxonomy TEXT,
    concept TEXT NOT NULL,
    label TEXT,
    unit TEXT,
    value REAL,
    period_start TEXT,
    period_end TEXT,
    fiscal_year INTEGER,
    fiscal_period TEXT,
    form TEXT,
    filed_date TEXT,
    accession_nodash TEXT,
    frame TEXT,
    source TEXT NOT NULL,
    confidence REAL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (company_id) REFERENCES companies(company_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS company_facts_quarterly (
    fact_id INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id INTEGER NOT NULL,
    period_end TEXT NOT NULL,
    fiscal_year INTEGER,
    fiscal_period TEXT,
    form TEXT,
    filed_date TEXT,
    accession_nodash TEXT,
    cash REAL,
    cash_and_equivalents REAL,
    short_term_investments REAL,
    cash_and_investments REAL,
    restricted_cash REAL,
    current_assets REAL,
    current_liabilities REAL,
    working_capital REAL,
    total_assets REAL,
    total_liabilities REAL,
    total_debt REAL,
    revenue REAL,
    rd_expense REAL,
    sgna_expense REAL,
    operating_income REAL,
    net_income REAL,
    operating_cash_flow REAL,
    investing_cash_flow REAL,
    financing_cash_flow REAL,
    capital_expenditures REAL,
    free_cash_flow REAL,
    shares_outstanding REAL,
    cash_source_concept TEXT,
    rd_source_concept TEXT,
    ocf_source_concept TEXT,
    shares_source_concept TEXT,
    missing_fields TEXT,
    proxy_fields_used TEXT,
    confidence TEXT,
    payload_json TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (company_id) REFERENCES companies(company_id) ON DELETE CASCADE,
    UNIQUE(company_id, period_end, fiscal_period, form)
);

CREATE TABLE IF NOT EXISTS company_facts_sync_state (
    company_id INTEGER PRIMARY KEY,
    latest_source_filing_date TEXT,
    payload_hash TEXT,
    last_synced_at TEXT NOT NULL,
    sync_status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (company_id) REFERENCES companies(company_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS forward_guidance_parse_state (
    accession_nodash TEXT PRIMARY KEY,
    text_hash TEXT,
    asof_year INTEGER,
    parsed_at TEXT NOT NULL,
    guidance_count INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (accession_nodash) REFERENCES sec_filings(accession_nodash) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS financial_survival_features (
    asof_date TEXT NOT NULL,
    company_id INTEGER NOT NULL,
    latest_period_end TEXT,
    cash_and_investments REAL,
    quarterly_cash_burn REAL,
    ttm_cash_burn REAL,
    operating_cash_flow_ttm REAL,
    rd_expense_ttm REAL,
    sgna_expense_ttm REAL,
    cash_runway_months REAL,
    working_capital REAL,
    working_capital_ratio REAL,
    debt_to_cash REAL,
    cash_qoq_change_pct REAL,
    cash_yoy_change_pct REAL,
    rd_qoq_change_pct REAL,
    rd_yoy_change_pct REAL,
    burn_acceleration_flag INTEGER,
    short_runway_flag INTEGER,
    severe_runway_flag INTEGER,
    atm_facility_active INTEGER,
    recent_offering_count_12m INTEGER,
    shelf_registration_active INTEGER,
    dilution_pressure_score REAL,
    going_concern_status TEXT,
    late_filing_count_12m INTEGER,
    financial_survival_score REAL,
    data_quality TEXT,
    missing_fields TEXT,
    proxy_fields_used TEXT,
    payload_json TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (asof_date, company_id),
    FOREIGN KEY (company_id) REFERENCES companies(company_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS data_quality_issues (
    issue_id INTEGER PRIMARY KEY AUTOINCREMENT,
    asof_date TEXT NOT NULL,
    company_id INTEGER,
    ticker TEXT,
    table_name TEXT NOT NULL,
    field_name TEXT,
    issue_type TEXT NOT NULL,
    severity TEXT NOT NULL,
    proxy_used TEXT,
    message TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY (company_id) REFERENCES companies(company_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS daily_features (
    asof_date TEXT NOT NULL,
    company_id INTEGER NOT NULL,
    catalyst_score_raw REAL,
    credibility_score_raw REAL,
    risk_score_raw REAL,
    feature_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (asof_date, company_id),
    FOREIGN KEY (company_id) REFERENCES companies(company_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS daily_scores (
    asof_date TEXT NOT NULL,
    company_id INTEGER NOT NULL,
    catalyst_score REAL,
    credibility_score REAL,
    financial_quality_score REAL,
    risk_score REAL,
    momentum_score REAL,
    opportunity_score REAL,
    rank INTEGER,
    bucket TEXT,
    top_evidence_json TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (asof_date, company_id),
    FOREIGN KEY (company_id) REFERENCES companies(company_id) ON DELETE CASCADE
);


CREATE TABLE IF NOT EXISTS market_snapshots_daily (
    asof_date TEXT NOT NULL,
    company_id INTEGER NOT NULL,
    ticker TEXT NOT NULL,
    source TEXT NOT NULL,
    last_price REAL,
    close_price REAL,
    market_cap REAL,
    shares_outstanding REAL,
    avg_volume_20d REAL,
    avg_dollar_volume_20d REAL,
    fifty_two_week_high REAL,
    fifty_two_week_low REAL,
    currency TEXT,
    price_adjustment TEXT,
    is_adjusted INTEGER,
    is_provisional INTEGER,
    first_bar_date TEXT,
    last_bar_date TEXT,
    bar_count INTEGER,
    expected_bar_count INTEGER,
    missing_bar_count INTEGER,
    continuity_status TEXT,
    data_quality TEXT,
    payload_json TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(asof_date, company_id, source),
    FOREIGN KEY (company_id) REFERENCES companies(company_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS market_bars_daily (
    ticker TEXT NOT NULL,
    bar_date TEXT NOT NULL,
    source TEXT NOT NULL,
    open REAL,
    high REAL,
    low REAL,
    close REAL,
    volume REAL,
    wap REAL,
    price_adjustment TEXT,
    raw_open REAL,
    raw_high REAL,
    raw_low REAL,
    raw_close REAL,
    adj_close REAL,
    adjustment_factor REAL,
    dividend_amount REAL,
    split_factor REAL,
    corporate_action_source TEXT,
    is_adjusted INTEGER NOT NULL DEFAULT 0,
    is_provisional INTEGER NOT NULL DEFAULT 0,
    data_quality TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT,
    PRIMARY KEY(ticker, bar_date, source)
);

CREATE TABLE IF NOT EXISTS market_features_daily (
    asof_date TEXT NOT NULL,
    company_id INTEGER NOT NULL,
    ticker TEXT NOT NULL,
    source TEXT NOT NULL,
    close_price REAL,
    market_cap REAL,
    shares_outstanding REAL,
    price_vs_200d_pct REAL,
    return_1m_pct REAL,
    return_3m_pct REAL,
    xbi_return_3m_pct REAL,
    relative_strength_3m_vs_xbi REAL,
    distance_from_52w_high_pct REAL,
    avg_dollar_volume_20d REAL,
    liquidity_score REAL,
    price_adjustment TEXT,
    is_adjusted INTEGER,
    is_provisional INTEGER,
    first_bar_date TEXT,
    last_bar_date TEXT,
    bar_count INTEGER,
    expected_bar_count INTEGER,
    missing_bar_count INTEGER,
    continuity_status TEXT,
    market_data_quality TEXT,
    payload_json TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(asof_date, company_id, source),
    FOREIGN KEY (company_id) REFERENCES companies(company_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS commercial_value_features_daily (
    asof_date TEXT NOT NULL,
    company_id INTEGER NOT NULL,
    ticker TEXT NOT NULL,
    company_name TEXT,
    latest_period_end TEXT,
    latest_quarter_revenue REAL,
    ttm_revenue REAL,
    revenue_qoq_growth_pct REAL,
    revenue_yoy_growth_pct REAL,
    gross_profit_ttm REAL,
    gross_margin_pct REAL,
    operating_income_ttm REAL,
    operating_margin_pct REAL,
    net_income_ttm REAL,
    net_margin_pct REAL,
    operating_cash_flow_ttm REAL,
    free_cash_flow_ttm REAL,
    rd_expense_ttm REAL,
    sgna_expense_ttm REAL,
    cash_and_investments REAL,
    total_debt REAL,
    net_cash REAL,
    shares_outstanding REAL,
    shares_yoy_growth_pct REAL,
    close_price REAL,
    market_cap REAL,
    enterprise_value REAL,
    price_to_sales REAL,
    ev_to_sales REAL,
    pe_ratio REAL,
    fcf_yield REAL,
    commercial_stage_flag INTEGER,
    profitable_flag INTEGER,
    revenue_scale_score REAL,
    revenue_growth_score REAL,
    margin_score REAL,
    profitability_score REAL,
    balance_sheet_score REAL,
    dilution_score REAL,
    valuation_score REAL,
    upside_capacity_score REAL,
    commercial_quality_score REAL,
    commercial_value_score REAL,
    data_quality TEXT,
    missing_fields TEXT,
    proxy_fields_used TEXT,
    payload_json TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(asof_date, company_id),
    FOREIGN KEY (company_id) REFERENCES companies(company_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS company_forward_guidance (
    guidance_id INTEGER PRIMARY KEY AUTOINCREMENT,
    asof_date TEXT NOT NULL,
    company_id INTEGER NOT NULL,
    ticker TEXT NOT NULL,
    company_name TEXT,
    accession_nodash TEXT NOT NULL,
    filing_date TEXT NOT NULL,
    form TEXT NOT NULL,
    metric TEXT NOT NULL,
    guidance_year INTEGER,
    period_label TEXT,
    low_value REAL,
    high_value REAL,
    midpoint_value REAL,
    unit TEXT,
    currency TEXT,
    confidence REAL NOT NULL DEFAULT 0.0,
    source_excerpt TEXT,
    source_payload TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (company_id) REFERENCES companies(company_id) ON DELETE CASCADE,
    FOREIGN KEY (accession_nodash) REFERENCES sec_filings(accession_nodash) ON DELETE CASCADE,
    UNIQUE(asof_date, company_id, accession_nodash, metric, guidance_year, low_value, high_value)
);

CREATE TABLE IF NOT EXISTS company_forward_guidance_overrides (
    override_id INTEGER PRIMARY KEY AUTOINCREMENT,
    asof_date TEXT NOT NULL,
    company_id INTEGER NOT NULL,
    ticker TEXT NOT NULL,
    company_name TEXT,
    unique_key TEXT NOT NULL,
    metric TEXT NOT NULL,
    guidance_year INTEGER,
    period_label TEXT,
    low_value REAL,
    high_value REAL,
    midpoint_value REAL,
    unit TEXT,
    currency TEXT,
    filing_date TEXT,
    form TEXT,
    confidence REAL NOT NULL DEFAULT 0.0,
    source_name TEXT,
    source_url TEXT,
    source_excerpt TEXT,
    override_reason TEXT,
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (company_id) REFERENCES companies(company_id) ON DELETE CASCADE,
    UNIQUE(unique_key)
);

CREATE TABLE IF NOT EXISTS forward_guidance_features_daily (
    asof_date TEXT NOT NULL,
    company_id INTEGER NOT NULL,
    ticker TEXT NOT NULL,
    company_name TEXT,
    latest_guidance_filing_date TEXT,
    forward_revenue_midpoint REAL,
    forward_revenue_low REAL,
    forward_revenue_high REAL,
    forward_revenue_year INTEGER,
    forward_revenue_growth_pct REAL,
    forward_ebitda_midpoint REAL,
    forward_ebitda_margin_pct REAL,
    forward_eps_midpoint REAL,
    guidance_confidence REAL,
    guidance_recency_days INTEGER,
    forward_profitability_flag INTEGER,
    guidance_score REAL,
    forward_growth_score REAL,
    forward_profitability_score REAL,
    forward_valuation_score REAL,
    data_quality TEXT,
    missing_fields TEXT,
    payload_json TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(asof_date, company_id),
    FOREIGN KEY (company_id) REFERENCES companies(company_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS governance_event_features_daily (
    asof_date TEXT NOT NULL,
    company_id INTEGER NOT NULL,
    ticker TEXT NOT NULL,
    company_name TEXT,
    form4_source_db TEXT,
    form4_snapshot_date TEXT,
    insider_buy_count_90d INTEGER,
    insider_buy_value_90d REAL,
    insider_buy_cluster_count_90d INTEGER,
    ceo_cfo_buy_count_180d INTEGER,
    director_buy_count_180d INTEGER,
    insider_sell_value_90d REAL,
    sell_to_buy_value_ratio_180d REAL,
    planned_10b5_1_buy_count INTEGER,
    activist_13d_count_365d INTEGER,
    buyback_event_count_365d INTEGER,
    asr_event_count_365d INTEGER,
    leadership_change_count_365d INTEGER,
    cfo_departure_flag_365d INTEGER,
    regulatory_setback_count_365d INTEGER,
    adverse_legal_event_count_365d INTEGER,
    generic_competition_risk_count_365d INTEGER,
    product_concentration_risk_count_365d INTEGER,
    commercial_fragility_risk_score REAL,
    governance_event_score REAL,
    governance_risk_score REAL,
    data_quality TEXT,
    missing_fields TEXT,
    payload_json TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(asof_date, company_id),
    FOREIGN KEY (company_id) REFERENCES companies(company_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS multibagger_features_daily (
    asof_date TEXT NOT NULL,
    company_id INTEGER NOT NULL,
    ticker TEXT NOT NULL,
    company_name TEXT,
    commercial_acceleration_score REAL,
    upside_capacity_score REAL,
    cash_flow_acceleration_score REAL,
    survival_quality_score REAL,
    governance_event_score REAL,
    market_confirmation_score REAL,
    catalyst_quality_score REAL,
    commercial_fragility_risk_score REAL,
    multibagger_risk_penalty REAL,
    evidence_or_catalyst_flag INTEGER,
    data_quality TEXT,
    missing_fields TEXT,
    proxy_fields_used TEXT,
    payload_json TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(asof_date, company_id),
    FOREIGN KEY (company_id) REFERENCES companies(company_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS multibagger_scores_daily (
    asof_date TEXT NOT NULL,
    company_id INTEGER NOT NULL,
    ticker TEXT NOT NULL,
    company_name TEXT,
    multibagger_score REAL,
    rank INTEGER,
    bucket TEXT,
    top_evidence_json TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(asof_date, company_id),
    FOREIGN KEY (company_id) REFERENCES companies(company_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_company_aliases_norm ON company_aliases(alias_norm);
CREATE INDEX IF NOT EXISTS idx_companies_cik ON companies(cik);
CREATE INDEX IF NOT EXISTS idx_companies_status ON companies(universe_status, is_active);
CREATE INDEX IF NOT EXISTS idx_trial_company_links_company ON trial_company_links(company_id, nct_id);
CREATE INDEX IF NOT EXISTS idx_ctgov_query_hits_company ON ctgov_query_hits(company_id, nct_id);
CREATE INDEX IF NOT EXISTS idx_sec_filings_company_date ON sec_filings(company_id, filing_date);
CREATE INDEX IF NOT EXISTS idx_ctgov_events_asof_company ON ctgov_events(asof_date, company_id);
CREATE INDEX IF NOT EXISTS idx_sec_events_company_date ON sec_events(company_id, filing_date);
CREATE INDEX IF NOT EXISTS idx_sec_events_type_date ON sec_events(event_type, filing_date);
CREATE INDEX IF NOT EXISTS idx_sec_event_parse_state_hash ON sec_event_parse_state(text_hash);
CREATE INDEX IF NOT EXISTS idx_financial_observations_company_concept ON financial_fact_observations(company_id, concept, period_end);
CREATE INDEX IF NOT EXISTS idx_company_facts_quarterly_company_period ON company_facts_quarterly(company_id, period_end);
CREATE INDEX IF NOT EXISTS idx_company_facts_sync_state_status ON company_facts_sync_state(sync_status, last_synced_at);
CREATE INDEX IF NOT EXISTS idx_financial_survival_asof_company ON financial_survival_features(asof_date, company_id);
CREATE INDEX IF NOT EXISTS idx_data_quality_issues_asof_company ON data_quality_issues(asof_date, company_id);
CREATE INDEX IF NOT EXISTS idx_market_bars_ticker_date ON market_bars_daily(ticker, bar_date);
CREATE INDEX IF NOT EXISTS idx_market_bars_source_ticker_date ON market_bars_daily(source, ticker, bar_date);
CREATE INDEX IF NOT EXISTS idx_market_features_asof_company ON market_features_daily(asof_date, company_id);
CREATE INDEX IF NOT EXISTS idx_commercial_value_asof_company ON commercial_value_features_daily(asof_date, company_id);
CREATE INDEX IF NOT EXISTS idx_forward_guidance_company_date ON company_forward_guidance(company_id, filing_date);
CREATE INDEX IF NOT EXISTS idx_forward_guidance_overrides_asof_company ON company_forward_guidance_overrides(asof_date, company_id);
CREATE INDEX IF NOT EXISTS idx_forward_guidance_features_asof_company ON forward_guidance_features_daily(asof_date, company_id);
CREATE INDEX IF NOT EXISTS idx_forward_guidance_parse_state_hash ON forward_guidance_parse_state(text_hash);
CREATE INDEX IF NOT EXISTS idx_sec_filing_documents_accession_type ON sec_filing_documents(accession_nodash, document_type, fetched_at);
CREATE INDEX IF NOT EXISTS idx_sec_filing_latest_document_hash ON sec_filing_latest_document(text_hash);
CREATE INDEX IF NOT EXISTS idx_sec_filing_latest_document_type ON sec_filing_latest_document(document_type, fetched_at);
CREATE INDEX IF NOT EXISTS idx_sec_filings_form_date_company ON sec_filings(form, filing_date, company_id);
CREATE INDEX IF NOT EXISTS idx_sec_filings_date_company ON sec_filings(filing_date, company_id);
CREATE INDEX IF NOT EXISTS idx_governance_features_asof_company ON governance_event_features_daily(asof_date, company_id);
CREATE INDEX IF NOT EXISTS idx_multibagger_features_asof_company ON multibagger_features_daily(asof_date, company_id);
CREATE INDEX IF NOT EXISTS idx_multibagger_scores_asof_rank ON multibagger_scores_daily(asof_date, rank);
CREATE INDEX IF NOT EXISTS idx_daily_scores_asof_rank ON daily_scores(asof_date, rank);
"""

COMPANY_OPTIONAL_COLUMNS = {
    "security_type": "TEXT",
    "is_primary_listing": "TEXT",
    "listing_status": "TEXT",
    "country": "TEXT",
    "currency": "TEXT",
    "manual_include": "TEXT",
    "manual_exclude": "TEXT",
    "manual_review": "TEXT",
    "notes": "TEXT",
}

SEC_EVENT_OPTIONAL_COLUMNS = {
    "event_date": "TEXT",
    "event_value": "TEXT",
    "source_payload": "TEXT",
    "updated_at": "TEXT",
}

SEC_FILING_DOCUMENT_OPTIONAL_COLUMNS = {
    "text_length": "INTEGER NOT NULL DEFAULT 0",
}

SEC_EVENT_PARSE_STATE_OPTIONAL_COLUMNS = {
    "parser_signature": "TEXT",
}


FORWARD_GUIDANCE_PARSE_STATE_OPTIONAL_COLUMNS = {
    "parser_signature": "TEXT",
}


COMPANY_FACTS_QUARTERLY_OPTIONAL_COLUMNS = {
    "cost_of_revenue": "REAL",
    "gross_profit": "REAL",
    "interest_expense": "REAL",
    "income_tax_expense": "REAL",
    "depreciation_amortization": "REAL",
    "eps_basic": "REAL",
    "eps_diluted": "REAL",
    "weighted_average_shares_basic": "REAL",
    "weighted_average_shares_diluted": "REAL",
    "revenue_source_concept": "TEXT",
    "gross_profit_source_concept": "TEXT",
    "cost_of_revenue_source_concept": "TEXT",
    "net_income_source_concept": "TEXT",
}

DAILY_SCORES_OPTIONAL_COLUMNS = {
    "clinical_opportunity_score": "REAL",
    "commercial_value_score": "REAL",
    "forward_guidance_score": "REAL",
    "valuation_score": "REAL",
    "upside_capacity_score": "REAL",
    "investment_score": "REAL",
}

MARKET_BARS_OPTIONAL_COLUMNS = {
    "price_adjustment": "TEXT",
    "raw_open": "REAL",
    "raw_high": "REAL",
    "raw_low": "REAL",
    "raw_close": "REAL",
    "adj_close": "REAL",
    "adjustment_factor": "REAL",
    "dividend_amount": "REAL",
    "split_factor": "REAL",
    "corporate_action_source": "TEXT",
    "is_adjusted": "INTEGER NOT NULL DEFAULT 0",
    "is_provisional": "INTEGER NOT NULL DEFAULT 0",
    "updated_at": "TEXT",
}

MARKET_DAILY_OPTIONAL_COLUMNS = {
    "price_adjustment": "TEXT",
    "is_adjusted": "INTEGER",
    "is_provisional": "INTEGER",
    "first_bar_date": "TEXT",
    "last_bar_date": "TEXT",
    "bar_count": "INTEGER",
    "expected_bar_count": "INTEGER",
    "missing_bar_count": "INTEGER",
    "continuity_status": "TEXT",
}

FORWARD_GUIDANCE_OVERRIDES_OPTIONAL_COLUMNS = {
    "unique_key": "TEXT",
}

GOVERNANCE_EVENT_OPTIONAL_COLUMNS = {
    "regulatory_setback_count_365d": "INTEGER",
    "adverse_legal_event_count_365d": "INTEGER",
    "generic_competition_risk_count_365d": "INTEGER",
    "product_concentration_risk_count_365d": "INTEGER",
    "commercial_fragility_risk_score": "REAL",
}

MULTIBAGGER_FEATURE_OPTIONAL_COLUMNS = {
    "commercial_fragility_risk_score": "REAL",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


class ManagedConnection:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def __enter__(self) -> sqlite3.Connection:
        return self._conn

    def __exit__(self, exc_type, exc_value, traceback):
        try:
            return self._conn.__exit__(exc_type, exc_value, traceback)
        finally:
            self._conn.close()

    def __getattr__(self, name: str):
        return getattr(self._conn, name)


def connect(db_path: Path, *, timeout_sec: float = 30.0) -> ManagedConnection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=timeout_sec)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    return ManagedConnection(conn)


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_SQL)
    ensure_company_optional_columns(conn)
    ensure_table_optional_columns(conn, "sec_filing_documents", SEC_FILING_DOCUMENT_OPTIONAL_COLUMNS)
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_sec_filing_documents_latest_meta
        ON sec_filing_documents(accession_nodash, document_type, fetched_at, document_id, text_hash, text_length)
        """
    )
    ensure_table_optional_columns(conn, "sec_events", SEC_EVENT_OPTIONAL_COLUMNS)
    ensure_table_optional_columns(conn, "company_facts_quarterly", COMPANY_FACTS_QUARTERLY_OPTIONAL_COLUMNS)
    ensure_table_optional_columns(conn, "daily_scores", DAILY_SCORES_OPTIONAL_COLUMNS)
    ensure_table_optional_columns(conn, "market_bars_daily", MARKET_BARS_OPTIONAL_COLUMNS)
    ensure_table_optional_columns(conn, "market_snapshots_daily", MARKET_DAILY_OPTIONAL_COLUMNS)
    ensure_table_optional_columns(conn, "market_features_daily", MARKET_DAILY_OPTIONAL_COLUMNS)
    ensure_table_optional_columns(conn, "company_forward_guidance_overrides", FORWARD_GUIDANCE_OVERRIDES_OPTIONAL_COLUMNS)
    ensure_table_optional_columns(conn, "governance_event_features_daily", GOVERNANCE_EVENT_OPTIONAL_COLUMNS)
    ensure_table_optional_columns(conn, "multibagger_features_daily", MULTIBAGGER_FEATURE_OPTIONAL_COLUMNS)
    ensure_state_tables_created_at(conn)
    ensure_table_optional_columns(conn, "sec_event_parse_state", SEC_EVENT_PARSE_STATE_OPTIONAL_COLUMNS)
    ensure_table_optional_columns(conn, "forward_guidance_parse_state", FORWARD_GUIDANCE_PARSE_STATE_OPTIONAL_COLUMNS)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_sec_event_parse_state_signature ON sec_event_parse_state(parser_signature)")
    conn.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_forward_guidance_overrides_unique_key
        ON company_forward_guidance_overrides(unique_key)
        """
    )
    conn.commit()


def ensure_company_optional_columns(conn: sqlite3.Connection) -> None:
    ensure_table_optional_columns(conn, "companies", COMPANY_OPTIONAL_COLUMNS)


def _table_column_names(conn: sqlite3.Connection, table_name: str) -> set[str]:
    columns: set[str] = set()
    for row in conn.execute(f"PRAGMA table_info({table_name})").fetchall():
        try:
            columns.add(str(row["name"]))
        except (TypeError, IndexError):
            columns.add(str(row[1]))
    return columns


def ensure_table_optional_columns(conn: sqlite3.Connection, table_name: str, columns: dict[str, str]) -> None:
    existing = _table_column_names(conn, table_name)
    for column, column_type in columns.items():
        if column not in existing:
            conn.execute(f"ALTER TABLE {table_name} ADD COLUMN {column} {column_type}")


def refresh_sec_latest_documents(conn: sqlite3.Connection, accessions: Iterable[str]) -> int:
    """Refresh latest SEC document metadata without reading large text_content blobs."""
    now = utc_now()
    refreshed = 0
    for accession in sorted({str(value or "").strip() for value in accessions if str(value or "").strip()}):
        row = conn.execute(
            """
            SELECT
                document_id,
                accession_nodash,
                document_url,
                document_type,
                text_hash,
                COALESCE(text_length, 0) AS text_length,
                fetched_at
            FROM sec_filing_documents
            WHERE accession_nodash = ?
              AND COALESCE(text_hash, '') <> ''
            ORDER BY
                CASE WHEN document_type = 'complete_submission_text' THEN 0 ELSE 1 END,
                COALESCE(fetched_at, updated_at, created_at) DESC,
                document_id DESC
            LIMIT 1
            """,
            (accession,),
        ).fetchone()
        if row is None:
            conn.execute("DELETE FROM sec_filing_latest_document WHERE accession_nodash = ?", (accession,))
            continue
        conn.execute(
            """
            INSERT INTO sec_filing_latest_document(
                accession_nodash, document_id, document_url, document_type, text_hash,
                text_length, fetched_at, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(accession_nodash) DO UPDATE SET
                document_id = excluded.document_id,
                document_url = excluded.document_url,
                document_type = excluded.document_type,
                text_hash = excluded.text_hash,
                text_length = excluded.text_length,
                fetched_at = excluded.fetched_at,
                updated_at = excluded.updated_at
            """,
            (
                str(row["accession_nodash"]),
                int(row["document_id"]),
                str(row["document_url"] or ""),
                str(row["document_type"] or ""),
                str(row["text_hash"] or ""),
                int(row["text_length"] or 0),
                str(row["fetched_at"] or ""),
                now,
                now,
            ),
        )
        refreshed += 1
    return refreshed


def ensure_state_tables_created_at(conn: sqlite3.Connection) -> None:
    sec_event_cols = _table_column_names(conn, "sec_event_parse_state")
    if "created_at" not in sec_event_cols:
        now = utc_now()
        parser_signature_expr = "parser_signature" if "parser_signature" in sec_event_cols else "'' AS parser_signature"
        conn.execute("DROP INDEX IF EXISTS idx_sec_event_parse_state_hash")
        conn.execute("ALTER TABLE sec_event_parse_state RENAME TO sec_event_parse_state_legacy")
        conn.execute(
            """
            CREATE TABLE sec_event_parse_state (
                accession_nodash TEXT PRIMARY KEY,
                text_hash TEXT,
                parser_signature TEXT,
                parsed_at TEXT NOT NULL,
                event_count INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (accession_nodash) REFERENCES sec_filings(accession_nodash) ON DELETE CASCADE
            )
            """
        )
        conn.execute(
            """
            INSERT INTO sec_event_parse_state(accession_nodash, text_hash, parser_signature, parsed_at, event_count, created_at, updated_at)
            SELECT accession_nodash, text_hash, {parser_signature_expr}, parsed_at, event_count,
                   COALESCE(updated_at, parsed_at, ?) AS created_at,
                   COALESCE(updated_at, parsed_at, ?) AS updated_at
            FROM sec_event_parse_state_legacy
            """.format(parser_signature_expr=parser_signature_expr),
            (now, now),
        )
        conn.execute("DROP TABLE sec_event_parse_state_legacy")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_sec_event_parse_state_hash ON sec_event_parse_state(text_hash)")

    facts_sync_cols = _table_column_names(conn, "company_facts_sync_state")
    if "created_at" not in facts_sync_cols:
        now = utc_now()
        conn.execute("DROP INDEX IF EXISTS idx_company_facts_sync_state_status")
        conn.execute("ALTER TABLE company_facts_sync_state RENAME TO company_facts_sync_state_legacy")
        conn.execute(
            """
            CREATE TABLE company_facts_sync_state (
                company_id INTEGER PRIMARY KEY,
                latest_source_filing_date TEXT,
                payload_hash TEXT,
                last_synced_at TEXT NOT NULL,
                sync_status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (company_id) REFERENCES companies(company_id) ON DELETE CASCADE
            )
            """
        )
        conn.execute(
            """
            INSERT INTO company_facts_sync_state(
                company_id, latest_source_filing_date, payload_hash, last_synced_at, sync_status, created_at, updated_at
            )
            SELECT company_id, latest_source_filing_date, payload_hash,
                   last_synced_at, sync_status,
                   COALESCE(updated_at, last_synced_at, ?) AS created_at,
                   COALESCE(updated_at, last_synced_at, ?) AS updated_at
            FROM company_facts_sync_state_legacy
            """,
            (now, now),
        )
        conn.execute("DROP TABLE company_facts_sync_state_legacy")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_company_facts_sync_state_status ON company_facts_sync_state(sync_status, last_synced_at)"
        )


def start_run(conn: sqlite3.Connection, *, run_type: str, input_path: Optional[Path]) -> int:
    now = utc_now()
    cur = conn.execute(
        """
        INSERT INTO runs(run_type, started_at, status, input_path, created_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (run_type, now, "running", str(input_path) if input_path else None, now),
    )
    conn.commit()
    return int(cur.lastrowid)


def finish_run(conn: sqlite3.Connection, *, run_id: int, status: str, row_count: int, message: str = "") -> None:
    conn.execute(
        """
        UPDATE runs
        SET completed_at = ?, status = ?, row_count = ?, message = ?
        WHERE run_id = ?
        """,
        (utc_now(), status, row_count, message, run_id),
    )
    conn.commit()
