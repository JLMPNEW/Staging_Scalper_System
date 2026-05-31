#!/usr/bin/env python3
from __future__ import annotations

import argparse
import bisect
import csv
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
from biotech_index.core.logging_utils import configure_utc_logging  # noqa: E402
from biotech_index.core.market_policy import calibration_market_sources  # noqa: E402
from biotech_index.core.text_norm import normalize_ticker  # noqa: E402


LOGGER = logging.getLogger("calibrate_biotech_commercial_growth_policy")
DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
SQLITE_PARAM_CHUNK_SIZE = 800
DEFAULT_TARGET_COHORT = "commercial_profitable_growth"
TARGET_COHORT = DEFAULT_TARGET_COHORT
DEFAULT_HORIZONS = [20, 60, 120]
DEFAULT_TOP_N = [10, 20]


@dataclass(frozen=True)
class Bar:
    day: date
    close: float


@dataclass(frozen=True)
class PolicySpec:
    candidate_id: str
    description: str
    cap_top10: int | None = None
    cap_top20: int | None = None
    score_penalty: float = 0.0
    rerank_by_adjusted_score: bool = False
    fill_replacements: bool = True
    min_momentum: float | None = None
    min_expected_return_quality: float | None = None
    max_value_trap: float | None = None
    max_mature_defensive: float | None = None
    max_commercial_risk_overlay: float | None = None
    max_commercial_deterioration: float | None = None


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


def parse_int_list(raw: str | None, default: list[int]) -> list[int]:
    if raw is None or str(raw).strip() == "":
        return list(default)
    values: list[int] = []
    for token in str(raw).replace(";", ",").replace("|", ",").split(","):
        token = token.strip()
        if token:
            values.append(int(token))
    return sorted(set(values)) or list(default)


def connect_readonly(db_path: Path) -> sqlite3.Connection:
    uri = f"{db_path.resolve().as_uri()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=30.0)
    conn.row_factory = sqlite3.Row
    return conn


def table_columns(conn: sqlite3.Connection, table_name: str) -> set[str]:
    return {str(row["name"]) for row in conn.execute(f"PRAGMA table_info({table_name})").fetchall()}


def select_expr(columns: set[str], column: str, fallback_sql: str = "''") -> str:
    return column if column in columns else f"{fallback_sql} AS {column}"


def chunked(items: list[Any], size: int) -> list[list[Any]]:
    return [items[idx : idx + size] for idx in range(0, len(items), size)]


def load_score_dates(conn: sqlite3.Connection, start_asof: str | None, end_asof: str | None) -> list[str]:
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
    return [str(row["asof_date"]) for row in rows]


def load_ranked_score_rows(
    conn: sqlite3.Connection,
    dates: list[str],
    *,
    rank_limit: int,
    require_investible: bool,
) -> list[dict[str, Any]]:
    columns = table_columns(conn, "daily_scores")
    required = {"asof_date", "ticker", "rank", "opportunity_score", "biotech_primary_cohort"}
    missing = required - columns
    if missing:
        raise RuntimeError(f"daily_scores is missing required columns: {sorted(missing)}")
    selected_columns = [
        "asof_date",
        "company_id",
        "ticker",
        select_expr(columns, "company_name"),
        "rank",
        "opportunity_score",
        select_expr(columns, "investment_score", "NULL"),
        select_expr(columns, "bucket"),
        "biotech_primary_cohort",
        select_expr(columns, "biotech_cohort_calibration_mode", "'unclassified'"),
        select_expr(columns, "biotech_cohort_investible_flag", "1.0"),
        select_expr(columns, "biotech_cohort_calibration_eligible_flag", "1.0"),
        select_expr(columns, "momentum_score", "NULL"),
        select_expr(columns, "expected_return_quality_score", "NULL"),
        select_expr(columns, "value_trap_score", "NULL"),
        select_expr(columns, "mature_defensive_score", "NULL"),
        select_expr(columns, "commercial_risk_overlay_score", "NULL"),
        select_expr(columns, "commercial_deterioration_score", "NULL"),
    ]
    out: list[dict[str, Any]] = []
    for date_chunk in chunked(dates, SQLITE_PARAM_CHUNK_SIZE - 1):
        placeholders = ",".join("?" for _ in date_chunk)
        rows = conn.execute(
            f"""
            SELECT {", ".join(selected_columns)}
            FROM daily_scores
            WHERE asof_date IN ({placeholders})
              AND rank IS NOT NULL
              AND rank > 0
              AND rank <= ?
            ORDER BY asof_date, rank, ticker
            """,
            (*date_chunk, rank_limit),
        ).fetchall()
        for row in rows:
            item = dict(row)
            investible = to_float(item.get("biotech_cohort_investible_flag"), 1.0)
            if require_investible and investible is not None and investible <= 0.0:
                continue
            out.append(item)
    return out


def load_bars(
    conn: sqlite3.Connection,
    *,
    tickers: set[str],
    min_date: date,
    market_sources: list[str],
) -> dict[str, list[Bar]]:
    if not tickers:
        return {}
    ordered_sources = [source for source in market_sources if source]
    source_priority = {source: idx for idx, source in enumerate(ordered_sources)}
    grouped: dict[tuple[str, str], list[Bar]] = defaultdict(list)
    ordered_tickers = sorted(tickers)
    ticker_chunk_size = max(1, SQLITE_PARAM_CHUNK_SIZE - len(ordered_sources) - 1)
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
              AND close IS NOT NULL
              AND close > 0
            ORDER BY ticker, source, bar_date
            """,
            (*ticker_chunk, *ordered_sources, min_date.isoformat()),
        ).fetchall()
        for row in rows:
            parsed = parse_date(row["bar_date"])
            close = to_float(row["close"])
            ticker = normalize_ticker(row["ticker"])
            if parsed is None or close is None or close <= 0 or not ticker:
                continue
            grouped[(ticker, str(row["source"] or ""))].append(Bar(day=parsed, close=close))
    by_ticker: dict[str, list[tuple[int, list[Bar]]]] = defaultdict(list)
    for (ticker, source), bars in grouped.items():
        by_ticker[ticker].append((source_priority.get(source, 9999), sorted(bars, key=lambda bar: bar.day)))
    return {
        ticker: min(candidates, key=lambda item: item[0])[1]
        for ticker, candidates in by_ticker.items()
        if candidates
    }


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
    if entry.close <= 0:
        return None, entry.day.isoformat(), target.day.isoformat(), "invalid_entry_close"
    return (target.close / entry.close) - 1.0, entry.day.isoformat(), target.day.isoformat(), ""


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
    gains = sum(value for value in values if value > 0)
    losses = -sum(value for value in values if value < 0)
    if losses <= 0:
        return 999.0 if gains > 0 else 0.0
    return gains / losses


def top3_gain_contribution_pct(values: list[float]) -> float:
    gains = sorted([value for value in values if value > 0], reverse=True)
    total_gain = sum(gains)
    if total_gain <= 0:
        return 0.0
    return (sum(gains[:3]) / total_gain) * 100.0


def summarize(values: list[float], *, lcb_z: float) -> dict[str, float]:
    if not values:
        return {
            "mean_return_pct": 0.0,
            "median_return_pct": 0.0,
            "lcb_return_pct": 0.0,
            "p10_return_pct": 0.0,
            "win_rate_pct": 0.0,
            "profit_factor": 0.0,
            "large_loss_20pct_rate_pct": 0.0,
            "large_loss_40pct_rate_pct": 0.0,
            "top3_gain_contribution_pct": 0.0,
        }
    return {
        "mean_return_pct": mean(values) * 100.0,
        "median_return_pct": percentile(values, 0.50) * 100.0,
        "lcb_return_pct": lcb_mean(values, lcb_z) * 100.0,
        "p10_return_pct": percentile(values, 0.10) * 100.0,
        "win_rate_pct": (sum(1 for value in values if value > 0) / len(values)) * 100.0,
        "profit_factor": profit_factor(values),
        "large_loss_20pct_rate_pct": (sum(1 for value in values if value <= -0.20) / len(values)) * 100.0,
        "large_loss_40pct_rate_pct": (sum(1 for value in values if value <= -0.40) / len(values)) * 100.0,
        "top3_gain_contribution_pct": top3_gain_contribution_pct(values),
    }


def candidate_specs() -> list[PolicySpec]:
    return [
        PolicySpec("baseline_current_ranking", "Current production ranking; no CPG-specific changes."),
        PolicySpec("cpg_exclude_top10_top20", "Exclude CPG from Top 10 and Top 20 selections.", 0, 0),
        PolicySpec(
            "cpg_exclude_no_fill_top10_top20",
            "Exclude CPG from Top 10/20 and do not fill skipped slots.",
            0,
            0,
            fill_replacements=False,
        ),
        PolicySpec(
            "cpg_cap_3_6_no_fill",
            "Limit CPG to 3/6 and do not fill skipped slots.",
            3,
            6,
            fill_replacements=False,
        ),
        PolicySpec(
            "cpg_cap_2_5_no_fill",
            "Limit CPG to 2/5 and do not fill skipped slots.",
            2,
            5,
            fill_replacements=False,
        ),
        PolicySpec("cpg_cap_0_top10_3_top20", "Exclude CPG from Top 10; limit CPG to 3 names in Top 20.", 0, 3),
        PolicySpec("cpg_cap_0_top10_6_top20", "Exclude CPG from Top 10; limit CPG to 6 names in Top 20.", 0, 6),
        PolicySpec("cpg_cap_3_top10_6_top20", "Limit CPG to 3 names in Top 10 and 6 names in Top 20.", 3, 6),
        PolicySpec("cpg_cap_2_top10_5_top20", "Limit CPG to 2 names in Top 10 and 5 names in Top 20.", 2, 5),
        PolicySpec("cpg_cap_1_top10_3_top20", "Limit CPG to 1 name in Top 10 and 3 names in Top 20.", 1, 3),
        PolicySpec("cpg_penalty_5", "Subtract 5 score points from CPG and rerank.", score_penalty=5.0, rerank_by_adjusted_score=True),
        PolicySpec("cpg_penalty_10", "Subtract 10 score points from CPG and rerank.", score_penalty=10.0, rerank_by_adjusted_score=True),
        PolicySpec("cpg_momentum_min_50", "Require CPG momentum score >= 50.", min_momentum=50.0),
        PolicySpec("cpg_momentum_min_55", "Require CPG momentum score >= 55.", min_momentum=55.0),
        PolicySpec("cpg_expected_return_min_55", "Require CPG expected-return quality >= 55.", min_expected_return_quality=55.0),
        PolicySpec("cpg_expected_return_min_60", "Require CPG expected-return quality >= 60.", min_expected_return_quality=60.0),
        PolicySpec("cpg_value_trap_max_40", "Require CPG value-trap score <= 40.", max_value_trap=40.0),
        PolicySpec("cpg_mature_defensive_max_55", "Require CPG mature-defensive score <= 55.", max_mature_defensive=55.0),
        PolicySpec("cpg_commercial_risk_max_60", "Require CPG commercial-risk overlay score <= 60.", max_commercial_risk_overlay=60.0),
        PolicySpec("cpg_deterioration_max_55", "Require CPG commercial-deterioration score <= 55.", max_commercial_deterioration=55.0),
        PolicySpec(
            "cpg_cap_3_6_momentum_50",
            "Limit CPG to 3/6 and require momentum >= 50.",
            3,
            6,
            min_momentum=50.0,
        ),
        PolicySpec(
            "cpg_cap_3_6_expected_55",
            "Limit CPG to 3/6 and require expected-return quality >= 55.",
            3,
            6,
            min_expected_return_quality=55.0,
        ),
        PolicySpec(
            "cpg_cap_3_6_value_trap_40",
            "Limit CPG to 3/6 and require value-trap <= 40.",
            3,
            6,
            max_value_trap=40.0,
        ),
        PolicySpec(
            "cpg_cap_2_5_momentum_55_expected_55",
            "Limit CPG to 2/5; require momentum >= 55 and expected-return quality >= 55.",
            2,
            5,
            min_momentum=55.0,
            min_expected_return_quality=55.0,
        ),
        PolicySpec(
            "cpg_cap_2_5_commercial_risk_60",
            "Limit CPG to 2/5 and require commercial-risk overlay <= 60.",
            2,
            5,
            max_commercial_risk_overlay=60.0,
        ),
    ]


def is_target_cohort(row: dict[str, Any]) -> bool:
    return str(row.get("biotech_primary_cohort") or "") == TARGET_COHORT


def cpg_passes_gates(row: dict[str, Any], spec: PolicySpec) -> bool:
    if not is_target_cohort(row):
        return True
    checks = [
        (spec.min_momentum, to_float(row.get("momentum_score")), "min"),
        (spec.min_expected_return_quality, to_float(row.get("expected_return_quality_score")), "min"),
        (spec.max_value_trap, to_float(row.get("value_trap_score")), "max"),
        (spec.max_mature_defensive, to_float(row.get("mature_defensive_score")), "max"),
        (spec.max_commercial_risk_overlay, to_float(row.get("commercial_risk_overlay_score")), "max"),
        (spec.max_commercial_deterioration, to_float(row.get("commercial_deterioration_score")), "max"),
    ]
    for threshold, value, mode in checks:
        if threshold is None:
            continue
        if value is None:
            return False
        if mode == "min" and value < threshold:
            return False
        if mode == "max" and value > threshold:
            return False
    return True


def target_cap_for_top_n(spec: PolicySpec, top_n: int) -> int | None:
    if top_n <= 10:
        return spec.cap_top10
    if top_n <= 20:
        return spec.cap_top20
    if spec.cap_top20 is None:
        return None
    return max(spec.cap_top20, int(math.ceil(spec.cap_top20 * top_n / 20.0)))


def adjusted_score(row: dict[str, Any], spec: PolicySpec) -> float:
    score = to_float(row.get("opportunity_score"), 0.0) or 0.0
    if is_target_cohort(row):
        score -= spec.score_penalty
    return score


def select_for_date(rows: list[dict[str, Any]], spec: PolicySpec, top_n: int) -> list[dict[str, Any]]:
    ordered = sorted(
        rows,
        key=lambda row: (
            -adjusted_score(row, spec) if spec.rerank_by_adjusted_score else 0.0,
            int(to_float(row.get("rank"), 999_999) or 999_999),
            normalize_ticker(row.get("ticker")),
        ),
    )
    cap = target_cap_for_top_n(spec, top_n)
    selected: list[dict[str, Any]] = []
    target_count = 0
    candidates = ordered if spec.fill_replacements else [
        row for row in ordered if int(to_float(row.get("rank"), 999_999) or 999_999) <= top_n
    ]
    for row in candidates:
        if not cpg_passes_gates(row, spec):
            continue
        if is_target_cohort(row):
            if cap is not None and target_count >= cap:
                continue
            target_count += 1
        selected.append(row)
        if len(selected) >= top_n:
            break
    return selected


def split_map_for_horizon(observation_rows: list[dict[str, Any]], horizon: int, train_fraction: float) -> dict[str, str]:
    baseline_dates = sorted(
        {
            str(row.get("asof_date") or "")
            for row in observation_rows
            if int(row["horizon_days"]) == horizon
            and row.get("net_return") is not None
            and str(row.get("candidate_id") or "") == "baseline_current_ranking"
        }
    )
    dates = baseline_dates or sorted(
        {
            str(row.get("asof_date") or "")
            for row in observation_rows
            if int(row["horizon_days"]) == horizon and row.get("net_return") is not None
        }
    )
    if len(dates) < 2:
        return {asof_date: "train" for asof_date in dates}
    bounded = max(0.10, min(0.90, float(train_fraction)))
    split_idx = int(math.floor(len(dates) * bounded))
    split_idx = max(1, min(len(dates) - 1, split_idx))
    return {**{d: "train" for d in dates[:split_idx]}, **{d: "test" for d in dates[split_idx:]}}


def selected_ticker_text(rows: list[dict[str, Any]], limit: int = 10) -> str:
    counts = Counter(normalize_ticker(row.get("ticker")) for row in rows)
    counts.pop("", None)
    return "|".join(f"{ticker}:{count}" for ticker, count in counts.most_common(limit))


def build_observations(
    rows_by_date: dict[str, list[dict[str, Any]]],
    specs: list[PolicySpec],
    bars_by_ticker: dict[str, list[Bar]],
    *,
    horizons: list[int],
    top_n_values: list[int],
    next_bar_entry: bool,
    round_trip_cost_bps: float,
) -> list[dict[str, Any]]:
    cost = round_trip_cost_bps / 10_000.0
    out: list[dict[str, Any]] = []
    missing_counts: Counter[tuple[int, str]] = Counter()
    for asof_date, date_rows in rows_by_date.items():
        parsed_asof = parse_date(asof_date)
        for spec in specs:
            for top_n in top_n_values:
                selected = select_for_date(date_rows, spec, top_n)
                for selected_rank, row in enumerate(selected, start=1):
                    ticker = normalize_ticker(row.get("ticker"))
                    for horizon in horizons:
                        ret = None
                        entry_date = ""
                        target_date = ""
                        reason = "invalid_asof_date"
                        if parsed_asof is not None:
                            ret, entry_date, target_date, reason = forward_return(
                                bars_by_ticker.get(ticker, []),
                                parsed_asof,
                                horizon,
                                next_bar_entry=next_bar_entry,
                            )
                        net_return = ret - cost if ret is not None else None
                        if ret is None:
                            missing_counts[(horizon, reason)] += 1
                        out.append(
                            {
                                "candidate_id": spec.candidate_id,
                                "candidate_description": spec.description,
                                "top_n": top_n,
                                "asof_date": asof_date,
                                "ticker": ticker,
                                "company_name": row.get("company_name", ""),
                                "baseline_rank": int(to_float(row.get("rank"), 999_999) or 999_999),
                                "selected_rank": selected_rank,
                                "horizon_days": horizon,
                                "entry_date": entry_date,
                                "target_date": target_date,
                                "return": ret,
                                "net_return": net_return,
                                "missing_return_reason": reason if ret is None else "",
                                "biotech_primary_cohort": row.get("biotech_primary_cohort", ""),
                                "opportunity_score": row.get("opportunity_score", ""),
                                "adjusted_score": round(adjusted_score(row, spec), 6),
                                "momentum_score": row.get("momentum_score", ""),
                                "expected_return_quality_score": row.get("expected_return_quality_score", ""),
                                "value_trap_score": row.get("value_trap_score", ""),
                                "mature_defensive_score": row.get("mature_defensive_score", ""),
                                "commercial_risk_overlay_score": row.get("commercial_risk_overlay_score", ""),
                                "commercial_deterioration_score": row.get("commercial_deterioration_score", ""),
                            }
                        )
    if missing_counts:
        LOGGER.warning(
            "Forward-return coverage gaps: %s",
            ", ".join(f"{horizon}d:{reason}={count}" for (horizon, reason), count in sorted(missing_counts.items())),
        )
    return out


def summarize_candidate_rows(
    observations: list[dict[str, Any]],
    *,
    horizons: list[int],
    top_n_values: list[int],
    train_fraction: float,
    lcb_z: float,
) -> tuple[list[dict[str, Any]], dict[tuple[str, int, int, str], dict[str, Any]]]:
    split_maps = {horizon: split_map_for_horizon(observations, horizon, train_fraction) for horizon in horizons}
    grouped: dict[tuple[str, int, int, str], list[dict[str, Any]]] = defaultdict(list)
    for row in observations:
        if row.get("net_return") is None:
            continue
        horizon = int(row["horizon_days"])
        split = split_maps[horizon].get(str(row.get("asof_date") or ""), "")
        row["evaluation_split"] = split
        for split_name in ("all", split):
            if split_name:
                grouped[(str(row["candidate_id"]), int(row["top_n"]), horizon, split_name)].append(row)

    baseline_date_returns: dict[tuple[int, int, str], dict[str, float]] = defaultdict(dict)
    for (candidate_id, top_n, horizon, split), rows in grouped.items():
        if candidate_id != "baseline_current_ranking" or split not in {"all", "train", "test"}:
            continue
        by_date: dict[str, list[float]] = defaultdict(list)
        for row in rows:
            by_date[str(row["asof_date"])].append(float(row["net_return"]))
        baseline_date_returns[(top_n, horizon, split)] = {d: mean(v) for d, v in by_date.items()}

    summary_rows: list[dict[str, Any]] = []
    summary_by_key: dict[tuple[str, int, int, str], dict[str, Any]] = {}
    for key, rows in sorted(grouped.items()):
        candidate_id, top_n, horizon, split = key
        values = [float(row["net_return"]) for row in rows]
        metrics = summarize(values, lcb_z=lcb_z)
        by_date: dict[str, list[float]] = defaultdict(list)
        for row in rows:
            by_date[str(row["asof_date"])].append(float(row["net_return"]))
        date_means = {d: mean(v) for d, v in by_date.items()}
        baseline_means = baseline_date_returns.get((top_n, horizon, split), {})
        comparable_dates = sorted(set(date_means) & set(baseline_means))
        deltas = [date_means[d] - baseline_means[d] for d in comparable_dates]
        improvement_rate = (sum(1 for delta in deltas if delta > 0.0) / len(deltas) * 100.0) if deltas else 0.0
        target_rows = [row for row in rows if str(row.get("biotech_primary_cohort") or "") == TARGET_COHORT]
        unique_tickers = {str(row.get("ticker") or "") for row in rows}
        summary = {
            "candidate_id": candidate_id,
            "candidate_description": str(rows[0].get("candidate_description") or "") if rows else "",
            "evaluation_split": split,
            "top_n": top_n,
            "horizon_days": horizon,
            "selected_n": len(rows),
            "asof_dates": len(by_date),
            "unique_tickers": len(unique_tickers),
            "target_cohort_selected_n": len(target_rows),
            "target_cohort_exposure_pct": (len(target_rows) / len(rows) * 100.0) if rows else 0.0,
            **{name: round(value, 6) for name, value in metrics.items()},
            "date_improvement_rate_vs_baseline_pct": round(improvement_rate, 6),
            "mean_date_return_delta_vs_baseline_pct": round(mean(deltas) * 100.0, 6) if deltas else 0.0,
            "median_date_return_delta_vs_baseline_pct": round(percentile(deltas, 0.50) * 100.0, 6) if deltas else 0.0,
            "comparable_dates_vs_baseline": len(comparable_dates),
            "top_selected_tickers": selected_ticker_text(rows),
        }
        summary_rows.append(summary)
        summary_by_key[key] = summary
    return summary_rows, summary_by_key


def acceptance_rows(
    specs: list[PolicySpec],
    summary_by_key: dict[tuple[str, int, int, str], dict[str, Any]],
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for spec in specs:
        row20 = summary_by_key.get((spec.candidate_id, 20, 20, "test"), {})
        row60 = summary_by_key.get((spec.candidate_id, 20, 60, "test"), {})
        row120_top10 = summary_by_key.get((spec.candidate_id, 10, 120, "test"), {})
        row120_top20 = summary_by_key.get((spec.candidate_id, 20, 120, "test"), {})
        min_promotion_pass = (
            float(row60.get("profit_factor") or 0.0) >= 0.80
            and float(row60.get("date_improvement_rate_vs_baseline_pct") or 0.0) >= 60.0
            and float(row20.get("profit_factor") or 0.0) >= 0.90
            and float(row20.get("date_improvement_rate_vs_baseline_pct") or 0.0) >= 55.0
            and float(row120_top10.get("mean_return_pct") or 0.0) >= 4.75
            and float(row120_top10.get("lcb_return_pct") or 0.0) >= 2.75
            and float(row120_top10.get("profit_factor") or 0.0) >= 1.75
            and float(row120_top10.get("top3_gain_contribution_pct") or 999.0) <= 40.0
        )
        strict_promotion_pass = (
            min_promotion_pass
            and float(row60.get("profit_factor") or 0.0) >= 1.00
            and float(row20.get("profit_factor") or 0.0) >= 1.00
            and float(row120_top20.get("profit_factor") or 0.0) >= 1.15
        )
        fail_reasons: list[str] = []
        if not min_promotion_pass and spec.candidate_id != "baseline_current_ranking":
            checks = [
                ("60d_top20_pf_lt_0.80", float(row60.get("profit_factor") or 0.0) >= 0.80),
                (
                    "60d_top20_date_improvement_lt_60",
                    float(row60.get("date_improvement_rate_vs_baseline_pct") or 0.0) >= 60.0,
                ),
                ("20d_top20_pf_lt_0.90", float(row20.get("profit_factor") or 0.0) >= 0.90),
                (
                    "20d_top20_date_improvement_lt_55",
                    float(row20.get("date_improvement_rate_vs_baseline_pct") or 0.0) >= 55.0,
                ),
                ("120d_top10_mean_lt_4.75", float(row120_top10.get("mean_return_pct") or 0.0) >= 4.75),
                ("120d_top10_lcb_lt_2.75", float(row120_top10.get("lcb_return_pct") or 0.0) >= 2.75),
                ("120d_top10_pf_lt_1.75", float(row120_top10.get("profit_factor") or 0.0) >= 1.75),
                (
                    "120d_top10_top3_gain_contribution_gt_40",
                    float(row120_top10.get("top3_gain_contribution_pct") or 999.0) <= 40.0,
                ),
            ]
            fail_reasons = [name for name, passed in checks if not passed]
        out.append(
            {
                "candidate_id": spec.candidate_id,
                "candidate_description": spec.description,
                "minimum_promotion_pass": int(min_promotion_pass),
                "strict_promotion_pass": int(strict_promotion_pass),
                "fail_reasons": "|".join(fail_reasons),
                "test_20d_top20_mean_return_pct": row20.get("mean_return_pct", ""),
                "test_20d_top20_lcb_return_pct": row20.get("lcb_return_pct", ""),
                "test_20d_top20_profit_factor": row20.get("profit_factor", ""),
                "test_20d_top20_date_improvement_rate_pct": row20.get("date_improvement_rate_vs_baseline_pct", ""),
                "test_60d_top20_mean_return_pct": row60.get("mean_return_pct", ""),
                "test_60d_top20_lcb_return_pct": row60.get("lcb_return_pct", ""),
                "test_60d_top20_profit_factor": row60.get("profit_factor", ""),
                "test_60d_top20_date_improvement_rate_pct": row60.get("date_improvement_rate_vs_baseline_pct", ""),
                "test_120d_top10_mean_return_pct": row120_top10.get("mean_return_pct", ""),
                "test_120d_top10_lcb_return_pct": row120_top10.get("lcb_return_pct", ""),
                "test_120d_top10_profit_factor": row120_top10.get("profit_factor", ""),
                "test_120d_top10_top3_gain_contribution_pct": row120_top10.get("top3_gain_contribution_pct", ""),
                "test_120d_top20_mean_return_pct": row120_top20.get("mean_return_pct", ""),
                "test_120d_top20_lcb_return_pct": row120_top20.get("lcb_return_pct", ""),
                "test_120d_top20_profit_factor": row120_top20.get("profit_factor", ""),
            }
        )
    return out


def write_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--output-dir", type=Path, default=None)
    parser.add_argument("--start-asof", default=None)
    parser.add_argument("--end-asof", default=None)
    parser.add_argument("--horizons", default=None)
    parser.add_argument("--top-n", default=None)
    parser.add_argument("--candidate-pool-rank-max", type=int, default=None)
    parser.add_argument("--next-bar-entry", action=argparse.BooleanOptionalAction, default=None)
    parser.add_argument("--require-investible", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--round-trip-cost-bps", type=float, default=None)
    parser.add_argument("--train-fraction", type=float, default=None)
    parser.add_argument("--lcb-z", type=float, default=1.0)
    parser.add_argument("--target-cohort", default=None)
    return parser.parse_args()


def main() -> None:
    global TARGET_COHORT
    args = parse_args()
    configure_utc_logging()
    config = load_yaml(args.config)
    config_base = args.config.resolve().parent
    TARGET_COHORT = str(
        args.target_cohort
        or cfg_get(
            config,
            "biotech_scoring.commercial_growth_policy_calibration.target_cohort",
            DEFAULT_TARGET_COHORT,
        )
    ).strip() or DEFAULT_TARGET_COHORT
    db_path_raw = cfg_get(config, "paths.database_path", None)
    if db_path_raw is None:
        db_path_raw = (config.get("database") or {}).get("database_path") if isinstance(config.get("database"), dict) else None
    if db_path_raw is None:
        raise ValueError("Missing database path: expected paths.database_path")
    db_path = args.db or resolve_path(db_path_raw, base_dir=config_base)
    output_dir = args.output_dir or resolve_path(
        cfg_get(
            config,
            "biotech_scoring.commercial_growth_policy_calibration.output_dir",
            "../output/biotech_index_reports/commercial_growth_policy_calibration",
        ),
        base_dir=config_base,
    )
    horizons = parse_int_list(
        args.horizons,
        [
            int(value)
            for value in cfg_get(
                config,
                "biotech_scoring.commercial_growth_policy_calibration.horizons",
                DEFAULT_HORIZONS,
            )
        ],
    )
    top_n_values = parse_int_list(
        args.top_n,
        [
            int(value)
            for value in cfg_get(
                config,
                "biotech_scoring.commercial_growth_policy_calibration.top_n",
                DEFAULT_TOP_N,
            )
        ],
    )
    rank_limit = int(
        args.candidate_pool_rank_max
        or cfg_get(config, "biotech_scoring.commercial_growth_policy_calibration.candidate_pool_rank_max", 60)
    )
    next_bar_entry = (
        bool(args.next_bar_entry)
        if args.next_bar_entry is not None
        else bool(
            cfg_get(
                config,
                "biotech_scoring.commercial_growth_policy_calibration.next_bar_entry",
                cfg_get(config, "calibration.tier1.next_bar_entry", True),
            )
        )
    )
    round_trip_cost_bps = (
        float(args.round_trip_cost_bps)
        if args.round_trip_cost_bps is not None
        else float(
            cfg_get(
                config,
                "biotech_scoring.commercial_growth_policy_calibration.costs.long_round_trip_bps",
                cfg_get(config, "calibration.tier1.costs.long_round_trip_bps", 40.0),
            )
        )
    )
    train_fraction = (
        float(args.train_fraction)
        if args.train_fraction is not None
        else float(
            cfg_get(
                config,
                "biotech_scoring.commercial_growth_policy_calibration.train_fraction",
                cfg_get(config, "calibration.tier1.train_fraction", 0.70),
            )
        )
    )
    if not 0.10 <= train_fraction <= 0.90:
        raise ValueError(f"--train-fraction must be between 0.10 and 0.90, got {train_fraction}")
    specs = candidate_specs()
    market_sources = calibration_market_sources(config)
    with connect_readonly(db_path) as conn:
        dates = load_score_dates(conn, args.start_asof, args.end_asof)
        rows = load_ranked_score_rows(
            conn,
            dates,
            rank_limit=rank_limit,
            require_investible=bool(args.require_investible),
        )
        rows_by_date: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in rows:
            rows_by_date[str(row.get("asof_date") or "")].append(row)
        parsed_dates = [parsed for parsed in (parse_date(row.get("asof_date")) for row in rows) if parsed is not None]
        min_bar_date = min(parsed_dates) - timedelta(days=14) if parsed_dates else date.today()
        tickers = {normalize_ticker(row.get("ticker")) for row in rows if normalize_ticker(row.get("ticker"))}
        bars_by_ticker = load_bars(conn, tickers=tickers, min_date=min_bar_date, market_sources=market_sources)

    observations = build_observations(
        rows_by_date,
        specs,
        bars_by_ticker,
        horizons=horizons,
        top_n_values=top_n_values,
        next_bar_entry=next_bar_entry,
        round_trip_cost_bps=round_trip_cost_bps,
    )
    summary_rows, summary_by_key = summarize_candidate_rows(
        observations,
        horizons=horizons,
        top_n_values=top_n_values,
        train_fraction=train_fraction,
        lcb_z=float(args.lcb_z),
    )
    accept_rows = acceptance_rows(specs, summary_by_key)

    summary_fields = [
        "candidate_id",
        "candidate_description",
        "evaluation_split",
        "top_n",
        "horizon_days",
        "selected_n",
        "asof_dates",
        "unique_tickers",
        "target_cohort_selected_n",
        "target_cohort_exposure_pct",
        "mean_return_pct",
        "median_return_pct",
        "lcb_return_pct",
        "p10_return_pct",
        "win_rate_pct",
        "profit_factor",
        "large_loss_20pct_rate_pct",
        "large_loss_40pct_rate_pct",
        "top3_gain_contribution_pct",
        "date_improvement_rate_vs_baseline_pct",
        "mean_date_return_delta_vs_baseline_pct",
        "median_date_return_delta_vs_baseline_pct",
        "comparable_dates_vs_baseline",
        "top_selected_tickers",
    ]
    observation_fields = [
        "candidate_id",
        "top_n",
        "asof_date",
        "ticker",
        "company_name",
        "baseline_rank",
        "selected_rank",
        "horizon_days",
        "evaluation_split",
        "entry_date",
        "target_date",
        "return",
        "net_return",
        "missing_return_reason",
        "biotech_primary_cohort",
        "opportunity_score",
        "adjusted_score",
        "momentum_score",
        "expected_return_quality_score",
        "value_trap_score",
        "mature_defensive_score",
        "commercial_risk_overlay_score",
        "commercial_deterioration_score",
    ]
    acceptance_fields = [
        "candidate_id",
        "candidate_description",
        "minimum_promotion_pass",
        "strict_promotion_pass",
        "fail_reasons",
        "test_20d_top20_mean_return_pct",
        "test_20d_top20_lcb_return_pct",
        "test_20d_top20_profit_factor",
        "test_20d_top20_date_improvement_rate_pct",
        "test_60d_top20_mean_return_pct",
        "test_60d_top20_lcb_return_pct",
        "test_60d_top20_profit_factor",
        "test_60d_top20_date_improvement_rate_pct",
        "test_120d_top10_mean_return_pct",
        "test_120d_top10_lcb_return_pct",
        "test_120d_top10_profit_factor",
        "test_120d_top10_top3_gain_contribution_pct",
        "test_120d_top20_mean_return_pct",
        "test_120d_top20_lcb_return_pct",
        "test_120d_top20_profit_factor",
    ]
    output_dir.mkdir(parents=True, exist_ok=True)
    write_csv(output_dir / "commercial_growth_policy_summary.csv", summary_rows, summary_fields)
    write_csv(output_dir / "commercial_growth_policy_observations.csv", observations, observation_fields)
    write_csv(output_dir / "commercial_growth_policy_acceptance.csv", accept_rows, acceptance_fields)
    manifest = {
        "db_path": str(db_path),
        "start_asof": args.start_asof,
        "end_asof": args.end_asof,
        "score_dates": len(rows_by_date),
        "ranked_rows_loaded": len(rows),
        "candidate_pool_rank_max": rank_limit,
        "candidate_count": len(specs),
        "observation_rows": len(observations),
        "summary_rows": len(summary_rows),
        "horizons": horizons,
        "top_n": top_n_values,
        "target_cohort": TARGET_COHORT,
        "next_bar_entry": next_bar_entry,
        "round_trip_cost_bps": round_trip_cost_bps,
        "train_fraction": train_fraction,
        "market_sources": market_sources,
        "acceptance_rules": {
            "minimum": [
                "test 60d Top20 PF >= 0.80",
                "test 60d Top20 date improvement rate >= 60%",
                "test 20d Top20 PF >= 0.90",
                "test 20d Top20 date improvement rate >= 55%",
                "test 120d Top10 mean >= 4.75%, LCB >= 2.75%, PF >= 1.75",
                "test 120d Top10 top3 gain contribution <= 40%",
            ],
            "strict": [
                "minimum pass",
                "test 60d Top20 PF >= 1.00",
                "test 20d Top20 PF >= 1.00",
                "test 120d Top20 PF >= 1.15",
            ],
        },
    }
    (output_dir / "commercial_growth_policy_manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    LOGGER.info("Wrote commercial-growth policy summary: %s", output_dir / "commercial_growth_policy_summary.csv")
    LOGGER.info("Wrote commercial-growth policy acceptance: %s", output_dir / "commercial_growth_policy_acceptance.csv")


if __name__ == "__main__":
    main()
