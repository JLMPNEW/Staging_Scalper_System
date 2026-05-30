#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import math
import re
import sys
from pathlib import Path
from statistics import mean, median
from typing import Any


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from med_devices.core.config import cfg_get, load_yaml, resolve_path  # noqa: E402
from med_devices.core.logging_utils import configure_utc_logging  # noqa: E402


DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
OUTPUT_FIELDS = [
    "calibration_type",
    "segment",
    "horizon_days",
    "count",
    "mean_forward_return",
    "median_forward_return",
    "hit_rate",
    "recommendation",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Summarize med-device score backtest output for gate calibration.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--input-csv", type=Path, default=None)
    parser.add_argument("--output-csv", type=Path, default=None)
    parser.add_argument("--asof", type=str, default="")
    parser.add_argument("--thresholds", type=str, default="60,65,70,75")
    return parser.parse_args()


def to_float(raw: object) -> float | None:
    try:
        value = float(str(raw).strip())
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def return_horizons(rows: list[dict[str, str]]) -> list[int]:
    if not rows:
        return []
    out: list[int] = []
    for key in rows[0]:
        if key.startswith("forward_return_") and key.endswith("d"):
            text = key[len("forward_return_") : -1]
            if text.isdigit():
                out.append(int(text))
    return sorted(out)


def stats_for(rows: list[dict[str, str]], *, horizon: int) -> tuple[int, str, str, str]:
    values: list[float] = []
    for row in rows:
        value = to_float(row.get(f"forward_return_{horizon}d"))
        if value is not None:
            values.append(value)
    if not values:
        return 0, "", "", ""
    return (
        len(values),
        f"{mean(values):.6f}",
        f"{median(values):.6f}",
        f"{sum(1 for value in values if value > 0) / len(values):.4f}",
    )


def recommendation(count: int, mean_return: str, *, min_count: int = 20) -> str:
    if count < min_count:
        return "insufficient_forward_return_observations"
    value = to_float(mean_return)
    if value is None:
        return "insufficient_forward_return_observations"
    if value > 0.02:
        return "positive_signal_keep_or_tighten"
    if value > 0.0:
        return "weak_positive_monitor"
    return "negative_signal_review_gate_or_weight"


def calibrate(rows: list[dict[str, str]], *, thresholds: list[float]) -> list[dict[str, Any]]:
    horizons = return_horizons(rows)
    out: list[dict[str, Any]] = []
    for horizon in horizons:
        for classification in sorted({row.get("classification", "") for row in rows}):
            segment_rows = [row for row in rows if row.get("classification", "") == classification]
            count, avg, med, hit = stats_for(segment_rows, horizon=horizon)
            out.append(
                {
                    "calibration_type": "classification",
                    "segment": classification,
                    "horizon_days": horizon,
                    "count": count,
                    "mean_forward_return": avg,
                    "median_forward_return": med,
                    "hit_rate": hit,
                    "recommendation": recommendation(count, avg),
                }
            )
        for entry_status in sorted({row.get("entry_status", "") for row in rows}):
            segment_rows = [row for row in rows if row.get("entry_status", "") == entry_status]
            count, avg, med, hit = stats_for(segment_rows, horizon=horizon)
            out.append(
                {
                    "calibration_type": "entry_status",
                    "segment": entry_status,
                    "horizon_days": horizon,
                    "count": count,
                    "mean_forward_return": avg,
                    "median_forward_return": med,
                    "hit_rate": hit,
                    "recommendation": recommendation(count, avg),
                }
            )
        for threshold in thresholds:
            segment_rows = [
                row
                for row in rows
                if (
                    to_float(row.get("raw_composite_score") or row.get("composite_score")) is not None
                    and float(row.get("raw_composite_score") or row["composite_score"]) >= threshold
                )
            ]
            count, avg, med, hit = stats_for(segment_rows, horizon=horizon)
            out.append(
                {
                    "calibration_type": "composite_threshold",
                    "segment": f"raw_composite_score>={threshold:g}",
                    "horizon_days": horizon,
                    "count": count,
                    "mean_forward_return": avg,
                    "median_forward_return": med,
                    "hit_rate": hit,
                    "recommendation": recommendation(count, avg),
                }
            )
    return out


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=OUTPUT_FIELDS, extrasaction="ignore", lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


DATE_DIR_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def dated_report_dir(base_output_dir: Path, asof: str) -> Path:
    return base_output_dir if base_output_dir.name == asof else base_output_dir / asof


def latest_dated_report_dir(base_output_dir: Path) -> Path | None:
    if not base_output_dir.exists():
        return None
    candidates = [path for path in base_output_dir.iterdir() if path.is_dir() and DATE_DIR_RE.fullmatch(path.name)]
    return max(candidates, key=lambda path: path.name) if candidates else None


def main() -> None:
    configure_utc_logging()
    args = parse_args()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    base_dir = config_path.parent
    report_base_dir = resolve_path(
        cfg_get(config, "scoring.review_pack_dir", "../output/med_devices_reports/score_review_pack"),
        base_dir=base_dir,
    )
    if args.input_csv:
        input_csv = args.input_csv.expanduser().resolve()
    elif args.asof.strip():
        input_csv = dated_report_dir(report_base_dir, args.asof.strip()) / "med_device_score_backtest.csv"
    else:
        latest_dir = latest_dated_report_dir(report_base_dir)
        input_csv = (
            latest_dir / "med_device_score_backtest.csv"
            if latest_dir is not None
            else resolve_path(
                cfg_get(config, "scoring.backtest_output_csv", "../output/med_devices_reports/med_device_score_backtest.csv"),
                base_dir=base_dir,
            )
        )
    output_csv = args.output_csv.expanduser().resolve() if args.output_csv else input_csv.with_name("med_device_score_calibration.csv")
    thresholds = [float(item.strip()) for item in str(args.thresholds or "60,65,70,75").split(",") if item.strip()]
    rows = read_csv(input_csv)
    calibration_rows = calibrate(rows, thresholds=thresholds)
    write_csv(output_csv, calibration_rows)
    print(f"calibration_csv={output_csv} rows={len(calibration_rows)} source={input_csv}")


if __name__ == "__main__":
    main()
