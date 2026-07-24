from __future__ import annotations

import hashlib
import html as html_lib
import json
import math
import re
from dataclasses import asdict, dataclass, replace
from html.parser import HTMLParser
from typing import Any, Iterable


MODEL_FAMILY = "transportation"
EXTRACTION_METHOD = "transportation_sec_filing_prose_v2"
SOURCE_DETAIL = "sec_archive_transportation_specialized_metric"
ANNUAL_FORMS = frozenset(
    {
        "10-K",
        "10-K/A",
        "10-12B",
        "10-12B/A",
        "20-F",
        "20-F/A",
        "40-F",
        "40-F/A",
    }
)
INTERIM_FORMS = frozenset({"10-Q", "10-Q/A", "6-K", "6-K/A"})
SUPPORTED_METRICS_BY_COHORT = {
    "surface_freight_and_logistics": frozenset(
        {
            "transport_volume_growth",
            "pricing_or_yield_growth",
            "operating_ratio",
            "asset_utilization",
            "purchased_transportation_ratio",
        }
    ),
    "air_transport_and_aviation_services": frozenset(
        {
            "traffic_growth",
            "capacity_growth",
            "load_factor_or_utilization",
            "passenger_or_lease_yield",
            "fuel_or_maintenance_intensity",
        }
    ),
    "marine_shipping_and_maritime": frozenset(
        {
            "fleet_capacity",
            "tce_or_day_rate",
            "charter_coverage",
            "fleet_utilization",
            "fleet_age",
        }
    ),
    "development_stage_and_speculative_transport": frozenset(
        {"going_concern_flag", "commercialization_progress"}
    ),
}

_BLOCK_TAGS = frozenset(
    {
        "article",
        "br",
        "div",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "li",
        "p",
        "section",
        "table",
        "tr",
    }
)
_CELL_TAGS = frozenset({"td", "th"})
_NUMBER = r"(?P<value>\d{1,3}(?:,\d{3})*(?:\.\d+)?)"
_PERCENT = rf"{_NUMBER}\s*(?:%|percent\b)"
_DIRECTION = (
    r"(?P<direction>increased|grew|rose|improved|higher|decreased|declined|"
    r"fell|lower|contracted)"
)
_COMPARATIVE_PERIOD = re.compile(
    r"\b(?:"
    r"year[- ]over[- ]year|"
    r"over\s+the\s+same\s+prior[- ]year\s+period|"
    r"compared\s+(?:with|to)\s+(?:the\s+)?(?:same\s+)?"
    r"(?:prior|previous|year[- ]ago)(?:\s+year)?(?:\s+period|\s+quarter)?|"
    r"(?:first|second|third|fourth)\s+quarter\s+20\d{2}.*"
    r"(?:first|second|third|fourth)\s+quarter\s+20\d{2}|"
    r"(?:three|six|nine|twelve)\s+months?\s+ended.*"
    r"compared\s+(?:with|to).*"
    r"(?:three|six|nine|twelve)\s+months?\s+ended|"
    r"\bin\s+20\d{2}\s+(?:from|compared\s+(?:with|to))\s+20\d{2}"
    r")\b",
    re.IGNORECASE,
)
_NON_ISSUER_GROWTH_SCOPE = re.compile(
    r"\b(?:"
    r"global|worldwide|industry|market|IATA|"
    r"regional|region|Puerto\s+Rico|Colombi(?:a|an)|"
    r"Mexico\s+City|Fort\s+Lauderdale|transcontinental|"
    r"capacity\s+purchase|per\s+ASM|ASM\s+basis|"
    r"available\s+seat\s+miles?\s+per\s+gallon"
    r")\b",
    re.IGNORECASE,
)
_OPERATING_STATISTICS_CONTEXT = re.compile(
    r"\b(?:"
    r"(?:consolidated\s+)?(?:operating|traffic)\s+statistics|"
    r"(?:statistical|operating)\s+information.*(?:company|operations)|"
    r"(?:company(?:'s)?|consolidated)\s+operations"
    r").*"
    r"(?:"
    r"(?:three|six|nine|twelve)\s+months?\s+ended|"
    r"year\s+ended|years\s+ending"
    r")",
    re.IGNORECASE,
)
_TABLE_NUMBER = re.compile(r"(?<![A-Za-z])(?P<value>\(?-?\d[\d,]*(?:\.\d+)?\)?)")


@dataclass(frozen=True)
class TransportationDisclosureCandidate:
    concept_name: str
    metric_name: str
    value: float | None
    unit: str
    period_start: str
    period_end: str
    scope: str
    confidence: float
    candidate_status: str
    status_reason: str
    evidence_text: str
    block_index: int
    extraction_method: str = EXTRACTION_METHOD

    def payload_json(
        self,
        *,
        document_name: str,
        source_url: str = "",
        content_sha256: str = "",
        source_detail: str = SOURCE_DETAIL,
    ) -> str:
        return json.dumps(
            {
                "document": document_name,
                "source": source_detail,
                "source_url": source_url,
                "content_sha256": content_sha256,
                **asdict(self),
            },
            sort_keys=True,
            separators=(",", ":"),
        )


class _FilingTextParser(HTMLParser):
    _SEPARATOR = "\x1e"

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        del attrs
        normalized = tag.lower()
        if normalized in {"script", "style"}:
            self._skip_depth += 1
        elif self._skip_depth == 0 and normalized in _BLOCK_TAGS:
            self._parts.append(self._SEPARATOR)
        elif self._skip_depth == 0 and normalized in _CELL_TAGS:
            self._parts.append(" | ")

    def handle_endtag(self, tag: str) -> None:
        normalized = tag.lower()
        if normalized in {"script", "style"} and self._skip_depth > 0:
            self._skip_depth -= 1
        elif self._skip_depth == 0 and normalized in _BLOCK_TAGS:
            self._parts.append(self._SEPARATOR)
        elif self._skip_depth == 0 and normalized in _CELL_TAGS:
            self._parts.append(" | ")

    def handle_data(self, data: str) -> None:
        if self._skip_depth == 0:
            self._parts.append(data)

    def blocks(self) -> list[str]:
        raw = html_lib.unescape("".join(self._parts)).replace("\xa0", " ")
        output: list[str] = []
        seen: set[str] = set()
        for part in raw.split(self._SEPARATOR):
            normalized = " ".join(part.split()).strip(" |")
            if len(normalized) < 20:
                continue
            fragments = (
                re.split(r"(?<=[.;])\s+(?=[A-Z0-9])", normalized)
                if len(normalized) > 1_500
                else [normalized]
            )
            for fragment in fragments:
                cleaned = " ".join(fragment.split()).strip(" |")
                if 20 <= len(cleaned) <= 2_000 and cleaned not in seen:
                    seen.add(cleaned)
                    output.append(cleaned)
        return output


def filing_text_blocks(document_text: str) -> list[str]:
    parser = _FilingTextParser()
    try:
        parser.feed(document_text)
        parser.close()
        blocks = parser.blocks()
    except (AssertionError, ValueError):
        blocks = []
    if blocks:
        return blocks
    return [
        " ".join(line.split())
        for line in document_text.splitlines()
        if 20 <= len(" ".join(line.split())) <= 2_000
    ]


def _number(match: re.Match[str]) -> float | None:
    try:
        value = float(match.group("value").replace(",", ""))
    except (IndexError, TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def _signed_growth(match: re.Match[str]) -> float | None:
    value = _number(match)
    if value is None:
        return None
    direction = str(match.groupdict().get("direction") or "").lower()
    if direction in {"decreased", "declined", "fell", "lower", "contracted"}:
        value = -value
    return value / 100.0


def _candidate(
    *,
    metric_name: str,
    concept_name: str,
    value: float | None,
    unit: str,
    period_end: str,
    evidence: str,
    block_index: int,
    confidence: float,
    accepted: bool = True,
    reason: str = "explicit_issuer_reported_value",
) -> TransportationDisclosureCandidate:
    return TransportationDisclosureCandidate(
        concept_name=concept_name,
        metric_name=metric_name,
        value=value,
        unit=unit,
        period_start="",
        period_end=period_end,
        scope="issuer_reported",
        confidence=confidence,
        candidate_status="ACCEPTED" if accepted else "REVIEW_REQUIRED",
        status_reason=reason,
        evidence_text=evidence[:1_500],
        block_index=block_index,
    )


def _candidate_context(block: str, match: re.Match[str], *, radius: int = 280) -> str:
    start = max(0, match.start() - radius)
    end = min(len(block), match.end() + radius)
    return " ".join(block[start:end].split())


def _growth_is_period_aligned_and_issuer_level(
    block: str,
    match: re.Match[str],
) -> bool:
    context = _candidate_context(block, match, radius=120)
    if _NON_ISSUER_GROWTH_SCOPE.search(context):
        return False
    return _COMPARATIVE_PERIOD.search(context) is not None


def _growth_candidates(
    blocks: list[str],
    *,
    metric_name: str,
    concept_name: str,
    labels: str,
    period_end: str,
    resolve_period_alignment: bool = False,
) -> list[TransportationDisclosureCandidate]:
    pattern = re.compile(
        rf"\b(?:{labels})\b[^.;]{{0,55}}?\b{_DIRECTION}\b"
        rf"[^.;]{{0,25}}?(?:by\s+)?{_PERCENT}",
        re.IGNORECASE,
    )
    output: list[TransportationDisclosureCandidate] = []
    for index, block in enumerate(blocks):
        for match in pattern.finditer(block):
            value = _signed_growth(match)
            if value is None or not -1.0 <= value <= 2.0:
                continue
            aligned = (
                resolve_period_alignment
                and _growth_is_period_aligned_and_issuer_level(block, match)
            )
            output.append(
                _candidate(
                    metric_name=metric_name,
                    concept_name=concept_name,
                    value=value,
                    unit="ratio",
                    period_end=period_end,
                    evidence=(
                        _candidate_context(block, match)
                        if aligned
                        else match.group(0)
                    ),
                    block_index=index,
                    confidence=0.95 if aligned else 0.82,
                    accepted=aligned,
                    reason=(
                        "issuer_comparative_period_explicit"
                        if aligned
                        else "growth_period_alignment_requires_review"
                    ),
                )
            )
    return output


def _table_numbers(block: str) -> list[float]:
    output: list[float] = []
    for match in _TABLE_NUMBER.finditer(block):
        raw = match.group("value").replace(",", "")
        negative = raw.startswith("(") and raw.endswith(")")
        raw = raw.strip("()")
        try:
            value = float(raw)
        except ValueError:
            continue
        output.append(-value if negative else value)
    return output


def _operating_statistics_context(
    blocks: list[str],
    *,
    block_index: int,
) -> str:
    context = " ".join(blocks[max(0, block_index - 8) : block_index])
    match = _OPERATING_STATISTICS_CONTEXT.search(context)
    return match.group(0) if match is not None else ""


def _operating_statistics_duration_months(context: str) -> int:
    normalized = context.lower()
    for label, months in (
        ("three months ended", 3),
        ("six months ended", 6),
        ("nine months ended", 9),
        ("twelve months ended", 12),
        ("year ended", 12),
        ("years ending", 12),
    ):
        if label in normalized:
            return months
    return 99


def _air_operating_statistics_candidates(
    blocks: list[str],
    *,
    period_end: str,
) -> list[TransportationDisclosureCandidate]:
    row_rules = (
        (
            "traffic_growth",
            "TrafficGrowth",
            re.compile(
                r"^(?:revenue\s+passenger\s+miles?|RPMs?|"
                r"revenue\s+passenger\s+kilomet(?:er|re)s?|RPKs?)\b",
                re.IGNORECASE,
            ),
            "growth",
            "ratio",
        ),
        (
            "capacity_growth",
            "CapacityGrowth",
            re.compile(
                r"^(?:available\s+seat\s+miles?|ASMs?|"
                r"available\s+seat\s+kilomet(?:er|re)s?|ASKs?)\b",
                re.IGNORECASE,
            ),
            "growth",
            "ratio",
        ),
        (
            "load_factor_or_utilization",
            "LoadFactorOrUtilization",
            re.compile(r"^(?:passenger\s+)?load\s+factor\b", re.IGNORECASE),
            "level",
            "ratio",
        ),
        (
            "passenger_or_lease_yield",
            "PassengerYield",
            re.compile(
                r"^(?:average\s+)?(?:passenger\s+yield|yield(?:\s+per\s+"
                r"revenue\s+passenger\s+(?:mile|kilomet(?:er|re)))?)\b",
                re.IGNORECASE,
            ),
            "level",
            "cents_per_passenger_unit",
        ),
    )
    candidates: list[tuple[int, TransportationDisclosureCandidate]] = []
    for index, block in enumerate(blocks):
        context = _operating_statistics_context(blocks, block_index=index)
        if not context:
            continue
        for metric_name, concept_name, label, value_type, unit in row_rules:
            row_label = block.split("|", 1)[0].strip()
            if label.search(row_label) is None:
                continue
            values = _table_numbers(block)
            value_block = block
            if len(values) < 2 and index + 1 < len(blocks):
                following_values = _table_numbers(blocks[index + 1])
                if len(following_values) >= 2:
                    values = following_values
                    value_block = blocks[index + 1]
            if value_type == "growth":
                if len(values) < 3:
                    continue
                value = values[-1] / 100.0
                if not -1.0 <= value <= 2.0:
                    continue
                reason = "operating_statistics_current_vs_prior_period_percent_change"
            else:
                if len(values) < 2:
                    continue
                value = values[0] / 100.0 if unit == "ratio" else values[0]
                if unit == "ratio" and not 0.0 <= value <= 1.5:
                    continue
                if unit != "ratio" and not 0.0 < value < 500.0:
                    continue
                reason = "operating_statistics_current_period_level"
            candidates.append(
                (
                    _operating_statistics_duration_months(context),
                    _candidate(
                    metric_name=metric_name,
                    concept_name=concept_name,
                    value=value,
                    unit=unit,
                    period_end=period_end,
                    evidence=f"{context} | {row_label} | {value_block}",
                    block_index=index,
                    confidence=0.97,
                    reason=reason,
                    ),
                )
            )
    minimum_duration = {
        candidate.metric_name: min(
            duration
            for duration, item in candidates
            if item.metric_name == candidate.metric_name
        )
        for _, candidate in candidates
    }
    return [
        candidate
        for duration, candidate in candidates
        if duration == minimum_duration[candidate.metric_name]
    ]


def _percentage_level_candidates(
    blocks: list[str],
    *,
    metric_name: str,
    concept_name: str,
    labels: str,
    period_end: str,
    minimum: float = 0.0,
    maximum: float = 1.5,
    confidence: float = 0.92,
) -> list[TransportationDisclosureCandidate]:
    patterns = (
        re.compile(
            rf"\b(?:{labels})\b.{{0,25}}?\b(?:was|were|of|at|to|is|equaled|"
            rf"averaged|represented|stood\s+at|accounted\s+for)\b"
            rf".{{0,12}}?{_PERCENT}",
            re.IGNORECASE,
        ),
        re.compile(
            rf"{_PERCENT}.{{0,12}}?\b(?:{labels})\b",
            re.IGNORECASE,
        ),
    )
    output: list[TransportationDisclosureCandidate] = []
    for index, block in enumerate(blocks):
        for pattern in patterns:
            match = pattern.search(block)
            if match is None:
                continue
            raw = _number(match)
            value = raw / 100.0 if raw is not None else None
            if value is None or not minimum <= value <= maximum:
                continue
            output.append(
                _candidate(
                    metric_name=metric_name,
                    concept_name=concept_name,
                    value=value,
                    unit="ratio",
                    period_end=period_end,
                    evidence=match.group(0),
                    block_index=index,
                    confidence=confidence,
                )
            )
            break
    return output


def _surface_candidates(
    blocks: list[str],
    *,
    industry: str,
    period_end: str,
) -> list[TransportationDisclosureCandidate]:
    output = [
        *_growth_candidates(
            blocks,
            metric_name="transport_volume_growth",
            concept_name="TransportVolumeGrowth",
            labels=(
                r"shipment\s+volume|number\s+of\s+shipments?|total\s+shipments?|"
                r"load\s+volume|number\s+of\s+loads?|package\s+volume|"
                r"average\s+daily\s+package\s+volume|carloads?|"
                r"revenue\s+ton[- ]miles?|tonnage|intermodal\s+units?"
            ),
            period_end=period_end,
        ),
        *_growth_candidates(
            blocks,
            metric_name="pricing_or_yield_growth",
            concept_name="PricingOrYieldGrowth",
            labels=(
                r"yield|pricing|price\s+per\s+shipment|revenue\s+per\s+shipment|"
                r"revenue\s+per\s+load|rate\s+per\s+mile|revenue\s+per\s+hundredweight"
            ),
            period_end=period_end,
        ),
        *_percentage_level_candidates(
            blocks,
            metric_name="asset_utilization",
            concept_name="AssetUtilization",
            labels=r"asset\s+utilization|tractor\s+utilization|trailer\s+utilization|fleet\s+utilization",
            period_end=period_end,
            confidence=0.86,
        ),
    ]
    if industry in {"Railroads", "Trucking"}:
        output.extend(
            _percentage_level_candidates(
                blocks,
                metric_name="operating_ratio",
                concept_name="OperatingRatio",
                labels=r"operating\s+ratio",
                period_end=period_end,
                minimum=0.20,
                maximum=1.50,
                confidence=0.97,
            )
        )
    if industry in {"Integrated Freight & Logistics", "Trucking"}:
        output.extend(
            _percentage_level_candidates(
                blocks,
                metric_name="purchased_transportation_ratio",
                concept_name="PurchasedTransportationRatio",
                labels=(
                    r"purchased\s+transportation(?:\s+(?:costs?|expense))?"
                    r"(?:\s+as\s+a\s+percentage\s+of\s+(?:revenue|revenues))?"
                ),
                period_end=period_end,
                confidence=0.94,
            )
        )
    return output


def _air_candidates(
    blocks: list[str],
    *,
    period_end: str,
) -> list[TransportationDisclosureCandidate]:
    output = [
        *_growth_candidates(
            blocks,
            metric_name="traffic_growth",
            concept_name="TrafficGrowth",
            labels=(
                r"passenger\s+traffic|cargo\s+traffic|traffic\s+\(measured\s+in\s+(?:RPKs?|RTKs?)\)|"
                r"revenue\s+passenger\s+miles?|RPMs?|"
                r"revenue\s+passenger\s+kilomet(?:er|re)s?|RPKs?"
            ),
            period_end=period_end,
            resolve_period_alignment=True,
        ),
        *_growth_candidates(
            blocks,
            metric_name="capacity_growth",
            concept_name="CapacityGrowth",
            labels=(
                r"capacity|(?<!per\s)available\s+seat\s+miles?|ASMs?|"
                r"(?<!per\s)available\s+seat\s+kilomet(?:er|re)s?|ASKs?"
            ),
            period_end=period_end,
            resolve_period_alignment=True,
        ),
        *_percentage_level_candidates(
            blocks,
            metric_name="load_factor_or_utilization",
            concept_name="LoadFactorOrUtilization",
            labels=r"(?:passenger\s+)?load\s+factor|aircraft\s+utilization|lease\s+utilization",
            period_end=period_end,
            confidence=0.96,
        ),
        *_percentage_level_candidates(
            blocks,
            metric_name="fuel_or_maintenance_intensity",
            concept_name="FuelOrMaintenanceIntensity",
            labels=(
                r"(?:aircraft\s+)?fuel(?:\s+expense)?\s+as\s+a\s+percentage\s+of\s+(?:revenue|revenues)|"
                r"maintenance(?:\s+expense)?\s+as\s+a\s+percentage\s+of\s+(?:revenue|revenues)"
            ),
            period_end=period_end,
            confidence=0.94,
        ),
    ]
    output.extend(_air_operating_statistics_candidates(blocks, period_end=period_end))
    percentage_yield = _percentage_level_candidates(
        blocks,
        metric_name="passenger_or_lease_yield",
        concept_name="LeaseYield",
        labels=r"lease\s+yield|net\s+spread",
        period_end=period_end,
        confidence=0.92,
    )
    output.extend(percentage_yield)
    cents_pattern = re.compile(
        rf"\b(?:passenger\s+yield|yield\s+per\s+passenger\s+mile)\b"
        rf".{{0,80}}?{_NUMBER}\s*(?:cents?|\u00a2)",
        re.IGNORECASE,
    )
    for index, block in enumerate(blocks):
        match = cents_pattern.search(block)
        value = _number(match) if match is not None else None
        if value is None or not 0.0 < value < 500.0:
            continue
        output.append(
            _candidate(
                metric_name="passenger_or_lease_yield",
                concept_name="PassengerYield",
                value=value,
                unit="cents_per_passenger_unit",
                period_end=period_end,
                evidence=block,
                block_index=index,
                confidence=0.93,
            )
        )
    return output


def _marine_candidates(
    blocks: list[str],
    *,
    period_end: str,
) -> list[TransportationDisclosureCandidate]:
    output = [
        *_percentage_level_candidates(
            blocks,
            metric_name="charter_coverage",
            concept_name="CharterCoverage",
            labels=(
                r"charter\s+coverage|contracted\s+coverage|available\s+days?\s+(?:were\s+)?"
                r"(?:fixed|contracted)|revenue\s+days?\s+(?:were\s+)?contracted"
            ),
            period_end=period_end,
            confidence=0.91,
        ),
        *_percentage_level_candidates(
            blocks,
            metric_name="fleet_utilization",
            concept_name="FleetUtilization",
            labels=r"(?:fleet|vessel)\s+utilization|commercial\s+utilization",
            period_end=period_end,
            confidence=0.96,
        ),
    ]
    fleet_pattern = re.compile(
        rf"\b(?:(?:(?:our|the\s+company(?:'s)?)\s+fleet)\s+"
        rf"(?:consisted|comprised|included|includes|consists)\s+of"
        rf"|we\s+operate(?:d|s)?\s+(?:a\s+fleet\s+of\s+)?)"
        rf".{{0,45}}?{_NUMBER}\s+(?:owned\s+|operating\s+|commercial\s+)?"
        rf"(?:vessels?|ships?)\b",
        re.IGNORECASE,
    )
    tce_pattern = re.compile(
        rf"\b(?:time\s+charter\s+equivalent|TCE)(?:\s+(?:rate|revenue))?"
        rf".{{0,80}}?(?:US\$|USD|\$)?\s*{_NUMBER}\s*(?:per\s+day|/\s*day)\b",
        re.IGNORECASE,
    )
    age_pattern = re.compile(
        rf"\b(?:average\s+(?:age\s+of\s+(?:our|the)\s+fleet|fleet\s+age)|"
        rf"(?:our|the)\s+fleet\s+had\s+an\s+average\s+age)\b"
        rf".{{0,70}}?{_NUMBER}\s+years?\b",
        re.IGNORECASE,
    )
    for index, block in enumerate(blocks):
        for metric_name, concept_name, pattern, unit, minimum, maximum, confidence in (
            (
                "fleet_capacity",
                "FleetVesselCount",
                fleet_pattern,
                "vessels",
                1.0,
                10_000.0,
                0.94,
            ),
            (
                "tce_or_day_rate",
                "TimeCharterEquivalentPerDay",
                tce_pattern,
                "USD_per_day",
                100.0,
                1_000_000.0,
                0.97,
            ),
            ("fleet_age", "AverageFleetAge", age_pattern, "years", 0.0, 100.0, 0.96),
        ):
            match = pattern.search(block)
            if match is None:
                continue
            value = _number(match)
            if value is None or not minimum <= value <= maximum:
                continue
            output.append(
                _candidate(
                    metric_name=metric_name,
                    concept_name=concept_name,
                    value=value,
                    unit=unit,
                    period_end=period_end,
                    evidence=match.group(0),
                    block_index=index,
                    confidence=confidence,
                )
            )
    return output


def _development_candidates(
    blocks: list[str],
    *,
    period_end: str,
) -> list[TransportationDisclosureCandidate]:
    output: list[TransportationDisclosureCandidate] = []
    going_concern = re.compile(
        r"\b(?:there\s+(?:is|exists)\s+substantial\s+doubt|"
        r"(?:conditions?|events?)\s+(?:raise|raised)\s+substantial\s+doubt|"
        r"substantial\s+doubt\s+(?:exists|remains))"
        r".{0,180}?\b(?:(?:our|the\s+company(?:'s)?|its)\s+)?"
        r"(?:ability\s+to\s+)?(?:continue|continuing)\s+as\s+a\s+going\s+concern\b|"
        r"\bsubstantial\s+doubt\s+(?:about|regarding)\s+(?:our|the\s+company(?:'s)?)"
        r"\s+ability\s+to\s+continue\s+as\s+a\s+going\s+concern\b",
        re.IGNORECASE,
    )
    milestones = re.compile(
        r"\b(?:type\s+certification|FAA\s+certification|entered\s+commercial\s+service|"
        r"commercial\s+operations?|first\s+commercial\s+(?:flight|delivery)|"
        r"production\s+prototype|flight[- ]test(?:ing)?|regulatory\s+approval)\b",
        re.IGNORECASE,
    )
    for index, block in enumerate(blocks):
        if going_concern.search(block):
            evidence_match = going_concern.search(block)
            assert evidence_match is not None
            output.append(
                _candidate(
                    metric_name="going_concern_flag",
                    concept_name="GoingConcernSubstantialDoubt",
                    value=1.0,
                    unit="boolean",
                    period_end=period_end,
                    evidence=evidence_match.group(0),
                    block_index=index,
                    confidence=0.99,
                    reason="explicit_substantial_doubt_going_concern_disclosure",
                )
            )
        if milestones.search(block):
            output.append(
                _candidate(
                    metric_name="commercialization_progress",
                    concept_name="CommercializationMilestone",
                    value=None,
                    unit="normalized_score",
                    period_end=period_end,
                    evidence=block,
                    block_index=index,
                    confidence=0.75,
                    accepted=False,
                    reason="milestone_detected_requires_policy_scoring_review",
                )
            )
    return output


def _deduplicate(
    candidates: Iterable[TransportationDisclosureCandidate],
) -> list[TransportationDisclosureCandidate]:
    ranked = sorted(
        candidates,
        key=lambda item: (
            item.metric_name,
            0 if item.candidate_status == "ACCEPTED" else 1,
            -item.confidence,
            item.block_index,
        ),
    )
    seen: set[tuple[str, str, str]] = set()
    output: list[TransportationDisclosureCandidate] = []
    per_metric: dict[str, int] = {}
    for candidate in ranked:
        value_key = "" if candidate.value is None else f"{candidate.value:.10g}"
        evidence_key = hashlib.sha256(candidate.evidence_text.encode("utf-8")).hexdigest()[:16]
        key = (candidate.metric_name, value_key, evidence_key)
        if key in seen or per_metric.get(candidate.metric_name, 0) >= 8:
            continue
        seen.add(key)
        per_metric[candidate.metric_name] = per_metric.get(candidate.metric_name, 0) + 1
        output.append(candidate)
    accepted_values: dict[str, set[tuple[str, str]]] = {}
    for candidate in output:
        if candidate.candidate_status != "ACCEPTED":
            continue
        value_key = "" if candidate.value is None else f"{candidate.value:.10g}"
        accepted_values.setdefault(candidate.metric_name, set()).add(
            (candidate.unit, value_key)
        )
    conflicting_metrics = {
        metric for metric, values in accepted_values.items() if len(values) > 1
    }
    return [
        replace(
            candidate,
            candidate_status="REVIEW_REQUIRED",
            status_reason="multiple_values_in_document_require_period_or_scope_resolution",
            confidence=min(candidate.confidence, 0.80),
        )
        if candidate.metric_name in conflicting_metrics
        and candidate.candidate_status == "ACCEPTED"
        else candidate
        for candidate in output
    ]


def extract_transportation_disclosure_candidates(
    document_text: str,
    *,
    filing: dict[str, Any],
    cohort: str,
    industry: str,
) -> list[TransportationDisclosureCandidate]:
    blocks = filing_text_blocks(document_text)
    period_end = str(filing.get("report_date") or filing.get("filing_date") or "")[:10]
    if cohort == "surface_freight_and_logistics":
        candidates = _surface_candidates(blocks, industry=industry, period_end=period_end)
    elif cohort == "air_transport_and_aviation_services":
        candidates = _air_candidates(blocks, period_end=period_end)
    elif cohort == "marine_shipping_and_maritime":
        candidates = _marine_candidates(blocks, period_end=period_end)
    elif cohort == "development_stage_and_speculative_transport":
        candidates = _development_candidates(blocks, period_end=period_end)
    else:
        return []
    supported = SUPPORTED_METRICS_BY_COHORT.get(cohort, frozenset())
    return _deduplicate(
        candidate for candidate in candidates if candidate.metric_name in supported
    )


def candidate_key(
    *,
    ticker: str,
    source_id: str,
    accession_number: str,
    document_name: str,
    candidate: TransportationDisclosureCandidate,
) -> str:
    value = "" if candidate.value is None else f"{candidate.value:.12g}"
    evidence_hash = hashlib.sha256(candidate.evidence_text.encode("utf-8")).hexdigest()
    payload = "|".join(
        (
            ticker,
            source_id,
            accession_number,
            document_name,
            candidate.metric_name,
            candidate.concept_name,
            candidate.period_start,
            candidate.period_end,
            value,
            evidence_hash,
        )
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def upsert_transportation_disclosure_candidates(
    conn: Any,
    *,
    ticker: str,
    cik: str,
    source_id: str,
    filing: dict[str, Any],
    document_name: str,
    source_url: str,
    content_sha256: str,
    candidates: Iterable[TransportationDisclosureCandidate],
    now: str,
) -> int:
    count = 0
    accession = str(filing.get("accession_number") or "").strip()
    for candidate in candidates:
        key = candidate_key(
            ticker=ticker,
            source_id=source_id,
            accession_number=accession,
            document_name=document_name,
            candidate=candidate,
        )
        conn.execute(
            """
            INSERT INTO fact_sec_metric_disclosure_candidate(
                candidate_key, ticker, cik, source_id, model_family,
                accession_number, form_type, filing_date, accepted_at,
                document_name, metric_name, concept_name, candidate_value,
                unit, period_start, period_end, scope, extraction_method,
                confidence, candidate_status, status_reason, evidence_text,
                provenance_json, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(candidate_key) DO UPDATE SET
                candidate_value=excluded.candidate_value,
                unit=excluded.unit,
                period_start=excluded.period_start,
                period_end=excluded.period_end,
                scope=excluded.scope,
                confidence=excluded.confidence,
                candidate_status=excluded.candidate_status,
                status_reason=excluded.status_reason,
                evidence_text=excluded.evidence_text,
                provenance_json=excluded.provenance_json,
                updated_at=excluded.updated_at
            """,
            (
                key,
                ticker,
                cik,
                source_id,
                MODEL_FAMILY,
                accession,
                str(filing.get("form_type") or ""),
                str(filing.get("filing_date") or ""),
                str(filing.get("accepted_at") or ""),
                document_name,
                candidate.metric_name,
                candidate.concept_name,
                candidate.value,
                candidate.unit,
                candidate.period_start,
                candidate.period_end,
                candidate.scope,
                candidate.extraction_method,
                candidate.confidence,
                candidate.candidate_status,
                candidate.status_reason,
                candidate.evidence_text,
                candidate.payload_json(
                    document_name=document_name,
                    source_url=source_url,
                    content_sha256=content_sha256,
                ),
                now,
                now,
            ),
        )
        count += 1
    return count
