#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import logging
from collections import defaultdict
from pathlib import Path

from macro_raw_config import configure_pipeline_logging

logger = logging.getLogger(__name__)

DEFAULT_FEATURE_POLICY = Path(__file__).resolve().with_name("macro_feature_policy.csv")
DEFAULT_OUTPUT = Path(__file__).resolve().with_name("macro_composite_policy.csv")

FIELDNAMES = [
    "composite_key",
    "metric_key",
    "feature_name",
    "ref_area",
    "regime_block",
    "base_weight",
    "required_flag",
    "min_feature_coverage_flag",
    "max_staleness_days_override",
    "source_quality_multiplier",
    "smoothing_window_days",
    "min_composite_coverage_ratio",
    "min_required_coverage_ratio",
    "notes",
]

COMPOSITE_ORDER = ["G_NOW", "G_LEAD", "PI_NOW", "PI_LEAD", "SHOCK", "G_LEAD_V21", "PI_NOW_V21"]
COMPOSITE_FROM_BLOCK = {
    "growth_now": "G_NOW",
    "growth_lead": "G_LEAD",
    "inflation_now": "PI_NOW",
    "inflation_lead": "PI_LEAD",
    "external_shock": "SHOCK",
}
# V2.1 candidate composites (frozen spec: V2_1_CANDIDATE_SPEC.md). Derived from the base
# composite of the same regime block, with membership deltas. The V21-only metrics are
# excluded from base composites so V1/V2 composite rows regenerate byte-identically.
V21_BASE_COMPOSITE = {
    "G_LEAD_V21": "G_LEAD",
    "PI_NOW_V21": "PI_NOW",
}
V21_EXCLUDED_METRICS = {
    "G_LEAD_V21": {"us_nfci", "us_anfci"},
    "PI_NOW_V21": {"us_avg_hourly_earnings"},
}
V21_ADDED_METRICS = {
    "G_LEAD_V21": {"us_ig_spread_baa10y", "us_equity_vol"},
    "PI_NOW_V21": {"us_avg_hourly_earnings_prod"},
}
V21_ONLY_METRICS = frozenset().union(*V21_ADDED_METRICS.values())
SMOOTHING_WINDOW_BY_COMPOSITE = {
    "G_NOW": 5,
    "G_LEAD": 5,
    "PI_NOW": 5,
    "PI_LEAD": 5,
    "SHOCK": 3,
    "G_LEAD_V21": 5,
    "PI_NOW_V21": 5,
}
MIN_COVERAGE_RATIO_BY_COMPOSITE = {
    "G_NOW": 0.40,
    "G_LEAD": 0.40,
    "PI_NOW": 0.50,
    "PI_LEAD": 0.67,
    "SHOCK": 0.50,
    "G_LEAD_V21": 0.40,
    "PI_NOW_V21": 0.50,
}
MIN_REQUIRED_COVERAGE_RATIO_BY_COMPOSITE = {
    "G_NOW": 1.00,
    "G_LEAD": 1.00,
    "PI_NOW": 1.00,
    "PI_LEAD": 1.00,
    "SHOCK": 0.00,
    "G_LEAD_V21": 1.00,
    "PI_NOW_V21": 1.00,
}
REQUIRED_METRICS_BY_COMPOSITE = {
    "G_NOW": {
        "us_ads_index",
        "us_cfnai_ma3",
        "us_nonfarm_payrolls",
        "us_unemployment_rate",
        "us_initial_claims",
        "us_real_gdp",
    },
    "G_LEAD": {
        "us_cli",
        "us_bci",
        "us_cci",
        "us_yield_curve_10y2y",
        "us_hy_oas",
        "us_nfci",
    },
    "PI_NOW": {
        "us_headline_cpi",
        "us_core_cpi",
        "us_headline_pce",
        "us_core_pce",
        "us_avg_hourly_earnings",
    },
    "PI_LEAD": {
        "us_5y_breakeven",
        "us_5y5y_forward_inflation",
        "us_10y_real_yield",
    },
    "SHOCK": set(),
    "G_LEAD_V21": {
        "us_cli",
        "us_bci",
        "us_cci",
        "us_yield_curve_10y2y",
        "us_hy_oas",
        "us_ig_spread_baa10y",
        "us_equity_vol",
    },
    "PI_NOW_V21": {
        "us_headline_cpi",
        "us_core_cpi",
        "us_headline_pce",
        "us_core_pce",
        "us_avg_hourly_earnings_prod",
    },
}


def _validate_composite_configuration() -> None:
    expected = set(COMPOSITE_ORDER)
    keyed_maps = {
        "SMOOTHING_WINDOW_BY_COMPOSITE": set(SMOOTHING_WINDOW_BY_COMPOSITE),
        "MIN_COVERAGE_RATIO_BY_COMPOSITE": set(MIN_COVERAGE_RATIO_BY_COMPOSITE),
        "MIN_REQUIRED_COVERAGE_RATIO_BY_COMPOSITE": set(MIN_REQUIRED_COVERAGE_RATIO_BY_COMPOSITE),
        "REQUIRED_METRICS_BY_COMPOSITE": set(REQUIRED_METRICS_BY_COMPOSITE),
    }
    for label, keys in keyed_maps.items():
        if keys != expected:
            raise ValueError(
                f"Composite configuration mismatch for {label}: expected keys={sorted(expected)} actual_keys={sorted(keys)}"
            )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build the tier-1 macro composite policy file from the feature policy.")
    parser.add_argument("--feature-policy", type=Path, default=DEFAULT_FEATURE_POLICY)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    return parser.parse_args()


def _read_rows(path: Path) -> list[dict[str, str]]:
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        return [dict(row) for row in csv.DictReader(f)]


def _composite_key(row: dict[str, str]) -> str | None:
    metric_key = str(row.get("metric_key", "") or "").strip()
    regime_block = str(row.get("regime_block", "") or "").strip()
    if not metric_key or not regime_block:
        return None
    if metric_key in V21_ONLY_METRICS:
        # V2.1-only components never join the base composites (keeps V1/V2 rows identical);
        # they are routed into their V21 composite in build_rows.
        return None
    composite_key = COMPOSITE_FROM_BLOCK.get(regime_block)
    if composite_key is None:
        return None
    if composite_key == "SHOCK":
        if metric_key.startswith("us_") or metric_key.startswith("global_"):
            return composite_key
        return None
    if metric_key.startswith("us_"):
        return composite_key
    return None


def build_rows(feature_rows: list[dict[str, str]]) -> list[dict[str, str]]:
    _validate_composite_configuration()
    grouped: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in feature_rows:
        composite_key = _composite_key(row)
        if composite_key is None:
            continue
        grouped[composite_key].append(row)

    # V2.1 candidate composites: base membership minus exclusions, plus the V21-only
    # metrics (whose feature rows were withheld from base grouping above).
    feature_by_metric: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in feature_rows:
        feature_by_metric[str(row.get("metric_key", "") or "").strip()].append(row)
    for v21_key, base_key in V21_BASE_COMPOSITE.items():
        members = [
            row for row in grouped.get(base_key, [])
            if str(row.get("metric_key", "") or "").strip() not in V21_EXCLUDED_METRICS[v21_key]
        ]
        missing_added = []
        for metric_key in sorted(V21_ADDED_METRICS[v21_key]):
            added_rows = feature_by_metric.get(metric_key, [])
            if not added_rows:
                missing_added.append(metric_key)
                continue
            members.extend(added_rows)
        if missing_added:
            raise ValueError(
                f"V2.1 composite {v21_key} is missing feature-policy rows for required added metrics: {missing_added}. "
                "Register the metrics and rebuild the feature policy first."
            )
        grouped[v21_key] = members

    out: list[dict[str, str]] = []
    for composite_key in COMPOSITE_ORDER:
        rows = grouped.get(composite_key, [])
        if not rows:
            continue
        base_weight = 1.0 / float(len(rows))
        for row in sorted(rows, key=lambda item: str(item.get("metric_key", "") or "")):
            metric_key = str(row.get("metric_key", "") or "").strip()
            out.append(
                {
                    "composite_key": composite_key,
                    "metric_key": metric_key,
                    "feature_name": str(row.get("feature_name", "") or "").strip(),
                    "ref_area": str(row.get("ref_area", "") or "").strip(),
                    "regime_block": str(row.get("regime_block", "") or "").strip(),
                    "base_weight": f"{base_weight:.8f}",
                    "required_flag": "1" if metric_key in REQUIRED_METRICS_BY_COMPOSITE.get(composite_key, set()) else "0",
                    "min_feature_coverage_flag": "1",
                    "max_staleness_days_override": "",
                    "source_quality_multiplier": "1.00",
                    "smoothing_window_days": str(SMOOTHING_WINDOW_BY_COMPOSITE[composite_key]),
                    "min_composite_coverage_ratio": f"{MIN_COVERAGE_RATIO_BY_COMPOSITE[composite_key]:.2f}",
                    "min_required_coverage_ratio": f"{MIN_REQUIRED_COVERAGE_RATIO_BY_COMPOSITE[composite_key]:.2f}",
                    "notes": "Generated tier-1 composite defaults from the feature policy. Core regime composites use U.S. features only; SHOCK uses U.S./global shock features.",
                }
            )
    return out


def write_rows(path: Path, rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f"{path.name}.tmp")
    with open(tmp_path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
        writer.writeheader()
        writer.writerows(rows)
    tmp_path.replace(path)


def main() -> None:
    configure_pipeline_logging()
    args = parse_args()
    rows = build_rows(_read_rows(args.feature_policy))
    write_rows(args.output, rows)
    logger.info("Wrote %d composite policy rows to: %s", len(rows), args.output)


if __name__ == "__main__":
    main()
