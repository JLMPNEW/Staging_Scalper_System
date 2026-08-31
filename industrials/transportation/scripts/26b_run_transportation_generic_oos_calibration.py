#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from industrials.core.config import (  # noqa: E402
    cfg_get,
    family_config,
    load_yaml,
    resolve_path,
)
from industrials.core.oos_research import (  # noqa: E402
    artifact_sha256,
    evaluate_candidate,
    finite_float,
    finite_or_default,
    fmt,
)
from industrials.core.reports import (  # noqa: E402
    write_csv_atomic,
    write_text_atomic,
)
from industrials.transportation.contracts import (  # noqa: E402
    COMPONENT_FIELDS,
    read_rows,
)
from industrials.transportation.prebuild_contract import (  # noqa: E402
    load_prebuild_contract,
)
from industrials.transportation.walk_forward_calibration import (  # noqa: E402
    purged_expanding_walk_forward_blocks,
)
from industrials.transportation.scripts._shared import (  # noqa: E402
    DEFAULT_CONFIG,
)


SUMMARY_FIELDS = [
    "candidate_id",
    "split",
    "selected_flag",
    "snapshot_count",
    "eligible_row_count",
    "available_outcome_row_count",
    "outcome_coverage",
    "mean_ic",
    "mean_top_excess_net",
    "top_excess_hit_rate",
    "max_drawdown",
    "non_overlapping_snapshot_count",
    "mean_non_overlapping_top_excess_net",
    "non_overlapping_top_excess_hit_rate",
    "average_turnover",
    "absolute_gate_status",
    "failed_gates",
    *COMPONENT_FIELDS,
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Run deterministic finite-candidate transportation generic-score "
            "calibration with purged validation, sealed holdout, and "
            "expanding walk-forward stability diagnostics."
        )
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--allow-overwrite", action="store_true")
    return parser.parse_args()


def gate_result(
    metrics: dict[str, Any],
    gates: dict[str, float],
) -> tuple[bool, list[str]]:
    checks = {
        "minimum_snapshot_count": (
            float(metrics.get("snapshot_count") or 0)
            >= float(gates["minimum_snapshot_count"])
        ),
        "minimum_outcome_coverage": (
            float(metrics.get("outcome_coverage") or 0)
            >= float(gates["minimum_outcome_coverage"])
        ),
        "minimum_mean_ic": (
            finite_float(metrics.get("mean_ic")) is not None
            and float(metrics["mean_ic"])
            >= float(gates["minimum_mean_ic"])
        ),
        "minimum_mean_top_excess_net": (
            finite_float(metrics.get("mean_top_excess_net")) is not None
            and float(metrics["mean_top_excess_net"])
            > float(gates["minimum_mean_top_excess_net"])
        ),
        "minimum_top_excess_hit_rate": (
            finite_float(metrics.get("top_excess_hit_rate")) is not None
            and float(metrics["top_excess_hit_rate"])
            >= float(gates["minimum_top_excess_hit_rate"])
        ),
        "minimum_max_drawdown": (
            finite_float(metrics.get("max_drawdown")) is not None
            and float(metrics["max_drawdown"])
            >= float(gates["minimum_max_drawdown"])
        ),
        "maximum_average_turnover": (
            finite_float(metrics.get("average_turnover")) is not None
            and float(metrics["average_turnover"])
            <= float(gates["maximum_average_turnover"])
        ),
    }
    failed = [name for name, passed in checks.items() if not passed]
    return not failed, failed


def serialize_summary(
    candidate_id: str,
    split: str,
    selected: bool,
    metrics: dict[str, Any],
    weights: dict[str, float],
    gates: dict[str, float],
) -> dict[str, str]:
    passed, failed = gate_result(metrics, gates)
    row = {
        "candidate_id": candidate_id,
        "split": split,
        "selected_flag": "1" if selected else "0",
        "snapshot_count": str(metrics.get("snapshot_count") or 0),
        "eligible_row_count": str(
            metrics.get("eligible_row_count") or 0
        ),
        "available_outcome_row_count": str(
            metrics.get("available_outcome_row_count") or 0
        ),
        "outcome_coverage": fmt(metrics.get("outcome_coverage")),
        "mean_ic": fmt(metrics.get("mean_ic")),
        "mean_top_excess_net": fmt(
            metrics.get("mean_top_excess_net")
        ),
        "top_excess_hit_rate": fmt(
            metrics.get("top_excess_hit_rate")
        ),
        "max_drawdown": fmt(metrics.get("max_drawdown")),
        "non_overlapping_snapshot_count": str(
            metrics.get("non_overlapping_snapshot_count") or 0
        ),
        "mean_non_overlapping_top_excess_net": fmt(
            metrics.get("mean_non_overlapping_top_excess_net")
        ),
        "non_overlapping_top_excess_hit_rate": fmt(
            metrics.get("non_overlapping_top_excess_hit_rate")
        ),
        "average_turnover": fmt(metrics.get("average_turnover")),
        "absolute_gate_status": "PASS" if passed else "FAIL",
        "failed_gates": ";".join(failed),
    }
    row.update(
        {
            field: fmt(weights.get(field, 0.0))
            for field in COMPONENT_FIELDS
        }
    )
    return row


def evaluate(
    rows: list[dict[str, str]],
    *,
    weights: dict[str, float],
    split: str,
    standards: dict[str, Any],
) -> dict[str, Any]:
    return evaluate_candidate(
        rows,
        weights=weights,
        split=split,
        horizon_sessions=63,
        top_fraction=float(standards["top_fraction"]),
        minimum_cross_section=int(
            standards["minimum_cross_section"]
        ),
        transaction_cost_bps=float(
            standards["transaction_cost_bps"]
        ),
        require_complete_components=True,
    )


def main() -> int:
    args = parse_args()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    base_dir = config_path.parent
    family = family_config(config, "transportation")
    prebuild = load_prebuild_contract(config_path, family)
    standards = cfg_get(
        config,
        "oos_calibration_standards.families.transportation",
    )
    if not isinstance(standards, dict):
        raise ValueError("Missing transportation OOS standards")
    root = resolve_path(
        standards["research_output_root"],
        base_dir=base_dir,
    )
    panel_path = root / "transportation_generic_oos_panel.csv"
    panel_manifest_path = (
        root / "transportation_generic_oos_panel_manifest.json"
    )
    panel_validation_path = (
        root / "transportation_generic_oos_panel_validation.json"
    )
    for path in (
        panel_path,
        panel_manifest_path,
        panel_validation_path,
    ):
        if not path.is_file():
            raise FileNotFoundError(path)
    panel_manifest = json.loads(
        panel_manifest_path.read_text(encoding="utf-8")
    )
    panel_validation = json.loads(
        panel_validation_path.read_text(encoding="utf-8")
    )
    if (
        panel_manifest.get("acceptance") != "PASS"
        or panel_validation.get("acceptance") != "PASS"
        or panel_manifest.get("panel_sha256")
        != artifact_sha256(panel_path)
        or panel_validation.get("panel_sha256")
        != artifact_sha256(panel_path)
    ):
        raise ValueError(
            "Generic OOS panel has not passed independent validation"
        )
    summary_path = (
        root / "transportation_generic_oos_calibration_summary.csv"
    )
    periods_path = (
        root / "transportation_generic_oos_backtest_periods.csv"
    )
    walk_forward_path = (
        root / "transportation_generic_oos_walk_forward.csv"
    )
    registry_path = (
        root / "transportation_generic_oos_candidate_registry.json"
    )
    manifest_path = (
        root / "transportation_generic_oos_calibration_manifest.json"
    )
    preflight_path = (
        root / "transportation_calibration_input_preflight.json"
    )
    if not preflight_path.is_file():
        raise FileNotFoundError(
            "Transportation calibration input preflight is required"
        )
    if (
        not args.allow_overwrite
        and any(
            path.exists()
            for path in (
                summary_path,
                periods_path,
                walk_forward_path,
                registry_path,
                manifest_path,
            )
        )
    ):
        raise FileExistsError(
            "Generic OOS calibration is sealed; use --allow-overwrite"
        )
    rows = read_rows(panel_path)
    candidates = {
        str(candidate_id): {
            str(field): float(weight) for field, weight in weights.items()
        }
        for candidate_id, weights in prebuild["enabled_candidate_registry"].items()
    }
    preflight = json.loads(preflight_path.read_text(encoding="utf-8"))
    if (
        preflight.get("acceptance") != "PASS"
        or preflight.get("panel_sha256") != artifact_sha256(panel_path)
        or preflight.get("candidate_registry") != candidates
    ):
        raise ValueError(
            "Transportation calibration input preflight failed or is stale: "
            + "; ".join(
                str(item) for item in (preflight.get("issues") or [])[:20]
            )
        )
    gates = {
        str(key): float(value)
        for key, value in standards["absolute_gates"].items()
    }
    validation_metrics = {
        candidate_id: evaluate(
            rows,
            weights=weights,
            split="validation",
            standards=standards,
        )
        for candidate_id, weights in candidates.items()
    }
    ordered_candidates = sorted(
        candidates,
        key=lambda candidate_id: (
            -finite_or_default(
                validation_metrics[candidate_id].get(
                    "mean_top_excess_net"
                ),
                default=-999.0,
            ),
            -finite_or_default(
                validation_metrics[candidate_id].get("mean_ic"),
                default=-999.0,
            ),
            candidate_id,
        ),
    )
    validation_passers = [
        candidate_id
        for candidate_id in ordered_candidates
        if gate_result(
            validation_metrics[candidate_id],
            gates,
        )[0]
    ]
    selected_id = (
        validation_passers[0]
        if validation_passers
        else ordered_candidates[0]
    )
    selected_weights = candidates[selected_id]
    holdout_metrics = evaluate(
        rows,
        weights=selected_weights,
        split="holdout",
        standards=standards,
    )
    baseline_candidate_id = "surface_balanced"
    if baseline_candidate_id not in candidates:
        raise ValueError(f"missing pre-registered baseline={baseline_candidate_id}")
    baseline_holdout = evaluate(
        rows,
        weights=candidates[baseline_candidate_id],
        split="holdout",
        standards=standards,
    )
    summary_rows: list[dict[str, str]] = []
    for candidate_id in candidates:
        summary_rows.append(
            serialize_summary(
                candidate_id,
                "validation",
                candidate_id == selected_id,
                validation_metrics[candidate_id],
                candidates[candidate_id],
                gates,
            )
        )
    summary_rows.append(
        serialize_summary(
            selected_id,
            "holdout",
            True,
            holdout_metrics,
            selected_weights,
            gates,
        )
    )
    if selected_id != baseline_candidate_id:
        summary_rows.append(
            serialize_summary(
                baseline_candidate_id,
                "holdout_reference",
                False,
                baseline_holdout,
                candidates[baseline_candidate_id],
                gates,
            )
        )
    write_csv_atomic(summary_path, SUMMARY_FIELDS, summary_rows)
    period_fields = [
        "asof_date",
        "exit_date",
        "cross_section",
        "selected",
        "ic",
        "turnover",
        "gross_excess",
        "net_excess",
    ]
    write_csv_atomic(
        periods_path,
        period_fields,
        [
            {
                field: fmt(row.get(field))
                if field not in {"asof_date", "exit_date", "cross_section", "selected"}
                else str(row.get(field) or "")
                for field in period_fields
            }
            for row in holdout_metrics["period_rows"]
        ],
    )
    walk_forward_blocks = purged_expanding_walk_forward_blocks(
        rows,
        horizon_sessions=63,
        block_count=4,
        minimum_initial_dates=52,
    )
    walk_rows: list[dict[str, str]] = []
    for block_spec in walk_forward_blocks:
        block = int(block_spec["block"])
        training_dates = set(block_spec["training_dates"])
        evaluation_dates = set(block_spec["evaluation_dates"])
        research_rows: list[dict[str, str]] = []
        for row in rows:
            cloned = dict(row)
            if row["asof_date"] in training_dates:
                cloned["split"] = f"wf_train_{block}"
            elif row["asof_date"] in evaluation_dates:
                cloned["split"] = f"wf_test_{block}"
            else:
                cloned["split"] = "excluded"
            research_rows.append(cloned)
        training_metrics = {
            candidate_id: evaluate(
                research_rows,
                weights=weights,
                split=f"wf_train_{block}",
                standards=standards,
            )
            for candidate_id, weights in candidates.items()
        }
        block_candidate = sorted(
            candidates,
            key=lambda candidate_id: (
                -finite_or_default(
                    training_metrics[candidate_id].get(
                        "mean_top_excess_net"
                    ),
                    default=-999.0,
                ),
                candidate_id,
            ),
        )[0]
        test_metrics = evaluate(
            research_rows,
            weights=candidates[block_candidate],
            split=f"wf_test_{block}",
            standards=standards,
        )
        block_pass = (
            finite_float(test_metrics.get("mean_top_excess_net"))
            is not None
            and float(test_metrics["mean_top_excess_net"]) > 0
            and finite_float(test_metrics.get("mean_ic")) is not None
            and float(test_metrics["mean_ic"]) >= 0
        )
        walk_rows.append(
            {
                "block": str(block),
                "train_start": str(block_spec["train_start"]),
                "train_end": str(block_spec["train_end"]),
                "test_start": str(block_spec["test_start"]),
                "test_end": str(block_spec["test_end"]),
                "selected_candidate_id": block_candidate,
                "test_snapshot_count": str(
                    test_metrics["snapshot_count"]
                ),
                "test_mean_ic": fmt(test_metrics.get("mean_ic")),
                "test_mean_top_excess_net": fmt(
                    test_metrics.get("mean_top_excess_net")
                ),
                "test_hit_rate": fmt(
                    test_metrics.get("top_excess_hit_rate")
                ),
                "block_status": "PASS" if block_pass else "FAIL",
            }
        )
    write_csv_atomic(
        walk_forward_path,
        [
            "block",
            "train_start",
            "train_end",
            "test_start",
            "test_end",
            "selected_candidate_id",
            "test_snapshot_count",
            "test_mean_ic",
            "test_mean_top_excess_net",
            "test_hit_rate",
            "block_status",
        ],
        walk_rows,
    )
    validation_pass, validation_failures = gate_result(
        validation_metrics[selected_id],
        gates,
    )
    holdout_pass, holdout_failures = gate_result(
        holdout_metrics,
        gates,
    )
    walk_forward_pass_rate = (
        sum(row["block_status"] == "PASS" for row in walk_rows)
        / len(walk_rows)
        if walk_rows
        else 0.0
    )
    split_dates = {
        split: sorted(
            {
                row["asof_date"]
                for row in rows
                if row["split"] == split
            }
        )
        for split in ("train", "validation", "holdout")
    }
    freeze_asof = str(prebuild["asof_date"])
    validation_is_post_freeze = bool(split_dates["validation"]) and all(
        value > freeze_asof for value in split_dates["validation"]
    )
    holdout_is_post_freeze = bool(split_dates["holdout"]) and all(
        value > freeze_asof for value in split_dates["holdout"]
    )
    promotion_evidence_eligible = bool(
        validation_is_post_freeze and holdout_is_post_freeze
    )
    statistical_gate_pass = (
        validation_pass
        and holdout_pass
        and walk_forward_pass_rate >= 0.50
    )
    promotion_eligible = statistical_gate_pass and promotion_evidence_eligible
    registry = {
        "artifact_family": "transportation_generic_oos_candidate_registry",
        "selection_rule": (
            "highest_validation_mean_top_excess_net_then_mean_ic; "
            "holdout untouched until one candidate is selected"
        ),
        "candidates": candidates,
        "registry_frozen_before_holdout_evaluation": True,
        "evidence_posture": (
            "untouched_post_freeze"
            if promotion_evidence_eligible
            else "spent_history_diagnostic_only"
        ),
        "prebuild_freeze_asof": freeze_asof,
    }
    write_text_atomic(
        registry_path,
        json.dumps(registry, indent=2, sort_keys=True) + "\n",
    )
    manifest = {
        "artifact_family": "transportation_generic_oos_calibration",
        "model_family": "transportation",
        "artifact_acceptance": "PASS",
        "promotion_gate_status": (
            "PASS"
            if promotion_eligible
            else "FAIL_EVIDENCE_REUSE"
            if statistical_gate_pass and not promotion_evidence_eligible
            else "FAIL_STATISTICAL_GATES"
        ),
        "promotion_eligible": promotion_eligible,
        "statistical_gate_pass": statistical_gate_pass,
        "promotion_evidence_eligible": promotion_evidence_eligible,
        "evidence_posture": (
            "untouched_post_freeze"
            if promotion_evidence_eligible
            else "spent_history_diagnostic_only"
        ),
        "prebuild_freeze_asof": freeze_asof,
        "validation_is_post_freeze": validation_is_post_freeze,
        "holdout_is_post_freeze": holdout_is_post_freeze,
        "promotion_from_revealed_holdout_allowed": False,
        "selected_candidate_id": selected_id,
        "selected_weights": selected_weights,
        "selection_used_holdout": False,
        "holdout_evaluated_candidate_id": selected_id,
        "validation_gate_status": (
            "PASS" if validation_pass else "FAIL"
        ),
        "validation_failed_gates": validation_failures,
        "holdout_gate_status": "PASS" if holdout_pass else "FAIL",
        "holdout_failed_gates": holdout_failures,
        "walk_forward_pass_rate": walk_forward_pass_rate,
        "minimum_walk_forward_pass_rate": 0.50,
        "walk_forward_boundary_policy": (
            "purged_actual_benchmark_exit_strictly_before_test_start"
        ),
        "return_basis": "next_session_open_execution_excess",
        "horizon_sessions": 63,
        "production_universe_policy": "frozen_24_surface_freight_only",
        "prebuild_contract_path": prebuild["manifest_path"],
        "prebuild_contract_sha256": prebuild["manifest_sha256"],
        "score_engine_version": prebuild["score_engine_version"],
        "train_start_date": (
            split_dates["train"][0] if split_dates["train"] else ""
        ),
        "train_end_date": (
            split_dates["train"][-1] if split_dates["train"] else ""
        ),
        "validation_start_date": (
            split_dates["validation"][0]
            if split_dates["validation"]
            else ""
        ),
        "validation_end_date": (
            split_dates["validation"][-1]
            if split_dates["validation"]
            else ""
        ),
        "holdout_start_date": (
            split_dates["holdout"][0]
            if split_dates["holdout"]
            else ""
        ),
        "holdout_end_date": (
            split_dates["holdout"][-1]
            if split_dates["holdout"]
            else ""
        ),
        "panel_path": str(panel_path),
        "panel_sha256": artifact_sha256(panel_path),
        "panel_validation_path": str(panel_validation_path),
        "panel_validation_sha256": artifact_sha256(
            panel_validation_path
        ),
        "calibration_input_preflight_path": str(preflight_path),
        "calibration_input_preflight_sha256": artifact_sha256(preflight_path),
        "component_missingness_policy": "strict_complete_positive_weight_components",
        "structurally_excluded_components": preflight.get(
            "structurally_excluded_components", []
        ),
        "candidate_registry_path": str(registry_path),
        "candidate_registry_sha256": artifact_sha256(registry_path),
        "summary_path": str(summary_path),
        "summary_sha256": artifact_sha256(summary_path),
        "backtest_periods_path": str(periods_path),
        "backtest_periods_sha256": artifact_sha256(periods_path),
        "walk_forward_path": str(walk_forward_path),
        "walk_forward_sha256": artifact_sha256(walk_forward_path),
        "absolute_gates": gates,
    }
    write_text_atomic(
        manifest_path,
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
    )
    print(json.dumps(manifest, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
