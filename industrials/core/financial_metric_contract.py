from __future__ import annotations


AVAILABILITY_STATUSES = frozenset(
    {
        "REPORTED",
        "PROXY",
        "EXEMPT",
        "NOT_APPLICABLE",
        "NOT_DISCLOSED",
        "DISCLOSED_UNPARSED",
        "PARSER_FAILURE",
    }
)

SOURCE_METRIC_FEATURES: dict[str, str] = {
    "orders": "orders",
    "funded_backlog": "funded_backlog",
    "reported_backlog": "reported_backlog",
    "remaining_performance_obligation": "remaining_performance_obligation",
    "rpo_current": "rpo_current",
}

DERIVED_METRIC_FEATURES: dict[str, str] = {
    "orders_yoy_growth": "orders_yoy_growth",
    "book_to_bill": "book_to_bill",
    "backlog_yoy_growth": "backlog_yoy_growth",
    "backlog_to_revenue": "backlog_to_revenue",
    "reported_backlog_yoy_growth": "reported_backlog_yoy_growth",
    "reported_backlog_to_revenue": "reported_backlog_to_revenue",
    "rpo_yoy_growth": "rpo_yoy_growth",
    "rpo_to_revenue": "rpo_to_revenue",
    "roic": "roic",
    "asset_turnover": "asset_turnover",
    "incremental_operating_margin": "incremental_operating_margin",
    "inventory_sales_growth_spread": "inventory_sales_growth_spread",
    "cash_conversion_cycle_change": "cash_conversion_cycle_change",
    "net_debt_to_ebitda": "net_debt_to_ebitda",
    "interest_coverage": "interest_coverage",
    "cash_runway_years": "cash_runway_years",
    "capital_raise_dependence": "capital_raise_dependence",
    "diluted_shares_yoy_growth": "diluted_shares_yoy_growth",
}

PROXY_METRIC_FEATURES: dict[str, str] = {
    "rpo_implied_orders": "rpo_implied_orders",
    "rpo_implied_book_to_bill": "rpo_implied_book_to_bill",
    "contract_load_proxy": "contract_load_proxy",
    "contract_load_proxy_yoy_growth": "contract_load_proxy_yoy_growth",
    "contract_load_proxy_to_revenue": "contract_load_proxy_to_revenue",
}

REQUIRED_METRIC_FEATURES: dict[str, str] = {
    **SOURCE_METRIC_FEATURES,
    **DERIVED_METRIC_FEATURES,
    **PROXY_METRIC_FEATURES,
}

METRIC_OPERANDS: dict[str, tuple[str, ...]] = {
    "orders_yoy_growth": ("orders", "prior_comparable_orders"),
    "book_to_bill": ("orders_ttm", "revenue_ttm"),
    "backlog_yoy_growth": (
        "funded_backlog",
        "prior_comparable_funded_backlog",
    ),
    "backlog_to_revenue": ("funded_backlog", "revenue_ttm"),
    "reported_backlog_yoy_growth": (
        "reported_backlog",
        "prior_comparable_reported_backlog",
    ),
    "reported_backlog_to_revenue": (
        "reported_backlog",
        "revenue_ttm",
    ),
    "rpo_yoy_growth": (
        "remaining_performance_obligation",
        "prior_comparable_rpo",
    ),
    "rpo_to_revenue": (
        "remaining_performance_obligation",
        "revenue_ttm",
    ),
    "rpo_implied_orders": (
        "remaining_performance_obligation",
        "prior_comparable_rpo",
        "revenue_ttm",
    ),
    "rpo_implied_book_to_bill": (
        "rpo_implied_orders",
        "revenue_ttm",
    ),
    "contract_load_proxy": (
        "reported_backlog",
        "remaining_performance_obligation",
    ),
    "contract_load_proxy_yoy_growth": (
        "contract_load_proxy",
        "prior_comparable_same_source_contract_load",
    ),
    "contract_load_proxy_to_revenue": (
        "contract_load_proxy",
        "revenue_ttm",
    ),
    "roic": ("operating_income_ttm", "average_invested_capital"),
    "asset_turnover": ("revenue_ttm", "average_assets"),
    "incremental_operating_margin": (
        "operating_income_change",
        "revenue_change",
    ),
    "inventory_sales_growth_spread": (
        "inventory_growth",
        "revenue_yoy_growth",
    ),
    "cash_conversion_cycle_change": (
        "cash_conversion_cycle",
        "prior_cash_conversion_cycle",
    ),
    "net_debt_to_ebitda": ("net_debt", "ebitda_ttm"),
    "interest_coverage": (
        "operating_income_ttm",
        "interest_expense_ttm",
    ),
    "cash_runway_years": ("cash_and_equivalents", "cash_burn_ttm"),
    "capital_raise_dependence": (
        "equity_issuance_ttm",
        "debt_issuance_ttm",
        "cash_burn_ttm",
    ),
    "diluted_shares_yoy_growth": (
        "diluted_shares",
        "prior_comparable_diluted_shares",
    ),
}

SUPPLEMENTAL_METRICS = frozenset(
    {
        *SOURCE_METRIC_FEATURES,
        "capex",
        "operating_cash_flow",
        "operating_income",
        "revenue",
        "shares_outstanding",
        "debt_total",
    }
)

SUPPLEMENTAL_TAXONOMIES = frozenset(
    {
        "dedicated-parser",
        "dei",
        "issuer-ir",
        "sec-footnote",
        "sec-text",
        "transportation-reviewed",
    }
)

AVAILABILITY_MODEL_FAMILIES = frozenset({"defense", "machinery"})


def required_metric_names() -> tuple[str, ...]:
    return tuple(REQUIRED_METRIC_FEATURES)
