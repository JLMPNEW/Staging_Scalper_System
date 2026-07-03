#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import hashlib
import logging
import math
import sqlite3
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from technology.core.config import cfg_get, load_yaml, resolve_path  # noqa: E402
from technology.core.db import connect, finish_run, init_db, start_run, utc_now  # noqa: E402
from technology.core.logging_utils import configure_utc_logging  # noqa: E402
from technology.core.text_norm import normalize_ticker  # noqa: E402


LOGGER = logging.getLogger("build_technology_financial_features")
DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
RUN_TYPE = "build_technology_financial_features"
FINANCIAL_FORMS = {"10-K", "10-K/A", "10-Q", "10-Q/A", "20-F", "20-F/A", "40-F"}
ANNUAL_FORMS = {"10-K", "10-K/A", "20-F", "20-F/A", "40-F"}
FLOW_METRICS = {
    "revenue",
    "cost_of_sales",
    "gross_profit",
    "operating_income",
    "pretax_income",
    "net_income",
    "operating_cash_flow",
    "investing_cash_flow",
    "financing_cash_flow",
    "capex",
    "research_and_development",
    "stock_based_compensation",
}
CRITICAL_METRICS = {"revenue", "assets"}
FLOW_USD_METRICS = {
    "revenue",
    "gross_profit",
    "operating_income",
    "net_income",
    "operating_cash_flow",
    "capex",
    "free_cash_flow",
}
BALANCE_USD_METRICS = {
    "assets",
    "liabilities",
    "equity",
    "cash_and_equivalents",
    "total_debt",
    "inventory",
    "accounts_receivable",
    "accounts_payable",
}
CSV_FIELDS = [
    "ticker",
    "asof_date",
    "form_type",
    "fiscal_period_end",
    "fiscal_year",
    "fiscal_period",
    "reporting_standard",
    "financial_frequency",
    "reported_currency",
    "fx_conversion_status",
    "fx_rate_income_statement",
    "fx_rate_balance_sheet",
    "revenue",
    "revenue_usd",
    "revenue_ttm",
    "gross_margin",
    "operating_margin",
    "free_cash_flow",
    "free_cash_flow_ttm",
    "revenue_yoy_growth",
    "inventory_days",
    "days_sales_outstanding",
    "days_payables_outstanding",
    "cash_conversion_cycle",
    "deferred_revenue",
    "remaining_performance_obligation",
    "data_quality_status",
    "review_reason",
]


@dataclass(frozen=True)
class CanonicalFact:
    metric: str
    value: float
    reported_currency: str
    source_unit: str
    source_taxonomy: str
    source_concept: str
    source_priority: int
    source_quality: float
    start_date: date | None
    end_date: date
    filing_date: date | None
    accession: str
    form_type: str
    fiscal_year: int | None
    fiscal_period: str


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build canonical SEC financial statement features for technology tickers.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--model-family", default="", help="Technology model family to build, e.g. semiconductors.")
    parser.add_argument("--tickers", default="")
    parser.add_argument("--output-csv", type=Path, default=None)
    return parser.parse_args()


def parse_date(raw: object) -> date | None:
    text = str(raw or "").strip()[:10]
    if not text:
        return None
    try:
        return datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError:
        return None


def safe_float(raw: object) -> float | None:
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def safe_div(num: float | None, den: float | None) -> float | None:
    if num is None or den is None or den == 0:
        return None
    value = num / den
    return value if math.isfinite(value) else None


def pct_change(value: float | None, prior: float | None) -> float | None:
    # Growth off a non-positive base is undefined (a negative prior inverts the sign).
    if prior is None or prior <= 0:
        return None
    ratio = safe_div(value, prior)
    return ratio - 1.0 if ratio is not None else None


def canonical_key(*parts: object) -> str:
    return hashlib.sha256("|".join(str(part or "") for part in parts).encode("utf-8")).hexdigest()


def load_universe(conn: Any, ticker_filter: set[str], *, model_family: str, include_historical: bool = False) -> list[str]:
    # Historical members (universe_status='historical') get PIT financial
    # features for research panels; they never enter production scoring, which
    # filters on is_active=1 downstream.
    if include_historical:
        rows = conn.execute(
            """
            SELECT DISTINCT c.ticker
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
            SELECT DISTINCT c.ticker
            FROM dim_company c
            JOIN dim_technology_taxonomy t
              ON t.ticker = c.ticker
             AND t.model_family = ?
            WHERE c.is_active = 1
            ORDER BY c.ticker
            """,
            (model_family,),
        ).fetchall()
    out = [normalize_ticker(row["ticker"]) for row in rows if normalize_ticker(row["ticker"])]
    return [ticker for ticker in out if not ticker_filter or ticker in ticker_filter]


def apply_sign_policy(value: float, sign_policy: str) -> float:
    if sign_policy == "positive_abs":
        return abs(value)
    if sign_policy == "positive" and value < 0:
        return abs(value)
    return value


def reported_currency(unit: str, unit_type: str) -> str:
    unit = str(unit or "").strip().upper()
    if unit_type != "currency":
        return ""
    if len(unit) == 3 and unit.isalpha():
        return unit
    return ""


def source_quality(priority: int, *, is_derived: bool = False) -> float:
    base = max(0.25, 1.0 - max(0, priority - 1) * 0.05)
    return base * (0.75 if is_derived else 1.0)


def load_profile(conn: Any, ticker: str) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM dim_issuer_reporting_profile WHERE ticker = ?", (ticker,)).fetchone()
    return dict(row) if row is not None else {}


def load_concept_map(conn: Any) -> dict[tuple[str, str], list[dict[str, Any]]]:
    rows = conn.execute(
        """
        SELECT canonical_metric, taxonomy, concept, priority, period_type, unit_type,
               sign_policy, currency_required
        FROM dim_xbrl_concept_map
        ORDER BY taxonomy, concept, priority, canonical_metric
        """
    ).fetchall()
    out: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in rows:
        out.setdefault((str(row["taxonomy"]), str(row["concept"])), []).append(dict(row))
    return out


def rebuild_canonical_for_ticker(conn: Any, ticker: str, source_id: str) -> int:
    concept_map = load_concept_map(conn)
    raw_rows = conn.execute(
        """
        SELECT *
        FROM fact_sec_xbrl_fact_raw
        WHERE ticker = ?
          AND source_id = ?
          AND form_type IN ('10-K', '10-K/A', '10-Q', '10-Q/A', '20-F', '20-F/A', '40-F')
          AND COALESCE(accession_number, '') <> ''
          AND COALESCE(end_date, '') <> ''
        ORDER BY accession_number, taxonomy, concept, unit
        """,
        (ticker, source_id),
    ).fetchall()
    now = utc_now()
    inserted = 0
    conn.execute("DELETE FROM fact_financial_statement_canonical WHERE ticker = ? AND source_id = ?", (ticker, source_id))
    for row in raw_rows:
        mappings = concept_map.get((str(row["taxonomy"]), str(row["concept"])), [])
        if not mappings:
            continue
        raw_value = safe_float(row["value"])
        if raw_value is None:
            continue
        end_date = parse_date(row["end_date"])
        if end_date is None:
            continue
        for mapping in mappings:
            value = apply_sign_policy(raw_value, str(mapping["sign_policy"] or "as_reported"))
            unit = str(row["unit"] or "")
            metric = str(mapping["canonical_metric"])
            priority = int(mapping["priority"] or 100)
            key = canonical_key(
                ticker,
                row["accession_number"],
                metric,
                row["taxonomy"],
                row["concept"],
                unit,
                row["start_date"],
                row["end_date"],
                row["frame"],
            )
            conn.execute(
                """
                INSERT INTO fact_financial_statement_canonical(
                    canonical_fact_key, ticker, cik, source_id, period_end_date,
                    period_start_date, fiscal_year, fiscal_period, form_type,
                    filing_date, accession_number, canonical_metric,
                    value_reported_currency, reported_currency, value_usd,
                    source_taxonomy, source_concept, source_unit, source_priority,
                    source_quality, is_direct_reported, is_derived, is_annual_only,
                    source_detail, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, 1, 0, ?, ?, ?, ?)
                ON CONFLICT(canonical_fact_key) DO UPDATE SET
                    value_reported_currency = excluded.value_reported_currency,
                    reported_currency = excluded.reported_currency,
                    source_priority = excluded.source_priority,
                    source_quality = excluded.source_quality,
                    source_detail = excluded.source_detail,
                    updated_at = excluded.updated_at
                """,
                (
                    key,
                    ticker,
                    str(row["cik"] or ""),
                    source_id,
                    end_date.isoformat(),
                    str(row["start_date"] or ""),
                    row["fiscal_year"],
                    str(row["fiscal_period"] or ""),
                    str(row["form_type"] or ""),
                    str(row["filing_date"] or ""),
                    str(row["accession_number"] or ""),
                    metric,
                    value,
                    reported_currency(unit, str(mapping["unit_type"] or "")),
                    str(row["taxonomy"] or ""),
                    str(row["concept"] or ""),
                    unit,
                    priority,
                    source_quality(priority),
                    int(str(row["form_type"] or "") in ANNUAL_FORMS),
                    str(row["source_detail"] or ""),
                    now,
                    now,
                ),
            )
            inserted += 1
    inserted += insert_derived_liabilities(conn, ticker, source_id)
    return inserted


def insert_derived_liabilities(conn: Any, ticker: str, source_id: str) -> int:
    now = utc_now()
    rows = conn.execute(
        """
        SELECT *
        FROM fact_financial_statement_canonical
        WHERE ticker = ? AND source_id = ?
          AND canonical_metric IN ('assets', 'equity', 'liabilities')
        """,
        (ticker, source_id),
    ).fetchall()
    by_key: dict[tuple[str, str], dict[str, sqlite3.Row]] = defaultdict(dict)
    for row in rows:
        by_key[(str(row["accession_number"]), str(row["period_end_date"]))][str(row["canonical_metric"])] = row
    inserted = 0
    for (accession, period_end), metrics in by_key.items():
        if "liabilities" in metrics or "assets" not in metrics or "equity" not in metrics:
            continue
        assets = safe_float(metrics["assets"]["value_reported_currency"])
        equity = safe_float(metrics["equity"]["value_reported_currency"])
        if assets is None or equity is None:
            continue
        currency = str(metrics["assets"]["reported_currency"] or metrics["equity"]["reported_currency"] or "")
        value = assets - equity
        if value < 0:
            continue
        template = metrics["assets"]
        key = canonical_key(ticker, accession, "liabilities", "derived", "assets_minus_equity", period_end)
        conn.execute(
            """
            INSERT INTO fact_financial_statement_canonical(
                canonical_fact_key, ticker, cik, source_id, period_end_date,
                period_start_date, fiscal_year, fiscal_period, form_type,
                filing_date, accession_number, canonical_metric,
                value_reported_currency, reported_currency, value_usd,
                source_taxonomy, source_concept, source_unit, source_priority,
                source_quality, is_direct_reported, is_derived, is_annual_only,
                source_detail, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'liabilities', ?, ?, NULL,
                    'derived', 'assets_minus_equity', ?, 90, ?, 0, 1, ?, ?, ?, ?)
            ON CONFLICT(canonical_fact_key) DO UPDATE SET
                value_reported_currency = excluded.value_reported_currency,
                reported_currency = excluded.reported_currency,
                source_quality = excluded.source_quality,
                source_detail = excluded.source_detail,
                updated_at = excluded.updated_at
            """,
            (
                key,
                ticker,
                str(template["cik"] or ""),
                source_id,
                period_end,
                str(template["period_start_date"] or ""),
                template["fiscal_year"],
                str(template["fiscal_period"] or ""),
                str(template["form_type"] or ""),
                str(template["filing_date"] or ""),
                accession,
                value,
                currency,
                str(template["source_unit"] or ""),
                source_quality(90, is_derived=True),
                int(template["is_annual_only"] or 0),
                str(template["source_detail"] or ""),
                now,
                now,
            ),
        )
        inserted += 1
    return inserted


def load_canonical_facts(conn: Any, ticker: str, source_id: str) -> list[CanonicalFact]:
    rows = conn.execute(
        """
        SELECT *
        FROM fact_financial_statement_canonical
        WHERE ticker = ? AND source_id = ?
        ORDER BY period_end_date, filing_date, accession_number, canonical_metric, source_priority
        """,
        (ticker, source_id),
    ).fetchall()
    out: list[CanonicalFact] = []
    for row in rows:
        value = safe_float(row["value_reported_currency"])
        end_date = parse_date(row["period_end_date"])
        if value is None or end_date is None:
            continue
        out.append(
            CanonicalFact(
                metric=str(row["canonical_metric"] or ""),
                value=value,
                reported_currency=str(row["reported_currency"] or ""),
                source_unit=str(row["source_unit"] or ""),
                source_taxonomy=str(row["source_taxonomy"] or ""),
                source_concept=str(row["source_concept"] or ""),
                source_priority=int(row["source_priority"] or 100),
                source_quality=float(row["source_quality"] or 0.0),
                start_date=parse_date(row["period_start_date"]),
                end_date=end_date,
                filing_date=parse_date(row["filing_date"]),
                accession=str(row["accession_number"] or ""),
                form_type=str(row["form_type"] or ""),
                fiscal_year=int(row["fiscal_year"]) if row["fiscal_year"] is not None else None,
                fiscal_period=str(row["fiscal_period"] or ""),
            )
        )
    return out


def load_filings(conn: Any, ticker: str, source_id: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT accession_number, form_type, filing_date, report_date, fiscal_year, fiscal_period
        FROM fact_sec_filing
        WHERE ticker = ? AND source_id = ?
          AND form_type IN ('10-K', '10-K/A', '10-Q', '10-Q/A', '20-F', '20-F/A', '40-F')
        ORDER BY COALESCE(report_date, filing_date), filing_date
        """,
        (ticker, source_id),
    ).fetchall()
    return [dict(row) for row in rows]


def duration_days(fact: CanonicalFact) -> int | None:
    if fact.start_date is None:
        return None
    return (fact.end_date - fact.start_date).days + 1


def unit_score(metric: str, unit: str) -> int:
    unit_lower = unit.lower()
    if metric in {"eps_basic", "eps_diluted"}:
        return 0 if "share" in unit_lower or "/" in unit_lower else 5
    if metric == "diluted_shares":
        return 0 if "share" in unit_lower else 5
    if metric in FLOW_METRICS or metric in {
        "assets",
        "liabilities",
        "equity",
        "cash_and_equivalents",
        "inventory",
        "accounts_receivable",
        "accounts_payable",
        "debt_current",
        "debt_noncurrent",
        "debt_total",
    }:
        return 5 if unit_lower in {"shares", "pure"} else 0
    return 1


def end_date_distance(fact: CanonicalFact, target_end: date | None) -> int:
    if target_end is None:
        return 99999
    return abs((fact.end_date - target_end).days)


QTD_DURATION = (70, 120)
FY_DURATION = (330, 400)


def select_balance_fact(
    candidates: list[CanonicalFact],
    metric: str,
    *,
    target_end: date | None,
) -> CanonicalFact | None:
    pool = [fact for fact in candidates if fact.metric == metric]
    if not pool:
        return None
    return sorted(
        pool,
        key=lambda fact: (
            fact.source_priority,
            unit_score(metric, fact.source_unit),
            end_date_distance(fact, target_end),
            0 if duration_days(fact) is None else 1,
            -fact.source_quality,
        ),
    )[0]


def select_flow_fact(
    candidates: list[CanonicalFact],
    metric: str,
    *,
    target_end: date | None,
    dur_lo: int,
    dur_hi: int,
    dur_target: int,
) -> CanonicalFact | None:
    """Pick a flow fact by reported duration instead of fiscal-period labels.

    SEC submissions metadata carries no fy/fp, so quarterly-versus-YTD selection
    must rely on the XBRL start/end dates themselves.
    """
    pool = [
        fact
        for fact in candidates
        if fact.metric == metric and duration_days(fact) is not None and dur_lo <= (duration_days(fact) or 0) <= dur_hi
    ]
    if not pool:
        return None
    return sorted(
        pool,
        key=lambda fact: (
            fact.source_priority,
            unit_score(metric, fact.source_unit),
            end_date_distance(fact, target_end),
            abs((duration_days(fact) or 0) - dur_target),
            -fact.source_quality,
        ),
    )[0]


def select_ytd_fact(
    candidates: list[CanonicalFact],
    metric: str,
    *,
    target_end: date | None,
) -> CanonicalFact | None:
    pool = [
        fact
        for fact in candidates
        if fact.metric == metric and duration_days(fact) is not None and (duration_days(fact) or 0) <= 400
    ]
    if not pool:
        return None
    return sorted(
        pool,
        key=lambda fact: (
            fact.source_priority,
            unit_score(metric, fact.source_unit),
            end_date_distance(fact, target_end),
            -(duration_days(fact) or 0),
            -fact.source_quality,
        ),
    )[0]


def shares_outstanding_at(conn: Any, ticker: str, raw_source: str, asof_iso: str) -> float | None:
    """Point-in-time shares outstanding from dei cover-page facts filed on or before asof."""
    row = conn.execute(
        """
        SELECT value
        FROM fact_sec_xbrl_fact_raw
        WHERE ticker = ? AND source_id = ?
          AND taxonomy = 'dei' AND concept = 'EntityCommonStockSharesOutstanding'
          AND COALESCE(filing_date, '') <> '' AND filing_date <= ?
          AND value IS NOT NULL AND value > 0
        ORDER BY filing_date DESC, end_date DESC
        LIMIT 1
        """,
        (ticker, raw_source, asof_iso),
    ).fetchone()
    return safe_float(row["value"]) if row is not None else None


def close_price_at(conn: Any, ticker: str, price_source: str, asof_iso: str, *, max_stale_days: int = 45) -> float | None:
    row = conn.execute(
        """
        SELECT bar_date, close
        FROM fact_price_ohlcv
        WHERE ticker = ? AND source_id = ? AND bar_date <= ? AND close IS NOT NULL AND close > 0
        ORDER BY bar_date DESC
        LIMIT 1
        """,
        (ticker, price_source, asof_iso),
    ).fetchone()
    if row is None:
        return None
    bar_date = parse_date(row["bar_date"])
    asof = parse_date(asof_iso)
    if bar_date is None or asof is None or (asof - bar_date).days > max_stale_days:
        return None
    return safe_float(row["close"])


def pit_market_cap(conn: Any, ticker: str, *, raw_source: str, price_source: str, asof_iso: str, diluted_shares: float | None) -> float | None:
    shares = shares_outstanding_at(conn, ticker, raw_source, asof_iso)
    if shares is None and diluted_shares is not None and diluted_shares > 0:
        shares = diluted_shares
    price = close_price_at(conn, ticker, price_source, asof_iso)
    if shares is None or price is None:
        return None
    return shares * price


def fx_rate_at_or_before(conn: Any, currency: str, asof: date | None, fx_source: str) -> float | None:
    currency = str(currency or "").upper()
    if not currency:
        return None
    if currency == "USD":
        return 1.0
    if asof is None:
        return None
    row = conn.execute(
        """
        SELECT fx_rate
        FROM fact_fx_rate
        WHERE base_currency = ?
          AND quote_currency = 'USD'
          AND source_id = ?
          AND rate_type = 'close'
          AND rate_date <= ?
        ORDER BY rate_date DESC
        LIMIT 1
        """,
        (currency, fx_source, asof.isoformat()),
    ).fetchone()
    return safe_float(row["fx_rate"]) if row is not None else None


def fx_rate_average(conn: Any, currency: str, start: date | None, end: date | None, fx_source: str) -> float | None:
    currency = str(currency or "").upper()
    if not currency:
        return None
    if currency == "USD":
        return 1.0
    if end is None:
        return None
    if start is None or start > end:
        return fx_rate_at_or_before(conn, currency, end, fx_source)
    row = conn.execute(
        """
        SELECT AVG(fx_rate) AS fx_rate
        FROM fact_fx_rate
        WHERE base_currency = ?
          AND quote_currency = 'USD'
          AND source_id = ?
          AND rate_type = 'close'
          AND rate_date BETWEEN ? AND ?
        """,
        (currency, fx_source, start.isoformat(), end.isoformat()),
    ).fetchone()
    rate = safe_float(row["fx_rate"]) if row is not None else None
    return rate if rate is not None else fx_rate_at_or_before(conn, currency, end, fx_source)


def apply_fx_conversion(conn: Any, feature: dict[str, Any], selected_facts: dict[str, CanonicalFact], fx_source: str) -> None:
    currency = str(feature.get("reported_currency") or "").upper()
    for metric in FLOW_USD_METRICS | BALANCE_USD_METRICS:
        feature[f"{metric}_usd"] = None
    if not currency:
        feature["fx_conversion_status"] = "missing_reported_currency"
        feature["fx_rate_income_statement"] = None
        feature["fx_rate_balance_sheet"] = None
        return
    if currency in {"USD", "USN", "USS"}:
        feature["fx_conversion_status"] = "usd_native"
        feature["fx_rate_income_statement"] = 1.0
        feature["fx_rate_balance_sheet"] = 1.0
    else:
        flow_anchor = selected_facts.get("revenue") or selected_facts.get("operating_cash_flow") or selected_facts.get("net_income")
        balance_anchor = selected_facts.get("assets") or selected_facts.get("cash_and_equivalents") or selected_facts.get("inventory")
        flow_rate = fx_rate_average(conn, currency, flow_anchor.start_date if flow_anchor else None, flow_anchor.end_date if flow_anchor else None, fx_source)
        balance_rate = fx_rate_at_or_before(conn, currency, balance_anchor.end_date if balance_anchor else None, fx_source)
        feature["fx_rate_income_statement"] = flow_rate
        feature["fx_rate_balance_sheet"] = balance_rate
        feature["fx_conversion_status"] = "converted" if flow_rate is not None and balance_rate is not None else "missing_fx_rate"
    flow_rate = safe_float(feature.get("fx_rate_income_statement"))
    balance_rate = safe_float(feature.get("fx_rate_balance_sheet"))
    for metric in FLOW_USD_METRICS:
        value = safe_float(feature.get(metric))
        feature[f"{metric}_usd"] = value * flow_rate if value is not None and flow_rate is not None else None
    for metric in BALANCE_USD_METRICS:
        value = safe_float(feature.get(metric))
        feature[f"{metric}_usd"] = value * balance_rate if value is not None and balance_rate is not None else None


def add_issue(conn: Any, ticker: str, source_id: str, issue_type: str, detail: str) -> None:
    now = utc_now()
    row = conn.execute("SELECT company_id FROM dim_company WHERE ticker = ?", (ticker,)).fetchone()
    company_id = int(row["company_id"]) if row is not None else None
    conn.execute(
        """
        INSERT INTO data_quality_issues(
            detected_at, severity, stage, ticker, company_id, source_id, issue_type,
            issue_detail, resolution_status, created_at, updated_at
        )
        VALUES (?, 'warning', ?, ?, ?, ?, ?, ?, 'open', ?, ?)
        """,
        (now, RUN_TYPE, ticker, company_id, source_id, issue_type, detail, now, now),
    )


def financial_sanity_issues(feature: dict[str, Any]) -> list[str]:
    issues: list[str] = []
    revenue = safe_float(feature.get("revenue"))
    if revenue is not None and revenue <= 0:
        issues.append("revenue_non_positive")
    for metric in ("gross_margin", "operating_margin"):
        value = safe_float(feature.get(metric))
        if value is not None and (value > 1.0 or value < -1.0):
            issues.append(f"{metric}_outside_100pct")
    fcf_margin = safe_float(feature.get("fcf_margin"))
    if fcf_margin is not None and (fcf_margin > 2.0 or fcf_margin < -2.0):
        issues.append("fcf_margin_outside_200pct")
    assets = safe_float(feature.get("assets"))
    liabilities = safe_float(feature.get("liabilities"))
    equity = safe_float(feature.get("equity"))
    if assets is not None and liabilities is not None and equity is not None and assets > 0:
        mismatch = abs(assets - liabilities - equity) / assets
        if mismatch > 0.05:
            issues.append(f"balance_sheet_identity_mismatch_{mismatch:.4f}")
    capex = safe_float(feature.get("capex"))
    if capex is not None and capex < 0:
        issues.append("capex_negative_after_normalization")
    return issues


def upsert_feature(conn: Any, feature: dict[str, Any]) -> None:
    now = utc_now()
    fields = [
        "ticker", "asof_date", "source_id", "model_family", "accession_number", "form_type",
        "fiscal_period_end", "fiscal_year", "fiscal_period", "reporting_standard",
        "financial_frequency", "reported_currency", "fx_conversion_status",
        "fx_rate_income_statement", "fx_rate_balance_sheet",
        "revenue", "gross_profit", "operating_income", "net_income",
        "eps_diluted", "assets", "liabilities", "equity", "cash_and_equivalents",
        "total_debt", "inventory", "accounts_receivable", "accounts_payable",
        "operating_cash_flow", "capex", "free_cash_flow",
        "research_and_development", "stock_based_compensation", "diluted_shares",
        "revenue_usd", "gross_profit_usd", "operating_income_usd", "net_income_usd",
        "operating_cash_flow_usd", "capex_usd", "free_cash_flow_usd",
        "assets_usd", "liabilities_usd", "equity_usd", "cash_and_equivalents_usd",
        "total_debt_usd", "inventory_usd", "accounts_receivable_usd", "accounts_payable_usd",
        "deferred_revenue", "remaining_performance_obligation",
        "revenue_ttm", "gross_profit_ttm", "operating_income_ttm", "net_income_ttm",
        "free_cash_flow_ttm", "gross_margin", "operating_margin", "fcf_margin",
        "r_and_d_pct_revenue", "sbc_pct_revenue", "net_cash", "net_cash_to_assets",
        "inventory_days", "days_sales_outstanding", "days_payables_outstanding",
        "cash_conversion_cycle", "revenue_yoy_growth", "gross_profit_yoy_growth",
        "operating_income_yoy_growth", "free_cash_flow_yoy_growth", "revenue_acceleration",
        "market_cap", "ev_gross_profit", "ev_operating_income", "fcf_yield",
        "canonical_quality", "data_quality_status",
    ]
    values = [feature.get(field) for field in fields] + [now, now]
    update_clause = ",\n                ".join(f"{field} = excluded.{field}" for field in fields[4:])
    conn.execute(
        f"""
        INSERT INTO feature_financial_statement(
            {", ".join(fields)}, created_at, updated_at
        )
        VALUES ({", ".join("?" for _ in values)})
        ON CONFLICT(ticker, asof_date, source_id, model_family, fiscal_period_end) DO UPDATE SET
            {update_clause},
            updated_at = excluded.updated_at
        """,
        values,
    )


def write_report(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS, extrasaction="ignore", lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


FLOW_EXTRACT_METRICS = (
    "revenue", "cost_of_sales", "gross_profit", "operating_income", "pretax_income",
    "net_income", "operating_cash_flow", "capex", "research_and_development",
    "stock_based_compensation", "eps_diluted", "diluted_shares",
)
BALANCE_EXTRACT_METRICS = (
    "assets", "liabilities", "equity", "cash_and_equivalents", "inventory",
    "accounts_receivable", "accounts_payable",
    "debt_current", "debt_noncurrent", "debt_total",
    "deferred_revenue_current", "deferred_revenue_noncurrent", "deferred_revenue_total",
    "remaining_performance_obligation",
)
TTM_METRICS = (
    "revenue", "cost_of_sales", "gross_profit", "operating_income", "net_income",
    "operating_cash_flow", "capex", "free_cash_flow", "research_and_development",
    "stock_based_compensation",
)
YOY_METRICS = (
    ("revenue", "revenue_yoy_growth"),
    ("gross_profit", "gross_profit_yoy_growth"),
    ("operating_income", "operating_income_yoy_growth"),
    ("free_cash_flow", "free_cash_flow_yoy_growth"),
)


def _derive_levels(values: dict[str, float | None]) -> None:
    """Fill gross_profit and free_cash_flow within one duration class."""
    if values.get("gross_profit") is None and values.get("revenue") is not None and values.get("cost_of_sales") is not None:
        values["gross_profit"] = float(values["revenue"]) - float(values["cost_of_sales"])
    capex = values.get("capex")
    if capex is not None:
        values["capex"] = abs(float(capex))
    if values.get("operating_cash_flow") is not None and values.get("capex") is not None:
        values["free_cash_flow"] = float(values["operating_cash_flow"]) - float(values["capex"])
    else:
        values.setdefault("free_cash_flow", None)


def _quarter_value(feature: dict[str, Any], metric: str) -> float | None:
    """Single-quarter flow: the row value for quarterly rows, derived Q4 for annual rows."""
    if feature["_is_annual"]:
        return feature["_q4"].get(metric)
    return safe_float(feature.get(metric))


def _find_prior_year_row(features: list[dict[str, Any]], idx: int, *, annual: bool) -> dict[str, Any] | None:
    cur_end: date = features[idx]["_end"]
    best = None
    for candidate in features[:idx]:
        if candidate["_is_annual"] != annual:
            continue
        gap = (cur_end - candidate["_end"]).days
        if 330 <= gap <= 400:
            best = candidate
    return best


def _find_prev_same_class_row(features: list[dict[str, Any]], idx: int) -> dict[str, Any] | None:
    cur = features[idx]
    lo, hi = (330, 400) if cur["_is_annual"] else (60, 130)
    for candidate in reversed(features[:idx]):
        if candidate["_is_annual"] != cur["_is_annual"]:
            continue
        gap = (cur["_end"] - candidate["_end"]).days
        if lo <= gap <= hi:
            return candidate
        if gap > hi:
            return None
    return None


def _ttm_value(features: list[dict[str, Any]], idx: int, metric: str) -> float | None:
    feature = features[idx]
    if feature["_is_annual"]:
        return safe_float(feature.get(metric))
    # Preferred: latest annual + current YTD - prior-year YTD over the same span.
    cur_ytd = feature["_ytd"].get(metric)
    if cur_ytd is not None:
        fy_row = None
        for candidate in reversed(features[:idx]):
            if not candidate["_is_annual"]:
                continue
            gap = (feature["_end"] - candidate["_end"]).days
            if 0 < gap <= 380 and safe_float(candidate.get(metric)) is not None:
                fy_row = candidate
            break
        if fy_row is not None:
            for candidate in reversed(features[:idx]):
                if candidate["_is_annual"]:
                    continue
                gap = (feature["_end"] - candidate["_end"]).days
                if gap > 400:
                    break
                prior_ytd = candidate["_ytd"].get(metric)
                if (
                    330 <= gap <= 400
                    and prior_ytd is not None
                    and abs(candidate["_ytd_days"].get(metric, 0) - feature["_ytd_days"].get(metric, 0)) <= 20
                ):
                    return float(fy_row[metric]) + float(cur_ytd) - float(prior_ytd)
    # Fallback: sum of four consecutive single-quarter values.
    total = 0.0
    count = 0
    expected_end = feature["_end"]
    pool = [feature] + list(reversed(features[:idx]))
    for candidate in pool:
        gap = (expected_end - candidate["_end"]).days
        if gap < 0 or gap > 130:
            continue
        value = _quarter_value(candidate, metric)
        if value is None:
            return None
        total += value
        count += 1
        if count == 4:
            return total
        expected_end = candidate["_end"] - timedelta(days=1)
    return None


def build_ticker_features(
    conn: Any,
    ticker: str,
    *,
    facts_source: str,
    filings_source: str,
    fx_source: str,
    price_source: str,
    model_family: str,
) -> list[dict[str, Any]]:
    profile = load_profile(conn, ticker)
    reporting_standard = str(profile.get("primary_reporting_taxonomy") or "")
    financial_frequency = str(profile.get("financial_statement_frequency") or "")
    facts = load_canonical_facts(conn, ticker, facts_source)
    by_accession: dict[str, list[CanonicalFact]] = defaultdict(list)
    for fact in facts:
        by_accession[fact.accession].append(fact)
    features: list[dict[str, Any]] = []
    for filing in load_filings(conn, ticker, filings_source):
        accession = str(filing.get("accession_number") or "")
        filing_facts = by_accession.get(accession, [])
        report_date = parse_date(filing.get("report_date"))
        filing_date = parse_date(filing.get("filing_date"))
        if not accession or not filing_facts or report_date is None or filing_date is None:
            continue
        form_type = str(filing.get("form_type") or "")
        is_annual = form_type.startswith(("10-K", "20-F", "40-F"))
        selected_facts: dict[str, CanonicalFact] = {}
        metric_values: dict[str, float | None] = {}
        ytd_values: dict[str, float | None] = {}
        ytd_starts: dict[str, str] = {}
        ytd_days: dict[str, int] = {}
        for metric in FLOW_EXTRACT_METRICS:
            if is_annual:
                selected = select_flow_fact(filing_facts, metric, target_end=report_date, dur_lo=FY_DURATION[0], dur_hi=FY_DURATION[1], dur_target=365)
                if selected is None:
                    selected = select_ytd_fact(filing_facts, metric, target_end=report_date)
                if selected is not None:
                    selected_facts[metric] = selected
                    metric_values[metric] = selected.value
                    ytd_values[metric] = selected.value
                    ytd_starts[metric] = selected.start_date.isoformat() if selected.start_date else ""
                    ytd_days[metric] = duration_days(selected) or 0
                else:
                    metric_values[metric] = None
            else:
                qtd = select_flow_fact(filing_facts, metric, target_end=report_date, dur_lo=QTD_DURATION[0], dur_hi=QTD_DURATION[1], dur_target=91)
                ytd = select_ytd_fact(filing_facts, metric, target_end=report_date)
                anchor = qtd or ytd
                if anchor is not None:
                    selected_facts[metric] = anchor
                metric_values[metric] = qtd.value if qtd is not None else None
                if ytd is not None:
                    ytd_values[metric] = ytd.value
                    ytd_starts[metric] = ytd.start_date.isoformat() if ytd.start_date else ""
                    ytd_days[metric] = duration_days(ytd) or 0
        for metric in BALANCE_EXTRACT_METRICS:
            selected = select_balance_fact(filing_facts, metric, target_end=report_date)
            if selected is not None:
                selected_facts[metric] = selected
            metric_values[metric] = selected.value if selected else None
        _derive_levels(metric_values)
        _derive_levels(ytd_values)
        if "free_cash_flow" in ytd_values and ytd_values.get("free_cash_flow") is not None:
            ytd_starts.setdefault("free_cash_flow", ytd_starts.get("operating_cash_flow", ""))
            ytd_days.setdefault("free_cash_flow", ytd_days.get("operating_cash_flow", 0))
        if ytd_values.get("gross_profit") is not None:
            ytd_starts.setdefault("gross_profit", ytd_starts.get("revenue", ""))
            ytd_days.setdefault("gross_profit", ytd_days.get("revenue", 0))
        total_debt = metric_values.get("debt_total")
        if total_debt is None and (metric_values.get("debt_current") is not None or metric_values.get("debt_noncurrent") is not None):
            total_debt = float(metric_values.get("debt_current") or 0.0) + float(metric_values.get("debt_noncurrent") or 0.0)
        currency_candidates = [
            selected_facts[metric].reported_currency
            for metric in ("revenue", "assets", "operating_cash_flow")
            if metric in selected_facts and selected_facts[metric].reported_currency
        ]
        statement_currency = currency_candidates[0] if currency_candidates else ""
        canonical_quality = "direct"
        if any(fact.source_taxonomy == "derived" or fact.source_quality < 0.9 for fact in selected_facts.values()):
            canonical_quality = "mixed_or_derived"
        feature = {
            "ticker": ticker,
            "asof_date": filing_date.isoformat(),
            "source_id": facts_source,
            "model_family": model_family,
            "accession_number": accession,
            "form_type": form_type,
            "fiscal_period_end": report_date.isoformat(),
            "fiscal_year": filing.get("fiscal_year"),
            "fiscal_period": str(filing.get("fiscal_period") or ""),
            "reporting_standard": reporting_standard,
            "financial_frequency": financial_frequency,
            "reported_currency": statement_currency,
            "revenue": metric_values.get("revenue"),
            "gross_profit": metric_values.get("gross_profit"),
            "operating_income": metric_values.get("operating_income"),
            "net_income": metric_values.get("net_income"),
            "eps_diluted": metric_values.get("eps_diluted"),
            "assets": metric_values.get("assets"),
            "liabilities": metric_values.get("liabilities"),
            "equity": metric_values.get("equity"),
            "cash_and_equivalents": metric_values.get("cash_and_equivalents"),
            "inventory": metric_values.get("inventory"),
            "accounts_receivable": metric_values.get("accounts_receivable"),
            "accounts_payable": metric_values.get("accounts_payable"),
            "operating_cash_flow": metric_values.get("operating_cash_flow"),
            "capex": metric_values.get("capex"),
            "total_debt": total_debt,
            "free_cash_flow": metric_values.get("free_cash_flow"),
            "research_and_development": metric_values.get("research_and_development"),
            "stock_based_compensation": metric_values.get("stock_based_compensation"),
            "diluted_shares": metric_values.get("diluted_shares"),
            "cost_of_sales": metric_values.get("cost_of_sales"),
            "deferred_revenue_current": metric_values.get("deferred_revenue_current"),
            "deferred_revenue_noncurrent": metric_values.get("deferred_revenue_noncurrent"),
            "deferred_revenue_total": metric_values.get("deferred_revenue_total"),
            "remaining_performance_obligation": metric_values.get("remaining_performance_obligation"),
            "canonical_quality": canonical_quality,
            "data_quality_status": "review",
            "_is_annual": is_annual,
            "_end": report_date,
            "_ytd": ytd_values,
            "_ytd_start": ytd_starts,
            "_ytd_days": ytd_days,
            "_q4": {},
        }
        apply_fx_conversion(conn, feature, selected_facts, fx_source)
        features.append(feature)
    features.sort(key=lambda row: (str(row["fiscal_period_end"]), str(row["asof_date"]), str(row["accession_number"])))
    # Pass 1: derive missing single-quarter flows by differencing same-start YTD spans,
    # and derive the final-quarter flow embedded in each annual filing.
    for idx, feature in enumerate(features):
        for metric in TTM_METRICS:
            cur_ytd = feature["_ytd"].get(metric)
            cur_start = feature["_ytd_start"].get(metric, "")
            cur_days = feature["_ytd_days"].get(metric, 0)
            if cur_ytd is None or not cur_start:
                continue
            target = None
            for candidate in reversed(features[:idx]):
                if (feature["_end"] - candidate["_end"]).days > 130:
                    break
                prior_ytd = candidate["_ytd"].get(metric)
                if (
                    prior_ytd is not None
                    and not candidate["_is_annual"]
                    and candidate["_ytd_start"].get(metric, "") == cur_start
                    and QTD_DURATION[0] <= cur_days - candidate["_ytd_days"].get(metric, 0) <= QTD_DURATION[1]
                ):
                    target = float(cur_ytd) - float(prior_ytd)
                    break
            if target is None:
                continue
            if feature["_is_annual"]:
                feature["_q4"][metric] = target
            elif feature.get(metric) is None:
                feature[metric] = target
        if not feature["_is_annual"] and feature.get("free_cash_flow") is None:
            if feature.get("operating_cash_flow") is not None and feature.get("capex") is not None:
                feature["free_cash_flow"] = float(feature["operating_cash_flow"]) - abs(float(feature["capex"]))
    # Pass 2: growth, TTM, margins, and point-in-time valuation.
    for idx, feature in enumerate(features):
        prior_year = _find_prior_year_row(features, idx, annual=feature["_is_annual"])
        for metric, out_name in YOY_METRICS:
            feature[out_name] = pct_change(
                safe_float(feature.get(metric)),
                safe_float(prior_year.get(metric)) if prior_year else None,
            )
        feature["revenue_acceleration"] = None
        prev_row = _find_prev_same_class_row(features, idx)
        if (
            feature.get("revenue_yoy_growth") is not None
            and prev_row is not None
            and prev_row.get("revenue_yoy_growth") is not None
        ):
            feature["revenue_acceleration"] = float(feature["revenue_yoy_growth"]) - float(prev_row["revenue_yoy_growth"])
        for metric in TTM_METRICS:
            feature[f"{metric}_ttm"] = _ttm_value(features, idx, metric)
        revenue_ttm = safe_float(feature.get("revenue_ttm"))
        revenue_period = safe_float(feature.get("revenue"))
        def _ratio(metric: str) -> float | None:
            ttm = safe_float(feature.get(f"{metric}_ttm"))
            if ttm is not None and revenue_ttm:
                return safe_div(ttm, revenue_ttm)
            return safe_div(safe_float(feature.get(metric)), revenue_period)
        feature["gross_margin"] = _ratio("gross_profit")
        feature["operating_margin"] = _ratio("operating_income")
        feature["fcf_margin"] = _ratio("free_cash_flow")
        feature["r_and_d_pct_revenue"] = _ratio("research_and_development")
        feature["sbc_pct_revenue"] = _ratio("stock_based_compensation")
        feature["net_cash"] = None
        if feature.get("cash_and_equivalents") is not None or feature.get("total_debt") is not None:
            feature["net_cash"] = float(feature.get("cash_and_equivalents") or 0.0) - float(feature.get("total_debt") or 0.0)
        # Deferred revenue = current + noncurrent contract liabilities (ASC 606 or
        # legacy split); fall back to the reported total when the split is absent.
        dr_current = safe_float(feature.get("deferred_revenue_current"))
        dr_noncurrent = safe_float(feature.get("deferred_revenue_noncurrent"))
        dr_total = safe_float(feature.get("deferred_revenue_total"))
        if dr_current is not None and dr_noncurrent is not None:
            feature["deferred_revenue"] = dr_current + dr_noncurrent
        elif dr_total is not None:
            feature["deferred_revenue"] = dr_total
        elif dr_current is not None:
            feature["deferred_revenue"] = dr_current
        else:
            feature["deferred_revenue"] = dr_noncurrent
        feature["net_cash_to_assets"] = safe_div(safe_float(feature.get("net_cash")), safe_float(feature.get("assets")))
        cogs_ttm = safe_float(feature.get("cost_of_sales_ttm"))
        if cogs_ttm is None and revenue_ttm is not None and safe_float(feature.get("gross_profit_ttm")) is not None:
            cogs_ttm = revenue_ttm - float(feature["gross_profit_ttm"])
        feature["inventory_days"] = None
        if feature.get("inventory") is not None and cogs_ttm and cogs_ttm > 0:
            feature["inventory_days"] = float(feature["inventory"]) / cogs_ttm * 365.0
        feature["days_sales_outstanding"] = None
        if feature.get("accounts_receivable") is not None and revenue_ttm and revenue_ttm > 0:
            feature["days_sales_outstanding"] = float(feature["accounts_receivable"]) / revenue_ttm * 365.0
        feature["days_payables_outstanding"] = None
        if feature.get("accounts_payable") is not None and cogs_ttm and cogs_ttm > 0:
            feature["days_payables_outstanding"] = float(feature["accounts_payable"]) / cogs_ttm * 365.0
        feature["cash_conversion_cycle"] = None
        if (
            feature.get("inventory_days") is not None
            and feature.get("days_sales_outstanding") is not None
            and feature.get("days_payables_outstanding") is not None
        ):
            feature["cash_conversion_cycle"] = (
                float(feature["inventory_days"])
                + float(feature["days_sales_outstanding"])
                - float(feature["days_payables_outstanding"])
            )
        # Point-in-time valuation: shares-from-filings x close price at the filing date.
        market_cap = pit_market_cap(
            conn,
            ticker,
            raw_source=facts_source,
            price_source=price_source,
            asof_iso=str(feature["asof_date"]),
            diluted_shares=safe_float(feature.get("diluted_shares")),
        )
        feature["market_cap"] = market_cap
        valuation_currency_ready = feature.get("fx_conversion_status") in {"usd_native", "converted"}
        flow_rate = safe_float(feature.get("fx_rate_income_statement"))
        balance_rate = safe_float(feature.get("fx_rate_balance_sheet"))
        feature["ev_gross_profit"] = None
        feature["ev_operating_income"] = None
        feature["fcf_yield"] = None
        if market_cap is not None and valuation_currency_ready and flow_rate is not None and balance_rate is not None:
            # EV requires both balance-sheet legs; a missing value is not a zero.
            total_debt = safe_float(feature.get("total_debt"))
            cash_and_equivalents = safe_float(feature.get("cash_and_equivalents"))
            enterprise_value = None
            if total_debt is not None and cash_and_equivalents is not None:
                enterprise_value = market_cap + total_debt * balance_rate - cash_and_equivalents * balance_rate
            gp_ttm_usd = safe_float(feature.get("gross_profit_ttm"))
            oi_ttm_usd = safe_float(feature.get("operating_income_ttm"))
            fcf_ttm_usd = safe_float(feature.get("free_cash_flow_ttm"))
            gp_ttm_usd = gp_ttm_usd * flow_rate if gp_ttm_usd is not None else None
            oi_ttm_usd = oi_ttm_usd * flow_rate if oi_ttm_usd is not None else None
            fcf_ttm_usd = fcf_ttm_usd * flow_rate if fcf_ttm_usd is not None else None
            # Ratios only when the denominator is positive: a negative stored ratio
            # then unambiguously means negative enterprise value (favorably cheap).
            if enterprise_value is not None and gp_ttm_usd is not None and gp_ttm_usd > 0:
                feature["ev_gross_profit"] = enterprise_value / gp_ttm_usd
            if enterprise_value is not None and oi_ttm_usd is not None and oi_ttm_usd > 0:
                feature["ev_operating_income"] = enterprise_value / oi_ttm_usd
            if fcf_ttm_usd is not None and market_cap > 0:
                feature["fcf_yield"] = fcf_ttm_usd / market_cap
        reasons = [f"missing_{metric}" for metric in sorted(CRITICAL_METRICS) if feature.get(metric) is None]
        statement_currency = str(feature.get("reported_currency") or "")
        if statement_currency and statement_currency != "USD" and feature.get("fx_conversion_status") != "converted":
            reasons.append(f"valuation_fx_unconverted_{statement_currency}")
        if not statement_currency:
            reasons.append("missing_reported_currency")
        if market_cap is None:
            reasons.append("missing_pit_market_cap")
        feature["data_quality_status"] = "complete" if not reasons else "review"
        feature["_review_reason"] = ";".join(reasons)
    for feature in features:
        for key in ("_is_annual", "_end", "_ytd", "_ytd_start", "_ytd_days", "_q4", "cost_of_sales", "cost_of_sales_ttm"):
            feature.pop(key, None)
    return features


def main() -> None:
    configure_utc_logging()
    args = parse_args()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    base_dir = config_path.parent
    db_path = args.db.expanduser().resolve() if args.db else resolve_path(cfg_get(config, "paths.database_path"), base_dir=base_dir)
    output_csv = args.output_csv.expanduser().resolve() if args.output_csv else resolve_path(cfg_get(config, "sec_fundamentals.feature_output_csv"), base_dir=base_dir)
    facts_source = str(cfg_get(config, "sec_fundamentals.companyfacts_source_id", "sec_companyfacts"))
    filings_source = str(cfg_get(config, "sec_fundamentals.submissions_source_id", "sec_submissions"))
    fx_source = str(cfg_get(config, "fx_rates.source_id", "yahoo_fx_rates"))
    price_source = str(cfg_get(config, "market_feature_build.source_id", "yahoo_finance_adjusted") or "yahoo_finance_adjusted")
    model_family = str(
        args.model_family
        or cfg_get(config, "technology_universe.initial_subsector", "semiconductors")
        or "semiconductors"
    ).strip()
    if not model_family:
        raise ValueError("model_family cannot be empty")
    ticker_filter = {normalize_ticker(x) for x in args.tickers.split(",") if normalize_ticker(x)}
    report_rows: list[dict[str, Any]] = []
    with connect(db_path, timeout_sec=float(cfg_get(config, "runtime.sqlite_timeout_sec", 30.0))) as conn:
        init_db(conn)
        run_id = start_run(conn, run_type=RUN_TYPE, input_path=config_path)
        try:
            include_historical = str(cfg_get(config, "sec_fundamentals.include_historical_members", True)).strip().lower() in {"1", "true", "yes", "y"}
            tickers = load_universe(conn, ticker_filter, model_family=model_family, include_historical=include_historical)
            if not tickers:
                raise ValueError(f"No SEC feature universe tickers found for model_family={model_family}.")
            with conn:
                placeholders = ",".join("?" for _ in tickers)
                conn.execute(f"DELETE FROM data_quality_issues WHERE stage = ? AND ticker IN ({placeholders})", (RUN_TYPE, *tickers))
                for ticker in tickers:
                    canonical_count = rebuild_canonical_for_ticker(conn, ticker, facts_source)
                    features = build_ticker_features(
                        conn,
                        ticker,
                        facts_source=facts_source,
                        filings_source=filings_source,
                        fx_source=fx_source,
                        price_source=price_source,
                        model_family=model_family,
                    )
                    if not features:
                        add_issue(conn, ticker, facts_source, "missing_financial_features", "No canonical SEC financial features could be built.")
                    rows: list[dict[str, Any]] = []
                    for feature in features:
                        sanity_issues = financial_sanity_issues(feature)
                        review_reason = str(feature.pop("_review_reason", "") or "")
                        if sanity_issues:
                            feature["data_quality_status"] = "review"
                            review_reason = ";".join(part for part in [review_reason, *sanity_issues] if part)
                        upsert_feature(conn, feature)
                        if feature.get("data_quality_status") != "complete":
                            missing = [metric for metric in CRITICAL_METRICS if feature.get(metric) is None]
                            details = []
                            if missing:
                                details.append(f"missing={','.join(sorted(missing))}")
                            if feature.get("canonical_quality") == "mixed_or_derived":
                                details.append("mixed_or_derived_canonical_facts")
                            if feature.get("fx_conversion_status") in {"missing_fx_rate", "missing_reported_currency"}:
                                details.append(str(feature.get("fx_conversion_status")))
                            details.extend(sanity_issues)
                            add_issue(conn, ticker, facts_source, "financial_feature_review", ";".join(details) or "review")
                        rows.append({**{field: feature.get(field) for field in CSV_FIELDS}, "ticker": ticker, "review_reason": review_reason})
                    report_rows.extend(rows[-8:])
                    LOGGER.info("%s canonical_rows=%d financial_feature_rows=%d", ticker, canonical_count, len(features))
            write_report(output_csv, report_rows)
            finish_run(conn, run_id=run_id, status="success", row_count=len(report_rows), message=f"tickers={len(tickers)} report_rows={len(report_rows)} output={output_csv}")
            LOGGER.info("Wrote financial feature coverage report: %s", output_csv)
            LOGGER.info("Financial feature build complete: tickers=%d report_rows=%d", len(tickers), len(report_rows))
        except BaseException as exc:
            finish_run(conn, run_id=run_id, status="failed", row_count=0, message=f"{type(exc).__name__}: {exc}")
            raise


if __name__ == "__main__":
    main()
