#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import hashlib
import logging
import math
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime
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
    ratio = safe_div(value, prior)
    return ratio - 1.0 if ratio is not None else None


def canonical_key(*parts: object) -> str:
    return hashlib.sha256("|".join(str(part or "") for part in parts).encode("utf-8")).hexdigest()


def load_universe(conn: Any, ticker_filter: set[str]) -> list[str]:
    rows = conn.execute("SELECT ticker FROM dim_company WHERE is_active = 1 ORDER BY ticker").fetchall()
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
    by_key: dict[tuple[str, str], dict[str, sqlite3.Row]] = defaultdict(dict)  # type: ignore[name-defined]
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
    if metric in FLOW_METRICS or metric in {"assets", "liabilities", "equity", "cash_and_equivalents", "inventory", "debt_current", "debt_noncurrent", "debt_total"}:
        return 5 if unit_lower in {"shares", "pure"} else 0
    return 1


def end_date_distance(fact: CanonicalFact, target_end: date | None) -> int:
    if target_end is None:
        return 99999
    return abs((fact.end_date - target_end).days)


def select_fact(
    candidates: list[CanonicalFact],
    metric: str,
    *,
    form_type: str,
    fiscal_period: str,
    target_end: date | None,
) -> CanonicalFact | None:
    candidates = [fact for fact in candidates if fact.metric == metric]
    if not candidates:
        return None
    if metric in FLOW_METRICS:
        if form_type.startswith("10-Q") and fiscal_period.upper() in {"Q1", "Q2", "Q3"}:
            return sorted(
                candidates,
                key=lambda fact: (
                    fact.source_priority,
                    unit_score(metric, fact.source_unit),
                    end_date_distance(fact, target_end),
                    abs((duration_days(fact) or 999) - 91),
                    -(duration_days(fact) or 0),
                    -fact.source_quality,
                ),
            )[0]
        if form_type.startswith(("10-K", "20-F", "40-F")) or fiscal_period.upper() == "FY":
            return sorted(
                candidates,
                key=lambda fact: (
                    fact.source_priority,
                    unit_score(metric, fact.source_unit),
                    end_date_distance(fact, target_end),
                    abs((duration_days(fact) or 999) - 365),
                    -(duration_days(fact) or 0),
                    -fact.source_quality,
                ),
            )[0]
        return sorted(
            candidates,
            key=lambda fact: (
                fact.source_priority,
                unit_score(metric, fact.source_unit),
                end_date_distance(fact, target_end),
                -1 * (duration_days(fact) or 0),
                -fact.source_quality,
            ),
        )[0]
    return sorted(
        candidates,
        key=lambda fact: (
            fact.source_priority,
            unit_score(metric, fact.source_unit),
            end_date_distance(fact, target_end),
            0 if duration_days(fact) is None else 1,
            -fact.source_quality,
        ),
    )[0]


def flow_ttm(features: list[dict[str, Any]], idx: int, metric: str) -> float | None:
    current = features[idx]
    if str(current.get("form_type") or "").startswith(("10-K", "20-F", "40-F")):
        return safe_float(current.get(metric))
    rows: list[dict[str, Any]] = []
    for prior in reversed(features[: idx + 1]):
        if safe_float(prior.get(metric)) is None:
            continue
        rows.append(prior)
        if len(rows) == 4:
            break
    if len(rows) < 4:
        return None
    return sum(float(row[metric]) for row in rows if row.get(metric) is not None)


def latest_market_cap(conn: Any, ticker: str, asof_date: str) -> float | None:
    row = conn.execute(
        """
        SELECT market_cap
        FROM fact_market_snapshot
        WHERE ticker = ? AND asof_date <= ? AND market_cap IS NOT NULL
        ORDER BY asof_date DESC
        LIMIT 1
        """,
        (ticker, asof_date),
    ).fetchone()
    return safe_float(row["market_cap"]) if row is not None else None


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
        "total_debt", "inventory", "operating_cash_flow", "capex", "free_cash_flow",
        "research_and_development", "stock_based_compensation", "diluted_shares",
        "revenue_usd", "gross_profit_usd", "operating_income_usd", "net_income_usd",
        "operating_cash_flow_usd", "capex_usd", "free_cash_flow_usd",
        "assets_usd", "liabilities_usd", "equity_usd", "cash_and_equivalents_usd",
        "total_debt_usd", "inventory_usd",
        "revenue_ttm", "gross_profit_ttm", "operating_income_ttm", "net_income_ttm",
        "free_cash_flow_ttm", "gross_margin", "operating_margin", "fcf_margin",
        "r_and_d_pct_revenue", "sbc_pct_revenue", "net_cash", "net_cash_to_assets",
        "inventory_days", "revenue_yoy_growth", "gross_profit_yoy_growth",
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


def build_ticker_features(
    conn: Any,
    ticker: str,
    *,
    facts_source: str,
    filings_source: str,
    fx_source: str,
    model_family: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    profile = load_profile(conn, ticker)
    reporting_standard = str(profile.get("primary_reporting_taxonomy") or "")
    financial_frequency = str(profile.get("financial_statement_frequency") or "")
    facts = load_canonical_facts(conn, ticker, facts_source)
    by_accession: dict[str, list[CanonicalFact]] = defaultdict(list)
    for fact in facts:
        by_accession[fact.accession].append(fact)
    features: list[dict[str, Any]] = []
    report_rows: list[dict[str, Any]] = []
    for filing in load_filings(conn, ticker, filings_source):
        accession = str(filing.get("accession_number") or "")
        filing_facts = by_accession.get(accession, [])
        report_date = parse_date(filing.get("report_date"))
        filing_date = parse_date(filing.get("filing_date"))
        if not accession or not filing_facts or report_date is None or filing_date is None:
            continue
        form_type = str(filing.get("form_type") or "")
        fiscal_period = str(filing.get("fiscal_period") or "")
        selected_facts: dict[str, CanonicalFact] = {}
        metric_values: dict[str, float | None] = {}
        for metric in {
            "revenue", "cost_of_sales", "gross_profit", "operating_income", "pretax_income",
            "net_income", "eps_diluted", "assets", "liabilities", "equity",
            "cash_and_equivalents", "inventory", "operating_cash_flow", "capex",
            "research_and_development", "stock_based_compensation", "diluted_shares",
            "debt_current", "debt_noncurrent", "debt_total",
        }:
            selected = select_fact(filing_facts, metric, form_type=form_type, fiscal_period=fiscal_period, target_end=report_date)
            if selected is not None:
                selected_facts[metric] = selected
            metric_values[metric] = selected.value if selected else None
        if metric_values.get("gross_profit") is None and metric_values.get("revenue") is not None and metric_values.get("cost_of_sales") is not None:
            metric_values["gross_profit"] = float(metric_values["revenue"]) - float(metric_values["cost_of_sales"])
        total_debt = metric_values.get("debt_total")
        if total_debt is None and (metric_values.get("debt_current") is not None or metric_values.get("debt_noncurrent") is not None):
            total_debt = float(metric_values.get("debt_current") or 0.0) + float(metric_values.get("debt_noncurrent") or 0.0)
        capex = abs(metric_values["capex"]) if metric_values.get("capex") is not None else None
        free_cash_flow = None
        if metric_values.get("operating_cash_flow") is not None and capex is not None:
            free_cash_flow = float(metric_values["operating_cash_flow"]) - capex
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
            "fiscal_period": fiscal_period,
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
            "operating_cash_flow": metric_values.get("operating_cash_flow"),
            "capex": capex,
            "total_debt": total_debt,
            "free_cash_flow": free_cash_flow,
            "research_and_development": metric_values.get("research_and_development"),
            "stock_based_compensation": metric_values.get("stock_based_compensation"),
            "diluted_shares": metric_values.get("diluted_shares"),
            "canonical_quality": canonical_quality,
            "data_quality_status": "review",
            "_reported_currency": statement_currency,
        }
        apply_fx_conversion(conn, feature, selected_facts, fx_source)
        reasons = [f"missing_{metric}" for metric in sorted(CRITICAL_METRICS) if metric_values.get(metric) is None]
        if statement_currency and statement_currency != "USD" and feature.get("fx_conversion_status") != "converted":
            reasons.append(f"valuation_fx_unconverted_{statement_currency}")
        if not statement_currency:
            reasons.append("missing_reported_currency")
        quality = "complete" if not reasons else "review"
        feature["data_quality_status"] = quality
        feature["_review_reason"] = ";".join(reasons)
        features.append(feature)
        report_rows.append(
            {
                "ticker": ticker,
                "asof_date": filing_date.isoformat(),
                "form_type": form_type,
                "fiscal_period_end": report_date.isoformat(),
                "fiscal_year": filing.get("fiscal_year"),
                "fiscal_period": fiscal_period,
                "reporting_standard": reporting_standard,
                "financial_frequency": financial_frequency,
                "reported_currency": statement_currency,
                "fx_conversion_status": feature.get("fx_conversion_status"),
                "fx_rate_income_statement": feature.get("fx_rate_income_statement"),
                "fx_rate_balance_sheet": feature.get("fx_rate_balance_sheet"),
                "revenue": metric_values.get("revenue"),
                "revenue_usd": feature.get("revenue_usd"),
                "free_cash_flow": free_cash_flow,
                "data_quality_status": quality,
                "review_reason": ";".join(reasons),
            }
        )
    features.sort(key=lambda row: (str(row["fiscal_period_end"]), str(row["asof_date"]), str(row["accession_number"])))
    by_period_key: dict[tuple[str, int | None], dict[str, Any]] = {}
    prev_yoy_growth: dict[str, float | None] = {}
    for idx, feature in enumerate(features):
        fiscal_year = int(feature["fiscal_year"]) if feature.get("fiscal_year") is not None else None
        fiscal_period = str(feature.get("fiscal_period") or "")
        prior = by_period_key.get((fiscal_period, fiscal_year - 1 if fiscal_year is not None else None))
        for metric, out_name in (
            ("revenue", "revenue_yoy_growth"),
            ("gross_profit", "gross_profit_yoy_growth"),
            ("operating_income", "operating_income_yoy_growth"),
            ("free_cash_flow", "free_cash_flow_yoy_growth"),
        ):
            feature[out_name] = pct_change(safe_float(feature.get(metric)), safe_float(prior.get(metric)) if prior else None)
        feature["revenue_acceleration"] = None
        if feature.get("revenue_yoy_growth") is not None and prev_yoy_growth.get(fiscal_period) is not None:
            feature["revenue_acceleration"] = float(feature["revenue_yoy_growth"]) - float(prev_yoy_growth[fiscal_period])
        if feature.get("revenue_yoy_growth") is not None:
            prev_yoy_growth[fiscal_period] = feature["revenue_yoy_growth"]
        for metric in ("revenue", "gross_profit", "operating_income", "net_income", "free_cash_flow"):
            feature[f"{metric}_ttm"] = flow_ttm(features, idx, metric)
        revenue = safe_float(feature.get("revenue"))
        feature["gross_margin"] = safe_div(safe_float(feature.get("gross_profit")), revenue)
        feature["operating_margin"] = safe_div(safe_float(feature.get("operating_income")), revenue)
        feature["fcf_margin"] = safe_div(safe_float(feature.get("free_cash_flow")), revenue)
        feature["r_and_d_pct_revenue"] = safe_div(safe_float(feature.get("research_and_development")), revenue)
        feature["sbc_pct_revenue"] = safe_div(safe_float(feature.get("stock_based_compensation")), revenue)
        feature["net_cash"] = None
        if feature.get("cash_and_equivalents") is not None or feature.get("total_debt") is not None:
            feature["net_cash"] = float(feature.get("cash_and_equivalents") or 0.0) - float(feature.get("total_debt") or 0.0)
        feature["net_cash_to_assets"] = safe_div(safe_float(feature.get("net_cash")), safe_float(feature.get("assets")))
        feature["inventory_days"] = None
        if feature.get("inventory") is not None and revenue:
            feature["inventory_days"] = float(feature["inventory"]) / revenue * 365.0
        market_cap = latest_market_cap(conn, ticker, str(feature["asof_date"]))
        feature["market_cap"] = market_cap
        enterprise_value = None
        valuation_currency_ready = feature.get("fx_conversion_status") in {"usd_native", "converted"}
        gross_profit_ttm_for_val = safe_float(feature.get("gross_profit_ttm"))
        operating_income_ttm_for_val = safe_float(feature.get("operating_income_ttm"))
        free_cash_flow_ttm_for_val = safe_float(feature.get("free_cash_flow_ttm"))
        if valuation_currency_ready:
            gross_profit_ttm_for_val = flow_ttm(features, idx, "gross_profit_usd")
            operating_income_ttm_for_val = flow_ttm(features, idx, "operating_income_usd")
            free_cash_flow_ttm_for_val = flow_ttm(features, idx, "free_cash_flow_usd")
        if market_cap is not None and valuation_currency_ready:
            enterprise_value = market_cap + float(feature.get("total_debt_usd") or 0.0) - float(feature.get("cash_and_equivalents_usd") or 0.0)
        feature["ev_gross_profit"] = safe_div(enterprise_value, gross_profit_ttm_for_val)
        feature["ev_operating_income"] = safe_div(enterprise_value, operating_income_ttm_for_val)
        feature["fcf_yield"] = safe_div(free_cash_flow_ttm_for_val, market_cap) if valuation_currency_ready else None
        feature.pop("_reported_currency", None)
        feature.pop("_review_reason", None)
        if fiscal_year is not None:
            by_period_key[(fiscal_period, fiscal_year)] = feature
    return features, report_rows


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
    model_family = str(cfg_get(config, "technology_universe.initial_subsector", "semiconductors") or "semiconductors")
    ticker_filter = {normalize_ticker(x) for x in args.tickers.split(",") if normalize_ticker(x)}
    report_rows: list[dict[str, Any]] = []
    with connect(db_path, timeout_sec=float(cfg_get(config, "runtime.sqlite_timeout_sec", 30.0))) as conn:
        init_db(conn)
        run_id = start_run(conn, run_type=RUN_TYPE, input_path=config_path)
        try:
            tickers = load_universe(conn, ticker_filter)
            with conn:
                conn.execute("DELETE FROM data_quality_issues WHERE stage = ?", (RUN_TYPE,))
                for ticker in tickers:
                    canonical_count = rebuild_canonical_for_ticker(conn, ticker, facts_source)
                    features, rows = build_ticker_features(
                        conn,
                        ticker,
                        facts_source=facts_source,
                        filings_source=filings_source,
                        fx_source=fx_source,
                        model_family=model_family,
                    )
                    if not features:
                        add_issue(conn, ticker, facts_source, "missing_financial_features", "No canonical SEC financial features could be built.")
                    for feature in features:
                        sanity_issues = financial_sanity_issues(feature)
                        if sanity_issues:
                            feature["data_quality_status"] = "review"
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
