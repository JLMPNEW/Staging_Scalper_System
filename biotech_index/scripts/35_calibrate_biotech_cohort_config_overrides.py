#!/usr/bin/env python3
from __future__ import annotations

import argparse
import bisect
import csv
import hashlib
import json
import logging
import math
import sqlite3
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from biotech_index.core.config import cfg_get, load_yaml, resolve_path  # noqa: E402
from biotech_index.core.db import quote_identifier  # noqa: E402
from biotech_index.core.logging_utils import configure_utc_logging  # noqa: E402
from biotech_index.core.market_policy import calibration_market_sources  # noqa: E402
from biotech_index.core.scoring_math import clamp, convex_risk_drag  # noqa: E402
from biotech_index.core.text_norm import normalize_ticker  # noqa: E402


LOGGER = logging.getLogger("calibrate_biotech_cohort_config_overrides")
DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
SQLITE_PARAM_CHUNK_SIZE = 800
DEFAULT_HORIZONS = [20, 60, 120]
DEFAULT_TOP_N = [10, 20]


@dataclass(frozen=True)
class Bar:
    day: date
    close: float


@dataclass(frozen=True)
class Thresholds:
    min_observations: int
    min_asof_dates: int
    min_unique_tickers: int
    min_current_investible_tickers: int
    min_recent_asof_coverage_pct: float
    min_median_confidence: float
    min_weighted_confidence: float
    max_review_share_pct: float


@dataclass(frozen=True)
class OverrideCandidate:
    candidate_id: str
    description: str
    profile_name: str
    weights: dict[str, float]
    risk_penalty: float
    risk_penalty_convexity: float
    risk_penalty_inflection: float
    override_strength: float


def parse_date(raw: object) -> date | None:
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        return datetime.strptime(text[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def to_float(raw: object, default: float | None = None) -> float | None:
    if raw is None:
        return default
    if isinstance(raw, bool):
        return float(int(raw))
    try:
        value = float(raw) if isinstance(raw, (int, float, str)) else float(str(raw))
    except (TypeError, ValueError):
        return default
    return value if math.isfinite(value) else default


def to_int(raw: object, default: int = 0) -> int:
    value = to_float(raw)
    return int(value) if value is not None else default


def parse_int_list(raw: object, default: list[int]) -> list[int]:
    if raw is None or str(raw).strip() == "":
        return list(default)
    values: list[int] = []
    for token in str(raw).replace(";", ",").replace("|", ",").split(","):
        token = token.strip()
        if token:
            values.append(int(token))
    return sorted(set(values)) or list(default)


def mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def percentile(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    rank = max(0.0, min(1.0, q)) * (len(ordered) - 1)
    lower = math.floor(rank)
    upper = math.ceil(rank)
    if lower == upper:
        return ordered[lower]
    weight = rank - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def lcb_mean(values: list[float], z: float) -> float:
    if not values:
        return 0.0
    avg = mean(values)
    if len(values) < 2:
        return avg
    variance = sum((value - avg) ** 2 for value in values) / (len(values) - 1)
    return avg - z * math.sqrt(variance) / math.sqrt(len(values))


def profit_factor(values: list[float]) -> float:
    gains = sum(value for value in values if value > 0.0)
    losses = -sum(value for value in values if value < 0.0)
    if losses <= 0.0:
        return 999.0 if gains > 0.0 else 0.0
    return gains / losses


def top3_gain_contribution_pct(values: list[float]) -> float:
    gains = sorted([value for value in values if value > 0.0], reverse=True)
    total_gain = sum(gains)
    if total_gain <= 0.0:
        return 0.0
    return (sum(gains[:3]) / total_gain) * 100.0


def summarize_returns(values: list[float], *, lcb_z: float) -> dict[str, float]:
    if not values:
        return {
            "mean_return_pct": 0.0,
            "median_return_pct": 0.0,
            "lcb_return_pct": 0.0,
            "p10_return_pct": 0.0,
            "win_rate_pct": 0.0,
            "profit_factor": 0.0,
            "large_loss_20pct_count": 0.0,
            "large_loss_20pct_rate_pct": 0.0,
            "large_loss_40pct_rate_pct": 0.0,
            "top3_gain_contribution_pct": 0.0,
        }
    return {
        "mean_return_pct": mean(values) * 100.0,
        "median_return_pct": percentile(values, 0.50) * 100.0,
        "lcb_return_pct": lcb_mean(values, lcb_z) * 100.0,
        "p10_return_pct": percentile(values, 0.10) * 100.0,
        "win_rate_pct": (sum(1 for value in values if value > 0.0) / len(values)) * 100.0,
        "profit_factor": profit_factor(values),
        "large_loss_20pct_count": float(sum(1 for value in values if value <= -0.20)),
        "large_loss_20pct_rate_pct": (sum(1 for value in values if value <= -0.20) / len(values)) * 100.0,
        "large_loss_40pct_rate_pct": (sum(1 for value in values if value <= -0.40) / len(values)) * 100.0,
        "top3_gain_contribution_pct": top3_gain_contribution_pct(values),
    }


def pct_rank_map(values: dict[str, float]) -> dict[str, float]:
    if not values:
        return {}
    ordered = sorted(set(values.values()))
    if len(ordered) == 1:
        return {key: 0.5 for key in values}
    denom = len(ordered) - 1
    rank_by_value = {value: idx / denom for idx, value in enumerate(ordered)}
    return {key: rank_by_value[value] for key, value in values.items()}


def connect_readonly(db_path: Path) -> sqlite3.Connection:
    uri = f"{db_path.resolve().as_uri()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=30.0)
    conn.row_factory = sqlite3.Row
    return conn


def table_columns(conn: sqlite3.Connection, table_name: str) -> set[str]:
    table_sql = quote_identifier(table_name)
    return {str(row["name"]) for row in conn.execute(f"PRAGMA table_info({table_sql})").fetchall()}


def chunked(items: list[Any], size: int) -> list[list[Any]]:
    return [items[idx : idx + size] for idx in range(0, len(items), size)]


def score_expr(columns: set[str], column: str, fallback_sql: str = "''") -> str:
    column_sql = quote_identifier(column)
    if column in columns:
        return f"s.{column_sql} AS {column_sql}"
    return f"{fallback_sql} AS {column_sql}"


def company_fallback_expr(columns: set[str], column: str, fallback_sql: str = "''") -> str:
    column_sql = quote_identifier(column)
    if column in columns:
        return f"COALESCE(s.{column_sql}, c.{column_sql}) AS {column_sql}"
    return f"COALESCE(c.{column_sql}, {fallback_sql}) AS {column_sql}"


def load_score_dates(
    conn: sqlite3.Connection,
    *,
    start_asof: str | None,
    end_asof: str | None,
    max_asof_dates: int,
) -> list[str]:
    clauses: list[str] = []
    params: list[str] = []
    if start_asof:
        clauses.append("asof_date >= ?")
        params.append(start_asof)
    if end_asof:
        clauses.append("asof_date <= ?")
        params.append(end_asof)
    where_sql = "WHERE " + " AND ".join(clauses) if clauses else ""
    rows = conn.execute(
        f"""
        SELECT DISTINCT asof_date
        FROM daily_scores
        {where_sql}
        ORDER BY asof_date
        """,
        params,
    ).fetchall()
    dates = [str(row["asof_date"]) for row in rows]
    if max_asof_dates > 0:
        return dates[-max_asof_dates:]
    return dates


def load_score_rows(
    conn: sqlite3.Connection,
    dates: list[str],
    *,
    rank_limit: int,
) -> list[dict[str, Any]]:
    if not dates:
        return []
    score_columns = table_columns(conn, "daily_scores")
    company_columns = table_columns(conn, "companies")
    required = {"asof_date", "company_id", "rank"}
    missing = required - score_columns
    if missing:
        raise RuntimeError(f"daily_scores is missing required column(s): {sorted(missing)}")
    selected_columns = [
        score_expr(score_columns, "asof_date"),
        score_expr(score_columns, "company_id"),
        company_fallback_expr(score_columns, "ticker") if "ticker" in company_columns else score_expr(score_columns, "ticker"),
        company_fallback_expr(score_columns, "company_name")
        if "company_name" in company_columns
        else score_expr(score_columns, "company_name"),
        score_expr(score_columns, "rank", "NULL"),
        score_expr(score_columns, "opportunity_score", "NULL"),
        score_expr(score_columns, "investment_score", "NULL"),
        score_expr(score_columns, "clinical_opportunity_score", "NULL"),
        score_expr(score_columns, "commercial_value_score", "NULL"),
        score_expr(score_columns, "forward_guidance_score", "NULL"),
        score_expr(score_columns, "valuation_score", "NULL"),
        score_expr(score_columns, "quality_adjusted_valuation_score", "NULL"),
        score_expr(score_columns, "upside_capacity_score", "NULL"),
        score_expr(score_columns, "institutional_upside_capacity_score", "NULL"),
        score_expr(score_columns, "financial_quality_score", "NULL"),
        score_expr(score_columns, "momentum_score", "NULL"),
        score_expr(score_columns, "risk_score", "NULL"),
        score_expr(score_columns, "legacy_risk_score", "NULL"),
        score_expr(score_columns, "risk_penalty_input_score", "NULL"),
        score_expr(score_columns, "predictive_risk_penalty_input_score", "NULL"),
        score_expr(score_columns, "uncompensated_risk_score", "NULL"),
        score_expr(score_columns, "compensated_risk_score", "NULL"),
        score_expr(score_columns, "liquidity_risk_score", "NULL"),
        score_expr(score_columns, "financing_survival_risk_score", "NULL"),
        score_expr(score_columns, "governance_filing_risk_score", "NULL"),
        score_expr(score_columns, "regulatory_setback_risk_score", "NULL"),
        score_expr(score_columns, "pipeline_anchor_risk_score", "NULL"),
        score_expr(score_columns, "collaborator_dependency_risk_score", "NULL"),
        score_expr(score_columns, "trial_staleness_risk_score", "NULL"),
        score_expr(score_columns, "bucket"),
        score_expr(score_columns, "biotech_primary_cohort", "'unclassified_review'"),
        score_expr(score_columns, "biotech_secondary_cohort"),
        score_expr(score_columns, "biotech_cohort_confidence", "NULL"),
        score_expr(score_columns, "biotech_cohort_margin", "NULL"),
        score_expr(score_columns, "biotech_cohort_data_quality"),
        score_expr(score_columns, "biotech_taxonomy_review_required", "0.0"),
        score_expr(score_columns, "biotech_cohort_sparse_data_flag", "0.0"),
        score_expr(score_columns, "biotech_cohort_calibration_weight", "0.0"),
        score_expr(score_columns, "biotech_cohort_investible_flag", "1.0"),
        score_expr(score_columns, "biotech_cohort_calibration_eligible_flag", "1.0"),
        score_expr(score_columns, "biotech_cohort_calibration_mode", "'unclassified'"),
        score_expr(score_columns, "biotech_cohort_exclusion_reason"),
        score_expr(score_columns, "biotech_cohort_overlays"),
        score_expr(score_columns, "rank_quality_cap_vetoed", "0.0"),
    ]
    rows_out: list[dict[str, Any]] = []
    for date_chunk in chunked(dates, SQLITE_PARAM_CHUNK_SIZE - 1):
        placeholders = ",".join("?" for _ in date_chunk)
        params: list[Any] = [*date_chunk]
        rank_clause = ""
        if rank_limit > 0:
            rank_clause = "AND s.rank <= ?"
            params.append(rank_limit)
        rows = conn.execute(
            f"""
            SELECT {", ".join(selected_columns)}
            FROM daily_scores s
            LEFT JOIN companies c ON c.company_id = s.company_id
            WHERE s.asof_date IN ({placeholders})
              AND s.rank IS NOT NULL
              AND s.rank > 0
              {rank_clause}
            ORDER BY s.asof_date, s.rank, ticker
            """,
            tuple(params),
        ).fetchall()
        rows_out.extend(dict(row) for row in rows)
    return rows_out


def load_bars(
    conn: sqlite3.Connection,
    *,
    tickers: set[str],
    min_date: date,
    max_date: date,
    market_sources: list[str],
) -> dict[str, list[Bar]]:
    if not tickers:
        return {}
    ordered_sources = [str(source or "").strip() for source in market_sources if str(source or "").strip()]
    if not ordered_sources:
        raise ValueError("At least one market source is required")
    source_priority = {source: idx for idx, source in enumerate(ordered_sources)}
    grouped: dict[tuple[str, str], list[Bar]] = defaultdict(list)
    ordered_tickers = sorted(tickers)
    ticker_chunk_size = max(1, SQLITE_PARAM_CHUNK_SIZE - len(ordered_sources) - 2)
    for ticker_chunk in chunked(ordered_tickers, ticker_chunk_size):
        ticker_placeholders = ",".join("?" for _ in ticker_chunk)
        source_placeholders = ",".join("?" for _ in ordered_sources)
        rows = conn.execute(
            f"""
            SELECT ticker, bar_date, source, close
            FROM market_bars_daily
            WHERE ticker IN ({ticker_placeholders})
              AND source IN ({source_placeholders})
              AND bar_date >= ?
              AND bar_date <= ?
              AND close IS NOT NULL
              AND close > 0
            ORDER BY ticker, source, bar_date
            """,
            (*ticker_chunk, *ordered_sources, min_date.isoformat(), max_date.isoformat()),
        ).fetchall()
        for row in rows:
            parsed = parse_date(row["bar_date"])
            close = to_float(row["close"])
            ticker = normalize_ticker(row["ticker"])
            if parsed is None or close is None or close <= 0.0 or not ticker:
                continue
            grouped[(ticker, str(row["source"] or ""))].append(Bar(day=parsed, close=close))
    by_ticker: dict[str, list[tuple[int, list[Bar]]]] = defaultdict(list)
    for (ticker, source), bars in grouped.items():
        if bars:
            by_ticker[ticker].append((source_priority.get(source, 9999), sorted(bars, key=lambda bar: bar.day)))
    out: dict[str, list[Bar]] = {}
    for ticker in ordered_tickers:
        candidates = by_ticker.get(ticker, [])
        if candidates:
            out[ticker] = min(candidates, key=lambda item: item[0])[1]
    return out


def forward_return(
    bars: list[Bar],
    asof: date,
    horizon: int,
    *,
    next_bar_entry: bool,
) -> tuple[float | None, str, str, str]:
    if not bars:
        return None, "", "", "no_market_bars"
    days = [bar.day for bar in bars]
    entry_idx = bisect.bisect_right(days, asof) if next_bar_entry else bisect.bisect_left(days, asof)
    if entry_idx < 0 or entry_idx >= len(bars):
        return None, "", "", "no_entry_bar"
    target_idx = entry_idx + horizon
    entry = bars[entry_idx]
    if target_idx >= len(bars):
        return None, entry.day.isoformat(), "", "insufficient_horizon_bars"
    target = bars[target_idx]
    if entry.close <= 0.0:
        return None, entry.day.isoformat(), target.day.isoformat(), "invalid_entry_close"
    return (target.close / entry.close) - 1.0, entry.day.isoformat(), target.day.isoformat(), ""


def cohort_of(row: dict[str, Any]) -> str:
    return str(row.get("biotech_primary_cohort") or "unclassified_review").strip() or "unclassified_review"


def is_review_or_unclassified(row: dict[str, Any]) -> bool:
    cohort = cohort_of(row)
    quality = str(row.get("biotech_cohort_data_quality") or "").strip().lower()
    return (
        cohort == "unclassified_review"
        or quality == "review"
        or (to_float(row.get("biotech_taxonomy_review_required"), 0.0) or 0.0) > 0.0
    )


def confidence_bucket(confidence: float | None) -> str:
    if confidence is None:
        return "missing"
    if confidence < 60.0:
        return "lt_60"
    if confidence < 75.0:
        return "60_to_75"
    if confidence < 80.0:
        return "75_to_80"
    if confidence < 85.0:
        return "80_to_85"
    if confidence < 90.0:
        return "85_to_90"
    return "gte_90"


def build_label_artifacts(
    score_rows: list[dict[str, Any]],
    *,
    thresholds: Thresholds,
    recent_dates: list[str],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    by_cohort: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in score_rows:
        by_cohort[cohort_of(row)].append(row)
    latest_asof = max((str(row.get("asof_date") or "") for row in score_rows), default="")
    latest_rows = [row for row in score_rows if str(row.get("asof_date") or "") == latest_asof]
    latest_by_cohort: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in latest_rows:
        latest_by_cohort[cohort_of(row)].append(row)

    ticker_history: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for row in score_rows:
        ticker = normalize_ticker(row.get("ticker"))
        asof = str(row.get("asof_date") or "")
        if ticker and asof:
            ticker_history[ticker].append((asof, cohort_of(row)))

    transition_counts: Counter[tuple[str, str]] = Counter()
    transition_tickers: dict[tuple[str, str], set[str]] = defaultdict(set)
    transition_dates: dict[tuple[str, str], list[str]] = defaultdict(list)
    transition_out_total: Counter[str] = Counter()
    transition_out_changed: Counter[str] = Counter()
    stable_ticker_count_by_cohort: Counter[str] = Counter()
    ticker_count_by_cohort: Counter[str] = Counter()
    for ticker, history in ticker_history.items():
        ordered = sorted(history)
        cohorts_seen = {cohort for _, cohort in ordered}
        for cohort in cohorts_seen:
            ticker_count_by_cohort[cohort] += 1
            if len(cohorts_seen) == 1:
                stable_ticker_count_by_cohort[cohort] += 1
        for (left_asof, left_cohort), (right_asof, right_cohort) in zip(ordered, ordered[1:]):
            transition_out_total[left_cohort] += 1
            if left_cohort != right_cohort:
                key = (left_cohort, right_cohort)
                transition_counts[key] += 1
                transition_tickers[key].add(ticker)
                transition_dates[key].extend([left_asof, right_asof])
                transition_out_changed[left_cohort] += 1

    transition_rows: list[dict[str, Any]] = []
    for (from_cohort, to_cohort), count in sorted(transition_counts.items()):
        dates = transition_dates[(from_cohort, to_cohort)]
        transition_rows.append(
            {
                "from_cohort": from_cohort,
                "to_cohort": to_cohort,
                "transition_count": count,
                "unique_tickers": len(transition_tickers[(from_cohort, to_cohort)]),
                "first_transition_asof": min(dates) if dates else "",
                "last_transition_asof": max(dates) if dates else "",
            }
        )

    confidence_rows: list[dict[str, Any]] = []
    for cohort, rows in sorted(by_cohort.items()):
        counts = Counter(confidence_bucket(to_float(row.get("biotech_cohort_confidence"))) for row in rows)
        total = sum(counts.values())
        for bucket in ("missing", "lt_60", "60_to_75", "75_to_80", "80_to_85", "85_to_90", "gte_90"):
            confidence_rows.append(
                {
                    "biotech_primary_cohort": cohort,
                    "confidence_bucket": bucket,
                    "row_count": counts.get(bucket, 0),
                    "row_share_pct": round((counts.get(bucket, 0) / total * 100.0) if total else 0.0, 6),
                }
            )

    recent_date_set = set(recent_dates)
    label_rows: list[dict[str, Any]] = []
    sparse_rows: list[dict[str, Any]] = []
    for cohort, rows in sorted(by_cohort.items()):
        confidence_pairs = [
            (
                to_float(row.get("biotech_cohort_confidence")),
                max(0.0, to_float(row.get("biotech_cohort_calibration_weight"), 0.0) or 0.0),
            )
            for row in rows
        ]
        confidences = [confidence for confidence, _weight in confidence_pairs if confidence is not None]
        weighted_numerator = sum(
            (confidence or 0.0) * weight
            for confidence, weight in confidence_pairs
            if confidence is not None
        )
        weighted_denominator = sum(weight for confidence, weight in confidence_pairs if confidence is not None)
        weighted_confidence = weighted_numerator / weighted_denominator if weighted_denominator > 0.0 else mean(confidences)
        unique_tickers = {normalize_ticker(row.get("ticker")) for row in rows if normalize_ticker(row.get("ticker"))}
        asof_dates = {str(row.get("asof_date") or "") for row in rows if str(row.get("asof_date") or "")}
        current_rows = latest_by_cohort.get(cohort, [])
        current_investible = [
            row for row in current_rows if (to_float(row.get("biotech_cohort_investible_flag"), 1.0) or 0.0) > 0.0
        ]
        current_eligible = [
            row
            for row in current_rows
            if (to_float(row.get("biotech_cohort_calibration_eligible_flag"), 1.0) or 0.0) > 0.0
        ]
        review_count = sum(1 for row in rows if is_review_or_unclassified(row))
        low_conf_count = sum(
            1
            for row in rows
            if (to_float(row.get("biotech_cohort_confidence")) is None)
            or (to_float(row.get("biotech_cohort_confidence")) or 0.0) < thresholds.min_median_confidence
        )
        transition_denominator = transition_out_total.get(cohort, 0)
        transition_rate = (
            transition_out_changed.get(cohort, 0) / transition_denominator * 100.0
            if transition_denominator
            else 0.0
        )
        stable_share = (
            stable_ticker_count_by_cohort.get(cohort, 0) / ticker_count_by_cohort.get(cohort, 1) * 100.0
            if ticker_count_by_cohort.get(cohort, 0)
            else 0.0
        )
        recent_covered = {
            str(row.get("asof_date") or "")
            for row in rows
            if str(row.get("asof_date") or "") in recent_date_set
        }
        recent_coverage_pct = (len(recent_covered) / len(recent_date_set) * 100.0) if recent_date_set else 0.0
        label_row = {
            "biotech_primary_cohort": cohort,
            "historical_observations": len(rows),
            "unique_tickers": len(unique_tickers),
            "asof_dates": len(asof_dates),
            "current_ticker_count": len(current_rows),
            "current_investible_tickers": len(current_investible),
            "current_calibration_eligible_tickers": len(current_eligible),
            "median_cohort_confidence": round(percentile(confidences, 0.50), 6) if confidences else 0.0,
            "mean_cohort_confidence": round(mean(confidences), 6) if confidences else 0.0,
            "calibration_weighted_confidence": round(weighted_confidence, 6) if confidences else 0.0,
            "low_confidence_share_pct": round(low_conf_count / len(rows) * 100.0 if rows else 0.0, 6),
            "unclassified_or_review_share_pct": round(review_count / len(rows) * 100.0 if rows else 0.0, 6),
            "transition_out_count": transition_out_changed.get(cohort, 0),
            "transition_out_rate_pct": round(transition_rate, 6),
            "ticker_stability_share_pct": round(stable_share, 6),
            "recent_asof_coverage_pct": round(recent_coverage_pct, 6),
            "latest_asof_date": latest_asof,
        }
        blockers = sparse_blockers(label_row, thresholds)
        sparse_row = dict(label_row)
        sparse_row["fallback_required"] = int(bool(blockers))
        sparse_row["fallback_reason"] = "|".join(blockers)
        label_rows.append(label_row)
        sparse_rows.append(sparse_row)
    return label_rows, transition_rows, confidence_rows, sparse_rows


def sparse_blockers(row: dict[str, Any], thresholds: Thresholds) -> list[str]:
    checks = [
        ("observations_below_min", float(row.get("historical_observations") or 0.0), thresholds.min_observations),
        ("asof_dates_below_min", float(row.get("asof_dates") or 0.0), thresholds.min_asof_dates),
        ("unique_tickers_below_min", float(row.get("unique_tickers") or 0.0), thresholds.min_unique_tickers),
        (
            "current_investible_below_min",
            float(row.get("current_investible_tickers") or 0.0),
            thresholds.min_current_investible_tickers,
        ),
        (
            "recent_coverage_below_min",
            float(row.get("recent_asof_coverage_pct") or 0.0),
            thresholds.min_recent_asof_coverage_pct,
        ),
        (
            "median_confidence_below_min",
            float(row.get("median_cohort_confidence") or 0.0),
            thresholds.min_median_confidence,
        ),
        (
            "weighted_confidence_below_min",
            float(row.get("calibration_weighted_confidence") or 0.0),
            thresholds.min_weighted_confidence,
        ),
    ]
    blockers = [name for name, actual, minimum in checks if actual < minimum]
    if float(row.get("unclassified_or_review_share_pct") or 0.0) > thresholds.max_review_share_pct:
        blockers.append("review_share_above_max")
    return blockers


def build_observations(
    score_rows: list[dict[str, Any]],
    bars_by_ticker: dict[str, list[Bar]],
    *,
    horizons: list[int],
    top_n_values: list[int],
    next_bar_entry: bool,
    round_trip_cost_bps: float,
) -> list[dict[str, Any]]:
    cost = round_trip_cost_bps / 10_000.0
    max_top_n = max(top_n_values)
    missing_counts: Counter[tuple[int, str]] = Counter()
    observations: list[dict[str, Any]] = []
    for row in score_rows:
        rank = to_int(row.get("rank"), 999_999)
        if rank > max_top_n:
            continue
        ticker = normalize_ticker(row.get("ticker"))
        asof = parse_date(row.get("asof_date"))
        for horizon in horizons:
            ret: float | None = None
            entry_date = ""
            target_date = ""
            reason = "invalid_asof_date"
            if asof is not None:
                ret, entry_date, target_date, reason = forward_return(
                    bars_by_ticker.get(ticker, []),
                    asof,
                    horizon,
                    next_bar_entry=next_bar_entry,
                )
            net_return = ret - cost if ret is not None else None
            if ret is None:
                missing_counts[(horizon, reason)] += 1
            observations.append(
                {
                    "asof_date": row.get("asof_date", ""),
                    "ticker": ticker,
                    "company_id": row.get("company_id", ""),
                    "company_name": row.get("company_name", ""),
                    "rank": rank,
                    "horizon_days": horizon,
                    "evaluation_split": "",
                    "entry_date": entry_date,
                    "target_date": target_date,
                    "return": ret,
                    "net_return": net_return,
                    "missing_return_reason": reason if ret is None else "",
                    "biotech_primary_cohort": cohort_of(row),
                    "opportunity_score": row.get("opportunity_score", ""),
                    "investment_score": row.get("investment_score", ""),
                    "rank_quality_cap_vetoed": row.get("rank_quality_cap_vetoed", ""),
                }
            )
    if missing_counts:
        LOGGER.warning(
            "Forward-return coverage gaps: %s",
            ", ".join(f"{horizon}d:{reason}={count}" for (horizon, reason), count in sorted(missing_counts.items())),
        )
    return observations


def split_maps_for_observations(
    observations: list[dict[str, Any]],
    *,
    horizons: list[int],
    train_fraction: float,
) -> dict[int, dict[str, str]]:
    split_maps: dict[int, dict[str, str]] = {}
    for horizon in horizons:
        dates = sorted(
            {
                str(row.get("asof_date") or "")
                for row in observations
                if int(row["horizon_days"]) == horizon and row.get("net_return") is not None
            }
        )
        if len(dates) < 2:
            split_maps[horizon] = {asof_date: "train" for asof_date in dates}
            continue
        bounded_fraction = max(0.10, min(0.90, float(train_fraction)))
        split_idx = int(math.floor(len(dates) * bounded_fraction))
        split_idx = max(1, min(len(dates) - 1, split_idx))
        split_maps[horizon] = {
            **{asof_date: "train" for asof_date in dates[:split_idx]},
            **{asof_date: "test" for asof_date in dates[split_idx:]},
        }
    return split_maps


def build_baseline_summary(
    observations: list[dict[str, Any]],
    *,
    horizons: list[int],
    top_n_values: list[int],
    train_fraction: float,
    lcb_z: float,
) -> list[dict[str, Any]]:
    split_maps = split_maps_for_observations(observations, horizons=horizons, train_fraction=train_fraction)
    grouped: dict[tuple[str, int, int, str], list[dict[str, Any]]] = defaultdict(list)
    for row in observations:
        if row.get("net_return") is None:
            continue
        horizon = int(row["horizon_days"])
        split = split_maps[horizon].get(str(row.get("asof_date") or ""), "")
        row["evaluation_split"] = split
        rank = int(row["rank"])
        for top_n in top_n_values:
            if rank > top_n:
                continue
            for split_name in ("all", split):
                if not split_name:
                    continue
                grouped[(cohort_of(row), top_n, horizon, split_name)].append(row)
                grouped[("all_cohorts", top_n, horizon, split_name)].append(row)

    summary_rows: list[dict[str, Any]] = []
    for (cohort, top_n, horizon, split), rows in sorted(grouped.items()):
        values = [float(row["net_return"]) for row in rows]
        metrics = summarize_returns(values, lcb_z=lcb_z)
        rank_values = [float(row["rank"]) for row in rows]
        score_values = [value for value in (to_float(row.get("opportunity_score")) for row in rows) if value is not None]
        summary_rows.append(
            {
                "evaluation_split": split,
                "top_n": top_n,
                "horizon_days": horizon,
                "biotech_primary_cohort": cohort,
                "selected_n": len(rows),
                "asof_dates": len({str(row.get("asof_date") or "") for row in rows}),
                "unique_tickers": len({str(row.get("ticker") or "") for row in rows}),
                "avg_rank": round(mean(rank_values), 6) if rank_values else 0.0,
                "avg_opportunity_score": round(mean(score_values), 6) if score_values else 0.0,
                **{key: round(value, 6) for key, value in metrics.items()},
                "top_tickers": top_ticker_text(rows),
            }
        )
    return summary_rows


def top_ticker_text(rows: list[dict[str, Any]], *, limit: int = 10) -> str:
    counts = Counter(normalize_ticker(row.get("ticker")) for row in rows)
    counts.pop("", None)
    return "|".join(f"{ticker}:{count}" for ticker, count in counts.most_common(limit))


def build_top_exposure(score_rows: list[dict[str, Any]], *, top_n_values: list[int]) -> list[dict[str, Any]]:
    cohorts = sorted({cohort_of(row) for row in score_rows})
    dates = sorted({str(row.get("asof_date") or "") for row in score_rows if str(row.get("asof_date") or "")})
    latest_asof = dates[-1] if dates else ""
    latest_rows = [row for row in score_rows if str(row.get("asof_date") or "") == latest_asof]
    rows: list[dict[str, Any]] = []
    for cohort in cohorts:
        out: dict[str, Any] = {
            "biotech_primary_cohort": cohort,
            "latest_asof_date": latest_asof,
            "current_ticker_count": sum(1 for row in latest_rows if cohort_of(row) == cohort),
        }
        for top_n in top_n_values:
            top_rows = [row for row in score_rows if cohort_of(row) == cohort and to_int(row.get("rank"), 999_999) <= top_n]
            latest_top_rows = [
                row for row in latest_rows if cohort_of(row) == cohort and to_int(row.get("rank"), 999_999) <= top_n
            ]
            dates_with_exposure = {str(row.get("asof_date") or "") for row in top_rows}
            total_top_slots = len(dates) * top_n
            out[f"top{top_n}_selected_n"] = len(top_rows)
            out[f"top{top_n}_share_pct"] = round((len(top_rows) / total_top_slots * 100.0) if total_top_slots else 0.0, 6)
            out[f"top{top_n}_asof_frequency_pct"] = round(
                (len(dates_with_exposure) / len(dates) * 100.0) if dates else 0.0,
                6,
            )
            out[f"latest_top{top_n}_count"] = len(latest_top_rows)
        rows.append(out)
    return rows


def rank_instability_by_cohort(score_rows: list[dict[str, Any]]) -> dict[str, float]:
    by_cohort_ticker: dict[tuple[str, str], list[float]] = defaultdict(list)
    for row in score_rows:
        ticker = normalize_ticker(row.get("ticker"))
        rank = to_float(row.get("rank"))
        if not ticker or rank is None:
            continue
        by_cohort_ticker[(cohort_of(row), ticker)].append(rank)
    by_cohort: dict[str, list[float]] = defaultdict(list)
    for (cohort, _ticker), ranks in by_cohort_ticker.items():
        if len(ranks) < 2:
            continue
        avg = mean(ranks)
        variance = sum((rank - avg) ** 2 for rank in ranks) / (len(ranks) - 1)
        by_cohort[cohort].append(math.sqrt(variance))
    return {cohort: mean(values) for cohort, values in by_cohort.items()}


def build_priority_rows(
    label_rows: list[dict[str, Any]],
    top_exposure_rows: list[dict[str, Any]],
    baseline_rows: list[dict[str, Any]],
    score_rows: list[dict[str, Any]],
    *,
    primary_top_n: int,
    primary_horizon: int,
    thresholds: Thresholds,
) -> list[dict[str, Any]]:
    cohorts = [str(row["biotech_primary_cohort"]) for row in label_rows]
    labels = {str(row["biotech_primary_cohort"]): row for row in label_rows}
    exposure = {str(row["biotech_primary_cohort"]): row for row in top_exposure_rows}
    instability = rank_instability_by_cohort(score_rows)
    baseline_by_key = {
        (
            str(row.get("biotech_primary_cohort") or ""),
            int(row.get("top_n") or 0),
            int(row.get("horizon_days") or 0),
            str(row.get("evaluation_split") or ""),
        ): row
        for row in baseline_rows
    }
    global_key = ("all_cohorts", primary_top_n, primary_horizon, "test")
    global_lcb = float(baseline_by_key.get(global_key, {}).get("lcb_return_pct") or 0.0)
    total_large_losses = sum(
        float(row.get("large_loss_20pct_count") or 0.0)
        for row in baseline_rows
        if str(row.get("evaluation_split")) == "test"
        and int(row.get("top_n") or 0) == primary_top_n
        and int(row.get("horizon_days") or 0) == primary_horizon
        and str(row.get("biotech_primary_cohort")) != "all_cohorts"
    )
    metric_maps = {
        "historical_observations": {
            cohort: float(labels[cohort].get("historical_observations") or 0.0) for cohort in cohorts
        },
        "current_ticker_count": {cohort: float(labels[cohort].get("current_ticker_count") or 0.0) for cohort in cohorts},
        "top20_exposure_frequency": {
            cohort: float(exposure.get(cohort, {}).get("top20_asof_frequency_pct") or 0.0) for cohort in cohorts
        },
        "calibration_eligible_count": {
            cohort: float(labels[cohort].get("current_calibration_eligible_tickers") or 0.0) for cohort in cohorts
        },
        "performance_gap_abs": {},
    }
    large_loss_contribution: dict[str, float] = {}
    for cohort in cohorts:
        key = (cohort, primary_top_n, primary_horizon, "test")
        row = baseline_by_key.get(key, {})
        cohort_lcb = float(row.get("lcb_return_pct") or 0.0)
        metric_maps["performance_gap_abs"][cohort] = abs(cohort_lcb - global_lcb)
        large_losses = float(row.get("large_loss_20pct_count") or 0.0)
        large_loss_contribution[cohort] = (large_losses / total_large_losses * 100.0) if total_large_losses else 0.0

    ranks = {name: pct_rank_map(values) for name, values in metric_maps.items()}
    instability_pct = pct_rank_map({cohort: float(instability.get(cohort, 0.0)) for cohort in cohorts})
    priority_rows: list[dict[str, Any]] = []
    for cohort in cohorts:
        base_priority = (
            0.25 * ranks["historical_observations"].get(cohort, 0.0)
            + 0.20 * ranks["current_ticker_count"].get(cohort, 0.0)
            + 0.20 * ranks["top20_exposure_frequency"].get(cohort, 0.0)
            + 0.15 * ranks["calibration_eligible_count"].get(cohort, 0.0)
            + 0.20 * ranks["performance_gap_abs"].get(cohort, 0.0)
        )
        appears_current_top10 = float(exposure.get(cohort, {}).get("latest_top10_count") or 0.0) > 0.0
        high_large_loss = large_loss_contribution.get(cohort, 0.0) >= 20.0
        high_instability = instability_pct.get(cohort, 0.0) >= 0.75
        boost = (0.10 if appears_current_top10 else 0.0) + (0.10 if high_large_loss else 0.0) + (0.05 if high_instability else 0.0)
        sparse_flag = bool(sparse_blockers(labels[cohort], thresholds))
        priority_rows.append(
            {
                "biotech_primary_cohort": cohort,
                "priority_score": round(base_priority + boost, 6),
                "base_priority_score": round(base_priority, 6),
                "priority_boost": round(boost, 6),
                "historical_observations": labels[cohort].get("historical_observations", 0),
                "current_ticker_count": labels[cohort].get("current_ticker_count", 0),
                "top20_exposure_frequency_pct": exposure.get(cohort, {}).get("top20_asof_frequency_pct", 0.0),
                "calibration_eligible_count": labels[cohort].get("current_calibration_eligible_tickers", 0),
                "test_top20_120d_lcb_gap_vs_global_pct": round(
                    float(baseline_by_key.get((cohort, primary_top_n, primary_horizon, "test"), {}).get("lcb_return_pct") or 0.0)
                    - global_lcb,
                    6,
                ),
                "performance_gap_abs_pct": round(metric_maps["performance_gap_abs"].get(cohort, 0.0), 6),
                "large_loss_contribution_pct": round(large_loss_contribution.get(cohort, 0.0), 6),
                "rank_instability_avg_std": round(instability.get(cohort, 0.0), 6),
                "appears_in_current_top10": int(appears_current_top10),
                "high_large_loss_contribution": int(high_large_loss),
                "high_score_rank_instability": int(high_instability),
                "sparse_or_label_blocked": int(sparse_flag),
                "recommended_next_step": "label_or_sparse_review" if sparse_flag else "calibrate_first_pass",
            }
        )
    priority_rows.sort(key=lambda row: (-float(row["priority_score"]), str(row["biotech_primary_cohort"])))
    for idx, row in enumerate(priority_rows, start=1):
        row["priority_rank"] = idx
    return priority_rows


INVESTMENT_COMPONENT_KEYS = [
    "clinical_opportunity",
    "commercial_value",
    "forward_guidance",
    "valuation",
    "upside_capacity",
    "institutional_upside",
    "financial_quality",
    "momentum",
]


def as_bool(raw: object, default: bool = False) -> bool:
    if raw is None or raw == "":
        return default
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, (int, float)):
        return bool(raw)
    text = str(raw).strip().lower()
    if text in {"1", "true", "t", "yes", "y", "on"}:
        return True
    if text in {"0", "false", "f", "no", "n", "off"}:
        return False
    return default


def profile_name_for_cohort(cohort: str) -> str:
    cohort_key = str(cohort or "").strip().lower()
    if "commercial" in cohort_key or "royalty" in cohort_key or "partnered_economics" in cohort_key:
        return "commercial_stage"
    return "clinical_stage"


def normalize_positive_weights(weights: dict[str, float]) -> dict[str, float]:
    cleaned = {key: max(0.0, float(weights.get(key, 0.0))) for key in INVESTMENT_COMPONENT_KEYS}
    total = sum(cleaned.values())
    if total <= 0.0:
        raise ValueError("candidate investment weights must have a positive sum")
    return {key: value / total for key, value in cleaned.items()}


def investment_profile(config: dict[str, Any], profile_name: str) -> tuple[dict[str, float], float]:
    raw = cfg_get(config, f"biotech_scoring.investment_weight_profiles.{profile_name}", None)
    if not isinstance(raw, dict):
        raw = cfg_get(config, "biotech_scoring.investment_weights", {})
    if not isinstance(raw, dict):
        raise ValueError("Missing biotech_scoring investment weight configuration")
    weights = normalize_positive_weights({key: to_float(raw.get(key), 0.0) or 0.0 for key in INVESTMENT_COMPONENT_KEYS})
    risk_penalty = to_float(raw.get("risk_penalty"), None)
    if risk_penalty is None:
        risk_penalty = float(cfg_get(config, "biotech_scoring.investment_weights.risk_penalty", 0.15))
    return weights, max(0.0, float(risk_penalty))


def candidate_hash(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]


def make_candidate(
    *,
    description: str,
    profile_name: str,
    weights: dict[str, float],
    risk_penalty: float,
    risk_penalty_convexity: float,
    risk_penalty_inflection: float,
    override_strength: float,
    baseline: bool = False,
) -> OverrideCandidate:
    normalized = normalize_positive_weights(weights)
    payload = {
        "profile_name": profile_name,
        "weights": {key: round(normalized[key], 8) for key in INVESTMENT_COMPONENT_KEYS},
        "risk_penalty": round(max(0.0, risk_penalty), 8),
        "risk_penalty_convexity": round(max(0.0, risk_penalty_convexity), 8),
        "risk_penalty_inflection": round(max(0.0, min(99.0, risk_penalty_inflection)), 8),
        "override_strength": round(max(0.0, min(1.0, override_strength)), 8),
    }
    return OverrideCandidate(
        candidate_id="baseline_current_config" if baseline else f"cohort_{candidate_hash(payload)}",
        description=description,
        profile_name=profile_name,
        weights=normalized,
        risk_penalty=payload["risk_penalty"],
        risk_penalty_convexity=payload["risk_penalty_convexity"],
        risk_penalty_inflection=payload["risk_penalty_inflection"],
        override_strength=0.0 if baseline else payload["override_strength"],
    )


def tilted_weights(base_weights: dict[str, float], boosts: dict[str, float]) -> dict[str, float]:
    adjusted = dict(base_weights)
    for key, boost in boosts.items():
        if key in adjusted:
            adjusted[key] = max(0.0, adjusted[key] + float(boost))
    return normalize_positive_weights(adjusted)


def candidate_grid(
    config: dict[str, Any],
    *,
    target_cohort: str,
    candidate_limit: int,
) -> list[OverrideCandidate]:
    profile_name = profile_name_for_cohort(target_cohort)
    base_weights, base_risk_penalty = investment_profile(config, profile_name)
    convexity = float(cfg_get(config, "biotech_scoring.risk_penalty_convexity", 0.35))
    inflection = float(cfg_get(config, "biotech_scoring.risk_penalty_inflection", 50.0))
    override_strength = float(cfg_get(config, "biotech_scoring.cohort_config_overrides.shrinkage.max_override_strength", 0.75))
    override_strength = max(0.0, min(1.0, override_strength))
    candidates: list[OverrideCandidate] = [
        make_candidate(
            description="current config baseline",
            profile_name=profile_name,
            weights=base_weights,
            risk_penalty=base_risk_penalty,
            risk_penalty_convexity=convexity,
            risk_penalty_inflection=inflection,
            override_strength=0.0,
            baseline=True,
        )
    ]

    for multiplier in (0.75, 0.90, 1.10, 1.25):
        candidates.append(
            make_candidate(
                description=f"risk_penalty_x{multiplier:g}",
                profile_name=profile_name,
                weights=base_weights,
                risk_penalty=max(0.01, base_risk_penalty * multiplier),
                risk_penalty_convexity=convexity,
                risk_penalty_inflection=inflection,
                override_strength=override_strength,
            )
        )
    for candidate_convexity in (0.20, 0.50):
        if abs(candidate_convexity - convexity) > 1e-9:
            candidates.append(
                make_candidate(
                    description=f"risk_convexity_{candidate_convexity:g}",
                    profile_name=profile_name,
                    weights=base_weights,
                    risk_penalty=base_risk_penalty,
                    risk_penalty_convexity=candidate_convexity,
                    risk_penalty_inflection=inflection,
                    override_strength=override_strength,
                )
            )
    for candidate_inflection in (40.0, 60.0):
        if abs(candidate_inflection - inflection) > 1e-9:
            candidates.append(
                make_candidate(
                    description=f"risk_inflection_{candidate_inflection:g}",
                    profile_name=profile_name,
                    weights=base_weights,
                    risk_penalty=base_risk_penalty,
                    risk_penalty_convexity=convexity,
                    risk_penalty_inflection=candidate_inflection,
                    override_strength=override_strength,
                )
            )

    if profile_name == "commercial_stage":
        tilts = [
            ("tilt_commercial_value", {"commercial_value": 0.05}),
            ("tilt_forward_guidance", {"forward_guidance": 0.05}),
            ("tilt_valuation", {"valuation": 0.05}),
            ("tilt_institutional_upside", {"institutional_upside": 0.05}),
            ("tilt_financial_quality", {"financial_quality": 0.05}),
            ("tilt_commercial_guidance", {"commercial_value": 0.04, "forward_guidance": 0.04}),
            ("tilt_commercial_valuation", {"commercial_value": 0.04, "valuation": 0.04}),
        ]
    else:
        tilts = [
            ("tilt_clinical", {"clinical_opportunity": 0.05}),
            ("tilt_clinical_strong", {"clinical_opportunity": 0.10}),
            ("tilt_upside_capacity", {"upside_capacity": 0.05}),
            ("tilt_financial_quality", {"financial_quality": 0.05}),
            ("tilt_momentum", {"momentum": 0.05}),
            ("tilt_clinical_upside", {"clinical_opportunity": 0.04, "upside_capacity": 0.04}),
            ("tilt_clinical_quality", {"clinical_opportunity": 0.04, "financial_quality": 0.04}),
        ]
    for description, boosts in tilts:
        candidates.append(
            make_candidate(
                description=description,
                profile_name=profile_name,
                weights=tilted_weights(base_weights, boosts),
                risk_penalty=base_risk_penalty,
                risk_penalty_convexity=convexity,
                risk_penalty_inflection=inflection,
                override_strength=override_strength,
            )
        )

    deduped: list[OverrideCandidate] = []
    seen: set[str] = set()
    for candidate in candidates:
        if candidate.candidate_id in seen:
            continue
        seen.add(candidate.candidate_id)
        deduped.append(candidate)
    if candidate_limit > 0:
        return deduped[: max(1, candidate_limit)]
    return deduped


def score_value(row: dict[str, Any], column: str, default: float) -> float:
    value = to_float(row.get(column), None)
    return clamp(default if value is None else value)


def component_scores(row: dict[str, Any], config: dict[str, Any]) -> dict[str, float]:
    missing_defaults_base = "biotech_scoring.missing_score_defaults"
    valuation_default = float(cfg_get(config, f"{missing_defaults_base}.valuation_score", 50.0))
    valuation = to_float(row.get("quality_adjusted_valuation_score"), None)
    if valuation is None:
        valuation = to_float(row.get("valuation_score"), valuation_default)
    valuation = clamp(valuation_default if valuation is None else valuation)
    return {
        "clinical_opportunity": score_value(row, "clinical_opportunity_score", to_float(row.get("opportunity_score"), 50.0) or 50.0),
        "commercial_value": score_value(
            row,
            "commercial_value_score",
            float(cfg_get(config, f"{missing_defaults_base}.commercial_value_score", 35.0)),
        ),
        "forward_guidance": score_value(
            row,
            "forward_guidance_score",
            float(cfg_get(config, f"{missing_defaults_base}.forward_guidance_score", 35.0)),
        ),
        "valuation": valuation,
        "upside_capacity": score_value(
            row,
            "upside_capacity_score",
            float(cfg_get(config, f"{missing_defaults_base}.upside_capacity_score", 50.0)),
        ),
        "institutional_upside": score_value(
            row,
            "institutional_upside_capacity_score",
            float(cfg_get(config, f"{missing_defaults_base}.institutional_upside_capacity_score", 50.0)),
        ),
        "financial_quality": score_value(row, "financial_quality_score", 0.0),
        "momentum": score_value(row, "momentum_score", 0.0),
        "risk": score_value(row, "risk_score", 100.0),
        "legacy_risk": score_value(row, "legacy_risk_score", score_value(row, "risk_score", 100.0)),
        "risk_penalty_input": score_value(row, "risk_penalty_input_score", score_value(row, "risk_score", 100.0)),
        "predictive_risk_penalty_input": score_value(
            row,
            "predictive_risk_penalty_input_score",
            score_value(row, "risk_penalty_input_score", score_value(row, "risk_score", 100.0)),
        ),
        "uncompensated_risk": score_value(row, "uncompensated_risk_score", score_value(row, "risk_score", 100.0)),
        "compensated_risk": score_value(row, "compensated_risk_score", 50.0),
        "liquidity_risk": score_value(row, "liquidity_risk_score", 0.0),
        "financing_survival_risk": score_value(row, "financing_survival_risk_score", 0.0),
        "governance_filing_risk": score_value(row, "governance_filing_risk_score", 0.0),
        "regulatory_setback_risk": score_value(row, "regulatory_setback_risk_score", 0.0),
        "pipeline_anchor_risk": score_value(row, "pipeline_anchor_risk_score", 0.0),
        "collaborator_dependency_risk": score_value(row, "collaborator_dependency_risk_score", 0.0),
        "trial_staleness_risk": score_value(row, "trial_staleness_risk_score", 0.0),
    }


def candidate_component_score(
    row: dict[str, Any],
    candidate: OverrideCandidate,
    config: dict[str, Any],
) -> float:
    scores = component_scores(row, config)
    positive = sum(candidate.weights[key] * scores[key] for key in INVESTMENT_COMPONENT_KEYS)
    risk_drag = convex_risk_drag(
        scores["risk"],
        candidate.risk_penalty,
        enabled=as_bool(cfg_get(config, "biotech_scoring.convex_risk_penalty_enabled", True), True),
        convexity=candidate.risk_penalty_convexity,
        inflection=candidate.risk_penalty_inflection,
    )
    return clamp(positive - risk_drag)


def adjusted_score_for_candidate(
    row: dict[str, Any],
    candidate: OverrideCandidate,
    baseline_candidate: OverrideCandidate,
    config: dict[str, Any],
    *,
    target_cohort: str,
) -> tuple[float, float, float, float]:
    current_score = to_float(row.get("opportunity_score"), 0.0) or 0.0
    if candidate.candidate_id == baseline_candidate.candidate_id or cohort_of(row) != target_cohort:
        baseline_component = candidate_component_score(row, baseline_candidate, config)
        return current_score, 0.0, baseline_component, baseline_component
    baseline_component = candidate_component_score(row, baseline_candidate, config)
    candidate_component = candidate_component_score(row, candidate, config)
    score_delta = (candidate_component - baseline_component) * candidate.override_strength
    return clamp(current_score + score_delta), score_delta, baseline_component, candidate_component


def build_candidate_grid_rows(candidates: list[OverrideCandidate], *, target_cohort: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for candidate in candidates:
        row = {
            "candidate_id": candidate.candidate_id,
            "target_cohort": target_cohort,
            "description": candidate.description,
            "profile_name": candidate.profile_name,
            "override_strength": round(candidate.override_strength, 6),
            "risk_penalty": round(candidate.risk_penalty, 6),
            "risk_penalty_convexity": round(candidate.risk_penalty_convexity, 6),
            "risk_penalty_inflection": round(candidate.risk_penalty_inflection, 6),
        }
        for key in INVESTMENT_COMPONENT_KEYS:
            row[f"weight_{key}"] = round(candidate.weights[key], 8)
        rows.append(row)
    return rows


def split_maps_for_score_dates(
    dates: list[str],
    *,
    horizons: list[int],
    train_fraction: float,
) -> dict[int, dict[str, str]]:
    ordered_dates = sorted({str(value or "") for value in dates if str(value or "")})
    split_maps: dict[int, dict[str, str]] = {}
    if len(ordered_dates) < 2:
        for horizon in horizons:
            split_maps[horizon] = {asof_date: "train" for asof_date in ordered_dates}
        return split_maps
    bounded_fraction = max(0.10, min(0.90, float(train_fraction)))
    split_idx = int(math.floor(len(ordered_dates) * bounded_fraction))
    split_idx = max(1, min(len(ordered_dates) - 1, split_idx))
    split_map = {
        **{asof_date: "train" for asof_date in ordered_dates[:split_idx]},
        **{asof_date: "test" for asof_date in ordered_dates[split_idx:]},
    }
    for horizon in horizons:
        split_maps[horizon] = dict(split_map)
    return split_maps


def split_maps_for_baseline_candidate_observations(
    observations: list[dict[str, Any]],
    *,
    horizons: list[int],
    train_fraction: float,
) -> dict[int, dict[str, str]]:
    split_maps: dict[int, dict[str, str]] = {}
    for horizon in horizons:
        dates = sorted(
            {
                str(row.get("asof_date") or "")
                for row in observations
                if str(row.get("candidate_id") or "") == "baseline_current_config"
                and int(row.get("horizon_days") or 0) == horizon
                and row.get("net_return") is not None
            }
        )
        if len(dates) < 2:
            split_maps[horizon] = {asof_date: "train" for asof_date in dates}
            continue
        bounded_fraction = max(0.10, min(0.90, float(train_fraction)))
        split_idx = int(math.floor(len(dates) * bounded_fraction))
        split_idx = max(1, min(len(dates) - 1, split_idx))
        split_maps[horizon] = {
            **{asof_date: "train" for asof_date in dates[:split_idx]},
            **{asof_date: "test" for asof_date in dates[split_idx:]},
        }
    return split_maps


def build_candidate_observations(
    score_rows: list[dict[str, Any]],
    bars_by_ticker: dict[str, list[Bar]],
    *,
    candidates: list[OverrideCandidate],
    target_cohort: str,
    config: dict[str, Any],
    horizons: list[int],
    top_n_values: list[int],
    next_bar_entry: bool,
    round_trip_cost_bps: float,
) -> list[dict[str, Any]]:
    if not candidates:
        return []
    baseline_candidate = candidates[0]
    rows_by_date: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in score_rows:
        asof_text = str(row.get("asof_date") or "")
        if asof_text:
            rows_by_date[asof_text].append(row)
    cost = round_trip_cost_bps / 10_000.0
    max_top_n = max(top_n_values)
    missing_counts: Counter[tuple[int, str]] = Counter()
    observations: list[dict[str, Any]] = []
    for candidate in candidates:
        for asof_text, rows_for_date in sorted(rows_by_date.items()):
            ranked_rows: list[tuple[float, int, str, dict[str, Any], float, float, float]] = []
            for row in rows_for_date:
                adjusted_score, score_delta, baseline_component, candidate_component = adjusted_score_for_candidate(
                    row,
                    candidate,
                    baseline_candidate,
                    config,
                    target_cohort=target_cohort,
                )
                ranked_rows.append(
                    (
                        adjusted_score,
                        to_int(row.get("rank"), 999_999),
                        normalize_ticker(row.get("ticker")),
                        row,
                        score_delta,
                        baseline_component,
                        candidate_component,
                    )
                )
            ranked_rows.sort(key=lambda item: (-item[0], item[1], item[2]))
            selection_sets = {
                "global_topn": ranked_rows[:max_top_n],
                "target_cohort_topn": [item for item in ranked_rows if cohort_of(item[3]) == target_cohort][:max_top_n],
            }
            asof = parse_date(asof_text)
            for selection_scope, selected_ranked_rows in selection_sets.items():
                for candidate_rank, (
                    adjusted_score,
                    baseline_rank,
                    ticker,
                    row,
                    score_delta,
                    baseline_component,
                    candidate_component,
                ) in enumerate(selected_ranked_rows, start=1):
                    component_values = component_scores(row, config)
                    for top_n in top_n_values:
                        if candidate_rank > top_n:
                            continue
                        for horizon in horizons:
                            ret: float | None = None
                            entry_date = ""
                            target_date = ""
                            reason = "invalid_asof_date"
                            if asof is not None:
                                ret, entry_date, target_date, reason = forward_return(
                                    bars_by_ticker.get(ticker, []),
                                    asof,
                                    horizon,
                                    next_bar_entry=next_bar_entry,
                                )
                            net_return = ret - cost if ret is not None else None
                            if ret is None:
                                missing_counts[(horizon, reason)] += 1
                            observations.append(
                                {
                                    "candidate_id": candidate.candidate_id,
                                    "candidate_description": candidate.description,
                                    "target_cohort": target_cohort,
                                    "selection_scope": selection_scope,
                                    "profile_name": candidate.profile_name,
                                    "asof_date": asof_text,
                                    "ticker": ticker,
                                    "company_id": row.get("company_id", ""),
                                    "company_name": row.get("company_name", ""),
                                    "baseline_rank": baseline_rank,
                                    "candidate_rank": candidate_rank,
                                    "top_n": top_n,
                                    "horizon_days": horizon,
                                    "evaluation_split": "",
                                    "entry_date": entry_date,
                                    "target_date": target_date,
                                    "return": ret,
                                    "net_return": net_return,
                                    "missing_return_reason": reason if ret is None else "",
                                    "biotech_primary_cohort": cohort_of(row),
                                    "opportunity_score": round(to_float(row.get("opportunity_score"), 0.0) or 0.0, 6),
                                    "adjusted_opportunity_score": round(adjusted_score, 6),
                                    "score_delta": round(score_delta, 6),
                                    "baseline_component_score": round(baseline_component, 6),
                                    "candidate_component_score": round(candidate_component, 6),
                                    "component_clinical_opportunity_score": round(
                                        component_values["clinical_opportunity"],
                                        6,
                                    ),
                                    "component_commercial_value_score": round(component_values["commercial_value"], 6),
                                    "component_forward_guidance_score": round(component_values["forward_guidance"], 6),
                                    "component_valuation_score": round(component_values["valuation"], 6),
                                    "component_upside_capacity_score": round(component_values["upside_capacity"], 6),
                                    "component_institutional_upside_score": round(
                                        component_values["institutional_upside"],
                                        6,
                                    ),
                                    "component_financial_quality_score": round(component_values["financial_quality"], 6),
                                    "component_momentum_score": round(component_values["momentum"], 6),
                                    "component_risk_score": round(component_values["risk"], 6),
                                    "override_strength": round(candidate.override_strength, 6),
                                    "risk_penalty": round(candidate.risk_penalty, 6),
                                    "risk_penalty_convexity": round(candidate.risk_penalty_convexity, 6),
                                    "risk_penalty_inflection": round(candidate.risk_penalty_inflection, 6),
                                    "rank_quality_cap_vetoed": row.get("rank_quality_cap_vetoed", ""),
                                }
                            )
    if missing_counts:
        LOGGER.warning(
            "Candidate forward-return coverage gaps: %s",
            ", ".join(f"{horizon}d:{reason}={count}" for (horizon, reason), count in sorted(missing_counts.items())),
        )
    return observations


def build_candidate_summary(
    observations: list[dict[str, Any]],
    *,
    dates: list[str],
    horizons: list[int],
    top_n_values: list[int],
    train_fraction: float,
    lcb_z: float,
) -> list[dict[str, Any]]:
    _ = dates
    split_maps = split_maps_for_baseline_candidate_observations(
        observations,
        horizons=horizons,
        train_fraction=train_fraction,
    )
    grouped: dict[tuple[str, int, int, str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in observations:
        if row.get("net_return") is None:
            continue
        horizon = int(row["horizon_days"])
        split = split_maps[horizon].get(str(row.get("asof_date") or ""), "")
        row["evaluation_split"] = split
        for split_name in ("all", split):
            if not split_name:
                continue
            for cohort in (str(row.get("biotech_primary_cohort") or ""), "all_cohorts"):
                if cohort:
                    grouped[
                        (
                            str(row["candidate_id"]),
                            int(row["top_n"]),
                            horizon,
                            split_name,
                            str(row.get("selection_scope") or "global_topn"),
                            cohort,
                        )
                    ].append(row)

    baseline_key_prefix = "baseline_current_config"
    summary_rows: list[dict[str, Any]] = []
    for (candidate_id, top_n, horizon, split, selection_scope, cohort), rows in sorted(grouped.items()):
        values = [float(row["net_return"]) for row in rows]
        metrics = summarize_returns(values, lcb_z=lcb_z)
        baseline_rows = grouped.get((baseline_key_prefix, top_n, horizon, split, selection_scope, cohort), [])
        baseline_values = [float(row["net_return"]) for row in baseline_rows]
        baseline_metrics = summarize_returns(baseline_values, lcb_z=lcb_z)
        candidate_date_means: dict[str, float] = {}
        baseline_date_means: dict[str, float] = {}
        for candidate_group, target in ((rows, candidate_date_means), (baseline_rows, baseline_date_means)):
            by_date: dict[str, list[float]] = defaultdict(list)
            for row in candidate_group:
                by_date[str(row.get("asof_date") or "")].append(float(row["net_return"]))
            target.update({asof: mean(date_values) for asof, date_values in by_date.items()})
        common_dates = sorted(set(candidate_date_means).intersection(baseline_date_means))
        date_improvement_rate = (
            sum(1 for asof in common_dates if candidate_date_means[asof] > baseline_date_means[asof]) / len(common_dates) * 100.0
            if common_dates
            else 0.0
        )
        rank_values = [float(row["baseline_rank"]) for row in rows]
        candidate_rank_values = [float(row["candidate_rank"]) for row in rows]
        score_values = [float(row.get("opportunity_score") or 0.0) for row in rows]
        adjusted_values = [float(row.get("adjusted_opportunity_score") or 0.0) for row in rows]
        target_score_deltas = [
            float(row.get("score_delta") or 0.0)
            for row in rows
            if str(row.get("biotech_primary_cohort") or "") == str(row.get("target_cohort") or "")
        ]
        target_selected = sum(1 for row in rows if str(row.get("biotech_primary_cohort") or "") == str(row.get("target_cohort") or ""))
        summary_rows.append(
            {
                "candidate_id": candidate_id,
                "candidate_description": rows[0].get("candidate_description", "") if rows else "",
                "target_cohort": rows[0].get("target_cohort", "") if rows else "",
                "selection_scope": selection_scope,
                "evaluation_split": split,
                "top_n": top_n,
                "horizon_days": horizon,
                "biotech_primary_cohort": cohort,
                "selected_n": len(rows),
                "asof_dates": len({str(row.get("asof_date") or "") for row in rows}),
                "unique_tickers": len({str(row.get("ticker") or "") for row in rows}),
                "target_cohort_selected_n": target_selected,
                "target_cohort_share_pct": round((target_selected / len(rows) * 100.0) if rows else 0.0, 6),
                "avg_rank": round(mean(rank_values), 6) if rank_values else 0.0,
                "avg_candidate_rank": round(mean(candidate_rank_values), 6) if candidate_rank_values else 0.0,
                "avg_opportunity_score": round(mean(score_values), 6) if score_values else 0.0,
                "avg_adjusted_opportunity_score": round(mean(adjusted_values), 6) if adjusted_values else 0.0,
                "avg_target_score_delta": round(mean(target_score_deltas), 6) if target_score_deltas else 0.0,
                **{key: round(value, 6) for key, value in metrics.items()},
                "baseline_lcb_return_pct": round(baseline_metrics["lcb_return_pct"], 6),
                "lcb_delta_pct": round(metrics["lcb_return_pct"] - baseline_metrics["lcb_return_pct"], 6),
                "baseline_profit_factor": round(baseline_metrics["profit_factor"], 6),
                "profit_factor_delta": round(metrics["profit_factor"] - baseline_metrics["profit_factor"], 6),
                "baseline_large_loss_20pct_rate_pct": round(baseline_metrics["large_loss_20pct_rate_pct"], 6),
                "large_loss_20pct_rate_delta_pct": round(
                    metrics["large_loss_20pct_rate_pct"] - baseline_metrics["large_loss_20pct_rate_pct"],
                    6,
                ),
                "date_improvement_rate_pct": round(date_improvement_rate, 6),
                "top_tickers": top_ticker_text(rows),
            }
        )
    return summary_rows


def build_ticker_breadth_rows(
    observations: list[dict[str, Any]],
    *,
    dates: list[str],
    horizons: list[int],
    top_n_values: list[int],
    train_fraction: float,
    target_cohort: str,
) -> list[dict[str, Any]]:
    _ = dates
    split_maps = split_maps_for_baseline_candidate_observations(
        observations,
        horizons=horizons,
        train_fraction=train_fraction,
    )
    values_by_key: dict[tuple[str, int, int, str, str, str, str], list[float]] = defaultdict(list)
    for row in observations:
        if row.get("net_return") is None:
            continue
        horizon = int(row["horizon_days"])
        split = split_maps[horizon].get(str(row.get("asof_date") or ""), "")
        scopes = ["all_cohorts"]
        if str(row.get("biotech_primary_cohort") or "") == target_cohort:
            scopes.append("target_cohort")
        for split_name in ("all", split):
            if not split_name:
                continue
            for scope in scopes:
                values_by_key[
                    (
                        str(row["candidate_id"]),
                        int(row["top_n"]),
                        horizon,
                        split_name,
                        str(row.get("selection_scope") or "global_topn"),
                        scope,
                        normalize_ticker(row.get("ticker")),
                    )
                ].append(float(row["net_return"]))

    candidate_ids = sorted({key[0] for key in values_by_key if key[0] != "baseline_current_config"})
    rows: list[dict[str, Any]] = []
    for candidate_id in candidate_ids:
        for top_n in top_n_values:
            for horizon in horizons:
                for split in ("all", "train", "test"):
                    for selection_scope in ("global_topn", "target_cohort_topn"):
                        for scope in ("all_cohorts", "target_cohort"):
                            if selection_scope == "target_cohort_topn" and scope == "all_cohorts":
                                continue
                            baseline_tickers = {
                                key[6]
                                for key in values_by_key
                                if key[:6]
                                == ("baseline_current_config", top_n, horizon, split, selection_scope, scope)
                            }
                            candidate_tickers = {
                                key[6]
                                for key in values_by_key
                                if key[:6] == (candidate_id, top_n, horizon, split, selection_scope, scope)
                            }
                            comparable_tickers = sorted((baseline_tickers | candidate_tickers) - {""})
                            if not comparable_tickers:
                                continue
                            deltas: list[float] = []
                            ticker_delta_pairs: list[tuple[str, float]] = []
                            for ticker in comparable_tickers:
                                baseline_value = mean(
                                    values_by_key.get(
                                        (
                                            "baseline_current_config",
                                            top_n,
                                            horizon,
                                            split,
                                            selection_scope,
                                            scope,
                                            ticker,
                                        ),
                                        [],
                                    )
                                )
                                candidate_value = mean(
                                    values_by_key.get(
                                        (candidate_id, top_n, horizon, split, selection_scope, scope, ticker),
                                        [],
                                    )
                                )
                                delta = candidate_value - baseline_value
                                deltas.append(delta)
                                ticker_delta_pairs.append((ticker, delta))
                            improved = sum(1 for delta in deltas if delta > 0.0)
                            harmed = sum(1 for delta in deltas if delta < 0.0)
                            top_delta_text = "|".join(
                                f"{ticker}:{delta * 100.0:.2f}"
                                for ticker, delta in sorted(ticker_delta_pairs, key=lambda item: -abs(item[1]))[:10]
                            )
                            rows.append(
                                {
                                    "candidate_id": candidate_id,
                                    "target_cohort": target_cohort,
                                    "selection_scope": selection_scope,
                                    "evaluation_split": split,
                                    "top_n": top_n,
                                    "horizon_days": horizon,
                                    "scope": scope,
                                    "comparable_unique_tickers": len(comparable_tickers),
                                    "improved_unique_tickers": improved,
                                    "improved_unique_ticker_rate_pct": round(improved / len(comparable_tickers) * 100.0, 6),
                                    "harmed_unique_tickers": harmed,
                                    "harmed_unique_ticker_rate_pct": round(harmed / len(comparable_tickers) * 100.0, 6),
                                    "median_unique_ticker_return_delta_pct": round(percentile(deltas, 0.50) * 100.0, 6),
                                    "mean_unique_ticker_return_delta_pct": round(mean(deltas) * 100.0, 6),
                                    "top_ticker_return_deltas_pct": top_delta_text,
                                }
                            )
    return rows


def choose_target_cohort(priority_rows: list[dict[str, Any]], requested: str | None) -> str:
    if requested and str(requested).strip():
        return str(requested).strip()
    for row in priority_rows:
        if str(row.get("recommended_next_step") or "") == "calibrate_first_pass":
            cohort = str(row.get("biotech_primary_cohort") or "").strip()
            if cohort:
                return cohort
    if priority_rows:
        return str(priority_rows[0].get("biotech_primary_cohort") or "").strip()
    raise ValueError("Unable to select target cohort; priority table is empty")


def promotion_threshold(config: dict[str, Any], path: str, default: float) -> float:
    return float(cfg_get(config, f"biotech_scoring.cohort_config_overrides.promotion_policy.{path}", default))


def build_promotion_recommendations(
    *,
    candidate_summary_rows: list[dict[str, Any]],
    ticker_breadth_rows: list[dict[str, Any]],
    candidates: list[OverrideCandidate],
    target_cohort: str,
    label_rows: list[dict[str, Any]],
    thresholds: Thresholds,
    config: dict[str, Any],
    primary_top_n: int,
    primary_horizon: int,
) -> list[dict[str, Any]]:
    summary_by_key = {
        (
            str(row.get("candidate_id") or ""),
            int(row.get("top_n") or 0),
            int(row.get("horizon_days") or 0),
            str(row.get("evaluation_split") or ""),
            str(row.get("selection_scope") or ""),
            str(row.get("biotech_primary_cohort") or ""),
        ): row
        for row in candidate_summary_rows
    }
    breadth_by_key = {
        (
            str(row.get("candidate_id") or ""),
            int(row.get("top_n") or 0),
            int(row.get("horizon_days") or 0),
            str(row.get("evaluation_split") or ""),
            str(row.get("selection_scope") or ""),
            str(row.get("scope") or ""),
        ): row
        for row in ticker_breadth_rows
    }
    label_by_cohort = {str(row.get("biotech_primary_cohort") or ""): row for row in label_rows}
    label_blockers = sparse_blockers(label_by_cohort.get(target_cohort, {}), thresholds)
    min_comparable = int(cfg_get(config, "biotech_scoring.cohort_config_overrides.min_comparable_unique_tickers", 5))
    max_large_loss = promotion_threshold(config, "within_cohort.max_large_loss_20pct_rate_increase", 3.0)
    rows: list[dict[str, Any]] = []
    for candidate in candidates:
        if candidate.candidate_id == "baseline_current_config":
            continue
        target_row = summary_by_key.get(
            (candidate.candidate_id, primary_top_n, primary_horizon, "test", "target_cohort_topn", target_cohort),
            {},
        )
        global_top20 = summary_by_key.get(
            (candidate.candidate_id, primary_top_n, primary_horizon, "test", "global_topn", "all_cohorts"),
            {},
        )
        top10_top_n = 10 if primary_top_n >= 10 else primary_top_n
        global_top10 = summary_by_key.get(
            (candidate.candidate_id, top10_top_n, primary_horizon, "test", "global_topn", "all_cohorts"),
            {},
        )
        breadth = breadth_by_key.get(
            (candidate.candidate_id, primary_top_n, primary_horizon, "test", "target_cohort_topn", "target_cohort"),
            {},
        )
        blockers: list[str] = []
        blockers.extend(f"label:{blocker}" for blocker in label_blockers)
        if not target_row:
            blockers.append("missing_target_cohort_test_summary")
        if not global_top20:
            blockers.append("missing_global_top20_test_summary")
        if float(target_row.get("lcb_delta_pct") or 0.0) < promotion_threshold(config, "within_cohort.min_lcb_improvement_pct", 0.50):
            blockers.append("within_lcb_delta_below_min")
        if float(target_row.get("profit_factor_delta") or 0.0) < promotion_threshold(config, "within_cohort.min_profit_factor_improvement", 0.10):
            blockers.append("within_profit_factor_delta_below_min")
        if float(target_row.get("large_loss_20pct_rate_delta_pct") or 0.0) > max_large_loss:
            blockers.append("within_large_loss_rate_delta_above_max")
        if int(breadth.get("comparable_unique_tickers") or 0) < min_comparable:
            blockers.append("comparable_unique_tickers_below_min")
        if float(breadth.get("improved_unique_ticker_rate_pct") or 0.0) < promotion_threshold(
            config,
            "within_cohort.min_unique_ticker_improvement_rate_pct",
            60.0,
        ):
            blockers.append("unique_ticker_improvement_rate_below_min")
        if float(breadth.get("median_unique_ticker_return_delta_pct") or 0.0) < promotion_threshold(
            config,
            "within_cohort.min_median_unique_ticker_return_delta_pct",
            0.0,
        ):
            blockers.append("median_unique_ticker_delta_below_min")
        if float(breadth.get("harmed_unique_ticker_rate_pct") or 0.0) > promotion_threshold(
            config,
            "within_cohort.max_unique_ticker_harm_rate_pct",
            40.0,
        ):
            blockers.append("unique_ticker_harm_rate_above_max")
        for short_horizon, max_degradation in (
            (20, promotion_threshold(config, "within_cohort.max_20d_lcb_degradation_pct", 2.0)),
            (60, promotion_threshold(config, "within_cohort.max_60d_lcb_degradation_pct", 2.0)),
        ):
            short_row = summary_by_key.get(
                (candidate.candidate_id, primary_top_n, short_horizon, "test", "target_cohort_topn", target_cohort),
                {},
            )
            if short_row and float(short_row.get("lcb_delta_pct") or 0.0) < -max_degradation:
                blockers.append(f"within_{short_horizon}d_lcb_degradation_above_max")
        if float(global_top10.get("lcb_delta_pct") or 0.0) < promotion_threshold(
            config,
            "global_validation.min_top10_lcb_delta_pct",
            0.0,
        ):
            blockers.append("global_top10_lcb_delta_below_min")
        if float(global_top20.get("lcb_delta_pct") or 0.0) < promotion_threshold(
            config,
            "global_validation.min_top20_lcb_delta_pct",
            0.0,
        ):
            blockers.append("global_top20_lcb_delta_below_min")
        if float(global_top10.get("profit_factor_delta") or 0.0) < promotion_threshold(
            config,
            "global_validation.min_top10_profit_factor_delta",
            0.0,
        ):
            blockers.append("global_top10_profit_factor_delta_below_min")
        if float(global_top20.get("profit_factor_delta") or 0.0) < promotion_threshold(
            config,
            "global_validation.min_top20_profit_factor_delta",
            0.0,
        ):
            blockers.append("global_top20_profit_factor_delta_below_min")
        if float(global_top10.get("large_loss_20pct_rate_delta_pct") or 0.0) > promotion_threshold(
            config,
            "global_validation.max_top10_large_loss_20pct_rate_increase",
            3.0,
        ):
            blockers.append("global_top10_large_loss_rate_delta_above_max")
        if float(global_top20.get("large_loss_20pct_rate_delta_pct") or 0.0) > promotion_threshold(
            config,
            "global_validation.max_top20_large_loss_20pct_rate_increase",
            3.0,
        ):
            blockers.append("global_top20_large_loss_rate_delta_above_max")
        if abs(float(target_row.get("avg_target_score_delta") or 0.0)) > promotion_threshold(
            config,
            "score_inflation.max_cohort_avg_score_delta",
            4.0,
        ):
            blockers.append("cohort_avg_score_delta_above_max")
        if float(global_top10.get("target_cohort_share_pct") or 0.0) > promotion_threshold(
            config,
            "score_inflation.max_single_cohort_top10_share_pct",
            40.0,
        ):
            blockers.append("single_cohort_top10_share_above_max")
        if float(global_top20.get("target_cohort_share_pct") or 0.0) > promotion_threshold(
            config,
            "score_inflation.max_single_cohort_top20_share_pct",
            35.0,
        ):
            blockers.append("single_cohort_top20_share_above_max")
        if float(global_top20.get("top3_gain_contribution_pct") or 0.0) > promotion_threshold(
            config,
            "robustness.max_top3_gain_contribution_pct",
            50.0,
        ):
            blockers.append("top3_gain_contribution_above_max")
        status = "promote" if not blockers else "reject"
        if blockers and not label_blockers and global_top20 and float(global_top20.get("lcb_delta_pct") or 0.0) >= 0.0:
            status = "shadow"
        rows.append(
            {
                "candidate_id": candidate.candidate_id,
                "candidate_description": candidate.description,
                "target_cohort": target_cohort,
                "status": status,
                "config_action": "manual_review_only" if status == "promote" else "do_not_promote",
                "primary_top_n": primary_top_n,
                "primary_horizon_days": primary_horizon,
                "within_selection_scope": "target_cohort_topn",
                "global_selection_scope": "global_topn",
                "evaluation_split": "test",
                "within_lcb_delta_pct": round(float(target_row.get("lcb_delta_pct") or 0.0), 6),
                "within_profit_factor_delta": round(float(target_row.get("profit_factor_delta") or 0.0), 6),
                "within_large_loss_20pct_rate_delta_pct": round(
                    float(target_row.get("large_loss_20pct_rate_delta_pct") or 0.0),
                    6,
                ),
                "comparable_unique_tickers": int(breadth.get("comparable_unique_tickers") or 0),
                "improved_unique_ticker_rate_pct": round(float(breadth.get("improved_unique_ticker_rate_pct") or 0.0), 6),
                "median_unique_ticker_return_delta_pct": round(
                    float(breadth.get("median_unique_ticker_return_delta_pct") or 0.0),
                    6,
                ),
                "harmed_unique_ticker_rate_pct": round(float(breadth.get("harmed_unique_ticker_rate_pct") or 0.0), 6),
                "global_top10_lcb_delta_pct": round(float(global_top10.get("lcb_delta_pct") or 0.0), 6),
                "global_top20_lcb_delta_pct": round(float(global_top20.get("lcb_delta_pct") or 0.0), 6),
                "global_top10_profit_factor_delta": round(float(global_top10.get("profit_factor_delta") or 0.0), 6),
                "global_top20_profit_factor_delta": round(float(global_top20.get("profit_factor_delta") or 0.0), 6),
                "global_top20_large_loss_20pct_rate_delta_pct": round(
                    float(global_top20.get("large_loss_20pct_rate_delta_pct") or 0.0),
                    6,
                ),
                "avg_target_score_delta": round(float(target_row.get("avg_target_score_delta") or 0.0), 6),
                "target_top10_share_pct": round(float(global_top10.get("target_cohort_share_pct") or 0.0), 6),
                "target_top20_share_pct": round(float(global_top20.get("target_cohort_share_pct") or 0.0), 6),
                "global_top20_top3_gain_contribution_pct": round(
                    float(global_top20.get("top3_gain_contribution_pct") or 0.0),
                    6,
                ),
                "label_blockers": "|".join(label_blockers),
                "promotion_blockers": "|".join(blockers),
                "recommendation": (
                    "Candidate passed report-only gates; review before copying into config.yaml."
                    if status == "promote"
                    else "Keep as shadow evidence only."
                    if status == "shadow"
                    else "Reject for production promotion."
                ),
            }
        )
    rows.sort(
        key=lambda row: (
            {"promote": 0, "shadow": 1, "reject": 2}.get(str(row.get("status")), 9),
            -float(row.get("global_top20_lcb_delta_pct") or 0.0),
            -float(row.get("within_lcb_delta_pct") or 0.0),
            str(row.get("candidate_id") or ""),
        )
    )
    return rows


def _split_for_observation(
    row: dict[str, Any],
    split_maps: dict[int, dict[str, str]],
) -> tuple[int, str]:
    horizon = int(row.get("horizon_days") or 0)
    split = split_maps.get(horizon, {}).get(str(row.get("asof_date") or ""), "")
    return horizon, split


def build_rank_movement_diagnostics(
    observations: list[dict[str, Any]],
    *,
    horizons: list[int],
    train_fraction: float,
) -> list[dict[str, Any]]:
    split_maps = split_maps_for_baseline_candidate_observations(
        observations,
        horizons=horizons,
        train_fraction=train_fraction,
    )
    selected: dict[tuple[str, int, int, str, str], dict[str, dict[str, dict[str, Any]]]] = defaultdict(
        lambda: defaultdict(dict)
    )
    descriptions: dict[str, str] = {}
    target_by_candidate: dict[str, str] = {}
    for row in observations:
        candidate_id = str(row.get("candidate_id") or "")
        if not candidate_id:
            continue
        descriptions[candidate_id] = str(row.get("candidate_description") or "")
        target_by_candidate[candidate_id] = str(row.get("target_cohort") or "")
        horizon, split = _split_for_observation(row, split_maps)
        if not split:
            continue
        ticker = normalize_ticker(row.get("ticker"))
        if not ticker:
            continue
        for split_name in ("all", split):
            key = (
                candidate_id,
                int(row.get("top_n") or 0),
                horizon,
                split_name,
                str(row.get("selection_scope") or "global_topn"),
            )
            selected[key][str(row.get("asof_date") or "")][ticker] = row

    candidate_keys = sorted(key for key in selected if key[0] != "baseline_current_config")
    rows: list[dict[str, Any]] = []
    for candidate_id, top_n, horizon, split, selection_scope in candidate_keys:
        baseline_by_date = selected.get(("baseline_current_config", top_n, horizon, split, selection_scope), {})
        candidate_by_date = selected.get((candidate_id, top_n, horizon, split, selection_scope), {})
        common_dates = sorted(set(baseline_by_date).intersection(candidate_by_date))
        entered_counter: Counter[str] = Counter()
        exited_counter: Counter[str] = Counter()
        entered_instances = 0
        exited_instances = 0
        unchanged_instances = 0
        rank_changed_instances = 0
        no_movement_dates = 0
        rank_deltas: list[float] = []
        score_deltas: list[float] = []
        adjusted_score_deltas: list[float] = []
        for asof in common_dates:
            baseline_rows = baseline_by_date.get(asof, {})
            candidate_rows = candidate_by_date.get(asof, {})
            baseline_tickers = set(baseline_rows)
            candidate_tickers = set(candidate_rows)
            entered = candidate_tickers - baseline_tickers
            exited = baseline_tickers - candidate_tickers
            common = baseline_tickers.intersection(candidate_tickers)
            if not entered and not exited:
                no_movement_dates += 1
            entered_instances += len(entered)
            exited_instances += len(exited)
            unchanged_instances += len(common)
            entered_counter.update(entered)
            exited_counter.update(exited)
            for ticker in common:
                baseline_rank = to_float(baseline_rows[ticker].get("candidate_rank"), 0.0) or 0.0
                candidate_rank = to_float(candidate_rows[ticker].get("candidate_rank"), 0.0) or 0.0
                rank_delta = candidate_rank - baseline_rank
                rank_deltas.append(abs(rank_delta))
                if abs(rank_delta) > 1e-9:
                    rank_changed_instances += 1
                score_delta = to_float(candidate_rows[ticker].get("score_delta"), 0.0) or 0.0
                score_deltas.append(score_delta)
                adjusted_delta = (
                    (to_float(candidate_rows[ticker].get("adjusted_opportunity_score"), 0.0) or 0.0)
                    - (to_float(candidate_rows[ticker].get("opportunity_score"), 0.0) or 0.0)
                )
                adjusted_score_deltas.append(adjusted_delta)
        selection_slots = len(common_dates) * top_n
        rank_movement_intensity = (
            (entered_instances + exited_instances) / selection_slots * 100.0 if selection_slots else 0.0
        )
        rows.append(
            {
                "candidate_id": candidate_id,
                "candidate_description": descriptions.get(candidate_id, ""),
                "target_cohort": target_by_candidate.get(candidate_id, ""),
                "selection_scope": selection_scope,
                "evaluation_split": split,
                "top_n": top_n,
                "horizon_days": horizon,
                "dates_compared": len(common_dates),
                "selection_slots": selection_slots,
                "entered_instances": entered_instances,
                "exited_instances": exited_instances,
                "unchanged_instances": unchanged_instances,
                "rank_changed_common_instances": rank_changed_instances,
                "rank_movement_intensity_pct": round(rank_movement_intensity, 6),
                "no_rank_movement_date_pct": round(
                    (no_movement_dates / len(common_dates) * 100.0) if common_dates else 0.0,
                    6,
                ),
                "avg_abs_common_rank_delta": round(mean(rank_deltas), 6) if rank_deltas else 0.0,
                "avg_common_score_delta": round(mean(score_deltas), 6) if score_deltas else 0.0,
                "avg_common_adjusted_score_delta": round(mean(adjusted_score_deltas), 6)
                if adjusted_score_deltas
                else 0.0,
                "unique_entered_tickers": len(entered_counter),
                "unique_exited_tickers": len(exited_counter),
                "top_entered_tickers": "|".join(f"{ticker}:{count}" for ticker, count in entered_counter.most_common(10)),
                "top_exited_tickers": "|".join(f"{ticker}:{count}" for ticker, count in exited_counter.most_common(10)),
                "movement_diagnosis": "too_weak_to_change_ranks"
                if rank_movement_intensity < 5.0
                else "moves_ranks_but_breadth_must_validate",
            }
        )
    return rows


def build_ticker_return_diagnostics(
    observations: list[dict[str, Any]],
    *,
    horizons: list[int],
    top_n_values: list[int],
    train_fraction: float,
    target_cohort: str,
) -> list[dict[str, Any]]:
    split_maps = split_maps_for_baseline_candidate_observations(
        observations,
        horizons=horizons,
        train_fraction=train_fraction,
    )
    values: dict[tuple[str, int, int, str, str, str, str], list[float]] = defaultdict(list)
    descriptions: dict[str, str] = {}
    for row in observations:
        if row.get("net_return") is None:
            continue
        candidate_id = str(row.get("candidate_id") or "")
        descriptions[candidate_id] = str(row.get("candidate_description") or "")
        horizon, split = _split_for_observation(row, split_maps)
        if not split:
            continue
        ticker = normalize_ticker(row.get("ticker"))
        if not ticker:
            continue
        scopes = ["all_cohorts"]
        if str(row.get("biotech_primary_cohort") or "") == target_cohort:
            scopes.append("target_cohort")
        for split_name in ("all", split):
            for scope in scopes:
                values[
                    (
                        candidate_id,
                        int(row.get("top_n") or 0),
                        horizon,
                        split_name,
                        str(row.get("selection_scope") or "global_topn"),
                        scope,
                        ticker,
                    )
                ].append(float(row["net_return"]))

    rows: list[dict[str, Any]] = []
    candidate_ids = sorted({key[0] for key in values if key[0] != "baseline_current_config"})
    for candidate_id in candidate_ids:
        for top_n in top_n_values:
            for horizon in horizons:
                for split in ("all", "train", "test"):
                    for selection_scope in ("global_topn", "target_cohort_topn"):
                        for scope in ("all_cohorts", "target_cohort"):
                            if selection_scope == "target_cohort_topn" and scope == "all_cohorts":
                                continue
                            baseline_tickers = {
                                key[6]
                                for key in values
                                if key[:6]
                                == ("baseline_current_config", top_n, horizon, split, selection_scope, scope)
                            }
                            candidate_tickers = {
                                key[6]
                                for key in values
                                if key[:6] == (candidate_id, top_n, horizon, split, selection_scope, scope)
                            }
                            for ticker in sorted((baseline_tickers | candidate_tickers) - {""}):
                                baseline_values = values.get(
                                    (
                                        "baseline_current_config",
                                        top_n,
                                        horizon,
                                        split,
                                        selection_scope,
                                        scope,
                                        ticker,
                                    ),
                                    [],
                                )
                                candidate_values = values.get(
                                    (candidate_id, top_n, horizon, split, selection_scope, scope, ticker),
                                    [],
                                )
                                baseline_mean = mean(baseline_values)
                                candidate_mean = mean(candidate_values)
                                delta = candidate_mean - baseline_mean
                                if baseline_values and not candidate_values:
                                    status = "removed"
                                elif candidate_values and not baseline_values:
                                    status = "added"
                                elif delta > 1e-9:
                                    status = "improved"
                                elif delta < -1e-9:
                                    status = "worsened"
                                else:
                                    status = "unchanged"
                                rows.append(
                                    {
                                        "candidate_id": candidate_id,
                                        "candidate_description": descriptions.get(candidate_id, ""),
                                        "target_cohort": target_cohort,
                                        "selection_scope": selection_scope,
                                        "scope": scope,
                                        "evaluation_split": split,
                                        "top_n": top_n,
                                        "horizon_days": horizon,
                                        "ticker": ticker,
                                        "baseline_selected_count": len(baseline_values),
                                        "candidate_selected_count": len(candidate_values),
                                        "baseline_mean_return_pct": round(baseline_mean * 100.0, 6),
                                        "candidate_mean_return_pct": round(candidate_mean * 100.0, 6),
                                        "return_delta_pct": round(delta * 100.0, 6),
                                        "ticker_outcome": status,
                                    }
                                )
    return rows


COMPONENT_ATTRIBUTION_SCORE_FIELDS = [
    "component_clinical_opportunity_score",
    "component_commercial_value_score",
    "component_forward_guidance_score",
    "component_valuation_score",
    "component_upside_capacity_score",
    "component_institutional_upside_score",
    "component_financial_quality_score",
    "component_momentum_score",
    "component_risk_score",
]


def build_component_attribution_rows(
    observations: list[dict[str, Any]],
    *,
    horizons: list[int],
    top_n_values: list[int],
    train_fraction: float,
    target_cohort: str,
) -> list[dict[str, Any]]:
    split_maps = split_maps_for_baseline_candidate_observations(
        observations,
        horizons=horizons,
        train_fraction=train_fraction,
    )
    by_key: dict[tuple[str, int, int, str, str, str, str], list[dict[str, Any]]] = defaultdict(list)
    descriptions: dict[str, str] = {}
    for row in observations:
        if row.get("net_return") is None:
            continue
        candidate_id = str(row.get("candidate_id") or "")
        descriptions[candidate_id] = str(row.get("candidate_description") or "")
        horizon, split = _split_for_observation(row, split_maps)
        if not split:
            continue
        ticker = normalize_ticker(row.get("ticker"))
        if not ticker:
            continue
        scopes = ["all_cohorts"]
        if str(row.get("biotech_primary_cohort") or "") == target_cohort:
            scopes.append("target_cohort")
        for split_name in ("all", split):
            for scope in scopes:
                by_key[
                    (
                        candidate_id,
                        int(row.get("top_n") or 0),
                        horizon,
                        split_name,
                        str(row.get("selection_scope") or "global_topn"),
                        scope,
                        ticker,
                    )
                ].append(row)

    candidate_ids = sorted({key[0] for key in by_key if key[0] != "baseline_current_config"})
    rows: list[dict[str, Any]] = []
    for candidate_id in candidate_ids:
        for top_n in top_n_values:
            for horizon in horizons:
                for split in ("all", "train", "test"):
                    for selection_scope in ("global_topn", "target_cohort_topn"):
                        for scope in ("all_cohorts", "target_cohort"):
                            if selection_scope == "target_cohort_topn" and scope == "all_cohorts":
                                continue
                            baseline_tickers = {
                                key[6]
                                for key in by_key
                                if key[:6]
                                == ("baseline_current_config", top_n, horizon, split, selection_scope, scope)
                            }
                            candidate_tickers = {
                                key[6]
                                for key in by_key
                                if key[:6] == (candidate_id, top_n, horizon, split, selection_scope, scope)
                            }
                            ticker_records: list[dict[str, Any]] = []
                            for ticker in sorted((baseline_tickers | candidate_tickers) - {""}):
                                baseline_rows = by_key.get(
                                    (
                                        "baseline_current_config",
                                        top_n,
                                        horizon,
                                        split,
                                        selection_scope,
                                        scope,
                                        ticker,
                                    ),
                                    [],
                                )
                                candidate_rows = by_key.get(
                                    (candidate_id, top_n, horizon, split, selection_scope, scope, ticker),
                                    [],
                                )
                                baseline_return = mean([float(row["net_return"]) for row in baseline_rows])
                                candidate_return = mean([float(row["net_return"]) for row in candidate_rows])
                                return_delta = candidate_return - baseline_return
                                if baseline_rows and not candidate_rows:
                                    outcome = "removed"
                                    component_source_rows = baseline_rows
                                elif candidate_rows and not baseline_rows:
                                    outcome = "added"
                                    component_source_rows = candidate_rows
                                else:
                                    component_source_rows = candidate_rows or baseline_rows
                                    if return_delta > 1e-9:
                                        outcome = "improved"
                                    elif return_delta < -1e-9:
                                        outcome = "worsened"
                                    else:
                                        outcome = "unchanged"
                                ticker_records.append(
                                    {
                                        "ticker": ticker,
                                        "outcome": outcome,
                                        "baseline_selected_count": len(baseline_rows),
                                        "candidate_selected_count": len(candidate_rows),
                                        "baseline_return": baseline_return,
                                        "candidate_return": candidate_return,
                                        "return_delta": return_delta,
                                        "baseline_rank": mean(
                                            [
                                                to_float(row.get("candidate_rank"), 0.0) or 0.0
                                                for row in baseline_rows
                                            ]
                                        ),
                                        "candidate_rank": mean(
                                            [
                                                to_float(row.get("candidate_rank"), 0.0) or 0.0
                                                for row in candidate_rows
                                            ]
                                        ),
                                        "score_delta": mean(
                                            [to_float(row.get("score_delta"), 0.0) or 0.0 for row in candidate_rows]
                                        ),
                                        "adjusted_score_delta": mean(
                                            [
                                                (to_float(row.get("adjusted_opportunity_score"), 0.0) or 0.0)
                                                - (to_float(row.get("opportunity_score"), 0.0) or 0.0)
                                                for row in candidate_rows
                                            ]
                                        ),
                                        "components": {
                                            field: mean(
                                                [
                                                    to_float(component_row.get(field), 0.0) or 0.0
                                                    for component_row in component_source_rows
                                                ]
                                            )
                                            for field in COMPONENT_ATTRIBUTION_SCORE_FIELDS
                                        },
                                    }
                                )
                            if not ticker_records:
                                continue
                            for outcome in ("all_comparable", "improved", "worsened", "unchanged", "added", "removed"):
                                outcome_records = (
                                    ticker_records
                                    if outcome == "all_comparable"
                                    else [record for record in ticker_records if record["outcome"] == outcome]
                                )
                                if not outcome_records:
                                    continue
                                return_deltas = [float(record["return_delta"]) for record in outcome_records]
                                baseline_counts = [float(record["baseline_selected_count"]) for record in outcome_records]
                                candidate_counts = [float(record["candidate_selected_count"]) for record in outcome_records]
                                baseline_ranks = [
                                    float(record["baseline_rank"])
                                    for record in outcome_records
                                    if float(record["baseline_rank"]) > 0.0
                                ]
                                candidate_ranks = [
                                    float(record["candidate_rank"])
                                    for record in outcome_records
                                    if float(record["candidate_rank"]) > 0.0
                                ]
                                attribution_row: dict[str, Any] = {
                                    "candidate_id": candidate_id,
                                    "candidate_description": descriptions.get(candidate_id, ""),
                                    "target_cohort": target_cohort,
                                    "selection_scope": selection_scope,
                                    "scope": scope,
                                    "evaluation_split": split,
                                    "top_n": top_n,
                                    "horizon_days": horizon,
                                    "ticker_outcome": outcome,
                                    "ticker_count": len(outcome_records),
                                    "baseline_total_selected_count": int(sum(baseline_counts)),
                                    "candidate_total_selected_count": int(sum(candidate_counts)),
                                    "mean_return_delta_pct": round(mean(return_deltas) * 100.0, 6),
                                    "median_return_delta_pct": round(percentile(return_deltas, 0.50) * 100.0, 6),
                                    "avg_baseline_rank": round(mean(baseline_ranks), 6) if baseline_ranks else 0.0,
                                    "avg_candidate_rank": round(mean(candidate_ranks), 6) if candidate_ranks else 0.0,
                                    "avg_rank_delta": round(
                                        (mean(candidate_ranks) if candidate_ranks else 0.0)
                                        - (mean(baseline_ranks) if baseline_ranks else 0.0),
                                        6,
                                    ),
                                    "avg_score_delta": round(
                                        mean([float(record["score_delta"]) for record in outcome_records]),
                                        6,
                                    ),
                                    "avg_adjusted_score_delta": round(
                                        mean([float(record["adjusted_score_delta"]) for record in outcome_records]),
                                        6,
                                    ),
                                    "top_tickers": "|".join(
                                        f"{record['ticker']}:{float(record['return_delta']) * 100.0:.2f}"
                                        for record in sorted(
                                            outcome_records,
                                            key=lambda item: abs(float(item["return_delta"])),
                                            reverse=True,
                                        )[:10]
                                    ),
                                }
                                for field in COMPONENT_ATTRIBUTION_SCORE_FIELDS:
                                    attribution_row[f"avg_{field}"] = round(
                                        mean([float(record["components"].get(field, 0.0)) for record in outcome_records]),
                                        6,
                                    )
                                rows.append(attribution_row)
    return rows


def blocker_failure_category(blockers: str, *, movement_intensity: float, comparable_tickers: int) -> str:
    blocker_set = {token for token in str(blockers or "").split("|") if token}
    if any(token.startswith("label:") for token in blocker_set):
        return "label_or_sparse_blocked"
    if "comparable_unique_tickers_below_min" in blocker_set or comparable_tickers <= 0:
        return "insufficient_comparable_tickers"
    if "unique_ticker_improvement_rate_below_min" in blocker_set:
        return "weak_rank_movement" if movement_intensity < 5.0 else "rank_movement_not_broadly_profitable"
    if any("single_cohort" in token or "score" in token for token in blocker_set):
        return "concentration_or_score_inflation"
    if any(token.startswith("global_") for token in blocker_set):
        return "global_validation_failure"
    if any(token.startswith("within_") or token.startswith("median_") for token in blocker_set):
        return "within_cohort_return_failure"
    return "passes_report_only_gates" if not blocker_set else "other"


def build_failure_diagnostics(
    *,
    promotion_rows: list[dict[str, Any]],
    rank_movement_rows: list[dict[str, Any]],
    ticker_breadth_rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    movement_by_key = {
        (
            str(row.get("candidate_id") or ""),
            int(row.get("top_n") or 0),
            int(row.get("horizon_days") or 0),
            str(row.get("evaluation_split") or ""),
            str(row.get("selection_scope") or ""),
        ): row
        for row in rank_movement_rows
    }
    breadth_by_key = {
        (
            str(row.get("candidate_id") or ""),
            int(row.get("top_n") or 0),
            int(row.get("horizon_days") or 0),
            str(row.get("evaluation_split") or ""),
            str(row.get("selection_scope") or ""),
            str(row.get("scope") or ""),
        ): row
        for row in ticker_breadth_rows
    }
    rows: list[dict[str, Any]] = []
    for row in promotion_rows:
        candidate_id = str(row.get("candidate_id") or "")
        top_n = int(row.get("primary_top_n") or 0)
        horizon = int(row.get("primary_horizon_days") or 0)
        movement = movement_by_key.get((candidate_id, top_n, horizon, "test", "target_cohort_topn"), {})
        global_movement = movement_by_key.get((candidate_id, top_n, horizon, "test", "global_topn"), {})
        breadth = breadth_by_key.get((candidate_id, top_n, horizon, "test", "target_cohort_topn", "target_cohort"), {})
        movement_intensity = float(movement.get("rank_movement_intensity_pct") or 0.0)
        comparable_tickers = int(breadth.get("comparable_unique_tickers") or row.get("comparable_unique_tickers") or 0)
        category = blocker_failure_category(
            str(row.get("promotion_blockers") or ""),
            movement_intensity=movement_intensity,
            comparable_tickers=comparable_tickers,
        )
        if category == "weak_rank_movement":
            next_action = "expand_first_pass_grid_or_increase_override_strength_in_shadow_only"
        elif category == "rank_movement_not_broadly_profitable":
            next_action = "do_not_expand_same_family_without_new_signal"
        elif category == "concentration_or_score_inflation":
            next_action = "keep_shadow_and_consider_exposure_caps_only_after_breadth_passes"
        elif category == "insufficient_comparable_tickers":
            next_action = "accumulate_more_holdout_dates_or_use_shadow_only"
        elif category == "global_validation_failure":
            next_action = "reject_until_full_universe_top10_top20_improves"
        elif category == "passes_report_only_gates":
            next_action = "manual_review_before_config_promotion"
        else:
            next_action = "review_blockers_before_additional_calibration"
        rows.append(
            {
                "candidate_id": candidate_id,
                "candidate_description": row.get("candidate_description", ""),
                "target_cohort": row.get("target_cohort", ""),
                "status": row.get("status", ""),
                "primary_failure_category": category,
                "recommended_next_action": next_action,
                "promotion_blockers": row.get("promotion_blockers", ""),
                "within_rank_movement_intensity_pct": round(movement_intensity, 6),
                "within_no_rank_movement_date_pct": round(float(movement.get("no_rank_movement_date_pct") or 0.0), 6),
                "within_entered_instances": int(movement.get("entered_instances") or 0),
                "within_exited_instances": int(movement.get("exited_instances") or 0),
                "within_top_entered_tickers": movement.get("top_entered_tickers", ""),
                "within_top_exited_tickers": movement.get("top_exited_tickers", ""),
                "global_rank_movement_intensity_pct": round(
                    float(global_movement.get("rank_movement_intensity_pct") or 0.0),
                    6,
                ),
                "comparable_unique_tickers": comparable_tickers,
                "improved_unique_ticker_rate_pct": row.get("improved_unique_ticker_rate_pct", ""),
                "harmed_unique_ticker_rate_pct": row.get("harmed_unique_ticker_rate_pct", ""),
                "median_unique_ticker_return_delta_pct": row.get("median_unique_ticker_return_delta_pct", ""),
                "within_lcb_delta_pct": row.get("within_lcb_delta_pct", ""),
                "global_top20_lcb_delta_pct": row.get("global_top20_lcb_delta_pct", ""),
                "target_top20_share_pct": row.get("target_top20_share_pct", ""),
            }
        )
    return rows


def rank_values(values: list[float]) -> list[float]:
    indexed = sorted(enumerate(values), key=lambda item: item[1])
    ranks = [0.0] * len(values)
    idx = 0
    while idx < len(indexed):
        end_idx = idx + 1
        while end_idx < len(indexed) and indexed[end_idx][1] == indexed[idx][1]:
            end_idx += 1
        avg_rank = (idx + 1 + end_idx) / 2.0
        for original_idx, _ in indexed[idx:end_idx]:
            ranks[original_idx] = avg_rank
        idx = end_idx
    return ranks


def pearson_corr(x_values: list[float], y_values: list[float]) -> float | None:
    if len(x_values) != len(y_values) or len(x_values) < 3:
        return None
    x_avg = mean(x_values)
    y_avg = mean(y_values)
    x_var = sum((value - x_avg) ** 2 for value in x_values)
    y_var = sum((value - y_avg) ** 2 for value in y_values)
    if x_var <= 0.0 or y_var <= 0.0:
        return None
    numerator = sum((x_value - x_avg) * (y_value - y_avg) for x_value, y_value in zip(x_values, y_values))
    return numerator / math.sqrt(x_var * y_var)


def spearman_corr(x_values: list[float], y_values: list[float]) -> float | None:
    if len(x_values) != len(y_values) or len(x_values) < 3:
        return None
    return pearson_corr(rank_values(x_values), rank_values(y_values))


def component_raw_column(component_name: str) -> str:
    if component_name == "clinical_opportunity":
        return "clinical_opportunity_score"
    if component_name == "commercial_value":
        return "commercial_value_score"
    if component_name == "forward_guidance":
        return "forward_guidance_score"
    if component_name == "valuation":
        return "quality_adjusted_valuation_score"
    if component_name == "upside_capacity":
        return "upside_capacity_score"
    if component_name == "institutional_upside":
        return "institutional_upside_capacity_score"
    if component_name == "financial_quality":
        return "financial_quality_score"
    if component_name == "momentum":
        return "momentum_score"
    if component_name == "risk":
        return "risk_score"
    if component_name == "legacy_risk":
        return "legacy_risk_score"
    if component_name == "risk_penalty_input":
        return "risk_penalty_input_score"
    if component_name == "predictive_risk_penalty_input":
        return "predictive_risk_penalty_input_score"
    if component_name == "uncompensated_risk":
        return "uncompensated_risk_score"
    if component_name == "compensated_risk":
        return "compensated_risk_score"
    if component_name == "liquidity_risk":
        return "liquidity_risk_score"
    if component_name == "financing_survival_risk":
        return "financing_survival_risk_score"
    if component_name == "governance_filing_risk":
        return "governance_filing_risk_score"
    if component_name == "regulatory_setback_risk":
        return "regulatory_setback_risk_score"
    if component_name == "pipeline_anchor_risk":
        return "pipeline_anchor_risk_score"
    if component_name == "collaborator_dependency_risk":
        return "collaborator_dependency_risk_score"
    if component_name == "trial_staleness_risk":
        return "trial_staleness_risk_score"
    raise ValueError(f"Unknown component: {component_name}")


COMPONENT_AUDIT_NAMES = [
    "clinical_opportunity",
    "commercial_value",
    "forward_guidance",
    "valuation",
    "upside_capacity",
    "institutional_upside",
    "financial_quality",
    "momentum",
    "risk",
    "legacy_risk",
    "risk_penalty_input",
    "predictive_risk_penalty_input",
    "uncompensated_risk",
    "compensated_risk",
    "liquidity_risk",
    "financing_survival_risk",
    "governance_filing_risk",
    "regulatory_setback_risk",
    "pipeline_anchor_risk",
    "collaborator_dependency_risk",
    "trial_staleness_risk",
]


def build_component_audit_observations(
    score_rows: list[dict[str, Any]],
    bars_by_ticker: dict[str, list[Bar]],
    *,
    config: dict[str, Any],
    horizons: list[int],
    next_bar_entry: bool,
    round_trip_cost_bps: float,
) -> list[dict[str, Any]]:
    cost = round_trip_cost_bps / 10_000.0
    rows: list[dict[str, Any]] = []
    missing_counts: Counter[tuple[int, str]] = Counter()
    for score_row in score_rows:
        ticker = normalize_ticker(score_row.get("ticker"))
        asof = parse_date(score_row.get("asof_date"))
        if not ticker or asof is None:
            continue
        components = component_scores(score_row, config)
        raw_missing = {
            component_name: int(to_float(score_row.get(component_raw_column(component_name)), None) is None)
            for component_name in COMPONENT_AUDIT_NAMES
        }
        raw_zero = {
            component_name: int((to_float(score_row.get(component_raw_column(component_name)), None) or 0.0) == 0.0)
            for component_name in COMPONENT_AUDIT_NAMES
        }
        for horizon in horizons:
            ret, entry_date, target_date, reason = forward_return(
                bars_by_ticker.get(ticker, []),
                asof,
                horizon,
                next_bar_entry=next_bar_entry,
            )
            if ret is None:
                missing_counts[(horizon, reason)] += 1
                continue
            row = {
                "asof_date": score_row.get("asof_date", ""),
                "ticker": ticker,
                "company_id": score_row.get("company_id", ""),
                "company_name": score_row.get("company_name", ""),
                "rank": to_int(score_row.get("rank"), 0),
                "horizon_days": horizon,
                "evaluation_split": "",
                "entry_date": entry_date,
                "target_date": target_date,
                "net_return": ret - cost,
                "biotech_primary_cohort": cohort_of(score_row),
            }
            for component_name in COMPONENT_AUDIT_NAMES:
                row[f"{component_name}_score"] = components[component_name]
                row[f"{component_name}_missing_flag"] = raw_missing[component_name]
                row[f"{component_name}_zero_flag"] = raw_zero[component_name]
            rows.append(row)
    if missing_counts:
        LOGGER.warning(
            "Component-audit forward-return coverage gaps: %s",
            ", ".join(f"{horizon}d:{reason}={count}" for (horizon, reason), count in sorted(missing_counts.items())),
        )
    return rows


def split_maps_for_component_audit_observations(
    observations: list[dict[str, Any]],
    *,
    horizons: list[int],
    train_fraction: float,
) -> dict[int, dict[str, str]]:
    split_maps: dict[int, dict[str, str]] = {}
    for horizon in horizons:
        dates = sorted(
            {
                str(row.get("asof_date") or "")
                for row in observations
                if int(row.get("horizon_days") or 0) == horizon and row.get("net_return") is not None
            }
        )
        if len(dates) < 2:
            split_maps[horizon] = {asof_date: "train" for asof_date in dates}
            continue
        bounded_fraction = max(0.10, min(0.90, float(train_fraction)))
        split_idx = int(math.floor(len(dates) * bounded_fraction))
        split_idx = max(1, min(len(dates) - 1, split_idx))
        split_maps[horizon] = {
            **{asof_date: "train" for asof_date in dates[:split_idx]},
            **{asof_date: "test" for asof_date in dates[split_idx:]},
        }
    return split_maps


def quintile_bucket_rows(rows: list[dict[str, Any]], *, component_name: str) -> dict[int, list[dict[str, Any]]]:
    ordered = sorted(rows, key=lambda row: (float(row[f"{component_name}_score"]), str(row.get("ticker") or "")))
    buckets: dict[int, list[dict[str, Any]]] = {idx: [] for idx in range(1, 6)}
    if not ordered:
        return buckets
    for idx, row in enumerate(ordered):
        bucket = min(5, int(math.floor(idx * 5 / len(ordered))) + 1)
        buckets[bucket].append(row)
    return buckets


def monotonicity_score(bucket_means: list[float], *, expected_sign: int) -> float:
    if len(bucket_means) < 2:
        return 0.0
    steps = [
        (right - left) * expected_sign
        for left, right in zip(bucket_means, bucket_means[1:])
    ]
    return sum(1 for step in steps if step >= 0.0) / len(steps) * 100.0


def classify_component_signal(
    *,
    valid_n: int,
    spearman_test: float,
    top_bottom_lcb_spread_test_pct: float,
    monotonicity_test_pct: float,
    train_test_sign_stable: int,
    top3_gain_contribution_pct: float,
) -> str:
    if valid_n < 60:
        return "sparse_or_noisy"
    if train_test_sign_stable == 0:
        return "unstable"
    if abs(spearman_test) < 0.03 and abs(top_bottom_lcb_spread_test_pct) < 1.0:
        return "neutral"
    if spearman_test < -0.03 and top_bottom_lcb_spread_test_pct < -1.0:
        return "inverted"
    if spearman_test > 0.08 and top_bottom_lcb_spread_test_pct > 2.0 and monotonicity_test_pct >= 50.0:
        if top3_gain_contribution_pct <= 50.0:
            return "strong_positive"
        return "weak_positive"
    if spearman_test > 0.03 or top_bottom_lcb_spread_test_pct > 1.0:
        return "weak_positive"
    return "neutral"


def build_component_predictive_power_rows(
    observations: list[dict[str, Any]],
    *,
    horizons: list[int],
    train_fraction: float,
    lcb_z: float,
) -> list[dict[str, Any]]:
    split_maps = split_maps_for_component_audit_observations(
        observations,
        horizons=horizons,
        train_fraction=train_fraction,
    )
    for row in observations:
        horizon = int(row.get("horizon_days") or 0)
        row["evaluation_split"] = split_maps.get(horizon, {}).get(str(row.get("asof_date") or ""), "")
    cohorts = sorted({cohort_of(row) for row in observations})
    groups = ["all_cohorts", *cohorts]
    output_rows: list[dict[str, Any]] = []
    for cohort in groups:
        cohort_rows = observations if cohort == "all_cohorts" else [row for row in observations if cohort_of(row) == cohort]
        if not cohort_rows:
            continue
        for horizon in horizons:
            horizon_rows = [row for row in cohort_rows if int(row.get("horizon_days") or 0) == horizon]
            if not horizon_rows:
                continue
            for component_name in COMPONENT_AUDIT_NAMES:
                component_rows = [
                    row
                    for row in horizon_rows
                    if to_float(row.get(f"{component_name}_score"), None) is not None
                    and row.get("net_return") is not None
                ]
                split_rows = {
                    split_name: [row for row in component_rows if split_name == "all" or row.get("evaluation_split") == split_name]
                    for split_name in ("all", "train", "test")
                }
                stats: dict[str, dict[str, float]] = {}
                for split_name, rows in split_rows.items():
                    if not rows:
                        stats[split_name] = {
                            "valid_n": 0.0,
                            "unique_tickers": 0.0,
                            "spearman": 0.0,
                            "top_bottom_mean_spread_pct": 0.0,
                            "top_bottom_lcb_spread_pct": 0.0,
                            "win_rate_spread_pct": 0.0,
                            "monotonicity_pct": 0.0,
                            "top3_gain_contribution_pct": 0.0,
                            "missing_rate_pct": 0.0,
                            "zero_rate_pct": 0.0,
                            "mean_component_score": 0.0,
                        }
                        continue
                    component_values = [float(row[f"{component_name}_score"]) for row in rows]
                    return_values = [float(row["net_return"]) for row in rows]
                    buckets = quintile_bucket_rows(rows, component_name=component_name)
                    bucket_means = [
                        mean([float(row["net_return"]) for row in buckets[bucket]])
                        for bucket in range(1, 6)
                    ]
                    bottom_returns = [float(row["net_return"]) for row in buckets[1]]
                    top_returns = [float(row["net_return"]) for row in buckets[5]]
                    expected_sign = (
                        -1
                        if component_name in {"risk", "risk_penalty_input", "predictive_risk_penalty_input"}
                        or component_name.endswith("_risk")
                        else 1
                    )
                    top_minus_bottom_mean = (mean(top_returns) - mean(bottom_returns)) * expected_sign
                    top_minus_bottom_lcb = (
                        lcb_mean(top_returns, lcb_z) - lcb_mean(bottom_returns, lcb_z)
                    ) * expected_sign
                    top_win = (sum(1 for value in top_returns if value > 0.0) / len(top_returns) * 100.0) if top_returns else 0.0
                    bottom_win = (
                        sum(1 for value in bottom_returns if value > 0.0) / len(bottom_returns) * 100.0
                        if bottom_returns
                        else 0.0
                    )
                    stats[split_name] = {
                        "valid_n": float(len(rows)),
                        "unique_tickers": float(len({normalize_ticker(row.get("ticker")) for row in rows} - {""})),
                        "spearman": float(spearman_corr(component_values, return_values) or 0.0) * expected_sign,
                        "top_bottom_mean_spread_pct": top_minus_bottom_mean * 100.0,
                        "top_bottom_lcb_spread_pct": top_minus_bottom_lcb * 100.0,
                        "win_rate_spread_pct": (top_win - bottom_win) * expected_sign,
                        "monotonicity_pct": monotonicity_score(bucket_means, expected_sign=expected_sign),
                        "top3_gain_contribution_pct": top3_gain_contribution_pct(return_values),
                        "missing_rate_pct": mean([float(row[f"{component_name}_missing_flag"]) for row in rows]) * 100.0,
                        "zero_rate_pct": mean([float(row[f"{component_name}_zero_flag"]) for row in rows]) * 100.0,
                        "mean_component_score": mean(component_values),
                    }
                train_spearman = stats["train"]["spearman"]
                test_spearman = stats["test"]["spearman"]
                train_test_sign_stable = int(
                    (train_spearman == 0.0 and test_spearman == 0.0)
                    or (train_spearman > 0.0 and test_spearman > 0.0)
                    or (train_spearman < 0.0 and test_spearman < 0.0)
                )
                classification = classify_component_signal(
                    valid_n=int(stats["test"]["valid_n"]),
                    spearman_test=stats["test"]["spearman"],
                    top_bottom_lcb_spread_test_pct=stats["test"]["top_bottom_lcb_spread_pct"],
                    monotonicity_test_pct=stats["test"]["monotonicity_pct"],
                    train_test_sign_stable=train_test_sign_stable,
                    top3_gain_contribution_pct=stats["test"]["top3_gain_contribution_pct"],
                )
                output_rows.append(
                    {
                        "biotech_primary_cohort": cohort,
                        "horizon_days": horizon,
                        "component_name": component_name,
                        "component_expected_direction": (
                            "lower_is_better"
                            if component_name in {"risk", "risk_penalty_input", "predictive_risk_penalty_input"}
                            or component_name.endswith("_risk")
                            else "higher_is_better"
                        ),
                        "classification": classification,
                        "valid_n_all": int(stats["all"]["valid_n"]),
                        "valid_n_train": int(stats["train"]["valid_n"]),
                        "valid_n_test": int(stats["test"]["valid_n"]),
                        "unique_tickers_test": int(stats["test"]["unique_tickers"]),
                        "spearman_all": round(stats["all"]["spearman"], 6),
                        "spearman_train": round(stats["train"]["spearman"], 6),
                        "spearman_test": round(stats["test"]["spearman"], 6),
                        "train_test_sign_stable": train_test_sign_stable,
                        "top_bottom_mean_spread_test_pct": round(stats["test"]["top_bottom_mean_spread_pct"], 6),
                        "top_bottom_lcb_spread_test_pct": round(stats["test"]["top_bottom_lcb_spread_pct"], 6),
                        "win_rate_spread_test_pct": round(stats["test"]["win_rate_spread_pct"], 6),
                        "monotonicity_test_pct": round(stats["test"]["monotonicity_pct"], 6),
                        "top3_gain_contribution_test_pct": round(stats["test"]["top3_gain_contribution_pct"], 6),
                        "missing_rate_test_pct": round(stats["test"]["missing_rate_pct"], 6),
                        "zero_rate_test_pct": round(stats["test"]["zero_rate_pct"], 6),
                        "mean_component_score_test": round(stats["test"]["mean_component_score"], 6),
                    }
                )
    output_rows.sort(
        key=lambda row: (
            str(row["biotech_primary_cohort"]) != "all_cohorts",
            str(row["biotech_primary_cohort"]),
            int(row["horizon_days"]),
            str(row["component_name"]),
        )
    )
    return output_rows


def write_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore", lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def config_hash(config: dict[str, Any]) -> str:
    payload = json.dumps(config, sort_keys=True, ensure_ascii=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def thresholds_from_config(config: dict[str, Any]) -> Thresholds:
    base = "biotech_scoring.cohort_config_overrides"
    label_base = f"{base}.promotion_policy.label_quality"
    return Thresholds(
        min_observations=int(cfg_get(config, f"{base}.min_observations", 60)),
        min_asof_dates=int(cfg_get(config, f"{base}.min_asof_dates", 8)),
        min_unique_tickers=int(cfg_get(config, f"{base}.min_unique_tickers", 5)),
        min_current_investible_tickers=int(cfg_get(config, f"{base}.min_current_investible_tickers", 5)),
        min_recent_asof_coverage_pct=float(cfg_get(config, f"{base}.min_recent_asof_coverage_pct", 60.0)),
        min_median_confidence=float(cfg_get(config, f"{label_base}.min_median_cohort_confidence", 80.0)),
        min_weighted_confidence=float(cfg_get(config, f"{label_base}.min_calibration_weighted_confidence", 85.0)),
        max_review_share_pct=float(cfg_get(config, f"{label_base}.max_unclassified_or_review_pct", 10.0)),
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build cohort-config override calibration audit artifacts.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--output-dir", type=Path, default=None)
    parser.add_argument(
        "--mode",
        choices=["all", "label-audit", "baseline", "priority", "calibrate-one", "component-audit"],
        default="all",
    )
    parser.add_argument("--start-asof", default=None)
    parser.add_argument("--end-asof", default=None)
    parser.add_argument("--max-asof-dates", type=int, default=0)
    parser.add_argument("--horizons", default=None)
    parser.add_argument("--top-n", default=None)
    parser.add_argument("--candidate-pool-rank-max", type=int, default=None)
    parser.add_argument("--target-cohort", default=None)
    parser.add_argument("--candidate-limit", type=int, default=0)
    parser.add_argument("--next-bar-entry", action=argparse.BooleanOptionalAction, default=None)
    parser.add_argument("--round-trip-cost-bps", type=float, default=None)
    parser.add_argument("--train-fraction", type=float, default=None)
    parser.add_argument("--lcb-z", type=float, default=1.0)
    return parser.parse_args()


def main() -> None:
    configure_utc_logging()
    args = parse_args()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    config_base = config_path.parent
    db_path_raw = cfg_get(config, "paths.database_path", None)
    if db_path_raw is None:
        db_path_raw = (config.get("database") or {}).get("database_path") if isinstance(config.get("database"), dict) else None
    if db_path_raw is None:
        raise ValueError("Missing database path: expected paths.database_path")
    db_path = args.db.expanduser().resolve() if args.db else resolve_path(db_path_raw, base_dir=config_base)
    reporting_base = "biotech_scoring.cohort_config_overrides.reporting"
    output_dir = (
        args.output_dir.expanduser().resolve()
        if args.output_dir
        else resolve_path(
            cfg_get(
                config,
                f"{reporting_base}.output_dir",
                "../output/biotech_index_reports/cohort_config_override_calibration",
            ),
            base_dir=config_base,
        )
    )
    horizons = parse_int_list(args.horizons, [int(value) for value in cfg_get(config, f"{reporting_base}.horizons", DEFAULT_HORIZONS)])
    top_n_values = parse_int_list(args.top_n, [int(value) for value in cfg_get(config, f"{reporting_base}.top_n", DEFAULT_TOP_N)])
    rank_limit = int(args.candidate_pool_rank_max or cfg_get(config, f"{reporting_base}.candidate_pool_rank_max", 60))
    train_fraction = float(args.train_fraction if args.train_fraction is not None else cfg_get(config, f"{reporting_base}.train_fraction", 0.70))
    if not 0.10 <= train_fraction <= 0.90:
        raise ValueError(f"train_fraction must be between 0.10 and 0.90, got {train_fraction}")
    next_bar_entry = (
        bool(args.next_bar_entry)
        if args.next_bar_entry is not None
        else bool(cfg_get(config, f"{reporting_base}.next_bar_entry", True))
    )
    round_trip_cost_bps = float(
        args.round_trip_cost_bps
        if args.round_trip_cost_bps is not None
        else cfg_get(config, f"{reporting_base}.costs.long_round_trip_bps", 40.0)
    )
    thresholds = thresholds_from_config(config)
    if thresholds.min_observations < 1 or thresholds.min_asof_dates < 1:
        raise ValueError("cohort_config_overrides min_observations and min_asof_dates must be >= 1")

    with connect_readonly(db_path) as conn:
        dates = load_score_dates(
            conn,
            start_asof=args.start_asof,
            end_asof=args.end_asof,
            max_asof_dates=max(0, int(args.max_asof_dates or 0)),
        )
        all_score_rows = load_score_rows(conn, dates, rank_limit=0)
        candidate_score_rows = [
            row for row in all_score_rows if rank_limit <= 0 or to_int(row.get("rank"), 999_999) <= rank_limit
        ]
        bar_source_rows = all_score_rows if args.mode == "component-audit" else candidate_score_rows
        parsed_dates = [
            parsed for parsed in (parse_date(row.get("asof_date")) for row in bar_source_rows) if parsed is not None
        ]
        bars_by_ticker: dict[str, list[Bar]] = {}
        if args.mode in {"all", "baseline", "priority", "calibrate-one", "component-audit"} and parsed_dates:
            min_bar_date = min(parsed_dates) - timedelta(days=14)
            max_bar_date = max(parsed_dates) + timedelta(days=max(horizons) * 3 + 30)
            tickers = {
                normalize_ticker(row.get("ticker"))
                for row in bar_source_rows
                if normalize_ticker(row.get("ticker"))
            }
            bars_by_ticker = load_bars(
                conn,
                tickers=tickers,
                min_date=min_bar_date,
                max_date=max_bar_date,
                market_sources=calibration_market_sources(config),
            )

    recent_dates = dates[-thresholds.min_asof_dates :] if thresholds.min_asof_dates > 0 else dates
    label_rows, transition_rows, confidence_rows, sparse_rows = build_label_artifacts(
        all_score_rows,
        thresholds=thresholds,
        recent_dates=recent_dates,
    )
    write_csv(output_dir / "cohort_label_audit.csv", label_rows, LABEL_AUDIT_FIELDS)
    write_csv(output_dir / "cohort_transition_matrix.csv", transition_rows, TRANSITION_FIELDS)
    write_csv(output_dir / "cohort_confidence_distribution.csv", confidence_rows, CONFIDENCE_FIELDS)
    write_csv(output_dir / "cohort_sparse_data_report.csv", sparse_rows, SPARSE_FIELDS)

    baseline_rows: list[dict[str, Any]] = []
    exposure_rows: list[dict[str, Any]] = []
    priority_rows: list[dict[str, Any]] = []
    observations: list[dict[str, Any]] = []
    candidate_observations: list[dict[str, Any]] = []
    candidate_summary_rows: list[dict[str, Any]] = []
    ticker_breadth_rows: list[dict[str, Any]] = []
    promotion_rows: list[dict[str, Any]] = []
    rank_movement_rows: list[dict[str, Any]] = []
    ticker_return_diagnostic_rows: list[dict[str, Any]] = []
    component_attribution_rows: list[dict[str, Any]] = []
    failure_diagnostic_rows: list[dict[str, Any]] = []
    component_audit_observations: list[dict[str, Any]] = []
    component_predictive_power_rows: list[dict[str, Any]] = []
    target_cohort = ""
    candidates: list[OverrideCandidate] = []
    if args.mode in {"all", "baseline", "priority", "calibrate-one"}:
        observations = build_observations(
            candidate_score_rows,
            bars_by_ticker,
            horizons=horizons,
            top_n_values=top_n_values,
            next_bar_entry=next_bar_entry,
            round_trip_cost_bps=round_trip_cost_bps,
        )
        baseline_rows = build_baseline_summary(
            observations,
            horizons=horizons,
            top_n_values=top_n_values,
            train_fraction=train_fraction,
            lcb_z=float(args.lcb_z),
        )
        exposure_rows = build_top_exposure(all_score_rows, top_n_values=top_n_values)
        holdout_rows = [row for row in baseline_rows if str(row.get("evaluation_split")) == "test"]
        write_csv(output_dir / "cohort_current_config_baseline.csv", baseline_rows, BASELINE_FIELDS)
        write_csv(output_dir / "cohort_current_top_exposure.csv", exposure_rows, TOP_EXPOSURE_FIELDS(top_n_values))
        write_csv(output_dir / "cohort_current_holdout_metrics.csv", holdout_rows, BASELINE_FIELDS)

    if args.mode in {"all", "priority", "calibrate-one"}:
        primary_top_n = 20 if 20 in top_n_values else max(top_n_values)
        primary_horizon = 120 if 120 in horizons else max(horizons)
        priority_rows = build_priority_rows(
            label_rows,
            exposure_rows,
            baseline_rows,
            all_score_rows,
            primary_top_n=primary_top_n,
            primary_horizon=primary_horizon,
            thresholds=thresholds,
        )
        write_csv(output_dir / "cohort_priority.csv", priority_rows, PRIORITY_FIELDS)

    if args.mode == "calibrate-one":
        primary_top_n = 20 if 20 in top_n_values else max(top_n_values)
        primary_horizon = 120 if 120 in horizons else max(horizons)
        target_cohort = choose_target_cohort(priority_rows, args.target_cohort)
        candidates = candidate_grid(
            config,
            target_cohort=target_cohort,
            candidate_limit=max(0, int(args.candidate_limit or 0)),
        )
        candidate_observations = build_candidate_observations(
            candidate_score_rows,
            bars_by_ticker,
            candidates=candidates,
            target_cohort=target_cohort,
            config=config,
            horizons=horizons,
            top_n_values=top_n_values,
            next_bar_entry=next_bar_entry,
            round_trip_cost_bps=round_trip_cost_bps,
        )
        candidate_summary_rows = build_candidate_summary(
            candidate_observations,
            dates=dates,
            horizons=horizons,
            top_n_values=top_n_values,
            train_fraction=train_fraction,
            lcb_z=float(args.lcb_z),
        )
        ticker_breadth_rows = build_ticker_breadth_rows(
            candidate_observations,
            dates=dates,
            horizons=horizons,
            top_n_values=top_n_values,
            train_fraction=train_fraction,
            target_cohort=target_cohort,
        )
        promotion_rows = build_promotion_recommendations(
            candidate_summary_rows=candidate_summary_rows,
            ticker_breadth_rows=ticker_breadth_rows,
            candidates=candidates,
            target_cohort=target_cohort,
            label_rows=label_rows,
            thresholds=thresholds,
            config=config,
            primary_top_n=primary_top_n,
            primary_horizon=primary_horizon,
        )
        rank_movement_rows = build_rank_movement_diagnostics(
            candidate_observations,
            horizons=horizons,
            train_fraction=train_fraction,
        )
        ticker_return_diagnostic_rows = build_ticker_return_diagnostics(
            candidate_observations,
            horizons=horizons,
            top_n_values=top_n_values,
            train_fraction=train_fraction,
            target_cohort=target_cohort,
        )
        component_attribution_rows = build_component_attribution_rows(
            candidate_observations,
            horizons=horizons,
            top_n_values=top_n_values,
            train_fraction=train_fraction,
            target_cohort=target_cohort,
        )
        failure_diagnostic_rows = build_failure_diagnostics(
            promotion_rows=promotion_rows,
            rank_movement_rows=rank_movement_rows,
            ticker_breadth_rows=ticker_breadth_rows,
        )
        write_csv(output_dir / "cohort_candidate_grid.csv", build_candidate_grid_rows(candidates, target_cohort=target_cohort), CANDIDATE_GRID_FIELDS)
        write_csv(output_dir / "cohort_candidate_observations.csv", candidate_observations, CANDIDATE_OBSERVATION_FIELDS)
        write_csv(output_dir / "cohort_candidate_summary.csv", candidate_summary_rows, CANDIDATE_SUMMARY_FIELDS)
        write_csv(output_dir / "cohort_ticker_improvement_breadth.csv", ticker_breadth_rows, TICKER_BREADTH_FIELDS)
        write_csv(output_dir / "promotion_recommendations.csv", promotion_rows, PROMOTION_RECOMMENDATION_FIELDS)
        write_csv(output_dir / "cohort_candidate_rank_movement.csv", rank_movement_rows, RANK_MOVEMENT_FIELDS)
        write_csv(
            output_dir / "cohort_ticker_return_diagnostics.csv",
            ticker_return_diagnostic_rows,
            TICKER_RETURN_DIAGNOSTIC_FIELDS,
        )
        write_csv(
            output_dir / "cohort_component_attribution.csv",
            component_attribution_rows,
            COMPONENT_ATTRIBUTION_FIELDS,
        )
        write_csv(
            output_dir / "cohort_candidate_failure_diagnostics.csv",
            failure_diagnostic_rows,
            FAILURE_DIAGNOSTIC_FIELDS,
        )

    if args.mode == "component-audit":
        component_audit_observations = build_component_audit_observations(
            all_score_rows,
            bars_by_ticker,
            config=config,
            horizons=horizons,
            next_bar_entry=next_bar_entry,
            round_trip_cost_bps=round_trip_cost_bps,
        )
        component_predictive_power_rows = build_component_predictive_power_rows(
            component_audit_observations,
            horizons=horizons,
            train_fraction=train_fraction,
            lcb_z=float(args.lcb_z),
        )
        write_csv(
            output_dir / "cohort_component_predictive_power.csv",
            component_predictive_power_rows,
            COMPONENT_PREDICTIVE_POWER_FIELDS,
        )

    manifest = {
        "mode": args.mode,
        "config_path": str(config_path),
        "config_hash": config_hash(config),
        "db_path": str(db_path),
        "output_dir": str(output_dir),
        "start_asof": args.start_asof,
        "end_asof": args.end_asof,
        "max_asof_dates": max(0, int(args.max_asof_dates or 0)),
        "score_dates": len(dates),
        "score_rows_loaded": len(all_score_rows),
        "candidate_score_rows_loaded": len(candidate_score_rows),
        "cohort_count": len(label_rows),
        "observation_rows": len(observations),
        "baseline_rows": len(baseline_rows),
        "priority_rows": len(priority_rows),
        "target_cohort": target_cohort,
        "candidate_count": len(candidates),
        "candidate_observation_rows": len(candidate_observations),
        "candidate_summary_rows": len(candidate_summary_rows),
        "ticker_breadth_rows": len(ticker_breadth_rows),
        "promotion_recommendation_rows": len(promotion_rows),
        "rank_movement_rows": len(rank_movement_rows),
        "ticker_return_diagnostic_rows": len(ticker_return_diagnostic_rows),
        "component_attribution_rows": len(component_attribution_rows),
        "failure_diagnostic_rows": len(failure_diagnostic_rows),
        "component_audit_observation_rows": len(component_audit_observations),
        "component_predictive_power_rows": len(component_predictive_power_rows),
        "candidate_scoring_method": "component_delta_overlay_report_only_v1",
        "horizons": horizons,
        "top_n": top_n_values,
        "candidate_pool_rank_max": rank_limit,
        "next_bar_entry": next_bar_entry,
        "round_trip_cost_bps": round_trip_cost_bps,
        "train_fraction": train_fraction,
        "market_sources": calibration_market_sources(config),
        "production_behavior_changed": False,
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "cohort_config_override_calibration_manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    LOGGER.info("Wrote cohort config override calibration artifacts to %s", output_dir)


LABEL_AUDIT_FIELDS = [
    "biotech_primary_cohort",
    "historical_observations",
    "unique_tickers",
    "asof_dates",
    "current_ticker_count",
    "current_investible_tickers",
    "current_calibration_eligible_tickers",
    "median_cohort_confidence",
    "mean_cohort_confidence",
    "calibration_weighted_confidence",
    "low_confidence_share_pct",
    "unclassified_or_review_share_pct",
    "transition_out_count",
    "transition_out_rate_pct",
    "ticker_stability_share_pct",
    "recent_asof_coverage_pct",
    "latest_asof_date",
]

TRANSITION_FIELDS = [
    "from_cohort",
    "to_cohort",
    "transition_count",
    "unique_tickers",
    "first_transition_asof",
    "last_transition_asof",
]

CONFIDENCE_FIELDS = [
    "biotech_primary_cohort",
    "confidence_bucket",
    "row_count",
    "row_share_pct",
]

SPARSE_FIELDS = [*LABEL_AUDIT_FIELDS, "fallback_required", "fallback_reason"]

BASELINE_FIELDS = [
    "evaluation_split",
    "top_n",
    "horizon_days",
    "biotech_primary_cohort",
    "selected_n",
    "asof_dates",
    "unique_tickers",
    "avg_rank",
    "avg_opportunity_score",
    "mean_return_pct",
    "median_return_pct",
    "lcb_return_pct",
    "p10_return_pct",
    "win_rate_pct",
    "profit_factor",
    "large_loss_20pct_count",
    "large_loss_20pct_rate_pct",
    "large_loss_40pct_rate_pct",
    "top3_gain_contribution_pct",
    "top_tickers",
]


def TOP_EXPOSURE_FIELDS(top_n_values: list[int]) -> list[str]:
    fields = ["biotech_primary_cohort", "latest_asof_date", "current_ticker_count"]
    for top_n in top_n_values:
        fields.extend(
            [
                f"top{top_n}_selected_n",
                f"top{top_n}_share_pct",
                f"top{top_n}_asof_frequency_pct",
                f"latest_top{top_n}_count",
            ]
        )
    return fields


PRIORITY_FIELDS = [
    "priority_rank",
    "biotech_primary_cohort",
    "priority_score",
    "base_priority_score",
    "priority_boost",
    "historical_observations",
    "current_ticker_count",
    "top20_exposure_frequency_pct",
    "calibration_eligible_count",
    "test_top20_120d_lcb_gap_vs_global_pct",
    "performance_gap_abs_pct",
    "large_loss_contribution_pct",
    "rank_instability_avg_std",
    "appears_in_current_top10",
    "high_large_loss_contribution",
    "high_score_rank_instability",
    "sparse_or_label_blocked",
    "recommended_next_step",
]

CANDIDATE_GRID_FIELDS = [
    "candidate_id",
    "target_cohort",
    "description",
    "profile_name",
    "override_strength",
    "risk_penalty",
    "risk_penalty_convexity",
    "risk_penalty_inflection",
    *[f"weight_{key}" for key in INVESTMENT_COMPONENT_KEYS],
]

CANDIDATE_OBSERVATION_FIELDS = [
    "candidate_id",
    "candidate_description",
    "target_cohort",
    "selection_scope",
    "profile_name",
    "asof_date",
    "ticker",
    "company_id",
    "company_name",
    "baseline_rank",
    "candidate_rank",
    "top_n",
    "horizon_days",
    "evaluation_split",
    "entry_date",
    "target_date",
    "return",
    "net_return",
    "missing_return_reason",
    "biotech_primary_cohort",
    "opportunity_score",
    "adjusted_opportunity_score",
    "score_delta",
    "baseline_component_score",
    "candidate_component_score",
    *COMPONENT_ATTRIBUTION_SCORE_FIELDS,
    "override_strength",
    "risk_penalty",
    "risk_penalty_convexity",
    "risk_penalty_inflection",
    "rank_quality_cap_vetoed",
]

CANDIDATE_SUMMARY_FIELDS = [
    "candidate_id",
    "candidate_description",
    "target_cohort",
    "selection_scope",
    "evaluation_split",
    "top_n",
    "horizon_days",
    "biotech_primary_cohort",
    "selected_n",
    "asof_dates",
    "unique_tickers",
    "target_cohort_selected_n",
    "target_cohort_share_pct",
    "avg_rank",
    "avg_candidate_rank",
    "avg_opportunity_score",
    "avg_adjusted_opportunity_score",
    "avg_target_score_delta",
    "mean_return_pct",
    "median_return_pct",
    "lcb_return_pct",
    "p10_return_pct",
    "win_rate_pct",
    "profit_factor",
    "large_loss_20pct_count",
    "large_loss_20pct_rate_pct",
    "large_loss_40pct_rate_pct",
    "top3_gain_contribution_pct",
    "baseline_lcb_return_pct",
    "lcb_delta_pct",
    "baseline_profit_factor",
    "profit_factor_delta",
    "baseline_large_loss_20pct_rate_pct",
    "large_loss_20pct_rate_delta_pct",
    "date_improvement_rate_pct",
    "top_tickers",
]

TICKER_BREADTH_FIELDS = [
    "candidate_id",
    "target_cohort",
    "selection_scope",
    "evaluation_split",
    "top_n",
    "horizon_days",
    "scope",
    "comparable_unique_tickers",
    "improved_unique_tickers",
    "improved_unique_ticker_rate_pct",
    "harmed_unique_tickers",
    "harmed_unique_ticker_rate_pct",
    "median_unique_ticker_return_delta_pct",
    "mean_unique_ticker_return_delta_pct",
    "top_ticker_return_deltas_pct",
]

PROMOTION_RECOMMENDATION_FIELDS = [
    "candidate_id",
    "candidate_description",
    "target_cohort",
    "status",
    "config_action",
    "primary_top_n",
    "primary_horizon_days",
    "within_selection_scope",
    "global_selection_scope",
    "evaluation_split",
    "within_lcb_delta_pct",
    "within_profit_factor_delta",
    "within_large_loss_20pct_rate_delta_pct",
    "comparable_unique_tickers",
    "improved_unique_ticker_rate_pct",
    "median_unique_ticker_return_delta_pct",
    "harmed_unique_ticker_rate_pct",
    "global_top10_lcb_delta_pct",
    "global_top20_lcb_delta_pct",
    "global_top10_profit_factor_delta",
    "global_top20_profit_factor_delta",
    "global_top20_large_loss_20pct_rate_delta_pct",
    "avg_target_score_delta",
    "target_top10_share_pct",
    "target_top20_share_pct",
    "global_top20_top3_gain_contribution_pct",
    "label_blockers",
    "promotion_blockers",
    "recommendation",
]

RANK_MOVEMENT_FIELDS = [
    "candidate_id",
    "candidate_description",
    "target_cohort",
    "selection_scope",
    "evaluation_split",
    "top_n",
    "horizon_days",
    "dates_compared",
    "selection_slots",
    "entered_instances",
    "exited_instances",
    "unchanged_instances",
    "rank_changed_common_instances",
    "rank_movement_intensity_pct",
    "no_rank_movement_date_pct",
    "avg_abs_common_rank_delta",
    "avg_common_score_delta",
    "avg_common_adjusted_score_delta",
    "unique_entered_tickers",
    "unique_exited_tickers",
    "top_entered_tickers",
    "top_exited_tickers",
    "movement_diagnosis",
]

TICKER_RETURN_DIAGNOSTIC_FIELDS = [
    "candidate_id",
    "candidate_description",
    "target_cohort",
    "selection_scope",
    "scope",
    "evaluation_split",
    "top_n",
    "horizon_days",
    "ticker",
    "baseline_selected_count",
    "candidate_selected_count",
    "baseline_mean_return_pct",
    "candidate_mean_return_pct",
    "return_delta_pct",
    "ticker_outcome",
]

COMPONENT_ATTRIBUTION_FIELDS = [
    "candidate_id",
    "candidate_description",
    "target_cohort",
    "selection_scope",
    "scope",
    "evaluation_split",
    "top_n",
    "horizon_days",
    "ticker_outcome",
    "ticker_count",
    "baseline_total_selected_count",
    "candidate_total_selected_count",
    "mean_return_delta_pct",
    "median_return_delta_pct",
    "avg_baseline_rank",
    "avg_candidate_rank",
    "avg_rank_delta",
    "avg_score_delta",
    "avg_adjusted_score_delta",
    *[f"avg_{field}" for field in COMPONENT_ATTRIBUTION_SCORE_FIELDS],
    "top_tickers",
]

FAILURE_DIAGNOSTIC_FIELDS = [
    "candidate_id",
    "candidate_description",
    "target_cohort",
    "status",
    "primary_failure_category",
    "recommended_next_action",
    "promotion_blockers",
    "within_rank_movement_intensity_pct",
    "within_no_rank_movement_date_pct",
    "within_entered_instances",
    "within_exited_instances",
    "within_top_entered_tickers",
    "within_top_exited_tickers",
    "global_rank_movement_intensity_pct",
    "comparable_unique_tickers",
    "improved_unique_ticker_rate_pct",
    "harmed_unique_ticker_rate_pct",
    "median_unique_ticker_return_delta_pct",
    "within_lcb_delta_pct",
    "global_top20_lcb_delta_pct",
    "target_top20_share_pct",
]

COMPONENT_PREDICTIVE_POWER_FIELDS = [
    "biotech_primary_cohort",
    "horizon_days",
    "component_name",
    "component_expected_direction",
    "classification",
    "valid_n_all",
    "valid_n_train",
    "valid_n_test",
    "unique_tickers_test",
    "spearman_all",
    "spearman_train",
    "spearman_test",
    "train_test_sign_stable",
    "top_bottom_mean_spread_test_pct",
    "top_bottom_lcb_spread_test_pct",
    "win_rate_spread_test_pct",
    "monotonicity_test_pct",
    "top3_gain_contribution_test_pct",
    "missing_rate_test_pct",
    "zero_rate_test_pct",
    "mean_component_score_test",
]


if __name__ == "__main__":
    main()
