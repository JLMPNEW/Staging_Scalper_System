from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import asdict, replace
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from dedicated_parser.contracts import (
    AdapterRegistry,
    MetricEvidence,
    MetricRequest,
    NormalizedFact,
    WorkItem,
)
from dedicated_parser.semantic import (
    SemanticBlock,
    SemanticDocument,
    parse_semantic_document,
)
from industrials.machinery.disclosure_candidates import (
    DisclosureCandidate,
    extract_machinery_prose_candidates,
    resolve_machinery_disclosure_candidates,
)
from industrials.machinery.disclosure_documents import extract_document_text


ADAPTER_VERSION = "machinery_specialized_metrics_v2.7"
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_REVIEW_POLICY_PATH = (
    Path(__file__).resolve().parent
    / "review_policies"
    / "dedicated_parser_review_policy.csv"
)
_REVIEW_POLICY_GOLDEN_PATH = (
    _PROJECT_ROOT
    / "dedicated_parser"
    / "golden_corpus"
    / "machinery_policy_generated.json"
)


def select_tickers(
    conn: sqlite3.Connection,
    asof_date: str,
) -> list[str]:
    return [
        str(row["ticker"])
        for row in conn.execute(
            """
            SELECT DISTINCT ticker
            FROM dim_universe_membership
            WHERE model_family = 'machinery'
              AND start_date <= ?
              AND COALESCE(end_date, '9999-12-31') >= ?
            ORDER BY ticker
            """,
            (asof_date, asof_date),
        )
    ]


def get_registry() -> AdapterRegistry:
    return AdapterRegistry(
        model_family="machinery",
        adapter_version=ADAPTER_VERSION,
        supported_forms=(
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
        ),
        source_metrics=(
            MetricRequest(
                "orders",
                (
                    r"(?:Order|Orders|Booking|Bookings|OrderIntake|"
                    r"NewAwards|OrderReceived)",
                ),
            ),
            MetricRequest(
                "funded_backlog",
                (
                    r"(?:Funded|Authorized).*Backlog",
                    r"Backlog.*(?:Funded|Authorized)",
                ),
            ),
            MetricRequest(
                "reported_backlog",
                (
                    r"(?:Backlog|OrderBook|UnfilledOrders)",
                ),
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
                    r"(?:Revenue)?RemainingPerformanceObligationPercentage",
                    r"RemainingPerformanceObligation.*ExpectedToBeRecognized",
                ),
            ),
        ),
        metric_dependencies={
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
        },
        document_keywords=(
            "backlog",
            "booking",
            "contract",
            "customer",
            "inventory",
            "order",
            "performance obligation",
            "remaining performance",
            "revenue",
            "segment",
        ),
        review_policy_path=str(_REVIEW_POLICY_PATH),
        review_policy_golden_path=str(_REVIEW_POLICY_GOLDEN_PATH),
    )


_TABLE_METRIC_PATTERNS: tuple[tuple[str, str, re.Pattern[str]], ...] = (
    (
        "RemainingPerformanceObligation",
        "remaining_performance_obligation",
        re.compile(
            r"\b(?:remaining|unsatisfied)\s+performance\s+obligations?\b"
            r"|\btransaction\s+price\s+allocated\s+to\s+remaining\b",
            re.IGNORECASE,
        ),
    ),
    (
        "FundedBacklog",
        "funded_backlog",
        re.compile(
            r"\b(?:funded|authorized|appropriated)\s+(?:order\s+)?backlog\b",
            re.IGNORECASE,
        ),
    ),
    (
        "ReportedBacklog",
        "reported_backlog",
        re.compile(
            r"\b(?:reported\s+|total\s+|consolidated\s+|order\s+)?backlog\b"
            r"|\border\s+book\b|\bunfilled\s+(?:open\s+)?orders\b",
            re.IGNORECASE,
        ),
    ),
    (
        "Orders",
        "orders",
        re.compile(
            r"\b(?:new\s+)?orders?\b|\bbookings?\b|\border\s+intake\b"
            r"|\bnew\s+awards?\b",
            re.IGNORECASE,
        ),
    ),
)
_TABLE_MONEY_PATTERN = re.compile(
    r"(?P<negative>\()?\s*"
    r"(?P<currency>U\.S\.\s*\$|US\$|USD|CA\$|C\$|CAD|AU\$|A\$|AUD|"
    r"EUR|\u20ac|GBP|\u00a3|CHF|\$)?\s*"
    r"(?P<number>\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)\s*"
    r"(?P<scale>billions?|millions?|thousands?|bn|mm|[bmk])?\s*"
    r"(?P<close>\))?",
    re.IGNORECASE,
)
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
_EXPLICIT_DATE = re.compile(
    rf"\b(?P<month>{_MONTH_PATTERN})\.?\s+(?P<day>\d{{1,2}}),?\s+"
    r"(?P<year>(?:19|20)\d{2})\b",
    re.IGNORECASE,
)
_ISO_DATE = re.compile(r"\b((?:19|20)\d{2}-\d{2}-\d{2})\b")
_CURRENT_HORIZON = re.compile(
    r"\b(?:(?:within|over)\s+(?:the\s+)?next|next)\s+"
    r"(?:12|twelve)\s+months?\b"
    r"|\bwithin\s+(?:one|1)\s+year\b",
    re.IGNORECASE,
)
_CURRENT_PERCENT = re.compile(
    r"\b(?P<percent>\d{1,3}(?:\.\d+)?)\s*%",
    re.IGNORECASE,
)
_NON_OPERATING_BACKLOG_TABLE = re.compile(
    r"\b(?:accumulated\s+amortization|estimated\s+fair\s+value|"
    r"estimated\s+useful\s+life|finite[-\s]+lived\s+(?:asset|intangible)|"
    r"finite[-\s]+lived\s+backlog|"
    r"gross\s+(?:asset|carrying\s+amount)|"
    r"net\s+carrying\s+amount|customer\s+relationships?\s+and\s+backlog|"
    r"weighted\s+average\s+amortization|"
    r"(?:carrying\s+value|step[-\s]*up|fair\s+value).{0,160}\bbacklog\b|"
    r"\bbacklog\b.{0,160}(?:carrying\s+value|step[-\s]*up|fair\s+value))\b",
    re.IGNORECASE,
)
_NON_COMMERCIAL_ORDER_TABLE = re.compile(
    r"\bin\s+order\s+to\b"
    r"|\border\s+(?:of\s+(?:a\s+)?court|preventing|suspending)\b"
    r"|\bjudgment\s+or\s+order\b"
    r"|\btask\s+order\s+no\.?\b"
    r"|\binterest\s+rate\s+swaps?\b"
    r"|\b(?:founder|ordinary|common)\s+shares?\b.{0,240}\border\b",
    re.IGNORECASE,
)
_NON_OPERATING_FACT = re.compile(
    r"(?:businesscombination|businessacquisition|proforma).{0,100}backlog"
    r"|backlog.{0,100}(?:amortization|intangible|fairvalue)"
    r"|identifiableassets?.{0,100}backlog",
    re.IGNORECASE,
)

_REVIEWED_RPO_DIMENSION_AGGREGATIONS: dict[
    str,
    tuple[str, frozenset[str]],
] = {
    "AEBI": (
        "StatementGeographicalAxis",
        frozenset(
            {
                "NorthAmericaMember",
                "EuropeAndRestOfTheWorldMember",
            }
        ),
    ),
    "BE": (
        "ProductOrServiceAxis",
        frozenset({"ProductMember", "ServiceMember"}),
    ),
    "SHMD": (
        "ProductsAndServicesAxis",
        frozenset(
            {
                "RevenueFromSalesInstallationDevelopmentAndWarrantiesOfMachinesMember",
                "RevenueFromSparePartsAndServicesMember",
            }
        ),
    ),
    "XOS": (
        "ProductOrServiceAxis",
        frozenset(
            {
                "SoftwareServicesMember",
                "PowertrainEngineeringServicesMember",
            }
        ),
    ),
}
_REVIEWED_CONSOLIDATED_EXTENSION_FACTS = frozenset(
    {
        (
            "LNN",
            "ContractWithCustomerUnsatisfiedPerformanceObligationAmount",
        ),
    }
)


def _table_metric(text: str) -> tuple[str, str] | None:
    for concept_name, metric_name, pattern in _TABLE_METRIC_PATTERNS:
        if pattern.search(text):
            return concept_name, metric_name
    return None


def _scale_from_text(text: str) -> float:
    normalized = text.lower()
    if re.search(r"\b(?:in|amounts?\s+in)\s+billions?\b|\(\s*billions?\s*\)", normalized):
        return 1_000_000_000.0
    if re.search(r"\b(?:in|amounts?\s+in)\s+millions?\b|\(\s*millions?\s*\)", normalized):
        return 1_000_000.0
    if re.search(r"\b(?:in|amounts?\s+in)\s+thousands?\b|\(\s*thousands?\s*\)", normalized):
        return 1_000.0
    return 1.0


def _money_from_text(
    text: str,
    *,
    context: str,
    company_currency: str,
    minimum_value: float = 1_000_000.0,
) -> tuple[float, str] | None:
    normalized_cell = " ".join(text.split())
    if re.search(
        r"%|\b(?:days?|months?|years?|quarter(?:s|ly)?|"
        r"mws?|gws?|shares?|units?)\b",
        normalized_cell,
        re.IGNORECASE,
    ):
        return None
    if re.fullmatch(
        rf"(?:{_MONTH_PATTERN})\.?\s+\d{{1,2}},?",
        normalized_cell,
        re.IGNORECASE,
    ) or _EXPLICIT_DATE.fullmatch(normalized_cell) or _ISO_DATE.fullmatch(
        normalized_cell
    ):
        return None
    value_text = _ISO_DATE.sub(" ", normalized_cell)
    value_text = _EXPLICIT_DATE.sub(" ", value_text)
    match = _TABLE_MONEY_PATTERN.search(value_text)
    if match is None:
        return None
    raw_number = str(match.group("number") or "")
    explicit_currency = str(match.group("currency") or "")
    explicit_scale = str(match.group("scale") or "").lower()
    has_numeric_signal = bool(
        explicit_currency
        or explicit_scale
        or "," in raw_number
        or _scale_from_text(context) != 1.0
    )
    if not has_numeric_signal:
        return None
    value = float(raw_number.replace(",", ""))
    scale = {
        "b": 1_000_000_000.0,
        "bn": 1_000_000_000.0,
        "billion": 1_000_000_000.0,
        "billions": 1_000_000_000.0,
        "m": 1_000_000.0,
        "mm": 1_000_000.0,
        "million": 1_000_000.0,
        "millions": 1_000_000.0,
        "k": 1_000.0,
        "thousand": 1_000.0,
        "thousands": 1_000.0,
    }.get(explicit_scale, _scale_from_text(context))
    if match.group("negative") and match.group("close"):
        value *= -1.0
    if value * scale < minimum_value:
        return None
    currency_token = explicit_currency.upper().replace(" ", "")
    unit = (
        "CAD"
        if currency_token in {"CA$", "C$", "CAD"}
        else "AUD"
        if currency_token in {"AU$", "A$", "AUD"}
        else "EUR"
        if currency_token in {"EUR", "\u20ac"}
        else "GBP"
        if currency_token in {"GBP", "\u00a3"}
        else "CHF"
        if currency_token == "CHF"
        else "USD"
        if currency_token in {"U.S.$", "US$", "USD"}
        else str(company_currency or "USD").upper()
    )
    return value * scale, unit


def _date_from_text(text: str, *, fallback: str) -> str:
    iso = _ISO_DATE.search(text)
    if iso:
        return iso.group(1)
    explicit = _EXPLICIT_DATE.search(text)
    if explicit:
        month = _MONTHS[explicit.group("month").lower().rstrip(".")]
        try:
            return date(
                int(explicit.group("year")),
                month,
                int(explicit.group("day")),
            ).isoformat()
        except ValueError:
            return fallback
    year_matches = re.findall(r"\b((?:19|20)\d{2})\b", text)
    if len(year_matches) == 1:
        return f"{year_matches[0]}-12-31"
    return fallback


def _duration_start(period_end: str, context: str) -> str:
    if not period_end:
        return ""
    try:
        end = date.fromisoformat(period_end)
    except ValueError:
        return ""
    normalized = context.lower()
    months = (
        3
        if re.search(r"\bthree\s+months?\b|\bquarter\b", normalized)
        else 6
        if re.search(r"\bsix\s+months?\b", normalized)
        else 9
        if re.search(r"\bnine\s+months?\b", normalized)
        else 12
        if re.search(r"\b(?:twelve\s+months?|year)\b", normalized)
        else 0
    )
    if not months:
        return ""
    month_index = end.year * 12 + end.month - months
    return date(
        month_index // 12,
        month_index % 12 + 1,
        1,
    ).isoformat()


def _orders_period_start(
    period_end: str,
    context: str,
    *,
    form_type: str,
) -> str:
    explicit = _duration_start(period_end, context)
    if explicit:
        return explicit
    if form_type.upper() in {"10-K", "10-K/A", "20-F", "20-F/A", "40-F", "40-F/A"}:
        return _duration_start(period_end, "year ended")
    return ""


def _semantic_scope(text: str) -> str:
    normalized = text.lower()
    if re.search(r"\b(?:consolidated|total\s+company|company-wide|overall)\b", normalized):
        return "consolidated"
    if re.search(r"\b(?:segment|division|business\s+unit|geographic)\b", normalized):
        return "segment"
    return "unknown"


def _table_cell_scope(
    *,
    header: str,
    row_label: str,
    section_path: tuple[str, ...],
) -> str:
    if re.fullmatch(
        r"\s*(?:total|consolidated|overall|company(?:-wide)?)\s*",
        row_label,
        re.IGNORECASE,
    ):
        return "consolidated"
    header_scope = _semantic_scope(header)
    if header_scope != "unknown":
        return header_scope
    row_scope = _semantic_scope(row_label)
    if row_scope != "unknown":
        return row_scope
    section_scope = _semantic_scope(" ".join(section_path))
    return section_scope


def _table_candidate_status(
    *,
    scope: str,
    period_start: str,
    period_end: str,
    unit: str,
    metric_name: str,
    period_verified: bool,
) -> tuple[str, str, float]:
    complete_period = bool(
        period_end
        and unit
        and (metric_name != "orders" or period_start)
    )
    if scope == "consolidated" and complete_period and period_verified:
        return (
            "ACCEPTED",
            "explicit_consolidated_semantic_table_value",
            0.92,
        )
    if scope == "segment":
        return (
            "REVIEW_REQUIRED",
            "semantic_table_dimensional_value_requires_policy",
            0.72,
        )
    return (
        "REVIEW_REQUIRED",
        (
            "semantic_table_candidate_requires_policy"
            if complete_period and period_verified
            else "semantic_table_period_requires_policy"
            if complete_period
            else "semantic_table_missing_period_or_currency"
        ),
        0.70,
    )


def _table_candidates(
    document: SemanticDocument,
    *,
    filing: dict[str, Any],
    company_currency: str,
) -> list[DisclosureCandidate]:
    report_date = str(
        filing.get("report_date") or filing.get("filing_date") or ""
    )[:10]
    form_type = str(filing.get("form_type") or "")
    output: list[DisclosureCandidate] = []
    for block in document.table_rows:
        if re.search(
            r"\b(?:threshold|payout|performance\s+goal|incentive)\b",
            block.search_text,
            re.IGNORECASE,
        ):
            continue
        non_operating_backlog = bool(
            _NON_OPERATING_BACKLOG_TABLE.search(block.search_text)
        )
        labeled_cells = [
            (index, metric)
            for index, cell in enumerate(block.cells)
            if (metric := _table_metric(cell)) is not None
        ]
        if len(block.cells) < 2:
            continue
        value_specs: list[
            tuple[int, str, str, str, str, bool]
        ] = []
        if labeled_cells:
            label_cell_index, metric = labeled_cells[0]
            concept_name, metric_name = metric
            row_label = block.cells[label_cell_index]
            for cell_index, cell in enumerate(block.cells):
                value_text = cell
                if cell_index == label_cell_index:
                    label_match = next(
                        (
                            pattern.search(cell)
                            for _, name, pattern in _TABLE_METRIC_PATTERNS
                            if name == metric_name
                        ),
                        None,
                    )
                    if label_match is None:
                        continue
                    value_text = cell[label_match.end() :]
                header = (
                    block.header_cells[cell_index]
                    if cell_index < len(block.header_cells)
                    else ""
                )
                value_specs.append(
                    (
                        cell_index,
                        concept_name,
                        metric_name,
                        value_text,
                        f"{header}\n{row_label}",
                        False,
                    )
                )
        else:
            row_label = next(
                (
                    cell
                    for cell in block.cells
                    if cell
                    and not re.fullmatch(
                        r"[$\u20ac\u00a3]|[-\u2013\u2014]",
                        cell,
                    )
                ),
                "",
            )
            if not re.fullmatch(
                r"\s*(?:total|consolidated|overall|company(?:-wide)?)\s*",
                row_label,
                re.IGNORECASE,
            ):
                continue
            for cell_index, cell in enumerate(block.cells):
                header = (
                    block.header_cells[cell_index]
                    if cell_index < len(block.header_cells)
                    else ""
                )
                metric = _table_metric(header)
                if metric is None:
                    continue
                concept_name, metric_name = metric
                if (
                    metric_name == "reported_backlog"
                    and re.search(
                        r"\b(?:under|within)\s+(?:one|1)\s+year\b",
                        header,
                        re.IGNORECASE,
                    )
                ):
                    continue
                value_specs.append(
                    (
                        cell_index,
                        concept_name,
                        metric_name,
                        cell,
                        f"{header}\n{row_label}",
                        True,
                    )
                )
        for (
            cell_index,
            concept_name,
            metric_name,
            value_text,
            label_context,
            header_metric,
        ) in value_specs:
            noncommercial_order = bool(
                metric_name == "orders"
                and _NON_COMMERCIAL_ORDER_TABLE.search(block.search_text)
            )
            parsed = _money_from_text(
                value_text,
                context=block.search_text,
                company_currency=company_currency,
                minimum_value=0.0 if header_metric else 1_000_000.0,
            )
            if parsed is None:
                continue
            value, unit = parsed
            header = (
                block.header_cells[cell_index]
                if cell_index < len(block.header_cells)
                else ""
            )
            period_context = f"{header} {block.search_text}"
            explicit_period = bool(
                _ISO_DATE.search(header)
                or _EXPLICIT_DATE.search(header)
            )
            periodic_report_date = bool(
                str(filing.get("report_date") or "")[:10]
                and form_type.upper()
                in {
                    "10-K",
                    "10-K/A",
                    "10-Q",
                    "10-Q/A",
                    "20-F",
                    "20-F/A",
                    "40-F",
                    "40-F/A",
                }
            )
            period_end = _date_from_text(
                header or block.search_text,
                fallback=report_date,
            )
            period_start = (
                _orders_period_start(
                    period_end,
                    period_context,
                    form_type=form_type,
                )
                if metric_name == "orders"
                else ""
            )
            scope = _table_cell_scope(
                header=header,
                row_label=label_context.rsplit("\n", maxsplit=1)[-1],
                section_path=block.section_path,
            )
            status, reason, confidence = _table_candidate_status(
                scope=scope,
                period_start=period_start,
                period_end=period_end,
                unit=unit,
                metric_name=metric_name,
                period_verified=explicit_period or periodic_report_date,
            )
            if (
                non_operating_backlog
                and metric_name
                in {
                    "reported_backlog",
                    "remaining_performance_obligation",
                }
            ):
                status = "REJECTED_POLICY"
                reason = "non_operating_acquisition_or_intangible_table"
                confidence = 0.99
            elif noncommercial_order:
                status = "REJECTED_POLICY"
                reason = "noncommercial_legal_or_transactional_order_context"
                confidence = 0.99
            elif header_metric and value < 1_000_000.0:
                status = "REVIEW_REQUIRED"
                reason = "semantic_table_scale_requires_policy"
                confidence = 0.70
            output.append(
                DisclosureCandidate(
                    concept_name=concept_name,
                    metric_name=metric_name,
                    value=value,
                    unit=unit,
                    period_start=period_start,
                    period_end=period_end,
                    scope=scope,
                    confidence=confidence,
                    candidate_status=status,
                    status_reason=reason,
                    evidence_text=block.search_text[:1000],
                    block_index=block.index,
                    extraction_method="semantic_html_table",
                )
            )
    return output


def _deduplicate_candidates(
    candidates: list[DisclosureCandidate],
) -> list[DisclosureCandidate]:
    output: dict[
        tuple[str, float, str, str, str, str, str],
        DisclosureCandidate,
    ] = {}
    for candidate in candidates:
        key = (
            candidate.metric_name,
            round(candidate.value, 6),
            candidate.unit,
            candidate.period_start,
            candidate.period_end,
            candidate.scope,
            candidate.candidate_status,
        )
        previous = output.get(key)
        if previous is None or candidate.confidence > previous.confidence:
            output[key] = candidate
    return sorted(
        output.values(),
        key=lambda item: (
            item.metric_name,
            item.period_end,
            item.period_start,
            item.value,
            item.candidate_status,
            item.block_index,
        ),
    )


def _candidate_evidence(
    candidate: DisclosureCandidate,
    *,
    document_name: str,
    document_sha256: str,
    extraction_method: str,
    extraction_warning: str,
) -> MetricEvidence:
    return MetricEvidence(
        metric_name=candidate.metric_name,
        concept_name=candidate.concept_name,
        value=candidate.value,
        unit=candidate.unit,
        period_start=candidate.period_start,
        period_end=candidate.period_end,
        scope=candidate.scope,
        confidence=candidate.confidence,
        status=candidate.candidate_status,
        reason=candidate.status_reason,
        evidence_text=candidate.evidence_text,
        source_document=document_name,
        extraction_method=f"dedicated_parser:{candidate.extraction_method}",
        provenance={
            "document_sha256": document_sha256,
            "document_extraction_method": extraction_method,
            "document_extraction_warning": extraction_warning,
            "adapter_version": ADAPTER_VERSION,
            "semantic_block_index": candidate.block_index,
        },
    )


def _rpo_amount_in_block(
    block: SemanticBlock,
    *,
    company_currency: str,
) -> tuple[float, str] | None:
    metric = _table_metric(block.search_text)
    if metric is None or metric[1] != "remaining_performance_obligation":
        return None
    label_match = _TABLE_METRIC_PATTERNS[0][2].search(block.search_text)
    if label_match is None:
        return None
    return _money_from_text(
        block.search_text[label_match.end() :],
        context=block.search_text,
        company_currency=company_currency,
    )


def _current_rpo_evidence(
    document: SemanticDocument,
    *,
    filing: dict[str, Any],
    company_currency: str,
    document_sha256: str,
    resolved_candidates: list[DisclosureCandidate],
) -> list[MetricEvidence]:
    report_date = str(
        filing.get("report_date") or filing.get("filing_date") or ""
    )[:10]
    output: list[MetricEvidence] = []
    accepted_totals = [
        candidate
        for candidate in resolved_candidates
        if candidate.metric_name
        in {
            "remaining_performance_obligation",
            "reported_backlog",
        }
        and candidate.candidate_status == "ACCEPTED"
    ]
    for block in document.blocks:
        text = block.search_text
        horizon_match = _CURRENT_HORIZON.search(text)
        if horizon_match is None:
            continue
        block_metric = _table_metric(text)
        if (
            block_metric is None
            or block_metric[1]
            not in {
                "remaining_performance_obligation",
                "reported_backlog",
            }
        ):
            continue
        percentage_match = _CURRENT_PERCENT.search(text)
        period_end = _date_from_text(text, fallback=report_date)
        matching_totals = [
            candidate
            for candidate in accepted_totals
            if candidate.period_end == period_end
        ]
        block_amount = _rpo_amount_in_block(
            block,
            company_currency=company_currency,
        )
        accepted_total = next(
            (
                candidate
                for candidate in matching_totals
                if block_amount is None
                or (
                    candidate.unit == block_amount[1]
                    and abs(candidate.value - block_amount[0])
                    <= max(1.0, abs(block_amount[0]) * 1e-9)
                )
            ),
            None,
        )
        if accepted_total is None and len(matching_totals) == 1:
            accepted_total = matching_totals[0]
        total_rpo = (
            accepted_total.value
            if accepted_total is not None
            else block_amount[0]
            if block_amount is not None
            else 0.0
        )
        unit = (
            accepted_total.unit
            if accepted_total is not None
            else block_amount[1]
            if block_amount is not None
            else ""
        )
        if percentage_match is not None and total_rpo > 0:
            percentage = float(percentage_match.group("percent"))
            if not 0.0 < percentage <= 100.0:
                continue
            accepted = accepted_total is not None
            output.append(
                MetricEvidence(
                    metric_name="rpo_current",
                    concept_name=(
                        "RemainingPerformanceObligationCurrent"
                        "FromExplicitPercentage"
                    ),
                    value=total_rpo * percentage / 100.0,
                    unit=unit,
                    period_start="",
                    period_end=period_end,
                    scope=(
                        accepted_total.scope
                        if accepted_total is not None
                        else _semantic_scope(text)
                    ),
                    confidence=0.88 if accepted else 0.72,
                    status="ACCEPTED" if accepted else "REVIEW_REQUIRED",
                    reason=(
                        "explicit_twelve_month_rpo_percentage"
                        if accepted
                        else "explicit_percentage_total_rpo_requires_review"
                    ),
                    evidence_text=text[:1000],
                    source_document=document.source_document,
                    extraction_method=(
                        "dedicated_parser:explicit_rpo_percentage"
                    ),
                    provenance={
                        "document_sha256": document_sha256,
                        "adapter_version": ADAPTER_VERSION,
                        "semantic_block_index": block.index,
                        "total_rpo": total_rpo,
                        "explicit_percentage": percentage,
                        "derivation_type": (
                            "explicit_disclosure_arithmetic"
                        ),
                    },
                )
            )
            continue
        if accepted_total is None:
            continue
        preceding = text[: horizon_match.end()]
        parsed_amounts: list[tuple[float, str]] = []
        for money_match in _TABLE_MONEY_PATTERN.finditer(preceding):
            parsed = _money_from_text(
                money_match.group(0),
                context=text,
                company_currency=company_currency,
            )
            if parsed is not None:
                parsed_amounts.append(parsed)
        if not parsed_amounts:
            continue
        current_value, current_unit = parsed_amounts[-1]
        explicit_current_clause = bool(
            re.search(
                r"\bof\s+which\b|\bexpected\s+to\s+be\s+recognized\b"
                r"|\bexpect(?:s|ed)?\s+to\s+recognize\b",
                preceding[-300:],
                re.IGNORECASE,
            )
        )
        if (
            not explicit_current_clause
            or current_unit != accepted_total.unit
            or current_value >= accepted_total.value
        ):
            continue
        output.append(
            MetricEvidence(
                metric_name="rpo_current",
                concept_name=(
                    "RemainingPerformanceObligationCurrent"
                    "ExplicitAmount"
                ),
                value=current_value,
                unit=current_unit,
                period_start="",
                period_end=period_end,
                scope=accepted_total.scope,
                confidence=0.95,
                status="ACCEPTED",
                reason="explicit_twelve_month_rpo_amount",
                evidence_text=text[:1000],
                source_document=document.source_document,
                extraction_method=(
                    "dedicated_parser:explicit_rpo_current_amount"
                ),
                provenance={
                    "document_sha256": document_sha256,
                    "adapter_version": ADAPTER_VERSION,
                    "semantic_block_index": block.index,
                    "total_rpo": accepted_total.value,
                    "derivation_type": "explicit_disclosure_amount",
                },
            )
        )
    return output


def extract_metric_evidence(item: WorkItem) -> tuple[MetricEvidence, ...]:
    filing = asdict(item.filing)
    evidence: list[MetricEvidence] = []
    for document in item.documents:
        if document.is_full_submission:
            continue
        path = Path(document.path)
        try:
            extracted = extract_document_text(
                path.read_bytes(),
                document_name=document.name,
                enable_pdf_ocr=item.enable_pdf_ocr,
                max_pdf_pages=item.max_pdf_pages,
                max_pdf_bytes=item.max_pdf_bytes,
                pdf_extraction_timeout_sec=(
                    item.pdf_extraction_timeout_seconds
                ),
            )
        except OSError:
            continue
        if not extracted.text.strip():
            if extracted.warning:
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
                        reason=extracted.warning,
                        evidence_text=extracted.warning,
                        source_document=document.name,
                        extraction_method=(
                            f"dedicated_parser:{extracted.extraction_method}"
                        ),
                        provenance={
                            "document_sha256": document.content_sha256,
                            "adapter_version": ADAPTER_VERSION,
                        },
                    )
                    for request in item.requested_metrics
                )
            continue
        semantic_document = parse_semantic_document(
            extracted.text,
            source_document=document.name,
        )
        candidates = extract_machinery_prose_candidates(
            extracted.text,
            filing=filing,
            company_currency=item.filing.company_currency,
        )
        candidates.extend(
            _table_candidates(
                semantic_document,
                filing=filing,
                company_currency=item.filing.company_currency,
            )
        )
        resolved = resolve_machinery_disclosure_candidates(
            _deduplicate_candidates(candidates),
            ticker=item.filing.ticker,
            filing=filing,
        )
        evidence.extend(
            _candidate_evidence(
                candidate,
                document_name=document.name,
                document_sha256=document.content_sha256,
                extraction_method=extracted.extraction_method,
                extraction_warning=(
                    ";".join(
                        part
                        for part in (
                            extracted.warning,
                            semantic_document.warning,
                        )
                        if part
                    )
                ),
            )
            for candidate in resolved
        )
        evidence.extend(
            _current_rpo_evidence(
                semantic_document,
                filing=filing,
                company_currency=item.filing.company_currency,
                document_sha256=document.content_sha256,
                resolved_candidates=resolved,
            )
        )
    unique: dict[str, MetricEvidence] = {}
    for item_evidence in evidence:
        key = item_evidence.evidence_key(
            model_family=item.model_family,
            filing=item.filing,
        )
        unique[key] = item_evidence
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
                row.extraction_method,
            ),
        )
    )


def _fact_metric(fact: NormalizedFact) -> tuple[str, str] | None:
    metadata = _fact_metadata(fact)
    semantic_text = " ".join(
        (
            fact.concept_name,
            str(metadata.get("label") or ""),
            str(metadata.get("documentation") or ""),
        )
    )
    lower = re.sub(r"[^a-z0-9]+", "", semantic_text.lower())
    if (
        "remainingperformanceobligation" in lower
        or "unsatisfiedperformanceobligation" in lower
    ):
        if "percentage" in lower or (
            fact.unit.lower() in {"pure", "percent", "%"}
            and "expected" in lower
        ):
            if re.search(
                r"(?:nexttwelvemonths|withintwelvemonths|"
                r"withinoneyear|currentportion)",
                lower,
            ):
                return "rpo_current_percentage", "RPOCurrentPercentage"
            return None
        if re.search(
            r"(?:current|nexttwelvemonths|withinoneyear|"
            r"expectedtoberecognized)",
            lower,
        ):
            return "rpo_current", "RemainingPerformanceObligationCurrent"
        return "remaining_performance_obligation", "RemainingPerformanceObligation"
    if "backlog" in lower and any(
        term in lower for term in ("funded", "authorized", "appropriated")
    ):
        return "funded_backlog", "FundedBacklog"
    if any(
        term in lower
        for term in ("backlog", "orderbook", "unfilledorders")
    ):
        return "reported_backlog", "ReportedBacklog"
    if any(
        term in lower
        for term in (
            "bookings",
            "neworders",
            "orderintake",
            "ordersreceived",
            "newawards",
        )
    ) or lower in {"order", "orders"}:
        return "orders", "Orders"
    return None


def _fact_metadata(fact: NormalizedFact) -> dict[str, Any]:
    try:
        payload = json.loads(fact.concept_metadata_json)
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _standard_taxonomy(fact: NormalizedFact) -> bool:
    metadata = _fact_metadata(fact)
    namespace = str(metadata.get("namespace_uri") or "").lower()
    taxonomy = fact.taxonomy.lower()
    return (
        taxonomy in {"us-gaap", "ifrs-full"}
        or "fasb.org/us-gaap" in namespace
        or "xbrl.ifrs.org" in namespace
    )


def _non_operating_fact(fact: NormalizedFact) -> bool:
    metadata = _fact_metadata(fact)
    semantic_text = " ".join(
        (
            fact.concept_name,
            str(metadata.get("label") or ""),
            str(metadata.get("documentation") or ""),
        )
    )
    return _NON_OPERATING_FACT.search(semantic_text) is not None


def _fact_evidence(
    fact: NormalizedFact,
    *,
    metric_name: str,
    concept_name: str,
    value: float,
    accepted: bool,
    reason: str,
    provenance: dict[str, Any] | None = None,
) -> MetricEvidence:
    metadata = _fact_metadata(fact)
    return MetricEvidence(
        metric_name=metric_name,
        concept_name=concept_name,
        value=value,
        unit=fact.unit,
        period_start=fact.period_start,
        period_end=fact.period_end,
        scope=fact.scope,
        confidence=0.98 if accepted else 0.78,
        status="ACCEPTED" if accepted else "REVIEW_REQUIRED",
        reason=reason,
        evidence_text=(
            f"{fact.taxonomy}:{fact.concept_name} "
            f"label={metadata.get('label', '')} "
            f"context={fact.context_id} dimensions={fact.dimensions_json}"
        )[:1000],
        source_document=fact.source_document,
        extraction_method="dedicated_parser:arelle_xbrl_semantic",
        provenance={
            "context_id": fact.context_id,
            "dimensions_json": fact.dimensions_json,
            "concept_metadata": metadata,
            "adapter_version": ADAPTER_VERSION,
            **(provenance or {}),
        },
    )


def _timing_dimension_member(fact: NormalizedFact) -> str:
    try:
        dimensions = json.loads(fact.dimensions_json)
    except json.JSONDecodeError:
        return ""
    if not isinstance(dimensions, dict) or len(dimensions) != 1:
        return ""
    axis, member = next(iter(dimensions.items()))
    if "ExpectedTimingOfSatisfactionStartDateAxis" not in str(axis):
        return ""
    value = str(member or "")
    match = _ISO_DATE.search(value)
    return match.group(1) if match else value


def _timing_dimension_aggregates(
    facts: tuple[NormalizedFact, ...],
) -> tuple[
    list[tuple[NormalizedFact, NormalizedFact, bool]],
    set[int],
]:
    grouped: dict[
        tuple[str, str, str],
        list[tuple[str, NormalizedFact]],
    ] = {}
    for fact in facts:
        if fact.numeric_value is None:
            continue
        mapping = _fact_metric(fact)
        member = _timing_dimension_member(fact)
        if (
            mapping is None
            or mapping[0] != "remaining_performance_obligation"
            or not member
        ):
            continue
        grouped.setdefault(
            (fact.period_end, fact.unit, fact.source_document),
            [],
        ).append((member, fact))
    output: list[tuple[NormalizedFact, NormalizedFact, bool]] = []
    consumed: set[int] = set()
    for items in grouped.values():
        unique_members = {member for member, _ in items}
        if len(unique_members) < 2:
            continue
        ordered = sorted(items, key=lambda item: item[0])
        template = ordered[0][1]
        try:
            period_date = date.fromisoformat(template.period_end)
            member_dates = [
                date.fromisoformat(member) for member, _ in ordered
            ]
            current_bucket_validated = (
                period_date - timedelta(days=370)
                <= member_dates[0]
                <= period_date + timedelta(days=31)
                and member_dates[-1] > period_date
            )
        except ValueError:
            current_bucket_validated = False
        total_value = sum(
            float(fact.numeric_value or 0.0) for _, fact in ordered
        )
        aggregate = replace(
            template,
            concept_name=(
                "RevenueRemainingPerformanceObligation"
                "TimingDimensionAggregate"
            ),
            value_text=str(total_value),
            numeric_value=total_value,
            context_id="timing-dimension-aggregate",
            dimensions_json="{}",
            scope="consolidated",
        )
        current = replace(
            template,
            concept_name=(
                "RevenueRemainingPerformanceObligationCurrent"
                "TimingDimensionBucket"
            ),
            context_id="timing-dimension-current-bucket",
            dimensions_json="{}",
            scope="consolidated",
        )
        output.append((aggregate, current, current_bucket_validated))
        consumed.update(id(fact) for _, fact in ordered)
    return output, consumed


def _local_name(value: object) -> str:
    return str(value or "").rsplit(":", maxsplit=1)[-1]


def _reviewed_rpo_dimension_aggregates(
    item: WorkItem,
    facts: tuple[NormalizedFact, ...],
) -> list[tuple[NormalizedFact, tuple[NormalizedFact, ...]]]:
    specification = _REVIEWED_RPO_DIMENSION_AGGREGATIONS.get(
        item.filing.ticker
    )
    if specification is None:
        return []
    expected_axis, expected_members = specification
    grouped: dict[
        tuple[str, str, str, str, str],
        dict[str, list[NormalizedFact]],
    ] = {}
    for fact in facts:
        if fact.numeric_value is None:
            continue
        mapping = _fact_metric(fact)
        if mapping is None or mapping[0] != "remaining_performance_obligation":
            continue
        try:
            dimensions = json.loads(fact.dimensions_json)
        except json.JSONDecodeError:
            continue
        if not isinstance(dimensions, dict) or len(dimensions) != 1:
            continue
        axis, member = next(iter(dimensions.items()))
        if (
            _local_name(axis) != expected_axis
            or _local_name(member) not in expected_members
        ):
            continue
        key = (
            fact.period_start,
            fact.period_end,
            fact.unit,
            fact.source_document,
            fact.concept_name,
        )
        grouped.setdefault(key, {}).setdefault(
            _local_name(member),
            [],
        ).append(fact)

    output: list[tuple[NormalizedFact, tuple[NormalizedFact, ...]]] = []
    for member_facts in grouped.values():
        if set(member_facts) != set(expected_members):
            continue
        selected: list[NormalizedFact] = []
        ambiguous = False
        for member in sorted(expected_members):
            facts_for_member = member_facts[member]
            values = {
                round(float(fact.numeric_value or 0.0), 6)
                for fact in facts_for_member
            }
            if len(values) != 1:
                ambiguous = True
                break
            selected.append(facts_for_member[0])
        if ambiguous:
            continue
        total_value = sum(float(fact.numeric_value or 0.0) for fact in selected)
        template = selected[0]
        aggregate = replace(
            template,
            concept_name=(
                "RevenueRemainingPerformanceObligation"
                "ReviewedDimensionAggregate"
            ),
            value_text=str(total_value),
            numeric_value=total_value,
            context_id="reviewed-dimension-aggregate",
            dimensions_json="{}",
            scope="consolidated",
        )
        output.append((aggregate, tuple(selected)))
    return output


def _reviewed_consolidated_extension(
    item: WorkItem,
    fact: NormalizedFact,
) -> bool:
    try:
        dimensions = json.loads(fact.dimensions_json)
    except json.JSONDecodeError:
        return False
    return (
        (item.filing.ticker, fact.concept_name)
        in _REVIEWED_CONSOLIDATED_EXTENSION_FACTS
        and dimensions == {}
        and fact.scope == "consolidated"
    )


def map_normalized_facts(
    item: WorkItem,
    facts: tuple[NormalizedFact, ...],
) -> tuple[MetricEvidence, ...]:
    evidence: list[MetricEvidence] = []
    total_rpo_facts: list[NormalizedFact] = []
    percentage_facts: list[NormalizedFact] = []
    timing_aggregates, consumed_timing_facts = (
        _timing_dimension_aggregates(facts)
    )
    for aggregate, components in _reviewed_rpo_dimension_aggregates(
        item,
        facts,
    ):
        total_rpo_facts.append(aggregate)
        evidence.append(
            _fact_evidence(
                aggregate,
                metric_name="remaining_performance_obligation",
                concept_name="RemainingPerformanceObligation",
                value=float(aggregate.numeric_value or 0.0),
                accepted=True,
                reason="reviewed_exhaustive_dimension_aggregation",
                provenance={
                    "derivation_type": "reviewed_exhaustive_dimension_sum",
                    "component_context_ids": [
                        fact.context_id for fact in components
                    ],
                    "component_values": [
                        float(fact.numeric_value or 0.0)
                        for fact in components
                    ],
                },
            )
        )
    for aggregate, current, current_bucket_validated in timing_aggregates:
        standard_taxonomy = _standard_taxonomy(aggregate)
        accepted = standard_taxonomy and current_bucket_validated
        total_rpo_facts.append(aggregate)
        evidence.append(
            _fact_evidence(
                aggregate,
                metric_name="remaining_performance_obligation",
                concept_name="RemainingPerformanceObligation",
                value=float(aggregate.numeric_value or 0.0),
                accepted=accepted,
                reason=(
                    "standard_timing_dimension_exhaustive_sum"
                    if accepted
                    else (
                        "timing_dimension_incomplete_schedule_requires_sector_review"
                        if standard_taxonomy
                        else "timing_dimension_sum_requires_sector_review"
                    )
                ),
                provenance={
                    "derivation_type": "exhaustive_dimension_sum",
                    "timing_schedule_complete": current_bucket_validated,
                },
            )
        )
        evidence.append(
            _fact_evidence(
                current,
                metric_name="rpo_current",
                concept_name="RemainingPerformanceObligationCurrent",
                value=float(current.numeric_value or 0.0),
                accepted=accepted and current_bucket_validated,
                reason=(
                    "standard_earliest_timing_dimension_bucket"
                    if accepted and current_bucket_validated
                    else "timing_dimension_current_requires_sector_review"
                ),
                provenance={
                    "derivation_type": "earliest_timing_dimension_bucket",
                },
            )
        )
    for fact in facts:
        if id(fact) in consumed_timing_facts:
            continue
        mapping = _fact_metric(fact)
        if mapping is None or fact.numeric_value is None:
            continue
        metric_name, concept_name = mapping
        if metric_name == "rpo_current_percentage":
            percentage_facts.append(fact)
            continue
        if metric_name == "remaining_performance_obligation":
            total_rpo_facts.append(fact)
        non_operating = _non_operating_fact(fact)
        reviewed_extension = _reviewed_consolidated_extension(item, fact)
        accepted = (
            not non_operating
            and
            metric_name
            in {
                "remaining_performance_obligation",
                "rpo_current",
            }
            and (_standard_taxonomy(fact) or reviewed_extension)
            and fact.scope == "consolidated"
        )
        mapped_evidence = _fact_evidence(
            fact,
            metric_name=metric_name,
            concept_name=concept_name,
            value=fact.numeric_value,
            reason=(
                "non_operating_acquisition_or_intangible_fact"
                if non_operating
                else (
                    "reviewed_consolidated_extension_fact"
                    if reviewed_extension
                    else (
                        "standard_taxonomy_consolidated_semantic_fact"
                        if accepted
                        else (
                            "extension_or_dimensional_fact_requires_sector_review"
                        )
                    )
                )
            ),
            accepted=accepted,
        )
        if non_operating:
            mapped_evidence = replace(
                mapped_evidence,
                status="REJECTED_POLICY",
                confidence=0.99,
            )
        evidence.append(mapped_evidence)
    for percentage_fact in percentage_facts:
        percentage = float(percentage_fact.numeric_value or 0.0)
        if percentage > 1.0:
            percentage /= 100.0
        if not 0.0 < percentage <= 1.0:
            continue
        matching_totals = [
            fact
            for fact in total_rpo_facts
            if fact.numeric_value is not None
            and fact.period_end == percentage_fact.period_end
            and fact.scope == percentage_fact.scope
        ]
        if len(matching_totals) != 1:
            continue
        total = matching_totals[0]
        accepted = (
            _standard_taxonomy(total)
            and _standard_taxonomy(percentage_fact)
            and total.scope == "consolidated"
        )
        derived = _fact_evidence(
            total,
            metric_name="rpo_current",
            concept_name=(
                "RemainingPerformanceObligationCurrent"
                "FromExplicitXBRLPercentage"
            ),
            value=float(total.numeric_value or 0.0) * percentage,
            accepted=accepted,
            reason=(
                "explicit_xbrl_current_percentage"
                if accepted
                else "xbrl_percentage_derivation_requires_sector_review"
            ),
            provenance={
                "derivation_type": "explicit_xbrl_percentage",
                "percentage_context_id": percentage_fact.context_id,
                "explicit_percentage": percentage,
                "total_rpo_fact": total.concept_name,
            },
        )
        evidence.append(derived)
    unique: dict[str, MetricEvidence] = {}
    for row in evidence:
        unique[
            row.evidence_key(
                model_family=item.model_family,
                filing=item.filing,
            )
        ] = row
    return tuple(
        sorted(
            unique.values(),
            key=lambda row: (
                row.metric_name,
                row.period_end,
                row.value if row.value is not None else float("-inf"),
                row.status,
                row.source_document,
            ),
        )
    )
