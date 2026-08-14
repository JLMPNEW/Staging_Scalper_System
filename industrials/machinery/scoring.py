from __future__ import annotations

import csv
import hashlib
import json
import math
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

from industrials.core.financial_filing_lineage import (
    LINEAGE_FIELDS,
    validate_financial_lineage_rank_rows,
    write_financial_lineage_report,
)
from industrials.core.policy_loader import PolicyKey, PolicyRow, resolve_policy
from industrials.core.reports import write_csv_atomic, write_text_atomic
from industrials.machinery.financial_contract import AVAILABILITY_STATUSES, required_metric_names


MODEL_FAMILY = "machinery"
NEUTRAL_SCORE = 50.0

RAW_FEATURE_FIELDS = [
    "latest_adj_close",
    "latest_bar_date",
    "stale_days",
    "market_data_quality",
    "avg_dollar_volume_60d",
    "ret_1m",
    "ret_3m",
    "ret_6m",
    "ret_12m_ex_1m",
    "rel_strength_bench_3m",
    "realized_vol_60d",
    "max_drawdown_12m",
    "distance_from_52w_high",
    "above_ma_50d",
    "above_ma_200d",
    "market_cap",
    "accession_number",
    "form_type",
    "fiscal_period_end",
    "fiscal_year",
    "fiscal_period",
    "reporting_standard",
    "reporting_profile",
    "financial_frequency",
    "reported_currency",
    "fx_conversion_status",
    "canonical_quality",
    "data_quality_status",
    "review_reason",
    "revenue_ttm_usd",
    "revenue_stub_annualized_usd",
    "revenue_stub_period_days",
    "revenue_stub_quality",
    "gross_margin",
    "operating_margin",
    "fcf_margin",
    "net_cash_to_assets",
    "fcf_to_net_income",
    "revenue_yoy_growth",
    "gross_profit_yoy_growth",
    "operating_income_yoy_growth",
    "free_cash_flow_yoy_growth",
    "revenue_acceleration",
    "fcf_yield",
    "ev_gross_profit",
    "ev_operating_income",
    "inventory_days",
    "cash_conversion_cycle",
    "book_to_bill",
    "funded_backlog",
    "reported_backlog",
    "remaining_performance_obligation",
    "rpo_current",
    "contract_load_proxy",
    "contract_load_proxy_source",
    "operating_cash_flow_ttm_usd",
    "capex_usd",
    "capex_ttm_usd",
    "orders_ttm_usd",
    "funded_backlog_usd",
    "reported_backlog_usd",
    "remaining_performance_obligation_usd",
    "rpo_current_usd",
    "contract_load_proxy_usd",
    "orders_yoy_growth",
    "backlog_yoy_growth",
    "reported_backlog_yoy_growth",
    "contract_load_proxy_yoy_growth",
    "roic",
    "roic_not_meaningful_flag",
    "asset_turnover",
    "incremental_operating_margin",
    "inventory_growth",
    "inventory_sales_growth_spread",
    "cash_conversion_cycle_change",
    "net_debt_to_ebitda",
    "negative_ebitda_leverage_flag",
    "negative_profit_valuation_flag",
    "interest_coverage",
    "cash_burn_ttm_usd",
    "cash_runway_years",
    "gross_capital_raised_ttm_usd",
    "capital_raise_dependence",
    "diluted_shares_yoy_growth",
    "sbc_pct_revenue",
    "financial_confidence",
    "financial_fallback_status",
    "insider_net_value_90d",
    "insider_cluster_buyers_90d",
    "institutional_ownership_delta_pct",
    "short_interest_change_3m",
    "latest_days_to_cover",
    "latest_borrow_fee_rate",
    "positioning_quality",
    "backlog_to_revenue",
    "reported_backlog_to_revenue",
    "contract_load_proxy_to_revenue",
    "rpo_to_revenue",
    "rpo_yoy_growth",
    "rpo_implied_orders_usd",
    "rpo_implied_book_to_bill",
    "capex_to_revenue",
    "financial_metric_reported_count",
    "financial_metric_proxy_count",
    "financial_metric_unavailable_count",
    "financial_metric_classified_fraction",
]
RAW_TEXT_FIELDS = {
    "latest_bar_date",
    "market_data_quality",
    "financial_fallback_status",
    "accession_number",
    "form_type",
    "fiscal_period_end",
    "fiscal_year",
    "fiscal_period",
    "reporting_standard",
    "contract_load_proxy_source",
    "reporting_profile",
    "financial_frequency",
    "reported_currency",
    "fx_conversion_status",
    "canonical_quality",
    "data_quality_status",
    "review_reason",
    "revenue_stub_quality",
    "positioning_quality",
}

AVAILABILITY_STATUS_FIELDS = [
    f"{metric_name}_availability_status" for metric_name in required_metric_names()
]

COMPONENT_FIELDS = [
    "quality_score",
    "growth_score",
    "valuation_score",
    "risk_control_score",
    "market_behavior_score",
    "positioning_score",
    "industrial_cycle_score",
    "orders_backlog_score",
    "capex_cycle_score",
    "development_stage_risk_score",
]

SCORING_FEATURE_FIELDS = [
    "asof_date",
    "ticker",
    "company_name",
    "sector",
    "industry",
    "industry_aggregate",
    "subsector",
    "calibration_cohort",
    "calibration_cohort_name",
    "calibration_use",
    "development_stage",
    "membership_source_id",
    "membership_basis",
    "membership_start_date",
    "membership_end_date",
    "membership_status",
    "membership_confidence",
    "market_feature_asof_date",
    "market_feature_source_id",
    "financial_feature_asof_date",
    "financial_feature_source_id",
    "positioning_feature_asof_date",
    "positioning_feature_source_id",
    *RAW_FEATURE_FIELDS,
    "financial_metric_availability_asof_date",
    *AVAILABILITY_STATUS_FIELDS,
    "rank_ready_policy",
    "minimum_financial_confidence",
    "policy_valid_from",
    "policy_gate_status",
    "market_cap_source",
    "liquidity_capacity_reason",
    *COMPONENT_FIELDS,
    "score_input_available_count",
    "score_input_total_count",
    "score_confidence",
    "final_score",
    "rank_ready_flag",
    "rank_ready_reason",
    "model_status",
]

PORTFOLIO_REQUIRED_FIELDS = [
    "asof_date",
    "ticker",
    "company_name",
    "sector",
    "industry",
    "calibration_cohort",
    "final_score",
    "final_rank",
    "rank_ready_flag",
    "model_status",
    "score_confidence",
    "score_model_version",
    "model_version",
    "scoring_contract_version",
    "portfolio_candidate_gate",
    "portfolio_candidate_score",
    "portfolio_candidate_status",
    "portfolio_candidate_reason",
    "calibration_eligible_flag",
    "research_calibration_input_eligible_flag",
    "research_calibration_reason",
    "calibration_sample_role",
    "stage11_calibration_panel_source",
    "stage11_calibration_input_eligible_flag",
    "stage11_calibration_input_reason",
    "survivorship_corrected_panel_flag",
    "oos_score_valid_flag",
    "oos_score_asof_date",
    "oos_invalid_reason",
    "calibration_lock_date",
    *LINEAGE_FIELDS,
]

FINAL_RANK_FIELDS = [
    *SCORING_FEATURE_FIELDS,
    "final_rank",
    "score_model_version",
    "model_version",
    "scoring_contract_version",
    "portfolio_universe_eligible_flag",
    "portfolio_selection_policy",
    "portfolio_sleeve_selected_flag",
    "portfolio_sleeve_target_weight",
    "portfolio_candidate_gate",
    "portfolio_candidate_score",
    "portfolio_candidate_status",
    "portfolio_candidate_reason",
    "calibration_eligible_flag",
    "research_calibration_input_eligible_flag",
    "research_calibration_reason",
    "calibration_sample_role",
    "stage11_calibration_panel_source",
    "stage11_calibration_input_eligible_flag",
    "stage11_calibration_input_reason",
    "survivorship_corrected_panel_flag",
    "oos_score_valid_flag",
    "oos_score_asof_date",
    "oos_invalid_reason",
    "calibration_lock_date",
    *LINEAGE_FIELDS,
]

METRIC_DIRECTIONS: dict[str, int] = {
    "gross_margin": 1,
    "operating_margin": 1,
    "fcf_margin": 1,
    "net_cash_to_assets": 1,
    "fcf_to_net_income": 1,
    "revenue_yoy_growth": 1,
    "gross_profit_yoy_growth": 1,
    "operating_income_yoy_growth": 1,
    "free_cash_flow_yoy_growth": 1,
    "revenue_acceleration": 1,
    "fcf_yield": 1,
    "ev_gross_profit": -1,
    "ev_operating_income": -1,
    "realized_vol_60d": -1,
    "max_drawdown_12m": 1,
    "latest_borrow_fee_rate": -1,
    "latest_days_to_cover": -1,
    "ret_1m": 1,
    "ret_3m": 1,
    "ret_6m": 1,
    "ret_12m_ex_1m": 1,
    "rel_strength_bench_3m": 1,
    "distance_from_52w_high": 1,
    "above_ma_50d": 1,
    "above_ma_200d": 1,
    "insider_net_value_90d": 1,
    "insider_cluster_buyers_90d": 1,
    "institutional_ownership_delta_pct": 1,
    "short_interest_change_3m": -1,
    "inventory_days": -1,
    "cash_conversion_cycle_change": -1,
    "inventory_sales_growth_spread": -1,
    "net_debt_to_ebitda": -1,
    "negative_ebitda_leverage_flag": -1,
    "interest_coverage": 1,
    "roic": 1,
    "asset_turnover": 1,
    "incremental_operating_margin": 1,
    "orders_yoy_growth": 1,
    "backlog_yoy_growth": 1,
    "diluted_shares_yoy_growth": -1,
    "cash_runway_years": 1,
    "capital_raise_dependence": -1,
    "sbc_pct_revenue": -1,
    "book_to_bill": 1,
    "backlog_to_revenue": 1,
    "rpo_to_revenue": 1,
    "capex_to_revenue": 1,
}

COMPONENT_METRICS: dict[str, list[str]] = {
    "quality_score": [
        "gross_margin",
        "operating_margin",
        "fcf_margin",
        "net_cash_to_assets",
        "fcf_to_net_income",
        "roic",
        "asset_turnover",
        "interest_coverage",
    ],
    "growth_score": [
        "revenue_yoy_growth",
        "gross_profit_yoy_growth",
        "operating_income_yoy_growth",
        "free_cash_flow_yoy_growth",
        "revenue_acceleration",
        "incremental_operating_margin",
    ],
    "valuation_score": ["fcf_yield", "ev_gross_profit", "ev_operating_income"],
    "risk_control_score": [
        "realized_vol_60d",
        "max_drawdown_12m",
        "net_cash_to_assets",
        "latest_borrow_fee_rate",
        "latest_days_to_cover",
        "net_debt_to_ebitda",
        "negative_ebitda_leverage_flag",
        "cash_conversion_cycle_change",
    ],
    "market_behavior_score": [
        "ret_1m",
        "ret_6m",
        "ret_12m_ex_1m",
        "rel_strength_bench_3m",
        "distance_from_52w_high",
        "above_ma_50d",
        "above_ma_200d",
    ],
    "positioning_score": [
        "insider_net_value_90d",
        "insider_cluster_buyers_90d",
        "institutional_ownership_delta_pct",
        "short_interest_change_3m",
    ],
    "industrial_cycle_score": [
        "ret_3m",
        "rel_strength_bench_3m",
        "revenue_acceleration",
        "inventory_days",
        "inventory_sales_growth_spread",
    ],
    "orders_backlog_score": [
        "book_to_bill",
        "orders_yoy_growth",
        "backlog_to_revenue",
        "backlog_yoy_growth",
        "rpo_to_revenue",
    ],
    "capex_cycle_score": ["capex_to_revenue", "revenue_yoy_growth", "ret_6m"],
}
DEVELOPMENT_SIGNAL_METRICS = {
    "cash_runway_years",
    "capital_raise_dependence",
    "diluted_shares_yoy_growth",
    "sbc_pct_revenue",
}


def parse_asof(raw: str) -> str:
    return datetime.strptime(str(raw).strip(), "%Y-%m-%d").date().isoformat()


def _float(value: object) -> float | None:
    try:
        number = float(str(value).strip())
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _fmt(value: object, digits: int = 8) -> str:
    number = _float(value)
    if number is None:
        return ""
    return f"{number:.{digits}f}".rstrip("0").rstrip(".")


def _ratio(numerator: object, denominator: object, *, absolute_numerator: bool = False) -> float | None:
    top = _float(numerator)
    bottom = _float(denominator)
    if top is None or bottom is None or abs(bottom) < 1e-12:
        return None
    return (abs(top) if absolute_numerator else top) / abs(bottom)


def _latest_rows(
    conn: sqlite3.Connection,
    table: str,
    *,
    asof: str,
    source_priority: tuple[str, ...] = (),
) -> dict[str, dict[str, Any]]:
    priority_sql = "source_id ASC"
    priority_params: tuple[str, ...] = ()
    if source_priority:
        cases = " ".join(f"WHEN ? THEN {index}" for index, _ in enumerate(source_priority))
        priority_sql = f"CASE source_id {cases} ELSE {len(source_priority)} END, source_id ASC"
        priority_params = source_priority
    rows = conn.execute(
        f"""
        SELECT *
        FROM (
            SELECT source_rows.*,
                   ROW_NUMBER() OVER (
                       PARTITION BY ticker
                       ORDER BY asof_date DESC, {priority_sql}
                   ) AS source_row_number
            FROM {table} source_rows
            WHERE model_family = ? AND asof_date <= ?
        )
        WHERE source_row_number = 1
        """,
        (*priority_params, MODEL_FAMILY, asof),
    ).fetchall()
    return {
        str(row["ticker"]): {key: row[key] for key in row.keys() if key != "source_row_number"}
        for row in rows
    }


def _latest_metric_availability(
    conn: sqlite3.Connection,
    *,
    asof: str,
) -> dict[str, dict[str, str]]:
    rows = conn.execute(
        """
        SELECT ticker, asof_date, metric_name, availability_status
        FROM feature_financial_metric_availability
        WHERE model_family = ? AND asof_date = ?
        """,
        (MODEL_FAMILY, asof),
    ).fetchall()
    output: dict[str, dict[str, str]] = {}
    for row in rows:
        ticker = str(row["ticker"])
        metric_name = str(row["metric_name"])
        ticker_values = output.setdefault(ticker, {})
        ticker_values[f"{metric_name}_availability_status"] = str(row["availability_status"])
        ticker_values["financial_metric_availability_asof_date"] = str(row["asof_date"])
    required_count = len(AVAILABILITY_STATUS_FIELDS)
    for ticker_values in output.values():
        statuses = [
            ticker_values[field]
            for field in AVAILABILITY_STATUS_FIELDS
            if field in ticker_values
        ]
        reported = sum(status == "REPORTED" for status in statuses)
        proxy = sum(status == "PROXY" for status in statuses)
        ticker_values["financial_metric_reported_count"] = str(reported)
        ticker_values["financial_metric_proxy_count"] = str(proxy)
        ticker_values["financial_metric_unavailable_count"] = str(
            len(statuses) - reported - proxy
        )
        ticker_values["financial_metric_classified_fraction"] = _fmt(
            len(statuses) / required_count if required_count else 1.0
        )
    return output


def _membership_rows(conn: sqlite3.Connection, *, asof: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT *
        FROM (
            SELECT
                m.ticker,
                m.membership_source_id,
                m.membership_basis,
                m.start_date AS membership_start_date,
                COALESCE(m.end_date, '') AS membership_end_date,
                m.membership_status,
                m.confidence AS membership_confidence,
                c.company_name,
                c.sector,
                c.industry,
                c.subsector,
                t.calibration_cohort_id,
                t.calibration_cohort,
                t.calibration_use,
                t.development_stage,
                ROW_NUMBER() OVER (
                    PARTITION BY m.ticker
                    ORDER BY
                        CASE WHEN m.membership_basis = 'survivorship_corrected_pit_contract' THEN 0 ELSE 1 END,
                        m.confidence DESC,
                        m.start_date DESC
                ) AS membership_row_number
            FROM dim_universe_membership m
            JOIN dim_company c ON c.company_id = m.company_id
            JOIN dim_industrials_taxonomy t
              ON t.ticker = m.ticker AND t.model_family = m.model_family
            WHERE m.model_family = ?
              AND m.start_date <= ?
              AND (m.end_date IS NULL OR m.end_date = '' OR m.end_date >= ?)
        )
        WHERE membership_row_number = 1
        ORDER BY ticker
        """,
        (MODEL_FAMILY, asof, asof),
    ).fetchall()
    return [
        {key: row[key] for key in row.keys() if key != "membership_row_number"}
        for row in rows
    ]


def _percentile(value: float, population: list[float], direction: int) -> float:
    if len(population) <= 1:
        return NEUTRAL_SCORE
    ordered = [direction * item for item in population]
    target = direction * value
    lower = sum(item < target for item in ordered)
    equal = sum(item == target for item in ordered)
    return 100.0 * (lower + 0.5 * equal) / len(ordered)


def _score_metrics(rows: list[dict[str, Any]]) -> dict[str, dict[str, float]]:
    results = {str(row["ticker"]): {} for row in rows}
    for metric, direction in METRIC_DIRECTIONS.items():
        global_population = [value for row in rows if (value := _float(row.get(metric))) is not None]
        by_cohort: dict[str, list[float]] = {}
        for row in rows:
            value = _float(row.get(metric))
            if value is None:
                continue
            by_cohort.setdefault(str(row.get("calibration_cohort") or ""), []).append(value)
        for row in rows:
            value = _float(row.get(metric))
            if value is None:
                continue
            cohort_population = by_cohort.get(str(row.get("calibration_cohort") or ""), [])
            population = cohort_population if len(cohort_population) >= 5 else global_population
            results[str(row["ticker"])][metric] = _percentile(value, population, direction)
    return results


def _is_development_row(row: dict[str, Any]) -> bool:
    cohort = str(row.get("calibration_cohort") or "")
    stage = str(row.get("development_stage") or "").lower()
    return cohort == "development_stage_emerging_machinery" or stage == "development_stage"


def _apply_negative_profit_valuation_cap(
    score: float,
    row: Mapping[str, Any],
    *,
    cap: float,
) -> float:
    if _float(row.get("negative_profit_valuation_flag")) == 1.0:
        return min(score, cap)
    return score


def _development_score(row: dict[str, Any]) -> float:
    stage = str(row.get("development_stage") or "").lower()
    if _is_development_row(row):
        observations: list[float] = []
        runway = _float(row.get("cash_runway_years"))
        if runway is not None:
            observations.append(80.0 if runway >= 3.0 else 65.0 if runway >= 2.0 else 45.0 if runway >= 1.0 else 20.0)
        elif _float(row.get("cash_burn_ttm_usd")) == 0.0:
            observations.append(80.0)
        capital_raise_dependence = _float(row.get("capital_raise_dependence"))
        partial_capital_raise_coverage = (
            "capital_raise_proceeds_partial_component_coverage"
            in {
                token.strip()
                for token in str(row.get("canonical_quality") or "").split(";")
            }
        )
        if capital_raise_dependence is not None:
            if partial_capital_raise_coverage:
                if capital_raise_dependence > 1.5:
                    observations.append(15.0)
                elif capital_raise_dependence > 0.75:
                    observations.append(35.0)
            else:
                observations.append(
                    75.0
                    if capital_raise_dependence <= 0.25
                    else 55.0
                    if capital_raise_dependence <= 0.75
                    else 35.0
                    if capital_raise_dependence <= 1.5
                    else 15.0
                )
        dilution = _float(row.get("diluted_shares_yoy_growth"))
        if dilution is not None:
            observations.append(75.0 if dilution <= 0.05 else 55.0 if dilution <= 0.15 else 30.0 if dilution <= 0.30 else 10.0)
        sbc_ratio = _float(row.get("sbc_pct_revenue"))
        if sbc_ratio is not None:
            observations.append(75.0 if sbc_ratio <= 0.05 else 50.0 if sbc_ratio <= 0.15 else 25.0)
        return sum(observations) / len(observations) if observations else 35.0
    if stage == "historical_delisted":
        # Point-in-time guard: the historical_delisted taxonomy label encodes
        # today's knowledge. At an asof before the membership end date the exit
        # was unknowable, so haircutting there leaks the future outcome into
        # backfilled/survivorship panels. Only haircut once the delisting has
        # actually occurred as of the scoring date.
        end_date = str(row.get("membership_end_date") or "")
        asof_date = str(row.get("asof_date") or "")
        if end_date and asof_date and end_date <= asof_date:
            return 70.0
        return 100.0
    return 100.0


def _validate_weights(weights: dict[str, Any]) -> dict[str, float]:
    expected = set(COMPONENT_FIELDS) - {"development_stage_risk_score"}
    if set(weights) != expected:
        raise ValueError(f"machinery scoring component_weights must equal {sorted(expected)}; found={sorted(weights)}")
    parsed = {field: float(weights[field]) for field in expected}
    if any(value < 0.0 for value in parsed.values()):
        raise ValueError("machinery scoring component weights must be non-negative")
    if not math.isclose(sum(parsed.values()), 1.0, rel_tol=0.0, abs_tol=1e-9):
        raise ValueError(f"machinery scoring component weights must sum to 1.0; found={sum(parsed.values())}")
    return parsed


def build_scoring_feature_rows(
    conn: sqlite3.Connection,
    *,
    asof: str,
    eligibility_policies: dict[PolicyKey, PolicyRow],
    market_source_priority: tuple[str, ...] = (),
    financial_source_priority: tuple[str, ...] = (),
    positioning_source_priority: tuple[str, ...] = (),
    component_weights: dict[str, Any],
    min_score_confidence: float,
    max_staleness_days: int,
    min_avg_dollar_volume: float,
    negative_profit_valuation_score_cap: float = 25.0,
) -> list[dict[str, str]]:
    asof = parse_asof(asof)
    memberships = _membership_rows(conn, asof=asof)
    if not memberships:
        raise ValueError(f"No machinery membership rows are effective at {asof}")
    market = _latest_rows(
        conn,
        "feature_market_technical",
        asof=asof,
        source_priority=market_source_priority,
    )
    financial = _latest_rows(
        conn,
        "feature_financial_statement",
        asof=asof,
        source_priority=financial_source_priority,
    )
    availability = _latest_metric_availability(conn, asof=asof)
    positioning = _latest_rows(
        conn,
        "feature_positioning",
        asof=asof,
        source_priority=positioning_source_priority,
    )
    weights = _validate_weights(component_weights)
    if not 0.0 <= negative_profit_valuation_score_cap <= NEUTRAL_SCORE:
        raise ValueError(
            "negative_profit_valuation_score_cap must be between 0 and 50"
        )
    combined: list[dict[str, Any]] = []
    for membership in memberships:
        ticker = str(membership["ticker"])
        market_row = market.get(ticker, {})
        financial_row = financial.get(ticker, {})
        availability_row = availability.get(ticker, {})
        positioning_row = positioning.get(ticker, {})
        row: dict[str, Any] = {
            "asof_date": asof,
            "ticker": ticker,
            "company_name": membership["company_name"],
            "sector": "Industrials",
            "industry": membership["industry"] or "Machinery",
            "industry_aggregate": "Machinery",
            "subsector": "Machinery",
            "calibration_cohort": membership["calibration_cohort_id"],
            "calibration_cohort_name": membership["calibration_cohort"],
            "calibration_use": membership["calibration_use"],
            # Normalize the legacy "development" alias so the dev-row check and
            # resolve_policy (whose CSV only defines development_stage) agree.
            "development_stage": (
                "development_stage"
                if str(membership["development_stage"] or "").strip().lower() == "development"
                else membership["development_stage"]
            ),
            "membership_source_id": membership["membership_source_id"],
            "membership_basis": membership["membership_basis"],
            "membership_start_date": membership["membership_start_date"],
            "membership_end_date": membership["membership_end_date"],
            "membership_status": membership["membership_status"],
            "membership_confidence": membership["membership_confidence"],
            "market_feature_asof_date": market_row.get("asof_date", ""),
            "market_feature_source_id": market_row.get("source_id", ""),
            "financial_feature_asof_date": financial_row.get("asof_date", ""),
            "financial_feature_source_id": financial_row.get("source_id", ""),
            "positioning_feature_asof_date": positioning_row.get("asof_date", ""),
            "positioning_feature_source_id": positioning_row.get("source_id", ""),
            "financial_metric_availability_asof_date": availability_row.get(
                "financial_metric_availability_asof_date", ""
            ),
        }
        for field in AVAILABILITY_STATUS_FIELDS:
            row[field] = availability_row.get(field, "")
        for field in RAW_FEATURE_FIELDS:
            if field in market_row:
                row[field] = market_row[field]
            elif field in financial_row:
                row[field] = financial_row[field]
            elif field in positioning_row:
                row[field] = positioning_row[field]
            else:
                row[field] = ""
        for field in (
            "financial_metric_reported_count",
            "financial_metric_proxy_count",
            "financial_metric_unavailable_count",
            "financial_metric_classified_fraction",
        ):
            if field in availability_row:
                row[field] = availability_row[field]
        # Stage 4 owns backlog period/currency alignment. Preserve its null on a
        # failed alignment rather than recomputing from mixed local/USD values.
        row["backlog_to_revenue"] = financial_row.get("backlog_to_revenue", "")
        row["rpo_to_revenue"] = financial_row.get("rpo_to_revenue", "")
        # TTM/TTM only: single-period capex over TTM revenue sawtooths with
        # filing frequency (10-K vs 10-Q YTD), biasing cross-sectional ranks.
        row["capex_to_revenue"] = _ratio(
            financial_row.get("capex_ttm_usd"),
            financial_row.get("revenue_ttm_usd"),
            absolute_numerator=True,
        )
        latest_bar_date = str(row.get("latest_bar_date") or "").strip()
        if latest_bar_date:
            try:
                row["stale_days"] = (
                    datetime.strptime(asof, "%Y-%m-%d").date()
                    - datetime.strptime(latest_bar_date, "%Y-%m-%d").date()
                ).days
            except ValueError:
                row["stale_days"] = ""
        row["market_cap_source"] = str(financial_row.get("source_id") or "") if _float(row.get("market_cap")) is not None else ""
        combined.append(row)

    metric_scores = _score_metrics(combined)
    scoring_signal_metrics = {metric for metrics in COMPONENT_METRICS.values() for metric in metrics}
    output: list[dict[str, str]] = []
    for row in combined:
        ticker = str(row["ticker"])
        reporting_profile = str(row.get("reporting_profile") or "NO_FINANCIALS_REVIEW").strip()
        development_stage = str(row.get("development_stage") or "operating").strip()
        policy = resolve_policy(eligibility_policies, reporting_profile, development_stage)
        if policy is None:
            raise ValueError(
                "Missing machinery scoring eligibility policy for "
                f"ticker={ticker} reporting_profile={reporting_profile} "
                f"development_stage={development_stage} asof={asof}"
            )
        rank_ready_policy = str(policy.get("rank_ready_policy") or "").strip()
        policy_minimum_confidence = _float(policy.get("minimum_financial_confidence"))
        if policy_minimum_confidence is None or not 0.0 <= policy_minimum_confidence <= 1.0:
            raise ValueError(
                f"Invalid minimum_financial_confidence for {reporting_profile}:{development_stage}"
            )
        scores = metric_scores[ticker]
        for component, metrics in COMPONENT_METRICS.items():
            available = [scores[metric] for metric in metrics if metric in scores]
            row[component] = sum(available) / len(available) if available else NEUTRAL_SCORE
        row["valuation_score"] = _apply_negative_profit_valuation_cap(
            float(row["valuation_score"]),
            row,
            cap=negative_profit_valuation_score_cap,
        )
        row["development_stage_risk_score"] = _development_score(row)
        # Development-stage risk is a transparent risk-control modifier rather than a separate weight.
        row["risk_control_score"] = 0.8 * float(row["risk_control_score"]) + 0.2 * float(
            row["development_stage_risk_score"]
        )
        row_signal_metrics = (
            scoring_signal_metrics | DEVELOPMENT_SIGNAL_METRICS
            if _is_development_row(row)
            else scoring_signal_metrics
        )
        total_signal_count = len(row_signal_metrics)
        available_count = sum(metric in scores for metric in row_signal_metrics)
        availability = available_count / total_signal_count if total_signal_count else 0.0
        financial_confidence = _float(row.get("financial_confidence"))
        confidence = availability if financial_confidence is None else 0.75 * availability + 0.25 * financial_confidence
        confidence = max(0.0, min(1.0, confidence))
        final_score = sum(float(row[component]) * weight for component, weight in weights.items())
        stale_days = _float(row.get("stale_days"))
        adv = _float(row.get("avg_dollar_volume_60d"))
        reasons: list[str] = []
        if not str(row.get("market_feature_asof_date") or ""):
            reasons.append("missing_market_features")
        if stale_days is None or stale_days > max_staleness_days:
            reasons.append("stale_market_features")
        # A negative stale_days (future market bar) is treated as a fatal
        # per-row contract error by validate_scoring_feature_rows — corrupt
        # upstream data crashes the publish rather than being quarantined, so
        # no row-level gate reason exists for it.
        if adv is None or adv < min_avg_dollar_volume:
            reasons.append("insufficient_liquidity")
        if confidence < min_score_confidence:
            reasons.append("low_score_confidence")
        financial_data_quality = str(row.get("data_quality_status") or "").strip()
        if financial_data_quality != "complete":
            reasons.append("financial_data_quality_not_complete")
        if financial_confidence is None:
            reasons.append("missing_financial_confidence")
        elif financial_confidence < policy_minimum_confidence:
            reasons.append("financial_confidence_below_policy_minimum")
        if not rank_ready_policy.startswith("eligible"):
            reasons.append("financial_policy_not_rank_ready")
        policy_gate_pass = (
            rank_ready_policy.startswith("eligible")
            and financial_data_quality == "complete"
            and financial_confidence is not None
            and financial_confidence >= policy_minimum_confidence
        )
        rank_ready = not reasons
        row["rank_ready_policy"] = rank_ready_policy
        row["minimum_financial_confidence"] = policy_minimum_confidence
        row["policy_valid_from"] = str(policy.get("valid_from") or "")
        row["policy_gate_status"] = "pass" if policy_gate_pass else "blocked"
        row["score_input_available_count"] = available_count
        row["score_input_total_count"] = total_signal_count
        row["score_confidence"] = confidence
        row["final_score"] = max(0.0, min(100.0, final_score))
        row["rank_ready_flag"] = int(rank_ready)
        row["rank_ready_reason"] = "ok" if rank_ready else ";".join(reasons)
        row["model_status"] = "complete" if rank_ready else "incomplete"
        row["liquidity_capacity_reason"] = (
            "ok"
            if adv is not None and adv >= min_avg_dollar_volume
            else "missing_avg_dollar_volume_60d"
            if adv is None
            else f"avg_dollar_volume_60d_below_{min_avg_dollar_volume:.0f}"
        )
        formatted: dict[str, str] = {}
        for field in SCORING_FEATURE_FIELDS:
            value = row.get(field, "")
            if (field in RAW_FEATURE_FIELDS and field not in RAW_TEXT_FIELDS) or field in COMPONENT_FIELDS or field in {
                "membership_confidence",
                "minimum_financial_confidence",
                "score_confidence",
                "final_score",
            }:
                formatted[field] = _fmt(value)
            else:
                formatted[field] = "" if value is None else str(value)
        output.append(formatted)
    return sorted(output, key=lambda item: item["ticker"])


def finalize_rank_rows(
    feature_rows: list[dict[str, str]],
    *,
    score_model_version: str,
    model_version: str,
    scoring_contract_version: str,
) -> list[dict[str, str]]:
    if not feature_rows:
        raise ValueError("Cannot finalize an empty machinery scoring feature contract")
    ordered = sorted(
        feature_rows,
        key=lambda row: (
            -int(str(row.get("rank_ready_flag") or "0")),
            -float(str(row.get("final_score") or "0")),
            row.get("ticker", ""),
        ),
    )
    final: list[dict[str, str]] = []
    for rank, source in enumerate(ordered, start=1):
        row = {field: str(source.get(field) or "") for field in SCORING_FEATURE_FIELDS}
        rank_ready = str(source.get("rank_ready_flag") or "0") == "1"
        row.update(
            {
                "final_rank": str(rank),
                "score_model_version": score_model_version,
                "model_version": model_version,
                "scoring_contract_version": scoring_contract_version,
                "portfolio_candidate_gate": "0",
                "portfolio_candidate_score": str(source.get("final_score") or ""),
                "portfolio_candidate_status": "shadow_only",
                "portfolio_candidate_reason": "shadow_only_oos_calibration_not_available",
                "calibration_eligible_flag": "1" if rank_ready else "0",
                "research_calibration_input_eligible_flag": "0",
                "research_calibration_reason": "dashboard_snapshot_not_survivorship_corrected_use_sidecar",
                "calibration_sample_role": "excluded",
                "stage11_calibration_panel_source": "dashboard_rank_snapshot_current_universe_replay",
                "stage11_calibration_input_eligible_flag": "0",
                "stage11_calibration_input_reason": "dashboard_snapshot_not_survivorship_corrected_use_sidecar",
                "survivorship_corrected_panel_flag": "0",
                "oos_score_valid_flag": "0",
                "oos_score_asof_date": "",
                "oos_invalid_reason": "shadow_pre_oos_calibration",
                "calibration_lock_date": "",
                "financial_lineage_checked_asof_date": str(source.get("asof_date") or ""),
                "financial_lineage_status": "REVIEW_REQUIRED",
                "financial_lineage_gate": "0",
                "incorporated_financial_core_metric_count": "0",
                "financial_lineage_reason": "financial_filing_lineage_not_reconciled",
            }
        )
        final.append({field: row.get(field, "") for field in FINAL_RANK_FIELDS})
    return final


def survivorship_sidecar(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    sidecar: list[dict[str, str]] = []
    for source in rows:
        row = dict(source)
        eligible = str(row.get("rank_ready_flag") or "0") == "1"
        row.update(
            {
                "portfolio_universe_eligible_flag": "",
                "portfolio_selection_policy": "",
                "portfolio_sleeve_selected_flag": "",
                "portfolio_sleeve_target_weight": "",
                "portfolio_candidate_gate": "0",
                "portfolio_candidate_status": "shadow_only",
                "portfolio_candidate_reason": "shadow_only_oos_calibration_not_available",
                "oos_score_valid_flag": "0",
                "oos_score_asof_date": "",
                "oos_invalid_reason": "shadow_pre_oos_calibration",
                "research_calibration_input_eligible_flag": "1" if eligible else "0",
                "research_calibration_reason": "ok" if eligible else str(row.get("rank_ready_reason") or "not_rank_ready"),
                "calibration_sample_role": "pre_lock_research" if eligible else "excluded",
                "stage11_calibration_panel_source": "survivorship_corrected_pit_membership_score_recompute",
                "stage11_calibration_input_eligible_flag": "1" if eligible else "0",
                "stage11_calibration_input_reason": "ok" if eligible else str(row.get("rank_ready_reason") or "not_rank_ready"),
                "survivorship_corrected_panel_flag": "1",
            }
        )
        sidecar.append({field: row.get(field, "") for field in FINAL_RANK_FIELDS})
    return sidecar


def read_rows(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None:
            raise ValueError(f"CSV has no header: {path}")
        return [{str(key): str(value or "") for key, value in row.items() if key is not None} for row in reader]


def validate_rank_rows(
    rows: list[dict[str, str]],
    *,
    asof: str,
    allow_production: bool = False,
) -> list[str]:
    errors: list[str] = []
    if not rows:
        return ["rank table is empty"]
    errors.extend(validate_scoring_feature_rows(rows, asof=asof))
    missing = sorted(set(PORTFOLIO_REQUIRED_FIELDS).difference(rows[0]))
    if missing:
        errors.append(f"missing portfolio/calibration columns={missing}")
    if "industry_aggregate" not in rows[0] and "subsector" not in rows[0]:
        errors.append("missing industry_aggregate/subsector")
    tickers = [str(row.get("ticker") or "").strip().upper() for row in rows]
    if not all(tickers):
        errors.append("blank ticker")
    if len(set(tickers)) != len(tickers):
        errors.append("duplicate ticker")
    if {str(row.get("asof_date") or "") for row in rows} != {asof}:
        errors.append("rank table must contain exactly the requested asof_date")
    ranks: list[int] = []
    for row in rows:
        ticker = str(row.get("ticker") or "<blank>")
        score = _float(row.get("final_score"))
        confidence = _float(row.get("score_confidence"))
        if score is None or not 0.0 <= score <= 100.0:
            errors.append(f"{ticker}: invalid final_score={row.get('final_score')!r}")
        if confidence is None or not 0.0 <= confidence <= 1.0:
            errors.append(f"{ticker}: invalid score_confidence={row.get('score_confidence')!r}")
        candidate = str(row.get("portfolio_candidate_gate") or "")
        oos_valid = str(row.get("oos_score_valid_flag") or "")
        selected = str(row.get("portfolio_sleeve_selected_flag") or "")
        universe_eligible = str(
            row.get("portfolio_universe_eligible_flag") or ""
        )
        if not allow_production:
            if candidate != "0":
                errors.append(
                    f"{ticker}: shadow publisher must set "
                    "portfolio_candidate_gate=0"
                )
            if oos_valid != "0":
                errors.append(
                    f"{ticker}: shadow publisher must set "
                    "oos_score_valid_flag=0"
                )
        else:
            if (
                candidate not in {"0", "1"}
                or oos_valid not in {"0", "1"}
                or selected not in {"0", "1"}
                or universe_eligible not in {"0", "1"}
            ):
                errors.append(f"{ticker}: invalid production eligibility flags")
            if candidate != selected:
                errors.append(
                    f"{ticker}: production candidate/selection mismatch"
                )
            if selected == "1" and universe_eligible != "1":
                errors.append(
                    f"{ticker}: selected production row is not universe eligible"
                )
            if universe_eligible == "1" and oos_valid != "1":
                errors.append(
                    f"{ticker}: production universe eligibility requires OOS validity"
                )
            if oos_valid == "1" and universe_eligible == "0":
                if (
                    row.get("portfolio_candidate_reason")
                    != "development_stage_core_sleeve_excluded"
                    or row.get("research_calibration_input_eligible_flag")
                    != "1"
                    or row.get("calibration_sample_role") != "strict_oos"
                ):
                    errors.append(
                        f"{ticker}: OOS-valid core-sleeve exclusion lacks "
                        "development-stage research provenance"
                    )
        try:
            ranks.append(int(str(row.get("final_rank") or "")))
        except ValueError:
            errors.append(f"{ticker}: invalid final_rank={row.get('final_rank')!r}")
    if sorted(ranks) != list(range(1, len(rows) + 1)):
        errors.append("final_rank must be contiguous from 1 through row count")
    errors.extend(validate_financial_lineage_rank_rows(rows))
    return errors


def validate_metric_availability_contract(
    rows: list[dict[str, str]],
    *,
    asof: str,
) -> list[str]:
    errors: list[str] = []
    for row in rows:
        ticker = str(row.get("ticker") or "<blank>")
        availability_asof = str(row.get("financial_metric_availability_asof_date") or "")
        if availability_asof != asof:
            errors.append(
                f"{ticker}: financial metric availability asof={availability_asof!r} expected={asof}"
            )
        classified_fraction = _float(row.get("financial_metric_classified_fraction"))
        if classified_fraction is None or not math.isclose(
            classified_fraction,
            1.0,
            rel_tol=0.0,
            abs_tol=1e-9,
        ):
            errors.append(
                f"{ticker}: financial_metric_classified_fraction={row.get('financial_metric_classified_fraction')!r}"
            )
        for field in AVAILABILITY_STATUS_FIELDS:
            status = str(row.get(field) or "")
            if status not in AVAILABILITY_STATUSES:
                errors.append(f"{ticker}: invalid {field}={status!r}")
        statuses = [str(row.get(field) or "") for field in AVAILABILITY_STATUS_FIELDS]
        expected_counts = {
            "financial_metric_reported_count": sum(status == "REPORTED" for status in statuses),
            "financial_metric_proxy_count": sum(status == "PROXY" for status in statuses),
            "financial_metric_unavailable_count": sum(
                status not in {"REPORTED", "PROXY"} for status in statuses
            ),
        }
        for field, expected in expected_counts.items():
            try:
                actual = int(str(row.get(field) or ""))
            except ValueError:
                errors.append(f"{ticker}: invalid {field}={row.get(field)!r}")
                continue
            if actual != expected:
                errors.append(f"{ticker}: {field}={actual} expected={expected}")
    return errors


def validate_scoring_feature_rows(rows: list[dict[str, str]], *, asof: str) -> list[str]:
    errors: list[str] = []
    if not rows:
        return ["scoring feature contract is empty"]
    missing = sorted(set(SCORING_FEATURE_FIELDS).difference(rows[0]))
    if missing:
        errors.append(f"missing scoring feature columns={missing}")
    tickers = [str(row.get("ticker") or "").strip().upper() for row in rows]
    if not all(tickers):
        errors.append("blank ticker")
    duplicates = sorted({ticker for ticker in tickers if tickers.count(ticker) > 1})
    if duplicates:
        errors.append(f"duplicate tickers={duplicates[:20]}")
    if {str(row.get("asof_date") or "") for row in rows} != {asof}:
        errors.append("scoring feature contract must contain exactly the requested asof_date")

    date_fields = [
        "latest_bar_date",
        "market_feature_asof_date",
        "financial_feature_asof_date",
        "positioning_feature_asof_date",
    ]
    for row in rows:
        ticker = str(row.get("ticker") or "<blank>")
        score = _float(row.get("final_score"))
        confidence = _float(row.get("score_confidence"))
        if score is None or not 0.0 <= score <= 100.0:
            errors.append(f"{ticker}: invalid final_score={row.get('final_score')!r}")
        if confidence is None or not 0.0 <= confidence <= 1.0:
            errors.append(f"{ticker}: invalid score_confidence={row.get('score_confidence')!r}")
        for component in COMPONENT_FIELDS:
            value = _float(row.get(component))
            if value is None or not 0.0 <= value <= 100.0:
                errors.append(f"{ticker}: invalid {component}={row.get(component)!r}")
        try:
            available = int(str(row.get("score_input_available_count") or ""))
            total = int(str(row.get("score_input_total_count") or ""))
            if available < 0 or total <= 0 or available > total:
                errors.append(f"{ticker}: invalid score input counts available={available} total={total}")
        except ValueError:
            errors.append(
                f"{ticker}: invalid score input counts "
                f"available={row.get('score_input_available_count')!r} total={row.get('score_input_total_count')!r}"
            )
        rank_ready = str(row.get("rank_ready_flag") or "")
        reason = str(row.get("rank_ready_reason") or "").strip()
        model_status = str(row.get("model_status") or "").strip()
        if rank_ready not in {"0", "1"}:
            errors.append(f"{ticker}: rank_ready_flag must be 0 or 1")
        elif rank_ready == "1" and (reason != "ok" or model_status != "complete"):
            errors.append(f"{ticker}: rank-ready row must have reason=ok and model_status=complete")
        elif rank_ready == "0" and (not reason or model_status != "incomplete"):
            errors.append(f"{ticker}: non-rank-ready row must have a reason and model_status=incomplete")
        policy_gate_status = str(row.get("policy_gate_status") or "")
        rank_ready_policy = str(row.get("rank_ready_policy") or "")
        if policy_gate_status not in {"pass", "blocked"}:
            errors.append(f"{ticker}: invalid policy_gate_status={policy_gate_status!r}")
        else:
            policy_minimum = _float(row.get("minimum_financial_confidence"))
            financial_confidence = _float(row.get("financial_confidence"))
            expected_policy_pass = (
                rank_ready_policy.startswith("eligible")
                and str(row.get("data_quality_status") or "") == "complete"
                and policy_minimum is not None
                and financial_confidence is not None
                and financial_confidence >= policy_minimum
            )
            if (policy_gate_status == "pass") != expected_policy_pass:
                errors.append(
                    f"{ticker}: policy_gate_status={policy_gate_status!r} is inconsistent with policy inputs"
                )
            if rank_ready == "1" and policy_gate_status != "pass":
                errors.append(f"{ticker}: rank-ready row must pass the financial policy gate")
        for field in date_fields:
            value = str(row.get(field) or "").strip()
            if value and value > asof:
                errors.append(f"{ticker}: {field}={value} is after asof={asof}")
        start_date = str(row.get("membership_start_date") or "").strip()
        end_date = str(row.get("membership_end_date") or "").strip()
        if not start_date or start_date > asof or (end_date and end_date < asof):
            errors.append(
                f"{ticker}: membership interval start={start_date!r} end={end_date!r} does not cover {asof}"
            )
    errors.extend(validate_metric_availability_contract(rows, asof=asof))
    return errors


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    write_text_atomic(path, json.dumps(payload, indent=2, sort_keys=True) + "\n")


def publish_dashboard(
    *,
    output_dir: Path,
    rows: list[dict[str, str]],
    asof: str,
    allow_overwrite: bool,
    production_policy_active: bool = False,
    activation_metadata: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    rank_path = output_dir / "machinery_final_rank_table.csv"
    sidecar_path = output_dir / "machinery_stage11_survivorship_calibration_panel.csv"
    lineage_path = output_dir / "machinery_financial_filing_lineage.csv"
    manifest_path = output_dir / "machinery_final_rank_table_manifest.json"
    existing = [
        path
        for path in (rank_path, sidecar_path, lineage_path, manifest_path)
        if path.exists()
    ]
    if existing and not allow_overwrite:
        raise FileExistsError(f"Refusing to overwrite immutable machinery dashboard artifacts: {existing}")
    errors = validate_rank_rows(
        rows,
        asof=asof,
        allow_production=production_policy_active,
    )
    if errors:
        raise ValueError("; ".join(errors[:20]))
    sidecar_rows = survivorship_sidecar(rows)
    write_csv_atomic(rank_path, FINAL_RANK_FIELDS, rows)
    write_csv_atomic(sidecar_path, FINAL_RANK_FIELDS, sidecar_rows)
    lineage_manifest = write_financial_lineage_report(
        lineage_path,
        rows,
        model_family=MODEL_FAMILY,
        asof=asof,
        policy_context="production",
    )
    manifest = {
        "acceptance": str(lineage_manifest["acceptance"]),
        "model_family": MODEL_FAMILY,
        "asof_date": asof,
        "rank_table": str(rank_path),
        "rank_table_sha256": file_sha256(rank_path),
        "sidecar": str(sidecar_path),
        "sidecar_sha256": file_sha256(sidecar_path),
        "financial_filing_lineage": lineage_manifest,
        "row_count": len(rows),
        "rank_ready_count": sum(str(row.get("rank_ready_flag") or "") == "1" for row in rows),
        "portfolio_candidate_count": sum(
            str(row.get("portfolio_candidate_gate") or "") == "1"
            for row in rows
        ),
        "production_policy_active": production_policy_active,
        "activation_metadata": dict(activation_metadata or {}),
        "selected_sleeve_count": sum(
            str(row.get("portfolio_sleeve_selected_flag") or "") == "1"
            for row in rows
        )
        if production_policy_active
        else 0,
        "sidecar_retained_shadow": production_policy_active,
        "sidecar_calibration_eligible_count": sum(
            str(row.get("stage11_calibration_input_eligible_flag") or "") == "1" for row in sidecar_rows
        ),
        "contract_fields": FINAL_RANK_FIELDS,
        "scoring_contract_versions": sorted({row["scoring_contract_version"] for row in rows}),
        "published_at_utc": datetime.now(timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z"),
    }
    write_json_atomic(manifest_path, manifest)
    return manifest


def dated_path(root: Path, asof: str, filename: str) -> Path:
    return root / asof / filename


def write_feature_rows(path: Path, rows: Iterable[dict[str, str]]) -> None:
    write_csv_atomic(path, SCORING_FEATURE_FIELDS, rows)


def write_rank_rows(path: Path, rows: Iterable[dict[str, str]]) -> None:
    write_csv_atomic(path, FINAL_RANK_FIELDS, rows)
