from __future__ import annotations

import json
import math
import re
import sqlite3
from collections import defaultdict
from dataclasses import asdict, replace
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from dedicated_parser.contracts import (
    AdapterRegistry,
    MetricEvidence,
    MetricRequest,
    MetricRequirement,
    NormalizedFact,
    ProductionMetricMapping,
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


ADAPTER_VERSION = "machinery_specialized_metrics_v3.6"
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
        supporting_metrics=(
            MetricRequest(
                "debt_total",
                (
                    r"(?:Debt|Borrowings|LongTermDebt|ShortTermBorrowings)",
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
            "roic": "debt_total",
        },
        metric_requirements={
            "orders": MetricRequirement("orders"),
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
            "funded_backlog": MetricRequirement("funded_backlog"),
            "backlog_yoy_growth": MetricRequirement(
                "backlog_yoy_growth"
            ),
            "backlog_to_revenue": MetricRequirement("backlog_to_revenue"),
            "reported_backlog": MetricRequirement("reported_backlog"),
            "reported_backlog_yoy_growth": MetricRequirement(
                "reported_backlog_yoy_growth"
            ),
            "reported_backlog_to_revenue": MetricRequirement(
                "reported_backlog_to_revenue"
            ),
            "remaining_performance_obligation": MetricRequirement(
                "remaining_performance_obligation"
            ),
            "rpo_current": MetricRequirement("rpo_current"),
            "rpo_yoy_growth": MetricRequirement("rpo_yoy_growth"),
            "rpo_to_revenue": MetricRequirement("rpo_to_revenue"),
            "rpo_implied_orders": MetricRequirement("rpo_implied_orders"),
            "rpo_implied_book_to_bill": MetricRequirement(
                "rpo_implied_book_to_bill"
            ),
            "contract_load_proxy": MetricRequirement(
                "contract_load_proxy"
            ),
            "contract_load_proxy_yoy_growth": MetricRequirement(
                "contract_load_proxy_yoy_growth"
            ),
            "contract_load_proxy_to_revenue": MetricRequirement(
                "contract_load_proxy_to_revenue"
            ),
            "roic": MetricRequirement("roic"),
        },
        production_mappings={
            "orders": ProductionMetricMapping(
                "orders",
                "orders",
                "duration",
                175,
            ),
            "funded_backlog": ProductionMetricMapping(
                "funded_backlog",
                "backlog",
                "instant",
                175,
            ),
            "reported_backlog": ProductionMetricMapping(
                "reported_backlog",
                "backlog",
                "instant",
                175,
            ),
            "remaining_performance_obligation": ProductionMetricMapping(
                "remaining_performance_obligation",
                "backlog",
                "instant",
                165,
            ),
            "rpo_current": ProductionMetricMapping(
                "rpo_current",
                "backlog",
                "instant",
                165,
            ),
            "debt_total": ProductionMetricMapping(
                "debt_total",
                "balance_sheet",
                "instant",
                175,
            ),
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
    # Trailing boundary keeps a bare scale letter from greedily matching the
    # first letter of the next word ("1,234 based" is not 1,234 billion).
    r"(?P<scale>(?:billions?|millions?|thousands?|bn|mm|[bmk])\b)?\s*"
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
    # Clause-bounded: the percentage must sit in the same clause as the
    # twelve-month horizon phrase (either order), otherwise any stray growth
    # percentage in the block ("Backlog increased 15% ...") hijacks the
    # current-RPO derivation.
    r"\b(?P<percent>\d{1,3}(?:\.\d+)?)\s*(?:%|percent\b)"
    r"[^.;]{0,160}?"
    r"(?:(?:within|over)\s+(?:the\s+)?next|next)\s+(?:12|twelve)\s+months?"
    r"|(?:(?:within|over)\s+(?:the\s+)?next|next)\s+(?:12|twelve)\s+months?"
    r"[^.;]{0,160}?"
    r"\b(?P<percent_after>\d{1,3}(?:\.\d+)?)\s*(?:%|percent\b)"
    r"|\b(?P<percent_one_year>\d{1,3}(?:\.\d+)?)\s*(?:%|percent\b)"
    r"[^.;]{0,160}?within\s+(?:one|1)\s+year",
    re.IGNORECASE,
)
_CURRENT_RECOGNITION = re.compile(
    r"\b(?:is|are|was|were|will\s+be|expected\s+to\s+be)\s+recognized\b"
    r"|\bexpect(?:s|ed)?\s+to\s+recognize\b",
    re.IGNORECASE,
)
_SUBCOMPONENT_AMOUNT = re.compile(
    r"\b(?:related|attributable)\s+to\b|\bsubsidiar(?:y|ies)\b"
    r"|\bsegment\b",
    re.IGNORECASE,
)
_HORIZON_ROW_OR_COLUMN = re.compile(
    r"\b(?:under|within|next)\s+(?:one|1)\s+year\b"
    r"|\b(?:within|next)\s+(?:the\s+next\s+)?(?:12|twelve)\s+months?\b"
    r"|\brecognized\s+within\s+(?:12|twelve)\s+months?\b",
    re.IGNORECASE,
)
_TOTAL_DEBT_LABEL = re.compile(
    r"^\s*(?:total\s+)?(?:debt|borrowings)(?:\s+outstanding)?\s*$",
    re.IGNORECASE,
)
_ZERO_TABLE_VALUE = re.compile(
    r"^\s*(?:[$\u20ac\u00a3]\s*)?(?:0(?:\.0+)?|[-\u2013\u2014])\s*$"
)
_NO_DEBT_PROSE = re.compile(
    r"\b(?:we|the\s+company)\s+(?:had|has)\s+no\s+"
    r"(?:debt|borrowings)\s+outstanding\b"
    r"|\bno\s+(?:debt|borrowings)\s+(?:was|were)\s+outstanding\b",
    re.IGNORECASE,
)
_FACILITY_NO_DEBT_PROSE = re.compile(
    r"\bno\s+(?:debt|borrowings)\s+outstanding\s+under\s+"
    r"(?:the|our)\s+(?:credit|revolving)\s+facility\b",
    re.IGNORECASE,
)
_RPO_PRACTICAL_EXPEDIENT = re.compile(
    r"\bpractical\s+expedient\b.{0,500}\b(?:remaining|unsatisfied)\s+"
    r"performance\s+obligations?\b"
    r"|\b(?:remaining|unsatisfied)\s+performance\s+obligations?\b"
    r".{0,500}\bpractical\s+expedient\b",
    re.IGNORECASE | re.DOTALL,
)
_BACKLOG_NOT_DISCLOSED = re.compile(
    r"\b(?:we|the\s+company)\s+(?:do|does)\s+not\s+"
    r"(?:disclose|report|track)\s+(?:a\s+)?backlog\b",
    re.IGNORECASE,
)
_NO_BINDING_BACKLOG = re.compile(
    r"\b(?:we|the\s+company)\s+(?:do|does)\s+not\s+"
    r"(?:maintain|have)\s+(?:a\s+)?(?:material\s+)?backlog\b"
    r"|\borders?\s+(?:are|is)\s+(?:generally\s+)?"
    r"(?:shipped|fulfilled)\s+(?:within|in)\s+"
    r"(?:days|weeks|a\s+short\s+period)\b",
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
_REVENUE_CONTRACT_DETAIL_TABLE = re.compile(
    r"\brevenue\s+from\s+contracts?\s+with\s+customers?\b"
    r"|\brevenue\s+from\s+contract\s+with\s+customer\s+\[text\s+block\]",
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


def _is_explicit_table_metric_label(
    text: str,
    *,
    metric_name: str,
) -> bool:
    """Distinguish a table label from narrative text containing a keyword."""
    normalized = " ".join(text.split())
    if (
        not normalized
        or len(normalized) > 160
        or re.search(r"\[\s*text\s+block\s*\]", normalized, re.IGNORECASE)
    ):
        return False
    return any(
        name == metric_name and pattern.search(normalized)
        for _, name, pattern in _TABLE_METRIC_PATTERNS
    )


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
    # Multi-row headers merge fragments with " | " ("March 31, | 2026"),
    # which defeats the day/year whitespace in _EXPLICIT_DATE and used to
    # drop into the bare-year branch — stamping a fabricated -12-31 period
    # end for a March column. Normalize the separator before matching.
    normalized_text = text.replace(" | ", " ")
    iso = _ISO_DATE.search(normalized_text)
    if iso:
        return iso.group(1)
    explicit = _EXPLICIT_DATE.search(normalized_text)
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
    year_matches = re.findall(r"\b((?:19|20)\d{2})\b", normalized_text)
    if len(year_matches) == 1 and re.search(
        r"\b(?:year[-\s]end(?:ed)?|fiscal|december)\b",
        normalized_text,
        re.IGNORECASE,
    ):
        # A bare year justifies a Dec-31 period end only when the context
        # actually says year-end; otherwise a lone comparative-year header
        # ("2024") in a non-December fiscal year fabricates the date.
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
            if _HORIZON_ROW_OR_COLUMN.search(row_label):
                # "... to be recognized within 12 months" rows are the CURRENT
                # portion, never a total. For RPO, reclassify to rpo_current
                # (structured current-RPO disclosure); for other metrics, skip
                # rather than record a partial-horizon value as the total.
                if metric_name == "remaining_performance_obligation":
                    concept_name = "RemainingPerformanceObligationCurrent"
                    metric_name = "rpo_current"
                else:
                    continue
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
                if _HORIZON_ROW_OR_COLUMN.search(header):
                    # Horizon-limited columns apply to every metric, not just
                    # reported_backlog, and issuers write "12 months" as often
                    # as "one year". RPO horizon columns are the structured
                    # current-RPO disclosure — capture them as rpo_current.
                    if metric_name == "remaining_performance_obligation":
                        concept_name = "RemainingPerformanceObligationCurrent"
                        metric_name = "rpo_current"
                    else:
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
            row_label = label_context.rsplit("\n", maxsplit=1)[-1]
            explicit_metric_label = (
                header_metric
                or _is_explicit_table_metric_label(
                    row_label,
                    metric_name=metric_name,
                )
            )
            revenue_contract_narrative_order = bool(
                metric_name == "orders"
                and not explicit_metric_label
                and _REVENUE_CONTRACT_DETAIL_TABLE.search(
                    block.search_text
                )
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
            # Duration from the cell's own column header first: search_text
            # carries EVERY column group's header, so "Three Months Ended"
            # from a sibling group would always win over this column's
            # "Six Months Ended".
            period_start = ""
            if metric_name == "orders":
                period_start = _duration_start(period_end, header)
                if not period_start:
                    period_start = _orders_period_start(
                        period_end,
                        period_context,
                        form_type=form_type,
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
            elif revenue_contract_narrative_order:
                status = "REJECTED_POLICY"
                reason = "revenue_contract_narrative_is_not_orders"
                confidence = 0.99
            elif metric_name == "orders" and not explicit_metric_label:
                status = "REVIEW_REQUIRED"
                reason = "semantic_table_order_label_not_explicit"
                confidence = min(confidence, 0.70)
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
    # Status is deliberately NOT part of the key: a prose REVIEW_REQUIRED and
    # a table ACCEPTED for the same observation are one observation — keep the
    # higher-precedence status so identical values don't add phantom review
    # queue rows.
    status_rank = {
        "ACCEPTED": 3,
        "REJECTED_POLICY": 2,
        "REVIEW_REQUIRED": 1,
    }
    output: dict[
        tuple[str, float, str, str, str, str],
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
        )
        previous = output.get(key)
        if previous is None:
            output[key] = candidate
            continue
        candidate_rank = (
            status_rank.get(candidate.candidate_status, 0),
            candidate.confidence,
        )
        previous_rank = (
            status_rank.get(previous.candidate_status, 0),
            previous.confidence,
        )
        if candidate_rank > previous_rank:
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
    # Validate each money MATCH substring, not the whole tail: horizon words
    # ("twelve months") appear in nearly every qualifying block and would make
    # a whole-tail _money_from_text call reject everything (dead guard).
    tail = block.search_text[label_match.end() :]
    for money_match in _TABLE_MONEY_PATTERN.finditer(tail):
        parsed = _money_from_text(
            money_match.group(0),
            context=block.search_text,
            company_currency=company_currency,
        )
        if parsed is not None:
            return parsed
    return None


def _clause_containing(
    text: str,
    *,
    start: int,
    end: int,
) -> tuple[str, int]:
    left = 0
    for boundary in re.finditer(r"(?:[.;]\s+|\n+)", text[:start]):
        left = boundary.end()
    right_match = re.search(r"(?:[.;](?:\s+|$)|\n+)", text[end:])
    right = end + right_match.start() if right_match is not None else len(text)
    return text[left:right], left


def _validated_total_scope(candidate: DisclosureCandidate) -> str:
    if candidate.scope != "unknown":
        return candidate.scope
    if re.search(
        r"\b(?:consolidated|company(?:'s)?|our|total)\s+"
        r"(?:remaining\s+performance\s+obligations?|(?:order\s+)?backlog)\b",
        candidate.evidence_text,
        re.IGNORECASE,
    ):
        return "consolidated"
    return "unknown"


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
        horizon_clause, clause_offset = _clause_containing(
            text,
            start=horizon_match.start(),
            end=horizon_match.end(),
        )
        clause_horizon = _CURRENT_HORIZON.search(horizon_clause)
        if clause_horizon is None:
            continue
        percentage_match = _CURRENT_PERCENT.search(horizon_clause)
        period_end = _date_from_text(text, fallback=report_date)
        matching_totals = []
        seen_total_keys: set[tuple[float, str]] = set()
        for candidate in accepted_totals:
            if candidate.period_end != period_end:
                continue
            total_key = (round(candidate.value, 2), candidate.unit)
            if total_key in seen_total_keys:
                continue
            seen_total_keys.add(total_key)
            matching_totals.append(candidate)
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
        accepted_total_scope = (
            _validated_total_scope(accepted_total)
            if accepted_total is not None
            else "unknown"
        )
        parent_total_eligible = (
            accepted_total is not None
            and accepted_total_scope == "consolidated"
            and (
                accepted_total.confidence >= 0.90
                or accepted_total.status_reason.startswith("reviewed_")
            )
        )
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
        # Explicit "of which $X is expected to be recognized ..." amounts take
        # precedence over percentage derivation: the disclosed dollar figure is
        # authoritative and the percentage path can only approximate it.
        if accepted_total is not None:
            explicit_amounts: list[tuple[float, str]] = []
            for money_match in _TABLE_MONEY_PATTERN.finditer(horizon_clause):
                protected_spans = [clause_horizon.span()]
                if percentage_match is not None:
                    protected_spans.append(percentage_match.span())
                if any(
                    money_match.start() < protected_end
                    and money_match.end() > protected_start
                    for protected_start, protected_end in protected_spans
                ):
                    continue
                parsed = _money_from_text(
                    money_match.group(0),
                    context=horizon_clause,
                    company_currency=company_currency,
                )
                bridge_start = min(money_match.end(), clause_horizon.start())
                bridge_end = max(money_match.end(), clause_horizon.start())
                bridge = horizon_clause[bridge_start:bridge_end]
                if (
                    parsed is not None
                    and _CURRENT_RECOGNITION.search(horizon_clause)
                    and _SUBCOMPONENT_AMOUNT.search(bridge) is None
                ):
                    explicit_amounts.append(parsed)
            if (
                explicit_amounts
                and explicit_amounts[-1][1] == accepted_total.unit
                and explicit_amounts[-1][0] < accepted_total.value
            ):
                output.append(
                    MetricEvidence(
                        metric_name="rpo_current",
                        concept_name=(
                            "RemainingPerformanceObligationCurrent"
                            "ExplicitAmount"
                        ),
                        value=explicit_amounts[-1][0],
                        unit=explicit_amounts[-1][1],
                        period_start="",
                        period_end=period_end,
                        scope=accepted_total_scope,
                        confidence=0.95 if parent_total_eligible else 0.72,
                        status=(
                            "ACCEPTED"
                            if parent_total_eligible
                            else "REVIEW_REQUIRED"
                        ),
                        reason=(
                            "explicit_twelve_month_rpo_amount"
                            if parent_total_eligible
                            else "explicit_amount_parent_total_requires_review"
                        ),
                        evidence_text=horizon_clause[:1000],
                        source_document=document.source_document,
                        extraction_method=(
                            "dedicated_parser:explicit_rpo_current_amount"
                        ),
                        provenance={
                            "document_sha256": document_sha256,
                            "adapter_version": ADAPTER_VERSION,
                            "semantic_block_index": block.index,
                            "clause_offset": clause_offset,
                            "total_rpo": accepted_total.value,
                            "derivation_type": "explicit_disclosure_amount",
                        },
                    )
                )
                continue
        if percentage_match is not None and total_rpo > 0:
            percent_text = next(
                (
                    group
                    for group in (
                        percentage_match.group("percent"),
                        percentage_match.group("percent_after"),
                        percentage_match.group("percent_one_year"),
                    )
                    if group
                ),
                "",
            )
            if not percent_text:
                continue
            percentage = float(percent_text)
            if not 0.0 < percentage <= 100.0:
                continue
            accepted = parent_total_eligible
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
                        accepted_total_scope
                        if accepted_total is not None
                        else _semantic_scope(text)
                    ),
                    confidence=0.93 if accepted else 0.72,
                    status="ACCEPTED" if accepted else "REVIEW_REQUIRED",
                    reason=(
                        "explicit_twelve_month_rpo_percentage"
                        if accepted
                        else "explicit_percentage_total_rpo_requires_review"
                    ),
                    evidence_text=horizon_clause[:1000],
                    source_document=document.source_document,
                    extraction_method=(
                        "dedicated_parser:explicit_rpo_percentage"
                    ),
                    provenance={
                        "document_sha256": document_sha256,
                        "adapter_version": ADAPTER_VERSION,
                        "semantic_block_index": block.index,
                        "clause_offset": clause_offset,
                        "total_rpo": total_rpo,
                        "explicit_percentage": percentage,
                        "derivation_type": (
                            "explicit_disclosure_arithmetic"
                        ),
                    },
                )
            )
            continue
    return output


def _review_only_structural_evidence(
    *,
    metric_name: str,
    concept_name: str,
    reason: str,
    text: str,
    document: SemanticDocument,
    block: SemanticBlock,
    report_date: str,
    document_sha256: str,
) -> MetricEvidence:
    return MetricEvidence(
        metric_name=metric_name,
        concept_name=concept_name,
        value=None,
        unit="",
        period_start="",
        period_end=report_date,
        scope="consolidated",
        confidence=0.90,
        status="REVIEW_REQUIRED",
        reason=reason,
        evidence_text=text[:1000],
        source_document=document.source_document,
        extraction_method="dedicated_parser:negative_disclosure",
        provenance={
            "document_sha256": document_sha256,
            "adapter_version": ADAPTER_VERSION,
            "semantic_block_index": block.index,
            "structural_candidate": True,
        },
    )


def _debt_and_structural_evidence(
    document: SemanticDocument,
    *,
    filing: dict[str, Any],
    company_currency: str,
    document_sha256: str,
) -> list[MetricEvidence]:
    report_date = str(
        filing.get("report_date") or filing.get("filing_date") or ""
    )[:10]
    output: list[MetricEvidence] = []
    for block in document.blocks:
        text = block.search_text
        if block.kind == "table_row":
            debt_label = next(
                (
                    cell
                    for cell in block.cells
                    if _TOTAL_DEBT_LABEL.fullmatch(cell)
                ),
                "",
            )
            zero_cells = [
                cell
                for cell in block.cells
                if cell != debt_label and _ZERO_TABLE_VALUE.fullmatch(cell)
            ]
            if debt_label and zero_cells:
                output.append(
                    MetricEvidence(
                        metric_name="debt_total",
                        concept_name="ExplicitZeroTotalDebt",
                        value=0.0,
                        unit=company_currency.upper(),
                        period_start="",
                        period_end=_date_from_text(
                            " ".join(block.header_cells) or text,
                            fallback=report_date,
                        ),
                        scope=(
                            "consolidated"
                            if _semantic_scope(text) != "segment"
                            else "segment"
                        ),
                        confidence=0.84,
                        status="REVIEW_REQUIRED",
                        reason="explicit_zero_total_debt_table_requires_review",
                        evidence_text=text[:1000],
                        source_document=document.source_document,
                        extraction_method=(
                            "dedicated_parser:explicit_zero_debt_table"
                        ),
                        provenance={
                            "document_sha256": document_sha256,
                            "adapter_version": ADAPTER_VERSION,
                            "semantic_block_index": block.index,
                            "zero_cell_count": len(zero_cells),
                        },
                    )
                )
        if _FACILITY_NO_DEBT_PROSE.search(text):
            output.append(
                MetricEvidence(
                    metric_name="debt_total",
                    concept_name="NoFacilityBorrowingsOutstanding",
                    value=0.0,
                    unit=company_currency.upper(),
                    period_start="",
                    period_end=report_date,
                    scope="facility",
                    confidence=0.65,
                    status="REVIEW_REQUIRED",
                    reason="facility_specific_zero_is_not_total_debt",
                    evidence_text=text[:1000],
                    source_document=document.source_document,
                    extraction_method=(
                        "dedicated_parser:explicit_zero_debt_prose"
                    ),
                    provenance={
                        "document_sha256": document_sha256,
                        "adapter_version": ADAPTER_VERSION,
                        "semantic_block_index": block.index,
                    },
                )
            )
        elif _NO_DEBT_PROSE.search(text):
            output.append(
                MetricEvidence(
                    metric_name="debt_total",
                    concept_name="NoDebtOutstanding",
                    value=0.0,
                    unit=company_currency.upper(),
                    period_start="",
                    period_end=report_date,
                    scope="consolidated",
                    confidence=0.88,
                    status="REVIEW_REQUIRED",
                    reason="explicit_zero_total_debt_prose_requires_review",
                    evidence_text=text[:1000],
                    source_document=document.source_document,
                    extraction_method=(
                        "dedicated_parser:explicit_zero_debt_prose"
                    ),
                    provenance={
                        "document_sha256": document_sha256,
                        "adapter_version": ADAPTER_VERSION,
                        "semantic_block_index": block.index,
                    },
                )
            )
        if _RPO_PRACTICAL_EXPEDIENT.search(text):
            output.append(
                _review_only_structural_evidence(
                    metric_name="remaining_performance_obligation",
                    concept_name="RPOPracticalExpedientDisclosure",
                    reason="asc606_practical_expedient_requires_review",
                    text=text,
                    document=document,
                    block=block,
                    report_date=report_date,
                    document_sha256=document_sha256,
                )
            )
        if _BACKLOG_NOT_DISCLOSED.search(text):
            output.append(
                _review_only_structural_evidence(
                    metric_name="reported_backlog",
                    concept_name="BacklogNotDisclosed",
                    reason=(
                        "confirmed_non_disclosure_not_structural_na"
                    ),
                    text=text,
                    document=document,
                    block=block,
                    report_date=report_date,
                    document_sha256=document_sha256,
                )
            )
        elif _NO_BINDING_BACKLOG.search(text):
            output.append(
                _review_only_structural_evidence(
                    metric_name="reported_backlog",
                    concept_name="NoBindingBacklog",
                    reason="short_cycle_or_no_binding_backlog_requires_review",
                    text=text,
                    document=document,
                    block=block,
                    report_date=report_date,
                    document_sha256=document_sha256,
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
        except OSError as exc:
            # A cached file that fails to read after planning (permissions,
            # OneDrive cloud-only placeholder, sharing violation) must surface
            # as PARSER_FAILURE — silently skipping collapses the metric to
            # baseline non-disclosure, the exact mislabeling the design forbids.
            evidence.extend(
                MetricEvidence(
                    metric_name=request.metric_name,
                    concept_name="DocumentReadFailure",
                    value=None,
                    unit="",
                    period_start="",
                    period_end=item.filing.report_date,
                    scope="unknown",
                    confidence=0.0,
                    status="PARSER_FAILURE",
                    reason=f"document_read_failed:{type(exc).__name__}",
                    evidence_text=f"{type(exc).__name__}: {exc}"[:500],
                    source_document=document.name,
                    extraction_method="dedicated_parser:document_read",
                    provenance={
                        "document_sha256": document.content_sha256,
                        "adapter_version": ADAPTER_VERSION,
                    },
                )
                for request in item.requested_metrics
            )
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
        evidence.extend(
            _debt_and_structural_evidence(
                semantic_document,
                filing=filing,
                company_currency=item.filing.company_currency,
                document_sha256=document.content_sha256,
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
    concept_lower = re.sub(
        r"[^a-z0-9]+",
        "",
        fact.concept_name.lower(),
    )
    if concept_lower in {
        "debtandfinanceleaseobligations",
        "longtermdebtandcapitalleaseobligations",
        "longtermdebtandfinanceleaseobligations",
        "longtermdebtincludingcurrentmaturities",
        "totalborrowings",
        "totaldebt",
    } and fact.numeric_value == 0.0:
        return "debt_total", "DebtTotal"
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
    list[
        tuple[
            NormalizedFact,
            NormalizedFact,
            bool,
            bool,
            bool,
            bool,
            float | None,
        ]
    ],
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
    dimensionless_totals: dict[
        tuple[str, str, str],
        set[float],
    ] = {}
    for fact in facts:
        mapping = _fact_metric(fact)
        if (
            fact.numeric_value is None
            or mapping is None
            or mapping[0] != "remaining_performance_obligation"
            or fact.scope != "consolidated"
            or not _standard_taxonomy(fact)
            or _timing_dimension_member(fact)
        ):
            continue
        dimensionless_totals.setdefault(
            (fact.period_end, fact.unit, fact.source_document),
            set(),
        ).add(round(float(fact.numeric_value), 6))

    output: list[
        tuple[
            NormalizedFact,
            NormalizedFact,
            bool,
            bool,
            bool,
            bool,
            float | None,
        ]
    ] = []
    consumed: set[int] = set()
    for items in grouped.values():
        # One value per member: iXBRL filings tag the same bucket fact in the
        # table and the narrative, and summing every occurrence doubles that
        # bucket. Conflicting values for one member make the schedule
        # ambiguous — bail out, mirroring _reviewed_rpo_dimension_aggregates.
        values_by_member: dict[str, list[tuple[str, NormalizedFact]]] = {}
        member_value_sets: dict[str, set[float]] = {}
        for member, fact in items:
            member_value_sets.setdefault(member, set()).add(
                float(fact.numeric_value or 0.0)
            )
            values_by_member.setdefault(member, []).append((member, fact))
        if any(len(values) > 1 for values in member_value_sets.values()):
            continue
        deduped_items = [values[0] for values in values_by_member.values()]
        unique_members = set(values_by_member)
        if len(unique_members) < 2:
            continue
        ordered = sorted(deduped_items, key=lambda item: item[0])
        template = ordered[0][1]
        dimensionless_values = dimensionless_totals.get(
            (template.period_end, template.unit, template.source_document),
            set(),
        )
        current_schedule_invalid = False
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
                # The first bucket is only the 12-MONTH current portion when
                # the next bucket starts ~one year later; quarterly or
                # semi-annual schedules would otherwise pass one quarter of
                # revenue off as current RPO.
                and (
                    len(member_dates) < 2
                    or timedelta(days=330)
                    <= member_dates[1] - member_dates[0]
                    <= timedelta(days=400)
                )
            )
            current_schedule_invalid = not current_bucket_validated
        except ValueError:
            current_bucket_validated = False
        current_fraction: float | None = None
        current_fraction_outside_range = False
        if len(dimensionless_values) == 1:
            dimensionless_total = next(iter(dimensionless_values))
            current_fraction = (
                float(ordered[0][1].numeric_value or 0.0)
                / dimensionless_total
                if dimensionless_total > 0.0
                else 0.0
            )
            current_fraction_outside_range = not (
                0.05 <= current_fraction <= 1.0
            )
            if current_fraction_outside_range:
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
        dimensionless_total_available = len(dimensionless_values) == 1
        output.append(
            (
                aggregate,
                current,
                current_bucket_validated,
                dimensionless_total_available,
                current_schedule_invalid,
                current_fraction_outside_range,
                current_fraction,
            )
        )
        consumed.update(id(fact) for _, fact in items)
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


def _calculation_network_evidence(
    facts: tuple[NormalizedFact, ...],
) -> list[MetricEvidence]:
    grouped: dict[
        tuple[str, str, str, str, str, str],
        dict[str, Any],
    ] = {}
    for fact in facts:
        if (
            fact.numeric_value is None
            or fact.scope != "consolidated"
            or fact.dimensions_json != "{}"
        ):
            continue
        metadata = _fact_metadata(fact)
        relationships = metadata.get("calculation_relationships")
        if not isinstance(relationships, list):
            continue
        for relationship in relationships:
            if (
                not isinstance(relationship, dict)
                or relationship.get("direction") != "incoming"
            ):
                continue
            parent = str(relationship.get("related_concept") or "")
            linkrole = str(relationship.get("linkrole") or "")
            network_children = relationship.get("network_children")
            if not parent or not isinstance(network_children, list):
                continue
            expected: dict[str, float] = {}
            valid_network = True
            for child in network_children:
                if not isinstance(child, dict):
                    valid_network = False
                    break
                child_name = str(child.get("concept_name") or "")
                weight_value = child.get("weight")
                try:
                    if weight_value is None:
                        valid_network = False
                        break
                    weight = float(weight_value)
                except (TypeError, ValueError):
                    valid_network = False
                    break
                if not child_name or not math.isfinite(weight):
                    valid_network = False
                    break
                expected[child_name] = weight
            if not valid_network or len(expected) < 2:
                continue
            key = (
                parent,
                linkrole,
                fact.period_start,
                fact.period_end,
                fact.unit,
                fact.source_document,
            )
            state = grouped.setdefault(
                key,
                {
                    "expected": expected,
                    "facts": defaultdict(list),
                },
            )
            if state["expected"] != expected:
                state["expected"] = {}
                continue
            state["facts"][fact.concept_name].append(fact)
    output: list[MetricEvidence] = []
    for key, state in grouped.items():
        parent, linkrole, _, _, _, _ = key
        expected = state["expected"]
        child_facts = state["facts"]
        if not expected or set(child_facts) != set(expected):
            continue
        selected: list[NormalizedFact] = []
        ambiguous = False
        for child_name in sorted(expected):
            candidates = child_facts[child_name]
            values = {
                round(float(candidate.numeric_value or 0.0), 6)
                for candidate in candidates
            }
            if len(values) != 1:
                ambiguous = True
                break
            selected.append(candidates[0])
        if ambiguous:
            continue
        template = selected[0]
        synthetic = replace(
            template,
            concept_name=parent,
            concept_metadata_json="{}",
        )
        mapping = _fact_metric(synthetic)
        if mapping is None:
            continue
        metric_name, concept_name = mapping
        value = sum(
            float(fact.numeric_value or 0.0)
            * expected[fact.concept_name]
            for fact in selected
        )
        if value < 0.0:
            continue
        output.append(
            _fact_evidence(
                synthetic,
                metric_name=metric_name,
                concept_name=concept_name,
                value=value,
                accepted=True,
                reason="complete_issuer_calculation_network_aggregation",
                provenance={
                    "derivation_type": "issuer_calculation_linkbase_sum",
                    "calculation_parent": parent,
                    "calculation_linkrole": linkrole,
                    "calculation_children": [
                        {
                            "concept_name": fact.concept_name,
                            "value": fact.numeric_value,
                            "weight": expected[fact.concept_name],
                            "context_id": fact.context_id,
                        }
                        for fact in selected
                    ],
                },
            )
        )
    return output


def map_normalized_facts(
    item: WorkItem,
    facts: tuple[NormalizedFact, ...],
) -> tuple[MetricEvidence, ...]:
    evidence: list[MetricEvidence] = []
    evidence.extend(_calculation_network_evidence(facts))
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
    for (
        aggregate,
        current,
        current_bucket_validated,
        dimensionless_total_available,
        current_schedule_invalid,
        current_fraction_outside_range,
        current_fraction,
    ) in timing_aggregates:
        standard_taxonomy = _standard_taxonomy(aggregate)
        accepted = standard_taxonomy and current_bucket_validated
        aggregate_evidence = _fact_evidence(
                aggregate,
                metric_name="remaining_performance_obligation",
                concept_name="RemainingPerformanceObligation",
                value=float(aggregate.numeric_value or 0.0),
                accepted=accepted and not dimensionless_total_available,
                reason=(
                    "dimensionless_total_supersedes_timing_dimension_sum"
                    if dimensionless_total_available
                    else (
                        "standard_timing_dimension_exhaustive_sum"
                        if accepted
                        else (
                            "timing_dimension_incomplete_schedule_requires_sector_review"
                            if standard_taxonomy
                            else "timing_dimension_sum_requires_sector_review"
                        )
                    )
                ),
                provenance={
                    "derivation_type": "exhaustive_dimension_sum",
                    "timing_schedule_complete": current_bucket_validated,
                    "dimensionless_total_available": (
                        dimensionless_total_available
                    ),
                },
            )
        if dimensionless_total_available:
            aggregate_evidence = replace(
                aggregate_evidence,
                status="REJECTED_POLICY",
                confidence=0.99,
            )
        else:
            total_rpo_facts.append(aggregate)
        evidence.append(aggregate_evidence)
        current_evidence = _fact_evidence(
                current,
                metric_name="rpo_current",
                concept_name="RemainingPerformanceObligationCurrent",
                value=float(current.numeric_value or 0.0),
                accepted=accepted and current_bucket_validated,
                reason=(
                    "timing_dimension_current_fraction_outside_valid_range"
                    if current_fraction_outside_range
                    else (
                        "timing_dimension_current_bucket_not_twelve_months"
                        if current_schedule_invalid
                        else (
                            "standard_earliest_timing_dimension_bucket"
                            if accepted and current_bucket_validated
                            else (
                                "timing_dimension_current_requires_sector_review"
                            )
                        )
                    )
                ),
                provenance={
                    "derivation_type": "earliest_timing_dimension_bucket",
                    "current_fraction_of_dimensionless_total": (
                        current_fraction
                    ),
                },
            )
        if current_fraction_outside_range or current_schedule_invalid:
            current_evidence = replace(
                current_evidence,
                status="REJECTED_POLICY",
                confidence=0.99,
            )
        evidence.append(current_evidence)
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
        if metric_name == "debt_total":
            accepted = False
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
                        "explicit_total_debt_fact_requires_zero_debt_review"
                        if metric_name == "debt_total"
                        else (
                        "standard_taxonomy_consolidated_semantic_fact"
                        if accepted
                        else (
                            "extension_or_dimensional_fact_requires_sector_review"
                        )
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


def _orders_interval_key(
    evidence: MetricEvidence,
) -> tuple[str, str, str, str, str]:
    return (
        evidence.source_document,
        evidence.period_start,
        evidence.period_end,
        evidence.unit.upper(),
        evidence.scope,
    )


def _derived_orders_quarter(
    broad: MetricEvidence,
    narrow: MetricEvidence,
) -> MetricEvidence | None:
    if (
        broad.metric_name != "orders"
        or narrow.metric_name != "orders"
        or broad.status != "ACCEPTED"
        or narrow.status != "ACCEPTED"
        or broad.value is None
        or narrow.value is None
        or broad.source_document != narrow.source_document
        or broad.unit.upper() != narrow.unit.upper()
        or broad.scope != "consolidated"
        or narrow.scope != "consolidated"
    ):
        return None
    try:
        broad_start = date.fromisoformat(broad.period_start)
        broad_end = date.fromisoformat(broad.period_end)
        narrow_start = date.fromisoformat(narrow.period_start)
        narrow_end = date.fromisoformat(narrow.period_end)
    except ValueError:
        return None
    if not (
        broad_start <= narrow_start
        and narrow_end <= broad_end
        and (broad_start == narrow_start or broad_end == narrow_end)
    ):
        return None
    if broad_start == narrow_start and broad_end > narrow_end:
        period_start = narrow_end + timedelta(days=1)
        period_end = broad_end
    elif broad_end == narrow_end and broad_start < narrow_start:
        period_start = broad_start
        period_end = narrow_start - timedelta(days=1)
    else:
        return None
    duration_days = (period_end - period_start).days
    value = float(broad.value) - float(narrow.value)
    if not 45 <= duration_days <= 130 or value < 0.0:
        return None
    return MetricEvidence(
        metric_name="orders",
        concept_name="OrdersDerivedDiscreteQuarter",
        value=value,
        unit=broad.unit,
        period_start=period_start.isoformat(),
        period_end=period_end.isoformat(),
        scope="consolidated",
        confidence=max(
            0.0,
            min(0.96, broad.confidence, narrow.confidence) - 0.02,
        ),
        status="ACCEPTED",
        reason="explicit_same_filing_interval_arithmetic",
        evidence_text=(
            f"Derived discrete orders {value:g} {broad.unit} as "
            f"{broad.value:g} ({broad.period_start}/{broad.period_end}) - "
            f"{narrow.value:g} ({narrow.period_start}/{narrow.period_end})."
        ),
        source_document=broad.source_document,
        extraction_method=(
            "dedicated_parser:explicit_interval_arithmetic"
        ),
        provenance={
            "adapter_version": ADAPTER_VERSION,
            "derivation_type": "explicit_disclosure_interval_subtraction",
            "broad_operand": {
                "concept_name": broad.concept_name,
                "period_start": broad.period_start,
                "period_end": broad.period_end,
                "value": broad.value,
                "extraction_method": broad.extraction_method,
            },
            "narrow_operand": {
                "concept_name": narrow.concept_name,
                "period_start": narrow.period_start,
                "period_end": narrow.period_end,
                "value": narrow.value,
                "extraction_method": narrow.extraction_method,
            },
        },
    )


def postprocess_metric_evidence(
    item: WorkItem,
    evidence: tuple[MetricEvidence, ...],
) -> tuple[MetricEvidence, ...]:
    output = list(evidence)
    order_rows: dict[
        tuple[str, str, str, str, str],
        MetricEvidence,
    ] = {}
    conflicting_intervals: set[tuple[str, str, str, str, str]] = set()
    for row in evidence:
        if (
            row.metric_name != "orders"
            or row.status != "ACCEPTED"
            or row.value is None
        ):
            continue
        key = _orders_interval_key(row)
        previous = order_rows.get(key)
        if (
            previous is not None
            and previous.value is not None
            and abs(float(previous.value) - float(row.value))
            > max(1.0, abs(float(row.value)) * 1e-9)
        ):
            conflicting_intervals.add(key)
            continue
        if previous is None or row.confidence > previous.confidence:
            order_rows[key] = row
    eligible_rows = [
        row
        for key, row in order_rows.items()
        if key not in conflicting_intervals
    ]
    derived_by_interval: dict[
        tuple[str, str, str, str, str],
        list[MetricEvidence],
    ] = {}
    for broad in eligible_rows:
        for narrow in eligible_rows:
            if broad is narrow:
                continue
            derived = _derived_orders_quarter(broad, narrow)
            if derived is None:
                continue
            derived_by_interval.setdefault(
                _orders_interval_key(derived),
                [],
            ).append(derived)
    for candidates in derived_by_interval.values():
        values = {
            round(float(candidate.value or 0.0), 6)
            for candidate in candidates
        }
        if len(values) != 1:
            output.extend(
                replace(
                    candidate,
                    status="REVIEW_REQUIRED",
                    confidence=min(candidate.confidence, 0.70),
                    reason="conflicting_interval_arithmetic_candidates",
                )
                for candidate in candidates
            )
            continue
        output.append(
            max(candidates, key=lambda candidate: candidate.confidence)
        )
    unique: dict[str, MetricEvidence] = {}
    for row in output:
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
                row.period_start,
                row.value if row.value is not None else float("-inf"),
                row.status,
                row.source_document,
                row.extraction_method,
            ),
        )
    )
