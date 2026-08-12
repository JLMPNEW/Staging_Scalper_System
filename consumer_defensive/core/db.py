from __future__ import annotations

import re
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from itertools import count
from pathlib import Path
from typing import Iterator

from dedicated_parser.schema import SCHEMA_SQL as DEDICATED_PARSER_SCHEMA_SQL


SAFE_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
TRANSIENT_SQLITE_MARKERS = (
    "database is locked",
    "database table is locked",
    "unable to open database file",
    "readonly database",
)

SCHEMA_SQL = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS sector_database_identity (
    identity_id INTEGER PRIMARY KEY CHECK(identity_id = 1),
    model_family TEXT NOT NULL CHECK(model_family = 'consumer_defensive'),
    internal_sector TEXT NOT NULL CHECK(internal_sector = 'Consumer Defensive'),
    schema_owner TEXT NOT NULL CHECK(schema_owner = 'consumer_defensive'),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

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
    subsector_scope TEXT NOT NULL DEFAULT 'consumer_defensive'
        CHECK(subsector_scope = 'consumer_defensive'),
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
    FOREIGN KEY(source_id) REFERENCES source_registry(source_id) ON DELETE RESTRICT
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
    FOREIGN KEY(source_id) REFERENCES source_registry(source_id) ON DELETE RESTRICT,
    FOREIGN KEY(ingestion_run_id) REFERENCES ingestion_runs(ingestion_run_id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS dim_company (
    company_id INTEGER PRIMARY KEY AUTOINCREMENT,
    primary_ticker TEXT NOT NULL UNIQUE,
    cik TEXT,
    company_name TEXT NOT NULL,
    issuer_domicile TEXT,
    reporting_currency TEXT,
    universe_status TEXT NOT NULL DEFAULT 'candidate',
    is_active INTEGER NOT NULL DEFAULT 1 CHECK(is_active IN (0, 1)),
    data_quality_status TEXT,
    first_seen_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS dim_security (
    security_id INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id INTEGER NOT NULL,
    ticker TEXT NOT NULL,
    provider_price_symbol TEXT,
    exchange TEXT,
    listing_country TEXT,
    security_type TEXT,
    adr_ads_flag INTEGER NOT NULL DEFAULT 0 CHECK(adr_ads_flag IN (0, 1)),
    listing_status TEXT,
    is_primary_listing INTEGER NOT NULL DEFAULT 1 CHECK(is_primary_listing IN (0, 1)),
    currency TEXT,
    listing_start_date TEXT,
    listing_end_date TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(company_id) REFERENCES dim_company(company_id) ON DELETE CASCADE,
    UNIQUE(ticker, exchange, listing_start_date)
);

CREATE TABLE IF NOT EXISTS dim_identifier (
    identifier_id INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id INTEGER NOT NULL,
    security_id INTEGER,
    identifier_type TEXT NOT NULL,
    identifier_value TEXT NOT NULL,
    source_id TEXT,
    valid_from TEXT,
    valid_to TEXT,
    confidence REAL NOT NULL DEFAULT 1.0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(company_id) REFERENCES dim_company(company_id) ON DELETE CASCADE,
    FOREIGN KEY(security_id) REFERENCES dim_security(security_id) ON DELETE CASCADE,
    FOREIGN KEY(source_id) REFERENCES source_registry(source_id) ON DELETE SET NULL,
    UNIQUE(identifier_type, identifier_value, valid_from)
);

CREATE TABLE IF NOT EXISTS dim_company_alias (
    alias_id INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id INTEGER NOT NULL,
    alias_raw TEXT NOT NULL,
    alias_norm TEXT NOT NULL,
    source_id TEXT,
    confidence REAL NOT NULL DEFAULT 1.0,
    is_manual INTEGER NOT NULL DEFAULT 0 CHECK(is_manual IN (0, 1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(company_id) REFERENCES dim_company(company_id) ON DELETE CASCADE,
    FOREIGN KEY(source_id) REFERENCES source_registry(source_id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS dim_consumer_defensive_taxonomy (
    taxonomy_id INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id INTEGER,
    security_id INTEGER,
    ticker TEXT NOT NULL,
    model_family TEXT NOT NULL DEFAULT 'consumer_defensive'
        CHECK(model_family = 'consumer_defensive'),
    sector TEXT NOT NULL DEFAULT 'Consumer Defensive'
        CHECK(sector = 'Consumer Defensive'),
    portfolio_sector TEXT NOT NULL DEFAULT 'Consumer Staples'
        CHECK(portfolio_sector = 'Consumer Staples'),
    calibration_cohort_id TEXT NOT NULL,
    calibration_cohort TEXT NOT NULL,
    applicability_subtype TEXT,
    taxonomy_confidence REAL NOT NULL DEFAULT 0.0,
    taxonomy_source TEXT,
    business_cohort_override_flag INTEGER NOT NULL DEFAULT 0,
    analyst_reviewed INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(company_id) REFERENCES dim_company(company_id) ON DELETE CASCADE,
    FOREIGN KEY(security_id) REFERENCES dim_security(security_id) ON DELETE CASCADE,
    UNIQUE(ticker, model_family)
);

CREATE TABLE IF NOT EXISTS dim_universe_membership (
    membership_id INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id INTEGER,
    security_id INTEGER,
    ticker TEXT NOT NULL,
    model_family TEXT NOT NULL DEFAULT 'consumer_defensive'
        CHECK(model_family = 'consumer_defensive'),
    membership_source_id TEXT,
    membership_basis TEXT NOT NULL,
    recognized_vehicle TEXT,
    start_date TEXT NOT NULL,
    end_date TEXT,
    membership_status TEXT NOT NULL,
    is_current_member INTEGER NOT NULL DEFAULT 0 CHECK(is_current_member IN (0, 1)),
    point_in_time_flag INTEGER NOT NULL DEFAULT 1 CHECK(point_in_time_flag IN (0, 1)),
    live_investable_flag INTEGER NOT NULL DEFAULT 0 CHECK(live_investable_flag IN (0, 1)),
    historical_calibration_eligible_flag INTEGER NOT NULL DEFAULT 0
        CHECK(historical_calibration_eligible_flag IN (0, 1)),
    confidence REAL NOT NULL DEFAULT 1.0,
    reason TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(company_id) REFERENCES dim_company(company_id) ON DELETE CASCADE,
    FOREIGN KEY(security_id) REFERENCES dim_security(security_id) ON DELETE CASCADE,
    FOREIGN KEY(membership_source_id) REFERENCES source_registry(source_id) ON DELETE SET NULL,
    UNIQUE(ticker, membership_source_id, start_date)
);

CREATE TABLE IF NOT EXISTS dim_specialized_metric (
    metric_id TEXT PRIMARY KEY,
    registry_version TEXT NOT NULL,
    cohorts_json TEXT NOT NULL,
    applicability_subtypes_json TEXT NOT NULL,
    unit_family TEXT NOT NULL,
    direction_hint TEXT NOT NULL,
    purpose TEXT NOT NULL,
    production_status TEXT NOT NULL,
    production_weight REAL NOT NULL DEFAULT 0.0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS fact_security_event (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    security_id INTEGER,
    ticker TEXT NOT NULL,
    event_type TEXT NOT NULL,
    event_date TEXT NOT NULL,
    last_trade_date TEXT,
    successor_ticker TEXT,
    cash_consideration REAL,
    stock_consideration_json TEXT,
    terminal_value REAL,
    terminal_value_currency TEXT,
    survivorship_complete INTEGER NOT NULL DEFAULT 0 CHECK(survivorship_complete IN (0, 1)),
    source_id TEXT,
    source_detail TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(security_id) REFERENCES dim_security(security_id) ON DELETE SET NULL,
    FOREIGN KEY(source_id) REFERENCES source_registry(source_id) ON DELETE SET NULL,
    UNIQUE(ticker, event_type, event_date)
);

CREATE TABLE IF NOT EXISTS fact_terminal_event_reconciliation (
    ticker TEXT PRIMARY KEY,
    security_id INTEGER NOT NULL,
    event_type TEXT NOT NULL,
    economic_event_date TEXT NOT NULL,
    last_trade_date TEXT NOT NULL,
    provider_last_quoted_date TEXT NOT NULL,
    terminal_type TEXT NOT NULL,
    cash_consideration REAL,
    cash_currency TEXT NOT NULL,
    successor_ticker TEXT,
    successor_share_ratio REAL,
    successor_security_type TEXT,
    successor_reference_date TEXT,
    successor_price_source_id TEXT,
    successor_provider_symbol TEXT,
    contingent_right_id TEXT,
    contingent_right_units REAL,
    contingent_max_cash REAL,
    contingent_status TEXT,
    fixed_terminal_value REAL,
    terminal_value_method TEXT NOT NULL,
    survivorship_complete INTEGER NOT NULL CHECK(survivorship_complete IN (0, 1)),
    calibration_eligible INTEGER NOT NULL CHECK(calibration_eligible IN (0, 1)),
    reconciliation_status TEXT NOT NULL,
    primary_source_url TEXT NOT NULL,
    secondary_source_url TEXT,
    source_document_date TEXT,
    notes TEXT,
    source_id TEXT NOT NULL,
    reviewed_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(security_id) REFERENCES dim_security(security_id) ON DELETE RESTRICT,
    FOREIGN KEY(source_id) REFERENCES source_registry(source_id) ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS fact_price_ohlcv (
    ticker TEXT NOT NULL,
    bar_date TEXT NOT NULL,
    source_id TEXT NOT NULL,
    open REAL,
    high REAL,
    low REAL,
    close REAL,
    adjusted_close REAL,
    volume REAL,
    dividend REAL,
    split_factor REAL,
    total_return_basis TEXT,
    source_timestamp TEXT,
    created_at TEXT NOT NULL,
    PRIMARY KEY(ticker, bar_date, source_id),
    FOREIGN KEY(source_id) REFERENCES source_registry(source_id) ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS fact_corporate_action (
    ticker TEXT NOT NULL,
    action_date TEXT NOT NULL,
    source_id TEXT NOT NULL,
    action_type TEXT NOT NULL,
    action_value REAL,
    action_currency TEXT,
    details_json TEXT,
    created_at TEXT NOT NULL,
    PRIMARY KEY(ticker, action_date, source_id, action_type),
    FOREIGN KEY(source_id) REFERENCES source_registry(source_id) ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS fact_sec_filing (
    accession_number TEXT PRIMARY KEY,
    company_id INTEGER,
    ticker TEXT NOT NULL,
    cik TEXT,
    form_type TEXT NOT NULL,
    filing_date TEXT NOT NULL,
    accepted_at TEXT,
    report_date TEXT,
    primary_document TEXT,
    source_id TEXT NOT NULL,
    source_url TEXT,
    content_sha256 TEXT,
    metadata_quality_flags_json TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(company_id) REFERENCES dim_company(company_id) ON DELETE SET NULL,
    FOREIGN KEY(source_id) REFERENCES source_registry(source_id) ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS bridge_sec_filing_company (
    accession_number TEXT NOT NULL,
    issuer_company_id INTEGER NOT NULL,
    issuer_ticker TEXT NOT NULL,
    issuer_cik TEXT NOT NULL,
    relationship TEXT NOT NULL
        CHECK(relationship IN ('associated_via_submissions')),
    relationship_evidence TEXT NOT NULL,
    form_type TEXT NOT NULL,
    filing_date TEXT NOT NULL,
    accepted_at TEXT,
    report_date TEXT,
    primary_document TEXT,
    source_id TEXT NOT NULL,
    source_url TEXT,
    association_status TEXT NOT NULL DEFAULT 'active'
        CHECK(association_status IN ('active','retired')),
    retirement_effective_asof TEXT,
    retirement_reason TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(accession_number, issuer_company_id),
    FOREIGN KEY(accession_number) REFERENCES fact_sec_filing(accession_number) ON DELETE CASCADE,
    FOREIGN KEY(issuer_company_id) REFERENCES dim_company(company_id) ON DELETE CASCADE,
    FOREIGN KEY(source_id) REFERENCES source_registry(source_id) ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS fact_sec_xbrl_fact_raw (
    raw_fact_id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL,
    cik TEXT,
    accession_number TEXT,
    taxonomy TEXT NOT NULL,
    concept TEXT NOT NULL,
    value_text TEXT,
    numeric_value REAL,
    unit TEXT,
    period_start TEXT,
    period_end TEXT,
    filed_date TEXT,
    accepted_at TEXT,
    form_type TEXT,
    frame TEXT,
    dimensions_json TEXT,
    source_id TEXT NOT NULL,
    source_detail TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY(accession_number) REFERENCES fact_sec_filing(accession_number) ON DELETE SET NULL,
    FOREIGN KEY(source_id) REFERENCES source_registry(source_id) ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS fact_financial_statement_canonical (
    canonical_fact_id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL,
    canonical_metric TEXT NOT NULL,
    canonical_component TEXT NOT NULL DEFAULT 'total',
    accession_number TEXT,
    taxonomy TEXT,
    source_concept TEXT,
    statement_type TEXT NOT NULL,
    period_start TEXT,
    period_end TEXT NOT NULL,
    accepted_at TEXT NOT NULL,
    frequency TEXT,
    value REAL,
    reported_value REAL,
    reported_currency TEXT,
    value_usd REAL,
    fx_rate REAL,
    source_raw_fact_id INTEGER,
    source_id TEXT NOT NULL,
    definition_version TEXT,
    quality_status TEXT,
    selection_method TEXT,
    sign_normalization_method TEXT NOT NULL DEFAULT 'none',
    quality_flags_json TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL,
    FOREIGN KEY(source_raw_fact_id) REFERENCES fact_sec_xbrl_fact_raw(raw_fact_id) ON DELETE SET NULL,
    FOREIGN KEY(source_id) REFERENCES source_registry(source_id) ON DELETE RESTRICT,
    UNIQUE(ticker, canonical_metric, canonical_component, period_start, period_end, accepted_at, source_id)
);

CREATE TABLE IF NOT EXISTS fact_fx_rate (
    base_currency TEXT NOT NULL,
    quote_currency TEXT NOT NULL,
    rate_date TEXT NOT NULL,
    source_id TEXT NOT NULL,
    rate REAL NOT NULL,
    raw_rate REAL,
    quality_status TEXT NOT NULL DEFAULT 'usable',
    quality_reason TEXT,
    created_at TEXT NOT NULL,
    PRIMARY KEY(base_currency, quote_currency, rate_date, source_id),
    FOREIGN KEY(source_id) REFERENCES source_registry(source_id) ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS fact_sec_ownership_transaction (
    transaction_id TEXT PRIMARY KEY,
    ticker TEXT NOT NULL,
    owner_cik TEXT,
    transaction_date TEXT,
    filed_at TEXT NOT NULL,
    transaction_code TEXT,
    shares REAL,
    price REAL,
    acquired_disposed TEXT,
    source_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY(source_id) REFERENCES source_registry(source_id) ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS fact_13f_positioning (
    ticker TEXT NOT NULL,
    asof_date TEXT NOT NULL,
    publication_date TEXT NOT NULL,
    source_id TEXT NOT NULL,
    institutional_value REAL,
    institutional_shares REAL,
    owner_count INTEGER,
    created_at TEXT NOT NULL,
    PRIMARY KEY(ticker, asof_date, source_id),
    FOREIGN KEY(source_id) REFERENCES source_registry(source_id) ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS fact_short_interest (
    ticker TEXT NOT NULL,
    settlement_date TEXT NOT NULL,
    publication_date TEXT NOT NULL,
    source_id TEXT NOT NULL,
    short_interest REAL,
    short_float_pct REAL,
    days_to_cover REAL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(ticker, settlement_date, source_id),
    FOREIGN KEY(source_id) REFERENCES source_registry(source_id) ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS fact_borrow_snapshot (
    ticker TEXT NOT NULL,
    asof_date TEXT NOT NULL,
    source_id TEXT NOT NULL,
    borrow_fee REAL,
    available_shares REAL,
    source_birthdate TEXT,
    created_at TEXT NOT NULL,
    PRIMARY KEY(ticker, asof_date, source_id),
    FOREIGN KEY(source_id) REFERENCES source_registry(source_id) ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS fact_specialized_metric_observation (
    observation_id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL,
    metric_id TEXT NOT NULL,
    period_start TEXT,
    period_end TEXT NOT NULL,
    accepted_at TEXT NOT NULL,
    numeric_value REAL,
    unit TEXT,
    definition_version TEXT NOT NULL,
    applicability_status TEXT NOT NULL,
    evidence_status TEXT NOT NULL,
    evidence_key TEXT,
    source_id TEXT NOT NULL,
    source_document TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY(metric_id) REFERENCES dim_specialized_metric(metric_id) ON DELETE RESTRICT,
    FOREIGN KEY(source_id) REFERENCES source_registry(source_id) ON DELETE RESTRICT,
    UNIQUE(ticker, metric_id, period_end, accepted_at, definition_version, source_id)
);

CREATE TABLE IF NOT EXISTS feature_market_technical (
    model_family TEXT NOT NULL DEFAULT 'consumer_defensive'
        CHECK(model_family = 'consumer_defensive'),
    ticker TEXT NOT NULL,
    asof_date TEXT NOT NULL,
    source_id TEXT NOT NULL,
    adjusted_close REAL,
    avg_dollar_volume_63d REAL,
    residual_momentum_63d REAL,
    residual_momentum_126d REAL,
    realized_volatility_63d REAL,
    downside_volatility_63d REAL,
    max_drawdown_252d REAL,
    history_days INTEGER,
    quality_status TEXT,
    created_at TEXT NOT NULL,
    PRIMARY KEY(model_family, ticker, asof_date, source_id),
    FOREIGN KEY(source_id) REFERENCES source_registry(source_id) ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS feature_financial_statement (
    model_family TEXT NOT NULL DEFAULT 'consumer_defensive'
        CHECK(model_family = 'consumer_defensive'),
    ticker TEXT NOT NULL,
    asof_date TEXT NOT NULL,
    source_id TEXT NOT NULL,
    revenue_ttm_usd REAL,
    gross_margin REAL,
    operating_margin REAL,
    free_cash_flow_margin REAL,
    return_on_invested_capital REAL,
    net_debt_to_ebitda REAL,
    inventory_turnover REAL,
    basis_period_end TEXT,
    feature_definition_version TEXT NOT NULL DEFAULT 'consumer_defensive_financial_features_v2',
    lineage_json TEXT NOT NULL DEFAULT '{}',
    financial_quality_status TEXT,
    financial_quality_reason TEXT,
    created_at TEXT NOT NULL,
    PRIMARY KEY(model_family, ticker, asof_date, source_id),
    FOREIGN KEY(source_id) REFERENCES source_registry(source_id) ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS feature_positioning (
    model_family TEXT NOT NULL DEFAULT 'consumer_defensive'
        CHECK(model_family = 'consumer_defensive'),
    ticker TEXT NOT NULL,
    asof_date TEXT NOT NULL,
    source_id TEXT NOT NULL,
    insider_net_buying REAL,
    institutional_flow REAL,
    short_float_pct REAL,
    borrow_fee REAL,
    source_birthdate TEXT,
    quality_status TEXT,
    created_at TEXT NOT NULL,
    PRIMARY KEY(model_family, ticker, asof_date, source_id),
    FOREIGN KEY(source_id) REFERENCES source_registry(source_id) ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS feature_scoring_input (
    model_family TEXT NOT NULL DEFAULT 'consumer_defensive'
        CHECK(model_family = 'consumer_defensive'),
    ticker TEXT NOT NULL,
    asof_date TEXT NOT NULL,
    calibration_cohort_id TEXT NOT NULL,
    rank_ready_flag INTEGER NOT NULL DEFAULT 0 CHECK(rank_ready_flag IN (0, 1)),
    review_reason TEXT,
    created_at TEXT NOT NULL,
    PRIMARY KEY(model_family, ticker, asof_date)
);

CREATE TABLE IF NOT EXISTS feature_scoring_component (
    model_family TEXT NOT NULL DEFAULT 'consumer_defensive'
        CHECK(model_family = 'consumer_defensive'),
    ticker TEXT NOT NULL,
    asof_date TEXT NOT NULL,
    component_name TEXT NOT NULL,
    raw_value REAL,
    normalized_value REAL,
    component_score REAL,
    component_weight REAL NOT NULL DEFAULT 0.0,
    availability_status TEXT NOT NULL,
    source_asof_date TEXT,
    quality_status TEXT,
    created_at TEXT NOT NULL,
    PRIMARY KEY(model_family, ticker, asof_date, component_name)
);

CREATE TABLE IF NOT EXISTS feature_scoring_model_output (
    model_family TEXT NOT NULL DEFAULT 'consumer_defensive'
        CHECK(model_family = 'consumer_defensive'),
    ticker TEXT NOT NULL,
    asof_date TEXT NOT NULL,
    source_id TEXT NOT NULL,
    final_score REAL,
    final_rank INTEGER,
    promotion_state TEXT NOT NULL DEFAULT 'deferred',
    portfolio_candidate_gate INTEGER NOT NULL DEFAULT 0 CHECK(portfolio_candidate_gate IN (0, 1)),
    oos_score_valid_flag INTEGER NOT NULL DEFAULT 0 CHECK(oos_score_valid_flag IN (0, 1)),
    model_version TEXT,
    created_at TEXT NOT NULL,
    PRIMARY KEY(model_family, ticker, asof_date, source_id),
    FOREIGN KEY(source_id) REFERENCES source_registry(source_id) ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS data_quality_issues (
    issue_id INTEGER PRIMARY KEY AUTOINCREMENT,
    detected_at TEXT NOT NULL,
    severity TEXT NOT NULL,
    stage TEXT NOT NULL,
    ticker TEXT,
    company_id INTEGER,
    source_id TEXT,
    issue_type TEXT NOT NULL,
    issue_detail TEXT NOT NULL,
    resolution_status TEXT NOT NULL DEFAULT 'open',
    resolution_detail TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(company_id) REFERENCES dim_company(company_id) ON DELETE SET NULL,
    FOREIGN KEY(source_id) REFERENCES source_registry(source_id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_raw_api_responses_source_asof
    ON raw_api_responses(source_id, asof_date);
CREATE INDEX IF NOT EXISTS idx_dim_security_ticker
    ON dim_security(ticker);
CREATE INDEX IF NOT EXISTS idx_dim_identifier_company
    ON dim_identifier(company_id);
CREATE INDEX IF NOT EXISTS idx_dim_company_alias_norm
    ON dim_company_alias(alias_norm);
CREATE INDEX IF NOT EXISTS idx_dim_cd_taxonomy_cohort
    ON dim_consumer_defensive_taxonomy(calibration_cohort_id, ticker);
CREATE INDEX IF NOT EXISTS idx_dim_universe_membership_lookup
    ON dim_universe_membership(ticker, start_date, end_date);
CREATE INDEX IF NOT EXISTS idx_fact_price_ohlcv_ticker_date
    ON fact_price_ohlcv(ticker, bar_date);
CREATE INDEX IF NOT EXISTS idx_cd_terminal_event_status
    ON fact_terminal_event_reconciliation(reconciliation_status, calibration_eligible);
CREATE INDEX IF NOT EXISTS idx_fact_sec_filing_ticker_accepted
    ON fact_sec_filing(ticker, accepted_at);
CREATE INDEX IF NOT EXISTS idx_bridge_sec_filing_company_ticker_accepted
    ON bridge_sec_filing_company(issuer_ticker, relationship, accepted_at, accession_number);
CREATE INDEX IF NOT EXISTS idx_fact_specialized_metric_lookup
    ON fact_specialized_metric_observation(ticker, metric_id, period_end, accepted_at);
CREATE INDEX IF NOT EXISTS idx_data_quality_issues_stage_ticker
    ON data_quality_issues(stage, ticker);
"""

REQUIRED_FOUNDATION_TABLES = frozenset(
    {
        "sector_database_identity",
        "runs",
        "source_registry",
        "ingestion_runs",
        "raw_api_responses",
        "dim_company",
        "dim_security",
        "dim_identifier",
        "dim_company_alias",
        "dim_consumer_defensive_taxonomy",
        "dim_universe_membership",
        "dim_specialized_metric",
        "fact_security_event",
        "fact_terminal_event_reconciliation",
        "fact_price_ohlcv",
        "fact_corporate_action",
        "fact_sec_filing",
        "bridge_sec_filing_company",
        "fact_sec_xbrl_fact_raw",
        "fact_financial_statement_canonical",
        "fact_fx_rate",
        "fact_sec_ownership_transaction",
        "fact_13f_positioning",
        "fact_short_interest",
        "fact_borrow_snapshot",
        "fact_specialized_metric_observation",
        "feature_market_technical",
        "feature_financial_statement",
        "feature_positioning",
        "feature_scoring_input",
        "feature_scoring_component",
        "feature_scoring_model_output",
        "data_quality_issues",
        "sec_parser_run",
        "sec_parser_document_catalog",
        "sec_parser_work_ledger",
        "sec_parser_metric_evidence_shadow",
    }
)
FORBIDDEN_TABLE_FRAGMENTS = (
    "technology",
    "industrial",
    "transportation",
    "defense",
    "machinery",
    "med_device",
    "biotech",
)

_SCHEMA_SAVEPOINT_SEQUENCE = count(1)


def _iter_sql_statements(script: str) -> Iterator[str]:
    """Yield complete SQLite statements without implicit commits."""

    pending: list[str] = []
    for character in script:
        pending.append(character)
        if character != ";":
            continue
        candidate = "".join(pending)
        if sqlite3.complete_statement(candidate):
            statement = candidate.strip()
            if statement:
                yield statement
            pending.clear()

    remainder = "".join(pending).strip()
    uncommented = re.sub(r"--[^\n]*(?:\n|$)", "", remainder)
    uncommented = re.sub(r"/\*.*?\*/", "", uncommented, flags=re.DOTALL)
    if uncommented.strip():
        raise sqlite3.OperationalError(
            "Schema SQL ended with an incomplete statement: "
            f"{remainder[:120]!r}"
        )


def _execute_sql_statements(conn: sqlite3.Connection, script: str) -> None:
    for statement in _iter_sql_statements(script):
        normalized = " ".join(statement.rstrip(";").upper().split())
        if re.fullmatch(r"PRAGMA FOREIGN_KEYS\s*=\s*ON", normalized):
            # This pragma is a no-op inside a transaction. init_db enables it
            # before beginning; other schema calls preserve the current setting.
            continue
        first_token = normalized.partition(" ")[0]
        if first_token in {
            "BEGIN",
            "COMMIT",
            "END",
            "RELEASE",
            "ROLLBACK",
            "SAVEPOINT",
        }:
            raise sqlite3.OperationalError(
                "Schema scripts must not contain transaction-control statements."
            )
        conn.execute(statement)


@contextmanager
def _atomic_schema_transaction(conn: sqlite3.Connection) -> Iterator[None]:
    """Create an atomic DDL boundary without committing a caller transaction."""

    nested = conn.in_transaction
    savepoint = f"consumer_defensive_schema_{next(_SCHEMA_SAVEPOINT_SEQUENCE)}"
    if nested:
        conn.execute(f"SAVEPOINT {savepoint}")
    else:
        conn.execute("BEGIN IMMEDIATE")

    try:
        yield
        if nested:
            conn.execute(f"RELEASE SAVEPOINT {savepoint}")
        else:
            conn.execute("COMMIT")
    except BaseException as exc:
        try:
            if conn.in_transaction:
                if nested:
                    conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
                    conn.execute(f"RELEASE SAVEPOINT {savepoint}")
                else:
                    conn.execute("ROLLBACK")
        except sqlite3.Error as rollback_exc:
            exc.add_note(f"Schema rollback also failed: {rollback_exc}")
        raise


def execute_schema_script(conn: sqlite3.Connection, script: str) -> None:
    """Execute a schema script atomically and preserve caller transaction state."""

    with _atomic_schema_transaction(conn):
        _execute_sql_statements(conn, script)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _is_transient_sqlite_error(exc: sqlite3.OperationalError) -> bool:
    message = str(exc).lower()
    return any(marker in message for marker in TRANSIENT_SQLITE_MARKERS)


def connect(db_path: Path, *, timeout_sec: float = 30.0) -> sqlite3.Connection:
    resolved = db_path.expanduser().resolve()
    resolved.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(resolved), timeout=float(timeout_sec))
    conn.row_factory = sqlite3.Row
    assert_database_ownership(conn)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute(f"PRAGMA busy_timeout = {int(float(timeout_sec) * 1000)}")
    for attempt in range(3):
        try:
            conn.execute("PRAGMA journal_mode = WAL")
            break
        except sqlite3.OperationalError as exc:
            if attempt >= 2 or not _is_transient_sqlite_error(exc):
                break
            time.sleep(0.25 * (attempt + 1))
    return conn


def assert_database_ownership(conn: sqlite3.Connection) -> None:
    """Reject a foreign sector database before changing schema or journal mode."""
    names = {
        str(row[0])
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    if not names:
        return
    forbidden = sorted(
        name
        for name in names
        if any(fragment in name.casefold() for fragment in FORBIDDEN_TABLE_FRAGMENTS)
    )
    if forbidden:
        raise RuntimeError(
            "Refusing to mutate a non-Consumer-Defensive database; foreign tables: "
            + ", ".join(forbidden)
        )
    if "sector_database_identity" in names:
        row = conn.execute(
            "SELECT model_family,internal_sector,schema_owner FROM sector_database_identity WHERE identity_id=1"
        ).fetchone()
        if row is None or tuple(row) != (
            "consumer_defensive", "Consumer Defensive", "consumer_defensive"
        ):
            raise RuntimeError("Consumer Defensive database identity is missing or inconsistent.")
        return
    if "dim_consumer_defensive_taxonomy" not in names:
        raise RuntimeError(
            "Refusing to claim a non-empty unowned database as Consumer Defensive."
        )


def _migrate_canonical_financial_key(conn: sqlite3.Connection) -> None:
    """Preserve distinct fact durations and additive metric components.

    The v1 key omitted ``period_start`` and collapsed annual and quarterly
    observations sharing a fiscal period end. SQLite cannot remove that old
    table-level UNIQUE constraint in place, so existing databases are rebuilt.
    """
    columns = {
        str(row[1])
        for row in conn.execute("PRAGMA table_info(fact_financial_statement_canonical)")
    }
    if not columns or "canonical_component" in columns:
        return
    conn.execute(
        "ALTER TABLE fact_financial_statement_canonical "
        "RENAME TO fact_financial_statement_canonical_v1"
    )
    conn.execute(
        """
        CREATE TABLE fact_financial_statement_canonical (
            canonical_fact_id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT NOT NULL,
            canonical_metric TEXT NOT NULL,
            canonical_component TEXT NOT NULL DEFAULT 'total',
            accession_number TEXT,
            taxonomy TEXT,
            source_concept TEXT,
            statement_type TEXT NOT NULL,
            period_start TEXT,
            period_end TEXT NOT NULL,
            accepted_at TEXT NOT NULL,
            frequency TEXT,
            value REAL,
            reported_value REAL,
            reported_currency TEXT,
            value_usd REAL,
            fx_rate REAL,
            source_raw_fact_id INTEGER,
            source_id TEXT NOT NULL,
            definition_version TEXT,
            quality_status TEXT,
            selection_method TEXT,
            sign_normalization_method TEXT NOT NULL DEFAULT 'none',
            quality_flags_json TEXT NOT NULL DEFAULT '[]',
            created_at TEXT NOT NULL,
            FOREIGN KEY(source_raw_fact_id)
                REFERENCES fact_sec_xbrl_fact_raw(raw_fact_id) ON DELETE SET NULL,
            FOREIGN KEY(source_id) REFERENCES source_registry(source_id) ON DELETE RESTRICT,
            UNIQUE(
                ticker, canonical_metric, canonical_component, period_start,
                period_end, accepted_at, source_id
            )
        )
        """
    )
    conn.execute(
        """
        INSERT INTO fact_financial_statement_canonical(
            canonical_fact_id, ticker, canonical_metric, canonical_component,
            statement_type, period_start, period_end, accepted_at, frequency,
            value, reported_currency, value_usd, fx_rate, source_raw_fact_id,
            source_id, definition_version, quality_status, created_at
        )
        SELECT canonical_fact_id, ticker, canonical_metric, 'total',
               statement_type, period_start, period_end, accepted_at, frequency,
               value, reported_currency, value_usd, fx_rate, source_raw_fact_id,
               source_id, definition_version, quality_status, created_at
        FROM fact_financial_statement_canonical_v1
        """
    )
    conn.execute("DROP TABLE fact_financial_statement_canonical_v1")


def _add_column_if_missing(
    conn: sqlite3.Connection,
    table: str,
    column: str,
    declaration: str,
) -> None:
    columns = {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}
    if column not in columns:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")


def _migrate_financial_semantic_columns(conn: sqlite3.Connection) -> None:
    canonical_columns = {
        "accession_number": "TEXT",
        "taxonomy": "TEXT",
        "source_concept": "TEXT",
        "reported_value": "REAL",
        "selection_method": "TEXT",
        "sign_normalization_method": "TEXT NOT NULL DEFAULT 'none'",
        "quality_flags_json": "TEXT NOT NULL DEFAULT '[]'",
    }
    fx_columns = {
        "raw_rate": "REAL",
        "quality_status": "TEXT NOT NULL DEFAULT 'usable'",
        "quality_reason": "TEXT",
    }
    feature_columns = {
        "basis_period_end": "TEXT",
        "feature_definition_version": "TEXT NOT NULL DEFAULT 'consumer_defensive_financial_features_v2'",
        "lineage_json": "TEXT NOT NULL DEFAULT '{}'",
        "financial_quality_reason": "TEXT",
    }
    for column, declaration in canonical_columns.items():
        _add_column_if_missing(conn, "fact_financial_statement_canonical", column, declaration)
    for column, declaration in fx_columns.items():
        _add_column_if_missing(conn, "fact_fx_rate", column, declaration)
    for column, declaration in feature_columns.items():
        _add_column_if_missing(conn, "feature_financial_statement", column, declaration)
    conn.execute(
        "UPDATE fact_financial_statement_canonical SET reported_value=value WHERE reported_value IS NULL"
    )
    conn.execute("UPDATE fact_fx_rate SET raw_rate=rate WHERE raw_rate IS NULL")


def _migrate_dedicated_parser_columns(conn: sqlite3.Connection) -> None:
    """Apply legacy parser column migrations inside the caller transaction."""

    migrations = {
        "sec_parser_work_ledger": {
            "provider_metadata_json": "TEXT NOT NULL DEFAULT '{}'",
        },
        "sec_parser_normalized_fact_shadow": {
            "concept_metadata_json": "TEXT NOT NULL DEFAULT '{}'",
        },
        "sec_parser_recovery_assessment": {
            "parser_failure_count": "INTEGER NOT NULL DEFAULT 0",
            "missing_cache_filing_count": "INTEGER NOT NULL DEFAULT 0",
            "current_match_mode": "TEXT NOT NULL DEFAULT 'none'",
            "current_evidence_period_end": "TEXT",
            "current_evidence_age_days": "INTEGER",
        },
    }
    for table, columns in migrations.items():
        for column, declaration in columns.items():
            _add_column_if_missing(conn, table, column, declaration)


def _enable_foreign_keys_for_init(conn: sqlite3.Connection) -> None:
    enabled = int(conn.execute("PRAGMA foreign_keys").fetchone()[0])
    if enabled:
        return
    if conn.in_transaction:
        raise RuntimeError(
            "Cannot initialize the Consumer Defensive schema inside a transaction "
            "while SQLite foreign-key enforcement is disabled."
        )
    conn.execute("PRAGMA foreign_keys = ON")
    if int(conn.execute("PRAGMA foreign_keys").fetchone()[0]) != 1:
        raise RuntimeError("Unable to enable SQLite foreign-key enforcement.")


def _assert_foreign_key_integrity(conn: sqlite3.Connection) -> None:
    violations = conn.execute("PRAGMA foreign_key_check").fetchmany(5)
    if violations:
        sample = [tuple(row) for row in violations]
        raise RuntimeError(
            "Consumer Defensive schema has foreign-key violations: "
            f"{sample!r}"
        )


def init_db(conn: sqlite3.Connection) -> None:
    for attempt in range(3):
        try:
            assert_database_ownership(conn)
            _enable_foreign_keys_for_init(conn)
            with _atomic_schema_transaction(conn):
                _execute_sql_statements(conn, SCHEMA_SQL)
                _migrate_canonical_financial_key(conn)
                _migrate_financial_semantic_columns(conn)
                _execute_sql_statements(conn, DEDICATED_PARSER_SCHEMA_SQL)
                _migrate_dedicated_parser_columns(conn)
                now = utc_now()
                conn.execute(
                    """INSERT INTO sector_database_identity(
                           identity_id,model_family,internal_sector,schema_owner,created_at,updated_at
                       ) VALUES(1,'consumer_defensive','Consumer Defensive','consumer_defensive',?,?)
                       ON CONFLICT(identity_id) DO UPDATE SET updated_at=excluded.updated_at""",
                    (now, now),
                )
                validate_foundation_schema(conn)
                _assert_foreign_key_integrity(conn)
            return
        except sqlite3.OperationalError as exc:
            if attempt >= 2 or not _is_transient_sqlite_error(exc):
                raise
            time.sleep(0.5 * (attempt + 1))


def table_names(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
    ).fetchall()
    return [str(row["name"]) for row in rows]


def validate_foundation_schema(conn: sqlite3.Connection) -> None:
    names = set(table_names(conn))
    missing = sorted(REQUIRED_FOUNDATION_TABLES.difference(names))
    if missing:
        raise RuntimeError(f"Consumer Defensive foundation tables missing: {', '.join(missing)}")
    forbidden = sorted(
        name
        for name in names
        if any(fragment in name.casefold() for fragment in FORBIDDEN_TABLE_FRAGMENTS)
    )
    if forbidden:
        raise RuntimeError(f"Cross-sector tables found in Consumer Defensive DB: {', '.join(forbidden)}")
    identity = conn.execute(
        "SELECT model_family,internal_sector,schema_owner FROM sector_database_identity WHERE identity_id=1"
    ).fetchone()
    if identity is None or tuple(identity) != (
        "consumer_defensive", "Consumer Defensive", "consumer_defensive"
    ):
        raise RuntimeError("Consumer Defensive database identity contract is invalid.")


def start_run(
    conn: sqlite3.Connection,
    *,
    run_type: str,
    input_path: Path | str | None = None,
) -> int:
    now = utc_now()
    with conn:
        cursor = conn.execute(
            """
            INSERT INTO runs(run_type, started_at, status, input_path, created_at)
            VALUES (?, ?, 'running', ?, ?)
            """,
            (run_type, now, str(input_path or ""), now),
        )
    return require_lastrowid(cursor, context="create Consumer Defensive run record")


def require_lastrowid(cursor: sqlite3.Cursor, *, context: str) -> int:
    """Return an inserted row id or fail with an actionable invariant error."""
    if cursor.lastrowid is None:
        raise RuntimeError(f"Failed to {context}: SQLite returned no row id.")
    return int(cursor.lastrowid)


def finish_run(
    conn: sqlite3.Connection,
    *,
    run_id: int,
    status: str,
    row_count: int = 0,
    message: str = "",
) -> None:
    with conn:
        conn.execute(
            """
            UPDATE runs
            SET completed_at = ?, status = ?, row_count = ?, message = ?
            WHERE run_id = ?
            """,
            (utc_now(), status, int(row_count), str(message or ""), int(run_id)),
        )


def count_rows(conn: sqlite3.Connection, table_name: str) -> int:
    if not SAFE_IDENTIFIER_RE.fullmatch(table_name):
        raise ValueError(f"Unsafe table name: {table_name}")
    row = conn.execute(f"SELECT COUNT(*) AS n FROM {table_name}").fetchone()
    return int(row["n"]) if row is not None else 0
