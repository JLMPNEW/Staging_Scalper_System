"""Governed FDA product-family mappings and deterministic review helpers."""
from __future__ import annotations

import csv
import json
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from med_devices.core.point_in_time import (
    parse_iso_date,
    pit_date_parse_errors,
    row_is_effective_asof,
    validate_pit_invariants,
)


MAPPING_REQUIRED_COLUMNS = {
    "ticker",
    "product_code",
    "product_family",
    "mapping_confidence",
    "mapping_method",
    "source_reference",
    "valid_from",
    "reviewed_at",
    "active",
}
EXPOSURE_REQUIRED_COLUMNS = {
    "ticker",
    "product_family",
    "exposure_metric",
    "exposure_value",
    "exposure_unit",
    "exposure_scope",
    "exposure_confidence",
    "source_reference",
    "source_asof_date",
    "valid_from",
    "reviewed_at",
    "active",
}
TRUE_VALUES = {"1", "true", "t", "yes", "y", "active"}
DEATH_OUTCOMES = {"d", "death", "deceased", "fatal", "fatality"}
EXPOSURE_STATUS_AVAILABLE = "available"
EXPOSURE_STATUS_UNAVAILABLE = "unavailable"
EXPOSURE_STATUS_WAIVED = "waived_no_specific_exposure"
EXPOSURE_STATUSES = {
    EXPOSURE_STATUS_AVAILABLE,
    EXPOSURE_STATUS_UNAVAILABLE,
    EXPOSURE_STATUS_WAIVED,
}
# The shadow-score denominator math divides exposure_value by 1_000 to obtain
# USD billions, so an available exposure row is only valid when its unit is a
# governed USD-millions unit. Any other unit must fail loud at load time.
ALLOWED_AVAILABLE_EXPOSURE_UNITS = {"USD_millions"}


def normalized_text(raw: object) -> str:
    return re.sub(r"\s+", " ", str(raw or "").strip())


def normalized_ticker(raw: object) -> str:
    return normalized_text(raw).upper()


def normalized_product_code(raw: object) -> str:
    return re.sub(r"[^A-Z0-9]", "", normalized_text(raw).upper())


def normalized_family(raw: object) -> str:
    return re.sub(r"[^a-z0-9]+", "_", normalized_text(raw).lower()).strip("_")


def as_bool(raw: object) -> bool:
    return normalized_text(raw).lower() in TRUE_VALUES


def as_float(raw: object) -> float | None:
    text = normalized_text(raw)
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def as_int(raw: object, default: int = 0) -> int:
    value = as_float(raw)
    return int(value) if value is not None else default


def json_dict(raw: object) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    try:
        value = json.loads(str(raw or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def json_list(raw: object) -> list[Any]:
    return raw if isinstance(raw, list) else []


def csv_rows(path: Path, *, required_columns: set[str]) -> list[dict[str, str]]:
    if not path.exists():
        raise FileNotFoundError(f"Governed FDA review CSV not found: {path}")
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        fieldnames = {str(name or "").strip() for name in reader.fieldnames or []}
        missing = sorted(required_columns - fieldnames)
        if missing:
            raise ValueError(f"{path} is missing required columns: {', '.join(missing)}")
        return [
            {str(key or "").strip(): normalized_text(value) for key, value in row.items()}
            for row in reader
        ]


@dataclass(frozen=True)
class ProductFamilyMapping:
    ticker: str
    product_code: str
    manufacturer_id: int | None
    product_family: str
    mapping_confidence: float
    mapping_method: str
    source_reference: str
    valid_from: str
    valid_to: str
    reviewed_at: str
    notes: str

    @property
    def key(self) -> tuple[str, str, int | None]:
        return self.ticker, self.product_code, self.manufacturer_id


@dataclass(frozen=True)
class ProductFamilyExposure:
    ticker: str
    product_family: str
    exposure_metric: str
    exposure_value: float | None
    exposure_unit: str
    exposure_scope: str
    exposure_confidence: float
    exposure_status: str
    source_reference: str
    source_asof_date: str
    valid_from: str
    valid_to: str
    reviewed_at: str
    notes: str


@dataclass(frozen=True)
class GovernanceIssue:
    severity: str
    source: str
    row_number: int
    issue_type: str
    detail: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "severity": self.severity,
            "source": self.source,
            "row_number": self.row_number,
            "issue_type": self.issue_type,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class ProductFamilyShadowScore:
    event_risk_score: float | None
    safety_score: float | None
    available_flag: int
    adjustment_applied_flag: int
    exposure_available_count: int
    exposure_waived_count: int
    exposure_missing_count: int
    status: str
    reason: str
    family_details: list[dict[str, Any]]


def load_product_family_mappings(
    path: Path,
    *,
    asof: date,
) -> tuple[list[ProductFamilyMapping], list[GovernanceIssue]]:
    rows = csv_rows(path, required_columns=MAPPING_REQUIRED_COLUMNS)
    mappings: list[ProductFamilyMapping] = []
    issues: list[GovernanceIssue] = []
    seen: dict[tuple[str, str, int | None], int] = {}
    for row_number, row in enumerate(rows, start=2):
        context = str(path)
        parse_errors = pit_date_parse_errors(row)
        for column in parse_errors:
            issues.append(
                GovernanceIssue("CRITICAL", context, row_number, "invalid_pit_date", column)
            )
        for detail in validate_pit_invariants(row, require_reviewed_at=True):
            issues.append(
                GovernanceIssue("CRITICAL", context, row_number, "pit_invariant", detail)
            )
        if not as_bool(row.get("active")) or not row_is_effective_asof(row, asof):
            continue
        ticker = normalized_ticker(row.get("ticker"))
        product_code = normalized_product_code(row.get("product_code"))
        family = normalized_family(row.get("product_family"))
        confidence = as_float(row.get("mapping_confidence"))
        manufacturer_id_text = normalized_text(row.get("fda_manufacturer_id"))
        manufacturer_id = as_int(manufacturer_id_text) if manufacturer_id_text else None
        if not ticker or not product_code or not family or confidence is None:
            issues.append(
                GovernanceIssue(
                    "CRITICAL",
                    context,
                    row_number,
                    "invalid_mapping_row",
                    "ticker, product_code, product_family, and numeric mapping_confidence are required",
                )
            )
            continue
        if not 0.0 <= confidence <= 100.0:
            issues.append(
                GovernanceIssue(
                    "CRITICAL",
                    context,
                    row_number,
                    "mapping_confidence_out_of_range",
                    f"mapping_confidence={confidence}",
                )
            )
            continue
        mapping = ProductFamilyMapping(
            ticker=ticker,
            product_code=product_code,
            manufacturer_id=manufacturer_id,
            product_family=family,
            mapping_confidence=confidence,
            mapping_method=normalized_text(row.get("mapping_method")),
            source_reference=normalized_text(row.get("source_reference")),
            valid_from=normalized_text(row.get("valid_from")),
            valid_to=normalized_text(row.get("valid_to")),
            reviewed_at=normalized_text(row.get("reviewed_at")),
            notes=normalized_text(row.get("notes")),
        )
        previous_row = seen.get(mapping.key)
        if previous_row is not None:
            issues.append(
                GovernanceIssue(
                    "CRITICAL",
                    context,
                    row_number,
                    "duplicate_effective_mapping",
                    f"key={mapping.key}; previous_row={previous_row}",
                )
            )
            continue
        seen[mapping.key] = row_number
        mappings.append(mapping)
    return mappings, issues


def load_product_family_exposures(
    path: Path,
    *,
    asof: date,
) -> tuple[list[ProductFamilyExposure], list[GovernanceIssue]]:
    rows = csv_rows(path, required_columns=EXPOSURE_REQUIRED_COLUMNS)
    exposures: list[ProductFamilyExposure] = []
    issues: list[GovernanceIssue] = []
    seen: dict[tuple[str, str], int] = {}
    for row_number, row in enumerate(rows, start=2):
        context = str(path)
        parse_errors = pit_date_parse_errors(row)
        for column in parse_errors:
            issues.append(
                GovernanceIssue("CRITICAL", context, row_number, "invalid_pit_date", column)
            )
        for detail in validate_pit_invariants(row, require_reviewed_at=True):
            issues.append(
                GovernanceIssue("CRITICAL", context, row_number, "pit_invariant", detail)
            )
        if not as_bool(row.get("active")) or not row_is_effective_asof(row, asof):
            continue
        ticker = normalized_ticker(row.get("ticker"))
        family = normalized_family(row.get("product_family"))
        metric = normalized_family(row.get("exposure_metric"))
        value = as_float(row.get("exposure_value"))
        confidence = as_float(row.get("exposure_confidence"))
        status = normalized_family(row.get("exposure_status")) or (
            EXPOSURE_STATUS_AVAILABLE
            if value is not None and value > 0
            else EXPOSURE_STATUS_UNAVAILABLE
        )
        if not ticker or not family or not metric or confidence is None:
            issues.append(
                GovernanceIssue(
                    "CRITICAL",
                    context,
                    row_number,
                    "invalid_exposure_row",
                    (
                        "ticker, product_family, exposure_metric, and numeric "
                        "exposure_confidence are required"
                    ),
                )
            )
            continue
        if not 0.0 <= confidence <= 100.0:
            issues.append(
                GovernanceIssue(
                    "CRITICAL",
                    context,
                    row_number,
                    "exposure_confidence_out_of_range",
                    f"exposure_confidence={confidence}",
                )
            )
            continue
        if status not in EXPOSURE_STATUSES:
            issues.append(
                GovernanceIssue(
                    "CRITICAL",
                    context,
                    row_number,
                    "invalid_exposure_status",
                    f"exposure_status={status!r}; allowed={sorted(EXPOSURE_STATUSES)}",
                )
            )
            continue
        if status == EXPOSURE_STATUS_AVAILABLE and (value is None or value <= 0):
            issues.append(
                GovernanceIssue(
                    "CRITICAL",
                    context,
                    row_number,
                    "invalid_available_exposure",
                    "available exposure requires exposure_value > 0",
                )
            )
            continue
        exposure_unit = normalized_text(row.get("exposure_unit"))
        if (
            status == EXPOSURE_STATUS_AVAILABLE
            and exposure_unit not in ALLOWED_AVAILABLE_EXPOSURE_UNITS
        ):
            issues.append(
                GovernanceIssue(
                    "CRITICAL",
                    context,
                    row_number,
                    "invalid_exposure_unit",
                    (
                        f"exposure_unit={exposure_unit!r}; available exposure "
                        "requires a governed unit in "
                        f"{sorted(ALLOWED_AVAILABLE_EXPOSURE_UNITS)} because the "
                        "shadow-score denominator assumes USD millions"
                    ),
                )
            )
            continue
        if status == EXPOSURE_STATUS_WAIVED:
            if value is not None and value > 0:
                issues.append(
                    GovernanceIssue(
                        "CRITICAL",
                        context,
                        row_number,
                        "waived_exposure_has_value",
                        "waived exposure must not carry a numeric denominator",
                    )
                )
                continue
            if not normalized_text(row.get("source_reference")) or not normalized_text(
                row.get("notes")
            ):
                issues.append(
                    GovernanceIssue(
                        "CRITICAL",
                        context,
                        row_number,
                        "undocumented_exposure_waiver",
                        "waived exposure requires source_reference and notes",
                    )
                )
                continue
        key = (ticker, family)
        previous_row = seen.get(key)
        if previous_row is not None:
            issues.append(
                GovernanceIssue(
                    "CRITICAL",
                    context,
                    row_number,
                    "duplicate_effective_exposure",
                    (
                        f"key={key}; exposure_metric={metric}; "
                        f"previous_row={previous_row}; at most one effective "
                        "exposure row is allowed per (ticker, product_family) "
                        "regardless of exposure_metric"
                    ),
                )
            )
            continue
        seen[key] = row_number
        exposures.append(
            ProductFamilyExposure(
                ticker=ticker,
                product_family=family,
                exposure_metric=metric,
                exposure_value=value,
                exposure_unit=exposure_unit,
                exposure_scope=normalized_family(row.get("exposure_scope")),
                exposure_confidence=confidence,
                exposure_status=status,
                source_reference=normalized_text(row.get("source_reference")),
                source_asof_date=normalized_text(row.get("source_asof_date")),
                valid_from=normalized_text(row.get("valid_from")),
                valid_to=normalized_text(row.get("valid_to")),
                reviewed_at=normalized_text(row.get("reviewed_at")),
                notes=normalized_text(row.get("notes")),
            )
        )
    return exposures, issues


def build_product_family_shadow_score(
    *,
    ticker: str,
    mdr_rows: list[dict[str, Any]],
    recall_rows: list[dict[str, Any]],
    exposures: list[ProductFamilyExposure],
    exposure_floor_usd_millions: float,
    class_i_recall_severity: float,
    recall_severity_rate_weight: float,
    class_i_recall_count_weight: float,
    death_rate_weight: float,
    injury_rate_weight: float,
    malfunction_rate_weight: float,
) -> ProductFamilyShadowScore:
    """Build a high-is-worse product-family shadow risk score.

    Sourced product-family exposure is used instead of total-company revenue,
    and a sourced denominator is used verbatim (never floored upward, which
    would understate small-family event rates). A formally waived family
    receives only the configured conservative floor; no unrelated business-line
    exposure is imputed. Class I recall counts are penalized directly and are
    never diversified away. More than one effective exposure row for a mapped
    event family blocks scoring instead of resolving by row order.
    """

    normalized_ticker_value = normalized_ticker(ticker)
    family_rows: dict[str, dict[str, int]] = {}

    def counters(family: str) -> dict[str, int]:
        return family_rows.setdefault(
            family,
            {
                "death_count": 0,
                "injury_count": 0,
                "malfunction_count": 0,
                "class_i_recall_count": 0,
            },
        )

    for row in mdr_rows:
        family = normalized_family(row.get("product_family"))
        if not family:
            continue
        values = counters(family)
        values["death_count"] += max(0, as_int(row.get("death_designated_flag")))
        values["injury_count"] += max(0, as_int(row.get("injury_flag")))
        values["malfunction_count"] += max(0, as_int(row.get("malfunction_flag")))
    for row in recall_rows:
        family = normalized_family(row.get("product_family"))
        if family:
            counters(family)["class_i_recall_count"] += 1

    if not family_rows:
        return ProductFamilyShadowScore(
            event_risk_score=None,
            safety_score=None,
            available_flag=0,
            adjustment_applied_flag=0,
            exposure_available_count=0,
            exposure_waived_count=0,
            exposure_missing_count=0,
            status="not_available_no_mapped_events",
            reason="No mapped product-family FDA events were available as of the review date.",
            family_details=[],
        )

    exposure_by_family: dict[str, ProductFamilyExposure] = {}
    ambiguous_metrics: dict[str, list[str]] = {}
    for exposure in exposures:
        if exposure.ticker != normalized_ticker_value:
            continue
        existing = exposure_by_family.get(exposure.product_family)
        if existing is not None:
            ambiguous_metrics.setdefault(
                exposure.product_family, [existing.exposure_metric]
            ).append(exposure.exposure_metric)
            continue
        exposure_by_family[exposure.product_family] = exposure
    ambiguous_families = sorted(set(ambiguous_metrics) & set(family_rows))
    if ambiguous_families:
        detail = ";".join(
            f"{family}({','.join(sorted(set(ambiguous_metrics[family])))})"
            for family in ambiguous_families
        )
        return ProductFamilyShadowScore(
            event_risk_score=None,
            safety_score=None,
            available_flag=0,
            adjustment_applied_flag=0,
            exposure_available_count=0,
            exposure_waived_count=0,
            exposure_missing_count=0,
            status="blocked_ambiguous_exposure",
            reason=(
                "Multiple effective exposure rows exist for: "
                f"{detail}; scoring refuses to resolve the conflict by row order."
            ),
            family_details=[],
        )
    missing = sorted(set(family_rows) - set(exposure_by_family))
    available_count = 0
    for family in family_rows:
        exposure = exposure_by_family.get(family)
        if (
            exposure is not None
            and exposure.exposure_status == EXPOSURE_STATUS_AVAILABLE
            and exposure.exposure_value is not None
            and exposure.exposure_value > 0
        ):
            available_count += 1
    waived_count = sum(
        exposure_by_family[family].exposure_status == EXPOSURE_STATUS_WAIVED
        for family in family_rows
        if family in exposure_by_family
    )
    unavailable = sorted(
        family
        for family in family_rows
        if family in exposure_by_family
        and exposure_by_family[family].exposure_status == EXPOSURE_STATUS_UNAVAILABLE
    )
    missing.extend(unavailable)
    missing = sorted(set(missing))
    if missing:
        return ProductFamilyShadowScore(
            event_risk_score=None,
            safety_score=None,
            available_flag=0,
            adjustment_applied_flag=0,
            exposure_available_count=available_count,
            exposure_waived_count=waived_count,
            exposure_missing_count=len(missing),
            status="blocked_missing_exposure",
            reason="Missing governed exposure treatment for: " + ",".join(missing),
            family_details=[],
        )

    floor_value = max(1.0, float(exposure_floor_usd_millions))
    family_details: list[dict[str, Any]] = []
    total_penalty = 0.0
    for family in sorted(family_rows):
        counts = family_rows[family]
        exposure = exposure_by_family[family]
        if exposure.exposure_status == EXPOSURE_STATUS_AVAILABLE:
            assert exposure.exposure_value is not None
            # Sourced denominators are used verbatim: flooring a small sourced
            # family would dampen its per-billion event rates below the sourced
            # truth, which is anti-conservative for a high-is-worse score.
            denominator_millions = exposure.exposure_value
            denominator_source = "sourced_product_family_exposure"
        else:
            denominator_millions = floor_value
            denominator_source = "governed_waiver_conservative_floor"
        denominator_billions = denominator_millions / 1_000.0
        class_i_count = counts["class_i_recall_count"]
        penalty = (
            (class_i_count * class_i_recall_severity / denominator_billions)
            * recall_severity_rate_weight
            + class_i_count * class_i_recall_count_weight
            + (counts["death_count"] / denominator_billions) * death_rate_weight
            + (counts["injury_count"] / denominator_billions) * injury_rate_weight
            + (counts["malfunction_count"] / denominator_billions)
            * malfunction_rate_weight
        )
        total_penalty += penalty
        family_details.append(
            {
                "product_family": family,
                "exposure_status": exposure.exposure_status,
                "denominator_usd_millions": round(denominator_millions, 4),
                "denominator_source": denominator_source,
                **counts,
                "risk_penalty": round(penalty, 6),
            }
        )

    event_risk = round(max(0.0, min(100.0, total_penalty)), 2)
    status = "ready_with_waiver" if waived_count else "ready"
    reason = (
        "All mapped families have sourced exposure denominators."
        if not waived_count
        else (
            f"{waived_count} family exposure denominator waived; the conservative "
            "floor is used without imputing another business line."
        )
    )
    return ProductFamilyShadowScore(
        event_risk_score=event_risk,
        safety_score=round(100.0 - event_risk, 2),
        available_flag=1,
        adjustment_applied_flag=1,
        exposure_available_count=available_count,
        exposure_waived_count=waived_count,
        exposure_missing_count=0,
        status=status,
        reason=reason,
        family_details=family_details,
    )


def mapping_for(
    mappings: list[ProductFamilyMapping],
    *,
    ticker: str,
    product_code: object,
    manufacturer_id: object,
) -> ProductFamilyMapping | None:
    normalized_ticker_value = normalized_ticker(ticker)
    normalized_code = normalized_product_code(product_code)
    manufacturer_value = as_int(manufacturer_id) if normalized_text(manufacturer_id) else None
    exact: ProductFamilyMapping | None = None
    generic: ProductFamilyMapping | None = None
    for mapping in mappings:
        if mapping.ticker != normalized_ticker_value or mapping.product_code != normalized_code:
            continue
        if mapping.manufacturer_id is None:
            generic = mapping
        elif manufacturer_value == mapping.manufacturer_id:
            exact = mapping
    return exact or generic


def structured_mdr_metadata(payload_json: object) -> dict[str, Any]:
    payload = json_dict(payload_json)
    raw_type = payload.get("type_of_report")
    report_types = raw_type if isinstance(raw_type, list) else [raw_type]
    report_type = ";".join(
        normalized_text(value) for value in report_types if normalized_text(value)
    )
    supplement_dates = json_list(payload.get("suppl_dates_fda_received"))
    patients = json_list(payload.get("patient"))
    outcomes: set[str] = set()
    for raw_patient in patients:
        patient = raw_patient if isinstance(raw_patient, dict) else {}
        for key in ("sequence_number_outcome", "patient_outcome", "outcome"):
            raw_outcomes = patient.get(key)
            values = raw_outcomes if isinstance(raw_outcomes, list) else [raw_outcomes]
            for raw_value in values:
                value = re.sub(
                    r"^\s*\d+\s*[.)-]?\s*",
                    "",
                    normalized_text(raw_value).lower(),
                )
                if value:
                    outcomes.add(value)
    summary_flag = normalized_text(payload.get("summary_report_flag")).upper() == "Y"
    return {
        "mdr_report_key": normalized_text(payload.get("mdr_report_key")),
        "report_number": normalized_text(payload.get("report_number")),
        "event_key": normalized_text(payload.get("event_key")),
        "summary_report_flag": int(summary_flag),
        "number_events_summarized": max(1, as_int(payload.get("noe_summarized"), 1)),
        "report_submission_type": report_type,
        "supplement_count": len(supplement_dates),
        "structured_death_outcome_flag": int(bool(outcomes.intersection(DEATH_OUTCOMES))),
        "exemption_number": normalized_text(payload.get("exemption_number")),
        "initial_report_to_fda": normalized_text(payload.get("initial_report_to_fda")),
    }


def mapping_coverage(
    rows: list[dict[str, Any]],
    *,
    count_field: str,
    minimum_mapping_confidence: float,
) -> tuple[int, int, float]:
    total = sum(max(0, as_int(row.get(count_field))) for row in rows)
    covered = sum(
        max(0, as_int(row.get(count_field)))
        for row in rows
        if normalized_text(row.get("product_family"))
        and (as_float(row.get("family_mapping_confidence")) or 0.0)
        >= minimum_mapping_confidence
    )
    percentage = 100.0 if total == 0 else 100.0 * covered / total
    return total, covered, round(percentage, 4)


def earliest_date(*values: object) -> str:
    parsed = [
        parsed_value
        for value in values
        if (parsed_value := parse_iso_date(value)) is not None
    ]
    return min(parsed).isoformat() if parsed else ""
