#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import math
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any


PACKAGE_ROOT = Path(__file__).resolve().parents[2]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from technology.core.measurement_diagnostics import (  # noqa: E402
    _newey_west_t,
    _nw_lags,
    _spearman,
)
from technology.software_infrastructure.software_parser_hydration import (  # noqa: E402
    atomic_csv,
    atomic_json,
)


DEFAULT_PANEL = (
    PROJECT_ROOT
    / "output"
    / "technology_reports"
    / "software_infrastructure"
    / "signal_diagnostics"
    / "signal_panel.csv"
)
DEFAULT_OUTPUT = (
    PROJECT_ROOT
    / "output"
    / "technology_reports"
    / "software_infrastructure"
    / "arr_historical_research"
    / "2026-07-30"
    / "sensitivity"
)
SIGNALS = (
    "annual_recurring_revenue_to_revenue",
    "annual_recurring_revenue_yoy_growth",
)
HORIZONS = (21, 63)
SENSITIVITY_MINIMUMS = (5, 8, 10)
LOCKED_MINIMUM = 30


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Run explicitly non-promotable low-coverage ARR IC sensitivity."
        )
    )
    parser.add_argument("--panel", type=Path, default=DEFAULT_PANEL)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    return parser.parse_args()


def _float(value: object) -> float | None:
    try:
        result = float(str(value or ""))
    except ValueError:
        return None
    return result if math.isfinite(result) else None


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _round(value: float | None, digits: int = 4) -> float | str:
    return "" if value is None else round(value, digits)


def _spread(values: list[float], returns: list[float]) -> float | None:
    if len(values) < 5 or len(values) != len(returns):
        return None
    order = sorted(range(len(values)), key=lambda index: values[index])
    width = max(1, len(order) // 5)
    bottom = [returns[index] for index in order[:width]]
    top = [returns[index] for index in order[-width:]]
    top_mean = _mean(top)
    bottom_mean = _mean(bottom)
    if top_mean is None or bottom_mean is None:
        return None
    return top_mean - bottom_mean


def main() -> int:
    args = parse_args()
    panel_path = args.panel.expanduser().resolve()
    with panel_path.open("r", encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    by_date: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in rows:
        by_date[str(row["asof_date"])].append(row)
    detail: list[dict[str, Any]] = []
    summary: list[dict[str, Any]] = []
    for signal in SIGNALS:
        max_cross_section = 0
        for horizon in HORIZONS:
            date_observations: list[dict[str, Any]] = []
            for asof, date_rows in sorted(by_date.items()):
                pairs = [
                    (signal_value, forward_value)
                    for row in date_rows
                    if (
                        signal_value := _float(row.get(signal))
                    ) is not None
                    and (
                        forward_value := _float(
                            row.get(f"fwd_resid_{horizon}d")
                        )
                    ) is not None
                ]
                max_cross_section = max(max_cross_section, len(pairs))
                if len(pairs) < min(SENSITIVITY_MINIMUMS):
                    continue
                values = [pair[0] for pair in pairs]
                returns = [pair[1] for pair in pairs]
                ic = _spearman(values, returns)
                spread = _spread(values, returns)
                benchmark = _float(
                    date_rows[0].get("benchmark_trailing_252d")
                )
                regime = (
                    "benchmark_up"
                    if benchmark is not None and benchmark >= 0
                    else "benchmark_down"
                    if benchmark is not None
                    else "unknown"
                )
                date_observations.append(
                    {
                        "asof_date": asof,
                        "coverage": len(pairs),
                        "spearman_ic": ic,
                        "top_minus_bottom_spread": spread,
                        "regime": regime,
                    }
                )
            for minimum in SENSITIVITY_MINIMUMS:
                selected = [
                    row
                    for row in date_observations
                    if int(row["coverage"]) >= minimum
                ]
                ics = [
                    float(row["spearman_ic"])
                    for row in selected
                    if row["spearman_ic"] is not None
                ]
                spreads = [
                    float(row["top_minus_bottom_spread"])
                    for row in selected
                    if row["top_minus_bottom_spread"] is not None
                ]
                lags = _nw_lags(horizon, 21)
                summary.append(
                    {
                        "signal": signal,
                        "horizon_days": horizon,
                        "minimum_cross_section": minimum,
                        "n_dates": len(ics),
                        "avg_cross_section": _round(
                            _mean(
                                [float(row["coverage"]) for row in selected]
                            ),
                            2,
                        ),
                        "mean_spearman_ic": _round(_mean(ics)),
                        "newey_west_ic_t_stat": _round(
                            _newey_west_t(ics, lags), 2
                        ),
                        "positive_ic_hit_rate": _round(
                            _mean([float(value > 0) for value in ics]), 3
                        ),
                        "mean_top_minus_bottom_spread": _round(
                            _mean(spreads)
                        ),
                        "newey_west_spread_t_stat": _round(
                            _newey_west_t(spreads, lags), 2
                        ),
                        "locked_minimum_cross_section": LOCKED_MINIMUM,
                        "locked_gate_pass_flag": 0,
                        "predictive_claim_authorized_flag": 0,
                        "production_weight": 0.0,
                    }
                )
                for row in selected:
                    detail.append(
                        {
                            "signal": signal,
                            "horizon_days": horizon,
                            "minimum_cross_section": minimum,
                            **row,
                            "predictive_claim_authorized_flag": 0,
                        }
                    )
        summary.append(
            {
                "signal": signal,
                "horizon_days": "coverage",
                "minimum_cross_section": LOCKED_MINIMUM,
                "n_dates": 0,
                "avg_cross_section": "",
                "mean_spearman_ic": "",
                "newey_west_ic_t_stat": "",
                "positive_ic_hit_rate": "",
                "mean_top_minus_bottom_spread": "",
                "newey_west_spread_t_stat": "",
                "locked_minimum_cross_section": LOCKED_MINIMUM,
                "locked_gate_pass_flag": int(
                    max_cross_section >= LOCKED_MINIMUM
                ),
                "predictive_claim_authorized_flag": 0,
                "production_weight": 0.0,
            }
        )
    output_dir = args.output_dir.expanduser().resolve()
    summary_path = output_dir / "software_arr_low_coverage_sensitivity.csv"
    detail_path = output_dir / "software_arr_low_coverage_date_ic.csv"
    manifest_path = output_dir / "software_arr_low_coverage_manifest.json"
    atomic_csv(summary_path, summary)
    atomic_csv(detail_path, detail)
    manifest = {
        "manifest_version": "software_arr_low_coverage_sensitivity_v1",
        "panel_path": str(panel_path),
        "panel_row_count": len(rows),
        "panel_date_count": len(by_date),
        "signals": list(SIGNALS),
        "horizons": list(HORIZONS),
        "sensitivity_minimums": list(SENSITIVITY_MINIMUMS),
        "locked_minimum_cross_section": LOCKED_MINIMUM,
        "locked_gate_pass_flag": 0,
        "predictive_claim_authorized_flag": 0,
        "production_weight_modified_flag": 0,
        "interpretation": (
            "Sensitivity only. No result can promote a signal while the "
            "locked 30-name contemporaneous cross-section gate fails."
        ),
        "summary_path": str(summary_path),
        "detail_path": str(detail_path),
    }
    atomic_json(manifest_path, manifest)
    print(json.dumps(manifest, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
