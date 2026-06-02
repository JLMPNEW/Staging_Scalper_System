from __future__ import annotations

import hashlib
import json
import logging
import math
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional


LOGGER = logging.getLogger(__name__)

SCHEMA_SQL = """
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

CREATE TABLE IF NOT EXISTS sec_governance_signal_cache (
    accession_nodash TEXT NOT NULL,
    text_hash TEXT NOT NULL,
    parser_signature TEXT NOT NULL,
    buyback_flag INTEGER NOT NULL DEFAULT 0,
    asr_flag INTEGER NOT NULL DEFAULT 0,
    leadership_flag INTEGER NOT NULL DEFAULT 0,
    cfo_departure_flag INTEGER NOT NULL DEFAULT 0,
    regulatory_setback_flag INTEGER NOT NULL DEFAULT 0,
    adverse_legal_flag INTEGER NOT NULL DEFAULT 0,
    generic_competition_flag INTEGER NOT NULL DEFAULT 0,
    product_concentration_flag INTEGER NOT NULL DEFAULT 0,
    matches TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(accession_nodash, text_hash, parser_signature),
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
    negative_cash_flag INTEGER,
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
    financial_quality_score_raw REAL,
    risk_score_raw REAL,
    momentum_score_raw REAL,
    feature_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (asof_date, company_id),
    FOREIGN KEY (company_id) REFERENCES companies(company_id) ON DELETE CASCADE
);

-- daily_scores starts with stable core columns; extended scoring/reporting fields are migrated
-- idempotently from DAILY_SCORES_OPTIONAL_COLUMNS in init_db().
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
    avg_dollar_volume_20d REAL,
    return_3m_pct REAL,
    price_vs_200d_pct REAL,
    distance_from_52w_high_pct REAL,
    relative_strength_3m_vs_xbi REAL,
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
    leverage_score REAL,
    dilution_score REAL,
    valuation_score REAL,
    quality_adjusted_valuation_score REAL,
    upside_capacity_score REAL,
    institutional_upside_capacity_score REAL,
    value_trap_score REAL,
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
    guidance_unique_key TEXT NOT NULL,
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
    quality_forward_valuation_score REAL,
    quality_adjusted_guidance_score REAL,
    guidance_recency_penalty REAL,
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
CREATE INDEX IF NOT EXISTS idx_company_aliases_company_id ON company_aliases(company_id);
CREATE INDEX IF NOT EXISTS idx_companies_cik ON companies(cik);
CREATE INDEX IF NOT EXISTS idx_companies_status ON companies(universe_status, is_active);
CREATE INDEX IF NOT EXISTS idx_trial_sponsors_nct_id ON trial_sponsors(nct_id);
CREATE INDEX IF NOT EXISTS idx_trial_company_links_company ON trial_company_links(company_id, nct_id);
CREATE INDEX IF NOT EXISTS idx_trial_company_links_nct ON trial_company_links(nct_id);
CREATE INDEX IF NOT EXISTS idx_ctgov_query_hits_company ON ctgov_query_hits(company_id, nct_id);
CREATE INDEX IF NOT EXISTS idx_sec_filings_company_date ON sec_filings(company_id, filing_date);
CREATE INDEX IF NOT EXISTS idx_sec_filings_company_date_form ON sec_filings(company_id, filing_date, form);
CREATE INDEX IF NOT EXISTS idx_ctgov_events_asof_company ON ctgov_events(asof_date, company_id);
CREATE INDEX IF NOT EXISTS idx_sec_events_company_date ON sec_events(company_id, filing_date);
CREATE INDEX IF NOT EXISTS idx_sec_events_type_date ON sec_events(event_type, filing_date);
CREATE INDEX IF NOT EXISTS idx_sec_event_parse_state_hash ON sec_event_parse_state(text_hash);
CREATE INDEX IF NOT EXISTS idx_sec_event_parse_state_accession ON sec_event_parse_state(accession_nodash);
CREATE INDEX IF NOT EXISTS idx_financial_observations_company_concept ON financial_fact_observations(company_id, concept, period_end);
CREATE INDEX IF NOT EXISTS idx_company_facts_quarterly_company_period ON company_facts_quarterly(company_id, period_end);
CREATE INDEX IF NOT EXISTS idx_company_facts_quarterly_company_period_desc ON company_facts_quarterly(company_id, period_end DESC);
CREATE INDEX IF NOT EXISTS idx_company_facts_sync_state_status ON company_facts_sync_state(sync_status, last_synced_at);
CREATE INDEX IF NOT EXISTS idx_financial_survival_asof_company ON financial_survival_features(asof_date, company_id);
CREATE INDEX IF NOT EXISTS idx_financial_survival_company_asof ON financial_survival_features(company_id, asof_date DESC);
CREATE INDEX IF NOT EXISTS idx_data_quality_issues_asof_company ON data_quality_issues(asof_date, company_id);
CREATE INDEX IF NOT EXISTS idx_market_bars_ticker_date ON market_bars_daily(ticker, bar_date);
CREATE INDEX IF NOT EXISTS idx_market_bars_source_ticker_date ON market_bars_daily(source, ticker, bar_date);
CREATE INDEX IF NOT EXISTS idx_market_features_asof_company ON market_features_daily(asof_date, company_id);
CREATE INDEX IF NOT EXISTS idx_market_features_company_asof ON market_features_daily(company_id, asof_date DESC, source);
CREATE INDEX IF NOT EXISTS idx_commercial_value_asof_company ON commercial_value_features_daily(asof_date, company_id);
CREATE INDEX IF NOT EXISTS idx_commercial_value_company_asof ON commercial_value_features_daily(company_id, asof_date DESC);
CREATE INDEX IF NOT EXISTS idx_forward_guidance_company_date ON company_forward_guidance(company_id, filing_date);
CREATE INDEX IF NOT EXISTS idx_forward_guidance_accession_asof ON company_forward_guidance(accession_nodash, asof_date);
CREATE INDEX IF NOT EXISTS idx_forward_guidance_overrides_asof_company ON company_forward_guidance_overrides(asof_date, company_id);
CREATE INDEX IF NOT EXISTS idx_forward_guidance_features_asof_company ON forward_guidance_features_daily(asof_date, company_id);
CREATE INDEX IF NOT EXISTS idx_forward_guidance_parse_state_hash ON forward_guidance_parse_state(text_hash);
CREATE INDEX IF NOT EXISTS idx_sec_governance_signal_cache_signature ON sec_governance_signal_cache(parser_signature, accession_nodash, text_hash);
CREATE INDEX IF NOT EXISTS idx_sec_filing_documents_accession ON sec_filing_documents(accession_nodash);
CREATE INDEX IF NOT EXISTS idx_sec_filing_documents_accession_type ON sec_filing_documents(accession_nodash, document_type, fetched_at);
CREATE INDEX IF NOT EXISTS idx_sec_filing_latest_document_hash ON sec_filing_latest_document(text_hash);
CREATE INDEX IF NOT EXISTS idx_sec_filing_latest_document_type ON sec_filing_latest_document(document_type, fetched_at);
CREATE INDEX IF NOT EXISTS idx_sec_filings_form_date_company ON sec_filings(form, filing_date, company_id);
CREATE INDEX IF NOT EXISTS idx_sec_filings_date_company ON sec_filings(filing_date, company_id);
CREATE INDEX IF NOT EXISTS idx_governance_features_asof_company ON governance_event_features_daily(asof_date, company_id);
CREATE INDEX IF NOT EXISTS idx_governance_features_company_asof ON governance_event_features_daily(company_id, asof_date DESC);
CREATE INDEX IF NOT EXISTS idx_daily_features_company_asof ON daily_features(company_id, asof_date DESC);
CREATE INDEX IF NOT EXISTS idx_daily_scores_company_asof ON daily_scores(company_id, asof_date DESC);
CREATE INDEX IF NOT EXISTS idx_daily_scores_asof_bucket ON daily_scores(asof_date, bucket);
CREATE INDEX IF NOT EXISTS idx_multibagger_features_asof_company ON multibagger_features_daily(asof_date, company_id);
CREATE INDEX IF NOT EXISTS idx_multibagger_features_company_asof ON multibagger_features_daily(company_id, asof_date DESC);
CREATE INDEX IF NOT EXISTS idx_multibagger_scores_company_asof ON multibagger_scores_daily(company_id, asof_date DESC);
CREATE INDEX IF NOT EXISTS idx_multibagger_scores_asof_rank ON multibagger_scores_daily(asof_date, rank);
CREATE INDEX IF NOT EXISTS idx_daily_scores_asof_rank ON daily_scores(asof_date, rank);
"""

SAFE_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
SAFE_COLUMN_TYPE_RE = re.compile(r"^[A-Z][A-Z0-9_]*(?:\s+[A-Z0-9_()'.+-]+)*$")


def quote_identifier(identifier: str) -> str:
    """Quote a trusted SQLite identifier after a strict identifier safety check."""
    text = str(identifier or "").strip()
    if not SAFE_IDENTIFIER_RE.fullmatch(text):
        raise ValueError(f"Unsafe SQLite identifier: {identifier!r}")
    return f'"{text}"'


COMPANY_OPTIONAL_COLUMNS = {
    "source_screen_decision": "TEXT",
    "reason_codes": "TEXT",
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

FINANCIAL_SURVIVAL_OPTIONAL_COLUMNS = {
    "negative_cash_flag": "INTEGER",
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
    "ticker": "TEXT",
    "company_name": "TEXT",
    "clinical_opportunity_score": "REAL",
    "commercial_quality_score": "REAL",
    "commercial_value_score": "REAL",
    "forward_guidance_score": "REAL",
    "valuation_score": "REAL",
    "upside_capacity_score": "REAL",
    "institutional_upside_capacity_score": "REAL",
    "leverage_score": "REAL",
    "value_trap_score": "REAL",
    "leverage_fragility_score": "REAL",
    "mature_defensive_score": "REAL",
    "expected_return_quality_score": "REAL",
    "commercial_entry_quality_score": "REAL",
    "commercial_overextension_score": "REAL",
    "valuation_growth_fit_score": "REAL",
    "commercial_expected_return_overlay_score": "REAL",
    "quality_adjusted_valuation_score": "REAL",
    "used_quality_adjusted_valuation": "REAL",
    "valuation_quality_adjustment_delta": "REAL",
    "quality_forward_valuation_score": "REAL",
    "quality_adjusted_guidance_score": "REAL",
    "used_quality_adjusted_guidance": "REAL",
    "guidance_quality_adjustment_delta": "REAL",
    "guidance_recency_penalty": "REAL",
    "investment_score": "REAL",
    "tier1_selection_gate_score": "REAL",
    "tier1_primary_horizon_trading_days": "INTEGER",
    "tier1_production_score_model": "TEXT",
    "tier1_selection_policy": "TEXT",
    "alpha_multibagger_role": "TEXT",
    "core_structural_veto_flag": "REAL",
    "core_structural_veto_reasons": "TEXT",
    "rank_demoted_by_core_veto": "REAL",
    "data_quality_confidence_multiplier": "REAL",
    "clinical_risk_drag": "REAL",
    "investment_risk_drag": "REAL",
    "legacy_risk_score": "REAL",
    "risk_penalty_input_score": "REAL",
    "predictive_risk_penalty_input_score": "REAL",
    "uncompensated_risk_score": "REAL",
    "compensated_risk_score": "REAL",
    "liquidity_risk_score": "REAL",
    "financing_survival_risk_score": "REAL",
    "governance_filing_risk_score": "REAL",
    "regulatory_setback_risk_score": "REAL",
    "pipeline_anchor_risk_score": "REAL",
    "collaborator_dependency_risk_score": "REAL",
    "trial_staleness_risk_score": "REAL",
    "risk_component_json": "TEXT",
    "allocation_risk_score": "REAL",
    "allocation_risk_penalty_mode": "TEXT",
    "discovery_risk_score": "REAL",
    "discovery_risk_penalty_mode": "TEXT",
    "discovery_clinical_opportunity_score": "REAL",
    "discovery_investment_score": "REAL",
    "discovery_opportunity_score": "REAL",
    "allocation_opportunity_score": "REAL",
    "allocation_bucket": "TEXT",
    "production_rank_score": "REAL",
    "production_rank_risk_score": "REAL",
    "production_rank_score_field": "TEXT",
    "production_score_source": "TEXT",
    "discovery_selection_gate_score": "REAL",
    "discovery_clinical_risk_drag": "REAL",
    "discovery_investment_risk_drag": "REAL",
    "discovery_policy_event_hard_penalty": "REAL",
    "discovery_policy_soft_weakness_penalty": "REAL",
    "discovery_policy_quality_penalty": "REAL",
    "discovery_policy_quality_bonus": "REAL",
    "discovery_rank_quality_cap": "REAL",
    "discovery_rank_quality_cap_reasons": "TEXT",
    "discovery_rank_quality_cap_vetoed": "REAL",
    "effective_pre_confidence_risk_drag": "REAL",
    "effective_post_confidence_risk_drag": "REAL",
    "effective_total_risk_drag": "REAL",
    "confidence_adjusted_score_reduction": "REAL",
    "commercial_risk_overlay_score": "REAL",
    "commercial_risk_overlay_flag": "REAL",
    "commercial_risk_overlay_reasons": "TEXT",
    "commercial_risk_overlay_penalty": "REAL",
    "production_policy_quality_penalty": "REAL",
    "production_policy_quality_bonus": "REAL",
    "pre_rank_cap_opportunity_score": "REAL",
    "rank_quality_cap": "REAL",
    "rank_quality_cap_reasons": "TEXT",
    "rank_quality_cap_vetoed": "REAL",
    "rank_quality_cap_veto_reasons": "TEXT",
    "commercial_deterioration_score": "REAL",
    "commercial_deterioration_flag": "REAL",
    "commercial_deterioration_reasons": "TEXT",
    "valuation_growth_mismatch_score": "REAL",
    "valuation_growth_mismatch_flag": "REAL",
    "valuation_growth_mismatch_reasons": "TEXT",
    "transient_revenue_anchor_score": "REAL",
    "transient_revenue_anchor_flag": "REAL",
    "transient_revenue_anchor_reasons": "TEXT",
    "commercial_business_shock_score": "REAL",
    "commercial_business_shock_flag": "REAL",
    "commercial_business_shock_reasons": "TEXT",
    "no_forward_guidance_flag": "REAL",
    "guidance_staleness_flag": "REAL",
    "guidance_stale_flag": "REAL",
    "no_guidance_negative_growth_flag": "REAL",
    "primary_nct": "TEXT",
    "primary_trial_title": "TEXT",
    "ctgov_evidence_type": "TEXT",
    "company_strategy_category": "TEXT",
    "ctgov_review_bucket": "TEXT",
    "ctgov_manual_root_cause": "TEXT",
    "verified_qualifying_active_trial_count": "REAL",
    "phase2_3_active_trials": "REAL",
    "lead_phase2_3_active_trials": "REAL",
    "program_phase2_3_active_trials": "REAL",
    "collaborator_phase2_3_active_trials": "REAL",
    "effective_phase2_3_trials": "REAL",
    "core_pipeline_quality_score": "REAL",
    "collaborator_dependency_ratio": "REAL",
    "collaborator_heavy_flag": "REAL",
    "active_pivotal_trials": "REAL",
    "median_addv20": "REAL",
    "cash_runway_months": "REAL",
    "financial_survival_score": "REAL",
    "financial_data_quality": "TEXT",
    "going_concern_status": "TEXT",
    "reverse_split_hits_2y": "REAL",
    "sec_regulatory_catalyst_count": "REAL",
    "sec_dilution_event_count": "REAL",
    "sec_negative_clinical_event_count": "REAL",
    "biotech_primary_cohort": "TEXT",
    "biotech_secondary_cohort": "TEXT",
    "biotech_cohort_reason_codes": "TEXT",
    "biotech_cohort_confidence": "REAL",
    "biotech_cohort_margin": "REAL",
    "biotech_cohort_source": "TEXT",
    "biotech_cohort_overlays": "TEXT",
    "biotech_cohort_data_quality": "TEXT",
    "biotech_taxonomy_review_required": "REAL",
    "biotech_cohort_sparse_data_flag": "REAL",
    "biotech_cohort_size": "REAL",
    "biotech_cohort_rank": "REAL",
    "biotech_cohort_percentile": "REAL",
    "biotech_cohort_percentile_shrunk": "REAL",
    "biotech_cohort_reliability_score": "REAL",
    "biotech_cohort_calibration_weight": "REAL",
    "biotech_cohort_investible_flag": "REAL",
    "biotech_cohort_calibration_eligible_flag": "REAL",
    "biotech_cohort_calibration_mode": "TEXT",
    "biotech_cohort_exclusion_reason": "TEXT",
    "biotech_cohort_model_version": "TEXT",
    "biotech_cohort_evidence_json": "TEXT",
}

COMMERCIAL_VALUE_OPTIONAL_COLUMNS = {
    "avg_dollar_volume_20d": "REAL",
    "return_3m_pct": "REAL",
    "price_vs_200d_pct": "REAL",
    "distance_from_52w_high_pct": "REAL",
    "relative_strength_3m_vs_xbi": "REAL",
    "leverage_score": "REAL",
    "quality_adjusted_valuation_score": "REAL",
    "institutional_upside_capacity_score": "REAL",
    "value_trap_score": "REAL",
}

FORWARD_GUIDANCE_FEATURE_OPTIONAL_COLUMNS = {
    "quality_forward_valuation_score": "REAL",
    "quality_adjusted_guidance_score": "REAL",
    "guidance_recency_penalty": "REAL",
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

DAILY_FEATURES_OPTIONAL_COLUMNS = {
    "financial_quality_score_raw": "REAL",
    "legacy_risk_score_raw": "REAL",
    "risk_penalty_input_score_raw": "REAL",
    "predictive_risk_penalty_input_score_raw": "REAL",
    "uncompensated_risk_score_raw": "REAL",
    "compensated_risk_score_raw": "REAL",
    "liquidity_risk_score_raw": "REAL",
    "financing_survival_risk_score_raw": "REAL",
    "governance_filing_risk_score_raw": "REAL",
    "regulatory_setback_risk_score_raw": "REAL",
    "pipeline_anchor_risk_score_raw": "REAL",
    "collaborator_dependency_risk_score_raw": "REAL",
    "trial_staleness_risk_score_raw": "REAL",
    "momentum_score_raw": "REAL",
}

FORWARD_GUIDANCE_OPTIONAL_COLUMNS = {
    "guidance_unique_key": "TEXT",
}

GOVERNANCE_EVENT_OPTIONAL_COLUMNS = {
    "regulatory_setback_count_365d": "INTEGER",
    "adverse_legal_event_count_365d": "INTEGER",
    "generic_competition_risk_count_365d": "INTEGER",
    "product_concentration_risk_count_365d": "INTEGER",
    "commercial_fragility_risk_score": "REAL",
    "proxy_fields_used": "TEXT",
}

MULTIBAGGER_FEATURE_OPTIONAL_COLUMNS = {
    "commercial_fragility_risk_score": "REAL",
}

MULTIBAGGER_SCORES_OPTIONAL_COLUMNS = {
    "base_multibagger_score": "REAL",
    "orthogonal_alpha_score": "REAL",
    "distinctive_acceleration_score": "REAL",
    "tier1_opportunity_score": "REAL",
    "tier1_risk_score": "REAL",
    "tier1_bucket": "TEXT",
    "tier1_gate_score": "REAL",
    "tier1_gate_multiplier": "REAL",
    "tier1_available": "INTEGER",
    "tier1_interaction_reason": "TEXT",
    "tier1_score_tier": "TEXT",
    "tier1_allocation_eligible": "INTEGER",
    "tier1_research_watchlist": "INTEGER",
    "tier1_score_spread_to_allocation": "REAL",
    "tier1_score_spread_to_high_confidence": "REAL",
    "tier1_rank_quality_cap": "REAL",
    "tier1_rank_quality_cap_reasons": "TEXT",
    "tier1_rank_quality_cap_vetoed": "INTEGER",
    "tier1_rank_quality_cap_veto_reasons": "TEXT",
    "tier1_mature_defensive_score": "REAL",
    "tier1_expected_return_quality_score": "REAL",
    "tier1_value_trap_score": "REAL",
    "tier1_leverage_score": "REAL",
    "tier1_leverage_fragility_score": "REAL",
    "tier1_no_forward_guidance_flag": "REAL",
    "tier1_guidance_stale_flag": "REAL",
    "tier1_no_guidance_negative_growth_flag": "REAL",
    "tier1_production_policy_quality_penalty": "REAL",
    "tier1_production_policy_quality_bonus": "REAL",
    "liquidity_ok": "INTEGER",
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
            suppress = self._conn.__exit__(exc_type, exc_value, traceback)
        except Exception:
            try:
                self._conn.rollback()
            except Exception:
                pass
            try:
                self._conn.close()
            except Exception:
                pass
            raise
        try:
            self._conn.close()
        except Exception:
            if exc_type is None:
                raise
        return suppress

    def __getattr__(self, name: str):
        return getattr(self._conn, name)


def connect(db_path: Path, *, timeout_sec: float = 30.0) -> ManagedConnection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=timeout_sec)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA cache_size = -65536")
    conn.execute("PRAGMA mmap_size = 268435456")
    conn.execute("PRAGMA temp_store = MEMORY")
    return ManagedConnection(conn)


def init_db(conn: sqlite3.Connection) -> None:
    if conn.in_transaction:
        raise RuntimeError("init_db() must be called outside an active transaction; sqlite3.executescript() commits ambient transactions.")
    conn.executescript(SCHEMA_SQL)
    conn.commit()
    ensure_company_universe_history_unique_index(conn)
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
    ensure_table_optional_columns(conn, "daily_features", DAILY_FEATURES_OPTIONAL_COLUMNS)
    ensure_table_optional_columns(conn, "financial_survival_features", FINANCIAL_SURVIVAL_OPTIONAL_COLUMNS)
    ensure_table_optional_columns(conn, "daily_scores", DAILY_SCORES_OPTIONAL_COLUMNS)
    ensure_table_optional_columns(conn, "commercial_value_features_daily", COMMERCIAL_VALUE_OPTIONAL_COLUMNS)
    ensure_table_optional_columns(conn, "market_bars_daily", MARKET_BARS_OPTIONAL_COLUMNS)
    ensure_table_optional_columns(conn, "market_snapshots_daily", MARKET_DAILY_OPTIONAL_COLUMNS)
    ensure_table_optional_columns(conn, "market_features_daily", MARKET_DAILY_OPTIONAL_COLUMNS)
    ensure_table_optional_columns(conn, "company_forward_guidance", FORWARD_GUIDANCE_OPTIONAL_COLUMNS)
    ensure_table_optional_columns(
        conn,
        "forward_guidance_features_daily",
        FORWARD_GUIDANCE_FEATURE_OPTIONAL_COLUMNS,
    )
    ensure_forward_guidance_unique_keys(conn)
    ensure_table_optional_columns(conn, "company_forward_guidance_overrides", FORWARD_GUIDANCE_OVERRIDES_OPTIONAL_COLUMNS)
    conn.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_forward_guidance_unique_key
        ON company_forward_guidance(guidance_unique_key)
        """
    )
    ensure_table_optional_columns(conn, "governance_event_features_daily", GOVERNANCE_EVENT_OPTIONAL_COLUMNS)
    ensure_table_optional_columns(conn, "multibagger_features_daily", MULTIBAGGER_FEATURE_OPTIONAL_COLUMNS)
    ensure_table_optional_columns(conn, "multibagger_scores_daily", MULTIBAGGER_SCORES_OPTIONAL_COLUMNS)
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
    table_sql = quote_identifier(table_name)
    columns: set[str] = set()
    for row in conn.execute(f"PRAGMA table_info({table_sql})").fetchall():
        try:
            columns.add(str(row["name"]))
        except (TypeError, IndexError):
            try:
                columns.add(str(row[1]))
            except (TypeError, IndexError):
                LOGGER.warning("Could not read column name from PRAGMA table_info(%s) row: %r", table_name, row)
    return columns


def _table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table_name,),
    ).fetchone()
    return row is not None


def ensure_table_optional_columns(conn: sqlite3.Connection, table_name: str, columns: dict[str, str]) -> None:
    existing = _table_column_names(conn, table_name)
    table_sql = quote_identifier(table_name)
    for column, column_type in columns.items():
        if column not in existing:
            normalized_type = str(column_type or "").strip().upper()
            if not SAFE_COLUMN_TYPE_RE.fullmatch(normalized_type):
                raise ValueError(f"Unsafe SQLite column type for {table_name}.{column}: {column_type!r}")
            column_sql = quote_identifier(column)
            conn.execute(f"ALTER TABLE {table_sql} ADD COLUMN {column_sql} {normalized_type}")


def _run_schema_migration(conn: sqlite3.Connection, name: str, callback) -> None:
    if not SAFE_IDENTIFIER_RE.fullmatch(str(name or "")):
        raise ValueError(f"Unsafe schema migration name: {name!r}")
    savepoint = f"schema_migration_{name}"
    savepoint_sql = quote_identifier(savepoint)
    conn.execute(f"SAVEPOINT {savepoint_sql}")
    try:
        callback()
    except Exception:
        conn.execute(f"ROLLBACK TO {savepoint_sql}")
        conn.execute(f"RELEASE {savepoint_sql}")
        raise
    conn.execute(f"RELEASE {savepoint_sql}")


def _coalesce_existing_expr(
    columns: set[str],
    candidates: list[str],
    fallback: str,
    *,
    fallback_param: object | None = None,
) -> tuple[str, tuple[object, ...]]:
    present = [column for column in candidates if column in columns]
    fallback_params = (fallback_param,) if fallback == "?" else ()
    if not present:
        return fallback, fallback_params
    return f"COALESCE({', '.join([*(quote_identifier(column) for column in present), fallback])})", fallback_params


def _normalize_guidance_key_number(raw: object, *, null_token: str = "<NULL>") -> str:
    if raw is None:
        return null_token
    try:
        value = float(str(raw).strip())
    except (TypeError, ValueError):
        return str(raw)
    if not math.isfinite(value):
        return null_token
    return f"{value:.12g}"


def _guidance_unique_key_from_values(
    asof_date: object,
    company_id: object,
    accession_nodash: object,
    metric: object,
    guidance_year: object,
    low_value: object,
    high_value: object,
) -> str:
    try:
        parsed_company_id = int(str(company_id).strip())
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Invalid company_id for guidance unique key: {company_id!r}") from exc
    return json.dumps(
        [
            str(asof_date or ""),
            parsed_company_id,
            str(accession_nodash or ""),
            str(metric or ""),
            "<NULL>" if guidance_year is None else str(guidance_year),
            _normalize_guidance_key_number(low_value),
            _normalize_guidance_key_number(high_value),
        ],
        ensure_ascii=True,
        separators=(",", ":"),
    )


def ensure_company_universe_history_unique_index(conn: sqlite3.Connection) -> None:
    if not _table_exists(conn, "company_universe_history"):
        return
    duplicate_row = conn.execute(
        """
        SELECT COUNT(*) AS duplicate_count
        FROM company_universe_history
        WHERE history_id NOT IN (
            SELECT MIN(history_id)
            FROM company_universe_history
            GROUP BY asof_date, company_id, COALESCE(run_id, -1)
        )
        """
    ).fetchone()
    try:
        duplicate_count = int(duplicate_row["duplicate_count"])
    except (TypeError, IndexError):
        duplicate_count = int(duplicate_row[0])
    if duplicate_count:
        LOGGER.warning("Removing %d duplicate company_universe_history row(s) before adding unique index", duplicate_count)
        conn.execute(
            """
            DELETE FROM company_universe_history
            WHERE history_id IN (
                SELECT history_id
                FROM (
                    SELECT
                        history_id,
                        ROW_NUMBER() OVER (
                            PARTITION BY asof_date, company_id, COALESCE(run_id, -1)
                            ORDER BY history_id
                        ) AS duplicate_rank
                    FROM company_universe_history
                )
                WHERE duplicate_rank > 1
            )
            """
        )
    conn.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_company_universe_history_unique_key
        ON company_universe_history(asof_date, company_id, COALESCE(run_id, -1))
        """
    )


def ensure_forward_guidance_unique_keys(conn: sqlite3.Connection) -> None:
    if not _table_exists(conn, "company_forward_guidance"):
        return
    columns = _table_column_names(conn, "company_forward_guidance")
    if "guidance_unique_key" not in columns:
        return

    existing_keys = {
        str(row["guidance_unique_key"])
        for row in conn.execute(
            """
            SELECT DISTINCT guidance_unique_key
            FROM company_forward_guidance
            WHERE COALESCE(guidance_unique_key, '') <> ''
            """
        ).fetchall()
    }
    rows = conn.execute(
        """
        SELECT guidance_id, asof_date, company_id, accession_nodash, metric,
               guidance_year, low_value, high_value
        FROM company_forward_guidance
        WHERE COALESCE(guidance_unique_key, '') = ''
        ORDER BY guidance_id
        """
    ).fetchall()

    ids_to_delete: list[int] = []
    updates: list[tuple[str, int]] = []
    seen_keys: set[str] = set()
    for row in rows:
        guidance_id = int(row["guidance_id"])
        unique_key = _guidance_unique_key_from_values(
            row["asof_date"],
            row["company_id"],
            row["accession_nodash"],
            row["metric"],
            row["guidance_year"],
            row["low_value"],
            row["high_value"],
        )
        if unique_key in existing_keys or unique_key in seen_keys:
            ids_to_delete.append(guidance_id)
            continue
        seen_keys.add(unique_key)
        updates.append((unique_key, guidance_id))

    if ids_to_delete:
        for start in range(0, len(ids_to_delete), 800):
            chunk = ids_to_delete[start : start + 800]
            if not all(isinstance(value, int) for value in chunk):
                raise TypeError("Non-integer guidance_id in company_forward_guidance cleanup")
            placeholders = ",".join("?" for _ in chunk)
            conn.execute(f"DELETE FROM company_forward_guidance WHERE guidance_id IN ({placeholders})", chunk)
    if updates:
        conn.executemany(
            """
            UPDATE company_forward_guidance
            SET guidance_unique_key = ?
            WHERE guidance_id = ?
            """,
            updates,
        )
    conn.execute(
        """
        DELETE FROM company_forward_guidance
        WHERE COALESCE(guidance_unique_key, '') <> ''
          AND guidance_id NOT IN (
              SELECT MIN(guidance_id)
              FROM company_forward_guidance
              WHERE COALESCE(guidance_unique_key, '') <> ''
              GROUP BY guidance_unique_key
          )
        """
    )

    conn.execute(
        """
        CREATE TRIGGER IF NOT EXISTS trg_forward_guidance_key_required_insert
        BEFORE INSERT ON company_forward_guidance
        WHEN NEW.guidance_unique_key IS NULL OR NEW.guidance_unique_key = ''
        BEGIN
            SELECT RAISE(ABORT, 'company_forward_guidance.guidance_unique_key is required');
        END
        """
    )
    conn.execute(
        """
        CREATE TRIGGER IF NOT EXISTS trg_forward_guidance_key_required_update
        BEFORE UPDATE OF guidance_unique_key ON company_forward_guidance
        WHEN NEW.guidance_unique_key IS NULL OR NEW.guidance_unique_key = ''
        BEGIN
            SELECT RAISE(ABORT, 'company_forward_guidance.guidance_unique_key is required');
        END
        """
    )


def refresh_sec_latest_documents(conn: sqlite3.Connection, accessions: Iterable[str]) -> int:
    """Refresh latest SEC document metadata without reading large text_content blobs."""
    now = utc_now()
    refreshed = 0
    target_accessions = sorted({str(value or "").strip() for value in accessions if str(value or "").strip()})

    def refresh_rows() -> None:
        nonlocal refreshed
        for accession in target_accessions:
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

    if conn.in_transaction:
        accession_digest = hashlib.sha1("|".join(target_accessions).encode("utf-8")).hexdigest()[:16]
        savepoint = quote_identifier(f"refresh_sec_latest_documents_{accession_digest}")
        conn.execute(f"SAVEPOINT {savepoint}")
        try:
            refresh_rows()
        except Exception:
            conn.execute(f"ROLLBACK TO {savepoint}")
            conn.execute(f"RELEASE {savepoint}")
            raise
        conn.execute(f"RELEASE {savepoint}")
    else:
        with conn:
            refresh_rows()
    return refreshed


def _create_sec_event_parse_state(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS sec_event_parse_state (
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


def _copy_sec_event_parse_state_legacy(conn: sqlite3.Connection) -> None:
    columns = _table_column_names(conn, "sec_event_parse_state_legacy")
    if "accession_nodash" not in columns:
        return
    now = utc_now()
    accession_expr = quote_identifier("accession_nodash")
    text_hash_expr = quote_identifier("text_hash") if "text_hash" in columns else "''"
    parser_signature_expr = quote_identifier("parser_signature") if "parser_signature" in columns else "''"
    parsed_at_expr, parsed_at_params = _coalesce_existing_expr(
        columns, ["parsed_at", "updated_at", "created_at"], "?", fallback_param=now
    )
    event_count_expr = f"COALESCE({quote_identifier('event_count')}, 0)" if "event_count" in columns else "0"
    created_at_expr, created_at_params = _coalesce_existing_expr(
        columns, ["created_at", "updated_at", "parsed_at"], "?", fallback_param=now
    )
    updated_at_expr, updated_at_params = _coalesce_existing_expr(
        columns, ["updated_at", "parsed_at", "created_at"], "?", fallback_param=now
    )
    sql = f"""
        INSERT OR IGNORE INTO sec_event_parse_state(
            accession_nodash, text_hash, parser_signature, parsed_at, event_count, created_at, updated_at
        )
        SELECT {accession_expr}, {text_hash_expr}, {parser_signature_expr}, {parsed_at_expr}, {event_count_expr},
               {created_at_expr}, {updated_at_expr}
        FROM {quote_identifier("sec_event_parse_state_legacy")}
        WHERE COALESCE({accession_expr}, '') <> ''
        """
    conn.execute(sql, parsed_at_params + created_at_params + updated_at_params)


def _create_company_facts_sync_state(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS company_facts_sync_state (
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


def _copy_company_facts_sync_state_legacy(conn: sqlite3.Connection) -> None:
    columns = _table_column_names(conn, "company_facts_sync_state_legacy")
    if "company_id" not in columns:
        return
    now = utc_now()
    company_id_expr = quote_identifier("company_id")
    latest_source_expr = quote_identifier("latest_source_filing_date") if "latest_source_filing_date" in columns else "''"
    payload_hash_expr = quote_identifier("payload_hash") if "payload_hash" in columns else "''"
    last_synced_expr, last_synced_params = _coalesce_existing_expr(
        columns, ["last_synced_at", "updated_at", "created_at"], "?", fallback_param=now
    )
    sync_status_expr, sync_status_params = _coalesce_existing_expr(columns, ["sync_status"], "'unknown'")
    created_at_expr, created_at_params = _coalesce_existing_expr(
        columns, ["created_at", "updated_at", "last_synced_at"], "?", fallback_param=now
    )
    updated_at_expr, updated_at_params = _coalesce_existing_expr(
        columns, ["updated_at", "last_synced_at", "created_at"], "?", fallback_param=now
    )
    sql = f"""
        INSERT OR IGNORE INTO company_facts_sync_state(
            company_id, latest_source_filing_date, payload_hash, last_synced_at, sync_status, created_at, updated_at
        )
        SELECT {company_id_expr}, {latest_source_expr}, {payload_hash_expr}, {last_synced_expr},
               {sync_status_expr}, {created_at_expr}, {updated_at_expr}
        FROM {quote_identifier("company_facts_sync_state_legacy")}
        WHERE {company_id_expr} IS NOT NULL
        """
    conn.execute(sql, last_synced_params + sync_status_params + created_at_params + updated_at_params)


def ensure_state_tables_created_at(conn: sqlite3.Connection) -> None:
    if _table_exists(conn, "sec_event_parse_state_legacy"):
        def recover_sec_event_legacy() -> None:
            _create_sec_event_parse_state(conn)
            _copy_sec_event_parse_state_legacy(conn)
            conn.execute("DROP TABLE sec_event_parse_state_legacy")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_sec_event_parse_state_hash ON sec_event_parse_state(text_hash)")

        _run_schema_migration(conn, "recover_sec_event_parse_state", recover_sec_event_legacy)

    sec_event_cols = _table_column_names(conn, "sec_event_parse_state")
    if sec_event_cols and "created_at" not in sec_event_cols:
        def migrate_sec_event() -> None:
            conn.execute("DROP INDEX IF EXISTS idx_sec_event_parse_state_hash")
            conn.execute("ALTER TABLE sec_event_parse_state RENAME TO sec_event_parse_state_legacy")
            _create_sec_event_parse_state(conn)
            _copy_sec_event_parse_state_legacy(conn)
            conn.execute("DROP TABLE sec_event_parse_state_legacy")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_sec_event_parse_state_hash ON sec_event_parse_state(text_hash)")

        _run_schema_migration(conn, "sec_event_parse_state_created_at", migrate_sec_event)

    if _table_exists(conn, "company_facts_sync_state_legacy"):
        def recover_company_facts_legacy() -> None:
            _create_company_facts_sync_state(conn)
            _copy_company_facts_sync_state_legacy(conn)
            conn.execute("DROP TABLE company_facts_sync_state_legacy")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_company_facts_sync_state_status ON company_facts_sync_state(sync_status, last_synced_at)"
            )

        _run_schema_migration(conn, "recover_company_facts_sync_state", recover_company_facts_legacy)

    facts_sync_cols = _table_column_names(conn, "company_facts_sync_state")
    if facts_sync_cols and "created_at" not in facts_sync_cols:
        def migrate_company_facts() -> None:
            conn.execute("DROP INDEX IF EXISTS idx_company_facts_sync_state_status")
            conn.execute("ALTER TABLE company_facts_sync_state RENAME TO company_facts_sync_state_legacy")
            _create_company_facts_sync_state(conn)
            _copy_company_facts_sync_state_legacy(conn)
            conn.execute("DROP TABLE company_facts_sync_state_legacy")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_company_facts_sync_state_status ON company_facts_sync_state(sync_status, last_synced_at)"
            )

        _run_schema_migration(conn, "company_facts_sync_state_created_at", migrate_company_facts)


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
    if cur.lastrowid is None:
        raise RuntimeError("Could not create pipeline run row; sqlite cursor returned no lastrowid.")
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
