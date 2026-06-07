#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import importlib.util
import math
import sys
from collections import defaultdict
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
RESTRICTED_TEMPLATE_SCRIPT = PACKAGE_ROOT / "scripts" / "41_test_med_device_restricted_cohort_templates.py"
RESULT_FIELDS = [
    "calibration_cohort",
    "template_id",
    "horizon_days",
    "split",
    "count",
    "unique_tickers",
    "selected_ticker_coverage",
    "mean_return",
    "median_return",
    "hit_rate",
    "mean_excess",
    "median_excess",
    "excess_hit_rate",
    "lcb_excess",
    "delta_mean_excess_vs_baseline",
    "delta_median_excess_vs_baseline",
    "delta_excess_hit_rate_vs_baseline",
    "delta_lcb_excess_vs_baseline",
    "improved_selected_ticker_rate",
    "improved_metric_count",
    "improvement_status",
    "improvement_reason",
    "weights_spec",
]
RECOMMENDATION_FIELDS = [
    "calibration_cohort",
    "recommended_template_id",
    "promotion_status",
    "best_horizon_days",
    "holdout_mean_excess",
    "holdout_median_excess",
    "holdout_excess_hit_rate",
    "holdout_lcb_excess",
    "holdout_unique_tickers",
    "holdout_selected_ticker_coverage",
    "improved_selected_ticker_rate",
    "improved_metric_count",
    "promotion_reason",
]
TICKER_FIELDS = [
    "calibration_cohort",
    "template_id",
    "horizon_days",
    "ticker",
    "selection_status",
    "selected_obs",
    "selected_asofs",
    "mean_excess",
    "median_excess",
    "baseline_selected_obs",
    "baseline_mean_excess",
    "delta_mean_excess_vs_baseline",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Validate restricted-cohort templates on holdout rows.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--input-csv", type=Path, default=None)
    parser.add_argument("--output-csv", type=Path, default=None)
    parser.add_argument("--recommendation-csv", type=Path, default=None)
    parser.add_argument("--ticker-csv", type=Path, default=None)
    parser.add_argument("--horizons", type=str, default="")
    return parser.parse_args()


def load_template_module() -> Any:
    path = RESTRICTED_TEMPLATE_SCRIPT
    if not path.exists():
        raise RuntimeError(
            "Restricted cohort template dependency is missing. "
            f"Expected script 41 at: {path}"
        )
    spec = importlib.util.spec_from_file_location("restricted_cohort_templates", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to import restricted template module: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def parse_int_list(raw: object) -> list[int]:
    out: list[int] = []
    for item in str(raw or "").split(","):
        text = item.strip()
        if text.isdigit():
            out.append(int(text))
    return out


def to_float(raw: object) -> float | None:
    try:
        value = float(str(raw).strip())
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def fmt(value: object) -> str:
    number = to_float(value)
    return "" if number is None else f"{number:.6f}"


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def write_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore", lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def load_validation_candidate_templates(config: dict[str, Any], *, base_dir: Path) -> tuple[set[tuple[str, str]], set[str]]:
    path = resolve_path(
        cfg_get(
            config,
            "calibration.restricted_cohort_templates.output_csv",
            "../output/med_devices_reports/calibration/med_device_restricted_cohort_template_results.csv",
        ),
        base_dir=base_dir,
    )
    if not path.exists():
        return set(), set()
    candidates: set[tuple[str, str]] = set()
    evaluated_cohorts: set[str] = set()
    for row in read_csv(path):
        cohort = str(row.get("calibration_cohort") or "").strip()
        template_id = str(row.get("template_id") or "").strip()
        if not cohort or not template_id or template_id == "baseline_existing":
            continue
        if str(row.get("split") or "").strip().lower() != "validation":
            continue
        evaluated_cohorts.add(cohort)
        if str(row.get("promotion_status") or "").strip().lower() == "candidate":
            candidates.add((cohort, template_id))
    return candidates, evaluated_cohorts


def active_production_templates(config: dict[str, Any]) -> set[tuple[str, str]]:
    raw_profiles = cfg_get(config, "scoring.cohort_profiles", {}) or {}
    if not isinstance(raw_profiles, dict):
        return set()
    out: set[tuple[str, str]] = set()
    for cohort, raw_profile in raw_profiles.items():
        if not isinstance(raw_profile, dict):
            continue
        if str(raw_profile.get("enabled", True)).strip().lower() in {"0", "false", "no", "off"}:
            continue
        if str(raw_profile.get("calibration_status", "production_eligible")).strip().lower() != "production_eligible":
            continue
        raw_template = raw_profile.get("score_template")
        if not isinstance(raw_template, dict):
            continue
        template_id = str(raw_template.get("template_id") or "").strip()
        if template_id:
            out.add((str(cohort), template_id))
    return out


def available_horizons(rows: list[dict[str, str]]) -> list[int]:
    if not rows:
        return []
    out: list[int] = []
    for key in rows[0]:
        if key.startswith("cohort_excess_return_") and key.endswith("d"):
            text = key[len("cohort_excess_return_") : -1]
            if text.isdigit():
                out.append(int(text))
    return sorted(out)


def lcb(values: list[float], z: float = 1.64) -> float:
    if not values:
        return 0.0
    if len(values) < 2:
        return values[0]
    avg = mean(values)
    variance = sum((value - avg) ** 2 for value in values) / (len(values) - 1)
    return avg - z * math.sqrt(variance / len(values))


def selected_rows(
    rows: list[dict[str, Any]],
    *,
    split: str,
    horizon: int,
    config: dict[str, Any],
    template_module: Any,
) -> list[dict[str, Any]]:
    return [
        row for row in rows
        if row.get("sim_cohort_rank_bucket") == "cohort_top_decile"
        and (split == "all" or template_module.split_for_row(row, config) == split)
        and to_float(row.get(f"forward_return_{horizon}d")) is not None
        and to_float(row.get(f"cohort_excess_return_{horizon}d")) is not None
    ]


def metrics(
    rows: list[dict[str, Any]],
    *,
    split: str,
    horizon: int,
    config: dict[str, Any],
    full_rows: list[dict[str, Any]],
    template_module: Any,
) -> dict[str, Any]:
    selected = selected_rows(rows, split=split, horizon=horizon, config=config, template_module=template_module)
    returns = [float(row[f"forward_return_{horizon}d"]) for row in selected]
    excess = [float(row[f"cohort_excess_return_{horizon}d"]) for row in selected]
    tickers = {str(row.get("ticker") or "") for row in selected}
    full_tickers = {str(row.get("ticker") or "") for row in full_rows}
    if not selected:
        return {
            "count": 0,
            "unique_tickers": 0,
            "selected_ticker_coverage": 0.0,
            "mean_return": 0.0,
            "median_return": 0.0,
            "hit_rate": 0.0,
            "mean_excess": 0.0,
            "median_excess": 0.0,
            "excess_hit_rate": 0.0,
            "lcb_excess": 0.0,
        }
    return {
        "count": len(selected),
        "unique_tickers": len(tickers),
        "selected_ticker_coverage": len(tickers) / len(full_tickers) if full_tickers else 0.0,
        "mean_return": mean(returns),
        "median_return": median(returns),
        "hit_rate": sum(1 for value in returns if value > 0) / len(returns),
        "mean_excess": mean(excess),
        "median_excess": median(excess),
        "excess_hit_rate": sum(1 for value in excess if value > 0) / len(excess),
        "lcb_excess": lcb(excess),
    }


def selected_ticker_means(
    rows: list[dict[str, Any]],
    *,
    split: str,
    horizon: int,
    config: dict[str, Any],
    template_module: Any,
) -> dict[str, float]:
    grouped: dict[str, list[float]] = defaultdict(list)
    for row in selected_rows(rows, split=split, horizon=horizon, config=config, template_module=template_module):
        value = to_float(row.get(f"cohort_excess_return_{horizon}d"))
        if value is not None:
            grouped[str(row.get("ticker") or "")].append(value)
    return {ticker: mean(values) for ticker, values in grouped.items() if values}


def aggregate_tickers(
    rows: list[dict[str, Any]],
    *,
    split: str,
    horizon: int,
    config: dict[str, Any],
    template_module: Any,
) -> dict[str, dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in selected_rows(rows, split=split, horizon=horizon, config=config, template_module=template_module):
        grouped[str(row.get("ticker") or "")].append(row)
    out: dict[str, dict[str, Any]] = {}
    for ticker, items in grouped.items():
        excess = [float(row[f"cohort_excess_return_{horizon}d"]) for row in items]
        asofs = sorted({str(row.get("asof_date") or "")[:10] for row in items})
        out[ticker] = {
            "selected_obs": len(items),
            "selected_asofs": ";".join(asofs),
            "mean_excess": mean(excess),
            "median_excess": median(excess),
        }
    return out


def weights_spec(template: Any) -> str:
    return ";".join(f"{field}:{direction}:{weight:.2f}" for field, direction, weight in template.weights)


def improve_row(
    row: dict[str, Any],
    baseline: dict[str, Any],
    ticker_rate: float,
    min_ticker_rate: float,
    *,
    validation_candidate: bool,
    require_positive_median: bool,
    require_positive_lcb: bool,
) -> None:
    deltas = {
        "mean": float(row["mean_excess"]) - float(baseline["mean_excess"]),
        "median": float(row["median_excess"]) - float(baseline["median_excess"]),
        "hit": float(row["excess_hit_rate"]) - float(baseline["excess_hit_rate"]),
        "lcb": float(row["lcb_excess"]) - float(baseline["lcb_excess"]),
    }
    for name, value in deltas.items():
        row[f"delta_{name if name != 'hit' else 'excess_hit_rate'}_excess_vs_baseline"] = value
    row["delta_excess_hit_rate_vs_baseline"] = deltas["hit"]
    improved_metrics = [key for key, value in deltas.items() if value > 0]
    row["improved_metric_count"] = len(improved_metrics)
    row["improved_selected_ticker_rate"] = ticker_rate
    reasons: list[str] = []
    if not validation_candidate:
        reasons.append("validation_template_not_candidate")
    if not improved_metrics:
        reasons.append("no_holdout_metric_improved")
    if require_positive_median and float(row["median_excess"]) <= 0:
        reasons.append("holdout_median_excess_not_positive")
    if require_positive_lcb and float(row["lcb_excess"]) <= 0:
        reasons.append("holdout_lcb_excess_not_positive")
    if ticker_rate < min_ticker_rate:
        reasons.append("improved_selected_ticker_rate_below_min")
    row["improvement_status"] = "improved" if not reasons else "mixed_or_reject"
    row["improvement_reason"] = ";".join(reasons) if reasons else "improved_" + "_".join(improved_metrics)


def add_ticker_rows(
    out: list[dict[str, Any]],
    *,
    cohort: str,
    template_id: str,
    horizon: int,
    baseline: dict[str, dict[str, Any]],
    candidate: dict[str, dict[str, Any]],
) -> None:
    for ticker in sorted(set(baseline) | set(candidate)):
        cand = candidate.get(ticker)
        base = baseline.get(ticker)
        if cand and base:
            status = "both"
        elif cand:
            status = "candidate_only"
        else:
            status = "baseline_only"
            cand = {
                "selected_obs": 0,
                "selected_asofs": "",
                "mean_excess": None,
                "median_excess": None,
            }
        out.append(
            {
                "calibration_cohort": cohort,
                "template_id": template_id,
                "horizon_days": horizon,
                "ticker": ticker,
                "selection_status": status,
                "selected_obs": cand["selected_obs"],
                "selected_asofs": cand["selected_asofs"],
                "mean_excess": fmt(cand["mean_excess"]),
                "median_excess": fmt(cand["median_excess"]),
                "baseline_selected_obs": base["selected_obs"] if base else 0,
                "baseline_mean_excess": fmt(base["mean_excess"] if base else None),
                "delta_mean_excess_vs_baseline": fmt(
                    cand["mean_excess"] - base["mean_excess"]
                    if cand["mean_excess"] is not None and base
                    else None
                ),
            }
        )


def main() -> None:
    configure_utc_logging()
    args = parse_args()
    template_module = load_template_module()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    base_dir = config_path.parent
    input_csv = (
        args.input_csv.expanduser().resolve()
        if args.input_csv
        else resolve_path(cfg_get(config, "calibration.cohort_neutral_backtest_csv"), base_dir=base_dir)
    )
    output_csv = (
        args.output_csv.expanduser().resolve()
        if args.output_csv
        else resolve_path(
            cfg_get(
                config,
                "calibration.restricted_cohort_template_holdout.output_csv",
                "../output/med_devices_reports/calibration/med_device_restricted_cohort_template_holdout_results.csv",
            ),
            base_dir=base_dir,
        )
    )
    recommendation_csv = (
        args.recommendation_csv.expanduser().resolve()
        if args.recommendation_csv
        else resolve_path(
            cfg_get(
                config,
                "calibration.restricted_cohort_template_holdout.recommendation_csv",
                "../output/med_devices_reports/calibration/med_device_restricted_cohort_template_holdout_recommendations.csv",
            ),
            base_dir=base_dir,
        )
    )
    ticker_csv = (
        args.ticker_csv.expanduser().resolve()
        if args.ticker_csv
        else resolve_path(
            cfg_get(
                config,
                "calibration.restricted_cohort_template_holdout.ticker_csv",
                "../output/med_devices_reports/calibration/med_device_restricted_cohort_template_holdout_tickers.csv",
            ),
            base_dir=base_dir,
        )
    )
    rows = read_csv(input_csv)
    validation_candidate_templates, validation_candidate_cohorts = load_validation_candidate_templates(
        config,
        base_dir=base_dir,
    )
    production_templates = active_production_templates(config)
    horizons = parse_int_list(args.horizons) or parse_int_list(
        cfg_get(config, "calibration.restricted_cohort_template_holdout.horizons", "30,60,120")
    )
    horizons = [horizon for horizon in horizons if horizon in available_horizons(rows)]
    min_ticker_rate = float(cfg_get(config, "calibration.restricted_cohort_template_holdout.min_improved_selected_ticker_rate", 0.34))
    require_positive_median = str(
        cfg_get(config, "calibration.restricted_cohort_template_holdout.require_positive_median_excess", True)
    ).strip().lower() not in {"0", "false", "no", "off"}
    require_positive_lcb = str(
        cfg_get(config, "calibration.restricted_cohort_template_holdout.require_positive_lcb_excess", True)
    ).strip().lower() not in {"0", "false", "no", "off"}
    by_cohort: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_cohort[str(row.get("calibration_cohort") or "")].append(row)

    result_rows: list[dict[str, Any]] = []
    ticker_rows: list[dict[str, Any]] = []
    baseline_by_key: dict[tuple[str, int], dict[str, Any]] = {}
    baseline_tickers_by_key: dict[tuple[str, int], dict[str, dict[str, Any]]] = {}
    for cohort in sorted({template.cohort for template in template_module.templates()}):
        cohort_rows = by_cohort.get(cohort, [])
        if not cohort_rows:
            continue
        baseline = template_module.baseline_rows(cohort_rows)
        for horizon in horizons:
            item = {
                "calibration_cohort": cohort,
                "template_id": "baseline_existing",
                "horizon_days": horizon,
                "split": "holdout_or_incomplete",
                "weights_spec": "",
                "improved_selected_ticker_rate": 0.0,
                "improved_metric_count": 0,
                "improvement_status": "baseline",
                "improvement_reason": "baseline_reference",
            }
            item.update(
                metrics(
                    baseline,
                    split="holdout_or_incomplete",
                    horizon=horizon,
                    config=config,
                    full_rows=cohort_rows,
                    template_module=template_module,
                )
            )
            for field in ("mean_excess", "median_excess", "excess_hit_rate", "lcb_excess"):
                item[f"delta_{field}_vs_baseline"] = 0.0
            baseline_by_key[(cohort, horizon)] = item
            baseline_tickers_by_key[(cohort, horizon)] = aggregate_tickers(
                baseline,
                split="holdout_or_incomplete",
                horizon=horizon,
                config=config,
                template_module=template_module,
            )
            result_rows.append(item)
        for template in [template for template in template_module.templates() if template.cohort == cohort]:
            simulated = template_module.simulated_rows(cohort_rows, template)
            for horizon in horizons:
                baseline_item = baseline_by_key[(cohort, horizon)]
                base_tickers = baseline_tickers_by_key[(cohort, horizon)]
                candidate_tickers = aggregate_tickers(
                    simulated,
                    split="holdout_or_incomplete",
                    horizon=horizon,
                    config=config,
                    template_module=template_module,
                )
                candidate_means = {ticker: item["mean_excess"] for ticker, item in candidate_tickers.items()}
                base_means = {ticker: item["mean_excess"] for ticker, item in base_tickers.items()}
                comparable = [ticker for ticker in candidate_means if ticker in base_means]
                improved = [ticker for ticker in comparable if candidate_means[ticker] > base_means[ticker]]
                ticker_rate = len(improved) / len(comparable) if comparable else 0.0
                item = {
                    "calibration_cohort": cohort,
                    "template_id": template.template_id,
                    "horizon_days": horizon,
                    "split": "holdout_or_incomplete",
                    "weights_spec": weights_spec(template),
                }
                item.update(
                    metrics(
                        simulated,
                        split="holdout_or_incomplete",
                        horizon=horizon,
                        config=config,
                        full_rows=cohort_rows,
                        template_module=template_module,
                    )
                )
                validation_candidate = (
                    cohort not in validation_candidate_cohorts
                    or (cohort, template.template_id) in validation_candidate_templates
                    or (cohort, template.template_id) in production_templates
                )
                improve_row(
                    item,
                    baseline_item,
                    ticker_rate,
                    min_ticker_rate,
                    validation_candidate=validation_candidate,
                    require_positive_median=require_positive_median,
                    require_positive_lcb=require_positive_lcb,
                )
                result_rows.append(item)
                add_ticker_rows(
                    ticker_rows,
                    cohort=cohort,
                    template_id=template.template_id,
                    horizon=horizon,
                    baseline=base_tickers,
                    candidate=candidate_tickers,
                )

    recommendations: list[dict[str, Any]] = []
    for cohort in sorted({row["calibration_cohort"] for row in result_rows}):
        candidates = [
            row for row in result_rows
            if row["calibration_cohort"] == cohort
            and row["template_id"] != "baseline_existing"
            and row["improvement_status"] == "improved"
        ]
        candidates.sort(
            key=lambda row: (
                int(row["improved_metric_count"]),
                int(row["horizon_days"]),
                float(row["lcb_excess"]),
                float(row["median_excess"]),
                float(row["mean_excess"]),
            ),
            reverse=True,
        )
        best = candidates[0] if candidates else None
        recommendations.append(
            {
                "calibration_cohort": cohort,
                "recommended_template_id": best["template_id"] if best else "",
                "promotion_status": "holdout_improved" if best else "no_holdout_improvement",
                "best_horizon_days": best["horizon_days"] if best else "",
                "holdout_mean_excess": best["mean_excess"] if best else "",
                "holdout_median_excess": best["median_excess"] if best else "",
                "holdout_excess_hit_rate": best["excess_hit_rate"] if best else "",
                "holdout_lcb_excess": best["lcb_excess"] if best else "",
                "holdout_unique_tickers": best["unique_tickers"] if best else "",
                "holdout_selected_ticker_coverage": best["selected_ticker_coverage"] if best else "",
                "improved_selected_ticker_rate": best["improved_selected_ticker_rate"] if best else "",
                "improved_metric_count": best["improved_metric_count"] if best else "",
                "promotion_reason": best["improvement_reason"] if best else "no tested template improved holdout metrics",
            }
        )
    for row in result_rows:
        for field in RESULT_FIELDS:
            if field in {"calibration_cohort", "template_id", "split", "improvement_status", "improvement_reason", "weights_spec"}:
                continue
            row[field] = fmt(row.get(field))
    write_csv(output_csv, result_rows, RESULT_FIELDS)
    write_csv(recommendation_csv, recommendations, RECOMMENDATION_FIELDS)
    write_csv(ticker_csv, ticker_rows, TICKER_FIELDS)
    print(f"restricted_cohort_template_holdout_results_csv={output_csv} rows={len(result_rows)}")
    print(f"restricted_cohort_template_holdout_recommendations_csv={recommendation_csv} rows={len(recommendations)}")
    print(f"restricted_cohort_template_holdout_ticker_csv={ticker_csv} rows={len(ticker_rows)}")


if __name__ == "__main__":
    main()
