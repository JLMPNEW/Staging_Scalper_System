#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parents[2]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from industrials.core.logging_utils import configure_utc_logging  # noqa: E402
from industrials.core.reports import write_csv_atomic  # noqa: E402
from industrials.defense.research_artifacts import (  # noqa: E402
    DEFAULT_FORWARD_DAYS,
    DEFAULT_PILLAR_WEIGHTS,
    MODEL_FAMILY,
    as_float,
    as_int,
    command_line,
    fmt,
    forward_window_calendar_days,
    max_drawdown,
    mean,
    normalize_weights,
    parse_date,
    read_csv_rows,
    sha256_file,
    stdev,
    utc_now,
    weighted_score,
    write_json_atomic,
)


PERIOD_FIELDS = [
    "asof_date",
    "split_name",
    "universe_count",
    "selected_count",
    "score_field",
    "score_cutoff",
    "selected_avg_score",
    "universe_avg_score",
    "selected_forward_return",
    "universe_forward_return",
    "benchmark_forward_return",
    "selected_excess_vs_benchmark",
    "selected_excess_vs_universe",
    "selected_tickers",
]
SUMMARY_FIELDS = [
    "status",
    "panel_rows",
    "eligible_rows",
    "period_count",
    "top_quantile",
    "min_positions",
    "score_source",
    "forward_days",
    "min_snapshot_gap_days",
    "overlapping_forward_windows_flag",
    "selected_mean_forward_return",
    "selected_mean_excess_vs_benchmark",
    "selected_mean_excess_vs_universe",
    "selected_hit_rate_vs_benchmark",
    "selected_hit_rate_vs_universe",
    "selected_stdev_excess_vs_benchmark",
    "selected_information_ratio_vs_benchmark",
    "selected_max_drawdown_forward_path",
    "holdout_period_count",
    "holdout_mean_excess_vs_benchmark",
    "promotable",
    "reason",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Backtest defense shadow/calibrated scores on the Stage 8 PIT research panel.")
    default_panel = PROJECT_ROOT / "output" / "industrials" / "defense" / "stage8" / "oos_calibration_panel" / "defense_oos_calibration_panel.csv"
    default_calibration = PROJECT_ROOT / "output" / "industrials" / "defense" / "stage8" / "optuna_calibration" / "defense_optuna_calibration_summary.csv"
    default_output = PROJECT_ROOT / "output" / "industrials" / "defense" / "stage9" / "score_backtest"
    parser.add_argument("--panel-csv", type=Path, default=default_panel)
    parser.add_argument("--calibration-summary-csv", type=Path, default=default_calibration)
    parser.add_argument("--output-dir", type=Path, default=default_output)
    parser.add_argument("--top-quantile", type=float, default=0.20)
    parser.add_argument("--min-positions", type=int, default=5)
    parser.add_argument("--min-periods", type=int, default=12)
    parser.add_argument("--allow-overwrite", action="store_true")
    return parser.parse_args()


def load_calibrated_weights(path: Path) -> tuple[str, dict[str, float]]:
    if not path.exists():
        return "native_final_score", {}
    rows = read_csv_rows(path)
    if not rows:
        return "native_final_score", {}
    raw = str(rows[0].get("best_weights_json") or "").strip()
    if not raw:
        return "native_final_score", {}
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return "native_final_score", {}
    if not isinstance(payload, dict):
        return "native_final_score", {}
    weights = {str(key): float(value) for key, value in payload.items() if as_float(value) is not None}
    return "stage8_report_only_weighted_pillar_score", normalize_weights(weights)


def row_score(row: dict[str, str], weights: dict[str, float]) -> float | None:
    if weights:
        return weighted_score(row, weights)
    return as_float(row.get("final_score"))


def eligible_rows(rows: list[dict[str, str]], weights: dict[str, float]) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    for row in rows:
        if str(row.get("panel_row_eligible_flag") or "") != "1":
            continue
        if as_float(row.get("forward_return")) is None:
            continue
        if as_float(row.get("forward_excess_return_vs_sector")) is None:
            continue
        if row_score(row, weights) is None:
            continue
        out.append(row)
    return out


def by_asof(rows: list[dict[str, str]]) -> dict[str, list[dict[str, str]]]:
    grouped: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in rows:
        grouped[str(row.get("asof_date") or "")].append(row)
    return dict(grouped)


def average_field(rows: list[dict[str, str]], field: str) -> float | None:
    return mean(value for row in rows if (value := as_float(row.get(field))) is not None)


def build_period_rows(
    rows: list[dict[str, str]],
    *,
    score_source: str,
    weights: dict[str, float],
    top_quantile: float,
    min_positions: int,
) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    for asof, members in sorted(by_asof(rows).items()):
        scored = [(row, row_score(row, weights)) for row in members]
        clean = [(row, score) for row, score in scored if score is not None]
        clean.sort(key=lambda item: (-float(item[1] or 0.0), str(item[0].get("ticker") or "")))
        if not clean:
            continue
        selected_count = min(len(clean), max(min_positions, int(math.ceil(len(clean) * top_quantile))))
        selected = clean[:selected_count]
        selected_rows = [row for row, _ in selected]
        universe_rows = [row for row, _ in clean]
        selected_forward = average_field(selected_rows, "forward_return")
        universe_forward = average_field(universe_rows, "forward_return")
        benchmark_forward = average_field(universe_rows, "benchmark_forward_return")
        selected_excess_benchmark = (
            selected_forward - benchmark_forward
            if selected_forward is not None and benchmark_forward is not None
            else None
        )
        selected_excess_universe = (
            selected_forward - universe_forward
            if selected_forward is not None and universe_forward is not None
            else None
        )
        out.append(
            {
                "asof_date": asof,
                "split_name": ";".join(sorted({str(row.get("split_name") or "") for row in universe_rows})),
                "universe_count": str(len(universe_rows)),
                "selected_count": str(len(selected_rows)),
                "score_field": score_source,
                "score_cutoff": fmt(selected[-1][1]),
                "selected_avg_score": fmt(mean(float(score or 0.0) for _, score in selected)),
                "universe_avg_score": fmt(mean(float(score or 0.0) for _, score in clean)),
                "selected_forward_return": fmt(selected_forward, 10),
                "universe_forward_return": fmt(universe_forward, 10),
                "benchmark_forward_return": fmt(benchmark_forward, 10),
                "selected_excess_vs_benchmark": fmt(selected_excess_benchmark, 10),
                "selected_excess_vs_universe": fmt(selected_excess_universe, 10),
                "selected_tickers": ";".join(str(row.get("ticker") or "") for row in selected_rows),
            }
        )
    return out


def date_gap_days(period_rows: list[dict[str, str]]) -> int | None:
    dates = [parse_date(row.get("asof_date"), field="asof_date") for row in period_rows]
    clean: list[date] = [item for item in dates if item is not None]
    if len(clean) < 2:
        return None
    return min((right - left).days for left, right in zip(clean, clean[1:]))


def build_summary(
    *,
    all_rows: list[dict[str, str]],
    eligible_count: int,
    period_rows: list[dict[str, str]],
    top_quantile: float,
    min_positions: int,
    min_periods: int,
    score_source: str,
    forward_days: int,
    min_gap_days: int | None,
    overlapping: bool,
) -> dict[str, str]:
    selected_excess_benchmark = [
        value for row in period_rows if (value := as_float(row.get("selected_excess_vs_benchmark"))) is not None
    ]
    selected_excess_universe = [
        value for row in period_rows if (value := as_float(row.get("selected_excess_vs_universe"))) is not None
    ]
    selected_forward = [
        value for row in period_rows if (value := as_float(row.get("selected_forward_return"))) is not None
    ]
    holdout_excess_benchmark = [
        value
        for row in period_rows
        if str(row.get("split_name") or "") == "holdout"
        and (value := as_float(row.get("selected_excess_vs_benchmark"))) is not None
    ]
    excess_stdev = stdev(selected_excess_benchmark)
    excess_mean = mean(selected_excess_benchmark)
    info_ratio = excess_mean / excess_stdev if excess_mean is not None and excess_stdev not in (None, 0.0) else None
    # Compounding overlapping forward windows into one equity path is
    # meaningless (each ~forward_days-long return would be chained as if
    # sequential). Only report the drawdown when snapshots are spaced at
    # least one full forward window apart.
    drawdown = "" if overlapping else fmt(max_drawdown(selected_forward), 10)
    insufficient = len(period_rows) < min_periods
    reason_parts: list[str] = []
    if insufficient:
        reason_parts.append(f"insufficient_backtest_periods {len(period_rows)}/{min_periods}")
    else:
        reason_parts.append("report_only_shadow_backtest_requires_manual_review")
    if overlapping:
        reason_parts.append("overlapping_forward_windows_autocorrelated_periods_drawdown_suppressed")
    return {
        "status": "insufficient_data" if insufficient else "report_only_backtest_complete",
        "panel_rows": str(len(all_rows)),
        "eligible_rows": str(eligible_count),
        "period_count": str(len(period_rows)),
        "top_quantile": fmt(top_quantile),
        "min_positions": str(min_positions),
        "score_source": score_source,
        "forward_days": str(forward_days),
        "min_snapshot_gap_days": "" if min_gap_days is None else str(min_gap_days),
        "overlapping_forward_windows_flag": "1" if overlapping else "0",
        "selected_mean_forward_return": fmt(mean(selected_forward), 10),
        "selected_mean_excess_vs_benchmark": fmt(excess_mean, 10),
        "selected_mean_excess_vs_universe": fmt(mean(selected_excess_universe), 10),
        "selected_hit_rate_vs_benchmark": fmt(mean([1.0 if value > 0 else 0.0 for value in selected_excess_benchmark]), 6),
        "selected_hit_rate_vs_universe": fmt(mean([1.0 if value > 0 else 0.0 for value in selected_excess_universe]), 6),
        "selected_stdev_excess_vs_benchmark": fmt(excess_stdev, 10),
        "selected_information_ratio_vs_benchmark": fmt(info_ratio, 10),
        "selected_max_drawdown_forward_path": drawdown,
        "holdout_period_count": str(len(holdout_excess_benchmark)),
        "holdout_mean_excess_vs_benchmark": fmt(mean(holdout_excess_benchmark), 10),
        "promotable": "0",
        "reason": ";".join(reason_parts),
    }


def valid_existing(output_dir: Path) -> bool:
    periods = output_dir / "defense_score_backtest_periods.csv"
    summary = output_dir / "defense_score_backtest_summary.csv"
    manifest = output_dir / "defense_score_backtest_manifest.json"
    if not periods.exists() or not summary.exists() or not manifest.exists():
        return False
    try:
        payload = json.loads(manifest.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return False
    files = payload.get("files")
    if not isinstance(files, dict):
        return False
    for path in [periods, summary]:
        meta = files.get(path.name)
        if not isinstance(meta, dict) or meta.get("sha256") != sha256_file(path):
            return False
    return True


def main() -> int:
    configure_utc_logging()
    args = parse_args()
    if not 0.0 < args.top_quantile <= 1.0:
        raise ValueError("--top-quantile must be > 0 and <= 1")
    if args.min_positions <= 0 or args.min_periods < 0:
        raise ValueError("--min-positions must be positive and --min-periods cannot be negative")
    panel_csv = args.panel_csv.expanduser().resolve()
    calibration_summary = args.calibration_summary_csv.expanduser().resolve()
    output_dir = args.output_dir.expanduser().resolve()
    if not panel_csv.exists():
        raise FileNotFoundError(panel_csv)
    artifact_names = [
        "defense_score_backtest_periods.csv",
        "defense_score_backtest_summary.csv",
        "defense_score_backtest_manifest.json",
    ]
    # Guard on the artifact files, not bare directory existence: an empty
    # directory left by a crashed run must not block a rebuild.
    if any((output_dir / name).exists() for name in artifact_names) and not args.allow_overwrite:
        if valid_existing(output_dir):
            print(f"Existing sealed score backtest artifacts are valid; keeping {output_dir}")
            return 0
        raise FileExistsError(f"Refusing to overwrite existing backtest artifacts under {output_dir}; use --allow-overwrite")
    all_rows = read_csv_rows(panel_csv)
    score_source, weights = load_calibrated_weights(calibration_summary)
    if not weights:
        weights = dict(DEFAULT_PILLAR_WEIGHTS)
        score_source = "native_final_score"
    rows = eligible_rows(all_rows, weights if score_source != "native_final_score" else {})
    period_rows = build_period_rows(
        rows,
        score_source=score_source,
        weights=weights if score_source != "native_final_score" else {},
        top_quantile=args.top_quantile,
        min_positions=args.min_positions,
    )
    # Forward horizon comes from the panel itself; falls back to the default
    # only when the panel carries no usable forward_days value.
    panel_forward_days = next(
        (as_int(row.get("forward_days"), 0) for row in rows if as_int(row.get("forward_days"), 0) > 0),
        DEFAULT_FORWARD_DAYS,
    )
    min_gap = date_gap_days(period_rows)
    overlap_threshold = forward_window_calendar_days(panel_forward_days, 0)
    overlapping = min_gap is not None and min_gap < overlap_threshold
    summary_row = build_summary(
        all_rows=all_rows,
        eligible_count=len(rows),
        period_rows=period_rows,
        top_quantile=args.top_quantile,
        min_positions=args.min_positions,
        min_periods=args.min_periods,
        score_source=score_source,
        forward_days=panel_forward_days,
        min_gap_days=min_gap,
        overlapping=overlapping,
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    period_path = output_dir / "defense_score_backtest_periods.csv"
    summary_path = output_dir / "defense_score_backtest_summary.csv"
    manifest_path = output_dir / "defense_score_backtest_manifest.json"
    write_csv_atomic(period_path, PERIOD_FIELDS, period_rows)
    write_csv_atomic(summary_path, SUMMARY_FIELDS, [summary_row])
    manifest = {
        "artifact_family": "defense_score_backtest",
        "model_family": MODEL_FAMILY,
        "created_at_utc": utc_now(),
        "generator": "25_backtest_defense_scores.py",
        "command": command_line(),
        "panel_csv": str(panel_csv),
        "panel_sha256": sha256_file(panel_csv),
        "calibration_summary_csv": str(calibration_summary) if calibration_summary.exists() else "",
        "score_source": score_source,
        "weights": weights if score_source != "native_final_score" else {},
        "top_quantile": args.top_quantile,
        "min_positions": args.min_positions,
        "min_periods": args.min_periods,
        "forward_days": panel_forward_days,
        "min_snapshot_gap_days": min_gap,
        "overlap_warning_flag": 1 if overlapping else 0,
        "promotable": False,
        "promotion_blockers": ["report_only_shadow_backtest", "requires_validated_pit_oos_panel"],
        "files": {
            period_path.name: {"path": str(period_path), "sha256": sha256_file(period_path), "rows": len(period_rows)},
            summary_path.name: {"path": str(summary_path), "sha256": sha256_file(summary_path), "rows": 1},
        },
    }
    write_json_atomic(manifest_path, manifest)
    print(
        f"Backtest report: status={summary_row['status']} periods={summary_row['period_count']} "
        f"eligible_rows={summary_row['eligible_rows']}"
    )
    print(f"Wrote {output_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
