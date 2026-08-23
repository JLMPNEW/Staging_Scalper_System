#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import hashlib
import logging
import math
import sqlite3
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from biotech_index.core.config import cfg_get, load_yaml, resolve_path
from biotech_index.core.db import connect, finish_run, init_db, quote_identifier, start_run, utc_now
from biotech_index.core.http_cache import CachedHttpClient, HostThrottle
from biotech_index.core.logging_utils import configure_utc_logging
from biotech_index.core.security_identity import SecurityIdentityRule, load_security_identity_rules
from biotech_index.core.pipeline_guards import (
    normalize_ticker,
    read_final_scoring_tickers,
    subset_mode_enabled,
    subset_output_path,
    validate_full_universe_coverage,
    validate_nonempty_selection,
    validate_requested_tickers,
)


LOGGER = logging.getLogger("sync_sec_companyfacts_history")
DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
SQLITE_PARAM_CHUNK_SIZE = 800
COMPANYFACTS_NORMALIZER_VERSION = "2026-08-21.1"


def chunked(values: list[Any] | tuple[Any, ...], size: int = SQLITE_PARAM_CHUNK_SIZE) -> list[list[Any]]:
    step = max(1, int(size))
    return [list(values[start : start + step]) for start in range(0, len(values), step)]


CONCEPT_GROUPS: dict[str, list[str]] = {
    "cash": [
        "CashAndCashEquivalentsAtCarryingValue",
        "Cash",
        "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents",
    ],
    "short_term_investments": [
        "ShortTermInvestments",
        "MarketableSecuritiesCurrent",
        "AvailableForSaleSecuritiesCurrent",
        "AvailableForSaleSecuritiesDebtSecuritiesCurrent",
        "ShortTermInvestmentsAvailableForSaleSecurities",
    ],
    "long_term_investments": [
        "LongTermInvestments",
        "MarketableSecuritiesNoncurrent",
        "AvailableForSaleSecuritiesNoncurrent",
        "AvailableForSaleSecuritiesDebtSecuritiesNoncurrent",
    ],
    "marketable_investments_reported_total": [
        "MarketableSecurities",
        "AvailableForSaleSecurities",
        "AvailableForSaleSecuritiesDebtSecurities",
        "AvailableForSaleSecuritiesDebtMaturitiesSingleMaturityDate",
    ],
    "restricted_cash": [
        "RestrictedCashAndCashEquivalentsCurrent",
        "RestrictedCashCurrent",
    ],
    "current_assets": ["AssetsCurrent"],
    "current_liabilities": ["LiabilitiesCurrent"],
    "total_assets": ["Assets"],
    "total_liabilities": ["Liabilities"],
    "short_term_debt": [
        "ShortTermBorrowings",
        "DebtCurrent",
        "LongTermDebtCurrent",
        "ConvertibleNotesPayableCurrent",
    ],
    "long_term_debt": [
        "LongTermDebtNoncurrent",
        "LongTermDebtAndFinanceLeaseObligationsNoncurrent",
        "ConvertibleNotesPayableNoncurrent",
    ],
    "total_debt_reported": [
        "LongTermDebt",
        "DebtAndFinanceLeaseObligations",
    ],
    "revenue": [
        "Revenues",
        "RevenueFromContractWithCustomerExcludingAssessedTax",
        "SalesRevenueNet",
    ],
    "product_revenue": [
        "ProductRevenue",
        "ProductRevenueNet",
        "ProductSales",
        "ProductSalesRevenue",
        "ProductSalesRevenueNet",
        "SalesRevenueGoodsNet",
    ],
    "collaboration_revenue": [
        "CollaborationRevenue",
        "RevenueFromCollaborativeArrangements",
        "RevenueFromCollaborativeArrangement",
        "ContractRevenue",
    ],
    "license_revenue": [
        "LicenseRevenue",
        "LicensingRevenue",
        "RoyaltyRevenue",
    ],
    "service_revenue": [
        "ServiceRevenue",
        "ServiceRevenueNet",
    ],
    "cost_of_revenue": [
        "CostOfRevenue",
        "CostOfGoodsAndServicesSold",
        "CostOfGoodsSold",
        "CostOfGoodsAndServiceExcludingDepreciationDepletionAndAmortization",
        "CostOfRevenueExcludingDepreciationAndAmortization",
    ],
    "gross_profit": ["GrossProfit"],
    "rd_expense": [
        "ResearchAndDevelopmentExpense",
        "ResearchAndDevelopmentExpenseExcludingAcquiredInProcessCost",
    ],
    "sgna_expense": [
        "SellingGeneralAndAdministrativeExpense",
        "GeneralAndAdministrativeExpense",
    ],
    "operating_income": ["OperatingIncomeLoss"],
    "net_income": ["NetIncomeLoss", "ProfitLoss"],
    "interest_expense": ["InterestExpenseNonOperating", "InterestExpense"],
    "income_tax_expense": ["IncomeTaxExpenseBenefit"],
    "depreciation_amortization": [
        "DepreciationDepletionAndAmortization",
        "DepreciationAndAmortization",
        "DepreciationDepletionAndAmortizationExpense",
    ],
    "eps_basic": ["EarningsPerShareBasic"],
    "eps_diluted": ["EarningsPerShareDiluted"],
    "weighted_average_shares_basic": ["WeightedAverageNumberOfSharesOutstandingBasic"],
    "weighted_average_shares_diluted": ["WeightedAverageNumberOfDilutedSharesOutstanding"],
    "operating_cash_flow": [
        "NetCashProvidedByUsedInOperatingActivities",
        "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations",
    ],
    "investing_cash_flow": ["NetCashProvidedByUsedInInvestingActivities"],
    "financing_cash_flow": ["NetCashProvidedByUsedInFinancingActivities"],
    "capital_expenditures": ["PaymentsToAcquirePropertyPlantAndEquipment"],
    "shares_outstanding": ["EntityCommonStockSharesOutstanding", "CommonStockSharesOutstanding"],
}

CONCEPT_TO_GROUP = {concept: group for group, concepts in CONCEPT_GROUPS.items() for concept in concepts}
OBSERVATION_CONCEPTS = set(CONCEPT_TO_GROUP)
ALLOWED_FORMS = {"10-Q", "10-Q/A", "10-K", "10-K/A", "20-F", "20-F/A", "40-F", "40-F/A", "6-K", "6-K/A"}


@dataclass(frozen=True)
class Company:
    company_id: int
    ticker: str
    cik: str
    company_name: str


@dataclass(frozen=True)
class CompanyFactsFetchResult:
    company: Company
    latest_source_filing_date: str
    payload_hash: str = ""
    observations: tuple[dict[str, Any], ...] = ()
    normalized: tuple[dict[str, Any], ...] = ()
    error: str = ""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Sync 3-year SEC companyfacts history for biotech scoring companies.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--asof", type=str, default="", help="History as-of date in YYYY-MM-DD. Defaults to UTC today.")
    parser.add_argument("--max-companies", type=int, default=0, help="Smoke-test limit. 0 means all.")
    parser.add_argument("--tickers", type=str, default="", help="Optional comma-separated ticker subset.")
    parser.add_argument("--universe-csv", type=Path, default=None, help="Override final scoring universe CSV. Used for calibration-only SEC backfills.")
    parser.add_argument("--output-csv", type=Path, default=None, help="Override companyfacts output CSV.")
    parser.add_argument("--sign-audit-csv", type=Path, default=None, help="Override companyfacts sign-convention audit CSV.")
    parser.add_argument("--lookback-years", type=int, default=0, help="Override sec_companyfacts_history.lookback_years.")
    parser.add_argument(
        "--include-delisted-calibration",
        action="store_true",
        help="Allow inactive companies with universe_status=delisted_calibration when the supplied universe CSV names them.",
    )
    parser.add_argument(
        "--include-historical-ciks",
        action="store_true",
        help="Atomically merge approved predecessor/successor CIK payloads from active_biotech_history.registry_csv.",
    )
    parser.add_argument("--full-refresh", action="store_true", help="Force refresh for all eligible companies regardless of sync state.")
    parser.add_argument("--audit-only", action="store_true", help="Write the sign-convention audit from existing DB rows without fetching SEC data.")
    parser.add_argument("--allow-partial", action="store_true", help="Return success even if one or more companyfacts fetches fail.")
    return parser.parse_args()


def configure_logging() -> None:
    configure_utc_logging()


def parse_date(raw: object) -> date | None:
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        return datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError:
        return None


def to_float(raw: object) -> float | None:
    try:
        value = float(str(raw).strip())
    except (TypeError, ValueError):
        return None
    if math.isnan(value) or math.isinf(value):
        return None
    return value


def normalize_cik(cik: object) -> str:
    text = "".join(ch for ch in str(cik or "") if ch.isdigit())
    return text.zfill(10) if text else ""


def as_bool(raw: object) -> bool:
    return str(raw or "").strip().lower() in {"1", "true", "yes", "y"}


def read_scoring_tickers(path: Path) -> set[str]:
    return read_final_scoring_tickers(path)


def load_companies(
    conn: sqlite3.Connection,
    *,
    scoring_tickers: set[str],
    ticker_filter: set[str],
    max_companies: int,
    include_delisted_calibration: bool = False,
) -> list[Company]:
    status_clause = "is_active = 1"
    if include_delisted_calibration:
        status_clause = "(is_active = 1 OR universe_status = 'delisted_calibration')"
    rows = conn.execute(
        f"""
        SELECT company_id, ticker, cik, company_name
        FROM companies
        WHERE {status_clause}
        ORDER BY ticker
        """
    ).fetchall()
    companies: list[Company] = []
    for row in rows:
        ticker = normalize_ticker(row["ticker"])
        if scoring_tickers and ticker not in scoring_tickers:
            continue
        if ticker_filter and ticker not in ticker_filter:
            continue
        cik = normalize_cik(row["cik"])
        companies.append(
            Company(
                company_id=int(row["company_id"]),
                ticker=ticker,
                cik=cik,
                company_name=str(row["company_name"] or ""),
            )
        )
        if max_companies > 0 and len(companies) >= max_companies:
            break
    return companies


def expand_company_cik_history(
    companies: list[Company],
    rules: dict[str, SecurityIdentityRule],
) -> list[Company]:
    expanded: list[Company] = []
    for company in companies:
        rule = rules.get(company.ticker)
        historical_ciks = rule.historical_ciks if rule is not None else ()
        for historical_cik in historical_ciks:
            if historical_cik and historical_cik != company.cik:
                expanded.append(
                    Company(
                        company_id=company.company_id,
                        ticker=company.ticker,
                        cik=historical_cik,
                        company_name=company.company_name,
                    )
                )
        expanded.append(company)
    return expanded


def merge_companyfacts_results(
    results: list[CompanyFactsFetchResult],
    *,
    primary_companies: dict[int, Company],
) -> list[CompanyFactsFetchResult]:
    """Merge reviewed issuer-CIK payloads once so one lineage cannot overwrite another."""
    grouped: dict[int, list[CompanyFactsFetchResult]] = {}
    for result in results:
        grouped.setdefault(result.company.company_id, []).append(result)
    merged: list[CompanyFactsFetchResult] = []
    for company_id, group in sorted(grouped.items()):
        primary = primary_companies[company_id]
        successful = [result for result in group if not result.error]
        if not successful:
            errors = "; ".join(sorted({result.error for result in group if result.error}))
            merged.append(
                CompanyFactsFetchResult(
                    company=primary,
                    latest_source_filing_date=max(
                        (result.latest_source_filing_date for result in group), default=""
                    ),
                    error=errors or "all_cik_fetches_failed",
                )
            )
            continue
        failed_ciks = [result.company.cik for result in group if result.error]
        if failed_ciks:
            LOGGER.warning(
                "SEC companyfacts partial issuer-lineage fetch for %s failed_ciks=%s",
                primary.ticker,
                ",".join(sorted(failed_ciks)),
            )
        observations: list[dict[str, Any]] = []
        seen_observations: set[tuple[Any, ...]] = set()
        for result in successful:
            for observation in result.observations:
                key = (
                    observation.get("cik"),
                    observation.get("taxonomy"),
                    observation.get("concept"),
                    observation.get("unit"),
                    observation.get("value"),
                    observation.get("period_start"),
                    observation.get("period_end"),
                    observation.get("form"),
                    observation.get("filed_date"),
                    observation.get("accession_nodash"),
                    observation.get("frame"),
                )
                if key in seen_observations:
                    continue
                seen_observations.add(key)
                observations.append(dict(observation))
        payload_material = "|".join(
            sorted(f"{result.company.cik}:{result.payload_hash}" for result in successful)
        )
        merged.append(
            CompanyFactsFetchResult(
                company=primary,
                latest_source_filing_date=max(
                    (result.latest_source_filing_date for result in successful), default=""
                ),
                payload_hash=hashlib.sha256(payload_material.encode("utf-8")).hexdigest(),
                observations=tuple(observations),
                normalized=tuple(normalize_rows(observations, company_id=company_id)),
            )
        )
    return merged


def fiscal_sort_key(obs: dict[str, Any]) -> tuple[str, str, str]:
    return (
        str(obs.get("filed_date") or ""),
        str(obs.get("form") or ""),
        str(obs.get("accession_nodash") or ""),
    )


def parse_observations(payload: dict[str, Any], *, company: Company, cutoff: date, asof: date | None = None) -> list[dict[str, Any]]:
    facts = payload.get("facts", {}) if isinstance(payload, dict) else {}
    observations: list[dict[str, Any]] = []
    if not isinstance(facts, dict):
        return observations
    for taxonomy, concepts in facts.items():
        if not isinstance(concepts, dict):
            continue
        for concept, detail in concepts.items():
            if concept not in OBSERVATION_CONCEPTS or not isinstance(detail, dict):
                continue
            label = str(detail.get("label") or "")
            units = detail.get("units", {})
            if not isinstance(units, dict):
                continue
            for unit, entries in units.items():
                if not isinstance(entries, list):
                    continue
                for entry in entries:
                    if not isinstance(entry, dict):
                        continue
                    end_date = parse_date(entry.get("end"))
                    if end_date is None or end_date < cutoff:
                        continue
                    # Point-in-time guard: a historical --asof run must not ingest
                    # facts whose period ends or whose filing occurred after asof.
                    # asof=None (legacy callers) skips the upper bound (live semantics).
                    if asof is not None and end_date > asof:
                        continue
                    filed_date = parse_date(entry.get("filed"))
                    if asof is not None and filed_date is not None and filed_date > asof:
                        continue
                    form = str(entry.get("form") or "").upper()
                    if not form or form not in ALLOWED_FORMS:
                        continue
                    value = to_float(entry.get("val"))
                    if value is None:
                        continue
                    start_date = parse_date(entry.get("start"))
                    duration_days = (end_date - start_date).days if start_date is not None else None
                    fy_text = str(entry.get("fy") or "")
                    observations.append(
                        {
                            "company_id": company.company_id,
                            "cik": company.cik,
                            "taxonomy": str(taxonomy),
                            "concept": str(concept),
                            "label": label,
                            "unit": str(unit),
                            "value": value,
                            "period_start": str(entry.get("start") or ""),
                            "period_end": end_date.isoformat(),
                            "duration_days": duration_days,
                            "fiscal_year": int(fy_text) if fy_text.isdigit() else None,
                            "fiscal_period": str(entry.get("fp") or ""),
                            "form": form,
                            "filed_date": str(entry.get("filed") or ""),
                            "accession_nodash": str(entry.get("accn") or "").replace("-", ""),
                            "frame": str(entry.get("frame") or ""),
                            "source": "sec_companyfacts",
                            "confidence": 1.0,
                        }
                    )
    return observations


def observation_duration_days(obs: dict[str, Any]) -> int | None:
    duration = obs.get("duration_days")
    if duration is not None:
        return int(duration)
    start = parse_date(obs.get("period_start"))
    end = parse_date(obs.get("period_end"))
    if start is None or end is None:
        return None
    return (end - start).days


def prefer_observation(current: dict[str, Any] | None, candidate: dict[str, Any]) -> dict[str, Any]:
    if current is None:
        return candidate
    candidate_unit = str(candidate.get("unit") or "").upper()
    current_unit = str(current.get("unit") or "").upper()
    if current_unit == "USD" and candidate_unit != "USD":
        return current
    if current_unit == "SHARES" and candidate_unit != "SHARES":
        return current
    if candidate_unit == "USD" and current_unit != "USD":
        return candidate
    if candidate_unit == "SHARES" and current_unit != "SHARES":
        return candidate
    # Flow (duration) facts: a 10-Q tags both the discrete 3-month value and the
    # YTD value with the same period_end/fp/form, so pick by duration -- shortest
    # for Q1-Q4 rows (keeps 3-month over YTD; semiannual IFRS filers with only a
    # ~182-day fact are unaffected) and longest for FY rows (keeps the annual
    # value over a Q4-only fact tagged FY). Instant balance-sheet facts have no
    # period_start, so duration is None and this preference is skipped.
    candidate_duration = observation_duration_days(candidate)
    current_duration = observation_duration_days(current)
    if candidate_duration is not None and current_duration is not None and candidate_duration != current_duration:
        if str(candidate.get("fiscal_period") or "").upper() == "FY":
            return candidate if candidate_duration > current_duration else current
        return candidate if candidate_duration < current_duration else current
    # Prefer the EARLIEST-filed observation so stored values stay point-in-time
    # (first-reported) instead of absorbing restatements from later filings that
    # re-report the same period as a comparative.
    return current if fiscal_sort_key(current) <= fiscal_sort_key(candidate) else candidate


def normalize_rows(observations: list[dict[str, Any]], *, company_id: int) -> list[dict[str, Any]]:
    best: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    for obs in observations:
        group = CONCEPT_TO_GROUP.get(str(obs["concept"]))
        if not group:
            continue
        key = (
            str(obs["period_end"]),
            str(obs.get("fiscal_period") or ""),
            str(obs.get("form") or ""),
            group,
        )
        best[key] = prefer_observation(best.get(key), obs)

    period_rows: dict[tuple[str, str, str], dict[str, Any]] = {}
    for (period_end, fiscal_period, form, group), obs in best.items():
        key = (period_end, fiscal_period, form)
        row = period_rows.setdefault(
            key,
            {
                "company_id": company_id,
                "period_end": period_end,
                "fiscal_year": obs.get("fiscal_year"),
                "fiscal_period": fiscal_period,
                "form": form,
                "filed_date": obs.get("filed_date"),
                "accession_nodash": obs.get("accession_nodash"),
                "_source_concepts": {},
                "_source_duration_days": {},
                "_source_period_starts": {},
            },
        )
        # Known limitation: concepts for one (period_end, fp, form) row can come
        # from different accessions (e.g. a concept first tagged only in a later
        # comparative filing). The row is stamped with the MAX filed_date /
        # accession across the observations actually used, so filed_date remains
        # a conservative "all inputs available by" bound; with the earliest-filed
        # preference above this normally reflects first-reported data.
        if fiscal_sort_key(obs) >= (str(row.get("filed_date") or ""), str(row.get("form") or ""), str(row.get("accession_nodash") or "")):
            row["filed_date"] = obs.get("filed_date")
            row["accession_nodash"] = obs.get("accession_nodash")
        row[group] = obs.get("value")
        row["_source_concepts"][group] = obs.get("concept")
        row["_source_duration_days"][group] = observation_duration_days(obs)
        row["_source_period_starts"][group] = obs.get("period_start")

    out: list[dict[str, Any]] = []
    for row in period_rows.values():
        source_concepts = row.pop("_source_concepts", {})
        source_duration_days = row.pop("_source_duration_days", {})
        source_period_starts = row.pop("_source_period_starts", {})
        cash = row.get("cash")
        short_term_investments = row.get("short_term_investments")
        long_term_investments = row.get("long_term_investments")
        reported_investments_total = row.get("marketable_investments_reported_total")
        if short_term_investments is not None and long_term_investments is not None:
            marketable_investments_total = float(short_term_investments) + float(long_term_investments)
        elif short_term_investments is not None:
            marketable_investments_total = float(short_term_investments)
        elif long_term_investments is not None:
            marketable_investments_total = float(long_term_investments)
        elif reported_investments_total is not None:
            marketable_investments_total = float(reported_investments_total)
        else:
            marketable_investments_total = None
        row["cash_and_equivalents"] = cash
        row["marketable_investments_total"] = marketable_investments_total
        row["cash_and_investments"] = (
            float(cash) + float(marketable_investments_total or 0.0) if cash is not None else None
        )
        if row.get("total_debt_reported") is not None:
            row["total_debt"] = row.get("total_debt_reported")
        else:
            debt_parts = [row.get("short_term_debt"), row.get("long_term_debt")]
            row["total_debt"] = sum(float(x or 0.0) for x in debt_parts if x is not None) if any(x is not None for x in debt_parts) else None
        if row.get("current_assets") is not None and row.get("current_liabilities") is not None:
            row["working_capital"] = float(row["current_assets"]) - float(row["current_liabilities"])

        proxies: list[str] = []
        revenue_components = [row.get("product_revenue"), row.get("collaboration_revenue"), row.get("license_revenue"), row.get("service_revenue")]
        if row.get("revenue") is None and any(value is not None for value in revenue_components):
            row["revenue"] = sum(float(value or 0.0) for value in revenue_components if value is not None)
            proxies.append("revenue_component_sum")
        if row.get("gross_profit") is None and row.get("revenue") is not None and row.get("cost_of_revenue") is not None:
            # Preserve the SEC fact sign convention; expense concepts may be negative in source filings.
            row["gross_profit"] = float(row["revenue"]) - float(row["cost_of_revenue"])
            proxies.append("gross_profit_revenue_minus_cost_of_revenue_raw_sign")
        if row.get("operating_income") is None and row.get("revenue") is not None:
            operating_costs = [row.get("cost_of_revenue"), row.get("rd_expense"), row.get("sgna_expense")]
            if all(value is not None for value in operating_costs):
                # Preserve the SEC fact sign convention; expense concepts may be negative in source filings.
                row["operating_income"] = float(row["revenue"]) - sum(float(value or 0.0) for value in operating_costs)
                proxies.append("operating_income_revenue_minus_cost_rd_sgna_raw_sign")
        if row.get("eps_diluted") is None and row.get("net_income") is not None and row.get("weighted_average_shares_diluted"):
            shares = float(row["weighted_average_shares_diluted"])
            if shares > 0:
                row["eps_diluted"] = float(row["net_income"]) / shares
                proxies.append("eps_diluted_net_income_over_weighted_diluted_shares")
        if row.get("shares_outstanding") is None:
            if row.get("weighted_average_shares_diluted") is not None:
                row["shares_outstanding"] = row.get("weighted_average_shares_diluted")
                proxies.append("weighted_diluted_shares_for_shares_outstanding")
            elif row.get("weighted_average_shares_basic") is not None:
                row["shares_outstanding"] = row.get("weighted_average_shares_basic")
                proxies.append("weighted_basic_shares_for_shares_outstanding")
        if row.get("operating_cash_flow") is not None and row.get("capital_expenditures") is not None:
            # Preserve the SEC fact sign convention; capital expenditures may already be negative.
            row["free_cash_flow"] = float(row["operating_cash_flow"]) - float(row["capital_expenditures"])

        missing: list[str] = []
        for field in ("cash_and_investments", "revenue", "rd_expense", "operating_cash_flow", "current_assets", "current_liabilities"):
            if row.get(field) is None:
                missing.append(field)
        if cash is not None and marketable_investments_total is None:
            proxies.append("cash_only_for_cash_and_investments")
        elif (
            cash is not None
            and short_term_investments is not None
            and long_term_investments is None
            and reported_investments_total is not None
            and not math.isclose(
                float(short_term_investments),
                float(reported_investments_total),
                rel_tol=0.001,
                abs_tol=1.0,
            )
        ):
            # Broad AFS totals can include securities classified inside cash equivalents.
            # Prefer the explicit balance-sheet component so cash is not counted twice.
            proxies.append("ignored_reported_investments_total_overlap_risk")
        elif (
            cash is not None
            and long_term_investments is not None
            and short_term_investments is None
            and reported_investments_total is None
        ):
            proxies.append("long_term_investments_only_for_cash_and_investments")
        elif (
            cash is not None
            and reported_investments_total is not None
            and short_term_investments is None
            and long_term_investments is None
        ):
            proxies.append("reported_investments_total_only_for_cash_and_investments")
        missing_set = set(missing)
        cash_is_proxy_only = "cash_only_for_cash_and_investments" in proxies
        if (
            "cash_and_investments" not in missing_set
            and "revenue" not in missing_set
            and not cash_is_proxy_only
        ):
            confidence = "high"
        elif row.get("cash_and_investments") is not None:
            confidence = "medium"
        else:
            confidence = "low"
        row["cash_source_concept"] = source_concepts.get("cash")
        row["short_term_investments_source_concept"] = source_concepts.get("short_term_investments")
        row["long_term_investments_source_concept"] = source_concepts.get("long_term_investments")
        row["marketable_investments_total_source_concept"] = source_concepts.get(
            "marketable_investments_reported_total"
        )
        row["rd_source_concept"] = source_concepts.get("rd_expense")
        row["ocf_source_concept"] = source_concepts.get("operating_cash_flow")
        row["operating_cash_flow_period_start"] = source_period_starts.get("operating_cash_flow")
        row["operating_cash_flow_duration_days"] = source_duration_days.get("operating_cash_flow")
        row["shares_source_concept"] = source_concepts.get("shares_outstanding") or source_concepts.get("weighted_average_shares_diluted") or source_concepts.get("weighted_average_shares_basic")
        row["revenue_source_concept"] = source_concepts.get("revenue")
        row["gross_profit_source_concept"] = source_concepts.get("gross_profit")
        row["cost_of_revenue_source_concept"] = source_concepts.get("cost_of_revenue")
        row["net_income_source_concept"] = source_concepts.get("net_income")
        row["missing_fields"] = ";".join(missing)
        row["proxy_fields_used"] = ";".join(proxies)
        row["confidence"] = confidence
        row["payload_json"] = json.dumps({"source_concepts": source_concepts}, ensure_ascii=True, sort_keys=True)
        out.append(row)
    out.sort(key=lambda item: (str(item.get("period_end") or ""), str(item.get("fiscal_period") or "")))
    return out


QUARTERLY_FIELDS = [
    "company_id",
    "period_end",
    "fiscal_year",
    "fiscal_period",
    "form",
    "filed_date",
    "accession_nodash",
    "cash",
    "cash_and_equivalents",
    "short_term_investments",
    "long_term_investments",
    "marketable_investments_total",
    "cash_and_investments",
    "restricted_cash",
    "current_assets",
    "current_liabilities",
    "working_capital",
    "total_assets",
    "total_liabilities",
    "total_debt",
    "revenue",
    "cost_of_revenue",
    "gross_profit",
    "rd_expense",
    "sgna_expense",
    "operating_income",
    "net_income",
    "interest_expense",
    "income_tax_expense",
    "depreciation_amortization",
    "eps_basic",
    "eps_diluted",
    "weighted_average_shares_basic",
    "weighted_average_shares_diluted",
    "operating_cash_flow",
    "operating_cash_flow_period_start",
    "operating_cash_flow_duration_days",
    "investing_cash_flow",
    "financing_cash_flow",
    "capital_expenditures",
    "free_cash_flow",
    "shares_outstanding",
    "cash_source_concept",
    "short_term_investments_source_concept",
    "long_term_investments_source_concept",
    "marketable_investments_total_source_concept",
    "rd_source_concept",
    "ocf_source_concept",
    "shares_source_concept",
    "revenue_source_concept",
    "gross_profit_source_concept",
    "cost_of_revenue_source_concept",
    "net_income_source_concept",
    "missing_fields",
    "proxy_fields_used",
    "confidence",
    "payload_json",
]

SIGN_CONVENTION_AUDIT_FIELDS = [
    "audit_type",
    "ticker",
    "company_name",
    "period_end",
    "form",
    "filed_date",
    "metric",
    "source_status",
    "reported_value",
    "current_abs_formula",
    "proposed_raw_formula",
    "current_abs_error",
    "proposed_raw_error",
    "winner",
    "error_delta_abs",
    "error_delta_pct",
    "revenue",
    "cost_of_revenue",
    "rd_expense",
    "sgna_expense",
    "operating_cash_flow",
    "capital_expenditures",
    "proxy_fields_used",
    "source_concept",
    "notes",
]


def sql_field_list(fields: list[str]) -> str:
    allowed = set(QUARTERLY_FIELDS)
    unknown = [field for field in fields if field not in allowed]
    if unknown:
        raise ValueError(f"Unknown company_facts_quarterly field(s): {', '.join(unknown)}")
    return ", ".join(quote_identifier(field) for field in fields)


def excluded_update_clause(fields: list[str]) -> str:
    allowed = set(QUARTERLY_FIELDS)
    unknown = [field for field in fields if field not in allowed]
    if unknown:
        raise ValueError(f"Unknown company_facts_quarterly update field(s): {', '.join(unknown)}")
    return ",\n                    ".join(
        f"{quote_identifier(field)} = excluded.{quote_identifier(field)}" for field in fields
    )


def parse_timestamp(raw: object) -> datetime | None:
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def payload_hash_for_json(payload: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def load_companyfacts_sync_state(conn: sqlite3.Connection, company_ids: list[int]) -> dict[int, dict[str, Any]]:
    if not company_ids:
        return {}
    if len(company_ids) > SQLITE_PARAM_CHUNK_SIZE:
        out: dict[int, dict[str, Any]] = {}
        for company_chunk in chunked(company_ids):
            out.update(load_companyfacts_sync_state(conn, [int(value) for value in company_chunk]))
        return out
    placeholders = ",".join("?" for _ in company_ids)
    rows = conn.execute(
        f"""
        SELECT company_id, latest_source_filing_date, payload_hash, normalizer_version, last_synced_at, sync_status
        FROM company_facts_sync_state
        WHERE company_id IN ({placeholders})
        """,
        tuple(company_ids),
    ).fetchall()
    return {int(row["company_id"]): dict(row) for row in rows}


def load_companyfacts_fact_summary(conn: sqlite3.Connection, company_ids: list[int]) -> dict[int, dict[str, Any]]:
    if not company_ids:
        return {}
    if len(company_ids) > SQLITE_PARAM_CHUNK_SIZE:
        out: dict[int, dict[str, Any]] = {}
        for company_chunk in chunked(company_ids):
            out.update(load_companyfacts_fact_summary(conn, [int(value) for value in company_chunk]))
        return out
    placeholders = ",".join("?" for _ in company_ids)
    rows = conn.execute(
        f"""
        SELECT company_id, COUNT(*) AS row_count, MAX(filed_date) AS latest_filed_date
        FROM company_facts_quarterly
        WHERE company_id IN ({placeholders})
        GROUP BY company_id
        """,
        tuple(company_ids),
    ).fetchall()
    return {int(row["company_id"]): dict(row) for row in rows}


def load_latest_source_filing_dates(conn: sqlite3.Connection, company_ids: list[int]) -> dict[int, str]:
    if not company_ids:
        return {}
    if len(company_ids) > SQLITE_PARAM_CHUNK_SIZE:
        out: dict[int, str] = {}
        for company_chunk in chunked(company_ids):
            out.update(load_latest_source_filing_dates(conn, [int(value) for value in company_chunk]))
        return out
    placeholders = ",".join("?" for _ in company_ids)
    form_placeholders = ",".join("?" for _ in ALLOWED_FORMS)
    rows = conn.execute(
        f"""
        SELECT company_id, MAX(filing_date) AS latest_filing_date
        FROM sec_filings
        WHERE company_id IN ({placeholders})
          AND form IN ({form_placeholders})
        GROUP BY company_id
        """,
        tuple(company_ids) + tuple(sorted(ALLOWED_FORMS)),
    ).fetchall()
    return {int(row["company_id"]): str(row["latest_filing_date"] or "") for row in rows}


def sync_state_is_fresh(sync_state: dict[str, Any] | None, *, ttl_hours: float) -> bool:
    if not sync_state:
        return False
    last_synced = parse_timestamp(sync_state.get("last_synced_at"))
    if last_synced is None:
        return False
    age_seconds = (datetime.now(timezone.utc) - last_synced.astimezone(timezone.utc)).total_seconds()
    return age_seconds <= ttl_hours * 3600.0


def should_refresh_company(
    company: Company,
    *,
    sync_state: dict[str, Any] | None,
    fact_summary: dict[str, Any] | None,
    latest_source_filing_date: str,
    ttl_hours: float,
    force_refresh: bool,
) -> bool:
    if force_refresh:
        return True
    if str((sync_state or {}).get("normalizer_version") or "") != COMPANYFACTS_NORMALIZER_VERSION:
        return True
    if not company.cik:
        return True
    row_count = int((fact_summary or {}).get("row_count") or 0)
    if row_count <= 0:
        return True
    if not sync_state:
        return True
    if latest_source_filing_date and latest_source_filing_date > str(sync_state.get("latest_source_filing_date") or ""):
        return True
    if not sync_state_is_fresh(sync_state, ttl_hours=ttl_hours):
        return True
    return False


def fetch_companyfacts_result(
    company: Company,
    *,
    url_template: str,
    headers: dict[str, str],
    cache_dir: Path,
    ttl_hours: float,
    sleep_sec: float,
    timeout_sec: float,
    max_retries: int,
    throttle: HostThrottle,
    cutoff: date,
    asof: date | None = None,
    latest_source_filing_date: str,
    http: CachedHttpClient | None = None,
) -> CompanyFactsFetchResult:
    if not company.cik:
        return CompanyFactsFetchResult(company=company, latest_source_filing_date=latest_source_filing_date, error="missing_cik")
    url = url_template.format(cik=company.cik)
    try:
        if http is None:
            with CachedHttpClient(
                cache_dir=cache_dir,
                sleep_sec=sleep_sec,
                timeout_sec=timeout_sec,
                max_retries=max_retries,
                throttle=throttle,
            ) as client:
                payload = client.fetch_json(
                    namespace="sec_companyfacts",
                    url=url,
                    headers=headers,
                    ttl_hours=ttl_hours,
                )
        else:
            payload = http.fetch_json(
                namespace="sec_companyfacts",
                url=url,
                headers=headers,
                ttl_hours=ttl_hours,
            )
        observations = parse_observations(payload, company=company, cutoff=cutoff, asof=asof)
        normalized = normalize_rows(observations, company_id=company.company_id)
        return CompanyFactsFetchResult(
            company=company,
            latest_source_filing_date=latest_source_filing_date,
            payload_hash=payload_hash_for_json(payload),
            observations=tuple(observations),
            normalized=tuple(normalized),
        )
    except Exception as exc:
        return CompanyFactsFetchResult(company=company, latest_source_filing_date=latest_source_filing_date, error=f"{type(exc).__name__}: {exc}")


def replace_company_facts(
    conn: sqlite3.Connection,
    *,
    company: Company,
    observations: list[dict[str, Any]],
    normalized: list[dict[str, Any]],
    cutoff: date,
) -> None:
    now = utc_now()
    cutoff_text = cutoff.isoformat()
    conn.execute(
        """
        DELETE FROM financial_fact_observations
        WHERE company_id = ?
          AND (period_end IS NULL OR period_end = '' OR period_end >= ?)
        """,
        (company.company_id, cutoff_text),
    )
    conn.execute(
        """
        DELETE FROM company_facts_quarterly
        WHERE company_id = ?
          AND (period_end IS NULL OR period_end = '' OR period_end >= ?)
        """,
        (company.company_id, cutoff_text),
    )
    conn.executemany(
        """
        INSERT INTO financial_fact_observations(
            company_id, cik, taxonomy, concept, label, unit, value, period_start, period_end,
            fiscal_year, fiscal_period, form, filed_date, accession_nodash, frame,
            source, confidence, created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                obs["company_id"],
                obs["cik"],
                obs["taxonomy"],
                obs["concept"],
                obs["label"],
                obs["unit"],
                obs["value"],
                obs["period_start"],
                obs["period_end"],
                obs["fiscal_year"],
                obs["fiscal_period"],
                obs["form"],
                obs["filed_date"],
                obs["accession_nodash"],
                obs["frame"],
                obs["source"],
                obs["confidence"],
                now,
            )
            for obs in observations
        ],
    )
    update_fields = [field for field in QUARTERLY_FIELDS if field not in {"company_id", "period_end", "fiscal_period", "form"}]
    update_clause = excluded_update_clause(update_fields)
    insert_fields_sql = sql_field_list(QUARTERLY_FIELDS)
    conn.executemany(
        f"""
        INSERT INTO company_facts_quarterly({insert_fields_sql}, created_at, updated_at)
        VALUES ({", ".join("?" for _ in QUARTERLY_FIELDS)}, ?, ?)
        ON CONFLICT(company_id, period_end, fiscal_period, form) DO UPDATE SET
            {update_clause},
            updated_at = excluded.updated_at
        """,
        [tuple(row.get(field) for field in QUARTERLY_FIELDS) + (now, now) for row in normalized],
    )


def upsert_company_facts_sync_state(
    conn: sqlite3.Connection,
    *,
    company_id: int,
    latest_source_filing_date: str,
    payload_hash: str,
    sync_status: str,
) -> None:
    now = utc_now()
    conn.execute(
        """
        INSERT INTO company_facts_sync_state(
            company_id, latest_source_filing_date, payload_hash, normalizer_version,
            last_synced_at, sync_status, created_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(company_id) DO UPDATE SET
            latest_source_filing_date = excluded.latest_source_filing_date,
            payload_hash = excluded.payload_hash,
            normalizer_version = excluded.normalizer_version,
            last_synced_at = excluded.last_synced_at,
            sync_status = excluded.sync_status,
            updated_at = excluded.updated_at
        """,
        (
            company_id,
            latest_source_filing_date,
            payload_hash,
            COMPANYFACTS_NORMALIZER_VERSION,
            now,
            sync_status,
            now,
            now,
        ),
    )


def write_quarterly_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["ticker", "company_name", *QUARTERLY_FIELDS], lineterminator="\n", extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def write_sign_convention_audit_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=SIGN_CONVENTION_AUDIT_FIELDS, lineterminator="\n", extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def payload_source_concept(row: dict[str, Any], field: str) -> str:
    direct = str(row.get(f"{field}_source_concept") or "").strip()
    if direct:
        return direct
    try:
        payload = json.loads(str(row.get("payload_json") or "{}"))
    except json.JSONDecodeError:
        return ""
    source_concepts = payload.get("source_concepts")
    if not isinstance(source_concepts, dict):
        return ""
    return str(source_concepts.get(field) or "").strip()


def formula_diff_is_material(
    current_abs_formula: float,
    proposed_raw_formula: float,
    *,
    materiality_abs: float,
    materiality_pct: float,
) -> bool:
    delta = abs(current_abs_formula - proposed_raw_formula)
    denominator = max(abs(current_abs_formula), abs(proposed_raw_formula), 1.0)
    return delta >= materiality_abs or (delta / denominator) >= materiality_pct


def formula_comparison_fields(
    *,
    reported_value: float | None,
    current_abs_formula: float,
    proposed_raw_formula: float,
) -> dict[str, Any]:
    if reported_value is None:
        return {
            "reported_value": "",
            "current_abs_error": "",
            "proposed_raw_error": "",
            "winner": "",
            "error_delta_abs": "",
            "error_delta_pct": "",
        }
    current_abs_error = abs(current_abs_formula - reported_value)
    proposed_raw_error = abs(proposed_raw_formula - reported_value)
    tolerance = max(abs(reported_value), 1.0) * 1e-6
    if current_abs_error + tolerance < proposed_raw_error:
        winner = "current_abs_formula"
    elif proposed_raw_error + tolerance < current_abs_error:
        winner = "proposed_raw_formula"
    else:
        winner = "tie"
    denominator = max(abs(reported_value), 1.0)
    return {
        "reported_value": reported_value,
        "current_abs_error": current_abs_error,
        "proposed_raw_error": proposed_raw_error,
        "winner": winner,
        "error_delta_abs": current_abs_error - proposed_raw_error,
        "error_delta_pct": (current_abs_error - proposed_raw_error) / denominator,
    }


def build_sign_audit_row(
    row: dict[str, Any],
    *,
    metric: str,
    source_status: str,
    source_concept: str,
    reported_value: float | None,
    current_abs_formula: float,
    proposed_raw_formula: float,
    notes: list[str],
) -> dict[str, Any]:
    comparison = formula_comparison_fields(
        reported_value=reported_value,
        current_abs_formula=current_abs_formula,
        proposed_raw_formula=proposed_raw_formula,
    )
    return {
        "audit_type": "reported_formula_compare" if reported_value is not None else "derived_formula_compare",
        "ticker": row.get("ticker", ""),
        "company_name": row.get("company_name", ""),
        "period_end": row.get("period_end", ""),
        "form": row.get("form", ""),
        "filed_date": row.get("filed_date", ""),
        "metric": metric,
        "source_status": source_status,
        "current_abs_formula": current_abs_formula,
        "proposed_raw_formula": proposed_raw_formula,
        "revenue": row.get("revenue", ""),
        "cost_of_revenue": row.get("cost_of_revenue", ""),
        "rd_expense": row.get("rd_expense", ""),
        "sgna_expense": row.get("sgna_expense", ""),
        "operating_cash_flow": row.get("operating_cash_flow", ""),
        "capital_expenditures": row.get("capital_expenditures", ""),
        "proxy_fields_used": row.get("proxy_fields_used", ""),
        "source_concept": source_concept,
        "notes": ";".join(notes),
        **comparison,
    }


def build_sign_convention_audit_rows(
    rows: list[dict[str, Any]],
    *,
    materiality_abs: float,
    materiality_pct: float,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    audit_rows: list[dict[str, Any]] = []
    summary = {
        "gross_comparable": 0,
        "operating_comparable": 0,
        "current_abs_wins": 0,
        "proposed_raw_wins": 0,
        "negative_component_rows": 0,
    }

    for row in rows:
        proxy_fields = set(str(row.get("proxy_fields_used") or "").split(";"))
        revenue = to_float(row.get("revenue"))
        cost = to_float(row.get("cost_of_revenue"))
        rd_expense = to_float(row.get("rd_expense"))
        sgna_expense = to_float(row.get("sgna_expense"))
        operating_cash_flow = to_float(row.get("operating_cash_flow"))
        capital_expenditures = to_float(row.get("capital_expenditures"))
        negative_components = [
            field
            for field, value in (
                ("cost_of_revenue", cost),
                ("rd_expense", rd_expense),
                ("sgna_expense", sgna_expense),
                ("capital_expenditures", capital_expenditures),
            )
            if value is not None and value < 0.0
        ]
        if negative_components:
            summary["negative_component_rows"] += 1

        if revenue is not None and cost is not None:
            gross_current = revenue - abs(cost)
            gross_proposed = revenue - cost
            gross_source = payload_source_concept(row, "gross_profit")
            gross_reported = to_float(row.get("gross_profit")) if gross_source else None
            gross_proxy_used = bool(
                {
                    "gross_profit_revenue_minus_cost_of_revenue",
                    "gross_profit_revenue_minus_cost_of_revenue_raw_sign",
                }
                & proxy_fields
            )
            source_status = "reported" if gross_source else "proxied" if gross_proxy_used else "derived"
            if gross_reported is not None:
                summary["gross_comparable"] += 1
            notes = [f"negative_components={','.join(negative_components)}"] if negative_components else []
            include = bool(negative_components) or formula_diff_is_material(
                gross_current,
                gross_proposed,
                materiality_abs=materiality_abs,
                materiality_pct=materiality_pct,
            )
            audit_row = build_sign_audit_row(
                row,
                metric="gross_profit",
                source_status=source_status,
                source_concept=gross_source,
                reported_value=gross_reported,
                current_abs_formula=gross_current,
                proposed_raw_formula=gross_proposed,
                notes=notes,
            )
            if gross_reported is not None and str(audit_row.get("winner")) == "current_abs_formula":
                summary["current_abs_wins"] += 1
            elif gross_reported is not None and str(audit_row.get("winner")) == "proposed_raw_formula":
                summary["proposed_raw_wins"] += 1
                include = True
            if include:
                audit_rows.append(audit_row)

        if revenue is not None and cost is not None and rd_expense is not None and sgna_expense is not None:
            op_current = revenue - sum(abs(value) for value in (cost, rd_expense, sgna_expense))
            op_proposed = revenue - sum((cost, rd_expense, sgna_expense))
            op_source = payload_source_concept(row, "operating_income")
            op_reported = to_float(row.get("operating_income")) if op_source else None
            operating_proxy_used = bool(
                {
                    "operating_income_revenue_minus_cost_rd_sgna",
                    "operating_income_revenue_minus_cost_rd_sgna_raw_sign",
                }
                & proxy_fields
            )
            source_status = "reported" if op_source else "proxied" if operating_proxy_used else "derived"
            if op_reported is not None:
                summary["operating_comparable"] += 1
            notes = [f"negative_components={','.join(negative_components)}"] if negative_components else []
            include = bool(negative_components) or formula_diff_is_material(
                op_current,
                op_proposed,
                materiality_abs=materiality_abs,
                materiality_pct=materiality_pct,
            )
            audit_row = build_sign_audit_row(
                row,
                metric="operating_income",
                source_status=source_status,
                source_concept=op_source,
                reported_value=op_reported,
                current_abs_formula=op_current,
                proposed_raw_formula=op_proposed,
                notes=notes,
            )
            if op_reported is not None and str(audit_row.get("winner")) == "current_abs_formula":
                summary["current_abs_wins"] += 1
            elif op_reported is not None and str(audit_row.get("winner")) == "proposed_raw_formula":
                summary["proposed_raw_wins"] += 1
                include = True
            if include:
                audit_rows.append(audit_row)

        if operating_cash_flow is not None and capital_expenditures is not None:
            fcf_current = operating_cash_flow - abs(capital_expenditures)
            fcf_proposed = operating_cash_flow - capital_expenditures
            notes = [f"negative_components={','.join(negative_components)}"] if negative_components else []
            include = bool(capital_expenditures < 0.0) or formula_diff_is_material(
                fcf_current,
                fcf_proposed,
                materiality_abs=materiality_abs,
                materiality_pct=materiality_pct,
            )
            if include:
                audit_rows.append(
                    build_sign_audit_row(
                        row,
                        metric="free_cash_flow",
                        source_status="derived",
                        source_concept=payload_source_concept(row, "capital_expenditures"),
                        reported_value=None,
                        current_abs_formula=fcf_current,
                        proposed_raw_formula=fcf_proposed,
                        notes=notes,
                    )
                )

    audit_rows.sort(
        key=lambda item: (
            str(item.get("metric") or ""),
            str(item.get("ticker") or ""),
            str(item.get("period_end") or ""),
            str(item.get("form") or ""),
        )
    )
    return audit_rows, summary


def log_sign_convention_audit_summary(
    *,
    audit_rows: list[dict[str, Any]],
    summary: dict[str, int],
    output_path: Path,
    raw_win_warning_pct: float,
) -> None:
    comparable = int(summary.get("gross_comparable", 0)) + int(summary.get("operating_comparable", 0))
    raw_wins = int(summary.get("proposed_raw_wins", 0))
    raw_win_rate = (100.0 * raw_wins / comparable) if comparable else 0.0
    log_func = LOGGER.warning if raw_wins and raw_win_rate >= raw_win_warning_pct else LOGGER.info
    log_func(
        "SEC companyfacts sign audit rows=%d comparable=%d current_abs_wins=%d proposed_raw_wins=%d raw_win_rate=%.2f%% negative_component_rows=%d output=%s",
        len(audit_rows),
        comparable,
        int(summary.get("current_abs_wins", 0)),
        raw_wins,
        raw_win_rate,
        int(summary.get("negative_component_rows", 0)),
        output_path,
    )


def export_quarterly_rows(conn: sqlite3.Connection, company_ids: list[int]) -> list[dict[str, Any]]:
    if not company_ids:
        return []
    if len(company_ids) > SQLITE_PARAM_CHUNK_SIZE:
        rows: list[dict[str, Any]] = []
        for company_chunk in chunked(company_ids):
            rows.extend(export_quarterly_rows(conn, [int(value) for value in company_chunk]))
        rows.sort(
            key=lambda row: (
                str(row.get("ticker") or ""),
                -int(str(row.get("period_end") or "").replace("-", "") or "0"),
                -int(str(row.get("filed_date") or "").replace("-", "") or "0"),
            )
        )
        return rows
    placeholders = ",".join("?" for _ in company_ids)
    rows = conn.execute(
        f"""
        SELECT c.ticker, c.company_name, q.*
        FROM company_facts_quarterly q
        JOIN companies c ON c.company_id = q.company_id
        WHERE q.company_id IN ({placeholders})
        ORDER BY c.ticker, q.period_end DESC, q.filed_date DESC
        """,
        tuple(company_ids),
    ).fetchall()
    return [dict(row) for row in rows]


def log_data_quality_issue(
    conn: sqlite3.Connection,
    *,
    company: Company,
    asof_date: str,
    issue_type: str,
    severity: str,
    message: str,
    field_name: str | None = None,
    proxy_used: str | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO data_quality_issues(
            asof_date, company_id, ticker, table_name, field_name, issue_type, severity, proxy_used, message, created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            asof_date,
            company.company_id,
            company.ticker,
            "company_facts_quarterly",
            field_name,
            issue_type,
            severity,
            proxy_used,
            message,
            utc_now(),
        ),
    )


def main() -> None:
    configure_logging()
    args = parse_args()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    base_dir = config_path.parent
    identity_registry_path = resolve_path(
        cfg_get(config, "active_biotech_history.registry_csv", "data/active_biotech_historical_additions.csv"),
        base_dir=base_dir,
    )
    security_identity_rules = (
        load_security_identity_rules(identity_registry_path) if args.include_historical_ciks else {}
    )
    db_path = args.db.expanduser().resolve() if args.db else resolve_path(cfg_get(config, "paths.database_path"), base_dir=base_dir)
    universe_csv = (
        args.universe_csv.expanduser().resolve()
        if args.universe_csv
        else resolve_path(cfg_get(config, "sec_companyfacts_history.final_scoring_universe_csv"), base_dir=base_dir)
    )
    output_csv = (
        args.output_csv.expanduser().resolve()
        if args.output_csv
        else resolve_path(cfg_get(config, "sec_companyfacts_history.output_csv"), base_dir=base_dir)
    )
    sign_audit_csv = resolve_path(
        cfg_get(
            config,
            "sec_companyfacts_history.sign_convention_audit_csv",
            "../output/biotech_index_reports/company_facts_sign_convention_audit.csv",
        ),
        base_dir=base_dir,
    )
    if args.sign_audit_csv:
        sign_audit_csv = args.sign_audit_csv.expanduser().resolve()
    cache_dir = resolve_path(cfg_get(config, "sec_companyfacts_history.cache_dir", "../output/biotech_index_cache"), base_dir=base_dir)
    url_template = str(cfg_get(config, "sec_companyfacts_history.companyfacts_url_template", "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"))
    user_agent = str(cfg_get(config, "sec_companyfacts_history.user_agent", "") or "").strip()
    if not user_agent:
        raise ValueError("sec_companyfacts_history.user_agent is required for SEC requests.")
    lookback_years = (
        int(args.lookback_years)
        if int(args.lookback_years) > 0
        else int(cfg_get(config, "sec_companyfacts_history.lookback_years", 3))
    )
    asof_obj = parse_date(args.asof) if args.asof else datetime.now(timezone.utc).date()
    if asof_obj is None:
        raise ValueError(f"Invalid --asof date: {args.asof}")
    lookback = max(1, lookback_years)
    try:
        cutoff = asof_obj.replace(year=asof_obj.year - lookback)
    except ValueError:
        # Feb 29 asof with no leap day in the target year.
        cutoff = asof_obj.replace(year=asof_obj.year - lookback, day=28)
    ticker_filter = {normalize_ticker(x) for x in args.tickers.split(",") if normalize_ticker(x)}
    sqlite_timeout_sec = float(cfg_get(config, "runtime.sqlite_timeout_sec", 30.0))
    asof = asof_obj.isoformat()
    ttl_hours = float(cfg_get(config, "sec_companyfacts_history.ttl_hours", 24.0))
    sleep_sec = float(cfg_get(config, "sec_companyfacts_history.sleep_sec", 0.15))
    timeout_sec = float(cfg_get(config, "sec_companyfacts_history.timeout_sec", 45.0))
    max_retries = int(cfg_get(config, "sec_companyfacts_history.max_retries", 3))
    max_workers = max(1, int(cfg_get(config, "sec_companyfacts_history.max_workers", 6)))
    sign_materiality_abs = float(cfg_get(config, "sec_companyfacts_history.sign_convention_materiality_abs", 5_000_000.0))
    sign_materiality_pct = float(cfg_get(config, "sec_companyfacts_history.sign_convention_materiality_pct", 0.05))
    sign_raw_win_warning_pct = float(cfg_get(config, "sec_companyfacts_history.sign_convention_raw_win_warning_pct", 5.0))
    all_csv_rows: list[dict[str, Any]] = []
    run_id: int | None = None

    with connect(db_path, timeout_sec=sqlite_timeout_sec) as conn:
        init_db(conn)
        scoring_tickers = read_scoring_tickers(universe_csv)
        primary_company_list = load_companies(
            conn,
            scoring_tickers=scoring_tickers,
            ticker_filter=ticker_filter,
            max_companies=int(args.max_companies),
            include_delisted_calibration=bool(args.include_delisted_calibration),
        )
        primary_companies = {company.company_id: company for company in primary_company_list}
        companies = expand_company_cik_history(primary_company_list, security_identity_rules)
        subset_mode = subset_mode_enabled(ticker_filter=ticker_filter, max_count=int(args.max_companies))
        output_csv = subset_output_path(output_csv, subset_mode=subset_mode)
        sign_audit_csv = subset_output_path(sign_audit_csv, subset_mode=subset_mode)
        validate_nonempty_selection(count=len(companies), context="SEC companyfacts sync", subset_mode=subset_mode)
        loaded_tickers = [company.ticker for company in companies]
        validate_requested_tickers(requested_tickers=ticker_filter, loaded_tickers=loaded_tickers, context="SEC companyfacts sync")
        validate_full_universe_coverage(
            expected_tickers=scoring_tickers,
            observed_tickers=loaded_tickers,
            context="SEC companyfacts sync",
            subset_mode=subset_mode,
        )
        company_ids = sorted(primary_companies)
        if args.audit_only:
            all_csv_rows = export_quarterly_rows(conn, company_ids)
            audit_rows, audit_summary = build_sign_convention_audit_rows(
                all_csv_rows,
                materiality_abs=sign_materiality_abs,
                materiality_pct=sign_materiality_pct,
            )
            write_sign_convention_audit_csv(sign_audit_csv, audit_rows)
            log_sign_convention_audit_summary(
                audit_rows=audit_rows,
                summary=audit_summary,
                output_path=sign_audit_csv,
                raw_win_warning_pct=sign_raw_win_warning_pct,
            )
            LOGGER.info("Wrote SEC companyfacts sign-convention audit from existing DB rows: rows=%d output=%s", len(audit_rows), sign_audit_csv)
            return
        try:
            run_id = start_run(conn, run_type="sync_sec_companyfacts_history", input_path=universe_csv)
            error_count = 0
            sync_state_by_company = load_companyfacts_sync_state(conn, company_ids)
            fact_summary_by_company = load_companyfacts_fact_summary(conn, company_ids)
            latest_source_dates = load_latest_source_filing_dates(conn, company_ids)
            refresh_targets = [
                company
                for company in companies
                if args.include_historical_ciks
                or should_refresh_company(
                    company,
                    sync_state=sync_state_by_company.get(company.company_id),
                    fact_summary=fact_summary_by_company.get(company.company_id),
                    latest_source_filing_date=latest_source_dates.get(company.company_id, ""),
                    ttl_hours=ttl_hours,
                    force_refresh=bool(args.full_refresh),
                )
            ]
            LOGGER.info(
                "SEC companyfacts refresh targets=%d skipped_fresh=%d max_workers=%d",
                len(refresh_targets),
                len(companies) - len(refresh_targets),
                max_workers,
            )
            headers = {"User-Agent": user_agent, "Accept": "application/json"}
            throttle = HostThrottle()
            results: list[CompanyFactsFetchResult] = []
            thread_local = threading.local()
            thread_clients: list[CachedHttpClient] = []
            thread_clients_lock = threading.Lock()

            def get_thread_http_client() -> CachedHttpClient:
                client = getattr(thread_local, "http_client", None)
                if client is None:
                    client = CachedHttpClient(
                        cache_dir=cache_dir,
                        sleep_sec=sleep_sec,
                        timeout_sec=timeout_sec,
                        max_retries=max_retries,
                        throttle=throttle,
                    )
                    thread_local.http_client = client
                    with thread_clients_lock:
                        thread_clients.append(client)
                return client

            def fetch_with_thread_client(company: Company) -> CompanyFactsFetchResult:
                return fetch_companyfacts_result(
                    company,
                    url_template=url_template,
                    headers=headers,
                    cache_dir=cache_dir,
                    ttl_hours=ttl_hours,
                    sleep_sec=sleep_sec,
                    timeout_sec=timeout_sec,
                    max_retries=max_retries,
                    throttle=throttle,
                    cutoff=cutoff,
                    asof=asof_obj,
                    latest_source_filing_date=latest_source_dates.get(company.company_id, ""),
                    http=get_thread_http_client(),
                )

            if max_workers == 1:
                with CachedHttpClient(
                    cache_dir=cache_dir,
                    sleep_sec=sleep_sec,
                    timeout_sec=timeout_sec,
                    max_retries=max_retries,
                    throttle=throttle,
                ) as http_client:
                    for company in refresh_targets:
                        results.append(
                            fetch_companyfacts_result(
                                company,
                                url_template=url_template,
                                headers=headers,
                                cache_dir=cache_dir,
                                ttl_hours=ttl_hours,
                                sleep_sec=sleep_sec,
                                timeout_sec=timeout_sec,
                                max_retries=max_retries,
                                throttle=throttle,
                                cutoff=cutoff,
                                asof=asof_obj,
                                latest_source_filing_date=latest_source_dates.get(company.company_id, ""),
                                http=http_client,
                            )
                        )
            else:
                try:
                    with ThreadPoolExecutor(max_workers=max_workers) as executor:
                        futures = {
                            executor.submit(fetch_with_thread_client, company): company
                            for company in refresh_targets
                        }
                        pending_raise: BaseException | None = None
                        for idx, future in enumerate(as_completed(futures), start=1):
                            company = futures[future]
                            try:
                                results.append(future.result())
                            except BaseException as exc:
                                pending_raise = exc
                                if isinstance(exc, (SystemExit, KeyboardInterrupt, GeneratorExit)):
                                    LOGGER.warning("SEC companyfacts worker interrupted for %s", company.ticker)
                                else:
                                    LOGGER.exception("SEC companyfacts worker failed for %s", company.ticker)
                                for other in futures:
                                    if other is not future:
                                        other.cancel()
                                break
                            if idx % 25 == 0 or idx == len(futures):
                                LOGGER.info("Fetched SEC companyfacts for %d/%d targets", idx, len(futures))
                        if pending_raise is not None:
                            raise pending_raise
                finally:
                    for client in thread_clients:
                        client.close()

            if args.include_historical_ciks:
                results = merge_companyfacts_results(results, primary_companies=primary_companies)
            for idx, result in enumerate(sorted(results, key=lambda item: item.company.ticker), start=1):
                company = result.company
                if result.error == "missing_cik":
                    error_count += 1
                    with conn:
                        log_data_quality_issue(
                            conn,
                            company=company,
                            asof_date=asof,
                            field_name="cik",
                            issue_type="missing_cik",
                            severity="high",
                            message="No CIK available for SEC companyfacts.",
                        )
                    continue
                if result.error:
                    error_count += 1
                    LOGGER.warning("SEC companyfacts failed for %s: %s", company.ticker, result.error)
                    with conn:
                        log_data_quality_issue(conn, company=company, asof_date=asof, issue_type="fetch_error", severity="high", message=result.error)
                        upsert_company_facts_sync_state(
                            conn,
                            company_id=company.company_id,
                            latest_source_filing_date=result.latest_source_filing_date,
                            payload_hash="",
                            sync_status="fetch_error",
                        )
                    continue
                state = sync_state_by_company.get(company.company_id)
                payload_unchanged = (
                    state is not None
                    and str(state.get("payload_hash") or "") == result.payload_hash
                    and str(state.get("latest_source_filing_date") or "") == result.latest_source_filing_date
                    and str(state.get("normalizer_version") or "") == COMPANYFACTS_NORMALIZER_VERSION
                )
                with conn:
                    if not payload_unchanged:
                        replace_company_facts(
                            conn,
                            company=company,
                            observations=list(result.observations),
                            normalized=list(result.normalized),
                            cutoff=cutoff,
                        )
                    upsert_company_facts_sync_state(
                        conn,
                        company_id=company.company_id,
                        latest_source_filing_date=result.latest_source_filing_date,
                        payload_hash=result.payload_hash,
                        sync_status="unchanged" if payload_unchanged else "synced",
                    )
                    if not result.normalized:
                        log_data_quality_issue(
                            conn,
                            company=company,
                            asof_date=asof,
                            issue_type="no_normalized_facts",
                            severity="medium",
                            message="SEC companyfacts returned no usable normalized financial rows.",
                        )
                LOGGER.info(
                    "[%d/%d] %s facts=%d normalized_rows=%d status=%s",
                    idx,
                    len(results),
                    company.ticker,
                    len(result.observations),
                    len(result.normalized),
                    "unchanged" if payload_unchanged else "synced",
                )
            all_csv_rows = export_quarterly_rows(conn, company_ids)
            write_quarterly_csv(output_csv, all_csv_rows)
            audit_rows, audit_summary = build_sign_convention_audit_rows(
                all_csv_rows,
                materiality_abs=sign_materiality_abs,
                materiality_pct=sign_materiality_pct,
            )
            write_sign_convention_audit_csv(sign_audit_csv, audit_rows)
            log_sign_convention_audit_summary(
                audit_rows=audit_rows,
                summary=audit_summary,
                output_path=sign_audit_csv,
                raw_win_warning_pct=sign_raw_win_warning_pct,
            )
            finish_run(
                conn,
                run_id=run_id,
                status="success" if error_count == 0 else "partial",
                row_count=len(all_csv_rows),
                message=f"companies={len(companies)} refreshed={len(refresh_targets)} errors={error_count} output={output_csv} sign_audit={sign_audit_csv}",
            )
            if error_count > 0 and not args.allow_partial:
                raise SystemExit(2)
        except BaseException as exc:
            if run_id is not None and not (isinstance(exc, SystemExit) and exc.code in (0, None)):
                finish_run(conn, run_id=run_id, status="failed", row_count=0, message=f"{type(exc).__name__}: {exc}")
            raise
    LOGGER.info("Synced SEC companyfacts history: companies=%d rows=%d output=%s sign_audit=%s", len(companies), len(all_csv_rows), output_csv, sign_audit_csv)


if __name__ == "__main__":
    main()
