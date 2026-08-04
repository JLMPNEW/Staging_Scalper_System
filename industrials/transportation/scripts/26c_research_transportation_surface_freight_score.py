#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Mapping


PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from industrials.core.config import cfg_get, load_yaml, resolve_path  # noqa: E402
from industrials.core.oos_research import (  # noqa: E402
    artifact_sha256,
    evaluate_candidate,
    finite_float,
    finite_or_default,
    fmt,
    weighted_score,
)
from industrials.core.reports import write_csv_atomic, write_text_atomic  # noqa: E402
from industrials.transportation.contracts import read_rows  # noqa: E402
from industrials.transportation.financial_contract import (  # noqa: E402
    load_metric_registry,
)
from industrials.transportation.surface_freight_research import (  # noqa: E402
    add_positioning_research_scores,
    build_directional_metric_scores,
    load_surface_freight_policy,
    metric_ic_diagnostics,
    positioning_research_definition,
    select_train_metrics,
    select_train_mean_reversion_metrics,
    top_bottom_diagnostic,
    train_derived_candidate_registry,
)
from industrials.transportation.scripts._shared import DEFAULT_CONFIG  # noqa: E402


DEFAULT_POLICY = (
    PROJECT_ROOT
    / "industrials"
    / "transportation"
    / "data"
    / "transportation_surface_freight_research_policy.yaml"
)
DEFAULT_REGISTRY = (
    PROJECT_ROOT
    / "industrials"
    / "transportation"
    / "data"
    / "transportation_metric_registry.yaml"
)
DEFAULT_PANEL_DIR = (
    PROJECT_ROOT
    / "output"
    / "industrials"
    / "transportation"
    / "research_redesign"
    / "surface_freight_v1"
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Research an outcome-blind surface-freight cohort and derive a "
            "bounded metric score from train-only IC evidence."
        )
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--policy", type=Path, default=DEFAULT_POLICY)
    parser.add_argument("--registry", type=Path, default=DEFAULT_REGISTRY)
    parser.add_argument("--panel-dir", type=Path, default=DEFAULT_PANEL_DIR)
    parser.add_argument("--latest-snapshot-dir", type=Path, default=None)
    parser.add_argument("--output-dir", type=Path, default=None)
    parser.add_argument("--allow-overwrite", action="store_true")
    return parser.parse_args()


def gate_result(
    metrics: Mapping[str, object],
    gates: Mapping[str, float],
) -> tuple[bool, list[str]]:
    checks = {
        "minimum_snapshot_count": (
            float(metrics.get("snapshot_count") or 0)
            >= gates["minimum_snapshot_count"]
        ),
        "minimum_outcome_coverage": (
            float(metrics.get("outcome_coverage") or 0)
            >= gates["minimum_outcome_coverage"]
        ),
        "minimum_mean_ic": (
            finite_float(metrics.get("mean_ic")) is not None
            and float(metrics["mean_ic"]) >= gates["minimum_mean_ic"]
        ),
        "minimum_mean_top_excess_net": (
            finite_float(metrics.get("mean_top_excess_net")) is not None
            and float(metrics["mean_top_excess_net"])
            > gates["minimum_mean_top_excess_net"]
        ),
        "minimum_top_excess_hit_rate": (
            finite_float(metrics.get("top_excess_hit_rate")) is not None
            and float(metrics["top_excess_hit_rate"])
            >= gates["minimum_top_excess_hit_rate"]
        ),
        "minimum_max_drawdown": (
            finite_float(metrics.get("max_drawdown")) is not None
            and float(metrics["max_drawdown"])
            >= gates["minimum_max_drawdown"]
        ),
        "maximum_average_turnover": (
            finite_float(metrics.get("average_turnover")) is not None
            and float(metrics["average_turnover"])
            <= gates["maximum_average_turnover"]
        ),
    }
    failed = [name for name, passed in checks.items() if not passed]
    return not failed, failed


def evaluate(
    rows: list[dict[str, object]],
    *,
    weights: Mapping[str, float],
    split: str,
    standards: Mapping[str, object],
) -> dict[str, Any]:
    return evaluate_candidate(
        rows,
        weights=weights,
        split=split,
        horizon_sessions=63,
        top_fraction=float(standards["top_fraction"]),
        minimum_cross_section=int(standards["minimum_cross_section"]),
        transaction_cost_bps=float(standards["transaction_cost_bps"]),
        require_complete_components=True,
    )


def metric_report_rows(
    diagnostics: list[dict[str, object]],
    selected: list[dict[str, object]],
) -> list[dict[str, str]]:
    selected_ids = {str(row["metric_id"]) for row in selected}
    output: list[dict[str, str]] = []
    for row in diagnostics:
        output.append(
            {
                "metric_id": str(row["metric_id"]),
                "component": str(row["component"]),
                "specialized": str(row["specialized"]),
                "direction": str(row["direction"]),
                "applicable_row_count": str(row["applicable_row_count"]),
                "observed_row_count": str(row["observed_row_count"]),
                "observation_coverage": fmt(row["observation_coverage"]),
                "ic_snapshot_count": str(row["ic_snapshot_count"]),
                "mean_ic": fmt(row["mean_ic"]),
                "positive_ic_snapshot_rate": fmt(
                    row["positive_ic_snapshot_rate"]
                ),
                "subperiod_ics": json.dumps(row["subperiod_ics"]),
                "positive_subperiod_count": str(
                    row["positive_subperiod_count"]
                ),
                "selected_for_candidates": (
                    "1" if str(row["metric_id"]) in selected_ids else "0"
                ),
            }
        )
    return output


def main() -> int:
    args = parse_args()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    base_dir = config_path.parent
    standards = cfg_get(
        config,
        "oos_calibration_standards.families.transportation",
    )
    if not isinstance(standards, dict):
        raise ValueError("transportation OOS standards are missing")
    policy_path = args.policy.expanduser().resolve()
    registry_path = args.registry.expanduser().resolve()
    panel_dir = args.panel_dir.expanduser().resolve()
    output_dir = (
        args.output_dir.expanduser().resolve()
        if args.output_dir is not None
        else panel_dir / "surface_freight_score_research"
    )
    output_paths = {
        "active": output_dir / "transportation_surface_freight_active_cohort.csv",
        "metrics": output_dir / "transportation_surface_freight_metric_diagnostics.csv",
        "candidates": output_dir / "transportation_surface_freight_candidate_results.csv",
        "registry": output_dir / "transportation_surface_freight_candidate_registry.json",
        "manifest": output_dir / "transportation_surface_freight_research_manifest.json",
    }
    if not args.allow_overwrite and any(path.exists() for path in output_paths.values()):
        raise FileExistsError(
            "surface-freight research artifacts already exist; use --allow-overwrite"
        )

    panel_path = panel_dir / "transportation_generic_oos_panel.csv"
    panel_manifest_path = panel_dir / "transportation_generic_oos_panel_manifest.json"
    panel_validation_path = panel_dir / "transportation_generic_oos_panel_validation.json"
    for path in (panel_path, panel_manifest_path, panel_validation_path):
        if not path.is_file():
            raise FileNotFoundError(path)
    panel_manifest = json.loads(panel_manifest_path.read_text(encoding="utf-8"))
    panel_validation = json.loads(panel_validation_path.read_text(encoding="utf-8"))
    if (
        panel_manifest.get("acceptance") != "PASS"
        or panel_validation.get("acceptance") != "PASS"
        or panel_manifest.get("panel_sha256") != artifact_sha256(panel_path)
        or panel_validation.get("panel_sha256") != artifact_sha256(panel_path)
        or panel_validation.get("return_reconstruction", {}).get("acceptance")
        != "PASS"
    ):
        raise ValueError("redesign panel has not passed frozen-price validation")

    policy = load_surface_freight_policy(policy_path)
    registry_version, definitions = load_metric_registry(registry_path)
    research_definitions = [*definitions, positioning_research_definition()]
    raw_panel_rows = [
        row
        for row in read_rows(panel_path)
        if row.get("horizon_sessions") == "63"
    ]
    scored_rows = build_directional_metric_scores(
        raw_panel_rows,
        definitions=definitions,
        policy=policy,
    )
    scored_rows = add_positioning_research_scores(scored_rows)
    research_gates = policy["metric_research_gates"]
    diagnostics = metric_ic_diagnostics(
        scored_rows,
        definitions=research_definitions,
        split="train",
        subperiod_count=int(research_gates["train_subperiod_count"]),
        minimum_cross_section=int(standards["minimum_cross_section"]),
    )
    selected_metrics = select_train_metrics(diagnostics, policy=policy)
    mean_reversion_metrics = select_train_mean_reversion_metrics(
        diagnostics,
        policy=policy,
    )
    candidates = train_derived_candidate_registry(
        selected_metrics,
        policy=policy,
        mean_reversion_metrics=mean_reversion_metrics,
    )
    metric_rows = metric_report_rows(diagnostics, selected_metrics)
    write_csv_atomic(
        output_paths["metrics"],
        list(metric_rows[0]) if metric_rows else ["metric_id"],
        metric_rows,
    )

    gates = {
        str(key): float(value)
        for key, value in standards["absolute_gates"].items()
    }
    candidate_results: list[dict[str, str]] = []
    validation_metrics: dict[str, dict[str, Any]] = {}
    all_metrics: dict[tuple[str, str], dict[str, Any]] = {}
    for candidate_id, weights in candidates.items():
        for split in ("train", "validation", "holdout"):
            metrics = evaluate(
                scored_rows,
                weights=weights,
                split=split,
                standards=standards,
            )
            all_metrics[(candidate_id, split)] = metrics
            if split == "validation":
                validation_metrics[candidate_id] = metrics
            passed, failures = gate_result(metrics, gates)
            spread = top_bottom_diagnostic(
                scored_rows,
                weights=weights,
                split=split,
                top_fraction=float(standards["top_fraction"]),
                minimum_cross_section=int(standards["minimum_cross_section"]),
            )
            candidate_results.append(
                {
                    "candidate_id": candidate_id,
                    "split": split,
                    "snapshot_count": str(metrics["snapshot_count"]),
                    "outcome_coverage": fmt(metrics["outcome_coverage"]),
                    "mean_ic": fmt(metrics["mean_ic"]),
                    "mean_top_excess_net": fmt(
                        metrics["mean_top_excess_net"]
                    ),
                    "top_excess_hit_rate": fmt(
                        metrics["top_excess_hit_rate"]
                    ),
                    "max_drawdown": fmt(metrics["max_drawdown"]),
                    "average_turnover": fmt(metrics["average_turnover"]),
                    "mean_bottom_excess": fmt(spread["mean_bottom_return"]),
                    "mean_top_bottom_spread": fmt(
                        spread["mean_top_bottom_spread"]
                    ),
                    "positive_spread_rate": fmt(
                        spread["positive_spread_rate"]
                    ),
                    "absolute_gate_status": "PASS" if passed else "FAIL",
                    "failed_gates": ";".join(failures),
                    "evidence_posture": (
                        "contaminated_research_diagnostic_only"
                        if split == "holdout"
                        else "candidate_derivation"
                        if split == "train"
                        else "contaminated_research_tuning"
                    ),
                }
            )

    ordered = sorted(
        candidates,
        key=lambda candidate_id: (
            -finite_or_default(
                validation_metrics[candidate_id].get("mean_top_excess_net"),
                default=-999.0,
            ),
            -finite_or_default(
                validation_metrics[candidate_id].get("mean_ic"),
                default=-999.0,
            ),
            candidate_id,
        ),
    )
    passers = [
        candidate_id
        for candidate_id in ordered
        if gate_result(validation_metrics[candidate_id], gates)[0]
    ]
    selected_candidate = passers[0] if passers else (ordered[0] if ordered else "")
    selected_weights = candidates.get(selected_candidate, {})

    latest_snapshot_dir = (
        args.latest_snapshot_dir.expanduser().resolve()
        if args.latest_snapshot_dir is not None
        else resolve_path(
            standards["snapshot_history_root"],
            base_dir=base_dir,
        )
        / str(standards["development_end_date"])
    )
    latest_path = (
        latest_snapshot_dir
        / "transportation_stage11_survivorship_calibration_panel.csv"
    )
    latest_rows = read_rows(latest_path)
    latest_scored = build_directional_metric_scores(
        latest_rows,
        definitions=definitions,
        policy=policy,
    )
    latest_scored = add_positioning_research_scores(latest_scored)
    active_rows: list[dict[str, str]] = []
    for row in latest_scored:
        score = (
            weighted_score(row, selected_weights, require_complete=True)
            if selected_weights
            else None
        )
        rank_ready = str(row.get("rank_ready_flag") or "") == "1"
        active_rows.append(
            {
                "asof_date": str(row.get("asof_date") or ""),
                "ticker": str(row.get("ticker") or ""),
                "company_name": str(row.get("company_name") or ""),
                "economic_peer_group": str(
                    row.get("economic_peer_group") or ""
                ),
                "rank_ready_flag": "1" if rank_ready else "0",
                "selected_candidate_id": selected_candidate,
                "research_score": fmt(score),
                "research_score_eligible_flag": (
                    "1" if rank_ready and score is not None else "0"
                ),
                "research_score_ineligible_reason": (
                    ""
                    if rank_ready and score is not None
                    else "not_rank_ready"
                    if not rank_ready
                    else "incomplete_selected_metrics"
                ),
                "production_authorized_flag": "0",
                "production_block_reason": "revealed_holdout_contaminated",
            }
        )
    ranked = sorted(
        (row for row in active_rows if row["research_score_eligible_flag"] == "1"),
        key=lambda row: (-float(row["research_score"]), row["ticker"]),
    )
    ranks = {row["ticker"]: index + 1 for index, row in enumerate(ranked)}
    for row in active_rows:
        row["research_rank"] = str(ranks.get(row["ticker"], ""))
    active_rows.sort(
        key=lambda row: (
            int(row["research_rank"]) if row["research_rank"] else 999999,
            row["ticker"],
        )
    )
    write_csv_atomic(
        output_paths["active"],
        list(active_rows[0]) if active_rows else ["ticker"],
        active_rows,
    )
    write_csv_atomic(
        output_paths["candidates"],
        list(candidate_results[0]) if candidate_results else ["candidate_id"],
        candidate_results,
    )

    candidate_registry_payload = {
        "artifact_family": "transportation_surface_freight_candidate_registry",
        "policy_version": policy["policy_version"],
        "cohort_id": policy["cohort_id"],
        "registry_version": registry_version,
        "derivation_split": "train",
        "selected_metric_ids": [row["metric_id"] for row in selected_metrics],
        "mean_reversion_metric_ids": [
            row["metric_id"] for row in mean_reversion_metrics
        ],
        "candidates": candidates,
        "selected_on_validation_candidate_id": selected_candidate,
        "validation_passers": passers,
        "holdout_used_for_candidate_or_metric_selection": False,
        "validation_status": "contaminated_research_tuning",
        "revealed_holdout_status": "contaminated_research_diagnostic_only",
    }
    write_text_atomic(
        output_paths["registry"],
        json.dumps(candidate_registry_payload, indent=2, sort_keys=True) + "\n",
    )

    active_count = len(active_rows)
    active_score_eligible_count = sum(
        row["research_score_eligible_flag"] == "1" for row in active_rows
    )
    research_acceptance = (
        "PASS"
        if active_count >= int(policy["minimum_active_cohort_size"])
        and active_score_eligible_count >= int(policy["minimum_active_cohort_size"])
        and bool(passers)
        else "FAIL"
    )
    manifest = {
        "artifact_family": "transportation_surface_freight_score_research",
        "research_acceptance": research_acceptance,
        "promotion_eligible": False,
        "promotion_blockers": [
            "validation_reused_during_research_redesign",
            "revealed_holdout_is_contaminated",
            "new_untouched_post_freeze_outcomes_not_yet_available",
        ],
        "policy_path": str(policy_path),
        "policy_sha256": artifact_sha256(policy_path),
        "policy_version": policy["policy_version"],
        "cohort_id": policy["cohort_id"],
        "panel_path": str(panel_path),
        "panel_sha256": artifact_sha256(panel_path),
        "panel_validation_path": str(panel_validation_path),
        "panel_validation_sha256": artifact_sha256(panel_validation_path),
        "metric_registry_path": str(registry_path),
        "metric_registry_sha256": artifact_sha256(registry_path),
        "metric_registry_version": registry_version,
        "train_selected_metric_count": len(selected_metrics),
        "train_selected_metric_ids": [
            str(row["metric_id"]) for row in selected_metrics
        ],
        "train_selected_mean_reversion_metric_count": len(
            mean_reversion_metrics
        ),
        "train_selected_mean_reversion_metric_ids": [
            str(row["metric_id"]) for row in mean_reversion_metrics
        ],
        "candidate_count": len(candidates),
        "selected_candidate_id": selected_candidate,
        "validation_passing_candidate_count": len(passers),
        "validation_passing_candidate_ids": passers,
        "active_cohort_count": active_count,
        "active_score_eligible_count": active_score_eligible_count,
        "minimum_active_cohort_size": int(policy["minimum_active_cohort_size"]),
        "historical_universe_policy": policy["governance"][
            "historical_universe_policy"
        ],
        "membership_selection_uses_outcomes": False,
        "holdout_used_for_candidate_or_metric_selection": False,
        "validation_status": "contaminated_research_tuning",
        "revealed_holdout_status": "contaminated_research_diagnostic_only",
        "next_untouched_signal_start_date": "2026-07-31",
        "active_cohort_path": str(output_paths["active"]),
        "active_cohort_sha256": artifact_sha256(output_paths["active"]),
        "metric_diagnostics_path": str(output_paths["metrics"]),
        "metric_diagnostics_sha256": artifact_sha256(output_paths["metrics"]),
        "candidate_results_path": str(output_paths["candidates"]),
        "candidate_results_sha256": artifact_sha256(output_paths["candidates"]),
        "candidate_registry_path": str(output_paths["registry"]),
        "candidate_registry_sha256": artifact_sha256(output_paths["registry"]),
    }
    write_text_atomic(
        output_paths["manifest"],
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
    )
    print(json.dumps(manifest, indent=2, sort_keys=True))
    return 0 if research_acceptance == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
