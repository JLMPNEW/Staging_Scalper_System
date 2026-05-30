#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import logging
import math
import sys
import time
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable


LOGGER = logging.getLogger("tier1_120d_candidate_report")
DEFAULT_INPUT_DIR = Path("output/biotech_index_reports/calibration_tier1_120d_confirm")

HOLDOUT_FILE = "tier1_weight_calibration_holdout.csv"
BOOTSTRAP_FILE = "tier1_weight_calibration_bootstrap_ci.csv"
TICKER_DIAGNOSTICS_FILE = "tier1_selected_ticker_diagnostics.csv"
CURRENT_CONFIG_CANDIDATE_NAME = "current_config"
HOLDOUT_LEGACY_ALIASES = {
    "test_selected_lcb_return_pct": ("test_lcb_return_pct",),
    "test_selected_sortino_like": ("test_sortino_like",),
    "test_selected_profit_factor": ("test_profit_factor",),
    "test_selected_omega_configured": ("test_omega_configured",),
    "test_selected_omega_0": ("test_omega_0",),
    "test_selected_mean_return_pct": ("test_mean_return_pct",),
    "test_selected_large_loss_20pct_rate_pct": ("test_large_loss_20pct_rate_pct",),
    "test_selected_large_loss_40pct_rate_pct": ("test_large_loss_40pct_rate_pct",),
    "test_selected_top3_gain_contribution_pct": ("test_top3_gain_contribution_pct",),
    "test_selected_core_hard_weakness_exposure_pct": ("test_core_hard_weakness_exposure_pct",),
    "test_selected_event_hard_weakness_exposure_pct": ("test_event_hard_weakness_exposure_pct",),
    "test_selected_soft_weakness_exposure_pct": ("test_soft_weakness_exposure_pct",),
    "test_selected_toxic_soft_weakness_exposure_pct": ("test_toxic_soft_weakness_exposure_pct",),
    "test_selected_mild_soft_weakness_exposure_pct": ("test_mild_soft_weakness_exposure_pct",),
    "test_selected_illiquid_weakness_exposure_pct": ("test_illiquid_weakness_exposure_pct",),
    "test_selected_value_trap_exposure_pct": ("test_value_trap_exposure_pct",),
    "test_selected_leverage_fragility_exposure_pct": ("test_leverage_fragility_exposure_pct",),
    "test_selected_guidance_staleness_exposure_pct": ("test_guidance_staleness_exposure_pct",),
    "test_selected_no_forward_guidance_exposure_pct": ("test_no_forward_guidance_exposure_pct",),
    "test_selected_stale_guidance_exposure_pct": ("test_stale_guidance_exposure_pct",),
    "test_selected_no_guidance_negative_growth_exposure_pct": ("test_no_guidance_negative_growth_exposure_pct",),
    "test_selected_rank_quality_cap_exposure_pct": ("test_rank_quality_cap_exposure_pct",),
}

WEIGHT_COLUMNS = [
    "clinical_catalyst_weight",
    "clinical_credibility_weight",
    "clinical_financial_quality_weight",
    "clinical_momentum_weight",
    "clinical_risk_penalty_weight",
    "clinical_stage_clinical_opportunity_weight",
    "clinical_stage_commercial_value_weight",
    "clinical_stage_forward_guidance_weight",
    "clinical_stage_valuation_weight",
    "clinical_stage_upside_capacity_weight",
    "clinical_stage_institutional_upside_weight",
    "clinical_stage_financial_quality_weight",
    "clinical_stage_momentum_weight",
    "clinical_stage_risk_penalty_weight",
    "commercial_stage_clinical_opportunity_weight",
    "commercial_stage_commercial_value_weight",
    "commercial_stage_forward_guidance_weight",
    "commercial_stage_valuation_weight",
    "commercial_stage_upside_capacity_weight",
    "commercial_stage_institutional_upside_weight",
    "commercial_stage_financial_quality_weight",
    "commercial_stage_momentum_weight",
    "commercial_stage_risk_penalty_weight",
]

SOFT_REASON_REPORT_COLUMNS = [
    "cash_runway_9_to_12m_clinical",
    "going_concern_warning",
    "single_dilution_event",
    "low_financial_data_quality",
    "high_commercial_fragility",
    "high_tier1_risk_score",
    "recent_nt_filing",
    "early_stage_or_unadvanced_trial_anchor",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Summarize confirmed Script 28 Tier-1 120d calibration outputs into production candidate "
            "decision reports. This script is read-only with respect to calibration inputs and config."
        )
    )
    parser.add_argument("--input-dir", type=Path, default=DEFAULT_INPUT_DIR)
    parser.add_argument("--output-dir", type=Path, default=None)
    parser.add_argument("--horizon", type=str, default="120")
    parser.add_argument(
        "--candidate-rank-limit",
        type=int,
        default=10,
        help="Number of train-ranked candidates per sample/top-N to include in the ticker audit.",
    )
    parser.add_argument(
        "--ticker-split",
        choices=["test", "train", "both"],
        default="both",
        help="Which selected ticker diagnostic split to include in the ticker audit.",
    )
    return parser.parse_args()


def configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)sZ %(levelname)s %(name)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    logging.Formatter.converter = time.gmtime  # type: ignore[method-assign]


def read_csv(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        raise FileNotFoundError(f"Script 28 calibration output not found at {path}. Run 28_calibrate_biotech_opportunity.py first.")
    with path.open("r", encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def validate_required_columns(rows: list[dict[str, Any]], *, file_label: str, required: set[str]) -> None:
    if not rows:
        return
    available = set(rows[0])
    missing = sorted(required - available)
    if missing:
        raise ValueError(f"{file_label} is missing required columns: {', '.join(missing)}")


def apply_legacy_column_aliases(
    rows: list[dict[str, Any]],
    aliases: dict[str, tuple[str, ...]],
    *,
    file_label: str,
) -> None:
    alias_hits: set[str] = set()
    for row in rows:
        for canonical, legacy_names in aliases.items():
            if row.get(canonical) not in {None, ""}:
                continue
            for legacy in legacy_names:
                if legacy in row:
                    row[canonical] = row.get(legacy, "")
                    alias_hits.add(f"{legacy}->{canonical}")
                    break
    if alias_hits:
        LOGGER.warning(
            "%s used legacy script 28 column aliases: %s",
            file_label,
            ", ".join(sorted(alias_hits)),
        )


def write_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str] | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if fieldnames is None:
        seen: set[str] = set()
        fieldnames = []
        for row in rows:
            for key in row:
                if key not in seen:
                    seen.add(key)
                    fieldnames.append(key)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, lineterminator="\n", extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True), encoding="utf-8")


def to_float(raw: object, default: float | None = None) -> float | None:
    if raw is None:
        return default
    try:
        value = float(str(raw).strip())
    except (TypeError, ValueError):
        return default
    return value if math.isfinite(value) else default


def to_int(raw: object, default: int = 0) -> int:
    value = to_float(raw)
    return int(value) if value is not None else default


def as_bool(raw: object, default: bool = False) -> bool:
    if raw is None:
        return default
    text = str(raw).strip().lower()
    if text in {"1", "true", "t", "yes", "y", "enabled", "on", "pass", "passed"}:
        return True
    if text in {"0", "false", "f", "no", "n", "disabled", "off"}:
        return False
    return default


def is_sparse_test_pass(row: dict[str, Any]) -> bool:
    value = str(row.get("test_calibration_pass") or "").strip().lower()
    state = str(row.get("test_calibration_pass_state") or "").strip().lower()
    return value == "sparse_data" or state == "sparse_data"


def bootstrap_ci_status(boot: dict[str, Any]) -> str:
    if not boot:
        return "missing"
    iterations = to_float(boot.get("bootstrap_iterations"))
    if iterations is None or iterations <= 0:
        return "not_computed"
    if to_float(boot.get("selected_lcb_return_pct_ci05")) is None:
        return "missing_ci"
    return "computed"


def rounded(raw: object, digits: int = 6) -> float | str:
    value = to_float(raw)
    return "" if value is None else round(value, digits)


def mean(values: Iterable[float]) -> float | None:
    vals = list(values)
    return sum(vals) / len(vals) if vals else None


def mean_numeric(rows: Iterable[dict[str, Any]], key: str) -> float | str:
    values = [value for row in rows if (value := to_float(row.get(key))) is not None]
    avg = mean(values)
    return "" if avg is None else round(avg, 6)


def numeric_or_default(raw: object, default: float) -> float:
    value = to_float(raw)
    return value if value is not None else default


def first_value(row: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = row.get(key)
        if value not in {None, ""}:
            return value
    return ""


def candidate_key(row: dict[str, Any], *, split: str | None = None) -> tuple[str, str, str, str, str, str]:
    return (
        str(row.get("sample") or ""),
        str(split if split is not None else row.get("evaluation_split") or ""),
        str(row.get("horizon_days") or ""),
        str(row.get("top_n") or ""),
        str(row.get("train_rank") or ""),
        str(row.get("candidate_id") or ""),
    )


def holdout_report_sort_key(row: dict[str, Any]) -> tuple[float, float, float, float, float, float, float]:
    ci05 = numeric_or_default(row.get("bootstrap_lcb_return_pct_ci05"), -1e9)
    # Fallback names without test_selected_ are for older script 28 outputs; new runs emit test_selected_*.
    lcb = numeric_or_default(first_value(row, "test_selected_lcb_return_pct", "test_lcb_return_pct"), -1e9)
    sortino = numeric_or_default(first_value(row, "test_selected_sortino_like", "test_sortino_like"), -1e9)
    profit = numeric_or_default(first_value(row, "test_selected_profit_factor", "test_profit_factor"), -1e9)
    loss20 = numeric_or_default(
        first_value(row, "test_selected_large_loss_20pct_rate_pct", "test_large_loss_20pct_rate_pct"),
        100.0,
    )
    core = numeric_or_default(
        first_value(row, "test_selected_core_hard_weakness_exposure_pct", "test_core_hard_weakness_exposure_pct"),
        100.0,
    )
    top3 = numeric_or_default(
        first_value(row, "test_selected_top3_gain_contribution_pct", "test_top3_gain_contribution_pct"),
        100.0,
    )
    return (ci05, lcb, sortino, profit, -loss20, -core, -top3)


def recommendation_bucket(row: dict[str, Any]) -> str:
    if str(row.get("candidate_name") or "") == CURRENT_CONFIG_CANDIDATE_NAME:
        return "current_config_benchmark"
    if str(row.get("train_rank") or "") == "1":
        return "train_rank_1_candidate"
    ci05 = to_float(row.get("bootstrap_lcb_return_pct_ci05"))
    if ci05 is not None and ci05 > 0.0:
        return "bootstrap_positive_candidate"
    return "confirmed_candidate"


def build_candidate_report(
    holdout_rows: list[dict[str, Any]],
    bootstrap_rows: list[dict[str, Any]],
    *,
    horizon: str,
) -> list[dict[str, Any]]:
    bootstrap_by_key = {candidate_key(row): row for row in bootstrap_rows if str(row.get("evaluation_split") or "") == "test"}
    sparse_test_rows = [
        row
        for row in holdout_rows
        if str(row.get("horizon_days") or "") == horizon
        and as_bool(row.get("train_calibration_pass"))
        and is_sparse_test_pass(row)
    ]
    if sparse_test_rows:
        LOGGER.warning(
            "Skipping %d train-passing candidate row(s) for horizon=%s because test_calibration_pass is sparse. "
            "This usually means the test split had sparse or unavailable completed returns.",
            len(sparse_test_rows),
            horizon,
        )
    confirmed = [
        row
        for row in holdout_rows
        if str(row.get("horizon_days") or "") == horizon
        and as_bool(row.get("train_calibration_pass"))
        and as_bool(row.get("test_calibration_pass"))
    ]

    out: list[dict[str, Any]] = []
    for row in confirmed:
        boot = bootstrap_by_key.get(candidate_key(row, split="test"), {})
        payload: dict[str, Any] = {
            "sample": row.get("sample", ""),
            "horizon_days": row.get("horizon_days", ""),
            "top_n": row.get("top_n", ""),
            "train_rank": row.get("train_rank", ""),
            "candidate_id": row.get("candidate_id", ""),
            "candidate_name": row.get("candidate_name", ""),
            "candidate_description": row.get("candidate_description", ""),
            "selection_policy_name": row.get("selection_policy_name", ""),
            "selection_policy_description": row.get("selection_policy_description", ""),
            "train_calibration_pass_state": row.get("train_calibration_pass_state", ""),
            "test_calibration_pass_state": row.get("test_calibration_pass_state", ""),
            "recommendation_bucket": "",
            "test_calibration_objective_vs_current_config": rounded(
                row.get("test_calibration_objective_vs_current_config")
            ),
            "test_lcb_return_pct": rounded(row.get("test_selected_lcb_return_pct")),
            "test_sortino_like": rounded(row.get("test_selected_sortino_like")),
            "test_profit_factor": rounded(row.get("test_selected_profit_factor")),
            "test_omega_configured": rounded(
                first_value(row, "test_selected_omega_configured", "test_selected_omega_0")
            ),
            "test_mean_return_pct": rounded(row.get("test_selected_mean_return_pct")),
            "test_large_loss_20pct_rate_pct": rounded(row.get("test_selected_large_loss_20pct_rate_pct")),
            "test_large_loss_40pct_rate_pct": rounded(row.get("test_selected_large_loss_40pct_rate_pct")),
            "test_top3_gain_contribution_pct": rounded(row.get("test_selected_top3_gain_contribution_pct")),
            "test_core_hard_weakness_exposure_pct": rounded(
                row.get("test_selected_core_hard_weakness_exposure_pct")
            ),
            "test_event_hard_weakness_exposure_pct": rounded(
                row.get("test_selected_event_hard_weakness_exposure_pct")
            ),
            "test_soft_weakness_exposure_pct": rounded(row.get("test_selected_soft_weakness_exposure_pct")),
            "test_toxic_soft_weakness_exposure_pct": rounded(
                row.get("test_selected_toxic_soft_weakness_exposure_pct")
            ),
            "test_mild_soft_weakness_exposure_pct": rounded(
                row.get("test_selected_mild_soft_weakness_exposure_pct")
            ),
            "test_illiquid_weakness_exposure_pct": rounded(
                row.get("test_selected_illiquid_weakness_exposure_pct")
            ),
            "test_value_trap_exposure_pct": rounded(row.get("test_selected_value_trap_exposure_pct")),
            "test_leverage_fragility_exposure_pct": rounded(
                row.get("test_selected_leverage_fragility_exposure_pct")
            ),
            "test_guidance_staleness_exposure_pct": rounded(
                row.get("test_selected_guidance_staleness_exposure_pct")
            ),
            "test_no_forward_guidance_exposure_pct": rounded(
                row.get("test_selected_no_forward_guidance_exposure_pct")
            ),
            "test_stale_guidance_exposure_pct": rounded(row.get("test_selected_stale_guidance_exposure_pct")),
            "test_no_guidance_negative_growth_exposure_pct": rounded(
                row.get("test_selected_no_guidance_negative_growth_exposure_pct")
            ),
            "test_rank_quality_cap_exposure_pct": rounded(row.get("test_selected_rank_quality_cap_exposure_pct")),
            "bootstrap_iterations": boot.get("bootstrap_iterations", ""),
            "bootstrap_ci_status": bootstrap_ci_status(boot),
            "bootstrap_lcb_return_pct_ci05": rounded(boot.get("selected_lcb_return_pct_ci05")),
            "bootstrap_lcb_return_pct_ci95": rounded(boot.get("selected_lcb_return_pct_ci95")),
            "bootstrap_sortino_like_ci05": rounded(boot.get("selected_sortino_like_ci05")),
            "bootstrap_sortino_like_ci95": rounded(boot.get("selected_sortino_like_ci95")),
            "bootstrap_profit_factor_ci05": rounded(boot.get("selected_profit_factor_ci05")),
            "bootstrap_profit_factor_ci95": rounded(boot.get("selected_profit_factor_ci95")),
            "bootstrap_omega_configured_ci05": rounded(
                first_value(boot, "selected_omega_configured_ci05", "selected_omega_0_ci05")
            ),
            "bootstrap_omega_configured_ci95": rounded(
                first_value(boot, "selected_omega_configured_ci95", "selected_omega_0_ci95")
            ),
            "bootstrap_large_loss_20pct_rate_pct_ci05": rounded(
                boot.get("selected_large_loss_20pct_rate_pct_ci05")
            ),
            "bootstrap_large_loss_20pct_rate_pct_ci95": rounded(
                boot.get("selected_large_loss_20pct_rate_pct_ci95")
            ),
            "bootstrap_core_hard_weakness_exposure_pct_ci05": rounded(
                boot.get("selected_core_hard_weakness_exposure_pct_ci05")
            ),
            "bootstrap_core_hard_weakness_exposure_pct_ci95": rounded(
                boot.get("selected_core_hard_weakness_exposure_pct_ci95")
            ),
            "bootstrap_event_hard_weakness_exposure_pct_ci05": rounded(
                boot.get("selected_event_hard_weakness_exposure_pct_ci05")
            ),
            "bootstrap_event_hard_weakness_exposure_pct_ci95": rounded(
                boot.get("selected_event_hard_weakness_exposure_pct_ci95")
            ),
            "bootstrap_soft_weakness_exposure_pct_ci05": rounded(
                boot.get("selected_soft_weakness_exposure_pct_ci05")
            ),
            "bootstrap_soft_weakness_exposure_pct_ci95": rounded(
                boot.get("selected_soft_weakness_exposure_pct_ci95")
            ),
            "bootstrap_illiquid_weakness_exposure_pct_ci05": rounded(
                boot.get("selected_illiquid_weakness_exposure_pct_ci05")
            ),
            "bootstrap_illiquid_weakness_exposure_pct_ci95": rounded(
                boot.get("selected_illiquid_weakness_exposure_pct_ci95")
            ),
            "selection_policy_hard_veto": row.get("selection_policy_hard_veto", ""),
            "selection_policy_hard_veto_reasons": row.get("selection_policy_hard_veto_reasons", ""),
            "selection_policy_hard_weakness_penalty": row.get("selection_policy_hard_weakness_penalty", ""),
            "selection_policy_hard_weakness_penalty_reasons": row.get(
                "selection_policy_hard_weakness_penalty_reasons", ""
            ),
            "selection_policy_soft_weakness_penalty": row.get("selection_policy_soft_weakness_penalty", ""),
            "selection_policy_targeted_soft_weakness_penalty": row.get(
                "selection_policy_targeted_soft_weakness_penalty", ""
            ),
            "selection_policy_targeted_soft_weakness_penalty_reasons": row.get(
                "selection_policy_targeted_soft_weakness_penalty_reasons", ""
            ),
        }
        for reason in SOFT_REASON_REPORT_COLUMNS:
            payload[f"test_soft_reason_{reason}_exposure_pct"] = rounded(
                row.get(f"test_selected_soft_reason_{reason}_exposure_pct")
            )
        for column in WEIGHT_COLUMNS:
            payload[column] = row.get(column, "")
        payload["recommendation_bucket"] = recommendation_bucket(payload)
        out.append(payload)

    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in out:
        grouped[(str(row.get("sample") or ""), str(row.get("top_n") or ""))].append(row)
    ranked_out: list[dict[str, Any]] = []
    for _, rows_for_group in sorted(grouped.items()):
        ranked = sorted(rows_for_group, key=holdout_report_sort_key, reverse=True)
        for rank, row in enumerate(ranked, start=1):
            ranked_out.append({"production_rank_within_sample_top_n": rank, **row})
    return ranked_out


def build_policy_summary(candidate_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in candidate_rows:
        grouped[(str(row.get("sample") or ""), str(row.get("selection_policy_name") or ""))].append(row)

    out: list[dict[str, Any]] = []
    for (sample, policy), rows in sorted(grouped.items()):
        positive_ci_count = sum(
            1
            for row in rows
            if (ci05 := to_float(row.get("bootstrap_lcb_return_pct_ci05"))) is not None and ci05 > 0.0
        )
        out.append(
            {
                "sample": sample,
                "selection_policy_name": policy,
                "confirmed_rows": len(rows),
                "positive_lcb_ci05_rows": positive_ci_count,
                "positive_lcb_ci05_rate_pct": round(100.0 * positive_ci_count / len(rows), 6) if rows else "",
                "avg_test_lcb_return_pct": mean_numeric(rows, "test_lcb_return_pct"),
                "avg_test_sortino_like": mean_numeric(rows, "test_sortino_like"),
                "avg_test_profit_factor": mean_numeric(rows, "test_profit_factor"),
                "avg_test_large_loss_20pct_rate_pct": mean_numeric(rows, "test_large_loss_20pct_rate_pct"),
                "avg_test_core_hard_weakness_exposure_pct": mean_numeric(
                    rows, "test_core_hard_weakness_exposure_pct"
                ),
                "avg_test_event_hard_weakness_exposure_pct": mean_numeric(
                    rows, "test_event_hard_weakness_exposure_pct"
                ),
                "avg_test_soft_weakness_exposure_pct": mean_numeric(rows, "test_soft_weakness_exposure_pct"),
                "avg_test_illiquid_weakness_exposure_pct": mean_numeric(
                    rows, "test_illiquid_weakness_exposure_pct"
                ),
                "avg_test_top3_gain_contribution_pct": mean_numeric(rows, "test_top3_gain_contribution_pct"),
                "best_candidate_name": max(rows, key=holdout_report_sort_key).get("candidate_name", "") if rows else "",
                "best_top_n": max(rows, key=holdout_report_sort_key).get("top_n", "") if rows else "",
            }
        )
    return sorted(
        out,
        key=lambda row: (
            numeric_or_default(row.get("avg_test_lcb_return_pct"), -1e9),
            numeric_or_default(row.get("avg_test_sortino_like"), -1e9),
            numeric_or_default(row.get("avg_test_profit_factor"), -1e9),
            -numeric_or_default(row.get("avg_test_large_loss_20pct_rate_pct"), 100.0),
            -numeric_or_default(row.get("avg_test_core_hard_weakness_exposure_pct"), 100.0),
            -numeric_or_default(row.get("avg_test_event_hard_weakness_exposure_pct"), 100.0),
            -numeric_or_default(row.get("avg_test_illiquid_weakness_exposure_pct"), 100.0),
            -numeric_or_default(row.get("avg_test_top3_gain_contribution_pct"), 100.0),
        ),
        reverse=True,
    )


def build_ticker_audit(
    ticker_rows: list[dict[str, Any]],
    candidate_rows: list[dict[str, Any]],
    *,
    rank_limit: int,
    ticker_split: str,
) -> list[dict[str, Any]]:
    selected_keys: set[tuple[str, str, str, str, str, str]] = set()
    selected_meta: dict[tuple[str, str, str, str, str, str], dict[str, Any]] = {}
    for row in candidate_rows:
        production_rank = to_int(row.get("production_rank_within_sample_top_n"))
        bucket = str(row.get("recommendation_bucket") or "")
        include = production_rank <= max(1, rank_limit) or bucket == "current_config_benchmark"
        if not include:
            continue
        for split in ["train", "test"]:
            if ticker_split != "both" and split != ticker_split:
                continue
            key = (
                str(row.get("sample") or ""),
                split,
                str(row.get("horizon_days") or ""),
                str(row.get("top_n") or ""),
                str(row.get("train_rank") or ""),
                str(row.get("candidate_id") or ""),
            )
            selected_keys.add(key)
            selected_meta[key] = row

    out: list[dict[str, Any]] = []
    for row in ticker_rows:
        key = candidate_key(row)
        if key not in selected_keys:
            continue
        meta = selected_meta.get(key, {})
        out.append(
            {
                "production_rank_within_sample_top_n": meta.get("production_rank_within_sample_top_n", ""),
                "recommendation_bucket": meta.get("recommendation_bucket", ""),
                "sample": row.get("sample", ""),
                "evaluation_split": row.get("evaluation_split", ""),
                "horizon_days": row.get("horizon_days", ""),
                "top_n": row.get("top_n", ""),
                "train_rank": row.get("train_rank", ""),
                "candidate_id": row.get("candidate_id", ""),
                "candidate_name": row.get("candidate_name", ""),
                "selection_policy_name": row.get("selection_policy_name", ""),
                "asof_date": row.get("asof_date", ""),
                "selected_rank_within_date": row.get("selected_rank_within_date", ""),
                "ticker": row.get("ticker", ""),
                "company_name": row.get("company_name", ""),
                "net_forward_return_pct": rounded(row.get("net_forward_return_pct")),
                "gross_forward_return_pct": rounded(row.get("gross_forward_return_pct")),
                "entry_date": row.get("entry_date", ""),
                "target_date": row.get("target_date", ""),
                "candidate_selection_score": rounded(row.get("candidate_selection_score")),
                "risk_score_raw": rounded(row.get("risk_score_raw")),
                "binary_weakness_severity": row.get("binary_weakness_severity", ""),
                "core_hard_weakness_reasons": row.get("core_hard_weakness_reasons", ""),
                "event_hard_weakness_reasons": row.get("event_hard_weakness_reasons", ""),
                "soft_weakness_reasons": row.get("soft_weakness_reasons", ""),
                "liquidity_ok": row.get("liquidity_ok", ""),
                "cash_runway_months": rounded(row.get("cash_runway_months")),
                "verified_active_trial_count": rounded(row.get("verified_active_trial_count")),
                "has_advanced_trial_anchor": row.get("has_advanced_trial_anchor", ""),
                "has_business_anchor": row.get("has_business_anchor", ""),
            }
        )
    return sorted(
        out,
        key=lambda row: (
            str(row.get("sample") or ""),
            to_int(row.get("top_n")),
            to_int(row.get("production_rank_within_sample_top_n")),
            str(row.get("evaluation_split") or ""),
            str(row.get("asof_date") or ""),
            to_int(row.get("selected_rank_within_date")),
        ),
    )


def main() -> None:
    configure_logging()
    args = parse_args()
    start = time.perf_counter()
    input_dir = args.input_dir.expanduser().resolve()
    output_dir = args.output_dir.expanduser().resolve() if args.output_dir else input_dir

    holdout_rows = read_csv(input_dir / HOLDOUT_FILE)
    bootstrap_rows = read_csv(input_dir / BOOTSTRAP_FILE)
    ticker_rows = read_csv(input_dir / TICKER_DIAGNOSTICS_FILE)
    if not bootstrap_rows:
        LOGGER.warning(
            "%s contains no bootstrap rows. Run script 28 with --bootstrap-iterations > 0 "
            "if bootstrap confidence intervals are required.",
            BOOTSTRAP_FILE,
        )
    apply_legacy_column_aliases(holdout_rows, HOLDOUT_LEGACY_ALIASES, file_label=HOLDOUT_FILE)
    validate_required_columns(
        holdout_rows,
        file_label=HOLDOUT_FILE,
        required={
            "sample",
            "horizon_days",
            "top_n",
            "train_rank",
            "candidate_id",
            "candidate_name",
            "selection_policy_name",
            "train_calibration_pass",
            "test_calibration_pass",
            "test_selected_lcb_return_pct",
            "test_selected_sortino_like",
            "test_selected_profit_factor",
        },
    )
    validate_required_columns(
        bootstrap_rows,
        file_label=BOOTSTRAP_FILE,
        required={
            "sample",
            "horizon_days",
            "top_n",
            "train_rank",
            "candidate_id",
            "selected_lcb_return_pct_ci05",
            "selected_lcb_return_pct_ci95",
            "selected_sortino_like_ci05",
            "selected_sortino_like_ci95",
            "selected_profit_factor_ci05",
            "selected_profit_factor_ci95",
        },
    )
    validate_required_columns(
        ticker_rows,
        file_label=TICKER_DIAGNOSTICS_FILE,
        required={
            "sample",
            "evaluation_split",
            "horizon_days",
            "top_n",
            "train_rank",
            "candidate_id",
            "asof_date",
            "selected_rank_within_date",
            "ticker",
            "candidate_selection_score",
        },
    )

    candidate_rows = build_candidate_report(holdout_rows, bootstrap_rows, horizon=str(args.horizon))
    policy_rows = build_policy_summary(candidate_rows)
    ticker_audit_rows = build_ticker_audit(
        ticker_rows,
        candidate_rows,
        rank_limit=max(1, int(args.candidate_rank_limit)),
        ticker_split=str(args.ticker_split),
    )

    write_csv(output_dir / "tier1_120d_production_candidate_report.csv", candidate_rows)
    write_csv(output_dir / "tier1_120d_policy_summary.csv", policy_rows)
    write_csv(output_dir / "tier1_120d_production_candidate_tickers.csv", ticker_audit_rows)
    write_json(
        output_dir / "tier1_120d_candidate_report_manifest.json",
        {
            "script": Path(__file__).name,
            "input_dir": str(input_dir),
            "output_dir": str(output_dir),
            "horizon": str(args.horizon),
            "holdout_rows": len(holdout_rows),
            "bootstrap_rows": len(bootstrap_rows),
            "ticker_diagnostic_rows": len(ticker_rows),
            "production_candidate_rows": len(candidate_rows),
            "policy_summary_rows": len(policy_rows),
            "ticker_audit_rows": len(ticker_audit_rows),
            "candidate_rank_limit": max(1, int(args.candidate_rank_limit)),
            "ticker_split": str(args.ticker_split),
            "notes": [
                "This report summarizes an already-completed Script 28 calibration run.",
                "production_rank_within_sample_top_n ranks confirmed train/test pass candidates by bootstrap LCB lower bound, test LCB, Sortino, profit factor, large-loss rate, core hard exposure, and top-winner concentration.",
                "The ticker audit is limited to top-ranked production candidates and current_config benchmark rows available in Script 28 selected ticker diagnostics.",
                "No production config or database tables are changed by this script.",
            ],
            "elapsed_sec": round(time.perf_counter() - start, 3),
        },
    )
    LOGGER.info(
        "Tier-1 120d production candidate report written: output_dir=%s candidates=%d tickers=%d elapsed=%.3fs",
        output_dir,
        len(candidate_rows),
        len(ticker_audit_rows),
        time.perf_counter() - start,
    )


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
    except BaseException as exc:
        if isinstance(exc, SystemExit) and exc.code in (0, None):
            raise
        LOGGER.exception("Unhandled exception in main()")
        sys.exit(1)
