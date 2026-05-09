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
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from biotech_index.core.config import cfg_get, load_yaml, resolve_path
from biotech_index.core.db import connect, finish_run, init_db, start_run, utc_now
from biotech_index.core.http_cache import CachedHttpClient, HostThrottle
from biotech_index.core.logging_utils import configure_utc_logging
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
        "ShortTermInvestmentsAvailableForSaleSecurities",
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
    parser.add_argument("--full-refresh", action="store_true", help="Force refresh for all eligible companies regardless of sync state.")
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
) -> list[Company]:
    rows = conn.execute(
        """
        SELECT company_id, ticker, cik, company_name
        FROM companies
        WHERE is_active = 1
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


def fiscal_sort_key(obs: dict[str, Any]) -> tuple[str, str, str]:
    return (
        str(obs.get("filed_date") or ""),
        str(obs.get("form") or ""),
        str(obs.get("accession_nodash") or ""),
    )


def parse_observations(payload: dict[str, Any], *, company: Company, cutoff: date) -> list[dict[str, Any]]:
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
                    form = str(entry.get("form") or "").upper()
                    if not form or form not in ALLOWED_FORMS:
                        continue
                    value = to_float(entry.get("val"))
                    if value is None:
                        continue
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
                            "fiscal_year": int(entry["fy"]) if str(entry.get("fy") or "").isdigit() else None,
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
    return candidate if fiscal_sort_key(candidate) >= fiscal_sort_key(current) else current


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
            },
        )
        if fiscal_sort_key(obs) >= (str(row.get("filed_date") or ""), str(row.get("form") or ""), str(row.get("accession_nodash") or "")):
            row["filed_date"] = obs.get("filed_date")
            row["accession_nodash"] = obs.get("accession_nodash")
        row[group] = obs.get("value")
        row["_source_concepts"][group] = obs.get("concept")

    out: list[dict[str, Any]] = []
    for row in period_rows.values():
        source_concepts = row.pop("_source_concepts", {})
        cash = row.get("cash")
        short_term_investments = row.get("short_term_investments")
        row["cash_and_equivalents"] = cash
        row["cash_and_investments"] = (
            (float(cash or 0.0) + float(short_term_investments or 0.0)) if cash is not None else None
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
            row["gross_profit"] = float(row["revenue"]) - abs(float(row["cost_of_revenue"]))
            proxies.append("gross_profit_revenue_minus_cost_of_revenue")
        if row.get("operating_income") is None and row.get("revenue") is not None:
            operating_costs = [row.get("cost_of_revenue"), row.get("rd_expense"), row.get("sgna_expense")]
            if all(value is not None for value in operating_costs):
                row["operating_income"] = float(row["revenue"]) - sum(abs(float(value or 0.0)) for value in operating_costs)
                proxies.append("operating_income_revenue_minus_cost_rd_sgna")
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
            row["free_cash_flow"] = float(row["operating_cash_flow"]) - abs(float(row["capital_expenditures"]))

        missing: list[str] = []
        for field in ("cash_and_investments", "revenue", "rd_expense", "operating_cash_flow", "current_assets", "current_liabilities"):
            if row.get(field) is None:
                missing.append(field)
        if cash is not None and short_term_investments is None:
            proxies.append("cash_only_for_cash_and_investments")
        confidence = "high" if not missing[:2] else "medium" if row.get("cash_and_investments") is not None else "low"
        row["cash_source_concept"] = source_concepts.get("cash")
        row["rd_source_concept"] = source_concepts.get("rd_expense")
        row["ocf_source_concept"] = source_concepts.get("operating_cash_flow")
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
    "investing_cash_flow",
    "financing_cash_flow",
    "capital_expenditures",
    "free_cash_flow",
    "shares_outstanding",
    "cash_source_concept",
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
        SELECT company_id, latest_source_filing_date, payload_hash, last_synced_at, sync_status
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
    latest_source_filing_date: str,
) -> CompanyFactsFetchResult:
    if not company.cik:
        return CompanyFactsFetchResult(company=company, latest_source_filing_date=latest_source_filing_date, error="missing_cik")
    url = url_template.format(cik=company.cik)
    try:
        with CachedHttpClient(
            cache_dir=cache_dir,
            sleep_sec=sleep_sec,
            timeout_sec=timeout_sec,
            max_retries=max_retries,
            throttle=throttle,
        ) as http:
            payload = http.fetch_json(
                namespace="sec_companyfacts",
                url=url,
                headers=headers,
                ttl_hours=ttl_hours,
            )
        observations = parse_observations(payload, company=company, cutoff=cutoff)
        normalized = normalize_rows(observations, company_id=company.company_id)
        return CompanyFactsFetchResult(
            company=company,
            latest_source_filing_date=latest_source_filing_date,
            payload_hash=payload_hash_for_json(payload),
            observations=tuple(observations),
            normalized=tuple(normalized),
        )
    except BaseException as exc:
        if isinstance(exc, (SystemExit, KeyboardInterrupt)):
            raise
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
    update_clause = ",\n                    ".join(f"{field} = excluded.{field}" for field in update_fields)
    conn.executemany(
        f"""
        INSERT INTO company_facts_quarterly({", ".join(QUARTERLY_FIELDS)}, created_at, updated_at)
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
            company_id, latest_source_filing_date, payload_hash, last_synced_at, sync_status, created_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(company_id) DO UPDATE SET
            latest_source_filing_date = excluded.latest_source_filing_date,
            payload_hash = excluded.payload_hash,
            last_synced_at = excluded.last_synced_at,
            sync_status = excluded.sync_status,
            updated_at = excluded.updated_at
        """,
        (company_id, latest_source_filing_date, payload_hash, now, sync_status, now, now),
    )


def write_quarterly_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["ticker", "company_name", *QUARTERLY_FIELDS], lineterminator="\n", extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


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
    db_path = args.db.expanduser().resolve() if args.db else resolve_path(cfg_get(config, "paths.database_path"), base_dir=base_dir)
    universe_csv = resolve_path(cfg_get(config, "sec_companyfacts_history.final_scoring_universe_csv"), base_dir=base_dir)
    output_csv = resolve_path(cfg_get(config, "sec_companyfacts_history.output_csv"), base_dir=base_dir)
    cache_dir = resolve_path(cfg_get(config, "sec_companyfacts_history.cache_dir", "../output/biotech_index_cache"), base_dir=base_dir)
    url_template = str(cfg_get(config, "sec_companyfacts_history.companyfacts_url_template", "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"))
    user_agent = str(cfg_get(config, "sec_companyfacts_history.user_agent", "") or "").strip()
    if not user_agent:
        raise ValueError("sec_companyfacts_history.user_agent is required for SEC requests.")
    lookback_years = int(cfg_get(config, "sec_companyfacts_history.lookback_years", 3))
    asof_obj = parse_date(args.asof) if args.asof else datetime.now(timezone.utc).date()
    if asof_obj is None:
        raise ValueError(f"Invalid --asof date: {args.asof}")
    cutoff = asof_obj - timedelta(days=max(1, lookback_years) * 366)
    ticker_filter = {normalize_ticker(x) for x in args.tickers.split(",") if normalize_ticker(x)}
    sqlite_timeout_sec = float(cfg_get(config, "runtime.sqlite_timeout_sec", 30.0))
    asof = asof_obj.isoformat()
    ttl_hours = float(cfg_get(config, "sec_companyfacts_history.ttl_hours", 24.0))
    sleep_sec = float(cfg_get(config, "sec_companyfacts_history.sleep_sec", 0.15))
    timeout_sec = float(cfg_get(config, "sec_companyfacts_history.timeout_sec", 45.0))
    max_retries = int(cfg_get(config, "sec_companyfacts_history.max_retries", 3))
    max_workers = max(1, int(cfg_get(config, "sec_companyfacts_history.max_workers", 4)))
    all_csv_rows: list[dict[str, Any]] = []

    with connect(db_path, timeout_sec=sqlite_timeout_sec) as conn:
        init_db(conn)
        scoring_tickers = read_scoring_tickers(universe_csv)
        companies = load_companies(
            conn,
            scoring_tickers=scoring_tickers,
            ticker_filter=ticker_filter,
            max_companies=int(args.max_companies),
        )
        subset_mode = subset_mode_enabled(ticker_filter=ticker_filter, max_count=int(args.max_companies))
        output_csv = subset_output_path(output_csv, subset_mode=subset_mode)
        validate_nonempty_selection(count=len(companies), context="SEC companyfacts sync", subset_mode=subset_mode)
        loaded_tickers = [company.ticker for company in companies]
        validate_requested_tickers(requested_tickers=ticker_filter, loaded_tickers=loaded_tickers, context="SEC companyfacts sync")
        validate_full_universe_coverage(
            expected_tickers=scoring_tickers,
            observed_tickers=loaded_tickers,
            context="SEC companyfacts sync",
            subset_mode=subset_mode,
        )
        run_id = start_run(conn, run_type="sync_sec_companyfacts_history", input_path=universe_csv)
        try:
            error_count = 0
            company_ids = [company.company_id for company in companies]
            sync_state_by_company = load_companyfacts_sync_state(conn, company_ids)
            fact_summary_by_company = load_companyfacts_fact_summary(conn, company_ids)
            latest_source_dates = load_latest_source_filing_dates(conn, company_ids)
            refresh_targets = [
                company
                for company in companies
                if should_refresh_company(
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
            if max_workers == 1:
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
                            latest_source_filing_date=latest_source_dates.get(company.company_id, ""),
                        )
                    )
            else:
                with ThreadPoolExecutor(max_workers=max_workers) as executor:
                    futures = {
                        executor.submit(
                            fetch_companyfacts_result,
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
                            latest_source_filing_date=latest_source_dates.get(company.company_id, ""),
                        ): company
                        for company in refresh_targets
                    }
                    for idx, future in enumerate(as_completed(futures), start=1):
                        results.append(future.result())
                        if idx % 25 == 0 or idx == len(futures):
                            LOGGER.info("Fetched SEC companyfacts for %d/%d targets", idx, len(futures))

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
            finish_run(
                conn,
                run_id=run_id,
                status="success" if error_count == 0 else "partial",
                row_count=len(all_csv_rows),
                message=f"companies={len(companies)} refreshed={len(refresh_targets)} errors={error_count} output={output_csv}",
            )
            if error_count > 0 and not args.allow_partial:
                raise SystemExit(2)
        except BaseException as exc:
            if not (isinstance(exc, SystemExit) and exc.code in (0, None)):
                finish_run(conn, run_id=run_id, status="failed", row_count=0, message=f"{type(exc).__name__}: {exc}")
            raise
    LOGGER.info("Synced SEC companyfacts history: companies=%d rows=%d output=%s", len(companies), len(all_csv_rows), output_csv)


if __name__ == "__main__":
    main()
