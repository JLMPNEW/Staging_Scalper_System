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

from industrials.core.config import load_yaml  # noqa: E402
from industrials.transportation.contracts import write_manifest  # noqa: E402
from industrials.transportation.selected_feature_history import (  # noqa: E402
    read_csv,
    read_json,
    sha256,
)
from industrials.transportation.scripts._shared import MODEL_FAMILY  # noqa: E402


EXPECTED_CANDIDATES = (
    "fleet_utilization",
    "operating_ratio",
    "passenger_load_factor",
)
DEFAULT_ARTIFACT_DIR = (
    PROJECT_ROOT
    / "output"
    / "industrials"
    / "transportation"
    / "historical_features"
    / "v3_conflict_resolved"
)
DEFAULT_PORTFOLIO_CONFIG = PROJECT_ROOT / "portfolio_layer" / "config.yaml"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Seal the zero-overlay transportation calibration decision and "
            "bind it to the passing portfolio-layer shadow adapter state."
        )
    )
    parser.add_argument("--asof", default="2026-07-22")
    parser.add_argument(
        "--artifact-dir",
        type=Path,
        default=DEFAULT_ARTIFACT_DIR,
    )
    parser.add_argument(
        "--portfolio-config",
        type=Path,
        default=DEFAULT_PORTFOLIO_CONFIG,
    )
    parser.add_argument("--output-json", type=Path, default=None)
    return parser.parse_args()


def _reference(path: Path, *, row_count: int | None = None) -> dict[str, Any]:
    result: dict[str, Any] = {
        "path": str(path.resolve()),
        "sha256": sha256(path),
    }
    if row_count is not None:
        result["row_count"] = row_count
    return result


def main() -> int:
    args = parse_args()
    asof = str(args.asof)[:10]
    artifact_dir = args.artifact_dir.expanduser().resolve()
    config_path = args.portfolio_config.expanduser().resolve()
    dashboard_dir = (
        PROJECT_ROOT
        / "output"
        / "industrials"
        / "transportation"
        / "dashboard"
        / asof
    ).resolve()
    calibration_manifest_path = (
        artifact_dir
        / "transportation_walk_forward_calibration_manifest.json"
    )
    calibration_validation_path = (
        artifact_dir
        / "transportation_walk_forward_calibration_validation.json"
    )
    rank_path = dashboard_dir / "transportation_final_rank_table.csv"
    rank_manifest_path = (
        dashboard_dir / "transportation_final_rank_table_manifest.json"
    )
    rank_validation_path = (
        dashboard_dir / "transportation_final_rank_table_validation.json"
    )
    portfolio_validation_path = (
        dashboard_dir / "transportation_portfolio_adapter_validation.json"
    )
    for path in (
        calibration_manifest_path,
        calibration_validation_path,
        rank_path,
        rank_manifest_path,
        rank_validation_path,
        portfolio_validation_path,
        config_path,
    ):
        if not path.is_file():
            raise FileNotFoundError(path)

    calibration_manifest = read_json(calibration_manifest_path)
    calibration_validation = read_json(calibration_validation_path)
    rank_manifest = read_json(rank_manifest_path)
    rank_validation = read_json(rank_validation_path)
    portfolio_validation = read_json(portfolio_validation_path)
    rank_rows = read_csv(rank_path)
    errors: list[str] = []

    if (
        calibration_manifest.get("acceptance") != "PASS"
        or calibration_manifest.get("calibration_executed") is not True
        or calibration_manifest.get("production_promotion_authorized")
        is not False
        or calibration_manifest.get("holdout_used_for_selection") is not False
    ):
        errors.append("DP13 calibration manifest is not passing and bounded")
    if (
        calibration_validation.get("acceptance") != "PASS"
        or calibration_validation.get("gate")
        != "DP14_VALIDATE_BOUNDED_WALK_FORWARD_CALIBRATION"
        or calibration_validation.get("holdout_used_for_selection") is not False
        or calibration_validation.get("production_promotion_authorized")
        is not False
    ):
        errors.append("DP14 independent calibration validation is not passing")
    manifest_reference = (
        calibration_validation.get("artifacts") or {}
    ).get("calibration_manifest") or {}
    if (
        str(manifest_reference.get("sha256") or "")
        != sha256(calibration_manifest_path)
    ):
        errors.append("DP14 does not bind the current DP13 manifest")
    final_weights = {
        str(metric): float(weight)
        for metric, weight in (
            calibration_validation.get("final_research_weights") or {}
        ).items()
    }
    if (
        tuple(sorted(final_weights)) != tuple(sorted(EXPECTED_CANDIDATES))
        or any(weight != 0.0 for weight in final_weights.values())
        or calibration_validation.get("confirmed_research_metric_count") != 0
    ):
        errors.append("the frozen calibration decision is not all-zero")
    decisions = calibration_manifest.get("candidate_decisions") or {}
    for metric in EXPECTED_CANDIDATES:
        item = decisions.get(metric) or {}
        if (
            float(item.get("final_research_weight") or 0) != 0.0
            or item.get("decision") != "RETAIN_ZERO_OVERLAY"
        ):
            errors.append(f"DP13 zero-overlay decision mismatch={metric}")

    if (
        rank_manifest.get("acceptance") != "PASS"
        or rank_manifest.get("model_family") != MODEL_FAMILY
        or rank_manifest.get("asof_date") != asof
        or rank_manifest.get("rank_table_sha256") != sha256(rank_path)
        or int(rank_manifest.get("row_count") or 0) != len(rank_rows)
    ):
        errors.append("sealed shadow rank-table manifest is invalid")
    if (
        rank_validation.get("acceptance") != "PASS"
        or rank_validation.get("model_family") != MODEL_FAMILY
        or rank_validation.get("asof_date") != asof
        or int(rank_validation.get("row_count") or 0) != len(rank_rows)
        or int(rank_validation.get("oos_score_valid_count") or 0) != 0
        or int(rank_validation.get("portfolio_candidate_count") or 0) != 0
    ):
        errors.append("sealed shadow rank-table validation is invalid")
    required_zero_fields = (
        "portfolio_candidate_gate",
        "research_calibration_input_eligible_flag",
        "stage11_calibration_input_eligible_flag",
        "survivorship_corrected_panel_flag",
        "oos_score_valid_flag",
    )
    for field in required_zero_fields:
        nonzero = [
            row["ticker"]
            for row in rank_rows
            if str(row.get(field) or "") != "0"
        ]
        if nonzero:
            errors.append(f"rank-table shadow boundary failed={field}:{nonzero[:10]}")

    expected_portfolio = {
        "acceptance": "PASS",
        "adapter": "industrial_family",
        "source_pipeline": MODEL_FAMILY,
        "source_asof_date": asof,
        "rows": len(rank_rows),
        "investable_rows": 0,
        "oos_score_valid_rows": 0,
        "research_eligible_rows": 0,
        "survivorship_corrected_rows": 0,
        "errors": [],
    }
    for key, expected in expected_portfolio.items():
        if portfolio_validation.get(key) != expected:
            errors.append(f"portfolio shadow validation mismatch={key}")

    portfolio_config = load_yaml(config_path)
    sources = [
        source
        for source in (
            (portfolio_config.get("score_contract") or {}).get("sectors")
            or []
        )
        if str(source.get("model_family") or "") == MODEL_FAMILY
    ]
    if len(sources) != 1:
        errors.append(
            f"expected one transportation portfolio source, found={len(sources)}"
        )
    else:
        source = sources[0]
        if (
            source.get("adapter") != "industrial_family"
            or source.get("enabled") is not True
            or source.get("required") is not False
            or source.get("require_oos_score_valid") is not True
        ):
            errors.append("transportation portfolio source is not fail-closed shadow")

    operation_counts = calibration_manifest.get("operations") or {}
    for key in (
        "database_writes",
        "parser_invocations",
        "network_requests",
        "feature_rebuilds",
        "membership_rebuilds",
        "portfolio_writes",
        "production_config_writes",
    ):
        if int(operation_counts.get(key) or 0) != 0:
            errors.append(f"bounded-operation violation={key}")
    if int(operation_counts.get("calibration_invocations") or 0) != 1:
        errors.append("calibration invocation count must remain exactly one")

    acceptance = "PASS" if not errors else "FAIL"
    output_path = (
        args.output_json.expanduser().resolve()
        if args.output_json
        else artifact_dir
        / "transportation_zero_overlay_portfolio_shadow_gate.json"
    )
    payload: dict[str, Any] = {
        "acceptance": acceptance,
        "gate": "DP15_FINALIZE_ZERO_OVERLAY_PORTFOLIO_SHADOW",
        "model_family": MODEL_FAMILY,
        "asof_date": asof,
        "final_research_weights": final_weights,
        "confirmed_research_metric_count": 0,
        "zero_overlay_decision_sealed": acceptance == "PASS",
        "portfolio_shadow_validation_executed": True,
        "portfolio_shadow_row_count": len(rank_rows),
        "investable_row_count": int(
            portfolio_validation.get("investable_rows") or 0
        ),
        "oos_score_valid_row_count": int(
            portfolio_validation.get("oos_score_valid_rows") or 0
        ),
        "production_promotion_authorized": False,
        "artifacts": {
            "calibration_manifest": _reference(calibration_manifest_path),
            "calibration_validation": _reference(
                calibration_validation_path
            ),
            "rank_table": _reference(rank_path, row_count=len(rank_rows)),
            "rank_manifest": _reference(rank_manifest_path),
            "rank_validation": _reference(rank_validation_path),
            "portfolio_adapter_validation": _reference(
                portfolio_validation_path
            ),
            "portfolio_config": _reference(config_path),
        },
        "operations": {
            "calibration_invocations": 0,
            "database_writes": 0,
            "parser_invocations": 0,
            "network_requests": 0,
            "feature_rebuilds": 0,
            "membership_rebuilds": 0,
            "portfolio_writes": 0,
            "production_config_writes": 0,
        },
        "errors": errors,
        "next_gate": (
            "CONTINUE_ZERO_OVERLAY_SHADOW_MONITORING"
            if acceptance == "PASS"
            else "REVIEW_ZERO_OVERLAY_PORTFOLIO_SHADOW_FAILURES"
        ),
    }
    write_manifest(output_path, payload)
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0 if acceptance == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
