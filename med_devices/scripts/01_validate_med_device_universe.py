#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import logging
import math
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import requests


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from med_devices.core.config import cfg_get, load_yaml, resolve_path  # noqa: E402
from med_devices.core.logging_utils import configure_utc_logging  # noqa: E402
from med_devices.core.text_norm import normalize_cik, normalize_ticker  # noqa: E402


LOGGER = logging.getLogger("validate_med_device_universe")
DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
DEFAULT_INPUT = PROJECT_ROOT / "ticker_mapping" / "med_dev_tickers.csv"

DEFAULT_SEC_SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"
DEFAULT_SEC_COMPANYFACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"
DEFAULT_YAHOO_CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
DEFAULT_USER_AGENT = "JL, Independent Research, jm.357@hotmail.com"

DEFAULT_ACTIVE_LISTING_STATUSES = {"active"}
DEFAULT_INVESTABLE_SECURITY_TYPES = {"common stock", "ordinary shares", "adr/ads"}
DEFAULT_US_EXCHANGES = {
    "nasdaq",
    "nasd",
    "nyse",
    "new york stock exchange",
    "nyse american",
    "nyse mkt",
    "amex",
}
DEFAULT_RECENT_FORMS = {"10-K", "10-Q", "20-F", "40-F"}
DEFAULT_ANNUAL_FORMS = {"10-K", "20-F", "40-F"}
DEFAULT_QUARTERLY_FORMS = {"10-Q"}
DEFAULT_CORE_FACT_GROUPS = {
    "revenue": [
        "RevenueFromContractWithCustomerExcludingAssessedTax",
        "RevenueFromContractWithCustomerIncludingAssessedTax",
        "Revenues",
        "SalesRevenueNet",
    ],
    "assets": ["Assets"],
    "liabilities": ["Liabilities"],
    "cash": ["CashAndCashEquivalentsAtCarryingValue", "CashAndCashEquivalentsAtFairValue"],
    "net_income": ["NetIncomeLoss"],
    "operating_cash_flow": [
        "NetCashProvidedByUsedInOperatingActivities",
        "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations",
    ],
}
DEFAULT_SCORE_PENALTIES = {
    "missing_cik": 30,
    "non_common_security_type": 25,
    "inactive_or_unknown_listing": 35,
    "non_us_primary_exchange": 20,
    "non_us_country": 15,
    "non_usd_currency": 15,
    "too_few_recent_financial_filings": 20,
    "too_few_annual_filings": 15,
    "too_few_quarterly_filings": 15,
    "no_recent_financial_filing": 35,
    "stale_sec_financial_filing": 30,
    "insufficient_core_fact_coverage": 25,
    "too_few_companyfacts_observations": 20,
    "no_companyfacts_periods": 35,
    "stale_companyfacts": 25,
    "non_usd_companyfacts_units": 20,
    "too_few_recent_market_bars": 20,
    "no_recent_market_bar": 35,
    "stale_market_bar": 30,
    "missing_latest_close": 25,
    "low_avg_dollar_volume_60d": 25,
}
DEFAULT_HARD_EXCLUSION_REASONS = {
    "missing_cik",
    "non_common_security_type",
    "inactive_or_unknown_listing",
    "non_us_primary_exchange",
    "no_recent_financial_filing",
    "stale_sec_financial_filing",
    "insufficient_core_fact_coverage",
    "no_companyfacts_periods",
    "stale_companyfacts",
    "no_recent_market_bar",
    "stale_market_bar",
    "missing_latest_close",
    "low_avg_dollar_volume_60d",
}

FIELDNAMES = [
    "ticker",
    "company_name",
    "cik",
    "exchange",
    "security_type",
    "listing_status",
    "country",
    "currency",
    "identity_status",
    "sec_submissions_status",
    "companyfacts_status",
    "market_trading_status",
    "quality_score",
    "recommended_action",
    "review_reason",
    "latest_financial_filing_date",
    "latest_10k_date",
    "latest_10q_date",
    "recent_financial_filing_count",
    "annual_filing_count",
    "quarterly_filing_count",
    "companyfacts_core_groups_present",
    "companyfacts_total_observations",
    "companyfacts_first_period_end",
    "companyfacts_latest_period_end",
    "companyfacts_fact_years",
    "companyfacts_min_core_group_years",
    "companyfacts_missing_core_groups",
    "companyfacts_non_usd_groups",
    "calibration_bucket",
    "latest_market_bar_date",
    "latest_close",
    "avg_dollar_volume_60d",
    "trading_bar_count",
    "input_industry",
    "input_index",
]


@dataclass(frozen=True)
class ValidationPolicy:
    sec_submissions_url_template: str
    sec_companyfacts_url_template: str
    yahoo_chart_url_template: str
    yahoo_market_range: str
    yahoo_market_interval: str
    yahoo_market_events: str
    user_agent: str
    cache_ttl_hours: float
    http_timeout_sec: float
    request_sleep_sec: float
    include_us_listed_only: bool
    active_listing_statuses: set[str]
    investable_security_types: set[str]
    us_exchanges: set[str]
    recent_forms: set[str]
    annual_forms: set[str]
    quarterly_forms: set[str]
    core_fact_groups: dict[str, list[str]]
    companyfacts_currency_units: set[str]
    min_recent_financial_filings: int
    min_annual_filings: int
    min_quarterly_filings: int
    max_sec_filing_staleness_days: int
    min_core_fact_groups: int
    min_companyfacts_observations: int
    max_companyfacts_staleness_days: int
    max_market_staleness_days: int
    min_avg_dollar_volume_60d: float
    min_trading_bars: int
    calibration_core_min_fact_years: float
    calibration_core_min_core_group_years: float
    calibration_short_history_min_fact_years: float
    score_penalties: dict[str, int]
    default_unknown_reason_penalty: int
    hard_exclusion_reasons: set[str]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Validate the medical-device ticker universe for clean SEC financial history "
            "and active market trading status."
        )
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--input", type=Path, default=None)
    parser.add_argument("--output-csv", type=Path, default=None)
    parser.add_argument("--keep-csv", type=Path, default=None)
    parser.add_argument(
        "--clean-tickers-csv",
        type=Path,
        default=None,
        help="Same-schema copy of the input ticker CSV filtered to recommended_action=keep.",
    )
    parser.add_argument(
        "--calibration-core-csv",
        type=Path,
        default=None,
        help="Same-schema copy of the input ticker CSV filtered to keep + core calibration depth.",
    )
    parser.add_argument("--cache-dir", type=Path, default=None)
    parser.add_argument("--asof", type=str, default="", help="Validation date in YYYY-MM-DD. Defaults to today UTC.")
    parser.add_argument("--max-tickers", type=int, default=0, help="Smoke-test limit; 0 means all tickers.")
    parser.add_argument("--refresh-cache", action="store_true", help="Refresh cached SEC/Yahoo responses.")
    parser.add_argument("--cache-ttl-hours", type=float, default=None)
    parser.add_argument("--http-timeout-sec", type=float, default=None)
    parser.add_argument("--request-sleep-sec", type=float, default=None)
    parser.add_argument("--user-agent", default="")
    return parser.parse_args()


def parse_date(raw: object) -> date | None:
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        return datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError:
        return None


def asof_from_args(raw: str) -> date:
    if raw:
        parsed = parse_date(raw)
        if parsed is None:
            raise ValueError(f"Invalid --asof date, expected YYYY-MM-DD: {raw}")
        return parsed
    return datetime.now(timezone.utc).date()


def normalize_text(raw: object) -> str:
    return str(raw or "").strip()


def normalize_float(raw: object, default: float = 0.0) -> float:
    try:
        value = float(str(raw or "").replace(",", "").strip())
    except (TypeError, ValueError):
        return default
    return value if math.isfinite(value) else default


def read_csv_flexible(path: Path) -> list[dict[str, str]]:
    last_error: Exception | None = None
    for encoding in ("utf-8-sig", "utf-8", "cp1252"):
        try:
            with path.open("r", encoding=encoding, newline="") as handle:
                reader = csv.DictReader(handle)
                if reader.fieldnames is None:
                    raise ValueError(f"CSV has no header: {path}")
                return [{str(key): str(value or "") for key, value in row.items()} for row in reader]
        except UnicodeDecodeError as exc:
            last_error = exc
            continue
    raise ValueError(f"Could not decode CSV {path}: {last_error}")


def write_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def cache_is_fresh(path: Path, ttl_hours: float) -> bool:
    if not path.exists() or ttl_hours < 0:
        return False
    age_hours = (time.time() - path.stat().st_mtime) / 3600.0
    return age_hours <= ttl_hours


def fetch_json_cached(
    session: requests.Session,
    url: str,
    cache_path: Path,
    *,
    timeout_sec: float,
    refresh_cache: bool,
    cache_ttl_hours: float,
    headers: dict[str, str],
) -> tuple[dict[str, Any], str]:
    if not refresh_cache and cache_is_fresh(cache_path, cache_ttl_hours):
        try:
            return json.loads(cache_path.read_text(encoding="utf-8")), "cache"
        except json.JSONDecodeError:
            LOGGER.warning("Ignoring malformed JSON cache: %s", cache_path)
    response = session.get(url, timeout=timeout_sec, headers=headers)
    response.raise_for_status()
    payload = response.json()
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps(payload, ensure_ascii=True), encoding="utf-8")
    return payload, "network"


def sec_headers(user_agent: str) -> dict[str, str]:
    return {
        "User-Agent": user_agent,
        "Accept": "application/json,text/plain,*/*",
        "Accept-Encoding": "gzip, deflate",
    }


def str_set(raw: object, default: set[str], *, lower: bool = True) -> set[str]:
    values = raw if isinstance(raw, list) else list(default)
    out: set[str] = set()
    for value in values:
        text = str(value or "").strip()
        if not text:
            continue
        out.add(text.lower() if lower else text.upper())
    return out or set(default)


def int_map(raw: object, default: dict[str, int]) -> dict[str, int]:
    if not isinstance(raw, dict):
        return dict(default)
    out: dict[str, int] = {}
    for key, value in raw.items():
        try:
            out[str(key).strip()] = int(value)
        except (TypeError, ValueError):
            LOGGER.warning("Ignoring non-integer score penalty for reason=%s value=%r", key, value)
    return out or dict(default)


def concept_group_map(raw: object, default: dict[str, list[str]]) -> dict[str, list[str]]:
    if not isinstance(raw, dict):
        return {key: list(values) for key, values in default.items()}
    out: dict[str, list[str]] = {}
    for group, concepts in raw.items():
        if not isinstance(concepts, list):
            continue
        cleaned = [str(concept or "").strip() for concept in concepts if str(concept or "").strip()]
        if cleaned:
            out[str(group).strip()] = cleaned
    return out or {key: list(values) for key, values in default.items()}


def default_policy(config: dict[str, Any]) -> ValidationPolicy:
    return ValidationPolicy(
        sec_submissions_url_template=str(
            cfg_get(config, "universe_validation.sec_submissions_url_template", DEFAULT_SEC_SUBMISSIONS_URL)
            or DEFAULT_SEC_SUBMISSIONS_URL
        ),
        sec_companyfacts_url_template=str(
            cfg_get(config, "universe_validation.sec_companyfacts_url_template", DEFAULT_SEC_COMPANYFACTS_URL)
            or DEFAULT_SEC_COMPANYFACTS_URL
        ),
        yahoo_chart_url_template=str(
            cfg_get(config, "universe_validation.yahoo_chart_url_template", DEFAULT_YAHOO_CHART_URL)
            or DEFAULT_YAHOO_CHART_URL
        ),
        yahoo_market_range=str(cfg_get(config, "universe_validation.yahoo_market_range", "90d") or "90d"),
        yahoo_market_interval=str(cfg_get(config, "universe_validation.yahoo_market_interval", "1d") or "1d"),
        yahoo_market_events=str(cfg_get(config, "universe_validation.yahoo_market_events", "history") or "history"),
        user_agent=str(cfg_get(config, "universe_validation.user_agent", DEFAULT_USER_AGENT) or DEFAULT_USER_AGENT),
        cache_ttl_hours=float(cfg_get(config, "universe_validation.cache_ttl_hours", 24.0)),
        http_timeout_sec=float(cfg_get(config, "universe_validation.http_timeout_sec", 20.0)),
        request_sleep_sec=float(cfg_get(config, "universe_validation.request_sleep_sec", 0.1)),
        include_us_listed_only=bool(cfg_get(config, "med_devices_universe.include_us_listed_only", True)),
        active_listing_statuses=str_set(
            cfg_get(config, "universe_validation.active_listing_statuses", list(DEFAULT_ACTIVE_LISTING_STATUSES)),
            DEFAULT_ACTIVE_LISTING_STATUSES,
        ),
        investable_security_types=str_set(
            cfg_get(config, "universe_validation.investable_security_types", list(DEFAULT_INVESTABLE_SECURITY_TYPES)),
            DEFAULT_INVESTABLE_SECURITY_TYPES,
        ),
        us_exchanges=str_set(
            cfg_get(config, "universe_validation.us_exchanges", list(DEFAULT_US_EXCHANGES)),
            DEFAULT_US_EXCHANGES,
        ),
        recent_forms=str_set(
            cfg_get(config, "universe_validation.recent_forms", list(DEFAULT_RECENT_FORMS)),
            DEFAULT_RECENT_FORMS,
            lower=False,
        ),
        annual_forms=str_set(
            cfg_get(config, "universe_validation.annual_forms", list(DEFAULT_ANNUAL_FORMS)),
            DEFAULT_ANNUAL_FORMS,
            lower=False,
        ),
        quarterly_forms=str_set(
            cfg_get(config, "universe_validation.quarterly_forms", list(DEFAULT_QUARTERLY_FORMS)),
            DEFAULT_QUARTERLY_FORMS,
            lower=False,
        ),
        core_fact_groups=concept_group_map(
            cfg_get(config, "universe_validation.core_fact_groups", DEFAULT_CORE_FACT_GROUPS),
            DEFAULT_CORE_FACT_GROUPS,
        ),
        companyfacts_currency_units=str_set(
            cfg_get(config, "universe_validation.companyfacts_currency_units", ["USD"]),
            {"USD"},
            lower=False,
        ),
        min_recent_financial_filings=int(cfg_get(config, "universe_validation.min_recent_financial_filings", 4)),
        min_annual_filings=int(cfg_get(config, "universe_validation.min_annual_filings", 2)),
        min_quarterly_filings=int(cfg_get(config, "universe_validation.min_quarterly_filings", 3)),
        max_sec_filing_staleness_days=int(cfg_get(config, "universe_validation.max_sec_filing_staleness_days", 550)),
        min_core_fact_groups=int(cfg_get(config, "universe_validation.min_core_fact_groups", 5)),
        min_companyfacts_observations=int(cfg_get(config, "universe_validation.min_companyfacts_observations", 20)),
        max_companyfacts_staleness_days=int(cfg_get(config, "universe_validation.max_companyfacts_staleness_days", 550)),
        max_market_staleness_days=int(cfg_get(config, "universe_validation.max_market_staleness_days", 7)),
        min_avg_dollar_volume_60d=float(
            cfg_get(
                config,
                "universe_validation.min_avg_dollar_volume_60d",
                cfg_get(config, "med_devices_universe.avg_dollar_volume_60d_min", 1_000_000),
            )
        ),
        min_trading_bars=int(cfg_get(config, "universe_validation.min_trading_bars", 20)),
        calibration_core_min_fact_years=float(
            cfg_get(config, "universe_validation.calibration_core_min_fact_years", 7.0)
        ),
        calibration_core_min_core_group_years=float(
            cfg_get(config, "universe_validation.calibration_core_min_core_group_years", 5.0)
        ),
        calibration_short_history_min_fact_years=float(
            cfg_get(config, "universe_validation.calibration_short_history_min_fact_years", 3.0)
        ),
        score_penalties=int_map(
            cfg_get(config, "universe_validation.score_penalties", DEFAULT_SCORE_PENALTIES),
            DEFAULT_SCORE_PENALTIES,
        ),
        default_unknown_reason_penalty=int(cfg_get(config, "universe_validation.default_unknown_reason_penalty", 10)),
        hard_exclusion_reasons=str_set(
            cfg_get(config, "universe_validation.hard_exclusion_reasons", list(DEFAULT_HARD_EXCLUSION_REASONS)),
            DEFAULT_HARD_EXCLUSION_REASONS,
        ),
    )


def validate_identity(row: dict[str, str], policy: ValidationPolicy) -> tuple[str, list[str]]:
    reasons: list[str] = []
    cik = normalize_cik(row.get("CIK"))
    security_type = normalize_text(row.get("SecurityType")).lower()
    listing_status = normalize_text(row.get("ListingStatus")).lower()
    exchange = normalize_text(row.get("Exchange")).lower()
    country = normalize_text(row.get("Country")).lower()
    currency = normalize_text(row.get("Currency")).upper()

    if not cik:
        reasons.append("missing_cik")
    if security_type not in policy.investable_security_types:
        reasons.append(f"non_common_security_type:{security_type or 'blank'}")
    if listing_status not in policy.active_listing_statuses:
        reasons.append(f"inactive_or_unknown_listing:{listing_status or 'blank'}")
    if policy.include_us_listed_only and exchange not in policy.us_exchanges:
        reasons.append(f"non_us_primary_exchange:{exchange or 'blank'}")
    if policy.include_us_listed_only and country and country != "united states":
        reasons.append(f"non_us_country:{country}")
    if currency and currency != "USD":
        reasons.append(f"non_usd_currency:{currency}")
    return ("pass" if not reasons else "fail", reasons)


def filing_summary(submissions: dict[str, Any], asof_date: date, policy: ValidationPolicy) -> tuple[dict[str, Any], list[str]]:
    recent = submissions.get("filings", {}).get("recent", {})
    forms = recent.get("form", [])
    dates = recent.get("filingDate", [])
    if not isinstance(forms, list) or not isinstance(dates, list):
        return (
            {
                "latest_financial_filing_date": "",
                "latest_10k_date": "",
                "latest_10q_date": "",
                "recent_financial_filing_count": 0,
                "annual_filing_count": 0,
                "quarterly_filing_count": 0,
            },
            ["malformed_sec_submissions"],
        )
    financial_dates: list[tuple[str, date]] = []
    annual_dates: list[date] = []
    quarterly_dates: list[date] = []
    for form, raw_date in zip(forms, dates):
        form_text = str(form or "").strip().upper()
        parsed = parse_date(raw_date)
        if parsed is None or parsed > asof_date:
            continue
        if form_text in policy.recent_forms:
            financial_dates.append((form_text, parsed))
        if form_text in policy.annual_forms:
            annual_dates.append(parsed)
        if form_text in policy.quarterly_forms:
            quarterly_dates.append(parsed)

    latest_financial = max((item[1] for item in financial_dates), default=None)
    latest_annual = max(annual_dates, default=None)
    latest_quarterly = max(quarterly_dates, default=None)
    reasons: list[str] = []
    if len(financial_dates) < policy.min_recent_financial_filings:
        reasons.append("too_few_recent_financial_filings")
    if len(annual_dates) < policy.min_annual_filings:
        reasons.append("too_few_annual_filings")
    if len(quarterly_dates) < policy.min_quarterly_filings:
        reasons.append("too_few_quarterly_filings")
    if latest_financial is None:
        reasons.append("no_recent_financial_filing")
    elif (asof_date - latest_financial).days > policy.max_sec_filing_staleness_days:
        reasons.append("stale_sec_financial_filing")

    return (
        {
            "latest_financial_filing_date": latest_financial.isoformat() if latest_financial else "",
            "latest_10k_date": latest_annual.isoformat() if latest_annual else "",
            "latest_10q_date": latest_quarterly.isoformat() if latest_quarterly else "",
            "recent_financial_filing_count": len(financial_dates),
            "annual_filing_count": len(annual_dates),
            "quarterly_filing_count": len(quarterly_dates),
        },
        reasons,
    )


def concept_observations(companyfacts: dict[str, Any], concept: str) -> tuple[int, date | None, date | None, set[str]]:
    facts = companyfacts.get("facts", {}).get("us-gaap", {})
    payload = facts.get(concept, {}) if isinstance(facts, dict) else {}
    units = payload.get("units", {}) if isinstance(payload, dict) else {}
    if not isinstance(units, dict):
        return 0, None, None, set()
    total = 0
    earliest: date | None = None
    latest: date | None = None
    unit_names: set[str] = set()
    for unit, observations in units.items():
        if not isinstance(observations, list):
            continue
        unit_names.add(str(unit))
        for obs in observations:
            if not isinstance(obs, dict):
                continue
            end_date = parse_date(obs.get("end"))
            if end_date is None:
                continue
            total += 1
            if earliest is None or end_date < earliest:
                earliest = end_date
            if latest is None or end_date > latest:
                latest = end_date
    return total, earliest, latest, unit_names


def year_span(first_date: date | None, last_date: date | None) -> float:
    if first_date is None or last_date is None or last_date < first_date:
        return 0.0
    return round((last_date - first_date).days / 365.25, 2)


def calibration_bucket(fact_years: float, min_core_group_years: float, policy: ValidationPolicy) -> str:
    if (
        fact_years >= policy.calibration_core_min_fact_years
        and min_core_group_years >= policy.calibration_core_min_core_group_years
    ):
        return "core_calibration"
    if fact_years >= policy.calibration_short_history_min_fact_years:
        return "short_history"
    return "new_issue_watchlist"


def companyfacts_summary(companyfacts: dict[str, Any], asof_date: date, policy: ValidationPolicy) -> tuple[dict[str, Any], list[str]]:
    present_groups: list[str] = []
    missing_groups: list[str] = []
    non_usd_groups: list[str] = []
    total_observations = 0
    first_period_end: date | None = None
    latest_period_end: date | None = None
    group_year_spans: list[float] = []

    for group, concepts in policy.core_fact_groups.items():
        best_count = 0
        best_first: date | None = None
        best_latest: date | None = None
        best_units: set[str] = set()
        for concept in concepts:
            count, first, latest, units = concept_observations(companyfacts, concept)
            if count > best_count:
                best_count = count
                best_first = first
                best_latest = latest
                best_units = units
        if best_count > 0:
            present_groups.append(group)
            total_observations += best_count
            if best_first is not None and (first_period_end is None or best_first < first_period_end):
                first_period_end = best_first
            if best_latest is not None and (latest_period_end is None or best_latest > latest_period_end):
                latest_period_end = best_latest
            group_year_spans.append(year_span(best_first, best_latest))
            if (
                group != "shares"
                and best_units
                and not any(unit.upper() in policy.companyfacts_currency_units for unit in best_units)
            ):
                non_usd_groups.append(group)
        else:
            missing_groups.append(group)

    reasons: list[str] = []
    if len(present_groups) < policy.min_core_fact_groups:
        reasons.append("insufficient_core_fact_coverage")
    if total_observations < policy.min_companyfacts_observations:
        reasons.append("too_few_companyfacts_observations")
    if latest_period_end is None:
        reasons.append("no_companyfacts_periods")
    elif (asof_date - latest_period_end).days > policy.max_companyfacts_staleness_days:
        reasons.append("stale_companyfacts")
    if non_usd_groups:
        reasons.append("non_usd_companyfacts_units")

    return (
        {
            "companyfacts_core_groups_present": len(present_groups),
            "companyfacts_total_observations": total_observations,
            "companyfacts_first_period_end": first_period_end.isoformat() if first_period_end else "",
            "companyfacts_latest_period_end": latest_period_end.isoformat() if latest_period_end else "",
            "companyfacts_fact_years": year_span(first_period_end, latest_period_end),
            "companyfacts_min_core_group_years": min(group_year_spans) if group_year_spans else 0.0,
            "companyfacts_missing_core_groups": ";".join(missing_groups),
            "companyfacts_non_usd_groups": ";".join(non_usd_groups),
            "calibration_bucket": calibration_bucket(
                year_span(first_period_end, latest_period_end),
                min(group_year_spans) if group_year_spans else 0.0,
                policy,
            ),
        },
        reasons,
    )


def yahoo_symbol(ticker: str) -> str:
    return normalize_ticker(ticker).replace("-", ".")


def market_summary(chart: dict[str, Any], asof_date: date, policy: ValidationPolicy) -> tuple[dict[str, Any], list[str]]:
    result = chart.get("chart", {}).get("result", [])
    if not result:
        error = chart.get("chart", {}).get("error")
        return (
            {
                "latest_market_bar_date": "",
                "latest_close": "",
                "avg_dollar_volume_60d": "",
                "trading_bar_count": 0,
            },
            [f"missing_yahoo_chart:{error or 'no_result'}"],
        )
    payload = result[0]
    timestamps = payload.get("timestamp", [])
    indicators = payload.get("indicators", {}).get("quote", [])
    quote = indicators[0] if indicators else {}
    closes = quote.get("close", [])
    volumes = quote.get("volume", [])
    bars: list[tuple[date, float, float]] = []
    if not isinstance(timestamps, list) or not isinstance(closes, list) or not isinstance(volumes, list):
        timestamps = []
    for ts, close_raw, volume_raw in zip(timestamps, closes, volumes):
        if close_raw is None or volume_raw is None:
            continue
        close = normalize_float(close_raw)
        volume = normalize_float(volume_raw)
        if close <= 0.0 or volume < 0.0:
            continue
        try:
            bar_date = datetime.fromtimestamp(int(ts), tz=timezone.utc).date()
        except (OSError, OverflowError, ValueError, TypeError):
            continue
        if bar_date <= asof_date:
            bars.append((bar_date, close, volume))
    latest_bar = max((bar[0] for bar in bars), default=None)
    latest_close = next((bar[1] for bar in reversed(bars) if bar[0] == latest_bar), None) if latest_bar else None
    recent_bars = sorted(bars, key=lambda item: item[0])[-60:]
    avg_dollar_volume = (
        sum(close * volume for _, close, volume in recent_bars) / len(recent_bars)
        if recent_bars
        else 0.0
    )

    reasons: list[str] = []
    if len(bars) < policy.min_trading_bars:
        reasons.append("too_few_recent_market_bars")
    if latest_bar is None:
        reasons.append("no_recent_market_bar")
    elif (asof_date - latest_bar).days > policy.max_market_staleness_days:
        reasons.append("stale_market_bar")
    if latest_close is None or latest_close <= 0.0:
        reasons.append("missing_latest_close")
    if avg_dollar_volume < policy.min_avg_dollar_volume_60d:
        reasons.append("low_avg_dollar_volume_60d")
    return (
        {
            "latest_market_bar_date": latest_bar.isoformat() if latest_bar else "",
            "latest_close": round(latest_close, 6) if latest_close is not None else "",
            "avg_dollar_volume_60d": round(avg_dollar_volume, 2) if avg_dollar_volume else "",
            "trading_bar_count": len(bars),
        },
        reasons,
    )


def score_from_reasons(
    identity_reasons: list[str],
    submissions_reasons: list[str],
    facts_reasons: list[str],
    market_reasons: list[str],
    policy: ValidationPolicy,
) -> int:
    score = 100
    for reason in [*identity_reasons, *submissions_reasons, *facts_reasons, *market_reasons]:
        reason_key = reason.split(":", 1)[0]
        score -= policy.score_penalties.get(reason_key, policy.default_unknown_reason_penalty)
    return max(0, score)


def classify(
    identity_reasons: list[str],
    submissions_reasons: list[str],
    facts_reasons: list[str],
    market_reasons: list[str],
    policy: ValidationPolicy,
) -> str:
    all_reasons = [*identity_reasons, *submissions_reasons, *facts_reasons, *market_reasons]
    if not all_reasons:
        return "keep"
    if any(reason.split(":", 1)[0] in policy.hard_exclusion_reasons for reason in all_reasons):
        return "exclude"
    return "review"


def status_from_reasons(reasons: list[str]) -> str:
    return "pass" if not reasons else "fail"


def output_paths(config: dict[str, Any], config_path: Path, args: argparse.Namespace) -> tuple[Path, Path, Path, Path, Path]:
    base_dir = config_path.parent
    cache_dir = args.cache_dir.expanduser().resolve() if args.cache_dir else resolve_path(cfg_get(config, "paths.cache_dir"), base_dir=base_dir)
    output_csv = (
        args.output_csv.expanduser().resolve()
        if args.output_csv
        else resolve_path(
            cfg_get(config, "universe_validation.output_csv", "../output/med_devices_reports/med_device_universe_validation.csv"),
            base_dir=base_dir,
        )
    )
    keep_csv = (
        args.keep_csv.expanduser().resolve()
        if args.keep_csv
        else resolve_path(
            cfg_get(config, "universe_validation.keep_csv", "../output/med_devices_reports/med_device_tickers_keep.csv"),
            base_dir=base_dir,
        )
    )
    clean_tickers_csv = (
        args.clean_tickers_csv.expanduser().resolve()
        if args.clean_tickers_csv
        else resolve_path(
            cfg_get(config, "universe_validation.clean_tickers_csv", "../output/med_devices_reports/med_dev_tickers_clean_keep.csv"),
            base_dir=base_dir,
        )
    )
    calibration_core_csv = (
        args.calibration_core_csv.expanduser().resolve()
        if args.calibration_core_csv
        else resolve_path(
            cfg_get(config, "universe_validation.calibration_core_csv", "../output/med_devices_reports/med_dev_tickers_calibration_core.csv"),
            base_dir=base_dir,
        )
    )
    return output_csv, keep_csv, clean_tickers_csv, calibration_core_csv, cache_dir


def write_clean_input_rows(path: Path, input_rows: list[dict[str, str]], keep_tickers: set[str]) -> None:
    fieldnames = list(input_rows[0].keys()) if input_rows else []
    rows: list[dict[str, str]] = []
    for row in input_rows:
        if normalize_ticker(row.get("Name") or row.get("Ticker")) not in keep_tickers:
            continue
        out = dict(row)
        if "CIK" in out:
            out["CIK"] = normalize_cik(out.get("CIK"))
        rows.append(out)
    write_csv(path, rows, fieldnames)


def validate_one(
    session: requests.Session,
    row: dict[str, str],
    *,
    asof_date: date,
    policy: ValidationPolicy,
    cache_dir: Path,
    timeout_sec: float,
    refresh_cache: bool,
    cache_ttl_hours: float,
    user_agent: str,
) -> dict[str, Any]:
    ticker = normalize_ticker(row.get("Name") or row.get("Ticker"))
    cik = normalize_cik(row.get("CIK"))
    company_name = normalize_text(row.get("Company_Name") or row.get("CompanyName"))
    out: dict[str, Any] = {
        "ticker": ticker,
        "company_name": company_name,
        "cik": cik,
        "exchange": normalize_text(row.get("Exchange")),
        "security_type": normalize_text(row.get("SecurityType")),
        "listing_status": normalize_text(row.get("ListingStatus")),
        "country": normalize_text(row.get("Country")),
        "currency": normalize_text(row.get("Currency")),
        "input_industry": normalize_text(row.get("Industry")),
        "input_index": normalize_text(row.get("Index")),
    }

    identity_status, identity_reasons = validate_identity(row, policy)
    out["identity_status"] = identity_status

    submissions_reasons: list[str] = []
    facts_reasons: list[str] = []
    market_reasons: list[str] = []

    if cik:
        try:
            submissions, _ = fetch_json_cached(
                session,
                policy.sec_submissions_url_template.format(cik=cik),
                cache_dir / "sec_submissions" / f"CIK{cik}.json",
                timeout_sec=timeout_sec,
                refresh_cache=refresh_cache,
                cache_ttl_hours=cache_ttl_hours,
                headers=sec_headers(user_agent),
            )
            summary, submissions_reasons = filing_summary(submissions, asof_date, policy)
            out.update(summary)
        except Exception as exc:
            LOGGER.warning("SEC submissions validation failed for %s CIK%s: %s", ticker, cik, exc)
            submissions_reasons = ["sec_submissions_fetch_failed"]
        try:
            companyfacts, _ = fetch_json_cached(
                session,
                policy.sec_companyfacts_url_template.format(cik=cik),
                cache_dir / "sec_companyfacts" / f"CIK{cik}.json",
                timeout_sec=timeout_sec,
                refresh_cache=refresh_cache,
                cache_ttl_hours=cache_ttl_hours,
                headers=sec_headers(user_agent),
            )
            summary, facts_reasons = companyfacts_summary(companyfacts, asof_date, policy)
            out.update(summary)
        except Exception as exc:
            LOGGER.warning("SEC companyfacts validation failed for %s CIK%s: %s", ticker, cik, exc)
            facts_reasons = ["companyfacts_fetch_failed"]
    else:
        submissions_reasons = ["missing_cik"]
        facts_reasons = ["missing_cik"]

    try:
        chart, _ = fetch_json_cached(
            session,
            (
                policy.yahoo_chart_url_template.format(ticker=yahoo_symbol(ticker))
                + f"?range={policy.yahoo_market_range}&interval={policy.yahoo_market_interval}"
                + f"&events={policy.yahoo_market_events}"
            ),
            cache_dir / "yahoo_chart" / f"{ticker}.json",
            timeout_sec=timeout_sec,
            refresh_cache=refresh_cache,
            cache_ttl_hours=cache_ttl_hours,
            headers={"User-Agent": user_agent, "Accept": "application/json,text/plain,*/*"},
        )
        summary, market_reasons = market_summary(chart, asof_date, policy)
        out.update(summary)
    except Exception as exc:
        LOGGER.warning("Yahoo market validation failed for %s: %s", ticker, exc)
        market_reasons = ["market_fetch_failed"]

    out["sec_submissions_status"] = status_from_reasons(submissions_reasons)
    out["companyfacts_status"] = status_from_reasons(facts_reasons)
    out["market_trading_status"] = status_from_reasons(market_reasons)
    out["quality_score"] = score_from_reasons(identity_reasons, submissions_reasons, facts_reasons, market_reasons, policy)
    out["recommended_action"] = classify(identity_reasons, submissions_reasons, facts_reasons, market_reasons, policy)
    out["review_reason"] = ";".join([*identity_reasons, *submissions_reasons, *facts_reasons, *market_reasons])

    for field in FIELDNAMES:
        out.setdefault(field, "")
    return out


def main() -> None:
    configure_utc_logging()
    args = parse_args()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    asof_date = asof_from_args(str(args.asof or ""))
    policy = default_policy(config)
    output_csv, keep_csv, clean_tickers_csv, calibration_core_csv, cache_dir = output_paths(config, config_path, args)
    input_csv = (
        args.input.expanduser().resolve()
        if args.input
        else resolve_path(cfg_get(config, "universe_validation.input_csv", DEFAULT_INPUT), base_dir=config_path.parent)
    )
    timeout_sec = float(args.http_timeout_sec if args.http_timeout_sec is not None else policy.http_timeout_sec)
    cache_ttl_hours = float(args.cache_ttl_hours if args.cache_ttl_hours is not None else policy.cache_ttl_hours)
    request_sleep_sec = float(args.request_sleep_sec if args.request_sleep_sec is not None else policy.request_sleep_sec)
    user_agent = str(args.user_agent or policy.user_agent)

    rows = read_csv_flexible(input_csv)
    if args.max_tickers > 0:
        rows = rows[: int(args.max_tickers)]
    LOGGER.info("Loaded med-device universe rows=%d input=%s", len(rows), input_csv)

    validated: list[dict[str, Any]] = []
    session = requests.Session()
    for idx, row in enumerate(rows, start=1):
        result = validate_one(
            session,
            row,
            asof_date=asof_date,
            policy=policy,
            cache_dir=cache_dir / "universe_validation",
            timeout_sec=timeout_sec,
            refresh_cache=bool(args.refresh_cache),
            cache_ttl_hours=cache_ttl_hours,
            user_agent=user_agent,
        )
        validated.append(result)
        LOGGER.info(
            "[%d/%d] %s action=%s score=%s reasons=%s",
            idx,
            len(rows),
            result["ticker"],
            result["recommended_action"],
            result["quality_score"],
            result["review_reason"] or "none",
        )
        if request_sleep_sec > 0:
            time.sleep(request_sleep_sec)

    keep_rows = [row for row in validated if row.get("recommended_action") == "keep"]
    keep_tickers = {str(row.get("ticker") or "") for row in keep_rows}
    calibration_core_rows = [
        row
        for row in keep_rows
        if str(row.get("calibration_bucket") or "") == "core_calibration"
    ]
    calibration_core_tickers = {str(row.get("ticker") or "") for row in calibration_core_rows}
    write_csv(output_csv, validated, FIELDNAMES)
    write_csv(keep_csv, keep_rows, FIELDNAMES)
    write_clean_input_rows(clean_tickers_csv, rows, keep_tickers)
    write_clean_input_rows(calibration_core_csv, rows, calibration_core_tickers)
    action_counts: dict[str, int] = {}
    calibration_counts: dict[str, int] = {}
    for row in validated:
        action = str(row.get("recommended_action") or "")
        action_counts[action] = action_counts.get(action, 0) + 1
        if action == "keep":
            bucket = str(row.get("calibration_bucket") or "")
            calibration_counts[bucket] = calibration_counts.get(bucket, 0) + 1
    LOGGER.info("Wrote validation audit: %s", output_csv)
    LOGGER.info("Wrote keep-only universe: %s", keep_csv)
    LOGGER.info("Wrote clean keep ticker file: %s", clean_tickers_csv)
    LOGGER.info("Wrote calibration core ticker file: %s", calibration_core_csv)
    LOGGER.info(
        "Validation summary rows=%d keep=%d calibration_core=%d action_counts=%s calibration_counts=%s",
        len(validated),
        len(keep_rows),
        len(calibration_core_rows),
        action_counts,
        calibration_counts,
    )


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except BaseException as exc:
        configure_utc_logging()
        LOGGER.exception("Fatal med-device universe validation error: %s", exc)
        raise SystemExit(1) from exc
