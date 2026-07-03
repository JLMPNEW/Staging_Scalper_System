#!/usr/bin/env python3
from __future__ import annotations

import argparse
import bisect
import csv
import importlib.util
import logging
import math
import sqlite3
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from biotech_index.core.config import cfg_get, load_yaml, resolve_path  # noqa: E402
from biotech_index.core.db import connect  # noqa: E402
from biotech_index.core.logging_utils import configure_utc_logging  # noqa: E402
from biotech_index.core.market_policy import calibration_market_sources  # noqa: E402
from biotech_index.core.text_norm import normalize_ticker  # noqa: E402
from market_positioning.core import borrow_cost_pressure_score, connect as connect_positioning  # noqa: E402


LOGGER = logging.getLogger("validate_biotech_borrow_availability")
DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
SQLITE_PARAM_CHUNK_SIZE = 800
METRIC_KEYS = [
    "n",
    "mean_return_pct",
    "median_return_pct",
    "hit_rate_pct",
    "loss_rate_pct",
    "winsorized_mean_return_pct",
    "stdev_return_pct",
    "downside_deviation_pct",
    "lcb_return_pct",
    "cvar_5_return_pct",
    "sharpe_like",
    "sortino_like",
    "profit_factor",
    "profit_factor_configured",
    "omega_configured",
    "omega_0",
    "top3_gain_contribution_pct",
    "worst_return_pct",
    "best_return_pct",
    "p05_return_pct",
    "p10_return_pct",
    "large_loss_20pct_rate_pct",
    "large_loss_40pct_rate_pct",
    "large_gain_20pct_rate_pct",
]
BORROW_FLAG_KEYS = [
    "borrow_data_available_flag",
    "borrow_snapshot_available_flag",
    "high_borrow_pressure_flag",
    "elevated_borrow_pressure_flag",
    "borrow_rate_high_flag",
    "borrow_rate_spike_flag",
    "borrow_rate_declining_flag",
    "hard_to_borrow_flag",
    "borrow_squeeze_setup_flag",
    "borrow_distress_flag",
]


@dataclass(frozen=True)
class BorrowHistory:
    days: list[date]
    rates: list[float]


@dataclass(frozen=True)
class ShortableSnapshot:
    day: date
    shares: float


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Shadow validation for IBKR borrow availability. The script joins historical "
            "biotech feature observations to point-in-time IBKR borrow fee rates and writes "
            "QA, cohort, holdout, and interaction diagnostics. It does not mutate production scoring."
        )
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None, help="Biotech SQLite DB path.")
    parser.add_argument("--market-positioning-db", type=Path, default=None, help="Shared market-positioning SQLite DB path.")
    parser.add_argument("--output-dir", type=Path, default=None)
    parser.add_argument("--start-asof", type=str, default="")
    parser.add_argument("--end-asof", "--asof", dest="end_asof", type=str, default="")
    parser.add_argument("--horizons", type=str, default="")
    parser.add_argument("--market-sources", type=str, default="")
    parser.add_argument("--max-snapshots", type=int, default=0)
    parser.add_argument("--include-non-fridays", action="store_true")
    parser.add_argument("--strict-feature-lag", action=argparse.BooleanOptionalAction, default=None)
    parser.add_argument("--next-bar-entry", action=argparse.BooleanOptionalAction, default=None)
    parser.add_argument("--train-fraction", type=float, default=None)
    parser.add_argument("--embargo-days", type=int, default=None)
    return parser.parse_args()


def load_calibration_module() -> Any:
    path = PACKAGE_ROOT / "scripts" / "28_calibrate_biotech_opportunity.py"
    spec = importlib.util.spec_from_file_location("biotech_borrow_calibration_base", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to import calibration module: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def parse_date(raw: object) -> date | None:
    if raw is None:
        return None
    if isinstance(raw, date):
        return raw
    text = str(raw).strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def as_bool(raw: object, default: bool = False) -> bool:
    if raw is None:
        return default
    text = str(raw).strip().lower()
    if text in {"1", "true", "t", "yes", "y", "enabled", "on"}:
        return True
    if text in {"0", "false", "f", "no", "n", "disabled", "off"}:
        return False
    return default


def to_float(raw: object, default: float | None = None) -> float | None:
    try:
        value = float(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    if not math.isfinite(value):
        return default
    return value


def config_float(raw: object, default: float) -> float:
    value = to_float(raw, default)
    return default if value is None else value


def config_int(raw: object, default: int) -> int:
    value = to_float(raw, float(default))
    return default if value is None else int(value)


def parse_int_list(raw: object, default: list[int]) -> list[int]:
    if isinstance(raw, (list, tuple, set)):
        out = [int(item) for item in raw if str(item).strip()]
        return out or list(default)
    text = str(raw or "").strip()
    if not text:
        return list(default)
    out: list[int] = []
    for part in text.replace(";", ",").replace("|", ",").split(","):
        part = part.strip()
        if not part:
            continue
        out.append(int(part))
    return out or list(default)


def chunked(values: list[str], size: int) -> list[list[str]]:
    return [values[idx : idx + size] for idx in range(0, len(values), max(1, size))]


def write_csv_rows(path: Path, fieldnames: list[str], rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore", lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def maybe_table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (name,),
    ).fetchone()
    return row is not None


def load_borrow_history(
    conn: sqlite3.Connection,
    *,
    tickers: set[str],
    start_date: date,
    end_date: date,
) -> dict[str, BorrowHistory]:
    if not tickers or not maybe_table_exists(conn, "ibkr_borrow_fee_rate_daily"):
        return {}
    start = (start_date - timedelta(days=95)).isoformat()
    end = end_date.isoformat()
    rows_by_ticker: dict[str, list[tuple[date, float]]] = defaultdict(list)
    ordered = sorted(tickers)
    for ticker_chunk in chunked(ordered, SQLITE_PARAM_CHUNK_SIZE - 2):
        placeholders = ",".join("?" for _ in ticker_chunk)
        rows = conn.execute(
            f"""
            SELECT ticker, asof_date, borrow_fee_rate
            FROM ibkr_borrow_fee_rate_daily
            WHERE ticker IN ({placeholders})
              AND asof_date >= ?
              AND asof_date <= ?
              AND borrow_fee_rate IS NOT NULL
            ORDER BY ticker, asof_date
            """,
            (*ticker_chunk, start, end),
        ).fetchall()
        for row in rows:
            ticker = normalize_ticker(row["ticker"])
            day = parse_date(row["asof_date"])
            rate = to_float(row["borrow_fee_rate"])
            if ticker and day is not None and rate is not None:
                rows_by_ticker[ticker].append((day, rate))
    out: dict[str, BorrowHistory] = {}
    for ticker, values in rows_by_ticker.items():
        ordered_values = sorted(values, key=lambda item: item[0])
        out[ticker] = BorrowHistory(
            days=[item[0] for item in ordered_values],
            rates=[item[1] for item in ordered_values],
        )
    return out


def load_shortable_snapshots(conn: sqlite3.Connection, *, tickers: set[str], end_date: date) -> dict[str, list[ShortableSnapshot]]:
    if not tickers or not maybe_table_exists(conn, "ibkr_shortable_shares_snapshots"):
        return {}
    out: dict[str, list[ShortableSnapshot]] = defaultdict(list)
    ordered = sorted(tickers)
    for ticker_chunk in chunked(ordered, SQLITE_PARAM_CHUNK_SIZE - 1):
        placeholders = ",".join("?" for _ in ticker_chunk)
        rows = conn.execute(
            f"""
            SELECT ticker, asof_date, shortable_shares
            FROM ibkr_shortable_shares_snapshots
            WHERE ticker IN ({placeholders})
              AND asof_date <= ?
              AND shortable_shares IS NOT NULL
            ORDER BY ticker, asof_date
            """,
            (*ticker_chunk, end_date.isoformat()),
        ).fetchall()
        for row in rows:
            ticker = normalize_ticker(row["ticker"])
            day = parse_date(row["asof_date"])
            shares = to_float(row["shortable_shares"])
            if ticker and day is not None and shares is not None:
                out[ticker].append(ShortableSnapshot(day=day, shares=shares))
    return dict(out)


def point_in_time_borrow_features(
    *,
    ticker: str,
    asof: date,
    history_by_ticker: dict[str, BorrowHistory],
    snapshots_by_ticker: dict[str, list[ShortableSnapshot]],
    max_fee_staleness_days: int,
    max_snapshot_staleness_days: int,
    hard_to_borrow_shares: float,
) -> dict[str, Any]:
    history = history_by_ticker.get(ticker)
    current_rate: float | None = None
    rate_asof: date | None = None
    avg_30: float | None = None
    avg_90: float | None = None
    peak_90: float | None = None
    if history and history.days:
        idx = bisect.bisect_right(history.days, asof) - 1
        if idx >= 0:
            rate_asof = history.days[idx]
            if (asof - rate_asof).days <= max_fee_staleness_days:
                current_rate = history.rates[idx]
            window_90 = asof - timedelta(days=90)
            window_30 = asof - timedelta(days=30)
            start_90 = bisect.bisect_left(history.days, window_90)
            start_30 = bisect.bisect_left(history.days, window_30)
            rates_90 = history.rates[start_90 : idx + 1]
            rates_30 = history.rates[start_30 : idx + 1]
            avg_90 = sum(rates_90) / len(rates_90) if rates_90 else None
            avg_30 = sum(rates_30) / len(rates_30) if rates_30 else None
            peak_90 = max(rates_90) if rates_90 else None

    snapshot_available = False
    shortable_shares: float | None = None
    snapshots = snapshots_by_ticker.get(ticker, [])
    if snapshots:
        days = [item.day for item in snapshots]
        idx = bisect.bisect_right(days, asof) - 1
        if idx >= 0 and (asof - snapshots[idx].day).days <= max_snapshot_staleness_days:
            snapshot_available = True
            shortable_shares = snapshots[idx].shares
    hard_to_borrow = shortable_shares is not None and shortable_shares < hard_to_borrow_shares
    spike = (
        current_rate is not None
        and avg_90 is not None
        and current_rate >= 0.05
        and current_rate >= max(avg_90 * 3.0, avg_90 + 0.05)
    )
    declining = (
        current_rate is not None
        and peak_90 is not None
        and peak_90 >= 0.15
        and current_rate <= peak_90 * 0.50
    )
    pressure = borrow_cost_pressure_score(current_rate, hard_to_borrow=hard_to_borrow)
    return {
        "borrow_fee_asof_date": rate_asof.isoformat() if rate_asof is not None else "",
        "borrow_data_available_flag": 1.0 if current_rate is not None else 0.0,
        "borrow_snapshot_available_flag": 1.0 if snapshot_available else 0.0,
        "borrow_rate_current": "" if current_rate is None else round(current_rate, 8),
        "borrow_rate_30d_avg": "" if avg_30 is None else round(avg_30, 8),
        "borrow_rate_90d_avg": "" if avg_90 is None else round(avg_90, 8),
        "borrow_rate_90d_peak": "" if peak_90 is None else round(peak_90, 8),
        "borrow_rate_spike_flag": 1.0 if spike else 0.0,
        "borrow_rate_declining_flag": 1.0 if declining else 0.0,
        "shortable_shares": "" if shortable_shares is None else round(shortable_shares, 4),
        "shares_shortable_k": "" if shortable_shares is None else round(shortable_shares / 1000.0, 4),
        "hard_to_borrow_flag": 1.0 if hard_to_borrow else 0.0,
        "borrow_pressure_score": round(pressure, 4),
    }


def score_bucket(value: object) -> str:
    numeric = to_float(value)
    if numeric is None:
        return "missing"
    if numeric <= 0.0:
        return "000_zero"
    if numeric < 20.0:
        return "001_0_to_20"
    if numeric < 40.0:
        return "002_20_to_40"
    if numeric < 60.0:
        return "003_40_to_60"
    if numeric < 80.0:
        return "004_60_to_80"
    return "005_80_to_100"


def rate_bucket(value: object) -> str:
    numeric = to_float(value)
    if numeric is None:
        return "missing"
    if numeric <= 0.0:
        return "000_zero"
    if numeric < 0.01:
        return "001_lt_1pct"
    if numeric < 0.05:
        return "002_1_to_5pct"
    if numeric < 0.15:
        return "003_5_to_15pct"
    if numeric < 0.50:
        return "004_15_to_50pct"
    return "005_ge_50pct"


def enrich_borrow_diagnostics(
    rows: list[dict[str, Any]],
    *,
    history_by_ticker: dict[str, BorrowHistory],
    snapshots_by_ticker: dict[str, list[ShortableSnapshot]],
    high_borrow_pressure_min: float,
    elevated_borrow_pressure_min: float,
    high_borrow_rate_min: float,
    squeeze_short_interest_min: float,
    squeeze_catalyst_min: float,
    hard_to_borrow_shares: float,
    max_fee_staleness_days: int,
    max_snapshot_staleness_days: int,
) -> None:
    for row in rows:
        ticker = normalize_ticker(row.get("ticker"))
        asof = parse_date(row.get("asof_date"))
        if not ticker or asof is None:
            continue
        features = point_in_time_borrow_features(
            ticker=ticker,
            asof=asof,
            history_by_ticker=history_by_ticker,
            snapshots_by_ticker=snapshots_by_ticker,
            max_fee_staleness_days=max_fee_staleness_days,
            max_snapshot_staleness_days=max_snapshot_staleness_days,
            hard_to_borrow_shares=hard_to_borrow_shares,
        )
        row.update(features)
        pressure = to_float(features.get("borrow_pressure_score"), 0.0) or 0.0
        current_rate = to_float(features.get("borrow_rate_current"), 0.0) or 0.0
        high_borrow = pressure >= high_borrow_pressure_min
        elevated_borrow = pressure >= elevated_borrow_pressure_min
        high_short = (
            (to_float(row.get("short_interest_pct_float"), 0.0) or 0.0) >= 0.10
            or (to_float(row.get("short_interest_signal_score"), 0.0) or 0.0) >= squeeze_short_interest_min
        )
        catalyst_or_quality = (
            (to_float(row.get("forward_catalyst_score"), 0.0) or 0.0) >= squeeze_catalyst_min
            or (to_float(row.get("sec_catalyst_score_used"), 0.0) or 0.0) >= 10.0
            or (to_float(row.get("indication_success_multiplier"), 1.0) or 1.0) > 1.05
        )
        weak_or_distressed = (
            (to_float(row.get("risk_for_penalty_score_raw"), 0.0) or 0.0) >= 65.0
            or (to_float(row.get("financial_quality_score_raw"), 100.0) or 100.0) < 40.0
            or (to_float(row.get("uncompensated_risk_score_raw"), 0.0) or 0.0) >= 60.0
            or (to_float(row.get("diag_core_hard_weakness_flag"), 0.0) or 0.0) > 0.0
        )
        row["high_borrow_pressure_flag"] = 1.0 if high_borrow else 0.0
        row["elevated_borrow_pressure_flag"] = 1.0 if elevated_borrow else 0.0
        row["borrow_rate_high_flag"] = 1.0 if current_rate >= high_borrow_rate_min else 0.0
        elevated_or_high_rate = elevated_borrow or current_rate >= high_borrow_rate_min
        row["borrow_squeeze_setup_flag"] = 1.0 if elevated_or_high_rate and high_short and catalyst_or_quality and not weak_or_distressed else 0.0
        row["borrow_distress_flag"] = 1.0 if high_borrow and weak_or_distressed else 0.0
        row["borrow_pressure_bucket"] = score_bucket(pressure)
        row["borrow_rate_bucket"] = rate_bucket(current_rate if features.get("borrow_data_available_flag") else None)


def completed_rows(rows: list[dict[str, Any]], ret_key: str) -> list[dict[str, Any]]:
    return [row for row in rows if to_float(row.get(ret_key)) is not None]


def split_train_test(
    rows: list[dict[str, Any]],
    *,
    train_fraction: float,
    embargo_days: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    dates = sorted({parsed for row in rows if (parsed := parse_date(row.get("asof_date"))) is not None})
    if len(dates) < 2:
        return rows, []
    train_idx = max(0, min(len(dates) - 1, int(len(dates) * train_fraction) - 1))
    train_end = dates[train_idx]
    test_start = train_end + timedelta(days=max(0, embargo_days))
    train_rows = [row for row in rows if (parsed := parse_date(row.get("asof_date"))) is not None and parsed <= train_end]
    test_rows = [row for row in rows if (parsed := parse_date(row.get("asof_date"))) is not None and parsed > test_start]
    return train_rows, test_rows


def metric_row(
    *,
    calibration: Any,
    rows: list[dict[str, Any]],
    params: Any,
    ret_key: str,
    prefix: dict[str, Any],
) -> dict[str, Any]:
    metrics = calibration.summarize_return_risk(calibration.numeric_values(rows, ret_key), params=params)
    return {**prefix, **metrics}


def cohort_groups(rows: list[dict[str, Any]]) -> list[tuple[str, list[dict[str, Any]]]]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        cohort = str(row.get("biotech_primary_cohort") or "unclassified")
        groups[cohort].append(row)
    return [("ALL", rows), *sorted(groups.items())]


def build_bucket_rows(
    *,
    calibration: Any,
    rows: list[dict[str, Any]],
    horizons: list[int],
    params: Any,
    train_fraction: float,
    embargo_days: int,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for horizon in horizons:
        ret_key = calibration.objective_return_key(horizon, params)
        horizon_rows = completed_rows(rows, ret_key)
        train_rows, test_rows = split_train_test(horizon_rows, train_fraction=train_fraction, embargo_days=embargo_days)
        for sample, sample_rows in (("all", horizon_rows), ("train", train_rows), ("test", test_rows)):
            for cohort, cohort_rows in cohort_groups(sample_rows):
                for bucket_source, bucket_key in (
                    ("borrow_pressure_score", "borrow_pressure_bucket"),
                    ("borrow_rate_current", "borrow_rate_bucket"),
                ):
                    buckets: dict[str, list[dict[str, Any]]] = defaultdict(list)
                    for row in cohort_rows:
                        buckets[str(row.get(bucket_key) or "missing")].append(row)
                    for bucket, bucket_rows in sorted(buckets.items()):
                        out.append(
                            metric_row(
                                calibration=calibration,
                                rows=bucket_rows,
                                params=params,
                                ret_key=ret_key,
                                prefix={
                                    "sample": sample,
                                    "horizon_days": horizon,
                                    "return_key": ret_key,
                                    "cohort": cohort,
                                    "bucket_source": bucket_source,
                                    "bucket": bucket,
                                },
                            )
                        )
    return out


def build_flag_rows(
    *,
    calibration: Any,
    rows: list[dict[str, Any]],
    horizons: list[int],
    params: Any,
    train_fraction: float,
    embargo_days: int,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for horizon in horizons:
        ret_key = calibration.objective_return_key(horizon, params)
        horizon_rows = completed_rows(rows, ret_key)
        train_rows, test_rows = split_train_test(horizon_rows, train_fraction=train_fraction, embargo_days=embargo_days)
        for sample, sample_rows in (("all", horizon_rows), ("train", train_rows), ("test", test_rows)):
            for cohort, cohort_rows in cohort_groups(sample_rows):
                for flag in BORROW_FLAG_KEYS:
                    for flag_value in (0.0, 1.0):
                        selected = [row for row in cohort_rows if (to_float(row.get(flag), 0.0) or 0.0) == flag_value]
                        out.append(
                            metric_row(
                                calibration=calibration,
                                rows=selected,
                                params=params,
                                ret_key=ret_key,
                                prefix={
                                    "sample": sample,
                                    "horizon_days": horizon,
                                    "return_key": ret_key,
                                    "cohort": cohort,
                                    "flag_name": flag,
                                    "flag_value": int(flag_value),
                                },
                            )
                        )
    return out


def numeric_metric(row: dict[str, Any], key: str) -> float | None:
    return to_float(row.get(key))


def metric_delta(left: dict[str, Any], right: dict[str, Any], key: str) -> float | None:
    left_value = numeric_metric(left, key)
    right_value = numeric_metric(right, key)
    if left_value is None or right_value is None:
        return None
    return left_value - right_value


def build_validation_summary(flag_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    indexed: dict[tuple[str, int, str, str, int], dict[str, Any]] = {}
    for row in flag_rows:
        indexed[
            (
                str(row.get("sample") or ""),
                int(to_float(row.get("horizon_days"), 0.0) or 0.0),
                str(row.get("cohort") or ""),
                str(row.get("flag_name") or ""),
                int(to_float(row.get("flag_value"), 0.0) or 0.0),
            )
        ] = row
    out: list[dict[str, Any]] = []
    for sample, horizon, cohort, flag, value in sorted(indexed):
        if value != 1 or cohort != "ALL" or flag not in {
            "high_borrow_pressure_flag",
            "borrow_squeeze_setup_flag",
            "borrow_distress_flag",
            "borrow_rate_high_flag",
        }:
            continue
        positive = indexed[(sample, horizon, cohort, flag, 1)]
        negative = indexed.get((sample, horizon, cohort, flag, 0), {})
        lcb_delta = metric_delta(positive, negative, "lcb_return_pct") if negative else None
        loss20_delta = metric_delta(positive, negative, "large_loss_20pct_rate_pct") if negative else None
        mean_delta = metric_delta(positive, negative, "mean_return_pct") if negative else None
        out.append(
            {
                "sample": sample,
                "horizon_days": horizon,
                "cohort": cohort,
                "comparison_flag": flag,
                "positive_n": positive.get("n", 0),
                "negative_n": negative.get("n", 0) if negative else 0,
                "positive_lcb_return_pct": positive.get("lcb_return_pct", ""),
                "negative_lcb_return_pct": negative.get("lcb_return_pct", ""),
                "lcb_delta_pct": "" if lcb_delta is None else round(lcb_delta, 6),
                "positive_mean_return_pct": positive.get("mean_return_pct", ""),
                "negative_mean_return_pct": negative.get("mean_return_pct", ""),
                "mean_delta_pct": "" if mean_delta is None else round(mean_delta, 6),
                "positive_loss20_pct": positive.get("large_loss_20pct_rate_pct", ""),
                "negative_loss20_pct": negative.get("large_loss_20pct_rate_pct", ""),
                "loss20_delta_pct": "" if loss20_delta is None else round(loss20_delta, 6),
                "recommendation": "shadow_only_pending_promotion_review",
            }
        )
    return out


def build_feature_qa_rows(
    *,
    rows: list[dict[str, Any]],
    snapshot_dates: list[str],
    history_by_ticker: dict[str, BorrowHistory],
    snapshots_by_ticker: dict[str, list[ShortableSnapshot]],
    horizons: list[int],
    calibration: Any,
    params: Any,
) -> list[dict[str, Any]]:
    tickers = {normalize_ticker(row.get("ticker")) for row in rows if normalize_ticker(row.get("ticker"))}
    available = [row for row in rows if (to_float(row.get("borrow_data_available_flag"), 0.0) or 0.0) > 0.0]
    snapshot_available = [
        row for row in rows if (to_float(row.get("borrow_snapshot_available_flag"), 0.0) or 0.0) > 0.0
    ]
    rates = [to_float(row.get("borrow_rate_current")) for row in available]
    rates = [rate for rate in rates if rate is not None]
    pressure = [to_float(row.get("borrow_pressure_score")) for row in available]
    pressure = [value for value in pressure if value is not None]
    completed_by_horizon = {}
    for horizon in horizons:
        ret_key = calibration.objective_return_key(horizon, params)
        completed_by_horizon[f"completed_return_rows_{horizon}d"] = len(completed_rows(rows, ret_key))
    return [
        {
            "qa_item": "coverage",
            "snapshot_date_count": len(snapshot_dates),
            "first_snapshot_date": snapshot_dates[0] if snapshot_dates else "",
            "last_snapshot_date": snapshot_dates[-1] if snapshot_dates else "",
            "observation_rows": len(rows),
            "ticker_count": len(tickers),
            "fee_history_ticker_count": len(history_by_ticker),
            "shortable_snapshot_ticker_count": len(snapshots_by_ticker),
            "borrow_fee_available_rows": len(available),
            "borrow_fee_available_pct": round(100.0 * len(available) / len(rows), 6) if rows else "",
            "shortable_snapshot_available_rows": len(snapshot_available),
            "shortable_snapshot_available_pct": round(100.0 * len(snapshot_available) / len(rows), 6) if rows else "",
            **completed_by_horizon,
        },
        {
            "qa_item": "borrow_rate_distribution",
            "borrow_rate_min": "" if not rates else round(min(rates), 8),
            "borrow_rate_median": "" if not rates else round(sorted(rates)[len(rates) // 2], 8),
            "borrow_rate_mean": "" if not rates else round(sum(rates) / len(rates), 8),
            "borrow_rate_max": "" if not rates else round(max(rates), 8),
            "borrow_pressure_min": "" if not pressure else round(min(pressure), 4),
            "borrow_pressure_median": "" if not pressure else round(sorted(pressure)[len(pressure) // 2], 4),
            "borrow_pressure_mean": "" if not pressure else round(sum(pressure) / len(pressure), 4),
            "borrow_pressure_max": "" if not pressure else round(max(pressure), 4),
        },
    ]


def main() -> None:
    configure_utc_logging()
    args = parse_args()
    calibration = load_calibration_module()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    base_dir = config_path.parent
    db_path = args.db or resolve_path(cfg_get(config, "paths.database_path"), base_dir=base_dir)
    positioning_db_path = args.market_positioning_db or resolve_path(
        cfg_get(config, "market_positioning.database_path"),
        base_dir=base_dir,
    )
    validation_cfg = cfg_get(config, "biotech_reports.borrow_availability_validation", {}) or {}
    if not isinstance(validation_cfg, dict):
        validation_cfg = {}
    output_dir = args.output_dir or resolve_path(
        validation_cfg.get("output_dir", "../output/biotech_index_reports/borrow_availability_validation"),
        base_dir=base_dir,
    )
    start_asof = parse_date(args.start_asof)
    end_asof = parse_date(args.end_asof)
    horizons = parse_int_list(args.horizons or validation_cfg.get("horizons"), [20, 60, 120])
    max_snapshots = max(0, int(args.max_snapshots or validation_cfg.get("max_snapshots", 0) or 0))
    strict_feature_lag = (
        args.strict_feature_lag
        if args.strict_feature_lag is not None
        else as_bool(cfg_get(config, "calibration.tier1.strict_feature_lag", True), True)
    )
    next_bar_entry = (
        args.next_bar_entry
        if args.next_bar_entry is not None
        else as_bool(cfg_get(config, "calibration.tier1.next_bar_entry", True), True)
    )
    train_fraction = float(args.train_fraction or validation_cfg.get("train_fraction", 0.70))
    train_fraction = max(0.10, min(0.90, train_fraction))
    # Horizons are trading bars; convert to calendar days for the embargo default
    # so forward-return overlap cannot leak across the split (see scripts 27/28).
    default_embargo_days = math.ceil(max(horizons) * 365.25 / 252.0) + 10
    embargo_days = int(
        args.embargo_days if args.embargo_days is not None else validation_cfg.get("embargo_days", default_embargo_days)
    )
    if embargo_days < default_embargo_days:
        LOGGER.warning(
            "Configured embargo_days=%d is below the leakage-safe calendar-day default %d for a %d-bar horizon; honoring configured value.",
            embargo_days,
            default_embargo_days,
            max(horizons),
        )
    market_sources_raw = args.market_sources if str(args.market_sources or "").strip() else None
    market_sources = [
        str(source).strip()
        for source in calibration.normalize_string_list(market_sources_raw, calibration_market_sources(config))
        if str(source).strip()
    ]
    if not market_sources:
        raise ValueError("No market sources configured for borrow validation.")
    params = calibration.load_calibration_params(config)
    min_addv20 = float(
        cfg_get(
            config,
            "biotech_scoring.core_structural_veto.min_addv20",
            cfg_get(config, "multibagger.min_addv20", 1_000_000.0),
        )
    )
    with connect(db_path, timeout_sec=float(cfg_get(config, "runtime.sqlite_timeout_sec", 30.0))) as conn:
        snapshot_dates = calibration.load_snapshot_dates(
            conn,
            start_asof=start_asof,
            end_asof=end_asof,
            fridays_only=not args.include_non_fridays,
            max_snapshots=max_snapshots,
        )
        if not snapshot_dates:
            raise ValueError("No daily_features snapshot dates found for borrow validation.")
        excluded_tickers = calibration.load_excluded_tickers(
            conn,
            exclude_current_removals=False,
            extra=set(),
        )
        rows = calibration.load_observations(
            conn,
            snapshot_dates,
            excluded_tickers,
            config,
            min_addv20=min_addv20,
            strict_feature_lag=strict_feature_lag,
            growth_drag_curve=params.growth_drag_curve,
            use_decomposed_risk_for_penalty=params.use_decomposed_risk_for_penalty,
        )
        if not rows:
            raise ValueError("No observations remain for borrow validation.")
        asof_dates = [parsed for row in rows if (parsed := parse_date(row.get("asof_date"))) is not None]
        if not asof_dates:
            raise ValueError("Borrow validation observations have no valid as-of dates.")
        tickers = {normalize_ticker(row.get("ticker")) for row in rows if normalize_ticker(row.get("ticker"))}
        benchmark_ticker = params.benchmark_ticker if params.alpha_adjustment_enabled else ""
        market_tickers = set(tickers)
        if benchmark_ticker:
            market_tickers.add(benchmark_ticker)
        bars_by_ticker = calibration.load_bars(
            conn,
            tickers=market_tickers,
            min_date=min(asof_dates),
            market_sources=market_sources,
        )

    with connect_positioning(positioning_db_path) as positioning_conn:
        history_by_ticker = load_borrow_history(
            positioning_conn,
            tickers=tickers,
            start_date=min(asof_dates),
            end_date=max(asof_dates),
        )
        snapshots_by_ticker = load_shortable_snapshots(
            positioning_conn,
            tickers=tickers,
            end_date=max(asof_dates),
        )

    enrich_borrow_diagnostics(
        rows,
        history_by_ticker=history_by_ticker,
        snapshots_by_ticker=snapshots_by_ticker,
        high_borrow_pressure_min=config_float(validation_cfg.get("high_borrow_pressure_min"), 60.0),
        elevated_borrow_pressure_min=config_float(validation_cfg.get("elevated_borrow_pressure_min"), 30.0),
        high_borrow_rate_min=config_float(validation_cfg.get("high_borrow_rate_min"), 0.15),
        squeeze_short_interest_min=config_float(validation_cfg.get("squeeze_short_interest_min"), 60.0),
        squeeze_catalyst_min=config_float(validation_cfg.get("squeeze_catalyst_min"), 40.0),
        hard_to_borrow_shares=config_float(validation_cfg.get("hard_to_borrow_shares"), 50_000.0),
        max_fee_staleness_days=config_int(validation_cfg.get("max_fee_staleness_days"), 10),
        max_snapshot_staleness_days=config_int(validation_cfg.get("max_snapshot_staleness_days"), 7),
    )
    calibration.add_forward_returns(
        rows,
        bars_by_ticker,
        horizons,
        round_trip_cost_bps=params.round_trip_cost_bps,
        next_bar_entry=next_bar_entry,
        benchmark_ticker=params.benchmark_ticker if params.alpha_adjustment_enabled else "",
        benchmark_bars=bars_by_ticker.get(params.benchmark_ticker, []) if params.alpha_adjustment_enabled else [],
    )

    feature_qa_rows = build_feature_qa_rows(
        rows=rows,
        snapshot_dates=snapshot_dates,
        history_by_ticker=history_by_ticker,
        snapshots_by_ticker=snapshots_by_ticker,
        horizons=horizons,
        calibration=calibration,
        params=params,
    )
    bucket_rows = build_bucket_rows(
        calibration=calibration,
        rows=rows,
        horizons=horizons,
        params=params,
        train_fraction=train_fraction,
        embargo_days=embargo_days,
    )
    flag_rows = build_flag_rows(
        calibration=calibration,
        rows=rows,
        horizons=horizons,
        params=params,
        train_fraction=train_fraction,
        embargo_days=embargo_days,
    )
    summary_rows = build_validation_summary(flag_rows)
    top_rows = sorted(
        rows,
        key=lambda row: (
            -(to_float(row.get("borrow_pressure_score"), 0.0) or 0.0),
            str(row.get("ticker") or ""),
        ),
    )[:200]

    output_dir.mkdir(parents=True, exist_ok=True)
    metric_fields = [
        "sample",
        "horizon_days",
        "return_key",
        "cohort",
        "bucket_source",
        "bucket",
        *METRIC_KEYS,
    ]
    write_csv_rows(output_dir / "borrow_feature_qa.csv", sorted({key for row in feature_qa_rows for key in row}), feature_qa_rows)
    write_csv_rows(output_dir / "borrow_bucket_performance.csv", metric_fields, bucket_rows)
    write_csv_rows(
        output_dir / "borrow_flag_performance.csv",
        ["sample", "horizon_days", "return_key", "cohort", "flag_name", "flag_value", *METRIC_KEYS],
        flag_rows,
    )
    write_csv_rows(
        output_dir / "borrow_validation_summary.csv",
        [
            "sample",
            "horizon_days",
            "cohort",
            "comparison_flag",
            "positive_n",
            "negative_n",
            "positive_lcb_return_pct",
            "negative_lcb_return_pct",
            "lcb_delta_pct",
            "positive_mean_return_pct",
            "negative_mean_return_pct",
            "mean_delta_pct",
            "positive_loss20_pct",
            "negative_loss20_pct",
            "loss20_delta_pct",
            "recommendation",
        ],
        summary_rows,
    )
    top_fields = [
        "asof_date",
        "ticker",
        "company_name",
        "biotech_primary_cohort",
        "borrow_fee_asof_date",
        "borrow_rate_current",
        "borrow_rate_30d_avg",
        "borrow_rate_90d_avg",
        "borrow_rate_90d_peak",
        "borrow_pressure_score",
        "short_interest_pct_float",
        "short_interest_signal_score",
        "forward_catalyst_score",
        "risk_for_penalty_score_raw",
        "financial_quality_score_raw",
        "high_borrow_pressure_flag",
        "borrow_squeeze_setup_flag",
        "borrow_distress_flag",
        "borrow_rate_spike_flag",
        "borrow_rate_declining_flag",
        "hard_to_borrow_flag",
        "shortable_shares",
    ]
    write_csv_rows(output_dir / "borrow_top_ticker_diagnostics.csv", top_fields, top_rows)
    LOGGER.info(
        "Borrow validation complete: observations=%d snapshots=%d output_dir=%s shadow_only=true",
        len(rows),
        len(snapshot_dates),
        output_dir,
    )


if __name__ == "__main__":
    main()
