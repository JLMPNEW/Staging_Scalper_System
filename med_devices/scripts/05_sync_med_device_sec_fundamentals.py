#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import hashlib
import html
import json
import logging
import math
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import requests  # type: ignore[reportMissingModuleSource]


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from med_devices.core.config import cfg_get, load_yaml, resolve_path  # noqa: E402
from med_devices.core.db import connect, finish_run, init_db, start_run, utc_now  # noqa: E402
from med_devices.core.logging_utils import configure_utc_logging  # noqa: E402
from med_devices.core.source_registry import load_source_registry, upsert_source_registry  # noqa: E402
from med_devices.core.text_norm import normalize_cik, normalize_ticker  # noqa: E402


LOGGER = logging.getLogger("sync_med_device_sec_fundamentals")
DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
DEFAULT_SEC_SUBMISSIONS_SOURCE = "sec_submissions"
DEFAULT_SEC_COMPANYFACTS_SOURCE = "sec_companyfacts"
DEFAULT_SEC_INLINE_XBRL_SOURCE = "sec_inline_xbrl_filing"
DEFAULT_SEC_ARCHIVES_BASE = "https://www.sec.gov/Archives/edgar/data"
DEFAULT_SEC_SUBMISSIONS_FILE_URL = "https://data.sec.gov/submissions/{file_name}"
DEFAULT_SEC_SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"
DEFAULT_SEC_COMPANYFACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"

FIELDNAMES = [
    "ticker",
    "company_id",
    "cik",
    "company_name",
    "submissions_status",
    "companyfacts_status",
    "submissions_payload_source",
    "companyfacts_payload_source",
    "sec_asof_date",
    "filing_rows",
    "financial_statement_rows",
    "inline_xbrl_fallback_rows",
    "first_period_end",
    "latest_period_end",
    "latest_financial_filing_date",
    "latest_parsed_filed_date",
    "review_reason",
]

DEFAULT_FINANCIAL_FORMS = {"10-K", "10-K/A", "10-Q", "10-Q/A", "20-F", "20-F/A", "40-F", "40-F/A"}
DEFAULT_FILING_FORMS = {
    *DEFAULT_FINANCIAL_FORMS,
    "8-K",
    "8-K/A",
    "6-K",
    "6-K/A",
}
PRIMARY_FINANCIAL_OBSERVATION_ORDER = (
    "revenue",
    "gross_profit",
    "operating_income",
    "net_income",
    "operating_cash_flow",
)

DEFAULT_METRIC_CONCEPTS: dict[str, list[str]] = {
    "revenue": [
        "RevenueFromContractWithCustomerExcludingAssessedTax",
        "RevenueFromContractWithCustomerIncludingAssessedTax",
        "Revenues",
        "SalesRevenueNet",
        "SalesRevenueGoodsNet",
        "SalesRevenueServicesNet",
        "Revenue",
        "RevenueFromContractsWithCustomers",
    ],
    "gross_profit": ["GrossProfit"],
    "operating_income": ["OperatingIncomeLoss", "ProfitLossFromOperatingActivities"],
    "cost_of_revenue": [
        "CostOfRevenue",
        "CostOfGoodsAndServicesSold",
        "CostOfGoodsSold",
        "CostOfGoodsAndServiceExcludingDepreciationDepletionAndAmortization",
    ],
    "selling_general_admin": [
        "SellingGeneralAndAdministrativeExpense",
        "GeneralAndAdministrativeExpense",
        "SellingAndMarketingExpense",
    ],
    "net_income": ["NetIncomeLoss", "ProfitLoss"],
    "operating_cash_flow": [
        "NetCashProvidedByUsedInOperatingActivities",
        "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations",
        "CashFlowsFromUsedInOperatingActivitiesContinuingOperations",
        "CashFlowsFromUsedInOperations",
    ],
    "capital_expenditures": [
        "PaymentsToAcquirePropertyPlantAndEquipment",
        "PaymentsToAcquirePropertyAndEquipment",
        "PaymentsToAcquireProductiveAssets",
        "PaymentsToAcquireOtherProductiveAssets",
        "PurchaseOfPropertyPlantAndEquipmentClassifiedAsInvestingActivities",
    ],
    "research_and_development": [
        "ResearchAndDevelopmentExpense",
        "ResearchAndDevelopmentExpenseExcludingAcquiredInProcessCost",
    ],
    "interest_expense": [
        "InterestExpense",
        "InterestAndDebtExpense",
        "FinanceCosts",
        "InterestExpenseOnBorrowings",
    ],
    "cash_and_investments": [
        "CashCashEquivalentsAndShortTermInvestments",
        "CashAndCashEquivalentsAtCarryingValue",
        "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents",
        "CashAndCashEquivalentsAtFairValue",
        "CashAndCashEquivalents",
    ],
    "total_assets": ["Assets"],
    "stockholders_equity": [
        "StockholdersEquity",
        "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest",
        "Equity",
        "EquityAttributableToOwnersOfParent",
    ],
    "shares_outstanding": [
        "EntityCommonStockSharesOutstanding",
        "WeightedAverageNumberOfDilutedSharesOutstanding",
        "WeightedAverageNumberOfSharesOutstandingDiluted",
        "WeightedAverageNumberOfBasicSharesOutstanding",
        "WeightedAverageShares",
        "NumberOfSharesIssued",
    ],
}
DEFAULT_CASH_AND_INVESTMENTS_COMBINED_CONCEPTS = [
    "CashCashEquivalentsAndShortTermInvestments",
    "CashCashEquivalentsAndMarketableSecurities",
]
DEFAULT_CASH_AND_INVESTMENTS_CASH_CONCEPTS = [
    "CashAndCashEquivalentsAtCarryingValue",
    "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents",
    "CashAndCashEquivalentsAtFairValue",
    "CashAndCashEquivalents",
]
DEFAULT_CASH_AND_INVESTMENTS_SECURITIES_CONCEPTS = [
    "ShortTermInvestments",
    "ShortTermInvestmentsAvailableForSale",
    "MarketableSecurities",
    "MarketableSecuritiesCurrent",
    "AvailableForSaleSecuritiesCurrent",
    "DebtSecuritiesAvailableForSaleCurrent",
]
DEFAULT_CAPEX_COMPONENT_CONCEPTS = [
    "PaymentsToAcquireBuildings",
    "PaymentsToAcquireOtherPropertyPlantAndEquipment",
    "PaymentsToAcquireMachineryAndEquipment",
    "PaymentsToAcquireFurnitureAndFixtures",
    "PaymentsToAcquireLeaseholdImprovements",
]
DEFAULT_DEBT_DIRECT_CONCEPTS = [
    "DebtAndFinanceLeaseObligations",
    "LongTermDebtAndFinanceLeaseObligations",
    "LongTermDebt",
    "Debt",
    "Borrowings",
    "FinanceLeaseLiability",
    "DebtInstrumentCarryingAmount",
    "NotesPayable",
    "ConvertibleDebt",
    "SecuredDebt",
]
DEFAULT_DEBT_CURRENT_CONCEPTS = [
    "LongTermDebtCurrent",
    "LongTermDebtAndFinanceLeaseObligationsCurrent",
    "LongTermDebtAndCapitalLeaseObligationsCurrent",
    "ShortTermBorrowings",
    "ShortTermDebt",
    "CurrentBorrowingsAndCurrentPortionOfNoncurrentBorrowings",
    "CurrentPortionOfLongtermBorrowings",
    "ShorttermBorrowings",
    "FinanceLeaseLiabilityCurrent",
    "ConvertibleDebtCurrent",
    "ConvertibleNotesPayableCurrent",
    "NotesPayableCurrent",
    "OtherNotesPayableCurrent",
    "NotesPayableRelatedPartiesClassifiedCurrent",
    "SecuredDebtCurrent",
    "LinesOfCreditCurrent",
]
DEFAULT_DEBT_NONCURRENT_CONCEPTS = [
    "LongTermDebtNoncurrent",
    "LongTermDebtAndFinanceLeaseObligationsNoncurrent",
    "LongTermDebtAndCapitalLeaseObligations",
    "LongTermDebtAndCapitalLeaseObligationsNoncurrent",
    "LongtermBorrowings",
    "FinanceLeaseLiabilityNoncurrent",
    "ConvertibleDebtNoncurrent",
    "LongTermNotesPayable",
    "NotesPayableRelatedPartiesNoncurrent",
    "SecuredDebtNoncurrent",
]


@dataclass(frozen=True)
class Company:
    company_id: int
    ticker: str
    cik: str
    company_name: str


@dataclass(frozen=True)
class FactObservation:
    metric: str
    concept: str
    unit: str
    value: float
    period_start: str
    period_end: str
    fiscal_year: int | None
    fiscal_period: str
    form: str
    filed_date: str
    accession_nodash: str
    frame: str
    concept_rank: int


@dataclass(frozen=True)
class SecIngestionPolicy:
    submissions_source_id: str
    companyfacts_source_id: str
    inline_xbrl_source_id: str
    sec_archives_base: str
    submissions_url_template: str
    companyfacts_url_template: str
    submissions_file_url_template: str
    fetch_paginated_submissions: bool
    inline_xbrl_fallback_enabled: bool
    inline_xbrl_max_filings_per_company: int
    forms: set[str]
    financial_forms: set[str]
    metric_concepts: dict[str, list[str]]
    debt_direct_concepts: list[str]
    debt_current_concepts: list[str]
    debt_noncurrent_concepts: list[str]
    capex_component_concepts: list[str]
    cash_and_investments_combined_concepts: list[str]
    cash_and_investments_cash_concepts: list[str]
    cash_and_investments_securities_concepts: list[str]
    composite_debt_concept_rank: int
    preferred_units: dict[str, set[str]]
    annual_gross_profit_from_operating_expenses_tickers: set[str]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Sync SEC submissions and companyfacts into the med-devices DB.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--output-csv", type=Path, default=None)
    parser.add_argument("--tickers", type=str, default="", help="Optional comma-separated ticker subset.")
    parser.add_argument("--max-tickers", type=int, default=0)
    parser.add_argument("--refresh-cache", action="store_true")
    parser.add_argument(
        "--refresh-submissions",
        action="store_true",
        help="Fetch every issuer's root SEC submissions index live.",
    )
    parser.add_argument(
        "--asof",
        default="",
        help="Attribute source-discovery evidence to this as-of date (YYYY-MM-DD).",
    )
    parser.add_argument("--allow-partial", action="store_true")
    return parser.parse_args()


def parse_float(raw: object) -> float | None:
    try:
        value = float(str(raw).strip())
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def accession_nodash(raw: object) -> str:
    return "".join(ch for ch in str(raw or "") if ch.isalnum()).upper()


def normalize_form(raw: object) -> str:
    return str(raw or "").strip().upper()


def as_bool(raw: object, *, default: bool) -> bool:
    if raw is None:
        return default
    text = str(raw).strip().lower()
    if text in {"1", "true", "yes", "y", "on"}:
        return True
    if text in {"0", "false", "no", "n", "off"}:
        return False
    return default


def form_set(raw: object, default: set[str]) -> set[str]:
    values = raw if isinstance(raw, list) else list(default)
    out = {normalize_form(value) for value in values if normalize_form(value)}
    return out or set(default)


def concept_list(raw: object, default: list[str]) -> list[str]:
    values = raw if isinstance(raw, list) else list(default)
    out = [str(value or "").strip() for value in values if str(value or "").strip()]
    return out or list(default)


def concept_map(raw: object, default: dict[str, list[str]]) -> dict[str, list[str]]:
    if not isinstance(raw, dict):
        return {metric: list(concepts) for metric, concepts in default.items()}
    out: dict[str, list[str]] = {}
    for metric, values in raw.items():
        cleaned = concept_list(values, [])
        if cleaned:
            out[str(metric).strip()] = cleaned
    return out or {metric: list(concepts) for metric, concepts in default.items()}


def ticker_set(raw: object) -> set[str]:
    values = raw if isinstance(raw, list) else []
    return {normalize_ticker(value) for value in values if normalize_ticker(value)}


def preferred_units_map(raw: object) -> dict[str, set[str]]:
    defaults = {"default": {"USD"}, "shares_outstanding": {"SHARES"}}
    if not isinstance(raw, dict):
        return defaults
    out: dict[str, set[str]] = {}
    for metric, values in raw.items():
        if not isinstance(values, list):
            continue
        cleaned = {str(value or "").strip().upper() for value in values if str(value or "").strip()}
        if cleaned:
            out[str(metric).strip()] = cleaned
    return {**defaults, **out}


def sec_ingestion_policy(config: dict[str, Any]) -> SecIngestionPolicy:
    debt_cfg = cfg_get(config, "sec_ingestion.debt_concepts", {})
    debt_cfg = debt_cfg if isinstance(debt_cfg, dict) else {}
    liquidity_cfg = cfg_get(config, "sec_ingestion.cash_and_investments_concepts", {})
    liquidity_cfg = liquidity_cfg if isinstance(liquidity_cfg, dict) else {}
    return SecIngestionPolicy(
        submissions_source_id=str(
            cfg_get(config, "sec_ingestion.submissions_source_id", DEFAULT_SEC_SUBMISSIONS_SOURCE)
            or DEFAULT_SEC_SUBMISSIONS_SOURCE
        ),
        companyfacts_source_id=str(
            cfg_get(config, "sec_ingestion.companyfacts_source_id", DEFAULT_SEC_COMPANYFACTS_SOURCE)
            or DEFAULT_SEC_COMPANYFACTS_SOURCE
        ),
        inline_xbrl_source_id=str(
            cfg_get(config, "sec_ingestion.inline_xbrl_source_id", DEFAULT_SEC_INLINE_XBRL_SOURCE)
            or DEFAULT_SEC_INLINE_XBRL_SOURCE
        ),
        sec_archives_base=str(
            cfg_get(config, "sec_ingestion.sec_archives_base", DEFAULT_SEC_ARCHIVES_BASE) or DEFAULT_SEC_ARCHIVES_BASE
        ).rstrip("/"),
        submissions_url_template=str(
            cfg_get(config, "sec_ingestion.submissions_url_template", DEFAULT_SEC_SUBMISSIONS_URL)
            or DEFAULT_SEC_SUBMISSIONS_URL
        ),
        companyfacts_url_template=str(
            cfg_get(config, "sec_ingestion.companyfacts_url_template", DEFAULT_SEC_COMPANYFACTS_URL)
            or DEFAULT_SEC_COMPANYFACTS_URL
        ),
        submissions_file_url_template=str(
            cfg_get(config, "sec_ingestion.submissions_file_url_template", DEFAULT_SEC_SUBMISSIONS_FILE_URL)
            or DEFAULT_SEC_SUBMISSIONS_FILE_URL
        ),
        fetch_paginated_submissions=as_bool(
            cfg_get(config, "sec_ingestion.fetch_paginated_submissions", True),
            default=True,
        ),
        inline_xbrl_fallback_enabled=as_bool(
            cfg_get(config, "sec_ingestion.inline_xbrl_fallback_enabled", True),
            default=True,
        ),
        inline_xbrl_max_filings_per_company=max(
            1,
            int(cfg_get(config, "sec_ingestion.inline_xbrl_max_filings_per_company", 1)),
        ),
        forms=form_set(cfg_get(config, "sec_ingestion.forms", list(DEFAULT_FILING_FORMS)), DEFAULT_FILING_FORMS),
        financial_forms=form_set(
            cfg_get(config, "sec_ingestion.financial_forms", list(DEFAULT_FINANCIAL_FORMS)),
            DEFAULT_FINANCIAL_FORMS,
        ),
        metric_concepts=concept_map(
            cfg_get(config, "sec_ingestion.metric_concepts", DEFAULT_METRIC_CONCEPTS),
            DEFAULT_METRIC_CONCEPTS,
        ),
        debt_direct_concepts=concept_list(debt_cfg.get("direct"), DEFAULT_DEBT_DIRECT_CONCEPTS),
        debt_current_concepts=concept_list(debt_cfg.get("current"), DEFAULT_DEBT_CURRENT_CONCEPTS),
        debt_noncurrent_concepts=concept_list(debt_cfg.get("noncurrent"), DEFAULT_DEBT_NONCURRENT_CONCEPTS),
        capex_component_concepts=concept_list(
            cfg_get(config, "sec_ingestion.capex_component_concepts", DEFAULT_CAPEX_COMPONENT_CONCEPTS),
            DEFAULT_CAPEX_COMPONENT_CONCEPTS,
        ),
        cash_and_investments_combined_concepts=concept_list(
            liquidity_cfg.get("combined"),
            DEFAULT_CASH_AND_INVESTMENTS_COMBINED_CONCEPTS,
        ),
        cash_and_investments_cash_concepts=concept_list(
            liquidity_cfg.get("cash"),
            DEFAULT_CASH_AND_INVESTMENTS_CASH_CONCEPTS,
        ),
        cash_and_investments_securities_concepts=concept_list(
            liquidity_cfg.get("securities"),
            DEFAULT_CASH_AND_INVESTMENTS_SECURITIES_CONCEPTS,
        ),
        composite_debt_concept_rank=int(cfg_get(config, "sec_ingestion.composite_debt_concept_rank", 1000)),
        preferred_units=preferred_units_map(cfg_get(config, "sec_ingestion.preferred_units", {})),
        annual_gross_profit_from_operating_expenses_tickers=ticker_set(
            cfg_get(config, "sec_ingestion.annual_gross_profit_from_operating_expenses_tickers", [])
        ),
    )


def cache_is_fresh(path: Path, ttl_hours: float) -> bool:
    if not path.exists() or ttl_hours <= 0:
        return False
    age_hours = (time.time() - path.stat().st_mtime) / 3600.0
    return age_hours <= ttl_hours


def load_json_file(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict):
        raise ValueError(f"Cached JSON root is not an object: {path}")
    return payload


def fetch_json(
    session: requests.Session,
    url: str,
    *,
    primary_cache_path: Path,
    fallback_cache_path: Path | None,
    refresh_cache: bool,
    cache_ttl_hours: float,
    timeout_sec: float,
    max_retries: int,
    sleep_sec: float,
    user_agent: str,
) -> tuple[dict[str, Any], str, str, int]:
    if not refresh_cache and cache_is_fresh(primary_cache_path, cache_ttl_hours):
        text = primary_cache_path.read_text(encoding="utf-8")
        return json.loads(text), text, "primary_cache", 200
    if not refresh_cache and fallback_cache_path is not None and fallback_cache_path.exists():
        text = fallback_cache_path.read_text(encoding="utf-8")
        return json.loads(text), text, "validation_cache", 200

    headers = {"User-Agent": user_agent, "Accept-Encoding": "gzip, deflate", "Host": "data.sec.gov"}
    last_status = 0
    last_text = ""
    for attempt in range(max(1, max_retries)):
        response = session.get(url, headers=headers, timeout=timeout_sec)
        last_status = int(response.status_code)
        last_text = response.text
        if response.status_code == 200:
            primary_cache_path.parent.mkdir(parents=True, exist_ok=True)
            primary_cache_path.write_text(last_text, encoding="utf-8")
            return response.json(), last_text, "fetched", last_status
        if response.status_code in {429, 500, 502, 503, 504} and attempt < max_retries - 1:
            time.sleep(max(0.1, sleep_sec) * (attempt + 1) * 2)
            continue
        break
    raise RuntimeError(f"SEC fetch failed status={last_status} url={url} body={last_text[:200]}")


def fetch_text(
    session: requests.Session,
    url: str,
    *,
    cache_path: Path,
    refresh_cache: bool,
    cache_ttl_hours: float,
    timeout_sec: float,
    max_retries: int,
    sleep_sec: float,
    user_agent: str,
) -> tuple[str, str, int]:
    if not refresh_cache and cache_is_fresh(cache_path, cache_ttl_hours):
        return cache_path.read_text(encoding="utf-8"), "primary_cache", 200

    headers = {"User-Agent": user_agent, "Accept-Encoding": "gzip, deflate"}
    last_status = 0
    last_text = ""
    for attempt in range(max(1, max_retries)):
        response = session.get(url, headers=headers, timeout=timeout_sec)
        last_status = int(response.status_code)
        last_text = response.text
        if response.status_code == 200:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_text(last_text, encoding="utf-8")
            return last_text, "fetched", last_status
        if response.status_code in {429, 500, 502, 503, 504} and attempt < max_retries - 1:
            time.sleep(max(0.1, sleep_sec) * (attempt + 1) * 2)
            continue
        break
    raise RuntimeError(f"SEC filing fetch failed status={last_status} url={url} body={last_text[:200]}")


INLINE_ATTR_RE = re.compile(r"([A-Za-z_:][-A-Za-z0-9_:.]*)\s*=\s*(\"[^\"]*\"|'[^']*'|[^\s>]+)")
INLINE_CONTEXT_RE = re.compile(
    r"<(?:[A-Za-z0-9_]+:)?context\b(?P<attrs>[^>]*)>(?P<body>.*?)</(?:[A-Za-z0-9_]+:)?context>",
    re.IGNORECASE | re.DOTALL,
)
INLINE_UNIT_RE = re.compile(
    r"<(?:[A-Za-z0-9_]+:)?unit\b(?P<attrs>[^>]*)>(?P<body>.*?)</(?:[A-Za-z0-9_]+:)?unit>",
    re.IGNORECASE | re.DOTALL,
)
INLINE_NON_FRACTION_RE = re.compile(
    r"<ix:nonfraction\b(?P<attrs>[^>]*)>(?P<body>.*?)</ix:nonfraction>",
    re.IGNORECASE | re.DOTALL,
)
INLINE_NON_NUMERIC_RE = re.compile(
    r"<ix:nonnumeric\b(?P<attrs>[^>]*)>(?P<body>.*?)</ix:nonnumeric>",
    re.IGNORECASE | re.DOTALL,
)
INLINE_TAG_RE = re.compile(r"<[^>]+>")
INLINE_NUMBER_RE = re.compile(r"[-+]?\d+(?:\.\d+)?")


def parse_inline_attrs(raw: str) -> dict[str, str]:
    attrs: dict[str, str] = {}
    for match in INLINE_ATTR_RE.finditer(raw or ""):
        key = match.group(1).lower()
        value = match.group(2).strip()
        if (value.startswith('"') and value.endswith('"')) or (value.startswith("'") and value.endswith("'")):
            value = value[1:-1]
        attrs[key] = html.unescape(value)
    return attrs


def clean_inline_text(raw_html: str) -> str:
    without_exclusions = re.sub(
        r"<ix:exclude\b.*?</ix:exclude>",
        "",
        raw_html or "",
        flags=re.IGNORECASE | re.DOTALL,
    )
    return html.unescape(INLINE_TAG_RE.sub("", without_exclusions)).replace("\xa0", " ").strip()


def normalized_inline_date(raw: object) -> str:
    text = str(raw or "").strip()[:10]
    try:
        return datetime.strptime(text, "%Y-%m-%d").date().isoformat()
    except ValueError:
        return ""


def parse_inline_contexts(document_text: str) -> dict[str, dict[str, Any]]:
    contexts: dict[str, dict[str, Any]] = {}
    for match in INLINE_CONTEXT_RE.finditer(document_text):
        attrs = parse_inline_attrs(match.group("attrs"))
        context_id = attrs.get("id", "").strip()
        if not context_id:
            continue
        body = match.group("body")
        start_match = re.search(
            r"<(?:[A-Za-z0-9_]+:)?startdate[^>]*>(.*?)</(?:[A-Za-z0-9_]+:)?startdate>",
            body,
            re.IGNORECASE | re.DOTALL,
        )
        end_match = re.search(
            r"<(?:[A-Za-z0-9_]+:)?enddate[^>]*>(.*?)</(?:[A-Za-z0-9_]+:)?enddate>",
            body,
            re.IGNORECASE | re.DOTALL,
        )
        instant_match = re.search(
            r"<(?:[A-Za-z0-9_]+:)?instant[^>]*>(.*?)</(?:[A-Za-z0-9_]+:)?instant>",
            body,
            re.IGNORECASE | re.DOTALL,
        )
        has_dimensions = bool(re.search(r"<(?:[A-Za-z0-9_]+:)?(?:explicitmember|typedmember)\b", body, re.IGNORECASE))
        start = normalized_inline_date(clean_inline_text(start_match.group(1))) if start_match else ""
        end_raw = clean_inline_text(end_match.group(1)) if end_match else ""
        instant_raw = clean_inline_text(instant_match.group(1)) if instant_match else ""
        contexts[context_id] = {
            "start_date": start,
            "end_date": normalized_inline_date(end_raw or instant_raw),
            "has_dimensions": has_dimensions,
        }
    return contexts


def parse_inline_units(document_text: str) -> dict[str, str]:
    units: dict[str, str] = {}
    for match in INLINE_UNIT_RE.finditer(document_text):
        attrs = parse_inline_attrs(match.group("attrs"))
        unit_id = attrs.get("id", "").strip()
        if not unit_id:
            continue
        measures = re.findall(
            r"<(?:[A-Za-z0-9_]+:)?measure[^>]*>(.*?)</(?:[A-Za-z0-9_]+:)?measure>",
            match.group("body"),
            re.IGNORECASE | re.DOTALL,
        )
        measure = clean_inline_text(measures[0]) if measures else unit_id
        measure_upper = measure.upper()
        if measure_upper.startswith("ISO4217:"):
            units[unit_id] = measure_upper.split(":", 1)[1]
        elif measure_upper.endswith(":SHARES") or measure_upper == "SHARES":
            units[unit_id] = "SHARES"
        elif measure_upper.endswith(":PURE") or measure_upper == "PURE":
            units[unit_id] = "PURE"
        else:
            units[unit_id] = measure_upper
    return units


def parse_inline_number(raw_html: str, attrs: dict[str, str]) -> float | None:
    if attrs.get("xsi:nil", "").lower() in {"1", "true"}:
        return None
    text = clean_inline_text(raw_html)
    if not text or text.upper() in {"-", "--", "N/A", "NA"}:
        return None
    negative = "(" in text and ")" in text
    normalized = (
        text.replace(",", "").replace("$", "").replace("%", "").replace("(", "").replace(")", "").replace(" ", "")
    )
    match = INLINE_NUMBER_RE.search(normalized)
    if not match:
        return None
    try:
        value = float(match.group(0))
    except ValueError:
        return None
    if negative or attrs.get("sign", "").strip().startswith("-"):
        value = -abs(value)
    try:
        scale = int(attrs.get("scale", "0"))
    except ValueError:
        scale = 0
    if scale:
        value *= 10**scale
    return value if math.isfinite(value) else None


def inline_document_focus(document_text: str, *, form: str, report_date: str) -> tuple[int | None, str]:
    fiscal_year: int | None = None
    fiscal_period = ""
    for match in INLINE_NON_NUMERIC_RE.finditer(document_text):
        attrs = parse_inline_attrs(match.group("attrs"))
        concept = attrs.get("name", "").split(":")[-1].lower()
        value = clean_inline_text(match.group("body")).strip().upper()
        if concept == "documentfiscalperiodfocus" and value in {"FY", "Q1", "Q2", "Q3"}:
            fiscal_period = value
        elif concept == "documentfiscalyearfocus" and value.isdigit():
            fiscal_year = int(value)
    if not fiscal_period and normalize_form(form).startswith(("10-K", "20-F", "40-F")):
        fiscal_period = "FY"
    if fiscal_year is None:
        parsed_report_date = normalized_inline_date(report_date)
        fiscal_year = int(parsed_report_date[:4]) if parsed_report_date else None
    return fiscal_year, fiscal_period


def inline_xbrl_companyfacts(document_text: str, *, filing: dict[str, Any]) -> dict[str, Any]:
    report_date = normalized_inline_date(filing.get("report_date"))
    filing_date = normalized_inline_date(filing.get("filing_date"))
    form = normalize_form(filing.get("form"))
    accession = accession_nodash(filing.get("accession_nodash"))
    if not report_date or not filing_date or not form or not accession:
        raise ValueError("Inline XBRL fallback requires report date, filing date, form, and accession.")
    fiscal_year, fiscal_period = inline_document_focus(document_text, form=form, report_date=report_date)
    if form.startswith("10-Q") and fiscal_period not in {"Q1", "Q2", "Q3"}:
        raise ValueError(f"Could not determine fiscal-period focus for {accession}.")

    contexts = parse_inline_contexts(document_text)
    units = parse_inline_units(document_text)
    facts: dict[str, dict[str, Any]] = {}
    accepted = 0
    for match in INLINE_NON_FRACTION_RE.finditer(document_text):
        attrs = parse_inline_attrs(match.group("attrs"))
        name = attrs.get("name", "").strip()
        context_ref = attrs.get("contextref", "").strip()
        if ":" not in name or context_ref not in contexts:
            continue
        context = contexts[context_ref]
        if context.get("has_dimensions") or context.get("end_date") != report_date:
            continue
        value = parse_inline_number(match.group("body"), attrs)
        if value is None:
            continue
        taxonomy, concept = name.split(":", 1)
        taxonomy = taxonomy.lower()
        if taxonomy not in {"us-gaap", "ifrs-full", "dei"}:
            continue
        unit_ref = attrs.get("unitref", "").strip()
        unit = units.get(unit_ref, unit_ref.upper())
        observation = {
            "start": str(context.get("start_date") or ""),
            "end": report_date,
            "fy": fiscal_year,
            "fp": fiscal_period,
            "form": form,
            "filed": filing_date,
            "accn": accession,
            "val": value,
            "frame": f"inline_context:{context_ref}",
        }
        concept_payload = facts.setdefault(taxonomy, {}).setdefault(concept, {"units": {}})
        concept_payload["units"].setdefault(unit, []).append(observation)
        accepted += 1
    if not accepted:
        raise ValueError(f"No dimension-free inline XBRL facts matched report date {report_date} for {accession}.")
    return {"facts": facts}


def unrepresented_financial_filings(
    filing_rows: list[dict[str, Any]],
    financial_rows: list[dict[str, Any]],
    policy: SecIngestionPolicy,
) -> list[dict[str, Any]]:
    represented_accessions = {
        accession_nodash(row.get("accession_nodash")) for row in financial_rows if row.get("accession_nodash")
    }
    represented_periods = {
        (str(row.get("period_end") or ""), normalize_form(row.get("form"))): str(row.get("filed_date") or "")
        for row in financial_rows
    }
    candidates: list[dict[str, Any]] = []
    for filing in filing_rows:
        form = normalize_form(filing.get("form"))
        accession = accession_nodash(filing.get("accession_nodash"))
        report_date = normalized_inline_date(filing.get("report_date"))
        filing_date = normalized_inline_date(filing.get("filing_date"))
        primary_document = str(filing.get("primary_document") or "").strip()
        if (
            form not in policy.financial_forms
            or not accession
            or not report_date
            or not filing_date
            or not primary_document
        ):
            continue
        if accession in represented_accessions:
            continue
        if represented_periods.get((report_date, form), "") >= filing_date:
            continue
        candidates.append(filing)
    candidates.sort(
        key=lambda row: (str(row.get("filing_date") or ""), accession_nodash(row.get("accession_nodash"))),
        reverse=True,
    )
    return candidates[: policy.inline_xbrl_max_filings_per_company]


def build_inline_fallback_rows(
    company: Company,
    filing: dict[str, Any],
    document_text: str,
    policy: SecIngestionPolicy,
) -> list[dict[str, Any]]:
    report_date = normalized_inline_date(filing.get("report_date"))
    payload = inline_xbrl_companyfacts(document_text, filing=filing)
    rows = [
        row
        for row in build_financial_statement_rows(company, payload, policy)
        if row.get("period_end") == report_date
        and accession_nodash(row.get("accession_nodash")) == accession_nodash(filing.get("accession_nodash"))
    ]
    if not rows:
        raise ValueError(
            f"Inline XBRL facts did not produce a current statement row for {company.ticker} "
            f"{filing.get('accession_nodash')} report_date={report_date}."
        )
    source_url = str(filing.get("archive_url") or "")
    for row in rows:
        row["source_id"] = policy.inline_xbrl_source_id
        metric_payload = json.loads(str(row.get("payload_json") or "{}"))
        metric_payload["_filing_fallback"] = {
            "source": "sec_inline_xbrl_primary_document",
            "source_url": source_url,
            "report_date": report_date,
            "comparative_contexts_excluded": True,
        }
        row["payload_json"] = json.dumps(metric_payload, ensure_ascii=True, sort_keys=True)
    return rows


def ensure_source_registry(conn: Any, config: dict[str, Any], base_dir: Path, policy: SecIngestionPolicy) -> None:
    needed = {policy.submissions_source_id, policy.companyfacts_source_id}
    if policy.inline_xbrl_fallback_enabled:
        needed.add(policy.inline_xbrl_source_id)
    placeholders = ",".join("?" for _ in needed)
    existing = {
        str(row["source_id"])
        for row in conn.execute(
            f"SELECT source_id FROM source_registry WHERE source_id IN ({placeholders})",
            tuple(sorted(needed)),
        ).fetchall()
    }
    if needed.issubset(existing):
        return
    registry_path = resolve_path(cfg_get(config, "source_registry.path"), base_dir=base_dir)
    upsert_source_registry(conn, load_source_registry(registry_path))
    existing = {
        str(row["source_id"])
        for row in conn.execute(
            f"SELECT source_id FROM source_registry WHERE source_id IN ({placeholders})",
            tuple(sorted(needed)),
        ).fetchall()
    }
    missing = sorted(needed - existing)
    if missing:
        raise ValueError(f"Source registry missing required SEC source IDs: {', '.join(missing)}")


def start_ingestion_run(conn: Any, source_id: str) -> int:
    now = utc_now()
    cur = conn.execute(
        "INSERT INTO ingestion_runs(source_id, started_at, status, created_at) VALUES (?, ?, 'running', ?)",
        (source_id, now, now),
    )
    if cur.lastrowid is None:
        raise RuntimeError("Could not create ingestion run")
    return int(cur.lastrowid)


def finish_ingestion_run(
    conn: Any,
    *,
    ingestion_run_id: int,
    status: str,
    request_count: int,
    row_count: int,
    message: str,
) -> None:
    conn.execute(
        """
        UPDATE ingestion_runs
        SET completed_at = ?, status = ?, request_count = ?, row_count = ?, message = ?
        WHERE ingestion_run_id = ?
        """,
        (utc_now(), status, request_count, row_count, message, ingestion_run_id),
    )


def store_raw_response(
    conn: Any,
    *,
    source_id: str,
    endpoint: str,
    payload_text: str,
    response_status: int,
    ingestion_run_id: int,
    asof_date: str,
    payload_source: str,
    response_kind: str,
) -> None:
    now = utc_now()
    conn.execute(
        """
        INSERT OR IGNORE INTO raw_api_responses(
            source_id, endpoint, query_params_json, request_time_utc, response_status,
            response_hash, asof_date, payload_text, ingestion_run_id, created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            source_id,
            endpoint,
            json.dumps(
                {
                    "payload_source": payload_source,
                    "response_kind": response_kind,
                },
                ensure_ascii=True,
                sort_keys=True,
            ),
            now,
            response_status,
            hashlib.sha256(payload_text.encode("utf-8", errors="replace")).hexdigest(),
            asof_date,
            payload_text,
            ingestion_run_id,
            now,
        ),
    )


def load_companies(conn: Any, *, ticker_filter: set[str], max_tickers: int) -> list[Company]:
    rows = conn.execute(
        """
        SELECT company_id, ticker, cik, company_name
        FROM dim_company
        WHERE is_active = 1
        ORDER BY ticker
        """
    ).fetchall()
    out: list[Company] = []
    for row in rows:
        ticker = str(row["ticker"] or "").strip().upper()
        if ticker_filter and ticker not in ticker_filter:
            continue
        cik = normalize_cik(row["cik"])
        if not cik:
            continue
        out.append(Company(int(row["company_id"]), ticker, cik, str(row["company_name"] or "")))
        if max_tickers > 0 and len(out) >= max_tickers:
            break
    return out


def parse_recent_filings(
    company: Company,
    submissions: dict[str, Any],
    forms: set[str],
    policy: SecIngestionPolicy | None = None,
) -> list[dict[str, Any]]:
    policy = policy or sec_ingestion_policy({})
    filings = submissions.get("filings")
    if isinstance(filings, dict) and isinstance(filings.get("recent"), dict):
        source_payload = filings["recent"]
    else:
        source_payload = submissions
    if not isinstance(source_payload, dict):
        return []
    arrays: dict[str, list[Any]] = {}
    for name in ("accessionNumber", "form", "filingDate", "reportDate", "primaryDocument"):
        value = source_payload.get(name)
        arrays[name] = value if isinstance(value, list) else []
    max_len = max((len(value) for value in arrays.values()), default=0)
    # SEC archive paths use the unpadded numeric CIK, unlike data.sec.gov JSON paths.
    cik_int = str(int(company.cik))
    out: list[dict[str, Any]] = []
    for idx in range(max_len):
        form = normalize_form(arrays["form"][idx] if idx < len(arrays["form"]) else "")
        if forms and form not in forms:
            continue
        accession_raw = str(arrays["accessionNumber"][idx] if idx < len(arrays["accessionNumber"]) else "")
        nodash = accession_nodash(accession_raw)
        filing_date = str(arrays["filingDate"][idx] if idx < len(arrays["filingDate"]) else "")
        if not nodash or not filing_date:
            continue
        primary_doc = str(arrays["primaryDocument"][idx] if idx < len(arrays["primaryDocument"]) else "")
        archive_url = (
            f"{policy.sec_archives_base}/{cik_int}/{nodash}/{primary_doc}"
            if primary_doc
            else f"{policy.sec_archives_base}/{cik_int}/{nodash}/"
        )
        out.append(
            {
                "accession_nodash": nodash,
                "company_id": company.company_id,
                "form": form,
                "filing_date": filing_date,
                "report_date": str(arrays["reportDate"][idx] if idx < len(arrays["reportDate"]) else ""),
                "primary_document": primary_doc,
                "archive_url": archive_url,
                "source_id": policy.submissions_source_id,
            }
        )
    return out


def submission_file_names(submissions: dict[str, Any]) -> list[str]:
    filings = submissions.get("filings")
    files = filings.get("files") if isinstance(filings, dict) else []
    if not isinstance(files, list):
        return []
    out: list[str] = []
    for entry in files:
        if not isinstance(entry, dict):
            continue
        file_name = str(entry.get("name") or "").strip()
        if file_name:
            out.append(file_name)
    return out


def upsert_filings(conn: Any, rows: list[dict[str, Any]]) -> int:
    if not rows:
        return 0
    deduped: dict[str, dict[str, Any]] = {}
    for row in rows:
        accession = str(row.get("accession_nodash") or "")
        if not accession:
            continue
        existing = deduped.get(accession)
        if existing is None or str(row.get("filing_date") or "") >= str(existing.get("filing_date") or ""):
            deduped[accession] = row
    rows = list(deduped.values())
    if not rows:
        return 0
    now = utc_now()
    fields = [
        "accession_nodash",
        "company_id",
        "form",
        "filing_date",
        "report_date",
        "primary_document",
        "archive_url",
        "source_id",
    ]
    conn.executemany(
        """
        INSERT INTO fact_sec_filing(
            accession_nodash, company_id, form, filing_date, report_date, primary_document,
            archive_url, source_id, created_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(accession_nodash) DO UPDATE SET
            company_id = excluded.company_id,
            form = excluded.form,
            filing_date = excluded.filing_date,
            report_date = excluded.report_date,
            primary_document = excluded.primary_document,
            archive_url = excluded.archive_url,
            source_id = excluded.source_id,
            updated_at = excluded.updated_at
        """,
        [tuple(row.get(field) for field in fields) + (now, now) for row in rows],
    )
    return len(rows)


def preferred_unit(metric: str, unit: str, policy: SecIngestionPolicy | None = None) -> bool:
    policy = policy or sec_ingestion_policy({})
    unit_upper = str(unit or "").upper()
    allowed = policy.preferred_units.get(metric) or policy.preferred_units.get("default") or {"USD"}
    return unit_upper in allowed


def flatten_metric_observations(
    companyfacts: dict[str, Any],
    metric: str,
    concepts: list[str],
    policy: SecIngestionPolicy | None = None,
) -> list[FactObservation]:
    policy = policy or sec_ingestion_policy({})
    facts_root = companyfacts.get("facts", {})
    if not isinstance(facts_root, dict):
        return []
    out: list[FactObservation] = []
    for concept_rank, concept in enumerate(concepts):
        for taxonomy in ("us-gaap", "ifrs-full", "dei"):
            facts = facts_root.get(taxonomy, {})
            if not isinstance(facts, dict):
                continue
            payload = facts.get(concept)
            if not isinstance(payload, dict):
                continue
            units = payload.get("units", {})
            if not isinstance(units, dict):
                continue
            for unit, observations in units.items():
                if not preferred_unit(metric, str(unit), policy):
                    continue
                if not isinstance(observations, list):
                    continue
                for obs in observations:
                    if not isinstance(obs, dict):
                        continue
                    value = parse_float(obs.get("val"))
                    period_end = str(obs.get("end") or "").strip()
                    if value is None or not period_end:
                        continue
                    if metric == "capital_expenditures" and value < 0:
                        value = abs(value)
                    form = normalize_form(obs.get("form"))
                    if form not in policy.financial_forms:
                        continue
                    accession = accession_nodash(obs.get("accn"))
                    filed = str(obs.get("filed") or "").strip()
                    fp = str(obs.get("fp") or "").strip().upper()
                    out.append(
                        FactObservation(
                            metric=metric,
                            concept=concept,
                            unit=str(unit),
                            value=value,
                            period_start=str(obs.get("start") or "").strip(),
                            period_end=period_end,
                            fiscal_year=int(obs["fy"]) if str(obs.get("fy") or "").isdigit() else None,
                            fiscal_period=fp,
                            form=form,
                            filed_date=filed,
                            accession_nodash=accession,
                            frame=str(obs.get("frame") or ""),
                            concept_rank=concept_rank,
                        )
                    )
    return out


def sortable_filed_date(raw: object) -> str:
    text = str(raw or "").strip()[:10]
    if not text:
        return ""
    try:
        return datetime.strptime(text, "%Y-%m-%d").date().isoformat()
    except ValueError:
        return ""


def observation_sort_key(obs: FactObservation) -> tuple[int, str, int]:
    # Lower concept_rank is preferred, then latest filing date, then longer duration when available.
    duration = 0
    try:
        if obs.period_start and obs.period_end:
            start = datetime.strptime(obs.period_start[:10], "%Y-%m-%d")
            end = datetime.strptime(obs.period_end[:10], "%Y-%m-%d")
            duration = max(0, (end - start).days)
    except ValueError:
        duration = 0
    return (obs.concept_rank, sortable_filed_date(obs.filed_date), duration)


def build_metric_map(
    companyfacts: dict[str, Any],
    metric: str,
    concepts: list[str],
    policy: SecIngestionPolicy | None = None,
) -> dict[tuple[str, str, str], FactObservation]:
    policy = policy or sec_ingestion_policy({})
    out: dict[tuple[str, str, str], FactObservation] = {}
    for obs in flatten_metric_observations(companyfacts, metric, concepts, policy):
        key = (obs.period_end, obs.fiscal_period, obs.form)
        existing = out.get(key)
        if existing is None:
            out[key] = obs
            continue
        existing_key = observation_sort_key(existing)
        candidate_key = observation_sort_key(obs)
        better_rank = candidate_key[0] < existing_key[0]
        same_rank_better_filing = candidate_key[0] == existing_key[0] and candidate_key[1:] > existing_key[1:]
        if better_rank or same_rank_better_filing:
            out[key] = obs
    return out


def build_debt_map(
    companyfacts: dict[str, Any],
    policy: SecIngestionPolicy | None = None,
) -> dict[tuple[str, str, str], FactObservation]:
    policy = policy or sec_ingestion_policy({})
    direct = build_metric_map(companyfacts, "total_debt", policy.debt_direct_concepts, policy)
    current = build_metric_map(companyfacts, "total_debt", policy.debt_current_concepts, policy)
    noncurrent = build_metric_map(companyfacts, "total_debt", policy.debt_noncurrent_concepts, policy)
    out = dict(direct)
    for key in sorted(set(current) | set(noncurrent)):
        if key in out:
            continue
        cur = current.get(key)
        noncur = noncurrent.get(key)
        if cur is None and noncur is None:
            continue
        template = cur or noncur
        if template is None:
            continue
        out[key] = FactObservation(
            metric="total_debt",
            concept="composite_debt_current_plus_noncurrent",
            unit="USD",
            value=(cur.value if cur is not None else 0.0) + (noncur.value if noncur is not None else 0.0),
            period_start=template.period_start,
            period_end=template.period_end,
            fiscal_year=template.fiscal_year,
            fiscal_period=template.fiscal_period,
            form=template.form,
            filed_date=max(cur.filed_date if cur else "", noncur.filed_date if noncur else ""),
            accession_nodash=template.accession_nodash,
            frame=template.frame,
            concept_rank=policy.composite_debt_concept_rank,
        )
    return out


def build_cash_and_investments_map(
    companyfacts: dict[str, Any],
    policy: SecIngestionPolicy | None = None,
) -> dict[tuple[str, str, str], FactObservation]:
    policy = policy or sec_ingestion_policy({})
    combined = build_metric_map(
        companyfacts,
        "cash_and_investments",
        policy.cash_and_investments_combined_concepts,
        policy,
    )
    cash = build_metric_map(
        companyfacts,
        "cash_and_investments",
        policy.cash_and_investments_cash_concepts,
        policy,
    )
    securities = build_metric_map(
        companyfacts,
        "cash_and_investments",
        policy.cash_and_investments_securities_concepts,
        policy,
    )
    out = dict(combined)
    for key in sorted(set(cash) | set(securities)):
        if key in out:
            continue
        cash_obs = cash.get(key)
        securities_obs = securities.get(key)
        if cash_obs is None and securities_obs is None:
            continue
        template = cash_obs or securities_obs
        if template is None:
            continue
        out[key] = FactObservation(
            metric="cash_and_investments",
            concept="composite_cash_plus_short_term_investments",
            unit=template.unit,
            value=(cash_obs.value if cash_obs is not None else 0.0)
            + (securities_obs.value if securities_obs is not None else 0.0),
            period_start=template.period_start,
            period_end=template.period_end,
            fiscal_year=template.fiscal_year,
            fiscal_period=template.fiscal_period,
            form=template.form,
            filed_date=max(
                cash_obs.filed_date if cash_obs else "", securities_obs.filed_date if securities_obs else ""
            ),
            accession_nodash=template.accession_nodash,
            frame=template.frame,
            concept_rank=policy.composite_debt_concept_rank + 2,
        )
    return out


def build_composite_capex_map(
    companyfacts: dict[str, Any],
    policy: SecIngestionPolicy | None = None,
) -> dict[tuple[str, str, str], FactObservation]:
    policy = policy or sec_ingestion_policy({})
    direct = build_metric_map(
        companyfacts,
        "capital_expenditures",
        policy.metric_concepts.get("capital_expenditures", DEFAULT_METRIC_CONCEPTS["capital_expenditures"]),
        policy,
    )
    component_best: dict[tuple[str, str, str, str], FactObservation] = {}
    for obs in flatten_metric_observations(
        companyfacts, "capital_expenditures", policy.capex_component_concepts, policy
    ):
        key = (obs.period_end, obs.fiscal_period, obs.form, obs.concept)
        existing = component_best.get(key)
        if existing is None or observation_sort_key(obs) > observation_sort_key(existing):
            component_best[key] = obs

    component_groups: dict[tuple[str, str, str], list[FactObservation]] = {}
    for key, obs in component_best.items():
        component_groups.setdefault(key[:3], []).append(obs)

    out = dict(direct)
    for key, observations in component_groups.items():
        if key in out:
            continue
        if not observations:
            continue
        observations.sort(key=lambda obs: (obs.concept_rank, obs.concept))
        template = observations[0]
        out[key] = FactObservation(
            metric="capital_expenditures",
            concept="composite_capex_components",
            unit=template.unit,
            value=sum(max(0.0, obs.value) for obs in observations),
            period_start=template.period_start,
            period_end=template.period_end,
            fiscal_year=template.fiscal_year,
            fiscal_period=template.fiscal_period,
            form=template.form,
            filed_date=max(obs.filed_date for obs in observations),
            accession_nodash=template.accession_nodash,
            frame=template.frame,
            concept_rank=policy.composite_debt_concept_rank + 1,
        )
    return out


def observation_value(observations: dict[str, FactObservation | None], metric: str) -> float | None:
    obs = observations.get(metric)
    return obs.value if obs is not None else None


def has_primary_statement_evidence(observations: dict[str, FactObservation | None]) -> bool:
    return any(observations.get(metric) is not None for metric in PRIMARY_FINANCIAL_OBSERVATION_ORDER)


def has_balance_sheet_evidence(observations: dict[str, FactObservation | None]) -> bool:
    return any(
        observations.get(metric) is not None
        for metric in ("cash_and_investments", "total_assets", "stockholders_equity")
    )


def derived_payload(concept: str, *, inputs: dict[str, float | None], note: str) -> dict[str, Any]:
    return {
        "concept": concept,
        "unit": "USD",
        "derived": True,
        "inputs": {key: value for key, value in inputs.items() if value is not None},
        "note": note,
    }


def selected_financial_observation(observations: dict[str, FactObservation | None]) -> FactObservation | None:
    for metric in PRIMARY_FINANCIAL_OBSERVATION_ORDER:
        obs = observations.get(metric)
        if obs is not None and (obs.accession_nodash or obs.filed_date):
            return obs
    candidates = [obs for obs in observations.values() if obs is not None and obs.accession_nodash]
    if not candidates:
        candidates = [obs for obs in observations.values() if obs is not None and obs.filed_date]
    if not candidates:
        return None
    candidates.sort(key=lambda obs: (obs.concept_rank, obs.filed_date))
    return candidates[0]


def selected_accession(observations: dict[str, FactObservation | None]) -> str:
    obs = selected_financial_observation(observations)
    return obs.accession_nodash if obs is not None and obs.accession_nodash else ""


def selected_filed_date(observations: dict[str, FactObservation | None]) -> str:
    obs = selected_financial_observation(observations)
    return obs.filed_date if obs is not None and obs.filed_date else ""


def build_financial_statement_rows(
    company: Company,
    companyfacts: dict[str, Any],
    policy: SecIngestionPolicy | None = None,
) -> list[dict[str, Any]]:
    policy = policy or sec_ingestion_policy({})
    metric_maps = {
        metric: build_metric_map(companyfacts, metric, concepts, policy)
        for metric, concepts in policy.metric_concepts.items()
    }
    metric_maps["capital_expenditures"] = build_composite_capex_map(companyfacts, policy)
    metric_maps["total_debt"] = build_debt_map(companyfacts, policy)
    metric_maps["cash_and_investments"] = build_cash_and_investments_map(companyfacts, policy)
    keys = sorted(set().union(*(set(mapping.keys()) for mapping in metric_maps.values())))
    rows: list[dict[str, Any]] = []
    for period_end, fiscal_period, form in keys:
        observations = {
            metric: mapping.get((period_end, fiscal_period, form)) for metric, mapping in metric_maps.items()
        }
        if not any(observations.values()):
            continue
        fiscal_years = [
            obs.fiscal_year for obs in observations.values() if obs is not None and obs.fiscal_year is not None
        ]
        capex = observation_value(observations, "capital_expenditures")
        ocf = observation_value(observations, "operating_cash_flow")
        filed_date = selected_filed_date(observations)
        payload = {
            metric: {
                "concept": obs.concept,
                "unit": obs.unit,
                "filed_date": obs.filed_date,
                "accession_nodash": obs.accession_nodash,
            }
            for metric, obs in observations.items()
            if obs is not None
        }
        primary_evidence = has_primary_statement_evidence(observations)
        balance_sheet_evidence = has_balance_sheet_evidence(observations)
        research_and_development = observation_value(observations, "research_and_development")
        if research_and_development is None and primary_evidence:
            research_and_development = 0.0
            payload["research_and_development"] = derived_payload(
                "assumed_zero_not_separately_reported",
                inputs={},
                note="No SEC companyfacts R&D expense fact was present for this statement period.",
            )

        if capex is None and ocf is not None:
            capex = 0.0
            payload["capital_expenditures"] = derived_payload(
                "assumed_zero_not_separately_reported",
                inputs={"operating_cash_flow": ocf},
                note="Operating cash flow was reported but no cash capital-expenditure fact was present.",
            )

        total_debt = observation_value(observations, "total_debt")
        if total_debt is None and balance_sheet_evidence:
            total_debt = 0.0
            payload["total_debt"] = derived_payload(
                "assumed_zero_no_debt_fact",
                inputs={},
                note="Balance-sheet facts were reported but no recognized debt or finance-lease fact was present.",
            )

        gross_profit = observation_value(observations, "gross_profit")
        operating_income = observation_value(observations, "operating_income")
        if operating_income is None:
            revenue = observation_value(observations, "revenue")
            sg_and_a = observation_value(observations, "selling_general_admin")
            cost_of_revenue = observation_value(observations, "cost_of_revenue")
            rd_for_derivation = research_and_development or 0.0
            if gross_profit is not None and sg_and_a is not None:
                operating_income = gross_profit - sg_and_a - rd_for_derivation
                payload["operating_income"] = derived_payload(
                    "derived_gross_profit_less_sga_and_rd",
                    inputs={
                        "gross_profit": gross_profit,
                        "selling_general_admin": sg_and_a,
                        "research_and_development": rd_for_derivation,
                    },
                    note="Direct operating-income fact was absent; derived from reported gross profit and operating-expense components.",
                )
            elif revenue is not None and cost_of_revenue is not None and sg_and_a is not None:
                operating_income = revenue - cost_of_revenue - sg_and_a - rd_for_derivation
                payload["operating_income"] = derived_payload(
                    "derived_revenue_less_cost_of_revenue_sga_and_rd",
                    inputs={
                        "revenue": revenue,
                        "cost_of_revenue": cost_of_revenue,
                        "selling_general_admin": sg_and_a,
                        "research_and_development": rd_for_derivation,
                    },
                    note="Direct operating-income fact was absent; derived from reported revenue and operating-cost components.",
                )

        if (
            gross_profit is None
            and fiscal_period == "FY"
            and company.ticker in policy.annual_gross_profit_from_operating_expenses_tickers
        ):
            revenue = observation_value(observations, "revenue")
            direct_operating_income = observation_value(observations, "operating_income")
            sg_and_a = observation_value(observations, "selling_general_admin")
            direct_rd = observation_value(observations, "research_and_development")
            if (
                revenue is not None
                and revenue > 0.0
                and direct_operating_income is not None
                and sg_and_a is not None
                and direct_rd is not None
            ):
                candidate_gross_profit = direct_operating_income + sg_and_a + direct_rd
                if 0.0 <= candidate_gross_profit <= revenue:
                    gross_profit = candidate_gross_profit
                    payload["gross_profit"] = derived_payload(
                        "derived_operating_income_plus_sga_and_rd",
                        inputs={
                            "operating_income": direct_operating_income,
                            "selling_general_admin": sg_and_a,
                            "research_and_development": direct_rd,
                            "revenue": revenue,
                        },
                        note=(
                            "Reviewed annual issuer statement presents gross contribution before R&D and SG&A "
                            "but no standardized gross-profit fact; derived only from direct SEC statement inputs."
                        ),
                    )
                else:
                    LOGGER.warning(
                        "%s %s %s: rejected gross-profit derivation outside [0, revenue]: %.2f vs %.2f",
                        company.ticker,
                        period_end,
                        fiscal_period,
                        candidate_gross_profit,
                        revenue,
                    )

        rows.append(
            {
                "company_id": company.company_id,
                "period_end": period_end,
                "fiscal_year": max(fiscal_years) if fiscal_years else None,
                "fiscal_period": fiscal_period,
                "form": form,
                "filed_date": filed_date,
                "accession_nodash": selected_accession(observations),
                "revenue": observation_value(observations, "revenue"),
                "gross_profit": gross_profit,
                "operating_income": operating_income,
                "net_income": observation_value(observations, "net_income"),
                "operating_cash_flow": ocf,
                "capital_expenditures": capex,
                "free_cash_flow": (ocf - capex) if ocf is not None and capex is not None else None,
                "research_and_development": research_and_development,
                "interest_expense": observation_value(observations, "interest_expense"),
                "cash_and_investments": observation_value(observations, "cash_and_investments"),
                "total_debt": total_debt,
                "total_assets": observation_value(observations, "total_assets"),
                "stockholders_equity": observation_value(observations, "stockholders_equity"),
                "shares_outstanding": observation_value(observations, "shares_outstanding"),
                "source_id": policy.companyfacts_source_id,
                "payload_json": json.dumps(payload, ensure_ascii=True, sort_keys=True),
            }
        )
    return rows


def upsert_financial_rows(conn: Any, rows: list[dict[str, Any]]) -> int:
    if not rows:
        return 0
    sanitized_rows = [dict(row) for row in rows]
    now = utc_now()
    accessions = sorted(
        {str(row.get("accession_nodash") or "") for row in sanitized_rows if row.get("accession_nodash")}
    )
    existing_accessions: set[str] = set()
    for start in range(0, len(accessions), 800):
        chunk = accessions[start : start + 800]
        if not chunk:
            continue
        placeholders = ",".join("?" for _ in chunk)
        existing_accessions.update(
            str(row["accession_nodash"])
            for row in conn.execute(
                f"SELECT accession_nodash FROM fact_sec_filing WHERE accession_nodash IN ({placeholders})",
                chunk,
            ).fetchall()
        )
    for row in sanitized_rows:
        accession = str(row.get("accession_nodash") or "")
        if accession and accession not in existing_accessions:
            row["accession_nodash"] = None
    fields = [
        "company_id",
        "period_end",
        "fiscal_year",
        "fiscal_period",
        "form",
        "filed_date",
        "accession_nodash",
        "revenue",
        "gross_profit",
        "operating_income",
        "net_income",
        "operating_cash_flow",
        "capital_expenditures",
        "free_cash_flow",
        "research_and_development",
        "interest_expense",
        "cash_and_investments",
        "total_debt",
        "total_assets",
        "stockholders_equity",
        "shares_outstanding",
        "source_id",
        "payload_json",
    ]
    conn.executemany(
        """
        INSERT INTO fact_financial_statement(
            company_id, period_end, fiscal_year, fiscal_period, form, filed_date, accession_nodash,
            revenue, gross_profit, operating_income, net_income, operating_cash_flow,
            capital_expenditures, free_cash_flow, research_and_development, interest_expense,
            cash_and_investments, total_debt, total_assets, stockholders_equity,
            shares_outstanding, source_id, payload_json, created_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(company_id, period_end, fiscal_period, form) DO UPDATE SET
            fiscal_year = excluded.fiscal_year,
            filed_date = excluded.filed_date,
            accession_nodash = excluded.accession_nodash,
            revenue = excluded.revenue,
            gross_profit = excluded.gross_profit,
            operating_income = excluded.operating_income,
            net_income = excluded.net_income,
            operating_cash_flow = excluded.operating_cash_flow,
            capital_expenditures = excluded.capital_expenditures,
            free_cash_flow = excluded.free_cash_flow,
            research_and_development = excluded.research_and_development,
            interest_expense = excluded.interest_expense,
            cash_and_investments = excluded.cash_and_investments,
            total_debt = excluded.total_debt,
            total_assets = excluded.total_assets,
            stockholders_equity = excluded.stockholders_equity,
            shares_outstanding = excluded.shares_outstanding,
            source_id = excluded.source_id,
            payload_json = excluded.payload_json,
            updated_at = excluded.updated_at
        """,
        [tuple(row.get(field) for field in fields) + (now, now) for row in sanitized_rows],
    )
    return len(sanitized_rows)


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDNAMES, extrasaction="ignore", lineterminator="\n")
        writer.writeheader()
        writer.writerows([{field: row.get(field, "") for field in FIELDNAMES} for row in rows])


def validate_url_template(template: str, placeholder: str, name: str) -> str:
    text = str(template or "").strip()
    if placeholder not in text:
        raise ValueError(f"SEC config {name} must include {placeholder}: {text!r}")
    return text


def main() -> None:
    configure_utc_logging()
    args = parse_args()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    policy = sec_ingestion_policy(config)
    base_dir = config_path.parent
    sec_asof = str(args.asof or "").strip() or datetime.utcnow().date().isoformat()
    try:
        datetime.strptime(sec_asof, "%Y-%m-%d")
    except ValueError as exc:
        raise ValueError("--asof must be a valid ISO date (YYYY-MM-DD)") from exc
    db_path = (
        args.db.expanduser().resolve()
        if args.db
        else resolve_path(cfg_get(config, "paths.database_path"), base_dir=base_dir)
    )
    output_csv = (
        args.output_csv.expanduser().resolve()
        if args.output_csv
        else resolve_path(
            cfg_get(
                config,
                "sec_ingestion.output_csv",
                "../output/med_devices_reports/med_device_sec_ingestion_coverage.csv",
            ),
            base_dir=base_dir,
        )
    )
    cache_dir = resolve_path(
        cfg_get(config, "sec_ingestion.cache_dir", "../output/med_devices_cache/sec_ingestion"), base_dir=base_dir
    )
    validation_cache_dir = resolve_path(
        cfg_get(config, "sec_ingestion.validation_cache_dir", "../output/med_devices_cache/universe_validation"),
        base_dir=base_dir,
    )
    submissions_url_template = validate_url_template(
        policy.submissions_url_template,
        "{cik}",
        "sec_ingestion.submissions_url_template",
    )
    companyfacts_url_template = validate_url_template(
        policy.companyfacts_url_template,
        "{cik}",
        "sec_ingestion.companyfacts_url_template",
    )
    submissions_file_url_template = validate_url_template(
        policy.submissions_file_url_template,
        "{file_name}",
        "sec_ingestion.submissions_file_url_template",
    )
    user_agent = str(cfg_get(config, "sec_ingestion.user_agent", "JL, Independent Research, jm.357@hotmail.com"))
    timeout_sec = float(cfg_get(config, "sec_ingestion.timeout_sec", 30.0))
    cache_ttl_hours = float(cfg_get(config, "sec_ingestion.cache_ttl_hours", 24.0))
    sleep_sec = float(cfg_get(config, "sec_ingestion.request_sleep_sec", 0.15))
    max_retries = int(cfg_get(config, "sec_ingestion.max_retries", 3))
    commit_every = max(1, int(cfg_get(config, "sec_ingestion.commit_every_companies", 25)))
    ticker_filter = {str(value).strip().upper() for value in str(args.tickers or "").split(",") if str(value).strip()}
    coverage_rows: list[dict[str, Any]] = []
    submissions_request_count = 0
    companyfacts_request_count = 0
    inline_xbrl_request_count = 0
    total_filings = 0
    total_financial_rows = 0
    total_inline_xbrl_rows = 0
    failed: list[str] = []

    LOGGER.info("SEC ingestion starting: db=%s output=%s", db_path, output_csv)
    with connect(db_path, timeout_sec=float(cfg_get(config, "runtime.sqlite_timeout_sec", 30.0))) as conn:
        init_db(conn)
        ensure_source_registry(conn, config, base_dir, policy)
        companies = load_companies(conn, ticker_filter=ticker_filter, max_tickers=int(args.max_tickers))
        if not companies:
            raise ValueError("No active companies with CIKs selected for SEC ingestion")
        run_id = start_run(conn, run_type="sync_med_device_sec_fundamentals", input_path=config_path)
        submissions_ingestion_id = start_ingestion_run(conn, policy.submissions_source_id)
        companyfacts_ingestion_id = start_ingestion_run(conn, policy.companyfacts_source_id)
        inline_xbrl_ingestion_id = (
            start_ingestion_run(conn, policy.inline_xbrl_source_id) if policy.inline_xbrl_fallback_enabled else None
        )
        session = requests.Session()
        try:
            for idx, company in enumerate(companies, start=1):
                reasons: list[str] = []
                filing_count = 0
                financial_count = 0
                inline_xbrl_count = 0
                submissions_status = "success"
                companyfacts_status = "success"
                first_period = ""
                latest_period = ""
                latest_financial_filing_date = ""
                latest_parsed_filed_date = ""
                submissions_payload_source = "not_requested"
                companyfacts_payload_source = "not_requested"
                filing_rows: list[dict[str, Any]] = []
                try:
                    submissions_url = submissions_url_template.format(cik=company.cik)
                    (
                        submissions,
                        submissions_text,
                        submissions_payload_source,
                        submissions_status_code,
                    ) = fetch_json(
                        session,
                        submissions_url,
                        primary_cache_path=cache_dir / "sec_submissions" / f"CIK{company.cik}.json",
                        fallback_cache_path=validation_cache_dir / "sec_submissions" / f"CIK{company.cik}.json",
                        refresh_cache=(args.refresh_cache or args.refresh_submissions),
                        cache_ttl_hours=cache_ttl_hours,
                        timeout_sec=timeout_sec,
                        max_retries=max_retries,
                        sleep_sec=sleep_sec,
                        user_agent=user_agent,
                    )
                    if submissions_payload_source == "fetched":
                        submissions_request_count += 1
                        time.sleep(max(0.0, sleep_sec))
                    store_raw_response(
                        conn,
                        source_id=policy.submissions_source_id,
                        endpoint=submissions_url,
                        payload_text=submissions_text,
                        response_status=submissions_status_code,
                        ingestion_run_id=submissions_ingestion_id,
                        asof_date=sec_asof,
                        payload_source=submissions_payload_source,
                        response_kind="root_submissions",
                    )
                    filing_rows = parse_recent_filings(company, submissions, policy.forms, policy)
                    if policy.fetch_paginated_submissions:
                        for file_name in submission_file_names(submissions):
                            file_url = submissions_file_url_template.format(file_name=file_name)
                            try:
                                extra_submissions, extra_text, extra_source, extra_status_code = fetch_json(
                                    session,
                                    file_url,
                                    primary_cache_path=cache_dir / "sec_submissions" / file_name,
                                    fallback_cache_path=None,
                                    refresh_cache=args.refresh_cache,
                                    cache_ttl_hours=cache_ttl_hours,
                                    timeout_sec=timeout_sec,
                                    max_retries=max_retries,
                                    sleep_sec=sleep_sec,
                                    user_agent=user_agent,
                                )
                                if extra_source == "fetched":
                                    submissions_request_count += 1
                                    time.sleep(max(0.0, sleep_sec))
                                store_raw_response(
                                    conn,
                                    source_id=policy.submissions_source_id,
                                    endpoint=file_url,
                                    payload_text=extra_text,
                                    response_status=extra_status_code,
                                    ingestion_run_id=submissions_ingestion_id,
                                    asof_date=sec_asof,
                                    payload_source=extra_source,
                                    response_kind="submissions_archive",
                                )
                                filing_rows.extend(
                                    parse_recent_filings(company, extra_submissions, policy.forms, policy)
                                )
                            except Exception as exc:
                                LOGGER.warning(
                                    "Skipping paginated SEC submissions file for %s file=%s: %s",
                                    company.ticker,
                                    file_name,
                                    exc,
                                )
                    filing_count = upsert_filings(conn, filing_rows)
                    total_filings += filing_count
                except Exception as exc:
                    submissions_status = "failed"
                    reasons.append(f"submissions:{type(exc).__name__}:{exc}")

                try:
                    companyfacts_url = companyfacts_url_template.format(cik=company.cik)
                    (
                        companyfacts,
                        companyfacts_text,
                        companyfacts_payload_source,
                        companyfacts_status_code,
                    ) = fetch_json(
                        session,
                        companyfacts_url,
                        primary_cache_path=cache_dir / "sec_companyfacts" / f"CIK{company.cik}.json",
                        fallback_cache_path=validation_cache_dir / "sec_companyfacts" / f"CIK{company.cik}.json",
                        refresh_cache=args.refresh_cache,
                        cache_ttl_hours=cache_ttl_hours,
                        timeout_sec=timeout_sec,
                        max_retries=max_retries,
                        sleep_sec=sleep_sec,
                        user_agent=user_agent,
                    )
                    if companyfacts_payload_source == "fetched":
                        companyfacts_request_count += 1
                        time.sleep(max(0.0, sleep_sec))
                    store_raw_response(
                        conn,
                        source_id=policy.companyfacts_source_id,
                        endpoint=companyfacts_url,
                        payload_text=companyfacts_text,
                        response_status=companyfacts_status_code,
                        ingestion_run_id=companyfacts_ingestion_id,
                        asof_date=sec_asof,
                        payload_source=companyfacts_payload_source,
                        response_kind="companyfacts",
                    )
                    financial_rows = build_financial_statement_rows(company, companyfacts, policy)
                    financial_count = upsert_financial_rows(conn, financial_rows)
                    financial_filing_dates = [
                        normalized_inline_date(row.get("filing_date"))
                        for row in filing_rows
                        if normalize_form(row.get("form")) in policy.financial_forms
                    ]
                    latest_financial_filing_date = max(
                        (value for value in financial_filing_dates if value),
                        default="",
                    )

                    if policy.inline_xbrl_fallback_enabled:
                        for filing in unrepresented_financial_filings(filing_rows, financial_rows, policy):
                            filing_url = str(filing.get("archive_url") or "").strip()
                            document_name = Path(str(filing.get("primary_document") or "")).name
                            accession = accession_nodash(filing.get("accession_nodash"))
                            if not filing_url or not document_name or not accession:
                                raise ValueError(f"Incomplete SEC filing fallback metadata for {company.ticker}.")
                            document_text, document_source, document_status = fetch_text(
                                session,
                                filing_url,
                                cache_path=(
                                    cache_dir / "sec_inline_xbrl" / f"CIK{company.cik}" / accession / document_name
                                ),
                                refresh_cache=args.refresh_cache,
                                cache_ttl_hours=cache_ttl_hours,
                                timeout_sec=timeout_sec,
                                max_retries=max_retries,
                                sleep_sec=sleep_sec,
                                user_agent=user_agent,
                            )
                            if document_source == "fetched":
                                inline_xbrl_request_count += 1
                                time.sleep(max(0.0, sleep_sec))
                            if inline_xbrl_ingestion_id is None:
                                raise RuntimeError("Inline XBRL ingestion run was not initialized.")
                            store_raw_response(
                                conn,
                                source_id=policy.inline_xbrl_source_id,
                                endpoint=filing_url,
                                payload_text=document_text,
                                response_status=document_status,
                                ingestion_run_id=inline_xbrl_ingestion_id,
                                asof_date=sec_asof,
                                payload_source=document_source,
                                response_kind="inline_xbrl_document",
                            )
                            fallback_rows = build_inline_fallback_rows(
                                company,
                                filing,
                                document_text,
                                policy,
                            )
                            inline_xbrl_count += upsert_financial_rows(conn, fallback_rows)
                            financial_rows.extend(fallback_rows)

                    financial_count += inline_xbrl_count
                    total_inline_xbrl_rows += inline_xbrl_count
                    total_financial_rows += financial_count
                    if financial_rows:
                        periods = sorted(
                            {str(row.get("period_end") or "") for row in financial_rows if row.get("period_end")}
                        )
                        first_period = periods[0] if periods else ""
                        latest_period = periods[-1] if periods else ""
                        latest_parsed_filed_date = max(
                            (str(row.get("filed_date") or "") for row in financial_rows),
                            default="",
                        )
                        if latest_financial_filing_date > latest_parsed_filed_date:
                            raise ValueError(
                                f"Latest periodic filing remains unparsed for {company.ticker}: "
                                f"filing={latest_financial_filing_date} parsed={latest_parsed_filed_date}."
                            )
                    else:
                        companyfacts_status = "failed"
                        reasons.append("companyfacts:no_financial_rows")
                except Exception as exc:
                    companyfacts_status = "failed"
                    reasons.append(f"companyfacts:{type(exc).__name__}:{exc}")

                if reasons:
                    failed.append(company.ticker)
                coverage_rows.append(
                    {
                        "ticker": company.ticker,
                        "company_id": company.company_id,
                        "cik": company.cik,
                        "company_name": company.company_name,
                        "submissions_status": submissions_status,
                        "companyfacts_status": companyfacts_status,
                        "submissions_payload_source": submissions_payload_source,
                        "companyfacts_payload_source": companyfacts_payload_source,
                        "sec_asof_date": sec_asof,
                        "filing_rows": filing_count,
                        "financial_statement_rows": financial_count,
                        "inline_xbrl_fallback_rows": inline_xbrl_count,
                        "first_period_end": first_period,
                        "latest_period_end": latest_period,
                        "latest_financial_filing_date": latest_financial_filing_date,
                        "latest_parsed_filed_date": latest_parsed_filed_date,
                        "review_reason": ";".join(reasons),
                    }
                )
                LOGGER.info(
                    "[%d/%d] %s filings=%d financial_rows=%d inline_xbrl_rows=%d status=%s/%s",
                    idx,
                    len(companies),
                    company.ticker,
                    filing_count,
                    financial_count,
                    inline_xbrl_count,
                    submissions_status,
                    companyfacts_status,
                )
                if idx % commit_every == 0:
                    conn.commit()
                    LOGGER.info("Committed SEC ingestion progress: %d/%d", idx, len(companies))

            status = "partial" if failed else "success"
            message = (
                f"companies={len(companies)} filings={total_filings} "
                f"financial_rows={total_financial_rows} inline_xbrl_rows={total_inline_xbrl_rows} "
                f"output={output_csv}"
            )
            if failed:
                message += " failed_tickers=" + ",".join(failed)
            finish_ingestion_run(
                conn,
                ingestion_run_id=submissions_ingestion_id,
                status=status,
                request_count=submissions_request_count,
                row_count=total_filings,
                message=message,
            )
            finish_ingestion_run(
                conn,
                ingestion_run_id=companyfacts_ingestion_id,
                status=status,
                request_count=companyfacts_request_count,
                row_count=total_financial_rows,
                message=message,
            )
            if inline_xbrl_ingestion_id is not None:
                finish_ingestion_run(
                    conn,
                    ingestion_run_id=inline_xbrl_ingestion_id,
                    status=status,
                    request_count=inline_xbrl_request_count,
                    row_count=total_inline_xbrl_rows,
                    message=message,
                )
            finish_run(conn, run_id=run_id, status=status, row_count=total_financial_rows, message=message)
        except BaseException as exc:
            message = f"{type(exc).__name__}: {exc}"
            finish_ingestion_run(
                conn,
                ingestion_run_id=submissions_ingestion_id,
                status="failed",
                request_count=submissions_request_count,
                row_count=total_filings,
                message=message,
            )
            finish_ingestion_run(
                conn,
                ingestion_run_id=companyfacts_ingestion_id,
                status="failed",
                request_count=companyfacts_request_count,
                row_count=total_financial_rows,
                message=message,
            )
            if inline_xbrl_ingestion_id is not None:
                finish_ingestion_run(
                    conn,
                    ingestion_run_id=inline_xbrl_ingestion_id,
                    status="failed",
                    request_count=inline_xbrl_request_count,
                    row_count=total_inline_xbrl_rows,
                    message=message,
                )
            finish_run(conn, run_id=run_id, status="failed", row_count=total_financial_rows, message=message)
            raise

    write_csv(output_csv, coverage_rows)
    LOGGER.info(
        "SEC ingestion complete: companies=%d filings=%d financial_rows=%d inline_xbrl_rows=%d output=%s failed=%d",
        len(coverage_rows),
        total_filings,
        total_financial_rows,
        total_inline_xbrl_rows,
        output_csv,
        len(failed),
    )
    if failed and not args.allow_partial:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
