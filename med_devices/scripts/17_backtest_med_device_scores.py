#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import logging
import math
import sqlite3
import sys
from datetime import date, datetime
from pathlib import Path
from statistics import mean, median
from typing import Any


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from med_devices.core.config import cfg_get, load_yaml, resolve_path  # noqa: E402
from med_devices.core.db import connect, init_db  # noqa: E402
from med_devices.core.logging_utils import configure_utc_logging  # noqa: E402
from med_devices.core.market_policy import calibration_market_sources  # noqa: E402


DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
LOGGER = logging.getLogger("backtest_med_device_scores")


def init_db_read_tolerant(conn: Any) -> None:
    try:
        init_db(conn)
    except sqlite3.OperationalError as exc:
        if "readonly database" not in str(exc).lower():
            raise
        LOGGER.warning("Skipping schema migration during read-only backtest connection: %s", exc)


BASE_FIELDS = [
    "asof_date",
    "scoring_model_version",
    "ticker",
    "company_name",
    "subsector",
    "rank",
    "classification",
    "decision_bucket",
    "entry_status",
    "final_investability_gate",
    "portfolio_candidate_gate",
    "calibration_cohort",
    "calibration_eligible_flag",
    "research_calibration_input_eligible_flag",
    "research_calibration_status",
    "research_calibration_reason",
    "calibration_sample_role",
    "stage11_calibration_input_eligible_flag",
    "stage11_calibration_input_reason",
    "stage11_calibration_panel_source",
    "survivorship_corrected_panel_flag",
    "composite_score",
    "raw_composite_score",
    "ic_tilted_composite_score",
    "ic_tilted_composite_delta",
    "ic_tilted_composite_coverage",
    "ic_tilted_composite_active_weight",
    "ic_tilted_composite_payload_json",
    "composite_percentile",
    "cohort_percentile",
    "safe_core_score",
    "safe_core_percentile",
    "safe_core_cohort_percentile",
    "safe_core_rank",
    "safe_core_status",
    "safe_core_reason",
    "passed_safe_core_gate",
    "safe_core_model_version",
    "legacy_all_gates_gate",
    "legacy_gate_misses",
    "tier1_safety_status",
    "tier1_safety_reason",
    "passed_tier1_safety_gate",
    "fundamental_quality_score",
    "durable_growth_score",
    "durable_growth_score_legacy",
    "durable_growth_alpha_score",
    "durable_growth_growth_score",
    "durable_growth_quality_score",
    "durable_growth_efficiency_score",
    "durable_growth_capital_discipline_score",
    "durable_growth_evidence_quality_score",
    "fda_product_score",
    "fda_product_score_legacy",
    "fda_alpha_score",
    "fda_safety_score",
    "fda_clearance_velocity_raw",
    "fda_clearance_velocity_score",
    "fda_clearance_acceleration_raw",
    "fda_clearance_acceleration_score",
    "fda_evidence_quality_score",
    "fda_safety_breadth_adjusted_score",
    "fda_safety_product_family_adjusted_score",
    "reimbursement_score",
    "valuation_score",
    "technical_entry_score",
    "sentiment_catalyst_score",
    "value_trap_score",
    "technical_trend_quality_score",
    "technical_relative_strength_score",
    "technical_liquidity_score",
    "technical_volume_breakout_score",
    "technical_volatility_risk_score",
    "technical_setup_score",
    "technical_core_score",
    "technical_alpha_score",
    "technical_pullback_score",
    "technical_overextension_score",
    "technical_breakdown_flag",
    "technical_liquidity_gate_flag",
    "momentum_12_1",
    "realized_vol_60d",
    "round_trip_cost_estimate",
    "technical_signal_mode",
    "technical_signal_direction",
    "technical_signal_reliability",
    "technical_score_source",
    "durable_growth_signal_mode",
    "durable_growth_signal_direction",
    "durable_growth_signal_reliability",
    "durable_growth_score_source",
    "durable_growth_gate_mode",
    "durable_growth_policy_reason",
    "durable_growth_gate_excluded",
    "durable_growth_component_weight",
    "durable_growth_repair_flag",
    "durable_growth_repair_reason",
    "durable_growth_validation_status",
    "durable_growth_validation_reason",
    "durable_growth_production_state",
    "quality_value_interaction_score",
    "fda_technical_interaction_score",
    "borrow_availability_score",
    "borrow_fee_score",
    "borrow_squeeze_risk_score",
    "borrow_pressure_score",
    "short_interest_score",
    "short_pressure_score",
    "short_squeeze_score",
    "short_volume_score",
    "short_interest_velocity_score",
    "days_to_cover_score",
    "institutional_accumulation_score",
    "institutional_crowding_score",
    "institutional_breadth_score",
    "insider_net_buy_score",
    "insider_cluster_buy_score",
    "insider_selling_pressure_score",
    "insider_activity_score",
    "pullback_candidate_tag",
    "pullback_candidate_template_id",
    "rank_bucket",
    "entry_price_date",
    "entry_price",
    "price_source_id",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Backtest med-device daily score buckets against forward returns.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--asof", type=str, default="", help="Single as-of date, or comma-separated as-of dates.")
    parser.add_argument("--asof-start", type=str, default="", help="Inclusive start date for multi-as-of backtests.")
    parser.add_argument("--asof-end", type=str, default="", help="Inclusive end date for multi-as-of backtests.")
    parser.add_argument("--all-asofs", action="store_true", help="Backtest every saved med_device_daily_scores as-of date.")
    parser.add_argument(
        "--stage11-eligible-only",
        action="store_true",
        help=(
            "Fail closed to score rows explicitly marked stage11_calibration_input_eligible_flag=1. "
            "Use this for lockbox calibration panels."
        ),
    )
    parser.add_argument(
        "--training-label-end-max",
        type=str,
        default="",
        help=(
            "Latest permissible forward-label date. Stage 11 mode defaults this to "
            "calibration.training_label_end_max and refuses to run without a valid cap."
        ),
    )
    parser.add_argument("--horizons", type=str, default="30,60,120", help="Comma-separated trading-day forward horizons.")
    parser.add_argument("--output-csv", type=Path, default=None)
    return parser.parse_args()


def parse_date(raw: object) -> date | None:
    text = str(raw or "").strip()[:10]
    if not text:
        return None
    try:
        return datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError:
        return None


def to_float(raw: object) -> float | None:
    try:
        value = float(str(raw).strip())
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def value_present(raw: object) -> bool:
    return raw is not None and str(raw).strip() != ""


def first_float(*raw_values: object, default: float = 0.0) -> float:
    for raw in raw_values:
        value = to_float(raw)
        if value is not None:
            return value
    return default


def first_float_or_none(*raw_values: object) -> float | None:
    for raw in raw_values:
        value = to_float(raw)
        if value is not None:
            return value
    return None


def value_or_blank(row: dict[str, Any], key: str) -> object:
    value = row.get(key)
    return "" if value is None else value


def flag_is_one(raw: object) -> bool:
    value = to_float(raw)
    return value is not None and value == 1.0


def filter_stage11_eligible_rows(
    score_rows_by_asof: dict[str, list[dict[str, Any]]],
) -> tuple[dict[str, list[dict[str, Any]]], list[str]]:
    filtered = {
        asof: [
            row
            for row in rows_for_asof
            if flag_is_one(row.get("stage11_calibration_input_eligible_flag"))
        ]
        for asof, rows_for_asof in score_rows_by_asof.items()
    }
    empty_asofs = [asof for asof, rows_for_asof in filtered.items() if not rows_for_asof]
    return (
        {asof: rows_for_asof for asof, rows_for_asof in filtered.items() if rows_for_asof},
        empty_asofs,
    )


def latest_score_asof(conn: Any) -> str:
    row = conn.execute("SELECT MAX(asof_date) AS asof_date FROM med_device_daily_scores").fetchone()
    asof = str(row["asof_date"] or "") if row is not None else ""
    if not asof:
        raise RuntimeError("No med_device_daily_scores rows found; run script 13 first.")
    return asof


def score_asofs(conn: Any, *, start: str = "", end: str = "") -> list[str]:
    clauses: list[str] = []
    params: list[str] = []
    if start:
        clauses.append("asof_date >= ?")
        params.append(start)
    if end:
        clauses.append("asof_date <= ?")
        params.append(end)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = conn.execute(
        f"""
        SELECT DISTINCT asof_date
        FROM med_device_daily_scores
        {where}
        ORDER BY asof_date
        """,
        params,
    ).fetchall()
    return [str(row["asof_date"]) for row in rows if str(row["asof_date"] or "").strip()]


def resolve_score_asofs(conn: Any, args: argparse.Namespace) -> list[str]:
    requested = [item.strip() for item in str(args.asof or "").split(",") if item.strip()]
    if requested:
        return requested
    if args.all_asofs or args.asof_start.strip() or args.asof_end.strip():
        return score_asofs(conn, start=args.asof_start.strip(), end=args.asof_end.strip())
    return [latest_score_asof(conn)]


def load_scores(conn: Any, *, asof: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT s.*, c.ticker, c.company_name, c.subsector,
               t.momentum_12_1,
               t.realized_vol_60d
        FROM med_device_daily_scores s
        JOIN dim_company c ON c.company_id = s.company_id
        LEFT JOIN feature_technical_entry t
          ON t.company_id = s.company_id
         AND t.asof_date = s.asof_date
        WHERE s.asof_date = ?
        ORDER BY s.rank
        """,
        (asof,),
    ).fetchall()
    return [dict(row) for row in rows]


def rank_bucket(percentile: float | None) -> str:
    if percentile is None:
        return "unknown"
    if percentile >= 90.0:
        return "top_decile"
    if percentile >= 80.0:
        return "top_quintile_ex_decile"
    if percentile <= 20.0:
        return "bottom_quintile"
    return "middle"


def load_price_series(
    conn: Any,
    *,
    tickers: list[str],
    source_priority: list[str],
    end_date: date | None = None,
) -> dict[str, tuple[str, list[tuple[date, float]]]]:
    if not tickers:
        return {}
    ticker_placeholders = ",".join("?" for _ in tickers)
    source_placeholders = ",".join("?" for _ in source_priority)
    end_date_clause = "AND bar_date <= ?" if end_date is not None else ""
    params: list[object] = [*tickers, *source_priority]
    if end_date is not None:
        params.append(end_date.isoformat())
    rows = conn.execute(
        f"""
        SELECT ticker, bar_date, source_id, COALESCE(adj_close, close) AS price
        FROM fact_price_ohlcv
        WHERE ticker IN ({ticker_placeholders})
          AND source_id IN ({source_placeholders})
          AND COALESCE(adj_close, close) > 0
          {end_date_clause}
        ORDER BY ticker, source_id, bar_date
        """,
        params,
    ).fetchall()
    by_ticker_source: dict[tuple[str, str], list[tuple[date, float]]] = {}
    for row in rows:
        item_date = parse_date(row["bar_date"])
        price = to_float(row["price"])
        if item_date is None or price is None or price <= 0:
            continue
        ticker = str(row["ticker"] or "").upper()
        source_id = str(row["source_id"] or "").lower()
        by_ticker_source.setdefault((ticker, source_id), []).append((item_date, price))

    selected: dict[str, tuple[str, list[tuple[date, float]]]] = {}
    for ticker in tickers:
        for source_id in source_priority:
            series = by_ticker_source.get((ticker, source_id))
            if series:
                selected[ticker] = (source_id, series)
                break
    return selected


def estimate_round_trip_cost(position_usd: float, adv_usd: float | None, annual_vol: float | None) -> float:
    if adv_usd is None or adv_usd <= 0:
        return 0.02
    daily_vol = max(0.0, (annual_vol or 0.0) / math.sqrt(252.0))
    participation = min(max(position_usd, 0.0) / adv_usd, 0.30)
    temp_impact = 0.142 * daily_vol * math.sqrt(participation)
    spread_cost = 0.002 if adv_usd > 2_000_000 else 0.004
    return max(0.0, min(0.20, 2.0 * (temp_impact + spread_cost)))


def entry_index(series: list[tuple[date, float]], asof_date: date) -> int | None:
    idx: int | None = None
    for pos, (bar_date, _) in enumerate(series):
        if bar_date <= asof_date:
            idx = pos
        else:
            break
    return idx


def build_backtest_rows(
    score_rows: list[dict[str, Any]],
    price_series: dict[str, tuple[str, list[tuple[date, float]]]],
    *,
    asof: str,
    horizons: list[int],
    position_usd: float,
    training_label_end_max: date | None = None,
) -> list[dict[str, Any]]:
    asof_date = parse_date(asof)
    if asof_date is None:
        raise ValueError(f"Invalid asof date: {asof}")
    out: list[dict[str, Any]] = []
    market_dates = sorted({bar_date for _, series in price_series.values() for bar_date, _ in series})
    market_date_index = {bar_date: idx for idx, bar_date in enumerate(market_dates)}
    price_maps = {ticker: {bar_date: price for bar_date, price in series} for ticker, (_, series) in price_series.items()}
    for row in score_rows:
        ticker = str(row["ticker"] or "").upper()
        source_id, series = price_series.get(ticker, ("", []))
        idx = entry_index(series, asof_date) if series else None
        composite_score = first_float(row.get("composite_score"))
        raw_composite_score = first_float(row.get("raw_composite_score"), default=composite_score)
        composite_percentile = first_float_or_none(row.get("composite_percentile"))
        round_trip_cost = estimate_round_trip_cost(
            position_usd,
            to_float(row.get("avg_dollar_volume_60d")),
            to_float(row.get("realized_vol_60d")),
        )
        item = {
            "asof_date": asof,
            "scoring_model_version": row.get("scoring_model_version") or "",
            "ticker": ticker,
            "company_name": row.get("company_name") or "",
            "subsector": row.get("subsector") or "",
            "rank": value_or_blank(row, "rank"),
            "classification": row.get("classification") or "",
            "decision_bucket": row.get("decision_bucket") or "",
            "entry_status": row.get("entry_status") or "",
            "final_investability_gate": value_or_blank(row, "final_investability_gate"),
            "portfolio_candidate_gate": value_or_blank(row, "portfolio_candidate_gate"),
            "calibration_cohort": row.get("calibration_cohort") or "",
            "calibration_eligible_flag": value_or_blank(row, "calibration_eligible_flag"),
            "research_calibration_input_eligible_flag": value_or_blank(
                row, "research_calibration_input_eligible_flag"
            ),
            "research_calibration_status": row.get("research_calibration_status") or "",
            "research_calibration_reason": row.get("research_calibration_reason") or "",
            "calibration_sample_role": row.get("calibration_sample_role") or "",
            "stage11_calibration_input_eligible_flag": value_or_blank(
                row, "stage11_calibration_input_eligible_flag"
            ),
            "stage11_calibration_input_reason": row.get("stage11_calibration_input_reason") or "",
            "stage11_calibration_panel_source": row.get("stage11_calibration_panel_source") or "",
            "survivorship_corrected_panel_flag": value_or_blank(row, "survivorship_corrected_panel_flag"),
            "composite_score": composite_score,
            "raw_composite_score": raw_composite_score,
            "ic_tilted_composite_score": value_or_blank(row, "ic_tilted_composite_score"),
            "ic_tilted_composite_delta": value_or_blank(row, "ic_tilted_composite_delta"),
            "ic_tilted_composite_coverage": value_or_blank(row, "ic_tilted_composite_coverage"),
            "ic_tilted_composite_active_weight": value_or_blank(row, "ic_tilted_composite_active_weight"),
            "ic_tilted_composite_payload_json": row.get("ic_tilted_composite_payload_json") or "",
            "composite_percentile": composite_percentile,
            "cohort_percentile": value_or_blank(row, "cohort_percentile"),
            "safe_core_score": value_or_blank(row, "safe_core_score"),
            "safe_core_percentile": value_or_blank(row, "safe_core_percentile"),
            "safe_core_cohort_percentile": value_or_blank(row, "safe_core_cohort_percentile"),
            "safe_core_rank": value_or_blank(row, "safe_core_rank"),
            "safe_core_status": row.get("safe_core_status") or "",
            "safe_core_reason": row.get("safe_core_reason") or "",
            "passed_safe_core_gate": value_or_blank(row, "passed_safe_core_gate"),
            "safe_core_model_version": row.get("safe_core_model_version") or "",
            "legacy_all_gates_gate": value_or_blank(row, "legacy_all_gates_gate"),
            "legacy_gate_misses": row.get("legacy_gate_misses") or "",
            "tier1_safety_status": row.get("tier1_safety_status") or "",
            "tier1_safety_reason": row.get("tier1_safety_reason") or "",
            "passed_tier1_safety_gate": value_or_blank(row, "passed_tier1_safety_gate"),
            "fundamental_quality_score": value_or_blank(row, "fundamental_quality_score"),
            "durable_growth_score": value_or_blank(row, "durable_growth_score"),
            "durable_growth_score_legacy": value_or_blank(row, "durable_growth_score_legacy"),
            "durable_growth_alpha_score": value_or_blank(row, "durable_growth_alpha_score"),
            "durable_growth_growth_score": value_or_blank(row, "durable_growth_growth_score"),
            "durable_growth_quality_score": value_or_blank(row, "durable_growth_quality_score"),
            "durable_growth_efficiency_score": value_or_blank(row, "durable_growth_efficiency_score"),
            "durable_growth_capital_discipline_score": value_or_blank(row, "durable_growth_capital_discipline_score"),
            "durable_growth_evidence_quality_score": value_or_blank(row, "durable_growth_evidence_quality_score"),
            "fda_product_score": value_or_blank(row, "fda_product_score"),
            "fda_product_score_legacy": value_or_blank(row, "fda_product_score_legacy"),
            "fda_alpha_score": value_or_blank(row, "fda_alpha_score"),
            "fda_safety_score": value_or_blank(row, "fda_safety_score"),
            "fda_clearance_velocity_raw": value_or_blank(row, "fda_clearance_velocity_raw"),
            "fda_clearance_velocity_score": value_or_blank(row, "fda_clearance_velocity_score"),
            "fda_clearance_acceleration_raw": value_or_blank(row, "fda_clearance_acceleration_raw"),
            "fda_clearance_acceleration_score": value_or_blank(row, "fda_clearance_acceleration_score"),
            "fda_evidence_quality_score": value_or_blank(row, "fda_evidence_quality_score"),
            "fda_safety_breadth_adjusted_score": value_or_blank(row, "fda_safety_breadth_adjusted_score"),
            "fda_safety_product_family_adjusted_score": value_or_blank(
                row, "fda_safety_product_family_adjusted_score"
            ),
            "reimbursement_score": value_or_blank(row, "reimbursement_score"),
            "valuation_score": value_or_blank(row, "valuation_score"),
            "technical_entry_score": value_or_blank(row, "technical_entry_score"),
            "sentiment_catalyst_score": value_or_blank(row, "sentiment_catalyst_score"),
            "value_trap_score": value_or_blank(row, "value_trap_score"),
            "technical_trend_quality_score": value_or_blank(row, "technical_trend_quality_score"),
            "technical_relative_strength_score": value_or_blank(row, "technical_relative_strength_score"),
            "technical_liquidity_score": value_or_blank(row, "technical_liquidity_score"),
            "technical_volume_breakout_score": value_or_blank(row, "technical_volume_breakout_score"),
            "technical_volatility_risk_score": value_or_blank(row, "technical_volatility_risk_score"),
            "technical_setup_score": value_or_blank(row, "technical_setup_score"),
            "technical_core_score": value_or_blank(row, "technical_core_score"),
            "technical_alpha_score": value_or_blank(row, "technical_alpha_score"),
            "technical_pullback_score": value_or_blank(row, "technical_pullback_score"),
            "technical_overextension_score": value_or_blank(row, "technical_overextension_score"),
            "technical_breakdown_flag": value_or_blank(row, "technical_breakdown_flag"),
            "technical_liquidity_gate_flag": value_or_blank(row, "technical_liquidity_gate_flag"),
            "momentum_12_1": value_or_blank(row, "momentum_12_1"),
            "realized_vol_60d": value_or_blank(row, "realized_vol_60d"),
            "round_trip_cost_estimate": round(round_trip_cost, 6),
            "technical_signal_mode": row.get("technical_signal_mode") or "",
            "technical_signal_direction": row.get("technical_signal_direction") or "",
            "technical_signal_reliability": value_or_blank(row, "technical_signal_reliability"),
            "technical_score_source": row.get("technical_score_source") or "",
            "durable_growth_signal_mode": row.get("durable_growth_signal_mode") or "",
            "durable_growth_signal_direction": row.get("durable_growth_signal_direction") or "",
            "durable_growth_signal_reliability": value_or_blank(row, "durable_growth_signal_reliability"),
            "durable_growth_score_source": row.get("durable_growth_score_source") or "",
            "durable_growth_gate_mode": row.get("durable_growth_gate_mode") or "",
            "durable_growth_policy_reason": row.get("durable_growth_policy_reason") or "",
            "durable_growth_gate_excluded": value_or_blank(row, "durable_growth_gate_excluded"),
            "durable_growth_component_weight": value_or_blank(row, "durable_growth_component_weight"),
            "durable_growth_repair_flag": value_or_blank(row, "durable_growth_repair_flag"),
            "durable_growth_repair_reason": row.get("durable_growth_repair_reason") or "",
            "durable_growth_validation_status": row.get("durable_growth_validation_status") or "",
            "durable_growth_validation_reason": row.get("durable_growth_validation_reason") or "",
            "durable_growth_production_state": row.get("durable_growth_production_state") or "",
            "quality_value_interaction_score": value_or_blank(row, "quality_value_interaction_score"),
            "fda_technical_interaction_score": value_or_blank(row, "fda_technical_interaction_score"),
            "borrow_availability_score": value_or_blank(row, "borrow_availability_score"),
            "borrow_fee_score": value_or_blank(row, "borrow_fee_score"),
            "borrow_squeeze_risk_score": value_or_blank(row, "borrow_squeeze_risk_score"),
            "borrow_pressure_score": value_or_blank(row, "borrow_pressure_score"),
            "short_interest_score": value_or_blank(row, "short_interest_score"),
            "short_pressure_score": value_or_blank(row, "short_pressure_score"),
            "short_squeeze_score": value_or_blank(row, "short_squeeze_score"),
            "short_volume_score": value_or_blank(row, "short_volume_score"),
            "short_interest_velocity_score": value_or_blank(row, "short_interest_velocity_score"),
            "days_to_cover_score": value_or_blank(row, "days_to_cover_score"),
            "institutional_accumulation_score": value_or_blank(row, "institutional_accumulation_score"),
            "institutional_crowding_score": value_or_blank(row, "institutional_crowding_score"),
            "institutional_breadth_score": value_or_blank(row, "institutional_breadth_score"),
            "insider_net_buy_score": value_or_blank(row, "insider_net_buy_score"),
            "insider_cluster_buy_score": value_or_blank(row, "insider_cluster_buy_score"),
            "insider_selling_pressure_score": value_or_blank(row, "insider_selling_pressure_score"),
            "insider_activity_score": value_or_blank(row, "insider_activity_score"),
            "pullback_candidate_tag": value_or_blank(row, "pullback_candidate_tag"),
            "pullback_candidate_template_id": row.get("pullback_candidate_template_id") or "",
            "rank_bucket": rank_bucket(composite_percentile),
            "entry_price_date": "",
            "entry_price": "",
            "price_source_id": source_id,
        }
        if idx is not None:
            entry_date, entry_price = series[idx]
            item["entry_price_date"] = entry_date.isoformat()
            item["entry_price"] = round(entry_price, 6)
            calendar_idx = market_date_index.get(entry_date)
            ticker_price_map = price_maps.get(ticker, {})
            for horizon in horizons:
                target_date = (
                    market_dates[calendar_idx + horizon]
                    if calendar_idx is not None and calendar_idx + horizon < len(market_dates)
                    else None
                )
                label_date_allowed = target_date is not None and (
                    training_label_end_max is None or target_date <= training_label_end_max
                )
                target_price = (
                    ticker_price_map.get(target_date)
                    if label_date_allowed and target_date is not None
                    else None
                )
                if label_date_allowed and target_date is not None and target_price is not None:
                    forward_return = (target_price - entry_price) / entry_price
                    item[f"forward_date_{horizon}d"] = target_date.isoformat()
                    item[f"forward_return_{horizon}d"] = round(forward_return, 6)
                    item[f"net_forward_return_{horizon}d"] = round(forward_return - round_trip_cost, 6)
                else:
                    item[f"forward_date_{horizon}d"] = ""
                    item[f"forward_return_{horizon}d"] = ""
                    item[f"net_forward_return_{horizon}d"] = ""
        else:
            for horizon in horizons:
                item[f"forward_date_{horizon}d"] = ""
                item[f"forward_return_{horizon}d"] = ""
                item[f"net_forward_return_{horizon}d"] = ""
        out.append(item)
    return out


def summarize(rows: list[dict[str, Any]], *, horizons: list[int]) -> list[dict[str, Any]]:
    summary: list[dict[str, Any]] = []
    group_specs = [
        ("classification", sorted({str(row["classification"]) for row in rows})),
        ("entry_status", sorted({str(row["entry_status"]) for row in rows})),
        ("rank_bucket", sorted({str(row["rank_bucket"]) for row in rows})),
    ]
    for group_name, group_values in group_specs:
        for group_value in group_values:
            group_rows = [row for row in rows if str(row.get(group_name)) == group_value]
            for horizon in horizons:
                values = [
                    float(row[f"forward_return_{horizon}d"])
                    for row in group_rows
                    if value_present(row.get(f"forward_return_{horizon}d"))
                ]
                summary.append(
                    {
                        "group_type": group_name,
                        "group_value": group_value,
                        "horizon_days": horizon,
                        "count": len(values),
                        "mean_forward_return": round(mean(values), 6) if values else "",
                        "median_forward_return": round(median(values), 6) if values else "",
                        "hit_rate": round(sum(1 for value in values if value > 0) / len(values), 4) if values else "",
                    }
                )
    return summary


def write_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore", lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def dated_output_dir(base_output_dir: Path, asof: str) -> Path:
    return base_output_dir if base_output_dir.name == asof else base_output_dir / asof


def main() -> None:
    configure_utc_logging()
    args = parse_args()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    base_dir = config_path.parent
    db_path = args.db.expanduser().resolve() if args.db else resolve_path(cfg_get(config, "paths.database_path"), base_dir=base_dir)
    horizons = [int(item.strip()) for item in str(args.horizons or "30,60,120").split(",") if item.strip()]
    if not horizons or any(horizon <= 0 for horizon in horizons):
        raise ValueError("--horizons must contain positive integers")
    position_usd = float(cfg_get(config, "calibration.transaction_cost.position_usd", 50_000.0))
    training_label_end_raw = str(args.training_label_end_max or "").strip()
    if args.stage11_eligible_only and not training_label_end_raw:
        training_label_end_raw = str(
            cfg_get(
                config,
                "calibration.training_label_end_max",
                cfg_get(config, "calibration.dev_window_end", ""),
            )
            or ""
        ).strip()
    training_label_end_max = parse_date(training_label_end_raw)
    if training_label_end_raw and training_label_end_max is None:
        raise ValueError(f"Invalid --training-label-end-max date: {training_label_end_raw!r}")
    if args.stage11_eligible_only and training_label_end_max is None:
        raise RuntimeError("Stage 11 backtests require calibration.training_label_end_max")

    with connect(db_path, timeout_sec=float(cfg_get(config, "runtime.sqlite_timeout_sec", 30.0))) as conn:
        init_db_read_tolerant(conn)
        asofs = resolve_score_asofs(conn, args)
        if not asofs:
            raise RuntimeError("No med_device_daily_scores as-of dates matched the requested backtest range.")
        single_asof = len(asofs) == 1
        output_csv = args.output_csv.expanduser().resolve() if args.output_csv else None
        if output_csv is None and single_asof:
            output_csv = (
                dated_output_dir(
                    resolve_path(
                        cfg_get(config, "scoring.review_pack_dir", "../output/med_devices_reports/score_review_pack"),
                        base_dir=base_dir,
                    ),
                    asofs[0],
                )
                / "med_device_score_backtest.csv"
            )
        if output_csv is None:
            output_csv = resolve_path(
                cfg_get(config, "scoring.backtest_output_csv", "../output/med_devices_reports/med_device_score_backtest.csv"),
                base_dir=base_dir,
            )
        score_rows_by_asof = {asof: load_scores(conn, asof=asof) for asof in asofs}
        empty_asofs = [asof for asof, rows_for_asof in score_rows_by_asof.items() if not rows_for_asof]
        if empty_asofs:
            raise RuntimeError(f"No score rows found for requested as-of dates: {','.join(empty_asofs)}")
        if args.stage11_eligible_only:
            requested_asof_count = len(score_rows_by_asof)
            score_rows_by_asof, empty_eligible_asofs = filter_stage11_eligible_rows(score_rows_by_asof)
            if len(empty_eligible_asofs) == requested_asof_count:
                raise RuntimeError(
                    "No Stage 11 eligible score rows found for any requested as-of date: "
                    + ",".join(empty_eligible_asofs)
                )
            if empty_eligible_asofs:
                LOGGER.warning(
                    "Excluded %d as-of dates with zero Stage 11 eligible rows: %s",
                    len(empty_eligible_asofs),
                    ",".join(empty_eligible_asofs),
                )
                asofs = [asof for asof in asofs if asof in score_rows_by_asof]
        source_priority = calibration_market_sources(config)
        tickers = sorted(
            {
                str(row["ticker"] or "").upper()
                for rows_for_asof in score_rows_by_asof.values()
                for row in rows_for_asof
            }
        )
        series = load_price_series(
            conn,
            tickers=tickers,
            source_priority=source_priority,
            end_date=training_label_end_max,
        )
        rows: list[dict[str, Any]] = []
        for asof in asofs:
            rows.extend(
                build_backtest_rows(
                    score_rows_by_asof[asof],
                    series,
                    asof=asof,
                    horizons=horizons,
                    position_usd=position_usd,
                    training_label_end_max=training_label_end_max,
                )
            )
        fieldnames = [
            *BASE_FIELDS,
            *[
                field
                for horizon in horizons
                for field in (f"forward_date_{horizon}d", f"forward_return_{horizon}d", f"net_forward_return_{horizon}d")
            ],
        ]
        write_csv(output_csv, rows, fieldnames)
        summary_rows = summarize(rows, horizons=horizons)
        write_csv(
            output_csv.with_name(output_csv.stem + "_summary" + output_csv.suffix),
            summary_rows,
            ["group_type", "group_value", "horizon_days", "count", "mean_forward_return", "median_forward_return", "hit_rate"],
        )
        available = {
            horizon: sum(1 for row in rows if value_present(row.get(f"forward_return_{horizon}d")))
            for horizon in horizons
        }
        LOGGER.info(
            "Backtest complete: output=%s asofs=%d rows=%d forward_counts=%s training_label_end_max=%s",
            output_csv,
            len(asofs),
            len(rows),
            available,
            training_label_end_max.isoformat() if training_label_end_max is not None else "",
        )


if __name__ == "__main__":
    raise SystemExit(main())
