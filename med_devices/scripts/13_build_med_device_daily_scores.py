#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import logging
import math
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from med_devices.core.config import DEFAULT_NEUTRAL_SCORE, cfg_get, load_yaml, resolve_path  # noqa: E402
from med_devices.core.db import connect, finish_run, init_db, quote_identifier, start_run, utc_now  # noqa: E402
from med_devices.core.fda_states import MANUAL_FDA_REVIEW_STATES  # noqa: E402
from med_devices.core.logging_utils import configure_utc_logging  # noqa: E402
from med_devices.core.text_norm import normalize_ticker  # noqa: E402


LOGGER = logging.getLogger("build_med_device_daily_scores")
DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
DEFAULT_WEIGHTS = {
    "fundamental_quality": 0.25,
    "durable_growth": 0.15,
    "fda_product": 0.15,
    "reimbursement": 0.10,
    "valuation": 0.20,
    "technical_entry": 0.10,
    "sentiment_catalyst": 0.05,
}
WEIGHT_EPSILON = 1e-9
TECHNICAL_GATE_HARD_POSITIVE = "hard_positive"
TECHNICAL_GATE_OVERLAY_ONLY = "overlay_only"
TECHNICAL_GATE_BREAKDOWN_VETO_ONLY = "breakdown_veto_only"
TECHNICAL_GATE_DISABLED = "disabled"
TECHNICAL_GATE_MODES = {
    TECHNICAL_GATE_HARD_POSITIVE,
    TECHNICAL_GATE_OVERLAY_ONLY,
    TECHNICAL_GATE_BREAKDOWN_VETO_ONLY,
    TECHNICAL_GATE_DISABLED,
}
FDA_GATE_HARD_POSITIVE = "hard_positive"
FDA_GATE_RISK_VETO_ONLY = "risk_veto_only"
FDA_GATE_OVERLAY_ONLY = "overlay_only"
FDA_GATE_DISABLED = "disabled"
FDA_GATE_MODES = {
    FDA_GATE_HARD_POSITIVE,
    FDA_GATE_RISK_VETO_ONLY,
    FDA_GATE_OVERLAY_ONLY,
    FDA_GATE_DISABLED,
}
DURABLE_GROWTH_MODE_POSITIVE_ALPHA = "positive_alpha"
DURABLE_GROWTH_MODE_INVERSE_ALPHA = "inverse_alpha"
DURABLE_GROWTH_MODE_NEUTRAL_OVERLAY = "neutral_overlay"
DURABLE_GROWTH_MODE_REPAIR_DATA = "repair_data"
DURABLE_GROWTH_MODE_LEGACY_PASSTHROUGH = "legacy_passthrough"
DURABLE_GROWTH_MODE_DISABLED = "disabled"
DURABLE_GROWTH_MODES = {
    DURABLE_GROWTH_MODE_POSITIVE_ALPHA,
    DURABLE_GROWTH_MODE_INVERSE_ALPHA,
    DURABLE_GROWTH_MODE_NEUTRAL_OVERLAY,
    DURABLE_GROWTH_MODE_REPAIR_DATA,
    DURABLE_GROWTH_MODE_LEGACY_PASSTHROUGH,
    DURABLE_GROWTH_MODE_DISABLED,
}
DURABLE_GROWTH_GATE_HARD_POSITIVE = "hard_positive"
DURABLE_GROWTH_GATE_ALPHA_ONLY = "alpha_only"
DURABLE_GROWTH_GATE_OVERLAY_ONLY = "overlay_only"
DURABLE_GROWTH_GATE_REPAIR_DATA = "repair_data"
DURABLE_GROWTH_GATE_DISABLED = "disabled"
DURABLE_GROWTH_GATE_MODES = {
    DURABLE_GROWTH_GATE_HARD_POSITIVE,
    DURABLE_GROWTH_GATE_ALPHA_ONLY,
    DURABLE_GROWTH_GATE_OVERLAY_ONLY,
    DURABLE_GROWTH_GATE_REPAIR_DATA,
    DURABLE_GROWTH_GATE_DISABLED,
}
DURABLE_GROWTH_PRODUCTION_PROMOTED = "promoted"
DURABLE_GROWTH_PRODUCTION_RESEARCH_ONLY = "research_only"
DURABLE_GROWTH_PRODUCTION_LEGACY_CHAMPION = "legacy_champion"
DURABLE_GROWTH_PRODUCTION_DISABLED = "disabled"
DURABLE_GROWTH_PRODUCTION_STATES = {
    DURABLE_GROWTH_PRODUCTION_PROMOTED,
    DURABLE_GROWTH_PRODUCTION_RESEARCH_ONLY,
    DURABLE_GROWTH_PRODUCTION_LEGACY_CHAMPION,
    DURABLE_GROWTH_PRODUCTION_DISABLED,
}
DURABLE_GROWTH_PROXY_INPUT_FIELDS = (
    "revenue_yoy_growth",
    "quarterly_revenue_surprise_yoy",
    "gross_margin_trend_3y",
    "gross_margin_ttm",
    "operating_margin_ttm",
    "fcf_margin_ttm",
    "free_cash_flow_margin_ttm",
    "rd_growth_yoy",
    "rd_to_revenue_ttm",
    "annualized_research_and_development",
    "research_and_development_ttm",
    "shares_yoy_growth",
    "net_debt_to_revenue",
    "financial_runway_years",
    "data_confidence_score",
)
CALIBRATION_STATUS_PRODUCTION_ELIGIBLE = "production_eligible"
CALIBRATION_STATUS_RESTRICTED_RESEARCH_ONLY = "restricted_research_only"
CALIBRATION_STATUS_EXCLUDED_FROM_TIER1 = "excluded_from_tier1"
CALIBRATION_STATUSES = {
    CALIBRATION_STATUS_PRODUCTION_ELIGIBLE,
    CALIBRATION_STATUS_RESTRICTED_RESEARCH_ONLY,
    CALIBRATION_STATUS_EXCLUDED_FROM_TIER1,
}
OPTIONAL_DAILY_SCORE_COLUMNS = {
    "calibration_status": "TEXT DEFAULT 'production_eligible'",
    "calibration_status_reason": "TEXT DEFAULT ''",
    "cohort_score_template_id": "TEXT DEFAULT ''",
    "cohort_score_template_spec": "TEXT DEFAULT ''",
    "durable_growth_score_legacy": "REAL DEFAULT 50.0",
    "durable_growth_alpha_score": "REAL DEFAULT 50.0",
    "durable_growth_growth_score": "REAL DEFAULT 50.0",
    "durable_growth_quality_score": "REAL DEFAULT 50.0",
    "durable_growth_efficiency_score": "REAL DEFAULT 50.0",
    "durable_growth_capital_discipline_score": "REAL DEFAULT 50.0",
    "durable_growth_evidence_quality_score": "REAL DEFAULT 0.0",
    "durable_growth_component_count": "INTEGER DEFAULT 0",
    "durable_growth_signal_mode": "TEXT DEFAULT ''",
    "durable_growth_signal_direction": "TEXT DEFAULT ''",
    "durable_growth_signal_reliability": "REAL DEFAULT 0.0",
    "durable_growth_score_source": "TEXT DEFAULT ''",
    "durable_growth_gate_mode": "TEXT DEFAULT ''",
    "durable_growth_policy_reason": "TEXT DEFAULT ''",
    "durable_growth_gate_excluded": "INTEGER DEFAULT 0",
    "durable_growth_component_weight": "REAL DEFAULT 0.0",
    "durable_growth_repair_flag": "INTEGER DEFAULT 0",
    "durable_growth_repair_reason": "TEXT DEFAULT ''",
    "durable_growth_validation_status": "TEXT DEFAULT ''",
    "durable_growth_validation_reason": "TEXT DEFAULT ''",
    "durable_growth_production_state": "TEXT DEFAULT ''",
    "fda_product_score_legacy": "REAL DEFAULT 0.0",
    "fda_alpha_score": "REAL DEFAULT 0.0",
    "fda_safety_score": "REAL DEFAULT 0.0",
    "fda_clearance_velocity_score": "REAL DEFAULT 0.0",
    "fda_evidence_quality_score": "REAL DEFAULT 0.0",
    "fda_event_risk_score": "REAL DEFAULT 0.0",
    "fda_signal_mode": "TEXT DEFAULT ''",
    "fda_signal_direction": "TEXT DEFAULT ''",
    "fda_signal_reliability": "REAL DEFAULT 0.0",
    "fda_score_source": "TEXT DEFAULT ''",
    "fda_gate_mode": "TEXT DEFAULT ''",
    "fda_policy_reason": "TEXT DEFAULT ''",
    "fda_gate_excluded": "INTEGER DEFAULT 0",
    "fda_component_weight": "REAL DEFAULT 0.0",
    "technical_gate_mode": "TEXT DEFAULT ''",
    "technical_overlay_status": "TEXT DEFAULT ''",
    "technical_policy_reason": "TEXT DEFAULT ''",
    "technical_gate_excluded": "INTEGER DEFAULT 0",
    "technical_component_weight": "REAL DEFAULT 0.0",
    "passed_technical_breakdown_veto": "INTEGER DEFAULT 1",
    "pullback_candidate_tag": "INTEGER DEFAULT 0",
    "pullback_candidate_reason": "TEXT DEFAULT ''",
    "pullback_candidate_template_id": "TEXT DEFAULT ''",
    "technical_trend_quality_score": "REAL DEFAULT 0.0",
    "technical_relative_strength_score": "REAL DEFAULT 0.0",
    "technical_liquidity_score": "REAL DEFAULT 0.0",
    "technical_volume_breakout_score": "REAL DEFAULT 0.0",
    "technical_volatility_risk_score": "REAL DEFAULT 0.0",
    "technical_setup_score": "REAL DEFAULT 0.0",
    "technical_core_score": "REAL DEFAULT 0.0",
    "technical_alpha_score": "REAL DEFAULT 0.0",
    "technical_pullback_score": "REAL DEFAULT 0.0",
    "technical_overextension_score": "REAL DEFAULT 0.0",
    "technical_breakdown_flag": "INTEGER DEFAULT 0",
    "technical_liquidity_gate_flag": "INTEGER DEFAULT 0",
    "technical_signal_mode": "TEXT DEFAULT ''",
    "technical_signal_direction": "TEXT DEFAULT ''",
    "technical_signal_reliability": "REAL DEFAULT 0.0",
    "technical_score_source": "TEXT DEFAULT ''",
    "technical_entry_status_score": "REAL DEFAULT 0.0",
    "technical_entry_status_score_source": "TEXT DEFAULT ''",
}
ALLOWED_FEATURE_TABLES = {
    "feature_financial_valuation",
    "feature_fda_product_risk",
    "feature_reimbursement",
    "feature_technical_entry",
    "feature_durable_growth",
    "feature_sentiment_catalyst",
}
FIELDNAMES = [
    "asof_date",
    "scoring_model_version",
    "rank",
    "company_id",
    "ticker",
    "company_name",
    "subsector",
    "composite_score",
    "raw_composite_score",
    "composite_percentile",
    "calibration_cohort",
    "calibration_status",
    "calibration_status_reason",
    "cohort_score_template_id",
    "cohort_score_template_spec",
    "cohort_percentile",
    "fundamental_quality_score",
    "durable_growth_score",
    "durable_growth_score_legacy",
    "durable_growth_alpha_score",
    "durable_growth_growth_score",
    "durable_growth_quality_score",
    "durable_growth_efficiency_score",
    "durable_growth_capital_discipline_score",
    "durable_growth_evidence_quality_score",
    "durable_growth_component_count",
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
    "fda_product_score",
    "fda_product_score_legacy",
    "fda_alpha_score",
    "fda_safety_score",
    "fda_clearance_velocity_score",
    "fda_evidence_quality_score",
    "fda_event_risk_score",
    "fda_signal_mode",
    "fda_signal_direction",
    "fda_signal_reliability",
    "fda_score_source",
    "fda_gate_mode",
    "fda_policy_reason",
    "fda_gate_excluded",
    "fda_component_weight",
    "fda_data_available",
    "reimbursement_score",
    "reimbursement_status",
    "direct_code_evidence",
    "payment_rate_evidence",
    "coverage_policy_evidence",
    "procedure_bundled_flag",
    "capital_equipment_flag",
    "diagnostics_lab_flag",
    "unknown_reimbursement_flag",
    "valuation_score",
    "technical_entry_score",
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
    "technical_signal_mode",
    "technical_signal_direction",
    "technical_signal_reliability",
    "technical_score_source",
    "technical_entry_status_score",
    "technical_entry_status_score_source",
    "sentiment_catalyst_score",
    "value_trap_score",
    "data_completeness_score",
    "live_component_count",
    "composite_score_delta",
    "rank_delta",
    "classification_change",
    "hard_red_flag",
    "hard_red_flag_reasons",
    "classification",
    "decision_bucket",
    "entry_status",
    "technical_gate_mode",
    "technical_overlay_status",
    "technical_policy_reason",
    "technical_gate_excluded",
    "technical_component_weight",
    "pullback_candidate_tag",
    "pullback_candidate_reason",
    "pullback_candidate_template_id",
    "gate_status",
    "review_reason",
    "failed_gates",
    "classification_reason",
    "fda_review_state",
    "market_cap",
    "current_shares_outstanding",
    "diluted_weighted_average_shares",
    "basic_weighted_average_shares",
    "shares_source_concept",
    "shares_source_form",
    "shares_source_period",
    "market_cap_validated_flag",
    "avg_dollar_volume_60d",
    "liquidity_score",
    "capacity_bucket",
    "min_position_size_feasible",
    "max_position_size_feasible",
    "passed_raw_score_gate",
    "passed_fundamental_gate",
    "passed_growth_gate",
    "passed_fda_gate",
    "passed_reimbursement_gate",
    "passed_valuation_gate",
    "passed_technical_gate",
    "passed_technical_breakdown_veto",
    "passed_value_trap_gate",
    "passed_data_quality_gate",
    "passed_liquidity_gate",
    "passed_fda_manual_review_gate",
    "final_investability_gate",
    "top_positive_drivers",
    "top_negative_drivers",
]


@dataclass
class ScoreRow:
    asof_date: str
    scoring_model_version: str
    rank: int
    company_id: int
    ticker: str
    company_name: str
    subsector: str
    composite_score: float = 0.0
    raw_composite_score: float = 0.0
    composite_percentile: float = 0.0
    calibration_cohort: str = ""
    calibration_status: str = CALIBRATION_STATUS_PRODUCTION_ELIGIBLE
    calibration_status_reason: str = ""
    cohort_score_template_id: str = ""
    cohort_score_template_spec: str = ""
    cohort_percentile: float = 50.0
    fundamental_quality_score: float = 0.0
    durable_growth_score: float = 50.0
    durable_growth_score_legacy: float = 50.0
    durable_growth_alpha_score: float = 50.0
    durable_growth_growth_score: float = 50.0
    durable_growth_quality_score: float = 50.0
    durable_growth_efficiency_score: float = 50.0
    durable_growth_capital_discipline_score: float = 50.0
    durable_growth_evidence_quality_score: float = 0.0
    durable_growth_component_count: int = 0
    durable_growth_signal_mode: str = DURABLE_GROWTH_MODE_NEUTRAL_OVERLAY
    durable_growth_signal_direction: str = "neutral"
    durable_growth_signal_reliability: float = 0.0
    durable_growth_score_source: str = "neutral"
    durable_growth_gate_mode: str = DURABLE_GROWTH_GATE_OVERLAY_ONLY
    durable_growth_policy_reason: str = ""
    durable_growth_gate_excluded: int = 1
    durable_growth_component_weight: float = DEFAULT_WEIGHTS["durable_growth"]
    durable_growth_repair_flag: int = 0
    durable_growth_repair_reason: str = ""
    durable_growth_validation_status: str = DURABLE_GROWTH_PRODUCTION_PROMOTED
    durable_growth_validation_reason: str = ""
    durable_growth_production_state: str = DURABLE_GROWTH_PRODUCTION_PROMOTED
    fda_product_score: float = 50.0
    fda_product_score_legacy: float = 50.0
    fda_alpha_score: float = 50.0
    fda_safety_score: float = 50.0
    fda_clearance_velocity_score: float = 50.0
    fda_evidence_quality_score: float = 50.0
    fda_event_risk_score: float = 0.0
    fda_signal_mode: str = ""
    fda_signal_direction: str = ""
    fda_signal_reliability: float = 0.0
    fda_score_source: str = "fda_product_score"
    fda_gate_mode: str = FDA_GATE_HARD_POSITIVE
    fda_policy_reason: str = ""
    fda_gate_excluded: int = 0
    fda_component_weight: float = DEFAULT_WEIGHTS["fda_product"]
    fda_data_available: int = 0
    reimbursement_score: float = 50.0
    reimbursement_status: str = "unknown"
    direct_code_evidence: int = 0
    payment_rate_evidence: int = 0
    coverage_policy_evidence: int = 0
    procedure_bundled_flag: int = 0
    capital_equipment_flag: int = 0
    diagnostics_lab_flag: int = 0
    unknown_reimbursement_flag: int = 1
    valuation_score: float = 0.0
    technical_entry_score: float = 50.0
    technical_trend_quality_score: float = 50.0
    technical_relative_strength_score: float = 50.0
    technical_liquidity_score: float = 50.0
    technical_volume_breakout_score: float = 50.0
    technical_volatility_risk_score: float = 50.0
    technical_setup_score: float = 50.0
    technical_core_score: float = 50.0
    technical_alpha_score: float = 50.0
    technical_pullback_score: float = 50.0
    technical_overextension_score: float = 0.0
    technical_breakdown_flag: int = 0
    technical_liquidity_gate_flag: int = 0
    technical_signal_mode: str = ""
    technical_signal_direction: str = ""
    technical_signal_reliability: float = 0.0
    technical_score_source: str = "legacy_setup"
    technical_entry_status_score: float | None = None
    technical_entry_status_score_source: str = "legacy_setup"
    sentiment_catalyst_score: float = 50.0
    value_trap_score: float = 0.0
    data_completeness_score: float = 0.0
    live_component_count: int = 0
    composite_score_delta: float | None = None
    rank_delta: int | None = None
    classification_change: str = ""
    hard_red_flag: int = 0
    hard_red_flag_reasons: str = ""
    classification: str = "unclassified"
    decision_bucket: str = "unclassified"
    entry_status: str = "unclassified"
    technical_gate_mode: str = TECHNICAL_GATE_HARD_POSITIVE
    technical_overlay_status: str = ""
    technical_policy_reason: str = ""
    technical_gate_excluded: int = 0
    technical_component_weight: float = DEFAULT_WEIGHTS["technical_entry"]
    pullback_candidate_tag: int = 0
    pullback_candidate_reason: str = ""
    pullback_candidate_template_id: str = ""
    gate_status: str = "fail"
    review_reason: str = ""
    failed_gates: str = ""
    classification_reason: str = ""
    fda_review_state: str = ""
    market_cap: float | None = None
    current_shares_outstanding: float | None = None
    diluted_weighted_average_shares: float | None = None
    basic_weighted_average_shares: float | None = None
    shares_source_concept: str = ""
    shares_source_form: str = ""
    shares_source_period: str = ""
    market_cap_validated_flag: int = 0
    avg_dollar_volume_60d: float | None = None
    liquidity_score: float | None = None
    capacity_bucket: str = "unknown"
    min_position_size_feasible: float | None = None
    max_position_size_feasible: float | None = None
    passed_raw_score_gate: int = 0
    passed_fundamental_gate: int = 0
    passed_growth_gate: int = 0
    passed_fda_gate: int = 0
    passed_reimbursement_gate: int = 0
    passed_valuation_gate: int = 0
    passed_technical_gate: int = 0
    passed_technical_breakdown_veto: int = 1
    passed_value_trap_gate: int = 0
    passed_data_quality_gate: int = 0
    passed_liquidity_gate: int = 0
    passed_fda_manual_review_gate: int = 0
    final_investability_gate: int = 0
    top_positive_drivers: list[str] = field(default_factory=list)
    top_negative_drivers: list[str] = field(default_factory=list)
    durable_growth_proxy_available: bool = False
    sentiment_proxy_available: bool = False
    sentiment_proxy_source: str = ""
    sentiment_proxy_input: str = ""


@dataclass(frozen=True)
class SentimentProxy:
    score: float
    source: str
    input_name: str


@dataclass(frozen=True)
class DurableGrowthProxy:
    legacy_score: float
    growth_score: float
    quality_score: float
    efficiency_score: float
    capital_discipline_score: float
    evidence_quality_score: float
    component_count: int
    source: str
    payload: dict[str, Any]


@dataclass(frozen=True)
class DurableGrowthSelection:
    legacy_score: float
    alpha_score: float
    growth_score: float
    quality_score: float
    efficiency_score: float
    capital_discipline_score: float
    evidence_quality_score: float
    component_count: int
    signal_mode: str
    signal_direction: str
    signal_reliability: float
    score_source: str
    repair_flag: int
    repair_reason: str
    validation_status: str
    validation_reason: str
    production_state: str


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build med-device daily composite scores.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--output-csv", type=Path, default=None)
    parser.add_argument("--asof", type=str, default="")
    parser.add_argument("--tickers", type=str, default="")
    parser.add_argument("--max-tickers", type=int, default=0)
    return parser.parse_args()


def to_float(raw: object) -> float | None:
    try:
        value = float(str(raw).strip())
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    if not math.isfinite(value):
        return low
    return max(low, min(high, value))


def cfg_float(config: dict[str, Any], dotted_key: str, default: float) -> float:
    value = to_float(cfg_get(config, dotted_key, default))
    if value is None:
        raise ValueError(f"Config value must be numeric: {dotted_key}")
    return value


def cfg_bool(config: dict[str, Any], dotted_key: str, default: bool) -> bool:
    raw = cfg_get(config, dotted_key, default)
    text = str(raw).strip().lower()
    if text in {"1", "true", "yes", "y", "on"}:
        return True
    if text in {"0", "false", "no", "n", "off"}:
        return False
    return default


def component_neutral(config: dict[str, Any], component: str, legacy_key: str, default: float) -> float:
    nested_key = f"scoring.component_neutral_defaults.{component}"
    raw = cfg_get(config, nested_key, None)
    if raw is not None:
        value = to_float(raw)
        if value is None:
            raise ValueError(f"Config value must be numeric: {nested_key}")
        return value
    return cfg_float(config, legacy_key, default)


def score_or(raw: object, default: float) -> float:
    value = to_float(raw)
    return default if value is None else value


def parse_date(raw: object) -> datetime | None:
    text = str(raw or "").strip()[:10]
    if not text:
        return None
    try:
        return datetime.strptime(text, "%Y-%m-%d")
    except ValueError:
        return None


def latest_financial_asof(conn: Any) -> str:
    row = conn.execute("SELECT MAX(asof_date) AS asof_date FROM feature_financial_valuation").fetchone()
    asof = str(row["asof_date"] or "") if row is not None else ""
    if not asof:
        raise ValueError("No feature_financial_valuation rows found; run script 06 first.")
    return asof


def table_exists(conn: Any, table_name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ? LIMIT 1",
        (table_name,),
    ).fetchone()
    return row is not None


def parse_component_weights(
    raw: object,
    *,
    default_weights: dict[str, float],
    context: str,
) -> dict[str, float]:
    if raw is None:
        return dict(default_weights)
    if not isinstance(raw, dict):
        raise ValueError(f"{context} must be a mapping")
    unknown = sorted(set(str(key) for key in raw) - set(DEFAULT_WEIGHTS))
    if unknown:
        LOGGER.warning("Ignoring unknown composite scoring weight key(s) in %s: %s", context, ", ".join(unknown))
    out = dict(default_weights)
    for key, raw_value in raw.items():
        key_text = str(key)
        if key_text not in DEFAULT_WEIGHTS:
            continue
        value = to_float(raw_value)
        if value is None or value < 0:
            raise ValueError(f"Composite score weight must be non-negative numeric: {context}.{key_text}")
        out[key_text] = value
    total = sum(out.values())
    if abs(total - 1.0) > 0.0001:
        raise ValueError(f"Composite score weights must sum to 1.0 for {context}: {total:.6f}")
    return out


def load_weights(config: dict[str, Any]) -> dict[str, float]:
    return parse_component_weights(
        cfg_get(config, "scoring.composite_weights", DEFAULT_WEIGHTS),
        default_weights=DEFAULT_WEIGHTS,
        context="scoring.composite_weights",
    )


def load_financial_rows(conn: Any, *, asof: str, ticker_filter: set[str], max_tickers: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT *
        FROM feature_financial_valuation
        WHERE asof_date = (
            SELECT MAX(asof_date)
            FROM feature_financial_valuation
            WHERE asof_date <= ?
        )
        ORDER BY ticker
        """,
        (asof,),
    ).fetchall()
    out: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        ticker = normalize_ticker(item.get("ticker"))
        if ticker_filter and ticker not in ticker_filter:
            continue
        out.append(item)
        if max_tickers > 0 and len(out) >= max_tickers:
            break
    return out


def load_latest_feature(conn: Any, table: str, score_col: str, *, asof: str) -> dict[int, dict[str, Any]]:
    if table not in ALLOWED_FEATURE_TABLES:
        raise ValueError(f"Unknown feature table: {table}")
    if not table_exists(conn, table):
        return {}
    table_sql = quote_identifier(table)
    columns = {str(row["name"]) for row in conn.execute(f"PRAGMA table_info({table_sql})").fetchall()}
    if not {"company_id", "asof_date", score_col}.issubset(columns):
        return {}
    rows = conn.execute(
        f"""
        SELECT t.*
        FROM {table_sql} t
        JOIN (
            SELECT company_id, MAX(asof_date) AS asof_date
            FROM {table_sql}
            WHERE asof_date <= ?
            GROUP BY company_id
        ) latest ON latest.company_id = t.company_id AND latest.asof_date = t.asof_date
        """,
        (asof,),
    ).fetchall()
    seen: set[int] = set()
    out: dict[int, dict[str, Any]] = {}
    for row in rows:
        company_id = int(row["company_id"])
        if company_id in seen:
            LOGGER.warning(
                "load_latest_feature: duplicate company_id=%d in %s at asof<=%s; keeping first occurrence",
                company_id,
                table,
                asof,
            )
            continue
        seen.add(company_id)
        out[company_id] = dict(row)
    return out


def load_company_model_taxonomy(conn: Any) -> dict[int, str]:
    if not table_exists(conn, "dim_company_model_taxonomy"):
        LOGGER.warning("dim_company_model_taxonomy is missing; cohort-specific scoring gates will not be applied")
        return {}
    rows = conn.execute(
        """
        SELECT company_id, calibration_cohort
        FROM dim_company_model_taxonomy
        """
    ).fetchall()
    return {
        int(row["company_id"]): str(row["calibration_cohort"] or "").strip()
        for row in rows
        if str(row["calibration_cohort"] or "").strip()
    }


def feature_row_count(conn: Any, table: str, *, asof: str) -> int:
    if table not in ALLOWED_FEATURE_TABLES:
        raise ValueError(f"Unknown feature table: {table}")
    if not table_exists(conn, table):
        return 0
    table_sql = quote_identifier(table)
    row = conn.execute(f"SELECT COUNT(*) AS n FROM {table_sql} WHERE asof_date <= ?", (asof,)).fetchone()
    return int(row["n"] or 0) if row is not None else 0


def preflight_required_features(conn: Any, *, asof: str) -> None:
    required = {
        "feature_financial_valuation": "run script 06 first",
        "feature_fda_product_risk": "run script 10 first",
        "feature_reimbursement": "run scripts 14, 15, then 11 first",
        "feature_technical_entry": "run script 12 first",
    }
    missing = [f"{table} ({hint})" for table, hint in required.items() if feature_row_count(conn, table, asof=asof) <= 0]
    if missing:
        raise RuntimeError(f"Required upstream feature table(s) are empty as of {asof}: {', '.join(missing)}")


def feature_latest_asof(conn: Any, table: str, *, asof: str) -> str:
    if table not in ALLOWED_FEATURE_TABLES:
        raise ValueError(f"Unknown feature table: {table}")
    if not table_exists(conn, table):
        return ""
    table_sql = quote_identifier(table)
    row = conn.execute(f"SELECT MAX(asof_date) AS asof_date FROM {table_sql} WHERE asof_date <= ?", (asof,)).fetchone()
    return str(row["asof_date"] or "") if row is not None else ""


def preflight_feature_freshness(conn: Any, *, asof: str, max_staleness_days: int) -> None:
    asof_date = parse_date(asof)
    if asof_date is None:
        raise ValueError(f"Invalid scoring asof date: {asof}")
    stale: list[str] = []
    for table in ("feature_financial_valuation", "feature_fda_product_risk", "feature_reimbursement", "feature_technical_entry"):
        latest = feature_latest_asof(conn, table, asof=asof)
        latest_date = parse_date(latest)
        if latest_date is None:
            stale.append(f"{table}:missing")
            continue
        days_stale = (asof_date - latest_date).days
        if days_stale < 0 or days_stale > max_staleness_days:
            stale.append(f"{table}:{days_stale}d")
    if stale:
        raise RuntimeError(
            f"Required upstream feature table(s) are stale for {asof}; max_staleness_days={max_staleness_days}: "
            + ", ".join(stale)
        )


def percentile(
    values: list[tuple[int, float]],
    *,
    higher_is_better: bool,
    winsor_low_pct: float = 0.05,
    winsor_high_pct: float = 0.95,
) -> dict[int, float]:
    if not values:
        return {}
    ranked_values = list(values)
    if len(ranked_values) >= 4:
        if not 0.0 <= winsor_low_pct < winsor_high_pct <= 1.0:
            raise ValueError(
                f"winsor bounds must satisfy 0 <= low < high <= 1, got {winsor_low_pct}, {winsor_high_pct}"
            )
        sorted_values = sorted(value for _, value in ranked_values)
        low_bound = sorted_values[max(0, min(len(sorted_values) - 1, math.ceil(winsor_low_pct * len(sorted_values)) - 1))]
        high_bound = sorted_values[max(0, min(len(sorted_values) - 1, math.ceil(winsor_high_pct * len(sorted_values)) - 1))]
        if low_bound > high_bound:
            low_bound, high_bound = high_bound, low_bound
        ranked_values = [(company_id, max(low_bound, min(high_bound, value))) for company_id, value in ranked_values]
    ranked_values.sort(key=lambda item: item[1])
    if len(ranked_values) == 1:
        return {ranked_values[0][0]: 50.0}
    out: dict[int, float] = {}
    denominator = len(ranked_values) - 1
    for rank, (company_id, _) in enumerate(ranked_values):
        pct = 100.0 * rank / denominator
        out[company_id] = pct if higher_is_better else 100.0 - pct
    return out


def first_float(row: dict[str, Any], *keys: str) -> float | None:
    for key in keys:
        value = to_float(row.get(key))
        if value is not None:
            return value
    return None


def ratio_or_none(numerator: float | None, denominator: float | None) -> float | None:
    if numerator is None or denominator is None or abs(denominator) < 1e-12:
        return None
    value = numerator / denominator
    return value if math.isfinite(value) else None


def blend_rank_scores(components: list[tuple[float | None, float]], *, neutral: float) -> tuple[float, int]:
    active = [(float(score), float(weight)) for score, weight in components if score is not None and weight > 0]
    if not active:
        return neutral, 0
    total_weight = sum(weight for _, weight in active)
    if total_weight <= 0:
        return neutral, 0
    return round(clamp(sum(score * weight for score, weight in active) / total_weight), 2), len(active)


def percentile_by_cohort(
    values: list[tuple[int, float]],
    *,
    taxonomy: dict[int, str],
    min_cohort_n: int,
    higher_is_better: bool,
) -> dict[int, float]:
    global_scores = percentile(values, higher_is_better=higher_is_better)
    if not values:
        return {}
    out = dict(global_scores)
    by_cohort: dict[str, list[tuple[int, float]]] = {}
    for company_id, value in values:
        cohort = taxonomy.get(company_id, "") or "unknown"
        by_cohort.setdefault(cohort, []).append((company_id, value))
    for cohort_values in by_cohort.values():
        if len(cohort_values) >= max(2, min_cohort_n):
            out.update(percentile(cohort_values, higher_is_better=higher_is_better))
    return out


def durable_growth_proxy(
    financial_rows: list[dict[str, Any]],
    *,
    taxonomy: dict[int, str],
    config: dict[str, Any],
    neutral: float,
) -> dict[int, DurableGrowthProxy]:
    min_cohort_n = int(cfg_get(config, "scoring.durable_growth_proxy.cohort_rank_min_n", 5))
    pairs: dict[str, list[tuple[int, float]]] = {
        "revenue_growth": [],
        "revenue_acceleration": [],
        "margin_trend": [],
        "gross_margin": [],
        "operating_margin": [],
        "fcf_margin": [],
        "rd_growth": [],
        "rd_intensity": [],
        "dilution": [],
        "leverage": [],
        "runway": [],
        "confidence": [],
    }
    raw_inputs: dict[int, dict[str, float]] = {}
    for row in financial_rows:
        company_id = int(row["company_id"])
        revenue = first_float(row, "revenue_ttm", "revenue_ltm")
        rd_ttm = first_float(row, "annualized_research_and_development", "research_and_development_ttm")
        rd_intensity = first_float(row, "rd_to_revenue_ttm", "rd_to_revenue", "research_and_development_to_revenue")
        if rd_intensity is None:
            rd_intensity = ratio_or_none(rd_ttm, revenue)
        inputs = {
            "revenue_growth": first_float(row, "revenue_yoy_growth", "revenue_growth_yoy", "sales_yoy_growth"),
            "revenue_acceleration": first_float(
                row,
                "revenue_growth_acceleration",
                "revenue_yoy_acceleration",
                "quarterly_revenue_surprise_yoy",
            ),
            "margin_trend": first_float(row, "gross_margin_trend_3y", "gross_margin_change_3y"),
            "gross_margin": first_float(row, "gross_margin_ttm", "gross_margin", "gross_margin_ltm"),
            "operating_margin": first_float(row, "operating_margin_ttm", "operating_margin", "ebit_margin_ttm"),
            "fcf_margin": first_float(row, "fcf_margin_ttm", "free_cash_flow_margin_ttm", "free_cash_flow_margin"),
            "rd_growth": first_float(row, "rd_growth_yoy", "research_and_development_growth_yoy"),
            "rd_intensity": rd_intensity,
            "dilution": first_float(row, "shares_yoy_growth", "diluted_shares_yoy_growth"),
            "leverage": first_float(row, "net_debt_to_revenue", "net_debt_to_sales"),
            "runway": first_float(row, "financial_runway_years", "cash_runway_years"),
            "confidence": first_float(row, "data_confidence_score"),
        }
        raw_inputs[company_id] = {key: value for key, value in inputs.items() if value is not None}
        for key, value in inputs.items():
            if value is not None:
                pairs[key].append((company_id, value))

    rank_maps = {
        "revenue_growth": percentile_by_cohort(
            pairs["revenue_growth"], taxonomy=taxonomy, min_cohort_n=min_cohort_n, higher_is_better=True
        ),
        "revenue_acceleration": percentile_by_cohort(
            pairs["revenue_acceleration"], taxonomy=taxonomy, min_cohort_n=min_cohort_n, higher_is_better=True
        ),
        "margin_trend": percentile_by_cohort(
            pairs["margin_trend"], taxonomy=taxonomy, min_cohort_n=min_cohort_n, higher_is_better=True
        ),
        "gross_margin": percentile_by_cohort(
            pairs["gross_margin"], taxonomy=taxonomy, min_cohort_n=min_cohort_n, higher_is_better=True
        ),
        "operating_margin": percentile_by_cohort(
            pairs["operating_margin"], taxonomy=taxonomy, min_cohort_n=min_cohort_n, higher_is_better=True
        ),
        "fcf_margin": percentile_by_cohort(
            pairs["fcf_margin"], taxonomy=taxonomy, min_cohort_n=min_cohort_n, higher_is_better=True
        ),
        "rd_growth": percentile_by_cohort(
            pairs["rd_growth"], taxonomy=taxonomy, min_cohort_n=min_cohort_n, higher_is_better=True
        ),
        "rd_intensity": percentile_by_cohort(
            pairs["rd_intensity"], taxonomy=taxonomy, min_cohort_n=min_cohort_n, higher_is_better=True
        ),
        "dilution": percentile_by_cohort(
            pairs["dilution"], taxonomy=taxonomy, min_cohort_n=min_cohort_n, higher_is_better=False
        ),
        "leverage": percentile_by_cohort(
            pairs["leverage"], taxonomy=taxonomy, min_cohort_n=min_cohort_n, higher_is_better=False
        ),
        "runway": percentile_by_cohort(
            pairs["runway"], taxonomy=taxonomy, min_cohort_n=min_cohort_n, higher_is_better=True
        ),
        "confidence": percentile_by_cohort(
            pairs["confidence"], taxonomy=taxonomy, min_cohort_n=min_cohort_n, higher_is_better=True
        ),
    }

    expected_component_count = int(cfg_get(config, "scoring.durable_growth_proxy.expected_component_count", 8))
    out: dict[int, DurableGrowthProxy] = {}
    for row in financial_rows:
        company_id = int(row["company_id"])
        ranks = {name: rank_map.get(company_id) for name, rank_map in rank_maps.items()}
        component_count = sum(1 for value in ranks.values() if value is not None)
        if component_count < 2:
            continue
        growth_score, _ = blend_rank_scores(
            [
                (ranks["revenue_growth"], 0.45),
                (ranks["revenue_acceleration"], 0.20),
                (ranks["rd_growth"], 0.15),
                (ranks["margin_trend"], 0.20),
            ],
            neutral=neutral,
        )
        quality_score, _ = blend_rank_scores(
            [
                (ranks["gross_margin"], 0.30),
                (ranks["operating_margin"], 0.25),
                (ranks["fcf_margin"], 0.25),
                (ranks["confidence"], 0.20),
            ],
            neutral=neutral,
        )
        efficiency_score, _ = blend_rank_scores(
            [
                (ranks["margin_trend"], 0.30),
                (ranks["fcf_margin"], 0.25),
                (ranks["rd_intensity"], 0.20),
                (ranks["revenue_growth"], 0.25),
            ],
            neutral=neutral,
        )
        capital_discipline_score, _ = blend_rank_scores(
            [
                (ranks["dilution"], 0.40),
                (ranks["leverage"], 0.35),
                (ranks["runway"], 0.25),
            ],
            neutral=neutral,
        )
        legacy_score, _ = blend_rank_scores(
            [
                (ranks["margin_trend"], 0.30),
                (ranks["rd_growth"], 0.20),
                (ranks["dilution"], 0.20),
                (ranks["leverage"], 0.15),
                (ranks["confidence"], 0.15),
            ],
            neutral=neutral,
        )
        diagnostic_composite_score, _ = blend_rank_scores(
            [
                (growth_score, 0.45),
                (quality_score, 0.25),
                (efficiency_score, 0.15),
                (capital_discipline_score, 0.15),
            ],
            neutral=neutral,
        )
        confidence_score = ranks.get("confidence")
        coverage_score = clamp(100.0 * component_count / max(1, expected_component_count))
        evidence_quality_score = round(
            clamp(0.70 * coverage_score + 0.30 * (confidence_score if confidence_score is not None else neutral)),
            2,
        )
        out[company_id] = DurableGrowthProxy(
            legacy_score=legacy_score,
            growth_score=growth_score,
            quality_score=quality_score,
            efficiency_score=efficiency_score,
            capital_discipline_score=capital_discipline_score,
            evidence_quality_score=evidence_quality_score,
            component_count=component_count,
            source="daily_score_durable_proxy_v2",
            payload={
                "raw_inputs": raw_inputs.get(company_id, {}),
                "rank_scores": {key: value for key, value in ranks.items() if value is not None},
                "durable_growth_diagnostic_composite_score": diagnostic_composite_score,
                "cohort_rank_min_n": min_cohort_n,
                "expected_component_count": expected_component_count,
            },
        )
    return out


def shrink_to_neutral(score: float, *, neutral: float, weight: float) -> float:
    return round(neutral + (score - neutral) * weight, 2)


def sentiment_catalyst_proxy(
    financial_rows: list[dict[str, Any]],
    *,
    config: dict[str, Any],
    neutral: float,
) -> dict[int, SentimentProxy]:
    annual_fallback_enabled = cfg_bool(
        config,
        "scoring.sentiment_catalyst_proxy.annual_revenue_growth_fallback_enabled",
        True,
    )
    rd_fallback_enabled = cfg_bool(
        config,
        "scoring.sentiment_catalyst_proxy.rd_scale_fallback_enabled",
        True,
    )
    annual_weight = cfg_float(config, "scoring.sentiment_catalyst_proxy.annual_revenue_growth_weight", 0.75)
    annual_min_revenue = cfg_float(
        config,
        "scoring.sentiment_catalyst_proxy.annual_revenue_growth_min_revenue_ttm",
        10_000_000.0,
    )
    rd_weight = cfg_float(config, "scoring.sentiment_catalyst_proxy.rd_scale_weight", 0.50)
    rd_max_revenue = cfg_float(
        config,
        "scoring.sentiment_catalyst_proxy.rd_scale_max_revenue_ttm",
        5_000_000.0,
    )
    runway_weight = cfg_float(config, "scoring.sentiment_catalyst_proxy.pre_revenue_runway_weight", 0.60)
    pre_revenue_rd_weight = cfg_float(config, "scoring.sentiment_catalyst_proxy.pre_revenue_rd_scale_weight", 0.40)
    rd_log_transform = cfg_bool(config, "scoring.sentiment_catalyst_proxy.rd_scale_log_transform", True)
    surprise_pairs: list[tuple[int, float]] = []
    annual_growth_pairs: list[tuple[int, float]] = []
    runway_pairs: list[tuple[int, float]] = []
    rd_scale_pairs: list[tuple[int, float]] = []
    for row in financial_rows:
        company_id = int(row["company_id"])
        surprise = to_float(row.get("quarterly_revenue_surprise_yoy"))
        if surprise is not None:
            surprise_pairs.append((company_id, surprise))
            continue
        revenue_ttm = to_float(row.get("revenue_ttm"))
        annual_growth = to_float(row.get("revenue_yoy_growth"))
        if annual_fallback_enabled and annual_growth is not None and revenue_ttm is not None and revenue_ttm >= annual_min_revenue:
            annual_growth_pairs.append((company_id, annual_growth))
            continue
        rd_ttm = to_float(row.get("annualized_research_and_development"))
        if rd_ttm is None:
            rd_ttm = to_float(row.get("research_and_development_ttm"))
        pre_revenue = revenue_ttm is None or revenue_ttm <= rd_max_revenue
        runway_years = to_float(row.get("financial_runway_years"))
        if rd_fallback_enabled and pre_revenue and runway_years is not None and runway_years > 0:
            runway_pairs.append((company_id, runway_years))
        if rd_fallback_enabled and pre_revenue and rd_ttm is not None and abs(rd_ttm) > 0:
            rd_value = math.log1p(abs(rd_ttm)) if rd_log_transform else abs(rd_ttm)
            rd_scale_pairs.append((company_id, rd_value))
    surprise_scores = percentile(surprise_pairs, higher_is_better=True)
    annual_growth_scores = percentile(annual_growth_pairs, higher_is_better=True)
    runway_scores = percentile(runway_pairs, higher_is_better=True)
    rd_scale_scores = percentile(rd_scale_pairs, higher_is_better=True)
    out: dict[int, SentimentProxy] = {
        company_id: SentimentProxy(
            score=round(score, 2),
            source="daily_score_quarterly_revenue_surprise_proxy",
            input_name="quarterly_revenue_surprise_yoy",
        )
        for company_id, score in surprise_scores.items()
    }
    for company_id, score in annual_growth_scores.items():
        if company_id in out:
            continue
        out[company_id] = SentimentProxy(
            score=shrink_to_neutral(score, neutral=neutral, weight=annual_weight),
            source="daily_score_annual_revenue_growth_fallback",
            input_name="revenue_yoy_growth",
        )
    pre_revenue_ids = sorted(set(runway_scores) | set(rd_scale_scores))
    for company_id in pre_revenue_ids:
        if company_id in out:
            continue
        components: list[tuple[float, float]] = []
        if company_id in runway_scores:
            components.append((float(runway_scores[company_id]), runway_weight))
        if company_id in rd_scale_scores:
            components.append((float(rd_scale_scores[company_id]), pre_revenue_rd_weight))
        if not components:
            continue
        total_weight = sum(weight for _, weight in components)
        blended = sum(score * weight for score, weight in components) / total_weight
        out[company_id] = SentimentProxy(
            score=shrink_to_neutral(blended, neutral=neutral, weight=rd_weight),
            source="daily_score_pre_revenue_runway_rd_fallback",
            input_name="financial_runway_years;annualized_research_and_development",
        )
    return out


def durable_proxy_available(financial_item: dict[str, Any]) -> bool:
    return sum(1 for key in DURABLE_GROWTH_PROXY_INPUT_FIELDS if to_float(financial_item.get(key)) is not None) >= 2


def upsert_durable_growth_proxy_rows(conn: Any, rows: list[ScoreRow]) -> int:
    now = utc_now()
    payload_rows = [
        (
            row.asof_date,
            row.company_id,
            row.durable_growth_score_legacy,
            json.dumps(
                {
                    "source": "daily_score_durable_proxy_v2_legacy",
                    "note": "Stores descriptive legacy proxy, not cohort-transformed alpha.",
                    "inputs": list(DURABLE_GROWTH_PROXY_INPUT_FIELDS),
                    "scores": {
                        "durable_growth_score_legacy": row.durable_growth_score_legacy,
                        "durable_growth_alpha_score": row.durable_growth_alpha_score,
                        "durable_growth_growth_score": row.durable_growth_growth_score,
                        "durable_growth_quality_score": row.durable_growth_quality_score,
                        "durable_growth_efficiency_score": row.durable_growth_efficiency_score,
                        "durable_growth_capital_discipline_score": row.durable_growth_capital_discipline_score,
                        "durable_growth_evidence_quality_score": row.durable_growth_evidence_quality_score,
                        "durable_growth_component_count": row.durable_growth_component_count,
                        "durable_growth_signal_mode": row.durable_growth_signal_mode,
                        "durable_growth_signal_direction": row.durable_growth_signal_direction,
                        "durable_growth_signal_reliability": row.durable_growth_signal_reliability,
                    },
                },
                ensure_ascii=True,
                sort_keys=True,
            ),
            now,
            now,
        )
        for row in rows
        if row.durable_growth_proxy_available
    ]
    if not payload_rows:
        return 0
    conn.executemany(
        """
        INSERT INTO feature_durable_growth(asof_date, company_id, score, payload_json, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(asof_date, company_id) DO UPDATE SET
            score = excluded.score,
            payload_json = excluded.payload_json,
            updated_at = excluded.updated_at
        """,
        payload_rows,
    )
    return len(payload_rows)


def upsert_sentiment_proxy_rows(conn: Any, rows: list[ScoreRow]) -> int:
    now = utc_now()
    payload_rows = [
        (
            row.asof_date,
            row.company_id,
            row.sentiment_catalyst_score,
            row.sentiment_catalyst_score,
            50.0,
            json.dumps(
                {
                    "source": row.sentiment_proxy_source or "daily_score_sentiment_proxy",
                    "input": row.sentiment_proxy_input,
                },
                ensure_ascii=True,
                sort_keys=True,
            ),
            now,
            now,
        )
        for row in rows
        if row.sentiment_proxy_available
    ]
    if not payload_rows:
        return 0
    conn.executemany(
        """
        INSERT INTO feature_sentiment_catalyst(
            asof_date, company_id, score, estimate_revision_proxy_score, event_risk_score,
            payload_json, created_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(asof_date, company_id) DO UPDATE SET
            score = excluded.score,
            estimate_revision_proxy_score = excluded.estimate_revision_proxy_score,
            event_risk_score = excluded.event_risk_score,
            payload_json = excluded.payload_json,
            updated_at = excluded.updated_at
        """,
        payload_rows,
    )
    return len(payload_rows)


def score_drivers(row: ScoreRow) -> tuple[list[str], list[str]]:
    items = [
        ("fundamental", row.fundamental_quality_score, 1.0),
        ("durable_growth", row.durable_growth_score, row.durable_growth_component_weight),
        ("fda_product", row.fda_product_score, row.fda_component_weight),
        ("reimbursement", row.reimbursement_score, 1.0),
        ("valuation", row.valuation_score, 1.0),
        ("technical_entry", row.technical_entry_score, row.technical_component_weight),
        ("sentiment_catalyst", row.sentiment_catalyst_score, 1.0),
    ]
    active_items = [(name, score) for name, score, weight in items if weight > WEIGHT_EPSILON]
    positives = [f"{name}:{score:.1f}" for name, score in sorted(active_items, key=lambda item: item[1], reverse=True)[:3]]
    below_neutral = [(name, score) for name, score in active_items if score < 50.0]
    negatives = [f"{name}:{score:.1f}" for name, score in sorted(below_neutral, key=lambda item: item[1])[:3]]
    if row.durable_growth_component_weight <= WEIGHT_EPSILON and row.durable_growth_gate_excluded:
        positives.append(f"durable_growth_overlay:{row.durable_growth_gate_mode}")
    if row.durable_growth_repair_flag:
        negatives.append(f"durable_growth_repair:{row.durable_growth_repair_reason or 'required'}")
    if row.fda_component_weight <= WEIGHT_EPSILON and row.fda_gate_excluded:
        positives.append(f"fda_overlay:{row.fda_gate_mode}")
    if row.technical_component_weight <= WEIGHT_EPSILON and row.technical_overlay_status:
        positives.append(f"technical_overlay:{row.technical_overlay_status}")
    if row.unknown_reimbursement_flag and "reimbursement:unknown" not in negatives:
        negatives.append("reimbursement:unknown")
    return positives, negatives


def weighted_available_score(scores: dict[str, float], available: dict[str, bool], weights: dict[str, float]) -> float:
    active_keys = [key for key, is_available in available.items() if is_available and key in scores]
    total_weight = sum(weights[key] for key in active_keys)
    if total_weight <= 0:
        return DEFAULT_NEUTRAL_SCORE
    return sum(scores[key] * weights[key] for key in active_keys) / total_weight


def value_trap_discount(value_trap_score: float, *, start: float = 40.0) -> float:
    if value_trap_score <= start:
        return 1.0
    return max(0.50, 1.0 - ((value_trap_score - start) / (2.0 * (100.0 - start))))


def cross_sectional_percentile_rank(rows: list[ScoreRow]) -> None:
    pairs = [
        (idx, row.raw_composite_score)
        for idx, row in enumerate(rows)
        if row.raw_composite_score is not None and math.isfinite(row.raw_composite_score)
    ]
    if len(pairs) <= 1:
        for row in rows:
            row.composite_percentile = 50.0
        return
    pairs.sort(key=lambda item: item[1])
    denominator = len(pairs) - 1
    for rank, (idx, _) in enumerate(pairs):
        rows[idx].composite_percentile = round(100.0 * rank / denominator, 2)


def cohort_percentile_rank(rows: list[ScoreRow]) -> None:
    by_cohort: dict[str, list[tuple[int, float]]] = {}
    for idx, row in enumerate(rows):
        cohort = row.calibration_cohort or row.subsector or "unknown"
        if row.raw_composite_score is None or not math.isfinite(row.raw_composite_score):
            continue
        by_cohort.setdefault(cohort, []).append((idx, row.raw_composite_score))
    for pairs in by_cohort.values():
        if len(pairs) <= 1:
            for idx, _ in pairs:
                rows[idx].cohort_percentile = 50.0
            continue
        pairs.sort(key=lambda item: item[1])
        denominator = len(pairs) - 1
        for rank, (idx, _) in enumerate(pairs):
            rows[idx].cohort_percentile = round(100.0 * rank / denominator, 2)


def load_previous_scores(conn: Any, *, asof: str) -> dict[int, dict[str, Any]]:
    previous = conn.execute(
        """
        SELECT MAX(asof_date) AS asof_date
        FROM med_device_daily_scores
        WHERE asof_date < ?
        """,
        (asof,),
    ).fetchone()
    previous_asof = str(previous["asof_date"] or "") if previous is not None else ""
    if not previous_asof:
        return {}
    rows = conn.execute(
        """
        SELECT company_id, composite_score, rank, classification
        FROM med_device_daily_scores
        WHERE asof_date = ?
        """,
        (previous_asof,),
    ).fetchall()
    return {int(row["company_id"]): dict(row) for row in rows}


NON_LIVE_REIMBURSEMENT_STATUSES = {"", "unknown", "cms_data_not_loaded"}


@dataclass(frozen=True)
class TechnicalPolicy:
    gate_mode: str = TECHNICAL_GATE_HARD_POSITIVE
    entry_min: float | None = None
    breakdown_min: float = 35.0
    block_classification: bool = True
    rationale: str = ""


@dataclass(frozen=True)
class FdaGatePolicy:
    gate_mode: str = FDA_GATE_HARD_POSITIVE
    entry_min: float | None = None
    max_event_risk: float = 75.0
    block_classification: bool = True
    rationale: str = ""


@dataclass(frozen=True)
class DurableGrowthPolicy:
    signal_mode: str = DURABLE_GROWTH_MODE_LEGACY_PASSTHROUGH
    gate_mode: str = DURABLE_GROWTH_GATE_HARD_POSITIVE
    entry_min: float | None = None
    reliability: float = 1.0
    min_component_count: int = 0
    min_evidence_quality: float = 0.0
    block_classification: bool = True
    production_state: str = DURABLE_GROWTH_PRODUCTION_PROMOTED
    latest_lcb_excess_delta: float | None = None
    latest_tier1_lcb_excess_delta: float | None = None
    latest_loss_rate: float | None = None
    latest_tier1_loss_rate: float | None = None
    min_lcb_excess_delta: float = 0.0
    max_loss_rate: float = 0.45
    require_positive_tier1_lcb_delta: bool = False
    require_validation_for_nonlegacy: bool = True
    rationale: str = "base_legacy_durable_growth_passthrough"


@dataclass(frozen=True)
class PullbackCandidatePolicy:
    enabled: bool = False
    technical_entry_max: float = 45.0
    fundamental_quality_min: float = 0.0
    valuation_min: float = 35.0
    fda_product_min: float = 50.0
    value_trap_max: float = 60.0
    data_completeness_min: float = 85.0
    template_id: str = ""
    rationale: str = ""


@dataclass(frozen=True)
class ScoreTemplateComponent:
    field: str
    direction: str
    weight: float


@dataclass(frozen=True)
class CohortScoreTemplate:
    template_id: str
    components: tuple[ScoreTemplateComponent, ...]
    rationale: str = ""


def int_flag(raw: object) -> int:
    return 1 if str(raw or "").strip().lower() in {"1", "true", "yes", "y", "on"} or raw == 1 else 0


def reimbursement_component_is_live(item: dict[str, Any], score: float | None) -> bool:
    if score is None:
        return False
    status = str(item.get("reimbursement_status") or "").strip().lower()
    if int_flag(item.get("unknown_reimbursement_flag")) or status in NON_LIVE_REIMBURSEMENT_STATUSES:
        return False
    return bool(
        int_flag(item.get("direct_code_evidence"))
        or int_flag(item.get("payment_rate_evidence"))
        or int_flag(item.get("coverage_policy_evidence"))
        or int_flag(item.get("procedure_bundled_flag"))
        or int_flag(item.get("capital_equipment_flag"))
        or int_flag(item.get("diagnostics_lab_flag"))
    )


def entry_status(technical_score: float) -> str:
    if technical_score < 35.0:
        return "avoid_technical_breakdown"
    if technical_score < 45.0:
        return "not_entry_ready"
    if technical_score < 55.0:
        return "watch_for_setup"
    return "entry_eligible"


def capacity_bucket(avg_dollar_volume_60d: float | None) -> str:
    if avg_dollar_volume_60d is None:
        return "unknown"
    if avg_dollar_volume_60d >= 50_000_000.0:
        return "institutional_liquid"
    if avg_dollar_volume_60d >= 10_000_000.0:
        return "liquid"
    if avg_dollar_volume_60d >= 2_000_000.0:
        return "moderate_capacity"
    if avg_dollar_volume_60d >= 1_000_000.0:
        return "minimum_capacity"
    return "illiquid"


def max_position_size(avg_dollar_volume_60d: float | None, *, participation_rate: float = 0.05) -> float | None:
    if avg_dollar_volume_60d is None or avg_dollar_volume_60d <= 0:
        return None
    return round(avg_dollar_volume_60d * participation_rate, 2)


def min_position_size(max_feasible: float | None, *, target_minimum: float = 25_000.0) -> float | None:
    if max_feasible is None:
        return None
    return round(min(target_minimum, max_feasible), 2)


GATE_KEYS = {
    "composite_min",
    "cohort_percentile_min",
    "fundamental_quality_min",
    "durable_growth_min",
    "fda_product_min",
    "reimbursement_min",
    "valuation_min",
    "technical_entry_min",
    "data_completeness_min",
    "min_avg_dollar_volume_60d",
    "watchlist_min",
    "value_trap_max",
    "value_trap_hard_max",
}


def base_scoring_gates(config: dict[str, Any]) -> dict[str, float]:
    return {
        "composite_min": cfg_float(config, "scoring.gates.composite_min", 75.0),
        "cohort_percentile_min": cfg_float(config, "scoring.gates.cohort_percentile_min", 0.0),
        "fundamental_quality_min": cfg_float(config, "scoring.gates.fundamental_quality_min", 70.0),
        "durable_growth_min": cfg_float(config, "scoring.gates.durable_growth_min", 60.0),
        "fda_product_min": cfg_float(config, "scoring.gates.fda_product_min", 60.0),
        "reimbursement_min": cfg_float(config, "scoring.gates.reimbursement_min", 45.0),
        "valuation_min": cfg_float(config, "scoring.gates.valuation_min", 60.0),
        "technical_entry_min": cfg_float(config, "scoring.gates.technical_entry_min", 55.0),
        "data_completeness_min": cfg_float(config, "scoring.gates.data_completeness_min", 90.0),
        "min_avg_dollar_volume_60d": cfg_float(config, "scoring.gates.min_avg_dollar_volume_60d", 1_000_000.0),
        "watchlist_min": cfg_float(config, "scoring.gates.watchlist_min", 60.0),
        "value_trap_max": cfg_float(config, "scoring.gates.value_trap_max", 20.0),
        "value_trap_hard_max": cfg_float(config, "scoring.gates.value_trap_hard_max", 85.0),
    }


def cohort_gate_profiles(config: dict[str, Any], base_gates: dict[str, float]) -> dict[str, dict[str, float]]:
    raw_profiles = cfg_get(config, "scoring.cohort_profiles", {}) or {}
    if not isinstance(raw_profiles, dict):
        raise ValueError("scoring.cohort_profiles must be a mapping when provided")
    profiles: dict[str, dict[str, float]] = {}
    for cohort, raw_profile in raw_profiles.items():
        if not isinstance(raw_profile, dict):
            continue
        if str(raw_profile.get("enabled", True)).strip().lower() in {"0", "false", "no", "off"}:
            continue
        raw_gates = raw_profile.get("gates", {})
        if not isinstance(raw_gates, dict):
            raise ValueError(f"scoring.cohort_profiles.{cohort}.gates must be a mapping")
        gates = dict(base_gates)
        for key, raw_value in raw_gates.items():
            key_text = str(key)
            if key_text == "raw_composite_min":
                key_text = "composite_min"
            if key_text not in GATE_KEYS:
                LOGGER.warning("Ignoring unknown cohort gate key for %s: %s", cohort, key)
                continue
            value = to_float(raw_value)
            if value is None:
                raise ValueError(f"Cohort gate value must be numeric: scoring.cohort_profiles.{cohort}.gates.{key}")
            gates[key_text] = value
        profiles[str(cohort)] = gates
    return profiles


def gates_for_row(row: ScoreRow, base_gates: dict[str, float], profiles: dict[str, dict[str, float]]) -> dict[str, float]:
    return profiles.get(row.calibration_cohort, base_gates)


def normalize_calibration_status(raw: object, *, context: str) -> str:
    status = str(raw or CALIBRATION_STATUS_PRODUCTION_ELIGIBLE).strip().lower()
    aliases = {
        "production": CALIBRATION_STATUS_PRODUCTION_ELIGIBLE,
        "eligible": CALIBRATION_STATUS_PRODUCTION_ELIGIBLE,
        "research_only": CALIBRATION_STATUS_RESTRICTED_RESEARCH_ONLY,
        "restricted": CALIBRATION_STATUS_RESTRICTED_RESEARCH_ONLY,
        "exclude_tier1": CALIBRATION_STATUS_EXCLUDED_FROM_TIER1,
        "excluded": CALIBRATION_STATUS_EXCLUDED_FROM_TIER1,
    }
    status = aliases.get(status, status)
    if status not in CALIBRATION_STATUSES:
        raise ValueError(f"{context} must be one of {sorted(CALIBRATION_STATUSES)}, got {status!r}")
    return status


def cohort_calibration_status_profiles(config: dict[str, Any]) -> dict[str, tuple[str, str]]:
    raw_profiles = cfg_get(config, "scoring.cohort_profiles", {}) or {}
    if not isinstance(raw_profiles, dict):
        raise ValueError("scoring.cohort_profiles must be a mapping when provided")
    out: dict[str, tuple[str, str]] = {}
    for cohort, raw_profile in raw_profiles.items():
        if not isinstance(raw_profile, dict):
            continue
        if str(raw_profile.get("enabled", True)).strip().lower() in {"0", "false", "no", "off"}:
            continue
        status = normalize_calibration_status(
            raw_profile.get("calibration_status", CALIBRATION_STATUS_PRODUCTION_ELIGIBLE),
            context=f"scoring.cohort_profiles.{cohort}.calibration_status",
        )
        reason = str(
            raw_profile.get("calibration_status_reason")
            or raw_profile.get("validation_note")
            or raw_profile.get("source")
            or ""
        ).strip()
        out[str(cohort)] = (status, reason)
    return out


def calibration_status_for_cohort(cohort: str, profiles: dict[str, tuple[str, str]]) -> tuple[str, str]:
    return profiles.get(cohort, (CALIBRATION_STATUS_PRODUCTION_ELIGIBLE, ""))


def bool_from_raw(raw: object, default: bool) -> bool:
    if raw is None:
        return default
    text = str(raw).strip().lower()
    if text in {"1", "true", "yes", "y", "on"}:
        return True
    if text in {"0", "false", "no", "n", "off"}:
        return False
    return default


def parse_optional_float(raw: object, *, context: str) -> float | None:
    if raw is None or str(raw).strip() == "":
        return None
    value = to_float(raw)
    if value is None:
        raise ValueError(f"Config value must be numeric: {context}")
    return value


def normalize_loss_rate(raw: float | None) -> float | None:
    if raw is None:
        return None
    return raw / 100.0 if raw > 1.0 else raw


def parse_technical_policy(raw: object, *, default: TechnicalPolicy, context: str) -> TechnicalPolicy:
    if raw is None:
        return default
    if not isinstance(raw, dict):
        raise ValueError(f"{context} must be a mapping")
    gate_mode = str(raw.get("gate_mode", default.gate_mode)).strip().lower()
    if gate_mode not in TECHNICAL_GATE_MODES:
        raise ValueError(f"{context}.gate_mode must be one of {sorted(TECHNICAL_GATE_MODES)}, got {gate_mode!r}")
    entry_min = (
        parse_optional_float(raw.get("entry_min"), context=f"{context}.entry_min")
        if "entry_min" in raw
        else default.entry_min
    )
    breakdown_min = (
        parse_optional_float(raw.get("breakdown_min"), context=f"{context}.breakdown_min")
        if "breakdown_min" in raw
        else default.breakdown_min
    )
    if breakdown_min is None:
        breakdown_min = default.breakdown_min
    default_block = gate_mode in {TECHNICAL_GATE_HARD_POSITIVE, TECHNICAL_GATE_BREAKDOWN_VETO_ONLY}
    block_classification = bool_from_raw(raw.get("block_classification"), default_block)
    rationale = str(raw.get("rationale", default.rationale) or "").strip()
    return TechnicalPolicy(
        gate_mode=gate_mode,
        entry_min=entry_min,
        breakdown_min=breakdown_min,
        block_classification=block_classification,
        rationale=rationale,
    )


def base_technical_policy(config: dict[str, Any], gates: dict[str, float]) -> TechnicalPolicy:
    default = TechnicalPolicy(
        gate_mode=TECHNICAL_GATE_HARD_POSITIVE,
        entry_min=gates["technical_entry_min"],
        breakdown_min=35.0,
        block_classification=True,
        rationale="base_hard_positive_technical_gate",
    )
    return parse_technical_policy(
        cfg_get(config, "scoring.technical_policy", None),
        default=default,
        context="scoring.technical_policy",
    )


def cohort_technical_policy_profiles(config: dict[str, Any], base_policy: TechnicalPolicy) -> dict[str, TechnicalPolicy]:
    raw_profiles = cfg_get(config, "scoring.cohort_profiles", {}) or {}
    if not isinstance(raw_profiles, dict):
        raise ValueError("scoring.cohort_profiles must be a mapping when provided")
    out: dict[str, TechnicalPolicy] = {}
    for cohort, raw_profile in raw_profiles.items():
        if not isinstance(raw_profile, dict):
            continue
        if str(raw_profile.get("enabled", True)).strip().lower() in {"0", "false", "no", "off"}:
            continue
        if "technical_policy" not in raw_profile:
            continue
        out[str(cohort)] = parse_technical_policy(
            raw_profile.get("technical_policy"),
            default=base_policy,
            context=f"scoring.cohort_profiles.{cohort}.technical_policy",
        )
    return out


def technical_policy_for_row(row: ScoreRow, base_policy: TechnicalPolicy, profiles: dict[str, TechnicalPolicy]) -> TechnicalPolicy:
    return profiles.get(row.calibration_cohort, base_policy)


def parse_fda_gate_policy(raw: object, *, default: FdaGatePolicy, context: str) -> FdaGatePolicy:
    if raw is None:
        return default
    if not isinstance(raw, dict):
        raise ValueError(f"{context} must be a mapping")
    gate_mode = str(raw.get("gate_mode", default.gate_mode)).strip().lower()
    aliases = {
        "risk_veto": FDA_GATE_RISK_VETO_ONLY,
        "veto_only": FDA_GATE_RISK_VETO_ONLY,
        "overlay": FDA_GATE_OVERLAY_ONLY,
        "neutral": FDA_GATE_OVERLAY_ONLY,
    }
    gate_mode = aliases.get(gate_mode, gate_mode)
    if gate_mode not in FDA_GATE_MODES:
        raise ValueError(f"{context}.gate_mode must be one of {sorted(FDA_GATE_MODES)}, got {gate_mode!r}")
    entry_min = (
        parse_optional_float(raw.get("entry_min"), context=f"{context}.entry_min")
        if "entry_min" in raw
        else default.entry_min
    )
    max_event_risk = (
        parse_optional_float(raw.get("max_event_risk"), context=f"{context}.max_event_risk")
        if "max_event_risk" in raw
        else default.max_event_risk
    )
    if max_event_risk is None:
        max_event_risk = default.max_event_risk
    default_block = gate_mode in {FDA_GATE_HARD_POSITIVE, FDA_GATE_RISK_VETO_ONLY}
    return FdaGatePolicy(
        gate_mode=gate_mode,
        entry_min=entry_min,
        max_event_risk=max_event_risk,
        block_classification=bool_from_raw(raw.get("block_classification"), default_block),
        rationale=str(raw.get("rationale", default.rationale) or "").strip(),
    )


def base_fda_gate_policy(config: dict[str, Any], gates: dict[str, float]) -> FdaGatePolicy:
    default = FdaGatePolicy(
        gate_mode=FDA_GATE_RISK_VETO_ONLY,
        entry_min=gates["fda_product_min"],
        max_event_risk=75.0,
        block_classification=True,
        rationale="base_fda_risk_veto_not_global_positive_alpha",
    )
    return parse_fda_gate_policy(
        cfg_get(config, "scoring.fda_policy", None),
        default=default,
        context="scoring.fda_policy",
    )


def cohort_fda_gate_policy_profiles(config: dict[str, Any], base_policy: FdaGatePolicy) -> dict[str, FdaGatePolicy]:
    raw_profiles = cfg_get(config, "scoring.cohort_profiles", {}) or {}
    if not isinstance(raw_profiles, dict):
        raise ValueError("scoring.cohort_profiles must be a mapping when provided")
    out: dict[str, FdaGatePolicy] = {}
    for cohort, raw_profile in raw_profiles.items():
        if not isinstance(raw_profile, dict):
            continue
        if str(raw_profile.get("enabled", True)).strip().lower() in {"0", "false", "no", "off"}:
            continue
        if "fda_policy" not in raw_profile:
            continue
        out[str(cohort)] = parse_fda_gate_policy(
            raw_profile.get("fda_policy"),
            default=base_policy,
            context=f"scoring.cohort_profiles.{cohort}.fda_policy",
        )
    return out


def fda_policy_for_row(row: ScoreRow, base_policy: FdaGatePolicy, profiles: dict[str, FdaGatePolicy]) -> FdaGatePolicy:
    return profiles.get(row.calibration_cohort, base_policy)


def parse_durable_growth_policy(raw: object, *, default: DurableGrowthPolicy, context: str) -> DurableGrowthPolicy:
    if raw is None:
        return default
    if not isinstance(raw, dict):
        raise ValueError(f"{context} must be a mapping")
    signal_mode = str(raw.get("signal_mode", raw.get("mode", default.signal_mode)) or default.signal_mode).strip().lower()
    signal_mode = {
        "positive": DURABLE_GROWTH_MODE_POSITIVE_ALPHA,
        "trend": DURABLE_GROWTH_MODE_POSITIVE_ALPHA,
        "inverse": DURABLE_GROWTH_MODE_INVERSE_ALPHA,
        "contrarian": DURABLE_GROWTH_MODE_INVERSE_ALPHA,
        "neutral": DURABLE_GROWTH_MODE_NEUTRAL_OVERLAY,
        "overlay": DURABLE_GROWTH_MODE_NEUTRAL_OVERLAY,
        "repair": DURABLE_GROWTH_MODE_REPAIR_DATA,
        "legacy": DURABLE_GROWTH_MODE_LEGACY_PASSTHROUGH,
        "passthrough": DURABLE_GROWTH_MODE_LEGACY_PASSTHROUGH,
    }.get(signal_mode, signal_mode)
    if signal_mode not in DURABLE_GROWTH_MODES:
        raise ValueError(f"{context}.signal_mode must be one of {sorted(DURABLE_GROWTH_MODES)}, got {signal_mode!r}")
    gate_mode = str(raw.get("gate_mode", default.gate_mode) or default.gate_mode).strip().lower()
    gate_mode = {
        "hard": DURABLE_GROWTH_GATE_HARD_POSITIVE,
        "alpha": DURABLE_GROWTH_GATE_ALPHA_ONLY,
        "overlay": DURABLE_GROWTH_GATE_OVERLAY_ONLY,
        "neutral": DURABLE_GROWTH_GATE_OVERLAY_ONLY,
        "repair": DURABLE_GROWTH_GATE_REPAIR_DATA,
    }.get(gate_mode, gate_mode)
    if gate_mode not in DURABLE_GROWTH_GATE_MODES:
        raise ValueError(f"{context}.gate_mode must be one of {sorted(DURABLE_GROWTH_GATE_MODES)}, got {gate_mode!r}")
    entry_min = (
        parse_optional_float(raw.get("entry_min"), context=f"{context}.entry_min")
        if "entry_min" in raw
        else default.entry_min
    )
    reliability = (
        parse_optional_float(raw.get("reliability"), context=f"{context}.reliability")
        if "reliability" in raw
        else default.reliability
    )
    if reliability is None:
        reliability = default.reliability
    raw_min_component_count = to_float(raw.get("min_component_count")) if "min_component_count" in raw else None
    min_component_count = int(raw_min_component_count) if raw_min_component_count is not None else default.min_component_count
    min_evidence_quality = (
        parse_optional_float(raw.get("min_evidence_quality"), context=f"{context}.min_evidence_quality")
        if "min_evidence_quality" in raw
        else default.min_evidence_quality
    )
    if min_evidence_quality is None:
        min_evidence_quality = default.min_evidence_quality
    production_state = str(
        raw.get("production_state", raw.get("validation_status", default.production_state)) or default.production_state
    ).strip().lower()
    production_state = {
        "active": DURABLE_GROWTH_PRODUCTION_PROMOTED,
        "production": DURABLE_GROWTH_PRODUCTION_PROMOTED,
        "promote": DURABLE_GROWTH_PRODUCTION_PROMOTED,
        "promoted": DURABLE_GROWTH_PRODUCTION_PROMOTED,
        "research": DURABLE_GROWTH_PRODUCTION_RESEARCH_ONLY,
        "research_only": DURABLE_GROWTH_PRODUCTION_RESEARCH_ONLY,
        "shadow": DURABLE_GROWTH_PRODUCTION_RESEARCH_ONLY,
        "legacy": DURABLE_GROWTH_PRODUCTION_LEGACY_CHAMPION,
        "legacy_champion": DURABLE_GROWTH_PRODUCTION_LEGACY_CHAMPION,
        "disabled": DURABLE_GROWTH_PRODUCTION_DISABLED,
        "kill": DURABLE_GROWTH_PRODUCTION_DISABLED,
        "off": DURABLE_GROWTH_PRODUCTION_DISABLED,
    }.get(production_state, production_state)
    if production_state not in DURABLE_GROWTH_PRODUCTION_STATES:
        raise ValueError(
            f"{context}.production_state must be one of {sorted(DURABLE_GROWTH_PRODUCTION_STATES)}, "
            f"got {production_state!r}"
        )
    latest_lcb_excess_delta = (
        parse_optional_float(raw.get("latest_lcb_excess_delta"), context=f"{context}.latest_lcb_excess_delta")
        if "latest_lcb_excess_delta" in raw
        else default.latest_lcb_excess_delta
    )
    latest_tier1_lcb_excess_delta = (
        parse_optional_float(raw.get("latest_tier1_lcb_excess_delta"), context=f"{context}.latest_tier1_lcb_excess_delta")
        if "latest_tier1_lcb_excess_delta" in raw
        else default.latest_tier1_lcb_excess_delta
    )
    latest_loss_rate = (
        parse_optional_float(raw.get("latest_loss_rate"), context=f"{context}.latest_loss_rate")
        if "latest_loss_rate" in raw
        else default.latest_loss_rate
    )
    latest_tier1_loss_rate = (
        parse_optional_float(raw.get("latest_tier1_loss_rate"), context=f"{context}.latest_tier1_loss_rate")
        if "latest_tier1_loss_rate" in raw
        else default.latest_tier1_loss_rate
    )
    min_lcb_excess_delta = (
        parse_optional_float(raw.get("min_lcb_excess_delta"), context=f"{context}.min_lcb_excess_delta")
        if "min_lcb_excess_delta" in raw
        else default.min_lcb_excess_delta
    )
    if min_lcb_excess_delta is None:
        min_lcb_excess_delta = default.min_lcb_excess_delta
    max_loss_rate = (
        parse_optional_float(raw.get("max_loss_rate"), context=f"{context}.max_loss_rate")
        if "max_loss_rate" in raw
        else default.max_loss_rate
    )
    if max_loss_rate is None:
        max_loss_rate = default.max_loss_rate
    default_block = gate_mode in {DURABLE_GROWTH_GATE_HARD_POSITIVE, DURABLE_GROWTH_GATE_REPAIR_DATA}
    return DurableGrowthPolicy(
        signal_mode=signal_mode,
        gate_mode=gate_mode,
        entry_min=entry_min,
        reliability=clamp(float(reliability), 0.0, 1.0),
        min_component_count=max(0, min_component_count),
        min_evidence_quality=clamp(float(min_evidence_quality)),
        block_classification=bool_from_raw(raw.get("block_classification"), default_block),
        production_state=production_state,
        latest_lcb_excess_delta=latest_lcb_excess_delta,
        latest_tier1_lcb_excess_delta=latest_tier1_lcb_excess_delta,
        latest_loss_rate=normalize_loss_rate(latest_loss_rate),
        latest_tier1_loss_rate=normalize_loss_rate(latest_tier1_loss_rate),
        min_lcb_excess_delta=float(min_lcb_excess_delta),
        max_loss_rate=clamp(float(normalize_loss_rate(max_loss_rate) or 0.0), 0.0, 1.0),
        require_positive_tier1_lcb_delta=bool_from_raw(
            raw.get("require_positive_tier1_lcb_delta"),
            default.require_positive_tier1_lcb_delta,
        ),
        require_validation_for_nonlegacy=bool_from_raw(
            raw.get("require_validation_for_nonlegacy"),
            default.require_validation_for_nonlegacy,
        ),
        rationale=str(raw.get("rationale", default.rationale) or "").strip(),
    )


def base_durable_growth_policy(config: dict[str, Any], gates: dict[str, float]) -> DurableGrowthPolicy:
    default = DurableGrowthPolicy(
        signal_mode=DURABLE_GROWTH_MODE_LEGACY_PASSTHROUGH,
        gate_mode=DURABLE_GROWTH_GATE_HARD_POSITIVE,
        entry_min=None,
        reliability=1.0,
        min_component_count=0,
        min_evidence_quality=0.0,
        block_classification=True,
        production_state=DURABLE_GROWTH_PRODUCTION_PROMOTED,
        latest_lcb_excess_delta=None,
        latest_tier1_lcb_excess_delta=None,
        latest_loss_rate=None,
        latest_tier1_loss_rate=None,
        min_lcb_excess_delta=0.0,
        max_loss_rate=0.45,
        require_positive_tier1_lcb_delta=False,
        require_validation_for_nonlegacy=True,
        rationale="base_legacy_durable_growth_passthrough",
    )
    return parse_durable_growth_policy(
        cfg_get(config, "scoring.durable_growth_policy", None),
        default=default,
        context="scoring.durable_growth_policy",
    )


def cohort_durable_growth_policy_profiles(config: dict[str, Any], base_policy: DurableGrowthPolicy) -> dict[str, DurableGrowthPolicy]:
    if not cfg_bool(config, "scoring.durable_growth_policy_profiles_enabled", True):
        return {}
    raw_profiles = cfg_get(config, "scoring.cohort_profiles", {}) or {}
    if not isinstance(raw_profiles, dict):
        raise ValueError("scoring.cohort_profiles must be a mapping when provided")
    out: dict[str, DurableGrowthPolicy] = {}
    for cohort, raw_profile in raw_profiles.items():
        if not isinstance(raw_profile, dict):
            continue
        if str(raw_profile.get("enabled", True)).strip().lower() in {"0", "false", "no", "off"}:
            continue
        if "durable_growth_policy" not in raw_profile:
            continue
        out[str(cohort)] = parse_durable_growth_policy(
            raw_profile.get("durable_growth_policy"),
            default=base_policy,
            context=f"scoring.cohort_profiles.{cohort}.durable_growth_policy",
        )
    return out


def durable_growth_policy_for_row(
    row: ScoreRow,
    base_policy: DurableGrowthPolicy,
    profiles: dict[str, DurableGrowthPolicy],
) -> DurableGrowthPolicy:
    return profiles.get(row.calibration_cohort, base_policy)


def cohort_component_weight_profiles(config: dict[str, Any], base_weights: dict[str, float]) -> dict[str, dict[str, float]]:
    raw_profiles = cfg_get(config, "scoring.cohort_profiles", {}) or {}
    if not isinstance(raw_profiles, dict):
        raise ValueError("scoring.cohort_profiles must be a mapping when provided")
    out: dict[str, dict[str, float]] = {}
    for cohort, raw_profile in raw_profiles.items():
        if not isinstance(raw_profile, dict):
            continue
        if str(raw_profile.get("enabled", True)).strip().lower() in {"0", "false", "no", "off"}:
            continue
        if "composite_weights" not in raw_profile:
            continue
        out[str(cohort)] = parse_component_weights(
            raw_profile.get("composite_weights"),
            default_weights=base_weights,
            context=f"scoring.cohort_profiles.{cohort}.composite_weights",
        )
    return out


def weights_for_cohort(cohort: str, base_weights: dict[str, float], profiles: dict[str, dict[str, float]]) -> dict[str, float]:
    return profiles.get(cohort, base_weights)


SCORE_TEMPLATE_FIELD_TO_ATTR = {
    "fundamental_quality_score": "fundamental_quality_score",
    "durable_growth_score": "durable_growth_score",
    "durable_growth_score_legacy": "durable_growth_score_legacy",
    "durable_growth_alpha_score": "durable_growth_alpha_score",
    "durable_growth_growth_score": "durable_growth_growth_score",
    "durable_growth_quality_score": "durable_growth_quality_score",
    "durable_growth_efficiency_score": "durable_growth_efficiency_score",
    "durable_growth_capital_discipline_score": "durable_growth_capital_discipline_score",
    "durable_growth_evidence_quality_score": "durable_growth_evidence_quality_score",
    "fda_product_score": "fda_product_score",
    "fda_alpha_score": "fda_alpha_score",
    "fda_safety_score": "fda_safety_score",
    "fda_clearance_velocity_score": "fda_clearance_velocity_score",
    "fda_evidence_quality_score": "fda_evidence_quality_score",
    "fda_event_risk_score": "fda_event_risk_score",
    "reimbursement_score": "reimbursement_score",
    "valuation_score": "valuation_score",
    "technical_entry_score": "technical_entry_score",
    "technical_setup_score": "technical_setup_score",
    "technical_core_score": "technical_core_score",
    "technical_alpha_score": "technical_alpha_score",
    "technical_pullback_score": "technical_pullback_score",
    "sentiment_catalyst_score": "sentiment_catalyst_score",
    "value_trap_score": "value_trap_score",
}
SCORE_TEMPLATE_FIELD_TO_COMPONENT = {
    "fundamental_quality_score": "fundamental_quality",
    "durable_growth_score": "durable_growth",
    "durable_growth_score_legacy": "durable_growth",
    "durable_growth_alpha_score": "durable_growth",
    "durable_growth_growth_score": "durable_growth",
    "durable_growth_quality_score": "durable_growth",
    "durable_growth_efficiency_score": "durable_growth",
    "durable_growth_capital_discipline_score": "durable_growth",
    "durable_growth_evidence_quality_score": "durable_growth",
    "fda_product_score": "fda_product",
    "fda_alpha_score": "fda_product",
    "fda_safety_score": "fda_product",
    "fda_clearance_velocity_score": "fda_product",
    "fda_evidence_quality_score": "fda_product",
    "fda_event_risk_score": "fda_product",
    "reimbursement_score": "reimbursement",
    "valuation_score": "valuation",
    "technical_entry_score": "technical_entry",
    "technical_setup_score": "technical_entry",
    "technical_core_score": "technical_entry",
    "technical_alpha_score": "technical_entry",
    "technical_pullback_score": "technical_entry",
    "sentiment_catalyst_score": "sentiment_catalyst",
    "value_trap_score": "valuation",
}
SCORE_TEMPLATE_DIRECTIONS = {"positive", "inverse"}


def normalize_score_template_field(raw: object, *, context: str) -> str:
    field = str(raw or "").strip()
    if field not in SCORE_TEMPLATE_FIELD_TO_ATTR:
        raise ValueError(
            f"{context}.field must be one of {sorted(SCORE_TEMPLATE_FIELD_TO_ATTR)}, got {field!r}"
        )
    return field


def parse_score_template_component(raw: object, *, context: str) -> ScoreTemplateComponent:
    if not isinstance(raw, dict):
        raise ValueError(f"{context} must be a mapping")
    field = normalize_score_template_field(raw.get("field"), context=context)
    direction = str(raw.get("direction", "positive") or "positive").strip().lower()
    if direction not in SCORE_TEMPLATE_DIRECTIONS:
        raise ValueError(f"{context}.direction must be one of {sorted(SCORE_TEMPLATE_DIRECTIONS)}, got {direction!r}")
    weight = to_float(raw.get("weight"))
    if weight is None or weight < 0:
        raise ValueError(f"{context}.weight must be a non-negative number")
    return ScoreTemplateComponent(field=field, direction=direction, weight=weight)


def parse_score_template(raw: object, *, context: str) -> CohortScoreTemplate:
    if not isinstance(raw, dict):
        raise ValueError(f"{context} must be a mapping")
    template_id = str(raw.get("template_id") or "").strip()
    if not template_id:
        raise ValueError(f"{context}.template_id is required")
    raw_components = raw.get("components")
    if not isinstance(raw_components, list) or not raw_components:
        raise ValueError(f"{context}.components must be a non-empty list")
    components = tuple(
        parse_score_template_component(item, context=f"{context}.components[{idx}]")
        for idx, item in enumerate(raw_components)
    )
    total = sum(component.weight for component in components)
    if total <= 0:
        raise ValueError(f"{context}.components must have positive total weight")
    rationale = str(raw.get("rationale") or "").strip()
    return CohortScoreTemplate(template_id=template_id, components=components, rationale=rationale)


def cohort_score_template_profiles(config: dict[str, Any]) -> dict[str, CohortScoreTemplate]:
    raw_profiles = cfg_get(config, "scoring.cohort_profiles", {}) or {}
    if not isinstance(raw_profiles, dict):
        raise ValueError("scoring.cohort_profiles must be a mapping when provided")
    out: dict[str, CohortScoreTemplate] = {}
    for cohort, raw_profile in raw_profiles.items():
        if not isinstance(raw_profile, dict):
            continue
        if str(raw_profile.get("enabled", True)).strip().lower() in {"0", "false", "no", "off"}:
            continue
        if "score_template" not in raw_profile:
            continue
        out[str(cohort)] = parse_score_template(
            raw_profile.get("score_template"),
            context=f"scoring.cohort_profiles.{cohort}.score_template",
        )
    return out


def score_template_spec(template: CohortScoreTemplate | None) -> str:
    if template is None:
        return ""
    return ";".join(
        f"{component.field}:{component.direction}:{component.weight:.2f}"
        for component in template.components
    )


def score_template_component_weight(template: CohortScoreTemplate | None, component_name: str) -> float:
    if template is None:
        return 0.0
    total = sum(component.weight for component in template.components)
    if total <= 0:
        return 0.0
    component_weight = sum(
        component.weight
        for component in template.components
        if SCORE_TEMPLATE_FIELD_TO_COMPONENT[component.field] == component_name
    )
    return component_weight / total


def score_template_technical_weight(template: CohortScoreTemplate | None) -> float:
    return score_template_component_weight(template, "technical_entry")


def score_template_fda_weight(template: CohortScoreTemplate | None) -> float:
    return score_template_component_weight(template, "fda_product")


def score_template_durable_growth_weight(template: CohortScoreTemplate | None) -> float:
    return score_template_component_weight(template, "durable_growth")


def score_template_value(
    row: ScoreRow,
    template: CohortScoreTemplate,
    field_available: dict[str, bool],
) -> float:
    numerator = 0.0
    denominator = 0.0
    for component in template.components:
        if not field_available.get(component.field, False):
            continue
        raw_value = to_float(getattr(row, SCORE_TEMPLATE_FIELD_TO_ATTR[component.field], None))
        if raw_value is None:
            continue
        score = clamp(raw_value)
        if component.direction == "inverse":
            score = 100.0 - score
        numerator += score * component.weight
        denominator += component.weight
    if denominator <= 0:
        return DEFAULT_NEUTRAL_SCORE
    return numerator / denominator


def fda_score_source(config: dict[str, Any]) -> str:
    source = str(cfg_get(config, "scoring.fda_score_source", "alpha_when_available") or "alpha_when_available").strip().lower()
    allowed = {"legacy_product", "alpha_when_available", "alpha"}
    if source not in allowed:
        raise ValueError(f"scoring.fda_score_source must be one of {sorted(allowed)}, got {source!r}")
    return source


def selected_fda_score(item: dict[str, Any], *, neutral: float, source: str) -> tuple[float, str]:
    if source in {"alpha", "alpha_when_available"}:
        alpha = to_float(item.get("fda_alpha_score"))
        if alpha is not None:
            return score_or(alpha, neutral), "fda_alpha_score"
        if source == "alpha":
            return neutral, "fda_alpha_score_missing"
    legacy = to_float(item.get("fda_product_score_legacy"))
    if legacy is not None:
        return score_or(legacy, neutral), "fda_product_score_legacy"
    return score_or(item.get("fda_product_score"), neutral), "fda_product_score"


def fda_score_available(item: dict[str, Any], *, source: str) -> bool:
    if not item:
        return False
    if source in {"alpha", "alpha_when_available"}:
        if to_float(item.get("fda_alpha_score")) is not None:
            return True
        if source == "alpha":
            return False
    return to_float(item.get("fda_product_score_legacy")) is not None or to_float(item.get("fda_product_score")) is not None


def durable_growth_score_source(config: dict[str, Any]) -> str:
    source = str(
        cfg_get(config, "scoring.durable_growth_score_source", "alpha_when_available") or "alpha_when_available"
    ).strip().lower()
    allowed = {"legacy", "proxy", "alpha", "alpha_when_available"}
    if source not in allowed:
        raise ValueError(f"scoring.durable_growth_score_source must be one of {sorted(allowed)}, got {source!r}")
    return source


def durable_float_from_payload(item: dict[str, Any], key: str) -> float | None:
    payload = item.get("payload_json") if item else None
    if not payload:
        return None
    try:
        parsed = json.loads(str(payload))
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(parsed, dict):
        return None
    for container in (parsed, parsed.get("scores"), parsed.get("component_scores")):
        if isinstance(container, dict):
            value = to_float(container.get(key))
            if value is not None:
                return value
    return None


def first_not_none(*values: float | None) -> float | None:
    for value in values:
        if value is not None:
            return value
    return None


def durable_legacy_components_from_feature(
    item: dict[str, Any],
    *,
    fallback_score: float,
    neutral: float,
) -> tuple[float, float, float, float, float, float, int]:
    growth_score = first_not_none(
        to_float(item.get("durable_growth_growth_score")),
        durable_float_from_payload(item, "durable_growth_growth_score"),
    )
    quality_score = first_not_none(
        to_float(item.get("durable_growth_quality_score")),
        durable_float_from_payload(item, "durable_growth_quality_score"),
    )
    efficiency_score = first_not_none(
        to_float(item.get("durable_growth_efficiency_score")),
        durable_float_from_payload(item, "durable_growth_efficiency_score"),
    )
    capital_score = first_not_none(
        to_float(item.get("durable_growth_capital_discipline_score")),
        durable_float_from_payload(item, "durable_growth_capital_discipline_score"),
    )
    evidence_score = first_not_none(
        to_float(item.get("durable_growth_evidence_quality_score")),
        durable_float_from_payload(item, "durable_growth_evidence_quality_score"),
    )
    component_count_raw = first_not_none(
        to_float(item.get("durable_growth_component_count")),
        durable_float_from_payload(item, "durable_growth_component_count"),
    )
    component_count = int(component_count_raw) if component_count_raw is not None else 0
    return (
        fallback_score,
        score_or(growth_score, fallback_score),
        score_or(quality_score, fallback_score),
        score_or(efficiency_score, neutral),
        score_or(capital_score, neutral),
        score_or(evidence_score, 50.0 if component_count else 0.0),
        component_count,
    )


def selected_durable_growth_legacy(
    item: dict[str, Any],
    proxy: DurableGrowthProxy | None,
    *,
    neutral: float,
    source: str,
) -> tuple[float, float, float, float, float, float, int, str]:
    if source in {"alpha", "alpha_when_available"} and item:
        alpha = to_float(item.get("durable_growth_alpha_score"))
        if alpha is not None:
            return (*durable_legacy_components_from_feature(item, fallback_score=alpha, neutral=neutral), "durable_growth_alpha_score")
        if source == "alpha":
            return neutral, neutral, neutral, neutral, neutral, 0.0, 0, "durable_growth_alpha_score_missing"
    if source in {"legacy", "alpha_when_available"} and item:
        legacy = to_float(item.get("durable_growth_score_legacy"))
        if legacy is None:
            legacy = to_float(item.get("score"))
        if legacy is not None:
            feature_components = durable_legacy_components_from_feature(item, fallback_score=legacy, neutral=neutral)
            if feature_components[-1] > 0 or proxy is None:
                return (*feature_components, "feature_durable_growth_legacy")
            return (
                proxy.legacy_score,
                proxy.growth_score,
                proxy.quality_score,
                proxy.efficiency_score,
                proxy.capital_discipline_score,
                proxy.evidence_quality_score,
                proxy.component_count,
                proxy.source,
            )
    if proxy is not None:
        return (
            proxy.legacy_score,
            proxy.growth_score,
            proxy.quality_score,
            proxy.efficiency_score,
            proxy.capital_discipline_score,
            proxy.evidence_quality_score,
            proxy.component_count,
            proxy.source,
        )
    return neutral, neutral, neutral, neutral, neutral, 0.0, 0, "durable_growth_missing"


def durable_growth_score_available(item: dict[str, Any], proxy: DurableGrowthProxy | None, *, source: str) -> bool:
    if source in {"alpha", "alpha_when_available"} and item and to_float(item.get("durable_growth_alpha_score")) is not None:
        return True
    if source == "alpha":
        return False
    if item and (to_float(item.get("durable_growth_score_legacy")) is not None or to_float(item.get("score")) is not None):
        return True
    return proxy is not None


def durable_growth_validation_status(policy: DurableGrowthPolicy) -> tuple[str, str]:
    status = policy.production_state
    reasons: list[str] = []
    if status != DURABLE_GROWTH_PRODUCTION_PROMOTED:
        reasons.append(f"production_state_{status}")
    validation_required = (
        status not in {DURABLE_GROWTH_PRODUCTION_LEGACY_CHAMPION, DURABLE_GROWTH_PRODUCTION_DISABLED}
        and policy.signal_mode in {DURABLE_GROWTH_MODE_POSITIVE_ALPHA, DURABLE_GROWTH_MODE_INVERSE_ALPHA}
        and policy.require_validation_for_nonlegacy
    )
    if validation_required:
        if policy.latest_lcb_excess_delta is None:
            status = DURABLE_GROWTH_PRODUCTION_RESEARCH_ONLY
            reasons.append("missing_latest_lcb_excess_delta")
        elif policy.latest_lcb_excess_delta < policy.min_lcb_excess_delta:
            status = DURABLE_GROWTH_PRODUCTION_RESEARCH_ONLY
            reasons.append(
                f"lcb_delta_{policy.latest_lcb_excess_delta:.2f}_below_{policy.min_lcb_excess_delta:.2f}"
            )
        if policy.latest_loss_rate is None:
            status = DURABLE_GROWTH_PRODUCTION_RESEARCH_ONLY
            reasons.append("missing_latest_loss_rate")
        elif policy.latest_loss_rate > policy.max_loss_rate:
            status = DURABLE_GROWTH_PRODUCTION_RESEARCH_ONLY
            reasons.append(f"loss_rate_{policy.latest_loss_rate:.2f}_above_{policy.max_loss_rate:.2f}")
        if policy.require_positive_tier1_lcb_delta:
            if policy.latest_tier1_lcb_excess_delta is None:
                status = DURABLE_GROWTH_PRODUCTION_RESEARCH_ONLY
                reasons.append("missing_latest_tier1_lcb_excess_delta")
            elif policy.latest_tier1_lcb_excess_delta <= 0:
                status = DURABLE_GROWTH_PRODUCTION_RESEARCH_ONLY
                reasons.append(f"tier1_lcb_delta_{policy.latest_tier1_lcb_excess_delta:.2f}_not_positive")
            if policy.latest_tier1_loss_rate is not None and policy.latest_tier1_loss_rate > policy.max_loss_rate:
                status = DURABLE_GROWTH_PRODUCTION_RESEARCH_ONLY
                reasons.append(f"tier1_loss_rate_{policy.latest_tier1_loss_rate:.2f}_above_{policy.max_loss_rate:.2f}")
    if status == DURABLE_GROWTH_PRODUCTION_DISABLED:
        reasons.append("durable_growth_disabled")
    if not reasons and status == DURABLE_GROWTH_PRODUCTION_PROMOTED:
        reasons.append("validation_promoted")
    return status, ";".join(dict.fromkeys(reasons))


def apply_durable_growth_policy(
    *,
    legacy_score: float,
    growth_score: float,
    quality_score: float,
    efficiency_score: float,
    capital_discipline_score: float,
    evidence_quality_score: float,
    component_count: int,
    policy: DurableGrowthPolicy,
    neutral: float,
    source: str,
) -> DurableGrowthSelection:
    repair_reasons: list[str] = []
    if component_count < policy.min_component_count:
        repair_reasons.append("insufficient_durable_growth_components")
    if evidence_quality_score < policy.min_evidence_quality:
        repair_reasons.append("low_durable_growth_evidence_quality")
    validation_status, validation_reason = durable_growth_validation_status(policy)
    validation_promoted = validation_status == DURABLE_GROWTH_PRODUCTION_PROMOTED
    if policy.signal_mode == DURABLE_GROWTH_MODE_DISABLED or validation_status == DURABLE_GROWTH_PRODUCTION_DISABLED:
        oriented = neutral
        direction = "disabled"
        base_reliability = 0.0
    elif policy.signal_mode == DURABLE_GROWTH_MODE_LEGACY_PASSTHROUGH:
        oriented = legacy_score
        direction = "legacy"
        base_reliability = 1.0
        repair_reasons = []
    elif validation_status == DURABLE_GROWTH_PRODUCTION_LEGACY_CHAMPION:
        oriented = legacy_score
        direction = "legacy_champion"
        base_reliability = 1.0
        repair_reasons = []
    elif not validation_promoted:
        oriented = neutral
        direction = f"{policy.signal_mode}_validation_blocked"
        base_reliability = 0.0
    elif policy.signal_mode == DURABLE_GROWTH_MODE_NEUTRAL_OVERLAY:
        oriented = neutral
        direction = "neutral"
        base_reliability = 0.0
    elif policy.signal_mode == DURABLE_GROWTH_MODE_INVERSE_ALPHA:
        oriented = 100.0 - legacy_score
        direction = "inverse"
        base_reliability = policy.reliability
    elif policy.signal_mode == DURABLE_GROWTH_MODE_REPAIR_DATA:
        oriented = neutral
        direction = "repair_data"
        base_reliability = 0.0
        if not repair_reasons:
            repair_reasons.append("cohort_marked_repair_data")
    else:
        oriented = legacy_score
        direction = "positive"
        base_reliability = policy.reliability
    if direction in {"legacy", "legacy_champion"}:
        effective_reliability = 1.0
        alpha_score = round(clamp(oriented), 2)
    else:
        evidence_multiplier = math.sqrt(clamp(evidence_quality_score) / 100.0) if evidence_quality_score > 0 else 0.0
        effective_reliability = clamp(base_reliability * evidence_multiplier, 0.0, 1.0)
        alpha_score = round(clamp(neutral + (oriented - neutral) * effective_reliability), 2)
    if repair_reasons and policy.gate_mode == DURABLE_GROWTH_GATE_REPAIR_DATA:
        alpha_score = neutral
    return DurableGrowthSelection(
        legacy_score=round(clamp(legacy_score), 2),
        alpha_score=alpha_score,
        growth_score=round(clamp(growth_score), 2),
        quality_score=round(clamp(quality_score), 2),
        efficiency_score=round(clamp(efficiency_score), 2),
        capital_discipline_score=round(clamp(capital_discipline_score), 2),
        evidence_quality_score=round(clamp(evidence_quality_score), 2),
        component_count=component_count,
        signal_mode=policy.signal_mode,
        signal_direction=direction,
        signal_reliability=round(effective_reliability, 4),
        score_source=source,
        repair_flag=1 if repair_reasons else 0,
        repair_reason=";".join(dict.fromkeys(repair_reasons)),
        validation_status=validation_status,
        validation_reason=validation_reason,
        production_state=policy.production_state,
    )


def durable_growth_is_active_for_composite(
    policy: DurableGrowthPolicy,
    selection: DurableGrowthSelection,
) -> bool:
    if selection.signal_direction in {"legacy", "legacy_champion"}:
        return selection.validation_status in {
            DURABLE_GROWTH_PRODUCTION_PROMOTED,
            DURABLE_GROWTH_PRODUCTION_LEGACY_CHAMPION,
        }
    if selection.validation_status != DURABLE_GROWTH_PRODUCTION_PROMOTED:
        return False
    if policy.signal_mode in {
        DURABLE_GROWTH_MODE_NEUTRAL_OVERLAY,
        DURABLE_GROWTH_MODE_REPAIR_DATA,
        DURABLE_GROWTH_MODE_DISABLED,
    }:
        return False
    return selection.signal_reliability > WEIGHT_EPSILON


def durable_growth_validation_excludes_gate(policy: DurableGrowthPolicy, row: ScoreRow) -> bool:
    if policy.gate_mode == DURABLE_GROWTH_GATE_REPAIR_DATA:
        return False
    if row.durable_growth_signal_direction == "legacy_champion":
        return False
    if policy.signal_mode == DURABLE_GROWTH_MODE_LEGACY_PASSTHROUGH:
        return row.durable_growth_validation_status not in {
            "",
            DURABLE_GROWTH_PRODUCTION_PROMOTED,
            DURABLE_GROWTH_PRODUCTION_LEGACY_CHAMPION,
        }
    return row.durable_growth_validation_status not in {
        "",
        DURABLE_GROWTH_PRODUCTION_PROMOTED,
        DURABLE_GROWTH_PRODUCTION_LEGACY_CHAMPION,
    }


def technical_score_source(config: dict[str, Any]) -> str:
    source = str(cfg_get(config, "scoring.technical_score_source", "legacy_setup") or "legacy_setup").strip().lower()
    allowed = {"legacy_setup", "alpha_when_available", "alpha"}
    if source not in allowed:
        raise ValueError(f"scoring.technical_score_source must be one of {sorted(allowed)}, got {source!r}")
    return source


def technical_composite_score_source(config: dict[str, Any]) -> str:
    default = technical_score_source(config)
    source = str(cfg_get(config, "scoring.technical_composite_score_source", default) or default).strip().lower()
    allowed = {"legacy_setup", "alpha_when_available", "alpha"}
    if source not in allowed:
        raise ValueError(f"scoring.technical_composite_score_source must be one of {sorted(allowed)}, got {source!r}")
    return source


def technical_entry_status_score_source(config: dict[str, Any]) -> str:
    source = str(
        cfg_get(config, "scoring.technical_entry_status_score_source", "legacy_setup") or "legacy_setup"
    ).strip().lower()
    allowed = {"legacy_setup", "alpha_when_available", "alpha"}
    if source not in allowed:
        raise ValueError(f"scoring.technical_entry_status_score_source must be one of {sorted(allowed)}, got {source!r}")
    return source


def selected_technical_score(item: dict[str, Any], *, neutral: float, source: str) -> tuple[float, str]:
    if source in {"alpha", "alpha_when_available"}:
        alpha = to_float(item.get("technical_alpha_score"))
        if alpha is not None:
            return score_or(alpha, neutral), "technical_alpha_score"
        if source == "alpha":
            return neutral, "technical_alpha_score_missing"
    setup = to_float(item.get("technical_setup_score"))
    if setup is not None:
        return score_or(setup, neutral), "technical_setup_score"
    return score_or(item.get("technical_score"), neutral), "technical_score"


def technical_score_available(item: dict[str, Any], *, source: str) -> bool:
    if not item:
        return False
    if source in {"alpha", "alpha_when_available"}:
        if to_float(item.get("technical_alpha_score")) is not None:
            return True
        if source == "alpha":
            return False
    return to_float(item.get("technical_setup_score")) is not None or to_float(item.get("technical_score")) is not None


def technical_overlay_status(entry: str, *, mode: str) -> str:
    overlay = {
        "entry_eligible": "momentum_confirmed",
        "watch_for_setup": "setup_watch",
        "not_entry_ready": "pullback_or_mean_reversion_candidate",
        "avoid_technical_breakdown": "breakdown_risk",
    }.get(entry, entry or "unclassified")
    return overlay if mode == TECHNICAL_GATE_HARD_POSITIVE else f"{overlay}_{mode}"


def parse_pullback_candidate_policy(raw: object, *, context: str) -> PullbackCandidatePolicy:
    if raw is None:
        return PullbackCandidatePolicy()
    if not isinstance(raw, dict):
        raise ValueError(f"{context} must be a mapping")

    def optional_threshold(key: str, default: float) -> float:
        if key not in raw:
            return default
        value = parse_optional_float(raw.get(key), context=f"{context}.{key}")
        return default if value is None else value

    return PullbackCandidatePolicy(
        enabled=bool_from_raw(raw.get("enabled"), True),
        technical_entry_max=optional_threshold("technical_entry_max", 45.0),
        fundamental_quality_min=optional_threshold("fundamental_quality_min", 0.0),
        valuation_min=optional_threshold("valuation_min", 35.0),
        fda_product_min=optional_threshold("fda_product_min", 50.0),
        value_trap_max=optional_threshold("value_trap_max", 60.0),
        data_completeness_min=optional_threshold("data_completeness_min", 85.0),
        template_id=str(raw.get("template_id") or "").strip(),
        rationale=str(raw.get("rationale") or "validated_pullback_candidate_tag").strip(),
    )


def cohort_pullback_candidate_profiles(config: dict[str, Any]) -> dict[str, PullbackCandidatePolicy]:
    raw_section = cfg_get(config, "scoring.pullback_candidate_tags", {}) or {}
    if not isinstance(raw_section, dict):
        raise ValueError("scoring.pullback_candidate_tags must be a mapping when provided")
    if not bool_from_raw(raw_section.get("enabled"), False):
        return {}
    raw_profiles = raw_section.get("cohorts", {}) or {}
    if not isinstance(raw_profiles, dict):
        raise ValueError("scoring.pullback_candidate_tags.cohorts must be a mapping when provided")
    out: dict[str, PullbackCandidatePolicy] = {}
    for cohort, raw_profile in raw_profiles.items():
        policy = parse_pullback_candidate_policy(
            raw_profile,
            context=f"scoring.pullback_candidate_tags.cohorts.{cohort}",
        )
        if policy.enabled:
            out[str(cohort)] = policy
    return out


def apply_pullback_candidate_tag(row: ScoreRow, policy: PullbackCandidatePolicy | None) -> None:
    row.pullback_candidate_tag = 0
    row.pullback_candidate_reason = ""
    row.pullback_candidate_template_id = ""
    if policy is None or not policy.enabled:
        return
    if row.classification in {"manual_review_regulatory_risk", "avoid_confirmed_regulatory_risk", "data_review_required"}:
        return
    if row.fda_review_state in MANUAL_FDA_REVIEW_STATES or row.hard_red_flag:
        return
    if row.value_trap_score > policy.value_trap_max:
        return
    if row.data_completeness_score < policy.data_completeness_min:
        return
    entry_score = row.technical_entry_status_score if row.technical_entry_status_score is not None else row.technical_entry_score
    if entry_score > policy.technical_entry_max:
        return
    if row.fundamental_quality_score < policy.fundamental_quality_min:
        return
    if row.valuation_score < policy.valuation_min:
        return
    if row.fda_product_score < policy.fda_product_min:
        return
    row.pullback_candidate_tag = 1
    row.pullback_candidate_reason = policy.rationale
    row.pullback_candidate_template_id = policy.template_id


def classify(
    row: ScoreRow,
    *,
    gates: dict[str, float],
    technical_policy: TechnicalPolicy | None = None,
    fda_policy: FdaGatePolicy | None = None,
    durable_policy: DurableGrowthPolicy | None = None,
) -> None:
    if technical_policy is None:
        technical_policy = TechnicalPolicy(
            gate_mode=TECHNICAL_GATE_HARD_POSITIVE,
            entry_min=gates["technical_entry_min"],
            breakdown_min=35.0,
            block_classification=True,
            rationale="legacy_default_hard_positive_technical_gate",
        )
    if fda_policy is None:
        fda_policy = FdaGatePolicy(
            gate_mode=FDA_GATE_HARD_POSITIVE,
            entry_min=gates["fda_product_min"],
            max_event_risk=75.0,
            block_classification=True,
            rationale="legacy_default_hard_positive_fda_gate",
        )
    if durable_policy is None:
        durable_policy = DurableGrowthPolicy(
            signal_mode=DURABLE_GROWTH_MODE_LEGACY_PASSTHROUGH,
            gate_mode=DURABLE_GROWTH_GATE_HARD_POSITIVE,
            entry_min=gates["durable_growth_min"],
            reliability=1.0,
            min_component_count=0,
            min_evidence_quality=0.0,
            block_classification=True,
            production_state=DURABLE_GROWTH_PRODUCTION_PROMOTED,
            latest_lcb_excess_delta=None,
            latest_tier1_lcb_excess_delta=None,
            latest_loss_rate=None,
            latest_tier1_loss_rate=None,
            min_lcb_excess_delta=0.0,
            max_loss_rate=0.45,
            require_positive_tier1_lcb_delta=False,
            require_validation_for_nonlegacy=True,
            rationale="legacy_default_hard_positive_durable_growth_gate",
        )
    reasons: list[str] = []
    entry_score = row.technical_entry_status_score if row.technical_entry_status_score is not None else row.technical_entry_score
    row.entry_status = entry_status(entry_score)
    row.technical_gate_mode = technical_policy.gate_mode
    row.technical_policy_reason = technical_policy.rationale
    row.technical_overlay_status = technical_overlay_status(row.entry_status, mode=technical_policy.gate_mode)
    row.capacity_bucket = capacity_bucket(row.avg_dollar_volume_60d)
    row.max_position_size_feasible = max_position_size(row.avg_dollar_volume_60d)
    row.min_position_size_feasible = min_position_size(row.max_position_size_feasible)
    row.passed_raw_score_gate = int(
        row.raw_composite_score >= gates["composite_min"]
        and row.cohort_percentile >= gates.get("cohort_percentile_min", 0.0)
    )
    row.passed_fundamental_gate = int(row.fundamental_quality_score >= gates["fundamental_quality_min"])
    row.durable_growth_gate_mode = durable_policy.gate_mode
    row.durable_growth_policy_reason = durable_policy.rationale
    growth_min = durable_policy.entry_min if durable_policy.entry_min is not None else gates["durable_growth_min"]
    base_growth_score_gate = int(row.durable_growth_score >= growth_min)
    repair_required = bool(
        durable_policy.gate_mode == DURABLE_GROWTH_GATE_REPAIR_DATA
        or durable_policy.signal_mode == DURABLE_GROWTH_MODE_REPAIR_DATA
        or (durable_policy.block_classification and row.durable_growth_repair_flag)
    )
    validation_excludes_gate = durable_growth_validation_excludes_gate(durable_policy, row)
    if durable_policy.gate_mode == DURABLE_GROWTH_GATE_HARD_POSITIVE:
        if validation_excludes_gate:
            row.passed_growth_gate = 1
            row.durable_growth_gate_excluded = 1
        else:
            row.passed_growth_gate = int(base_growth_score_gate and not repair_required)
            row.durable_growth_gate_excluded = 0
    elif durable_policy.gate_mode == DURABLE_GROWTH_GATE_ALPHA_ONLY:
        row.passed_growth_gate = int(not repair_required)
        row.durable_growth_gate_excluded = 1
    elif durable_policy.gate_mode == DURABLE_GROWTH_GATE_REPAIR_DATA:
        row.passed_growth_gate = 0
        row.durable_growth_gate_excluded = 1
    else:
        row.passed_growth_gate = 1
        row.durable_growth_gate_excluded = 1
    manual_regulatory_state = row.fda_review_state in MANUAL_FDA_REVIEW_STATES
    confirmed_hard_red = row.fda_review_state == "confirmed_hard_red"
    row.fda_gate_mode = fda_policy.gate_mode
    row.fda_policy_reason = fda_policy.rationale
    fda_entry_min = fda_policy.entry_min if fda_policy.entry_min is not None else gates["fda_product_min"]
    base_fda_score_gate = int(
        (not row.fda_data_available and not row.fda_review_state)
        or row.fda_product_score >= fda_entry_min
    )
    if fda_policy.gate_mode == FDA_GATE_HARD_POSITIVE:
        row.passed_fda_gate = base_fda_score_gate
        row.fda_gate_excluded = 0
    elif fda_policy.gate_mode == FDA_GATE_RISK_VETO_ONLY:
        row.passed_fda_gate = int(
            not manual_regulatory_state
            and not row.hard_red_flag
            and row.fda_event_risk_score <= fda_policy.max_event_risk
        )
        row.fda_gate_excluded = 1
    else:
        row.passed_fda_gate = 1
        row.fda_gate_excluded = 1
    reimbursement_live = row.unknown_reimbursement_flag == 0 and row.reimbursement_status not in NON_LIVE_REIMBURSEMENT_STATUSES
    row.passed_reimbursement_gate = int(reimbursement_live and row.reimbursement_score >= gates["reimbursement_min"])
    row.passed_valuation_gate = int(row.valuation_score >= gates["valuation_min"])
    entry_min = technical_policy.entry_min if technical_policy.entry_min is not None else gates["technical_entry_min"]
    row.passed_technical_breakdown_veto = int(
        entry_score >= technical_policy.breakdown_min
        and row.entry_status != "avoid_technical_breakdown"
        and not row.technical_breakdown_flag
    )
    if technical_policy.gate_mode == TECHNICAL_GATE_HARD_POSITIVE:
        row.passed_technical_gate = int(entry_score >= entry_min)
        row.technical_gate_excluded = 0
    elif technical_policy.gate_mode == TECHNICAL_GATE_BREAKDOWN_VETO_ONLY:
        row.passed_technical_gate = row.passed_technical_breakdown_veto
        row.technical_gate_excluded = 0
    else:
        row.passed_technical_gate = 1
        row.technical_gate_excluded = 1
    row.passed_value_trap_gate = int(row.value_trap_score <= gates["value_trap_max"])
    row.passed_data_quality_gate = int(row.data_completeness_score >= gates["data_completeness_min"])
    row.passed_liquidity_gate = int(
        row.avg_dollar_volume_60d is not None and row.avg_dollar_volume_60d >= gates["min_avg_dollar_volume_60d"]
    )
    row.passed_fda_manual_review_gate = int(not manual_regulatory_state and not row.hard_red_flag)

    if not row.passed_raw_score_gate:
        if row.raw_composite_score < gates["composite_min"]:
            reasons.append("composite_below_gate")
        if row.cohort_percentile < gates.get("cohort_percentile_min", 0.0):
            reasons.append("cohort_percentile_below_gate")
    if not row.passed_fundamental_gate:
        reasons.append("fundamental_below_gate")
    if not row.passed_growth_gate:
        if durable_policy.gate_mode == DURABLE_GROWTH_GATE_REPAIR_DATA or repair_required:
            reasons.append("durable_growth_repair_required")
        else:
            reasons.append("growth_below_gate")
    if not row.passed_fda_gate:
        reasons.append("fda_risk_veto" if fda_policy.gate_mode == FDA_GATE_RISK_VETO_ONLY else "fda_below_gate")
    if not row.passed_reimbursement_gate and not reimbursement_live:
        reasons.append("reimbursement_missing_evidence")
    elif not row.passed_reimbursement_gate:
        reasons.append("reimbursement_below_gate")
    if not row.passed_valuation_gate:
        reasons.append("valuation_below_gate")
    if not row.passed_technical_gate:
        reasons.append(
            "technical_breakdown_veto"
            if technical_policy.gate_mode == TECHNICAL_GATE_BREAKDOWN_VETO_ONLY
            else "technical_below_gate"
        )
    if not row.passed_data_quality_gate:
        reasons.append("data_quality_below_gate")
    if not row.passed_liquidity_gate:
        reasons.append("liquidity_below_gate")
    if row.hard_red_flag:
        reasons.append("hard_red_flag")
    elif manual_regulatory_state:
        reasons.append("fda_review_required")
    if row.value_trap_score >= gates["value_trap_hard_max"]:
        reasons.append("value_trap")
    elif not row.passed_value_trap_gate:
        reasons.append("value_trap_soft_gate")
    tier1_restricted = row.calibration_status in {
        CALIBRATION_STATUS_RESTRICTED_RESEARCH_ONLY,
        CALIBRATION_STATUS_EXCLUDED_FROM_TIER1,
    }
    if tier1_restricted:
        reasons.append(row.calibration_status)

    row.failed_gates = ";".join(reasons)
    row.review_reason = ";".join(reasons)
    technical_classification_block = bool(technical_policy.block_classification and not row.passed_technical_gate)
    fda_classification_block = bool(fda_policy.block_classification and not row.passed_fda_gate)
    durable_growth_classification_block = bool(durable_policy.block_classification and not row.passed_growth_gate)
    base_investability_gate = int(
        row.passed_raw_score_gate
        and row.passed_fundamental_gate
        and row.passed_growth_gate
        and row.passed_fda_gate
        and row.passed_reimbursement_gate
        and row.passed_valuation_gate
        and row.passed_technical_gate
        and row.passed_value_trap_gate
        and row.passed_data_quality_gate
        and row.passed_liquidity_gate
        and row.passed_fda_manual_review_gate
        and not technical_classification_block
        and not fda_classification_block
        and not durable_growth_classification_block
    )
    row.final_investability_gate = int(base_investability_gate and not tier1_restricted)
    row.gate_status = "pass" if row.final_investability_gate else "fail"
    if confirmed_hard_red:
        row.classification = "avoid_confirmed_regulatory_risk"
        row.classification_reason = "confirmed_hard_red"
    elif manual_regulatory_state or row.hard_red_flag:
        row.classification = "manual_review_regulatory_risk"
        row.classification_reason = "fda_manual_review_required"
    elif fda_classification_block:
        row.classification = "manual_review_regulatory_risk" if fda_policy.gate_mode == FDA_GATE_RISK_VETO_ONLY else "watchlist"
        row.classification_reason = "fda_risk_veto" if fda_policy.gate_mode == FDA_GATE_RISK_VETO_ONLY else "fda_below_gate"
    elif durable_growth_classification_block and (repair_required or durable_policy.gate_mode == DURABLE_GROWTH_GATE_REPAIR_DATA):
        row.classification = "data_review_required"
        row.classification_reason = row.durable_growth_repair_reason or "durable_growth_repair_required"
    elif durable_growth_classification_block:
        row.classification = "watchlist"
        row.classification_reason = "durable_growth_below_gate"
    elif not row.passed_data_quality_gate:
        row.classification = "data_review_required"
        row.classification_reason = "data_completeness_below_gate"
    elif technical_classification_block:
        row.classification = "watchlist_wait_for_entry"
        row.classification_reason = row.technical_overlay_status or row.entry_status
    elif row.fundamental_quality_score >= gates["fundamental_quality_min"] and row.valuation_score < gates["valuation_min"]:
        row.classification = "quality_watchlist_wait_for_price"
        row.classification_reason = "quality_but_valuation_below_gate"
    elif row.valuation_score >= 75.0 and row.fundamental_quality_score < gates["fundamental_quality_min"]:
        row.classification = "cheap_but_needs_proof"
        row.classification_reason = "cheap_but_fundamental_below_gate"
    elif not row.passed_value_trap_gate and row.raw_composite_score >= gates["watchlist_min"]:
        row.classification = "cheap_but_needs_proof"
        row.classification_reason = "value_trap_soft_gate"
    elif tier1_restricted and base_investability_gate:
        row.classification = "research_watchlist_restricted_cohort"
        row.classification_reason = (
            f"{row.calibration_status};{row.calibration_status_reason}"
            if row.calibration_status_reason
            else row.calibration_status
        )
    elif row.final_investability_gate:
        row.classification = "tier_1_long_candidate"
        overlay_reasons = []
        if row.durable_growth_gate_excluded:
            overlay_reasons.append(f"durable_growth_{row.durable_growth_gate_mode}")
        if row.technical_gate_excluded:
            overlay_reasons.append("technical_overlay_only")
        if row.fda_gate_excluded:
            overlay_reasons.append(f"fda_{row.fda_gate_mode}")
        row.classification_reason = (
            "all_tier1_gates_passed"
            if not overlay_reasons
            else "all_tier1_active_gates_passed;" + ";".join(overlay_reasons)
        )
    elif row.raw_composite_score >= gates["watchlist_min"]:
        row.classification = "watchlist"
        row.classification_reason = "raw_score_above_watchlist_floor"
    else:
        row.classification = "avoid"
        row.classification_reason = "raw_score_below_watchlist_floor"
    row.decision_bucket = row.classification


def build_rows(
    conn: Any,
    *,
    asof: str,
    weights: dict[str, float],
    config: dict[str, Any],
    ticker_filter: set[str],
    max_tickers: int,
) -> list[ScoreRow]:
    financial_rows = load_financial_rows(conn, asof=asof, ticker_filter=ticker_filter, max_tickers=max_tickers)
    fda_rows = load_latest_feature(conn, "feature_fda_product_risk", "fda_product_score", asof=asof)
    reimbursement_rows = load_latest_feature(conn, "feature_reimbursement", "score", asof=asof)
    technical_rows = load_latest_feature(conn, "feature_technical_entry", "technical_score", asof=asof)
    durable_rows = load_latest_feature(conn, "feature_durable_growth", "score", asof=asof)
    sentiment_rows = load_latest_feature(conn, "feature_sentiment_catalyst", "score", asof=asof)
    taxonomy = load_company_model_taxonomy(conn)
    neutral_fundamental = component_neutral(config, "fundamental_quality", "scoring.neutral_fundamental_quality_score", 50.0)
    neutral_durable = component_neutral(config, "durable_growth", "scoring.neutral_durable_growth_score", 50.0)
    neutral_reimbursement = component_neutral(config, "reimbursement", "scoring.neutral_reimbursement_score", 50.0)
    neutral_fda_no_data = component_neutral(config, "fda_product", "scoring.neutral_fda_no_data_score", 45.0)
    neutral_valuation = component_neutral(config, "valuation", "scoring.neutral_valuation_score", 50.0)
    neutral_technical = component_neutral(config, "technical_entry", "scoring.neutral_technical_entry_score", 50.0)
    neutral_sentiment = component_neutral(config, "sentiment_catalyst", "scoring.neutral_sentiment_catalyst_score", 50.0)
    neutral_value_trap = cfg_float(config, "scoring.neutral_value_trap_score", 50.0)
    durable_proxy = durable_growth_proxy(financial_rows, taxonomy=taxonomy, config=config, neutral=neutral_durable)
    sentiment_proxy = sentiment_catalyst_proxy(financial_rows, config=config, neutral=neutral_sentiment)
    gates = base_scoring_gates(config)
    gate_profiles = cohort_gate_profiles(config, gates)
    calibration_status_profiles = cohort_calibration_status_profiles(config)
    weight_profiles = cohort_component_weight_profiles(config, weights)
    score_template_profiles = cohort_score_template_profiles(config)
    default_technical_policy = base_technical_policy(config, gates)
    technical_policy_profiles = cohort_technical_policy_profiles(config, default_technical_policy)
    default_fda_policy = base_fda_gate_policy(config, gates)
    fda_policy_profiles = cohort_fda_gate_policy_profiles(config, default_fda_policy)
    default_durable_growth_policy = base_durable_growth_policy(config, gates)
    durable_growth_policy_profiles = cohort_durable_growth_policy_profiles(config, default_durable_growth_policy)
    pullback_candidate_profiles = cohort_pullback_candidate_profiles(config)
    fda_source = fda_score_source(config)
    durable_source = durable_growth_score_source(config)
    technical_source = technical_composite_score_source(config)
    technical_entry_source = technical_entry_status_score_source(config)
    rank_composite = cfg_bool(config, "scoring.cross_sectional_composite_rank", True)
    model_version = str(cfg_get(config, "scoring.model_version", "med_device_score_v1") or "med_device_score_v1").strip()
    rows: list[ScoreRow] = []
    for item in financial_rows:
        company_id = int(item["company_id"])
        cohort = taxonomy.get(company_id, "")
        calibration_status, calibration_status_reason = calibration_status_for_cohort(cohort, calibration_status_profiles)
        active_weights = weights_for_cohort(cohort, weights, weight_profiles)
        active_score_template = score_template_profiles.get(cohort)
        technical_component_weight = (
            score_template_technical_weight(active_score_template)
            if active_score_template is not None
            else active_weights.get("technical_entry", 0.0)
        )
        fda_component_weight = (
            score_template_fda_weight(active_score_template)
            if active_score_template is not None
            else active_weights.get("fda_product", 0.0)
        )
        durable_component_weight = (
            score_template_durable_growth_weight(active_score_template)
            if active_score_template is not None
            else active_weights.get("durable_growth", 0.0)
        )
        durable_policy = durable_growth_policy_profiles.get(cohort, default_durable_growth_policy)
        fda_item = fda_rows.get(company_id, {})
        reimbursement_item = reimbursement_rows.get(company_id, {})
        technical_item = technical_rows.get(company_id, {})
        durable_item = durable_rows.get(company_id, {})
        sentiment_item = sentiment_rows.get(company_id, {})
        durable_proxy_item = durable_proxy.get(company_id)
        has_durable_proxy = durable_proxy_item is not None
        sentiment_proxy_item = sentiment_proxy.get(company_id)
        has_sentiment_proxy = sentiment_proxy_item is not None
        sentiment_table_score = to_float(sentiment_item.get("score")) if sentiment_item else None
        has_durable_live_score = durable_growth_score_available(durable_item, durable_proxy_item, source=durable_source)
        has_sentiment_live_score = sentiment_table_score is not None or has_sentiment_proxy
        fda_hard_flag = int(fda_item.get("hard_red_flag") or 0) if fda_item else 0
        fda_data_available = int(fda_item.get("fda_data_available") or 0) if fda_item else 0
        reimbursement_hard_flag = int(reimbursement_item.get("hard_red_flag") or 0) if reimbursement_item else 0
        reimbursement_table_score = to_float(reimbursement_item.get("score")) if reimbursement_item else None
        reimbursement_status = str(reimbursement_item.get("reimbursement_status") or "unknown").strip().lower() if reimbursement_item else "unknown"
        direct_code_evidence = int_flag(reimbursement_item.get("direct_code_evidence")) if reimbursement_item else 0
        payment_rate_evidence = int_flag(reimbursement_item.get("payment_rate_evidence")) if reimbursement_item else 0
        coverage_policy_evidence = int_flag(reimbursement_item.get("coverage_policy_evidence")) if reimbursement_item else 0
        procedure_bundled_flag = int_flag(reimbursement_item.get("procedure_bundled_flag")) if reimbursement_item else 0
        capital_equipment_flag = int_flag(reimbursement_item.get("capital_equipment_flag")) if reimbursement_item else 0
        diagnostics_lab_flag = int_flag(reimbursement_item.get("diagnostics_lab_flag")) if reimbursement_item else 0
        unknown_reimbursement_flag = int_flag(reimbursement_item.get("unknown_reimbursement_flag")) if reimbursement_item else 1
        has_reimbursement_live_score = (
            bool(reimbursement_item)
            and reimbursement_component_is_live(reimbursement_item, reimbursement_table_score)
        )
        fda_review_state = str(fda_item.get("review_adjusted_fda_state") or "").strip().lower() if fda_item else ""
        fda_score, active_fda_score_source = (
            selected_fda_score(fda_item, neutral=neutral_fda_no_data, source=fda_source)
            if fda_item
            else (neutral_fda_no_data, "no_fda_feature")
        )
        if fda_item and not fda_data_available and not fda_review_state.startswith("manual_fda_footprint_"):
            fda_score = neutral_fda_no_data
            active_fda_score_source = "neutral_no_mapped_fda_records"
        (
            durable_legacy_score,
            durable_growth_growth_score,
            durable_growth_quality_score,
            durable_growth_efficiency_score,
            durable_growth_capital_discipline_score,
            durable_growth_evidence_quality_score,
            durable_growth_component_count,
            active_durable_growth_score_source,
        ) = selected_durable_growth_legacy(
            durable_item,
            durable_proxy_item,
            neutral=neutral_durable,
            source=durable_source,
        )
        durable_selection = apply_durable_growth_policy(
            legacy_score=durable_legacy_score,
            growth_score=durable_growth_growth_score,
            quality_score=durable_growth_quality_score,
            efficiency_score=durable_growth_efficiency_score,
            capital_discipline_score=durable_growth_capital_discipline_score,
            evidence_quality_score=durable_growth_evidence_quality_score,
            component_count=durable_growth_component_count,
            policy=durable_policy,
            neutral=neutral_durable,
            source=active_durable_growth_score_source,
        )
        durable_score = durable_selection.alpha_score
        sentiment_score = (
            score_or(
                sentiment_item.get("score"),
                sentiment_proxy_item.score if sentiment_proxy_item is not None else neutral_sentiment,
            )
            if sentiment_item
            else sentiment_proxy_item.score if sentiment_proxy_item is not None else neutral_sentiment
        )
        technical_score, active_technical_score_source = (
            selected_technical_score(technical_item, neutral=neutral_technical, source=technical_source)
            if technical_item
            else (neutral_technical, "no_technical_feature")
        )
        technical_entry_status_score, active_technical_entry_status_score_source = (
            selected_technical_score(technical_item, neutral=neutral_technical, source=technical_entry_source)
            if technical_item
            else (neutral_technical, "no_technical_feature")
        )
        technical_trend_quality_score = score_or(technical_item.get("trend_quality_score"), neutral_technical) if technical_item else neutral_technical
        technical_relative_strength_score = score_or(technical_item.get("relative_strength_score"), neutral_technical) if technical_item else neutral_technical
        technical_liquidity_score = score_or(technical_item.get("liquidity_score"), neutral_technical) if technical_item else neutral_technical
        technical_volume_breakout_score = score_or(technical_item.get("volume_breakout_score"), neutral_technical) if technical_item else neutral_technical
        technical_volatility_risk_score = score_or(technical_item.get("volatility_risk_score"), neutral_technical) if technical_item else neutral_technical
        technical_setup_score = (
            score_or(
                technical_item.get("technical_setup_score"),
                score_or(technical_item.get("technical_score"), neutral_technical),
            )
            if technical_item
            else neutral_technical
        )
        technical_core_score = score_or(technical_item.get("technical_core_score"), neutral_technical) if technical_item else neutral_technical
        technical_alpha_score = score_or(technical_item.get("technical_alpha_score"), neutral_technical) if technical_item else neutral_technical
        technical_pullback_score = score_or(technical_item.get("technical_pullback_score"), neutral_technical) if technical_item else neutral_technical
        technical_overextension_score = score_or(technical_item.get("technical_overextension_score"), 0.0) if technical_item else 0.0
        technical_breakdown_flag = int_flag(technical_item.get("technical_breakdown_flag")) if technical_item else 0
        technical_liquidity_gate_flag = int_flag(technical_item.get("technical_liquidity_gate_flag")) if technical_item else 0
        technical_signal_mode = str(technical_item.get("technical_signal_mode") or "") if technical_item else ""
        technical_signal_direction = str(technical_item.get("technical_signal_direction") or "") if technical_item else ""
        technical_signal_reliability = score_or(technical_item.get("technical_signal_reliability"), 0.0) if technical_item else 0.0
        avg_dollar_volume_60d = to_float(technical_item.get("avg_dollar_volume_60d")) if technical_item else None
        liquidity_score = to_float(technical_item.get("liquidity_score")) if technical_item else None
        market_cap = to_float(item.get("market_cap"))
        durable_alpha_active = durable_growth_is_active_for_composite(durable_policy, durable_selection)
        effective_weights = dict(active_weights)
        if not durable_alpha_active:
            effective_weights["durable_growth"] = 0.0
            durable_component_weight = 0.0
        current_shares_outstanding = to_float(item.get("current_shares_outstanding"))
        diluted_weighted_average_shares = to_float(item.get("diluted_weighted_average_shares"))
        basic_weighted_average_shares = to_float(item.get("basic_weighted_average_shares"))
        market_cap_validated_flag = int(item.get("market_cap_validated_flag") or 0)
        component_available = {
            "fundamental_quality": to_float(item.get("fundamental_quality_score_v1")) is not None,
            "durable_growth": bool(has_durable_live_score and durable_alpha_active),
            "fda_product": fda_score_available(fda_item, source=fda_source),
            "reimbursement": has_reimbursement_live_score,
            "valuation": to_float(item.get("valuation_score_v1")) is not None,
            "technical_entry": technical_score_available(technical_item, source=technical_source),
            "sentiment_catalyst": has_sentiment_live_score,
        }
        score_field_available = {
            "fundamental_quality_score": component_available["fundamental_quality"],
            "durable_growth_score": component_available["durable_growth"],
            "durable_growth_score_legacy": component_available["durable_growth"],
            "durable_growth_alpha_score": component_available["durable_growth"],
            "durable_growth_growth_score": component_available["durable_growth"],
            "durable_growth_quality_score": component_available["durable_growth"],
            "durable_growth_efficiency_score": component_available["durable_growth"],
            "durable_growth_capital_discipline_score": component_available["durable_growth"],
            "durable_growth_evidence_quality_score": component_available["durable_growth"],
            "fda_product_score": component_available["fda_product"],
            "fda_alpha_score": bool(fda_item) and to_float(fda_item.get("fda_alpha_score")) is not None,
            "fda_safety_score": bool(fda_item) and to_float(fda_item.get("fda_safety_score")) is not None,
            "fda_clearance_velocity_score": bool(fda_item) and to_float(fda_item.get("fda_clearance_velocity_score")) is not None,
            "fda_evidence_quality_score": bool(fda_item) and to_float(fda_item.get("fda_evidence_quality_score")) is not None,
            "fda_event_risk_score": bool(fda_item) and to_float(fda_item.get("fda_event_risk_score")) is not None,
            "reimbursement_score": component_available["reimbursement"],
            "valuation_score": component_available["valuation"],
            "technical_entry_score": component_available["technical_entry"],
            "technical_setup_score": bool(technical_item)
            and (
                to_float(technical_item.get("technical_setup_score")) is not None
                or to_float(technical_item.get("technical_score")) is not None
            ),
            "technical_core_score": bool(technical_item) and to_float(technical_item.get("technical_core_score")) is not None,
            "technical_alpha_score": bool(technical_item) and to_float(technical_item.get("technical_alpha_score")) is not None,
            "technical_pullback_score": bool(technical_item) and to_float(technical_item.get("technical_pullback_score")) is not None,
            "sentiment_catalyst_score": component_available["sentiment_catalyst"],
            "value_trap_score": to_float(item.get("value_trap_score")) is not None,
        }
        if active_score_template is not None:
            active_template_fields = [
                component.field
                for component in active_score_template.components
                if component.weight > WEIGHT_EPSILON
                and (durable_alpha_active or SCORE_TEMPLATE_FIELD_TO_COMPONENT[component.field] != "durable_growth")
            ]
            active_live_count = sum(1 for field in active_template_fields if score_field_available.get(field, False))
            data_completeness = (
                round(100.0 * active_live_count / len(active_template_fields), 2)
                if active_template_fields
                else 0.0
            )
        else:
            active_component_keys = [key for key, weight in effective_weights.items() if weight > WEIGHT_EPSILON]
            active_live_count = sum(1 for key in active_component_keys if component_available.get(key, False))
            data_completeness = round(100.0 * active_live_count / len(active_component_keys), 2) if active_component_keys else 0.0
        row = ScoreRow(
            asof_date=asof,
            scoring_model_version=model_version,
            rank=0,
            company_id=company_id,
            ticker=normalize_ticker(item.get("ticker")),
            company_name=str(item.get("company_name") or ""),
            subsector=str(item.get("subsector") or ""),
            calibration_cohort=cohort,
            calibration_status=calibration_status,
            calibration_status_reason=calibration_status_reason,
            cohort_score_template_id=active_score_template.template_id if active_score_template is not None else "",
            cohort_score_template_spec=score_template_spec(active_score_template),
            fundamental_quality_score=score_or(item.get("fundamental_quality_score_v1"), neutral_fundamental),
            durable_growth_score=durable_score,
            durable_growth_score_legacy=durable_selection.legacy_score,
            durable_growth_alpha_score=durable_selection.alpha_score,
            durable_growth_growth_score=durable_selection.growth_score,
            durable_growth_quality_score=durable_selection.quality_score,
            durable_growth_efficiency_score=durable_selection.efficiency_score,
            durable_growth_capital_discipline_score=durable_selection.capital_discipline_score,
            durable_growth_evidence_quality_score=durable_selection.evidence_quality_score,
            durable_growth_component_count=durable_selection.component_count,
            durable_growth_signal_mode=durable_selection.signal_mode,
            durable_growth_signal_direction=durable_selection.signal_direction,
            durable_growth_signal_reliability=durable_selection.signal_reliability,
            durable_growth_score_source=durable_selection.score_source,
            durable_growth_gate_mode=durable_policy.gate_mode,
            durable_growth_policy_reason=durable_policy.rationale,
            durable_growth_gate_excluded=int(
                durable_policy.gate_mode
                in {DURABLE_GROWTH_GATE_ALPHA_ONLY, DURABLE_GROWTH_GATE_OVERLAY_ONLY, DURABLE_GROWTH_GATE_DISABLED}
                or not durable_alpha_active
            ),
            durable_growth_component_weight=durable_component_weight,
            durable_growth_repair_flag=durable_selection.repair_flag,
            durable_growth_repair_reason=durable_selection.repair_reason,
            durable_growth_validation_status=durable_selection.validation_status,
            durable_growth_validation_reason=durable_selection.validation_reason,
            durable_growth_production_state=durable_selection.production_state,
            fda_product_score=fda_score,
            fda_product_score_legacy=score_or(fda_item.get("fda_product_score_legacy"), fda_score) if fda_item else neutral_fda_no_data,
            fda_alpha_score=score_or(fda_item.get("fda_alpha_score"), fda_score) if fda_item else neutral_fda_no_data,
            fda_safety_score=score_or(fda_item.get("fda_safety_score"), 50.0) if fda_item else 50.0,
            fda_clearance_velocity_score=score_or(fda_item.get("fda_clearance_velocity_score"), 50.0) if fda_item else 50.0,
            fda_evidence_quality_score=score_or(fda_item.get("fda_evidence_quality_score"), 50.0) if fda_item else 50.0,
            fda_event_risk_score=score_or(fda_item.get("fda_event_risk_score"), 0.0) if fda_item else 0.0,
            fda_signal_mode=str(fda_item.get("fda_signal_mode") or "") if fda_item else "",
            fda_signal_direction=str(fda_item.get("fda_signal_direction") or "") if fda_item else "",
            fda_signal_reliability=(
                (to_float(fda_item.get("fda_signal_reliability")) or 0.0)
                if fda_item
                else 0.0
            ),
            fda_score_source=active_fda_score_source,
            fda_component_weight=fda_component_weight,
            fda_data_available=fda_data_available,
            reimbursement_score=score_or(reimbursement_table_score, neutral_reimbursement) if reimbursement_item else neutral_reimbursement,
            reimbursement_status=reimbursement_status,
            direct_code_evidence=direct_code_evidence,
            payment_rate_evidence=payment_rate_evidence,
            coverage_policy_evidence=coverage_policy_evidence,
            procedure_bundled_flag=procedure_bundled_flag,
            capital_equipment_flag=capital_equipment_flag,
            diagnostics_lab_flag=diagnostics_lab_flag,
            unknown_reimbursement_flag=unknown_reimbursement_flag,
            valuation_score=score_or(item.get("valuation_score_v1"), neutral_valuation),
            technical_entry_score=technical_score,
            technical_trend_quality_score=technical_trend_quality_score,
            technical_relative_strength_score=technical_relative_strength_score,
            technical_liquidity_score=technical_liquidity_score,
            technical_volume_breakout_score=technical_volume_breakout_score,
            technical_volatility_risk_score=technical_volatility_risk_score,
            technical_setup_score=technical_setup_score,
            technical_core_score=technical_core_score,
            technical_alpha_score=technical_alpha_score,
            technical_pullback_score=technical_pullback_score,
            technical_overextension_score=technical_overextension_score,
            technical_breakdown_flag=technical_breakdown_flag,
            technical_liquidity_gate_flag=technical_liquidity_gate_flag,
            technical_signal_mode=technical_signal_mode,
            technical_signal_direction=technical_signal_direction,
            technical_signal_reliability=technical_signal_reliability,
            technical_score_source=active_technical_score_source,
            technical_entry_status_score=technical_entry_status_score,
            technical_entry_status_score_source=active_technical_entry_status_score_source,
            sentiment_catalyst_score=sentiment_score,
            value_trap_score=score_or(item.get("value_trap_score"), neutral_value_trap),
            live_component_count=active_live_count,
            data_completeness_score=data_completeness,
            hard_red_flag=1 if fda_hard_flag or reimbursement_hard_flag else 0,
            hard_red_flag_reasons=";".join(
                reason
                for reason in [
                    str(fda_item.get("hard_red_flag_reasons") or ""),
                    str(reimbursement_item.get("hard_red_flag_reasons") or ""),
                ]
                if reason
            ),
            durable_growth_proxy_available=has_durable_proxy,
            sentiment_proxy_available=has_sentiment_proxy,
            sentiment_proxy_source=sentiment_proxy_item.source if sentiment_proxy_item is not None else "",
            sentiment_proxy_input=sentiment_proxy_item.input_name if sentiment_proxy_item is not None else "",
            avg_dollar_volume_60d=avg_dollar_volume_60d,
            liquidity_score=liquidity_score,
            technical_component_weight=technical_component_weight,
            market_cap=market_cap,
            current_shares_outstanding=current_shares_outstanding,
            diluted_weighted_average_shares=diluted_weighted_average_shares,
            basic_weighted_average_shares=basic_weighted_average_shares,
            shares_source_concept=str(item.get("shares_source_concept") or ""),
            shares_source_form=str(item.get("shares_source_form") or ""),
            shares_source_period=str(item.get("shares_source_period") or ""),
            market_cap_validated_flag=market_cap_validated_flag,
            fda_review_state=fda_review_state,
        )
        row.fda_product_score = row.fda_product_score if row.fda_product_score is not None else 50.0
        row.durable_growth_score = row.durable_growth_score if row.durable_growth_score is not None else 50.0
        row.durable_growth_score_legacy = (
            row.durable_growth_score_legacy
            if row.durable_growth_score_legacy is not None
            else neutral_durable
        )
        row.durable_growth_alpha_score = (
            row.durable_growth_alpha_score
            if row.durable_growth_alpha_score is not None
            else row.durable_growth_score
        )
        row.reimbursement_score = row.reimbursement_score if row.reimbursement_score is not None else neutral_reimbursement
        row.technical_entry_score = row.technical_entry_score if row.technical_entry_score is not None else neutral_technical
        row.technical_entry_status_score = (
            row.technical_entry_status_score
            if row.technical_entry_status_score is not None
            else row.technical_setup_score
        )
        row.sentiment_catalyst_score = row.sentiment_catalyst_score if row.sentiment_catalyst_score is not None else neutral_sentiment
        component_scores = {
            "fundamental_quality": row.fundamental_quality_score,
            "durable_growth": row.durable_growth_score,
            "fda_product": row.fda_product_score,
            "reimbursement": row.reimbursement_score,
            "valuation": row.valuation_score,
            "technical_entry": row.technical_entry_score,
            "sentiment_catalyst": row.sentiment_catalyst_score,
        }
        raw_composite = (
            score_template_value(row, active_score_template, score_field_available)
            if active_score_template is not None
            else weighted_available_score(component_scores, component_available, effective_weights)
        )
        row.raw_composite_score = round(
            clamp(raw_composite * value_trap_discount(row.value_trap_score)),
            2,
        )
        row.composite_score = row.raw_composite_score
        rows.append(row)
    if rank_composite:
        cross_sectional_percentile_rank(rows)
    else:
        for row in rows:
            row.composite_percentile = row.raw_composite_score
    cohort_percentile_rank(rows)
    for row in rows:
        classify(
            row,
            gates=gates_for_row(row, gates, gate_profiles),
            technical_policy=technical_policy_for_row(row, default_technical_policy, technical_policy_profiles),
            fda_policy=fda_policy_for_row(row, default_fda_policy, fda_policy_profiles),
            durable_policy=durable_growth_policy_for_row(
                row,
                default_durable_growth_policy,
                durable_growth_policy_profiles,
            ),
        )
        apply_pullback_candidate_tag(row, pullback_candidate_profiles.get(row.calibration_cohort))
        row.top_positive_drivers, row.top_negative_drivers = score_drivers(row)
    rows.sort(
        key=lambda item: (
            -item.composite_percentile,
            -item.raw_composite_score,
            -item.fundamental_quality_score,
            -item.data_completeness_score,
            item.ticker,
        )
    )
    for rank, row in enumerate(rows, start=1):
        row.rank = rank
    previous_scores = load_previous_scores(conn, asof=asof)
    for row in rows:
        previous = previous_scores.get(row.company_id)
        if not previous:
            continue
        previous_score = to_float(previous.get("composite_score"))
        previous_rank = int(previous["rank"]) if previous.get("rank") is not None else None
        previous_classification = str(previous.get("classification") or "")
        row.composite_score_delta = round(row.composite_score - previous_score, 2) if previous_score is not None else None
        row.rank_delta = previous_rank - row.rank if previous_rank is not None else None
        if previous_classification and previous_classification != row.classification:
            row.classification_change = f"{previous_classification}->{row.classification}"
    return rows


def ensure_daily_score_policy_columns(conn: Any) -> None:
    if not table_exists(conn, "med_device_daily_scores"):
        return
    existing = {str(row["name"]) for row in conn.execute("PRAGMA table_info(med_device_daily_scores)").fetchall()}
    for column, ddl in OPTIONAL_DAILY_SCORE_COLUMNS.items():
        if column not in existing:
            conn.execute(f"ALTER TABLE med_device_daily_scores ADD COLUMN {quote_identifier(column)} {ddl}")


def upsert_rows(conn: Any, rows: list[ScoreRow]) -> int:
    if not rows:
        return 0
    ensure_daily_score_policy_columns(conn)
    now = utc_now()
    columns = [
        "asof_date",
        "company_id",
        "scoring_model_version",
        "composite_score",
        "raw_composite_score",
        "composite_percentile",
        "calibration_cohort",
        "calibration_status",
        "calibration_status_reason",
        "cohort_score_template_id",
        "cohort_score_template_spec",
        "cohort_percentile",
        "fundamental_quality_score",
        "durable_growth_score",
        "durable_growth_score_legacy",
        "durable_growth_alpha_score",
        "durable_growth_growth_score",
        "durable_growth_quality_score",
        "durable_growth_efficiency_score",
        "durable_growth_capital_discipline_score",
        "durable_growth_evidence_quality_score",
        "durable_growth_component_count",
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
        "fda_product_score",
        "fda_product_score_legacy",
        "fda_alpha_score",
        "fda_safety_score",
        "fda_clearance_velocity_score",
        "fda_evidence_quality_score",
        "fda_event_risk_score",
        "fda_signal_mode",
        "fda_signal_direction",
        "fda_signal_reliability",
        "fda_score_source",
        "fda_gate_mode",
        "fda_policy_reason",
        "fda_gate_excluded",
        "fda_component_weight",
        "reimbursement_score",
        "reimbursement_status",
        "direct_code_evidence",
        "payment_rate_evidence",
        "coverage_policy_evidence",
        "procedure_bundled_flag",
        "capital_equipment_flag",
        "diagnostics_lab_flag",
        "unknown_reimbursement_flag",
        "valuation_score",
        "technical_entry_score",
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
        "technical_signal_mode",
        "technical_signal_direction",
        "technical_signal_reliability",
        "technical_score_source",
        "technical_entry_status_score",
        "technical_entry_status_score_source",
        "sentiment_catalyst_score",
        "value_trap_score",
        "rank",
        "data_completeness_score",
        "live_component_count",
        "composite_score_delta",
        "rank_delta",
        "classification_change",
        "classification",
        "decision_bucket",
        "entry_status",
        "technical_gate_mode",
        "technical_overlay_status",
        "technical_policy_reason",
        "technical_gate_excluded",
        "technical_component_weight",
        "pullback_candidate_tag",
        "pullback_candidate_reason",
        "pullback_candidate_template_id",
        "gate_status",
        "review_reason",
        "failed_gates",
        "classification_reason",
        "fda_review_state",
        "market_cap",
        "current_shares_outstanding",
        "diluted_weighted_average_shares",
        "basic_weighted_average_shares",
        "shares_source_concept",
        "shares_source_form",
        "shares_source_period",
        "market_cap_validated_flag",
        "avg_dollar_volume_60d",
        "liquidity_score",
        "capacity_bucket",
        "min_position_size_feasible",
        "max_position_size_feasible",
        "passed_raw_score_gate",
        "passed_fundamental_gate",
        "passed_growth_gate",
        "passed_fda_gate",
        "passed_reimbursement_gate",
        "passed_valuation_gate",
        "passed_technical_gate",
        "passed_technical_breakdown_veto",
        "passed_value_trap_gate",
        "passed_data_quality_gate",
        "passed_liquidity_gate",
        "passed_fda_manual_review_gate",
        "final_investability_gate",
        "hard_red_flag",
        "hard_red_flag_reasons",
        "top_positive_drivers_json",
        "top_negative_drivers_json",
        "created_at",
        "updated_at",
    ]
    placeholders = ", ".join("?" for _ in columns)
    column_sql = ", ".join(quote_identifier(column) for column in columns)
    update_sql = ",\n            ".join(
        f"{quote_identifier(column)} = excluded.{quote_identifier(column)}"
        for column in columns
        if column not in {"asof_date", "company_id", "created_at"}
    )
    conn.executemany(
        f"""
        INSERT INTO med_device_daily_scores({column_sql})
        VALUES ({placeholders})
        ON CONFLICT(asof_date, company_id) DO UPDATE SET
            {update_sql}
        """,
        [
            (
                row.asof_date,
                row.company_id,
                row.scoring_model_version,
                row.composite_score,
                row.raw_composite_score,
                row.composite_percentile,
                row.calibration_cohort,
                row.calibration_status,
                row.calibration_status_reason,
                row.cohort_score_template_id,
                row.cohort_score_template_spec,
                row.cohort_percentile,
                row.fundamental_quality_score,
                row.durable_growth_score,
                row.durable_growth_score_legacy,
                row.durable_growth_alpha_score,
                row.durable_growth_growth_score,
                row.durable_growth_quality_score,
                row.durable_growth_efficiency_score,
                row.durable_growth_capital_discipline_score,
                row.durable_growth_evidence_quality_score,
                row.durable_growth_component_count,
                row.durable_growth_signal_mode,
                row.durable_growth_signal_direction,
                row.durable_growth_signal_reliability,
                row.durable_growth_score_source,
                row.durable_growth_gate_mode,
                row.durable_growth_policy_reason,
                row.durable_growth_gate_excluded,
                row.durable_growth_component_weight,
                row.durable_growth_repair_flag,
                row.durable_growth_repair_reason,
                row.durable_growth_validation_status,
                row.durable_growth_validation_reason,
                row.durable_growth_production_state,
                row.fda_product_score,
                row.fda_product_score_legacy,
                row.fda_alpha_score,
                row.fda_safety_score,
                row.fda_clearance_velocity_score,
                row.fda_evidence_quality_score,
                row.fda_event_risk_score,
                row.fda_signal_mode,
                row.fda_signal_direction,
                row.fda_signal_reliability,
                row.fda_score_source,
                row.fda_gate_mode,
                row.fda_policy_reason,
                row.fda_gate_excluded,
                row.fda_component_weight,
                row.reimbursement_score,
                row.reimbursement_status,
                row.direct_code_evidence,
                row.payment_rate_evidence,
                row.coverage_policy_evidence,
                row.procedure_bundled_flag,
                row.capital_equipment_flag,
                row.diagnostics_lab_flag,
                row.unknown_reimbursement_flag,
                row.valuation_score,
                row.technical_entry_score,
                row.technical_trend_quality_score,
                row.technical_relative_strength_score,
                row.technical_liquidity_score,
                row.technical_volume_breakout_score,
                row.technical_volatility_risk_score,
                row.technical_setup_score,
                row.technical_core_score,
                row.technical_alpha_score,
                row.technical_pullback_score,
                row.technical_overextension_score,
                row.technical_breakdown_flag,
                row.technical_liquidity_gate_flag,
                row.technical_signal_mode,
                row.technical_signal_direction,
                row.technical_signal_reliability,
                row.technical_score_source,
                row.technical_entry_status_score,
                row.technical_entry_status_score_source,
                row.sentiment_catalyst_score,
                row.value_trap_score,
                row.rank,
                row.data_completeness_score,
                row.live_component_count,
                row.composite_score_delta,
                row.rank_delta,
                row.classification_change,
                row.classification,
                row.decision_bucket,
                row.entry_status,
                row.technical_gate_mode,
                row.technical_overlay_status,
                row.technical_policy_reason,
                row.technical_gate_excluded,
                row.technical_component_weight,
                row.pullback_candidate_tag,
                row.pullback_candidate_reason,
                row.pullback_candidate_template_id,
                row.gate_status,
                row.review_reason,
                row.failed_gates,
                row.classification_reason,
                row.fda_review_state,
                row.market_cap,
                row.current_shares_outstanding,
                row.diluted_weighted_average_shares,
                row.basic_weighted_average_shares,
                row.shares_source_concept,
                row.shares_source_form,
                row.shares_source_period,
                row.market_cap_validated_flag,
                row.avg_dollar_volume_60d,
                row.liquidity_score,
                row.capacity_bucket,
                row.min_position_size_feasible,
                row.max_position_size_feasible,
                row.passed_raw_score_gate,
                row.passed_fundamental_gate,
                row.passed_growth_gate,
                row.passed_fda_gate,
                row.passed_reimbursement_gate,
                row.passed_valuation_gate,
                row.passed_technical_gate,
                row.passed_technical_breakdown_veto,
                row.passed_value_trap_gate,
                row.passed_data_quality_gate,
                row.passed_liquidity_gate,
                row.passed_fda_manual_review_gate,
                row.final_investability_gate,
                row.hard_red_flag,
                row.hard_red_flag_reasons,
                json.dumps(row.top_positive_drivers, ensure_ascii=True),
                json.dumps(row.top_negative_drivers, ensure_ascii=True),
                now,
                now,
            )
            for row in rows
        ],
    )
    return len(rows)


def row_to_dict(row: ScoreRow) -> dict[str, Any]:
    item = {field: getattr(row, field) for field in FIELDNAMES if hasattr(row, field)}
    item["top_positive_drivers"] = "; ".join(row.top_positive_drivers)
    item["top_negative_drivers"] = "; ".join(row.top_negative_drivers)
    return item


def write_csv(path: Path, rows: list[ScoreRow]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDNAMES, extrasaction="ignore", lineterminator="\n")
        writer.writeheader()
        writer.writerows(row_to_dict(row) for row in rows)


def main() -> None:
    configure_utc_logging()
    args = parse_args()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    base_dir = config_path.parent
    db_path = args.db.expanduser().resolve() if args.db else resolve_path(cfg_get(config, "paths.database_path"), base_dir=base_dir)
    output_csv = (
        args.output_csv.expanduser().resolve()
        if args.output_csv
        else resolve_path(
            cfg_get(config, "scoring.output_csv", "../output/med_devices_reports/med_device_daily_composite_scores.csv"),
            base_dir=base_dir,
        )
    )
    ticker_filter = {normalize_ticker(value) for value in str(args.tickers or "").split(",") if normalize_ticker(value)}
    weights = load_weights(config)
    with connect(db_path, timeout_sec=float(cfg_get(config, "runtime.sqlite_timeout_sec", 30.0))) as conn:
        init_db(conn)
        run_id = start_run(conn, run_type="build_med_device_daily_scores", input_path=config_path)
        try:
            asof = args.asof.strip() or latest_financial_asof(conn)
            preflight_required_features(conn, asof=asof)
            preflight_feature_freshness(
                conn,
                asof=asof,
                max_staleness_days=int(cfg_get(config, "scoring.max_feature_staleness_days", 7)),
            )
            rows = build_rows(
                conn,
                asof=asof,
                weights=weights,
                config=config,
                ticker_filter=ticker_filter,
                max_tickers=int(args.max_tickers),
            )
            upserted = upsert_rows(conn, rows)
            upsert_durable_growth_proxy_rows(conn, rows)
            upsert_sentiment_proxy_rows(conn, rows)
            write_csv(output_csv, rows)
            message = f"asof={asof} rows={upserted} output={output_csv}"
            finish_run(conn, run_id=run_id, status="success", row_count=upserted, message=message)
            LOGGER.info("Daily composite scores complete: %s", message)
        except BaseException as exc:
            finish_run(conn, run_id=run_id, status="failed", row_count=0, message=f"{type(exc).__name__}: {exc}")
            raise


if __name__ == "__main__":
    main()
