#!/usr/bin/env python3
"""Run the frozen v5 candidates separately by cohort on diagnostic history."""
from __future__ import annotations

import argparse
import csv
import json
import math
import sys
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from industrials.core.oos_research import (  # noqa: E402
    evaluate_candidate,
    finite_float,
    fmt,
    summarize_candidate_period_rows,
    weighted_score,
)
from industrials.core.reports import write_csv_atomic, write_text_atomic  # noqa: E402
from industrials.transportation.contracts import file_sha256  # noqa: E402


ROOT = PROJECT_ROOT / "output" / "industrials" / "transportation"
DEFAULT_PANEL_DIR = ROOT / "investable_v5" / "outcome_panel" / "2026-08-15"
DEFAULT_VALIDATION = ROOT / "investable_v5" / "outcome_validation" / "2026-08-15" / "transportation_v5_outcome_panel_validation.json"
DEFAULT_PROTOCOL = ROOT / "investable_v5" / "research_protocol" / "2026-08-15" / "transportation_v5_research_protocol.json"
DEFAULT_OUTPUT_DIR = ROOT / "investable_v5" / "diagnostic_calibration" / "2026-08-15"
RESULT_FIELDS = (
    "cohort_id",
    "candidate_id",
    "horizon_sessions",
    "evaluation_block",
    "eligible_row_count",
    "available_outcome_row_count",
    "outcome_coverage",
    "snapshot_count",
    "mean_ic",
    "mean_top_excess_net",
    "top_excess_hit_rate",
    "mean_cohort_excess",
    "mean_top_minus_cohort_net",
    "top_minus_cohort_hit_rate",
    "mean_top_minus_bottom_gross",
    "top_minus_bottom_hit_rate",
    "non_overlapping_snapshot_count",
    "mean_non_overlapping_top_excess_net",
    "non_overlapping_top_excess_hit_rate",
    "mean_non_overlapping_top_minus_cohort_net",
    "non_overlapping_top_minus_cohort_hit_rate",
    "max_drawdown",
    "average_turnover",
    "ranking_gate",
    "investability_gate",
    "effective_sample_gate",
    "evidence_role",
    "diagnostic_gate",
)
PERIOD_FIELDS = (
    "cohort_id",
    "candidate_id",
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
    parser.add_argument("--panel-dir", type=Path, default=DEFAULT_PANEL_DIR)
    parser.add_argument("--outcome-validation", type=Path, default=DEFAULT_VALIDATION)
    parser.add_argument("--protocol", type=Path, default=DEFAULT_PROTOCOL)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    return parser.parse_args()


def read_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path}: expected JSON object")
    return payload


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def finite_sort(value: object) -> float:
    parsed = finite_float(value)
    return parsed if parsed is not None else -math.inf


def assign_blocks(
    rows: list[dict[str, str]],
    *,
    horizon: int,
    calendar_blocks: list[dict[str, object]],
) -> dict[str, str]:
    dates = sorted(
        {
            str(row["asof_date"])
            for row in rows
            if str(row.get("horizon_sessions") or "") == str(horizon)
            and str(row.get("calibration_eligible_flag") or "") == "1"
            and str(row.get("outcome_available_flag") or "") == "1"
        }
    )
    result: dict[str, str] = {}
    for asof in dates:
        matches = [
            str(block["block_id"])
            for block in calendar_blocks
            if str(block["start_date"]) <= asof <= str(block["end_date"])
        ]
        if len(matches) != 1:
            raise ValueError(
                f"{asof}: expected exactly one fixed calendar block; got {matches}"
            )
        result[asof] = matches[0]
    return result


def candidate_block_counts(
    rows: list[dict[str, str]],
    *,
    weights: dict[str, float],
    horizon: int,
    block: str,
    block_map: dict[str, str],
    require_complete: bool,
) -> tuple[int, int]:
    eligible = 0
    available = 0
    for row in rows:
        if str(row.get("horizon_sessions") or "") != str(horizon):
            continue
        if block_map.get(str(row.get("asof_date") or "")) != block:
            continue
        if str(row.get("calibration_eligible_flag") or "") != "1":
            continue
        eligible += 1
        if (
            finite_float(row.get("forward_excess_return")) is not None
            and weighted_score(
                row, weights, require_complete=require_complete
            )
            is not None
        ):
            available += 1
    return eligible, available


def metric_row(
    *,
    cohort: str,
    candidate: str,
    horizon: int,
    block: str,
    metrics: dict[str, object],
    minimum_non_overlapping_snapshots: int,
) -> dict[str, str]:
    mean_ic = finite_float(metrics.get("mean_ic"))
    net = finite_float(metrics.get("mean_top_excess_net"))
    hit = finite_float(metrics.get("top_excess_hit_rate"))
    rank_vs_cohort = finite_float(metrics.get("mean_top_minus_cohort_net"))
    rank_top_bottom = finite_float(metrics.get("mean_top_minus_bottom_gross"))
    coverage = finite_float(metrics.get("outcome_coverage")) or 0.0
    descriptive = block == "diagnostic_all"
    ranking_gate = bool(
        coverage >= 0.80
        and mean_ic is not None
        and mean_ic >= 0.0
        and rank_vs_cohort is not None
        and rank_vs_cohort > 0.0
        and rank_top_bottom is not None
        and rank_top_bottom > 0.0
    )
    investability_gate = bool(
        net is not None
        and net > 0.0
        and hit is not None
        and hit >= 0.50
    )
    effective_sample_gate = (
        int(metrics.get("non_overlapping_snapshot_count") or 0)
        >= minimum_non_overlapping_snapshots
    )
    gate = (
        not descriptive
        and ranking_gate
        and investability_gate
        and effective_sample_gate
    )
    return {
        "cohort_id": cohort,
        "candidate_id": candidate,
        "horizon_sessions": str(horizon),
        "evaluation_block": block,
        "eligible_row_count": str(metrics.get("eligible_row_count") or 0),
        "available_outcome_row_count": str(metrics.get("available_outcome_row_count") or 0),
        "outcome_coverage": fmt(metrics.get("outcome_coverage")),
        "snapshot_count": str(metrics.get("snapshot_count") or 0),
        "mean_ic": fmt(metrics.get("mean_ic")),
        "mean_top_excess_net": fmt(metrics.get("mean_top_excess_net")),
        "top_excess_hit_rate": fmt(metrics.get("top_excess_hit_rate")),
        "mean_cohort_excess": fmt(metrics.get("mean_cohort_excess")),
        "mean_top_minus_cohort_net": fmt(metrics.get("mean_top_minus_cohort_net")),
        "top_minus_cohort_hit_rate": fmt(metrics.get("top_minus_cohort_hit_rate")),
        "mean_top_minus_bottom_gross": fmt(metrics.get("mean_top_minus_bottom_gross")),
        "top_minus_bottom_hit_rate": fmt(metrics.get("top_minus_bottom_hit_rate")),
        "non_overlapping_snapshot_count": str(metrics.get("non_overlapping_snapshot_count") or 0),
        "mean_non_overlapping_top_excess_net": fmt(metrics.get("mean_non_overlapping_top_excess_net")),
        "non_overlapping_top_excess_hit_rate": fmt(metrics.get("non_overlapping_top_excess_hit_rate")),
        "mean_non_overlapping_top_minus_cohort_net": fmt(metrics.get("mean_non_overlapping_top_minus_cohort_net")),
        "non_overlapping_top_minus_cohort_hit_rate": fmt(metrics.get("non_overlapping_top_minus_cohort_hit_rate")),
        "max_drawdown": fmt(metrics.get("max_drawdown")),
        "average_turnover": fmt(metrics.get("average_turnover")),
        "ranking_gate": "PASS" if ranking_gate else "FAIL",
        "investability_gate": "PASS" if investability_gate else "FAIL",
        "effective_sample_gate": "PASS" if effective_sample_gate else "FAIL",
        "evidence_role": "DESCRIPTIVE_ONLY" if descriptive else "STABILITY_DIAGNOSTIC",
        "diagnostic_gate": (
            "DESCRIPTIVE_ONLY" if descriptive else "PASS" if gate else "FAIL"
        ),
    }


def main() -> int:
    args = parse_args()
    panel_dir = args.panel_dir.expanduser().resolve()
    validation_path = args.outcome_validation.expanduser().resolve()
    protocol_path = args.protocol.expanduser().resolve()
    output_dir = args.output_dir.expanduser().resolve()
    panel_path = panel_dir / "transportation_v5_outcome_panel.csv"
    panel_manifest_path = panel_dir / "transportation_v5_outcome_panel_manifest.json"
    validation = read_json(validation_path)
    protocol = read_json(protocol_path)
    manifest = read_json(panel_manifest_path)
    if validation.get("acceptance") != "PASS" or not validation.get(
        "historical_diagnostic_calibration_authorized"
    ):
        raise ValueError("outcome panel has not passed independent reconciliation")
    if manifest.get("panel_sha256") != file_sha256(panel_path):
        raise ValueError("outcome panel hash mismatch")
    if protocol.get("acceptance") != "PASS":
        raise ValueError("research protocol is not PASS")
    rows = read_csv(panel_path)
    evaluation = dict(protocol["evaluation"])
    horizons = [int(item) for item in evaluation["horizons_sessions"]]
    primary_horizon = int(evaluation["primary_horizon_sessions"])
    top_fraction = float(evaluation["selection_fraction"])
    transaction_cost_bps = float(evaluation["transaction_cost_bps"])
    require_complete = bool(evaluation["require_complete_components"])
    calendar_blocks = [
        dict(item) for item in list(evaluation.get("calendar_blocks") or [])
    ]
    if not calendar_blocks:
        raise ValueError("research protocol is missing fixed calendar blocks")
    block_ids = [str(item["block_id"]) for item in calendar_blocks]
    minimum_non_overlapping = int(
        evaluation.get("minimum_non_overlapping_snapshots_per_block") or 0
    )
    if minimum_non_overlapping <= 0:
        raise ValueError("minimum non-overlapping block sample must be positive")
    result_rows: list[dict[str, str]] = []
    period_rows: list[dict[str, str]] = []
    cohort_summaries: dict[str, Any] = {}
    for cohort, registry in dict(protocol["candidate_registries"]).items():
        cohort_rows = [
            dict(row) for row in rows if str(row["calibration_cohort"]) == cohort
        ]
        minimum_cross_section = int(registry["minimum_cross_section"])
        candidates = dict(registry["candidates"])
        blocks_by_horizon = {
            horizon: assign_blocks(
                cohort_rows,
                horizon=horizon,
                calendar_blocks=calendar_blocks,
            )
            for horizon in horizons
        }
        candidate_primary: dict[str, dict[str, object]] = {}
        block_gates: dict[str, list[str]] = {}
        for candidate_id, weights in candidates.items():
            candidate_weights = {
                str(field): float(weight) for field, weight in dict(weights).items()
            }
            block_gates[candidate_id] = []
            for horizon in horizons:
                all_rows = [dict(row, split="diagnostic_all") for row in cohort_rows]
                metrics = evaluate_candidate(
                    all_rows,
                    weights=candidate_weights,
                    split="diagnostic_all",
                    horizon_sessions=horizon,
                    top_fraction=top_fraction,
                    minimum_cross_section=minimum_cross_section,
                    transaction_cost_bps=transaction_cost_bps,
                    require_complete_components=require_complete,
                )
                full_row = metric_row(
                    cohort=cohort,
                    candidate=candidate_id,
                    horizon=horizon,
                    block="diagnostic_all",
                    metrics=metrics,
                    minimum_non_overlapping_snapshots=minimum_non_overlapping,
                )
                result_rows.append(full_row)
                if horizon == primary_horizon:
                    candidate_primary[candidate_id] = metrics
                for period in list(metrics.get("period_rows") or []):
                    block = blocks_by_horizon[horizon].get(
                        str(period.get("asof_date") or "")
                    )
                    if block is None:
                        raise ValueError(
                            f"{cohort}/{horizon}: period outside fixed calendar blocks"
                        )
                    period_rows.append(
                        {
                            "cohort_id": cohort,
                            "candidate_id": candidate_id,
                            "horizon_sessions": str(horizon),
                            "evaluation_block": block,
                            **{
                                field: (
                                    fmt(period.get(field))
                                    if field
                                    in {
                                        "ic",
                                        "turnover",
                                        "gross_excess",
                                        "net_excess",
                                        "cohort_excess",
                                        "bottom_excess",
                                        "top_minus_cohort_gross",
                                        "top_minus_cohort_net",
                                        "top_minus_bottom_gross",
                                    }
                                    else str(period.get(field) or "")
                                )
                                for field in PERIOD_FIELDS[4:]
                            },
                        }
                    )
                block_map = blocks_by_horizon[horizon]
                for block in block_ids:
                    eligible_count, available_count = candidate_block_counts(
                        cohort_rows,
                        weights=candidate_weights,
                        horizon=horizon,
                        block=block,
                        block_map=block_map,
                        require_complete=require_complete,
                    )
                    block_periods = [
                        dict(period)
                        for period in list(metrics.get("period_rows") or [])
                        if block_map.get(str(period.get("asof_date") or ""))
                        == block
                    ]
                    block_metrics = summarize_candidate_period_rows(
                        block_periods,
                        eligible_row_count=eligible_count,
                        available_outcome_row_count=available_count,
                    )
                    block_row = metric_row(
                        cohort=cohort,
                        candidate=candidate_id,
                        horizon=horizon,
                        block=block,
                        metrics=block_metrics,
                        minimum_non_overlapping_snapshots=minimum_non_overlapping,
                    )
                    result_rows.append(block_row)
                    if horizon == primary_horizon:
                        block_gates[candidate_id].append(block_row["diagnostic_gate"])
        ordered = sorted(
            candidates,
            key=lambda candidate_id: (
                -int(
                    finite_sort(candidate_primary[candidate_id].get("mean_ic")) >= 0
                    and finite_sort(
                        candidate_primary[candidate_id].get(
                            "mean_top_minus_cohort_net"
                        )
                    )
                    > 0
                    and finite_sort(
                        candidate_primary[candidate_id].get(
                            "mean_top_minus_bottom_gross"
                        )
                    )
                    > 0
                ),
                -finite_sort(candidate_primary[candidate_id].get("mean_ic")),
                -finite_sort(
                    candidate_primary[candidate_id].get(
                        "mean_top_minus_cohort_net"
                    )
                ),
                -finite_sort(
                    candidate_primary[candidate_id].get(
                        "mean_top_minus_bottom_gross"
                    )
                ),
                -finite_sort(candidate_primary[candidate_id].get("mean_top_excess_net")),
                candidate_id,
            ),
        )
        selected = ordered[0] if ordered else ""
        full = candidate_primary.get(selected, {})
        selected_block_gates = block_gates.get(selected, [])
        stable = (
            len(selected_block_gates) == len(block_ids)
            and all(value == "PASS" for value in selected_block_gates)
        )
        cohort_summaries[cohort] = {
            "diagnostic_selected_candidate": selected,
            "primary_horizon_sessions": primary_horizon,
            "mean_ic": full.get("mean_ic"),
            "mean_top_excess_net": full.get("mean_top_excess_net"),
            "top_excess_hit_rate": full.get("top_excess_hit_rate"),
            "mean_top_minus_cohort_net": full.get("mean_top_minus_cohort_net"),
            "mean_top_minus_bottom_gross": full.get("mean_top_minus_bottom_gross"),
            "all_fixed_calendar_blocks_pass": stable,
            "diagnostic_conclusion": (
                "POSITIVE_RESEARCH_SIGNAL_FUTURE_PROOF_REQUIRED"
                if stable
                else "NO_STABLE_DIAGNOSTIC_RANKING_POWER"
            ),
        }
    output_dir.mkdir(parents=True, exist_ok=True)
    result_csv = output_dir / "transportation_v5_candidate_results.csv"
    period_csv = output_dir / "transportation_v5_candidate_period_results.csv"
    output_path = output_dir / "transportation_v5_diagnostic_calibration.json"
    write_csv_atomic(result_csv, RESULT_FIELDS, result_rows)
    write_csv_atomic(period_csv, PERIOD_FIELDS, period_rows)
    result = {
        "acceptance": "PASS",
        "contract_version": "transportation_v5_diagnostic_calibration_v2",
        "cohort_summaries": cohort_summaries,
        "historical_evidence_class": "diagnostic_only",
        "historical_results_can_authorize_production": False,
        "aggregate_history_role": "descriptive_only",
        "calendar_blocks": calendar_blocks,
        "minimum_non_overlapping_snapshots_per_block": minimum_non_overlapping,
        "future_proof_cutoff_date": "2026-07-30",
        "production_activation_authorized": False,
        "artifacts": {
            "outcome_panel": {"path": str(panel_path), "sha256": file_sha256(panel_path)},
            "outcome_validation": {"path": str(validation_path), "sha256": file_sha256(validation_path)},
            "research_protocol": {"path": str(protocol_path), "sha256": file_sha256(protocol_path)},
            "candidate_results": {"path": str(result_csv), "sha256": file_sha256(result_csv)},
            "period_results": {"path": str(period_csv), "sha256": file_sha256(period_csv)},
        },
        "next_gate": "BEGIN_FUTURE_ONLY_MONTHLY_SHADOW_EVIDENCE_AFTER_2026_07_30",
    }
    write_text_atomic(output_path, json.dumps(result, indent=2, sort_keys=True) + "\n")
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
