from __future__ import annotations

import re
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SAFE_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
TRANSIENT_SQLITE_MARKERS = (
    "database is locked",
    "database table is locked",
    "unable to open database file",
    "readonly database",
)

XBRL_CONCEPT_MAP_SEED: list[dict[str, Any]] = [
    {"canonical_metric": "revenue", "statement": "income_statement", "taxonomy": "us-gaap", "concept": "Revenues", "priority": 1, "period_type": "duration", "unit_type": "currency", "sign_policy": "positive", "currency_required": 1, "is_core": 1},
    {"canonical_metric": "revenue", "statement": "income_statement", "taxonomy": "us-gaap", "concept": "RevenueFromContractWithCustomerExcludingAssessedTax", "priority": 2, "period_type": "duration", "unit_type": "currency", "sign_policy": "positive", "currency_required": 1, "is_core": 1},
    {"canonical_metric": "revenue", "statement": "income_statement", "taxonomy": "us-gaap", "concept": "RevenueFromContractWithCustomerIncludingAssessedTax", "priority": 3, "period_type": "duration", "unit_type": "currency", "sign_policy": "positive", "currency_required": 1, "is_core": 1},
    {"canonical_metric": "revenue", "statement": "income_statement", "taxonomy": "us-gaap", "concept": "SalesRevenueNet", "priority": 4, "period_type": "duration", "unit_type": "currency", "sign_policy": "positive", "currency_required": 1, "is_core": 1},
    {"canonical_metric": "revenue", "statement": "income_statement", "taxonomy": "us-gaap", "concept": "SalesRevenueGoodsNet", "priority": 5, "period_type": "duration", "unit_type": "currency", "sign_policy": "positive", "currency_required": 1, "is_core": 1},
    {"canonical_metric": "revenue", "statement": "income_statement", "taxonomy": "ifrs-full", "concept": "Revenue", "priority": 1, "period_type": "duration", "unit_type": "currency", "sign_policy": "positive", "currency_required": 1, "is_core": 1},
    {"canonical_metric": "cost_of_sales", "statement": "income_statement", "taxonomy": "us-gaap", "concept": "CostOfRevenue", "priority": 1, "period_type": "duration", "unit_type": "currency", "sign_policy": "positive_abs", "currency_required": 1, "is_core": 0},
    {"canonical_metric": "cost_of_sales", "statement": "income_statement", "taxonomy": "us-gaap", "concept": "CostOfGoodsAndServicesSold", "priority": 2, "period_type": "duration", "unit_type": "currency", "sign_policy": "positive_abs", "currency_required": 1, "is_core": 0},
    {"canonical_metric": "cost_of_sales", "statement": "income_statement", "taxonomy": "us-gaap", "concept": "CostOfGoodsSold", "priority": 3, "period_type": "duration", "unit_type": "currency", "sign_policy": "positive_abs", "currency_required": 1, "is_core": 0},
    {"canonical_metric": "cost_of_sales", "statement": "income_statement", "taxonomy": "ifrs-full", "concept": "CostOfSales", "priority": 1, "period_type": "duration", "unit_type": "currency", "sign_policy": "positive_abs", "currency_required": 1, "is_core": 0},
    {"canonical_metric": "gross_profit", "statement": "income_statement", "taxonomy": "us-gaap", "concept": "GrossProfit", "priority": 1, "period_type": "duration", "unit_type": "currency", "sign_policy": "as_reported", "currency_required": 1, "is_core": 0},
    {"canonical_metric": "gross_profit", "statement": "income_statement", "taxonomy": "ifrs-full", "concept": "GrossProfit", "priority": 1, "period_type": "duration", "unit_type": "currency", "sign_policy": "as_reported", "currency_required": 1, "is_core": 0},
    {"canonical_metric": "operating_income", "statement": "income_statement", "taxonomy": "us-gaap", "concept": "OperatingIncomeLoss", "priority": 1, "period_type": "duration", "unit_type": "currency", "sign_policy": "as_reported", "currency_required": 1, "is_core": 0},
    {"canonical_metric": "operating_income", "statement": "income_statement", "taxonomy": "ifrs-full", "concept": "ProfitLossFromOperatingActivities", "priority": 1, "period_type": "duration", "unit_type": "currency", "sign_policy": "as_reported", "currency_required": 1, "is_core": 0},
    {"canonical_metric": "pretax_income", "statement": "income_statement", "taxonomy": "us-gaap", "concept": "IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest", "priority": 1, "period_type": "duration", "unit_type": "currency", "sign_policy": "as_reported", "currency_required": 1, "is_core": 0},
    {"canonical_metric": "pretax_income", "statement": "income_statement", "taxonomy": "ifrs-full", "concept": "ProfitLossBeforeTax", "priority": 1, "period_type": "duration", "unit_type": "currency", "sign_policy": "as_reported", "currency_required": 1, "is_core": 0},
    {"canonical_metric": "net_income", "statement": "income_statement", "taxonomy": "us-gaap", "concept": "NetIncomeLoss", "priority": 1, "period_type": "duration", "unit_type": "currency", "sign_policy": "as_reported", "currency_required": 1, "is_core": 0},
    {"canonical_metric": "net_income", "statement": "income_statement", "taxonomy": "ifrs-full", "concept": "ProfitLossAttributableToOwnersOfParent", "priority": 1, "period_type": "duration", "unit_type": "currency", "sign_policy": "as_reported", "currency_required": 1, "is_core": 0},
    {"canonical_metric": "net_income", "statement": "income_statement", "taxonomy": "ifrs-full", "concept": "ProfitLoss", "priority": 2, "period_type": "duration", "unit_type": "currency", "sign_policy": "as_reported", "currency_required": 1, "is_core": 0},
    {"canonical_metric": "eps_basic", "statement": "income_statement", "taxonomy": "us-gaap", "concept": "EarningsPerShareBasic", "priority": 1, "period_type": "duration", "unit_type": "per_share", "sign_policy": "as_reported", "currency_required": 0, "is_core": 0},
    {"canonical_metric": "eps_basic", "statement": "income_statement", "taxonomy": "ifrs-full", "concept": "BasicEarningsLossPerShare", "priority": 1, "period_type": "duration", "unit_type": "per_share", "sign_policy": "as_reported", "currency_required": 0, "is_core": 0},
    {"canonical_metric": "eps_diluted", "statement": "income_statement", "taxonomy": "us-gaap", "concept": "EarningsPerShareDiluted", "priority": 1, "period_type": "duration", "unit_type": "per_share", "sign_policy": "as_reported", "currency_required": 0, "is_core": 0},
    {"canonical_metric": "eps_diluted", "statement": "income_statement", "taxonomy": "ifrs-full", "concept": "DilutedEarningsLossPerShare", "priority": 1, "period_type": "duration", "unit_type": "per_share", "sign_policy": "as_reported", "currency_required": 0, "is_core": 0},
    {"canonical_metric": "assets", "statement": "balance_sheet", "taxonomy": "us-gaap", "concept": "Assets", "priority": 1, "period_type": "instant", "unit_type": "currency", "sign_policy": "positive", "currency_required": 1, "is_core": 1},
    {"canonical_metric": "assets", "statement": "balance_sheet", "taxonomy": "ifrs-full", "concept": "Assets", "priority": 1, "period_type": "instant", "unit_type": "currency", "sign_policy": "positive", "currency_required": 1, "is_core": 1},
    {"canonical_metric": "current_assets", "statement": "balance_sheet", "taxonomy": "us-gaap", "concept": "AssetsCurrent", "priority": 1, "period_type": "instant", "unit_type": "currency", "sign_policy": "positive", "currency_required": 1, "is_core": 0},
    {"canonical_metric": "current_assets", "statement": "balance_sheet", "taxonomy": "ifrs-full", "concept": "CurrentAssets", "priority": 1, "period_type": "instant", "unit_type": "currency", "sign_policy": "positive", "currency_required": 1, "is_core": 0},
    {"canonical_metric": "liabilities", "statement": "balance_sheet", "taxonomy": "us-gaap", "concept": "Liabilities", "priority": 1, "period_type": "instant", "unit_type": "currency", "sign_policy": "positive", "currency_required": 1, "is_core": 0},
    {"canonical_metric": "liabilities", "statement": "balance_sheet", "taxonomy": "ifrs-full", "concept": "Liabilities", "priority": 1, "period_type": "instant", "unit_type": "currency", "sign_policy": "positive", "currency_required": 1, "is_core": 0},
    {"canonical_metric": "current_liabilities", "statement": "balance_sheet", "taxonomy": "us-gaap", "concept": "LiabilitiesCurrent", "priority": 1, "period_type": "instant", "unit_type": "currency", "sign_policy": "positive", "currency_required": 1, "is_core": 0},
    {"canonical_metric": "current_liabilities", "statement": "balance_sheet", "taxonomy": "ifrs-full", "concept": "CurrentLiabilities", "priority": 1, "period_type": "instant", "unit_type": "currency", "sign_policy": "positive", "currency_required": 1, "is_core": 0},
    {"canonical_metric": "equity", "statement": "balance_sheet", "taxonomy": "us-gaap", "concept": "StockholdersEquity", "priority": 1, "period_type": "instant", "unit_type": "currency", "sign_policy": "as_reported", "currency_required": 1, "is_core": 0},
    {"canonical_metric": "equity", "statement": "balance_sheet", "taxonomy": "us-gaap", "concept": "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest", "priority": 2, "period_type": "instant", "unit_type": "currency", "sign_policy": "as_reported", "currency_required": 1, "is_core": 0},
    {"canonical_metric": "equity", "statement": "balance_sheet", "taxonomy": "ifrs-full", "concept": "EquityAttributableToOwnersOfParent", "priority": 1, "period_type": "instant", "unit_type": "currency", "sign_policy": "as_reported", "currency_required": 1, "is_core": 0},
    {"canonical_metric": "equity", "statement": "balance_sheet", "taxonomy": "ifrs-full", "concept": "Equity", "priority": 2, "period_type": "instant", "unit_type": "currency", "sign_policy": "as_reported", "currency_required": 1, "is_core": 0},
    {"canonical_metric": "cash_and_equivalents", "statement": "balance_sheet", "taxonomy": "us-gaap", "concept": "CashAndCashEquivalentsAtCarryingValue", "priority": 1, "period_type": "instant", "unit_type": "currency", "sign_policy": "positive", "currency_required": 1, "is_core": 0},
    {"canonical_metric": "cash_and_equivalents", "statement": "balance_sheet", "taxonomy": "us-gaap", "concept": "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents", "priority": 2, "period_type": "instant", "unit_type": "currency", "sign_policy": "positive", "currency_required": 1, "is_core": 0},
    {"canonical_metric": "cash_and_equivalents", "statement": "balance_sheet", "taxonomy": "ifrs-full", "concept": "CashAndCashEquivalents", "priority": 1, "period_type": "instant", "unit_type": "currency", "sign_policy": "positive", "currency_required": 1, "is_core": 0},
    {"canonical_metric": "inventory", "statement": "balance_sheet", "taxonomy": "us-gaap", "concept": "InventoryNet", "priority": 1, "period_type": "instant", "unit_type": "currency", "sign_policy": "positive", "currency_required": 1, "is_core": 0},
    {"canonical_metric": "inventory", "statement": "balance_sheet", "taxonomy": "ifrs-full", "concept": "Inventories", "priority": 1, "period_type": "instant", "unit_type": "currency", "sign_policy": "positive", "currency_required": 1, "is_core": 0},
    # Working-capital efficiency inputs for technology hardware CCC signals.
    {"canonical_metric": "accounts_receivable", "statement": "balance_sheet", "taxonomy": "us-gaap", "concept": "AccountsReceivableNetCurrent", "priority": 1, "period_type": "instant", "unit_type": "currency", "sign_policy": "positive", "currency_required": 1, "is_core": 0},
    {"canonical_metric": "accounts_receivable", "statement": "balance_sheet", "taxonomy": "us-gaap", "concept": "AccountsReceivableNet", "priority": 2, "period_type": "instant", "unit_type": "currency", "sign_policy": "positive", "currency_required": 1, "is_core": 0},
    {"canonical_metric": "accounts_receivable", "statement": "balance_sheet", "taxonomy": "ifrs-full", "concept": "CurrentTradeReceivables", "priority": 1, "period_type": "instant", "unit_type": "currency", "sign_policy": "positive", "currency_required": 1, "is_core": 0},
    {"canonical_metric": "accounts_receivable", "statement": "balance_sheet", "taxonomy": "ifrs-full", "concept": "TradeAndOtherCurrentReceivables", "priority": 2, "period_type": "instant", "unit_type": "currency", "sign_policy": "positive", "currency_required": 1, "is_core": 0},
    {"canonical_metric": "accounts_payable", "statement": "balance_sheet", "taxonomy": "us-gaap", "concept": "AccountsPayableCurrent", "priority": 1, "period_type": "instant", "unit_type": "currency", "sign_policy": "positive", "currency_required": 1, "is_core": 0},
    {"canonical_metric": "accounts_payable", "statement": "balance_sheet", "taxonomy": "us-gaap", "concept": "AccountsPayableTradeCurrent", "priority": 2, "period_type": "instant", "unit_type": "currency", "sign_policy": "positive", "currency_required": 1, "is_core": 0},
    {"canonical_metric": "accounts_payable", "statement": "balance_sheet", "taxonomy": "ifrs-full", "concept": "CurrentTradePayables", "priority": 1, "period_type": "instant", "unit_type": "currency", "sign_policy": "positive", "currency_required": 1, "is_core": 0},
    {"canonical_metric": "accounts_payable", "statement": "balance_sheet", "taxonomy": "ifrs-full", "concept": "TradeAndOtherCurrentPayables", "priority": 2, "period_type": "instant", "unit_type": "currency", "sign_policy": "positive", "currency_required": 1, "is_core": 0},
    {"canonical_metric": "operating_cash_flow", "statement": "cash_flow", "taxonomy": "us-gaap", "concept": "NetCashProvidedByUsedInOperatingActivities", "priority": 1, "period_type": "duration", "unit_type": "currency", "sign_policy": "as_reported", "currency_required": 1, "is_core": 0},
    {"canonical_metric": "operating_cash_flow", "statement": "cash_flow", "taxonomy": "ifrs-full", "concept": "CashFlowsFromUsedInOperatingActivities", "priority": 1, "period_type": "duration", "unit_type": "currency", "sign_policy": "as_reported", "currency_required": 1, "is_core": 0},
    {"canonical_metric": "investing_cash_flow", "statement": "cash_flow", "taxonomy": "us-gaap", "concept": "NetCashProvidedByUsedInInvestingActivities", "priority": 1, "period_type": "duration", "unit_type": "currency", "sign_policy": "as_reported", "currency_required": 1, "is_core": 0},
    {"canonical_metric": "investing_cash_flow", "statement": "cash_flow", "taxonomy": "ifrs-full", "concept": "CashFlowsFromUsedInInvestingActivities", "priority": 1, "period_type": "duration", "unit_type": "currency", "sign_policy": "as_reported", "currency_required": 1, "is_core": 0},
    {"canonical_metric": "financing_cash_flow", "statement": "cash_flow", "taxonomy": "us-gaap", "concept": "NetCashProvidedByUsedInFinancingActivities", "priority": 1, "period_type": "duration", "unit_type": "currency", "sign_policy": "as_reported", "currency_required": 1, "is_core": 0},
    {"canonical_metric": "financing_cash_flow", "statement": "cash_flow", "taxonomy": "ifrs-full", "concept": "CashFlowsFromUsedInFinancingActivities", "priority": 1, "period_type": "duration", "unit_type": "currency", "sign_policy": "as_reported", "currency_required": 1, "is_core": 0},
    {"canonical_metric": "capex", "statement": "cash_flow", "taxonomy": "us-gaap", "concept": "PaymentsToAcquirePropertyPlantAndEquipment", "priority": 1, "period_type": "duration", "unit_type": "currency", "sign_policy": "positive_abs", "currency_required": 1, "is_core": 0},
    {"canonical_metric": "capex", "statement": "cash_flow", "taxonomy": "us-gaap", "concept": "PaymentsToAcquireProductiveAssets", "priority": 2, "period_type": "duration", "unit_type": "currency", "sign_policy": "positive_abs", "currency_required": 1, "is_core": 0},
    {"canonical_metric": "capex", "statement": "cash_flow", "taxonomy": "ifrs-full", "concept": "PurchaseOfPropertyPlantAndEquipmentClassifiedAsInvestingActivities", "priority": 1, "period_type": "duration", "unit_type": "currency", "sign_policy": "positive_abs", "currency_required": 1, "is_core": 0},
    {"canonical_metric": "research_and_development", "statement": "income_statement", "taxonomy": "us-gaap", "concept": "ResearchAndDevelopmentExpense", "priority": 1, "period_type": "duration", "unit_type": "currency", "sign_policy": "positive_abs", "currency_required": 1, "is_core": 0},
    {"canonical_metric": "research_and_development", "statement": "income_statement", "taxonomy": "ifrs-full", "concept": "ResearchAndDevelopmentExpense", "priority": 1, "period_type": "duration", "unit_type": "currency", "sign_policy": "positive_abs", "currency_required": 1, "is_core": 0},
    {"canonical_metric": "stock_based_compensation", "statement": "cash_flow", "taxonomy": "us-gaap", "concept": "ShareBasedCompensation", "priority": 1, "period_type": "duration", "unit_type": "currency", "sign_policy": "positive_abs", "currency_required": 1, "is_core": 0},
    {"canonical_metric": "stock_based_compensation", "statement": "cash_flow", "taxonomy": "us-gaap", "concept": "ShareBasedCompensationArrangementByShareBasedPaymentAwardExpense", "priority": 2, "period_type": "duration", "unit_type": "currency", "sign_policy": "positive_abs", "currency_required": 1, "is_core": 0},
    {"canonical_metric": "stock_based_compensation", "statement": "cash_flow", "taxonomy": "ifrs-full", "concept": "ShareBasedPaymentExpense", "priority": 1, "period_type": "duration", "unit_type": "currency", "sign_policy": "positive_abs", "currency_required": 1, "is_core": 0},
    {"canonical_metric": "debt_current", "statement": "balance_sheet", "taxonomy": "us-gaap", "concept": "ShortTermBorrowings", "priority": 1, "period_type": "instant", "unit_type": "currency", "sign_policy": "positive", "currency_required": 1, "is_core": 0},
    {"canonical_metric": "debt_current", "statement": "balance_sheet", "taxonomy": "us-gaap", "concept": "ShortTermDebt", "priority": 2, "period_type": "instant", "unit_type": "currency", "sign_policy": "positive", "currency_required": 1, "is_core": 0},
    {"canonical_metric": "debt_current", "statement": "balance_sheet", "taxonomy": "us-gaap", "concept": "CurrentPortionOfLongTermDebt", "priority": 3, "period_type": "instant", "unit_type": "currency", "sign_policy": "positive", "currency_required": 1, "is_core": 0},
    {"canonical_metric": "debt_current", "statement": "balance_sheet", "taxonomy": "ifrs-full", "concept": "CurrentBorrowings", "priority": 1, "period_type": "instant", "unit_type": "currency", "sign_policy": "positive", "currency_required": 1, "is_core": 0},
    {"canonical_metric": "debt_noncurrent", "statement": "balance_sheet", "taxonomy": "us-gaap", "concept": "LongTermDebtNoncurrent", "priority": 1, "period_type": "instant", "unit_type": "currency", "sign_policy": "positive", "currency_required": 1, "is_core": 0},
    {"canonical_metric": "debt_noncurrent", "statement": "balance_sheet", "taxonomy": "us-gaap", "concept": "LongTermDebtAndFinanceLeaseObligationsNoncurrent", "priority": 2, "period_type": "instant", "unit_type": "currency", "sign_policy": "positive", "currency_required": 1, "is_core": 0},
    {"canonical_metric": "debt_noncurrent", "statement": "balance_sheet", "taxonomy": "us-gaap", "concept": "LongTermDebt", "priority": 3, "period_type": "instant", "unit_type": "currency", "sign_policy": "positive", "currency_required": 1, "is_core": 0},
    {"canonical_metric": "debt_noncurrent", "statement": "balance_sheet", "taxonomy": "ifrs-full", "concept": "NoncurrentBorrowings", "priority": 1, "period_type": "instant", "unit_type": "currency", "sign_policy": "positive", "currency_required": 1, "is_core": 0},
    {"canonical_metric": "debt_total", "statement": "balance_sheet", "taxonomy": "ifrs-full", "concept": "Borrowings", "priority": 1, "period_type": "instant", "unit_type": "currency", "sign_policy": "positive", "currency_required": 1, "is_core": 0},
    # Deferred revenue / contract liabilities + remaining performance obligations.
    # Software booking / forward-demand signals; measurement-only (no production weight).
    {"canonical_metric": "deferred_revenue_current", "statement": "balance_sheet", "taxonomy": "us-gaap", "concept": "ContractWithCustomerLiabilityCurrent", "priority": 1, "period_type": "instant", "unit_type": "currency", "sign_policy": "positive", "currency_required": 1, "is_core": 0},
    {"canonical_metric": "deferred_revenue_current", "statement": "balance_sheet", "taxonomy": "us-gaap", "concept": "DeferredRevenueCurrent", "priority": 2, "period_type": "instant", "unit_type": "currency", "sign_policy": "positive", "currency_required": 1, "is_core": 0},
    {"canonical_metric": "deferred_revenue_current", "statement": "balance_sheet", "taxonomy": "ifrs-full", "concept": "CurrentContractLiabilities", "priority": 3, "period_type": "instant", "unit_type": "currency", "sign_policy": "positive", "currency_required": 1, "is_core": 0},
    {"canonical_metric": "deferred_revenue_noncurrent", "statement": "balance_sheet", "taxonomy": "us-gaap", "concept": "ContractWithCustomerLiabilityNoncurrent", "priority": 1, "period_type": "instant", "unit_type": "currency", "sign_policy": "positive", "currency_required": 1, "is_core": 0},
    {"canonical_metric": "deferred_revenue_noncurrent", "statement": "balance_sheet", "taxonomy": "us-gaap", "concept": "DeferredRevenueNoncurrent", "priority": 2, "period_type": "instant", "unit_type": "currency", "sign_policy": "positive", "currency_required": 1, "is_core": 0},
    {"canonical_metric": "deferred_revenue_noncurrent", "statement": "balance_sheet", "taxonomy": "ifrs-full", "concept": "NoncurrentContractLiabilities", "priority": 3, "period_type": "instant", "unit_type": "currency", "sign_policy": "positive", "currency_required": 1, "is_core": 0},
    {"canonical_metric": "deferred_revenue_total", "statement": "balance_sheet", "taxonomy": "us-gaap", "concept": "ContractWithCustomerLiability", "priority": 1, "period_type": "instant", "unit_type": "currency", "sign_policy": "positive", "currency_required": 1, "is_core": 0},
    {"canonical_metric": "deferred_revenue_total", "statement": "balance_sheet", "taxonomy": "us-gaap", "concept": "DeferredRevenue", "priority": 2, "period_type": "instant", "unit_type": "currency", "sign_policy": "positive", "currency_required": 1, "is_core": 0},
    {"canonical_metric": "deferred_revenue_total", "statement": "balance_sheet", "taxonomy": "ifrs-full", "concept": "ContractLiabilities", "priority": 3, "period_type": "instant", "unit_type": "currency", "sign_policy": "positive", "currency_required": 1, "is_core": 0},
    {"canonical_metric": "remaining_performance_obligation", "statement": "balance_sheet", "taxonomy": "us-gaap", "concept": "RevenueRemainingPerformanceObligation", "priority": 1, "period_type": "instant", "unit_type": "currency", "sign_policy": "positive", "currency_required": 1, "is_core": 0},
    {"canonical_metric": "remaining_performance_obligation", "statement": "balance_sheet", "taxonomy": "ifrs-full", "concept": "TransactionPriceAllocatedToRemainingPerformanceObligations", "priority": 2, "period_type": "instant", "unit_type": "currency", "sign_policy": "positive", "currency_required": 1, "is_core": 0},
    {"canonical_metric": "diluted_shares", "statement": "income_statement", "taxonomy": "us-gaap", "concept": "WeightedAverageNumberOfDilutedSharesOutstanding", "priority": 1, "period_type": "duration", "unit_type": "shares", "sign_policy": "positive", "currency_required": 0, "is_core": 0},
    {"canonical_metric": "diluted_shares", "statement": "income_statement", "taxonomy": "us-gaap", "concept": "WeightedAverageNumberOfShareDiluted", "priority": 2, "period_type": "duration", "unit_type": "shares", "sign_policy": "positive", "currency_required": 0, "is_core": 0},
    {"canonical_metric": "diluted_shares", "statement": "income_statement", "taxonomy": "ifrs-full", "concept": "AdjustedWeightedAverageShares", "priority": 1, "period_type": "duration", "unit_type": "shares", "sign_policy": "positive", "currency_required": 0, "is_core": 0},
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
    canonical_tables TEXT,
    feature_stages TEXT,
    subsector_scope TEXT NOT NULL DEFAULT 'technology',
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

CREATE TABLE IF NOT EXISTS dim_technology_taxonomy (
    taxonomy_id INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id INTEGER,
    ticker TEXT NOT NULL,
    model_family TEXT NOT NULL,
    sector TEXT NOT NULL DEFAULT 'Technology',
    subsector TEXT NOT NULL,
    calibration_cohort_id TEXT,
    calibration_cohort TEXT,
    subindustry_role TEXT,
    calibration_use TEXT,
    liquidity_instrument_flag TEXT,
    taxonomy_confidence REAL NOT NULL DEFAULT 0.0,
    taxonomy_source TEXT,
    analyst_reviewed INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (company_id) REFERENCES dim_company(company_id) ON DELETE CASCADE,
    UNIQUE(ticker, model_family)
);

CREATE TABLE IF NOT EXISTS dim_universe_membership (
    membership_id INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id INTEGER,
    ticker TEXT NOT NULL,
    model_family TEXT NOT NULL,
    membership_source_id TEXT,
    membership_basis TEXT NOT NULL DEFAULT 'current_source_of_truth',
    start_date TEXT NOT NULL DEFAULT '1900-01-01',
    end_date TEXT,
    membership_status TEXT NOT NULL DEFAULT 'active',
    is_current_member INTEGER NOT NULL DEFAULT 1,
    point_in_time_flag INTEGER NOT NULL DEFAULT 0,
    confidence REAL NOT NULL DEFAULT 1.0,
    reason TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (company_id) REFERENCES dim_company(company_id) ON DELETE CASCADE,
    FOREIGN KEY (membership_source_id) REFERENCES source_registry(source_id) ON DELETE SET NULL,
    UNIQUE(ticker, model_family, membership_source_id, start_date)
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

CREATE TABLE IF NOT EXISTS fact_corporate_action (
    ticker TEXT NOT NULL,
    action_date TEXT NOT NULL,
    source_id TEXT NOT NULL,
    action_type TEXT NOT NULL,
    cash_amount REAL,
    split_numerator REAL,
    split_denominator REAL,
    split_factor REAL,
    raw_value TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(ticker, action_date, source_id, action_type),
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
    model_family TEXT NOT NULL DEFAULT 'semiconductors',
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
    rel_strength_smh_3m REAL,
    rel_strength_soxx_3m REAL,
    rel_strength_qqq_3m REAL,
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
    cik TEXT NOT NULL,
    accession_number TEXT NOT NULL,
    source_id TEXT NOT NULL,
    form_type TEXT NOT NULL,
    filing_date TEXT,
    report_date TEXT,
    acceptance_datetime TEXT,
    primary_document TEXT,
    primary_doc_description TEXT,
    fiscal_year INTEGER,
    fiscal_period TEXT,
    is_amendment INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(ticker, accession_number, source_id),
    FOREIGN KEY (source_id) REFERENCES source_registry(source_id) ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS dim_issuer_reporting_profile (
    ticker TEXT PRIMARY KEY,
    cik TEXT NOT NULL,
    source_id TEXT NOT NULL,
    primary_reporting_taxonomy TEXT,
    secondary_taxonomies TEXT,
    primary_annual_form TEXT,
    primary_quarterly_form TEXT,
    is_foreign_private_issuer INTEGER NOT NULL DEFAULT 0,
    has_us_gaap_facts INTEGER NOT NULL DEFAULT 0,
    has_ifrs_full_facts INTEGER NOT NULL DEFAULT 0,
    has_dei_facts INTEGER NOT NULL DEFAULT 0,
    has_operating_financial_facts INTEGER NOT NULL DEFAULT 0,
    financial_statement_frequency TEXT,
    latest_operating_filing_date TEXT,
    latest_operating_form TEXT,
    latest_companyfacts_filing_date TEXT,
    companyfacts_lag_flag INTEGER NOT NULL DEFAULT 0,
    companyfacts_lag_status TEXT,
    coverage_status TEXT,
    review_reason TEXT,
    calibration_fundamental_eligible INTEGER NOT NULL DEFAULT 0,
    calibration_exclusion_reason TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (source_id) REFERENCES source_registry(source_id) ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS dim_xbrl_concept_map (
    concept_map_id INTEGER PRIMARY KEY AUTOINCREMENT,
    canonical_metric TEXT NOT NULL,
    statement TEXT NOT NULL,
    taxonomy TEXT NOT NULL,
    concept TEXT NOT NULL,
    priority INTEGER NOT NULL DEFAULT 100,
    period_type TEXT NOT NULL,
    unit_type TEXT NOT NULL,
    sign_policy TEXT NOT NULL DEFAULT 'as_reported',
    currency_required INTEGER NOT NULL DEFAULT 0,
    is_core INTEGER NOT NULL DEFAULT 0,
    fallback_group TEXT,
    notes TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(canonical_metric, taxonomy, concept)
);

CREATE TABLE IF NOT EXISTS fact_sec_xbrl_fact_raw (
    fact_key TEXT PRIMARY KEY,
    ticker TEXT NOT NULL,
    cik TEXT NOT NULL,
    source_id TEXT NOT NULL,
    taxonomy TEXT NOT NULL,
    concept TEXT NOT NULL,
    unit TEXT NOT NULL,
    value REAL,
    start_date TEXT,
    end_date TEXT,
    fiscal_year INTEGER,
    fiscal_period TEXT,
    form_type TEXT,
    filing_date TEXT,
    accession_number TEXT,
    frame TEXT,
    period_type TEXT,
    source_detail TEXT,
    source_accession_url TEXT,
    source_payload_hash TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (source_id) REFERENCES source_registry(source_id) ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS fact_sec_xbrl_fact (
    fact_key TEXT PRIMARY KEY,
    ticker TEXT NOT NULL,
    cik TEXT NOT NULL,
    taxonomy TEXT NOT NULL,
    concept TEXT NOT NULL,
    metric_name TEXT NOT NULL,
    unit TEXT NOT NULL,
    accession_number TEXT NOT NULL,
    source_id TEXT NOT NULL,
    form_type TEXT,
    filing_date TEXT,
    fiscal_year INTEGER,
    fiscal_period TEXT,
    start_date TEXT,
    end_date TEXT,
    frame TEXT,
    value REAL,
    decimals TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (source_id) REFERENCES source_registry(source_id) ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS fact_financial_statement_canonical (
    canonical_fact_key TEXT PRIMARY KEY,
    ticker TEXT NOT NULL,
    cik TEXT NOT NULL,
    source_id TEXT NOT NULL,
    period_end_date TEXT NOT NULL,
    period_start_date TEXT,
    fiscal_year INTEGER,
    fiscal_period TEXT,
    form_type TEXT,
    filing_date TEXT,
    accepted_at TEXT,
    accession_number TEXT NOT NULL,
    canonical_metric TEXT NOT NULL,
    value_reported_currency REAL,
    reported_currency TEXT,
    value_usd REAL,
    source_taxonomy TEXT NOT NULL,
    source_concept TEXT NOT NULL,
    source_unit TEXT,
    source_priority INTEGER NOT NULL DEFAULT 100,
    source_quality REAL NOT NULL DEFAULT 0.0,
    is_direct_reported INTEGER NOT NULL DEFAULT 1,
    is_derived INTEGER NOT NULL DEFAULT 0,
    is_annual_only INTEGER NOT NULL DEFAULT 0,
    source_detail TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (source_id) REFERENCES source_registry(source_id) ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS fact_fx_rate (
    base_currency TEXT NOT NULL,
    quote_currency TEXT NOT NULL DEFAULT 'USD',
    rate_date TEXT NOT NULL,
    source_id TEXT NOT NULL,
    rate_type TEXT NOT NULL DEFAULT 'close',
    fx_rate REAL NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(base_currency, quote_currency, rate_date, source_id, rate_type),
    FOREIGN KEY (source_id) REFERENCES source_registry(source_id) ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS feature_financial_statement (
    ticker TEXT NOT NULL,
    asof_date TEXT NOT NULL,
    source_id TEXT NOT NULL,
    model_family TEXT NOT NULL DEFAULT 'semiconductors',
    accession_number TEXT,
    form_type TEXT,
    fiscal_period_end TEXT NOT NULL,
    fiscal_year INTEGER,
    fiscal_period TEXT,
    reporting_standard TEXT,
    financial_frequency TEXT,
    reported_currency TEXT,
    fx_conversion_status TEXT,
    fx_rate_income_statement REAL,
    fx_rate_balance_sheet REAL,
    revenue REAL,
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
    market_cap REAL,
    ev_gross_profit REAL,
    ev_operating_income REAL,
    fcf_yield REAL,
    canonical_quality TEXT,
    data_quality_status TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(ticker, asof_date, source_id, model_family, fiscal_period_end),
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

CREATE TABLE IF NOT EXISTS dim_insider_reporting_profile (
    ticker TEXT PRIMARY KEY,
    cik TEXT NOT NULL,
    source_id TEXT NOT NULL,
    issuer_type TEXT,
    issuer_country TEXT,
    primary_insider_source TEXT,
    section16_expected_status TEXT,
    fpi_qualifying_exemption_status TEXT,
    local_insider_source_required INTEGER NOT NULL DEFAULT 0,
    hfia_effective_date TEXT,
    latest_ownership_filing_date TEXT,
    latest_ownership_form TEXT,
    ownership_filing_count INTEGER NOT NULL DEFAULT 0,
    ownership_transaction_count INTEGER NOT NULL DEFAULT 0,
    direct_sec_checked_at TEXT,
    coverage_status TEXT,
    review_reason TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (source_id) REFERENCES source_registry(source_id) ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS fact_sec_ownership_filing (
    ticker TEXT NOT NULL,
    accession_number TEXT NOT NULL,
    reporting_owner_cik TEXT NOT NULL DEFAULT '',
    source_id TEXT NOT NULL,
    cik TEXT NOT NULL,
    issuer_cik TEXT,
    issuer_name TEXT,
    issuer_trading_symbol TEXT,
    foreign_trading_symbol TEXT,
    form_type TEXT NOT NULL,
    filed_date TEXT,
    accepted_datetime TEXT,
    period_of_report TEXT,
    reporting_owner_name TEXT,
    reporting_owner_relationship TEXT,
    reporting_owner_title TEXT,
    is_director INTEGER,
    is_officer INTEGER,
    is_ten_percent_owner INTEGER,
    has_nonderivative_transactions INTEGER NOT NULL DEFAULT 0,
    has_derivative_transactions INTEGER NOT NULL DEFAULT 0,
    has_holdings INTEGER NOT NULL DEFAULT 0,
    parsed_successfully INTEGER NOT NULL DEFAULT 0,
    parse_error TEXT,
    source_url TEXT,
    raw_xml_hash TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(ticker, accession_number, reporting_owner_cik, source_id),
    FOREIGN KEY (source_id) REFERENCES source_registry(source_id) ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS fact_sec_ownership_nonderivative_transaction (
    ticker TEXT NOT NULL,
    accession_number TEXT NOT NULL,
    transaction_seq INTEGER NOT NULL,
    reporting_owner_cik TEXT NOT NULL DEFAULT '',
    source_id TEXT NOT NULL,
    security_title TEXT,
    transaction_date TEXT,
    deemed_execution_date TEXT,
    transaction_code TEXT,
    equity_swap_involved INTEGER,
    transaction_shares REAL,
    transaction_price_per_share REAL,
    transaction_value REAL,
    acquired_disposed_code TEXT,
    shares_owned_following_transaction REAL,
    direct_or_indirect_ownership TEXT,
    nature_of_ownership TEXT,
    footnotes_json TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(ticker, accession_number, transaction_seq, reporting_owner_cik, source_id),
    FOREIGN KEY (source_id) REFERENCES source_registry(source_id) ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS fact_sec_ownership_derivative_transaction (
    ticker TEXT NOT NULL,
    accession_number TEXT NOT NULL,
    transaction_seq INTEGER NOT NULL,
    reporting_owner_cik TEXT NOT NULL DEFAULT '',
    source_id TEXT NOT NULL,
    security_title TEXT,
    conversion_or_exercise_price REAL,
    transaction_date TEXT,
    deemed_execution_date TEXT,
    transaction_code TEXT,
    equity_swap_involved INTEGER,
    transaction_shares REAL,
    transaction_price_per_share REAL,
    transaction_value REAL,
    acquired_disposed_code TEXT,
    exercise_date TEXT,
    expiration_date TEXT,
    underlying_security_title TEXT,
    underlying_security_shares REAL,
    shares_owned_following_transaction REAL,
    direct_or_indirect_ownership TEXT,
    nature_of_ownership TEXT,
    footnotes_json TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(ticker, accession_number, transaction_seq, reporting_owner_cik, source_id),
    FOREIGN KEY (source_id) REFERENCES source_registry(source_id) ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS fact_sec_ownership_holding (
    ticker TEXT NOT NULL,
    accession_number TEXT NOT NULL,
    holding_type TEXT NOT NULL,
    holding_seq INTEGER NOT NULL,
    reporting_owner_cik TEXT NOT NULL DEFAULT '',
    source_id TEXT NOT NULL,
    security_title TEXT,
    conversion_or_exercise_price REAL,
    exercise_date TEXT,
    expiration_date TEXT,
    ownership_shares REAL,
    underlying_security_title TEXT,
    underlying_security_shares REAL,
    direct_or_indirect_ownership TEXT,
    nature_of_ownership TEXT,
    footnotes_json TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(ticker, accession_number, holding_type, holding_seq, reporting_owner_cik, source_id),
    FOREIGN KEY (source_id) REFERENCES source_registry(source_id) ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS fact_13f_positioning (
    ticker TEXT NOT NULL,
    asof_date TEXT NOT NULL,
    period_of_report TEXT,
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
    PRIMARY KEY(ticker, asof_date, source_id),
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
    float_source TEXT,
    float_source_asof_date TEXT,
    float_measurement_date TEXT,
    float_proxy_flag INTEGER,
    float_confidence REAL,
    float_selection_reason TEXT,
    float_split_adjustment_factor REAL,
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
    model_family TEXT NOT NULL DEFAULT 'semiconductors',
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
    positioning_quality TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(ticker, asof_date, source_id, model_family),
    FOREIGN KEY (source_id) REFERENCES source_registry(source_id) ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS dim_scoring_component (
    model_family TEXT NOT NULL,
    component_name TEXT NOT NULL,
    component_group TEXT NOT NULL,
    display_name TEXT NOT NULL,
    description TEXT,
    is_core_component INTEGER NOT NULL DEFAULT 0,
    default_score REAL NOT NULL DEFAULT 50.0,
    default_quality REAL NOT NULL DEFAULT 0.0,
    default_status TEXT NOT NULL DEFAULT 'not_loaded',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(model_family, component_name)
);

CREATE TABLE IF NOT EXISTS feature_scoring_input (
    ticker TEXT NOT NULL,
    asof_date TEXT NOT NULL,
    source_id TEXT NOT NULL,
    model_family TEXT NOT NULL DEFAULT 'semiconductors',
    scoring_contract_version TEXT NOT NULL,
    calibration_cohort_id TEXT,
    calibration_cohort TEXT,
    market_feature_asof_date TEXT,
    financial_feature_asof_date TEXT,
    financial_source_accession TEXT,
    financial_source_fiscal_period_end TEXT,
    financial_source_feature_updated_at TEXT,
    positioning_feature_asof_date TEXT,
    reporting_standard TEXT,
    financial_frequency TEXT,
    latest_price REAL,
    market_cap REAL,
    revenue_yoy_growth REAL,
    gross_profit_yoy_growth REAL,
    operating_income_yoy_growth REAL,
    free_cash_flow_yoy_growth REAL,
    revenue_acceleration REAL,
    gross_margin REAL,
    operating_margin REAL,
    fcf_margin REAL,
    fcf_to_net_income REAL,
    net_cash_to_assets REAL,
    sbc_pct_revenue REAL,
    r_and_d_pct_revenue REAL,
    share_count_yoy_growth REAL,
    inventory_days REAL,
    ev_gross_profit REAL,
    ev_operating_income REAL,
    fcf_yield REAL,
    ret_3m REAL,
    ret_12m_ex_1m REAL,
    rel_strength_bench_3m REAL,
    rel_strength_soxx_3m REAL,
    realized_vol_60d REAL,
    max_drawdown_12m REAL,
    distance_from_52w_high REAL,
    avg_dollar_volume_60d REAL,
    low_liquidity_flag INTEGER NOT NULL DEFAULT 0,
    insider_net_value_90d REAL,
    insider_cluster_buyers_90d REAL,
    institutional_ownership_delta_pct REAL,
    latest_short_interest_pct_float REAL,
    short_interest_change_3m REAL,
    latest_days_to_cover REAL,
    latest_borrow_fee_rate REAL,
    quality_score REAL NOT NULL DEFAULT 50.0,
    growth_score REAL NOT NULL DEFAULT 50.0,
    valuation_score REAL NOT NULL DEFAULT 50.0,
    market_behavior_score REAL NOT NULL DEFAULT 50.0,
    positioning_score REAL NOT NULL DEFAULT 50.0,
    risk_control_score REAL NOT NULL DEFAULT 50.0,
    sector_cycle_score REAL NOT NULL DEFAULT 50.0,
    equipment_cycle_score REAL NOT NULL DEFAULT 50.0,
    sector_inventory_cycle_score REAL NOT NULL DEFAULT 50.0,
    big_tech_capex_score REAL NOT NULL DEFAULT 50.0,
    memory_ai_proxy_score REAL NOT NULL DEFAULT 50.0,
    innovation_score REAL NOT NULL DEFAULT 50.0,
    geo_customer_risk_score REAL NOT NULL DEFAULT 50.0,
    sector_overlay_score REAL NOT NULL DEFAULT 50.0,
    quality_component_quality REAL NOT NULL DEFAULT 0.0,
    growth_component_quality REAL NOT NULL DEFAULT 0.0,
    valuation_component_quality REAL NOT NULL DEFAULT 0.0,
    market_component_quality REAL NOT NULL DEFAULT 0.0,
    positioning_component_quality REAL NOT NULL DEFAULT 0.0,
    risk_component_quality REAL NOT NULL DEFAULT 0.0,
    sector_overlay_quality REAL NOT NULL DEFAULT 0.0,
    sector_overlay_status TEXT NOT NULL DEFAULT 'not_loaded',
    core_available_component_count INTEGER NOT NULL DEFAULT 0,
    core_missing_component_count INTEGER NOT NULL DEFAULT 0,
    core_data_quality_confidence REAL NOT NULL DEFAULT 0.0,
    full_data_quality_confidence REAL NOT NULL DEFAULT 0.0,
    market_quality TEXT,
    financial_quality TEXT,
    positioning_quality TEXT,
    rank_ready_flag INTEGER NOT NULL DEFAULT 0,
    calibration_eligible_flag INTEGER NOT NULL DEFAULT 0,
    feature_status TEXT NOT NULL DEFAULT 'review',
    review_reason TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(ticker, asof_date, source_id, model_family),
    FOREIGN KEY (source_id) REFERENCES source_registry(source_id) ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS feature_scoring_component (
    ticker TEXT NOT NULL,
    asof_date TEXT NOT NULL,
    source_id TEXT NOT NULL,
    model_family TEXT NOT NULL DEFAULT 'semiconductors',
    component_name TEXT NOT NULL,
    component_group TEXT NOT NULL,
    calibration_cohort_id TEXT,
    component_score REAL NOT NULL DEFAULT 50.0,
    universe_percentile REAL,
    cohort_percentile REAL,
    component_quality REAL NOT NULL DEFAULT 0.0,
    component_status TEXT NOT NULL DEFAULT 'review',
    available_subfeature_count INTEGER NOT NULL DEFAULT 0,
    missing_subfeature_count INTEGER NOT NULL DEFAULT 0,
    default_applied INTEGER NOT NULL DEFAULT 0,
    review_reason TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(ticker, asof_date, source_id, model_family, component_name),
    FOREIGN KEY (source_id) REFERENCES source_registry(source_id) ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS feature_scoring_model_output (
    ticker TEXT NOT NULL,
    asof_date TEXT NOT NULL,
    source_id TEXT NOT NULL,
    model_family TEXT NOT NULL DEFAULT 'semiconductors',
    model_version TEXT NOT NULL,
    baseline_source_id TEXT,
    core_score REAL NOT NULL DEFAULT 50.0,
    sector_overlay_score REAL NOT NULL DEFAULT 50.0,
    final_score REAL NOT NULL DEFAULT 50.0,
    final_rank INTEGER,
    final_percentile REAL,
    component_weights_json TEXT,
    component_scores_json TEXT,
    component_quality_json TEXT,
    data_quality_confidence REAL NOT NULL DEFAULT 0.0,
    rank_ready_flag INTEGER NOT NULL DEFAULT 0,
    calibration_eligible_flag INTEGER NOT NULL DEFAULT 0,
    model_status TEXT NOT NULL DEFAULT 'review',
    review_reason TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(ticker, asof_date, source_id, model_family),
    FOREIGN KEY (source_id) REFERENCES source_registry(source_id) ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS fact_semiconductor_wsts_billings (
    source_id TEXT NOT NULL,
    dataset_type TEXT NOT NULL,
    period_month TEXT NOT NULL,
    region TEXT NOT NULL,
    value_usd_thousands REAL NOT NULL,
    value_millions_usd REAL NOT NULL,
    source_url TEXT,
    source_file TEXT,
    workbook_sheet TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(source_id, dataset_type, period_month, region),
    FOREIGN KEY (source_id) REFERENCES source_registry(source_id) ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS feature_semiconductor_sector_cycle (
    asof_date TEXT NOT NULL,
    source_id TEXT NOT NULL,
    model_family TEXT NOT NULL DEFAULT 'semiconductors',
    latest_month TEXT,
    global_sales_millions_usd REAL,
    global_sales_yoy REAL,
    global_sales_3m_change REAL,
    global_sales_6m_change REAL,
    global_3mma_millions_usd REAL,
    global_3mma_3m_change REAL,
    regional_breadth_score REAL,
    sector_cycle_score REAL NOT NULL DEFAULT 50.0,
    component_quality REAL NOT NULL DEFAULT 0.0,
    stale_data INTEGER NOT NULL DEFAULT 0,
    source_status TEXT NOT NULL DEFAULT 'review',
    data_quality_status TEXT NOT NULL DEFAULT 'review',
    review_reason TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(asof_date, source_id, model_family),
    FOREIGN KEY (source_id) REFERENCES source_registry(source_id) ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS fact_big_tech_capex (
    ticker TEXT NOT NULL,
    calendar_period TEXT NOT NULL,
    source_id TEXT NOT NULL,
    cik TEXT NOT NULL,
    period_start_date TEXT,
    period_end_date TEXT NOT NULL,
    fiscal_year INTEGER,
    fiscal_period TEXT,
    form_type TEXT,
    filed_date TEXT,
    accession_number TEXT NOT NULL,
    source_concept TEXT NOT NULL,
    frame TEXT,
    duration_days INTEGER,
    capex_usd REAL NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(ticker, calendar_period, source_id, accession_number, source_concept),
    FOREIGN KEY (source_id) REFERENCES source_registry(source_id) ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS feature_big_tech_capex_cycle (
    asof_date TEXT NOT NULL,
    source_id TEXT NOT NULL,
    model_family TEXT NOT NULL DEFAULT 'semiconductors',
    latest_calendar_period TEXT,
    latest_period_end_date TEXT,
    latest_filed_date TEXT,
    companies_expected INTEGER NOT NULL DEFAULT 0,
    companies_current INTEGER NOT NULL DEFAULT 0,
    companies_yoy INTEGER NOT NULL DEFAULT 0,
    companies_qoq INTEGER NOT NULL DEFAULT 0,
    current_capex_usd REAL,
    prior_year_capex_usd REAL,
    prior_quarter_capex_usd REAL,
    capex_yoy_growth REAL,
    capex_qoq_growth REAL,
    capex_breadth_score REAL,
    big_tech_capex_score REAL NOT NULL DEFAULT 50.0,
    component_quality REAL NOT NULL DEFAULT 0.0,
    stale_data INTEGER NOT NULL DEFAULT 0,
    source_status TEXT NOT NULL DEFAULT 'review',
    data_quality_status TEXT NOT NULL DEFAULT 'review',
    review_reason TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(asof_date, source_id, model_family),
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

CREATE INDEX IF NOT EXISTS idx_dim_company_alias_norm
    ON dim_company_alias(alias_norm);

CREATE INDEX IF NOT EXISTS idx_dim_technology_taxonomy_model_subsector
    ON dim_technology_taxonomy(model_family, subsector);

CREATE INDEX IF NOT EXISTS idx_dim_universe_membership_lookup
    ON dim_universe_membership(model_family, ticker, start_date, end_date);

CREATE INDEX IF NOT EXISTS idx_dim_universe_membership_current
    ON dim_universe_membership(model_family, is_current_member, membership_basis);

CREATE INDEX IF NOT EXISTS idx_fact_price_ohlcv_ticker_date
    ON fact_price_ohlcv(ticker, bar_date);

CREATE INDEX IF NOT EXISTS idx_fact_price_ohlcv_source_date
    ON fact_price_ohlcv(source_id, bar_date);

CREATE INDEX IF NOT EXISTS idx_fact_price_ohlcv_pit_lookup
    ON fact_price_ohlcv(source_id, ticker, bar_date DESC);

CREATE INDEX IF NOT EXISTS idx_fact_corporate_action_ticker_date
    ON fact_corporate_action(ticker, action_date);

CREATE INDEX IF NOT EXISTS idx_fact_market_snapshot_ticker_asof
    ON fact_market_snapshot(ticker, asof_date);

CREATE INDEX IF NOT EXISTS idx_feature_market_technical_asof
    ON feature_market_technical(model_family, asof_date);

CREATE INDEX IF NOT EXISTS idx_feature_market_technical_pit_lookup
    ON feature_market_technical(model_family, source_id, ticker, asof_date DESC);

CREATE INDEX IF NOT EXISTS idx_fact_sec_filing_ticker_date
    ON fact_sec_filing(ticker, filing_date);

CREATE INDEX IF NOT EXISTS idx_fact_sec_xbrl_fact_ticker_metric_end
    ON fact_sec_xbrl_fact(ticker, metric_name, end_date);

CREATE INDEX IF NOT EXISTS idx_dim_issuer_reporting_profile_status
    ON dim_issuer_reporting_profile(coverage_status);

CREATE INDEX IF NOT EXISTS idx_dim_xbrl_concept_map_lookup
    ON dim_xbrl_concept_map(taxonomy, concept, canonical_metric);

CREATE INDEX IF NOT EXISTS idx_fact_sec_xbrl_fact_raw_ticker_taxonomy
    ON fact_sec_xbrl_fact_raw(ticker, taxonomy, end_date);

CREATE INDEX IF NOT EXISTS idx_fact_financial_statement_canonical_ticker_metric
    ON fact_financial_statement_canonical(ticker, canonical_metric, period_end_date);

CREATE INDEX IF NOT EXISTS idx_fact_fx_rate_lookup
    ON fact_fx_rate(base_currency, quote_currency, rate_date);

CREATE INDEX IF NOT EXISTS idx_feature_financial_statement_ticker_asof
    ON feature_financial_statement(model_family, ticker, asof_date);

CREATE INDEX IF NOT EXISTS idx_feature_financial_statement_pit_lookup
    ON feature_financial_statement(
        model_family, source_id, ticker, asof_date, fiscal_period_end DESC
    );

CREATE INDEX IF NOT EXISTS idx_fact_sec_form4_transaction_ticker_date
    ON fact_sec_form4_transaction(ticker, transaction_date);

CREATE INDEX IF NOT EXISTS idx_dim_insider_reporting_profile_status
    ON dim_insider_reporting_profile(section16_expected_status, coverage_status);

CREATE INDEX IF NOT EXISTS idx_fact_sec_ownership_filing_ticker_date
    ON fact_sec_ownership_filing(ticker, filed_date);

CREATE INDEX IF NOT EXISTS idx_fact_sec_ownership_filing_owner
    ON fact_sec_ownership_filing(reporting_owner_cik, filed_date);

CREATE INDEX IF NOT EXISTS idx_fact_sec_ownership_nonderiv_ticker_date
    ON fact_sec_ownership_nonderivative_transaction(ticker, transaction_date);

CREATE INDEX IF NOT EXISTS idx_fact_sec_ownership_deriv_ticker_date
    ON fact_sec_ownership_derivative_transaction(ticker, transaction_date);

CREATE INDEX IF NOT EXISTS idx_fact_sec_ownership_holding_ticker
    ON fact_sec_ownership_holding(ticker, accession_number);

CREATE INDEX IF NOT EXISTS idx_fact_13f_positioning_ticker_asof
    ON fact_13f_positioning(ticker, asof_date);

CREATE INDEX IF NOT EXISTS idx_fact_short_interest_ticker_settle
    ON fact_short_interest(ticker, settlement_date);

CREATE INDEX IF NOT EXISTS idx_fact_ibkr_borrow_snapshot_ticker_asof
    ON fact_ibkr_borrow_snapshot(ticker, asof_date);

CREATE INDEX IF NOT EXISTS idx_feature_positioning_asof
    ON feature_positioning(model_family, asof_date);

CREATE INDEX IF NOT EXISTS idx_feature_positioning_pit_lookup
    ON feature_positioning(model_family, source_id, ticker, asof_date DESC);

CREATE INDEX IF NOT EXISTS idx_feature_scoring_input_asof
    ON feature_scoring_input(model_family, asof_date);

CREATE INDEX IF NOT EXISTS idx_feature_scoring_input_pit_lookup
    ON feature_scoring_input(model_family, source_id, ticker, asof_date DESC);

CREATE INDEX IF NOT EXISTS idx_feature_scoring_component_lookup
    ON feature_scoring_component(model_family, asof_date, component_name);

CREATE INDEX IF NOT EXISTS idx_feature_scoring_model_output_asof
    ON feature_scoring_model_output(model_family, source_id, asof_date);

CREATE INDEX IF NOT EXISTS idx_fact_semiconductor_wsts_month
    ON fact_semiconductor_wsts_billings(dataset_type, period_month, region);

CREATE INDEX IF NOT EXISTS idx_feature_semiconductor_sector_cycle_asof
    ON feature_semiconductor_sector_cycle(model_family, asof_date);

CREATE INDEX IF NOT EXISTS idx_fact_big_tech_capex_period
    ON fact_big_tech_capex(ticker, calendar_period);

CREATE INDEX IF NOT EXISTS idx_feature_big_tech_capex_cycle_asof
    ON feature_big_tech_capex_cycle(model_family, asof_date);

CREATE INDEX IF NOT EXISTS idx_data_quality_issues_stage_ticker
    ON data_quality_issues(stage, ticker);
"""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _is_transient_sqlite_error(exc: sqlite3.OperationalError) -> bool:
    message = str(exc).lower()
    return any(marker in message for marker in TRANSIENT_SQLITE_MARKERS)


def connect(db_path: Path, *, timeout_sec: float = 30.0) -> sqlite3.Connection:
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
                # WAL is a performance/concurrency setting, not a schema requirement.
                # If another short-lived process is negotiating SQLite state, continue
                # with the existing journal mode and let normal write operations decide.
                break
            time.sleep(0.25 * (attempt + 1))
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    for attempt in range(3):
        try:
            with conn:
                conn.executescript(SCHEMA_SQL)
                migrate_schema(conn)
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
    ensure_column(conn, "dim_technology_taxonomy", "calibration_cohort_id", "TEXT")
    ensure_column(conn, "feature_market_technical", "low_liquidity_flag", "INTEGER NOT NULL DEFAULT 0")
    ensure_column(conn, "fact_short_interest", "float_source", "TEXT")
    ensure_column(conn, "fact_short_interest", "float_source_asof_date", "TEXT")
    ensure_column(conn, "fact_short_interest", "float_measurement_date", "TEXT")
    ensure_column(conn, "fact_short_interest", "float_proxy_flag", "INTEGER")
    ensure_column(conn, "fact_short_interest", "float_confidence", "REAL")
    ensure_column(conn, "fact_short_interest", "float_selection_reason", "TEXT")
    ensure_column(conn, "fact_short_interest", "float_split_adjustment_factor", "REAL")
    ensure_column(conn, "feature_financial_statement", "reporting_standard", "TEXT")
    ensure_column(conn, "feature_financial_statement", "financial_frequency", "TEXT")
    ensure_column(conn, "feature_financial_statement", "reported_currency", "TEXT")
    ensure_column(conn, "feature_financial_statement", "fx_conversion_status", "TEXT")
    ensure_column(conn, "feature_financial_statement", "fx_rate_income_statement", "REAL")
    ensure_column(conn, "feature_financial_statement", "fx_rate_balance_sheet", "REAL")
    ensure_column(conn, "feature_financial_statement", "canonical_quality", "TEXT")
    ensure_column(conn, "feature_financial_statement", "deferred_revenue", "REAL")
    ensure_column(conn, "feature_financial_statement", "remaining_performance_obligation", "REAL")
    ensure_column(conn, "feature_financial_statement", "accounts_receivable", "REAL")
    ensure_column(conn, "feature_financial_statement", "accounts_payable", "REAL")
    ensure_column(conn, "feature_financial_statement", "days_sales_outstanding", "REAL")
    ensure_column(conn, "feature_financial_statement", "days_payables_outstanding", "REAL")
    ensure_column(conn, "feature_financial_statement", "cash_conversion_cycle", "REAL")
    for column_name in (
        "revenue_usd",
        "gross_profit_usd",
        "operating_income_usd",
        "net_income_usd",
        "operating_cash_flow_usd",
        "capex_usd",
        "free_cash_flow_usd",
        "assets_usd",
        "liabilities_usd",
        "equity_usd",
        "cash_and_equivalents_usd",
        "total_debt_usd",
        "inventory_usd",
        "accounts_receivable_usd",
        "accounts_payable_usd",
    ):
        ensure_column(conn, "feature_financial_statement", column_name, "REAL")
    for table_name in ("dim_issuer_reporting_profile",):
        ensure_column(conn, table_name, "latest_companyfacts_filing_date", "TEXT")
        ensure_column(conn, table_name, "companyfacts_lag_flag", "INTEGER NOT NULL DEFAULT 0")
        ensure_column(conn, table_name, "companyfacts_lag_status", "TEXT")
        ensure_column(conn, table_name, "calibration_fundamental_eligible", "INTEGER NOT NULL DEFAULT 0")
        ensure_column(conn, table_name, "calibration_exclusion_reason", "TEXT")
    ensure_column(conn, "fact_sec_xbrl_fact_raw", "source_detail", "TEXT")
    ensure_column(conn, "fact_sec_xbrl_fact_raw", "source_accession_url", "TEXT")
    ensure_column(conn, "fact_financial_statement_canonical", "source_detail", "TEXT")
    ensure_column(conn, "fact_financial_statement_canonical", "accepted_at", "TEXT")
    ensure_column(conn, "feature_scoring_input", "rel_strength_bench_3m", "REAL")
    ensure_column(conn, "feature_scoring_input", "financial_source_accession", "TEXT")
    ensure_column(conn, "feature_scoring_input", "financial_source_fiscal_period_end", "TEXT")
    ensure_column(conn, "feature_scoring_input", "financial_source_feature_updated_at", "TEXT")


def seed_xbrl_concept_map(conn: sqlite3.Connection) -> None:
    now = utc_now()
    for row in XBRL_CONCEPT_MAP_SEED:
        conn.execute(
            """
            INSERT INTO dim_xbrl_concept_map(
                canonical_metric, statement, taxonomy, concept, priority, period_type,
                unit_type, sign_policy, currency_required, is_core, fallback_group,
                notes, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(canonical_metric, taxonomy, concept) DO UPDATE SET
                statement = excluded.statement,
                priority = excluded.priority,
                period_type = excluded.period_type,
                unit_type = excluded.unit_type,
                sign_policy = excluded.sign_policy,
                currency_required = excluded.currency_required,
                is_core = excluded.is_core,
                fallback_group = excluded.fallback_group,
                notes = excluded.notes,
                updated_at = excluded.updated_at
            """,
            (
                row["canonical_metric"],
                row["statement"],
                row["taxonomy"],
                row["concept"],
                int(row.get("priority", 100)),
                row["period_type"],
                row["unit_type"],
                row.get("sign_policy", "as_reported"),
                int(row.get("currency_required", 0)),
                int(row.get("is_core", 0)),
                str(row.get("fallback_group") or ""),
                str(row.get("notes") or ""),
                now,
                now,
            ),
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
