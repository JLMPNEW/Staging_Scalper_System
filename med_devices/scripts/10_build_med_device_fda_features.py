#!/usr/bin/env python3
from __future__ import annotations

import argparse
import calendar
import csv
import hashlib
import json
import logging
import math
import re
import sys
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from med_devices.core.config import cfg_get, load_yaml, resolve_path  # noqa: E402
from med_devices.core.db import connect, finish_run, init_db, quote_identifier, start_run, utc_now  # noqa: E402
from med_devices.core.logging_utils import configure_utc_logging  # noqa: E402
from med_devices.core.fda_states import MANUAL_FDA_REVIEW_STATES, normalize_fda_state  # noqa: E402
from med_devices.core.text_norm import normalize_ticker  # noqa: E402


LOGGER = logging.getLogger("build_med_device_fda_features")
DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
NON_DATA_QUALITY_REVIEW_REASONS = {
    "duplicate_cleanup_required",
    "mapping_review_required",
    "manual_review_required",
    "no_mapped_fda_records",
    "regulatory_review_required",
    "regulatory_watch",
    "recent_class_i_recall_watch",
    "recent_death_adverse_event_watch",
    "low_fda_mapping_confidence_watch",
    "manual_fda_device_footprint_no_mapped_events",
    "manual_fda_ivd_lab_footprint_no_mapped_events",
    "manual_fda_infrastructure_or_indirect_footprint",
    "manual_fda_non_cdrh_or_service_footprint",
}
FDA_SIGNAL_LEGACY_BROAD = "legacy_broad"
FDA_SIGNAL_CALIBRATED_ALPHA = "calibrated_alpha"
FDA_SIGNAL_RISK_VETO_ONLY = "risk_veto_only"
FDA_SIGNAL_NEUTRAL_OVERLAY = "neutral_overlay"
FDA_SIGNAL_DISABLED = "disabled"
FDA_SIGNAL_MODES = {
    FDA_SIGNAL_LEGACY_BROAD,
    FDA_SIGNAL_CALIBRATED_ALPHA,
    FDA_SIGNAL_RISK_VETO_ONLY,
    FDA_SIGNAL_NEUTRAL_OVERLAY,
    FDA_SIGNAL_DISABLED,
}
FDA_DIRECTION_POSITIVE = "positive"
FDA_DIRECTION_INVERSE = "inverse"
FDA_DIRECTION_NEUTRAL = "neutral"
FDA_DIRECTIONS = {FDA_DIRECTION_POSITIVE, FDA_DIRECTION_INVERSE, FDA_DIRECTION_NEUTRAL}
OPTIONAL_FDA_FEATURE_COLUMNS = {
    "calibration_cohort": "TEXT DEFAULT ''",
    "fda_product_score_legacy": "REAL DEFAULT 0.0",
    "fda_alpha_score": "REAL DEFAULT 0.0",
    "fda_safety_score": "REAL DEFAULT 0.0",
    "fda_clearance_velocity_raw": "REAL DEFAULT 0.0",
    "fda_clearance_velocity_score": "REAL DEFAULT 50.0",
    "fda_clearance_acceleration_raw": "REAL DEFAULT 0.0",
    "fda_clearance_acceleration_score": "REAL DEFAULT 50.0",
    "fda_evidence_quality_score": "REAL DEFAULT 0.0",
    "fda_event_risk_score": "REAL DEFAULT 0.0",
    "fda_event_risk_breadth_adjusted_score": "REAL DEFAULT 0.0",
    "fda_safety_breadth_adjusted_score": "REAL DEFAULT 50.0",
    "fda_distinct_device_category_count": "INTEGER DEFAULT 0",
    "fda_recall_count_raw": "INTEGER DEFAULT 0",
    "fda_recall_count_per_category": "REAL DEFAULT 0.0",
    "fda_class_i_recall_count": "INTEGER DEFAULT 0",
    "fda_warning_letter_count_36m": "INTEGER DEFAULT 0",
    "fda_mdr_death_injury_count_24m": "INTEGER DEFAULT 0",
    "fda_mdr_malfunction_count_24m": "INTEGER DEFAULT 0",
    "fda_mdr_malfunction_count_per_category": "REAL DEFAULT 0.0",
    "fda_breadth_adjustment_applied": "INTEGER DEFAULT 0",
    "fda_signal_mode": "TEXT DEFAULT ''",
    "fda_signal_direction": "TEXT DEFAULT ''",
    "fda_signal_reliability": "REAL DEFAULT 0.0",
    "fda_policy_reason": "TEXT DEFAULT ''",
    "class_i_multi_source_recall_count_36m": "INTEGER DEFAULT 0",
}
FIELDNAMES = [
    "asof_date",
    "company_id",
    "ticker",
    "company_name",
    "calibration_cohort",
    "approval_count_12m",
    "approval_count_24m",
    "approval_count_36m",
    "pma_count_36m",
    "product_code_count_36m",
    "recall_count_24m",
    "recall_count_36m",
    "class_i_recall_count_36m",
    "dedup_class_i_recall_count_36m",
    "class_i_multi_source_recall_count_36m",
    "open_class_i_recall_count_12m",
    "open_class_i_recall_count_36m",
    "terminated_class_i_recall_count_36m",
    "canonical_recall_duplicate_source_count",
    "recall_severity_36m",
    "death_count_24m",
    "injury_count_24m",
    "malfunction_count_24m",
    "revenue_ttm",
    "recall_severity_per_billion_revenue",
    "adverse_event_rate_per_billion_revenue",
    "fda_data_available",
    "latest_fda_event_date",
    "fda_data_recency_score",
    "mapped_manufacturer_count",
    "avg_mapping_confidence",
    "risk_mapping_confidence_min",
    "regulatory_innovation_score",
    "regulatory_risk_score",
    "fda_product_score",
    "fda_product_score_legacy",
    "fda_alpha_score",
    "fda_safety_score",
    "fda_clearance_velocity_raw",
    "fda_clearance_velocity_score",
    "fda_clearance_acceleration_raw",
    "fda_clearance_acceleration_score",
    "fda_evidence_quality_score",
    "fda_event_risk_score",
    "fda_event_risk_breadth_adjusted_score",
    "fda_safety_breadth_adjusted_score",
    "fda_distinct_device_category_count",
    "fda_recall_count_raw",
    "fda_recall_count_per_category",
    "fda_class_i_recall_count",
    "fda_warning_letter_count_36m",
    "fda_mdr_death_injury_count_24m",
    "fda_mdr_malfunction_count_24m",
    "fda_mdr_malfunction_count_per_category",
    "fda_breadth_adjustment_applied",
    "fda_signal_mode",
    "fda_signal_direction",
    "fda_signal_reliability",
    "fda_policy_reason",
    "raw_fda_red_flag",
    "confirmed_hard_red_flag",
    "hard_red_flag",
    "hard_red_flag_reasons",
    "review_adjusted_fda_state",
    "review_reason",
    "clearance_metrics_suppressed",
    "clearance_metrics_suppression_reason",
    "approval_product_code_filter",
    "approval_product_code_filter_note",
    "fda_evidence_type",
    "regulatory_stage",
    "evidence_confidence",
    "next_review_date",
    "manual_evidence_note",
]

FDA_HARD_RED_REVIEW_FIELDNAMES = [
    "ticker",
    "company_name",
    "company_id",
    "fda_manufacturer_id",
    "manufacturer_name",
    "mapping_confidence",
    "mapping_method",
    "recall_number",
    "event_id",
    "canonical_recall_key",
    "source_endpoints",
    "classification",
    "severity_weight",
    "status",
    "is_open",
    "is_terminated",
    "recall_initiation_date",
    "center_classification_date",
    "termination_date",
    "product_code",
    "product_description",
    "device_name",
    "reason_for_recall",
    "affected_units",
    "death_count_linked",
    "injury_count_linked",
    "maude_event_count_same_product_code",
    "revenue_ttm",
    "segment_revenue",
    "estimated_revenue_at_risk",
    "revenue_at_risk_pct",
    "raw_trigger_reason",
    "dedup_trigger_reason",
    "recommended_state",
    "analyst_review_status",
    "analyst_note",
]


@dataclass(frozen=True)
class Company:
    company_id: int
    ticker: str
    company_name: str
    calibration_cohort: str = ""


@dataclass
class FdaFeatureRow:
    asof_date: str
    company_id: int
    ticker: str
    company_name: str
    calibration_cohort: str = ""
    approval_count_12m: int = 0
    approval_count_24m: int = 0
    approval_count_36m: int = 0
    pma_count_36m: int = 0
    product_code_count_36m: int = 0
    recall_count_24m: int = 0
    recall_count_36m: int = 0
    class_i_recall_count_36m: int = 0
    dedup_class_i_recall_count_36m: int = 0
    class_i_multi_source_recall_count_36m: int = 0
    open_class_i_recall_count_12m: int = 0
    open_class_i_recall_count_36m: int = 0
    terminated_class_i_recall_count_36m: int = 0
    canonical_recall_duplicate_source_count: int = 0
    recall_severity_36m: float = 0.0
    class_i_recall_severity_36m: float = 0.0
    death_count_24m: int = 0
    injury_count_24m: int = 0
    malfunction_count_24m: int = 0
    prev_death_count_24m: int = 0
    prev_injury_count_24m: int = 0
    prev_malfunction_count_24m: int = 0
    prev_adverse_event_count_24m: int = 0
    current_adverse_event_count_24m: int = 0
    revenue_ttm: float | None = None
    recall_severity_per_billion_revenue: float | None = None
    adverse_event_rate_per_billion_revenue: float | None = None
    fda_data_available: int = 0
    latest_fda_event_date: str = ""
    fda_data_recency_score: float | None = None
    mapped_manufacturer_count: int = 0
    avg_mapping_confidence: float | None = None
    risk_mapping_confidence_min: float | None = None
    regulatory_innovation_score: float = 0.0
    regulatory_risk_score: float = 0.0
    fda_product_score: float = 0.0
    fda_product_score_legacy: float = 0.0
    fda_alpha_score: float = 0.0
    fda_safety_score: float = 0.0
    fda_clearance_velocity_raw: float | None = None
    fda_clearance_velocity_score: float = 50.0
    fda_clearance_acceleration_raw: float | None = None
    fda_clearance_acceleration_score: float = 50.0
    fda_evidence_quality_score: float = 0.0
    fda_event_risk_score: float = 0.0
    fda_event_risk_breadth_adjusted_score: float = 0.0
    fda_safety_breadth_adjusted_score: float = 50.0
    fda_distinct_device_category_count: int = 0
    fda_recall_count_raw: int = 0
    fda_recall_count_per_category: float = 0.0
    fda_class_i_recall_count: int = 0
    fda_warning_letter_count_36m: int = 0
    fda_mdr_death_injury_count_24m: int = 0
    fda_mdr_malfunction_count_24m: int = 0
    fda_mdr_malfunction_count_per_category: float = 0.0
    fda_breadth_adjustment_applied: int = 0
    fda_signal_mode: str = FDA_SIGNAL_LEGACY_BROAD
    fda_signal_direction: str = FDA_DIRECTION_POSITIVE
    fda_signal_reliability: float = 1.0
    fda_policy_reason: str = ""
    raw_fda_red_flag: int = 0
    confirmed_hard_red_flag: int = 0
    hard_red_flag: int = 0
    hard_red_flag_reasons: list[str] | None = None
    review_adjusted_fda_state: str = "cleared"
    review_reason: str = ""
    clearance_metrics_suppressed: int = 0
    clearance_metrics_suppression_reason: str = ""
    approval_product_code_filter: str = ""
    approval_product_code_filter_note: str = ""
    fda_evidence_type: str = ""
    regulatory_stage: str = ""
    evidence_confidence: float | None = None
    next_review_date: str = ""
    manual_evidence_note: str = ""
    payload: dict[str, Any] | None = None


@dataclass(frozen=True)
class FdaFeaturePolicy:
    source_id: str
    short_months: int
    medium_months: int
    long_months: int
    no_data_innovation_score: float
    no_data_risk_score: float
    revenue_floor: float
    recall_decay_half_life_days: float
    innovation_base_score: float
    innovation_approval_log_weight: float
    innovation_pma_log_weight: float
    innovation_product_code_log_weight: float
    risk_recall_severity_weight: float
    risk_class_i_recall_weight: float
    risk_death_per_billion_weight: float
    risk_injury_per_billion_weight: float
    risk_malfunction_per_billion_weight: float
    risk_adverse_acceleration_per_billion_weight: float
    min_mapping_confidence: float
    class_i_lookback_months: int
    death_lookback_months: int
    death_event_min_count: int
    class_i_hard_min_count: int
    class_i_hard_min_severity_per_billion: float
    death_event_hard_min_count: int
    death_event_min_rate_per_billion: float
    low_mapping_confidence_is_hard_red: bool
    regulatory_risk_weight: float
    regulatory_innovation_weight: float
    alpha_neutral_score: float = 50.0
    evidence_quality_data_weight: float = 0.40
    evidence_quality_mapping_weight: float = 0.35
    evidence_quality_recency_weight: float = 0.25
    hard_red_alpha_cap: float = 20.0
    review_required_alpha_cap: float = 35.0
    regulatory_watch_alpha_cap: float = 50.0
    no_data_default_score: float = 50.0
    mapping_confirmed_min_confidence: float = 95.0
    open_class_i_12m_confirmed_min_count: int = 1
    open_class_i_36m_confirmed_min_count: int = 2
    innovation_approval_12m_log_weight: float = 24.0
    breadth_adjustment_min_device_categories: int = 3
    warning_letter_event_weight: float = 20.0


@dataclass(frozen=True)
class FdaSignalProfile:
    mode: str = FDA_SIGNAL_CALIBRATED_ALPHA
    reliability: float = 0.35
    innovation_direction: str = FDA_DIRECTION_NEUTRAL
    safety_direction: str = FDA_DIRECTION_POSITIVE
    innovation_weight: float = 0.35
    safety_weight: float = 0.45
    evidence_weight: float = 0.20
    no_data_score: float | None = None
    rationale: str = ""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build med-device FDA/product risk feature rows.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--output-csv", type=Path, default=None)
    parser.add_argument("--asof", type=str, default="")
    parser.add_argument("--tickers", type=str, default="")
    parser.add_argument("--max-tickers", type=int, default=0)
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


def clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    if not math.isfinite(value):
        return low
    return max(low, min(high, value))


def months_before(asof: date, months: int) -> date:
    month = asof.month - months
    year = asof.year + (month - 1) // 12
    month = (month - 1) % 12 + 1
    day = min(asof.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


def cfg_float(config: dict[str, Any], dotted_key: str, default: float) -> float:
    value = to_float(cfg_get(config, dotted_key, default))
    if value is None:
        raise ValueError(f"Config value must be numeric: {dotted_key}")
    return value


def csv_bool(raw: object, default: int) -> int:
    text = str(raw or "").strip().lower()
    if not text:
        return default
    return 1 if text in {"1", "true", "yes", "y", "on"} else 0


def read_csv_flexible(path: Path) -> list[dict[str, str]]:
    last_error: Exception | None = None
    for encoding in ("utf-8-sig", "utf-8", "cp1252"):
        try:
            with path.open("r", encoding=encoding, newline="") as handle:
                reader = csv.DictReader(handle)
                if reader.fieldnames is None:
                    raise ValueError(f"CSV has no header: {path}")
                return [{str(key): str(value or "") for key, value in row.items()} for row in reader]
        except UnicodeDecodeError as exc:
            last_error = exc
    raise ValueError(f"Could not decode CSV {path}: {last_error}")


def row_get(row: dict[str, str], *keys: str) -> str:
    lowered = {str(key).strip().lower(): str(value or "") for key, value in row.items()}
    for key in keys:
        value = row.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
        value = lowered.get(key.lower())
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def split_code_set(raw: object) -> set[str]:
    out: set[str] = set()
    for item in re.split(r"[;|,]", str(raw or "")):
        value = re.sub(r"[^A-Z0-9]+", "", item.upper().strip())
        if value:
            out.add(value)
    return out


def fda_feature_policy(config: dict[str, Any]) -> FdaFeaturePolicy:
    if cfg_get(config, "fda_features.recall_severity_weights", None) is not None:
        LOGGER.warning(
            "Config key fda_features.recall_severity_weights is ignored by script 10; "
            "recall severity weights are applied during FDA core ingestion."
        )
    risk_weight = cfg_float(config, "fda_features.score_weights.regulatory_risk", 0.60)
    innovation_weight = cfg_float(config, "fda_features.score_weights.regulatory_innovation", 0.40)
    if risk_weight < 0 or innovation_weight < 0 or abs((risk_weight + innovation_weight) - 1.0) > 0.0001:
        raise ValueError("fda_features.score_weights must be non-negative and sum to 1.0")
    return FdaFeaturePolicy(
        source_id=str(cfg_get(config, "fda_features.source_id", "openfda_device") or "openfda_device"),
        short_months=int(cfg_get(config, "fda_features.windows_months.short", 12)),
        medium_months=int(cfg_get(config, "fda_features.windows_months.medium", 24)),
        long_months=int(cfg_get(config, "fda_features.windows_months.long", 36)),
        no_data_innovation_score=cfg_float(config, "fda_features.no_data_innovation_score", 20.0),
        no_data_risk_score=cfg_float(config, "fda_features.no_data_risk_score", 65.0),
        revenue_floor=cfg_float(config, "fda_features.normalization.revenue_floor", 100000000.0),
        recall_decay_half_life_days=cfg_float(config, "fda_features.recall_decay_half_life_days", 730.0),
        innovation_base_score=cfg_float(config, "fda_features.innovation_score.base_score", 25.0),
        innovation_approval_12m_log_weight=cfg_float(config, "fda_features.innovation_score.approval_12m_log_weight", 24.0),
        innovation_approval_log_weight=cfg_float(config, "fda_features.innovation_score.approval_log_weight", 18.0),
        innovation_pma_log_weight=cfg_float(config, "fda_features.innovation_score.pma_log_weight", 16.0),
        innovation_product_code_log_weight=cfg_float(config, "fda_features.innovation_score.product_code_log_weight", 12.0),
        risk_recall_severity_weight=cfg_float(config, "fda_features.risk_penalties.recall_severity_per_billion_weight", 4.0),
        risk_class_i_recall_weight=cfg_float(config, "fda_features.risk_penalties.class_i_recall_weight", 20.0),
        risk_death_per_billion_weight=cfg_float(config, "fda_features.risk_penalties.death_per_billion_weight", 5.0),
        risk_injury_per_billion_weight=cfg_float(config, "fda_features.risk_penalties.injury_per_billion_weight", 0.5),
        risk_malfunction_per_billion_weight=cfg_float(config, "fda_features.risk_penalties.malfunction_per_billion_weight", 0.1),
        risk_adverse_acceleration_per_billion_weight=cfg_float(
            config,
            "fda_features.risk_penalties.adverse_acceleration_per_billion_weight",
            0.5,
        ),
        min_mapping_confidence=cfg_float(config, "fda_features.min_mapping_confidence_for_high_confidence", 75.0),
        class_i_lookback_months=int(cfg_get(config, "fda_features.hard_red_flags.class_i_recall_lookback_months", 36)),
        death_lookback_months=int(cfg_get(config, "fda_features.hard_red_flags.death_event_lookback_months", 24)),
        death_event_min_count=int(cfg_get(config, "fda_features.hard_red_flags.death_event_min_count", 1)),
        class_i_hard_min_count=int(cfg_get(config, "fda_features.hard_red_flags.class_i_recall_min_count", 5)),
        class_i_hard_min_severity_per_billion=cfg_float(
            config,
            "fda_features.hard_red_flags.class_i_recall_min_severity_per_billion_revenue",
            10.0,
        ),
        death_event_hard_min_count=int(cfg_get(config, "fda_features.hard_red_flags.death_event_hard_min_count", 3)),
        death_event_min_rate_per_billion=cfg_float(
            config,
            "fda_features.hard_red_flags.death_event_min_rate_per_billion_revenue",
            1.0,
        ),
        low_mapping_confidence_is_hard_red=str(
            cfg_get(config, "fda_features.hard_red_flags.low_mapping_confidence_is_hard_red", False)
        ).strip().lower()
        in {"1", "true", "yes", "y", "on"},
        regulatory_risk_weight=risk_weight,
        regulatory_innovation_weight=innovation_weight,
        alpha_neutral_score=cfg_float(config, "fda_features.alpha.neutral_score", 50.0),
        evidence_quality_data_weight=cfg_float(config, "fda_features.alpha.evidence_quality_data_weight", 0.40),
        evidence_quality_mapping_weight=cfg_float(config, "fda_features.alpha.evidence_quality_mapping_weight", 0.35),
        evidence_quality_recency_weight=cfg_float(config, "fda_features.alpha.evidence_quality_recency_weight", 0.25),
        hard_red_alpha_cap=cfg_float(config, "fda_features.alpha.hard_red_cap", 20.0),
        review_required_alpha_cap=cfg_float(config, "fda_features.alpha.review_required_cap", 35.0),
        regulatory_watch_alpha_cap=cfg_float(config, "fda_features.alpha.regulatory_watch_cap", 50.0),
        no_data_default_score=cfg_float(config, "fda_features.alpha.no_data_default_score", 50.0),
        mapping_confirmed_min_confidence=cfg_float(config, "fda_features.review_state.mapping_confirmed_min_confidence", 95.0),
        open_class_i_12m_confirmed_min_count=int(
            cfg_get(config, "fda_features.review_state.open_class_i_12m_confirmed_min_count", 1)
        ),
        open_class_i_36m_confirmed_min_count=int(
            cfg_get(config, "fda_features.review_state.open_class_i_36m_confirmed_min_count", 2)
        ),
        breadth_adjustment_min_device_categories=max(
            1,
            int(cfg_get(config, "fda_features.event_risk_breadth_adjustment.min_device_categories", 3)),
        ),
        warning_letter_event_weight=cfg_float(
            config,
            "fda_features.event_risk_breadth_adjustment.warning_letter_event_weight",
            20.0,
        ),
    )


def optional_float(raw: object, default: float | None, *, context: str) -> float | None:
    if raw is None or str(raw).strip() == "":
        return default
    value = to_float(raw)
    if value is None:
        raise ValueError(f"Config value must be numeric: {context}")
    return value


def normalized_signal_mode(raw: object, *, context: str) -> str:
    mode = str(raw or FDA_SIGNAL_CALIBRATED_ALPHA).strip().lower()
    aliases = {
        "legacy": FDA_SIGNAL_LEGACY_BROAD,
        "alpha": FDA_SIGNAL_CALIBRATED_ALPHA,
        "calibrated": FDA_SIGNAL_CALIBRATED_ALPHA,
        "risk_veto": FDA_SIGNAL_RISK_VETO_ONLY,
        "neutral": FDA_SIGNAL_NEUTRAL_OVERLAY,
        "overlay": FDA_SIGNAL_NEUTRAL_OVERLAY,
    }
    mode = aliases.get(mode, mode)
    if mode not in FDA_SIGNAL_MODES:
        raise ValueError(f"{context}.mode must be one of {sorted(FDA_SIGNAL_MODES)}, got {mode!r}")
    return mode


def normalized_signal_direction(raw: object, *, context: str) -> str:
    direction = str(raw or FDA_DIRECTION_NEUTRAL).strip().lower()
    if direction not in FDA_DIRECTIONS:
        raise ValueError(f"{context} must be one of {sorted(FDA_DIRECTIONS)}, got {direction!r}")
    return direction


def parse_fda_signal_profile(raw: object, *, default: FdaSignalProfile, context: str) -> FdaSignalProfile:
    if raw is None:
        return default
    if not isinstance(raw, dict):
        raise ValueError(f"{context} must be a mapping")
    mode = normalized_signal_mode(raw.get("mode", default.mode), context=context)
    reliability = optional_float(raw.get("reliability"), default.reliability, context=f"{context}.reliability")
    innovation_direction = normalized_signal_direction(
        raw.get("innovation_direction", default.innovation_direction),
        context=f"{context}.innovation_direction",
    )
    safety_direction = normalized_signal_direction(
        raw.get("safety_direction", default.safety_direction),
        context=f"{context}.safety_direction",
    )
    innovation_weight = optional_float(raw.get("innovation_weight"), default.innovation_weight, context=f"{context}.innovation_weight")
    safety_weight = optional_float(raw.get("safety_weight"), default.safety_weight, context=f"{context}.safety_weight")
    evidence_weight = optional_float(raw.get("evidence_weight"), default.evidence_weight, context=f"{context}.evidence_weight")
    no_data_score = optional_float(raw.get("no_data_score"), default.no_data_score, context=f"{context}.no_data_score")
    if reliability is None or not 0.0 <= reliability <= 1.0:
        raise ValueError(f"{context}.reliability must be in [0, 1]")
    weights = [innovation_weight or 0.0, safety_weight or 0.0, evidence_weight or 0.0]
    if any(weight < 0.0 for weight in weights) or sum(weights) <= 0.0:
        raise ValueError(f"{context} weights must be non-negative and have positive total")
    return FdaSignalProfile(
        mode=mode,
        reliability=float(reliability),
        innovation_direction=innovation_direction,
        safety_direction=safety_direction,
        innovation_weight=float(innovation_weight or 0.0),
        safety_weight=float(safety_weight or 0.0),
        evidence_weight=float(evidence_weight or 0.0),
        no_data_score=no_data_score,
        rationale=str(raw.get("rationale", default.rationale) or "").strip(),
    )


def default_fda_signal_profile(config: dict[str, Any]) -> FdaSignalProfile:
    default = FdaSignalProfile(
        mode=FDA_SIGNAL_CALIBRATED_ALPHA,
        reliability=cfg_float(config, "fda_features.alpha.default_reliability", 0.35),
        innovation_direction=str(
            cfg_get(config, "fda_features.alpha.default_innovation_direction", FDA_DIRECTION_NEUTRAL)
        ).strip().lower(),
        safety_direction=str(
            cfg_get(config, "fda_features.alpha.default_safety_direction", FDA_DIRECTION_POSITIVE)
        ).strip().lower(),
        innovation_weight=cfg_float(config, "fda_features.alpha.default_innovation_weight", 0.35),
        safety_weight=cfg_float(config, "fda_features.alpha.default_safety_weight", 0.45),
        evidence_weight=cfg_float(config, "fda_features.alpha.default_evidence_weight", 0.20),
        no_data_score=None,
        rationale="default_shrunk_fda_alpha_profile",
    )
    return parse_fda_signal_profile(
        cfg_get(config, "fda_features.signal_profile", None),
        default=default,
        context="fda_features.signal_profile",
    )


def fda_signal_profiles(config: dict[str, Any], default_profile: FdaSignalProfile) -> dict[str, FdaSignalProfile]:
    raw_profiles = cfg_get(config, "fda_features.cohort_signal_profiles", {}) or {}
    if not isinstance(raw_profiles, dict):
        raise ValueError("fda_features.cohort_signal_profiles must be a mapping when provided")
    out: dict[str, FdaSignalProfile] = {}
    for cohort, raw_profile in raw_profiles.items():
        out[str(cohort)] = parse_fda_signal_profile(
            raw_profile,
            default=default_profile,
            context=f"fda_features.cohort_signal_profiles.{cohort}",
        )
    return out


def table_exists(conn: Any, table_name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ? LIMIT 1",
        (table_name,),
    ).fetchone()
    return row is not None


def latest_asof(conn: Any) -> str:
    row = conn.execute("SELECT MAX(asof_date) AS asof_date FROM feature_financial_valuation").fetchone()
    asof = str(row["asof_date"] or "") if row is not None else ""
    return asof or datetime.now(timezone.utc).date().isoformat()


def load_companies(conn: Any, *, ticker_filter: set[str], max_tickers: int) -> list[Company]:
    if table_exists(conn, "dim_company_model_taxonomy"):
        rows = conn.execute(
            """
            SELECT c.company_id, c.ticker, c.company_name,
                   COALESCE(t.calibration_cohort, '') AS calibration_cohort
            FROM dim_company c
            LEFT JOIN dim_company_model_taxonomy t ON t.company_id = c.company_id
            WHERE c.is_active = 1
            ORDER BY c.ticker
            """
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT company_id, ticker, company_name, '' AS calibration_cohort
            FROM dim_company
            WHERE is_active = 1
            ORDER BY ticker
            """
        ).fetchall()
    out: list[Company] = []
    for row in rows:
        ticker = normalize_ticker(row["ticker"])
        if ticker_filter and ticker not in ticker_filter:
            continue
        out.append(
            Company(
                int(row["company_id"]),
                ticker,
                str(row["company_name"] or ""),
                str(row["calibration_cohort"] or "").strip(),
            )
        )
        if max_tickers > 0 and len(out) >= max_tickers:
            break
    return out


def load_review_overrides(path: Path | None) -> dict[str, dict[str, str]]:
    if path is None:
        return {}
    if not path.exists():
        LOGGER.warning("Configured FDA review override CSV does not exist: %s", path)
        return {}
    out: dict[str, dict[str, str]] = {}
    for row in read_csv_flexible(path):
        ticker = normalize_ticker(row_get(row, "ticker", "symbol"))
        if not ticker:
            continue
        out[ticker] = row
    LOGGER.info("Loaded FDA regulatory review overrides: rows=%d path=%s", len(out), path)
    return out


def load_footprint_overrides(path: Path | None) -> dict[str, dict[str, str]]:
    if path is None:
        return {}
    if not path.exists():
        LOGGER.warning("Configured FDA footprint CSV does not exist: %s", path)
        return {}
    out: dict[str, dict[str, str]] = {}
    for row in read_csv_flexible(path):
        ticker = normalize_ticker(row_get(row, "ticker", "symbol"))
        if not ticker:
            continue
        out[ticker] = row
    LOGGER.info("Loaded FDA footprint overrides: rows=%d path=%s", len(out), path)
    return out


def load_manual_footprint_evidence(path: Path | None) -> dict[str, dict[str, str]]:
    if path is None:
        return {}
    if not path.exists():
        LOGGER.warning("Configured FDA manual footprint evidence CSV does not exist: %s", path)
        return {}
    out: dict[str, dict[str, str]] = {}
    for row in read_csv_flexible(path):
        ticker = normalize_ticker(row_get(row, "ticker", "symbol"))
        if not ticker:
            continue
        out[ticker] = row
    LOGGER.info("Loaded FDA manual footprint evidence: rows=%d path=%s", len(out), path)
    return out


def update_latest_fda_event_date(row: FdaFeatureRow, event_date: str) -> None:
    if not event_date:
        return
    if not row.latest_fda_event_date or event_date > row.latest_fda_event_date:
        row.latest_fda_event_date = event_date


def update_risk_mapping_confidence(row: FdaFeatureRow, raw_confidence: object) -> None:
    confidence = to_float(raw_confidence)
    if confidence is None:
        return
    if row.risk_mapping_confidence_min is None or confidence < row.risk_mapping_confidence_min:
        row.risk_mapping_confidence_min = confidence


def safe_json_loads(raw: object) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    try:
        value = json.loads(str(raw or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def nested_field(payload: dict[str, Any], *names: str) -> str:
    for name in names:
        value = payload.get(name)
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def normalize_recall_key_text(raw: object) -> str:
    return re.sub(r"[^A-Z0-9]+", "", str(raw or "").upper()).strip()


def canonical_recall_key_from_row(item: Any) -> str:
    recall_number = normalize_recall_key_text(item["recall_number"])
    if recall_number:
        return f"recall_number:{recall_number}"
    event_id = normalize_recall_key_text(item["event_id"])
    if event_id:
        return f"event_id:{event_id}"
    payload = safe_json_loads(item["payload_json"])
    material = json.dumps(
        {
            "firm": normalize_recall_key_text(item["recalling_firm"]),
            "product": normalize_recall_key_text(nested_field(payload, "product_description", "device_name")),
            "date": str(item["recall_initiation_date"] or item["center_classification_date"] or ""),
            "reason": normalize_recall_key_text(item["reason_for_recall"]),
        },
        ensure_ascii=True,
        sort_keys=True,
    )
    return f"hash:{hashlib.sha256(material.encode('utf-8')).hexdigest()}"


def source_endpoint_from_row(item: Any) -> str:
    if "endpoint_name" in item.keys():
        endpoint = str(item["endpoint_name"] or "").strip()
        if endpoint:
            return endpoint
    key = str(item["recall_key"] or "")
    prefix = key.split(":", 1)[0].strip()
    if prefix and prefix not in {"recall_number", "event_id", "hash"}:
        return prefix
    return str(item["source_id"] or "unknown")


def has_explicit_endpoint_name(item: Any) -> bool:
    return "endpoint_name" in item.keys() and bool(str(item["endpoint_name"] or "").strip())


def recall_source_identity(item: Any) -> tuple[str, str, str, str, str]:
    return (
        source_endpoint_from_row(item),
        normalize_recall_key_text(item["recall_number"]),
        normalize_recall_key_text(item["event_id"]),
        normalize_recall_key_text(item["product_code"]),
        str(item["recall_initiation_date"] or item["center_classification_date"] or ""),
    )


def dedupe_recall_source_items(items: list[Any]) -> tuple[list[Any], list[int]]:
    selected: dict[tuple[str, str, str, str, str], Any] = {}
    duplicate_ids: list[int] = []
    for item in items:
        identity = recall_source_identity(item)
        existing = selected.get(identity)
        if existing is None:
            selected[identity] = item
            continue
        existing_score = (
            1 if has_explicit_endpoint_name(existing) else 0,
            int(existing["fda_recall_id"]),
        )
        candidate_score = (
            1 if has_explicit_endpoint_name(item) else 0,
            int(item["fda_recall_id"]),
        )
        if candidate_score > existing_score:
            duplicate_ids.append(int(existing["fda_recall_id"]))
            selected[identity] = item
        else:
            duplicate_ids.append(int(item["fda_recall_id"]))
    return list(selected.values()), sorted(duplicate_ids)


def source_rank(source: object) -> int:
    text = str(source or "").lower()
    if "recall" in text and "enforcement" not in text:
        return 3
    if "enforcement" in text:
        return 2
    return 1


def is_terminated_status(status: object, termination_date: object) -> bool:
    if str(termination_date or "").strip():
        return True
    text = str(status or "").lower()
    return any(marker in text for marker in ("terminated", "complete", "completed", "closed"))


def recall_status_multiplier(status: object, termination_date: object, *, asof: date) -> float:
    termination_day = parse_date(termination_date)
    if termination_day is not None:
        days_since = max(0, (asof - termination_day).days)
        if days_since <= 365:
            return 0.35
        if days_since <= 730:
            return 0.20
        if days_since <= 1095:
            return 0.10
        return 0.0
    text = str(status or "").lower()
    if "complete" in text or "completed" in text:
        return 0.50
    if "correction" in text or "initiated" in text:
        return 0.75
    return 1.00


def refresh_canonical_recalls(conn: Any) -> int:
    raw_rows = conn.execute(
        """
        SELECT r.*, m.mapping_confidence, m.mapping_method
        FROM fact_fda_recall r
        LEFT JOIN dim_fda_manufacturer m
          ON m.fda_manufacturer_id = r.fda_manufacturer_id
        """
    ).fetchall()
    grouped: dict[str, list[Any]] = {}
    for item in raw_rows:
        grouped.setdefault(canonical_recall_key_from_row(item), []).append(item)

    now = utc_now()
    payload_rows: list[tuple[Any, ...]] = []
    for canonical_key, raw_items in grouped.items():
        items, duplicate_raw_ids = dedupe_recall_source_items(raw_items)
        ranked = sorted(
            items,
            key=lambda item: (
                0 if is_terminated_status(item["status"], item["termination_date"]) else 1,
                source_rank(source_endpoint_from_row(item)),
                str(item["termination_date"] or item["center_classification_date"] or item["recall_initiation_date"] or ""),
                float(item["mapping_confidence"] or 0.0),
            ),
            reverse=True,
        )
        selected = ranked[0]
        severity_item = max(items, key=lambda item: recall_severity_weight(item["classification"]))
        payload = safe_json_loads(selected["payload_json"])
        endpoints = sorted({source_endpoint_from_row(item) for item in items})
        manufacturer_item = max(items, key=lambda item: float(item["mapping_confidence"] or 0.0))
        is_terminated = 1 if is_terminated_status(selected["status"], selected["termination_date"]) else 0
        source_payload = {
            "canonical_recall_key": canonical_key,
            "source_fda_recall_ids": [int(item["fda_recall_id"]) for item in items],
            "duplicate_raw_fda_recall_ids": duplicate_raw_ids,
            "source_endpoints": endpoints,
            "selected_fda_recall_id": int(selected["fda_recall_id"]),
            "selected_payload": payload,
        }
        payload_rows.append(
            (
                canonical_key,
                selected["recall_number"],
                selected["event_id"],
                manufacturer_item["company_id"],
                manufacturer_item["fda_manufacturer_id"],
                selected["product_code"],
                severity_item["classification"],
                recall_severity_weight(severity_item["classification"]),
                selected["status"],
                0 if is_terminated else 1,
                is_terminated,
                selected["recall_initiation_date"],
                selected["center_classification_date"],
                selected["termination_date"],
                selected["recalling_firm"],
                nested_field(payload, "product_description", "device_name"),
                selected["reason_for_recall"],
                len(items),
                ";".join(endpoints),
                source_endpoint_from_row(selected),
                to_float(manufacturer_item["mapping_confidence"]),
                manufacturer_item["mapping_method"],
                json.dumps(source_payload, ensure_ascii=True, sort_keys=True),
                now,
                now,
            )
        )
    conn.execute("SAVEPOINT refresh_canonical_recalls")
    try:
        conn.execute("DELETE FROM fact_fda_recall_canonical")
        if payload_rows:
            conn.executemany(
                """
                INSERT INTO fact_fda_recall_canonical(
                    canonical_recall_key, recall_number, event_id, company_id, fda_manufacturer_id,
                    product_code, classification, max_severity_weight, status, is_open, is_terminated,
                    recall_initiation_date, center_classification_date, termination_date, recalling_firm,
                    product_description, reason_for_recall, source_count, source_endpoints, source_priority,
                    mapping_confidence, mapping_method, payload_json, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                payload_rows,
            )
        conn.execute("RELEASE SAVEPOINT refresh_canonical_recalls")
    except BaseException:
        try:
            conn.execute("ROLLBACK TO SAVEPOINT refresh_canonical_recalls")
        finally:
            conn.execute("RELEASE SAVEPOINT refresh_canonical_recalls")
        raise
    return len(payload_rows)


def preflight_fda_company_links(conn: Any) -> None:
    raw_total = 0
    linked_total = 0
    for table_name in ("fact_fda_approval", "fact_fda_recall", "fact_fda_adverse_event"):
        raw_row = conn.execute(f"SELECT COUNT(*) AS n FROM {table_name}").fetchone()
        linked_row = conn.execute(f"SELECT COUNT(*) AS n FROM {table_name} WHERE company_id IS NOT NULL").fetchone()
        raw_total += int(raw_row["n"] or 0) if raw_row is not None else 0
        linked_total += int(linked_row["n"] or 0) if linked_row is not None else 0
    if raw_total > 0 and linked_total == 0:
        raise RuntimeError("FDA core rows exist but no FDA-to-company links were found; run script 09 first.")


def count_approvals(
    conn: Any,
    row: FdaFeatureRow,
    *,
    asof: date,
    policy: FdaFeaturePolicy,
    include_product_codes: set[str] | None = None,
    exclude_product_codes: set[str] | None = None,
) -> None:
    long_start = months_before(asof, policy.long_months).isoformat()
    medium_start = months_before(asof, policy.medium_months).isoformat()
    short_start = months_before(asof, policy.short_months).isoformat()
    rows = conn.execute(
        """
        SELECT submission_type, product_code, decision_date
        FROM fact_fda_approval
        WHERE company_id = ?
          AND COALESCE(decision_date, '') != ''
          AND decision_date <= ?
          AND decision_date >= ?
        """,
        (row.company_id, asof.isoformat(), long_start),
    ).fetchall()
    product_codes: set[str] = set()
    for item in rows:
        product_code = str(item["product_code"] or "").strip().upper()
        if include_product_codes and product_code not in include_product_codes:
            continue
        if exclude_product_codes and product_code in exclude_product_codes:
            continue
        day = str(item["decision_date"] or "")
        if day >= short_start:
            row.approval_count_12m += 1
        if day >= medium_start:
            row.approval_count_24m += 1
        row.approval_count_36m += 1
        if "PMA" in str(item["submission_type"] or "").upper():
            row.pma_count_36m += 1
        if product_code:
            product_codes.add(product_code)
        update_latest_fda_event_date(row, day)
    row.product_code_count_36m = len(product_codes)


def is_class_i(classification: object) -> bool:
    text = re.sub(r"[^a-z0-9]+", "_", str(classification or "").strip().lower()).strip("_")
    return text in {"i", "class_i", "class_1", "classi", "class1"}


def recall_severity_weight(classification: object) -> float:
    text = re.sub(r"[^a-z0-9]+", "_", str(classification or "").strip().lower()).strip("_")
    if text in {"i", "class_i", "class_1", "classi", "class1"}:
        return 5.0
    if text in {"ii", "class_ii", "class_2", "classii", "class2"}:
        return 2.0
    if text in {"iii", "class_iii", "class_3", "classiii", "class3"}:
        return 0.5
    return 1.0


def count_recalls(conn: Any, row: FdaFeatureRow, *, asof: date, policy: FdaFeaturePolicy) -> None:
    long_start = months_before(asof, policy.long_months).isoformat()
    medium_start = months_before(asof, policy.medium_months).isoformat()
    short_start = months_before(asof, policy.short_months).isoformat()
    rows = conn.execute(
        """
        SELECT classification, COALESCE(max_severity_weight, 1.0) AS severity_weight,
               status, termination_date, is_open, is_terminated, source_count,
               mapping_confidence,
               COALESCE(recall_initiation_date, center_classification_date) AS event_date
        FROM fact_fda_recall_canonical
        WHERE company_id = ?
          AND COALESCE(recall_initiation_date, center_classification_date, '') != ''
          AND COALESCE(recall_initiation_date, center_classification_date) <= ?
          AND COALESCE(recall_initiation_date, center_classification_date) >= ?
        """,
        (row.company_id, asof.isoformat(), long_start),
    ).fetchall()
    for item in rows:
        event_date = str(item["event_date"] or "")
        if event_date >= medium_start:
            row.recall_count_24m += 1
        row.recall_count_36m += 1
        row.canonical_recall_duplicate_source_count += max(0, int(item["source_count"] or 1) - 1)
        event_day = parse_date(event_date)
        days_since = (asof - event_day).days if event_day is not None else 0
        decay = 0.5 ** (max(0, days_since) / max(1.0, policy.recall_decay_half_life_days))
        status_multiplier = recall_status_multiplier(item["status"], item["termination_date"], asof=asof)
        adjusted_severity = float(item["severity_weight"] or 1.0) * decay * status_multiplier
        row.recall_severity_36m += adjusted_severity
        if is_class_i(item["classification"]):
            row.class_i_recall_count_36m += 1
            row.class_i_recall_severity_36m += adjusted_severity
            if int(item["source_count"] or 1) >= 2:
                row.dedup_class_i_recall_count_36m += 1
                row.class_i_multi_source_recall_count_36m += 1
            update_risk_mapping_confidence(row, item["mapping_confidence"])
            if int(item["is_open"] or 0):
                row.open_class_i_recall_count_36m += 1
                if event_date >= short_start:
                    row.open_class_i_recall_count_12m += 1
            if int(item["is_terminated"] or 0):
                row.terminated_class_i_recall_count_36m += 1
        update_latest_fda_event_date(row, event_date)


def count_adverse_events(conn: Any, row: FdaFeatureRow, *, asof: date, policy: FdaFeaturePolicy) -> None:
    medium_start = months_before(asof, policy.medium_months)
    previous_start = months_before(asof, policy.medium_months * 2)
    rows = conn.execute(
        """
        SELECT e.report_date, e.death_count, e.injury_count, e.malfunction_count,
               m.mapping_confidence
        FROM fact_fda_adverse_event e
        LEFT JOIN dim_fda_manufacturer m
          ON m.fda_manufacturer_id = e.fda_manufacturer_id
        WHERE e.company_id = ?
          AND COALESCE(e.report_date, e.event_date, '') != ''
          AND COALESCE(e.report_date, e.event_date) <= ?
          AND COALESCE(e.report_date, e.event_date) >= ?
        """,
        (row.company_id, asof.isoformat(), previous_start.isoformat()),
    ).fetchall()
    for item in rows:
        event_day = parse_date(item["report_date"])
        if event_day is None:
            continue
        event_count = int(item["death_count"] or 0) + int(item["injury_count"] or 0) + int(item["malfunction_count"] or 0)
        if event_day >= medium_start:
            row.current_adverse_event_count_24m += event_count
            row.death_count_24m += int(item["death_count"] or 0)
            row.injury_count_24m += int(item["injury_count"] or 0)
            row.malfunction_count_24m += int(item["malfunction_count"] or 0)
            if int(item["death_count"] or 0) > 0:
                update_risk_mapping_confidence(row, item["mapping_confidence"])
        else:
            row.prev_adverse_event_count_24m += event_count
            row.prev_death_count_24m += int(item["death_count"] or 0)
            row.prev_injury_count_24m += int(item["injury_count"] or 0)
            row.prev_malfunction_count_24m += int(item["malfunction_count"] or 0)
        update_latest_fda_event_date(row, event_day.isoformat())


def count_device_categories(conn: Any, row: FdaFeatureRow, *, asof: date, policy: FdaFeaturePolicy) -> None:
    long_start = months_before(asof, policy.long_months).isoformat()
    rows = conn.execute(
        """
        WITH product_events AS (
            SELECT product_code
            FROM fact_fda_approval
            WHERE company_id = ?
              AND COALESCE(decision_date, '') != ''
              AND decision_date <= ?
              AND decision_date >= ?
            UNION
            SELECT product_code
            FROM fact_fda_recall_canonical
            WHERE company_id = ?
              AND COALESCE(recall_initiation_date, center_classification_date, '') != ''
              AND COALESCE(recall_initiation_date, center_classification_date) <= ?
              AND COALESCE(recall_initiation_date, center_classification_date) >= ?
            UNION
            SELECT product_code
            FROM fact_fda_adverse_event
            WHERE company_id = ?
              AND COALESCE(report_date, event_date, '') != ''
              AND COALESCE(report_date, event_date) <= ?
              AND COALESCE(report_date, event_date) >= ?
        )
        SELECT DISTINCT
               COALESCE(NULLIF(TRIM(p.medical_specialty), ''), NULLIF(TRIM(e.product_code), '')) AS device_category
        FROM product_events e
        LEFT JOIN dim_fda_product_code p
          ON p.product_code = e.product_code
        WHERE COALESCE(NULLIF(TRIM(p.medical_specialty), ''), NULLIF(TRIM(e.product_code), '')) IS NOT NULL
        """,
        (
            row.company_id,
            asof.isoformat(),
            long_start,
            row.company_id,
            asof.isoformat(),
            long_start,
            row.company_id,
            asof.isoformat(),
            long_start,
        ),
    ).fetchall()
    row.fda_distinct_device_category_count = len({str(item["device_category"] or "").strip() for item in rows if item["device_category"]})


def manufacturer_mapping_summary(conn: Any, row: FdaFeatureRow) -> None:
    rows = conn.execute(
        """
        SELECT DISTINCT m.fda_manufacturer_id, m.mapping_confidence
        FROM dim_fda_manufacturer m
        WHERE m.parent_company_id = ?
        """,
        (row.company_id,),
    ).fetchall()
    confidences = [float(item["mapping_confidence"] or 0.0) for item in rows]
    row.mapped_manufacturer_count = len(confidences)
    row.avg_mapping_confidence = round(sum(confidences) / len(confidences), 2) if confidences else None


def latest_revenue_ttm(conn: Any, company_id: int, *, asof: date) -> float | None:
    row = conn.execute(
        """
        SELECT revenue_ttm
        FROM feature_financial_valuation
        WHERE company_id = ?
          AND asof_date <= ?
        ORDER BY asof_date DESC
        LIMIT 1
        """,
        (company_id, asof.isoformat()),
    ).fetchone()
    return to_float(row["revenue_ttm"]) if row is not None else None


def revenue_normalizer(row: FdaFeatureRow, *, policy: FdaFeaturePolicy) -> float:
    revenue = row.revenue_ttm if row.revenue_ttm is not None and row.revenue_ttm > 0 else policy.revenue_floor
    return max(policy.revenue_floor, revenue) / 1_000_000_000.0


def shrink_to_neutral(score: float, *, neutral: float, reliability: float) -> float:
    return neutral + (score - neutral) * max(0.0, min(1.0, reliability))


def directional_score(score: float, direction: str, *, neutral: float) -> float:
    if direction == FDA_DIRECTION_POSITIVE:
        return score
    if direction == FDA_DIRECTION_INVERSE:
        return 100.0 - score
    return neutral


def apply_breadth_adjusted_event_risk(row: FdaFeatureRow, *, policy: FdaFeaturePolicy, revenue_base: float | None) -> None:
    row.fda_recall_count_raw = row.recall_count_36m
    row.fda_class_i_recall_count = row.class_i_recall_count_36m
    row.fda_warning_letter_count_36m = max(0, row.fda_warning_letter_count_36m)
    row.fda_mdr_death_injury_count_24m = row.death_count_24m + row.injury_count_24m
    row.fda_mdr_malfunction_count_24m = row.malfunction_count_24m
    if not row.fda_data_available or revenue_base is None or revenue_base <= 0:
        row.fda_event_risk_breadth_adjusted_score = row.fda_event_risk_score
        row.fda_safety_breadth_adjusted_score = row.fda_safety_score
        return

    category_count = max(0, row.fda_distinct_device_category_count)
    adjustment_applies = category_count >= policy.breadth_adjustment_min_device_categories
    scoring_divisor = float(category_count if adjustment_applies else 1)
    per_category_divisor = float(max(category_count, 1))
    lower_severity_recall_count = max(
        0,
        row.recall_count_36m - row.class_i_recall_count_36m,
    )
    row.fda_recall_count_per_category = round(lower_severity_recall_count / per_category_divisor, 4)
    row.fda_mdr_malfunction_count_per_category = round(row.malfunction_count_24m / per_category_divisor, 4)
    row.fda_breadth_adjustment_applied = int(adjustment_applies)

    lower_severity_recall_severity = max(0.0, row.recall_severity_36m - row.class_i_recall_severity_36m)
    adjusted_recall_severity = row.class_i_recall_severity_36m + (lower_severity_recall_severity / scoring_divisor)
    adjusted_recall_severity_rate = adjusted_recall_severity / revenue_base
    death_rate = row.death_count_24m / revenue_base
    injury_rate = row.injury_count_24m / revenue_base
    adjusted_malfunction_rate = (row.malfunction_count_24m / scoring_divisor) / revenue_base
    current_severe_mdr = row.death_count_24m + row.injury_count_24m
    previous_severe_mdr = row.prev_death_count_24m + row.prev_injury_count_24m
    severe_mdr_acceleration = max(0, current_severe_mdr - previous_severe_mdr)
    malfunction_acceleration = max(0, row.malfunction_count_24m - row.prev_malfunction_count_24m) / scoring_divisor
    adjusted_adverse_acceleration_rate = (severe_mdr_acceleration + malfunction_acceleration) / revenue_base
    # The current FDA ingestion schema has no canonical warning-letter source table,
    # so this is zero today. Keep the formula parameterized so it activates when
    # fda_warning_letter_count_36m is wired.
    warning_letter_penalty = row.fda_warning_letter_count_36m * policy.warning_letter_event_weight
    adjusted_regulatory_risk = round(
        clamp(
            100.0
            - adjusted_recall_severity_rate * policy.risk_recall_severity_weight
            - row.class_i_recall_count_36m * policy.risk_class_i_recall_weight
            - warning_letter_penalty
            - death_rate * policy.risk_death_per_billion_weight
            - injury_rate * policy.risk_injury_per_billion_weight
            - adjusted_malfunction_rate * policy.risk_malfunction_per_billion_weight
            - adjusted_adverse_acceleration_rate * policy.risk_adverse_acceleration_per_billion_weight
        ),
        2,
    )
    # regulatory_risk_score is a high-is-good safety score; fda_event_risk_* fields are high-is-worse.
    row.fda_safety_breadth_adjusted_score = adjusted_regulatory_risk
    row.fda_event_risk_breadth_adjusted_score = round(clamp(100.0 - adjusted_regulatory_risk), 2)
    if row.payload is not None:
        row.payload["fda_event_risk_breadth_adjustment"] = {
            "min_device_categories": policy.breadth_adjustment_min_device_categories,
            "device_category_count": row.fda_distinct_device_category_count,
            "adjustment_applied": row.fda_breadth_adjustment_applied,
            "adjustment_eligible": int(adjustment_applies),
            "scoring_divisor": scoring_divisor,
            "per_category_divisor": per_category_divisor,
            "raw_event_risk_score": row.fda_event_risk_score,
            "breadth_adjusted_event_risk_score": row.fda_event_risk_breadth_adjusted_score,
            "breadth_adjusted_safety_score": row.fda_safety_breadth_adjusted_score,
            "lower_severity_recall_count": lower_severity_recall_count,
            "lower_severity_recall_count_per_category": row.fda_recall_count_per_category,
            "class_i_recall_count_unadjusted": row.class_i_recall_count_36m,
            "warning_letter_count_unadjusted": row.fda_warning_letter_count_36m,
            "warning_letter_source_status": (
                "observed" if row.fda_warning_letter_count_36m > 0 else "not_configured_or_zero"
            ),
            "warning_letter_penalty_active": bool(row.fda_warning_letter_count_36m > 0),
            "death_injury_mdr_count_unadjusted": row.fda_mdr_death_injury_count_24m,
            "malfunction_mdr_count_per_category": row.fda_mdr_malfunction_count_per_category,
            "normalizer": "distinct_medical_specialty_or_product_code",
        }


def percentile_from_pairs(pairs: list[tuple[int, float]], *, higher_is_better: bool) -> dict[int, float]:
    if len(pairs) <= 1:
        return {idx: 50.0 for idx, _ in pairs}
    sorted_pairs = sorted(pairs, key=lambda item: item[1])
    denominator = len(sorted_pairs) - 1
    out: dict[int, float] = {}
    pos = 0
    while pos < len(sorted_pairs):
        end = pos + 1
        while end < len(sorted_pairs) and sorted_pairs[end][1] == sorted_pairs[pos][1]:
            end += 1
        avg_rank = (pos + end - 1) / 2.0
        pct = 100.0 * avg_rank / denominator
        score = pct if higher_is_better else 100.0 - pct
        for idx, _ in sorted_pairs[pos:end]:
            out[idx] = score
        pos = end
    return out


def cohort_percentile_maps(
    rows: list[FdaFeatureRow],
    *,
    field_name: str,
    higher_is_better: bool,
    min_cohort_n: int,
) -> dict[int, float]:
    global_pairs: list[tuple[int, float]] = []
    by_cohort: dict[str, list[tuple[int, float]]] = {}
    for idx, row in enumerate(rows):
        value = to_float(getattr(row, field_name, None))
        if value is None:
            continue
        global_pairs.append((idx, value))
        by_cohort.setdefault(row.calibration_cohort or "unknown", []).append((idx, value))
    global_scores = percentile_from_pairs(global_pairs, higher_is_better=higher_is_better)
    out: dict[int, float] = {}
    for pairs in by_cohort.values():
        if len(pairs) >= min_cohort_n:
            out.update(percentile_from_pairs(pairs, higher_is_better=higher_is_better))
        else:
            out.update({idx: global_scores.get(idx, 50.0) for idx, _ in pairs})
    return out


def apply_fda_velocity_scores(rows: list[FdaFeatureRow], *, min_cohort_n: int) -> None:
    velocity_rank = cohort_percentile_maps(
        rows,
        field_name="fda_clearance_velocity_raw",
        higher_is_better=True,
        min_cohort_n=min_cohort_n,
    )
    acceleration_rank = cohort_percentile_maps(
        rows,
        field_name="fda_clearance_acceleration_raw",
        higher_is_better=True,
        min_cohort_n=min_cohort_n,
    )
    for idx, row in enumerate(rows):
        if row.clearance_metrics_suppressed:
            row.fda_clearance_velocity_raw = None
            row.fda_clearance_acceleration_raw = None
        if row.fda_clearance_velocity_raw is None:
            row.fda_clearance_velocity_score = 50.0
        else:
            row.fda_clearance_velocity_score = round(clamp(velocity_rank.get(idx, 50.0)), 2)
        if row.fda_clearance_acceleration_raw is None:
            row.fda_clearance_acceleration_score = 50.0
        else:
            row.fda_clearance_acceleration_score = round(clamp(acceleration_rank.get(idx, 50.0)), 2)


def fda_evidence_quality_score(
    row: FdaFeatureRow,
    *,
    policy: FdaFeaturePolicy,
    mapping_confidence_for_gate: float | None,
) -> float:
    data_score = 100.0 if row.fda_data_available else 0.0
    mapping_score = mapping_confidence_for_gate
    if mapping_score is None:
        mapping_score = row.avg_mapping_confidence
    if mapping_score is None:
        mapping_score = 50.0 if row.fda_data_available else 25.0
    recency_score = row.fda_data_recency_score
    if recency_score is None:
        recency_score = 60.0 if row.fda_data_available else 25.0
    total_weight = (
        policy.evidence_quality_data_weight
        + policy.evidence_quality_mapping_weight
        + policy.evidence_quality_recency_weight
    )
    if total_weight <= 0.0:
        return 50.0
    return round(
        clamp(
            (
                data_score * policy.evidence_quality_data_weight
                + mapping_score * policy.evidence_quality_mapping_weight
                + recency_score * policy.evidence_quality_recency_weight
            )
            / total_weight
        ),
        2,
    )


def cap_fda_alpha_for_review_state(row: FdaFeatureRow, score: float, *, policy: FdaFeaturePolicy) -> float:
    state = normalize_fda_state(row.review_adjusted_fda_state)
    if row.confirmed_hard_red_flag or state == "confirmed_hard_red":
        return min(score, policy.hard_red_alpha_cap)
    if row.hard_red_flag or state in {"regulatory_review_required", "mapping_review_required"}:
        return min(score, policy.review_required_alpha_cap)
    if state == "regulatory_watch":
        return min(score, policy.regulatory_watch_alpha_cap)
    return score


FDA_PROFILE_COHORT_ALIASES = {
    "capital_equipment_procedure_platforms": "capital_equipment_imaging_monitoring",
    "home_chronic_care_devices_dme_drug_delivery": "diabetes_wearables_drug_delivery",
    "healthcare_services_cro_lab_services": "healthcare_services_cro_other",
    "hospital_supplies_surgical_consumables_oem": "hospital_supplies_consumables_dme",
    "orthopedics_spine_sports_implants": "orthopedics_spine_dental",
    "surgical_robotics_platforms": "capital_equipment_procedure_platforms",
}


def fda_profile_for_row(
    row: FdaFeatureRow,
    default_profile: FdaSignalProfile,
    profiles: dict[str, FdaSignalProfile],
) -> FdaSignalProfile:
    cohort = str(row.calibration_cohort or "")
    if cohort in profiles:
        return profiles[cohort]
    alias = FDA_PROFILE_COHORT_ALIASES.get(cohort)
    if alias and alias in profiles:
        return profiles[alias]
    return default_profile


def apply_fda_alpha_scores(
    rows: list[FdaFeatureRow],
    *,
    policy: FdaFeaturePolicy,
    default_profile: FdaSignalProfile,
    profiles: dict[str, FdaSignalProfile],
    min_cohort_n: int,
) -> None:
    innovation_rank = cohort_percentile_maps(
        rows,
        field_name="regulatory_innovation_score",
        higher_is_better=True,
        min_cohort_n=min_cohort_n,
    )
    safety_rank = cohort_percentile_maps(
        rows,
        field_name="fda_safety_score",
        higher_is_better=True,
        min_cohort_n=min_cohort_n,
    )
    evidence_rank = cohort_percentile_maps(
        rows,
        field_name="fda_evidence_quality_score",
        higher_is_better=True,
        min_cohort_n=min_cohort_n,
    )
    for idx, row in enumerate(rows):
        profile = fda_profile_for_row(row, default_profile, profiles)
        row.fda_signal_mode = profile.mode
        row.fda_signal_reliability = profile.reliability
        row.fda_policy_reason = profile.rationale
        row.fda_signal_direction = f"innovation:{profile.innovation_direction};safety:{profile.safety_direction}"
        neutral = policy.alpha_neutral_score
        state = normalize_fda_state(row.review_adjusted_fda_state)
        if not row.fda_data_available and state.startswith("manual_fda_footprint_"):
            alpha = row.fda_product_score_legacy
        elif profile.mode == FDA_SIGNAL_DISABLED:
            alpha = neutral
        elif profile.mode == FDA_SIGNAL_LEGACY_BROAD:
            alpha = row.fda_product_score_legacy
        elif profile.mode in {FDA_SIGNAL_NEUTRAL_OVERLAY, FDA_SIGNAL_RISK_VETO_ONLY}:
            alpha = neutral
        else:
            innovation_component = directional_score(
                innovation_rank.get(idx, 50.0),
                profile.innovation_direction,
                neutral=neutral,
            )
            safety_component = directional_score(
                safety_rank.get(idx, 50.0),
                profile.safety_direction,
                neutral=neutral,
            )
            evidence_component = evidence_rank.get(idx, 50.0)
            total_weight = profile.innovation_weight + profile.safety_weight + profile.evidence_weight
            raw = (
                innovation_component * profile.innovation_weight
                + safety_component * profile.safety_weight
                + evidence_component * profile.evidence_weight
            ) / total_weight
            alpha = shrink_to_neutral(raw, neutral=neutral, reliability=profile.reliability)
        if not row.fda_data_available and profile.no_data_score is not None:
            alpha = profile.no_data_score
        row.fda_alpha_score = round(clamp(cap_fda_alpha_for_review_state(row, alpha, policy=policy)), 2)
        # Keep fda_product_score as the legacy broad score for downstream compatibility.
        row.fda_product_score = row.fda_product_score_legacy
        if row.payload is not None:
            row.payload["alpha_scores"] = {
                "fda_product_score_legacy": row.fda_product_score_legacy,
                "fda_alpha_score": row.fda_alpha_score,
                "fda_safety_score": row.fda_safety_score,
                "fda_clearance_velocity_raw": row.fda_clearance_velocity_raw,
                "fda_clearance_velocity_score": row.fda_clearance_velocity_score,
                "fda_clearance_acceleration_raw": row.fda_clearance_acceleration_raw,
                "fda_clearance_acceleration_score": row.fda_clearance_acceleration_score,
                "fda_evidence_quality_score": row.fda_evidence_quality_score,
                "fda_event_risk_score": row.fda_event_risk_score,
                "fda_event_risk_breadth_adjusted_score": row.fda_event_risk_breadth_adjusted_score,
                "fda_signal_mode": row.fda_signal_mode,
                "fda_signal_direction": row.fda_signal_direction,
                "fda_signal_reliability": row.fda_signal_reliability,
                "fda_policy_reason": row.fda_policy_reason,
            }


def score_row(row: FdaFeatureRow, *, policy: FdaFeaturePolicy) -> None:
    has_fda_records = any(
        [
            row.approval_count_36m,
            row.recall_count_36m,
            row.current_adverse_event_count_24m,
            row.prev_adverse_event_count_24m,
        ]
    )
    row.fda_data_available = 1 if has_fda_records else 0
    prior_12m_count = max(0, row.approval_count_24m - row.approval_count_12m)
    month_25_36_count = max(0, row.approval_count_36m - row.approval_count_24m)
    row.fda_clearance_velocity_raw = float(row.approval_count_12m - prior_12m_count)
    prior_velocity = prior_12m_count - month_25_36_count
    row.fda_clearance_acceleration_raw = float(row.fda_clearance_velocity_raw - prior_velocity)
    breadth_adjustment_revenue_base: float | None = None
    if has_fda_records:
        revenue_base = revenue_normalizer(row, policy=policy)
        breadth_adjustment_revenue_base = revenue_base
        recall_severity_rate = round(row.recall_severity_36m / revenue_base, 4)
        row.recall_severity_per_billion_revenue = recall_severity_rate
        row.adverse_event_rate_per_billion_revenue = round(row.current_adverse_event_count_24m / revenue_base, 4)
        death_rate = row.death_count_24m / revenue_base
        injury_rate = row.injury_count_24m / revenue_base
        malfunction_rate = row.malfunction_count_24m / revenue_base
        adverse_acceleration_rate = max(0, row.current_adverse_event_count_24m - row.prev_adverse_event_count_24m) / revenue_base
        if row.latest_fda_event_date:
            event_day = parse_date(row.latest_fda_event_date)
            if event_day is not None:
                days_since = max(0, (date.fromisoformat(row.asof_date) - event_day).days)
                row.fda_data_recency_score = round(
                    clamp(100.0 * (0.5 ** (days_since / max(1.0, policy.recall_decay_half_life_days)))),
                    2,
                )
        raw_innovation = (
            policy.innovation_base_score
            + math.log1p(row.approval_count_12m) * policy.innovation_approval_12m_log_weight
            + math.log1p(row.approval_count_36m) * policy.innovation_approval_log_weight
            + math.log1p(row.pma_count_36m) * policy.innovation_pma_log_weight
            + math.log1p(row.product_code_count_36m) * policy.innovation_product_code_log_weight
        )
        recency_multiplier = 0.5 + 0.5 * ((row.fda_data_recency_score or 0.0) / 100.0)
        row.regulatory_innovation_score = round(clamp(raw_innovation * recency_multiplier), 2)
        if row.clearance_metrics_suppressed:
            row.regulatory_innovation_score = 0.0
            row.fda_clearance_velocity_raw = None
            row.fda_clearance_acceleration_raw = None
        row.regulatory_risk_score = round(
            clamp(
                100.0
                - recall_severity_rate * policy.risk_recall_severity_weight
                - row.class_i_recall_count_36m * policy.risk_class_i_recall_weight
                - death_rate * policy.risk_death_per_billion_weight
                - injury_rate * policy.risk_injury_per_billion_weight
                - malfunction_rate * policy.risk_malfunction_per_billion_weight
                - adverse_acceleration_rate * policy.risk_adverse_acceleration_per_billion_weight
            ),
            2,
        )
    else:
        row.regulatory_innovation_score = policy.no_data_innovation_score
        row.regulatory_risk_score = policy.no_data_risk_score
        row.review_reason = "no_mapped_fda_records"
        row.fda_clearance_velocity_raw = None
        row.fda_clearance_acceleration_raw = None

    raw_hard_reasons: list[str] = []
    review_reasons: list[str] = []
    recall_severity_rate_for_flag = row.recall_severity_per_billion_revenue or 0.0
    adverse_rate_for_flag = row.adverse_event_rate_per_billion_revenue or 0.0
    if row.class_i_recall_count_36m > 0:
        class_i_is_material = (
            row.class_i_recall_count_36m >= policy.class_i_hard_min_count
            or recall_severity_rate_for_flag >= policy.class_i_hard_min_severity_per_billion
        )
        if class_i_is_material:
            raw_hard_reasons.append("material_recent_class_i_recall")
        else:
            review_reasons.append("recent_class_i_recall_watch")
    if row.death_count_24m >= policy.death_event_min_count:
        death_is_material = (
            row.death_count_24m >= policy.death_event_hard_min_count
            or adverse_rate_for_flag >= policy.death_event_min_rate_per_billion
        )
        if death_is_material:
            raw_hard_reasons.append("material_recent_death_adverse_event")
        else:
            review_reasons.append("recent_death_adverse_event_watch")
    if row.avg_mapping_confidence is not None and row.avg_mapping_confidence < policy.min_mapping_confidence:
        if policy.low_mapping_confidence_is_hard_red:
            raw_hard_reasons.append("low_fda_mapping_confidence")
        else:
            review_reasons.append("low_fda_mapping_confidence_watch")

    mapping_confidence_for_gate = row.risk_mapping_confidence_min
    if mapping_confidence_for_gate is None:
        mapping_confidence_for_gate = row.avg_mapping_confidence
    mapping_confirmed = mapping_confidence_for_gate is None or mapping_confidence_for_gate >= policy.mapping_confirmed_min_confidence
    row.raw_fda_red_flag = 1 if raw_hard_reasons else 0
    row.confirmed_hard_red_flag = 0
    if not row.fda_data_available:
        row.review_adjusted_fda_state = "no_mapped_fda_records"
    elif raw_hard_reasons and not mapping_confirmed:
        row.review_adjusted_fda_state = "mapping_review_required"
        review_reasons.insert(0, "mapping_review_required")
    elif raw_hard_reasons:
        row.review_adjusted_fda_state = "regulatory_review_required"
        review_reasons.insert(0, "regulatory_review_required")
    elif review_reasons:
        row.review_adjusted_fda_state = "regulatory_watch"
        review_reasons.insert(0, "regulatory_watch")
    else:
        row.review_adjusted_fda_state = "cleared"

    # Until an analyst confirms product-family materiality, raw FDA red flags remain
    # automatic Tier-1 review gates, not confirmed portfolio hard-reds.
    row.hard_red_flag = row.raw_fda_red_flag
    if review_reasons and not row.review_reason:
        row.review_reason = ";".join(dict.fromkeys(review_reasons))
    elif review_reasons:
        row.review_reason = ";".join(dict.fromkeys([row.review_reason, *review_reasons]))
    row.hard_red_flag_reasons = raw_hard_reasons
    row.fda_product_score_legacy = round(
        clamp(
            policy.regulatory_risk_weight * row.regulatory_risk_score
            + policy.regulatory_innovation_weight * row.regulatory_innovation_score
        ),
        2,
    )
    row.fda_safety_score = round(clamp(row.regulatory_risk_score), 2)
    row.fda_clearance_velocity_score = 50.0
    row.fda_clearance_acceleration_score = 50.0
    row.fda_event_risk_score = round(clamp(100.0 - row.regulatory_risk_score), 2)
    apply_breadth_adjusted_event_risk(row, policy=policy, revenue_base=breadth_adjustment_revenue_base)
    row.fda_evidence_quality_score = fda_evidence_quality_score(
        row,
        policy=policy,
        mapping_confidence_for_gate=mapping_confidence_for_gate,
    )
    row.fda_product_score = row.fda_product_score_legacy
    row.fda_alpha_score = row.fda_product_score_legacy
    row.fda_signal_mode = FDA_SIGNAL_LEGACY_BROAD
    row.fda_signal_direction = FDA_DIRECTION_POSITIVE
    row.fda_signal_reliability = 1.0
    row.fda_policy_reason = "legacy_broad_score_before_alpha_calibration"
    row.payload = {
        "source": "fda_core",
        "counts": {
            "approval_12m": row.approval_count_12m,
            "approval_24m": row.approval_count_24m,
            "approval_36m": row.approval_count_36m,
            "pma_36m": row.pma_count_36m,
            "product_code_36m": row.product_code_count_36m,
            "recall_24m": row.recall_count_24m,
            "recall_36m": row.recall_count_36m,
            "class_i_recall_36m": row.class_i_recall_count_36m,
            "dedup_class_i_recall_36m": row.dedup_class_i_recall_count_36m,
            "class_i_multi_source_recall_36m": row.class_i_multi_source_recall_count_36m,
            "open_class_i_recall_12m": row.open_class_i_recall_count_12m,
            "open_class_i_recall_36m": row.open_class_i_recall_count_36m,
            "terminated_class_i_recall_36m": row.terminated_class_i_recall_count_36m,
            "canonical_recall_duplicate_source_count": row.canonical_recall_duplicate_source_count,
            "recall_severity_36m": row.recall_severity_36m,
            "fda_distinct_device_category_count": row.fda_distinct_device_category_count,
            "fda_recall_count_raw": row.fda_recall_count_raw,
            "fda_recall_count_per_category": row.fda_recall_count_per_category,
            "fda_class_i_recall_count": row.fda_class_i_recall_count,
            "fda_warning_letter_count_36m": row.fda_warning_letter_count_36m,
            "death_24m": row.death_count_24m,
            "injury_24m": row.injury_count_24m,
            "malfunction_24m": row.malfunction_count_24m,
            "fda_mdr_death_injury_count_24m": row.fda_mdr_death_injury_count_24m,
            "fda_mdr_malfunction_count_24m": row.fda_mdr_malfunction_count_24m,
            "fda_mdr_malfunction_count_per_category": row.fda_mdr_malfunction_count_per_category,
            "current_adverse_24m": row.current_adverse_event_count_24m,
            "previous_adverse_24m": row.prev_adverse_event_count_24m,
            "approval_prior_12m": prior_12m_count,
            "approval_month_25_36": month_25_36_count,
            "fda_clearance_velocity_raw": row.fda_clearance_velocity_raw,
            "fda_clearance_acceleration_raw": row.fda_clearance_acceleration_raw,
            "recall_severity_per_billion_revenue": row.recall_severity_per_billion_revenue,
            "adverse_event_rate_per_billion_revenue": row.adverse_event_rate_per_billion_revenue,
        },
        "normalization": {
            "revenue_ttm": row.revenue_ttm,
            "revenue_floor": policy.revenue_floor,
            "normalizer": "per_1b_revenue_with_floor",
            "recall_decay_half_life_days": policy.recall_decay_half_life_days,
        },
        "recency": {
            "latest_fda_event_date": row.latest_fda_event_date,
            "fda_data_recency_score": row.fda_data_recency_score,
        },
        "mapping": {
            "mapped_manufacturer_count": row.mapped_manufacturer_count,
            "avg_mapping_confidence": row.avg_mapping_confidence,
            "risk_mapping_confidence_min": row.risk_mapping_confidence_min,
            "mapping_confidence_for_gate": mapping_confidence_for_gate,
            "min_high_confidence": policy.min_mapping_confidence,
            "confirmed_parent_mapping_confidence": policy.mapping_confirmed_min_confidence,
        },
        "hard_red_policy": {
            "class_i_min_count": policy.class_i_hard_min_count,
            "class_i_min_severity_per_billion_revenue": policy.class_i_hard_min_severity_per_billion,
            "death_event_min_count": policy.death_event_min_count,
            "death_event_hard_min_count": policy.death_event_hard_min_count,
            "death_event_min_rate_per_billion_revenue": policy.death_event_min_rate_per_billion,
            "low_mapping_confidence_is_hard_red": policy.low_mapping_confidence_is_hard_red,
            "review_reasons": review_reasons,
            "raw_fda_red_flag": row.raw_fda_red_flag,
            "confirmed_hard_red_flag": row.confirmed_hard_red_flag,
            "review_adjusted_fda_state": row.review_adjusted_fda_state,
        },
        "score_weights": {
            "regulatory_risk": policy.regulatory_risk_weight,
            "regulatory_innovation": policy.regulatory_innovation_weight,
        },
        "alpha_scores": {
            "fda_product_score_legacy": row.fda_product_score_legacy,
            "fda_alpha_score": row.fda_alpha_score,
            "fda_safety_score": row.fda_safety_score,
            "fda_clearance_velocity_raw": row.fda_clearance_velocity_raw,
            "fda_clearance_velocity_score": row.fda_clearance_velocity_score,
            "fda_clearance_acceleration_raw": row.fda_clearance_acceleration_raw,
            "fda_clearance_acceleration_score": row.fda_clearance_acceleration_score,
            "fda_evidence_quality_score": row.fda_evidence_quality_score,
            "fda_event_risk_score": row.fda_event_risk_score,
            "fda_event_risk_breadth_adjusted_score": row.fda_event_risk_breadth_adjusted_score,
            "fda_safety_breadth_adjusted_score": row.fda_safety_breadth_adjusted_score,
            "fda_signal_mode": row.fda_signal_mode,
            "fda_signal_direction": row.fda_signal_direction,
            "fda_signal_reliability": row.fda_signal_reliability,
            "fda_policy_reason": row.fda_policy_reason,
        },
        "fda_event_risk_breadth_adjustment": {
            "min_device_categories": policy.breadth_adjustment_min_device_categories,
            "device_category_count": row.fda_distinct_device_category_count,
            "adjustment_applied": row.fda_breadth_adjustment_applied,
            "adjustment_eligible": int(
                row.fda_distinct_device_category_count >= policy.breadth_adjustment_min_device_categories
            ),
            "scoring_divisor": float(
                row.fda_distinct_device_category_count
                if row.fda_distinct_device_category_count >= policy.breadth_adjustment_min_device_categories
                else 1
            ),
            "per_category_divisor": float(max(row.fda_distinct_device_category_count, 1)),
            "raw_event_risk_score": row.fda_event_risk_score,
            "breadth_adjusted_event_risk_score": row.fda_event_risk_breadth_adjusted_score,
            "breadth_adjusted_safety_score": row.fda_safety_breadth_adjusted_score,
            "lower_severity_recall_count": max(0, row.recall_count_36m - row.class_i_recall_count_36m),
            "lower_severity_recall_count_per_category": row.fda_recall_count_per_category,
            "class_i_recall_count_unadjusted": row.class_i_recall_count_36m,
            "warning_letter_count_unadjusted": row.fda_warning_letter_count_36m,
            "warning_letter_source_status": (
                "observed" if row.fda_warning_letter_count_36m > 0 else "not_configured_or_zero"
            ),
            "warning_letter_penalty_active": bool(row.fda_warning_letter_count_36m > 0),
            "death_injury_mdr_count_unadjusted": row.fda_mdr_death_injury_count_24m,
            "malfunction_mdr_count_per_category": row.fda_mdr_malfunction_count_per_category,
            "normalizer": "distinct_medical_specialty_or_product_code",
            "production_usage": "shadow_only",
        },
        "risk_penalties": {
            "recall_severity_per_billion": policy.risk_recall_severity_weight,
            "class_i_recall": policy.risk_class_i_recall_weight,
            "death_per_billion": policy.risk_death_per_billion_weight,
            "injury_per_billion": policy.risk_injury_per_billion_weight,
            "malfunction_per_billion": policy.risk_malfunction_per_billion_weight,
            "adverse_acceleration_per_billion": policy.risk_adverse_acceleration_per_billion_weight,
        },
    }


def apply_review_override(row: FdaFeatureRow, override: dict[str, str]) -> None:
    state = row_get(override, "review_adjusted_fda_state", "recommended_state")
    if state:
        row.review_adjusted_fda_state = state
    row.hard_red_flag = csv_bool(row_get(override, "hard_red_flag"), row.hard_red_flag)
    row.confirmed_hard_red_flag = csv_bool(
        row_get(override, "confirmed_hard_red_flag"),
        row.confirmed_hard_red_flag,
    )
    reasons = row_get(override, "hard_red_flag_reasons")
    if reasons:
        row.hard_red_flag_reasons = [reason for reason in reasons.split(";") if reason]
    elif row.hard_red_flag == 0 and row.confirmed_hard_red_flag == 0:
        row.hard_red_flag_reasons = []
    review_reason = row_get(override, "review_reason")
    if review_reason:
        row.review_reason = review_reason
    row.clearance_metrics_suppressed = csv_bool(
        row_get(override, "suppress_clearance_metrics", "clearance_metrics_suppressed"),
        row.clearance_metrics_suppressed,
    )
    suppression_reason = row_get(override, "clearance_metrics_suppression_reason", "suppress_clearance_reason")
    if suppression_reason:
        row.clearance_metrics_suppression_reason = suppression_reason
    filter_parts: list[str] = []
    include_codes = row_get(override, "approval_product_code_allowlist", "approval_product_codes_include")
    exclude_codes = row_get(override, "approval_product_code_excludelist", "approval_product_codes_exclude")
    if include_codes:
        filter_parts.append(f"include={include_codes}")
    if exclude_codes:
        filter_parts.append(f"exclude={exclude_codes}")
    if filter_parts:
        row.approval_product_code_filter = ";".join(filter_parts)
    filter_note = row_get(override, "approval_product_code_filter_note", "product_line_filter_note")
    if filter_note:
        row.approval_product_code_filter_note = filter_note
    if row.confirmed_hard_red_flag:
        row.hard_red_flag = 1
    if row.payload is None:
        row.payload = {}
    row.payload["analyst_review_override"] = {
        "review_adjusted_fda_state": row.review_adjusted_fda_state,
        "hard_red_flag": row.hard_red_flag,
        "confirmed_hard_red_flag": row.confirmed_hard_red_flag,
        "hard_red_flag_reasons": row.hard_red_flag_reasons or [],
        "review_reason": row.review_reason,
        "suppress_clearance_metrics": row.clearance_metrics_suppressed,
        "clearance_metrics_suppression_reason": row.clearance_metrics_suppression_reason,
        "approval_product_code_filter": row.approval_product_code_filter,
        "approval_product_code_filter_note": row.approval_product_code_filter_note,
        "analyst_review_status": row_get(override, "analyst_review_status"),
        "analyst_note": row_get(override, "analyst_note", "note"),
    }


def apply_footprint_override(row: FdaFeatureRow, footprint: dict[str, str]) -> None:
    if row.fda_data_available:
        return
    state = row_get(footprint, "review_adjusted_fda_state", "fda_footprint_state")
    if state:
        row.review_adjusted_fda_state = state
    review_reason = row_get(footprint, "review_reason")
    if review_reason:
        row.review_reason = review_reason
    score = to_float(row_get(footprint, "fda_product_score", "footprint_score"))
    if score is not None:
        manual_score = round(clamp(score), 2)
        row.fda_product_score = manual_score
        row.fda_product_score_legacy = manual_score
        row.fda_alpha_score = manual_score
        row.fda_policy_reason = row.fda_policy_reason or "manual_fda_footprint_score"
    if row.payload is None:
        row.payload = {}
    row.payload["manual_fda_footprint"] = {
        "footprint_category": row_get(footprint, "footprint_category", "category"),
        "primary_fda_entity": row_get(footprint, "primary_fda_entity", "fda_entity"),
        "regulatory_route": row_get(footprint, "regulatory_route"),
        "key_class": row_get(footprint, "key_class"),
        "product_codes": row_get(footprint, "product_codes", "product_code"),
        "premarket_numbers": row_get(footprint, "premarket_numbers", "premarket_number", "submission_numbers"),
        "fei_numbers": row_get(footprint, "fei_numbers", "fei_number", "establishment_identifier"),
        "expected_cdrh_records": row_get(footprint, "expected_cdrh_records"),
        "review_adjusted_fda_state": row.review_adjusted_fda_state,
        "review_reason": row.review_reason,
        "note": row_get(footprint, "note", "notes"),
    }


def apply_manual_footprint_evidence(row: FdaFeatureRow, evidence: dict[str, str]) -> None:
    evidence_type = row_get(evidence, "fda_evidence_type", "evidence_type")
    if evidence_type:
        row.fda_evidence_type = evidence_type
    regulatory_stage = row_get(evidence, "regulatory_stage", "stage")
    if regulatory_stage:
        row.regulatory_stage = regulatory_stage
    confidence = to_float(row_get(evidence, "evidence_confidence", "confidence"))
    if confidence is not None:
        row.evidence_confidence = round(clamp(confidence), 2)
    next_review_date = row_get(evidence, "next_review_date")
    if next_review_date:
        row.next_review_date = next_review_date
    note = row_get(evidence, "manual_evidence_note", "evidence_note", "note")
    if note:
        row.manual_evidence_note = note
    if row.payload is None:
        row.payload = {}
    row.payload["manual_fda_footprint_evidence"] = {
        "fda_evidence_type": row.fda_evidence_type,
        "regulatory_stage": row.regulatory_stage,
        "evidence_confidence": row.evidence_confidence,
        "next_review_date": row.next_review_date,
        "manual_evidence_note": row.manual_evidence_note,
        "source": row_get(evidence, "source", "evidence_source"),
    }


def build_rows(
    conn: Any,
    companies: list[Company],
    *,
    asof: date,
    policy: FdaFeaturePolicy,
    review_overrides: dict[str, dict[str, str]] | None = None,
    footprint_overrides: dict[str, dict[str, str]] | None = None,
    manual_evidence: dict[str, dict[str, str]] | None = None,
    default_signal_profile: FdaSignalProfile | None = None,
    signal_profiles: dict[str, FdaSignalProfile] | None = None,
    min_cohort_rank_n: int = 5,
) -> list[FdaFeatureRow]:
    review_overrides = review_overrides or {}
    footprint_overrides = footprint_overrides or {}
    manual_evidence = manual_evidence or {}
    default_signal_profile = default_signal_profile or FdaSignalProfile()
    signal_profiles = signal_profiles or {}
    rows: list[FdaFeatureRow] = []
    for company in companies:
        override = review_overrides.get(company.ticker)
        suppress_clearance_metrics = csv_bool(
            row_get(override or {}, "suppress_clearance_metrics", "clearance_metrics_suppressed"),
            0,
        )
        include_approval_codes = split_code_set(
            row_get(override or {}, "approval_product_code_allowlist", "approval_product_codes_include")
        )
        exclude_approval_codes = split_code_set(
            row_get(override or {}, "approval_product_code_excludelist", "approval_product_codes_exclude")
        )
        row = FdaFeatureRow(
            asof_date=asof.isoformat(),
            company_id=company.company_id,
            ticker=company.ticker,
            company_name=company.company_name,
            calibration_cohort=company.calibration_cohort,
        )
        if suppress_clearance_metrics:
            row.clearance_metrics_suppressed = 1
            row.clearance_metrics_suppression_reason = row_get(
                override or {},
                "clearance_metrics_suppression_reason",
                "suppress_clearance_reason",
            )
        else:
            count_approvals(
                conn,
                row,
                asof=asof,
                policy=policy,
                include_product_codes=include_approval_codes,
                exclude_product_codes=exclude_approval_codes,
            )
            filter_parts: list[str] = []
            if include_approval_codes:
                filter_parts.append(f"include={';'.join(sorted(include_approval_codes))}")
            if exclude_approval_codes:
                filter_parts.append(f"exclude={';'.join(sorted(exclude_approval_codes))}")
            row.approval_product_code_filter = ";".join(filter_parts)
            row.approval_product_code_filter_note = row_get(
                override or {},
                "approval_product_code_filter_note",
                "product_line_filter_note",
            )
        count_recalls(conn, row, asof=asof, policy=policy)
        count_adverse_events(conn, row, asof=asof, policy=policy)
        count_device_categories(conn, row, asof=asof, policy=policy)
        manufacturer_mapping_summary(conn, row)
        row.revenue_ttm = latest_revenue_ttm(conn, company.company_id, asof=asof)
        score_row(row, policy=policy)
        footprint = footprint_overrides.get(row.ticker)
        if footprint:
            apply_footprint_override(row, footprint)
        evidence = manual_evidence.get(row.ticker)
        if evidence:
            apply_manual_footprint_evidence(row, evidence)
        if override:
            apply_review_override(row, override)
        rows.append(row)
    apply_fda_velocity_scores(rows, min_cohort_n=min_cohort_rank_n)
    apply_fda_alpha_scores(
        rows,
        policy=policy,
        default_profile=default_signal_profile,
        profiles=signal_profiles,
        min_cohort_n=min_cohort_rank_n,
    )
    return rows


def ensure_fda_feature_policy_columns(conn: Any) -> None:
    if not table_exists(conn, "feature_fda_product_risk"):
        return
    existing = {
        str(row["name"])
        for row in conn.execute("PRAGMA table_info(feature_fda_product_risk)").fetchall()
    }
    for column, ddl in OPTIONAL_FDA_FEATURE_COLUMNS.items():
        if column not in existing:
            conn.execute(f"ALTER TABLE feature_fda_product_risk ADD COLUMN {quote_identifier(column)} {ddl}")


def upsert_feature_rows(conn: Any, rows: list[FdaFeatureRow]) -> int:
    if not rows:
        return 0
    ensure_fda_feature_policy_columns(conn)
    now = utc_now()
    columns = [
        "asof_date",
        "company_id",
        "regulatory_innovation_score",
        "regulatory_risk_score",
        "fda_product_score",
        "fda_product_score_legacy",
        "fda_alpha_score",
        "fda_safety_score",
        "fda_clearance_velocity_raw",
        "fda_clearance_velocity_score",
        "fda_clearance_acceleration_raw",
        "fda_clearance_acceleration_score",
        "fda_evidence_quality_score",
        "fda_event_risk_score",
        "fda_event_risk_breadth_adjusted_score",
        "fda_safety_breadth_adjusted_score",
        "fda_distinct_device_category_count",
        "fda_recall_count_raw",
        "fda_recall_count_per_category",
        "fda_class_i_recall_count",
        "fda_warning_letter_count_36m",
        "fda_mdr_death_injury_count_24m",
        "fda_mdr_malfunction_count_24m",
        "fda_mdr_malfunction_count_per_category",
        "fda_breadth_adjustment_applied",
        "fda_signal_mode",
        "fda_signal_direction",
        "fda_signal_reliability",
        "fda_policy_reason",
        "fda_data_available",
        "latest_fda_event_date",
        "calibration_cohort",
        "mapped_manufacturer_count",
        "avg_mapping_confidence",
        "risk_mapping_confidence_min",
        "hard_red_flag",
        "hard_red_flag_reasons",
        "raw_fda_red_flag",
        "confirmed_hard_red_flag",
        "review_adjusted_fda_state",
        "dedup_class_i_recall_count_36m",
        "class_i_multi_source_recall_count_36m",
        "open_class_i_recall_count_12m",
        "open_class_i_recall_count_36m",
        "terminated_class_i_recall_count_36m",
        "canonical_recall_duplicate_source_count",
        "review_reason",
        "clearance_metrics_suppressed",
        "clearance_metrics_suppression_reason",
        "approval_product_code_filter",
        "approval_product_code_filter_note",
        "fda_evidence_type",
        "regulatory_stage",
        "evidence_confidence",
        "next_review_date",
        "manual_evidence_note",
        "payload_json",
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

    def row_values(row: FdaFeatureRow) -> tuple[Any, ...]:
        values = {
            "asof_date": row.asof_date,
            "company_id": row.company_id,
            "regulatory_innovation_score": row.regulatory_innovation_score,
            "regulatory_risk_score": row.regulatory_risk_score,
            "fda_product_score": row.fda_product_score,
            "fda_product_score_legacy": row.fda_product_score_legacy,
            "fda_alpha_score": row.fda_alpha_score,
            "fda_safety_score": row.fda_safety_score,
            "fda_clearance_velocity_raw": row.fda_clearance_velocity_raw,
            "fda_clearance_velocity_score": row.fda_clearance_velocity_score,
            "fda_clearance_acceleration_raw": row.fda_clearance_acceleration_raw,
            "fda_clearance_acceleration_score": row.fda_clearance_acceleration_score,
            "fda_evidence_quality_score": row.fda_evidence_quality_score,
            "fda_event_risk_score": row.fda_event_risk_score,
            "fda_event_risk_breadth_adjusted_score": row.fda_event_risk_breadth_adjusted_score,
            "fda_safety_breadth_adjusted_score": row.fda_safety_breadth_adjusted_score,
            "fda_distinct_device_category_count": row.fda_distinct_device_category_count,
            "fda_recall_count_raw": row.fda_recall_count_raw,
            "fda_recall_count_per_category": row.fda_recall_count_per_category,
            "fda_class_i_recall_count": row.fda_class_i_recall_count,
            "fda_warning_letter_count_36m": row.fda_warning_letter_count_36m,
            "fda_mdr_death_injury_count_24m": row.fda_mdr_death_injury_count_24m,
            "fda_mdr_malfunction_count_24m": row.fda_mdr_malfunction_count_24m,
            "fda_mdr_malfunction_count_per_category": row.fda_mdr_malfunction_count_per_category,
            "fda_breadth_adjustment_applied": row.fda_breadth_adjustment_applied,
            "fda_signal_mode": row.fda_signal_mode,
            "fda_signal_direction": row.fda_signal_direction,
            "fda_signal_reliability": row.fda_signal_reliability,
            "fda_policy_reason": row.fda_policy_reason,
            "fda_data_available": row.fda_data_available,
            "latest_fda_event_date": row.latest_fda_event_date,
            "calibration_cohort": row.calibration_cohort,
            "mapped_manufacturer_count": row.mapped_manufacturer_count,
            "avg_mapping_confidence": row.avg_mapping_confidence,
            "risk_mapping_confidence_min": row.risk_mapping_confidence_min,
            "hard_red_flag": row.hard_red_flag,
            "hard_red_flag_reasons": ";".join(row.hard_red_flag_reasons or []),
            "raw_fda_red_flag": row.raw_fda_red_flag,
            "confirmed_hard_red_flag": row.confirmed_hard_red_flag,
            "review_adjusted_fda_state": row.review_adjusted_fda_state,
            "dedup_class_i_recall_count_36m": row.dedup_class_i_recall_count_36m,
            "class_i_multi_source_recall_count_36m": row.class_i_multi_source_recall_count_36m,
            "open_class_i_recall_count_12m": row.open_class_i_recall_count_12m,
            "open_class_i_recall_count_36m": row.open_class_i_recall_count_36m,
            "terminated_class_i_recall_count_36m": row.terminated_class_i_recall_count_36m,
            "canonical_recall_duplicate_source_count": row.canonical_recall_duplicate_source_count,
            "review_reason": row.review_reason,
            "clearance_metrics_suppressed": row.clearance_metrics_suppressed,
            "clearance_metrics_suppression_reason": row.clearance_metrics_suppression_reason,
            "approval_product_code_filter": row.approval_product_code_filter,
            "approval_product_code_filter_note": row.approval_product_code_filter_note,
            "fda_evidence_type": row.fda_evidence_type,
            "regulatory_stage": row.regulatory_stage,
            "evidence_confidence": row.evidence_confidence,
            "next_review_date": row.next_review_date,
            "manual_evidence_note": row.manual_evidence_note,
            "payload_json": json.dumps(row.payload or {}, ensure_ascii=True, sort_keys=True),
            "created_at": now,
            "updated_at": now,
        }
        return tuple(values[column] for column in columns)

    conn.executemany(
        f"""
        INSERT INTO feature_fda_product_risk({column_sql})
        VALUES ({placeholders})
        ON CONFLICT(asof_date, company_id) DO UPDATE SET
            {update_sql}
        """,
        [row_values(row) for row in rows],
    )
    return len(rows)


def replace_data_quality_issues(conn: Any, rows: list[FdaFeatureRow], *, asof: str) -> int:
    conn.execute(
        "DELETE FROM data_quality_issues WHERE table_name = ? AND asof_date = ?",
        ("feature_fda_product_risk", asof),
    )
    now = utc_now()
    issue_rows: list[tuple[Any, ...]] = []
    for row in rows:
        reasons: list[str] = []
        if row.review_reason:
            reasons.extend(
                reason
                for reason in row.review_reason.split(";")
                if reason and reason not in NON_DATA_QUALITY_REVIEW_REASONS and not reason.startswith("analyst_")
            )
        if not reasons:
            continue
        issue_rows.append(
            (
                asof,
                row.company_id,
                None,
                "feature_fda_product_risk",
                "fda_product_score",
                ";".join(reasons),
                "warning",
                f"{row.ticker}: {';'.join(reasons)}",
                now,
            )
        )
    if issue_rows:
        conn.executemany(
            """
            INSERT INTO data_quality_issues(
                asof_date, company_id, source_id, table_name, field_name, issue_type,
                severity, message, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            issue_rows,
        )
    return len(issue_rows)


def row_to_dict(row: FdaFeatureRow) -> dict[str, Any]:
    out = {field: getattr(row, field) for field in FIELDNAMES if hasattr(row, field)}
    out["hard_red_flag_reasons"] = ";".join(row.hard_red_flag_reasons or [])
    return out


def write_csv(path: Path, rows: list[FdaFeatureRow]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDNAMES, extrasaction="ignore", lineterminator="\n")
        writer.writeheader()
        writer.writerows(row_to_dict(row) for row in rows)


def linked_adverse_counts(conn: Any, *, company_id: int, product_code: str, asof: date, months: int = 24) -> tuple[int, int, int]:
    if not product_code:
        return 0, 0, 0
    start = months_before(asof, months).isoformat()
    item = conn.execute(
        """
        SELECT COUNT(*) AS event_count,
               COALESCE(SUM(death_count), 0) AS death_count,
               COALESCE(SUM(injury_count), 0) AS injury_count
        FROM fact_fda_adverse_event
        WHERE company_id = ?
          AND product_code = ?
          AND COALESCE(report_date, event_date, '') >= ?
          AND COALESCE(report_date, event_date, '') <= ?
        """,
        (company_id, product_code, start, asof.isoformat()),
    ).fetchone()
    if item is None:
        return 0, 0, 0
    return int(item["death_count"] or 0), int(item["injury_count"] or 0), int(item["event_count"] or 0)


def selected_recall_payload(item: Any) -> dict[str, Any]:
    payload = safe_json_loads(item["payload_json"])
    selected = payload.get("selected_payload")
    return selected if isinstance(selected, dict) else {}


def affected_units_from_payload(payload: dict[str, Any]) -> str:
    return nested_field(
        payload,
        "product_quantity",
        "quantity_in_commerce",
        "quantity_recalled",
        "distribution_pattern",
    )


def dedup_trigger_reason(item: Any) -> str:
    reasons: list[str] = []
    source_count = int(item["source_count"] or 0)
    if source_count > 1:
        reasons.append(f"deduped_{source_count}_source_rows")
    if is_class_i(item["classification"]):
        reasons.append("class_i_recall")
    if int(item["is_open"] or 0):
        reasons.append("open_recall")
    if int(item["is_terminated"] or 0):
        reasons.append("terminated_recall")
    return ";".join(reasons)


def hard_red_review_rows(conn: Any, rows: list[FdaFeatureRow], *, asof: date) -> list[dict[str, Any]]:
    flagged = {
        row.company_id: row
        for row in rows
        if (
            row.raw_fda_red_flag
            or normalize_fda_state(row.review_adjusted_fda_state) in MANUAL_FDA_REVIEW_STATES
        )
    }
    if not flagged:
        return []
    placeholders = ",".join("?" for _ in flagged)
    query = f"""
        SELECT c.*, co.ticker, co.company_name, m.manufacturer_name, p.device_name,
               fv.revenue_ttm
        FROM fact_fda_recall_canonical c
        JOIN dim_company co ON co.company_id = c.company_id
        LEFT JOIN dim_fda_manufacturer m
          ON m.fda_manufacturer_id = c.fda_manufacturer_id
        LEFT JOIN dim_fda_product_code p
          ON p.product_code = c.product_code
        LEFT JOIN feature_financial_valuation fv
          ON fv.company_id = c.company_id
         AND fv.asof_date = (
             SELECT MAX(fv2.asof_date)
             FROM feature_financial_valuation fv2
             WHERE fv2.company_id = c.company_id
               AND fv2.asof_date <= ?
         )
        WHERE c.company_id IN ({placeholders})
          AND (
              LOWER(REPLACE(REPLACE(COALESCE(c.classification, ''), ' ', '_'), '-', '_'))
                  IN ('i', 'class_i', 'class_1', 'classi', 'class1')
              OR COALESCE(c.max_severity_weight, 0.0) >= 5.0
          )
        ORDER BY co.ticker, c.recall_initiation_date DESC, c.canonical_recall_key
    """
    out: list[dict[str, Any]] = []
    for item in conn.execute(query, [asof.isoformat(), *flagged.keys()]).fetchall():
        feature = flagged[int(item["company_id"])]
        product_code = str(item["product_code"] or "")
        death_count, injury_count, maude_count = linked_adverse_counts(
            conn,
            company_id=int(item["company_id"]),
            product_code=product_code,
            asof=asof,
        )
        payload = selected_recall_payload(item)
        out.append(
            {
                "ticker": item["ticker"],
                "company_name": item["company_name"],
                "company_id": item["company_id"],
                "fda_manufacturer_id": item["fda_manufacturer_id"],
                "manufacturer_name": item["manufacturer_name"],
                "mapping_confidence": item["mapping_confidence"],
                "mapping_method": item["mapping_method"],
                "recall_number": item["recall_number"],
                "event_id": item["event_id"],
                "canonical_recall_key": item["canonical_recall_key"],
                "source_endpoints": item["source_endpoints"],
                "classification": item["classification"],
                "severity_weight": item["max_severity_weight"],
                "status": item["status"],
                "is_open": item["is_open"],
                "is_terminated": item["is_terminated"],
                "recall_initiation_date": item["recall_initiation_date"],
                "center_classification_date": item["center_classification_date"],
                "termination_date": item["termination_date"],
                "product_code": product_code,
                "product_description": item["product_description"],
                "device_name": item["device_name"],
                "reason_for_recall": item["reason_for_recall"],
                "affected_units": affected_units_from_payload(payload),
                "death_count_linked": death_count,
                "injury_count_linked": injury_count,
                "maude_event_count_same_product_code": maude_count,
                "revenue_ttm": item["revenue_ttm"],
                "segment_revenue": "",
                "estimated_revenue_at_risk": "",
                "revenue_at_risk_pct": "",
                "raw_trigger_reason": ";".join(feature.hard_red_flag_reasons or []),
                "dedup_trigger_reason": dedup_trigger_reason(item),
                "recommended_state": feature.review_adjusted_fda_state,
                "analyst_review_status": "",
                "analyst_note": "",
            }
        )
    return out


def write_hard_red_review_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=FDA_HARD_RED_REVIEW_FIELDNAMES,
            extrasaction="ignore",
            lineterminator="\n",
        )
        writer.writeheader()
        writer.writerows(rows)


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
            cfg_get(config, "fda_features.output_csv", "../output/med_devices_reports/med_device_fda_product_risk_features.csv"),
            base_dir=base_dir,
        )
    )
    review_csv_template = str(
        cfg_get(config, "fda_features.hard_red_review_csv", "../output/med_devices_reports/fda_hard_red_review_{asof}.csv")
        or ""
    ).strip()
    review_override_raw = str(cfg_get(config, "fda_features.review_override_csv", "") or "").strip()
    review_override_csv = resolve_path(review_override_raw, base_dir=base_dir) if review_override_raw else None
    footprint_raw = str(cfg_get(config, "fda_features.footprint_csv", "") or "").strip()
    footprint_csv = resolve_path(footprint_raw, base_dir=base_dir) if footprint_raw else None
    manual_evidence_raw = str(cfg_get(config, "fda_features.manual_footprint_evidence_csv", "") or "").strip()
    manual_evidence_csv = resolve_path(manual_evidence_raw, base_dir=base_dir) if manual_evidence_raw else None
    policy = fda_feature_policy(config)
    default_signal_profile = default_fda_signal_profile(config)
    signal_profiles = fda_signal_profiles(config, default_signal_profile)
    min_cohort_rank_n = int(cfg_get(config, "fda_features.alpha.min_cohort_rank_n", 5))
    ticker_filter = {normalize_ticker(value) for value in str(args.tickers or "").split(",") if normalize_ticker(value)}
    with connect(db_path, timeout_sec=float(cfg_get(config, "runtime.sqlite_timeout_sec", 30.0))) as conn:
        init_db(conn)
        asof_text = args.asof.strip() if args.asof else latest_asof(conn)
        asof = parse_date(asof_text)
        if asof is None:
            raise ValueError(f"Invalid as-of date: {asof_text}")
        companies = load_companies(conn, ticker_filter=ticker_filter, max_tickers=int(args.max_tickers))
        if not companies:
            raise ValueError("No active companies selected")
        run_id = start_run(conn, run_type="build_med_device_fda_features", input_path=config_path)
        try:
            canonical_count = refresh_canonical_recalls(conn)
            preflight_fda_company_links(conn)
            review_overrides = load_review_overrides(review_override_csv)
            footprint_overrides = load_footprint_overrides(footprint_csv)
            manual_evidence = load_manual_footprint_evidence(manual_evidence_csv)
            rows = build_rows(
                conn,
                companies,
                asof=asof,
                policy=policy,
                review_overrides=review_overrides,
                footprint_overrides=footprint_overrides,
                manual_evidence=manual_evidence,
                default_signal_profile=default_signal_profile,
                signal_profiles=signal_profiles,
                min_cohort_rank_n=min_cohort_rank_n,
            )
            upserted = upsert_feature_rows(conn, rows)
            issue_count = replace_data_quality_issues(conn, rows, asof=asof.isoformat())
            write_csv(output_csv, rows)
            review_row_count = 0
            review_csv = ""
            if review_csv_template:
                review_csv_path = resolve_path(review_csv_template.replace("{asof}", asof.isoformat()), base_dir=base_dir)
                review_rows = hard_red_review_rows(conn, rows, asof=asof)
                write_hard_red_review_csv(review_csv_path, review_rows)
                review_row_count = len(review_rows)
                review_csv = str(review_csv_path)
            red_flags = sum(1 for row in rows if row.hard_red_flag)
            message = (
                f"asof={asof.isoformat()} rows={upserted} canonical_recalls={canonical_count} "
                f"red_flags={red_flags} review_rows={review_row_count} issues={issue_count} "
                f"output={output_csv} review_output={review_csv}"
            )
            finish_run(conn, run_id=run_id, status="success", row_count=upserted, message=message)
            LOGGER.info("FDA features complete: %s", message)
        except BaseException as exc:
            finish_run(conn, run_id=run_id, status="failed", row_count=0, message=f"{type(exc).__name__}: {exc}")
            raise


if __name__ == "__main__":
    main()
