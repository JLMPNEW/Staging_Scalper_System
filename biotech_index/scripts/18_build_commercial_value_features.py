#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import logging
import math
import sqlite3
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from biotech_index.core.config import cfg_get, load_yaml, resolve_path  # noqa: E402
from biotech_index.core.db import connect, finish_run, init_db, start_run, utc_now  # noqa: E402
from biotech_index.core.logging_utils import configure_utc_logging  # noqa: E402
from biotech_index.core.market_policy import scoring_market_sources, select_latest_rows_by_source_priority  # noqa: E402
from biotech_index.core.pipeline_guards import (  # noqa: E402
    normalize_ticker,
    read_final_scoring_tickers,
    subset_mode_enabled,
    subset_output_path,
    validate_full_universe_coverage,
    validate_layer_freshness,
    validate_nonempty_selection,
    validate_output_coverage,
    validate_requested_tickers,
)
from biotech_index.core.report_inputs import resolve_dated_report_input_csv  # noqa: E402
from biotech_index.core.scoring_math import score_growth  # noqa: E402


LOGGER = logging.getLogger("build_commercial_value_features")
DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
SQLITE_PARAM_CHUNK_SIZE = 800
QUARTER_PERIODS = {"Q1", "Q2", "Q3", "Q4"}
DEFAULT_UPSIDE_CAPACITY_POINTS = [
    (150_000_000.0, 35.0),
    (550_000_000.0, 55.0),
    (1_750_000_000.0, 80.0),
    (5_500_000_000.0, 84.0),
    (15_800_000_000.0, 74.0),
    (35_400_000_000.0, 58.0),
    (70_700_000_000.0, 40.0),
    (158_100_000_000.0, 28.0),
    (250_000_000_000.0, 16.0),
]
DEFAULT_INSTITUTIONAL_UPSIDE_CAPACITY_POINTS = [
    (150_000_000.0, 40.0),
    (550_000_000.0, 60.0),
    (1_750_000_000.0, 74.0),
    (5_500_000_000.0, 82.0),
    (15_800_000_000.0, 78.0),
    (35_400_000_000.0, 64.0),
    (70_700_000_000.0, 52.0),
    (158_100_000_000.0, 38.0),
    (250_000_000_000.0, 26.0),
]


def chunked(values: list[Any] | tuple[Any, ...], size: int = SQLITE_PARAM_CHUNK_SIZE) -> list[list[Any]]:
    step = max(1, int(size))
    return [list(values[start : start + step]) for start in range(0, len(values), step)]


COMMERCIAL_FIELDS = [
    "asof_date", "company_id", "ticker", "company_name", "latest_period_end",
    "latest_quarter_revenue", "ttm_revenue", "revenue_qoq_growth_pct", "revenue_yoy_growth_pct",
    "gross_profit_ttm", "gross_margin_pct", "operating_income_ttm", "operating_margin_pct",
    "net_income_ttm", "net_margin_pct", "operating_cash_flow_ttm", "free_cash_flow_ttm",
    "rd_expense_ttm", "sgna_expense_ttm", "cash_and_investments", "total_debt", "net_cash",
    "shares_outstanding", "shares_yoy_growth_pct", "close_price", "market_cap", "enterprise_value",
    "avg_dollar_volume_20d", "return_3m_pct", "price_vs_200d_pct", "distance_from_52w_high_pct",
    "relative_strength_3m_vs_xbi",
    "price_to_sales", "ev_to_sales", "pe_ratio", "fcf_yield", "commercial_stage_flag", "profitable_flag",
    "revenue_scale_score", "revenue_growth_score", "margin_score", "profitability_score",
    "balance_sheet_score", "leverage_score", "dilution_score", "valuation_score",
    "quality_adjusted_valuation_score", "upside_capacity_score", "institutional_upside_capacity_score",
    "value_trap_score",
    "commercial_quality_score", "commercial_value_score", "data_quality", "missing_fields",
    "proxy_fields_used", "payload_json",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build SEC-derived commercial value features for biotech scoring.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--asof", type=str, default="", help="Feature date in YYYY-MM-DD. Defaults to UTC today.")
    parser.add_argument("--max-companies", type=int, default=0, help="Smoke-test limit. 0 means all.")
    parser.add_argument("--tickers", type=str, default="", help="Optional comma-separated ticker subset.")
    parser.add_argument("--allow-missing-market", action="store_true", help="Build low-quality rows for companies without market features instead of failing freshness validation.")
    parser.add_argument(
        "--allow-stale-market",
        action="store_true",
        help="For historical validation rebuilds, continue when latest market features are older than the configured freshness window.",
    )
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
    return value if math.isfinite(value) else None


def pct_change(current: float | None, previous: float | None) -> float | None:
    if current is None or previous is None or previous == 0:
        return None
    return (current - previous) / abs(previous)


def clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return low if not math.isfinite(value) else max(low, min(high, value))


def safe_div(num: float | None, den: float | None) -> float | None:
    if num is None or den is None or den == 0:
        return None
    return num / den


def as_bool(raw: object) -> bool:
    return str(raw or "").strip().lower() in {"1", "true", "yes", "y"}


def market_int(raw: object, *, field: str, ticker: object) -> int | None:
    if raw is None:
        return None
    if isinstance(raw, bool):
        return int(raw)
    text = str(raw).strip()
    if not text:
        return None
    lowered = text.lower()
    if lowered in {"true", "yes", "y", "on"}:
        return 1
    if lowered in {"false", "no", "n", "off"}:
        return 0
    value = to_float(raw)
    if value is None:
        LOGGER.warning("Ignoring non-numeric market %s for %s: %r", field, ticker, raw)
        return None
    return int(value)


def parse_market_cap_curve_points(
    raw: object,
    default: list[tuple[float, float]],
    *,
    section: str,
) -> list[tuple[float, float]]:
    if raw in (None, ""):
        return list(default)
    if not isinstance(raw, list):
        raise ValueError(f"{section} must be a list of market_cap/score mappings")
    points: list[tuple[float, float]] = []
    for index, item in enumerate(raw, start=1):
        if not isinstance(item, dict):
            raise ValueError(f"{section}[{index}] must be a mapping")
        market_cap = to_float(item.get("market_cap"))
        score = to_float(item.get("score"))
        if market_cap is None or market_cap <= 0.0 or score is None:
            raise ValueError(f"{section}[{index}] requires positive market_cap and finite score")
        points.append((market_cap, clamp(score)))
    if len(points) < 2:
        raise ValueError(f"{section} requires at least two points")
    return sorted(points)


def log_linear_market_cap_score(
    market_cap: float | None,
    points: list[tuple[float, float]],
    *,
    default_score: float = 50.0,
) -> float:
    if market_cap is None or market_cap <= 0.0:
        return default_score
    ordered = sorted(points)
    if market_cap <= ordered[0][0]:
        return clamp(ordered[0][1])
    if market_cap >= ordered[-1][0]:
        return clamp(ordered[-1][1])
    log_cap = math.log10(market_cap)
    for (left_cap, left_score), (right_cap, right_score) in zip(ordered, ordered[1:]):
        if left_cap <= market_cap <= right_cap:
            left_log = math.log10(left_cap)
            right_log = math.log10(right_cap)
            span = right_log - left_log
            if abs(span) <= 1e-12:
                return clamp(right_score)
            ratio = (log_cap - left_log) / span
            return clamp(left_score + ratio * (right_score - left_score))
    return clamp(ordered[-1][1])


def read_scoring_tickers(path: Path) -> set[str]:
    return read_final_scoring_tickers(path)


def universe_has_delisted_calibration_rows(path: Path) -> bool:
    if not path.exists():
        return False
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            if (
                str(row.get("calibration_only") or "").strip().lower() in {"1", "true", "yes"}
                or str(row.get("universe_status") or "").strip().lower() == "delisted_calibration"
                or str(row.get("historical_universe_source") or "").strip().lower()
                == "delisted_biotech_calibration_universe"
            ):
                return True
    return False


def load_companies(conn: sqlite3.Connection, *, scoring_tickers: set[str], ticker_filter: set[str], max_companies: int) -> list[dict[str, Any]]:
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


def load_fact_rows(conn: sqlite3.Connection, company_id: int, asof_date: date) -> list[dict[str, Any]]:
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
    return [dict(row) for row in rows]


def load_fact_rows_bulk(conn: sqlite3.Connection, company_ids: list[int], asof_date: date) -> dict[int, list[dict[str, Any]]]:
    if not company_ids:
        return {}
    if len(company_ids) > SQLITE_PARAM_CHUNK_SIZE:
        out: dict[int, list[dict[str, Any]]] = {int(company_id): [] for company_id in company_ids}
        for company_chunk in chunked(company_ids):
            out.update(load_fact_rows_bulk(conn, [int(value) for value in company_chunk], asof_date))
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


def load_latest_market_bulk(
    conn: sqlite3.Connection,
    company_ids: list[int],
    asof_date: date,
    *,
    source_priority: list[str],
    max_staleness_days: int,
) -> dict[int, dict[str, Any]]:
    if not company_ids:
        return {}
    if len(company_ids) > SQLITE_PARAM_CHUNK_SIZE:
        out: dict[int, dict[str, Any]] = {}
        for company_chunk in chunked(company_ids):
            out.update(
                load_latest_market_bulk(
                    conn,
                    [int(value) for value in company_chunk],
                    asof_date,
                    source_priority=source_priority,
                    max_staleness_days=max_staleness_days,
                )
            )
        return out
    placeholders = ",".join("?" for _ in company_ids)
    source_clause = ""
    params: list[Any] = [*company_ids, asof_date.isoformat()]
    if source_priority:
        source_clause = " AND source IN (" + ",".join("?" for _ in source_priority) + ")"
        params.extend(source_priority)
    rows = conn.execute(
        f"""
        SELECT f.*
        FROM market_features_daily f
        JOIN (
            SELECT company_id, source, MAX(asof_date) AS max_asof
            FROM market_features_daily
            WHERE company_id IN ({placeholders}) AND asof_date <= ?{source_clause}
            GROUP BY company_id, source
        ) latest
          ON latest.company_id = f.company_id
         AND latest.source = f.source
         AND latest.max_asof = f.asof_date
        """,
        tuple(params),
    ).fetchall()
    return select_latest_rows_by_source_priority(
        rows,
        asof_date=asof_date,
        source_priority=source_priority,
        max_staleness_days=max_staleness_days,
    )


def latest_nonnull(rows: list[dict[str, Any]], field: str) -> tuple[float | None, dict[str, Any] | None]:
    for row in rows:
        value = to_float(row.get(field))
        if value is not None:
            return value, row
    return None, None


def amount_for_period(row: dict[str, Any], field: str, proxies: list[str]) -> float | None:
    value = to_float(row.get(field))
    if value is None:
        return None
    if str(row.get("fiscal_period") or "").upper() == "FY":
        proxies.append(f"quarterly_{field}_from_annual_div4")
        return value / 4.0
    return value


def ttm_amount(
    rows: list[dict[str, Any]],
    field: str,
    proxies: list[str],
    *,
    asof_date: date | None = None,
    max_fy_age_days: int = 550,
) -> float | None:
    quarterly: list[float] = []
    for row in rows:
        value = to_float(row.get(field))
        if value is None:
            continue
        fp = str(row.get("fiscal_period") or "").upper()
        if fp in QUARTER_PERIODS:
            quarterly.append(value)
        if len(quarterly) >= 4:
            return sum(quarterly[:4])
    if len(quarterly) >= 2:
        proxies.append(f"partial_quarter_annualized_{field}")
        return sum(quarterly) / len(quarterly) * 4.0
    for row in rows:
        if str(row.get("fiscal_period") or "").upper() == "FY":
            # Staleness bound (mirrors script 16): reject FY fallback rows whose
            # period_end is older than max_fy_age_days before asof.
            period_end = parse_date(row.get("period_end"))
            if asof_date is not None and period_end is not None:
                age_days = (asof_date - period_end).days
                if age_days < 0 or age_days > max_fy_age_days:
                    continue
            value = to_float(row.get(field))
            if value is not None:
                return value
    return None


def closest_period_amount(rows: list[dict[str, Any]], field: str, target_date: date, min_days: int, max_days: int, proxies: list[str]) -> float | None:
    best: tuple[int, float] | None = None
    for row in rows:
        period_end = parse_date(row.get("period_end"))
        if period_end is None:
            continue
        age = (target_date - period_end).days
        if min_days <= age <= max_days:
            value = amount_for_period(row, field, proxies)
            if value is not None and (best is None or age < best[0]):
                best = (age, value)
    return best[1] if best else None


def score_revenue_scale(ttm_revenue: float | None) -> float:
    if ttm_revenue is None or ttm_revenue <= 0:
        return 10.0
    # 10M revenue ~= 25, 100M ~= 50, 1B ~= 75, 10B caps near 100.
    return clamp(25.0 + (math.log10(max(ttm_revenue, 1.0)) - 7.0) * 25.0)


def score_margin(gross_margin: float | None) -> float:
    if gross_margin is None:
        return 50.0
    if gross_margin < 0:
        return 10.0
    return clamp(25.0 + gross_margin * 80.0)


def score_profitability(ttm_revenue: float | None, op_margin: float | None, net_margin: float | None, fcf: float | None) -> float:
    if ttm_revenue is None or ttm_revenue <= 0:
        return 15.0
    score = 35.0
    if op_margin is not None:
        score += clamp(op_margin * 100.0, -30.0, 30.0)
    if net_margin is not None:
        score += clamp(net_margin * 100.0, -30.0, 30.0)
    if fcf is not None and fcf > 0:
        score += 15.0
    return clamp(score)


def score_balance(cash: float | None, debt: float | None, net_cash: float | None, ttm_revenue: float | None) -> float:
    if cash is None:
        return 35.0
    debt = debt or 0.0
    score = 65.0
    if net_cash is not None and net_cash > 0:
        score += 15.0
    if debt > cash:
        score -= 25.0
    if ttm_revenue and cash / max(ttm_revenue, 1.0) > 0.5:
        score += 10.0
    return clamp(score)


def score_leverage(
    cash: float | None,
    debt: float | None,
    operating_income_ttm: float | None,
    free_cash_flow_ttm: float | None,
    ttm_revenue: float | None,
) -> float:
    """Higher is better: debt is rewarded only when supported by cash flow."""
    cash = cash or 0.0
    debt = debt or 0.0
    if debt <= 0.0:
        return 92.0 if cash > 0.0 else 78.0

    net_debt = max(0.0, debt - cash)
    score = 72.0
    if ttm_revenue and ttm_revenue > 0.0:
        debt_to_sales = debt / max(ttm_revenue, 1.0)
        if debt_to_sales > 4.0:
            score -= 34.0
        elif debt_to_sales > 2.5:
            score -= 24.0
        elif debt_to_sales > 1.25:
            score -= 12.0

    if free_cash_flow_ttm and free_cash_flow_ttm > 0.0:
        net_debt_to_fcf = net_debt / max(free_cash_flow_ttm, 1.0)
        if net_debt_to_fcf < 2.0:
            score += 16.0
        elif net_debt_to_fcf < 4.0:
            score += 6.0
        elif net_debt_to_fcf > 8.0:
            score -= 24.0
    elif operating_income_ttm and operating_income_ttm > 0.0:
        net_debt_to_operating_income = net_debt / max(operating_income_ttm, 1.0)
        if net_debt_to_operating_income < 3.0:
            score += 8.0
        elif net_debt_to_operating_income > 7.0:
            score -= 18.0
    else:
        score -= 24.0

    if cash / max(debt, 1.0) > 0.50:
        score += 8.0
    return clamp(score)


def score_dilution(shares_yoy_growth: float | None) -> float:
    if shares_yoy_growth is None:
        return 55.0
    if shares_yoy_growth <= 0.02:
        return 95.0
    if shares_yoy_growth <= 0.10:
        return 80.0
    if shares_yoy_growth <= 0.25:
        return 55.0
    if shares_yoy_growth <= 0.50:
        return 30.0
    return 10.0


def score_valuation(
    enterprise_value: float | None,
    operating_income_ttm: float | None,
    ev_to_sales: float | None,
    price_to_sales: float | None,
    pe_ratio: float | None,
    fcf_yield: float | None,
) -> float:
    scores: list[float] = []
    if (
        enterprise_value is not None
        and enterprise_value > 0.0
        and operating_income_ttm is not None
        and operating_income_ttm > 0.0
    ):
        ev_to_operating_income = enterprise_value / operating_income_ttm
        if ev_to_operating_income < 8.0:
            scores.append(95.0)
        elif ev_to_operating_income < 12.0:
            scores.append(82.0)
        elif ev_to_operating_income < 18.0:
            scores.append(68.0)
        elif ev_to_operating_income < 28.0:
            scores.append(48.0)
        else:
            scores.append(28.0)
    if pe_ratio is not None and pe_ratio > 0:
        if pe_ratio < 10:
            scores.append(92.0)
        elif pe_ratio < 16:
            scores.append(82.0)
        elif pe_ratio < 24:
            scores.append(64.0)
        elif pe_ratio < 35:
            scores.append(45.0)
        else:
            scores.append(25.0)
    multiple = ev_to_sales if ev_to_sales is not None and ev_to_sales > 0 else price_to_sales
    if multiple is not None and multiple > 0:
        if multiple < 1.0:
            scores.append(88.0)
        elif multiple < 2.0:
            scores.append(78.0)
        elif multiple < 4.0:
            scores.append(64.0)
        elif multiple < 7.0:
            scores.append(48.0)
        elif multiple < 12.0:
            scores.append(34.0)
        else:
            scores.append(18.0)
    score = sum(scores) / len(scores) if scores else 50.0
    if fcf_yield is not None:
        if fcf_yield > 0.08:
            score += 10.0
        elif fcf_yield < -0.10:
            score -= 10.0
    return clamp(score)


def score_value_trap(
    revenue_yoy_growth: float | None,
    gross_margin: float | None,
    operating_margin: float | None,
    net_margin: float | None,
    fcf_yield: float | None,
    cash: float | None,
    debt: float | None,
    pe_ratio: float | None,
    ev_to_sales: float | None,
) -> float:
    """Higher means low valuation is more likely tied to deteriorating fundamentals."""
    score = 0.0
    if revenue_yoy_growth is not None:
        if revenue_yoy_growth <= -0.30:
            score += 45.0
        elif revenue_yoy_growth <= -0.10:
            score += 28.0
        elif revenue_yoy_growth < 0.0:
            score += 16.0
    if gross_margin is not None and gross_margin < 0.35:
        score += 10.0
    if operating_margin is not None:
        if operating_margin < 0.0:
            score += 18.0
        elif operating_margin < 0.05:
            score += 8.0
    if net_margin is not None and net_margin < 0.0:
        score += 12.0
    if fcf_yield is not None and fcf_yield < -0.05:
        score += 14.0
    if (debt or 0.0) > max(cash or 0.0, 1.0) * 3.0 and (fcf_yield is None or fcf_yield <= 0.0):
        score += 16.0
    if pe_ratio is not None and 0.0 < pe_ratio < 9.0 and revenue_yoy_growth is not None and revenue_yoy_growth < 0.0:
        score += 12.0
    if ev_to_sales is not None and 0.0 < ev_to_sales < 2.0 and revenue_yoy_growth is not None and revenue_yoy_growth < 0.0:
        score += 8.0
    return clamp(score)


def score_upside_capacity(market_cap: float | None, config: dict[str, Any] | None = None) -> float:
    if market_cap is None or market_cap <= 0:
        return 50.0
    curve_cfg = cfg_get(config or {}, "commercial_value.upside_capacity_curve", {}) or {}
    if as_bool(curve_cfg.get("enabled")):
        curve = str(curve_cfg.get("curve") or "log_linear").strip().lower().replace("-", "_")
        if curve != "log_linear":
            raise ValueError(f"Unsupported commercial_value.upside_capacity_curve.curve: {curve}")
        points = parse_market_cap_curve_points(
            curve_cfg.get("points"),
            DEFAULT_UPSIDE_CAPACITY_POINTS,
            section="commercial_value.upside_capacity_curve.points",
        )
        return log_linear_market_cap_score(market_cap, points, default_score=50.0)
    # Tier-1 investability sweet spot: enough upside to rerate, but
    # not so small that liquidity/survival lottery effects dominate.
    if market_cap < 300_000_000:
        return 35.0
    if market_cap < 1_000_000_000:
        return 55.0
    if market_cap < 3_000_000_000:
        return 80.0
    if market_cap < 10_000_000_000:
        return 84.0
    if market_cap < 25_000_000_000:
        return 74.0
    if market_cap < 50_000_000_000:
        return 58.0
    if market_cap < 100_000_000_000:
        return 40.0
    if market_cap < 250_000_000_000:
        return 28.0
    return 16.0


def score_institutional_upside_capacity(market_cap: float | None, config: dict[str, Any] | None = None) -> float:
    """Upside score with less lottery-ticket bias than raw market-cap size."""
    if market_cap is None or market_cap <= 0:
        return 50.0
    curve_cfg = cfg_get(config or {}, "commercial_value.institutional_upside_capacity_curve", {}) or {}
    if as_bool(curve_cfg.get("enabled")):
        curve = str(curve_cfg.get("curve") or "log_linear").strip().lower().replace("-", "_")
        if curve != "log_linear":
            raise ValueError(f"Unsupported commercial_value.institutional_upside_capacity_curve.curve: {curve}")
        points = parse_market_cap_curve_points(
            curve_cfg.get("points"),
            DEFAULT_INSTITUTIONAL_UPSIDE_CAPACITY_POINTS,
            section="commercial_value.institutional_upside_capacity_curve.points",
        )
        return log_linear_market_cap_score(market_cap, points, default_score=50.0)
    if market_cap < 300_000_000:
        return 40.0
    if market_cap < 1_000_000_000:
        return 60.0
    if market_cap < 3_000_000_000:
        return 74.0
    if market_cap < 10_000_000_000:
        return 82.0
    if market_cap < 25_000_000_000:
        return 78.0
    if market_cap < 50_000_000_000:
        return 64.0
    if market_cap < 100_000_000_000:
        return 52.0
    if market_cap < 250_000_000_000:
        return 38.0
    return 26.0


def validate_weight_sum(section: str, weights: dict[str, Any], keys: list[str], *, expected: float = 1.0) -> None:
    total = sum(float(weights.get(key, 0.0)) for key in keys)
    if abs(total - expected) > 1e-6:
        raise ValueError(
            f"{section} additive weights sum to {total:.6f}, expected {expected:.6f}; "
            "fix config before building commercial value features."
        )


def validate_commercial_value_config(config: dict[str, Any]) -> None:
    quality_weights = cfg_get(config, "commercial_value.quality_weights", {}) or {}
    value_weights = cfg_get(config, "commercial_value.value_weights", {}) or {}
    validate_weight_sum(
        "commercial_value.quality_weights",
        quality_weights,
        ["revenue_scale", "revenue_growth", "margin", "profitability", "balance_sheet", "leverage", "dilution"],
    )
    validate_weight_sum(
        "commercial_value.value_weights",
        value_weights,
        ["commercial_quality", "valuation", "upside_capacity", "commercial_stage"],
    )


def build_feature(company: dict[str, Any], rows: list[dict[str, Any]], market: dict[str, Any] | None, asof_date: date, config: dict[str, Any]) -> dict[str, Any]:
    missing: list[str] = []
    proxies: list[str] = []
    latest_revenue, latest_revenue_row = latest_nonnull(rows, "revenue")
    latest_quarter_revenue = amount_for_period(latest_revenue_row, "revenue", proxies) if latest_revenue_row else None
    latest_period_end = str(latest_revenue_row.get("period_end") if latest_revenue_row else rows[0].get("period_end") if rows else "")
    latest_period_date = parse_date(latest_period_end)
    if latest_period_date is None:
        latest_period_date = asof_date

    ttm_revenue = ttm_amount(rows, "revenue", proxies, asof_date=asof_date)
    gross_profit_ttm = ttm_amount(rows, "gross_profit", proxies, asof_date=asof_date)
    operating_income_ttm = ttm_amount(rows, "operating_income", proxies, asof_date=asof_date)
    net_income_ttm = ttm_amount(rows, "net_income", proxies, asof_date=asof_date)
    operating_cash_flow_ttm = ttm_amount(rows, "operating_cash_flow", proxies, asof_date=asof_date)
    free_cash_flow_ttm = ttm_amount(rows, "free_cash_flow", proxies, asof_date=asof_date)
    rd_expense_ttm = ttm_amount(rows, "rd_expense", proxies, asof_date=asof_date)
    sgna_expense_ttm = ttm_amount(rows, "sgna_expense", proxies, asof_date=asof_date)
    partial_ttm_proxy_fields = sorted(
        {
            proxy[len("partial_quarter_annualized_") :]
            for proxy in proxies
            if proxy.startswith("partial_quarter_annualized_")
        }
    )
    partial_ttm_proxy_flag = bool(partial_ttm_proxy_fields)

    prev_quarter_revenue = closest_period_amount(rows, "revenue", latest_period_date or asof_date, 60, 150, proxies)
    prior_year_revenue = closest_period_amount(rows, "revenue", latest_period_date or asof_date, 270, 455, proxies)
    revenue_qoq_growth = pct_change(latest_quarter_revenue, prev_quarter_revenue)
    revenue_yoy_growth = pct_change(latest_quarter_revenue, prior_year_revenue)

    cash, _ = latest_nonnull(rows, "cash_and_investments")
    debt, _ = latest_nonnull(rows, "total_debt")
    shares, shares_row = latest_nonnull(rows, "shares_outstanding")
    shares_prior = closest_period_amount(rows, "shares_outstanding", latest_period_date or asof_date, 270, 455, proxies)
    shares_yoy_growth = pct_change(shares, shares_prior)

    close_price = to_float(market.get("close_price") if market else None)
    market_cap = to_float(market.get("market_cap") if market else None)
    avg_dollar_volume_20d = to_float(market.get("avg_dollar_volume_20d") if market else None)
    return_3m_pct = to_float(market.get("return_3m_pct") if market else None)
    price_vs_200d_pct = to_float(market.get("price_vs_200d_pct") if market else None)
    distance_from_52w_high_pct = to_float(market.get("distance_from_52w_high_pct") if market else None)
    relative_strength_3m_vs_xbi = to_float(market.get("relative_strength_3m_vs_xbi") if market else None)
    if market_cap is None and close_price is not None and shares is not None and shares > 0:
        market_cap = close_price * shares
        proxies.append("market_cap_from_ib_price_x_sec_shares")
    enterprise_value = (
        market_cap + (debt or 0.0) - (cash or 0.0)
        if market_cap is not None and (cash is not None or debt is not None)
        else None
    )
    net_cash = (cash or 0.0) - (debt or 0.0) if cash is not None or debt is not None else None

    gross_margin = safe_div(gross_profit_ttm, ttm_revenue)
    operating_margin = safe_div(operating_income_ttm, ttm_revenue)
    net_margin = safe_div(net_income_ttm, ttm_revenue)
    price_to_sales = safe_div(market_cap, ttm_revenue)
    ev_to_sales = safe_div(enterprise_value, ttm_revenue)
    pe_ratio = safe_div(market_cap, net_income_ttm) if net_income_ttm is not None and net_income_ttm > 0.0 else None
    fcf_yield = safe_div(free_cash_flow_ttm, market_cap)

    commercial_stage_flag = bool(ttm_revenue is not None and ttm_revenue >= float(cfg_get(config, "commercial_value.commercial_stage_revenue_min", 50_000_000)))
    profitable_flag = bool((net_income_ttm is not None and net_income_ttm > 0) or (free_cash_flow_ttm is not None and free_cash_flow_ttm > 0))

    if ttm_revenue is None:
        missing.append("ttm_revenue")
    if gross_profit_ttm is None:
        missing.append("gross_profit_ttm")
    if operating_income_ttm is None:
        missing.append("operating_income_ttm")
    if net_income_ttm is None:
        missing.append("net_income_ttm")
    if rd_expense_ttm is None:
        missing.append("rd_expense_ttm")
    if sgna_expense_ttm is None:
        missing.append("sgna_expense_ttm")
    if operating_cash_flow_ttm is None:
        missing.append("operating_cash_flow_ttm")
    if free_cash_flow_ttm is None:
        missing.append("free_cash_flow_ttm")
    if cash is None:
        missing.append("cash_and_investments")
    if shares is None:
        missing.append("shares_outstanding")
    if market_cap is None:
        missing.append("market_cap")

    revenue_scale_score = score_revenue_scale(ttm_revenue)
    growth_curve = str(cfg_get(config, "commercial_value.growth_curve", "legacy") or "legacy")
    revenue_growth_score = score_growth(revenue_yoy_growth, curve=growth_curve)
    margin_score = score_margin(gross_margin)
    profitability_score = score_profitability(ttm_revenue, operating_margin, net_margin, free_cash_flow_ttm)
    balance_sheet_score = score_balance(cash, debt, net_cash, ttm_revenue)
    leverage_score = score_leverage(cash, debt, operating_income_ttm, free_cash_flow_ttm, ttm_revenue)
    dilution_score = score_dilution(shares_yoy_growth)
    valuation_score = score_valuation(enterprise_value, operating_income_ttm, ev_to_sales, price_to_sales, pe_ratio, fcf_yield)
    value_trap_score = score_value_trap(
        revenue_yoy_growth,
        gross_margin,
        operating_margin,
        net_margin,
        fcf_yield,
        cash,
        debt,
        pe_ratio,
        ev_to_sales,
    )
    quality_adjusted_cfg = cfg_get(config, "commercial_value.quality_adjusted_valuation", {}) or {}
    value_trap_valuation_discount = clamp(
        float(quality_adjusted_cfg.get("value_trap_discount", 0.45)),
        0.0,
        1.0,
    )
    quality_adjusted_valuation_score = clamp(
        valuation_score * max(0.0, 1.0 - value_trap_valuation_discount * value_trap_score / 100.0)
    )
    upside_capacity_score = score_upside_capacity(market_cap, config)
    institutional_upside_capacity_score = score_institutional_upside_capacity(market_cap, config)
    commercial_stage_scores = cfg_get(config, "commercial_value.commercial_stage_scores", {}) or {}
    commercial_stage_score = (
        float(commercial_stage_scores.get("commercial", 80.0))
        if commercial_stage_flag
        else float(commercial_stage_scores.get("revenue_positive", 45.0))
        if ttm_revenue and ttm_revenue > 0
        else float(commercial_stage_scores.get("pre_revenue", 15.0))
    )

    quality_weights = cfg_get(config, "commercial_value.quality_weights", {}) or {}
    commercial_quality_score = clamp(
        float(quality_weights.get("revenue_scale", 0.08)) * revenue_scale_score
        + float(quality_weights.get("revenue_growth", 0.22)) * revenue_growth_score
        + float(quality_weights.get("margin", 0.15)) * margin_score
        + float(quality_weights.get("profitability", 0.22)) * profitability_score
        + float(quality_weights.get("balance_sheet", 0.10)) * balance_sheet_score
        + float(quality_weights.get("leverage", 0.10)) * leverage_score
        + float(quality_weights.get("dilution", 0.13)) * dilution_score
    )
    value_weights = cfg_get(config, "commercial_value.value_weights", {}) or {}
    penalty_weights = cfg_get(config, "commercial_value.penalty_weights", {}) or {}
    value_trap_drag = max(
        0.0,
        min(1.0, float(penalty_weights.get("value_trap_drag", value_weights.get("value_trap_drag", 0.22)))),
    )
    # Leverage is additive quality because supported debt can be durable; value-trap risk is a post-score drag
    # because cheapness paired with deteriorating fundamentals should not raise the commercial value score.
    commercial_value_score = clamp(
        float(value_weights.get("commercial_quality", 0.58)) * commercial_quality_score
        + float(value_weights.get("valuation", 0.18)) * quality_adjusted_valuation_score
        + float(value_weights.get("upside_capacity", 0.04)) * upside_capacity_score
        + float(value_weights.get("commercial_stage", 0.20)) * commercial_stage_score
        - value_trap_drag * value_trap_score
    )

    if len(missing) <= 1:
        data_quality = "high"
    elif ttm_revenue is not None and cash is not None:
        data_quality = "medium"
    else:
        data_quality = "low"
    if partial_ttm_proxy_flag and data_quality == "high":
        data_quality = "medium"

    payload = {
        "source": "sec_companyfacts_primary_market_optional",
        "market_source": str(market.get("source") if market else ""),
        "market_price_adjustment": str(market.get("price_adjustment") if market else ""),
        "market_is_adjusted": market_int(market.get("is_adjusted") if market else None, field="is_adjusted", ticker=company["ticker"]),
        "market_asof_date": str(market.get("asof_date") if market else ""),
        "market_last_bar_date": str(market.get("last_bar_date") if market else ""),
        "market_bar_count": market_int(market.get("bar_count") if market else None, field="bar_count", ticker=company["ticker"]),
        "market_data_quality": str(market.get("market_data_quality") if market else ""),
        "growth_curve": growth_curve,
        "latest_shares_period_end": str(shares_row.get("period_end") if shares_row else ""),
        "partial_ttm_proxy_flag": int(partial_ttm_proxy_flag),
        "partial_ttm_proxy_fields": partial_ttm_proxy_fields,
        "component_scores": {
            "revenue_scale_score": round(revenue_scale_score, 4),
            "revenue_growth_score": round(revenue_growth_score, 4),
            "margin_score": round(margin_score, 4),
            "profitability_score": round(profitability_score, 4),
            "balance_sheet_score": round(balance_sheet_score, 4),
            "leverage_score": round(leverage_score, 4),
            "dilution_score": round(dilution_score, 4),
            "valuation_score": round(valuation_score, 4),
            "quality_adjusted_valuation_score": round(quality_adjusted_valuation_score, 4),
            "upside_capacity_score": round(upside_capacity_score, 4),
            "institutional_upside_capacity_score": round(institutional_upside_capacity_score, 4),
            "value_trap_score": round(value_trap_score, 4),
        },
        "entry_context": {
            "avg_dollar_volume_20d": avg_dollar_volume_20d,
            "return_3m_pct": return_3m_pct,
            "price_vs_200d_pct": price_vs_200d_pct,
            "distance_from_52w_high_pct": distance_from_52w_high_pct,
            "relative_strength_3m_vs_xbi": relative_strength_3m_vs_xbi,
        },
    }
    return {
        "asof_date": asof_date.isoformat(),
        "company_id": int(company["company_id"]),
        "ticker": str(company["ticker"]),
        "company_name": str(company["company_name"] or ""),
        "latest_period_end": latest_period_end,
        "latest_quarter_revenue": latest_quarter_revenue,
        "ttm_revenue": ttm_revenue,
        "revenue_qoq_growth_pct": revenue_qoq_growth,
        "revenue_yoy_growth_pct": revenue_yoy_growth,
        "gross_profit_ttm": gross_profit_ttm,
        "gross_margin_pct": gross_margin,
        "operating_income_ttm": operating_income_ttm,
        "operating_margin_pct": operating_margin,
        "net_income_ttm": net_income_ttm,
        "net_margin_pct": net_margin,
        "operating_cash_flow_ttm": operating_cash_flow_ttm,
        "free_cash_flow_ttm": free_cash_flow_ttm,
        "rd_expense_ttm": rd_expense_ttm,
        "sgna_expense_ttm": sgna_expense_ttm,
        "cash_and_investments": cash,
        "total_debt": debt,
        "net_cash": net_cash,
        "shares_outstanding": shares,
        "shares_yoy_growth_pct": shares_yoy_growth,
        "close_price": close_price,
        "market_cap": market_cap,
        "enterprise_value": enterprise_value,
        "avg_dollar_volume_20d": avg_dollar_volume_20d,
        "return_3m_pct": return_3m_pct,
        "price_vs_200d_pct": price_vs_200d_pct,
        "distance_from_52w_high_pct": distance_from_52w_high_pct,
        "relative_strength_3m_vs_xbi": relative_strength_3m_vs_xbi,
        "price_to_sales": price_to_sales,
        "ev_to_sales": ev_to_sales,
        "pe_ratio": pe_ratio,
        "fcf_yield": fcf_yield,
        "commercial_stage_flag": int(commercial_stage_flag),
        "profitable_flag": int(profitable_flag),
        "revenue_scale_score": round(revenue_scale_score, 4),
        "revenue_growth_score": round(revenue_growth_score, 4),
        "margin_score": round(margin_score, 4),
        "profitability_score": round(profitability_score, 4),
        "balance_sheet_score": round(balance_sheet_score, 4),
        "leverage_score": round(leverage_score, 4),
        "dilution_score": round(dilution_score, 4),
        "valuation_score": round(valuation_score, 4),
        "quality_adjusted_valuation_score": round(quality_adjusted_valuation_score, 4),
        "upside_capacity_score": round(upside_capacity_score, 4),
        "institutional_upside_capacity_score": round(institutional_upside_capacity_score, 4),
        "value_trap_score": round(value_trap_score, 4),
        "commercial_quality_score": round(commercial_quality_score, 4),
        "commercial_value_score": round(commercial_value_score, 4),
        "data_quality": data_quality,
        "missing_fields": ";".join(missing),
        "proxy_fields_used": ";".join(dict.fromkeys(proxies)),
        "payload_json": json.dumps(payload, ensure_ascii=True, sort_keys=True),
    }


def upsert_features(
    conn: sqlite3.Connection,
    rows: list[dict[str, Any]],
    asof_date: str,
    *,
    target_company_ids: set[int] | None = None,
) -> None:
    now = utc_now()
    with conn:
        if target_company_ids is None:
            conn.execute("DELETE FROM commercial_value_features_daily WHERE asof_date = ?", (asof_date,))
        elif target_company_ids:
            for company_chunk in chunked(sorted(target_company_ids)):
                company_placeholders = ",".join("?" for _ in company_chunk)
                conn.execute(
                    f"DELETE FROM commercial_value_features_daily WHERE asof_date = ? AND company_id IN ({company_placeholders})",
                    (asof_date, *company_chunk),
                )
        else:
            return
        conn.executemany(
            f"""
            INSERT INTO commercial_value_features_daily({", ".join(COMMERCIAL_FIELDS)}, created_at, updated_at)
            VALUES ({", ".join("?" for _ in COMMERCIAL_FIELDS)}, ?, ?)
            """,
            [tuple(row.get(field) for field in COMMERCIAL_FIELDS) + (now, now) for row in rows],
        )


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=COMMERCIAL_FIELDS, lineterminator="\n")
        writer.writeheader()
        writer.writerows([{field: row.get(field, "") for field in COMMERCIAL_FIELDS} for row in rows])


def main() -> None:
    configure_logging()
    args = parse_args()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    validate_commercial_value_config(config)
    base_dir = config_path.parent
    db_path = args.db.expanduser().resolve() if args.db else resolve_path(cfg_get(config, "paths.database_path"), base_dir=base_dir)
    output_csv = resolve_path(cfg_get(config, "commercial_value.output_csv"), base_dir=base_dir)
    configured_final_universe_csv = resolve_path(cfg_get(config, "commercial_value.final_scoring_universe_csv"), base_dir=base_dir)
    sqlite_timeout_sec = float(cfg_get(config, "runtime.sqlite_timeout_sec", 30.0))
    asof_date = parse_date(args.asof) if args.asof else datetime.now(timezone.utc).date()
    if asof_date is None:
        raise ValueError(f"Invalid --asof date: {args.asof}")
    final_universe_csv = resolve_dated_report_input_csv(
        configured_final_universe_csv,
        base_output_dir=resolve_path(cfg_get(config, "biotech_scoring.output_dir", "../output/biotech_index_reports"), base_dir=base_dir),
        asof_date=asof_date.isoformat(),
        logger=LOGGER,
    )
    ticker_filter = {normalize_ticker(value) for value in args.tickers.split(",") if normalize_ticker(value)}

    with connect(db_path, timeout_sec=sqlite_timeout_sec) as conn:
        run_id: int | None = None
        try:
            init_db(conn)
            run_id = start_run(conn, run_type="build_commercial_value_features", input_path=db_path)
            scoring_tickers = read_scoring_tickers(final_universe_csv)
            companies = load_companies(conn, scoring_tickers=scoring_tickers, ticker_filter=ticker_filter, max_companies=args.max_companies)
            subset_mode = subset_mode_enabled(ticker_filter=ticker_filter, max_count=int(args.max_companies))
            output_csv = subset_output_path(output_csv, subset_mode=subset_mode)
            validate_nonempty_selection(count=len(companies), context="commercial value feature build", subset_mode=subset_mode)
            loaded_tickers = [str(company["ticker"]) for company in companies]
            validate_requested_tickers(requested_tickers=ticker_filter, loaded_tickers=loaded_tickers, context="commercial value feature build")
            validate_full_universe_coverage(
                expected_tickers=scoring_tickers,
                observed_tickers=loaded_tickers,
                context="commercial value feature build",
                subset_mode=subset_mode,
            )
            company_ids = [int(company["company_id"]) for company in companies]
            fact_rows_by_company = load_fact_rows_bulk(conn, company_ids, asof_date)
            market_source_priority = scoring_market_sources(config)
            if universe_has_delisted_calibration_rows(final_universe_csv) and "norgate_us_equities_total_return" not in market_source_priority:
                market_source_priority = [*market_source_priority, "norgate_us_equities_total_return"]
            max_market_staleness_days = int(cfg_get(config, "biotech_refresh.max_upstream_staleness_days", 2))
            market_by_company = load_latest_market_bulk(
                conn,
                company_ids,
                asof_date,
                source_priority=market_source_priority,
                max_staleness_days=max_market_staleness_days,
            )
            LOGGER.info("Commercial value market source priority: %s", ",".join(market_source_priority))
            freshness_base_rows = companies
            if args.allow_missing_market:
                market_company_ids = set(market_by_company)
                missing_market_tickers = [
                    str(company.get("ticker") or "")
                    for company in companies
                    if int(company["company_id"]) not in market_company_ids
                ]
                freshness_base_rows = [
                    company for company in companies if int(company["company_id"]) in market_company_ids
                ]
                if missing_market_tickers:
                    LOGGER.warning(
                        "Commercial value feature build continuing without market rows for %d ticker(s): %s",
                        len(missing_market_tickers),
                        ",".join(sorted(missing_market_tickers)[:25]) + (f"...(+{len(missing_market_tickers) - 25})" if len(missing_market_tickers) > 25 else ""),
                    )
            if args.allow_stale_market:
                fresh_rows: list[dict[str, Any]] = []
                stale_market_tickers: list[str] = []
                for company in freshness_base_rows:
                    company_id = int(company["company_id"])
                    market_row = market_by_company.get(company_id)
                    market_asof = parse_date(market_row.get("asof_date") if market_row else None)
                    age_days = (asof_date - market_asof).days if market_asof is not None else 999_999
                    if age_days > max_market_staleness_days:
                        stale_market_tickers.append(str(company.get("ticker") or ""))
                    else:
                        fresh_rows.append(company)
                freshness_base_rows = fresh_rows
                if stale_market_tickers:
                    LOGGER.warning(
                        "Commercial value feature build continuing with stale market rows for %d ticker(s): %s",
                        len(stale_market_tickers),
                        ",".join(sorted(stale_market_tickers)[:25]) + (f"...(+{len(stale_market_tickers) - 25})" if len(stale_market_tickers) > 25 else ""),
                    )
            validate_layer_freshness(
                base_rows=freshness_base_rows,
                layer_rows_by_company=market_by_company,
                asof_date=asof_date,
                context="commercial value feature build market_features_daily",
                max_staleness_days=max_market_staleness_days,
            )
            rows: list[dict[str, Any]] = []
            for idx, company in enumerate(companies, start=1):
                company_id = int(company["company_id"])
                fact_rows = fact_rows_by_company.get(company_id, [])
                market = market_by_company.get(company_id)
                rows.append(build_feature(company, fact_rows, market, asof_date, config))
                if idx % 50 == 0:
                    LOGGER.info("Built commercial features for %d/%d companies", idx, len(companies))
            partial_run = bool(ticker_filter) or int(args.max_companies) > 0
            validate_output_coverage(
                expected_tickers=scoring_tickers,
                output_tickers=[row["ticker"] for row in rows],
                context="commercial value feature build",
                subset_mode=subset_mode,
            )
            upsert_features(
                conn,
                rows,
                asof_date.isoformat(),
                target_company_ids=set(company_ids) if partial_run else None,
            )
            write_csv(output_csv, rows)
            LOGGER.info("Built commercial value features: rows=%d output=%s", len(rows), output_csv)
            finish_run(conn, run_id=run_id, status="success", row_count=len(rows), message=f"asof={asof_date.isoformat()} output={output_csv}")
        except BaseException as exc:
            if run_id is not None and not (isinstance(exc, SystemExit) and exc.code in (0, None)):
                finish_run(conn, run_id=run_id, status="failed", row_count=0, message=f"{type(exc).__name__}: {exc}")
            raise


if __name__ == "__main__":
    main()
