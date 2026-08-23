#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import logging
import math
import re
import sqlite3
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from biotech_index.core.config import cfg_get, load_yaml, resolve_path
from biotech_index.core.db import connect, finish_run, init_db, start_run, utc_now
from biotech_index.core.financial_survival import cash_runway_is_reliable, proxy_field_names
from biotech_index.core.logging_utils import configure_utc_logging
from biotech_index.core.pipeline_guards import (
    normalize_ticker,
    read_final_scoring_tickers,
    subset_mode_enabled,
    subset_output_path,
    validate_full_universe_coverage,
    validate_nonempty_selection,
    validate_output_coverage,
    validate_requested_tickers,
)
from biotech_index.core.report_inputs import resolve_dated_report_input_csv
from biotech_index.core.security_identity import identity_start_dates_by_company, load_security_identity_rules


LOGGER = logging.getLogger("build_financial_survival_features")
DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
SQLITE_PARAM_CHUNK_SIZE = 800
QUARTER_PERIODS = {"Q1", "Q2", "Q3", "Q4"}
FISCAL_QUARTER_NUMBER = {"Q1": 1, "Q2": 2, "Q3": 3, "Q4": 4, "FY": 4}
CUMULATIVE_CASH_FLOW_FORMS = {"10-Q", "10-Q/A", "10-K", "10-K/A"}


def chunked(values: list[Any] | tuple[Any, ...], size: int = SQLITE_PARAM_CHUNK_SIZE) -> list[list[Any]]:
    step = max(1, int(size))
    return [list(values[start : start + step]) for start in range(0, len(values), step)]


SURVIVAL_FIELDS = [
    "asof_date",
    "company_id",
    "ticker",
    "company_name",
    "latest_period_end",
    "cash_and_investments",
    "quarterly_cash_burn",
    "ttm_cash_burn",
    "operating_cash_flow_ttm",
    "rd_expense_ttm",
    "sgna_expense_ttm",
    "cash_runway_months",
    "cash_runway_reliable_flag",
    "working_capital",
    "working_capital_ratio",
    "debt_to_cash",
    "cash_qoq_change_pct",
    "cash_yoy_change_pct",
    "rd_qoq_change_pct",
    "rd_yoy_change_pct",
    "negative_cash_flag",
    "burn_acceleration_flag",
    "short_runway_flag",
    "severe_runway_flag",
    "atm_facility_active",
    "recent_offering_count_12m",
    "shelf_registration_active",
    "dilution_pressure_score",
    "going_concern_status",
    "late_filing_count_12m",
    "financial_survival_score",
    "data_quality",
    "missing_fields",
    "proxy_fields_used",
    "payload_json",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build financial survival features from normalized SEC companyfacts.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--asof", type=str, default="", help="Feature date in YYYY-MM-DD. Defaults to UTC today.")
    parser.add_argument("--max-companies", type=int, default=0, help="Smoke-test limit. 0 means all.")
    parser.add_argument("--tickers", type=str, default="", help="Optional comma-separated ticker subset.")
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
    if raw is None:
        return None
    try:
        value = float(str(raw).strip())
    except (TypeError, ValueError):
        return None
    if math.isnan(value) or math.isinf(value):
        return None
    return value


def to_int(raw: object, default: int = 0) -> int:
    value = to_float(raw)
    return default if value is None else int(round(value))


def pct_change(current: float | None, previous: float | None) -> float | None:
    if current is None or previous is None or previous == 0:
        return None
    return (current - previous) / abs(previous)


def clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return low
    if not math.isfinite(parsed):
        return low
    return max(low, min(high, parsed))


def as_bool(raw: object) -> bool:
    return str(raw or "").strip().lower() in {"1", "true", "yes", "y"}


def read_screen_rows(path: Path) -> dict[str, dict[str, str]]:
    if not path.exists():
        LOGGER.warning("Financial survival screen CSV is missing; screen-derived fields will be blank: %s", path)
        return {}
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        return {str(row.get("ticker") or "").strip().upper(): {str(k): str(v or "") for k, v in row.items()} for row in reader}


SCREEN_ASOF_FIELDS = ("asof_date", "source_snapshot_asof_date", "snapshot_date")
PERIODIC_GOING_CONCERN_FORMS = frozenset({"10-K", "10-Q", "20-F", "40-F"})


def screen_rows_for_asof(
    path: Path,
    *,
    asof_date: date,
    current_date: date | None = None,
) -> dict[str, dict[str, str]]:
    """Return only screener rows whose provenance is valid at the requested date."""
    rows = read_screen_rows(path)
    today = current_date or datetime.now(timezone.utc).date()
    if asof_date >= today:
        return rows
    filtered: dict[str, dict[str, str]] = {}
    for ticker, row in rows.items():
        row_asof = next(
            (
                parsed
                for field in SCREEN_ASOF_FIELDS
                if (parsed := parse_date(row.get(field))) is not None
            ),
            None,
        )
        if row_asof is not None and row_asof <= asof_date:
            filtered[ticker] = row
    if rows and len(filtered) != len(rows):
        LOGGER.info(
            "Excluded %d undated/future screener rows for historical financial-survival asof=%s",
            len(rows) - len(filtered),
            asof_date.isoformat(),
        )
    return filtered


def going_concern_status_for_form(form: object) -> str:
    base_form = str(form or "").strip().upper().removesuffix("/A")
    return "confirmed" if base_form in PERIODIC_GOING_CONCERN_FORMS else "possible"


def read_scoring_tickers(path: Path) -> set[str]:
    return read_final_scoring_tickers(path)


def load_companies(
    conn: sqlite3.Connection,
    *,
    scoring_tickers: set[str],
    ticker_filter: set[str],
    max_companies: int,
) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT company_id, ticker, company_name
        FROM companies
        WHERE is_active = 1
           OR (universe_status = 'delisted_calibration' AND ticker IN (
                SELECT value FROM json_each(?)
           ))
        ORDER BY ticker
        """
        ,
        (json.dumps(sorted(scoring_tickers)),),
    ).fetchall()
    out: list[dict[str, Any]] = []
    for row in rows:
        ticker = normalize_ticker(row["ticker"])
        if scoring_tickers and ticker not in scoring_tickers:
            continue
        if ticker_filter and ticker not in ticker_filter:
            continue
        out.append(dict(row))
        if max_companies > 0 and len(out) >= max_companies:
            break
    return out


def load_fact_rows(
    conn: sqlite3.Connection,
    company_id: int,
    asof_date: date,
    *,
    identity_start_date: date | None = None,
) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT *
        FROM company_facts_quarterly
        WHERE company_id = ?
          AND period_end <= ?
          AND (
                (filed_date IS NOT NULL AND filed_date != '' AND filed_date <= ?)
                -- Rows without a filed_date could leak look-ahead data into historical
                -- rebuilds; only admit them once a typical filing lag has elapsed.
                OR ((filed_date IS NULL OR filed_date = '') AND date(period_end, '+45 days') <= ?)
          )
        ORDER BY period_end DESC, filed_date DESC
        """,
        (company_id, asof_date.isoformat(), asof_date.isoformat(), asof_date.isoformat()),
    ).fetchall()
    return [
        dict(row)
        for row in rows
        if identity_start_date is None
        or ((period_end := parse_date(row["period_end"])) is not None and period_end >= identity_start_date)
    ]


def load_fact_rows_bulk(
    conn: sqlite3.Connection,
    company_ids: list[int],
    asof_date: date,
    *,
    identity_start_dates: dict[int, date] | None = None,
) -> dict[int, list[dict[str, Any]]]:
    if not company_ids:
        return {}
    if len(company_ids) > SQLITE_PARAM_CHUNK_SIZE:
        out: dict[int, list[dict[str, Any]]] = {int(company_id): [] for company_id in company_ids}
        for company_chunk in chunked(company_ids):
            out.update(
                load_fact_rows_bulk(
                    conn,
                    [int(value) for value in company_chunk],
                    asof_date,
                    identity_start_dates=identity_start_dates,
                )
            )
        return out
    placeholders = ",".join("?" for _ in company_ids)
    rows = conn.execute(
        f"""
        SELECT *
        FROM company_facts_quarterly
        WHERE company_id IN ({placeholders})
          AND period_end <= ?
          AND (
                (filed_date IS NOT NULL AND filed_date != '' AND filed_date <= ?)
                -- Rows without a filed_date could leak look-ahead data into historical
                -- rebuilds; only admit them once a typical filing lag has elapsed.
                OR ((filed_date IS NULL OR filed_date = '') AND date(period_end, '+45 days') <= ?)
          )
        ORDER BY company_id, period_end DESC, filed_date DESC
        """,
        tuple(company_ids) + (asof_date.isoformat(), asof_date.isoformat(), asof_date.isoformat()),
    ).fetchall()
    out: dict[int, list[dict[str, Any]]] = {company_id: [] for company_id in company_ids}
    floors = identity_start_dates or {}
    for row in rows:
        company_id = int(row["company_id"])
        period_end = parse_date(row["period_end"])
        identity_start = floors.get(company_id)
        if identity_start is not None and (period_end is None or period_end < identity_start):
            continue
        out.setdefault(company_id, []).append(dict(row))
    for company_id, company_rows in out.items():
        out[company_id] = dedup_fact_rows(company_rows)
    return out


def dedup_fact_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    # Key intentionally excludes form so an amendment (e.g. 10-Q/A) supersedes the
    # original filing instead of both surviving and double-counting in TTM sums.
    # Rows arrive ordered period_end DESC, filed_date DESC, so the first row seen
    # per (period_end, fiscal_period) is the latest-filed one.
    seen: set[tuple[str, str]] = set()
    out: list[dict[str, Any]] = []
    for row in rows:
        key = (
            str(row.get("period_end") or ""),
            str(row.get("fiscal_period") or ""),
        )
        if key in seen:
            continue
        seen.add(key)
        out.append(row)
    return out


def latest_nonnull(rows: list[dict[str, Any]], field: str) -> tuple[float | None, dict[str, Any] | None]:
    for row in rows:
        value = to_float(row.get(field))
        if value is not None:
            return value, row
    return None, None


def rows_after_marker(rows: list[dict[str, Any]], marker: dict[str, Any] | None) -> list[dict[str, Any]]:
    if marker is None:
        return rows
    for idx, row in enumerate(rows):
        if row is marker:
            return rows[idx + 1 :]
    marker_key = (
        str(marker.get("period_end") or ""),
        str(marker.get("fiscal_period") or ""),
        str(marker.get("form") or ""),
    )
    for idx, row in enumerate(rows):
        row_key = (
            str(row.get("period_end") or ""),
            str(row.get("fiscal_period") or ""),
            str(row.get("form") or ""),
        )
        if row_key == marker_key:
            return rows[idx + 1 :]
    return rows


def closest_prior_value(rows: list[dict[str, Any]], field: str, target_date: date, min_days: int, max_days: int) -> float | None:
    best: tuple[int, float] | None = None
    for row in rows:
        period_end = parse_date(row.get("period_end"))
        value = to_float(row.get(field))
        if period_end is None or value is None:
            continue
        age = (target_date - period_end).days
        if min_days <= age <= max_days and (best is None or age < best[0]):
            best = (age, value)
    return best[1] if best else None


def amount_for_period(row: dict[str, Any], field: str, proxies: list[str]) -> float | None:
    value = to_float(row.get(field))
    if value is None:
        return None
    fp = str(row.get("fiscal_period") or "").upper()
    if fp == "FY":
        proxies.append(f"annualized_{field}_from_10k")
        return value / 4.0
    return value


def fiscal_year_key(row: dict[str, Any]) -> int | None:
    fiscal_year = to_float(row.get("fiscal_year"))
    if fiscal_year is not None:
        return int(fiscal_year)
    period_end = parse_date(row.get("period_end"))
    return period_end.year if period_end is not None else None


def operating_cash_flow_is_cumulative(row: dict[str, Any]) -> bool:
    fiscal_period = str(row.get("fiscal_period") or "").upper()
    duration_days = to_float(row.get("operating_cash_flow_duration_days"))
    if duration_days is not None:
        if fiscal_period == "Q2":
            return duration_days >= 140.0
        if fiscal_period == "Q3":
            return duration_days >= 220.0
        if fiscal_period == "FY":
            return duration_days >= 300.0
        return False
    form = str(row.get("form") or "").upper()
    return form in CUMULATIVE_CASH_FLOW_FORMS and fiscal_period in {"Q2", "Q3", "FY"}


def operating_cash_flow_rows_by_quarter(
    rows: list[dict[str, Any]],
) -> dict[tuple[int, int], dict[str, Any]]:
    by_quarter: dict[tuple[int, int], dict[str, Any]] = {}
    for row in rows:
        value = to_float(row.get("operating_cash_flow"))
        fiscal_year = fiscal_year_key(row)
        quarter = FISCAL_QUARTER_NUMBER.get(str(row.get("fiscal_period") or "").upper())
        if value is None or fiscal_year is None or quarter is None:
            continue
        key = (fiscal_year, quarter)
        current = by_quarter.get(key)
        if current is None:
            by_quarter[key] = row
            continue
        current_period = parse_date(current.get("period_end")) or date.min
        candidate_period = parse_date(row.get("period_end")) or date.min
        current_is_fy = str(current.get("fiscal_period") or "").upper() == "FY"
        candidate_is_fy = str(row.get("fiscal_period") or "").upper() == "FY"
        if candidate_period > current_period or (
            candidate_period == current_period and current_is_fy and not candidate_is_fy
        ):
            by_quarter[key] = row
    return by_quarter


def discrete_operating_cash_flow_quarters(
    rows: list[dict[str, Any]],
    proxies: list[str],
) -> list[tuple[dict[str, Any], float]]:
    by_quarter = operating_cash_flow_rows_by_quarter(rows)
    discrete: list[tuple[dict[str, Any], float]] = []
    for (fiscal_year, quarter), row in by_quarter.items():
        value = to_float(row.get("operating_cash_flow"))
        if value is None:
            continue
        fiscal_period = str(row.get("fiscal_period") or "").upper()
        if not operating_cash_flow_is_cumulative(row):
            discrete_value = value
        else:
            prior = by_quarter.get((fiscal_year, quarter - 1))
            prior_value = to_float((prior or {}).get("operating_cash_flow"))
            prior_is_valid_baseline = (
                prior is not None
                and prior_value is not None
                and (
                    quarter - 1 == 1
                    or operating_cash_flow_is_cumulative(prior)
                )
            )
            if not prior_is_valid_baseline:
                continue
            assert prior_value is not None
            discrete_value = value - prior_value
            proxy = f"discrete_operating_cash_flow_{fiscal_period.lower()}_from_ytd_delta"
            if proxy not in proxies:
                proxies.append(proxy)
        discrete.append((row, discrete_value))
    discrete.sort(
        key=lambda item: parse_date(item[0].get("period_end")) or date.min,
        reverse=True,
    )
    return discrete


def contiguous_operating_cash_flow_values(
    discrete: list[tuple[dict[str, Any], float]],
) -> list[float]:
    values: list[float] = []
    previous_ordinal: int | None = None
    for row, value in discrete:
        fiscal_year = fiscal_year_key(row)
        quarter = FISCAL_QUARTER_NUMBER.get(str(row.get("fiscal_period") or "").upper())
        if fiscal_year is None or quarter is None:
            break
        ordinal = fiscal_year * 4 + quarter
        if previous_ordinal is not None and previous_ordinal - ordinal != 1:
            break
        values.append(value)
        previous_ordinal = ordinal
    return values


def latest_operating_cash_flow_quarter(
    rows: list[dict[str, Any]],
    proxies: list[str],
) -> float | None:
    latest_row = next(
        (row for row in rows if to_float(row.get("operating_cash_flow")) is not None),
        None,
    )
    if latest_row is None:
        return None
    latest_key = (
        fiscal_year_key(latest_row),
        FISCAL_QUARTER_NUMBER.get(str(latest_row.get("fiscal_period") or "").upper()),
    )
    for row, value in discrete_operating_cash_flow_quarters(rows, proxies):
        row_key = (
            fiscal_year_key(row),
            FISCAL_QUARTER_NUMBER.get(str(row.get("fiscal_period") or "").upper()),
        )
        if row_key == latest_key:
            return value
    value = to_float(latest_row.get("operating_cash_flow"))
    fiscal_period = str(latest_row.get("fiscal_period") or "").upper()
    quarter = FISCAL_QUARTER_NUMBER.get(fiscal_period)
    if value is None or quarter is None:
        return None
    if operating_cash_flow_is_cumulative(latest_row):
        proxy = "annualized_ytd_operating_cash_flow"
        if proxy not in proxies:
            proxies.append(proxy)
        return value / float(quarter)
    return value


def ttm_amount(
    rows: list[dict[str, Any]],
    field: str,
    proxies: list[str],
    *,
    asof_date: date | None = None,
    max_fy_age_days: int = 550,
) -> float | None:
    if field == "operating_cash_flow":
        discrete_values = contiguous_operating_cash_flow_values(
            discrete_operating_cash_flow_quarters(rows, proxies)
        )
        if len(discrete_values) >= 4:
            return sum(discrete_values[:4])
        if len(discrete_values) >= 2:
            proxies.append("partial_quarter_annualized_operating_cash_flow")
            return sum(discrete_values) / len(discrete_values) * 4.0
    if field != "operating_cash_flow":
        quarterly_values: list[float] = []
        for row in rows:
            fp = str(row.get("fiscal_period") or "").upper()
            value = to_float(row.get(field))
            if value is None:
                continue
            if fp in QUARTER_PERIODS:
                quarterly_values.append(value)
            if len(quarterly_values) >= 4:
                break
        if len(quarterly_values) >= 2:
            if len(quarterly_values) < 4:
                proxies.append(f"partial_quarter_annualized_{field}")
                return sum(quarterly_values) / len(quarterly_values) * 4.0
            return sum(quarterly_values[:4])
    for row in rows:
        if str(row.get("fiscal_period") or "").upper() == "FY":
            period_end = parse_date(row.get("period_end"))
            if asof_date is not None and period_end is not None:
                age_days = (asof_date - period_end).days
                if age_days < 0 or age_days > max_fy_age_days:
                    continue
            value = to_float(row.get(field))
            if value is not None:
                return value
    if field == "operating_cash_flow":
        latest_row = next(
            (row for row in rows if to_float(row.get(field)) is not None),
            None,
        )
        if latest_row is not None and operating_cash_flow_is_cumulative(latest_row):
            value = to_float(latest_row.get(field))
            quarter = FISCAL_QUARTER_NUMBER.get(
                str(latest_row.get("fiscal_period") or "").upper()
            )
            if value is not None and quarter:
                proxies.append("annualized_ytd_operating_cash_flow")
                return value / float(quarter) * 4.0
    return None


def burn_metrics(
    rows: list[dict[str, Any]],
    proxies: list[str],
    missing: list[str],
    *,
    asof_date: date | None = None,
) -> tuple[float | None, float | None, float | None]:
    latest_ocf_row = next((row for row in rows if to_float(row.get("operating_cash_flow")) is not None), None)
    latest_burn: float | None = None
    if latest_ocf_row is not None:
        ocf_quarter = latest_operating_cash_flow_quarter(rows, proxies)
        latest_burn = max(0.0, -(ocf_quarter or 0.0))
    else:
        latest_net_income_row = next((row for row in rows if to_float(row.get("net_income")) is not None), None)
        if latest_net_income_row is not None:
            proxies.append("net_income_for_quarterly_cash_burn")
            net_income_quarter = amount_for_period(latest_net_income_row, "net_income", proxies)
            latest_burn = max(0.0, -(net_income_quarter or 0.0))
        else:
            missing.append("quarterly_cash_burn")

    ocf_ttm = ttm_amount(rows, "operating_cash_flow", proxies, asof_date=asof_date)
    if ocf_ttm is None:
        net_income_ttm = ttm_amount(rows, "net_income", proxies, asof_date=asof_date)
        if net_income_ttm is not None:
            proxies.append("net_income_for_ttm_cash_burn")
            ocf_ttm = net_income_ttm
        else:
            missing.append("operating_cash_flow_ttm")
    ttm_burn = max(0.0, -(ocf_ttm or 0.0)) if ocf_ttm is not None else None
    return latest_burn, ttm_burn, ocf_ttm


def financing_event_is_current(event_type: str, excerpt: str, *, asof_date: date) -> bool:
    text = " ".join(str(excerpt or "").lower().split())
    if not text:
        return False
    if any(term in text for term in ("risk factors", "may issue", "could issue", "from time to time", "there can be no assurance")):
        return False

    # 10-Ks often restate years-old financing history. Keep events that are
    # explicitly current/recent, but do not punish stale financing boilerplate.
    years = [int(match) for match in re.findall(r"\b(20\d{2})\b", text)]
    if years and max(years) < asof_date.year:
        return False

    if event_type in {"atm_program", "atm_facility"}:
        return any(term in text for term in ("at-the-market", "at the market", "sales agreement", "equity distribution", "net proceeds"))
    if event_type in {"public_offering", "pipe_financing"}:
        return any(term in text for term in ("completed", "closed", "priced", "entered into", "net proceeds", "underwritten", "private placement"))
    if event_type in {"shelf_registration", "financing_shelf"}:
        return any(term in text for term in ("effective", "filed", "registration statement", "shelf"))
    return False


def load_dilution_events(conn: sqlite3.Connection, *, company_id: int, asof_date: date) -> dict[str, Any]:
    cutoff = (asof_date - timedelta(days=365)).isoformat()
    rows = conn.execute(
        """
        SELECT event_type, accession_nodash, extracted_text
        FROM sec_events
        WHERE company_id = ?
          AND filing_date >= ?
          AND filing_date <= ?
        """,
        (company_id, cutoff, asof_date.isoformat()),
    ).fetchall()
    seen: set[tuple[str, str]] = set()
    counts: dict[str, int] = {}
    for row in rows:
        event_type = str(row["event_type"] or "")
        if event_type not in {"atm_program", "atm_facility", "public_offering", "pipe_financing", "shelf_registration", "financing_shelf"}:
            continue
        if not financing_event_is_current(event_type, str(row["extracted_text"] or ""), asof_date=asof_date):
            continue
        key = (event_type, str(row["accession_nodash"] or ""))
        if key in seen:
            continue
        seen.add(key)
        counts[event_type] = counts.get(event_type, 0) + 1
    return {
        "atm_facility_active": 1 if (counts.get("atm_facility", 0) + counts.get("atm_program", 0)) > 0 else 0,
        "recent_offering_count_12m": counts.get("public_offering", 0) + counts.get("pipe_financing", 0),
        "shelf_registration_active": 1 if (counts.get("shelf_registration", 0) + counts.get("financing_shelf", 0)) > 0 else 0,
    }


def load_dilution_events_bulk(
    conn: sqlite3.Connection,
    *,
    company_ids: list[int],
    asof_date: date,
    identity_start_dates: dict[int, date] | None = None,
) -> dict[int, dict[str, Any]]:
    if not company_ids:
        return {}
    if len(company_ids) > SQLITE_PARAM_CHUNK_SIZE:
        out: dict[int, dict[str, Any]] = {}
        for company_chunk in chunked(company_ids):
            out.update(
                load_dilution_events_bulk(
                    conn,
                    company_ids=[int(value) for value in company_chunk],
                    asof_date=asof_date,
                    identity_start_dates=identity_start_dates,
                )
            )
        return out
    cutoff = (asof_date - timedelta(days=365)).isoformat()
    placeholders = ",".join("?" for _ in company_ids)
    rows = conn.execute(
        f"""
        SELECT company_id, filing_date, event_type, accession_nodash, extracted_text
        FROM sec_events
        WHERE company_id IN ({placeholders})
          AND filing_date >= ?
          AND filing_date <= ?
        ORDER BY company_id
        """,
        tuple(company_ids) + (cutoff, asof_date.isoformat()),
    ).fetchall()
    grouped: dict[int, set[tuple[str, str]]] = {company_id: set() for company_id in company_ids}
    counts_by_company: dict[int, dict[str, int]] = {company_id: {} for company_id in company_ids}
    valid_types = {"atm_program", "atm_facility", "public_offering", "pipe_financing", "shelf_registration", "financing_shelf"}
    floors = identity_start_dates or {}
    for row in rows:
        company_id = int(row["company_id"])
        filing_date = parse_date(row["filing_date"])
        identity_start = floors.get(company_id)
        if identity_start is not None and (filing_date is None or filing_date < identity_start):
            continue
        event_type = str(row["event_type"] or "")
        if event_type not in valid_types:
            continue
        if not financing_event_is_current(event_type, str(row["extracted_text"] or ""), asof_date=asof_date):
            continue
        key = (event_type, str(row["accession_nodash"] or ""))
        if key in grouped[company_id]:
            continue
        grouped[company_id].add(key)
        counts = counts_by_company[company_id]
        counts[event_type] = counts.get(event_type, 0) + 1
    return {
        company_id: {
            "atm_facility_active": 1 if (counts.get("atm_facility", 0) + counts.get("atm_program", 0)) > 0 else 0,
            "recent_offering_count_12m": counts.get("public_offering", 0) + counts.get("pipe_financing", 0),
            "shelf_registration_active": 1 if (counts.get("shelf_registration", 0) + counts.get("financing_shelf", 0)) > 0 else 0,
        }
        for company_id, counts in counts_by_company.items()
    }


def load_going_concern_status_bulk(
    conn: sqlite3.Connection,
    *,
    company_ids: list[int],
    asof_date: date,
    identity_start_dates: dict[int, date] | None = None,
) -> dict[int, str]:
    if not company_ids:
        return {}
    if len(company_ids) > SQLITE_PARAM_CHUNK_SIZE:
        out: dict[int, str] = {}
        for company_chunk in chunked(company_ids):
            out.update(
                load_going_concern_status_bulk(
                    conn,
                    company_ids=[int(value) for value in company_chunk],
                    asof_date=asof_date,
                    identity_start_dates=identity_start_dates,
                )
            )
        return out
    cutoff = (asof_date - timedelta(days=400)).isoformat()
    placeholders = ",".join("?" for _ in company_ids)
    rows = conn.execute(
        f"""
        SELECT company_id, filing_date, form
        FROM sec_events
        WHERE company_id IN ({placeholders})
          AND filing_date >= ?
          AND filing_date <= ?
          AND event_type = 'going_concern_confirmed'
        """,
        tuple(company_ids) + (cutoff, asof_date.isoformat()),
    ).fetchall()
    floors = identity_start_dates or {}
    out: dict[int, str] = {}
    for row in rows:
        company_id = int(row["company_id"])
        filing_date = parse_date(row["filing_date"])
        identity_start = floors.get(company_id)
        if identity_start is not None and (filing_date is None or filing_date < identity_start):
            continue
        event_status = going_concern_status_for_form(row["form"])
        if event_status == "confirmed" or company_id not in out:
            out[company_id] = event_status
    return out


def compute_survival_row(
    *,
    company: dict[str, Any],
    rows: list[dict[str, Any]],
    screen_row: dict[str, str],
    asof_date: date,
    dilution_events: dict[str, Any],
    going_concern_status: str,
    config: dict[str, Any],
) -> dict[str, Any]:
    missing: list[str] = []
    proxies: list[str] = []
    ticker = str(company["ticker"] or "").upper()
    company_id = int(company["company_id"])
    cash_field = "cash_and_investments"
    cash, cash_row = latest_nonnull(rows, "cash_and_investments")
    if cash is None:
        cash_field = "cash_and_equivalents"
        cash, cash_row = latest_nonnull(rows, "cash_and_equivalents")
        if cash is not None:
            proxies.append("cash_and_equivalents_for_cash_and_investments")
    if cash is None:
        missing.append("cash_and_investments")
    if cash_row:
        proxies.extend(
            proxy
            for proxy in proxy_field_names(cash_row.get("proxy_fields_used"))
            if "cash_and_investments" in proxy
        )
    negative_cash_flag = int(cash is not None and cash < 0.0)
    latest_period_end = str((cash_row or {}).get("period_end") or "")

    quarterly_burn, ttm_burn, ocf_ttm = burn_metrics(rows, proxies, missing, asof_date=asof_date)
    revenue_ttm = ttm_amount(rows, "revenue", proxies, asof_date=asof_date)
    if cash is not None and cash <= 0:
        runway = 0.0
    elif ttm_burn is not None and ttm_burn > 0 and cash is not None:
        runway = cash / (ttm_burn / 12.0)
    elif ocf_ttm is not None and ocf_ttm >= 0 and cash is not None:
        revenue_buffer_pct = float(cfg_get(config, "financial_survival.profitable_runway_revenue_buffer_pct", 0.10))
        runway_cap = float(cfg_get(config, "financial_survival.profitable_company_runway_cap_months", 120.0))
        if revenue_ttm is not None and revenue_ttm > 0.0 and revenue_buffer_pct > 0.0:
            monthly_proxy_burn = max((revenue_ttm * revenue_buffer_pct) / 12.0, 1.0)
            runway = min(runway_cap, cash / monthly_proxy_burn)
            proxies.append("nonnegative_operating_cash_flow_revenue_buffer_runway_proxy")
        else:
            runway = runway_cap
            proxies.append("nonnegative_operating_cash_flow_runway_cap")
    else:
        runway = None
        missing.append("cash_runway_months")
    cash_reliability_record = dict(cash_row or {})
    cash_reliability_record["proxy_fields_used"] = proxies
    cash_runway_reliable_flag = int(
        cash is not None
        and runway is not None
        and cash_runway_is_reliable(cash_reliability_record)
    )

    rd_ttm = ttm_amount(rows, "rd_expense", proxies, asof_date=asof_date)
    sgna_ttm = ttm_amount(rows, "sgna_expense", proxies, asof_date=asof_date)
    if rd_ttm is None:
        missing.append("rd_expense_ttm")
    if sgna_ttm is None:
        missing.append("sgna_expense_ttm")

    working_capital, wc_row = latest_nonnull(rows, "working_capital")
    current_assets, _ = latest_nonnull(rows, "current_assets")
    current_liabilities, _ = latest_nonnull(rows, "current_liabilities")
    working_capital_ratio = (
        current_assets / current_liabilities
        if current_assets is not None and current_liabilities is not None and current_liabilities != 0
        else None
    )
    if working_capital is None:
        missing.append("working_capital")
    if working_capital_ratio is None:
        missing.append("working_capital_ratio")

    total_debt, _ = latest_nonnull(rows, "total_debt")
    if total_debt is not None and total_debt < 0.0:
        # Negative reported total_debt is a benign data artifact; clamp to zero so the
        # ratio cannot go negative (the negative-cash stress case is handled via 999.0).
        total_debt = 0.0
    if total_debt is not None and total_debt > 0 and cash is not None and cash <= 0:
        debt_to_cash = 999.0
    else:
        debt_to_cash = total_debt / cash if total_debt is not None and cash is not None and cash != 0 else None

    cash_period_date = parse_date(cash_row.get("period_end")) if cash_row else None
    cash_qoq = pct_change(cash, closest_prior_value(rows_after_marker(rows, cash_row), cash_field, cash_period_date or asof_date, 30, 140))
    cash_yoy = pct_change(cash, closest_prior_value(rows, cash_field, (cash_period_date or asof_date) - timedelta(days=365), 0, 120))

    latest_rd, rd_row = latest_nonnull(rows, "rd_expense")
    rd_period_date = parse_date(rd_row.get("period_end")) if rd_row else None
    rd_qoq = pct_change(latest_rd, closest_prior_value(rows_after_marker(rows, rd_row), "rd_expense", rd_period_date or asof_date, 30, 140))
    rd_yoy = pct_change(latest_rd, closest_prior_value(rows, "rd_expense", (rd_period_date or asof_date) - timedelta(days=365), 0, 120))

    rd_growth_threshold = float(cfg_get(config, "financial_survival.rd_growth_threshold", 0.30))
    cash_decline_threshold = float(cfg_get(config, "financial_survival.cash_decline_threshold", -0.30))
    if rd_yoy is None:
        missing.append("rd_yoy_change_pct")
    if cash_yoy is None:
        missing.append("cash_yoy_change_pct")
    if rd_qoq is None:
        missing.append("rd_qoq_change_pct")
    if cash_qoq is None:
        missing.append("cash_qoq_change_pct")
    burn_acceleration = int((rd_yoy is not None and rd_yoy > rd_growth_threshold) and (cash_yoy is not None and cash_yoy < cash_decline_threshold))
    short_runway_months = float(cfg_get(config, "financial_survival.short_runway_months", 6))
    severe_runway_months = float(cfg_get(config, "financial_survival.severe_runway_months", 3))
    short_runway_flag = int(cash_runway_reliable_flag > 0 and runway is not None and runway < short_runway_months)
    severe_runway_flag = int(cash_runway_reliable_flag > 0 and runway is not None and runway < severe_runway_months)

    atm_active = int(dilution_events.get("atm_facility_active") or 0)
    offering_count = int(dilution_events.get("recent_offering_count_12m") or 0)
    shelf_active = int(dilution_events.get("shelf_registration_active") or 0)
    dilution_score = 0.0
    dilution_score += float(cfg_get(config, "financial_survival.active_atm_penalty", 15)) if atm_active else 0.0
    dilution_score += min(30.0, offering_count * float(cfg_get(config, "financial_survival.recent_offering_penalty", 10)))
    dilution_score += float(cfg_get(config, "financial_survival.shelf_registration_penalty", 8)) if shelf_active else 0.0
    strong_cash_generation = (ocf_ttm is not None and ocf_ttm >= 0) or (runway is not None and runway >= 24)
    if strong_cash_generation:
        dilution_score *= 0.45
    elif runway is not None and runway >= float(cfg_get(config, "financial_survival.min_acceptable_runway_months", 12)):
        dilution_score *= 0.70

    source_priority = [
        str(item).strip().lower()
        for item in (cfg_get(config, "financial_survival.going_concern_source_priority", ["db", "csv"]) or [])
    ]
    db_going_status = str(going_concern_status or "").strip().lower()
    csv_going_status = str(screen_row.get("going_concern_status") or "").strip().lower()
    going_status = ""
    for source in source_priority or ["db", "csv"]:
        if source == "db" and db_going_status:
            going_status = db_going_status
            if csv_going_status and csv_going_status != db_going_status:
                LOGGER.warning(
                    "DB going_concern_status for %s overrides conflicting CSV value: db=%s csv=%s",
                    ticker,
                    db_going_status,
                    csv_going_status,
                )
            break
        if source == "csv" and csv_going_status:
            going_status = csv_going_status
            if not db_going_status:
                LOGGER.warning("Using CSV going_concern_status for %s because DB value is absent", ticker)
            elif csv_going_status != db_going_status:
                LOGGER.warning(
                    "CSV going_concern_status for %s overrides conflicting DB value due to configured priority: csv=%s db=%s",
                    ticker,
                    csv_going_status,
                    db_going_status,
                )
            break
    if not going_status:
        going_status = db_going_status or csv_going_status
    latest_periodic_status = str(screen_row.get("latest_periodic_going_concern_status") or "").strip().lower()
    if csv_going_status == "resolved" and latest_periodic_status in {"none", "resolved"}:
        going_status = "resolved"
    resolution_runway_months = float(
        cfg_get(config, "financial_survival.going_concern_resolution_runway_months", 18.0)
    )
    runway_resolution_applied = bool(
        going_status in {"confirmed", "possible"}
        and cash_runway_reliable_flag > 0
        and runway is not None
        and runway >= resolution_runway_months
    )
    if runway_resolution_applied:
        going_status = "resolved"
    # Prefer the broader 2-year NT-filing screen when present; the output keeps
    # the historical 12m field name for downstream schema compatibility.
    late_filing_raw = screen_row.get("recent_nt_filing_count_2y")
    if late_filing_raw is None:
        late_filing_raw = screen_row.get("late_filing_count_12m")
    if "recent_nt_filing_count_2y" not in screen_row and "late_filing_count_12m" not in screen_row:
        missing.append("late_filing_count_12m")
    late_filing_count = to_int(late_filing_raw, 0)

    data_quality = "high"
    if "cash_and_investments" in missing or "cash_runway_months" in missing:
        data_quality = "low"
    elif missing or proxies:
        data_quality = "medium"

    # Start below the ceiling so durable runway can create an observable quality bonus.
    score = 95.0
    if runway is None:
        score -= 25.0
    elif runway < severe_runway_months:
        score -= 45.0
    elif runway < short_runway_months:
        score -= 30.0
    elif runway < float(cfg_get(config, "financial_survival.min_acceptable_runway_months", 12)):
        score -= 15.0
    elif runway >= float(cfg_get(config, "financial_survival.min_high_quality_runway_months", 18)):
        score += 5.0
    if debt_to_cash is not None:
        # Ratio is non-negative by construction (negative total_debt is clamped to 0
        # upstream and negative cash routes to the 999.0 sentinel).
        if debt_to_cash > 1.0:
            score -= 15.0
        elif debt_to_cash > 0.5:
            score -= 8.0
    if working_capital_ratio is not None and working_capital_ratio < 1.0:
        score -= 12.0
    if working_capital is not None and working_capital < 0:
        score -= 10.0
    if burn_acceleration:
        score -= 10.0
    score -= dilution_score
    if going_status == "confirmed":
        score -= 35.0
    elif going_status == "possible":
        score -= 15.0
    elif going_status == "resolved":
        score -= 5.0
    if late_filing_count > 0:
        score -= min(15.0, late_filing_count * 5.0)
    if data_quality == "low":
        score -= float(cfg_get(config, "financial_survival.low_data_quality_penalty", 12))
    elif data_quality == "medium" and any("net_income_for" in p for p in proxies):
        score -= float(cfg_get(config, "financial_survival.missing_burn_proxy_penalty", 10))

    payload = {
        "ticker": ticker,
        "latest_period_end": latest_period_end,
        "missing_fields": missing,
        "proxy_fields_used": proxies,
        "data_quality": data_quality,
        "cash_runway_reliable_flag": cash_runway_reliable_flag,
        "going_concern_runway_resolution_applied": int(runway_resolution_applied),
        "going_concern_resolution_runway_months": resolution_runway_months,
    }
    return {
        "asof_date": asof_date.isoformat(),
        "company_id": company_id,
        "ticker": ticker,
        "company_name": str(company["company_name"] or ""),
        "latest_period_end": latest_period_end,
        "cash_and_investments": cash,
        "quarterly_cash_burn": quarterly_burn,
        "ttm_cash_burn": ttm_burn,
        "operating_cash_flow_ttm": ocf_ttm,
        "rd_expense_ttm": rd_ttm,
        "sgna_expense_ttm": sgna_ttm,
        "cash_runway_months": runway,
        "cash_runway_reliable_flag": cash_runway_reliable_flag,
        "working_capital": working_capital,
        "working_capital_ratio": working_capital_ratio,
        "debt_to_cash": debt_to_cash,
        "cash_qoq_change_pct": cash_qoq,
        "cash_yoy_change_pct": cash_yoy,
        "rd_qoq_change_pct": rd_qoq,
        "rd_yoy_change_pct": rd_yoy,
        "negative_cash_flag": negative_cash_flag,
        "burn_acceleration_flag": burn_acceleration,
        "short_runway_flag": short_runway_flag,
        "severe_runway_flag": severe_runway_flag,
        "atm_facility_active": atm_active,
        "recent_offering_count_12m": offering_count,
        "shelf_registration_active": shelf_active,
        "dilution_pressure_score": round(dilution_score, 4),
        "going_concern_status": going_status,
        "late_filing_count_12m": late_filing_count,
        "financial_survival_score": round(clamp(score), 4),
        "data_quality": data_quality,
        "missing_fields": ";".join(dict.fromkeys(missing)),
        "proxy_fields_used": ";".join(dict.fromkeys(proxies)),
        "payload_json": json.dumps(payload, ensure_ascii=True, sort_keys=True),
    }


def replace_survival_features(
    conn: sqlite3.Connection,
    rows: list[dict[str, Any]],
    asof_date: str,
    *,
    target_company_ids: set[int] | None = None,
) -> None:
    now = utc_now()
    db_fields = [field for field in SURVIVAL_FIELDS if field not in {"ticker", "company_name"}]
    with conn:
        if target_company_ids is None:
            conn.execute("DELETE FROM financial_survival_features WHERE asof_date = ?", (asof_date,))
        elif target_company_ids:
            for company_chunk in chunked(sorted(target_company_ids)):
                company_placeholders = ",".join("?" for _ in company_chunk)
                conn.execute(
                    f"DELETE FROM financial_survival_features WHERE asof_date = ? AND company_id IN ({company_placeholders})",
                    (asof_date, *company_chunk),
                )
        else:
            return
        conn.executemany(
            f"""
            INSERT INTO financial_survival_features({", ".join(db_fields)}, created_at, updated_at)
            VALUES ({", ".join("?" for _ in db_fields)}, ?, ?)
            """,
            [tuple(row.get(field) for field in db_fields) + (now, now) for row in rows],
        )


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=SURVIVAL_FIELDS, lineterminator="\n", extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def log_missing_issue(conn: sqlite3.Connection, *, row: dict[str, Any], field: str, severity: str, proxy: str = "") -> None:
    conn.execute(
        """
        INSERT INTO data_quality_issues(
            asof_date, company_id, ticker, table_name, field_name, issue_type, severity, proxy_used, message, created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            row["asof_date"],
            row["company_id"],
            row["ticker"],
            "financial_survival_features",
            field,
            "missing_or_proxy",
            severity,
            proxy,
            f"{field} missing or proxy used in financial survival calculation.",
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
    security_identity_rules = load_security_identity_rules(identity_registry_path)
    db_path = args.db.expanduser().resolve() if args.db else resolve_path(cfg_get(config, "paths.database_path"), base_dir=base_dir)
    configured_universe_csv = resolve_path(
        cfg_get(
            config,
            "financial_survival.final_scoring_universe_csv",
            cfg_get(config, "sec_companyfacts_history.final_scoring_universe_csv"),
        ),
        base_dir=base_dir,
    )
    screen_csv = resolve_path(cfg_get(config, "biotech_features.screen_results_csv"), base_dir=base_dir)
    output_csv = resolve_path(cfg_get(config, "financial_survival.output_csv"), base_dir=base_dir)
    asof_date = parse_date(args.asof) if args.asof else datetime.now(timezone.utc).date()
    if asof_date is None:
        raise ValueError(f"Invalid --asof date: {args.asof}")
    universe_csv = resolve_dated_report_input_csv(
        configured_universe_csv,
        base_output_dir=resolve_path(cfg_get(config, "biotech_scoring.output_dir", "../output/biotech_index_reports"), base_dir=base_dir),
        asof_date=asof_date.isoformat(),
        logger=LOGGER,
    )
    ticker_filter = {normalize_ticker(x) for x in args.tickers.split(",") if normalize_ticker(x)}
    sqlite_timeout_sec = float(cfg_get(config, "runtime.sqlite_timeout_sec", 30.0))

    screen_rows = screen_rows_for_asof(screen_csv, asof_date=asof_date)
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
        validate_nonempty_selection(count=len(companies), context="financial survival feature build", subset_mode=subset_mode)
        loaded_tickers = [str(company["ticker"]) for company in companies]
        validate_requested_tickers(
            requested_tickers=ticker_filter,
            loaded_tickers=loaded_tickers,
            context="financial survival feature build",
        )
        validate_full_universe_coverage(
            expected_tickers=scoring_tickers,
            observed_tickers=loaded_tickers,
            context="financial survival feature build",
            subset_mode=subset_mode,
        )
        run_id: int | None = None
        try:
            run_id = start_run(conn, run_type="build_financial_survival_features", input_path=universe_csv)
            company_ids = [int(company["company_id"]) for company in companies]
            company_ids_by_ticker = {
                normalize_ticker(company["ticker"]): int(company["company_id"])
                for company in companies
            }
            identity_start_dates = identity_start_dates_by_company(
                security_identity_rules,
                company_ids_by_ticker,
            )
            fact_rows_by_company = load_fact_rows_bulk(
                conn,
                company_ids,
                asof_date,
                identity_start_dates=identity_start_dates,
            )
            dilution_events_by_company = load_dilution_events_bulk(
                conn,
                company_ids=company_ids,
                asof_date=asof_date,
                identity_start_dates=identity_start_dates,
            )
            going_concern_by_company = load_going_concern_status_bulk(
                conn,
                company_ids=company_ids,
                asof_date=asof_date,
                identity_start_dates=identity_start_dates,
            )
            rows: list[dict[str, Any]] = []
            for company in companies:
                ticker = str(company["ticker"] or "").upper()
                company_id = int(company["company_id"])
                fact_rows = fact_rows_by_company.get(company_id, [])
                dilution_events = dilution_events_by_company.get(
                    company_id,
                    {"atm_facility_active": 0, "recent_offering_count_12m": 0, "shelf_registration_active": 0},
                )
                survival_row = compute_survival_row(
                    company=company,
                    rows=fact_rows,
                    screen_row=screen_rows.get(ticker, {}),
                    asof_date=asof_date,
                    dilution_events=dilution_events,
                    going_concern_status=going_concern_by_company.get(company_id, ""),
                    config=config,
                )
                rows.append(survival_row)
            partial_run = bool(ticker_filter) or int(args.max_companies) > 0
            validate_output_coverage(
                expected_tickers=scoring_tickers,
                output_tickers=[row["ticker"] for row in rows],
                context="financial survival feature build",
                subset_mode=subset_mode,
            )
            replace_survival_features(
                conn,
                rows,
                asof_date.isoformat(),
                target_company_ids=set(company_ids) if partial_run else None,
            )
            with conn:
                for row in rows:
                    if to_int(row.get("negative_cash_flag"), 0) > 0:
                        log_missing_issue(conn, row=row, field="cash_and_investments", severity="high", proxy="negative_cash_position")
                    if row["data_quality"] == "low":
                        for field in str(row.get("missing_fields") or "").split(";"):
                            if field:
                                log_missing_issue(conn, row=row, field=field, severity="high")
                    elif str(row.get("proxy_fields_used") or ""):
                        log_missing_issue(conn, row=row, field="proxy_fields_used", severity="medium", proxy=str(row.get("proxy_fields_used") or ""))
            write_csv(output_csv, rows)
            LOGGER.info("Built financial survival features: rows=%d output=%s", len(rows), output_csv)
            finish_run(conn, run_id=run_id, status="success", row_count=len(rows), message=f"companies={len(companies)} output={output_csv}")
        except BaseException as exc:
            if run_id is not None and not (isinstance(exc, SystemExit) and exc.code in (0, None)):
                finish_run(conn, run_id=run_id, status="failed", row_count=0, message=f"{type(exc).__name__}: {exc}")
            raise


if __name__ == "__main__":
    main()
