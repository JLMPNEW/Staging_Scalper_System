from __future__ import annotations

import csv
import hashlib
import json
import math
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from industrials.core.reports import write_csv_atomic, write_text_atomic


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
    "revenue_ttm_usd",
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
    "remaining_performance_obligation",
    "operating_cash_flow_ttm_usd",
    "capex_usd",
    "orders_ttm_usd",
    "funded_backlog_usd",
    "orders_yoy_growth",
    "backlog_yoy_growth",
    "roic",
    "asset_turnover",
    "incremental_operating_margin",
    "inventory_growth",
    "inventory_sales_growth_spread",
    "cash_conversion_cycle_change",
    "net_debt_to_ebitda",
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
    "rpo_to_revenue",
    "capex_to_revenue",
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
]

FINAL_RANK_FIELDS = [
    *SCORING_FEATURE_FIELDS,
    "final_rank",
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
) -> dict[str, dict[str, Any]]:
    rows = conn.execute(
        f"""
        SELECT *
        FROM (
            SELECT source_rows.*,
                   ROW_NUMBER() OVER (
                       PARTITION BY ticker
                       ORDER BY asof_date DESC, source_id ASC
                   ) AS source_row_number
            FROM {table} source_rows
            WHERE model_family = ? AND asof_date <= ?
        )
        WHERE source_row_number = 1
        """,
        (MODEL_FAMILY, asof),
    ).fetchall()
    return {
        str(row["ticker"]): {key: row[key] for key in row.keys() if key != "source_row_number"}
        for row in rows
    }


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
    return cohort == "development_stage_emerging_machinery" or stage in {"development", "development_stage"}


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
        if capital_raise_dependence is not None:
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
        return 70.0
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
    component_weights: dict[str, Any],
    min_score_confidence: float,
    max_staleness_days: int,
    min_avg_dollar_volume: float,
) -> list[dict[str, str]]:
    asof = parse_asof(asof)
    memberships = _membership_rows(conn, asof=asof)
    if not memberships:
        raise ValueError(f"No machinery membership rows are effective at {asof}")
    market = _latest_rows(conn, "feature_market_technical", asof=asof)
    financial = _latest_rows(conn, "feature_financial_statement", asof=asof)
    positioning = _latest_rows(conn, "feature_positioning", asof=asof)
    weights = _validate_weights(component_weights)
    combined: list[dict[str, Any]] = []
    for membership in memberships:
        ticker = str(membership["ticker"])
        market_row = market.get(ticker, {})
        financial_row = financial.get(ticker, {})
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
            "development_stage": membership["development_stage"],
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
        }
        for field in RAW_FEATURE_FIELDS:
            if field in market_row:
                row[field] = market_row[field]
            elif field in financial_row:
                row[field] = financial_row[field]
            elif field in positioning_row:
                row[field] = positioning_row[field]
            else:
                row[field] = ""
        revenue_usd = financial_row.get("revenue_ttm_usd") or financial_row.get("revenue_usd")
        revenue_local = financial_row.get("revenue_ttm") or financial_row.get("revenue")
        # Stage 4 owns backlog period/currency alignment. Preserve its null on a
        # failed alignment rather than recomputing from mixed local/USD values.
        row["backlog_to_revenue"] = financial_row.get("backlog_to_revenue", "")
        row["rpo_to_revenue"] = _ratio(financial_row.get("remaining_performance_obligation"), revenue_local)
        row["capex_to_revenue"] = _ratio(financial_row.get("capex_usd"), revenue_usd, absolute_numerator=True)
        row["market_cap_source"] = str(financial_row.get("source_id") or "") if _float(row.get("market_cap")) is not None else ""
        combined.append(row)

    metric_scores = _score_metrics(combined)
    scoring_signal_metrics = {metric for metrics in COMPONENT_METRICS.values() for metric in metrics}
    output: list[dict[str, str]] = []
    for row in combined:
        ticker = str(row["ticker"])
        scores = metric_scores[ticker]
        for component, metrics in COMPONENT_METRICS.items():
            available = [scores[metric] for metric in metrics if metric in scores]
            row[component] = sum(available) / len(available) if available else NEUTRAL_SCORE
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
        if adv is None or adv < min_avg_dollar_volume:
            reasons.append("insufficient_liquidity")
        if confidence < min_score_confidence:
            reasons.append("low_score_confidence")
        rank_ready = not reasons
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
            if field in RAW_FEATURE_FIELDS or field in COMPONENT_FIELDS or field in {
                "membership_confidence",
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


def validate_rank_rows(rows: list[dict[str, str]], *, asof: str) -> list[str]:
    errors: list[str] = []
    if not rows:
        return ["rank table is empty"]
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
        if str(row.get("portfolio_candidate_gate") or "") != "0":
            errors.append(f"{ticker}: shadow publisher must set portfolio_candidate_gate=0")
        if str(row.get("oos_score_valid_flag") or "") != "0":
            errors.append(f"{ticker}: shadow publisher must set oos_score_valid_flag=0")
        try:
            ranks.append(int(str(row.get("final_rank") or "")))
        except ValueError:
            errors.append(f"{ticker}: invalid final_rank={row.get('final_rank')!r}")
    if sorted(ranks) != list(range(1, len(rows) + 1)):
        errors.append("final_rank must be contiguous from 1 through row count")
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
) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    rank_path = output_dir / "machinery_final_rank_table.csv"
    sidecar_path = output_dir / "machinery_stage11_survivorship_calibration_panel.csv"
    manifest_path = output_dir / "machinery_final_rank_table_manifest.json"
    existing = [path for path in (rank_path, sidecar_path, manifest_path) if path.exists()]
    if existing and not allow_overwrite:
        raise FileExistsError(f"Refusing to overwrite immutable machinery dashboard artifacts: {existing}")
    errors = validate_rank_rows(rows, asof=asof)
    if errors:
        raise ValueError("; ".join(errors[:20]))
    sidecar_rows = survivorship_sidecar(rows)
    write_csv_atomic(rank_path, FINAL_RANK_FIELDS, rows)
    write_csv_atomic(sidecar_path, FINAL_RANK_FIELDS, sidecar_rows)
    manifest = {
        "acceptance": "PASS",
        "model_family": MODEL_FAMILY,
        "asof_date": asof,
        "rank_table": str(rank_path),
        "rank_table_sha256": file_sha256(rank_path),
        "sidecar": str(sidecar_path),
        "sidecar_sha256": file_sha256(sidecar_path),
        "row_count": len(rows),
        "rank_ready_count": sum(str(row.get("rank_ready_flag") or "") == "1" for row in rows),
        "portfolio_candidate_count": 0,
        "sidecar_calibration_eligible_count": sum(
            str(row.get("stage11_calibration_input_eligible_flag") or "") == "1" for row in sidecar_rows
        ),
        "contract_fields": FINAL_RANK_FIELDS,
        "scoring_contract_versions": sorted({row["scoring_contract_version"] for row in rows}),
        "published_at_utc": datetime.utcnow().isoformat(timespec="seconds") + "Z",
    }
    write_json_atomic(manifest_path, manifest)
    return manifest


def dated_path(root: Path, asof: str, filename: str) -> Path:
    return root / asof / filename


def write_feature_rows(path: Path, rows: Iterable[dict[str, str]]) -> None:
    write_csv_atomic(path, SCORING_FEATURE_FIELDS, rows)


def write_rank_rows(path: Path, rows: Iterable[dict[str, str]]) -> None:
    write_csv_atomic(path, FINAL_RANK_FIELDS, rows)
