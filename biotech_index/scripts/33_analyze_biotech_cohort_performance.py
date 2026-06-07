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


LOGGER = logging.getLogger("analyze_biotech_cohort_performance")
DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
SQLITE_PARAM_CHUNK_SIZE = 800
DEFAULT_HORIZONS = [20, 60, 120]
DEFAULT_TOP_N = [10, 20]


@dataclass(frozen=True)
class Bar:
    day: date
    close: float


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
    if not math.isfinite(value):
        return default
    return value


def parse_int_list(raw: str | None, default: list[int]) -> list[int]:
    if raw is None or str(raw).strip() == "":
        return list(default)
    values: list[int] = []
    for token in str(raw).replace(";", ",").replace("|", ",").split(","):
        token = token.strip()
        if not token:
            continue
        values.append(int(token))
    return sorted(set(values)) or list(default)


def connect_readonly(db_path: Path) -> sqlite3.Connection:
    uri = f"{db_path.resolve().as_uri()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=30.0)
    conn.row_factory = sqlite3.Row
    return conn


def table_columns(conn: sqlite3.Connection, table_name: str) -> set[str]:
    rows = conn.execute(f"PRAGMA table_info({table_name})").fetchall()
    return {str(row["name"]) for row in rows}


def chunked(items: list[Any], size: int) -> list[list[Any]]:
    return [items[idx : idx + size] for idx in range(0, len(items), size)]


def date_range_filter_sql(start_asof: str | None, end_asof: str | None) -> tuple[str, list[str]]:
    clauses: list[str] = []
    params: list[str] = []
    if start_asof:
        clauses.append("asof_date >= ?")
        params.append(start_asof)
    if end_asof:
        clauses.append("asof_date <= ?")
        params.append(end_asof)
    return ("WHERE " + " AND ".join(clauses)) if clauses else "", params


def load_score_dates(conn: sqlite3.Connection, *, start_asof: str | None, end_asof: str | None) -> list[str]:
    where_sql, params = date_range_filter_sql(start_asof, end_asof)
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


def select_expr(columns: set[str], column: str, fallback_sql: str = "''") -> str:
    return column if column in columns else f"{fallback_sql} AS {column}"


def load_selected_daily_scores(
    conn: sqlite3.Connection,
    dates: list[str],
    *,
    max_top_n: int,
    require_investible: bool,
) -> list[dict[str, Any]]:
    if not dates:
        return []
    columns = table_columns(conn, "daily_scores")
    required = {"asof_date", "company_id", "ticker", "rank"}
    missing = required - columns
    if missing:
        raise RuntimeError(f"daily_scores is missing required columns: {sorted(missing)}")

    selected_columns = [
        "asof_date",
        "company_id",
        "ticker",
        select_expr(columns, "company_name"),
        "rank",
        select_expr(columns, "opportunity_score", "NULL"),
        select_expr(columns, "investment_score", "NULL"),
        select_expr(columns, "bucket"),
        select_expr(columns, "biotech_primary_cohort", "'unmapped_calibration_cohort'"),
        select_expr(columns, "biotech_cohort_calibration_mode", "'unclassified'"),
        select_expr(columns, "biotech_cohort_investible_flag", "1.0"),
        select_expr(columns, "biotech_cohort_calibration_eligible_flag", "1.0"),
        select_expr(columns, "biotech_cohort_exclusion_reason"),
        select_expr(columns, "biotech_cohort_overlays"),
    ]
    rows_out: list[dict[str, Any]] = []
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
            (*date_chunk, max_top_n),
        ).fetchall()
        for row in rows:
            item = dict(row)
            investible_flag = to_float(item.get("biotech_cohort_investible_flag"), 1.0)
            if require_investible and investible_flag is not None and investible_flag <= 0.0:
                continue
            rows_out.append(item)
    return rows_out


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
    if not ordered_sources:
        raise ValueError("At least one market source is required")
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


def profit_factor(values: list[float]) -> float:
    gains = sum(value for value in values if value > 0)
    losses = -sum(value for value in values if value < 0)
    if losses <= 0:
        return 999.0 if gains > 0 else 0.0
    return gains / losses


def lcb_mean(values: list[float], z: float) -> float:
    if not values:
        return 0.0
    avg = mean(values)
    if len(values) < 2:
        return avg
    variance = sum((value - avg) ** 2 for value in values) / (len(values) - 1)
    return avg - z * math.sqrt(variance) / math.sqrt(len(values))


def summarize_returns(values: list[float], *, lcb_z: float) -> dict[str, float]:
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
    }


def top_ticker_text(rows: list[dict[str, Any]], *, limit: int = 8) -> str:
    counts = Counter(normalize_ticker(row.get("ticker")) for row in rows)
    counts.pop("", None)
    return "|".join(f"{ticker}:{count}" for ticker, count in counts.most_common(limit))


def recommendation_for(
    *,
    horizon: int,
    long_horizon: int,
    sample_n: int,
    asof_dates: int,
    mean_pct: float,
    lcb_pct: float,
    pf: float,
    large_loss_20_pct: float,
    min_observations: int,
    min_asof_dates: int,
) -> str:
    if sample_n < min_observations or asof_dates < min_asof_dates:
        return "insufficient_sample"
    if mean_pct < 0.0 and lcb_pct < 0.0 and pf < 1.0:
        return "short_horizon_gate_candidate" if horizon < long_horizon else "review_or_cap_candidate"
    if lcb_pct < 0.0 or pf < 1.15:
        return "watch_or_cap_candidate"
    if large_loss_20_pct > 15.0:
        return "large_loss_cap_candidate"
    return "keep"


def write_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def analyze(
    selected_rows: list[dict[str, Any]],
    bars_by_ticker: dict[str, list[Bar]],
    *,
    horizons: list[int],
    top_n_values: list[int],
    next_bar_entry: bool,
    round_trip_cost_bps: float,
    lcb_z: float,
    train_fraction: float,
    min_observations: int,
    min_asof_dates: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    cost = round_trip_cost_bps / 10_000.0
    summary_rows: list[dict[str, Any]] = []
    observation_rows: list[dict[str, Any]] = []
    missing_counts: Counter[tuple[int, str]] = Counter()
    parsed_cache = {id(row): parse_date(row.get("asof_date")) for row in selected_rows}
    max_top_n = max(top_n_values)

    for row in selected_rows:
        ticker = normalize_ticker(row.get("ticker"))
        asof = parsed_cache[id(row)]
        rank = int(to_float(row.get("rank"), 999_999) or 999_999)
        if rank > max_top_n:
            continue
        for horizon in horizons:
            ret: float | None = None
            entry_date = ""
            target_date = ""
            missing_reason = "invalid_asof_date"
            if asof is not None:
                ret, entry_date, target_date, missing_reason = forward_return(
                    bars_by_ticker.get(ticker, []),
                    asof,
                    horizon,
                    next_bar_entry=next_bar_entry,
                )
            net_ret = ret - cost if ret is not None else None
            if ret is None:
                missing_counts[(horizon, missing_reason)] += 1
            observation_rows.append(
                {
                    "asof_date": row.get("asof_date", ""),
                    "ticker": ticker,
                    "company_name": row.get("company_name", ""),
                    "rank": rank,
                    "horizon_days": horizon,
                    "evaluation_split": "",
                    "entry_date": entry_date,
                    "target_date": target_date,
                    "return_pct": ret * 100.0 if ret is not None else "",
                    "net_return_pct": net_ret * 100.0 if net_ret is not None else "",
                    "missing_return_reason": missing_reason if ret is None else "",
                    "biotech_primary_cohort": row.get("biotech_primary_cohort", ""),
                    "biotech_cohort_calibration_mode": row.get("biotech_cohort_calibration_mode", ""),
                    "biotech_cohort_investible_flag": row.get("biotech_cohort_investible_flag", ""),
                    "biotech_cohort_calibration_eligible_flag": row.get(
                        "biotech_cohort_calibration_eligible_flag",
                        "",
                    ),
                    "biotech_cohort_exclusion_reason": row.get("biotech_cohort_exclusion_reason", ""),
                    "biotech_cohort_overlays": row.get("biotech_cohort_overlays", ""),
                    "opportunity_score": row.get("opportunity_score", ""),
                    "investment_score": row.get("investment_score", ""),
                    "bucket": row.get("bucket", ""),
                }
            )

    if missing_counts:
        LOGGER.warning(
            "Forward-return coverage gaps: %s",
            ", ".join(f"{horizon}d:{reason}={count}" for (horizon, reason), count in sorted(missing_counts.items())),
        )

    for horizon in horizons:
        eligible_dates = sorted(
            {
                str(obs.get("asof_date") or "")
                for obs in observation_rows
                if int(obs["horizon_days"]) == horizon and obs["net_return_pct"] != ""
            }
        )
        if len(eligible_dates) < 2:
            split_by_date = {asof_date: "train" for asof_date in eligible_dates}
        else:
            bounded_fraction = max(0.10, min(0.90, float(train_fraction)))
            split_idx = int(math.floor(len(eligible_dates) * bounded_fraction))
            split_idx = max(1, min(len(eligible_dates) - 1, split_idx))
            split_by_date = {
                **{asof_date: "train" for asof_date in eligible_dates[:split_idx]},
                **{asof_date: "test" for asof_date in eligible_dates[split_idx:]},
            }
        for obs in observation_rows:
            if int(obs["horizon_days"]) == horizon:
                obs["evaluation_split"] = split_by_date.get(str(obs.get("asof_date") or ""), "")

    for top_n in top_n_values:
        for horizon in horizons:
            horizon_eligible = [
                obs
                for obs in observation_rows
                if int(obs["rank"]) <= top_n
                and int(obs["horizon_days"]) == horizon
                and obs["net_return_pct"] != ""
            ]
            sample_sets = {
                "all": horizon_eligible,
                "train": [obs for obs in horizon_eligible if obs.get("evaluation_split") == "train"],
                "test": [obs for obs in horizon_eligible if obs.get("evaluation_split") == "test"],
            }
            for evaluation_split, eligible in sample_sets.items():
                grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
                for obs in eligible:
                    cohort = str(obs.get("biotech_primary_cohort") or "unmapped_calibration_cohort")
                    mode = str(obs.get("biotech_cohort_calibration_mode") or "unclassified")
                    grouped[(cohort, mode)].append(obs)

                for (cohort, mode), group_rows in sorted(grouped.items()):
                    net_values = [float(obs["net_return_pct"]) / 100.0 for obs in group_rows]
                    metrics = summarize_returns(net_values, lcb_z=lcb_z)
                    asof_count = len({str(obs.get("asof_date") or "") for obs in group_rows})
                    unique_tickers = len({str(obs.get("ticker") or "") for obs in group_rows})
                    avg_rank = mean([float(obs["rank"]) for obs in group_rows])
                    avg_score_values = [
                        value
                        for value in (to_float(obs.get("opportunity_score")) for obs in group_rows)
                        if value is not None
                    ]
                    rec = recommendation_for(
                        horizon=horizon,
                        long_horizon=max(horizons),
                        sample_n=len(group_rows),
                        asof_dates=asof_count,
                        mean_pct=metrics["mean_return_pct"],
                        lcb_pct=metrics["lcb_return_pct"],
                        pf=metrics["profit_factor"],
                        large_loss_20_pct=metrics["large_loss_20pct_rate_pct"],
                        min_observations=min_observations,
                        min_asof_dates=min_asof_dates,
                    )
                    summary_rows.append(
                        {
                            "evaluation_split": evaluation_split,
                            "top_n": top_n,
                            "horizon_days": horizon,
                            "biotech_primary_cohort": cohort,
                            "biotech_cohort_calibration_mode": mode,
                            "selected_n": len(group_rows),
                            "asof_dates": asof_count,
                            "unique_tickers": unique_tickers,
                            "avg_rank": round(avg_rank, 4),
                            "avg_opportunity_score": round(mean(avg_score_values), 4) if avg_score_values else "",
                            **{key: round(value, 6) for key, value in metrics.items()},
                            "recommendation": rec,
                            "top_tickers": top_ticker_text(group_rows),
                        }
                    )

    gate_rows = build_gate_candidate_rows(summary_rows)
    return summary_rows, observation_rows, gate_rows


def build_gate_candidate_rows(summary_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_key: dict[tuple[str, int, str, str], dict[int, dict[str, Any]]] = defaultdict(dict)
    for row in summary_rows:
        key = (
            str(row["evaluation_split"]),
            int(row["top_n"]),
            str(row["biotech_primary_cohort"]),
            str(row["biotech_cohort_calibration_mode"]),
        )
        by_key[key][int(row["horizon_days"])] = row

    gate_rows: list[dict[str, Any]] = []
    for (evaluation_split, top_n, cohort, mode), by_horizon in sorted(by_key.items()):
        short_recs = [
            str(by_horizon[horizon].get("recommendation") or "")
            for horizon in (20, 60)
            if horizon in by_horizon
        ]
        h120 = by_horizon.get(120)
        h20 = by_horizon.get(20)
        h60 = by_horizon.get(60)
        if any(rec == "insufficient_sample" for rec in short_recs):
            action = "insufficient_short_horizon_sample"
        elif short_recs and all(rec in {"short_horizon_gate_candidate", "watch_or_cap_candidate"} for rec in short_recs):
            action = "short_horizon_gate_or_cap"
        elif h120 and str(h120.get("recommendation")) == "keep":
            action = "keep_120d_bias"
        else:
            action = "monitor"
        gate_rows.append(
            {
                "evaluation_split": evaluation_split,
                "top_n": top_n,
                "biotech_primary_cohort": cohort,
                "biotech_cohort_calibration_mode": mode,
                "proposed_action": action,
                "h20_selected_n": h20.get("selected_n", "") if h20 else "",
                "h20_mean_return_pct": h20.get("mean_return_pct", "") if h20 else "",
                "h20_lcb_return_pct": h20.get("lcb_return_pct", "") if h20 else "",
                "h20_profit_factor": h20.get("profit_factor", "") if h20 else "",
                "h20_recommendation": h20.get("recommendation", "") if h20 else "",
                "h60_selected_n": h60.get("selected_n", "") if h60 else "",
                "h60_mean_return_pct": h60.get("mean_return_pct", "") if h60 else "",
                "h60_lcb_return_pct": h60.get("lcb_return_pct", "") if h60 else "",
                "h60_profit_factor": h60.get("profit_factor", "") if h60 else "",
                "h60_recommendation": h60.get("recommendation", "") if h60 else "",
                "h120_selected_n": h120.get("selected_n", "") if h120 else "",
                "h120_mean_return_pct": h120.get("mean_return_pct", "") if h120 else "",
                "h120_lcb_return_pct": h120.get("lcb_return_pct", "") if h120 else "",
                "h120_profit_factor": h120.get("profit_factor", "") if h120 else "",
                "h120_recommendation": h120.get("recommendation", "") if h120 else "",
            }
        )
    return gate_rows


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--output-dir", type=Path, default=None)
    parser.add_argument("--start-asof", default=None)
    parser.add_argument("--end-asof", default=None)
    parser.add_argument("--horizons", default=None, help="Comma-separated trading-bar horizons. Default: 20,60,120")
    parser.add_argument("--top-n", default=None, help="Comma-separated rank cutoffs. Default: 10,20")
    parser.add_argument("--next-bar-entry", action=argparse.BooleanOptionalAction, default=None)
    parser.add_argument("--require-investible", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--round-trip-cost-bps", type=float, default=None)
    parser.add_argument("--lcb-z", type=float, default=1.0)
    parser.add_argument("--train-fraction", type=float, default=None)
    parser.add_argument("--min-observations", type=int, default=None)
    parser.add_argument("--min-asof-dates", type=int, default=None)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    configure_utc_logging()
    config = load_yaml(args.config)
    config_base = args.config.resolve().parent
    db_path_raw = cfg_get(config, "paths.database_path", None)
    if db_path_raw is None:
        db_path_raw = (config.get("database") or {}).get("database_path") if isinstance(config.get("database"), dict) else None
    if db_path_raw is None:
        raise ValueError("Missing database path: expected paths.database_path")
    db_path = args.db or resolve_path(db_path_raw, base_dir=config_base)
    output_dir = args.output_dir or resolve_path(
        cfg_get(
            config,
            "biotech_scoring.cohort_performance_attribution.output_dir",
            "../output/biotech_index_reports/cohort_performance_attribution",
        ),
        base_dir=config_base,
    )
    horizons = parse_int_list(
        args.horizons,
        [int(value) for value in cfg_get(config, "biotech_scoring.cohort_performance_attribution.horizons", DEFAULT_HORIZONS)],
    )
    top_n_values = parse_int_list(
        args.top_n,
        [int(value) for value in cfg_get(config, "biotech_scoring.cohort_performance_attribution.top_n", DEFAULT_TOP_N)],
    )
    min_observations = int(
        args.min_observations
        if args.min_observations is not None
        else cfg_get(config, "biotech_scoring.cohort_performance_attribution.min_observations", 30)
    )
    min_asof_dates = int(
        args.min_asof_dates
        if args.min_asof_dates is not None
        else cfg_get(config, "biotech_scoring.cohort_performance_attribution.min_asof_dates", 6)
    )
    if min_observations < 1:
        raise ValueError("min_observations must be >= 1")
    if min_asof_dates < 1:
        raise ValueError("min_asof_dates must be >= 1")
    next_bar_entry = (
        bool(args.next_bar_entry)
        if args.next_bar_entry is not None
        else bool(
            cfg_get(
                config,
                "biotech_scoring.cohort_performance_attribution.next_bar_entry",
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
                "biotech_scoring.cohort_performance_attribution.costs.long_round_trip_bps",
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
                "biotech_scoring.cohort_performance_attribution.train_fraction",
                cfg_get(config, "calibration.tier1.train_fraction", 0.70),
            )
        )
    )
    if not 0.10 <= train_fraction <= 0.90:
        raise ValueError(f"--train-fraction must be between 0.10 and 0.90, got {train_fraction}")
    market_sources = calibration_market_sources(config)
    with connect_readonly(db_path) as conn:
        dates = load_score_dates(conn, start_asof=args.start_asof, end_asof=args.end_asof)
        rows = load_selected_daily_scores(
            conn,
            dates,
            max_top_n=max(top_n_values),
            require_investible=bool(args.require_investible),
        )
        parsed_dates = [parsed for parsed in (parse_date(row.get("asof_date")) for row in rows) if parsed is not None]
        min_bar_date = (min(parsed_dates) - timedelta(days=14)) if parsed_dates else date.today()
        tickers = {normalize_ticker(row.get("ticker")) for row in rows if normalize_ticker(row.get("ticker"))}
        bars_by_ticker = load_bars(conn, tickers=tickers, min_date=min_bar_date, market_sources=market_sources)

    summary_rows, observation_rows, gate_rows = analyze(
        rows,
        bars_by_ticker,
        horizons=horizons,
        top_n_values=top_n_values,
        next_bar_entry=next_bar_entry,
        round_trip_cost_bps=round_trip_cost_bps,
        lcb_z=float(args.lcb_z),
        train_fraction=train_fraction,
        min_observations=min_observations,
        min_asof_dates=min_asof_dates,
    )

    summary_path = output_dir / "biotech_cohort_performance_attribution.csv"
    observation_path = output_dir / "biotech_cohort_performance_observations.csv"
    gate_path = output_dir / "biotech_cohort_gate_candidates.csv"
    manifest_path = output_dir / "biotech_cohort_performance_manifest.json"

    summary_fields = [
        "evaluation_split",
        "top_n",
        "horizon_days",
        "biotech_primary_cohort",
        "biotech_cohort_calibration_mode",
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
        "large_loss_20pct_rate_pct",
        "large_loss_40pct_rate_pct",
        "recommendation",
        "top_tickers",
    ]
    observation_fields = [
        "asof_date",
        "ticker",
        "company_name",
        "rank",
        "horizon_days",
        "evaluation_split",
        "entry_date",
        "target_date",
        "return_pct",
        "net_return_pct",
        "missing_return_reason",
        "biotech_primary_cohort",
        "biotech_cohort_calibration_mode",
        "biotech_cohort_investible_flag",
        "biotech_cohort_calibration_eligible_flag",
        "biotech_cohort_exclusion_reason",
        "biotech_cohort_overlays",
        "opportunity_score",
        "investment_score",
        "bucket",
    ]
    gate_fields = [
        "evaluation_split",
        "top_n",
        "biotech_primary_cohort",
        "biotech_cohort_calibration_mode",
        "proposed_action",
        "h20_selected_n",
        "h20_mean_return_pct",
        "h20_lcb_return_pct",
        "h20_profit_factor",
        "h20_recommendation",
        "h60_selected_n",
        "h60_mean_return_pct",
        "h60_lcb_return_pct",
        "h60_profit_factor",
        "h60_recommendation",
        "h120_selected_n",
        "h120_mean_return_pct",
        "h120_lcb_return_pct",
        "h120_profit_factor",
        "h120_recommendation",
    ]
    write_csv(summary_path, summary_rows, summary_fields)
    write_csv(observation_path, observation_rows, observation_fields)
    write_csv(gate_path, gate_rows, gate_fields)
    manifest = {
        "db_path": str(db_path),
        "start_asof": args.start_asof,
        "end_asof": args.end_asof,
        "score_dates": len(set(row.get("asof_date") for row in rows)),
        "selected_score_rows": len(rows),
        "observation_rows": len(observation_rows),
        "summary_rows": len(summary_rows),
        "gate_rows": len(gate_rows),
        "horizons": horizons,
        "top_n": top_n_values,
        "next_bar_entry": next_bar_entry,
        "round_trip_cost_bps": round_trip_cost_bps,
        "train_fraction": train_fraction,
        "market_sources": market_sources,
        "require_investible": bool(args.require_investible),
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
    LOGGER.info("Wrote cohort attribution: %s", summary_path)
    LOGGER.info("Wrote cohort gate candidates: %s", gate_path)


if __name__ == "__main__":
    main()
