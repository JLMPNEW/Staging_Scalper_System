#!/usr/bin/env python3
from __future__ import annotations

import argparse
import calendar
import csv
import json
import logging
import math
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
    "regulatory_innovation_score",
    "regulatory_risk_score",
    "fda_product_score",
    "hard_red_flag",
    "hard_red_flag_reasons",
    "review_reason",
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
    regulatory_innovation_score: float = 0.0
    regulatory_risk_score: float = 0.0
    fda_product_score: float = 0.0
    hard_red_flag: int = 0
    hard_red_flag_reasons: list[str] | None = None
    review_reason: str = ""
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
    regulatory_risk_weight: float
    regulatory_innovation_weight: float


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


def fda_feature_policy(config: dict[str, Any]) -> FdaFeaturePolicy:
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
        regulatory_risk_weight=risk_weight,
        regulatory_innovation_weight=innovation_weight,
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


def update_latest_fda_event_date(row: FdaFeatureRow, event_date: str) -> None:
    if not event_date:
        return
    if not row.latest_fda_event_date or event_date > row.latest_fda_event_date:
        row.latest_fda_event_date = event_date


def count_approvals(conn: Any, row: FdaFeatureRow, *, asof: date, policy: FdaFeaturePolicy) -> None:
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
        day = str(item["decision_date"] or "")
        if day >= short_start:
            row.approval_count_12m += 1
        if day >= medium_start:
            row.approval_count_24m += 1
        row.approval_count_36m += 1
        if "PMA" in str(item["submission_type"] or "").upper():
            row.pma_count_36m += 1
        product_code = str(item["product_code"] or "").strip()
        if product_code:
            product_codes.add(product_code)
        update_latest_fda_event_date(row, day)
    row.product_code_count_36m = len(product_codes)


def is_class_i(classification: object) -> bool:
    text = str(classification or "").strip().lower().replace(" ", "_")
    return "class_i" in text or text == "i"


def count_recalls(conn: Any, row: FdaFeatureRow, *, asof: date, policy: FdaFeaturePolicy) -> None:
    long_start = months_before(asof, policy.long_months).isoformat()
    medium_start = months_before(asof, policy.medium_months).isoformat()
    rows = conn.execute(
        """
        SELECT classification, COALESCE(severity_weight, 1.0) AS severity_weight,
               COALESCE(recall_initiation_date, center_classification_date) AS event_date
        FROM fact_fda_recall
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
        event_day = parse_date(event_date)
        days_since = (asof - event_day).days if event_day is not None else 0
        decay = 0.5 ** (max(0, days_since) / max(1.0, policy.recall_decay_half_life_days))
        row.recall_severity_36m += float(item["severity_weight"] or 1.0) * decay
        if is_class_i(item["classification"]):
            row.class_i_recall_count_36m += 1
        update_latest_fda_event_date(row, event_date)


def count_adverse_events(conn: Any, row: FdaFeatureRow, *, asof: date, policy: FdaFeaturePolicy) -> None:
    medium_start = months_before(asof, policy.medium_months)
    previous_start = months_before(asof, policy.medium_months * 2)
    rows = conn.execute(
        """
        SELECT report_date, death_count, injury_count, malfunction_count
        FROM fact_fda_adverse_event
        WHERE company_id = ?
          AND COALESCE(report_date, event_date, '') != ''
          AND COALESCE(report_date, event_date) <= ?
          AND COALESCE(report_date, event_date) >= ?
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
            + math.log1p(row.approval_count_24m) * policy.innovation_approval_log_weight
            + math.log1p(row.pma_count_36m) * policy.innovation_pma_log_weight
            + math.log1p(row.product_code_count_36m) * policy.innovation_product_code_log_weight
        )
        recency_multiplier = 0.5 + 0.5 * ((row.fda_data_recency_score or 0.0) / 100.0)
        row.regulatory_innovation_score = round(clamp(raw_innovation * recency_multiplier), 2)
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

    reasons: list[str] = []
    if row.class_i_recall_count_36m > 0:
        reasons.append("recent_class_i_recall")
    if row.death_count_24m >= policy.death_event_min_count:
        reasons.append("recent_death_adverse_event")
    if row.avg_mapping_confidence is not None and row.avg_mapping_confidence < policy.min_mapping_confidence:
        reasons.append("low_fda_mapping_confidence")
    row.hard_red_flag_reasons = reasons
    row.hard_red_flag = 1 if reasons else 0
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
            "min_high_confidence": policy.min_mapping_confidence,
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


def build_rows(conn: Any, companies: list[Company], *, asof: date, policy: FdaFeaturePolicy) -> list[FdaFeatureRow]:
    rows: list[FdaFeatureRow] = []
    for company in companies:
        row = FdaFeatureRow(
            asof_date=asof.isoformat(),
            company_id=company.company_id,
            ticker=company.ticker,
            company_name=company.company_name,
        )
        count_approvals(conn, row, asof=asof, policy=policy)
        count_recalls(conn, row, asof=asof, policy=policy)
        count_adverse_events(conn, row, asof=asof, policy=policy)
        manufacturer_mapping_summary(conn, row)
        row.revenue_ttm = latest_revenue_ttm(conn, company.company_id, asof=asof)
        score_row(row, policy=policy)
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
            mapped_manufacturer_count, avg_mapping_confidence, hard_red_flag,
            hard_red_flag_reasons, review_reason, payload_json, created_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(asof_date, company_id) DO UPDATE SET
            regulatory_innovation_score = excluded.regulatory_innovation_score,
            regulatory_risk_score = excluded.regulatory_risk_score,
            fda_product_score = excluded.fda_product_score,
            fda_data_available = excluded.fda_data_available,
            latest_fda_event_date = excluded.latest_fda_event_date,
            mapped_manufacturer_count = excluded.mapped_manufacturer_count,
            avg_mapping_confidence = excluded.avg_mapping_confidence,
            hard_red_flag = excluded.hard_red_flag,
            hard_red_flag_reasons = excluded.hard_red_flag_reasons,
            review_reason = excluded.review_reason,
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
                row.hard_red_flag,
                ";".join(row.hard_red_flag_reasons or []),
                row.review_reason,
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
        "DELETE FROM data_quality_issues WHERE asof_date = ? AND table_name = ?",
        (asof, "feature_fda_product_risk"),
    )
    now = utc_now()
    issue_rows: list[tuple[Any, ...]] = []
    for row in rows:
        reasons: list[str] = []
        if row.review_reason:
            reasons.append(row.review_reason)
        if row.hard_red_flag_reasons:
            reasons.extend(row.hard_red_flag_reasons)
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
            rows = build_rows(conn, companies, asof=asof, policy=policy)
            upserted = upsert_feature_rows(conn, rows)
            issue_count = replace_data_quality_issues(conn, rows, asof=asof.isoformat())
            write_csv(output_csv, rows)
            red_flags = sum(1 for row in rows if row.hard_red_flag)
            message = f"asof={asof.isoformat()} rows={upserted} red_flags={red_flags} issues={issue_count} output={output_csv}"
            finish_run(conn, run_id=run_id, status="success", row_count=upserted, message=message)
            LOGGER.info("FDA features complete: %s", message)
        except BaseException as exc:
            finish_run(conn, run_id=run_id, status="failed", row_count=0, message=f"{type(exc).__name__}: {exc}")
            raise


if __name__ == "__main__":
    main()
