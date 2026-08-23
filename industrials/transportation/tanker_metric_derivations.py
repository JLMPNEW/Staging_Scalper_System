from __future__ import annotations

import hashlib
import re
from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import Iterable

from dedicated_parser.contracts import MetricEvidence, WorkItem
from dedicated_parser.semantic import SemanticBlock, normalize_space


_NUMBER = re.compile(r"(?<![A-Za-z0-9])\(?\s*([-+]?\d[\d,]*(?:\.\d+)?)\s*\)?")
_YEAR = re.compile(r"\b(?:19|20)\d{2}\b")
_YEAR_HEADER = re.compile(r"\b(?:year\s+built|build\s+year|built)\b", re.IGNORECASE)
_VESSEL_HEADER = re.compile(r"\b(?:vessel|ship)(?:\s+name)?\b", re.IGNORECASE)
_EMPLOYMENT_HEADER = re.compile(r"\b(?:employment|charter\s+type|contract\s+type|market|deployment)\b", re.IGNORECASE)
_EXPIRATION_HEADER = re.compile(r"\b(?:expiry|expiration|redelivery|charter\s+end|contract\s+end)\b", re.IGNORECASE)
_CAPACITY_HEADER = re.compile(
    r"\b(?:dwt|dead\s*weight|deadweight|carrying\s+capacity|capacity)\b",
    re.IGNORECASE,
)
_REVENUE_DAYS = re.compile(
    r"\b(?:total\s+)?(?:revenue|earning|net\s+earnings|earnings\s+capacity|available\s+earning)\s+days\b",
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
_OPERATING_DAYS = re.compile(r"\b(?:total\s+)?operating\s+days\b", re.IGNORECASE)
_VESSEL_OPERATING_EXPENSE = re.compile(r"\b(?:total\s+)?vessel\s+operating\s+(?:expenses?|costs?)\b", re.IGNORECASE)

_DIRECT_ROW_RULES = {
    "fleet_capacity": (
        re.compile(r"\b(?:total|aggregate)\s+(?:fleet\s+)?(?:carrying\s+)?capacity(?:\s*\(?(?:dwt|deadweight)\)?)?\b|\b(?:total|aggregate)\s+(?:fleet\s+)?dwt\b|\bfleet\s+deadweight\s+tons?\b", re.I),
        "ReportedAggregateFleetCapacity",
        "segment_native_capacity",
    ),
    "revenue_days": (
        re.compile(r"\b(?:total\s+)?(?:revenue|earning|net\s+earnings|earnings\s+capacity|available\s+earning)\s+days\b", re.I),
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
        re.compile(r"\b(?:fleet\s+size|(?:average\s+)?number\s+of\s+vessels|total\s+operating\s+fleet|owned\s+and\s+operated\s+fleet|owned\s+fleet)\b", re.I),
        "ReportedOperatingVesselCount",
        "count",
    ),
    "vessel_opex_per_day": (
        re.compile(r"\b(?:daily\s+)?vessel\s+operating\s+(?:expenses?|costs?)(?:\s+per\s+(?:operating\s+)?day)?\b|\bopex\s+per\s+day\b|\boperating\s+expenses\s+per\s+operating\s+day\b", re.I),
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
        re.compile(r"\b(?:contract(?:ed)?|fixed(?:[- ]rate)?|time\s+charter)\s+coverage(?:\s+next\s+12\s+months?)?\b|\b(?:available|earning)\s+days\s+(?:fixed|covered)\b|\bpercentage\s+covered\b", re.I),
        "ReportedForwardCharterCoverage",
        "ratio",
    ),
    "weighted_average_charter_term": (
        re.compile(r"\b(?:weighted\s+)?average\s+remaining\s+(?:charter|lease)\s+(?:duration|term)\b", re.I),
        "ReportedWeightedAverageCharterTerm",
        "years",
    ),
    "cash_breakeven_per_day": (
        re.compile(r"\b(?:(?:estimated\s+average\s+)?daily\s+|free\s+cash\s+flow\s+|operational?\s+cash\s+flow\s+|all[- ]in\s+)?cash\s+break[- ]?even(?:\s+(?:rate|per\s+day))?\b", re.I),
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


def _reported_definition_basis(metric_name: str, text: str) -> str:
    if metric_name == "cash_breakeven_per_day":
        if re.search(r"\ball[- ]in\b", text, re.I):
            return "all_in_cash_breakeven"
        if re.search(r"\boperational?\s+cash\s+flow\b", text, re.I):
            return "operating_cash_flow_breakeven"
        return "issuer_defined_cash_breakeven"
    if metric_name == "revenue_days":
        return "issuer_reported_revenue_or_earning_days"
    if metric_name == "vessel_opex_per_day":
        return "issuer_reported_daily_vessel_opex"
    return "issuer_reported_exact_label"


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
                    "definition_basis": _reported_definition_basis(
                        metric_name, row.search_text
                    ),
                },
            )
        )
    return tuple(output)


def _date_from_cell(value: str) -> date | None:
    text = normalize_space(value)
    for fmt in ("%Y-%m-%d", "%B %d, %Y", "%b %d, %Y", "%B %Y", "%b %Y"):
        try:
            parsed = datetime.strptime(text, fmt).date()
            if fmt in {"%B %Y", "%b %Y"}:
                return date(parsed.year, parsed.month, 1)
            return parsed
        except ValueError:
            pass
    quarter = re.search(r"\bQ([1-4])\s+(20\d{2})\b", text, re.IGNORECASE)
    if quarter:
        return date(int(quarter.group(2)), int(quarter.group(1)) * 3, 1)
    return None


def _vessel_schedule(
    rows: list[SemanticBlock],
    *,
    report_date: date,
) -> list[dict[str, object]]:
    headers = _table_headers(rows)
    vessel_index = _header_index(headers, _VESSEL_HEADER)
    built_index = _header_index(headers, _YEAR_HEADER)
    dwt_index = _header_index(headers, _CAPACITY_HEADER)
    employment_index = _header_index(headers, _EMPLOYMENT_HEADER)
    expiry_index = _header_index(headers, _EXPIRATION_HEADER)
    if vessel_index is None and built_index is None and dwt_index is None:
        return []
    schedule: dict[str, dict[str, object]] = {}
    for row in rows:
        if not row.cells or row.cells == headers:
            continue
        identity_index = vessel_index if vessel_index is not None else 0
        if identity_index >= len(row.cells):
            continue
        identity = normalize_space(row.cells[identity_index])
        if not identity or re.search(r"\b(?:total|average|fleet)\b", identity, re.IGNORECASE):
            continue
        built_year: int | None = None
        if built_index is not None and built_index < len(row.cells):
            match = _YEAR.search(row.cells[built_index])
            if match:
                candidate = int(match.group(0))
                if 1970 <= candidate <= report_date.year:
                    built_year = candidate
        dwt: float | None = None
        if dwt_index is not None and dwt_index < len(row.cells):
            dwt = _number(row.cells[dwt_index])
            if dwt is not None and dwt <= 0:
                dwt = None
        employment = normalize_space(row.cells[employment_index]) if employment_index is not None and employment_index < len(row.cells) else ""
        expiry = _date_from_cell(row.cells[expiry_index]) if expiry_index is not None and expiry_index < len(row.cells) else None
        if built_year is None and dwt is None and not employment and expiry is None:
            continue
        schedule[identity.casefold()] = {
            "identity": identity, "built_year": built_year, "dwt": dwt,
            "employment": employment, "expiry": expiry, "row_index": row.row_index,
        }
    return list(schedule.values())


def _scaled_monetary_row_value(
    rows: list[SemanticBlock],
    pattern: re.Pattern[str],
) -> tuple[float, SemanticBlock] | None:
    found = _row_value(rows, pattern)
    if found is None:
        return None
    value, row = found
    table_text = " | ".join(candidate.search_text for candidate in rows)
    if re.search(r"\b(?:in\s+)?thousands\b", table_text, re.IGNORECASE):
        value *= 1_000.0
    elif re.search(r"\b(?:in\s+)?millions\b", table_text, re.IGNORECASE):
        value *= 1_000_000.0
    return value, row


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
    report_date = date.fromisoformat(item.filing.report_date[:10])
    report_year = report_date.year
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

        schedule = _vessel_schedule(rows, report_date=report_date)
        if len(schedule) >= 2:
            if "vessel_count" in requested_metrics:
                output.append(
                    _evidence(
                        item, metric_name="vessel_count",
                        concept_name="DerivedVesselCountFromSchedule",
                        value=float(len(schedule)), unit="count",
                        reason="derived_from_unique_vessel_schedule_rows_requires_fixture_review",
                        source_document=source_document,
                        document_sha256=document_sha256, table_id=table_id,
                        evidence_text=f"Unique issuer vessel rows={len(schedule)}; {table_text}",
                        provenance={"operand_count": len(schedule), "identity_basis": "normalized_unique_vessel_name"},
                    )
                )
            dwt_rows = [row for row in schedule if row.get("dwt") is not None]
            if "fleet_capacity" in requested_metrics and len(dwt_rows) >= 2:
                total_dwt = sum(float(row["dwt"]) for row in dwt_rows)
                output.append(
                    _evidence(
                        item, metric_name="fleet_capacity",
                        concept_name="DerivedFleetCapacityFromVesselSchedule",
                        value=total_dwt, unit="segment_native_capacity",
                        reason="derived_from_vessel_schedule_dwt_requires_fixture_review",
                        source_document=source_document,
                        document_sha256=document_sha256, table_id=table_id,
                        evidence_text=f"Summed DWT from {len(dwt_rows)} vessel rows; {table_text}",
                        provenance={"operand_count": len(dwt_rows), "total_capacity": total_dwt, "capacity_basis": "DWT"},
                    )
                )
            age_rows = [row for row in schedule if row.get("built_year") is not None]
            if "fleet_age" in requested_metrics and len(age_rows) >= 2:
                weighted = [row for row in age_rows if row.get("dwt") is not None]
                if len(weighted) == len(age_rows):
                    total_capacity = sum(float(row["dwt"]) for row in weighted)
                    fleet_age = sum((report_year - int(row["built_year"])) * float(row["dwt"]) for row in weighted) / total_capacity
                    concept, basis = "DerivedDwtWeightedFleetAge", "DWT_or_table_capacity"
                else:
                    total_capacity = 0.0
                    fleet_age = sum(report_year - int(row["built_year"]) for row in age_rows) / len(age_rows)
                    concept, basis = "DerivedSimpleAverageFleetAge", "simple_average_year_built"
                output.append(
                    _evidence(
                        item, metric_name="fleet_age", concept_name=concept,
                        value=fleet_age, unit="years",
                        reason="derived_from_vessel_year_built_requires_fixture_review",
                        source_document=source_document,
                        document_sha256=document_sha256, table_id=table_id,
                        evidence_text=f"Fleet age from {len(age_rows)} vessel rows; {table_text}",
                        provenance={"operand_count": len(age_rows), "total_capacity": total_capacity, "weighting_basis": basis},
                    )
                )
            if "charter_coverage_next_12m" in requested_metrics:
                horizon = report_date + timedelta(days=365)
                fixed = re.compile(r"\b(?:time\s+charter|fixed|contracted|tc)\b", re.I)
                spot = re.compile(r"\b(?:spot|pool)\b", re.I)
                fixed_rows = [row for row in schedule if fixed.search(str(row.get("employment") or "")) and not spot.search(str(row.get("employment") or "")) and isinstance(row.get("expiry"), date)]
                covered_days = sum(max(0, (min(horizon, row["expiry"]) - report_date).days) for row in fixed_rows)
                available_days = len(schedule) * 365
                if fixed_rows and 0 <= covered_days <= available_days:
                    output.append(
                        _evidence(
                            item, metric_name="charter_coverage_next_12m",
                            concept_name="DerivedForwardCharterCoverageFromVesselSchedule",
                            value=covered_days / available_days, unit="ratio",
                            reason="derived_from_vessel_charter_expiry_schedule_requires_fixture_review",
                            source_document=source_document,
                            document_sha256=document_sha256, table_id=table_id,
                            evidence_text=f"Contracted vessel-days={covered_days}; available vessel-days={available_days}; {table_text}",
                            provenance={"fixed_vessel_count": len(fixed_rows), "vessel_count": len(schedule), "contracted_days": covered_days, "available_days": available_days, "coverage_start_date": report_date.isoformat(), "coverage_end_date": horizon.isoformat(), "denominator_basis": "all_schedule_vessels_x_365"},
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

        if "fleet_utilization" in requested_metrics:
            direct_util = _row_value(rows, _REVENUE_DAYS)
            available_util = _row_value(rows, _AVAILABLE_DAYS)
            if direct_util and available_util:
                revenue_value, revenue_row = direct_util
                available_value, available_row = available_util
                if available_value > 0 and 0 <= revenue_value <= available_value:
                    output.append(
                        _evidence(
                            item, metric_name="fleet_utilization",
                            concept_name="DerivedFleetUtilizationFromDays",
                            value=revenue_value / available_value, unit="ratio",
                            reason="derived_from_revenue_and_available_days_requires_fixture_review",
                            source_document=source_document,
                            document_sha256=document_sha256, table_id=table_id,
                            evidence_text=f"Revenue/earning days {revenue_value} divided by available days {available_value}.",
                            provenance={"revenue_days": revenue_value, "revenue_row_index": revenue_row.row_index, "available_days": available_value, "available_row_index": available_row.row_index, "denominator_basis": "available_days"},
                        )
                    )

        if "vessel_opex_per_day" in requested_metrics:
            expense = _scaled_monetary_row_value(rows, _VESSEL_OPERATING_EXPENSE)
            operating = _row_value(rows, _OPERATING_DAYS)
            if expense and operating:
                expense_value, expense_row = expense
                operating_value, operating_row = operating
                value = expense_value / operating_value if operating_value > 0 else 0.0
                if 100 <= value <= 500_000:
                    output.append(
                        _evidence(
                            item, metric_name="vessel_opex_per_day",
                            concept_name="DerivedVesselOpexPerOperatingDay",
                            value=value, unit="currency_per_day",
                            reason="derived_from_vessel_opex_and_operating_days_requires_fixture_review",
                            source_document=source_document,
                            document_sha256=document_sha256, table_id=table_id,
                            evidence_text=f"Vessel operating expense {expense_value} divided by operating days {operating_value}.",
                            provenance={"vessel_operating_expense": expense_value, "expense_row_index": expense_row.row_index, "operating_days": operating_value, "operating_row_index": operating_row.row_index, "denominator_basis": "operating_days"},
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
