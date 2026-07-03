from __future__ import annotations

import re
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path


SAFE_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
TRANSIENT_SQLITE_MARKERS = (
    "database is locked",
    "database table is locked",
    "unable to open database file",
    "readonly database",
)


def _xbrl_concept(
    taxonomy: str,
    concept_name: str,
    canonical_metric: str,
    financial_statement: str,
    period_type: str,
    *,
    priority: int = 100,
    sign_policy: str = "as_reported",
) -> dict[str, object]:
    return {
        "taxonomy": taxonomy,
        "concept_name": concept_name,
        "canonical_metric": canonical_metric,
        "financial_statement": financial_statement,
        "period_type": period_type,
        "sign_policy": sign_policy,
        "priority": priority,
    }


XBRL_CONCEPT_MAP_SEED: list[dict[str, object]] = [
    _xbrl_concept("us-gaap", "RevenueFromContractWithCustomerExcludingAssessedTax", "revenue", "income_statement", "duration", priority=10),
    _xbrl_concept("us-gaap", "RevenueFromContractWithCustomerIncludingAssessedTax", "revenue", "income_statement", "duration", priority=15),
    _xbrl_concept("us-gaap", "Revenues", "revenue", "income_statement", "duration", priority=20),
    _xbrl_concept("us-gaap", "SalesRevenueNet", "revenue", "income_statement", "duration", priority=30),
    _xbrl_concept("us-gaap", "SalesRevenueGoodsNet", "revenue", "income_statement", "duration", priority=40),
    _xbrl_concept("us-gaap", "SalesRevenueServicesNet", "revenue", "income_statement", "duration", priority=45),
    _xbrl_concept("us-gaap", "CostOfRevenue", "cost_of_sales", "income_statement", "duration", priority=10, sign_policy="positive_abs"),
    _xbrl_concept("us-gaap", "CostOfGoodsAndServicesSold", "cost_of_sales", "income_statement", "duration", priority=20, sign_policy="positive_abs"),
    _xbrl_concept("us-gaap", "GrossProfit", "gross_profit", "income_statement", "duration", priority=10),
    _xbrl_concept("us-gaap", "OperatingIncomeLoss", "operating_income", "income_statement", "duration", priority=10),
    _xbrl_concept("us-gaap", "LossFromOperations", "operating_income", "income_statement", "duration", priority=20),
    _xbrl_concept("us-gaap", "NetIncomeLoss", "net_income", "income_statement", "duration", priority=10),
    _xbrl_concept("us-gaap", "ProfitLoss", "net_income", "income_statement", "duration", priority=20),
    _xbrl_concept("us-gaap", "EarningsPerShareDiluted", "eps_diluted", "income_statement", "duration", priority=10),
    _xbrl_concept("us-gaap", "Assets", "assets", "balance_sheet", "instant", priority=10),
    _xbrl_concept("us-gaap", "Liabilities", "liabilities", "balance_sheet", "instant", priority=10),
    _xbrl_concept("us-gaap", "StockholdersEquity", "equity", "balance_sheet", "instant", priority=10),
    _xbrl_concept("us-gaap", "CashAndCashEquivalentsAtCarryingValue", "cash_and_equivalents", "balance_sheet", "instant", priority=10),
    _xbrl_concept("us-gaap", "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents", "cash_and_equivalents", "balance_sheet", "instant", priority=20),
    _xbrl_concept("us-gaap", "CashCashEquivalentsAndShortTermInvestments", "cash_and_equivalents", "balance_sheet", "instant", priority=30),
    _xbrl_concept("us-gaap", "InventoryNet", "inventory", "balance_sheet", "instant", priority=10),
    _xbrl_concept("us-gaap", "AccountsReceivableNetCurrent", "accounts_receivable", "balance_sheet", "instant", priority=10),
    _xbrl_concept("us-gaap", "AccountsPayableCurrent", "accounts_payable", "balance_sheet", "instant", priority=10),
    _xbrl_concept("us-gaap", "NetCashProvidedByUsedInOperatingActivities", "operating_cash_flow", "cash_flow", "duration", priority=10),
    _xbrl_concept("us-gaap", "PaymentsToAcquirePropertyPlantAndEquipment", "capex", "cash_flow", "duration", priority=10, sign_policy="positive_abs"),
    _xbrl_concept("us-gaap", "ResearchAndDevelopmentExpense", "research_and_development", "income_statement", "duration", priority=10, sign_policy="positive_abs"),
    _xbrl_concept("us-gaap", "ShareBasedCompensation", "stock_based_compensation", "cash_flow", "duration", priority=10, sign_policy="positive_abs"),
    _xbrl_concept("us-gaap", "WeightedAverageNumberOfDilutedSharesOutstanding", "diluted_shares", "income_statement", "duration", priority=10),
    _xbrl_concept("us-gaap", "DebtCurrent", "debt_current", "balance_sheet", "instant", priority=10),
    _xbrl_concept("us-gaap", "LongTermDebtCurrent", "debt_current", "balance_sheet", "instant", priority=20),
    _xbrl_concept("us-gaap", "LongTermDebtNoncurrent", "debt_noncurrent", "balance_sheet", "instant", priority=10),
    _xbrl_concept("us-gaap", "LongTermDebtAndFinanceLeaseObligationsNoncurrent", "debt_noncurrent", "balance_sheet", "instant", priority=20),
    _xbrl_concept("us-gaap", "DebtAndFinanceLeaseObligations", "debt_total", "balance_sheet", "instant", priority=10),
    _xbrl_concept("us-gaap", "ContractWithCustomerLiabilityCurrent", "deferred_revenue_current", "balance_sheet", "instant", priority=10),
    _xbrl_concept("us-gaap", "ContractWithCustomerLiabilityNoncurrent", "deferred_revenue_noncurrent", "balance_sheet", "instant", priority=10),
    _xbrl_concept("us-gaap", "ContractWithCustomerLiability", "deferred_revenue_total", "balance_sheet", "instant", priority=10),
    _xbrl_concept("us-gaap", "RevenueRemainingPerformanceObligation", "remaining_performance_obligation", "backlog", "instant", priority=10),
    _xbrl_concept("us-gaap", "ContractWithCustomerLiabilityRevenueRecognized", "contract_liability_revenue_recognized", "income_statement", "duration", priority=10),
    _xbrl_concept("ifrs-full", "Revenue", "revenue", "income_statement", "duration", priority=10),
    _xbrl_concept("ifrs-full", "RevenueFromContractsWithCustomers", "revenue", "income_statement", "duration", priority=15),
    _xbrl_concept("ifrs-full", "CostOfSales", "cost_of_sales", "income_statement", "duration", priority=10, sign_policy="positive_abs"),
    _xbrl_concept("ifrs-full", "GrossProfit", "gross_profit", "income_statement", "duration", priority=10),
    _xbrl_concept("ifrs-full", "ProfitLossFromOperatingActivities", "operating_income", "income_statement", "duration", priority=10),
    _xbrl_concept("ifrs-full", "OperatingProfitLoss", "operating_income", "income_statement", "duration", priority=20),
    _xbrl_concept("ifrs-full", "ProfitLoss", "net_income", "income_statement", "duration", priority=10),
    _xbrl_concept("ifrs-full", "DilutedEarningsLossPerShare", "eps_diluted", "income_statement", "duration", priority=10),
    _xbrl_concept("ifrs-full", "Assets", "assets", "balance_sheet", "instant", priority=10),
    _xbrl_concept("ifrs-full", "Liabilities", "liabilities", "balance_sheet", "instant", priority=10),
    _xbrl_concept("ifrs-full", "Equity", "equity", "balance_sheet", "instant", priority=10),
    _xbrl_concept("ifrs-full", "CashAndCashEquivalents", "cash_and_equivalents", "balance_sheet", "instant", priority=10),
    _xbrl_concept("ifrs-full", "Inventories", "inventory", "balance_sheet", "instant", priority=10),
    _xbrl_concept("ifrs-full", "TradeAndOtherCurrentReceivables", "accounts_receivable", "balance_sheet", "instant", priority=10),
    _xbrl_concept("ifrs-full", "TradeAndOtherCurrentPayablesToTradeSuppliers", "accounts_payable", "balance_sheet", "instant", priority=10),
    _xbrl_concept("ifrs-full", "CashFlowsFromUsedInOperatingActivities", "operating_cash_flow", "cash_flow", "duration", priority=10),
    _xbrl_concept("ifrs-full", "NetCashFlowsFromUsedInOperatingActivities", "operating_cash_flow", "cash_flow", "duration", priority=20),
    _xbrl_concept("ifrs-full", "PurchaseOfPropertyPlantAndEquipmentClassifiedAsInvestingActivities", "capex", "cash_flow", "duration", priority=10, sign_policy="positive_abs"),
    _xbrl_concept("ifrs-full", "PaymentsToAcquirePropertyPlantAndEquipment", "capex", "cash_flow", "duration", priority=20, sign_policy="positive_abs"),
    _xbrl_concept("ifrs-full", "ResearchAndDevelopmentExpense", "research_and_development", "income_statement", "duration", priority=10, sign_policy="positive_abs"),
    _xbrl_concept("ifrs-full", "SharebasedPaymentArrangementExpense", "stock_based_compensation", "cash_flow", "duration", priority=10, sign_policy="positive_abs"),
    _xbrl_concept("ifrs-full", "WeightedAverageNumberOfDilutedSharesOutstanding", "diluted_shares", "income_statement", "duration", priority=10),
    _xbrl_concept("ifrs-full", "CurrentBorrowings", "debt_current", "balance_sheet", "instant", priority=10),
    _xbrl_concept("ifrs-full", "NoncurrentBorrowings", "debt_noncurrent", "balance_sheet", "instant", priority=10),
    _xbrl_concept("ifrs-full", "Borrowings", "debt_total", "balance_sheet", "instant", priority=10),
    _xbrl_concept("ifrs-full", "ContractLiabilitiesCurrent", "deferred_revenue_current", "balance_sheet", "instant", priority=10),
    _xbrl_concept("ifrs-full", "ContractLiabilitiesNoncurrent", "deferred_revenue_noncurrent", "balance_sheet", "instant", priority=10),
    _xbrl_concept("ifrs-full", "ContractLiabilities", "deferred_revenue_total", "balance_sheet", "instant", priority=10),
    _xbrl_concept("sec-text", "Revenue", "revenue", "income_statement", "duration", priority=200),
    _xbrl_concept("sec-text", "CostOfRevenue", "cost_of_sales", "income_statement", "duration", priority=200, sign_policy="positive_abs"),
    _xbrl_concept("sec-text", "GrossProfit", "gross_profit", "income_statement", "duration", priority=200),
    _xbrl_concept("sec-text", "OperatingIncomeLoss", "operating_income", "income_statement", "duration", priority=200),
    _xbrl_concept("sec-text", "NetIncomeLoss", "net_income", "income_statement", "duration", priority=200),
    _xbrl_concept("sec-text", "Assets", "assets", "balance_sheet", "instant", priority=200),
    _xbrl_concept("sec-text", "Liabilities", "liabilities", "balance_sheet", "instant", priority=200),
    _xbrl_concept("sec-text", "Equity", "equity", "balance_sheet", "instant", priority=200),
    _xbrl_concept("sec-text", "CashAndCashEquivalents", "cash_and_equivalents", "balance_sheet", "instant", priority=200),
    _xbrl_concept("sec-text", "Inventory", "inventory", "balance_sheet", "instant", priority=200),
    _xbrl_concept("sec-text", "AccountsReceivable", "accounts_receivable", "balance_sheet", "instant", priority=200),
    _xbrl_concept("sec-text", "AccountsPayable", "accounts_payable", "balance_sheet", "instant", priority=200),
    _xbrl_concept("sec-text", "OperatingCashFlow", "operating_cash_flow", "cash_flow", "duration", priority=200),
    _xbrl_concept("sec-text", "Capex", "capex", "cash_flow", "duration", priority=200, sign_policy="positive_abs"),
    _xbrl_concept("sec-text", "ResearchAndDevelopment", "research_and_development", "income_statement", "duration", priority=200, sign_policy="positive_abs"),
    _xbrl_concept("sec-text", "DilutedShares", "diluted_shares", "income_statement", "duration", priority=200),
    _xbrl_concept("sec-text", "DebtTotal", "debt_total", "balance_sheet", "instant", priority=200),
]


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
    subsector_scope TEXT NOT NULL DEFAULT 'industrials',
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
    sector TEXT,
    industry TEXT,
    subsector TEXT,
    country TEXT,
    currency TEXT,
    universe_status TEXT NOT NULL DEFAULT 'candidate',
    is_active INTEGER NOT NULL DEFAULT 1,
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
    UNIQUE(company_id, identifier_type, identifier_value)
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

CREATE TABLE IF NOT EXISTS dim_ticker_alias (
    alias_id INTEGER PRIMARY KEY AUTOINCREMENT,
    contract_ticker TEXT NOT NULL,
    active_ticker TEXT NOT NULL,
    predecessor_ticker TEXT,
    effective_date TEXT NOT NULL,
    price_history_csv TEXT,
    issuer_id TEXT,
    reason TEXT,
    source TEXT,
    verified_flag INTEGER NOT NULL DEFAULT 0,
    notes TEXT,
    source_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (source_id) REFERENCES source_registry(source_id) ON DELETE SET NULL,
    UNIQUE(contract_ticker, effective_date)
);

CREATE TABLE IF NOT EXISTS fact_corporate_action (
    action_id INTEGER PRIMARY KEY AUTOINCREMENT,
    issuer_id TEXT,
    ticker TEXT NOT NULL,
    related_ticker TEXT,
    action_type TEXT NOT NULL,
    action_date TEXT NOT NULL,
    source_id TEXT,
    reason TEXT,
    notes TEXT,
    cash_amount REAL,
    split_numerator REAL,
    split_denominator REAL,
    split_factor REAL,
    raw_value TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (source_id) REFERENCES source_registry(source_id) ON DELETE SET NULL,
    UNIQUE(ticker, related_ticker, action_type, action_date)
);

CREATE TABLE IF NOT EXISTS dim_industrials_taxonomy (
    taxonomy_id INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id INTEGER NOT NULL,
    ticker TEXT NOT NULL,
    model_family TEXT NOT NULL DEFAULT 'defense',
    sector TEXT NOT NULL,
    industry TEXT NOT NULL,
    subsector TEXT NOT NULL,
    calibration_cohort_id TEXT NOT NULL,
    calibration_cohort TEXT NOT NULL,
    calibration_use TEXT NOT NULL DEFAULT 'core',
    development_stage TEXT NOT NULL DEFAULT 'operating',
    taxonomy_confidence REAL NOT NULL DEFAULT 1.0,
    taxonomy_source TEXT,
    analyst_reviewed INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (company_id) REFERENCES dim_company(company_id) ON DELETE CASCADE,
    UNIQUE(ticker, model_family)
);

CREATE TABLE IF NOT EXISTS dim_universe_membership (
    membership_id INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id INTEGER NOT NULL,
    ticker TEXT NOT NULL,
    model_family TEXT NOT NULL DEFAULT 'defense',
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

CREATE TABLE IF NOT EXISTS dim_delisted_calibration_seed (
    ticker TEXT NOT NULL,
    internal_ticker TEXT NOT NULL,
    model_family TEXT NOT NULL DEFAULT 'defense',
    company_name TEXT NOT NULL,
    calibration_cohort_id TEXT NOT NULL,
    exit_type TEXT,
    terminal_type TEXT,
    acquirer TEXT,
    exit_year INTEGER,
    cik TEXT,
    confidence_label TEXT,
    confidence_score REAL NOT NULL DEFAULT 0.0,
    source_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(ticker, model_family),
    FOREIGN KEY (source_id) REFERENCES source_registry(source_id) ON DELETE RESTRICT
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
    dividend REAL,
    split_coefficient REAL,
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
    market_cap REAL,
    shares_outstanding REAL,
    regular_market_price REAL,
    currency TEXT,
    quote_type TEXT,
    exchange TEXT,
    source_timestamp TEXT,
    payload_json TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(ticker, asof_date, source_id),
    FOREIGN KEY (source_id) REFERENCES source_registry(source_id) ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS feature_market_technical (
    ticker TEXT NOT NULL,
    asof_date TEXT NOT NULL,
    source_id TEXT NOT NULL,
    model_family TEXT NOT NULL DEFAULT 'defense',
    latest_close REAL,
    latest_adj_close REAL,
    latest_volume REAL,
    trading_days_available INTEGER NOT NULL DEFAULT 0,
    latest_bar_date TEXT,
    stale_days INTEGER,
    stale_flag INTEGER NOT NULL DEFAULT 0,
    low_history_flag INTEGER NOT NULL DEFAULT 0,
    low_liquidity_flag INTEGER NOT NULL DEFAULT 0,
    ret_1m REAL,
    ret_3m REAL,
    ret_6m REAL,
    ret_12m_ex_1m REAL,
    rel_strength_bench_3m REAL,
    rel_strength_xar_3m REAL,
    rel_strength_ita_3m REAL,
    rel_strength_spy_3m REAL,
    avg_volume_20d REAL,
    avg_volume_60d REAL,
    avg_dollar_volume_20d REAL,
    avg_dollar_volume_60d REAL,
    realized_vol_60d REAL,
    max_drawdown_6m REAL,
    max_drawdown_12m REAL,
    distance_from_52w_high REAL,
    ma_50d REAL,
    ma_200d REAL,
    above_ma_50d INTEGER,
    above_ma_200d INTEGER,
    market_data_quality TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(ticker, asof_date, source_id, model_family),
    FOREIGN KEY (source_id) REFERENCES source_registry(source_id) ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS fact_sec_filing (
    ticker TEXT NOT NULL,
    cik TEXT,
    source_id TEXT NOT NULL,
    accession_number TEXT NOT NULL,
    form_type TEXT NOT NULL,
    filing_date TEXT NOT NULL,
    accepted_at TEXT,
    report_date TEXT,
    fiscal_year INTEGER,
    fiscal_period TEXT,
    primary_document TEXT,
    filing_url TEXT,
    reporting_standard TEXT,
    taxonomy TEXT,
    source_detail TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(ticker, accession_number, source_id),
    FOREIGN KEY (source_id) REFERENCES source_registry(source_id) ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS dim_issuer_reporting_profile (
    ticker TEXT NOT NULL,
    model_family TEXT NOT NULL DEFAULT 'defense',
    cik TEXT,
    country TEXT,
    reporting_profile TEXT NOT NULL,
    reporting_standard TEXT,
    primary_taxonomy TEXT,
    latest_filing_date TEXT,
    latest_form_type TEXT,
    latest_accession_number TEXT,
    fallback_status TEXT,
    financial_confidence REAL NOT NULL DEFAULT 0.0,
    usable_xbrl_flag INTEGER NOT NULL DEFAULT 0,
    source_id TEXT,
    review_reason TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(ticker, model_family),
    FOREIGN KEY (source_id) REFERENCES source_registry(source_id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS dim_xbrl_concept_map (
    taxonomy TEXT NOT NULL,
    concept_name TEXT NOT NULL,
    canonical_metric TEXT NOT NULL,
    financial_statement TEXT NOT NULL,
    period_type TEXT NOT NULL,
    sign_policy TEXT NOT NULL DEFAULT 'as_reported',
    priority INTEGER NOT NULL DEFAULT 100,
    active_flag INTEGER NOT NULL DEFAULT 1,
    notes TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(taxonomy, concept_name, canonical_metric)
);

CREATE TABLE IF NOT EXISTS fact_sec_xbrl_fact_raw (
    raw_fact_id INTEGER PRIMARY KEY AUTOINCREMENT,
    fact_key TEXT NOT NULL UNIQUE,
    ticker TEXT NOT NULL,
    cik TEXT,
    source_id TEXT NOT NULL,
    accession_number TEXT,
    form_type TEXT,
    filing_date TEXT,
    accepted_at TEXT,
    fiscal_year INTEGER,
    fiscal_period TEXT,
    period_start TEXT,
    period_end TEXT,
    frame TEXT,
    taxonomy TEXT NOT NULL,
    concept_name TEXT NOT NULL,
    unit TEXT,
    raw_value REAL,
    decimals TEXT,
    source_detail TEXT,
    payload_json TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (source_id) REFERENCES source_registry(source_id) ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS fact_sec_xbrl_fact (
    fact_id INTEGER PRIMARY KEY AUTOINCREMENT,
    raw_fact_id INTEGER,
    ticker TEXT NOT NULL,
    cik TEXT,
    source_id TEXT NOT NULL,
    accession_number TEXT,
    form_type TEXT,
    filing_date TEXT,
    accepted_at TEXT,
    fiscal_year INTEGER,
    fiscal_period TEXT,
    period_start TEXT,
    period_end TEXT,
    frame TEXT,
    taxonomy TEXT NOT NULL,
    concept_name TEXT NOT NULL,
    canonical_metric TEXT NOT NULL,
    financial_statement TEXT,
    period_type TEXT,
    unit TEXT,
    value REAL,
    sign_policy TEXT,
    source_priority INTEGER NOT NULL DEFAULT 100,
    source_detail TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(ticker, source_id, accession_number, taxonomy, concept_name, canonical_metric, unit, period_start, period_end, frame),
    FOREIGN KEY (raw_fact_id) REFERENCES fact_sec_xbrl_fact_raw(raw_fact_id) ON DELETE SET NULL,
    FOREIGN KEY (source_id) REFERENCES source_registry(source_id) ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS fact_financial_statement_canonical (
    ticker TEXT NOT NULL,
    source_id TEXT NOT NULL,
    model_family TEXT NOT NULL DEFAULT 'defense',
    canonical_metric TEXT NOT NULL,
    period_end TEXT NOT NULL,
    period_start TEXT,
    filing_date TEXT,
    accepted_at TEXT,
    accession_number TEXT,
    form_type TEXT,
    fiscal_year INTEGER,
    fiscal_period TEXT,
    reporting_standard TEXT,
    taxonomy TEXT,
    concept_name TEXT,
    unit TEXT,
    value REAL,
    value_usd REAL,
    source_priority INTEGER NOT NULL DEFAULT 100,
    canonical_quality TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(ticker, source_id, model_family, canonical_metric, period_end, accession_number, unit),
    FOREIGN KEY (source_id) REFERENCES source_registry(source_id) ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS fact_fx_rate (
    currency_pair TEXT NOT NULL,
    rate_date TEXT NOT NULL,
    source_id TEXT NOT NULL,
    from_currency TEXT NOT NULL,
    to_currency TEXT NOT NULL,
    fx_rate REAL NOT NULL,
    rate_type TEXT NOT NULL DEFAULT 'spot_close',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(currency_pair, rate_date, source_id),
    FOREIGN KEY (source_id) REFERENCES source_registry(source_id) ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS feature_financial_statement (
    ticker TEXT NOT NULL,
    asof_date TEXT NOT NULL,
    source_id TEXT NOT NULL,
    model_family TEXT NOT NULL DEFAULT 'defense',
    accession_number TEXT,
    form_type TEXT,
    fiscal_period_end TEXT,
    fiscal_year INTEGER,
    fiscal_period TEXT,
    reporting_standard TEXT,
    reporting_profile TEXT,
    financial_frequency TEXT,
    reported_currency TEXT,
    fx_conversion_status TEXT,
    fx_rate_income_statement REAL,
    fx_rate_balance_sheet REAL,
    revenue REAL,
    cost_of_sales REAL,
    gross_profit REAL,
    operating_income REAL,
    net_income REAL,
    eps_diluted REAL,
    assets REAL,
    liabilities REAL,
    equity REAL,
    cash_and_equivalents REAL,
    total_debt REAL,
    inventory REAL,
    accounts_receivable REAL,
    accounts_payable REAL,
    operating_cash_flow REAL,
    capex REAL,
    free_cash_flow REAL,
    research_and_development REAL,
    stock_based_compensation REAL,
    diluted_shares REAL,
    revenue_usd REAL,
    gross_profit_usd REAL,
    operating_income_usd REAL,
    net_income_usd REAL,
    operating_cash_flow_usd REAL,
    capex_usd REAL,
    free_cash_flow_usd REAL,
    assets_usd REAL,
    liabilities_usd REAL,
    equity_usd REAL,
    cash_and_equivalents_usd REAL,
    total_debt_usd REAL,
    inventory_usd REAL,
    accounts_receivable_usd REAL,
    accounts_payable_usd REAL,
    revenue_ttm REAL,
    revenue_stub_annualized REAL,
    revenue_stub_annualized_usd REAL,
    revenue_stub_period_days REAL,
    revenue_stub_quality TEXT,
    gross_profit_ttm REAL,
    operating_income_ttm REAL,
    net_income_ttm REAL,
    free_cash_flow_ttm REAL,
    gross_margin REAL,
    operating_margin REAL,
    fcf_margin REAL,
    r_and_d_pct_revenue REAL,
    sbc_pct_revenue REAL,
    net_cash REAL,
    net_cash_to_assets REAL,
    inventory_days REAL,
    days_sales_outstanding REAL,
    days_payables_outstanding REAL,
    cash_conversion_cycle REAL,
    revenue_yoy_growth REAL,
    gross_profit_yoy_growth REAL,
    operating_income_yoy_growth REAL,
    free_cash_flow_yoy_growth REAL,
    revenue_acceleration REAL,
    fcf_to_net_income REAL,
    fcf_yield REAL,
    ev_gross_profit REAL,
    ev_operating_income REAL,
    market_cap REAL,
    latest_price REAL,
    deferred_revenue REAL,
    contract_liabilities REAL,
    remaining_performance_obligation REAL,
    book_to_bill REAL,
    funded_backlog REAL,
    development_stage TEXT,
    financial_confidence REAL NOT NULL DEFAULT 0.0,
    financial_fallback_status TEXT,
    canonical_quality TEXT,
    data_quality_status TEXT,
    review_reason TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(ticker, asof_date, source_id, model_family),
    FOREIGN KEY (source_id) REFERENCES source_registry(source_id) ON DELETE RESTRICT
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
    FOREIGN KEY (company_id) REFERENCES dim_company(company_id) ON DELETE SET NULL,
    FOREIGN KEY (source_id) REFERENCES source_registry(source_id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_raw_api_responses_source_asof
    ON raw_api_responses(source_id, asof_date);

CREATE INDEX IF NOT EXISTS idx_dim_company_cik
    ON dim_company(cik);

CREATE INDEX IF NOT EXISTS idx_dim_security_ticker
    ON dim_security(ticker);

CREATE INDEX IF NOT EXISTS idx_dim_identifier_company
    ON dim_identifier(company_id);

CREATE INDEX IF NOT EXISTS idx_dim_identifier_type_value
    ON dim_identifier(identifier_type, identifier_value);

CREATE INDEX IF NOT EXISTS idx_dim_company_alias_norm
    ON dim_company_alias(alias_norm);

CREATE INDEX IF NOT EXISTS idx_dim_ticker_alias_contract_date
    ON dim_ticker_alias(contract_ticker, effective_date);

CREATE INDEX IF NOT EXISTS idx_fact_corporate_action_ticker_date
    ON fact_corporate_action(ticker, action_date);

CREATE INDEX IF NOT EXISTS idx_dim_industrials_taxonomy_model_cohort
    ON dim_industrials_taxonomy(model_family, calibration_cohort_id);

CREATE INDEX IF NOT EXISTS idx_dim_universe_membership_lookup
    ON dim_universe_membership(model_family, ticker, start_date, end_date);

CREATE INDEX IF NOT EXISTS idx_dim_universe_membership_current
    ON dim_universe_membership(model_family, is_current_member, membership_basis);

CREATE INDEX IF NOT EXISTS idx_fact_price_ohlcv_ticker_date
    ON fact_price_ohlcv(ticker, bar_date);

CREATE INDEX IF NOT EXISTS idx_fact_price_ohlcv_source_date
    ON fact_price_ohlcv(source_id, bar_date);

CREATE INDEX IF NOT EXISTS idx_fact_market_snapshot_ticker_asof
    ON fact_market_snapshot(ticker, asof_date);

CREATE INDEX IF NOT EXISTS idx_feature_market_technical_asof
    ON feature_market_technical(model_family, asof_date);

CREATE INDEX IF NOT EXISTS idx_fact_sec_filing_ticker_date
    ON fact_sec_filing(ticker, filing_date);

CREATE INDEX IF NOT EXISTS idx_dim_issuer_reporting_profile_status
    ON dim_issuer_reporting_profile(model_family, reporting_profile, usable_xbrl_flag);

CREATE INDEX IF NOT EXISTS idx_dim_xbrl_concept_map_lookup
    ON dim_xbrl_concept_map(taxonomy, concept_name, active_flag);

CREATE INDEX IF NOT EXISTS idx_fact_sec_xbrl_fact_raw_ticker_taxonomy
    ON fact_sec_xbrl_fact_raw(ticker, taxonomy, concept_name);

CREATE INDEX IF NOT EXISTS idx_fact_sec_xbrl_fact_ticker_metric_end
    ON fact_sec_xbrl_fact(ticker, canonical_metric, period_end, filing_date);

CREATE INDEX IF NOT EXISTS idx_fact_financial_statement_canonical_ticker_metric
    ON fact_financial_statement_canonical(model_family, ticker, canonical_metric, period_end, filing_date);

CREATE INDEX IF NOT EXISTS idx_fact_fx_rate_lookup
    ON fact_fx_rate(from_currency, to_currency, rate_date);

CREATE INDEX IF NOT EXISTS idx_feature_financial_statement_ticker_asof
    ON feature_financial_statement(model_family, ticker, asof_date);

CREATE INDEX IF NOT EXISTS idx_data_quality_issues_stage_ticker
    ON data_quality_issues(stage, ticker);
"""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _is_transient_sqlite_error(exc: sqlite3.OperationalError) -> bool:
    message = str(exc).lower()
    return any(marker in message for marker in TRANSIENT_SQLITE_MARKERS)


def connect(db_path: Path, *, timeout_sec: float = 120.0) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), timeout=float(timeout_sec))
    conn.row_factory = sqlite3.Row
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


def apply_schema(conn: sqlite3.Connection) -> list[str]:
    deferred_indexes: list[str] = []
    for statement in SCHEMA_SQL.split(";"):
        sql = statement.strip()
        if not sql:
            continue
        try:
            conn.execute(sql)
        except sqlite3.OperationalError as exc:
            if sql.upper().startswith("CREATE INDEX") and "no such column" in str(exc).lower():
                deferred_indexes.append(sql)
                continue
            raise
    return deferred_indexes


def init_db(conn: sqlite3.Connection) -> None:
    for attempt in range(3):
        try:
            with conn:
                deferred_indexes = apply_schema(conn)
                migrate_schema(conn)
                for index_sql in deferred_indexes:
                    conn.execute(index_sql)
                seed_xbrl_concept_map(conn)
            return
        except sqlite3.OperationalError as exc:
            if attempt >= 2 or not _is_transient_sqlite_error(exc):
                raise
            conn.rollback()
            time.sleep(0.5 * (attempt + 1))


def table_columns(conn: sqlite3.Connection, table_name: str) -> set[str]:
    if not SAFE_IDENTIFIER_RE.match(table_name):
        raise ValueError(f"Unsafe table name: {table_name}")
    return {str(row["name"]) for row in conn.execute(f"PRAGMA table_info({table_name})").fetchall()}


def ensure_column(conn: sqlite3.Connection, table_name: str, column_name: str, declaration: str) -> None:
    if not SAFE_IDENTIFIER_RE.match(table_name):
        raise ValueError(f"Unsafe table name: {table_name}")
    if not SAFE_IDENTIFIER_RE.match(column_name):
        raise ValueError(f"Unsafe column name: {column_name}")
    if column_name not in table_columns(conn, table_name):
        conn.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {declaration}")


def migrate_schema(conn: sqlite3.Connection) -> None:
    ensure_column(conn, "dim_delisted_calibration_seed", "internal_ticker", "TEXT")
    ensure_column(conn, "fact_price_ohlcv", "dividend_amount", "REAL")
    ensure_column(conn, "fact_price_ohlcv", "split_factor", "REAL")
    ensure_column(conn, "fact_price_ohlcv", "price_adjustment", "TEXT")
    ensure_column(conn, "fact_price_ohlcv", "is_adjusted", "INTEGER NOT NULL DEFAULT 0")
    ensure_column(conn, "fact_corporate_action", "cash_amount", "REAL")
    ensure_column(conn, "fact_corporate_action", "split_numerator", "REAL")
    ensure_column(conn, "fact_corporate_action", "split_denominator", "REAL")
    ensure_column(conn, "fact_corporate_action", "split_factor", "REAL")
    ensure_column(conn, "fact_corporate_action", "raw_value", "TEXT")
    ensure_column(conn, "dim_issuer_reporting_profile", "reporting_profile", "TEXT")
    ensure_column(conn, "dim_issuer_reporting_profile", "fallback_status", "TEXT")
    ensure_column(conn, "dim_issuer_reporting_profile", "financial_confidence", "REAL NOT NULL DEFAULT 0.0")
    ensure_column(conn, "fact_financial_statement_canonical", "concept_name", "TEXT")
    ensure_column(conn, "feature_financial_statement", "cost_of_sales", "REAL")
    ensure_column(conn, "feature_financial_statement", "reporting_profile", "TEXT")
    ensure_column(conn, "feature_financial_statement", "contract_liabilities", "REAL")
    ensure_column(conn, "feature_financial_statement", "book_to_bill", "REAL")
    ensure_column(conn, "feature_financial_statement", "funded_backlog", "REAL")
    ensure_column(conn, "feature_financial_statement", "revenue_stub_annualized", "REAL")
    ensure_column(conn, "feature_financial_statement", "revenue_stub_annualized_usd", "REAL")
    ensure_column(conn, "feature_financial_statement", "revenue_stub_period_days", "REAL")
    ensure_column(conn, "feature_financial_statement", "revenue_stub_quality", "TEXT")
    ensure_column(conn, "feature_financial_statement", "development_stage", "TEXT")
    ensure_column(conn, "feature_financial_statement", "financial_confidence", "REAL NOT NULL DEFAULT 0.0")
    ensure_column(conn, "feature_financial_statement", "financial_fallback_status", "TEXT")
    ensure_column(conn, "feature_financial_statement", "review_reason", "TEXT")
    conn.execute(
        """
        UPDATE dim_delisted_calibration_seed
        SET internal_ticker = ticker
        WHERE COALESCE(internal_ticker, '') = ''
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_dim_delisted_calibration_internal
        ON dim_delisted_calibration_seed(model_family, internal_ticker)
        """
    )
    conn.execute(
        """
        UPDATE fact_price_ohlcv
        SET dividend_amount = COALESCE(dividend_amount, dividend),
            split_factor = COALESCE(split_factor, split_coefficient),
            price_adjustment = COALESCE(NULLIF(price_adjustment, ''), CASE WHEN adj_close IS NOT NULL THEN 'adjusted_close' ELSE 'missing_adjusted_close' END),
            is_adjusted = CASE WHEN adj_close IS NOT NULL THEN 1 ELSE COALESCE(is_adjusted, 0) END
        """
    )


def seed_xbrl_concept_map(conn: sqlite3.Connection) -> None:
    now = utc_now()
    conn.executemany(
        """
        INSERT INTO dim_xbrl_concept_map(
            taxonomy, concept_name, canonical_metric, financial_statement, period_type,
            sign_policy, priority, active_flag, notes, created_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, 1, 'seeded by industrials.core.db', ?, ?)
        ON CONFLICT(taxonomy, concept_name, canonical_metric) DO UPDATE SET
            financial_statement = excluded.financial_statement,
            period_type = excluded.period_type,
            sign_policy = excluded.sign_policy,
            priority = excluded.priority,
            active_flag = 1,
            updated_at = excluded.updated_at
        """,
        [
            (
                row["taxonomy"],
                row["concept_name"],
                row["canonical_metric"],
                row["financial_statement"],
                row["period_type"],
                row["sign_policy"],
                row["priority"],
                now,
                now,
            )
            for row in XBRL_CONCEPT_MAP_SEED
        ],
    )


def start_run(conn: sqlite3.Connection, *, run_type: str, input_path: Path | str | None = None) -> int:
    now = utc_now()
    with conn:
        cur = conn.execute(
            """
            INSERT INTO runs(run_type, started_at, status, input_path, created_at)
            VALUES (?, ?, 'running', ?, ?)
            """,
            (run_type, now, str(input_path or ""), now),
        )
    if cur.lastrowid is None:
        raise RuntimeError("Failed to create run record.")
    return int(cur.lastrowid)


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


def table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    if not SAFE_IDENTIFIER_RE.match(table_name):
        raise ValueError(f"Unsafe table name: {table_name}")
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ? LIMIT 1",
        (table_name,),
    ).fetchone()
    return row is not None


def table_names(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name").fetchall()
    return [str(row["name"]) for row in rows]


def count_rows(conn: sqlite3.Connection, table_name: str) -> int:
    if not SAFE_IDENTIFIER_RE.match(table_name):
        raise ValueError(f"Unsafe table name: {table_name}")
    row = conn.execute(f"SELECT COUNT(*) AS n FROM {table_name}").fetchone()
    return int(row["n"]) if row is not None else 0
