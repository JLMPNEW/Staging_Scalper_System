#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import math
import sys
from collections import Counter, defaultdict
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
COHORT_FIELDS = [
    "calibration_cohort",
    "unique_tickers",
    "observations_120d",
    "mean_excess_120d",
    "median_excess_120d",
    "hit_rate_excess_120d",
    "lcb_excess_120d",
    "profit_factor_excess_120d",
    "ticker_inventory",
    "business_model_mix",
    "reimbursement_model_mix",
    "regulatory_model_mix",
    "positive_components_120d",
    "weak_or_negative_components_120d",
    "recommended_next_step",
    "rationale",
]
SPLIT_FIELDS = [
    "calibration_cohort",
    "split_dimension",
    "split_value",
    "unique_tickers",
    "observations_120d",
    "median_excess_120d",
    "mean_excess_120d",
    "hit_rate_excess_120d",
    "lcb_excess_120d",
    "profit_factor_excess_120d",
    "ticker_inventory",
    "recommended_action",
]
ACTION_FIELDS = [
    "calibration_cohort",
    "priority",
    "action_type",
    "target",
    "reason",
    "recommended_action",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Audit med-device cohort taxonomy and feature actions after v6 calibration.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--backtest-csv", type=Path, default=None)
    parser.add_argument("--summary-csv", type=Path, default=None)
    parser.add_argument("--ic-csv", type=Path, default=None)
    parser.add_argument("--recommendations-csv", type=Path, default=None)
    parser.add_argument("--cohort-output-csv", type=Path, default=None)
    parser.add_argument("--split-output-csv", type=Path, default=None)
    parser.add_argument("--action-output-csv", type=Path, default=None)
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


def write_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore", lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def pct(value: float | None) -> str:
    return "" if value is None else f"{value:.6f}"


def counter_text(values: list[str]) -> str:
    counts = Counter(value for value in values if value)
    return ";".join(f"{key}:{counts[key]}" for key in sorted(counts))


def metrics(rows: list[dict[str, str]], *, horizon: int = 120) -> dict[str, Any]:
    values: list[float] = []
    tickers: set[str] = set()
    for row in rows:
        value = to_float(row.get(f"cohort_excess_return_{horizon}d"))
        if value is None:
            continue
        values.append(value)
        ticker = str(row.get("ticker") or "")
        if ticker:
            tickers.add(ticker)
    if not values:
        return {
            "unique_tickers": 0,
            "observations_120d": 0,
            "mean_excess_120d": "",
            "median_excess_120d": "",
            "hit_rate_excess_120d": "",
            "lcb_excess_120d": "",
            "profit_factor_excess_120d": "",
        }
    avg = mean(values)
    if len(values) == 1:
        lcb = values[0]
    else:
        variance = sum((value - avg) ** 2 for value in values) / (len(values) - 1)
        lcb = avg - 1.64 * math.sqrt(variance) / math.sqrt(len(values))
    gains = sum(value for value in values if value > 0)
    losses = -sum(value for value in values if value < 0)
    profit_factor = 999.0 if losses <= 1e-12 and gains > 0 else (gains / losses if losses > 1e-12 else 0.0)
    return {
        "unique_tickers": len(tickers),
        "observations_120d": len(values),
        "mean_excess_120d": pct(avg),
        "median_excess_120d": pct(median(values)),
        "hit_rate_excess_120d": pct(sum(1 for value in values if value > 0) / len(values)),
        "lcb_excess_120d": pct(lcb),
        "profit_factor_excess_120d": pct(profit_factor),
    }


def components_for(ic_rows: list[dict[str, str]], cohort: str) -> tuple[str, str]:
    positive: list[str] = []
    weak: list[str] = []
    for row in ic_rows:
        if row.get("calibration_cohort") != cohort or row.get("horizon_days") != "120":
            continue
        component = str(row.get("component") or "")
        recommendation = str(row.get("recommendation") or "")
        if recommendation == "positive_candidate_factor":
            positive.append(component)
        elif recommendation in {"negative_or_inverse_factor", "weak_or_unstable_factor"}:
            weak.append(component)
    return ";".join(positive), ";".join(weak)


def recommendation_for(cohort: str, rows: list[dict[str, str]], recommendation_row: dict[str, str] | None) -> tuple[str, str]:
    tickers = {row.get("ticker", "") for row in rows if row.get("ticker")}
    pass_fail = str((recommendation_row or {}).get("pass_fail") or "")
    if pass_fail == "pass":
        return "maintain_promoted_or_review_gate", "A broad cohort gate passed the current validation constraints."
    if len(tickers) < 8:
        return "collect_more_history_or_merge_parent_cohort", "Cohort has too few tickers for reliable standalone optimization."
    if len(tickers) >= 18:
        return "review_subcohort_split_candidates", "Cohort is broad enough to justify testing exposure-tag splits before more parameter tuning."
    return "improve_features_before_retuning", "No promoted gate passed; inspect feature sleeves and taxonomy before another optimization round."


def build_cohort_audit(
    backtest_rows: list[dict[str, str]],
    summary_rows: list[dict[str, str]],
    ic_rows: list[dict[str, str]],
    recommendation_rows: list[dict[str, str]],
) -> list[dict[str, Any]]:
    by_cohort: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in backtest_rows:
        cohort = str(row.get("calibration_cohort") or "")
        if cohort:
            by_cohort[cohort].append(row)
    rec_by_cohort = {row["calibration_cohort"]: row for row in recommendation_rows if row.get("calibration_cohort")}
    summary_by_cohort = {
        row["segment"]: row
        for row in summary_rows
        if row.get("summary_type") == "calibration_cohort" and row.get("horizon_days") == "120"
    }
    out: list[dict[str, Any]] = []
    for cohort, rows in sorted(by_cohort.items()):
        summary = summary_by_cohort.get(cohort, {})
        positive, weak = components_for(ic_rows, cohort)
        action, rationale = recommendation_for(cohort, rows, rec_by_cohort.get(cohort))
        item = {
            "calibration_cohort": cohort,
            "unique_tickers": len({row.get("ticker", "") for row in rows if row.get("ticker")}),
            "observations_120d": summary.get("count", ""),
            "mean_excess_120d": summary.get("mean_excess_return", ""),
            "median_excess_120d": summary.get("median_excess_return", ""),
            "hit_rate_excess_120d": summary.get("excess_hit_rate", ""),
            "lcb_excess_120d": summary.get("lcb_excess_return", ""),
            "profit_factor_excess_120d": summary.get("profit_factor_excess", ""),
            "ticker_inventory": ";".join(sorted({str(row.get("ticker") or "") for row in rows if row.get("ticker")})),
            "business_model_mix": counter_text([str(row.get("business_model") or "") for row in rows]),
            "reimbursement_model_mix": counter_text([str(row.get("reimbursement_model") or "") for row in rows]),
            "regulatory_model_mix": counter_text([str(row.get("regulatory_model") or "") for row in rows]),
            "positive_components_120d": positive,
            "weak_or_negative_components_120d": weak,
            "recommended_next_step": action,
            "rationale": rationale,
        }
        out.append(item)
    return out


def split_rows(backtest_rows: list[dict[str, str]]) -> list[dict[str, Any]]:
    dimensions = [
        "business_model",
        "reimbursement_model",
        "regulatory_model",
        "capital_equipment_flag",
        "consumables_flag",
        "diagnostics_flag",
        "implantable_flag",
        "single_product_risk_flag",
    ]
    grouped: dict[tuple[str, str, str], list[dict[str, str]]] = defaultdict(list)
    for row in backtest_rows:
        cohort = str(row.get("calibration_cohort") or "")
        if not cohort:
            continue
        for dimension in dimensions:
            value = str(row.get(dimension) or "")
            if value:
                grouped[(cohort, dimension, value)].append(row)
    out: list[dict[str, Any]] = []
    for (cohort, dimension, value), rows in sorted(grouped.items()):
        tickers = sorted({str(row.get("ticker") or "") for row in rows if row.get("ticker")})
        item = {
            "calibration_cohort": cohort,
            "split_dimension": dimension,
            "split_value": value,
            "ticker_inventory": ";".join(tickers),
        }
        item.update(metrics(rows))
        median_excess = to_float(item["median_excess_120d"]) or 0.0
        lcb = to_float(item["lcb_excess_120d"]) or 0.0
        hit_rate = to_float(item["hit_rate_excess_120d"]) or 0.0
        if len(tickers) == 1:
            action = "singleton_merge_review"
        elif len(tickers) >= 5 and median_excess > 0 and lcb > 0 and hit_rate >= 0.52:
            action = "test_as_candidate_subcohort"
        elif len(tickers) >= 5:
            action = "monitor_not_ready"
        else:
            action = "too_small_for_standalone_gate"
        item["recommended_action"] = action
        out.append(item)
    out.sort(
        key=lambda row: (
            row["recommended_action"] == "test_as_candidate_subcohort",
            to_float(row.get("lcb_excess_120d")) or -999.0,
            to_float(row.get("median_excess_120d")) or -999.0,
        ),
        reverse=True,
    )
    return out


def feature_actions(cohort_rows: list[dict[str, Any]], split_candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for row in cohort_rows:
        cohort = str(row["calibration_cohort"])
        weak = str(row.get("weak_or_negative_components_120d") or "")
        positives = str(row.get("positive_components_120d") or "")
        if row["recommended_next_step"] == "review_subcohort_split_candidates":
            candidate_count = sum(
                1
                for item in split_candidates
                if item["calibration_cohort"] == cohort and item["recommended_action"] == "test_as_candidate_subcohort"
            )
            out.append(
                {
                    "calibration_cohort": cohort,
                    "priority": "high" if candidate_count else "medium",
                    "action_type": "taxonomy",
                    "target": "exposure_tag_splits",
                    "reason": f"{candidate_count} candidate subcohort split(s) passed broad metrics." if candidate_count else "Broad cohort with no robust promoted gate.",
                    "recommended_action": "review candidate split rows and manually validate ticker membership.",
                }
            )
        if positives:
            out.append(
                {
                    "calibration_cohort": cohort,
                    "priority": "medium",
                    "action_type": "feature_validation",
                    "target": positives,
                    "reason": "Positive 120d IC/spread candidates exist; validate if they are economically sensible before weight tuning.",
                    "recommended_action": "review sleeve definitions and data quality for listed positive components.",
                }
            )
        if weak:
            out.append(
                {
                    "calibration_cohort": cohort,
                    "priority": "medium",
                    "action_type": "feature_cleanup",
                    "target": weak,
                    "reason": "Weak or negative 120d component behavior; avoid increasing weight until feature definition improves.",
                    "recommended_action": "treat as risk/review signal or improve raw feature engineering.",
                }
            )
    return out


def main() -> None:
    configure_utc_logging()
    args = parse_args()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    base_dir = config_path.parent
    backtest_csv = (
        args.backtest_csv.expanduser().resolve()
        if args.backtest_csv
        else resolve_path(cfg_get(config, "calibration.cohort_neutral_backtest_csv"), base_dir=base_dir)
    )
    summary_csv = (
        args.summary_csv.expanduser().resolve()
        if args.summary_csv
        else resolve_path(cfg_get(config, "calibration.cohort_neutral_summary_csv"), base_dir=base_dir)
    )
    ic_csv = (
        args.ic_csv.expanduser().resolve()
        if args.ic_csv
        else resolve_path(cfg_get(config, "calibration.component_ic_csv"), base_dir=base_dir)
    )
    recommendations_csv = (
        args.recommendations_csv.expanduser().resolve()
        if args.recommendations_csv
        else resolve_path(cfg_get(config, "calibration.recommendations_csv"), base_dir=base_dir)
    )
    cohort_output_csv = (
        args.cohort_output_csv.expanduser().resolve()
        if args.cohort_output_csv
        else resolve_path(cfg_get(config, "calibration.taxonomy_audit.cohort_audit_csv"), base_dir=base_dir)
    )
    split_output_csv = (
        args.split_output_csv.expanduser().resolve()
        if args.split_output_csv
        else resolve_path(cfg_get(config, "calibration.taxonomy_audit.split_candidates_csv"), base_dir=base_dir)
    )
    action_output_csv = (
        args.action_output_csv.expanduser().resolve()
        if args.action_output_csv
        else resolve_path(cfg_get(config, "calibration.taxonomy_audit.feature_action_plan_csv"), base_dir=base_dir)
    )

    backtest_rows = read_csv(backtest_csv)
    cohort_rows = build_cohort_audit(backtest_rows, read_csv(summary_csv), read_csv(ic_csv), read_csv(recommendations_csv))
    splits = split_rows(backtest_rows)
    actions = feature_actions(cohort_rows, splits)
    write_csv(cohort_output_csv, cohort_rows, COHORT_FIELDS)
    write_csv(split_output_csv, splits, SPLIT_FIELDS)
    write_csv(action_output_csv, actions, ACTION_FIELDS)
    print(f"cohort_taxonomy_audit_csv={cohort_output_csv} rows={len(cohort_rows)}")
    print(f"cohort_split_candidates_csv={split_output_csv} rows={len(splits)}")
    print(f"cohort_feature_action_plan_csv={action_output_csv} rows={len(actions)}")


if __name__ == "__main__":
    main()
