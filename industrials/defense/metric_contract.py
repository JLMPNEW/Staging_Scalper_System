from __future__ import annotations

from types import MappingProxyType


# Raw feature columns exported into the cross-sector rank-table contract.
BASE_FEATURE_ALIASES = MappingProxyType(
    {
        "latest_price": "financial_latest_price",
        "revenue_yoy_growth": "financial_revenue_yoy_growth",
        "gross_profit_yoy_growth": "financial_gross_profit_yoy_growth",
        "operating_income_yoy_growth": "financial_operating_income_yoy_growth",
        "free_cash_flow_yoy_growth": "financial_free_cash_flow_yoy_growth",
        "revenue_acceleration": "financial_revenue_acceleration",
        "gross_margin": "financial_gross_margin",
        "operating_margin": "financial_operating_margin",
        "fcf_margin": "financial_fcf_margin",
        "fcf_to_net_income": "financial_fcf_to_net_income",
        "net_cash_to_assets": "financial_net_cash_to_assets",
        "sbc_pct_revenue": "financial_sbc_pct_revenue",
        "r_and_d_pct_revenue": "financial_r_and_d_pct_revenue",
        "inventory_days": "financial_inventory_days",
        "fcf_yield": "financial_fcf_yield",
        "ev_gross_profit": "financial_ev_gross_profit",
        "ev_operating_income": "financial_ev_operating_income",
        "ret_3m": "market_ret_3m",
        "ret_12m_ex_1m": "market_ret_12m_ex_1m",
        "rel_strength_bench_3m": "market_rel_strength_bench_3m",
        "realized_vol_60d": "market_realized_vol_60d",
        "max_drawdown_12m": "market_max_drawdown_12m",
        "distance_from_52w_high": "market_distance_from_52w_high",
        "avg_dollar_volume_60d": "market_avg_dollar_volume_60d",
        "insider_net_value_90d": "positioning_insider_net_value_90d",
        "insider_cluster_buyers_90d": "positioning_insider_cluster_buyers_90d",
        "institutional_ownership_delta_pct": "positioning_institutional_ownership_delta_pct",
        "latest_short_interest_pct_float": "positioning_latest_short_interest_pct_float",
        "short_interest_change_3m": "positioning_short_interest_change_3m",
        "latest_days_to_cover": "positioning_latest_days_to_cover",
        "latest_borrow_fee_rate": "positioning_latest_borrow_fee_rate",
    }
)

HIGHER_IS_BETTER_SCORE_FIELDS = (
    "fcf_yield",
    "gross_margin",
    "operating_margin",
    "fcf_margin",
    "fcf_to_net_income",
    "net_cash_to_assets",
    "revenue_yoy_growth",
    "gross_profit_yoy_growth",
    "operating_income_yoy_growth",
    "free_cash_flow_yoy_growth",
    "revenue_acceleration",
    "ret_3m",
    "ret_12m_ex_1m",
    "rel_strength_bench_3m",
    "distance_from_52w_high",
    "avg_dollar_volume_60d",
    "insider_net_value_90d",
    "insider_cluster_buyers_90d",
    "institutional_ownership_delta_pct",
)

LOWER_IS_BETTER_SCORE_FIELDS = (
    "ev_gross_profit",
    "ev_operating_income",
    "inventory_days",
    "sbc_pct_revenue",
    "r_and_d_pct_revenue",
    "realized_vol_60d",
    "latest_short_interest_pct_float",
    "short_interest_change_3m",
    "latest_days_to_cover",
    "latest_borrow_fee_rate",
)

PILLAR_INPUT_FIELDS = MappingProxyType(
    {
        "valuation": ("fcf_yield", "ev_gross_profit", "ev_operating_income"),
        "quality": (
            "gross_margin",
            "operating_margin",
            "fcf_margin",
            "fcf_to_net_income",
            "net_cash_to_assets",
            "sbc_pct_revenue",
        ),
        "risk_control": (
            "realized_vol_60d",
            "max_drawdown_12m",
            "distance_from_52w_high",
        ),
        "positioning": (
            "insider_net_value_90d",
            "insider_cluster_buyers_90d",
            "institutional_ownership_delta_pct",
            "latest_short_interest_pct_float",
            "short_interest_change_3m",
            "latest_days_to_cover",
            "latest_borrow_fee_rate",
        ),
        "market_behavior": (
            "ret_3m",
            "ret_12m_ex_1m",
            "rel_strength_bench_3m",
            "realized_vol_60d",
            "max_drawdown_12m",
            "distance_from_52w_high",
        ),
        "growth": (
            "revenue_yoy_growth",
            "gross_profit_yoy_growth",
            "operating_income_yoy_growth",
            "free_cash_flow_yoy_growth",
            "revenue_acceleration",
        ),
    }
)

SPECIALIZED_PILLAR_FIELDS = (
    "defense_orders_growth",
    "defense_backlog_growth",
    "defense_backlog_coverage",
    "defense_book_to_bill",
)

SPECIALIZED_SOURCE_COLUMNS = frozenset(
    {
        "orders",
        "orders_usd",
        "orders_ttm",
        "orders_ttm_usd",
        "orders_yoy_growth",
        "book_to_bill",
        "funded_backlog",
        "funded_backlog_usd",
        "backlog_yoy_growth",
        "backlog_to_revenue",
        "reported_backlog",
        "reported_backlog_usd",
        "reported_backlog_yoy_growth",
        "reported_backlog_to_revenue",
        "remaining_performance_obligation",
        "remaining_performance_obligation_usd",
        "rpo_current",
        "rpo_current_usd",
        "rpo_yoy_growth",
        "rpo_to_revenue",
        "rpo_implied_orders",
        "rpo_implied_orders_usd",
        "rpo_implied_book_to_bill",
        "contract_load_proxy",
        "contract_load_proxy_usd",
        "contract_load_proxy_source",
        "contract_load_proxy_yoy_growth",
        "contract_load_proxy_to_revenue",
    }
)

# Sector-cycle is intentionally neutral until a separately sourced macro cycle
# feature passes PIT/OOS validation. It must never consume calibration weight.
STRUCTURALLY_DISABLED_PILLARS = frozenset({"sector_cycle_score"})

