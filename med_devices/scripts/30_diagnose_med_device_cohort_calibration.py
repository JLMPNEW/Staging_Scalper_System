#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
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
COMPONENT_FIELDS = [
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
]
SUMMARY_FIELDS = [
    "calibration_cohort",
    "diagnosis",
    "gate_source",
    "pass_fail",
    "rejection_reason",
    "validation_tickers",
    "selected_tickers",
    "selected_ticker_coverage_120d",
    "improved_selected_ticker_rate_120d",
    "validation_median_120d",
    "selected_median_120d",
    "selected_lcb_120d",
    "selected_hit_rate_120d",
    "selected_profit_factor_120d",
    "positive_components_120d",
    "negative_or_weak_components_120d",
    "recommended_next_step",
]
COMPONENT_OUTPUT_FIELDS = [
    "calibration_cohort",
    "sample",
    "horizon_days",
    "component",
    "count",
    "unique_tickers",
    "spearman_ic_excess",
    "pearson_ic_excess",
    "top_quintile_median_excess",
    "bottom_quintile_median_excess",
    "top_minus_bottom_median_excess",
    "top_quintile_hit_rate_excess",
    "bottom_quintile_hit_rate_excess",
    "recommendation",
]
EXCLUSION_FIELDS = [
    "calibration_cohort",
    "sample",
    "gate_reason",
    "row_count",
    "unique_tickers",
    "median_excess_120d",
    "hit_rate_excess_120d",
    "ticker_inventory",
]
TICKER_FIELDS = [
    "calibration_cohort",
    "ticker",
    "company_name",
    "validation_rows",
    "selected_rows",
    "selected_rate",
    "mean_excess_120d",
    "median_excess_120d",
    "hit_rate_excess_120d",
    "selected_mean_excess_120d",
    "selected_median_excess_120d",
    "selected_hit_rate_excess_120d",
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
    "dominant_exclusion_reasons",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Diagnose why a med-device cohort is or is not ready for calibration promotion.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--cohort", type=str, default="")
    parser.add_argument("--input-csv", type=Path, default=None)
    parser.add_argument("--gate-csv", type=Path, default=None)
    parser.add_argument("--summary-csv", type=Path, default=None)
    parser.add_argument("--component-csv", type=Path, default=None)
    parser.add_argument("--exclusion-csv", type=Path, default=None)
    parser.add_argument("--ticker-csv", type=Path, default=None)
    return parser.parse_args()


def to_float(raw: object) -> float | None:
    try:
        value = float(str(raw).strip())
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def int_flag(raw: object) -> int:
    text = str(raw or "").strip().lower()
    return 1 if text in {"1", "true", "yes", "y", "on"} or raw == 1 else 0


def fmt(value: float | None, digits: int = 6) -> str:
    return "" if value is None else f"{value:.{digits}f}"


def float_or_default(raw: object, default: float) -> float:
    value = to_float(raw)
    return default if value is None else value


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


def fractional_rank(values: list[float]) -> list[float]:
    indexed = sorted(enumerate(values), key=lambda item: item[1])
    ranks = [0.0] * len(values)
    idx = 0
    while idx < len(indexed):
        end = idx
        while end + 1 < len(indexed) and indexed[end + 1][1] == indexed[idx][1]:
            end += 1
        avg_rank = (idx + end) / 2.0 + 1.0
        for pos in range(idx, end + 1):
            ranks[indexed[pos][0]] = avg_rank
        idx = end + 1
    return ranks


def correlation(xs: list[float], ys: list[float]) -> float | None:
    if len(xs) < 5 or len(xs) != len(ys):
        return None
    mx = mean(xs)
    my = mean(ys)
    cov = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    sx = math.sqrt(sum((x - mx) ** 2 for x in xs))
    sy = math.sqrt(sum((y - my) ** 2 for y in ys))
    if sx <= 1e-12 or sy <= 1e-12:
        return 0.0
    return max(-1.0, min(1.0, cov / (sx * sy)))


def spearman(xs: list[float], ys: list[float]) -> float | None:
    return correlation(fractional_rank(xs), fractional_rank(ys))


def hit_rate(values: list[float]) -> float | None:
    return None if not values else sum(1 for value in values if value > 0) / len(values)


def metric_payload(rows: list[dict[str, str]], *, horizon: int = 120) -> dict[str, Any]:
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
        "hit_rate": f"{(hit_rate(values) or 0.0):.4f}",
        "lcb": f"{lcb:.6f}",
        "profit_factor": f"{profit_factor:.4f}",
    }


def recommendation(count: int, ic: float | None, spread: float | None) -> str:
    if count < 50 or ic is None or spread is None:
        return "insufficient_observations"
    if ic > 0.05 and spread > 0:
        return "positive_candidate_factor"
    if ic < -0.05 and spread < 0:
        return "negative_or_inverse_factor"
    return "weak_or_unstable_factor"


def analyze_component(rows: list[dict[str, str]], *, cohort: str, sample: str, horizon: int, component: str) -> dict[str, Any]:
    pairs: list[tuple[str, float, float]] = []
    for row in rows:
        if str(row.get("calibration_cohort") or "") != cohort:
            continue
        component_value = to_float(row.get(component))
        excess = to_float(row.get(f"cohort_excess_return_{horizon}d"))
        if component_value is None or excess is None:
            continue
        pairs.append((str(row.get("ticker") or ""), component_value, excess))
    xs = [item[1] for item in pairs]
    ys = [item[2] for item in pairs]
    sorted_pairs = sorted(pairs, key=lambda item: item[1])
    quintile_n = max(1, len(sorted_pairs) // 5) if sorted_pairs else 0
    bottom = [item[2] for item in sorted_pairs[:quintile_n]]
    top = [item[2] for item in sorted_pairs[-quintile_n:]]
    top_med = median(top) if top else None
    bottom_med = median(bottom) if bottom else None
    spread = (top_med - bottom_med) if top_med is not None and bottom_med is not None else None
    ic = spearman(xs, ys)
    return {
        "calibration_cohort": cohort,
        "sample": sample,
        "horizon_days": horizon,
        "component": component,
        "count": len(pairs),
        "unique_tickers": len({item[0] for item in pairs}),
        "spearman_ic_excess": fmt(ic),
        "pearson_ic_excess": fmt(correlation(xs, ys)),
        "top_quintile_median_excess": fmt(top_med),
        "bottom_quintile_median_excess": fmt(bottom_med),
        "top_minus_bottom_median_excess": fmt(spread),
        "top_quintile_hit_rate_excess": fmt(hit_rate(top), 4),
        "bottom_quintile_hit_rate_excess": fmt(hit_rate(bottom), 4),
        "recommendation": recommendation(len(pairs), ic, spread),
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


def gate_config_from_row(row: dict[str, str]) -> dict[str, Any]:
    return {
        "raw_score_min": float_or_default(row.get("raw_score_min"), 55.0),
        "cohort_percentile_min": float_or_default(row.get("cohort_percentile_min"), 60.0),
        "value_trap_max": float_or_default(row.get("value_trap_max"), 40.0),
        "min_avg_dollar_volume_60d": float_or_default(row.get("min_avg_dollar_volume_60d"), 0.0),
        "data_completeness_min": float_or_default(row.get("data_completeness_min"), 90.0),
        "entry_status_policy": str(row.get("entry_status_policy") or "entry_eligible_or_setup"),
        "fda_review_policy": str(row.get("fda_review_policy") or "exclude_manual_hard_red"),
        "reimbursement_policy": str(row.get("reimbursement_policy") or "all_known"),
    }


def current_default_gate(config: dict[str, Any]) -> dict[str, Any]:
    gates = cfg_get(config, "scoring.gates", {}) or {}
    return {
        "raw_score_min": float(gates.get("composite_min", 75.0)),
        "cohort_percentile_min": 0.0,
        "value_trap_max": float(gates.get("value_trap_max", 20.0)),
        "min_avg_dollar_volume_60d": float(gates.get("min_avg_dollar_volume_60d", 1_000_000.0)),
        "data_completeness_min": float(gates.get("data_completeness_min", 90.0)),
        "entry_status_policy": "entry_eligible_or_setup",
        "fda_review_policy": "exclude_manual_hard_red",
        "reimbursement_policy": "all_known",
    }


def exclusion_reasons(row: dict[str, str], gate: dict[str, Any]) -> list[str]:
    reasons: list[str] = []
    if str(row.get("classification") or "") in REVIEW_CLASSIFICATIONS:
        reasons.append("review_or_data_classification")
    raw = to_float(row.get("raw_composite_score"))
    if raw is None or raw < float(gate["raw_score_min"]):
        reasons.append("raw_score_below_gate")
    pct = to_float(row.get("cohort_percentile"))
    if pct is None or pct < float(gate["cohort_percentile_min"]):
        reasons.append("cohort_percentile_below_gate")
    value_trap = to_float(row.get("value_trap_score"))
    if value_trap is not None and value_trap > float(gate["value_trap_max"]):
        reasons.append("value_trap_above_gate")
    adv = to_float(row.get("avg_dollar_volume_60d"))
    if float(gate["min_avg_dollar_volume_60d"]) > 0 and (adv is None or adv < float(gate["min_avg_dollar_volume_60d"])):
        reasons.append("liquidity_below_gate")
    completeness = to_float(row.get("data_completeness_score"))
    if completeness is None or completeness < float(gate["data_completeness_min"]):
        reasons.append("data_completeness_below_gate")

    entry = str(row.get("entry_status") or "")
    entry_policy = str(gate["entry_status_policy"])
    if entry_policy == "entry_eligible_only" and entry != "entry_eligible":
        reasons.append("entry_status_not_eligible")
    elif entry_policy == "entry_eligible_or_setup" and entry not in {"entry_eligible", "watch_for_setup"}:
        reasons.append("entry_status_not_eligible_or_setup")

    fda_state = str(row.get("fda_review_state") or "").strip().lower()
    fda_policy = str(gate["fda_review_policy"])
    if fda_policy == "clean_or_cleared_only" and fda_state not in {"", "clean", "cleared", "low_materiality"}:
        reasons.append("fda_not_clean_or_cleared")
    elif fda_policy == "exclude_manual_hard_red" and (fda_state in MANUAL_FDA_STATES or int_flag(row.get("hard_red_flag"))):
        reasons.append("fda_manual_or_hard_red")

    reimb_policy = str(gate["reimbursement_policy"])
    if reimb_policy == "all_known" and not reimbursement_live(row):
        reasons.append("reimbursement_not_live")
    elif reimb_policy == "live_evidence_only" and (not reimbursement_live(row) or not has_reimbursement_evidence(row)):
        reasons.append("reimbursement_missing_evidence")
    elif reimb_policy == "direct_or_bundled_or_capital" and not any(
        int_flag(row.get(field))
        for field in ("direct_code_evidence", "payment_rate_evidence", "procedure_bundled_flag", "capital_equipment_flag")
    ):
        reasons.append("reimbursement_not_direct_bundled_or_capital")
    return reasons


def passes_gate(row: dict[str, str], gate: dict[str, Any]) -> bool:
    return not exclusion_reasons(row, gate)


def ticker_set(rows: list[dict[str, str]], *, horizon: int = 120) -> set[str]:
    out: set[str] = set()
    for row in rows:
        if to_float(row.get(f"cohort_excess_return_{horizon}d")) is not None and str(row.get("ticker") or ""):
            out.add(str(row["ticker"]))
    return out


def improved_ticker_rate(rows: list[dict[str, str]], *, horizon: int = 120) -> float | None:
    grouped: dict[str, list[float]] = {}
    for row in rows:
        value = to_float(row.get(f"cohort_excess_return_{horizon}d"))
        ticker = str(row.get("ticker") or "")
        if value is not None and ticker:
            grouped.setdefault(ticker, []).append(value)
    if not grouped:
        return None
    return sum(1 for values in grouped.values() if median(values) > 0) / len(grouped)


def build_exclusion_rows(rows: list[dict[str, str]], *, cohort: str, gate: dict[str, Any]) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, str]]] = {}
    selected: list[dict[str, str]] = []
    for row in rows:
        reasons = exclusion_reasons(row, gate)
        if not reasons:
            selected.append(row)
        for reason in reasons:
            grouped.setdefault(reason, []).append(row)
    grouped["selected_by_gate"] = selected
    out: list[dict[str, Any]] = []
    for reason, reason_rows in sorted(grouped.items()):
        payload = metric_payload(reason_rows, horizon=120)
        tickers = sorted(ticker_set(reason_rows, horizon=120))
        out.append(
            {
                "calibration_cohort": cohort,
                "sample": "validation",
                "gate_reason": reason,
                "row_count": payload["count"],
                "unique_tickers": payload["unique_tickers"],
                "median_excess_120d": payload["median"],
                "hit_rate_excess_120d": payload["hit_rate"],
                "ticker_inventory": ";".join(tickers),
            }
        )
    return out


def avg(rows: list[dict[str, str]], field: str) -> str:
    values = [value for row in rows if (value := to_float(row.get(field))) is not None]
    return "" if not values else f"{mean(values):.4f}"


def build_ticker_rows(rows: list[dict[str, str]], *, cohort: str, gate: dict[str, Any]) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, str]]] = {}
    for row in rows:
        ticker = str(row.get("ticker") or "")
        if ticker:
            grouped.setdefault(ticker, []).append(row)
    out: list[dict[str, Any]] = []
    for ticker, ticker_rows in sorted(grouped.items()):
        selected = [row for row in ticker_rows if passes_gate(row, gate)]
        payload = metric_payload(ticker_rows, horizon=120)
        selected_payload = metric_payload(selected, horizon=120)
        reason_counts: dict[str, int] = {}
        for row in ticker_rows:
            for reason in exclusion_reasons(row, gate):
                reason_counts[reason] = reason_counts.get(reason, 0) + 1
        reasons = ";".join(f"{key}:{reason_counts[key]}" for key in sorted(reason_counts, key=lambda key: (-reason_counts[key], key))[:5])
        out.append(
            {
                "calibration_cohort": cohort,
                "ticker": ticker,
                "company_name": ticker_rows[0].get("company_name", ""),
                "validation_rows": len(ticker_rows),
                "selected_rows": len(selected),
                "selected_rate": f"{len(selected) / len(ticker_rows):.4f}" if ticker_rows else "",
                "mean_excess_120d": payload["mean"],
                "median_excess_120d": payload["median"],
                "hit_rate_excess_120d": payload["hit_rate"],
                "selected_mean_excess_120d": selected_payload["mean"],
                "selected_median_excess_120d": selected_payload["median"],
                "selected_hit_rate_excess_120d": selected_payload["hit_rate"],
                "avg_raw_composite_score": avg(ticker_rows, "raw_composite_score"),
                "avg_cohort_percentile": avg(ticker_rows, "cohort_percentile"),
                "avg_fundamental_quality_score": avg(ticker_rows, "fundamental_quality_score"),
                "avg_durable_growth_score": avg(ticker_rows, "durable_growth_score"),
                "avg_fda_product_score": avg(ticker_rows, "fda_product_score"),
                "avg_reimbursement_score": avg(ticker_rows, "reimbursement_score"),
                "avg_valuation_score": avg(ticker_rows, "valuation_score"),
                "avg_technical_entry_score": avg(ticker_rows, "technical_entry_score"),
                "avg_sentiment_catalyst_score": avg(ticker_rows, "sentiment_catalyst_score"),
                "avg_value_trap_score": avg(ticker_rows, "value_trap_score"),
                "dominant_exclusion_reasons": reasons,
            }
        )
    out.sort(key=lambda row: (to_float(row["median_excess_120d"]) or -999.0), reverse=True)
    return out


def build_summary(
    *,
    cohort: str,
    gate_source: str,
    gate_row: dict[str, str] | None,
    validation_rows: list[dict[str, str]],
    selected_rows: list[dict[str, str]],
    component_rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    validation_payload = metric_payload(validation_rows, horizon=120)
    selected_payload = metric_payload(selected_rows, horizon=120)
    validation_tickers = sorted(ticker_set(validation_rows, horizon=120))
    selected_tickers = sorted(ticker_set(selected_rows, horizon=120))
    selected_coverage = len(selected_tickers) / len(validation_tickers) if validation_tickers else 0.0
    improved = improved_ticker_rate(selected_rows, horizon=120)
    positives = sorted(
        {
            str(row["component"])
            for row in component_rows
            if row["sample"] == "validation" and row["horizon_days"] == 120 and row["recommendation"] == "positive_candidate_factor"
        }
    )
    weak = sorted(
        {
            str(row["component"])
            for row in component_rows
            if row["sample"] == "validation"
            and row["horizon_days"] == 120
            and row["recommendation"] in {"negative_or_inverse_factor", "weak_or_unstable_factor"}
        }
    )
    if (gate_row or {}).get("pass_fail") == "pass":
        diagnosis = "promotion_candidate"
        next_step = "Review selected tickers manually before copying gates into config."
    elif selected_coverage < 0.60:
        diagnosis = "do_not_promote_gate_too_concentrated"
        next_step = "Keep global config; inspect feature definitions and exclusion reasons before another optimization."
    else:
        diagnosis = "do_not_promote_feature_or_validation_gap"
        next_step = "Keep global config; improve weak feature sleeves and rerun validation."
    return [
        {
            "calibration_cohort": cohort,
            "diagnosis": diagnosis,
            "gate_source": gate_source,
            "pass_fail": (gate_row or {}).get("pass_fail", ""),
            "rejection_reason": (gate_row or {}).get("rejection_reason", ""),
            "validation_tickers": ";".join(validation_tickers),
            "selected_tickers": ";".join(selected_tickers),
            "selected_ticker_coverage_120d": f"{selected_coverage:.4f}",
            "improved_selected_ticker_rate_120d": "" if improved is None else f"{improved:.4f}",
            "validation_median_120d": validation_payload["median"],
            "selected_median_120d": selected_payload["median"],
            "selected_lcb_120d": selected_payload["lcb"],
            "selected_hit_rate_120d": selected_payload["hit_rate"],
            "selected_profit_factor_120d": selected_payload["profit_factor"],
            "positive_components_120d": ";".join(positives),
            "negative_or_weak_components_120d": ";".join(weak),
            "recommended_next_step": next_step,
        }
    ]


def output_path(base_dir: Path, config: dict[str, Any], key: str, cohort: str, explicit: Path | None) -> Path:
    if explicit:
        return explicit.expanduser().resolve()
    raw = str(cfg_get(config, key, ""))
    if raw:
        return resolve_path(raw.format(cohort=cohort), base_dir=base_dir)
    filename = key.rsplit(".", 1)[-1].replace("_csv", "")
    return resolve_path(f"../output/med_devices_reports/calibration/med_device_{cohort}_{filename}.csv", base_dir=base_dir)


def main() -> None:
    configure_utc_logging()
    args = parse_args()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    base_dir = config_path.parent
    cohort = args.cohort.strip() or str(cfg_get(config, "calibration.diagnosis.default_cohort", ""))
    if not cohort:
        raise ValueError("Provide --cohort or calibration.diagnosis.default_cohort")
    input_csv = (
        args.input_csv.expanduser().resolve()
        if args.input_csv
        else resolve_path(cfg_get(config, "calibration.cohort_neutral_backtest_csv"), base_dir=base_dir)
    )
    gate_csv = (
        args.gate_csv.expanduser().resolve()
        if args.gate_csv
        else resolve_path(cfg_get(config, "calibration.diagnosis.gate_csv", ""), base_dir=base_dir)
        if cfg_get(config, "calibration.diagnosis.gate_csv", "")
        else None
    )
    summary_csv = output_path(base_dir, config, "calibration.diagnosis.summary_csv", cohort, args.summary_csv)
    component_csv = output_path(base_dir, config, "calibration.diagnosis.component_csv", cohort, args.component_csv)
    exclusion_csv = output_path(base_dir, config, "calibration.diagnosis.exclusion_csv", cohort, args.exclusion_csv)
    ticker_csv = output_path(base_dir, config, "calibration.diagnosis.ticker_csv", cohort, args.ticker_csv)

    rows = read_csv(input_csv)
    horizons = return_horizons(rows)
    train_end = effective_train_end(
        str(cfg_get(config, "calibration.train_end_asof", "2025-05-30")),
        str(cfg_get(config, "calibration.validation_start_asof", "2025-06-06")),
        int(cfg_get(config, "calibration.embargo_days", 120)),
    )
    validation_start = str(cfg_get(config, "calibration.validation_start_asof", "2025-06-06"))
    validation_end = str(cfg_get(config, "calibration.validation_end_asof", "2025-11-28"))
    cohort_rows = [row for row in rows if str(row.get("calibration_cohort") or "") == cohort]
    train_rows = [row for row in cohort_rows if str(row.get("asof_date") or "") <= train_end]
    validation_rows = [row for row in cohort_rows if validation_start <= str(row.get("asof_date") or "") <= validation_end]

    gate_row: dict[str, str] | None = None
    gate_source = "default_global_gate"
    if gate_csv and gate_csv.exists():
        gate_rows = read_csv(gate_csv)
        gate_row = gate_rows[0] if gate_rows else None
        gate_cohort = str((gate_row or {}).get("calibration_cohort") or "")
        if gate_cohort and gate_cohort != cohort:
            raise ValueError(f"Gate file cohort {gate_cohort!r} does not match requested cohort {cohort!r}")
        gate_source = str(gate_csv)
    gate = gate_config_from_row(gate_row) if gate_row else current_default_gate(config)
    selected_rows = [row for row in validation_rows if passes_gate(row, gate)]

    component_rows = [
        analyze_component(sample_rows, cohort=cohort, sample=sample, horizon=horizon, component=component)
        for sample, sample_rows in (("train", train_rows), ("validation", validation_rows))
        for horizon in horizons
        for component in COMPONENT_FIELDS
    ]
    exclusion_rows = build_exclusion_rows(validation_rows, cohort=cohort, gate=gate)
    ticker_rows = build_ticker_rows(validation_rows, cohort=cohort, gate=gate)
    summary_rows = build_summary(
        cohort=cohort,
        gate_source=gate_source,
        gate_row=gate_row,
        validation_rows=validation_rows,
        selected_rows=selected_rows,
        component_rows=component_rows,
    )

    write_csv(summary_csv, summary_rows, SUMMARY_FIELDS)
    write_csv(component_csv, component_rows, COMPONENT_OUTPUT_FIELDS)
    write_csv(exclusion_csv, exclusion_rows, EXCLUSION_FIELDS)
    write_csv(ticker_csv, ticker_rows, TICKER_FIELDS)
    print(f"diagnosis_summary_csv={summary_csv}")
    print(f"diagnosis_component_csv={component_csv} rows={len(component_rows)}")
    print(f"diagnosis_exclusion_csv={exclusion_csv} rows={len(exclusion_rows)}")
    print(f"diagnosis_ticker_csv={ticker_csv} rows={len(ticker_rows)}")


if __name__ == "__main__":
    main()
