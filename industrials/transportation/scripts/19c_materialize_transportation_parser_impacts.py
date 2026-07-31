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
from industrials.transportation.scripts._shared import (  # noqa: E402
    DEFAULT_CONFIG,
    MODEL_FAMILY,
    resolve_foundation,
)
from industrials.transportation.selected_feature_history import (  # noqa: E402
    COVERAGE_FIELDS,
    PANEL_VERSION,
    evidence_lineage,
    materialize_panels,
    normalized_accepted_evidence,
    read_csv,
    read_json,
    read_only_connection,
    sha256,
    snapshot_dates,
    verify_artifact,
    write_manifest,
)
from industrials.core.reports import write_csv_atomic  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Materialize and hash-freeze the complete transportation v3 "
            "discovery panel once from frozen v2 and reviewed parser evidence."
        )
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
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
    foundation = resolve_foundation(config_path, args.db)
    asof_date = str(parser_cfg["source_census_asof_date"])
    historical_root = resolve_path(
        historical_cfg["output_root"], base_dir=base_dir
    )
    output_dir = (
        args.output_dir.expanduser().resolve()
        if args.output_dir
        else historical_root / "v3"
    )
    preflight_path = output_dir / "transportation_dp8_historical_impact_preflight.json"
    preflight = read_json(preflight_path)
    if (
        preflight.get("acceptance") != "PASS"
        or preflight.get("decision") != "GO_ALL_SPECIALIZED_PARTITIONS_ONLY"
    ):
        raise ValueError("DP8 must authorize all specialized partitions")
    discovery_path = (
        output_dir / "transportation_v3_specialized_discovery_panel.csv.gz"
    )
    complete_path = output_dir / "transportation_v3_complete_panel.csv.gz"
    coverage_output = output_dir / "transportation_v3_historical_coverage.csv"
    subset_path = output_dir / "transportation_v3_calibration_subset_manifest.json"
    panel_manifest_path = output_dir / "transportation_v3_panel_manifest.json"
    current_preflight_hash = sha256(preflight_path)
    if panel_manifest_path.is_file():
        existing = read_json(panel_manifest_path)
        existing_preflight = (
            existing.get("inputs", {}).get("dp8_preflight", {})
        )
        if (
            existing.get("acceptance") != "PASS"
            or existing.get("panel_status") != "HASH_FROZEN"
            or existing_preflight.get("sha256") != current_preflight_hash
        ):
            raise ValueError(
                "Existing v3 panel is frozen under a different or failed "
                "contract; use a new versioned output directory"
            )
        for label, reference in existing.get("artifacts", {}).items():
            verify_artifact(reference, label=f"existing {label}")
        reused = dict(existing)
        reused["execution_action"] = "REUSED_FROZEN_PANEL"
        reused["historical_materialization_invocations_this_run"] = 0
        print(json.dumps(reused, indent=2, sort_keys=True))
        return 0
    for label, reference in preflight["inputs"].items():
        if label == "v2_snapshot_hash_set_sha256":
            continue
        verify_artifact(reference, label=label)
    freeze_path = Path(
        preflight["inputs"]["final_freeze_manifest"]["path"]
    ).resolve()
    dispositions_path = Path(
        preflight["inputs"]["final_dispositions"]["path"]
    ).resolve()
    coverage_path = Path(preflight["inputs"]["final_coverage"]["path"]).resolve()
    scope_path = Path(preflight["inputs"]["scope"]["path"]).resolve()
    registry_path = Path(
        preflight["inputs"]["discovery_registry"]["path"]
    ).resolve()
    build_path = Path(
        preflight["inputs"]["v2_build_manifest"]["path"]
    ).resolve()
    dates = snapshot_dates(read_json(build_path))
    _, generic_metrics = load_metric_registry(
        resolve_path(financial_cfg["metric_registry"], base_dir=base_dir)
    )
    generic_metric_ids = [
        metric.metric_id
        for metric in generic_metrics
        if not bool(metric.specialized)
    ]
    scope_rows = read_csv(scope_path)
    coverage_rows = read_csv(coverage_path)
    disposition_rows = read_csv(dispositions_path)
    registry_rows = read_csv(registry_path)
    with read_only_connection(
        foundation.db_path, timeout_sec=foundation.timeout_sec
    ) as connection:
        evidence_rows = evidence_lineage(
            connection=connection,
            evaluation_ids=[
                int(value) for value in preflight["review_evaluation_ids"]
            ],
            supplemental_run_ids=[
                int(value)
                for value in preflight["supplemental_evidence_run_ids"]
            ],
        )
    accepted = normalized_accepted_evidence(evidence_rows)
    result = materialize_panels(
        historical_root=historical_root,
        dates=dates,
        scope_rows=scope_rows,
        coverage_rows=coverage_rows,
        disposition_rows=disposition_rows,
        discovery_registry_rows=registry_rows,
        generic_metric_ids=generic_metric_ids,
        accepted=accepted,
        discovery_path=discovery_path,
        complete_path=complete_path,
    )
    write_csv_atomic(
        coverage_output,
        COVERAGE_FIELDS,
        result["coverage_rows"],
    )
    candidate_ids = [
        row["metric_id"]
        for row in disposition_rows
        if row["calibration_candidate"] == "1"
    ]
    complete_hash = sha256(complete_path)
    subset = {
        "acceptance": "PASS",
        "gate": "DP9_CALIBRATION_SUBSET_FREEZE",
        "panel_version": PANEL_VERSION,
        "selection_mode": "column_selection_from_frozen_complete_panel",
        "selected_metric_ids": candidate_ids,
        "selected_metric_count": len(candidate_ids),
        "complete_panel_path": str(complete_path),
        "complete_panel_sha256": complete_hash,
        "calibration_input_panel_sha256": complete_hash,
        "second_feature_materialization_required": False,
        "prohibited_dispositions": [
            "DEFERRED_REVIEW",
            "DIAGNOSTIC_ONLY",
            "EXCLUDED_INSUFFICIENT_EVIDENCE",
        ],
        "calibration_executed": False,
        "production_promotion_authorized": False,
        "next_gate": "VALIDATE_V3_PANEL_AND_SUBSET",
    }
    write_manifest(subset_path, subset)
    artifacts = {
        "specialized_discovery_panel": {
            "path": str(discovery_path),
            "row_count": result["discovery_row_count"],
            "sha256": sha256(discovery_path),
        },
        "complete_panel": {
            "path": str(complete_path),
            "row_count": result["complete_row_count"],
            "sha256": complete_hash,
        },
        "historical_coverage": {
            "path": str(coverage_output),
            "row_count": len(result["coverage_rows"]),
            "sha256": sha256(coverage_output),
        },
        "calibration_subset_manifest": {
            "path": str(subset_path),
            "row_count": len(candidate_ids),
            "sha256": sha256(subset_path),
        },
    }
    errors: list[str] = []
    if result["membership_row_count"] != 9_496:
        errors.append(
            f"membership rows={result['membership_row_count']} expected=9496"
        )
    if result["discovery_row_count"] != 854_640:
        errors.append(
            f"discovery rows={result['discovery_row_count']} expected=854640"
        )
    if result["complete_row_count"] != 1_025_568:
        errors.append(
            f"complete rows={result['complete_row_count']} expected=1025568"
        )
    frozen_candidate_ids = sorted(
        str(value)
        for value in preflight["calibration_candidate_metric_ids"]
    )
    if sorted(candidate_ids) != frozen_candidate_ids:
        errors.append(
            "calibration candidates do not match the final freeze="
            f"{sorted(candidate_ids)} expected={frozen_candidate_ids}"
        )
    manifest = {
        "acceptance": "PASS" if not errors else "FAIL",
        "gate": "DP9_MATERIALIZE_AND_FREEZE_V3_PANEL",
        "panel_status": "HASH_FROZEN" if not errors else "NOT_FROZEN",
        "panel_version": PANEL_VERSION,
        "model_family": MODEL_FAMILY,
        "asof_date": asof_date,
        "snapshot_date_count": len(dates),
        "first_snapshot_date": dates[0],
        "last_snapshot_date": dates[-1],
        "historical_membership_row_count": result["membership_row_count"],
        "generic_metric_count": len(generic_metric_ids),
        "specialized_discovery_metric_count": len(registry_rows),
        "complete_metric_count": len(generic_metric_ids) + len(registry_rows),
        "accepted_evidence_observation_count": sum(
            len(rows) for rows in accepted.values()
        ),
        "accepted_evidence_ticker_metric_count": len(accepted),
        "calibration_candidate_metric_ids": candidate_ids,
        "inputs": {
            "dp8_preflight": {
                "path": str(preflight_path),
                "sha256": current_preflight_hash,
            },
            "final_freeze_manifest": {
                "path": str(freeze_path),
                "sha256": sha256(freeze_path),
            },
            "frozen_v2_panel_manifest": {
                "path": str(build_path),
                "sha256": sha256(build_path),
            },
        },
        "artifacts": artifacts,
        "operations": {
            "historical_materialization_invocations": 1,
            "database_writes": 0,
            "network_requests": 0,
            "parser_invocations": 0,
            "source_document_opens": 0,
            "market_feature_builds": 0,
            "financial_feature_builds": 0,
            "membership_rebuilds": 0,
            "portfolio_writes": 0,
            "calibration_invocations": 0,
        },
        "errors": errors,
        "next_gate": (
            "VALIDATE_V3_PANEL_AND_SUBSET"
            if not errors
            else "STOP_REPAIR_V3_MATERIALIZATION"
        ),
    }
    write_manifest(panel_manifest_path, manifest)
    print(json.dumps(manifest, indent=2, sort_keys=True))
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
