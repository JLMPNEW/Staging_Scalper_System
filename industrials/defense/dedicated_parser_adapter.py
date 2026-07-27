from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import replace
from datetime import date, datetime, timedelta
from pathlib import Path

from dedicated_parser.contracts import (
    AdapterRegistry,
    MetricEvidence,
    MetricRequest,
    MetricRequirement,
    NormalizedFact,
    ProductionMetricMapping,
    WorkItem,
)
from dedicated_parser.semantic import SemanticBlock, parse_semantic_document
from industrials.machinery.disclosure_documents import extract_document_text


ADAPTER_VERSION = "defense_specialized_metrics_v1.3"
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_REVIEW_POLICY_PATH = Path(__file__).resolve().parent / "review_policies" / "dedicated_parser_review_policy.csv"
_REVIEW_POLICY_GOLDEN_PATH = _PROJECT_ROOT / "dedicated_parser" / "golden_corpus" / "defense_policy_generated.json"
_SUPPORTED_FORMS = (
    "10-K",
    "10-K/A",
    "10-Q",
    "10-Q/A",
    "20-F",
    "20-F/A",
    "40-F",
    "40-F/A",
    "6-K",
    "6-K/A",
    "8-K",
    "8-K/A",
    "S-1",
    "S-1/A",
    "F-1",
    "F-1/A",
    "F-4",
    "F-4/A",
    "424B3",
    "424B4",
    "10-12B",
    "10-12B/A",
    "10-12G",
    "10-12G/A",
    "S-4",
    "S-4/A",
)
_SOURCE_METRICS = (
    MetricRequest(
        "orders",
        (
            r"(?:Order|Orders|Booking|Bookings|OrderIntake|"
            r"NewAwards|ContractAwards)",
        ),
    ),
    MetricRequest(
        "funded_backlog",
        (
            r"(?:Funded|Authorized|Appropriated).*Backlog",
            r"Backlog.*(?:Funded|Authorized|Appropriated)",
        ),
    ),
    MetricRequest(
        "reported_backlog",
        (r"(?:Backlog|OrderBook|UnfilledOrders)",),
    ),
    MetricRequest(
        "remaining_performance_obligation",
        (
            r"RemainingPerformanceObligation",
            r"TransactionPriceAllocatedToRemainingPerformanceObligation",
            r"UnsatisfiedPerformanceObligation",
        ),
    ),
    MetricRequest(
        "rpo_current",
        (
            r"RemainingPerformanceObligation.*(?:Current|NextTwelveMonths)",
            r"RemainingPerformanceObligation.*ExpectedToBeRecognized",
        ),
    ),
)
_DEPENDENCIES = {
    "orders_yoy_growth": "orders",
    "book_to_bill": "orders",
    "backlog_yoy_growth": "funded_backlog",
    "backlog_to_revenue": "funded_backlog",
    "reported_backlog_yoy_growth": "reported_backlog",
    "reported_backlog_to_revenue": "reported_backlog",
    "rpo_yoy_growth": "remaining_performance_obligation",
    "rpo_to_revenue": "remaining_performance_obligation",
    "rpo_implied_orders": "remaining_performance_obligation",
    "rpo_implied_book_to_bill": "remaining_performance_obligation",
    "contract_load_proxy": "reported_backlog",
    "contract_load_proxy_yoy_growth": "reported_backlog",
    "contract_load_proxy_to_revenue": "reported_backlog",
}
_PRODUCTION_MAPPINGS = {
    "orders": ProductionMetricMapping(
        "orders",
        "orders",
        "duration",
        176,
    ),
    "funded_backlog": ProductionMetricMapping(
        "funded_backlog",
        "backlog",
        "instant",
        176,
    ),
    "reported_backlog": ProductionMetricMapping(
        "reported_backlog",
        "backlog",
        "instant",
        176,
    ),
    "remaining_performance_obligation": ProductionMetricMapping(
        "remaining_performance_obligation",
        "revenue",
        "instant",
        176,
    ),
    "rpo_current": ProductionMetricMapping(
        "rpo_current",
        "revenue",
        "instant",
        176,
    ),
}
_METRIC_PATTERNS = (
    (
        "funded_backlog",
        re.compile(
            r"\b(?:funded|authorized|appropriated)\s+(?:order\s+)?backlog\b|"
            r"\b(?:order\s+)?backlog\s+(?:that\s+is\s+)?"
            r"(?:funded|authorized|appropriated)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "rpo_current",
        re.compile(
            r"\bremaining\s+performance\s+obligations?.{0,80}"
            r"(?:next\s+twelve\s+months?|next\s+12\s+months?|current)\b|"
            r"\bcurrent\s+(?:portion\s+of\s+)?remaining\s+performance"
            r"\s+obligations?\b",
            re.IGNORECASE,
        ),
    ),
    (
        "remaining_performance_obligation",
        re.compile(
            r"\bremaining\s+performance\s+obligations?\b|"
            r"\btransaction\s+price\s+allocated\s+to\s+(?:the\s+)?"
            r"remaining\s+performance\s+obligations?\b|"
            r"\bunsatisfied\s+performance\s+obligations?\b",
            re.IGNORECASE,
        ),
    ),
    (
        "reported_backlog",
        re.compile(
            r"\b(?:total\s+)?(?:order\s+)?backlog\b|"
            r"\border\s+book\b|\bunfilled\s+orders?\b",
            re.IGNORECASE,
        ),
    ),
    (
        "orders",
        re.compile(
            r"\b(?:new\s+)?(?:orders?|bookings?|order\s+intake|"
            r"contract\s+awards?)\b",
            re.IGNORECASE,
        ),
    ),
)
_REJECT_PATTERN = re.compile(
    r"\b(?:maximum\s+potential|contract\s+ceiling|backlog\s+ceiling|"
    r"ceiling\s+value|"
    r"indefinite.delivery.indefinite.quantity|idiq|sales\s+pipeline|"
    r"opportunity\s+pipeline|proposal(?:s)?|letter\s+of\s+intent|"
    r"\bmou\b|memorandum\s+of\s+understanding|reservation(?:s)?|"
    r"non.?binding|unexercised\s+options?|potential\s+options?|"
    r"fair\s+value\s+of\s+acquired\s+backlog|change\s+in\s+backlog)\b",
    re.IGNORECASE,
)
_TRANSACTION_TARGET_PATTERN = re.compile(
    r"\b(?:acquisition|transaction)\s+target\b|"
    r"\bprospective\s+(?:acquisition|transaction)\b|"
    r"\bpro\s+forma\b|"
    r"\bon\s+a\s+combined\s+basis\b|"
    r"\b(?:metrics?|financial\s+information|data)\b.{0,240}"
    r"\b(?:has|have)\s+not\s+been\s+audited\s+by\b",
    re.IGNORECASE | re.DOTALL,
)
_NON_OPERATING_PATTERN = re.compile(
    r"\b(?:purchase\s+orders?|court\s+orders?|protective\s+orders?|"
    r"orders?\s+of\s+the\s+commission|orderly|border)\b",
    re.IGNORECASE,
)
_SEGMENT_PATTERN = re.compile(
    r"\b(?:segment|program|platform|geographic|aeronautics|space\s+systems|"
    r"mission\s+systems|rotary\s+and\s+mission)\b",
    re.IGNORECASE,
)
_TOTAL_SCOPE_PATTERN = re.compile(
    r"\b(?:consolidated|company(?:'s)?|our|total)\s+"
    r"(?:funded\s+)?(?:order\s+)?backlog\b|"
    r"\b(?:consolidated|company(?:'s)?|our|total)\s+remaining\s+"
    r"performance\s+obligations?\b|"
    r"\b(?:consolidated|company(?:'s)?|our|total)\s+"
    r"(?:orders?|bookings?|order\s+intake)\b",
    re.IGNORECASE,
)
_MONEY_PATTERN = re.compile(
    r"(?P<currency>US\$|C\$|CA\$|\$|USD|CAD|GBP|EUR|ILS)\s*"
    r"(?P<value>\(?-?\d[\d,]*(?:\.\d+)?\)?)"
    r"\s*(?P<scale>billions?|millions?|thousands?|bn|mm|m|k)?\b",
    re.IGNORECASE,
)
_ISO_DATE_PATTERN = re.compile(r"\b(20\d{2}-\d{2}-\d{2})\b")
_MONTH_DATE_PATTERN = re.compile(
    r"\b("
    r"(?:January|February|March|April|May|June|July|August|"
    r"September|October|November|December)"
    r"\s+\d{1,2},\s+20\d{2})\b",
    re.IGNORECASE,
)


def select_tickers(
    conn: sqlite3.Connection,
    asof_date: str,
) -> list[str]:
    """Select every PIT-eligible current or historical defense identity."""
    return [
        str(row["ticker"])
        for row in conn.execute(
            """
            SELECT DISTINCT ticker
            FROM dim_universe_membership
            WHERE model_family = 'defense'
              AND start_date <= ?
            ORDER BY ticker
            """,
            (asof_date,),
        )
    ]


def get_registry() -> AdapterRegistry:
    requirements = {request.metric_name: MetricRequirement(request.metric_name) for request in _SOURCE_METRICS}
    requirements.update(
        {
            "orders_yoy_growth": MetricRequirement(
                "orders_yoy_growth",
                mode="comparable_period",
                series_metric="orders",
                minimum_discrete_periods=2,
                lookback_days=550,
            ),
            "book_to_bill": MetricRequirement(
                "book_to_bill",
                mode="series_ttm",
                series_metric="orders",
                minimum_discrete_periods=4,
                lookback_days=460,
            ),
        }
    )
    for dependent_metric in _DEPENDENCIES:
        requirements.setdefault(
            dependent_metric,
            MetricRequirement(dependent_metric),
        )
    return AdapterRegistry(
        model_family="defense",
        adapter_version=ADAPTER_VERSION,
        supported_forms=_SUPPORTED_FORMS,
        source_metrics=_SOURCE_METRICS,
        metric_dependencies=_DEPENDENCIES,
        metric_requirements=requirements,
        production_mappings=_PRODUCTION_MAPPINGS,
        metric_freshness_days={
            "orders": 457,
            "funded_backlog": 457,
            "reported_backlog": 457,
        },
        document_keywords=(
            "backlog",
            "bookings",
            "contract awards",
            "funded backlog",
            "order intake",
            "orders",
            "performance obligations",
            "remaining performance",
        ),
        review_policy_path=str(_REVIEW_POLICY_PATH),
        review_policy_golden_path=str(_REVIEW_POLICY_GOLDEN_PATH),
    )


def _metric_match(text: str) -> tuple[str, re.Match[str]] | None:
    if _NON_OPERATING_PATTERN.search(text):
        return None
    for metric_name, pattern in _METRIC_PATTERNS:
        match = pattern.search(text)
        if match is not None:
            return metric_name, match
    return None


def _normalized_unit(raw: str, fallback: str) -> str:
    text = raw.strip().upper()
    if text in {"$", "US$", "USD"}:
        return "USD"
    if text in {"C$", "CA$", "CAD"}:
        return "CAD"
    if text in {"GBP", "EUR", "ILS"}:
        return text
    if ":" in text:
        text = text.rsplit(":", 1)[-1]
    return text if len(text) == 3 and text.isalpha() else fallback.upper()


def _money_value(
    match: re.Match[str],
    *,
    context: str,
    fallback_currency: str,
) -> tuple[float, str] | None:
    raw_value = match.group("value")
    negative = raw_value.startswith("(") and raw_value.endswith(")")
    try:
        value = float(raw_value.strip("()").replace(",", ""))
    except ValueError:
        return None
    if negative:
        value = -value
    scale = str(match.group("scale") or "").lower()
    if not scale:
        prefix = context[max(0, match.start() - 160) : match.start()]
        scale_match = re.search(
            r"\bin\s+(?:whole\s+)?(billions?|millions?|thousands?)\b",
            prefix,
            re.IGNORECASE,
        )
        scale = str(scale_match.group(1) if scale_match else "").lower()
    multiplier = (
        1_000_000_000.0
        if scale in {"billion", "billions", "bn"}
        else 1_000_000.0
        if scale in {"million", "millions", "mm", "m"}
        else 1_000.0
        if scale in {"thousand", "thousands", "k"}
        else 1.0
    )
    return (
        value * multiplier,
        _normalized_unit(match.group("currency"), fallback_currency),
    )


def _period_end(text: str, fallback: str) -> tuple[str, bool]:
    iso_match = _ISO_DATE_PATTERN.search(text)
    if iso_match:
        return iso_match.group(1), True
    month_match = _MONTH_DATE_PATTERN.search(text)
    if month_match:
        try:
            parsed = datetime.strptime(
                month_match.group(1).title(),
                "%B %d, %Y",
            ).date()
        except ValueError:
            pass
        else:
            return parsed.isoformat(), True
    return fallback[:10], False


def _subtract_months(value: date, months: int) -> date:
    month_index = value.year * 12 + value.month - 1 - months
    year, month_zero = divmod(month_index, 12)
    return date(year, month_zero + 1, 1)


def _duration_start(text: str, period_end: str) -> str:
    try:
        end = date.fromisoformat(period_end)
    except ValueError:
        return ""
    lowered = text.lower()
    if re.search(r"\b(?:year|twelve\s+months?)\s+ended\b", lowered):
        return _subtract_months(end, 11).isoformat()
    if re.search(r"\bnine\s+months?\s+ended\b", lowered):
        return _subtract_months(end, 8).isoformat()
    if re.search(r"\bsix\s+months?\s+ended\b", lowered):
        return _subtract_months(end, 5).isoformat()
    if re.search(r"\b(?:three\s+months?|quarter)\s+ended\b", lowered):
        return _subtract_months(end, 2).isoformat()
    return ""


def _block_scope(text: str) -> str:
    if _SEGMENT_PATTERN.search(text) and not _TOTAL_SCOPE_PATTERN.search(text):
        return "segment"
    if _TOTAL_SCOPE_PATTERN.search(text):
        return "consolidated"
    return "unknown"


def _block_evidence(
    item: WorkItem,
    block: SemanticBlock,
    *,
    source_document: str,
    document_sha256: str,
    extraction_method: str,
    extraction_warning: str,
) -> list[MetricEvidence]:
    text = block.search_text
    matched = _metric_match(text)
    if matched is None:
        return []
    metric_name, metric_match = matched
    money_matches = list(_MONEY_PATTERN.finditer(text))
    if not money_matches:
        return []
    money_match = min(
        money_matches,
        key=lambda candidate: abs(candidate.start() - metric_match.end()),
    )
    parsed_money = _money_value(
        money_match,
        context=text,
        fallback_currency=item.filing.company_currency,
    )
    if parsed_money is None:
        return []
    value, unit = parsed_money
    period_end, explicit_period = _period_end(
        text,
        item.filing.report_date or item.filing.filing_date,
    )
    period_start = _duration_start(text, period_end) if metric_name == "orders" else ""
    scope = _block_scope(text)
    status = "REVIEW_REQUIRED"
    reason = "defense_disclosure_requires_review"
    confidence = 0.72
    if _TRANSACTION_TARGET_PATTERN.search(text):
        status = "REJECTED_POLICY"
        reason = "transaction_target_or_pro_forma_value_not_issuer_consolidated"
        confidence = 0.99
    elif _REJECT_PATTERN.search(text):
        status = "REJECTED_POLICY"
        reason = "nonbinding_ceiling_pipeline_option_or_change_not_metric"
        confidence = 0.99
    elif scope == "segment":
        status = "REJECTED_POLICY"
        reason = "segment_or_program_value_not_consolidated_total"
        confidence = 0.99
    elif value < 0.0:
        status = "REJECTED_POLICY"
        reason = "negative_change_value_not_period_metric_balance"
        confidence = 0.99
    elif scope == "consolidated" and explicit_period and (metric_name != "orders" or period_start):
        status = "ACCEPTED"
        reason = "explicit_consolidated_defense_disclosure"
        confidence = 0.92
    elif not explicit_period:
        reason = "disclosure_period_not_explicit"
    elif metric_name == "orders" and not period_start:
        reason = "orders_duration_not_explicit"
    elif scope != "consolidated":
        reason = "consolidated_scope_not_explicit"
    return [
        MetricEvidence(
            metric_name=metric_name,
            concept_name={
                "orders": "Orders",
                "funded_backlog": "FundedBacklog",
                "reported_backlog": "ReportedBacklog",
                "remaining_performance_obligation": ("RemainingPerformanceObligation"),
                "rpo_current": "RemainingPerformanceObligationCurrent",
            }[metric_name],
            value=value,
            unit=unit,
            period_start=period_start,
            period_end=period_end,
            scope=scope,
            confidence=confidence,
            status=status,
            reason=reason,
            evidence_text=text[:1500],
            source_document=source_document,
            extraction_method="dedicated_parser:defense_semantic_text",
            provenance={
                "adapter_version": ADAPTER_VERSION,
                "document_sha256": document_sha256,
                "document_extraction_method": extraction_method,
                "document_extraction_warning": extraction_warning,
                "semantic_block_index": block.index,
            },
        )
    ]


def extract_metric_evidence(item: WorkItem) -> tuple[MetricEvidence, ...]:
    output: list[MetricEvidence] = []
    requested = {request.metric_name for request in item.requested_metrics}
    for document in item.documents:
        if document.is_full_submission:
            continue
        try:
            extracted = extract_document_text(
                Path(document.path).read_bytes(),
                document_name=document.name,
                enable_pdf_ocr=item.enable_pdf_ocr,
                max_pdf_pages=item.max_pdf_pages,
                max_pdf_bytes=item.max_pdf_bytes,
                pdf_extraction_timeout_sec=(item.pdf_extraction_timeout_seconds),
            )
        except OSError as exc:
            output.extend(
                MetricEvidence(
                    metric_name=metric_name,
                    concept_name="DocumentReadFailure",
                    value=None,
                    unit="",
                    period_start="",
                    period_end=item.filing.report_date,
                    scope="unknown",
                    confidence=0.0,
                    status="PARSER_FAILURE",
                    reason=f"document_read_failed:{type(exc).__name__}",
                    evidence_text=str(exc)[:500],
                    source_document=document.name,
                    extraction_method="dedicated_parser:document_read",
                    provenance={"adapter_version": ADAPTER_VERSION},
                )
                for metric_name in sorted(requested)
            )
            continue
        if not extracted.text.strip():
            if extracted.warning:
                output.extend(
                    MetricEvidence(
                        metric_name=metric_name,
                        concept_name="DocumentExtractionFailure",
                        value=None,
                        unit="",
                        period_start="",
                        period_end=item.filing.report_date,
                        scope="unknown",
                        confidence=0.0,
                        status="PARSER_FAILURE",
                        reason=extracted.warning,
                        evidence_text=extracted.warning,
                        source_document=document.name,
                        extraction_method=(f"dedicated_parser:{extracted.extraction_method}"),
                        provenance={"adapter_version": ADAPTER_VERSION},
                    )
                    for metric_name in sorted(requested)
                )
            continue
        semantic = parse_semantic_document(
            extracted.text,
            source_document=document.name,
        )
        for block in semantic.blocks:
            output.extend(
                evidence
                for evidence in _block_evidence(
                    item,
                    block,
                    source_document=document.name,
                    document_sha256=document.content_sha256,
                    extraction_method=extracted.extraction_method,
                    extraction_warning=extracted.warning,
                )
                if evidence.metric_name in requested
            )
    return _deduplicate_evidence(item, tuple(output))


def _metadata_text(fact: NormalizedFact) -> str:
    try:
        metadata = json.loads(fact.concept_metadata_json or "{}")
    except json.JSONDecodeError:
        metadata = {}
    values: list[str] = [fact.concept_name, fact.taxonomy]
    if isinstance(metadata, dict):
        for value in metadata.values():
            if isinstance(value, str):
                values.append(value)
            elif isinstance(value, list):
                values.extend(str(item) for item in value)
    return " ".join(values)


def _fact_metadata(fact: NormalizedFact) -> dict[str, object]:
    try:
        payload = json.loads(fact.concept_metadata_json or "{}")
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _standard_taxonomy(fact: NormalizedFact) -> bool:
    metadata = _fact_metadata(fact)
    namespace = str(metadata.get("namespace_uri") or "").lower()
    taxonomy = fact.taxonomy.lower()
    return taxonomy in {"us-gaap", "ifrs-full"} or "fasb.org/us-gaap" in namespace or "xbrl.ifrs.org" in namespace


def _timing_dimension(
    fact: NormalizedFact,
) -> tuple[str, str] | None:
    try:
        dimensions = json.loads(fact.dimensions_json or "{}")
    except json.JSONDecodeError:
        return None
    if not isinstance(dimensions, dict) or len(dimensions) != 1:
        return None
    axis, raw_member = next(iter(dimensions.items()))
    if "RevenueRemainingPerformanceObligationExpectedTimingOfSatisfactionStartDateAxis" not in str(axis):
        return None
    member_match = _ISO_DATE_PATTERN.search(str(raw_member or ""))
    if member_match is None:
        return None
    return str(axis), member_match.group(1)


def _fact_metric(fact: NormalizedFact) -> str | None:
    text = _metadata_text(fact)
    compact = re.sub(r"[^a-z0-9]+", "", text.lower())
    if any(
        token in compact
        for token in (
            "changeinbacklog",
            "acquiredbacklogfairvalue",
            "purchaseorder",
            "salespipeline",
            "contractceiling",
            "maximumcontractvalue",
        )
    ):
        return None
    if "remainingperformanceobligation" in compact or "unsatisfiedperformanceobligation" in compact:
        dimensionless = fact.unit.lower() in {"pure", "percent", "%"}
        explicit_percentage = "percentage" in compact or "percent" in compact
        if explicit_percentage or (dimensionless and _timing_dimension(fact) is not None):
            if _timing_dimension(fact) is not None or any(
                token in compact
                for token in (
                    "current",
                    "nexttwelvemonths",
                    "next12months",
                    "withintwelvemonths",
                    "withinoneyear",
                )
            ):
                return "_rpo_current_percentage"
            return None
        if any(token in compact for token in ("current", "nexttwelvemonths", "next12months")):
            return "rpo_current"
        return "remaining_performance_obligation"
    if "backlog" in compact or "orderbook" in compact or "unfilledorders" in compact:
        if any(token in compact for token in ("funded", "authorized", "appropriated")):
            return "funded_backlog"
        return "reported_backlog"
    if any(token in compact for token in ("orderintake", "bookings", "neworders", "contractawards")):
        return "orders"
    return None


def _fact_unit(fact: NormalizedFact) -> str:
    return _normalized_unit(fact.unit, "")


def _monetary_fact_unit(fact: NormalizedFact) -> str:
    unit = _fact_unit(fact)
    return unit if len(unit) == 3 and unit.isalpha() else ""


def _standard_rpo_fact(fact: NormalizedFact, metric_name: str) -> bool:
    return metric_name in {"remaining_performance_obligation", "rpo_current"} and _standard_taxonomy(fact)


def _exact_total_extension(fact: NormalizedFact, metric_name: str) -> bool:
    compact = re.sub(r"[^a-z0-9]+", "", _metadata_text(fact).lower())
    exact_tokens = {
        "orders": ("totalorders", "orderintake", "totalbookings"),
        "funded_backlog": ("fundedbacklog", "authorizedbacklog"),
        "reported_backlog": (
            "totalbacklog",
            "orderbacklog",
            "totalorderbook",
            "unfilledorders",
        ),
        "remaining_performance_obligation": (
            "remainingperformanceobligation",
            "unsatisfiedperformanceobligation",
        ),
        "rpo_current": (
            "remainingperformanceobligationcurrent",
            "remainingperformanceobligationnexttwelvemonths",
        ),
    }
    return any(token in compact for token in exact_tokens[metric_name])


def _fact_evidence(
    fact: NormalizedFact,
    *,
    metric_name: str,
    concept_name: str,
    value: float | None,
    unit: str,
    scope: str,
    status: str,
    reason: str,
    confidence: float,
    provenance: dict[str, object] | None = None,
) -> MetricEvidence:
    return MetricEvidence(
        metric_name=metric_name,
        concept_name=concept_name,
        value=value,
        unit=unit,
        period_start="",
        period_end=fact.period_end,
        scope=scope,
        confidence=confidence,
        status=status,
        reason=reason,
        evidence_text=(
            f"{fact.taxonomy}:{fact.concept_name}="
            f"{fact.numeric_value if fact.numeric_value is not None else ''} "
            f"{unit} context={fact.context_id} "
            f"dimensions={fact.dimensions_json}"
        )[:1500],
        source_document=fact.source_document,
        extraction_method=(f"dedicated_parser:{fact.provider}:normalized_fact"),
        provenance={
            "adapter_version": ADAPTER_VERSION,
            "context_id": fact.context_id,
            "dimensions_json": fact.dimensions_json,
            "concept_metadata": _fact_metadata(fact),
            **(provenance or {}),
        },
    )


def _dimensionless_rpo_totals(
    facts: tuple[NormalizedFact, ...],
) -> dict[tuple[str, str], list[NormalizedFact]]:
    output: dict[tuple[str, str], list[NormalizedFact]] = {}
    for fact in facts:
        if (
            fact.numeric_value is None
            or _fact_metric(fact) != "remaining_performance_obligation"
            or fact.scope != "consolidated"
            or not _standard_taxonomy(fact)
            or _timing_dimension(fact) is not None
            or not _monetary_fact_unit(fact)
            or float(fact.numeric_value) <= 0.0
        ):
            continue
        output.setdefault(
            (fact.period_end, fact.source_document),
            [],
        ).append(fact)
    return output


def _unique_total(
    rows: list[NormalizedFact],
) -> NormalizedFact | None:
    values = {round(float(row.numeric_value or 0.0), 6) for row in rows}
    return rows[0] if rows and len(values) == 1 else None


def _timing_schedule_evidence(
    facts: tuple[NormalizedFact, ...],
) -> tuple[list[MetricEvidence], set[int]]:
    grouped: dict[
        tuple[str, str, str],
        list[tuple[str, NormalizedFact]],
    ] = {}
    for fact in facts:
        timing = _timing_dimension(fact)
        if (
            timing is None
            or fact.numeric_value is None
            or _fact_metric(fact) != "remaining_performance_obligation"
            or not _monetary_fact_unit(fact)
        ):
            continue
        grouped.setdefault(
            (fact.period_end, _monetary_fact_unit(fact), fact.source_document),
            [],
        ).append((timing[1], fact))

    totals = _dimensionless_rpo_totals(facts)
    output: list[MetricEvidence] = []
    consumed: set[int] = set()
    for (period_end, unit, source_document), rows in grouped.items():
        values_by_member: dict[str, list[NormalizedFact]] = {}
        for member, fact in rows:
            values_by_member.setdefault(member, []).append(fact)
        if len(values_by_member) < 2 or any(
            len({round(float(row.numeric_value or 0.0), 6) for row in member_rows}) != 1
            for member_rows in values_by_member.values()
        ):
            continue
        ordered = sorted(
            (
                member,
                member_rows[0],
            )
            for member, member_rows in values_by_member.items()
        )
        template = ordered[0][1]
        try:
            period_date = date.fromisoformat(period_end)
            member_dates = [date.fromisoformat(member) for member, _ in ordered]
        except ValueError:
            continue
        gaps = [later - earlier for earlier, later in zip(member_dates, member_dates[1:])]
        schedule_valid = period_date - timedelta(days=7) <= member_dates[0] <= period_date + timedelta(days=31) and all(
            timedelta(days=330) <= gap <= timedelta(days=400) for gap in gaps
        )
        matching_totals = totals.get(
            (period_end, source_document),
            [],
        )
        dimensionless_total = _unique_total(matching_totals)
        total_conflict = bool(matching_totals and dimensionless_total is None)
        current_value = float(ordered[0][1].numeric_value or 0.0)
        current_fraction: float | None = None
        fraction_valid = True
        if dimensionless_total is not None:
            total_value = float(dimensionless_total.numeric_value or 0.0)
            current_fraction = current_value / total_value if total_value > 0.0 else 0.0
            fraction_valid = 0.05 <= current_fraction <= 1.0
        accepted = schedule_valid and fraction_valid and not total_conflict and _standard_taxonomy(template)
        schedule_total = sum(float(fact.numeric_value or 0.0) for _, fact in ordered)
        if dimensionless_total is not None:
            total_status = "REJECTED_POLICY"
            total_reason = "dimensionless_total_supersedes_timing_dimension_sum"
            total_confidence = 0.99
        elif accepted:
            total_status = "ACCEPTED"
            total_reason = "standard_timing_dimension_exhaustive_sum"
            total_confidence = 0.97
        else:
            total_status = "REVIEW_REQUIRED"
            total_reason = "timing_dimension_incomplete_schedule_requires_defense_review"
            total_confidence = 0.72
        output.append(
            _fact_evidence(
                template,
                metric_name="remaining_performance_obligation",
                concept_name=("RemainingPerformanceObligationTimingDimensionAggregate"),
                value=schedule_total,
                unit=unit,
                scope="consolidated",
                status=total_status,
                reason=total_reason,
                confidence=total_confidence,
                provenance={
                    "derivation_type": "exhaustive_timing_dimension_sum",
                    "timing_schedule_complete": schedule_valid,
                    "dimensionless_total_available": (dimensionless_total is not None),
                    "component_context_ids": [fact.context_id for _, fact in ordered],
                },
            )
        )
        if total_conflict:
            current_status = "REVIEW_REQUIRED"
            current_reason = "conflicting_dimensionless_rpo_totals"
            current_confidence = 0.70
        elif not schedule_valid:
            current_status = "REJECTED_POLICY"
            current_reason = "timing_dimension_current_bucket_not_twelve_months"
            current_confidence = 0.99
        elif not fraction_valid:
            current_status = "REJECTED_POLICY"
            current_reason = "timing_dimension_current_fraction_outside_valid_range"
            current_confidence = 0.99
        elif accepted:
            current_status = "ACCEPTED"
            current_reason = "standard_earliest_timing_dimension_twelve_month_bucket"
            current_confidence = 0.97
        else:
            current_status = "REVIEW_REQUIRED"
            current_reason = "timing_dimension_current_requires_defense_review"
            current_confidence = 0.72
        output.append(
            _fact_evidence(
                ordered[0][1],
                metric_name="rpo_current",
                concept_name=("RemainingPerformanceObligationCurrentTimingBucket"),
                value=current_value,
                unit=unit,
                scope="consolidated",
                status=current_status,
                reason=current_reason,
                confidence=current_confidence,
                provenance={
                    "derivation_type": "earliest_timing_dimension_bucket",
                    "timing_axis_member": ordered[0][0],
                    "next_timing_axis_member": ordered[1][0],
                    "current_fraction_of_dimensionless_total": (current_fraction),
                },
            )
        )
        consumed.update(id(fact) for _, fact in rows)
    return output, consumed


def _timing_percentage_evidence(
    facts: tuple[NormalizedFact, ...],
) -> tuple[list[MetricEvidence], set[int]]:
    totals = _dimensionless_rpo_totals(facts)
    output: list[MetricEvidence] = []
    consumed: set[int] = set()
    for fact in facts:
        if fact.numeric_value is None or _fact_metric(fact) != "_rpo_current_percentage":
            continue
        consumed.add(id(fact))
        percentage = float(fact.numeric_value)
        if percentage > 1.0:
            percentage /= 100.0
        timing = _timing_dimension(fact)
        compact = re.sub(
            r"[^a-z0-9]+",
            "",
            _metadata_text(fact).lower(),
        )
        explicit_current_concept = any(
            token in compact
            for token in (
                "current",
                "nexttwelvemonths",
                "next12months",
                "withintwelvemonths",
                "withinoneyear",
            )
        )
        timing_valid = explicit_current_concept
        timing_member = ""
        timing_delta_days: int | None = None
        if timing is not None:
            timing_member = timing[1]
            try:
                period_date = date.fromisoformat(fact.period_end)
                member_date = date.fromisoformat(timing_member)
            except ValueError:
                timing_valid = False
            else:
                timing_delta_days = (member_date - period_date).days
                timing_valid = 0 <= timing_delta_days <= 400
        matching_totals = totals.get(
            (fact.period_end, fact.source_document),
            [],
        )
        total = _unique_total(matching_totals)
        percentage_valid = 0.0 < percentage <= 1.0
        accepted = total is not None and percentage_valid and timing_valid and _standard_taxonomy(fact)
        if not percentage_valid:
            status = "REJECTED_POLICY"
            reason = "timing_dimension_current_fraction_outside_valid_range"
            confidence = 0.99
        elif not timing_valid:
            status = "REJECTED_POLICY"
            reason = "timing_dimension_current_bucket_not_twelve_months"
            confidence = 0.99
        elif total is None:
            status = "REVIEW_REQUIRED"
            reason = "timing_percentage_missing_unique_consolidated_total_rpo"
            confidence = 0.72
        elif accepted:
            status = "ACCEPTED"
            reason = "standard_timing_percentage_times_consolidated_total_rpo"
            confidence = 0.97
        else:
            status = "REVIEW_REQUIRED"
            reason = "timing_percentage_requires_defense_review"
            confidence = 0.72
        value = float(total.numeric_value or 0.0) * percentage if total is not None else None
        output.append(
            _fact_evidence(
                fact,
                metric_name="rpo_current",
                concept_name=("RemainingPerformanceObligationCurrentFromTimingPercentage"),
                value=value,
                unit=_fact_unit(total) if total is not None else "",
                scope="consolidated",
                status=status,
                reason=reason,
                confidence=confidence,
                provenance={
                    "derivation_type": ("explicit_timing_percentage_times_total_rpo"),
                    "explicit_percentage": percentage,
                    "percentage_context_id": fact.context_id,
                    "timing_axis": timing[0] if timing is not None else "",
                    "timing_axis_member": timing_member,
                    "timing_delta_days": timing_delta_days,
                    "total_rpo_context_id": (total.context_id if total is not None else ""),
                    "total_rpo_concept_name": (total.concept_name if total is not None else ""),
                    "total_rpo_value": (total.numeric_value if total is not None else None),
                },
            )
        )
    return output, consumed


def map_normalized_facts(
    item: WorkItem,
    facts: tuple[NormalizedFact, ...],
) -> tuple[MetricEvidence, ...]:
    requested = {request.metric_name for request in item.requested_metrics}
    output: list[MetricEvidence] = []
    timing_evidence, consumed_timing = _timing_schedule_evidence(facts)
    percentage_evidence, consumed_percentage = _timing_percentage_evidence(facts)
    if "remaining_performance_obligation" in requested:
        output.extend(row for row in timing_evidence if row.metric_name == "remaining_performance_obligation")
    if "rpo_current" in requested:
        output.extend(row for row in (*timing_evidence, *percentage_evidence) if row.metric_name == "rpo_current")
    for fact in facts:
        if id(fact) in consumed_timing | consumed_percentage:
            continue
        metric_name = _fact_metric(fact)
        if (
            metric_name is None
            or metric_name.startswith("_")
            or metric_name not in requested
            or fact.numeric_value is None
        ):
            continue
        value = float(fact.numeric_value)
        unit = _fact_unit(fact)
        period_shape_valid = bool(fact.period_start) if metric_name == "orders" else not bool(fact.period_start)
        standard = _standard_rpo_fact(fact, metric_name)
        exact_extension = _exact_total_extension(fact, metric_name)
        status = "REVIEW_REQUIRED"
        reason = "extension_or_period_semantics_require_defense_review"
        confidence = 0.78
        if fact.scope != "consolidated":
            status = "REJECTED_POLICY"
            reason = "dimensional_or_segment_fact_not_consolidated"
            confidence = 0.99
        elif metric_name in {
            "remaining_performance_obligation",
            "rpo_current",
        } and not _monetary_fact_unit(fact):
            status = "REJECTED_POLICY"
            reason = "rpo_amount_requires_monetary_unit"
            confidence = 0.99
        elif value < 0.0:
            status = "REJECTED_POLICY"
            reason = "negative_change_value_not_period_metric_balance"
            confidence = 0.99
        elif not period_shape_valid:
            reason = "metric_period_type_mismatch"
        elif standard:
            status = "ACCEPTED"
            reason = "standard_taxonomy_consolidated_rpo_fact"
            confidence = 0.98
        elif exact_extension:
            status = "ACCEPTED"
            reason = "explicit_consolidated_defense_extension_fact"
            confidence = 0.92
        output.append(
            MetricEvidence(
                metric_name=metric_name,
                concept_name=fact.concept_name,
                value=value,
                unit=unit,
                period_start=fact.period_start,
                period_end=fact.period_end,
                scope=fact.scope,
                confidence=confidence,
                status=status,
                reason=reason,
                evidence_text=(f"{fact.taxonomy}:{fact.concept_name}={value:g} {unit}"),
                source_document=fact.source_document,
                extraction_method=(f"dedicated_parser:{fact.provider}:normalized_fact"),
                provenance={
                    "adapter_version": ADAPTER_VERSION,
                    "context_id": fact.context_id,
                    "dimensions_json": fact.dimensions_json,
                    "concept_metadata": json.loads(fact.concept_metadata_json or "{}"),
                },
            )
        )
    return _deduplicate_evidence(item, tuple(output))


def _deduplicate_evidence(
    item: WorkItem,
    evidence: tuple[MetricEvidence, ...],
) -> tuple[MetricEvidence, ...]:
    status_rank = {
        "ACCEPTED": 4,
        "REJECTED_POLICY": 3,
        "REVIEW_REQUIRED": 2,
        "PARSER_FAILURE": 1,
    }
    grouped: dict[
        tuple[str, str, str, str, str, str],
        list[MetricEvidence],
    ] = {}
    for row in evidence:
        key = (
            row.metric_name,
            row.period_start,
            row.period_end,
            row.unit.upper(),
            row.scope,
            row.source_document,
        )
        grouped.setdefault(key, []).append(row)
    output: list[MetricEvidence] = []
    for rows in grouped.values():
        accepted_values = {
            round(float(row.value), 6) for row in rows if row.status == "ACCEPTED" and row.value is not None
        }
        if len(accepted_values) > 1:
            output.extend(
                replace(
                    row,
                    status="REVIEW_REQUIRED",
                    confidence=min(row.confidence, 0.70),
                    reason="conflicting_values_same_metric_period_scope",
                )
                for row in rows
            )
            continue
        winner = max(
            rows,
            key=lambda row: (
                status_rank.get(row.status, 0),
                row.confidence,
                row.concept_name,
                row.evidence_text,
            ),
        )
        output.append(winner)
    unique = {
        row.evidence_key(
            model_family=item.model_family,
            filing=item.filing,
        ): row
        for row in output
    }
    return tuple(
        sorted(
            unique.values(),
            key=lambda row: (
                row.metric_name,
                row.period_end,
                row.period_start,
                row.value if row.value is not None else float("-inf"),
                row.status,
                row.source_document,
            ),
        )
    )


def postprocess_metric_evidence(
    item: WorkItem,
    evidence: tuple[MetricEvidence, ...],
) -> tuple[MetricEvidence, ...]:
    return _deduplicate_evidence(item, evidence)
