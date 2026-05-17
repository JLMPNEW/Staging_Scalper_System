#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import logging
import math
import sqlite3
import sys
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from biotech_index.core.config import cfg_get, load_yaml, resolve_path  # noqa: E402
from biotech_index.core.db import connect, finish_run, init_db, start_run, utc_now  # noqa: E402
from biotech_index.core.logging_utils import configure_utc_logging  # noqa: E402
from biotech_index.core.pipeline_guards import (  # noqa: E402
    read_final_scoring_tickers,
    validate_full_universe_coverage,
    validate_layer_freshness,
)


LOGGER = logging.getLogger("score_biotech_index")
DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
CORE_STRUCTURAL_VETO_DEFAULT_REASONS = [
    "cash_runway_lt_9m",
    "severe_runway_flag",
    "going_concern_confirmed",
    "reverse_split_history",
    "no_active_trial_no_business_anchor",
    "illiquid",
]
EVENT_HARD_WEAKNESS_DEFAULT_REASONS = [
    "negative_clinical_event",
    "repeated_dilution",
]
SOFT_WEAKNESS_DEFAULT_REASONS = [
    "cash_runway_9_to_12m_clinical",
    "going_concern_warning",
    "single_dilution_event",
    "low_financial_data_quality",
    "high_commercial_fragility",
    "high_tier1_risk_score",
    "recent_nt_filing",
    "early_stage_or_unadvanced_trial_anchor",
]
GOING_CONCERN_HARD_STATUSES = frozenset({"confirmed"})
GOING_CONCERN_SOFT_STATUSES = frozenset({"possible", "substantial_doubt", "going_concern_warning"})


@dataclass(frozen=True)
class BucketParams:
    high_min: float
    watch_min: float
    spec_min: float
    max_high_risk: float
    max_watch_risk: float
    max_spec_risk: float
    avoid_risk_min: float
    min_high_runway: float
    require_advanced: bool
    require_active_watch: bool


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Score the Tier-1 biotech opportunity index.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--asof", type=str, default="", help="Score date in YYYY-MM-DD. Defaults to latest features date.")
    return parser.parse_args()


def configure_logging() -> None:
    configure_utc_logging()


def parse_date(raw: object) -> date | None:
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        return datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError:
        return None


def compact_asof(asof_date: str) -> str:
    parsed = parse_date(asof_date)
    return parsed.strftime("%Y%m%d") if parsed is not None else str(asof_date).replace("-", "")


def dated_output_dir(base_output_dir: Path, asof_date: str) -> Path:
    compact = compact_asof(asof_date)
    return base_output_dir if base_output_dir.name == compact else base_output_dir / compact


def resolve_report_input_csv(configured_path: Path, *, base_output_dir: Path, asof_date: str) -> Path:
    if configured_path.exists():
        return configured_path
    dated_candidate = dated_output_dir(base_output_dir, asof_date) / configured_path.name
    if dated_candidate.exists():
        return dated_candidate
    candidates = sorted(
        base_output_dir.glob(f"*/{configured_path.name}"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    if candidates:
        LOGGER.warning(
            "Configured report input not found at %s or %s; using latest dated copy %s",
            configured_path,
            dated_candidate,
            candidates[0],
        )
        return candidates[0]
    return configured_path


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


def finite_value_or_blank(raw: object) -> float | str:
    value = to_float(raw, math.nan)
    return value if math.isfinite(value) else ""


def finite_value_or_none(raw: object) -> float | None:
    value = to_float(raw, math.nan)
    return value if math.isfinite(value) else None


def parse_json(raw: object) -> dict[str, Any]:
    try:
        payload = json.loads(str(raw or "{}"))
    except json.JSONDecodeError as exc:
        LOGGER.warning("Malformed JSON payload skipped: %s", exc)
        return {}
    return payload if isinstance(payload, dict) else {}


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
    return default


def parse_string_list(raw: object, default: list[str] | None = None) -> list[str]:
    if raw is None:
        return list(default or [])
    if isinstance(raw, str):
        parts = raw.replace(";", ",").replace("|", ",").split(",")
    elif isinstance(raw, (list, tuple, set)):
        parts = [str(item) for item in raw]
    else:
        parts = [str(raw)]
    values = [part.strip() for part in parts if part.strip()]
    return values or list(default or [])


def count_missing_fields(raw: object) -> int:
    if raw is None:
        return 0
    if isinstance(raw, (list, tuple, set)):
        return len(raw)
    if isinstance(raw, dict):
        return len(raw)
    text = str(raw or "").strip()
    if not text or text in {"[]", "{}", "null", "None"}:
        return 0
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        payload = None
    if isinstance(payload, (list, tuple, set)):
        return len(payload)
    if isinstance(payload, dict):
        return len(payload)
    return len([part for part in text.replace(";", ",").replace("|", ",").split(",") if part.strip()])


def convex_risk_drag(risk: float, weight: float, config: dict[str, Any], section: str) -> float:
    base_drag = weight * risk
    if not as_bool(cfg_get(config, f"{section}.convex_risk_penalty_enabled", False), False):
        return base_drag
    convexity = float(cfg_get(config, f"{section}.risk_penalty_convexity", 0.35))
    inflection = float(cfg_get(config, f"{section}.risk_penalty_inflection", 50.0))
    excess = max(0.0, risk - inflection) / max(1.0, 100.0 - inflection)
    return base_drag * (1.0 + convexity * excess)


def score_confidence_multiplier(
    config: dict[str, Any],
    payload: dict[str, Any],
    commercial: dict[str, Any],
    forward_guidance: dict[str, Any],
    profile_name: str,
) -> float:
    if not as_bool(cfg_get(config, "biotech_scoring.data_quality_adjustment.enabled", False), False):
        return 1.0
    min_multiplier = float(cfg_get(config, "biotech_scoring.data_quality_adjustment.min_multiplier", 0.82))
    low_quality_penalty = float(cfg_get(config, "biotech_scoring.data_quality_adjustment.low_quality_penalty", 0.06))
    missing_field_penalty = float(cfg_get(config, "biotech_scoring.data_quality_adjustment.missing_field_penalty", 0.006))
    max_missing_penalty = float(cfg_get(config, "biotech_scoring.data_quality_adjustment.max_missing_penalty", 0.08))

    survival = payload.get("financial_survival", {}) if isinstance(payload, dict) else {}
    qualities = [str(survival.get("data_quality") or "").lower()]
    commercial_stage = bool(to_float(commercial.get("commercial_stage_flag"), 0.0))
    profitable = bool(to_float(commercial.get("profitable_flag"), 0.0))
    has_guidance = bool(str(forward_guidance.get("latest_guidance_filing_date") or "").strip())
    if profile_name == "commercial_stage" or commercial_stage or profitable:
        qualities.append(str(commercial.get("data_quality") or "").lower())
    if has_guidance:
        qualities.append(str(forward_guidance.get("data_quality") or "").lower())

    low_quality_count = sum(1 for quality in qualities if quality in {"low", "poor", "stale"})
    missing_count = 0
    if profile_name == "commercial_stage" or commercial_stage or profitable:
        missing_count += count_missing_fields(commercial.get("missing_fields"))
    if has_guidance:
        missing_count += count_missing_fields(forward_guidance.get("missing_fields"))
    penalty = low_quality_count * low_quality_penalty + min(max_missing_penalty, missing_count * missing_field_penalty)
    return clamp(1.0 - penalty, min_multiplier, 1.0)


def tier1_selection_gate_score(opportunity: float, risk: float) -> float:
    return clamp(0.70 * opportunity + 0.30 * (100.0 - risk))


def core_structural_veto_settings(config: dict[str, Any]) -> dict[str, Any]:
    min_addv20 = float(
        cfg_get(
            config,
            "biotech_scoring.core_structural_veto.min_addv20",
            cfg_get(config, "multibagger.min_addv20", 1_000_000.0),
        )
    )
    return {
        "enabled": as_bool(cfg_get(config, "biotech_scoring.core_structural_veto.enabled", False), False),
        "apply_to_rank": as_bool(cfg_get(config, "biotech_scoring.core_structural_veto.apply_to_rank", True), True),
        "force_avoid_bucket": as_bool(
            cfg_get(config, "biotech_scoring.core_structural_veto.force_avoid_bucket", True),
            True,
        ),
        "min_addv20": min_addv20,
        "reasons": set(
            parse_string_list(
                cfg_get(config, "biotech_scoring.core_structural_veto.reasons", CORE_STRUCTURAL_VETO_DEFAULT_REASONS),
                CORE_STRUCTURAL_VETO_DEFAULT_REASONS,
            )
        ),
    }


def bucket_params(config: dict[str, Any]) -> BucketParams:
    return BucketParams(
        high_min=float(cfg_get(config, "biotech_scoring.buckets.high_conviction_min", 80)),
        watch_min=float(cfg_get(config, "biotech_scoring.buckets.watchlist_min", 60)),
        spec_min=float(cfg_get(config, "biotech_scoring.buckets.speculative_min", 45)),
        max_high_risk=float(cfg_get(config, "biotech_scoring.buckets.max_high_conviction_risk", 35)),
        max_watch_risk=float(cfg_get(config, "biotech_scoring.buckets.max_watchlist_risk", 50)),
        max_spec_risk=float(cfg_get(config, "biotech_scoring.buckets.max_speculative_risk", 75)),
        avoid_risk_min=float(cfg_get(config, "biotech_scoring.buckets.avoid_risk_min", 80)),
        min_high_runway=float(cfg_get(config, "biotech_scoring.buckets.high_conviction_min_runway_months", 12)),
        require_advanced=as_bool(
            cfg_get(config, "biotech_scoring.buckets.require_advanced_catalyst_for_high_conviction", True),
            True,
        ),
        require_active_watch=as_bool(
            cfg_get(config, "biotech_scoring.buckets.require_active_trial_for_watchlist", True),
            True,
        ),
    )


def tier1_production_baseline(config: dict[str, Any]) -> dict[str, Any]:
    return {
        "enabled": as_bool(cfg_get(config, "biotech_scoring.production_baseline.enabled", True), True),
        "score_model": str(cfg_get(config, "biotech_scoring.production_baseline.score_model", "biotech_opportunity_score")),
        "candidate_id": str(cfg_get(config, "biotech_scoring.production_baseline.candidate_id", "")),
        "candidate_name": str(cfg_get(config, "biotech_scoring.production_baseline.candidate_name", "")),
        "top_n": int(float(cfg_get(config, "biotech_scoring.production_baseline.top_n", 20))),
        "selection_policy": str(
            cfg_get(config, "biotech_scoring.production_baseline.selection_policy", "core_structural_veto")
        ),
        "primary_horizon_trading_days": int(
            float(cfg_get(config, "biotech_scoring.production_baseline.primary_horizon_trading_days", 120))
        ),
        "alpha_multibagger_role": str(
            cfg_get(config, "biotech_scoring.production_baseline.alpha_multibagger_role", "context_only")
        ),
        "calibration_output_dir": str(
            cfg_get(
                config,
                "biotech_scoring.production_baseline.calibration_output_dir",
                "../output/biotech_index_reports/calibration_tier1_120d_confirm",
            )
        ),
    }


def production_policy_settings(config: dict[str, Any]) -> dict[str, Any]:
    event_hard_penalty = cfg_get(config, "biotech_scoring.production_policy.event_hard_penalty", None)
    soft_weakness_penalty = cfg_get(config, "biotech_scoring.production_policy.soft_weakness_penalty", None)
    if event_hard_penalty is None:
        raise ValueError("Missing required config key biotech_scoring.production_policy.event_hard_penalty")
    if soft_weakness_penalty is None:
        raise ValueError("Missing required config key biotech_scoring.production_policy.soft_weakness_penalty")
    return {
        "event_hard_penalty": float(event_hard_penalty),
        "soft_weakness_penalty": float(soft_weakness_penalty),
        "event_hard_reasons": set(
            parse_string_list(
                cfg_get(config, "biotech_scoring.production_policy.event_hard_reasons", EVENT_HARD_WEAKNESS_DEFAULT_REASONS),
                EVENT_HARD_WEAKNESS_DEFAULT_REASONS,
            )
        ),
        "soft_weakness_reasons": set(
            parse_string_list(
                cfg_get(config, "biotech_scoring.production_policy.soft_weakness_reasons", SOFT_WEAKNESS_DEFAULT_REASONS),
                SOFT_WEAKNESS_DEFAULT_REASONS,
            )
        ),
        "commercial_fragility_threshold": float(
            cfg_get(config, "biotech_scoring.production_policy.commercial_fragility_threshold", 70.0)
        ),
        "high_risk_threshold": float(cfg_get(config, "biotech_scoring.production_policy.high_risk_threshold", 75.0)),
    }


def core_structural_veto_reasons(
    payload: dict[str, Any],
    commercial: dict[str, Any],
    settings: dict[str, Any],
) -> list[str]:
    if not as_bool(settings.get("enabled"), False):
        return []
    ctgov = payload.get("ctgov", {}) if isinstance(payload, dict) else {}
    sec_liq = payload.get("sec_and_liquidity", {}) if isinstance(payload, dict) else {}
    survival = payload.get("financial_survival", {}) if isinstance(payload, dict) else {}
    configured_reasons = set(settings.get("reasons") or CORE_STRUCTURAL_VETO_DEFAULT_REASONS)
    reasons: list[str] = []

    cash_runway = to_float(survival.get("cash_runway_months"), math.nan)
    if math.isfinite(cash_runway) and cash_runway < 9.0:
        reasons.append("cash_runway_lt_9m")
    if as_bool(survival.get("severe_runway_flag"), False):
        reasons.append("severe_runway_flag")
    going_status = str(sec_liq.get("going_concern_status") or "").strip().lower()
    if going_status in GOING_CONCERN_HARD_STATUSES:
        reasons.append("going_concern_confirmed")
    if to_float(sec_liq.get("reverse_split_hits_2y"), 0.0) > 0.0:
        reasons.append("reverse_split_history")

    verified_active = to_float(ctgov.get("verified_qualifying_active_trial_count"), 0.0)
    commercial_stage = bool(to_float(commercial.get("commercial_stage_flag"), 0.0))
    profitable = bool(to_float(commercial.get("profitable_flag"), 0.0))
    if verified_active <= 0.0 and not (commercial_stage or profitable):
        reasons.append("no_active_trial_no_business_anchor")

    addv = to_float(sec_liq.get("median_addv20", sec_liq.get("avg_dollar_volume_20d")), math.nan)
    min_addv20 = float(settings.get("min_addv20") or 0.0)
    if math.isfinite(addv) and addv < min_addv20:
        reasons.append("illiquid")

    return [reason for reason in reasons if reason in configured_reasons]


def event_hard_weakness_reasons(payload: dict[str, Any], settings: dict[str, Any]) -> list[str]:
    sec_events = payload.get("sec_events", {}) if isinstance(payload, dict) else {}
    configured_reasons = set(settings.get("event_hard_reasons") or EVENT_HARD_WEAKNESS_DEFAULT_REASONS)
    reasons: list[str] = []
    if to_float(sec_events.get("dilution_event_count"), 0.0) >= 2.0:
        reasons.append("repeated_dilution")
    if to_float(sec_events.get("negative_clinical_event_count"), 0.0) > 0.0:
        reasons.append("negative_clinical_event")
    return [reason for reason in reasons if reason in configured_reasons]


def soft_weakness_reasons(
    payload: dict[str, Any],
    commercial: dict[str, Any],
    governance: dict[str, Any],
    *,
    risk: float,
    settings: dict[str, Any],
) -> list[str]:
    ctgov = payload.get("ctgov", {}) if isinstance(payload, dict) else {}
    sec_liq = payload.get("sec_and_liquidity", {}) if isinstance(payload, dict) else {}
    survival = payload.get("financial_survival", {}) if isinstance(payload, dict) else {}
    sec_events = payload.get("sec_events", {}) if isinstance(payload, dict) else {}
    configured_reasons = set(settings.get("soft_weakness_reasons") or SOFT_WEAKNESS_DEFAULT_REASONS)
    reasons: list[str] = []

    cash_runway = to_float(survival.get("cash_runway_months"), math.nan)
    commercial_stage = bool(to_float(commercial.get("commercial_stage_flag"), 0.0))
    profitable = bool(to_float(commercial.get("profitable_flag"), 0.0))
    has_business_anchor = commercial_stage or profitable
    verified_active = to_float(ctgov.get("verified_qualifying_active_trial_count"), 0.0)
    lead_phase2_3 = to_float(ctgov.get("lead_phase2_3_active_trials"), 0.0)
    program_phase2_3 = to_float(ctgov.get("program_phase2_3_active_trials"), 0.0)
    active_pivotal = to_float(ctgov.get("active_pivotal_trials"), 0.0)
    has_advanced_trial = lead_phase2_3 > 0.0 or program_phase2_3 > 0.0 or active_pivotal > 0.0

    if math.isfinite(cash_runway) and 9.0 <= cash_runway < 12.0 and not has_business_anchor:
        reasons.append("cash_runway_9_to_12m_clinical")
    going_status = str(sec_liq.get("going_concern_status") or "").strip().lower()
    if going_status in GOING_CONCERN_SOFT_STATUSES:
        reasons.append("going_concern_warning")
    if to_float(sec_events.get("dilution_event_count"), 0.0) == 1.0:
        reasons.append("single_dilution_event")
    financial_quality = str(survival.get("data_quality") or "").strip().lower()
    if financial_quality in {"low", "poor", "stale"}:
        reasons.append("low_financial_data_quality")
    commercial_fragility = to_float(governance.get("commercial_fragility_risk_score"), 0.0)
    if commercial_fragility >= float(settings.get("commercial_fragility_threshold") or 70.0):
        reasons.append("high_commercial_fragility")
    if risk >= float(settings.get("high_risk_threshold") or 75.0):
        reasons.append("high_tier1_risk_score")
    if to_float(sec_liq.get("recent_nt_filing_count_2y"), 0.0) > 0.0:
        reasons.append("recent_nt_filing")
    if verified_active > 0.0 and not has_advanced_trial and not has_business_anchor:
        reasons.append("early_stage_or_unadvanced_trial_anchor")

    return [reason for reason in reasons if reason in configured_reasons]


def apply_production_selection_policy(
    raw_opportunity: float,
    *,
    selection_policy: str,
    event_reasons: list[str],
    soft_reasons: list[str],
    settings: dict[str, Any],
) -> tuple[float, float, float]:
    event_penalty = 0.0
    soft_penalty = 0.0
    if selection_policy in {"core_veto_event_drag", "core_veto_event_soft_drag"} and event_reasons:
        event_penalty = float(settings.get("event_hard_penalty") or 0.0)
    if selection_policy == "core_veto_event_soft_drag" and soft_reasons:
        soft_penalty = float(settings.get("soft_weakness_penalty") or 0.0)
    return clamp(raw_opportunity - event_penalty - soft_penalty), event_penalty, soft_penalty


def latest_feature_date(conn: sqlite3.Connection) -> str:
    row = conn.execute("SELECT MAX(asof_date) AS asof_date FROM daily_features").fetchone()
    asof = str(row["asof_date"] or "") if row else ""
    if not asof:
        raise ValueError("No daily_features rows found. Run 10_build_biotech_features.py first.")
    return asof


def load_feature_rows(conn: sqlite3.Connection, asof_date: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT
            f.asof_date, f.company_id, f.catalyst_score_raw, f.credibility_score_raw,
            f.financial_quality_score_raw, f.risk_score_raw, f.momentum_score_raw, f.feature_json,
            c.ticker, c.company_name
        FROM daily_features f
        LEFT JOIN companies c ON c.company_id = f.company_id
        WHERE f.asof_date = ?
        ORDER BY c.ticker
        """,
        (asof_date,),
    ).fetchall()
    out = [dict(row) for row in rows]
    missing_company_ids = [str(row["company_id"]) for row in out if not str(row.get("ticker") or "").strip()]
    if missing_company_ids:
        raise RuntimeError(
            "daily_features contains company_id value(s) missing from companies table: "
            + ",".join(missing_company_ids[:25])
            + (f"...(+{len(missing_company_ids) - 25})" if len(missing_company_ids) > 25 else "")
        )
    return out


def load_commercial_rows(conn: sqlite3.Connection, asof_date: str) -> dict[int, dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT c.*
        FROM commercial_value_features_daily c
        JOIN (
            SELECT company_id, MAX(asof_date) AS max_asof
            FROM commercial_value_features_daily
            WHERE asof_date <= ?
            GROUP BY company_id
        ) latest
          ON latest.company_id = c.company_id AND latest.max_asof = c.asof_date
        """,
        (asof_date,),
    ).fetchall()
    return {int(row["company_id"]): dict(row) for row in rows}


def load_forward_guidance_rows(conn: sqlite3.Connection, asof_date: str) -> dict[int, dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT g.*
        FROM forward_guidance_features_daily g
        JOIN (
            SELECT company_id, MAX(asof_date) AS max_asof
            FROM forward_guidance_features_daily
            WHERE asof_date <= ?
            GROUP BY company_id
        ) latest
          ON latest.company_id = g.company_id AND latest.max_asof = g.asof_date
        """,
        (asof_date,),
    ).fetchall()
    return {int(row["company_id"]): dict(row) for row in rows}


def load_governance_rows(conn: sqlite3.Connection, asof_date: str) -> dict[int, dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT g.*
        FROM governance_event_features_daily g
        JOIN (
            SELECT company_id, MAX(asof_date) AS max_asof
            FROM governance_event_features_daily
            WHERE asof_date <= ?
            GROUP BY company_id
        ) latest
          ON latest.company_id = g.company_id AND latest.max_asof = g.asof_date
        """,
        (asof_date,),
    ).fetchall()
    return {int(row["company_id"]): dict(row) for row in rows}


def score_bucket(
    score: float,
    risk: float,
    params: BucketParams,
    payload: dict[str, Any],
    commercial: dict[str, Any],
    *,
    core_veto_reasons: list[str] | None = None,
    force_core_veto_avoid: bool = False,
) -> str:
    ctgov = payload.get("ctgov", {}) if isinstance(payload, dict) else {}
    sec_liq = payload.get("sec_and_liquidity", {}) if isinstance(payload, dict) else {}
    survival = payload.get("financial_survival", {}) if isinstance(payload, dict) else {}
    verified_active = int(to_float(ctgov.get("verified_qualifying_active_trial_count", 0)))
    lead_phase2_3 = int(to_float(ctgov.get("lead_phase2_3_active_trials", 0)))
    program_phase2_3 = int(to_float(ctgov.get("program_phase2_3_active_trials", 0)))
    pivotal = int(to_float(ctgov.get("active_pivotal_trials", 0)))
    runway = to_float(survival.get("cash_runway_months"), math.nan)
    severe_runway = as_bool(survival.get("severe_runway_flag"), False)
    survival_quality = str(survival.get("data_quality") or "").lower()
    going_status = str(sec_liq.get("going_concern_status") or "").lower()
    recent_nt = int(to_float(sec_liq.get("recent_nt_filing_count_2y", 0)))
    has_advanced_catalyst = lead_phase2_3 > 0 or program_phase2_3 > 0 or pivotal > 0
    commercial_stage = bool(to_float(commercial.get("commercial_stage_flag"), 0.0))
    profitable = bool(to_float(commercial.get("profitable_flag"), 0.0))
    has_business_anchor = commercial_stage or profitable

    if force_core_veto_avoid and core_veto_reasons:
        return "avoid"
    if risk >= params.avoid_risk_min or severe_runway or going_status in GOING_CONCERN_HARD_STATUSES:
        return "avoid"
    if verified_active <= 0 and not has_business_anchor:
        return "avoid"
    if (
        score >= params.high_min
        and risk <= params.max_high_risk
        and recent_nt == 0
        and (not math.isfinite(runway) or runway >= params.min_high_runway)
        and survival_quality != "low"
        and (has_advanced_catalyst or has_business_anchor or not params.require_advanced)
    ):
        return "high_conviction"
    if score >= params.watch_min and risk <= params.max_watch_risk and (verified_active > 0 or has_business_anchor or not params.require_active_watch):
        return "watchlist"
    if score >= params.spec_min and risk <= params.max_spec_risk and (verified_active > 0 or has_business_anchor):
        return "speculative"
    return "avoid"


def investment_weight_profile(config: dict[str, Any], commercial: dict[str, Any]) -> tuple[str, dict[str, float]]:
    commercial_stage = bool(to_float(commercial.get("commercial_stage_flag"), 0.0))
    profitable = bool(to_float(commercial.get("profitable_flag"), 0.0))
    ttm_revenue = to_float(commercial.get("ttm_revenue"), 0.0)
    revenue_min = float(cfg_get(config, "commercial_value.commercial_stage_revenue_min", 50_000_000))
    profile_name = "commercial_stage" if commercial_stage or profitable or ttm_revenue >= revenue_min else "clinical_stage"
    profiles = cfg_get(config, "biotech_scoring.investment_weight_profiles", {}) or {}
    fallback = cfg_get(config, "biotech_scoring.investment_weights", {}) or {}
    raw_profile = profiles.get(profile_name) if isinstance(profiles, dict) else None
    raw_weights = dict(raw_profile if raw_profile is not None else fallback)
    if "commercial_value" not in raw_weights and "commercial_quality" in raw_weights:
        LOGGER.warning(
            "biotech_scoring.investment_weight_profiles.%s.commercial_quality is deprecated; rename to commercial_value",
            profile_name,
        )
        raw_weights["commercial_value"] = raw_weights["commercial_quality"]
    weights = {
        "clinical_opportunity": float(raw_weights.get("clinical_opportunity", 0.25)),
        "commercial_value": float(raw_weights.get("commercial_value", 0.25)),
        "forward_guidance": float(raw_weights.get("forward_guidance", 0.0)),
        "valuation": float(raw_weights.get("valuation", 0.20)),
        "upside_capacity": float(raw_weights.get("upside_capacity", 0.10)),
        "financial_quality": float(raw_weights.get("financial_quality", 0.15)),
        "momentum": float(raw_weights.get("momentum", 0.05)),
        "risk_penalty": float(raw_weights.get("risk_penalty", 0.15)),
    }
    negative = [name for name, value in weights.items() if value < 0.0]
    if negative:
        raise ValueError(f"Investment weight profile '{profile_name}' has negative weight(s): {', '.join(negative)}")
    positive_total = sum(value for name, value in weights.items() if name != "risk_penalty")
    if positive_total <= 1e-12 or abs(positive_total - 1.0) > 1e-3:
        raise ValueError(
            f"Investment weight profile '{profile_name}' positive weights sum to {positive_total:.4f}; expected 1.0 +/- 0.001"
        )
    return (
        profile_name,
        weights,
    )


def score_rows(
    rows: list[dict[str, Any]],
    config: dict[str, Any],
    commercial_by_company: dict[int, dict[str, Any]],
    forward_by_company: dict[int, dict[str, Any]],
    governance_by_company: dict[int, dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    governance_by_company = governance_by_company or {}
    weights = cfg_get(config, "biotech_scoring.weights", {}) or {}
    catalyst_w = float(weights.get("catalyst", 0.45))
    credibility_w = float(weights.get("credibility", 0.30))
    financial_w = float(weights.get("financial_quality", 0.15))
    momentum_w = float(weights.get("momentum", 0.10))
    risk_w = float(weights.get("risk_penalty", 0.30))

    investment_enabled = as_bool(cfg_get(config, "biotech_scoring.use_investment_score", True), True)
    core_veto_settings = core_structural_veto_settings(config)
    production_baseline = tier1_production_baseline(config)
    policy_settings = production_policy_settings(config)
    bucket_settings = bucket_params(config)
    selection_policy = str(production_baseline["selection_policy"])
    apply_core_veto_to_rank = bool(core_veto_settings["enabled"] and core_veto_settings["apply_to_rank"])
    force_core_veto_avoid = bool(core_veto_settings["enabled"] and core_veto_settings["force_avoid_bucket"])

    scored: list[dict[str, Any]] = []
    missing_catalyst_raw: list[str] = []
    missing_credibility_raw: list[str] = []
    missing_financial_raw: list[str] = []
    missing_momentum_raw: list[str] = []
    for row in rows:
        company_id = int(row["company_id"])
        payload = parse_json(row.get("feature_json"))
        raw_scores = payload.get("raw_scores", {}) if isinstance(payload, dict) else {}
        commercial = commercial_by_company.get(company_id, {})
        forward_guidance = forward_by_company.get(company_id, {})
        governance = governance_by_company.get(company_id, {})
        forward_payload = parse_json(forward_guidance.get("payload_json"))
        if "catalyst_score_raw" not in raw_scores and row.get("catalyst_score_raw") in (None, ""):
            missing_catalyst_raw.append(str(row.get("ticker") or company_id))
        if "credibility_score_raw" not in raw_scores and row.get("credibility_score_raw") in (None, ""):
            missing_credibility_raw.append(str(row.get("ticker") or company_id))
        if "financial_quality_score_raw" not in raw_scores and row.get("financial_quality_score_raw") in (None, ""):
            missing_financial_raw.append(str(row.get("ticker") or company_id))
        if "momentum_score_raw" not in raw_scores and row.get("momentum_score_raw") in (None, ""):
            missing_momentum_raw.append(str(row.get("ticker") or company_id))
        catalyst = clamp(to_float(raw_scores.get("catalyst_score_raw", row.get("catalyst_score_raw", 0.0))))
        credibility = clamp(to_float(raw_scores.get("credibility_score_raw", row.get("credibility_score_raw", 0.0))))
        financial_quality = clamp(to_float(raw_scores.get("financial_quality_score_raw", row.get("financial_quality_score_raw", 0.0))))
        risk = clamp(to_float(raw_scores.get("risk_score_raw", row.get("risk_score_raw", 0.0))))
        momentum = clamp(to_float(raw_scores.get("momentum_score_raw", row.get("momentum_score_raw", 0.0))))
        clinical_positive = (
            catalyst_w * catalyst
            + credibility_w * credibility
            + financial_w * financial_quality
            + momentum_w * momentum
        )
        clinical_risk_drag = convex_risk_drag(risk, risk_w, config, "biotech_scoring")
        clinical_opportunity = clamp(clinical_positive - clinical_risk_drag)

        commercial_quality = clamp(to_float(commercial.get("commercial_quality_score"), float(cfg_get(config, "biotech_scoring.missing_score_defaults.commercial_quality_score", 35.0))))
        commercial_value = clamp(to_float(commercial.get("commercial_value_score"), float(cfg_get(config, "biotech_scoring.missing_score_defaults.commercial_value_score", 35.0))))
        forward_guidance_score = clamp(to_float(forward_guidance.get("guidance_score"), float(cfg_get(config, "biotech_scoring.missing_score_defaults.forward_guidance_score", 45.0))))
        valuation_score = clamp(to_float(commercial.get("valuation_score"), float(cfg_get(config, "biotech_scoring.missing_score_defaults.valuation_score", 50.0))))
        upside_capacity_score = clamp(to_float(commercial.get("upside_capacity_score"), float(cfg_get(config, "biotech_scoring.missing_score_defaults.upside_capacity_score", 50.0))))
        profile_name, profile_weights = investment_weight_profile(config, commercial)
        investment_positive = (
            profile_weights["clinical_opportunity"] * clinical_opportunity
            + profile_weights["commercial_value"] * commercial_value
            + profile_weights["forward_guidance"] * forward_guidance_score
            + profile_weights["valuation"] * valuation_score
            + profile_weights["upside_capacity"] * upside_capacity_score
            + profile_weights["financial_quality"] * financial_quality
            + profile_weights["momentum"] * momentum
        )
        investment_risk_drag = convex_risk_drag(risk, profile_weights["risk_penalty"], config, "biotech_scoring")
        effective_pre_confidence_risk_drag = profile_weights["clinical_opportunity"] * clinical_risk_drag + investment_risk_drag
        confidence_multiplier = score_confidence_multiplier(config, payload, commercial, forward_guidance, profile_name)
        investment_score = clamp((investment_positive - investment_risk_drag) * confidence_multiplier)
        raw_opportunity = investment_score if investment_enabled else clinical_opportunity
        core_veto_reasons = core_structural_veto_reasons(payload, commercial, core_veto_settings)
        event_reasons = event_hard_weakness_reasons(payload, policy_settings)
        soft_reasons = soft_weakness_reasons(
            payload,
            commercial,
            governance,
            risk=risk,
            settings=policy_settings,
        )
        opportunity, event_penalty, soft_penalty = apply_production_selection_policy(
            raw_opportunity,
            selection_policy=selection_policy,
            event_reasons=event_reasons,
            soft_reasons=soft_reasons,
            settings=policy_settings,
        )
        selection_gate = tier1_selection_gate_score(opportunity, risk)
        core_veto_flag = 1.0 if core_veto_reasons else 0.0
        rank_demoted_by_core_veto = bool(apply_core_veto_to_rank and core_veto_reasons)

        ctgov = payload.get("ctgov", {}) if isinstance(payload, dict) else {}
        sec_liq = payload.get("sec_and_liquidity", {}) if isinstance(payload, dict) else {}
        survival = payload.get("financial_survival", {}) if isinstance(payload, dict) else {}
        sec_events = payload.get("sec_events", {}) if isinstance(payload, dict) else {}
        commercial_evidence = {
            "latest_period_end": commercial.get("latest_period_end", ""),
            "ttm_revenue": commercial.get("ttm_revenue", ""),
            "revenue_yoy_growth_pct": commercial.get("revenue_yoy_growth_pct", ""),
            "gross_margin_pct": commercial.get("gross_margin_pct", ""),
            "operating_margin_pct": commercial.get("operating_margin_pct", ""),
            "net_margin_pct": commercial.get("net_margin_pct", ""),
            "free_cash_flow_ttm": commercial.get("free_cash_flow_ttm", ""),
            "cash_and_investments": commercial.get("cash_and_investments", ""),
            "net_cash": commercial.get("net_cash", ""),
            "shares_yoy_growth_pct": commercial.get("shares_yoy_growth_pct", ""),
            "market_cap": commercial.get("market_cap", ""),
            "enterprise_value": commercial.get("enterprise_value", ""),
            "price_to_sales": commercial.get("price_to_sales", ""),
            "ev_to_sales": commercial.get("ev_to_sales", ""),
            "pe_ratio": commercial.get("pe_ratio", ""),
            "fcf_yield": commercial.get("fcf_yield", ""),
            "commercial_stage_flag": bool(to_float(commercial.get("commercial_stage_flag"), 0.0)),
            "profitable_flag": bool(to_float(commercial.get("profitable_flag"), 0.0)),
            "commercial_quality_score": commercial_quality,
            "commercial_value_score": commercial_value,
            "valuation_score": valuation_score,
            "upside_capacity_score": upside_capacity_score,
            "data_quality": commercial.get("data_quality", ""),
            "missing_fields": commercial.get("missing_fields", ""),
            "proxy_fields_used": commercial.get("proxy_fields_used", ""),
        }
        top_evidence = {
            "primary_nct": ctgov.get("primary_nct", ""),
            "primary_trial_title": ctgov.get("primary_trial_title", ""),
            "top_ncts": ctgov.get("top_ncts", []),
            "ctgov_quality": {
                "verified_qualifying_active_trial_count": ctgov.get("verified_qualifying_active_trial_count", 0),
                "phase2_3_active_trials": ctgov.get("phase2_3_active_trials", 0),
                "lead_phase2_3_active_trials": ctgov.get("lead_phase2_3_active_trials", 0),
                "program_phase2_3_active_trials": ctgov.get("program_phase2_3_active_trials", 0),
                "collaborator_phase2_3_active_trials": ctgov.get("collaborator_phase2_3_active_trials", 0),
                "effective_phase2_3_trials": ctgov.get("effective_phase2_3_trials", 0),
                "core_pipeline_quality_score": ctgov.get("core_pipeline_quality_score", 0),
                "collaborator_dependency_ratio": ctgov.get("collaborator_dependency_ratio", 0),
                "collaborator_heavy_flag": ctgov.get("collaborator_heavy_flag", False),
                "active_lead_sponsor_trials": ctgov.get("active_lead_sponsor_trials", 0),
                "active_collaborator_trials": ctgov.get("active_collaborator_trials", 0),
                "active_program_override_trials": ctgov.get("active_program_override_trials", 0),
            },
            "commercial_value": commercial_evidence,
            "forward_guidance": {
                "latest_guidance_filing_date": forward_guidance.get("latest_guidance_filing_date", ""),
                "forward_revenue_midpoint": forward_guidance.get("forward_revenue_midpoint", ""),
                "forward_revenue_low": forward_guidance.get("forward_revenue_low", ""),
                "forward_revenue_high": forward_guidance.get("forward_revenue_high", ""),
                "forward_revenue_year": forward_guidance.get("forward_revenue_year", ""),
                "forward_revenue_growth_pct": forward_guidance.get("forward_revenue_growth_pct", ""),
                "forward_ebitda_midpoint": forward_guidance.get("forward_ebitda_midpoint", ""),
                "forward_ebitda_margin_pct": forward_guidance.get("forward_ebitda_margin_pct", ""),
                "forward_eps_midpoint": forward_guidance.get("forward_eps_midpoint", ""),
                "guidance_confidence": forward_guidance.get("guidance_confidence", ""),
                "guidance_recency_days": forward_guidance.get("guidance_recency_days", ""),
                "forward_profitability_flag": bool(to_float(forward_guidance.get("forward_profitability_flag"), 0.0)),
                "guidance_score": forward_guidance_score,
                "forward_growth_score": forward_guidance.get("forward_growth_score", ""),
                "forward_profitability_score": forward_guidance.get("forward_profitability_score", ""),
                "forward_valuation_score": forward_guidance.get("forward_valuation_score", ""),
                "data_quality": forward_guidance.get("data_quality", ""),
                "missing_fields": forward_guidance.get("missing_fields", ""),
                "guidance_records": forward_payload.get("guidance_records", []) if isinstance(forward_payload, dict) else [],
            },
            "score_components": {
                "model_role": "tier1_core_investability_gate",
                "production_baseline_score_model": production_baseline["score_model"],
                "primary_horizon_trading_days": production_baseline["primary_horizon_trading_days"],
                "selection_policy": production_baseline["selection_policy"],
                "alpha_multibagger_role": production_baseline["alpha_multibagger_role"],
                "clinical_opportunity_score": round(clinical_opportunity, 4),
                "investment_score": round(investment_score, 4),
                "raw_opportunity_score_before_policy": round(raw_opportunity, 4),
                "policy_adjusted_opportunity_score": round(opportunity, 4),
                "production_policy_event_hard_penalty": round(event_penalty, 4),
                "production_policy_soft_weakness_penalty": round(soft_penalty, 4),
                "production_policy_total_penalty": round(event_penalty + soft_penalty, 4),
                "production_policy_event_hard_reasons": event_reasons,
                "production_policy_soft_weakness_reasons": soft_reasons,
                "tier1_selection_gate_score": round(selection_gate, 4),
                "investment_profile": profile_name,
                "investment_weights": profile_weights,
                "clinical_risk_drag": round(clinical_risk_drag, 4),
                "investment_risk_drag": round(investment_risk_drag, 4),
                "effective_pre_confidence_risk_drag": round(effective_pre_confidence_risk_drag, 4),
                "effective_total_risk_drag": round(effective_pre_confidence_risk_drag * confidence_multiplier, 4),
                "data_quality_confidence_multiplier": round(confidence_multiplier, 4),
                "commercial_quality_score": round(commercial_quality, 4),
                "commercial_value_score": round(commercial_value, 4),
                "forward_guidance_score": round(forward_guidance_score, 4),
                "valuation_score": round(valuation_score, 4),
                "upside_capacity_score": round(upside_capacity_score, 4),
            },
            "downstream_interaction": {
                "recommended_use": "gate_or_cap_multibagger_candidates_do_not_add_as_duplicate_alpha",
                "alpha_multibagger_role": production_baseline["alpha_multibagger_role"],
                "selection_gate_score": round(selection_gate, 4),
                "opportunity_score": round(opportunity, 4),
                "risk_score": round(risk, 4),
            },
            "production_baseline": production_baseline,
            "production_selection_policy": {
                "selection_policy": selection_policy,
                "event_hard_penalty": policy_settings["event_hard_penalty"],
                "soft_weakness_penalty": policy_settings["soft_weakness_penalty"],
                "event_hard_reasons": event_reasons,
                "soft_weakness_reasons": soft_reasons,
                "configured_event_hard_reasons": sorted(policy_settings["event_hard_reasons"]),
                "configured_soft_weakness_reasons": sorted(policy_settings["soft_weakness_reasons"]),
            },
            "core_structural_veto": {
                "enabled": bool(core_veto_settings["enabled"]),
                "flag": bool(core_veto_reasons),
                "reasons": core_veto_reasons,
                "configured_reasons": sorted(core_veto_settings["reasons"]),
                "apply_to_rank": bool(core_veto_settings["apply_to_rank"]),
                "force_avoid_bucket": bool(core_veto_settings["force_avoid_bucket"]),
                "rank_demoted_by_core_veto": rank_demoted_by_core_veto,
                "min_addv20": core_veto_settings["min_addv20"],
            },
            "sec_events": sec_events,
            "risk_flags": {
                "going_concern_status": sec_liq.get("going_concern_status", ""),
                "reverse_split_hits_2y": sec_liq.get("reverse_split_hits_2y", 0),
                "median_addv20": sec_liq.get("median_addv20", 0),
                "cash_runway_months": finite_value_or_none(survival.get("cash_runway_months")),
                "financial_survival_score": finite_value_or_none(survival.get("financial_survival_score")),
                "financial_data_quality": survival.get("data_quality", ""),
                "sec_dilution_event_count": sec_events.get("dilution_event_count", 0) if isinstance(sec_events, dict) else 0,
                "sec_negative_clinical_event_count": sec_events.get("negative_clinical_event_count", 0) if isinstance(sec_events, dict) else 0,
                "core_structural_veto_flag": bool(core_veto_reasons),
                "core_structural_veto_reasons": "|".join(core_veto_reasons),
                "event_hard_weakness_reasons": "|".join(event_reasons),
                "soft_weakness_reasons": "|".join(soft_reasons),
            },
            "manual": payload.get("manual", {}),
        }
        scored.append(
            {
                "asof_date": row["asof_date"],
                "company_id": company_id,
                "ticker": row["ticker"],
                "company_name": row["company_name"],
                "catalyst_score": round(catalyst, 4),
                "credibility_score": round(credibility, 4),
                "financial_quality_score": round(financial_quality, 4),
                "risk_score": round(risk, 4),
                "momentum_score": round(momentum, 4),
                "clinical_opportunity_score": round(clinical_opportunity, 4),
                "commercial_value_score": round(commercial_value, 4),
                "forward_guidance_score": round(forward_guidance_score, 4),
                "valuation_score": round(valuation_score, 4),
                "upside_capacity_score": round(upside_capacity_score, 4),
                "investment_score": round(investment_score, 4),
                "opportunity_score": round(opportunity, 4),
                "tier1_selection_gate_score": round(selection_gate, 4),
                "tier1_primary_horizon_trading_days": production_baseline["primary_horizon_trading_days"],
                "tier1_production_score_model": production_baseline["score_model"],
                "tier1_selection_policy": production_baseline["selection_policy"],
                "alpha_multibagger_role": production_baseline["alpha_multibagger_role"],
                "core_structural_veto_flag": core_veto_flag,
                "core_structural_veto_reasons": "|".join(core_veto_reasons),
                "rank_demoted_by_core_veto": 1.0 if rank_demoted_by_core_veto else 0.0,
                "data_quality_confidence_multiplier": round(confidence_multiplier, 4),
                "clinical_risk_drag": round(clinical_risk_drag, 4),
                "investment_risk_drag": round(investment_risk_drag, 4),
                "effective_total_risk_drag": round(effective_pre_confidence_risk_drag * confidence_multiplier, 4),
                "bucket": score_bucket(
                    opportunity,
                    risk,
                    bucket_settings,
                    payload,
                    commercial,
                    core_veto_reasons=core_veto_reasons,
                    force_core_veto_avoid=force_core_veto_avoid,
                ),
                "primary_nct": ctgov.get("primary_nct", ""),
                "primary_trial_title": ctgov.get("primary_trial_title", ""),
                "verified_qualifying_active_trial_count": ctgov.get("verified_qualifying_active_trial_count", 0),
                "phase2_3_active_trials": ctgov.get("phase2_3_active_trials", 0),
                "lead_phase2_3_active_trials": ctgov.get("lead_phase2_3_active_trials", 0),
                "program_phase2_3_active_trials": ctgov.get("program_phase2_3_active_trials", 0),
                "collaborator_phase2_3_active_trials": ctgov.get("collaborator_phase2_3_active_trials", 0),
                "effective_phase2_3_trials": ctgov.get("effective_phase2_3_trials", 0),
                "core_pipeline_quality_score": ctgov.get("core_pipeline_quality_score", 0),
                "collaborator_dependency_ratio": ctgov.get("collaborator_dependency_ratio", 0),
                "collaborator_heavy_flag": ctgov.get("collaborator_heavy_flag", False),
                "active_pivotal_trials": ctgov.get("active_pivotal_trials", 0),
                "median_addv20": sec_liq.get("median_addv20", 0),
                "cash_runway_months": finite_value_or_blank(survival.get("cash_runway_months")),
                "financial_survival_score": finite_value_or_blank(survival.get("financial_survival_score")),
                "financial_data_quality": survival.get("data_quality", ""),
                "going_concern_status": sec_liq.get("going_concern_status", ""),
                "reverse_split_hits_2y": sec_liq.get("reverse_split_hits_2y", 0),
                "sec_regulatory_catalyst_count": sec_events.get("regulatory_catalyst_count", 0) if isinstance(sec_events, dict) else 0,
                "sec_dilution_event_count": sec_events.get("dilution_event_count", 0) if isinstance(sec_events, dict) else 0,
                "sec_negative_clinical_event_count": sec_events.get("negative_clinical_event_count", 0) if isinstance(sec_events, dict) else 0,
                "top_evidence_json": json.dumps(top_evidence, ensure_ascii=True, sort_keys=True),
            }
        )
    scored.sort(
        key=lambda item: (
            1 if apply_core_veto_to_rank and to_float(item.get("core_structural_veto_flag"), 0.0) > 0.0 else 0,
            -clamp(to_float(item.get("opportunity_score"), 0.0)),
            clamp(to_float(item.get("risk_score"), 100.0)),
            str(item["ticker"]),
        )
    )
    for idx, row in enumerate(scored, start=1):
        row["rank"] = idx
    if missing_catalyst_raw:
        LOGGER.warning(
            "catalyst_score_raw missing for %d row(s); used 0.0 fallback for sample=%s",
            len(missing_catalyst_raw),
            ",".join(missing_catalyst_raw[:10]),
        )
    if missing_credibility_raw:
        LOGGER.warning(
            "credibility_score_raw missing for %d row(s); used 0.0 fallback for sample=%s",
            len(missing_credibility_raw),
            ",".join(missing_credibility_raw[:10]),
        )
    if missing_financial_raw:
        LOGGER.warning(
            "financial_quality_score_raw missing for %d row(s); used 0.0 fallback for sample=%s",
            len(missing_financial_raw),
            ",".join(missing_financial_raw[:10]),
        )
    if missing_momentum_raw:
        LOGGER.warning(
            "momentum_score_raw missing for %d row(s); used 0.0 fallback for sample=%s",
            len(missing_momentum_raw),
            ",".join(missing_momentum_raw[:10]),
        )
    return scored

def upsert_scores(conn: sqlite3.Connection, rows: list[dict[str, Any]], asof_date: str) -> None:
    now = utc_now()
    params = [
        (
            row["asof_date"],
            row["company_id"],
            row["catalyst_score"],
            row["credibility_score"],
            row["financial_quality_score"],
            row["risk_score"],
            row["momentum_score"],
            row["clinical_opportunity_score"],
            row["commercial_value_score"],
            row["forward_guidance_score"],
            row["valuation_score"],
            row["upside_capacity_score"],
            row["investment_score"],
            row["opportunity_score"],
            row["tier1_selection_gate_score"],
            row["data_quality_confidence_multiplier"],
            row["clinical_risk_drag"],
            row["investment_risk_drag"],
            row["rank"],
            row["bucket"],
            row["top_evidence_json"],
            now,
            now,
        )
        for row in rows
    ]
    with conn:
        conn.executemany(
            """
            INSERT OR REPLACE INTO daily_scores(
                asof_date, company_id, catalyst_score, credibility_score,
                financial_quality_score, risk_score, momentum_score, clinical_opportunity_score,
                commercial_value_score, forward_guidance_score, valuation_score, upside_capacity_score, investment_score, opportunity_score,
                tier1_selection_gate_score, data_quality_confidence_multiplier, clinical_risk_drag, investment_risk_drag,
                rank, bucket, top_evidence_json, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            params,
        )


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "asof_date",
        "rank",
        "ticker",
        "company_name",
        "bucket",
        "opportunity_score",
        "investment_score",
        "clinical_opportunity_score",
        "tier1_selection_gate_score",
        "tier1_primary_horizon_trading_days",
        "tier1_production_score_model",
        "tier1_selection_policy",
        "alpha_multibagger_role",
        "core_structural_veto_flag",
        "core_structural_veto_reasons",
        "rank_demoted_by_core_veto",
        "data_quality_confidence_multiplier",
        "clinical_risk_drag",
        "investment_risk_drag",
        "effective_total_risk_drag",
        "commercial_value_score",
        "forward_guidance_score",
        "valuation_score",
        "upside_capacity_score",
        "catalyst_score",
        "credibility_score",
        "financial_quality_score",
        "risk_score",
        "momentum_score",
        "primary_nct",
        "primary_trial_title",
        "verified_qualifying_active_trial_count",
        "phase2_3_active_trials",
        "lead_phase2_3_active_trials",
        "program_phase2_3_active_trials",
        "collaborator_phase2_3_active_trials",
        "effective_phase2_3_trials",
        "core_pipeline_quality_score",
        "collaborator_dependency_ratio",
        "collaborator_heavy_flag",
        "active_pivotal_trials",
        "median_addv20",
        "cash_runway_months",
        "financial_survival_score",
        "financial_data_quality",
        "going_concern_status",
        "reverse_split_hits_2y",
        "sec_regulatory_catalyst_count",
        "sec_dilution_event_count",
        "sec_negative_clinical_event_count",
        "top_evidence_json",
    ]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, lineterminator="\n")
        writer.writeheader()
        writer.writerows([{field: row.get(field, "") for field in fieldnames} for row in rows])


def main() -> None:
    configure_logging()
    args = parse_args()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    base_dir = config_path.parent
    db_path = args.db.expanduser().resolve() if args.db else resolve_path(cfg_get(config, "paths.database_path"), base_dir=base_dir)
    base_output_dir = resolve_path(cfg_get(config, "biotech_scoring.output_dir", "../output/biotech_index_reports"), base_dir=base_dir)
    configured_universe_csv = resolve_path(
        cfg_get(config, "biotech_features.final_scoring_universe_csv"),
        base_dir=base_dir,
    )
    sqlite_timeout_sec = float(cfg_get(config, "runtime.sqlite_timeout_sec", 30.0))

    with connect(db_path, timeout_sec=sqlite_timeout_sec) as conn:
        run_id: int | None = None
        try:
            init_db(conn)
            if args.asof:
                parsed_asof = parse_date(args.asof)
                if parsed_asof is None:
                    raise ValueError(f"Invalid --asof date: {args.asof}")
                asof_date = parsed_asof.isoformat()
            else:
                asof_date = latest_feature_date(conn)
            output_dir = dated_output_dir(base_output_dir, asof_date)
            output_csv = output_dir / str(cfg_get(config, "biotech_scoring.output_csv", "biotech_daily_scores.csv"))
            universe_csv = resolve_report_input_csv(
                configured_universe_csv,
                base_output_dir=base_output_dir,
                asof_date=asof_date,
            )
            expected_tickers = read_final_scoring_tickers(universe_csv)
            run_id = start_run(conn, run_type="score_biotech_index", input_path=db_path)
            features = load_feature_rows(conn, asof_date)
            if not features:
                raise ValueError(f"No features found for asof_date={asof_date}")
            validate_full_universe_coverage(
                expected_tickers=expected_tickers,
                observed_tickers=[row["ticker"] for row in features],
                context="biotech scoring input features",
                subset_mode=False,
            )
            commercial_by_company = load_commercial_rows(conn, asof_date)
            forward_by_company = load_forward_guidance_rows(conn, asof_date)
            governance_by_company = load_governance_rows(conn, asof_date)
            missing_commercial = [str(row["ticker"]) for row in features if int(row["company_id"]) not in commercial_by_company]
            if missing_commercial:
                raise RuntimeError(
                    "biotech scoring missing commercial_value_features_daily row(s): "
                    + ",".join(sorted(missing_commercial)[:25])
                    + (f"...(+{len(missing_commercial) - 25})" if len(missing_commercial) > 25 else "")
                )
            missing_forward = [str(row["ticker"]) for row in features if int(row["company_id"]) not in forward_by_company]
            if missing_forward:
                raise RuntimeError(
                    "biotech scoring missing forward_guidance_features_daily row(s): "
                    + ",".join(sorted(missing_forward)[:25])
                    + (f"...(+{len(missing_forward) - 25})" if len(missing_forward) > 25 else "")
                )
            missing_governance = [str(row["ticker"]) for row in features if int(row["company_id"]) not in governance_by_company]
            if missing_governance:
                raise RuntimeError(
                    "biotech scoring missing governance_event_features_daily row(s): "
                    + ",".join(sorted(missing_governance)[:25])
                    + (f"...(+{len(missing_governance) - 25})" if len(missing_governance) > 25 else "")
                )
            max_upstream_staleness_days = int(cfg_get(config, "biotech_refresh.max_upstream_staleness_days", 0))
            validate_layer_freshness(
                base_rows=features,
                layer_rows_by_company=commercial_by_company,
                asof_date=asof_date,
                context="biotech scoring commercial_value_features_daily",
                max_staleness_days=max_upstream_staleness_days,
            )
            validate_layer_freshness(
                base_rows=features,
                layer_rows_by_company=forward_by_company,
                asof_date=asof_date,
                context="biotech scoring forward_guidance_features_daily",
                max_staleness_days=max_upstream_staleness_days,
            )
            validate_layer_freshness(
                base_rows=features,
                layer_rows_by_company=governance_by_company,
                asof_date=asof_date,
                context="biotech scoring governance_event_features_daily",
                max_staleness_days=max_upstream_staleness_days,
            )
            scored = score_rows(features, config, commercial_by_company, forward_by_company, governance_by_company)
            validate_full_universe_coverage(
                expected_tickers=expected_tickers,
                observed_tickers=[row["ticker"] for row in scored],
                context="biotech scoring output",
                subset_mode=False,
            )
            upsert_scores(conn, scored, asof_date)
            write_csv(output_csv, scored)
            finish_run(conn, run_id=run_id, status="success", row_count=len(scored), message=f"asof={asof_date} output={output_csv}")
            LOGGER.info("Scored biotech index: rows=%d output=%s", len(scored), output_csv)
        except BaseException as exc:
            if run_id is not None and not (isinstance(exc, SystemExit) and exc.code in (0, None)):
                finish_run(conn, run_id=run_id, status="failed", row_count=0, message=f"{type(exc).__name__}: {exc}")
            raise


if __name__ == "__main__":
    main()

