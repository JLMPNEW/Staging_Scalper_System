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
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import requests  # type: ignore[reportMissingModuleSource]


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from technology.core.config import cfg_get, expand_env_vars, load_yaml, resolve_path  # noqa: E402
from technology.core.db import connect, finish_run, init_db, start_run, utc_now  # noqa: E402
from technology.core.logging_utils import configure_utc_logging  # noqa: E402
from technology.core.source_registry import load_source_registry, upsert_source_registry  # noqa: E402
from technology.core.text_norm import normalize_cik, normalize_ticker  # noqa: E402


LOGGER = logging.getLogger("sync_technology_sec_fundamentals")
DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
RUN_TYPE = "sync_technology_sec_fundamentals"
FILING_FORMS = {
    "10-K",
    "10-K/A",
    "10-Q",
    "10-Q/A",
    "20-F",
    "20-F/A",
    "40-F",
    "8-K",
    "8-K/A",
    "6-K",
    "6-K/A",
    "S-1",
    "S-1/A",
    "F-1",
    "F-1/A",
    "DEF 14A",
    "S-3",
    "S-3/A",
    "S-8",
    "424B2",
    "424B3",
    "424B5",
}
FACT_FORMS = {"10-K", "10-K/A", "10-Q", "10-Q/A", "20-F", "20-F/A", "40-F", "6-K", "6-K/A"}
ANNUAL_FORMS = {"10-K", "10-K/A", "20-F", "20-F/A", "40-F"}
QUARTERLY_FORMS = {"10-Q", "10-Q/A"}
CORE_OPERATING_METRICS = {"revenue", "assets"}
CONCEPT_METRICS: dict[str, str] = {
    "Revenues": "revenue",
    "RevenueFromContractWithCustomerExcludingAssessedTax": "revenue",
    "RevenueFromContractWithCustomerIncludingAssessedTax": "revenue",
    "SalesRevenueNet": "revenue",
    "SalesRevenueGoodsNet": "revenue",
    "GrossProfit": "gross_profit",
    "OperatingIncomeLoss": "operating_income",
    "NetIncomeLoss": "net_income",
    "EarningsPerShareDiluted": "eps_diluted",
    "Assets": "assets",
    "Liabilities": "liabilities",
    "StockholdersEquity": "equity",
    "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest": "equity",
    "CashAndCashEquivalentsAtCarryingValue": "cash_and_equivalents",
    "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents": "cash_and_equivalents",
    "InventoryNet": "inventory",
    "LongTermDebtNoncurrent": "debt_noncurrent",
    "LongTermDebtAndFinanceLeaseObligationsNoncurrent": "debt_noncurrent",
    "LongTermDebt": "debt_noncurrent",
    "ShortTermBorrowings": "debt_current",
    "ShortTermDebt": "debt_current",
    "CurrentPortionOfLongTermDebt": "debt_current",
    "NetCashProvidedByUsedInOperatingActivities": "operating_cash_flow",
    "PaymentsToAcquirePropertyPlantAndEquipment": "capex",
    "PaymentsToAcquireProductiveAssets": "capex",
    "ResearchAndDevelopmentExpense": "research_and_development",
    "ShareBasedCompensation": "stock_based_compensation",
    "ShareBasedCompensationArrangementByShareBasedPaymentAwardExpense": "stock_based_compensation",
    "WeightedAverageNumberOfDilutedSharesOutstanding": "diluted_shares",
    "WeightedAverageNumberOfShareDiluted": "diluted_shares",
}
CSV_FIELDS = [
    "ticker",
    "cik",
    "company_name",
    "submissions_status",
    "filings_upserted",
    "companyfacts_status",
    "facts_upserted",
    "mapped_facts_upserted",
    "coverage_status",
    "companyfacts_lag_status",
    "inline_fallback_status",
    "inline_fallback_mapped_facts",
    "latest_financial_filing_date",
    "submissions_payload_source",
    "companyfacts_payload_source",
    "submissions_cache_age_hours_before_refresh",
    "companyfacts_cache_age_hours_before_refresh",
    "sec_refresh_mode",
    "review_reason",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Sync SEC submissions and selected companyfacts for technology tickers.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--model-family", default="", help="Technology model family to sync, e.g. semiconductors.")
    parser.add_argument("--tickers", default="")
    parser.add_argument("--max-tickers", type=int, default=0)
    parser.add_argument("--force-refresh", action="store_true")
    parser.add_argument(
        "--force-submissions-refresh",
        action="store_true",
        help=(
            "Fetch each issuer's root SEC submissions index live while leaving "
            "companyfacts cache behavior unchanged."
        ),
    )
    parser.add_argument(
        "--asof",
        default="",
        help="Attribute the ingestion evidence to this production as-of date (YYYY-MM-DD).",
    )
    parser.add_argument(
        "--refresh-if-stale-hours",
        type=float,
        default=None,
        help="Refresh root SEC submissions/companyfacts caches older than this many hours; omitted preserves cache-only reuse.",
    )
    parser.add_argument("--allow-partial", action="store_true")
    parser.add_argument("--output-csv", type=Path, default=None)
    parser.add_argument(
        "--current-members-only",
        action="store_true",
        help="Exclude historical PIT members from a current production refresh.",
    )
    parser.add_argument(
        "--filing-index-only",
        action="store_true",
        help="Sync submissions filing metadata without reprocessing companyfacts.",
    )
    return parser.parse_args()


def parse_date(raw: object) -> date | None:
    text = str(raw or "").strip()[:10]
    if not text:
        return None
    for fmt in ("%Y-%m-%d", "%d-%b-%Y", "%d-%B-%Y"):
        try:
            return datetime.strptime(text.upper(), fmt).date()
        except ValueError:
            continue
    return None


def date_text(raw: object) -> str:
    parsed = parse_date(raw)
    return parsed.isoformat() if parsed else ""


def is_before(left: object, right: object) -> bool:
    left_date = parse_date(left)
    right_date = parse_date(right)
    return left_date is not None and right_date is not None and left_date < right_date


def to_float(raw: Any) -> float | None:
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def cache_age_hours(cache_path: Path, *, now: datetime | None = None) -> float | None:
    """Return cache age in UTC hours, or None when the cache is unavailable."""
    if not cache_path.exists():
        return None
    try:
        modified = datetime.fromtimestamp(cache_path.stat().st_mtime, tz=timezone.utc)
    except OSError:
        return None
    reference = now or datetime.now(timezone.utc)
    return max(0.0, (reference - modified).total_seconds() / 3600.0)


def cache_age_text(age_hours: float | None) -> str:
    return "" if age_hours is None else f"{age_hours:.2f}"


def cache_is_stale(cache_path: Path, refresh_if_stale_hours: float | None) -> bool:
    if refresh_if_stale_hours is None or refresh_if_stale_hours <= 0:
        return False
    age_hours = cache_age_hours(cache_path)
    return age_hours is not None and age_hours >= refresh_if_stale_hours


def cik10(raw: str) -> str:
    return normalize_cik(raw).zfill(10)


def load_universe(conn: Any, ticker_filter: set[str], *, model_family: str, include_historical: bool = False) -> list[dict[str, Any]]:
    # Historical (delisted/acquired) members carry universe_status='historical'
    # from the membership loader; their EDGAR fundamentals are backfillable even
    # though their price feeds are not, so they are opt-in for this sync only.
    if include_historical:
        rows = conn.execute(
            """
            SELECT DISTINCT c.ticker, c.cik, c.company_name, c.is_active
            FROM dim_company c
            JOIN dim_universe_membership m
              ON m.ticker = c.ticker
             AND m.model_family = ?
             AND (m.is_current_member = 1 OR m.point_in_time_flag = 1)
            ORDER BY c.ticker
            """,
            (model_family,),
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT DISTINCT c.ticker, c.cik, c.company_name, c.is_active
            FROM dim_company c
            JOIN dim_technology_taxonomy t
              ON t.ticker = c.ticker
             AND t.model_family = ?
            WHERE c.is_active = 1
            ORDER BY c.ticker
            """,
            (model_family,),
        ).fetchall()
    out: list[dict[str, Any]] = []
    for row in rows:
        ticker = normalize_ticker(row["ticker"])
        if ticker_filter and ticker not in ticker_filter:
            continue
        out.append(
            {
                "ticker": ticker,
                "cik": cik10(str(row["cik"] or "")),
                "company_name": str(row["company_name"] or ""),
                "historical": int(row["is_active"] or 0) == 0,
            }
        )
    return out


def request_json(url: str, *, headers: dict[str, str], timeout_sec: float, retries: int, sleep_sec: float) -> tuple[int, str, Any]:
    last_exc: Exception | None = None
    for attempt in range(max(1, retries)):
        try:
            response = requests.get(url, headers=headers, timeout=timeout_sec)
            if response.status_code in {429, 500, 502, 503, 504} and attempt + 1 < retries:
                time.sleep(sleep_sec * (attempt + 1))
                continue
            return response.status_code, response.text, response.json() if response.text else {}
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            if attempt + 1 < retries:
                time.sleep(sleep_sec * (attempt + 1))
                continue
    raise RuntimeError(f"Request failed for {url}: {last_exc}")


def cached_json(
    url: str,
    cache_path: Path,
    *,
    headers: dict[str, str],
    timeout_sec: float,
    retries: int,
    sleep_sec: float,
    force_refresh: bool,
    refresh_if_stale_hours: float | None = None,
) -> tuple[int, str, Any, str]:
    stale_cache = cache_is_stale(cache_path, refresh_if_stale_hours)
    if cache_path.exists() and not force_refresh and not stale_cache:
        try:
            text = cache_path.read_text(encoding="utf-8")
            return 200, text, json.loads(text), "cache"
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            LOGGER.warning("Ignoring unreadable SEC JSON cache %s: %s", cache_path, exc)
            stale_cache = True
    status, text, payload = request_json(url, headers=headers, timeout_sec=timeout_sec, retries=retries, sleep_sec=sleep_sec)
    # Only cache successful responses; a cached error body would replay as 200 forever.
    if status == 200:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(text, encoding="utf-8")
    time.sleep(sleep_sec)
    source = "live_stale" if stale_cache and not force_refresh else "live"
    return status, text, payload, source


def request_text(url: str, *, headers: dict[str, str], timeout_sec: float, retries: int, sleep_sec: float) -> tuple[int, str]:
    last_exc: Exception | None = None
    for attempt in range(max(1, retries)):
        try:
            response = requests.get(url, headers=headers, timeout=timeout_sec)
            if response.status_code in {429, 500, 502, 503, 504} and attempt + 1 < retries:
                time.sleep(sleep_sec * (attempt + 1))
                continue
            return response.status_code, response.text
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            if attempt + 1 < retries:
                time.sleep(sleep_sec * (attempt + 1))
                continue
    raise RuntimeError(f"Request failed for {url}: {last_exc}")


def cached_text(
    url: str,
    cache_path: Path,
    *,
    headers: dict[str, str],
    timeout_sec: float,
    retries: int,
    sleep_sec: float,
    force_refresh: bool,
    refresh_if_stale_hours: float | None = None,
) -> tuple[int, str, str]:
    stale_cache = cache_is_stale(cache_path, refresh_if_stale_hours)
    if cache_path.exists() and not force_refresh and not stale_cache:
        try:
            return 200, cache_path.read_text(encoding="utf-8", errors="replace"), "cache"
        except OSError as exc:
            LOGGER.warning("Ignoring unreadable SEC text cache %s: %s", cache_path, exc)
            stale_cache = True
    status, text = request_text(url, headers=headers, timeout_sec=timeout_sec, retries=retries, sleep_sec=sleep_sec)
    if status == 200:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(text, encoding="utf-8")
    time.sleep(sleep_sec)
    source = "live_stale" if stale_cache and not force_refresh else "live"
    return status, text, source


def record_raw_response(
    conn: Any,
    *,
    source_id: str,
    endpoint: str,
    status: int,
    text: str,
    ingestion_run_id: int | None,
    asof: str,
    query_params: dict[str, Any] | None = None,
) -> None:
    now = utc_now()
    digest = hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()
    conn.execute(
        """
        INSERT INTO raw_api_responses(
            source_id, endpoint, query_params_json, request_time_utc, response_status,
            response_hash, asof_date, payload_text, ingestion_run_id, created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            source_id,
            endpoint,
            json.dumps(query_params or {}, ensure_ascii=True, sort_keys=True),
            now,
            status,
            digest,
            asof,
            text,
            ingestion_run_id,
            now,
        ),
    )


def filing_records(payload: dict[str, Any]) -> list[dict[str, Any]]:
    recent = payload.get("filings", {}).get("recent", {})
    keys = list(recent.keys())
    count = max((len(recent.get(key) or []) for key in keys), default=0)
    rows: list[dict[str, Any]] = []
    for idx in range(count):
        row: dict[str, Any] = {}
        for key in keys:
            values = recent.get(key) or []
            row[key] = values[idx] if idx < len(values) else ""
        rows.append(row)
    return rows


def archive_file_names(payload: dict[str, Any]) -> list[str]:
    files = payload.get("filings", {}).get("files", [])
    if not isinstance(files, list):
        return []
    return [str(row.get("name") or "") for row in files if isinstance(row, dict) and str(row.get("name") or "").strip()]


def upsert_filings(conn: Any, ticker: str, cik: str, rows: list[dict[str, Any]], *, source_id: str, start: date) -> int:
    now = utc_now()
    count = 0
    for row in rows:
        form = str(row.get("form") or "").strip().upper()
        filing_date = parse_date(row.get("filingDate"))
        if form not in FILING_FORMS or filing_date is None or filing_date < start:
            continue
        accession = str(row.get("accessionNumber") or "").strip()
        if not accession:
            continue
        report_date = parse_date(row.get("reportDate"))
        fiscal_year = to_int_or_none(row.get("fy"))
        conn.execute(
            """
            INSERT INTO fact_sec_filing(
                ticker, cik, accession_number, source_id, form_type, filing_date, report_date,
                acceptance_datetime, primary_document, primary_doc_description, fiscal_year,
                fiscal_period, is_amendment, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(ticker, accession_number, source_id) DO UPDATE SET
                form_type = excluded.form_type,
                filing_date = excluded.filing_date,
                report_date = excluded.report_date,
                acceptance_datetime = excluded.acceptance_datetime,
                primary_document = excluded.primary_document,
                primary_doc_description = excluded.primary_doc_description,
                fiscal_year = excluded.fiscal_year,
                fiscal_period = excluded.fiscal_period,
                is_amendment = excluded.is_amendment,
                updated_at = excluded.updated_at
            """,
            (
                ticker,
                cik,
                accession,
                source_id,
                form,
                filing_date.isoformat(),
                report_date.isoformat() if report_date else "",
                str(row.get("acceptanceDateTime") or ""),
                str(row.get("primaryDocument") or ""),
                str(row.get("primaryDocDescription") or ""),
                fiscal_year,
                str(row.get("fp") or ""),
                int("/A" in form),
                now,
                now,
            ),
        )
        count += 1
    return count


def fact_key(*parts: object) -> str:
    return hashlib.sha256("|".join(str(part or "") for part in parts).encode("utf-8")).hexdigest()


def to_int_or_none(raw: object) -> int | None:
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return None


def infer_period_type(raw: dict[str, Any]) -> str:
    start = parse_date(raw.get("start"))
    end = parse_date(raw.get("end"))
    if start is not None and end is not None and start != end:
        return "duration"
    return "instant"


def load_concept_map(conn: Any) -> dict[tuple[str, str], list[dict[str, Any]]]:
    rows = conn.execute(
        """
        SELECT canonical_metric, taxonomy, concept, priority, period_type, unit_type, sign_policy
        FROM dim_xbrl_concept_map
        ORDER BY taxonomy, concept, priority, canonical_metric
        """
    ).fetchall()
    out: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in rows:
        key = (str(row["taxonomy"]), str(row["concept"]))
        out.setdefault(key, []).append(dict(row))
    return out


def apply_sign_policy(value: float, sign_policy: str) -> float:
    if sign_policy == "positive_abs":
        return abs(value)
    if sign_policy == "positive" and value < 0:
        return abs(value)
    return value


def upsert_reporting_profile(
    conn: Any,
    ticker: str,
    cik: str,
    *,
    source_id: str,
    taxonomies: set[str],
    mapped_by_taxonomy: dict[str, int],
    mapped_metrics: set[str],
) -> str:
    now = utc_now()
    db_taxonomy_rows = conn.execute(
        """
        SELECT DISTINCT taxonomy
        FROM fact_sec_xbrl_fact_raw
        WHERE ticker = ? AND source_id = ?
        """,
        (ticker, source_id),
    ).fetchall()
    db_mapped_rows = conn.execute(
        """
        SELECT taxonomy, COUNT(*) AS n
        FROM fact_sec_xbrl_fact
        WHERE ticker = ? AND source_id = ?
        GROUP BY taxonomy
        """,
        (ticker, source_id),
    ).fetchall()
    db_metric_rows = conn.execute(
        """
        SELECT DISTINCT metric_name
        FROM fact_sec_xbrl_fact
        WHERE ticker = ? AND source_id = ?
        """,
        (ticker, source_id),
    ).fetchall()
    taxonomies = set(taxonomies) | {str(row["taxonomy"] or "") for row in db_taxonomy_rows if str(row["taxonomy"] or "")}
    mapped_by_taxonomy = dict(mapped_by_taxonomy)
    for row in db_mapped_rows:
        taxonomy = str(row["taxonomy"] or "")
        if taxonomy:
            mapped_by_taxonomy[taxonomy] = max(mapped_by_taxonomy.get(taxonomy, 0), int(row["n"] or 0))
    mapped_metrics = set(mapped_metrics) | {str(row["metric_name"] or "") for row in db_metric_rows if str(row["metric_name"] or "")}
    annual_row = conn.execute(
        """
        SELECT form_type, MAX(filing_date) AS latest_date
        FROM fact_sec_filing
        WHERE ticker = ? AND form_type IN ('10-K', '10-K/A', '20-F', '20-F/A', '40-F')
        GROUP BY form_type
        ORDER BY latest_date DESC
        LIMIT 1
        """,
        (ticker,),
    ).fetchone()
    quarterly_row = conn.execute(
        """
        SELECT form_type, MAX(filing_date) AS latest_date
        FROM fact_sec_filing
        WHERE ticker = ? AND form_type IN ('10-Q', '10-Q/A')
        GROUP BY form_type
        ORDER BY latest_date DESC
        LIMIT 1
        """,
        (ticker,),
    ).fetchone()
    latest_row = conn.execute(
        """
        SELECT form_type, filing_date
        FROM fact_sec_filing
        WHERE ticker = ? AND form_type IN ('10-K', '10-K/A', '10-Q', '10-Q/A', '20-F', '20-F/A', '40-F')
        ORDER BY filing_date DESC
        LIMIT 1
        """,
        (ticker,),
    ).fetchone()
    latest_companyfacts_row = conn.execute(
        """
        SELECT MAX(filing_date) AS latest_date
        FROM fact_sec_xbrl_fact
        WHERE ticker = ? AND source_id = ?
          AND form_type IN ('10-K', '10-K/A', '10-Q', '10-Q/A', '20-F', '20-F/A', '40-F')
          AND COALESCE(filing_date, '') <> ''
        """,
        (ticker, source_id),
    ).fetchone()
    has_us_gaap = int("us-gaap" in taxonomies)
    has_ifrs = int("ifrs-full" in taxonomies)
    has_dei = int("dei" in taxonomies)
    has_operating = int(CORE_OPERATING_METRICS.issubset(mapped_metrics))
    primary_taxonomy = ""
    if mapped_by_taxonomy.get("us-gaap", 0) > 0:
        primary_taxonomy = "us-gaap"
    elif mapped_by_taxonomy.get("ifrs-full", 0) > 0:
        primary_taxonomy = "ifrs-full"
    elif taxonomies:
        primary_taxonomy = sorted(taxonomies)[0]
    primary_annual_form = str(annual_row["form_type"] or "") if annual_row is not None else ""
    primary_quarterly_form = str(quarterly_row["form_type"] or "") if quarterly_row is not None else ""
    latest_form = str(latest_row["form_type"] or "") if latest_row is not None else ""
    latest_date = str(latest_row["filing_date"] or "") if latest_row is not None else ""
    latest_companyfacts_date = str(latest_companyfacts_row["latest_date"] or "") if latest_companyfacts_row is not None else ""
    frequency = "quarterly_sec" if primary_quarterly_form else ("annual_sec" if primary_annual_form else "none")
    is_fpi = int(primary_annual_form.startswith(("20-F", "40-F")) or (has_ifrs and not has_us_gaap))
    lag_flag = int(bool(latest_date and (not latest_companyfacts_date or is_before(latest_companyfacts_date, latest_date))))
    lag_status = "current"
    if lag_flag and latest_form.startswith(("20-F", "40-F")):
        lag_status = "SEC_COMPANYFACTS_LAG_AFTER_20F"
    elif lag_flag:
        lag_status = "SEC_COMPANYFACTS_LAG_AFTER_LATEST_REGULAR_FILING"
    if has_operating and primary_taxonomy == "us-gaap":
        coverage_status = "SEC_OK_US_GAAP"
        reason = ""
    elif has_operating and primary_taxonomy == "ifrs-full":
        coverage_status = "SEC_OK_IFRS_FULL"
        reason = ""
    elif not primary_annual_form and not primary_quarterly_form:
        coverage_status = "SEC_NEW_ISSUER_INSUFFICIENT_FILINGS"
        reason = "No regular 10-K/10-Q/20-F operating filing found in SEC submissions."
    elif taxonomies:
        coverage_status = "SEC_METADATA_ONLY_NO_OPERATING_FACTS"
        reason = "Companyfacts payload exists but no mapped operating financial concept set was found."
    else:
        coverage_status = "SEC_REVIEW_REQUIRED"
        reason = "No usable companyfacts taxonomy payload found."
    review_parts = [reason] if reason else []
    if lag_flag:
        review_parts.append(
            f"{lag_status}: latest_regular_filing={latest_date or 'none'} latest_companyfacts_filing={latest_companyfacts_date or 'none'}"
        )
    calibration_eligible = int(has_operating)
    calibration_exclusion_reason = ""
    if not calibration_eligible:
        if coverage_status == "SEC_NEW_ISSUER_INSUFFICIENT_FILINGS":
            calibration_exclusion_reason = "insufficient_regular_sec_filings"
        elif not mapped_metrics:
            calibration_exclusion_reason = "missing_mapped_financial_statement_facts"
        else:
            calibration_exclusion_reason = coverage_status
    conn.execute(
        """
        INSERT INTO dim_issuer_reporting_profile(
            ticker, cik, source_id, primary_reporting_taxonomy, secondary_taxonomies,
            primary_annual_form, primary_quarterly_form, is_foreign_private_issuer,
            has_us_gaap_facts, has_ifrs_full_facts, has_dei_facts,
            has_operating_financial_facts, financial_statement_frequency,
            latest_operating_filing_date, latest_operating_form, latest_companyfacts_filing_date,
            companyfacts_lag_flag, companyfacts_lag_status, coverage_status,
            review_reason, calibration_fundamental_eligible, calibration_exclusion_reason,
            created_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(ticker) DO UPDATE SET
            cik = excluded.cik,
            source_id = excluded.source_id,
            primary_reporting_taxonomy = excluded.primary_reporting_taxonomy,
            secondary_taxonomies = excluded.secondary_taxonomies,
            primary_annual_form = excluded.primary_annual_form,
            primary_quarterly_form = excluded.primary_quarterly_form,
            is_foreign_private_issuer = excluded.is_foreign_private_issuer,
            has_us_gaap_facts = excluded.has_us_gaap_facts,
            has_ifrs_full_facts = excluded.has_ifrs_full_facts,
            has_dei_facts = excluded.has_dei_facts,
            has_operating_financial_facts = excluded.has_operating_financial_facts,
            financial_statement_frequency = excluded.financial_statement_frequency,
            latest_operating_filing_date = excluded.latest_operating_filing_date,
            latest_operating_form = excluded.latest_operating_form,
            latest_companyfacts_filing_date = excluded.latest_companyfacts_filing_date,
            companyfacts_lag_flag = excluded.companyfacts_lag_flag,
            companyfacts_lag_status = excluded.companyfacts_lag_status,
            coverage_status = excluded.coverage_status,
            review_reason = excluded.review_reason,
            calibration_fundamental_eligible = excluded.calibration_fundamental_eligible,
            calibration_exclusion_reason = excluded.calibration_exclusion_reason,
            updated_at = excluded.updated_at
        """,
        (
            ticker,
            cik,
            source_id,
            primary_taxonomy,
            json.dumps(sorted(taxonomies), ensure_ascii=True),
            primary_annual_form,
            primary_quarterly_form,
            is_fpi,
            has_us_gaap,
            has_ifrs,
            has_dei,
            has_operating,
            frequency,
            latest_date,
            latest_form,
            latest_companyfacts_date,
            lag_flag,
            lag_status,
            coverage_status,
            "; ".join(review_parts),
            calibration_eligible,
            calibration_exclusion_reason,
            now,
            now,
        ),
    )
    return coverage_status


def upsert_companyfacts(conn: Any, ticker: str, cik: str, payload: dict[str, Any], *, source_id: str, start: date, payload_hash: str) -> dict[str, Any]:
    facts = payload.get("facts", {})
    if not isinstance(facts, dict):
        return {"raw_count": 0, "mapped_count": 0, "coverage_status": "SEC_REVIEW_REQUIRED", "taxonomies": set(), "mapped_by_taxonomy": {}, "mapped_metrics": set()}
    now = utc_now()
    concept_map = load_concept_map(conn)
    raw_count = 0
    mapped_count = 0
    taxonomies: set[str] = set()
    mapped_by_taxonomy: dict[str, int] = {}
    mapped_metrics: set[str] = set()
    for taxonomy, concepts in facts.items():
        if not isinstance(concepts, dict):
            continue
        taxonomy = str(taxonomy or "").strip()
        if not taxonomy:
            continue
        taxonomies.add(taxonomy)
        for concept, concept_payload in concepts.items():
            if not isinstance(concept_payload, dict):
                continue
            units = concept_payload.get("units", {})
            if not isinstance(units, dict):
                continue
            for unit, fact_rows in units.items():
                if not isinstance(fact_rows, list):
                    continue
                for raw in fact_rows:
                    if not isinstance(raw, dict):
                        continue
                    end_date = parse_date(raw.get("end"))
                    filing_date = parse_date(raw.get("filed"))
                    if filing_date is not None and filing_date < start and (end_date is None or end_date < start):
                        continue
                    value = to_float(raw.get("val"))
                    if value is None:
                        continue
                    accession = str(raw.get("accn") or "").strip()
                    form = str(raw.get("form") or "").strip().upper()
                    key = fact_key(ticker, taxonomy, concept, unit, accession, raw.get("start"), raw.get("end"), raw.get("frame"))
                    period_type = infer_period_type(raw)
                    conn.execute(
                        """
                        INSERT INTO fact_sec_xbrl_fact_raw(
                            fact_key, ticker, cik, source_id, taxonomy, concept, unit, value,
                            start_date, end_date, fiscal_year, fiscal_period, form_type,
                            filing_date, accession_number, frame, period_type, source_detail,
                            source_accession_url, source_payload_hash,
                            created_at, updated_at
                        )
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(fact_key) DO UPDATE SET
                            value = excluded.value,
                            fiscal_year = excluded.fiscal_year,
                            fiscal_period = excluded.fiscal_period,
                            form_type = excluded.form_type,
                            filing_date = excluded.filing_date,
                            period_type = excluded.period_type,
                            source_detail = excluded.source_detail,
                            source_accession_url = excluded.source_accession_url,
                            source_payload_hash = excluded.source_payload_hash,
                            updated_at = excluded.updated_at
                        """,
                        (
                            key,
                            ticker,
                            cik,
                            source_id,
                            taxonomy,
                            str(concept or ""),
                            str(unit or ""),
                            value,
                            str(raw.get("start") or ""),
                            end_date.isoformat() if end_date else "",
                            to_int_or_none(raw.get("fy")),
                            str(raw.get("fp") or ""),
                            form,
                            filing_date.isoformat() if filing_date else "",
                            accession,
                            str(raw.get("frame") or ""),
                            period_type,
                            "companyfacts_api",
                            "",
                            payload_hash,
                            now,
                            now,
                        ),
                    )
                    raw_count += 1
                    if form not in FACT_FORMS or not accession or end_date is None:
                        continue
                    for mapping in concept_map.get((taxonomy, str(concept or "")), []):
                        metric = str(mapping["canonical_metric"])
                        mapped_value = apply_sign_policy(value, str(mapping["sign_policy"] or "as_reported"))
                        mapped_by_taxonomy[taxonomy] = mapped_by_taxonomy.get(taxonomy, 0) + 1
                        mapped_metrics.add(metric)
                        legacy_key = fact_key(ticker, taxonomy, concept, unit, accession, raw.get("start"), raw.get("end"), raw.get("frame"), metric)
                        conn.execute(
                            """
                            INSERT INTO fact_sec_xbrl_fact(
                                fact_key, ticker, cik, taxonomy, concept, metric_name, unit, accession_number,
                                source_id, form_type, filing_date, fiscal_year, fiscal_period, start_date,
                                end_date, frame, value, decimals, created_at, updated_at
                            )
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            ON CONFLICT(fact_key) DO UPDATE SET
                                metric_name = excluded.metric_name,
                                filing_date = excluded.filing_date,
                                fiscal_year = excluded.fiscal_year,
                                fiscal_period = excluded.fiscal_period,
                                value = excluded.value,
                                decimals = excluded.decimals,
                                updated_at = excluded.updated_at
                            """,
                            (
                                legacy_key,
                                ticker,
                                cik,
                                taxonomy,
                                str(concept or ""),
                                metric,
                                str(unit or ""),
                                accession,
                                source_id,
                                form,
                                filing_date.isoformat() if filing_date else "",
                                to_int_or_none(raw.get("fy")),
                                str(raw.get("fp") or ""),
                                str(raw.get("start") or ""),
                                end_date.isoformat(),
                                str(raw.get("frame") or ""),
                                mapped_value,
                                str(raw.get("decimals") or ""),
                                now,
                                now,
                            ),
                        )
                        mapped_count += 1
    coverage_status = upsert_reporting_profile(
        conn,
        ticker,
        cik,
        source_id=source_id,
        taxonomies=taxonomies,
        mapped_by_taxonomy=mapped_by_taxonomy,
        mapped_metrics=mapped_metrics,
    )
    return {
        "raw_count": raw_count,
        "mapped_count": mapped_count,
        "coverage_status": coverage_status,
        "taxonomies": taxonomies,
        "mapped_by_taxonomy": mapped_by_taxonomy,
        "mapped_metrics": mapped_metrics,
    }


ATTR_RE = re.compile(r"([A-Za-z_:][-A-Za-z0-9_:.]*)\s*=\s*(\"[^\"]*\"|'[^']*'|[^\s>]+)")
CONTEXT_RE = re.compile(r"<(?:[A-Za-z0-9_]+:)?context\b(?P<attrs>[^>]*)>(?P<body>.*?)</(?:[A-Za-z0-9_]+:)?context>", re.IGNORECASE | re.DOTALL)
UNIT_RE = re.compile(r"<(?:[A-Za-z0-9_]+:)?unit\b(?P<attrs>[^>]*)>(?P<body>.*?)</(?:[A-Za-z0-9_]+:)?unit>", re.IGNORECASE | re.DOTALL)
NON_FRACTION_RE = re.compile(r"<ix:nonfraction\b(?P<attrs>[^>]*)>(?P<body>.*?)</ix:nonfraction>", re.IGNORECASE | re.DOTALL)
TAG_RE = re.compile(r"<[^>]+>")
NUMBER_RE = re.compile(r"[-+]?\d+(?:\.\d+)?")


def parse_attrs(raw: str) -> dict[str, str]:
    attrs: dict[str, str] = {}
    for match in ATTR_RE.finditer(raw or ""):
        key = match.group(1)
        value = match.group(2).strip()
        if (value.startswith('"') and value.endswith('"')) or (value.startswith("'") and value.endswith("'")):
            value = value[1:-1]
        attrs[key] = html.unescape(value)
    return attrs


def lower_attrs(attrs: dict[str, str]) -> dict[str, str]:
    return {key.lower(): value for key, value in attrs.items()}


def clean_fact_text(raw_html: str) -> str:
    without_exclusions = re.sub(r"<ix:exclude\b.*?</ix:exclude>", "", raw_html or "", flags=re.IGNORECASE | re.DOTALL)
    text = TAG_RE.sub("", without_exclusions)
    return html.unescape(text).replace("\xa0", " ").strip()


def parse_contexts(document_text: str) -> dict[str, dict[str, Any]]:
    contexts: dict[str, dict[str, Any]] = {}
    for match in CONTEXT_RE.finditer(document_text):
        attrs = lower_attrs(parse_attrs(match.group("attrs")))
        context_id = attrs.get("id", "").strip()
        if not context_id:
            continue
        body = match.group("body")
        start_match = re.search(r"<(?:[A-Za-z0-9_]+:)?startdate[^>]*>(.*?)</(?:[A-Za-z0-9_]+:)?startdate>", body, re.IGNORECASE | re.DOTALL)
        end_match = re.search(r"<(?:[A-Za-z0-9_]+:)?enddate[^>]*>(.*?)</(?:[A-Za-z0-9_]+:)?enddate>", body, re.IGNORECASE | re.DOTALL)
        instant_match = re.search(r"<(?:[A-Za-z0-9_]+:)?instant[^>]*>(.*?)</(?:[A-Za-z0-9_]+:)?instant>", body, re.IGNORECASE | re.DOTALL)
        has_dimensions = bool(re.search(r"<(?:[A-Za-z0-9_]+:)?(?:explicitmember|typedmember)\b", body, re.IGNORECASE))
        start = clean_fact_text(start_match.group(1)) if start_match else ""
        end = clean_fact_text(end_match.group(1)) if end_match else ""
        instant = clean_fact_text(instant_match.group(1)) if instant_match else ""
        contexts[context_id] = {
            "start_date": date_text(start),
            "end_date": date_text(end or instant),
            "period_type": "duration" if start and end else "instant",
            "has_dimensions": has_dimensions,
        }
    return contexts


def parse_units(document_text: str) -> dict[str, str]:
    units: dict[str, str] = {}
    for match in UNIT_RE.finditer(document_text):
        attrs = lower_attrs(parse_attrs(match.group("attrs")))
        unit_id = attrs.get("id", "").strip()
        if not unit_id:
            continue
        body = match.group("body")
        measures = re.findall(r"<(?:[A-Za-z0-9_]+:)?measure[^>]*>(.*?)</(?:[A-Za-z0-9_]+:)?measure>", body, re.IGNORECASE | re.DOTALL)
        measure = clean_fact_text(measures[0]) if measures else unit_id
        measure_upper = measure.upper()
        if measure_upper.startswith("ISO4217:"):
            units[unit_id] = measure_upper.split(":", 1)[1]
        elif measure_upper.endswith(":SHARES") or measure_upper == "SHARES":
            units[unit_id] = "shares"
        elif measure_upper.endswith(":PURE") or measure_upper == "PURE":
            units[unit_id] = "pure"
        else:
            units[unit_id] = measure
    return units


def parse_inline_number(raw_html: str, attrs: dict[str, str]) -> float | None:
    attr_lc = lower_attrs(attrs)
    if attr_lc.get("xsi:nil", "").lower() in {"1", "true"}:
        return None
    text = clean_fact_text(raw_html)
    if not text or text in {"-", "--", "N/A", "na"}:
        return None
    negative = "(" in text and ")" in text
    normalized = text.replace(",", "").replace("$", "").replace("%", "").replace("(", "").replace(")", "").replace(" ", "")
    match = NUMBER_RE.search(normalized)
    if not match:
        return None
    try:
        value = float(match.group(0))
    except ValueError:
        return None
    if negative:
        value = -abs(value)
    if str(attr_lc.get("sign") or "").strip().startswith("-"):
        value = -abs(value)
    try:
        scale = int(str(attr_lc.get("scale") or "0"))
    except ValueError:
        scale = 0
    if scale:
        value *= 10 ** scale
    return value if math.isfinite(value) else None


def parse_inline_xbrl_facts(document_text: str, *, filing: dict[str, Any]) -> list[dict[str, Any]]:
    contexts = parse_contexts(document_text)
    units = parse_units(document_text)
    out: list[dict[str, Any]] = []
    form_type = str(filing.get("form_type") or "").strip().upper()
    accession = str(filing.get("accession_number") or "").strip()
    filing_date = str(filing.get("filing_date") or "")
    fallback_fy = to_int_or_none(filing.get("fiscal_year"))
    fallback_fp = str(filing.get("fiscal_period") or ("FY" if form_type in ANNUAL_FORMS else ""))
    for match in NON_FRACTION_RE.finditer(document_text):
        attrs = parse_attrs(match.group("attrs"))
        attr_lc = lower_attrs(attrs)
        name = str(attr_lc.get("name") or "").strip()
        context_ref = str(attr_lc.get("contextref") or "").strip()
        if ":" not in name or context_ref not in contexts:
            continue
        context = contexts[context_ref]
        if context.get("has_dimensions"):
            continue
        end_date = str(context.get("end_date") or "")
        if not end_date:
            continue
        unit_ref = str(attr_lc.get("unitref") or "").strip()
        value = parse_inline_number(match.group("body"), attrs)
        if value is None:
            continue
        taxonomy, concept = name.split(":", 1)
        parsed_end_date = parse_date(end_date)
        fy = fallback_fy or (parsed_end_date.year if parsed_end_date is not None else None)
        out.append(
            {
                "taxonomy": taxonomy,
                "concept": concept,
                "unit": units.get(unit_ref, unit_ref),
                "value": value,
                "start_date": str(context.get("start_date") or ""),
                "end_date": end_date,
                "period_type": str(context.get("period_type") or ""),
                "fiscal_year": fy,
                "fiscal_period": fallback_fp,
                "form_type": form_type,
                "filing_date": filing_date,
                "accession_number": accession,
                "frame": f"inline_context:{context_ref}",
                "decimals": str(attr_lc.get("decimals") or ""),
            }
        )
    return out


def latest_lagged_filing(conn: Any, ticker: str, fallback_forms: set[str]) -> dict[str, Any] | None:
    row = conn.execute(
        f"""
        SELECT accession_number, form_type, filing_date, report_date, primary_document,
               fiscal_year, fiscal_period
        FROM fact_sec_filing
        WHERE ticker = ?
          AND form_type IN ({",".join("?" for _ in sorted(fallback_forms))})
          AND COALESCE(primary_document, '') <> ''
        ORDER BY filing_date DESC, accession_number DESC
        LIMIT 1
        """,
        (ticker, *sorted(fallback_forms)),
    ).fetchone()
    return dict(row) if row is not None else None


def inline_document_url(cik: str, accession: str, primary_document: str) -> str:
    accession_no_dash = accession.replace("-", "")
    return f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{accession_no_dash}/{primary_document}"


def upsert_inline_facts(
    conn: Any,
    ticker: str,
    cik: str,
    facts: list[dict[str, Any]],
    *,
    source_id: str,
    source_detail: str,
    source_url: str,
    payload_hash: str,
) -> tuple[int, int, set[str], dict[str, int], set[str]]:
    now = utc_now()
    concept_map = load_concept_map(conn)
    raw_count = 0
    mapped_count = 0
    taxonomies: set[str] = set()
    mapped_by_taxonomy: dict[str, int] = {}
    mapped_metrics: set[str] = set()
    for raw in facts:
        taxonomy = str(raw.get("taxonomy") or "")
        concept = str(raw.get("concept") or "")
        unit = str(raw.get("unit") or "")
        value = to_float(raw.get("value"))
        end_date = parse_date(raw.get("end_date"))
        accession = str(raw.get("accession_number") or "")
        form = str(raw.get("form_type") or "").upper()
        if not taxonomy or not concept or value is None or end_date is None or not accession:
            continue
        taxonomies.add(taxonomy)
        key = fact_key(ticker, source_detail, taxonomy, concept, unit, accession, raw.get("start_date"), raw.get("end_date"), raw.get("frame"))
        conn.execute(
            """
            INSERT INTO fact_sec_xbrl_fact_raw(
                fact_key, ticker, cik, source_id, taxonomy, concept, unit, value,
                start_date, end_date, fiscal_year, fiscal_period, form_type,
                filing_date, accession_number, frame, period_type, source_detail,
                source_accession_url, source_payload_hash, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(fact_key) DO UPDATE SET
                value = excluded.value,
                fiscal_year = excluded.fiscal_year,
                fiscal_period = excluded.fiscal_period,
                form_type = excluded.form_type,
                filing_date = excluded.filing_date,
                period_type = excluded.period_type,
                source_detail = excluded.source_detail,
                source_accession_url = excluded.source_accession_url,
                source_payload_hash = excluded.source_payload_hash,
                updated_at = excluded.updated_at
            """,
            (
                key,
                ticker,
                cik,
                source_id,
                taxonomy,
                concept,
                unit,
                value,
                str(raw.get("start_date") or ""),
                end_date.isoformat(),
                raw.get("fiscal_year"),
                str(raw.get("fiscal_period") or ""),
                form,
                str(raw.get("filing_date") or ""),
                accession,
                str(raw.get("frame") or ""),
                str(raw.get("period_type") or ""),
                source_detail,
                source_url,
                payload_hash,
                now,
                now,
            ),
        )
        raw_count += 1
        if form not in FACT_FORMS:
            continue
        for mapping in concept_map.get((taxonomy, concept), []):
            metric = str(mapping["canonical_metric"])
            mapped_value = apply_sign_policy(value, str(mapping["sign_policy"] or "as_reported"))
            mapped_by_taxonomy[taxonomy] = mapped_by_taxonomy.get(taxonomy, 0) + 1
            mapped_metrics.add(metric)
            legacy_key = fact_key(ticker, source_detail, taxonomy, concept, unit, accession, raw.get("start_date"), raw.get("end_date"), raw.get("frame"), metric)
            conn.execute(
                """
                INSERT INTO fact_sec_xbrl_fact(
                    fact_key, ticker, cik, taxonomy, concept, metric_name, unit, accession_number,
                    source_id, form_type, filing_date, fiscal_year, fiscal_period, start_date,
                    end_date, frame, value, decimals, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(fact_key) DO UPDATE SET
                    metric_name = excluded.metric_name,
                    filing_date = excluded.filing_date,
                    fiscal_year = excluded.fiscal_year,
                    fiscal_period = excluded.fiscal_period,
                    value = excluded.value,
                    decimals = excluded.decimals,
                    updated_at = excluded.updated_at
                """,
                (
                    legacy_key,
                    ticker,
                    cik,
                    taxonomy,
                    concept,
                    metric,
                    unit,
                    accession,
                    source_id,
                    form,
                    str(raw.get("filing_date") or ""),
                    raw.get("fiscal_year"),
                    str(raw.get("fiscal_period") or ""),
                    str(raw.get("start_date") or ""),
                    end_date.isoformat(),
                    str(raw.get("frame") or ""),
                    mapped_value,
                    str(raw.get("decimals") or ""),
                    now,
                    now,
                ),
            )
            mapped_count += 1
    return raw_count, mapped_count, taxonomies, mapped_by_taxonomy, mapped_metrics


def maybe_run_inline_fallback(
    conn: Any,
    ticker: str,
    cik: str,
    *,
    source_id: str,
    cache_dir: Path,
    source_detail: str,
    fallback_forms: set[str],
    headers: dict[str, str],
    timeout_sec: float,
    retries: int,
    sleep_sec: float,
    force_refresh: bool,
    asof: str,
) -> dict[str, Any]:
    profile = conn.execute(
        """
        SELECT companyfacts_lag_flag, companyfacts_lag_status
        FROM dim_issuer_reporting_profile
        WHERE ticker = ?
        """,
        (ticker,),
    ).fetchone()
    if profile is None or int(profile["companyfacts_lag_flag"] or 0) == 0:
        return {"status": "not_needed", "raw_count": 0, "mapped_count": 0}
    filing = latest_lagged_filing(conn, ticker, fallback_forms)
    if filing is None:
        return {"status": "no_supported_filing", "raw_count": 0, "mapped_count": 0}
    url = inline_document_url(cik, str(filing["accession_number"]), str(filing["primary_document"]))
    cache_name = f"{cik}_{str(filing['accession_number']).replace('-', '')}_{str(filing['primary_document']).replace('/', '_')}"
    status, text, cache_status = cached_text(
        url,
        cache_dir / "inline_xbrl" / cache_name,
        headers=headers,
        timeout_sec=timeout_sec,
        retries=retries,
        sleep_sec=sleep_sec,
        force_refresh=force_refresh,
    )
    record_raw_response(
        conn,
        source_id=source_id,
        endpoint=url,
        status=status,
        text=text,
        ingestion_run_id=None,
        asof=asof,
        query_params={
            "payload_source": cache_status,
            "response_kind": "inline_xbrl_document",
        },
    )
    if status != 200:
        return {"status": f"fetch_failed_{status}", "raw_count": 0, "mapped_count": 0}
    payload_hash = hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()
    inline_facts = parse_inline_xbrl_facts(text, filing=filing)
    raw_count, mapped_count, taxonomies, mapped_by_taxonomy, mapped_metrics = upsert_inline_facts(
        conn,
        ticker,
        cik,
        inline_facts,
        source_id=source_id,
        source_detail=source_detail,
        source_url=url,
        payload_hash=payload_hash,
    )
    upsert_reporting_profile(
        conn,
        ticker,
        cik,
        source_id=source_id,
        taxonomies=taxonomies,
        mapped_by_taxonomy=mapped_by_taxonomy,
        mapped_metrics=mapped_metrics,
    )
    return {
        "status": "success" if mapped_count else "no_mapped_facts",
        "raw_count": raw_count,
        "mapped_count": mapped_count,
        "cache_status": cache_status,
        "url": url,
    }


def add_issue(conn: Any, ticker: str, source_id: str, issue_type: str, detail: str, severity: str = "warning") -> None:
    now = utc_now()
    row = conn.execute("SELECT company_id FROM dim_company WHERE ticker = ?", (ticker,)).fetchone()
    company_id = int(row["company_id"]) if row is not None else None
    conn.execute(
        """
        INSERT INTO data_quality_issues(
            detected_at, severity, stage, ticker, company_id, source_id, issue_type,
            issue_detail, resolution_status, created_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, ?)
        """,
        (now, severity, RUN_TYPE, ticker, company_id, source_id, issue_type, detail, now, now),
    )


def write_report(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS, extrasaction="ignore", lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    configure_utc_logging()
    args = parse_args()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    base_dir = config_path.parent
    sec_asof = date_text(args.asof) if args.asof else date.today().isoformat()
    if args.asof and sec_asof != str(args.asof).strip():
        raise ValueError("--asof must be a valid ISO date (YYYY-MM-DD)")
    db_path = args.db.expanduser().resolve() if args.db else resolve_path(cfg_get(config, "paths.database_path"), base_dir=base_dir)
    output_csv = args.output_csv.expanduser().resolve() if args.output_csv else resolve_path(cfg_get(config, "sec_fundamentals.report_output_csv"), base_dir=base_dir)
    registry_path = resolve_path(cfg_get(config, "source_registry.path"), base_dir=base_dir)
    cache_dir = resolve_path(cfg_get(config, "sec_fundamentals.cache_dir"), base_dir=base_dir)
    start = parse_date(cfg_get(config, "sec_fundamentals.start_date", "2015-01-01")) or date(2015, 1, 1)
    submissions_source = str(cfg_get(config, "sec_fundamentals.submissions_source_id", "sec_submissions"))
    companyfacts_source = str(cfg_get(config, "sec_fundamentals.companyfacts_source_id", "sec_companyfacts"))
    model_family = str(
        args.model_family
        or cfg_get(config, "technology_universe.initial_subsector", "semiconductors")
        or "semiconductors"
    ).strip()
    if not model_family:
        raise ValueError("model_family cannot be empty")
    user_agent = expand_env_vars(cfg_get(config, "sec_fundamentals.user_agent", "technology-research/1.0"))
    headers = {"User-Agent": user_agent, "Accept-Encoding": "gzip, deflate", "Host": "data.sec.gov"}
    timeout_sec = float(cfg_get(config, "sec_fundamentals.timeout_sec", 30.0))
    retries = int(cfg_get(config, "sec_fundamentals.max_retries", 3))
    sleep_sec = float(cfg_get(config, "sec_fundamentals.request_sleep_sec", 0.12))
    include_archives = str(cfg_get(config, "sec_fundamentals.include_submission_archives", True)).lower() in {"1", "true", "yes", "y"}
    inline_fallback_enabled = str(cfg_get(config, "sec_fundamentals.inline_xbrl_fallback_enabled", True)).lower() in {"1", "true", "yes", "y"}
    inline_source_detail = str(cfg_get(config, "sec_fundamentals.inline_xbrl_source_detail", "inline_xbrl_fallback"))
    refresh_if_stale_hours = args.refresh_if_stale_hours
    if refresh_if_stale_hours is not None and refresh_if_stale_hours < 0:
        raise ValueError("--refresh-if-stale-hours must be non-negative")
    sec_refresh_mode = (
        "force_all"
        if args.force_refresh
        else "live_submissions"
        if args.force_submissions_refresh
        else f"stale_if_older_than_{refresh_if_stale_hours:g}_hours"
        if refresh_if_stale_hours and refresh_if_stale_hours > 0
        else "cache_reuse"
    )
    inline_fallback_forms = {str(x).strip().upper() for x in cfg_get(config, "sec_fundamentals.inline_xbrl_fallback_forms", ["20-F", "20-F/A", "40-F"]) if str(x).strip()}
    archive_headers = {"User-Agent": user_agent, "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"}
    max_tickers = args.max_tickers or int(cfg_get(config, "sec_fundamentals.max_tickers_per_run", 0) or 0)
    ticker_filter = {normalize_ticker(x) for x in args.tickers.split(",") if normalize_ticker(x)}

    report_rows: list[dict[str, Any]] = []
    with connect(db_path, timeout_sec=float(cfg_get(config, "runtime.sqlite_timeout_sec", 30.0))) as conn:
        init_db(conn)
        upsert_source_registry(conn, load_source_registry(registry_path))
        run_id = start_run(conn, run_type=RUN_TYPE, input_path=config_path)
        try:
            include_historical = (
                not args.current_members_only
                and str(
                    cfg_get(
                        config,
                        "sec_fundamentals.include_historical_members",
                        True,
                    )
                ).strip().lower()
                in {"1", "true", "yes", "y"}
            )
            companies = load_universe(conn, ticker_filter, model_family=model_family, include_historical=include_historical)
            if max_tickers > 0:
                companies = companies[:max_tickers]
            if not companies:
                raise ValueError(f"No SEC universe tickers found for model_family={model_family}.")
            with conn:
                processed = [company["ticker"] for company in companies]
                if processed:
                    placeholders = ",".join("?" for _ in processed)
                    conn.execute(f"DELETE FROM data_quality_issues WHERE stage = ? AND ticker IN ({placeholders})", (RUN_TYPE, *processed))
            failures = 0
            for idx, company in enumerate(companies, start=1):
                ticker = company["ticker"]
                cik = company["cik"]
                if not normalize_cik(cik).strip("0"):
                    # Missing CIK is a review condition, not a fetch failure.
                    with conn:
                        add_issue(conn, ticker, submissions_source, "missing_cik_skipped_sec_sync", "Ticker has no CIK; SEC fundamentals sync skipped.")
                    report_rows.append(
                        {
                            "ticker": ticker,
                            "cik": "",
                            "company_name": company["company_name"],
                            "submissions_status": "skipped_missing_cik",
                            "filings_upserted": 0,
                            "companyfacts_status": "skipped_missing_cik",
                            "facts_upserted": 0,
                            "mapped_facts_upserted": 0,
                            "coverage_status": "SEC_REVIEW_REQUIRED",
                            "companyfacts_lag_status": "",
                            "inline_fallback_status": "not_needed",
                            "inline_fallback_mapped_facts": 0,
                            "latest_financial_filing_date": "",
                            "submissions_payload_source": "not_requested",
                            "companyfacts_payload_source": "not_requested",
                            "submissions_cache_age_hours_before_refresh": "",
                            "companyfacts_cache_age_hours_before_refresh": "",
                            "sec_refresh_mode": sec_refresh_mode,
                            "review_reason": "missing_cik",
                        }
                    )
                    continue
                reasons: list[str] = []
                filings_count = 0
                facts_count = 0
                mapped_facts_count = 0
                coverage_status = ""
                companyfacts_lag_status = ""
                inline_fallback_status = "not_needed"
                inline_fallback_mapped = 0
                sub_status = "success"
                facts_status = "success"
                latest_financial = ""
                submissions_payload_source = "not_requested"
                companyfacts_payload_source = "not_requested"
                submissions_cache_age_before_refresh = ""
                companyfacts_cache_age_before_refresh = ""
                try:
                    sub_url = str(cfg_get(config, "sec_fundamentals.submissions_url_template")).format(cik=cik)
                    sub_cache_path = cache_dir / "submissions" / f"CIK{cik}.json"
                    submissions_cache_age_before_refresh = cache_age_text(cache_age_hours(sub_cache_path))
                    status, text, payload, submissions_payload_source = cached_json(
                        sub_url,
                        sub_cache_path,
                        headers=headers,
                        timeout_sec=timeout_sec,
                        retries=retries,
                        sleep_sec=sleep_sec,
                        force_refresh=(
                            args.force_refresh or args.force_submissions_refresh
                        ),
                        refresh_if_stale_hours=refresh_if_stale_hours,
                    )
                    with conn:
                        record_raw_response(
                            conn,
                            source_id=submissions_source,
                            endpoint=sub_url,
                            status=status,
                            text=text,
                            ingestion_run_id=None,
                            asof=sec_asof,
                            query_params={
                                "payload_source": submissions_payload_source,
                                "response_kind": "root_submissions",
                            },
                        )
                        records = filing_records(payload)
                        if include_archives:
                            for file_name in archive_file_names(payload):
                                archive_url = str(cfg_get(config, "sec_fundamentals.submissions_archive_url_template")).format(file_name=file_name)
                                (
                                    archive_status,
                                    archive_text,
                                    archive_payload,
                                    archive_payload_source,
                                ) = cached_json(
                                    archive_url,
                                    cache_dir / "submissions" / file_name,
                                    headers=headers,
                                    timeout_sec=timeout_sec,
                                    retries=retries,
                                    sleep_sec=sleep_sec,
                                    force_refresh=args.force_refresh,
                                )
                                record_raw_response(
                                    conn,
                                    source_id=submissions_source,
                                    endpoint=archive_url,
                                    status=archive_status,
                                    text=archive_text,
                                    ingestion_run_id=None,
                                    asof=sec_asof,
                                    query_params={
                                        "payload_source": archive_payload_source,
                                        "response_kind": "submissions_archive",
                                    },
                                )
                                records.extend(filing_records({"filings": {"recent": archive_payload}}))
                        filings_count = upsert_filings(conn, ticker, cik, records, source_id=submissions_source, start=start)
                        latest_row = conn.execute(
                            """
                            SELECT MAX(filing_date) FROM fact_sec_filing
                            WHERE ticker = ? AND source_id = ? AND form_type IN ('10-K', '10-K/A', '10-Q', '10-Q/A', '20-F', '20-F/A', '40-F')
                            """,
                            (ticker, submissions_source),
                        ).fetchone()
                        latest_financial = str(latest_row[0] or "") if latest_row is not None else ""
                    if filings_count == 0:
                        reasons.append("no_financial_filings_since_start")
                        sub_status = "review"
                except Exception as exc:  # noqa: BLE001
                    if company.get("historical"):
                        # Deregistered issuers can drop off EDGAR's JSON APIs
                        # entirely; that is a data-availability fact, not a
                        # pipeline failure.
                        sub_status = "review"
                        reasons.append(f"historical_submissions_unavailable:{type(exc).__name__}")
                        with conn:
                            add_issue(conn, ticker, submissions_source, "historical_member_sec_data_unavailable", reasons[-1], "warning")
                    else:
                        failures += 1
                        sub_status = "failed"
                        reasons.append(f"submissions_error:{type(exc).__name__}:{exc}")
                        with conn:
                            add_issue(conn, ticker, submissions_source, "sec_submissions_fetch_failed", reasons[-1], "error")

                if args.filing_index_only:
                    with conn:
                        profile_row = conn.execute(
                            """
                            SELECT coverage_status, companyfacts_lag_status
                            FROM dim_issuer_reporting_profile
                            WHERE ticker = ?
                            """,
                            (ticker,),
                        ).fetchone()
                    if profile_row is not None:
                        coverage_status = str(profile_row["coverage_status"] or "")
                        companyfacts_lag_status = str(
                            profile_row["companyfacts_lag_status"] or ""
                        )
                    facts_status = "skipped_filing_index_only"
                    inline_fallback_status = "skipped_filing_index_only"
                    report_rows.append(
                        {
                            "ticker": ticker,
                            "cik": cik,
                            "company_name": company["company_name"],
                            "submissions_status": sub_status,
                            "filings_upserted": filings_count,
                            "companyfacts_status": facts_status,
                            "facts_upserted": 0,
                            "mapped_facts_upserted": 0,
                            "coverage_status": coverage_status,
                            "companyfacts_lag_status": companyfacts_lag_status,
                            "inline_fallback_status": inline_fallback_status,
                            "inline_fallback_mapped_facts": 0,
                            "latest_financial_filing_date": latest_financial,
                            "submissions_payload_source": submissions_payload_source,
                            "companyfacts_payload_source": companyfacts_payload_source,
                            "submissions_cache_age_hours_before_refresh": submissions_cache_age_before_refresh,
                            "companyfacts_cache_age_hours_before_refresh": companyfacts_cache_age_before_refresh,
                            "sec_refresh_mode": sec_refresh_mode,
                            "review_reason": ";".join(reasons),
                        }
                    )
                    LOGGER.info(
                        "[%d/%d] %s filings=%d status=%s filing_index_only",
                        idx,
                        len(companies),
                        ticker,
                        filings_count,
                        sub_status,
                    )
                    continue

                try:
                    facts_url = str(cfg_get(config, "sec_fundamentals.companyfacts_url_template")).format(cik=cik)
                    facts_cache_path = cache_dir / "companyfacts" / f"CIK{cik}.json"
                    companyfacts_cache_age_before_refresh = cache_age_text(cache_age_hours(facts_cache_path))
                    status, text, payload, companyfacts_payload_source = cached_json(
                        facts_url,
                        facts_cache_path,
                        headers=headers,
                        timeout_sec=timeout_sec,
                        retries=retries,
                        sleep_sec=sleep_sec,
                        force_refresh=args.force_refresh,
                        refresh_if_stale_hours=refresh_if_stale_hours,
                    )
                    with conn:
                        record_raw_response(
                            conn,
                            source_id=companyfacts_source,
                            endpoint=facts_url,
                            status=status,
                            text=text,
                            ingestion_run_id=None,
                            asof=sec_asof,
                            query_params={
                                "payload_source": companyfacts_payload_source,
                                "response_kind": "companyfacts",
                            },
                        )
                        payload_hash = hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()
                        fact_stats = upsert_companyfacts(conn, ticker, cik, payload, source_id=companyfacts_source, start=start, payload_hash=payload_hash)
                        facts_count = int(fact_stats["raw_count"])
                        mapped_facts_count = int(fact_stats["mapped_count"])
                        coverage_status = str(fact_stats["coverage_status"])
                        profile_row = conn.execute(
                            """
                            SELECT companyfacts_lag_flag, companyfacts_lag_status
                            FROM dim_issuer_reporting_profile
                            WHERE ticker = ?
                            """,
                            (ticker,),
                        ).fetchone()
                        companyfacts_lag_status = str(profile_row["companyfacts_lag_status"] or "") if profile_row is not None else ""
                        if profile_row is not None and int(profile_row["companyfacts_lag_flag"] or 0):
                            add_issue(
                                conn,
                                ticker,
                                companyfacts_source,
                                "sec_companyfacts_lag_after_latest_filing",
                                companyfacts_lag_status or "Companyfacts facts lag latest regular SEC filing.",
                                "warning",
                            )
                        if inline_fallback_enabled and profile_row is not None and int(profile_row["companyfacts_lag_flag"] or 0):
                            fallback_stats = maybe_run_inline_fallback(
                                conn,
                                ticker,
                                cik,
                                source_id=companyfacts_source,
                                cache_dir=cache_dir,
                                source_detail=inline_source_detail,
                                fallback_forms=inline_fallback_forms,
                                headers=archive_headers,
                                timeout_sec=timeout_sec,
                                retries=retries,
                                sleep_sec=sleep_sec,
                                force_refresh=args.force_refresh,
                                asof=sec_asof,
                            )
                            inline_fallback_status = str(fallback_stats.get("status") or "")
                            inline_fallback_mapped = int(fallback_stats.get("mapped_count") or 0)
                            facts_count += int(fallback_stats.get("raw_count") or 0)
                            mapped_facts_count += inline_fallback_mapped
                            profile_row = conn.execute(
                                """
                                SELECT coverage_status, companyfacts_lag_status
                                FROM dim_issuer_reporting_profile
                                WHERE ticker = ?
                                """,
                                (ticker,),
                            ).fetchone()
                            if profile_row is not None:
                                coverage_status = str(profile_row["coverage_status"] or coverage_status)
                                companyfacts_lag_status = str(profile_row["companyfacts_lag_status"] or "")
                            if inline_fallback_status != "success":
                                add_issue(
                                    conn,
                                    ticker,
                                    companyfacts_source,
                                    "inline_xbrl_fallback_no_mapped_facts",
                                    f"status={inline_fallback_status}; latest_lag_status={companyfacts_lag_status}",
                                    "warning",
                                )
                    if not coverage_status.startswith("SEC_OK"):
                        reasons.append(coverage_status or "no_mapped_operating_companyfacts_since_start")
                        facts_status = "review"
                        with conn:
                            add_issue(conn, ticker, companyfacts_source, "missing_mapped_operating_companyfacts", "No mapped operating financial companyfacts since configured start date.")
                except Exception as exc:  # noqa: BLE001
                    if company.get("historical"):
                        facts_status = "review"
                        reasons.append(f"historical_companyfacts_unavailable:{type(exc).__name__}")
                        with conn:
                            add_issue(conn, ticker, companyfacts_source, "historical_member_sec_data_unavailable", reasons[-1], "warning")
                    else:
                        failures += 1
                        facts_status = "failed"
                        reasons.append(f"companyfacts_error:{type(exc).__name__}:{exc}")
                        with conn:
                            add_issue(conn, ticker, companyfacts_source, "sec_companyfacts_fetch_failed", reasons[-1], "error")

                report_rows.append(
                    {
                        "ticker": ticker,
                        "cik": cik,
                        "company_name": company["company_name"],
                        "submissions_status": sub_status,
                        "filings_upserted": filings_count,
                        "companyfacts_status": facts_status,
                        "facts_upserted": facts_count,
                        "mapped_facts_upserted": mapped_facts_count,
                        "coverage_status": coverage_status,
                        "companyfacts_lag_status": companyfacts_lag_status,
                        "inline_fallback_status": inline_fallback_status,
                        "inline_fallback_mapped_facts": inline_fallback_mapped,
                        "latest_financial_filing_date": latest_financial,
                        "submissions_payload_source": submissions_payload_source,
                        "companyfacts_payload_source": companyfacts_payload_source,
                        "submissions_cache_age_hours_before_refresh": submissions_cache_age_before_refresh,
                        "companyfacts_cache_age_hours_before_refresh": companyfacts_cache_age_before_refresh,
                        "sec_refresh_mode": sec_refresh_mode,
                        "review_reason": ";".join(reasons),
                    }
                )
                LOGGER.info(
                    "[%d/%d] %s filings=%d raw_facts=%d mapped_facts=%d status=%s/%s coverage=%s",
                    idx,
                    len(companies),
                    ticker,
                    filings_count,
                    facts_count,
                    mapped_facts_count,
                    sub_status,
                    facts_status,
                    coverage_status,
                )
            write_report(output_csv, report_rows)
            status = "success" if failures == 0 else ("partial" if args.allow_partial else "failed")
            finish_run(conn, run_id=run_id, status=status, row_count=sum(int(r["mapped_facts_upserted"]) for r in report_rows), message=f"tickers={len(report_rows)} failures={failures} output={output_csv}")
            LOGGER.info("Wrote SEC fundamentals coverage report: %s", output_csv)
            LOGGER.info("SEC fundamentals sync complete: tickers=%d failures=%d", len(report_rows), failures)
            if failures and not args.allow_partial:
                raise SystemExit(1)
        except BaseException as exc:
            if not isinstance(exc, SystemExit):
                finish_run(conn, run_id=run_id, status="failed", row_count=0, message=f"{type(exc).__name__}: {exc}")
            raise


if __name__ == "__main__":
    main()
