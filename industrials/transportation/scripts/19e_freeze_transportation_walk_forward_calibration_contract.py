#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from industrials.core.reports import (  # noqa: E402
    write_csv_atomic,
)
from industrials.transportation.calibration_contract import (  # noqa: E402
    CALIBRATION_CONTRACT_VERSION,
    FLAG_METRICS,
    flag_exception_decision,
    purged_split_calendar,
    summarize_flag_history,
)
from industrials.transportation.selected_feature_history import (  # noqa: E402
    iter_gzip_csv,
    read_csv,
    read_json,
    sha256,
    verify_artifact,
    write_manifest,
)
from industrials.transportation.scripts._shared import (  # noqa: E402
    DEFAULT_CONFIG,
)


EXPECTED_CANDIDATES = (
    "fleet_utilization",
    "operating_ratio",
    "passenger_load_factor",
)
FLAG_AUDIT_FIELDS = (
    "metric_id",
    "current_metric_disposition",
    "accepted_breadth_gate_pass",
    "evidence_precision_gate_pass",
    "value_row_count",
    "value_ticker_count",
    "median_distinct_period_count",
    "observed_binary_values",
    "flag_exception_authorized",
    "decision_reason",
)
CALENDAR_FIELDS = ("asof_date", "split_name")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Freeze the transportation walk-forward calibration contract "
            "and flag-depth decision against the hash-frozen v3 panel. "
            "Calibration is not executed."
        )
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path(
            "output/industrials/transportation/historical_features/"
            "v3_conflict_resolved"
        ),
    )
    parser.add_argument("--forward-trading-days", type=int, default=63)
    parser.add_argument("--embargo-days", type=int, default=21)
    parser.add_argument("--transaction-cost-bps", type=float, default=20.0)
    parser.add_argument(
        "--transaction-cost-stress-bps",
        type=float,
        default=40.0,
    )
    return parser.parse_args()


def _artifact(path: Path, *, row_count: int | None = None) -> dict[str, Any]:
    output: dict[str, Any] = {
        "path": str(path.resolve()),
        "sha256": sha256(path),
    }
    if row_count is not None:
        output["row_count"] = row_count
    return output


def main() -> int:
    args = parse_args()
    output_dir = args.output_dir.expanduser().resolve()
    if args.forward_trading_days <= 0:
        raise ValueError("--forward-trading-days must be positive")
    if args.embargo_days < 0:
        raise ValueError("--embargo-days cannot be negative")
    if (
        args.transaction_cost_bps < 0
        or args.transaction_cost_stress_bps
        < args.transaction_cost_bps
    ):
        raise ValueError("transaction-cost stress must be at least base cost")

    preflight_path = (
        output_dir / "transportation_dp8_historical_impact_preflight.json"
    )
    panel_manifest_path = (
        output_dir / "transportation_v3_panel_manifest.json"
    )
    validation_path = (
        output_dir / "transportation_v3_panel_validation.json"
    )
    subset_path = (
        output_dir / "transportation_v3_calibration_subset_manifest.json"
    )
    coverage_path = (
        output_dir / "transportation_v3_historical_coverage.csv"
    )
    specialized_path = (
        output_dir / "transportation_v3_specialized_discovery_panel.csv.gz"
    )
    for path in (
        preflight_path,
        panel_manifest_path,
        validation_path,
        subset_path,
        coverage_path,
        specialized_path,
    ):
        if not path.is_file():
            raise FileNotFoundError(path)

    preflight = read_json(preflight_path)
    panel_manifest = read_json(panel_manifest_path)
    validation = read_json(validation_path)
    subset = read_json(subset_path)
    errors: list[str] = []
    if (
        preflight.get("acceptance") != "PASS"
        or panel_manifest.get("acceptance") != "PASS"
        or panel_manifest.get("panel_status") != "HASH_FROZEN"
        or validation.get("acceptance") != "PASS"
        or validation.get("panel_status") != "FROZEN"
        or subset.get("acceptance") != "PASS"
        or subset.get("calibration_executed") is not False
    ):
        errors.append("DP8, DP9, G8, and subset freezes must all pass")
    for label, reference in (
        panel_manifest.get("artifacts") or {}
    ).items():
        verify_artifact(reference, label=f"panel {label}")

    candidates = tuple(
        sorted(str(value) for value in subset.get("selected_metric_ids", []))
    )
    if candidates != tuple(sorted(EXPECTED_CANDIDATES)):
        errors.append(
            f"candidate set changed actual={candidates} "
            f"expected={tuple(sorted(EXPECTED_CANDIDATES))}"
        )
    complete_reference = (
        panel_manifest.get("artifacts") or {}
    ).get("complete_panel") or {}
    panel_hash = sha256(
        Path(str(complete_reference.get("path") or "")).resolve()
    )
    if (
        panel_hash
        != str(subset.get("calibration_input_panel_sha256") or "")
        or panel_hash
        != str(validation.get("calibration_input_panel_sha256") or "")
    ):
        errors.append("calibration input hash is not the validated panel hash")

    coverage_rows = read_csv(coverage_path)
    coverage_by_metric = {
        row["metric_id"]: row for row in coverage_rows
    }
    for metric_id in EXPECTED_CANDIDATES:
        row = coverage_by_metric.get(metric_id)
        if (
            row is None
            or row["calibration_candidate"] != "1"
            or int(row["value_ticker_count"] or 0) < 3
            or int(row["value_membership_rows"] or 0) < 1
        ):
            errors.append(
                f"{metric_id}: frozen historical candidate coverage failed"
            )

    freeze_path = Path(
        str(
            (
                preflight.get("inputs", {}).get(
                    "final_freeze_manifest", {}
                )
            ).get("path")
            or ""
        )
    ).resolve()
    v2_build_path = Path(
        str(
            (
                preflight.get("inputs", {}).get(
                    "v2_build_manifest", {}
                )
            ).get("path")
            or ""
        )
    ).resolve()
    final_freeze = read_json(freeze_path)
    v2_build = read_json(v2_build_path)
    disposition_path = Path(
        str(
            (
                final_freeze.get("artifacts", {}).get(
                    "final_metric_dispositions", {}
                )
            ).get("path")
            or ""
        )
    ).resolve()
    dispositions = {
        row["metric_id"]: row for row in read_csv(disposition_path)
    }

    flag_history = summarize_flag_history(
        iter_gzip_csv(specialized_path)
    )
    flag_rows: list[dict[str, object]] = []
    flag_exception_count = 0
    for metric_id in FLAG_METRICS:
        disposition = dispositions.get(metric_id)
        history = flag_history[metric_id]
        if disposition is None:
            errors.append(f"missing flag disposition={metric_id}")
            continue
        authorized, reason = flag_exception_decision(
            disposition,
            history,
        )
        flag_exception_count += int(authorized)
        flag_rows.append(
            {
                "metric_id": metric_id,
                "current_metric_disposition": disposition[
                    "metric_disposition"
                ],
                "accepted_breadth_gate_pass": disposition[
                    "accepted_breadth_gate_pass"
                ],
                "evidence_precision_gate_pass": disposition[
                    "evidence_precision_gate_pass"
                ],
                "value_row_count": history.value_row_count,
                "value_ticker_count": history.ticker_count,
                "median_distinct_period_count": (
                    history.median_period_count
                ),
                "observed_binary_values": "|".join(
                    f"{value:g}" for value in history.observed_values
                ),
                "flag_exception_authorized": int(authorized),
                "decision_reason": reason,
            }
        )
    if flag_exception_count:
        errors.append(
            "flag-specific exception unexpectedly changes the frozen subset"
        )

    snapshot_dates = [
        str(value) for value in v2_build.get("completed_dates", [])
    ]
    if (
        len(snapshot_dates) != 92
        or snapshot_dates != sorted(set(snapshot_dates))
    ):
        errors.append("frozen 92-date evaluation calendar changed")
    split_map = purged_split_calendar(
        snapshot_dates,
        forward_trading_days=args.forward_trading_days,
        embargo_days=args.embargo_days,
    )
    calendar_rows = [
        {"asof_date": value, "split_name": split_map[value]}
        for value in snapshot_dates
    ]
    split_counts = Counter(split_map.values())
    for split_name in ("train", "validation", "holdout", "embargo"):
        if split_counts.get(split_name, 0) == 0:
            errors.append(f"calibration split is empty={split_name}")

    flag_path = (
        output_dir / "transportation_v3_flag_exception_audit.csv"
    )
    calendar_path = (
        output_dir
        / "transportation_walk_forward_evaluation_calendar.csv"
    )
    contract_path = (
        output_dir
        / "transportation_walk_forward_calibration_contract.json"
    )
    write_csv_atomic(flag_path, FLAG_AUDIT_FIELDS, flag_rows)
    write_csv_atomic(calendar_path, CALENDAR_FIELDS, calendar_rows)

    acceptance = "PASS" if not errors else "FAIL"
    payload = {
        "acceptance": acceptance,
        "gate": "DP10_WALK_FORWARD_CALIBRATION_CONTRACT_FREEZE",
        "contract_version": CALIBRATION_CONTRACT_VERSION,
        "model_family": "transportation",
        "panel_sha256": panel_hash,
        "candidate_metric_ids": list(EXPECTED_CANDIDATES),
        "candidate_metric_count": len(EXPECTED_CANDIDATES),
        "cohort_specific_overlay": {
            "marine_shipping_and_maritime": "fleet_utilization",
            "surface_freight_and_logistics": "operating_ratio",
            "air_transport_and_aviation_services": (
                "passenger_load_factor"
            ),
            "development_stage_and_speculative_transport": None,
        },
        "flag_exception_authorized_count": flag_exception_count,
        "flag_exception_decision": (
            "NO_EXCEPTION_GENERAL_GATES_REMAIN_UNCHANGED"
        ),
        "observation_contract": {
            "cadence": "month_end_research_snapshots",
            "portfolio_rebalance_policy_defined": False,
            "portfolio_rebalance_note": (
                "research observation dates do not define a deployment "
                "rebalance schedule"
            ),
            "snapshot_count": len(snapshot_dates),
            "first_snapshot_date": snapshot_dates[0],
            "last_snapshot_date": snapshot_dates[-1],
        },
        "outcome_contract": {
            "forward_return_trading_days": args.forward_trading_days,
            "primary_benchmark": "IYT",
            "robustness_benchmarks": ["XTN", "SPY"],
            "objective": (
                "direction_adjusted_rank_ic_and_top_bottom_excess_return"
            ),
            "transaction_cost_bps_per_one_way_turnover": (
                args.transaction_cost_bps
            ),
            "transaction_cost_stress_bps": (
                args.transaction_cost_stress_bps
            ),
        },
        "split_contract": {
            "method": (
                "chronological_60_20_20_with_forward_window_and_"
                "embargo_boundary_purge"
            ),
            "embargo_days": args.embargo_days,
            "split_counts": dict(sorted(split_counts.items())),
            "holdout_used_for_selection": False,
        },
        "optimization_contract": {
            "mode": "bounded_per_cohort_specialized_overlay_grid",
            "candidate_weights": [0.0, 0.025, 0.05, 0.075, 0.10],
            "direction_applied_before_weight": True,
            "maximum_specialized_overlay_weight_per_cohort": 0.10,
            "generic_component_weights_frozen": True,
            "development_stage_overlay_weight": 0.0,
            "selection_set": "train_and_validation_only",
            "confirmatory_set": "holdout_only",
            "baselines": [
                "zero_specialized_overlay",
                "equal_candidate_overlay_within_applicable_cohort",
            ],
        },
        "acceptance_gates": {
            "minimum_candidate_value_tickers": 3,
            "validation_mean_rank_ic_minimum": 0.0,
            "holdout_mean_rank_ic_minimum": 0.0,
            "validation_net_top_bottom_spread_minimum": 0.0,
            "holdout_net_top_bottom_spread_minimum": 0.0,
            "minimum_holdout_periods_per_candidate_cohort": 12,
            "maximum_average_one_way_turnover": 0.75,
            "base_cost_and_stress_cost_must_pass": True,
            "primary_and_robustness_benchmarks_must_pass": True,
            "candidate_must_beat_zero_overlay_on_validation": True,
            "failed_candidate_action": "retain_zero_overlay_weight",
        },
        "artifacts": {
            "flag_exception_audit": _artifact(
                flag_path,
                row_count=len(flag_rows),
            ),
            "evaluation_calendar": _artifact(
                calendar_path,
                row_count=len(calendar_rows),
            ),
        },
        "inputs": {
            "dp8_preflight": _artifact(preflight_path),
            "panel_manifest": _artifact(panel_manifest_path),
            "panel_validation": _artifact(validation_path),
            "calibration_subset": _artifact(subset_path),
            "historical_coverage": _artifact(
                coverage_path,
                row_count=len(coverage_rows),
            ),
            "specialized_panel": _artifact(
                specialized_path,
                row_count=int(
                    validation.get("specialized_discovery_row_count") or 0
                ),
            ),
            "final_metric_freeze": _artifact(freeze_path),
            "final_metric_dispositions": _artifact(
                disposition_path,
                row_count=len(dispositions),
            ),
            "v2_build_manifest": _artifact(v2_build_path),
        },
        "operations": {
            "parser_invocations": 0,
            "source_document_opens": 0,
            "network_requests": 0,
            "feature_rebuilds": 0,
            "historical_materialization_invocations": 0,
            "calibration_invocations": 0,
            "portfolio_writes": 0,
            "database_writes": 0,
        },
        "calibration_executed": False,
        "single_calibration_authorized": acceptance == "PASS",
        "production_promotion_authorized": False,
        "errors": errors,
        "next_gate": (
            "RUN_SINGLE_WALK_FORWARD_CALIBRATION"
            if acceptance == "PASS"
            else "REVIEW_CALIBRATION_CONTRACT_ERRORS"
        ),
    }
    write_manifest(contract_path, payload)
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0 if acceptance == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
