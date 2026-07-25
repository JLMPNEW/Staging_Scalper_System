#!/usr/bin/env python3
"""Build and persist governed FDA product-family shadow-risk features."""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sqlite3
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from med_devices.core.config import cfg_get, load_yaml, resolve_path  # noqa: E402
from med_devices.core.db import connect, init_db, utc_now  # noqa: E402
from med_devices.core.fda_product_family_review import (  # noqa: E402
    EXPOSURE_STATUS_AVAILABLE,
    EXPOSURE_STATUS_WAIVED,
    ProductFamilyExposure,
    ProductFamilyMapping,
    ProductFamilyShadowScore,
    as_float,
    as_int,
    build_product_family_shadow_score,
    earliest_date,
    load_product_family_exposures,
    load_product_family_mappings,
    mapping_coverage,
    mapping_for,
    normalized_family,
    normalized_text,
    normalized_ticker,
    structured_mdr_metadata,
)
from med_devices.core.point_in_time import parse_iso_date  # noqa: E402


DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
CONFIG_KEY = "fda_product_family_review"

MDR_FIELDS = [
    "asof_date",
    "ticker",
    "company_id",
    "adverse_event_id",
    "report_number",
    "mdr_report_key",
    "event_key",
    "event_date",
    "report_date",
    "source_available_date",
    "event_type",
    "death_designated_flag",
    "structured_death_outcome_flag",
    "injury_flag",
    "malfunction_flag",
    "summary_report_flag",
    "number_events_summarized",
    "report_submission_type",
    "supplement_count",
    "exemption_number",
    "initial_report_to_fda",
    "fda_manufacturer_id",
    "manufacturer_name",
    "manufacturer_mapping_confidence",
    "manufacturer_mapping_method",
    "product_code",
    "fda_device_name",
    "product_family",
    "family_mapping_confidence",
    "family_mapping_method",
    "family_mapping_source",
    "family_mapping_valid_from",
    "family_mapping_reviewed_at",
    "causality_status",
    "manual_review_required",
    "exception_reasons",
]
RECALL_FIELDS = [
    "asof_date",
    "ticker",
    "company_id",
    "canonical_recall_key",
    "recall_number",
    "event_id",
    "classification",
    "recall_initiation_date",
    "center_classification_date",
    "source_available_date",
    "termination_date",
    "status",
    "is_open",
    "is_terminated",
    "source_count",
    "source_endpoints",
    "fda_manufacturer_id",
    "manufacturer_name",
    "manufacturer_mapping_confidence",
    "manufacturer_mapping_method",
    "product_code",
    "product_description",
    "reason_for_recall",
    "product_family",
    "family_mapping_confidence",
    "family_mapping_method",
    "family_mapping_source",
    "family_mapping_valid_from",
    "family_mapping_reviewed_at",
    "causality_status",
    "manual_review_required",
    "exception_reasons",
]
QA_FIELDS = [
    "asof_date",
    "ticker",
    "check_name",
    "status",
    "observed",
    "required",
    "total_count",
    "covered_count",
    "detail",
]
EXCEPTION_FIELDS = [
    "asof_date",
    "ticker",
    "priority",
    "source_type",
    "product_family",
    "product_code",
    "fda_manufacturer_id",
    "manufacturer_name",
    "record_count",
    "death_designated_count",
    "injury_count",
    "malfunction_count",
    "reported_events_summarized",
    "record_ids",
    "exception_reasons",
    "recommended_action",
]
SUMMARY_FIELDS = [
    "asof_date",
    "ticker",
    "company_id",
    "source_data_through",
    "mdr_window_start",
    "recall_window_start",
    "mdr_record_count",
    "death_designated_mdr_count",
    "reported_events_summarized_by_death_mdrs",
    "injury_mdr_count",
    "malfunction_mdr_count",
    "class_i_recall_family_count",
    "distinct_product_family_count",
    "family_exposure_available_count",
    "family_exposure_waived_count",
    "family_exposure_missing_count",
    "fda_event_risk_product_family_adjusted_score",
    "fda_safety_product_family_adjusted_score",
    "fda_product_family_shadow_available_flag",
    "fda_product_family_shadow_oos_valid_flag",
    "fda_product_family_adjustment_applied_flag",
    "fda_product_family_shadow_status",
    "fda_product_family_shadow_reason",
    "qa_pass_count",
    "qa_warning_count",
    "qa_failure_count",
    "review_status",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build FDA product-family MDR, recall, QA, and exception review ledgers."
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument(
        "--asof",
        default="",
        help=(
            "Review as-of date (YYYY-MM-DD); blank resolves to the latest "
            "feature_fda_product_risk asof_date, matching the other pipeline steps."
        ),
    )
    parser.add_argument("--ticker", action="append", default=[])
    parser.add_argument("--mapping-csv", type=Path, default=None)
    parser.add_argument("--exposure-csv", type=Path, default=None)
    parser.add_argument("--output-dir", type=Path, default=None)
    parser.add_argument(
        "--warn-only",
        action="store_true",
        help="Write all artifacts but do not fail when QA thresholds are not met.",
    )
    return parser.parse_args()


def months_before(value: date, months: int) -> date:
    month_index = value.year * 12 + value.month - 1 - months
    year, zero_based_month = divmod(month_index, 12)
    month = zero_based_month + 1
    if month == 12:
        next_month = date(year + 1, 1, 1)
    else:
        next_month = date(year, month + 1, 1)
    last_day = (next_month - date.resolution).day
    return date(year, month, min(value.day, last_day))


def write_csv(path: Path, rows: list[dict[str, Any]], fields: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=fields,
            extrasaction="ignore",
            lineterminator="\n",
        )
        writer.writeheader()
        writer.writerows(rows)
    os.replace(tmp, path)


def exact_class_i(raw: object) -> bool:
    normalized = re.sub(r"[^a-z0-9]+", " ", normalized_text(raw).lower()).strip()
    return normalized in {"class i", "class 1"}


def mapping_fields(mapping: ProductFamilyMapping | None) -> dict[str, Any]:
    if mapping is None:
        return {
            "product_family": "",
            "family_mapping_confidence": "",
            "family_mapping_method": "",
            "family_mapping_source": "",
            "family_mapping_valid_from": "",
            "family_mapping_reviewed_at": "",
        }
    return {
        "product_family": mapping.product_family,
        "family_mapping_confidence": round(mapping.mapping_confidence, 4),
        "family_mapping_method": mapping.mapping_method,
        "family_mapping_source": mapping.source_reference,
        "family_mapping_valid_from": mapping.valid_from,
        "family_mapping_reviewed_at": mapping.reviewed_at,
    }


def mdr_rows(
    conn: sqlite3.Connection,
    *,
    company_id: int,
    ticker: str,
    asof: date,
    window_start: date,
    mappings: list[ProductFamilyMapping],
    minimum_family_confidence: float,
    minimum_manufacturer_confidence: float,
) -> list[dict[str, Any]]:
    raw_rows = conn.execute(
        """
        SELECT e.adverse_event_id, e.event_date, e.report_date, e.report_type,
               e.death_count, e.injury_count, e.malfunction_count, e.event_type,
               e.fda_manufacturer_id, e.product_code, e.payload_json,
               m.manufacturer_name, m.mapping_confidence, m.mapping_method,
               p.device_name
        FROM fact_fda_adverse_event e
        LEFT JOIN dim_fda_manufacturer m
          ON m.fda_manufacturer_id = e.fda_manufacturer_id
        LEFT JOIN dim_fda_product_code p
          ON p.product_code = e.product_code
        WHERE e.company_id = ?
          AND COALESCE(e.report_date, e.event_date, '') != ''
          AND COALESCE(e.report_date, e.event_date) BETWEEN ? AND ?
        ORDER BY COALESCE(e.report_date, e.event_date), e.adverse_event_id
        """,
        (company_id, window_start.isoformat(), asof.isoformat()),
    ).fetchall()
    output: list[dict[str, Any]] = []
    for raw in raw_rows:
        metadata = structured_mdr_metadata(raw["payload_json"])
        mapping = mapping_for(
            mappings,
            ticker=ticker,
            product_code=raw["product_code"],
            manufacturer_id=raw["fda_manufacturer_id"],
        )
        death = max(0, as_int(raw["death_count"]))
        injury = max(0, as_int(raw["injury_count"]))
        malfunction = max(0, as_int(raw["malfunction_count"]))
        reasons: list[str] = []
        family_confidence = mapping.mapping_confidence if mapping is not None else 0.0
        manufacturer_confidence = as_float(raw["mapping_confidence"]) or 0.0
        if mapping is None:
            reasons.append("unmapped_product_family")
        elif family_confidence < minimum_family_confidence:
            reasons.append("product_family_mapping_below_confidence_threshold")
        if death and manufacturer_confidence < minimum_manufacturer_confidence:
            reasons.append("severe_event_manufacturer_mapping_below_confidence_threshold")
        if death:
            reasons.append("death_causality_review_required")
        if metadata["summary_report_flag"]:
            reasons.append("summary_report_scope_review_required")
        output.append(
            {
                "asof_date": asof.isoformat(),
                "ticker": ticker,
                "company_id": company_id,
                "adverse_event_id": raw["adverse_event_id"],
                "report_number": metadata["report_number"],
                "mdr_report_key": metadata["mdr_report_key"],
                "event_key": metadata["event_key"],
                "event_date": raw["event_date"] or "",
                "report_date": raw["report_date"] or "",
                "source_available_date": raw["report_date"] or raw["event_date"] or "",
                "event_type": raw["event_type"] or "",
                "death_designated_flag": death,
                "structured_death_outcome_flag": metadata[
                    "structured_death_outcome_flag"
                ],
                "injury_flag": injury,
                "malfunction_flag": malfunction,
                "summary_report_flag": metadata["summary_report_flag"],
                "number_events_summarized": metadata["number_events_summarized"],
                "report_submission_type": metadata["report_submission_type"],
                "supplement_count": metadata["supplement_count"],
                "exemption_number": metadata["exemption_number"],
                "initial_report_to_fda": metadata["initial_report_to_fda"],
                "fda_manufacturer_id": raw["fda_manufacturer_id"] or "",
                "manufacturer_name": raw["manufacturer_name"] or "",
                "manufacturer_mapping_confidence": round(
                    manufacturer_confidence, 4
                ),
                "manufacturer_mapping_method": raw["mapping_method"] or "",
                "product_code": raw["product_code"] or "",
                "fda_device_name": raw["device_name"] or "",
                **mapping_fields(mapping),
                "causality_status": "not_assessed" if death or injury else "not_applicable",
                "manual_review_required": int(bool(reasons)),
                "exception_reasons": ";".join(dict.fromkeys(reasons)),
            }
        )
    return output


def recall_rows(
    conn: sqlite3.Connection,
    *,
    company_id: int,
    ticker: str,
    asof: date,
    window_start: date,
    mappings: list[ProductFamilyMapping],
    minimum_family_confidence: float,
    minimum_manufacturer_confidence: float,
) -> list[dict[str, Any]]:
    # dim_fda_manufacturer is the authoritative manufacturer-mapping
    # confidence/method source (dim-first COALESCE), matching mdr_rows, which
    # reads the dim values only. The fact-row values are a fallback solely for
    # canonical recalls whose fda_manufacturer_id never joined the dim; they
    # are NOT per-recall overrides — record any override in the dim/governed
    # mapping layer instead.
    raw_rows = conn.execute(
        """
        SELECT r.*, m.manufacturer_name,
               COALESCE(m.mapping_confidence, r.mapping_confidence) AS manufacturer_confidence,
               COALESCE(NULLIF(m.mapping_method, ''), r.mapping_method) AS manufacturer_method
        FROM fact_fda_recall_canonical r
        LEFT JOIN dim_fda_manufacturer m
          ON m.fda_manufacturer_id = r.fda_manufacturer_id
        WHERE r.company_id = ?
          AND COALESCE(r.recall_initiation_date, r.center_classification_date, '') != ''
          AND COALESCE(r.recall_initiation_date, r.center_classification_date)
              BETWEEN ? AND ?
        ORDER BY COALESCE(r.recall_initiation_date, r.center_classification_date),
                 r.canonical_recall_key
        """,
        (company_id, window_start.isoformat(), asof.isoformat()),
    ).fetchall()
    output: list[dict[str, Any]] = []
    for raw in raw_rows:
        if not exact_class_i(raw["classification"]):
            continue
        mapping = mapping_for(
            mappings,
            ticker=ticker,
            product_code=raw["product_code"],
            manufacturer_id=raw["fda_manufacturer_id"],
        )
        confidence = as_float(raw["manufacturer_confidence"]) or 0.0
        reasons = ["class_i_materiality_review_required"]
        if mapping is None:
            reasons.append("unmapped_product_family")
        elif mapping.mapping_confidence < minimum_family_confidence:
            reasons.append("product_family_mapping_below_confidence_threshold")
        if confidence < minimum_manufacturer_confidence:
            reasons.append(
                "severe_event_manufacturer_mapping_below_confidence_threshold"
            )
        output.append(
            {
                "asof_date": asof.isoformat(),
                "ticker": ticker,
                "company_id": company_id,
                "canonical_recall_key": raw["canonical_recall_key"] or "",
                "recall_number": raw["recall_number"] or "",
                "event_id": raw["event_id"] or "",
                "classification": raw["classification"] or "",
                "recall_initiation_date": raw["recall_initiation_date"] or "",
                "center_classification_date": raw["center_classification_date"]
                or "",
                "source_available_date": earliest_date(
                    raw["center_classification_date"],
                    raw["recall_initiation_date"],
                ),
                "termination_date": raw["termination_date"] or "",
                "status": raw["status"] or "",
                "is_open": as_int(raw["is_open"]),
                "is_terminated": as_int(raw["is_terminated"]),
                "source_count": as_int(raw["source_count"]),
                "source_endpoints": raw["source_endpoints"] or "",
                "fda_manufacturer_id": raw["fda_manufacturer_id"] or "",
                "manufacturer_name": raw["manufacturer_name"] or "",
                "manufacturer_mapping_confidence": round(confidence, 4),
                "manufacturer_mapping_method": raw["manufacturer_method"] or "",
                "product_code": raw["product_code"] or "",
                "product_description": raw["product_description"] or "",
                "reason_for_recall": raw["reason_for_recall"] or "",
                **mapping_fields(mapping),
                "causality_status": "not_assessed",
                "manual_review_required": 1,
                "exception_reasons": ";".join(dict.fromkeys(reasons)),
            }
        )
    return output


def qa_row(
    *,
    asof: date,
    ticker: str,
    name: str,
    status: str,
    observed: object,
    required: object,
    total: int = 0,
    covered: int = 0,
    detail: str = "",
) -> dict[str, Any]:
    return {
        "asof_date": asof.isoformat(),
        "ticker": ticker,
        "check_name": name,
        "status": status,
        "observed": observed,
        "required": required,
        "total_count": total,
        "covered_count": covered,
        "detail": detail,
    }


def build_qa_rows(
    *,
    asof: date,
    ticker: str,
    mdr: list[dict[str, Any]],
    recalls: list[dict[str, Any]],
    exposures: list[ProductFamilyExposure],
    governance_critical_count: int,
    minimum_family_confidence: float,
    minimum_manufacturer_confidence: float,
    death_coverage_min: float,
    injury_coverage_min: float,
    malfunction_coverage_min: float,
    class_i_coverage_min: float,
) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for name, field, required in (
        ("death_product_family_coverage", "death_designated_flag", death_coverage_min),
        ("injury_product_family_coverage", "injury_flag", injury_coverage_min),
        (
            "malfunction_product_family_coverage",
            "malfunction_flag",
            malfunction_coverage_min,
        ),
    ):
        total, covered, percentage = mapping_coverage(
            mdr,
            count_field=field,
            minimum_mapping_confidence=minimum_family_confidence,
        )
        output.append(
            qa_row(
                asof=asof,
                ticker=ticker,
                name=name,
                status="PASS" if percentage >= required else "FAIL",
                observed=f"{percentage:.4f}",
                required=f">={required:.4f}",
                total=total,
                covered=covered,
            )
        )
    class_i_total = len(recalls)
    class_i_covered = sum(
        1
        for row in recalls
        if normalized_text(row.get("product_family"))
        and (as_float(row.get("family_mapping_confidence")) or 0.0)
        >= minimum_family_confidence
    )
    class_i_percentage = (
        100.0 if class_i_total == 0 else 100.0 * class_i_covered / class_i_total
    )
    output.append(
        qa_row(
            asof=asof,
            ticker=ticker,
            name="class_i_product_family_coverage",
            status="PASS"
            if class_i_percentage >= class_i_coverage_min
            else "FAIL",
            observed=f"{class_i_percentage:.4f}",
            required=f">={class_i_coverage_min:.4f}",
            total=class_i_total,
            covered=class_i_covered,
        )
    )
    severe_confidences = [
        as_float(row.get("manufacturer_mapping_confidence")) or 0.0
        for row in mdr
        if as_int(row.get("death_designated_flag"))
    ] + [
        as_float(row.get("manufacturer_mapping_confidence")) or 0.0
        for row in recalls
    ]
    minimum_observed = min(severe_confidences) if severe_confidences else 100.0
    output.append(
        qa_row(
            asof=asof,
            ticker=ticker,
            name="severe_event_manufacturer_mapping_confidence",
            status="PASS"
            if minimum_observed >= minimum_manufacturer_confidence
            else "FAIL",
            observed=f"{minimum_observed:.4f}",
            required=f">={minimum_manufacturer_confidence:.4f}",
            total=len(severe_confidences),
            covered=sum(
                1
                for value in severe_confidences
                if value >= minimum_manufacturer_confidence
            ),
        )
    )
    output.append(
        qa_row(
            asof=asof,
            ticker=ticker,
            name="governed_input_integrity",
            status="PASS" if governance_critical_count == 0 else "FAIL",
            observed=governance_critical_count,
            required="0",
            detail="Critical mapping/exposure CSV validation issues.",
        )
    )
    ticker_families = {
        normalized_family(row.get("product_family"))
        for row in [*mdr, *recalls]
        if normalized_family(row.get("product_family"))
    }
    available_families = {
        exposure.product_family
        for exposure in exposures
        if exposure.ticker == ticker
        and exposure.exposure_status == EXPOSURE_STATUS_AVAILABLE
        and exposure.exposure_value is not None
        and exposure.exposure_value > 0
    }
    waived_families = {
        exposure.product_family
        for exposure in exposures
        if exposure.ticker == ticker
        and exposure.exposure_status == EXPOSURE_STATUS_WAIVED
    }
    governed_families = available_families | waived_families
    missing_exposure = sorted(ticker_families - governed_families)
    output.append(
        qa_row(
            asof=asof,
            ticker=ticker,
            name="product_family_exposure_availability",
            status="PASS" if not missing_exposure else "WARNING",
            observed=len(governed_families.intersection(ticker_families)),
            required=len(ticker_families),
            total=len(ticker_families),
            covered=len(governed_families.intersection(ticker_families)),
            detail=(
                "missing="
                + ",".join(missing_exposure)
                + ";waived="
                + ",".join(sorted(waived_families.intersection(ticker_families)))
            ),
        )
    )
    return output


def exception_priority(reasons: set[str]) -> str:
    if {
        "unmapped_product_family",
        "severe_event_manufacturer_mapping_below_confidence_threshold",
        "death_causality_review_required",
        "class_i_materiality_review_required",
    }.intersection(reasons):
        return "P1"
    if "product_family_mapping_below_confidence_threshold" in reasons:
        return "P2"
    return "P3"


def exception_action(reasons: set[str]) -> str:
    if "unmapped_product_family" in reasons:
        return "Assign an effective-dated product-family mapping from primary FDA evidence."
    if "severe_event_manufacturer_mapping_below_confidence_threshold" in reasons:
        return "Verify legal parent ownership and add a governed manufacturer override only with primary evidence."
    if "death_causality_review_required" in reasons:
        return "Review structured MDR scope and causality; retain severe weight while causality is unknown."
    if "class_i_materiality_review_required" in reasons:
        return "Confirm root-cause family, affected products, status, and commercial materiality."
    if "summary_report_scope_review_required" in reasons:
        return "Confirm the summarized-event scope without treating report count as patient count."
    return "Complete the governed FDA product-family review."


def build_exceptions(
    *,
    asof: date,
    ticker: str,
    mdr: list[dict[str, Any]],
    recalls: list[dict[str, Any]],
    exposures: list[ProductFamilyExposure],
) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str, str, str, str, str], dict[str, Any]] = {}
    for source_type, rows, id_field in (
        ("mdr", mdr, "adverse_event_id"),
        ("class_i_recall", recalls, "canonical_recall_key"),
    ):
        for row in rows:
            reasons = {
                reason
                for reason in normalized_text(row.get("exception_reasons")).split(";")
                if reason
            }
            if not reasons:
                continue
            key = (
                source_type,
                normalized_family(row.get("product_family")) or "unmapped",
                normalized_text(row.get("product_code")),
                normalized_text(row.get("fda_manufacturer_id")),
                normalized_text(row.get("manufacturer_name")),
                ";".join(sorted(reasons)),
            )
            group = grouped.setdefault(
                key,
                {
                    "asof_date": asof.isoformat(),
                    "ticker": ticker,
                    "priority": exception_priority(reasons),
                    "source_type": source_type,
                    "product_family": key[1],
                    "product_code": key[2],
                    "fda_manufacturer_id": key[3],
                    "manufacturer_name": key[4],
                    "record_count": 0,
                    "death_designated_count": 0,
                    "injury_count": 0,
                    "malfunction_count": 0,
                    "reported_events_summarized": 0,
                    "record_ids": [],
                    "exception_reasons": key[5],
                    "recommended_action": exception_action(reasons),
                },
            )
            group["record_count"] += 1
            group["death_designated_count"] += as_int(
                row.get("death_designated_flag")
            )
            group["injury_count"] += as_int(row.get("injury_flag"))
            group["malfunction_count"] += as_int(row.get("malfunction_flag"))
            group["reported_events_summarized"] += as_int(
                row.get("number_events_summarized"), 1
            )
            group["record_ids"].append(normalized_text(row.get(id_field)))
    for exposure in exposures:
        if exposure.ticker != ticker or exposure.exposure_status in {
            EXPOSURE_STATUS_AVAILABLE,
            EXPOSURE_STATUS_WAIVED,
        }:
            continue
        key = (
            "exposure",
            exposure.product_family,
            "",
            "",
            "",
            "product_family_exposure_missing",
        )
        grouped[key] = {
            "asof_date": asof.isoformat(),
            "ticker": ticker,
            "priority": "P3",
            "source_type": "exposure",
            "product_family": exposure.product_family,
            "product_code": "",
            "fda_manufacturer_id": "",
            "manufacturer_name": "",
            "record_count": 1,
            "death_designated_count": 0,
            "injury_count": 0,
            "malfunction_count": 0,
            "reported_events_summarized": 0,
            "record_ids": [exposure.exposure_metric],
            "exception_reasons": "product_family_exposure_missing",
            "recommended_action": (
                "Add a sourced PIT product-family exposure denominator; do not infer or backfill an unsourced value."
            ),
        }
    output: list[dict[str, Any]] = []
    for group in grouped.values():
        group["record_ids"] = "|".join(
            record_id for record_id in group["record_ids"] if record_id
        )
        output.append(group)
    priority_order = {"P1": 0, "P2": 1, "P3": 2}
    return sorted(
        output,
        key=lambda row: (
            priority_order.get(str(row["priority"]), 9),
            str(row["source_type"]),
            str(row["product_family"]),
            str(row["product_code"]),
        ),
    )


def configured_tickers(config: dict[str, Any], cli_tickers: list[str]) -> list[str]:
    if cli_tickers:
        values = cli_tickers
    else:
        raw = cfg_get(config, f"{CONFIG_KEY}.target_tickers", ["ABT"])
        values = raw if isinstance(raw, list) else str(raw or "").split(",")
    return sorted(
        {
            ticker
            for value in values
            if (ticker := normalized_ticker(value))
        }
    )


def persist_shadow_score(
    conn: sqlite3.Connection,
    *,
    asof: date,
    company_id: int,
    shadow: ProductFamilyShadowScore,
    shadow_oos_valid_flag: int,
    production_usage: str,
) -> int:
    feature = conn.execute(
        """
        SELECT payload_json, fda_product_family_shadow_oos_valid_flag
        FROM feature_fda_product_risk
        WHERE asof_date = ? AND company_id = ?
        """,
        (asof.isoformat(), company_id),
    ).fetchone()
    if feature is None:
        raise RuntimeError(
            "FDA product-family shadow requires the same-asof "
            "feature_fda_product_risk row; run script 10 first"
        )
    # Never demote an earned live-capture OOS flag: a replay of a past asof
    # outside scoring.oos_replay_window_days recomputes shadow_oos_valid_flag
    # as 0 from wall-clock age, but a stored 1 was earned by live capture and
    # no script can ever restore it. Retain the stored 1 while the replayed
    # shadow is still available; only a blocked/unavailable shadow clears it,
    # because a null score with oos_valid_flag=1 would be incoherent.
    existing_oos_valid_flag = as_int(
        feature["fda_product_family_shadow_oos_valid_flag"]
    )
    persisted_oos_valid_flag = int(
        bool(shadow.available_flag)
        and (shadow_oos_valid_flag == 1 or existing_oos_valid_flag == 1)
    )
    try:
        payload = json.loads(str(feature["payload_json"] or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    payload["fda_product_family_shadow"] = {
        "event_risk_score": shadow.event_risk_score,
        "safety_score": shadow.safety_score,
        "available_flag": shadow.available_flag,
        "oos_valid_flag": persisted_oos_valid_flag,
        "adjustment_applied_flag": shadow.adjustment_applied_flag,
        "exposure_available_count": shadow.exposure_available_count,
        "exposure_waived_count": shadow.exposure_waived_count,
        "exposure_missing_count": shadow.exposure_missing_count,
        "status": shadow.status,
        "reason": shadow.reason,
        "family_details": shadow.family_details,
        "production_usage": production_usage,
    }
    conn.execute(
        """
        UPDATE feature_fda_product_risk
        SET fda_event_risk_product_family_adjusted_score = ?,
            fda_safety_product_family_adjusted_score = ?,
            fda_product_family_shadow_available_flag = ?,
            fda_product_family_shadow_oos_valid_flag = ?,
            fda_product_family_adjustment_applied_flag = ?,
            fda_product_family_exposure_available_count = ?,
            fda_product_family_exposure_waived_count = ?,
            fda_product_family_exposure_missing_count = ?,
            fda_product_family_shadow_status = ?,
            fda_product_family_shadow_reason = ?,
            payload_json = ?,
            updated_at = ?
        WHERE asof_date = ? AND company_id = ?
        """,
        (
            shadow.event_risk_score,
            shadow.safety_score,
            shadow.available_flag,
            persisted_oos_valid_flag,
            shadow.adjustment_applied_flag,
            shadow.exposure_available_count,
            shadow.exposure_waived_count,
            shadow.exposure_missing_count,
            shadow.status,
            shadow.reason,
            json.dumps(payload, sort_keys=True),
            utc_now(),
            asof.isoformat(),
            company_id,
        ),
    )
    return persisted_oos_valid_flag


def main() -> int:
    args = parse_args()
    asof: date | None = None
    if args.asof:
        asof = parse_iso_date(args.asof)
        if asof is None or asof.isoformat() != args.asof:
            raise ValueError("--asof must be an ISO date in YYYY-MM-DD form")
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    base_dir = config_path.parent
    db_path = (
        args.db.expanduser().resolve()
        if args.db
        else resolve_path(cfg_get(config, "paths.database_path"), base_dir=base_dir)
    )
    mapping_path = (
        args.mapping_csv.expanduser().resolve()
        if args.mapping_csv
        else resolve_path(
            cfg_get(
                config,
                f"{CONFIG_KEY}.product_family_mapping_csv",
                "data/fda_product_family_mapping.csv",
            ),
            base_dir=base_dir,
        )
    )
    exposure_path = (
        args.exposure_csv.expanduser().resolve()
        if args.exposure_csv
        else resolve_path(
            cfg_get(
                config,
                f"{CONFIG_KEY}.product_family_exposure_csv",
                "data/fda_product_family_exposure.csv",
            ),
            base_dir=base_dir,
        )
    )
    output_root = (
        args.output_dir.expanduser().resolve()
        if args.output_dir
        else resolve_path(
            cfg_get(
                config,
                f"{CONFIG_KEY}.output_dir",
                "../output/med_devices_reports/fda_product_family_review",
            ),
            base_dir=base_dir,
        )
    )
    minimum_family_confidence = float(
        cfg_get(config, f"{CONFIG_KEY}.minimum_family_mapping_confidence", 95.0)
    )
    minimum_manufacturer_confidence = float(
        cfg_get(
            config,
            f"{CONFIG_KEY}.minimum_severe_manufacturer_mapping_confidence",
            95.0,
        )
    )
    mdr_months = int(cfg_get(config, f"{CONFIG_KEY}.mdr_window_months", 24))
    recall_months = int(
        cfg_get(config, f"{CONFIG_KEY}.recall_window_months", 36)
    )
    exposure_floor_usd_millions = float(
        cfg_get(
            config,
            f"{CONFIG_KEY}.shadow_score.exposure_floor_usd_millions",
            500.0,
        )
    )
    class_i_recall_severity = float(
        cfg_get(config, "fda_features.recall_severity_weights.class_i", 5.0)
    )
    recall_severity_rate_weight = float(
        cfg_get(
            config,
            "fda_features.risk_penalties.recall_severity_per_billion_weight",
            4.0,
        )
    )
    class_i_recall_count_weight = float(
        cfg_get(
            config,
            "fda_features.risk_penalties.class_i_recall_weight",
            20.0,
        )
    )
    death_rate_weight = float(
        cfg_get(
            config,
            "fda_features.risk_penalties.death_per_billion_weight",
            5.0,
        )
    )
    injury_rate_weight = float(
        cfg_get(
            config,
            "fda_features.risk_penalties.injury_per_billion_weight",
            0.5,
        )
    )
    malfunction_rate_weight = float(
        cfg_get(
            config,
            "fda_features.risk_penalties.malfunction_per_billion_weight",
            0.1,
        )
    )
    replay_window_days = int(
        cfg_get(config, "scoring.oos_replay_window_days", 5)
    )
    production_usage = str(
        cfg_get(
            config,
            f"{CONFIG_KEY}.production_usage",
            "shadow_only_until_oos_validation",
        )
    )
    all_mdr: list[dict[str, Any]] = []
    all_recalls: list[dict[str, Any]] = []
    all_qa: list[dict[str, Any]] = []
    all_exceptions: list[dict[str, Any]] = []
    summaries: list[dict[str, Any]] = []
    tickers = configured_tickers(config, args.ticker)
    with connect(
        db_path,
        timeout_sec=float(cfg_get(config, "runtime.sqlite_timeout_sec", 30.0)),
    ) as conn:
        init_db(conn)
        if asof is None:
            latest_row = conn.execute(
                "SELECT MAX(asof_date) AS asof_date FROM feature_fda_product_risk"
            ).fetchone()
            latest_asof = (
                str(latest_row["asof_date"] or "") if latest_row is not None else ""
            )
            if not latest_asof:
                raise RuntimeError(
                    "--asof was not provided and feature_fda_product_risk is "
                    "empty; run script 10 first or pass --asof explicitly"
                )
            asof = parse_iso_date(latest_asof)
            if asof is None or asof.isoformat() != latest_asof:
                raise RuntimeError(
                    "feature_fda_product_risk holds a non-ISO MAX(asof_date): "
                    f"{latest_asof!r}"
                )
        asof_age_days = (datetime.now(timezone.utc).date() - asof).days
        live_capture_oos_valid = int(
            0 <= asof_age_days <= max(0, replay_window_days)
        )
        output_dir = (
            args.output_dir.expanduser().resolve()
            if args.output_dir
            else output_root / asof.isoformat()
        )
        mappings, mapping_issues = load_product_family_mappings(
            mapping_path, asof=asof
        )
        exposures, exposure_issues = load_product_family_exposures(
            exposure_path, asof=asof
        )
        governance_issues = [*mapping_issues, *exposure_issues]
        governance_critical_count = sum(
            issue.severity == "CRITICAL" for issue in governance_issues
        )
        for ticker in tickers:
            company = conn.execute(
                """
                SELECT company_id, ticker
                FROM dim_company
                WHERE UPPER(ticker) = ?
                """,
                (ticker,),
            ).fetchone()
            if company is None:
                raise ValueError(f"Ticker is absent from dim_company: {ticker}")
            company_id = int(company["company_id"])
            if not any(mapping.ticker == ticker for mapping in mappings):
                # Governance CRITICAL issues can drop mapping rows during load,
                # so "no mapping" is not trustworthy while they are present:
                # block instead of stamping not_effective_asof.
                if governance_critical_count > 0:
                    blocked_status = "blocked_qa_failure"
                    blocked_reason = (
                        f"{governance_critical_count} CRITICAL governed "
                        "mapping/exposure CSV issue(s); mapping absence as of "
                        f"{asof.isoformat()} cannot be trusted."
                    )
                else:
                    blocked_status = "not_effective_asof"
                    blocked_reason = (
                        "No governed product-family mapping was effective as of "
                        f"{asof.isoformat()}."
                    )
                shadow = ProductFamilyShadowScore(
                    event_risk_score=None,
                    safety_score=None,
                    available_flag=0,
                    adjustment_applied_flag=0,
                    exposure_available_count=0,
                    exposure_waived_count=0,
                    exposure_missing_count=0,
                    status=blocked_status,
                    reason=blocked_reason,
                    family_details=[],
                )
                persist_shadow_score(
                    conn,
                    asof=asof,
                    company_id=company_id,
                    shadow=shadow,
                    shadow_oos_valid_flag=0,
                    production_usage=production_usage,
                )
                summaries.append(
                    {
                        "asof_date": asof.isoformat(),
                        "ticker": ticker,
                        "company_id": company_id,
                        "source_data_through": "",
                        "mdr_window_start": months_before(
                            asof, mdr_months
                        ).isoformat(),
                        "recall_window_start": months_before(
                            asof, recall_months
                        ).isoformat(),
                        "mdr_record_count": 0,
                        "death_designated_mdr_count": 0,
                        "reported_events_summarized_by_death_mdrs": 0,
                        "injury_mdr_count": 0,
                        "malfunction_mdr_count": 0,
                        "class_i_recall_family_count": 0,
                        "distinct_product_family_count": 0,
                        "family_exposure_available_count": 0,
                        "family_exposure_waived_count": 0,
                        "family_exposure_missing_count": 0,
                        "fda_event_risk_product_family_adjusted_score": "",
                        "fda_safety_product_family_adjusted_score": "",
                        "fda_product_family_shadow_available_flag": 0,
                        "fda_product_family_shadow_oos_valid_flag": 0,
                        "fda_product_family_adjustment_applied_flag": 0,
                        "fda_product_family_shadow_status": shadow.status,
                        "fda_product_family_shadow_reason": shadow.reason,
                        "qa_pass_count": 0,
                        "qa_warning_count": 0,
                        "qa_failure_count": 0,
                        "review_status": "blocked"
                        if governance_critical_count > 0
                        else "not_effective_asof",
                    }
                )
                continue
            ticker_mdr = mdr_rows(
                conn,
                company_id=company_id,
                ticker=ticker,
                asof=asof,
                window_start=months_before(asof, mdr_months),
                mappings=mappings,
                minimum_family_confidence=minimum_family_confidence,
                minimum_manufacturer_confidence=minimum_manufacturer_confidence,
            )
            ticker_recalls = recall_rows(
                conn,
                company_id=company_id,
                ticker=ticker,
                asof=asof,
                window_start=months_before(asof, recall_months),
                mappings=mappings,
                minimum_family_confidence=minimum_family_confidence,
                minimum_manufacturer_confidence=minimum_manufacturer_confidence,
            )
            ticker_exposures = [
                exposure for exposure in exposures if exposure.ticker == ticker
            ]
            ticker_qa = build_qa_rows(
                asof=asof,
                ticker=ticker,
                mdr=ticker_mdr,
                recalls=ticker_recalls,
                exposures=ticker_exposures,
                governance_critical_count=governance_critical_count,
                minimum_family_confidence=minimum_family_confidence,
                minimum_manufacturer_confidence=minimum_manufacturer_confidence,
                death_coverage_min=float(
                    cfg_get(config, f"{CONFIG_KEY}.death_coverage_min_pct", 100.0)
                ),
                injury_coverage_min=float(
                    cfg_get(config, f"{CONFIG_KEY}.injury_coverage_min_pct", 95.0)
                ),
                malfunction_coverage_min=float(
                    cfg_get(
                        config,
                        f"{CONFIG_KEY}.malfunction_coverage_min_pct",
                        90.0,
                    )
                ),
                class_i_coverage_min=float(
                    cfg_get(
                        config,
                        f"{CONFIG_KEY}.class_i_coverage_min_pct",
                        100.0,
                    )
                ),
            )
            ticker_exceptions = build_exceptions(
                asof=asof,
                ticker=ticker,
                mdr=ticker_mdr,
                recalls=ticker_recalls,
                exposures=ticker_exposures,
            )
            shadow = build_product_family_shadow_score(
                ticker=ticker,
                mdr_rows=ticker_mdr,
                recall_rows=ticker_recalls,
                exposures=ticker_exposures,
                exposure_floor_usd_millions=exposure_floor_usd_millions,
                class_i_recall_severity=class_i_recall_severity,
                recall_severity_rate_weight=recall_severity_rate_weight,
                class_i_recall_count_weight=class_i_recall_count_weight,
                death_rate_weight=death_rate_weight,
                injury_rate_weight=injury_rate_weight,
                malfunction_rate_weight=malfunction_rate_weight,
            )
            statuses = [str(row["status"]) for row in ticker_qa]
            # Fail-loud persistence gate: never commit a scoreable shadow when
            # this ticker has a QA FAIL or the governed CSVs carry CRITICAL
            # issues. The CSV ledgers document the failure; the DB feature row
            # records an explicit blocked state instead of a usable score.
            qa_blocked = (
                "FAIL" in statuses or governance_critical_count > 0
            )
            if qa_blocked:
                failing_checks = sorted(
                    {
                        str(row["check_name"])
                        for row in ticker_qa
                        if str(row["status"]) == "FAIL"
                    }
                )
                persisted_shadow = ProductFamilyShadowScore(
                    event_risk_score=None,
                    safety_score=None,
                    available_flag=0,
                    adjustment_applied_flag=0,
                    exposure_available_count=shadow.exposure_available_count,
                    exposure_waived_count=shadow.exposure_waived_count,
                    exposure_missing_count=shadow.exposure_missing_count,
                    status="blocked_qa_failure",
                    reason=(
                        f"QA failures={','.join(failing_checks) or 'none'}; "
                        "governance_critical_count="
                        f"{governance_critical_count}; shadow persistence "
                        "blocked pending remediation."
                    ),
                    family_details=[],
                )
            else:
                persisted_shadow = shadow
            persisted_oos_valid_flag = persist_shadow_score(
                conn,
                asof=asof,
                company_id=company_id,
                shadow=persisted_shadow,
                shadow_oos_valid_flag=int(
                    bool(persisted_shadow.available_flag)
                    and live_capture_oos_valid == 1
                ),
                production_usage=production_usage,
            )
            source_dates = [
                str(row.get("source_available_date") or "")
                for row in [*ticker_mdr, *ticker_recalls]
                if str(row.get("source_available_date") or "")
            ]
            family_set = {
                normalized_family(row.get("product_family"))
                for row in [*ticker_mdr, *ticker_recalls]
                if normalized_family(row.get("product_family"))
            }
            available_exposure_families = {
                exposure.product_family
                for exposure in ticker_exposures
                if exposure.exposure_status == EXPOSURE_STATUS_AVAILABLE
                and exposure.exposure_value is not None
                and exposure.exposure_value > 0
            }
            waived_exposure_families = {
                exposure.product_family
                for exposure in ticker_exposures
                if exposure.exposure_status == EXPOSURE_STATUS_WAIVED
            }
            governed_exposure_families = (
                available_exposure_families | waived_exposure_families
            )
            summaries.append(
                {
                    "asof_date": asof.isoformat(),
                    "ticker": ticker,
                    "company_id": company_id,
                    "source_data_through": max(source_dates) if source_dates else "",
                    "mdr_window_start": months_before(asof, mdr_months).isoformat(),
                    "recall_window_start": months_before(
                        asof, recall_months
                    ).isoformat(),
                    "mdr_record_count": len(ticker_mdr),
                    "death_designated_mdr_count": sum(
                        as_int(row["death_designated_flag"]) for row in ticker_mdr
                    ),
                    "reported_events_summarized_by_death_mdrs": sum(
                        as_int(row["number_events_summarized"], 1)
                        for row in ticker_mdr
                        if as_int(row["death_designated_flag"])
                    ),
                    "injury_mdr_count": sum(
                        as_int(row["injury_flag"]) for row in ticker_mdr
                    ),
                    "malfunction_mdr_count": sum(
                        as_int(row["malfunction_flag"]) for row in ticker_mdr
                    ),
                    "class_i_recall_family_count": len(ticker_recalls),
                    "distinct_product_family_count": len(family_set),
                    "family_exposure_available_count": len(
                        family_set.intersection(available_exposure_families)
                    ),
                    "family_exposure_waived_count": len(
                        family_set.intersection(waived_exposure_families)
                    ),
                    "family_exposure_missing_count": len(
                        family_set - governed_exposure_families
                    ),
                    "fda_event_risk_product_family_adjusted_score": (
                        persisted_shadow.event_risk_score
                    ),
                    "fda_safety_product_family_adjusted_score": (
                        persisted_shadow.safety_score
                    ),
                    "fda_product_family_shadow_available_flag": (
                        persisted_shadow.available_flag
                    ),
                    "fda_product_family_shadow_oos_valid_flag": (
                        persisted_oos_valid_flag
                    ),
                    "fda_product_family_adjustment_applied_flag": (
                        persisted_shadow.adjustment_applied_flag
                    ),
                    "fda_product_family_shadow_status": persisted_shadow.status,
                    "fda_product_family_shadow_reason": persisted_shadow.reason,
                    "qa_pass_count": statuses.count("PASS"),
                    "qa_warning_count": statuses.count("WARNING"),
                    "qa_failure_count": statuses.count("FAIL"),
                    # A waiver label is only truthful for an available,
                    # warning-free shadow; blocked/pending states win first.
                    "review_status": "blocked"
                    if qa_blocked
                    else "mapping_complete_exposure_pending"
                    if "WARNING" in statuses or not persisted_shadow.available_flag
                    else "review_ready_with_waiver"
                    if persisted_shadow.exposure_waived_count
                    else "review_ready",
                }
            )
            all_mdr.extend(ticker_mdr)
            all_recalls.extend(ticker_recalls)
            all_qa.extend(ticker_qa)
            all_exceptions.extend(ticker_exceptions)
    output_dir.mkdir(parents=True, exist_ok=True)
    write_csv(output_dir / "med_device_fda_mdr_review.csv", all_mdr, MDR_FIELDS)
    write_csv(
        output_dir / "med_device_fda_class_i_recall_review.csv",
        all_recalls,
        RECALL_FIELDS,
    )
    write_csv(
        output_dir / "med_device_fda_product_family_qa.csv",
        all_qa,
        QA_FIELDS,
    )
    write_csv(
        output_dir / "med_device_fda_product_family_exceptions.csv",
        all_exceptions,
        EXCEPTION_FIELDS,
    )
    write_csv(
        output_dir / "med_device_fda_product_family_review_summary.csv",
        summaries,
        SUMMARY_FIELDS,
    )
    write_csv(
        output_dir / "med_device_fda_product_family_governance_issues.csv",
        [issue.as_dict() for issue in governance_issues],
        ["severity", "source", "row_number", "issue_type", "detail"],
    )
    failure_count = sum(row["status"] == "FAIL" for row in all_qa)
    print(
        "fda_product_family_review "
        f"asof={asof.isoformat()} tickers={len(tickers)} mdr_rows={len(all_mdr)} "
        f"class_i_families={len(all_recalls)} exceptions={len(all_exceptions)} "
        f"qa_failures={failure_count} governance_critical={governance_critical_count} "
        f"output={output_dir}"
    )
    if failure_count and not args.warn_only:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
