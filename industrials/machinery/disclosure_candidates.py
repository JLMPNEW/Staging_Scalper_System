from __future__ import annotations

import hashlib
import html as html_lib
import json
import re
from calendar import monthrange
from dataclasses import asdict, dataclass, replace
from datetime import date, datetime
from html.parser import HTMLParser
from typing import Any, Iterable


PROSE_SOURCE_DETAIL = "sec_archive_prose_metric"
SUPPORTED_PROSE_CONCEPTS = frozenset(
    {
        "Orders",
        "FundedBacklog",
        "ReportedBacklog",
        "RemainingPerformanceObligation",
    }
)
PROSE_CONCEPT_MAPPINGS = {
    "Orders": ("orders", "orders", "duration", 200),
    "FundedBacklog": ("funded_backlog", "backlog", "instant", 200),
    "ReportedBacklog": ("reported_backlog", "backlog", "instant", 200),
    "RemainingPerformanceObligation": (
        "remaining_performance_obligation",
        "backlog",
        "instant",
        180,
    ),
}

_PERIODIC_FORMS = frozenset({"10-K", "10-K/A", "10-Q", "10-Q/A"})
_SPECIAL_DISCLOSURE_METRICS = frozenset(
    {
        "funded_backlog",
        "orders",
        "remaining_performance_obligation",
        "reported_backlog",
        "rpo_current",
    }
)
_RPO_MASTER_TICKERS = frozenset({"NPO", "NVT", "WAB"})
_VERIFIED_CONSOLIDATED_CONCEPTS = {
    "ETN": frozenset({"ReportedBacklog"}),
    "JCI": frozenset({"RemainingPerformanceObligation"}),
    "OTIS": frozenset({"RemainingPerformanceObligation"}),
    "PH": frozenset({"ReportedBacklog"}),
    "POWL": frozenset({"ReportedBacklog"}),
}
_GTLS_APPROVED_ORDERS = {
    ("2025-09-30", 1_680_400_000.0),
    ("2026-03-31", 1_280_300_000.0),
}
_MAIR_FINAL_PROSPECTUS_ACCESSION = "0001193125-26-160250"
_MAIR_INFORMATION_BARRIER = "2026-04-15"
_CXT_SEPARATION_DATE = "2023-04-03"
_MAIR_FINAL_PROSPECTUS_VALUES = {
    ("ReportedBacklog", "", "2024-12-31", 987_400_000.0),
    ("ReportedBacklog", "", "2025-12-31", 2_160_800_000.0),
    ("Orders", "2024-01-01", "2024-12-31", 3_428_100_000.0),
    ("Orders", "2024-10-01", "2024-12-31", 721_800_000.0),
    ("Orders", "2025-01-01", "2025-12-31", 4_530_800_000.0),
    ("Orders", "2025-10-01", "2025-12-31", 1_653_400_000.0),
}

_BLOCK_TAGS = frozenset({"article", "br", "div", "h1", "h2", "h3", "h4", "h5", "h6", "li", "p", "section"})
_MONTHS = {
    "jan": 1,
    "january": 1,
    "feb": 2,
    "february": 2,
    "mar": 3,
    "march": 3,
    "apr": 4,
    "april": 4,
    "may": 5,
    "jun": 6,
    "june": 6,
    "jul": 7,
    "july": 7,
    "aug": 8,
    "august": 8,
    "sep": 9,
    "sept": 9,
    "september": 9,
    "oct": 10,
    "october": 10,
    "nov": 11,
    "november": 11,
    "dec": 12,
    "december": 12,
}
_MONTH_PATTERN = "|".join(sorted(_MONTHS, key=len, reverse=True))
_DATE_PATTERN = rf"(?P<month>{_MONTH_PATTERN})\.?\s+(?P<day>\d{{1,2}}),?\s+(?P<year>(?:19|20)\d{{2}})"
_MONTH_END_PATTERN = (
    rf"(?P<end_month>{_MONTH_PATTERN})\.?\s+(?P<end_year>(?:19|20)\d{{2}})"
)
_YEAR_END_PATTERN = r"(?P<year_end>(?:19|20)\d{2})"
_MONEY_PATTERN = (
    r"(?P<currency>U\.S\.\s*\$|US\$|USD\s*|CA\$|C\$|CAD\s*|"
    r"AU\$|A\$|AUD\s*|EUR\s*|\u20ac|GBP\s*|\u00a3|CHF\s*|\$)\s*"
    r"(?P<number>\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)\s*"
    r"(?P<scale>billions?|millions?|thousands?|bn|mm|[bmk])?\b"
)


@dataclass(frozen=True)
class DisclosureCandidate:
    concept_name: str
    metric_name: str
    value: float
    unit: str
    period_start: str
    period_end: str
    scope: str
    confidence: float
    candidate_status: str
    status_reason: str
    evidence_text: str
    block_index: int
    extraction_method: str = "filing_html_prose"

    def payload_json(
        self,
        *,
        document_name: str,
        source_detail: str = PROSE_SOURCE_DETAIL,
    ) -> str:
        return json.dumps(
            {"document": document_name, "source": source_detail, **asdict(self)},
            sort_keys=True,
            separators=(",", ":"),
        )


class _NarrativeBlockParser(HTMLParser):
    _BLOCK_SEPARATOR = "\x1e"

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []
        self._table_depth = 0
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        del attrs
        tag = tag.lower()
        if tag in {"script", "style"}:
            self._skip_depth += 1
        elif tag == "table":
            self._table_depth += 1
        elif tag in _BLOCK_TAGS and self._table_depth == 0 and self._skip_depth == 0:
            self._parts.append(self._BLOCK_SEPARATOR)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in {"script", "style"} and self._skip_depth > 0:
            self._skip_depth -= 1
        elif tag == "table" and self._table_depth > 0:
            self._table_depth -= 1
            self._parts.append(self._BLOCK_SEPARATOR)
        elif tag in _BLOCK_TAGS and self._table_depth == 0 and self._skip_depth == 0:
            self._parts.append(self._BLOCK_SEPARATOR)

    def handle_data(self, data: str) -> None:
        if self._table_depth == 0 and self._skip_depth == 0:
            self._parts.append(data)

    def blocks(self) -> list[str]:
        text = html_lib.unescape("".join(self._parts)).replace("\xa0", " ")
        blocks: list[str] = []
        for raw in text.split(self._BLOCK_SEPARATOR):
            normalized = " ".join(raw.split()).strip()
            if normalized and (not blocks or normalized != blocks[-1]):
                blocks.append(normalized)
        return blocks


def narrative_blocks(document_text: str) -> list[str]:
    parser = _NarrativeBlockParser()
    try:
        parser.feed(document_text)
        parser.close()
    except (ValueError, AssertionError):
        return []
    return parser.blocks()


def _parse_date(match: re.Match[str]) -> str:
    groups = match.groupdict()
    if not all(groups.get(name) for name in ("month", "day", "year")):
        return ""
    month = _MONTHS[str(groups["month"]).lower().rstrip(".")]
    try:
        return date(int(str(groups["year"])), month, int(str(groups["day"]))).isoformat()
    except ValueError:
        return ""


def _disclosure_period_end(match: re.Match[str], *, report_date: str = "") -> str:
    evidence = match.group(0)
    dates = list(re.finditer(_DATE_PATTERN, evidence, re.IGNORECASE))
    if len(dates) == 1:
        return _parse_date(match)
    if dates:
        money_offset = match.start("number") - match.start(0)
        nearest = min(
            dates,
            key=lambda item: min(
                abs(item.start() - money_offset),
                abs(item.end() - money_offset),
            ),
        )
        return _parse_date(nearest)
    groups = match.groupdict()
    if groups.get("end_month") and groups.get("end_year"):
        month = _MONTHS[str(groups["end_month"]).lower().rstrip(".")]
        year = int(str(groups["end_year"]))
        return date(year, month, monthrange(year, month)[1]).isoformat()
    if groups.get("year_end"):
        return date(int(str(groups["year_end"])), 12, 31).isoformat()
    if groups.get("prior_year") and report_date:
        try:
            anchor = date.fromisoformat(report_date)
            return anchor.replace(year=anchor.year - 1).isoformat()
        except ValueError:
            return ""
    return ""


def _period_start(period_end: str, evidence: str, *, duration_metric: bool) -> str:
    if not duration_metric or not period_end:
        return ""
    end = date.fromisoformat(period_end)
    normalized = evidence.lower()
    if re.search(r"\bthree\s+months?\s+ended\b|\bquarter\s+ended\b", normalized):
        months = 3
    elif re.search(r"\bsix\s+months?\s+ended\b", normalized):
        months = 6
    elif re.search(r"\bnine\s+months?\s+ended\b", normalized):
        months = 9
    elif re.search(r"\b(?:year|twelve\s+months?)\s+ended\b|\bfiscal\s+year\b", normalized):
        months = 12
    else:
        return ""
    month_index = end.year * 12 + end.month - months
    return date(month_index // 12, month_index % 12 + 1, 1).isoformat()


def _money(match: re.Match[str], *, company_currency: str) -> tuple[float, str, float]:
    value = float(match.group("number").replace(",", ""))
    scale_name = str(match.group("scale") or "").lower()
    scale = 1.0
    if scale_name.startswith("billion") or scale_name in {"b", "bn"}:
        scale = 1_000_000_000.0
    elif scale_name.startswith("million") or scale_name in {"m", "mm"}:
        scale = 1_000_000.0
    elif scale_name.startswith("thousand") or scale_name == "k":
        scale = 1_000.0
    currency_marker = re.sub(r"[.\s]", "", str(match.group("currency") or "").upper())
    if currency_marker in {"US$", "USD"}:
        unit = "USD"
        explicit_currency = True
    elif currency_marker in {"C$", "CA$", "CAD"}:
        unit = "CAD"
        explicit_currency = True
    elif currency_marker in {"A$", "AU$", "AUD"}:
        unit = "AUD"
        explicit_currency = True
    elif currency_marker in {"EUR", "\u20ac"}:
        unit = "EUR"
        explicit_currency = True
    elif currency_marker in {"GBP", "\u00a3"}:
        unit = "GBP"
        explicit_currency = True
    elif currency_marker == "CHF":
        unit = "CHF"
        explicit_currency = True
    else:
        # A bare dollar sign follows the issuer's reporting currency. Treating
        # every "$" as USD corrupts Canadian filing disclosures such as ATS.
        unit = str(company_currency or "").strip().upper()
        explicit_currency = False
    confidence = (
        0.95
        if explicit_currency and scale_name
        else 0.88
        if explicit_currency
        else 0.90
        if unit and scale_name
        else 0.80
        if unit
        else 0.60
    )
    return value * scale, unit, confidence


def _scope(evidence: str, context: str) -> str:
    normalized = f"{context} {evidence}".lower()
    if re.search(
        r"\b(?:consolidated|overall,?|company(?:'s)?|our)\s+(?:order\s+)?backlog\b",
        normalized,
    ):
        return "consolidated"
    if "consolidated results" in normalized or "consolidated operations" in normalized:
        return "consolidated"
    if re.search(r"\b(?:segment|business unit|equipment group|division)\b", context.lower()):
        return "segment"
    return "unknown"


def _canonical_metric(concept_name: str) -> str:
    return {
        "Orders": "orders",
        "FundedBacklog": "funded_backlog",
        "ReportedBacklog": "reported_backlog",
        "RemainingPerformanceObligation": "remaining_performance_obligation",
    }[concept_name]


def _concept_from_label(label: str) -> str:
    normalized = " ".join(label.lower().split())
    if "remaining performance obligation" in normalized:
        return "RemainingPerformanceObligation"
    if any(
        label in normalized
        for label in ("funded backlog", "authorized backlog", "appropriated backlog")
    ):
        return "FundedBacklog"
    if "backlog" in normalized or "unfilled open orders" in normalized:
        return "ReportedBacklog"
    return "Orders"


def _candidate_patterns() -> tuple[re.Pattern[str], ...]:
    flags = re.IGNORECASE
    funded_backlog_label = (
        r"(?P<label>(?:funded|authorized|appropriated)\s+(?:order\s+)?backlog)"
    )
    backlog_label = (
        r"(?P<label>(?:(?:total|overall|order|reported|funded|firm|consolidated)\s+)?"
        r"backlog|unfilled\s+open\s+orders(?:\s+for\s+the\s+next\s+six\s+months)?)"
    )
    rpo_label = r"(?P<label>remaining\s+performance\s+obligations?)"
    order_label = (
        r"(?P<label>(?:(?:total|consolidated)\s+)?"
        r"(?:new\s+)?(?:orders|bookings)(?:\s+received)?)"
    )
    date = _DATE_PATTERN
    month_end = _MONTH_END_PATTERN
    year_end = _YEAR_END_PATTERN
    money = _MONEY_PATTERN
    return (
        re.compile(
            rf"(?P<label>overall,?\s+backlog)\s+"
            rf"(?:increased|decreased|rose|declined).{{0,100}}?\bto\s+"
            rf"(?:approximately\s+|about\s+)?{money}.{{0,80}}?"
            rf"(?:\s+as\s+of|\s+at)\s+{date}",
            flags,
        ),
        re.compile(rf"{backlog_label}\s+(?:of|was|were|stood\s+at|totaled|totalled|was\s+valued\s+at)\s+(?:approximately\s+|about\s+)?{money}(?:\s+as\s+of|\s+at)\s+{date}", flags),
        re.compile(
            rf"{backlog_label}\s+"
            rf"(?:of|was|were|stood\s+at|totaled|totalled|was\s+valued\s+at)\s+"
            rf"(?:approximately\s+|about\s+)?{money}"
            rf"(?:(?:\.(?=\d))|[^.;]){{0,200}}?"
            rf"(?:\s+as\s+of|\s+at)\s+{date}",
            flags,
        ),
        re.compile(rf"{backlog_label}\s+(?:as\s+of|at)\s+{date}\s+(?:was|were|stood\s+at|totaled|totalled|was\s+valued\s+at)\s+(?:approximately\s+|about\s+)?{money}", flags),
        re.compile(rf"(?:dollar\s+amount\s+of\s+)?{backlog_label}\s+(?:as\s+of|at)\s+{date}\s+(?:was|were)\s+(?:approximately\s+|about\s+)?{money}", flags),
        re.compile(rf"(?:as\s+of|at)\s+{date}[^.;]{{0,120}}?{backlog_label}\s+(?:of|was|were|totaled|totalled)\s+(?:approximately\s+|about\s+)?{money}", flags),
        re.compile(
            rf"{backlog_label}\s+(?:of|was|were|stood\s+at|totaled|totalled)\s+"
            rf"(?:approximately\s+|about\s+)?{money}\s+ending\s+{month_end}",
            flags,
        ),
        re.compile(
            rf"(?:at\s+the\s+end\s+of|year[-\s]+end)\s+{year_end}"
            rf"[^.;]{{0,160}}?{backlog_label}\s+"
            rf"(?:of|was|were|stood\s+at|totaled|totalled)\s+"
            rf"(?:approximately\s+|about\s+)?{money}",
            flags,
        ),
        re.compile(
            rf"(?P<prior_year>(?:prior|previous)\s+year(?:[-\s]+end(?:ing)?)?)\s+"
            rf"{backlog_label}\s+(?:of|was|were|stood\s+at|totaled|totalled)\s+"
            rf"(?:approximately\s+|about\s+)?{money}",
            flags,
        ),
        # Date-bearing patterns must run before these fallbacks. Otherwise a
        # filing with current and comparative amounts assigns both to report_date.
        re.compile(
            rf"{funded_backlog_label}\s+(?:was|stood\s+at|totaled|totalled|was\s+valued\s+at)\s+"
            rf"(?:approximately\s+|about\s+)?{money}",
            flags,
        ),
        re.compile(
            rf"{backlog_label}\s+(?:was|stood\s+at|totaled|totalled|was\s+valued\s+at)\s+"
            rf"(?:approximately\s+|about\s+)?{money}",
            flags,
        ),
        re.compile(rf"{rpo_label}.{{0,180}}?(?:was|were|totaled|totalled)\s+(?:approximately\s+|about\s+)?{money}.{{0,100}}?(?:as\s+of|at)\s+{date}", flags),
        re.compile(rf"(?:as\s+of|at)\s+{date}.{{0,180}}?{rpo_label}.{{0,100}}?(?:was|were|totaled|totalled)\s+(?:approximately\s+|about\s+)?{money}", flags),
        re.compile(rf"{order_label}\s+for\s+the\s+(?:year|(?:three|six|nine|twelve)\s+months?)\s+ended\s+{date}\s+(?:of|were|was|totaled|totalled|totaling)\s+(?:approximately\s+|about\s+)?{money}", flags),
        re.compile(rf"{order_label}[^.;]{{0,80}}?(?:of|were|was|totaled|totalled|totaling)\s+(?:approximately\s+|about\s+)?{money}[^.;]{{0,120}}?(?:for\s+the\s+)?(?:year|(?:(?:three|six|nine|twelve)\s+)?months?)\s+ended\s+{date}", flags),
        re.compile(rf"(?:for\s+the\s+)?(?:year|(?:(?:three|six|nine|twelve)\s+)?months?)\s+ended\s+{date}.{{0,180}}?{order_label}.{{0,80}}?(?:of|were|was|totaled|totalled|totaling)\s+(?:approximately\s+|about\s+)?{money}", flags),
    )


_PATTERNS = _candidate_patterns()


def _rank_candidates(candidates: list[DisclosureCandidate]) -> list[DisclosureCandidate]:
    grouped: dict[tuple[str, str, str, str], list[DisclosureCandidate]] = {}
    for candidate in candidates:
        grouped.setdefault(
            (
                candidate.concept_name,
                candidate.period_start,
                candidate.period_end,
                candidate.unit,
            ),
            [],
        ).append(candidate)
    output: list[DisclosureCandidate] = []
    for grouped_candidates in grouped.values():
        unique: dict[float, DisclosureCandidate] = {}
        scope_rank = {"consolidated": 2, "unknown": 1, "segment": 0}
        for item in grouped_candidates:
            value_key = round(item.value, 6)
            previous = unique.get(value_key)
            if previous is None or (
                scope_rank.get(item.scope, 0), item.confidence
            ) > (
                scope_rank.get(previous.scope, 0), previous.confidence
            ):
                unique[value_key] = item
        values = list(unique.values())
        accepted: DisclosureCandidate | None = None
        consolidated = [item for item in values if item.scope == "consolidated"]
        if len(consolidated) == 1:
            accepted = consolidated[0]
        elif len(values) == 1:
            accepted = values[0]
        elif len(values) >= 3:
            largest = max(values, key=lambda item: item.value)
            component_sum = sum(item.value for item in values if item is not largest)
            if largest.value > 0 and abs(largest.value - component_sum) / largest.value <= 0.08:
                accepted = largest
        for candidate in values:
            complete_period = bool(
                candidate.period_end
                and candidate.unit
                and (candidate.concept_name != "Orders" or candidate.period_start)
            )
            if candidate is accepted and complete_period:
                output.append(
                    DisclosureCandidate(
                        **{
                            **asdict(candidate),
                            "candidate_status": "ACCEPTED",
                            "status_reason": "explicit_consolidated_prose_value",
                            "confidence": min(candidate.confidence, 0.85),
                        }
                    )
                )
            else:
                reason = "ambiguous_multiple_scope_values" if len(values) > 1 else "missing_period_or_currency"
                output.append(
                    DisclosureCandidate(
                        **{
                            **asdict(candidate),
                            "candidate_status": "REVIEW_REQUIRED",
                            "status_reason": reason,
                            "confidence": min(candidate.confidence, 0.65),
                        }
                    )
                )
    return output


def extract_machinery_prose_candidates(
    document_text: str,
    *,
    filing: dict[str, Any],
    company_currency: str = "USD",
) -> list[DisclosureCandidate]:
    report_date = str(filing.get("report_date") or filing.get("filing_date") or "").strip()
    blocks = narrative_blocks(document_text)
    candidates: list[DisclosureCandidate] = []
    seen: set[tuple[str, str, float, int]] = set()
    seen_dated_values: set[tuple[str, float, int]] = set()
    for block_index, block in enumerate(blocks):
        lowered = block.lower()
        if not any(token in lowered for token in ("backlog", "bookings", "orders", "performance obligation")):
            continue
        context = " ".join(blocks[max(0, block_index - 5) : block_index])[-1200:]
        for pattern in _PATTERNS:
            for match in pattern.finditer(block):
                matched_disclosure = match.group(0)
                unbound_prior_year = (
                    re.search(
                        r"\b(?:prior|previous)\s+year\b",
                        matched_disclosure,
                        re.IGNORECASE,
                    )
                    is not None
                    and not match.groupdict().get("prior_year")
                )
                single_date_comparison = (
                    re.search(
                        r"\bcompared\s+to\b",
                        matched_disclosure,
                        re.IGNORECASE,
                    )
                    is not None
                    and len(
                        list(
                            re.finditer(
                                _DATE_PATTERN,
                                matched_disclosure,
                                re.IGNORECASE,
                            )
                        )
                    )
                    < 2
                )
                if unbound_prior_year or single_date_comparison:
                    continue
                concept_name = _concept_from_label(match.group("label"))
                value, unit, confidence = _money(match, company_currency=company_currency)
                extracted_period_end = _disclosure_period_end(
                    match,
                    report_date=report_date,
                )
                value_key = (concept_name, round(value, 6), block_index)
                if not extracted_period_end and value_key in seen_dated_values:
                    continue
                if extracted_period_end:
                    seen_dated_values.add(value_key)
                period_end = extracted_period_end or report_date
                duration_metric = concept_name == "Orders"
                period_start = _period_start(period_end, match.group(0), duration_metric=duration_metric)
                if duration_metric and not period_start:
                    confidence = min(confidence, 0.6)
                matched_text = " ".join(match.group(0).split())
                evidence = " ".join(
                    block[max(0, match.start() - 220) : match.end() + 220].split()
                )[:1000]
                if (
                    concept_name == "ReportedBacklog"
                    and re.search(
                        r"\bfair\s+value\b.{0,220}\bbacklog\b"
                        r"|\bbacklog\b.{0,80}\bfair\s+value\b",
                        block,
                        re.IGNORECASE,
                    )
                ):
                    continue
                if concept_name == "Orders" and re.search(
                    r"\bchange\s+orders?\b"
                    r"|\borders?\s+(?:increased|decreased)\s+by\b"
                    r"|\borders?\s+contributed\s+by\s+acquisitions?\b",
                    evidence,
                    re.IGNORECASE,
                ):
                    continue
                key = (concept_name, period_end, round(value, 6), block_index)
                if key in seen or value < 1_000_000.0:
                    continue
                seen.add(key)
                candidates.append(
                    DisclosureCandidate(
                        concept_name=concept_name,
                        metric_name=_canonical_metric(concept_name),
                        value=value,
                        unit=unit,
                        period_start=period_start,
                        period_end=period_end,
                        scope=_scope(matched_text, context),
                        confidence=confidence,
                        candidate_status="REVIEW_REQUIRED",
                        status_reason="pending_scope_resolution",
                        evidence_text=evidence,
                        block_index=block_index,
                    )
                )
    return _rank_candidates(candidates)


def _resolved(
    candidate: DisclosureCandidate,
    *,
    status: str,
    reason: str,
    concept_name: str | None = None,
    scope: str | None = None,
    confidence: float | None = None,
) -> DisclosureCandidate:
    resolved_concept = concept_name or candidate.concept_name
    return replace(
        candidate,
        concept_name=resolved_concept,
        metric_name=_canonical_metric(resolved_concept),
        scope=scope or candidate.scope,
        confidence=candidate.confidence if confidence is None else confidence,
        candidate_status=status,
        status_reason=reason,
    )


def _aggregate_components(
    candidates: list[DisclosureCandidate],
    *,
    reason: str,
) -> list[DisclosureCandidate]:
    """Retain component evidence and add one accepted consolidated observation."""
    output = [
        _resolved(
            candidate,
            status="CONSUMED_BY_AGGREGATE",
            reason=reason,
        )
        for candidate in candidates
    ]
    unique_values = {round(candidate.value, 6): candidate for candidate in candidates}
    if len(unique_values) < 2:
        return [
            _resolved(
                candidate,
                status="REVIEW_REQUIRED",
                reason="aggregate_requires_all_reviewed_components",
                confidence=min(candidate.confidence, 0.65),
            )
            for candidate in candidates
        ]
    ordered = sorted(unique_values.values(), key=lambda item: (item.block_index, item.value))
    anchor = ordered[0]
    output.append(
        replace(
            anchor,
            value=sum(item.value for item in ordered),
            scope="consolidated",
            confidence=min(max(item.confidence for item in ordered), 0.90),
            candidate_status="ACCEPTED",
            status_reason=reason,
            evidence_text=" + ".join(item.evidence_text for item in ordered)[:1000],
            extraction_method="filing_html_prose_reviewed_aggregate",
        )
    )
    return output


def _deduplicate_resolved_candidates(
    candidates: list[DisclosureCandidate],
) -> list[DisclosureCandidate]:
    grouped: dict[tuple[str, str, str, str, float], list[DisclosureCandidate]] = {}
    passthrough: list[DisclosureCandidate] = []
    for candidate in candidates:
        if candidate.candidate_status != "ACCEPTED":
            passthrough.append(candidate)
            continue
        grouped.setdefault(
            (
                candidate.concept_name,
                candidate.period_start,
                candidate.period_end,
                candidate.unit,
                round(candidate.value, 6),
            ),
            [],
        ).append(candidate)
    for duplicates in grouped.values():
        ranked = sorted(
            duplicates,
            key=lambda item: (
                item.scope == "consolidated",
                item.confidence,
                -item.block_index,
            ),
            reverse=True,
        )
        passthrough.append(ranked[0])
        passthrough.extend(
            _resolved(
                candidate,
                status="SUPPRESSED_SEMANTIC_DUPLICATE",
                reason="same_period_value_uses_one_canonical_metric",
            )
            for candidate in ranked[1:]
        )
    return sorted(passthrough, key=lambda item: (item.block_index, item.concept_name, item.value))


def resolve_machinery_disclosure_candidates(
    candidates: Iterable[DisclosureCandidate],
    *,
    ticker: str,
    filing: dict[str, Any],
) -> list[DisclosureCandidate]:
    """Apply reviewed issuer semantics before prose facts enter canonical data."""
    symbol = str(ticker or "").strip().upper()
    form_type = str(filing.get("form_type") or "").strip().upper()
    accession = str(filing.get("accession_number") or "").strip()
    known_date = accepted_date(
        str(filing.get("accepted_at") or ""),
        str(filing.get("filing_date") or ""),
    )
    resolved = list(candidates)

    if symbol == "BLDP":
        filtered: list[DisclosureCandidate] = []
        for candidate in resolved:
            evidence = candidate.evidence_text.lower()
            short_horizon = bool(
                re.search(
                    r"\b(?:12|twelve)[-\s]+months?\b|\bnext\s+(?:12|twelve)\s+months?\b",
                    evidence,
                )
            )
            approved_total = (
                candidate.concept_name == "ReportedBacklog"
                and candidate.period_end == "2026-03-31"
                and abs(candidate.value - 112_900_000.0) <= 1.0
                and "order backlog" in evidence
                and not short_horizon
            )
            if short_horizon:
                filtered.append(
                    _resolved(
                        candidate,
                        status="REJECTED_POLICY",
                        reason="twelve_month_operating_backlog_separate_from_total",
                    )
                )
            elif approved_total:
                filtered.append(
                    _resolved(
                        candidate,
                        status="ACCEPTED",
                        reason="reviewed_total_order_backlog_usd",
                        scope="consolidated",
                        confidence=min(candidate.confidence, 0.90),
                    )
                )
            else:
                filtered.append(candidate)
        resolved = filtered
    elif symbol == "MAIR":
        filtered = []
        final_prospectus = (
            form_type == "424B4"
            and accession == _MAIR_FINAL_PROSPECTUS_ACCESSION
            and known_date >= _MAIR_INFORMATION_BARRIER
        )
        for candidate in resolved:
            evidence = candidate.evidence_text.lower()
            if final_prospectus and candidate.concept_name == "ReportedBacklog":
                historical_backlog_dates = {
                    987_400_000.0: "2024-12-31",
                    2_160_800_000.0: "2025-12-31",
                }
                reviewed_period_end = historical_backlog_dates.get(
                    round(candidate.value, 6)
                )
                if reviewed_period_end:
                    candidate = replace(candidate, period_end=reviewed_period_end)
            key = (
                candidate.concept_name,
                candidate.period_start,
                candidate.period_end,
                round(candidate.value, 6),
            )
            segment_fragment = (
                candidate.scope == "segment"
                or abs(candidate.value - 57_500_000.0) <= 1.0
                or "aprilaire's" in evidence
                or "commercial segment" in evidence
                or "residential segment" in evidence
            )
            reviewed_q1_backlog = (
                form_type in _PERIODIC_FORMS
                and candidate.concept_name == "ReportedBacklog"
                and candidate.period_end == "2026-03-31"
                and abs(candidate.value - 2_520_200_000.0) <= 1.0
            )
            historical_candidate = (
                candidate.concept_name in {"Orders", "ReportedBacklog"}
                and bool(candidate.period_end)
                and candidate.period_end <= "2025-12-31"
            )
            if segment_fragment:
                filtered.append(
                    _resolved(
                        candidate,
                        status="REJECTED_POLICY",
                        reason="reviewed_segment_backlog_fragment_rejected",
                    )
                )
            elif reviewed_q1_backlog:
                filtered.append(
                    _resolved(
                        candidate,
                        status="ACCEPTED",
                        reason="reviewed_q1_2026_consolidated_backlog",
                        scope="consolidated",
                        confidence=min(candidate.confidence, 0.90),
                    )
                )
            elif final_prospectus and key in _MAIR_FINAL_PROSPECTUS_VALUES:
                filtered.append(
                    _resolved(
                        candidate,
                        status="ACCEPTED",
                        reason="reviewed_final_424b4_historical_value",
                        scope="consolidated",
                        confidence=min(candidate.confidence, 0.90),
                    )
                )
            elif historical_candidate:
                filtered.append(
                    _resolved(
                        candidate,
                        status="REJECTED_POLICY",
                        reason=(
                            "unreviewed_424b4_period_or_value_rejected"
                            if final_prospectus
                            else "superseded_registration_statement_candidate"
                        ),
                    )
                )
            else:
                filtered.append(candidate)
        resolved = filtered
    elif symbol == "ASTE":
        reviewed_totals = {
            ("2019-03-31", 236_500_000.0),
            ("2019-06-30", 246_100_000.0),
        }
        reviewed_international_components = {
            ("2019-03-31", 74_700_000.0),
            ("2019-06-30", 84_500_000.0),
        }
        filtered = []
        for candidate in resolved:
            key = (candidate.period_end, round(candidate.value, 6))
            if candidate.concept_name != "ReportedBacklog":
                filtered.append(candidate)
            elif key in reviewed_totals:
                filtered.append(
                    _resolved(
                        candidate,
                        status="ACCEPTED",
                        reason="reviewed_explicit_company_backlog",
                        scope="consolidated",
                        confidence=min(candidate.confidence, 0.90),
                    )
                )
            elif key in reviewed_international_components:
                filtered.append(
                    _resolved(
                        candidate,
                        status="REJECTED_POLICY",
                        reason="reviewed_international_backlog_component",
                        scope="segment",
                    )
                )
            else:
                filtered.append(candidate)
        resolved = filtered
    elif symbol == "EOSE" and accession == "0001628280-23-005669":
        reviewed_periods = {
            147_500_000.0: "2021-12-31",
            463_800_000.0: "2022-12-31",
        }
        resolved = [
            _resolved(
                replace(
                    candidate,
                    period_end=reviewed_periods[round(candidate.value, 6)],
                ),
                status="ACCEPTED",
                reason="reviewed_current_and_comparative_orders_backlog",
                scope="consolidated",
                confidence=min(candidate.confidence, 0.90),
            )
            if candidate.concept_name == "ReportedBacklog"
            and round(candidate.value, 6) in reviewed_periods
            else candidate
            for candidate in resolved
        ]
    elif symbol == "JBTM" and accession == "0001433660-22-000034":
        reviewed_components = {
            662_000_000.0: "FoodTech",
            387_000_000.0: "AeroTech",
        }
        components: list[DisclosureCandidate] = []
        remainder: list[DisclosureCandidate] = []
        for candidate in resolved:
            if (
                candidate.concept_name == "ReportedBacklog"
                and round(candidate.value, 6) in reviewed_components
            ):
                components.append(
                    replace(
                        candidate,
                        period_end="2022-09-30",
                        scope="segment",
                    )
                )
            else:
                remainder.append(candidate)
        if {round(item.value, 6) for item in components} == set(
            reviewed_components
        ):
            resolved = remainder + _aggregate_components(
                components,
                reason="reviewed_foodtech_aerotech_exhaustive_segment_sum",
            )
        else:
            resolved = remainder + [
                _resolved(
                    item,
                    status="REVIEW_REQUIRED",
                    reason="jbtm_exhaustive_segment_pair_incomplete",
                )
                for item in components
            ]
    elif symbol == "PTRA" and accession == "0001628280-23-008121":
        reviewed_components = {1_000_000_000.0, 600_000_000.0}
        components = [
            replace(candidate, period_end="2022-12-31", scope="segment")
            for candidate in resolved
            if candidate.concept_name == "ReportedBacklog"
            and round(candidate.value, 6) in reviewed_components
        ]
        remainder = [
            candidate
            for candidate in resolved
            if not (
                candidate.concept_name == "ReportedBacklog"
                and round(candidate.value, 6) in reviewed_components
            )
        ]
        if {round(item.value, 6) for item in components} == reviewed_components:
            resolved = remainder + _aggregate_components(
                components,
                reason="reviewed_powered_energy_transit_exhaustive_segment_sum",
            )
        else:
            resolved = remainder + [
                _resolved(
                    item,
                    status="REVIEW_REQUIRED",
                    reason="ptra_exhaustive_segment_pair_incomplete",
                )
                for item in components
            ]
    elif symbol == "VRT" and accession == "0001193125-20-028316":
        reviewed_periods = {
            1_400_800_000.0: "2019-09-30",
            1_527_600_000.0: "2018-09-30",
            1_502_000_000.0: "2018-12-31",
            1_314_400_000.0: "2017-12-31",
        }
        normalized = []
        for candidate in resolved:
            reviewed_period_end = reviewed_periods.get(round(candidate.value, 6))
            if (
                candidate.concept_name == "ReportedBacklog"
                and reviewed_period_end
            ):
                normalized.append(
                    _resolved(
                        replace(candidate, period_end=reviewed_period_end),
                        status="ACCEPTED",
                        reason="reviewed_combined_backlog_respectively_mapping",
                        scope="consolidated",
                        confidence=min(candidate.confidence, 0.90),
                    )
                )
            else:
                normalized.append(candidate)
        resolved = normalized
    elif symbol == "MTW":
        filtered = []
        for candidate in resolved:
            approved = (
                candidate.concept_name == "ReportedBacklog"
                and candidate.period_end == "2025-12-31"
                and abs(candidate.value - 793_500_000.0) <= 1.0
            )
            component = candidate.concept_name == "ReportedBacklog" and (
                candidate.scope == "segment"
                or re.search(
                    r"\b(?:euram|apac|regional|region|component)\b",
                    candidate.evidence_text,
                    re.IGNORECASE,
                )
                is not None
            )
            if approved:
                filtered.append(
                    _resolved(
                        candidate,
                        status="ACCEPTED",
                        reason="reviewed_consolidated_company_backlog",
                        scope="consolidated",
                        confidence=min(candidate.confidence, 0.90),
                    )
                )
            elif component:
                filtered.append(
                    _resolved(
                        candidate,
                        status="REJECTED_POLICY",
                        reason="regional_or_component_backlog_rejected",
                    )
                )
            else:
                filtered.append(candidate)
        resolved = filtered
    elif symbol == "TWIN":
        reviewed_values = {
            ("2024-06-30", 133_700_000.0),
            ("2025-06-30", 150_500_000.0),
        }
        resolved = [
            _resolved(
                candidate,
                status="ACCEPTED",
                reason="reviewed_consolidated_six_month_backlog_anchor",
                scope="consolidated",
                confidence=min(candidate.confidence, 0.90),
            )
            if candidate.concept_name == "ReportedBacklog"
            and (candidate.period_end, round(candidate.value, 6)) in reviewed_values
            else candidate
            for candidate in resolved
        ]
    elif symbol == "CIR":
        grouped: dict[tuple[str, str, str], list[DisclosureCandidate]] = {}
        remainder: list[DisclosureCandidate] = []
        for candidate in resolved:
            if candidate.concept_name == "ReportedBacklog":
                grouped.setdefault(
                    (candidate.period_start, candidate.period_end, candidate.unit), []
                ).append(candidate)
            else:
                remainder.append(candidate)
        resolved = remainder
        for components in grouped.values():
            direct_totals = [
                candidate
                for candidate in components
                if candidate.candidate_status == "ACCEPTED"
                and candidate.scope == "consolidated"
            ]
            if len(direct_totals) == 1:
                selected = direct_totals[0]
                resolved.append(selected)
                resolved.extend(
                    _resolved(
                        candidate,
                        status="CONSUMED_BY_CONSOLIDATED_TOTAL",
                        reason="explicit_consolidated_total_preferred",
                    )
                    for candidate in components
                    if candidate is not selected
                )
            elif len({round(candidate.value, 6) for candidate in components}) == 3:
                resolved.extend(
                    _aggregate_components(
                        components,
                        reason="reviewed_exhaustive_operating_segment_sum",
                    )
                )
            else:
                resolved.extend(
                    _resolved(
                        candidate,
                        status="REJECTED_POLICY",
                        reason="incomplete_operating_segment_backlog_components",
                    )
                    for candidate in components
                )
    elif symbol == "CXT":
        resolved = [
            _resolved(
                candidate,
                status="REJECTED_POLICY",
                reason="pre_separation_segment_backlog_excluded",
            )
            if candidate.concept_name == "ReportedBacklog"
            and (
                candidate.scope == "segment"
                or (bool(known_date) and known_date < _CXT_SEPARATION_DATE)
            )
            else candidate
            for candidate in resolved
        ]
    elif symbol == "CR":
        grouped: dict[tuple[str, str, str], list[DisclosureCandidate]] = {}
        remainder: list[DisclosureCandidate] = []
        for candidate in resolved:
            if candidate.concept_name == "ReportedBacklog":
                grouped.setdefault(
                    (candidate.period_start, candidate.period_end, candidate.unit), []
                ).append(candidate)
            else:
                remainder.append(candidate)
        resolved = remainder
        for components in grouped.values():
            direct_totals = [
                candidate
                for candidate in components
                if re.search(
                    r"\b(?:total|consolidated|company(?:'s)?)\s+(?:order\s+)?backlog\b",
                    candidate.evidence_text,
                    re.IGNORECASE,
                )
                or (
                    re.search(
                        r"^(?:as\s+of|at)\b.{0,80}\bbacklog\s+"
                        r"(?:was|were|totaled|totalled)\b",
                        candidate.evidence_text,
                        re.IGNORECASE,
                    )
                    is not None
                )
            ]
            if len(direct_totals) == 1:
                selected = direct_totals[0]
                resolved.append(
                    _resolved(
                        selected,
                        status="ACCEPTED",
                        reason="reviewed_explicit_consolidated_total",
                        scope="consolidated",
                        confidence=min(selected.confidence, 0.90),
                    )
                )
                resolved.extend(
                    _resolved(
                        candidate,
                        status="CONSUMED_BY_CONSOLIDATED_TOTAL",
                        reason="reviewed_explicit_consolidated_total",
                    )
                    for candidate in components
                    if candidate is not selected
                )
            else:
                resolved.extend(
                    _aggregate_components(
                        components,
                        reason="reviewed_exhaustive_operating_segment_sum",
                    )
                )
    elif symbol == "MIDD":
        grouped: dict[tuple[str, str, str], list[DisclosureCandidate]] = {}
        remainder: list[DisclosureCandidate] = []
        for candidate in resolved:
            if candidate.concept_name == "ReportedBacklog":
                grouped.setdefault(
                    (candidate.period_start, candidate.period_end, candidate.unit), []
                ).append(candidate)
            else:
                remainder.append(candidate)
        resolved = remainder
        for components in grouped.values():
            direct_totals = [
                candidate
                for candidate in components
                if candidate.scope != "segment"
                and re.search(
                    r"\b(?:total|consolidated|company(?:'s)?)\s+(?:order\s+)?backlog\b",
                    candidate.evidence_text,
                    re.IGNORECASE,
                )
            ]
            segment_components = [
                candidate for candidate in components if candidate.scope == "segment"
            ]
            if len(direct_totals) == 1:
                selected = direct_totals[0]
                resolved.append(
                    _resolved(
                        selected,
                        status="ACCEPTED",
                        reason="reviewed_explicit_consolidated_total",
                        scope="consolidated",
                        confidence=min(selected.confidence, 0.90),
                    )
                )
                resolved.extend(
                    _resolved(
                        candidate,
                        status="CONSUMED_BY_CONSOLIDATED_TOTAL",
                        reason="reviewed_explicit_consolidated_total",
                    )
                    for candidate in components
                    if candidate is not selected
                )
            elif len(segment_components) == len(components) and len(
                {round(candidate.value, 6) for candidate in segment_components}
            ) >= 2:
                resolved.extend(
                    _aggregate_components(
                        segment_components,
                        reason="reviewed_exhaustive_operating_segment_sum",
                    )
                )
            else:
                resolved.extend(
                    _resolved(
                        candidate,
                        status="REJECTED_POLICY",
                        reason="incomplete_or_mixed_scope_backlog_components",
                    )
                    for candidate in components
                )
    elif symbol == "LEU":
        grouped = {}
        remainder = []
        for candidate in resolved:
            if candidate.concept_name == "ReportedBacklog":
                remainder.append(
                    _resolved(
                        candidate,
                        status="REJECTED_POLICY",
                        reason="contingent_or_unfunded_options_excluded_from_rpo",
                    )
                )
            elif candidate.concept_name == "RemainingPerformanceObligation":
                grouped.setdefault(
                    (candidate.period_start, candidate.period_end, candidate.unit), []
                ).append(candidate)
            else:
                remainder.append(candidate)
        resolved = remainder
        for components in grouped.values():
            direct_totals = [
                candidate
                for candidate in components
                if " segment" not in candidate.evidence_text.lower()
            ]
            if direct_totals:
                selected = max(direct_totals, key=lambda item: item.value)
                resolved.append(
                    _resolved(
                        selected,
                        status="ACCEPTED",
                        reason="reviewed_committed_gaap_rpo_total",
                        scope="consolidated",
                        confidence=min(selected.confidence, 0.90),
                    )
                )
                resolved.extend(
                    _resolved(
                        candidate,
                        status="CONSUMED_BY_CONSOLIDATED_TOTAL",
                        reason="reviewed_committed_gaap_rpo_total",
                    )
                    for candidate in components
                    if candidate is not selected
                )
            else:
                if len({round(candidate.value, 6) for candidate in components}) < 2:
                    resolved.extend(
                        _resolved(
                            candidate,
                            status="REJECTED_POLICY",
                            reason="incomplete_segment_rpo_components",
                        )
                        for candidate in components
                    )
                else:
                    resolved.extend(
                        _aggregate_components(
                            components,
                            reason="reviewed_committed_rpo_segment_sum",
                        )
                    )
    elif symbol == "FCEL":
        grouped: dict[tuple[str, str, str], list[DisclosureCandidate]] = {}
        remainder: list[DisclosureCandidate] = []
        for candidate in resolved:
            if candidate.concept_name == "ReportedBacklog":
                grouped.setdefault(
                    (candidate.period_start, candidate.period_end, candidate.unit), []
                ).append(candidate)
            else:
                remainder.append(candidate)
        resolved = remainder
        for components in grouped.values():
            direct_totals = [
                candidate
                for candidate in components
                if candidate.scope == "consolidated"
                and re.search(
                    r"\boverall,?\s+backlog\b",
                    candidate.evidence_text,
                    re.IGNORECASE,
                )
            ]
            if len(direct_totals) == 1:
                selected = direct_totals[0]
                resolved.append(
                    _resolved(
                        selected,
                        status="ACCEPTED",
                        reason="reviewed_explicit_consolidated_total",
                        scope="consolidated",
                        confidence=min(selected.confidence, 0.90),
                    )
                )
                resolved.extend(
                    _resolved(
                        candidate,
                        status="CONSUMED_BY_CONSOLIDATED_TOTAL",
                        reason="reviewed_explicit_consolidated_total",
                    )
                    for candidate in components
                    if candidate is not selected
                )
            else:
                resolved.extend(
                    _resolved(
                        candidate,
                        status="REJECTED_POLICY",
                        reason="incomplete_backlog_category_components",
                    )
                    for candidate in components
                )
    elif symbol == "FLS":
        resolved = [
            _resolved(
                candidate,
                status=(
                    "ACCEPTED"
                    if candidate.candidate_status == "ACCEPTED"
                    and candidate.concept_name == "ReportedBacklog"
                    else "REJECTED_POLICY"
                ),
                reason=(
                    "reviewed_consolidated_backlog_total"
                    if candidate.candidate_status == "ACCEPTED"
                    and candidate.concept_name == "ReportedBacklog"
                    else "segment_overlap_or_booking_adjustment_rejected"
                ),
                scope=(
                    "consolidated"
                    if candidate.candidate_status == "ACCEPTED"
                    and candidate.concept_name == "ReportedBacklog"
                    else None
                ),
            )
            for candidate in resolved
        ]
    elif symbol == "FSS":
        filtered = []
        for candidate in resolved:
            evidence = candidate.evidence_text.lower().strip()
            approved_total_orders = (
                candidate.concept_name == "Orders"
                and (
                    evidence.startswith("total orders for the ")
                    or evidence.startswith("consolidated orders ")
                )
            )
            approved_backlog = (
                candidate.concept_name == "ReportedBacklog"
                and candidate.candidate_status == "ACCEPTED"
            )
            approved = approved_total_orders or approved_backlog
            filtered.append(
                _resolved(
                    candidate,
                    status="ACCEPTED" if approved else "REJECTED_POLICY",
                    reason=(
                        "reviewed_consolidated_orders_or_backlog"
                        if approved
                        else "segment_allocation_or_transactional_adjustment_rejected"
                    ),
                    scope="consolidated" if approved else None,
                    confidence=min(candidate.confidence, 0.90),
                )
            )
        resolved = filtered
    elif symbol == "GTLS":
        filtered: list[DisclosureCandidate] = []
        for candidate in resolved:
            approved_order = (
                candidate.concept_name == "Orders"
                and (candidate.period_end, round(candidate.value, 6)) in _GTLS_APPROVED_ORDERS
            )
            approved_backlog = (
                candidate.concept_name == "ReportedBacklog"
                and candidate.candidate_status == "ACCEPTED"
            )
            filtered.append(
                _resolved(
                    candidate,
                    status="ACCEPTED" if approved_order or approved_backlog else "REJECTED_POLICY",
                    reason=(
                        "reviewed_period_aligned_consolidated_value"
                        if approved_order or approved_backlog
                        else "comparison_period_or_segment_value_rejected"
                    ),
                    scope="consolidated" if approved_order or approved_backlog else None,
                    confidence=min(candidate.confidence, 0.90),
                )
            )
        resolved = filtered
    elif symbol == "MCRN":
        grouped: dict[tuple[str, str, str], list[DisclosureCandidate]] = {}
        remainder: list[DisclosureCandidate] = []
        for candidate in resolved:
            if candidate.concept_name == "Orders":
                grouped.setdefault(
                    (candidate.period_start, candidate.period_end, candidate.unit), []
                ).append(candidate)
            else:
                remainder.append(candidate)
        resolved = remainder
        for components in grouped.values():
            unique_values = {round(candidate.value, 6) for candidate in components}
            if (
                len(unique_values) == 1
                and len(components) == 1
                and components[0].candidate_status == "ACCEPTED"
            ):
                resolved.append(components[0])
            elif len(unique_values) == 2:
                selected = max(components, key=lambda item: item.value)
                resolved.append(
                    _resolved(
                        selected,
                        status="ACCEPTED",
                        reason="reviewed_reported_orders_over_pro_forma",
                        scope="consolidated",
                        confidence=min(selected.confidence, 0.90),
                    )
                )
                resolved.extend(
                    _resolved(
                        candidate,
                        status="REJECTED_POLICY",
                        reason="pro_forma_orders_separate_from_reported_orders",
                    )
                    for candidate in components
                    if candidate is not selected
                )
            else:
                resolved.extend(
                    _resolved(
                        candidate,
                        status="REJECTED_POLICY",
                        reason="reported_and_pro_forma_orders_not_both_present",
                    )
                    for candidate in components
                )
    elif symbol == "MIR":
        filtered = []
        for candidate in resolved:
            evidence = candidate.evidence_text.lower()
            approved = (
                "all open customer contracts" in evidence
                or "committed but undelivered contracts and purchase orders" in evidence
            )
            filtered.append(
                _resolved(
                    candidate,
                    status="ACCEPTED" if approved else "REJECTED_POLICY",
                    reason=(
                        "reviewed_total_consolidated_rpo"
                        if approved
                        else "regional_or_project_subset_rejected"
                    ),
                    scope="consolidated" if approved else None,
                    confidence=min(candidate.confidence, 0.90),
                )
            )
        resolved = filtered

    verified_concepts = _VERIFIED_CONSOLIDATED_CONCEPTS.get(symbol, frozenset())
    if verified_concepts:
        resolved = [
            _resolved(
                candidate,
                status="ACCEPTED",
                reason="reviewed_issuer_consolidated_disclosure",
                scope="consolidated",
                confidence=min(candidate.confidence, 0.90),
            )
            if candidate.concept_name in verified_concepts and candidate.scope != "segment"
            else candidate
            for candidate in resolved
        ]

    if symbol in _RPO_MASTER_TICKERS:
        normalized: list[DisclosureCandidate] = []
        for candidate in resolved:
            evidence = candidate.evidence_text.lower()
            if (
                symbol == "WAB"
                and candidate.concept_name == "ReportedBacklog"
                and re.search(r"\b(?:12[-\s]+month|multi[-\s]+year)\s+backlog\b", evidence)
            ):
                normalized.append(
                    _resolved(
                        candidate,
                        status="REJECTED_POLICY",
                        reason="horizon_specific_backlog_not_total_company_rpo",
                    )
                )
            elif candidate.concept_name in {
                "ReportedBacklog",
                "RemainingPerformanceObligation",
            }:
                normalized.append(
                    _resolved(
                        candidate,
                        status="ACCEPTED",
                        reason="reviewed_rpo_master_semantic_alias",
                        concept_name="RemainingPerformanceObligation",
                        scope="consolidated",
                        confidence=min(candidate.confidence, 0.90),
                    )
                )
            else:
                normalized.append(candidate)
        resolved = normalized

    return _deduplicate_resolved_candidates(resolved)


def candidate_key(
    *,
    ticker: str,
    source_id: str,
    accession_number: str,
    document_name: str,
    candidate: DisclosureCandidate,
) -> str:
    payload = "|".join(
        (
            ticker,
            source_id,
            accession_number,
            document_name,
            candidate.concept_name,
            candidate.period_start,
            candidate.period_end,
            str(candidate.block_index),
            f"{candidate.value:.12g}",
        )
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def upsert_disclosure_candidates(
    conn: Any,
    *,
    ticker: str,
    cik: str,
    source_id: str,
    model_family: str,
    filing: dict[str, Any],
    document_name: str,
    candidates: Iterable[DisclosureCandidate],
    now: str,
    source_detail: str = PROSE_SOURCE_DETAIL,
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
                candidate_value = excluded.candidate_value,
                unit = excluded.unit,
                period_start = excluded.period_start,
                period_end = excluded.period_end,
                scope = excluded.scope,
                confidence = excluded.confidence,
                candidate_status = excluded.candidate_status,
                status_reason = excluded.status_reason,
                evidence_text = excluded.evidence_text,
                provenance_json = excluded.provenance_json,
                updated_at = excluded.updated_at
            """,
            (
                key,
                ticker,
                cik,
                source_id,
                model_family,
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
                    source_detail=source_detail,
                ),
                now,
                now,
            ),
        )
        count += 1
    return count


def _prose_fact_key(
    *,
    ticker: str,
    source_id: str,
    accession_number: str,
    document_name: str,
    candidate: DisclosureCandidate,
    taxonomy: str = "sec-text",
) -> str:
    frame = (
        f"prose:{document_name}:{candidate.block_index}:"
        f"{candidate.concept_name}:{candidate.period_end}"
    )
    payload = "|".join(
        (
            ticker,
            source_id,
            accession_number,
            taxonomy,
            candidate.concept_name,
            candidate.unit.lower(),
            candidate.period_start,
            candidate.period_end,
            frame,
            document_name,
        )
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def replace_document_candidates_and_facts(
    conn: Any,
    *,
    ticker: str,
    cik: str,
    source_id: str,
    model_family: str,
    filing: dict[str, Any],
    document_name: str,
    candidates: Iterable[DisclosureCandidate],
    now: str,
    source_detail: str = PROSE_SOURCE_DETAIL,
    taxonomy: str = "sec-text",
    source_priority_floor: int = 0,
) -> tuple[int, int, int]:
    """Atomically replace one cached document's candidates and promoted facts."""
    accession = str(filing.get("accession_number") or "").strip()
    candidate_rows = list(candidates)
    frame_prefix = f"prose:{document_name}:%"
    conn.execute(
        """
        DELETE FROM fact_sec_xbrl_fact
        WHERE raw_fact_id IN (
            SELECT raw_fact_id
            FROM fact_sec_xbrl_fact_raw
            WHERE ticker = ? AND source_id = ? AND accession_number = ?
              AND source_detail = ? AND frame LIKE ?
        )
        """,
        (ticker, source_id, accession, source_detail, frame_prefix),
    )
    conn.execute(
        """
        DELETE FROM fact_sec_xbrl_fact_raw
        WHERE ticker = ? AND source_id = ? AND accession_number = ?
          AND source_detail = ? AND frame LIKE ?
        """,
        (ticker, source_id, accession, source_detail, frame_prefix),
    )
    conn.execute(
        """
        DELETE FROM fact_sec_metric_disclosure_candidate
        WHERE ticker = ? AND source_id = ? AND model_family = ?
          AND accession_number = ? AND document_name = ?
        """,
        (ticker, source_id, model_family, accession, document_name),
    )
    candidate_count = upsert_disclosure_candidates(
        conn,
        ticker=ticker,
        cik=cik,
        source_id=source_id,
        model_family=model_family,
        filing=filing,
        document_name=document_name,
        candidates=candidate_rows,
        now=now,
        source_detail=source_detail,
    )

    raw_count = 0
    mapped_count = 0
    for candidate in accepted_candidates(candidate_rows):
        mapping = PROSE_CONCEPT_MAPPINGS.get(candidate.concept_name)
        if mapping is None:
            continue
        canonical_metric, financial_statement, period_type, priority = mapping
        priority = max(priority, source_priority_floor)
        fact_key = _prose_fact_key(
            ticker=ticker,
            source_id=source_id,
            accession_number=accession,
            document_name=document_name,
            candidate=candidate,
            taxonomy=taxonomy,
        )
        frame = (
            f"prose:{document_name}:{candidate.block_index}:"
            f"{candidate.concept_name}:{candidate.period_end}"
        )
        unit = candidate.unit.lower()
        conn.execute(
            """
            INSERT INTO fact_sec_xbrl_fact_raw(
                fact_key, ticker, cik, source_id, accession_number, form_type,
                filing_date, accepted_at, fiscal_year, fiscal_period, period_start,
                period_end, frame, taxonomy, concept_name, unit, raw_value, decimals,
                source_detail, payload_json, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '', ?, ?, ?, ?)
            ON CONFLICT(fact_key) DO UPDATE SET
                filing_date = excluded.filing_date,
                accepted_at = excluded.accepted_at,
                fiscal_year = excluded.fiscal_year,
                fiscal_period = excluded.fiscal_period,
                raw_value = excluded.raw_value,
                payload_json = excluded.payload_json,
                updated_at = excluded.updated_at
            """,
            (
                fact_key,
                ticker,
                cik,
                source_id,
                accession,
                str(filing.get("form_type") or "").strip().upper(),
                str(filing.get("filing_date") or "")[:10],
                str(filing.get("accepted_at") or ""),
                filing.get("fiscal_year"),
                str(filing.get("fiscal_period") or ""),
                candidate.period_start,
                candidate.period_end,
                frame,
                taxonomy,
                candidate.concept_name,
                unit,
                candidate.value,
                source_detail,
                candidate.payload_json(
                    document_name=document_name,
                    source_detail=source_detail,
                ),
                now,
                now,
            ),
        )
        raw_row = conn.execute(
            "SELECT raw_fact_id FROM fact_sec_xbrl_fact_raw WHERE fact_key = ?",
            (fact_key,),
        ).fetchone()
        raw_fact_id = int(raw_row["raw_fact_id"]) if raw_row is not None else None
        conn.execute(
            """
            INSERT INTO fact_sec_xbrl_fact(
                raw_fact_id, ticker, cik, source_id, accession_number,
                form_type, filing_date, accepted_at, fiscal_year, fiscal_period,
                period_start, period_end, frame, taxonomy, concept_name,
                canonical_metric, financial_statement, period_type, unit,
                value, sign_policy, source_priority, source_detail,
                created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    'positive_abs', ?, ?, ?, ?)
            ON CONFLICT(ticker, source_id, accession_number, taxonomy, concept_name,
                        canonical_metric, unit, period_start, period_end, frame)
            DO UPDATE SET
                raw_fact_id = excluded.raw_fact_id,
                filing_date = excluded.filing_date,
                accepted_at = excluded.accepted_at,
                fiscal_year = excluded.fiscal_year,
                fiscal_period = excluded.fiscal_period,
                value = excluded.value,
                source_priority = excluded.source_priority,
                source_detail = excluded.source_detail,
                updated_at = excluded.updated_at
            """,
            (
                raw_fact_id,
                ticker,
                cik,
                source_id,
                accession,
                str(filing.get("form_type") or "").strip().upper(),
                str(filing.get("filing_date") or "")[:10],
                str(filing.get("accepted_at") or ""),
                filing.get("fiscal_year"),
                str(filing.get("fiscal_period") or ""),
                candidate.period_start,
                candidate.period_end,
                frame,
                taxonomy,
                candidate.concept_name,
                canonical_metric,
                financial_statement,
                period_type,
                unit,
                abs(candidate.value),
                priority,
                f"{source_detail}_mapped",
                now,
                now,
            ),
        )
        raw_count += 1
        mapped_count += 1
    return candidate_count, raw_count, mapped_count


def accepted_candidates(candidates: Iterable[DisclosureCandidate]) -> list[DisclosureCandidate]:
    return [candidate for candidate in candidates if candidate.candidate_status == "ACCEPTED"]


def _delete_promoted_candidate_fact(
    conn: Any,
    row: dict[str, Any],
    *,
    source_detail: str,
) -> tuple[int, int]:
    params = (
        row["ticker"],
        row["source_id"],
        row["accession_number"],
        row["concept_name"],
        row["period_start"],
        row["period_end"],
        row["candidate_value"],
        row["candidate_value"],
    )
    raw_ids = [
        int(item[0])
        for item in conn.execute(
            """
            SELECT raw_fact_id
            FROM fact_sec_xbrl_fact_raw
            WHERE ticker = ? AND source_id = ? AND accession_number = ?
              AND source_detail = ? AND concept_name = ?
              AND COALESCE(period_start, '') = COALESCE(?, '') AND period_end = ?
              AND ABS(raw_value - ?) <= MAX(1.0, ABS(?) * 1e-9)
            """,
            (
                params[0],
                params[1],
                params[2],
                source_detail,
                params[3],
                params[4],
                params[5],
                params[6],
                params[7],
            ),
        ).fetchall()
    ]
    if not raw_ids:
        return 0, 0
    placeholders = ",".join("?" for _ in raw_ids)
    mapped = conn.execute(
        f"DELETE FROM fact_sec_xbrl_fact WHERE raw_fact_id IN ({placeholders})",
        raw_ids,
    ).rowcount
    raw = conn.execute(
        f"DELETE FROM fact_sec_xbrl_fact_raw WHERE raw_fact_id IN ({placeholders})",
        raw_ids,
    ).rowcount
    return int(raw or 0), int(mapped or 0)


def _suppress_candidate(
    conn: Any,
    row: dict[str, Any],
    *,
    status: str,
    reason: str,
    now: str,
    source_detail: str,
) -> tuple[int, int]:
    deleted = _delete_promoted_candidate_fact(
        conn,
        row,
        source_detail=source_detail,
    )
    conn.execute(
        """
        UPDATE fact_sec_metric_disclosure_candidate
        SET candidate_status = ?, status_reason = ?, updated_at = ?
        WHERE candidate_key = ?
        """,
        (status, reason, now, row["candidate_key"]),
    )
    return deleted


def _candidate_precedence(row: dict[str, Any]) -> tuple[int, str, str]:
    form_type = str(row.get("form_type") or "").strip().upper()
    if form_type in {"10-K/A", "10-Q/A"}:
        form_rank = 4
    elif form_type in _PERIODIC_FORMS:
        form_rank = 3
    elif form_type == "8-K/A":
        form_rank = 2
    elif form_type == "8-K":
        form_rank = 1
    else:
        form_rank = 0
    return (
        -form_rank,
        str(row.get("accepted_at") or row.get("filing_date") or "9999-12-31"),
        str(row.get("document_name") or ""),
    )


def _mapped_fact_precedence(
    row: dict[str, Any],
    *,
    prose_source_detail: str,
) -> tuple[int, int, str, str, int]:
    form_type = str(row.get("form_type") or "").strip().upper()
    if form_type in {"10-K/A", "10-Q/A"}:
        form_rank = 4
    elif form_type in _PERIODIC_FORMS:
        form_rank = 3
    elif form_type == "8-K/A":
        form_rank = 2
    elif form_type == "8-K":
        form_rank = 1
    else:
        form_rank = 0
    source_rank = {
        "sec_archive_xbrl_mapped": 0,
        "sec_archive_footnote_xbrl_mapped": 1,
        "sec_archive_text_table_mapped": 2,
        f"{prose_source_detail}_mapped": 3,
    }.get(str(row.get("source_detail") or ""), 4)
    return (
        -form_rank,
        source_rank,
        str(row.get("accepted_at") or row.get("filing_date") or "9999-12-31"),
        str(row.get("accession_number") or ""),
        int(row.get("fact_id") or 0),
    )


def _suppress_duplicate_mapped_facts(
    conn: Any,
    *,
    ticker: str,
    source_id: str,
    model_family: str,
    prose_source_detail: str,
) -> tuple[int, int]:
    placeholders = ",".join("?" for _ in _SPECIAL_DISCLOSURE_METRICS)
    rows = [
        dict(row)
        for row in conn.execute(
            f"""
            SELECT fact_id, raw_fact_id, accession_number, form_type, filing_date,
                   accepted_at, period_start, period_end, canonical_metric, unit,
                   value, source_detail
            FROM fact_sec_xbrl_fact
            WHERE ticker = ? AND source_id = ?
              AND canonical_metric IN ({placeholders})
            ORDER BY canonical_metric, period_end, value, accession_number, fact_id
            """,
            (ticker, source_id, *sorted(_SPECIAL_DISCLOSURE_METRICS)),
        ).fetchall()
    ]
    grouped: dict[tuple[str, str, str, str, float], list[dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(
            (
                str(row["canonical_metric"]),
                str(row["period_start"] or ""),
                str(row["period_end"] or ""),
                str(row["unit"] or "").upper(),
                round(float(row["value"]), 6),
            ),
            [],
        ).append(row)

    fact_ids: list[int] = []
    canonical_deleted = 0
    for duplicates in grouped.values():
        if len(duplicates) < 2:
            continue
        winner = min(
            duplicates,
            key=lambda item: _mapped_fact_precedence(
                item,
                prose_source_detail=prose_source_detail,
            ),
        )
        for duplicate in duplicates:
            if duplicate["fact_id"] == winner["fact_id"]:
                continue
            fact_ids.append(int(duplicate["fact_id"]))
            canonical_deleted += int(
                conn.execute(
                    """
                    DELETE FROM fact_financial_statement_canonical
                    WHERE ticker = ? AND source_id = ? AND model_family = ?
                      AND accession_number = ? AND canonical_metric = ?
                      AND COALESCE(period_start, '') = COALESCE(?, '')
                      AND period_end = ?
                      AND LOWER(COALESCE(unit, '')) = LOWER(?)
                      AND ABS(value - ?) <= MAX(1.0, ABS(?) * 1e-9)
                    """,
                    (
                        ticker,
                        source_id,
                        model_family,
                        duplicate["accession_number"],
                        duplicate["canonical_metric"],
                        duplicate["period_start"],
                        duplicate["period_end"],
                        duplicate["unit"],
                        duplicate["value"],
                        duplicate["value"],
                    ),
                ).rowcount
                or 0
            )
    if not fact_ids:
        return 0, canonical_deleted
    fact_placeholders = ",".join("?" for _ in fact_ids)
    mapped_deleted = int(
        conn.execute(
            f"DELETE FROM fact_sec_xbrl_fact WHERE fact_id IN ({fact_placeholders})",
            fact_ids,
        ).rowcount
        or 0
    )
    return mapped_deleted, canonical_deleted


def reconcile_machinery_disclosure_facts(
    conn: Any,
    *,
    ticker: str,
    source_id: str,
    model_family: str,
    now: str,
    prose_source_detail: str = PROSE_SOURCE_DETAIL,
) -> dict[str, int]:
    """Suppress cross-document and structured duplicates after ticker ingestion."""
    symbol = str(ticker or "").strip().upper()
    rows = [
        dict(row)
        for row in conn.execute(
            """
            SELECT candidate_key, ticker, source_id, accession_number, form_type,
                   filing_date, accepted_at, document_name, metric_name,
                   concept_name, candidate_value, unit, period_start, period_end
            FROM fact_sec_metric_disclosure_candidate
            WHERE ticker = ? AND source_id = ? AND model_family = ?
              AND candidate_status = 'ACCEPTED'
            ORDER BY period_end, metric_name, candidate_value, filing_date,
                     accession_number, document_name
            """,
            (symbol, source_id, model_family),
        ).fetchall()
    ]
    stats = {
        "candidate_suppressions": 0,
        "raw_facts_deleted": 0,
        "mapped_facts_deleted": 0,
        "duplicate_mapped_facts_suppressed": 0,
        "duplicate_canonical_facts_suppressed": 0,
        "backlog_aliases_deleted": 0,
        "derived_rpo_current_deleted": 0,
    }
    grouped: dict[tuple[str, str, str, str, float], list[dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(
            (
                str(row["metric_name"]),
                str(row["period_start"]),
                str(row["period_end"]),
                str(row["unit"]).upper(),
                round(float(row["candidate_value"]), 6),
            ),
            [],
        ).append(row)
    retained: list[dict[str, Any]] = []
    for duplicates in grouped.values():
        winner = min(duplicates, key=_candidate_precedence)
        retained.append(winner)
        for duplicate in duplicates:
            if duplicate["candidate_key"] == winner["candidate_key"]:
                continue
            raw_deleted, mapped_deleted = _suppress_candidate(
                conn,
                duplicate,
                status="SUPPRESSED_DUPLICATE_PROVENANCE",
                reason="periodic_filing_precedence_then_earliest_accepted_document",
                now=now,
                source_detail=prose_source_detail,
            )
            stats["candidate_suppressions"] += 1
            stats["raw_facts_deleted"] += raw_deleted
            stats["mapped_facts_deleted"] += mapped_deleted

    for row in retained:
        structured = conn.execute(
            """
            SELECT 1
            FROM fact_sec_xbrl_fact
            WHERE ticker = ? AND source_id = ? AND canonical_metric = ?
              AND COALESCE(period_start, '') = COALESCE(?, '')
              AND period_end = ?
              AND LOWER(COALESCE(unit, '')) = LOWER(?)
              AND ABS(value - ?) <= MAX(1.0, ABS(?) * 1e-9)
              AND source_detail != ?
            LIMIT 1
            """,
            (
                symbol,
                source_id,
                row["metric_name"],
                row["period_start"],
                row["period_end"],
                row["unit"],
                row["candidate_value"],
                row["candidate_value"],
                f"{prose_source_detail}_mapped",
            ),
        ).fetchone()
        if structured is None:
            continue
        raw_deleted, mapped_deleted = _suppress_candidate(
            conn,
            row,
            status="SUPPRESSED_STRUCTURED_DUPLICATE",
            reason="equivalent_structured_fact_has_primary_provenance",
            now=now,
            source_detail=prose_source_detail,
        )
        stats["candidate_suppressions"] += 1
        stats["raw_facts_deleted"] += raw_deleted
        stats["mapped_facts_deleted"] += mapped_deleted

    mapped_deleted, canonical_deleted = _suppress_duplicate_mapped_facts(
        conn,
        ticker=symbol,
        source_id=source_id,
        model_family=model_family,
        prose_source_detail=prose_source_detail,
    )
    stats["mapped_facts_deleted"] += mapped_deleted
    stats["duplicate_mapped_facts_suppressed"] = mapped_deleted
    stats["duplicate_canonical_facts_suppressed"] = canonical_deleted

    if symbol in _RPO_MASTER_TICKERS:
        duplicate_backlog_ids = [
            int(row[0])
            for row in conn.execute(
                """
                SELECT backlog.fact_id
                FROM fact_sec_xbrl_fact backlog
                WHERE backlog.ticker = ? AND backlog.source_id = ?
                  AND backlog.canonical_metric = 'reported_backlog'
                  AND EXISTS (
                        SELECT 1
                        FROM fact_sec_xbrl_fact rpo
                        WHERE rpo.ticker = backlog.ticker
                          AND rpo.source_id = backlog.source_id
                          AND rpo.canonical_metric = 'remaining_performance_obligation'
                          AND rpo.period_end = backlog.period_end
                          AND LOWER(COALESCE(rpo.unit, '')) = LOWER(COALESCE(backlog.unit, ''))
                          AND ABS(rpo.value - backlog.value)
                              <= MAX(1.0, ABS(backlog.value) * 1e-9)
                  )
                """,
                (symbol, source_id),
            ).fetchall()
        ]
        if duplicate_backlog_ids:
            placeholders = ",".join("?" for _ in duplicate_backlog_ids)
            stats["backlog_aliases_deleted"] = int(
                conn.execute(
                    f"DELETE FROM fact_sec_xbrl_fact WHERE fact_id IN ({placeholders})",
                    duplicate_backlog_ids,
                ).rowcount
                or 0
            )
        conn.execute(
            """
            DELETE FROM fact_financial_statement_canonical
            WHERE ticker = ? AND source_id = ? AND model_family = ?
              AND canonical_metric = 'reported_backlog'
              AND EXISTS (
                    SELECT 1
                    FROM fact_financial_statement_canonical rpo
                    WHERE rpo.ticker = fact_financial_statement_canonical.ticker
                      AND rpo.source_id = fact_financial_statement_canonical.source_id
                      AND rpo.model_family = fact_financial_statement_canonical.model_family
                      AND rpo.canonical_metric = 'remaining_performance_obligation'
                      AND rpo.period_end = fact_financial_statement_canonical.period_end
                      AND LOWER(COALESCE(rpo.unit, '')) = LOWER(COALESCE(fact_financial_statement_canonical.unit, ''))
                      AND ABS(rpo.value - fact_financial_statement_canonical.value)
                          <= MAX(1.0, ABS(fact_financial_statement_canonical.value) * 1e-9)
              )
            """,
            (symbol, source_id, model_family),
        )
    if symbol == "OTIS":
        derived_raw_ids = [
            int(row[0])
            for row in conn.execute(
                """
                SELECT raw_fact_id
                FROM fact_sec_xbrl_fact_raw
                WHERE ticker = ? AND source_id = ?
                  AND concept_name = 'RemainingPerformanceObligationCurrent'
                  AND frame LIKE 'footnote_derived:%'
                """,
                (symbol, source_id),
            ).fetchall()
        ]
        if derived_raw_ids:
            placeholders = ",".join("?" for _ in derived_raw_ids)
            mapped_deleted = int(
                conn.execute(
                    f"DELETE FROM fact_sec_xbrl_fact WHERE raw_fact_id IN ({placeholders})",
                    derived_raw_ids,
                ).rowcount
                or 0
            )
            raw_deleted = int(
                conn.execute(
                    f"DELETE FROM fact_sec_xbrl_fact_raw WHERE raw_fact_id IN ({placeholders})",
                    derived_raw_ids,
                ).rowcount
                or 0
            )
            stats["derived_rpo_current_deleted"] = mapped_deleted
            stats["mapped_facts_deleted"] += mapped_deleted
            stats["raw_facts_deleted"] += raw_deleted
        conn.execute(
            """
            DELETE FROM fact_financial_statement_canonical
            WHERE ticker = ? AND source_id = ? AND model_family = ?
              AND canonical_metric = 'rpo_current'
              AND taxonomy = 'sec-footnote'
              AND concept_name = 'RemainingPerformanceObligationCurrent'
            """,
            (symbol, source_id, model_family),
        )
    return stats


def reapply_reviewed_disclosure_policies(
    conn: Any,
    *,
    tickers: Iterable[str],
    model_family: str,
    now: str,
) -> dict[str, int]:
    """Replay issuer policies for stored documents that still require review."""
    symbols = sorted(
        {
            str(ticker or "").strip().upper()
            for ticker in tickers
            if str(ticker or "").strip()
        }
    )
    stats = {
        "documents_replayed": 0,
        "candidate_rows": 0,
        "promoted_raw": 0,
        "promoted_mapped": 0,
    }
    if not symbols:
        return stats
    placeholders = ",".join("?" for _ in symbols)
    rows = [
        dict(row)
        for row in conn.execute(
            f"""
            SELECT c.*
            FROM fact_sec_metric_disclosure_candidate AS c
            WHERE c.model_family = ?
              AND c.ticker IN ({placeholders})
              AND c.candidate_value IS NOT NULL
              AND EXISTS (
                    SELECT 1
                    FROM fact_sec_metric_disclosure_candidate AS pending
                    WHERE pending.ticker = c.ticker
                      AND pending.source_id = c.source_id
                      AND pending.model_family = c.model_family
                      AND pending.accession_number = c.accession_number
                      AND pending.document_name = c.document_name
                      AND pending.candidate_status = 'REVIEW_REQUIRED'
              )
            ORDER BY c.ticker, c.source_id, c.accession_number,
                     c.document_name, c.candidate_key
            """,
            (model_family, *symbols),
        ).fetchall()
    ]
    grouped: dict[
        tuple[str, str, str, str],
        list[dict[str, Any]],
    ] = {}
    for row in rows:
        grouped.setdefault(
            (
                str(row["ticker"]),
                str(row["source_id"]),
                str(row["accession_number"]),
                str(row["document_name"]),
            ),
            [],
        ).append(row)

    touched_sources: set[tuple[str, str]] = set()
    for (ticker, source_id, accession, document_name), document_rows in grouped.items():
        first = document_rows[0]
        filing = {
            "accession_number": accession,
            "form_type": str(first.get("form_type") or ""),
            "filing_date": str(first.get("filing_date") or ""),
            "accepted_at": str(first.get("accepted_at") or ""),
        }
        candidates: list[DisclosureCandidate] = []
        source_detail = PROSE_SOURCE_DETAIL
        for row in document_rows:
            provenance: dict[str, Any] = {}
            try:
                provenance = json.loads(str(row.get("provenance_json") or "{}"))
            except (TypeError, ValueError, json.JSONDecodeError):
                provenance = {}
            source_detail = str(provenance.get("source") or source_detail)
            candidates.append(
                DisclosureCandidate(
                    concept_name=str(row["concept_name"]),
                    metric_name=str(row["metric_name"]),
                    value=float(row["candidate_value"]),
                    unit=str(row.get("unit") or ""),
                    period_start=str(row.get("period_start") or ""),
                    period_end=str(row.get("period_end") or ""),
                    scope=str(row.get("scope") or "unknown"),
                    confidence=float(row.get("confidence") or 0.0),
                    candidate_status=str(row.get("candidate_status") or ""),
                    status_reason=str(row.get("status_reason") or ""),
                    evidence_text=str(row.get("evidence_text") or ""),
                    block_index=int(provenance.get("block_index") or 0),
                    extraction_method=str(
                        row.get("extraction_method") or "filing_html_prose"
                    ),
                )
            )
        resolved = resolve_machinery_disclosure_candidates(
            candidates,
            ticker=ticker,
            filing=filing,
        )
        with conn:
            candidate_count, raw_count, mapped_count = (
                replace_document_candidates_and_facts(
                    conn,
                    ticker=ticker,
                    cik=str(first.get("cik") or ""),
                    source_id=source_id,
                    model_family=model_family,
                    filing=filing,
                    document_name=document_name,
                    candidates=resolved,
                    now=now,
                    source_detail=source_detail,
                )
            )
        stats["documents_replayed"] += 1
        stats["candidate_rows"] += candidate_count
        stats["promoted_raw"] += raw_count
        stats["promoted_mapped"] += mapped_count
        touched_sources.add((ticker, source_id))

    for ticker, source_id in sorted(touched_sources):
        with conn:
            reconciliation = reconcile_machinery_disclosure_facts(
                conn,
                ticker=ticker,
                source_id=source_id,
                model_family=model_family,
                now=now,
            )
        stats["promoted_raw"] -= reconciliation["raw_facts_deleted"]
        stats["promoted_mapped"] -= reconciliation["mapped_facts_deleted"]
    return stats


def accepted_date(value: str, fallback: str = "") -> str:
    text = str(value or "").strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}.*", text):
        return text[:10]
    if re.fullmatch(r"\d{8}.*", text):
        return f"{text[:4]}-{text[4:6]}-{text[6:8]}"
    return str(fallback or "")[:10]


def is_known_by_asof(filing: dict[str, Any], asof: str) -> bool:
    known = accepted_date(str(filing.get("accepted_at") or ""), str(filing.get("filing_date") or ""))
    try:
        datetime.strptime(asof, "%Y-%m-%d")
    except ValueError:
        return False
    return bool(known and known <= asof)
