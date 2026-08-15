from __future__ import annotations

import hashlib
import re
from collections import defaultdict
from datetime import date
from typing import Iterable

from dedicated_parser.contracts import MetricEvidence, WorkItem
from dedicated_parser.semantic import SemanticBlock, normalize_space


_NUMBER = re.compile(r"(?<![A-Za-z0-9])\(?\s*([-+]?\d[\d,]*(?:\.\d+)?)\s*\)?")
_YEAR = re.compile(r"\b(?:19|20)\d{2}\b")
_YEAR_HEADER = re.compile(r"\b(?:year\s+built|build\s+year|built)\b", re.IGNORECASE)
_CAPACITY_HEADER = re.compile(
    r"\b(?:dwt|dead\s*weight|deadweight|carrying\s+capacity|capacity)\b",
    re.IGNORECASE,
)
_REVENUE_DAYS = re.compile(
    r"\b(?:total\s+)?(?:revenue|earning)\s+days\b",
    re.IGNORECASE,
)
_AVAILABLE_DAYS = re.compile(
    r"\b(?:total\s+)?available\s+days\b",
    re.IGNORECASE,
)
_OFFHIRE_DAYS = re.compile(
    r"\b(?:technical\s+)?off[ -]?hire\s+days\b|\b(?:scheduled\s+)?dry[ -]?dock(?:ing)?\s+days\b",
    re.IGNORECASE,
)
_FIXED_DAYS = re.compile(
    r"\b(?:fixed(?:[ -]?rate)?|contracted|covered)\b.{0,40}\b(?:revenue|earning|available)?\s*days\b",
    re.IGNORECASE,
)

_DIRECT_ROW_RULES = {
    "fleet_capacity": (
        re.compile(r"\b(?:total|aggregate)\s+(?:fleet\s+)?(?:carrying\s+)?capacity(?:\s*\(?(?:dwt|deadweight)\)?)?\b", re.I),
        "ReportedAggregateFleetCapacity",
        "segment_native_capacity",
    ),
    "revenue_days": (
        re.compile(r"\b(?:total\s+)?(?:revenue|earning)\s+days\b", re.I),
        "ReportedRevenueDays",
        "days",
    ),
    "tce_day_rate": (
        re.compile(r"\b(?:average\s+daily\s+)?(?:time\s+charter\s+equivalent|tce)(?:\s+(?:rate|earnings))?(?:\s+per\s+day)?\b", re.I),
        "ReportedTceDayRate",
        "currency_per_day",
    ),
    "fleet_age": (
        re.compile(r"\b(?:weighted\s+)?average\s+(?:fleet|vessel)\s+age\b", re.I),
        "ReportedAverageFleetAge",
        "years",
    ),
    "vessel_count": (
        re.compile(r"\b(?:total\s+)?(?:number\s+of\s+)?(?:owned\s+|operating\s+)?(?:fleet|vessels?)\b", re.I),
        "ReportedOperatingVesselCount",
        "count",
    ),
    "vessel_opex_per_day": (
        re.compile(r"\b(?:daily\s+)?vessel\s+operating\s+(?:expenses?|costs?)(?:\s+per\s+day)?\b|\bopex\s+per\s+day\b", re.I),
        "ReportedVesselOpexPerDay",
        "currency_per_day",
    ),
    "spot_or_charter_day_rate": (
        re.compile(r"\b(?:spot(?:\s+market)?|time\s+charter|pool)\s+(?:tce\s+)?rate(?:\s+per\s+day)?\b", re.I),
        "ReportedSpotOrCharterDayRate",
        "currency_per_day",
    ),
    "fleet_utilization": (
        re.compile(r"\b(?:fleet|commercial)\s+utili[sz]ation(?:\s+rate)?\b", re.I),
        "ReportedFleetUtilization",
        "ratio",
    ),
    "charter_coverage_next_12m": (
        re.compile(r"\b(?:contract(?:ed)?|fixed(?:[- ]rate)?)\s+coverage(?:\s+next\s+12\s+months?)?\b|\bavailable\s+days\s+fixed\b", re.I),
        "ReportedForwardCharterCoverage",
        "ratio",
    ),
    "weighted_average_charter_term": (
        re.compile(r"\b(?:weighted\s+)?average\s+remaining\s+(?:charter|lease)\s+(?:duration|term)\b", re.I),
        "ReportedWeightedAverageCharterTerm",
        "years",
    ),
    "cash_breakeven_per_day": (
        re.compile(r"\b(?:estimated\s+daily\s+|free\s+cash\s+flow\s+)?cash\s+breakeven(?:\s+per\s+day)?\b", re.I),
        "ReportedCashBreakevenPerDay",
        "currency_per_day",
    ),
    "spot_exposure_ratio": (
        re.compile(r"\b(?:percent(?:age)?\s+)?spot(?:\s+market)?\s+exposure\b", re.I),
        "ReportedSpotExposure",
        "ratio",
    ),
}


def _number(value: str) -> float | None:
    match = _NUMBER.search(value)
    if match is None:
        return None
    try:
        parsed = float(match.group(1).replace(",", ""))
    except ValueError:
        return None
    if "(" in match.group(0) and ")" in match.group(0):
        return -abs(parsed)
    return parsed


def _header_index(headers: tuple[str, ...], pattern: re.Pattern[str]) -> int | None:
    for index, header in enumerate(headers):
        if pattern.search(normalize_space(header)):
            return index
    return None


def _table_headers(rows: list[SemanticBlock]) -> tuple[str, ...]:
    for row in rows:
        if row.header_cells:
            return row.header_cells
    for row in rows:
        if _header_index(row.cells, _YEAR_HEADER) is not None:
            return row.cells
    return ()


def _row_value(
    rows: Iterable[SemanticBlock],
    pattern: re.Pattern[str],
) -> tuple[float, SemanticBlock] | None:
    for row in rows:
        cells = row.cells or (row.text,)
        for index, cell in enumerate(cells):
            if pattern.search(normalize_space(cell)) is None:
                continue
            for candidate in cells[index + 1 :]:
                value = _number(candidate)
                if value is not None:
                    return value, row
            # Plain-text tables sometimes collapse the label and first value
            # into one cell. Remove the matched label before reading a value.
            residual = pattern.sub(" ", cell)
            value = _number(residual)
            if value is not None:
                return value, row
    return None


def _direct_row_value(
    rows: Iterable[SemanticBlock],
    pattern: re.Pattern[str],
) -> tuple[float, str, SemanticBlock] | None:
    for row in rows:
        cells = row.cells or (row.text,)
        for index, cell in enumerate(cells):
            label = normalize_space(cell)
            if len(label) > 180 or pattern.search(label) is None:
                continue
            for candidate in cells[index + 1 :]:
                value = _number(candidate)
                if value is not None:
                    return value, normalize_space(candidate), row
            residual = pattern.sub(" ", cell)
            value = _number(residual)
            if value is not None:
                return value, normalize_space(residual), row
    return None


def _normalize_direct_value(
    metric_name: str,
    value: float,
    raw: str,
    context: str,
) -> float | None:
    combined = f"{raw} {context}"
    percent = bool(re.search(r"%|\bpercent(?:age)?\b", combined, re.I))
    if metric_name in {
        "fleet_utilization",
        "charter_coverage_next_12m",
        "spot_exposure_ratio",
    }:
        if re.search(r"\b(?:increase|increased|decrease|decreased|change|changed)\b", combined, re.I):
            return None
        if percent:
            value /= 100.0
        return value if 0.0 <= value <= 1.0 else None
    if metric_name in {"tce_day_rate", "vessel_opex_per_day", "spot_or_charter_day_rate", "cash_breakeven_per_day"}:
        return value if 100.0 <= value <= 500_000.0 and not percent else None
    if metric_name == "revenue_days":
        return value if 1.0 <= value <= 250_000.0 and not percent else None
    if metric_name == "fleet_capacity":
        return value if value >= 1_000.0 and not percent else None
    if metric_name == "vessel_count":
        return value if 1.0 <= value <= 2_000.0 and value.is_integer() and not percent else None
    if metric_name == "fleet_age":
        return value if 0.0 <= value <= 60.0 and not percent else None
    if metric_name == "weighted_average_charter_term":
        return value if 0.0 <= value <= 30.0 and not percent else None
    return None


def _direct_table_evidence(
    item: WorkItem,
    rows: list[SemanticBlock],
    *,
    requested_metrics: set[str],
    source_document: str,
    document_sha256: str,
    table_id: int,
) -> tuple[MetricEvidence, ...]:
    output: list[MetricEvidence] = []
    table_text = " | ".join(row.search_text for row in rows)
    for metric_name, (pattern, concept_name, unit) in _DIRECT_ROW_RULES.items():
        if metric_name not in requested_metrics:
            continue
        found = _direct_row_value(rows, pattern)
        if found is None:
            continue
        raw_value, raw_text, row = found
        value = _normalize_direct_value(metric_name, raw_value, raw_text, row.search_text)
        if value is None:
            continue
        output.append(
            _evidence(
                item,
                metric_name=metric_name,
                concept_name=concept_name,
                value=value,
                unit=unit,
                reason="strict_reported_tanker_table_row_requires_semantic_replay",
                source_document=source_document,
                document_sha256=document_sha256,
                table_id=table_id,
                evidence_text=row.search_text,
                provenance={
                    "strict_rule_id": f"{metric_name}_exact_table_row_v2",
                    "raw_value_text": raw_text,
                    "unit_contract": unit,
                    "semantic_row_index": row.row_index,
                    "table_context_sha256": hashlib.sha256(
                        table_text.encode("utf-8")
                    ).hexdigest(),
                },
            )
        )
    return tuple(output)


def _evidence(
    item: WorkItem,
    *,
    metric_name: str,
    concept_name: str,
    value: float,
    unit: str,
    reason: str,
    source_document: str,
    document_sha256: str,
    table_id: int,
    evidence_text: str,
    provenance: dict[str, object],
) -> MetricEvidence:
    return MetricEvidence(
        metric_name=metric_name,
        concept_name=concept_name,
        value=value,
        unit=unit,
        period_start="",
        period_end=item.filing.report_date[:10],
        scope="unknown",
        confidence=0.82,
        status="REVIEW_REQUIRED",
        reason=reason,
        evidence_text=evidence_text[:2000],
        source_document=source_document,
        extraction_method="dedicated_parser:transportation_table_derivation",
        provenance={
            "derivation_version": "transportation_tanker_tables_v2",
            "document_sha256": document_sha256,
            "filing_form_type": item.filing.form_type,
            "semantic_table_id": table_id,
            **provenance,
        },
    )


def derive_tanker_table_evidence(
    item: WorkItem,
    blocks: Iterable[SemanticBlock],
    *,
    requested_metrics: set[str],
    source_document: str,
    document_sha256: str,
) -> tuple[MetricEvidence, ...]:
    """Derive review-only tanker metrics from internally consistent tables.

    These candidates deliberately remain review-only.  The function expands
    discovery without weakening the later semantic-fixture acceptance gate.
    """

    by_table: dict[int, list[SemanticBlock]] = defaultdict(list)
    for block in blocks:
        if block.kind == "table_row" and block.table_id is not None:
            by_table[int(block.table_id)].append(block)

    output: list[MetricEvidence] = []
    report_year = date.fromisoformat(item.filing.report_date[:10]).year
    for table_id, rows in sorted(by_table.items()):
        table_text = " | ".join(row.search_text for row in rows)
        if re.search(r"\b(?:competitor|peer\s+group|pro\s+forma)\b", table_text, re.IGNORECASE):
            continue
        output.extend(
            _direct_table_evidence(
                item,
                rows,
                requested_metrics=requested_metrics,
                source_document=source_document,
                document_sha256=document_sha256,
                table_id=table_id,
            )
        )

        if "fleet_age" in requested_metrics:
            headers = _table_headers(rows)
            built_index = _header_index(headers, _YEAR_HEADER)
            capacity_index = _header_index(headers, _CAPACITY_HEADER)
            weighted_rows: list[tuple[int, float, str]] = []
            if built_index is not None and capacity_index is not None:
                for row in rows:
                    if len(row.cells) <= max(built_index, capacity_index):
                        continue
                    year_match = _YEAR.search(row.cells[built_index])
                    capacity = _number(row.cells[capacity_index])
                    if year_match is None or capacity is None or capacity <= 0:
                        continue
                    built_year = int(year_match.group(0))
                    if not 1970 <= built_year <= report_year:
                        continue
                    identity = row.cells[0] if row.cells else f"row-{row.row_index}"
                    weighted_rows.append((built_year, capacity, identity))
            unique_rows = {
                (built_year, capacity, identity): (built_year, capacity, identity)
                for built_year, capacity, identity in weighted_rows
            }
            weighted_rows = list(unique_rows.values())
            if len(weighted_rows) >= 2:
                total_capacity = sum(capacity for _, capacity, _ in weighted_rows)
                weighted_age = sum(
                    (report_year - built_year) * capacity
                    for built_year, capacity, _ in weighted_rows
                ) / total_capacity
                output.append(
                    _evidence(
                        item,
                        metric_name="fleet_age",
                        concept_name="DerivedDwtWeightedFleetAge",
                        value=weighted_age,
                        unit="years",
                        reason="derived_from_vessel_year_built_and_capacity_requires_fixture_review",
                        source_document=source_document,
                        document_sha256=document_sha256,
                        table_id=table_id,
                        evidence_text=(
                            f"DWT-weighted fleet age from {len(weighted_rows)} vessel rows; "
                            f"headers={' | '.join(headers)}"
                        ),
                        provenance={
                            "operand_count": len(weighted_rows),
                            "total_capacity": total_capacity,
                            "weighting_basis": "DWT_or_table_capacity",
                        },
                    )
                )

        available = _row_value(rows, _AVAILABLE_DAYS)
        offhire = _row_value(rows, _OFFHIRE_DAYS)
        direct_revenue = _row_value(rows, _REVENUE_DAYS)
        if available is not None and offhire is not None:
            available_value, available_row = available
            offhire_value, offhire_row = offhire
            if 0 <= offhire_value <= available_value and available_value > 0:
                if "revenue_days" in requested_metrics and direct_revenue is None:
                    output.append(
                        _evidence(
                            item,
                            metric_name="revenue_days",
                            concept_name="DerivedRevenueDaysFromAvailableLessOffhire",
                            value=available_value - offhire_value,
                            unit="days",
                            reason="derived_from_available_days_less_offhire_requires_fixture_review",
                            source_document=source_document,
                            document_sha256=document_sha256,
                            table_id=table_id,
                            evidence_text=(
                                f"Available days {available_value} less off-hire/drydock days "
                                f"{offhire_value}."
                            ),
                            provenance={
                                "available_days": available_value,
                                "available_row_index": available_row.row_index,
                                "offhire_days": offhire_value,
                                "offhire_row_index": offhire_row.row_index,
                            },
                        )
                    )
                if "offhire_or_drydock_ratio" in requested_metrics:
                    output.append(
                        _evidence(
                            item,
                            metric_name="offhire_or_drydock_ratio",
                            concept_name="DerivedOffhireDaysToAvailableDays",
                            value=offhire_value / available_value,
                            unit="ratio",
                            reason="derived_from_offhire_and_available_days_requires_fixture_review",
                            source_document=source_document,
                            document_sha256=document_sha256,
                            table_id=table_id,
                            evidence_text=(
                                f"Off-hire/drydock days {offhire_value} divided by available "
                                f"days {available_value}."
                            ),
                            provenance={
                                "available_days": available_value,
                                "offhire_days": offhire_value,
                            },
                        )
                    )

        if "charter_coverage_next_12m" in requested_metrics:
            fixed = _row_value(rows, _FIXED_DAYS)
            denominator = available or direct_revenue
            if fixed is not None and denominator is not None:
                fixed_value, fixed_row = fixed
                denominator_value, denominator_row = denominator
                if 0 <= fixed_value <= denominator_value and denominator_value > 0:
                    output.append(
                        _evidence(
                            item,
                            metric_name="charter_coverage_next_12m",
                            concept_name="DerivedFixedDaysCoverageRatio",
                            value=fixed_value / denominator_value,
                            unit="ratio",
                            reason="derived_from_fixed_and_available_days_requires_fixture_review",
                            source_document=source_document,
                            document_sha256=document_sha256,
                            table_id=table_id,
                            evidence_text=(
                                f"Fixed/contracted days {fixed_value} divided by available/earning "
                                f"days {denominator_value}."
                            ),
                            provenance={
                                "fixed_days": fixed_value,
                                "fixed_row_index": fixed_row.row_index,
                                "denominator_days": denominator_value,
                                "denominator_row_index": denominator_row.row_index,
                            },
                        )
                    )

    return tuple(output)
