#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from industrials.core.config import family_config, load_yaml, resolve_path  # noqa: E402
from industrials.transportation.financial_contract import load_metric_registry  # noqa: E402
from industrials.transportation.scripts._shared import DEFAULT_CONFIG, MODEL_FAMILY  # noqa: E402
from industrials.transportation.selected_feature_history import (  # noqa: E402
    read_csv,
    read_json,
    sha256,
    snapshot_dates,
    validate_panel_stream,
    verify_artifact,
    verify_v2_snapshots,
    write_manifest,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Validate exact rows, membership, hashes, point-in-time dates, and "
            "the one-pass calibration subset for the transportation v3 panel."
        )
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--output-dir", type=Path, default=None)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    family = family_config(config, MODEL_FAMILY)
    parser_cfg = family["dedicated_parser"]
    historical_cfg = family["historical_features"]
    financial_cfg = family["financial"]
    base_dir = config_path.parent
    historical_root = resolve_path(
        historical_cfg["output_root"], base_dir=base_dir
    )
    output_dir = (
        args.output_dir.expanduser().resolve()
        if args.output_dir
        else historical_root / "v3"
    )
    panel_manifest_path = output_dir / "transportation_v3_panel_manifest.json"
    panel = read_json(panel_manifest_path)
    errors: list[str] = []
    if panel.get("acceptance") != "PASS":
        errors.append("v3 panel manifest does not pass")
    artifacts: dict[str, Path] = {}
    for label, reference in panel.get("artifacts", {}).items():
        try:
            artifacts[label] = verify_artifact(reference, label=label)
        except (FileNotFoundError, ValueError) as exc:
            errors.append(str(exc))
    build_path = resolve_path(
        historical_cfg["build_manifest_json"], base_dir=base_dir
    )
    validation_path = resolve_path(
        historical_cfg["validation_output_json"], base_dir=base_dir
    )
    dates = snapshot_dates(read_json(build_path))
    try:
        verify_v2_snapshots(
            historical_root=historical_root,
            validation_manifest=read_json(validation_path),
        )
    except (FileNotFoundError, ValueError) as exc:
        errors.append(str(exc))
    registry_path = resolve_path(
        parser_cfg["discovery_registry_csv"], base_dir=base_dir
    )
    discovery_ids = sorted(
        row["metric_id"] for row in read_csv(registry_path)
    )
    _, generic_metrics = load_metric_registry(
        resolve_path(financial_cfg["metric_registry"], base_dir=base_dir)
    )
    generic_ids = sorted(
        metric.metric_id
        for metric in generic_metrics
        if not bool(metric.specialized)
    )
    discovery_rows = 0
    complete_rows = 0
    if "specialized_discovery_panel" in artifacts:
        discovery_rows, stream_errors = validate_panel_stream(
            path=artifacts["specialized_discovery_panel"],
            historical_root=historical_root,
            dates=dates,
            expected_metric_keys=[
                ("specialized_discovery", metric_id)
                for metric_id in discovery_ids
            ],
        )
        errors.extend(stream_errors)
    if "complete_panel" in artifacts:
        complete_rows, stream_errors = validate_panel_stream(
            path=artifacts["complete_panel"],
            historical_root=historical_root,
            dates=dates,
            expected_metric_keys=[
                *[("generic", metric_id) for metric_id in generic_ids],
                *[
                    ("specialized_discovery", metric_id)
                    for metric_id in discovery_ids
                ],
            ],
        )
        errors.extend(stream_errors)
    if discovery_rows != 854_640:
        errors.append(f"discovery rows={discovery_rows} expected=854640")
    if complete_rows != 1_025_568:
        errors.append(f"complete rows={complete_rows} expected=1025568")
    subset_path = artifacts.get("calibration_subset_manifest")
    subset = read_json(subset_path) if subset_path else {}
    dispositions_path = (
        Path(
            read_json(
                output_dir / "transportation_dp8_historical_impact_preflight.json"
            )["inputs"]["final_dispositions"]["path"]
        )
        .expanduser()
        .resolve()
    )
    dispositions = {
        row["metric_id"]: row for row in read_csv(dispositions_path)
    }
    selected = list(subset.get("selected_metric_ids", []))
    expected_selected = sorted(
        metric_id
        for metric_id, row in dispositions.items()
        if row.get("calibration_candidate") == "1"
    )
    if sorted(selected) != expected_selected:
        errors.append(
            "calibration subset does not match frozen dispositions="
            f"{sorted(selected)} expected={expected_selected}"
        )
    panel_selected = sorted(
        str(value)
        for value in panel.get("calibration_candidate_metric_ids", [])
    )
    if sorted(selected) != panel_selected:
        errors.append(
            "calibration subset does not match panel manifest="
            f"{sorted(selected)} expected={panel_selected}"
        )
    prohibited = set(subset.get("prohibited_dispositions", []))
    invalid_selected = [
        metric_id
        for metric_id in selected
        if dispositions.get(metric_id, {}).get("metric_disposition")
        in prohibited
        or dispositions.get(metric_id, {}).get("calibration_candidate") != "1"
    ]
    if invalid_selected:
        errors.append(
            f"calibration subset contains ineligible metrics={invalid_selected}"
        )
    complete_hash = (
        sha256(artifacts["complete_panel"])
        if "complete_panel" in artifacts
        else ""
    )
    if (
        subset.get("complete_panel_sha256") != complete_hash
        or subset.get("calibration_input_panel_sha256") != complete_hash
    ):
        errors.append("calibration subset does not hash-select the complete panel")
    result = {
        "acceptance": "PASS" if not errors else "FAIL",
        "gate": "G8_V3_HISTORICAL_MATERIALIZATION_VALIDATION",
        "model_family": MODEL_FAMILY,
        "panel_status": "FROZEN" if not errors else "NOT_FROZEN",
        "snapshot_date_count": len(dates),
        "historical_membership_row_count": int(
            panel.get("historical_membership_row_count") or 0
        ),
        "specialized_discovery_metric_count": len(discovery_ids),
        "generic_metric_count": len(generic_ids),
        "complete_metric_count": len(discovery_ids) + len(generic_ids),
        "specialized_discovery_row_count": discovery_rows,
        "complete_panel_row_count": complete_rows,
        "calibration_subset_metric_ids": selected,
        "calibration_input_panel_sha256": complete_hash,
        "future_availability_error_count": sum(
            error.startswith("future availability") for error in errors
        ),
        "future_period_error_count": sum(
            error.startswith("future period") for error in errors
        ),
        "single_calibration_authorized": not errors,
        "calibration_executed": False,
        "production_promotion_authorized": False,
        "operations": {
            "database_writes": 0,
            "network_requests": 0,
            "parser_invocations": 0,
            "feature_rebuilds": 0,
            "portfolio_writes": 0,
            "calibration_invocations": 0,
        },
        "errors": errors[:100],
        "next_gate": (
            "FREEZE_WALK_FORWARD_CALIBRATION_CONTRACT"
            if not errors
            else "STOP_REPAIR_G8_VALIDATION"
        ),
    }
    output_path = output_dir / "transportation_v3_panel_validation.json"
    write_manifest(output_path, result)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
