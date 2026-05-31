#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import importlib.util
import json
import logging
import math
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from statistics import median
from typing import Any


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from biotech_index.core.config import cfg_get, load_yaml, resolve_optional_path, resolve_path  # noqa: E402
from biotech_index.core.db import connect, finish_run, init_db, start_run  # noqa: E402
from biotech_index.core.logging_utils import configure_utc_logging  # noqa: E402


LOGGER = logging.getLogger("publish_biotech_reports")
DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
DEFAULT_CALIBRATION_SHADOW_MODULE = PACKAGE_ROOT / "scripts" / "28_calibrate_biotech_opportunity.py"


def first_nonblank(*values: object) -> object:
    for value in values:
        if str(value if value is not None else "").strip():
            return value
    return ""


TOP_SCORE_FIELDS = [
    "asof_date",
    "rank",
    "ticker",
    "company_name",
    "bucket",
    "opportunity_score",
    "action_tier",
    "action_tier_reason",
    "allocation_candidate_flag",
    "research_watchlist_flag",
    "investment_score",
    "investment_profile",
    "biotech_primary_cohort",
    "biotech_secondary_cohort",
    "biotech_cohort_reason_codes",
    "biotech_cohort_confidence",
    "biotech_cohort_margin",
    "biotech_cohort_source",
    "biotech_cohort_overlays",
    "biotech_cohort_data_quality",
    "biotech_taxonomy_review_required",
    "biotech_cohort_sparse_data_flag",
    "biotech_cohort_size",
    "biotech_cohort_rank",
    "biotech_cohort_percentile",
    "biotech_cohort_percentile_shrunk",
    "biotech_cohort_reliability_score",
    "biotech_cohort_calibration_weight",
    "biotech_cohort_investible_flag",
    "biotech_cohort_calibration_eligible_flag",
    "biotech_cohort_calibration_mode",
    "biotech_cohort_exclusion_reason",
    "biotech_cohort_model_version",
    "clinical_opportunity_score",
    "tier1_selection_gate_score",
    "tier1_primary_horizon_trading_days",
    "tier1_production_score_model",
    "tier1_selection_policy",
    "alpha_multibagger_role",
    "core_structural_veto_flag",
    "core_structural_veto_reasons",
    "rank_demoted_by_core_veto",
    "event_hard_weakness_reasons",
    "data_quality_confidence_multiplier",
    "clinical_risk_drag",
    "investment_risk_drag",
    "effective_total_risk_drag",
    "production_policy_quality_penalty",
    "production_policy_quality_bonus",
    "rank_quality_cap",
    "rank_quality_cap_reasons",
    "rank_quality_cap_vetoed",
    "rank_quality_cap_veto_reasons",
    "commercial_quality_score",
    "commercial_value_score",
    "forward_guidance_score",
    "valuation_score",
    "upside_capacity_score",
    "institutional_upside_capacity_score",
    "leverage_score",
    "leverage_fragility_score",
    "value_trap_score",
    "mature_defensive_score",
    "expected_return_quality_score",
    "no_forward_guidance_flag",
    "guidance_staleness_flag",
    "guidance_stale_flag",
    "no_guidance_negative_growth_flag",
    "catalyst_score",
    "credibility_score",
    "financial_quality_score",
    "risk_score",
    "momentum_score",
    "primary_nct",
    "primary_trial_title",
    "ctgov_evidence_type",
    "company_strategy_category",
    "ctgov_review_bucket",
    "ctgov_manual_root_cause",
    "lead_phase2_3_active_trials",
    "program_phase2_3_active_trials",
    "collaborator_phase2_3_active_trials",
    "effective_phase2_3_trials",
    "core_pipeline_quality_score",
    "collaborator_dependency_ratio",
    "collaborator_heavy_flag",
    "going_concern_status",
    "reverse_split_hits_2y",
    "median_addv20",
    "cash_runway_months",
    "financial_survival_score",
    "ttm_revenue",
    "revenue_yoy_growth_pct",
    "gross_margin_pct",
    "net_margin_pct",
    "market_cap",
    "ev_to_sales",
    "pe_ratio",
    "commercial_stage_flag",
    "profitable_flag",
    "latest_guidance_filing_date",
    "forward_revenue_midpoint",
    "forward_revenue_growth_pct",
    "forward_ebitda_midpoint",
    "forward_ebitda_margin_pct",
    "guidance_confidence",
    "forward_guidance_data_quality",
    "forward_guidance_source_type",
    "forward_guidance_source_name",
    "forward_guidance_source_url",
    "forward_guidance_override_reason",
    "financial_data_quality",
    "sec_regulatory_catalyst_count",
    "sec_catalyst_raw_score",
    "sec_catalyst_recency_adjusted_score",
    "sec_catalyst_score_used",
    "sec_catalyst_decay_delta",
    "sec_catalyst_latest_filing_date",
    "sec_catalyst_latest_event_date",
    "sec_catalyst_latest_event_type",
    "sec_catalyst_recency_days",
    "sec_catalyst_recency_basis",
    "sec_catalyst_event_types",
    "sec_event_recency_decay_enabled",
    "sec_event_recency_half_life_days",
    "sec_event_pre_decay_points",
    "sec_event_post_decay_points",
    "sec_event_decay_delta",
    "latest_positive_sec_event_age_days",
    "latest_positive_sec_event_type",
    "sec_dilution_event_count",
    "sec_negative_clinical_event_count",
    "industry",
    "industry_aggregate",
]

SHADOW_SCORE_FIELDS = [
    "asof_date",
    "rank",
    "ticker",
    "company_name",
    "shadow_candidate_name",
    "shadow_selection_policy",
    "shadow_top_n",
    "shadow_score",
    "biotech_primary_cohort",
    "biotech_secondary_cohort",
    "biotech_cohort_investible_flag",
    "biotech_cohort_calibration_eligible_flag",
    "biotech_cohort_calibration_mode",
    "biotech_cohort_exclusion_reason",
    "biotech_cohort_confidence",
    "biotech_cohort_sparse_data_flag",
    "action_tier",
    "action_tier_reason",
    "allocation_candidate_flag",
    "research_watchlist_flag",
    "candidate_investment_score",
    "candidate_pre_rank_cap_selection_score",
    "candidate_clinical_opportunity_score",
    "profile_name",
    "commercial_value_score",
    "forward_guidance_score",
    "valuation_score",
    "quality_adjusted_valuation_score",
    "quality_adjusted_guidance_score",
    "institutional_upside_capacity_score",
    "value_trap_score",
    "leverage_score",
    "leverage_fragility_score",
    "mature_defensive_score",
    "expected_return_quality_score",
    "rank_quality_cap",
    "rank_quality_cap_flag",
    "rank_quality_cap_reasons",
    "rank_quality_cap_vetoed",
    "risk_score_raw",
    "momentum_score_raw",
    "core_hard_weakness_reasons",
    "event_hard_weakness_reasons",
    "soft_weakness_reasons",
    "commercial_business_shock_score",
    "commercial_business_shock_reasons",
    "no_forward_guidance_flag",
    "guidance_staleness_flag",
    "no_guidance_negative_growth_flag",
    "ctgov_evidence_type",
    "company_strategy_category",
    "ctgov_review_bucket",
    "ctgov_manual_root_cause",
    "sec_catalyst_raw_score",
    "sec_catalyst_recency_adjusted_score",
    "sec_catalyst_score_used",
    "sec_catalyst_decay_delta",
    "sec_catalyst_latest_filing_date",
    "sec_catalyst_latest_event_date",
    "sec_catalyst_latest_event_type",
    "sec_catalyst_recency_days",
    "sec_catalyst_recency_basis",
    "sec_catalyst_event_types",
    "sec_event_recency_decay_enabled",
    "sec_event_recency_half_life_days",
    "sec_event_pre_decay_points",
    "sec_event_post_decay_points",
    "sec_event_decay_delta",
    "latest_positive_sec_event_age_days",
    "latest_positive_sec_event_type",
]

RANKING_DIAGNOSTIC_FIELDS = [
    "asof_date",
    "diagnostic_group",
    "ticker",
    "company_name",
    "prod_rank",
    "prod_score",
    "prod_action_tier",
    "shadow_rank",
    "shadow_score",
    "shadow_action_tier",
    "score_gap_prod_minus_shadow",
    "commercial_value_score",
    "forward_guidance_score",
    "quality_adjusted_guidance_score",
    "quality_adjusted_valuation_score",
    "institutional_upside_capacity_score",
    "mature_defensive_score",
    "expected_return_quality_score",
    "risk_score",
    "event_hard_reasons",
    "rank_quality_cap",
    "rank_quality_cap_reasons",
    "rank_quality_cap_vetoed",
    "production_policy_quality_penalty",
    "production_policy_quality_bonus",
]

COHORT_DIAGNOSTIC_FIELDS = [
    "asof_date",
    "biotech_primary_cohort",
    "company_count",
    "top10_count",
    "top20_count",
    "avg_rank",
    "avg_opportunity_score",
    "avg_investment_score",
    "avg_taxonomy_confidence",
    "avg_cohort_reliability_score",
    "review_required_count",
    "sparse_data_count",
    "non_investible_count",
    "global_fallback_count",
    "cohort_specific_count",
    "calibration_excluded_count",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Publish Tier-1 biotech index reports.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--asof", type=str, default="", help="Report date in YYYY-MM-DD. Defaults to latest score date.")
    return parser.parse_args()


def configure_logging() -> None:
    configure_utc_logging()


def parse_date_text(raw: object) -> str:
    text = str(raw or "").strip()
    if not text:
        return ""
    try:
        return datetime.strptime(text, "%Y-%m-%d").date().isoformat()
    except ValueError as exc:
        raise ValueError(f"Invalid date: {text}") from exc


def compact_asof(asof_date: str) -> str:
    parsed = parse_date_text(asof_date)
    return datetime.strptime(parsed, "%Y-%m-%d").strftime("%Y%m%d") if parsed else str(asof_date).replace("-", "")


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
        key=lambda path: (
            1 if path.parent.name.isdigit() and len(path.parent.name) == 8 else 0,
            path.parent.name,
            path.stat().st_mtime,
        ),
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


def normalized_ticker_list(raw: object, default: list[str] | None = None) -> list[str]:
    if raw is None:
        return list(default or [])
    if isinstance(raw, str):
        values = [part.strip() for part in raw.replace(";", ",").split(",")]
    elif isinstance(raw, (list, tuple, set)):
        values = [str(part).strip() for part in raw]
    else:
        values = [str(raw).strip()]
    return [value.upper() for value in values if value.strip()]


def action_tier_settings(config: dict[str, Any]) -> dict[str, float]:
    settings = cfg_get(config, "biotech_reports.action_tiers", {}) or {}
    if not isinstance(settings, dict):
        settings = {}
    return {
        "high_confidence_score_min": float(settings.get("high_confidence_score_min", 55.0)),
        "allocation_rank_max": float(settings.get("allocation_rank_max", 10)),
        "allocation_score_min": float(settings.get("allocation_score_min", 50.0)),
        "research_rank_max": float(settings.get("research_rank_max", 20)),
        "research_score_min": float(settings.get("research_score_min", 45.0)),
    }


def apply_action_tier(
    row: dict[str, Any],
    settings: dict[str, float],
    *,
    score_field: str,
    rank_field: str = "rank",
) -> dict[str, Any]:
    out = dict(row)
    rank = int(to_float(out.get(rank_field), 0.0))
    score = to_float(out.get(score_field), 0.0)
    allocation_rank_max = int(settings["allocation_rank_max"])
    high_confidence_score_min = float(settings["high_confidence_score_min"])
    allocation_score_min = float(settings["allocation_score_min"])
    research_rank_max = int(settings["research_rank_max"])
    research_score_min = float(settings["research_score_min"])

    if to_float(out.get("biotech_cohort_investible_flag"), 1.0) <= 0.0:
        tier = "low_priority"
        reason = str(out.get("biotech_cohort_exclusion_reason") or "non_investible_cohort")
        allocation_flag = 0
        research_flag = 0
    elif rank > 0 and rank <= allocation_rank_max and score >= high_confidence_score_min:
        tier = "high_confidence_allocation"
        reason = f"rank<={allocation_rank_max} and score>={high_confidence_score_min:g}"
        allocation_flag = 1
        research_flag = 0
    elif rank > 0 and rank <= allocation_rank_max and score >= allocation_score_min:
        tier = "allocation_candidate"
        reason = f"rank<={allocation_rank_max} and score>={allocation_score_min:g}"
        allocation_flag = 1
        research_flag = 0
    elif rank > 0 and score >= research_score_min and rank <= research_rank_max:
        tier = "research_watchlist"
        reason = f"rank<={research_rank_max} and score>={research_score_min:g}"
        allocation_flag = 0
        research_flag = 1
    else:
        tier = "low_priority"
        reason = f"score<{research_score_min:g} or rank>{research_rank_max}"
        allocation_flag = 0
        research_flag = 0

    out["action_tier"] = tier
    out["action_tier_reason"] = reason
    out["allocation_candidate_flag"] = allocation_flag
    out["research_watchlist_flag"] = research_flag
    return out


def latest_score_date(conn: sqlite3.Connection) -> str:
    null_count = int(conn.execute("SELECT COUNT(*) AS n FROM daily_scores WHERE asof_date IS NULL").fetchone()["n"] or 0)
    if null_count:
        raise ValueError(f"daily_scores contains {null_count} row(s) with NULL asof_date")
    row = conn.execute("SELECT MAX(asof_date) AS asof_date FROM daily_scores").fetchone()
    asof = str(row["asof_date"] or "") if row else ""
    if not asof:
        raise ValueError("No daily_scores rows found. Run 11_score_biotech_index.py first.")
    return asof


def previous_score_date(conn: sqlite3.Connection, asof_date: str) -> str:
    row = conn.execute(
        "SELECT MAX(asof_date) AS asof_date FROM daily_scores WHERE asof_date < ?",
        (asof_date,),
    ).fetchone()
    return str(row["asof_date"] or "") if row else ""


def load_scores(conn: sqlite3.Connection, asof_date: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT
            s.asof_date, s.company_id, s.catalyst_score, s.credibility_score,
            s.financial_quality_score, s.risk_score, s.momentum_score,
            s.clinical_opportunity_score, s.commercial_quality_score,
            s.commercial_value_score, s.valuation_score,
            s.forward_guidance_score, s.upside_capacity_score, s.investment_score, s.opportunity_score,
            s.tier1_selection_gate_score, s.data_quality_confidence_multiplier,
            s.clinical_risk_drag, s.investment_risk_drag,
            s.biotech_primary_cohort, s.biotech_secondary_cohort, s.biotech_cohort_reason_codes,
            s.biotech_cohort_confidence, s.biotech_cohort_margin, s.biotech_cohort_source,
            s.biotech_cohort_overlays, s.biotech_cohort_data_quality, s.biotech_taxonomy_review_required,
            s.biotech_cohort_sparse_data_flag, s.biotech_cohort_size, s.biotech_cohort_rank,
            s.biotech_cohort_percentile, s.biotech_cohort_percentile_shrunk,
            s.biotech_cohort_reliability_score, s.biotech_cohort_calibration_weight,
            s.biotech_cohort_investible_flag, s.biotech_cohort_calibration_eligible_flag,
            s.biotech_cohort_calibration_mode, s.biotech_cohort_exclusion_reason,
            s.biotech_cohort_model_version,
            s.rank, s.bucket, s.top_evidence_json,
            s.ctgov_evidence_type, s.company_strategy_category,
            s.ctgov_review_bucket, s.ctgov_manual_root_cause,
            c.ticker, c.company_name, c.exchange, c.industry, c.industry_aggregate
        FROM daily_scores s
        JOIN companies c ON c.company_id = s.company_id
        WHERE s.asof_date = ?
        ORDER BY s.rank
        """,
        (asof_date,),
    ).fetchall()
    return [dict(row) for row in rows]


def load_features(conn: sqlite3.Connection, asof_date: str) -> dict[int, dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT company_id, catalyst_score_raw, credibility_score_raw, risk_score_raw, feature_json
        FROM daily_features
        WHERE asof_date = ?
        """,
        (asof_date,),
    ).fetchall()
    return {int(row["company_id"]): dict(row) for row in rows}


def write_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, lineterminator="\n")
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in fieldnames})


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True), encoding="utf-8")


def read_csv_rows(path: Path, *, required: bool = False) -> list[dict[str, str]]:
    if not path.exists():
        if required:
            raise FileNotFoundError(f"Required CSV not found: {path}")
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        return [{str(k): str(v or "") for k, v in row.items()} for row in reader]


def apply_trial_status_overrides(rows: list[dict[str, str]], overrides: list[dict[str, str]]) -> list[dict[str, str]]:
    if not rows or not overrides:
        return rows
    out = [dict(row) for row in rows]
    for row in out:
        row.setdefault("outcome_override_applied", "")
        row.setdefault("outcome_override_status", "")
        row.setdefault("outcome_override_reason", "")
        row.setdefault("outcome_override_source_url", "")
        row.setdefault("outcome_override_manual_review", "")

    override_index = {
        (str(override.get("ticker") or "").strip().upper(), str(override.get("nct_id") or "").strip().upper()): override
        for override in overrides
        if bool_text(override.get("enabled", "true"))
        and str(override.get("ticker") or "").strip()
        and str(override.get("nct_id") or "").strip()
    }
    for row in out:
        override = override_index.get(
            (str(row.get("ticker") or "").strip().upper(), str(row.get("nct_id") or "").strip().upper())
        )
        if not override:
            continue
        status = str(override.get("override_status") or "").strip()
        reason = str(override.get("override_reason") or "").strip()
        source_url = str(override.get("source_url") or "").strip()
        row["outcome_override_applied"] = "True"
        row["outcome_override_status"] = status
        row["outcome_override_reason"] = reason
        row["outcome_override_source_url"] = source_url
        row["outcome_override_manual_review"] = "True" if bool_text(override.get("manual_review")) else "False"
        if bool_text(override.get("exclude_from_scoring")):
            row["is_active_status"] = "False"
            row["qualifying_trial"] = "False"
            row["trial_score"] = "0.0"
            suffix = f"outcome_override:{status}" if status else "outcome_override"
            row["exclusion_reasons"] = ";".join(part for part in [str(row.get("exclusion_reasons") or ""), suffix] if part)
    return out


def assert_output_paths_writable(paths: list[Path]) -> None:
    locked: list[str] = []
    for path in paths:
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with path.open("a", encoding="utf-8"):
                pass
        except PermissionError:
            locked.append(str(path))
    if locked:
        raise PermissionError("Report output file is not writable. Close the file and rerun: " + "; ".join(locked))


def build_index_summary(
    scores: list[dict[str, Any]],
    asof_date: str,
    top_n: int,
    *,
    weights: dict[str, float] | None = None,
) -> dict[str, Any]:
    values = [to_float(row.get("opportunity_score")) for row in scores]
    top_values = values[:top_n]
    top_n_avg_score = round(sum(top_values) / len(top_values), 4) if top_values else 0.0
    median_score = round(median(values), 4) if values else 0.0
    full_universe_avg_score = round(sum(values) / len(values), 4) if values else 0.0
    raw_weights = weights or {}
    top_weight = float(raw_weights.get("top_n_avg_score", 0.70))
    universe_weight = float(raw_weights.get("full_universe_avg_score", 0.20))
    median_weight = float(raw_weights.get("median_score", 0.10))
    total_weight = top_weight + universe_weight + median_weight
    if total_weight <= 0.0:
        raise ValueError("biotech_reports.index_weights must sum to a positive value")
    top_weight, universe_weight, median_weight = (
        top_weight / total_weight,
        universe_weight / total_weight,
        median_weight / total_weight,
    )
    # Blend leader strength with universe breadth so the index is not just an alias for top-N average.
    index_level = round(
        (top_weight * top_n_avg_score)
        + (universe_weight * full_universe_avg_score)
        + (median_weight * median_score),
        4,
    )
    bucket_counts: dict[str, int] = {}
    for row in scores:
        bucket = str(row.get("bucket") or "unknown")
        bucket_counts[bucket] = bucket_counts.get(bucket, 0) + 1
    return {
        "asof_date": asof_date,
        "company_count": len(scores),
        "top_n": top_n,
        "biotech_opportunity_index_level": index_level,
        "top_n_avg_score": top_n_avg_score,
        "full_universe_avg_score": full_universe_avg_score,
        "median_score": median_score,
        "max_score": round(max(values), 4) if values else 0.0,
        "high_conviction_count": bucket_counts.get("high_conviction", 0),
        "watchlist_count": bucket_counts.get("watchlist", 0),
        "speculative_count": bucket_counts.get("speculative", 0),
        "avoid_count": bucket_counts.get("avoid", 0),
        "index_method": (
            f"{top_weight:.2f}*top_n_avg_score+"
            f"{universe_weight:.2f}*full_universe_avg_score+"
            f"{median_weight:.2f}*median_score"
        ),
    }


def parse_json_object(raw: object, *, context: str, ticker: object = "") -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    try:
        payload = json.loads(str(raw or "{}"))
    except json.JSONDecodeError as exc:
        LOGGER.error("Malformed JSON in %s for ticker=%s: %s", context, ticker, exc)
        return {}
    return payload if isinstance(payload, dict) else {}


def flatten_score_row(row: dict[str, Any]) -> dict[str, Any]:
    evidence = parse_json_object(row.get("top_evidence_json"), context="top_evidence_json", ticker=row.get("ticker"))
    risk_flags = evidence.get("risk_flags", {}) if isinstance(evidence, dict) else {}
    sec_events = evidence.get("sec_events", {}) if isinstance(evidence, dict) else {}
    ctgov_quality = evidence.get("ctgov_quality", {}) if isinstance(evidence, dict) else {}
    commercial_value = evidence.get("commercial_value", {}) if isinstance(evidence, dict) else {}
    forward_guidance = evidence.get("forward_guidance", {}) if isinstance(evidence, dict) else {}
    guidance_records = forward_guidance.get("guidance_records", []) if isinstance(forward_guidance, dict) else []
    if not isinstance(guidance_records, list):
        guidance_records = []
    primary_guidance = next((item for item in guidance_records if isinstance(item, dict) and item.get("metric") == "revenue"), None)
    if primary_guidance is None:
        primary_guidance = next((item for item in guidance_records if isinstance(item, dict)), {})
    score_components = evidence.get("score_components", {}) if isinstance(evidence, dict) else {}
    biotech_taxonomy = evidence.get("biotech_taxonomy", {}) if isinstance(evidence, dict) else {}
    core_veto = evidence.get("core_structural_veto", {}) if isinstance(evidence, dict) else {}
    production_baseline = evidence.get("production_baseline", {}) if isinstance(evidence, dict) else {}
    core_veto_flag = core_veto.get("flag", risk_flags.get("core_structural_veto_flag", "")) if isinstance(core_veto, dict) else ""
    core_veto_reasons = core_veto.get("reasons", risk_flags.get("core_structural_veto_reasons", "")) if isinstance(core_veto, dict) else ""
    if isinstance(core_veto_reasons, list):
        core_veto_reasons = "|".join(str(reason) for reason in core_veto_reasons)
    sec_catalyst_recency_days = sec_events.get("sec_catalyst_recency_days", "") if isinstance(sec_events, dict) else ""
    sec_catalyst_recency_basis = sec_events.get("sec_catalyst_recency_basis", "") if isinstance(sec_events, dict) else ""
    latest_positive_sec_event_age_days = (
        ""
        if sec_catalyst_recency_basis == "pdufa_event_date_proximity"
        else sec_catalyst_recency_days
    )
    return {
        "asof_date": row.get("asof_date", ""),
        "rank": row.get("rank", ""),
        "ticker": row.get("ticker", ""),
        "company_name": row.get("company_name", ""),
        "bucket": row.get("bucket", ""),
        "opportunity_score": row.get("opportunity_score", ""),
        "investment_score": row.get("investment_score", score_components.get("investment_score", "") if isinstance(score_components, dict) else ""),
        "investment_profile": score_components.get("investment_profile", "") if isinstance(score_components, dict) else "",
        "biotech_primary_cohort": row.get("biotech_primary_cohort", biotech_taxonomy.get("biotech_primary_cohort", "") if isinstance(biotech_taxonomy, dict) else ""),
        "biotech_secondary_cohort": row.get("biotech_secondary_cohort", biotech_taxonomy.get("biotech_secondary_cohort", "") if isinstance(biotech_taxonomy, dict) else ""),
        "biotech_cohort_reason_codes": row.get("biotech_cohort_reason_codes", biotech_taxonomy.get("biotech_cohort_reason_codes", "") if isinstance(biotech_taxonomy, dict) else ""),
        "biotech_cohort_confidence": row.get("biotech_cohort_confidence", biotech_taxonomy.get("biotech_cohort_confidence", "") if isinstance(biotech_taxonomy, dict) else ""),
        "biotech_cohort_margin": row.get("biotech_cohort_margin", biotech_taxonomy.get("biotech_cohort_margin", "") if isinstance(biotech_taxonomy, dict) else ""),
        "biotech_cohort_source": row.get("biotech_cohort_source", biotech_taxonomy.get("biotech_cohort_source", "") if isinstance(biotech_taxonomy, dict) else ""),
        "biotech_cohort_overlays": row.get("biotech_cohort_overlays", biotech_taxonomy.get("biotech_cohort_overlays", "") if isinstance(biotech_taxonomy, dict) else ""),
        "biotech_cohort_data_quality": row.get("biotech_cohort_data_quality", biotech_taxonomy.get("biotech_cohort_data_quality", "") if isinstance(biotech_taxonomy, dict) else ""),
        "biotech_taxonomy_review_required": row.get("biotech_taxonomy_review_required", biotech_taxonomy.get("biotech_taxonomy_review_required", "") if isinstance(biotech_taxonomy, dict) else ""),
        "biotech_cohort_sparse_data_flag": row.get("biotech_cohort_sparse_data_flag", biotech_taxonomy.get("biotech_cohort_sparse_data_flag", "") if isinstance(biotech_taxonomy, dict) else ""),
        "biotech_cohort_size": row.get("biotech_cohort_size", biotech_taxonomy.get("biotech_cohort_size", "") if isinstance(biotech_taxonomy, dict) else ""),
        "biotech_cohort_rank": row.get("biotech_cohort_rank", biotech_taxonomy.get("biotech_cohort_rank", "") if isinstance(biotech_taxonomy, dict) else ""),
        "biotech_cohort_percentile": row.get("biotech_cohort_percentile", biotech_taxonomy.get("biotech_cohort_percentile", "") if isinstance(biotech_taxonomy, dict) else ""),
        "biotech_cohort_percentile_shrunk": row.get("biotech_cohort_percentile_shrunk", biotech_taxonomy.get("biotech_cohort_percentile_shrunk", "") if isinstance(biotech_taxonomy, dict) else ""),
        "biotech_cohort_reliability_score": row.get("biotech_cohort_reliability_score", biotech_taxonomy.get("biotech_cohort_reliability_score", "") if isinstance(biotech_taxonomy, dict) else ""),
        "biotech_cohort_calibration_weight": row.get("biotech_cohort_calibration_weight", biotech_taxonomy.get("biotech_cohort_calibration_weight", "") if isinstance(biotech_taxonomy, dict) else ""),
        "biotech_cohort_investible_flag": row.get("biotech_cohort_investible_flag", biotech_taxonomy.get("biotech_cohort_investible_flag", "") if isinstance(biotech_taxonomy, dict) else ""),
        "biotech_cohort_calibration_eligible_flag": row.get("biotech_cohort_calibration_eligible_flag", biotech_taxonomy.get("biotech_cohort_calibration_eligible_flag", "") if isinstance(biotech_taxonomy, dict) else ""),
        "biotech_cohort_calibration_mode": row.get("biotech_cohort_calibration_mode", biotech_taxonomy.get("biotech_cohort_calibration_mode", "") if isinstance(biotech_taxonomy, dict) else ""),
        "biotech_cohort_exclusion_reason": row.get("biotech_cohort_exclusion_reason", biotech_taxonomy.get("biotech_cohort_exclusion_reason", "") if isinstance(biotech_taxonomy, dict) else ""),
        "biotech_cohort_model_version": row.get("biotech_cohort_model_version", biotech_taxonomy.get("biotech_cohort_model_version", "") if isinstance(biotech_taxonomy, dict) else ""),
        "clinical_opportunity_score": row.get("clinical_opportunity_score", score_components.get("clinical_opportunity_score", "") if isinstance(score_components, dict) else ""),
        "tier1_selection_gate_score": row.get("tier1_selection_gate_score", score_components.get("tier1_selection_gate_score", "") if isinstance(score_components, dict) else ""),
        "tier1_primary_horizon_trading_days": production_baseline.get("primary_horizon_trading_days", score_components.get("primary_horizon_trading_days", "") if isinstance(score_components, dict) else "") if isinstance(production_baseline, dict) else "",
        "tier1_production_score_model": production_baseline.get("score_model", score_components.get("production_baseline_score_model", "") if isinstance(score_components, dict) else "") if isinstance(production_baseline, dict) else "",
        "tier1_selection_policy": production_baseline.get("selection_policy", score_components.get("selection_policy", "") if isinstance(score_components, dict) else "") if isinstance(production_baseline, dict) else "",
        "alpha_multibagger_role": production_baseline.get("alpha_multibagger_role", score_components.get("alpha_multibagger_role", "") if isinstance(score_components, dict) else "") if isinstance(production_baseline, dict) else "",
        "core_structural_veto_flag": core_veto_flag,
        "core_structural_veto_reasons": core_veto_reasons,
        "rank_demoted_by_core_veto": core_veto.get("rank_demoted_by_core_veto", "") if isinstance(core_veto, dict) else "",
        "data_quality_confidence_multiplier": row.get("data_quality_confidence_multiplier", score_components.get("data_quality_confidence_multiplier", "") if isinstance(score_components, dict) else ""),
        "clinical_risk_drag": row.get("clinical_risk_drag", score_components.get("clinical_risk_drag", "") if isinstance(score_components, dict) else ""),
        "investment_risk_drag": row.get("investment_risk_drag", score_components.get("investment_risk_drag", "") if isinstance(score_components, dict) else ""),
        "effective_total_risk_drag": score_components.get("effective_total_risk_drag", "") if isinstance(score_components, dict) else "",
        "production_policy_quality_penalty": row.get("production_policy_quality_penalty", score_components.get("production_policy_quality_penalty", "") if isinstance(score_components, dict) else ""),
        "production_policy_quality_bonus": row.get("production_policy_quality_bonus", score_components.get("production_policy_quality_bonus", "") if isinstance(score_components, dict) else ""),
        "rank_quality_cap": row.get("rank_quality_cap", score_components.get("rank_quality_cap", "") if isinstance(score_components, dict) else ""),
        "rank_quality_cap_reasons": row.get("rank_quality_cap_reasons", "|".join(str(reason) for reason in score_components.get("rank_quality_cap_reasons", [])) if isinstance(score_components, dict) and isinstance(score_components.get("rank_quality_cap_reasons"), list) else score_components.get("rank_quality_cap_reasons", "") if isinstance(score_components, dict) else ""),
        "rank_quality_cap_vetoed": row.get("rank_quality_cap_vetoed", score_components.get("rank_quality_cap_vetoed", "") if isinstance(score_components, dict) else ""),
        "rank_quality_cap_veto_reasons": row.get("rank_quality_cap_veto_reasons", "|".join(str(reason) for reason in score_components.get("rank_quality_cap_veto_reasons", [])) if isinstance(score_components, dict) and isinstance(score_components.get("rank_quality_cap_veto_reasons"), list) else score_components.get("rank_quality_cap_veto_reasons", "") if isinstance(score_components, dict) else ""),
        "event_hard_reasons": row.get("event_hard_weakness_reasons", "|".join(str(reason) for reason in score_components.get("production_policy_event_hard_reasons", [])) if isinstance(score_components, dict) and isinstance(score_components.get("production_policy_event_hard_reasons"), list) else risk_flags.get("event_hard_weakness_reasons", "") if isinstance(risk_flags, dict) else ""),
        "event_hard_weakness_reasons": row.get("event_hard_weakness_reasons", "|".join(str(reason) for reason in score_components.get("production_policy_event_hard_reasons", [])) if isinstance(score_components, dict) and isinstance(score_components.get("production_policy_event_hard_reasons"), list) else risk_flags.get("event_hard_weakness_reasons", "") if isinstance(risk_flags, dict) else ""),
        "commercial_quality_score": row.get("commercial_quality_score", commercial_value.get("commercial_quality_score", "") if isinstance(commercial_value, dict) else ""),
        "commercial_value_score": row.get("commercial_value_score", commercial_value.get("commercial_value_score", "") if isinstance(commercial_value, dict) else ""),
        "forward_guidance_score": row.get("forward_guidance_score", score_components.get("forward_guidance_score", "") if isinstance(score_components, dict) else ""),
        "valuation_score": row.get("valuation_score", commercial_value.get("valuation_score", "") if isinstance(commercial_value, dict) else ""),
        "quality_adjusted_valuation_score": row.get("quality_adjusted_valuation_score", commercial_value.get("quality_adjusted_valuation_score", "") if isinstance(commercial_value, dict) else ""),
        "quality_adjusted_guidance_score": row.get("quality_adjusted_guidance_score", forward_guidance.get("quality_adjusted_guidance_score", score_components.get("quality_adjusted_guidance_score", "") if isinstance(score_components, dict) else "") if isinstance(forward_guidance, dict) else ""),
        "upside_capacity_score": row.get("upside_capacity_score", commercial_value.get("upside_capacity_score", "") if isinstance(commercial_value, dict) else ""),
        "institutional_upside_capacity_score": row.get("institutional_upside_capacity_score", commercial_value.get("institutional_upside_capacity_score", "") if isinstance(commercial_value, dict) else ""),
        "leverage_score": row.get("leverage_score", commercial_value.get("leverage_score", "") if isinstance(commercial_value, dict) else ""),
        "leverage_fragility_score": row.get("leverage_fragility_score", score_components.get("leverage_fragility_score", "") if isinstance(score_components, dict) else ""),
        "value_trap_score": row.get("value_trap_score", commercial_value.get("value_trap_score", "") if isinstance(commercial_value, dict) else ""),
        "mature_defensive_score": row.get("mature_defensive_score", score_components.get("mature_defensive_score", "") if isinstance(score_components, dict) else ""),
        "expected_return_quality_score": row.get("expected_return_quality_score", score_components.get("expected_return_quality_score", "") if isinstance(score_components, dict) else ""),
        "no_forward_guidance_flag": row.get("no_forward_guidance_flag", score_components.get("no_forward_guidance_flag", "") if isinstance(score_components, dict) else ""),
        "guidance_staleness_flag": row.get("guidance_staleness_flag", score_components.get("guidance_staleness_flag", "") if isinstance(score_components, dict) else ""),
        "guidance_stale_flag": row.get("guidance_stale_flag", score_components.get("guidance_stale_flag", "") if isinstance(score_components, dict) else ""),
        "no_guidance_negative_growth_flag": row.get("no_guidance_negative_growth_flag", score_components.get("no_guidance_negative_growth_flag", "") if isinstance(score_components, dict) else ""),
        "catalyst_score": row.get("catalyst_score", ""),
        "credibility_score": row.get("credibility_score", ""),
        "financial_quality_score": row.get("financial_quality_score", ""),
        "risk_score": row.get("risk_score", ""),
        "momentum_score": row.get("momentum_score", ""),
        "primary_nct": evidence.get("primary_nct", "") if isinstance(evidence, dict) else "",
        "primary_trial_title": evidence.get("primary_trial_title", "") if isinstance(evidence, dict) else "",
        "ctgov_evidence_type": row.get("ctgov_evidence_type", ctgov_quality.get("ctgov_evidence_type", "") if isinstance(ctgov_quality, dict) else ""),
        "company_strategy_category": row.get("company_strategy_category", ctgov_quality.get("company_strategy_category", "") if isinstance(ctgov_quality, dict) else ""),
        "ctgov_review_bucket": row.get("ctgov_review_bucket", ctgov_quality.get("ctgov_review_bucket", "") if isinstance(ctgov_quality, dict) else ""),
        "ctgov_manual_root_cause": row.get("ctgov_manual_root_cause", ctgov_quality.get("ctgov_manual_root_cause", "") if isinstance(ctgov_quality, dict) else ""),
        "lead_phase2_3_active_trials": ctgov_quality.get("lead_phase2_3_active_trials", "") if isinstance(ctgov_quality, dict) else "",
        "program_phase2_3_active_trials": ctgov_quality.get("program_phase2_3_active_trials", "") if isinstance(ctgov_quality, dict) else "",
        "collaborator_phase2_3_active_trials": ctgov_quality.get("collaborator_phase2_3_active_trials", "") if isinstance(ctgov_quality, dict) else "",
        "effective_phase2_3_trials": ctgov_quality.get("effective_phase2_3_trials", "") if isinstance(ctgov_quality, dict) else "",
        "core_pipeline_quality_score": ctgov_quality.get("core_pipeline_quality_score", "") if isinstance(ctgov_quality, dict) else "",
        "collaborator_dependency_ratio": ctgov_quality.get("collaborator_dependency_ratio", "") if isinstance(ctgov_quality, dict) else "",
        "collaborator_heavy_flag": ctgov_quality.get("collaborator_heavy_flag", "") if isinstance(ctgov_quality, dict) else "",
        "going_concern_status": risk_flags.get("going_concern_status", "") if isinstance(risk_flags, dict) else "",
        "reverse_split_hits_2y": risk_flags.get("reverse_split_hits_2y", "") if isinstance(risk_flags, dict) else "",
        "median_addv20": risk_flags.get("median_addv20", "") if isinstance(risk_flags, dict) else "",
        "cash_runway_months": risk_flags.get("cash_runway_months", "") if isinstance(risk_flags, dict) else "",
        "financial_survival_score": risk_flags.get("financial_survival_score", "") if isinstance(risk_flags, dict) else "",
        "ttm_revenue": commercial_value.get("ttm_revenue", "") if isinstance(commercial_value, dict) else "",
        "revenue_yoy_growth_pct": commercial_value.get("revenue_yoy_growth_pct", "") if isinstance(commercial_value, dict) else "",
        "gross_margin_pct": commercial_value.get("gross_margin_pct", "") if isinstance(commercial_value, dict) else "",
        "net_margin_pct": commercial_value.get("net_margin_pct", "") if isinstance(commercial_value, dict) else "",
        "market_cap": commercial_value.get("market_cap", "") if isinstance(commercial_value, dict) else "",
        "ev_to_sales": commercial_value.get("ev_to_sales", "") if isinstance(commercial_value, dict) else "",
        "pe_ratio": commercial_value.get("pe_ratio", "") if isinstance(commercial_value, dict) else "",
        "commercial_stage_flag": commercial_value.get("commercial_stage_flag", "") if isinstance(commercial_value, dict) else "",
        "profitable_flag": commercial_value.get("profitable_flag", "") if isinstance(commercial_value, dict) else "",
        "latest_guidance_filing_date": forward_guidance.get("latest_guidance_filing_date", "") if isinstance(forward_guidance, dict) else "",
        "forward_revenue_midpoint": forward_guidance.get("forward_revenue_midpoint", "") if isinstance(forward_guidance, dict) else "",
        "forward_revenue_growth_pct": forward_guidance.get("forward_revenue_growth_pct", "") if isinstance(forward_guidance, dict) else "",
        "forward_ebitda_midpoint": forward_guidance.get("forward_ebitda_midpoint", "") if isinstance(forward_guidance, dict) else "",
        "forward_ebitda_margin_pct": forward_guidance.get("forward_ebitda_margin_pct", "") if isinstance(forward_guidance, dict) else "",
        "guidance_confidence": forward_guidance.get("guidance_confidence", "") if isinstance(forward_guidance, dict) else "",
        "forward_guidance_data_quality": forward_guidance.get("data_quality", "") if isinstance(forward_guidance, dict) else "",
        "forward_guidance_source_type": primary_guidance.get("source_type", "") if isinstance(primary_guidance, dict) else "",
        "forward_guidance_source_name": primary_guidance.get("source_name", "") if isinstance(primary_guidance, dict) else "",
        "forward_guidance_source_url": primary_guidance.get("source_url", "") if isinstance(primary_guidance, dict) else "",
        "forward_guidance_override_reason": primary_guidance.get("override_reason", "") if isinstance(primary_guidance, dict) else "",
        "financial_data_quality": risk_flags.get("financial_data_quality", "") if isinstance(risk_flags, dict) else "",
        "sec_regulatory_catalyst_count": sec_events.get("regulatory_catalyst_count", "") if isinstance(sec_events, dict) else "",
        "sec_catalyst_raw_score": sec_events.get("sec_catalyst_raw_score", "") if isinstance(sec_events, dict) else "",
        "sec_catalyst_recency_adjusted_score": sec_events.get("sec_catalyst_recency_adjusted_score", "") if isinstance(sec_events, dict) else "",
        "sec_catalyst_score_used": sec_events.get("sec_catalyst_score_used", "") if isinstance(sec_events, dict) else "",
        "sec_catalyst_decay_delta": sec_events.get("sec_catalyst_decay_delta", "") if isinstance(sec_events, dict) else "",
        "sec_catalyst_latest_filing_date": sec_events.get("sec_catalyst_latest_filing_date", "") if isinstance(sec_events, dict) else "",
        "sec_catalyst_latest_event_date": sec_events.get("sec_catalyst_latest_event_date", "") if isinstance(sec_events, dict) else "",
        "sec_catalyst_latest_event_type": sec_events.get("sec_catalyst_latest_event_type", "") if isinstance(sec_events, dict) else "",
        "sec_catalyst_recency_days": sec_catalyst_recency_days,
        "sec_catalyst_recency_basis": sec_catalyst_recency_basis,
        "sec_catalyst_event_types": sec_events.get("sec_catalyst_event_types", "") if isinstance(sec_events, dict) else "",
        "sec_event_recency_decay_enabled": sec_events.get("sec_catalyst_recency_decay_enabled", "") if isinstance(sec_events, dict) else "",
        "sec_event_recency_half_life_days": sec_events.get("sec_catalyst_decay_half_life_days", "") if isinstance(sec_events, dict) else "",
        "sec_event_pre_decay_points": first_nonblank(
            sec_events.get("sec_event_pre_decay_points", ""),
            sec_events.get("sec_catalyst_raw_score", ""),
        )
        if isinstance(sec_events, dict)
        else "",
        "sec_event_post_decay_points": first_nonblank(
            sec_events.get("sec_event_post_decay_points", ""),
            sec_events.get("sec_catalyst_score_used", ""),
        )
        if isinstance(sec_events, dict)
        else "",
        "sec_event_decay_delta": first_nonblank(
            sec_events.get("sec_event_decay_delta", ""),
            sec_events.get("sec_catalyst_decay_delta", ""),
        )
        if isinstance(sec_events, dict)
        else "",
        "latest_positive_sec_event_age_days": latest_positive_sec_event_age_days,
        "latest_positive_sec_event_type": sec_events.get("sec_catalyst_latest_event_type", "") if isinstance(sec_events, dict) else "",
        "sec_dilution_event_count": sec_events.get("dilution_event_count", "") if isinstance(sec_events, dict) else "",
        "sec_negative_clinical_event_count": sec_events.get("negative_clinical_event_count", "") if isinstance(sec_events, dict) else "",
        "industry": row.get("industry", ""),
        "industry_aggregate": row.get("industry_aggregate", ""),
    }


def build_cohort_diagnostics(rows: list[dict[str, Any]], *, asof_date: str) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        cohort = str(row.get("biotech_primary_cohort") or "unclassified_review")
        grouped.setdefault(cohort, []).append(row)

    def mean_numeric(items: list[dict[str, Any]], field: str) -> float:
        values: list[float] = []
        for item in items:
            raw_value = item.get(field)
            if raw_value is None:
                continue
            try:
                value = float(raw_value)
            except (TypeError, ValueError):
                continue
            if math.isfinite(value):
                values.append(value)
        return round(sum(values) / len(values), 4) if values else 0.0

    def rank_leq(row: dict[str, Any], threshold: int) -> bool:
        try:
            return int(float(row.get("rank") or 999999)) <= threshold
        except (TypeError, ValueError):
            return False

    out: list[dict[str, Any]] = []
    for cohort, cohort_rows in sorted(grouped.items()):
        out.append(
            {
                "asof_date": asof_date,
                "biotech_primary_cohort": cohort,
                "company_count": len(cohort_rows),
                "top10_count": sum(1 for row in cohort_rows if rank_leq(row, 10)),
                "top20_count": sum(1 for row in cohort_rows if rank_leq(row, 20)),
                "avg_rank": mean_numeric(cohort_rows, "rank"),
                "avg_opportunity_score": mean_numeric(cohort_rows, "opportunity_score"),
                "avg_investment_score": mean_numeric(cohort_rows, "investment_score"),
                "avg_taxonomy_confidence": mean_numeric(cohort_rows, "biotech_cohort_confidence"),
                "avg_cohort_reliability_score": mean_numeric(cohort_rows, "biotech_cohort_reliability_score"),
                "review_required_count": sum(1 for row in cohort_rows if str(row.get("biotech_taxonomy_review_required") or "").strip() in {"1", "1.0", "true", "True"}),
                "sparse_data_count": sum(1 for row in cohort_rows if str(row.get("biotech_cohort_sparse_data_flag") or "").strip() in {"1", "1.0", "true", "True"}),
                "non_investible_count": sum(1 for row in cohort_rows if to_float(row.get("biotech_cohort_investible_flag"), 1.0) <= 0.0),
                "global_fallback_count": sum(1 for row in cohort_rows if str(row.get("biotech_cohort_calibration_mode") or "") == "global_fallback"),
                "cohort_specific_count": sum(1 for row in cohort_rows if str(row.get("biotech_cohort_calibration_mode") or "") == "cohort_specific"),
                "calibration_excluded_count": sum(1 for row in cohort_rows if to_float(row.get("biotech_cohort_calibration_eligible_flag"), 1.0) <= 0.0),
            }
        )
    return out


def load_calibration_module(config: dict[str, Any] | None = None) -> Any:
    module_path = (
        resolve_path(
            cfg_get(config, "biotech_reports.calibration_shadow_module"),
            base_dir=PACKAGE_ROOT,
        )
        if config and cfg_get(config, "biotech_reports.calibration_shadow_module")
        else DEFAULT_CALIBRATION_SHADOW_MODULE
    )
    spec = importlib.util.spec_from_file_location("biotech_tier1_calibration_shadow", module_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load calibration module from {module_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def build_shadow_top_rows(
    conn: sqlite3.Connection,
    config: dict[str, Any],
    asof_date: str,
) -> list[dict[str, Any]]:
    shadow = cfg_get(config, "biotech_scoring.production_baseline.shadow_research_pool", {}) or {}
    if not isinstance(shadow, dict) or not shadow:
        return []
    candidate_name = str(shadow.get("candidate_name") or "").strip()
    policy_name = str(shadow.get("selection_policy") or "").strip()
    if not candidate_name or not policy_name:
        LOGGER.warning("Shadow research pool configured without candidate_name or selection_policy; skipping shadow report.")
        return []
    top_n = int(float(shadow.get("top_n", 20)))
    strict_feature_lag = str(shadow.get("strict_feature_lag", "false")).strip().lower() in {"1", "true", "yes", "on"}

    calibration = load_calibration_module(config)
    params = calibration.load_calibration_params(config)
    specs = {spec.candidate_name: spec for spec in calibration.generate_weight_specs(config)}
    policies = {policy.policy_name: policy for policy in calibration.generate_selection_policies(config)}
    if candidate_name not in specs:
        raise ValueError(f"Shadow research pool candidate not found in calibration specs: {candidate_name}")
    if policy_name not in policies:
        raise ValueError(f"Shadow research pool policy not found in calibration policies: {policy_name}")

    observations = calibration.load_observations(
        conn,
        [asof_date],
        set(),
        config,
        min_addv20=0.0,
        strict_feature_lag=strict_feature_lag,
        growth_drag_curve=params.growth_drag_curve,
    )
    ret_key = "_shadow_report_return"
    if any(ret_key in row for row in observations):
        raise RuntimeError(f"Reserved shadow return key already exists in calibration observations: {ret_key}")
    for row in observations:
        row[ret_key] = 0.0
    selected = calibration.select_top_rows(
        observations,
        specs[candidate_name],
        policies[policy_name],
        ret_key=ret_key,
        top_n=top_n,
        params=params,
    )
    taxonomy_rows = conn.execute(
        """
        SELECT
            c.ticker, s.biotech_primary_cohort, s.biotech_secondary_cohort,
            s.biotech_cohort_confidence, s.biotech_cohort_sparse_data_flag,
            s.biotech_cohort_investible_flag, s.biotech_cohort_calibration_eligible_flag,
            s.biotech_cohort_calibration_mode, s.biotech_cohort_exclusion_reason
        FROM daily_scores s
        JOIN companies c ON c.company_id = s.company_id
        WHERE s.asof_date = ?
        """,
        (asof_date,),
    ).fetchall()
    taxonomy_by_ticker = {str(row["ticker"] or "").upper(): dict(row) for row in taxonomy_rows}
    out: list[dict[str, Any]] = []
    tier_settings = action_tier_settings(config)
    for rank, row in enumerate(selected, start=1):
        ticker = str(row.get("ticker") or "").upper()
        taxonomy = taxonomy_by_ticker.get(ticker, {})
        shadow_row = {
            "asof_date": asof_date,
            "rank": rank,
            "ticker": row.get("ticker", ""),
            "company_name": row.get("company_name", ""),
            "shadow_candidate_name": candidate_name,
            "shadow_selection_policy": policy_name,
            "shadow_top_n": top_n,
            "shadow_score": round(to_float(row.get("candidate_selection_score")), 4),
            "biotech_primary_cohort": taxonomy.get("biotech_primary_cohort", ""),
            "biotech_secondary_cohort": taxonomy.get("biotech_secondary_cohort", ""),
            "biotech_cohort_investible_flag": taxonomy.get("biotech_cohort_investible_flag", ""),
            "biotech_cohort_calibration_eligible_flag": taxonomy.get("biotech_cohort_calibration_eligible_flag", ""),
            "biotech_cohort_calibration_mode": taxonomy.get("biotech_cohort_calibration_mode", ""),
            "biotech_cohort_exclusion_reason": taxonomy.get("biotech_cohort_exclusion_reason", ""),
            "biotech_cohort_confidence": taxonomy.get("biotech_cohort_confidence", ""),
            "biotech_cohort_sparse_data_flag": taxonomy.get("biotech_cohort_sparse_data_flag", ""),
            "candidate_investment_score": row.get("candidate_investment_score", ""),
            "candidate_pre_rank_cap_selection_score": row.get("candidate_pre_rank_cap_selection_score", ""),
            "candidate_clinical_opportunity_score": row.get("candidate_clinical_opportunity_score", ""),
            "profile_name": row.get("profile_name", ""),
            "commercial_value_score": row.get("commercial_value_score", ""),
            "forward_guidance_score": row.get("forward_guidance_score", ""),
            "valuation_score": row.get("valuation_score", ""),
            "quality_adjusted_valuation_score": row.get("quality_adjusted_valuation_score", ""),
            "quality_adjusted_guidance_score": row.get("quality_adjusted_guidance_score", ""),
            "institutional_upside_capacity_score": row.get("institutional_upside_capacity_score", ""),
            "value_trap_score": row.get("diag_value_trap_score", row.get("value_trap_score", "")),
            "leverage_score": row.get("leverage_score", ""),
            "leverage_fragility_score": row.get("diag_leverage_fragility_score", ""),
            "mature_defensive_score": row.get("diag_mature_defensive_score", ""),
            "expected_return_quality_score": row.get("diag_expected_return_quality_score", ""),
            "rank_quality_cap": row.get("rank_quality_cap", ""),
            "rank_quality_cap_flag": row.get("rank_quality_cap_flag", ""),
            "rank_quality_cap_reasons": row.get("rank_quality_cap_reasons", ""),
            "rank_quality_cap_vetoed": row.get("rank_quality_cap_vetoed", ""),
            "risk_score_raw": row.get("risk_score_raw", ""),
            "momentum_score_raw": row.get("momentum_score_raw", ""),
            "core_hard_weakness_reasons": first_nonblank(
                row.get("diag_core_hard_weakness_reasons"),
                row.get("core_hard_weakness_reasons"),
            ),
            "event_hard_weakness_reasons": first_nonblank(
                row.get("diag_event_hard_weakness_reasons"),
                row.get("event_hard_weakness_reasons"),
            ),
            "soft_weakness_reasons": first_nonblank(
                row.get("diag_soft_weakness_reasons"),
                row.get("soft_weakness_reasons"),
            ),
            "commercial_business_shock_score": first_nonblank(
                row.get("diag_commercial_business_shock_score"),
                row.get("commercial_business_shock_score"),
            ),
            "commercial_business_shock_reasons": first_nonblank(
                row.get("diag_commercial_business_shock_reasons"),
                row.get("commercial_business_shock_reasons"),
            ),
            "no_forward_guidance_flag": first_nonblank(
                row.get("diag_no_forward_guidance_flag"),
                row.get("no_forward_guidance_flag"),
            ),
            "guidance_staleness_flag": first_nonblank(
                row.get("diag_guidance_staleness_flag"),
                row.get("guidance_staleness_flag"),
            ),
            "no_guidance_negative_growth_flag": first_nonblank(
                row.get("diag_no_guidance_negative_growth_flag"),
                row.get("no_guidance_negative_growth_flag"),
            ),
            "ctgov_evidence_type": row.get("ctgov_evidence_type", ""),
            "company_strategy_category": row.get("company_strategy_category", ""),
            "ctgov_review_bucket": row.get("ctgov_review_bucket", ""),
            "ctgov_manual_root_cause": row.get("ctgov_manual_root_cause", ""),
            "sec_catalyst_raw_score": row.get("sec_catalyst_raw_score", ""),
            "sec_catalyst_recency_adjusted_score": row.get("sec_catalyst_recency_adjusted_score", ""),
            "sec_catalyst_score_used": row.get("sec_catalyst_score_used", ""),
            "sec_catalyst_decay_delta": row.get("sec_catalyst_decay_delta", ""),
            "sec_catalyst_latest_filing_date": row.get("sec_catalyst_latest_filing_date", ""),
            "sec_catalyst_latest_event_date": row.get("sec_catalyst_latest_event_date", ""),
            "sec_catalyst_latest_event_type": row.get("sec_catalyst_latest_event_type", ""),
            "sec_catalyst_recency_days": row.get("sec_catalyst_recency_days", ""),
            "sec_catalyst_recency_basis": row.get("sec_catalyst_recency_basis", ""),
            "sec_catalyst_event_types": row.get("sec_catalyst_event_types", ""),
            "sec_event_recency_decay_enabled": row.get("sec_event_recency_decay_enabled", ""),
            "sec_event_recency_half_life_days": row.get("sec_event_recency_half_life_days", ""),
            "sec_event_pre_decay_points": row.get("sec_event_pre_decay_points", ""),
            "sec_event_post_decay_points": row.get("sec_event_post_decay_points", ""),
            "sec_event_decay_delta": row.get("sec_event_decay_delta", ""),
            "latest_positive_sec_event_age_days": row.get("latest_positive_sec_event_age_days", ""),
            "latest_positive_sec_event_type": row.get("latest_positive_sec_event_type", ""),
        }
        out.append(apply_action_tier(shadow_row, tier_settings, score_field="shadow_score"))
    return out


def build_ranking_order_diagnostics(
    *,
    config: dict[str, Any],
    asof_date: str,
    production_rows_by_ticker: dict[str, dict[str, Any]],
    shadow_rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    diagnostic_config = cfg_get(config, "biotech_reports.ranking_diagnostics", {}) or {}
    if not isinstance(diagnostic_config, dict):
        diagnostic_config = {}
    over_ranked = normalized_ticker_list(
        diagnostic_config.get("over_ranked_tickers"),
        default=["BMRN", "BIIB", "TECH", "EW", "MDT"],
    )
    under_ranked = normalized_ticker_list(
        diagnostic_config.get("under_ranked_tickers"),
        default=["EXEL", "PODD", "TARS", "UTHR", "TMDX", "COLL"],
    )
    shadow_by_ticker = {str(row.get("ticker") or "").upper(): row for row in shadow_rows}
    rows: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for group_name, tickers in [("potentially_over_ranked", over_ranked), ("potentially_under_ranked", under_ranked)]:
        for ticker in tickers:
            key = (group_name, ticker)
            if key in seen:
                continue
            seen.add(key)
            prod = production_rows_by_ticker.get(ticker, {})
            shadow = shadow_by_ticker.get(ticker, {})
            prod_score = to_float(prod.get("opportunity_score"), math.nan)
            shadow_score = to_float(shadow.get("shadow_score"), math.nan)
            score_gap = round(prod_score - shadow_score, 4) if math.isfinite(prod_score) and math.isfinite(shadow_score) else ""
            rows.append(
                {
                    "asof_date": asof_date,
                    "diagnostic_group": group_name,
                    "ticker": ticker,
                    "company_name": prod.get("company_name", shadow.get("company_name", "")),
                    "prod_rank": prod.get("rank", ""),
                    "prod_score": prod.get("opportunity_score", ""),
                    "prod_action_tier": prod.get("action_tier", ""),
                    "shadow_rank": shadow.get("rank", ""),
                    "shadow_score": shadow.get("shadow_score", ""),
                    "shadow_action_tier": shadow.get("action_tier", ""),
                    "score_gap_prod_minus_shadow": score_gap,
                    "commercial_value_score": prod.get("commercial_value_score", shadow.get("commercial_value_score", "")),
                    "forward_guidance_score": prod.get("forward_guidance_score", shadow.get("forward_guidance_score", "")),
                    "quality_adjusted_guidance_score": prod.get("quality_adjusted_guidance_score", shadow.get("quality_adjusted_guidance_score", "")),
                    "quality_adjusted_valuation_score": prod.get("quality_adjusted_valuation_score", shadow.get("quality_adjusted_valuation_score", "")),
                    "institutional_upside_capacity_score": prod.get("institutional_upside_capacity_score", shadow.get("institutional_upside_capacity_score", "")),
                    "mature_defensive_score": prod.get("mature_defensive_score", shadow.get("mature_defensive_score", "")),
                    "expected_return_quality_score": prod.get("expected_return_quality_score", shadow.get("expected_return_quality_score", "")),
                    "risk_score": prod.get("risk_score", shadow.get("risk_score_raw", shadow.get("risk_score", ""))),
                    "event_hard_reasons": prod.get("event_hard_reasons", shadow.get("event_hard_weakness_reasons", "")),
                    "rank_quality_cap": prod.get("rank_quality_cap", shadow.get("rank_quality_cap", "")),
                    "rank_quality_cap_reasons": prod.get("rank_quality_cap_reasons", shadow.get("rank_quality_cap_reasons", "")),
                    "rank_quality_cap_vetoed": prod.get("rank_quality_cap_vetoed", shadow.get("rank_quality_cap_vetoed", "")),
                    "production_policy_quality_penalty": prod.get("production_policy_quality_penalty", ""),
                    "production_policy_quality_bonus": prod.get("production_policy_quality_bonus", ""),
                }
            )
    return rows


def validate_top_score_fields(sample_row: dict[str, Any]) -> None:
    missing = [field for field in TOP_SCORE_FIELDS if field not in sample_row]
    if missing:
        raise RuntimeError(
            "TOP_SCORE_FIELDS contains field(s) not emitted by flatten_score_row(): "
            + ",".join(missing)
        )


def load_previous_scores(conn: sqlite3.Connection, prev_asof: str) -> dict[int, dict[str, Any]]:
    if not prev_asof:
        return {}
    rows = conn.execute(
        """
        SELECT company_id, opportunity_score, rank, bucket
        FROM daily_scores
        WHERE asof_date = ?
        """,
        (prev_asof,),
    ).fetchall()
    return {int(row["company_id"]): dict(row) for row in rows}


def build_alerts(
    *,
    current_scores: list[dict[str, Any]],
    previous_scores: dict[int, dict[str, Any]],
    prev_asof: str,
    score_change_min: float,
    rank_move_min: int,
    bucket_transition_enabled: bool,
    top_n: int,
) -> list[dict[str, Any]]:
    alerts: list[dict[str, Any]] = []
    strong_buckets = {"high_conviction", "watchlist"}
    if not previous_scores:
        for row in current_scores[:top_n]:
            alerts.append(
                {
                    "asof_date": row.get("asof_date", ""),
                    "ticker": row.get("ticker", ""),
                    "company_name": row.get("company_name", ""),
                    "alert_type": "initial_top_candidate",
                    "current_score": row.get("opportunity_score", ""),
                    "previous_score": "",
                    "score_change": "",
                    "current_rank": row.get("rank", ""),
                    "previous_rank": "",
                    "current_bucket": row.get("bucket", ""),
                    "previous_bucket": "",
                    "previous_asof_date": "",
                }
            )
        return alerts

    for row in current_scores:
        try:
            company_id = int(row.get("company_id") or 0)
        except (TypeError, ValueError):
            company_id = 0
        if company_id <= 0:
            LOGGER.warning("Skipping alert row without a valid company_id: ticker=%s", row.get("ticker", ""))
            continue
        prev = previous_scores.get(company_id)
        if not prev:
            if int(row.get("rank") or 999999) <= top_n:
                alert_type = "new_top_candidate"
            else:
                continue
            prev_score = ""
            score_change = ""
            prev_rank = ""
            prev_bucket = ""
        else:
            current_score = to_float(row.get("opportunity_score"))
            prev_score_value = to_float(prev.get("opportunity_score"))
            delta = current_score - prev_score_value
            current_bucket = str(row.get("bucket") or "")
            prev_bucket_value = str(prev.get("bucket") or "")
            if delta >= score_change_min:
                alert_type = "score_jump"
            elif bucket_transition_enabled and current_bucket in strong_buckets and current_bucket != prev_bucket_value:
                alert_type = "bucket_upgrade"
            elif (
                int(prev.get("rank") or 999999) - int(row.get("rank") or 999999) >= rank_move_min
                and int(row.get("rank") or 999999) <= top_n
            ):
                alert_type = "rank_improvement"
            elif int(row.get("rank") or 999999) <= top_n and int(prev.get("rank") or 999999) > top_n:
                alert_type = "entered_top_n"
            else:
                continue
            prev_score = round(prev_score_value, 4)
            score_change = round(delta, 4)
            prev_rank = prev.get("rank", "")
            prev_bucket = prev_bucket_value
        alerts.append(
            {
                "asof_date": row.get("asof_date", ""),
                "ticker": row.get("ticker", ""),
                "company_name": row.get("company_name", ""),
                "alert_type": alert_type,
                "current_score": row.get("opportunity_score", ""),
                "previous_score": prev_score,
                "score_change": score_change,
                "current_rank": row.get("rank", ""),
                "previous_rank": prev_rank,
                "current_bucket": row.get("bucket", ""),
                "previous_bucket": prev_bucket,
                "previous_asof_date": prev_asof,
            }
        )
    return alerts


def build_trial_validation_rows(
    *,
    scores: list[dict[str, Any]],
    evidence_rows: list[dict[str, str]],
    top_n: int,
    extra_tickers: list[str],
    max_trials_per_ticker: int,
    asof_date: str,
) -> list[dict[str, Any]]:
    score_by_ticker = {str(row.get("ticker") or "").upper(): row for row in scores}
    selected = {str(row.get("ticker") or "").upper() for row in scores[:top_n]}
    extra_selected = {str(ticker or "").strip().upper() for ticker in extra_tickers if str(ticker or "").strip()}
    missing_extra = sorted(ticker for ticker in extra_selected if ticker not in score_by_ticker)
    if missing_extra:
        LOGGER.warning(
            "Trial-validation extra ticker(s) not present in current scores for asof=%s: %s",
            asof_date,
            ",".join(missing_extra[:25]) + (f"...(+{len(missing_extra) - 25})" if len(missing_extra) > 25 else ""),
        )
    selected.update(extra_selected)
    grouped: dict[str, list[dict[str, str]]] = {}
    for row in evidence_rows:
        ticker = str(row.get("ticker") or "").upper()
        if ticker in selected:
            grouped.setdefault(ticker, []).append(row)

    out: list[dict[str, Any]] = []
    for ticker in sorted(grouped, key=lambda value: int(score_by_ticker.get(value, {}).get("rank") or 999999)):
        score_row = score_by_ticker.get(ticker, {})
        rows = grouped[ticker]
        rows.sort(
            key=lambda row: (
                1 if str(row.get("is_active_status") or "").lower() == "true" else 0,
                1 if str(row.get("qualifying_trial") or "").lower() == "true" else 0,
                1 if "lead" in str(row.get("match_roles") or "").split(";") else 0,
                to_float(row.get("phase_rank")),
                to_float(row.get("trial_score")),
                str(row.get("last_update_post_date") or ""),
            ),
            reverse=True,
        )
        for row in rows[:max(1, max_trials_per_ticker)]:
            out.append(
                {
                    "asof_date": score_row.get("asof_date") or row.get("asof_date") or asof_date,
                    "rank": score_row.get("rank", ""),
                    "ticker": ticker,
                    "company_name": score_row.get("company_name", row.get("company_name", "")),
                    "opportunity_score": score_row.get("opportunity_score", ""),
                    "nct_id": row.get("nct_id", ""),
                    "brief_title": row.get("brief_title", ""),
                    "overall_status": row.get("overall_status", ""),
                    "phase_text": row.get("phase_text", ""),
                    "phase_rank": row.get("phase_rank", ""),
                    "primary_purpose": row.get("primary_purpose", ""),
                    "match_roles": row.get("match_roles", ""),
                    "match_methods": row.get("match_methods", ""),
                    "strong_company_link": row.get("strong_company_link", ""),
                    "max_confidence": row.get("max_confidence", ""),
                    "is_active_status": row.get("is_active_status", ""),
                    "is_pivotal": row.get("is_pivotal", ""),
                    "qualifying_trial": row.get("qualifying_trial", ""),
                    "trial_score": row.get("trial_score", ""),
                    "days_since_last_update": row.get("days_since_last_update", ""),
                    "last_update_post_date": row.get("last_update_post_date", ""),
                    "primary_completion_date": row.get("primary_completion_date", ""),
                    "intervention_types": row.get("intervention_types", ""),
                    "intervention_names": row.get("intervention_names", ""),
                    "exclusion_reasons": row.get("exclusion_reasons", ""),
                    "outcome_override_applied": row.get("outcome_override_applied", ""),
                    "outcome_override_status": row.get("outcome_override_status", ""),
                    "outcome_override_reason": row.get("outcome_override_reason", ""),
                    "outcome_override_source_url": row.get("outcome_override_source_url", ""),
                    "outcome_override_manual_review": row.get("outcome_override_manual_review", ""),
                    "sponsors": row.get("sponsors", ""),
                }
            )
    return out


def bool_text(raw: object) -> bool:
    return str(raw or "").strip().lower() in {"1", "true", "yes", "y"}


def split_roles(raw: object) -> set[str]:
    return {part.strip().lower() for part in str(raw or "").split(";") if part.strip()}


def days_since_update(row: dict[str, Any]) -> int:
    return int(to_float(row.get("days_since_last_update"), 999999.0))


def is_terminal_status(row: dict[str, Any]) -> bool:
    return str(row.get("overall_status") or "").strip().upper() in {"COMPLETED", "TERMINATED", "WITHDRAWN", "SUSPENDED"}


def build_trial_validation_summary_rows(
    trial_rows: list[dict[str, Any]],
    validation_cap: int,
    *,
    stale_days: int,
) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in trial_rows:
        ticker = str(row.get("ticker") or "").strip().upper()
        if ticker:
            grouped.setdefault(ticker, []).append(row)

    out: list[dict[str, Any]] = []
    for ticker, rows in grouped.items():
        first = rows[0]
        active = [row for row in rows if bool_text(row.get("is_active_status"))]
        active_qualifying = [row for row in active if bool_text(row.get("qualifying_trial"))]
        active_phase2_3 = [row for row in active_qualifying if int(to_float(row.get("phase_rank"))) in {2, 3}]
        fresh_active_phase2_3 = [row for row in active_phase2_3 if days_since_update(row) <= stale_days]
        stale_active_phase2_3 = [row for row in active_phase2_3 if days_since_update(row) > stale_days]
        lead_active_qualifying = [row for row in active_qualifying if "lead" in split_roles(row.get("match_roles"))]
        lead_active_phase2_3 = [row for row in active_phase2_3 if "lead" in split_roles(row.get("match_roles"))]
        program_active_qualifying = [row for row in active_qualifying if "program" in split_roles(row.get("match_roles"))]
        program_active_phase2_3 = [row for row in active_phase2_3 if "program" in split_roles(row.get("match_roles"))]
        collab_only_active_qualifying = [
            row
            for row in active_qualifying
            if "collaborator" in split_roles(row.get("match_roles"))
            and "lead" not in split_roles(row.get("match_roles"))
            and "program" not in split_roles(row.get("match_roles"))
        ]
        collab_only_active_phase2_3 = [
            row
            for row in active_phase2_3
            if "collaborator" in split_roles(row.get("match_roles"))
            and "lead" not in split_roles(row.get("match_roles"))
            and "program" not in split_roles(row.get("match_roles"))
        ]
        pivotal_active_qualifying = [row for row in active_qualifying if bool_text(row.get("is_pivotal"))]
        lead_or_program_pivotal_active = [
            row
            for row in pivotal_active_qualifying
            if {"lead", "program"} & split_roles(row.get("match_roles"))
        ]
        weak_link_rows = [row for row in rows if not bool_text(row.get("strong_company_link"))]
        stale_active = [row for row in active if days_since_update(row) > stale_days]
        non_qualifying = [row for row in rows if not bool_text(row.get("qualifying_trial"))]
        terminal = [row for row in rows if is_terminal_status(row)]
        outcome_overridden = [row for row in rows if bool_text(row.get("outcome_override_applied"))]
        outcome_excluded = [
            row
            for row in outcome_overridden
            if any(
                part == "outcome_override" or part.startswith("outcome_override:")
                for part in str(row.get("exclusion_reasons") or "").split(";")
            )
        ]
        outcome_review = [row for row in outcome_overridden if bool_text(row.get("outcome_override_manual_review"))]

        review_flags: list[str] = []
        if outcome_excluded:
            review_flags.append("outcome_override_excluded")
        if outcome_review:
            review_flags.append("outcome_override_review")
        if weak_link_rows:
            review_flags.append("weak_links")
        if stale_active_phase2_3:
            review_flags.append("stale_active_phase2_3")
        if len(collab_only_active_phase2_3) > (len(lead_active_phase2_3) + len(program_active_phase2_3)):
            review_flags.append("collaborator_heavy_phase2_3")
        if non_qualifying:
            review_flags.append("non_qualifying_rows")
        if terminal:
            review_flags.append("terminal_rows")

        out.append(
            {
                "asof_date": first.get("asof_date", ""),
                "rank": first.get("rank", ""),
                "ticker": ticker,
                "company_name": first.get("company_name", ""),
                "opportunity_score": first.get("opportunity_score", ""),
                "rows_in_validation_csv": len(rows),
                "validation_cap_reached": len(rows) >= max(1, validation_cap),
                "needs_manual_review": bool(review_flags),
                "active_trials": len(active),
                "active_qualifying_trials": len(active_qualifying),
                "active_phase2_3_trials": len(active_phase2_3),
                "fresh_active_phase2_3_trials": len(fresh_active_phase2_3),
                "stale_active_phase2_3_trials": len(stale_active_phase2_3),
                "lead_active_qualifying_trials": len(lead_active_qualifying),
                "lead_active_phase2_3_trials": len(lead_active_phase2_3),
                "program_active_qualifying_trials": len(program_active_qualifying),
                "program_active_phase2_3_trials": len(program_active_phase2_3),
                "collab_only_active_qualifying_trials": len(collab_only_active_qualifying),
                "collab_only_active_phase2_3_trials": len(collab_only_active_phase2_3),
                "pivotal_active_qualifying_trials": len(pivotal_active_qualifying),
                "lead_or_program_pivotal_active_trials": len(lead_or_program_pivotal_active),
                "weak_link_rows": len(weak_link_rows),
                "stale_active_trials": len(stale_active),
                "non_qualifying_rows": len(non_qualifying),
                "terminal_rows": len(terminal),
                "outcome_override_rows": len(outcome_overridden),
                "outcome_override_excluded_rows": len(outcome_excluded),
                "outcome_override_review_rows": len(outcome_review),
                "review_flags": ";".join(review_flags),
            }
        )
    out.sort(key=lambda row: int(to_float(row.get("rank"), 999999.0)))
    return out


def build_evidence_cards(scores: list[dict[str, Any]], features: dict[int, dict[str, Any]], top_n: int) -> list[dict[str, Any]]:
    cards: list[dict[str, Any]] = []
    for row in scores[:top_n]:
        company_id = int(row["company_id"])
        evidence = parse_json_object(row.get("top_evidence_json"), context="top_evidence_json", ticker=row.get("ticker"))
        feature_payload = parse_json_object(
            features.get(company_id, {}).get("feature_json"),
            context="feature_json",
            ticker=row.get("ticker"),
        )
        cards.append(
            {
                "asof_date": row.get("asof_date", ""),
                "rank": row.get("rank", ""),
                "ticker": row.get("ticker", ""),
                "company_name": row.get("company_name", ""),
                "bucket": row.get("bucket", ""),
                "opportunity_score": row.get("opportunity_score", ""),
                "scores": {
                    "catalyst": row.get("catalyst_score", ""),
                    "credibility": row.get("credibility_score", ""),
                    "financial_quality": row.get("financial_quality_score", ""),
                    "risk": row.get("risk_score", ""),
                    "momentum": row.get("momentum_score", ""),
                },
                "top_evidence": evidence,
                "feature_detail": feature_payload,
            }
        )
    return cards


def main() -> None:
    configure_logging()
    args = parse_args()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    base_dir = config_path.parent
    db_path = args.db.expanduser().resolve() if args.db else resolve_path(cfg_get(config, "paths.database_path"), base_dir=base_dir)
    base_output_dir = resolve_path(cfg_get(config, "biotech_reports.output_dir", "../output/biotech_index_reports"), base_dir=base_dir)
    top_n = int(cfg_get(config, "biotech_reports.top_n", 20))
    score_change_min = float(cfg_get(config, "biotech_reports.alert_config.score_change_min", 12))
    rank_move_min = int(cfg_get(config, "biotech_reports.alert_config.rank_move_min", 5))
    bucket_transition_enabled = str(
        cfg_get(config, "biotech_reports.alert_config.bucket_transition_enabled", True)
    ).strip().lower() not in {"0", "false", "no", "off"}
    tier_settings = action_tier_settings(config)
    index_weights = cfg_get(config, "biotech_reports.index_weights", {}) or {}
    configured_ctgov_evidence_csv = resolve_path(cfg_get(config, "biotech_features.ctgov_evidence_csv", "../output/biotech_index_reports/ctgov_trial_evidence.csv"), base_dir=base_dir)
    trial_status_overrides_csv = resolve_optional_path(cfg_get(config, "ctgov_audit.trial_status_overrides_csv"), base_dir=base_dir)
    validation_extra_tickers = [str(x).upper() for x in (cfg_get(config, "biotech_reports.trial_validation_extra_tickers", []) or [])]
    validation_max_trials = int(cfg_get(config, "biotech_reports.trial_validation_max_trials_per_ticker", 25))
    trial_stale_days = int(cfg_get(config, "ctgov_audit.stale_days", 365))
    sqlite_timeout_sec = float(cfg_get(config, "runtime.sqlite_timeout_sec", 30.0))

    with connect(db_path, timeout_sec=sqlite_timeout_sec) as conn:
        run_id: int | None = None
        init_db(conn)
        asof_date = parse_date_text(args.asof) if args.asof else latest_score_date(conn)
        run_id = start_run(conn, run_type="publish_biotech_reports", input_path=db_path)
        try:
            output_dir = dated_output_dir(base_output_dir, asof_date)
            index_csv = output_dir / str(cfg_get(config, "biotech_reports.index_latest_csv", "biotech_index_latest.csv"))
            top_csv = output_dir / str(cfg_get(config, "biotech_reports.top_candidates_csv", "biotech_top_candidates.csv"))
            shadow_top_csv = output_dir / str(
                cfg_get(config, "biotech_reports.shadow_top_candidates_csv", "biotech_shadow_top20_candidates.csv")
            )
            ranking_diag_csv = output_dir / str(
                cfg_get(config, "biotech_reports.ranking_diagnostics_csv", "biotech_ranking_order_diagnostics.csv")
            )
            cohort_diag_csv = output_dir / str(
                cfg_get(config, "biotech_reports.cohort_diagnostics_csv", "biotech_cohort_diagnostics.csv")
            )
            alerts_csv = output_dir / str(cfg_get(config, "biotech_reports.alerts_csv", "biotech_alerts.csv"))
            evidence_json = output_dir / str(cfg_get(config, "biotech_reports.evidence_cards_json", "biotech_evidence_cards.json"))
            trial_validation_csv = output_dir / str(cfg_get(config, "biotech_reports.trial_validation_csv", "biotech_top_trial_validation.csv"))
            trial_validation_summary_csv = output_dir / str(
                cfg_get(config, "biotech_reports.trial_validation_summary_csv", "biotech_top_trial_validation_summary.csv")
            )
            ctgov_evidence_csv = resolve_report_input_csv(
                configured_ctgov_evidence_csv,
                base_output_dir=base_output_dir,
                asof_date=asof_date,
            )
            assert_output_paths_writable(
                [
                    index_csv,
                    top_csv,
                    shadow_top_csv,
                    ranking_diag_csv,
                    cohort_diag_csv,
                    alerts_csv,
                    evidence_json,
                    trial_validation_csv,
                    trial_validation_summary_csv,
                ]
            )
            scores = load_scores(conn, asof_date)
            if not scores:
                raise ValueError(f"No daily_scores rows found for asof_date={asof_date}")
            features = load_features(conn, asof_date)
            prev_asof = previous_score_date(conn, asof_date)
            previous = load_previous_scores(conn, prev_asof)

            summary = build_index_summary(scores, asof_date, top_n, weights=index_weights)
            flattened_rows = [
                apply_action_tier(flatten_score_row(row), tier_settings, score_field="opportunity_score")
                for row in scores
            ]
            top_rows = flattened_rows[:top_n]
            cohort_diagnostic_rows = build_cohort_diagnostics(flattened_rows, asof_date=asof_date)
            if top_rows:
                validate_top_score_fields(top_rows[0])
            shadow_top_rows = build_shadow_top_rows(conn, config, asof_date)
            production_rows_by_ticker = {
                str(row.get("ticker") or "").upper(): row for row in flattened_rows if str(row.get("ticker") or "").strip()
            }
            ranking_diagnostic_rows = build_ranking_order_diagnostics(
                config=config,
                asof_date=asof_date,
                production_rows_by_ticker=production_rows_by_ticker,
                shadow_rows=shadow_top_rows,
            )
            alerts = build_alerts(
                current_scores=scores,
                previous_scores=previous,
                prev_asof=prev_asof,
                score_change_min=score_change_min,
                rank_move_min=rank_move_min,
                bucket_transition_enabled=bucket_transition_enabled,
                top_n=top_n,
            )
            cards = build_evidence_cards(scores, features, top_n)
            trial_validation_rows = build_trial_validation_rows(
                scores=scores,
                evidence_rows=apply_trial_status_overrides(
                    read_csv_rows(ctgov_evidence_csv, required=True),
                    read_csv_rows(trial_status_overrides_csv) if trial_status_overrides_csv else [],
                ),
                top_n=top_n,
                extra_tickers=validation_extra_tickers,
                max_trials_per_ticker=validation_max_trials,
                asof_date=asof_date,
            )
            trial_validation_summary_rows = build_trial_validation_summary_rows(
                trial_validation_rows,
                validation_max_trials,
                stale_days=trial_stale_days,
            )

            write_csv(index_csv, [summary], list(summary.keys()))
            write_csv(top_csv, top_rows, TOP_SCORE_FIELDS)
            write_csv(shadow_top_csv, shadow_top_rows, SHADOW_SCORE_FIELDS)
            write_csv(ranking_diag_csv, ranking_diagnostic_rows, RANKING_DIAGNOSTIC_FIELDS)
            write_csv(cohort_diag_csv, cohort_diagnostic_rows, COHORT_DIAGNOSTIC_FIELDS)
            write_csv(
                alerts_csv,
                alerts,
                [
                    "asof_date",
                    "ticker",
                    "company_name",
                    "alert_type",
                    "current_score",
                    "previous_score",
                    "score_change",
                    "current_rank",
                    "previous_rank",
                    "current_bucket",
                    "previous_bucket",
                    "previous_asof_date",
                ],
            )
            write_json(evidence_json, cards)
            write_csv(
                trial_validation_csv,
                trial_validation_rows,
                [
                    "asof_date", "rank", "ticker", "company_name", "opportunity_score", "nct_id", "brief_title",
                    "overall_status", "phase_text", "phase_rank", "primary_purpose", "match_roles",
                    "match_methods", "strong_company_link", "max_confidence", "is_active_status",
                    "is_pivotal", "qualifying_trial", "trial_score", "days_since_last_update",
                    "last_update_post_date", "primary_completion_date", "intervention_types",
                    "intervention_names", "exclusion_reasons", "outcome_override_applied",
                    "outcome_override_status", "outcome_override_reason", "outcome_override_source_url",
                    "outcome_override_manual_review", "sponsors",
                ],
            )
            write_csv(
                trial_validation_summary_csv,
                trial_validation_summary_rows,
                [
                    "asof_date", "rank", "ticker", "company_name", "opportunity_score",
                    "rows_in_validation_csv", "validation_cap_reached", "needs_manual_review",
                    "active_trials", "active_qualifying_trials",
                    "active_phase2_3_trials", "fresh_active_phase2_3_trials",
                    "stale_active_phase2_3_trials", "lead_active_qualifying_trials",
                    "lead_active_phase2_3_trials", "program_active_qualifying_trials",
                    "program_active_phase2_3_trials", "collab_only_active_qualifying_trials",
                    "collab_only_active_phase2_3_trials", "pivotal_active_qualifying_trials",
                    "lead_or_program_pivotal_active_trials", "weak_link_rows", "stale_active_trials",
                    "non_qualifying_rows", "terminal_rows", "outcome_override_rows",
                    "outcome_override_excluded_rows", "outcome_override_review_rows", "review_flags",
                ],
            )
            LOGGER.info(
                "Published biotech reports: rows=%d top_rows=%d shadow_rows=%d ranking_diag_rows=%d cohort_diag_rows=%d output_dir=%s",
                len(scores),
                len(top_rows),
                len(shadow_top_rows),
                len(ranking_diagnostic_rows),
                len(cohort_diagnostic_rows),
                output_dir,
            )
            finish_run(
                conn,
                run_id=run_id,
                status="success",
                row_count=len(scores),
                message=(
                    f"asof={asof_date} top_n={top_n} shadow_top_n={len(shadow_top_rows)} "
                    f"ranking_diag={len(ranking_diagnostic_rows)} alerts={len(alerts)} output_dir={output_dir}"
                ),
            )
        except BaseException as exc:
            if run_id is not None and not (isinstance(exc, SystemExit) and exc.code in (0, None)):
                finish_run(conn, run_id=run_id, status="failed", row_count=0, message=f"{type(exc).__name__}: {exc}")
            raise


if __name__ == "__main__":
    main()



