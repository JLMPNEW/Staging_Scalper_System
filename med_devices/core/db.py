from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


SAFE_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
SAFE_COLUMN_TYPE_RE = re.compile(
    r"^(TEXT|INTEGER|REAL|BLOB|NUMERIC)(?:\s+DEFAULT\s+(?:NULL|[-+]?\d+(?:\.\d+)?|'[^']*'))?$",
    re.IGNORECASE,
)


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
    FOREIGN KEY (ingestion_run_id) REFERENCES ingestion_runs(ingestion_run_id) ON DELETE SET NULL
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

CREATE TABLE IF NOT EXISTS dim_company_model_taxonomy (
    company_id INTEGER PRIMARY KEY,
    model_family TEXT NOT NULL DEFAULT 'med_devices',
    ticker TEXT NOT NULL,
    company_name TEXT,
    primary_subsector_raw TEXT,
    calibration_cohort TEXT NOT NULL,
    reimbursement_model TEXT,
    regulatory_model TEXT,
    business_model TEXT,
    procedure_sensitivity TEXT,
    capital_equipment_flag INTEGER NOT NULL DEFAULT 0,
    consumables_flag INTEGER NOT NULL DEFAULT 0,
    diagnostics_flag INTEGER NOT NULL DEFAULT 0,
    implantable_flag INTEGER NOT NULL DEFAULT 0,
    single_product_risk_flag INTEGER NOT NULL DEFAULT 0,
    taxonomy_confidence REAL NOT NULL DEFAULT 0.0,
    taxonomy_source TEXT,
    analyst_reviewed INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (company_id) REFERENCES dim_company(company_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS dim_universe_membership (
    membership_id INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id INTEGER NOT NULL,
    ticker TEXT NOT NULL,
    model_family TEXT NOT NULL DEFAULT 'med_devices',
    membership_source_id TEXT NOT NULL,
    membership_basis TEXT,
    start_date TEXT NOT NULL,
    end_date TEXT,
    membership_status TEXT NOT NULL,
    is_current_member INTEGER NOT NULL DEFAULT 0,
    point_in_time_flag INTEGER NOT NULL DEFAULT 1,
    confidence REAL NOT NULL DEFAULT 0.0,
    reason TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (company_id) REFERENCES dim_company(company_id) ON DELETE CASCADE,
    FOREIGN KEY (membership_source_id) REFERENCES source_registry(source_id) ON DELETE RESTRICT,
    UNIQUE(ticker, model_family, membership_source_id, start_date)
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
    company_id INTEGER,
    short_interest REAL,
    avg_daily_volume REAL,
    days_to_cover REAL,
    float_shares REAL,
    short_interest_pct_float REAL,
    publication_date TEXT,
    payload_json TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(ticker, settlement_date, source_id),
    FOREIGN KEY (company_id) REFERENCES dim_company(company_id) ON DELETE SET NULL,
    FOREIGN KEY (source_id) REFERENCES source_registry(source_id) ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS fact_finra_short_volume (
    ticker TEXT NOT NULL,
    trade_date TEXT NOT NULL,
    source_id TEXT NOT NULL,
    company_id INTEGER,
    short_volume REAL,
    short_exempt_volume REAL,
    total_volume REAL,
    short_volume_ratio REAL,
    market TEXT,
    payload_json TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(ticker, trade_date, source_id),
    FOREIGN KEY (company_id) REFERENCES dim_company(company_id) ON DELETE SET NULL,
    FOREIGN KEY (source_id) REFERENCES source_registry(source_id) ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS fact_ibkr_borrow_snapshot (
    ticker TEXT NOT NULL,
    asof_date TEXT NOT NULL,
    source_id TEXT NOT NULL,
    company_id INTEGER,
    shortable_status REAL,
    shortable_shares REAL,
    borrow_fee_rate REAL,
    source_timestamp TEXT,
    payload_json TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(ticker, asof_date, source_id),
    FOREIGN KEY (company_id) REFERENCES dim_company(company_id) ON DELETE SET NULL,
    FOREIGN KEY (source_id) REFERENCES source_registry(source_id) ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS fact_sec_13f_holding (
    holding_id INTEGER PRIMARY KEY AUTOINCREMENT,
    accession_nodash TEXT NOT NULL,
    report_date TEXT NOT NULL,
    source_id TEXT NOT NULL,
    manager_cik TEXT,
    manager_name TEXT NOT NULL DEFAULT '',
    ticker TEXT NOT NULL,
    company_id INTEGER,
    cusip TEXT,
    shares REAL,
    market_value_usd REAL,
    manager_count REAL,
    institutional_ownership_pct REAL,
    institutional_ownership_delta_pct REAL,
    put_call TEXT,
    investment_discretion TEXT,
    voting_authority_json TEXT,
    payload_json TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (company_id) REFERENCES dim_company(company_id) ON DELETE SET NULL,
    FOREIGN KEY (source_id) REFERENCES source_registry(source_id) ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS fact_sec_form4_transaction (
    accession_nodash TEXT NOT NULL,
    transaction_id TEXT NOT NULL,
    source_id TEXT NOT NULL,
    company_id INTEGER,
    ticker TEXT NOT NULL,
    issuer_cik TEXT,
    reporting_owner_cik TEXT,
    reporting_owner_name TEXT,
    officer_title TEXT,
    is_director INTEGER DEFAULT 0,
    is_officer INTEGER DEFAULT 0,
    is_ten_percent_owner INTEGER DEFAULT 0,
    transaction_date TEXT,
    transaction_code TEXT,
    transaction_shares REAL,
    transaction_price REAL,
    transaction_value_usd REAL,
    direct_or_indirect TEXT,
    post_transaction_shares REAL,
    derivative_flag INTEGER DEFAULT 0,
    payload_json TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(accession_nodash, transaction_id, source_id),
    FOREIGN KEY (company_id) REFERENCES dim_company(company_id) ON DELETE SET NULL,
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
    fda_product_score_legacy REAL,
    fda_alpha_score REAL,
    fda_safety_score REAL,
    fda_clearance_velocity_raw REAL,
    fda_clearance_velocity_score REAL,
    fda_clearance_acceleration_raw REAL,
    fda_clearance_acceleration_score REAL,
    fda_evidence_quality_score REAL,
    fda_event_risk_score REAL,
    fda_event_risk_breadth_adjusted_score REAL DEFAULT 0.0,
    fda_safety_breadth_adjusted_score REAL DEFAULT 50.0,
    fda_distinct_device_category_count INTEGER DEFAULT 0,
    fda_recall_count_raw INTEGER DEFAULT 0,
    fda_recall_count_per_category REAL DEFAULT 0.0,
    fda_class_i_recall_count INTEGER DEFAULT 0,
    fda_warning_letter_count_36m INTEGER DEFAULT 0,
    fda_mdr_death_injury_count_24m INTEGER DEFAULT 0,
    fda_mdr_malfunction_count_24m INTEGER DEFAULT 0,
    fda_mdr_malfunction_count_per_category REAL DEFAULT 0.0,
    fda_breadth_adjustment_applied INTEGER DEFAULT 0,
    fda_signal_mode TEXT,
    fda_signal_direction TEXT,
    fda_signal_reliability REAL,
    fda_policy_reason TEXT,
    calibration_cohort TEXT,
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
    calibration_cohort TEXT,
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
    technical_setup_score REAL,
    technical_core_score REAL,
    technical_alpha_score REAL,
    technical_pullback_score REAL,
    technical_overextension_score REAL,
    technical_breakdown_flag INTEGER DEFAULT 0,
    technical_liquidity_gate_flag INTEGER DEFAULT 0,
    technical_signal_mode TEXT,
    technical_signal_direction TEXT,
    technical_signal_reliability REAL,
    technical_policy_reason TEXT,
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

CREATE TABLE IF NOT EXISTS feature_borrow_risk (
    asof_date TEXT NOT NULL,
    company_id INTEGER NOT NULL,
    ticker TEXT,
    borrow_availability_score REAL DEFAULT 50.0,
    borrow_fee_score REAL DEFAULT 50.0,
    borrow_squeeze_risk_score REAL DEFAULT 50.0,
    borrow_pressure_score REAL DEFAULT 50.0,
    shortable_status REAL,
    shortable_shares REAL,
    borrow_fee_rate REAL,
    data_quality_score REAL DEFAULT 0.0,
    payload_json TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(asof_date, company_id),
    FOREIGN KEY (company_id) REFERENCES dim_company(company_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS feature_short_interest (
    asof_date TEXT NOT NULL,
    company_id INTEGER NOT NULL,
    ticker TEXT,
    short_interest_score REAL DEFAULT 50.0,
    short_pressure_score REAL DEFAULT 50.0,
    short_squeeze_score REAL DEFAULT 50.0,
    short_volume_score REAL DEFAULT 50.0,
    short_interest_velocity_score REAL DEFAULT 50.0,
    days_to_cover_score REAL DEFAULT 50.0,
    short_interest REAL,
    short_interest_pct_float REAL,
    days_to_cover REAL,
    short_volume_ratio_20d REAL,
    short_volume_ratio_delta_20d REAL,
    data_quality_score REAL DEFAULT 0.0,
    payload_json TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(asof_date, company_id),
    FOREIGN KEY (company_id) REFERENCES dim_company(company_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS feature_institutional_flow (
    asof_date TEXT NOT NULL,
    company_id INTEGER NOT NULL,
    ticker TEXT,
    institutional_ownership_delta_pct REAL DEFAULT 0.0,
    institutional_accumulation_score REAL DEFAULT 50.0,
    institutional_crowding_score REAL DEFAULT 50.0,
    institutional_breadth_score REAL DEFAULT 50.0,
    institutional_manager_count REAL,
    institutional_share_count REAL,
    institutional_market_value_usd REAL,
    data_quality_score REAL DEFAULT 0.0,
    payload_json TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(asof_date, company_id),
    FOREIGN KEY (company_id) REFERENCES dim_company(company_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS feature_insider_activity (
    asof_date TEXT NOT NULL,
    company_id INTEGER NOT NULL,
    ticker TEXT,
    insider_net_buy_score REAL DEFAULT 50.0,
    insider_cluster_buy_score REAL DEFAULT 50.0,
    insider_selling_pressure_score REAL DEFAULT 50.0,
    insider_activity_score REAL DEFAULT 50.0,
    net_purchase_value_90d REAL,
    open_market_buy_count_90d INTEGER DEFAULT 0,
    open_market_sell_count_90d INTEGER DEFAULT 0,
    unique_buyer_count_90d INTEGER DEFAULT 0,
    unique_seller_count_90d INTEGER DEFAULT 0,
    data_quality_score REAL DEFAULT 0.0,
    payload_json TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(asof_date, company_id),
    FOREIGN KEY (company_id) REFERENCES dim_company(company_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS med_device_daily_scores (
    asof_date TEXT NOT NULL,
    company_id INTEGER NOT NULL,
    score_model_version TEXT DEFAULT '',
    model_family TEXT DEFAULT '',
    model_version TEXT DEFAULT '',
    scoring_contract_version TEXT DEFAULT '',
    sector TEXT DEFAULT '',
    industry TEXT DEFAULT '',
    country TEXT DEFAULT '',
    currency TEXT DEFAULT '',
    scoring_model_version TEXT,
    composite_score REAL,
    raw_composite_score REAL,
    composite_percentile REAL,
    calibration_cohort TEXT,
    calibration_status TEXT DEFAULT 'production_eligible',
    calibration_status_reason TEXT DEFAULT '',
    calibration_eligible_flag INTEGER DEFAULT 1,
    cohort_score_template_id TEXT DEFAULT '',
    cohort_score_template_spec TEXT DEFAULT '',
    cohort_score_template_tier1_role TEXT DEFAULT '',
    cohort_score_template_tier1_eligible INTEGER DEFAULT 0,
    single_product_risk_flag INTEGER DEFAULT 0,
    binary_event_risk_flag INTEGER DEFAULT 0,
    tier1_safety_status TEXT DEFAULT '',
    tier1_safety_reason TEXT DEFAULT '',
    passed_tier1_safety_gate INTEGER DEFAULT 1,
    portfolio_candidate_gate INTEGER DEFAULT 0,
    portfolio_candidate_status TEXT DEFAULT '',
    portfolio_candidate_reason TEXT DEFAULT '',
    portfolio_candidate_score REAL DEFAULT 0.0,
    analyst_review_decision TEXT DEFAULT '',
    analyst_review_reason TEXT DEFAULT '',
    analyst_review_owner TEXT DEFAULT '',
    analyst_review_expires_at TEXT DEFAULT '',
    analyst_portfolio_override_applied INTEGER DEFAULT 0,
    safe_core_score REAL DEFAULT 0.0,
    safe_core_percentile REAL DEFAULT 0.0,
    safe_core_cohort_percentile REAL DEFAULT 0.0,
    safe_core_rank INTEGER DEFAULT 0,
    safe_core_status TEXT DEFAULT '',
    safe_core_reason TEXT DEFAULT '',
    passed_safe_core_gate INTEGER DEFAULT 0,
    safe_core_model_version TEXT DEFAULT '',
    legacy_all_gates_gate INTEGER DEFAULT 0,
    legacy_gate_misses TEXT DEFAULT '',
    ic_tilted_composite_score REAL DEFAULT 0.0,
    ic_tilted_composite_delta REAL DEFAULT 0.0,
    ic_tilted_composite_mode TEXT DEFAULT '',
    ic_tilted_component_ics_json TEXT DEFAULT '{}',
    cohort_percentile REAL,
    fundamental_quality_score REAL,
    durable_growth_score REAL,
    durable_growth_score_legacy REAL DEFAULT 50.0,
    durable_growth_alpha_score REAL DEFAULT 50.0,
    durable_growth_growth_score REAL DEFAULT 50.0,
    durable_growth_quality_score REAL DEFAULT 50.0,
    durable_growth_efficiency_score REAL DEFAULT 50.0,
    durable_growth_capital_discipline_score REAL DEFAULT 50.0,
    durable_growth_evidence_quality_score REAL DEFAULT 0.0,
    durable_growth_component_count INTEGER DEFAULT 0,
    durable_growth_signal_mode TEXT DEFAULT '',
    durable_growth_signal_direction TEXT DEFAULT '',
    durable_growth_signal_reliability REAL DEFAULT 0.0,
    durable_growth_score_source TEXT DEFAULT '',
    durable_growth_gate_mode TEXT DEFAULT '',
    durable_growth_policy_reason TEXT DEFAULT '',
    durable_growth_gate_excluded INTEGER DEFAULT 0,
    durable_growth_component_weight REAL DEFAULT 0.0,
    durable_growth_repair_flag INTEGER DEFAULT 0,
    durable_growth_repair_reason TEXT DEFAULT '',
    durable_growth_validation_status TEXT DEFAULT '',
    durable_growth_validation_reason TEXT DEFAULT '',
    durable_growth_production_state TEXT DEFAULT '',
    fda_product_score REAL,
    fda_product_score_legacy REAL DEFAULT 0.0,
    fda_alpha_score REAL DEFAULT 0.0,
    fda_safety_score REAL DEFAULT 0.0,
    fda_clearance_velocity_raw REAL DEFAULT 0.0,
    fda_clearance_velocity_score REAL DEFAULT 50.0,
    fda_clearance_acceleration_raw REAL DEFAULT 0.0,
    fda_clearance_acceleration_score REAL DEFAULT 50.0,
    fda_evidence_quality_score REAL DEFAULT 0.0,
    fda_event_risk_score REAL DEFAULT 0.0,
    fda_event_risk_breadth_adjusted_score REAL DEFAULT 0.0,
    fda_safety_breadth_adjusted_score REAL DEFAULT 50.0,
    fda_distinct_device_category_count INTEGER DEFAULT 0,
    fda_recall_count_raw INTEGER DEFAULT 0,
    fda_recall_count_per_category REAL DEFAULT 0.0,
    fda_class_i_recall_count INTEGER DEFAULT 0,
    fda_warning_letter_count_36m INTEGER DEFAULT 0,
    fda_mdr_death_injury_count_24m INTEGER DEFAULT 0,
    fda_mdr_malfunction_count_24m INTEGER DEFAULT 0,
    fda_mdr_malfunction_count_per_category REAL DEFAULT 0.0,
    fda_breadth_adjustment_applied INTEGER DEFAULT 0,
    fda_signal_mode TEXT DEFAULT '',
    fda_signal_direction TEXT DEFAULT '',
    fda_signal_reliability REAL DEFAULT 0.0,
    fda_score_source TEXT DEFAULT '',
    fda_gate_mode TEXT DEFAULT '',
    fda_policy_reason TEXT DEFAULT '',
    fda_gate_excluded INTEGER DEFAULT 0,
    fda_component_weight REAL DEFAULT 0.0,
    fda_data_available INTEGER DEFAULT 0,
    quality_value_interaction_score REAL DEFAULT 50.0,
    fda_technical_interaction_score REAL DEFAULT 50.0,
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
    technical_trend_quality_score REAL DEFAULT 0.0,
    technical_relative_strength_score REAL DEFAULT 0.0,
    technical_liquidity_score REAL DEFAULT 0.0,
    technical_volume_breakout_score REAL DEFAULT 0.0,
    technical_volatility_risk_score REAL DEFAULT 0.0,
    technical_setup_score REAL DEFAULT 0.0,
    technical_core_score REAL DEFAULT 0.0,
    technical_alpha_score REAL DEFAULT 0.0,
    technical_pullback_score REAL DEFAULT 0.0,
    technical_overextension_score REAL DEFAULT 0.0,
    technical_breakdown_flag INTEGER DEFAULT 0,
    technical_liquidity_gate_flag INTEGER DEFAULT 0,
    technical_signal_mode TEXT DEFAULT '',
    technical_signal_direction TEXT DEFAULT '',
    technical_signal_reliability REAL DEFAULT 0.0,
    technical_score_source TEXT DEFAULT '',
    technical_entry_status_score REAL DEFAULT 0.0,
    technical_entry_status_score_source TEXT DEFAULT '',
    borrow_availability_score REAL DEFAULT 50.0,
    borrow_fee_score REAL DEFAULT 50.0,
    borrow_squeeze_risk_score REAL DEFAULT 50.0,
    borrow_pressure_score REAL DEFAULT 50.0,
    borrow_data_quality_score REAL DEFAULT 0.0,
    short_interest_score REAL DEFAULT 50.0,
    short_pressure_score REAL DEFAULT 50.0,
    short_squeeze_score REAL DEFAULT 50.0,
    short_volume_score REAL DEFAULT 50.0,
    short_interest_velocity_score REAL DEFAULT 50.0,
    days_to_cover_score REAL DEFAULT 50.0,
    short_data_quality_score REAL DEFAULT 0.0,
    institutional_accumulation_score REAL DEFAULT 50.0,
    institutional_crowding_score REAL DEFAULT 50.0,
    institutional_breadth_score REAL DEFAULT 50.0,
    institutional_flow_data_quality_score REAL DEFAULT 0.0,
    insider_net_buy_score REAL DEFAULT 50.0,
    insider_cluster_buy_score REAL DEFAULT 50.0,
    insider_selling_pressure_score REAL DEFAULT 50.0,
    insider_activity_score REAL DEFAULT 50.0,
    insider_data_quality_score REAL DEFAULT 0.0,
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
    technical_gate_mode TEXT DEFAULT '',
    technical_overlay_status TEXT DEFAULT '',
    technical_policy_reason TEXT DEFAULT '',
    technical_gate_excluded INTEGER DEFAULT 0,
    technical_component_weight REAL DEFAULT 0.0,
    pullback_candidate_tag INTEGER DEFAULT 0,
    pullback_candidate_reason TEXT DEFAULT '',
    pullback_candidate_template_id TEXT DEFAULT '',
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
    passed_technical_breakdown_veto INTEGER DEFAULT 1,
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
CREATE UNIQUE INDEX IF NOT EXISTS idx_raw_api_responses_run_query
ON raw_api_responses(source_id, endpoint, COALESCE(query_params_json, ''), COALESCE(ingestion_run_id, -1));
CREATE INDEX IF NOT EXISTS idx_dim_company_cik ON dim_company(cik);
CREATE INDEX IF NOT EXISTS idx_dim_company_subsector ON dim_company(subsector);
CREATE UNIQUE INDEX IF NOT EXISTS idx_dim_company_alias_unique
ON dim_company_alias(company_id, alias_norm, COALESCE(source_id, 'manual'));
CREATE INDEX IF NOT EXISTS idx_company_model_taxonomy_cohort
ON dim_company_model_taxonomy(calibration_cohort);
CREATE INDEX IF NOT EXISTS idx_dim_universe_membership_model_dates
ON dim_universe_membership(model_family, start_date, end_date, membership_status);
CREATE INDEX IF NOT EXISTS idx_dim_universe_membership_ticker_model
ON dim_universe_membership(ticker, model_family);
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
        conn.execute("PRAGMA temp_store = MEMORY")
        try:
            conn.execute("PRAGMA journal_mode = WAL")
        except sqlite3.OperationalError as exc:
            if "unable to open database file" not in str(exc).lower():
                raise
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
    _migrate_raw_api_responses_unique(conn)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_raw_api_responses_source ON raw_api_responses(source_id, request_time_utc)")
    conn.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_raw_api_responses_run_query
        ON raw_api_responses(source_id, endpoint, COALESCE(query_params_json, ''), COALESCE(ingestion_run_id, -1))
        """
    )
    _ensure_table_optional_columns(conn, "fact_price_ohlcv", {"price_adjustment": "TEXT"})
    _ensure_table_optional_columns(
        conn,
        "fact_short_interest",
        {
            "company_id": "INTEGER",
            "float_shares": "REAL",
            "short_interest_pct_float": "REAL",
            "publication_date": "TEXT",
            "payload_json": "TEXT",
        },
    )
    _ensure_table_optional_columns(
        conn,
        "fact_sec_13f_holding",
        {
            "manager_count": "REAL",
            "institutional_ownership_pct": "REAL",
            "institutional_ownership_delta_pct": "REAL",
        },
    )
    _ensure_sec_13f_holding_source_identity(conn)
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
        "dim_company_model_taxonomy",
        {
            "model_family": "TEXT DEFAULT 'med_devices'",
            "company_name": "TEXT",
            "primary_subsector_raw": "TEXT",
            "reimbursement_model": "TEXT",
            "regulatory_model": "TEXT",
            "business_model": "TEXT",
            "procedure_sensitivity": "TEXT",
            "capital_equipment_flag": "INTEGER",
            "consumables_flag": "INTEGER",
            "diagnostics_flag": "INTEGER",
            "implantable_flag": "INTEGER",
            "single_product_risk_flag": "INTEGER",
            "taxonomy_confidence": "REAL",
            "taxonomy_source": "TEXT",
            "analyst_reviewed": "INTEGER",
            "updated_at": "TEXT",
        },
    )
    conn.execute(
        """
        UPDATE dim_company_model_taxonomy
        SET model_family = 'med_devices'
        WHERE model_family IS NULL OR TRIM(model_family) = ''
        """
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
            "calibration_cohort": "TEXT",
            "fda_product_score_legacy": "REAL",
            "fda_alpha_score": "REAL",
            "fda_safety_score": "REAL",
            "fda_clearance_velocity_raw": "REAL",
            "fda_clearance_velocity_score": "REAL",
            "fda_clearance_acceleration_raw": "REAL",
            "fda_clearance_acceleration_score": "REAL",
            "fda_evidence_quality_score": "REAL",
            "fda_event_risk_score": "REAL",
            "fda_event_risk_breadth_adjusted_score": "REAL DEFAULT 0.0",
            "fda_safety_breadth_adjusted_score": "REAL DEFAULT 50.0",
            "fda_distinct_device_category_count": "INTEGER DEFAULT 0",
            "fda_recall_count_raw": "INTEGER DEFAULT 0",
            "fda_recall_count_per_category": "REAL DEFAULT 0.0",
            "fda_class_i_recall_count": "INTEGER DEFAULT 0",
            "fda_warning_letter_count_36m": "INTEGER DEFAULT 0",
            "fda_mdr_death_injury_count_24m": "INTEGER DEFAULT 0",
            "fda_mdr_malfunction_count_24m": "INTEGER DEFAULT 0",
            "fda_mdr_malfunction_count_per_category": "REAL DEFAULT 0.0",
            "fda_breadth_adjustment_applied": "INTEGER DEFAULT 0",
            "fda_signal_mode": "TEXT",
            "fda_signal_direction": "TEXT",
            "fda_signal_reliability": "REAL",
            "fda_policy_reason": "TEXT",
            "fda_data_available": "INTEGER",
            "latest_fda_event_date": "TEXT",
            "mapped_manufacturer_count": "INTEGER",
            "avg_mapping_confidence": "REAL",
            "risk_mapping_confidence_min": "REAL",
            "raw_fda_red_flag": "INTEGER",
            "confirmed_hard_red_flag": "INTEGER",
            "review_adjusted_fda_state": "TEXT",
            "dedup_class_i_recall_count_36m": "INTEGER",
            "class_i_multi_source_recall_count_36m": "INTEGER",
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
            "calibration_cohort": "TEXT",
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
            "technical_setup_score": "REAL",
            "technical_core_score": "REAL",
            "technical_alpha_score": "REAL",
            "technical_pullback_score": "REAL",
            "technical_overextension_score": "REAL",
            "technical_breakdown_flag": "INTEGER",
            "technical_liquidity_gate_flag": "INTEGER",
            "technical_signal_mode": "TEXT",
            "technical_signal_direction": "TEXT",
            "technical_signal_reliability": "REAL",
            "technical_policy_reason": "TEXT",
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
            "calibration_cohort": "TEXT",
            "calibration_status": "TEXT",
            "calibration_status_reason": "TEXT",
            "calibration_eligible_flag": "INTEGER",
            "cohort_score_template_id": "TEXT",
            "cohort_score_template_spec": "TEXT",
            "cohort_score_template_tier1_role": "TEXT",
            "cohort_score_template_tier1_eligible": "INTEGER",
            "single_product_risk_flag": "INTEGER",
            "binary_event_risk_flag": "INTEGER",
            "tier1_safety_status": "TEXT",
            "tier1_safety_reason": "TEXT",
            "passed_tier1_safety_gate": "INTEGER",
            "portfolio_candidate_gate": "INTEGER",
            "portfolio_candidate_status": "TEXT",
            "portfolio_candidate_reason": "TEXT",
            "portfolio_candidate_score": "REAL",
            "analyst_review_decision": "TEXT",
            "analyst_review_reason": "TEXT",
            "analyst_review_owner": "TEXT",
            "analyst_review_expires_at": "TEXT",
            "analyst_portfolio_override_applied": "INTEGER",
            "safe_core_score": "REAL",
            "safe_core_percentile": "REAL",
            "safe_core_cohort_percentile": "REAL",
            "safe_core_rank": "INTEGER",
            "safe_core_status": "TEXT",
            "safe_core_reason": "TEXT",
            "passed_safe_core_gate": "INTEGER",
            "safe_core_model_version": "TEXT",
            "legacy_all_gates_gate": "INTEGER",
            "legacy_gate_misses": "TEXT",
            "ic_tilted_composite_score": "REAL",
            "ic_tilted_composite_delta": "REAL",
            "ic_tilted_composite_mode": "TEXT",
            "ic_tilted_component_ics_json": "TEXT",
            "cohort_percentile": "REAL",
            "durable_growth_score_legacy": "REAL",
            "durable_growth_alpha_score": "REAL",
            "durable_growth_growth_score": "REAL",
            "durable_growth_quality_score": "REAL",
            "durable_growth_efficiency_score": "REAL",
            "durable_growth_capital_discipline_score": "REAL",
            "durable_growth_evidence_quality_score": "REAL",
            "durable_growth_component_count": "INTEGER",
            "durable_growth_signal_mode": "TEXT",
            "durable_growth_signal_direction": "TEXT",
            "durable_growth_signal_reliability": "REAL",
            "durable_growth_score_source": "TEXT",
            "durable_growth_gate_mode": "TEXT",
            "durable_growth_policy_reason": "TEXT",
            "durable_growth_gate_excluded": "INTEGER",
            "durable_growth_component_weight": "REAL",
            "durable_growth_repair_flag": "INTEGER",
            "durable_growth_repair_reason": "TEXT",
            "durable_growth_validation_status": "TEXT",
            "durable_growth_validation_reason": "TEXT",
            "durable_growth_production_state": "TEXT",
            "value_trap_score": "REAL",
            "data_completeness_score": "REAL",
            "live_component_count": "INTEGER",
            "composite_score_delta": "REAL",
            "rank_delta": "INTEGER",
            "classification_change": "TEXT",
            "decision_bucket": "TEXT",
            "entry_status": "TEXT",
            "fda_product_score_legacy": "REAL",
            "fda_alpha_score": "REAL",
            "fda_safety_score": "REAL",
            "fda_clearance_velocity_raw": "REAL",
            "fda_clearance_velocity_score": "REAL",
            "fda_clearance_acceleration_raw": "REAL",
            "fda_clearance_acceleration_score": "REAL",
            "fda_evidence_quality_score": "REAL",
            "fda_event_risk_score": "REAL",
            "fda_event_risk_breadth_adjusted_score": "REAL DEFAULT 0.0",
            "fda_safety_breadth_adjusted_score": "REAL DEFAULT 50.0",
            "fda_distinct_device_category_count": "INTEGER DEFAULT 0",
            "fda_recall_count_raw": "INTEGER DEFAULT 0",
            "fda_recall_count_per_category": "REAL DEFAULT 0.0",
            "fda_class_i_recall_count": "INTEGER DEFAULT 0",
            "fda_warning_letter_count_36m": "INTEGER DEFAULT 0",
            "fda_mdr_death_injury_count_24m": "INTEGER DEFAULT 0",
            "fda_mdr_malfunction_count_24m": "INTEGER DEFAULT 0",
            "fda_mdr_malfunction_count_per_category": "REAL DEFAULT 0.0",
            "fda_breadth_adjustment_applied": "INTEGER DEFAULT 0",
            "fda_signal_mode": "TEXT",
            "fda_signal_direction": "TEXT",
            "fda_signal_reliability": "REAL",
            "fda_score_source": "TEXT",
            "fda_gate_mode": "TEXT",
            "fda_policy_reason": "TEXT",
            "fda_gate_excluded": "INTEGER",
            "fda_component_weight": "REAL",
            "fda_data_available": "INTEGER",
            "quality_value_interaction_score": "REAL",
            "fda_technical_interaction_score": "REAL",
            "technical_gate_mode": "TEXT",
            "technical_overlay_status": "TEXT",
            "technical_policy_reason": "TEXT",
            "technical_gate_excluded": "INTEGER",
            "technical_component_weight": "REAL",
            "technical_trend_quality_score": "REAL",
            "technical_relative_strength_score": "REAL",
            "technical_liquidity_score": "REAL",
            "technical_volume_breakout_score": "REAL",
            "technical_volatility_risk_score": "REAL",
            "technical_setup_score": "REAL",
            "technical_core_score": "REAL",
            "technical_alpha_score": "REAL",
            "technical_pullback_score": "REAL",
            "technical_overextension_score": "REAL",
            "technical_breakdown_flag": "INTEGER",
            "technical_liquidity_gate_flag": "INTEGER",
            "technical_signal_mode": "TEXT",
            "technical_signal_direction": "TEXT",
            "technical_signal_reliability": "REAL",
            "technical_score_source": "TEXT",
            "technical_entry_status_score": "REAL",
            "technical_entry_status_score_source": "TEXT",
            "borrow_availability_score": "REAL",
            "borrow_fee_score": "REAL",
            "borrow_squeeze_risk_score": "REAL",
            "borrow_pressure_score": "REAL",
            "borrow_data_quality_score": "REAL",
            "short_interest_score": "REAL",
            "short_pressure_score": "REAL",
            "short_squeeze_score": "REAL",
            "short_volume_score": "REAL",
            "short_interest_velocity_score": "REAL",
            "days_to_cover_score": "REAL",
            "short_data_quality_score": "REAL",
            "institutional_accumulation_score": "REAL",
            "institutional_crowding_score": "REAL",
            "institutional_breadth_score": "REAL",
            "institutional_flow_data_quality_score": "REAL",
            "insider_net_buy_score": "REAL",
            "insider_cluster_buy_score": "REAL",
            "insider_selling_pressure_score": "REAL",
            "insider_activity_score": "REAL",
            "insider_data_quality_score": "REAL",
            "pullback_candidate_tag": "INTEGER",
            "pullback_candidate_reason": "TEXT",
            "pullback_candidate_template_id": "TEXT",
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
            "passed_technical_breakdown_veto": "INTEGER",
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
    conn.execute(
        """
        UPDATE med_device_daily_scores
        SET calibration_eligible_flag = CASE
            WHEN LOWER(COALESCE(calibration_status, 'production_eligible')) = 'production_eligible' THEN 1
            ELSE 0
        END
        WHERE calibration_eligible_flag IS NULL
        """
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
    conn.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_fact_sec_13f_holding_identity
        ON fact_sec_13f_holding(
            accession_nodash,
            ticker,
            COALESCE(cusip, ''),
            COALESCE(put_call, ''),
            source_id
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_fact_finra_short_volume_company_date ON fact_finra_short_volume(company_id, trade_date)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_fact_ibkr_borrow_company_date ON fact_ibkr_borrow_snapshot(company_id, asof_date)"
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_fact_13f_holding_company_date ON fact_sec_13f_holding(company_id, report_date)")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_fact_form4_transaction_company_date ON fact_sec_form4_transaction(company_id, transaction_date)"
    )
    conn.execute("PRAGMA user_version = 1")
    conn.commit()


def _migrate_raw_api_responses_unique(conn: sqlite3.Connection) -> None:
    row = conn.execute(
        """
        SELECT sql
        FROM sqlite_master
        WHERE type = 'table'
          AND name = 'raw_api_responses'
        """
    ).fetchone()
    table_sql = str(row["sql"] if isinstance(row, sqlite3.Row) else row[0]) if row is not None else ""
    if "UNIQUE(source_id, endpoint, response_hash)" not in table_sql:
        return

    conn.execute("ALTER TABLE raw_api_responses RENAME TO raw_api_responses_legacy_unique")
    conn.execute(
        """
        CREATE TABLE raw_api_responses (
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
            FOREIGN KEY (ingestion_run_id) REFERENCES ingestion_runs(ingestion_run_id) ON DELETE SET NULL
        )
        """
    )
    conn.execute(
        """
        INSERT OR IGNORE INTO raw_api_responses(
            raw_response_id, source_id, endpoint, query_params_json, request_time_utc,
            response_status, response_hash, asof_date, payload_text, ingestion_run_id, created_at
        )
        SELECT
            raw_response_id, source_id, endpoint, query_params_json, request_time_utc,
            response_status, response_hash, asof_date, payload_text, ingestion_run_id, created_at
        FROM raw_api_responses_legacy_unique
        WHERE raw_response_id IN (
            SELECT MIN(raw_response_id)
            FROM raw_api_responses_legacy_unique
            GROUP BY source_id, endpoint, COALESCE(query_params_json, ''), COALESCE(ingestion_run_id, -1)
        )
        """
    )
    conn.execute("DROP TABLE raw_api_responses_legacy_unique")


def _ensure_sec_13f_holding_source_identity(conn: sqlite3.Connection) -> None:
    """Make SEC 13F upserts deterministic across CSV and aggregate rebuilds."""
    conn.execute(
        """
        UPDATE fact_sec_13f_holding
        SET manager_name = ''
        WHERE manager_name IS NULL
        """
    )
    conn.execute(
        """
        DELETE FROM fact_sec_13f_holding
        WHERE holding_id NOT IN (
            SELECT MAX(holding_id)
            FROM fact_sec_13f_holding
            GROUP BY accession_nodash, report_date, source_id, manager_name, ticker
        )
        """
    )
    conn.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_fact_sec_13f_holding_source_identity
        ON fact_sec_13f_holding(accession_nodash, report_date, source_id, manager_name, ticker)
        """
    )


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
        normalized_type = str(column_type or "").strip()
        if not SAFE_COLUMN_TYPE_RE.fullmatch(normalized_type):
            raise ValueError(
                f"Non-standard SQLite column type for {table_name}.{column}: {column_type!r}. "
                "Use TEXT, INTEGER, REAL, BLOB, NUMERIC, optionally with a literal DEFAULT."
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
