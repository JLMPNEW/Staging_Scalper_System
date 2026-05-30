from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


SAFE_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


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

CREATE TABLE IF NOT EXISTS source_registry (
    source_id TEXT PRIMARY KEY,
    stage TEXT NOT NULL,
    source_name TEXT NOT NULL,
    source_owner TEXT,
    source_type TEXT NOT NULL,
    base_url TEXT NOT NULL,
    documentation_url TEXT,
    authentication_required INTEGER NOT NULL DEFAULT 0,
    free_key_required INTEGER NOT NULL DEFAULT 0,
    api_key_env TEXT,
    rate_limit_notes TEXT,
    refresh_frequency TEXT,
    terms_url TEXT,
    data_owner TEXT,
    raw_schema TEXT,
    staging_tables TEXT,
    canonical_tables TEXT,
    feature_stages TEXT,
    priority INTEGER NOT NULL DEFAULT 100,
    status TEXT NOT NULL DEFAULT 'planned',
    notes TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ingestion_runs (
    ingestion_run_id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id TEXT NOT NULL,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    status TEXT NOT NULL,
    request_count INTEGER NOT NULL DEFAULT 0,
    row_count INTEGER NOT NULL DEFAULT 0,
    message TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY (source_id) REFERENCES source_registry(source_id) ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS raw_api_responses (
    raw_response_id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id TEXT NOT NULL,
    endpoint TEXT NOT NULL,
    query_params_json TEXT,
    request_time_utc TEXT NOT NULL,
    response_status INTEGER,
    response_hash TEXT NOT NULL,
    asof_date TEXT,
    payload_text TEXT,
    ingestion_run_id INTEGER,
    created_at TEXT NOT NULL,
    FOREIGN KEY (source_id) REFERENCES source_registry(source_id) ON DELETE RESTRICT,
    FOREIGN KEY (ingestion_run_id) REFERENCES ingestion_runs(ingestion_run_id) ON DELETE SET NULL,
    UNIQUE(source_id, endpoint, response_hash)
);

CREATE TABLE IF NOT EXISTS dim_company (
    company_id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL UNIQUE,
    cik TEXT,
    company_name TEXT NOT NULL,
    exchange TEXT,
    sector TEXT,
    industry TEXT,
    subsector TEXT,
    country TEXT,
    currency TEXT,
    universe_status TEXT NOT NULL DEFAULT 'candidate',
    is_active INTEGER NOT NULL DEFAULT 1,
    medtech_pure_play_flag INTEGER NOT NULL DEFAULT 0,
    data_quality_status TEXT,
    first_seen_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS dim_security (
    security_id INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id INTEGER NOT NULL,
    ticker TEXT NOT NULL,
    exchange TEXT,
    security_type TEXT,
    listing_status TEXT,
    is_primary_listing INTEGER NOT NULL DEFAULT 1,
    currency TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (company_id) REFERENCES dim_company(company_id) ON DELETE CASCADE,
    UNIQUE(ticker, exchange)
);

CREATE TABLE IF NOT EXISTS dim_identifier (
    identifier_id INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id INTEGER NOT NULL,
    identifier_type TEXT NOT NULL,
    identifier_value TEXT NOT NULL,
    source_id TEXT,
    confidence REAL NOT NULL DEFAULT 1.0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (company_id) REFERENCES dim_company(company_id) ON DELETE CASCADE,
    FOREIGN KEY (source_id) REFERENCES source_registry(source_id) ON DELETE SET NULL,
    UNIQUE(identifier_type, identifier_value)
);

CREATE TABLE IF NOT EXISTS dim_company_alias (
    alias_id INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id INTEGER NOT NULL,
    alias_raw TEXT NOT NULL,
    alias_norm TEXT NOT NULL,
    source_id TEXT,
    confidence REAL NOT NULL DEFAULT 1.0,
    is_manual INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (company_id) REFERENCES dim_company(company_id) ON DELETE CASCADE,
    FOREIGN KEY (source_id) REFERENCES source_registry(source_id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS dim_fda_manufacturer (
    fda_manufacturer_id INTEGER PRIMARY KEY AUTOINCREMENT,
    manufacturer_name TEXT NOT NULL,
    manufacturer_name_norm TEXT NOT NULL,
    fei_number TEXT,
    parent_company_id INTEGER,
    mapping_confidence REAL NOT NULL DEFAULT 0.0,
    mapping_method TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (parent_company_id) REFERENCES dim_company(company_id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS dim_fda_product_code (
    product_code TEXT PRIMARY KEY,
    device_name TEXT,
    medical_specialty TEXT,
    device_class TEXT,
    regulation_number TEXT,
    source_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (source_id) REFERENCES source_registry(source_id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS dim_device (
    device_id INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id INTEGER,
    fda_manufacturer_id INTEGER,
    product_code TEXT,
    device_name TEXT NOT NULL,
    brand_name TEXT,
    udi_di TEXT,
    model_number TEXT,
    catalog_number TEXT,
    device_class TEXT,
    mapping_confidence REAL NOT NULL DEFAULT 0.0,
    source_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (company_id) REFERENCES dim_company(company_id) ON DELETE SET NULL,
    FOREIGN KEY (fda_manufacturer_id) REFERENCES dim_fda_manufacturer(fda_manufacturer_id) ON DELETE SET NULL,
    FOREIGN KEY (product_code) REFERENCES dim_fda_product_code(product_code) ON DELETE SET NULL,
    FOREIGN KEY (source_id) REFERENCES source_registry(source_id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS dim_reimbursement_code (
    reimbursement_code_id INTEGER PRIMARY KEY AUTOINCREMENT,
    code_type TEXT NOT NULL,
    code TEXT NOT NULL,
    short_description TEXT,
    long_description TEXT,
    effective_date TEXT,
    termination_date TEXT,
    source_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (source_id) REFERENCES source_registry(source_id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS fact_price_ohlcv (
    ticker TEXT NOT NULL,
    bar_date TEXT NOT NULL,
    source_id TEXT NOT NULL,
    open REAL,
    high REAL,
    low REAL,
    close REAL,
    adj_close REAL,
    volume REAL,
    dividend_amount REAL,
    split_factor REAL,
    price_adjustment TEXT,
    is_adjusted INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(ticker, bar_date, source_id),
    FOREIGN KEY (source_id) REFERENCES source_registry(source_id) ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS fact_market_snapshot (
    ticker TEXT NOT NULL,
    asof_date TEXT NOT NULL,
    source_id TEXT NOT NULL,
    shares_outstanding REAL,
    market_cap REAL,
    currency TEXT,
    source_timestamp TEXT,
    payload_json TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(ticker, asof_date, source_id),
    FOREIGN KEY (source_id) REFERENCES source_registry(source_id) ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS fact_sec_filing (
    accession_nodash TEXT PRIMARY KEY,
    company_id INTEGER NOT NULL,
    form TEXT NOT NULL,
    filing_date TEXT NOT NULL,
    report_date TEXT,
    primary_document TEXT,
    archive_url TEXT,
    source_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (company_id) REFERENCES dim_company(company_id) ON DELETE CASCADE,
    FOREIGN KEY (source_id) REFERENCES source_registry(source_id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS fact_financial_statement (
    financial_statement_id INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id INTEGER NOT NULL,
    period_end TEXT NOT NULL,
    fiscal_year INTEGER,
    fiscal_period TEXT,
    form TEXT,
    filed_date TEXT,
    accession_nodash TEXT,
    revenue REAL,
    gross_profit REAL,
    operating_income REAL,
    net_income REAL,
    operating_cash_flow REAL,
    capital_expenditures REAL,
    free_cash_flow REAL,
    research_and_development REAL,
    interest_expense REAL,
    cash_and_investments REAL,
    total_debt REAL,
    total_assets REAL,
    stockholders_equity REAL,
    shares_outstanding REAL,
    source_id TEXT,
    payload_json TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (company_id) REFERENCES dim_company(company_id) ON DELETE CASCADE,
    FOREIGN KEY (accession_nodash) REFERENCES fact_sec_filing(accession_nodash) ON DELETE SET NULL,
    FOREIGN KEY (source_id) REFERENCES source_registry(source_id) ON DELETE SET NULL,
    UNIQUE(company_id, period_end, fiscal_period, form)
);

CREATE TABLE IF NOT EXISTS fact_fda_approval (
    fda_approval_id INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id INTEGER,
    fda_manufacturer_id INTEGER,
    product_code TEXT,
    submission_number TEXT NOT NULL,
    submission_type TEXT NOT NULL,
    decision_date TEXT,
    receipt_date TEXT,
    device_name TEXT,
    decision TEXT,
    source_id TEXT,
    payload_json TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (company_id) REFERENCES dim_company(company_id) ON DELETE SET NULL,
    FOREIGN KEY (fda_manufacturer_id) REFERENCES dim_fda_manufacturer(fda_manufacturer_id) ON DELETE SET NULL,
    FOREIGN KEY (product_code) REFERENCES dim_fda_product_code(product_code) ON DELETE SET NULL,
    FOREIGN KEY (source_id) REFERENCES source_registry(source_id) ON DELETE SET NULL,
    UNIQUE(submission_number, submission_type)
);

CREATE TABLE IF NOT EXISTS fact_fda_recall (
    fda_recall_id INTEGER PRIMARY KEY AUTOINCREMENT,
    recall_key TEXT,
    endpoint_name TEXT,
    company_id INTEGER,
    fda_manufacturer_id INTEGER,
    product_code TEXT,
    recall_number TEXT,
    event_id TEXT,
    classification TEXT,
    severity_weight REAL,
    status TEXT,
    recalling_firm TEXT,
    reason_for_recall TEXT,
    recall_initiation_date TEXT,
    center_classification_date TEXT,
    termination_date TEXT,
    source_id TEXT,
    payload_json TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (company_id) REFERENCES dim_company(company_id) ON DELETE SET NULL,
    FOREIGN KEY (fda_manufacturer_id) REFERENCES dim_fda_manufacturer(fda_manufacturer_id) ON DELETE SET NULL,
    FOREIGN KEY (product_code) REFERENCES dim_fda_product_code(product_code) ON DELETE SET NULL,
    FOREIGN KEY (source_id) REFERENCES source_registry(source_id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS fact_fda_recall_canonical (
    canonical_recall_id INTEGER PRIMARY KEY AUTOINCREMENT,
    canonical_recall_key TEXT NOT NULL UNIQUE,
    recall_number TEXT,
    event_id TEXT,
    company_id INTEGER,
    fda_manufacturer_id INTEGER,
    product_code TEXT,
    classification TEXT,
    max_severity_weight REAL,
    status TEXT,
    is_open INTEGER NOT NULL DEFAULT 0,
    is_terminated INTEGER NOT NULL DEFAULT 0,
    recall_initiation_date TEXT,
    center_classification_date TEXT,
    termination_date TEXT,
    recalling_firm TEXT,
    product_description TEXT,
    reason_for_recall TEXT,
    source_count INTEGER NOT NULL DEFAULT 0,
    source_endpoints TEXT,
    source_priority TEXT,
    mapping_confidence REAL,
    mapping_method TEXT,
    payload_json TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (company_id) REFERENCES dim_company(company_id) ON DELETE SET NULL,
    FOREIGN KEY (fda_manufacturer_id) REFERENCES dim_fda_manufacturer(fda_manufacturer_id) ON DELETE SET NULL,
    FOREIGN KEY (product_code) REFERENCES dim_fda_product_code(product_code) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS fact_fda_adverse_event (
    adverse_event_id TEXT PRIMARY KEY,
    company_id INTEGER,
    fda_manufacturer_id INTEGER,
    product_code TEXT,
    event_date TEXT,
    report_date TEXT,
    report_type TEXT,
    death_count INTEGER NOT NULL DEFAULT 0,
    injury_count INTEGER NOT NULL DEFAULT 0,
    malfunction_count INTEGER NOT NULL DEFAULT 0,
    event_type TEXT,
    device_problem_codes TEXT,
    patient_problem_codes TEXT,
    source_id TEXT,
    payload_json TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (company_id) REFERENCES dim_company(company_id) ON DELETE SET NULL,
    FOREIGN KEY (fda_manufacturer_id) REFERENCES dim_fda_manufacturer(fda_manufacturer_id) ON DELETE SET NULL,
    FOREIGN KEY (product_code) REFERENCES dim_fda_product_code(product_code) ON DELETE SET NULL,
    FOREIGN KEY (source_id) REFERENCES source_registry(source_id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS fact_fda_inspection (
    fda_inspection_id INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id INTEGER,
    fda_manufacturer_id INTEGER,
    fei_number TEXT,
    legal_name TEXT,
    inspection_end_date TEXT,
    classification_code TEXT,
    classification TEXT,
    product_type TEXT,
    project_area TEXT,
    source_id TEXT,
    payload_json TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (company_id) REFERENCES dim_company(company_id) ON DELETE SET NULL,
    FOREIGN KEY (fda_manufacturer_id) REFERENCES dim_fda_manufacturer(fda_manufacturer_id) ON DELETE SET NULL,
    FOREIGN KEY (source_id) REFERENCES source_registry(source_id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS fact_fda_compliance_action (
    fda_compliance_action_id INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id INTEGER,
    fda_manufacturer_id INTEGER,
    fei_number TEXT,
    legal_name TEXT,
    action_type TEXT,
    action_date TEXT,
    subject TEXT,
    status TEXT,
    source_id TEXT,
    payload_json TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (company_id) REFERENCES dim_company(company_id) ON DELETE SET NULL,
    FOREIGN KEY (fda_manufacturer_id) REFERENCES dim_fda_manufacturer(fda_manufacturer_id) ON DELETE SET NULL,
    FOREIGN KEY (source_id) REFERENCES source_registry(source_id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS fact_reimbursement_policy (
    reimbursement_policy_id INTEGER PRIMARY KEY AUTOINCREMENT,
    policy_id TEXT,
    policy_type TEXT NOT NULL,
    title TEXT,
    contractor_name TEXT,
    jurisdiction TEXT,
    effective_date TEXT,
    retirement_date TEXT,
    status TEXT,
    related_codes TEXT,
    source_id TEXT,
    payload_json TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (source_id) REFERENCES source_registry(source_id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS fact_reimbursement_rate (
    reimbursement_rate_id INTEGER PRIMARY KEY AUTOINCREMENT,
    reimbursement_code_id INTEGER,
    payment_system TEXT NOT NULL,
    effective_date TEXT,
    locality TEXT,
    apc TEXT,
    drg TEXT,
    payment_rate REAL,
    status_indicator TEXT,
    source_id TEXT,
    payload_json TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (reimbursement_code_id) REFERENCES dim_reimbursement_code(reimbursement_code_id) ON DELETE SET NULL,
    FOREIGN KEY (source_id) REFERENCES source_registry(source_id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS map_company_reimbursement_policy (
    company_id INTEGER NOT NULL,
    reimbursement_policy_id INTEGER NOT NULL,
    confidence REAL NOT NULL DEFAULT 0.0,
    mapping_method TEXT,
    matched_term TEXT,
    source_id TEXT,
    payload_json TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(company_id, reimbursement_policy_id),
    FOREIGN KEY (company_id) REFERENCES dim_company(company_id) ON DELETE CASCADE,
    FOREIGN KEY (reimbursement_policy_id) REFERENCES fact_reimbursement_policy(reimbursement_policy_id) ON DELETE CASCADE,
    FOREIGN KEY (source_id) REFERENCES source_registry(source_id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS map_company_reimbursement_code (
    company_reimbursement_code_id INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id INTEGER NOT NULL,
    reimbursement_code_id INTEGER NOT NULL,
    reimbursement_policy_id INTEGER,
    confidence REAL NOT NULL DEFAULT 0.0,
    mapping_method TEXT,
    matched_term TEXT,
    source_id TEXT,
    payload_json TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (company_id) REFERENCES dim_company(company_id) ON DELETE CASCADE,
    FOREIGN KEY (reimbursement_code_id) REFERENCES dim_reimbursement_code(reimbursement_code_id) ON DELETE CASCADE,
    FOREIGN KEY (reimbursement_policy_id) REFERENCES fact_reimbursement_policy(reimbursement_policy_id) ON DELETE CASCADE,
    FOREIGN KEY (source_id) REFERENCES source_registry(source_id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS fact_clinical_trial_status (
    clinical_trial_status_id INTEGER PRIMARY KEY AUTOINCREMENT,
    nct_id TEXT NOT NULL,
    company_id INTEGER,
    brief_title TEXT,
    overall_status TEXT,
    study_type TEXT,
    enrollment_count INTEGER,
    start_date TEXT,
    primary_completion_date TEXT,
    completion_date TEXT,
    last_update_post_date TEXT,
    interventions_json TEXT,
    source_id TEXT,
    payload_json TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (company_id) REFERENCES dim_company(company_id) ON DELETE SET NULL,
    FOREIGN KEY (source_id) REFERENCES source_registry(source_id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS fact_patent (
    patent_id TEXT PRIMARY KEY,
    company_id INTEGER,
    assignee_name TEXT,
    filing_date TEXT,
    grant_date TEXT,
    title TEXT,
    cpc_codes TEXT,
    citation_count INTEGER,
    source_id TEXT,
    payload_json TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (company_id) REFERENCES dim_company(company_id) ON DELETE SET NULL,
    FOREIGN KEY (source_id) REFERENCES source_registry(source_id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS fact_short_interest (
    ticker TEXT NOT NULL,
    settlement_date TEXT NOT NULL,
    source_id TEXT NOT NULL,
    short_interest REAL,
    avg_daily_volume REAL,
    days_to_cover REAL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(ticker, settlement_date, source_id),
    FOREIGN KEY (source_id) REFERENCES source_registry(source_id) ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS fact_macro (
    series_id TEXT NOT NULL,
    observation_date TEXT NOT NULL,
    source_id TEXT NOT NULL,
    value REAL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(series_id, observation_date, source_id),
    FOREIGN KEY (source_id) REFERENCES source_registry(source_id) ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS fact_policy_event (
    policy_event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_date TEXT,
    agency TEXT,
    document_type TEXT,
    title TEXT,
    docket_id TEXT,
    document_id TEXT,
    url TEXT,
    comment_due_date TEXT,
    source_id TEXT,
    payload_json TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (source_id) REFERENCES source_registry(source_id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS fact_news_event (
    news_event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_time_utc TEXT,
    company_id INTEGER,
    ticker TEXT,
    source_name TEXT,
    title TEXT,
    url TEXT,
    tone REAL,
    event_tags TEXT,
    source_id TEXT,
    payload_json TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (company_id) REFERENCES dim_company(company_id) ON DELETE SET NULL,
    FOREIGN KEY (source_id) REFERENCES source_registry(source_id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS feature_fundamental_quality (
    asof_date TEXT NOT NULL,
    company_id INTEGER NOT NULL,
    score REAL,
    payload_json TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(asof_date, company_id),
    FOREIGN KEY (company_id) REFERENCES dim_company(company_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS feature_durable_growth (
    asof_date TEXT NOT NULL,
    company_id INTEGER NOT NULL,
    score REAL,
    payload_json TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(asof_date, company_id),
    FOREIGN KEY (company_id) REFERENCES dim_company(company_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS feature_fda_product_risk (
    asof_date TEXT NOT NULL,
    company_id INTEGER NOT NULL,
    regulatory_innovation_score REAL,
    regulatory_risk_score REAL,
    fda_product_score REAL,
    hard_red_flag INTEGER NOT NULL DEFAULT 0,
    hard_red_flag_reasons TEXT,
    payload_json TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(asof_date, company_id),
    FOREIGN KEY (company_id) REFERENCES dim_company(company_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS feature_reimbursement (
    asof_date TEXT NOT NULL,
    company_id INTEGER NOT NULL,
    score REAL,
    coverage_clarity_score REAL,
    payment_adequacy_score REAL,
    reimbursement_status TEXT,
    direct_code_evidence INTEGER,
    payment_rate_evidence INTEGER,
    coverage_policy_evidence INTEGER,
    procedure_bundled_flag INTEGER,
    capital_equipment_flag INTEGER,
    diagnostics_lab_flag INTEGER,
    unknown_reimbursement_flag INTEGER,
    hard_red_flag INTEGER NOT NULL DEFAULT 0,
    hard_red_flag_reasons TEXT,
    payload_json TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(asof_date, company_id),
    FOREIGN KEY (company_id) REFERENCES dim_company(company_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS feature_valuation (
    asof_date TEXT NOT NULL,
    company_id INTEGER NOT NULL,
    score REAL,
    value_trap_score REAL,
    payload_json TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(asof_date, company_id),
    FOREIGN KEY (company_id) REFERENCES dim_company(company_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS feature_financial_valuation (
    asof_date TEXT NOT NULL,
    company_id INTEGER NOT NULL,
    ticker TEXT NOT NULL,
    company_name TEXT,
    subsector TEXT,
    market_source_id TEXT,
    latest_price_date TEXT,
    latest_close REAL,
    price_staleness_days INTEGER,
    revenue_ttm REAL,
    gross_profit_ttm REAL,
    operating_income_ttm REAL,
    net_income_ttm REAL,
    operating_cash_flow_ttm REAL,
    capital_expenditures_ttm REAL,
    free_cash_flow_ttm REAL,
    research_and_development_ttm REAL,
    annualized_research_and_development REAL,
    interest_expense_ttm REAL,
    revenue_yoy_growth REAL,
    rd_growth_yoy REAL,
    gross_margin_ttm REAL,
    operating_margin_ttm REAL,
    net_margin_ttm REAL,
    fcf_margin_ttm REAL,
    rd_to_revenue_ttm REAL,
    rule_of_40 REAL,
    cash_and_investments REAL,
    total_liquidity REAL,
    latest_quarter_operating_cash_burn REAL,
    annualized_operating_cash_burn REAL,
    financial_runway_years REAL,
    total_debt REAL,
    total_assets REAL,
    stockholders_equity REAL,
    net_debt REAL,
    shares_outstanding REAL,
    current_shares_outstanding REAL,
    diluted_weighted_average_shares REAL,
    basic_weighted_average_shares REAL,
    shares_source_concept TEXT,
    shares_source_form TEXT,
    shares_source_period TEXT,
    market_cap_validated_flag INTEGER,
    shares_yoy_growth REAL,
    market_cap REAL,
    enterprise_value REAL,
    price_to_sales REAL,
    ev_to_sales REAL,
    growth_to_ev_sales REAL,
    fcf_yield REAL,
    net_debt_to_revenue REAL,
    return_on_assets REAL,
    return_on_equity REAL,
    interest_coverage REAL,
    accrual_ratio REAL,
    gross_margin_trend_3y REAL,
    quarterly_revenue_surprise_yoy REAL,
    financial_history_years REAL,
    min_core_group_years REAL,
    data_confidence_score REAL,
    calibration_bucket TEXT,
    ttm_method TEXT,
    data_quality_status TEXT,
    missing_fields TEXT,
    fundamental_quality_score_v1 REAL,
    valuation_score_v1 REAL,
    value_trap_score REAL,
    payload_json TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(asof_date, company_id),
    FOREIGN KEY (company_id) REFERENCES dim_company(company_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS feature_technical_entry (
    asof_date TEXT NOT NULL,
    company_id INTEGER NOT NULL,
    ticker TEXT,
    company_name TEXT,
    subsector TEXT,
    market_source_id TEXT,
    latest_price_date TEXT,
    latest_close REAL,
    avg_dollar_volume_60d REAL,
    return_21d REAL,
    return_63d REAL,
    return_126d REAL,
    return_252d REAL,
    momentum_12_1 REAL,
    relative_strength_63d REAL,
    relative_strength_126d REAL,
    sma_50 REAL,
    sma_200 REAL,
    price_vs_sma_50 REAL,
    price_vs_sma_200 REAL,
    sma_50_vs_200 REAL,
    sma_200_slope_63d REAL,
    rsi_14 REAL,
    atr_14_pct REAL,
    realized_vol_60d REAL,
    max_drawdown_252d REAL,
    pct_from_52w_high REAL,
    volume_trend_ratio REAL,
    technical_score REAL,
    trend_quality_score REAL,
    relative_strength_score REAL,
    liquidity_score REAL,
    volume_breakout_score REAL,
    volatility_risk_score REAL,
    entry_signal TEXT,
    data_quality_status TEXT,
    missing_fields TEXT,
    payload_json TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(asof_date, company_id),
    FOREIGN KEY (company_id) REFERENCES dim_company(company_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS feature_sentiment_catalyst (
    asof_date TEXT NOT NULL,
    company_id INTEGER NOT NULL,
    score REAL,
    estimate_revision_proxy_score REAL,
    event_risk_score REAL,
    payload_json TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(asof_date, company_id),
    FOREIGN KEY (company_id) REFERENCES dim_company(company_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS med_device_daily_scores (
    asof_date TEXT NOT NULL,
    company_id INTEGER NOT NULL,
    scoring_model_version TEXT,
    composite_score REAL,
    raw_composite_score REAL,
    composite_percentile REAL,
    fundamental_quality_score REAL,
    durable_growth_score REAL,
    fda_product_score REAL,
    reimbursement_score REAL,
    reimbursement_status TEXT,
    direct_code_evidence INTEGER,
    payment_rate_evidence INTEGER,
    coverage_policy_evidence INTEGER,
    procedure_bundled_flag INTEGER,
    capital_equipment_flag INTEGER,
    diagnostics_lab_flag INTEGER,
    unknown_reimbursement_flag INTEGER,
    valuation_score REAL,
    technical_entry_score REAL,
    sentiment_catalyst_score REAL,
    value_trap_score REAL,
    data_completeness_score REAL,
    live_component_count INTEGER,
    composite_score_delta REAL,
    rank_delta INTEGER,
    classification_change TEXT,
    rank INTEGER,
    classification TEXT,
    decision_bucket TEXT,
    entry_status TEXT,
    gate_status TEXT,
    review_reason TEXT,
    failed_gates TEXT,
    classification_reason TEXT,
    fda_review_state TEXT,
    market_cap REAL,
    current_shares_outstanding REAL,
    diluted_weighted_average_shares REAL,
    basic_weighted_average_shares REAL,
    shares_source_concept TEXT,
    shares_source_form TEXT,
    shares_source_period TEXT,
    market_cap_validated_flag INTEGER,
    avg_dollar_volume_60d REAL,
    liquidity_score REAL,
    capacity_bucket TEXT,
    min_position_size_feasible REAL,
    max_position_size_feasible REAL,
    passed_raw_score_gate INTEGER,
    passed_fundamental_gate INTEGER,
    passed_growth_gate INTEGER,
    passed_fda_gate INTEGER,
    passed_reimbursement_gate INTEGER,
    passed_valuation_gate INTEGER,
    passed_technical_gate INTEGER,
    passed_value_trap_gate INTEGER,
    passed_data_quality_gate INTEGER,
    passed_liquidity_gate INTEGER,
    passed_fda_manual_review_gate INTEGER,
    final_investability_gate INTEGER,
    hard_red_flag INTEGER NOT NULL DEFAULT 0,
    hard_red_flag_reasons TEXT,
    top_positive_drivers_json TEXT,
    top_negative_drivers_json TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(asof_date, company_id),
    FOREIGN KEY (company_id) REFERENCES dim_company(company_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS data_quality_issues (
    issue_id INTEGER PRIMARY KEY AUTOINCREMENT,
    asof_date TEXT NOT NULL,
    company_id INTEGER,
    source_id TEXT,
    table_name TEXT NOT NULL,
    field_name TEXT,
    issue_type TEXT NOT NULL,
    severity TEXT NOT NULL,
    message TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY (company_id) REFERENCES dim_company(company_id) ON DELETE SET NULL,
    FOREIGN KEY (source_id) REFERENCES source_registry(source_id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_source_registry_stage ON source_registry(stage, priority);
CREATE INDEX IF NOT EXISTS idx_raw_api_responses_source ON raw_api_responses(source_id, request_time_utc);
CREATE INDEX IF NOT EXISTS idx_dim_company_cik ON dim_company(cik);
CREATE INDEX IF NOT EXISTS idx_dim_company_subsector ON dim_company(subsector);
CREATE UNIQUE INDEX IF NOT EXISTS idx_dim_company_alias_unique
ON dim_company_alias(company_id, alias_norm, COALESCE(source_id, 'manual'));
CREATE UNIQUE INDEX IF NOT EXISTS idx_dim_fda_manufacturer_unique
ON dim_fda_manufacturer(manufacturer_name_norm, COALESCE(fei_number, ''));
CREATE UNIQUE INDEX IF NOT EXISTS idx_dim_reimbursement_code_unique
ON dim_reimbursement_code(code_type, code, COALESCE(effective_date, ''));
CREATE INDEX IF NOT EXISTS idx_fact_price_ohlcv_ticker_date ON fact_price_ohlcv(ticker, bar_date);
CREATE INDEX IF NOT EXISTS idx_fact_price_ohlcv_ticker_source_date ON fact_price_ohlcv(ticker, source_id, bar_date DESC);
CREATE INDEX IF NOT EXISTS idx_fact_price_ohlcv_source_date ON fact_price_ohlcv(source_id, bar_date DESC);
CREATE INDEX IF NOT EXISTS idx_fact_fda_recall_number_event
ON fact_fda_recall(COALESCE(recall_number, ''), COALESCE(event_id, ''), source_id);
CREATE INDEX IF NOT EXISTS idx_fact_fda_approval_company_date ON fact_fda_approval(company_id, decision_date);
CREATE INDEX IF NOT EXISTS idx_fact_fda_recall_company_date ON fact_fda_recall(company_id, recall_initiation_date);
CREATE INDEX IF NOT EXISTS idx_fact_fda_recall_canonical_company_date
ON fact_fda_recall_canonical(company_id, recall_initiation_date);
CREATE INDEX IF NOT EXISTS idx_fact_fda_recall_canonical_class_status
ON fact_fda_recall_canonical(classification, is_open, is_terminated);
CREATE INDEX IF NOT EXISTS idx_fact_fda_adverse_company_date ON fact_fda_adverse_event(company_id, report_date);
CREATE UNIQUE INDEX IF NOT EXISTS idx_fact_reimbursement_policy_unique
ON fact_reimbursement_policy(policy_type, COALESCE(policy_id, ''), COALESCE(effective_date, ''));
CREATE INDEX IF NOT EXISTS idx_fact_reimbursement_policy_source
ON fact_reimbursement_policy(source_id, policy_type);
CREATE INDEX IF NOT EXISTS idx_dim_reimbursement_code_code
ON dim_reimbursement_code(code_type, code);
CREATE INDEX IF NOT EXISTS idx_fact_reimbursement_rate_code_source
ON fact_reimbursement_rate(reimbursement_code_id, source_id, effective_date);
CREATE INDEX IF NOT EXISTS idx_map_reimb_policy_company
ON map_company_reimbursement_policy(company_id, confidence);
CREATE INDEX IF NOT EXISTS idx_map_reimb_code_company
ON map_company_reimbursement_code(company_id, confidence);
CREATE UNIQUE INDEX IF NOT EXISTS idx_map_reimb_code_unique
ON map_company_reimbursement_code(company_id, reimbursement_code_id, COALESCE(reimbursement_policy_id, -1));
CREATE UNIQUE INDEX IF NOT EXISTS idx_fact_clinical_trial_status_unique
ON fact_clinical_trial_status(nct_id, COALESCE(company_id, -1));
CREATE INDEX IF NOT EXISTS idx_feature_financial_valuation_asof
ON feature_financial_valuation(asof_date, ticker);
CREATE INDEX IF NOT EXISTS idx_feature_financial_valuation_company_asof
ON feature_financial_valuation(company_id, asof_date DESC);
CREATE INDEX IF NOT EXISTS idx_feature_fda_product_risk_company_asof
ON feature_fda_product_risk(company_id, asof_date DESC);
CREATE INDEX IF NOT EXISTS idx_feature_reimbursement_company_asof
ON feature_reimbursement(company_id, asof_date DESC);
CREATE INDEX IF NOT EXISTS idx_feature_technical_entry_company_asof
ON feature_technical_entry(company_id, asof_date DESC);
CREATE INDEX IF NOT EXISTS idx_feature_durable_growth_company_asof
ON feature_durable_growth(company_id, asof_date DESC);
CREATE INDEX IF NOT EXISTS idx_feature_sentiment_catalyst_company_asof
ON feature_sentiment_catalyst(company_id, asof_date DESC);
CREATE INDEX IF NOT EXISTS idx_scores_asof_rank ON med_device_daily_scores(asof_date, rank);
"""


class ManagedConnection:
    def __init__(self, db_path: Path, *, timeout_sec: float = 30.0) -> None:
        self.db_path = db_path
        self.timeout_sec = timeout_sec
        self._conn: Optional[sqlite3.Connection] = None

    def __enter__(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.db_path, timeout=self.timeout_sec)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        self._conn = conn
        return conn

    def __exit__(self, exc_type, exc, tb) -> None:
        if self._conn is None:
            return
        if exc_type is None:
            self._conn.commit()
        else:
            self._conn.rollback()
        self._conn.close()
        self._conn = None


def quote_identifier(identifier: str) -> str:
    if not SAFE_IDENTIFIER_RE.fullmatch(str(identifier or "")):
        raise ValueError(f"Unsafe SQLite identifier: {identifier!r}")
    return '"' + identifier.replace('"', '""') + '"'


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def connect(db_path: Path, *, timeout_sec: float = 30.0) -> ManagedConnection:
    return ManagedConnection(db_path.expanduser().resolve(), timeout_sec=timeout_sec)


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_SQL)
    conn.execute("PRAGMA foreign_keys = ON")
    _ensure_table_optional_columns(conn, "fact_price_ohlcv", {"price_adjustment": "TEXT"})
    _ensure_table_optional_columns(
        conn,
        "fact_financial_statement",
        {
            "research_and_development": "REAL",
            "interest_expense": "REAL",
            "total_assets": "REAL",
            "stockholders_equity": "REAL",
        },
    )
    _ensure_table_optional_columns(
        conn,
        "feature_financial_valuation",
        {
            "company_name": "TEXT",
            "subsector": "TEXT",
            "research_and_development_ttm": "REAL",
            "annualized_research_and_development": "REAL",
            "interest_expense_ttm": "REAL",
            "rd_growth_yoy": "REAL",
            "rd_to_revenue_ttm": "REAL",
            "rule_of_40": "REAL",
            "total_liquidity": "REAL",
            "latest_quarter_operating_cash_burn": "REAL",
            "annualized_operating_cash_burn": "REAL",
            "financial_runway_years": "REAL",
            "total_assets": "REAL",
            "stockholders_equity": "REAL",
            "current_shares_outstanding": "REAL",
            "diluted_weighted_average_shares": "REAL",
            "basic_weighted_average_shares": "REAL",
            "shares_source_concept": "TEXT",
            "shares_source_form": "TEXT",
            "shares_source_period": "TEXT",
            "market_cap_validated_flag": "INTEGER",
            "shares_yoy_growth": "REAL",
            "growth_to_ev_sales": "REAL",
            "return_on_assets": "REAL",
            "return_on_equity": "REAL",
            "interest_coverage": "REAL",
            "accrual_ratio": "REAL",
            "gross_margin_trend_3y": "REAL",
            "quarterly_revenue_surprise_yoy": "REAL",
            "data_confidence_score": "REAL",
        },
    )
    _ensure_table_optional_columns(
        conn,
        "fact_fda_recall",
        {
            "recall_key": "TEXT",
            "endpoint_name": "TEXT",
            "severity_weight": "REAL",
        },
    )
    _ensure_table_optional_columns(
        conn,
        "feature_reimbursement",
        {
            "ticker": "TEXT",
            "company_name": "TEXT",
            "policy_evidence_count": "INTEGER",
            "company_mention_count": "INTEGER",
            "mapped_product_code_count": "INTEGER",
            "reimbursement_code_count": "INTEGER",
            "rate_row_count": "INTEGER",
            "billing_category": "TEXT",
            "payment_rate_status": "TEXT",
            "primary_payment_file": "TEXT",
            "regional_mac_name": "TEXT",
            "regional_payment_rate": "REAL",
            "regional_rate_status": "TEXT",
            "reimbursement_status": "TEXT",
            "direct_code_evidence": "INTEGER",
            "payment_rate_evidence": "INTEGER",
            "coverage_policy_evidence": "INTEGER",
            "procedure_bundled_flag": "INTEGER",
            "capital_equipment_flag": "INTEGER",
            "diagnostics_lab_flag": "INTEGER",
            "unknown_reimbursement_flag": "INTEGER",
            "hard_red_flag_reasons": "TEXT",
            "review_reason": "TEXT",
        },
    )
    _ensure_table_optional_columns(
        conn,
        "feature_fda_product_risk",
        {
            "fda_data_available": "INTEGER",
            "latest_fda_event_date": "TEXT",
            "mapped_manufacturer_count": "INTEGER",
            "avg_mapping_confidence": "REAL",
            "risk_mapping_confidence_min": "REAL",
            "raw_fda_red_flag": "INTEGER",
            "confirmed_hard_red_flag": "INTEGER",
            "review_adjusted_fda_state": "TEXT",
            "dedup_class_i_recall_count_36m": "INTEGER",
            "open_class_i_recall_count_12m": "INTEGER",
            "open_class_i_recall_count_36m": "INTEGER",
            "terminated_class_i_recall_count_36m": "INTEGER",
            "canonical_recall_duplicate_source_count": "INTEGER",
            "review_reason": "TEXT",
            "clearance_metrics_suppressed": "INTEGER",
            "clearance_metrics_suppression_reason": "TEXT",
            "approval_product_code_filter": "TEXT",
            "approval_product_code_filter_note": "TEXT",
            "fda_evidence_type": "TEXT",
            "regulatory_stage": "TEXT",
            "evidence_confidence": "REAL",
            "next_review_date": "TEXT",
            "manual_evidence_note": "TEXT",
        },
    )
    _ensure_table_optional_columns(
        conn,
        "feature_technical_entry",
        {
            "ticker": "TEXT",
            "company_name": "TEXT",
            "subsector": "TEXT",
            "market_source_id": "TEXT",
            "latest_price_date": "TEXT",
            "latest_close": "REAL",
            "avg_dollar_volume_60d": "REAL",
            "volume_trend_ratio": "REAL",
            "return_21d": "REAL",
            "return_63d": "REAL",
            "return_126d": "REAL",
            "return_252d": "REAL",
            "momentum_12_1": "REAL",
            "relative_strength_63d": "REAL",
            "relative_strength_126d": "REAL",
            "sma_50": "REAL",
            "sma_200": "REAL",
            "price_vs_sma_50": "REAL",
            "price_vs_sma_200": "REAL",
            "sma_50_vs_200": "REAL",
            "sma_200_slope_63d": "REAL",
            "rsi_14": "REAL",
            "atr_14_pct": "REAL",
            "realized_vol_60d": "REAL",
            "max_drawdown_252d": "REAL",
            "pct_from_52w_high": "REAL",
            "technical_score": "REAL",
            "volume_breakout_score": "REAL",
            "volatility_risk_score": "REAL",
            "entry_signal": "TEXT",
            "data_quality_status": "TEXT",
            "missing_fields": "TEXT",
        },
    )
    _ensure_table_optional_columns(
        conn,
        "med_device_daily_scores",
        {
            "scoring_model_version": "TEXT",
            "raw_composite_score": "REAL",
            "composite_percentile": "REAL",
            "value_trap_score": "REAL",
            "data_completeness_score": "REAL",
            "live_component_count": "INTEGER",
            "composite_score_delta": "REAL",
            "rank_delta": "INTEGER",
            "classification_change": "TEXT",
            "decision_bucket": "TEXT",
            "entry_status": "TEXT",
            "gate_status": "TEXT",
            "review_reason": "TEXT",
            "failed_gates": "TEXT",
            "classification_reason": "TEXT",
            "fda_review_state": "TEXT",
            "market_cap": "REAL",
            "current_shares_outstanding": "REAL",
            "diluted_weighted_average_shares": "REAL",
            "basic_weighted_average_shares": "REAL",
            "shares_source_concept": "TEXT",
            "shares_source_form": "TEXT",
            "shares_source_period": "TEXT",
            "market_cap_validated_flag": "INTEGER",
            "avg_dollar_volume_60d": "REAL",
            "liquidity_score": "REAL",
            "capacity_bucket": "TEXT",
            "min_position_size_feasible": "REAL",
            "max_position_size_feasible": "REAL",
            "passed_raw_score_gate": "INTEGER",
            "passed_fundamental_gate": "INTEGER",
            "passed_growth_gate": "INTEGER",
            "passed_fda_gate": "INTEGER",
            "passed_reimbursement_gate": "INTEGER",
            "passed_valuation_gate": "INTEGER",
            "passed_technical_gate": "INTEGER",
            "passed_value_trap_gate": "INTEGER",
            "passed_data_quality_gate": "INTEGER",
            "passed_liquidity_gate": "INTEGER",
            "passed_fda_manual_review_gate": "INTEGER",
            "final_investability_gate": "INTEGER",
            "reimbursement_status": "TEXT",
            "direct_code_evidence": "INTEGER",
            "payment_rate_evidence": "INTEGER",
            "coverage_policy_evidence": "INTEGER",
            "procedure_bundled_flag": "INTEGER",
            "capital_equipment_flag": "INTEGER",
            "diagnostics_lab_flag": "INTEGER",
            "unknown_reimbursement_flag": "INTEGER",
        },
    )
    conn.execute("DROP INDEX IF EXISTS idx_fact_fda_recall_unique")
    conn.execute("DROP INDEX IF EXISTS idx_fact_fda_recall_key")
    conn.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_fact_fda_recall_key_endpoint
        ON fact_fda_recall(recall_key, source_id, COALESCE(endpoint_name, ''))
        WHERE recall_key IS NOT NULL AND recall_key != ''
        """
    )
    conn.execute("PRAGMA user_version = 1")
    conn.commit()


def _table_column_names(conn: sqlite3.Connection, table_name: str) -> set[str]:
    if not SAFE_IDENTIFIER_RE.fullmatch(str(table_name or "")):
        raise ValueError(f"Unsafe SQLite table name: {table_name!r}")
    rows = conn.execute(f"PRAGMA table_info({quote_identifier(table_name)})").fetchall()
    return {str(row["name"] if isinstance(row, sqlite3.Row) else row[1]) for row in rows}


def _ensure_table_optional_columns(conn: sqlite3.Connection, table_name: str, columns: dict[str, str]) -> None:
    existing = _table_column_names(conn, table_name)
    for column, column_type in columns.items():
        if column in existing:
            continue
        if not SAFE_IDENTIFIER_RE.fullmatch(str(column or "")):
            raise ValueError(f"Unsafe SQLite column name: {column!r}")
        normalized_type = str(column_type or "").strip().upper()
        if normalized_type not in {"TEXT", "INTEGER", "REAL", "BLOB", "NUMERIC"}:
            raise ValueError(
                f"Non-standard SQLite column type for {table_name}.{column}: {column_type!r}. "
                "Use TEXT, INTEGER, REAL, BLOB, or NUMERIC."
            )
        conn.execute(f"ALTER TABLE {quote_identifier(table_name)} ADD COLUMN {quote_identifier(column)} {normalized_type}")


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
        raise RuntimeError("Could not create run row; sqlite cursor returned no lastrowid.")
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
