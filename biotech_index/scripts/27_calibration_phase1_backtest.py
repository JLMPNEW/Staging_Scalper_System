#!/usr/bin/env python3
from __future__ import annotations

import argparse
import bisect
import csv
import json
import logging
import math
import random
import sqlite3
import sys
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from statistics import median
from typing import Any, Iterable


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from biotech_index.core.config import cfg_get, load_yaml, normalize_string_list, resolve_path  # noqa: E402
from biotech_index.core.db import connect  # noqa: E402
from biotech_index.core.logging_utils import configure_utc_logging  # noqa: E402
from biotech_index.core.market_policy import calibration_market_sources  # noqa: E402
from biotech_index.core.text_norm import normalize_ticker  # noqa: E402


LOGGER = logging.getLogger("calibration_phase1_backtest")
DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
SQLITE_PARAM_CHUNK_SIZE = 800
DEFAULT_ROUND_TRIP_COST_BPS = 40.0

SCORE_MODELS: dict[str, str] = {
    "biotech_opportunity": "biotech_opportunity_score",
    "biotech_investment": "biotech_investment_score",
    "tier1_gate": "tier1_selection_gate_score",
    "multibagger": "multibagger_score",
    "base_multibagger": "base_multibagger_score",
    "orthogonal_alpha": "orthogonal_alpha_score",
}

BUCKET_DIAGNOSTIC_SCORE_KEYS = [
    "biotech_opportunity_score",
    "tier1_selection_gate_score",
    "multibagger_score",
    "base_multibagger_score",
    "orthogonal_alpha_score",
    "distinctive_acceleration_score",
]
TIER1_GATE_SCORE_KEY = "tier1_selection_gate_score"
TIER1_GATE_SPECS = [
    ("tier1_top_50pct", 0.50),
    ("tier1_top_33pct", 0.33),
]
CONFLICT_HIGH_PERCENTILE = 80.0
CONFLICT_LOW_PERCENTILE = 20.0


@dataclass(frozen=True)
class Bar:
    day: date
    close: float


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Phase 1 calibration/backtest diagnostics for biotech and multibagger scores. "
            "This script reads historical snapshots and market bars, then writes diagnostics only; "
            "it does not change scoring parameters."
        )
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--output-dir", type=Path, default=None)
    parser.add_argument("--start-asof", type=str, default="")
    parser.add_argument("--end-asof", type=str, default="")
    parser.add_argument("--horizons", type=str, default="", help="Comma-separated trading-day horizons. Defaults to calibration.phase1.horizons or calibration.tier1.medium_term_horizons.")
    parser.add_argument("--top-n", type=str, default="10,20,30", help="Comma-separated top-N cutoffs.")
    parser.add_argument("--market-sources", type=str, default="")
    parser.add_argument("--max-snapshots", type=int, default=0, help="Optional limit for smoke tests; keeps latest dates.")
    parser.add_argument("--include-non-fridays", action="store_true", help="Include non-Friday snapshots.")
    parser.add_argument(
        "--exclude-current-removals",
        action=argparse.BooleanOptionalAction,
        default=None,
        help=(
            "Exclude current inactive/remove/manual-exclude companies from historical calibration. "
            "Default comes from calibration.phase1.exclude_current_removals, otherwise false."
        ),
    )
    parser.add_argument(
        "--next-bar-entry",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Enter on the first market bar after the snapshot date. Default true.",
    )
    parser.add_argument(
        "--round-trip-cost-bps",
        type=float,
        default=None,
        help="Round-trip trading cost in basis points used for net-return Top-N diagnostics.",
    )
    parser.add_argument(
        "--train-fraction",
        type=float,
        default=None,
        help="Chronological train fraction for Phase 1 train/test diagnostic splits. Defaults to calibration.phase1.train_fraction or 0.70.",
    )
    parser.add_argument(
        "--embargo-days",
        type=int,
        default=None,
        help=(
            "Calendar-day gap excluded around the train/test split to reduce forward-return overlap leakage. "
            "Defaults to calibration.phase1.embargo_days or the max configured horizon."
        ),
    )
    parser.add_argument(
        "--bootstrap-iterations",
        type=int,
        default=None,
        help="Bootstrap iterations for Phase 1 Top-N risk-adjusted CI diagnostics. Defaults to calibration.phase1.bootstrap_iterations.",
    )
    parser.add_argument("--exclude-tickers", type=str, default="")
    return parser.parse_args()


def configure_logging() -> None:
    configure_utc_logging()


def parse_date(raw: object) -> date | None:
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        return datetime.strptime(text[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def parse_int_list(raw: str, *, default: list[int]) -> list[int]:
    values: list[int] = []
    for part in str(raw or "").split(","):
        part = part.strip()
        if not part:
            continue
        parsed = float(part)
        if not parsed.is_integer():
            raise ValueError(f"Expected integer list value, got {part}")
        value = int(parsed)
        if value <= 0:
            raise ValueError(f"Expected positive integer list value, got {value}")
        values.append(value)
    return values or list(default)


def parse_string_set(raw: object) -> set[str]:
    if raw is None:
        return set()
    if isinstance(raw, str):
        parts = raw.replace(";", ",").split(",")
    elif isinstance(raw, (list, tuple, set)):
        parts = [str(item) for item in raw]
    else:
        parts = [str(raw)]
    return {ticker for part in parts if (ticker := normalize_ticker(part))}


def to_float(raw: object, default: float | None = None) -> float | None:
    if raw is None:
        return default
    if isinstance(raw, bool):
        raw = int(raw)
    try:
        value = float(str(raw))
    except (TypeError, ValueError):
        return default
    return value if math.isfinite(value) else default


def as_bool(raw: object, default: bool = False) -> bool:
    if raw is None:
        return default
    text = str(raw).strip().lower()
    if text in {"1", "true", "t", "yes", "y", "enabled", "on"}:
        return True
    if text in {"0", "false", "f", "no", "n", "disabled", "off"}:
        return False
    return default


def parse_json(raw: object) -> dict[str, Any]:
    try:
        payload = json.loads(str(raw or "{}"))
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def chunked(values: list[Any], size: int = SQLITE_PARAM_CHUNK_SIZE) -> Iterable[list[Any]]:
    step = max(1, int(size))
    for start in range(0, len(values), step):
        yield values[start : start + step]


def table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(row["name"]) for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def optional_select(columns: set[str], column: str, alias: str | None = None, *, table_alias: str = "") -> str:
    out = alias or column
    if column in columns:
        prefix = f"{table_alias}." if table_alias else ""
        return f"{prefix}{column} AS {out}"
    return f"NULL AS {out}"


def load_snapshot_dates(
    conn: sqlite3.Connection,
    *,
    start_asof: date | None,
    end_asof: date | None,
    fridays_only: bool,
    max_snapshots: int,
) -> list[str]:
    rows = conn.execute(
        """
        SELECT d.asof_date
        FROM daily_scores d
        GROUP BY d.asof_date
        ORDER BY d.asof_date
        """
    ).fetchall()
    dates: list[str] = []
    for row in rows:
        text = str(row["asof_date"] or "")
        parsed = parse_date(text)
        if parsed is None:
            continue
        if start_asof and parsed < start_asof:
            continue
        if end_asof and parsed > end_asof:
            continue
        if fridays_only and parsed.weekday() != 4:
            continue
        dates.append(parsed.isoformat())
    if max_snapshots > 0:
        dates = dates[-max_snapshots:]
    return dates


def load_excluded_tickers(conn: sqlite3.Connection, *, exclude_current_removals: bool, extra: set[str]) -> set[str]:
    out = set(extra)
    if exclude_current_removals:
        raise ValueError(
            "calibration.phase1.exclude_current_removals is disabled because current removal status "
            "retroactively excludes historical snapshots. Use calibration.exclude_tickers for explicit "
            "non-temporal exclusions until removal_date history is available."
        )
    return {ticker for ticker in out if ticker}


def load_score_rows(conn: sqlite3.Connection, dates: list[str], excluded_tickers: set[str]) -> list[dict[str, Any]]:
    if not dates:
        return []
    daily_cols = table_columns(conn, "daily_scores")
    multi_cols = table_columns(conn, "multibagger_scores_daily")
    select_columns = [
        "d.asof_date",
        "d.company_id",
        "c.ticker",
        "c.company_name",
        "d.rank AS biotech_rank",
        "d.bucket AS biotech_bucket",
        "d.top_evidence_json AS biotech_top_evidence_json",
        "d.opportunity_score AS biotech_opportunity_score",
        optional_select(daily_cols, "investment_score", "biotech_investment_score", table_alias="d"),
        optional_select(daily_cols, "clinical_opportunity_score", "biotech_clinical_opportunity_score", table_alias="d"),
        "d.risk_score AS biotech_risk_score",
        optional_select(daily_cols, "tier1_selection_gate_score", "tier1_selection_gate_score", table_alias="d"),
        "m.rank AS multibagger_rank",
        "m.bucket AS multibagger_bucket",
        "m.top_evidence_json AS multibagger_top_evidence_json",
        "m.multibagger_score AS multibagger_score",
        optional_select(multi_cols, "base_multibagger_score", "base_multibagger_score", table_alias="m"),
        optional_select(multi_cols, "orthogonal_alpha_score", "orthogonal_alpha_score", table_alias="m"),
        optional_select(multi_cols, "distinctive_acceleration_score", "distinctive_acceleration_score", table_alias="m"),
        optional_select(multi_cols, "tier1_gate_score", "multibagger_tier1_gate_score", table_alias="m"),
        optional_select(multi_cols, "tier1_gate_multiplier", "multibagger_tier1_gate_multiplier", table_alias="m"),
        optional_select(multi_cols, "tier1_available", "multibagger_tier1_available", table_alias="m"),
    ]
    rows_out: list[dict[str, Any]] = []
    excluded = sorted(ticker for ticker in excluded_tickers if ticker)
    exclusion_clause = ""
    exclusion_params: tuple[str, ...] = ()
    if excluded:
        exclusion_placeholders = ",".join("?" for _ in excluded)
        exclusion_clause = f" AND UPPER(c.ticker) NOT IN ({exclusion_placeholders})"
        exclusion_params = tuple(excluded)
    for chunk in chunked(dates):
        placeholders = ",".join("?" for _ in chunk)
        rows = conn.execute(
            f"""
            SELECT {", ".join(select_columns)}
            FROM daily_scores d
            INNER JOIN multibagger_scores_daily m
                ON m.asof_date = d.asof_date
               AND m.company_id = d.company_id
            INNER JOIN companies c ON c.company_id = d.company_id
            WHERE d.asof_date IN ({placeholders})
              {exclusion_clause}
            ORDER BY d.asof_date, d.rank, c.ticker
            """,
            tuple(chunk) + exclusion_params,
        ).fetchall()
        for row in rows:
            record = dict(row)
            record["ticker"] = normalize_ticker(record.get("ticker"))
            if record["ticker"]:
                rows_out.append(record)
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
    source_priority = {source: idx for idx, source in enumerate(market_sources)}
    grouped: dict[tuple[str, str], list[Bar]] = defaultdict(list)
    ordered_tickers = sorted(tickers)
    ordered_sources = [source for source in market_sources if source]
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
            if parsed is None or close is None or close <= 0:
                continue
            ticker = normalize_ticker(row["ticker"])
            if ticker:
                grouped[(ticker, str(row["source"] or ""))].append(Bar(day=parsed, close=close))
    by_ticker: dict[str, list[tuple[int, list[Bar]]]] = defaultdict(list)
    for (group_ticker, source), bars in grouped.items():
        if bars:
            by_ticker[group_ticker].append((source_priority.get(source, 9999), bars))

    out: dict[str, list[Bar]] = {}
    for ticker in ordered_tickers:
        candidates = by_ticker.get(ticker, [])
        if not candidates:
            continue
        out[ticker] = sorted(min(candidates, key=lambda item: item[0])[1], key=lambda bar: bar.day)
    return out


def forward_return(
    bars: list[Bar],
    asof: date,
    horizon: int,
    *,
    next_bar_entry: bool,
) -> tuple[float | None, str, str]:
    if not bars:
        return None, "", ""
    days = [bar.day for bar in bars]
    entry_idx = bisect.bisect_right(days, asof) if next_bar_entry else bisect.bisect_left(days, asof)
    if entry_idx < 0 or entry_idx >= len(bars):
        return None, "", ""
    target_idx = entry_idx + horizon
    if target_idx >= len(bars):
        return None, bars[entry_idx].day.isoformat(), ""
    entry = bars[entry_idx]
    target = bars[target_idx]
    if entry.close <= 0:
        return None, entry.day.isoformat(), target.day.isoformat()
    return (target.close / entry.close) - 1.0, entry.day.isoformat(), target.day.isoformat()


def add_forward_returns(
    rows: list[dict[str, Any]],
    bars_by_ticker: dict[str, list[Bar]],
    horizons: list[int],
    *,
    round_trip_cost_bps: float,
    next_bar_entry: bool,
) -> None:
    cost = float(round_trip_cost_bps) / 10_000.0
    missing_return_counts: defaultdict[tuple[int, str], int] = defaultdict(int)
    for row in rows:
        ticker = normalize_ticker(row.get("ticker"))
        asof = parse_date(row.get("asof_date"))
        bars = bars_by_ticker.get(ticker, [])
        for horizon in horizons:
            prefix = f"fwd_{horizon}d"
            if asof is None:
                row[f"{prefix}_return"] = ""
                row[f"{prefix}_net_return"] = ""
                row[f"{prefix}_round_trip_cost_bps"] = round_trip_cost_bps
                row[f"{prefix}_entry_date"] = ""
                row[f"{prefix}_target_date"] = ""
                missing_return_counts[(horizon, "invalid_asof_date")] += 1
                continue
            ret, entry_date, target_date = forward_return(bars, asof, horizon, next_bar_entry=next_bar_entry)
            row[f"{prefix}_return"] = ret if ret is not None else ""
            row[f"{prefix}_net_return"] = ret - cost if ret is not None else ""
            row[f"{prefix}_round_trip_cost_bps"] = round_trip_cost_bps
            row[f"{prefix}_entry_date"] = entry_date
            row[f"{prefix}_target_date"] = target_date
            if ret is None:
                if not bars:
                    reason = "no_market_bars"
                elif not entry_date:
                    reason = "no_entry_bar"
                elif not target_date:
                    reason = "insufficient_horizon_bars"
                else:
                    reason = "invalid_entry_close"
                missing_return_counts[(horizon, reason)] += 1
    if missing_return_counts:
        summary = ", ".join(
            f"{horizon}d:{reason}={count}"
            for (horizon, reason), count in sorted(missing_return_counts.items())
        )
        LOGGER.warning("Forward-return coverage gaps: %s", summary)


def nested_dict(payload: dict[str, Any], key: str) -> dict[str, Any]:
    value = payload.get(key)
    return value if isinstance(value, dict) else {}


def first_float(*values: object) -> float | None:
    for value in values:
        parsed = to_float(value)
        if parsed is not None:
            return parsed
    return None


def add_percentile_by_date(rows: list[dict[str, Any]], source_key: str, output_key: str) -> None:
    for row in rows:
        row[output_key] = ""
    rows_by_date: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        rows_by_date[str(row.get("asof_date") or "")].append(row)
    for date_rows in rows_by_date.values():
        scored = [(to_float(row.get(source_key)), idx, row) for idx, row in enumerate(date_rows)]
        scored = [(score, idx, row) for score, idx, row in scored if score is not None]
        scored.sort(key=lambda item: (item[0], item[1]))
        n = len(scored)
        if n == 1:
            scored[0][2][output_key] = 50.0
            continue
        i = 0
        while i < n:
            j = i
            while j + 1 < n and scored[j + 1][0] == scored[i][0]:
                j += 1
            percentile = 100.0 * ((i + j) / 2.0) / float(max(1, n - 1))
            for k in range(i, j + 1):
                scored[k][2][output_key] = percentile
            i = j + 1


def add_bucket_diagnostic_fields(rows: list[dict[str, Any]], horizons: list[int], *, min_addv20: float) -> None:
    for row in rows:
        multi = parse_json(row.get("multibagger_top_evidence_json"))
        daily = parse_json(row.get("biotech_top_evidence_json"))
        components = nested_dict(multi, "component_scores")
        market = nested_dict(multi, "market")
        commercial = nested_dict(multi, "commercial")
        survival = nested_dict(multi, "survival")
        risk = nested_dict(multi, "risk")
        daily_risk = nested_dict(daily, "risk_flags")

        addv = first_float(market.get("avg_dollar_volume_20d"), risk.get("median_addv20"), daily_risk.get("median_addv20"))
        row["diag_multibagger_risk_penalty"] = first_float(components.get("multibagger_risk_penalty"))
        row["diag_commercial_fragility_risk_score"] = first_float(components.get("commercial_fragility_risk_score"))
        row["diag_avg_dollar_volume_20d"] = addv
        row["diag_liquidity_score"] = first_float(market.get("liquidity_score"))
        row["diag_liquidity_ok"] = 1.0 if addv is not None and addv >= min_addv20 else 0.0 if addv is not None else ""
        row["diag_market_cap"] = first_float(commercial.get("market_cap"))
        row["diag_cash_runway_months"] = first_float(survival.get("cash_runway_months"), daily_risk.get("cash_runway_months"))
        row["diag_reverse_split_hits_2y"] = first_float(risk.get("reverse_split_hits_2y"), daily_risk.get("reverse_split_hits_2y"))

    for score_key in BUCKET_DIAGNOSTIC_SCORE_KEYS:
        add_percentile_by_date(rows, score_key, f"diag_{score_key}_percentile")
    add_percentile_by_date(rows, "diag_multibagger_risk_penalty", "diag_multibagger_risk_penalty_percentile")
    add_percentile_by_date(
        rows,
        "diag_commercial_fragility_risk_score",
        "diag_commercial_fragility_risk_score_percentile",
    )
    for horizon in horizons:
        add_percentile_by_date(rows, f"fwd_{horizon}d_return", f"diag_fwd_{horizon}d_return_percentile")


def numeric_pairs(rows: Iterable[dict[str, Any]], x_key: str, y_key: str) -> list[tuple[float, float]]:
    pairs: list[tuple[float, float]] = []
    for row in rows:
        x = to_float(row.get(x_key))
        y = to_float(row.get(y_key))
        if x is not None and y is not None:
            pairs.append((x, y))
    return pairs


def pearson_from_pairs(pairs: list[tuple[float, float]]) -> float | None:
    n = len(pairs)
    if n < 3:
        return None
    xs = [pair[0] for pair in pairs]
    ys = [pair[1] for pair in pairs]
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    cov = sum((x - mean_x) * (y - mean_y) for x, y in pairs)
    var_x = sum((x - mean_x) ** 2 for x in xs)
    var_y = sum((y - mean_y) ** 2 for y in ys)
    denom = math.sqrt(var_x * var_y)
    if denom <= 0:
        return None
    return cov / denom


def ranks(values: list[float]) -> list[float]:
    indexed = sorted(enumerate(values), key=lambda item: item[1])
    out = [0.0] * len(values)
    i = 0
    while i < len(indexed):
        j = i
        while j + 1 < len(indexed) and indexed[j + 1][1] == indexed[i][1]:
            j += 1
        rank = (i + j) / 2.0 + 1.0
        for k in range(i, j + 1):
            out[indexed[k][0]] = rank
        i = j + 1
    return out


def spearman_from_pairs(pairs: list[tuple[float, float]]) -> float | None:
    if len(pairs) < 3:
        return None
    xs = [pair[0] for pair in pairs]
    ys = [pair[1] for pair in pairs]
    return pearson_from_pairs(list(zip(ranks(xs), ranks(ys))))


def linear_residual_pairs(rows: Iterable[dict[str, Any]], predictor_key: str, y_key: str, residual_score_key: str) -> list[tuple[float, float]]:
    rows = list(rows)
    base_pairs = numeric_pairs(rows, predictor_key, y_key)
    if len(base_pairs) < 3:
        return []
    xs = [pair[0] for pair in base_pairs]
    ys = [pair[1] for pair in base_pairs]
    mean_x = sum(xs) / len(xs)
    mean_y = sum(ys) / len(ys)
    var_x = sum((x - mean_x) ** 2 for x in xs)
    if var_x <= 0:
        return []
    slope = sum((x - mean_x) * (y - mean_y) for x, y in base_pairs) / var_x
    intercept = mean_y - slope * mean_x
    out: list[tuple[float, float]] = []
    for row in rows:
        predictor = to_float(row.get(predictor_key))
        y = to_float(row.get(y_key))
        residual_score = to_float(row.get(residual_score_key))
        if predictor is None or y is None or residual_score is None:
            continue
        out.append((residual_score, y - (intercept + slope * predictor)))
    return out


def mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def stdev(values: list[float]) -> float | None:
    if len(values) < 2:
        return None
    avg = sum(values) / len(values)
    return math.sqrt(sum((value - avg) ** 2 for value in values) / (len(values) - 1))


def quantile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    pos = max(0.0, min(1.0, q)) * (len(ordered) - 1)
    lower = int(math.floor(pos))
    upper = int(math.ceil(pos))
    if lower == upper:
        return ordered[lower]
    weight = pos - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def pct(value: float | None) -> float | str:
    return "" if value is None else round(100.0 * value, 6)


def rounded(value: float | None, digits: int = 6) -> float | str:
    return "" if value is None or not math.isfinite(value) else round(value, digits)


def safe_ratio(numerator: float | None, denominator: float | None) -> float | None:
    if numerator is None or denominator is None or abs(denominator) <= 1e-12:
        return None
    return numerator / denominator


def profit_factor(values: list[float]) -> float | None:
    gains = sum(value for value in values if value > 0.0)
    losses = -sum(value for value in values if value < 0.0)
    if losses <= 1e-12:
        return None if gains <= 1e-12 else 999.0
    return gains / losses


def winsorized_mean(values: list[float], lower_q: float = 0.05, upper_q: float = 0.95) -> float | None:
    if not values:
        return None
    lower = quantile(values, lower_q)
    upper = quantile(values, upper_q)
    if lower is None or upper is None:
        return None
    clipped = [min(max(value, lower), upper) for value in values]
    return mean(clipped)


def summarize_returns(values: list[float]) -> dict[str, Any]:
    if not values:
        return {"n": 0, "mean_return_pct": "", "median_return_pct": "", "hit_rate_pct": "", "loss_rate_pct": "", "mean_positive_pct": "", "mean_negative_pct": ""}
    positives = [value for value in values if value > 0]
    negatives = [value for value in values if value < 0]
    return {
        "n": len(values),
        "mean_return_pct": pct(mean(values)),
        "median_return_pct": pct(median(values)),
        "hit_rate_pct": round(100.0 * len(positives) / len(values), 6),
        "loss_rate_pct": round(100.0 * len(negatives) / len(values), 6),
        "mean_positive_pct": pct(mean(positives)),
        "mean_negative_pct": pct(mean(negatives)),
    }


def summarize_return_risk(values: list[float]) -> dict[str, Any]:
    if not values:
        return {
            "winsorized_mean_return_pct": "",
            "stdev_return_pct": "",
            "downside_deviation_pct": "",
            "sharpe_like": "",
            "sortino_like": "",
            "gain_loss_ratio": "",
            "worst_return_pct": "",
            "best_return_pct": "",
            "p05_return_pct": "",
            "p10_return_pct": "",
            "p25_return_pct": "",
            "p75_return_pct": "",
            "p90_return_pct": "",
            "large_loss_20pct_rate_pct": "",
            "large_loss_40pct_rate_pct": "",
            "large_gain_20pct_rate_pct": "",
        }
    positives = [value for value in values if value > 0]
    losses = [value for value in values if value < 0]
    negatives = [min(0.0, value) for value in values]
    avg = mean(values)
    volatility = stdev(values)
    downside = math.sqrt(sum(value ** 2 for value in negatives) / len(values))
    avg_positive = mean(positives)
    avg_loss = mean(losses)
    return {
        "winsorized_mean_return_pct": pct(winsorized_mean(values)),
        "stdev_return_pct": pct(stdev(values)),
        "downside_deviation_pct": pct(downside),
        "sharpe_like": rounded(safe_ratio(avg, volatility)),
        "sortino_like": rounded(safe_ratio(avg, downside)),
        "gain_loss_ratio": rounded(safe_ratio(avg_positive, abs(avg_loss) if avg_loss is not None else None)),
        "worst_return_pct": pct(min(values)),
        "best_return_pct": pct(max(values)),
        "p05_return_pct": pct(quantile(values, 0.05)),
        "p10_return_pct": pct(quantile(values, 0.10)),
        "p25_return_pct": pct(quantile(values, 0.25)),
        "p75_return_pct": pct(quantile(values, 0.75)),
        "p90_return_pct": pct(quantile(values, 0.90)),
        "large_loss_20pct_rate_pct": round(100.0 * sum(1 for value in values if value <= -0.20) / len(values), 6),
        "large_loss_40pct_rate_pct": round(100.0 * sum(1 for value in values if value <= -0.40) / len(values), 6),
        "large_gain_20pct_rate_pct": round(100.0 * sum(1 for value in values if value >= 0.20) / len(values), 6),
    }


def prefixed(prefix: str, values: dict[str, Any]) -> dict[str, Any]:
    return {f"{prefix}{key}": value for key, value in values.items()}


def bootstrap_metric_cis(values: list[float], *, iterations: int, seed: int, block_size: int = 1) -> dict[str, Any]:
    if not values or iterations <= 0:
        return {}
    rng = random.Random(seed)
    n = len(values)
    effective_block_size = max(1, min(n, int(block_size)))
    mean_samples: list[float] = []
    sortino_samples: list[float] = []
    profit_samples: list[float] = []
    for _ in range(iterations):
        if effective_block_size <= 1:
            sample = [values[rng.randrange(n)] for _ in range(n)]
        else:
            sample = []
            while len(sample) < n:
                start = rng.randrange(n)
                for offset in range(effective_block_size):
                    sample.append(values[(start + offset) % n])
                    if len(sample) >= n:
                        break
        avg = mean(sample)
        if avg is None:
            continue
        downside = math.sqrt(sum(min(0.0, value) ** 2 for value in sample) / len(sample))
        mean_samples.append(avg)
        sortino_value = safe_ratio(avg, downside)
        if sortino_value is not None:
            sortino_samples.append(sortino_value)
        pf = profit_factor(sample)
        if pf is not None:
            profit_samples.append(pf)
    return {
        "mean_return_pct_ci05": pct(quantile(mean_samples, 0.05)),
        "mean_return_pct_ci95": pct(quantile(mean_samples, 0.95)),
        "sortino_like_ci05": rounded(quantile(sortino_samples, 0.05)),
        "sortino_like_ci95": rounded(quantile(sortino_samples, 0.95)),
        "profit_factor_ci05": rounded(quantile(profit_samples, 0.05)),
        "profit_factor_ci95": rounded(quantile(profit_samples, 0.95)),
        "bootstrap_block_size_observations": effective_block_size,
    }


def risk_adjusted_summary(
    values: list[float],
    *,
    bootstrap_iterations: int = 0,
    seed: int = 0,
    bootstrap_block_size: int = 1,
) -> dict[str, Any]:
    summary = {**summarize_returns(values), **summarize_return_risk(values)}
    summary["profit_factor"] = rounded(profit_factor(values))
    summary.update(
        bootstrap_metric_cis(
            values,
            iterations=bootstrap_iterations,
            seed=seed,
            block_size=bootstrap_block_size,
        )
    )
    return summary


def numeric_values(rows: Iterable[dict[str, Any]], key: str) -> list[float]:
    values: list[float] = []
    for row in rows:
        value = to_float(row.get(key))
        if value is not None:
            values.append(value)
    return values


def mean_numeric(rows: Iterable[dict[str, Any]], key: str) -> float | str:
    return rounded(mean(numeric_values(rows, key)))


def median_numeric(rows: Iterable[dict[str, Any]], key: str) -> float | str:
    values = numeric_values(rows, key)
    return rounded(median(values)) if values else ""


def pct_where(rows: Iterable[dict[str, Any]], key: str, threshold: float, *, op: str) -> float | str:
    values = numeric_values(rows, key)
    if not values:
        return ""
    if op == ">=":
        count = sum(1 for value in values if value >= threshold)
    elif op == "<=":
        count = sum(1 for value in values if value <= threshold)
    else:
        raise ValueError(f"Unsupported pct_where op: {op}")
    return round(100.0 * count / len(values), 6)


def bucket_family(bucket_type: str, bucket: str) -> str:
    text = bucket.lower().strip()
    if not text or text == "blank":
        return "blank"
    if text.startswith("avoid"):
        if "illiquid" in text:
            return "risk_liquidity_flag"
        if "risk" in text or "fragility" in text or "conflict" in text:
            return "risk_gate_flag"
        return "avoid_label"
    if "watch" in text:
        return "watchlist_label"
    if "speculative" in text:
        return "speculative_label"
    if bucket_type == "biotech_bucket":
        return "tier1_label"
    return "other"


def build_correlation_rows(rows: list[dict[str, Any]], horizons: list[int]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    rows_by_date: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        rows_by_date[str(row.get("asof_date") or "")].append(row)
    for horizon in horizons:
        ret_key = f"fwd_{horizon}d_return"
        for model_name, score_key in SCORE_MODELS.items():
            pairs = numeric_pairs(rows, score_key, ret_key)
            cs_pearson_values: list[float] = []
            cs_spearman_values: list[float] = []
            cs_obs_counts: list[float] = []
            for date_rows in rows_by_date.values():
                date_pairs = numeric_pairs(date_rows, score_key, ret_key)
                if len(date_pairs) < 5:
                    continue
                pearson_value = pearson_from_pairs(date_pairs)
                spearman_value = spearman_from_pairs(date_pairs)
                if pearson_value is not None:
                    cs_pearson_values.append(pearson_value)
                if spearman_value is not None:
                    cs_spearman_values.append(spearman_value)
                    cs_obs_counts.append(float(len(date_pairs)))
            cs_spearman_mean = mean(cs_spearman_values)
            cs_spearman_std = stdev(cs_spearman_values)
            cs_spearman_t = safe_ratio(
                cs_spearman_mean,
                (cs_spearman_std / math.sqrt(len(cs_spearman_values)))
                if cs_spearman_std not in {None, 0.0} and cs_spearman_values
                else None,
            )
            residual_pairs = (
                linear_residual_pairs(rows, "biotech_opportunity_score", ret_key, score_key)
                if score_key != "biotech_opportunity_score"
                else []
            )
            out.append(
                {
                    "horizon_days": horizon,
                    "model": model_name,
                    "score_column": score_key,
                    "n": len(pairs),
                    "pearson_score_return": rounded(pearson_from_pairs(pairs)),
                    "spearman_score_return": rounded(spearman_from_pairs(pairs)),
                    "cross_sectional_ic_dates": len(cs_spearman_values),
                    "cross_sectional_avg_obs_per_date": rounded(mean(cs_obs_counts)),
                    "cross_sectional_mean_pearson": rounded(mean(cs_pearson_values)),
                    "cross_sectional_mean_spearman": rounded(cs_spearman_mean),
                    "cross_sectional_spearman_std": rounded(cs_spearman_std),
                    "cross_sectional_spearman_t_stat": rounded(cs_spearman_t),
                    "pearson_score_residual_return_after_biotech": rounded(pearson_from_pairs(residual_pairs)),
                    "spearman_score_residual_return_after_biotech": rounded(spearman_from_pairs(residual_pairs)),
                }
            )
    return out


def build_decile_rows(rows: list[dict[str, Any]], horizons: list[int]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for horizon in horizons:
        ret_key = f"fwd_{horizon}d_return"
        for model_name, score_key in SCORE_MODELS.items():
            scored = [
                (to_float(row.get(score_key)), to_float(row.get(ret_key)))
                for row in rows
                if to_float(row.get(score_key)) is not None and to_float(row.get(ret_key)) is not None
            ]
            scored = [(score, ret) for score, ret in scored if score is not None and ret is not None]
            scored.sort(key=lambda item: item[0])
            n = len(scored)
            if n == 0:
                continue
            groups: dict[int, list[float]] = defaultdict(list)
            for idx, (_, ret) in enumerate(scored):
                decile = min(10, int(idx * 10 / n) + 1)
                groups[decile].append(ret)
            for decile in range(1, 11):
                summary = summarize_returns(groups.get(decile, []))
                out.append(
                    {
                        "horizon_days": horizon,
                        "model": model_name,
                        "decile": decile,
                        "score_decile_order": "1_lowest_score_to_10_highest_score",
                        **summary,
                    }
                )
    return out


def build_bucket_rows(rows: list[dict[str, Any]], horizons: list[int]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    bucket_specs = [
        ("biotech_bucket", "biotech_bucket"),
        ("multibagger_bucket", "multibagger_bucket"),
    ]
    for horizon in horizons:
        ret_key = f"fwd_{horizon}d_return"
        for label, bucket_key in bucket_specs:
            groups: dict[str, list[float]] = defaultdict(list)
            for row in rows:
                bucket = str(row.get(bucket_key) or "").strip() or "blank"
                ret = to_float(row.get(ret_key))
                if ret is not None:
                    groups[bucket].append(ret)
            for bucket in sorted(groups):
                out.append({"horizon_days": horizon, "bucket_type": label, "bucket": bucket, **summarize_returns(groups[bucket])})
    return out


def build_bucket_diagnostic_rows(
    rows: list[dict[str, Any]],
    horizons: list[int],
    *,
    sample: str = "all",
) -> list[dict[str, Any]]:
    """Diagnose whether bucket labels behave like return ranks or risk/liquidity flags."""
    out: list[dict[str, Any]] = []
    bucket_specs = [
        ("biotech_bucket", "biotech_bucket"),
        ("multibagger_bucket", "multibagger_bucket"),
    ]
    for horizon in horizons:
        ret_key = f"fwd_{horizon}d_return"
        ret_pctile_key = f"diag_fwd_{horizon}d_return_percentile"
        universe_returns = numeric_values(rows, ret_key)
        universe_mean = mean(universe_returns)
        for label, bucket_key in bucket_specs:
            groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
            for row in rows:
                if to_float(row.get(ret_key)) is None:
                    continue
                bucket = str(row.get(bucket_key) or "").strip() or "blank"
                groups[bucket].append(row)
            for bucket in sorted(groups):
                bucket_rows = groups[bucket]
                returns = numeric_values(bucket_rows, ret_key)
                bucket_mean = mean(returns)
                top_decile_pct = pct_where(bucket_rows, ret_pctile_key, 90.0, op=">=")
                bottom_decile_pct = pct_where(bucket_rows, ret_pctile_key, 10.0, op="<=")
                top_decile_rate = to_float(top_decile_pct)
                bottom_decile_rate = to_float(bottom_decile_pct)
                top_minus_bottom_decile = (
                    top_decile_rate - bottom_decile_rate
                    if top_decile_rate is not None and bottom_decile_rate is not None
                    else None
                )
                out.append(
                    {
                        "horizon_days": horizon,
                        "sample": sample,
                        "bucket_type": label,
                        "bucket": bucket,
                        "bucket_family": bucket_family(label, bucket),
                        **summarize_returns(returns),
                        **summarize_return_risk(returns),
                        "mean_return_spread_vs_universe_pct": pct(
                            bucket_mean - universe_mean
                            if bucket_mean is not None and universe_mean is not None
                            else None
                        ),
                        "mean_return_percentile": mean_numeric(bucket_rows, ret_pctile_key),
                        "top_return_decile_pct": top_decile_pct,
                        "bottom_return_decile_pct": bottom_decile_pct,
                        "top_minus_bottom_return_decile_pct": rounded(top_minus_bottom_decile),
                        "mean_biotech_opportunity_score": mean_numeric(bucket_rows, "biotech_opportunity_score"),
                        "mean_biotech_opportunity_percentile": mean_numeric(
                            bucket_rows, "diag_biotech_opportunity_score_percentile"
                        ),
                        "mean_biotech_risk_score": mean_numeric(bucket_rows, "biotech_risk_score"),
                        "mean_tier1_gate_score": mean_numeric(bucket_rows, "tier1_selection_gate_score"),
                        "mean_tier1_gate_percentile": mean_numeric(
                            bucket_rows, "diag_tier1_selection_gate_score_percentile"
                        ),
                        "mean_multibagger_score": mean_numeric(bucket_rows, "multibagger_score"),
                        "mean_multibagger_percentile": mean_numeric(bucket_rows, "diag_multibagger_score_percentile"),
                        "mean_base_multibagger_score": mean_numeric(bucket_rows, "base_multibagger_score"),
                        "mean_orthogonal_alpha_score": mean_numeric(bucket_rows, "orthogonal_alpha_score"),
                        "mean_orthogonal_alpha_percentile": mean_numeric(
                            bucket_rows, "diag_orthogonal_alpha_score_percentile"
                        ),
                        "mean_distinctive_acceleration_score": mean_numeric(
                            bucket_rows, "distinctive_acceleration_score"
                        ),
                        "mean_distinctive_acceleration_percentile": mean_numeric(
                            bucket_rows, "diag_distinctive_acceleration_score_percentile"
                        ),
                        "mean_tier1_risk_score": mean_numeric(bucket_rows, "biotech_risk_score"),
                        "mean_multibagger_risk_penalty": mean_numeric(bucket_rows, "diag_multibagger_risk_penalty"),
                        "mean_multibagger_risk_penalty_percentile": mean_numeric(
                            bucket_rows, "diag_multibagger_risk_penalty_percentile"
                        ),
                        "mean_commercial_fragility_risk_score": mean_numeric(
                            bucket_rows, "diag_commercial_fragility_risk_score"
                        ),
                        "mean_commercial_fragility_risk_percentile": mean_numeric(
                            bucket_rows, "diag_commercial_fragility_risk_score_percentile"
                        ),
                        "mean_liquidity_score": mean_numeric(bucket_rows, "diag_liquidity_score"),
                        "median_avg_dollar_volume_20d": median_numeric(bucket_rows, "diag_avg_dollar_volume_20d"),
                        "liquidity_ok_pct": pct_where(bucket_rows, "diag_liquidity_ok", 1.0, op=">="),
                        "median_market_cap": median_numeric(bucket_rows, "diag_market_cap"),
                        "median_cash_runway_months": median_numeric(bucket_rows, "diag_cash_runway_months"),
                        "mean_reverse_split_hits_2y": mean_numeric(bucket_rows, "diag_reverse_split_hits_2y"),
                    }
                )
    return out


def build_topn_rows(rows: list[dict[str, Any]], horizons: list[int], top_ns: list[int]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    rows_by_date: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        rows_by_date[str(row.get("asof_date") or "")].append(row)
    for horizon in horizons:
        ret_key = f"fwd_{horizon}d_net_return"
        for model_name, score_key in SCORE_MODELS.items():
            for top_n in top_ns:
                selected_returns: list[float] = []
                date_count = 0
                selected_counts: list[int] = []
                for asof_date, date_rows in rows_by_date.items():
                    candidates = [
                        row for row in date_rows
                        if to_float(row.get(score_key)) is not None and to_float(row.get(ret_key)) is not None
                    ]
                    if not candidates:
                        continue
                    candidates.sort(key=lambda row: (to_float(row.get(score_key), -1e9) or -1e9), reverse=True)
                    selected = candidates[:top_n]
                    selected_rets = [to_float(row.get(ret_key)) for row in selected]
                    selected_rets = [ret for ret in selected_rets if ret is not None]
                    if not selected_rets:
                        continue
                    date_count += 1
                    selected_counts.append(len(selected_rets))
                    selected_returns.extend(selected_rets)
                summary = summarize_returns(selected_returns)
                out.append(
                    {
                        "horizon_days": horizon,
                        "return_basis": "net_after_round_trip_costs",
                        "model": model_name,
                        "top_n": top_n,
                        "asof_dates": date_count,
                        "avg_names_per_date": rounded(mean([float(v) for v in selected_counts])),
                        **summary,
                    }
                )
    return out


def summary_metric_spread(left: dict[str, Any], right: dict[str, Any], key: str) -> float | str:
    left_value = to_float(left.get(key))
    right_value = to_float(right.get(key))
    return rounded(left_value - right_value) if left_value is not None and right_value is not None else ""


def build_topn_risk_adjusted_rows(
    rows: list[dict[str, Any]],
    horizons: list[int],
    top_ns: list[int],
    *,
    sample: str,
    bootstrap_iterations: int = 0,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    rows_by_date: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        rows_by_date[str(row.get("asof_date") or "")].append(row)
    for horizon in horizons:
        ret_key = f"fwd_{horizon}d_net_return"
        bootstrap_block_size = max(1, int(math.ceil(float(horizon) / 5.0)))
        for model_name, score_key in SCORE_MODELS.items():
            for top_n in top_ns:
                selected_returns: list[float] = []
                baseline_returns: list[float] = []
                selected_counts: list[int] = []
                date_count = 0
                for date_rows in rows_by_date.values():
                    candidates = [
                        row
                        for row in date_rows
                        if to_float(row.get(score_key)) is not None and to_float(row.get(ret_key)) is not None
                    ]
                    if not candidates:
                        continue
                    candidates.sort(key=lambda row: (to_float(row.get(score_key), -1e9) or -1e9), reverse=True)
                    selected = candidates[:top_n]
                    selected_rets = [to_float(row.get(ret_key)) for row in selected]
                    selected_rets = [ret for ret in selected_rets if ret is not None]
                    base_rets = [to_float(row.get(ret_key)) for row in candidates]
                    base_rets = [ret for ret in base_rets if ret is not None]
                    if not selected_rets or not base_rets:
                        continue
                    date_count += 1
                    selected_counts.append(len(selected_rets))
                    selected_returns.extend(selected_rets)
                    baseline_returns.extend(base_rets)

                seed_text = f"{sample}|{horizon}|{model_name}|{top_n}"
                seed_base = sum((idx + 1) * ord(ch) for idx, ch in enumerate(seed_text)) % 2_000_000_000
                selected_summary = risk_adjusted_summary(
                    selected_returns,
                    bootstrap_iterations=bootstrap_iterations,
                    seed=seed_base,
                    bootstrap_block_size=bootstrap_block_size,
                )
                baseline_summary = risk_adjusted_summary(
                    baseline_returns,
                    bootstrap_iterations=bootstrap_iterations,
                    seed=seed_base + 1,
                    bootstrap_block_size=bootstrap_block_size,
                )
                spread_keys = [
                    "mean_return_pct",
                    "winsorized_mean_return_pct",
                    "median_return_pct",
                    "sharpe_like",
                    "sortino_like",
                    "profit_factor",
                    "gain_loss_ratio",
                    "p05_return_pct",
                    "p10_return_pct",
                    "large_loss_20pct_rate_pct",
                    "large_loss_40pct_rate_pct",
                    "large_gain_20pct_rate_pct",
                ]
                out.append(
                    {
                        "sample": sample,
                        "horizon_days": horizon,
                        "return_basis": "net_after_round_trip_costs",
                        "model": model_name,
                        "score_column": score_key,
                        "top_n": top_n,
                        "asof_dates": date_count,
                        "avg_names_per_date": rounded(mean([float(v) for v in selected_counts])),
                        "baseline_type": "sample_universe",
                        **prefixed("selected_", selected_summary),
                        **prefixed("baseline_", baseline_summary),
                        **{
                            f"selected_minus_baseline_{key}": summary_metric_spread(
                                selected_summary,
                                baseline_summary,
                                key,
                            )
                            for key in spread_keys
                        },
                    }
                )
    return out


def top_rows_by_score(rows: list[dict[str, Any]], score_key: str, top_n: int) -> list[dict[str, Any]]:
    candidates = [row for row in rows if to_float(row.get(score_key)) is not None]
    candidates.sort(key=lambda row: (to_float(row.get(score_key), -1e9) or -1e9, str(row.get("ticker") or "")), reverse=True)
    return candidates[:top_n]


def gate_rows_by_top_pct(rows: list[dict[str, Any]], score_key: str, top_pct: float) -> list[dict[str, Any]]:
    candidates = [row for row in rows if to_float(row.get(score_key)) is not None]
    candidates.sort(key=lambda row: (to_float(row.get(score_key), -1e9) or -1e9, str(row.get("ticker") or "")), reverse=True)
    if not candidates:
        return []
    keep_count = max(1, math.ceil(len(candidates) * max(0.0, min(1.0, top_pct))))
    return candidates[:keep_count]


def build_tier1_gate_ranked_additive_rows(
    rows: list[dict[str, Any]],
    horizons: list[int],
    top_ns: list[int],
    *,
    sample: str,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    rows_by_date: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        rows_by_date[str(row.get("asof_date") or "")].append(row)

    strategies = [
        ("tier1_topn", TIER1_GATE_SCORE_KEY, "all_eligible"),
        ("tier1_gate_rank_orthogonal_alpha", "orthogonal_alpha_score", "tier1_gate"),
        ("tier1_gate_rank_multibagger", "multibagger_score", "tier1_gate"),
        ("tier1_gate_rank_base_multibagger", "base_multibagger_score", "tier1_gate"),
    ]
    spread_keys = [
        "mean_return_pct",
        "winsorized_mean_return_pct",
        "median_return_pct",
        "sharpe_like",
        "sortino_like",
        "gain_loss_ratio",
        "p05_return_pct",
        "p10_return_pct",
        "large_loss_20pct_rate_pct",
        "large_loss_40pct_rate_pct",
        "large_gain_20pct_rate_pct",
    ]

    for horizon in horizons:
        ret_key = f"fwd_{horizon}d_net_return"
        for top_n in top_ns:
            for gate_name, gate_top_pct in TIER1_GATE_SPECS:
                collectors: dict[str, dict[str, Any]] = {
                    strategy_name: {"selected": [], "counts": [], "dates": 0}
                    for strategy_name, _, _ in strategies
                }
                gate_baseline_returns: list[float] = []
                tier1_topn_returns: list[float] = []
                gate_counts: list[int] = []
                gate_date_count = 0

                for date_rows in rows_by_date.values():
                    eligible = [
                        row
                        for row in date_rows
                        if to_float(row.get(ret_key)) is not None
                        and to_float(row.get(TIER1_GATE_SCORE_KEY)) is not None
                    ]
                    if not eligible:
                        continue
                    gated = gate_rows_by_top_pct(eligible, TIER1_GATE_SCORE_KEY, gate_top_pct)
                    gated_returns = numeric_values(gated, ret_key)
                    if gated_returns:
                        gate_baseline_returns.extend(gated_returns)
                        gate_counts.append(len(gated_returns))
                        gate_date_count += 1

                    tier1_selected = top_rows_by_score(eligible, TIER1_GATE_SCORE_KEY, top_n)
                    tier1_returns = numeric_values(tier1_selected, ret_key)
                    if tier1_returns:
                        tier1_topn_returns.extend(tier1_returns)

                    for strategy_name, score_key, scope in strategies:
                        pool = gated if scope == "tier1_gate" else eligible
                        selected = top_rows_by_score(pool, score_key, top_n)
                        selected_returns = numeric_values(selected, ret_key)
                        if not selected_returns:
                            continue
                        collectors[strategy_name]["selected"].extend(selected_returns)
                        collectors[strategy_name]["counts"].append(len(selected_returns))
                        collectors[strategy_name]["dates"] += 1

                gate_summary = risk_adjusted_summary(gate_baseline_returns)
                tier1_summary = risk_adjusted_summary(tier1_topn_returns)
                for strategy_name, score_key, scope in strategies:
                    payload = collectors[strategy_name]
                    selected_returns = payload["selected"]
                    selected_summary = risk_adjusted_summary(selected_returns)
                    out.append(
                        {
                            "sample": sample,
                            "horizon_days": horizon,
                            "return_basis": "net_after_round_trip_costs",
                            "top_n": top_n,
                            "gate_name": gate_name,
                            "gate_score_column": TIER1_GATE_SCORE_KEY,
                            "gate_top_pct": gate_top_pct,
                            "strategy": strategy_name,
                            "rank_score_column": score_key,
                            "selection_scope": scope,
                            "asof_dates": payload["dates"],
                            "avg_selected_names_per_date": rounded(mean([float(v) for v in payload["counts"]])),
                            "gate_asof_dates": gate_date_count,
                            "avg_gate_names_per_date": rounded(mean([float(v) for v in gate_counts])),
                            **prefixed("selected_", selected_summary),
                            **prefixed("gate_baseline_", gate_summary),
                            **prefixed("tier1_topn_", tier1_summary),
                            **{
                                f"selected_minus_gate_{key}": summary_metric_spread(
                                    selected_summary,
                                    gate_summary,
                                    key,
                                )
                                for key in spread_keys
                            },
                            **{
                                f"selected_minus_tier1_topn_{key}": summary_metric_spread(
                                    selected_summary,
                                    tier1_summary,
                                    key,
                                )
                                for key in spread_keys
                            },
                        }
                    )
    return out


def conflict_record(
    row: dict[str, Any],
    *,
    horizon: int,
    conflict_type: str,
    ret_key: str,
    return_pctile_key: str,
) -> dict[str, Any]:
    return {
        "asof_date": row.get("asof_date", ""),
        "horizon_days": horizon,
        "conflict_type": conflict_type,
        "ticker": row.get("ticker", ""),
        "company_name": row.get("company_name", ""),
        "biotech_bucket": row.get("biotech_bucket", ""),
        "multibagger_bucket": row.get("multibagger_bucket", ""),
        "biotech_opportunity_score": rounded(to_float(row.get("biotech_opportunity_score"))),
        "biotech_opportunity_percentile": rounded(to_float(row.get("diag_biotech_opportunity_score_percentile"))),
        "biotech_risk_score": rounded(to_float(row.get("biotech_risk_score"))),
        "tier1_gate_score": rounded(to_float(row.get("tier1_selection_gate_score"))),
        "multibagger_score": rounded(to_float(row.get("multibagger_score"))),
        "multibagger_percentile": rounded(to_float(row.get("diag_multibagger_score_percentile"))),
        "base_multibagger_score": rounded(to_float(row.get("base_multibagger_score"))),
        "orthogonal_alpha_score": rounded(to_float(row.get("orthogonal_alpha_score"))),
        "orthogonal_alpha_percentile": rounded(to_float(row.get("diag_orthogonal_alpha_score_percentile"))),
        "forward_return_pct": pct(to_float(row.get(ret_key))),
        "forward_return_percentile": rounded(to_float(row.get(return_pctile_key))),
        "avg_dollar_volume_20d": rounded(to_float(row.get("diag_avg_dollar_volume_20d"))),
        "liquidity_ok": rounded(to_float(row.get("diag_liquidity_ok"))),
        "multibagger_risk_penalty": rounded(to_float(row.get("diag_multibagger_risk_penalty"))),
        "commercial_fragility_risk_score": rounded(to_float(row.get("diag_commercial_fragility_risk_score"))),
    }


def build_score_conflict_diagnostic_rows(rows: list[dict[str, Any]], horizons: list[int]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for horizon in horizons:
        ret_key = f"fwd_{horizon}d_return"
        return_pctile_key = f"diag_fwd_{horizon}d_return_percentile"
        for row in rows:
            tier1_pct = to_float(row.get("diag_biotech_opportunity_score_percentile"))
            multibagger_pct = to_float(row.get("diag_multibagger_score_percentile"))
            orthogonal_pct = to_float(row.get("diag_orthogonal_alpha_score_percentile"))
            return_pct = to_float(row.get(return_pctile_key))
            if tier1_pct is None or return_pct is None:
                continue
            conflict_types: list[str] = []
            if tier1_pct >= CONFLICT_HIGH_PERCENTILE:
                if multibagger_pct is not None and multibagger_pct <= CONFLICT_LOW_PERCENTILE:
                    conflict_types.append("high_tier1_low_multibagger_rank")
                if orthogonal_pct is not None and orthogonal_pct <= CONFLICT_LOW_PERCENTILE:
                    conflict_types.append("high_tier1_low_orthogonal_alpha_rank")
                if return_pct <= 10.0:
                    conflict_types.append("high_tier1_bottom_return_decile")
            if tier1_pct <= CONFLICT_LOW_PERCENTILE:
                if multibagger_pct is not None and multibagger_pct >= CONFLICT_HIGH_PERCENTILE:
                    conflict_types.append("low_tier1_high_multibagger_rank")
                if orthogonal_pct is not None and orthogonal_pct >= CONFLICT_HIGH_PERCENTILE:
                    conflict_types.append("low_tier1_high_orthogonal_alpha_rank")
                if return_pct >= 90.0:
                    conflict_types.append("low_tier1_top_return_decile_rebound")
            for conflict_type in conflict_types:
                out.append(
                    conflict_record(
                        row,
                        horizon=horizon,
                        conflict_type=conflict_type,
                        ret_key=ret_key,
                        return_pctile_key=return_pctile_key,
                    )
                )
    return out


def with_sample(rows: list[dict[str, Any]], sample: str) -> list[dict[str, Any]]:
    return [{"sample": sample, **row} for row in rows]


def liquidity_ok_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [row for row in rows if to_float(row.get("diag_liquidity_ok")) == 1.0]


def build_bucket_ticker_driver_rows(
    rows: list[dict[str, Any]],
    horizons: list[int],
    *,
    driver_limit: int = 10,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    bucket_specs = [
        ("biotech_bucket", "biotech_bucket"),
        ("multibagger_bucket", "multibagger_bucket"),
    ]
    for horizon in horizons:
        ret_key = f"fwd_{horizon}d_return"
        ret_pctile_key = f"diag_fwd_{horizon}d_return_percentile"
        for bucket_type, bucket_key in bucket_specs:
            groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
            for row in rows:
                if to_float(row.get(ret_key)) is None:
                    continue
                bucket = str(row.get(bucket_key) or "").strip() or "blank"
                ticker = normalize_ticker(row.get("ticker"))
                if ticker:
                    groups[(bucket, ticker)].append(row)
            by_bucket: dict[str, list[tuple[str, list[dict[str, Any]], float]]] = defaultdict(list)
            for (bucket, ticker), ticker_rows in groups.items():
                ticker_mean = mean(numeric_values(ticker_rows, ret_key))
                if ticker_mean is None:
                    continue
                by_bucket[bucket].append((ticker, ticker_rows, ticker_mean))
            for bucket, ticker_groups in sorted(by_bucket.items()):
                ranked_best = sorted(ticker_groups, key=lambda item: (item[2], item[0]), reverse=True)[:driver_limit]
                ranked_worst = sorted(ticker_groups, key=lambda item: (item[2], item[0]))[:driver_limit]
                for side, ranked in [("best", ranked_best), ("worst", ranked_worst)]:
                    for rank, (ticker, ticker_rows, _) in enumerate(ranked, start=1):
                        returns = numeric_values(ticker_rows, ret_key)
                        first_row = ticker_rows[0]
                        out.append(
                            {
                                "horizon_days": horizon,
                                "bucket_type": bucket_type,
                                "bucket": bucket,
                                "bucket_family": bucket_family(bucket_type, bucket),
                                "side": side,
                                "rank": rank,
                                "ticker": ticker,
                                "company_name": str(first_row.get("company_name") or ""),
                                **summarize_returns(returns),
                                **summarize_return_risk(returns),
                                "top_return_decile_hit_pct": pct_where(ticker_rows, ret_pctile_key, 90.0, op=">="),
                                "bottom_return_decile_hit_pct": pct_where(ticker_rows, ret_pctile_key, 10.0, op="<="),
                                "mean_biotech_opportunity_percentile": mean_numeric(
                                    ticker_rows, "diag_biotech_opportunity_score_percentile"
                                ),
                                "mean_multibagger_percentile": mean_numeric(
                                    ticker_rows, "diag_multibagger_score_percentile"
                                ),
                                "mean_orthogonal_alpha_percentile": mean_numeric(
                                    ticker_rows, "diag_orthogonal_alpha_score_percentile"
                                ),
                                "mean_biotech_risk_score": mean_numeric(ticker_rows, "biotech_risk_score"),
                                "mean_multibagger_risk_penalty": mean_numeric(
                                    ticker_rows, "diag_multibagger_risk_penalty"
                                ),
                                "median_avg_dollar_volume_20d": median_numeric(
                                    ticker_rows, "diag_avg_dollar_volume_20d"
                                ),
                                "liquidity_ok_pct": pct_where(ticker_rows, "diag_liquidity_ok", 1.0, op=">="),
                            }
                        )
    return out


def build_monotonicity_rows(
    rows: list[dict[str, Any]],
    horizons: list[int],
    *,
    sample: str,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    rows_by_date: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        rows_by_date[str(row.get("asof_date") or "")].append(row)
    for horizon in horizons:
        ret_key = f"fwd_{horizon}d_return"
        for model_name, score_key in SCORE_MODELS.items():
            decile_returns: dict[int, list[float]] = defaultdict(list)
            date_count = 0
            for date_rows in rows_by_date.values():
                scored = [
                    (to_float(row.get(score_key)), to_float(row.get(ret_key)))
                    for row in date_rows
                    if to_float(row.get(score_key)) is not None and to_float(row.get(ret_key)) is not None
                ]
                scored = [(score, ret) for score, ret in scored if score is not None and ret is not None]
                scored.sort(key=lambda item: item[0])
                n = len(scored)
                if n < 10:
                    continue
                date_count += 1
                for idx, (_, ret) in enumerate(scored):
                    decile = min(10, int(idx * 10 / n) + 1)
                    decile_returns[decile].append(ret)
            decile_means = {decile: mean(decile_returns.get(decile, [])) for decile in range(1, 11)}
            low_mean = decile_means.get(1)
            high_mean = decile_means.get(10)
            high_minus_low = high_mean - low_mean if high_mean is not None and low_mean is not None else None
            mean_pairs = [
                (float(decile), value)
                for decile, value in decile_means.items()
                if value is not None
            ]
            adjacent_inversions = 0
            adjacent_pairs = 0
            for decile in range(1, 10):
                left = decile_means.get(decile)
                right = decile_means.get(decile + 1)
                if left is None or right is None:
                    continue
                adjacent_pairs += 1
                if right < left:
                    adjacent_inversions += 1
            direction = "insufficient"
            if high_minus_low is not None:
                if high_minus_low > 0:
                    direction = "normal"
                elif high_minus_low < 0:
                    direction = "inverted"
                else:
                    direction = "flat"
            decile_spearman = spearman_from_pairs(mean_pairs)
            low_returns = decile_returns.get(1, [])
            high_returns = decile_returns.get(10, [])
            low_summary = risk_adjusted_summary(low_returns)
            high_summary = risk_adjusted_summary(high_returns)
            low_median = median(low_returns) if low_returns else None
            high_median = median(high_returns) if high_returns else None
            low_p10 = quantile(low_returns, 0.10)
            high_p10 = quantile(high_returns, 0.10)
            low_sharpe = to_float(low_summary.get("sharpe_like"))
            high_sharpe = to_float(high_summary.get("sharpe_like"))
            low_sortino = to_float(low_summary.get("sortino_like"))
            high_sortino = to_float(high_summary.get("sortino_like"))
            low_loss_20 = to_float(low_summary.get("large_loss_20pct_rate_pct"))
            high_loss_20 = to_float(high_summary.get("large_loss_20pct_rate_pct"))
            low_loss_40 = to_float(low_summary.get("large_loss_40pct_rate_pct"))
            high_loss_40 = to_float(high_summary.get("large_loss_40pct_rate_pct"))
            out.append(
                {
                    "sample": sample,
                    "horizon_days": horizon,
                    "model": model_name,
                    "score_column": score_key,
                    "asof_dates": date_count,
                    "score_decile_order": "1_lowest_score_to_10_highest_score",
                    "low_score_decile_n": len(decile_returns.get(1, [])),
                    "high_score_decile_n": len(decile_returns.get(10, [])),
                    "low_score_decile_mean_return_pct": pct(low_mean),
                    "high_score_decile_mean_return_pct": pct(high_mean),
                    "high_minus_low_mean_return_pct": pct(high_minus_low),
                    "low_score_decile_median_return_pct": pct(low_median),
                    "high_score_decile_median_return_pct": pct(high_median),
                    "high_minus_low_median_return_pct": pct(
                        high_median - low_median
                        if high_median is not None and low_median is not None
                        else None
                    ),
                    "low_score_decile_p10_return_pct": pct(low_p10),
                    "high_score_decile_p10_return_pct": pct(high_p10),
                    "high_minus_low_p10_return_pct": pct(
                        high_p10 - low_p10
                        if high_p10 is not None and low_p10 is not None
                        else None
                    ),
                    "low_score_decile_sharpe_like": low_summary.get("sharpe_like", ""),
                    "high_score_decile_sharpe_like": high_summary.get("sharpe_like", ""),
                    "high_minus_low_sharpe_like": rounded(
                        high_sharpe - low_sharpe
                        if high_sharpe is not None and low_sharpe is not None
                        else None
                    ),
                    "low_score_decile_sortino_like": low_summary.get("sortino_like", ""),
                    "high_score_decile_sortino_like": high_summary.get("sortino_like", ""),
                    "high_minus_low_sortino_like": rounded(
                        high_sortino - low_sortino
                        if high_sortino is not None and low_sortino is not None
                        else None
                    ),
                    "low_score_decile_large_loss_20pct_rate_pct": low_summary.get("large_loss_20pct_rate_pct", ""),
                    "high_score_decile_large_loss_20pct_rate_pct": high_summary.get("large_loss_20pct_rate_pct", ""),
                    "high_minus_low_large_loss_20pct_rate_pct": rounded(
                        high_loss_20 - low_loss_20
                        if high_loss_20 is not None and low_loss_20 is not None
                        else None
                    ),
                    "low_score_decile_large_loss_40pct_rate_pct": low_summary.get("large_loss_40pct_rate_pct", ""),
                    "high_score_decile_large_loss_40pct_rate_pct": high_summary.get("large_loss_40pct_rate_pct", ""),
                    "high_minus_low_large_loss_40pct_rate_pct": rounded(
                        high_loss_40 - low_loss_40
                        if high_loss_40 is not None and low_loss_40 is not None
                        else None
                    ),
                    "decile_mean_spearman": rounded(decile_spearman),
                    "adjacent_inversion_count": adjacent_inversions,
                    "adjacent_pair_count": adjacent_pairs,
                    "monotonicity_direction": direction,
                    "monotonicity_pass": bool(
                        high_minus_low is not None
                        and high_minus_low > 0
                        and (decile_spearman or 0.0) > 0
                    ),
                    **{
                        f"decile_{decile}_mean_return_pct": pct(decile_means.get(decile))
                        for decile in range(1, 11)
                    },
                }
            )
    return out


def build_orthogonality_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    comparisons = [
        ("biotech_vs_multibagger", "biotech_opportunity_score", "multibagger_score"),
        ("biotech_vs_base_multibagger", "biotech_opportunity_score", "base_multibagger_score"),
        ("biotech_vs_orthogonal_alpha", "biotech_opportunity_score", "orthogonal_alpha_score"),
        ("biotech_risk_vs_multibagger", "biotech_risk_score", "multibagger_score"),
        ("biotech_gate_vs_multibagger", "tier1_selection_gate_score", "multibagger_score"),
    ]
    rows_by_date: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        rows_by_date[str(row.get("asof_date") or "")].append(row)
    for label, left, right in comparisons:
        all_pairs = numeric_pairs(rows, left, right)
        out.append(
            {
                "scope": "overall",
                "asof_date": "",
                "comparison": label,
                "left_score": left,
                "right_score": right,
                "n": len(all_pairs),
                "pearson": rounded(pearson_from_pairs(all_pairs)),
                "spearman": rounded(spearman_from_pairs(all_pairs)),
            }
        )
        for asof_date, date_rows in sorted(rows_by_date.items()):
            pairs = numeric_pairs(date_rows, left, right)
            out.append(
                {
                    "scope": "asof_date",
                    "asof_date": asof_date,
                    "comparison": label,
                    "left_score": left,
                    "right_score": right,
                    "n": len(pairs),
                    "pearson": rounded(pearson_from_pairs(pairs)),
                    "spearman": rounded(spearman_from_pairs(pairs)),
                }
            )
    return out


def write_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str] | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if fieldnames is None:
        keys: list[str] = []
        seen: set[str] = set()
        for row in rows:
            for key in row:
                if key not in seen:
                    seen.add(key)
                    keys.append(key)
        fieldnames = keys
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, lineterminator="\n", extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True), encoding="utf-8")


def chronological_train_test_rows(
    rows: list[dict[str, Any]],
    snapshot_dates: list[str],
    *,
    train_fraction: float,
    embargo_days: int = 0,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str], list[str]]:
    if len(snapshot_dates) < 2:
        return rows, [], list(snapshot_dates), []
    split_idx = int(math.floor(len(snapshot_dates) * train_fraction))
    split_idx = max(1, min(len(snapshot_dates) - 1, split_idx))
    train_dates = snapshot_dates[:split_idx]
    test_dates = snapshot_dates[split_idx:]
    if embargo_days > 0 and train_dates and test_dates:
        boundary = parse_date(test_dates[0])
        if boundary is not None:
            filtered_train_dates = []
            for item in train_dates:
                parsed = parse_date(item)
                if parsed is not None and (boundary - parsed).days > embargo_days:
                    filtered_train_dates.append(item)
            if filtered_train_dates:
                LOGGER.info(
                    "Applied Phase 1 purged-train embargo: days=%d train_dates=%d->%d test_dates=%d",
                    embargo_days,
                    len(train_dates),
                    len(filtered_train_dates),
                    len(test_dates),
                )
                train_dates = filtered_train_dates
            else:
                LOGGER.warning(
                    "Skipping Phase 1 embargo because it would empty the training split: days=%d train_dates=%d->%d test_dates=%d",
                    embargo_days,
                    len(train_dates),
                    len(filtered_train_dates),
                    len(test_dates),
                )
    train_set = set(train_dates)
    test_set = set(test_dates)
    return (
        [row for row in rows if str(row.get("asof_date") or "") in train_set],
        [row for row in rows if str(row.get("asof_date") or "") in test_set],
        train_dates,
        test_dates,
    )


def build_return_data_completeness_rows(
    rows: list[dict[str, Any]],
    horizons: list[int],
    *,
    sample: str,
) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    row_count = len(rows)
    for horizon in horizons:
        gross_key = f"fwd_{horizon}d_return"
        net_key = f"fwd_{horizon}d_net_return"
        gross_count = sum(1 for row in rows if to_float(row.get(gross_key)) is not None)
        net_count = sum(1 for row in rows if to_float(row.get(net_key)) is not None)
        output.append(
            {
                "sample": sample,
                "horizon_days": horizon,
                "score_row_count": row_count,
                "gross_return_observation_count": gross_count,
                "net_return_observation_count": net_count,
                "missing_net_return_count": max(0, row_count - net_count),
                "net_return_completeness_pct": pct(net_count / row_count if row_count else None),
            }
        )
    return output


def write_phase1_sample_outputs(
    output_dir: Path,
    *,
    sample_name: str,
    rows: list[dict[str, Any]],
    horizons: list[int],
    top_ns: list[int],
    observation_fields: list[str],
    bootstrap_iterations: int,
) -> None:
    liquid_rows = liquidity_ok_rows(rows)
    write_csv(output_dir / f"phase1_{sample_name}_observations.csv", rows, observation_fields)
    write_csv(output_dir / f"phase1_{sample_name}_score_correlations.csv", build_correlation_rows(rows, horizons))
    write_csv(output_dir / f"phase1_{sample_name}_deciles.csv", build_decile_rows(rows, horizons))
    write_csv(output_dir / f"phase1_{sample_name}_buckets.csv", build_bucket_rows(rows, horizons))
    write_csv(
        output_dir / f"phase1_{sample_name}_return_data_completeness.csv",
        build_return_data_completeness_rows(rows, horizons, sample=sample_name),
    )
    risk_rows = build_topn_risk_adjusted_rows(
        rows,
        horizons,
        top_ns,
        sample=sample_name,
        bootstrap_iterations=bootstrap_iterations,
    )
    risk_rows.extend(
        build_topn_risk_adjusted_rows(
            liquid_rows,
            horizons,
            top_ns,
            sample=f"{sample_name}_liquidity_ok",
            bootstrap_iterations=bootstrap_iterations,
        )
    )
    write_csv(output_dir / f"phase1_{sample_name}_topn_risk_adjusted.csv", risk_rows)
    write_csv(output_dir / f"phase1_{sample_name}_topn.csv", build_topn_rows(rows, horizons, top_ns))


def main() -> None:
    global CONFLICT_HIGH_PERCENTILE, CONFLICT_LOW_PERCENTILE
    configure_logging()
    args = parse_args()
    start_time = time.perf_counter()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    CONFLICT_HIGH_PERCENTILE = float(cfg_get(config, "calibration.phase1.conflict_high_pct", CONFLICT_HIGH_PERCENTILE))
    CONFLICT_LOW_PERCENTILE = float(cfg_get(config, "calibration.phase1.conflict_low_pct", CONFLICT_LOW_PERCENTILE))
    base_dir = config_path.parent
    db_path = args.db.expanduser().resolve() if args.db else resolve_path(cfg_get(config, "paths.database_path"), base_dir=base_dir)
    output_dir = (
        args.output_dir.expanduser().resolve()
        if args.output_dir
        else resolve_path(
            cfg_get(
                config,
                "calibration.phase1.output_dir",
                "../output/biotech_index_reports/calibration_phase1",
            ),
            base_dir=base_dir,
        )
    )
    start_asof = parse_date(args.start_asof)
    end_asof = parse_date(args.end_asof)
    if args.start_asof and start_asof is None:
        raise ValueError(f"Invalid --start-asof date: {args.start_asof}")
    if args.end_asof and end_asof is None:
        raise ValueError(f"Invalid --end-asof date: {args.end_asof}")
    default_horizons_raw = cfg_get(
        config,
        "calibration.phase1.horizons",
        cfg_get(config, "calibration.tier1.medium_term_horizons", [60, 120]),
    )
    default_horizons = parse_int_list(
        ",".join(str(value) for value in normalize_string_list(default_horizons_raw, ["60", "120"])),
        default=[60, 120],
    )
    horizons = parse_int_list(args.horizons, default=default_horizons)
    top_ns = parse_int_list(args.top_n, default=[10, 20, 30])
    market_sources_raw = args.market_sources if str(args.market_sources or "").strip() else None
    market_sources = [
        token.strip()
        for raw_source in normalize_string_list(market_sources_raw, calibration_market_sources(config))
        for token in str(raw_source).split(",")
        if token.strip()
    ]
    phase1_costs = cfg_get(config, "calibration.phase1.costs", {}) or {}
    round_trip_cost_bps = (
        float(args.round_trip_cost_bps)
        if args.round_trip_cost_bps is not None
        else float(phase1_costs.get("long_round_trip_bps", DEFAULT_ROUND_TRIP_COST_BPS))
    )
    train_fraction = (
        float(args.train_fraction)
        if args.train_fraction is not None
        else float(cfg_get(config, "calibration.phase1.train_fraction", 0.70))
    )
    train_fraction = max(0.10, min(0.90, train_fraction))
    # When the config key is absent, derive a calendar-day embargo from the
    # longest horizon (trading bars → calendar days, same formula as script 28).
    # Using max(horizons) directly as calendar days would undercount by ~54 days
    # for a 120-bar horizon (120 bars ≈ 174 calendar days, not 120).
    _default_embargo = math.ceil(max(horizons) * 365.25 / 252.0) + 10
    embargo_days = (
        int(args.embargo_days)
        if args.embargo_days is not None
        else int(cfg_get(config, "calibration.phase1.embargo_days", _default_embargo))
    )
    embargo_days = max(0, embargo_days)
    bootstrap_iterations = (
        int(args.bootstrap_iterations)
        if args.bootstrap_iterations is not None
        else int(cfg_get(config, "calibration.phase1.bootstrap_iterations", 200))
    )
    bootstrap_iterations = max(0, bootstrap_iterations)
    next_bar_entry = (
        args.next_bar_entry
        if args.next_bar_entry is not None
        else as_bool(cfg_get(config, "calibration.phase1.next_bar_entry", True), True)
    )
    exclude_current_removals = (
        args.exclude_current_removals
        if args.exclude_current_removals is not None
        else as_bool(cfg_get(config, "calibration.phase1.exclude_current_removals", False), False)
    )
    extra_exclusions = parse_string_set(args.exclude_tickers) | parse_string_set(
        cfg_get(config, "calibration.exclude_tickers", [])
    )

    with connect(db_path, timeout_sec=float(cfg_get(config, "runtime.sqlite_timeout_sec", 30.0))) as conn:
        snapshot_dates = load_snapshot_dates(
            conn,
            start_asof=start_asof,
            end_asof=end_asof,
            fridays_only=not args.include_non_fridays,
            max_snapshots=max(0, int(args.max_snapshots)),
        )
        if not snapshot_dates:
            raise ValueError("No paired daily/multibagger score snapshot dates found for calibration.")
        excluded_tickers = load_excluded_tickers(
            conn,
            exclude_current_removals=exclude_current_removals,
            extra=extra_exclusions,
        )
        score_rows = load_score_rows(conn, snapshot_dates, excluded_tickers)
        if not score_rows:
            raise ValueError("No score rows remain after exclusions.")
        tickers = {ticker for row in score_rows if (ticker := normalize_ticker(row["ticker"]))}
        asof_dates = [parsed for row in score_rows if (parsed := parse_date(row["asof_date"])) is not None]
        if not asof_dates:
            raise ValueError("Score rows do not contain valid as-of dates.")
        min_asof = min(asof_dates)
        bars_by_ticker = load_bars(conn, tickers=tickers, min_date=min_asof, market_sources=market_sources)

    LOGGER.info(
        "Loaded calibration inputs: dates=%d rows=%d tickers=%d bars_tickers=%d excluded_tickers=%d",
        len(snapshot_dates),
        len(score_rows),
        len(tickers),
        len(bars_by_ticker),
        len(excluded_tickers),
    )

    add_forward_returns(
        score_rows,
        bars_by_ticker,
        horizons,
        round_trip_cost_bps=round_trip_cost_bps,
        next_bar_entry=next_bar_entry,
    )
    add_bucket_diagnostic_fields(
        score_rows,
        horizons,
        min_addv20=float(cfg_get(config, "multibagger.min_addv20", 1_000_000.0)),
    )
    observation_fields = [
        "asof_date", "company_id", "ticker", "company_name",
        "biotech_rank", "biotech_bucket", "biotech_opportunity_score", "biotech_investment_score",
        "biotech_clinical_opportunity_score", "biotech_risk_score", "tier1_selection_gate_score",
        "multibagger_rank", "multibagger_bucket", "multibagger_score", "base_multibagger_score",
        "orthogonal_alpha_score", "distinctive_acceleration_score", "multibagger_tier1_gate_score",
        "multibagger_tier1_gate_multiplier", "multibagger_tier1_available",
    ]
    for horizon in horizons:
        observation_fields.extend(
            [
                f"fwd_{horizon}d_return",
                f"fwd_{horizon}d_net_return",
                f"fwd_{horizon}d_round_trip_cost_bps",
                f"fwd_{horizon}d_entry_date",
                f"fwd_{horizon}d_target_date",
            ]
        )
    observation_fields.extend(
        [
            "diag_biotech_opportunity_score_percentile",
            "diag_tier1_selection_gate_score_percentile",
            "diag_multibagger_score_percentile",
            "diag_base_multibagger_score_percentile",
            "diag_orthogonal_alpha_score_percentile",
            "diag_distinctive_acceleration_score_percentile",
            "diag_multibagger_risk_penalty",
            "diag_multibagger_risk_penalty_percentile",
            "diag_commercial_fragility_risk_score",
            "diag_commercial_fragility_risk_score_percentile",
            "diag_avg_dollar_volume_20d",
            "diag_liquidity_score",
            "diag_liquidity_ok",
            "diag_market_cap",
            "diag_cash_runway_months",
            "diag_reverse_split_hits_2y",
        ]
    )

    correlation_rows = build_correlation_rows(score_rows, horizons)
    decile_rows = build_decile_rows(score_rows, horizons)
    bucket_rows = build_bucket_rows(score_rows, horizons)
    bucket_diagnostic_rows = build_bucket_diagnostic_rows(score_rows, horizons)
    liquid_score_rows = liquidity_ok_rows(score_rows)
    liquidity_filtered_bucket_rows = build_bucket_diagnostic_rows(liquid_score_rows, horizons, sample="liquidity_ok")
    liquidity_filtered_topn_rows = with_sample(
        build_topn_rows(liquid_score_rows, horizons, top_ns),
        "liquidity_ok",
    )
    topn_risk_adjusted_rows = build_topn_risk_adjusted_rows(
        score_rows,
        horizons,
        top_ns,
        sample="all",
        bootstrap_iterations=bootstrap_iterations,
    )
    topn_risk_adjusted_rows.extend(
        build_topn_risk_adjusted_rows(
            liquid_score_rows,
            horizons,
            top_ns,
            sample="liquidity_ok",
            bootstrap_iterations=bootstrap_iterations,
        )
    )
    tier1_gate_ranked_additive_rows = build_tier1_gate_ranked_additive_rows(
        score_rows,
        horizons,
        top_ns,
        sample="all",
    )
    tier1_gate_ranked_additive_rows.extend(
        build_tier1_gate_ranked_additive_rows(
            liquid_score_rows,
            horizons,
            top_ns,
            sample="liquidity_ok",
        )
    )
    score_conflict_rows = build_score_conflict_diagnostic_rows(score_rows, horizons)
    bucket_ticker_driver_rows = build_bucket_ticker_driver_rows(score_rows, horizons)
    monotonicity_rows = build_monotonicity_rows(score_rows, horizons, sample="all")
    monotonicity_rows.extend(build_monotonicity_rows(liquid_score_rows, horizons, sample="liquidity_ok"))
    topn_rows = build_topn_rows(score_rows, horizons, top_ns)
    orthogonality_rows = build_orthogonality_rows(score_rows)
    train_rows, test_rows, train_dates, test_dates = chronological_train_test_rows(
        score_rows,
        snapshot_dates,
        train_fraction=train_fraction,
        embargo_days=embargo_days,
    )

    write_csv(output_dir / "phase1_observations.csv", score_rows, observation_fields)
    write_csv(output_dir / "phase1_score_correlations.csv", correlation_rows)
    write_csv(output_dir / "phase1_deciles.csv", decile_rows)
    write_csv(output_dir / "phase1_buckets.csv", bucket_rows)
    write_csv(output_dir / "phase1_bucket_diagnostics.csv", bucket_diagnostic_rows)
    write_csv(output_dir / "phase1_liquidity_filtered_buckets.csv", liquidity_filtered_bucket_rows)
    write_csv(output_dir / "phase1_liquidity_filtered_topn.csv", liquidity_filtered_topn_rows)
    write_csv(output_dir / "phase1_topn_risk_adjusted.csv", topn_risk_adjusted_rows)
    return_data_completeness_rows = build_return_data_completeness_rows(score_rows, horizons, sample="all")
    write_csv(output_dir / "phase1_return_data_completeness.csv", return_data_completeness_rows)
    write_csv(output_dir / "phase1_tier1_gate_ranked_additive.csv", tier1_gate_ranked_additive_rows)
    write_csv(output_dir / "phase1_score_conflict_diagnostics.csv", score_conflict_rows)
    write_csv(output_dir / "phase1_bucket_ticker_drivers.csv", bucket_ticker_driver_rows)
    write_csv(output_dir / "phase1_monotonicity.csv", monotonicity_rows)
    write_csv(output_dir / "phase1_topn.csv", topn_rows)
    write_csv(output_dir / "phase1_orthogonality.csv", orthogonality_rows)
    write_phase1_sample_outputs(
        output_dir,
        sample_name="train",
        rows=train_rows,
        horizons=horizons,
        top_ns=top_ns,
        observation_fields=observation_fields,
        bootstrap_iterations=bootstrap_iterations,
    )
    write_phase1_sample_outputs(
        output_dir,
        sample_name="test",
        rows=test_rows,
        horizons=horizons,
        top_ns=top_ns,
        observation_fields=observation_fields,
        bootstrap_iterations=bootstrap_iterations,
    )

    horizon_counts = {
        str(horizon): sum(1 for row in score_rows if to_float(row.get(f"fwd_{horizon}d_return")) is not None)
        for horizon in horizons
    }
    net_horizon_counts = {
        str(horizon): sum(1 for row in score_rows if to_float(row.get(f"fwd_{horizon}d_net_return")) is not None)
        for horizon in horizons
    }
    manifest = {
        "script": Path(__file__).name,
        "db_path": str(db_path),
        "output_dir": str(output_dir),
        "snapshot_dates": snapshot_dates,
        "snapshot_date_count": len(snapshot_dates),
        "train_fraction": train_fraction,
        "embargo_days": embargo_days,
        "train_snapshot_dates": train_dates,
        "test_snapshot_dates": test_dates,
        "train_score_row_count": len(train_rows),
        "test_score_row_count": len(test_rows),
        "score_row_count_after_exclusions": len(score_rows),
        "liquidity_ok_score_row_count": len(liquid_score_rows),
        "ticker_count_after_exclusions": len(tickers),
        "excluded_ticker_count": len(excluded_tickers),
        "excluded_tickers_sample": sorted(excluded_tickers)[:100],
        "market_sources": market_sources,
        "horizons": horizons,
        "top_n": top_ns,
        "forward_return_observation_counts": horizon_counts,
        "net_forward_return_observation_counts": net_horizon_counts,
        "return_data_completeness": {
            str(row["horizon_days"]): row for row in return_data_completeness_rows
        },
        "round_trip_cost_bps": round_trip_cost_bps,
        "bootstrap_iterations": bootstrap_iterations,
        "next_bar_entry": next_bar_entry,
        "exclude_current_removals": exclude_current_removals,
        "elapsed_sec": round(time.perf_counter() - start_time, 3),
        "notes": [
            "Phase 1 is diagnostic only and does not change config.yaml.",
            "Forward returns use the configured market source priority, trading-day horizons, and next-bar entry by default.",
            "Top-N and Tier-1 additive diagnostics use net forward returns after configured round-trip costs.",
            "Train/test sample outputs apply an embargo around the split boundary by default to reduce overlap leakage from forward-return horizons.",
            "The horizon_days column name is retained for compatibility but represents trading bars, not calendar days.",
            "Current removals/manual exclusions are not excluded by default to reduce survivorship bias; set --exclude-current-removals to match current investable-universe diagnostics.",
            "phase1_bucket_diagnostics.csv separates bucket return behavior from risk, liquidity, and score-rank behavior.",
            "phase1_bucket_ticker_drivers.csv shows the best and worst tickers driving each bucket.",
            "phase1_liquidity_filtered_*.csv retests buckets and Top-N selections after the liquidity gate.",
            "phase1_return_data_completeness.csv reports per-horizon completed-return coverage to surface bar-data gaps.",
            "phase1_monotonicity.csv defines decile 1 as lowest score and decile 10 as highest score.",
            "Sharpe-like and Sortino-like ratios use zero risk-free rate on overlapping forward-return cohorts; they are relative diagnostics, not formal portfolio Sharpe/Sortino ratios.",
            "phase1_tier1_gate_ranked_additive.csv treats orthogonal alpha and multibagger as second-stage rankers inside Tier 1 gates.",
            "phase1_score_conflict_diagnostics.csv flags high-quality names downgraded by multibagger layers and low-quality rebound candidates.",
        ],
    }
    write_json(output_dir / "phase1_manifest.json", manifest)
    LOGGER.info(
        "Calibration Phase 1 diagnostics written: output_dir=%s rows=%d horizon_counts=%s elapsed=%.3fs",
        output_dir,
        len(score_rows),
        horizon_counts,
        time.perf_counter() - start_time,
    )


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
    except BaseException as exc:
        if isinstance(exc, SystemExit) and exc.code in (0, None):
            raise
        LOGGER.exception("Unhandled exception in main()")
        sys.exit(1)
