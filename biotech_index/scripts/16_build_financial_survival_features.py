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
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from biotech_index.core.config import cfg_get, load_yaml, resolve_path
from biotech_index.core.db import connect, finish_run, init_db, start_run, utc_now


LOGGER = logging.getLogger("build_financial_survival_features")
DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
QUARTER_PERIODS = {"Q1", "Q2", "Q3", "Q4"}


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
    "working_capital",
    "working_capital_ratio",
    "debt_to_cash",
    "cash_qoq_change_pct",
    "cash_yoy_change_pct",
    "rd_qoq_change_pct",
    "rd_yoy_change_pct",
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
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%SZ",
    )
    for handler in logging.getLogger().handlers:
        if handler.formatter is not None:
            handler.formatter.converter = time.gmtime


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
    return max(low, min(high, value))


def as_bool(raw: object) -> bool:
    return str(raw or "").strip().lower() in {"1", "true", "yes", "y"}


def read_screen_rows(path: Path) -> dict[str, dict[str, str]]:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        return {str(row.get("ticker") or "").strip().upper(): {str(k): str(v or "") for k, v in row.items()} for row in reader}


def read_scoring_tickers(path: Path) -> set[str]:
    if not path.exists():
        raise FileNotFoundError(f"Final scoring universe CSV not found: {path}")
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        out: set[str] = set()
        for row in reader:
            ticker = str(row.get("ticker") or "").strip().upper()
            if ticker and str(row.get("final_status") or "").strip().lower() == "keep" and as_bool(row.get("scoring_include")):
                out.add(ticker)
    if not out:
        raise ValueError(f"Final scoring universe CSV contains no scoring tickers: {path}")
    return out


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
        ORDER BY ticker
        """
    ).fetchall()
    out: list[dict[str, Any]] = []
    for row in rows:
        ticker = str(row["ticker"] or "").upper()
        if scoring_tickers and ticker not in scoring_tickers:
            continue
        if ticker_filter and ticker not in ticker_filter:
            continue
        out.append(dict(row))
        if max_companies > 0 and len(out) >= max_companies:
            break
    return out


def load_fact_rows(conn: sqlite3.Connection, company_id: int, asof_date: date) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT *
        FROM company_facts_quarterly
        WHERE company_id = ?
          AND period_end <= ?
          AND (filed_date IS NULL OR filed_date = '' OR filed_date <= ?)
        ORDER BY period_end DESC, filed_date DESC
        """,
        (company_id, asof_date.isoformat(), asof_date.isoformat()),
    ).fetchall()
    return [dict(row) for row in rows]


def load_fact_rows_bulk(conn: sqlite3.Connection, company_ids: list[int], asof_date: date) -> dict[int, list[dict[str, Any]]]:
    if not company_ids:
        return {}
    placeholders = ",".join("?" for _ in company_ids)
    rows = conn.execute(
        f"""
        SELECT *
        FROM company_facts_quarterly
        WHERE company_id IN ({placeholders})
          AND period_end <= ?
          AND (filed_date IS NULL OR filed_date = '' OR filed_date <= ?)
        ORDER BY company_id, period_end DESC, filed_date DESC
        """,
        tuple(company_ids) + (asof_date.isoformat(), asof_date.isoformat()),
    ).fetchall()
    out: dict[int, list[dict[str, Any]]] = {company_id: [] for company_id in company_ids}
    for row in rows:
        out.setdefault(int(row["company_id"]), []).append(dict(row))
    for company_id, company_rows in out.items():
        out[company_id] = dedup_fact_rows(company_rows)
    return out


def dedup_fact_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[tuple[str, str]] = set()
    out: list[dict[str, Any]] = []
    for row in rows:
        key = (str(row.get("period_end") or ""), str(row.get("fiscal_period") or ""))
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


def ttm_amount(rows: list[dict[str, Any]], field: str, proxies: list[str]) -> float | None:
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
            value = to_float(row.get(field))
            if value is not None:
                return value
    return None


def burn_metrics(rows: list[dict[str, Any]], proxies: list[str], missing: list[str]) -> tuple[float | None, float | None, float | None]:
    latest_ocf_row = next((row for row in rows if to_float(row.get("operating_cash_flow")) is not None), None)
    latest_burn: float | None = None
    if latest_ocf_row is not None:
        ocf_quarter = amount_for_period(latest_ocf_row, "operating_cash_flow", proxies)
        latest_burn = max(0.0, -(ocf_quarter or 0.0))
    else:
        latest_net_income_row = next((row for row in rows if to_float(row.get("net_income")) is not None), None)
        if latest_net_income_row is not None:
            proxies.append("net_income_for_quarterly_cash_burn")
            net_income_quarter = amount_for_period(latest_net_income_row, "net_income", proxies)
            latest_burn = max(0.0, -(net_income_quarter or 0.0))
        else:
            missing.append("quarterly_cash_burn")

    ocf_ttm = ttm_amount(rows, "operating_cash_flow", proxies)
    if ocf_ttm is None:
        net_income_ttm = ttm_amount(rows, "net_income", proxies)
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
    if years and max(years) < asof_date.year - 1:
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


def load_dilution_events_bulk(conn: sqlite3.Connection, *, company_ids: list[int], asof_date: date) -> dict[int, dict[str, Any]]:
    if not company_ids:
        return {}
    cutoff = (asof_date - timedelta(days=365)).isoformat()
    placeholders = ",".join("?" for _ in company_ids)
    rows = conn.execute(
        f"""
        SELECT company_id, event_type, accession_nodash, extracted_text
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
    for row in rows:
        company_id = int(row["company_id"])
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


def load_going_concern_status_bulk(conn: sqlite3.Connection, *, company_ids: list[int], asof_date: date) -> dict[int, str]:
    if not company_ids:
        return {}
    cutoff = (asof_date - timedelta(days=400)).isoformat()
    placeholders = ",".join("?" for _ in company_ids)
    rows = conn.execute(
        f"""
        SELECT company_id, MAX(filing_date) AS latest_filing_date
        FROM sec_events
        WHERE company_id IN ({placeholders})
          AND filing_date >= ?
          AND filing_date <= ?
          AND event_type = 'going_concern_confirmed'
        GROUP BY company_id
        """,
        tuple(company_ids) + (cutoff, asof_date.isoformat()),
    ).fetchall()
    return {int(row["company_id"]): "confirmed" for row in rows}


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
    cash, cash_row = latest_nonnull(rows, "cash_and_investments")
    if cash is None:
        cash, cash_row = latest_nonnull(rows, "cash_and_equivalents")
        if cash is not None:
            proxies.append("cash_and_equivalents_for_cash_and_investments")
    if cash is None:
        missing.append("cash_and_investments")
    latest_period_end = str((cash_row or rows[0] if rows else {}).get("period_end") or "")

    quarterly_burn, ttm_burn, ocf_ttm = burn_metrics(rows, proxies, missing)
    if ttm_burn is not None and ttm_burn > 0 and cash is not None:
        runway = cash / (ttm_burn / 12.0)
    elif ocf_ttm is not None and ocf_ttm >= 0 and cash is not None:
        runway = 999.0
        proxies.append("positive_operating_cash_flow_runway_cap")
    else:
        runway = None
        missing.append("cash_runway_months")

    rd_ttm = ttm_amount(rows, "rd_expense", proxies)
    sgna_ttm = ttm_amount(rows, "sgna_expense", proxies)
    if rd_ttm is None:
        missing.append("rd_expense_ttm")

    working_capital, wc_row = latest_nonnull(rows, "working_capital")
    current_assets, _ = latest_nonnull(rows, "current_assets")
    current_liabilities, _ = latest_nonnull(rows, "current_liabilities")
    working_capital_ratio = current_assets / current_liabilities if current_assets is not None and current_liabilities not in {None, 0} else None
    if working_capital is None:
        missing.append("working_capital")
    if working_capital_ratio is None:
        missing.append("working_capital_ratio")

    total_debt, _ = latest_nonnull(rows, "total_debt")
    debt_to_cash = total_debt / cash if total_debt is not None and cash not in {None, 0} else None

    cash_period_date = parse_date(cash_row.get("period_end")) if cash_row else None
    cash_qoq = pct_change(cash, closest_prior_value(rows[1:] if rows else [], "cash_and_investments", cash_period_date or asof_date, 30, 140))
    cash_yoy = pct_change(cash, closest_prior_value(rows, "cash_and_investments", (cash_period_date or asof_date) - timedelta(days=365), 0, 120))

    latest_rd, rd_row = latest_nonnull(rows, "rd_expense")
    rd_period_date = parse_date(rd_row.get("period_end")) if rd_row else None
    rd_qoq = pct_change(latest_rd, closest_prior_value(rows[1:] if rows else [], "rd_expense", rd_period_date or asof_date, 30, 140))
    rd_yoy = pct_change(latest_rd, closest_prior_value(rows, "rd_expense", (rd_period_date or asof_date) - timedelta(days=365), 0, 120))

    rd_growth_threshold = float(cfg_get(config, "financial_survival.rd_growth_threshold", 0.30))
    cash_decline_threshold = float(cfg_get(config, "financial_survival.cash_decline_threshold", -0.30))
    burn_acceleration = int((rd_yoy is not None and rd_yoy > rd_growth_threshold) and (cash_yoy is not None and cash_yoy < cash_decline_threshold))
    short_runway_months = float(cfg_get(config, "financial_survival.short_runway_months", 6))
    severe_runway_months = float(cfg_get(config, "financial_survival.severe_runway_months", 3))
    short_runway_flag = int(runway is not None and runway < short_runway_months)
    severe_runway_flag = int(runway is not None and runway < severe_runway_months)

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

    going_status = str(going_concern_status or screen_row.get("going_concern_status") or "").strip().lower()
    late_filing_count = to_int(screen_row.get("recent_nt_filing_count_2y"), 0)

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
        "working_capital": working_capital,
        "working_capital_ratio": working_capital_ratio,
        "debt_to_cash": debt_to_cash,
        "cash_qoq_change_pct": cash_qoq,
        "cash_yoy_change_pct": cash_yoy,
        "rd_qoq_change_pct": rd_qoq,
        "rd_yoy_change_pct": rd_yoy,
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
    update_fields = [field for field in db_fields if field not in {"asof_date", "company_id"}]
    update_clause = ",\n                ".join(f"{field} = excluded.{field}" for field in update_fields)
    with conn:
        if target_company_ids is None:
            conn.execute("DELETE FROM financial_survival_features WHERE asof_date = ?", (asof_date,))
        elif target_company_ids:
            company_placeholders = ",".join("?" for _ in target_company_ids)
            conn.execute(
                f"DELETE FROM financial_survival_features WHERE asof_date = ? AND company_id IN ({company_placeholders})",
                (asof_date, *sorted(target_company_ids)),
            )
        else:
            return
        conn.executemany(
            f"""
            INSERT INTO financial_survival_features({", ".join(db_fields)}, created_at, updated_at)
            VALUES ({", ".join("?" for _ in db_fields)}, ?, ?)
            ON CONFLICT(asof_date, company_id) DO UPDATE SET
                {update_clause},
                updated_at = excluded.updated_at
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
    db_path = args.db.expanduser().resolve() if args.db else resolve_path(cfg_get(config, "paths.database_path"), base_dir=base_dir)
    universe_csv = resolve_path(cfg_get(config, "sec_companyfacts_history.final_scoring_universe_csv"), base_dir=base_dir)
    screen_csv = resolve_path(cfg_get(config, "biotech_features.screen_results_csv"), base_dir=base_dir)
    output_csv = resolve_path(cfg_get(config, "financial_survival.output_csv"), base_dir=base_dir)
    asof_date = parse_date(args.asof) if args.asof else datetime.now(timezone.utc).date()
    if asof_date is None:
        raise ValueError(f"Invalid --asof date: {args.asof}")
    ticker_filter = {x.strip().upper() for x in args.tickers.split(",") if x.strip()}
    sqlite_timeout_sec = float(cfg_get(config, "runtime.sqlite_timeout_sec", 30.0))

    screen_rows = read_screen_rows(screen_csv)
    with connect(db_path, timeout_sec=sqlite_timeout_sec) as conn:
        init_db(conn)
        scoring_tickers = read_scoring_tickers(universe_csv)
        companies = load_companies(
            conn,
            scoring_tickers=scoring_tickers,
            ticker_filter=ticker_filter,
            max_companies=int(args.max_companies),
        )
        run_id = start_run(conn, run_type="build_financial_survival_features", input_path=universe_csv)
        try:
            company_ids = [int(company["company_id"]) for company in companies]
            fact_rows_by_company = load_fact_rows_bulk(conn, company_ids, asof_date)
            dilution_events_by_company = load_dilution_events_bulk(conn, company_ids=company_ids, asof_date=asof_date)
            going_concern_by_company = load_going_concern_status_bulk(conn, company_ids=company_ids, asof_date=asof_date)
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
            replace_survival_features(
                conn,
                rows,
                asof_date.isoformat(),
                target_company_ids=set(company_ids) if partial_run else None,
            )
            with conn:
                for row in rows:
                    if row["data_quality"] == "low":
                        for field in str(row.get("missing_fields") or "").split(";"):
                            if field:
                                log_missing_issue(conn, row=row, field=field, severity="high")
                    elif str(row.get("proxy_fields_used") or ""):
                        log_missing_issue(conn, row=row, field="proxy_fields_used", severity="medium", proxy=str(row.get("proxy_fields_used") or ""))
            write_csv(output_csv, rows)
            finish_run(conn, run_id=run_id, status="success", row_count=len(rows), message=f"companies={len(companies)} output={output_csv}")
        except Exception as exc:
            finish_run(conn, run_id=run_id, status="failed", row_count=0, message=f"{type(exc).__name__}: {exc}")
            raise
    LOGGER.info("Built financial survival features: rows=%d output=%s", len(rows), output_csv)


if __name__ == "__main__":
    main()
