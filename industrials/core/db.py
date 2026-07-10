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
    _xbrl_concept("us-gaap", "NetIncomeLoss", "net_income", "income_statement", "duration", priority=10),
    _xbrl_concept("us-gaap", "ProfitLoss", "net_income", "income_statement", "duration", priority=20),
    _xbrl_concept("us-gaap", "EarningsPerShareDiluted", "eps_diluted", "income_statement", "duration", priority=10),
    _xbrl_concept("us-gaap", "Assets", "assets", "balance_sheet", "instant", priority=10),
    _xbrl_concept("us-gaap", "Liabilities", "liabilities", "balance_sheet", "instant", priority=10),
    _xbrl_concept("us-gaap", "StockholdersEquity", "equity", "balance_sheet", "instant", priority=10),
    _xbrl_concept("us-gaap", "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest", "equity", "balance_sheet", "instant", priority=20),
    _xbrl_concept("us-gaap", "CashAndCashEquivalentsAtCarryingValue", "cash_and_equivalents", "balance_sheet", "instant", priority=10),
    _xbrl_concept("us-gaap", "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents", "cash_and_equivalents", "balance_sheet", "instant", priority=20),
    _xbrl_concept("us-gaap", "CashCashEquivalentsAndShortTermInvestments", "cash_and_equivalents", "balance_sheet", "instant", priority=30),
    _xbrl_concept("us-gaap", "InventoryNet", "inventory", "balance_sheet", "instant", priority=10),
    _xbrl_concept("us-gaap", "AccountsReceivableNetCurrent", "accounts_receivable", "balance_sheet", "instant", priority=10),
    _xbrl_concept("us-gaap", "AccountsPayableCurrent", "accounts_payable", "balance_sheet", "instant", priority=10),
    _xbrl_concept("us-gaap", "NetCashProvidedByUsedInOperatingActivities", "operating_cash_flow", "cash_flow", "duration", priority=10),
    _xbrl_concept("us-gaap", "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations", "operating_cash_flow", "cash_flow", "duration", priority=20),
    _xbrl_concept("us-gaap", "PaymentsToAcquirePropertyPlantAndEquipment", "capex", "cash_flow", "duration", priority=10, sign_policy="positive_abs"),
    _xbrl_concept("us-gaap", "DepreciationDepletionAndAmortization", "depreciation_and_amortization", "cash_flow", "duration", priority=10, sign_policy="positive_abs"),
    _xbrl_concept("us-gaap", "DepreciationAndAmortization", "depreciation_and_amortization", "cash_flow", "duration", priority=20, sign_policy="positive_abs"),
    _xbrl_concept("us-gaap", "Depreciation", "depreciation_and_amortization", "cash_flow", "duration", priority=30, sign_policy="positive_abs"),
    _xbrl_concept("us-gaap", "InterestExpense", "interest_expense", "income_statement", "duration", priority=10, sign_policy="positive_abs"),
    _xbrl_concept("us-gaap", "InterestExpenseNonoperating", "interest_expense", "income_statement", "duration", priority=20, sign_policy="positive_abs"),
    _xbrl_concept("us-gaap", "InterestExpenseDebt", "interest_expense", "income_statement", "duration", priority=30, sign_policy="positive_abs"),
    _xbrl_concept("us-gaap", "IncomeTaxExpenseBenefit", "income_tax_expense", "income_statement", "duration", priority=10),
    _xbrl_concept("us-gaap", "IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest", "pretax_income", "income_statement", "duration", priority=10),
    _xbrl_concept("us-gaap", "ProceedsFromIssuanceOfCommonStock", "equity_issuance_proceeds", "cash_flow", "duration", priority=10, sign_policy="positive_abs"),
    _xbrl_concept("us-gaap", "ProceedsFromIssuanceOfCommonStockIncludingAdditionalCapitalContribution", "equity_issuance_proceeds", "cash_flow", "duration", priority=20, sign_policy="positive_abs"),
    _xbrl_concept("us-gaap", "ProceedsFromIssuanceOfCommonAndPreferredStock", "equity_issuance_proceeds", "cash_flow", "duration", priority=30, sign_policy="positive_abs"),
    _xbrl_concept("us-gaap", "ProceedsFromIssuanceOfLongTermDebt", "debt_issuance_proceeds", "cash_flow", "duration", priority=10, sign_policy="positive_abs"),
    _xbrl_concept("us-gaap", "ProceedsFromIssuanceOfShortTermDebt", "debt_issuance_proceeds", "cash_flow", "duration", priority=20, sign_policy="positive_abs"),
    _xbrl_concept("us-gaap", "ProceedsFromIssuanceOfDebt", "debt_issuance_proceeds", "cash_flow", "duration", priority=30, sign_policy="positive_abs"),
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
    _xbrl_concept("ifrs-full", "DepreciationAndAmortisationExpense", "depreciation_and_amortization", "cash_flow", "duration", priority=10, sign_policy="positive_abs"),
    _xbrl_concept("ifrs-full", "AdjustmentsForDepreciationAndAmortisationExpense", "depreciation_and_amortization", "cash_flow", "duration", priority=20, sign_policy="positive_abs"),
    _xbrl_concept("ifrs-full", "InterestExpense", "interest_expense", "income_statement", "duration", priority=10, sign_policy="positive_abs"),
    _xbrl_concept("ifrs-full", "IncomeTaxExpenseContinuingOperations", "income_tax_expense", "income_statement", "duration", priority=10),
    _xbrl_concept("ifrs-full", "ProfitLossBeforeTax", "pretax_income", "income_statement", "duration", priority=10),
    _xbrl_concept("ifrs-full", "ProceedsFromIssuingShares", "equity_issuance_proceeds", "cash_flow", "duration", priority=10, sign_policy="positive_abs"),
    _xbrl_concept("ifrs-full", "ProceedsFromBorrowings", "debt_issuance_proceeds", "cash_flow", "duration", priority=10, sign_policy="positive_abs"),
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
    _xbrl_concept("sec-text", "DepreciationAndAmortization", "depreciation_and_amortization", "cash_flow", "duration", priority=200, sign_policy="positive_abs"),
    _xbrl_concept("sec-text", "InterestExpense", "interest_expense", "income_statement", "duration", priority=200, sign_policy="positive_abs"),
    _xbrl_concept("sec-text", "Orders", "orders", "orders", "duration", priority=200, sign_policy="positive_abs"),
    _xbrl_concept("sec-text", "FundedBacklog", "funded_backlog", "backlog", "instant", priority=200, sign_policy="positive_abs"),
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
    -- SC-9: reporting_standard / taxonomy are reserved provenance columns that no
    -- writer populates today (always NULL). Do not read them as authoritative until
    -- a writer is added. Issuer-level standard/taxonomy live on
    -- dim_issuer_reporting_profile instead.
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
    -- SC-10: nullable on purpose. NULL means "confidence unknown" while 0.0 is a
    -- deliberate worst-case assignment written explicitly by the profile writer.
    financial_confidence REAL,
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
    -- SC-13: PK columns must be NOT NULL. NULLs never conflict in ON CONFLICT, so a
    -- nullable PK column silently accumulates duplicate rows. Writers use '' for
    -- "unknown", never NULL.
    accession_number TEXT NOT NULL DEFAULT '',
    form_type TEXT,
    fiscal_year INTEGER,
    fiscal_period TEXT,
    reporting_standard TEXT,
    taxonomy TEXT,
    concept_name TEXT,
    unit TEXT NOT NULL DEFAULT '',
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
    depreciation_and_amortization REAL,
    interest_expense REAL,
    pretax_income REAL,
    income_tax_expense REAL,
    equity_issuance_proceeds REAL,
    debt_issuance_proceeds REAL,
    orders REAL,
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
    depreciation_and_amortization_usd REAL,
    interest_expense_usd REAL,
    equity_issuance_proceeds_usd REAL,
    debt_issuance_proceeds_usd REAL,
    orders_usd REAL,
    free_cash_flow_usd REAL,
    assets_usd REAL,
    liabilities_usd REAL,
    equity_usd REAL,
    cash_and_equivalents_usd REAL,
    total_debt_usd REAL,
    inventory_usd REAL,
    accounts_receivable_usd REAL,
    accounts_payable_usd REAL,
    -- FN-14: unsuffixed TTM/net_cash columns hold LOCAL reported-currency
    -- values (matching the unsuffixed statement columns above). USD-converted
    -- values live only in the *_usd variants, converted at the TTM-window
    -- average FX rate for duration metrics. (No semicolons in this comment:
    -- apply_schema splits SCHEMA_SQL on them.)
    revenue_ttm REAL,
    revenue_ttm_usd REAL,
    revenue_stub_annualized REAL,
    revenue_stub_annualized_usd REAL,
    revenue_stub_period_days REAL,
    revenue_stub_quality TEXT,
    gross_profit_ttm REAL,
    gross_profit_ttm_usd REAL,
    operating_income_ttm REAL,
    operating_income_ttm_usd REAL,
    net_income_ttm REAL,
    net_income_ttm_usd REAL,
    free_cash_flow_ttm REAL,
    free_cash_flow_ttm_usd REAL,
    depreciation_and_amortization_ttm REAL,
    depreciation_and_amortization_ttm_usd REAL,
    interest_expense_ttm REAL,
    interest_expense_ttm_usd REAL,
    equity_issuance_proceeds_ttm REAL,
    equity_issuance_proceeds_ttm_usd REAL,
    debt_issuance_proceeds_ttm REAL,
    debt_issuance_proceeds_ttm_usd REAL,
    orders_ttm REAL,
    orders_ttm_usd REAL,
    gross_margin REAL,
    operating_margin REAL,
    fcf_margin REAL,
    r_and_d_pct_revenue REAL,
    sbc_pct_revenue REAL,
    net_cash REAL,
    net_cash_usd REAL,
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
    funded_backlog_usd REAL,
    orders_yoy_growth REAL,
    backlog_yoy_growth REAL,
    backlog_to_revenue REAL,
    invested_capital_usd REAL,
    roic REAL,
    asset_turnover REAL,
    incremental_operating_margin REAL,
    inventory_growth REAL,
    inventory_sales_growth_spread REAL,
    cash_conversion_cycle_change REAL,
    ebitda_ttm_usd REAL,
    net_debt_to_ebitda REAL,
    interest_coverage REAL,
    cash_burn_ttm_usd REAL,
    cash_runway_years REAL,
    gross_capital_raised_ttm_usd REAL,
    capital_raise_dependence REAL,
    diluted_shares_yoy_growth REAL,
    development_stage TEXT,
    -- SC-10: nullable on purpose. NULL means "confidence unknown" while 0.0 is a
    -- deliberate worst-case assignment written explicitly by the feature builder.
    financial_confidence REAL,
    financial_fallback_status TEXT,
    canonical_quality TEXT,
    data_quality_status TEXT,
    review_reason TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(ticker, asof_date, source_id, model_family),
    FOREIGN KEY (source_id) REFERENCES source_registry(source_id) ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS fact_sec_form4_transaction (
    ticker TEXT NOT NULL,
    accession_number TEXT NOT NULL,
    nonderiv_trans_sk TEXT NOT NULL,
    rptowner_cik TEXT NOT NULL DEFAULT '',
    source_id TEXT NOT NULL,
    filing_date TEXT,
    period_of_report TEXT,
    transaction_date TEXT,
    transaction_code TEXT,
    acquired_disposed_code TEXT,
    transaction_shares REAL,
    transaction_price_per_share REAL,
    transaction_value REAL,
    shares_owned_following_transaction REAL,
    direct_or_indirect_ownership TEXT,
    reporting_owner_name TEXT,
    reporting_owner_relationship TEXT,
    reporting_owner_title TEXT,
    is_director INTEGER,
    is_officer INTEGER,
    is_ten_percent_owner INTEGER,
    is_open_market_purchase INTEGER NOT NULL DEFAULT 0,
    is_open_market_sale INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(ticker, accession_number, nonderiv_trans_sk, rptowner_cik, source_id),
    FOREIGN KEY (source_id) REFERENCES source_registry(source_id) ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS fact_13f_positioning (
    ticker TEXT NOT NULL,
    asof_date TEXT NOT NULL,
    -- PS-11: period_of_report is part of the PK so two reporting periods that share
    -- the same last-filing-date asof do not collapse into one row. NOT NULL DEFAULT
    -- '' keeps the PK conflict-detectable (SC-13) and writers coalesce NULL to ''.
    period_of_report TEXT NOT NULL DEFAULT '',
    source_id TEXT NOT NULL,
    institutional_shares REAL,
    institutional_value REAL,
    manager_count INTEGER,
    institutional_ownership_delta_pct REAL,
    new_buyer_count INTEGER,
    exiting_holder_count INTEGER,
    net_buyer_count INTEGER,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(ticker, asof_date, period_of_report, source_id),
    FOREIGN KEY (source_id) REFERENCES source_registry(source_id) ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS fact_short_interest (
    ticker TEXT NOT NULL,
    settlement_date TEXT NOT NULL,
    source_id TEXT NOT NULL,
    asof_date TEXT,
    publication_date TEXT,
    short_interest_shares REAL,
    float_shares REAL,
    short_interest_pct_float REAL,
    days_to_cover REAL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(ticker, settlement_date, source_id),
    FOREIGN KEY (source_id) REFERENCES source_registry(source_id) ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS fact_ibkr_borrow_snapshot (
    ticker TEXT NOT NULL,
    asof_date TEXT NOT NULL,
    source_id TEXT NOT NULL,
    con_id TEXT,
    borrow_fee_rate REAL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(ticker, asof_date, source_id),
    FOREIGN KEY (source_id) REFERENCES source_registry(source_id) ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS feature_positioning (
    ticker TEXT NOT NULL,
    asof_date TEXT NOT NULL,
    source_id TEXT NOT NULL,
    model_family TEXT NOT NULL DEFAULT 'defense',
    insider_purchase_count_90d INTEGER NOT NULL DEFAULT 0,
    insider_purchase_value_90d REAL,
    insider_sale_count_90d INTEGER NOT NULL DEFAULT 0,
    insider_sale_value_90d REAL,
    insider_cluster_buyers_90d INTEGER NOT NULL DEFAULT 0,
    insider_net_value_90d REAL,
    latest_institutional_shares REAL,
    latest_institutional_value REAL,
    latest_manager_count INTEGER,
    institutional_ownership_delta_pct REAL,
    latest_short_interest_shares REAL,
    latest_short_interest_pct_float REAL,
    latest_days_to_cover REAL,
    short_interest_change_3m REAL,
    latest_borrow_fee_rate REAL,
    form4_status TEXT,
    form4_status_reason TEXT,
    positioning_quality TEXT,
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
    -- SC-12: issues are family-scoped like every feature table. Writers must stamp
    -- their model_family and scope per-stage clears/re-opens by it so one family's
    -- build never wipes another family's open issues.
    model_family TEXT NOT NULL DEFAULT 'defense',
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

CREATE INDEX IF NOT EXISTS idx_fact_sec_form4_transaction_ticker_date
    ON fact_sec_form4_transaction(ticker, transaction_date);

CREATE INDEX IF NOT EXISTS idx_fact_13f_positioning_ticker_asof
    ON fact_13f_positioning(ticker, asof_date);

CREATE INDEX IF NOT EXISTS idx_fact_short_interest_ticker_settle
    ON fact_short_interest(ticker, settlement_date);

CREATE INDEX IF NOT EXISTS idx_fact_ibkr_borrow_snapshot_ticker_asof
    ON fact_ibkr_borrow_snapshot(ticker, asof_date);

CREATE INDEX IF NOT EXISTS idx_feature_positioning_asof
    ON feature_positioning(model_family, asof_date);

CREATE INDEX IF NOT EXISTS idx_data_quality_issues_stage_ticker
    ON data_quality_issues(stage, ticker);

CREATE INDEX IF NOT EXISTS idx_data_quality_issues_family_stage_ticker
    ON data_quality_issues(model_family, stage, ticker);
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


# PRAGMA user_version schema/data version for this database (SC-1 / SC-5).
# One-time data backfills and table rebuilds are gated on this version so init_db
# (which every pipeline script runs on every connection) does not repeat them.
# Bump the constant when adding a new gated migration step below.
DB_USER_VERSION = 4


def db_user_version(conn: sqlite3.Connection) -> int:
    row = conn.execute("PRAGMA user_version").fetchone()
    return int(row[0]) if row is not None else 0


def _column_info(conn: sqlite3.Connection, table_name: str) -> dict[str, sqlite3.Row]:
    if not SAFE_IDENTIFIER_RE.match(table_name):
        raise ValueError(f"Unsafe table name: {table_name}")
    return {str(row["name"]): row for row in conn.execute(f"PRAGMA table_info({table_name})").fetchall()}


def _backfill_price_adjustment_columns(conn: sqlite3.Connection) -> None:
    # SC-1: one-time backfill of the legacy dividend/split columns into their
    # canonical names. Guarded per column so only rows that actually need the
    # backfill are rewritten (contrast the previous unbounded full-table UPDATE),
    # and gated on user_version so it runs once per database.
    conn.execute(
        """
        UPDATE fact_price_ohlcv
        SET dividend_amount = COALESCE(dividend_amount, dividend),
            split_factor = COALESCE(split_factor, split_coefficient),
            price_adjustment = COALESCE(NULLIF(price_adjustment, ''), CASE WHEN adj_close IS NOT NULL THEN 'adjusted_close' ELSE 'missing_adjusted_close' END),
            is_adjusted = CASE WHEN adj_close IS NOT NULL THEN 1 ELSE COALESCE(is_adjusted, 0) END
        WHERE (dividend_amount IS NULL AND dividend IS NOT NULL)
           OR (split_factor IS NULL AND split_coefficient IS NOT NULL)
           OR COALESCE(price_adjustment, '') = ''
           OR (adj_close IS NOT NULL AND COALESCE(is_adjusted, 0) = 0)
        """
    )


def _rebuild_fact_financial_statement_canonical(conn: sqlite3.Connection) -> None:
    # SC-13: the PK contains accession_number/unit, which were nullable on legacy
    # databases. NULL PK components never conflict in ON CONFLICT, so duplicates
    # accumulate silently. Rebuild with NOT NULL DEFAULT '' and coalesce legacy
    # NULLs; on exact-key duplicates the most recently updated row wins.
    info = _column_info(conn, "fact_financial_statement_canonical")
    if bool(info["accession_number"]["notnull"]) and bool(info["unit"]["notnull"]):
        return
    conn.execute("DROP TABLE IF EXISTS fact_financial_statement_canonical_rebuild")
    conn.execute(
        """
        CREATE TABLE fact_financial_statement_canonical_rebuild (
            ticker TEXT NOT NULL,
            source_id TEXT NOT NULL,
            model_family TEXT NOT NULL DEFAULT 'defense',
            canonical_metric TEXT NOT NULL,
            period_end TEXT NOT NULL,
            period_start TEXT,
            filing_date TEXT,
            accepted_at TEXT,
            accession_number TEXT NOT NULL DEFAULT '',
            form_type TEXT,
            fiscal_year INTEGER,
            fiscal_period TEXT,
            reporting_standard TEXT,
            taxonomy TEXT,
            concept_name TEXT,
            unit TEXT NOT NULL DEFAULT '',
            value REAL,
            value_usd REAL,
            source_priority INTEGER NOT NULL DEFAULT 100,
            canonical_quality TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY(ticker, source_id, model_family, canonical_metric, period_end, accession_number, unit),
            FOREIGN KEY (source_id) REFERENCES source_registry(source_id) ON DELETE RESTRICT
        )
        """
    )
    conn.execute(
        """
        INSERT OR REPLACE INTO fact_financial_statement_canonical_rebuild(
            ticker, source_id, model_family, canonical_metric, period_end, period_start,
            filing_date, accepted_at, accession_number, form_type, fiscal_year, fiscal_period,
            reporting_standard, taxonomy, concept_name, unit, value, value_usd,
            source_priority, canonical_quality, created_at, updated_at
        )
        SELECT ticker, source_id, model_family, canonical_metric, period_end, period_start,
               filing_date, accepted_at, COALESCE(accession_number, ''), form_type, fiscal_year, fiscal_period,
               reporting_standard, taxonomy, concept_name, COALESCE(unit, ''), value, value_usd,
               source_priority, canonical_quality, created_at, updated_at
        FROM fact_financial_statement_canonical
        ORDER BY COALESCE(updated_at, ''), rowid
        """
    )
    conn.execute("DROP TABLE fact_financial_statement_canonical")
    conn.execute("ALTER TABLE fact_financial_statement_canonical_rebuild RENAME TO fact_financial_statement_canonical")
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_fact_financial_statement_canonical_ticker_metric
        ON fact_financial_statement_canonical(model_family, ticker, canonical_metric, period_end, filing_date)
        """
    )


def _rebuild_fact_13f_positioning(conn: sqlite3.Connection) -> None:
    # PS-11 (DDL half): legacy PK (ticker, asof_date, source_id) collapses two
    # reporting periods that share the same last-filing-date asof. Rebuild with
    # period_of_report in the PK (NOT NULL DEFAULT '' per SC-13); on exact-key
    # duplicates the most recently updated row wins.
    info = _column_info(conn, "fact_13f_positioning")
    period_row = info.get("period_of_report")
    if period_row is not None and int(period_row["pk"]) > 0 and bool(period_row["notnull"]):
        return
    conn.execute("DROP TABLE IF EXISTS fact_13f_positioning_rebuild")
    conn.execute(
        """
        CREATE TABLE fact_13f_positioning_rebuild (
            ticker TEXT NOT NULL,
            asof_date TEXT NOT NULL,
            period_of_report TEXT NOT NULL DEFAULT '',
            source_id TEXT NOT NULL,
            institutional_shares REAL,
            institutional_value REAL,
            manager_count INTEGER,
            institutional_ownership_delta_pct REAL,
            new_buyer_count INTEGER,
            exiting_holder_count INTEGER,
            net_buyer_count INTEGER,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY(ticker, asof_date, period_of_report, source_id),
            FOREIGN KEY (source_id) REFERENCES source_registry(source_id) ON DELETE RESTRICT
        )
        """
    )
    conn.execute(
        """
        INSERT OR REPLACE INTO fact_13f_positioning_rebuild(
            ticker, asof_date, period_of_report, source_id, institutional_shares,
            institutional_value, manager_count, institutional_ownership_delta_pct,
            new_buyer_count, exiting_holder_count, net_buyer_count, created_at, updated_at
        )
        SELECT ticker, asof_date, COALESCE(period_of_report, ''), source_id, institutional_shares,
               institutional_value, manager_count, institutional_ownership_delta_pct,
               new_buyer_count, exiting_holder_count, net_buyer_count, created_at, updated_at
        FROM fact_13f_positioning
        ORDER BY COALESCE(updated_at, ''), rowid
        """
    )
    conn.execute("DROP TABLE fact_13f_positioning")
    conn.execute("ALTER TABLE fact_13f_positioning_rebuild RENAME TO fact_13f_positioning")
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_fact_13f_positioning_ticker_asof
        ON fact_13f_positioning(ticker, asof_date)
        """
    )


def _backfill_feature_usd_ttm_columns(conn: sqlite3.Connection) -> None:
    # FN-14: legacy feature rows stored USD-converted values in the unsuffixed
    # TTM/net_cash columns. Copy them into the new *_usd columns, then clear
    # the unsuffixed columns wherever local != USD (the local-currency values
    # cannot be reconstructed here; the next feature build repopulates them).
    # usd_native rows keep both columns because local == USD for them.
    conn.execute(
        """
        UPDATE feature_financial_statement
        SET revenue_ttm_usd = COALESCE(revenue_ttm_usd, revenue_ttm),
            gross_profit_ttm_usd = COALESCE(gross_profit_ttm_usd, gross_profit_ttm),
            operating_income_ttm_usd = COALESCE(operating_income_ttm_usd, operating_income_ttm),
            net_income_ttm_usd = COALESCE(net_income_ttm_usd, net_income_ttm),
            free_cash_flow_ttm_usd = COALESCE(free_cash_flow_ttm_usd, free_cash_flow_ttm),
            net_cash_usd = COALESCE(net_cash_usd, net_cash)
        WHERE revenue_ttm IS NOT NULL
           OR gross_profit_ttm IS NOT NULL
           OR operating_income_ttm IS NOT NULL
           OR net_income_ttm IS NOT NULL
           OR free_cash_flow_ttm IS NOT NULL
           OR net_cash IS NOT NULL
        """
    )
    conn.execute(
        """
        UPDATE feature_financial_statement
        SET revenue_ttm = NULL,
            gross_profit_ttm = NULL,
            operating_income_ttm = NULL,
            net_income_ttm = NULL,
            free_cash_flow_ttm = NULL,
            net_cash = NULL
        WHERE COALESCE(fx_conversion_status, '') <> 'usd_native'
          AND (
                revenue_ttm IS NOT NULL
             OR gross_profit_ttm IS NOT NULL
             OR operating_income_ttm IS NOT NULL
             OR net_income_ttm IS NOT NULL
             OR free_cash_flow_ttm IS NOT NULL
             OR net_cash IS NOT NULL
          )
        """
    )


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
    # SC-6: match the fresh DDL's NOT NULL contract when migrating legacy tables.
    ensure_column(conn, "dim_issuer_reporting_profile", "reporting_profile", "TEXT NOT NULL DEFAULT ''")
    ensure_column(conn, "dim_issuer_reporting_profile", "fallback_status", "TEXT")
    # SC-10: nullable REAL — migrated legacy rows must read NULL ("unknown"),
    # not 0.0 ("worst"). Writers always supply explicit values.
    ensure_column(conn, "dim_issuer_reporting_profile", "financial_confidence", "REAL")
    ensure_column(conn, "fact_financial_statement_canonical", "concept_name", "TEXT")
    ensure_column(conn, "fact_13f_positioning", "period_of_report", "TEXT")
    ensure_column(conn, "feature_financial_statement", "cost_of_sales", "REAL")
    ensure_column(conn, "feature_financial_statement", "reporting_profile", "TEXT")
    ensure_column(conn, "feature_financial_statement", "contract_liabilities", "REAL")
    ensure_column(conn, "feature_financial_statement", "book_to_bill", "REAL")
    ensure_column(conn, "feature_financial_statement", "funded_backlog", "REAL")
    ensure_column(conn, "feature_financial_statement", "depreciation_and_amortization", "REAL")
    ensure_column(conn, "feature_financial_statement", "interest_expense", "REAL")
    ensure_column(conn, "feature_financial_statement", "pretax_income", "REAL")
    ensure_column(conn, "feature_financial_statement", "income_tax_expense", "REAL")
    ensure_column(conn, "feature_financial_statement", "equity_issuance_proceeds", "REAL")
    ensure_column(conn, "feature_financial_statement", "debt_issuance_proceeds", "REAL")
    ensure_column(conn, "feature_financial_statement", "orders", "REAL")
    ensure_column(conn, "feature_financial_statement", "depreciation_and_amortization_usd", "REAL")
    ensure_column(conn, "feature_financial_statement", "interest_expense_usd", "REAL")
    ensure_column(conn, "feature_financial_statement", "equity_issuance_proceeds_usd", "REAL")
    ensure_column(conn, "feature_financial_statement", "debt_issuance_proceeds_usd", "REAL")
    ensure_column(conn, "feature_financial_statement", "orders_usd", "REAL")
    ensure_column(conn, "feature_financial_statement", "depreciation_and_amortization_ttm", "REAL")
    ensure_column(conn, "feature_financial_statement", "depreciation_and_amortization_ttm_usd", "REAL")
    ensure_column(conn, "feature_financial_statement", "interest_expense_ttm", "REAL")
    ensure_column(conn, "feature_financial_statement", "interest_expense_ttm_usd", "REAL")
    ensure_column(conn, "feature_financial_statement", "equity_issuance_proceeds_ttm", "REAL")
    ensure_column(conn, "feature_financial_statement", "equity_issuance_proceeds_ttm_usd", "REAL")
    ensure_column(conn, "feature_financial_statement", "debt_issuance_proceeds_ttm", "REAL")
    ensure_column(conn, "feature_financial_statement", "debt_issuance_proceeds_ttm_usd", "REAL")
    ensure_column(conn, "feature_financial_statement", "orders_ttm", "REAL")
    ensure_column(conn, "feature_financial_statement", "orders_ttm_usd", "REAL")
    ensure_column(conn, "feature_financial_statement", "funded_backlog_usd", "REAL")
    ensure_column(conn, "feature_financial_statement", "orders_yoy_growth", "REAL")
    ensure_column(conn, "feature_financial_statement", "backlog_yoy_growth", "REAL")
    ensure_column(conn, "feature_financial_statement", "backlog_to_revenue", "REAL")
    ensure_column(conn, "feature_financial_statement", "invested_capital_usd", "REAL")
    ensure_column(conn, "feature_financial_statement", "roic", "REAL")
    ensure_column(conn, "feature_financial_statement", "asset_turnover", "REAL")
    ensure_column(conn, "feature_financial_statement", "incremental_operating_margin", "REAL")
    ensure_column(conn, "feature_financial_statement", "inventory_growth", "REAL")
    ensure_column(conn, "feature_financial_statement", "inventory_sales_growth_spread", "REAL")
    ensure_column(conn, "feature_financial_statement", "cash_conversion_cycle_change", "REAL")
    ensure_column(conn, "feature_financial_statement", "ebitda_ttm_usd", "REAL")
    ensure_column(conn, "feature_financial_statement", "net_debt_to_ebitda", "REAL")
    ensure_column(conn, "feature_financial_statement", "interest_coverage", "REAL")
    ensure_column(conn, "feature_financial_statement", "cash_burn_ttm_usd", "REAL")
    ensure_column(conn, "feature_financial_statement", "cash_runway_years", "REAL")
    ensure_column(conn, "feature_financial_statement", "gross_capital_raised_ttm_usd", "REAL")
    ensure_column(conn, "feature_financial_statement", "capital_raise_dependence", "REAL")
    ensure_column(conn, "feature_financial_statement", "diluted_shares_yoy_growth", "REAL")
    ensure_column(conn, "feature_financial_statement", "revenue_stub_annualized", "REAL")
    ensure_column(conn, "feature_financial_statement", "revenue_stub_annualized_usd", "REAL")
    ensure_column(conn, "feature_financial_statement", "revenue_stub_period_days", "REAL")
    ensure_column(conn, "feature_financial_statement", "revenue_stub_quality", "TEXT")
    ensure_column(conn, "feature_financial_statement", "development_stage", "TEXT")
    # FN-14: USD variants of the TTM/net_cash feature columns; the unsuffixed
    # columns hold local reported-currency values from this migration on.
    ensure_column(conn, "feature_financial_statement", "revenue_ttm_usd", "REAL")
    ensure_column(conn, "feature_financial_statement", "gross_profit_ttm_usd", "REAL")
    ensure_column(conn, "feature_financial_statement", "operating_income_ttm_usd", "REAL")
    ensure_column(conn, "feature_financial_statement", "net_income_ttm_usd", "REAL")
    ensure_column(conn, "feature_financial_statement", "free_cash_flow_ttm_usd", "REAL")
    ensure_column(conn, "feature_financial_statement", "net_cash_usd", "REAL")
    ensure_column(conn, "feature_financial_statement", "financial_confidence", "REAL")
    ensure_column(conn, "feature_financial_statement", "financial_fallback_status", "TEXT")
    ensure_column(conn, "feature_financial_statement", "review_reason", "TEXT")
    ensure_column(conn, "feature_positioning", "form4_status", "TEXT")
    ensure_column(conn, "feature_positioning", "form4_status_reason", "TEXT")
    # SC-12: family-scope the issues table on legacy databases. Existing rows were
    # all written by defense-family runs, so the backfill default is 'defense'.
    ensure_column(conn, "data_quality_issues", "model_family", "TEXT NOT NULL DEFAULT 'defense'")
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_dim_delisted_calibration_internal
        ON dim_delisted_calibration_seed(model_family, internal_ticker)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_data_quality_issues_family_stage_ticker
        ON data_quality_issues(model_family, stage, ticker)
        """
    )
    version = db_user_version(conn)
    if version < 1:
        conn.execute(
            """
            UPDATE dim_delisted_calibration_seed
            SET internal_ticker = ticker
            WHERE COALESCE(internal_ticker, '') = ''
            """
        )
        # SC-6: legacy databases may have gained reporting_profile as a plain
        # nullable TEXT column; align stored NULLs with the NOT NULL '' contract.
        conn.execute("UPDATE dim_issuer_reporting_profile SET reporting_profile = '' WHERE reporting_profile IS NULL")
        _backfill_price_adjustment_columns(conn)
    if version < 2:
        _rebuild_fact_financial_statement_canonical(conn)
    if version < 3:
        _rebuild_fact_13f_positioning(conn)
    if version < 4:
        _backfill_feature_usd_ttm_columns(conn)
    if version < DB_USER_VERSION:
        conn.execute(f"PRAGMA user_version = {int(DB_USER_VERSION)}")


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
            notes = excluded.notes,
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
    # SC-5: deactivate seed-managed concepts that were removed from the seed list
    # (e.g. the nonexistent us-gaap:LossFromOperations, FN-19) so stale mappings do
    # not keep matching facts. Only rows this seeder authored are touched; manually
    # curated rows (different notes) are left alone.
    seeded_keys = {
        (str(row["taxonomy"]), str(row["concept_name"]), str(row["canonical_metric"]))
        for row in XBRL_CONCEPT_MAP_SEED
    }
    stale_rows = conn.execute(
        """
        SELECT taxonomy, concept_name, canonical_metric
        FROM dim_xbrl_concept_map
        WHERE active_flag = 1
          AND notes = 'seeded by industrials.core.db'
        """
    ).fetchall()
    for row in stale_rows:
        key = (str(row["taxonomy"]), str(row["concept_name"]), str(row["canonical_metric"]))
        if key in seeded_keys:
            continue
        conn.execute(
            """
            UPDATE dim_xbrl_concept_map
            SET active_flag = 0,
                notes = 'deactivated by industrials.core.db seed sync (removed from seed list)',
                updated_at = ?
            WHERE taxonomy = ? AND concept_name = ? AND canonical_metric = ?
            """,
            (now, key[0], key[1], key[2]),
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
