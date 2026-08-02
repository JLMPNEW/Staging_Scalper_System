#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import logging
import math
import os
import sqlite3
import sys
from contextlib import closing
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable, Mapping


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from industrials.core.config import cfg_get, load_yaml, resolve_path  # noqa: E402
from industrials.core.db import connect, finish_run, init_db, start_run, utc_now  # noqa: E402
from industrials.core.logging_utils import configure_utc_logging  # noqa: E402
from industrials.core.reports import write_csv_atomic  # noqa: E402
from industrials.core.share_sources import (  # noqa: E402
    ShareConversion,
    load_share_conversions,
    resolve_share_conversion,
    resolve_share_snapshot,
)
from industrials.core.sec_predecessor_bridge import (  # noqa: E402
    DESPAC_BRIDGE_PROFILE,
    DESPAC_BRIDGE_TAXONOMY,
    load_certified_predecessor_rows,
)
from industrials.core.text_norm import normalize_ticker  # noqa: E402
from industrials.core.financial_metric_contract import (  # noqa: E402
    AVAILABILITY_MODEL_FAMILIES,
    AVAILABILITY_STATUSES,
    METRIC_OPERANDS,
    PROXY_METRIC_FEATURES,
    REQUIRED_METRIC_FEATURES,
    SOURCE_METRIC_FEATURES,
    SUPPLEMENTAL_METRICS,
    SUPPLEMENTAL_TAXONOMIES,
)


LOGGER = logging.getLogger("build_industrials_financial_features")
DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
RUN_TYPE = "build_industrials_financial_features"
XBRL_PROFILE_TAXONOMY = {
    "SEC_XBRL_US_GAAP": "us-gaap",
    "SEC_XBRL_US_GAAP_PARTIAL": "us-gaap",
    "SEC_XBRL_IFRS": "ifrs-full",
    "SEC_XBRL_IFRS_PARTIAL": "ifrs-full",
}
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
    "financial_fallback_status",
    "canonical_quality",
    "fx_conversion_status",
    "revenue_usd",
    "operating_cash_flow_usd",
    "operating_cash_flow_ttm_usd",
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
AVAILABILITY_REPORT_FIELDS = [
    "ticker",
    "asof_date",
    "model_family",
    "metric_name",
    "availability_status",
    "metric_value",
    "unit",
    "source_id",
    "accession_number",
    "filing_date",
    "period_start",
    "period_end",
    "taxonomy",
    "concept_name",
    "extraction_method",
    "confidence",
    "status_reason",
    "provenance_json",
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
    "operating_cash_flow_ttm",
    "operating_cash_flow_ttm_usd",
    "capex_ttm",
    "capex_ttm_usd",
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
    "negative_profit_valuation_flag",
    "market_cap",
    "latest_price",
    "deferred_revenue",
    "contract_liabilities",
    "remaining_performance_obligation",
    "remaining_performance_obligation_usd",
    "rpo_current",
    "rpo_current_usd",
    "book_to_bill",
    "funded_backlog",
    "funded_backlog_usd",
    "reported_backlog",
    "reported_backlog_usd",
    "contract_load_proxy",
    "contract_load_proxy_usd",
    "contract_load_proxy_source",
    "orders_yoy_growth",
    "backlog_yoy_growth",
    "backlog_to_revenue",
    "reported_backlog_yoy_growth",
    "reported_backlog_to_revenue",
    "contract_load_proxy_yoy_growth",
    "contract_load_proxy_to_revenue",
    "rpo_yoy_growth",
    "rpo_to_revenue",
    "rpo_implied_orders",
    "rpo_implied_orders_usd",
    "rpo_implied_book_to_bill",
    "invested_capital_usd",
    "roic",
    "roic_not_meaningful_flag",
    "asset_turnover",
    "incremental_operating_margin",
    "inventory_growth",
    "inventory_sales_growth_spread",
    "cash_conversion_cycle_change",
    "ebitda_ttm_usd",
    "net_debt_to_ebitda",
    "negative_ebitda_leverage_flag",
    "interest_coverage",
    "cash_burn_ttm_usd",
    "cash_runway_years",
    "gross_capital_raised_ttm_usd",
    "capital_raise_dependence",
    "diluted_shares_yoy_growth",
    "financial_metric_reported_count",
    "financial_metric_proxy_count",
    "financial_metric_unavailable_count",
    "financial_metric_classified_fraction",
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
    "costs_and_expenses",
    "gross_profit",
    "operating_income",
    "net_income",
    "eps_basic",
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
    "selling_general_admin",
    "stock_based_compensation",
    "basic_shares",
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
    "eps_basic",
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
    "basic_shares",
    "diluted_shares",
    "shares_outstanding",
    "debt_current",
    "debt_noncurrent",
    "debt_total",
    "deferred_revenue_current",
    "deferred_revenue_noncurrent",
    "deferred_revenue_total",
    "remaining_performance_obligation",
    "funded_backlog",
    "reported_backlog",
    "rpo_current",
    "costs_and_expenses",
    "selling_general_admin",
)

# Maximum spread (in days) allowed between the durations of facts combined into
# a derived metric or ratio (FN-4). Matches the 20-day tolerance already used by
# periods_are_one_year_apart.
PERIOD_DURATION_TOLERANCE_DAYS = 20

# A larger one-year increase can occur after a listing or restructuring, but it
# is not safe to score automatically. Values above this bound remain missing
# and carry an explicit review flag instead of being clipped into the model.
MAX_AUTOMATIC_DILUTED_SHARES_YOY_GROWTH = 5.0

# A selected fact whose period ended more than this many days before the anchor
# filing's period is a stale carry-forward from an old accession (e.g. a
# subtotal the issuer stopped tagging years ago) and must not publish under the
# current filing's metadata. One fiscal year + reporting slack keeps legitimate
# prior-year comparatives; multi-year-old facts are discarded.
STALE_FACT_MAX_LAG_DAYS = 400

# Order/backlog availability metrics that are structurally absent for issuers
# with cancelable (automotive-style) order models.
ORDER_BACKLOG_METRICS = {
    "orders",
    "orders_yoy_growth",
    "book_to_bill",
    "funded_backlog",
    "reported_backlog",
    "reported_backlog_yoy_growth",
    "reported_backlog_to_revenue",
    "backlog_to_revenue",
    "backlog_yoy_growth",
}

# Funded backlog is a government-contracting disclosure. Its derived metrics
# are equally inapplicable when the source disclosure is structurally absent;
# classifying only the source metric as NOT_APPLICABLE made the derivatives
# look like extraction failures.
FUNDED_BACKLOG_METRICS = {
    "funded_backlog",
    "backlog_yoy_growth",
    "backlog_to_revenue",
}

RPO_METRICS = {
    "remaining_performance_obligation",
    "rpo_current",
    "rpo_yoy_growth",
    "rpo_to_revenue",
    "rpo_implied_orders",
    "rpo_implied_book_to_bill",
}

CONTRACT_LOAD_PROXY_METRICS = {
    "contract_load_proxy",
    "contract_load_proxy_yoy_growth",
    "contract_load_proxy_to_revenue",
}

# The reviewed short-cycle policy applies to the full order/contract-load
# family. A direct value always wins before this applicability branch runs.
STRUCTURAL_CONTRACT_LOAD_METRICS = (
    ORDER_BACKLOG_METRICS | RPO_METRICS | CONTRACT_LOAD_PROXY_METRICS
)


@dataclass(frozen=True)
class TtmResult:
    value: float | None
    quality_flag: str
    window_start: date | None = None
    window_end: date | None = None


@dataclass(frozen=True)
class CccSnapshot:
    inventory_days: float
    days_sales_outstanding: float
    days_payables_outstanding: float
    cash_conversion_cycle: float
    window_end: date


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
    ELSE COALESCE(NULLIF(filing_date, ''), '9999-12-31')
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
    parser.add_argument("--availability-output-csv", type=Path, default=None)
    parser.add_argument(
        "--suppress-data-quality-issues",
        action="store_true",
        help="Build PIT rows without mutating the latest-state data-quality issue queue.",
    )
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


def sanitize_gross_proceeds_ttm(
    metric_name: str,
    value: float | None,
) -> tuple[float | None, str]:
    """Reject negative TTM values created by incompatible cumulative windows."""
    if value is None or value >= 0:
        return value, ""
    return None, f"ttm_{metric_name}_negative_gross_proceeds_discarded"


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


def backfill_mapped_xbrl_facts(
    conn: Any,
    *,
    source_ids: tuple[str, ...],
    tickers: list[str],
    asof: date,
) -> int:
    """Apply current shared aliases to facts already present in the raw store.

    This is deliberately network-free. It prevents a concept-map correction
    from requiring another SEC fetch/parse cycle merely to populate the mapped
    and canonical layers.
    """
    if not source_ids or not tickers:
        return 0
    source_ph = placeholders(list(source_ids))
    ticker_ph = placeholders(tickers)
    now = utc_now()
    with conn:
        cursor = conn.execute(
            f"""
            INSERT INTO fact_sec_xbrl_fact(
                raw_fact_id, ticker, cik, source_id, accession_number,
                form_type, filing_date, accepted_at, fiscal_year, fiscal_period,
                period_start, period_end, frame, taxonomy, concept_name,
                canonical_metric, financial_statement, period_type, unit,
                value, sign_policy, source_priority, source_detail,
                created_at, updated_at
            )
            SELECT r.raw_fact_id, r.ticker, r.cik, r.source_id,
                   r.accession_number, r.form_type, r.filing_date, r.accepted_at,
                   r.fiscal_year, r.fiscal_period, r.period_start, r.period_end,
                   r.frame, r.taxonomy, r.concept_name, m.canonical_metric,
                   m.financial_statement, m.period_type, r.unit,
                   CASE m.sign_policy
                       WHEN 'positive_abs' THEN ABS(r.raw_value)
                       WHEN 'abs' THEN ABS(r.raw_value)
                       WHEN 'negative_abs' THEN -ABS(r.raw_value)
                       WHEN 'expense_from_net' THEN MAX(-r.raw_value, 0.0)
                       ELSE r.raw_value
                   END,
                   m.sign_policy, m.priority,
                   COALESCE(r.source_detail, 'loaded_raw') || '_mapped',
                   ?, ?
            FROM fact_sec_xbrl_fact_raw AS r
            JOIN dim_xbrl_concept_map AS m
              ON m.taxonomy = r.taxonomy
             AND m.concept_name = r.concept_name
             AND m.active_flag = 1
            WHERE r.source_id IN ({source_ph})
              AND r.ticker IN ({ticker_ph})
              AND r.period_end IS NOT NULL
              AND r.period_end <= ?
              AND COALESCE(
                    NULLIF(SUBSTR(r.accepted_at, 1, 10), ''),
                    r.filing_date,
                    r.period_end
                  ) <= ?
              AND NOT EXISTS (
                    SELECT 1
                    FROM fact_sec_xbrl_fact AS f
                    WHERE f.raw_fact_id = r.raw_fact_id
                      AND f.canonical_metric = m.canonical_metric
              )
            """,
            (
                now,
                now,
                *source_ids,
                *tickers,
                asof.isoformat(),
                asof.isoformat(),
            ),
        )
    return max(0, int(cursor.rowcount or 0))


def refresh_canonical_facts(
    conn: Any,
    *,
    source_id: str,
    model_family: str,
    tickers: list[str],
    asof: date,
    supplemental_source_ids: tuple[str, ...] = (),
) -> int:
    if not tickers:
        return 0
    ph = placeholders(tickers)
    input_source_ids = tuple(
        dict.fromkeys(
            source
            for source in (source_id, *supplemental_source_ids)
            if str(source).strip()
        )
    )
    backfill_mapped_xbrl_facts(
        conn,
        source_ids=input_source_ids,
        tickers=tickers,
        asof=asof,
    )
    source_ph = placeholders(list(input_source_ids))
    append_only = os.environ.get("INDUSTRIALS_HISTORICAL_APPEND", "").strip() == "1"
    missing_only_sql = ""
    suppression_table_exists = (
        conn.execute(
            """
            SELECT 1
            FROM sqlite_master
            WHERE type = 'table'
              AND name = 'sec_parser_production_suppression'
            """
        ).fetchone()
        is not None
    )
    suppression_sql = ""
    query_params: tuple[Any, ...] = (
        *input_source_ids,
        *tickers,
        asof.isoformat(),
        asof.isoformat(),
    )
    if suppression_table_exists:
        suppression_sql = """
          AND NOT EXISTS (
              SELECT 1
              FROM sec_parser_production_suppression AS suppression
              WHERE suppression.active = 1
                AND suppression.model_family = ?
                AND suppression.ticker = f.ticker
                AND suppression.canonical_metric = f.canonical_metric
                AND suppression.period_end = f.period_end
                AND (
                    suppression.period_start = ''
                    OR suppression.period_start = COALESCE(f.period_start, '')
                )
                AND UPPER(suppression.unit) = UPPER(COALESCE(f.unit, ''))
                AND (
                    suppression.accession_number = ''
                    OR suppression.accession_number =
                       COALESCE(f.accession_number, '')
                )
                AND ABS(f.value - suppression.candidate_value)
                    <= suppression.value_tolerance
                AND suppression.valid_from <= ?
                AND COALESCE(suppression.valid_to, '9999-12-31') >= ?
          )
        """
        query_params = (
            *query_params,
            model_family,
            asof.isoformat(),
            asof.isoformat(),
        )
    if append_only:
        missing_only_sql = """
          AND NOT EXISTS (
              SELECT 1
              FROM fact_financial_statement_canonical existing
              WHERE existing.ticker = f.ticker
                AND existing.source_id = ?
                AND existing.model_family = ?
                AND existing.canonical_metric = f.canonical_metric
                AND existing.period_end = f.period_end
                AND existing.accession_number = COALESCE(f.accession_number, '')
                AND existing.unit = COALESCE(f.unit, '')
          )
        """
        query_params = (*query_params, source_id, model_family)
    rows = conn.execute(
        f"""
        SELECT f.ticker, f.source_id, f.canonical_metric, f.period_end, f.period_start,
               f.filing_date, f.accepted_at, f.accession_number, f.form_type,
               f.fiscal_year, f.fiscal_period, f.taxonomy, f.concept_name,
               f.unit, f.value, f.source_priority
        FROM fact_sec_xbrl_fact f
        JOIN dim_company c
          ON c.ticker = f.ticker
         AND COALESCE(c.cik, '') = COALESCE(f.cik, '')
        WHERE f.source_id IN ({source_ph})
          AND f.ticker IN ({ph})
          AND f.period_end IS NOT NULL
          AND ({ACCEPTED_DATE_SQL}) <= ?
          AND f.period_end <= ?
          {suppression_sql}
          {missing_only_sql}
        ORDER BY f.ticker, f.period_end, f.accession_number, f.canonical_metric,
                 f.source_priority DESC, f.concept_name DESC
        """,
        query_params,
    ).fetchall()
    now = utc_now()
    with conn:
        # SC-4: scope the DELETE with the same accepted-date predicate used for
        # the re-insert SELECT, so a historical-asof rebuild only replaces the
        # facts visible at that asof and never destroys later-accepted facts.
        # Rows accepted after asof are preserved; load_canonical_rows PIT-filters
        # them out at read time via the same predicate.
        if not append_only:
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
                    source_id,
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


def load_profile(
    conn: Any,
    *,
    ticker: str,
    model_family: str,
    company: dict[str, Any],
    source_id: str,
    asof: date,
) -> dict[str, Any]:
    if model_family in {"machinery", "transportation"}:
        row = conn.execute(
            """
            SELECT *
            FROM dim_issuer_reporting_profile_history
            WHERE ticker = ?
              AND model_family = ?
              AND profile_asof_date <= ?
            ORDER BY profile_asof_date DESC
            LIMIT 1
            """,
            (ticker, model_family, asof.isoformat()),
        ).fetchone()
    else:
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
    if model_family in {"machinery", "transportation"}:
        reason = "reporting_profile_snapshot_missing_for_asof"
    return {
        "ticker": ticker,
        "model_family": model_family,
        "cik": company.get("cik"),
        "country": country,
        "reporting_profile": profile,
        "reporting_standard": standard,
        "primary_taxonomy": "",
        "fallback_status": "neutral_low_confidence",
        "financial_confidence": confidence,
        "usable_xbrl_flag": 0,
        "source_id": source_id,
        "review_reason": reason,
        "profile_asof_date": (
            asof.isoformat()
            if model_family in {"machinery", "transportation"}
            else ""
        ),
    }


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


def select_fact_at(rows: list[dict[str, Any]], metric: str, iso_period_end: str) -> dict[str, Any] | None:
    candidates = [
        row
        for row in rows
        if str(row.get("canonical_metric") or "") == metric
        and as_float(row.get("value")) is not None
        and str(row.get("period_end") or "")[:10] == iso_period_end
    ]
    return max(candidates, key=row_sort_key) if candidates else None


def capital_at_instant(
    rows: list[dict[str, Any]],
    iso_end: str,
) -> float | None:
    equity_at = select_fact_at(rows, "equity", iso_end)
    cash_at = select_fact_at(rows, "cash_and_equivalents", iso_end)
    if equity_at is None or cash_at is None:
        return None
    total_at = select_fact_at(rows, "debt_total", iso_end)
    if total_at is not None:
        debt_at = as_float(total_at.get("value"))
    else:
        current_at = select_fact_at(rows, "debt_current", iso_end)
        noncurrent_at = select_fact_at(rows, "debt_noncurrent", iso_end)
        current_value = as_float(current_at.get("value")) if current_at is not None else None
        noncurrent_value = as_float(noncurrent_at.get("value")) if noncurrent_at is not None else None
        debt_at = (
            current_value + noncurrent_value
            if current_value is not None and noncurrent_value is not None
            else None
        )
    if debt_at is None:
        return None
    equity_value = as_float(equity_at.get("value"))
    cash_value = as_float(cash_at.get("value"))
    if equity_value is None or cash_value is None:
        return None
    return debt_at + equity_value - cash_value


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


def select_latest_comparable_pair(
    rows: list[dict[str, Any]],
    metric: str,
    *,
    instant_metric: bool = False,
    prefer_annual: bool = False,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    candidates = [
        row
        for row in rows
        if str(row.get("canonical_metric") or "") == metric
        and as_float(row.get("value")) is not None
    ]
    if prefer_annual:
        annual = [row for row in candidates if is_annual_fact(row)]
        if annual:
            candidates = annual
    candidates.sort(key=row_sort_key, reverse=True)
    seen_periods: set[tuple[str, str]] = set()
    for current in candidates:
        period_key = (
            str(current.get("period_start") or ""),
            str(current.get("period_end") or ""),
        )
        if period_key in seen_periods:
            continue
        seen_periods.add(period_key)
        previous = select_previous_comparable(
            rows,
            metric,
            current,
            instant_metric=instant_metric,
        )
        if previous is not None:
            return current, previous
    return None, None


def validated_diluted_share_growth(
    current: dict[str, Any] | None,
    previous: dict[str, Any] | None,
) -> tuple[float | None, bool]:
    value = growth(
        as_float(current.get("value")) if current is not None else None,
        as_float(previous.get("value")) if previous is not None else None,
    )
    if value is None:
        return None, False
    if value < -1.0 or value > MAX_AUTOMATIC_DILUTED_SHARES_YOY_GROWTH:
        return None, True
    return value, False


def select_aligned_inventory_revenue_growth(
    rows: list[dict[str, Any]],
) -> tuple[float | None, float | None, float | None]:
    """Return inventory growth, revenue growth, and their spread from one
    shared pair of fiscal period ends. This avoids combining quarterly
    inventory growth with annual revenue growth."""
    inventories = rows_for_metric(rows, "inventory")
    inventories.sort(key=row_sort_key, reverse=True)
    for current_inventory in inventories:
        previous_inventory = select_previous_comparable(
            rows,
            "inventory",
            current_inventory,
            instant_metric=True,
        )
        if previous_inventory is None:
            continue
        current_end = str(current_inventory.get("period_end") or "")[:10]
        previous_end = str(previous_inventory.get("period_end") or "")[:10]
        current_revenues = [
            row
            for row in rows_for_metric(rows, "revenue")
            if str(row.get("period_end") or "")[:10] == current_end
        ]
        current_revenues.sort(
            key=lambda row: (is_annual_fact(row), row_sort_key(row)),
            reverse=True,
        )
        for current_revenue in current_revenues:
            previous_revenues = [
                row
                for row in rows_for_metric(rows, "revenue")
                if str(row.get("period_end") or "")[:10] == previous_end
                and periods_are_one_year_apart(current_revenue, row)
            ]
            if not previous_revenues:
                continue
            previous_revenue = max(previous_revenues, key=row_sort_key)
            inventory_growth = growth(
                as_float(current_inventory.get("value")),
                as_float(previous_inventory.get("value")),
            )
            revenue_growth = growth(
                as_float(current_revenue.get("value")),
                as_float(previous_revenue.get("value")),
            )
            if inventory_growth is not None and revenue_growth is not None:
                return inventory_growth, revenue_growth, inventory_growth - revenue_growth
    return None, None, None


def eps_basic_equals_diluted_for_share_period(
    rows: list[dict[str, Any]],
    share_row: dict[str, Any],
) -> bool:
    basic_eps_rows = [
        row
        for row in rows_for_metric(rows, "eps_basic")
        if combined_periods_match(share_row, row)
    ]
    diluted_eps_rows = [
        row
        for row in rows_for_metric(rows, "eps_diluted")
        if combined_periods_match(share_row, row)
    ]
    return any(
        math.isclose(
            as_float(basic.get("value")) or 0.0,
            as_float(diluted.get("value")) or 0.0,
            rel_tol=1e-9,
            abs_tol=1e-12,
        )
        for basic in basic_eps_rows
        for diluted in diluted_eps_rows
        if combined_periods_match(basic, diluted)
        and as_float(basic.get("value")) is not None
        and as_float(diluted.get("value")) is not None
    )


def select_basic_share_pair_when_eps_equal(
    rows: list[dict[str, Any]],
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    candidates = rows_for_metric(rows, "basic_shares")
    candidates.sort(key=row_sort_key, reverse=True)
    for current in candidates:
        previous = select_previous_comparable(rows, "basic_shares", current)
        if (
            previous is not None
            and eps_basic_equals_diluted_for_share_period(rows, current)
            and eps_basic_equals_diluted_for_share_period(rows, previous)
        ):
            return current, previous
    return None, None


def is_recent_public_transition(
    company: dict[str, Any],
    *,
    asof: date,
    maximum_age_days: int = 550,
) -> bool:
    membership_start = parse_date(company.get("membership_start_date"))
    if membership_start is None:
        return False
    age_days = (asof - membership_start).days
    return 0 <= age_days <= maximum_age_days


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

    latest_interim_start = parse_date(latest_interim.get("period_start"))
    latest_interim_days = duration_days(latest_interim)
    if (
        latest_annual_end is not None
        and latest_interim_start is not None
        and latest_interim_days is not None
        and latest_interim_days <= 130
    ):
        start_gap_days = (latest_interim_start - latest_annual_end).days
        if start_gap_days < -1 or start_gap_days > PERIOD_DURATION_TOLERANCE_DAYS:
            # An unlabeled discrete Q2/Q3 fact is not a cumulative interim.
            # Prefer a complete four-quarter window instead of applying the
            # annual-plus-Q1 formula to the wrong fiscal quarter.
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


def derived_cost_of_sales_ttm_result(rows: list[dict[str, Any]]) -> TtmResult:
    reported = ttm_metric_result(rows, "cost_of_sales")
    if reported.value is not None:
        return reported
    revenue = ttm_metric_result(rows, "revenue")
    gross_profit = ttm_metric_result(rows, "gross_profit")
    if (
        revenue.value is None
        or gross_profit.value is None
        or not ttm_windows_match(revenue, gross_profit)
    ):
        return reported
    return TtmResult(
        revenue.value - gross_profit.value,
        "ccc_cost_of_sales_ttm_derived_from_revenue_less_gross_profit",
        window_start=revenue.window_start,
        window_end=revenue.window_end,
    )


def select_instant_fact_near(
    rows: list[dict[str, Any]],
    metric: str,
    *,
    target_end: date,
    tolerance_days: int = PERIOD_DURATION_TOLERANCE_DAYS,
) -> dict[str, Any] | None:
    candidates = [
        row
        for row in rows
        if str(row.get("canonical_metric") or "") == metric
        and as_float(row.get("value")) is not None
        and not str(row.get("period_start") or "").strip()
        and (period_end := parse_date(row.get("period_end"))) is not None
        and abs((period_end - target_end).days) <= tolerance_days
    ]
    if not candidates:
        return None
    return max(
        candidates,
        key=lambda row: (
            -abs(((parse_date(row.get("period_end")) or target_end) - target_end).days),
            row_sort_key(row),
        ),
    )


def ttm_rows_ending_near(
    rows: list[dict[str, Any]],
    *,
    target_end: date | None,
) -> list[dict[str, Any]]:
    if target_end is None:
        return rows
    maximum_end = target_end + timedelta(days=PERIOD_DURATION_TOLERANCE_DAYS)
    return [
        row
        for row in rows
        if (period_end := parse_date(row.get("period_end"))) is not None
        and period_end <= maximum_end
    ]


def direct_ttm_result_uses_taxonomy(
    rows: list[dict[str, Any]],
    metric: str,
    result: TtmResult,
    *,
    allowed_taxonomies: frozenset[str],
) -> bool:
    if result.value is None or result.window_start is None or result.window_end is None:
        return False
    for row in rows_for_metric(rows, metric):
        row_start = parse_date(row.get("period_start"))
        row_end = parse_date(row.get("period_end"))
        if row_start is None or row_end is None:
            continue
        if (
            str(row.get("taxonomy") or "") in allowed_taxonomies
            and abs((row_start - result.window_start).days)
            <= PERIOD_DURATION_TOLERANCE_DAYS
            and abs((row_end - result.window_end).days)
            <= PERIOD_DURATION_TOLERANCE_DAYS
        ):
            return True
    return False


def calculate_book_to_bill(
    rows: list[dict[str, Any]],
    *,
    orders: TtmResult,
    revenue: TtmResult,
) -> tuple[float | None, str]:
    if (
        orders.value is None
        or revenue.value is None
        or revenue.value <= 0
    ):
        return None, ""
    if ttm_windows_match(orders, revenue):
        return orders.value / revenue.value, ""
    if (
        orders.window_end is not None
        and revenue.window_end is not None
        and (revenue.window_end - orders.window_end).days
        > STALE_FACT_MAX_LAG_DAYS
    ):
        return None, "stale_orders_window_book_to_bill"
    if (
        orders.window_end is None
        or not direct_ttm_result_uses_taxonomy(
            rows,
            "orders",
            orders,
            allowed_taxonomies=frozenset({"dedicated-parser"}),
        )
    ):
        return None, "period_mismatch_book_to_bill"
    aligned_revenue = ttm_metric_result(
        ttm_rows_ending_near(rows, target_end=orders.window_end),
        "revenue",
    )
    if (
        aligned_revenue.value is None
        or aligned_revenue.value <= 0
        or not ttm_windows_match(orders, aligned_revenue)
    ):
        return None, "period_mismatch_book_to_bill"
    return (
        orders.value / aligned_revenue.value,
        "book_to_bill_aligned_to_latest_reported_orders_window",
    )


def revenue_ttm_aligned_to_instant_metric(
    rows: list[dict[str, Any]],
    *,
    metric_period_end: date | None,
    current_revenue: TtmResult,
) -> tuple[TtmResult, str]:
    if metric_period_end is None:
        return TtmResult(None, ""), "period_mismatch_missing_metric_date"
    if (
        current_revenue.value is not None
        and current_revenue.value > 0
        and current_revenue.window_end is not None
        and abs((metric_period_end - current_revenue.window_end).days)
        <= PERIOD_DURATION_TOLERANCE_DAYS
    ):
        return current_revenue, ""
    if (
        current_revenue.window_end is not None
        and (current_revenue.window_end - metric_period_end).days
        > STALE_FACT_MAX_LAG_DAYS
    ):
        return TtmResult(None, ""), "stale_instant_metric_revenue_alignment"
    aligned = ttm_metric_result(
        ttm_rows_ending_near(rows, target_end=metric_period_end),
        "revenue",
    )
    if (
        aligned.value is None
        or aligned.value <= 0
        or aligned.window_end is None
        or abs((metric_period_end - aligned.window_end).days)
        > PERIOD_DURATION_TOLERANCE_DAYS
    ):
        return TtmResult(None, ""), "period_mismatch_instant_metric_to_revenue"
    return aligned, "aligned_to_disclosure_period"


def build_ccc_snapshot(
    rows: list[dict[str, Any]],
    *,
    target_end: date | None = None,
) -> tuple[CccSnapshot | None, list[str]]:
    scoped = ttm_rows_ending_near(rows, target_end=target_end)
    revenue = ttm_metric_result(scoped, "revenue")
    cost_of_sales = derived_cost_of_sales_ttm_result(scoped)
    flags = [
        flag
        for flag in (revenue.quality_flag, cost_of_sales.quality_flag)
        if flag
    ]
    if (
        revenue.value is None
        or cost_of_sales.value is None
        or revenue.value <= 0
        or cost_of_sales.value <= 0
        or not ttm_windows_match(revenue, cost_of_sales)
        or revenue.window_end is None
    ):
        flags.append("ccc_unavailable_missing_or_misaligned_ttm_denominators")
        return None, flags
    if target_end is not None and abs((revenue.window_end - target_end).days) > PERIOD_DURATION_TOLERANCE_DAYS:
        flags.append("ccc_unavailable_no_ttm_window_near_target")
        return None, flags
    balance_rows = {
        metric: select_instant_fact_near(scoped, metric, target_end=revenue.window_end)
        for metric in ("inventory", "accounts_receivable", "accounts_payable")
    }
    if any(row is None for row in balance_rows.values()):
        missing = sorted(metric for metric, row in balance_rows.items() if row is None)
        flags.append("ccc_unavailable_missing_aligned_balances:" + ",".join(missing))
        return None, flags
    inventory = as_float((balance_rows["inventory"] or {}).get("value"))
    receivables = as_float((balance_rows["accounts_receivable"] or {}).get("value"))
    payables = as_float((balance_rows["accounts_payable"] or {}).get("value"))
    if any(value is None or value < 0 for value in (inventory, receivables, payables)):
        flags.append("ccc_unavailable_invalid_aligned_balance")
        return None, flags
    inventory_days = (inventory or 0.0) / cost_of_sales.value * 365.0
    dso = (receivables or 0.0) / revenue.value * 365.0
    dpo = (payables or 0.0) / cost_of_sales.value * 365.0
    return (
        CccSnapshot(
            inventory_days=inventory_days,
            days_sales_outstanding=dso,
            days_payables_outstanding=dpo,
            cash_conversion_cycle=inventory_days + dso - dpo,
            window_end=revenue.window_end,
        ),
        flags,
    )


def prior_year_date(value: date) -> date:
    try:
        return value.replace(year=value.year - 1)
    except ValueError:
        return value.replace(year=value.year - 1, day=28)


def build_machinery_ccc(
    rows: list[dict[str, Any]],
) -> tuple[CccSnapshot | None, float | None, list[str]]:
    current, flags = build_ccc_snapshot(rows)
    if current is None:
        return None, None, flags
    previous, previous_flags = build_ccc_snapshot(
        rows,
        target_end=prior_year_date(current.window_end),
    )
    flags.extend(previous_flags)
    if previous is None:
        return current, None, flags
    if not 345 <= abs((current.window_end - previous.window_end).days) <= 385:
        flags.append("ccc_change_unavailable_noncomparable_windows")
        return current, None, flags
    return current, current.cash_conversion_cycle - previous.cash_conversion_cycle, flags


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


def rows_for_reporting_profile(
    rows: list[dict[str, Any]],
    profile: dict[str, Any],
    *,
    model_family: str = "",
    machinery_sec_text_core_metrics: frozenset[str] = frozenset(),
) -> tuple[list[dict[str, Any]], str | None]:
    """Keep promoted XBRL profiles on their declared accounting taxonomy.

    Archive text facts may coexist with XBRL facts for fallback issuers. Once a
    profile is promoted to an XBRL profile, allowing those text rows back into
    currency and period selection can silently replace consolidated tagged
    values with table values in display units.
    """
    reporting_profile = str(profile.get("reporting_profile") or "").strip().upper()
    if reporting_profile == DESPAC_BRIDGE_PROFILE:
        allowed = {"us-gaap", DESPAC_BRIDGE_TAXONOMY}
        filtered = [row for row in rows if str(row.get("taxonomy") or "") in allowed]
        if model_family in AVAILABILITY_MODEL_FAMILIES:
            filtered.extend(
                row
                for row in rows
                if str(row.get("taxonomy") or "") in SUPPLEMENTAL_TAXONOMIES
                and str(row.get("canonical_metric") or "")
                in SUPPLEMENTAL_METRICS | machinery_sec_text_core_metrics
            )
        return filtered, "us-gaap+audited-predecessor"
    if model_family in AVAILABILITY_MODEL_FAMILIES and reporting_profile in {
        "SEC_ARCHIVE_TEXT_TABLE",
        "SEC_ARCHIVE_TEXT_TABLE_PARTIAL",
    }:
        filtered = [
            row
            for row in rows
            if str(row.get("taxonomy") or "") == "sec-text"
        ]
        filtered.extend(
            row
            for row in rows
            if str(row.get("taxonomy") or "")
            in SUPPLEMENTAL_TAXONOMIES - {"sec-text"}
            and str(row.get("canonical_metric") or "")
            in SUPPLEMENTAL_METRICS
            | machinery_sec_text_core_metrics
        )
        return filtered, "sec-text"
    target_taxonomy = XBRL_PROFILE_TAXONOMY.get(reporting_profile)
    if target_taxonomy is None:
        return rows, None
    filtered = [row for row in rows if str(row.get("taxonomy") or "") == target_taxonomy]
    if model_family in AVAILABILITY_MODEL_FAMILIES:
        filtered.extend(
            row
            for row in rows
            if str(row.get("taxonomy") or "") in SUPPLEMENTAL_TAXONOMIES
            and str(row.get("canonical_metric") or "")
            in SUPPLEMENTAL_METRICS | machinery_sec_text_core_metrics
        )
    return filtered, target_taxonomy


def reporting_currency_basis_rows(
    rows: list[dict[str, Any]],
    *,
    profile_taxonomy_filter: str | None,
) -> list[dict[str, Any]]:
    if profile_taxonomy_filter in {"us-gaap", "ifrs-full"}:
        primary = [
            row
            for row in rows
            if str(row.get("taxonomy") or "") == profile_taxonomy_filter
        ]
        return primary or rows
    if profile_taxonomy_filter == "us-gaap+audited-predecessor":
        primary = [
            row
            for row in rows
            if str(row.get("taxonomy") or "") in {"us-gaap", DESPAC_BRIDGE_TAXONOMY}
        ]
        return primary or rows
    return rows


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


def latest_unadjusted_close(
    conn: Any,
    *,
    ticker: str,
    market_source_ids: list[str],
    asof: date,
    max_staleness_days: int = 7,
) -> float | None:
    """Return a PIT unadjusted close suitable for a market-cap denominator."""
    for market_source_id in market_source_ids:
        try:
            row = conn.execute(
                """
                SELECT bar_date, close
                FROM fact_price_ohlcv
                WHERE ticker = ?
                  AND source_id = ?
                  AND bar_date <= ?
                  AND COALESCE(close, 0.0) > 0.0
                ORDER BY bar_date DESC
                LIMIT 1
                """,
                (ticker, market_source_id, asof.isoformat()),
            ).fetchone()
        except sqlite3.OperationalError as exc:
            if "no such table" not in str(exc).lower():
                raise
            return None
        if row is None:
            continue
        bar_date = parse_date(row["bar_date"])
        price = as_float(row["close"])
        if (
            bar_date is not None
            and price is not None
            and price > 0.0
            and 0 <= (asof - bar_date).days <= max_staleness_days
        ):
            return price
    return None


def latest_market_values(conn: Any, *, ticker: str, market_source_ids: list[str], model_family: str, asof: date) -> tuple[float | None, float | None]:
    # Family-scoped share observations are resolved before the legacy global
    # market snapshot. Families that have not opted into the share loader have
    # no rows and retain their legacy behavior below.
    try:
        shares = resolve_share_snapshot(
            conn,
            ticker=ticker,
            model_family=model_family,
            asof=asof,
        )
    except sqlite3.OperationalError as exc:
        if "no such table" not in str(exc).lower():
            raise
        shares = None
    if shares is not None and shares.market_cap is not None:
        return shares.market_cap, shares.price

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
        if market_cap is not None:
            return market_cap, latest_price

    # Filing-derived share observations normally do not carry a market price.
    # Combine them only with an unadjusted PIT close from the configured market
    # source. Adjusted closes can disagree with contemporaneous share counts.
    unadjusted_close = latest_unadjusted_close(
        conn,
        ticker=ticker,
        market_source_ids=market_source_ids,
        asof=asof,
    )
    if shares is not None and shares.shares_outstanding is not None and unadjusted_close is not None:
        return shares.shares_outstanding * unadjusted_close, unadjusted_close
    if shares is not None and shares.price is not None:
        return None, shares.price

    # Legacy families may not yet have family-scoped share observations. Keep
    # their historical price-only behavior without fabricating market cap.
    for market_source_id in market_source_ids:
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
        if latest_price is not None:
            return None, latest_price
    return None, None

def diluted_share_market_cap_proxy(
    conn: Any,
    *,
    ticker: str,
    model_family: str,
    asof: date,
    diluted_shares: object,
    country: object,
    market_source_ids: list[str],
    conversions: Mapping[str, Iterable[ShareConversion]],
) -> tuple[float | None, float | None, str]:
    shares = as_float(diluted_shares)
    if shares is None or shares <= 0.0:
        return None, None, ""

    country_key = str(country or "").strip().upper()
    if country_key in {"UNITED STATES", "USA", "US"}:
        ratio = 1.0
        method = "market_cap_proxy_diluted_shares_domestic"
    else:
        conversion = resolve_share_conversion(
            conversions, ticker=ticker, day=asof
        )
        if (
            conversion is None
            or conversion.status not in {"REVIEWED_ADR", "REVIEWED_DIRECT"}
            or conversion.ratio is None
        ):
            return None, None, ""
        ratio = conversion.ratio
        method = f"market_cap_proxy_diluted_shares_{conversion.status.lower()}"

    price = latest_unadjusted_close(
        conn,
        ticker=ticker,
        market_source_ids=market_source_ids,
        asof=asof,
    )
    if price is None:
        return None, None, ""
    return shares / ratio * price, price, method


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


def is_development_stage_revenue_policy(*, company: dict[str, Any], profile: dict[str, Any]) -> bool:
    development_stage = str(company.get("development_stage") or "").strip().lower()
    reporting_profile = str(profile.get("reporting_profile") or "").strip().upper()
    return (
        development_stage in {"development_stage", "pre_revenue", "pre_commercial", "speculative", "recent_ipo"}
        or reporting_profile in {"RECENT_IPO_DEVELOPMENT_STAGE", "VERIFIED_PRE_REVENUE_US_GAAP"}
    )


def should_default_missing_revenue_to_zero(
    *,
    company: dict[str, Any],
    profile: dict[str, Any],
    operating_cash_flow: float | None,
) -> bool:
    return (
        is_development_stage_revenue_policy(company=company, profile=profile)
        and operating_cash_flow is not None
        and operating_cash_flow < 0.0
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
    share_conversions: Mapping[str, Iterable[ShareConversion]] | None = None,
    enable_statement_share_fallback: bool = False,
    machinery_sec_text_core_metrics: frozenset[str] = frozenset(),
) -> dict[str, Any]:
    feature = base_feature(ticker=ticker, asof=asof, source_id=source_id, model_family=model_family, company=company, profile=profile)
    rows, profile_taxonomy_filter = rows_for_reporting_profile(
        rows,
        profile,
        model_family=model_family,
        machinery_sec_text_core_metrics=machinery_sec_text_core_metrics,
    )
    # FN-3: resolve one reporting currency for the ticker slice and restrict
    # candidate facts to it. Facts available only in another currency are
    # excluded from selection and flagged per metric.
    currency_basis = reporting_currency_basis_rows(
        rows,
        profile_taxonomy_filter=profile_taxonomy_filter,
    )
    reported_currency = resolve_reporting_currency(
        currency_basis,
        str(company.get("currency") or "USD"),
    )
    currency_rows = [row for row in rows if unit_currency(row.get("unit")) in (None, reported_currency)]
    currency_flags: list[str] = []
    period_flags: list[str] = []
    selected: dict[str, dict[str, Any] | None] = {}
    for metric in METRIC_SELECTION_ORDER:
        selected[metric] = select_fact(currency_rows, metric, prefer_annual=metric in DURATION_METRICS)
        if selected[metric] is None and select_fact(rows, metric, prefer_annual=metric in DURATION_METRICS) is not None:
            currency_flags.append(f"currency_mismatch_{metric}")

    # Staleness screen: a fact whose period ended years before the anchor
    # period is a carry-forward from an old accession (verified real case: a
    # FY2020 operating income publishing under a FY2025 filing after the issuer
    # stopped tagging the subtotal). Discard rather than misattribute.
    anchor_preview = next(
        (selected[metric] for metric in METRIC_SELECTION_ORDER if selected.get(metric) is not None), None
    )
    anchor_period_end = parse_date(anchor_preview.get("period_end")) if anchor_preview is not None else None
    if anchor_period_end is not None:
        stale_cutoff_iso = (anchor_period_end - timedelta(days=STALE_FACT_MAX_LAG_DAYS)).isoformat()
        for metric, fact in selected.items():
            if fact is None:
                continue
            fact_end = parse_date(fact.get("period_end"))
            if fact_end is not None and (anchor_period_end - fact_end).days > STALE_FACT_MAX_LAG_DAYS:
                # Before discarding outright, retry against recent facts only:
                # an issuer that stopped annual tagging may still tag the metric
                # quarterly, and prefer_annual would otherwise pin the stale FY.
                fresh_candidates = [
                    row
                    for row in currency_rows
                    if str(row.get("canonical_metric") or "") == metric
                    and str(row.get("period_end") or "")[:10] >= stale_cutoff_iso
                ]
                replacement = select_fact(fresh_candidates, metric, prefer_annual=metric in DURATION_METRICS)
                selected[metric] = replacement
                if replacement is None:
                    period_flags.append(f"stale_fact_discarded_{metric}")
                else:
                    period_flags.append(f"stale_fact_replaced_with_recent_{metric}")

    revenue = metric_value(selected, "revenue")
    cost_of_sales = metric_value(selected, "cost_of_sales")
    gross_profit = metric_value(selected, "gross_profit")
    cost_of_sales_derived = False
    if gross_profit is None and revenue is not None and cost_of_sales is not None:
        # FN-4: only derive across facts from the same fiscal period.
        if combined_periods_match(selected.get("revenue"), selected.get("cost_of_sales")):
            gross_profit = revenue - cost_of_sales
        else:
            period_flags.append("period_mismatch_gross_profit")
    if cost_of_sales is None and revenue is not None and gross_profit is not None:
        if combined_periods_match(selected.get("revenue"), selected.get("gross_profit")):
            cost_of_sales = revenue - gross_profit
            cost_of_sales_derived = True
        else:
            period_flags.append("period_mismatch_cost_of_sales")
    operating_income = metric_value(selected, "operating_income")
    net_income = metric_value(selected, "net_income")
    eps_diluted = metric_value(selected, "eps_diluted")
    assets = metric_value(selected, "assets")
    liabilities = metric_value(selected, "liabilities")
    equity = metric_value(selected, "equity")
    cash = metric_value(selected, "cash_and_equivalents")
    debt_total = metric_value(selected, "debt_total")
    if (
        debt_total is None
        and selected.get("debt_current") is not None
        and selected.get("debt_noncurrent") is not None
    ):
        if combined_periods_match(selected.get("debt_current"), selected.get("debt_noncurrent")):
            debt_current = metric_value(selected, "debt_current")
            debt_noncurrent = metric_value(selected, "debt_noncurrent")
            if debt_current is not None and debt_noncurrent is not None:
                debt_total = debt_current + debt_noncurrent
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
    costs_and_expenses = metric_value(selected, "costs_and_expenses")
    operating_income_derived = False
    if operating_income is None and revenue is not None and costs_and_expenses is not None:
        # Issuers that drop the operating-income subtotal but tag total costs
        # (us-gaap:CostsAndExpenses) still determine it — UNLESS this filer's
        # rollup includes non-operating items, in which case revenue - costs
        # collapses to pretax income and is NOT an operating subtotal
        # (verified real case: SWK). FN-4 same-period guard mirrors the
        # gross-profit derivation above.
        if combined_periods_match(selected.get("revenue"), selected.get("costs_and_expenses")):
            derived_operating_income = revenue - costs_and_expenses
            pretax_comparable = pretax_income is not None and combined_periods_match(
                selected.get("revenue"), selected.get("pretax_income")
            )
            collapse_tolerance = max(abs(pretax_income or 0.0) * 0.02, abs(revenue) * 0.001)
            if pretax_income is not None and not pretax_comparable:
                # A different-period pretax cannot arbitrate the collapse test —
                # neither accept nor reject on a coincidental match.
                quality_flags_pending_operating_income = (
                    "operating_income_derivation_skipped_collapse_guard_unavailable"
                )
            elif pretax_income is not None and abs(derived_operating_income - pretax_income) <= collapse_tolerance:
                quality_flags_pending_operating_income = "operating_income_derivation_rejected_costs_include_nonoperating"
            else:
                operating_income = derived_operating_income
                operating_income_derived = True
                quality_flags_pending_operating_income = ""
        else:
            period_flags.append("period_mismatch_operating_income")
            quality_flags_pending_operating_income = ""
    else:
        quality_flags_pending_operating_income = ""
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
    quality_flags_pending_synthetic = ""
    if operating_income is None and gross_profit is not None:
        # Last-resort synthetic operating subtotal (top-down EBIT
        # reconstitution) for filers that tag neither OperatingIncomeLoss nor a
        # collapse-free CostsAndExpenses rollup. Omits restructuring/other
        # operating charges, so it can overstate — always flagged, and the
        # excl-R&D variant is flagged separately.
        sga = metric_value(selected, "selling_general_admin")
        if sga is not None:
            gross_profit_operands = (
                [selected.get("gross_profit")]
                if selected.get("gross_profit") is not None
                else [selected.get("revenue"), selected.get("cost_of_sales")]
            )
            if combined_periods_match(*gross_profit_operands, selected.get("selling_general_admin")):
                synthetic_operating_income = gross_profit - sga
                if r_and_d is not None and combined_periods_match(
                    selected.get("selling_general_admin"),
                    selected.get("research_and_development"),
                ):
                    synthetic_operating_income -= r_and_d
                    quality_flags_pending_synthetic = "operating_income_synthetic_gross_profit_less_sga_r_and_d"
                else:
                    quality_flags_pending_synthetic = "operating_income_synthetic_gross_profit_less_sga_excl_r_and_d"
                operating_income = synthetic_operating_income
    deferred_revenue = metric_value(selected, "deferred_revenue_total")
    if deferred_revenue is None and (selected.get("deferred_revenue_current") is not None or selected.get("deferred_revenue_noncurrent") is not None):
        if combined_periods_match(selected.get("deferred_revenue_current"), selected.get("deferred_revenue_noncurrent")):
            deferred_revenue = (metric_value(selected, "deferred_revenue_current") or 0.0) + (metric_value(selected, "deferred_revenue_noncurrent") or 0.0)
        else:
            period_flags.append("period_mismatch_deferred_revenue")
    rpo = metric_value(selected, "remaining_performance_obligation")
    rpo_current = metric_value(selected, "rpo_current")
    funded_backlog = metric_value(selected, "funded_backlog")
    reported_backlog = metric_value(selected, "reported_backlog")
    zero_revenue_defaulted = False
    zero_revenue_validated = False
    zero_revenue_validation_failed = False
    development_revenue_policy = is_development_stage_revenue_policy(company=company, profile=profile)
    if revenue is None and development_revenue_policy:
        if should_default_missing_revenue_to_zero(
            company=company,
            profile=profile,
            operating_cash_flow=operating_cash_flow,
        ):
            revenue = 0.0
            zero_revenue_defaulted = True
            zero_revenue_validated = True
        else:
            zero_revenue_validation_failed = True
    elif revenue == 0.0 and development_revenue_policy:
        if operating_cash_flow is not None and operating_cash_flow < 0.0:
            zero_revenue_validated = True
        else:
            revenue = None
            zero_revenue_validation_failed = True

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
    rpo_usd = usd_balance(rpo)
    rpo_current_usd = usd_balance(rpo_current)
    funded_backlog_usd = usd_balance(funded_backlog)
    reported_backlog_usd = usd_balance(reported_backlog)
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
    market_cap_proxy_method = ""
    if (
        market_cap is None
        and enable_statement_share_fallback
    ):
        proxy_cap, proxy_price, market_cap_proxy_method = (
            diluted_share_market_cap_proxy(
                conn,
                ticker=ticker,
                model_family=model_family,
                asof=asof,
                diluted_shares=diluted_shares,
                country=company.get("country"),
                market_source_ids=market_source_ids,
                conversions=share_conversions or {},
            )
        )
        if proxy_cap is not None:
            market_cap = proxy_cap
            latest_price = proxy_price
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
            "cost_of_sales",
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
            "costs_and_expenses",
            "selling_general_admin",
            "research_and_development",
        ]
    }
    # TTM analogue of the single-period staleness screen: without it, a
    # years-old annual fact (issuer stopped tagging the metric) still resolves
    # as "the TTM" and flows into EBITDA/ROIC/coverage under the current
    # filing's metadata — and, being non-None, suppresses the derivation
    # fallbacks that exist for exactly that situation.
    if anchor_period_end is not None:
        for ttm_metric, ttm_result in list(ttm_results.items()):
            if (
                ttm_result.value is not None
                and ttm_result.window_end is not None
                and (anchor_period_end - ttm_result.window_end).days > STALE_FACT_MAX_LAG_DAYS
            ):
                ttm_results[ttm_metric] = TtmResult(
                    None, f"ttm_{ttm_metric}_stale_window_discarded"
                )
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
    # FN-4 for TTM composites: OCF and capex TTMs can resolve from different
    # windows (annual vs four-quarter) under mixed interim tagging.
    fcf_ttm_window_mismatch = (
        operating_cash_flow_ttm_local is not None
        and capex_ttm_local is not None
        and not ttm_windows_match(ttm_results["operating_cash_flow"], ttm_results["capex"])
    )
    free_cash_flow_ttm_local = (
        operating_cash_flow_ttm_local - capex_ttm_local
        if operating_cash_flow_ttm_local is not None
        and capex_ttm_local is not None
        and not fcf_ttm_window_mismatch
        else None
    )

    reasons: list[str] = []
    quality_flags: list[str] = []
    if market_cap_proxy_method:
        quality_flags.append(market_cap_proxy_method)
    equity_issuance_proceeds_ttm_local, equity_proceeds_flag = sanitize_gross_proceeds_ttm(
        "equity_issuance_proceeds",
        equity_issuance_proceeds_ttm_local,
    )
    debt_issuance_proceeds_ttm_local, debt_proceeds_flag = sanitize_gross_proceeds_ttm(
        "debt_issuance_proceeds",
        debt_issuance_proceeds_ttm_local,
    )
    quality_flags.extend(
        flag for flag in (equity_proceeds_flag, debt_proceeds_flag) if flag
    )
    if fcf_ttm_window_mismatch:
        quality_flags.append("ttm_window_mismatch_free_cash_flow")
    if profile_taxonomy_filter is not None:
        quality_flags.append(f"profile_taxonomy_filter_{profile_taxonomy_filter}")
    if cost_of_sales_derived:
        quality_flags.append("cost_of_sales_derived_from_revenue_less_gross_profit")
    if operating_income_derived:
        quality_flags.append("operating_income_derived_from_revenue_less_costs_and_expenses")
    if quality_flags_pending_operating_income:
        quality_flags.append(quality_flags_pending_operating_income)
    if quality_flags_pending_synthetic:
        quality_flags.append(quality_flags_pending_synthetic)
    if zero_revenue_validation_failed:
        reasons.append("development_stage_zero_revenue_requires_negative_operating_cash_flow")
    if zero_revenue_validated:
        quality_flags.append("development_stage_zero_revenue_validated_by_negative_operating_cash_flow")
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
    previous_rpo_row = select_previous_comparable(
        currency_rows,
        "remaining_performance_obligation",
        selected.get("remaining_performance_obligation"),
        instant_metric=True,
    )
    orders_growth_current_row, orders_growth_previous_row = select_latest_comparable_pair(
        currency_rows,
        "orders",
        prefer_annual=True,
    )
    backlog_growth_current_row, backlog_growth_previous_row = select_latest_comparable_pair(
        currency_rows,
        "funded_backlog",
        instant_metric=True,
    )
    reported_backlog_growth_current_row, reported_backlog_growth_previous_row = (
        select_latest_comparable_pair(
            currency_rows,
            "reported_backlog",
            instant_metric=True,
        )
    )
    rpo_growth_current_row, rpo_growth_previous_row = select_latest_comparable_pair(
        currency_rows,
        "remaining_performance_obligation",
        instant_metric=True,
    )
    growth_pairs = (
        (
            "orders",
            selected.get("orders"),
            orders_growth_current_row,
        ),
        (
            "funded_backlog",
            selected.get("funded_backlog"),
            backlog_growth_current_row,
        ),
        (
            "reported_backlog",
            selected.get("reported_backlog"),
            reported_backlog_growth_current_row,
        ),
        (
            "remaining_performance_obligation",
            selected.get("remaining_performance_obligation"),
            rpo_growth_current_row,
        ),
    )
    invalid_growth_metrics = {
        metric_name
        for metric_name, selected_row, growth_row in growth_pairs
        if growth_row is not None and selected_row is None
    }
    if "orders" in invalid_growth_metrics:
        orders_growth_current_row = orders_growth_previous_row = None
    if "funded_backlog" in invalid_growth_metrics:
        backlog_growth_current_row = backlog_growth_previous_row = None
    if "reported_backlog" in invalid_growth_metrics:
        reported_backlog_growth_current_row = reported_backlog_growth_previous_row = None
    if "remaining_performance_obligation" in invalid_growth_metrics:
        rpo_growth_current_row = rpo_growth_previous_row = None
    period_flags.extend(
        f"growth_pair_without_current_source_{metric_name}"
        for metric_name in sorted(invalid_growth_metrics)
    )
    previous_assets = as_float(previous_assets_row.get("value")) if previous_assets_row is not None else None
    previous_equity = as_float(previous_equity_row.get("value")) if previous_equity_row is not None else None
    previous_cash = as_float(previous_cash_row.get("value")) if previous_cash_row is not None else None
    previous_inventory = as_float(previous_inventory_row.get("value")) if previous_inventory_row is not None else None
    previous_rpo = as_float(previous_rpo_row.get("value")) if previous_rpo_row is not None else None
    orders_growth_current = (
        as_float(orders_growth_current_row.get("value"))
        if orders_growth_current_row is not None
        else None
    )
    orders_growth_previous = (
        as_float(orders_growth_previous_row.get("value"))
        if orders_growth_previous_row is not None
        else None
    )
    backlog_growth_current = (
        as_float(backlog_growth_current_row.get("value"))
        if backlog_growth_current_row is not None
        else None
    )
    backlog_growth_previous = (
        as_float(backlog_growth_previous_row.get("value"))
        if backlog_growth_previous_row is not None
        else None
    )
    reported_backlog_growth_current = (
        as_float(reported_backlog_growth_current_row.get("value"))
        if reported_backlog_growth_current_row is not None
        else None
    )
    reported_backlog_growth_previous = (
        as_float(reported_backlog_growth_previous_row.get("value"))
        if reported_backlog_growth_previous_row is not None
        else None
    )
    rpo_growth_current = (
        as_float(rpo_growth_current_row.get("value"))
        if rpo_growth_current_row is not None
        else None
    )
    rpo_growth_previous = (
        as_float(rpo_growth_previous_row.get("value"))
        if rpo_growth_previous_row is not None
        else None
    )

    current_debt_rows = (
        [selected.get("debt_total")]
        if selected.get("debt_total") is not None
        else [selected.get("debt_current"), selected.get("debt_noncurrent")]
    )
    previous_debt_rows: list[dict[str, Any] | None] = [previous_debt_row]
    previous_debt = as_float(previous_debt_row.get("value")) if previous_debt_row is not None else None
    if model_family == "machinery" and previous_debt_row is None:
        current_component_rows: list[dict[str, Any]] = []
        for metric in ("debt_current", "debt_noncurrent"):
            component_row = selected.get(metric)
            if component_row is not None:
                current_component_rows.append(component_row)
        previous_component_rows = [
            select_previous_comparable(
                currency_rows,
                str(row.get("canonical_metric") or ""),
                row,
                instant_metric=True,
            )
            for row in current_component_rows
        ]
        if (
            len(current_component_rows) == 2
            and len(previous_component_rows) == 2
            and all(row is not None for row in previous_component_rows)
            and combined_periods_match(*current_component_rows, selected.get("debt_total"))
        ):
            previous_debt_rows = previous_component_rows
            previous_debt = sum(
                value
                for row in previous_component_rows
                if row is not None and (value := as_float(row.get("value"))) is not None
            )
            quality_flags.append("roic_previous_debt_from_components")
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
        *previous_debt_rows,
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
    # Common-instant retry (verified 21 cases): operands picked independently
    # can straddle accession instants even though a single filing carries a
    # fully matched balance-sheet set. Retry at recent common instants before
    # nulling; bounded to the staleness window of the balance anchor.
    ic_instant_end: date | None = None
    if invested_capital is None:
        balance_anchor_end = parse_date(
            ((selected.get("assets") or selected.get("equity") or {}).get("period_end"))
        )
        equity_ends = sorted(
            {
                str(row.get("period_end") or "")[:10]
                for row in currency_rows
                if str(row.get("canonical_metric") or "") == "equity"
                and as_float(row.get("value")) is not None
            },
            reverse=True,
        )
        for iso_end in equity_ends[:4]:
            end_date_parsed = parse_date(iso_end)
            if end_date_parsed is None:
                continue
            if balance_anchor_end is not None and (balance_anchor_end - end_date_parsed).days > STALE_FACT_MAX_LAG_DAYS:
                break
            capital = capital_at_instant(currency_rows, iso_end)
            if capital is not None:
                invested_capital = capital
                ic_instant_end = end_date_parsed
                quality_flags.append("invested_capital_realigned_common_instant")
                break
    elif (selected_equity := selected.get("equity")) is not None:
        ic_instant_end = parse_date(selected_equity.get("period_end"))
    if invested_capital is not None and previous_invested_capital is None and ic_instant_end is not None:
        prior_candidates = sorted(
            {
                str(row.get("period_end") or "")[:10]
                for row in currency_rows
                if str(row.get("canonical_metric") or "") == "equity"
                and as_float(row.get("value")) is not None
                and (prior_end := parse_date(row.get("period_end"))) is not None
                and 345 <= (ic_instant_end - prior_end).days <= 405
            },
            reverse=True,
        )
        for iso_end in prior_candidates:
            capital = capital_at_instant(currency_rows, iso_end)
            if capital is not None:
                previous_invested_capital = capital
                quality_flags.append("previous_invested_capital_realigned_common_instant")
                break
    average_invested_capital = (
        (invested_capital + previous_invested_capital) / 2.0
        if invested_capital is not None and previous_invested_capital is not None
        else None
    )
    average_assets = (assets + previous_assets) / 2.0 if assets is not None and previous_assets is not None else None
    pretax_ttm = ttm_results["pretax_income"].value
    tax_ttm = ttm_results["income_tax_expense"].value
    if operating_income_ttm_local is None:
        # TTM analogue of the single-period derivation: when the issuer stopped
        # tagging the operating-income subtotal, derive its TTM from matching
        # revenue and total-costs TTM windows — with the same pretax-collapse
        # guard (a rollup that includes non-operating items yields pretax, not
        # an operating subtotal).
        revenue_ttm_result = ttm_results["revenue"]
        costs_ttm_result = ttm_results["costs_and_expenses"]
        if (
            revenue_ttm_result.value is not None
            and costs_ttm_result.value is not None
            and ttm_windows_match(revenue_ttm_result, costs_ttm_result)
        ):
            derived_ttm = revenue_ttm_result.value - costs_ttm_result.value
            pretax_ttm_comparable = pretax_ttm is not None and ttm_windows_match(
                revenue_ttm_result, ttm_results["pretax_income"]
            )
            ttm_collapse_tolerance = max(abs(pretax_ttm or 0.0) * 0.02, abs(revenue_ttm_result.value) * 0.001)
            if pretax_ttm is not None and not pretax_ttm_comparable:
                # A different-window pretax TTM cannot arbitrate the collapse
                # test — neither accept nor reject on a coincidental match.
                quality_flags.append("operating_income_ttm_derivation_skipped_collapse_guard_unavailable")
            elif pretax_ttm is not None and abs(derived_ttm - pretax_ttm) <= ttm_collapse_tolerance:
                quality_flags.append("operating_income_ttm_derivation_rejected_costs_include_nonoperating")
            else:
                operating_income_ttm_local = derived_ttm
                ttm_results["operating_income"] = TtmResult(
                    operating_income_ttm_local,
                    "",
                    window_start=revenue_ttm_result.window_start,
                    window_end=revenue_ttm_result.window_end,
                )
                quality_flags.append("operating_income_ttm_derived_from_revenue_less_costs_and_expenses")
        if operating_income_ttm_local is None:
            # Second fallback: synthetic TTM operating subtotal from matching
            # component TTM windows (revenue - COGS - SG&A [- R&D]). Flagged;
            # can overstate for filers with material other operating charges.
            cogs_ttm_result = ttm_results["cost_of_sales"]
            sga_ttm_result = ttm_results["selling_general_admin"]
            rnd_ttm_result = ttm_results["research_and_development"]
            if (
                revenue_ttm_result.value is not None
                and cogs_ttm_result.value is not None
                and sga_ttm_result.value is not None
                and ttm_windows_match(revenue_ttm_result, cogs_ttm_result)
                and ttm_windows_match(revenue_ttm_result, sga_ttm_result)
            ):
                synthetic_ttm = revenue_ttm_result.value - cogs_ttm_result.value - sga_ttm_result.value
                if rnd_ttm_result.value is not None and ttm_windows_match(revenue_ttm_result, rnd_ttm_result):
                    synthetic_ttm -= rnd_ttm_result.value
                    quality_flags.append("operating_income_ttm_synthetic_gross_profit_less_sga_r_and_d")
                else:
                    quality_flags.append("operating_income_ttm_synthetic_gross_profit_less_sga_excl_r_and_d")
                operating_income_ttm_local = synthetic_ttm
                ttm_results["operating_income"] = TtmResult(
                    synthetic_ttm,
                    "",
                    window_start=revenue_ttm_result.window_start,
                    window_end=revenue_ttm_result.window_end,
                )
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
    roic_not_meaningful_flag = (
        int(average_invested_capital <= 0)
        if average_invested_capital is not None
        else None
    )
    asset_turnover = (
        safe_div(revenue_ttm_local, average_assets)
        if average_assets is not None and average_assets > 0
        else None
    )
    if asset_turnover is None and assets is not None and assets > 0 and is_recent_public_transition(
        company,
        asof=asof,
    ):
        if revenue_ttm_local is not None and revenue_ttm_local > 0:
            asset_turnover = safe_div(revenue_ttm_local, assets)
            quality_flags.append("asset_turnover_proxy_ttm_revenue_over_ending_assets")
        elif revenue_stub_annualized is not None and revenue_stub_annualized > 0:
            asset_turnover = safe_div(revenue_stub_annualized, assets)
            quality_flags.append(
                "asset_turnover_proxy_annualized_stub_revenue_over_ending_assets"
            )
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

    inventory_growth, _, inventory_sales_growth_spread = (
        select_aligned_inventory_revenue_growth(currency_rows)
    )
    if inventory_sales_growth_spread is not None:
        quality_flags.append("inventory_sales_growth_spread_aligned_historical_periods")
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
    gross_profit_ttm_usd = usd_ttm(
        gross_profit_ttm_local,
        ttm_results["gross_profit"],
    )
    operating_income_ttm_usd = usd_ttm(operating_income_ttm_local, ttm_results["operating_income"])
    revenue_ttm_usd = usd_ttm(revenue_ttm_local, ttm_results["revenue"])
    operating_cash_flow_ttm_usd = usd_ttm(operating_cash_flow_ttm_local, ttm_results["operating_cash_flow"])
    capex_ttm_usd = usd_ttm(capex_ttm_local, ttm_results["capex"])
    gross_profit_for_valuation_usd = gross_profit_ttm_usd
    if gross_profit_for_valuation_usd is None and gross_profit_usd is not None:
        gross_profit_for_valuation_usd = gross_profit_usd
        quality_flags.append("valuation_gross_profit_fallback_latest_period")
    operating_income_for_valuation_usd = operating_income_ttm_usd
    if (
        operating_income_for_valuation_usd is None
        and operating_income_usd is not None
    ):
        operating_income_for_valuation_usd = operating_income_usd
        quality_flags.append("valuation_operating_income_fallback_latest_period")
    # FN-4 for TTM composites: only combine OI and D&A resolved over the same
    # TTM window (mirrors book_to_bill's existing gate).
    if (
        operating_income_ttm_usd is not None
        and depreciation_and_amortization_ttm_usd is not None
        and not ttm_windows_match(
            ttm_results["operating_income"], ttm_results["depreciation_and_amortization"]
        )
    ):
        ebitda_ttm_usd = None
        quality_flags.append("ttm_window_mismatch_ebitda")
    else:
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
    negative_ebitda_leverage_flag = (
        int(ebitda_ttm_usd <= 0)
        if ebitda_ttm_usd is not None
        else None
    )
    if (
        operating_income_ttm_usd is not None
        and interest_expense_ttm_usd is not None
        and not ttm_windows_match(ttm_results["operating_income"], ttm_results["interest_expense"])
    ):
        interest_coverage = None
        quality_flags.append("ttm_window_mismatch_interest_coverage")
    else:
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
    # burn == 0 (cash-generative) means the company is not dependent on
    # raises regardless of gross proceeds (e.g. routine bond refinancing):
    # dependence is an explicit 0.0, not missing.
    if cash_burn_ttm_usd is not None and cash_burn_ttm_usd <= 0:
        capital_raise_dependence = 0.0
    elif (
        capital_raise_components
        and gross_capital_raised_ttm_usd is not None
        and cash_burn_ttm_usd is not None
    ):
        # Partial proceeds are a lower bound. Preserve the ratio for audit and
        # one-sided scoring: it may establish high dependence, but incomplete
        # evidence must never earn low-dependence credit.
        capital_raise_dependence = gross_capital_raised_ttm_usd / cash_burn_ttm_usd
    else:
        capital_raise_dependence = None
    diluted_share_current_row, diluted_share_previous_row = select_latest_comparable_pair(
        currency_rows,
        "diluted_shares",
    )
    diluted_shares_yoy_growth, diluted_share_outlier = validated_diluted_share_growth(
        diluted_share_current_row,
        diluted_share_previous_row,
    )
    if diluted_share_outlier:
        quality_flags.append("diluted_shares_yoy_outlier_rejected")
    if diluted_shares_yoy_growth is None:
        basic_share_current_row, basic_share_previous_row = select_basic_share_pair_when_eps_equal(
            currency_rows
        )
        diluted_shares_yoy_growth, basic_share_outlier = validated_diluted_share_growth(
            basic_share_current_row,
            basic_share_previous_row,
        )
        if basic_share_outlier:
            quality_flags.append("basic_shares_yoy_outlier_rejected")
        if diluted_shares_yoy_growth is not None:
            quality_flags.append("diluted_shares_proxy_basic_when_eps_equal")
    if (
        diluted_shares_yoy_growth is None
        and str(company.get("development_stage") or "").strip().lower() != "operating"
    ):
        outstanding_current_row, outstanding_previous_row = select_latest_comparable_pair(
            currency_rows,
            "shares_outstanding",
            instant_metric=True,
        )
        diluted_shares_yoy_growth, outstanding_share_outlier = validated_diluted_share_growth(
            outstanding_current_row,
            outstanding_previous_row,
        )
        if outstanding_share_outlier:
            quality_flags.append("shares_outstanding_yoy_outlier_rejected")
        if diluted_shares_yoy_growth is not None:
            quality_flags.append("diluted_shares_yoy_proxy_outstanding_shares")
    orders_yoy_growth = growth(orders_growth_current, orders_growth_previous)
    backlog_yoy_growth = growth(backlog_growth_current, backlog_growth_previous)
    reported_backlog_yoy_growth = growth(
        reported_backlog_growth_current,
        reported_backlog_growth_previous,
    )
    rpo_yoy_growth = growth(rpo_growth_current, rpo_growth_previous)
    book_to_bill, book_to_bill_quality = calculate_book_to_bill(
        currency_rows,
        orders=ttm_results["orders"],
        revenue=ttm_results["revenue"],
    )
    if book_to_bill_quality:
        quality_flags.append(book_to_bill_quality)
    current_revenue_result = ttm_results["revenue"]
    backlog_row = selected.get("funded_backlog")
    backlog_period_end = (
        parse_date(backlog_row.get("period_end"))
        if backlog_row is not None
        else None
    )
    backlog_revenue, backlog_alignment = revenue_ttm_aligned_to_instant_metric(
        currency_rows,
        metric_period_end=backlog_period_end,
        current_revenue=current_revenue_result,
    )
    backlog_to_revenue = safe_div(funded_backlog, backlog_revenue.value)
    if funded_backlog is not None:
        if backlog_to_revenue is None:
            quality_flags.append(
                f"backlog_to_revenue_{backlog_alignment}"
                if backlog_alignment
                else "period_mismatch_backlog_to_revenue"
            )
        elif backlog_alignment:
            quality_flags.append(
                f"backlog_to_revenue_{backlog_alignment}"
            )

    reported_backlog_row = selected.get("reported_backlog")
    reported_backlog_period_end = (
        parse_date(reported_backlog_row.get("period_end"))
        if reported_backlog_row is not None
        else None
    )
    reported_backlog_revenue, reported_backlog_alignment = (
        revenue_ttm_aligned_to_instant_metric(
            currency_rows,
            metric_period_end=reported_backlog_period_end,
            current_revenue=current_revenue_result,
        )
    )
    reported_backlog_to_revenue = safe_div(
        reported_backlog,
        reported_backlog_revenue.value,
    )
    if reported_backlog is not None:
        if reported_backlog_to_revenue is None:
            quality_flags.append(
                "reported_backlog_to_revenue_"
                f"{reported_backlog_alignment}"
                if reported_backlog_alignment
                else "period_mismatch_reported_backlog_to_revenue"
            )
        elif reported_backlog_alignment:
            quality_flags.append(
                "reported_backlog_to_revenue_"
                f"{reported_backlog_alignment}"
            )

    rpo_row = selected.get("remaining_performance_obligation")
    rpo_period_end = (
        parse_date(rpo_row.get("period_end"))
        if rpo_row is not None
        else None
    )
    rpo_revenue, rpo_alignment = revenue_ttm_aligned_to_instant_metric(
        currency_rows,
        metric_period_end=rpo_period_end,
        current_revenue=current_revenue_result,
    )
    rpo_to_revenue = safe_div(rpo, rpo_revenue.value)
    if rpo is not None:
        if rpo_to_revenue is None:
            quality_flags.append(
                f"rpo_to_revenue_{rpo_alignment}"
                if rpo_alignment
                else "period_mismatch_rpo_to_revenue"
            )
        elif rpo_alignment:
            quality_flags.append(f"rpo_to_revenue_{rpo_alignment}")
    rpo_implied_orders = (
        rpo - previous_rpo + rpo_revenue.value
        if rpo is not None
        and previous_rpo is not None
        and rpo_revenue.value is not None
        else None
    )
    rpo_implied_book_to_bill = safe_div(
        rpo_implied_orders,
        rpo_revenue.value,
    )
    rpo_implied_orders_usd = usd_ttm(rpo_implied_orders, rpo_revenue)
    if rpo_implied_orders is not None:
        quality_flags.append(
            "rpo_implied_orders_proxy_unadjusted_for_fx_cancellations_"
            "and_contract_changes"
        )

    # Keep reported backlog and GAAP/IFRS RPO intact. This separate proxy gives
    # calibration one contract-load series without treating the two disclosure
    # labels as independent signals or mixing their histories in a growth rate.
    if reported_backlog is not None:
        contract_load_proxy = reported_backlog
        contract_load_proxy_usd = reported_backlog_usd
        contract_load_proxy_source = "reported_backlog"
        contract_load_proxy_yoy_growth = reported_backlog_yoy_growth
        contract_load_proxy_to_revenue = reported_backlog_to_revenue
    elif rpo is not None:
        contract_load_proxy = rpo
        contract_load_proxy_usd = rpo_usd
        contract_load_proxy_source = "remaining_performance_obligation"
        contract_load_proxy_yoy_growth = rpo_yoy_growth
        contract_load_proxy_to_revenue = rpo_to_revenue
    else:
        contract_load_proxy = None
        contract_load_proxy_usd = None
        contract_load_proxy_source = None
        contract_load_proxy_yoy_growth = None
        contract_load_proxy_to_revenue = None

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

    inventory_to_cogs: float | None = None
    receivables_to_revenue: float | None = None
    payables_to_cogs: float | None = None
    if model_family != "machinery":
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
    # FCF/NI is sign-unstable when net income <= 0: an FCF burner over losses
    # produces a positive "conversion" ratio while a cash generator over losses
    # goes negative. Not meaningful — null it, mirroring net_debt_to_ebitda's
    # negative-EBITDA treatment.
    if fcf_to_net_income is not None and net_income is not None and net_income <= 0:
        fcf_to_net_income = None
        quality_flags.append("fcf_to_net_income_not_meaningful_nonpositive_net_income")
    if selected.get("debt_total") is not None:
        debt_operand_rows: list[dict[str, Any] | None] = [selected.get("debt_total")]
    else:
        debt_operand_rows = [selected.get("debt_current"), selected.get("debt_noncurrent")]
    # FN-14: the unsuffixed net_cash column carries the LOCAL reported-currency
    # value; the USD conversion lives only in net_cash_usd. Both balance-sheet
    # operands share fx_rate_balance_sheet, so usd_balance(net_cash) equals
    # cash_usd - debt_usd whenever the rate is available.
    net_cash = cash - debt_total if cash is not None and debt_total is not None else None
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
    if model_family == "machinery":
        ccc_snapshot, cash_conversion_cycle_change, ccc_flags = build_machinery_ccc(currency_rows)
        quality_flags.extend(ccc_flags)
        inventory_days = ccc_snapshot.inventory_days if ccc_snapshot is not None else None
        days_sales_outstanding = (
            ccc_snapshot.days_sales_outstanding if ccc_snapshot is not None else None
        )
        days_payables_outstanding = (
            ccc_snapshot.days_payables_outstanding if ccc_snapshot is not None else None
        )
        cash_conversion_cycle = (
            ccc_snapshot.cash_conversion_cycle if ccc_snapshot is not None else None
        )
    else:
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
            "gross_profit_ttm_usd": gross_profit_ttm_usd,
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
            "operating_cash_flow_ttm": operating_cash_flow_ttm_local,
            "operating_cash_flow_ttm_usd": operating_cash_flow_ttm_usd,
            "capex_ttm": capex_ttm_local,
            "capex_ttm_usd": capex_ttm_usd,
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
            # EV multiples over a non-positive profit denominator are negative and
            # would rank loss-makers as the cheapest names under direction -1;
            # emit NULL instead (negative EV over positive profit stays: net cash
            # in excess of market cap is legitimately cheap).
            "ev_gross_profit": safe_div(
                enterprise_value,
                gross_profit_for_valuation_usd,
            )
            if gross_profit_for_valuation_usd is not None
            and gross_profit_for_valuation_usd > 0
            else None,
            "ev_operating_income": safe_div(
                enterprise_value,
                operating_income_for_valuation_usd,
            )
            if operating_income_for_valuation_usd is not None
            and operating_income_for_valuation_usd > 0
            else None,
            # Valuation unmeasurable BECAUSE of losses must not score neutral:
            # nulling the EV multiples for non-positive profits is correct
            # (negative multiples are meaningless ranks), but the component
            # falling back to 50 rewarded unmeasurability. This flag carries
            # the penalty into the valuation component instead (mirrors
            # negative_ebitda_leverage_flag in risk control).
            "negative_profit_valuation_flag": (
                int(
                    (
                        gross_profit_for_valuation_usd is not None
                        and gross_profit_for_valuation_usd <= 0
                    )
                    or (
                        operating_income_for_valuation_usd is not None
                        and operating_income_for_valuation_usd <= 0
                    )
                )
                if gross_profit_for_valuation_usd is not None
                or operating_income_for_valuation_usd is not None
                else None
            ),
            "market_cap": market_cap,
            "latest_price": latest_price,
            "deferred_revenue": deferred_revenue,
            "contract_liabilities": deferred_revenue,
            "remaining_performance_obligation": rpo,
            "remaining_performance_obligation_usd": rpo_usd,
            "rpo_current": rpo_current,
            "rpo_current_usd": rpo_current_usd,
            "book_to_bill": book_to_bill,
            "funded_backlog": funded_backlog,
            "funded_backlog_usd": funded_backlog_usd,
            "reported_backlog": reported_backlog,
            "reported_backlog_usd": reported_backlog_usd,
            "contract_load_proxy": contract_load_proxy,
            "contract_load_proxy_usd": contract_load_proxy_usd,
            "contract_load_proxy_source": contract_load_proxy_source,
            "orders_yoy_growth": orders_yoy_growth,
            "backlog_yoy_growth": backlog_yoy_growth,
            "backlog_to_revenue": backlog_to_revenue,
            "reported_backlog_yoy_growth": reported_backlog_yoy_growth,
            "reported_backlog_to_revenue": reported_backlog_to_revenue,
            "contract_load_proxy_yoy_growth": contract_load_proxy_yoy_growth,
            "contract_load_proxy_to_revenue": contract_load_proxy_to_revenue,
            "rpo_yoy_growth": rpo_yoy_growth,
            "rpo_to_revenue": rpo_to_revenue,
            "rpo_implied_orders": rpo_implied_orders,
            "rpo_implied_orders_usd": rpo_implied_orders_usd,
            "rpo_implied_book_to_bill": rpo_implied_book_to_bill,
            "invested_capital_usd": invested_capital_usd,
            "roic": roic,
            "roic_not_meaningful_flag": roic_not_meaningful_flag,
            "asset_turnover": asset_turnover,
            "incremental_operating_margin": incremental_operating_margin,
            "inventory_growth": inventory_growth,
            "inventory_sales_growth_spread": inventory_sales_growth_spread,
            "cash_conversion_cycle_change": cash_conversion_cycle_change,
            "ebitda_ttm_usd": ebitda_ttm_usd,
            "net_debt_to_ebitda": net_debt_to_ebitda,
            "negative_ebitda_leverage_flag": negative_ebitda_leverage_flag,
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


def metric_source_row(rows: list[dict[str, Any]], metric_name: str) -> dict[str, Any] | None:
    return select_fact(rows, metric_name, prefer_annual=metric_name in DURATION_METRICS)


def has_rpo_exemption_evidence(
    conn: Any,
    *,
    ticker: str,
    source_id: str,
    asof: date,
) -> bool:
    row = conn.execute(
        f"""
        SELECT 1
        FROM fact_sec_xbrl_fact_raw
        WHERE ticker = ?
          AND source_id = ?
          AND concept_name IN (
                'RPOPracticalExpedient',
                'RevenuePracticalExpedientRemainingPerformanceObligation'
          )
          AND COALESCE(raw_value, 1.0) <> 0.0
          AND ({ACCEPTED_DATE_SQL}) <= ?
        LIMIT 1
        """,
        (ticker, source_id, asof.isoformat()),
    ).fetchone()
    return row is not None


def has_sec_parser_failure(
    conn: Any,
    *,
    ticker: str,
    model_family: str,
    asof: date,
) -> bool:
    row = conn.execute(
        """
        SELECT 1
        FROM data_quality_issues
        WHERE ticker = ?
          AND model_family = ?
          AND resolution_status = 'open'
          AND COALESCE(NULLIF(SUBSTR(detected_at, 1, 10), ''), '9999-12-31') <= ?
          AND issue_type IN (
                'sec_sync_failed',
                'sec_endpoint_not_available',
                'sec_archive_xbrl_unavailable',
                'sec_archive_xbrl_no_filing_metadata'
          )
        LIMIT 1
        """,
        (ticker, model_family, asof.isoformat()),
    ).fetchone()
    return row is not None


DISCLOSURE_SOURCE_METRIC = {
    "orders": "orders",
    "orders_yoy_growth": "orders",
    "book_to_bill": "orders",
    "funded_backlog": "funded_backlog",
    "backlog_yoy_growth": "funded_backlog",
    "backlog_to_revenue": "funded_backlog",
    "reported_backlog": "reported_backlog",
    "reported_backlog_yoy_growth": "reported_backlog",
    "reported_backlog_to_revenue": "reported_backlog",
    "remaining_performance_obligation": "remaining_performance_obligation",
    "rpo_current": "remaining_performance_obligation",
    "rpo_yoy_growth": "remaining_performance_obligation",
    "rpo_to_revenue": "remaining_performance_obligation",
    "rpo_implied_orders": "remaining_performance_obligation",
    "rpo_implied_book_to_bill": "remaining_performance_obligation",
}


def unresolved_disclosure_candidate(
    conn: Any,
    *,
    ticker: str,
    model_family: str,
    metric_name: str,
    asof: date,
) -> dict[str, Any] | None:
    source_metric = DISCLOSURE_SOURCE_METRIC.get(metric_name)
    if not source_metric:
        return None
    row = conn.execute(
        """
        SELECT accession_number, document_name, candidate_status, status_reason,
               confidence, period_end
        FROM fact_sec_metric_disclosure_candidate
        WHERE ticker = ?
          AND model_family = ?
          AND metric_name = ?
          AND candidate_status IN ('ACCEPTED', 'REVIEW_REQUIRED')
          AND CASE
                WHEN COALESCE(accepted_at, '') GLOB '????-??-??*'
                    THEN SUBSTR(accepted_at, 1, 10)
                WHEN COALESCE(accepted_at, '') GLOB '[0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]*'
                    THEN SUBSTR(accepted_at, 1, 4) || '-' || SUBSTR(accepted_at, 5, 2) || '-' || SUBSTR(accepted_at, 7, 2)
                ELSE COALESCE(NULLIF(filing_date, ''), '9999-12-31')
              END <= ?
        ORDER BY candidate_status = 'ACCEPTED' DESC, confidence DESC,
                 period_end DESC, accession_number DESC
        LIMIT 1
        """,
        (ticker, model_family, source_metric, asof.isoformat()),
    ).fetchone()
    return dict(row) if row is not None else None


def extraction_method_for_row(row: dict[str, Any] | None, *, derived: bool, proxy: bool) -> str:
    if proxy:
        return "derived_proxy"
    if derived:
        return "derived_reported_operands"
    taxonomy = str((row or {}).get("taxonomy") or "")
    source_detail = str((row or {}).get("source_detail") or "")
    if source_detail == "sec_archive_prose_metric_mapped":
        return "filing_html_prose"
    if taxonomy == "issuer-ir":
        return "issuer_ir_document"
    if taxonomy == "sec-footnote":
        return "inline_xbrl_footnote"
    if taxonomy == "sec-text":
        return "filing_html_table"
    if taxonomy in {"us-gaap", "ifrs-full"}:
        return "standard_xbrl"
    return "reported_fact"


def availability_confidence(row: dict[str, Any] | None, *, derived: bool, proxy: bool) -> float:
    if proxy:
        return 0.5
    if derived:
        return 0.9
    taxonomy = str((row or {}).get("taxonomy") or "")
    source_detail = str((row or {}).get("source_detail") or "")
    if source_detail == "sec_archive_prose_metric_mapped":
        return 0.85
    if taxonomy == "issuer-ir":
        return 0.80
    if taxonomy == "sec-footnote":
        return 0.85
    if taxonomy == "sec-text":
        return 0.75
    return 0.95


def dynamic_metric_proxy_reason(
    metric_name: str,
    quality_tokens: set[str],
) -> str:
    if metric_name == "asset_turnover":
        prefixes = ("asset_turnover_proxy_",)
    elif metric_name == "diluted_shares_yoy_growth":
        prefixes = (
            "diluted_shares_proxy_",
            "diluted_shares_yoy_proxy_",
        )
    elif metric_name == "capital_raise_dependence":
        prefixes = ("capital_raise_proceeds_partial_component_coverage",)
    else:
        return ""
    return next(
        (
            token
            for token in quality_tokens
            if any(token.startswith(prefix) for prefix in prefixes)
        ),
        "",
    )


def classify_financial_metric_availability(
    conn: Any,
    *,
    feature: dict[str, Any],
    rows: list[dict[str, Any]],
    company: dict[str, Any],
    source_id: str,
    model_family: str,
    asof: date,
    availability_policy: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    if model_family not in AVAILABILITY_MODEL_FAMILIES:
        return []
    policy = availability_policy or {}
    funded_backlog_default_applicable = bool(
        policy.get("funded_backlog_default_applicable", False)
    )
    funded_backlog_applicable = {
        normalize_ticker(item) for item in (policy.get("funded_backlog_applicable_tickers") or [])
    }
    cancelable_order_tickers = {
        normalize_ticker(item) for item in (policy.get("cancelable_order_model_tickers") or [])
    }
    structural_no_backlog_tickers = {
        normalize_ticker(item) for item in (policy.get("structural_no_backlog_tickers") or [])
    }
    structural_policy_valid_from = parse_date(policy.get("structural_no_backlog_valid_from"))
    if structural_policy_valid_from and asof < structural_policy_valid_from:
        structural_no_backlog_tickers.clear()
    structural_no_inventory_tickers = {
        normalize_ticker(item) for item in (policy.get("structural_no_inventory_tickers") or [])
    }
    structural_no_inventory_valid_from = parse_date(
        policy.get("structural_no_inventory_valid_from")
    )
    if structural_no_inventory_valid_from and asof < structural_no_inventory_valid_from:
        structural_no_inventory_tickers.clear()
    recent_public_share_basis_transition_tickers = {
        normalize_ticker(item)
        for item in (policy.get("recent_public_share_basis_transition_tickers") or [])
    }
    recent_public_share_basis_valid_from = parse_date(
        policy.get("recent_public_share_basis_valid_from")
    )
    if recent_public_share_basis_valid_from and asof < recent_public_share_basis_valid_from:
        recent_public_share_basis_transition_tickers.clear()
    ticker = normalize_ticker(company.get("ticker"))
    exemption = has_rpo_exemption_evidence(conn, ticker=ticker, source_id=source_id, asof=asof)
    parser_failure = has_sec_parser_failure(
        conn,
        ticker=ticker,
        model_family=model_family,
        asof=asof,
    )
    development_stage = str(company.get("development_stage") or "").strip().lower()
    precommercial = development_stage != "operating" and as_float(feature.get("revenue")) in {None, 0.0}
    revenue_dependent = {
        "book_to_bill",
        "backlog_to_revenue",
        "reported_backlog_to_revenue",
        "rpo_to_revenue",
        "rpo_implied_orders",
        "rpo_implied_book_to_bill",
        "inventory_sales_growth_spread",
        "cash_conversion_cycle_change",
        "asset_turnover",
        "incremental_operating_margin",
        "contract_load_proxy_to_revenue",
    }
    output: list[dict[str, Any]] = []
    canonical_quality = str(feature.get("canonical_quality") or "")
    quality_tokens = {token for token in canonical_quality.split(";") if token}
    production_overrides: dict[str, dict[str, Any]] = {}
    override_table_exists = (
        conn.execute(
            """
            SELECT 1
            FROM sqlite_master
            WHERE type = 'table'
              AND name = 'sec_parser_production_metric_override'
            """
        ).fetchone()
        is not None
    )
    if override_table_exists:
        production_overrides = {
            str(row["metric_name"]): dict(row)
            for row in conn.execute(
                """
                SELECT metric_name, availability_status, status_reason
                FROM sec_parser_production_metric_override
                WHERE model_family = ? AND ticker = ? AND active = 1
                  AND valid_from <= ?
                  AND COALESCE(valid_to, '9999-12-31') >= ?
                ORDER BY valid_from DESC, evidence_key DESC
                """,
                (
                    model_family,
                    ticker,
                    asof.isoformat(),
                    asof.isoformat(),
                ),
            )
        }
    for metric_name, feature_field in REQUIRED_METRIC_FEATURES.items():
        value = as_float(feature.get(feature_field))
        source_metric = metric_name if metric_name in SOURCE_METRIC_FEATURES else ""
        if metric_name in CONTRACT_LOAD_PROXY_METRICS:
            source_metric = str(feature.get("contract_load_proxy_source") or "")
        source_row = metric_source_row(rows, source_metric) if source_metric else None
        dynamic_proxy_reason = dynamic_metric_proxy_reason(
            metric_name,
            quality_tokens,
        )
        proxy = metric_name in PROXY_METRIC_FEATURES or bool(dynamic_proxy_reason)
        derived = metric_name not in SOURCE_METRIC_FEATURES
        disclosure_candidate = unresolved_disclosure_candidate(
            conn,
            ticker=ticker,
            model_family=model_family,
            metric_name=metric_name,
            asof=asof,
        )
        disclosure_source_metric = DISCLOSURE_SOURCE_METRIC.get(metric_name, "")
        disclosure_source_row = (
            metric_source_row(rows, disclosure_source_metric) if disclosure_source_metric else None
        )
        unresolved_candidate = (
            disclosure_candidate
            if disclosure_candidate is not None
            and (
                str(disclosure_candidate.get("candidate_status") or "") != "ACCEPTED"
                or disclosure_source_row is None
            )
            else None
        )
        production_override = production_overrides.get(
            metric_name
        ) or production_overrides.get(
            DISCLOSURE_SOURCE_METRIC.get(metric_name, "")
        )
        if value is not None:
            status = "PROXY" if proxy else "REPORTED"
            if metric_name in CONTRACT_LOAD_PROXY_METRICS:
                reason = f"canonical_contract_load_proxy_from_{source_metric}"
            else:
                reason = (
                    dynamic_proxy_reason
                    or ("reported_value" if not derived else "derived_from_validated_reported_operands")
                )
        elif production_override is not None:
            status = str(
                production_override["availability_status"]
            )
            reason = (
                "reviewed_parser_structural_override:"
                f"{production_override['status_reason']}"
            )
        elif metric_name == "roic" and int(feature.get("roic_not_meaningful_flag") or 0) == 1:
            status = "NOT_APPLICABLE"
            reason = "average_invested_capital_nonpositive_roic_not_meaningful"
        elif metric_name == "net_debt_to_ebitda" and int(
            feature.get("negative_ebitda_leverage_flag") or 0
        ) == 1:
            status = "NOT_APPLICABLE"
            reason = "ebitda_nonpositive_leverage_multiple_not_meaningful"
        elif unresolved_candidate is not None:
            status = "DISCLOSED_UNPARSED"
            reason = (
                "sec_disclosure_candidate_not_projected:"
                f"{unresolved_candidate['candidate_status']}:"
                f"{unresolved_candidate['accession_number']}:"
                f"{unresolved_candidate['document_name']}"
            )
        elif metric_name in RPO_METRICS and exemption:
            status = "EXEMPT"
            reason = "issuer_applied_asc_606_rpo_practical_expedient"
        elif (
            metric_name == "inventory_sales_growth_spread"
            and ticker in structural_no_inventory_tickers
        ):
            status = "NOT_APPLICABLE"
            reason = "reviewed_nonphysical_business_model_no_manufacturing_inventory"
        elif (
            metric_name == "diluted_shares_yoy_growth"
            and ticker in recent_public_share_basis_transition_tickers
        ):
            status = "NOT_APPLICABLE"
            reason = "recent_public_share_basis_transition_no_same_basis_prior_year"
        elif precommercial and metric_name in revenue_dependent:
            status = "NOT_APPLICABLE"
            reason = "precommercial_or_zero_revenue_metric_not_applicable"
        elif metric_name in STRUCTURAL_CONTRACT_LOAD_METRICS and ticker in cancelable_order_tickers:
            # Cancelable order models (automotive-style truck orders) have no
            # binding backlog to disclose; the absence is structural, not a gap.
            status = "NOT_APPLICABLE"
            reason = "cancelable_order_model_no_binding_backlog_disclosure"
        elif metric_name in STRUCTURAL_CONTRACT_LOAD_METRICS and ticker in structural_no_backlog_tickers:
            # Short-cycle book-and-ship issuers with no (or ceased, via the
            # ASC 606 short-cycle expedient) backlog/RPO disclosure history.
            # Reported values always win over this branch, so historical asofs
            # where the issuer still disclosed are unaffected.
            status = "NOT_APPLICABLE"
            reason = "short_cycle_issuer_no_or_ceased_backlog_disclosure"
        elif (
            metric_name in FUNDED_BACKLOG_METRICS
            and not funded_backlog_default_applicable
            and ticker not in funded_backlog_applicable
        ):
            # Funded backlog is a government-contracting disclosure; commercial
            # issuers structurally never report it.
            status = "NOT_APPLICABLE"
            reason = "funded_backlog_specific_to_government_contract_issuers"
        elif (
            metric_name == "cash_runway_years"
            and (burn := as_float(feature.get("cash_burn_ttm_usd"))) is not None
            and burn <= 0.0
        ):
            status = "NOT_APPLICABLE"
            reason = "issuer_cash_generative_runway_not_meaningful"
        elif (
            metric_name == "capital_raise_dependence"
            and (burn := as_float(feature.get("cash_burn_ttm_usd"))) is not None
            and burn <= 0.0
        ):
            status = "NOT_APPLICABLE"
            reason = "issuer_cash_generative_external_capital_dependence_not_meaningful"
        elif (
            metric_name == "interest_coverage"
            and (debt := as_float(feature.get("total_debt_usd"))) is not None
            and debt == 0.0
            and as_float(feature.get("interest_expense_ttm_usd")) in {None, 0.0}
        ):
            status = "NOT_APPLICABLE"
            reason = "issuer_has_explicit_zero_debt_and_no_interest_expense"
        elif parser_failure:
            status = "PARSER_FAILURE"
            reason = "open_sec_ingestion_or_archive_failure"
        else:
            status = "NOT_DISCLOSED"
            operands = METRIC_OPERANDS.get(metric_name, ())
            reason = (
                "insufficient_comparable_history_or_missing_operands:" + ",".join(operands)
                if operands
                else "issuer_did_not_report_metric"
            )
        if status not in AVAILABILITY_STATUSES:
            raise ValueError(f"Unsupported availability status {status!r} for {ticker}:{metric_name}")
        extraction_method = (
            "filing_html_prose_candidate"
            if status == "DISCLOSED_UNPARSED"
            else extraction_method_for_row(source_row, derived=derived, proxy=proxy)
        )
        confidence = (
            float(unresolved_candidate["confidence"])
            if status == "DISCLOSED_UNPARSED" and unresolved_candidate is not None
            else availability_confidence(source_row, derived=derived, proxy=proxy)
            if value is not None
            else 0.0
        )
        provenance = {
            "operands": list(METRIC_OPERANDS.get(metric_name, ())),
            "feature_field": feature_field,
            "reporting_profile": feature.get("reporting_profile"),
            "data_quality_status": feature.get("data_quality_status"),
            "canonical_quality": canonical_quality,
            "disclosure_candidate": unresolved_candidate,
        }
        candidate_source = unresolved_candidate if status == "DISCLOSED_UNPARSED" else None
        output.append(
            {
                "ticker": ticker,
                "asof_date": asof.isoformat(),
                "model_family": model_family,
                "metric_name": metric_name,
                "availability_status": status,
                "metric_value": value,
                "unit": (source_row or {}).get("unit")
                or (
                    feature.get("reported_currency")
                    if metric_name == "contract_load_proxy"
                    else "ratio"
                    if derived
                    else feature.get("reported_currency")
                ),
                "source_id": source_id,
                "accession_number": (source_row or {}).get("accession_number")
                or (candidate_source or {}).get("accession_number"),
                "filing_date": (source_row or {}).get("filing_date"),
                "period_start": (source_row or {}).get("period_start"),
                "period_end": (source_row or {}).get("period_end")
                or (candidate_source or {}).get("period_end")
                or feature.get("fiscal_period_end"),
                "taxonomy": (source_row or {}).get("taxonomy"),
                "concept_name": (source_row or {}).get("concept_name"),
                "extraction_method": extraction_method,
                "confidence": confidence,
                "status_reason": reason,
                "provenance_json": json.dumps(provenance, sort_keys=True, separators=(",", ":")),
            }
        )
    return output


def apply_availability_summary(feature: dict[str, Any], rows: list[dict[str, Any]]) -> None:
    reported = sum(row["availability_status"] == "REPORTED" for row in rows)
    proxy = sum(row["availability_status"] == "PROXY" for row in rows)
    feature["financial_metric_reported_count"] = reported
    feature["financial_metric_proxy_count"] = proxy
    feature["financial_metric_unavailable_count"] = len(rows) - reported - proxy
    feature["financial_metric_classified_fraction"] = (
        len(rows) / len(REQUIRED_METRIC_FEATURES) if REQUIRED_METRIC_FEATURES else 1.0
    )


def upsert_metric_availability(conn: Any, rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    now = utc_now()
    columns = [*AVAILABILITY_REPORT_FIELDS, "created_at", "updated_at"]
    update_columns = [
        column
        for column in AVAILABILITY_REPORT_FIELDS
        if column not in {"ticker", "asof_date", "model_family", "metric_name"}
    ]
    placeholders_sql = ", ".join("?" for _ in columns)
    update_sql = ", ".join(f"{column} = excluded.{column}" for column in [*update_columns, "updated_at"])
    conn.executemany(
        f"""
        INSERT INTO feature_financial_metric_availability({", ".join(columns)})
        VALUES ({placeholders_sql})
        ON CONFLICT(ticker, asof_date, model_family, metric_name) DO UPDATE SET
            {update_sql}
        """,
        [tuple(row.get(column) for column in AVAILABILITY_REPORT_FIELDS) + (now, now) for row in rows],
    )


def write_report(path: Path, rows: list[dict[str, Any]]) -> None:
    # XC-10: atomic final-path write via the shared tmp + os.replace helper.
    path.parent.mkdir(parents=True, exist_ok=True)
    write_csv_atomic(path, REPORT_FIELDS, rows)


def resolve_model_family(
    requested_model_family: object,
    configured_model_family: object,
) -> str:
    requested = str(requested_model_family or "").strip()
    configured = str(configured_model_family or "").strip()
    if requested and configured and requested != configured:
        raise ValueError(
            f"--model-family={requested!r} conflicts with configured "
            f"industrials_universe.initial_subsector={configured!r}; "
            "use the matching sector config"
        )
    return requested or configured or "defense"


def main() -> None:
    configure_utc_logging()
    args = parse_args()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    base_dir = config_path.parent
    db_path = args.db.expanduser().resolve() if args.db else resolve_path(cfg_get(config, "paths.database_path"), base_dir=base_dir)
    configured_model_family = cfg_get(
        config,
        "industrials_universe.initial_subsector",
        "defense",
    )
    configured_families = cfg_get(config, "model_families", {}) or {}
    if (
        args.model_family
        and isinstance(configured_families, dict)
        and args.model_family in configured_families
    ):
        # A consolidated family-aware config is authoritative for an explicit
        # family request. The legacy initial_subsector remains the fallback
        # guard for older single-family configs.
        configured_model_family = args.model_family
    model_family = resolve_model_family(
        args.model_family,
        configured_model_family,
    )
    source_id = str(cfg_get(config, "sec_fundamentals.companyfacts_source_id", "sec_companyfacts") or "sec_companyfacts")
    family_supplemental_source_ids = cfg_get(
        config,
        (
            f"model_families.{model_family}.financial."
            "supplemental_disclosure_source_ids"
        ),
        None,
    )
    supplemental_disclosure_source_ids = tuple(
        parse_source_list(family_supplemental_source_ids)
        if family_supplemental_source_ids is not None
        else parse_source_list(
            cfg_get(
                config,
                "sec_fundamentals.supplemental_disclosure_source_ids",
                [],
            )
        )
        if model_family in AVAILABILITY_MODEL_FAMILIES
        else ()
    )
    market_source_id = str(cfg_get(config, "market_data_policy.scoring_primary_source", "yahoo_finance_adjusted") or "yahoo_finance_adjusted")
    market_fallback_source_ids = parse_source_list(cfg_get(config, "market_data_policy.scoring_fallback_sources", []))
    market_source_ids = source_priority_list(market_source_id, market_fallback_source_ids)
    availability_enabled = model_family in AVAILABILITY_MODEL_FAMILIES
    availability_policy_raw = cfg_get(config, "financial_validation.availability_policy", {}) or {}
    availability_policy = availability_policy_raw if isinstance(availability_policy_raw, dict) else {}
    archive_core_metric_recovery_tickers = {
        normalize_ticker(item)
        for item in (cfg_get(config, "sec_archive.core_metric_recovery_tickers", []) or [])
        if normalize_ticker(item)
    }
    archive_core_metric_recovery_metrics = frozenset(
        str(item).strip()
        for item in (cfg_get(config, "sec_archive.core_metric_recovery_metrics", []) or [])
        if str(item).strip()
    )
    output_csv = args.output_csv.expanduser().resolve() if args.output_csv else resolve_path(cfg_get(config, "sec_fundamentals.feature_output_csv"), base_dir=base_dir)
    availability_output_raw = cfg_get(config, "sec_fundamentals.metric_availability_output_csv", "")
    availability_output_csv = (
        args.availability_output_csv.expanduser().resolve()
        if args.availability_output_csv
        else resolve_path(availability_output_raw, base_dir=base_dir)
        if availability_output_raw
        else output_csv.with_name("financial_metric_availability.csv")
    )
    ticker_filter = parse_ticker_list(args.tickers)
    # MK-3: FX rates older than this many days before the evaluation date are
    # rejected instead of silently converting at arbitrarily old rates.
    fx_max_staleness_days = int(cfg_get(config, "fx_rates.max_staleness_days", 30) or 30)
    share_conversion_raw = (
        str(
            cfg_get(
                config,
                f"model_families.{model_family}.financial.share_conversion_overrides_csv",
                "",
            )
            or ""
        ).strip()
    )
    share_conversion_path = resolve_path(share_conversion_raw, base_dir=base_dir) if share_conversion_raw else None
    if share_conversion_path is not None and not share_conversion_path.is_file():
        raise FileNotFoundError(share_conversion_path)
    share_conversions = load_share_conversions(share_conversion_path)
    enable_statement_share_fallback = share_conversion_path is not None

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
            canonical_rows = refresh_canonical_facts(
                conn,
                source_id=source_id,
                model_family=model_family,
                tickers=tickers,
                asof=effective_asof,
                supplemental_source_ids=supplemental_disclosure_source_ids,
            )
            report_rows: list[dict[str, Any]] = []
            availability_report_rows: list[dict[str, Any]] = []
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
                    if availability_enabled:
                        conn.execute(
                            f"""
                            DELETE FROM feature_financial_metric_availability
                            WHERE asof_date = ?
                              AND model_family = ?
                              AND ticker NOT IN ({ph})
                            """,
                            (effective_asof.isoformat(), model_family, *tickers),
                        )
                if availability_enabled:
                    conn.execute(
                        f"""
                        DELETE FROM feature_financial_metric_availability
                        WHERE asof_date = ?
                          AND model_family = ?
                          AND ticker IN ({ph})
                        """,
                        (effective_asof.isoformat(), model_family, *tickers),
                    )
                if not args.suppress_data_quality_issues:
                    # A full current build owns this family's complete stage
                    # queue. Ticker-filtered repairs own only their tickers.
                    if ticker_filter:
                        conn.execute(
                            f"DELETE FROM data_quality_issues WHERE stage = ? AND model_family = ? AND ticker IN ({ph})",
                            (RUN_TYPE, model_family, *tickers),
                        )
                    else:
                        conn.execute(
                            "DELETE FROM data_quality_issues WHERE stage = ? AND model_family = ?",
                            (RUN_TYPE, model_family),
                        )
                for company in universe:
                    ticker = normalize_ticker(company.get("ticker"))
                    profile = load_profile(
                        conn,
                        ticker=ticker,
                        model_family=model_family,
                        company=company,
                        source_id=source_id,
                        asof=effective_asof,
                    )
                    rows = load_canonical_rows(conn, ticker=ticker, source_id=source_id, model_family=model_family, asof=effective_asof)
                    if str(profile.get("reporting_profile") or "").strip().upper() == DESPAC_BRIDGE_PROFILE:
                        rows.extend(
                            load_certified_predecessor_rows(
                                conn,
                                ticker=ticker,
                                source_id=source_id,
                                asof=effective_asof,
                            )
                        )
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
                            share_conversions=share_conversions,
                            enable_statement_share_fallback=enable_statement_share_fallback,
                            machinery_sec_text_core_metrics=(
                                archive_core_metric_recovery_metrics
                                if ticker in archive_core_metric_recovery_tickers
                                else frozenset()
                            ),
                        )
                    # Families with a dated reporting-profile contract use the
                    # exact snapshot selected above. Other legacy families still
                    # consume the latest-state profile dimension.
                    if model_family not in {"machinery", "transportation"}:
                        profile_updated = parse_date(profile.get("updated_at"))
                        if profile_updated is not None and profile_updated > effective_asof:
                            existing_quality = str(feature.get("canonical_quality") or "")
                            provenance_flag = "reporting_profile_provenance_post_asof"
                            feature["canonical_quality"] = (
                                f"{existing_quality};{provenance_flag}" if existing_quality else provenance_flag
                            )
                    metric_availability: list[dict[str, Any]] = []
                    if availability_enabled:
                        metric_availability = classify_financial_metric_availability(
                            conn,
                            feature=feature,
                            rows=rows,
                            company=company,
                            source_id=source_id,
                            model_family=model_family,
                            asof=effective_asof,
                            availability_policy=availability_policy,
                        )
                        apply_availability_summary(feature, metric_availability)
                    upsert_feature(conn, feature)
                    if availability_enabled:
                        upsert_metric_availability(conn, metric_availability)
                        availability_report_rows.extend(metric_availability)
                    if (
                        not args.suppress_data_quality_issues
                        and str(feature.get("data_quality_status") or "") != "complete"
                    ):
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
                            "financial_fallback_status": feature.get("financial_fallback_status", ""),
                            "canonical_quality": feature.get("canonical_quality", ""),
                            "fx_conversion_status": feature.get("fx_conversion_status", ""),
                            "revenue_usd": feature.get("revenue_usd", ""),
                            "operating_cash_flow_usd": feature.get("operating_cash_flow_usd", ""),
                            "operating_cash_flow_ttm_usd": feature.get("operating_cash_flow_ttm_usd", ""),
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
            if model_family == "machinery":
                availability_output_csv.parent.mkdir(parents=True, exist_ok=True)
                write_csv_atomic(
                    availability_output_csv,
                    AVAILABILITY_REPORT_FIELDS,
                    availability_report_rows,
                )
            review_count = sum(1 for row in report_rows if row["status"] != "success")
            finish_run(conn, run_id=run_id, status="success", row_count=len(report_rows), message=f"asof={effective_asof.isoformat()} rows={len(report_rows)} review={review_count} canonical_rows={canonical_rows} availability_rows={len(availability_report_rows)} output={output_csv}")
            LOGGER.info("Wrote financial feature coverage report: %s", output_csv)
            LOGGER.info("Built financial features: asof=%s rows=%d review=%d canonical_rows=%d", effective_asof, len(report_rows), review_count, canonical_rows)
        except BaseException as exc:
            finish_run(conn, run_id=run_id, status="failed", row_count=0, message=f"{type(exc).__name__}: {exc}")
            raise


if __name__ == "__main__":
    main()
