#!/usr/bin/env python3
from __future__ import annotations

import argparse
import logging
import math
import sys
from contextlib import closing
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from industrials.core.config import cfg_get, load_yaml, resolve_path  # noqa: E402
from industrials.core.db import connect, finish_run, init_db, start_run, utc_now  # noqa: E402
from industrials.core.logging_utils import configure_utc_logging  # noqa: E402
from industrials.core.reports import write_csv_atomic  # noqa: E402
from industrials.core.text_norm import normalize_ticker  # noqa: E402


LOGGER = logging.getLogger("build_industrials_financial_features")
DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
RUN_TYPE = "build_industrials_financial_features"
REPORT_FIELDS = [
    "ticker",
    "asof_date",
    "source_id",
    "model_family",
    "status",
    "reporting_profile",
    "reporting_standard",
    "financial_confidence",
    "data_quality_status",
    "fx_conversion_status",
    "revenue_usd",
    "revenue_stub_annualized_usd",
    "revenue_stub_period_days",
    "revenue_stub_quality",
    "assets_usd",
    "gross_margin",
    "operating_margin",
    "fcf_margin",
    "fcf_yield",
    "ev_gross_profit",
    "ev_operating_income",
    "review_reason",
]

FEATURE_COLUMNS = [
    "ticker",
    "asof_date",
    "source_id",
    "model_family",
    "accession_number",
    "form_type",
    "fiscal_period_end",
    "fiscal_year",
    "fiscal_period",
    "reporting_standard",
    "reporting_profile",
    "financial_frequency",
    "reported_currency",
    "fx_conversion_status",
    "fx_rate_income_statement",
    "fx_rate_balance_sheet",
    "revenue",
    "cost_of_sales",
    "gross_profit",
    "operating_income",
    "net_income",
    "eps_diluted",
    "assets",
    "liabilities",
    "equity",
    "cash_and_equivalents",
    "total_debt",
    "inventory",
    "accounts_receivable",
    "accounts_payable",
    "operating_cash_flow",
    "capex",
    "depreciation_and_amortization",
    "interest_expense",
    "pretax_income",
    "income_tax_expense",
    "equity_issuance_proceeds",
    "debt_issuance_proceeds",
    "orders",
    "free_cash_flow",
    "research_and_development",
    "stock_based_compensation",
    "diluted_shares",
    "revenue_usd",
    "gross_profit_usd",
    "operating_income_usd",
    "net_income_usd",
    "operating_cash_flow_usd",
    "capex_usd",
    "depreciation_and_amortization_usd",
    "interest_expense_usd",
    "equity_issuance_proceeds_usd",
    "debt_issuance_proceeds_usd",
    "orders_usd",
    "free_cash_flow_usd",
    "assets_usd",
    "liabilities_usd",
    "equity_usd",
    "cash_and_equivalents_usd",
    "total_debt_usd",
    "inventory_usd",
    "accounts_receivable_usd",
    "accounts_payable_usd",
    "revenue_ttm",
    "revenue_ttm_usd",
    "revenue_stub_annualized",
    "revenue_stub_annualized_usd",
    "revenue_stub_period_days",
    "revenue_stub_quality",
    "gross_profit_ttm",
    "gross_profit_ttm_usd",
    "operating_income_ttm",
    "operating_income_ttm_usd",
    "net_income_ttm",
    "net_income_ttm_usd",
    "free_cash_flow_ttm",
    "free_cash_flow_ttm_usd",
    "depreciation_and_amortization_ttm",
    "depreciation_and_amortization_ttm_usd",
    "interest_expense_ttm",
    "interest_expense_ttm_usd",
    "equity_issuance_proceeds_ttm",
    "equity_issuance_proceeds_ttm_usd",
    "debt_issuance_proceeds_ttm",
    "debt_issuance_proceeds_ttm_usd",
    "orders_ttm",
    "orders_ttm_usd",
    "gross_margin",
    "operating_margin",
    "fcf_margin",
    "r_and_d_pct_revenue",
    "sbc_pct_revenue",
    "net_cash",
    "net_cash_usd",
    "net_cash_to_assets",
    "inventory_days",
    "days_sales_outstanding",
    "days_payables_outstanding",
    "cash_conversion_cycle",
    "revenue_yoy_growth",
    "gross_profit_yoy_growth",
    "operating_income_yoy_growth",
    "free_cash_flow_yoy_growth",
    "revenue_acceleration",
    "fcf_to_net_income",
    "fcf_yield",
    "ev_gross_profit",
    "ev_operating_income",
    "market_cap",
    "latest_price",
    "deferred_revenue",
    "contract_liabilities",
    "remaining_performance_obligation",
    "book_to_bill",
    "funded_backlog",
    "funded_backlog_usd",
    "orders_yoy_growth",
    "backlog_yoy_growth",
    "backlog_to_revenue",
    "invested_capital_usd",
    "roic",
    "asset_turnover",
    "incremental_operating_margin",
    "inventory_growth",
    "inventory_sales_growth_spread",
    "cash_conversion_cycle_change",
    "ebitda_ttm_usd",
    "net_debt_to_ebitda",
    "interest_coverage",
    "cash_burn_ttm_usd",
    "cash_runway_years",
    "gross_capital_raised_ttm_usd",
    "capital_raise_dependence",
    "diluted_shares_yoy_growth",
    "development_stage",
    "financial_confidence",
    "financial_fallback_status",
    "canonical_quality",
    "data_quality_status",
    "review_reason",
]

DURATION_METRICS = {
    "revenue",
    "cost_of_sales",
    "gross_profit",
    "operating_income",
    "net_income",
    "eps_diluted",
    "operating_cash_flow",
    "capex",
    "depreciation_and_amortization",
    "interest_expense",
    "pretax_income",
    "income_tax_expense",
    "equity_issuance_proceeds",
    "debt_issuance_proceeds",
    "orders",
    "research_and_development",
    "stock_based_compensation",
    "diluted_shares",
}

# Ordered anchor-preference / selection list (FN-17): the first metric with a
# selected fact provides the feature's fiscal metadata anchor, so this order is
# deliberate and must stay deterministic across runs.
METRIC_SELECTION_ORDER = (
    "revenue",
    "assets",
    "operating_income",
    "net_income",
    "operating_cash_flow",
    "gross_profit",
    "cost_of_sales",
    "eps_diluted",
    "liabilities",
    "equity",
    "cash_and_equivalents",
    "inventory",
    "accounts_receivable",
    "accounts_payable",
    "capex",
    "depreciation_and_amortization",
    "interest_expense",
    "pretax_income",
    "income_tax_expense",
    "equity_issuance_proceeds",
    "debt_issuance_proceeds",
    "orders",
    "research_and_development",
    "stock_based_compensation",
    "diluted_shares",
    "debt_current",
    "debt_noncurrent",
    "debt_total",
    "deferred_revenue_current",
    "deferred_revenue_noncurrent",
    "deferred_revenue_total",
    "remaining_performance_obligation",
    "funded_backlog",
)

# Maximum spread (in days) allowed between the durations of facts combined into
# a derived metric or ratio (FN-4). Matches the 20-day tolerance already used by
# periods_are_one_year_apart.
PERIOD_DURATION_TOLERANCE_DAYS = 20


@dataclass(frozen=True)
class TtmResult:
    value: float | None
    quality_flag: str
    window_start: date | None = None
    window_end: date | None = None


def ttm_windows_match(left: TtmResult, right: TtmResult) -> bool:
    if left.value is None or right.value is None or left.window_end is None or right.window_end is None:
        return False
    if abs((left.window_end - right.window_end).days) > PERIOD_DURATION_TOLERANCE_DAYS:
        return False
    if left.window_start is None or right.window_start is None:
        return True
    return abs((left.window_start - right.window_start).days) <= PERIOD_DURATION_TOLERANCE_DAYS

ACCEPTED_DATE_SQL = """
CASE
    WHEN COALESCE(accepted_at, '') GLOB '????-??-??*' THEN SUBSTR(accepted_at, 1, 10)
    WHEN COALESCE(accepted_at, '') GLOB '[0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]*'
        THEN SUBSTR(accepted_at, 1, 4) || '-' || SUBSTR(accepted_at, 5, 2) || '-' || SUBSTR(accepted_at, 7, 2)
    ELSE filing_date
END
"""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build PIT financial features for an industrials model family.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--model-family", default="", help="Industrials model family to build, e.g. defense.")
    parser.add_argument("--tickers", default="", help="Optional comma-separated ticker filter.")
    parser.add_argument("--asof", default="", help="Feature as-of date. Defaults to latest market feature as-of date, then latest filing date.")
    parser.add_argument("--include-historical", action="store_true", help="Also build features for non-current historical/delisted members.")
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


def iso(raw: object) -> str:
    parsed = parse_date(raw)
    return parsed.isoformat() if parsed is not None else ""


def parse_ticker_list(raw: object) -> list[str]:
    values = raw if isinstance(raw, list) else str(raw or "").split(",")
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        ticker = normalize_ticker(value)
        if ticker and ticker not in seen:
            out.append(ticker)
            seen.add(ticker)
    return out


def parse_source_list(raw: object) -> list[str]:
    values = raw if isinstance(raw, list) else str(raw or "").split(",")
    out: list[str] = []
    for value in values:
        source = str(value or "").strip()
        if source and source not in out:
            out.append(source)
    return out


def source_priority_list(primary_source: str, fallback_sources: list[str]) -> list[str]:
    out: list[str] = []
    for source in [primary_source, *fallback_sources]:
        text = str(source or "").strip()
        if text and text not in out:
            out.append(text)
    return out


def placeholders(values: list[str]) -> str:
    if not values:
        raise ValueError("values cannot be empty")
    return ",".join("?" for _ in values)


def safe_div(num: float | None, den: float | None) -> float | None:
    if num is None or den is None or den == 0:
        return None
    value = num / den
    return value if math.isfinite(value) else None


def growth(cur: float | None, prev: float | None) -> float | None:
    if cur is None or prev is None or prev == 0:
        return None
    return cur / prev - 1.0


def as_float(raw: object) -> float | None:
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    try:
        value = float(text)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(value):
        return None
    return value


def load_universe(
    conn: Any,
    *,
    model_family: str,
    ticker_filter: list[str],
    include_historical: bool,
    asof: date | None,
) -> list[dict[str, Any]]:
    filter_sql = ""
    params: list[Any] = [model_family]
    if ticker_filter:
        filter_sql = f"AND c.ticker IN ({placeholders(ticker_filter)})"
    if include_historical or asof is not None:
        asof_sql = ""
        if asof is not None:
            asof_sql = "AND m.start_date <= ? AND COALESCE(m.end_date, '9999-12-31') >= ?"
            params.extend([asof.isoformat(), asof.isoformat()])
        if ticker_filter:
            params.extend(ticker_filter)
        rows = conn.execute(
            f"""
            SELECT c.company_id, c.ticker, c.cik, c.company_name, c.country, c.currency,
                   t.development_stage, t.calibration_cohort_id, t.calibration_cohort,
                   MIN(m.start_date) AS membership_start_date,
                   MAX(COALESCE(m.end_date, '')) AS membership_end_date
            FROM dim_company c
            JOIN dim_universe_membership m
              ON m.company_id = c.company_id
             AND m.model_family = ?
            JOIN dim_industrials_taxonomy t
              ON t.company_id = c.company_id
             AND t.model_family = m.model_family
            WHERE 1 = 1
              {asof_sql}
              {filter_sql}
            GROUP BY c.company_id, c.ticker
            ORDER BY c.is_active DESC, c.ticker
            """,
            tuple(params),
        ).fetchall()
        return [dict(row) for row in rows]
    rows = conn.execute(
        f"""
        SELECT c.company_id, c.ticker, c.cik, c.company_name, c.country, c.currency,
               t.development_stage, t.calibration_cohort_id, t.calibration_cohort
        FROM dim_company c
        JOIN dim_industrials_taxonomy t
          ON t.company_id = c.company_id
         AND t.model_family = ?
        WHERE c.is_active = 1
          {filter_sql}
        ORDER BY c.ticker
        """,
        tuple([*params, *ticker_filter] if ticker_filter else params),
    ).fetchall()
    return [dict(row) for row in rows]


def latest_panel_asof(conn: Any, *, model_family: str, market_source_ids: list[str], sec_source_id: str) -> date | None:
    ph_sources = placeholders(market_source_ids)
    row = conn.execute(
        f"""
        SELECT MAX(asof_date) AS asof_date
        FROM feature_market_technical
        WHERE model_family = ? AND source_id IN ({ph_sources})
        """,
        (model_family, *market_source_ids),
    ).fetchone()
    parsed = parse_date(row["asof_date"] if row is not None else "")
    if parsed is not None:
        return parsed
    row = conn.execute(
        f"""
        SELECT MAX({ACCEPTED_DATE_SQL}) AS asof_date
        FROM fact_sec_xbrl_fact
        WHERE source_id = ?
        """,
        (sec_source_id,),
    ).fetchone()
    return parse_date(row["asof_date"] if row is not None else "")


def reporting_standard_from_taxonomy(taxonomy: str) -> str:
    if taxonomy == "us-gaap":
        return "US_GAAP"
    if taxonomy == "ifrs-full":
        return "IFRS"
    return taxonomy


def refresh_canonical_facts(conn: Any, *, source_id: str, model_family: str, tickers: list[str], asof: date) -> int:
    if not tickers:
        return 0
    ph = placeholders(tickers)
    rows = conn.execute(
        f"""
        SELECT ticker, source_id, canonical_metric, period_end, period_start, filing_date,
               accepted_at, accession_number, form_type, fiscal_year, fiscal_period,
               taxonomy, concept_name, unit, value, source_priority
        FROM fact_sec_xbrl_fact
        WHERE source_id = ?
          AND ticker IN ({ph})
          AND period_end IS NOT NULL
          AND ({ACCEPTED_DATE_SQL}) <= ?
          AND period_end <= ?
        ORDER BY ticker, period_end, accession_number, canonical_metric,
                 source_priority DESC, concept_name DESC
        """,
        (source_id, *tickers, asof.isoformat(), asof.isoformat()),
    ).fetchall()
    now = utc_now()
    with conn:
        # SC-4: scope the DELETE with the same accepted-date predicate used for
        # the re-insert SELECT, so a historical-asof rebuild only replaces the
        # facts visible at that asof and never destroys later-accepted facts.
        # Rows accepted after asof are preserved; load_canonical_rows PIT-filters
        # them out at read time via the same predicate.
        conn.execute(
            f"""
            DELETE FROM fact_financial_statement_canonical
            WHERE source_id = ?
              AND model_family = ?
              AND ticker IN ({ph})
              AND period_end <= ?
              AND ({ACCEPTED_DATE_SQL}) <= ?
            """,
            (source_id, model_family, *tickers, asof.isoformat(), asof.isoformat()),
        )
        for row in rows:
            unit = str(row["unit"] or "")
            value = as_float(row["value"])
            value_usd = value if unit.upper() == "USD" else None
            conn.execute(
                """
                INSERT INTO fact_financial_statement_canonical(
                    ticker, source_id, model_family, canonical_metric, period_end,
                    period_start, filing_date, accepted_at, accession_number, form_type,
                    fiscal_year, fiscal_period, reporting_standard, taxonomy, concept_name,
                    unit, value, value_usd, source_priority, canonical_quality, created_at,
                    updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'mapped_xbrl', ?, ?)
                ON CONFLICT(ticker, source_id, model_family, canonical_metric, period_end, accession_number, unit)
                DO UPDATE SET
                    period_start = excluded.period_start,
                    filing_date = excluded.filing_date,
                    accepted_at = excluded.accepted_at,
                    form_type = excluded.form_type,
                    fiscal_year = excluded.fiscal_year,
                    fiscal_period = excluded.fiscal_period,
                    reporting_standard = excluded.reporting_standard,
                    taxonomy = excluded.taxonomy,
                    concept_name = excluded.concept_name,
                    value = excluded.value,
                    value_usd = excluded.value_usd,
                    source_priority = excluded.source_priority,
                    canonical_quality = excluded.canonical_quality,
                    updated_at = excluded.updated_at
                WHERE excluded.source_priority < fact_financial_statement_canonical.source_priority
                   OR (
                        excluded.source_priority = fact_financial_statement_canonical.source_priority
                    AND COALESCE(excluded.concept_name, '') < COALESCE(fact_financial_statement_canonical.concept_name, '')
                   )
                """,
                (
                    row["ticker"],
                    row["source_id"],
                    model_family,
                    row["canonical_metric"],
                    row["period_end"],
                    row["period_start"],
                    row["filing_date"],
                    row["accepted_at"],
                    row["accession_number"],
                    row["form_type"],
                    row["fiscal_year"],
                    row["fiscal_period"],
                    reporting_standard_from_taxonomy(str(row["taxonomy"] or "")),
                    row["taxonomy"],
                    row["concept_name"],
                    unit,
                    value,
                    value_usd,
                    int(row["source_priority"] or 100),
                    now,
                    now,
                ),
            )
    return len(rows)


def load_profile(conn: Any, *, ticker: str, model_family: str, company: dict[str, Any], source_id: str) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT *
        FROM dim_issuer_reporting_profile
        WHERE ticker = ? AND model_family = ?
        """,
        (ticker, model_family),
    ).fetchone()
    if row is not None:
        return dict(row)

    country = str(company.get("country") or "")
    if country and country.upper() not in {"UNITED STATES", "USA", "US"}:
        profile = "FOREIGN_NEUTRAL_LOW_CONFIDENCE"
        standard = "foreign_no_sec_xbrl"
        confidence = 0.25
        reason = "no_reporting_profile_loaded_foreign_issuer"
    else:
        profile = "NO_FINANCIALS_REVIEW"
        standard = "unavailable"
        confidence = 0.0
        reason = "no_reporting_profile_loaded"
    now = utc_now()
    # XC-1: execute on the caller's transaction. A nested `with conn:` here
    # committed the outer build transaction mid-loop, making the asof DELETEs
    # and any partial feature upserts permanent on a mid-build crash.
    conn.execute(
        """
        INSERT INTO dim_issuer_reporting_profile(
            ticker, model_family, cik, country, reporting_profile, reporting_standard,
            primary_taxonomy, fallback_status, financial_confidence, usable_xbrl_flag,
            source_id, review_reason, created_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, '', 'neutral_low_confidence', ?, 0, ?, ?, ?, ?)
        ON CONFLICT(ticker, model_family) DO NOTHING
        """,
        (
            ticker,
            model_family,
            company.get("cik"),
            country,
            profile,
            standard,
            confidence,
            source_id,
            reason,
            now,
            now,
        ),
    )
    row = conn.execute("SELECT * FROM dim_issuer_reporting_profile WHERE ticker = ? AND model_family = ?", (ticker, model_family)).fetchone()
    return dict(row) if row is not None else {}


def load_canonical_rows(conn: Any, *, ticker: str, source_id: str, model_family: str, asof: date) -> list[dict[str, Any]]:
    rows = conn.execute(
        f"""
        SELECT *
        FROM fact_financial_statement_canonical
        WHERE ticker = ?
          AND source_id = ?
          AND model_family = ?
          AND period_end <= ?
          AND ({ACCEPTED_DATE_SQL}) <= ?
        ORDER BY period_end DESC, filing_date DESC, source_priority ASC, concept_name ASC
        """,
        (ticker, source_id, model_family, asof.isoformat(), asof.isoformat()),
    ).fetchall()
    return [dict(row) for row in rows]


def duration_days(row: dict[str, Any]) -> int | None:
    start = parse_date(row.get("period_start"))
    end = parse_date(row.get("period_end"))
    if start is None or end is None:
        return None
    return (end - start).days


def is_annual_fact(row: dict[str, Any]) -> bool:
    fiscal_period = str(row.get("fiscal_period") or "").upper()
    form_type = str(row.get("form_type") or "").upper()
    days = duration_days(row)
    return fiscal_period == "FY" or form_type in {"10-K", "20-F", "40-F"} or (days is not None and days >= 300)


def row_sort_key(row: dict[str, Any]) -> tuple[str, str, int]:
    return (
        str(row.get("period_end") or ""),
        str(row.get("filing_date") or ""),
        -int(row.get("source_priority") or 100),
    )


def select_fact(rows: list[dict[str, Any]], metric: str, *, prefer_annual: bool) -> dict[str, Any] | None:
    candidates = [row for row in rows if str(row.get("canonical_metric") or "") == metric and as_float(row.get("value")) is not None]
    if not candidates:
        return None
    if prefer_annual:
        annual = [row for row in candidates if is_annual_fact(row)]
        if annual:
            candidates = annual
    return max(candidates, key=row_sort_key)


def select_previous_annual(rows: list[dict[str, Any]], metric: str, current: dict[str, Any] | None, *, offset: int = 1) -> dict[str, Any] | None:
    if current is None:
        return None
    current_end = parse_date(current.get("period_end"))
    if current_end is None:
        return None
    candidates: list[dict[str, Any]] = []
    for row in rows:
        row_period_end = parse_date(row.get("period_end"))
        if (
            str(row.get("canonical_metric") or "") == metric
            and row is not current
            and as_float(row.get("value")) is not None
            and is_annual_fact(row)
            and row_period_end is not None
            and row_period_end < current_end
        ):
            candidates.append(row)
    candidates.sort(key=row_sort_key, reverse=True)
    return candidates[offset - 1] if len(candidates) >= offset else None


def select_previous_comparable(
    rows: list[dict[str, Any]],
    metric: str,
    current: dict[str, Any] | None,
    *,
    instant_metric: bool = False,
) -> dict[str, Any] | None:
    if current is None:
        return None
    candidates = [
        row
        for row in rows
        if str(row.get("canonical_metric") or "") == metric
        and row is not current
        and as_float(row.get("value")) is not None
        and (
            period_ends_are_one_year_apart(current, row)
            if instant_metric
            else periods_are_one_year_apart(current, row)
        )
    ]
    return max(candidates, key=row_sort_key) if candidates else None


def is_quarterly_or_interim_fact(row: dict[str, Any]) -> bool:
    fiscal_period = str(row.get("fiscal_period") or "").upper()
    days = duration_days(row)
    if fiscal_period in {"Q1", "Q2", "Q3", "Q4"}:
        return True
    return days is not None and 45 <= days <= 130


def fiscal_quarter(row: dict[str, Any]) -> int | None:
    fiscal_period = str(row.get("fiscal_period") or "").upper()
    if fiscal_period in {"Q1", "Q2", "Q3"}:
        return int(fiscal_period[1])
    days = duration_days(row)
    if days is None:
        return None
    if 45 <= days <= 130:
        return 1
    if 130 < days <= 225:
        return 2
    if 225 < days <= 315:
        return 3
    return None


def is_cumulative_interim_fact(row: dict[str, Any]) -> bool:
    if is_annual_fact(row):
        return False
    quarter = fiscal_quarter(row)
    days = duration_days(row)
    if quarter is None or days is None:
        return False
    if quarter == 1:
        return 45 <= days <= 130
    if quarter == 2:
        return 130 < days <= 225
    if quarter == 3:
        return 225 < days <= 315
    return False


def latest_quarterly_facts(rows: list[dict[str, Any]], metric: str, *, count: int = 4) -> list[dict[str, Any]]:
    best_by_period: dict[str, dict[str, Any]] = {}
    for row in rows:
        if str(row.get("canonical_metric") or "") != metric:
            continue
        if as_float(row.get("value")) is None or not is_quarterly_or_interim_fact(row):
            continue
        # FN-18: only discrete quarters qualify. Cumulative interims tagged
        # Q2/Q3 (six/nine-month durations) must not shadow discrete quarters,
        # which caused false-negative nonconsecutive_quarters TTM rejections.
        days = duration_days(row)
        if days is None or not 45 <= days <= 130:
            continue
        period_end = str(row.get("period_end") or "")
        if not period_end:
            continue
        current = best_by_period.get(period_end)
        if current is None or row_sort_key(row) > row_sort_key(current):
            best_by_period[period_end] = row
    selected = sorted(best_by_period.values(), key=row_sort_key, reverse=True)[:count]
    return selected if len(selected) == count else []


def rows_for_metric(rows: list[dict[str, Any]], metric: str) -> list[dict[str, Any]]:
    return [row for row in rows if str(row.get("canonical_metric") or "") == metric and as_float(row.get("value")) is not None]


def days_between(left: date, right: date) -> int:
    return abs((right - left).days)


def combined_periods_match(
    *rows: dict[str, Any] | None,
    duration_tolerance_days: int = PERIOD_DURATION_TOLERANCE_DAYS,
) -> bool:
    """FN-4: facts combined into a derived metric or ratio must share the same
    fiscal period. Requires identical period_end across all present operands and,
    where durations are known, a duration spread within tolerance. Instant
    (balance-sheet) facts have no period_start, so they are held only to the
    period_end equality requirement."""
    present = [row for row in rows if row is not None]
    if len(present) < 2:
        return True
    ends: list[date] = []
    for row in present:
        end = parse_date(row.get("period_end"))
        if end is None:
            return False
        ends.append(end)
    if any(end != ends[0] for end in ends[1:]):
        return False
    durations = [days for row in present if (days := duration_days(row)) is not None]
    if durations and max(durations) - min(durations) > duration_tolerance_days:
        return False
    return True


def periods_are_one_year_apart(current: dict[str, Any], prior: dict[str, Any], *, tolerance_days: int = 20) -> bool:
    current_start = parse_date(current.get("period_start"))
    current_end = parse_date(current.get("period_end"))
    prior_start = parse_date(prior.get("period_start"))
    prior_end = parse_date(prior.get("period_end"))
    if current_start is None or current_end is None or prior_start is None or prior_end is None:
        return False
    return (
        345 <= days_between(current_start, prior_start) <= 385 + tolerance_days
        and 345 <= days_between(current_end, prior_end) <= 385 + tolerance_days
    )


def period_ends_are_one_year_apart(
    current: dict[str, Any],
    prior: dict[str, Any],
    *,
    tolerance_days: int = 20,
) -> bool:
    current_end = parse_date(current.get("period_end"))
    prior_end = parse_date(prior.get("period_end"))
    if current_end is None or prior_end is None:
        return False
    return 345 <= days_between(current_end, prior_end) <= 385 + tolerance_days


def valid_consecutive_quarter_window(quarters: list[dict[str, Any]]) -> bool:
    if len(quarters) != 4:
        return False
    periods: list[tuple[date, date]] = []
    for row in quarters:
        start = parse_date(row.get("period_start"))
        end = parse_date(row.get("period_end"))
        days = duration_days(row)
        if start is None or end is None or days is None or not 45 <= days <= 130:
            return False
        periods.append((start, end))
    periods.sort(key=lambda item: item[1])
    if len({end for _, end in periods}) != 4:
        return False
    if not 340 <= (periods[-1][1] - periods[0][0]).days <= 380:
        return False
    for idx in range(1, len(periods)):
        prev_start, prev_end = periods[idx - 1]
        cur_start, cur_end = periods[idx]
        if cur_start <= prev_start or cur_end <= prev_end:
            return False
        gap_days = (cur_start - prev_end).days
        if gap_days < 0 or gap_days > 7:
            return False
    return True


def consecutive_quarter_ttm_result(rows: list[dict[str, Any]], metric: str) -> TtmResult:
    quarters = latest_quarterly_facts(rows, metric)
    if len(quarters) != 4:
        return TtmResult(None, f"ttm_{metric}_unavailable_no_four_quarter_window")
    if not valid_consecutive_quarter_window(quarters):
        return TtmResult(None, f"ttm_{metric}_unavailable_nonconsecutive_quarters")
    values = [as_float(row.get("value")) for row in quarters]
    if not all(value is not None for value in values):
        return TtmResult(None, f"ttm_{metric}_unavailable_missing_quarter_value")
    starts = [start for row in quarters if (start := parse_date(row.get("period_start"))) is not None]
    ends = [end for row in quarters if (end := parse_date(row.get("period_end"))) is not None]
    return TtmResult(
        sum(value for value in values if value is not None),
        "",
        window_start=min(starts) if starts else None,
        window_end=max(ends) if ends else None,
    )


def annual_plus_interim_ttm_result(rows: list[dict[str, Any]], metric: str) -> TtmResult:
    metric_rows = rows_for_metric(rows, metric)
    if not metric_rows:
        return TtmResult(None, f"ttm_{metric}_unavailable_no_metric_facts")

    annual_rows = [row for row in metric_rows if is_annual_fact(row) and parse_date(row.get("period_end")) is not None]
    annual_rows.sort(key=row_sort_key, reverse=True)
    interim_rows = [row for row in metric_rows if is_cumulative_interim_fact(row) and parse_date(row.get("period_end")) is not None]
    interim_rows.sort(key=row_sort_key, reverse=True)

    latest_annual = annual_rows[0] if annual_rows else None
    latest_annual_end = parse_date(latest_annual.get("period_end")) if latest_annual is not None else None
    latest_interim = interim_rows[0] if interim_rows else None
    latest_interim_end = parse_date(latest_interim.get("period_end")) if latest_interim is not None else None

    if latest_annual is not None and (latest_interim_end is None or (latest_annual_end is not None and latest_annual_end >= latest_interim_end)):
        annual_value = as_float(latest_annual.get("value"))
        if annual_value is None:
            return TtmResult(None, f"ttm_{metric}_unavailable_missing_annual_value")
        return TtmResult(
            annual_value,
            "",
            window_start=parse_date(latest_annual.get("period_start")),
            window_end=latest_annual_end,
        )
    if latest_interim is None or latest_interim_end is None:
        return consecutive_quarter_ttm_result(rows, metric)

    quarter = fiscal_quarter(latest_interim)
    if quarter is None:
        return TtmResult(None, f"ttm_{metric}_unavailable_latest_interim_period_unknown")

    annual_candidates = [
        row
        for row in annual_rows
        if (period_end := parse_date(row.get("period_end"))) is not None and period_end < latest_interim_end
    ]
    if not annual_candidates:
        return consecutive_quarter_ttm_result(rows, metric)
    annual = annual_candidates[0]
    annual_start = parse_date(annual.get("period_start"))
    annual_end = parse_date(annual.get("period_end"))
    if annual_start is None or annual_end is None:
        return TtmResult(None, f"ttm_{metric}_unavailable_annual_period_missing")

    prior_same_interim_candidates = [
        row
        for row in interim_rows
        if row is not latest_interim
        and fiscal_quarter(row) == quarter
        and periods_are_one_year_apart(latest_interim, row)
        and (period_end := parse_date(row.get("period_end"))) is not None
        and period_end < annual_end
    ]
    if not prior_same_interim_candidates:
        return consecutive_quarter_ttm_result(rows, metric)
    prior_same_interim = prior_same_interim_candidates[0]
    prior_start = parse_date(prior_same_interim.get("period_start"))
    prior_end = parse_date(prior_same_interim.get("period_end"))
    latest_start = parse_date(latest_interim.get("period_start"))
    if prior_start is None or prior_end is None or latest_start is None:
        return TtmResult(None, f"ttm_{metric}_unavailable_interim_period_missing")
    if days_between(prior_start, annual_start) > 20 or not (prior_end < annual_end < latest_interim_end):
        return TtmResult(None, f"ttm_{metric}_unavailable_interim_annual_window_mismatch")

    annual_value = as_float(annual.get("value"))
    latest_interim_value = as_float(latest_interim.get("value"))
    prior_interim_value = as_float(prior_same_interim.get("value"))
    if annual_value is None or latest_interim_value is None or prior_interim_value is None:
        return TtmResult(None, f"ttm_{metric}_unavailable_missing_formula_value")
    return TtmResult(
        annual_value + latest_interim_value - prior_interim_value,
        "",
        window_start=prior_end,
        window_end=latest_interim_end,
    )


def ttm_metric_result(rows: list[dict[str, Any]], metric: str) -> TtmResult:
    result = annual_plus_interim_ttm_result(rows, metric)
    if result.value is not None or result.quality_flag:
        return result
    return consecutive_quarter_ttm_result(rows, metric)


def metric_value(selected: dict[str, dict[str, Any] | None], metric: str) -> float | None:
    row = selected.get(metric)
    return as_float(row.get("value")) if row is not None else None


def unit_currency(unit: object) -> str | None:
    """Return the ISO currency code carried by a fact unit, or None for
    non-monetary units (e.g. 'shares', 'pure'). Per-share units like
    'USD/shares' resolve to their currency component. Comparison is
    case-insensitive so legacy mixed-case rows (SC-2) still resolve."""
    text = str(unit or "").strip().upper()
    head = text.split("/", 1)[0].strip()
    if len(head) == 3 and head.isalpha():
        return head
    return None


def resolve_reporting_currency(rows: list[dict[str, Any]], fallback: str) -> str:
    """FN-3: resolve ONE reporting currency per ticker-period slice as the
    majority currency across all monetary facts; ties prefer the profile/company
    currency, then alphabetical order for determinism."""
    counts: dict[str, int] = {}
    for row in rows:
        currency = unit_currency(row.get("unit"))
        if currency is not None:
            counts[currency] = counts.get(currency, 0) + 1
    fallback_currency = str(fallback or "USD").strip().upper() or "USD"
    if not counts:
        return fallback_currency
    top = max(counts.values())
    leaders = sorted(currency for currency, count in counts.items() if count == top)
    if len(leaders) > 1 and fallback_currency in leaders:
        return fallback_currency
    return leaders[0]


def lookup_fx_rate(conn: Any, *, from_currency: str, to_currency: str, asof: date, max_staleness_days: int) -> float | None:
    """MK-3: reject FX rates older than max_staleness_days before asof so
    non-USD financials never convert at arbitrarily old rates."""
    if from_currency == to_currency:
        return 1.0
    min_rate_date = asof - timedelta(days=max_staleness_days)
    row = conn.execute(
        """
        SELECT fx_rate
        FROM fact_fx_rate
        WHERE from_currency = ?
          AND to_currency = ?
          AND rate_date <= ?
          AND rate_date >= ?
        ORDER BY rate_date DESC
        LIMIT 1
        """,
        (from_currency, to_currency, asof.isoformat(), min_rate_date.isoformat()),
    ).fetchone()
    return as_float(row["fx_rate"]) if row is not None else None


def latest_fx_rate_date(conn: Any, *, from_currency: str, to_currency: str, asof: date) -> date | None:
    row = conn.execute(
        """
        SELECT MAX(rate_date) AS rate_date
        FROM fact_fx_rate
        WHERE from_currency = ?
          AND to_currency = ?
          AND rate_date <= ?
        """,
        (from_currency, to_currency, asof.isoformat()),
    ).fetchone()
    return parse_date(row["rate_date"]) if row is not None else None


def lookup_average_fx_rate(
    conn: Any,
    *,
    from_currency: str,
    to_currency: str,
    start: date | None,
    end: date,
    max_staleness_days: int,
) -> float | None:
    if from_currency == to_currency:
        return 1.0
    if start is None or start > end:
        return lookup_fx_rate(conn, from_currency=from_currency, to_currency=to_currency, asof=end, max_staleness_days=max_staleness_days)
    row = conn.execute(
        """
        SELECT AVG(fx_rate) AS avg_fx_rate, MAX(rate_date) AS max_rate_date
        FROM fact_fx_rate
        WHERE from_currency = ?
          AND to_currency = ?
          AND rate_date >= ?
          AND rate_date <= ?
        """,
        (from_currency, to_currency, start.isoformat(), end.isoformat()),
    ).fetchone()
    average = as_float(row["avg_fx_rate"]) if row is not None else None
    max_rate_date = parse_date(row["max_rate_date"]) if row is not None else None
    if (
        average is not None
        and max_rate_date is not None
        and (end - max_rate_date).days <= max_staleness_days
    ):
        return average
    return lookup_fx_rate(conn, from_currency=from_currency, to_currency=to_currency, asof=end, max_staleness_days=max_staleness_days)


def latest_market_values(conn: Any, *, ticker: str, market_source_ids: list[str], model_family: str, asof: date) -> tuple[float | None, float | None]:
    for market_source_id in market_source_ids:
        row = conn.execute(
            """
            SELECT market_cap, regular_market_price
            FROM fact_market_snapshot
            WHERE ticker = ?
              AND source_id = ?
              AND asof_date <= ?
            ORDER BY asof_date DESC
            LIMIT 1
            """,
            (ticker, market_source_id, asof.isoformat()),
        ).fetchone()
        market_cap = as_float(row["market_cap"]) if row is not None else None
        latest_price = as_float(row["regular_market_price"]) if row is not None else None
        if latest_price is None:
            row = conn.execute(
                """
                SELECT latest_adj_close
                FROM feature_market_technical
                WHERE ticker = ?
                  AND source_id = ?
                  AND model_family = ?
                  AND asof_date <= ?
                ORDER BY asof_date DESC
                LIMIT 1
                """,
                (ticker, market_source_id, model_family, asof.isoformat()),
            ).fetchone()
            latest_price = as_float(row["latest_adj_close"]) if row is not None else None
        if market_cap is not None or latest_price is not None:
            return market_cap, latest_price
    return None, None


def add_issue(conn: Any, *, ticker: str, source_id: str, model_family: str, severity: str, issue_type: str, detail: str) -> None:
    now = utc_now()
    row = conn.execute("SELECT company_id FROM dim_company WHERE ticker = ?", (ticker,)).fetchone()
    company_id = int(row["company_id"]) if row is not None else None
    # SC-12: stamp the writing model family so one family's build never wipes
    # or shadows another family's open issues.
    conn.execute(
        """
        INSERT INTO data_quality_issues(
            detected_at, severity, stage, model_family, ticker, company_id, source_id,
            issue_type, issue_detail, resolution_status, created_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, ?)
        """,
        (now, severity, RUN_TYPE, model_family, ticker, company_id, source_id, issue_type, detail, now, now),
    )


def base_feature(*, ticker: str, asof: date, source_id: str, model_family: str, company: dict[str, Any], profile: dict[str, Any]) -> dict[str, Any]:
    # XC-7: explicit is-None coalescing so a deliberate 0.0 confidence survives.
    profile_confidence = as_float(profile.get("financial_confidence"))
    return {
        column: None
        for column in FEATURE_COLUMNS
    } | {
        "ticker": ticker,
        "asof_date": asof.isoformat(),
        "source_id": source_id,
        "model_family": model_family,
        "reporting_standard": profile.get("reporting_standard"),
        "reporting_profile": profile.get("reporting_profile"),
        "reported_currency": str(company.get("currency") or "USD").upper(),
        "financial_frequency": "annual_preferred",
        "development_stage": company.get("development_stage"),
        "financial_confidence": profile_confidence if profile_confidence is not None else 0.0,
        "financial_fallback_status": profile.get("fallback_status"),
    }


def neutral_feature(
    *,
    ticker: str,
    asof: date,
    source_id: str,
    model_family: str,
    company: dict[str, Any],
    profile: dict[str, Any],
    reason: str,
) -> dict[str, Any]:
    feature = base_feature(ticker=ticker, asof=asof, source_id=source_id, model_family=model_family, company=company, profile=profile)
    # XC-7: explicit is-None coalescing — NO_FINANCIALS_REVIEW deliberately
    # assigns 0.0 confidence, which `or 0.25` used to rewrite to 0.25.
    profile_confidence = as_float(profile.get("financial_confidence"))
    feature.update(
        {
            "fx_conversion_status": "not_applicable",
            "financial_confidence": profile_confidence if profile_confidence is not None else 0.25,
            "financial_fallback_status": str(profile.get("fallback_status") or "neutral_low_confidence"),
            "canonical_quality": "not_available",
            "data_quality_status": "neutral_low_confidence",
            "review_reason": reason,
        }
    )
    return feature


def should_default_missing_revenue_to_zero(*, company: dict[str, Any], profile: dict[str, Any]) -> bool:
    development_stage = str(company.get("development_stage") or "").strip().lower()
    reporting_profile = str(profile.get("reporting_profile") or "").strip().upper()
    fallback_status = str(profile.get("fallback_status") or "").strip().lower()
    return (
        development_stage in {"development_stage", "pre_revenue", "pre_commercial", "speculative", "recent_ipo"}
        or reporting_profile == "RECENT_IPO_DEVELOPMENT_STAGE"
        or (reporting_profile.endswith("_PARTIAL") and fallback_status == "component_limited" and development_stage != "operating")
    )


def build_feature_from_facts(
    conn: Any,
    *,
    ticker: str,
    asof: date,
    source_id: str,
    model_family: str,
    company: dict[str, Any],
    profile: dict[str, Any],
    rows: list[dict[str, Any]],
    market_source_ids: list[str],
    fx_max_staleness_days: int,
) -> dict[str, Any]:
    feature = base_feature(ticker=ticker, asof=asof, source_id=source_id, model_family=model_family, company=company, profile=profile)
    # FN-3: resolve one reporting currency for the ticker slice and restrict
    # candidate facts to it. Facts available only in another currency are
    # excluded from selection and flagged per metric.
    reported_currency = resolve_reporting_currency(rows, str(company.get("currency") or "USD"))
    currency_rows = [row for row in rows if unit_currency(row.get("unit")) in (None, reported_currency)]
    currency_flags: list[str] = []
    period_flags: list[str] = []
    selected: dict[str, dict[str, Any] | None] = {}
    for metric in METRIC_SELECTION_ORDER:
        selected[metric] = select_fact(currency_rows, metric, prefer_annual=metric in DURATION_METRICS)
        if selected[metric] is None and select_fact(rows, metric, prefer_annual=metric in DURATION_METRICS) is not None:
            currency_flags.append(f"currency_mismatch_{metric}")

    revenue = metric_value(selected, "revenue")
    cost_of_sales = metric_value(selected, "cost_of_sales")
    gross_profit = metric_value(selected, "gross_profit")
    if gross_profit is None and revenue is not None and cost_of_sales is not None:
        # FN-4: only derive across facts from the same fiscal period.
        if combined_periods_match(selected.get("revenue"), selected.get("cost_of_sales")):
            gross_profit = revenue - cost_of_sales
        else:
            period_flags.append("period_mismatch_gross_profit")
    operating_income = metric_value(selected, "operating_income")
    net_income = metric_value(selected, "net_income")
    eps_diluted = metric_value(selected, "eps_diluted")
    assets = metric_value(selected, "assets")
    liabilities = metric_value(selected, "liabilities")
    equity = metric_value(selected, "equity")
    cash = metric_value(selected, "cash_and_equivalents")
    debt_total = metric_value(selected, "debt_total")
    if debt_total is None and (selected.get("debt_current") is not None or selected.get("debt_noncurrent") is not None):
        if combined_periods_match(selected.get("debt_current"), selected.get("debt_noncurrent")):
            debt_total = (metric_value(selected, "debt_current") or 0.0) + (metric_value(selected, "debt_noncurrent") or 0.0)
        else:
            period_flags.append("period_mismatch_total_debt")
    inventory = metric_value(selected, "inventory")
    receivables = metric_value(selected, "accounts_receivable")
    payables = metric_value(selected, "accounts_payable")
    operating_cash_flow = metric_value(selected, "operating_cash_flow")
    capex = metric_value(selected, "capex")
    depreciation_and_amortization = metric_value(selected, "depreciation_and_amortization")
    interest_expense = metric_value(selected, "interest_expense")
    pretax_income = metric_value(selected, "pretax_income")
    income_tax_expense = metric_value(selected, "income_tax_expense")
    equity_issuance_proceeds = metric_value(selected, "equity_issuance_proceeds")
    debt_issuance_proceeds = metric_value(selected, "debt_issuance_proceeds")
    orders = metric_value(selected, "orders")
    free_cash_flow = None
    if operating_cash_flow is not None and capex is not None:
        if combined_periods_match(selected.get("operating_cash_flow"), selected.get("capex")):
            free_cash_flow = operating_cash_flow - capex
        else:
            period_flags.append("period_mismatch_free_cash_flow")
    r_and_d = metric_value(selected, "research_and_development")
    sbc = metric_value(selected, "stock_based_compensation")
    diluted_shares = metric_value(selected, "diluted_shares")
    deferred_revenue = metric_value(selected, "deferred_revenue_total")
    if deferred_revenue is None and (selected.get("deferred_revenue_current") is not None or selected.get("deferred_revenue_noncurrent") is not None):
        if combined_periods_match(selected.get("deferred_revenue_current"), selected.get("deferred_revenue_noncurrent")):
            deferred_revenue = (metric_value(selected, "deferred_revenue_current") or 0.0) + (metric_value(selected, "deferred_revenue_noncurrent") or 0.0)
        else:
            period_flags.append("period_mismatch_deferred_revenue")
    rpo = metric_value(selected, "remaining_performance_obligation")
    funded_backlog = metric_value(selected, "funded_backlog")
    zero_revenue_defaulted = False
    if revenue is None and should_default_missing_revenue_to_zero(company=company, profile=profile):
        revenue = 0.0
        zero_revenue_defaulted = True

    # FN-17: deterministic anchor preference (revenue, assets, then the fixed
    # METRIC_SELECTION_ORDER) instead of arbitrary set/dict iteration.
    anchor = next((selected[metric] for metric in METRIC_SELECTION_ORDER if selected.get(metric) is not None), None)
    if anchor is not None:
        feature.update(
            {
                "accession_number": anchor.get("accession_number"),
                "form_type": anchor.get("form_type"),
                "fiscal_period_end": anchor.get("period_end"),
                "fiscal_year": anchor.get("fiscal_year"),
                "fiscal_period": anchor.get("fiscal_period"),
                "reporting_standard": anchor.get("reporting_standard") or profile.get("reporting_standard"),
            }
        )
    feature["reported_currency"] = reported_currency

    currency = reported_currency
    income_anchor = selected.get("revenue") or selected.get("operating_income") or selected.get("net_income") or anchor
    income_period_end = parse_date(income_anchor.get("period_end")) if income_anchor is not None else None
    income_period_start = parse_date(income_anchor.get("period_start")) if income_anchor is not None else None
    balance_anchor = selected.get("assets") or selected.get("liabilities") or selected.get("cash_and_equivalents") or anchor
    balance_period_end = parse_date(balance_anchor.get("period_end")) if balance_anchor is not None else None
    fx_income_end = income_period_end or asof
    fx_balance_end = balance_period_end or fx_income_end
    fx_rate_income_statement = lookup_average_fx_rate(
        conn,
        from_currency=currency,
        to_currency="USD",
        start=income_period_start,
        end=fx_income_end,
        max_staleness_days=fx_max_staleness_days,
    )
    fx_rate_balance_sheet = lookup_fx_rate(
        conn,
        from_currency=currency,
        to_currency="USD",
        asof=fx_balance_end,
        max_staleness_days=fx_max_staleness_days,
    )
    if currency == "USD":
        fx_status = "usd_native"
    elif fx_rate_income_statement is not None and fx_rate_balance_sheet is not None:
        fx_status = "converted_to_usd"
    else:
        fx_status = "missing_fx_rate"
    if currency == "USD":
        fx_rate_income_statement = 1.0
        fx_rate_balance_sheet = 1.0

    def usd_income(value: float | None) -> float | None:
        return value * fx_rate_income_statement if value is not None and fx_rate_income_statement is not None else None

    def usd_balance(value: float | None) -> float | None:
        return value * fx_rate_balance_sheet if value is not None and fx_rate_balance_sheet is not None else None

    def usd_ttm(value: float | None, result: TtmResult | None) -> float | None:
        # FN-14 (FX half): convert TTM values at the average FX rate over the
        # TTM window itself, not the latest period's average.
        if value is None:
            return None
        if currency == "USD":
            return value
        if result is not None and result.window_end is not None:
            rate = lookup_average_fx_rate(
                conn,
                from_currency=currency,
                to_currency="USD",
                start=result.window_start,
                end=result.window_end,
                max_staleness_days=fx_max_staleness_days,
            )
        else:
            rate = fx_rate_income_statement
        return value * rate if rate is not None else None

    revenue_usd = usd_income(revenue)
    gross_profit_usd = usd_income(gross_profit)
    operating_income_usd = usd_income(operating_income)
    net_income_usd = usd_income(net_income)
    operating_cash_flow_usd = usd_income(operating_cash_flow)
    capex_usd = usd_income(capex)
    depreciation_and_amortization_usd = usd_income(depreciation_and_amortization)
    interest_expense_usd = usd_income(interest_expense)
    equity_issuance_proceeds_usd = usd_income(equity_issuance_proceeds)
    debt_issuance_proceeds_usd = usd_income(debt_issuance_proceeds)
    orders_usd = usd_income(orders)
    free_cash_flow_usd = usd_income(free_cash_flow)
    assets_usd = usd_balance(assets)
    liabilities_usd = usd_balance(liabilities)
    equity_usd = usd_balance(equity)
    cash_usd = usd_balance(cash)
    debt_usd = usd_balance(debt_total)
    inventory_usd = usd_balance(inventory)
    receivables_usd = usd_balance(receivables)
    payables_usd = usd_balance(payables)
    funded_backlog_usd = usd_balance(funded_backlog)
    revenue_row = selected.get("revenue")
    revenue_stub_annualized: float | None = None
    revenue_stub_period_days: float | None = None
    revenue_stub_quality: str | None = None
    reporting_profile = str(profile.get("reporting_profile") or "").strip().upper()
    revenue_row_days = duration_days(revenue_row) if revenue_row is not None else None
    if revenue is not None and revenue_row is not None and revenue_row_days is not None and 45 <= revenue_row_days < 300 and not is_annual_fact(revenue_row):
        revenue_stub_annualized = revenue * 365.0 / revenue_row_days
        revenue_stub_period_days = float(revenue_row_days)
        if reporting_profile in {"RECENT_PUBLIC_STUB", "RECENT_IPO_DEVELOPMENT_STAGE"}:
            revenue_stub_quality = "recent_public_stub_observation_only"
        elif reporting_profile in {"FPI_HYBRID_STUB_LOADED", "FPI_HYBRID_LOADED"}:
            revenue_stub_quality = "fpi_hybrid_interim_annualized_observation_only"
        else:
            revenue_stub_quality = "interim_revenue_annualized_observation_only"
    revenue_stub_annualized_usd = usd_income(revenue_stub_annualized)
    market_cap, latest_price = latest_market_values(conn, ticker=ticker, market_source_ids=market_source_ids, model_family=model_family, asof=asof)
    enterprise_value = market_cap + (debt_usd or 0.0) - (cash_usd or 0.0) if market_cap is not None else None

    prev_revenue = metric_value({"revenue": select_previous_annual(currency_rows, "revenue", selected.get("revenue"))}, "revenue")
    prev2_revenue_row = select_previous_annual(currency_rows, "revenue", select_previous_annual(currency_rows, "revenue", selected.get("revenue")), offset=1)
    prev2_revenue = as_float(prev2_revenue_row.get("value")) if prev2_revenue_row is not None else None
    cur_revenue_growth = growth(revenue, prev_revenue)
    prev_revenue_growth = growth(prev_revenue, prev2_revenue)

    prev_gross_profit = metric_value({"gross_profit": select_previous_annual(currency_rows, "gross_profit", selected.get("gross_profit"))}, "gross_profit")
    prev_operating_income = metric_value({"operating_income": select_previous_annual(currency_rows, "operating_income", selected.get("operating_income"))}, "operating_income")
    prev_fcf_row = select_previous_annual(currency_rows, "operating_cash_flow", selected.get("operating_cash_flow"))
    prev_capex_row = select_previous_annual(currency_rows, "capex", selected.get("capex"))
    prev_fcf = None
    if prev_fcf_row is not None and prev_capex_row is not None:
        if combined_periods_match(prev_fcf_row, prev_capex_row):
            prev_ocf = as_float(prev_fcf_row.get("value"))
            prev_capex = as_float(prev_capex_row.get("value"))
            prev_fcf = prev_ocf - prev_capex if prev_ocf is not None and prev_capex is not None else None
        else:
            period_flags.append("period_mismatch_free_cash_flow_yoy_growth")

    ttm_results = {
        metric: ttm_metric_result(currency_rows, metric)
        for metric in [
            "revenue",
            "gross_profit",
            "operating_income",
            "net_income",
            "operating_cash_flow",
            "capex",
            "depreciation_and_amortization",
            "interest_expense",
            "pretax_income",
            "income_tax_expense",
            "equity_issuance_proceeds",
            "debt_issuance_proceeds",
            "orders",
        ]
    }
    revenue_ttm_local = ttm_results["revenue"].value
    if revenue_ttm_local is None and zero_revenue_defaulted:
        revenue_ttm_local = 0.0
    gross_profit_ttm_local = ttm_results["gross_profit"].value
    operating_income_ttm_local = ttm_results["operating_income"].value
    net_income_ttm_local = ttm_results["net_income"].value
    operating_cash_flow_ttm_local = ttm_results["operating_cash_flow"].value
    capex_ttm_local = ttm_results["capex"].value
    depreciation_and_amortization_ttm_local = ttm_results["depreciation_and_amortization"].value
    interest_expense_ttm_local = ttm_results["interest_expense"].value
    equity_issuance_proceeds_ttm_local = ttm_results["equity_issuance_proceeds"].value
    debt_issuance_proceeds_ttm_local = ttm_results["debt_issuance_proceeds"].value
    orders_ttm_local = ttm_results["orders"].value
    free_cash_flow_ttm_local = (
        operating_cash_flow_ttm_local - capex_ttm_local
        if operating_cash_flow_ttm_local is not None and capex_ttm_local is not None
        else None
    )

    reasons: list[str] = []
    quality_flags: list[str] = []
    previous_assets_row = select_previous_comparable(
        currency_rows,
        "assets",
        selected.get("assets"),
        instant_metric=True,
    )
    previous_equity_row = select_previous_comparable(
        currency_rows,
        "equity",
        selected.get("equity"),
        instant_metric=True,
    )
    previous_cash_row = select_previous_comparable(
        currency_rows,
        "cash_and_equivalents",
        selected.get("cash_and_equivalents"),
        instant_metric=True,
    )
    previous_debt_row = select_previous_comparable(
        currency_rows,
        "debt_total",
        selected.get("debt_total"),
        instant_metric=True,
    )
    previous_inventory_row = select_previous_comparable(
        currency_rows,
        "inventory",
        selected.get("inventory"),
        instant_metric=True,
    )
    previous_receivables_row = select_previous_comparable(
        currency_rows,
        "accounts_receivable",
        selected.get("accounts_receivable"),
        instant_metric=True,
    )
    previous_payables_row = select_previous_comparable(
        currency_rows,
        "accounts_payable",
        selected.get("accounts_payable"),
        instant_metric=True,
    )
    previous_backlog_row = select_previous_comparable(
        currency_rows,
        "funded_backlog",
        selected.get("funded_backlog"),
        instant_metric=True,
    )
    previous_diluted_shares_row = select_previous_comparable(
        currency_rows,
        "diluted_shares",
        selected.get("diluted_shares"),
    )
    previous_orders_row = select_previous_comparable(currency_rows, "orders", selected.get("orders"))
    previous_assets = as_float(previous_assets_row.get("value")) if previous_assets_row is not None else None
    previous_equity = as_float(previous_equity_row.get("value")) if previous_equity_row is not None else None
    previous_cash = as_float(previous_cash_row.get("value")) if previous_cash_row is not None else None
    previous_debt = as_float(previous_debt_row.get("value")) if previous_debt_row is not None else None
    previous_inventory = as_float(previous_inventory_row.get("value")) if previous_inventory_row is not None else None
    previous_backlog = as_float(previous_backlog_row.get("value")) if previous_backlog_row is not None else None
    previous_diluted_shares = (
        as_float(previous_diluted_shares_row.get("value")) if previous_diluted_shares_row is not None else None
    )
    previous_orders = as_float(previous_orders_row.get("value")) if previous_orders_row is not None else None

    current_debt_rows = (
        [selected.get("debt_total")]
        if selected.get("debt_total") is not None
        else [selected.get("debt_current"), selected.get("debt_noncurrent")]
    )
    current_capital_periods_match = combined_periods_match(
        *current_debt_rows,
        selected.get("equity"),
        selected.get("cash_and_equivalents"),
    )
    invested_capital = (
        debt_total + equity - cash
        if debt_total is not None
        and equity is not None
        and cash is not None
        and current_capital_periods_match
        else None
    )
    previous_capital_periods_match = combined_periods_match(
        previous_debt_row,
        previous_equity_row,
        previous_cash_row,
    )
    previous_invested_capital = (
        previous_debt + previous_equity - previous_cash
        if previous_debt is not None
        and previous_equity is not None
        and previous_cash is not None
        and previous_capital_periods_match
        else None
    )
    average_invested_capital = (
        (invested_capital + previous_invested_capital) / 2.0
        if invested_capital is not None and previous_invested_capital is not None
        else None
    )
    average_assets = (assets + previous_assets) / 2.0 if assets is not None and previous_assets is not None else None
    pretax_ttm = ttm_results["pretax_income"].value
    tax_ttm = ttm_results["income_tax_expense"].value
    if pretax_ttm is not None and pretax_ttm > 0 and tax_ttm is not None:
        effective_tax_rate = min(0.35, max(0.0, tax_ttm / pretax_ttm))
    else:
        effective_tax_rate = 0.21
        quality_flags.append("roic_tax_rate_fallback_21pct")
    nopat = (
        operating_income_ttm_local * (1.0 - effective_tax_rate)
        if operating_income_ttm_local is not None
        else None
    )
    roic = (
        safe_div(nopat, average_invested_capital)
        if average_invested_capital is not None and average_invested_capital > 0
        else None
    )
    asset_turnover = safe_div(revenue_ttm_local, average_assets) if average_assets is not None and average_assets > 0 else None
    invested_capital_usd = usd_balance(invested_capital)

    previous_revenue_comparable_row = select_previous_comparable(
        currency_rows,
        "revenue",
        selected.get("revenue"),
    )
    previous_operating_income_comparable_row = select_previous_comparable(
        currency_rows,
        "operating_income",
        selected.get("operating_income"),
    )
    previous_revenue_comparable = (
        as_float(previous_revenue_comparable_row.get("value"))
        if previous_revenue_comparable_row is not None
        else None
    )
    previous_operating_income_comparable = (
        as_float(previous_operating_income_comparable_row.get("value"))
        if previous_operating_income_comparable_row is not None
        else None
    )
    revenue_delta = (
        revenue - previous_revenue_comparable
        if revenue is not None and previous_revenue_comparable is not None
        else None
    )
    operating_income_delta = (
        operating_income - previous_operating_income_comparable
        if operating_income is not None and previous_operating_income_comparable is not None
        else None
    )
    incremental_operating_margin = None
    if (
        revenue_delta is not None
        and operating_income_delta is not None
        and previous_revenue_comparable is not None
        and revenue_delta >= max(abs(previous_revenue_comparable) * 0.01, 1e-12)
    ):
        incremental_operating_margin = operating_income_delta / revenue_delta

    inventory_growth = growth(inventory, previous_inventory)
    inventory_sales_growth_spread = (
        inventory_growth - cur_revenue_growth
        if inventory_growth is not None and cur_revenue_growth is not None
        else None
    )
    previous_cost_row = select_previous_comparable(
        currency_rows,
        "cost_of_sales",
        selected.get("cost_of_sales"),
    )
    previous_revenue_row = previous_revenue_comparable_row
    previous_cost = as_float(previous_cost_row.get("value")) if previous_cost_row is not None else None
    previous_revenue_for_ccc = (
        as_float(previous_revenue_row.get("value")) if previous_revenue_row is not None else None
    )
    previous_receivables = (
        as_float(previous_receivables_row.get("value")) if previous_receivables_row is not None else None
    )
    previous_payables = as_float(previous_payables_row.get("value")) if previous_payables_row is not None else None
    previous_ccc_periods_match = combined_periods_match(
        previous_inventory_row,
        previous_receivables_row,
        previous_payables_row,
        previous_cost_row,
        previous_revenue_row,
    )
    previous_inventory_days = safe_div(previous_inventory, previous_cost) if previous_ccc_periods_match else None
    previous_dso = safe_div(previous_receivables, previous_revenue_for_ccc) if previous_ccc_periods_match else None
    previous_dpo = safe_div(previous_payables, previous_cost) if previous_ccc_periods_match else None
    previous_ccc = (
        (previous_inventory_days + previous_dso - previous_dpo) * 365.0
        if previous_inventory_days is not None and previous_dso is not None and previous_dpo is not None
        else None
    )

    depreciation_and_amortization_ttm_usd = usd_ttm(
        depreciation_and_amortization_ttm_local,
        ttm_results["depreciation_and_amortization"],
    )
    interest_expense_ttm_usd = usd_ttm(interest_expense_ttm_local, ttm_results["interest_expense"])
    equity_issuance_proceeds_ttm_usd = usd_ttm(
        equity_issuance_proceeds_ttm_local,
        ttm_results["equity_issuance_proceeds"],
    )
    debt_issuance_proceeds_ttm_usd = usd_ttm(
        debt_issuance_proceeds_ttm_local,
        ttm_results["debt_issuance_proceeds"],
    )
    orders_ttm_usd = usd_ttm(orders_ttm_local, ttm_results["orders"])
    operating_income_ttm_usd = usd_ttm(operating_income_ttm_local, ttm_results["operating_income"])
    revenue_ttm_usd = usd_ttm(revenue_ttm_local, ttm_results["revenue"])
    ebitda_ttm_usd = (
        operating_income_ttm_usd + depreciation_and_amortization_ttm_usd
        if operating_income_ttm_usd is not None and depreciation_and_amortization_ttm_usd is not None
        else None
    )
    net_debt_usd = debt_usd - cash_usd if debt_usd is not None and cash_usd is not None else None
    net_debt_to_ebitda = (
        net_debt_usd / ebitda_ttm_usd
        if net_debt_usd is not None and ebitda_ttm_usd is not None and ebitda_ttm_usd > 0
        else None
    )
    interest_coverage = (
        operating_income_ttm_usd / interest_expense_ttm_usd
        if operating_income_ttm_usd is not None
        and interest_expense_ttm_usd is not None
        and interest_expense_ttm_usd > 0
        else None
    )
    free_cash_flow_ttm_usd = usd_ttm(free_cash_flow_ttm_local, ttm_results["operating_cash_flow"])
    cash_burn_ttm_usd = max(-free_cash_flow_ttm_usd, 0.0) if free_cash_flow_ttm_usd is not None else None
    cash_runway_years = (
        cash_usd / cash_burn_ttm_usd
        if cash_usd is not None and cash_burn_ttm_usd is not None and cash_burn_ttm_usd > 0
        else None
    )
    capital_raise_components = [
        value
        for value in (equity_issuance_proceeds_ttm_usd, debt_issuance_proceeds_ttm_usd)
        if value is not None
    ]
    gross_capital_raised_ttm_usd = sum(capital_raise_components) if capital_raise_components else None
    if len(capital_raise_components) == 1:
        quality_flags.append("capital_raise_proceeds_partial_component_coverage")
    capital_raise_dependence = (
        gross_capital_raised_ttm_usd / cash_burn_ttm_usd
        if len(capital_raise_components) == 2
        and gross_capital_raised_ttm_usd is not None
        and cash_burn_ttm_usd is not None
        and cash_burn_ttm_usd > 0
        else None
    )
    diluted_shares_yoy_growth = growth(diluted_shares, previous_diluted_shares)
    orders_yoy_growth = growth(orders, previous_orders)
    backlog_yoy_growth = growth(funded_backlog, previous_backlog)
    book_to_bill = None
    if orders_ttm_local is not None and revenue_ttm_local is not None and revenue_ttm_local > 0:
        if ttm_windows_match(ttm_results["orders"], ttm_results["revenue"]):
            book_to_bill = orders_ttm_local / revenue_ttm_local
        else:
            quality_flags.append("period_mismatch_book_to_bill")
    backlog_row = selected.get("funded_backlog")
    backlog_period_end = parse_date(backlog_row.get("period_end")) if backlog_row is not None else None
    revenue_window_end = ttm_results["revenue"].window_end
    backlog_to_revenue = (
        funded_backlog / revenue_ttm_local
        if funded_backlog is not None
        and revenue_ttm_local is not None
        and revenue_ttm_local > 0
        and backlog_period_end is not None
        and revenue_window_end is not None
        and abs((backlog_period_end - revenue_window_end).days) <= PERIOD_DURATION_TOLERANCE_DAYS
        else None
    )
    if funded_backlog is not None and backlog_to_revenue is None:
        quality_flags.append("period_mismatch_backlog_to_revenue")

    if revenue is None:
        reasons.append("missing_revenue")
    elif zero_revenue_defaulted:
        quality_flags.append("development_stage_missing_revenue_defaulted_to_zero")
    if assets is None:
        reasons.append("missing_assets")
    if operating_income is None and net_income is None:
        reasons.append("missing_income_metrics")
    if fx_status == "missing_fx_rate":
        # MK-3: distinguish a genuinely absent pair from one whose latest rate
        # exceeds the configured staleness bound; both fail conversion.
        stale_rate_date = latest_fx_rate_date(conn, from_currency=currency, to_currency="USD", asof=asof)
        if stale_rate_date is not None:
            reasons.append(f"stale_fx_rate_{currency}_USD_older_than_{fx_max_staleness_days}d")
        else:
            reasons.append(f"missing_fx_rate_{currency}_USD")
    if revenue_row is not None and not is_annual_fact(revenue_row):
        reasons.append("revenue_not_annual")
        if revenue_stub_annualized is not None:
            quality_flags.append("revenue_stub_annualized_observation_only")
    for metric, result in ttm_results.items():
        if result.value is None and result.quality_flag:
            quality_flags.append(result.quality_flag)
    if revenue is not None and revenue > 0 and revenue_ttm_local is None and str(company.get("development_stage") or "").lower() != "operating":
        quality_flags.append("revenue_transition_ttm_incomplete")
    if free_cash_flow_ttm_local is None and (
        operating_cash_flow_ttm_local is None or capex_ttm_local is None
    ):
        quality_flags.append("ttm_free_cash_flow_unavailable_missing_operating_cash_flow_or_capex")
    if rpo is not None:
        quality_flags.append("rpo_total_not_funded_backlog_or_bookings")

    def guarded_ratio(flag_metric: str, value: float | None, *operand_rows: dict[str, Any] | None) -> float | None:
        # FN-4: null out ratios whose operands come from different fiscal
        # periods and record a period_mismatch_<metric> flag.
        if value is None:
            return None
        if combined_periods_match(*operand_rows):
            return value
        period_flags.append(f"period_mismatch_{flag_metric}")
        return None

    inventory_to_cogs = guarded_ratio("inventory_days", safe_div(inventory, cost_of_sales), selected.get("inventory"), selected.get("cost_of_sales"))
    receivables_to_revenue = guarded_ratio("days_sales_outstanding", safe_div(receivables, revenue), selected.get("accounts_receivable"), selected.get("revenue"))
    payables_to_cogs = guarded_ratio("days_payables_outstanding", safe_div(payables, cost_of_sales), selected.get("accounts_payable"), selected.get("cost_of_sales"))
    gross_margin = guarded_ratio("gross_margin", safe_div(gross_profit, revenue), selected.get("gross_profit"), selected.get("revenue"))
    operating_margin = guarded_ratio("operating_margin", safe_div(operating_income, revenue), selected.get("operating_income"), selected.get("revenue"))
    fcf_margin = guarded_ratio(
        "fcf_margin",
        safe_div(free_cash_flow, revenue),
        selected.get("operating_cash_flow"),
        selected.get("capex"),
        selected.get("revenue"),
    )
    r_and_d_pct_revenue = guarded_ratio("r_and_d_pct_revenue", safe_div(r_and_d, revenue), selected.get("research_and_development"), selected.get("revenue"))
    sbc_pct_revenue = guarded_ratio("sbc_pct_revenue", safe_div(sbc, revenue), selected.get("stock_based_compensation"), selected.get("revenue"))
    fcf_to_net_income = guarded_ratio(
        "fcf_to_net_income",
        safe_div(free_cash_flow, net_income),
        selected.get("operating_cash_flow"),
        selected.get("capex"),
        selected.get("net_income"),
    )
    if selected.get("debt_total") is not None:
        debt_operand_rows: list[dict[str, Any] | None] = [selected.get("debt_total")]
    else:
        debt_operand_rows = [selected.get("debt_current"), selected.get("debt_noncurrent")]
    # FN-14: the unsuffixed net_cash column carries the LOCAL reported-currency
    # value; the USD conversion lives only in net_cash_usd. Both balance-sheet
    # operands share fx_rate_balance_sheet, so usd_balance(net_cash) equals
    # cash_usd - debt_usd whenever the rate is available.
    net_cash = (cash or 0.0) - (debt_total or 0.0) if cash is not None or debt_total is not None else None
    net_cash = guarded_ratio("net_cash", net_cash, selected.get("cash_and_equivalents"), *debt_operand_rows)
    net_cash_usd = usd_balance(net_cash)
    # net_cash_to_assets is currency-invariant (same balance-sheet FX rate on
    # both sides), so compute it local/local and keep it available when the FX
    # rate is missing.
    net_cash_to_assets = guarded_ratio(
        "net_cash_to_assets",
        safe_div(net_cash, assets),
        selected.get("cash_and_equivalents"),
        *debt_operand_rows,
        selected.get("assets"),
    )
    inventory_days = inventory_to_cogs * 365.0 if inventory_to_cogs is not None else None
    days_sales_outstanding = receivables_to_revenue * 365.0 if receivables_to_revenue is not None else None
    days_payables_outstanding = payables_to_cogs * 365.0 if payables_to_cogs is not None else None
    cash_conversion_cycle = (
        inventory_days + days_sales_outstanding - days_payables_outstanding
        if inventory_days is not None
        and days_sales_outstanding is not None
        and days_payables_outstanding is not None
        else None
    )
    cash_conversion_cycle_change = (
        cash_conversion_cycle - previous_ccc
        if cash_conversion_cycle is not None and previous_ccc is not None
        else None
    )
    quality_flags.extend(currency_flags)
    quality_flags.extend(period_flags)

    feature.update(
        {
            "fx_conversion_status": fx_status,
            "fx_rate_income_statement": fx_rate_income_statement,
            "fx_rate_balance_sheet": fx_rate_balance_sheet,
            "revenue": revenue,
            "cost_of_sales": cost_of_sales,
            "gross_profit": gross_profit,
            "operating_income": operating_income,
            "net_income": net_income,
            "eps_diluted": eps_diluted,
            "assets": assets,
            "liabilities": liabilities,
            "equity": equity,
            "cash_and_equivalents": cash,
            "total_debt": debt_total,
            "inventory": inventory,
            "accounts_receivable": receivables,
            "accounts_payable": payables,
            "operating_cash_flow": operating_cash_flow,
            "capex": capex,
            "depreciation_and_amortization": depreciation_and_amortization,
            "interest_expense": interest_expense,
            "pretax_income": pretax_income,
            "income_tax_expense": income_tax_expense,
            "equity_issuance_proceeds": equity_issuance_proceeds,
            "debt_issuance_proceeds": debt_issuance_proceeds,
            "orders": orders,
            "free_cash_flow": free_cash_flow,
            "research_and_development": r_and_d,
            "stock_based_compensation": sbc,
            "diluted_shares": diluted_shares,
            "revenue_usd": revenue_usd,
            "gross_profit_usd": gross_profit_usd,
            "operating_income_usd": operating_income_usd,
            "net_income_usd": net_income_usd,
            "operating_cash_flow_usd": operating_cash_flow_usd,
            "capex_usd": capex_usd,
            "depreciation_and_amortization_usd": depreciation_and_amortization_usd,
            "interest_expense_usd": interest_expense_usd,
            "equity_issuance_proceeds_usd": equity_issuance_proceeds_usd,
            "debt_issuance_proceeds_usd": debt_issuance_proceeds_usd,
            "orders_usd": orders_usd,
            "free_cash_flow_usd": free_cash_flow_usd,
            "assets_usd": assets_usd,
            "liabilities_usd": liabilities_usd,
            "equity_usd": equity_usd,
            "cash_and_equivalents_usd": cash_usd,
            "total_debt_usd": debt_usd,
            "inventory_usd": inventory_usd,
            "accounts_receivable_usd": receivables_usd,
            "accounts_payable_usd": payables_usd,
            # FN-14: unsuffixed TTM columns hold the local reported-currency
            # values; USD conversions (TTM-window-average FX) live in *_usd.
            "revenue_ttm": revenue_ttm_local,
            "revenue_ttm_usd": revenue_ttm_usd,
            "revenue_stub_annualized": revenue_stub_annualized,
            "revenue_stub_annualized_usd": revenue_stub_annualized_usd,
            "revenue_stub_period_days": revenue_stub_period_days,
            "revenue_stub_quality": revenue_stub_quality,
            "gross_profit_ttm": gross_profit_ttm_local,
            "gross_profit_ttm_usd": usd_ttm(gross_profit_ttm_local, ttm_results["gross_profit"]),
            "operating_income_ttm": operating_income_ttm_local,
            "operating_income_ttm_usd": operating_income_ttm_usd,
            "net_income_ttm": net_income_ttm_local,
            "net_income_ttm_usd": usd_ttm(net_income_ttm_local, ttm_results["net_income"]),
            "free_cash_flow_ttm": free_cash_flow_ttm_local,
            "free_cash_flow_ttm_usd": free_cash_flow_ttm_usd,
            "depreciation_and_amortization_ttm": depreciation_and_amortization_ttm_local,
            "depreciation_and_amortization_ttm_usd": depreciation_and_amortization_ttm_usd,
            "interest_expense_ttm": interest_expense_ttm_local,
            "interest_expense_ttm_usd": interest_expense_ttm_usd,
            "equity_issuance_proceeds_ttm": equity_issuance_proceeds_ttm_local,
            "equity_issuance_proceeds_ttm_usd": equity_issuance_proceeds_ttm_usd,
            "debt_issuance_proceeds_ttm": debt_issuance_proceeds_ttm_local,
            "debt_issuance_proceeds_ttm_usd": debt_issuance_proceeds_ttm_usd,
            "orders_ttm": orders_ttm_local,
            "orders_ttm_usd": orders_ttm_usd,
            "gross_margin": gross_margin,
            "operating_margin": operating_margin,
            "fcf_margin": fcf_margin,
            "r_and_d_pct_revenue": r_and_d_pct_revenue,
            "sbc_pct_revenue": sbc_pct_revenue,
            "net_cash": net_cash,
            "net_cash_usd": net_cash_usd,
            "net_cash_to_assets": net_cash_to_assets,
            "inventory_days": inventory_days,
            "days_sales_outstanding": days_sales_outstanding,
            "days_payables_outstanding": days_payables_outstanding,
            "cash_conversion_cycle": cash_conversion_cycle,
            "revenue_yoy_growth": cur_revenue_growth,
            "gross_profit_yoy_growth": growth(gross_profit, prev_gross_profit),
            "operating_income_yoy_growth": growth(operating_income, prev_operating_income),
            "free_cash_flow_yoy_growth": growth(free_cash_flow, prev_fcf),
            "revenue_acceleration": cur_revenue_growth - prev_revenue_growth if cur_revenue_growth is not None and prev_revenue_growth is not None else None,
            "fcf_to_net_income": fcf_to_net_income,
            "fcf_yield": safe_div(free_cash_flow_usd, market_cap),
            "ev_gross_profit": safe_div(enterprise_value, gross_profit_usd),
            "ev_operating_income": safe_div(enterprise_value, operating_income_usd),
            "market_cap": market_cap,
            "latest_price": latest_price,
            "deferred_revenue": deferred_revenue,
            "contract_liabilities": deferred_revenue,
            "remaining_performance_obligation": rpo,
            "book_to_bill": book_to_bill,
            "funded_backlog": funded_backlog,
            "funded_backlog_usd": funded_backlog_usd,
            "orders_yoy_growth": orders_yoy_growth,
            "backlog_yoy_growth": backlog_yoy_growth,
            "backlog_to_revenue": backlog_to_revenue,
            "invested_capital_usd": invested_capital_usd,
            "roic": roic,
            "asset_turnover": asset_turnover,
            "incremental_operating_margin": incremental_operating_margin,
            "inventory_growth": inventory_growth,
            "inventory_sales_growth_spread": inventory_sales_growth_spread,
            "cash_conversion_cycle_change": cash_conversion_cycle_change,
            "ebitda_ttm_usd": ebitda_ttm_usd,
            "net_debt_to_ebitda": net_debt_to_ebitda,
            "interest_coverage": interest_coverage,
            "cash_burn_ttm_usd": cash_burn_ttm_usd,
            "cash_runway_years": cash_runway_years,
            "gross_capital_raised_ttm_usd": gross_capital_raised_ttm_usd,
            "capital_raise_dependence": capital_raise_dependence,
            "diluted_shares_yoy_growth": diluted_shares_yoy_growth,
            "canonical_quality": "mapped_xbrl" + (f";{';'.join(quality_flags)}" if quality_flags else ""),
            "data_quality_status": "complete" if not reasons else "review",
            "review_reason": ";".join(reasons),
        }
    )
    coverage_components = [
        revenue,
        gross_profit,
        operating_income,
        net_income,
        assets,
        cash,
        debt_total,
        operating_cash_flow,
        free_cash_flow,
    ]
    coverage = sum(1 for value in coverage_components if value is not None) / len(coverage_components)
    # XC-7: explicit is-None coalescing so a deliberate 0.0 profile confidence
    # is not silently rewritten to 0.5.
    base_confidence = as_float(profile.get("financial_confidence"))
    if base_confidence is None:
        base_confidence = 0.5
    feature["financial_confidence"] = min(1.0, max(0.0, base_confidence * (0.6 + 0.4 * coverage)))
    return feature


def upsert_feature(conn: Any, feature: dict[str, Any]) -> None:
    now = utc_now()
    insert_columns = [*FEATURE_COLUMNS, "created_at", "updated_at"]
    placeholders_sql = ", ".join("?" for _ in insert_columns)
    update_columns = [column for column in FEATURE_COLUMNS if column not in {"ticker", "asof_date", "source_id", "model_family"}]
    update_sql = ", ".join(f"{column} = excluded.{column}" for column in [*update_columns, "updated_at"])
    conn.execute(
        f"""
        INSERT INTO feature_financial_statement({", ".join(insert_columns)})
        VALUES ({placeholders_sql})
        ON CONFLICT(ticker, asof_date, source_id, model_family) DO UPDATE SET
            {update_sql}
        """,
        tuple(feature.get(column) for column in FEATURE_COLUMNS) + (now, now),
    )


def write_report(path: Path, rows: list[dict[str, Any]]) -> None:
    # XC-10: atomic final-path write via the shared tmp + os.replace helper.
    path.parent.mkdir(parents=True, exist_ok=True)
    write_csv_atomic(path, REPORT_FIELDS, rows)


def main() -> None:
    configure_utc_logging()
    args = parse_args()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    base_dir = config_path.parent
    db_path = args.db.expanduser().resolve() if args.db else resolve_path(cfg_get(config, "paths.database_path"), base_dir=base_dir)
    model_family = str(args.model_family or cfg_get(config, "industrials_universe.initial_subsector", "defense") or "defense").strip()
    source_id = str(cfg_get(config, "sec_fundamentals.companyfacts_source_id", "sec_companyfacts") or "sec_companyfacts")
    market_source_id = str(cfg_get(config, "market_data_policy.scoring_primary_source", "yahoo_finance_adjusted") or "yahoo_finance_adjusted")
    market_fallback_source_ids = parse_source_list(cfg_get(config, "market_data_policy.scoring_fallback_sources", []))
    market_source_ids = source_priority_list(market_source_id, market_fallback_source_ids)
    output_csv = args.output_csv.expanduser().resolve() if args.output_csv else resolve_path(cfg_get(config, "sec_fundamentals.feature_output_csv"), base_dir=base_dir)
    ticker_filter = parse_ticker_list(args.tickers)
    # MK-3: FX rates older than this many days before the evaluation date are
    # rejected instead of silently converting at arbitrarily old rates.
    fx_max_staleness_days = int(cfg_get(config, "fx_rates.max_staleness_days", 30) or 30)

    with closing(connect(db_path, timeout_sec=float(cfg_get(config, "runtime.sqlite_timeout_sec", 30.0)))) as conn:
        init_db(conn)
        asof_text = str(args.asof or "").strip()
        requested_asof = parse_date(asof_text)
        if asof_text and requested_asof is None:
            # Align with siblings 04/06/07/09/10: a malformed operator date must
            # raise, never silently fall back to the latest panel asof.
            raise ValueError(f"Unparseable --asof value: {args.asof!r}; expected YYYY-MM-DD.")
        effective_asof = requested_asof or latest_panel_asof(conn, model_family=model_family, market_source_ids=market_source_ids, sec_source_id=source_id) or date.today()
        universe = load_universe(
            conn,
            model_family=model_family,
            ticker_filter=ticker_filter,
            include_historical=bool(args.include_historical),
            asof=effective_asof,
        )
        if not universe:
            raise ValueError(f"No as-of eligible industrials universe tickers found for model_family={model_family} asof={effective_asof}")
        tickers = [str(item["ticker"]) for item in universe]
        run_id = start_run(conn, run_type=RUN_TYPE, input_path=config_path)
        try:
            canonical_rows = refresh_canonical_facts(conn, source_id=source_id, model_family=model_family, tickers=tickers, asof=effective_asof)
            report_rows: list[dict[str, Any]] = []
            with conn:
                ph = placeholders(tickers)
                if not ticker_filter:
                    conn.execute(
                        f"""
                        DELETE FROM feature_financial_statement
                        WHERE asof_date = ?
                          AND source_id = ?
                          AND model_family = ?
                          AND ticker NOT IN ({ph})
                        """,
                        (effective_asof.isoformat(), source_id, model_family, *tickers),
                    )
                # SC-12: per-stage clear scoped by model_family so this build
                # only re-opens its own family's issues.
                conn.execute(
                    f"DELETE FROM data_quality_issues WHERE stage = ? AND model_family = ? AND ticker IN ({ph})",
                    (RUN_TYPE, model_family, *tickers),
                )
                for company in universe:
                    ticker = normalize_ticker(company.get("ticker"))
                    profile = load_profile(conn, ticker=ticker, model_family=model_family, company=company, source_id=source_id)
                    rows = load_canonical_rows(conn, ticker=ticker, source_id=source_id, model_family=model_family, asof=effective_asof)
                    usable_xbrl = int(profile.get("usable_xbrl_flag") or 0) == 1
                    if not usable_xbrl or not rows:
                        reason = str(profile.get("review_reason") or "no_usable_financial_facts")
                        feature = neutral_feature(
                            ticker=ticker,
                            asof=effective_asof,
                            source_id=source_id,
                            model_family=model_family,
                            company=company,
                            profile=profile,
                            reason=reason,
                        )
                    else:
                        feature = build_feature_from_facts(
                            conn,
                            ticker=ticker,
                            asof=effective_asof,
                            source_id=source_id,
                            model_family=model_family,
                            company=company,
                            profile=profile,
                            rows=rows,
                            market_source_ids=market_source_ids,
                            fx_max_staleness_days=fx_max_staleness_days,
                        )
                    # EL-3 (consumer half): dim_issuer_reporting_profile is
                    # latest-state, so profile/override effectiveness cannot be
                    # selected by valid_from at a historical asof. Whenever the
                    # profile row in force was authored/updated after the
                    # evaluation asof — whether that asof came from an explicit
                    # --asof or from the panel — flag the non-PIT provenance so
                    # calibration panels can exclude or discount those rows.
                    profile_updated = parse_date(profile.get("updated_at"))
                    if profile_updated is not None and profile_updated > effective_asof:
                        existing_quality = str(feature.get("canonical_quality") or "")
                        provenance_flag = "reporting_profile_provenance_post_asof"
                        feature["canonical_quality"] = (
                            f"{existing_quality};{provenance_flag}" if existing_quality else provenance_flag
                        )
                    upsert_feature(conn, feature)
                    if str(feature.get("data_quality_status") or "") != "complete":
                        add_issue(
                            conn,
                            ticker=ticker,
                            source_id=source_id,
                            model_family=model_family,
                            severity="warning",
                            issue_type="financial_feature_review",
                            detail=str(feature.get("review_reason") or feature.get("data_quality_status") or "review"),
                        )
                    report_rows.append(
                        {
                            "ticker": ticker,
                            "asof_date": effective_asof.isoformat(),
                            "source_id": source_id,
                            "model_family": model_family,
                            "status": "success" if str(feature.get("data_quality_status") or "") == "complete" else "review",
                            "reporting_profile": feature.get("reporting_profile", ""),
                            "reporting_standard": feature.get("reporting_standard", ""),
                            "financial_confidence": feature.get("financial_confidence", ""),
                            "data_quality_status": feature.get("data_quality_status", ""),
                            "fx_conversion_status": feature.get("fx_conversion_status", ""),
                            "revenue_usd": feature.get("revenue_usd", ""),
                            "revenue_stub_annualized_usd": feature.get("revenue_stub_annualized_usd", ""),
                            "revenue_stub_period_days": feature.get("revenue_stub_period_days", ""),
                            "revenue_stub_quality": feature.get("revenue_stub_quality", ""),
                            "assets_usd": feature.get("assets_usd", ""),
                            "gross_margin": feature.get("gross_margin", ""),
                            "operating_margin": feature.get("operating_margin", ""),
                            "fcf_margin": feature.get("fcf_margin", ""),
                            "fcf_yield": feature.get("fcf_yield", ""),
                            "ev_gross_profit": feature.get("ev_gross_profit", ""),
                            "ev_operating_income": feature.get("ev_operating_income", ""),
                            "review_reason": feature.get("review_reason", ""),
                        }
                    )
            write_report(output_csv, report_rows)
            review_count = sum(1 for row in report_rows if row["status"] != "success")
            finish_run(conn, run_id=run_id, status="success", row_count=len(report_rows), message=f"asof={effective_asof.isoformat()} rows={len(report_rows)} review={review_count} canonical_rows={canonical_rows} output={output_csv}")
            LOGGER.info("Wrote financial feature coverage report: %s", output_csv)
            LOGGER.info("Built financial features: asof=%s rows=%d review=%d canonical_rows=%d", effective_asof, len(report_rows), review_count, canonical_rows)
        except BaseException as exc:
            finish_run(conn, run_id=run_id, status="failed", row_count=0, message=f"{type(exc).__name__}: {exc}")
            raise


if __name__ == "__main__":
    main()
