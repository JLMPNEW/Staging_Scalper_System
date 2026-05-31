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
    "base_gate_parameter_set_id",
    "policy_template_id",
    "entry_policy_template",
    "technical_entry_min",
    "technical_entry_max",
    "pullback_fundamental_min",
    "pullback_valuation_min",
    "pullback_fda_product_min",
    "objective_score",
    "pass_fail",
    "rejection_reason",
    "validation_cohort_unique_tickers_120d",
    "validation_selected_ticker_coverage_120d",
    "validation_improved_selected_ticker_rate_120d",
    "selected_tickers_validation",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Test momentum versus pullback entry-policy templates for one cohort.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--cohort", type=str, default="")
    parser.add_argument("--input-csv", type=Path, default=None)
    parser.add_argument("--gate-csv", type=Path, default=None)
    parser.add_argument("--output-csv", type=Path, default=None)
    parser.add_argument("--recommendation-csv", type=Path, default=None)
    parser.add_argument("--comparison-csv", type=Path, default=None)
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


def reimbursement_live(row: dict[str, str]) -> bool:
    status = str(row.get("reimbursement_status") or "").strip().lower()
    if int_flag(row.get("unknown_reimbursement_flag")) or status in {"", "unknown", "cms_data_not_loaded"}:
        return False
    return True


def has_reimbursement_evidence(row: dict[str, str]) -> bool:
    return any(
        int_flag(row.get(field))
        for field in (
            "direct_code_evidence",
            "payment_rate_evidence",
            "coverage_policy_evidence",
            "procedure_bundled_flag",
            "capital_equipment_flag",
            "diagnostics_lab_flag",
        )
    )


def passes_base_gate(row: dict[str, str], gate: dict[str, str]) -> bool:
    if str(row.get("classification") or "") in REVIEW_CLASSIFICATIONS:
        return False
    checks = [
        ("raw_composite_score", float_or_default(gate.get("raw_score_min"), 0.0)),
        ("cohort_percentile", float_or_default(gate.get("cohort_percentile_min"), 0.0)),
        ("avg_dollar_volume_60d", float_or_default(gate.get("min_avg_dollar_volume_60d"), 0.0)),
        ("data_completeness_score", float_or_default(gate.get("data_completeness_min"), 0.0)),
    ]
    for field, threshold in checks:
        value = to_float(row.get(field))
        if field == "avg_dollar_volume_60d" and threshold <= 0 and value is None:
            continue
        if value is None or value < threshold:
            return False
    value_trap = to_float(row.get("value_trap_score"))
    if value_trap is not None and value_trap > float_or_default(gate.get("value_trap_max"), 100.0):
        return False

    fda_state = str(row.get("fda_review_state") or "").strip().lower()
    fda_policy = str(gate.get("fda_review_policy") or "exclude_manual_hard_red")
    if fda_policy == "clean_or_cleared_only" and fda_state not in {"", "clean", "cleared", "low_materiality"}:
        return False
    if fda_policy == "exclude_manual_hard_red" and (fda_state in MANUAL_FDA_STATES or int_flag(row.get("hard_red_flag"))):
        return False

    reimbursement_policy = str(gate.get("reimbursement_policy") or "all_known")
    if reimbursement_policy == "all_known" and not reimbursement_live(row):
        return False
    if reimbursement_policy == "live_evidence_only" and (not reimbursement_live(row) or not has_reimbursement_evidence(row)):
        return False
    if reimbursement_policy == "direct_or_bundled_or_capital" and not any(
        int_flag(row.get(field))
        for field in ("direct_code_evidence", "payment_rate_evidence", "procedure_bundled_flag", "capital_equipment_flag")
    ):
        return False
    return True


def pick_broad_gate(grid_rows: list[dict[str, str]], *, cohort: str, min_coverage: float) -> dict[str, str]:
    candidates = [
        row
        for row in grid_rows
        if str(row.get("calibration_cohort") or "") == cohort
        and (to_float(row.get("validation_selected_ticker_coverage_120d")) or 0.0) >= min_coverage
        and (to_float(row.get("validation_median_120d")) or 0.0) > 0
        and (to_float(row.get("validation_lcb_120d")) or 0.0) > 0
    ]
    if not candidates:
        candidates = [row for row in grid_rows if str(row.get("calibration_cohort") or "") == cohort]
    if not candidates:
        raise ValueError(f"No grid rows found for cohort {cohort!r}")
    return sorted(candidates, key=lambda row: float_or_default(row.get("objective_score"), -999.0), reverse=True)[0]


def passes_policy(row: dict[str, str], candidate: dict[str, Any]) -> bool:
    template = str(candidate["entry_policy_template"])
    technical = to_float(row.get("technical_entry_score"))
    if template == "technical_neutral":
        return True
    if technical is None:
        return False
    if template == "momentum_entry":
        if str(row.get("entry_status") or "") not in {"entry_eligible", "watch_for_setup"}:
            return False
        return technical >= float(candidate["technical_entry_min"])
    if template == "pullback_entry":
        fundamental = to_float(row.get("fundamental_quality_score"))
        valuation = to_float(row.get("valuation_score"))
        fda_score = to_float(row.get("fda_product_score"))
        return (
            technical <= float(candidate["technical_entry_max"])
            and fundamental is not None
            and fundamental >= float(candidate["pullback_fundamental_min"])
            and valuation is not None
            and valuation >= float(candidate["pullback_valuation_min"])
            and fda_score is not None
            and fda_score >= float(candidate["pullback_fda_product_min"])
        )
    raise ValueError(f"Unknown entry policy template: {template}")


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


def policy_id(*values: object) -> str:
    return hashlib.sha1("|".join(str(value) for value in values).encode("utf-8")).hexdigest()[:12]


def output_fields(horizons: list[int]) -> list[str]:
    fields = list(GRID_FIELDS)
    for horizon in horizons:
        for prefix in ("train", "validation"):
            for key in METRIC_KEYS:
                fields.append(f"{prefix}_{key}_{horizon}d")
    return fields


def comparison_fields(horizons: list[int]) -> list[str]:
    fields = ["selection_name", "selected_tickers"]
    for horizon in horizons:
        for key in METRIC_KEYS:
            fields.append(f"{key}_{horizon}d")
    return fields


def evaluate_candidate(
    rows: list[dict[str, str]],
    *,
    cohort: str,
    base_gate: dict[str, str],
    candidate: dict[str, Any],
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
) -> dict[str, Any]:
    selected = [
        row
        for row in rows
        if str(row.get("calibration_cohort") or "") == cohort
        and passes_base_gate(row, base_gate)
        and passes_policy(row, candidate)
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
        values, tickers = selected_values(train, horizon=horizon)
        metrics_train[horizon] = metrics(values, tickers)
        values, tickers = selected_values(validation, horizon=horizon)
        metrics_validation[horizon] = metrics(values, tickers)

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
        "base_gate_parameter_set_id": base_gate.get("parameter_set_id", ""),
        "policy_template_id": policy_id(base_gate.get("parameter_set_id", ""), *candidate.values()),
        "entry_policy_template": candidate["entry_policy_template"],
        "technical_entry_min": candidate.get("technical_entry_min", ""),
        "technical_entry_max": candidate.get("technical_entry_max", ""),
        "pullback_fundamental_min": candidate.get("pullback_fundamental_min", ""),
        "pullback_valuation_min": candidate.get("pullback_valuation_min", ""),
        "pullback_fda_product_min": candidate.get("pullback_fda_product_min", ""),
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


def comparison_row(name: str, rows: list[dict[str, str]], *, horizons: list[int]) -> dict[str, Any]:
    item: dict[str, Any] = {"selection_name": name, "selected_tickers": ";".join(sorted(unique_tickers_with_returns(rows, horizon=max(horizons))))}
    for horizon in horizons:
        values, tickers = selected_values(rows, horizon=horizon)
        payload = metrics(values, tickers)
        for key, value in payload.items():
            item[f"{key}_{horizon}d"] = value
    return item


def candidate_policies(config: dict[str, Any]) -> list[dict[str, Any]]:
    section = "calibration.entry_policy_templates"
    out: list[dict[str, Any]] = [{"entry_policy_template": "technical_neutral"}]
    for technical_min in parse_float_list(cfg_get(config, f"{section}.momentum_technical_entry_min"), "45,50,55,65"):
        out.append({"entry_policy_template": "momentum_entry", "technical_entry_min": technical_min})
    for technical_max, fundamental_min, valuation_min, fda_min in itertools.product(
        parse_float_list(cfg_get(config, f"{section}.pullback_technical_entry_max"), "45,55,65,75"),
        parse_float_list(cfg_get(config, f"{section}.pullback_fundamental_min"), "0,55,60"),
        parse_float_list(cfg_get(config, f"{section}.pullback_valuation_min"), "35,40,45"),
        parse_float_list(cfg_get(config, f"{section}.pullback_fda_product_min"), "50,55,60"),
    ):
        out.append(
            {
                "entry_policy_template": "pullback_entry",
                "technical_entry_max": technical_max,
                "pullback_fundamental_min": fundamental_min,
                "pullback_valuation_min": valuation_min,
                "pullback_fda_product_min": fda_min,
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
    section = "calibration.entry_policy_templates"
    cohort = args.cohort.strip() or str(cfg_get(config, f"{section}.default_cohort", ""))
    if not cohort:
        raise ValueError("Provide --cohort or calibration.entry_policy_templates.default_cohort")

    input_csv = (
        args.input_csv.expanduser().resolve()
        if args.input_csv
        else resolve_path(cfg_get(config, "calibration.cohort_neutral_backtest_csv"), base_dir=base_dir)
    )
    gate_csv = args.gate_csv.expanduser().resolve() if args.gate_csv else resolve_path(cfg_get(config, f"{section}.gate_csv"), base_dir=base_dir)
    output_csv = path_from_config(base_dir, config, f"{section}.output_csv", args.output_csv)
    recommendation_csv = path_from_config(base_dir, config, f"{section}.recommendation_csv", args.recommendation_csv)
    comparison_csv = path_from_config(base_dir, config, f"{section}.comparison_csv", args.comparison_csv)

    rows = read_csv(input_csv)
    horizons = return_horizons(rows)
    gate_rows = read_csv(gate_csv)
    validation_start = str(cfg_get(config, "calibration.validation_start_asof", "2025-06-06"))
    validation_end = str(cfg_get(config, "calibration.validation_end_asof", "2025-11-28"))
    effective_train = effective_train_end(
        str(cfg_get(config, "calibration.train_end_asof", "2025-05-30")),
        validation_start,
        int(cfg_get(config, "calibration.embargo_days", 120)),
    )
    min_selected_ticker_coverage = float(cfg_get(config, "calibration.min_selected_ticker_coverage", 0.60))
    objective_weights = cfg_get(config, "calibration.objective", {}) or {}

    base_gate = pick_broad_gate(gate_rows, cohort=cohort, min_coverage=min_selected_ticker_coverage)
    results = [
        evaluate_candidate(
            rows,
            cohort=cohort,
            base_gate=base_gate,
            candidate=candidate,
            horizons=horizons,
            train_end_asof=effective_train,
            validation_start_asof=validation_start,
            validation_end_asof=validation_end,
            min_train_obs=int(cfg_get(config, "calibration.min_train_obs", 100)),
            min_validation_obs=int(cfg_get(config, "calibration.min_validation_obs", 40)),
            min_unique_tickers=int(cfg_get(config, "calibration.min_unique_tickers", 5)),
            min_selected_validation=int(cfg_get(config, "calibration.min_selected_validation", 20)),
            min_selected_ticker_coverage=min_selected_ticker_coverage,
            min_improved_selected_ticker_rate=float(cfg_get(config, "calibration.min_improved_selected_ticker_rate", 0.60)),
            objective_weights=objective_weights,
        )
        for candidate in candidate_policies(config)
    ]
    results.sort(key=lambda item: (item["pass_fail"] == "pass", float_or_default(item["objective_score"], -999.0)), reverse=True)
    write_csv(output_csv, results, output_fields(horizons))
    write_csv(recommendation_csv, results[:1], output_fields(horizons))

    validation_rows = [
        row
        for row in rows
        if str(row.get("calibration_cohort") or "") == cohort and validation_start <= str(row.get("asof_date") or "") <= validation_end
    ]
    base_selected = [row for row in validation_rows if passes_base_gate(row, base_gate)]
    best_candidate = {
        "entry_policy_template": results[0]["entry_policy_template"],
        "technical_entry_min": to_float(results[0]["technical_entry_min"]) if results[0]["technical_entry_min"] != "" else "",
        "technical_entry_max": to_float(results[0]["technical_entry_max"]) if results[0]["technical_entry_max"] != "" else "",
        "pullback_fundamental_min": to_float(results[0]["pullback_fundamental_min"]) if results[0]["pullback_fundamental_min"] != "" else "",
        "pullback_valuation_min": to_float(results[0]["pullback_valuation_min"]) if results[0]["pullback_valuation_min"] != "" else "",
        "pullback_fda_product_min": to_float(results[0]["pullback_fda_product_min"]) if results[0]["pullback_fda_product_min"] != "" else "",
    }
    best_selected = [row for row in base_selected if passes_policy(row, best_candidate)]
    write_csv(
        comparison_csv,
        [
            comparison_row("full_validation_cohort", validation_rows, horizons=horizons),
            comparison_row("base_gate_technical_neutral", base_selected, horizons=horizons),
            comparison_row(f"best_policy_{results[0]['entry_policy_template']}", best_selected, horizons=horizons),
        ],
        comparison_fields(horizons),
    )
    print(f"entry_policy_template_grid_csv={output_csv} rows={len(results)}")
    print(f"entry_policy_template_recommendation_csv={recommendation_csv} pass_fail={results[0]['pass_fail']}")
    print(f"entry_policy_template_comparison_csv={comparison_csv}")


if __name__ == "__main__":
    main()
