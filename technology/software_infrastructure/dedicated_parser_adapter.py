from __future__ import annotations

import re
import sqlite3
from dataclasses import replace
from datetime import date, datetime
from pathlib import Path

from dedicated_parser.contracts import (
    AdapterRegistry,
    MetricEvidence,
    MetricRequest,
    MetricRequirement,
    NormalizedFact,
    WorkItem,
)
from dedicated_parser.semantic import SemanticBlock, SemanticDocument, parse_semantic_document


ADAPTER_VERSION = "software_infrastructure_specialized_metrics_v1.2"
SUPPORTED_FORMS = (
    "10-K",
    "10-K/A",
    "10-Q",
    "10-Q/A",
    "20-F",
    "20-F/A",
    "40-F",
    "40-F/A",
    "8-K",
    "8-K/A",
    "6-K",
    "6-K/A",
    "S-1",
    "S-1/A",
    "F-1",
    "F-1/A",
)
METRIC_CONCEPT_PATTERNS: dict[str, tuple[str, ...]] = {
    "remaining_performance_obligation": (
        r"RemainingPerformanceObligation",
        r"TransactionPriceAllocatedToRemainingPerformanceObligation",
        r"UnsatisfiedPerformanceObligation",
    ),
    "current_remaining_performance_obligation": (
        r"RemainingPerformanceObligation.*(?:Current|TwelveMonth|OneYear)",
        r"RemainingPerformanceObligation.*ExpectedToBeRecognized",
    ),
    "deferred_revenue_current": (
        r"ContractWithCustomerLiabilityCurrent",
        r"DeferredRevenueCurrent",
    ),
    "deferred_revenue_noncurrent": (
        r"ContractWithCustomerLiabilityNoncurrent",
        r"DeferredRevenueNoncurrent",
    ),
    "deferred_revenue_total": (
        r"ContractWithCustomerLiability$",
        r"DeferredRevenue$",
        r"ContractLiabilities$",
    ),
    "selling_and_marketing_expense": (
        r"SellingAndMarketingExpense",
        r"SalesAndMarketingExpense",
    ),
    "annual_recurring_revenue": (r"Annual(?:ized)?RecurringRevenue",),
    "net_revenue_retention": (
        r"NetRevenueRetention",
        r"DollarBasedNetRetention",
        r"NetDollarRetention",
    ),
    "subscription_revenue": (r"Subscription(?:AndServices)?Revenue",),
    "disclosed_billings": (r"(?:Calculated)?Billings",),
    "customer_count_threshold": (r"CustomerCount",),
    "customer_concentration_pct": (
        r"RevenueFromMajorCustomer",
        r"CustomerConcentration",
    ),
}
STANDARD_ACCEPTED_METRICS = frozenset(
    {
        "remaining_performance_obligation",
        "deferred_revenue_current",
        "deferred_revenue_noncurrent",
        "deferred_revenue_total",
        "selling_and_marketing_expense",
    }
)
PROSE_PRIMARY_METRICS = frozenset(
    {
        "current_remaining_performance_obligation",
        "annual_recurring_revenue",
        "net_revenue_retention",
        "subscription_revenue",
        "disclosed_billings",
    }
)
PROSE_RECONCILIATION_METRICS = frozenset(
    {
        "remaining_performance_obligation",
        "deferred_revenue_current",
        "deferred_revenue_noncurrent",
        "deferred_revenue_total",
    }
)
PROSE_EVENT_METRICS = frozenset({"customer_count_threshold"})
PROSE_ENABLED_METRICS = (
    PROSE_PRIMARY_METRICS
    | PROSE_RECONCILIATION_METRICS
    | PROSE_EVENT_METRICS
)
PROSE_PATTERNS: dict[str, re.Pattern[str]] = {
    "remaining_performance_obligation": re.compile(
        r"(?<!current\s)\b(?:remaining|unsatisfied)\s+performance\s+obligations?\b"
        r"|\bRPO\b",
        re.IGNORECASE,
    ),
    "current_remaining_performance_obligation": re.compile(
        r"\bcurrent\s+(?:remaining\s+performance\s+obligations?|RPO)\b"
        r"|\bcRPO\b"
        r"|\bremaining\s+performance\s+obligations?\s+due\s+within\s+12\s+months\b",
        re.IGNORECASE,
    ),
    "deferred_revenue_current": re.compile(
        r"\bcurrent\s+deferred\s+revenue\b|\bdeferred\s+revenue,\s*current\b"
        r"|\bdeferred\s+revenue\s*\(\s*current\s*\)",
        re.IGNORECASE,
    ),
    "deferred_revenue_noncurrent": re.compile(
        r"\bnoncurrent\s+deferred\s+revenue\b|\bdeferred\s+revenue,\s*noncurrent\b"
        r"|\bdeferred\s+revenue\s*\(\s*non[- ]?current\s*\)",
        re.IGNORECASE,
    ),
    "deferred_revenue_total": re.compile(r"\bdeferred\s+revenue\b", re.IGNORECASE),
    "selling_and_marketing_expense": re.compile(
        r"\b(?:sales|selling)\s+and\s+marketing\b",
        re.IGNORECASE,
    ),
    "annual_recurring_revenue": re.compile(
        r"\bannual(?:ized)?\s+recurring\s+revenue\b|\bARR\b",
        re.IGNORECASE,
    ),
    "net_revenue_retention": re.compile(
        r"\b(?:net\s+revenue|net\s+dollar|dollar[- ]based\s+net)\s+retention\b",
        re.IGNORECASE,
    ),
    "subscription_revenue": re.compile(r"\bsubscription\s+revenue\b", re.IGNORECASE),
    "disclosed_billings": re.compile(
        r"\b(?:calculated\s+)?billings\b",
        re.IGNORECASE,
    ),
    "customer_count_threshold": re.compile(
        r"\b(?:had|have|serv(?:e|ed|es))\s+(?:more\s+than\s+|over\s+)?"
        r"(?P<count>\d[\d,]*)\s+customers?\b",
        re.IGNORECASE,
    ),
    "customer_concentration_pct": re.compile(
        r"\b(?:customer|customers)\b.{0,120}?\b(?:represented|accounted\s+for|comprised)\b",
        re.IGNORECASE,
    ),
}
MONEY_PATTERN = re.compile(
    r"(?P<currency>US\$|USD|\$|EUR|GBP|\u20ac|\u00a3)?\s*"
    r"(?P<number>\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)\s*"
    r"(?P<scale>billions?|millions?|thousands?|bn|mm|[bmk])?\b",
    re.IGNORECASE,
)
PERCENT_PATTERN = re.compile(r"(?P<number>\d{1,3}(?:\.\d+)?)\s*(?:%|percent)\b", re.IGNORECASE)
PERIODIC_FORMS = frozenset({"10-K", "10-K/A", "10-Q", "10-Q/A", "20-F", "20-F/A", "40-F", "40-F/A"})
MONTH_DATE_TOKEN = (
    r"(?:January|February|March|April|May|June|July|August|"
    r"September|October|November|December|Jan\.?|Feb\.?|Mar\.?|Apr\.?|"
    r"Jun\.?|Jul\.?|Aug\.?|Sep\.?|Sept\.?|Oct\.?|Nov\.?|Dec\.?)"
    r"\s+\d{1,2},\s+\d{4}"
)
PERIOD_CUE_PATTERN = re.compile(
    r"\b(?:as\s+of|ended|ending|through)\s+(?P<date>" + MONTH_DATE_TOKEN + r")\b",
    re.IGNORECASE,
)
DOCUMENT_PERIOD_CUE_PATTERN = re.compile(
    r"\b(?:(?:three|six|nine|twelve)\s+months?\s+(?:ended|ending)|"
    r"(?:fourth|third|second|first|fiscal)?\s*(?:quarter|year|fiscal\s+year)\s+ended|"
    r"(?:results?|period)\b.{0,80}?\bended)\s+(?P<date>" + MONTH_DATE_TOKEN + r")\b",
    re.IGNORECASE,
)
GUIDANCE_PATTERN = re.compile(
    r"\b(?:guidance|outlook|forecast|expectations?|project(?:s|ed|ion)?|"
    r"anticipat(?:e|es|ed)|"
    r"expect(?:s|ed)?\s+to\s+be|is\s+expected\s+to\s+be|"
    r"in\s+the\s+range\s+of|between\s+\$?[\d,.]+\s+(?:million|billion)?\s+and|"
    r"for\s+fiscal\s+(?:year\s+)?20\d{2})\b",
    re.IGNORECASE,
)
FLOW_PATTERN = re.compile(
    r"\b(?:net[- ]new|incremental|increase(?:d)?\s+(?:of|by)|"
    r"decrease(?:d)?\s+(?:of|by)|grew\s+\$|declined\s+\$|"
    r"cash[- ]flow\s+(?:increase|decrease))\b",
    re.IGNORECASE,
)
ARR_THRESHOLD_PATTERN = re.compile(
    r"\b(?:customers?\s+with|number\s+of\s+customers?\s+with|"
    r"customers?\s+(?:spending|greater\s+than|over))\b.{0,100}?"
    r"(?:ARR|annual(?:ized)?\s+recurring\s+revenue)",
    re.IGNORECASE,
)
BOOKED_ARR_PATTERN = re.compile(
    r"\b(?:booked\s+annual\s+recurring\s+revenue|bARR|gross\s+bARR)\b",
    re.IGNORECASE,
)
PURCHASE_ACCOUNTING_PATTERN = re.compile(
    r"\b(?:acquired\s+but\s+not\s+recognized|business[- ]combination\s+accounting|"
    r"purchase[- ]accounting\s+adjustment)\b",
    re.IGNORECASE,
)
SUBSET_PATTERN = re.compile(
    r"\b(?:excluding|subset|sales-led|product[- ]line|service\s+collection|"
    r"next-generation\s+security|cloud\s+ARR|AI\s+customer\s+ARR)\b",
    re.IGNORECASE,
)
SEGMENT_PATTERN = re.compile(r"\b(?:segment|Cylance\s+ARR)\b", re.IGNORECASE)
CURRENT_RPO_SCHEDULE_PATTERN = re.compile(
    r"\b(?:expect(?:s|ed)?\s+to\s+recognize|recognizable|recognized)"
    r".{0,160}?(?P<money>(?:US\$|USD|\$|EUR|GBP|\u20ac|\u00a3)\s*"
    r"(?:\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)\s*"
    r"(?:billions?|millions?|thousands?|bn|mm|[bmk])?)"
    r".{0,100}?\b(?:next\s+(?:12|twelve)\s+months?|within\s+(?:one|1)\s+year)\b",
    re.IGNORECASE,
)


def select_tickers(conn: sqlite3.Connection, asof_date: str) -> list[str]:
    return [
        str(row["ticker"])
        for row in conn.execute(
            """
            SELECT DISTINCT ticker
            FROM dim_universe_membership
            WHERE model_family = 'software_infrastructure'
              AND start_date <= ?
              AND COALESCE(NULLIF(end_date, ''), '9999-12-31') >= ?
            ORDER BY ticker
            """,
            (asof_date, asof_date),
        )
    ]


def get_registry() -> AdapterRegistry:
    requests = tuple(MetricRequest(metric_name, patterns) for metric_name, patterns in METRIC_CONCEPT_PATTERNS.items())
    return AdapterRegistry(
        model_family="software_infrastructure",
        adapter_version=ADAPTER_VERSION,
        supported_forms=SUPPORTED_FORMS,
        source_metrics=requests,
        metric_dependencies={metric.metric_name: metric.metric_name for metric in requests},
        document_keywords=(
            "annual recurring revenue",
            "arr",
            "billings",
            "customer",
            "deferred revenue",
            "net retention",
            "performance obligation",
            "remaining performance",
            "sales and marketing",
            "subscription revenue",
        ),
        metric_requirements={metric.metric_name: MetricRequirement(metric.metric_name) for metric in requests},
    )


def _document_text(path: Path) -> tuple[str, str]:
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        try:
            from pypdf import PdfReader

            reader = PdfReader(path)
            return "\n\n".join(page.extract_text() or "" for page in reader.pages), "pypdf"
        except (ImportError, OSError, ValueError) as exc:
            return "", f"pdf_extraction_failed:{type(exc).__name__}"
    try:
        return path.read_text(encoding="utf-8", errors="replace"), "text_decode"
    except OSError as exc:
        return "", f"document_read_failed:{type(exc).__name__}"


def _scaled_value(
    match: re.Match[str],
    *,
    default_currency: str = "USD",
) -> tuple[float, str]:
    value = float(match.group("number").replace(",", ""))
    scale = str(match.group("scale") or "").lower()
    if scale in {"b", "bn", "billion", "billions"}:
        value *= 1_000_000_000
    elif scale in {"m", "mm", "million", "millions"}:
        value *= 1_000_000
    elif scale in {"k", "thousand", "thousands"}:
        value *= 1_000
    currency = str(match.group("currency") or "").upper()
    unit = (
        str(default_currency or "USD").upper()
        if not currency
        else "USD"
        if currency in {"$", "US$", "USD"}
        else currency
    )
    return value, unit


def _parse_month_date(value: str) -> date | None:
    normalized = value.replace(".", "").replace("Sept ", "Sep ").strip()
    for pattern in ("%B %d, %Y", "%b %d, %Y"):
        try:
            return datetime.strptime(normalized, pattern).date()
        except ValueError:
            continue
    return None


def _iso_date(value: str) -> date | None:
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _document_period_end(semantic: SemanticDocument, item: WorkItem) -> tuple[str, str]:
    filing_date = _iso_date(item.filing.filing_date)
    candidates: list[date] = []
    for block in semantic.blocks:
        for match in DOCUMENT_PERIOD_CUE_PATTERN.finditer(block.search_text):
            parsed = _parse_month_date(match.group("date"))
            if parsed is not None and (filing_date is None or parsed <= filing_date):
                candidates.append(parsed)
    if candidates:
        return max(candidates).isoformat(), "document_fiscal_period_cue"
    report_date = _iso_date(item.filing.report_date)
    if report_date is not None and (item.filing.form_type.upper() in PERIODIC_FORMS or report_date != filing_date):
        return report_date.isoformat(), "sec_report_date"
    return "", "unresolved"


def _candidate_period_end(
    block: SemanticBlock,
    label: re.Match[str],
    *,
    document_period_end: str,
    document_period_source: str,
) -> tuple[str, str]:
    local: list[tuple[int, date]] = []
    for match in PERIOD_CUE_PATTERN.finditer(block.text):
        parsed = _parse_month_date(match.group("date"))
        if parsed is not None:
            local.append((abs(match.start() - label.start()), parsed))
    if local:
        _, parsed = min(local, key=lambda item: (item[0], item[1]))
        return parsed.isoformat(), "local_period_cue"
    return document_period_end, document_period_source


def _table_scale(block: SemanticBlock) -> float:
    context = " ".join((block.preamble_text, *block.header_cells)).lower()
    if re.search(r"\bin\s+billions?\b", context):
        return 1_000_000_000.0
    if re.search(r"\bin\s+millions?\b", context):
        return 1_000_000.0
    if re.search(r"\bin\s+thousands?\b", context):
        return 1_000.0
    return 1.0


def _table_value(
    block: SemanticBlock,
    metric_name: str,
    *,
    default_currency: str,
) -> tuple[float, str] | None:
    if block.kind != "table_row" or not block.cells:
        return None
    pattern = PROSE_PATTERNS[metric_name]
    label_index = next(
        (index for index, cell in enumerate(block.cells) if pattern.search(cell)),
        None,
    )
    if label_index is None:
        return None
    scale = _table_scale(block)
    has_dollar_context = "$" in block.text or "dollar" in block.preamble_text.lower()
    for cell in block.cells[label_index + 1 :]:
        normalized = cell.strip().strip("()$ ")
        if not re.fullmatch(r"\d{1,3}(?:,\d{3})*(?:\.\d+)?|\d+(?:\.\d+)?", normalized):
            continue
        value = float(normalized.replace(",", "")) * scale
        return (
            value,
            "USD"
            if has_dollar_context
            else default_currency.upper()
            if scale != 1.0
            else "",
        )
    return None


def _money_after_label(
    block: SemanticBlock,
    metric_name: str,
    label: re.Match[str],
    *,
    default_currency: str,
) -> tuple[float, str, int] | None:
    table_value = _table_value(
        block,
        metric_name,
        default_currency=default_currency,
    )
    if table_value is not None:
        return table_value[0], table_value[1], len(block.text)
    prefix = block.text[max(0, label.start() - 120) : label.start()]
    preceding = list(MONEY_PATTERN.finditer(prefix))
    if preceding:
        nearest = preceding[-1]
        relationship = prefix[nearest.end() :]
        if re.fullmatch(
            r"\s+(?:is|was)\s+(?:recorded|reported)\s+as\s+",
            relationship,
            re.IGNORECASE,
        ) and (nearest.group("currency") or nearest.group("scale")):
            value, unit = _scaled_value(
                nearest,
                default_currency=default_currency,
            )
            return value, unit, label.start()
    suffix = block.text[label.end() : label.end() + 320]
    for money in MONEY_PATTERN.finditer(suffix):
        if money.group("currency") or money.group("scale"):
            value, unit = _scaled_value(
                money,
                default_currency=default_currency,
            )
            return value, unit, label.end() + money.end()
    return None


def _scope(text: str) -> str:
    if SEGMENT_PATTERN.search(text):
        return "segment"
    if SUBSET_PATTERN.search(text):
        return "subset"
    if re.search(r"\b(?:total|consolidated)\b", text, re.IGNORECASE):
        return "consolidated"
    return "unknown"


def _local_clause_start(text: str, position: int) -> int:
    boundary = max(text.rfind(marker, 0, position) for marker in (".", ";", "\n", "\u2022"))
    return boundary + 1


def _candidate_status(
    *,
    metric_name: str,
    context_before_value: str,
    evidence_text: str,
    period_end: str,
    filing_date: str,
    scope: str,
    value: float,
) -> tuple[str, str, float]:
    filing = _iso_date(filing_date)
    period = _iso_date(period_end)
    if period is not None and filing is not None and period > filing:
        return "REJECTED_POLICY", "forward_period_not_actual", 0.2
    if GUIDANCE_PATTERN.search(context_before_value):
        return "REJECTED_POLICY", "forward_guidance_not_actual", 0.2
    if PURCHASE_ACCOUNTING_PATTERN.search(evidence_text):
        return "REJECTED_POLICY", "purchase_accounting_adjustment_not_balance", 0.2
    if metric_name == "annual_recurring_revenue" and ARR_THRESHOLD_PATTERN.search(evidence_text):
        return "REJECTED_POLICY", "customer_threshold_not_arr_level", 0.2
    if metric_name == "annual_recurring_revenue" and BOOKED_ARR_PATTERN.search(evidence_text):
        return "REJECTED_POLICY", "booked_arr_flow_not_ending_arr", 0.2
    if FLOW_PATTERN.search(context_before_value):
        return "REJECTED_POLICY", "change_or_flow_not_level", 0.2
    if scope == "segment":
        return "REJECTED_POLICY", "segment_value_not_consolidated_metric", 0.3
    if scope == "subset":
        return "REJECTED_POLICY", "subset_value_not_total_metric", 0.3
    if metric_name == "net_revenue_retention" and not 0.50 <= value <= 2.00:
        return "REJECTED_POLICY", "nrr_outside_plausible_range", 0.1
    if (
        metric_name
        not in {"net_revenue_retention", "customer_concentration_pct"}
        and value <= 0
    ):
        return "REJECTED_POLICY", "nonpositive_metric_value", 0.1
    if metric_name in PROSE_RECONCILIATION_METRICS:
        return (
            "REVIEW_REQUIRED",
            "prose_reconciliation_candidate_requires_xbrl_check",
            0.65,
        )
    if metric_name in PROSE_EVENT_METRICS:
        return (
            "REVIEW_REQUIRED",
            "censored_threshold_event_requires_definition_review",
            0.6,
        )
    return "REVIEW_REQUIRED", "prose_candidate_requires_period_unit_scope_review", 0.7


def _suppress_semantic_duplicates(
    evidence: list[MetricEvidence],
) -> list[MetricEvidence]:
    seen: set[tuple[str, float | None, str, str, str]] = set()
    output: list[MetricEvidence] = []
    for row in evidence:
        key = (row.metric_name, row.value, row.unit, row.period_end, row.scope)
        if key in seen and row.status == "REVIEW_REQUIRED":
            output.append(
                replace(
                    row,
                    status="SUPPRESSED_SEMANTIC_DUPLICATE",
                    reason="duplicate_metric_value_period_scope_in_accession",
                    confidence=min(row.confidence, 0.2),
                )
            )
            continue
        seen.add(key)
        output.append(row)
    return output


def _prose_evidence(
    *,
    item: WorkItem,
    document_name: str,
    document_hash: str,
    text: str,
) -> list[MetricEvidence]:
    semantic = parse_semantic_document(text, source_document=document_name)
    document_period_end, document_period_source = _document_period_end(semantic, item)
    requested = {request.metric_name for request in item.requested_metrics}
    evidence: list[MetricEvidence] = []
    for block in semantic.blocks:
        block_text = block.text
        for metric_name in sorted(requested):
            if metric_name not in PROSE_ENABLED_METRICS:
                continue
            label = PROSE_PATTERNS[metric_name].search(block_text)
            schedule = None
            if metric_name == "current_remaining_performance_obligation" and label is None:
                schedule = CURRENT_RPO_SCHEDULE_PATTERN.search(block_text)
                label = schedule
            if label is None:
                continue
            if metric_name == "deferred_revenue_total" and re.search(
                r"\bdeferred\s+revenue\s*[,(]?\s*(?:non[- ]?current|current)\b",
                block_text,
                re.IGNORECASE,
            ):
                continue
            if metric_name == "remaining_performance_obligation":
                current_label = PROSE_PATTERNS["current_remaining_performance_obligation"].search(block_text)
                if current_label is not None and current_label.start() <= label.start() < current_label.end():
                    continue
            value_end = label.end()
            value: float | None = None
            unit = ""
            if metric_name == "customer_count_threshold" and label.groupdict().get("count"):
                value = float(str(label.group("count")).replace(",", ""))
                unit = "count"
                value_end = label.end()
            elif metric_name in {"net_revenue_retention", "customer_concentration_pct"}:
                percent = PERCENT_PATTERN.search(block_text[label.end() : label.end() + 240])
                if percent is not None:
                    value = float(percent.group("number")) / 100.0
                    unit = "ratio"
                    value_end = label.end() + percent.end()
            elif schedule is not None:
                money = MONEY_PATTERN.search(schedule.group("money"))
                if money is not None:
                    value, unit = _scaled_value(
                        money,
                        default_currency=item.filing.company_currency,
                    )
                    value_end = schedule.end()
            else:
                extracted = _money_after_label(
                    block,
                    metric_name,
                    label,
                    default_currency=item.filing.company_currency,
                )
                if extracted is not None:
                    value, unit, value_end = extracted
            if value is None:
                continue
            period_end, period_source = _candidate_period_end(
                block,
                label,
                document_period_end=document_period_end,
                document_period_source=document_period_source,
            )
            evidence_start = max(0, label.start() - 120)
            evidence_end = min(
                len(block_text),
                max(value_end + 220, label.end() + 260),
            )
            evidence_text = block_text[evidence_start:evidence_end]
            policy_end = min(len(block_text), max(label.end(), value_end))
            clause_start = _local_clause_start(block_text, label.start())
            policy_text = block_text[clause_start:policy_end]
            context_before_value = " | ".join(
                part
                for part in (
                    *block.section_path,
                    block.preamble_text,
                    *block.header_cells,
                    policy_text,
                )
                if part
            )
            scope = _scope(policy_text)
            status, reason, confidence = _candidate_status(
                metric_name=metric_name,
                context_before_value=context_before_value,
                evidence_text=evidence_text,
                period_end=period_end,
                filing_date=item.filing.filing_date,
                scope=scope,
                value=value,
            )
            has_explicit_money = any(
                match.group("currency") or match.group("scale")
                for match in MONEY_PATTERN.finditer(block_text[label.end() : min(len(block_text), value_end)])
            )
            if block.kind == "table_row" and _table_scale(block) == 1.0 and not has_explicit_money:
                status = "REJECTED_POLICY"
                reason = "table_scale_unresolved"
                confidence = 0.1
            period_kind = ""
            period_context = context_before_value.lower()
            if re.search(
                r"\b(?:full|fiscal)\s+year\b|\byear\s+ended\b",
                period_context,
            ):
                period_kind = "annual"
            elif re.search(r"\b(?:quarter|three\s+months)\b", period_context):
                period_kind = "quarterly"
            evidence.append(
                MetricEvidence(
                    metric_name=metric_name,
                    concept_name="SoftwareDisclosureProseCandidate",
                    value=value,
                    unit=unit,
                    period_start="",
                    period_end=period_end,
                    scope=scope,
                    confidence=confidence,
                    status=status,
                    reason=reason,
                    evidence_text=evidence_text[:1000],
                    source_document=document_name,
                    extraction_method="software_adapter:semantic_prose",
                    provenance={
                        "adapter_version": ADAPTER_VERSION,
                        "document_sha256": document_hash,
                        "semantic_block_index": block.index,
                        "form_type": item.filing.form_type,
                        "semantic_block_kind": block.kind,
                        "period_source": period_source,
                        "period_kind": period_kind,
                        "source_role": (
                            "prose_primary"
                            if metric_name in PROSE_PRIMARY_METRICS
                            else "structured_reconciliation"
                            if metric_name in PROSE_RECONCILIATION_METRICS
                            else "censored_disclosure_event"
                        ),
                        "censored_flag": int(
                            metric_name in PROSE_EVENT_METRICS
                        ),
                        "company_currency": item.filing.company_currency,
                    },
                )
            )
    return _suppress_semantic_duplicates(evidence)


def extract_metric_evidence(item: WorkItem) -> tuple[MetricEvidence, ...]:
    evidence: list[MetricEvidence] = []
    for document in item.documents:
        if document.is_full_submission:
            continue
        text, method = _document_text(Path(document.path))
        if not text.strip():
            if method.startswith(("document_read_failed", "pdf_extraction_failed")):
                evidence.extend(
                    MetricEvidence(
                        metric_name=request.metric_name,
                        concept_name="DocumentExtractionFailure",
                        value=None,
                        unit="",
                        period_start="",
                        period_end=item.filing.report_date,
                        scope="unknown",
                        confidence=0.0,
                        status="PARSER_FAILURE",
                        reason=method,
                        evidence_text=method,
                        source_document=document.name,
                        extraction_method="software_adapter:document_read",
                        provenance={
                            "adapter_version": ADAPTER_VERSION,
                            "document_sha256": document.content_sha256,
                        },
                    )
                    for request in item.requested_metrics
                )
            continue
        evidence.extend(
            _prose_evidence(
                item=item,
                document_name=document.name,
                document_hash=document.content_sha256,
                text=text,
            )
        )
    unique = {row.evidence_key(model_family=item.model_family, filing=item.filing): row for row in evidence}
    return tuple(
        sorted(
            unique.values(),
            key=lambda row: (
                row.metric_name,
                row.period_end,
                row.value if row.value is not None else -1.0,
                row.source_document,
            ),
        )
    )


def _normalized_metric(fact: NormalizedFact) -> str:
    semantic = re.sub(r"[^a-z0-9]+", "", fact.concept_name.lower())
    for metric_name, patterns in METRIC_CONCEPT_PATTERNS.items():
        if any(re.search(pattern, semantic, re.IGNORECASE) for pattern in patterns):
            return metric_name
    return ""


def map_normalized_facts(
    item: WorkItem,
    facts: tuple[NormalizedFact, ...],
) -> tuple[MetricEvidence, ...]:
    requested = {request.metric_name for request in item.requested_metrics}
    evidence: list[MetricEvidence] = []
    for fact in facts:
        metric_name = _normalized_metric(fact)
        if not metric_name or metric_name not in requested or fact.numeric_value is None:
            continue
        consolidated = str(fact.dimensions_json or "{}").strip() in {"", "{}"}
        standard = fact.taxonomy.lower() in {"us-gaap", "ifrs-full"}
        accepted = consolidated and standard and metric_name in STANDARD_ACCEPTED_METRICS
        evidence.append(
            MetricEvidence(
                metric_name=metric_name,
                concept_name=fact.concept_name,
                value=float(fact.numeric_value),
                unit=fact.unit,
                period_start=fact.period_start,
                period_end=fact.period_end,
                scope="consolidated" if consolidated else "dimensional",
                confidence=0.95 if accepted else 0.75,
                status="ACCEPTED" if accepted else "REVIEW_REQUIRED",
                reason=(
                    "standard_taxonomy_dimensionless_fact"
                    if accepted
                    else "extension_or_dimensional_xbrl_requires_review"
                ),
                evidence_text=f"{fact.concept_name}={fact.numeric_value} {fact.unit}",
                source_document=fact.source_document,
                extraction_method=f"software_adapter:arelle:{fact.provider}",
                provenance={
                    "adapter_version": ADAPTER_VERSION,
                    "taxonomy": fact.taxonomy,
                    "context_id": fact.context_id,
                    "dimensions_json": fact.dimensions_json,
                },
            )
        )
    unique = {row.evidence_key(model_family=item.model_family, filing=item.filing): row for row in evidence}
    return tuple(unique.values())
