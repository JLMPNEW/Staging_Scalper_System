#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


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
    finite_float,
)
from industrials.core.reports import (  # noqa: E402
    write_csv_atomic,
    write_text_atomic,
)
from industrials.transportation.contracts import read_rows  # noqa: E402
from industrials.transportation.legacy_production_routes import (  # noqa: E402
    route_diagnostic,
)
from industrials.transportation.scripts._shared import (  # noqa: E402
    DEFAULT_CONFIG,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Audit transportation generic OOS evidence before any "
            "production promotion. This gate never mutates rank tables."
        )
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--asof", default="2026-07-30")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    base_dir = config_path.parent
    family = family_config(config, "transportation")
    standards = cfg_get(
        config,
        "oos_calibration_standards.families.transportation",
    )
    if not isinstance(standards, dict):
        raise ValueError("Missing transportation OOS standards")
    research_root = resolve_path(
        standards["research_output_root"],
        base_dir=base_dir,
    )
    dashboard_root = resolve_path(
        family["historical_scores"]["output_root"],
        base_dir=base_dir,
    )
    history_build_manifest = resolve_path(
        family["historical_scores"]["build_manifest_json"],
        base_dir=base_dir,
    )
    history_validation_path = history_build_manifest.with_name(
        "transportation_weekly_rank_history_validation.json"
    )
    panel_validation_path = (
        research_root / "transportation_generic_oos_panel_validation.json"
    )
    calibration_path = (
        research_root
        / "transportation_generic_oos_calibration_manifest.json"
    )
    rank_path = (
        dashboard_root
        / args.asof
        / "transportation_final_rank_table.csv"
    )
    rank_manifest_path = rank_path.with_name(
        "transportation_final_rank_table_manifest.json"
    )
    adapter_path = rank_path.with_name(
        "transportation_portfolio_adapter_validation.json"
    )
    required = {
        "daily_history_validation": history_validation_path,
        "panel_validation": panel_validation_path,
        "calibration_manifest": calibration_path,
        "current_rank_table": rank_path,
        "current_rank_manifest": rank_manifest_path,
        "portfolio_adapter_shadow_validation": adapter_path,
    }
    issues: list[str] = []
    for label, path in required.items():
        if not path.is_file():
            issues.append(f"{label}: missing {path}")
    payloads: dict[str, dict[str, object]] = {}
    for label, path in required.items():
        if path.suffix == ".json" and path.is_file():
            payloads[label] = json.loads(
                path.read_text(encoding="utf-8")
            )
    history = payloads.get("daily_history_validation", {})
    panel = payloads.get("panel_validation", {})
    calibration = payloads.get("calibration_manifest", {})
    rank_manifest = payloads.get("current_rank_manifest", {})
    adapter = payloads.get("portfolio_adapter_shadow_validation", {})
    raw_validation_failed = calibration.get("validation_failed_gates")
    validation_failed = (
        [str(item) for item in raw_validation_failed]
        if isinstance(raw_validation_failed, list)
        else []
    )
    raw_holdout_failed = calibration.get("holdout_failed_gates")
    holdout_failed = (
        [str(item) for item in raw_holdout_failed]
        if isinstance(raw_holdout_failed, list)
        else []
    )
    walk_forward_rate = finite_float(
        calibration.get("walk_forward_pass_rate")
    ) or 0.0
    minimum_walk_forward_rate = finite_float(
        calibration.get("minimum_walk_forward_pass_rate")
    )
    if minimum_walk_forward_rate is None:
        minimum_walk_forward_rate = 1.0
    scoring_config = family.get("scoring")
    frozen_surface_universe = (
        isinstance(scoring_config, dict)
        and str(scoring_config.get("score_construction_mode") or "")
        == "surface_freight_fixed_denominator_v2"
    )
    if history.get("acceptance") != "PASS":
        issues.append("weekly score history has not passed")
    if (
        not frozen_surface_universe
        and not history.get("active_and_inactive_membership_sources_present")
    ):
        issues.append("active/delisted history sources are not both present")
    if panel.get("acceptance") != "PASS":
        issues.append("generic OOS panel has not passed")
    if calibration.get("artifact_acceptance") != "PASS":
        issues.append("generic OOS calibration artifacts are invalid")
    if calibration.get("selection_used_holdout") is not False:
        issues.append("candidate selection used holdout evidence")
    if calibration.get("validation_gate_status") != "PASS":
        issues.append(
            "validation gate failed: "
            + ",".join(validation_failed)
        )
    if calibration.get("holdout_gate_status") != "PASS":
        issues.append(
            "holdout gate failed: "
            + ",".join(holdout_failed)
        )
    if walk_forward_rate < minimum_walk_forward_rate:
        issues.append(
            "walk-forward stability gate failed: "
            f"{calibration.get('walk_forward_pass_rate')}"
        )
    if calibration.get("promotion_eligible") is not True:
        issues.append("calibration is not promotion eligible")
    if calibration.get("promotion_evidence_eligible") is not True:
        issues.append("untouched post-freeze promotion evidence is not available")
    if rank_manifest.get("acceptance") != "PASS":
        issues.append("current rank manifest has not passed")
    if (
        rank_path.is_file()
        and rank_manifest.get("rank_table_sha256")
        != artifact_sha256(rank_path)
    ):
        issues.append("current rank hash mismatch")
    rank_rows = read_rows(rank_path) if rank_path.is_file() else []
    if any(
        row.get("portfolio_candidate_gate") != "0"
        or row.get("oos_score_valid_flag") != "0"
        for row in rank_rows
    ):
        issues.append("source rank table is not a fail-closed shadow")
    if adapter.get("acceptance") != "PASS":
        issues.append("portfolio adapter shadow validation has not passed")
    artifact_hash_issues: list[str] = []
    for path_key, hash_key in (
        ("panel_path", "panel_sha256"),
        ("panel_validation_path", "panel_validation_sha256"),
        ("candidate_registry_path", "candidate_registry_sha256"),
        ("summary_path", "summary_sha256"),
        ("backtest_periods_path", "backtest_periods_sha256"),
        ("walk_forward_path", "walk_forward_sha256"),
    ):
        raw = str(calibration.get(path_key) or "")
        if not raw:
            artifact_hash_issues.append(f"{path_key}: missing")
            continue
        path = Path(raw)
        if (
            not path.is_file()
            or artifact_sha256(path) != calibration.get(hash_key)
        ):
            artifact_hash_issues.append(f"{path_key}: hash mismatch")
    issues.extend(artifact_hash_issues)
    supersession = route_diagnostic(
        "27_audit_transportation_production_readiness"
    )
    issues.append(
        "legacy retrospective generic-OOS evidence cannot authorize production"
    )
    gates = [
        {
            "gate": "daily_history",
            "status": "PASS" if history.get("acceptance") == "PASS" else "FAIL",
            "detail": (
                f"{history.get('validated_date_count') or ''} weekly snapshots; "
                + (
                    "frozen active surface-freight cohort"
                    if frozen_surface_universe
                    else "active/delisted PIT universe"
                )
            ),
        },
        {
            "gate": "oos_panel",
            "status": "PASS" if panel.get("acceptance") == "PASS" else "FAIL",
            "detail": str(panel.get("weekly_snapshot_count") or ""),
        },
        {
            "gate": "validation",
            "status": str(calibration.get("validation_gate_status") or "FAIL"),
            "detail": ",".join(validation_failed),
        },
        {
            "gate": "holdout",
            "status": str(calibration.get("holdout_gate_status") or "FAIL"),
            "detail": ",".join(holdout_failed),
        },
        {
            "gate": "walk_forward",
            "status": (
                "PASS"
                if walk_forward_rate >= minimum_walk_forward_rate
                else "FAIL"
            ),
            "detail": str(
                calibration.get("walk_forward_pass_rate") or 0
            ),
        },
        {
            "gate": "portfolio_adapter_shadow",
            "status": "PASS" if adapter.get("acceptance") == "PASS" else "FAIL",
            "detail": "fail-closed source required",
        },
        {
            "gate": "artifact_hashes",
            "status": "PASS" if not artifact_hash_issues else "FAIL",
            "detail": ";".join(artifact_hash_issues),
        },
        {
            "gate": "canonical_prospective_activation",
            "status": "FAIL",
            "detail": str(supersession["route_status"]),
        },
    ]
    report_path = (
        research_root
        / "transportation_production_readiness_audit.csv"
    )
    manifest_path = (
        research_root
        / "transportation_production_readiness_audit.json"
    )
    write_csv_atomic(
        report_path,
        ["gate", "status", "detail"],
        gates,
    )
    result = {
        "artifact_family": "transportation_production_readiness_audit",
        "model_family": "transportation",
        "audit_acceptance": "PASS",
        "promotion_readiness": "FAIL",
        "promotion_eligible": False,
        "production_activation_authorized": False,
        "portfolio_allocation_authorized": False,
        "evidence_scope": "RETROSPECTIVE_DIAGNOSTIC_ONLY",
        "frozen_surface_universe": frozen_surface_universe,
        "asof_date": args.asof,
        "legacy_route": supersession,
        "selected_candidate_id": calibration.get(
            "selected_candidate_id", ""
        ),
        "selected_weights": calibration.get("selected_weights") or {},
        "issues": issues,
        "report_path": str(report_path),
        "report_sha256": artifact_sha256(report_path),
        "inputs": {
            label: {
                "path": str(path),
                "sha256": (
                    artifact_sha256(path) if path.is_file() else ""
                ),
            }
            for label, path in required.items()
        },
    }
    write_text_atomic(
        manifest_path,
        json.dumps(result, indent=2, sort_keys=True) + "\n",
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
