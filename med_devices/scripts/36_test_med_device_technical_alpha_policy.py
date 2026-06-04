#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import math
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from statistics import mean, median
from typing import Any


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from med_devices.core.config import cfg_get, load_yaml, resolve_path  # noqa: E402
from med_devices.core.fda_states import MANUAL_FDA_REVIEW_STATES  # noqa: E402
from med_devices.core.logging_utils import configure_utc_logging  # noqa: E402


DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
DEFAULT_WEIGHTS = {
    "fundamental_quality": 0.25,
    "durable_growth": 0.15,
    "fda_product": 0.15,
    "reimbursement": 0.10,
    "valuation": 0.20,
    "technical_entry": 0.10,
    "sentiment_catalyst": 0.05,
}
COMPONENT_FIELDS = {
    "fundamental_quality": "fundamental_quality_score",
    "durable_growth": "durable_growth_score",
    "fda_product": "fda_product_score",
    "reimbursement": "reimbursement_score",
    "valuation": "valuation_score",
    "sentiment_catalyst": "sentiment_catalyst_score",
}
NON_LIVE_REIMBURSEMENT_STATUSES = {"", "unknown", "cms_data_not_loaded"}
SUMMARY_FIELDS = [
    "variant_id",
    "technical_weight",
    "composite_source",
    "entry_source",
    "technical_gate_policy",
    "classification_block_policy",
    "split",
    "n_rows",
    "n_unique_tickers",
    "top_decile_n",
    "top_decile_unique_tickers",
    "top_decile_mean_return_120d",
    "top_decile_median_return_120d",
    "top_decile_hit_rate_120d",
    "top_decile_mean_excess_120d",
    "top_decile_median_excess_120d",
    "top_decile_excess_hit_rate_120d",
    "top_decile_lcb_excess_120d",
    "top_decile_sortino_excess_120d",
    "top_decile_profit_factor_excess_120d",
    "top_decile_watchlist_wait_for_entry_n",
    "top_decile_watchlist_wait_for_entry_mean_return_120d",
    "top_decile_watchlist_wait_for_entry_median_return_120d",
    "top_decile_watchlist_wait_for_entry_hit_rate_120d",
    "tier1_n",
    "watchlist_wait_for_entry_n",
    "manual_regulatory_n",
    "regulatory_risk_n",
    "latest_top25_watchlist_wait_for_entry_n",
    "latest_top25_tier1_n",
    "latest_top25_overlap_vs_baseline",
    "delta_top_decile_mean_return_vs_baseline",
    "delta_top_decile_median_return_vs_baseline",
    "delta_top_decile_hit_rate_vs_baseline",
    "delta_top_decile_lcb_excess_vs_baseline",
    "delta_top_decile_watchlist_wait_for_entry_vs_baseline",
    "promotion_status",
    "promotion_reason",
]
TOP25_FIELDS = [
    "variant_id",
    "technical_weight",
    "rank",
    "ticker",
    "company_name",
    "calibration_cohort",
    "raw_composite_score",
    "composite_percentile",
    "cohort_percentile",
    "classification",
    "entry_status",
    "technical_score_for_composite",
    "technical_score_for_entry",
    "technical_setup_score",
    "technical_alpha_score",
    "technical_breakdown_flag",
    "failed_gates",
    "forward_return_120d",
    "cohort_excess_return_120d",
]


@dataclass(frozen=True)
class Variant:
    variant_id: str
    composite_source: str
    entry_source: str
    technical_gate_policy: str
    block_classification: bool


VARIANTS = [
    Variant(
        variant_id="legacy_setup_hard_gate",
        composite_source="setup",
        entry_source="setup",
        technical_gate_policy="hard_positive",
        block_classification=True,
    ),
    Variant(
        variant_id="alpha_direct_hard_gate",
        composite_source="alpha",
        entry_source="alpha",
        technical_gate_policy="hard_positive",
        block_classification=True,
    ),
    Variant(
        variant_id="alpha_composite_setup_entry",
        composite_source="alpha",
        entry_source="setup",
        technical_gate_policy="hard_positive",
        block_classification=True,
    ),
    Variant(
        variant_id="alpha_composite_breakdown_veto",
        composite_source="alpha",
        entry_source="setup",
        technical_gate_policy="breakdown_veto",
        block_classification=True,
    ),
    Variant(
        variant_id="alpha_composite_overlay_only",
        composite_source="alpha",
        entry_source="setup",
        technical_gate_policy="overlay_only",
        block_classification=False,
    ),
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Test technical-alpha scoring and entry-gate policy variants.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--input-csv", type=Path, default=None)
    parser.add_argument("--output-csv", type=Path, default=None)
    parser.add_argument("--top25-output-csv", type=Path, default=None)
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


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def write_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore", lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def technical_score(row: dict[str, Any], source: str) -> float:
    if source == "alpha":
        return float_or_default(row.get("technical_alpha_score"), 50.0)
    if source == "setup":
        return float_or_default(row.get("technical_setup_score"), float_or_default(row.get("technical_entry_score"), 50.0))
    raise ValueError(f"Unknown technical score source: {source}")


def entry_status(score: float) -> str:
    if score < 35.0:
        return "avoid_technical_breakdown"
    if score < 45.0:
        return "not_entry_ready"
    if score < 55.0:
        return "watch_for_setup"
    return "entry_eligible"


def weights_for_technical_weight(technical_weight: float) -> dict[str, float]:
    nontechnical_total = sum(value for key, value in DEFAULT_WEIGHTS.items() if key != "technical_entry")
    scale = (1.0 - technical_weight) / nontechnical_total if nontechnical_total > 0 else 0.0
    out = {
        key: (technical_weight if key == "technical_entry" else value * scale)
        for key, value in DEFAULT_WEIGHTS.items()
    }
    return out


def value_trap_discount(value_trap_score: float, *, start: float = 40.0) -> float:
    if value_trap_score <= start:
        return 1.0
    return max(0.50, 1.0 - ((value_trap_score - start) / (2.0 * (100.0 - start))))


def composite_score(row: dict[str, Any], *, variant: Variant, weights: dict[str, float]) -> float:
    scores: dict[str, float] = {
        key: float_or_default(row.get(field), 50.0)
        for key, field in COMPONENT_FIELDS.items()
    }
    scores["technical_entry"] = technical_score(row, variant.composite_source)
    available = [key for key in DEFAULT_WEIGHTS if to_float(scores.get(key)) is not None]
    total_weight = sum(weights[key] for key in available)
    if total_weight <= 0:
        raw = 50.0
    else:
        raw = sum(scores[key] * weights[key] for key in available) / total_weight
    return round(max(0.0, min(100.0, raw * value_trap_discount(float_or_default(row.get("value_trap_score"), 50.0)))), 2)


def percentile_rank(values: list[tuple[int, float]]) -> dict[int, float]:
    if not values:
        return {}
    if len(values) == 1:
        return {values[0][0]: 50.0}
    ranked = sorted(values, key=lambda item: item[1])
    denominator = len(ranked) - 1
    return {idx: round(100.0 * rank / denominator, 2) for rank, (idx, _) in enumerate(ranked)}


def rank_bucket(percentile: float) -> str:
    if percentile >= 90.0:
        return "top_decile"
    if percentile >= 80.0:
        return "top_quintile_ex_decile"
    if percentile <= 20.0:
        return "bottom_quintile"
    return "middle"


def base_gates(config: dict[str, Any]) -> dict[str, float]:
    return {
        "composite_min": float_or_default(cfg_get(config, "scoring.gates.composite_min", 75.0), 75.0),
        "cohort_percentile_min": float_or_default(cfg_get(config, "scoring.gates.cohort_percentile_min", 0.0), 0.0),
        "fundamental_quality_min": float_or_default(cfg_get(config, "scoring.gates.fundamental_quality_min", 70.0), 70.0),
        "durable_growth_min": float_or_default(cfg_get(config, "scoring.gates.durable_growth_min", 60.0), 60.0),
        "fda_product_min": float_or_default(cfg_get(config, "scoring.gates.fda_product_min", 60.0), 60.0),
        "reimbursement_min": float_or_default(cfg_get(config, "scoring.gates.reimbursement_min", 45.0), 45.0),
        "valuation_min": float_or_default(cfg_get(config, "scoring.gates.valuation_min", 60.0), 60.0),
        "technical_entry_min": float_or_default(cfg_get(config, "scoring.gates.technical_entry_min", 55.0), 55.0),
        "data_completeness_min": float_or_default(cfg_get(config, "scoring.gates.data_completeness_min", 90.0), 90.0),
        "min_avg_dollar_volume_60d": float_or_default(
            cfg_get(config, "scoring.gates.min_avg_dollar_volume_60d", 1_000_000.0),
            1_000_000.0,
        ),
        "watchlist_min": float_or_default(cfg_get(config, "scoring.gates.watchlist_min", 60.0), 60.0),
        "value_trap_max": float_or_default(cfg_get(config, "scoring.gates.value_trap_max", 20.0), 20.0),
        "value_trap_hard_max": float_or_default(cfg_get(config, "scoring.gates.value_trap_hard_max", 85.0), 85.0),
    }


def cohort_gate_profiles(config: dict[str, Any], gates: dict[str, float]) -> dict[str, dict[str, float]]:
    raw_profiles = cfg_get(config, "scoring.cohort_profiles", {}) or {}
    if not isinstance(raw_profiles, dict):
        return {}
    out: dict[str, dict[str, float]] = {}
    for cohort, profile in raw_profiles.items():
        if not isinstance(profile, dict):
            continue
        raw_gates = profile.get("gates")
        if not isinstance(raw_gates, dict):
            continue
        merged = dict(gates)
        for key in gates:
            if key in raw_gates:
                merged[key] = float_or_default(raw_gates.get(key), gates[key])
        out[str(cohort)] = merged
    return out


def gates_for_row(row: dict[str, Any], gates: dict[str, float], profiles: dict[str, dict[str, float]]) -> dict[str, float]:
    return profiles.get(str(row.get("calibration_cohort") or ""), gates)


def classify_row(row: dict[str, Any], *, variant: Variant, gates: dict[str, float]) -> tuple[str, str, str, int, str]:
    score_for_entry = technical_score(row, variant.entry_source)
    status = entry_status(score_for_entry)
    raw = float_or_default(row.get("sim_raw_composite_score"), 0.0)
    cohort_percentile = float_or_default(row.get("sim_cohort_percentile"), 0.0)
    reimbursement_status = str(row.get("reimbursement_status") or "").strip()
    reimbursement_live = int_flag(row.get("unknown_reimbursement_flag")) == 0 and reimbursement_status not in NON_LIVE_REIMBURSEMENT_STATUSES
    fda_state = str(row.get("fda_review_state") or "").strip()
    hard_red = int_flag(row.get("hard_red_flag"))
    manual_regulatory_state = fda_state in MANUAL_FDA_REVIEW_STATES
    breakdown_flag = int_flag(row.get("technical_breakdown_flag"))
    technical_gate_score = technical_score(row, variant.entry_source)

    passed_raw = int(raw >= gates["composite_min"] and cohort_percentile >= gates.get("cohort_percentile_min", 0.0))
    passed_fundamental = int(float_or_default(row.get("fundamental_quality_score"), 50.0) >= gates["fundamental_quality_min"])
    passed_growth = int(float_or_default(row.get("durable_growth_score"), 50.0) >= gates["durable_growth_min"])
    passed_fda = int(float_or_default(row.get("fda_product_score"), 45.0) >= gates["fda_product_min"])
    passed_reimbursement = int(reimbursement_live and float_or_default(row.get("reimbursement_score"), 50.0) >= gates["reimbursement_min"])
    passed_valuation = int(float_or_default(row.get("valuation_score"), 50.0) >= gates["valuation_min"])
    if variant.technical_gate_policy == "hard_positive":
        passed_technical = int(technical_gate_score >= gates["technical_entry_min"])
    elif variant.technical_gate_policy == "breakdown_veto":
        passed_technical = int(breakdown_flag == 0 and status != "avoid_technical_breakdown")
    elif variant.technical_gate_policy == "overlay_only":
        passed_technical = 1
    else:
        raise ValueError(f"Unknown technical gate policy: {variant.technical_gate_policy}")
    passed_value_trap = int(float_or_default(row.get("value_trap_score"), 50.0) <= gates["value_trap_max"])
    passed_data = int(float_or_default(row.get("data_completeness_score"), 0.0) >= gates["data_completeness_min"])
    passed_liquidity = int(float_or_default(row.get("avg_dollar_volume_60d"), 0.0) >= gates["min_avg_dollar_volume_60d"])
    passed_fda_review = int(not manual_regulatory_state and not hard_red)

    reasons: list[str] = []
    if not passed_raw:
        if raw < gates["composite_min"]:
            reasons.append("composite_below_gate")
        if cohort_percentile < gates.get("cohort_percentile_min", 0.0):
            reasons.append("cohort_percentile_below_gate")
    if not passed_fundamental:
        reasons.append("fundamental_below_gate")
    if not passed_growth:
        reasons.append("growth_below_gate")
    if not passed_fda:
        reasons.append("fda_below_gate")
    if not passed_reimbursement:
        reasons.append("reimbursement_missing_evidence" if not reimbursement_live else "reimbursement_below_gate")
    if not passed_valuation:
        reasons.append("valuation_below_gate")
    if not passed_technical:
        reasons.append("technical_breakdown_veto" if variant.technical_gate_policy == "breakdown_veto" else "technical_below_gate")
    if not passed_data:
        reasons.append("data_quality_below_gate")
    if not passed_liquidity:
        reasons.append("liquidity_below_gate")
    if hard_red:
        reasons.append("hard_red_flag")
    elif manual_regulatory_state:
        reasons.append("fda_review_required")
    if float_or_default(row.get("value_trap_score"), 50.0) >= gates["value_trap_hard_max"]:
        reasons.append("value_trap")
    elif not passed_value_trap:
        reasons.append("value_trap_soft_gate")

    technical_block = bool(variant.block_classification and not passed_technical)
    final_gate = int(
        passed_raw
        and passed_fundamental
        and passed_growth
        and passed_fda
        and passed_reimbursement
        and passed_valuation
        and passed_technical
        and passed_value_trap
        and passed_data
        and passed_liquidity
        and passed_fda_review
        and not technical_block
    )
    if fda_state == "confirmed_hard_red":
        classification = "avoid_confirmed_regulatory_risk"
    elif manual_regulatory_state or hard_red:
        classification = "manual_review_regulatory_risk"
    elif not passed_data:
        classification = "data_review_required"
    elif technical_block:
        classification = "watchlist_wait_for_entry"
    elif float_or_default(row.get("fundamental_quality_score"), 50.0) >= gates["fundamental_quality_min"] and float_or_default(row.get("valuation_score"), 50.0) < gates["valuation_min"]:
        classification = "quality_watchlist_wait_for_price"
    elif float_or_default(row.get("valuation_score"), 50.0) >= 75.0 and float_or_default(row.get("fundamental_quality_score"), 50.0) < gates["fundamental_quality_min"]:
        classification = "cheap_but_needs_proof"
    elif not passed_value_trap and raw >= gates["watchlist_min"]:
        classification = "cheap_but_needs_proof"
    elif final_gate:
        classification = "tier_1_long_candidate"
    elif raw >= gates["watchlist_min"]:
        classification = "watchlist"
    else:
        classification = "avoid"
    return classification, status, ";".join(reasons), final_gate, "pass" if final_gate else "fail"


def simulate(rows: list[dict[str, str]], *, config: dict[str, Any], variant: Variant, technical_weight: float) -> list[dict[str, Any]]:
    weights = weights_for_technical_weight(technical_weight)
    gates = base_gates(config)
    profiles = cohort_gate_profiles(config, gates)
    out = [dict(row) for row in rows]
    for row in out:
        raw = composite_score(row, variant=variant, weights=weights)
        row["sim_raw_composite_score"] = raw
        row["technical_score_for_composite"] = technical_score(row, variant.composite_source)
        row["technical_score_for_entry"] = technical_score(row, variant.entry_source)
    by_asof: dict[str, list[tuple[int, float]]] = defaultdict(list)
    by_asof_cohort: dict[tuple[str, str], list[tuple[int, float]]] = defaultdict(list)
    for idx, row in enumerate(out):
        asof = str(row.get("asof_date") or "")
        cohort = str(row.get("calibration_cohort") or "unknown")
        raw = float_or_default(row.get("sim_raw_composite_score"), 0.0)
        by_asof[asof].append((idx, raw))
        by_asof_cohort[(asof, cohort)].append((idx, raw))
    for pairs in by_asof.values():
        percentiles = percentile_rank(pairs)
        for idx, percentile in percentiles.items():
            out[idx]["sim_composite_percentile"] = percentile
            out[idx]["sim_rank_bucket"] = rank_bucket(percentile)
    for pairs in by_asof_cohort.values():
        percentiles = percentile_rank(pairs)
        for idx, percentile in percentiles.items():
            out[idx]["sim_cohort_percentile"] = percentile
    for asof, pairs in by_asof.items():
        ordered = sorted(
            pairs,
            key=lambda item: (
                -float_or_default(out[item[0]].get("sim_composite_percentile"), 0.0),
                -float_or_default(out[item[0]].get("sim_raw_composite_score"), 0.0),
                str(out[item[0]].get("ticker") or ""),
            ),
        )
        for rank, (idx, _) in enumerate(ordered, start=1):
            out[idx]["sim_rank"] = rank
    for row in out:
        row_gates = gates_for_row(row, gates, profiles)
        classification, status, failed, final_gate, gate_status = classify_row(row, variant=variant, gates=row_gates)
        row["sim_classification"] = classification
        row["sim_entry_status"] = status
        row["sim_failed_gates"] = failed
        row["sim_final_investability_gate"] = final_gate
        row["sim_gate_status"] = gate_status
    return out


def lcb(values: list[float], z: float = 1.64) -> float:
    if not values:
        return 0.0
    if len(values) < 2:
        return values[0]
    avg = mean(values)
    variance = sum((value - avg) ** 2 for value in values) / (len(values) - 1)
    return avg - z * math.sqrt(variance / len(values))


def sortino(values: list[float]) -> float:
    if not values:
        return 0.0
    downside = [min(0.0, value) for value in values]
    negative_count = sum(1 for value in values if value < 0)
    if negative_count <= 0:
        return 0.0
    downside_dev = math.sqrt(sum(value * value for value in downside) / negative_count)
    return mean(values) / downside_dev if downside_dev > 0 else 0.0


def profit_factor(values: list[float]) -> float:
    gains = sum(value for value in values if value > 0)
    losses = -sum(value for value in values if value < 0)
    if losses <= 0:
        return 0.0
    return gains / losses


def metric_summary(values: list[float]) -> dict[str, float]:
    if not values:
        return {
            "mean": 0.0,
            "median": 0.0,
            "hit_rate": 0.0,
            "lcb": 0.0,
            "sortino": 0.0,
            "profit_factor": 0.0,
        }
    return {
        "mean": mean(values),
        "median": median(values),
        "hit_rate": sum(1 for value in values if value > 0) / len(values),
        "lcb": lcb(values),
        "sortino": sortino(values),
        "profit_factor": profit_factor(values),
    }


def split_for_row(row: dict[str, Any], config: dict[str, Any]) -> str:
    asof = str(row.get("asof_date") or "")[:10]
    train_end = str(cfg_get(config, "calibration.train_end_asof", "2025-05-30"))
    validation_start = str(cfg_get(config, "calibration.validation_start_asof", "2025-06-06"))
    validation_end = str(cfg_get(config, "calibration.validation_end_asof", "2025-11-28"))
    if asof <= train_end:
        return "train"
    if validation_start <= asof <= validation_end:
        return "validation"
    return "holdout_or_incomplete"


def summarize(
    rows: list[dict[str, Any]],
    *,
    variant: Variant,
    technical_weight: float,
    split: str,
    latest_asof: str,
    baseline_top25: set[str] | None,
    config: dict[str, Any],
) -> dict[str, Any]:
    scoped = [row for row in rows if split == "all" or split_for_row(row, config) == split]
    top_decile = [
        row for row in scoped
        if row.get("sim_rank_bucket") == "top_decile" and to_float(row.get("forward_return_120d")) is not None
    ]
    returns = [float_or_default(row.get("forward_return_120d"), 0.0) for row in top_decile]
    excess = [float_or_default(row.get("cohort_excess_return_120d"), 0.0) for row in top_decile]
    ret_metrics = metric_summary(returns)
    excess_metrics = metric_summary(excess)
    top_wait = [row for row in top_decile if row.get("sim_classification") == "watchlist_wait_for_entry"]
    top_wait_returns = [float_or_default(row.get("forward_return_120d"), 0.0) for row in top_wait]
    top_wait_metrics = metric_summary(top_wait_returns)
    classes = Counter(str(row.get("sim_classification") or "") for row in scoped)
    latest_top25 = sorted(
        [row for row in rows if str(row.get("asof_date") or "")[:10] == latest_asof],
        key=lambda row: int(float_or_default(row.get("sim_rank"), 999999)),
    )[:25]
    latest_tickers = {str(row.get("ticker") or "") for row in latest_top25}
    regulatory_risk_n = classes.get("manual_review_regulatory_risk", 0) + classes.get("avoid_confirmed_regulatory_risk", 0)
    return {
        "variant_id": variant.variant_id,
        "technical_weight": technical_weight,
        "composite_source": variant.composite_source,
        "entry_source": variant.entry_source,
        "technical_gate_policy": variant.technical_gate_policy,
        "classification_block_policy": "block_on_technical_gate" if variant.block_classification else "no_technical_block",
        "split": split,
        "n_rows": len(scoped),
        "n_unique_tickers": len({str(row.get("ticker") or "") for row in scoped}),
        "top_decile_n": len(top_decile),
        "top_decile_unique_tickers": len({str(row.get("ticker") or "") for row in top_decile}),
        "top_decile_mean_return_120d": ret_metrics["mean"],
        "top_decile_median_return_120d": ret_metrics["median"],
        "top_decile_hit_rate_120d": ret_metrics["hit_rate"],
        "top_decile_mean_excess_120d": excess_metrics["mean"],
        "top_decile_median_excess_120d": excess_metrics["median"],
        "top_decile_excess_hit_rate_120d": excess_metrics["hit_rate"],
        "top_decile_lcb_excess_120d": excess_metrics["lcb"],
        "top_decile_sortino_excess_120d": excess_metrics["sortino"],
        "top_decile_profit_factor_excess_120d": excess_metrics["profit_factor"],
        "top_decile_watchlist_wait_for_entry_n": len(top_wait),
        "top_decile_watchlist_wait_for_entry_mean_return_120d": top_wait_metrics["mean"],
        "top_decile_watchlist_wait_for_entry_median_return_120d": top_wait_metrics["median"],
        "top_decile_watchlist_wait_for_entry_hit_rate_120d": top_wait_metrics["hit_rate"],
        "tier1_n": classes.get("tier_1_long_candidate", 0),
        "watchlist_wait_for_entry_n": classes.get("watchlist_wait_for_entry", 0),
        "manual_regulatory_n": classes.get("manual_review_regulatory_risk", 0),
        "regulatory_risk_n": regulatory_risk_n,
        "latest_top25_watchlist_wait_for_entry_n": sum(
            1 for row in latest_top25 if row.get("sim_classification") == "watchlist_wait_for_entry"
        ),
        "latest_top25_tier1_n": sum(1 for row in latest_top25 if row.get("sim_classification") == "tier_1_long_candidate"),
        "latest_top25_overlap_vs_baseline": len(latest_tickers & baseline_top25) if baseline_top25 is not None else "",
    }


def add_deltas_and_status(rows: list[dict[str, Any]]) -> None:
    baseline_by_split = {
        row["split"]: row
        for row in rows
        if row["variant_id"] == "legacy_setup_hard_gate" and abs(float(row["technical_weight"]) - 0.10) < 1e-9
    }
    alpha_direct_by_split = {
        row["split"]: row
        for row in rows
        if row["variant_id"] == "alpha_direct_hard_gate" and abs(float(row["technical_weight"]) - 0.10) < 1e-9
    }
    for row in rows:
        baseline = baseline_by_split.get(row["split"])
        if baseline:
            row["delta_top_decile_mean_return_vs_baseline"] = (
                float(row["top_decile_mean_return_120d"]) - float(baseline["top_decile_mean_return_120d"])
            )
            row["delta_top_decile_median_return_vs_baseline"] = (
                float(row["top_decile_median_return_120d"]) - float(baseline["top_decile_median_return_120d"])
            )
            row["delta_top_decile_hit_rate_vs_baseline"] = (
                float(row["top_decile_hit_rate_120d"]) - float(baseline["top_decile_hit_rate_120d"])
            )
            row["delta_top_decile_lcb_excess_vs_baseline"] = (
                float(row["top_decile_lcb_excess_120d"]) - float(baseline["top_decile_lcb_excess_120d"])
            )
            row["delta_top_decile_watchlist_wait_for_entry_vs_baseline"] = (
                int(row["top_decile_watchlist_wait_for_entry_n"]) - int(baseline["top_decile_watchlist_wait_for_entry_n"])
            )
        else:
            for key in [
                "delta_top_decile_mean_return_vs_baseline",
                "delta_top_decile_median_return_vs_baseline",
                "delta_top_decile_hit_rate_vs_baseline",
                "delta_top_decile_lcb_excess_vs_baseline",
                "delta_top_decile_watchlist_wait_for_entry_vs_baseline",
            ]:
                row[key] = ""
        if row["split"] != "validation":
            row["promotion_status"] = "not_evaluated_split"
            row["promotion_reason"] = "promotion checks use validation split"
            continue
        if row["variant_id"] == "legacy_setup_hard_gate" and abs(float(row["technical_weight"]) - 0.10) < 1e-9:
            row["promotion_status"] = "baseline"
            row["promotion_reason"] = "baseline_reference"
            continue
        baseline = baseline_by_split.get("validation")
        alpha_direct = alpha_direct_by_split.get("validation")
        reasons: list[str] = []
        if baseline:
            if float(row["top_decile_mean_return_120d"]) < float(baseline["top_decile_mean_return_120d"]):
                reasons.append("mean_return_below_baseline")
            if float(row["top_decile_median_return_120d"]) < float(baseline["top_decile_median_return_120d"]):
                reasons.append("median_return_below_baseline")
            if float(row["top_decile_hit_rate_120d"]) < float(baseline["top_decile_hit_rate_120d"]):
                reasons.append("hit_rate_below_baseline")
            if int(row["latest_top25_watchlist_wait_for_entry_n"]) > int(baseline["latest_top25_watchlist_wait_for_entry_n"]) + 5:
                reasons.append("latest_top25_wait_for_entry_expanded")
        if alpha_direct and row["variant_id"] != "alpha_direct_hard_gate":
            if float(row["top_decile_mean_return_120d"]) < 0.90 * float(alpha_direct["top_decile_mean_return_120d"]):
                reasons.append("mean_return_far_below_alpha_direct")
        row["promotion_status"] = "candidate" if not reasons else "reject"
        row["promotion_reason"] = ";".join(reasons) if reasons else "passes_validation_policy_checks"


def latest_top25_rows(
    simulated_rows: list[dict[str, Any]],
    *,
    variant: Variant,
    technical_weight: float,
    latest_asof: str,
) -> list[dict[str, Any]]:
    rows = sorted(
        [row for row in simulated_rows if str(row.get("asof_date") or "")[:10] == latest_asof],
        key=lambda row: int(float_or_default(row.get("sim_rank"), 999999)),
    )[:25]
    out: list[dict[str, Any]] = []
    for row in rows:
        out.append(
            {
                "variant_id": variant.variant_id,
                "technical_weight": technical_weight,
                "rank": int(float_or_default(row.get("sim_rank"), 0.0)),
                "ticker": row.get("ticker"),
                "company_name": row.get("company_name"),
                "calibration_cohort": row.get("calibration_cohort"),
                "raw_composite_score": row.get("sim_raw_composite_score"),
                "composite_percentile": row.get("sim_composite_percentile"),
                "cohort_percentile": row.get("sim_cohort_percentile"),
                "classification": row.get("sim_classification"),
                "entry_status": row.get("sim_entry_status"),
                "technical_score_for_composite": row.get("technical_score_for_composite"),
                "technical_score_for_entry": row.get("technical_score_for_entry"),
                "technical_setup_score": row.get("technical_setup_score"),
                "technical_alpha_score": row.get("technical_alpha_score"),
                "technical_breakdown_flag": row.get("technical_breakdown_flag"),
                "failed_gates": row.get("sim_failed_gates"),
                "forward_return_120d": row.get("forward_return_120d"),
                "cohort_excess_return_120d": row.get("cohort_excess_return_120d"),
            }
        )
    return out


def main() -> None:
    configure_utc_logging()
    args = parse_args()
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
                "calibration.technical_alpha_policy_test_csv",
                "../output/med_devices_reports/calibration/med_device_technical_alpha_policy_test.csv",
            ),
            base_dir=base_dir,
        )
    )
    top25_output_csv = (
        args.top25_output_csv.expanduser().resolve()
        if args.top25_output_csv
        else resolve_path(
            cfg_get(
                config,
                "calibration.technical_alpha_policy_top25_csv",
                "../output/med_devices_reports/calibration/med_device_technical_alpha_policy_top25.csv",
            ),
            base_dir=base_dir,
        )
    )
    technical_weights = parse_float_list(
        cfg_get(config, "calibration.technical_alpha_policy_candidate_weights", "0.05,0.075,0.10,0.125"),
        "0.05,0.075,0.10,0.125",
    )
    rows = read_csv(input_csv)
    if not rows:
        raise RuntimeError(f"No rows found in {input_csv}")
    required_columns = {"forward_return_120d", "cohort_excess_return_120d"}
    missing_columns = sorted(required_columns - set(rows[0]))
    if missing_columns:
        raise ValueError(
            f"{input_csv} is missing required 120d columns for technical alpha policy testing: "
            f"{','.join(missing_columns)}"
        )
    latest_asof = max(str(row.get("asof_date") or "")[:10] for row in rows)
    all_summary_rows: list[dict[str, Any]] = []
    all_top25_rows: list[dict[str, Any]] = []
    baseline_top25: set[str] | None = None
    simulated_by_key: dict[tuple[str, float], list[dict[str, Any]]] = {}
    for variant in VARIANTS:
        for technical_weight in technical_weights:
            simulated = simulate(rows, config=config, variant=variant, technical_weight=technical_weight)
            simulated_by_key[(variant.variant_id, technical_weight)] = simulated
            if variant.variant_id == "legacy_setup_hard_gate" and abs(technical_weight - 0.10) < 1e-9:
                baseline_top25 = {
                    str(row.get("ticker") or "")
                    for row in sorted(
                        [item for item in simulated if str(item.get("asof_date") or "")[:10] == latest_asof],
                        key=lambda item: int(float_or_default(item.get("sim_rank"), 999999)),
                    )[:25]
                }
    for variant in VARIANTS:
        for technical_weight in technical_weights:
            simulated = simulated_by_key[(variant.variant_id, technical_weight)]
            for split in ["all", "train", "validation"]:
                all_summary_rows.append(
                    summarize(
                        simulated,
                        variant=variant,
                        technical_weight=technical_weight,
                        split=split,
                        latest_asof=latest_asof,
                        baseline_top25=baseline_top25,
                        config=config,
                    )
                )
            all_top25_rows.extend(
                latest_top25_rows(
                    simulated,
                    variant=variant,
                    technical_weight=technical_weight,
                    latest_asof=latest_asof,
                )
            )
    add_deltas_and_status(all_summary_rows)
    write_csv(output_csv, all_summary_rows, SUMMARY_FIELDS)
    write_csv(top25_output_csv, all_top25_rows, TOP25_FIELDS)
    validation_candidates = [
        row for row in all_summary_rows
        if row["split"] == "validation" and row["promotion_status"] == "candidate"
    ]
    validation_candidates.sort(
        key=lambda row: (
            float(row["top_decile_mean_return_120d"]),
            float(row["top_decile_median_return_120d"]),
            float(row["top_decile_hit_rate_120d"]),
        ),
        reverse=True,
    )
    best = validation_candidates[0] if validation_candidates else None
    best_text = (
        f" best_candidate={best['variant_id']} technical_weight={best['technical_weight']}"
        if best is not None
        else " best_candidate="
    )
    print(f"technical_alpha_policy_test_csv={output_csv} rows={len(all_summary_rows)}{best_text}")
    print(f"technical_alpha_policy_top25_csv={top25_output_csv} rows={len(all_top25_rows)}")


if __name__ == "__main__":
    main()
