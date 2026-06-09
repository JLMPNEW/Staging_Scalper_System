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
from biotech_index.core.constants import (  # noqa: E402
    CORE_HARD_WEAKNESS_REASONS,
    EVENT_HARD_WEAKNESS_REASONS,
    GOING_CONCERN_HARD_STATUSES,
    GOING_CONCERN_SOFT_STATUSES,
    SOFT_WEAKNESS_REASONS,
)
from biotech_index.core.commercial_risk import commercial_risk_overlay_fields  # noqa: E402
from biotech_index.core.biotech_taxonomy import (  # noqa: E402
    apply_manual_taxonomy_override,
    classify_biotech_cohort,
)
from biotech_index.core.db import (  # noqa: E402
    DAILY_SCORES_OPTIONAL_COLUMNS,
    connect,
    ensure_table_optional_columns,
    finish_run,
    init_db,
    start_run,
    utc_now,
)
from biotech_index.core.logging_utils import configure_utc_logging  # noqa: E402
from biotech_index.core.pipeline_guards import (  # noqa: E402
    read_final_scoring_tickers,
    validate_full_universe_coverage,
    validate_layer_freshness,
)
from biotech_index.core.scoring_math import (  # noqa: E402
    convex_risk_drag as shared_convex_risk_drag,
    normalize_growth_drag_curve,
    normalize_risk_penalty_mode,
    risk_score_from_components,
    score_commercial_expected_return_overlay,
    score_growth_drag as shared_growth_drag_score,
)


LOGGER = logging.getLogger("score_biotech_index")
DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
CORE_STRUCTURAL_VETO_DEFAULT_REASONS = sorted(CORE_HARD_WEAKNESS_REASONS)
EVENT_HARD_WEAKNESS_DEFAULT_REASONS = sorted(EVENT_HARD_WEAKNESS_REASONS)
SOFT_WEAKNESS_DEFAULT_REASONS = sorted(SOFT_WEAKNESS_REASONS)
EXPECTED_COMMERCIAL_RISK_FIELDS = frozenset(
    {
        "commercial_deterioration_score",
        "commercial_deterioration_flag",
        "commercial_deterioration_reasons",
        "valuation_growth_mismatch_score",
        "valuation_growth_mismatch_flag",
        "valuation_growth_mismatch_reasons",
        "transient_revenue_anchor_score",
        "transient_revenue_anchor_flag",
        "transient_revenue_anchor_reasons",
        "commercial_business_shock_score",
        "commercial_business_shock_flag",
        "commercial_business_shock_reasons",
        "commercial_risk_overlay_score",
        "commercial_risk_overlay_flag",
        "commercial_risk_overlay_reasons",
        "commercial_risk_sub_scores",
    }
)


@dataclass(frozen=True)
class TaxonomyOverride:
    ticker: str
    effective_start_date: date | None
    effective_end_date: date | None
    primary_cohort: str
    secondary_cohort: str
    confidence: float | None
    overlays_add: list[str]
    reason_codes_add: list[str]
    source: str
    note: str

    def applies_to(self, *, asof_date: date) -> bool:
        if self.effective_start_date is not None and asof_date < self.effective_start_date:
            return False
        if self.effective_end_date is not None and asof_date > self.effective_end_date:
            return False
        return True


@dataclass(frozen=True)
class CohortPolicy:
    enabled: bool
    min_cohort_specific_company_count: int
    sparse_valid_calibration_mode: str
    non_investible_cohorts: set[str]
    calibration_excluded_cohorts: set[str]

PRODUCTION_SELECTION_POLICIES = frozenset(
    {
        "raw_legacy_score",
        "hard_weakness_veto",
        "hard_veto_soft_drag",
        "investable_core_risk_cap",
        "core_structural_veto",
        "core_veto_event_drag",
        "core_veto_event_soft_drag",
        "core_veto_event_drag_quality_guardrail",
        "core_veto_event_soft_drag_quality_guardrail",
        "core_veto_event_drag_business_shock_strict",
        "core_veto_event_drag_expected_return_tilt",
        "core_veto_event_drag_mature_defensive_guard",
    }
)
SOFT_DRAG_SELECTION_POLICIES = frozenset(
    {
        "core_veto_event_soft_drag",
        "core_veto_event_soft_drag_quality_guardrail",
    }
)
QUALITY_GUARDRAIL_SELECTION_POLICIES = frozenset(
    {
        "core_veto_event_drag_quality_guardrail",
        "core_veto_event_soft_drag_quality_guardrail",
        "core_veto_event_drag_business_shock_strict",
        "core_veto_event_drag_expected_return_tilt",
        "core_veto_event_drag_mature_defensive_guard",
    }
)


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
    min_watch_runway: float
    terminal_runway: float
    commercial_stage_revenue_min: float
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
    if parsed is None:
        raise ValueError(f"Invalid as_of date for output folder: {asof_date!r}")
    return parsed.strftime("%Y%m%d")


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


def parse_json(raw: object, *, context: str = "") -> dict[str, Any]:
    try:
        payload = json.loads(str(raw or "{}"))
    except json.JSONDecodeError as exc:
        LOGGER.warning("Malformed JSON payload skipped%s: %s", f" ({context})" if context else "", exc)
        return {}
    return payload if isinstance(payload, dict) else {}


def clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    if not math.isfinite(value):
        return low
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
        parts = [str(item).strip() for item in raw]
    else:
        parts = [str(raw)]
    values = [part.strip() for part in parts if part.strip()]
    return values or list(default or [])


def load_taxonomy_overrides(path: Path | None) -> dict[str, list[TaxonomyOverride]]:
    if path is None or not path.exists():
        return {}
    overrides: dict[str, list[TaxonomyOverride]] = {}
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None:
            raise ValueError(f"Taxonomy overrides CSV has no header: {path}")
        for line_no, row in enumerate(reader, start=2):
            ticker = str(row.get("ticker") or "").strip().upper()
            if not ticker:
                raise ValueError(f"Taxonomy override row {line_no} missing ticker: {path}")
            primary_cohort = str(row.get("primary_cohort") or "").strip()
            if not primary_cohort:
                raise ValueError(f"Taxonomy override row {line_no} missing primary_cohort for {ticker}: {path}")
            confidence_text = str(row.get("confidence") or "").strip()
            confidence_value = to_float(confidence_text, math.nan) if confidence_text else math.nan
            overrides.setdefault(ticker, []).append(
                TaxonomyOverride(
                    ticker=ticker,
                    effective_start_date=parse_date(row.get("effective_start_date")),
                    effective_end_date=parse_date(row.get("effective_end_date")),
                    primary_cohort=primary_cohort,
                    secondary_cohort=str(row.get("secondary_cohort") or "").strip(),
                    confidence=confidence_value if math.isfinite(confidence_value) else None,
                    overlays_add=parse_string_list(row.get("overlays_add")),
                    reason_codes_add=parse_string_list(row.get("reason_codes_add")),
                    source=str(row.get("source") or "manual_taxonomy_override").strip() or "manual_taxonomy_override",
                    note=str(row.get("note") or "").strip(),
                )
            )
    return overrides


def load_calibration_cohort_overrides(path: Path | None) -> dict[str, dict[str, str]]:
    if path is None:
        return {}
    if not path.exists():
        raise FileNotFoundError(f"Calibration cohort CSV not found: {path}")
    overrides: dict[str, dict[str, str]] = {}
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None:
            raise ValueError(f"Calibration cohort CSV has no header: {path}")
        fields = {str(field or "").strip() for field in reader.fieldnames}
        cohort_field = (
            "biotech_calibration_cohort"
            if "biotech_calibration_cohort" in fields
            else "official_cohort"
            if "official_cohort" in fields
            else "biotech_primary_cohort"
            if "biotech_primary_cohort" in fields
            else ""
        )
        if "ticker" not in fields or not cohort_field:
            raise ValueError(
                "Calibration cohort CSV must include ticker plus one of "
                f"biotech_calibration_cohort, official_cohort, or biotech_primary_cohort: {path}"
            )
        for line_no, row in enumerate(reader, start=2):
            ticker = str(row.get("ticker") or "").strip().upper()
            cohort = str(row.get(cohort_field) or "").strip()
            if not ticker:
                raise ValueError(f"Calibration cohort row {line_no} missing ticker: {path}")
            if not cohort:
                raise ValueError(f"Calibration cohort row {line_no} missing official cohort for {ticker}: {path}")
            if ticker in overrides:
                previous = overrides[ticker]["biotech_calibration_cohort"]
                raise ValueError(
                    f"Duplicate calibration cohort assignment for {ticker}: {previous!r} and {cohort!r} in {path}"
                )
            overrides[ticker] = {
                "biotech_calibration_cohort": cohort,
                "biotech_calibration_cohort_source": str(
                    row.get("source") or "manual_calibration_cohort_csv"
                ).strip()
                or "manual_calibration_cohort_csv",
                "biotech_calibration_cohort_reason": str(row.get("reason") or "").strip(),
            }
    return overrides


def calibration_cohort_fields(
    *,
    ticker: str,
    primary_cohort: str,
    calibration_cohorts_by_ticker: dict[str, dict[str, str]],
    enabled: bool,
    fallback_to_primary: bool,
    version: str,
) -> dict[str, str]:
    clean_ticker = str(ticker or "").strip().upper()
    clean_primary = str(primary_cohort or "unmapped_calibration_cohort").strip() or "unmapped_calibration_cohort"
    if enabled and clean_ticker in calibration_cohorts_by_ticker:
        row = calibration_cohorts_by_ticker[clean_ticker]
        return {
            "biotech_calibration_cohort": row["biotech_calibration_cohort"],
            "biotech_calibration_cohort_source": row["biotech_calibration_cohort_source"],
            "biotech_calibration_cohort_reason": row["biotech_calibration_cohort_reason"],
            "biotech_calibration_cohort_version": version,
        }
    if fallback_to_primary:
        return {
            "biotech_calibration_cohort": clean_primary,
            "biotech_calibration_cohort_source": "primary_cohort_fallback",
            "biotech_calibration_cohort_reason": f"no calibration override; primary_cohort={clean_primary}",
            "biotech_calibration_cohort_version": version,
        }
    return {
        "biotech_calibration_cohort": "unmapped_calibration_cohort",
        "biotech_calibration_cohort_source": "unmapped",
        "biotech_calibration_cohort_reason": f"no calibration override; primary_cohort={clean_primary}",
        "biotech_calibration_cohort_version": version,
    }


def row_calibration_cohort(row: dict[str, Any]) -> str:
    return (
        str(row.get("biotech_calibration_cohort") or row.get("biotech_primary_cohort") or "unmapped_calibration_cohort").strip()
        or "unmapped_calibration_cohort"
    )


def apply_official_operational_cohort_payload(
    *,
    ticker: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    cohort = (
        str(payload.get("biotech_calibration_cohort") or payload.get("biotech_primary_cohort") or "").strip()
        or "unmapped_calibration_cohort"
    )
    source = str(payload.get("biotech_calibration_cohort_source") or "manual_calibration_cohort_csv").strip()
    reason = str(payload.get("biotech_calibration_cohort_reason") or "").strip()
    version = str(payload.get("biotech_calibration_cohort_version") or "calibration_cohort_unversioned").strip()
    out = dict(payload)
    out["biotech_primary_cohort"] = cohort
    out["biotech_secondary_cohort"] = ""
    out["biotech_calibration_cohort"] = cohort
    out["biotech_cohort_reason_codes"] = "official_five_bucket_operational_cohort"
    out["biotech_cohort_confidence"] = 100.0 if cohort != "unmapped_calibration_cohort" else 0.0
    out["biotech_cohort_margin"] = 100.0 if cohort != "unmapped_calibration_cohort" else 0.0
    out["biotech_cohort_source"] = source
    out["biotech_cohort_overlays"] = ""
    out["biotech_cohort_data_quality"] = "high" if cohort != "unmapped_calibration_cohort" else "review"
    out["biotech_taxonomy_review_required"] = 0.0 if cohort != "unmapped_calibration_cohort" else 1.0
    out["biotech_cohort_calibration_weight"] = 1.0 if cohort != "unmapped_calibration_cohort" else 0.0
    out["biotech_cohort_model_version"] = version
    out["biotech_cohort_evidence_json"] = json.dumps(
        {
            "ticker": str(ticker or "").strip().upper(),
            "official_cohort": cohort,
            "source": source,
            "reason": reason,
            "version": version,
        },
        ensure_ascii=True,
        sort_keys=True,
    )
    return out


def public_biotech_taxonomy_payload(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in payload.items()
        if not key.startswith("biotech_calibration_") and key != "biotech_secondary_cohort"
    }


def cohort_policy_settings(config: dict[str, Any]) -> CohortPolicy:
    settings = cfg_get(config, "biotech_scoring.cohort_policy", {}) or {}
    if not isinstance(settings, dict):
        settings = {}
    default_excluded = ["unmapped_calibration_cohort"]
    non_investible = {
        str(item).strip()
        for item in parse_string_list(settings.get("non_investible_cohorts"), default=default_excluded)
        if str(item).strip()
    }
    calibration_excluded = {
        str(item).strip()
        for item in parse_string_list(settings.get("calibration_excluded_cohorts"), default=sorted(non_investible))
        if str(item).strip()
    }
    min_size = int(float(settings.get("min_cohort_specific_company_count", 8)))
    if min_size < 1:
        raise ValueError("biotech_scoring.cohort_policy.min_cohort_specific_company_count must be >= 1")
    return CohortPolicy(
        enabled=as_bool(settings.get("enabled", True), True),
        min_cohort_specific_company_count=min_size,
        sparse_valid_calibration_mode=str(settings.get("sparse_valid_calibration_mode") or "global_fallback").strip()
        or "global_fallback",
        non_investible_cohorts=non_investible,
        calibration_excluded_cohorts=calibration_excluded,
    )


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


def count_value(raw: object) -> int:
    value = to_float(raw, 0.0)
    return max(0, int(round(value)))


def optional_score(raw_scores: dict[str, Any], row: dict[str, Any], key: str) -> float | None:
    for raw in (raw_scores.get(key), row.get(key)):
        value = to_float(raw, math.nan)
        if math.isfinite(value):
            return value
    return None


def convex_risk_drag(risk: float, weight: float, config: dict[str, Any], section: str) -> float:
    return shared_convex_risk_drag(
        risk,
        weight,
        enabled=as_bool(cfg_get(config, f"{section}.convex_risk_penalty_enabled", True), True),
        convexity=float(cfg_get(config, f"{section}.risk_penalty_convexity", 0.35)),
        inflection=float(cfg_get(config, f"{section}.risk_penalty_inflection", 50.0)),
    )


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
        "commercial_stage_revenue_min": float(cfg_get(config, "commercial_value.commercial_stage_revenue_min", 50_000_000)),
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
        min_watch_runway=float(cfg_get(config, "biotech_scoring.buckets.watchlist_min_runway_months", 6)),
        terminal_runway=float(cfg_get(config, "biotech_scoring.buckets.terminal_runway_months", 3)),
        commercial_stage_revenue_min=float(cfg_get(config, "commercial_value.commercial_stage_revenue_min", 50_000_000)),
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
            cfg_get(config, "biotech_scoring.production_baseline.selection_policy", "core_veto_event_drag")
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
    def policy_float(key: str, default: object, *, min_value: float = 0.0, max_value: float = 100.0) -> float:
        value = float(cfg_get(config, f"biotech_scoring.production_policy.{key}", default))
        if not min_value <= value <= max_value:
            raise ValueError(
                f"biotech_scoring.production_policy.{key} must be between "
                f"{min_value:g} and {max_value:g}, got {value:g}"
            )
        return value

    event_hard_penalty_value = policy_float("event_hard_penalty", 10.0)
    soft_weakness_penalty_value = policy_float("soft_weakness_penalty", 8.0)
    return {
        "event_hard_penalty": event_hard_penalty_value,
        "soft_weakness_penalty": soft_weakness_penalty_value,
        "value_trap_penalty": policy_float("value_trap_penalty", 0.0),
        "leverage_fragility_penalty": policy_float("leverage_fragility_penalty", 0.0),
        "guidance_staleness_penalty": policy_float("guidance_staleness_penalty", 0.0),
        "mature_defensive_penalty": policy_float("mature_defensive_penalty", 0.0),
        "expected_return_quality_bonus": policy_float("expected_return_quality_bonus", 0.0),
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
        "commercial_fragility_threshold": policy_float("commercial_fragility_threshold", 70.0),
        "high_risk_threshold": policy_float("high_risk_threshold", 75.0),
        "commercial_stage_revenue_min": float(cfg_get(config, "commercial_value.commercial_stage_revenue_min", 50_000_000)),
    }


def finite_float(raw: object) -> float | None:
    value = to_float(raw, math.nan)
    return value if math.isfinite(value) else None


def configured_growth_drag_curve(config: dict[str, Any]) -> str:
    return normalize_growth_drag_curve(
        cfg_get(
            config,
            "biotech_scoring.growth_drag_curve",
            cfg_get(config, "calibration.tier1.growth_drag_curve", "legacy"),
        )
    )


def growth_drag_score(*growth_values: object, curve: str = "legacy") -> float:
    return shared_growth_drag_score(*growth_values, curve=curve)


def market_cap_maturity_score(market_cap: object) -> float:
    cap = finite_float(market_cap)
    if cap is None or cap <= 0.0:
        return 0.0
    if cap >= 100_000_000_000:
        return 100.0
    if cap >= 50_000_000_000:
        return 75.0
    if cap >= 25_000_000_000:
        return 55.0
    if cap >= 10_000_000_000:
        return 30.0
    return 0.0


def mature_defensive_score(
    commercial: dict[str, Any],
    forward_guidance: dict[str, Any],
    *,
    growth_drag_curve: str = "legacy",
) -> float:
    if to_float(commercial.get("commercial_stage_flag"), 0.0) <= 0.0:
        return 0.0
    size_score = market_cap_maturity_score(commercial.get("market_cap"))
    growth_drag = growth_drag_score(
        forward_guidance.get("forward_revenue_growth_pct"),
        commercial.get("revenue_yoy_growth_pct"),
        curve=growth_drag_curve,
    )
    upside_drag = 100.0 - clamp(to_float(commercial.get("institutional_upside_capacity_score"), 50.0))
    score = clamp(0.40 * size_score + 0.35 * growth_drag + 0.25 * upside_drag)
    if growth_drag <= 25.0:
        score *= 0.45
    return clamp(score)


def expected_return_quality_score(
    *,
    commercial: dict[str, Any],
    forward_guidance: dict[str, Any],
    momentum: float,
    risk: float,
    mature_defensive: float,
) -> float:
    value_trap = clamp(to_float(commercial.get("value_trap_score"), 0.0))
    score = (
        0.24
        * clamp(
            to_float(
                forward_guidance.get("quality_adjusted_guidance_score"),
                to_float(forward_guidance.get("guidance_score"), 35.0),
            )
        )
        + 0.20 * clamp(to_float(commercial.get("institutional_upside_capacity_score"), 50.0))
        + 0.16 * clamp(to_float(commercial.get("commercial_value_score"), 35.0))
        + 0.14 * clamp(momentum)
        + 0.12
        * clamp(
            to_float(
                commercial.get("quality_adjusted_valuation_score"),
                to_float(commercial.get("valuation_score"), 50.0),
            )
        )
        + 0.14 * (100.0 - clamp(risk))
        - 0.08 * value_trap
        - 0.05 * mature_defensive
    )
    return clamp(score)


def risk_decomposition_from_raw_scores(raw_scores: dict[str, Any], fallback_risk: float) -> dict[str, Any]:
    components = raw_scores.get("risk_component_scores", {}) if isinstance(raw_scores, dict) else {}
    structural_components = components.get("structural", {}) if isinstance(components, dict) else {}
    compensated_components = components.get("compensated", {}) if isinstance(components, dict) else {}
    if not isinstance(structural_components, dict):
        structural_components = {}
    if not isinstance(compensated_components, dict):
        compensated_components = {}
    legacy = clamp(to_float(raw_scores.get("legacy_risk_score_raw"), fallback_risk))
    uncompensated = clamp(to_float(raw_scores.get("uncompensated_risk_score_raw"), legacy))
    compensated = clamp(to_float(raw_scores.get("compensated_risk_score_raw"), 50.0))
    penalty_input = clamp(to_float(raw_scores.get("risk_penalty_input_score_raw"), legacy))
    predictive_penalty_input = clamp(to_float(raw_scores.get("predictive_risk_penalty_input_score_raw"), penalty_input))

    def component_score(group: dict[str, Any], key: str) -> float | str:
        value = to_float(group.get(key), math.nan)
        return finite_value_or_blank(value)

    return {
        "legacy_risk_score": legacy,
        "risk_penalty_input_score": penalty_input,
        "predictive_risk_penalty_input_score": predictive_penalty_input,
        "uncompensated_risk_score": uncompensated,
        "compensated_risk_score": compensated,
        "liquidity_risk_score": component_score(structural_components, "liquidity"),
        "financing_survival_risk_score": component_score(structural_components, "financing_survival"),
        "governance_filing_risk_score": component_score(structural_components, "governance_filing"),
        "regulatory_setback_risk_score": component_score(structural_components, "regulatory_setback"),
        "pipeline_anchor_risk_score": component_score(structural_components, "pipeline_anchor"),
        "collaborator_dependency_risk_score": component_score(compensated_components, "collaborator_dependency"),
        "trial_staleness_risk_score": component_score(compensated_components, "trial_staleness"),
        "risk_component_json": json.dumps(components if isinstance(components, dict) else {}, ensure_ascii=True, sort_keys=True),
        "risk_component_scores": components if isinstance(components, dict) else {},
    }


def configured_risk_penalty_score(
    *,
    config: dict[str, Any],
    risk_components: dict[str, Any],
    legacy_risk: float,
    purpose: str = "allocation",
    primary_cohort: str = "",
) -> tuple[float, str]:
    clean_purpose = str(purpose or "allocation").strip().lower()
    if clean_purpose not in {"allocation", "discovery"}:
        raise ValueError(f"Unsupported risk penalty purpose: {purpose!r}")
    routing = cfg_get(config, "biotech_scoring.risk_mode_routing", {}) or {}
    if isinstance(routing, dict) and as_bool(routing.get("enabled", False), False):
        mode = normalize_risk_penalty_mode(routing.get(f"default_{clean_purpose}_mode", "legacy"))
        cohort_modes = routing.get("cohort_modes", {})
        if isinstance(cohort_modes, dict) and primary_cohort:
            cohort_cfg = cohort_modes.get(primary_cohort, {})
            if isinstance(cohort_cfg, dict):
                mode = normalize_risk_penalty_mode(cohort_cfg.get(f"{clean_purpose}_mode", mode), default=mode)
        return risk_score_from_components(risk_components, legacy_risk=legacy_risk, mode=mode), mode

    risk_cfg = cfg_get(config, "biotech_scoring.risk_decomposition", {}) or {}
    if not isinstance(risk_cfg, dict):
        risk_cfg = {}
    if not as_bool(risk_cfg.get("use_for_penalty", risk_cfg.get("enabled", False)), False):
        return legacy_risk, "legacy"
    mode = normalize_risk_penalty_mode(risk_cfg.get("risk_penalty_mode", "legacy"))
    return risk_score_from_components(risk_components, legacy_risk=legacy_risk, mode=mode), mode


def production_rank_score_field(config: dict[str, Any]) -> str:
    """Return the score field used for production rank ordering."""
    allow_discovery_rank = as_bool(
        cfg_get(config, "biotech_scoring.risk_mode_routing.allow_discovery_as_production_rank", False),
        False,
    )
    raw = str(
        cfg_get(
            config,
            "biotech_scoring.risk_mode_routing.production_score_source",
            "opportunity_score",
        )
        or "opportunity_score"
    ).strip().lower()
    aliases = {
        "legacy": "opportunity_score",
        "legacy_allocation": "opportunity_score",
        "allocation": "opportunity_score",
        "opportunity": "opportunity_score",
        "opportunity_score": "opportunity_score",
    }
    if allow_discovery_rank:
        aliases |= {
            "routed_discovery": "discovery_opportunity_score",
            "discovery": "discovery_opportunity_score",
            "discovery_score": "discovery_opportunity_score",
            "discovery_opportunity_score": "discovery_opportunity_score",
        }
    field = aliases.get(raw)
    if field is None:
        expected = "opportunity_score"
        if allow_discovery_rank:
            expected += " or discovery_opportunity_score"
        raise ValueError(
            "Unsupported biotech_scoring.risk_mode_routing.production_score_source="
            f"{raw!r}; expected {expected}"
        )
    return field


def production_rank_blocked(row: dict[str, Any], *, apply_core_veto_to_rank: bool) -> bool:
    """True when a row is not eligible for production capital-allocation rank."""
    allocation_bucket = str(row.get("allocation_bucket") or row.get("bucket") or "").strip().lower()
    return (
        to_float(row.get("biotech_cohort_investible_flag"), 1.0) <= 0.0
        or allocation_bucket == "avoid"
        or to_float(row.get("rank_quality_cap_vetoed"), 0.0) > 0.0
        or (apply_core_veto_to_rank and to_float(row.get("core_structural_veto_flag"), 0.0) > 0.0)
    )


def commercial_risk_overlay_settings(config: dict[str, Any]) -> dict[str, Any]:
    settings = dict(cfg_get(config, "biotech_scoring.commercial_risk_overlay", {}) or {})
    production_fragility_threshold = float(
        cfg_get(config, "biotech_scoring.production_policy.commercial_fragility_threshold", 70.0)
    )
    settings.setdefault(
        "commercial_stage_revenue_min",
        float(cfg_get(config, "commercial_value.commercial_stage_revenue_min", 50_000_000.0)),
    )
    settings.setdefault("commercial_fragility_threshold", production_fragility_threshold)
    overlay_fragility_threshold = to_float(
        settings.get("commercial_fragility_threshold"),
        production_fragility_threshold,
    )
    if abs(overlay_fragility_threshold - production_fragility_threshold) > 1e-9:
        LOGGER.warning(
            "commercial_risk_overlay.commercial_fragility_threshold %.4f differs from "
            "production_policy.commercial_fragility_threshold %.4f",
            overlay_fragility_threshold,
            production_fragility_threshold,
        )
    return settings


def commercial_risk_policy_penalty(fields: dict[str, Any], settings: dict[str, Any]) -> float:
    if not as_bool(settings.get("enabled", True), True):
        return 0.0
    component_penalties = [
        to_float(settings.get("commercial_deterioration_penalty"), 0.0) or 0.0,
        to_float(settings.get("valuation_growth_mismatch_penalty"), 0.0) or 0.0,
        to_float(settings.get("transient_revenue_anchor_penalty"), 0.0) or 0.0,
        to_float(settings.get("commercial_business_shock_penalty"), 0.0) or 0.0,
    ]
    composite_penalty = to_float(settings.get("commercial_risk_overlay_penalty"), 0.0) or 0.0
    if composite_penalty > 0.0 and any(value > 0.0 for value in component_penalties):
        raise ValueError(
            "Commercial risk overlay cannot use both sub-component penalties and "
            "commercial_risk_overlay_penalty; calibrate one penalty architecture at a time."
        )
    penalty = 0.0
    for field, setting in [
        ("commercial_deterioration_score", "commercial_deterioration_penalty"),
        ("valuation_growth_mismatch_score", "valuation_growth_mismatch_penalty"),
        ("transient_revenue_anchor_score", "transient_revenue_anchor_penalty"),
        ("commercial_business_shock_score", "commercial_business_shock_penalty"),
        ("commercial_risk_overlay_score", "commercial_risk_overlay_penalty"),
    ]:
        score = to_float(fields.get(field), 0.0) or 0.0
        max_penalty = to_float(settings.get(setting), 0.0) or 0.0
        penalty += max(0.0, max_penalty) * max(0.0, min(100.0, score)) / 100.0
    max_total = to_float(settings.get("max_total_penalty"), 25.0)
    return min(penalty, max_total) if max_total is not None and max_total > 0.0 else penalty


def rank_quality_cap_settings(config: dict[str, Any]) -> dict[str, Any]:
    raw = cfg_get(config, "biotech_scoring.rank_quality_caps", {}) or {}
    return {
        "enabled": as_bool(raw.get("enabled", True), True),
        "business_shock_min_score": float(raw.get("business_shock_min_score", 70.0)),
        "business_shock_cap": float(raw.get("business_shock_cap", 48.0)),
        "severe_deterioration_min_score": float(raw.get("severe_deterioration_min_score", 70.0)),
        "severe_deterioration_revenue_yoy_max": float(raw.get("severe_deterioration_revenue_yoy_max", -0.20)),
        "severe_deterioration_cap": float(raw.get("severe_deterioration_cap", 50.0)),
        "no_guidance_negative_growth_cap": float(raw.get("no_guidance_negative_growth_cap", 52.0)),
        "valuation_mismatch_min_score": float(raw.get("valuation_mismatch_min_score", 70.0)),
        "unprofitable_value_mismatch_cap": float(raw.get("unprofitable_value_mismatch_cap", 50.0)),
        "cheap_low_growth_valuation_min_score": float(raw.get("cheap_low_growth_valuation_min_score", 90.0)),
        "cheap_low_growth_revenue_yoy_max": float(raw.get("cheap_low_growth_revenue_yoy_max", 0.10)),
        "cheap_low_growth_guidance_max_score": float(raw.get("cheap_low_growth_guidance_max_score", 60.0)),
        "cheap_low_growth_cap": float(raw.get("cheap_low_growth_cap", 60.0)),
        "rank_cap_veto_enabled": as_bool(
            raw.get("rank_quality_cap_veto_enabled", raw.get("rank_cap_veto_enabled", True)),
            True,
        ),
        "rank_cap_veto_threshold": float(raw.get("rank_cap_veto_threshold", 49.0)),
        "rank_cap_veto_reasons": set(
            parse_string_list(
                raw.get(
                    "rank_cap_veto_reasons",
                    [
                        "commercial_business_shock_cap",
                        "severe_commercial_deterioration_cap",
                        "no_guidance_negative_growth_cap",
                        "unprofitable_value_mismatch_cap",
                    ],
                )
            )
        ),
        "use_quality_adjusted_valuation_component": as_bool(
            cfg_get(config, "biotech_scoring.use_quality_adjusted_valuation_component", True),
            True,
        ),
    }


def guidance_quality_flags(commercial: dict[str, Any], forward_guidance: dict[str, Any]) -> dict[str, float]:
    latest_guidance = str(forward_guidance.get("latest_guidance_filing_date") or "").strip()
    recency_days = to_float(forward_guidance.get("guidance_recency_days"), math.nan)
    recency_penalty = to_float(forward_guidance.get("guidance_recency_penalty"), 0.0) or 0.0
    revenue_yoy = to_float(commercial.get("revenue_yoy_growth_pct"), math.nan)
    commercial_stage = bool(to_float(commercial.get("commercial_stage_flag"), 0.0))
    no_guidance = 1.0 if not latest_guidance else 0.0
    guidance_staleness = (
        1.0
        if no_guidance > 0.0
        or recency_penalty > 0.0
        or (math.isfinite(recency_days) and recency_days > 240.0)
        else 0.0
    )
    stale_guidance = 1.0 if math.isfinite(recency_days) and recency_days > 365.0 else 0.0
    no_guidance_negative_growth = (
        1.0
        if no_guidance > 0.0
        and commercial_stage
        and math.isfinite(revenue_yoy)
        and revenue_yoy <= 0.0
        else 0.0
    )
    return {
        "no_forward_guidance_flag": no_guidance,
        "guidance_staleness_flag": guidance_staleness,
        "guidance_stale_flag": stale_guidance,
        "no_guidance_negative_growth_flag": no_guidance_negative_growth,
    }


def apply_rank_quality_caps(
    opportunity: float,
    *,
    commercial: dict[str, Any],
    forward_guidance: dict[str, Any],
    commercial_risk: dict[str, Any],
    settings: dict[str, Any],
) -> tuple[float, float | None, list[str], dict[str, float]]:
    flags = guidance_quality_flags(commercial, forward_guidance)
    if not as_bool(settings.get("enabled", True), True):
        return opportunity, None, [], flags

    capped = opportunity
    cap_value: float | None = None
    reasons: list[str] = []
    revenue_yoy = to_float(commercial.get("revenue_yoy_growth_pct"), math.nan)
    commercial_stage = bool(to_float(commercial.get("commercial_stage_flag"), 0.0))
    profitable = bool(to_float(commercial.get("profitable_flag"), 0.0))
    valuation_score = (
        to_float(commercial.get("quality_adjusted_valuation_score"), math.nan)
        if as_bool(settings.get("use_quality_adjusted_valuation_component", True), True)
        and commercial.get("quality_adjusted_valuation_score") not in (None, "")
        else to_float(commercial.get("valuation_score"), math.nan)
    )
    guidance_score = (
        to_float(forward_guidance.get("quality_adjusted_guidance_score"), math.nan)
        if forward_guidance.get("quality_adjusted_guidance_score") not in (None, "")
        else to_float(forward_guidance.get("guidance_score"), math.nan)
    )
    business_shock = to_float(commercial_risk.get("commercial_business_shock_score"), 0.0) or 0.0
    deterioration = to_float(commercial_risk.get("commercial_deterioration_score"), 0.0) or 0.0
    valuation_mismatch = to_float(commercial_risk.get("valuation_growth_mismatch_score"), 0.0) or 0.0

    def apply_cap(reason: str, raw_cap: object) -> None:
        nonlocal capped, cap_value
        cap = to_float(raw_cap, math.nan)
        if not math.isfinite(cap):
            return
        if capped > cap:
            capped = cap
            cap_value = cap if cap_value is None else min(cap_value, cap)
            reasons.append(reason)

    if business_shock >= float(settings.get("business_shock_min_score") or 70.0):
        apply_cap("commercial_business_shock_cap", settings.get("business_shock_cap", 48.0))
    if (
        deterioration >= float(settings.get("severe_deterioration_min_score") or 70.0)
        and math.isfinite(revenue_yoy)
        and revenue_yoy <= float(settings.get("severe_deterioration_revenue_yoy_max") or -0.20)
    ):
        apply_cap("severe_commercial_deterioration_cap", settings.get("severe_deterioration_cap", 50.0))
    if flags["no_guidance_negative_growth_flag"] > 0.0:
        apply_cap("no_guidance_negative_growth_cap", settings.get("no_guidance_negative_growth_cap", 52.0))
    if valuation_mismatch >= float(settings.get("valuation_mismatch_min_score") or 70.0) and not profitable:
        apply_cap("unprofitable_value_mismatch_cap", settings.get("unprofitable_value_mismatch_cap", 50.0))
    if (
        commercial_stage
        and math.isfinite(valuation_score)
        and valuation_score >= float(settings.get("cheap_low_growth_valuation_min_score") or 90.0)
        and math.isfinite(revenue_yoy)
        and revenue_yoy <= float(settings.get("cheap_low_growth_revenue_yoy_max") or 0.10)
        and math.isfinite(guidance_score)
        and guidance_score <= float(settings.get("cheap_low_growth_guidance_max_score") or 60.0)
    ):
        apply_cap("cheap_low_growth_valuation_cap", settings.get("cheap_low_growth_cap", 60.0))
    return clamp(capped), cap_value, reasons, flags


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
    going_status = str(sec_liq.get("going_concern_status") or survival.get("going_concern_status") or "").strip().lower()
    if going_status in GOING_CONCERN_HARD_STATUSES:
        reasons.append("going_concern_confirmed")
    if to_float(sec_liq.get("reverse_split_hits_2y"), 0.0) > 0.0:
        reasons.append("reverse_split_history")

    verified_active = to_float(ctgov.get("verified_qualifying_active_trial_count"), 0.0)
    commercial_stage = bool(to_float(commercial.get("commercial_stage_flag"), 0.0))
    profitable = bool(to_float(commercial.get("profitable_flag"), 0.0))
    ttm_revenue = to_float(commercial.get("ttm_revenue"), 0.0)
    has_business_anchor = (
        commercial_stage
        or profitable
        or ttm_revenue >= float(settings.get("commercial_stage_revenue_min") or 50_000_000.0)
    )
    if verified_active <= 0.0 and not has_business_anchor:
        reasons.append("no_active_trial_no_business_anchor")

    addv = to_float(sec_liq.get("median_addv20", sec_liq.get("avg_dollar_volume_20d")), math.nan)
    min_addv20 = float(settings.get("min_addv20") or 0.0)
    if min_addv20 > 0.0 and (not math.isfinite(addv) or addv < min_addv20):
        reasons.append("illiquid")

    return [reason for reason in reasons if reason in configured_reasons]


def event_hard_weakness_reasons(payload: dict[str, Any], settings: dict[str, Any]) -> list[str]:
    sec_events = payload.get("sec_events", {}) if isinstance(payload, dict) else {}
    configured_reasons = set(settings.get("event_hard_reasons") or EVENT_HARD_WEAKNESS_DEFAULT_REASONS)
    reasons: list[str] = []
    if count_value(sec_events.get("dilution_event_count")) >= 2:
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
    ttm_revenue = to_float(commercial.get("ttm_revenue"), 0.0)
    has_business_anchor = (
        commercial_stage
        or profitable
        or ttm_revenue >= float(settings.get("commercial_stage_revenue_min") or 50_000_000.0)
    )
    verified_active = to_float(ctgov.get("verified_qualifying_active_trial_count"), 0.0)
    lead_phase2_3 = to_float(ctgov.get("lead_phase2_3_active_trials"), 0.0)
    program_phase2_3 = to_float(ctgov.get("program_phase2_3_active_trials"), 0.0)
    active_pivotal = to_float(ctgov.get("active_pivotal_trials"), 0.0)
    has_advanced_trial = lead_phase2_3 > 0.0 or program_phase2_3 > 0.0 or active_pivotal > 0.0

    if math.isfinite(cash_runway) and 9.0 <= cash_runway < 12.0 and not has_business_anchor:
        reasons.append("cash_runway_9_to_12m_clinical")
    going_status = str(sec_liq.get("going_concern_status") or survival.get("going_concern_status") or "").strip().lower()
    if going_status in GOING_CONCERN_SOFT_STATUSES:
        reasons.append("going_concern_warning")
    dilution_events = count_value(sec_events.get("dilution_event_count"))
    if 0 < dilution_events < 2:
        reasons.append("single_dilution_event")
    financial_quality = str(survival.get("data_quality") or "").strip().lower()
    if financial_quality in {"low", "poor", "stale"}:
        reasons.append("low_financial_data_quality")
    if as_bool(survival.get("burn_acceleration_flag"), False):
        reasons.append("burn_acceleration")
    commercial_fragility = to_float(governance.get("commercial_fragility_risk_score"), math.nan)
    if math.isfinite(commercial_fragility) and commercial_fragility >= float(settings.get("commercial_fragility_threshold") or 70.0):
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
    diagnostics: dict[str, float] | None = None,
) -> tuple[float, float, float, float, float]:
    if selection_policy not in PRODUCTION_SELECTION_POLICIES:
        raise ValueError(
            "Unknown biotech_scoring.production_baseline.selection_policy: "
            f"{selection_policy!r}; supported={sorted(PRODUCTION_SELECTION_POLICIES)}"
        )
    event_penalty = 0.0
    soft_penalty = 0.0
    if selection_policy.startswith("core_veto_event") and event_reasons:
        event_penalty = float(settings.get("event_hard_penalty") or 0.0)
    if selection_policy in SOFT_DRAG_SELECTION_POLICIES and soft_reasons:
        soft_penalty = float(settings.get("soft_weakness_penalty") or 0.0)
    diagnostics = diagnostics or {}
    quality_penalty = 0.0
    quality_bonus = 0.0
    if selection_policy in QUALITY_GUARDRAIL_SELECTION_POLICIES:
        quality_penalty += (
            float(settings.get("value_trap_penalty") or 0.0)
            * clamp(diagnostics.get("value_trap_score", 0.0))
            / 100.0
        )
        quality_penalty += (
            float(settings.get("leverage_fragility_penalty") or 0.0)
            * clamp(diagnostics.get("leverage_fragility_score", 0.0))
            / 100.0
        )
        quality_penalty += float(settings.get("guidance_staleness_penalty") or 0.0) * (
            1.0 if diagnostics.get("guidance_staleness_flag", 0.0) > 0.0 else 0.0
        )
        quality_penalty += (
            float(settings.get("mature_defensive_penalty") or 0.0)
            * clamp(diagnostics.get("mature_defensive_score", 0.0))
            / 100.0
        )
        quality_bonus += (
            float(settings.get("expected_return_quality_bonus") or 0.0)
            * clamp(diagnostics.get("expected_return_quality_score", 0.0))
            / 100.0
        )
    return (
        clamp(raw_opportunity - event_penalty - soft_penalty - quality_penalty + quality_bonus),
        event_penalty,
        soft_penalty,
        quality_penalty,
        quality_bonus,
    )


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
    going_status = str(sec_liq.get("going_concern_status") or survival.get("going_concern_status") or "").lower()
    recent_nt = int(to_float(sec_liq.get("recent_nt_filing_count_2y", 0)))
    has_advanced_catalyst = lead_phase2_3 > 0 or program_phase2_3 > 0 or pivotal > 0
    commercial_stage = bool(to_float(commercial.get("commercial_stage_flag"), 0.0))
    profitable = bool(to_float(commercial.get("profitable_flag"), 0.0))
    ttm_revenue = to_float(commercial.get("ttm_revenue"), 0.0)
    has_business_anchor = commercial_stage or profitable or ttm_revenue >= params.commercial_stage_revenue_min
    score_cmp = score + 1e-9

    if force_core_veto_avoid:
        return "avoid"
    if (
        risk >= params.avoid_risk_min
        or severe_runway
        or (math.isfinite(runway) and runway <= params.terminal_runway)
        or going_status in GOING_CONCERN_HARD_STATUSES
    ):
        return "avoid"
    if verified_active <= 0 and not has_business_anchor:
        return "avoid"
    if (
        score_cmp >= params.high_min
        and risk <= params.max_high_risk
        and recent_nt == 0
        and math.isfinite(runway)
        and runway >= params.min_high_runway
        and survival_quality != "low"
        and (has_advanced_catalyst or has_business_anchor or not params.require_advanced)
    ):
        return "high_conviction"
    if (
        score_cmp >= params.watch_min
        and risk <= params.max_watch_risk
        and (has_business_anchor or (math.isfinite(runway) and runway >= params.min_watch_runway))
        and (verified_active > 0 or has_business_anchor or not params.require_active_watch)
    ):
        return "watchlist"
    if score_cmp >= params.spec_min and risk <= params.max_spec_risk and (verified_active > 0 or has_business_anchor):
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
    if raw_profile is None:
        LOGGER.warning(
            "Missing biotech_scoring.investment_weight_profiles.%s; using biotech_scoring.investment_weights fallback.",
            profile_name,
        )
    elif isinstance(raw_profile, dict) and not raw_profile:
        LOGGER.warning("biotech_scoring.investment_weight_profiles.%s is empty; using field defaults.", profile_name)
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
        "institutional_upside": float(raw_weights.get("institutional_upside", 0.0)),
        "financial_quality": float(raw_weights.get("financial_quality", 0.15)),
        "momentum": float(raw_weights.get("momentum", 0.05)),
        # IC-validated signals: borrow_signal (IC=0.096) and institutional_crowding
        # (inverted 13F accumulation, IC=-0.074 → crowding penalty for commercial).
        "borrow_signal": float(raw_weights.get("borrow_signal", 0.0)),
        "institutional_crowding": float(raw_weights.get("institutional_crowding", 0.0)),
        "risk_penalty": float(raw_weights.get("risk_penalty", 0.15)),
    }
    negative = [name for name, value in weights.items() if value < 0.0]
    if negative:
        raise ValueError(f"Investment weight profile '{profile_name}' has negative weight(s): {', '.join(negative)}")
    non_finite = [name for name, value in weights.items() if not math.isfinite(value)]
    if non_finite:
        raise ValueError(f"Investment weight profile '{profile_name}' has non-finite weight(s): {', '.join(non_finite)}")
    risk_penalty = float(weights.get("risk_penalty", 0.15))
    if not 0.0 < risk_penalty <= 1.0:
        raise ValueError(
            f"Investment weight profile '{profile_name}' risk_penalty must be in (0, 1], got {risk_penalty}"
        )
    positive_total = sum(value for name, value in weights.items() if name != "risk_penalty")
    if positive_total <= 1e-12 or abs(positive_total - 1.0) > 1e-3:
        raise ValueError(
            f"Investment weight profile '{profile_name}' positive weights sum to {positive_total:.4f}; expected 1.0 +/- 0.001. "
            "Note: borrow_signal and institutional_crowding are now part of the positive weight sum."
        )
    return (
        profile_name,
        weights,
    )


def validate_clinical_weights(weights: dict[str, Any]) -> None:
    values = {
        "catalyst": float(weights.get("catalyst", 0.55)),
        "credibility": float(weights.get("credibility", 0.25)),
        "financial_quality": float(weights.get("financial_quality", 0.15)),
        "momentum": float(weights.get("momentum", 0.05)),
    }
    negative = [name for name, value in values.items() if value < 0.0]
    if negative:
        raise ValueError(f"biotech_scoring.weights has negative positive-component weight(s): {', '.join(negative)}")
    total = sum(values.values())
    if abs(total - 1.0) > 1e-3:
        raise ValueError(f"biotech_scoring.weights positive components sum to {total:.4f}; expected 1.0 +/- 0.001")
    risk_penalty = float(weights.get("risk_penalty", 0.15))
    if not 0.0 < risk_penalty <= 1.0:
        raise ValueError(f"biotech_scoring.weights.risk_penalty must be in (0, 1], got {risk_penalty}")


def enrich_biotech_cohort_rank_stats(rows: list[dict[str, Any]]) -> None:
    cohorts: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        cohort = row_calibration_cohort(row)
        cohorts.setdefault(cohort, []).append(row)
    for cohort_rows in cohorts.values():
        size = len(cohort_rows)
        reliability = min(1.0, size / 12.0) if size > 0 else 0.0
        sorted_rows = sorted(
            cohort_rows,
            key=lambda item: (
                -clamp(to_float(item.get("opportunity_score"), 0.0)),
                clamp(to_float(item.get("risk_score"), 100.0)),
                str(item.get("ticker") or ""),
            ),
        )
        for idx, row in enumerate(sorted_rows, start=1):
            percentile = 50.0 if size <= 1 else 100.0 * (size - idx) / (size - 1)
            shrunk = 50.0 + reliability * (percentile - 50.0)
            row["biotech_cohort_size"] = float(size)
            row["biotech_cohort_rank"] = float(idx)
            row["biotech_cohort_percentile"] = round(percentile, 4)
            row["biotech_cohort_percentile_shrunk"] = round(shrunk, 4)
            row["biotech_cohort_reliability_score"] = round(reliability, 4)
            row["biotech_cohort_sparse_data_flag"] = 1.0 if size < 8 else 0.0


def apply_biotech_cohort_policy(rows: list[dict[str, Any]], policy: CohortPolicy) -> None:
    if not policy.enabled:
        for row in rows:
            row["biotech_cohort_investible_flag"] = 1.0
            row["biotech_cohort_calibration_eligible_flag"] = 1.0
            row["biotech_cohort_calibration_mode"] = "policy_disabled"
            row["biotech_cohort_exclusion_reason"] = ""
        return
    for row in rows:
        cohort = row_calibration_cohort(row)
        cohort_size = int(to_float(row.get("biotech_cohort_size"), 0.0))
        review_required = to_float(row.get("biotech_taxonomy_review_required"), 0.0) > 0.0
        investible = cohort not in policy.non_investible_cohorts and not review_required
        if not investible:
            mode = "excluded"
            calibration_eligible = 0.0
            if review_required:
                reason = "taxonomy_review_required"
            elif cohort in policy.non_investible_cohorts:
                reason = f"non_investible_cohort:{cohort}"
            else:
                reason = "non_investible_cohort_policy"
        elif cohort in policy.calibration_excluded_cohorts:
            mode = "excluded"
            calibration_eligible = 0.0
            reason = f"calibration_excluded_cohort:{cohort}"
        elif cohort_size < policy.min_cohort_specific_company_count:
            mode = policy.sparse_valid_calibration_mode
            calibration_eligible = 1.0
            reason = f"sparse_valid_cohort_size_lt_{policy.min_cohort_specific_company_count}"
        else:
            mode = "cohort_specific"
            calibration_eligible = 1.0
            reason = ""
        row["biotech_cohort_investible_flag"] = 1.0 if investible else 0.0
        row["biotech_cohort_calibration_eligible_flag"] = calibration_eligible
        row["biotech_cohort_calibration_mode"] = mode
        row["biotech_cohort_exclusion_reason"] = reason
        if not investible:
            row["bucket"] = "avoid"


def score_rows(
    rows: list[dict[str, Any]],
    config: dict[str, Any],
    commercial_by_company: dict[int, dict[str, Any]],
    forward_by_company: dict[int, dict[str, Any]],
    governance_by_company: dict[int, dict[str, Any]] | None = None,
    taxonomy_overrides_by_ticker: dict[str, list[TaxonomyOverride]] | None = None,
    calibration_cohorts_by_ticker: dict[str, dict[str, str]] | None = None,
) -> list[dict[str, Any]]:
    governance_by_company = governance_by_company or {}
    taxonomy_overrides_by_ticker = taxonomy_overrides_by_ticker or {}
    calibration_cohorts_by_ticker = calibration_cohorts_by_ticker or {}
    calibration_cohort_settings = cfg_get(config, "biotech_scoring.calibration_cohorts", {}) or {}
    if not isinstance(calibration_cohort_settings, dict):
        calibration_cohort_settings = {}
    calibration_cohorts_enabled = as_bool(calibration_cohort_settings.get("enabled", False), False)
    calibration_cohorts_fallback_to_primary = as_bool(
        calibration_cohort_settings.get("fallback_to_primary", False),
        False,
    )
    calibration_cohorts_require_all = as_bool(
        calibration_cohort_settings.get("require_all_tickers", True),
        True,
    )
    calibration_cohort_version = (
        str(calibration_cohort_settings.get("version") or "calibration_cohort_unversioned").strip()
        or "calibration_cohort_unversioned"
    )
    weights = cfg_get(config, "biotech_scoring.weights", {}) or {}
    validate_clinical_weights(weights)
    catalyst_w = float(weights.get("catalyst", 0.55))
    credibility_w = float(weights.get("credibility", 0.25))
    financial_w = float(weights.get("financial_quality", 0.15))
    momentum_w = float(weights.get("momentum", 0.05))
    risk_w = float(weights.get("risk_penalty", 0.15))

    investment_enabled = as_bool(cfg_get(config, "biotech_scoring.use_investment_score", True), True)
    growth_drag_curve = configured_growth_drag_curve(config)
    core_veto_settings = core_structural_veto_settings(config)
    production_baseline = tier1_production_baseline(config)
    policy_settings = production_policy_settings(config)
    commercial_risk_settings = commercial_risk_overlay_settings(config)
    rank_cap_settings = rank_quality_cap_settings(config)
    bucket_settings = bucket_params(config)
    cohort_policy = cohort_policy_settings(config)
    selection_policy = str(production_baseline["selection_policy"])
    apply_core_veto_to_rank = bool(core_veto_settings["enabled"] and core_veto_settings["apply_to_rank"])
    force_core_veto_avoid = bool(core_veto_settings["enabled"] and core_veto_settings["force_avoid_bucket"])
    production_score_field = production_rank_score_field(config)

    scored: list[dict[str, Any]] = []
    missing_catalyst_raw: list[str] = []
    missing_credibility_raw: list[str] = []
    missing_financial_raw: list[str] = []
    missing_risk_raw: list[str] = []
    missing_momentum_raw: list[str] = []
    for row in rows:
        company_id = int(row["company_id"])
        payload = parse_json(
            row.get("feature_json"),
            context=f"company_id={company_id} ticker={row.get('ticker') or ''} source=daily_features",
        )
        raw_scores = payload.get("raw_scores", {}) if isinstance(payload, dict) else {}
        shadow_signals = payload.get("shadow_signals", {}) if isinstance(payload, dict) else {}
        forward_catalyst_calendar = (
            shadow_signals.get("forward_catalyst_calendar", {})
            if isinstance(shadow_signals, dict)
            else {}
        )
        if not isinstance(forward_catalyst_calendar, dict):
            forward_catalyst_calendar = {}
        short_interest = (
            shadow_signals.get("short_interest", {})
            if isinstance(shadow_signals, dict)
            else {}
        )
        if not isinstance(short_interest, dict):
            short_interest = {}
        borrow_availability = (
            shadow_signals.get("borrow_availability", {})
            if isinstance(shadow_signals, dict)
            else {}
        )
        if not isinstance(borrow_availability, dict):
            borrow_availability = {}
        institutional_ownership = (
            shadow_signals.get("institutional_ownership", {})
            if isinstance(shadow_signals, dict)
            else {}
        )
        if not isinstance(institutional_ownership, dict):
            institutional_ownership = {}
        insider_activity = (
            shadow_signals.get("insider_activity", {})
            if isinstance(shadow_signals, dict)
            else {}
        )
        if not isinstance(insider_activity, dict):
            insider_activity = {}
        commercial = commercial_by_company.get(company_id, {})
        forward_guidance = forward_by_company.get(company_id, {})
        governance = governance_by_company.get(company_id, {})
        commercial_risk = commercial_risk_overlay_fields(commercial, governance, commercial_risk_settings)
        missing_commercial_risk_fields = EXPECTED_COMMERCIAL_RISK_FIELDS - set(commercial_risk)
        if missing_commercial_risk_fields:
            raise RuntimeError(
                "commercial_risk_overlay_fields() missing expected field(s): "
                + ", ".join(sorted(missing_commercial_risk_fields))
            )
        forward_payload = parse_json(
            forward_guidance.get("payload_json"),
            context=f"company_id={company_id} ticker={row.get('ticker') or ''} source=forward_guidance",
        )
        catalyst_raw = optional_score(raw_scores, row, "catalyst_score_raw")
        credibility_raw = optional_score(raw_scores, row, "credibility_score_raw")
        financial_raw = optional_score(raw_scores, row, "financial_quality_score_raw")
        risk_raw = optional_score(raw_scores, row, "risk_score_raw")
        momentum_raw = optional_score(raw_scores, row, "momentum_score_raw")
        if catalyst_raw is None:
            missing_catalyst_raw.append(str(row.get("ticker") or company_id))
        if credibility_raw is None:
            missing_credibility_raw.append(str(row.get("ticker") or company_id))
        if financial_raw is None:
            missing_financial_raw.append(str(row.get("ticker") or company_id))
        if risk_raw is None:
            missing_risk_raw.append(str(row.get("ticker") or company_id))
        if momentum_raw is None:
            missing_momentum_raw.append(str(row.get("ticker") or company_id))
        catalyst = clamp(catalyst_raw if catalyst_raw is not None else 0.0)
        credibility = clamp(credibility_raw if credibility_raw is not None else 0.0)
        financial_quality = clamp(financial_raw if financial_raw is not None else 0.0)
        legacy_risk = clamp(risk_raw if risk_raw is not None else 0.0)
        risk_components = risk_decomposition_from_raw_scores(raw_scores, legacy_risk)
        risk, risk_penalty_mode_used = configured_risk_penalty_score(
            config=config,
            risk_components=risk_components,
            legacy_risk=legacy_risk,
        )
        uncompensated_risk = clamp(to_float(risk_components["uncompensated_risk_score"], legacy_risk))
        compensated_risk = clamp(to_float(risk_components["compensated_risk_score"], 50.0))
        forward_catalyst_nearest_days = finite_value_or_blank(
            forward_catalyst_calendar.get("forward_catalyst_nearest_days")
        )
        forward_catalyst_event_type = str(forward_catalyst_calendar.get("forward_catalyst_event_type") or "")
        forward_catalyst_source = str(forward_catalyst_calendar.get("forward_catalyst_source") or "")
        forward_catalyst_source_url = str(forward_catalyst_calendar.get("forward_catalyst_source_url") or "")
        forward_catalyst_confidence = finite_value_or_blank(
            forward_catalyst_calendar.get("forward_catalyst_confidence")
        )
        forward_catalyst_score = clamp(to_float(forward_catalyst_calendar.get("forward_catalyst_score"), 0.0))
        forward_catalyst_unfiltered_score = clamp(
            to_float(
                forward_catalyst_calendar.get("forward_catalyst_unfiltered_score"),
                forward_catalyst_score,
            )
        )
        ctgov_forward_catalyst_score = clamp(
            to_float(forward_catalyst_calendar.get("ctgov_forward_catalyst_score"), 0.0)
        )
        ctgov_forward_catalyst_guardrail_pass = (
            1.0
            if to_float(forward_catalyst_calendar.get("ctgov_forward_catalyst_guardrail_pass"), 0.0) > 0.0
            else 0.0
        )
        short_interest_shares = to_float(short_interest.get("short_interest_shares"), 0.0)
        float_shares = to_float(short_interest.get("float_shares"), 0.0)
        short_interest_pct_float = to_float(short_interest.get("short_interest_pct_float"), 0.0)
        days_to_cover = to_float(short_interest.get("days_to_cover"), 0.0)
        float_shares_source = str(short_interest.get("float_shares_source") or "")
        float_shares_asof_date = str(short_interest.get("float_shares_asof_date") or "")
        float_shares_source_asof_date = str(short_interest.get("float_shares_source_asof_date") or "")
        float_shares_staleness_days = to_float(short_interest.get("float_shares_staleness_days"), 0.0)
        float_shares_measurement_staleness_days = to_float(
            short_interest.get("float_shares_measurement_staleness_days"),
            0.0,
        )
        float_shares_proxy_flag = 1.0 if to_float(short_interest.get("float_shares_proxy_flag"), 0.0) > 0.0 else 0.0
        public_float_usd = to_float(short_interest.get("public_float_usd"), 0.0)
        public_float_price_date = str(short_interest.get("public_float_price_date") or "")
        public_float_close_price = to_float(short_interest.get("public_float_close_price"), 0.0)
        short_interest_pct_float_available_flag = (
            1.0 if to_float(short_interest.get("short_interest_pct_float_available_flag"), 0.0) > 0.0 else 0.0
        )
        short_interest_pct_score = clamp(to_float(short_interest.get("short_interest_pct_score"), 0.0))
        short_interest_days_to_cover_score = clamp(
            to_float(short_interest.get("short_interest_days_to_cover_score"), 0.0)
        )
        short_interest_signal_basis = str(short_interest.get("short_interest_signal_basis") or "")
        short_interest_signal_max_possible_score = clamp(
            to_float(short_interest.get("short_interest_signal_max_possible_score"), 0.0)
        )
        short_interest_signal_score = clamp(to_float(short_interest.get("short_interest_signal_score"), 0.0))
        borrow_rate_current = to_float(borrow_availability.get("borrow_rate_current"), 0.0)
        borrow_fee_data_available_flag = (
            1.0 if to_float(borrow_availability.get("borrow_fee_data_available_flag"), 0.0) > 0.0 else 0.0
        )
        shortable_data_available_flag = (
            1.0 if to_float(borrow_availability.get("shortable_data_available_flag"), 0.0) > 0.0 else 0.0
        )
        borrow_fee_stale_flag = 1.0 if to_float(borrow_availability.get("borrow_fee_stale_flag"), 0.0) > 0.0 else 0.0
        shortable_stale_flag = 1.0 if to_float(borrow_availability.get("shortable_stale_flag"), 0.0) > 0.0 else 0.0
        borrow_fee_staleness_days = to_float(borrow_availability.get("borrow_fee_staleness_days"), 0.0)
        shortable_staleness_days = to_float(borrow_availability.get("shortable_staleness_days"), 0.0)
        borrow_fee_history_count_30d = to_float(borrow_availability.get("borrow_fee_history_count_30d"), 0.0)
        borrow_fee_history_count_90d = to_float(borrow_availability.get("borrow_fee_history_count_90d"), 0.0)
        borrow_rate_30d_avg = to_float(borrow_availability.get("borrow_rate_30d_avg"), 0.0)
        borrow_rate_90d_avg = to_float(borrow_availability.get("borrow_rate_90d_avg"), 0.0)
        borrow_rate_spike_flag = 1.0 if to_float(borrow_availability.get("borrow_rate_spike_flag"), 0.0) > 0.0 else 0.0
        borrow_rate_declining_flag = 1.0 if to_float(borrow_availability.get("borrow_rate_declining_flag"), 0.0) > 0.0 else 0.0
        shortable_shares = to_float(borrow_availability.get("shortable_shares"), 0.0)
        shares_shortable_k = to_float(borrow_availability.get("shares_shortable_k"), 0.0)
        hard_to_borrow_flag = 1.0 if to_float(borrow_availability.get("hard_to_borrow_flag"), 0.0) > 0.0 else 0.0
        borrow_pressure_score = clamp(to_float(borrow_availability.get("borrow_pressure_score"), 0.0))
        high_borrow_pressure_flag = (
            1.0 if to_float(borrow_availability.get("high_borrow_pressure_flag"), 0.0) > 0.0 else 0.0
        )
        elevated_borrow_pressure_flag = (
            1.0 if to_float(borrow_availability.get("elevated_borrow_pressure_flag"), 0.0) > 0.0 else 0.0
        )
        borrow_rate_high_flag = (
            1.0 if to_float(borrow_availability.get("borrow_rate_high_flag"), 0.0) > 0.0 else 0.0
        )
        borrow_squeeze_setup_flag = (
            1.0 if to_float(borrow_availability.get("borrow_squeeze_setup_flag"), 0.0) > 0.0 else 0.0
        )
        borrow_distress_flag = (
            1.0 if to_float(borrow_availability.get("borrow_distress_flag"), 0.0) > 0.0 else 0.0
        )
        institutional_ownership_delta_pct = to_float(institutional_ownership.get("institutional_ownership_delta_pct"), 0.0)
        institutional_accumulation_score = clamp(to_float(institutional_ownership.get("institutional_accumulation_score"), 50.0))
        new_institutional_buyer_count = to_float(institutional_ownership.get("new_institutional_buyer_count"), 0.0)
        exiting_institutional_holder_count = to_float(institutional_ownership.get("exiting_institutional_holder_count"), 0.0)
        net_institutional_buyer_count = to_float(institutional_ownership.get("net_institutional_buyer_count"), 0.0)
        insider_buy_count_90d = to_float(insider_activity.get("insider_buy_count_90d"), 0.0)
        open_market_buy_count_90d = to_float(insider_activity.get("open_market_buy_count_90d"), 0.0)
        planned_10b5_1_buy_count = to_float(insider_activity.get("planned_10b5_1_buy_count"), 0.0)
        insider_buy_value_90d = to_float(insider_activity.get("insider_buy_value_90d"), 0.0)
        insider_buy_cluster_count_90d = to_float(insider_activity.get("insider_buy_cluster_count_90d"), 0.0)
        insider_sell_value_90d = to_float(insider_activity.get("insider_sell_value_90d"), 0.0)
        insider_accumulation_score = clamp(to_float(insider_activity.get("insider_accumulation_score"), 50.0))
        momentum = clamp(momentum_raw if momentum_raw is not None else 0.0)
        clinical_positive = (
            catalyst_w * catalyst
            + credibility_w * credibility
            + financial_w * financial_quality
            + momentum_w * momentum
        )
        clinical_risk_drag = convex_risk_drag(risk, risk_w, config, "biotech_scoring")
        clinical_opportunity = clamp(clinical_positive - clinical_risk_drag)

        commercial_value_default = float(cfg_get(config, "biotech_scoring.missing_score_defaults.commercial_value_score", 35.0))
        commercial_quality = clamp(to_float(commercial.get("commercial_quality_score"), commercial_value_default))
        commercial_value = clamp(to_float(commercial.get("commercial_value_score"), commercial_value_default))
        forward_guidance_score = clamp(to_float(forward_guidance.get("guidance_score"), float(cfg_get(config, "biotech_scoring.missing_score_defaults.forward_guidance_score", 35.0))))
        valuation_score = clamp(to_float(commercial.get("valuation_score"), float(cfg_get(config, "biotech_scoring.missing_score_defaults.valuation_score", 50.0))))
        upside_capacity_score = clamp(to_float(commercial.get("upside_capacity_score"), float(cfg_get(config, "biotech_scoring.missing_score_defaults.upside_capacity_score", 50.0))))
        institutional_upside_capacity_score = clamp(
            to_float(
                commercial.get("institutional_upside_capacity_score"),
                float(cfg_get(config, "biotech_scoring.missing_score_defaults.institutional_upside_capacity_score", 50.0)),
            )
        )
        leverage_score = clamp(to_float(commercial.get("leverage_score"), 50.0))
        value_trap_score = clamp(to_float(commercial.get("value_trap_score"), 0.0))
        mature_defensive = mature_defensive_score(
            commercial,
            forward_guidance,
            growth_drag_curve=growth_drag_curve,
        )
        quality_adjusted_valuation_score = clamp(to_float(commercial.get("quality_adjusted_valuation_score"), valuation_score))
        forward_valuation_score = to_float(forward_guidance.get("forward_valuation_score"), 50.0)
        quality_forward_valuation_score = clamp(
            to_float(forward_guidance.get("quality_forward_valuation_score"), forward_valuation_score)
        )
        quality_adjusted_guidance_score = clamp(to_float(forward_guidance.get("quality_adjusted_guidance_score"), forward_guidance_score))
        guidance_recency_penalty = clamp(to_float(forward_guidance.get("guidance_recency_penalty"), 0.0), 0.0, 100.0)
        guidance_flags_preview = guidance_quality_flags(commercial, forward_guidance)
        expected_return_quality = expected_return_quality_score(
            commercial=commercial,
            forward_guidance=forward_guidance,
            momentum=momentum,
            risk=risk,
            mature_defensive=mature_defensive,
        )
        commercial_overlay_context = dict(commercial)
        commercial_overlay_context["valuation_growth_mismatch_score"] = commercial_risk[
            "valuation_growth_mismatch_score"
        ]
        commercial_expected_return_overlay = score_commercial_expected_return_overlay(
            commercial=commercial_overlay_context,
            forward_guidance=forward_guidance,
            momentum_score=momentum,
            risk_score=risk,
            mature_defensive_score=mature_defensive,
        )
        policy_diagnostics = {
            "value_trap_score": value_trap_score,
            "leverage_fragility_score": clamp(100.0 - leverage_score),
            "guidance_staleness_flag": guidance_flags_preview["guidance_staleness_flag"],
            "mature_defensive_score": mature_defensive,
            "expected_return_quality_score": expected_return_quality,
            **commercial_expected_return_overlay,
            "commercial_deterioration_score": commercial_risk["commercial_deterioration_score"],
            "valuation_growth_mismatch_score": commercial_risk["valuation_growth_mismatch_score"],
            "commercial_business_shock_score": commercial_risk["commercial_business_shock_score"],
            "legacy_risk_score": legacy_risk,
            "risk_penalty_input_score": risk_components["risk_penalty_input_score"],
            "predictive_risk_penalty_input_score": risk_components["predictive_risk_penalty_input_score"],
            "uncompensated_risk_score": uncompensated_risk,
            "compensated_risk_score": compensated_risk,
        }
        biotech_taxonomy = classify_biotech_cohort(
            payload=payload,
            commercial=commercial,
            forward_guidance=forward_guidance,
            diagnostics=policy_diagnostics,
        )
        ticker = str(row.get("ticker") or "").strip().upper()
        row_asof_date = parse_date(row.get("asof_date"))
        if row_asof_date is None:
            row_asof_date = date.min
        matching_overrides = [
            override
            for override in taxonomy_overrides_by_ticker.get(ticker, [])
            if override.applies_to(asof_date=row_asof_date)
        ]
        if len(matching_overrides) > 1:
            raise ValueError(f"Multiple active biotech taxonomy overrides for ticker={ticker} asof_date={row_asof_date}")
        if matching_overrides:
            override = matching_overrides[0]
            biotech_taxonomy = apply_manual_taxonomy_override(
                biotech_taxonomy,
                primary_cohort=override.primary_cohort,
                secondary_cohort=override.secondary_cohort,
                confidence=override.confidence,
                overlays_add=override.overlays_add,
                reason_codes_add=override.reason_codes_add,
                source=override.source,
                note=override.note,
            )
        biotech_taxonomy_payload = biotech_taxonomy.as_payload()
        primary_cohort_for_calibration = str(
            biotech_taxonomy_payload.get("biotech_primary_cohort")
            or biotech_taxonomy_payload.get("primary_cohort")
            or "unmapped_calibration_cohort"
        )
        if calibration_cohorts_enabled and calibration_cohorts_require_all and ticker not in calibration_cohorts_by_ticker:
            raise ValueError(
                f"Ticker {ticker} is missing from the official biotech calibration cohort map; "
                "old taxonomy-cohort fallback is disabled."
            )
        biotech_taxonomy_payload |= calibration_cohort_fields(
            ticker=ticker,
            primary_cohort=primary_cohort_for_calibration,
            calibration_cohorts_by_ticker=calibration_cohorts_by_ticker,
            enabled=calibration_cohorts_enabled,
            fallback_to_primary=calibration_cohorts_fallback_to_primary,
            version=calibration_cohort_version,
        )
        biotech_taxonomy_payload = apply_official_operational_cohort_payload(
            ticker=ticker,
            payload=biotech_taxonomy_payload,
        )
        use_quality_adjusted_valuation = as_bool(
            cfg_get(config, "biotech_scoring.use_quality_adjusted_valuation_component", True),
            True,
        )
        use_quality_adjusted_guidance = as_bool(
            cfg_get(config, "biotech_scoring.use_quality_adjusted_guidance_component", True),
            True,
        )
        valuation_component_score = quality_adjusted_valuation_score if use_quality_adjusted_valuation else valuation_score
        forward_guidance_component_score = (
            quality_adjusted_guidance_score if use_quality_adjusted_guidance else forward_guidance_score
        )
        used_quality_adjusted_valuation = 1.0 if use_quality_adjusted_valuation else 0.0
        used_quality_adjusted_guidance = 1.0 if use_quality_adjusted_guidance else 0.0
        valuation_quality_adjustment_delta = valuation_score - quality_adjusted_valuation_score
        guidance_quality_adjustment_delta = forward_guidance_score - quality_adjusted_guidance_score
        profile_name, profile_weights = investment_weight_profile(config, commercial)
        embedded_financial_quality_weight = profile_weights["clinical_opportunity"] * financial_w
        embedded_momentum_weight = profile_weights["clinical_opportunity"] * momentum_w
        # The investment layer uses the raw clinical-positive composite, then
        # adds only residual weights for financial quality and momentum so their
        # total effective allocation equals the profile-level target.
        residual_financial_quality_weight = max(
            0.0,
            profile_weights["financial_quality"] - embedded_financial_quality_weight,
        )
        residual_momentum_weight = max(0.0, profile_weights["momentum"] - embedded_momentum_weight)
        # borrow_signal uses borrow_pressure_score (already extracted; IC=0.096 at 120d).
        # Higher borrow pressure = expensive to short = squeeze potential = positive signal.
        borrow_signal_component = borrow_pressure_score
        # institutional_crowding inverts the 13F accumulation signal.
        # IC=-0.074 at 120d overall; -0.245 for commercial_profitable. High accumulation
        # = crowded consensus trade = underperformance. Low accumulation = contrarian.
        institutional_crowding_component = clamp(100.0 - institutional_accumulation_score)
        investment_positive = (
            profile_weights["clinical_opportunity"] * clinical_positive
            + profile_weights["commercial_value"] * commercial_value
            + profile_weights["forward_guidance"] * forward_guidance_component_score
            + profile_weights["valuation"] * valuation_component_score
            + profile_weights["upside_capacity"] * upside_capacity_score
            + profile_weights["institutional_upside"] * institutional_upside_capacity_score
            + residual_financial_quality_weight * financial_quality
            + residual_momentum_weight * momentum
            + profile_weights.get("borrow_signal", 0.0) * borrow_signal_component
            + profile_weights.get("institutional_crowding", 0.0) * institutional_crowding_component
        )
        investment_risk_drag = convex_risk_drag(risk, profile_weights["risk_penalty"], config, "biotech_scoring")
        effective_pre_confidence_risk_drag = investment_risk_drag
        confidence_multiplier = score_confidence_multiplier(config, payload, commercial, forward_guidance, profile_name)
        pre_confidence_investment_score = clamp(investment_positive - investment_risk_drag)
        confidence_adjusted_score_reduction = pre_confidence_investment_score * (1.0 - confidence_multiplier)
        investment_score = clamp(pre_confidence_investment_score * confidence_multiplier)
        effective_total_risk_drag = max(0.0, investment_positive - investment_score)
        effective_post_confidence_risk_drag = effective_total_risk_drag
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
        opportunity, event_penalty, soft_penalty, quality_penalty, quality_bonus = apply_production_selection_policy(
            raw_opportunity,
            selection_policy=selection_policy,
            event_reasons=event_reasons,
            soft_reasons=soft_reasons,
            settings=policy_settings,
            diagnostics=policy_diagnostics,
        )
        commercial_overlay_penalty = commercial_risk_policy_penalty(commercial_risk, commercial_risk_settings)
        opportunity = clamp(opportunity - commercial_overlay_penalty)
        pre_rank_cap_opportunity = opportunity
        opportunity, rank_quality_cap, rank_quality_cap_reasons, guidance_flags = apply_rank_quality_caps(
            opportunity,
            commercial=commercial,
            forward_guidance=forward_guidance,
            commercial_risk=commercial_risk,
            settings=rank_cap_settings,
        )
        rank_cap_reason_set = set(rank_quality_cap_reasons)
        rank_cap_vetoed = (
            as_bool(rank_cap_settings.get("rank_cap_veto_enabled", True), True)
            and rank_quality_cap is not None
            and rank_quality_cap <= float(rank_cap_settings.get("rank_cap_veto_threshold", 49.0))
            and bool(rank_cap_reason_set.intersection(rank_cap_settings.get("rank_cap_veto_reasons") or set()))
        )
        rank_quality_cap_veto_reasons = sorted(
            rank_cap_reason_set.intersection(rank_cap_settings.get("rank_cap_veto_reasons") or set())
        )
        if rank_cap_vetoed:
            opportunity = 0.0
        selection_gate = tier1_selection_gate_score(opportunity, risk)
        primary_cohort = str(
            biotech_taxonomy_payload.get("biotech_primary_cohort")
            or biotech_taxonomy_payload.get("primary_cohort")
            or ""
        )
        operational_cohort = str(
            biotech_taxonomy_payload.get("biotech_calibration_cohort")
            or primary_cohort
            or ""
        )
        allocation_risk = risk
        allocation_risk_penalty_mode = risk_penalty_mode_used
        discovery_risk, discovery_risk_penalty_mode = configured_risk_penalty_score(
            config=config,
            risk_components=risk_components,
            legacy_risk=legacy_risk,
            purpose="discovery",
            primary_cohort=operational_cohort,
        )
        discovery_clinical_risk_drag = convex_risk_drag(discovery_risk, risk_w, config, "biotech_scoring")
        discovery_clinical_opportunity = clamp(clinical_positive - discovery_clinical_risk_drag)
        discovery_investment_risk_drag = convex_risk_drag(
            discovery_risk,
            profile_weights["risk_penalty"],
            config,
            "biotech_scoring",
        )
        discovery_pre_confidence_investment_score = clamp(investment_positive - discovery_investment_risk_drag)
        discovery_investment_score = clamp(discovery_pre_confidence_investment_score * confidence_multiplier)
        discovery_raw_opportunity = discovery_investment_score if investment_enabled else discovery_clinical_opportunity
        discovery_expected_return_quality = expected_return_quality_score(
            commercial=commercial,
            forward_guidance=forward_guidance,
            momentum=momentum,
            risk=discovery_risk,
            mature_defensive=mature_defensive,
        )
        discovery_policy_diagnostics = {
            **policy_diagnostics,
            "expected_return_quality_score": discovery_expected_return_quality,
        }
        discovery_soft_reasons = soft_weakness_reasons(
            payload,
            commercial,
            governance,
            risk=discovery_risk,
            settings=policy_settings,
        )
        (
            discovery_opportunity,
            discovery_event_penalty,
            discovery_soft_penalty,
            discovery_quality_penalty,
            discovery_quality_bonus,
        ) = apply_production_selection_policy(
            discovery_raw_opportunity,
            selection_policy=selection_policy,
            event_reasons=event_reasons,
            soft_reasons=discovery_soft_reasons,
            settings=policy_settings,
            diagnostics=discovery_policy_diagnostics,
        )
        discovery_opportunity = clamp(discovery_opportunity - commercial_overlay_penalty)
        discovery_opportunity, discovery_rank_quality_cap, discovery_rank_quality_cap_reasons, _discovery_flags = (
            apply_rank_quality_caps(
                discovery_opportunity,
                commercial=commercial,
                forward_guidance=forward_guidance,
                commercial_risk=commercial_risk,
                settings=rank_cap_settings,
            )
        )
        discovery_rank_cap_reason_set = set(discovery_rank_quality_cap_reasons)
        discovery_rank_cap_vetoed = (
            as_bool(rank_cap_settings.get("rank_cap_veto_enabled", True), True)
            and discovery_rank_quality_cap is not None
            and discovery_rank_quality_cap <= float(rank_cap_settings.get("rank_cap_veto_threshold", 49.0))
            and bool(discovery_rank_cap_reason_set.intersection(rank_cap_settings.get("rank_cap_veto_reasons") or set()))
        )
        if discovery_rank_cap_vetoed:
            discovery_opportunity = 0.0
        discovery_selection_gate = tier1_selection_gate_score(discovery_opportunity, discovery_risk)
        allocation_opportunity_score = opportunity
        allocation_bucket = score_bucket(
            allocation_opportunity_score,
            risk,
            bucket_settings,
            payload,
            commercial,
            core_veto_reasons=core_veto_reasons,
            force_core_veto_avoid=force_core_veto_avoid and bool(core_veto_reasons),
        )
        if production_score_field == "discovery_opportunity_score":
            production_rank_score = discovery_opportunity
            production_rank_risk_score = discovery_risk
            production_score_source = "routed_discovery"
        else:
            production_rank_score = allocation_opportunity_score
            production_rank_risk_score = risk
            production_score_source = "legacy_allocation"
        if allocation_bucket == "avoid" or rank_cap_vetoed or (force_core_veto_avoid and bool(core_veto_reasons)):
            production_rank_score = 0.0
            production_rank_risk_score = 100.0
        production_bucket = score_bucket(
            production_rank_score,
            production_rank_risk_score,
            bucket_settings,
            payload,
            commercial,
            core_veto_reasons=core_veto_reasons,
            force_core_veto_avoid=force_core_veto_avoid and bool(core_veto_reasons),
        )
        core_veto_flag = 1.0 if core_veto_reasons else 0.0
        rank_demoted_by_core_veto = bool(apply_core_veto_to_rank and core_veto_reasons)

        ctgov = payload.get("ctgov", {}) if isinstance(payload, dict) else {}
        sec_liq = payload.get("sec_and_liquidity", {}) if isinstance(payload, dict) else {}
        survival = payload.get("financial_survival", {}) if isinstance(payload, dict) else {}
        sec_events = payload.get("sec_events", {}) if isinstance(payload, dict) else {}
        company_strategy_category = str(payload.get("company_strategy_category") or "") if isinstance(payload, dict) else ""
        ctgov_evidence_type = str(ctgov.get("ctgov_evidence_type") or "")
        ctgov_review_bucket = str(ctgov.get("review_bucket") or "")
        ctgov_manual_root_cause = str(ctgov.get("manual_root_cause") or "")
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
            "avg_dollar_volume_20d": commercial.get("avg_dollar_volume_20d", ""),
            "return_3m_pct": commercial.get("return_3m_pct", ""),
            "price_vs_200d_pct": commercial.get("price_vs_200d_pct", ""),
            "distance_from_52w_high_pct": commercial.get("distance_from_52w_high_pct", ""),
            "relative_strength_3m_vs_xbi": commercial.get("relative_strength_3m_vs_xbi", ""),
            "price_to_sales": commercial.get("price_to_sales", ""),
            "ev_to_sales": commercial.get("ev_to_sales", ""),
            "pe_ratio": commercial.get("pe_ratio", ""),
            "fcf_yield": commercial.get("fcf_yield", ""),
            "commercial_stage_flag": bool(to_float(commercial.get("commercial_stage_flag"), 0.0)),
            "profitable_flag": bool(to_float(commercial.get("profitable_flag"), 0.0)),
            "commercial_quality_score": commercial_quality,
            "commercial_value_score": commercial_value,
            "valuation_score": valuation_score,
            "quality_adjusted_valuation_score": quality_adjusted_valuation_score,
            "upside_capacity_score": upside_capacity_score,
            "institutional_upside_capacity_score": institutional_upside_capacity_score,
            "leverage_score": leverage_score,
            "value_trap_score": value_trap_score,
            **commercial_expected_return_overlay,
            "data_quality": commercial.get("data_quality", ""),
            "missing_fields": commercial.get("missing_fields", ""),
            "proxy_fields_used": commercial.get("proxy_fields_used", ""),
        }
        top_evidence = {
            "primary_nct": ctgov.get("primary_nct", ""),
            "primary_trial_title": ctgov.get("primary_trial_title", ""),
            "top_ncts": ctgov.get("top_ncts", []),
            "ctgov_quality": {
                "ctgov_evidence_type": ctgov_evidence_type,
                "company_strategy_category": company_strategy_category,
                "ctgov_review_bucket": ctgov_review_bucket,
                "ctgov_manual_root_cause": ctgov_manual_root_cause,
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
                "quality_forward_valuation_score": quality_forward_valuation_score,
                "quality_adjusted_guidance_score": quality_adjusted_guidance_score,
                "guidance_recency_penalty": guidance_recency_penalty,
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
                "discovery_clinical_opportunity_score": round(discovery_clinical_opportunity, 4),
                "investment_score": round(investment_score, 4),
                "discovery_investment_score": round(discovery_investment_score, 4),
                "raw_opportunity_score_before_policy": round(raw_opportunity, 4),
                "policy_adjusted_opportunity_score": round(opportunity, 4),
                "allocation_opportunity_score": round(allocation_opportunity_score, 4),
                "allocation_bucket": allocation_bucket,
                "production_rank_score": round(production_rank_score, 4),
                "production_rank_risk_score": round(production_rank_risk_score, 4),
                "production_rank_score_field": production_score_field,
                "production_score_source": production_score_source,
                "discovery_opportunity_score": round(discovery_opportunity, 4),
                "production_policy_event_hard_penalty": round(event_penalty, 4),
                "production_policy_soft_weakness_penalty": round(soft_penalty, 4),
                "production_policy_quality_penalty": round(quality_penalty, 4),
                "production_policy_quality_bonus": round(quality_bonus, 4),
                "discovery_policy_event_hard_penalty": round(discovery_event_penalty, 4),
                "discovery_policy_soft_weakness_penalty": round(discovery_soft_penalty, 4),
                "discovery_policy_quality_penalty": round(discovery_quality_penalty, 4),
                "discovery_policy_quality_bonus": round(discovery_quality_bonus, 4),
                "commercial_risk_overlay_penalty": round(commercial_overlay_penalty, 4),
                "pre_rank_cap_opportunity_score": round(pre_rank_cap_opportunity, 4),
                "rank_quality_cap": rank_quality_cap if rank_quality_cap is not None else "",
                "rank_quality_cap_reasons": rank_quality_cap_reasons,
                "rank_quality_cap_vetoed": 1.0 if rank_cap_vetoed else 0.0,
                "rank_quality_cap_veto_reasons": rank_quality_cap_veto_reasons,
                "discovery_rank_quality_cap": discovery_rank_quality_cap
                if discovery_rank_quality_cap is not None
                else "",
                "discovery_rank_quality_cap_reasons": discovery_rank_quality_cap_reasons,
                "discovery_rank_quality_cap_vetoed": 1.0 if discovery_rank_cap_vetoed else 0.0,
                "production_policy_total_penalty": round(
                    event_penalty + soft_penalty + quality_penalty + commercial_overlay_penalty - quality_bonus,
                    4,
                ),
                "production_policy_event_hard_reasons": event_reasons,
                "production_policy_soft_weakness_reasons": soft_reasons,
                "commercial_risk_overlay_score": commercial_risk["commercial_risk_overlay_score"],
                "commercial_risk_overlay_reasons": commercial_risk["commercial_risk_overlay_reasons"],
                "commercial_deterioration_score": commercial_risk["commercial_deterioration_score"],
                "valuation_growth_mismatch_score": commercial_risk["valuation_growth_mismatch_score"],
                "transient_revenue_anchor_score": commercial_risk["transient_revenue_anchor_score"],
                "commercial_business_shock_score": commercial_risk["commercial_business_shock_score"],
                "forward_catalyst_nearest_days": forward_catalyst_nearest_days,
                "forward_catalyst_event_type": forward_catalyst_event_type,
                "forward_catalyst_source": forward_catalyst_source,
                "forward_catalyst_source_url": forward_catalyst_source_url,
                "forward_catalyst_confidence": forward_catalyst_confidence,
                "forward_catalyst_score": round(forward_catalyst_score, 4),
                "forward_catalyst_unfiltered_score": round(forward_catalyst_unfiltered_score, 4),
                "ctgov_forward_catalyst_score": round(ctgov_forward_catalyst_score, 4),
                "ctgov_forward_catalyst_guardrail_pass": ctgov_forward_catalyst_guardrail_pass,
                "short_interest_shares": round(short_interest_shares, 4),
                "float_shares": round(float_shares, 4),
                "short_interest_pct_float": round(short_interest_pct_float, 8),
                "days_to_cover": round(days_to_cover, 4),
                "float_shares_source": float_shares_source,
                "float_shares_asof_date": float_shares_asof_date,
                "float_shares_source_asof_date": float_shares_source_asof_date,
                "float_shares_staleness_days": round(float_shares_staleness_days, 4),
                "float_shares_measurement_staleness_days": round(float_shares_measurement_staleness_days, 4),
                "float_shares_proxy_flag": float_shares_proxy_flag,
                "public_float_usd": round(public_float_usd, 4),
                "public_float_price_date": public_float_price_date,
                "public_float_close_price": round(public_float_close_price, 8),
                "short_interest_pct_float_available_flag": short_interest_pct_float_available_flag,
                "short_interest_pct_score": round(short_interest_pct_score, 4),
                "short_interest_days_to_cover_score": round(short_interest_days_to_cover_score, 4),
                "short_interest_signal_basis": short_interest_signal_basis,
                "short_interest_signal_max_possible_score": round(short_interest_signal_max_possible_score, 4),
                "short_interest_signal_score": round(short_interest_signal_score, 4),
                "borrow_rate_current": round(borrow_rate_current, 8),
                "borrow_fee_data_available_flag": borrow_fee_data_available_flag,
                "shortable_data_available_flag": shortable_data_available_flag,
                "borrow_fee_stale_flag": borrow_fee_stale_flag,
                "shortable_stale_flag": shortable_stale_flag,
                "borrow_fee_staleness_days": round(borrow_fee_staleness_days, 4),
                "shortable_staleness_days": round(shortable_staleness_days, 4),
                "borrow_fee_history_count_30d": round(borrow_fee_history_count_30d, 4),
                "borrow_fee_history_count_90d": round(borrow_fee_history_count_90d, 4),
                "borrow_rate_30d_avg": round(borrow_rate_30d_avg, 8),
                "borrow_rate_90d_avg": round(borrow_rate_90d_avg, 8),
                "borrow_rate_spike_flag": borrow_rate_spike_flag,
                "borrow_rate_declining_flag": borrow_rate_declining_flag,
                "shortable_shares": round(shortable_shares, 4),
                "shares_shortable_k": round(shares_shortable_k, 4),
                "hard_to_borrow_flag": hard_to_borrow_flag,
                "borrow_pressure_score": round(borrow_pressure_score, 4),
                "high_borrow_pressure_flag": high_borrow_pressure_flag,
                "elevated_borrow_pressure_flag": elevated_borrow_pressure_flag,
                "borrow_rate_high_flag": borrow_rate_high_flag,
                "borrow_squeeze_setup_flag": borrow_squeeze_setup_flag,
                "borrow_distress_flag": borrow_distress_flag,
                "institutional_ownership_delta_pct": round(institutional_ownership_delta_pct, 8),
                "institutional_accumulation_score": round(institutional_accumulation_score, 4),
                "new_institutional_buyer_count": round(new_institutional_buyer_count, 4),
                "exiting_institutional_holder_count": round(exiting_institutional_holder_count, 4),
                "net_institutional_buyer_count": round(net_institutional_buyer_count, 4),
                "insider_buy_count_90d": round(insider_buy_count_90d, 4),
                "open_market_buy_count_90d": round(open_market_buy_count_90d, 4),
                "planned_10b5_1_buy_count": round(planned_10b5_1_buy_count, 4),
                "insider_buy_value_90d": round(insider_buy_value_90d, 2),
                "insider_buy_cluster_count_90d": round(insider_buy_cluster_count_90d, 4),
                "insider_sell_value_90d": round(insider_sell_value_90d, 2),
                "insider_accumulation_score": round(insider_accumulation_score, 4),
                "tier1_selection_gate_score": round(selection_gate, 4),
                "discovery_selection_gate_score": round(discovery_selection_gate, 4),
                "investment_profile": profile_name,
                "investment_weights": profile_weights,
                "growth_drag_curve": growth_drag_curve,
                "clinical_risk_drag": round(clinical_risk_drag, 4),
                "investment_risk_drag": round(investment_risk_drag, 4),
                "discovery_clinical_risk_drag": round(discovery_clinical_risk_drag, 4),
                "discovery_investment_risk_drag": round(discovery_investment_risk_drag, 4),
                "risk_penalty_mode_used": risk_penalty_mode_used,
                "allocation_risk_penalty_mode": allocation_risk_penalty_mode,
                "discovery_risk_penalty_mode": discovery_risk_penalty_mode,
                "legacy_risk_score": round(legacy_risk, 4),
                "allocation_risk_score": round(allocation_risk, 4),
                "discovery_risk_score": round(discovery_risk, 4),
                "risk_penalty_input_score": round(
                    to_float(risk_components["risk_penalty_input_score"], legacy_risk),
                    4,
                ),
                "predictive_risk_penalty_input_score": round(
                    to_float(risk_components["predictive_risk_penalty_input_score"], legacy_risk),
                    4,
                ),
                "risk_score_used_for_penalty": round(risk, 4),
                "uncompensated_risk_score": round(uncompensated_risk, 4),
                "compensated_risk_score": round(compensated_risk, 4),
                "risk_component_scores": risk_components["risk_component_scores"],
                "effective_pre_confidence_risk_drag": round(effective_pre_confidence_risk_drag, 4),
                "effective_post_confidence_risk_drag": round(effective_post_confidence_risk_drag, 4),
                "effective_total_risk_drag": round(effective_total_risk_drag, 4),
                "confidence_adjusted_score_reduction": round(confidence_adjusted_score_reduction, 4),
                "embedded_financial_quality_weight": round(embedded_financial_quality_weight, 6),
                "residual_financial_quality_weight": round(residual_financial_quality_weight, 6),
                "embedded_momentum_weight": round(embedded_momentum_weight, 6),
                "residual_momentum_weight": round(residual_momentum_weight, 6),
                "data_quality_confidence_multiplier": round(confidence_multiplier, 4),
                "commercial_quality_score": round(commercial_quality, 4),
                "commercial_value_score": round(commercial_value, 4),
                "forward_guidance_score": round(forward_guidance_score, 4),
                "valuation_score": round(valuation_score, 4),
                "valuation_component_score": round(valuation_component_score, 4),
                "quality_adjusted_valuation_score": round(quality_adjusted_valuation_score, 4),
                "used_quality_adjusted_valuation": bool(used_quality_adjusted_valuation),
                "valuation_quality_adjustment_delta": round(valuation_quality_adjustment_delta, 4),
                "upside_capacity_score": round(upside_capacity_score, 4),
                "institutional_upside_capacity_score": round(institutional_upside_capacity_score, 4),
                "leverage_score": round(leverage_score, 4),
                "value_trap_score": round(value_trap_score, 4),
                "leverage_fragility_score": round(policy_diagnostics["leverage_fragility_score"], 4),
                "mature_defensive_score": round(mature_defensive, 4),
                "expected_return_quality_score": round(expected_return_quality, 4),
                **commercial_expected_return_overlay,
                "quality_forward_valuation_score": round(quality_forward_valuation_score, 4),
                "quality_adjusted_guidance_score": round(quality_adjusted_guidance_score, 4),
                "forward_guidance_component_score": round(forward_guidance_component_score, 4),
                "used_quality_adjusted_guidance": bool(used_quality_adjusted_guidance),
                "guidance_quality_adjustment_delta": round(guidance_quality_adjustment_delta, 4),
                "guidance_recency_penalty": round(guidance_recency_penalty, 4),
                "no_forward_guidance_flag": guidance_flags["no_forward_guidance_flag"],
                "guidance_staleness_flag": guidance_flags["guidance_staleness_flag"],
                "guidance_stale_flag": guidance_flags["guidance_stale_flag"],
                "no_guidance_negative_growth_flag": guidance_flags["no_guidance_negative_growth_flag"],
            },
            "downstream_interaction": {
                "recommended_use": "gate_or_cap_multibagger_candidates_do_not_add_as_duplicate_alpha",
                "alpha_multibagger_role": production_baseline["alpha_multibagger_role"],
                "selection_gate_score": round(selection_gate, 4),
                "opportunity_score": round(opportunity, 4),
                "risk_score": round(risk, 4),
                "allocation_risk_score": round(allocation_risk, 4),
                "allocation_risk_penalty_mode": allocation_risk_penalty_mode,
                "discovery_risk_score": round(discovery_risk, 4),
                "discovery_risk_penalty_mode": discovery_risk_penalty_mode,
                "discovery_opportunity_score": round(discovery_opportunity, 4),
                "discovery_selection_gate_score": round(discovery_selection_gate, 4),
                "legacy_risk_score": round(legacy_risk, 4),
                "risk_penalty_input_score": round(to_float(risk_components["risk_penalty_input_score"], legacy_risk), 4),
                "predictive_risk_penalty_input_score": round(
                    to_float(risk_components["predictive_risk_penalty_input_score"], legacy_risk),
                    4,
                ),
                "uncompensated_risk_score": round(uncompensated_risk, 4),
                "compensated_risk_score": round(compensated_risk, 4),
            },
            "production_baseline": production_baseline,
            "production_selection_policy": {
                "selection_policy": selection_policy,
                "event_hard_penalty": policy_settings["event_hard_penalty"],
                "soft_weakness_penalty": policy_settings["soft_weakness_penalty"],
                "value_trap_penalty": policy_settings["value_trap_penalty"],
                "leverage_fragility_penalty": policy_settings["leverage_fragility_penalty"],
                "guidance_staleness_penalty": policy_settings["guidance_staleness_penalty"],
                "mature_defensive_penalty": policy_settings["mature_defensive_penalty"],
                "expected_return_quality_bonus": policy_settings["expected_return_quality_bonus"],
                "quality_penalty": round(quality_penalty, 4),
                "quality_bonus": round(quality_bonus, 4),
                "event_hard_reasons": event_reasons,
                "soft_weakness_reasons": soft_reasons,
                "configured_event_hard_reasons": sorted(policy_settings["event_hard_reasons"]),
                "configured_soft_weakness_reasons": sorted(policy_settings["soft_weakness_reasons"]),
            },
            "biotech_taxonomy": public_biotech_taxonomy_payload(biotech_taxonomy_payload),
            "commercial_risk_overlay": commercial_risk | {
                "enabled": as_bool(commercial_risk_settings.get("enabled", True), True),
                "penalty": round(commercial_overlay_penalty, 4),
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
                "going_concern_status": sec_liq.get("going_concern_status") or survival.get("going_concern_status", ""),
                "reverse_split_hits_2y": sec_liq.get("reverse_split_hits_2y", 0),
                "median_addv20": sec_liq.get("median_addv20", 0),
                "cash_runway_months": finite_value_or_none(survival.get("cash_runway_months")),
                "financial_survival_score": finite_value_or_none(survival.get("financial_survival_score")),
                "financial_data_quality": survival.get("data_quality", ""),
                "sec_dilution_event_count": sec_events.get("dilution_event_count", 0) if isinstance(sec_events, dict) else 0,
                "sec_negative_clinical_event_count": sec_events.get("negative_clinical_event_count", 0) if isinstance(sec_events, dict) else 0,
                "legacy_risk_score": round(legacy_risk, 4),
                "allocation_risk_score": round(allocation_risk, 4),
                "allocation_risk_penalty_mode": allocation_risk_penalty_mode,
                "discovery_risk_score": round(discovery_risk, 4),
                "discovery_risk_penalty_mode": discovery_risk_penalty_mode,
                "risk_penalty_input_score": round(to_float(risk_components["risk_penalty_input_score"], legacy_risk), 4),
                "predictive_risk_penalty_input_score": round(
                    to_float(risk_components["predictive_risk_penalty_input_score"], legacy_risk),
                    4,
                ),
                "uncompensated_risk_score": round(uncompensated_risk, 4),
                "compensated_risk_score": round(compensated_risk, 4),
                "core_structural_veto_flag": bool(core_veto_reasons),
                "core_structural_veto_reasons": "|".join(core_veto_reasons),
                "event_hard_weakness_reasons": "|".join(event_reasons),
                "soft_weakness_reasons": "|".join(soft_reasons),
                "commercial_risk_overlay_reasons": commercial_risk["commercial_risk_overlay_reasons"],
                "rank_quality_cap_reasons": "|".join(rank_quality_cap_reasons),
                "rank_quality_cap_vetoed": 1.0 if rank_cap_vetoed else 0.0,
                "rank_quality_cap_veto_reasons": "|".join(rank_quality_cap_veto_reasons),
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
                "legacy_risk_score": round(legacy_risk, 4),
                "allocation_risk_score": round(allocation_risk, 4),
                "allocation_risk_penalty_mode": allocation_risk_penalty_mode,
                "discovery_risk_score": round(discovery_risk, 4),
                "discovery_risk_penalty_mode": discovery_risk_penalty_mode,
                "risk_penalty_input_score": round(to_float(risk_components["risk_penalty_input_score"], legacy_risk), 4),
                "predictive_risk_penalty_input_score": round(
                    to_float(risk_components["predictive_risk_penalty_input_score"], legacy_risk),
                    4,
                ),
                "uncompensated_risk_score": round(uncompensated_risk, 4),
                "compensated_risk_score": round(compensated_risk, 4),
                "liquidity_risk_score": risk_components["liquidity_risk_score"],
                "financing_survival_risk_score": risk_components["financing_survival_risk_score"],
                "governance_filing_risk_score": risk_components["governance_filing_risk_score"],
                "regulatory_setback_risk_score": risk_components["regulatory_setback_risk_score"],
                "pipeline_anchor_risk_score": risk_components["pipeline_anchor_risk_score"],
                "collaborator_dependency_risk_score": risk_components["collaborator_dependency_risk_score"],
                "trial_staleness_risk_score": risk_components["trial_staleness_risk_score"],
                "risk_component_json": risk_components["risk_component_json"],
                "momentum_score": round(momentum, 4),
                "clinical_opportunity_score": round(clinical_opportunity, 4),
                "commercial_quality_score": round(commercial_quality, 4),
                "commercial_value_score": round(commercial_value, 4),
                "forward_guidance_score": round(forward_guidance_score, 4),
                "valuation_score": round(valuation_score, 4),
                "upside_capacity_score": round(upside_capacity_score, 4),
                "institutional_upside_capacity_score": round(institutional_upside_capacity_score, 4),
                "leverage_score": round(leverage_score, 4),
                "value_trap_score": round(value_trap_score, 4),
                "leverage_fragility_score": round(policy_diagnostics["leverage_fragility_score"], 4),
                "mature_defensive_score": round(mature_defensive, 4),
                "expected_return_quality_score": round(expected_return_quality, 4),
                "forward_catalyst_nearest_days": forward_catalyst_nearest_days,
                "forward_catalyst_event_type": forward_catalyst_event_type,
                "forward_catalyst_source": forward_catalyst_source,
                "forward_catalyst_source_url": forward_catalyst_source_url,
                "forward_catalyst_confidence": forward_catalyst_confidence,
                "forward_catalyst_score": round(forward_catalyst_score, 4),
                "forward_catalyst_unfiltered_score": round(forward_catalyst_unfiltered_score, 4),
                "ctgov_forward_catalyst_score": round(ctgov_forward_catalyst_score, 4),
                "ctgov_forward_catalyst_guardrail_pass": ctgov_forward_catalyst_guardrail_pass,
                "short_interest_shares": round(short_interest_shares, 4),
                "float_shares": round(float_shares, 4),
                "short_interest_pct_float": round(short_interest_pct_float, 8),
                "days_to_cover": round(days_to_cover, 4),
                "float_shares_source": float_shares_source,
                "float_shares_asof_date": float_shares_asof_date,
                "float_shares_source_asof_date": float_shares_source_asof_date,
                "float_shares_staleness_days": round(float_shares_staleness_days, 4),
                "float_shares_measurement_staleness_days": round(float_shares_measurement_staleness_days, 4),
                "float_shares_proxy_flag": float_shares_proxy_flag,
                "public_float_usd": round(public_float_usd, 4),
                "public_float_price_date": public_float_price_date,
                "public_float_close_price": round(public_float_close_price, 8),
                "short_interest_pct_float_available_flag": short_interest_pct_float_available_flag,
                "short_interest_pct_score": round(short_interest_pct_score, 4),
                "short_interest_days_to_cover_score": round(short_interest_days_to_cover_score, 4),
                "short_interest_signal_basis": short_interest_signal_basis,
                "short_interest_signal_max_possible_score": round(short_interest_signal_max_possible_score, 4),
                "short_interest_signal_score": round(short_interest_signal_score, 4),
                "commercial_entry_quality_score": commercial_expected_return_overlay[
                    "commercial_entry_quality_score"
                ],
                "commercial_overextension_score": commercial_expected_return_overlay[
                    "commercial_overextension_score"
                ],
                "valuation_growth_fit_score": commercial_expected_return_overlay["valuation_growth_fit_score"],
                "commercial_expected_return_overlay_score": commercial_expected_return_overlay[
                    "commercial_expected_return_overlay_score"
                ],
                "borrow_rate_current": round(borrow_rate_current, 8),
                "borrow_fee_data_available_flag": borrow_fee_data_available_flag,
                "shortable_data_available_flag": shortable_data_available_flag,
                "borrow_fee_stale_flag": borrow_fee_stale_flag,
                "shortable_stale_flag": shortable_stale_flag,
                "borrow_fee_staleness_days": round(borrow_fee_staleness_days, 4),
                "shortable_staleness_days": round(shortable_staleness_days, 4),
                "borrow_fee_history_count_30d": round(borrow_fee_history_count_30d, 4),
                "borrow_fee_history_count_90d": round(borrow_fee_history_count_90d, 4),
                "borrow_rate_30d_avg": round(borrow_rate_30d_avg, 8),
                "borrow_rate_90d_avg": round(borrow_rate_90d_avg, 8),
                "borrow_rate_spike_flag": borrow_rate_spike_flag,
                "borrow_rate_declining_flag": borrow_rate_declining_flag,
                "shortable_shares": round(shortable_shares, 4),
                "shares_shortable_k": round(shares_shortable_k, 4),
                "hard_to_borrow_flag": hard_to_borrow_flag,
                "borrow_pressure_score": round(borrow_pressure_score, 4),
                "high_borrow_pressure_flag": high_borrow_pressure_flag,
                "elevated_borrow_pressure_flag": elevated_borrow_pressure_flag,
                "borrow_rate_high_flag": borrow_rate_high_flag,
                "borrow_squeeze_setup_flag": borrow_squeeze_setup_flag,
                "borrow_distress_flag": borrow_distress_flag,
                "institutional_ownership_delta_pct": round(institutional_ownership_delta_pct, 8),
                "institutional_accumulation_score": round(institutional_accumulation_score, 4),
                "new_institutional_buyer_count": round(new_institutional_buyer_count, 4),
                "exiting_institutional_holder_count": round(exiting_institutional_holder_count, 4),
                "net_institutional_buyer_count": round(net_institutional_buyer_count, 4),
                "insider_buy_count_90d": round(insider_buy_count_90d, 4),
                "open_market_buy_count_90d": round(open_market_buy_count_90d, 4),
                "planned_10b5_1_buy_count": round(planned_10b5_1_buy_count, 4),
                "insider_buy_value_90d": round(insider_buy_value_90d, 2),
                "insider_buy_cluster_count_90d": round(insider_buy_cluster_count_90d, 4),
                "insider_sell_value_90d": round(insider_sell_value_90d, 2),
                "insider_accumulation_score": round(insider_accumulation_score, 4),
                "quality_adjusted_valuation_score": round(quality_adjusted_valuation_score, 4),
                "used_quality_adjusted_valuation": used_quality_adjusted_valuation,
                "valuation_quality_adjustment_delta": round(valuation_quality_adjustment_delta, 4),
                "quality_forward_valuation_score": round(quality_forward_valuation_score, 4),
                "quality_adjusted_guidance_score": round(quality_adjusted_guidance_score, 4),
                "used_quality_adjusted_guidance": used_quality_adjusted_guidance,
                "guidance_quality_adjustment_delta": round(guidance_quality_adjustment_delta, 4),
                "guidance_recency_penalty": round(guidance_recency_penalty, 4),
                "investment_score": round(investment_score, 4),
                "discovery_investment_score": round(discovery_investment_score, 4),
                "opportunity_score": round(opportunity, 4),
                "allocation_opportunity_score": round(allocation_opportunity_score, 4),
                "production_rank_score": round(production_rank_score, 4),
                "production_rank_risk_score": round(production_rank_risk_score, 4),
                "production_rank_score_field": production_score_field,
                "production_score_source": production_score_source,
                "discovery_opportunity_score": round(discovery_opportunity, 4),
                "tier1_selection_gate_score": round(selection_gate, 4),
                "discovery_selection_gate_score": round(discovery_selection_gate, 4),
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
                "discovery_clinical_opportunity_score": round(discovery_clinical_opportunity, 4),
                "discovery_clinical_risk_drag": round(discovery_clinical_risk_drag, 4),
                "discovery_investment_risk_drag": round(discovery_investment_risk_drag, 4),
                "effective_pre_confidence_risk_drag": round(effective_pre_confidence_risk_drag, 4),
                "effective_post_confidence_risk_drag": round(effective_post_confidence_risk_drag, 4),
                "effective_total_risk_drag": round(effective_total_risk_drag, 4),
                "confidence_adjusted_score_reduction": round(confidence_adjusted_score_reduction, 4),
                "commercial_risk_overlay_score": commercial_risk["commercial_risk_overlay_score"],
                "commercial_risk_overlay_flag": commercial_risk["commercial_risk_overlay_flag"],
                "commercial_risk_overlay_reasons": commercial_risk["commercial_risk_overlay_reasons"],
                "commercial_risk_overlay_penalty": round(commercial_overlay_penalty, 4),
                "production_policy_quality_penalty": round(quality_penalty, 4),
                "production_policy_quality_bonus": round(quality_bonus, 4),
                "discovery_policy_event_hard_penalty": round(discovery_event_penalty, 4),
                "discovery_policy_soft_weakness_penalty": round(discovery_soft_penalty, 4),
                "discovery_policy_quality_penalty": round(discovery_quality_penalty, 4),
                "discovery_policy_quality_bonus": round(discovery_quality_bonus, 4),
                "pre_rank_cap_opportunity_score": round(pre_rank_cap_opportunity, 4),
                "rank_quality_cap": rank_quality_cap if rank_quality_cap is not None else None,
                "rank_quality_cap_reasons": "|".join(rank_quality_cap_reasons),
                "rank_quality_cap_vetoed": 1.0 if rank_cap_vetoed else 0.0,
                "rank_quality_cap_veto_reasons": "|".join(rank_quality_cap_veto_reasons),
                "discovery_rank_quality_cap": discovery_rank_quality_cap
                if discovery_rank_quality_cap is not None
                else None,
                "discovery_rank_quality_cap_reasons": "|".join(discovery_rank_quality_cap_reasons),
                "discovery_rank_quality_cap_vetoed": 1.0 if discovery_rank_cap_vetoed else 0.0,
                "commercial_deterioration_score": commercial_risk["commercial_deterioration_score"],
                "commercial_deterioration_flag": commercial_risk["commercial_deterioration_flag"],
                "commercial_deterioration_reasons": commercial_risk["commercial_deterioration_reasons"],
                "valuation_growth_mismatch_score": commercial_risk["valuation_growth_mismatch_score"],
                "valuation_growth_mismatch_flag": commercial_risk["valuation_growth_mismatch_flag"],
                "valuation_growth_mismatch_reasons": commercial_risk["valuation_growth_mismatch_reasons"],
                "transient_revenue_anchor_score": commercial_risk["transient_revenue_anchor_score"],
                "transient_revenue_anchor_flag": commercial_risk["transient_revenue_anchor_flag"],
                "transient_revenue_anchor_reasons": commercial_risk["transient_revenue_anchor_reasons"],
                "commercial_business_shock_score": commercial_risk["commercial_business_shock_score"],
                "commercial_business_shock_flag": commercial_risk["commercial_business_shock_flag"],
                "commercial_business_shock_reasons": commercial_risk["commercial_business_shock_reasons"],
                "no_forward_guidance_flag": guidance_flags["no_forward_guidance_flag"],
                "guidance_staleness_flag": guidance_flags["guidance_staleness_flag"],
                "guidance_stale_flag": guidance_flags["guidance_stale_flag"],
                "no_guidance_negative_growth_flag": guidance_flags["no_guidance_negative_growth_flag"],
                "bucket": production_bucket,
                "allocation_bucket": allocation_bucket,
                "primary_nct": ctgov.get("primary_nct", ""),
                "primary_trial_title": ctgov.get("primary_trial_title", ""),
                "ctgov_evidence_type": ctgov_evidence_type,
                "company_strategy_category": company_strategy_category,
                "ctgov_review_bucket": ctgov_review_bucket,
                "ctgov_manual_root_cause": ctgov_manual_root_cause,
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
                "cash_runway_months": finite_value_or_none(survival.get("cash_runway_months")),
                "financial_survival_score": finite_value_or_none(survival.get("financial_survival_score")),
                "financial_data_quality": survival.get("data_quality", ""),
                "going_concern_status": sec_liq.get("going_concern_status") or survival.get("going_concern_status", ""),
                "reverse_split_hits_2y": sec_liq.get("reverse_split_hits_2y", 0),
                "sec_regulatory_catalyst_count": sec_events.get("regulatory_catalyst_count", 0) if isinstance(sec_events, dict) else 0,
                "sec_dilution_event_count": sec_events.get("dilution_event_count", 0) if isinstance(sec_events, dict) else 0,
                "sec_negative_clinical_event_count": sec_events.get("negative_clinical_event_count", 0) if isinstance(sec_events, dict) else 0,
                **biotech_taxonomy_payload,
                "top_evidence_json": json.dumps(top_evidence, ensure_ascii=True, sort_keys=True),
            }
        )
    enrich_biotech_cohort_rank_stats(scored)
    apply_biotech_cohort_policy(scored, cohort_policy)
    scored.sort(
        key=lambda item: (
            1 if production_rank_blocked(item, apply_core_veto_to_rank=apply_core_veto_to_rank) else 0,
            -clamp(to_float(item.get("allocation_opportunity_score"), to_float(item.get("opportunity_score"), 0.0))),
            clamp(to_float(item.get("allocation_risk_score"), to_float(item.get("risk_score"), 100.0))),
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
    if missing_risk_raw:
        LOGGER.warning(
            "risk_score_raw missing for %d row(s); used 0.0 fallback for sample=%s",
            len(missing_risk_raw),
            ",".join(missing_risk_raw[:10]),
        )
    if missing_momentum_raw:
        LOGGER.warning(
            "momentum_score_raw missing for %d row(s); used 0.0 fallback for sample=%s",
            len(missing_momentum_raw),
            ",".join(missing_momentum_raw[:10]),
        )
    return scored

def upsert_scores(conn: sqlite3.Connection, rows: list[dict[str, Any]], asof_date: str) -> None:
    bad_dates = sorted({str(row.get("asof_date") or "") for row in rows if str(row.get("asof_date") or "") != asof_date})
    if bad_dates:
        raise ValueError(f"upsert_scores received rows outside asof_date={asof_date}: {', '.join(bad_dates[:5])}")
    ensure_table_optional_columns(conn, "daily_scores", DAILY_SCORES_OPTIONAL_COLUMNS)
    now = utc_now()
    fields = [
        "asof_date",
        "company_id",
        "ticker",
        "company_name",
        "catalyst_score",
        "credibility_score",
        "financial_quality_score",
        "risk_score",
        "legacy_risk_score",
        "allocation_risk_score",
        "allocation_risk_penalty_mode",
        "discovery_risk_score",
        "discovery_risk_penalty_mode",
        "risk_penalty_input_score",
        "predictive_risk_penalty_input_score",
        "uncompensated_risk_score",
        "compensated_risk_score",
        "liquidity_risk_score",
        "financing_survival_risk_score",
        "governance_filing_risk_score",
        "regulatory_setback_risk_score",
        "pipeline_anchor_risk_score",
        "collaborator_dependency_risk_score",
        "trial_staleness_risk_score",
        "risk_component_json",
        "momentum_score",
        "clinical_opportunity_score",
        "commercial_quality_score",
        "commercial_value_score",
        "forward_guidance_score",
        "valuation_score",
        "upside_capacity_score",
        "institutional_upside_capacity_score",
        "leverage_score",
        "value_trap_score",
        "leverage_fragility_score",
        "mature_defensive_score",
        "expected_return_quality_score",
        "forward_catalyst_nearest_days",
        "forward_catalyst_event_type",
        "forward_catalyst_source",
        "forward_catalyst_source_url",
        "forward_catalyst_confidence",
        "forward_catalyst_score",
        "forward_catalyst_unfiltered_score",
        "ctgov_forward_catalyst_score",
        "ctgov_forward_catalyst_guardrail_pass",
        "short_interest_shares",
        "float_shares",
        "short_interest_pct_float",
        "days_to_cover",
        "float_shares_source",
        "float_shares_asof_date",
        "float_shares_source_asof_date",
        "float_shares_staleness_days",
        "float_shares_measurement_staleness_days",
        "float_shares_proxy_flag",
        "public_float_usd",
        "public_float_price_date",
        "public_float_close_price",
        "short_interest_pct_float_available_flag",
        "short_interest_pct_score",
        "short_interest_days_to_cover_score",
        "short_interest_signal_basis",
        "short_interest_signal_max_possible_score",
        "short_interest_signal_score",
        "borrow_rate_current",
        "borrow_fee_data_available_flag",
        "shortable_data_available_flag",
        "borrow_fee_stale_flag",
        "shortable_stale_flag",
        "borrow_fee_staleness_days",
        "shortable_staleness_days",
        "borrow_fee_history_count_30d",
        "borrow_fee_history_count_90d",
        "borrow_rate_30d_avg",
        "borrow_rate_90d_avg",
        "borrow_rate_spike_flag",
        "borrow_rate_declining_flag",
        "shortable_shares",
        "shares_shortable_k",
        "hard_to_borrow_flag",
        "borrow_pressure_score",
        "high_borrow_pressure_flag",
        "elevated_borrow_pressure_flag",
        "borrow_rate_high_flag",
        "borrow_squeeze_setup_flag",
        "borrow_distress_flag",
        "institutional_ownership_delta_pct",
        "institutional_accumulation_score",
        "new_institutional_buyer_count",
        "exiting_institutional_holder_count",
        "net_institutional_buyer_count",
        "insider_buy_count_90d",
        "open_market_buy_count_90d",
        "planned_10b5_1_buy_count",
        "insider_buy_value_90d",
        "insider_buy_cluster_count_90d",
        "insider_sell_value_90d",
        "insider_accumulation_score",
        "commercial_entry_quality_score",
        "commercial_overextension_score",
        "valuation_growth_fit_score",
        "commercial_expected_return_overlay_score",
        "quality_adjusted_valuation_score",
        "used_quality_adjusted_valuation",
        "valuation_quality_adjustment_delta",
        "quality_forward_valuation_score",
        "quality_adjusted_guidance_score",
        "used_quality_adjusted_guidance",
        "guidance_quality_adjustment_delta",
        "guidance_recency_penalty",
        "investment_score",
        "discovery_investment_score",
        "opportunity_score",
        "allocation_opportunity_score",
        "allocation_bucket",
        "production_rank_score",
        "production_rank_risk_score",
        "production_rank_score_field",
        "production_score_source",
        "discovery_opportunity_score",
        "tier1_selection_gate_score",
        "discovery_selection_gate_score",
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
        "discovery_clinical_opportunity_score",
        "discovery_clinical_risk_drag",
        "discovery_investment_risk_drag",
        "effective_pre_confidence_risk_drag",
        "effective_post_confidence_risk_drag",
        "effective_total_risk_drag",
        "confidence_adjusted_score_reduction",
        "commercial_risk_overlay_score",
        "commercial_risk_overlay_flag",
        "commercial_risk_overlay_reasons",
        "commercial_risk_overlay_penalty",
        "production_policy_quality_penalty",
        "production_policy_quality_bonus",
        "discovery_policy_event_hard_penalty",
        "discovery_policy_soft_weakness_penalty",
        "discovery_policy_quality_penalty",
        "discovery_policy_quality_bonus",
        "pre_rank_cap_opportunity_score",
        "rank_quality_cap",
        "rank_quality_cap_reasons",
        "rank_quality_cap_vetoed",
        "rank_quality_cap_veto_reasons",
        "discovery_rank_quality_cap",
        "discovery_rank_quality_cap_reasons",
        "discovery_rank_quality_cap_vetoed",
        "commercial_deterioration_score",
        "commercial_deterioration_flag",
        "commercial_deterioration_reasons",
        "valuation_growth_mismatch_score",
        "valuation_growth_mismatch_flag",
        "valuation_growth_mismatch_reasons",
        "transient_revenue_anchor_score",
        "transient_revenue_anchor_flag",
        "transient_revenue_anchor_reasons",
        "commercial_business_shock_score",
        "commercial_business_shock_flag",
        "commercial_business_shock_reasons",
        "no_forward_guidance_flag",
        "guidance_staleness_flag",
        "guidance_stale_flag",
        "no_guidance_negative_growth_flag",
        "rank",
        "bucket",
        "primary_nct",
        "primary_trial_title",
        "ctgov_evidence_type",
        "company_strategy_category",
        "ctgov_review_bucket",
        "ctgov_manual_root_cause",
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
        "biotech_primary_cohort",
        "biotech_secondary_cohort",
        "biotech_calibration_cohort",
        "biotech_calibration_cohort_source",
        "biotech_calibration_cohort_reason",
        "biotech_calibration_cohort_version",
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
        "biotech_cohort_evidence_json",
        "top_evidence_json",
    ]
    text_fields = {
        "asof_date",
        "ticker",
        "company_name",
        "tier1_production_score_model",
        "tier1_selection_policy",
        "alpha_multibagger_role",
        "allocation_risk_penalty_mode",
        "discovery_risk_penalty_mode",
        "allocation_bucket",
        "production_rank_score_field",
        "production_score_source",
        "forward_catalyst_event_type",
        "forward_catalyst_source",
        "forward_catalyst_source_url",
        "float_shares_source",
        "float_shares_asof_date",
        "float_shares_source_asof_date",
        "public_float_price_date",
        "core_structural_veto_reasons",
        "bucket",
        "primary_nct",
        "primary_trial_title",
        "ctgov_evidence_type",
        "company_strategy_category",
        "ctgov_review_bucket",
        "ctgov_manual_root_cause",
        "commercial_risk_overlay_reasons",
        "rank_quality_cap_reasons",
        "rank_quality_cap_veto_reasons",
        "discovery_rank_quality_cap_reasons",
        "commercial_deterioration_reasons",
        "valuation_growth_mismatch_reasons",
        "transient_revenue_anchor_reasons",
        "commercial_business_shock_reasons",
        "financial_data_quality",
        "going_concern_status",
        "biotech_primary_cohort",
        "biotech_secondary_cohort",
        "biotech_calibration_cohort",
        "biotech_calibration_cohort_source",
        "biotech_calibration_cohort_reason",
        "biotech_calibration_cohort_version",
        "biotech_cohort_reason_codes",
        "biotech_cohort_source",
        "biotech_cohort_overlays",
        "biotech_cohort_data_quality",
        "biotech_cohort_calibration_mode",
        "biotech_cohort_exclusion_reason",
        "biotech_cohort_model_version",
        "biotech_cohort_evidence_json",
        "risk_component_json",
        "top_evidence_json",
    }

    def db_value(row: dict[str, Any], field: str) -> Any:
        if field in row:
            return row[field]
        return "" if field in text_fields else None

    params = [tuple(db_value(row, field) for field in fields) + (now, now) for row in rows]
    placeholders = ", ".join("?" for _ in [*fields, "created_at", "updated_at"])
    update_fields = [field for field in fields if field not in {"asof_date", "company_id"}]
    update_clause = ", ".join(f"{field} = excluded.{field}" for field in [*update_fields, "updated_at"])
    with conn:
        conn.execute("DELETE FROM daily_scores WHERE asof_date = ?", (asof_date,))
        conn.executemany(
            f"""
            INSERT INTO daily_scores(
                {", ".join(fields)}, created_at, updated_at
            )
            VALUES ({placeholders})
            ON CONFLICT(asof_date, company_id) DO UPDATE SET
                {update_clause}
            """,
            params,
        )


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    market_positioning_fieldnames = [
        "forward_catalyst_nearest_days",
        "forward_catalyst_event_type",
        "forward_catalyst_source",
        "forward_catalyst_source_url",
        "forward_catalyst_confidence",
        "forward_catalyst_score",
        "forward_catalyst_unfiltered_score",
        "ctgov_forward_catalyst_score",
        "ctgov_forward_catalyst_guardrail_pass",
        "short_interest_shares",
        "float_shares",
        "short_interest_pct_float",
        "days_to_cover",
        "float_shares_source",
        "float_shares_asof_date",
        "float_shares_source_asof_date",
        "float_shares_staleness_days",
        "float_shares_measurement_staleness_days",
        "float_shares_proxy_flag",
        "public_float_usd",
        "public_float_price_date",
        "public_float_close_price",
        "short_interest_pct_float_available_flag",
        "short_interest_pct_score",
        "short_interest_days_to_cover_score",
        "short_interest_signal_basis",
        "short_interest_signal_max_possible_score",
        "short_interest_signal_score",
        "borrow_rate_current",
        "borrow_fee_data_available_flag",
        "shortable_data_available_flag",
        "borrow_fee_stale_flag",
        "shortable_stale_flag",
        "borrow_fee_staleness_days",
        "shortable_staleness_days",
        "borrow_fee_history_count_30d",
        "borrow_fee_history_count_90d",
        "borrow_rate_30d_avg",
        "borrow_rate_90d_avg",
        "borrow_rate_spike_flag",
        "borrow_rate_declining_flag",
        "shortable_shares",
        "shares_shortable_k",
        "hard_to_borrow_flag",
        "borrow_pressure_score",
        "high_borrow_pressure_flag",
        "elevated_borrow_pressure_flag",
        "borrow_rate_high_flag",
        "borrow_squeeze_setup_flag",
        "borrow_distress_flag",
        "institutional_ownership_delta_pct",
        "institutional_accumulation_score",
        "new_institutional_buyer_count",
        "exiting_institutional_holder_count",
        "net_institutional_buyer_count",
        "insider_buy_count_90d",
        "open_market_buy_count_90d",
        "planned_10b5_1_buy_count",
        "insider_buy_value_90d",
        "insider_buy_cluster_count_90d",
        "insider_sell_value_90d",
        "insider_accumulation_score",
    ]
    fieldnames = [
        "asof_date",
        "rank",
        "company_id",
        "ticker",
        "company_name",
        "bucket",
        "opportunity_score",
        "allocation_opportunity_score",
        "allocation_bucket",
        "production_rank_score",
        "production_rank_risk_score",
        "production_rank_score_field",
        "production_score_source",
        "discovery_opportunity_score",
        "investment_score",
        "discovery_investment_score",
        "biotech_primary_cohort",
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
        "discovery_clinical_opportunity_score",
        "tier1_selection_gate_score",
        "discovery_selection_gate_score",
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
        "discovery_clinical_risk_drag",
        "discovery_investment_risk_drag",
        "effective_pre_confidence_risk_drag",
        "effective_post_confidence_risk_drag",
        "effective_total_risk_drag",
        "confidence_adjusted_score_reduction",
        "commercial_risk_overlay_score",
        "commercial_risk_overlay_flag",
        "commercial_risk_overlay_reasons",
        "commercial_risk_overlay_penalty",
        "production_policy_quality_penalty",
        "production_policy_quality_bonus",
        "discovery_policy_event_hard_penalty",
        "discovery_policy_soft_weakness_penalty",
        "discovery_policy_quality_penalty",
        "discovery_policy_quality_bonus",
        "pre_rank_cap_opportunity_score",
        "rank_quality_cap",
        "rank_quality_cap_reasons",
        "rank_quality_cap_vetoed",
        "rank_quality_cap_veto_reasons",
        "discovery_rank_quality_cap",
        "discovery_rank_quality_cap_reasons",
        "discovery_rank_quality_cap_vetoed",
        "commercial_deterioration_score",
        "commercial_deterioration_flag",
        "commercial_deterioration_reasons",
        "valuation_growth_mismatch_score",
        "valuation_growth_mismatch_flag",
        "valuation_growth_mismatch_reasons",
        "transient_revenue_anchor_score",
        "transient_revenue_anchor_flag",
        "transient_revenue_anchor_reasons",
        "commercial_business_shock_score",
        "commercial_business_shock_flag",
        "commercial_business_shock_reasons",
        "no_forward_guidance_flag",
        "guidance_staleness_flag",
        "guidance_stale_flag",
        "no_guidance_negative_growth_flag",
        "commercial_value_score",
        "commercial_quality_score",
        "forward_guidance_score",
        "valuation_score",
        "upside_capacity_score",
        "institutional_upside_capacity_score",
        "leverage_score",
        "value_trap_score",
        "leverage_fragility_score",
        "mature_defensive_score",
        "expected_return_quality_score",
        "commercial_entry_quality_score",
        "commercial_overextension_score",
        "valuation_growth_fit_score",
        "commercial_expected_return_overlay_score",
        *market_positioning_fieldnames,
        "quality_adjusted_valuation_score",
        "used_quality_adjusted_valuation",
        "valuation_quality_adjustment_delta",
        "quality_forward_valuation_score",
        "quality_adjusted_guidance_score",
        "used_quality_adjusted_guidance",
        "guidance_quality_adjustment_delta",
        "guidance_recency_penalty",
        "catalyst_score",
        "credibility_score",
        "financial_quality_score",
        "risk_score",
        "legacy_risk_score",
        "allocation_risk_score",
        "allocation_risk_penalty_mode",
        "discovery_risk_score",
        "discovery_risk_penalty_mode",
        "risk_penalty_input_score",
        "predictive_risk_penalty_input_score",
        "uncompensated_risk_score",
        "compensated_risk_score",
        "liquidity_risk_score",
        "financing_survival_risk_score",
        "governance_filing_risk_score",
        "regulatory_setback_risk_score",
        "pipeline_anchor_risk_score",
        "collaborator_dependency_risk_score",
        "trial_staleness_risk_score",
        "risk_component_json",
        "momentum_score",
        "primary_nct",
        "primary_trial_title",
        "ctgov_evidence_type",
        "company_strategy_category",
        "ctgov_review_bucket",
        "ctgov_manual_root_cause",
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
    taxonomy_overrides_path = resolve_path(
        cfg_get(config, "biotech_scoring.taxonomy_overrides_csv", "data/biotech_taxonomy_overrides.csv"),
        base_dir=base_dir,
    )
    calibration_cohort_settings = cfg_get(config, "biotech_scoring.calibration_cohorts", {}) or {}
    if not isinstance(calibration_cohort_settings, dict):
        calibration_cohort_settings = {}
    calibration_cohorts_path = resolve_path(
        calibration_cohort_settings.get("csv", "data/biotech_calibration_cohorts.csv"),
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
            require_governance_features = as_bool(
                cfg_get(config, "biotech_scoring.require_governance_features", False),
                False,
            )
            missing_governance = [str(row["ticker"]) for row in features if int(row["company_id"]) not in governance_by_company]
            if missing_governance:
                message = (
                    "biotech scoring missing governance_event_features_daily row(s): "
                    + ",".join(sorted(missing_governance)[:25])
                    + (f"...(+{len(missing_governance) - 25})" if len(missing_governance) > 25 else "")
                )
                if require_governance_features:
                    raise RuntimeError(message)
                LOGGER.warning("%s; continuing with empty governance diagnostics for missing companies.", message)
            max_upstream_staleness_days = int(cfg_get(config, "biotech_refresh.max_upstream_staleness_days", 2))
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
            if require_governance_features:
                validate_layer_freshness(
                    base_rows=features,
                    layer_rows_by_company=governance_by_company,
                    asof_date=asof_date,
                    context="biotech scoring governance_event_features_daily",
                    max_staleness_days=max_upstream_staleness_days,
                )
            elif governance_by_company:
                governance_base_rows = [
                    row for row in features if int(row["company_id"]) in governance_by_company
                ]
                validate_layer_freshness(
                    base_rows=governance_base_rows,
                    layer_rows_by_company=governance_by_company,
                    asof_date=asof_date,
                    context="biotech scoring optional governance_event_features_daily",
                    max_staleness_days=max_upstream_staleness_days,
                )
            else:
                LOGGER.warning("Skipping optional governance freshness validation because no governance rows are available.")
            calibration_cohorts_by_ticker = (
                load_calibration_cohort_overrides(calibration_cohorts_path)
                if as_bool(calibration_cohort_settings.get("enabled", False), False)
                else {}
            )
            if calibration_cohorts_by_ticker:
                LOGGER.info(
                    "Loaded biotech calibration cohort overrides: tickers=%d path=%s",
                    len(calibration_cohorts_by_ticker),
                    calibration_cohorts_path,
                )
            taxonomy_overrides_by_ticker: dict[str, list[TaxonomyOverride]] = {}
            if calibration_cohorts_by_ticker:
                LOGGER.info(
                    "Skipping legacy biotech taxonomy overrides because official five-bucket cohorts are enabled."
                )
            else:
                taxonomy_overrides_by_ticker = load_taxonomy_overrides(taxonomy_overrides_path)
                if taxonomy_overrides_by_ticker:
                    LOGGER.info(
                        "Loaded biotech taxonomy overrides: tickers=%d path=%s",
                        len(taxonomy_overrides_by_ticker),
                        taxonomy_overrides_path,
                    )
            scored = score_rows(
                features,
                config,
                commercial_by_company,
                forward_by_company,
                governance_by_company,
                taxonomy_overrides_by_ticker,
                calibration_cohorts_by_ticker,
            )
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

