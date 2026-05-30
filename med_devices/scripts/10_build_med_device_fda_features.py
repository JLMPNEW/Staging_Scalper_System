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
from med_devices.core.db import connect, finish_run, init_db, start_run, utc_now  # noqa: E402
from med_devices.core.logging_utils import configure_utc_logging  # noqa: E402
from med_devices.core.text_norm import normalize_ticker  # noqa: E402


LOGGER = logging.getLogger("build_med_device_fda_features")
DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
NON_DATA_QUALITY_REVIEW_REASONS = {
    "mapping_review_required",
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
FIELDNAMES = [
    "asof_date",
    "company_id",
    "ticker",
    "company_name",
    "approval_count_12m",
    "approval_count_24m",
    "approval_count_36m",
    "pma_count_36m",
    "product_code_count_36m",
    "recall_count_24m",
    "recall_count_36m",
    "class_i_recall_count_36m",
    "dedup_class_i_recall_count_36m",
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


@dataclass
class FdaFeatureRow:
    asof_date: str
    company_id: int
    ticker: str
    company_name: str
    approval_count_12m: int = 0
    approval_count_24m: int = 0
    approval_count_36m: int = 0
    pma_count_36m: int = 0
    product_code_count_36m: int = 0
    recall_count_24m: int = 0
    recall_count_36m: int = 0
    class_i_recall_count_36m: int = 0
    dedup_class_i_recall_count_36m: int = 0
    open_class_i_recall_count_12m: int = 0
    open_class_i_recall_count_36m: int = 0
    terminated_class_i_recall_count_36m: int = 0
    canonical_recall_duplicate_source_count: int = 0
    recall_severity_36m: float = 0.0
    death_count_24m: int = 0
    injury_count_24m: int = 0
    malfunction_count_24m: int = 0
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
    mapping_confirmed_min_confidence: float = 95.0
    open_class_i_12m_confirmed_min_count: int = 1
    open_class_i_36m_confirmed_min_count: int = 2
    innovation_approval_12m_log_weight: float = 24.0


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
        mapping_confirmed_min_confidence=cfg_float(config, "fda_features.review_state.mapping_confirmed_min_confidence", 95.0),
        open_class_i_12m_confirmed_min_count=int(
            cfg_get(config, "fda_features.review_state.open_class_i_12m_confirmed_min_count", 1)
        ),
        open_class_i_36m_confirmed_min_count=int(
            cfg_get(config, "fda_features.review_state.open_class_i_36m_confirmed_min_count", 2)
        ),
    )


def latest_asof(conn: Any) -> str:
    row = conn.execute("SELECT MAX(asof_date) AS asof_date FROM feature_financial_valuation").fetchone()
    asof = str(row["asof_date"] or "") if row is not None else ""
    return asof or datetime.now(timezone.utc).date().isoformat()


def load_companies(conn: Any, *, ticker_filter: set[str], max_tickers: int) -> list[Company]:
    rows = conn.execute(
        """
        SELECT company_id, ticker, company_name
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
        out.append(Company(int(row["company_id"]), ticker, str(row["company_name"] or "")))
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
                1 if is_terminated_status(item["status"], item["termination_date"]) else 0,
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
        row.recall_severity_36m += float(item["severity_weight"] or 1.0) * decay * status_multiplier
        if is_class_i(item["classification"]):
            row.class_i_recall_count_36m += 1
            row.dedup_class_i_recall_count_36m += max(0, int(item["source_count"] or 1) - 1)
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
        update_latest_fda_event_date(row, event_day.isoformat())


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
    if has_fda_records:
        revenue_base = revenue_normalizer(row, policy=policy)
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
    row.fda_product_score = round(
        clamp(
            policy.regulatory_risk_weight * row.regulatory_risk_score
            + policy.regulatory_innovation_weight * row.regulatory_innovation_score
        ),
        2,
    )
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
            "open_class_i_recall_12m": row.open_class_i_recall_count_12m,
            "open_class_i_recall_36m": row.open_class_i_recall_count_36m,
            "terminated_class_i_recall_36m": row.terminated_class_i_recall_count_36m,
            "canonical_recall_duplicate_source_count": row.canonical_recall_duplicate_source_count,
            "recall_severity_36m": row.recall_severity_36m,
            "death_24m": row.death_count_24m,
            "injury_24m": row.injury_count_24m,
            "malfunction_24m": row.malfunction_count_24m,
            "current_adverse_24m": row.current_adverse_event_count_24m,
            "previous_adverse_24m": row.prev_adverse_event_count_24m,
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
        row.fda_product_score = round(clamp(score), 2)
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
) -> list[FdaFeatureRow]:
    review_overrides = review_overrides or {}
    footprint_overrides = footprint_overrides or {}
    manual_evidence = manual_evidence or {}
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
    return rows


def upsert_feature_rows(conn: Any, rows: list[FdaFeatureRow]) -> int:
    if not rows:
        return 0
    now = utc_now()
    conn.executemany(
        """
        INSERT INTO feature_fda_product_risk(
            asof_date, company_id, regulatory_innovation_score, regulatory_risk_score,
            fda_product_score, fda_data_available, latest_fda_event_date,
            mapped_manufacturer_count, avg_mapping_confidence, risk_mapping_confidence_min,
            hard_red_flag,
            hard_red_flag_reasons, raw_fda_red_flag, confirmed_hard_red_flag,
            review_adjusted_fda_state, dedup_class_i_recall_count_36m,
            open_class_i_recall_count_12m, open_class_i_recall_count_36m,
            terminated_class_i_recall_count_36m, canonical_recall_duplicate_source_count,
            review_reason, clearance_metrics_suppressed, clearance_metrics_suppression_reason,
            approval_product_code_filter, approval_product_code_filter_note,
            fda_evidence_type, regulatory_stage, evidence_confidence, next_review_date,
            manual_evidence_note,
            payload_json, created_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(asof_date, company_id) DO UPDATE SET
            regulatory_innovation_score = excluded.regulatory_innovation_score,
            regulatory_risk_score = excluded.regulatory_risk_score,
            fda_product_score = excluded.fda_product_score,
            fda_data_available = excluded.fda_data_available,
            latest_fda_event_date = excluded.latest_fda_event_date,
            mapped_manufacturer_count = excluded.mapped_manufacturer_count,
            avg_mapping_confidence = excluded.avg_mapping_confidence,
            risk_mapping_confidence_min = excluded.risk_mapping_confidence_min,
            hard_red_flag = excluded.hard_red_flag,
            hard_red_flag_reasons = excluded.hard_red_flag_reasons,
            raw_fda_red_flag = excluded.raw_fda_red_flag,
            confirmed_hard_red_flag = excluded.confirmed_hard_red_flag,
            review_adjusted_fda_state = excluded.review_adjusted_fda_state,
            dedup_class_i_recall_count_36m = excluded.dedup_class_i_recall_count_36m,
            open_class_i_recall_count_12m = excluded.open_class_i_recall_count_12m,
            open_class_i_recall_count_36m = excluded.open_class_i_recall_count_36m,
            terminated_class_i_recall_count_36m = excluded.terminated_class_i_recall_count_36m,
            canonical_recall_duplicate_source_count = excluded.canonical_recall_duplicate_source_count,
            review_reason = excluded.review_reason,
            clearance_metrics_suppressed = excluded.clearance_metrics_suppressed,
            clearance_metrics_suppression_reason = excluded.clearance_metrics_suppression_reason,
            approval_product_code_filter = excluded.approval_product_code_filter,
            approval_product_code_filter_note = excluded.approval_product_code_filter_note,
            fda_evidence_type = excluded.fda_evidence_type,
            regulatory_stage = excluded.regulatory_stage,
            evidence_confidence = excluded.evidence_confidence,
            next_review_date = excluded.next_review_date,
            manual_evidence_note = excluded.manual_evidence_note,
            payload_json = excluded.payload_json,
            updated_at = excluded.updated_at
        """,
        [
            (
                row.asof_date,
                row.company_id,
                row.regulatory_innovation_score,
                row.regulatory_risk_score,
                row.fda_product_score,
                row.fda_data_available,
                row.latest_fda_event_date,
                row.mapped_manufacturer_count,
                row.avg_mapping_confidence,
                row.risk_mapping_confidence_min,
                row.hard_red_flag,
                ";".join(row.hard_red_flag_reasons or []),
                row.raw_fda_red_flag,
                row.confirmed_hard_red_flag,
                row.review_adjusted_fda_state,
                row.dedup_class_i_recall_count_36m,
                row.open_class_i_recall_count_12m,
                row.open_class_i_recall_count_36m,
                row.terminated_class_i_recall_count_36m,
                row.canonical_recall_duplicate_source_count,
                row.review_reason,
                row.clearance_metrics_suppressed,
                row.clearance_metrics_suppression_reason,
                row.approval_product_code_filter,
                row.approval_product_code_filter_note,
                row.fda_evidence_type,
                row.regulatory_stage,
                row.evidence_confidence,
                row.next_review_date,
                row.manual_evidence_note,
                json.dumps(row.payload or {}, ensure_ascii=True, sort_keys=True),
                now,
                now,
            )
            for row in rows
        ],
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
            or row.review_adjusted_fda_state
            in {"mapping_review_required", "regulatory_review_required", "confirmed_hard_red"}
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
