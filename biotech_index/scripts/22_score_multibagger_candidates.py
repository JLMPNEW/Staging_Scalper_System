#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import logging
import math
import re
import sqlite3
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from biotech_index.core.config import cfg_get, load_yaml, resolve_path
from biotech_index.core.db import connect, finish_run, init_db, start_run, utc_now
from biotech_index.core.logging_utils import configure_utc_logging
from biotech_index.core.scoring_math import convex_risk_drag as shared_convex_risk_drag


LOGGER = logging.getLogger("score_multibagger_candidates")
DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
DATE_PREFIX_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})(?:$|[T\s])")


SCORE_FIELDS = [
    "asof_date",
    "company_id",
    "ticker",
    "company_name",
    "multibagger_score",
    "base_multibagger_score",
    "orthogonal_alpha_score",
    "distinctive_acceleration_score",
    "tier1_opportunity_score",
    "tier1_risk_score",
    "tier1_bucket",
    "tier1_gate_score",
    "tier1_gate_multiplier",
    "tier1_available",
    "tier1_interaction_reason",
    "tier1_score_tier",
    "tier1_allocation_eligible",
    "tier1_research_watchlist",
    "tier1_score_spread_to_allocation",
    "tier1_score_spread_to_high_confidence",
    "tier1_rank_quality_cap",
    "tier1_rank_quality_cap_reasons",
    "tier1_rank_quality_cap_vetoed",
    "tier1_rank_quality_cap_veto_reasons",
    "tier1_mature_defensive_score",
    "tier1_expected_return_quality_score",
    "tier1_value_trap_score",
    "tier1_leverage_score",
    "tier1_leverage_fragility_score",
    "tier1_no_forward_guidance_flag",
    "tier1_guidance_stale_flag",
    "tier1_no_guidance_negative_growth_flag",
    "tier1_production_policy_quality_penalty",
    "tier1_production_policy_quality_bonus",
    "rank",
    "bucket",
    "top_evidence_json",
]


CSV_FIELDS = [
    "asof_date",
    "rank",
    "ticker",
    "company_name",
    "bucket",
    "multibagger_score",
    "base_multibagger_score",
    "orthogonal_alpha_score",
    "distinctive_acceleration_score",
    "tier1_opportunity_score",
    "tier1_risk_score",
    "tier1_bucket",
    "tier1_gate_score",
    "tier1_gate_multiplier",
    "tier1_available",
    "tier1_interaction_reason",
    "tier1_score_tier",
    "tier1_allocation_eligible",
    "tier1_research_watchlist",
    "tier1_score_spread_to_allocation",
    "tier1_score_spread_to_high_confidence",
    "tier1_rank_quality_cap",
    "tier1_rank_quality_cap_reasons",
    "tier1_rank_quality_cap_vetoed",
    "tier1_rank_quality_cap_veto_reasons",
    "tier1_mature_defensive_score",
    "tier1_expected_return_quality_score",
    "tier1_value_trap_score",
    "tier1_leverage_score",
    "tier1_leverage_fragility_score",
    "tier1_no_forward_guidance_flag",
    "tier1_guidance_stale_flag",
    "tier1_no_guidance_negative_growth_flag",
    "tier1_production_policy_quality_penalty",
    "tier1_production_policy_quality_bonus",
    "commercial_acceleration_score",
    "upside_capacity_score",
    "cash_flow_acceleration_score",
    "survival_quality_score",
    "governance_event_score",
    "market_confirmation_score",
    "catalyst_quality_score",
    "commercial_fragility_risk_score",
    "multibagger_risk_penalty",
    "ttm_revenue",
    "revenue_yoy_growth_pct",
    "free_cash_flow_ttm",
    "fcf_yield",
    "market_cap",
    "ev_to_sales",
    "pe_ratio",
    "commercial_value_score",
    "valuation_score",
    "quality_adjusted_valuation_score",
    "institutional_upside_capacity_score",
    "value_trap_score",
    "leverage_score",
    "cash_runway_months",
    "forward_revenue_midpoint",
    "forward_revenue_growth_pct",
    "forward_ebitda_midpoint",
    "forward_eps_midpoint",
    "guidance_score",
    "quality_forward_valuation_score",
    "quality_adjusted_guidance_score",
    "guidance_recency_days",
    "guidance_recency_penalty",
    "insider_buy_count_90d",
    "insider_buy_value_90d",
    "buyback_event_count_365d",
    "asr_event_count_365d",
    "relative_strength_3m_vs_xbi",
    "price_vs_200d_pct",
    "distance_from_52w_high_pct",
    "avg_dollar_volume_20d",
    "primary_nct",
    "lead_phase2_3_active_trials",
    "program_phase2_3_active_trials",
    "active_pivotal_trials",
    "evidence_or_catalyst_flag",
    "data_quality",
    "missing_fields",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Score multibagger candidate composite.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--asof", type=str, default="", help="Score date in YYYY-MM-DD. Defaults to latest multibagger feature date.")
    return parser.parse_args()


def configure_logging() -> None:
    configure_utc_logging()


def parse_date(raw: object) -> date | None:
    text = str(raw or "").strip()
    if not text:
        return None
    match = DATE_PREFIX_RE.match(text)
    if not match:
        LOGGER.debug("Invalid multibagger date ignored: %r", raw)
        return None
    try:
        return datetime.strptime(match.group(1), "%Y-%m-%d").date()
    except ValueError:
        LOGGER.debug("Invalid multibagger date ignored: %r", raw)
        return None


def to_float(raw: object, default: float = 0.0) -> float:
    if raw is None:
        return default
    candidate: int | float | str
    if isinstance(raw, bool):
        candidate = int(raw)
    elif isinstance(raw, (int, float, str)):
        candidate = raw
    else:
        candidate = str(raw).strip()
    try:
        value = float(candidate)
    except (TypeError, ValueError):
        return default
    return value if math.isfinite(value) else default


def to_int(raw: object, default: int = 0) -> int:
    return int(round(to_float(raw, float(default))))


def clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, value))


def as_bool(raw: object, default: bool = False) -> bool:
    if raw is None:
        return default
    if isinstance(raw, bool):
        return raw
    text = str(raw).strip().lower()
    if text in {"1", "true", "t", "yes", "y", "enabled", "on"}:
        return True
    if text in {"0", "false", "f", "no", "n", "disabled", "off"}:
        return False
    try:
        value = float(text)
    except ValueError:
        return default
    if math.isfinite(value):
        return value != 0.0
    return default


def convex_risk_drag(risk: float, weight: float, config: dict[str, Any], section: str) -> float:
    return shared_convex_risk_drag(
        risk,
        weight,
        enabled=as_bool(cfg_get(config, f"{section}.convex_risk_penalty_enabled", False), False),
        convexity=float(cfg_get(config, f"{section}.risk_penalty_convexity", 0.35)),
        inflection=float(cfg_get(config, f"{section}.risk_penalty_inflection", 50.0)),
    )


def parse_json(raw: object) -> dict[str, Any]:
    try:
        payload = json.loads(str(raw or "{}"))
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def row_liquidity_ok(row: dict[str, Any], payload: dict[str, Any], config: dict[str, Any]) -> bool:
    raw = row.get("liquidity_ok")
    if raw not in {None, ""}:
        return as_bool(raw, False)
    market_payload = payload.get("market", {}) if isinstance(payload, dict) else {}
    addv = to_float(
        market_payload.get("avg_dollar_volume_20d", row.get("avg_dollar_volume_20d")),
        0.0,
    )
    min_addv = float(cfg_get(config, "multibagger.min_addv20", 1_000_000))
    return addv >= min_addv


def latest_feature_date(conn: sqlite3.Connection) -> str:
    row = conn.execute("SELECT MAX(asof_date) AS asof_date FROM multibagger_features_daily").fetchone()
    asof = str(row["asof_date"] or "") if row else ""
    if not asof:
        raise ValueError("No multibagger_features_daily rows found. Run 21_build_multibagger_features.py first.")
    return asof


def load_feature_rows(conn: sqlite3.Connection, asof_date: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT *
        FROM multibagger_features_daily
        WHERE asof_date = ?
        ORDER BY ticker
        """,
        (asof_date,),
    ).fetchall()
    return [dict(row) for row in rows]


def table_columns(conn: sqlite3.Connection, table_name: str) -> set[str]:
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table_name})").fetchall()}


def optional_column_expr(existing_columns: set[str], column_name: str) -> str:
    if column_name in existing_columns:
        return f"s.{column_name}"
    return f"NULL AS {column_name}"


def load_tier1_score_rows(conn: sqlite3.Connection, asof_date: str) -> dict[int, dict[str, Any]]:
    daily_score_columns = table_columns(conn, "daily_scores")
    optional_columns = [
        "rank_quality_cap",
        "rank_quality_cap_reasons",
        "rank_quality_cap_vetoed",
        "rank_quality_cap_veto_reasons",
        "mature_defensive_score",
        "expected_return_quality_score",
        "value_trap_score",
        "leverage_score",
        "leverage_fragility_score",
        "no_forward_guidance_flag",
        "guidance_stale_flag",
        "guidance_staleness_flag",
        "no_guidance_negative_growth_flag",
        "production_policy_quality_penalty",
        "production_policy_quality_bonus",
    ]
    optional_select = ",\n            ".join(optional_column_expr(daily_score_columns, column) for column in optional_columns)
    rows = conn.execute(
        f"""
        SELECT
            s.asof_date, s.company_id, s.opportunity_score, s.investment_score,
            s.clinical_opportunity_score, s.commercial_value_score, s.upside_capacity_score,
            s.risk_score, s.bucket, s.tier1_selection_gate_score, s.top_evidence_json,
            {optional_select}
        FROM daily_scores s
        JOIN (
            SELECT company_id, MAX(asof_date) AS max_asof
            FROM daily_scores
            WHERE asof_date <= ?
            GROUP BY company_id
        ) latest
          ON latest.company_id = s.company_id AND latest.max_asof = s.asof_date
        """,
        (asof_date,),
    ).fetchall()
    return {int(row["company_id"]): dict(row) for row in rows}


def normalized_config_list(raw: object, default: list[str]) -> set[str]:
    if raw is None:
        return {item.lower() for item in default}
    if isinstance(raw, str):
        return {part.strip().lower() for part in raw.split(",") if part.strip()}
    if isinstance(raw, (list, tuple, set)):
        return {str(item).strip().lower() for item in raw if str(item).strip()}
    return {item.lower() for item in default}


def tier1_score_tier(tier1_score: float | None, *, rank_cap_vetoed: bool = False) -> str:
    if rank_cap_vetoed:
        return "rank_vetoed"
    if tier1_score is None or not math.isfinite(tier1_score):
        return "missing"
    if tier1_score >= 55.0:
        return "high_confidence_allocation"
    if tier1_score >= 50.0:
        return "allocation_candidate"
    if tier1_score >= 45.0:
        return "research_watchlist"
    return "low_priority"


def tier1_score_tier_flags(tier1: dict[str, Any] | None) -> dict[str, Any]:
    if not tier1:
        return {
            "tier1_score_tier": "missing",
            "tier1_allocation_eligible": 0,
            "tier1_research_watchlist": 0,
            "tier1_score_spread_to_allocation": "",
            "tier1_score_spread_to_high_confidence": "",
        }
    score = to_float(tier1.get("opportunity_score"), math.nan)
    rank_cap_vetoed = as_bool(tier1.get("rank_quality_cap_vetoed"), False)
    tier = tier1_score_tier(score if math.isfinite(score) else None, rank_cap_vetoed=rank_cap_vetoed)
    return {
        "tier1_score_tier": tier,
        "tier1_allocation_eligible": 1 if tier in {"high_confidence_allocation", "allocation_candidate"} else 0,
        "tier1_research_watchlist": 1 if tier == "research_watchlist" else 0,
        "tier1_score_spread_to_allocation": round(score - 50.0, 4) if math.isfinite(score) else "",
        "tier1_score_spread_to_high_confidence": round(score - 55.0, 4) if math.isfinite(score) else "",
    }


def tier1_context_values(tier1: dict[str, Any] | None, gate_score: float | None, gate_multiplier: float | None) -> dict[str, Any]:
    tier_flags = tier1_score_tier_flags(tier1)
    if not tier1:
        return {
            "tier1_available": False,
            "tier1_opportunity_score": "",
            "tier1_risk_score": "",
            "tier1_bucket": "",
            "tier1_gate_score": "",
            "tier1_gate_multiplier": "",
            **tier_flags,
            "tier1_rank_quality_cap": "",
            "tier1_rank_quality_cap_reasons": "",
            "tier1_rank_quality_cap_vetoed": 0,
            "tier1_rank_quality_cap_veto_reasons": "",
            "tier1_mature_defensive_score": "",
            "tier1_expected_return_quality_score": "",
            "tier1_value_trap_score": "",
            "tier1_leverage_score": "",
            "tier1_leverage_fragility_score": "",
            "tier1_no_forward_guidance_flag": "",
            "tier1_guidance_stale_flag": "",
            "tier1_no_guidance_negative_growth_flag": "",
            "tier1_production_policy_quality_penalty": "",
            "tier1_production_policy_quality_bonus": "",
        }
    guidance_stale = tier1.get("guidance_stale_flag")
    if guidance_stale in (None, ""):
        guidance_stale = tier1.get("guidance_staleness_flag")
    return {
        "tier1_available": True,
        "tier1_opportunity_score": round(to_float(tier1.get("opportunity_score"), 0.0), 4),
        "tier1_risk_score": round(to_float(tier1.get("risk_score"), 0.0), 4),
        "tier1_bucket": str(tier1.get("bucket") or ""),
        "tier1_gate_score": round(gate_score, 4) if gate_score is not None else "",
        "tier1_gate_multiplier": round(gate_multiplier, 4) if gate_multiplier is not None else "",
        **tier_flags,
        "tier1_rank_quality_cap": round(to_float(tier1.get("rank_quality_cap"), 0.0), 4)
        if tier1.get("rank_quality_cap") not in (None, "")
        else "",
        "tier1_rank_quality_cap_reasons": str(tier1.get("rank_quality_cap_reasons") or ""),
        "tier1_rank_quality_cap_vetoed": 1 if as_bool(tier1.get("rank_quality_cap_vetoed"), False) else 0,
        "tier1_rank_quality_cap_veto_reasons": str(tier1.get("rank_quality_cap_veto_reasons") or ""),
        "tier1_mature_defensive_score": round(to_float(tier1.get("mature_defensive_score"), 0.0), 4)
        if tier1.get("mature_defensive_score") not in (None, "")
        else "",
        "tier1_expected_return_quality_score": round(to_float(tier1.get("expected_return_quality_score"), 0.0), 4)
        if tier1.get("expected_return_quality_score") not in (None, "")
        else "",
        "tier1_value_trap_score": round(to_float(tier1.get("value_trap_score"), 0.0), 4)
        if tier1.get("value_trap_score") not in (None, "")
        else "",
        "tier1_leverage_score": round(to_float(tier1.get("leverage_score"), 0.0), 4)
        if tier1.get("leverage_score") not in (None, "")
        else "",
        "tier1_leverage_fragility_score": round(to_float(tier1.get("leverage_fragility_score"), 0.0), 4)
        if tier1.get("leverage_fragility_score") not in (None, "")
        else "",
        "tier1_no_forward_guidance_flag": round(to_float(tier1.get("no_forward_guidance_flag"), 0.0), 4)
        if tier1.get("no_forward_guidance_flag") not in (None, "")
        else "",
        "tier1_guidance_stale_flag": round(to_float(guidance_stale, 0.0), 4)
        if guidance_stale not in (None, "")
        else "",
        "tier1_no_guidance_negative_growth_flag": round(to_float(tier1.get("no_guidance_negative_growth_flag"), 0.0), 4)
        if tier1.get("no_guidance_negative_growth_flag") not in (None, "")
        else "",
        "tier1_production_policy_quality_penalty": round(to_float(tier1.get("production_policy_quality_penalty"), 0.0), 4)
        if tier1.get("production_policy_quality_penalty") not in (None, "")
        else "",
        "tier1_production_policy_quality_bonus": round(to_float(tier1.get("production_policy_quality_bonus"), 0.0), 4)
        if tier1.get("production_policy_quality_bonus") not in (None, "")
        else "",
    }


def tier1_gate_values(tier1: dict[str, Any] | None, config: dict[str, Any]) -> tuple[float | None, float | None]:
    if not tier1:
        return None, None
    stored_gate = to_float(tier1.get("tier1_selection_gate_score"), math.nan)
    if math.isfinite(stored_gate):
        gate_score = clamp(stored_gate)
    else:
        opportunity = to_float(tier1.get("opportunity_score"), 0.0)
        risk = to_float(tier1.get("risk_score"), 100.0)
        gate_score = clamp(0.70 * opportunity + 0.30 * (100.0 - risk))
    min_multiplier = float(cfg_get(config, "multibagger.tier1_interaction.min_gate_multiplier", 0.55))
    multiplier = min_multiplier + (1.0 - min_multiplier) * gate_score / 100.0
    return gate_score, clamp(multiplier, min_multiplier, 1.0)


def tier1_reason_disabled(config: dict[str, Any], tier1: dict[str, Any] | None) -> str:
    if not as_bool(cfg_get(config, "multibagger.tier1_interaction.enabled", False), False):
        return "disabled"
    if not tier1:
        return "missing_tier1_context"
    return "pending_tier1_interaction"


def build_tier1_interaction_reason(
    row: dict[str, Any],
    config: dict[str, Any],
    *,
    residualization_status: str,
    pre_cap_score: float,
    final_score: float,
) -> str:
    if not as_bool(cfg_get(config, "multibagger.tier1_interaction.enabled", False), False):
        return "disabled"
    if not as_bool(row.get("tier1_available")):
        return "missing_tier1_context"

    reasons: list[str] = []
    tier1_bucket = str(row.get("tier1_bucket") or "").lower()
    tier1_risk = to_float(row.get("tier1_risk_score"), 0.0)
    tier1_tier = str(row.get("tier1_score_tier") or "").lower()
    tier1_rank_vetoed = as_bool(row.get("tier1_rank_quality_cap_vetoed"), False)
    gate_multiplier = to_float(row.get("tier1_gate_multiplier"), 1.0)
    veto_buckets = normalized_config_list(
        cfg_get(config, "multibagger.tier1_interaction.veto_buckets", ["avoid"]),
        ["avoid"],
    )
    tier1_risk_veto = float(cfg_get(config, "multibagger.tier1_interaction.tier1_risk_veto", 80.0))
    tier1_avoid_cap = float(cfg_get(config, "multibagger.tier1_interaction.tier1_avoid_score_cap", 49.0))
    tier1_risk_cap = float(cfg_get(config, "multibagger.tier1_interaction.tier1_risk_score_cap", 55.0))
    tier1_research_cap = float(cfg_get(config, "multibagger.tier1_interaction.tier1_research_watchlist_score_cap", 55.0))
    tier1_low_priority_cap = float(cfg_get(config, "multibagger.tier1_interaction.tier1_low_priority_score_cap", 45.0))

    if residualization_status:
        reasons.append(f"orthogonal_alpha={residualization_status}")
    if gate_multiplier < 0.9999:
        reasons.append(f"tier1_gate_multiplier={gate_multiplier:.4f}")
    if tier1_tier:
        reasons.append(f"tier1_score_tier={tier1_tier}")
    if tier1_rank_vetoed:
        reasons.append(f"tier1_rank_quality_cap_veto;cap<={tier1_avoid_cap:.1f}")
    if tier1_tier == "research_watchlist":
        reasons.append(f"tier1_research_watchlist_cap<={tier1_research_cap:.1f}")
    if tier1_tier == "low_priority":
        reasons.append(f"tier1_low_priority_cap<={tier1_low_priority_cap:.1f}")
    if tier1_bucket in veto_buckets:
        reasons.append(f"tier1_bucket_veto={tier1_bucket};cap<={tier1_avoid_cap:.1f}")
    if tier1_risk >= tier1_risk_veto:
        reasons.append(f"tier1_risk_veto={tier1_risk:.1f};cap<={tier1_risk_cap:.1f}")
    if final_score < pre_cap_score - 1e-6:
        reasons.append(f"score_after_caps={final_score:.4f};score_before_caps={pre_cap_score:.4f}")
    if not reasons:
        return "tier1_interaction_applied_no_penalty"
    return "|".join(reasons)


def bucket_for(
    score: float,
    risk: float,
    evidence: bool,
    liquidity_ok: bool,
    payload: dict[str, Any],
    config: dict[str, Any],
    tier1: dict[str, Any] | None = None,
) -> str:
    high_min = float(cfg_get(config, "multibagger.high_conviction_min", 80))
    watch_min = float(cfg_get(config, "multibagger.watchlist_min", 65))
    spec_min = float(cfg_get(config, "multibagger.speculative_min", 50))
    max_high_risk = float(cfg_get(config, "multibagger.max_high_conviction_risk", 35))
    max_watch_risk = float(cfg_get(config, "multibagger.max_watchlist_risk", 55))
    max_spec_risk = float(cfg_get(config, "multibagger.max_speculative_risk", 75))
    avoid_risk_min = float(cfg_get(config, "multibagger.avoid_risk_min", 75))
    large_cap_quality_max_risk = float(cfg_get(config, "multibagger.large_cap_quality_max_risk", max_watch_risk))
    avoid_fragility_min = float(cfg_get(config, "multibagger.avoid_fragility_min", 70))
    require_evidence = as_bool(cfg_get(config, "multibagger.require_event_or_catalyst", True), True)
    commercial = payload.get("commercial", {}) if isinstance(payload, dict) else {}
    components = payload.get("component_scores", {}) if isinstance(payload, dict) else {}
    market_cap = to_float(commercial.get("market_cap"), 0.0)
    fragility = to_float(components.get("commercial_fragility_risk_score"), 0.0)
    hard_cap = float(cfg_get(config, "multibagger.market_cap_hard_cap", 75_000_000_000))
    tier1_enabled = as_bool(cfg_get(config, "multibagger.tier1_interaction.enabled", False), False)

    if not liquidity_ok:
        return "avoid_illiquid"
    if tier1_enabled and tier1 and as_bool(tier1.get("tier1_available", True), True):
        tier1_bucket = str(tier1.get("tier1_bucket") or tier1.get("bucket") or "").lower()
        tier1_risk = to_float(tier1.get("tier1_risk_score", tier1.get("risk_score")), 0.0)
        tier1_tier = str(tier1.get("tier1_score_tier") or "").lower()
        veto_buckets = normalized_config_list(
            cfg_get(config, "multibagger.tier1_interaction.veto_buckets", ["avoid"]),
            ["avoid"],
        )
        tier1_risk_veto = float(cfg_get(config, "multibagger.tier1_interaction.tier1_risk_veto", 80.0))
        if as_bool(tier1.get("tier1_rank_quality_cap_vetoed"), False):
            return "avoid_tier1_rank_veto"
        if tier1_tier == "low_priority":
            return "avoid_tier1_low_priority"
        if tier1_bucket in veto_buckets:
            return "avoid_tier1_conflict"
        if tier1_risk >= tier1_risk_veto:
            return "avoid_tier1_risk"
    if risk >= avoid_risk_min:
        return "avoid_high_risk"
    if fragility >= avoid_fragility_min:
        return "avoid_commercial_fragility"
    if require_evidence and not evidence:
        return "avoid_no_event_or_catalyst"
    if market_cap >= hard_cap and score >= watch_min and risk <= large_cap_quality_max_risk:
        return "large_cap_quality"
    if score >= high_min and risk <= max_high_risk:
        return "high_conviction_multibagger"
    if score >= watch_min and risk <= max_watch_risk:
        return "multibagger_watchlist"
    if score >= spec_min and risk <= max_spec_risk:
        return "speculative_multibagger"
    return "avoid"


def score_one(row: dict[str, Any], config: dict[str, Any], tier1: dict[str, Any] | None = None) -> dict[str, Any]:
    weights = cfg_get(config, "multibagger.weights", {}) or {}
    commercial_w = float(weights.get("commercial_acceleration", 0.25))
    upside_w = float(weights.get("upside_capacity", 0.20))
    cash_flow_w = float(weights.get("cash_flow_acceleration", 0.15))
    survival_w = float(weights.get("survival_quality", 0.15))
    governance_w = float(weights.get("governance_event", 0.10))
    market_w = float(weights.get("market_confirmation", 0.10))
    catalyst_w = float(weights.get("catalyst_quality", 0.05))
    risk_w = float(weights.get("risk_penalty", 0.20))
    positive_weight_total = (
        commercial_w
        + upside_w
        + cash_flow_w
        + survival_w
        + governance_w
        + market_w
        + catalyst_w
    )
    if positive_weight_total <= 0.0:
        raise ValueError("multibagger.weights positive component total must be > 0")
    if abs(positive_weight_total - 1.0) > 1e-6:
        scale = 1.0 / positive_weight_total
        commercial_w *= scale
        upside_w *= scale
        cash_flow_w *= scale
        survival_w *= scale
        governance_w *= scale
        market_w *= scale
        catalyst_w *= scale

    commercial = clamp(to_float(row.get("commercial_acceleration_score")))
    upside = clamp(to_float(row.get("upside_capacity_score")))
    cash_flow = clamp(to_float(row.get("cash_flow_acceleration_score")))
    survival = clamp(to_float(row.get("survival_quality_score")))
    governance = clamp(to_float(row.get("governance_event_score")))
    market = clamp(to_float(row.get("market_confirmation_score")))
    catalyst = clamp(to_float(row.get("catalyst_quality_score")))
    fragility = clamp(to_float(row.get("commercial_fragility_risk_score")))
    risk = clamp(to_float(row.get("multibagger_risk_penalty")))
    payload = parse_json(row.get("payload_json"))

    positive = (
        commercial_w * commercial
        + upside_w * upside
        + cash_flow_w * cash_flow
        + survival_w * survival
        + governance_w * governance
        + market_w * market
        + catalyst_w * catalyst
    )
    risk_drag = convex_risk_drag(risk, risk_w, config, "multibagger")
    score = round(clamp(positive - risk_drag), 4)
    evidence = bool(to_int(row.get("evidence_or_catalyst_flag")))
    liquidity_ok = row_liquidity_ok(row, payload, config)
    gate_score, gate_multiplier = tier1_gate_values(tier1, config)
    tier1_context = tier1_context_values(tier1, gate_score, gate_multiplier)
    bucket = bucket_for(score, risk, evidence, liquidity_ok, payload, config, tier1_context)
    distinct_score = distinctive_acceleration_score(
        {
            "commercial_acceleration_score": commercial,
            "cash_flow_acceleration_score": cash_flow,
            "governance_event_score": governance,
            "market_confirmation_score": market,
            "commercial_fragility_risk_score": fragility,
        }
    )
    evidence_json = {
        "component_scores": {
            "commercial_acceleration_score": commercial,
            "upside_capacity_score": upside,
            "cash_flow_acceleration_score": cash_flow,
            "survival_quality_score": survival,
            "governance_event_score": governance,
            "market_confirmation_score": market,
            "catalyst_quality_score": catalyst,
            "commercial_fragility_risk_score": fragility,
            "multibagger_risk_penalty": risk,
            "risk_drag": round(risk_drag, 4),
            "positive_weight_total_before_normalization": round(positive_weight_total, 6),
        },
        "commercial": payload.get("commercial", {}),
        "survival": payload.get("survival", {}),
        "market": payload.get("market", {}),
        "governance": payload.get("governance", {}),
        "forward_guidance": payload.get("forward_guidance", {}),
        "clinical": {
            "primary_nct": payload.get("clinical", {}).get("primary_nct", "") if isinstance(payload.get("clinical", {}), dict) else "",
            "primary_trial_title": payload.get("clinical", {}).get("primary_trial_title", "") if isinstance(payload.get("clinical", {}), dict) else "",
            "lead_phase2_3_active_trials": payload.get("clinical", {}).get("lead_phase2_3_active_trials", 0) if isinstance(payload.get("clinical", {}), dict) else 0,
            "program_phase2_3_active_trials": payload.get("clinical", {}).get("program_phase2_3_active_trials", 0) if isinstance(payload.get("clinical", {}), dict) else 0,
            "active_pivotal_trials": payload.get("clinical", {}).get("active_pivotal_trials", 0) if isinstance(payload.get("clinical", {}), dict) else 0,
        },
        "risk": payload.get("risk", {}),
        "tier1_context": tier1_context,
        "source_dates": payload.get("source_dates", {}),
        "data_quality": row.get("data_quality", ""),
        "missing_fields": row.get("missing_fields", ""),
    }
    return {
        "asof_date": row["asof_date"],
        "company_id": int(row["company_id"]),
        "ticker": str(row["ticker"] or ""),
        "company_name": str(row["company_name"] or ""),
        "base_multibagger_score": score,
        "multibagger_score": score,
        "orthogonal_alpha_score": None,
        "distinctive_acceleration_score": round(distinct_score, 4),
        "tier1_opportunity_score": tier1_context["tier1_opportunity_score"],
        "tier1_risk_score": tier1_context["tier1_risk_score"],
        "tier1_bucket": tier1_context["tier1_bucket"],
        "tier1_gate_score": tier1_context["tier1_gate_score"],
        "tier1_gate_multiplier": tier1_context["tier1_gate_multiplier"],
        "tier1_available": 1 if tier1_context["tier1_available"] else 0,
        "tier1_interaction_reason": tier1_reason_disabled(config, tier1),
        "tier1_score_tier": tier1_context["tier1_score_tier"],
        "tier1_allocation_eligible": tier1_context["tier1_allocation_eligible"],
        "tier1_research_watchlist": tier1_context["tier1_research_watchlist"],
        "tier1_score_spread_to_allocation": tier1_context["tier1_score_spread_to_allocation"],
        "tier1_score_spread_to_high_confidence": tier1_context["tier1_score_spread_to_high_confidence"],
        "tier1_rank_quality_cap": tier1_context["tier1_rank_quality_cap"],
        "tier1_rank_quality_cap_reasons": tier1_context["tier1_rank_quality_cap_reasons"],
        "tier1_rank_quality_cap_vetoed": tier1_context["tier1_rank_quality_cap_vetoed"],
        "tier1_rank_quality_cap_veto_reasons": tier1_context["tier1_rank_quality_cap_veto_reasons"],
        "tier1_mature_defensive_score": tier1_context["tier1_mature_defensive_score"],
        "tier1_expected_return_quality_score": tier1_context["tier1_expected_return_quality_score"],
        "tier1_value_trap_score": tier1_context["tier1_value_trap_score"],
        "tier1_leverage_score": tier1_context["tier1_leverage_score"],
        "tier1_leverage_fragility_score": tier1_context["tier1_leverage_fragility_score"],
        "tier1_no_forward_guidance_flag": tier1_context["tier1_no_forward_guidance_flag"],
        "tier1_guidance_stale_flag": tier1_context["tier1_guidance_stale_flag"],
        "tier1_no_guidance_negative_growth_flag": tier1_context["tier1_no_guidance_negative_growth_flag"],
        "tier1_production_policy_quality_penalty": tier1_context["tier1_production_policy_quality_penalty"],
        "tier1_production_policy_quality_bonus": tier1_context["tier1_production_policy_quality_bonus"],
        "commercial_acceleration_score": commercial,
        "cash_flow_acceleration_score": cash_flow,
        "governance_event_score": governance,
        "market_confirmation_score": market,
        "commercial_fragility_risk_score": fragility,
        "multibagger_risk_penalty": risk,
        "evidence_or_catalyst_flag": int(evidence),
        "liquidity_ok": 1 if liquidity_ok else 0,
        "payload_json": row.get("payload_json"),
        "rank": 0,
        "bucket": bucket,
        "top_evidence_json": json.dumps(evidence_json, ensure_ascii=True, sort_keys=True),
    }


def percentile_scores(indexed_values: dict[int, float]) -> dict[int, float]:
    if not indexed_values:
        return {}
    if len(indexed_values) == 1:
        return {next(iter(indexed_values)): 100.0}
    sorted_items = sorted(indexed_values.items(), key=lambda item: item[1])
    scores: dict[int, float] = {}
    n = len(sorted_items)
    i = 0
    while i < n:
        j = i
        while j + 1 < n and sorted_items[j + 1][1] == sorted_items[i][1]:
            j += 1
        percentile = 100.0 * ((i + j) / 2.0) / float(n - 1)
        for k in range(i, j + 1):
            scores[sorted_items[k][0]] = percentile
        i = j + 1
    return scores


def solve_linear_system(matrix: list[list[float]], vector: list[float]) -> list[float] | None:
    n = len(vector)
    augmented = [row[:] + [rhs] for row, rhs in zip(matrix, vector)]
    for col in range(n):
        pivot = max(range(col, n), key=lambda row_idx: abs(augmented[row_idx][col]))
        if abs(augmented[pivot][col]) < 1e-10:
            return None
        augmented[col], augmented[pivot] = augmented[pivot], augmented[col]
        scale = augmented[col][col]
        augmented[col] = [value / scale for value in augmented[col]]
        for row_idx in range(n):
            if row_idx == col:
                continue
            factor = augmented[row_idx][col]
            augmented[row_idx] = [
                value - factor * pivot_value
                for value, pivot_value in zip(augmented[row_idx], augmented[col])
            ]
    return [augmented[row_idx][-1] for row_idx in range(n)]


def orthogonal_alpha_scores(rows: list[dict[str, Any]], config: dict[str, Any]) -> tuple[dict[int, float], str]:
    min_rows = int(cfg_get(config, "multibagger.tier1_interaction.min_residualization_rows", 10))
    ridge = float(cfg_get(config, "multibagger.tier1_interaction.residualization_ridge", 1e-6))
    leave_one_out = as_bool(cfg_get(config, "multibagger.tier1_interaction.leave_one_out_residualization", True), True)
    predictor_keys = ["tier1_opportunity_score", "tier1_risk_score"]
    observations: list[tuple[int, float, list[float]]] = []
    for idx, row in enumerate(rows):
        if not as_bool(row.get("tier1_available")):
            continue
        predictors = [to_float(row.get(key), math.nan) for key in predictor_keys]
        if any(not math.isfinite(value) for value in predictors):
            continue
        observations.append((idx, to_float(row.get("base_multibagger_score")), predictors))
    if len(observations) < min_rows:
        return ({idx: to_float(row.get("base_multibagger_score")) for idx, row in enumerate(rows)}, "not_enough_tier1_rows")

    def fit_model(training: list[tuple[int, float, list[float]]]) -> tuple[list[float], list[float], list[float]] | None:
        means = [sum(obs[2][col] for obs in training) / len(training) for col in range(len(predictor_keys))]
        stdevs = []
        for col, mean in enumerate(means):
            variance = sum((obs[2][col] - mean) ** 2 for obs in training) / max(1, len(training) - 1)
            stdevs.append(max(math.sqrt(variance), 1e-6))
        size = len(predictor_keys) + 1
        xtx = [[0.0 for _ in range(size)] for _ in range(size)]
        xty = [0.0 for _ in range(size)]
        for _, y, predictors in training:
            x = [1.0] + [(predictors[col] - means[col]) / stdevs[col] for col in range(len(predictor_keys))]
            for i in range(size):
                xty[i] += x[i] * y
                for j in range(size):
                    xtx[i][j] += x[i] * x[j]
        for i in range(1, size):
            xtx[i][i] += ridge
        beta = solve_linear_system(xtx, xty)
        return None if beta is None else (means, stdevs, beta)

    residuals: dict[int, float] = {}
    failed_fits = 0
    if leave_one_out and len(observations) > min_rows:
        for obs_idx, (idx, y, predictors) in enumerate(observations):
            model = fit_model(observations[:obs_idx] + observations[obs_idx + 1 :])
            if model is None:
                failed_fits += 1
                continue
            means, stdevs, beta = model
            x = [1.0] + [(predictors[col] - means[col]) / stdevs[col] for col in range(len(predictor_keys))]
            residuals[idx] = y - sum(coef * value for coef, value in zip(beta, x))
        if residuals:
            status = "applied_leave_one_out" if failed_fits == 0 else f"applied_leave_one_out_partial_failed_fits={failed_fits}"
            return percentile_scores(residuals), status

    model = fit_model(observations)
    if model is None:
        return ({idx: to_float(row.get("base_multibagger_score")) for idx, row in enumerate(rows)}, "singular_residualization")
    means, stdevs, beta = model
    for idx, y, predictors in observations:
        x = [1.0] + [(predictors[col] - means[col]) / stdevs[col] for col in range(len(predictor_keys))]
        residuals[idx] = y - sum(coef * value for coef, value in zip(beta, x))
    return percentile_scores(residuals), "applied_in_sample"


def distinctive_acceleration_score(row: dict[str, Any]) -> float:
    return clamp(
        0.30 * to_float(row.get("commercial_acceleration_score"))
        + 0.25 * to_float(row.get("cash_flow_acceleration_score"))
        + 0.20 * to_float(row.get("governance_event_score"))
        + 0.15 * to_float(row.get("market_confirmation_score"))
        + 0.10 * (100.0 - to_float(row.get("commercial_fragility_risk_score")))
    )


def apply_tier1_interaction(rows: list[dict[str, Any]], config: dict[str, Any]) -> None:
    if not rows or not as_bool(cfg_get(config, "multibagger.tier1_interaction.enabled", False), False):
        return
    alpha_scores, residualization_status = orthogonal_alpha_scores(rows, config)
    requested_alpha_weight = float(cfg_get(config, "multibagger.tier1_interaction.orthogonal_alpha_weight", 0.30))
    distinctive_weight = float(cfg_get(config, "multibagger.tier1_interaction.distinctive_acceleration_weight", 0.15))
    alpha_available = residualization_status.startswith("applied_leave_one_out")
    alpha_weight = requested_alpha_weight if alpha_available else 0.0
    base_weight = max(0.0, 1.0 - alpha_weight - distinctive_weight)
    veto_buckets = normalized_config_list(
        cfg_get(config, "multibagger.tier1_interaction.veto_buckets", ["avoid"]),
        ["avoid"],
    )
    tier1_avoid_cap = float(cfg_get(config, "multibagger.tier1_interaction.tier1_avoid_score_cap", 49.0))
    tier1_risk_cap = float(cfg_get(config, "multibagger.tier1_interaction.tier1_risk_score_cap", 55.0))
    tier1_risk_veto = float(cfg_get(config, "multibagger.tier1_interaction.tier1_risk_veto", 80.0))
    tier1_research_cap = float(cfg_get(config, "multibagger.tier1_interaction.tier1_research_watchlist_score_cap", 55.0))
    tier1_low_priority_cap = float(cfg_get(config, "multibagger.tier1_interaction.tier1_low_priority_score_cap", 45.0))

    for idx, row in enumerate(rows):
        alpha_score = clamp(alpha_scores[idx]) if alpha_available and idx in alpha_scores else None
        row_alpha_weight = alpha_weight if alpha_score is not None else 0.0
        row_base_weight = base_weight + (alpha_weight if alpha_score is None else 0.0)
        distinct_score = distinctive_acceleration_score(row)
        gate_multiplier = to_float(row.get("tier1_gate_multiplier"), 1.0)
        pre_gate = (
            row_base_weight * to_float(row.get("base_multibagger_score"))
            + row_alpha_weight * (alpha_score if alpha_score is not None else 0.0)
            + distinctive_weight * distinct_score
        )
        final_score = clamp(pre_gate * gate_multiplier)
        pre_cap_score = final_score
        tier1_bucket = str(row.get("tier1_bucket") or "").lower()
        tier1_risk = to_float(row.get("tier1_risk_score"), 0.0)
        tier1_tier = str(row.get("tier1_score_tier") or "").lower()
        if as_bool(row.get("tier1_available")) and as_bool(row.get("tier1_rank_quality_cap_vetoed"), False):
            final_score = min(final_score, tier1_avoid_cap)
        if as_bool(row.get("tier1_available")) and tier1_tier == "research_watchlist":
            final_score = min(final_score, tier1_research_cap)
        if as_bool(row.get("tier1_available")) and tier1_tier == "low_priority":
            final_score = min(final_score, tier1_low_priority_cap)
        if as_bool(row.get("tier1_available")) and tier1_bucket in veto_buckets:
            final_score = min(final_score, tier1_avoid_cap)
        if as_bool(row.get("tier1_available")) and tier1_risk >= tier1_risk_veto:
            final_score = min(final_score, tier1_risk_cap)

        row["orthogonal_alpha_score"] = round(alpha_score, 4) if alpha_score is not None else None
        row["distinctive_acceleration_score"] = round(distinct_score, 4)
        row["multibagger_score"] = round(final_score, 4)
        row["tier1_interaction_reason"] = build_tier1_interaction_reason(
            row,
            config,
            residualization_status=residualization_status,
            pre_cap_score=pre_cap_score,
            final_score=final_score,
        )
        payload = parse_json(row.get("payload_json"))
        row["bucket"] = bucket_for(
            final_score,
            to_float(row.get("multibagger_risk_penalty")),
            bool(to_int(row.get("evidence_or_catalyst_flag"))),
            row_liquidity_ok(row, payload, config),
            payload,
            config,
            row,
        )
        evidence_json = parse_json(row.get("top_evidence_json"))
        evidence_json["tier1_interaction"] = {
            "enabled": True,
            "residualization_status": residualization_status,
            "orthogonal_alpha_available": alpha_available,
            "requested_orthogonal_alpha_weight": requested_alpha_weight,
            "effective_orthogonal_alpha_weight": row_alpha_weight,
            "effective_base_multibagger_weight": row_base_weight,
            "base_multibagger_score": row.get("base_multibagger_score"),
            "orthogonal_alpha_score": row.get("orthogonal_alpha_score"),
            "distinctive_acceleration_score": row.get("distinctive_acceleration_score"),
            "tier1_gate_score": row.get("tier1_gate_score"),
            "tier1_gate_multiplier": row.get("tier1_gate_multiplier"),
            "tier1_score_tier": row.get("tier1_score_tier"),
            "tier1_allocation_eligible": row.get("tier1_allocation_eligible"),
            "tier1_research_watchlist": row.get("tier1_research_watchlist"),
            "tier1_score_spread_to_allocation": row.get("tier1_score_spread_to_allocation"),
            "tier1_score_spread_to_high_confidence": row.get("tier1_score_spread_to_high_confidence"),
            "tier1_rank_quality_cap": row.get("tier1_rank_quality_cap"),
            "tier1_rank_quality_cap_reasons": row.get("tier1_rank_quality_cap_reasons"),
            "tier1_rank_quality_cap_vetoed": row.get("tier1_rank_quality_cap_vetoed"),
            "tier1_rank_quality_cap_veto_reasons": row.get("tier1_rank_quality_cap_veto_reasons"),
            "tier1_mature_defensive_score": row.get("tier1_mature_defensive_score"),
            "tier1_expected_return_quality_score": row.get("tier1_expected_return_quality_score"),
            "tier1_value_trap_score": row.get("tier1_value_trap_score"),
            "tier1_leverage_score": row.get("tier1_leverage_score"),
            "tier1_leverage_fragility_score": row.get("tier1_leverage_fragility_score"),
            "tier1_no_forward_guidance_flag": row.get("tier1_no_forward_guidance_flag"),
            "tier1_guidance_stale_flag": row.get("tier1_guidance_stale_flag"),
            "tier1_no_guidance_negative_growth_flag": row.get("tier1_no_guidance_negative_growth_flag"),
            "tier1_production_policy_quality_penalty": row.get("tier1_production_policy_quality_penalty"),
            "tier1_production_policy_quality_bonus": row.get("tier1_production_policy_quality_bonus"),
            "tier1_interaction_reason": row.get("tier1_interaction_reason"),
            "final_multibagger_score": row.get("multibagger_score"),
            "method": "residualize_base_score_vs_tier1_opportunity_and_risk_then_gate_by_tier1_quality",
        }
        row["top_evidence_json"] = json.dumps(evidence_json, ensure_ascii=True, sort_keys=True)


def flatten_for_csv(score_row: dict[str, Any], feature_row: dict[str, Any]) -> dict[str, Any]:
    payload = parse_json(feature_row.get("payload_json"))
    commercial = payload.get("commercial", {}) if isinstance(payload, dict) else {}
    survival = payload.get("survival", {}) if isinstance(payload, dict) else {}
    market = payload.get("market", {}) if isinstance(payload, dict) else {}
    governance = payload.get("governance", {}) if isinstance(payload, dict) else {}
    forward = payload.get("forward_guidance", {}) if isinstance(payload, dict) else {}
    clinical = payload.get("clinical", {}) if isinstance(payload, dict) else {}
    return {
        **score_row,
        "commercial_acceleration_score": feature_row.get("commercial_acceleration_score"),
        "upside_capacity_score": feature_row.get("upside_capacity_score"),
        "cash_flow_acceleration_score": feature_row.get("cash_flow_acceleration_score"),
        "survival_quality_score": feature_row.get("survival_quality_score"),
        "governance_event_score": feature_row.get("governance_event_score"),
        "market_confirmation_score": feature_row.get("market_confirmation_score"),
        "catalyst_quality_score": feature_row.get("catalyst_quality_score"),
        "commercial_fragility_risk_score": feature_row.get("commercial_fragility_risk_score"),
        "multibagger_risk_penalty": feature_row.get("multibagger_risk_penalty"),
        "ttm_revenue": commercial.get("ttm_revenue"),
        "revenue_yoy_growth_pct": commercial.get("revenue_yoy_growth_pct"),
        "free_cash_flow_ttm": commercial.get("free_cash_flow_ttm"),
        "fcf_yield": commercial.get("fcf_yield"),
        "market_cap": commercial.get("market_cap"),
        "ev_to_sales": commercial.get("ev_to_sales"),
        "pe_ratio": commercial.get("pe_ratio"),
        "commercial_value_score": commercial.get("commercial_value_score"),
        "valuation_score": commercial.get("valuation_score"),
        "quality_adjusted_valuation_score": commercial.get("quality_adjusted_valuation_score"),
        "institutional_upside_capacity_score": commercial.get("institutional_upside_capacity_score"),
        "value_trap_score": commercial.get("value_trap_score"),
        "leverage_score": commercial.get("leverage_score"),
        "cash_runway_months": survival.get("cash_runway_months"),
        "forward_revenue_midpoint": forward.get("forward_revenue_midpoint"),
        "forward_revenue_growth_pct": forward.get("forward_revenue_growth_pct"),
        "forward_ebitda_midpoint": forward.get("forward_ebitda_midpoint"),
        "forward_eps_midpoint": forward.get("forward_eps_midpoint"),
        "guidance_score": forward.get("guidance_score"),
        "quality_forward_valuation_score": forward.get("quality_forward_valuation_score"),
        "quality_adjusted_guidance_score": forward.get("quality_adjusted_guidance_score"),
        "guidance_recency_days": forward.get("guidance_recency_days"),
        "guidance_recency_penalty": forward.get("guidance_recency_penalty"),
        "insider_buy_count_90d": governance.get("insider_buy_count_90d"),
        "insider_buy_value_90d": governance.get("insider_buy_value_90d"),
        "buyback_event_count_365d": governance.get("buyback_event_count_365d"),
        "asr_event_count_365d": governance.get("asr_event_count_365d"),
        "relative_strength_3m_vs_xbi": market.get("relative_strength_3m_vs_xbi"),
        "price_vs_200d_pct": market.get("price_vs_200d_pct"),
        "distance_from_52w_high_pct": market.get("distance_from_52w_high_pct"),
        "avg_dollar_volume_20d": market.get("avg_dollar_volume_20d"),
        "primary_nct": clinical.get("primary_nct"),
        "lead_phase2_3_active_trials": clinical.get("lead_phase2_3_active_trials"),
        "program_phase2_3_active_trials": clinical.get("program_phase2_3_active_trials"),
        "active_pivotal_trials": clinical.get("active_pivotal_trials"),
        "evidence_or_catalyst_flag": feature_row.get("evidence_or_catalyst_flag"),
        "data_quality": feature_row.get("data_quality"),
        "missing_fields": feature_row.get("missing_fields"),
    }


def upsert_scores(conn: sqlite3.Connection, rows: list[dict[str, Any]], asof_date: str) -> None:
    now = utc_now()
    placeholders = ", ".join("?" for _ in SCORE_FIELDS)
    missing_by_row = [
        (str(row.get("ticker") or row.get("company_id") or "<unknown>"), [field for field in SCORE_FIELDS if field not in row])
        for row in rows
        if any(field not in row for field in SCORE_FIELDS)
    ]
    if missing_by_row:
        sample = "; ".join(f"{label}: {','.join(fields)}" for label, fields in missing_by_row[:5])
        raise ValueError(f"multibagger score rows missing required field(s): {sample}")
    with conn:
        conn.execute("DELETE FROM multibagger_scores_daily WHERE asof_date = ?", (asof_date,))
        conn.executemany(
            f"""
            INSERT INTO multibagger_scores_daily({", ".join(SCORE_FIELDS)}, created_at, updated_at)
            VALUES ({placeholders}, ?, ?)
            """,
            [tuple(row[field] for field in SCORE_FIELDS) + (now, now) for row in rows],
        )


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS, lineterminator="\n", extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    configure_logging()
    args = parse_args()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    base_dir = config_path.parent
    db_path = args.db.expanduser().resolve() if args.db else resolve_path(cfg_get(config, "paths.database_path"), base_dir=base_dir)
    output_dir = resolve_path(cfg_get(config, "multibagger.output_dir"), base_dir=base_dir)
    output_csv = output_dir / str(cfg_get(config, "multibagger.scores_csv", "biotech_multibagger_scores.csv"))
    sqlite_timeout_sec = float(cfg_get(config, "runtime.sqlite_timeout_sec", 30.0))

    with connect(db_path, timeout_sec=sqlite_timeout_sec) as conn:
        run_id: int | None = None
        init_db(conn)
        asof_obj = parse_date(args.asof) if args.asof else None
        if args.asof and asof_obj is None:
            raise ValueError(f"Invalid --asof date: {args.asof}")
        asof_date = asof_obj.isoformat() if asof_obj else latest_feature_date(conn)
        run_id = start_run(conn, run_type="score_multibagger_candidates", input_path=db_path)
        try:
            feature_rows = load_feature_rows(conn, asof_date)
            if not feature_rows:
                raise ValueError(f"No multibagger_features_daily rows found for asof_date={asof_date}")
            tier1_by_company = load_tier1_score_rows(conn, asof_date)
            tier1_enabled = as_bool(cfg_get(config, "multibagger.tier1_interaction.enabled", False), False)
            missing_tier1 = [
                str(row["ticker"])
                for row in feature_rows
                if int(row["company_id"]) not in tier1_by_company
            ]
            if tier1_enabled and missing_tier1 and as_bool(cfg_get(config, "multibagger.tier1_interaction.fail_on_missing_tier1", True), True):
                raise RuntimeError(
                    "multibagger Tier 1 interaction enabled but no daily_scores row exists on or before asof for: "
                    + ",".join(sorted(missing_tier1)[:25])
                    + (f"...(+{len(missing_tier1) - 25})" if len(missing_tier1) > 25 else "")
                )
            max_staleness_days = int(cfg_get(config, "multibagger.tier1_interaction.max_staleness_days", 7))
            stale_tier1: list[str] = []
            if max_staleness_days < 0:
                raise ValueError("multibagger.tier1_interaction.max_staleness_days must be >= 0")
            if tier1_enabled:
                target_date = parse_date(asof_date)
                invalid_tier1_dates: list[str] = []
                for row in feature_rows:
                    tier1 = tier1_by_company.get(int(row["company_id"]))
                    tier1_date = parse_date(tier1.get("asof_date")) if tier1 else None
                    if tier1 and tier1_date is None:
                        invalid_tier1_dates.append(str(row["ticker"]))
                        continue
                    if target_date is not None and tier1_date is not None and (target_date - tier1_date).days > max_staleness_days:
                        stale_tier1.append(str(row["ticker"]))
                if invalid_tier1_dates:
                    LOGGER.warning(
                        "Invalid daily_scores asof_date for %d multibagger Tier 1 row(s); sample=%s",
                        len(invalid_tier1_dates),
                        ",".join(sorted(invalid_tier1_dates)[:10]),
                    )
                if stale_tier1 and as_bool(cfg_get(config, "multibagger.tier1_interaction.fail_on_missing_tier1", True), True):
                    raise RuntimeError(
                        "multibagger Tier 1 interaction enabled but latest daily_scores row(s) are stale: "
                        + ",".join(sorted(stale_tier1)[:25])
                        + (f"...(+{len(stale_tier1) - 25})" if len(stale_tier1) > 25 else "")
                    )
            if not tier1_by_company:
                LOGGER.warning("No daily_scores rows found for asof_date=%s; multibagger Tier 1 context will be blank", asof_date)
            feature_by_company = {int(row["company_id"]): row for row in feature_rows}
            scored = [score_one(row, config, tier1_by_company.get(int(row["company_id"]))) for row in feature_rows]
            apply_tier1_interaction(scored, config)
            scored.sort(key=lambda item: (-to_float(item["multibagger_score"]), str(item["ticker"])))
            for idx, row in enumerate(scored, start=1):
                row["rank"] = idx
            upsert_scores(conn, scored, asof_date)
            csv_rows = [flatten_for_csv(row, feature_by_company[int(row["company_id"])]) for row in scored]
            write_csv(output_csv, csv_rows)
            finish_run(conn, run_id=run_id, status="success", row_count=len(scored), message=f"asof={asof_date} output={output_csv}")
            LOGGER.info("Multibagger scoring complete: rows=%d output=%s", len(scored), output_csv)
        except BaseException as exc:
            if run_id is not None and not (isinstance(exc, SystemExit) and exc.code in (0, None)):
                finish_run(conn, run_id=run_id, status="failed", row_count=0, message=f"{type(exc).__name__}: {exc}")
            raise


if __name__ == "__main__":
    main()
