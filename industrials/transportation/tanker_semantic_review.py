from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from datetime import date
from typing import Mapping


REVIEW_POLICY_VERSION = "transportation_tanker_semantic_review_v4"

_NUMBER = re.compile(
    r"(?P<currency>US\$|USD|\$)?\s*(?P<open>\()?\s*"
    r"(?P<value>[-+]?\d[\d,]*(?:\.\d+)?)\s*"
    r"(?P<percent>%|percent)?\s*(?P<close>\))?",
    re.IGNORECASE,
)

_EXPECTED_UNITS = {
    "fleet_capacity": "segment_native_capacity",
    "revenue_days": "days",
    "offhire_or_drydock_ratio": "ratio",
    "tce_day_rate": "currency_per_day",
    "fleet_age": "years",
    "vessel_count": "count",
    "newbuild_capacity_commitments": "count_and_segment_native_capacity",
    "capex_commitments": "currency",
    "vessel_opex_per_day": "currency_per_day",
    "spot_or_charter_day_rate": "currency_per_day",
    "fleet_utilization": "ratio",
    "charter_coverage_next_12m": "ratio",
    "contracted_revenue_backlog": "currency",
    "weighted_average_charter_term": "years",
    "cash_breakeven_per_day": "currency_per_day",
    "spot_exposure_ratio": "ratio",
}


@dataclass(frozen=True)
class CandidateReview:
    approved: bool
    reason: str
    reviewed_value: float | None


def normalized(value: object) -> str:
    return " ".join(str(value or "").lower().split())


def definition_signature(row: Mapping[str, object]) -> tuple[str, ...]:
    provenance = _json(row.get("provenance_json"))
    return (
        str(row.get("source_lane") or ""),
        str(row.get("ticker") or ""),
        str(row.get("metric_id") or ""),
        normalized(row.get("concept_name")),
        normalized(row.get("unit")),
        normalized(row.get("extraction_method")),
        normalized(row.get("status_reason") or row.get("reason")),
        normalized(row.get("formula") or provenance.get("formula")),
        normalized(row.get("numerator_concept")),
        normalized(row.get("denominator_concept")),
        normalized(provenance.get("definition_basis")),
        normalized(provenance.get("weighting_basis")),
        normalized(provenance.get("denominator_basis")),
        normalized(provenance.get("coverage_start_date")),
        normalized(provenance.get("coverage_end_date")),
    )


def definition_id(row: Mapping[str, object]) -> str:
    return hashlib.sha256("\x1f".join(definition_signature(row)).encode("utf-8")).hexdigest()


def candidate_key(row: Mapping[str, object]) -> str:
    key = str(row.get("evidence_key") or "")
    if key:
        return key
    values = (
        str(row.get("source_lane") or ""),
        str(row.get("ticker") or ""),
        str(row.get("metric_id") or ""),
        str(row.get("accession_number") or ""),
        str(row.get("period_start") or ""),
        str(row.get("period_end") or ""),
        str(row.get("candidate_value") if row.get("candidate_value") is not None else row.get("value") or ""),
        str(row.get("source_document") or ""),
    )
    return hashlib.sha256("\x1f".join(values).encode("utf-8")).hexdigest()


def _json(value: object) -> dict[str, object]:
    try:
        payload = json.loads(str(value or "{}"))
    except (TypeError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _value(row: Mapping[str, object]) -> float | None:
    raw = row.get("candidate_value")
    if raw is None or raw == "":
        raw = row.get("value")
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def _unit_ok(metric: str, unit: str, provenance: Mapping[str, object]) -> bool:
    expected = _EXPECTED_UNITS.get(metric, "")
    actual = unit.lower().strip()
    declared = str(provenance.get("unit_contract") or "").lower().strip()
    if declared == expected:
        return True
    if actual == expected:
        return True
    if expected == "currency" and re.fullmatch(r"[a-z]{3}", actual):
        return True
    if expected == "currency_per_day" and re.fullmatch(r"[a-z]{3}_currency_per_day", actual):
        return True
    if expected == "count_and_segment_native_capacity" and actual in {"count", "segment_native_capacity"}:
        return True
    return False


def _derived_review(row: Mapping[str, object], value: float) -> CandidateReview | None:
    concept = str(row.get("concept_name") or "")
    provenance = _json(row.get("provenance_json"))
    metric = str(row.get("metric_id") or "")
    try:
        if concept == "DerivedDwtWeightedFleetAge":
            valid = (
                metric == "fleet_age"
                and 0 <= value <= 60
                and int(provenance["operand_count"]) >= 2
                and float(provenance["total_capacity"]) > 0
                and provenance.get("weighting_basis") == "DWT_or_table_capacity"
            )
        elif concept == "DerivedSimpleAverageFleetAge":
            valid = (
                metric == "fleet_age"
                and 0 <= value <= 60
                and int(provenance["operand_count"]) >= 2
                and provenance.get("weighting_basis") == "simple_average_year_built"
            )
        elif concept == "DerivedVesselCountFromSchedule":
            valid = (
                metric == "vessel_count"
                and int(provenance["operand_count"]) >= 2
                and provenance.get("identity_basis") == "normalized_unique_vessel_name"
                and math.isclose(value, float(provenance["operand_count"]))
            )
        elif concept == "DerivedFleetCapacityFromVesselSchedule":
            valid = (
                metric == "fleet_capacity"
                and int(provenance["operand_count"]) >= 2
                and provenance.get("capacity_basis") == "DWT"
                and float(provenance["total_capacity"]) > 0
                and math.isclose(value, float(provenance["total_capacity"]))
            )
        elif concept == "DerivedRevenueDaysFromAvailableLessOffhire":
            available = float(provenance["available_days"])
            offhire = float(provenance["offhire_days"])
            valid = metric == "revenue_days" and 0 <= offhire <= available and math.isclose(value, available - offhire)
        elif concept == "DerivedOffhireDaysToAvailableDays":
            available = float(provenance["available_days"])
            offhire = float(provenance["offhire_days"])
            valid = (
                metric == "offhire_or_drydock_ratio"
                and available > 0
                and 0 <= offhire <= available
                and 0 <= value <= 0.25
                and math.isclose(value, offhire / available)
            )
        elif concept == "DerivedFixedDaysCoverageRatio":
            fixed = float(provenance["fixed_days"])
            denominator = float(provenance["denominator_days"])
            valid = metric == "charter_coverage_next_12m" and denominator > 0 and 0 <= fixed <= denominator and math.isclose(value, fixed / denominator)
        elif concept == "DerivedFleetUtilizationFromDays":
            revenue = float(provenance["revenue_days"])
            available = float(provenance["available_days"])
            valid = metric == "fleet_utilization" and available > 0 and 0 <= revenue <= available and math.isclose(value, revenue / available)
        elif concept == "DerivedVesselOpexPerOperatingDay":
            expense = float(provenance["vessel_operating_expense"])
            operating = float(provenance["operating_days"])
            valid = metric == "vessel_opex_per_day" and operating > 0 and expense >= 0 and provenance.get("denominator_basis") == "operating_days" and math.isclose(value, expense / operating)
        elif concept == "DerivedForwardCharterCoverageFromVesselSchedule":
            contracted = float(provenance["contracted_days"])
            available = float(provenance["available_days"])
            vessel_count = int(provenance["vessel_count"])
            fixed_vessel_count = int(provenance["fixed_vessel_count"])
            coverage_start = date.fromisoformat(str(provenance["coverage_start_date"]))
            coverage_end = date.fromisoformat(str(provenance["coverage_end_date"]))
            valid = (
                metric == "charter_coverage_next_12m"
                and (coverage_end - coverage_start).days == 365
                and available == vessel_count * 365
                and available > 0 and 0 <= contracted <= available
                and vessel_count >= fixed_vessel_count >= 1
                and provenance.get("denominator_basis") == "all_schedule_vessels_x_365"
                and math.isclose(value, contracted / available)
            )
        else:
            return None
    except (KeyError, TypeError, ValueError):
        valid = False
    return CandidateReview(valid, "auditable_tanker_derivation_pass" if valid else "tanker_derivation_contract_failed", value)


def _near_value(text: str, label: str, value: float, *, width: int = 180) -> str:
    for match in re.finditer(label, text, re.I):
        snippet = text[max(0, match.start() - 80) : match.end() + width]
        for number in _NUMBER.finditer(snippet):
            try:
                parsed = float(number.group("value").replace(",", ""))
            except ValueError:
                continue
            candidates = {abs(parsed), abs(parsed / 100.0)} if number.group("percent") else {abs(parsed)}
            if any(math.isclose(abs(value), candidate, rel_tol=1e-8, abs_tol=1e-8) for candidate in candidates):
                return snippet
    return ""


def review_candidate(row: Mapping[str, object]) -> CandidateReview:
    metric = str(row.get("metric_id") or "")
    value = _value(row)
    text = " ".join(str(row.get("evidence_text") or "").split())
    provenance = _json(row.get("provenance_json"))
    if metric not in _EXPECTED_UNITS or value is None or not text:
        return CandidateReview(False, "missing_or_unsupported_tanker_candidate", value)
    if not _unit_ok(metric, str(row.get("unit") or ""), provenance):
        return CandidateReview(False, "tanker_unit_contract_failed", value)
    if re.search(r"\b(?:peer|competitor|pro\s+forma|industry average)\b", text, re.I):
        return CandidateReview(False, "nonissuer_or_proforma_context", value)
    derived = _derived_review(row, value)
    if derived is not None:
        return derived

    change_context = bool(re.search(r"\b(?:increase|decrease|decline|change|improve)(?:d|s)?\b", text, re.I))
    currency_context = bool(re.search(r"US\$|USD|\$", text, re.I))
    percent_context = bool(re.search(r"%|\bpercent(?:age)?\b", text, re.I))
    filing_year_value = 1900 <= value <= 2100 and value.is_integer()
    patterns = {
        "fleet_capacity": r"(?:total|aggregate)\s+(?:fleet\s+)?(?:carrying\s+)?capacity|total\s+(?:fleet\s+)?deadweight\s+tonnage|fleet\s+(?:capacity|dwt)",
        "revenue_days": r"(?:total\s+)?(?:revenue|earning|net\s+earnings|earnings\s+capacity|available\s+earning)\s+days",
        "offhire_or_drydock_ratio": r"off[- ]?hire|dry[- ]?dock",
        "tce_day_rate": r"time\s+charter\s+equivalent|\btce(?:\s+rate|\s+per\s+day)?\b",
        "fleet_age": r"(?:weighted\s+)?average\s+(?:fleet|vessel)\s+age",
        "vessel_count": r"fleet\s+size|(?:average\s+)?number\s+of\s+vessels|total\s+operating\s+fleet|owned\s+and\s+operated\s+fleet|owned\s+fleet",
        "newbuild_capacity_commitments": r"newbuild(?:ing)?\s+(?:contracts?|commitments?)|vessel\s+purchase\s+commitments?",
        "capex_commitments": r"(?:contracted|contractual|capital\s+expenditure)\s+(?:capital\s+)?commitments?|remaining\s+committed\s+payments",
        "vessel_opex_per_day": r"vessel\s+operating\s+(?:expenses?|costs?)\s+per\s+day|daily\s+vessel\s+operating\s+expenses?|opex\s+per\s+day",
        "spot_or_charter_day_rate": r"(?:spot(?:\s+market)?|time\s+charter|pool)\s+(?:tce\s+)?rate",
        "fleet_utilization": r"(?:fleet|commercial)\s+utili[sz]ation",
        "charter_coverage_next_12m": r"(?:contract(?:ed)?|fixed[- ]rate|time\s+charter)\s+coverage|(?:available|earning)\s+days\s+(?:fixed|covered)|percentage\s+covered",
        "contracted_revenue_backlog": r"remaining\s+performance\s+obligations?|future\s+(?:minimum\s+)?charter\s+(?:revenue|hire)|minimum\s+lease\s+payments\s+receivable",
        "weighted_average_charter_term": r"(?:weighted\s+)?average\s+remaining\s+(?:charter|lease)\s+(?:duration|term)",
        "cash_breakeven_per_day": r"cash\s+break[- ]?even(?:\s+(?:rate|per\s+day))?",
        "spot_exposure_ratio": r"spot(?:\s+market)?\s+exposure",
    }
    snippet = _near_value(text, patterns[metric], value)
    if not snippet:
        return CandidateReview(False, "tanker_metric_value_not_bound_to_exact_label", value)

    if metric == "fleet_capacity":
        valid = 10_000 <= value <= 50_000_000 and not filing_year_value and not currency_context and not percent_context
    elif metric == "revenue_days":
        valid = 10 <= value <= 100_000 and not filing_year_value and not percent_context
    elif metric == "offhire_or_drydock_ratio":
        valid = 0 <= value <= 0.25 and percent_context
    elif metric == "tce_day_rate":
        valid = 1_000 <= value <= 250_000 and not filing_year_value and currency_context and not change_context
    elif metric == "vessel_opex_per_day":
        valid = 2_500 <= value <= 50_000 and not filing_year_value and currency_context and not change_context
    elif metric == "spot_or_charter_day_rate":
        valid = 2_500 <= value <= 250_000 and not filing_year_value and currency_context and not change_context
    elif metric == "cash_breakeven_per_day":
        valid = 1_000 <= value <= 250_000 and not filing_year_value and currency_context and not change_context
    elif metric == "fleet_age":
        valid = 0 <= value <= 60 and not percent_context
    elif metric == "vessel_count":
        valid = 1 <= value <= 2_000 and value.is_integer() and not currency_context and not percent_context
    elif metric == "newbuild_capacity_commitments":
        valid = value >= 0 and not filing_year_value and not change_context
    elif metric in {"capex_commitments", "contracted_revenue_backlog"}:
        valid = value >= 100_000 and not filing_year_value and currency_context and not change_context
    elif metric in {"fleet_utilization", "charter_coverage_next_12m", "spot_exposure_ratio"}:
        valid = 0 <= value <= 1 and percent_context and not change_context
    elif metric == "weighted_average_charter_term":
        valid = 0 <= value <= 30 and not percent_context
    else:
        valid = False
    return CandidateReview(valid, "tanker_semantic_row_guard_pass" if valid else "tanker_semantic_row_guard_failed", value)
