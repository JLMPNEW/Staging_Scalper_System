#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import importlib.util
import math
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path
from statistics import mean, median
from typing import Any, Iterable


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from biotech_index.core.config import cfg_get, load_yaml, normalize_string_list, resolve_path  # noqa: E402
from biotech_index.core.db import connect  # noqa: E402
from biotech_index.core.market_policy import calibration_market_sources  # noqa: E402
from biotech_index.core.text_norm import normalize_ticker  # noqa: E402


DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "output" / "biotech_index_reports" / "borrow_rank_lift_validation"
CALIBRATION_MODULE_PATH = PACKAGE_ROOT / "scripts" / "28_calibrate_biotech_opportunity.py"
DEFAULT_TOP_N = [10, 20]
DEFAULT_BONUSES = [4.0, 6.0, 8.0]
DEFAULT_SCORE_FIELDS = ["allocation_opportunity_score", "discovery_opportunity_score"]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Rank-lift probe for biotech borrow signals. This evaluates elevated-borrow names across the "
            "full ranked universe and estimates whether reasonable shadow bonuses could move them into "
            "Top-N allocation/discovery lists. It is report-only and does not mutate scoring."
        )
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--start-asof", type=str, default="")
    parser.add_argument("--end-asof", "--asof", dest="end_asof", type=str, default="")
    parser.add_argument("--horizons", type=str, default="20,60,120")
    parser.add_argument("--top-n", type=str, default="10,20")
    parser.add_argument("--bonus-points", type=str, default="4,6,8")
    parser.add_argument("--score-fields", type=str, default="allocation_opportunity_score,discovery_opportunity_score")
    parser.add_argument("--market-sources", type=str, default="")
    parser.add_argument("--max-snapshots", type=int, default=0)
    parser.add_argument("--include-non-fridays", action="store_true")
    parser.add_argument("--strict-feature-lag", action=argparse.BooleanOptionalAction, default=None)
    parser.add_argument("--next-bar-entry", action=argparse.BooleanOptionalAction, default=None)
    parser.add_argument("--elevated-borrow-pressure-min", type=float, default=None)
    parser.add_argument("--high-borrow-pressure-min", type=float, default=None)
    parser.add_argument("--high-borrow-rate-min", type=float, default=None)
    parser.add_argument("--require-completed-forward-return", action=argparse.BooleanOptionalAction, default=True)
    return parser.parse_args()


def load_calibration_module() -> Any:
    spec = importlib.util.spec_from_file_location("biotech_borrow_rank_lift_calibration", CALIBRATION_MODULE_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load calibration module from {CALIBRATION_MODULE_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def parse_optional_date(raw: str) -> date | None:
    clean = str(raw or "").strip()
    if not clean:
        return None
    if len(clean) == 8 and clean.isdigit():
        clean = f"{clean[:4]}-{clean[4:6]}-{clean[6:]}"
    return date.fromisoformat(clean)


def parse_int_list(raw: object, default: list[int]) -> list[int]:
    text = str(raw or "").strip()
    if not text:
        return list(default)
    out: list[int] = []
    for part in text.replace(";", ",").replace("|", ",").split(","):
        clean = part.strip()
        if clean:
            out.append(int(clean))
    return sorted(set(out)) or list(default)


def parse_float_list(raw: object, default: list[float]) -> list[float]:
    text = str(raw or "").strip()
    if not text:
        return list(default)
    out: list[float] = []
    for part in text.replace(";", ",").replace("|", ",").split(","):
        clean = part.strip()
        if clean:
            out.append(float(clean))
    return sorted(set(out)) or list(default)


def to_float(raw: object, default: float | None = None) -> float | None:
    try:
        value = float(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    if not math.isfinite(value):
        return default
    return value


def pct(value: float | None) -> str:
    return "" if value is None else f"{100.0 * value:.4f}"


def write_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str] | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if fieldnames is None:
        fieldnames = []
        seen: set[str] = set()
        for row in rows:
            for key in row:
                if key not in seen:
                    seen.add(key)
                    fieldnames.append(key)
        if not fieldnames:
            fieldnames = ["message"]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore", lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def table_columns(conn: Any, table: str) -> set[str]:
    return {str(row["name"]) for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def lcb(values: list[float], *, z: float) -> float | None:
    if not values:
        return None
    if len(values) == 1:
        return values[0]
    avg = mean(values)
    variance = sum((value - avg) ** 2 for value in values) / (len(values) - 1)
    return avg - z * math.sqrt(variance) / math.sqrt(len(values))


def top3_gain_contribution(values: list[float]) -> float | None:
    gains = sorted((value for value in values if value > 0.0), reverse=True)
    total_gain = sum(gains)
    if total_gain <= 0.0:
        return 0.0 if values else None
    return 100.0 * sum(gains[:3]) / total_gain


def metrics_for_returns(values: list[float], *, lcb_z: float) -> dict[str, Any]:
    clean = [value for value in values if math.isfinite(value)]
    lower = lcb(clean, z=lcb_z)
    return {
        "n": len(clean),
        "mean_return_pct": "" if not clean else round(100.0 * mean(clean), 4),
        "median_return_pct": "" if not clean else round(100.0 * median(clean), 4),
        "hit_rate_pct": "" if not clean else round(100.0 * sum(1 for value in clean if value > 0.0) / len(clean), 4),
        "lcb_return_pct": "" if lower is None else round(100.0 * lower, 4),
        "large_loss_20pct_rate_pct": "" if not clean else round(100.0 * sum(1 for value in clean if value <= -0.20) / len(clean), 4),
        "large_gain_20pct_rate_pct": "" if not clean else round(100.0 * sum(1 for value in clean if value >= 0.20) / len(clean), 4),
        "worst_return_pct": "" if not clean else round(100.0 * min(clean), 4),
        "best_return_pct": "" if not clean else round(100.0 * max(clean), 4),
        "top3_gain_contribution_pct": "" if (top3 := top3_gain_contribution(clean)) is None else round(top3, 4),
    }


def score_field_list(raw: str) -> list[str]:
    requested = [item.strip() for item in str(raw or "").replace(";", ",").replace("|", ",").split(",") if item.strip()]
    return requested or list(DEFAULT_SCORE_FIELDS)


def score_field_label(field: str) -> str:
    if field == "allocation_opportunity_score":
        return "allocation"
    if field == "discovery_opportunity_score":
        return "discovery"
    if field == "production_rank_score":
        return "production_rank"
    return field


def score_columns_for_query(conn: Any, score_fields: list[str]) -> list[str]:
    available = table_columns(conn, "daily_scores")
    required = {
        "asof_date",
        "ticker",
        "company_name",
        "company_id",
        "rank",
        "bucket",
        "allocation_bucket",
        "rank_quality_cap_vetoed",
        "core_structural_veto_flag",
        "biotech_cohort_investible_flag",
        "biotech_primary_cohort",
        "biotech_cohort_reason_codes",
        "borrow_pressure_score",
        "borrow_rate_current",
        "short_interest_signal_score",
        "short_interest_pct_float",
        "forward_catalyst_score",
        "momentum_score",
        "risk_score",
        "financial_quality_score",
        "uncompensated_risk_score",
        "elevated_borrow_pressure_flag",
        "high_borrow_pressure_flag",
        "borrow_rate_high_flag",
        "borrow_squeeze_setup_flag",
        "borrow_distress_flag",
    }
    selected = [column for column in sorted(required.union(score_fields)) if column in available]
    missing_scores = [field for field in score_fields if field not in available]
    if missing_scores:
        raise RuntimeError(f"daily_scores is missing requested score field(s): {missing_scores}")
    return selected


def load_score_rows(
    conn: Any,
    *,
    asof_dates: list[str],
    score_fields: list[str],
) -> list[dict[str, Any]]:
    if not asof_dates:
        return []
    selected = score_columns_for_query(conn, score_fields)
    placeholders = ",".join("?" for _ in asof_dates)
    rows = conn.execute(
        f"""
        SELECT {", ".join(selected)}
        FROM daily_scores
        WHERE asof_date IN ({placeholders})
        """,
        tuple(asof_dates),
    ).fetchall()
    return [dict(row) for row in rows]


def float_or_default(raw: object, default: float) -> float:
    value = to_float(raw, None)
    return default if value is None else value


def rank_rows(rows: list[dict[str, Any]], *, score_field: str) -> list[dict[str, Any]]:
    ranked = [
        row
        for row in rows
        if normalize_ticker(row.get("ticker")) and to_float(row.get(score_field)) is not None
    ]
    return sorted(
        ranked,
        key=lambda row: (
            -float_or_default(row.get(score_field), -1e12),
            float_or_default(row.get("risk_score"), 100.0),
            normalize_ticker(row.get("ticker")),
        ),
    )


def borrow_candidate_status(
    row: dict[str, Any],
    *,
    elevated_borrow_pressure_min: float,
    high_borrow_pressure_min: float,
    high_borrow_rate_min: float,
) -> dict[str, Any]:
    pressure = to_float(row.get("borrow_pressure_score"), 0.0) or 0.0
    rate = to_float(row.get("borrow_rate_current"), 0.0) or 0.0
    elevated = pressure >= elevated_borrow_pressure_min
    high_pressure = pressure >= high_borrow_pressure_min
    high_rate = rate >= high_borrow_rate_min
    squeeze = (to_float(row.get("borrow_squeeze_setup_flag"), 0.0) or 0.0) > 0.0
    distress = (to_float(row.get("borrow_distress_flag"), 0.0) or 0.0) > 0.0
    candidate = elevated or high_rate or squeeze or distress
    return {
        "is_borrow_lift_candidate": candidate,
        "elevated_borrow_pressure_flag_calc": 1.0 if elevated else 0.0,
        "high_borrow_pressure_flag_calc": 1.0 if high_pressure else 0.0,
        "borrow_rate_high_flag_calc": 1.0 if high_rate else 0.0,
        "borrow_squeeze_setup_flag_calc": 1.0 if squeeze else 0.0,
        "borrow_distress_flag_calc": 1.0 if distress else 0.0,
    }


def rank_gap_rows_for_date(
    *,
    asof_date: str,
    rows: list[dict[str, Any]],
    score_field: str,
    top_ns: list[int],
    bonuses: list[float],
    thresholds: dict[str, float],
) -> list[dict[str, Any]]:
    ranked = rank_rows(rows, score_field=score_field)
    rank_by_ticker = {normalize_ticker(row.get("ticker")): idx for idx, row in enumerate(ranked, start=1)}
    cutoff_by_top_n: dict[int, float] = {}
    for top_n in top_ns:
        if len(ranked) >= top_n:
            cutoff_by_top_n[top_n] = to_float(ranked[top_n - 1].get(score_field), 0.0) or 0.0
    out: list[dict[str, Any]] = []
    for row in ranked:
        status = borrow_candidate_status(
            row,
            elevated_borrow_pressure_min=thresholds["elevated_borrow_pressure_min"],
            high_borrow_pressure_min=thresholds["high_borrow_pressure_min"],
            high_borrow_rate_min=thresholds["high_borrow_rate_min"],
        )
        if not status["is_borrow_lift_candidate"]:
            continue
        ticker = normalize_ticker(row.get("ticker"))
        score = to_float(row.get(score_field), 0.0) or 0.0
        base = {
            "asof_date": asof_date,
            "score_field": score_field,
            "score_purpose": score_field_label(score_field),
            "ticker": ticker,
            "company_name": row.get("company_name", ""),
            "biotech_primary_cohort": row.get("biotech_primary_cohort", ""),
            "rank": rank_by_ticker.get(ticker, ""),
            "score": round(score, 4),
            "allocation_bucket": row.get("allocation_bucket", row.get("bucket", "")),
            "rank_quality_cap_vetoed": row.get("rank_quality_cap_vetoed", ""),
            "borrow_pressure_score": row.get("borrow_pressure_score", ""),
            "borrow_rate_current": row.get("borrow_rate_current", ""),
            "short_interest_signal_score": row.get("short_interest_signal_score", ""),
            "short_interest_pct_float": row.get("short_interest_pct_float", ""),
            "forward_catalyst_score": row.get("forward_catalyst_score", ""),
            "momentum_score": row.get("momentum_score", ""),
            "risk_score": row.get("risk_score", ""),
            **{key: value for key, value in status.items() if key != "is_borrow_lift_candidate"},
        }
        for top_n in top_ns:
            cutoff = cutoff_by_top_n.get(top_n)
            if cutoff is None:
                continue
            gap = max(0.0, cutoff - score)
            row_out = {
                **base,
                "top_n": top_n,
                "cutoff_score": round(cutoff, 4),
                "score_gap_to_cutoff": round(gap, 4),
            }
            for bonus in bonuses:
                row_out[f"bonus_{bonus:g}_closes_gap"] = 1.0 if score + bonus >= cutoff else 0.0
                row_out[f"bonus_{bonus:g}_post_score"] = round(score + bonus, 4)
            out.append(row_out)
    return out


def simulate_bonus_for_date(
    *,
    asof_date: str,
    rows: list[dict[str, Any]],
    score_field: str,
    top_n: int,
    bonus: float,
    thresholds: dict[str, float],
) -> list[dict[str, Any]]:
    simulated: list[dict[str, Any]] = []
    for row in rows:
        score = to_float(row.get(score_field))
        if score is None:
            continue
        status = borrow_candidate_status(
            row,
            elevated_borrow_pressure_min=thresholds["elevated_borrow_pressure_min"],
            high_borrow_pressure_min=thresholds["high_borrow_pressure_min"],
            high_borrow_rate_min=thresholds["high_borrow_rate_min"],
        )
        adjusted = score + bonus if status["is_borrow_lift_candidate"] else score
        simulated.append({**row, "_base_score": score, "_adjusted_score": adjusted, **status})
    ranked = sorted(
        simulated,
        key=lambda row: (
            -float_or_default(row.get("_adjusted_score"), -1e12),
            float_or_default(row.get("risk_score"), 100.0),
            normalize_ticker(row.get("ticker")),
        ),
    )
    base_ranked = rank_rows(rows, score_field=score_field)
    base_top = {normalize_ticker(row.get("ticker")) for row in base_ranked[:top_n]}
    out: list[dict[str, Any]] = []
    for rank, row in enumerate(ranked[:top_n], start=1):
        ticker = normalize_ticker(row.get("ticker"))
        out.append(
            {
                **row,
                "asof_date": asof_date,
                "score_field": score_field,
                "score_purpose": score_field_label(score_field),
                "top_n": top_n,
                "bonus_points": bonus,
                "simulated_rank": rank,
                "base_score": round(to_float(row.get("_base_score"), 0.0) or 0.0, 4),
                "adjusted_score": round(to_float(row.get("_adjusted_score"), 0.0) or 0.0, 4),
                "borrow_bonus_applied": 1.0 if row.get("is_borrow_lift_candidate") else 0.0,
                "new_entrant_vs_base_topn": 1.0 if ticker not in base_top else 0.0,
            }
        )
    return out


def completed_return_rows(rows: Iterable[dict[str, Any]], ret_key: str) -> list[dict[str, Any]]:
    return [row for row in rows if to_float(row.get(ret_key)) is not None]


def aggregate_rows(
    rows: list[dict[str, Any]],
    *,
    horizon: int,
    group_fields: dict[str, Any],
    ret_key: str,
    lcb_z: float,
) -> dict[str, Any]:
    completed = completed_return_rows(rows, ret_key)
    returns = [to_float(row.get(ret_key), 0.0) or 0.0 for row in completed]
    tickers = {normalize_ticker(row.get("ticker")) for row in completed if normalize_ticker(row.get("ticker"))}
    asofs = {str(row.get("asof_date") or "") for row in completed if str(row.get("asof_date") or "")}
    borrow_rows = [row for row in completed if (to_float(row.get("borrow_bonus_applied"), 0.0) or 0.0) > 0.0]
    new_entrants = [row for row in completed if (to_float(row.get("new_entrant_vs_base_topn"), 0.0) or 0.0) > 0.0]
    return {
        **group_fields,
        "horizon_days": horizon,
        "completed_forward_return_rows": len(completed),
        "unique_tickers": len(tickers),
        "asof_dates": len(asofs),
        "borrow_bonus_applied_rows": len(borrow_rows),
        "new_entrant_rows": len(new_entrants),
        "new_entrant_rate_pct": "" if not completed else round(100.0 * len(new_entrants) / len(completed), 4),
        **metrics_for_returns(returns, lcb_z=lcb_z),
    }


def build_gap_summary(gap_rows: list[dict[str, Any]], bonuses: list[float]) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, int], list[dict[str, Any]]] = defaultdict(list)
    for row in gap_rows:
        grouped[(str(row.get("score_field") or ""), int(to_float(row.get("top_n"), 0.0) or 0))].append(row)
    out: list[dict[str, Any]] = []
    for (score_field, top_n), rows in sorted(grouped.items()):
        gaps: list[float] = []
        for row in rows:
            gap = to_float(row.get("score_gap_to_cutoff"))
            if gap is not None:
                gaps.append(gap)
        base: dict[str, Any] = {
            "score_field": score_field,
            "score_purpose": score_field_label(score_field),
            "top_n": top_n,
            "borrow_candidate_rows": len(rows),
            "unique_tickers": len({normalize_ticker(row.get("ticker")) for row in rows if normalize_ticker(row.get("ticker"))}),
            "asof_dates": len({str(row.get("asof_date") or "") for row in rows if str(row.get("asof_date") or "")}),
            "median_gap_to_cutoff": "" if not gaps else round(median(gaps), 4),
            "mean_gap_to_cutoff": "" if not gaps else round(mean(gaps), 4),
            "min_gap_to_cutoff": "" if not gaps else round(min(gaps), 4),
            "p90_gap_to_cutoff": "",
        }
        if gaps:
            ordered = sorted(gaps)
            base["p90_gap_to_cutoff"] = round(ordered[min(len(ordered) - 1, int(0.90 * (len(ordered) - 1)))], 4)
        for bonus in bonuses:
            closes = sum(1 for row in rows if (to_float(row.get(f"bonus_{bonus:g}_closes_gap"), 0.0) or 0.0) > 0.0)
            base[f"bonus_{bonus:g}_gap_close_rows"] = closes
            base[f"bonus_{bonus:g}_gap_close_rate_pct"] = "" if not rows else round(100.0 * closes / len(rows), 4)
        out.append(base)
    return out


def main() -> None:
    args = parse_args()
    calibration = load_calibration_module()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    base_dir = config_path.parent
    db_path = args.db.expanduser().resolve() if args.db else resolve_path(cfg_get(config, "paths.database_path"), base_dir=base_dir)
    output_dir = args.output_dir.expanduser().resolve()
    horizons = parse_int_list(args.horizons, [20, 60, 120])
    top_ns = parse_int_list(args.top_n, DEFAULT_TOP_N)
    bonuses = parse_float_list(args.bonus_points, DEFAULT_BONUSES)
    score_fields = score_field_list(args.score_fields)
    validation_cfg = cfg_get(config, "biotech_reports.borrow_availability_validation", {}) or {}
    if not isinstance(validation_cfg, dict):
        validation_cfg = {}
    thresholds = {
        "elevated_borrow_pressure_min": float(
            args.elevated_borrow_pressure_min
            if args.elevated_borrow_pressure_min is not None
            else validation_cfg.get("elevated_borrow_pressure_min", 30.0)
        ),
        "high_borrow_pressure_min": float(
            args.high_borrow_pressure_min
            if args.high_borrow_pressure_min is not None
            else validation_cfg.get("high_borrow_pressure_min", 60.0)
        ),
        "high_borrow_rate_min": float(
            args.high_borrow_rate_min
            if args.high_borrow_rate_min is not None
            else validation_cfg.get("high_borrow_rate_min", 0.15)
        ),
    }
    params = calibration.load_calibration_params(config)
    strict_feature_lag = (
        bool(args.strict_feature_lag)
        if args.strict_feature_lag is not None
        else calibration.as_bool(cfg_get(config, "calibration.tier1.strict_feature_lag", True), True)
    )
    next_bar_entry = (
        bool(args.next_bar_entry)
        if args.next_bar_entry is not None
        else calibration.as_bool(cfg_get(config, "calibration.tier1.next_bar_entry", True), True)
    )
    market_sources_raw = args.market_sources if str(args.market_sources or "").strip() else None
    market_sources = [
        str(source).strip()
        for source in normalize_string_list(market_sources_raw, calibration_market_sources(config))
        if str(source).strip()
    ]
    if not market_sources:
        raise ValueError("No market sources configured for forward-return lookup.")

    with connect(db_path, timeout_sec=float(cfg_get(config, "runtime.sqlite_timeout_sec", 30.0))) as conn:
        snapshot_dates = calibration.load_snapshot_dates(
            conn,
            start_asof=parse_optional_date(args.start_asof),
            end_asof=parse_optional_date(args.end_asof),
            fridays_only=not bool(args.include_non_fridays),
            max_snapshots=max(0, int(args.max_snapshots)),
        )
        if not snapshot_dates:
            raise ValueError("No snapshot dates found for borrow rank-lift validation.")
        rows = load_score_rows(conn, asof_dates=snapshot_dates, score_fields=score_fields)
        if not rows:
            raise ValueError("No daily_scores rows found for selected snapshot dates.")
        tickers = {normalize_ticker(row.get("ticker")) for row in rows if normalize_ticker(row.get("ticker"))}
        price_ticker_alias = calibration.load_calibration_ticker_alias_map(conn)
        market_tickers = set(tickers)
        for observation_ticker in tickers:
            canonical_price_ticker = price_ticker_alias.get(observation_ticker)
            if canonical_price_ticker:
                market_tickers.add(canonical_price_ticker)
        if params.alpha_adjustment_enabled and params.benchmark_ticker:
            market_tickers.add(params.benchmark_ticker)
        asof_dates = [parsed for row in rows if (parsed := calibration.parse_date(row.get("asof_date"))) is not None]
        if not asof_dates:
            raise ValueError("daily_scores rows have no valid as-of dates.")
        bars_by_ticker = calibration.load_bars(
            conn,
            tickers=market_tickers,
            min_date=min(asof_dates),
            market_sources=market_sources,
        )
        calibration.apply_delisted_price_series_overlay(
            conn,
            bars_by_ticker,
            price_ticker_alias=price_ticker_alias,
            min_date=min(asof_dates),
            config=config,
        )

    calibration.add_forward_returns(
        rows,
        bars_by_ticker,
        horizons,
        round_trip_cost_bps=params.round_trip_cost_bps,
        next_bar_entry=next_bar_entry,
        benchmark_ticker=params.benchmark_ticker if params.alpha_adjustment_enabled else "",
        benchmark_bars=bars_by_ticker.get(params.benchmark_ticker, []) if params.alpha_adjustment_enabled else [],
        price_ticker_alias=price_ticker_alias,
    )

    rows_by_date: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        rows_by_date[str(row.get("asof_date") or "")].append(row)

    gap_rows: list[dict[str, Any]] = []
    simulated_selection_rows: list[dict[str, Any]] = []
    for asof_date, date_rows in sorted(rows_by_date.items()):
        for score_field in score_fields:
            gap_rows.extend(
                rank_gap_rows_for_date(
                    asof_date=asof_date,
                    rows=date_rows,
                    score_field=score_field,
                    top_ns=top_ns,
                    bonuses=bonuses,
                    thresholds=thresholds,
                )
            )
            for top_n in top_ns:
                for bonus in bonuses:
                    simulated_selection_rows.extend(
                        simulate_bonus_for_date(
                            asof_date=asof_date,
                            rows=date_rows,
                            score_field=score_field,
                            top_n=top_n,
                            bonus=bonus,
                            thresholds=thresholds,
                        )
                    )

    simulation_rows: list[dict[str, Any]] = []
    cohort_rows: list[dict[str, Any]] = []
    for horizon in horizons:
        ret_key = calibration.objective_return_key(horizon, params)
        for score_field in score_fields:
            for top_n in top_ns:
                for bonus in bonuses:
                    subset = [
                        row
                        for row in simulated_selection_rows
                        if row.get("score_field") == score_field
                        and int(to_float(row.get("top_n"), 0.0) or 0) == top_n
                        and abs((to_float(row.get("bonus_points"), 0.0) or 0.0) - bonus) < 1e-9
                    ]
                    if args.require_completed_forward_return:
                        subset = completed_return_rows(subset, ret_key)
                    simulation_rows.append(
                        aggregate_rows(
                            subset,
                            horizon=horizon,
                            group_fields={
                                "score_field": score_field,
                                "score_purpose": score_field_label(score_field),
                                "top_n": top_n,
                                "bonus_points": bonus,
                            },
                            ret_key=ret_key,
                            lcb_z=float(params.lcb_z),
                        )
                    )
                    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
                    for row in subset:
                        cohort = str(row.get("biotech_primary_cohort") or "unknown").strip() or "unknown"
                        grouped[cohort].append(row)
                    for cohort, cohort_subset in sorted(grouped.items()):
                        cohort_rows.append(
                            aggregate_rows(
                                cohort_subset,
                                horizon=horizon,
                                group_fields={
                                    "score_field": score_field,
                                    "score_purpose": score_field_label(score_field),
                                    "top_n": top_n,
                                    "bonus_points": bonus,
                                    "biotech_primary_cohort": cohort,
                                },
                                ret_key=ret_key,
                                lcb_z=float(params.lcb_z),
                            )
                        )

    manifest_rows = [
        {
            "db_path": str(db_path),
            "output_dir": str(output_dir),
            "snapshot_dates": len(snapshot_dates),
            "first_snapshot_date": min(snapshot_dates) if snapshot_dates else "",
            "last_snapshot_date": max(snapshot_dates) if snapshot_dates else "",
            "daily_score_rows": len(rows),
            "score_fields": "|".join(score_fields),
            "top_n": "|".join(str(item) for item in top_ns),
            "bonus_points": "|".join(f"{item:g}" for item in bonuses),
            "horizons": "|".join(str(item) for item in horizons),
            "elevated_borrow_pressure_min": thresholds["elevated_borrow_pressure_min"],
            "high_borrow_pressure_min": thresholds["high_borrow_pressure_min"],
            "high_borrow_rate_min": thresholds["high_borrow_rate_min"],
            "strict_feature_lag": 1 if strict_feature_lag else 0,
            "next_bar_entry": 1 if next_bar_entry else 0,
            "return_objective": calibration.return_objective_label(params),
            "round_trip_cost_bps": params.round_trip_cost_bps,
            "gap_rows": len(gap_rows),
            "simulated_selection_rows": len(simulated_selection_rows),
        }
    ]

    write_csv(output_dir / "borrow_rank_lift_manifest.csv", manifest_rows)
    write_csv(output_dir / "borrow_rank_lift_ticker_gaps.csv", gap_rows)
    write_csv(output_dir / "borrow_rank_lift_gap_summary.csv", build_gap_summary(gap_rows, bonuses))
    write_csv(output_dir / "borrow_rank_lift_simulation.csv", simulation_rows)
    write_csv(output_dir / "borrow_rank_lift_by_cohort.csv", cohort_rows)
    write_csv(output_dir / "borrow_rank_lift_simulated_selections.csv", simulated_selection_rows)
    print(f"Wrote borrow rank-lift validation outputs to {output_dir}")


if __name__ == "__main__":
    main()
