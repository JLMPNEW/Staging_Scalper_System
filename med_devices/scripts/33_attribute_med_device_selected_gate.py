#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import math
import sys
from collections import Counter
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
FEATURE_FIELDS = [
    "raw_composite_score",
    "cohort_percentile",
    "fundamental_quality_score",
    "durable_growth_score",
    "fda_product_score",
    "reimbursement_score",
    "valuation_score",
    "technical_entry_score",
    "sentiment_catalyst_score",
    "value_trap_score",
    "data_completeness_score",
    "liquidity_score",
    "avg_dollar_volume_60d",
    "market_cap",
]
HIGHER_IS_BETTER_FILTERS = [
    "raw_composite_score",
    "cohort_percentile",
    "fundamental_quality_score",
    "durable_growth_score",
    "fda_product_score",
    "reimbursement_score",
    "valuation_score",
    "sentiment_catalyst_score",
    "data_completeness_score",
    "liquidity_score",
    "avg_dollar_volume_60d",
    "market_cap",
]
LOWER_IS_BETTER_FILTERS = [
    "value_trap_score",
    "technical_entry_score",
]
SUMMARY_FIELDS = [
    "calibration_cohort",
    "selected_gate_source",
    "selected_gate_parameter_set_id",
    "gate_rejection_reason",
    "selected_ticker_coverage_120d",
    "improved_selected_ticker_rate_120d",
    "selected_median_120d",
    "selected_lcb_120d",
    "selected_hit_rate_120d",
    "selected_profit_factor_120d",
    "selected_tickers",
    "improving_tickers",
    "non_improving_tickers",
    "top_single_filter_candidate",
    "recommended_next_step",
]
TICKER_FIELDS = [
    "calibration_cohort",
    "ticker",
    "company_name",
    "improved_flag",
    "selected_rows",
    "mean_excess_120d",
    "median_excess_120d",
    "hit_rate_excess_120d",
    "first_selected_asof",
    "last_selected_asof",
    "dominant_classification",
    "dominant_entry_status",
    "dominant_fda_review_state",
    "avg_raw_composite_score",
    "avg_cohort_percentile",
    "avg_fundamental_quality_score",
    "avg_durable_growth_score",
    "avg_fda_product_score",
    "avg_reimbursement_score",
    "avg_valuation_score",
    "avg_technical_entry_score",
    "avg_sentiment_catalyst_score",
    "avg_value_trap_score",
    "avg_data_completeness_score",
    "avg_liquidity_score",
    "avg_avg_dollar_volume_60d",
    "avg_market_cap",
]
PROFILE_FIELDS = [
    "calibration_cohort",
    "group",
    "ticker_count",
    "row_count",
    "median_excess_120d",
    "hit_rate_excess_120d",
    "classification_mix",
    "entry_status_mix",
    "fda_review_state_mix",
] + [f"avg_{field}" for field in FEATURE_FIELDS]
FILTER_FIELDS = [
    "calibration_cohort",
    "filter_name",
    "filter_direction",
    "threshold",
    "selected_ticker_coverage_120d",
    "improved_selected_ticker_rate_120d",
    "count_120d",
    "unique_tickers_120d",
    "mean_120d",
    "median_120d",
    "hit_rate_120d",
    "lcb_120d",
    "profit_factor_120d",
    "selected_tickers",
    "pass_candidate",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Attribute selected ticker behavior for a broad med-device gate.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--cohort", type=str, default="")
    parser.add_argument("--input-csv", type=Path, default=None)
    parser.add_argument("--grid-csv", type=Path, default=None)
    parser.add_argument("--summary-csv", type=Path, default=None)
    parser.add_argument("--ticker-csv", type=Path, default=None)
    parser.add_argument("--profile-csv", type=Path, default=None)
    parser.add_argument("--filter-csv", type=Path, default=None)
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


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def write_csv(path: Path, rows: list[dict[str, Any]], fields: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore", lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


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


def metric_values(rows: list[dict[str, str]], *, horizon: int = 120) -> list[float]:
    out: list[float] = []
    for row in rows:
        value = to_float(row.get(f"cohort_excess_return_{horizon}d"))
        if value is not None:
            out.append(value)
    return out


def ticker_set(rows: list[dict[str, str]], *, horizon: int = 120) -> set[str]:
    out: set[str] = set()
    for row in rows:
        if to_float(row.get(f"cohort_excess_return_{horizon}d")) is not None and str(row.get("ticker") or ""):
            out.add(str(row["ticker"]))
    return out


def metrics(rows: list[dict[str, str]], *, horizon: int = 120) -> dict[str, Any]:
    values = metric_values(rows, horizon=horizon)
    tickers = ticker_set(rows, horizon=horizon)
    if not values:
        return {
            "count": 0,
            "unique_tickers": 0,
            "mean": "",
            "median": "",
            "hit_rate": "",
            "lcb": "",
            "profit_factor": "",
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
        "count": len(values),
        "unique_tickers": len(tickers),
        "mean": f"{avg:.6f}",
        "median": f"{median(values):.6f}",
        "hit_rate": f"{sum(1 for value in values if value > 0) / len(values):.4f}",
        "lcb": f"{lcb:.6f}",
        "profit_factor": f"{profit_factor:.4f}",
    }


def selected_ticker_improvement_rate(rows: list[dict[str, str]], *, horizon: int = 120) -> float | None:
    grouped: dict[str, list[float]] = {}
    for row in rows:
        value = to_float(row.get(f"cohort_excess_return_{horizon}d"))
        ticker = str(row.get("ticker") or "")
        if value is not None and ticker:
            grouped.setdefault(ticker, []).append(value)
    if not grouped:
        return None
    return sum(1 for values in grouped.values() if median(values) > 0) / len(grouped)


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


def passes_gate(row: dict[str, str], gate: dict[str, str]) -> bool:
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


def avg(rows: list[dict[str, str]], field: str) -> str:
    values = [value for row in rows if (value := to_float(row.get(field))) is not None]
    return "" if not values else f"{mean(values):.4f}"


def mode_text(rows: list[dict[str, str]], field: str) -> str:
    counter = Counter(str(row.get(field) or "<blank>") for row in rows)
    return "" if not counter else counter.most_common(1)[0][0]


def counter_text(rows: list[dict[str, str]], field: str) -> str:
    counter = Counter(str(row.get(field) or "<blank>") for row in rows)
    return ";".join(f"{key}:{counter[key]}" for key in sorted(counter, key=lambda key: (-counter[key], key))[:8])


def build_ticker_rows(rows: list[dict[str, str]], *, cohort: str) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, str]]] = {}
    for row in rows:
        ticker = str(row.get("ticker") or "")
        if ticker:
            grouped.setdefault(ticker, []).append(row)
    out: list[dict[str, Any]] = []
    for ticker, ticker_rows in sorted(grouped.items()):
        payload = metrics(ticker_rows)
        values = metric_values(ticker_rows)
        asofs = sorted(str(row.get("asof_date") or "") for row in ticker_rows if str(row.get("asof_date") or ""))
        item: dict[str, Any] = {
            "calibration_cohort": cohort,
            "ticker": ticker,
            "company_name": ticker_rows[0].get("company_name", ""),
            "improved_flag": int(bool(values) and median(values) > 0),
            "selected_rows": payload["count"],
            "mean_excess_120d": payload["mean"],
            "median_excess_120d": payload["median"],
            "hit_rate_excess_120d": payload["hit_rate"],
            "first_selected_asof": asofs[0] if asofs else "",
            "last_selected_asof": asofs[-1] if asofs else "",
            "dominant_classification": mode_text(ticker_rows, "classification"),
            "dominant_entry_status": mode_text(ticker_rows, "entry_status"),
            "dominant_fda_review_state": mode_text(ticker_rows, "fda_review_state"),
        }
        for field in FEATURE_FIELDS:
            item[f"avg_{field}"] = avg(ticker_rows, field)
        out.append(item)
    out.sort(key=lambda row: (int(row["improved_flag"]), to_float(row["median_excess_120d"]) or -999.0), reverse=True)
    return out


def build_profile_rows(rows: list[dict[str, str]], ticker_rows: list[dict[str, Any]], *, cohort: str) -> list[dict[str, Any]]:
    improving = {str(row["ticker"]) for row in ticker_rows if int(row["improved_flag"]) == 1}
    non_improving = {str(row["ticker"]) for row in ticker_rows if int(row["improved_flag"]) == 0}
    groups = {
        "all_selected": rows,
        "improving_selected_tickers": [row for row in rows if str(row.get("ticker") or "") in improving],
        "non_improving_selected_tickers": [row for row in rows if str(row.get("ticker") or "") in non_improving],
    }
    out: list[dict[str, Any]] = []
    for group, group_rows in groups.items():
        payload = metrics(group_rows)
        item: dict[str, Any] = {
            "calibration_cohort": cohort,
            "group": group,
            "ticker_count": payload["unique_tickers"],
            "row_count": payload["count"],
            "median_excess_120d": payload["median"],
            "hit_rate_excess_120d": payload["hit_rate"],
            "classification_mix": counter_text(group_rows, "classification"),
            "entry_status_mix": counter_text(group_rows, "entry_status"),
            "fda_review_state_mix": counter_text(group_rows, "fda_review_state"),
        }
        for field in FEATURE_FIELDS:
            item[f"avg_{field}"] = avg(group_rows, field)
        out.append(item)
    return out


def quantile_thresholds(values: list[float]) -> list[float]:
    if not values:
        return []
    values = sorted(values)
    out: list[float] = []
    for frac in (0.25, 0.50, 0.75):
        idx = min(len(values) - 1, max(0, round(frac * (len(values) - 1))))
        out.append(values[idx])
    return sorted(set(out))


def build_filter_rows(
    rows: list[dict[str, str]],
    *,
    cohort: str,
    validation_ticker_count: int,
    min_coverage: float,
    min_improved_rate: float,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    tests: list[tuple[str, str, float]] = []
    for field in HIGHER_IS_BETTER_FILTERS:
        values = [value for row in rows if (value := to_float(row.get(field))) is not None]
        tests.extend((field, ">=", threshold) for threshold in quantile_thresholds(values))
    for field in LOWER_IS_BETTER_FILTERS:
        values = [value for row in rows if (value := to_float(row.get(field))) is not None]
        tests.extend((field, "<=", threshold) for threshold in quantile_thresholds(values))
    for field, direction, threshold in tests:
        if direction == ">=":
            filtered = [row for row in rows if (to_float(row.get(field)) is not None and (to_float(row.get(field)) or 0.0) >= threshold)]
        else:
            filtered = [row for row in rows if (to_float(row.get(field)) is not None and (to_float(row.get(field)) or 0.0) <= threshold)]
        payload = metrics(filtered)
        selected_tickers = sorted(ticker_set(filtered))
        coverage = len(selected_tickers) / validation_ticker_count if validation_ticker_count else 0.0
        improved = selected_ticker_improvement_rate(filtered)
        median_value = to_float(payload["median"]) or 0.0
        lcb = to_float(payload["lcb"]) or 0.0
        item = {
            "calibration_cohort": cohort,
            "filter_name": field,
            "filter_direction": direction,
            "threshold": f"{threshold:.6f}",
            "selected_ticker_coverage_120d": f"{coverage:.4f}",
            "improved_selected_ticker_rate_120d": "" if improved is None else f"{improved:.4f}",
            "count_120d": payload["count"],
            "unique_tickers_120d": payload["unique_tickers"],
            "mean_120d": payload["mean"],
            "median_120d": payload["median"],
            "hit_rate_120d": payload["hit_rate"],
            "lcb_120d": payload["lcb"],
            "profit_factor_120d": payload["profit_factor"],
            "selected_tickers": ";".join(selected_tickers),
            "pass_candidate": int(
                coverage >= min_coverage
                and (improved is not None and improved >= min_improved_rate)
                and median_value > 0
                and lcb > 0
            ),
        }
        out.append(item)
    out.sort(
        key=lambda row: (
            int(row["pass_candidate"]),
            to_float(row["improved_selected_ticker_rate_120d"]) or -1.0,
            to_float(row["median_120d"]) or -999.0,
            to_float(row["lcb_120d"]) or -999.0,
        ),
        reverse=True,
    )
    return out


def output_path(base_dir: Path, config: dict[str, Any], key: str, cohort: str, explicit: Path | None) -> Path:
    if explicit:
        return explicit.expanduser().resolve()
    raw = str(cfg_get(config, key, ""))
    if raw:
        return resolve_path(raw.format(cohort=cohort), base_dir=base_dir)
    name = key.rsplit(".", 1)[-1].replace("_csv", "")
    return resolve_path(f"../output/med_devices_reports/calibration/med_device_{cohort}_{name}.csv", base_dir=base_dir)


def main() -> None:
    configure_utc_logging()
    args = parse_args()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    base_dir = config_path.parent
    cohort = args.cohort.strip() or str(cfg_get(config, "calibration.selected_gate_attribution.default_cohort", ""))
    if not cohort:
        raise ValueError("Provide --cohort or calibration.selected_gate_attribution.default_cohort")
    input_csv = (
        args.input_csv.expanduser().resolve()
        if args.input_csv
        else resolve_path(cfg_get(config, "calibration.cohort_neutral_backtest_csv"), base_dir=base_dir)
    )
    grid_csv = (
        args.grid_csv.expanduser().resolve()
        if args.grid_csv
        else resolve_path(cfg_get(config, "calibration.selected_gate_attribution.grid_csv"), base_dir=base_dir)
    )
    summary_csv = output_path(base_dir, config, "calibration.selected_gate_attribution.summary_csv", cohort, args.summary_csv)
    ticker_csv = output_path(base_dir, config, "calibration.selected_gate_attribution.ticker_csv", cohort, args.ticker_csv)
    profile_csv = output_path(base_dir, config, "calibration.selected_gate_attribution.profile_csv", cohort, args.profile_csv)
    filter_csv = output_path(base_dir, config, "calibration.selected_gate_attribution.filter_csv", cohort, args.filter_csv)

    rows = read_csv(input_csv)
    grid_rows = read_csv(grid_csv)
    validation_start = str(cfg_get(config, "calibration.validation_start_asof", "2025-06-06"))
    validation_end = str(cfg_get(config, "calibration.validation_end_asof", "2025-11-28"))
    _ = effective_train_end(
        str(cfg_get(config, "calibration.train_end_asof", "2025-05-30")),
        validation_start,
        int(cfg_get(config, "calibration.embargo_days", 120)),
    )
    min_coverage = float(cfg_get(config, "calibration.min_selected_ticker_coverage", 0.60))
    min_improved_rate = float(cfg_get(config, "calibration.min_improved_selected_ticker_rate", 0.60))

    gate = pick_broad_gate(grid_rows, cohort=cohort, min_coverage=min_coverage)
    validation_rows = [
        row
        for row in rows
        if str(row.get("calibration_cohort") or "") == cohort and validation_start <= str(row.get("asof_date") or "") <= validation_end
    ]
    selected_rows = [row for row in validation_rows if passes_gate(row, gate)]
    validation_tickers = sorted(ticker_set(validation_rows))
    ticker_rows = build_ticker_rows(selected_rows, cohort=cohort)
    improving = sorted(str(row["ticker"]) for row in ticker_rows if int(row["improved_flag"]) == 1)
    non_improving = sorted(str(row["ticker"]) for row in ticker_rows if int(row["improved_flag"]) == 0)
    filter_rows = build_filter_rows(
        selected_rows,
        cohort=cohort,
        validation_ticker_count=len(validation_tickers),
        min_coverage=min_coverage,
        min_improved_rate=min_improved_rate,
    )
    payload = metrics(selected_rows)
    improved_rate = selected_ticker_improvement_rate(selected_rows)
    pass_filters = [row for row in filter_rows if int(row["pass_candidate"]) == 1]
    summary = [
        {
            "calibration_cohort": cohort,
            "selected_gate_source": str(grid_csv),
            "selected_gate_parameter_set_id": gate.get("parameter_set_id", ""),
            "gate_rejection_reason": gate.get("rejection_reason", ""),
            "selected_ticker_coverage_120d": gate.get("validation_selected_ticker_coverage_120d", ""),
            "improved_selected_ticker_rate_120d": "" if improved_rate is None else f"{improved_rate:.4f}",
            "selected_median_120d": payload["median"],
            "selected_lcb_120d": payload["lcb"],
            "selected_hit_rate_120d": payload["hit_rate"],
            "selected_profit_factor_120d": payload["profit_factor"],
            "selected_tickers": ";".join(sorted(ticker_set(selected_rows))),
            "improving_tickers": ";".join(improving),
            "non_improving_tickers": ";".join(non_improving),
            "top_single_filter_candidate": (
                ""
                if not filter_rows
                else f"{filter_rows[0]['filter_name']} {filter_rows[0]['filter_direction']} {filter_rows[0]['threshold']}"
                f" pass={filter_rows[0]['pass_candidate']}"
            ),
            "recommended_next_step": (
                "A simple extra filter produced a passing candidate; review economics before promotion."
                if pass_filters
                else "No one-filter cleanup fixed the improved-ticker failure; keep default config and improve feature definitions."
            ),
        }
    ]
    profile_rows = build_profile_rows(selected_rows, ticker_rows, cohort=cohort)
    write_csv(summary_csv, summary, SUMMARY_FIELDS)
    write_csv(ticker_csv, ticker_rows, TICKER_FIELDS)
    write_csv(profile_csv, profile_rows, PROFILE_FIELDS)
    write_csv(filter_csv, filter_rows, FILTER_FIELDS)
    print(f"selected_gate_attribution_summary_csv={summary_csv}")
    print(f"selected_gate_attribution_ticker_csv={ticker_csv} rows={len(ticker_rows)}")
    print(f"selected_gate_attribution_profile_csv={profile_csv} rows={len(profile_rows)}")
    print(f"selected_gate_attribution_filter_csv={filter_csv} rows={len(filter_rows)}")


if __name__ == "__main__":
    main()
