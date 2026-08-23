from __future__ import annotations

import math
import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from typing import Iterable, Mapping, Sequence

from dedicated_parser.contracts import MetricEvidence, NormalizedFact, WorkItem
from dedicated_parser.semantic import SemanticBlock, normalize_space


_NUMBER = re.compile(
    r"(?<![A-Za-z0-9])(?P<open>\()?\s*(?P<currency>US\$|CA\$|USD|CAD|\$)?\s*"
    r"(?P<value>[-+]?\d[\d,]*(?:\.\d+)?)\s*(?P<percent>%|percent)?\s*(?P<close>\))?",
    re.IGNORECASE,
)
_YEAR = re.compile(r"\b(?:19|20)\d{2}\b")
_DATE = re.compile(
    r"\b(?P<iso>20\d{2}-\d{2}-\d{2})\b|"
    r"\b(?P<month>January|February|March|April|May|June|July|August|"
    r"September|October|November|December)\s+"
    r"(?P<day>\d{1,2}),\s+(?P<year>20\d{2})\b",
    re.IGNORECASE,
)
_NONISSUER = re.compile(
    r"\b(?:peer|competitor|industry|market average|pro forma|acquisition target|customer fleet)\b",
    re.IGNORECASE,
)
_ISSUER = re.compile(
    r"\b(?:our|we|the company(?:'s)?|consolidated|company operations)\b",
    re.IGNORECASE,
)

_GROWTH_METRICS = frozenset(
    {
        "rail_carload_growth",
        "rail_intermodal_volume_growth",
        "revenue_ton_miles_growth",
        "shipment_or_load_growth",
        "pricing_or_yield_growth",
    }
)
_RATIO_METRICS = frozenset(
    {
        "operating_ratio",
        "purchased_transportation_ratio",
        "fuel_surcharge_revenue_ratio",
        "driver_turnover_rate",
        "empty_mile_ratio",
        "service_reliability_rate",
        "insurance_claims_cost_ratio",
        "logistics_net_revenue_margin",
    }
)
_UNIT_BY_METRIC = {
    "fleet_or_equipment_count": "count",
    "average_length_of_haul": "distance",
    "freight_weight_per_shipment": "weight_per_shipment",
    "revenue_per_shipment_or_load": "currency_per_unit",
    "rail_fuel_efficiency": "fuel_per_gross_ton_mile",
    "rail_network_velocity": "distance_per_time",
    "terminal_dwell_time": "hours",
    "revenue_per_tractor_or_power_unit": "currency_per_asset_period",
    "surface_asset_age": "years",
}

_LTL_POWER_UNIT_TICKERS = frozenset({"ARCB", "ODFL", "SAIA", "XPO"})
_RAIL_POWER_UNIT_TICKERS = frozenset({"CNI", "CP", "CSX", "NSC", "UNP"})
_LTL_YIELD_LABEL = re.compile(
    r"\b(?:ltl\s+|gross\s+)?revenue\s+per\s+"
    r"(?:hundredweight|cwt|shipment)\b|\bltl\s+yield\b|\byield\s+growth\b",
    re.IGNORECASE,
)
_LTL_SHIPMENT_LABEL = re.compile(
    r"\b(?:ltl\s+)?shipments?\s+per\s+(?:day|workday)\b|"
    r"\btotal\s+ltl\s+shipments?\b",
    re.IGNORECASE,
)
_PURCHASED_TRANSPORTATION_ROW = re.compile(
    r"\b(?:total\s+)?purchased\s+transportation(?:\s+and\s+related\s+services)?\b",
    re.IGNORECASE,
)
_CONSOLIDATED_REVENUE_ROW = re.compile(
    r"\b(?:total\s+consolidated\s+revenues?|total\s+revenues?)\b",
    re.IGNORECASE,
)
_FUEL_SURCHARGE_ROW = re.compile(
    r"\bfuel\s+surcharge\s+revenues?\b",
    re.IGNORECASE,
)
_RAIL_REVENUE_ROW = re.compile(
    r"\b(?:total\s+)?(?:freight|railway\s+operating|operating)\s+revenues?\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class _ParsedNumber:
    value: float
    percent: bool
    currency: str
    raw: str


def _phrase_pattern(value: str) -> re.Pattern[str]:
    tokens = re.findall(r"[A-Za-z0-9]+", value)
    return re.compile(
        r"\b" + r"[\s/_-]*".join(re.escape(token) for token in tokens) + r"\b",
        re.IGNORECASE,
    )


def _aliases(contract: Mapping[str, str]) -> tuple[re.Pattern[str], ...]:
    values = [
        value.strip()
        for value in str(contract.get("parser_aliases") or "").split("|")
        if value.strip()
    ]
    return tuple(_phrase_pattern(value) for value in values)


def _numbers(value: str, *, keep_years: bool = False) -> list[_ParsedNumber]:
    output: list[_ParsedNumber] = []
    for match in _NUMBER.finditer(value):
        try:
            parsed = float(match.group("value").replace(",", ""))
        except ValueError:
            continue
        if match.group("open") and match.group("close"):
            parsed = -abs(parsed)
        is_year = parsed.is_integer() and 1900 <= parsed <= 2100
        if is_year and not keep_years:
            continue
        output.append(
            _ParsedNumber(
                value=parsed,
                percent=bool(match.group("percent")),
                currency=str(match.group("currency") or ""),
                raw=match.group(0),
            )
        )
    return output


def _date_value(text: str, fallback: str) -> str:
    match = _DATE.search(text)
    if match is None:
        return fallback[:10]
    if match.group("iso"):
        return str(match.group("iso"))
    try:
        return datetime.strptime(
            f"{match.group('month')} {match.group('day')}, {match.group('year')}",
            "%B %d, %Y",
        ).date().isoformat()
    except ValueError:
        return fallback[:10]


def _scope(text: str) -> str:
    if _NONISSUER.search(text):
        return "nonissuer"
    if _ISSUER.search(text):
        return "consolidated"
    return "unknown"


def _section_context(
    item: WorkItem,
    block: SemanticBlock,
    contract: Mapping[str, str],
    *,
    source_kind: str,
    filing_profile: Mapping[str, str] | None = None,
) -> dict[str, object]:
    form = item.filing.form_type.upper()
    if form.startswith("10-"):
        field = "source_sections_10k"
    elif form.startswith(("20-F", "40-F")):
        field = "source_sections_foreign"
    else:
        field = "source_sections_event"
    preferred = tuple(
        value.strip()
        for value in str(contract.get(field) or "").split("|")
        if value.strip()
    )
    text = normalize_space(" | ".join((*block.section_path, block.preamble_text, block.text))).casefold()
    matched = [value for value in preferred if normalize_space(value).casefold() in text]
    profile = filing_profile or {}
    expected_form = str(profile.get("annual_form") or "")
    annual = form in {"10-K", "10-K/A", "20-F", "20-F/A", "40-F", "40-F/A"}
    return {
        "filing_form_type": item.filing.form_type,
        "accounting_framework": str(profile.get("accounting_framework") or ""),
        "expected_annual_form": expected_form,
        "form_profile_match": bool(annual and expected_form and form.startswith(expected_form)),
        "document_source_kind": source_kind,
        "preferred_sections": list(preferred),
        "matched_sections": matched,
        "preferred_section_match": bool(matched),
        "expected_tables": [
            value.strip()
            for value in str(contract.get("expected_tables") or "").split("|")
            if value.strip()
        ],
        "source_posture": str(contract.get("source_posture") or ""),
    }


def _row_label_index(
    cells: Sequence[str],
    patterns: Sequence[re.Pattern[str]],
) -> int | None:
    for index, cell in enumerate(cells):
        if any(pattern.search(normalize_space(cell)) for pattern in patterns):
            return index
    return None


def _value_after_label(
    cells: Sequence[str],
    label_index: int,
    patterns: Sequence[re.Pattern[str]],
) -> _ParsedNumber | None:
    for cell in cells[label_index + 1 :]:
        values = _numbers(cell)
        if values:
            return values[0]
    residual = cells[label_index]
    for pattern in patterns:
        residual = pattern.sub(" ", residual)
    values = _numbers(residual)
    return values[0] if values else None


def _year_column_values(
    row: SemanticBlock,
) -> list[tuple[int, float]]:
    headers = row.header_cells
    if not headers or len(headers) != len(row.cells):
        return []
    output: list[tuple[int, float]] = []
    for index, header in enumerate(headers):
        year = _YEAR.search(header)
        if year is None or index >= len(row.cells):
            continue
        values = _numbers(row.cells[index])
        if values:
            output.append((int(year.group(0)), values[0].value))
    return sorted(output, reverse=True)


def _normalize_metric_value(
    metric_id: str,
    parsed: _ParsedNumber,
    *,
    context: str,
) -> tuple[float, str, dict[str, object]] | None:
    value = parsed.value
    normalized_context = context.casefold()
    if metric_id in _GROWTH_METRICS or metric_id in _RATIO_METRICS:
        percent_context = parsed.percent or bool(
            re.search(r"\b(?:percent|percentage|ratio|margin|change|growth)\b|%", normalized_context)
        )
        if metric_id in _GROWTH_METRICS and not (
            percent_context
            or re.search(
                r"\b(?:increased|decreased|declined|grew|fell|higher|lower|contracted)\b",
                normalized_context,
            )
        ):
            # A raw carload/load/RTM/yield level is not a growth rate. Let
            # the comparable-year table derivation below compute YoY.
            return None
        if percent_context and abs(value) > 1.0:
            value /= 100.0
        if metric_id in _GROWTH_METRICS and re.search(
            r"\b(?:decreased|declined|fell|lower|contracted)\b",
            normalized_context,
        ):
            value = -abs(value)
        return value, "ratio", {"raw_value_text": parsed.raw, "percent_context": percent_context}
    if metric_id == "equipment_utilization":
        if "day" in normalized_context and not (parsed.percent or "%" in normalized_context):
            return value, "days", {"raw_value_text": parsed.raw}
        if parsed.percent or "%" in normalized_context or "utilization" in normalized_context:
            if abs(value) > 1.0:
                value /= 100.0
            return value, "ratio", {"raw_value_text": parsed.raw}
        return None
    if metric_id == "rail_fuel_efficiency":
        if re.search(r"\b(?:gtm|gross\s+ton[- ]?miles?)\s+per\s+gallon\b", normalized_context):
            if value <= 0:
                return None
            value = 1000.0 / value
            basis = "converted_from_gtm_per_gallon"
        elif re.search(
            r"gallons?.{0,30}(?:per|/).{0,20}(?:1,?000|thousand).{0,20}(?:gtm|gross\s+ton)",
            normalized_context,
        ):
            basis = "reported_gallons_per_1000_gtm"
        elif re.search(r"gallons?.{0,20}(?:per|/).{0,20}(?:gtm|gross\s+ton)", normalized_context):
            value *= 1000.0
            basis = "converted_from_gallons_per_gtm"
        else:
            basis = "issuer_reported_fuel_efficiency"
        return value, "fuel_per_gross_ton_mile", {
            "raw_value_text": parsed.raw,
            "definition_basis": basis,
            "normalized_unit": "gallons_per_1000_gtm",
        }
    if metric_id == "rail_network_velocity":
        if re.search(r"\b(?:car|freight\s+car)\s+(?:velocity|miles?\s+per\s+day)\b", normalized_context):
            basis = "car_velocity_miles_per_day"
        elif re.search(r"\b(?:train\s+speed|train\s+velocity)\b", normalized_context):
            basis = "train_speed_mph"
        else:
            basis = "network_velocity_unresolved"
        return value, "distance_per_time", {
            "raw_value_text": parsed.raw,
            "definition_basis": basis,
        }
    unit = _UNIT_BY_METRIC.get(metric_id)
    if not unit:
        return None
    return value, unit, {
        "raw_value_text": parsed.raw,
        "raw_currency": parsed.currency,
    }


def _table_evidence(
    item: WorkItem,
    *,
    metric_id: str,
    value: float,
    unit: str,
    block: SemanticBlock,
    source_document: str,
    document_sha256: str,
    source_kind: str,
    contract: Mapping[str, str],
    filing_profile: Mapping[str, str] | None,
    concept_name: str,
    reason: str,
    extra_provenance: Mapping[str, object],
) -> MetricEvidence:
    source_context = _section_context(
        item,
        block,
        contract,
        source_kind=source_kind,
        filing_profile=filing_profile,
    )
    scope = _scope(block.search_text)
    return MetricEvidence(
        metric_name=metric_id,
        concept_name=concept_name,
        value=value,
        unit=unit,
        period_start="",
        period_end=_date_value(block.search_text, item.filing.report_date),
        scope=scope,
        confidence=0.90 if source_context["preferred_section_match"] else 0.82,
        status="REJECTED_POLICY" if scope == "nonissuer" else "REVIEW_REQUIRED",
        reason="nonissuer_or_proforma_scope" if scope == "nonissuer" else reason,
        evidence_text=block.search_text[:2000],
        source_document=source_document,
        extraction_method="dedicated_parser:transportation_surface_table_v1",
        provenance={
            "surface_parser_version": "transportation_surface_tables_v1",
            "document_sha256": document_sha256,
            "semantic_block_index": block.index,
            "semantic_table_id": block.table_id,
            "semantic_row_index": block.row_index,
            "semantic_section_path": list(block.section_path),
            "unit_contract": unit,
            **source_context,
            **dict(extra_provenance),
        },
    )


def _signed_growth_value(
    text: str,
    label: re.Pattern[str],
) -> _ParsedNumber | None:
    value_pattern = r"(?P<value>\d[\d,]*(?:\.\d+)?)\s*(?P<unit>%|percent)"
    direction_pattern = r"(?P<direction>increase(?:d)?|decrease(?:d)?|decline(?:d)?|grew|fell|up|down)"
    patterns = (
        re.compile(
            value_pattern
            + r"\s+"
            + direction_pattern
            + r"\s+in\s+(?:our\s+)?(?:"
            + label.pattern
            + r")",
            re.IGNORECASE,
        ),
        re.compile(
            r"(?:"
            + label.pattern
            + r").{0,100}?"
            + direction_pattern
            + r"(?:\s+by)?\s+"
            + value_pattern,
            re.IGNORECASE,
        ),
        re.compile(
            r"(?:"
            + label.pattern
            + r").{0,40}?\b(?:growth\s+)?of\s+"
            + value_pattern,
            re.IGNORECASE,
        ),
    )
    for pattern in patterns:
        match = pattern.search(text)
        if match is None:
            continue
        value = float(match.group("value").replace(",", ""))
        direction = str(match.groupdict().get("direction") or "").casefold()
        if direction.startswith(("decrease", "decline")) or direction in {"fell", "down"}:
            value = -abs(value)
        else:
            value = abs(value)
        return _ParsedNumber(
            value=value,
            percent=True,
            currency="",
            raw=f"{match.group('value')} {match.group('unit')}",
        )
    return None


def _strict_surface_evidence(
    item: WorkItem,
    block: SemanticBlock,
    *,
    metric_id: str,
    parsed: _ParsedNumber,
    value: float,
    unit: str,
    concept_name: str,
    rule_id: str,
    source_document: str,
    document_sha256: str,
    source_kind: str,
    contract: Mapping[str, str],
    filing_profile: Mapping[str, str] | None,
    document_extraction_method: str,
    document_extraction_warning: str,
    document_extraction_cache_status: str,
) -> MetricEvidence:
    source_context = _section_context(
        item,
        block,
        contract,
        source_kind=source_kind,
        filing_profile=filing_profile,
    )
    scope = _scope(block.search_text)
    return MetricEvidence(
        metric_name=metric_id,
        concept_name=concept_name,
        value=value,
        unit=unit,
        period_start="",
        period_end=_date_value(block.search_text, item.filing.report_date),
        scope=scope,
        confidence=0.94 if source_context["preferred_section_match"] else 0.88,
        status="REJECTED_POLICY" if scope == "nonissuer" else "REVIEW_REQUIRED",
        reason=(
            "nonissuer_or_proforma_scope"
            if scope == "nonissuer"
            else "strict_surface_definition_requires_semantic_replay"
        ),
        evidence_text=block.search_text[:2000],
        source_document=source_document,
        extraction_method="dedicated_parser:transportation_surface_strict_v2",
        provenance={
            "surface_parser_version": "transportation_surface_strict_v2",
            "strict_rule_id": rule_id,
            "raw_value_text": parsed.raw,
            "raw_currency": parsed.currency,
            "unit_contract": unit,
            "document_sha256": document_sha256,
            "semantic_block_index": block.index,
            "semantic_block_kind": block.kind,
            "semantic_table_id": block.table_id,
            "semantic_row_index": block.row_index,
            "semantic_section_path": list(block.section_path),
            "document_extraction_method": document_extraction_method,
            "document_extraction_warning": document_extraction_warning,
            "document_extraction_cache_status": document_extraction_cache_status,
            **source_context,
        },
    )


def _strict_power_unit_count(
    ticker: str,
    text: str,
) -> tuple[_ParsedNumber, str, str] | None:
    if ticker in _LTL_POWER_UNIT_TICKERS:
        patterns = (
            re.compile(
                r"\b(?:owned|operated)(?:\s+(?:a|an))?(?:\s+fleet\s+of)?"
                r"(?:\s+approximately|\s+about)?\s+"
                r"(?P<count>\d[\d,]*)\s+(?:tractors|power\s+units)\b",
                re.IGNORECASE,
            ),
            re.compile(
                r"\bpower\s+fleet\s+(?:consisted\s+of|included)\s+"
                r"(?P<count>\d[\d,]*)\s+tractors\b",
                re.IGNORECASE,
            ),
        )
        concept = "ReportedLtlTractorCount"
        rule_id = "ltl_owned_or_operated_tractor_count"
    elif ticker in _RAIL_POWER_UNIT_TICKERS:
        patterns = (
            re.compile(
                r"\b(?:owned|leased|operated)(?:\s+(?:a|an))?(?:\s+fleet\s+of)?"
                r"(?:\s+approximately|\s+about)?\s+"
                r"(?P<count>\d[\d,]*)\s+locomotives\b",
                re.IGNORECASE,
            ),
            re.compile(
                r"\blocomotive\s+fleet\s+(?:consisted\s+of|included)\s+"
                r"(?P<count>\d[\d,]*)\b",
                re.IGNORECASE,
            ),
        )
        concept = "ReportedRailLocomotiveCount"
        rule_id = "rail_owned_or_operated_locomotive_count"
    else:
        return None
    prohibited = re.compile(
        r"\b(?:capital\s+expenditure|"
        r"in\s+millions|useful\s+life|locomotive\s+engineers?)\b",
        re.IGNORECASE,
    )
    if prohibited.search(text):
        return None
    for pattern in patterns:
        match = pattern.search(text)
        if match is None:
            continue
        count = float(match.group("count").replace(",", ""))
        if 1 <= count <= 250_000 and count.is_integer():
            return (
                _ParsedNumber(count, False, "", match.group("count")),
                concept,
                rule_id,
            )
    return None


def _row_operand(
    rows: Sequence[SemanticBlock],
    pattern: re.Pattern[str],
) -> tuple[float, SemanticBlock, str] | None:
    for row in rows:
        for index, cell in enumerate(row.cells):
            label = normalize_space(cell)
            match = pattern.search(label)
            if match is None or len(label) > 180:
                continue
            parsed = _value_after_label(row.cells, index, (pattern,))
            if parsed is not None and parsed.value > 0:
                return parsed.value, row, label
    return None


def _strict_surface_ratio_evidence(
    item: WorkItem,
    rows: Sequence[SemanticBlock],
    *,
    metric_id: str,
    numerator_pattern: re.Pattern[str],
    denominator_pattern: re.Pattern[str],
    numerator_concept: str,
    denominator_concept: str,
    formula: str,
    source_document: str,
    document_sha256: str,
    source_kind: str,
    contract: Mapping[str, str],
    filing_profile: Mapping[str, str] | None,
) -> MetricEvidence | None:
    table_text = " | ".join(row.search_text for row in rows)
    if metric_id == "purchased_transportation_ratio" and re.search(
        r"\breportable\s+segment\s+information\b", table_text, re.IGNORECASE
    ):
        return None
    numerator = _row_operand(rows, numerator_pattern)
    denominator = _row_operand(rows, denominator_pattern)
    if numerator is None or denominator is None:
        return None
    numerator_value, numerator_row, numerator_label = numerator
    denominator_value, denominator_row, denominator_label = denominator
    if denominator_value <= 0 or numerator_value > denominator_value:
        return None
    value = numerator_value / denominator_value
    if not math.isfinite(value) or not 0 <= value <= 1:
        return None
    source_context = _section_context(
        item,
        numerator_row,
        contract,
        source_kind=source_kind,
        filing_profile=filing_profile,
    )
    scope = _scope(table_text)
    return MetricEvidence(
        metric_name=metric_id,
        concept_name=(
            "DerivedPurchasedTransportationRatioFromReportedTable"
            if metric_id == "purchased_transportation_ratio"
            else "DerivedFuelSurchargeRevenueRatioFromReportedTable"
        ),
        value=value,
        unit="ratio",
        period_start="",
        period_end=item.filing.report_date[:10],
        scope=scope,
        confidence=0.94 if source_context["preferred_section_match"] else 0.89,
        status="REJECTED_POLICY" if scope == "nonissuer" else "REVIEW_REQUIRED",
        reason=(
            "nonissuer_or_proforma_scope"
            if scope == "nonissuer"
            else "derived_from_exact_reported_table_operands_requires_semantic_replay"
        ),
        evidence_text=(
            f"{numerator_label}={numerator_value:g}; "
            f"{denominator_label}={denominator_value:g}; formula={formula}; "
            f"{table_text}"
        )[:2000],
        source_document=source_document,
        extraction_method="dedicated_parser:transportation_surface_strict_v2",
        provenance={
            "surface_parser_version": "transportation_surface_strict_v2",
            "strict_rule_id": f"{metric_id}_paired_table_operands",
            "formula": formula,
            "numerator_concept": numerator_concept,
            "numerator_value": numerator_value,
            "denominator_concept": denominator_concept,
            "denominator_value": denominator_value,
            "raw_value_text": f"{value:.12g}",
            "unit_contract": "ratio",
            "document_sha256": document_sha256,
            "semantic_table_id": numerator_row.table_id,
            "numerator_row_index": numerator_row.row_index,
            "denominator_row_index": denominator_row.row_index,
            **source_context,
        },
    )


def _pipe_values(value: object) -> tuple[str, ...]:
    return tuple(
        item.strip() for item in str(value or "").split("|") if item.strip()
    )


def _operand_from_aliases(
    rows: Sequence[SemanticBlock],
    aliases: object,
) -> tuple[float, SemanticBlock, str] | None:
    patterns = tuple(_phrase_pattern(alias) for alias in _pipe_values(aliases))
    if not patterns:
        return None
    for row in rows:
        for index, cell in enumerate(row.cells):
            label = normalize_space(cell)
            if len(label) > 180 or not any(pattern.search(label) for pattern in patterns):
                continue
            parsed = _value_after_label(row.cells, index, patterns)
            if parsed is not None and parsed.value >= 0:
                return parsed.value, row, label
    return None


def _surface_contract_evidence(
    item: WorkItem,
    rows: Sequence[SemanticBlock],
    *,
    contract: Mapping[str, str],
    source_document: str,
    document_sha256: str,
    source_kind: str,
    source_contract: Mapping[str, str],
    filing_profile: Mapping[str, str] | None,
) -> MetricEvidence | None:
    ticker = item.filing.ticker.upper()
    if ticker not in _pipe_values(contract.get("applicable_tickers", "")):
        return None
    table_text = " | ".join(row.search_text for row in rows)
    segment_aliases = _pipe_values(contract.get("segment_aliases", ""))
    matched_segment = next(
        (
            alias
            for alias in segment_aliases
            if _phrase_pattern(alias).search(table_text)
        ),
        "",
    )
    if segment_aliases and not matched_segment:
        return None
    numerator = _operand_from_aliases(rows, contract.get("numerator_aliases", ""))
    denominator = _operand_from_aliases(rows, contract.get("denominator_aliases", ""))
    alternate = _operand_from_aliases(
        rows, contract.get("alternate_numerator_aliases", "")
    )
    if denominator is None or denominator[0] <= 0:
        return None
    denominator_value, denominator_row, denominator_label = denominator
    formula_contract = str(contract.get("formula") or "")
    if numerator is not None:
        numerator_value, numerator_row, numerator_label = numerator
        if formula_contract.startswith("1-"):
            value = 1.0 - numerator_value / denominator_value
            formula = "1-numerator/denominator"
        else:
            value = numerator_value / denominator_value
            formula = "numerator/denominator"
    elif alternate is not None and "alternate" in formula_contract:
        numerator_value, numerator_row, numerator_label = alternate
        value = 1.0 - numerator_value / denominator_value
        formula = "1-alternate/denominator"
    else:
        return None
    metric_id = str(contract["metric_id"])
    upper_bound = {
        "freight_weight_per_shipment": 10_000.0,
        "operating_ratio": 1.5,
    }.get(metric_id, 1.0)
    if not math.isfinite(value) or not 0 <= value <= upper_bound:
        return None
    concept_by_metric = {
        "freight_weight_per_shipment": "DerivedFreightWeightPerShipment",
        "operating_ratio": "DerivedSurfaceSegmentOperatingRatio",
        "purchased_transportation_ratio": "DerivedSurfacePurchasedTransportationRatio",
        "logistics_net_revenue_margin": "DerivedSurfaceLogisticsNetRevenueMargin",
    }
    concept_name = concept_by_metric.get(metric_id)
    if concept_name is None:
        return None
    source_context = _section_context(
        item,
        numerator_row,
        source_contract,
        source_kind=source_kind,
        filing_profile=filing_profile,
    )
    scope = _scope(table_text)
    comparability = str(contract.get("comparability_class") or "")
    return MetricEvidence(
        metric_name=metric_id,
        concept_name=concept_name,
        value=value,
        unit=str(contract.get("unit_contract") or ""),
        period_start="",
        period_end=_date_value(table_text, item.filing.report_date),
        scope=scope,
        confidence=0.94 if comparability.startswith("exact") else 0.82,
        status="REJECTED_POLICY" if scope == "nonissuer" else "REVIEW_REQUIRED",
        reason=(
            "nonissuer_or_proforma_scope"
            if scope == "nonissuer"
            else "contract_derived_surface_metric_requires_semantic_replay"
        ),
        evidence_text=(
            f"{numerator_label}={numerator_value:g}; "
            f"{denominator_label}={denominator_value:g}; "
            f"formula={formula}; segment={matched_segment}; {table_text}"
        )[:2000],
        source_document=source_document,
        extraction_method="dedicated_parser:transportation_surface_contract_v1",
        provenance={
            "surface_parser_version": "transportation_surface_contract_v1",
            "derivation_contract_version": contract.get("contract_version", ""),
            "derivation_id": contract.get("derivation_id", ""),
            "formula": formula,
            "numerator_value": numerator_value,
            "numerator_label": numerator_label,
            "numerator_row_index": numerator_row.row_index,
            "denominator_value": denominator_value,
            "denominator_label": denominator_label,
            "denominator_row_index": denominator_row.row_index,
            "segment_id": matched_segment,
            "definition_basis": contract.get("definition_basis", ""),
            "comparability_class": comparability,
            "unit_contract": contract.get("unit_contract", ""),
            "document_sha256": document_sha256,
            "semantic_table_id": numerator_row.table_id,
            **source_context,
        },
    )


def derive_surface_strict_evidence(
    item: WorkItem,
    blocks: Sequence[SemanticBlock],
    *,
    requested_metrics: set[str],
    source_document: str,
    document_sha256: str,
    source_kind: str,
    source_contracts: Mapping[str, Mapping[str, str]],
    derivation_contracts: Sequence[Mapping[str, str]] = (),
    filing_profiles: Mapping[str, Mapping[str, str]] | None = None,
    document_extraction_method: str = "",
    document_extraction_warning: str = "",
    document_extraction_cache_status: str = "",
) -> tuple[MetricEvidence, ...]:
    output: list[MetricEvidence] = []
    ticker = item.filing.ticker.upper()
    profile = (filing_profiles or {}).get(ticker)
    for block in blocks:
        text = block.search_text
        metric_labels = (
            ("pricing_or_yield_growth", _LTL_YIELD_LABEL, "ReportedLtlYieldGrowth", "ltl_yield_growth"),
            ("shipment_or_load_growth", _LTL_SHIPMENT_LABEL, "ReportedLtlShipmentGrowth", "ltl_shipment_growth"),
        )
        for metric_id, label, concept_name, rule_id in metric_labels:
            if metric_id not in requested_metrics or metric_id not in source_contracts:
                continue
            parsed = _signed_growth_value(text, label)
            if parsed is None:
                continue
            output.append(
                _strict_surface_evidence(
                    item,
                    block,
                    metric_id=metric_id,
                    parsed=parsed,
                    value=parsed.value / 100.0,
                    unit="ratio",
                    concept_name=concept_name,
                    rule_id=rule_id,
                    source_document=source_document,
                    document_sha256=document_sha256,
                    source_kind=source_kind,
                    contract=source_contracts[metric_id],
                    filing_profile=profile,
                    document_extraction_method=document_extraction_method,
                    document_extraction_warning=document_extraction_warning,
                    document_extraction_cache_status=document_extraction_cache_status,
                )
            )
        if "fleet_or_equipment_count" in requested_metrics:
            count = _strict_power_unit_count(ticker, text)
            if count is not None:
                parsed, concept_name, rule_id = count
                output.append(
                    _strict_surface_evidence(
                        item,
                        block,
                        metric_id="fleet_or_equipment_count",
                        parsed=parsed,
                        value=parsed.value,
                        unit="count",
                        concept_name=concept_name,
                        rule_id=rule_id,
                        source_document=source_document,
                        document_sha256=document_sha256,
                        source_kind=source_kind,
                        contract=source_contracts["fleet_or_equipment_count"],
                        filing_profile=profile,
                        document_extraction_method=document_extraction_method,
                        document_extraction_warning=document_extraction_warning,
                        document_extraction_cache_status=document_extraction_cache_status,
                    )
                )

    by_table: defaultdict[int, list[SemanticBlock]] = defaultdict(list)
    for block in blocks:
        if block.kind == "table_row" and block.table_id is not None:
            by_table[int(block.table_id)].append(block)
    ratio_rules = (
        (
            "purchased_transportation_ratio",
            _PURCHASED_TRANSPORTATION_ROW,
            _CONSOLIDATED_REVENUE_ROW,
            "PurchasedTransportationAndRelatedServices",
            "TotalConsolidatedRevenues",
            "purchased_transportation/revenue",
        ),
        (
            "fuel_surcharge_revenue_ratio",
            _FUEL_SURCHARGE_ROW,
            _RAIL_REVENUE_ROW,
            "FuelSurchargeRevenue",
            "FreightRevenues",
            "fuel_surcharge_revenue/freight_revenue",
        ),
    )
    for rows in by_table.values():
        for contract in derivation_contracts:
            metric_id = str(contract.get("metric_id") or "")
            if metric_id not in requested_metrics or metric_id not in source_contracts:
                continue
            evidence = _surface_contract_evidence(
                item,
                rows,
                contract=contract,
                source_document=source_document,
                document_sha256=document_sha256,
                source_kind=source_kind,
                source_contract=source_contracts[metric_id],
                filing_profile=profile,
            )
            if evidence is not None:
                output.append(evidence)
        for metric_id, numerator, denominator, numerator_concept, denominator_concept, formula in ratio_rules:
            if metric_id not in requested_metrics or metric_id not in source_contracts:
                continue
            evidence = _strict_surface_ratio_evidence(
                item,
                rows,
                metric_id=metric_id,
                numerator_pattern=numerator,
                denominator_pattern=denominator,
                numerator_concept=numerator_concept,
                denominator_concept=denominator_concept,
                formula=formula,
                source_document=source_document,
                document_sha256=document_sha256,
                source_kind=source_kind,
                contract=source_contracts[metric_id],
                filing_profile=profile,
            )
            if evidence is not None:
                output.append(evidence)
    return tuple(output)


def derive_surface_table_evidence(
    item: WorkItem,
    blocks: Iterable[SemanticBlock],
    *,
    requested_metrics: set[str],
    source_document: str,
    document_sha256: str,
    source_kind: str,
    source_contracts: Mapping[str, Mapping[str, str]],
    derivation_contracts: Sequence[Mapping[str, str]] = (),
    filing_profiles: Mapping[str, Mapping[str, str]] | None = None,
    document_extraction_method: str = "",
    document_extraction_warning: str = "",
    document_extraction_cache_status: str = "",
) -> tuple[MetricEvidence, ...]:
    """Extract review-only surface KPIs from section-aware semantic tables."""

    semantic_blocks = tuple(blocks)
    output = list(
        derive_surface_strict_evidence(
            item,
            semantic_blocks,
            requested_metrics=requested_metrics,
            source_document=source_document,
            document_sha256=document_sha256,
            source_kind=source_kind,
            source_contracts=source_contracts,
            derivation_contracts=derivation_contracts,
            filing_profiles=filing_profiles,
            document_extraction_method=document_extraction_method,
            document_extraction_warning=document_extraction_warning,
            document_extraction_cache_status=document_extraction_cache_status,
        )
    )
    per_metric: defaultdict[str, int] = defaultdict(int)
    for evidence in output:
        per_metric[evidence.metric_name] += 1
    for block in semantic_blocks:
        if block.kind != "table_row" or not block.cells:
            continue
        if _NONISSUER.search(block.search_text):
            # Preserve one rejected observation when an actual metric/value is
            # found; do not emit an entire peer table merely because aliases hit.
            nonissuer = True
        else:
            nonissuer = False
        for metric_id in sorted(requested_metrics & set(source_contracts)):
            if per_metric[metric_id] >= 24:
                continue
            contract = source_contracts[metric_id]
            patterns = _aliases(contract)
            label_index = _row_label_index(block.cells, patterns)
            if label_index is None:
                continue
            label_text = normalize_space(block.cells[label_index])
            if len(label_text) > 160:
                # Malformed filing tables often place an entire MD&A
                # paragraph in one cell. Equipment words in that prose
                # are discovery anchors, not row labels. Broad prose
                # discovery still retains them for review.
                continue
            parsed = _value_after_label(block.cells, label_index, patterns)
            normalized = (
                _normalize_metric_value(
                    metric_id,
                    parsed,
                    context=block.search_text,
                )
                if parsed is not None
                else None
            )
            concept_name = "ReportedSurfaceOperatingKpi"
            reason = "reported_surface_kpi_requires_semantic_fixture_review"
            provenance: dict[str, object] = {"label_cell_index": label_index}
            if normalized is None and metric_id in _GROWTH_METRICS:
                year_values = _year_column_values(block)
                if len(year_values) >= 2:
                    latest_year, latest = year_values[0]
                    prior_year, prior = year_values[1]
                    if latest_year - prior_year == 1 and prior != 0:
                        derived = latest / prior - 1.0
                        if math.isfinite(derived) and -10.0 <= derived <= 10.0:
                            normalized = (derived, "ratio", {})
                            concept_name = "DerivedSurfaceYearOverYearGrowth"
                            reason = "derived_from_comparable_table_periods_requires_semantic_fixture_review"
                            provenance.update(
                                {
                                    "formula": "latest/prior-1",
                                    "latest_year": latest_year,
                                    "latest_value": latest,
                                    "prior_year": prior_year,
                                    "prior_value": prior,
                                }
                            )
            if normalized is None:
                continue
            value, unit, numeric_provenance = normalized
            provenance.update(numeric_provenance)
            provenance.update(
                {
                    "document_extraction_method": document_extraction_method,
                    "document_extraction_warning": document_extraction_warning,
                    "document_extraction_cache_status": document_extraction_cache_status,
                }
            )
            evidence = _table_evidence(
                item,
                metric_id=metric_id,
                value=value,
                unit=unit,
                block=block,
                source_document=source_document,
                document_sha256=document_sha256,
                source_kind=source_kind,
                contract=contract,
                filing_profile=(filing_profiles or {}).get(item.filing.ticker.upper()),
                concept_name=concept_name,
                reason=reason,
                extra_provenance=provenance,
            )
            if nonissuer and evidence.status != "REJECTED_POLICY":
                continue
            output.append(evidence)
            per_metric[metric_id] += 1
    return tuple(output)


def _fact_rule(
    metric_id: str,
    concept_name: str,
    rules_by_metric: Mapping[str, Sequence[Mapping[str, str]]],
) -> Mapping[str, str] | None:
    for rule in rules_by_metric.get(metric_id, ()):
        if re.search(str(rule.get("concept_pattern") or r"(?!)"), concept_name):
            return rule
    return None


def _fact_currency(fact: NormalizedFact) -> str:
    match = re.search(r"\b([A-Z]{3})\b", str(fact.unit or "").upper())
    return match.group(1) if match is not None else str(fact.unit or "").upper()


def _select_operand(
    facts: Sequence[tuple[NormalizedFact, Mapping[str, str]]],
) -> tuple[NormalizedFact, Mapping[str, str]] | None:
    if not facts:
        return None
    ranked = sorted(
        facts,
        key=lambda item: (
            int(str(item[1].get("priority") or 99)),
            item[0].concept_name,
            item[0].context_id,
        ),
    )
    best_priority = int(str(ranked[0][1].get("priority") or 99))
    best = [item for item in ranked if int(str(item[1].get("priority") or 99)) == best_priority]
    values = {float(item[0].numeric_value) for item in best if item[0].numeric_value is not None}
    if len(values) != 1:
        return None
    return best[0]


def derive_surface_xbrl_evidence(
    item: WorkItem,
    facts: Sequence[NormalizedFact],
    *,
    requested_metrics: set[str],
    rules_by_metric: Mapping[str, Sequence[Mapping[str, str]]],
) -> tuple[MetricEvidence, ...]:
    """Derive review-only ratios from same-period consolidated XBRL operands."""

    output: list[MetricEvidence] = []
    annual = item.filing.form_type.upper() in {"10-K", "10-K/A", "20-F", "20-F/A", "40-F", "40-F/A"}
    for metric_id in sorted(requested_metrics & {"operating_ratio", "purchased_transportation_ratio"}):
        grouped: defaultdict[
            tuple[str, str, str, str, str],
            defaultdict[str, list[tuple[NormalizedFact, Mapping[str, str]]]],
        ] = defaultdict(lambda: defaultdict(list))
        for fact in facts:
            if fact.numeric_value is None or fact.scope != "consolidated":
                continue
            rule = _fact_rule(metric_id, fact.concept_name, rules_by_metric)
            if rule is None:
                continue
            role = str(rule.get("operand_role") or "")
            if role == "direct_value":
                continue
            key = (
                fact.period_start,
                fact.period_end,
                _fact_currency(fact),
                fact.context_id,
                fact.source_document,
            )
            grouped[key][role].append((fact, rule))

        for (period_start, period_end, currency, context_id, source_document), roles in sorted(grouped.items()):
            revenue = _select_operand(roles.get("revenue", ()))
            if revenue is None or revenue[0].numeric_value is None or revenue[0].numeric_value <= 0:
                continue
            denominator = float(revenue[0].numeric_value)
            numerator: tuple[NormalizedFact, Mapping[str, str]] | None = None
            formula = ""
            broad_operand = False
            if metric_id == "operating_ratio":
                numerator = _select_operand(roles.get("operating_expense", ()))
                if numerator is not None and numerator[0].numeric_value is not None:
                    value = float(numerator[0].numeric_value) / denominator
                    formula = "operating_expense/revenue"
                else:
                    numerator = _select_operand(roles.get("operating_income", ()))
                    if numerator is None or numerator[0].numeric_value is None:
                        continue
                    value = 1.0 - float(numerator[0].numeric_value) / denominator
                    formula = "1-operating_income/revenue"
            else:
                numerator = _select_operand(roles.get("purchased_transportation", ()))
                if numerator is None:
                    numerator = _select_operand(roles.get("purchased_transportation_broad", ()))
                    broad_operand = numerator is not None
                if numerator is None or numerator[0].numeric_value is None:
                    continue
                value = float(numerator[0].numeric_value) / denominator
                formula = "purchased_transportation/revenue"
            if not math.isfinite(value) or value < 0 or value > (3.0 if metric_id == "operating_ratio" else 1.0):
                continue
            numerator_fact, numerator_rule = numerator
            reason = (
                "broad_contracted_services_operand_requires_note_confirmation"
                if broad_operand
                else "derived_from_audited_xbrl_operands_requires_definition_review"
            )
            confidence = 0.78 if broad_operand else 0.92 if annual else 0.86
            output.append(
                MetricEvidence(
                    metric_name=metric_id,
                    concept_name=(
                        "DerivedOperatingRatioFromXbrlOperands"
                        if metric_id == "operating_ratio"
                        else "DerivedPurchasedTransportationRatioFromXbrlOperands"
                    ),
                    value=value,
                    unit="ratio",
                    period_start=period_start,
                    period_end=period_end,
                    scope="consolidated",
                    confidence=confidence,
                    status="REVIEW_REQUIRED",
                    reason=reason,
                    evidence_text=(
                        f"{numerator_fact.taxonomy}:{numerator_fact.concept_name}="
                        f"{numerator_fact.numeric_value:g} {numerator_fact.unit}; "
                        f"{revenue[0].taxonomy}:{revenue[0].concept_name}="
                        f"{revenue[0].numeric_value:g} {revenue[0].unit}; formula={formula}"
                    ),
                    source_document=numerator_fact.source_document,
                    extraction_method="dedicated_parser:transportation_surface_xbrl_derivation_v1",
                    provenance={
                        "surface_parser_version": "transportation_surface_xbrl_v1",
                        "formula": formula,
                        "currency": currency,
                        "paired_context_id": context_id,
                        "paired_source_document": source_document,
                        "numerator_concept": numerator_fact.concept_name,
                        "numerator_context_id": numerator_fact.context_id,
                        "numerator_posture": numerator_rule.get("semantic_posture", ""),
                        "denominator_concept": revenue[0].concept_name,
                        "denominator_context_id": revenue[0].context_id,
                        "annual_form": annual,
                        "unit_contract": "ratio",
                    },
                )
            )
    return tuple(output)


def surface_fact_rule(
    metric_id: str,
    concept_name: str,
    rules_by_metric: Mapping[str, Sequence[Mapping[str, str]]],
) -> Mapping[str, str] | None:
    """Public adapter helper used to suppress raw operand-as-metric mapping."""

    return _fact_rule(metric_id, concept_name, rules_by_metric)
