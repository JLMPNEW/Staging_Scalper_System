#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import logging
import math
import sqlite3
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, cast

import pandas as pd


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from biotech_index.core.config import cfg_get, load_yaml, resolve_optional_path, resolve_path  # noqa: E402
from biotech_index.core.db import (  # noqa: E402
    DAILY_FEATURES_OPTIONAL_COLUMNS,
    connect,
    ensure_table_optional_columns,
    finish_run,
    init_db,
    start_run,
    utc_now,
)
from biotech_index.core.logging_utils import configure_utc_logging  # noqa: E402
from biotech_index.core.market_policy import scoring_market_sources, select_latest_rows_by_source_priority  # noqa: E402
from biotech_index.core.pipeline_guards import (  # noqa: E402
    normalize_ticker,
    validate_full_universe_coverage,
    validate_layer_freshness,
    validate_nonempty_selection,
)
from biotech_index.core.scoring_math import (  # noqa: E402
    decomposed_risk_penalty_input,
    weighted_predictive_risk_penalty_input,
)


LOGGER = logging.getLogger("build_biotech_features")
DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
FEATURE_CSV_FIELDNAMES = [
    "asof_date",
    "company_id",
    "ticker",
    "company_name",
    "catalyst_score_raw",
    "credibility_score_raw",
    "financial_quality_score_raw",
    "risk_score_raw",
    "legacy_risk_score_raw",
    "risk_penalty_input_score_raw",
    "predictive_risk_penalty_input_score_raw",
    "uncompensated_risk_score_raw",
    "compensated_risk_score_raw",
    "liquidity_risk_score_raw",
    "financing_survival_risk_score_raw",
    "governance_filing_risk_score_raw",
    "regulatory_setback_risk_score_raw",
    "pipeline_anchor_risk_score_raw",
    "collaborator_dependency_risk_score_raw",
    "trial_staleness_risk_score_raw",
    "momentum_score_raw",
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
    "active_lead_sponsor_trials",
    "active_program_override_trials",
    "active_collaborator_trials",
    "median_addv20",
    "going_concern_status",
    "reverse_split_hits_2y",
    "sec_regulatory_catalyst_count",
    "sec_dilution_event_count",
    "sec_negative_clinical_event_count",
    "sec_catalyst_raw_score",
    "sec_catalyst_recency_adjusted_score",
    "sec_catalyst_score_used",
    "sec_catalyst_decay_delta",
    "sec_catalyst_latest_event_type",
    "sec_catalyst_latest_filing_date",
    "sec_catalyst_latest_event_date",
    "sec_catalyst_recency_days",
    "sec_catalyst_recency_basis",
    "sec_catalyst_event_types",
    "indication_success_area",
    "indication_success_probability",
    "indication_success_multiplier",
    "indication_weighted_phase2_3_component",
    "forward_catalyst_nearest_days",
    "forward_catalyst_event_type",
    "forward_catalyst_score",
    "short_interest_pct_float",
    "short_interest_signal_score",
    "institutional_ownership_delta_pct",
    "institutional_accumulation_score",
    "insider_buy_count_90d",
    "insider_buy_value_90d",
    "insider_buy_cluster_count_90d",
    "insider_sell_value_90d",
    "insider_accumulation_score",
    "manual_verdict",
    "feature_json",
]

CATALYST_COMPONENT_MAX = {
    "verified_active_trials": 18.0,
    "effective_phase2_3_trials": 30.0,
    "active_pivotal_trials": 18.0,
    "active_lead_sponsor_trials": 15.0,
    "active_program_override_trials": 10.0,
    "pipeline_density": 10.0,
    "core_pipeline_quality": 18.0,
    "recent_trial_update": 8.0,
    "regulatory_or_positive_clinical_event": 25.0,
}
CATALYST_POSITIVE_MAX = sum(CATALYST_COMPONENT_MAX.values())
SEC_CATALYST_EVENT_WEIGHTS = {
    "pdufa_date": 18.0,
    "nda_bla_accepted": 16.0,
    "regulatory_submission": 7.0,
    "endpoint_met": 10.0,
    "clinical_update_positive": 5.0,
}
DEFAULT_PIPELINE_QUALITY_SETTINGS = {
    "effective_program_phase23_weight": 0.75,
    "effective_collaborator_phase23_weight": 0.25,
    "lead_phase23_cap": 35.0,
    "lead_phase23_weight": 7.0,
    "program_phase23_cap": 18.0,
    "program_phase23_weight": 6.0,
    "lead_active_cap": 15.0,
    "lead_active_weight": 3.0,
    "pivotal_core_cap": 12.0,
    "pivotal_core_weight": 6.0,
    "pipeline_density_cap": 10.0,
    "pipeline_density_weight": 10.0,
    "collab_phase23_cap": 5.0,
    "collab_phase23_weight": 0.5,
    "collab_penalty_threshold": 0.50,
    "collaborator_heavy_threshold": 0.60,
    "core_pipeline_quality_multiplier": 0.18,
    "collab_penalty_cap": 25.0,
    "collab_penalty_weight": 50.0,
}

DIAGNOSTICS_SERVICE_CATEGORIES = frozenset(
    {
        "clinical_research_services",
        "molecular_diagnostics",
        "molecular_diagnostics_laboratory",
        "precision_medicine_diagnostics",
        "life_science_tools_services",
        "sterilization_services",
    }
)
DEVICE_CATEGORIES = frozenset(
    {
        "diabetes_device",
        "medical_device_infrastructure",
        "medical_device_therapeutic_platform",
        "post_market_device_services",
        "surgical_regenerative_devices",
        "surgical_robotics_device",
        "non_therapeutic_device_removed",
    }
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build daily Tier-1 biotech index features.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--asof", type=str, default="", help="Feature date in YYYY-MM-DD. Defaults to UTC today.")
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


def decay_multiplier_for_date(event_date: object, asof_date: date, *, half_life_days: float) -> tuple[float, int | None]:
    parsed = parse_date(event_date)
    if parsed is None:
        return 0.0, None
    age_days = max(0, (asof_date - parsed).days)
    if half_life_days <= 0.0:
        return 1.0, age_days
    return math.pow(0.5, age_days / half_life_days), age_days


def sec_catalyst_event_multiplier(
    *,
    event_type: str,
    filing_date: object,
    event_date: object,
    asof_date: date,
    half_life_days: float,
) -> tuple[float, int | None, str]:
    parsed_event_date = parse_date(event_date)
    if event_type == "pdufa_date" and parsed_event_date is not None and parsed_event_date >= asof_date:
        days_until = (parsed_event_date - asof_date).days
        if half_life_days <= 0.0:
            return 1.0, days_until, "pdufa_event_date_proximity"
        return math.pow(0.5, days_until / half_life_days), days_until, "pdufa_event_date_proximity"
    multiplier, age_days = decay_multiplier_for_date(
        filing_date,
        asof_date,
        half_life_days=half_life_days,
    )
    return multiplier, age_days, "filing_age"


def to_float(raw: object, default: float = 0.0) -> float:
    if raw is None:
        return default
    text = str(raw).strip()
    if not text or text.lower() in {"nan", "none", "null"}:
        return default
    try:
        value = float(text)
    except ValueError:
        return default
    if math.isnan(value) or math.isinf(value):
        return default
    return value


def finite_or_none(value: float | None) -> float | None:
    return value if value is not None and math.isfinite(value) else None


def to_int(raw: object, default: int = 0) -> int:
    return int(round(to_float(raw, float(default))))


def optional_market_int(raw: object, *, field: str, ticker: object) -> int | None:
    if raw is None:
        return None
    if isinstance(raw, bool):
        return int(raw)
    text = str(raw).strip()
    if not text:
        return None
    lowered = text.lower()
    if lowered in {"true", "yes", "y", "on"}:
        return 1
    if lowered in {"false", "no", "n", "off"}:
        return 0
    try:
        value = float(text)
    except ValueError:
        LOGGER.warning("Ignoring non-numeric market %s for %s: %r", field, ticker, raw)
        return None
    if math.isnan(value) or math.isinf(value):
        LOGGER.warning("Ignoring non-finite market %s for %s: %r", field, ticker, raw)
        return None
    return int(round(value))


def as_bool(raw: object) -> bool:
    return str(raw or "").strip().lower() in {"1", "true", "yes", "y"}


def read_optional_csv(path: Path | None) -> pd.DataFrame:
    if path is None or not path.exists():
        return pd.DataFrame()
    return pd.read_csv(path, dtype=str).fillna("")


def apply_trial_status_overrides(evidence_df: pd.DataFrame, overrides_df: pd.DataFrame) -> pd.DataFrame:
    if evidence_df.empty or overrides_df.empty:
        return evidence_df
    out = evidence_df.copy()
    for column in [
        "outcome_override_applied",
        "outcome_override_status",
        "outcome_override_reason",
        "outcome_override_source_url",
        "outcome_override_manual_review",
    ]:
        if column not in out.columns:
            out[column] = ""

    for override in overrides_df.to_dict("records"):
        if not as_bool(override.get("enabled", "true")):
            continue
        ticker = str(override.get("ticker") or "").strip().upper()
        nct_id = str(override.get("nct_id") or "").strip().upper()
        if not ticker or not nct_id:
            continue
        mask = out["ticker"].astype(str).str.upper().eq(ticker) & out["nct_id"].astype(str).str.upper().eq(nct_id)
        if not mask.any():
            continue
        status = str(override.get("override_status") or "").strip()
        reason = str(override.get("override_reason") or "").strip()
        source_url = str(override.get("source_url") or "").strip()
        out.loc[mask, "outcome_override_applied"] = "True"
        out.loc[mask, "outcome_override_status"] = status
        out.loc[mask, "outcome_override_reason"] = reason
        out.loc[mask, "outcome_override_source_url"] = source_url
        out.loc[mask, "outcome_override_manual_review"] = "True" if as_bool(override.get("manual_review")) else "False"
        if as_bool(override.get("exclude_from_scoring")):
            out.loc[mask, "is_active_status"] = "False"
            out.loc[mask, "is_therapeutic"] = "False"
            out.loc[mask, "qualifying_trial"] = "False"
            out.loc[mask, "trial_score"] = "0.0"
            existing = out.loc[mask, "exclusion_reasons"].astype(str)
            suffix = f"outcome_override:{status}" if status else "outcome_override"
            out.loc[mask, "exclusion_reasons"] = existing.map(lambda value: ";".join(part for part in [value, suffix] if part))
    return out


def roles_contain(raw: object, role: str) -> bool:
    return role in {part.strip().lower() for part in str(raw or "").split(";") if part.strip()}


def clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, value))


def read_csv(path: Path) -> pd.DataFrame:
    if not path.exists():
        raise FileNotFoundError(path)
    return pd.read_csv(path, dtype=str).fillna("")


def bounded_float(raw: object, default: float, *, low: float | None = None, high: float | None = None) -> float:
    value = to_float(raw, default)
    if low is not None:
        value = max(low, value)
    if high is not None:
        value = min(high, value)
    return value


DEFAULT_STRUCTURAL_RISK_WEIGHTS = {
    "liquidity": 0.18,
    "financing_survival": 0.28,
    "governance_filing": 0.18,
    "regulatory_setback": 0.18,
    "pipeline_anchor": 0.12,
    "data_quality": 0.06,
}
DEFAULT_COMPENSATED_RISK_WEIGHTS = {
    "clinical_binary": 0.35,
    "collaborator_dependency": 0.30,
    "trial_staleness": 0.20,
    "dilution_optional": 0.15,
}
DEFAULT_PREDICTIVE_RISK_PENALTY_WEIGHTS = {
    "liquidity": 0.34,
    "pipeline_anchor": 0.28,
    "collaborator_dependency": 0.18,
    "trial_staleness": 0.15,
    "data_quality": 0.05,
    "financing_survival": 0.0,
    "governance_filing": 0.0,
    "regulatory_setback": 0.0,
    "clinical_binary": 0.0,
    "dilution_optional": 0.0,
}
DEFAULT_PREDICTIVE_RISK_FREE_BANDS = {
    "liquidity": 0.0,
    "pipeline_anchor": 10.0,
    "collaborator_dependency": 20.0,
    "trial_staleness": 10.0,
    "data_quality": 0.0,
}
DEFAULT_INDICATION_SUCCESS_RATES = {
    "oncology": {"phase2": 0.22, "phase3": 0.48},
    "cns": {"phase2": 0.14, "phase3": 0.42},
    "autoimmune_inflammatory": {"phase2": 0.28, "phase3": 0.58},
    "rare_genetic": {"phase2": 0.24, "phase3": 0.55},
    "cardiometabolic": {"phase2": 0.30, "phase3": 0.62},
    "infectious_disease": {"phase2": 0.32, "phase3": 0.64},
    "ophthalmology": {"phase2": 0.27, "phase3": 0.57},
    "device_diagnostics": {"phase2": 0.35, "phase3": 0.70},
    "general": {"phase2": 0.25, "phase3": 0.55},
}


def load_indication_success_settings(config: dict[str, Any]) -> dict[str, Any]:
    raw = cfg_get(config, "biotech_features.indication_success_weighting", {}) or {}
    if not isinstance(raw, dict):
        raw = {}
    configured_rates = raw.get("phase_success_rates") if isinstance(raw.get("phase_success_rates"), dict) else {}
    rates: dict[str, dict[str, float]] = {}
    for area, defaults in DEFAULT_INDICATION_SUCCESS_RATES.items():
        area_raw = configured_rates.get(area, {}) if isinstance(configured_rates.get(area), dict) else {}
        rates[area] = {
            "phase2": bounded_float(area_raw.get("phase2"), defaults["phase2"], low=0.01, high=0.99),
            "phase3": bounded_float(area_raw.get("phase3"), defaults["phase3"], low=0.01, high=0.99),
        }
    return {
        "enabled": as_bool(raw.get("enabled", True)),
        "apply_to_catalyst": as_bool(raw.get("apply_to_catalyst", False)),
        "default_area": str(raw.get("default_area") or "general").strip().lower() or "general",
        "min_multiplier": bounded_float(raw.get("min_multiplier"), 0.70, low=0.10, high=5.0),
        "max_multiplier": bounded_float(raw.get("max_multiplier"), 1.30, low=0.10, high=5.0),
        "phase_success_rates": rates,
    }


def normalize_pct_decimal(raw: object, default: float | None = None) -> float | None:
    text = str(raw if raw is not None else "").strip()
    if not text or text.lower() in {"nan", "none", "null"}:
        return default
    try:
        value = float(text)
    except ValueError:
        return default
    if not math.isfinite(value):
        return default
    return value / 100.0 if abs(value) > 2.0 else value


def linear_score(value: float, points: list[tuple[float, float]]) -> float:
    ordered = sorted(points)
    if value <= ordered[0][0]:
        return clamp(ordered[0][1])
    if value >= ordered[-1][0]:
        return clamp(ordered[-1][1])
    for (left_x, left_y), (right_x, right_y) in zip(ordered, ordered[1:]):
        if left_x <= value <= right_x:
            span = max(1e-12, right_x - left_x)
            return clamp(left_y + (right_y - left_y) * (value - left_x) / span)
    return clamp(ordered[-1][1])


def infer_indication_success_area(
    *,
    universe_row: Any,
    evidence: dict[str, Any],
    strategy_category: str,
    default_area: str,
) -> str:
    if strategy_category in DEVICE_CATEGORIES or strategy_category in DIAGNOSTICS_SERVICE_CATEGORIES:
        return "device_diagnostics"
    text_parts = [
        strategy_category,
        str(universe_row.get("company_name") or ""),
        str(universe_row.get("primary_trial_title") or ""),
        str(universe_row.get("manual_root_cause") or universe_row.get("root_cause_category") or ""),
    ]
    top_ncts = evidence.get("top_ncts", [])
    if isinstance(top_ncts, list):
        text_parts.extend(str(item.get("title") or "") for item in top_ncts if isinstance(item, dict))
    haystack = " ".join(text_parts).lower()
    keyword_map = [
        ("oncology", ["cancer", "oncolog", "tumor", "carcinoma", "lymphoma", "leukemia", "myeloma"]),
        ("cns", ["alzheimer", "parkinson", "cns", "depression", "epilepsy", "schizophrenia", "neuro"]),
        ("autoimmune_inflammatory", ["autoimmune", "inflamm", "arthritis", "psoriasis", "crohn", "ulcerative", "ibd"]),
        ("rare_genetic", ["rare", "orphan", "genetic", "duchenne", "hemophilia", "sickle", "thalassemia"]),
        ("cardiometabolic", ["diabetes", "obesity", "cardio", "heart", "renal", "kidney", "nash", "mash"]),
        ("infectious_disease", ["infection", "infectious", "antibiotic", "antiviral", "vaccine", "covid", "influenza"]),
        ("ophthalmology", ["ophthalm", "retina", "macular", "glaucoma"]),
    ]
    for area, keywords in keyword_map:
        if any(keyword in haystack for keyword in keywords):
            return area
    return default_area if default_area in DEFAULT_INDICATION_SUCCESS_RATES else "general"


def indication_success_probability(
    *,
    area: str,
    active_phase3_trials: int,
    active_pivotal_trials: int,
    active_phase2_trials: int,
    phase2_3_trials: int,
    settings: dict[str, Any],
) -> tuple[float, float, float]:
    rates = settings.get("phase_success_rates", DEFAULT_INDICATION_SUCCESS_RATES)
    if not isinstance(rates, dict):
        rates = DEFAULT_INDICATION_SUCCESS_RATES
    area_rates = rates.get(area) if isinstance(rates.get(area), dict) else rates.get("general", {})
    general_rates = rates.get("general", DEFAULT_INDICATION_SUCCESS_RATES["general"])
    phase3_rate = bounded_float(area_rates.get("phase3"), general_rates["phase3"], low=0.01, high=0.99)
    phase2_rate = bounded_float(area_rates.get("phase2"), general_rates["phase2"], low=0.01, high=0.99)
    general_phase3 = bounded_float(general_rates.get("phase3"), DEFAULT_INDICATION_SUCCESS_RATES["general"]["phase3"], low=0.01, high=0.99)
    general_phase2 = bounded_float(general_rates.get("phase2"), DEFAULT_INDICATION_SUCCESS_RATES["general"]["phase2"], low=0.01, high=0.99)
    phase3_count = max(0, active_phase3_trials + active_pivotal_trials)
    phase2_count = max(0, active_phase2_trials, phase2_3_trials - phase3_count)
    total = phase2_count + phase3_count
    if total <= 0:
        return phase2_rate, general_phase2, 1.0
    probability = (phase3_count * phase3_rate + phase2_count * phase2_rate) / total
    baseline = (phase3_count * general_phase3 + phase2_count * general_phase2) / total
    multiplier = probability / max(0.01, baseline)
    return probability, baseline, multiplier


def load_ticker_feature_csv(path: Path | None) -> dict[str, dict[str, Any]]:
    df = read_optional_csv(path)
    if df.empty:
        return {}
    out: dict[str, dict[str, Any]] = {}
    for record in cast(list[dict[str, Any]], cast(Any, df).to_dict("records")):
        ticker = normalize_ticker(record.get("ticker") or record.get("symbol"))
        if ticker:
            out[ticker] = record
    return out


def load_forward_catalyst_calendar(path: Path | None, asof_date: date, *, lookahead_days: int) -> dict[str, dict[str, Any]]:
    df = read_optional_csv(path)
    if df.empty:
        return {}
    out: dict[str, dict[str, Any]] = {}
    for record in cast(list[dict[str, Any]], cast(Any, df).to_dict("records")):
        ticker = normalize_ticker(record.get("ticker") or record.get("symbol"))
        event_date = parse_date(record.get("event_date") or record.get("catalyst_date") or record.get("date"))
        if not ticker or event_date is None:
            continue
        days_until = (event_date - asof_date).days
        if days_until < 0 or days_until > lookahead_days:
            continue
        candidate = {**record, "event_date": event_date.isoformat(), "days_until": days_until}
        current = out.get(ticker)
        if current is None or days_until < to_int(current.get("days_until"), 999999):
            out[ticker] = candidate
    return out


def forward_catalyst_signal(row: dict[str, Any] | None, *, lookahead_days: int) -> dict[str, Any]:
    if not row:
        return {
            "forward_catalyst_nearest_days": "",
            "forward_catalyst_event_type": "",
            "forward_catalyst_score": 0.0,
        }
    days_until = to_int(row.get("days_until"), 999999)
    event_type = str(row.get("event_type") or row.get("catalyst_type") or "catalyst").strip().lower()
    confidence = normalize_pct_decimal(row.get("confidence") or row.get("confidence_pct"), 0.65) or 0.65
    proximity = max(0.0, 1.0 - min(days_until, lookahead_days) / max(1.0, float(lookahead_days)))
    type_multiplier = 1.0
    if any(token in event_type for token in ["pdufa", "approval", "phase 3", "phase3", "pivotal", "topline"]):
        type_multiplier = 1.15
    elif any(token in event_type for token in ["phase 1", "phase1", "preclinical"]):
        type_multiplier = 0.75
    return {
        "forward_catalyst_nearest_days": days_until,
        "forward_catalyst_event_type": event_type,
        "forward_catalyst_score": round(clamp(100.0 * proximity * clamp(confidence, 0.0, 1.0) * type_multiplier), 4),
    }


def short_interest_signal(row: dict[str, Any] | None) -> dict[str, float]:
    if not row:
        return {"short_interest_pct_float": 0.0, "short_interest_signal_score": 0.0}
    short_pct = normalize_pct_decimal(
        row.get("short_interest_pct_float")
        or row.get("short_percent_float")
        or row.get("short_interest_pct")
        or row.get("short_interest_percent_float"),
        0.0,
    ) or 0.0
    days_to_cover = to_float(row.get("days_to_cover") or row.get("short_ratio"), 0.0)
    pct_score = linear_score(short_pct, [(0.0, 0.0), (0.05, 20.0), (0.10, 50.0), (0.20, 78.0), (0.35, 100.0)])
    cover_score = linear_score(days_to_cover, [(0.0, 0.0), (2.0, 25.0), (5.0, 60.0), (10.0, 100.0)])
    return {
        "short_interest_pct_float": round(short_pct, 6),
        "short_interest_signal_score": round(clamp(0.75 * pct_score + 0.25 * cover_score), 4),
    }


def institutional_ownership_signal(row: dict[str, Any] | None) -> dict[str, float]:
    if not row:
        return {"institutional_ownership_delta_pct": 0.0, "institutional_accumulation_score": 50.0}
    delta = normalize_pct_decimal(
        row.get("institutional_ownership_delta_pct")
        or row.get("ownership_delta_pct")
        or row.get("thirteen_f_ownership_delta_pct")
        or row.get("13f_ownership_delta_pct"),
        0.0,
    ) or 0.0
    score = linear_score(delta, [(-0.20, 0.0), (-0.10, 20.0), (0.0, 50.0), (0.05, 70.0), (0.15, 92.0), (0.30, 100.0)])
    return {
        "institutional_ownership_delta_pct": round(delta, 6),
        "institutional_accumulation_score": round(score, 4),
    }


def insider_activity_signal(row: dict[str, Any] | None) -> dict[str, float]:
    if not row:
        return {
            "insider_buy_count_90d": 0.0,
            "insider_buy_value_90d": 0.0,
            "insider_buy_cluster_count_90d": 0.0,
            "insider_sell_value_90d": 0.0,
            "insider_accumulation_score": 50.0,
        }
    buy_count = to_float(row.get("insider_buy_count_90d"), 0.0)
    buy_value = to_float(row.get("insider_buy_value_90d"), 0.0)
    cluster_count = to_float(row.get("insider_buy_cluster_count_90d"), 0.0)
    sell_value = to_float(row.get("insider_sell_value_90d"), 0.0)
    buy_count_score = linear_score(buy_count, [(0.0, 0.0), (1.0, 35.0), (3.0, 75.0), (6.0, 100.0)])
    buy_value_score = linear_score(math.log10(max(1.0, buy_value)), [(0.0, 0.0), (5.0, 35.0), (6.0, 65.0), (7.0, 100.0)])
    cluster_score = linear_score(cluster_count, [(0.0, 0.0), (1.0, 55.0), (2.0, 85.0), (4.0, 100.0)])
    sell_penalty = linear_score(math.log10(max(1.0, sell_value)), [(0.0, 0.0), (5.0, 10.0), (6.0, 25.0), (7.0, 45.0)])
    score = 50.0 + 0.18 * buy_count_score + 0.18 * buy_value_score + 0.22 * cluster_score - sell_penalty
    return {
        "insider_buy_count_90d": round(buy_count, 4),
        "insider_buy_value_90d": round(buy_value, 2),
        "insider_buy_cluster_count_90d": round(cluster_count, 4),
        "insider_sell_value_90d": round(sell_value, 2),
        "insider_accumulation_score": round(clamp(score), 4),
    }


def load_risk_decomposition_settings(config: dict[str, Any]) -> dict[str, Any]:
    raw = cfg_get(config, "biotech_features.risk_decomposition", {}) or {}
    if not isinstance(raw, dict):
        raw = {}
    weights_raw = raw.get("weights") if isinstance(raw.get("weights"), dict) else {}
    compensated_weights_raw = (
        raw.get("compensated_weights")
        if isinstance(raw.get("compensated_weights"), dict)
        else {}
    )
    penalty_weights_raw = raw.get("penalty_weights") if isinstance(raw.get("penalty_weights"), dict) else {}
    free_bands_raw = raw.get("penalty_free_bands") if isinstance(raw.get("penalty_free_bands"), dict) else {}
    caps_raw = raw.get("penalty_caps") if isinstance(raw.get("penalty_caps"), dict) else {}
    return {
        "compute_enabled": as_bool(raw.get("compute_enabled", raw.get("enabled", True))),
        "use_for_penalty": as_bool(raw.get("use_for_penalty", False)),
        "risk_penalty_mode": str(raw.get("risk_penalty_mode") or "legacy").strip().lower(),
        "compensated_free_band": bounded_float(raw.get("compensated_free_band"), 60.0, low=0.0, high=100.0),
        "compensated_penalty_weight": bounded_float(raw.get("compensated_penalty_weight"), 0.20, low=0.0, high=1.0),
        "weights": {
            key: bounded_float(weights_raw.get(key), default, low=0.0)
            for key, default in DEFAULT_STRUCTURAL_RISK_WEIGHTS.items()
        },
        "compensated_weights": {
            key: bounded_float(compensated_weights_raw.get(key), default, low=0.0)
            for key, default in DEFAULT_COMPENSATED_RISK_WEIGHTS.items()
        },
        "penalty_weights": {
            key: bounded_float(penalty_weights_raw.get(key), default, low=0.0)
            for key, default in DEFAULT_PREDICTIVE_RISK_PENALTY_WEIGHTS.items()
        },
        "penalty_free_bands": {
            key: bounded_float(free_bands_raw.get(key), default, low=0.0, high=100.0)
            for key, default in DEFAULT_PREDICTIVE_RISK_FREE_BANDS.items()
        },
        "penalty_caps": {
            key: bounded_float(caps_raw.get(key), 100.0, low=0.0, high=100.0)
            for key in DEFAULT_PREDICTIVE_RISK_PENALTY_WEIGHTS
        },
    }


def weighted_component_score(components: dict[str, float], weights: dict[str, float]) -> float:
    total_weight = sum(max(0.0, value) for value in weights.values())
    if total_weight <= 0.0:
        return 0.0
    weighted_sum = sum(max(0.0, weights.get(key, 0.0)) * clamp(value) for key, value in components.items())
    return clamp(weighted_sum / total_weight)


def load_pipeline_quality_settings(config: dict[str, Any]) -> dict[str, float]:
    raw = cfg_get(config, "biotech_features.pipeline_quality", {}) or {}
    if not isinstance(raw, dict):
        raw = {}
    settings = dict(DEFAULT_PIPELINE_QUALITY_SETTINGS)
    for key, default in DEFAULT_PIPELINE_QUALITY_SETTINGS.items():
        settings[key] = bounded_float(raw.get(key), default, low=0.0)
    return settings


def load_sec_catalyst_event_weights(config: dict[str, Any]) -> dict[str, float]:
    raw = cfg_get(config, "biotech_features.sec_event_weights", {}) or {}
    if not isinstance(raw, dict):
        raw = {}
    weights: dict[str, float] = {}
    for event_type, default in SEC_CATALYST_EVENT_WEIGHTS.items():
        weights[event_type] = bounded_float(raw.get(event_type), default, low=0.0)
    return weights


def configured_source_priority(raw: object, default: list[str]) -> list[str]:
    if not isinstance(raw, list):
        return default
    out = [str(item).strip().lower() for item in raw if str(item).strip()]
    return out or default


def resolve_going_concern_status(
    *,
    screen: Any,
    survival: dict[str, Any] | None,
    source_priority: list[str],
) -> tuple[str, str, str]:
    db_status = str((survival or {}).get("going_concern_status") or "").strip().lower()
    csv_status = str(screen.get("going_concern_status") or "").strip().lower()
    latest_status = str(
        screen.get("latest_periodic_going_concern_status")
        or (survival or {}).get("latest_periodic_going_concern_status")
        or ""
    ).strip().lower()
    for source in source_priority:
        if source == "db" and db_status:
            return db_status, latest_status, "db"
        if source == "csv" and csv_status:
            return csv_status, latest_status, "csv"
    if db_status:
        return db_status, latest_status, "db"
    if csv_status:
        return csv_status, latest_status, "csv"
    return "", latest_status, ""


def load_company_strategy_overrides(path: Path | None) -> dict[str, str]:
    df = read_optional_csv(path)
    if df.empty:
        return {}
    required = {"ticker", "company_strategy_category"}
    missing = sorted(required - set(df.columns))
    if missing:
        raise ValueError(f"Company strategy override CSV missing required columns: {','.join(missing)}")
    out: dict[str, str] = {}
    for row in df.to_dict("records"):
        ticker = normalize_ticker(row.get("ticker"))
        category = str(row.get("company_strategy_category") or "").strip().lower()
        if ticker and category:
            out[ticker] = category
    return out


def company_strategy_category(
    *,
    ticker: str,
    universe_row: Any,
    category_overrides: dict[str, str],
) -> str:
    override = category_overrides.get(ticker)
    if override:
        return override
    root_cause = str(universe_row.get("root_cause_category") or "").strip().lower()
    manual_root = str(universe_row.get("manual_root_cause") or "").strip().lower()
    if root_cause == "post_market_device_only" or manual_root == "post_market_device_only":
        return "post_market_device_services"
    if as_bool(universe_row.get("company_diagnostic_like")):
        return "diagnostics_services"
    return "clinical_therapeutics"


def ctgov_evidence_type(
    *,
    active_lead: int,
    active_collab: int,
    active_program: int,
    lead_phase2_3: int,
    program_phase2_3: int,
    collaborator_phase2_3: int,
    strategy_category: str,
    universe_row: Any,
) -> str:
    final_status = str(universe_row.get("final_status") or "").strip().lower()
    manual_verdict = str(universe_row.get("manual_verdict") or "").strip().lower()
    root_cause = str(universe_row.get("root_cause_category") or "").strip().lower()
    manual_root = str(universe_row.get("manual_root_cause") or "").strip().lower()
    category = strategy_category.strip().lower()

    if final_status == "remove" or manual_verdict == "manual_remove":
        if category in DEVICE_CATEGORIES or root_cause == "post_market_device_only" or manual_root == "post_market_device_only":
            return "non_therapeutic_device_removed"
        return "removed"
    if category in DIAGNOSTICS_SERVICE_CATEGORIES:
        return "diagnostics_services"
    if active_program > 0 or program_phase2_3 > 0:
        return "program_owner_active"
    if active_lead > 0 or lead_phase2_3 > 0:
        return "lead_sponsor_active"
    if active_collab > 0 or collaborator_phase2_3 > 0:
        return "collaborator_only_active"
    return "historical_only"


def load_company_ids(conn: sqlite3.Connection) -> dict[str, int]:
    rows = conn.execute("SELECT company_id, ticker FROM companies WHERE is_active = 1").fetchall()
    return {normalize_ticker(row["ticker"]): int(row["company_id"]) for row in rows}


def load_latest_survival_features(conn: sqlite3.Connection, asof_date: date) -> dict[int, dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT f.*
        FROM financial_survival_features f
        JOIN (
            SELECT company_id, MAX(asof_date) AS max_asof
            FROM financial_survival_features
            WHERE asof_date <= ?
            GROUP BY company_id
        ) latest
          ON latest.company_id = f.company_id AND latest.max_asof = f.asof_date
        """,
        (asof_date.isoformat(),),
    ).fetchall()
    return {int(row["company_id"]): dict(row) for row in rows}


def load_latest_market_features(
    conn: sqlite3.Connection,
    asof_date: date,
    *,
    source_priority: list[str],
    max_staleness_days: int,
) -> dict[int, dict[str, Any]]:
    source_clause = ""
    params: list[Any] = [asof_date.isoformat()]
    if source_priority:
        source_clause = " AND source IN (" + ",".join("?" for _ in source_priority) + ")"
        params.extend(source_priority)
    rows = conn.execute(
        f"""
        SELECT f.*
        FROM market_features_daily f
        JOIN (
            SELECT company_id, source, MAX(asof_date) AS max_asof
            FROM market_features_daily
            WHERE asof_date <= ?{source_clause}
            GROUP BY company_id, source
        ) latest
          ON latest.company_id = f.company_id
         AND latest.source = f.source
         AND latest.max_asof = f.asof_date
        """,
        tuple(params),
    ).fetchall()
    return select_latest_rows_by_source_priority(
        rows,
        asof_date=asof_date,
        source_priority=source_priority,
        max_staleness_days=max_staleness_days,
    )


def load_latest_governance_features(conn: sqlite3.Connection, asof_date: date) -> dict[int, dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT f.*
        FROM governance_event_features_daily f
        JOIN (
            SELECT company_id, MAX(asof_date) AS max_asof
            FROM governance_event_features_daily
            WHERE asof_date <= ?
            GROUP BY company_id
        ) latest
          ON latest.company_id = f.company_id AND latest.max_asof = f.asof_date
        """,
        (asof_date.isoformat(),),
    ).fetchall()
    return {int(row["company_id"]): dict(row) for row in rows}


def load_recent_sec_filing_summary(conn: sqlite3.Connection, asof_date: date, *, lookback_days: int = 730) -> dict[int, dict[str, int]]:
    cutoff = (asof_date - timedelta(days=max(1, lookback_days))).isoformat()
    rows = conn.execute(
        """
        SELECT company_id, form, COUNT(*) AS n
        FROM sec_filings
        WHERE filing_date >= ?
          AND filing_date <= ?
        GROUP BY company_id, form
        """,
        (cutoff, asof_date.isoformat()),
    ).fetchall()
    out: dict[int, dict[str, int]] = {}
    for row in rows:
        company_id = int(row["company_id"])
        form = str(row["form"] or "").upper()
        bucket = out.setdefault(company_id, {"recent_sec_filing_count_2y": 0, "recent_current_report_count_2y": 0, "recent_nt_filing_count_2y": 0})
        count = int(row["n"] or 0)
        bucket["recent_sec_filing_count_2y"] += count
        if form in {"8-K", "8-K/A", "6-K", "6-K/A"}:
            bucket["recent_current_report_count_2y"] += count
        if form.startswith("NT "):
            bucket["recent_nt_filing_count_2y"] += count
    return out


def screen_row_with_fallbacks(
    screen_row: Any | None,
    *,
    market: dict[str, Any] | None,
    sec_filings: dict[str, int] | None,
) -> Any | None:
    if screen_row is not None:
        out = screen_row.copy()
    else:
        out = pd.Series(dtype=str)
    market = market or {}
    sec_filings = sec_filings or {}

    if to_float(out.get("median_addv20"), 0.0) <= 0.0:
        addv = to_float(market.get("avg_dollar_volume_20d"), 0.0)
        if addv > 0.0:
            out["median_addv20"] = addv
            out["liquidity_status"] = out.get("liquidity_status") or "market_feature_fallback"

    if not str(out.get("has_recent_sec_filing_2y") or "").strip() and sec_filings.get("recent_sec_filing_count_2y", 0) > 0:
        out["has_recent_sec_filing_2y"] = "True"
        out["recent_sec_filing_count_2y"] = sec_filings.get("recent_sec_filing_count_2y", 0)
        out["recent_current_report_count_2y"] = sec_filings.get("recent_current_report_count_2y", 0)
        out["recent_nt_filing_count_2y"] = sec_filings.get("recent_nt_filing_count_2y", 0)
    return out


def is_actionable_sec_event(event_type: str, excerpt: str) -> bool:
    text = " ".join(str(excerpt or "").lower().split())
    if not text:
        return False

    hypothetical_terms = (
        "can place",
        "could place",
        "may place",
        "can impose",
        "could impose",
        "may impose",
        "could result",
        "may result",
        "risk factors",
        "there can be no assurance",
        "we may",
        "we could",
        "if we",
    )
    generic_nda_terms = (
        "an nda must contain",
        "a bla must contain",
        "once the submission has been",
        "if the nda",
        "if the bla",
        "as part of an nda",
        "submitted to the fda as part of",
    )

    if event_type in {"clinical_hold", "partial_clinical_hold"}:
        if any(term in text for term in hypothetical_terms):
            return False
        return any(term in text for term in ("imposed", "placed", "issued", "maintained", "continued", "received", "announced"))

    if event_type == "clinical_update_negative":
        if any(term in text for term in hypothetical_terms):
            return False
        if any(term in text for term in ("repurchase", "retained earnings", "license termination rights", "bankruptcy or similar")):
            return False
        return any(term in text for term in ("topline", "clinical", "phase 1", "phase 2", "phase 3", "trial", "study", "program", "safety signal"))

    if event_type in {"nda_bla_accepted", "regulatory_submission", "pdufa_date"}:
        if any(term in text for term in generic_nda_terms):
            return False
        if event_type == "regulatory_submission" and any(
            term in text for term in ("can submit", "may submit", "would submit", "is required to submit")
        ):
            return False

    if event_type == "going_concern_confirmed":
        if any(term in text for term in ("no substantial doubt", "alleviate substantial doubt", "alleviated substantial doubt")):
            return False

    return True


def load_recent_sec_event_summary(
    conn: sqlite3.Connection,
    asof_date: date,
    *,
    lookback_days: int = 730,
    sec_catalyst_half_life_days: float = 90.0,
    sec_catalyst_event_weights: dict[str, float] | None = None,
) -> dict[int, dict[str, Any]]:
    event_weights = sec_catalyst_event_weights or SEC_CATALYST_EVENT_WEIGHTS
    cutoff = (asof_date - timedelta(days=max(1, lookback_days))).isoformat()
    event_rows = conn.execute(
        """
        SELECT company_id, filing_date, form, event_type, event_date, event_value, polarity, confidence, extracted_text, accession_nodash
        FROM sec_events
        WHERE filing_date >= ?
          AND filing_date <= ?
        ORDER BY filing_date DESC, confidence DESC, event_id DESC
        """,
        (cutoff, asof_date.isoformat()),
    ).fetchall()
    summary: dict[int, dict[str, Any]] = {}
    per_company_seen: dict[int, int] = {}
    seen_count_keys: set[tuple[int, str, str, str]] = set()
    for row in event_rows:
        event_type = str(row["event_type"] or "")
        excerpt = str(row["extracted_text"] or "")
        if not is_actionable_sec_event(event_type, excerpt):
            continue
        company_id = int(row["company_id"])
        filing_date = str(row["filing_date"] or "")
        polarity = str(row["polarity"] or "neutral")
        accession = str(row["accession_nodash"] or "")
        bucket = summary.setdefault(
            company_id,
            {
                "counts": {},
                "polarity_counts": {},
                "latest_filing_dates": {},
                "recent_events": [],
                "sec_catalyst_recency": {
                    "raw_score": 0.0,
                    "recency_adjusted_score": 0.0,
                    "event_count": 0,
                    "latest_filing_date": "",
                    "latest_event_date": "",
                    "latest_event_type": "",
                    "recency_days": "",
                    "recency_basis": "",
                    "max_event_age_days": "",
                    "event_types": [],
                    "future_pdufa_event_count": 0,
                    "half_life_days": sec_catalyst_half_life_days,
                },
            },
        )
        count_key = (company_id, event_type, polarity, accession)
        if count_key not in seen_count_keys:
            seen_count_keys.add(count_key)
            bucket["counts"][event_type] = bucket["counts"].get(event_type, 0) + 1
            bucket["polarity_counts"][polarity] = bucket["polarity_counts"].get(polarity, 0) + 1
            latest = str(bucket["latest_filing_dates"].get(event_type) or "")
            if not latest or filing_date > latest:
                bucket["latest_filing_dates"][event_type] = filing_date
            event_weight = event_weights.get(event_type)
            if event_weight is not None:
                event_date = str(row["event_date"] or "")
                multiplier, age_days, recency_basis = sec_catalyst_event_multiplier(
                    event_type=event_type,
                    filing_date=filing_date,
                    event_date=event_date,
                    asof_date=asof_date,
                    half_life_days=sec_catalyst_half_life_days,
                )
                recency = bucket["sec_catalyst_recency"]
                recency["raw_score"] = round(float(recency.get("raw_score") or 0.0) + event_weight, 6)
                recency["recency_adjusted_score"] = round(
                    float(recency.get("recency_adjusted_score") or 0.0) + event_weight * multiplier,
                    6,
                )
                recency["event_count"] = int(recency.get("event_count") or 0) + 1
                event_types = recency.setdefault("event_types", [])
                if isinstance(event_types, list) and event_type not in event_types:
                    event_types.append(event_type)
                if recency_basis == "pdufa_event_date_proximity":
                    recency["future_pdufa_event_count"] = int(recency.get("future_pdufa_event_count") or 0) + 1
                if age_days is not None:
                    max_age = recency.get("max_event_age_days")
                    max_age_value = to_int(max_age) if str(max_age if max_age is not None else "").strip() else None
                    if max_age_value is None or age_days > max_age_value:
                        recency["max_event_age_days"] = age_days
                latest_event_date = str(recency.get("latest_filing_date") or "")
                if filing_date and (not latest_event_date or filing_date > latest_event_date):
                    recency["latest_filing_date"] = filing_date
                    recency["latest_event_date"] = event_date
                    recency["latest_event_type"] = event_type
                    recency["recency_days"] = "" if age_days is None else age_days
                    recency["recency_basis"] = recency_basis
        if per_company_seen.get(company_id, 0) >= 5:
            continue
        bucket["recent_events"].append(
            {
                "filing_date": filing_date,
                "event_date": str(row["event_date"] or ""),
                "event_value": str(row["event_value"] or ""),
                "form": str(row["form"] or ""),
                "event_type": event_type,
                "polarity": polarity,
                "confidence": to_float(row["confidence"], 0.0),
                "excerpt": excerpt[:320],
            }
        )
        per_company_seen[company_id] = per_company_seen.get(company_id, 0) + 1
    return summary


def empty_evidence_summary() -> dict[str, Any]:
    return {
        "active_pivotal_trials": 0,
        "active_phase3_trials": 0,
        "active_phase2_trials": 0,
        "active_qualifying_device_trials": 0,
        "active_lead_phase2_3_trials": 0,
        "active_program_phase2_3_trials": 0,
        "active_collaborator_phase2_3_trials": 0,
        "effective_phase2_3_trials": 0.0,
        "core_pipeline_quality_score": 0.0,
        "collaborator_dependency_ratio": 0.0,
        "collaborator_heavy_flag": False,
        "outcome_override_rows": 0,
        "outcome_override_excluded_rows": 0,
        "outcome_override_review_rows": 0,
        "top_ncts": [],
    }


def evidence_summary(
    evidence_df: pd.DataFrame,
    ticker: str,
    pipeline_quality_settings: dict[str, float],
) -> dict[str, Any]:
    if evidence_df.empty:
        return empty_evidence_summary()
    ev = cast(Any, evidence_df[evidence_df["ticker"].str.upper() == ticker.upper()].copy())
    if ev.empty:
        return empty_evidence_summary()
    override_mask = ev["outcome_override_applied"].astype(str).map(as_bool).astype(bool) if "outcome_override_applied" in ev else pd.Series(False, index=ev.index)
    override_excluded_mask = (
        ev["exclusion_reasons"]
        .astype(str)
        .map(lambda value: any(part == "outcome_override" or part.startswith("outcome_override:") for part in value.split(";")))
        if "exclusion_reasons" in ev
        else pd.Series(False, index=ev.index)
    )
    override_review_mask = ev["outcome_override_manual_review"].astype(str).map(as_bool).astype(bool) if "outcome_override_manual_review" in ev else pd.Series(False, index=ev.index)
    override_counts = {
        "outcome_override_rows": int(override_mask.sum()),
        "outcome_override_excluded_rows": int((override_mask & override_excluded_mask).sum()),
        "outcome_override_review_rows": int((override_mask & override_review_mask).sum()),
    }

    # Only strong company links should drive scoring. Weak links remain visible in audit outputs,
    # but should not inflate catalyst quality for broad sponsors or collaborator-heavy tickers.
    strong_mask = ev["strong_company_link"].astype(str).map(as_bool).astype(bool) if "strong_company_link" in ev else pd.Series(True, index=ev.index)
    strong = cast(Any, ev[strong_mask])
    active_status_mask = strong["is_active_status"].astype(str).map(as_bool).astype(bool)
    qualifying_mask = strong["qualifying_trial"].astype(str).map(as_bool).astype(bool)
    active = cast(Any, strong[active_status_mask & qualifying_mask].copy())
    if active.empty:
        empty = empty_evidence_summary()
        empty.update(override_counts)
        return empty

    phase_rank = active["phase_rank"].map(to_int) if "phase_rank" in active else pd.Series(0, index=active.index)
    phase23_mask = phase_rank.isin([2, 3])
    lead_mask = active["match_roles"].astype(str).map(lambda raw: roles_contain(raw, "lead")).astype(bool)
    program_mask = active["match_roles"].astype(str).map(lambda raw: roles_contain(raw, "program")).astype(bool)
    collab_mask = active["match_roles"].astype(str).map(lambda raw: roles_contain(raw, "collaborator")).astype(bool)
    pivotal_mask = active["is_pivotal"].astype(str).map(as_bool).astype(bool) if "is_pivotal" in active else pd.Series(False, index=active.index)

    lead_phase23 = int((phase23_mask & lead_mask).sum())
    program_phase23 = int((phase23_mask & program_mask).sum())
    collab_phase23 = int((phase23_mask & collab_mask & ~lead_mask & ~program_mask).sum())
    lead_active = int(lead_mask.sum())
    program_active = int(program_mask.sum())
    collab_only_active = int((collab_mask & ~lead_mask & ~program_mask).sum())
    verified_active = int(len(active))
    collab_ratio = round(collab_only_active / verified_active, 4) if verified_active else 0.0
    effective_phase23 = round(
        lead_phase23
        + pipeline_quality_settings["effective_program_phase23_weight"] * program_phase23
        + pipeline_quality_settings["effective_collaborator_phase23_weight"] * collab_phase23,
        4,
    )
    pipeline_density = effective_phase23 / verified_active if verified_active else 0.0
    pivotal_core = int((pivotal_mask & (lead_mask | program_mask)).sum())

    core_quality = 0.0
    core_quality += min(
        pipeline_quality_settings["lead_phase23_cap"],
        lead_phase23 * pipeline_quality_settings["lead_phase23_weight"],
    )
    core_quality += min(
        pipeline_quality_settings["program_phase23_cap"],
        program_phase23 * pipeline_quality_settings["program_phase23_weight"],
    )
    core_quality += min(
        pipeline_quality_settings["lead_active_cap"],
        lead_active * pipeline_quality_settings["lead_active_weight"],
    )
    core_quality += min(
        pipeline_quality_settings["pivotal_core_cap"],
        pivotal_core * pipeline_quality_settings["pivotal_core_weight"],
    )
    core_quality += min(
        pipeline_quality_settings["pipeline_density_cap"],
        pipeline_density * pipeline_quality_settings["pipeline_density_weight"],
    )
    core_quality += min(
        pipeline_quality_settings["collab_phase23_cap"],
        collab_phase23 * pipeline_quality_settings["collab_phase23_weight"],
    )
    core_quality -= min(
        pipeline_quality_settings["collab_penalty_cap"],
        max(0.0, collab_ratio - pipeline_quality_settings["collab_penalty_threshold"])
        * pipeline_quality_settings["collab_penalty_weight"],
    )
    core_quality = clamp(core_quality)
    collaborator_heavy_threshold = pipeline_quality_settings["collaborator_heavy_threshold"]
    collaborator_heavy = collab_ratio >= collaborator_heavy_threshold and collab_only_active > (lead_active + program_active)

    top = cast(Any, active.copy())
    top["_score"] = top["trial_score"].map(to_float) if "trial_score" in top.columns else 0.0
    top["_lead_or_program"] = lead_mask.astype(int) + program_mask.astype(int)
    if "last_update_post_date" not in top.columns:
        top["last_update_post_date"] = ""
    top = top.sort_values(["_lead_or_program", "_score", "last_update_post_date"], ascending=[False, False, False]).head(5)
    return {
        "active_pivotal_trials": int(pivotal_mask.sum()),
        "active_phase3_trials": int((phase_rank == 3).sum()),
        "active_phase2_trials": int((phase_rank == 2).sum()),
        "active_qualifying_device_trials": int((active["is_qualifying_device"].astype(str).map(as_bool).astype(bool)).sum()) if "is_qualifying_device" in active else 0,
        "active_lead_phase2_3_trials": lead_phase23,
        "active_program_phase2_3_trials": program_phase23,
        "active_collaborator_phase2_3_trials": collab_phase23,
        "effective_phase2_3_trials": effective_phase23,
        "core_pipeline_quality_score": round(core_quality, 4),
        "collaborator_dependency_ratio": collab_ratio,
        "collaborator_heavy_flag": collaborator_heavy,
        **override_counts,
        "top_ncts": [
            {
                "nct_id": str(row.get("nct_id", "")),
                "title": str(row.get("brief_title", "")),
                "status": str(row.get("overall_status", "")),
                "phase": str(row.get("phase_text", "")),
                "match_roles": str(row.get("match_roles", "")),
                "score": to_float(row.get("trial_score", 0.0)),
            }
            for row in top.to_dict("records")
        ],
    }


def build_evidence_summary_index(
    evidence_df: pd.DataFrame,
    pipeline_quality_settings: dict[str, float],
) -> dict[str, dict[str, Any]]:
    if evidence_df.empty or "ticker" not in evidence_df.columns:
        return {}
    out: dict[str, dict[str, Any]] = {}
    ticker_series = evidence_df["ticker"].astype(str).str.upper()
    for ticker, group in evidence_df.groupby(ticker_series, sort=False):
        normalized = normalize_ticker(ticker)
        if not normalized:
            LOGGER.warning("Skipping blank ticker in evidence summary index")
            continue
        out[normalized] = evidence_summary(group, normalized, pipeline_quality_settings)
    return out


def compute_feature_row(
    *,
    universe_row: Any,
    screen_row: Any | None,
    evidence: dict[str, Any],
    company_id: int,
    asof_date: date,
    min_liquidity_addv20: float,
    low_liquidity_addv20: float,
    strong_liquidity_addv20: float,
    category_overrides: dict[str, str],
    going_concern_source_priority: list[str],
    survival_score_blend_weight: float,
    core_pipeline_quality_multiplier: float,
    sec_catalyst_event_weights: dict[str, float],
    risk_decomposition_settings: dict[str, Any],
    sec_catalyst_recency_decay_enabled: bool,
    sec_catalyst_half_life_days: float,
    market: dict[str, Any] | None,
    survival: dict[str, Any] | None,
    sec_events: dict[str, Any] | None,
    indication_success_settings: dict[str, Any] | None = None,
    forward_catalyst: dict[str, Any] | None = None,
    short_interest: dict[str, Any] | None = None,
    institutional_ownership: dict[str, Any] | None = None,
    governance: dict[str, Any] | None = None,
) -> dict[str, Any]:
    ticker = str(universe_row["ticker"]).upper()
    verified_active = to_int(universe_row.get("verified_qualifying_active_trial_count"))
    active_lead = to_int(universe_row.get("active_lead_sponsor_trials"))
    active_collab = to_int(universe_row.get("active_collaborator_trials"))
    active_program = to_int(universe_row.get("active_program_override_trials"))
    phase2_3 = to_int(universe_row.get("phase2_3_active_trials"))
    lead_phase2_3 = to_int(evidence.get("active_lead_phase2_3_trials"))
    program_phase2_3 = to_int(evidence.get("active_program_phase2_3_trials"))
    collaborator_phase2_3 = to_int(evidence.get("active_collaborator_phase2_3_trials"))
    effective_phase2_3 = to_float(evidence.get("effective_phase2_3_trials"))
    core_pipeline_quality = to_float(evidence.get("core_pipeline_quality_score"))
    collaborator_dependency_ratio = to_float(evidence.get("collaborator_dependency_ratio"))
    collaborator_heavy = bool(evidence.get("collaborator_heavy_flag"))
    outcome_override_excluded = to_int(evidence.get("outcome_override_excluded_rows"))
    outcome_override_review = to_int(evidence.get("outcome_override_review_rows"))
    review_bucket = str(universe_row.get("review_bucket") or "")
    root_cause_category = str(universe_row.get("root_cause_category") or "")
    manual_root_cause = str(universe_row.get("manual_root_cause") or root_cause_category)
    strategy_category = company_strategy_category(
        ticker=ticker,
        universe_row=universe_row,
        category_overrides=category_overrides,
    )
    evidence_type = ctgov_evidence_type(
        active_lead=active_lead,
        active_collab=active_collab,
        active_program=active_program,
        lead_phase2_3=lead_phase2_3,
        program_phase2_3=program_phase2_3,
        collaborator_phase2_3=collaborator_phase2_3,
        strategy_category=strategy_category,
        universe_row=universe_row,
    )
    total_trials = to_int(universe_row.get("total_linked_trials"))
    pipeline_density = to_float(universe_row.get("pipeline_density"))
    stale_active = to_int(universe_row.get("stale_active_trials"))
    manual_keep = str(universe_row.get("manual_verdict") or "").strip().lower() == "manual_keep"
    primary_trial_score = to_float(universe_row.get("primary_trial_score"))
    raw_days_since_update = universe_row.get("days_since_last_update")
    days_since_update = to_int(raw_days_since_update, 9999)
    has_trial_update_age = str(raw_days_since_update if raw_days_since_update is not None else "").strip() != ""

    screen = screen_row if screen_row is not None else pd.Series(dtype=str)
    median_addv20 = to_float(screen.get("median_addv20"), 0.0)
    liquidity_status = str(screen.get("liquidity_status") or "")
    recent_sec = as_bool(screen.get("has_recent_sec_filing_2y"))
    recent_sec_count = to_int(screen.get("recent_sec_filing_count_2y"))
    recent_current_reports = to_int(screen.get("recent_current_report_count_2y"))
    recent_nt = to_int(screen.get("recent_nt_filing_count_2y"))
    rnd_disclosure = as_bool(screen.get("has_recent_rnd_disclosure"))
    pipeline_disclosure = as_bool(screen.get("has_pipeline_disclosure"))
    rnd_fact_hit_count = to_int(screen.get("recent_rnd_fact_hit_count"))
    going_status, latest_gc_status, going_concern_source = resolve_going_concern_status(
        screen=screen,
        survival=survival,
        source_priority=going_concern_source_priority,
    )
    google_gc_confirmed = as_bool(screen.get("google_going_concern_confirmed"))
    reverse_2y = to_int(screen.get("reverse_split_hits_2y"))
    reverse_5y = to_int(screen.get("reverse_split_hits_5y"))
    reverse_soft_2y = to_int(screen.get("reverse_split_soft_hits_2y"))
    google_reverse_confirmed = as_bool(screen.get("google_reverse_split_confirmed"))
    source_reason_codes = str(universe_row.get("source_reason_codes") or "")
    survival_score = to_float(survival.get("financial_survival_score") if survival else None, math.nan)
    survival_score_for_calc = survival_score if math.isfinite(survival_score) else 45.0
    survival_quality = str(survival.get("data_quality") if survival else "").strip().lower()
    cash_runway_months = to_float(survival.get("cash_runway_months") if survival else None, math.nan)
    severe_runway_raw = survival.get("severe_runway_flag") if survival else None
    short_runway_raw = survival.get("short_runway_flag") if survival else None
    severe_runway_flag = (
        to_int(severe_runway_raw)
        if severe_runway_raw not in {None, ""}
        else None
    )
    short_runway_flag = (
        to_int(short_runway_raw)
        if short_runway_raw not in {None, ""}
        else None
    )
    dilution_pressure_score = to_float(survival.get("dilution_pressure_score") if survival else None, 0.0)
    burn_acceleration_flag = to_int(survival.get("burn_acceleration_flag") if survival else None)
    event_counts = dict(sec_events.get("counts", {}) if sec_events else {})
    polarity_counts = dict(sec_events.get("polarity_counts", {}) if sec_events else {})
    nda_bla_accepted = to_int(event_counts.get("nda_bla_accepted"))
    pdufa_date = to_int(event_counts.get("pdufa_date"))
    regulatory_submission = to_int(event_counts.get("regulatory_submission"))
    endpoint_met = to_int(event_counts.get("endpoint_met"))
    clinical_update_positive = to_int(event_counts.get("clinical_update_positive"))
    endpoint_missed = to_int(event_counts.get("endpoint_missed"))
    clinical_update_negative = to_int(event_counts.get("clinical_update_negative"))
    clinical_hold = to_int(event_counts.get("clinical_hold"))
    partial_clinical_hold = to_int(event_counts.get("partial_clinical_hold"))
    safety_signal = to_int(event_counts.get("safety_signal"))
    atm_program = to_int(event_counts.get("atm_program"))
    atm_facility = to_int(event_counts.get("atm_facility"))
    public_offering = to_int(event_counts.get("public_offering"))
    pipe_financing = to_int(event_counts.get("pipe_financing"))
    shelf_registration = to_int(event_counts.get("shelf_registration"))
    financing_shelf = to_int(event_counts.get("financing_shelf"))
    partnership_license = to_int(event_counts.get("partnership_license"))
    partnership_signed = to_int(event_counts.get("partnership_signed"))
    going_concern_confirmed = to_int(event_counts.get("going_concern_confirmed"))
    going_concern = to_int(event_counts.get("going_concern"))
    active_pivotal_trials = to_int(evidence.get("active_pivotal_trials"))
    active_phase3_trials = to_int(evidence.get("active_phase3_trials"))
    active_phase2_trials = to_int(evidence.get("active_phase2_trials"))
    active_qualifying_device_trials = to_int(evidence.get("active_qualifying_device_trials"))
    top_ncts = evidence.get("top_ncts", [])
    if not isinstance(top_ncts, list):
        top_ncts = []

    regulatory_catalysts = nda_bla_accepted + pdufa_date + regulatory_submission
    positive_clinical_events = endpoint_met + clinical_update_positive
    negative_clinical_events = endpoint_missed + clinical_update_negative + clinical_hold + partial_clinical_hold + safety_signal
    dilution_events = atm_program + atm_facility + public_offering + pipe_financing + shelf_registration + financing_shelf
    partnership_events = partnership_license + partnership_signed
    sec_gc_events = going_concern_confirmed + going_concern
    sec_catalyst_raw_score = (
        pdufa_date * sec_catalyst_event_weights["pdufa_date"]
        + nda_bla_accepted * sec_catalyst_event_weights["nda_bla_accepted"]
        + regulatory_submission * sec_catalyst_event_weights["regulatory_submission"]
        + endpoint_met * sec_catalyst_event_weights["endpoint_met"]
        + clinical_update_positive * sec_catalyst_event_weights["clinical_update_positive"]
    )
    sec_catalyst_recency = dict(sec_events.get("sec_catalyst_recency", {}) if sec_events else {})
    sec_catalyst_recency_adjusted_score = to_float(
        sec_catalyst_recency.get("recency_adjusted_score"),
        sec_catalyst_raw_score,
    )
    sec_catalyst_score_used = (
        sec_catalyst_recency_adjusted_score
        if sec_catalyst_recency_decay_enabled
        else sec_catalyst_raw_score
    )
    sec_catalyst_decay_delta = sec_catalyst_score_used - sec_catalyst_raw_score
    sec_catalyst_latest_filing_date = str(sec_catalyst_recency.get("latest_filing_date") or "")
    sec_catalyst_latest_event_date = str(sec_catalyst_recency.get("latest_event_date") or "")
    sec_catalyst_latest_event_type = str(sec_catalyst_recency.get("latest_event_type") or "")
    sec_catalyst_recency_basis = str(sec_catalyst_recency.get("recency_basis") or "")
    sec_catalyst_event_types_raw = sec_catalyst_recency.get("event_types", [])
    sec_catalyst_event_types = (
        "|".join(str(item) for item in sec_catalyst_event_types_raw)
        if isinstance(sec_catalyst_event_types_raw, list)
        else str(sec_catalyst_event_types_raw or "")
    )
    sec_catalyst_recency_days_raw = sec_catalyst_recency.get("recency_days")
    sec_catalyst_recency_days = (
        to_int(sec_catalyst_recency_days_raw)
        if str(sec_catalyst_recency_days_raw if sec_catalyst_recency_days_raw is not None else "").strip()
        else ""
    )
    indication_settings = indication_success_settings or load_indication_success_settings({})
    indication_area = ""
    indication_success_prob = 0.0
    indication_success_baseline = 0.0
    indication_success_multiplier = 1.0
    base_phase2_3_component = min(
        CATALYST_COMPONENT_MAX["effective_phase2_3_trials"],
        math.log1p(max(effective_phase2_3, 0.0)) * 10.0,
    )
    if bool(indication_settings.get("enabled", True)):
        indication_area = infer_indication_success_area(
            universe_row=universe_row,
            evidence=evidence,
            strategy_category=strategy_category,
            default_area=str(indication_settings.get("default_area") or "general"),
        )
        indication_success_prob, indication_success_baseline, raw_indication_multiplier = indication_success_probability(
            area=indication_area,
            active_phase3_trials=active_phase3_trials,
            active_pivotal_trials=active_pivotal_trials,
            active_phase2_trials=active_phase2_trials,
            phase2_3_trials=phase2_3,
            settings=indication_settings,
        )
        indication_success_multiplier = max(
            float(indication_settings.get("min_multiplier", 0.70)),
            min(float(indication_settings.get("max_multiplier", 1.30)), raw_indication_multiplier),
        )
    indication_weighted_phase2_3_component = min(
        CATALYST_COMPONENT_MAX["effective_phase2_3_trials"],
        math.log1p(max(effective_phase2_3, 0.0) * max(0.0, indication_success_multiplier)) * 10.0,
    )
    phase2_3_component_used = (
        indication_weighted_phase2_3_component
        if bool(indication_settings.get("enabled", True)) and bool(indication_settings.get("apply_to_catalyst", False))
        else base_phase2_3_component
    )
    forward_catalyst_signals = forward_catalyst_signal(
        forward_catalyst,
        lookahead_days=to_int(indication_settings.get("forward_catalyst_lookahead_days"), 365),
    )
    short_interest_signals = short_interest_signal(short_interest)
    institutional_ownership_signals = institutional_ownership_signal(institutional_ownership)
    insider_activity_signals = insider_activity_signal(governance)

    catalyst_components = {
        "verified_active_trials": min(
            CATALYST_COMPONENT_MAX["verified_active_trials"],
            math.log1p(max(verified_active, 0)) * 5.0,
        ),
        "effective_phase2_3_trials": phase2_3_component_used,
        "active_pivotal_trials": min(
            CATALYST_COMPONENT_MAX["active_pivotal_trials"],
            math.log1p(max(active_pivotal_trials, 0)) * 8.0,
        ),
        "active_lead_sponsor_trials": min(
            CATALYST_COMPONENT_MAX["active_lead_sponsor_trials"],
            math.log1p(max(active_lead, 0)) * 5.0,
        ),
        "active_program_override_trials": min(
            CATALYST_COMPONENT_MAX["active_program_override_trials"],
            math.log1p(max(active_program, 0)) * 4.0,
        ),
        "pipeline_density": min(CATALYST_COMPONENT_MAX["pipeline_density"], pipeline_density * 10.0),
        "core_pipeline_quality": min(
            CATALYST_COMPONENT_MAX["core_pipeline_quality"],
            core_pipeline_quality * core_pipeline_quality_multiplier,
        ),
        "recent_trial_update": 0.0,
        "regulatory_or_positive_clinical_event": 0.0,
    }
    catalyst_penalty = 0.0
    if collaborator_heavy:
        catalyst_penalty += min(15.0, max(0.0, collaborator_dependency_ratio - 0.50) * 30.0)
    if has_trial_update_age and days_since_update <= 90:
        catalyst_components["recent_trial_update"] = 8.0
    elif has_trial_update_age and days_since_update <= 180:
        catalyst_components["recent_trial_update"] = 4.0
    elif has_trial_update_age and days_since_update >= 365 and verified_active > 0:
        catalyst_penalty += 6.0
    catalyst_components["regulatory_or_positive_clinical_event"] = min(
        CATALYST_COMPONENT_MAX["regulatory_or_positive_clinical_event"],
        sec_catalyst_score_used,
    )
    catalyst_positive_raw = sum(catalyst_components.values())
    catalyst_positive_after_penalty = max(0.0, catalyst_positive_raw - catalyst_penalty)
    catalyst_raw = clamp((catalyst_positive_after_penalty / CATALYST_POSITIVE_MAX) * 100.0)

    credibility_raw = 0.0
    credibility_raw += min(25.0, active_lead * 5.0)
    credibility_raw += min(20.0, active_program * 5.0)
    credibility_raw += min(8.0, math.log1p(max(active_collab, 0)) * 2.0)
    credibility_raw += min(12.0, math.log1p(max(total_trials, 0)) * 3.0)
    credibility_raw += min(10.0, core_pipeline_quality * 0.10)
    if collaborator_heavy:
        credibility_raw -= 8.0
    credibility_raw += 10.0 if recent_sec else 0.0
    credibility_raw += min(8.0, recent_sec_count * 0.5)
    credibility_raw += 7.0 if rnd_disclosure else 0.0
    credibility_raw += 7.0 if pipeline_disclosure else 0.0
    credibility_raw += min(6.0, rnd_fact_hit_count * 2.0)
    credibility_raw += 5.0 if manual_keep else 0.0
    credibility_raw += min(10.0, partnership_events * 5.0 + regulatory_catalysts * 2.0)
    credibility_raw = clamp(credibility_raw)

    liquidity_risk = 0.0
    if median_addv20 <= 0:
        liquidity_risk += 35.0
    elif median_addv20 < min_liquidity_addv20:
        liquidity_risk += 35.0
    elif median_addv20 < low_liquidity_addv20:
        liquidity_risk += 22.0
    elif median_addv20 < strong_liquidity_addv20:
        liquidity_risk += 8.0

    filing_risk = 0.0
    confirmed_gc = google_gc_confirmed or going_status == "confirmed" or latest_gc_status == "hard"
    if confirmed_gc:
        filing_risk += 45.0
    elif sec_gc_events > 0 and going_status != "resolved":
        filing_risk += 18.0
    elif going_status == "possible" or "possible_going_concern" in source_reason_codes:
        filing_risk += 18.0
    elif going_status == "resolved":
        filing_risk += 2.0
    if google_reverse_confirmed:
        filing_risk += 35.0
    elif reverse_2y > 0:
        filing_risk += min(30.0, 12.0 + reverse_2y * 6.0)
    elif reverse_5y > 0 or reverse_soft_2y > 0:
        filing_risk += 8.0
    if recent_nt > 0:
        filing_risk += min(15.0, recent_nt * 5.0)
    if not recent_sec:
        filing_risk += 15.0
    critical_negative_events = (
        clinical_hold
        + partial_clinical_hold
        + endpoint_missed
        + safety_signal
    )
    if critical_negative_events > 0:
        filing_risk += min(
            35.0,
            clinical_hold * 30.0
            + partial_clinical_hold * 22.0
            + endpoint_missed * 20.0
            + safety_signal * 12.0,
        )
    elif clinical_update_negative > 0:
        filing_risk += min(6.0, clinical_update_negative * 1.5)
    if survival:
        if severe_runway_flag:
            filing_risk += 30.0
        elif short_runway_flag:
            filing_risk += 18.0
        elif math.isfinite(cash_runway_months) and 0 < cash_runway_months < 12:
            filing_risk += 10.0
        if dilution_pressure_score > 0:
            if (math.isfinite(cash_runway_months) and cash_runway_months >= 24) or survival_score_for_calc >= 80:
                filing_risk += min(10.0, dilution_pressure_score * 0.25)
            else:
                filing_risk += min(18.0, dilution_pressure_score * 0.45)
        if burn_acceleration_flag:
            filing_risk += 8.0
        if survival_quality == "low":
            filing_risk += 8.0
    else:
        filing_risk += 8.0

    trial_risk = 0.0
    trial_risk += min(12.0, stale_active * 2.5)
    if collaborator_heavy and active_lead == 0 and active_program == 0:
        trial_risk += 15.0
    elif collaborator_heavy:
        trial_risk += 6.0
    if verified_active == 0:
        trial_risk += 20.0
    trial_risk += min(18.0, outcome_override_excluded * 9.0 + outcome_override_review * 2.0)

    legacy_risk_raw = clamp(liquidity_risk + filing_risk + trial_risk)

    governance_filing_risk = 0.0
    if confirmed_gc:
        governance_filing_risk += 55.0
    elif sec_gc_events > 0 and going_status != "resolved":
        governance_filing_risk += 24.0
    elif going_status == "possible" or "possible_going_concern" in source_reason_codes:
        governance_filing_risk += 20.0
    elif going_status == "resolved":
        governance_filing_risk += 2.0
    if google_reverse_confirmed:
        governance_filing_risk += 45.0
    elif reverse_2y > 0:
        governance_filing_risk += min(40.0, 16.0 + reverse_2y * 8.0)
    elif reverse_5y > 0 or reverse_soft_2y > 0:
        governance_filing_risk += 8.0
    if recent_nt > 0:
        governance_filing_risk += min(18.0, recent_nt * 6.0)
    if not recent_sec:
        governance_filing_risk += 18.0

    regulatory_setback_risk = 0.0
    if critical_negative_events > 0:
        regulatory_setback_risk += min(
            55.0,
            clinical_hold * 30.0
            + partial_clinical_hold * 22.0
            + endpoint_missed * 20.0
            + safety_signal * 12.0,
        )
    elif clinical_update_negative > 0:
        regulatory_setback_risk += min(10.0, clinical_update_negative * 2.5)

    financing_survival_risk = 0.0
    data_quality_risk = 0.0
    dilution_optional_risk = 0.0
    if survival:
        if severe_runway_flag:
            financing_survival_risk += 55.0
        elif short_runway_flag:
            financing_survival_risk += 32.0
        elif math.isfinite(cash_runway_months) and 0 < cash_runway_months < 12:
            financing_survival_risk += 18.0
        if dilution_pressure_score > 0:
            if (math.isfinite(cash_runway_months) and cash_runway_months >= 24) or survival_score_for_calc >= 80:
                financing_survival_risk += min(8.0, dilution_pressure_score * 0.20)
                dilution_optional_risk += min(20.0, dilution_pressure_score * 0.25)
            else:
                financing_survival_risk += min(30.0, dilution_pressure_score * 0.55)
        if burn_acceleration_flag:
            financing_survival_risk += 14.0
        if survival_quality == "low":
            data_quality_risk += 14.0
    else:
        data_quality_risk += 10.0

    pipeline_anchor_risk = 0.0
    if verified_active == 0:
        pipeline_anchor_risk += 35.0
    pipeline_anchor_risk += min(25.0, outcome_override_excluded * 12.0 + outcome_override_review * 3.0)

    collaborator_dependency_risk = 0.0
    if collaborator_heavy and active_lead == 0 and active_program == 0:
        collaborator_dependency_risk += 60.0
    elif collaborator_heavy:
        collaborator_dependency_risk += 25.0

    trial_staleness_risk = min(25.0, stale_active * 5.0)

    clinical_binary_risk = 0.0
    if active_pivotal_trials > 0 or pdufa_date > 0 or active_phase3_trials > 0:
        clinical_binary_risk += 35.0
    elif phase2_3 > 0:
        clinical_binary_risk += 22.0
    elif verified_active > 0:
        clinical_binary_risk += 12.0

    structural_risk_components = {
        "liquidity": liquidity_risk,
        "financing_survival": financing_survival_risk,
        "governance_filing": governance_filing_risk,
        "regulatory_setback": regulatory_setback_risk,
        "pipeline_anchor": pipeline_anchor_risk,
        "data_quality": data_quality_risk,
    }
    compensated_risk_components = {
        "clinical_binary": clinical_binary_risk,
        "collaborator_dependency": collaborator_dependency_risk,
        "trial_staleness": trial_staleness_risk,
        "dilution_optional": dilution_optional_risk,
    }
    all_risk_components = {
        **structural_risk_components,
        **compensated_risk_components,
    }
    if bool(risk_decomposition_settings.get("compute_enabled", True)):
        uncompensated_risk_raw = weighted_component_score(
            structural_risk_components,
            cast(dict[str, float], risk_decomposition_settings["weights"]),
        )
        compensated_risk_raw = weighted_component_score(
            compensated_risk_components,
            cast(dict[str, float], risk_decomposition_settings["compensated_weights"]),
        )
        risk_penalty_input_score_raw = decomposed_risk_penalty_input(
            structural_risk=uncompensated_risk_raw,
            compensated_risk=compensated_risk_raw,
            compensated_free_band=float(risk_decomposition_settings["compensated_free_band"]),
            compensated_weight=float(risk_decomposition_settings["compensated_penalty_weight"]),
        )
        predictive_risk_penalty_input_score_raw = weighted_predictive_risk_penalty_input(
            all_risk_components,
            cast(dict[str, float], risk_decomposition_settings["penalty_weights"]),
            free_bands=cast(dict[str, float], risk_decomposition_settings["penalty_free_bands"]),
            caps=cast(dict[str, float], risk_decomposition_settings["penalty_caps"]),
        )
    else:
        uncompensated_risk_raw = legacy_risk_raw
        compensated_risk_raw = 50.0
        risk_penalty_input_score_raw = legacy_risk_raw
        predictive_risk_penalty_input_score_raw = legacy_risk_raw

    risk_raw = legacy_risk_raw
    risk_penalty_mode = str(risk_decomposition_settings.get("risk_penalty_mode") or "legacy").strip().lower()

    financial_quality_raw = 100.0
    financial_quality_raw -= liquidity_risk * 1.4
    financial_quality_raw -= filing_risk * 0.9
    if recent_sec:
        financial_quality_raw += 5.0
    financial_quality_raw = clamp(financial_quality_raw)
    if survival:
        feature_quality_weight = 1.0 - survival_score_blend_weight
        financial_quality_raw = (
            financial_quality_raw * feature_quality_weight
            + survival_score_for_calc * survival_score_blend_weight
        )
    else:
        financial_quality_raw -= 8.0
    financial_quality_raw = clamp(financial_quality_raw)

    momentum_raw = 0.0
    momentum_raw += (
        30.0
        if has_trial_update_age and days_since_update <= 90
        else 15.0
        if has_trial_update_age and days_since_update <= 180
        else 0.0
    )
    momentum_raw += min(25.0, recent_current_reports * 2.5)
    momentum_raw += 15.0 if median_addv20 >= strong_liquidity_addv20 else 8.0 if median_addv20 >= low_liquidity_addv20 else 0.0
    momentum_raw += min(20.0, primary_trial_score * 2.0)
    momentum_raw = clamp(momentum_raw)

    feature_json = {
        "ticker": ticker,
        "company_name": str(universe_row.get("company_name") or ""),
        "company_strategy_category": strategy_category,
        "ctgov": {
            "primary_nct": str(universe_row.get("primary_nct") or ""),
            "primary_trial_title": str(universe_row.get("primary_trial_title") or ""),
            "ctgov_evidence_type": evidence_type,
            "review_bucket": review_bucket,
            "root_cause_category": root_cause_category,
            "manual_root_cause": manual_root_cause,
            "verified_qualifying_active_trial_count": verified_active,
            "active_lead_sponsor_trials": active_lead,
            "active_collaborator_trials": active_collab,
            "active_program_override_trials": active_program,
            "phase2_3_active_trials": phase2_3,
            "lead_phase2_3_active_trials": lead_phase2_3,
            "program_phase2_3_active_trials": program_phase2_3,
            "collaborator_phase2_3_active_trials": collaborator_phase2_3,
            "effective_phase2_3_trials": effective_phase2_3,
            "core_pipeline_quality_score": round(core_pipeline_quality, 4),
            "collaborator_dependency_ratio": collaborator_dependency_ratio,
            "collaborator_heavy_flag": collaborator_heavy,
            "outcome_override_rows": evidence.get("outcome_override_rows", 0),
            "outcome_override_excluded_rows": evidence.get("outcome_override_excluded_rows", 0),
            "outcome_override_review_rows": evidence.get("outcome_override_review_rows", 0),
            "active_pivotal_trials": active_pivotal_trials,
            "active_phase3_trials": active_phase3_trials,
            "active_phase2_trials": active_phase2_trials,
            "active_qualifying_device_trials": active_qualifying_device_trials,
            "pipeline_density": pipeline_density,
            "days_since_last_update": days_since_update,
            "has_trial_update_age": has_trial_update_age,
            "top_ncts": top_ncts,
        },
        "sec_and_liquidity": {
            "recent_sec_filing_count_2y": recent_sec_count,
            "recent_current_report_count_2y": recent_current_reports,
            "recent_nt_filing_count_2y": recent_nt,
            "has_recent_rnd_disclosure": rnd_disclosure,
            "has_pipeline_disclosure": pipeline_disclosure,
            "median_addv20": median_addv20,
            "liquidity_status": liquidity_status,
            "market_source": str(market.get("source") if market else ""),
            "market_asof_date": str(market.get("asof_date") if market else ""),
            "market_price_adjustment": str(market.get("price_adjustment") if market else ""),
            "market_is_adjusted": optional_market_int(
                market.get("is_adjusted") if market else None,
                field="is_adjusted",
                ticker=ticker,
            ),
            "market_last_bar_date": str(market.get("last_bar_date") if market else ""),
            "market_bar_count": optional_market_int(
                market.get("bar_count") if market else None,
                field="bar_count",
                ticker=ticker,
            ),
            "market_data_quality": str(market.get("market_data_quality") if market else ""),
            "going_concern_status": going_status,
            "latest_periodic_going_concern_status": latest_gc_status,
            "going_concern_source": going_concern_source,
            "reverse_split_hits_2y": reverse_2y,
            "reverse_split_hits_5y": reverse_5y,
        },
        "financial_survival": {
            "latest_period_end": str(survival.get("latest_period_end") if survival else ""),
            "cash_and_investments": to_float(survival.get("cash_and_investments") if survival else None, 0.0),
            "quarterly_cash_burn": to_float(survival.get("quarterly_cash_burn") if survival else None, 0.0),
            "ttm_cash_burn": to_float(survival.get("ttm_cash_burn") if survival else None, 0.0),
            "cash_runway_months": finite_or_none(cash_runway_months),
            "working_capital_ratio": to_float(survival.get("working_capital_ratio") if survival else None, 0.0),
            "debt_to_cash": to_float(survival.get("debt_to_cash") if survival else None, 0.0),
            "cash_yoy_change_pct": to_float(survival.get("cash_yoy_change_pct") if survival else None, 0.0),
            "rd_yoy_change_pct": to_float(survival.get("rd_yoy_change_pct") if survival else None, 0.0),
            "burn_acceleration_flag": bool(burn_acceleration_flag),
            "short_runway_flag": None if short_runway_flag is None else bool(short_runway_flag),
            "severe_runway_flag": None if severe_runway_flag is None else bool(severe_runway_flag),
            "dilution_pressure_score": dilution_pressure_score,
            "financial_survival_score": finite_or_none(survival_score),
            "data_quality": survival_quality,
            "missing_fields": str(survival.get("missing_fields") if survival else ""),
            "proxy_fields_used": str(survival.get("proxy_fields_used") if survival else ""),
        },
        "sec_events": {
            "counts": event_counts,
            "polarity_counts": polarity_counts,
            "regulatory_catalyst_count": regulatory_catalysts,
            "positive_clinical_event_count": positive_clinical_events,
            "negative_clinical_event_count": negative_clinical_events,
            "dilution_event_count": dilution_events,
            "partnership_event_count": partnership_events,
            "going_concern_event_count": sec_gc_events,
            "sec_catalyst_raw_score": round(sec_catalyst_raw_score, 4),
            "sec_catalyst_recency_adjusted_score": round(sec_catalyst_recency_adjusted_score, 4),
            "sec_catalyst_score_used": round(sec_catalyst_score_used, 4),
            "sec_catalyst_decay_delta": round(sec_catalyst_decay_delta, 4),
            "sec_catalyst_recency_decay_enabled": sec_catalyst_recency_decay_enabled,
            "sec_catalyst_decay_half_life_days": sec_catalyst_half_life_days,
            "sec_catalyst_latest_filing_date": sec_catalyst_latest_filing_date,
            "sec_catalyst_latest_event_date": sec_catalyst_latest_event_date,
            "sec_catalyst_latest_event_type": sec_catalyst_latest_event_type,
            "sec_catalyst_recency_days": sec_catalyst_recency_days,
            "sec_catalyst_recency_basis": sec_catalyst_recency_basis,
            "sec_catalyst_event_types": sec_catalyst_event_types,
            "sec_catalyst_max_event_age_days": sec_catalyst_recency.get("max_event_age_days", ""),
            "sec_catalyst_future_pdufa_event_count": sec_catalyst_recency.get("future_pdufa_event_count", 0),
            "recent_events": list(sec_events.get("recent_events", []) if sec_events else []),
        },
        "shadow_signals": {
            "indication_success": {
                "area": indication_area,
                "probability": round(indication_success_prob, 6),
                "baseline_probability": round(indication_success_baseline, 6),
                "multiplier": round(indication_success_multiplier, 6),
                "base_phase2_3_component": round(base_phase2_3_component, 4),
                "weighted_phase2_3_component": round(indication_weighted_phase2_3_component, 4),
                "applied_to_catalyst": bool(indication_settings.get("apply_to_catalyst", False)),
            },
            "forward_catalyst_calendar": forward_catalyst_signals,
            "short_interest": short_interest_signals,
            "institutional_ownership": institutional_ownership_signals,
            "insider_activity": insider_activity_signals,
        },
        "raw_scores": {
            "catalyst_score_raw": round(catalyst_raw, 4),
            "credibility_score_raw": round(credibility_raw, 4),
            "financial_quality_score_raw": round(financial_quality_raw, 4),
            "risk_score_raw": round(risk_raw, 4),
            "legacy_risk_score_raw": round(legacy_risk_raw, 4),
            "risk_penalty_input_score_raw": round(risk_penalty_input_score_raw, 4),
            "predictive_risk_penalty_input_score_raw": round(predictive_risk_penalty_input_score_raw, 4),
            "uncompensated_risk_score_raw": round(uncompensated_risk_raw, 4),
            "compensated_risk_score_raw": round(compensated_risk_raw, 4),
            "risk_decomposition_compute_enabled": bool(risk_decomposition_settings.get("compute_enabled", True)),
            "risk_component_scores": {
                "structural": {
                    key: round(value, 4)
                    for key, value in sorted(structural_risk_components.items())
                },
                "compensated": {
                    key: round(value, 4)
                    for key, value in sorted(compensated_risk_components.items())
                },
                "compensated_free_band": round(float(risk_decomposition_settings["compensated_free_band"]), 4),
                "compensated_penalty_weight": round(float(risk_decomposition_settings["compensated_penalty_weight"]), 4),
                "risk_penalty_mode": risk_penalty_mode,
                "use_for_penalty": bool(risk_decomposition_settings.get("use_for_penalty", False)),
                "predictive_penalty_weights": {
                    key: round(value, 4)
                    for key, value in sorted(cast(dict[str, float], risk_decomposition_settings["penalty_weights"]).items())
                },
                "predictive_penalty_free_bands": {
                    key: round(value, 4)
                    for key, value in sorted(cast(dict[str, float], risk_decomposition_settings["penalty_free_bands"]).items())
                },
            },
            "momentum_score_raw": round(momentum_raw, 4),
            "catalyst_component_scores": {
                key: round(value, 4)
                for key, value in sorted(catalyst_components.items())
            },
            "catalyst_positive_raw_sum": round(catalyst_positive_raw, 4),
            "catalyst_positive_after_penalty": round(catalyst_positive_after_penalty, 4),
            "catalyst_positive_max_sum": round(CATALYST_POSITIVE_MAX, 4),
            "catalyst_penalty": round(catalyst_penalty, 4),
            "catalyst_penalty_normalized": round((catalyst_penalty / CATALYST_POSITIVE_MAX) * 100.0, 4),
            "catalyst_penalty_unit_space": "raw_points_pre_normalization",
            "survival_score_blend_weight": round(survival_score_blend_weight, 4),
            "sec_catalyst_raw_score": round(sec_catalyst_raw_score, 4),
            "sec_catalyst_recency_adjusted_score": round(sec_catalyst_recency_adjusted_score, 4),
            "sec_catalyst_score_used": round(sec_catalyst_score_used, 4),
            "sec_catalyst_decay_delta": round(sec_catalyst_decay_delta, 4),
            "sec_catalyst_recency_decay_enabled": sec_catalyst_recency_decay_enabled,
            "sec_catalyst_recency_basis": sec_catalyst_recency_basis,
            "sec_catalyst_event_types": sec_catalyst_event_types,
            "indication_success_probability": round(indication_success_prob, 6),
            "indication_success_multiplier": round(indication_success_multiplier, 6),
            "indication_weighted_phase2_3_component": round(indication_weighted_phase2_3_component, 4),
            "forward_catalyst_score": forward_catalyst_signals["forward_catalyst_score"],
            "short_interest_signal_score": short_interest_signals["short_interest_signal_score"],
            "institutional_accumulation_score": institutional_ownership_signals["institutional_accumulation_score"],
            "insider_accumulation_score": insider_activity_signals["insider_accumulation_score"],
        },
        "manual": {
            "manual_verdict": str(universe_row.get("manual_verdict") or ""),
            "manual_notes": str(universe_row.get("manual_notes") or ""),
        },
    }
    return {
        "asof_date": asof_date.isoformat(),
        "company_id": company_id,
        "ticker": ticker,
        "company_name": feature_json["company_name"],
        "catalyst_score_raw": round(catalyst_raw, 4),
        "credibility_score_raw": round(credibility_raw, 4),
        "financial_quality_score_raw": round(financial_quality_raw, 4),
        "risk_score_raw": round(risk_raw, 4),
        "legacy_risk_score_raw": round(legacy_risk_raw, 4),
        "risk_penalty_input_score_raw": round(risk_penalty_input_score_raw, 4),
        "predictive_risk_penalty_input_score_raw": round(predictive_risk_penalty_input_score_raw, 4),
        "uncompensated_risk_score_raw": round(uncompensated_risk_raw, 4),
        "compensated_risk_score_raw": round(compensated_risk_raw, 4),
        "liquidity_risk_score_raw": round(liquidity_risk, 4),
        "financing_survival_risk_score_raw": round(financing_survival_risk, 4),
        "governance_filing_risk_score_raw": round(governance_filing_risk, 4),
        "regulatory_setback_risk_score_raw": round(regulatory_setback_risk, 4),
        "pipeline_anchor_risk_score_raw": round(pipeline_anchor_risk, 4),
        "collaborator_dependency_risk_score_raw": round(collaborator_dependency_risk, 4),
        "trial_staleness_risk_score_raw": round(trial_staleness_risk, 4),
        "momentum_score_raw": round(momentum_raw, 4),
        "primary_nct": feature_json["ctgov"]["primary_nct"],
        "primary_trial_title": feature_json["ctgov"]["primary_trial_title"],
        "ctgov_evidence_type": evidence_type,
        "company_strategy_category": strategy_category,
        "ctgov_review_bucket": review_bucket,
        "ctgov_manual_root_cause": manual_root_cause,
        "verified_qualifying_active_trial_count": verified_active,
        "phase2_3_active_trials": phase2_3,
        "lead_phase2_3_active_trials": lead_phase2_3,
        "program_phase2_3_active_trials": program_phase2_3,
        "collaborator_phase2_3_active_trials": collaborator_phase2_3,
        "effective_phase2_3_trials": effective_phase2_3,
        "core_pipeline_quality_score": round(core_pipeline_quality, 4),
        "collaborator_dependency_ratio": collaborator_dependency_ratio,
        "collaborator_heavy_flag": collaborator_heavy,
        "active_lead_sponsor_trials": active_lead,
        "active_program_override_trials": active_program,
        "active_collaborator_trials": active_collab,
        "median_addv20": median_addv20,
        "going_concern_status": going_status,
        "reverse_split_hits_2y": reverse_2y,
        "sec_regulatory_catalyst_count": regulatory_catalysts,
        "sec_dilution_event_count": dilution_events,
        "sec_negative_clinical_event_count": negative_clinical_events,
        "sec_catalyst_raw_score": round(sec_catalyst_raw_score, 4),
        "sec_catalyst_recency_adjusted_score": round(sec_catalyst_recency_adjusted_score, 4),
        "sec_catalyst_score_used": round(sec_catalyst_score_used, 4),
        "sec_catalyst_decay_delta": round(sec_catalyst_decay_delta, 4),
        "sec_catalyst_latest_event_type": sec_catalyst_latest_event_type,
        "sec_catalyst_latest_filing_date": sec_catalyst_latest_filing_date,
        "sec_catalyst_latest_event_date": sec_catalyst_latest_event_date,
        "sec_catalyst_recency_days": sec_catalyst_recency_days,
        "sec_catalyst_recency_basis": sec_catalyst_recency_basis,
        "sec_catalyst_event_types": sec_catalyst_event_types,
        "indication_success_area": indication_area,
        "indication_success_probability": round(indication_success_prob, 6),
        "indication_success_multiplier": round(indication_success_multiplier, 6),
        "indication_weighted_phase2_3_component": round(indication_weighted_phase2_3_component, 4),
        "forward_catalyst_nearest_days": forward_catalyst_signals["forward_catalyst_nearest_days"],
        "forward_catalyst_event_type": forward_catalyst_signals["forward_catalyst_event_type"],
        "forward_catalyst_score": forward_catalyst_signals["forward_catalyst_score"],
        "short_interest_pct_float": short_interest_signals["short_interest_pct_float"],
        "short_interest_signal_score": short_interest_signals["short_interest_signal_score"],
        "institutional_ownership_delta_pct": institutional_ownership_signals["institutional_ownership_delta_pct"],
        "institutional_accumulation_score": institutional_ownership_signals["institutional_accumulation_score"],
        "insider_buy_count_90d": insider_activity_signals["insider_buy_count_90d"],
        "insider_buy_value_90d": insider_activity_signals["insider_buy_value_90d"],
        "insider_buy_cluster_count_90d": insider_activity_signals["insider_buy_cluster_count_90d"],
        "insider_sell_value_90d": insider_activity_signals["insider_sell_value_90d"],
        "insider_accumulation_score": insider_activity_signals["insider_accumulation_score"],
        "manual_verdict": str(universe_row.get("manual_verdict") or ""),
        "feature_json": json.dumps(feature_json, ensure_ascii=True, sort_keys=True),
    }


def upsert_features(conn: sqlite3.Connection, rows: list[dict[str, Any]], asof_date: str) -> None:
    ensure_table_optional_columns(conn, "daily_features", DAILY_FEATURES_OPTIONAL_COLUMNS)
    now = utc_now()
    fields = [
        "asof_date",
        "company_id",
        "catalyst_score_raw",
        "credibility_score_raw",
        "financial_quality_score_raw",
        "risk_score_raw",
        "legacy_risk_score_raw",
        "risk_penalty_input_score_raw",
        "predictive_risk_penalty_input_score_raw",
        "uncompensated_risk_score_raw",
        "compensated_risk_score_raw",
        "liquidity_risk_score_raw",
        "financing_survival_risk_score_raw",
        "governance_filing_risk_score_raw",
        "regulatory_setback_risk_score_raw",
        "pipeline_anchor_risk_score_raw",
        "collaborator_dependency_risk_score_raw",
        "trial_staleness_risk_score_raw",
        "indication_success_area",
        "indication_success_probability",
        "indication_success_multiplier",
        "indication_weighted_phase2_3_component",
        "forward_catalyst_nearest_days",
        "forward_catalyst_event_type",
        "forward_catalyst_score",
        "short_interest_pct_float",
        "short_interest_signal_score",
        "institutional_ownership_delta_pct",
        "institutional_accumulation_score",
        "insider_buy_count_90d",
        "insider_buy_value_90d",
        "insider_buy_cluster_count_90d",
        "insider_sell_value_90d",
        "insider_accumulation_score",
        "momentum_score_raw",
        "feature_json",
    ]
    placeholders = ", ".join("?" for _ in [*fields, "created_at", "updated_at"])
    with conn:
        conn.execute("DELETE FROM daily_features WHERE asof_date = ?", (asof_date,))
        conn.executemany(
            f"""
            INSERT INTO daily_features(
                {", ".join(fields)}, created_at, updated_at
            )
            VALUES ({placeholders})
            """,
            [
                tuple(row.get(field) for field in fields) + (now, now)
                for row in rows
            ],
        )


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FEATURE_CSV_FIELDNAMES, lineterminator="\n", extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    configure_logging()
    args = parse_args()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    base_dir = config_path.parent
    asof_date = parse_date(args.asof) if args.asof else datetime.now(timezone.utc).date()
    if asof_date is None:
        raise ValueError(f"Invalid --asof date: {args.asof}")
    db_path = args.db.expanduser().resolve() if args.db else resolve_path(cfg_get(config, "paths.database_path"), base_dir=base_dir)
    output_dir = resolve_path(cfg_get(config, "biotech_features.output_dir", "../output/biotech_index_reports"), base_dir=base_dir)
    universe_csv = resolve_path(cfg_get(config, "biotech_features.final_scoring_universe_csv"), base_dir=base_dir)
    evidence_csv = resolve_path(cfg_get(config, "biotech_features.ctgov_evidence_csv"), base_dir=base_dir)
    trial_status_overrides_csv = resolve_optional_path(cfg_get(config, "ctgov_audit.trial_status_overrides_csv"), base_dir=base_dir)
    category_overrides_csv = resolve_optional_path(
        cfg_get(config, "biotech_features.company_strategy_overrides_csv"),
        base_dir=base_dir,
    )
    forward_catalyst_calendar_csv = resolve_optional_path(
        cfg_get(config, "biotech_features.forward_catalyst_calendar_csv"),
        base_dir=base_dir,
    )
    short_interest_csv = resolve_optional_path(
        cfg_get(config, "biotech_features.short_interest_csv"),
        base_dir=base_dir,
    )
    institutional_ownership_csv = resolve_optional_path(
        cfg_get(config, "biotech_features.institutional_ownership_csv"),
        base_dir=base_dir,
    )
    screen_csv = resolve_path(cfg_get(config, "biotech_features.screen_results_csv"), base_dir=base_dir)
    output_csv = output_dir / str(cfg_get(config, "biotech_features.output_csv", "biotech_daily_features.csv"))
    min_liquidity = float(cfg_get(config, "biotech_features.min_liquidity_addv20", 1_000_000))
    low_liquidity = float(cfg_get(config, "biotech_features.low_liquidity_addv20", 2_000_000))
    strong_liquidity = float(cfg_get(config, "biotech_features.strong_liquidity_addv20", 10_000_000))
    sec_decay_cfg = cfg_get(config, "biotech_features.sec_event_recency_decay", {}) or {}
    if not isinstance(sec_decay_cfg, dict):
        sec_decay_cfg = {}
    sec_catalyst_recency_decay_enabled = as_bool(sec_decay_cfg.get("enabled", True))
    sec_catalyst_half_life_days = max(1.0, float(sec_decay_cfg.get("half_life_days", 90.0)))
    pipeline_quality_settings = load_pipeline_quality_settings(config)
    sec_catalyst_event_weights = load_sec_catalyst_event_weights(config)
    risk_decomposition_settings = load_risk_decomposition_settings(config)
    indication_success_settings = load_indication_success_settings(config)
    forward_catalyst_lookahead_days = int(
        cfg_get(config, "biotech_features.forward_catalyst_calendar_lookahead_days", 365)
    )
    going_concern_source_priority = configured_source_priority(
        cfg_get(config, "financial_survival.going_concern_source_priority", ["db", "csv"]),
        ["db", "csv"],
    )
    survival_score_blend_weight = bounded_float(
        cfg_get(config, "financial_survival.survival_score_blend_weight", 0.55),
        0.55,
        low=0.0,
        high=1.0,
    )
    survival_max_staleness_days = int(
        cfg_get(
            config,
            "biotech_features.financial_survival_max_staleness_days",
            cfg_get(config, "biotech_refresh.max_upstream_staleness_days", 2),
        )
    )

    universe = read_csv(universe_csv)
    evidence_df = read_csv(evidence_csv)
    evidence_df = apply_trial_status_overrides(evidence_df, read_optional_csv(trial_status_overrides_csv))
    screen = read_optional_csv(screen_csv)
    if screen.empty:
        LOGGER.warning(
            "Screen results CSV missing or empty; using market/SEC fallback fields for biotech features: %s",
            screen_csv,
        )
    category_overrides = load_company_strategy_overrides(category_overrides_csv)
    universe = universe[universe["scoring_include"].map(as_bool)].copy()
    universe_records = cast(list[dict[str, Any]], cast(Any, universe).to_dict("records"))
    normalized_universe = [(normalize_ticker(row.get("ticker")), row) for row in universe_records]
    expected_tickers = {ticker for ticker, _ in normalized_universe if ticker}
    if not expected_tickers:
        raise ValueError(f"No scoring_include tickers found in final scoring universe CSV: {universe_csv}")
    screen_by_ticker = {
        ticker: row
        for ticker, row in (
            (normalize_ticker(row.get("ticker")), row)
            for row in cast(list[dict[str, Any]], cast(Any, screen).to_dict("records"))
        )
        if ticker
    }
    evidence_by_ticker = build_evidence_summary_index(evidence_df, pipeline_quality_settings)
    forward_catalysts_by_ticker = load_forward_catalyst_calendar(
        forward_catalyst_calendar_csv,
        asof_date,
        lookahead_days=forward_catalyst_lookahead_days,
    )
    short_interest_by_ticker = load_ticker_feature_csv(short_interest_csv)
    institutional_ownership_by_ticker = load_ticker_feature_csv(institutional_ownership_csv)

    sqlite_timeout_sec = float(cfg_get(config, "runtime.sqlite_timeout_sec", 30.0))
    run_id: int | None = None
    with connect(db_path, timeout_sec=sqlite_timeout_sec) as conn:
        init_db(conn)
        try:
            run_id = start_run(conn, run_type="build_biotech_features", input_path=universe_csv)
            company_ids = load_company_ids(conn)
            survival_features = load_latest_survival_features(conn, asof_date)
            market_source_priority = scoring_market_sources(config)
            market_features = load_latest_market_features(
                conn,
                asof_date,
                source_priority=market_source_priority,
                max_staleness_days=int(cfg_get(config, "biotech_refresh.max_upstream_staleness_days", 2)),
            )
            governance_features = load_latest_governance_features(conn, asof_date)
            LOGGER.info("Biotech feature market source priority: %s", ",".join(market_source_priority))
            sec_filing_summary = load_recent_sec_filing_summary(
                conn,
                asof_date,
                lookback_days=int(cfg_get(config, "sec_event_parser.lookback_days", 730)),
            )
            sec_event_summary = load_recent_sec_event_summary(
                conn,
                asof_date,
                lookback_days=int(cfg_get(config, "sec_event_parser.lookback_days", 730)),
                sec_catalyst_half_life_days=sec_catalyst_half_life_days,
                sec_catalyst_event_weights=sec_catalyst_event_weights,
            )
            rows: list[dict[str, Any]] = []
            skipped: list[str] = []
            for ticker, universe_row in normalized_universe:
                if not ticker:
                    continue
                company_id = company_ids.get(ticker)
                if company_id is None:
                    skipped.append(ticker)
                    continue
                market = market_features.get(company_id)
                rows.append(
                    compute_feature_row(
                        universe_row=universe_row,
                        screen_row=screen_row_with_fallbacks(
                            screen_by_ticker.get(ticker),
                            market=market,
                            sec_filings=sec_filing_summary.get(company_id),
                        ),
                        evidence=evidence_by_ticker.get(ticker, empty_evidence_summary()),
                        company_id=company_id,
                        asof_date=asof_date,
                        min_liquidity_addv20=min_liquidity,
                        low_liquidity_addv20=low_liquidity,
                        strong_liquidity_addv20=strong_liquidity,
                        category_overrides=category_overrides,
                        going_concern_source_priority=going_concern_source_priority,
                        survival_score_blend_weight=survival_score_blend_weight,
                        core_pipeline_quality_multiplier=pipeline_quality_settings["core_pipeline_quality_multiplier"],
                        sec_catalyst_event_weights=sec_catalyst_event_weights,
                        risk_decomposition_settings=risk_decomposition_settings,
                        sec_catalyst_recency_decay_enabled=sec_catalyst_recency_decay_enabled,
                        sec_catalyst_half_life_days=sec_catalyst_half_life_days,
                        market=market,
                        survival=survival_features.get(company_id),
                        sec_events=sec_event_summary.get(company_id),
                        indication_success_settings={
                            **indication_success_settings,
                            "forward_catalyst_lookahead_days": forward_catalyst_lookahead_days,
                        },
                        forward_catalyst=forward_catalysts_by_ticker.get(ticker),
                        short_interest=short_interest_by_ticker.get(ticker),
                        institutional_ownership=institutional_ownership_by_ticker.get(ticker),
                        governance=governance_features.get(company_id),
                    )
                )
            validate_nonempty_selection(count=len(rows), context="biotech feature build")
            if skipped:
                raise RuntimeError(
                    "biotech feature build missing active DB company_id for final-universe ticker(s): "
                    + ",".join(sorted(skipped)[:25])
                    + (f"...(+{len(skipped) - 25})" if len(skipped) > 25 else "")
                )
            validate_full_universe_coverage(
                expected_tickers=expected_tickers,
                observed_tickers=[row["ticker"] for row in rows],
                context="biotech feature build",
                subset_mode=False,
            )
            validate_layer_freshness(
                base_rows=rows,
                layer_rows_by_company=survival_features,
                asof_date=asof_date,
                context="biotech feature build financial_survival_features",
                max_staleness_days=survival_max_staleness_days,
            )
            upsert_features(conn, rows, asof_date.isoformat())
            write_csv(output_csv, rows)
            LOGGER.info("Built biotech features: rows=%d output=%s", len(rows), output_csv)
            finish_run(
                conn,
                run_id=run_id,
                status="success",
                row_count=len(rows),
                message=f"skipped_missing_company_id={len(skipped)} output={output_csv}",
            )
        except BaseException as exc:
            if run_id is not None and not (isinstance(exc, SystemExit) and exc.code in (0, None)):
                finish_run(conn, run_id=run_id, status="failed", row_count=0, message=f"{type(exc).__name__}: {exc}")
            raise


if __name__ == "__main__":
    main()
