#!/usr/bin/env python3
"""Evaluate v8 comparison groups before any fixed-weight cohort aggregation."""
from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Mapping


PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from industrials.core.oos_research import (  # noqa: E402
    evaluate_candidate,
    finite_float,
    summarize_candidate_period_rows,
)
from industrials.core.reports import write_csv_atomic, write_text_atomic  # noqa: E402
from industrials.transportation.contracts import file_sha256  # noqa: E402
from industrials.transportation.subgroup_scoring import load_subgroup_score_policy  # noqa: E402


ROOT = PROJECT_ROOT / "output" / "industrials" / "transportation"
DEFAULT_POLICY = (
    PROJECT_ROOT
    / "industrials"
    / "transportation"
    / "data"
    / "transportation_subgroup_score_policy_v8.yaml"
)
DEFAULT_SCORE_MANIFEST = (
    ROOT
    / "investable_v5"
    / "subgroup_scores_v8"
    / "2026-08-21"
    / "transportation_v8_subgroup_score_history.json"
)
DEFAULT_PANEL = (
    ROOT
    / "investable_v5"
    / "outcome_panel_v6"
    / "2026-08-16"
    / "transportation_v5_outcome_panel.csv"
)
DEFAULT_PANEL_MANIFEST = DEFAULT_PANEL.parent / "transportation_v5_outcome_panel_manifest.json"
DEFAULT_OUTPUT_ROOT = ROOT / "investable_v5" / "subgroup_calibration_v8"

RESULT_FIELDS = (
    "cohort_id",
    "group_id",
    "ranking_mode",
    "horizon_sessions",
    "evaluation_block",
    "eligible_row_count",
    "available_outcome_row_count",
    "outcome_coverage",
    "snapshot_count",
    "mean_ic",
    "mean_top_excess_net",
    "top_excess_hit_rate",
    "mean_top_minus_cohort_net",
    "mean_top_minus_bottom_gross",
    "non_overlapping_snapshot_count",
    "ranking_gate",
    "investability_gate",
    "effective_sample_gate",
    "group_gate",
    "evidence_role",
)
PERIOD_FIELDS = (
    "cohort_id",
    "group_id",
    "horizon_sessions",
    "evaluation_block",
    "asof_date",
    "exit_date",
    "cross_section",
    "selected",
    "ic",
    "turnover",
    "gross_excess",
    "net_excess",
    "cohort_excess",
    "bottom_excess",
    "top_minus_cohort_gross",
    "top_minus_cohort_net",
    "top_minus_bottom_gross",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--asof", default="2026-08-21")
    parser.add_argument("--policy", type=Path, default=DEFAULT_POLICY)
    parser.add_argument("--score-manifest", type=Path, default=DEFAULT_SCORE_MANIFEST)
    parser.add_argument("--panel", type=Path, default=DEFAULT_PANEL)
    parser.add_argument("--panel-manifest", type=Path, default=DEFAULT_PANEL_MANIFEST)
    parser.add_argument("--output-dir", type=Path, default=None)
    return parser.parse_args()


def read_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path}: expected JSON object")
    return payload


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def fmt(value: object) -> str:
    parsed = finite_float(value)
    return "" if parsed is None else (f"{parsed:.12f}".rstrip("0").rstrip(".") or "0")


def block_id(asof: str, blocks: list[dict[str, object]]) -> str:
    matches = [
        str(item["block_id"])
        for item in blocks
        if str(item["start_date"]) <= asof <= str(item["end_date"])
    ]
    if len(matches) != 1:
        raise ValueError(f"{asof}: expected exactly one fixed diagnostic block")
    return matches[0]


def gate_row(
    *,
    cohort_id: str,
    group_id: str,
    ranking_mode: str,
    horizon: int,
    block: str,
    metrics: Mapping[str, object],
    gates: Mapping[str, object],
) -> dict[str, str]:
    coverage = finite_float(metrics.get("outcome_coverage")) or 0.0
    ic = finite_float(metrics.get("mean_ic"))
    spread = finite_float(metrics.get("mean_top_minus_cohort_net"))
    top_bottom = finite_float(metrics.get("mean_top_minus_bottom_gross"))
    net = finite_float(metrics.get("mean_top_excess_net"))
    hit = finite_float(metrics.get("top_excess_hit_rate"))
    descriptive = block == "diagnostic_all"
    ranking_gate = bool(
        coverage >= 0.80
        and ic is not None
        and ic > float(gates["minimum_ic"])
        and spread is not None
        and spread > float(gates["minimum_top_minus_group_net"])
        and top_bottom is not None
        and top_bottom > float(gates["minimum_top_minus_bottom_gross"])
    )
    investability_gate = bool(
        net is not None
        and net > 0.0
        and hit is not None
        and hit >= float(gates["minimum_hit_rate"])
    )
    sample_gate = int(metrics.get("non_overlapping_snapshot_count") or 0) >= int(
        gates["minimum_non_overlapping_snapshots_per_block"]
    )
    passed = not descriptive and ranking_gate and investability_gate and sample_gate
    return {
        "cohort_id": cohort_id,
        "group_id": group_id,
        "ranking_mode": ranking_mode,
        "horizon_sessions": str(horizon),
        "evaluation_block": block,
        "eligible_row_count": str(metrics.get("eligible_row_count") or 0),
        "available_outcome_row_count": str(metrics.get("available_outcome_row_count") or 0),
        "outcome_coverage": fmt(metrics.get("outcome_coverage")),
        "snapshot_count": str(metrics.get("snapshot_count") or 0),
        "mean_ic": fmt(metrics.get("mean_ic")),
        "mean_top_excess_net": fmt(metrics.get("mean_top_excess_net")),
        "top_excess_hit_rate": fmt(metrics.get("top_excess_hit_rate")),
        "mean_top_minus_cohort_net": fmt(metrics.get("mean_top_minus_cohort_net")),
        "mean_top_minus_bottom_gross": fmt(metrics.get("mean_top_minus_bottom_gross")),
        "non_overlapping_snapshot_count": str(
            metrics.get("non_overlapping_snapshot_count") or 0
        ),
        "ranking_gate": "PASS" if ranking_gate else "FAIL",
        "investability_gate": "PASS" if investability_gate else "FAIL",
        "effective_sample_gate": "PASS" if sample_gate else "FAIL",
        "group_gate": "DESCRIPTIVE_ONLY" if descriptive else "PASS" if passed else "FAIL",
        "evidence_role": "REVEALED_DIAGNOSTIC_ONLY",
    }


def main() -> int:
    args = parse_args()
    paths = {
        "policy": args.policy.expanduser().resolve(),
        "score_manifest": args.score_manifest.expanduser().resolve(),
        "panel": args.panel.expanduser().resolve(),
        "panel_manifest": args.panel_manifest.expanduser().resolve(),
    }
    missing = [str(path) for path in paths.values() if not path.is_file()]
    if missing:
        raise FileNotFoundError(f"missing v8 calibration inputs={missing}")
    policy = load_subgroup_score_policy(paths["policy"])
    score_manifest = read_json(paths["score_manifest"])
    if score_manifest.get("acceptance") != "PASS":
        raise ValueError("v8 score regeneration is not PASS")
    if score_manifest.get("historical_score_regeneration_count") != 1:
        raise ValueError("v8 score history was not regenerated exactly once")
    if score_manifest.get("post_semantic_parser_invocations") != 0:
        raise ValueError("calibration cannot follow a second parser pass")
    if str(score_manifest["lineage"]["policy"]["sha256"]) != file_sha256(paths["policy"]):
        raise ValueError("v8 policy changed after score regeneration")
    score_path = Path(str(score_manifest["artifacts"]["score_history"]["path"]))
    if file_sha256(score_path) != str(
        score_manifest["artifacts"]["score_history"]["sha256"]
    ):
        raise ValueError("v8 score-history hash mismatch")
    panel_manifest = read_json(paths["panel_manifest"])
    if str(panel_manifest.get("panel_sha256") or "") != file_sha256(paths["panel"]):
        raise ValueError("immutable outcome panel hash mismatch")
    if panel_manifest.get("historical_results_can_authorize_production") is not False:
        raise ValueError("revealed panel cannot authorize production")

    scores = {
        (row["asof_date"], row["ticker"]): row for row in read_csv(score_path)
    }
    joined: list[dict[str, object]] = []
    for panel_row in read_csv(paths["panel"]):
        score = scores.get((panel_row["asof_date"], panel_row["ticker"]))
        if score is None:
            continue
        joined.append(
            {
                **panel_row,
                "v8_cohort_id": score["v8_cohort_id"],
                "v8_group_id": score["v8_group_id"],
                "ranking_mode": score["ranking_mode"],
                "v8_score": score["v8_group_percentile_score"],
                "calibration_eligible_flag": score["v8_calibration_eligible_flag"],
            }
        )

    gates = policy["calibration_gates"]
    blocks = [dict(item) for item in gates["fixed_calendar_blocks"]]
    block_ids = [str(item["block_id"]) for item in blocks]
    horizons = [int(item) for item in gates["horizons_sessions"]]
    result_rows: list[dict[str, str]] = []
    period_rows: list[dict[str, str]] = []
    group_summary: dict[str, Any] = {}
    cohort_authorization = {
        str(key): bool(value)
        for key, value in score_manifest[
            "cohort_diagnostic_calibration_authorized"
        ].items()
    }

    for cohort_id, cohort in policy["cohorts"].items():
        cohort_ready = cohort_authorization.get(str(cohort_id), False)
        for group_id, group in cohort["groups"].items():
            key = f"{cohort_id}::{group_id}"
            ranking_mode = str(group["ranking_mode"])
            if ranking_mode != "ranked":
                group_summary[key] = {
                    "status": "NON_RANKED_FIXED_WEIGHT_SLEEVE",
                    "all_fixed_blocks_pass": True,
                }
                continue
            if not cohort_ready:
                group_summary[key] = {
                    "status": "BLOCKED_REQUIRED_SPECIALIZED_PACK_COVERAGE",
                    "all_fixed_blocks_pass": False,
                }
                continue
            group_rows = [
                dict(row)
                for row in joined
                if row["v8_cohort_id"] == cohort_id and row["v8_group_id"] == group_id
            ]
            group_block_gates: list[str] = []
            for horizon in horizons:
                all_rows = [dict(row, split="diagnostic_all") for row in group_rows]
                metrics = evaluate_candidate(
                    all_rows,
                    weights={"v8_score": 1.0},
                    split="diagnostic_all",
                    horizon_sessions=horizon,
                    top_fraction=float(policy["aggregation"]["selection_fraction"]),
                    minimum_cross_section=int(group["minimum_cross_section"]),
                    transaction_cost_bps=float(gates["transaction_cost_bps"]),
                    require_complete_components=True,
                )
                result_rows.append(
                    gate_row(
                        cohort_id=str(cohort_id),
                        group_id=str(group_id),
                        ranking_mode=ranking_mode,
                        horizon=horizon,
                        block="diagnostic_all",
                        metrics=metrics,
                        gates=gates,
                    )
                )
                for period in metrics.get("period_rows") or []:
                    period_block = block_id(str(period["asof_date"]), blocks)
                    period_rows.append(
                        {
                            "cohort_id": str(cohort_id),
                            "group_id": str(group_id),
                            "horizon_sessions": str(horizon),
                            "evaluation_block": period_block,
                            **{
                                field: fmt(period.get(field))
                                if field not in {"asof_date", "exit_date", "cross_section", "selected"}
                                else str(period.get(field) or "")
                                for field in PERIOD_FIELDS[4:]
                            },
                        }
                    )
                for block in block_ids:
                    block_rows = [
                        dict(row, split=block)
                        for row in group_rows
                        if block_id(str(row["asof_date"]), blocks) == block
                    ]
                    block_metrics = evaluate_candidate(
                        block_rows,
                        weights={"v8_score": 1.0},
                        split=block,
                        horizon_sessions=horizon,
                        top_fraction=float(policy["aggregation"]["selection_fraction"]),
                        minimum_cross_section=int(group["minimum_cross_section"]),
                        transaction_cost_bps=float(gates["transaction_cost_bps"]),
                        require_complete_components=True,
                    )
                    gated = gate_row(
                        cohort_id=str(cohort_id),
                        group_id=str(group_id),
                        ranking_mode=ranking_mode,
                        horizon=horizon,
                        block=block,
                        metrics=block_metrics,
                        gates=gates,
                    )
                    result_rows.append(gated)
                    if horizon == int(gates["primary_horizon_sessions"]):
                        group_block_gates.append(gated["group_gate"])
            group_summary[key] = {
                "status": "CALIBRATED_REVEALED_DIAGNOSTIC",
                "all_fixed_blocks_pass": bool(group_block_gates)
                and all(value == "PASS" for value in group_block_gates),
                "primary_block_gates": group_block_gates,
            }

    cohort_results: dict[str, Any] = {}
    for cohort_id, cohort in policy["cohorts"].items():
        ranked_keys = [
            f"{cohort_id}::{group_id}"
            for group_id, group in cohort["groups"].items()
            if str(group["ranking_mode"]) == "ranked"
        ]
        group_pass = bool(ranked_keys) and all(
            bool(group_summary[key]["all_fixed_blocks_pass"]) for key in ranked_keys
        )
        cohort_results[str(cohort_id)] = {
            "cohort_diagnostic_calibration_authorized": cohort_authorization.get(
                str(cohort_id), False
            ),
            "ranked_group_count": len(ranked_keys),
            "all_ranked_groups_pass": group_pass,
            "aggregate_calibration_run": False,
            "aggregate_calibration_reason": (
                "AUTHORIZED_BUT_NOT_PRODUCTION_ELIGIBLE"
                if group_pass
                else "NOT_RUN_GROUP_GATES_FAILED_OR_COHORT_NOT_AUTHORIZED"
            ),
        }

    output_dir = (
        args.output_dir.expanduser().resolve()
        if args.output_dir
        else DEFAULT_OUTPUT_ROOT / args.asof
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    result_csv = output_dir / "transportation_v8_group_calibration_results.csv"
    period_csv = output_dir / "transportation_v8_group_period_results.csv"
    output_path = output_dir / "transportation_v8_subgroup_calibration.json"
    write_csv_atomic(result_csv, RESULT_FIELDS, result_rows)
    write_csv_atomic(period_csv, PERIOD_FIELDS, period_rows)
    payload = {
        "acceptance": "PASS",
        "contract_version": "transportation_v8_subgroup_diagnostic_calibration_v1",
        "asof_date": args.asof,
        "decision": "NO_PRODUCTION_PROMOTION_FROM_REVEALED_V8_HISTORY",
        "group_results": group_summary,
        "cohort_results": cohort_results,
        "lineage": {
            name: {"path": str(path), "sha256": file_sha256(path)}
            for name, path in paths.items()
        },
        "artifacts": {
            "group_results": {"path": str(result_csv), "sha256": file_sha256(result_csv)},
            "period_results": {"path": str(period_csv), "sha256": file_sha256(period_csv)},
        },
        "historical_score_regeneration_count": 1,
        "historical_calibration_execution_count": 1,
        "network_requests": 0,
        "parser_invocations": 0,
        "historical_results_can_authorize_production": False,
        "production_activation_authorized": False,
        "next_gate": "FREEZE_V8_DIAGNOSTIC_AND_BEGIN_FUTURE_ONLY_GROUP_MONITORING",
    }
    write_text_atomic(output_path, json.dumps(payload, indent=2, sort_keys=True) + "\n")
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

