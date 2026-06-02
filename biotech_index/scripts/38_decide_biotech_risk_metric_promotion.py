#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import math
import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from biotech_index.core.config import cfg_get, load_yaml, resolve_path  # noqa: E402


DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"


@dataclass(frozen=True)
class MetricSet:
    horizon_days: int
    top_n: int
    lcb_pct: float
    mean_pct: float
    profit_factor: float
    loss20_rate_pct: float


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Create a promotion decision artifact for the biotech risk metric redesign."
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--output-dir", type=Path, default=None)
    parser.add_argument("--decision-date", type=str, default=date.today().isoformat())
    return parser.parse_args()


def to_float(raw: object, default: float = math.nan) -> float:
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return default
    return value if math.isfinite(value) else default


def to_int(raw: object, default: int = 0) -> int:
    value = to_float(raw, math.nan)
    return int(value) if math.isfinite(value) else default


def read_csv_rows(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        raise FileNotFoundError(f"Required validation artifact not found: {path}")
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def load_current_config_holdout(path: Path) -> dict[tuple[int, int], MetricSet]:
    rows = read_csv_rows(path)
    metrics: dict[tuple[int, int], MetricSet] = {}
    for row in rows:
        if str(row.get("sample") or "").strip() != "all":
            continue
        if str(row.get("candidate_name") or "").strip() != "current_config":
            continue
        if str(row.get("selection_policy_name") or "").strip() != "core_structural_veto":
            continue
        horizon = to_int(row.get("horizon_days"))
        top_n = to_int(row.get("top_n"))
        if horizon <= 0 or top_n <= 0:
            continue
        metrics[(horizon, top_n)] = MetricSet(
            horizon_days=horizon,
            top_n=top_n,
            lcb_pct=to_float(row.get("test_selected_lcb_return_pct")),
            mean_pct=to_float(row.get("test_selected_mean_return_pct")),
            profit_factor=to_float(row.get("test_selected_profit_factor")),
            loss20_rate_pct=to_float(row.get("test_selected_large_loss_20pct_rate_pct")),
        )
    if not metrics:
        raise ValueError(f"No current_config/core_structural_veto holdout rows found in {path}")
    return metrics


def compare_holdout_modes(
    *,
    baseline_name: str,
    challenger_name: str,
    baseline: dict[tuple[int, int], MetricSet],
    challenger: dict[tuple[int, int], MetricSet],
) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    blocking: list[str] = []
    for key in sorted(set(baseline).intersection(challenger)):
        base = baseline[key]
        cand = challenger[key]
        row = {
            "horizon_days": base.horizon_days,
            "top_n": base.top_n,
            "baseline_lcb_pct": round(base.lcb_pct, 6),
            "challenger_lcb_pct": round(cand.lcb_pct, 6),
            "delta_lcb_pct": round(cand.lcb_pct - base.lcb_pct, 6),
            "baseline_profit_factor": round(base.profit_factor, 6),
            "challenger_profit_factor": round(cand.profit_factor, 6),
            "delta_profit_factor": round(cand.profit_factor - base.profit_factor, 6),
            "baseline_loss20_rate_pct": round(base.loss20_rate_pct, 6),
            "challenger_loss20_rate_pct": round(cand.loss20_rate_pct, 6),
            "delta_loss20_rate_pct": round(cand.loss20_rate_pct - base.loss20_rate_pct, 6),
        }
        rows.append(row)
        if base.top_n == 10 and cand.lcb_pct < base.lcb_pct:
            blocking.append(
                f"{base.horizon_days}d Top10 LCB degraded "
                f"{cand.lcb_pct - base.lcb_pct:.2f} pct points"
            )
        if base.top_n == 10 and cand.profit_factor < base.profit_factor:
            blocking.append(
                f"{base.horizon_days}d Top10 profit factor degraded "
                f"{cand.profit_factor - base.profit_factor:.2f}"
            )
        if base.top_n == 10 and cand.loss20_rate_pct > base.loss20_rate_pct + 3.0:
            blocking.append(
                f"{base.horizon_days}d Top10 20% loss rate increased "
                f"{cand.loss20_rate_pct - base.loss20_rate_pct:.2f} pct points"
            )
    missing = sorted(set(baseline).symmetric_difference(challenger))
    if missing:
        blocking.append(f"mode comparison has missing horizon/top_n keys: {missing}")
    return {
        "baseline_mode": baseline_name,
        "challenger_mode": challenger_name,
        "status": "reject_for_allocation" if blocking else "allocation_candidate",
        "blocking_issues": blocking,
        "comparisons": rows,
    }


def evaluate_routed_discovery(path: Path) -> dict[str, Any]:
    rows = read_csv_rows(path)
    blocking: list[str] = []
    warnings: list[str] = []
    comparisons: list[dict[str, Any]] = []
    for row in rows:
        horizon = to_int(row.get("horizon_days"))
        top_n = to_int(row.get("top_n"))
        delta_lcb = to_float(row.get("delta_lcb_pct"))
        delta_pf = to_float(row.get("updated_profit_factor")) - to_float(row.get("current_profit_factor"))
        delta_loss20 = to_float(row.get("updated_loss20_rate_pct")) - to_float(row.get("current_loss20_rate_pct"))
        updated_late_share = to_float(row.get("updated_late_clinical_share_pct"))
        comparisons.append(
            {
                "horizon_days": horizon,
                "top_n": top_n,
                "delta_lcb_pct": round(delta_lcb, 6),
                "delta_profit_factor": round(delta_pf, 6),
                "delta_loss20_rate_pct": round(delta_loss20, 6),
                "updated_late_clinical_share_pct": round(updated_late_share, 6),
            }
        )
        if delta_lcb < 0.0:
            blocking.append(f"{horizon}d Top{top_n} routed discovery LCB degraded {delta_lcb:.2f} pct points")
        if delta_pf < 0.0:
            blocking.append(f"{horizon}d Top{top_n} routed discovery profit factor degraded {delta_pf:.2f}")
        if delta_loss20 > 3.0:
            blocking.append(
                f"{horizon}d Top{top_n} routed discovery 20% loss rate increased {delta_loss20:.2f} pct points"
            )
        if updated_late_share > 75.0:
            warnings.append(
                f"{horizon}d Top{top_n} routed discovery is concentrated in late_clinical_pivotal "
                f"({updated_late_share:.1f}%)"
            )
    return {
        "status": "shadow_promote_discovery_only" if not blocking else "reject",
        "blocking_issues": blocking,
        "warnings": warnings,
        "comparisons": comparisons,
    }


def evaluate_cohort_discovery(path: Path) -> list[dict[str, Any]]:
    rows = read_csv_rows(path)
    decisions: list[dict[str, Any]] = []
    for row in rows:
        cohort = str(row.get("cohort") or "").strip()
        updated_n = to_int(row.get("updated_n"))
        updated_unique = to_int(row.get("updated_unique_tickers"))
        delta_lcb = to_float(row.get("delta_lcb_pct"))
        delta_pf = to_float(row.get("updated_profit_factor")) - to_float(row.get("current_profit_factor"))
        delta_loss20 = to_float(row.get("updated_loss20_rate_pct")) - to_float(row.get("current_loss20_rate_pct"))
        if updated_n <= 0:
            status = "not_selected_by_routed_discovery"
        elif cohort in {"late_clinical_pivotal", "mid_clinical_phase2_poc"}:
            passes = updated_unique >= 5 and delta_lcb >= 0.5 and delta_pf >= 0.10 and delta_loss20 <= 3.0
            status = "shadow_promote_discovery_only" if passes else "insufficient_for_promotion"
        elif delta_lcb < 0.0 or delta_pf < 0.0:
            status = "do_not_promote_for_this_cohort"
        else:
            status = "neutral_non_target_cohort"
        decisions.append(
            {
                "cohort": cohort,
                "status": status,
                "current_n": to_int(row.get("current_n")),
                "updated_n": updated_n,
                "current_unique_tickers": to_int(row.get("current_unique_tickers")),
                "updated_unique_tickers": updated_unique,
                "delta_lcb_pct": None if not math.isfinite(delta_lcb) else round(delta_lcb, 6),
                "delta_profit_factor": None if not math.isfinite(delta_pf) else round(delta_pf, 6),
                "delta_loss20_rate_pct": None if not math.isfinite(delta_loss20) else round(delta_loss20, 6),
            }
        )
    return decisions


def evaluate_routed_discovery_robustness(path: Path) -> dict[str, Any]:
    """Read the strict routed-discovery robustness gate if it exists.

    The performance-comparison artifact is useful for explaining the upside, but
    promotion must use the stricter robustness artifact because it includes
    bootstrap win rate, breadth, top-gain concentration, and known false-positive
    checks.
    """
    if not path.exists():
        return {
            "available": False,
            "status": "missing_robustness_gate",
            "blocking_issues": [f"missing robustness artifact: {path}"],
            "warnings": [],
            "test_top20_rows": [],
        }
    with path.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    gate_settings = payload.get("gate_settings", {}) if isinstance(payload.get("gate_settings"), dict) else {}
    min_breadth = to_float(gate_settings.get("min_improved_unique_ticker_rate_pct"), 60.0)
    promotion_top_n = gate_settings.get("promotion_top_n", [20])
    test_top20_rows = payload.get("promotion_rows") or payload.get("test_top20_rows", [])
    blocking: list[str] = []
    warnings: list[str] = []
    for row in test_top20_rows:
        horizon = to_int(row.get("horizon_days"))
        top_n = to_int(row.get("top_n"))
        fail_reasons = str(row.get("gate_fail_reasons") or "").strip()
        if fail_reasons:
            blocking.append(f"{horizon}d Top{top_n}: {fail_reasons}")
        breadth = to_float(row.get("improved_unique_ticker_rate_pct"))
        if math.isfinite(breadth) and breadth < min_breadth:
            warnings.append(f"{horizon}d Top{top_n} breadth {breadth:.1f}% below {min_breadth:g}% promotion gate")
        bootstrap_win = to_float(row.get("bootstrap_win_rate_pct"))
        delta_lcb_p05 = to_float(row.get("bootstrap_delta_lcb_p05_pct"))
        if math.isfinite(bootstrap_win) and math.isfinite(delta_lcb_p05):
            warnings.append(
                f"{horizon}d Top{top_n} bootstrap win {bootstrap_win:.1f}%, "
                f"LCB p05 delta {delta_lcb_p05:.2f}%"
            )
    raw_status = str(payload.get("status") or "").strip().lower()
    if raw_status == "pass" and not blocking:
        status = "operational_shadow_discovery"
    elif blocking and all("improved_unique_ticker_rate_lt_" in issue for issue in blocking):
        status = "shadow_candidate_pending_breadth"
    else:
        status = "reject"
    return {
        "available": True,
        "status": status,
        "raw_status": raw_status,
        "blocking_issues": blocking,
        "warnings": sorted(set(warnings)),
        "test_top20_rows": test_top20_rows,
        "gate_settings": gate_settings,
        "promotion_top_n": promotion_top_n,
        "source_artifact": str(path),
    }


def write_decision_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    fieldnames = [
        "area",
        "status",
        "scope",
        "primary_reason",
        "blocking_issues",
        "warnings",
        "next_action",
    ]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def main() -> int:
    args = parse_args()
    config = load_yaml(args.config)
    base_dir = args.config.resolve().parent
    output_dir = (
        args.output_dir.resolve()
        if args.output_dir is not None
        else resolve_path(cfg_get(config, "biotech_reports.output_dir", "../output/biotech_index_reports"), base_dir=base_dir)
    )
    output_dir.mkdir(parents=True, exist_ok=True)

    legacy_holdout = load_current_config_holdout(output_dir / "risk_mode_validation_legacy" / "tier1_weight_calibration_holdout.csv")
    decomposed_holdout = load_current_config_holdout(
        output_dir / "risk_mode_validation_decomposed" / "tier1_weight_calibration_holdout.csv"
    )
    predictive_holdout = load_current_config_holdout(
        output_dir / "risk_mode_validation_predictive" / "tier1_weight_calibration_holdout.csv"
    )
    decomposed_allocation = compare_holdout_modes(
        baseline_name="legacy",
        challenger_name="decomposed",
        baseline=legacy_holdout,
        challenger=decomposed_holdout,
    )
    predictive_allocation = compare_holdout_modes(
        baseline_name="legacy",
        challenger_name="predictive",
        baseline=legacy_holdout,
        challenger=predictive_holdout,
    )
    routed_discovery = evaluate_routed_discovery(output_dir / "risk_mode_routed_discovery_performance_comparison.csv")
    cohort_discovery = evaluate_cohort_discovery(
        output_dir / "risk_mode_routed_discovery_by_cohort_aggregate_comparison.csv"
    )
    routed_robustness = evaluate_routed_discovery_robustness(
        output_dir / f"risk_mode_routed_discovery_robustness_{args.decision_date.replace('-', '')}.json"
    )
    production_score_source = str(
        cfg_get(config, "biotech_scoring.risk_mode_routing.production_score_source", "opportunity_score")
        or "opportunity_score"
    ).strip().lower()
    production_allocation_decision = (
        "promote_routed_discovery_rank_with_legacy_audit"
        if production_score_source in {"routed_discovery", "discovery", "discovery_opportunity_score"}
        and routed_robustness["status"] == "operational_shadow_discovery"
        else "keep_legacy_risk"
    )
    routed_discovery_status = routed_robustness["status"]
    routed_discovery_blockers = routed_robustness["blocking_issues"]
    routed_discovery_warnings = routed_robustness["warnings"]
    if routed_discovery_status == "operational_shadow_discovery":
        if production_allocation_decision != "keep_legacy_risk":
            routed_discovery_next_action = (
                "use production_rank_score for production rank; keep legacy opportunity_score for audit"
            )
            routed_discovery_reason = "passes strict robustness gate and breadth exception is approved"
        else:
            routed_discovery_next_action = "enable as operational shadow discovery ranking; keep allocation unchanged"
            routed_discovery_reason = "passes strict robustness gate"
    elif routed_discovery_status == "shadow_candidate_pending_breadth":
        routed_discovery_next_action = "keep as shadow candidate; improve ticker breadth before promotion"
        routed_discovery_reason = "passes return/bootstrap/loss gates but fails breadth gate"
    else:
        routed_discovery_next_action = "do not promote routed discovery"
        routed_discovery_reason = "fails strict robustness gate"

    decision_rows = [
        {
            "area": "decomposed_risk_as_global_allocation_penalty",
            "status": decomposed_allocation["status"],
            "scope": "production_allocation",
            "primary_reason": "fails strict Top10 allocation non-degradation gate"
            if decomposed_allocation["blocking_issues"]
            else "passes allocation gate",
            "blocking_issues": "; ".join(decomposed_allocation["blocking_issues"]),
            "warnings": "",
            "next_action": "do not enable decomposed risk for production allocation",
        },
        {
            "area": "predictive_risk_as_global_allocation_penalty",
            "status": predictive_allocation["status"],
            "scope": "production_allocation",
            "primary_reason": "fails strict Top10 allocation non-degradation gate"
            if predictive_allocation["blocking_issues"]
            else "passes allocation gate",
            "blocking_issues": "; ".join(predictive_allocation["blocking_issues"]),
            "warnings": "",
            "next_action": "keep production allocation on legacy risk",
        },
        {
            "area": "routed_discovery_score",
            "status": routed_discovery_status,
            "scope": "production_rank_and_shadow_discovery_top20"
            if production_allocation_decision != "keep_legacy_risk"
            else "shadow_discovery_top20",
            "primary_reason": routed_discovery_reason,
            "blocking_issues": "; ".join(routed_discovery_blockers),
            "warnings": "; ".join(routed_discovery_warnings),
            "next_action": routed_discovery_next_action,
        },
    ]
    for row in cohort_discovery:
        decision_rows.append(
            {
                "area": f"cohort_discovery:{row['cohort']}",
                "status": row["status"],
                "scope": "cohort_shadow_discovery",
                "primary_reason": (
                    f"LCB delta {row['delta_lcb_pct']}, PF delta {row['delta_profit_factor']}, "
                    f"20% loss delta {row['delta_loss20_rate_pct']}"
                ),
                "blocking_issues": "",
                "warnings": "",
                "next_action": "retain routed discovery only for validated clinical cohorts"
                if row["status"] == "shadow_promote_discovery_only"
                else "do not promote this cohort from the risk metric work",
            }
        )

    decision = {
        "decision_date": args.decision_date,
        "metric_family": "biotech risk penalty and routed discovery risk",
        "final_decision": {
            "production_allocation": production_allocation_decision,
            "global_predictive_or_decomposed_allocation": "reject",
            "routed_discovery": routed_discovery_status,
            "validated_discovery_cohorts": [
                row["cohort"] for row in cohort_discovery if row["status"] == "shadow_promote_discovery_only"
            ]
            if routed_discovery_status == "operational_shadow_discovery"
            else [],
        },
        "allocation_mode_comparisons": {
            "decomposed": decomposed_allocation,
            "predictive": predictive_allocation,
        },
        "routed_discovery": routed_discovery,
        "routed_discovery_robustness": routed_robustness,
        "production_score_source": production_score_source,
        "cohort_discovery": cohort_discovery,
        "source_artifacts": [
            str(output_dir / "risk_mode_validation_legacy" / "tier1_weight_calibration_holdout.csv"),
            str(output_dir / "risk_mode_validation_decomposed" / "tier1_weight_calibration_holdout.csv"),
            str(output_dir / "risk_mode_validation_predictive" / "tier1_weight_calibration_holdout.csv"),
            str(output_dir / "risk_mode_routed_discovery_performance_comparison.csv"),
            str(output_dir / "risk_mode_routed_discovery_by_cohort_aggregate_comparison.csv"),
            str(
                output_dir
                / f"risk_mode_routed_discovery_robustness_{args.decision_date.replace('-', '')}.json"
            ),
        ],
    }

    json_path = output_dir / f"risk_metric_promotion_decision_{args.decision_date.replace('-', '')}.json"
    csv_path = output_dir / f"risk_metric_promotion_decision_{args.decision_date.replace('-', '')}.csv"
    with json_path.open("w", encoding="utf-8") as handle:
        json.dump(decision, handle, indent=2, sort_keys=True)
        handle.write("\n")
    write_decision_csv(csv_path, decision_rows)

    print(f"Wrote {json_path}")
    print(f"Wrote {csv_path}")
    print(
        f"Decision: production_allocation={production_allocation_decision}; "
        f"routed discovery status={routed_discovery_status}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
