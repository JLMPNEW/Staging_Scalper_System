#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


PACKAGE_ROOT = Path(__file__).resolve().parents[2]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from industrials.core.config import cfg_get, load_yaml, resolve_path  # noqa: E402
from industrials.core.db import init_db  # noqa: E402
from industrials.core.logging_utils import configure_utc_logging  # noqa: E402
from industrials.core.text_norm import normalize_ticker  # noqa: E402


DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
MODEL_FAMILY = "defense"
SCORE_MODEL_VERSION = "defense_shadow_v0.1.0"
MODEL_VERSION = "defense_shadow_2026_07"
SCORING_CONTRACT_VERSION = "tech_family_final_rank_table_v1_shadow"
NEUTRAL_SCORE = 50.0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Publish the shadow defense final rank table.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--asof", required=True, help="Market/PIT as-of date, YYYY-MM-DD.")
    parser.add_argument("--output-dir", type=Path, default=None)
    return parser.parse_args()


def parse_asof(raw: str) -> str:
    return datetime.strptime(raw.strip(), "%Y-%m-%d").date().isoformat()


def finite(raw: Any) -> float | None:
    if raw is None:
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def fmt(value: Any, digits: int = 6) -> str:
    number = finite(value)
    if number is None:
        return ""
    return f"{number:.{digits}f}".rstrip("0").rstrip(".")


def flag(value: bool) -> str:
    return "1" if value else "0"


def percentile_map(rows: list[dict[str, Any]], field: str, *, higher_is_better: bool = True) -> dict[str, float]:
    values: list[tuple[str, float]] = []
    for row in rows:
        value = finite(row.get(field))
        ticker = str(row.get("ticker") or "")
        if ticker and value is not None:
            values.append((ticker, value))
    if not values:
        return {}
    values.sort(key=lambda item: item[1])
    if len(values) == 1:
        score = 100.0 if higher_is_better else 0.0
        return {values[0][0]: score}
    out: dict[str, float] = {}
    for index, (ticker, _) in enumerate(values):
        pct = 100.0 * index / (len(values) - 1)
        out[ticker] = pct if higher_is_better else 100.0 - pct
    return out


@dataclass(frozen=True)
class Component:
    score: float
    quality: float
    status: str
    available: int
    missing: int


def component(
    ticker: str,
    fields: list[str],
    score_maps: dict[str, dict[str, float]],
    *,
    extra_scores: list[float] | None = None,
) -> Component:
    scores: list[float] = []
    missing = 0
    for field in fields:
        value = score_maps.get(field, {}).get(ticker)
        if value is None:
            missing += 1
        else:
            scores.append(value)
    for score in extra_scores or []:
        if math.isfinite(score):
            scores.append(score)
    available = len(scores)
    total = available + missing
    if not scores:
        return Component(NEUTRAL_SCORE, 0.0, "missing_neutralized", 0, total)
    quality = available / total if total else 0.0
    status = "complete" if missing == 0 else "partial"
    return Component(sum(scores) / len(scores), quality, status, available, missing)


def quality_value(status: str, *, kind: str) -> float:
    text = str(status or "").strip().lower()
    if kind == "market":
        return 1.0 if text == "complete" else 0.65 if text == "review" else 0.5
    if kind == "financial":
        value = finite(status)
        if value is not None:
            return max(0.0, min(1.0, value))
        return 1.0 if text == "complete" else 0.5
    if kind == "positioning":
        return 1.0 if text == "complete" else 0.75 if text == "policy_exempt" else 0.4
    return 0.5


def load_csv_policy(path: Path) -> dict[tuple[str, str], dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    out: dict[tuple[str, str], dict[str, str]] = {}
    for row in rows:
        profile = str(row.get("reporting_profile") or "").strip()
        stage = str(row.get("development_stage") or "").strip()
        if profile and stage:
            out[(profile, stage)] = row
    return out


def resolve_policy(policies: dict[tuple[str, str], dict[str, str]], profile: str, stage: str) -> dict[str, str]:
    return policies.get((profile, stage)) or policies.get((profile, "any")) or {}


def header(project_root: Path) -> list[str]:
    semi = project_root / "output" / "technology_reports" / "semi_dashboard" / "semiconductor_final_rank_table.csv"
    if not semi.exists():
        raise FileNotFoundError(f"Semiconductor rank-table contract header not found: {semi}")
    with semi.open("r", encoding="utf-8-sig", newline="") as handle:
        cols = next(csv.reader(handle))
    return [col.replace("big_tech_capex_", "defense_budget_backlog_") for col in cols]


def fetch_map(conn: sqlite3.Connection, sql: str, params: tuple[Any, ...]) -> dict[str, dict[str, Any]]:
    rows = conn.execute(sql, params).fetchall()
    return {normalize_ticker(row["ticker"]): dict(row) for row in rows if normalize_ticker(row["ticker"])}


def load_rows(conn: sqlite3.Connection, *, asof: str, config: dict[str, Any]) -> list[dict[str, Any]]:
    market_source = str(cfg_get(config, "market_feature_build.source_id", "yahoo_finance_adjusted"))
    financial_source = str(cfg_get(config, "sec_fundamentals.companyfacts_source_id", "sec_companyfacts"))
    positioning_source = str(cfg_get(config, "positioning_import.source_id", "industrials_positioning_composite"))
    submissions_source = str(cfg_get(config, "sec_fundamentals.submissions_source_id", "sec_submissions"))
    active = fetch_map(
        conn,
        """
        SELECT c.ticker, c.company_name, c.sector, c.industry, c.subsector, c.country, c.currency,
               c.universe_status, t.calibration_cohort_id, t.calibration_cohort, t.development_stage,
               m.membership_source_id AS historical_universe_source, m.start_date AS price_start_date,
               m.end_date AS terminal_date
        FROM dim_company c
        JOIN dim_industrials_taxonomy t ON t.company_id = c.company_id AND t.model_family = ?
        LEFT JOIN dim_universe_membership m
          ON m.company_id = c.company_id AND m.model_family = t.model_family AND m.is_current_member = 1
        WHERE c.is_active = 1
        ORDER BY c.ticker
        """,
        (MODEL_FAMILY,),
    )
    market = fetch_map(
        conn,
        "SELECT * FROM feature_market_technical WHERE model_family = ? AND source_id = ? AND asof_date = ?",
        (MODEL_FAMILY, market_source, asof),
    )
    financial = fetch_map(
        conn,
        "SELECT * FROM feature_financial_statement WHERE model_family = ? AND source_id = ? AND asof_date = ?",
        (MODEL_FAMILY, financial_source, asof),
    )
    positioning = fetch_map(
        conn,
        "SELECT * FROM feature_positioning WHERE model_family = ? AND source_id = ? AND asof_date = ?",
        (MODEL_FAMILY, positioning_source, asof),
    )
    profiles = fetch_map(
        conn,
        """
        SELECT *
        FROM dim_issuer_reporting_profile
        WHERE model_family = ?
          AND source_id IN (?, ?)
        ORDER BY ticker
        """,
        (MODEL_FAMILY, financial_source, submissions_source),
    )
    price_dates = fetch_map(
        conn,
        """
        SELECT ticker, MIN(bar_date) AS price_start_date, MAX(bar_date) AS price_end_date
        FROM fact_price_ohlcv
        WHERE source_id = ?
        GROUP BY ticker
        """,
        (market_source,),
    )
    filing_urls = {
        (str(row["ticker"]), str(row["accession_number"])): dict(row)
        for row in conn.execute(
            """
            SELECT ticker, accession_number, form_type, filing_date, filing_url
            FROM fact_sec_filing
            WHERE source_id = ?
            """,
            (submissions_source,),
        )
    }
    rows: list[dict[str, Any]] = []
    for ticker, base in active.items():
        merged: dict[str, Any] = dict(base)
        merged.update({f"market_{k}": v for k, v in market.get(ticker, {}).items()})
        merged.update({f"financial_{k}": v for k, v in financial.get(ticker, {}).items()})
        merged.update({f"positioning_{k}": v for k, v in positioning.get(ticker, {}).items()})
        merged.update({f"profile_{k}": v for k, v in profiles.get(ticker, {}).items()})
        merged["price_start_date"] = price_dates.get(ticker, {}).get("price_start_date") or merged.get("price_start_date") or ""
        merged["price_end_date"] = price_dates.get(ticker, {}).get("price_end_date") or ""
        accession = str(merged.get("financial_accession_number") or "")
        filing = filing_urls.get((ticker, accession), {})
        merged["latest_sec_form"] = merged.get("financial_form_type") or filing.get("form_type") or merged.get("profile_latest_form_type") or ""
        merged["latest_sec_filing_date"] = filing.get("filing_date") or merged.get("profile_latest_filing_date") or ""
        merged["latest_sec_url"] = filing.get("filing_url") or ""
        rows.append(merged)
    return rows


def add_feature_aliases(rows: list[dict[str, Any]]) -> None:
    aliases = {
        "market_cap": "financial_market_cap",
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
    for row in rows:
        row["ticker"] = normalize_ticker(row.get("ticker"))
        row["low_liquidity_flag"] = int(finite(row.get("market_low_liquidity_flag")) or 0)
        row["share_count_yoy_growth"] = ""
        for out_name, source_name in aliases.items():
            row[out_name] = row.get(source_name)


def build_scores(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    add_feature_aliases(rows)
    score_maps: dict[str, dict[str, float]] = {}
    high_fields = [
        "fcf_yield", "gross_margin", "operating_margin", "fcf_margin", "fcf_to_net_income",
        "net_cash_to_assets", "revenue_yoy_growth", "gross_profit_yoy_growth",
        "operating_income_yoy_growth", "free_cash_flow_yoy_growth", "revenue_acceleration",
        "ret_3m", "ret_12m_ex_1m", "rel_strength_bench_3m", "distance_from_52w_high",
        "avg_dollar_volume_60d", "insider_net_value_90d", "insider_cluster_buyers_90d",
        "institutional_ownership_delta_pct",
    ]
    low_fields = [
        "ev_gross_profit", "ev_operating_income", "inventory_days", "sbc_pct_revenue",
        "r_and_d_pct_revenue", "realized_vol_60d", "latest_short_interest_pct_float",
        "short_interest_change_3m", "latest_days_to_cover", "latest_borrow_fee_rate",
    ]
    for field in high_fields:
        score_maps[field] = percentile_map(rows, field, higher_is_better=True)
    for field in low_fields:
        score_maps[field] = percentile_map(rows, field, higher_is_better=False)
    score_maps["max_drawdown_12m"] = percentile_map(rows, "max_drawdown_12m", higher_is_better=True)
    out: dict[str, dict[str, Any]] = {}
    for row in rows:
        ticker = str(row["ticker"])
        low_liquidity_score = 0.0 if int(row.get("low_liquidity_flag") or 0) else 100.0
        valuation = component(ticker, ["fcf_yield", "ev_gross_profit", "ev_operating_income"], score_maps)
        quality = component(
            ticker,
            ["gross_margin", "operating_margin", "fcf_margin", "fcf_to_net_income", "net_cash_to_assets", "sbc_pct_revenue"],
            score_maps,
        )
        risk = component(
            ticker,
            ["realized_vol_60d", "max_drawdown_12m", "distance_from_52w_high"],
            score_maps,
            extra_scores=[low_liquidity_score],
        )
        positioning = component(
            ticker,
            [
                "insider_net_value_90d", "insider_cluster_buyers_90d", "institutional_ownership_delta_pct",
                "latest_short_interest_pct_float", "short_interest_change_3m", "latest_days_to_cover", "latest_borrow_fee_rate",
            ],
            score_maps,
        )
        market_behavior = component(
            ticker,
            ["ret_3m", "ret_12m_ex_1m", "rel_strength_bench_3m", "realized_vol_60d", "max_drawdown_12m", "distance_from_52w_high"],
            score_maps,
        )
        growth = component(
            ticker,
            [
                "revenue_yoy_growth", "gross_profit_yoy_growth", "operating_income_yoy_growth",
                "free_cash_flow_yoy_growth", "revenue_acceleration",
            ],
            score_maps,
        )
        components = [valuation, quality, risk, positioning, market_behavior, growth]
        weighted = [
            (valuation.score, 0.16), (quality.score, 0.18), (risk.score, 0.18),
            (positioning.score, 0.12), (market_behavior.score, 0.20), (growth.score, 0.16),
        ]
        total_weight = sum(weight for comp, weight in zip(components, [w for _, w in weighted]) if comp.available > 0)
        core = (
            sum(score * weight for (score, weight), comp in zip(weighted, components) if comp.available > 0) / total_weight
            if total_weight > 0
            else NEUTRAL_SCORE
        )
        overlay = NEUTRAL_SCORE
        final_score = max(0.0, min(100.0, 0.9 * core + 0.1 * overlay))
        out[ticker] = {
            "valuation": valuation,
            "quality": quality,
            "risk_control": risk,
            "positioning": positioning,
            "market_behavior": market_behavior,
            "growth": growth,
            "sector_cycle": Component(NEUTRAL_SCORE, 0.0, "neutralized_not_loaded", 0, 1),
            "defense_budget_backlog": Component(NEUTRAL_SCORE, 0.0, "neutralized_not_loaded", 0, 1),
            "core_score": core,
            "sector_overlay_score": overlay,
            "final_score": final_score,
            "available": sum(comp.available for comp in components),
            "missing": sum(comp.missing for comp in components),
        }
    return out


def rank_percentiles(scores: dict[str, dict[str, Any]]) -> dict[str, tuple[int, float]]:
    ordered = sorted(scores.items(), key=lambda item: (-float(item[1]["final_score"]), item[0]))
    out: dict[str, tuple[int, float]] = {}
    n = len(ordered)
    for idx, (ticker, _) in enumerate(ordered, start=1):
        pct = 100.0 if n == 1 else 100.0 * (n - idx) / (n - 1)
        out[ticker] = (idx, pct)
    return out


def compose_rows(rows: list[dict[str, Any]], scores: dict[str, dict[str, Any]], policies: dict[tuple[str, str], dict[str, str]], asof: str) -> list[dict[str, str]]:
    ranks = rank_percentiles(scores)
    out: list[dict[str, str]] = []
    for row in rows:
        ticker = str(row["ticker"])
        score = scores[ticker]
        rank, pct = ranks[ticker]
        market_quality = quality_value(str(row.get("market_market_data_quality") or ""), kind="market")
        financial_confidence = quality_value(str(row.get("financial_financial_confidence") or ""), kind="financial")
        positioning_quality = quality_value(str(row.get("positioning_positioning_quality") or ""), kind="positioning")
        confidence = (market_quality + financial_confidence + positioning_quality) / 3.0
        profile = str(row.get("financial_reporting_profile") or row.get("profile_reporting_profile") or "NO_FINANCIALS_REVIEW")
        development_stage = str(row.get("financial_development_stage") or row.get("development_stage") or "operating")
        policy = resolve_policy(policies, profile, development_stage)
        rank_policy = str(policy.get("rank_ready_policy") or "")
        min_conf = finite(policy.get("minimum_financial_confidence")) or 0.0
        rank_ready = (
            rank_policy.startswith("eligible")
            and financial_confidence >= min_conf
            and str(row.get("financial_data_quality_status") or "") == "complete"
            and str(row.get("positioning_positioning_quality") or "") in {"complete", "policy_exempt"}
        )
        review_reasons = []
        if not rank_ready:
            review_reasons.append(str(policy.get("review_reason") or rank_policy or "not_rank_ready"))
        if str(row.get("market_market_data_quality") or "") == "review":
            review_reasons.append("market_data_review")
        if str(row.get("positioning_positioning_quality") or "") == "policy_exempt":
            review_reasons.append("positioning_policy_exempt")
        model_status = "complete" if rank_ready else "review"
        calibration_reason = "shadow_only_oos_calibration_not_available"
        record: dict[str, str] = {
            "ticker": ticker,
            "asof_date": asof,
            "score_model_version": SCORE_MODEL_VERSION,
            "model_family": MODEL_FAMILY,
            "model_version": MODEL_VERSION,
            "scoring_contract_version": SCORING_CONTRACT_VERSION,
            "company_name": str(row.get("company_name") or ""),
            "sector": "Industrials",
            "industry": "Aerospace & Defense",
            "subsector": "Defense",
            "country": str(row.get("country") or "United States"),
            "currency": str(row.get("currency") or "USD"),
            "universe_status": str(row.get("universe_status") or "active"),
            "historical_universe_source": str(row.get("historical_universe_source") or ""),
            "price_start_date": str(row.get("price_start_date") or ""),
            "price_end_date": str(row.get("price_end_date") or ""),
            "terminal_date": str(row.get("terminal_date") or ""),
            "historical_price_ticker": ticker,
            "latest_price_date": str(row.get("market_latest_bar_date") or ""),
            "market_feature_asof_date": str(row.get("market_asof_date") or asof),
            "financial_feature_asof_date": str(row.get("financial_asof_date") or asof),
            "positioning_feature_asof_date": str(row.get("positioning_asof_date") or asof),
            "final_rank": str(rank),
            "final_percentile": fmt(pct, 4),
            "final_score": fmt(score["final_score"]),
            "core_score": fmt(score["core_score"]),
            "sector_overlay_score": fmt(score["sector_overlay_score"]),
            "data_quality_confidence": fmt(confidence, 4),
            "rank_ready_flag": flag(rank_ready),
            "calibration_eligible_flag": "0",
            "model_status": model_status,
            "review_reason": ";".join(reason for reason in review_reasons if reason),
            "calibration_cohort_id": str(row.get("calibration_cohort_id") or ""),
            "calibration_cohort": str(row.get("calibration_cohort") or ""),
            "market_quality": fmt(market_quality, 4),
            "financial_quality": fmt(financial_confidence, 4),
            "positioning_quality": fmt(positioning_quality, 4),
            "core_available_component_count": str(score["available"]),
            "core_missing_component_count": str(score["missing"]),
            "core_data_quality_confidence": fmt(confidence, 4),
            "latest_sec_form": str(row.get("latest_sec_form") or ""),
            "latest_sec_filing_date": str(row.get("latest_sec_filing_date") or ""),
            "latest_sec_url": str(row.get("latest_sec_url") or ""),
            "calibration_usage": "shadow_only",
            "calibration_input_valid_flag": "0",
            "oos_score_valid_flag": "0",
            "oos_invalid_reason": calibration_reason,
            "feature_point_in_time_flag": "1",
            "future_return_excluded_flag": "1",
            "non_point_in_time_sections_omitted_flag": "1",
            "scoring_weights_frozen_flag": "1",
            "calibration_train_start_date": "",
            "calibration_train_end_date": "",
            "calibration_lock_date": "",
            "calibration_production_start_date": "",
            "calibration_validation_method": "not_available_shadow_only",
            "calibration_provenance_version": SCORE_MODEL_VERSION,
            "oos_assertion_basis": "not_available_shadow_only",
            "portfolio_candidate_gate": "0",
            "portfolio_candidate_score": fmt(score["final_score"]),
            "portfolio_candidate_status": "shadow_only",
            "portfolio_candidate_reason": calibration_reason,
            "research_calibration_input_eligible_flag": "0",
            "research_calibration_status": "shadow_only",
            "research_calibration_reason": calibration_reason,
            "calibration_sample_role": "excluded",
            "calibration_status": "shadow_only",
            "calibration_status_reason": calibration_reason,
            "survivorship_corrected_panel_flag": "0",
            "stage11_calibration_panel_source": "",
            "stage11_calibration_input_eligible_flag": "0",
            "stage11_calibration_input_reason": calibration_reason,
            "score_scale_min": "0",
            "score_scale_max": "100",
            "score_neutral_value": "50",
            "score_confidence": fmt(confidence, 4),
            "eligibility_reason": "shadow_only_oos_pending" if rank_ready else ";".join(review_reasons) or "not_rank_ready",
            "native_score_field": "final_score",
            "native_score_value": fmt(score["final_score"]),
            "score_zero_is_missing_flag": "0",
            "calibration_only": "0",
            "recovery_type": "",
            "equity_recovery": "",
            "drop_otc_tape": "0",
            "source_snapshot_asof_date": asof,
            "price_data_asof_date": str(row.get("market_latest_bar_date") or ""),
            "feature_data_asof_date": asof,
            "financial_data_asof_date": str(row.get("financial_asof_date") or asof),
            "short_interest_asof_date": asof if row.get("positioning_latest_short_interest_shares") is not None else "",
            "institutional_data_asof_date": asof if row.get("positioning_latest_institutional_shares") is not None else "",
            "insider_data_asof_date": asof,
            "borrow_data_asof_date": asof if row.get("positioning_latest_borrow_fee_rate") is not None else "",
            "forward_catalyst_event_date": "",
            "forward_catalyst_event_type": "",
            "forward_catalyst_nearest_days": "",
            "forward_catalyst_source": "",
            "forward_catalyst_confidence": "",
            "forward_catalyst_asof_date": "",
        }
        for field in [
            "market_cap", "latest_price", "revenue_yoy_growth", "gross_profit_yoy_growth",
            "operating_income_yoy_growth", "free_cash_flow_yoy_growth", "revenue_acceleration",
            "gross_margin", "operating_margin", "fcf_margin", "fcf_to_net_income",
            "net_cash_to_assets", "sbc_pct_revenue", "r_and_d_pct_revenue",
            "share_count_yoy_growth", "inventory_days", "fcf_yield", "ev_gross_profit",
            "ev_operating_income", "ret_3m", "ret_12m_ex_1m", "rel_strength_bench_3m",
            "realized_vol_60d", "max_drawdown_12m", "distance_from_52w_high",
            "avg_dollar_volume_60d", "low_liquidity_flag", "insider_net_value_90d",
            "insider_cluster_buyers_90d", "institutional_ownership_delta_pct",
            "latest_short_interest_pct_float", "short_interest_change_3m", "latest_days_to_cover",
            "latest_borrow_fee_rate",
        ]:
            record[field] = fmt(row.get(field)) if field != "low_liquidity_flag" else str(row.get(field) or 0)
        for name in [
            "valuation", "quality", "risk_control", "positioning", "market_behavior",
            "growth", "sector_cycle", "defense_budget_backlog",
        ]:
            comp = score[name]
            record[f"{name}_score"] = fmt(comp.score)
            if name != "positioning":
                record[f"{name}_quality"] = fmt(comp.quality, 4)
            record[f"{name}_status"] = comp.status
        out.append(record)
    return out


def write_csv(path: Path, fields: list[str], rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore", lineterminator="\n")
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in fields})


def seal_manifest(path: Path, *, rows: int, asof: str) -> Path:
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    manifest = {
        "artifact": str(path),
        "asof_date": asof,
        "rows": rows,
        "sha256": digest,
        "score_model_version": SCORE_MODEL_VERSION,
        "model_family": MODEL_FAMILY,
        "shadow_only": True,
        "sealed_at_utc": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
    }
    manifest_path = path.with_name("defense_final_rank_table_manifest.json")
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return manifest_path


def main() -> int:
    configure_utc_logging()
    args = parse_args()
    asof = parse_asof(args.asof)
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    base_dir = config_path.parent
    db_path = resolve_path(cfg_get(config, "paths.database_path"), base_dir=base_dir)
    policy_path = resolve_path(cfg_get(config, "scoring_policy.defense_eligibility_policy_csv"), base_dir=base_dir)
    output_dir = (
        args.output_dir.expanduser().resolve()
        if args.output_dir
        else PROJECT_ROOT / "output" / "industrials" / "defense" / "dashboard" / asof
    )
    output_path = output_dir / "defense_final_rank_table.csv"
    fields = header(PROJECT_ROOT)
    policies = load_csv_policy(policy_path)
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        init_db(conn)
        rows = load_rows(conn, asof=asof, config=config)
    if not rows:
        raise ValueError(f"No active defense rows found for asof={asof}")
    missing_feature = [
        row["ticker"]
        for row in rows
        if not row.get("market_ticker") or not row.get("financial_ticker") or not row.get("positioning_ticker")
    ]
    if missing_feature:
        raise ValueError(f"Missing Stage 3/4/5 feature rows for active tickers: {missing_feature[:20]}")
    scores = build_scores(rows)
    out_rows = compose_rows(rows, scores, policies, asof)
    write_csv(output_path, fields, out_rows)
    manifest_path = seal_manifest(output_path, rows=len(out_rows), asof=asof)
    print(f"Wrote {output_path}")
    print(f"Wrote {manifest_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
