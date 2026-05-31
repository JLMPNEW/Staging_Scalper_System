#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import hashlib
import itertools
import math
import sys
from datetime import datetime, timedelta
from pathlib import Path
from statistics import mean, median
from typing import Any


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from med_devices.core.config import cfg_get, load_yaml, resolve_path  # noqa: E402
from med_devices.core.fda_states import MANUAL_FDA_REVIEW_STATES as MANUAL_FDA_STATES  # noqa: E402
from med_devices.core.logging_utils import configure_utc_logging  # noqa: E402


DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
REVIEW_CLASSIFICATIONS = {
    "manual_review_regulatory_risk",
    "avoid_confirmed_regulatory_risk",
    "data_review_required",
}
METRIC_KEYS = ("count", "unique_tickers", "mean", "median", "hit_rate", "lcb", "sortino", "profit_factor")
GRID_FIELDS = [
    "calibration_cohort",
    "tag_template_id",
    "technical_entry_max",
    "fundamental_quality_min",
    "valuation_min",
    "fda_product_min",
    "value_trap_max",
    "data_completeness_min",
    "objective_score",
    "pass_fail",
    "rejection_reason",
    "validation_cohort_unique_tickers_120d",
    "validation_selected_ticker_coverage_120d",
    "validation_improved_selected_ticker_rate_120d",
    "selected_tickers_validation",
]
SUMMARY_FIELDS = [
    "calibration_cohort",
    "validation_cohort_unique_tickers_120d",
    "full_cohort_median_120d",
    "full_cohort_lcb_120d",
    "best_tag_template_id",
    "best_pass_fail",
    "best_rejection_reason",
    "best_selected_ticker_coverage_120d",
    "best_improved_selected_ticker_rate_120d",
    "best_median_120d",
    "best_lcb_120d",
    "best_hit_rate_120d",
    "best_profit_factor_120d",
    "selected_tickers_validation",
    "recommended_next_step",
]
TICKER_FIELDS = [
    "calibration_cohort",
    "tag_template_id",
    "ticker",
    "company_name",
    "improved_flag",
    "selected_rows",
    "mean_excess_120d",
    "median_excess_120d",
    "hit_rate_excess_120d",
    "avg_technical_entry_score",
    "avg_fundamental_quality_score",
    "avg_valuation_score",
    "avg_fda_product_score",
    "avg_value_trap_score",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Test pullback-candidate tags across med-device calibration cohorts.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--input-csv", type=Path, default=None)
    parser.add_argument("--output-csv", type=Path, default=None)
    parser.add_argument("--summary-csv", type=Path, default=None)
    parser.add_argument("--ticker-csv", type=Path, default=None)
    return parser.parse_args()


def to_float(raw: object) -> float | None:
    try:
        value = float(str(raw).strip())
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def float_or_default(raw: object, default: float) -> float:
    value = to_float(raw)
    return default if value is None else value


def int_flag(raw: object) -> int:
    text = str(raw or "").strip().lower()
    return 1 if text in {"1", "true", "yes", "y", "on"} or raw == 1 else 0


def parse_float_list(raw: object, default: str) -> list[float]:
    return [float(item.strip()) for item in str(raw or default).split(",") if item.strip()]


def parse_date(raw: object) -> datetime | None:
    text = str(raw or "").strip()[:10]
    if not text:
        return None
    try:
        return datetime.strptime(text, "%Y-%m-%d")
    except ValueError:
        return None


def effective_train_end(train_end_asof: str, validation_start_asof: str, embargo_days: int) -> str:
    train_end = parse_date(train_end_asof)
    validation_start = parse_date(validation_start_asof)
    if train_end is None or validation_start is None or embargo_days <= 0:
        return train_end_asof
    return min(train_end, validation_start - timedelta(days=embargo_days)).strftime("%Y-%m-%d")


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def write_csv(path: Path, rows: list[dict[str, Any]], fields: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore", lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def return_horizons(rows: list[dict[str, str]]) -> list[int]:
    if not rows:
        return []
    out: list[int] = []
    for key in rows[0]:
        if key.startswith("cohort_excess_return_") and key.endswith("d"):
            text = key[len("cohort_excess_return_") : -1]
            if text.isdigit():
                out.append(int(text))
    return sorted(out)


def cohorts(rows: list[dict[str, str]]) -> list[str]:
    return sorted({str(row.get("calibration_cohort") or "") for row in rows if str(row.get("calibration_cohort") or "")})


def selected_values(rows: list[dict[str, str]], *, horizon: int) -> tuple[list[float], list[str]]:
    values: list[float] = []
    tickers: list[str] = []
    field = f"cohort_excess_return_{horizon}d"
    for row in rows:
        value = to_float(row.get(field))
        if value is None:
            continue
        values.append(value)
        tickers.append(str(row.get("ticker") or ""))
    return values, tickers


def unique_tickers_with_returns(rows: list[dict[str, str]], *, horizon: int) -> set[str]:
    out: set[str] = set()
    for row in rows:
        if to_float(row.get(f"cohort_excess_return_{horizon}d")) is not None and str(row.get("ticker") or ""):
            out.add(str(row["ticker"]))
    return out


def selected_ticker_improvement_rate(rows: list[dict[str, str]], *, horizon: int) -> float | None:
    grouped: dict[str, list[float]] = {}
    for row in rows:
        value = to_float(row.get(f"cohort_excess_return_{horizon}d"))
        ticker = str(row.get("ticker") or "")
        if value is not None and ticker:
            grouped.setdefault(ticker, []).append(value)
    if not grouped:
        return None
    return sum(1 for values in grouped.values() if median(values) > 0) / len(grouped)


def metrics(values: list[float], tickers: list[str]) -> dict[str, Any]:
    if not values:
        return {key: "" for key in METRIC_KEYS} | {"count": 0, "unique_tickers": 0}
    avg = mean(values)
    if len(values) == 1:
        lcb = values[0]
    else:
        variance = sum((value - avg) ** 2 for value in values) / (len(values) - 1)
        lcb = avg - 1.64 * math.sqrt(variance) / math.sqrt(len(values))
    downside = [value for value in values if value < 0]
    if downside:
        downside_dev = math.sqrt(sum(value * value for value in downside) / len(downside))
        sortino = avg / downside_dev if downside_dev > 1e-12 else 999.0
    else:
        sortino = 999.0 if avg > 0 else 0.0
    gains = sum(value for value in values if value > 0)
    losses = -sum(value for value in values if value < 0)
    profit_factor = 999.0 if losses <= 1e-12 and gains > 0 else (gains / losses if losses > 1e-12 else 0.0)
    return {
        "count": len(values),
        "unique_tickers": len({ticker for ticker in tickers if ticker}),
        "mean": f"{avg:.6f}",
        "median": f"{median(values):.6f}",
        "hit_rate": f"{sum(1 for value in values if value > 0) / len(values):.4f}",
        "lcb": f"{lcb:.6f}",
        "sortino": f"{sortino:.4f}",
        "profit_factor": f"{profit_factor:.4f}",
    }


def row_metrics(rows: list[dict[str, str]], *, horizon: int) -> dict[str, Any]:
    values, tickers = selected_values(rows, horizon=horizon)
    return metrics(values, tickers)


def score_objective(metrics_by_horizon: dict[int, dict[str, Any]], weights: dict[str, float]) -> float:
    total = 0.0
    used = 0
    for payload in metrics_by_horizon.values():
        median_value = to_float(payload.get("median")) or 0.0
        lcb_value = to_float(payload.get("lcb")) or 0.0
        mean_value = to_float(payload.get("mean")) or 0.0
        sortino = min(3.0, max(-3.0, to_float(payload.get("sortino")) or 0.0))
        profit = min(3.0, max(0.0, to_float(payload.get("profit_factor")) or 0.0))
        total += (
            float(weights.get("median_excess_return", 0.35)) * median_value * 100.0
            + float(weights.get("lower_confidence_bound", 0.25)) * lcb_value * 100.0
            + float(weights.get("sortino", 0.15)) * sortino
            + float(weights.get("profit_factor", 0.15)) * (profit - 1.0)
            + float(weights.get("mean_excess_return", 0.10)) * mean_value * 100.0
        )
        used += 1
    return total / used if used else -999.0


def template_id(*values: object) -> str:
    return hashlib.sha1("|".join(str(value) for value in values).encode("utf-8")).hexdigest()[:12]


def output_fields(horizons: list[int]) -> list[str]:
    fields = list(GRID_FIELDS)
    for horizon in horizons:
        for prefix in ("train", "validation"):
            for key in METRIC_KEYS:
                fields.append(f"{prefix}_{key}_{horizon}d")
    return fields


def passes_static_controls(row: dict[str, str], *, value_trap_max: float, data_completeness_min: float) -> bool:
    if str(row.get("classification") or "") in REVIEW_CLASSIFICATIONS:
        return False
    fda_state = str(row.get("fda_review_state") or "").strip().lower()
    if fda_state in MANUAL_FDA_STATES or int_flag(row.get("hard_red_flag")):
        return False
    value_trap = to_float(row.get("value_trap_score"))
    if value_trap is not None and value_trap > value_trap_max:
        return False
    completeness = to_float(row.get("data_completeness_score"))
    return completeness is not None and completeness >= data_completeness_min


def passes_pullback_tag(
    row: dict[str, str],
    *,
    technical_entry_max: float,
    fundamental_quality_min: float,
    valuation_min: float,
    fda_product_min: float,
    value_trap_max: float,
    data_completeness_min: float,
) -> bool:
    if not passes_static_controls(row, value_trap_max=value_trap_max, data_completeness_min=data_completeness_min):
        return False
    checks = [
        ("technical_entry_score", technical_entry_max, False),
        ("fundamental_quality_score", fundamental_quality_min, True),
        ("valuation_score", valuation_min, True),
        ("fda_product_score", fda_product_min, True),
    ]
    for field, threshold, higher_is_better in checks:
        value = to_float(row.get(field))
        if value is None:
            return False
        if higher_is_better and value < threshold:
            return False
        if not higher_is_better and value > threshold:
            return False
    return True


def evaluate_template(
    rows: list[dict[str, str]],
    *,
    cohort: str,
    horizons: list[int],
    train_end_asof: str,
    validation_start_asof: str,
    validation_end_asof: str,
    min_train_obs: int,
    min_validation_obs: int,
    min_unique_tickers: int,
    min_selected_validation: int,
    min_selected_ticker_coverage: float,
    min_improved_selected_ticker_rate: float,
    objective_weights: dict[str, float],
    technical_entry_max: float,
    fundamental_quality_min: float,
    valuation_min: float,
    fda_product_min: float,
    value_trap_max: float,
    data_completeness_min: float,
) -> dict[str, Any]:
    selected = [
        row
        for row in rows
        if str(row.get("calibration_cohort") or "") == cohort
        and passes_pullback_tag(
            row,
            technical_entry_max=technical_entry_max,
            fundamental_quality_min=fundamental_quality_min,
            valuation_min=valuation_min,
            fda_product_min=fda_product_min,
            value_trap_max=value_trap_max,
            data_completeness_min=data_completeness_min,
        )
    ]
    train = [row for row in selected if str(row.get("asof_date") or "") <= train_end_asof]
    validation = [row for row in selected if validation_start_asof <= str(row.get("asof_date") or "") <= validation_end_asof]
    validation_all = [
        row
        for row in rows
        if str(row.get("calibration_cohort") or "") == cohort
        and validation_start_asof <= str(row.get("asof_date") or "") <= validation_end_asof
    ]
    metrics_train: dict[int, dict[str, Any]] = {}
    metrics_validation: dict[int, dict[str, Any]] = {}
    for horizon in horizons:
        metrics_train[horizon] = row_metrics(train, horizon=horizon)
        metrics_validation[horizon] = row_metrics(validation, horizon=horizon)

    ref_horizon = max(horizons)
    validation_ref = metrics_validation[ref_horizon]
    train_ref = metrics_train[ref_horizon]
    validation_cohort_tickers = unique_tickers_with_returns(validation_all, horizon=ref_horizon)
    selected_tickers = unique_tickers_with_returns(validation, horizon=ref_horizon)
    selected_coverage = len(selected_tickers) / len(validation_cohort_tickers) if validation_cohort_tickers else 0.0
    improved_rate = selected_ticker_improvement_rate(validation, horizon=ref_horizon)
    rejection: list[str] = []
    if int(train_ref["count"]) < min_train_obs:
        rejection.append("insufficient_train_obs")
    if int(validation_ref["count"]) < min_validation_obs:
        rejection.append("insufficient_validation_obs")
    if int(validation_ref["count"]) < min_selected_validation:
        rejection.append("insufficient_selected_validation")
    if int(validation_ref["unique_tickers"]) < min_unique_tickers:
        rejection.append("insufficient_unique_tickers")
    if selected_coverage < min_selected_ticker_coverage:
        rejection.append("insufficient_selected_ticker_coverage")
    if improved_rate is None or improved_rate < min_improved_selected_ticker_rate:
        rejection.append("insufficient_improved_selected_ticker_rate")
    if (to_float(validation_ref.get("median")) or 0.0) <= 0:
        rejection.append("nonpositive_validation_median_excess")
    if (to_float(validation_ref.get("lcb")) or 0.0) <= 0:
        rejection.append("nonpositive_validation_lcb_excess")
    if (to_float(train_ref.get("median")) or 0.0) > 0 and (to_float(validation_ref.get("median")) or 0.0) < 0:
        rejection.append("train_validation_sign_flip")

    item: dict[str, Any] = {
        "calibration_cohort": cohort,
        "tag_template_id": template_id(
            cohort,
            technical_entry_max,
            fundamental_quality_min,
            valuation_min,
            fda_product_min,
            value_trap_max,
            data_completeness_min,
        ),
        "technical_entry_max": technical_entry_max,
        "fundamental_quality_min": fundamental_quality_min,
        "valuation_min": valuation_min,
        "fda_product_min": fda_product_min,
        "value_trap_max": value_trap_max,
        "data_completeness_min": data_completeness_min,
        "objective_score": f"{score_objective(metrics_validation, objective_weights):.6f}",
        "pass_fail": "fail" if rejection else "pass",
        "rejection_reason": ";".join(rejection),
        "validation_cohort_unique_tickers_120d": len(validation_cohort_tickers),
        "validation_selected_ticker_coverage_120d": f"{selected_coverage:.4f}",
        "validation_improved_selected_ticker_rate_120d": "" if improved_rate is None else f"{improved_rate:.4f}",
        "selected_tickers_validation": ";".join(sorted(selected_tickers))[:500],
    }
    for horizon in horizons:
        for prefix, payload in (("train", metrics_train[horizon]), ("validation", metrics_validation[horizon])):
            for key, value in payload.items():
                item[f"{prefix}_{key}_{horizon}d"] = value
    return item


def avg(rows: list[dict[str, str]], field: str) -> str:
    values = [value for row in rows if (value := to_float(row.get(field))) is not None]
    return "" if not values else f"{mean(values):.4f}"


def ticker_rows_for_best(
    rows: list[dict[str, str]],
    best_rows: list[dict[str, Any]],
    *,
    validation_start_asof: str,
    validation_end_asof: str,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for best in best_rows:
        cohort = str(best["calibration_cohort"])
        selected = [
            row
            for row in rows
            if str(row.get("calibration_cohort") or "") == cohort
            and validation_start_asof <= str(row.get("asof_date") or "") <= validation_end_asof
            and passes_pullback_tag(
                row,
                technical_entry_max=float(best["technical_entry_max"]),
                fundamental_quality_min=float(best["fundamental_quality_min"]),
                valuation_min=float(best["valuation_min"]),
                fda_product_min=float(best["fda_product_min"]),
                value_trap_max=float(best["value_trap_max"]),
                data_completeness_min=float(best["data_completeness_min"]),
            )
        ]
        grouped: dict[str, list[dict[str, str]]] = {}
        for row in selected:
            ticker = str(row.get("ticker") or "")
            if ticker:
                grouped.setdefault(ticker, []).append(row)
        for ticker, ticker_rows in sorted(grouped.items()):
            payload = row_metrics(ticker_rows, horizon=120)
            values, _ = selected_values(ticker_rows, horizon=120)
            out.append(
                {
                    "calibration_cohort": cohort,
                    "tag_template_id": best["tag_template_id"],
                    "ticker": ticker,
                    "company_name": ticker_rows[0].get("company_name", ""),
                    "improved_flag": int(bool(values) and median(values) > 0),
                    "selected_rows": payload["count"],
                    "mean_excess_120d": payload["mean"],
                    "median_excess_120d": payload["median"],
                    "hit_rate_excess_120d": payload["hit_rate"],
                    "avg_technical_entry_score": avg(ticker_rows, "technical_entry_score"),
                    "avg_fundamental_quality_score": avg(ticker_rows, "fundamental_quality_score"),
                    "avg_valuation_score": avg(ticker_rows, "valuation_score"),
                    "avg_fda_product_score": avg(ticker_rows, "fda_product_score"),
                    "avg_value_trap_score": avg(ticker_rows, "value_trap_score"),
                }
            )
    out.sort(key=lambda row: (row["calibration_cohort"], -int(row["improved_flag"]), row["ticker"]))
    return out


def summary_rows(
    rows: list[dict[str, str]],
    grid_rows: list[dict[str, Any]],
    *,
    validation_start_asof: str,
    validation_end_asof: str,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    by_cohort: dict[str, list[dict[str, Any]]] = {}
    for row in grid_rows:
        by_cohort.setdefault(str(row["calibration_cohort"]), []).append(row)
    for cohort, cohort_rows in sorted(by_cohort.items()):
        best = sorted(cohort_rows, key=lambda item: (item["pass_fail"] == "pass", float_or_default(item["objective_score"], -999.0)), reverse=True)[0]
        validation_rows = [
            row
            for row in rows
            if str(row.get("calibration_cohort") or "") == cohort
            and validation_start_asof <= str(row.get("asof_date") or "") <= validation_end_asof
        ]
        full_metrics = row_metrics(validation_rows, horizon=120)
        reason = str(best.get("rejection_reason") or "")
        if best["pass_fail"] == "pass":
            next_step = "Review economics; eligible for a recommendation-only pullback tag, not automatic promotion."
        elif "insufficient_selected_ticker_coverage" in reason:
            next_step = "Signal is too narrow; keep as diagnostic tag only."
        elif "insufficient_improved_selected_ticker_rate" in reason:
            next_step = "Signal is not broad enough across selected tickers; do not promote."
        else:
            next_step = "No robust pullback tag detected."
        out.append(
            {
                "calibration_cohort": cohort,
                "validation_cohort_unique_tickers_120d": best["validation_cohort_unique_tickers_120d"],
                "full_cohort_median_120d": full_metrics["median"],
                "full_cohort_lcb_120d": full_metrics["lcb"],
                "best_tag_template_id": best["tag_template_id"],
                "best_pass_fail": best["pass_fail"],
                "best_rejection_reason": reason,
                "best_selected_ticker_coverage_120d": best["validation_selected_ticker_coverage_120d"],
                "best_improved_selected_ticker_rate_120d": best["validation_improved_selected_ticker_rate_120d"],
                "best_median_120d": best.get("validation_median_120d", ""),
                "best_lcb_120d": best.get("validation_lcb_120d", ""),
                "best_hit_rate_120d": best.get("validation_hit_rate_120d", ""),
                "best_profit_factor_120d": best.get("validation_profit_factor_120d", ""),
                "selected_tickers_validation": best["selected_tickers_validation"],
                "recommended_next_step": next_step,
            }
        )
    return out


def path_from_config(base_dir: Path, config: dict[str, Any], key: str, explicit: Path | None) -> Path:
    if explicit:
        return explicit.expanduser().resolve()
    return resolve_path(cfg_get(config, key), base_dir=base_dir)


def main() -> None:
    configure_utc_logging()
    args = parse_args()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    base_dir = config_path.parent
    section = "calibration.pullback_candidate_tag"
    input_csv = (
        args.input_csv.expanduser().resolve()
        if args.input_csv
        else resolve_path(cfg_get(config, "calibration.cohort_neutral_backtest_csv"), base_dir=base_dir)
    )
    output_csv = path_from_config(base_dir, config, f"{section}.output_csv", args.output_csv)
    summary_csv = path_from_config(base_dir, config, f"{section}.summary_csv", args.summary_csv)
    ticker_csv = path_from_config(base_dir, config, f"{section}.ticker_csv", args.ticker_csv)

    rows = read_csv(input_csv)
    horizons = return_horizons(rows)
    validation_start = str(cfg_get(config, "calibration.validation_start_asof", "2025-06-06"))
    validation_end = str(cfg_get(config, "calibration.validation_end_asof", "2025-11-28"))
    effective_train = effective_train_end(
        str(cfg_get(config, "calibration.train_end_asof", "2025-05-30")),
        validation_start,
        int(cfg_get(config, "calibration.embargo_days", 120)),
    )
    objective_weights = cfg_get(config, "calibration.objective", {}) or {}

    grid_rows: list[dict[str, Any]] = []
    for cohort, technical_max, fundamental_min, valuation_min, fda_min, trap_max, completeness_min in itertools.product(
        cohorts(rows),
        parse_float_list(cfg_get(config, f"{section}.technical_entry_max"), "45,55,65,75"),
        parse_float_list(cfg_get(config, f"{section}.fundamental_quality_min"), "0,55,60"),
        parse_float_list(cfg_get(config, f"{section}.valuation_min"), "35,40,45"),
        parse_float_list(cfg_get(config, f"{section}.fda_product_min"), "50,55,60"),
        parse_float_list(cfg_get(config, f"{section}.value_trap_max"), "30,40,60"),
        parse_float_list(cfg_get(config, f"{section}.data_completeness_min"), "85,90,100"),
    ):
        grid_rows.append(
            evaluate_template(
                rows,
                cohort=cohort,
                horizons=horizons,
                train_end_asof=effective_train,
                validation_start_asof=validation_start,
                validation_end_asof=validation_end,
                min_train_obs=int(cfg_get(config, "calibration.min_train_obs", 100)),
                min_validation_obs=int(cfg_get(config, "calibration.min_validation_obs", 40)),
                min_unique_tickers=int(cfg_get(config, "calibration.min_unique_tickers", 5)),
                min_selected_validation=int(cfg_get(config, "calibration.min_selected_validation", 20)),
                min_selected_ticker_coverage=float(cfg_get(config, "calibration.min_selected_ticker_coverage", 0.60)),
                min_improved_selected_ticker_rate=float(cfg_get(config, "calibration.min_improved_selected_ticker_rate", 0.60)),
                objective_weights=objective_weights,
                technical_entry_max=technical_max,
                fundamental_quality_min=fundamental_min,
                valuation_min=valuation_min,
                fda_product_min=fda_min,
                value_trap_max=trap_max,
                data_completeness_min=completeness_min,
            )
        )
    grid_rows.sort(
        key=lambda row: (
            row["pass_fail"] == "pass",
            row["calibration_cohort"],
            float_or_default(row["objective_score"], -999.0),
        ),
        reverse=True,
    )
    summaries = summary_rows(rows, grid_rows, validation_start_asof=validation_start, validation_end_asof=validation_end)
    best_rows = [row for row in grid_rows if row["tag_template_id"] in {summary["best_tag_template_id"] for summary in summaries}]
    tickers = ticker_rows_for_best(rows, best_rows, validation_start_asof=validation_start, validation_end_asof=validation_end)

    write_csv(output_csv, grid_rows, output_fields(horizons))
    write_csv(summary_csv, summaries, SUMMARY_FIELDS)
    write_csv(ticker_csv, tickers, TICKER_FIELDS)
    print(f"pullback_candidate_grid_csv={output_csv} rows={len(grid_rows)}")
    print(f"pullback_candidate_summary_csv={summary_csv} rows={len(summaries)}")
    print(f"pullback_candidate_ticker_csv={ticker_csv} rows={len(tickers)}")


if __name__ == "__main__":
    main()
