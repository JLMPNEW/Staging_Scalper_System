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
    seen: dict[tuple[str, str, str], int] = {}
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
            "available" if value is not None and value > 0 else "unavailable"
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
        if status == "available" and (value is None or value <= 0):
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
        key = (ticker, family, metric)
        previous_row = seen.get(key)
        if previous_row is not None:
            issues.append(
                GovernanceIssue(
                    "CRITICAL",
                    context,
                    row_number,
                    "duplicate_effective_exposure",
                    f"key={key}; previous_row={previous_row}",
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
                exposure_unit=normalized_text(row.get("exposure_unit")),
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
