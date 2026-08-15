#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import logging
import math
import sys
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from med_devices.core.config import DEFAULT_NEUTRAL_SCORE, cfg_get, load_yaml, resolve_path  # noqa: E402
from med_devices.core.db import connect, finish_run, init_db, start_run, utc_now  # noqa: E402
from med_devices.core.logging_utils import configure_utc_logging  # noqa: E402
from med_devices.core.market_policy import scoring_market_sources, select_latest_rows_by_source_priority  # noqa: E402
from med_devices.core.text_norm import normalize_ticker  # noqa: E402


LOGGER = logging.getLogger("build_med_device_financial_features")
DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"

FLOW_METRICS = [
    "revenue",
    "gross_profit",
    "operating_income",
    "net_income",
    "operating_cash_flow",
    "capital_expenditures",
    "free_cash_flow",
    "research_and_development",
    "interest_expense",
]
BALANCE_METRICS = ["cash_and_investments", "total_debt", "total_assets", "stockholders_equity", "shares_outstanding"]
CORE_HISTORY_METRICS = [
    "revenue",
    "operating_income",
    "operating_cash_flow",
    "cash_and_investments",
    "shares_outstanding",
]
FEATURE_FIELDS = [
    "asof_date",
    "company_id",
    "ticker",
    "company_name",
    "subsector",
    "market_source_id",
    "latest_price_date",
    "latest_close",
    "price_staleness_days",
    "revenue_ttm",
    "gross_profit_ttm",
    "operating_income_ttm",
    "net_income_ttm",
    "operating_cash_flow_ttm",
    "capital_expenditures_ttm",
    "free_cash_flow_ttm",
    "research_and_development_ttm",
    "annualized_research_and_development",
    "interest_expense_ttm",
    "revenue_yoy_growth",
    "rd_growth_yoy",
    "gross_margin_ttm",
    "operating_margin_ttm",
    "net_margin_ttm",
    "fcf_margin_ttm",
    "rd_to_revenue_ttm",
    "rule_of_40",
    "cash_and_investments",
    "total_liquidity",
    "latest_quarter_operating_cash_burn",
    "annualized_operating_cash_burn",
    "financial_runway_years",
    "total_debt",
    "total_assets",
    "stockholders_equity",
    "net_debt",
    "shares_outstanding",
    "current_shares_outstanding",
    "diluted_weighted_average_shares",
    "basic_weighted_average_shares",
    "shares_source_concept",
    "shares_source_form",
    "shares_source_period",
    "market_cap_validated_flag",
    "shares_yoy_growth",
    "market_cap",
    "enterprise_value",
    "price_to_sales",
    "ev_to_sales",
    "growth_to_ev_sales",
    "fcf_yield",
    "net_debt_to_revenue",
    "return_on_assets",
    "return_on_equity",
    "interest_coverage",
    "accrual_ratio",
    "gross_margin_trend_3y",
    "quarterly_revenue_surprise_yoy",
    "financial_history_years",
    "min_core_group_years",
    "data_confidence_score",
    "calibration_bucket",
    "ttm_method",
    "data_quality_status",
    "missing_fields",
    "fundamental_quality_score_v1",
    "valuation_score_v1",
    "value_trap_score",
]
DEFAULT_NEUTRAL_COMPONENT_SCORE = DEFAULT_NEUTRAL_SCORE
CURRENT_SHARE_CONCEPTS = {
    "EntityCommonStockSharesOutstanding",
    "NumberOfSharesIssued",
}
DILUTED_WEIGHTED_SHARE_CONCEPTS = {
    "WeightedAverageNumberOfDilutedSharesOutstanding",
    "WeightedAverageNumberOfSharesOutstandingDiluted",
}
BASIC_WEIGHTED_SHARE_CONCEPTS = {
    "WeightedAverageNumberOfBasicSharesOutstanding",
    "WeightedAverageShares",
}
DEFAULT_FUNDAMENTAL_COMPONENT_WEIGHTS = {
    "gross_margin": 0.13,
    "operating_margin": 0.13,
    "fcf_margin": 0.16,
    "revenue_growth": 0.14,
    "balance_sheet": 0.10,
    "rd_intensity": 0.08,
    "history_confidence": 0.08,
    "accrual_quality": 0.10,
    "dilution_control": 0.08,
}
DEFAULT_VALUATION_COMPONENT_WEIGHTS = {
    "ev_to_sales": 0.35,
    "price_to_sales": 0.20,
    "fcf_yield": 0.25,
    "growth_to_ev_sales": 0.10,
    "history_confidence": 0.10,
}


@dataclass(frozen=True)
class Company:
    company_id: int
    ticker: str
    company_name: str
    subsector: str


@dataclass(frozen=True)
class FinancialRow:
    company_id: int
    accession_nodash: str
    period_end: str
    fiscal_year: int | None
    fiscal_period: str
    form: str
    filed_date: str
    values: dict[str, float | None]
    payload: dict[str, Any]


@dataclass(frozen=True)
class ShareSelection:
    value: float | None
    concept: str
    form: str
    period_end: str
    current_shares: float | None = None
    diluted_weighted_average_shares: float | None = None
    basic_weighted_average_shares: float | None = None


@dataclass(frozen=True)
class MarketShareSnapshot:
    ticker: str
    asof_date: str
    source_id: str
    shares_outstanding: float
    market_cap: float | None
    currency: str


@dataclass(frozen=True)
class ShareCountOverride:
    ticker: str
    current_shares_outstanding: float
    asof_date: str
    source: str
    note: str


@dataclass(frozen=True)
class FinancialFeaturePolicy:
    market_sources: list[str]
    share_count_sources: list[str]
    share_count_max_staleness_days: int
    allow_sec_weighted_average_share_fallback: bool
    max_staleness_days: int
    require_adjusted: bool
    core_min_years: float
    core_min_group_years: float
    short_min_years: float
    neutral_component_score: float
    fundamental_weights: dict[str, float]
    valuation_weights: dict[str, float]
    subsector_blend_weight: float
    winsor_low_pct: float
    winsor_high_pct: float
    ttm_sanity_min_annual_ratio: float
    ttm_sanity_max_annual_ratio: float


@dataclass
class FeatureRow:
    asof_date: str
    company_id: int
    ticker: str
    company_name: str
    subsector: str
    market_source_id: str = ""
    latest_price_date: str = ""
    latest_close: float | None = None
    price_staleness_days: int | None = None
    revenue_ttm: float | None = None
    gross_profit_ttm: float | None = None
    operating_income_ttm: float | None = None
    net_income_ttm: float | None = None
    operating_cash_flow_ttm: float | None = None
    capital_expenditures_ttm: float | None = None
    free_cash_flow_ttm: float | None = None
    research_and_development_ttm: float | None = None
    annualized_research_and_development: float | None = None
    interest_expense_ttm: float | None = None
    revenue_yoy_growth: float | None = None
    rd_growth_yoy: float | None = None
    gross_margin_ttm: float | None = None
    operating_margin_ttm: float | None = None
    net_margin_ttm: float | None = None
    fcf_margin_ttm: float | None = None
    rd_to_revenue_ttm: float | None = None
    rule_of_40: float | None = None
    cash_and_investments: float | None = None
    total_liquidity: float | None = None
    latest_quarter_operating_cash_burn: float | None = None
    annualized_operating_cash_burn: float | None = None
    financial_runway_years: float | None = None
    total_debt: float | None = None
    total_assets: float | None = None
    stockholders_equity: float | None = None
    net_debt: float | None = None
    shares_outstanding: float | None = None
    current_shares_outstanding: float | None = None
    diluted_weighted_average_shares: float | None = None
    basic_weighted_average_shares: float | None = None
    shares_source_concept: str = ""
    shares_source_form: str = ""
    shares_source_period: str = ""
    market_cap_validated_flag: int = 0
    shares_yoy_growth: float | None = None
    market_cap: float | None = None
    enterprise_value: float | None = None
    price_to_sales: float | None = None
    ev_to_sales: float | None = None
    fcf_yield: float | None = None
    net_debt_to_revenue: float | None = None
    return_on_assets: float | None = None
    return_on_equity: float | None = None
    interest_coverage: float | None = None
    accrual_ratio: float | None = None
    gross_margin_trend_3y: float | None = None
    quarterly_revenue_surprise_yoy: float | None = None
    growth_to_ev_sales: float | None = None
    financial_history_years: float = 0.0
    min_core_group_years: float = 0.0
    data_confidence_score: float = 0.0
    calibration_bucket: str = "new_issue_watchlist"
    ttm_method: str = "unavailable"
    data_quality_status: str = "fail"
    missing_fields: list[str] = field(default_factory=list)
    fundamental_quality_score_v1: float | None = None
    valuation_score_v1: float | None = None
    value_trap_score: float | None = None
    payload: dict[str, Any] = field(default_factory=dict)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build med-device financial and valuation feature rows.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--output-csv", type=Path, default=None)
    parser.add_argument("--share-count-overrides-csv", type=Path, default=None)
    parser.add_argument("--asof", type=str, default="", help="Feature as-of date, YYYY-MM-DD. Defaults to latest scoring bar.")
    parser.add_argument("--tickers", type=str, default="", help="Optional comma-separated ticker subset.")
    parser.add_argument("--max-tickers", type=int, default=0)
    parser.add_argument("--include-historical-members", action="store_true")
    return parser.parse_args()


def parse_date(raw: object) -> date | None:
    text = str(raw or "").strip()[:10]
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
    return value if math.isfinite(value) else None


def safe_json_loads(raw: object) -> dict[str, Any]:
    if raw is None:
        return {}
    try:
        value = json.loads(str(raw))
    except (TypeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def row_get(row: dict[str, Any], *names: str) -> str:
    normalized = {str(key or "").strip().lower(): value for key, value in row.items()}
    for name in names:
        value = normalized.get(name.strip().lower())
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def safe_div(numerator: float | None, denominator: float | None) -> float | None:
    if numerator is None or denominator is None or abs(denominator) < 1e-12:
        return None
    value = numerator / denominator
    return value if math.isfinite(value) else None


def clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    if not math.isfinite(value):
        return low
    return max(low, min(high, value))


def year_span(first_date: str, latest_date: str) -> float:
    first = parse_date(first_date)
    latest = parse_date(latest_date)
    if first is None or latest is None or latest < first:
        return 0.0
    return round((latest - first).days / 365.25, 2)


def quarter_number(fiscal_period: str) -> int | None:
    text = fiscal_period.strip().upper()
    if text in {"Q1", "Q2", "Q3", "Q4"}:
        return int(text[1])
    return None


def as_bool_config(raw: object, *, default: bool) -> bool:
    if raw is None:
        return default
    text = str(raw).strip().lower()
    if text in {"1", "true", "yes", "y", "on"}:
        return True
    if text in {"0", "false", "no", "n", "off"}:
        return False
    return default


def cfg_float(config: dict[str, Any], dotted_key: str, default: float) -> float:
    value = to_float(cfg_get(config, dotted_key, default))
    if value is None:
        raise ValueError(f"Config value must be numeric: {dotted_key}")
    return value


def cfg_weight_map(config: dict[str, Any], dotted_key: str, default: dict[str, float]) -> dict[str, float]:
    raw = cfg_get(config, dotted_key, None)
    if raw is None:
        out = dict(default)
    elif isinstance(raw, dict):
        unknown = sorted(set(str(key) for key in raw) - set(default))
        if unknown:
            raise ValueError(f"Unknown score weight key(s) in {dotted_key}: {', '.join(unknown)}")
        out = dict(default)
        for key in default:
            if key not in raw:
                continue
            value = to_float(raw.get(key))
            if value is None:
                raise ValueError(f"Score weight must be numeric: {dotted_key}.{key}")
            out[key] = value
    else:
        raise ValueError(f"Config value must be a mapping: {dotted_key}")

    if any(value < 0 for value in out.values()):
        raise ValueError(f"Score weights must be non-negative: {dotted_key}")
    total = sum(out.values())
    if abs(total - 1.0) > 0.0001:
        raise ValueError(f"Score weights must sum to 1.0: {dotted_key} sum={total:.6f}")
    return out


def financial_feature_policy(config: dict[str, Any]) -> FinancialFeaturePolicy:
    share_count_sources_raw = cfg_get(config, "financial_features.share_count_sources", ["ib_market_data", "yahoo_finance_backup"])
    if isinstance(share_count_sources_raw, list):
        share_count_sources = [str(item or "").strip() for item in share_count_sources_raw if str(item or "").strip()]
    else:
        share_count_sources = [
            str(item or "").strip()
            for item in str(share_count_sources_raw or "").split(",")
            if str(item or "").strip()
        ]
    return FinancialFeaturePolicy(
        market_sources=scoring_market_sources(config),
        share_count_sources=share_count_sources or ["ib_market_data", "yahoo_finance_backup"],
        share_count_max_staleness_days=int(cfg_get(config, "financial_features.share_count_max_staleness_days", 14)),
        allow_sec_weighted_average_share_fallback=as_bool_config(
            cfg_get(config, "financial_features.allow_sec_weighted_average_share_fallback", False),
            default=False,
        ),
        max_staleness_days=int(
            cfg_get(
                config,
                "financial_features.market_max_staleness_days",
                cfg_get(config, "market_data_policy.max_staleness_days", 7),
            )
        ),
        require_adjusted=as_bool_config(
            cfg_get(
                config,
                "financial_features.require_adjusted_prices",
                cfg_get(config, "market_data_policy.require_adjusted_for_scoring", True),
            ),
            default=True,
        ),
        core_min_years=cfg_float(
            config,
            "financial_features.core_calibration_min_fact_years",
            cfg_get(config, "universe_validation.calibration_core_min_fact_years", 7.0),
        ),
        core_min_group_years=cfg_float(
            config,
            "financial_features.core_calibration_min_core_group_years",
            cfg_get(config, "universe_validation.calibration_core_min_core_group_years", 5.0),
        ),
        short_min_years=cfg_float(
            config,
            "financial_features.short_history_min_fact_years",
            cfg_get(config, "universe_validation.calibration_short_history_min_fact_years", 3.0),
        ),
        neutral_component_score=cfg_float(
            config,
            "financial_features.neutral_component_score",
            DEFAULT_NEUTRAL_COMPONENT_SCORE,
        ),
        fundamental_weights=cfg_weight_map(
            config,
            "financial_features.fundamental_component_weights",
            DEFAULT_FUNDAMENTAL_COMPONENT_WEIGHTS,
        ),
        valuation_weights=cfg_weight_map(
            config,
            "financial_features.valuation_component_weights",
            DEFAULT_VALUATION_COMPONENT_WEIGHTS,
        ),
        subsector_blend_weight=cfg_float(config, "financial_features.subsector_percentile_blend_weight", 0.60),
        winsor_low_pct=cfg_float(config, "financial_features.winsor_low_pct", 0.10),
        winsor_high_pct=cfg_float(config, "financial_features.winsor_high_pct", 0.90),
        ttm_sanity_min_annual_ratio=cfg_float(config, "financial_features.ttm_sanity_min_annual_ratio", 0.20),
        ttm_sanity_max_annual_ratio=cfg_float(config, "financial_features.ttm_sanity_max_annual_ratio", 3.00),
    )


def load_companies(
    conn: Any,
    *,
    asof: date,
    ticker_filter: set[str],
    max_tickers: int,
    include_historical_members: bool,
) -> list[Company]:
    rows = conn.execute(
        """
        SELECT company_id, ticker, company_name, subsector
        FROM dim_company c
        WHERE (c.is_active = 1 AND EXISTS (
                SELECT 1
                FROM dim_company_model_taxonomy t
                WHERE t.company_id = c.company_id
                  AND t.model_family = 'med_devices'
           ))
           OR (? = 1 AND EXISTS (
                SELECT 1
                FROM dim_universe_membership m
                WHERE m.company_id = c.company_id
                  AND m.model_family = 'med_devices'
                  AND m.point_in_time_flag = 1
                  AND m.start_date <= ?
                  AND (m.end_date IS NULL OR m.end_date >= ?)
           ))
        ORDER BY ticker
        """,
        (1 if include_historical_members else 0, asof.isoformat(), asof.isoformat()),
    ).fetchall()
    out: list[Company] = []
    for row in rows:
        ticker = normalize_ticker(row["ticker"])
        if ticker_filter and ticker not in ticker_filter:
            continue
        out.append(
            Company(
                company_id=int(row["company_id"]),
                ticker=ticker,
                company_name=str(row["company_name"] or ""),
                subsector=str(row["subsector"] or ""),
            )
        )
        if max_tickers > 0 and len(out) >= max_tickers:
            break
    return out


def latest_market_asof(conn: Any, sources: list[str], *, require_adjusted: bool) -> str:
    if not sources:
        raise ValueError("No configured market sources")
    placeholders = ",".join("?" for _ in sources)
    adjusted_clause = "AND is_adjusted = 1" if require_adjusted else ""
    row = conn.execute(
        f"""
        SELECT MAX(bar_date) AS max_bar_date
        FROM fact_price_ohlcv
        WHERE source_id IN ({placeholders})
          {adjusted_clause}
        """,
        sources,
    ).fetchone()
    asof = str(row["max_bar_date"] or "") if row is not None else ""
    if not asof:
        raise ValueError("No market bars found for configured scoring sources")
    return asof


def load_price_selection(
    conn: Any,
    companies: list[Company],
    *,
    asof: date,
    sources: list[str],
    max_staleness_days: int,
    require_adjusted: bool,
) -> dict[str, dict[str, Any]]:
    tickers = [company.ticker for company in companies]
    if not tickers:
        return {}
    ticker_clause = ",".join("?" for _ in tickers)
    source_clause = ",".join("?" for _ in sources)
    adjusted_clause = "AND is_adjusted = 1" if require_adjusted else ""
    rows = conn.execute(
        f"""
        SELECT ticker, source_id, bar_date, close, adj_close, is_adjusted, price_adjustment
        FROM fact_price_ohlcv
        WHERE ticker IN ({ticker_clause})
          AND source_id IN ({source_clause})
          AND bar_date <= ?
          {adjusted_clause}
        """,
        [*tickers, *sources, asof.isoformat()],
    ).fetchall()
    return select_latest_rows_by_source_priority(
        rows,
        asof_date=asof,
        source_priority=sources,
        max_staleness_days=max_staleness_days,
    )


def load_financial_rows(conn: Any, companies: list[Company], *, asof: date) -> dict[int, list[FinancialRow]]:
    company_ids = [company.company_id for company in companies]
    if not company_ids:
        return {}
    placeholders = ",".join("?" for _ in company_ids)
    rows = conn.execute(
        f"""
        SELECT company_id, accession_nodash, period_end, fiscal_year, fiscal_period, form, filed_date,
               revenue, gross_profit, operating_income, net_income, operating_cash_flow,
               capital_expenditures, free_cash_flow, research_and_development, interest_expense,
               cash_and_investments, total_debt, total_assets, stockholders_equity, shares_outstanding,
               payload_json
        FROM fact_financial_statement
        WHERE company_id IN ({placeholders})
          AND period_end <= ?
          AND NULLIF(filed_date, '') IS NOT NULL
          AND filed_date <= ?
        ORDER BY company_id, period_end, filed_date
        """,
        [*company_ids, asof.isoformat(), asof.isoformat()],
    ).fetchall()
    out: dict[int, list[FinancialRow]] = {}
    for row in rows:
        values = {metric: to_float(row[metric]) for metric in [*FLOW_METRICS, *BALANCE_METRICS]}
        item = FinancialRow(
            company_id=int(row["company_id"]),
            accession_nodash=str(row["accession_nodash"] or ""),
            period_end=str(row["period_end"] or ""),
            fiscal_year=int(row["fiscal_year"]) if row["fiscal_year"] is not None else None,
            fiscal_period=str(row["fiscal_period"] or "").upper(),
            form=str(row["form"] or "").upper(),
            filed_date=str(row["filed_date"] or ""),
            values=values,
            payload=safe_json_loads(row["payload_json"]),
        )
        out.setdefault(item.company_id, []).append(item)
    return out


def table_exists(conn: Any, table_name: str) -> bool:
    row = conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table_name,)).fetchone()
    return row is not None


def load_market_share_snapshots(
    conn: Any,
    companies: list[Company],
    *,
    asof: date,
    sources: list[str],
    max_staleness_days: int,
) -> dict[str, MarketShareSnapshot]:
    if not companies or not sources or not table_exists(conn, "fact_market_snapshot"):
        return {}
    tickers = [company.ticker for company in companies]
    placeholders = ",".join("?" for _ in tickers)
    source_placeholders = ",".join("?" for _ in sources)
    rows = conn.execute(
        f"""
        SELECT ticker, asof_date, source_id, shares_outstanding, market_cap, currency
        FROM fact_market_snapshot
        WHERE ticker IN ({placeholders})
          AND asof_date <= ?
          AND source_id IN ({source_placeholders})
          AND shares_outstanding IS NOT NULL
          AND shares_outstanding > 0
        ORDER BY ticker, source_id, asof_date DESC
        """,
        [*tickers, asof.isoformat(), *sources],
    ).fetchall()
    by_ticker_source: dict[tuple[str, str], MarketShareSnapshot] = {}
    for row in rows:
        snapshot_date = parse_date(row["asof_date"])
        if snapshot_date is None or (asof - snapshot_date).days > max_staleness_days:
            continue
        ticker = normalize_ticker(row["ticker"])
        source_id = str(row["source_id"] or "")
        key = (ticker, source_id)
        if key in by_ticker_source:
            continue
        shares = to_float(row["shares_outstanding"])
        if shares is None or shares <= 0:
            continue
        by_ticker_source[key] = MarketShareSnapshot(
            ticker=ticker,
            asof_date=str(row["asof_date"] or ""),
            source_id=source_id,
            shares_outstanding=shares,
            market_cap=to_float(row["market_cap"]),
            currency=str(row["currency"] or ""),
        )
    out: dict[str, MarketShareSnapshot] = {}
    for ticker in tickers:
        for source_id in sources:
            snapshot = by_ticker_source.get((ticker, source_id))
            if snapshot is not None:
                out[ticker] = snapshot
                break
    return out


def latest_metric(rows: list[FinancialRow], metric: str) -> float | None:
    row = latest_metric_row(rows, metric)
    return row.values.get(metric) if row is not None else None


def latest_metric_row(rows: list[FinancialRow], metric: str) -> FinancialRow | None:
    candidates = [row for row in rows if row.values.get(metric) is not None]
    if not candidates:
        return None
    candidates.sort(key=lambda item: (item.period_end, item.filed_date), reverse=True)
    return candidates[0]


def metric_concept(row: FinancialRow, metric: str) -> str:
    payload = row.payload.get(metric)
    if isinstance(payload, dict):
        return str(payload.get("concept") or "")
    return ""


def latest_share_value_by_concepts(rows: list[FinancialRow], concepts: set[str]) -> float | None:
    candidates = [
        row
        for row in rows
        if row.values.get("shares_outstanding") is not None and metric_concept(row, "shares_outstanding") in concepts
    ]
    if not candidates:
        return None
    candidates.sort(key=lambda item: (item.period_end, item.filed_date), reverse=True)
    return candidates[0].values.get("shares_outstanding")


def select_shares(rows: list[FinancialRow], *, allow_weighted_average_fallback: bool = False) -> ShareSelection:
    candidates = [row for row in rows if row.values.get("shares_outstanding") is not None]
    if not candidates:
        return ShareSelection(value=None, concept="", form="", period_end="")

    current_candidates = [
        row for row in candidates if metric_concept(row, "shares_outstanding") in CURRENT_SHARE_CONCEPTS
    ]
    diluted_candidates = [
        row for row in candidates if metric_concept(row, "shares_outstanding") in DILUTED_WEIGHTED_SHARE_CONCEPTS
    ]
    basic_candidates = [
        row for row in candidates if metric_concept(row, "shares_outstanding") in BASIC_WEIGHTED_SHARE_CONCEPTS
    ]

    current_shares = latest_share_value_by_concepts(rows, CURRENT_SHARE_CONCEPTS)
    diluted_shares = latest_share_value_by_concepts(rows, DILUTED_WEIGHTED_SHARE_CONCEPTS)
    basic_shares = latest_share_value_by_concepts(rows, BASIC_WEIGHTED_SHARE_CONCEPTS)

    fallback_groups = (diluted_candidates, basic_candidates, candidates) if allow_weighted_average_fallback else ()
    for group in (current_candidates, *fallback_groups):
        if group:
            group.sort(key=lambda item: (item.period_end, item.filed_date), reverse=True)
            selected = group[0]
            return ShareSelection(
                value=selected.values.get("shares_outstanding"),
                concept=metric_concept(selected, "shares_outstanding"),
                form=selected.form,
                period_end=selected.period_end,
                current_shares=current_shares,
                diluted_weighted_average_shares=diluted_shares,
                basic_weighted_average_shares=basic_shares,
            )

    return ShareSelection(
        value=None,
        concept="",
        form="",
        period_end="",
        current_shares=current_shares,
        diluted_weighted_average_shares=diluted_shares,
        basic_weighted_average_shares=basic_shares,
    )


def read_csv_flexible(path: Path) -> list[dict[str, str]]:
    encodings = ("utf-8-sig", "utf-8", "cp1252")
    last_error: UnicodeDecodeError | None = None
    for encoding in encodings:
        try:
            with path.open("r", encoding=encoding, newline="") as handle:
                return [dict(row) for row in csv.DictReader(handle)]
        except UnicodeDecodeError as exc:
            last_error = exc
            continue
    raise ValueError(f"Could not decode CSV {path}: {last_error}")


def load_share_count_overrides(path: Path | None, *, asof: date) -> dict[str, ShareCountOverride]:
    if path is None:
        return {}
    if not path.exists():
        LOGGER.warning("Configured share-count override CSV does not exist: %s", path)
        return {}
    out: dict[str, ShareCountOverride] = {}
    skipped = 0
    for raw_row in read_csv_flexible(path):
        ticker = normalize_ticker(row_get(raw_row, "ticker", "symbol"))
        shares = to_float(row_get(raw_row, "current_shares_outstanding", "shares_outstanding", "shares"))
        override_asof = row_get(raw_row, "asof_date", "date", "period_end")
        override_date = parse_date(override_asof) if override_asof else asof
        if not ticker or shares is None or shares <= 0 or override_date is None or override_date > asof:
            skipped += 1
            continue
        candidate = ShareCountOverride(
            ticker=ticker,
            current_shares_outstanding=shares,
            asof_date=override_date.isoformat(),
            source=row_get(raw_row, "source") or "manual_share_count_override",
            note=row_get(raw_row, "note", "notes"),
        )
        existing = out.get(ticker)
        if existing is None or candidate.asof_date > existing.asof_date:
            out[ticker] = candidate
    LOGGER.info("Loaded share-count overrides: rows=%d skipped=%d path=%s", len(out), skipped, path)
    return out


def latest_annual_row(rows: list[FinancialRow], metric: str = "revenue") -> FinancialRow | None:
    candidates = [
        row
        for row in rows
        if row.fiscal_period == "FY" and row.values.get(metric) is not None
    ]
    if not candidates:
        candidates = [row for row in rows if row.fiscal_period == "FY"]
    if not candidates:
        return None
    candidates.sort(key=lambda item: (item.period_end, item.filed_date), reverse=True)
    return candidates[0]


def prior_annual_row(rows: list[FinancialRow], latest: FinancialRow | None, metric: str = "revenue") -> FinancialRow | None:
    if latest is None:
        return None
    candidates = [
        row
        for row in rows
        if row.fiscal_period == "FY"
        and row.period_end < latest.period_end
        and row.values.get(metric) is not None
    ]
    candidates.sort(key=lambda item: (item.period_end, item.filed_date), reverse=True)
    return candidates[0] if candidates else None


def latest_interim_after_annual(rows: list[FinancialRow], annual: FinancialRow | None) -> FinancialRow | None:
    min_period = annual.period_end if annual is not None else ""
    candidates = [
        row
        for row in rows
        if quarter_number(row.fiscal_period) in {1, 2, 3}
        and (not min_period or row.period_end > min_period)
    ]
    revenue_candidates = [row for row in candidates if row.values.get("revenue") is not None]
    flow_candidates = [
        row for row in candidates if any(row.values.get(metric) is not None for metric in FLOW_METRICS)
    ]
    candidates = revenue_candidates or flow_candidates or candidates
    candidates.sort(key=lambda item: (item.period_end, item.filed_date), reverse=True)
    return candidates[0] if candidates else None


def prior_matching_interim(rows: list[FinancialRow], interim: FinancialRow | None) -> FinancialRow | None:
    if interim is None:
        return None
    interim_date = parse_date(interim.period_end)
    candidates: list[tuple[int, FinancialRow]] = []
    for row in rows:
        if row.fiscal_period != interim.fiscal_period or row.period_end >= interim.period_end:
            continue
        row_date = parse_date(row.period_end)
        if interim_date is None or row_date is None:
            distance = 9999
        else:
            distance = abs((interim_date - row_date).days - 365)
        candidates.append((distance, row))
    candidates = [candidate for candidate in candidates if candidate[0] <= 90]
    if not candidates:
        return None
    revenue_candidates = [candidate for candidate in candidates if candidate[1].values.get("revenue") is not None]
    flow_candidates = [
        candidate
        for candidate in candidates
        if any(candidate[1].values.get(metric) is not None for metric in FLOW_METRICS)
    ]
    candidates = revenue_candidates or flow_candidates or candidates
    candidates.sort(key=lambda item: (item[0], item[1].period_end))
    return candidates[0][1] if candidates else None


def prior_sequential_interim(rows: list[FinancialRow], interim: FinancialRow | None) -> FinancialRow | None:
    if interim is None:
        return None
    q = quarter_number(interim.fiscal_period)
    if q is None or q <= 1:
        return None
    candidates = [
        row
        for row in rows
        if row.fiscal_year == interim.fiscal_year
        and quarter_number(row.fiscal_period) == q - 1
        and row.period_end < interim.period_end
        and any(row.values.get(metric) is not None for metric in FLOW_METRICS)
    ]
    if not candidates:
        return None
    revenue_candidates = [row for row in candidates if row.values.get("revenue") is not None]
    candidates = revenue_candidates or candidates
    candidates.sort(key=lambda item: (item.period_end, item.filed_date), reverse=True)
    return candidates[0]


def latest_quarter_flow_value(
    rows: list[FinancialRow],
    interim: FinancialRow | None,
    metric: str,
) -> float | None:
    if interim is None:
        return None
    current_value = interim.values.get(metric)
    if current_value is None:
        return None
    q = quarter_number(interim.fiscal_period)
    if q is None:
        return None
    if q <= 1:
        return current_value
    prior_interim = prior_sequential_interim(rows, interim)
    prior_value = prior_interim.values.get(metric) if prior_interim is not None else None
    if prior_value is None:
        return None
    return current_value - prior_value


def annualize_interim(value: float | None, fiscal_period: str) -> float | None:
    q = quarter_number(fiscal_period)
    if value is None or q is None or q <= 0:
        return None
    return value * (4.0 / q)


def sanity_check_ttm(
    ttm_value: float | None,
    annual_value: float | None,
    metric: str,
    *,
    min_ratio: float,
    max_ratio: float,
) -> float | None:
    if ttm_value is None or annual_value is None or abs(annual_value) < 1e-6:
        return ttm_value
    ratio = ttm_value / annual_value
    if ratio > max_ratio or ratio < min_ratio:
        LOGGER.warning("TTM %s ratio=%.2f looks anomalous; using latest annual value", metric, ratio)
        return annual_value
    return ttm_value


def ttm_flow_value(
    rows: list[FinancialRow],
    metric: str,
    annual: FinancialRow | None,
    latest_interim: FinancialRow | None,
    prior_interim: FinancialRow | None,
    *,
    min_annual_ratio: float = 0.20,
    max_annual_ratio: float = 3.00,
) -> tuple[float | None, str]:
    annual_value = annual.values.get(metric) if annual is not None else None
    current_value = latest_interim.values.get(metric) if latest_interim is not None else None
    prior_value = prior_interim.values.get(metric) if prior_interim is not None else None
    if annual_value is not None and current_value is not None and prior_value is not None:
        value = annual_value + current_value - prior_value
        return (
            sanity_check_ttm(
                value,
                annual_value,
                metric,
                min_ratio=min_annual_ratio,
                max_ratio=max_annual_ratio,
            ),
            "annual_plus_interim_ytd_delta",
        )
    if annual_value is not None:
        return annual_value, "latest_annual"
    if latest_interim is not None:
        return annualize_interim(current_value, latest_interim.fiscal_period), "interim_annualized_proxy"
    latest_row = latest_metric_row(rows, metric)
    if latest_row is None:
        return None, "unavailable"
    latest_value = latest_row.values.get(metric)
    if quarter_number(latest_row.fiscal_period) is not None:
        return annualize_interim(latest_value, latest_row.fiscal_period), "latest_available_annualized_proxy"
    return latest_value, "latest_available_proxy"


def history_years(rows: list[FinancialRow], metric: str | None = None) -> float:
    dates = [
        row.period_end
        for row in rows
        if row.period_end and (metric is None or row.values.get(metric) is not None)
    ]
    if len(dates) < 2:
        return 0.0
    return year_span(min(dates), max(dates))


def min_core_history_years(rows: list[FinancialRow]) -> float:
    spans = [history_years(rows, metric) for metric in CORE_HISTORY_METRICS]
    spans = [span for span in spans if span > 0]
    return round(min(spans), 2) if spans else 0.0


def gross_margin_trend(rows: list[FinancialRow]) -> float | None:
    annual_rows = [
        row
        for row in rows
        if row.fiscal_period == "FY"
        and row.values.get("gross_profit") is not None
        and row.values.get("revenue") is not None
    ]
    annual_rows.sort(key=lambda item: (item.period_end, item.filed_date))
    recent_rows = annual_rows[-3:]
    for prev, curr in zip(recent_rows[:-1], recent_rows[1:]):
        prev_rev = prev.values.get("revenue") or 0.0
        curr_rev = curr.values.get("revenue") or 0.0
        if prev_rev > 0 and abs(curr_rev / prev_rev - 1.0) > 0.80:
            LOGGER.debug("gross_margin_trend: revenue discontinuity detected; skipping trend")
            return None
    gm_series = [
        safe_div(row.values.get("gross_profit"), row.values.get("revenue"))
        for row in recent_rows
    ]
    values = [value for value in gm_series if value is not None]
    if len(values) < 2:
        return None
    n = len(values)
    x_mean = (n - 1) / 2.0
    y_mean = sum(values) / n
    denominator = sum((idx - x_mean) ** 2 for idx in range(n))
    if denominator <= 0:
        return None
    slope = sum((idx - x_mean) * (value - y_mean) for idx, value in enumerate(values)) / denominator
    return slope if math.isfinite(slope) else None


def data_confidence_score(
    fact_years: float,
    min_core_years: float,
    *,
    core_min_years: float,
    core_min_group_years: float,
) -> float:
    fact_component = clamp(safe_div(fact_years, core_min_years) or 0.0, 0.0, 1.0) * 100.0
    group_component = clamp(safe_div(min_core_years, core_min_group_years) or 0.0, 0.0, 1.0) * 100.0
    return round(0.6 * fact_component + 0.4 * group_component, 1)


def calibration_bucket(
    fact_years: float,
    min_core_group_years: float,
    *,
    core_min_years: float,
    core_min_group_years: float,
    short_min_years: float,
) -> str:
    if fact_years >= core_min_years and min_core_group_years >= core_min_group_years:
        return "core_calibration"
    if fact_years >= short_min_years:
        return "short_history"
    return "new_issue_watchlist"


def build_raw_feature_row(
    company: Company,
    financial_rows: list[FinancialRow],
    price_row: dict[str, Any] | None,
    *,
    asof: date,
    max_staleness_days: int,
    core_min_years: float,
    core_min_group_years: float,
    short_min_years: float,
    ttm_sanity_min_annual_ratio: float,
    ttm_sanity_max_annual_ratio: float,
    allow_sec_weighted_average_share_fallback: bool,
    market_share_snapshot: MarketShareSnapshot | None = None,
    share_override: ShareCountOverride | None = None,
) -> FeatureRow:
    feature = FeatureRow(
        asof_date=asof.isoformat(),
        company_id=company.company_id,
        ticker=company.ticker,
        company_name=company.company_name,
        subsector=company.subsector,
    )
    if price_row is not None:
        feature.market_source_id = str(price_row.get("source_id") or "")
        feature.latest_price_date = str(price_row.get("bar_date") or "")
        adjusted_close = to_float(price_row.get("adj_close"))
        raw_close = to_float(price_row.get("close"))
        feature.latest_close = adjusted_close if adjusted_close is not None else raw_close
        price_date = parse_date(feature.latest_price_date)
        feature.price_staleness_days = (asof - price_date).days if price_date is not None else None

    financial_rows = sorted(financial_rows, key=lambda item: (item.period_end, item.filed_date))
    annual = latest_annual_row(financial_rows)
    prior_annual = prior_annual_row(financial_rows, annual)
    interim = latest_interim_after_annual(financial_rows, annual)
    prior_interim = prior_matching_interim(financial_rows, interim)
    methods: dict[str, str] = {}
    ttm_values: dict[str, float | None] = {}
    for metric in FLOW_METRICS:
        value, method = ttm_flow_value(
            financial_rows,
            metric,
            annual,
            interim,
            prior_interim,
            min_annual_ratio=ttm_sanity_min_annual_ratio,
            max_annual_ratio=ttm_sanity_max_annual_ratio,
        )
        ttm_values[metric] = value
        methods[metric] = method

    feature.revenue_ttm = ttm_values["revenue"]
    feature.gross_profit_ttm = ttm_values["gross_profit"]
    feature.operating_income_ttm = ttm_values["operating_income"]
    feature.net_income_ttm = ttm_values["net_income"]
    feature.operating_cash_flow_ttm = ttm_values["operating_cash_flow"]
    feature.capital_expenditures_ttm = ttm_values["capital_expenditures"]
    if feature.capital_expenditures_ttm is not None and feature.capital_expenditures_ttm < 0:
        LOGGER.debug(
            "%s: capital_expenditures_ttm is negative (%.0f); normalizing sign for FCF",
            company.ticker,
            feature.capital_expenditures_ttm,
        )
        feature.capital_expenditures_ttm = abs(feature.capital_expenditures_ttm)
    feature.research_and_development_ttm = ttm_values["research_and_development"]
    feature.interest_expense_ttm = ttm_values["interest_expense"]
    computed_fcf = None
    if feature.operating_cash_flow_ttm is not None and feature.capital_expenditures_ttm is not None:
        computed_fcf = feature.operating_cash_flow_ttm - feature.capital_expenditures_ttm
    feature.free_cash_flow_ttm = computed_fcf if computed_fcf is not None else ttm_values["free_cash_flow"]
    feature.ttm_method = methods.get("revenue", "unavailable")
    latest_quarter_rd = latest_quarter_flow_value(financial_rows, interim, "research_and_development")
    if latest_quarter_rd is not None:
        feature.annualized_research_and_development = latest_quarter_rd * 4.0
    else:
        interim_rd = interim.values.get("research_and_development") if interim is not None else None
        annualized_rd = annualize_interim(interim_rd, interim.fiscal_period) if interim is not None else None
        if annualized_rd is not None:
            feature.annualized_research_and_development = annualized_rd
        elif feature.research_and_development_ttm is not None:
            feature.annualized_research_and_development = feature.research_and_development_ttm

    if annual is not None and prior_annual is not None:
        current_revenue = annual.values.get("revenue")
        prior_revenue = prior_annual.values.get("revenue")
        if current_revenue is not None and prior_revenue is not None:
            feature.revenue_yoy_growth = safe_div(current_revenue - prior_revenue, prior_revenue)
        current_rd = annual.values.get("research_and_development")
        prior_rd = prior_annual.values.get("research_and_development")
        if current_rd is not None and prior_rd is not None:
            feature.rd_growth_yoy = safe_div(current_rd - prior_rd, prior_rd)
    if interim is not None and prior_interim is not None:
        interim_revenue = interim.values.get("revenue")
        prior_interim_revenue = prior_interim.values.get("revenue")
        if interim_revenue is not None and prior_interim_revenue is not None:
            feature.quarterly_revenue_surprise_yoy = safe_div(
                interim_revenue - prior_interim_revenue,
                abs(prior_interim_revenue),
            )
    feature.gross_margin_ttm = safe_div(feature.gross_profit_ttm, feature.revenue_ttm)
    feature.operating_margin_ttm = safe_div(feature.operating_income_ttm, feature.revenue_ttm)
    feature.net_margin_ttm = safe_div(feature.net_income_ttm, feature.revenue_ttm)
    feature.fcf_margin_ttm = safe_div(feature.free_cash_flow_ttm, feature.revenue_ttm)
    feature.rd_to_revenue_ttm = safe_div(feature.research_and_development_ttm, feature.revenue_ttm)
    if feature.revenue_yoy_growth is not None and feature.fcf_margin_ttm is not None:
        annual_end = parse_date(annual.period_end) if annual is not None else None
        period_gap_days = (asof - annual_end).days if annual_end is not None else 999
        if period_gap_days <= 270 or feature.ttm_method == "annual_plus_interim_ytd_delta":
            feature.rule_of_40 = (feature.revenue_yoy_growth + feature.fcf_margin_ttm) * 100.0

    feature.cash_and_investments = latest_metric(financial_rows, "cash_and_investments")
    feature.total_liquidity = feature.cash_and_investments
    latest_quarter_ocf = latest_quarter_flow_value(financial_rows, interim, "operating_cash_flow")
    if latest_quarter_ocf is not None and latest_quarter_ocf < 0:
        feature.latest_quarter_operating_cash_burn = abs(latest_quarter_ocf)
        feature.annualized_operating_cash_burn = feature.latest_quarter_operating_cash_burn * 4.0
    else:
        interim_ocf = interim.values.get("operating_cash_flow") if interim is not None else None
        annualized_ocf = annualize_interim(interim_ocf, interim.fiscal_period) if interim is not None else None
        if annualized_ocf is not None and annualized_ocf < 0:
            feature.annualized_operating_cash_burn = abs(annualized_ocf)
            feature.latest_quarter_operating_cash_burn = feature.annualized_operating_cash_burn / 4.0
        elif feature.operating_cash_flow_ttm is not None and feature.operating_cash_flow_ttm < 0:
            feature.annualized_operating_cash_burn = abs(feature.operating_cash_flow_ttm)
            feature.latest_quarter_operating_cash_burn = feature.annualized_operating_cash_burn / 4.0
    feature.financial_runway_years = safe_div(feature.total_liquidity, feature.annualized_operating_cash_burn)
    feature.total_debt = latest_metric(financial_rows, "total_debt")
    feature.total_assets = latest_metric(financial_rows, "total_assets")
    feature.stockholders_equity = latest_metric(financial_rows, "stockholders_equity")
    share_selection = select_shares(
        financial_rows,
        allow_weighted_average_fallback=allow_sec_weighted_average_share_fallback,
    )
    feature.shares_outstanding = share_selection.value
    feature.current_shares_outstanding = share_selection.current_shares
    feature.diluted_weighted_average_shares = share_selection.diluted_weighted_average_shares
    feature.basic_weighted_average_shares = share_selection.basic_weighted_average_shares
    feature.shares_source_concept = share_selection.concept
    feature.shares_source_form = share_selection.form
    feature.shares_source_period = share_selection.period_end
    feature.market_cap_validated_flag = int(share_selection.concept in CURRENT_SHARE_CONCEPTS)
    if market_share_snapshot is not None:
        feature.shares_outstanding = market_share_snapshot.shares_outstanding
        feature.current_shares_outstanding = market_share_snapshot.shares_outstanding
        feature.shares_source_concept = f"{market_share_snapshot.source_id}_shares_outstanding"
        feature.shares_source_form = market_share_snapshot.source_id
        feature.shares_source_period = market_share_snapshot.asof_date
        feature.market_cap_validated_flag = 1
    if share_override is not None:
        feature.shares_outstanding = share_override.current_shares_outstanding
        feature.current_shares_outstanding = share_override.current_shares_outstanding
        feature.shares_source_concept = "manual_current_shares_override"
        feature.shares_source_form = share_override.source
        feature.shares_source_period = share_override.asof_date
        feature.market_cap_validated_flag = 1
    prior_shares = prior_annual.values.get("shares_outstanding") if prior_annual is not None else None
    if feature.shares_outstanding is not None and prior_shares is not None:
        feature.shares_yoy_growth = safe_div(feature.shares_outstanding - prior_shares, prior_shares)
    if feature.total_debt is not None and feature.cash_and_investments is not None:
        feature.net_debt = feature.total_debt - feature.cash_and_investments

    if feature.latest_close is not None and feature.shares_outstanding is not None and feature.shares_outstanding > 0:
        feature.market_cap = feature.latest_close * feature.shares_outstanding
    elif market_share_snapshot is not None and market_share_snapshot.market_cap is not None:
        feature.market_cap = market_share_snapshot.market_cap
    if feature.market_cap is not None and feature.total_debt is not None and feature.cash_and_investments is not None:
        feature.enterprise_value = feature.market_cap + feature.total_debt - feature.cash_and_investments
    feature.price_to_sales = safe_div(feature.market_cap, feature.revenue_ttm)
    feature.ev_to_sales = safe_div(feature.enterprise_value, feature.revenue_ttm)
    feature.fcf_yield = safe_div(feature.free_cash_flow_ttm, feature.market_cap)
    feature.net_debt_to_revenue = safe_div(feature.net_debt, feature.revenue_ttm)
    feature.return_on_assets = safe_div(feature.net_income_ttm, feature.total_assets)
    if feature.stockholders_equity is not None and feature.stockholders_equity > 0:
        feature.return_on_equity = safe_div(feature.net_income_ttm, feature.stockholders_equity)
    if feature.interest_expense_ttm is not None and feature.interest_expense_ttm > 0:
        feature.interest_coverage = safe_div(feature.operating_income_ttm, feature.interest_expense_ttm)
    if feature.net_income_ttm is not None and feature.operating_cash_flow_ttm is not None:
        prior_total_assets = prior_annual.values.get("total_assets") if prior_annual is not None else None
        if feature.total_assets is not None and prior_total_assets is not None:
            avg_assets = (feature.total_assets + prior_total_assets) / 2.0
            if avg_assets > 0:
                feature.accrual_ratio = safe_div(feature.net_income_ttm - feature.operating_cash_flow_ttm, avg_assets)
        if feature.accrual_ratio is None and feature.total_assets is not None and feature.total_assets > 0:
            feature.accrual_ratio = safe_div(feature.net_income_ttm - feature.operating_cash_flow_ttm, feature.total_assets)
    feature.gross_margin_trend_3y = gross_margin_trend(financial_rows)
    feature.financial_history_years = history_years(financial_rows)
    feature.min_core_group_years = min_core_history_years(financial_rows)
    feature.data_confidence_score = data_confidence_score(
        feature.financial_history_years,
        feature.min_core_group_years,
        core_min_years=core_min_years,
        core_min_group_years=core_min_group_years,
    )
    feature.calibration_bucket = calibration_bucket(
        feature.financial_history_years,
        feature.min_core_group_years,
        core_min_years=core_min_years,
        core_min_group_years=core_min_group_years,
        short_min_years=short_min_years,
    )

    critical_fields = ["latest_close", "revenue_ttm", "shares_outstanding"]
    optional_fields = [
        "operating_income_ttm",
        "free_cash_flow_ttm",
        "research_and_development_ttm",
        "cash_and_investments",
        "total_debt",
    ]
    feature.missing_fields = [
        field_name
        for field_name in [*critical_fields, *optional_fields]
        if getattr(feature, field_name) is None
    ]
    if any(field_name in feature.missing_fields for field_name in critical_fields):
        feature.data_quality_status = "fail"
    elif feature.price_staleness_days is not None and feature.price_staleness_days > max_staleness_days:
        feature.data_quality_status = "review"
    elif feature.market_cap is not None and feature.shares_source_concept and not feature.market_cap_validated_flag:
        feature.data_quality_status = "review"
    elif "proxy" in feature.ttm_method or len(feature.missing_fields) >= 3:
        feature.data_quality_status = "review"
    else:
        feature.data_quality_status = "pass"

    selected_source_rows = {
        "annual": annual,
        "prior_annual": prior_annual,
        "latest_interim": interim,
        "prior_matching_interim": prior_interim,
    }
    selected_financial_accessions = sorted(
        {
            row.accession_nodash
            for row in selected_source_rows.values()
            if row is not None and row.accession_nodash
        }
    )
    feature.payload = {
        "annual_period_end": annual.period_end if annual is not None else "",
        "prior_annual_period_end": prior_annual.period_end if prior_annual is not None else "",
        "latest_interim_period_end": interim.period_end if interim is not None else "",
        "prior_matching_interim_period_end": prior_interim.period_end if prior_interim is not None else "",
        "ttm_method": feature.ttm_method,
        "metric_ttm_methods": methods,
        "financial_row_count": len(financial_rows),
        "selected_financial_accessions": selected_financial_accessions,
        "selected_financial_sources": {
            label: (
                {
                    "accession_nodash": row.accession_nodash,
                    "filed_date": row.filed_date,
                    "form": row.form,
                    "period_end": row.period_end,
                    "fiscal_period": row.fiscal_period,
                }
                if row is not None
                else None
            )
            for label, row in selected_source_rows.items()
        },
        "missing_fields": feature.missing_fields,
        "shares_source": {
            "concept": feature.shares_source_concept,
            "form": feature.shares_source_form,
            "period": feature.shares_source_period,
            "market_cap_validated": bool(feature.market_cap_validated_flag),
        },
        "pre_revenue_runway_inputs": {
            "total_liquidity": feature.total_liquidity,
            "latest_quarter_operating_cash_burn": feature.latest_quarter_operating_cash_burn,
            "annualized_operating_cash_burn": feature.annualized_operating_cash_burn,
            "financial_runway_years": feature.financial_runway_years,
            "annualized_research_and_development": feature.annualized_research_and_development,
        },
        "share_count_override": (
            {
                "ticker": share_override.ticker,
                "current_shares_outstanding": share_override.current_shares_outstanding,
                "asof_date": share_override.asof_date,
                "source": share_override.source,
                "note": share_override.note,
            }
            if share_override is not None
            else None
        ),
        "market_share_snapshot": (
            {
                "ticker": market_share_snapshot.ticker,
                "source_id": market_share_snapshot.source_id,
                "shares_outstanding": market_share_snapshot.shares_outstanding,
                "market_cap": market_share_snapshot.market_cap,
                "asof_date": market_share_snapshot.asof_date,
            }
            if market_share_snapshot is not None
            else None
        ),
    }
    return feature


def winsorize_pairs(values: list[tuple[int, float]], *, low_pct: float, high_pct: float) -> list[tuple[int, float]]:
    if len(values) < 4:
        return list(values)
    if not 0.0 <= low_pct < high_pct <= 1.0:
        raise ValueError(f"winsor bounds must satisfy 0 <= low < high <= 1, got {low_pct}, {high_pct}")
    sorted_values = sorted(value for _, value in values)
    low_idx = max(0, min(len(sorted_values) - 1, math.ceil(low_pct * len(sorted_values)) - 1))
    high_idx = max(0, min(len(sorted_values) - 1, math.ceil(high_pct * len(sorted_values)) - 1))
    low_bound = sorted_values[low_idx]
    high_bound = sorted_values[high_idx]
    if low_bound > high_bound:
        low_bound, high_bound = high_bound, low_bound
    return [(idx, max(low_bound, min(high_bound, value))) for idx, value in values]


def percentile_from_pairs(
    values: list[tuple[int, float]],
    *,
    higher_is_better: bool,
    winsor_low_pct: float = 0.10,
    winsor_high_pct: float = 0.90,
) -> dict[int, float]:
    if not values:
        return {}
    values = winsorize_pairs(values, low_pct=winsor_low_pct, high_pct=winsor_high_pct)
    values.sort(key=lambda item: item[1])
    if len(values) == 1:
        return {values[0][0]: 50.0}
    scores: dict[int, float] = {}
    denominator = len(values) - 1
    for rank, (idx, _) in enumerate(values):
        pct = 100.0 * rank / denominator
        scores[idx] = pct if higher_is_better else 100.0 - pct
    return scores


def percentile_scores(
    rows: list[FeatureRow],
    field_name: str,
    *,
    higher_is_better: bool,
    exclude_nonpositive: bool = False,
    winsor_low_pct: float = 0.10,
    winsor_high_pct: float = 0.90,
) -> dict[int, float]:
    values: list[tuple[int, float]] = []
    for idx, row in enumerate(rows):
        value = to_float(getattr(row, field_name))
        if value is None:
            continue
        if exclude_nonpositive and value <= 0:
            continue
        values.append((idx, value))
    return percentile_from_pairs(
        values,
        higher_is_better=higher_is_better,
        winsor_low_pct=winsor_low_pct,
        winsor_high_pct=winsor_high_pct,
    )


def percentile_scores_by_subsector(
    rows: list[FeatureRow],
    field_name: str,
    *,
    higher_is_better: bool,
    exclude_nonpositive: bool = False,
    blend_weight: float,
    winsor_low_pct: float,
    winsor_high_pct: float,
) -> dict[int, float]:
    universe_scores = percentile_scores(
        rows,
        field_name,
        higher_is_better=higher_is_better,
        exclude_nonpositive=exclude_nonpositive,
        winsor_low_pct=winsor_low_pct,
        winsor_high_pct=winsor_high_pct,
    )
    by_subsector: dict[str, list[tuple[int, float]]] = {}
    for idx, row in enumerate(rows):
        value = to_float(getattr(row, field_name))
        if value is None:
            continue
        if exclude_nonpositive and value <= 0:
            continue
        subsector = str(row.subsector or "unknown").strip().lower() or "unknown"
        by_subsector.setdefault(subsector, []).append((idx, value))
    subsector_scores: dict[int, float] = {}
    for group_values in by_subsector.values():
        subsector_scores.update(
            percentile_from_pairs(
                group_values,
                higher_is_better=higher_is_better,
                winsor_low_pct=winsor_low_pct,
                winsor_high_pct=winsor_high_pct,
            )
        )
    blend = clamp(blend_weight, 0.0, 1.0)
    out: dict[int, float] = {}
    for idx in universe_scores:
        universe_score = universe_scores[idx]
        subsector = str(rows[idx].subsector or "unknown").strip().lower() or "unknown"
        subsector_score = subsector_scores.get(idx)
        if subsector_score is None or len(by_subsector.get(subsector, [])) < 3:
            out[idx] = universe_score
        else:
            out[idx] = blend * subsector_score + (1.0 - blend) * universe_score
    return out


def weighted_score(components: list[tuple[float | None, float]], *, neutral: float) -> float:
    active = [(score, weight) for score, weight in components if score is not None and math.isfinite(score)]
    if not active:
        return neutral
    total_weight = sum(weight for _, weight in active)
    if total_weight <= 0:
        return neutral
    return sum(score * weight for score, weight in active) / total_weight


def history_confidence(row: FeatureRow, *, core_min_years: float, core_min_group_years: float) -> float:
    if row.data_confidence_score:
        return row.data_confidence_score
    return data_confidence_score(
        row.financial_history_years,
        row.min_core_group_years,
        core_min_years=core_min_years,
        core_min_group_years=core_min_group_years,
    )


def apply_scores(rows: list[FeatureRow], *, policy: FinancialFeaturePolicy) -> None:
    for row in rows:
        growth = row.revenue_yoy_growth
        ev_to_sales = row.ev_to_sales
        row.growth_to_ev_sales = safe_div(max(growth, -0.25) if growth is not None else None, ev_to_sales)

    def scores(field_name: str, *, higher_is_better: bool, exclude_nonpositive: bool = False) -> dict[int, float]:
        return percentile_scores_by_subsector(
            rows,
            field_name,
            higher_is_better=higher_is_better,
            exclude_nonpositive=exclude_nonpositive,
            blend_weight=policy.subsector_blend_weight,
            winsor_low_pct=policy.winsor_low_pct,
            winsor_high_pct=policy.winsor_high_pct,
        )

    score_maps = {
        "gross_margin_ttm": scores("gross_margin_ttm", higher_is_better=True),
        "operating_margin_ttm": scores("operating_margin_ttm", higher_is_better=True),
        "fcf_margin_ttm": scores("fcf_margin_ttm", higher_is_better=True),
        "revenue_yoy_growth": scores("revenue_yoy_growth", higher_is_better=True),
        "net_debt_to_revenue": scores("net_debt_to_revenue", higher_is_better=False),
        "rd_to_revenue_ttm": scores("rd_to_revenue_ttm", higher_is_better=True),
        "accrual_ratio": scores("accrual_ratio", higher_is_better=False),
        "shares_yoy_growth": scores("shares_yoy_growth", higher_is_better=False),
        "ev_to_sales": scores("ev_to_sales", higher_is_better=False, exclude_nonpositive=True),
        "price_to_sales": scores("price_to_sales", higher_is_better=False, exclude_nonpositive=True),
        "fcf_yield": scores("fcf_yield", higher_is_better=True),
        "growth_to_ev_sales": scores("growth_to_ev_sales", higher_is_better=True),
    }

    for idx, row in enumerate(rows):
        if row.data_quality_status == "fail":
            row.fundamental_quality_score_v1 = None
            row.valuation_score_v1 = None
            row.value_trap_score = None
            continue
        hist_score = history_confidence(
            row,
            core_min_years=policy.core_min_years,
            core_min_group_years=policy.core_min_group_years,
        )
        fundamental = weighted_score(
            [
                (score_maps["gross_margin_ttm"].get(idx), policy.fundamental_weights["gross_margin"]),
                (score_maps["operating_margin_ttm"].get(idx), policy.fundamental_weights["operating_margin"]),
                (score_maps["fcf_margin_ttm"].get(idx), policy.fundamental_weights["fcf_margin"]),
                (score_maps["revenue_yoy_growth"].get(idx), policy.fundamental_weights["revenue_growth"]),
                (score_maps["net_debt_to_revenue"].get(idx), policy.fundamental_weights["balance_sheet"]),
                (score_maps["rd_to_revenue_ttm"].get(idx), policy.fundamental_weights["rd_intensity"]),
                (hist_score, policy.fundamental_weights["history_confidence"]),
                (score_maps["accrual_ratio"].get(idx), policy.fundamental_weights["accrual_quality"]),
                (score_maps["shares_yoy_growth"].get(idx), policy.fundamental_weights["dilution_control"]),
            ],
            neutral=policy.neutral_component_score,
        )
        if "proxy" in row.ttm_method:
            fundamental -= 5.0
        if row.calibration_bucket == "new_issue_watchlist":
            fundamental -= 5.0
        row.fundamental_quality_score_v1 = round(clamp(fundamental), 2)

        pre_valuation = weighted_score(
            [
                (score_maps["ev_to_sales"].get(idx), policy.valuation_weights["ev_to_sales"]),
                (score_maps["price_to_sales"].get(idx), policy.valuation_weights["price_to_sales"]),
                (score_maps["fcf_yield"].get(idx), policy.valuation_weights["fcf_yield"]),
                (score_maps["growth_to_ev_sales"].get(idx), policy.valuation_weights["growth_to_ev_sales"]),
                (hist_score, policy.valuation_weights["history_confidence"]),
            ],
            neutral=policy.neutral_component_score,
        )
        weak_quality_penalty = max(0.0, 65.0 - row.fundamental_quality_score_v1)
        deteriorating_penalty = 10.0 if row.revenue_yoy_growth is not None and row.revenue_yoy_growth < -0.05 else 0.0
        debt_penalty = 0.0
        if row.net_debt_to_revenue is not None and row.net_debt_to_revenue > 0.5:
            debt_penalty = min(15.0, row.net_debt_to_revenue * 10.0)
        margin_penalty = 0.0
        if row.gross_margin_trend_3y is not None and row.gross_margin_trend_3y < -0.02:
            margin_penalty = min(10.0, abs(row.gross_margin_trend_3y) * 200.0)
        dilution_penalty = 0.0
        if row.shares_yoy_growth is not None and row.shares_yoy_growth > 0.05:
            dilution_penalty = min(10.0, row.shares_yoy_growth * 50.0)
        row.value_trap_score = round(
            clamp(weak_quality_penalty + deteriorating_penalty + debt_penalty + margin_penalty + dilution_penalty),
            2,
        )
        row.valuation_score_v1 = round(clamp(pre_valuation - min(25.0, row.value_trap_score * 0.35)), 2)
        row.payload["component_scores"] = {
            "gross_margin": score_maps["gross_margin_ttm"].get(idx),
            "operating_margin": score_maps["operating_margin_ttm"].get(idx),
            "fcf_margin": score_maps["fcf_margin_ttm"].get(idx),
            "revenue_growth": score_maps["revenue_yoy_growth"].get(idx),
            "balance_sheet": score_maps["net_debt_to_revenue"].get(idx),
            "rd_intensity": score_maps["rd_to_revenue_ttm"].get(idx),
            "accrual_quality": score_maps["accrual_ratio"].get(idx),
            "dilution_control": score_maps["shares_yoy_growth"].get(idx),
            "ev_to_sales": score_maps["ev_to_sales"].get(idx),
            "price_to_sales": score_maps["price_to_sales"].get(idx),
            "fcf_yield": score_maps["fcf_yield"].get(idx),
            "growth_to_ev_sales": score_maps["growth_to_ev_sales"].get(idx),
            "history_confidence": hist_score,
            "subsector_percentile_blend_weight": policy.subsector_blend_weight,
            "winsor_low_pct": policy.winsor_low_pct,
            "winsor_high_pct": policy.winsor_high_pct,
            "value_trap_penalties": {
                "weak_quality": weak_quality_penalty,
                "deteriorating_revenue": deteriorating_penalty,
                "debt": debt_penalty,
                "gross_margin_deterioration": margin_penalty,
                "share_dilution": dilution_penalty,
            },
            "neutral_missing_component_score": policy.neutral_component_score,
            "fundamental_component_weights": policy.fundamental_weights,
            "valuation_component_weights": policy.valuation_weights,
        }


def row_to_dict(row: FeatureRow) -> dict[str, Any]:
    out = {field_name: getattr(row, field_name) for field_name in FEATURE_FIELDS if hasattr(row, field_name)}
    out["missing_fields"] = ";".join(row.missing_fields)
    return out


def upsert_feature_rows(conn: Any, rows: list[FeatureRow]) -> int:
    if not rows:
        return 0
    now = utc_now()
    fields = [
        "asof_date",
        "company_id",
        "ticker",
        "company_name",
        "subsector",
        "market_source_id",
        "latest_price_date",
        "latest_close",
        "price_staleness_days",
        "revenue_ttm",
        "gross_profit_ttm",
        "operating_income_ttm",
        "net_income_ttm",
        "operating_cash_flow_ttm",
        "capital_expenditures_ttm",
        "free_cash_flow_ttm",
        "research_and_development_ttm",
        "annualized_research_and_development",
        "interest_expense_ttm",
        "revenue_yoy_growth",
        "rd_growth_yoy",
        "gross_margin_ttm",
        "operating_margin_ttm",
        "net_margin_ttm",
        "fcf_margin_ttm",
        "rd_to_revenue_ttm",
        "rule_of_40",
        "cash_and_investments",
        "total_liquidity",
        "latest_quarter_operating_cash_burn",
        "annualized_operating_cash_burn",
        "financial_runway_years",
        "total_debt",
        "total_assets",
        "stockholders_equity",
        "net_debt",
        "shares_outstanding",
        "current_shares_outstanding",
        "diluted_weighted_average_shares",
        "basic_weighted_average_shares",
        "shares_source_concept",
        "shares_source_form",
        "shares_source_period",
        "market_cap_validated_flag",
        "shares_yoy_growth",
        "market_cap",
        "enterprise_value",
        "price_to_sales",
        "ev_to_sales",
        "growth_to_ev_sales",
        "fcf_yield",
        "net_debt_to_revenue",
        "return_on_assets",
        "return_on_equity",
        "interest_coverage",
        "accrual_ratio",
        "gross_margin_trend_3y",
        "quarterly_revenue_surprise_yoy",
        "financial_history_years",
        "min_core_group_years",
        "data_confidence_score",
        "calibration_bucket",
        "ttm_method",
        "data_quality_status",
        "missing_fields",
        "fundamental_quality_score_v1",
        "valuation_score_v1",
        "value_trap_score",
        "payload_json",
    ]
    payload_rows = []
    for row in rows:
        values = row_to_dict(row)
        values["payload_json"] = json.dumps(row.payload, ensure_ascii=True, sort_keys=True)
        payload_rows.append(tuple(values.get(field_name) for field_name in fields) + (now, now))
    field_sql = ", ".join(fields)
    placeholder_sql = ", ".join("?" for _ in fields)
    update_sql = ",\n            ".join(
        f"{field_name} = excluded.{field_name}"
        for field_name in fields
        if field_name not in {"asof_date", "company_id"}
    )
    conn.executemany(
        f"""
        INSERT INTO feature_financial_valuation(
            {field_sql}, created_at, updated_at
        )
        VALUES ({placeholder_sql}, ?, ?)
        ON CONFLICT(asof_date, company_id) DO UPDATE SET
            {update_sql},
            updated_at = excluded.updated_at
        """,
        payload_rows,
    )
    conn.executemany(
        """
        INSERT INTO feature_fundamental_quality(asof_date, company_id, score, payload_json, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(asof_date, company_id) DO UPDATE SET
            score = excluded.score,
            payload_json = excluded.payload_json,
            updated_at = excluded.updated_at
        """,
        [
            (
                row.asof_date,
                row.company_id,
                row.fundamental_quality_score_v1,
                json.dumps({"source": "feature_financial_valuation", **row.payload}, ensure_ascii=True, sort_keys=True),
                now,
                now,
            )
            for row in rows
        ],
    )
    conn.executemany(
        """
        INSERT INTO feature_valuation(asof_date, company_id, score, value_trap_score, payload_json, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(asof_date, company_id) DO UPDATE SET
            score = excluded.score,
            value_trap_score = excluded.value_trap_score,
            payload_json = excluded.payload_json,
            updated_at = excluded.updated_at
        """,
        [
            (
                row.asof_date,
                row.company_id,
                row.valuation_score_v1,
                row.value_trap_score,
                json.dumps({"source": "feature_financial_valuation", **row.payload}, ensure_ascii=True, sort_keys=True),
                now,
                now,
            )
            for row in rows
        ],
    )
    return len(rows)


def replace_data_quality_issues(conn: Any, rows: list[FeatureRow], *, asof: str) -> int:
    conn.execute(
        "DELETE FROM data_quality_issues WHERE table_name = ? AND asof_date = ?",
        ("feature_financial_valuation", asof),
    )
    issue_rows: list[tuple[Any, ...]] = []
    now = utc_now()
    for row in rows:
        if row.data_quality_status == "pass":
            continue
        severity = "error" if row.data_quality_status == "fail" else "warning"
        issue_rows.append(
            (
                asof,
                row.company_id,
                row.market_source_id or None,
                "feature_financial_valuation",
                "missing_fields",
                row.data_quality_status,
                severity,
                f"{row.ticker}: {','.join(row.missing_fields) or row.ttm_method}",
                now,
            )
        )
    if issue_rows:
        conn.executemany(
            """
            INSERT INTO data_quality_issues(
                asof_date, company_id, source_id, table_name, field_name, issue_type,
                severity, message, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            issue_rows,
        )
    return len(issue_rows)


def write_csv(path: Path, rows: list[FeatureRow]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FEATURE_FIELDS, extrasaction="ignore", lineterminator="\n")
        writer.writeheader()
        writer.writerows(row_to_dict(row) for row in rows)


def build_features(
    conn: Any,
    companies: list[Company],
    *,
    asof: date,
    policy: FinancialFeaturePolicy,
    share_overrides: dict[str, ShareCountOverride] | None = None,
) -> list[FeatureRow]:
    share_overrides = share_overrides or {}
    price_rows = load_price_selection(
        conn,
        companies,
        asof=asof,
        sources=policy.market_sources,
        max_staleness_days=policy.max_staleness_days,
        require_adjusted=policy.require_adjusted,
    )
    financial_rows_by_company = load_financial_rows(conn, companies, asof=asof)
    market_share_snapshots = load_market_share_snapshots(
        conn,
        companies,
        asof=asof,
        sources=policy.share_count_sources,
        max_staleness_days=policy.share_count_max_staleness_days,
    )
    rows = [
        build_raw_feature_row(
            company,
            financial_rows_by_company.get(company.company_id, []),
            price_rows.get(company.ticker),
            asof=asof,
            max_staleness_days=policy.max_staleness_days,
            core_min_years=policy.core_min_years,
            core_min_group_years=policy.core_min_group_years,
            short_min_years=policy.short_min_years,
            ttm_sanity_min_annual_ratio=policy.ttm_sanity_min_annual_ratio,
            ttm_sanity_max_annual_ratio=policy.ttm_sanity_max_annual_ratio,
            allow_sec_weighted_average_share_fallback=policy.allow_sec_weighted_average_share_fallback,
            market_share_snapshot=market_share_snapshots.get(company.ticker),
            share_override=share_overrides.get(company.ticker),
        )
        for company in companies
    ]
    apply_scores(rows, policy=policy)
    return rows


def main() -> None:
    configure_utc_logging()
    args = parse_args()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    base_dir = config_path.parent
    db_path = args.db.expanduser().resolve() if args.db else resolve_path(cfg_get(config, "paths.database_path"), base_dir=base_dir)
    output_csv = (
        args.output_csv.expanduser().resolve()
        if args.output_csv
        else resolve_path(
            cfg_get(config, "financial_features.output_csv", "../output/med_devices_reports/med_device_financial_valuation_features.csv"),
            base_dir=base_dir,
        )
    )
    override_raw = str(cfg_get(config, "financial_features.share_count_override_csv", "") or "").strip()
    share_count_overrides_csv = (
        args.share_count_overrides_csv.expanduser().resolve()
        if args.share_count_overrides_csv
        else resolve_path(override_raw, base_dir=base_dir)
        if override_raw
        else None
    )
    policy = financial_feature_policy(config)
    ticker_filter = {normalize_ticker(value) for value in str(args.tickers or "").split(",") if normalize_ticker(value)}

    with connect(db_path, timeout_sec=float(cfg_get(config, "runtime.sqlite_timeout_sec", 30.0))) as conn:
        init_db(conn)
        asof_text = args.asof.strip() if args.asof else latest_market_asof(conn, policy.market_sources, require_adjusted=policy.require_adjusted)
        asof = parse_date(asof_text)
        if asof is None:
            raise ValueError(f"Invalid as-of date: {asof_text}")
        companies = load_companies(
            conn,
            asof=asof,
            ticker_filter=ticker_filter,
            max_tickers=int(args.max_tickers),
            include_historical_members=bool(args.include_historical_members),
        )
        if not companies:
            raise ValueError("No active or point-in-time historical med-device companies selected")
        LOGGER.info(
            "Building financial features: db=%s asof=%s companies=%d market_sources=%s share_count_sources=%s",
            db_path,
            asof.isoformat(),
            len(companies),
            ",".join(policy.market_sources),
            ",".join(policy.share_count_sources),
        )
        share_overrides = load_share_count_overrides(share_count_overrides_csv, asof=asof)
        run_id = start_run(conn, run_type="build_med_device_financial_features", input_path=config_path)
        try:
            rows = build_features(
                conn,
                companies,
                asof=asof,
                policy=policy,
                share_overrides=share_overrides,
            )
            upserted = upsert_feature_rows(conn, rows)
            issue_count = replace_data_quality_issues(conn, rows, asof=asof.isoformat())
            write_csv(output_csv, rows)
            status_counts: dict[str, int] = {}
            bucket_counts: dict[str, int] = {}
            for row in rows:
                status_counts[row.data_quality_status] = status_counts.get(row.data_quality_status, 0) + 1
                bucket_counts[row.calibration_bucket] = bucket_counts.get(row.calibration_bucket, 0) + 1
            message = (
                f"asof={asof.isoformat()} rows={upserted} issues={issue_count} "
                f"statuses={status_counts} buckets={bucket_counts} output={output_csv}"
            )
            finish_run(conn, run_id=run_id, status="success", row_count=upserted, message=message)
            LOGGER.info("Financial features complete: %s", message)
        except BaseException as exc:
            finish_run(conn, run_id=run_id, status="failed", row_count=0, message=f"{type(exc).__name__}: {exc}")
            raise


if __name__ == "__main__":
    raise SystemExit(main())
