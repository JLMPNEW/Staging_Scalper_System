from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from typing import Mapping


REVIEW_POLICY_VERSION = "transportation_surface_semantic_review_v5"

_NUMBER = re.compile(
    r"(?P<currency>US\$|CA\$|USD|CAD|\$)?\s*"
    r"(?P<open>\()?\s*(?P<value>[-+]?\d[\d,]*(?:\.\d+)?)\s*"
    r"(?P<percent>%|percent)?\s*(?P<close>\))?",
    re.IGNORECASE,
)

_EXPECTED_UNITS = {
    "average_length_of_haul": "distance",
    "empty_mile_ratio": "ratio",
    "fleet_or_equipment_count": "count",
    "freight_weight_per_shipment": "weight_per_shipment",
    "fuel_surcharge_revenue_ratio": "ratio",
    "logistics_net_revenue_margin": "ratio",
    "operating_ratio": "ratio",
    "pricing_or_yield_growth": "ratio",
    "purchased_transportation_ratio": "ratio",
    "rail_carload_growth": "ratio",
    "rail_fuel_efficiency": "fuel_per_gross_ton_mile",
    "rail_intermodal_volume_growth": "ratio",
    "rail_network_velocity": "distance_per_time",
    "revenue_per_shipment_or_load": "currency_per_unit",
    "revenue_per_tractor_or_power_unit": "currency_per_asset_period",
    "revenue_ton_miles_growth": "ratio",
    "service_reliability_rate": "ratio",
    "shipment_or_load_growth": "ratio",
    "terminal_dwell_time": "hours",
}

_FACT_REVENUE = {
    "RevenueFromContractWithCustomerExcludingAssessedTax",
    "Revenues",
    "SalesRevenueNet",
}
_FACT_OPERATING_EXPENSE = {"OperatingExpenses", "OperatingCostsAndExpenses"}
_FACT_OPERATING_INCOME = {"OperatingIncomeLoss"}
_FACT_PURCHASED = {"PurchasedTransportation", "PurchasedTransportationCosts"}


@dataclass(frozen=True)
class CandidateReview:
    approved: bool
    reason: str
    reviewed_value: float | None


def normalized(value: object) -> str:
    return " ".join(str(value or "").lower().split())


def definition_signature(row: Mapping[str, object]) -> tuple[str, ...]:
    return (
        str(row.get("source_lane") or ""),
        str(row.get("ticker") or ""),
        str(row.get("metric_id") or ""),
        normalized(row.get("concept_name")),
        normalized(row.get("unit")),
        normalized(row.get("extraction_method")),
        normalized(row.get("status_reason") or row.get("reason")),
        normalized(row.get("formula")),
        normalized(row.get("numerator_concept")),
        normalized(row.get("denominator_concept")),
    )


def definition_id(row: Mapping[str, object]) -> str:
    payload = "\x1f".join(definition_signature(row)).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def candidate_key(row: Mapping[str, object]) -> str:
    if str(row.get("source_lane") or "") == "parser_run_evidence":
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
        str(row.get("candidate_value") or row.get("value") or ""),
        str(row.get("numerator_concept") or ""),
        str(row.get("denominator_concept") or ""),
        str(row.get("source_document") or row.get("source_id") or ""),
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


def _raw_value(row: Mapping[str, object]) -> tuple[float | None, str, str]:
    provenance = _json(row.get("provenance_json"))
    raw = str(provenance.get("raw_value_text") or "")
    currency = str(provenance.get("raw_currency") or "")
    match = _NUMBER.search(raw)
    if match is None:
        return None, raw, currency
    try:
        value = float(match.group("value").replace(",", ""))
    except ValueError:
        return None, raw, currency
    if match.group("open") and match.group("close"):
        value = -abs(value)
    return value, raw, currency or str(match.group("currency") or "")


def _near_label(text: str, pattern: str, raw_value: float | None, *, width: int = 220) -> str:
    if raw_value is None:
        return ""
    for match in re.finditer(pattern, text, re.IGNORECASE):
        snippet = text[max(0, match.start() - 120) : match.end() + width]
        values: list[float] = []
        for number in _NUMBER.finditer(snippet):
            try:
                parsed = float(number.group("value").replace(",", ""))
            except ValueError:
                continue
            if 1900 <= parsed <= 2100 and parsed.is_integer():
                continue
            values.append(parsed)
            if len(values) >= 4:
                break
        if any(math.isclose(abs(value), abs(raw_value), rel_tol=1e-8, abs_tol=1e-8) for value in values):
            return snippet
    return ""


def _primary_power_unit_value(text: str) -> float | None:
    label = re.compile(r"\b(?:tractors|locomotives|power units)\b", re.I)
    for match in label.finditer(text):
        before = text[max(0, match.start() - 45) : match.start()]
        preceding = list(_NUMBER.finditer(before))
        if preceding:
            candidate = preceding[-1]
            try:
                value = float(candidate.group("value").replace(",", ""))
            except ValueError:
                value = 0.0
            if value >= 100 and not 1900 <= value <= 2100:
                return value
        after = text[match.end() : match.end() + 180]
        for number in _NUMBER.finditer(after):
            try:
                value = float(number.group("value").replace(",", ""))
            except ValueError:
                continue
            if value < 100 or 1900 <= value <= 2100:
                continue
            return value
    return None


def _derived_surface_ratio_review(
    row: Mapping[str, object],
    value: float | None,
) -> CandidateReview | None:
    concept = str(row.get("concept_name") or "")
    expected = {
        "DerivedPurchasedTransportationRatioFromReportedTable": (
            "purchased_transportation_ratio",
            "purchased_transportation/revenue",
            "PurchasedTransportationAndRelatedServices",
            "TotalConsolidatedRevenues",
        ),
        "DerivedFuelSurchargeRevenueRatioFromReportedTable": (
            "fuel_surcharge_revenue_ratio",
            "fuel_surcharge_revenue/freight_revenue",
            "FuelSurchargeRevenue",
            "FreightRevenues",
        ),
    }.get(concept)
    if expected is None:
        return None
    metric, formula, numerator_concept, denominator_concept = expected
    if str(row.get("metric_id") or "") != metric or value is None or not 0 <= value <= 1:
        return CandidateReview(False, "strict_table_ratio_contract_failed", value)
    provenance = _json(row.get("provenance_json"))
    if (
        provenance.get("formula") != formula
        or provenance.get("numerator_concept") != numerator_concept
        or provenance.get("denominator_concept") != denominator_concept
    ):
        return CandidateReview(False, "strict_table_ratio_operand_identity_failed", value)
    try:
        numerator = float(provenance["numerator_value"])
        denominator = float(provenance["denominator_value"])
    except (KeyError, TypeError, ValueError):
        return CandidateReview(False, "strict_table_ratio_operands_missing", value)
    if denominator <= 0 or numerator < 0 or numerator > denominator:
        return CandidateReview(False, "strict_table_ratio_operand_bounds_failed", value)
    if not math.isclose(value, numerator / denominator, rel_tol=1e-10, abs_tol=1e-10):
        return CandidateReview(False, "strict_table_ratio_recalculation_failed", value)
    return CandidateReview(True, "strict_table_ratio_definition_and_recalculation_pass", value)


def _percent_context(snippet: str, raw: str) -> bool:
    return bool(re.search(r"%|\bpercent(?:age)?\b", f"{raw} {snippet}", re.IGNORECASE))


def _raw_is_percent(raw: str) -> bool:
    return bool(re.search(r"%|\bpercent(?:age)?\b", raw, re.IGNORECASE))


def _currency_context(snippet: str, raw: str, currency: str) -> bool:
    return bool(currency or re.search(r"(?:US\$|CA\$|USD|CAD|\$)", f"{raw} {snippet}", re.IGNORECASE))


def _fact_review(row: Mapping[str, object]) -> CandidateReview:
    metric = str(row.get("metric_id") or "")
    numerator = str(row.get("numerator_concept") or "")
    denominator = str(row.get("denominator_concept") or "")
    formula = str(row.get("formula") or "")
    value = _value(row)
    provenance = _json(row.get("provenance_json"))
    try:
        numerator_value = float(provenance["numerator_value"])
        denominator_value = float(provenance["denominator_value"])
    except (KeyError, TypeError, ValueError):
        return CandidateReview(False, "fact_operand_values_missing", value)
    if value is None or denominator_value <= 0:
        return CandidateReview(False, "invalid_fact_ratio_inputs", value)
    if denominator not in _FACT_REVENUE:
        return CandidateReview(False, "revenue_denominator_not_exact", value)
    if metric == "operating_ratio":
        if numerator in _FACT_OPERATING_EXPENSE and formula == "operating_expense/revenue":
            calculated = numerator_value / denominator_value
        elif numerator in _FACT_OPERATING_INCOME and formula == "1-operating_income/revenue":
            calculated = 1.0 - numerator_value / denominator_value
        else:
            return CandidateReview(False, "operating_operand_not_definition_exact", value)
        if not 0.35 <= value <= 1.50:
            return CandidateReview(False, "operating_ratio_outside_semantic_range", value)
    elif metric == "purchased_transportation_ratio":
        if numerator not in _FACT_PURCHASED or formula != "purchased_transportation/revenue":
            return CandidateReview(False, "purchased_transportation_operand_not_exact", value)
        calculated = numerator_value / denominator_value
        if not 0.0 <= value <= 1.0:
            return CandidateReview(False, "purchased_transportation_ratio_out_of_range", value)
    else:
        return CandidateReview(False, "unsupported_fact_store_metric", value)
    if not math.isclose(value, calculated, rel_tol=1e-10, abs_tol=1e-10):
        return CandidateReview(False, "fact_ratio_recalculation_failed", value)
    return CandidateReview(True, "exact_fact_definition_and_recalculation_pass", value)


def _parser_review(row: Mapping[str, object]) -> CandidateReview:
    metric = str(row.get("metric_id") or "")
    concept = str(row.get("concept_name") or "")
    value = _value(row)
    unit = str(row.get("unit") or "")
    text = " ".join(str(row.get("evidence_text") or "").split())
    raw_value, raw, currency = _raw_value(row)
    if value is None or not text:
        return CandidateReview(False, "missing_parser_value_or_evidence", value)
    derived_ratio = _derived_surface_ratio_review(row, value)
    if derived_ratio is not None:
        return derived_ratio
    if unit != _EXPECTED_UNITS.get(metric):
        return CandidateReview(False, "metric_unit_contract_failed", value)
    if re.search(r"\b(?:peer|competitor|acquisition target|customer fleet|pro forma)\b", text, re.I):
        return CandidateReview(False, "nonissuer_or_proforma_context", value)

    def near(pattern: str, width: int = 220) -> str:
        return _near_label(text, pattern, raw_value, width=width)

    if metric == "average_length_of_haul":
        snippet = near(r"(?:average|ltl average)\s+length\s+of\s+haul(?:\s*\(miles\))?")
        ok = bool(snippet) and 50 <= value <= 3000 and not _raw_is_percent(raw)
    elif metric == "empty_mile_ratio":
        snippet = near(r"(?:non[- ]?paid\s+)?empty\s+mile(?:s)?\s+(?:percentage|ratio)")
        ok = bool(snippet) and 0 <= value <= 0.80 and _percent_context(snippet, raw)
    elif metric == "fleet_or_equipment_count":
        snippet = near(r"(?:tractors|locomotives|power units)")
        primary_count = _primary_power_unit_value(text)
        count_context = bool(re.search(
            r"average\s+(?:company-owned\s+)?tractors|number of (?:tractors|locomotives)|"
            r"tractors\s*\(end of period\)|"
            r"(?:tractor|locomotive) fleet|fleet (?:consisted|included)|"
            r"owned (?:approximately |about )?\d[\d,]* (?:tractors|locomotives)|"
            r"operated (?:a fleet of )?\d[\d,]* (?:tractors|locomotives)|"
            r"power fleet (?:consisted|included)",
            text, re.I,
        ))
        dollar_context = bool(re.search(
            r"capital expenditures?|property and equipment consisted|\(\$?\s*in millions\)|"
            r"net capital expenditures|purchased over|ordered|new tier",
            text, re.I,
        ))
        ok = (
            bool(snippet) and count_context and not dollar_context and value >= 1
            and value <= 250_000
            and primary_count is not None
            and math.isclose(value, primary_count, rel_tol=1e-8, abs_tol=1e-8)
            and math.isclose(value, round(value), abs_tol=1e-8)
            and not _raw_is_percent(raw) and not _currency_context(snippet, raw, currency)
            and concept in {
                "ReportedSurfaceOperatingKpi",
                "ReportedLtlTractorCount",
                "ReportedRailLocomotiveCount",
            }
        )
    elif metric == "freight_weight_per_shipment":
        snippet = near(r"(?:average\s+)?(?:weight|pounds)\s+per\s+shipment")
        ok = bool(snippet) and 100 <= value <= 10000 and not _raw_is_percent(raw)
    elif metric == "fuel_surcharge_revenue_ratio":
        snippet = near(r"fuel\s+surcharge\s+revenue(?:s)?")
        ok = bool(snippet) and 0 <= value <= 1 and _percent_context(snippet, raw) and bool(
            re.search(r"percent(?:age)?\s+of\s+(?:operating\s+)?revenue|%\s+of\s+(?:operating\s+)?revenue", text, re.I)
        )
    elif metric == "logistics_net_revenue_margin":
        snippet = near(r"(?:adjusted\s+)?gross\s+profit\s+margin|net\s+revenue\s+margin|variable\s+contribution\s+margin")
        ok = bool(snippet) and 0 <= value <= 1 and _percent_context(snippet, raw)
    elif metric == "operating_ratio":
        snippet = near(r"(?:railway\s+|gaap:\s*|adjusted\s+)?operating\s+ratio")
        excluded = bool(re.search(r"profit-sharing bonus|achievement of|threshold|target operating ratio", text, re.I))
        ok = bool(snippet) and not excluded and 0.35 <= value <= 1.50 and _percent_context(snippet, raw)
    elif metric == "pricing_or_yield_growth":
        snippet = near(r"pricing\s+growth|yield\s+growth|revenue\s+per\s+(?:hundredweight|cwt|shipment)|rate\s+per\s+mile")
        ok = (
            bool(snippet) and -1 <= value <= 1 and _percent_context(snippet, raw)
            and not _currency_context(snippet, raw, currency)
        )
    elif metric == "purchased_transportation_ratio":
        snippet = near(r"(?:rents\s+and\s+)?purchased\s+transportation(?:\s+(?:costs|expense))?")
        ok = bool(snippet) and 0 <= value <= 1 and _percent_context(snippet, raw) and not _currency_context(snippet, raw, currency)
    elif metric == "rail_carload_growth":
        snippet = near(r"carloads?")
        ok = bool(snippet) and -1 <= value <= 1 and _percent_context(snippet, raw) and bool(
            re.search(r"carloads?.{0,100}(?:increase|decrease|growth|change|higher|lower)", text, re.I)
        )
    elif metric == "rail_fuel_efficiency":
        snippet = near(
            r"fuel\s+efficiency|fuel\s+consumption\s+rate|"
            r"gallons?\s+(?:of\s+fuel\s+)?per\s+(?:1,?000|thousand)\s+gross\s+ton[- ]?miles"
        )
        ok = bool(snippet) and 0.05 <= value <= 10 and not _raw_is_percent(raw)
    elif metric == "rail_intermodal_volume_growth":
        snippet = near(r"intermodal\s+(?:shipments?|volume|carloads?)")
        ok = bool(snippet) and -1 <= value <= 1 and _percent_context(snippet, raw) and bool(
            re.search(r"intermodal.{0,120}(?:increase|decrease|decline|growth|change|higher|lower)", text, re.I)
        )
    elif metric == "rail_network_velocity":
        snippet = near(r"train\s+velocity|average\s+train\s+speed|train\s+speed")
        ok = bool(snippet) and 5 <= value <= 80 and not _raw_is_percent(raw)
    elif metric == "revenue_per_shipment_or_load":
        snippet = near(r"(?:billed\s+|ltl\s+)?revenue\s+per\s+(?:shipment|load)")
        ok = bool(snippet) and value > 0 and _currency_context(snippet, raw, currency)
    elif metric == "revenue_per_tractor_or_power_unit":
        snippet = near(r"revenue\s+per\s+(?:truck|tractor|power unit)(?:\s+per\s+(?:week|day|month|year))?")
        multiplier = 0.0
        if re.search(r"per\s+(?:truck|tractor|power unit)\s+per\s+week", snippet, re.I):
            multiplier = 52.0
        elif re.search(r"per\s+(?:truck|tractor|power unit)\s+per\s+day", snippet, re.I):
            multiplier = 365.0
        elif re.search(r"per\s+(?:truck|tractor|power unit)\s+per\s+month", snippet, re.I):
            multiplier = 12.0
        elif re.search(r"per\s+(?:truck|tractor|power unit)\s+per\s+year|\byear ended\b|\bannual\b", snippet, re.I):
            multiplier = 1.0
        elif re.search(r"\bthree months ended\b", snippet, re.I):
            multiplier = 4.0
        elif re.search(r"\bsix months ended\b", snippet, re.I):
            multiplier = 2.0
        elif re.search(r"\bnine months ended\b", snippet, re.I):
            multiplier = 4.0 / 3.0
        ok = (
            bool(snippet) and multiplier > 0 and value >= 100
            and _currency_context(snippet, raw, currency)
        )
        if ok:
            value *= multiplier
    elif metric == "revenue_ton_miles_growth":
        snippet = near(r"rtms?|revenue\s+ton[- ]miles")
        local = bool(re.search(
            r"(?:rtms?|revenue\s+ton[- ]miles)[^|.]{0,120}(?:increase|decrease|decline|growth|change|higher|lower)[^|.]{0,80}(?:%|percent)",
            text, re.I,
        ))
        ok = bool(snippet) and local and -1 <= value <= 1 and _percent_context(snippet, raw)
    elif metric == "service_reliability_rate":
        snippet = near(r"on[- ]time\s+(?:delivery|performance)|service\s+reliability(?:\s+rate)?")
        excluded = bool(re.search(r"compensation|performance award|customer service\s+\d+%", text, re.I))
        ok = bool(snippet) and not excluded and 0 <= value <= 1 and _percent_context(snippet, raw)
    elif metric == "shipment_or_load_growth":
        snippet = near(
            r"(?:ltl\s+)?shipments?\s+per\s+(?:day|workday)|"
            r"loads?\s+(?:growth|change)"
        )
        ok = bool(snippet) and -1 <= value <= 1 and _percent_context(snippet, raw)
    elif metric == "terminal_dwell_time":
        snippet = near(r"(?:average\s+)?terminal\s+dwell(?:\s+time)?")
        ok = (
            bool(snippet) and 0.5 <= value <= 100 and not _raw_is_percent(raw)
            and bool(re.search(r"hours?", snippet, re.I))
        )
    else:
        return CandidateReview(False, "unsupported_parser_metric", value)
    return CandidateReview(bool(ok), "semantic_row_guard_pass" if ok else "semantic_row_guard_failed", value)


def review_candidate(row: Mapping[str, object]) -> CandidateReview:
    lane = str(row.get("source_lane") or "")
    if lane == "fact_store_ratio":
        return _fact_review(row)
    if lane == "parser_run_evidence":
        return _parser_review(row)
    return CandidateReview(False, "unsupported_source_lane", _value(row))
