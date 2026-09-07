"""SQLite ownership, migrations, and run bookkeeping for Basic Materials."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Any, Iterable
import uuid

from basic_materials import MODEL_FAMILY, SECTOR


SCHEMA_OWNER = MODEL_FAMILY
SCHEMA_VERSION = 8


class DatabaseIdentityError(RuntimeError):
    """Raised when a database is not exclusively owned by Basic Materials."""


class MigrationError(RuntimeError):
    """Raised when the migration ledger does not match package code."""


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


FOUNDATION_SQL = r"""
CREATE TABLE IF NOT EXISTS sector_database_identity (
    identity_id INTEGER PRIMARY KEY CHECK (identity_id = 1),
    model_family TEXT NOT NULL CHECK (model_family = 'basic_materials'),
    sector TEXT NOT NULL CHECK (sector = 'Basic Materials'),
    schema_owner TEXT NOT NULL CHECK (schema_owner = 'basic_materials'),
    schema_version INTEGER NOT NULL CHECK (schema_version >= 1),
    created_at_utc TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    checksum TEXT NOT NULL,
    applied_at_utc TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS model_control_state (
    identity_id INTEGER PRIMARY KEY CHECK (identity_id = 1),
    promotion_state TEXT NOT NULL CHECK (promotion_state = 'shadow_monitor'),
    portfolio_candidate_gate INTEGER NOT NULL CHECK (portfolio_candidate_gate = 0),
    oos_score_valid_flag INTEGER NOT NULL CHECK (oos_score_valid_flag = 0),
    current_universe_is_survivorship_corrected INTEGER NOT NULL
        CHECK (current_universe_is_survivorship_corrected = 0),
    current_universe_calibration_eligible INTEGER NOT NULL
        CHECK (current_universe_calibration_eligible = 0),
    updated_at_utc TEXT NOT NULL,
    FOREIGN KEY (identity_id) REFERENCES sector_database_identity(identity_id)
);

CREATE TABLE IF NOT EXISTS source_registry (
    source_id TEXT PRIMARY KEY,
    stage TEXT NOT NULL,
    provider TEXT NOT NULL,
    category TEXT NOT NULL,
    description TEXT NOT NULL,
    point_in_time_role TEXT NOT NULL,
    active INTEGER NOT NULL CHECK (active IN (0, 1)),
    registry_version TEXT NOT NULL,
    registry_checksum TEXT NOT NULL,
    loaded_at_utc TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS pipeline_runs (
    run_id TEXT PRIMARY KEY,
    stage TEXT NOT NULL,
    command TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('running', 'succeeded', 'failed')),
    database_path TEXT NOT NULL,
    input_path TEXT,
    input_sha256 TEXT,
    input_row_count INTEGER,
    started_at_utc TEXT NOT NULL,
    completed_at_utc TEXT,
    details_json TEXT NOT NULL DEFAULT '{}',
    error_message TEXT
);

CREATE TABLE IF NOT EXISTS raw_source_payloads (
    snapshot_id TEXT PRIMARY KEY,
    source_id TEXT NOT NULL,
    source_snapshot_date TEXT NOT NULL,
    source_path TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    byte_size INTEGER NOT NULL CHECK (byte_size >= 0),
    row_count INTEGER NOT NULL CHECK (row_count >= 0),
    media_type TEXT NOT NULL,
    payload BLOB NOT NULL,
    manifest_version TEXT NOT NULL,
    ingested_at_utc TEXT NOT NULL,
    UNIQUE (source_id, sha256),
    FOREIGN KEY (source_id) REFERENCES source_registry(source_id)
);

CREATE TABLE IF NOT EXISTS dim_company (
    company_id INTEGER PRIMARY KEY AUTOINCREMENT,
    cik TEXT NOT NULL UNIQUE CHECK (length(cik) = 10 AND cik NOT GLOB '*[^0-9]*'),
    legal_name TEXT NOT NULL,
    primary_ticker TEXT NOT NULL COLLATE NOCASE,
    domicile_country TEXT NOT NULL,
    universe_status TEXT NOT NULL,
    is_active INTEGER NOT NULL CHECK (is_active IN (0, 1)),
    first_seen_date TEXT NOT NULL,
    last_seen_date TEXT NOT NULL,
    created_at_utc TEXT NOT NULL,
    updated_at_utc TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS dim_security (
    security_id INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id INTEGER NOT NULL,
    ticker TEXT NOT NULL COLLATE NOCASE UNIQUE,
    exchange TEXT NOT NULL,
    trading_currency TEXT NOT NULL,
    security_type TEXT NOT NULL,
    listing_status TEXT NOT NULL,
    is_primary_listing INTEGER NOT NULL CHECK (is_primary_listing IN (0, 1)),
    source_id TEXT NOT NULL,
    valid_from_date TEXT NOT NULL,
    valid_to_date TEXT,
    created_at_utc TEXT NOT NULL,
    updated_at_utc TEXT NOT NULL,
    FOREIGN KEY (company_id) REFERENCES dim_company(company_id),
    FOREIGN KEY (source_id) REFERENCES source_registry(source_id),
    CHECK (valid_to_date IS NULL OR valid_to_date >= valid_from_date)
);

CREATE TABLE IF NOT EXISTS dim_identifier (
    identifier_id INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id INTEGER NOT NULL,
    security_id INTEGER,
    identifier_type TEXT NOT NULL,
    identifier_value TEXT NOT NULL,
    is_primary INTEGER NOT NULL CHECK (is_primary IN (0, 1)),
    valid_from_date TEXT NOT NULL,
    valid_to_date TEXT,
    source_id TEXT NOT NULL,
    created_at_utc TEXT NOT NULL,
    UNIQUE (identifier_type, identifier_value, valid_from_date),
    FOREIGN KEY (company_id) REFERENCES dim_company(company_id),
    FOREIGN KEY (security_id) REFERENCES dim_security(security_id),
    FOREIGN KEY (source_id) REFERENCES source_registry(source_id),
    CHECK (valid_to_date IS NULL OR valid_to_date >= valid_from_date)
);

CREATE TABLE IF NOT EXISTS dim_basic_materials_taxonomy (
    taxonomy_id INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id INTEGER NOT NULL,
    security_id INTEGER NOT NULL UNIQUE,
    ticker TEXT NOT NULL COLLATE NOCASE UNIQUE,
    sector TEXT NOT NULL CHECK (sector = 'Basic Materials'),
    industry TEXT NOT NULL,
    cohort_id TEXT NOT NULL,
    calibration_group TEXT NOT NULL,
    calibration_parent TEXT NOT NULL,
    lifecycle_state TEXT NOT NULL,
    classification_confidence REAL NOT NULL
        CHECK (classification_confidence >= 0.0 AND classification_confidence <= 1.0),
    source_id TEXT NOT NULL,
    policy_version TEXT NOT NULL,
    input_sha256 TEXT NOT NULL,
    created_at_utc TEXT NOT NULL,
    updated_at_utc TEXT NOT NULL,
    FOREIGN KEY (company_id) REFERENCES dim_company(company_id),
    FOREIGN KEY (security_id) REFERENCES dim_security(security_id),
    FOREIGN KEY (source_id) REFERENCES source_registry(source_id)
);

CREATE TABLE IF NOT EXISTS dim_universe_membership (
    membership_id INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id INTEGER NOT NULL,
    security_id INTEGER NOT NULL,
    ticker TEXT NOT NULL COLLATE NOCASE,
    model_family TEXT NOT NULL CHECK (model_family = 'basic_materials'),
    cohort_id TEXT NOT NULL,
    membership_start_date TEXT NOT NULL,
    membership_end_date TEXT,
    membership_status TEXT NOT NULL CHECK (membership_status IN ('current', 'historical')),
    membership_source_id TEXT NOT NULL,
    membership_basis TEXT NOT NULL,
    current_source_only INTEGER NOT NULL CHECK (current_source_only IN (0, 1)),
    survivorship_corrected INTEGER NOT NULL CHECK (survivorship_corrected IN (0, 1)),
    calibration_eligible INTEGER NOT NULL CHECK (calibration_eligible IN (0, 1)),
    membership_confidence REAL NOT NULL
        CHECK (membership_confidence >= 0.0 AND membership_confidence <= 1.0),
    source_snapshot_date TEXT NOT NULL,
    policy_version TEXT NOT NULL,
    input_sha256 TEXT NOT NULL,
    created_at_utc TEXT NOT NULL,
    updated_at_utc TEXT NOT NULL,
    UNIQUE (security_id, membership_source_id, membership_start_date),
    FOREIGN KEY (company_id) REFERENCES dim_company(company_id),
    FOREIGN KEY (security_id) REFERENCES dim_security(security_id),
    FOREIGN KEY (membership_source_id) REFERENCES source_registry(source_id),
    CHECK (membership_end_date IS NULL OR membership_end_date >= membership_start_date),
    CHECK (NOT (calibration_eligible = 1 AND survivorship_corrected = 0))
);

CREATE TABLE IF NOT EXISTS fact_security_event (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id INTEGER NOT NULL,
    security_id INTEGER,
    event_type TEXT NOT NULL,
    effective_date TEXT NOT NULL,
    old_value TEXT,
    new_value TEXT,
    source_id TEXT NOT NULL,
    evidence_json TEXT NOT NULL DEFAULT '{}',
    reviewed INTEGER NOT NULL DEFAULT 0 CHECK (reviewed IN (0, 1)),
    created_at_utc TEXT NOT NULL,
    FOREIGN KEY (company_id) REFERENCES dim_company(company_id),
    FOREIGN KEY (security_id) REFERENCES dim_security(security_id),
    FOREIGN KEY (source_id) REFERENCES source_registry(source_id)
);

CREATE TABLE IF NOT EXISTS fact_terminal_event_reconciliation (
    reconciliation_id INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id INTEGER NOT NULL,
    security_id INTEGER,
    event_date TEXT NOT NULL,
    terminal_event_type TEXT NOT NULL,
    return_treatment TEXT NOT NULL,
    resolved INTEGER NOT NULL DEFAULT 0 CHECK (resolved IN (0, 1)),
    source_id TEXT NOT NULL,
    evidence_json TEXT NOT NULL DEFAULT '{}',
    created_at_utc TEXT NOT NULL,
    FOREIGN KEY (company_id) REFERENCES dim_company(company_id),
    FOREIGN KEY (security_id) REFERENCES dim_security(security_id),
    FOREIGN KEY (source_id) REFERENCES source_registry(source_id)
);

CREATE TABLE IF NOT EXISTS data_quality_issues (
    issue_id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT,
    stage TEXT NOT NULL,
    severity TEXT NOT NULL CHECK (severity IN ('info', 'warning', 'error')),
    issue_code TEXT NOT NULL,
    ticker TEXT COLLATE NOCASE,
    message TEXT NOT NULL,
    details_json TEXT NOT NULL DEFAULT '{}',
    resolved INTEGER NOT NULL DEFAULT 0 CHECK (resolved IN (0, 1)),
    created_at_utc TEXT NOT NULL,
    FOREIGN KEY (run_id) REFERENCES pipeline_runs(run_id)
);

CREATE TABLE IF NOT EXISTS artifact_manifest (
    artifact_id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT,
    stage TEXT NOT NULL,
    artifact_path TEXT NOT NULL,
    artifact_type TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    row_count INTEGER,
    created_at_utc TEXT NOT NULL,
    UNIQUE (artifact_path, sha256),
    FOREIGN KEY (run_id) REFERENCES pipeline_runs(run_id)
);

CREATE INDEX IF NOT EXISTS idx_company_active ON dim_company(is_active, primary_ticker);
CREATE INDEX IF NOT EXISTS idx_security_company ON dim_security(company_id);
CREATE INDEX IF NOT EXISTS idx_taxonomy_cohort ON dim_basic_materials_taxonomy(cohort_id, ticker);
CREATE INDEX IF NOT EXISTS idx_membership_current
    ON dim_universe_membership(membership_status, membership_end_date, cohort_id, ticker);
CREATE INDEX IF NOT EXISTS idx_membership_dates
    ON dim_universe_membership(membership_start_date, membership_end_date);
CREATE INDEX IF NOT EXISTS idx_quality_stage_severity ON data_quality_issues(stage, severity, resolved);
"""


HISTORICAL_RECONCILIATION_SQL = r"""
CREATE TABLE IF NOT EXISTS dim_ticker_alias (
    alias_id INTEGER PRIMARY KEY AUTOINCREMENT,
    alias_key TEXT NOT NULL UNIQUE,
    company_id INTEGER NOT NULL,
    security_id INTEGER NOT NULL,
    alias_ticker TEXT NOT NULL COLLATE NOCASE,
    canonical_ticker TEXT NOT NULL COLLATE NOCASE,
    security_scope TEXT NOT NULL,
    relationship TEXT NOT NULL,
    valid_from_date TEXT NOT NULL,
    valid_to_date TEXT,
    provider_history_owner TEXT NOT NULL,
    provider_asset_id TEXT,
    load_as_separate_security INTEGER NOT NULL CHECK (load_as_separate_security IN (0, 1)),
    source_id TEXT NOT NULL,
    evidence_json TEXT NOT NULL DEFAULT '{}',
    reviewed INTEGER NOT NULL DEFAULT 0 CHECK (reviewed IN (0, 1)),
    created_at_utc TEXT NOT NULL,
    updated_at_utc TEXT NOT NULL,
    FOREIGN KEY (company_id) REFERENCES dim_company(company_id),
    FOREIGN KEY (security_id) REFERENCES dim_security(security_id),
    FOREIGN KEY (source_id) REFERENCES source_registry(source_id),
    CHECK (valid_to_date IS NULL OR valid_to_date >= valid_from_date)
);

ALTER TABLE fact_security_event ADD COLUMN event_key TEXT;
ALTER TABLE fact_terminal_event_reconciliation ADD COLUMN event_key TEXT;

CREATE UNIQUE INDEX IF NOT EXISTS idx_ticker_alias_key ON dim_ticker_alias(alias_key);
CREATE INDEX IF NOT EXISTS idx_ticker_alias_lookup
    ON dim_ticker_alias(alias_ticker, valid_from_date, valid_to_date, security_scope);
CREATE UNIQUE INDEX IF NOT EXISTS idx_security_event_key ON fact_security_event(event_key);
CREATE UNIQUE INDEX IF NOT EXISTS idx_terminal_event_key ON fact_terminal_event_reconciliation(event_key);
"""


MARKET_DATA_SQL = r"""
CREATE TABLE IF NOT EXISTS dim_market_instrument (
    instrument_id INTEGER PRIMARY KEY AUTOINCREMENT,
    instrument_key TEXT NOT NULL UNIQUE,
    provider_source_id TEXT NOT NULL,
    provider_asset_id TEXT NOT NULL,
    provider_symbol TEXT NOT NULL COLLATE NOCASE,
    canonical_ticker TEXT NOT NULL COLLATE NOCASE,
    provider_database TEXT NOT NULL,
    trading_currency TEXT NOT NULL,
    provider_first_quoted_date TEXT NOT NULL,
    provider_last_quoted_date TEXT,
    adjustment_basis TEXT NOT NULL CHECK (adjustment_basis = 'norgate_total_return'),
    contract_version TEXT NOT NULL,
    contract_sha256 TEXT NOT NULL,
    created_at_utc TEXT NOT NULL,
    updated_at_utc TEXT NOT NULL,
    UNIQUE (provider_source_id, provider_asset_id),
    FOREIGN KEY (provider_source_id) REFERENCES source_registry(source_id),
    CHECK (provider_asset_id NOT GLOB '*[^0-9]*'),
    CHECK (provider_last_quoted_date IS NULL OR provider_last_quoted_date >= provider_first_quoted_date)
);

CREATE TABLE IF NOT EXISTS bridge_market_instrument_role (
    role_key TEXT PRIMARY KEY,
    instrument_id INTEGER NOT NULL,
    security_id INTEGER,
    event_key TEXT,
    role_type TEXT NOT NULL CHECK (
        role_type IN ('current_universe', 'historical_pilot', 'sector_benchmark',
                      'broad_benchmark', 'terminal_successor')
    ),
    model_ticker TEXT NOT NULL COLLATE NOCASE,
    security_scope TEXT NOT NULL,
    expected_start_date TEXT NOT NULL,
    expected_end_date TEXT,
    required_for_stage3 INTEGER NOT NULL CHECK (required_for_stage3 IN (0, 1)),
    required_for_current_gate INTEGER NOT NULL CHECK (required_for_current_gate IN (0, 1)),
    source_id TEXT NOT NULL,
    contract_version TEXT NOT NULL,
    contract_sha256 TEXT NOT NULL,
    created_at_utc TEXT NOT NULL,
    updated_at_utc TEXT NOT NULL,
    FOREIGN KEY (instrument_id) REFERENCES dim_market_instrument(instrument_id),
    FOREIGN KEY (security_id) REFERENCES dim_security(security_id),
    FOREIGN KEY (event_key) REFERENCES fact_terminal_event_reconciliation(event_key),
    FOREIGN KEY (source_id) REFERENCES source_registry(source_id),
    CHECK (expected_end_date IS NULL OR expected_end_date >= expected_start_date),
    CHECK ((role_type IN ('current_universe', 'historical_pilot') AND security_id IS NOT NULL)
           OR role_type NOT IN ('current_universe', 'historical_pilot')),
    CHECK ((role_type = 'terminal_successor' AND event_key IS NOT NULL)
           OR role_type <> 'terminal_successor')
);

CREATE TABLE IF NOT EXISTS fact_market_provider_snapshot (
    snapshot_key TEXT PRIMARY KEY,
    provider_source_id TEXT NOT NULL,
    extraction_asof_date TEXT NOT NULL,
    database_fingerprint_json TEXT NOT NULL,
    contract_manifest_sha256 TEXT NOT NULL,
    raw_manifest_sha256 TEXT NOT NULL,
    instrument_count INTEGER NOT NULL CHECK (instrument_count >= 0),
    bar_count INTEGER NOT NULL CHECK (bar_count >= 0),
    cache_root TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('loaded', 'partial', 'failed')),
    created_at_utc TEXT NOT NULL,
    FOREIGN KEY (provider_source_id) REFERENCES source_registry(source_id)
);

CREATE TABLE IF NOT EXISTS fact_adjusted_price_bar (
    instrument_id INTEGER NOT NULL,
    bar_date TEXT NOT NULL,
    provider_source_id TEXT NOT NULL,
    open REAL,
    high REAL,
    low REAL,
    close REAL NOT NULL CHECK (close > 0),
    adjusted_close REAL NOT NULL CHECK (adjusted_close > 0),
    volume REAL CHECK (volume IS NULL OR volume >= 0),
    dividend REAL CHECK (dividend IS NULL OR dividend >= 0),
    capital_event INTEGER NOT NULL DEFAULT 0 CHECK (capital_event IN (0, 1)),
    adjustment_basis TEXT NOT NULL CHECK (adjustment_basis = 'norgate_total_return'),
    snapshot_key TEXT NOT NULL,
    payload_sha256 TEXT NOT NULL,
    source_timestamp_utc TEXT NOT NULL,
    created_at_utc TEXT NOT NULL,
    updated_at_utc TEXT NOT NULL,
    PRIMARY KEY (instrument_id, bar_date, provider_source_id),
    FOREIGN KEY (instrument_id) REFERENCES dim_market_instrument(instrument_id),
    FOREIGN KEY (provider_source_id) REFERENCES source_registry(source_id),
    FOREIGN KEY (snapshot_key) REFERENCES fact_market_provider_snapshot(snapshot_key),
    CHECK (high IS NULL OR (open IS NULL OR high >= open)),
    CHECK (high IS NULL OR high >= close),
    CHECK (low IS NULL OR (open IS NULL OR low <= open)),
    CHECK (low IS NULL OR low <= close)
);

CREATE TABLE IF NOT EXISTS fact_corporate_action (
    instrument_id INTEGER NOT NULL,
    action_date TEXT NOT NULL,
    provider_source_id TEXT NOT NULL,
    action_type TEXT NOT NULL CHECK (action_type IN ('cash_dividend', 'capital_event')),
    action_value REAL,
    action_currency TEXT,
    snapshot_key TEXT NOT NULL,
    details_json TEXT NOT NULL DEFAULT '{}',
    created_at_utc TEXT NOT NULL,
    updated_at_utc TEXT NOT NULL,
    PRIMARY KEY (instrument_id, action_date, provider_source_id, action_type),
    FOREIGN KEY (instrument_id) REFERENCES dim_market_instrument(instrument_id),
    FOREIGN KEY (provider_source_id) REFERENCES source_registry(source_id),
    FOREIGN KEY (snapshot_key) REFERENCES fact_market_provider_snapshot(snapshot_key)
);

CREATE TABLE IF NOT EXISTS dim_trading_calendar_session (
    calendar_code TEXT NOT NULL CHECK (calendar_code = 'XNYS_PROXY_SPY'),
    session_date TEXT NOT NULL,
    source_instrument_id INTEGER NOT NULL,
    provider_source_id TEXT NOT NULL,
    snapshot_key TEXT NOT NULL,
    created_at_utc TEXT NOT NULL,
    updated_at_utc TEXT NOT NULL,
    PRIMARY KEY (calendar_code, session_date),
    FOREIGN KEY (source_instrument_id) REFERENCES dim_market_instrument(instrument_id),
    FOREIGN KEY (provider_source_id) REFERENCES source_registry(source_id),
    FOREIGN KEY (snapshot_key) REFERENCES fact_market_provider_snapshot(snapshot_key)
);

CREATE TABLE IF NOT EXISTS fact_market_data_coverage (
    audit_asof_date TEXT NOT NULL,
    role_key TEXT NOT NULL,
    instrument_id INTEGER NOT NULL,
    expected_start_date TEXT NOT NULL,
    expected_end_date TEXT NOT NULL,
    first_bar_date TEXT,
    last_bar_date TEXT,
    bar_count INTEGER NOT NULL CHECK (bar_count >= 0),
    expected_session_count INTEGER NOT NULL CHECK (expected_session_count >= 0),
    missing_session_count INTEGER NOT NULL CHECK (missing_session_count >= 0),
    missing_session_ratio REAL NOT NULL CHECK (missing_session_ratio >= 0 AND missing_session_ratio <= 1),
    longest_missing_session_gap INTEGER NOT NULL CHECK (longest_missing_session_gap >= 0),
    invalid_bar_count INTEGER NOT NULL CHECK (invalid_bar_count >= 0),
    coverage_status TEXT NOT NULL CHECK (
        coverage_status IN ('complete', 'recent_listing_short_history', 'partial', 'missing', 'failed')
    ),
    rank_ready INTEGER NOT NULL CHECK (rank_ready IN (0, 1)),
    issue_detail TEXT NOT NULL DEFAULT '',
    provider_source_id TEXT NOT NULL,
    snapshot_key TEXT,
    created_at_utc TEXT NOT NULL,
    PRIMARY KEY (audit_asof_date, role_key),
    FOREIGN KEY (role_key) REFERENCES bridge_market_instrument_role(role_key),
    FOREIGN KEY (instrument_id) REFERENCES dim_market_instrument(instrument_id),
    FOREIGN KEY (provider_source_id) REFERENCES source_registry(source_id),
    FOREIGN KEY (snapshot_key) REFERENCES fact_market_provider_snapshot(snapshot_key)
);

CREATE TABLE IF NOT EXISTS dim_terminal_return_rule (
    event_key TEXT PRIMARY KEY,
    outcome_class TEXT NOT NULL CHECK (
        outcome_class IN ('fixed_cash', 'stock_conversion', 'mixed_prorated',
                          'bankruptcy_distribution', 'otc_continuation')
    ),
    cash_weight REAL NOT NULL CHECK (cash_weight >= 0 AND cash_weight <= 1),
    stock_weight REAL NOT NULL CHECK (stock_weight >= 0 AND stock_weight <= 1),
    bankruptcy_distribution_value REAL CHECK (bankruptcy_distribution_value IS NULL OR bankruptcy_distribution_value >= 0),
    distribution_currency TEXT,
    otc_continuation_symbol TEXT,
    fractional_share_treatment TEXT NOT NULL,
    max_reference_lag_calendar_days INTEGER NOT NULL CHECK (max_reference_lag_calendar_days >= 0),
    rule_status TEXT NOT NULL CHECK (rule_status IN ('ready_for_calculation', 'pending_distribution_evidence')),
    source_id TEXT NOT NULL,
    evidence_json TEXT NOT NULL,
    contract_version TEXT NOT NULL,
    contract_sha256 TEXT NOT NULL,
    created_at_utc TEXT NOT NULL,
    updated_at_utc TEXT NOT NULL,
    FOREIGN KEY (event_key) REFERENCES fact_terminal_event_reconciliation(event_key),
    FOREIGN KEY (source_id) REFERENCES source_registry(source_id),
    CHECK (cash_weight + stock_weight <= 1.0000001)
);

CREATE TABLE IF NOT EXISTS fact_terminal_return_calculation (
    event_key TEXT NOT NULL,
    calculation_asof_date TEXT NOT NULL,
    historical_instrument_id INTEGER NOT NULL,
    successor_instrument_id INTEGER,
    historical_final_price_date TEXT,
    historical_final_close REAL,
    historical_final_adjusted_close REAL,
    successor_reference_price_date TEXT,
    successor_reference_close REAL,
    successor_reference_adjusted_close REAL,
    cash_component REAL,
    stock_component REAL,
    distribution_component REAL,
    terminal_value REAL,
    terminal_currency TEXT,
    calculation_status TEXT NOT NULL,
    resolved INTEGER NOT NULL CHECK (resolved IN (0, 1)),
    no_future_price_used INTEGER NOT NULL CHECK (no_future_price_used IN (0, 1)),
    fractional_share_treatment TEXT NOT NULL,
    market_snapshot_key TEXT,
    rule_contract_sha256 TEXT NOT NULL,
    calculation_evidence_sha256 TEXT NOT NULL,
    evidence_json TEXT NOT NULL,
    created_at_utc TEXT NOT NULL,
    updated_at_utc TEXT NOT NULL,
    PRIMARY KEY (event_key, calculation_asof_date),
    FOREIGN KEY (event_key) REFERENCES fact_terminal_event_reconciliation(event_key),
    FOREIGN KEY (historical_instrument_id) REFERENCES dim_market_instrument(instrument_id),
    FOREIGN KEY (successor_instrument_id) REFERENCES dim_market_instrument(instrument_id),
    FOREIGN KEY (market_snapshot_key) REFERENCES fact_market_provider_snapshot(snapshot_key),
    CHECK ((resolved = 0 AND terminal_value IS NULL) OR (resolved = 1 AND terminal_value IS NOT NULL))
);

CREATE TABLE IF NOT EXISTS feature_market_technical (
    security_id INTEGER NOT NULL,
    instrument_id INTEGER NOT NULL,
    ticker TEXT NOT NULL COLLATE NOCASE,
    asof_date TEXT NOT NULL,
    provider_source_id TEXT NOT NULL,
    snapshot_key TEXT NOT NULL,
    adjusted_close REAL NOT NULL,
    return_21d REAL,
    return_63d REAL,
    return_126d REAL,
    return_252d REAL,
    momentum_12m_ex_1m REAL,
    xlb_residual_momentum REAL,
    spy_beta_252d REAL,
    spy_beta_residual_momentum_126d REAL,
    realized_volatility_63d REAL,
    downside_volatility_63d REAL,
    max_drawdown_252d REAL,
    distance_from_52_week_high REAL,
    moving_average_50d REAL,
    moving_average_200d REAL,
    trend_50_over_200 INTEGER CHECK (trend_50_over_200 IS NULL OR trend_50_over_200 IN (0, 1)),
    average_dollar_volume_63d REAL,
    history_days INTEGER NOT NULL CHECK (history_days >= 1),
    history_start_date TEXT NOT NULL,
    last_price_date TEXT NOT NULL,
    quality_status TEXT NOT NULL CHECK (quality_status IN ('full', 'partial_history', 'insufficient_history', 'stale')),
    quality_reasons_json TEXT NOT NULL DEFAULT '[]',
    feature_definition_version TEXT NOT NULL,
    created_at_utc TEXT NOT NULL,
    updated_at_utc TEXT NOT NULL,
    PRIMARY KEY (security_id, asof_date),
    FOREIGN KEY (security_id) REFERENCES dim_security(security_id),
    FOREIGN KEY (instrument_id) REFERENCES dim_market_instrument(instrument_id),
    FOREIGN KEY (provider_source_id) REFERENCES source_registry(source_id),
    FOREIGN KEY (snapshot_key) REFERENCES fact_market_provider_snapshot(snapshot_key)
);

CREATE INDEX IF NOT EXISTS idx_market_instrument_symbol
    ON dim_market_instrument(provider_source_id, provider_symbol);
CREATE INDEX IF NOT EXISTS idx_market_role_type
    ON bridge_market_instrument_role(role_type, model_ticker);
CREATE INDEX IF NOT EXISTS idx_adjusted_price_date
    ON fact_adjusted_price_bar(bar_date, instrument_id);
CREATE INDEX IF NOT EXISTS idx_market_coverage_status
    ON fact_market_data_coverage(audit_asof_date, coverage_status, rank_ready);
CREATE INDEX IF NOT EXISTS idx_terminal_calculation_status
    ON fact_terminal_return_calculation(calculation_asof_date, resolved, calculation_status);
CREATE INDEX IF NOT EXISTS idx_market_feature_asof
    ON feature_market_technical(asof_date, quality_status, ticker);
"""


FINANCIAL_CONTRACT_SQL = r"""
CREATE TABLE IF NOT EXISTS dim_financial_metric (
    metric_id INTEGER PRIMARY KEY AUTOINCREMENT,
    canonical_metric TEXT NOT NULL UNIQUE,
    statement_type TEXT NOT NULL CHECK (
        statement_type IN ('income', 'balance_sheet', 'cash_flow')
    ),
    period_type TEXT NOT NULL CHECK (period_type IN ('duration', 'instant')),
    sign_policy TEXT NOT NULL CHECK (
        sign_policy IN ('preserve', 'positive_expense', 'positive_outflow', 'positive_liability')
    ),
    definition_version TEXT NOT NULL,
    source_id TEXT NOT NULL,
    contract_sha256 TEXT NOT NULL,
    created_at_utc TEXT NOT NULL,
    updated_at_utc TEXT NOT NULL,
    FOREIGN KEY (source_id) REFERENCES source_registry(source_id)
);

CREATE TABLE IF NOT EXISTS bridge_financial_metric_concept (
    metric_id INTEGER NOT NULL,
    taxonomy TEXT NOT NULL CHECK (taxonomy IN ('us-gaap', 'ifrs-full')),
    concept TEXT NOT NULL,
    priority INTEGER NOT NULL CHECK (priority >= 1),
    source_id TEXT NOT NULL,
    contract_version TEXT NOT NULL,
    contract_sha256 TEXT NOT NULL,
    created_at_utc TEXT NOT NULL,
    updated_at_utc TEXT NOT NULL,
    PRIMARY KEY (metric_id, taxonomy, concept),
    UNIQUE (metric_id, taxonomy, priority),
    FOREIGN KEY (metric_id) REFERENCES dim_financial_metric(metric_id) ON DELETE CASCADE,
    FOREIGN KEY (source_id) REFERENCES source_registry(source_id)
);

CREATE TABLE IF NOT EXISTS dim_issuer_reporting_profile (
    profile_key TEXT PRIMARY KEY,
    company_id INTEGER NOT NULL,
    security_id INTEGER NOT NULL UNIQUE,
    ticker TEXT NOT NULL COLLATE NOCASE UNIQUE,
    role_type TEXT NOT NULL CHECK (role_type IN ('current_universe', 'historical_pilot')),
    profile_asof_date TEXT NOT NULL,
    source_cutoff_date TEXT NOT NULL,
    cik TEXT NOT NULL CHECK (length(cik) = 10 AND cik NOT GLOB '*[^0-9]*'),
    sec_entity_name TEXT NOT NULL,
    domicile_country TEXT NOT NULL,
    primary_annual_form TEXT NOT NULL CHECK (
        primary_annual_form IN ('10-K', '20-F', '40-F', 'UNKNOWN')
    ),
    filing_regime TEXT NOT NULL CHECK (
        filing_regime IN ('domestic_sec', 'foreign_private_issuer', 'canadian_mjds', 'unknown')
    ),
    accounting_basis TEXT NOT NULL CHECK (
        accounting_basis IN ('US_GAAP', 'IFRS', 'MIXED_REVIEW', 'UNKNOWN')
    ),
    primary_taxonomy TEXT NOT NULL CHECK (
        primary_taxonomy IN ('us-gaap', 'ifrs-full', 'mixed', 'unknown')
    ),
    fiscal_year_end TEXT NOT NULL,
    reporting_currency TEXT NOT NULL,
    reporting_currency_method TEXT NOT NULL CHECK (
        reporting_currency_method IN (
            'latest_annual_anchor_concepts',
            'latest_annual_all_monetary_facts',
            'eligible_companyfacts_plurality',
            'reviewed_override',
            'unresolved'
        )
    ),
    trading_currency TEXT NOT NULL,
    expected_cadence TEXT NOT NULL CHECK (
        expected_cadence IN ('quarterly', 'annual_or_interim_6k', 'unknown')
    ),
    latest_financial_form TEXT NOT NULL,
    latest_financial_accession TEXT NOT NULL,
    latest_financial_accepted_at TEXT NOT NULL,
    latest_annual_accession TEXT NOT NULL,
    latest_annual_accepted_at TEXT NOT NULL,
    latest_companyfacts_accepted_at TEXT NOT NULL,
    companyfacts_lag_days INTEGER,
    inline_xbrl_fallback_expected INTEGER NOT NULL CHECK (
        inline_xbrl_fallback_expected IN (0, 1)
    ),
    submissions_source_id TEXT NOT NULL,
    companyfacts_source_id TEXT NOT NULL,
    submissions_sha256 TEXT NOT NULL,
    companyfacts_sha256 TEXT NOT NULL,
    profile_status TEXT NOT NULL CHECK (
        profile_status IN (
            'ready_for_ingestion',
            'companyfacts_fallback_required',
            'taxonomy_review_required',
            'currency_review_required',
            'profile_metadata_review_required',
            'annual_form_review_required',
            'legacy_archive_required'
        )
    ),
    review_reason TEXT NOT NULL,
    confidence REAL NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
    calibration_eligible INTEGER NOT NULL CHECK (calibration_eligible = 0),
    evidence_json TEXT NOT NULL DEFAULT '{}',
    contract_version TEXT NOT NULL,
    profile_sha256 TEXT NOT NULL,
    contract_sha256 TEXT NOT NULL,
    created_at_utc TEXT NOT NULL,
    updated_at_utc TEXT NOT NULL,
    FOREIGN KEY (company_id) REFERENCES dim_company(company_id),
    FOREIGN KEY (security_id) REFERENCES dim_security(security_id),
    FOREIGN KEY (submissions_source_id) REFERENCES source_registry(source_id),
    FOREIGN KEY (companyfacts_source_id) REFERENCES source_registry(source_id),
    CHECK (source_cutoff_date <= profile_asof_date),
    CHECK (reporting_currency = '' OR (
        length(reporting_currency) = 3
        AND reporting_currency NOT GLOB '*[^A-Z]*'
    ))
);

CREATE TABLE IF NOT EXISTS fact_financial_ingestion_snapshot (
    snapshot_key TEXT PRIMARY KEY,
    extraction_asof_date TEXT NOT NULL,
    contract_manifest_sha256 TEXT NOT NULL,
    cache_manifest_sha256 TEXT NOT NULL,
    issuer_count INTEGER NOT NULL CHECK (issuer_count >= 0),
    filing_count INTEGER NOT NULL CHECK (filing_count >= 0),
    raw_fact_count INTEGER NOT NULL CHECK (raw_fact_count >= 0),
    fx_observation_count INTEGER NOT NULL CHECK (fx_observation_count >= 0),
    cache_root TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('loaded', 'partial', 'failed')),
    created_at_utc TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS fact_sec_filing (
    filing_key TEXT PRIMARY KEY,
    company_id INTEGER NOT NULL,
    security_id INTEGER NOT NULL,
    ticker TEXT NOT NULL COLLATE NOCASE,
    cik TEXT NOT NULL,
    accession_number TEXT NOT NULL,
    form_type TEXT NOT NULL,
    form_family TEXT NOT NULL,
    filing_date TEXT NOT NULL,
    accepted_at TEXT NOT NULL,
    report_date TEXT NOT NULL,
    primary_document TEXT NOT NULL,
    source_id TEXT NOT NULL,
    source_url TEXT NOT NULL,
    payload_sha256 TEXT NOT NULL,
    snapshot_key TEXT NOT NULL,
    created_at_utc TEXT NOT NULL,
    updated_at_utc TEXT NOT NULL,
    UNIQUE (company_id, accession_number),
    FOREIGN KEY (company_id) REFERENCES dim_company(company_id),
    FOREIGN KEY (security_id) REFERENCES dim_security(security_id),
    FOREIGN KEY (source_id) REFERENCES source_registry(source_id),
    FOREIGN KEY (snapshot_key) REFERENCES fact_financial_ingestion_snapshot(snapshot_key)
);

CREATE TABLE IF NOT EXISTS fact_sec_xbrl_fact_raw (
    source_observation_id TEXT PRIMARY KEY,
    filing_key TEXT,
    company_id INTEGER NOT NULL,
    security_id INTEGER NOT NULL,
    ticker TEXT NOT NULL COLLATE NOCASE,
    cik TEXT NOT NULL,
    accession_number TEXT NOT NULL,
    taxonomy TEXT NOT NULL,
    concept TEXT NOT NULL,
    value_text TEXT NOT NULL,
    numeric_value REAL,
    unit TEXT NOT NULL,
    period_start TEXT NOT NULL,
    period_end TEXT NOT NULL,
    filed_date TEXT NOT NULL,
    accepted_at TEXT NOT NULL,
    form_type TEXT NOT NULL,
    frame TEXT NOT NULL,
    dimensions_json TEXT NOT NULL DEFAULT '{}',
    source_id TEXT NOT NULL,
    source_detail TEXT NOT NULL,
    snapshot_key TEXT NOT NULL,
    created_at_utc TEXT NOT NULL,
    FOREIGN KEY (filing_key) REFERENCES fact_sec_filing(filing_key),
    FOREIGN KEY (company_id) REFERENCES dim_company(company_id),
    FOREIGN KEY (security_id) REFERENCES dim_security(security_id),
    FOREIGN KEY (source_id) REFERENCES source_registry(source_id),
    FOREIGN KEY (snapshot_key) REFERENCES fact_financial_ingestion_snapshot(snapshot_key)
);

CREATE TABLE IF NOT EXISTS fact_fx_rate (
    base_currency TEXT NOT NULL,
    quote_currency TEXT NOT NULL CHECK (quote_currency = 'USD'),
    rate_date TEXT NOT NULL,
    rate REAL NOT NULL CHECK (rate > 0),
    source_id TEXT NOT NULL,
    source_timestamp_utc TEXT NOT NULL,
    payload_sha256 TEXT NOT NULL,
    snapshot_key TEXT NOT NULL,
    quality_status TEXT NOT NULL CHECK (quality_status IN ('usable', 'stale', 'quarantined')),
    created_at_utc TEXT NOT NULL,
    updated_at_utc TEXT NOT NULL,
    PRIMARY KEY (base_currency, quote_currency, rate_date, source_id),
    FOREIGN KEY (source_id) REFERENCES source_registry(source_id),
    FOREIGN KEY (snapshot_key) REFERENCES fact_financial_ingestion_snapshot(snapshot_key)
);

CREATE TABLE IF NOT EXISTS fact_financial_statement_canonical (
    canonical_fact_id TEXT PRIMARY KEY,
    company_id INTEGER NOT NULL,
    security_id INTEGER NOT NULL,
    ticker TEXT NOT NULL COLLATE NOCASE,
    canonical_metric TEXT NOT NULL,
    period_start TEXT NOT NULL,
    period_end TEXT NOT NULL,
    period_type TEXT NOT NULL CHECK (period_type IN ('duration', 'instant')),
    accepted_at TEXT NOT NULL,
    filing_key TEXT NOT NULL,
    accession_number TEXT NOT NULL,
    taxonomy TEXT NOT NULL,
    concept TEXT NOT NULL,
    reported_value REAL NOT NULL,
    reported_currency TEXT NOT NULL,
    usd_value REAL,
    fx_rate_date TEXT,
    fx_rate REAL,
    source_observation_id TEXT NOT NULL,
    source_id TEXT NOT NULL,
    selection_rank INTEGER NOT NULL CHECK (selection_rank >= 1),
    amendment_sequence INTEGER NOT NULL CHECK (amendment_sequence >= 0),
    superseded_by_fact_id TEXT,
    quality_status TEXT NOT NULL CHECK (
        quality_status IN ('usable', 'fx_missing', 'conflicted', 'superseded', 'quarantined')
    ),
    quality_reasons_json TEXT NOT NULL DEFAULT '[]',
    definition_version TEXT NOT NULL,
    snapshot_key TEXT NOT NULL,
    created_at_utc TEXT NOT NULL,
    updated_at_utc TEXT NOT NULL,
    FOREIGN KEY (company_id) REFERENCES dim_company(company_id),
    FOREIGN KEY (security_id) REFERENCES dim_security(security_id),
    FOREIGN KEY (canonical_metric) REFERENCES dim_financial_metric(canonical_metric),
    FOREIGN KEY (filing_key) REFERENCES fact_sec_filing(filing_key),
    FOREIGN KEY (source_observation_id) REFERENCES fact_sec_xbrl_fact_raw(source_observation_id),
    FOREIGN KEY (source_id) REFERENCES source_registry(source_id),
    FOREIGN KEY (superseded_by_fact_id) REFERENCES fact_financial_statement_canonical(canonical_fact_id),
    FOREIGN KEY (snapshot_key) REFERENCES fact_financial_ingestion_snapshot(snapshot_key),
    CHECK (accepted_at >= period_end)
);

CREATE TABLE IF NOT EXISTS feature_financial_statement (
    security_id INTEGER NOT NULL,
    ticker TEXT NOT NULL COLLATE NOCASE,
    asof_date TEXT NOT NULL,
    reporting_currency TEXT NOT NULL,
    latest_period_end TEXT NOT NULL,
    latest_accepted_at TEXT NOT NULL,
    revenue_ttm_usd REAL,
    gross_profit_ttm_usd REAL,
    operating_income_ttm_usd REAL,
    net_income_ttm_usd REAL,
    operating_cash_flow_ttm_usd REAL,
    capital_expenditures_ttm_usd REAL,
    free_cash_flow_ttm_usd REAL,
    cash_usd REAL,
    debt_usd REAL,
    assets_usd REAL,
    equity_usd REAL,
    inventory_usd REAL,
    diluted_shares REAL,
    gross_margin REAL,
    operating_margin REAL,
    free_cash_flow_margin REAL,
    return_on_invested_capital REAL,
    asset_turnover REAL,
    net_debt_to_ebitda REAL,
    interest_coverage REAL,
    inventory_days REAL,
    inventory_turnover REAL,
    cash_conversion_cycle REAL,
    revenue_growth REAL,
    operating_income_growth REAL,
    free_cash_flow_growth REAL,
    incremental_operating_margin REAL,
    inventory_sales_growth_spread REAL,
    capex_to_revenue REAL,
    free_cash_flow_yield REAL,
    enterprise_value_to_ebitda REAL,
    enterprise_value_to_ebit REAL,
    enterprise_value_to_gross_profit REAL,
    dilution REAL,
    data_confidence REAL CHECK (data_confidence IS NULL OR (
        data_confidence >= 0 AND data_confidence <= 1
    )),
    quality_status TEXT NOT NULL CHECK (
        quality_status IN ('full', 'partial', 'insufficient', 'stale', 'blocked')
    ),
    quality_reasons_json TEXT NOT NULL DEFAULT '[]',
    feature_definition_version TEXT NOT NULL,
    market_snapshot_key TEXT NOT NULL,
    financial_snapshot_key TEXT NOT NULL,
    created_at_utc TEXT NOT NULL,
    updated_at_utc TEXT NOT NULL,
    PRIMARY KEY (security_id, asof_date),
    FOREIGN KEY (security_id) REFERENCES dim_security(security_id),
    FOREIGN KEY (market_snapshot_key) REFERENCES fact_market_provider_snapshot(snapshot_key),
    FOREIGN KEY (financial_snapshot_key) REFERENCES fact_financial_ingestion_snapshot(snapshot_key)
);

CREATE TABLE IF NOT EXISTS fact_financial_data_coverage (
    audit_asof_date TEXT NOT NULL,
    security_id INTEGER NOT NULL,
    ticker TEXT NOT NULL COLLATE NOCASE,
    profile_status TEXT NOT NULL,
    expected_metric_count INTEGER NOT NULL CHECK (expected_metric_count >= 0),
    available_metric_count INTEGER NOT NULL CHECK (available_metric_count >= 0),
    stale_metric_count INTEGER NOT NULL CHECK (stale_metric_count >= 0),
    conflict_metric_count INTEGER NOT NULL CHECK (conflict_metric_count >= 0),
    coverage_ratio REAL NOT NULL CHECK (coverage_ratio >= 0 AND coverage_ratio <= 1),
    rank_ready INTEGER NOT NULL CHECK (rank_ready IN (0, 1)),
    issue_detail TEXT NOT NULL,
    snapshot_key TEXT NOT NULL,
    created_at_utc TEXT NOT NULL,
    PRIMARY KEY (audit_asof_date, security_id),
    FOREIGN KEY (security_id) REFERENCES dim_security(security_id),
    FOREIGN KEY (snapshot_key) REFERENCES fact_financial_ingestion_snapshot(snapshot_key)
);

CREATE INDEX IF NOT EXISTS idx_reporting_profile_role
    ON dim_issuer_reporting_profile(role_type, profile_status, ticker);
CREATE INDEX IF NOT EXISTS idx_financial_concept_lookup
    ON bridge_financial_metric_concept(taxonomy, concept, priority);
CREATE INDEX IF NOT EXISTS idx_sec_filing_acceptance
    ON fact_sec_filing(ticker, accepted_at, form_family);
CREATE INDEX IF NOT EXISTS idx_raw_financial_fact_lookup
    ON fact_sec_xbrl_fact_raw(ticker, accepted_at, taxonomy, concept);
CREATE INDEX IF NOT EXISTS idx_canonical_financial_fact_lookup
    ON fact_financial_statement_canonical(ticker, accepted_at, canonical_metric, period_end);
CREATE INDEX IF NOT EXISTS idx_financial_feature_asof
    ON feature_financial_statement(asof_date, quality_status, ticker);
CREATE INDEX IF NOT EXISTS idx_financial_coverage_status
    ON fact_financial_data_coverage(audit_asof_date, rank_ready, ticker);
"""


FINANCIAL_INGESTION_SQL = r"""
CREATE TABLE IF NOT EXISTS dim_financial_profile_resolution (
    profile_key TEXT PRIMARY KEY,
    company_id INTEGER NOT NULL,
    security_id INTEGER NOT NULL UNIQUE,
    ticker TEXT NOT NULL COLLATE NOCASE UNIQUE,
    role_type TEXT NOT NULL CHECK (role_type IN ('current_universe', 'historical_pilot')),
    original_profile_status TEXT NOT NULL,
    ingestion_route TEXT NOT NULL,
    resolution_status TEXT NOT NULL CHECK (
        resolution_status IN (
            'resolved_standard',
            'resolved_inline_xbrl',
            'resolved_xbrl_instance',
            'resolved_metadata_unstructured',
            'resolved_interim_only',
            'missing_required_source'
        )
    ),
    effective_annual_form TEXT NOT NULL,
    effective_accounting_basis TEXT NOT NULL,
    effective_taxonomy TEXT NOT NULL,
    effective_reporting_currency TEXT NOT NULL,
    canonical_eligible INTEGER NOT NULL CHECK (canonical_eligible IN (0, 1)),
    evidence_accession TEXT NOT NULL,
    evidence_form TEXT NOT NULL,
    evidence_accepted_at TEXT NOT NULL,
    evidence_url TEXT NOT NULL,
    evidence_sha256 TEXT NOT NULL,
    resolution_reason TEXT NOT NULL,
    source_cutoff_date TEXT NOT NULL,
    source_id TEXT NOT NULL,
    policy_version TEXT NOT NULL,
    policy_sha256 TEXT NOT NULL,
    resolution_sha256 TEXT NOT NULL,
    calibration_eligible INTEGER NOT NULL CHECK (calibration_eligible = 0),
    created_at_utc TEXT NOT NULL,
    updated_at_utc TEXT NOT NULL,
    FOREIGN KEY (profile_key) REFERENCES dim_issuer_reporting_profile(profile_key),
    FOREIGN KEY (company_id) REFERENCES dim_company(company_id),
    FOREIGN KEY (security_id) REFERENCES dim_security(security_id),
    FOREIGN KEY (source_id) REFERENCES source_registry(source_id)
);

CREATE TABLE IF NOT EXISTS fact_financial_normalization_issue (
    issue_key TEXT PRIMARY KEY,
    snapshot_key TEXT NOT NULL,
    company_id INTEGER NOT NULL,
    security_id INTEGER NOT NULL,
    ticker TEXT NOT NULL COLLATE NOCASE,
    stage TEXT NOT NULL,
    severity TEXT NOT NULL CHECK (severity IN ('info', 'warning', 'error')),
    issue_code TEXT NOT NULL,
    canonical_metric TEXT NOT NULL,
    accession_number TEXT NOT NULL,
    source_observation_ids_json TEXT NOT NULL DEFAULT '[]',
    evidence_json TEXT NOT NULL DEFAULT '{}',
    message TEXT NOT NULL,
    created_at_utc TEXT NOT NULL,
    FOREIGN KEY (snapshot_key) REFERENCES fact_financial_ingestion_snapshot(snapshot_key),
    FOREIGN KEY (company_id) REFERENCES dim_company(company_id),
    FOREIGN KEY (security_id) REFERENCES dim_security(security_id)
);

ALTER TABLE fact_financial_ingestion_snapshot ADD COLUMN policy_sha256 TEXT NOT NULL DEFAULT '';
ALTER TABLE fact_financial_ingestion_snapshot ADD COLUMN resolution_sha256 TEXT NOT NULL DEFAULT '';
ALTER TABLE fact_financial_ingestion_snapshot ADD COLUMN companyfacts_payload_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE fact_financial_ingestion_snapshot ADD COLUMN inline_payload_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE fact_financial_ingestion_snapshot ADD COLUMN canonical_fact_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE fact_financial_ingestion_snapshot ADD COLUMN feature_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE fact_financial_ingestion_snapshot ADD COLUMN details_json TEXT NOT NULL DEFAULT '{}';

ALTER TABLE fact_sec_filing ADD COLUMN fiscal_year TEXT NOT NULL DEFAULT '';
ALTER TABLE fact_sec_filing ADD COLUMN fiscal_period TEXT NOT NULL DEFAULT '';
ALTER TABLE fact_sec_filing ADD COLUMN is_amendment INTEGER NOT NULL DEFAULT 0;
ALTER TABLE fact_sec_filing ADD COLUMN filing_payload_kind TEXT NOT NULL DEFAULT 'submissions';

ALTER TABLE fact_sec_xbrl_fact_raw ADD COLUMN fiscal_year TEXT NOT NULL DEFAULT '';
ALTER TABLE fact_sec_xbrl_fact_raw ADD COLUMN fiscal_period TEXT NOT NULL DEFAULT '';
ALTER TABLE fact_sec_xbrl_fact_raw ADD COLUMN decimals TEXT NOT NULL DEFAULT '';
ALTER TABLE fact_sec_xbrl_fact_raw ADD COLUMN context_id TEXT NOT NULL DEFAULT '';
ALTER TABLE fact_sec_xbrl_fact_raw ADD COLUMN payload_sha256 TEXT NOT NULL DEFAULT '';
ALTER TABLE fact_sec_xbrl_fact_raw ADD COLUMN evidence_url TEXT NOT NULL DEFAULT '';
ALTER TABLE fact_sec_xbrl_fact_raw ADD COLUMN quality_status TEXT NOT NULL DEFAULT 'usable';
ALTER TABLE fact_sec_xbrl_fact_raw ADD COLUMN quarantine_reason TEXT NOT NULL DEFAULT '';

ALTER TABLE fact_financial_statement_canonical ADD COLUMN fiscal_year TEXT NOT NULL DEFAULT '';
ALTER TABLE fact_financial_statement_canonical ADD COLUMN fiscal_period TEXT NOT NULL DEFAULT '';
ALTER TABLE fact_financial_statement_canonical ADD COLUMN context_id TEXT NOT NULL DEFAULT '';
ALTER TABLE fact_financial_statement_canonical ADD COLUMN normalization_method TEXT NOT NULL DEFAULT '';
ALTER TABLE fact_financial_statement_canonical ADD COLUMN evidence_json TEXT NOT NULL DEFAULT '{}';

ALTER TABLE feature_financial_statement ADD COLUMN market_price REAL;
ALTER TABLE feature_financial_statement ADD COLUMN market_price_date TEXT NOT NULL DEFAULT '';
ALTER TABLE feature_financial_statement ADD COLUMN market_cap_usd REAL;
ALTER TABLE feature_financial_statement ADD COLUMN enterprise_value_usd REAL;
ALTER TABLE feature_financial_statement ADD COLUMN ebitda_ttm_usd REAL;
ALTER TABLE feature_financial_statement ADD COLUMN feature_inputs_json TEXT NOT NULL DEFAULT '{}';
ALTER TABLE feature_financial_statement ADD COLUMN available_metric_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE feature_financial_statement ADD COLUMN expected_metric_count INTEGER NOT NULL DEFAULT 0;

ALTER TABLE fact_financial_data_coverage ADD COLUMN role_type TEXT NOT NULL DEFAULT '';
ALTER TABLE fact_financial_data_coverage ADD COLUMN filing_regime TEXT NOT NULL DEFAULT '';
ALTER TABLE fact_financial_data_coverage ADD COLUMN ingestion_route TEXT NOT NULL DEFAULT '';
ALTER TABLE fact_financial_data_coverage ADD COLUMN latest_accepted_at TEXT NOT NULL DEFAULT '';
ALTER TABLE fact_financial_data_coverage ADD COLUMN missing_metrics_json TEXT NOT NULL DEFAULT '[]';

CREATE INDEX IF NOT EXISTS idx_financial_resolution_status
    ON dim_financial_profile_resolution(role_type, resolution_status, ticker);
CREATE INDEX IF NOT EXISTS idx_financial_normalization_issue
    ON fact_financial_normalization_issue(snapshot_key, severity, issue_code, ticker);
CREATE INDEX IF NOT EXISTS idx_raw_financial_fact_period
    ON fact_sec_xbrl_fact_raw(ticker, period_end, fiscal_period, quality_status);
"""


FINANCIAL_PERFORMANCE_SQL = r"""
CREATE INDEX IF NOT EXISTS idx_canonical_financial_snapshot_ticker
    ON fact_financial_statement_canonical(snapshot_key, ticker);
CREATE INDEX IF NOT EXISTS idx_canonical_financial_superseded_by
    ON fact_financial_statement_canonical(superseded_by_fact_id);
"""

FINANCIAL_REMEDIATION_SQL = r"""
ALTER TABLE dim_financial_profile_resolution
    RENAME TO dim_financial_profile_resolution_v6;

CREATE TABLE dim_financial_profile_resolution (
    profile_key TEXT PRIMARY KEY,
    company_id INTEGER NOT NULL,
    security_id INTEGER NOT NULL UNIQUE,
    ticker TEXT NOT NULL COLLATE NOCASE UNIQUE,
    role_type TEXT NOT NULL CHECK (role_type IN ('current_universe', 'historical_pilot')),
    original_profile_status TEXT NOT NULL,
    ingestion_route TEXT NOT NULL,
    resolution_status TEXT NOT NULL CHECK (
        resolution_status IN (
            'resolved_standard',
            'resolved_inline_xbrl',
            'resolved_xbrl_instance',
            'resolved_audited_html',
            'resolved_metadata_unstructured',
            'resolved_interim_only',
            'missing_required_source'
        )
    ),
    effective_annual_form TEXT NOT NULL,
    effective_accounting_basis TEXT NOT NULL,
    effective_taxonomy TEXT NOT NULL,
    effective_reporting_currency TEXT NOT NULL,
    canonical_eligible INTEGER NOT NULL CHECK (canonical_eligible IN (0, 1)),
    evidence_accession TEXT NOT NULL,
    evidence_form TEXT NOT NULL,
    evidence_accepted_at TEXT NOT NULL,
    evidence_url TEXT NOT NULL,
    evidence_sha256 TEXT NOT NULL,
    resolution_reason TEXT NOT NULL,
    source_cutoff_date TEXT NOT NULL,
    source_id TEXT NOT NULL,
    policy_version TEXT NOT NULL,
    policy_sha256 TEXT NOT NULL,
    resolution_sha256 TEXT NOT NULL,
    calibration_eligible INTEGER NOT NULL CHECK (calibration_eligible = 0),
    created_at_utc TEXT NOT NULL,
    updated_at_utc TEXT NOT NULL,
    FOREIGN KEY (profile_key) REFERENCES dim_issuer_reporting_profile(profile_key),
    FOREIGN KEY (company_id) REFERENCES dim_company(company_id),
    FOREIGN KEY (security_id) REFERENCES dim_security(security_id),
    FOREIGN KEY (source_id) REFERENCES source_registry(source_id)
);

INSERT INTO dim_financial_profile_resolution (
    profile_key, company_id, security_id, ticker, role_type,
    original_profile_status, ingestion_route, resolution_status,
    effective_annual_form, effective_accounting_basis, effective_taxonomy,
    effective_reporting_currency, canonical_eligible, evidence_accession,
    evidence_form, evidence_accepted_at, evidence_url, evidence_sha256,
    resolution_reason, source_cutoff_date, source_id, policy_version,
    policy_sha256, resolution_sha256, calibration_eligible,
    created_at_utc, updated_at_utc
)
SELECT
    profile_key, company_id, security_id, ticker, role_type,
    original_profile_status, ingestion_route, resolution_status,
    effective_annual_form, effective_accounting_basis, effective_taxonomy,
    effective_reporting_currency, canonical_eligible, evidence_accession,
    evidence_form, evidence_accepted_at, evidence_url, evidence_sha256,
    resolution_reason, source_cutoff_date, source_id, policy_version,
    policy_sha256, resolution_sha256, calibration_eligible,
    created_at_utc, updated_at_utc
FROM dim_financial_profile_resolution_v6;

DROP TABLE dim_financial_profile_resolution_v6;

CREATE TABLE dim_security_share_ratio (
    ratio_key TEXT PRIMARY KEY,
    security_id INTEGER NOT NULL,
    ticker TEXT NOT NULL COLLATE NOCASE,
    cik TEXT NOT NULL CHECK (length(cik) = 10 AND cik NOT GLOB '*[^0-9]*'),
    exchange TEXT NOT NULL,
    listed_security_title TEXT NOT NULL,
    security_basis TEXT NOT NULL CHECK (security_basis IN ('direct_share', 'adr_ads')),
    issuer_shares_per_traded_security REAL NOT NULL CHECK (
        issuer_shares_per_traded_security > 0
    ),
    effective_from_date TEXT NOT NULL,
    effective_to_date TEXT NOT NULL DEFAULT '',
    evidence_accession TEXT NOT NULL,
    evidence_form TEXT NOT NULL,
    evidence_accepted_at TEXT NOT NULL,
    evidence_document TEXT NOT NULL,
    evidence_url TEXT NOT NULL,
    evidence_payload_sha256 TEXT NOT NULL,
    ratio_evidence_text TEXT NOT NULL DEFAULT '',
    source_id TEXT NOT NULL,
    review_status TEXT NOT NULL CHECK (review_status = 'approved_current_snapshot'),
    reviewed_on TEXT NOT NULL,
    policy_version TEXT NOT NULL,
    policy_sha256 TEXT NOT NULL,
    row_sha256 TEXT NOT NULL,
    created_at_utc TEXT NOT NULL,
    updated_at_utc TEXT NOT NULL,
    UNIQUE (security_id, effective_from_date),
    UNIQUE (ticker, effective_from_date),
    FOREIGN KEY (security_id) REFERENCES dim_security(security_id),
    FOREIGN KEY (source_id) REFERENCES source_registry(source_id),
    CHECK (effective_to_date = '' OR effective_to_date >= effective_from_date),
    CHECK (substr(evidence_accepted_at, 1, 10) >= effective_from_date)
);

ALTER TABLE feature_financial_statement
    ADD COLUMN security_ratio_key TEXT NOT NULL DEFAULT '';
ALTER TABLE feature_financial_statement
    ADD COLUMN security_basis TEXT NOT NULL DEFAULT '';
ALTER TABLE feature_financial_statement
    ADD COLUMN issuer_shares_per_traded_security REAL;

CREATE INDEX IF NOT EXISTS idx_financial_resolution_status
    ON dim_financial_profile_resolution(role_type, resolution_status, ticker);
CREATE INDEX IF NOT EXISTS idx_security_share_ratio_effective
    ON dim_security_share_ratio(security_id, effective_from_date, effective_to_date);
CREATE INDEX IF NOT EXISTS idx_security_share_ratio_ticker
    ON dim_security_share_ratio(ticker, effective_from_date);
"""


SPECIALIZED_METRIC_SQL = r"""
CREATE TABLE IF NOT EXISTS dim_specialized_metric (
    metric_id TEXT PRIMARY KEY,
    metric_role TEXT NOT NULL CHECK (metric_role IN ('direct', 'operand', 'derived')),
    unit_family TEXT NOT NULL,
    period_type TEXT NOT NULL CHECK (period_type IN ('duration', 'instant', 'event')),
    dimension_family TEXT NOT NULL,
    direction_hint TEXT NOT NULL CHECK (
        direction_hint IN ('positive', 'negative', 'context_dependent', 'diagnostic')
    ),
    definition TEXT NOT NULL,
    formula TEXT NOT NULL DEFAULT '',
    definition_variants_json TEXT NOT NULL DEFAULT '[]',
    plausibility_json TEXT NOT NULL DEFAULT '{}',
    production_weight REAL NOT NULL CHECK (production_weight = 0),
    scoring_eligible INTEGER NOT NULL CHECK (scoring_eligible = 0),
    calibration_eligible INTEGER NOT NULL CHECK (calibration_eligible = 0),
    source_id TEXT NOT NULL,
    policy_version TEXT NOT NULL,
    policy_sha256 TEXT NOT NULL,
    created_at_utc TEXT NOT NULL,
    updated_at_utc TEXT NOT NULL,
    FOREIGN KEY (source_id) REFERENCES source_registry(source_id)
);
"""


SPECIALIZED_SOURCE_CENSUS_SQL = r"""
CREATE TABLE IF NOT EXISTS bridge_specialized_metric_cohort (
    metric_id TEXT NOT NULL,
    cohort_id TEXT NOT NULL,
    applicability_mode TEXT NOT NULL CHECK (
        applicability_mode IN ('all_cohort_issuers', 'issuer_selective_review')
    ),
    coverage_tier TEXT NOT NULL CHECK (coverage_tier IN ('core', 'supporting', 'optional')),
    minimum_current_coverage REAL NOT NULL CHECK (
        minimum_current_coverage >= 0 AND minimum_current_coverage <= 1
    ),
    minimum_historical_coverage REAL NOT NULL CHECK (
        minimum_historical_coverage >= 0 AND minimum_historical_coverage <= 1
    ),
    source_families_json TEXT NOT NULL,
    table_families_json TEXT NOT NULL,
    policy_version TEXT NOT NULL,
    policy_sha256 TEXT NOT NULL,
    created_at_utc TEXT NOT NULL,
    updated_at_utc TEXT NOT NULL,
    PRIMARY KEY (metric_id, cohort_id),
    FOREIGN KEY (metric_id) REFERENCES dim_specialized_metric(metric_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS bridge_specialized_metric_operand (
    derived_metric_id TEXT NOT NULL,
    operand_metric_id TEXT NOT NULL,
    operand_role TEXT NOT NULL,
    required INTEGER NOT NULL CHECK (required IN (0, 1)),
    policy_version TEXT NOT NULL,
    policy_sha256 TEXT NOT NULL,
    created_at_utc TEXT NOT NULL,
    PRIMARY KEY (derived_metric_id, operand_metric_id, operand_role),
    FOREIGN KEY (derived_metric_id) REFERENCES dim_specialized_metric(metric_id) ON DELETE CASCADE,
    FOREIGN KEY (operand_metric_id) REFERENCES dim_specialized_metric(metric_id),
    CHECK (derived_metric_id <> operand_metric_id)
);

CREATE TABLE IF NOT EXISTS bridge_specialized_metric_applicability (
    security_id INTEGER NOT NULL,
    metric_id TEXT NOT NULL,
    ticker TEXT NOT NULL COLLATE NOCASE,
    cohort_id TEXT NOT NULL,
    applicability_status TEXT NOT NULL CHECK (
        applicability_status IN ('applicable', 'not_applicable', 'review_required')
    ),
    applicability_basis TEXT NOT NULL,
    reviewed INTEGER NOT NULL CHECK (reviewed IN (0, 1)),
    review_source_id TEXT NOT NULL,
    row_sha256 TEXT NOT NULL,
    policy_version TEXT NOT NULL,
    policy_sha256 TEXT NOT NULL,
    created_at_utc TEXT NOT NULL,
    updated_at_utc TEXT NOT NULL,
    PRIMARY KEY (security_id, metric_id),
    FOREIGN KEY (security_id) REFERENCES dim_security(security_id),
    FOREIGN KEY (metric_id) REFERENCES dim_specialized_metric(metric_id) ON DELETE CASCADE,
    FOREIGN KEY (review_source_id) REFERENCES source_registry(source_id),
    CHECK ((applicability_status = 'review_required' AND reviewed = 0)
           OR (applicability_status <> 'review_required' AND reviewed = 1))
);

CREATE TABLE IF NOT EXISTS fact_specialized_source_census (
    census_key TEXT PRIMARY KEY,
    security_id INTEGER NOT NULL,
    ticker TEXT NOT NULL COLLATE NOCASE,
    cohort_id TEXT NOT NULL,
    source_family TEXT NOT NULL CHECK (
        source_family IN (
            'sec_filing', 'issuer_ir', 'local_exchange', 'archived_issuer',
            'technical_report', 'reserve_resource', 'commodity_market',
            'positioning_market'
        )
    ),
    source_record_key TEXT NOT NULL,
    document_role TEXT NOT NULL,
    accession_number TEXT NOT NULL DEFAULT '',
    form_type TEXT NOT NULL DEFAULT '',
    period_end TEXT NOT NULL DEFAULT '',
    availability_timestamp TEXT NOT NULL DEFAULT '',
    source_url TEXT NOT NULL DEFAULT '',
    source_id TEXT NOT NULL,
    discovery_status TEXT NOT NULL CHECK (
        discovery_status IN (
            'identified_unhydrated', 'cached_hashed', 'discovery_required',
            'terminal_unavailable', 'not_applicable'
        )
    ),
    source_metadata_sha256 TEXT NOT NULL DEFAULT '',
    content_sha256 TEXT NOT NULL DEFAULT '',
    terminal_reason TEXT NOT NULL DEFAULT '',
    requested_metric_ids_json TEXT NOT NULL,
    census_version TEXT NOT NULL,
    census_sha256 TEXT NOT NULL,
    production_eligible INTEGER NOT NULL CHECK (production_eligible = 0),
    calibration_eligible INTEGER NOT NULL CHECK (calibration_eligible = 0),
    created_at_utc TEXT NOT NULL,
    updated_at_utc TEXT NOT NULL,
    UNIQUE (security_id, source_family, source_record_key),
    FOREIGN KEY (security_id) REFERENCES dim_security(security_id),
    FOREIGN KEY (source_id) REFERENCES source_registry(source_id),
    CHECK ((discovery_status = 'cached_hashed' AND length(content_sha256) = 64)
           OR discovery_status <> 'cached_hashed'),
    CHECK ((discovery_status = 'terminal_unavailable' AND terminal_reason <> '')
           OR discovery_status <> 'terminal_unavailable')
);

CREATE INDEX IF NOT EXISTS idx_specialized_metric_cohort
    ON bridge_specialized_metric_cohort(cohort_id, coverage_tier, metric_id);
CREATE INDEX IF NOT EXISTS idx_specialized_applicability_status
    ON bridge_specialized_metric_applicability(cohort_id, applicability_status, metric_id, ticker);
CREATE INDEX IF NOT EXISTS idx_specialized_source_status
    ON fact_specialized_source_census(source_family, discovery_status, cohort_id, ticker);
CREATE INDEX IF NOT EXISTS idx_specialized_source_availability
    ON fact_specialized_source_census(ticker, availability_timestamp, source_family);
"""


SPECIALIZED_PARSER_LEDGER_SQL = r"""
CREATE TABLE IF NOT EXISTS fact_specialized_document (
    content_sha256 TEXT PRIMARY KEY CHECK (length(content_sha256) = 64),
    storage_path TEXT NOT NULL,
    byte_size INTEGER NOT NULL CHECK (byte_size >= 0),
    media_type TEXT NOT NULL,
    source_timestamp TEXT NOT NULL,
    hydration_status TEXT NOT NULL CHECK (
        hydration_status IN ('cached_hashed', 'compiled', 'terminal_unavailable')
    ),
    compiled_text_sha256 TEXT NOT NULL DEFAULT '',
    decoder_version TEXT NOT NULL DEFAULT '',
    created_at_utc TEXT NOT NULL,
    updated_at_utc TEXT NOT NULL,
    CHECK ((hydration_status = 'compiled' AND length(compiled_text_sha256) = 64)
           OR hydration_status <> 'compiled')
);

CREATE TABLE IF NOT EXISTS bridge_specialized_source_document (
    census_key TEXT NOT NULL,
    content_sha256 TEXT NOT NULL,
    relationship TEXT NOT NULL CHECK (relationship IN ('primary', 'exhibit', 'attachment', 'mirror')),
    created_at_utc TEXT NOT NULL,
    PRIMARY KEY (census_key, content_sha256),
    FOREIGN KEY (census_key) REFERENCES fact_specialized_source_census(census_key) ON DELETE CASCADE,
    FOREIGN KEY (content_sha256) REFERENCES fact_specialized_document(content_sha256)
);

CREATE TABLE IF NOT EXISTS fact_specialized_parser_work (
    work_key TEXT PRIMARY KEY,
    content_sha256 TEXT NOT NULL,
    adapter_id TEXT NOT NULL,
    requested_metric_ids_json TEXT NOT NULL,
    requested_metric_set_sha256 TEXT NOT NULL,
    work_status TEXT NOT NULL CHECK (
        work_status IN ('planned', 'running', 'completed', 'failed', 'skipped_terminal')
    ),
    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0 AND attempt_count <= 2),
    resume_parent_work_key TEXT,
    started_at_utc TEXT,
    completed_at_utc TEXT,
    error_message TEXT NOT NULL DEFAULT '',
    run_id TEXT,
    created_at_utc TEXT NOT NULL,
    updated_at_utc TEXT NOT NULL,
    FOREIGN KEY (content_sha256) REFERENCES fact_specialized_document(content_sha256),
    FOREIGN KEY (resume_parent_work_key) REFERENCES fact_specialized_parser_work(work_key),
    FOREIGN KEY (run_id) REFERENCES pipeline_runs(run_id)
);

CREATE TABLE IF NOT EXISTS fact_specialized_metric_candidate (
    candidate_key TEXT PRIMARY KEY,
    work_key TEXT NOT NULL,
    security_id INTEGER NOT NULL,
    ticker TEXT NOT NULL COLLATE NOCASE,
    metric_id TEXT NOT NULL,
    reported_value_text TEXT NOT NULL,
    numeric_value REAL,
    unit TEXT NOT NULL,
    period_start TEXT NOT NULL DEFAULT '',
    period_end TEXT NOT NULL,
    availability_timestamp TEXT NOT NULL,
    dimension_json TEXT NOT NULL DEFAULT '{}',
    definition_variant TEXT NOT NULL,
    evidence_locator_json TEXT NOT NULL,
    evidence_sha256 TEXT NOT NULL,
    candidate_status TEXT NOT NULL CHECK (
        candidate_status IN ('extracted', 'review_required', 'rejected')
    ),
    rejection_reason TEXT NOT NULL DEFAULT '',
    created_at_utc TEXT NOT NULL,
    FOREIGN KEY (work_key) REFERENCES fact_specialized_parser_work(work_key),
    FOREIGN KEY (security_id) REFERENCES dim_security(security_id),
    FOREIGN KEY (metric_id) REFERENCES dim_specialized_metric(metric_id),
    CHECK (availability_timestamp >= period_end)
);

CREATE TABLE IF NOT EXISTS fact_specialized_metric_observation (
    observation_key TEXT PRIMARY KEY,
    candidate_key TEXT NOT NULL,
    security_id INTEGER NOT NULL,
    ticker TEXT NOT NULL COLLATE NOCASE,
    metric_id TEXT NOT NULL,
    numeric_value REAL NOT NULL,
    unit TEXT NOT NULL,
    period_start TEXT NOT NULL DEFAULT '',
    period_end TEXT NOT NULL,
    availability_timestamp TEXT NOT NULL,
    dimension_json TEXT NOT NULL DEFAULT '{}',
    definition_variant TEXT NOT NULL,
    observation_status TEXT NOT NULL CHECK (
        observation_status IN ('accepted', 'conflicted', 'superseded', 'rejected')
    ),
    production_weight REAL NOT NULL CHECK (production_weight = 0),
    scoring_eligible INTEGER NOT NULL CHECK (scoring_eligible = 0),
    calibration_eligible INTEGER NOT NULL CHECK (calibration_eligible = 0),
    policy_version TEXT NOT NULL,
    policy_sha256 TEXT NOT NULL,
    created_at_utc TEXT NOT NULL,
    updated_at_utc TEXT NOT NULL,
    FOREIGN KEY (candidate_key) REFERENCES fact_specialized_metric_candidate(candidate_key),
    FOREIGN KEY (security_id) REFERENCES dim_security(security_id),
    FOREIGN KEY (metric_id) REFERENCES dim_specialized_metric(metric_id),
    CHECK (availability_timestamp >= period_end)
);

CREATE INDEX IF NOT EXISTS idx_specialized_work_status
    ON fact_specialized_parser_work(work_status, content_sha256);
CREATE INDEX IF NOT EXISTS idx_specialized_candidate_lookup
    ON fact_specialized_metric_candidate(metric_id, ticker, period_end, availability_timestamp);
CREATE INDEX IF NOT EXISTS idx_specialized_observation_lookup
    ON fact_specialized_metric_observation(metric_id, ticker, period_end, availability_timestamp);
"""


SPECIALIZED_EXTRACTION_CONTRACT_SQL = (
    SPECIALIZED_METRIC_SQL
    + SPECIALIZED_SOURCE_CENSUS_SQL
    + SPECIALIZED_PARSER_LEDGER_SQL
)


MIGRATIONS: tuple[tuple[int, str, str], ...] = (
    (1, "basic_materials_foundation", FOUNDATION_SQL),
    (2, "basic_materials_historical_reconciliation", HISTORICAL_RECONCILIATION_SQL),
    (3, "basic_materials_adjusted_market_data", MARKET_DATA_SQL),
    (4, "basic_materials_financial_contract", FINANCIAL_CONTRACT_SQL),
    (5, "basic_materials_financial_ingestion", FINANCIAL_INGESTION_SQL),
    (6, "basic_materials_financial_performance_indexes", FINANCIAL_PERFORMANCE_SQL),
    (7, "basic_materials_financial_source_and_security_ratio_remediation", FINANCIAL_REMEDIATION_SQL),
    (8, "basic_materials_specialized_extraction_contract", SPECIALIZED_EXTRACTION_CONTRACT_SQL),
)


def connect(
    database_path: str | Path,
    timeout_seconds: float = 30.0,
    *,
    read_only: bool = False,
) -> sqlite3.Connection:
    path = Path(database_path).expanduser().resolve(strict=False)
    if read_only:
        if not path.is_file():
            raise FileNotFoundError(f"Database not found: {path}")
        conn = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True, timeout=timeout_seconds)
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path, timeout=timeout_seconds)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute(f"PRAGMA busy_timeout = {int(timeout_seconds * 1000)}")
    if read_only:
        conn.execute("PRAGMA query_only = ON")
    else:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
    return conn


def _table_names(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
    ).fetchall()
    return {str(row[0]) for row in rows}


def _execute_statements(conn: sqlite3.Connection, sql: str) -> None:
    for statement in sql.split(";"):
        stripped = statement.strip()
        if stripped:
            conn.execute(stripped)


def migration_checksum(sql: str) -> str:
    return hashlib.sha256(sql.encode("utf-8")).hexdigest()


def assert_database_identity(
    conn: sqlite3.Connection,
    *,
    allow_older_schema: bool = False,
) -> sqlite3.Row:
    tables = _table_names(conn)
    if "sector_database_identity" not in tables:
        raise DatabaseIdentityError("Database has no Basic Materials identity table")
    rows = conn.execute("SELECT * FROM sector_database_identity").fetchall()
    if len(rows) != 1:
        raise DatabaseIdentityError(f"Expected one database identity row, found {len(rows)}")
    identity = rows[0]
    expected = (MODEL_FAMILY, SECTOR, SCHEMA_OWNER)
    actual = (identity["model_family"], identity["sector"], identity["schema_owner"])
    if actual != expected:
        raise DatabaseIdentityError(f"Database identity mismatch: expected {expected}, got {actual}")
    actual_version = int(identity["schema_version"])
    version_is_valid = (
        1 <= actual_version <= SCHEMA_VERSION
        if allow_older_schema
        else actual_version == SCHEMA_VERSION
    )
    if not version_is_valid:
        raise DatabaseIdentityError(
            f"Database schema version is {identity['schema_version']}, expected {SCHEMA_VERSION}"
        )
    return identity


def _validate_migration_ledger(conn: sqlite3.Connection) -> None:
    tables = _table_names(conn)
    if "schema_migrations" not in tables:
        raise MigrationError("Identified database has no migration ledger")
    expected = {version: (name, migration_checksum(sql)) for version, name, sql in MIGRATIONS}
    rows = conn.execute("SELECT version, name, checksum FROM schema_migrations ORDER BY version").fetchall()
    actual_versions = {int(row["version"]) for row in rows}
    unknown = actual_versions - set(expected)
    if unknown:
        raise MigrationError(f"Database contains unknown migration versions: {sorted(unknown)}")
    for row in rows:
        expected_name, expected_checksum = expected[int(row["version"])]
        if row["name"] != expected_name or row["checksum"] != expected_checksum:
            raise MigrationError(f"Migration checksum mismatch at version {row['version']}")


def init_db(conn: sqlite3.Connection) -> dict[str, Any]:
    """Initialize an empty database or verify and migrate an owned database."""

    initial_tables = _table_names(conn)
    if initial_tables:
        if "sector_database_identity" not in initial_tables:
            raise DatabaseIdentityError(
                "Refusing to initialize a non-empty database without a Basic Materials identity"
            )
        assert_database_identity(conn, allow_older_schema=True)
        _validate_migration_ledger(conn)

    applied: list[int] = []
    started_at = utc_now()
    conn.execute("BEGIN IMMEDIATE")
    try:
        for version, name, sql in MIGRATIONS:
            existing = None
            if "schema_migrations" in _table_names(conn):
                existing = conn.execute(
                    "SELECT name, checksum FROM schema_migrations WHERE version = ?", (version,)
                ).fetchone()
            checksum = migration_checksum(sql)
            if existing:
                if existing["name"] != name or existing["checksum"] != checksum:
                    raise MigrationError(f"Migration checksum mismatch at version {version}")
                continue
            _execute_statements(conn, sql)
            conn.execute(
                "INSERT INTO schema_migrations(version, name, checksum, applied_at_utc) VALUES (?, ?, ?, ?)",
                (version, name, checksum, started_at),
            )
            applied.append(version)

        conn.execute(
            """
            INSERT INTO sector_database_identity (
                identity_id, model_family, sector, schema_owner, schema_version, created_at_utc
            ) VALUES (1, ?, ?, ?, ?, ?)
            ON CONFLICT(identity_id) DO UPDATE SET
                schema_version = excluded.schema_version
            """,
            (MODEL_FAMILY, SECTOR, SCHEMA_OWNER, SCHEMA_VERSION, started_at),
        )
        conn.execute(
            """
            INSERT INTO model_control_state (
                identity_id, promotion_state, portfolio_candidate_gate, oos_score_valid_flag,
                current_universe_is_survivorship_corrected, current_universe_calibration_eligible,
                updated_at_utc
            ) VALUES (1, 'shadow_monitor', 0, 0, 0, 0, ?)
            ON CONFLICT(identity_id) DO UPDATE SET updated_at_utc = excluded.updated_at_utc
            """,
            (started_at,),
        )
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        conn.commit()
    except Exception:
        conn.rollback()
        raise

    identity = assert_database_identity(conn)
    _validate_migration_ledger(conn)
    return {
        "model_family": identity["model_family"],
        "sector": identity["sector"],
        "schema_version": int(identity["schema_version"]),
        "migrations_applied": applied,
    }


def start_run(
    conn: sqlite3.Connection,
    *,
    stage: str,
    command: str,
    database_path: str | Path,
    input_path: str | Path | None = None,
    input_sha256: str | None = None,
    input_row_count: int | None = None,
    details: dict[str, Any] | None = None,
) -> str:
    run_id = str(uuid.uuid4())
    conn.execute(
        """
        INSERT INTO pipeline_runs (
            run_id, stage, command, status, database_path, input_path, input_sha256,
            input_row_count, started_at_utc, details_json
        ) VALUES (?, ?, ?, 'running', ?, ?, ?, ?, ?, ?)
        """,
        (
            run_id,
            stage,
            command,
            str(Path(database_path).resolve(strict=False)),
            str(Path(input_path).resolve()) if input_path else None,
            input_sha256,
            input_row_count,
            utc_now(),
            json.dumps(details or {}, sort_keys=True),
        ),
    )
    conn.commit()
    return run_id


def finish_run(
    conn: sqlite3.Connection,
    run_id: str,
    *,
    succeeded: bool,
    details: dict[str, Any] | None = None,
    error_message: str | None = None,
) -> None:
    cursor = conn.execute(
        """
        UPDATE pipeline_runs
        SET status = ?, completed_at_utc = ?, details_json = ?, error_message = ?
        WHERE run_id = ? AND status = 'running'
        """,
        (
            "succeeded" if succeeded else "failed",
            utc_now(),
            json.dumps(details or {}, sort_keys=True),
            error_message,
            run_id,
        ),
    )
    if cursor.rowcount != 1:
        conn.rollback()
        raise RuntimeError(f"Run {run_id} is missing or no longer running")
    conn.commit()


def record_data_quality_issues(
    conn: sqlite3.Connection,
    issues: Iterable[dict[str, Any]],
    *,
    run_id: str | None = None,
) -> int:
    count = 0
    for issue in issues:
        conn.execute(
            """
            INSERT INTO data_quality_issues (
                run_id, stage, severity, issue_code, ticker, message, details_json, created_at_utc
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                issue["stage"],
                issue["severity"],
                issue["issue_code"],
                issue.get("ticker"),
                issue["message"],
                json.dumps(issue.get("details", {}), sort_keys=True),
                utc_now(),
            ),
        )
        count += 1
    return count


def database_counts(conn: sqlite3.Connection) -> dict[str, int]:
    assert_database_identity(conn)
    tables = (
        "source_registry",
        "raw_source_payloads",
        "dim_company",
        "dim_security",
        "dim_identifier",
        "dim_ticker_alias",
        "dim_basic_materials_taxonomy",
        "dim_universe_membership",
        "fact_security_event",
        "fact_terminal_event_reconciliation",
        "dim_market_instrument",
        "bridge_market_instrument_role",
        "fact_market_provider_snapshot",
        "fact_adjusted_price_bar",
        "fact_corporate_action",
        "dim_trading_calendar_session",
        "fact_market_data_coverage",
        "dim_terminal_return_rule",
        "fact_terminal_return_calculation",
        "feature_market_technical",
        "dim_financial_metric",
        "bridge_financial_metric_concept",
        "dim_issuer_reporting_profile",
        "dim_financial_profile_resolution",
        "fact_financial_ingestion_snapshot",
        "fact_sec_filing",
        "fact_sec_xbrl_fact_raw",
        "fact_fx_rate",
        "fact_financial_statement_canonical",
        "feature_financial_statement",
        "fact_financial_data_coverage",
        "fact_financial_normalization_issue",
        "dim_security_share_ratio",
        "dim_specialized_metric",
        "bridge_specialized_metric_cohort",
        "bridge_specialized_metric_operand",
        "bridge_specialized_metric_applicability",
        "fact_specialized_source_census",
        "fact_specialized_document",
        "bridge_specialized_source_document",
        "fact_specialized_parser_work",
        "fact_specialized_metric_candidate",
        "fact_specialized_metric_observation",
    )
    return {table: int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]) for table in tables}
