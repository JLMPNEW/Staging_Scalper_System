#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from dedicated_parser.contracts import file_sha256  # noqa: E402
from industrials.core.config import family_config, load_yaml, resolve_path  # noqa: E402
from industrials.transportation.scripts._shared import (  # noqa: E402
    DEFAULT_CONFIG,
    MODEL_FAMILY,
    resolve_foundation,
)
from industrials.transportation.selected_feature_history import (  # noqa: E402
    PREFLIGHT_FIELDS,
    PREFLIGHT_VERSION,
    build_preflight_rows,
    evidence_lineage,
    first_evidence_dates,
    read_csv,
    read_json,
    read_only_connection,
    sha256,
    snapshot_dates,
    stable_json_sha256,
    verify_artifact,
    verify_v2_snapshots,
    write_manifest,
)
from industrials.core.reports import write_csv_atomic  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Run the read-only DP8 transportation historical-impact preflight. "
            "No parser, feature builder, portfolio writer, or database mutation "
            "is invoked."
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
    base_dir = config_path.parent
    foundation = resolve_foundation(config_path, args.db)
    asof_date = str(parser_cfg["source_census_asof_date"])
    parser_output = (
        resolve_path(parser_cfg["output_root"], base_dir=base_dir) / asof_date
    )
    historical_root = resolve_path(
        historical_cfg["output_root"], base_dir=base_dir
    )
    output_dir = (
        args.output_dir.expanduser().resolve()
        if args.output_dir
        else historical_root / "v3"
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    freeze_path = parser_output / "transportation_final_metric_freeze_manifest.json"
    freeze = read_json(freeze_path)
    if freeze.get("acceptance") != "PASS":
        raise ValueError("Final metric freeze must PASS before DP8")
    dispositions_path = verify_artifact(
        freeze["artifacts"]["final_metric_dispositions"],
        label="final metric dispositions",
    )
    coverage_path = verify_artifact(
        freeze["inputs"]["all_source_coverage"],
        label="final all-source coverage",
    )
    verify_artifact(
        freeze["inputs"]["all_source_coverage_manifest"],
        label="final all-source coverage manifest",
    )
    v2_build_path = resolve_path(
        historical_cfg["build_manifest_json"], base_dir=base_dir
    )
    v2_validation_path = resolve_path(
        historical_cfg["validation_output_json"], base_dir=base_dir
    )
    v2_build = read_json(v2_build_path)
    v2_validation = read_json(v2_validation_path)
    dates = snapshot_dates(v2_build)
    snapshot_hashes = verify_v2_snapshots(
        historical_root=historical_root,
        validation_manifest=v2_validation,
    )
    scope_path = resolve_path(
        parser_cfg["scope_manifest_csv"], base_dir=base_dir
    )
    registry_path = resolve_path(
        parser_cfg["discovery_registry_csv"], base_dir=base_dir
    )
    scope_rows = read_csv(scope_path)
    coverage_rows = read_csv(coverage_path)
    disposition_rows = read_csv(dispositions_path)
    registry_rows = read_csv(registry_path)
    errors: list[str] = []
    if len(scope_rows) != 14_400:
        errors.append(f"scope rows={len(scope_rows)} expected=14400")
    if len(coverage_rows) != 14_400:
        errors.append(f"coverage rows={len(coverage_rows)} expected=14400")
    if len(disposition_rows) != 90:
        errors.append(f"disposition rows={len(disposition_rows)} expected=90")
    if len(registry_rows) != 90:
        errors.append(f"registry rows={len(registry_rows)} expected=90")
    if int(v2_validation.get("total_membership_rows") or 0) != 9_496:
        errors.append(
            "v2 membership rows="
            f"{v2_validation.get('total_membership_rows')} expected=9496"
        )
    review_evaluation_ids = sorted(
        {
            int(value)
            for value in freeze.get("review_evaluation_ids", {}).values()
            if int(value or 0) > 0
        }
    )
    supplemental_run_ids = sorted(
        {
            int(value)
            for value in freeze.get("supplemental_parser_run_ids", [])
            if int(value or 0) > 0
        }
    )
    with read_only_connection(
        foundation.db_path, timeout_sec=foundation.timeout_sec
    ) as connection:
        evidence = evidence_lineage(
            connection=connection,
            evaluation_ids=review_evaluation_ids,
            supplemental_run_ids=supplemental_run_ids,
        )
        placeholders = ",".join("?" for _ in review_evaluation_ids)
        reviewed_source_run_ids = sorted(
            int(row[0])
            for row in connection.execute(
                f"""
                SELECT base_run_id
                FROM sec_parser_review_evaluation
                WHERE evaluation_id IN ({placeholders})
                """,
                review_evaluation_ids,
            )
        )
    expected_reviewed_runs = sorted(
        int(value) for value in freeze.get("reviewed_source_run_ids", [])
    )
    if reviewed_source_run_ids != expected_reviewed_runs:
        errors.append(
            "reviewed source runs="
            f"{reviewed_source_run_ids} expected={expected_reviewed_runs}"
        )
    preflight_rows = build_preflight_rows(
        scope_rows=scope_rows,
        coverage_rows=coverage_rows,
        disposition_rows=disposition_rows,
        dates=dates,
        first_dates=first_evidence_dates(evidence),
    )
    if len(preflight_rows) != 14_400:
        errors.append(
            f"preflight decision rows={len(preflight_rows)} expected=14400"
        )
    output_csv = output_dir / "transportation_dp8_historical_impact_preflight.csv"
    output_json = output_dir / "transportation_dp8_historical_impact_preflight.json"
    write_csv_atomic(output_csv, PREFLIGHT_FIELDS, preflight_rows)
    decision = "GO_ALL_SPECIALIZED_PARTITIONS_ONLY" if not errors else "NO_GO"
    manifest = {
        "acceptance": "PASS" if not errors else "FAIL",
        "gate": "DP8_HISTORICAL_IMPACT_PREFLIGHT",
        "preflight_version": PREFLIGHT_VERSION,
        "model_family": MODEL_FAMILY,
        "asof_date": asof_date,
        "decision": decision,
        "decision_reason": (
            "the frozen 90-metric v3 registry is additive to the frozen v2 "
            "registry; all specialized partitions require explicit states"
        ),
        "snapshot_date_count": len(dates),
        "first_snapshot_date": dates[0],
        "last_snapshot_date": dates[-1],
        "historical_membership_row_count": int(
            v2_validation["total_membership_rows"]
        ),
        "affected_specialized_partition_count": (
            int(v2_validation["total_membership_rows"]) * len(registry_rows)
        ),
        "expected_specialized_row_count": 854_640,
        "expected_complete_row_count": 1_025_568,
        "decision_row_count": len(preflight_rows),
        "review_evaluation_ids": review_evaluation_ids,
        "reviewed_source_run_ids": reviewed_source_run_ids,
        "supplemental_evidence_run_ids": supplemental_run_ids,
        "evidence_row_count": len(evidence),
        "accepted_evidence_row_count": sum(
            str(row.get("candidate_status") or "") == "ACCEPTED"
            for row in evidence
        ),
        "calibration_candidate_metric_ids": freeze[
            "calibration_candidate_metric_ids"
        ],
        "inputs": {
            "final_freeze_manifest": {
                "path": str(freeze_path),
                "sha256": file_sha256(freeze_path),
            },
            "final_dispositions": {
                "path": str(dispositions_path),
                "sha256": sha256(dispositions_path),
            },
            "final_coverage": {
                "path": str(coverage_path),
                "sha256": sha256(coverage_path),
            },
            "scope": {"path": str(scope_path), "sha256": sha256(scope_path)},
            "discovery_registry": {
                "path": str(registry_path),
                "sha256": sha256(registry_path),
            },
            "v2_build_manifest": {
                "path": str(v2_build_path),
                "sha256": sha256(v2_build_path),
            },
            "v2_validation_manifest": {
                "path": str(v2_validation_path),
                "sha256": sha256(v2_validation_path),
            },
            "v2_snapshot_hash_set_sha256": stable_json_sha256(snapshot_hashes),
        },
        "artifacts": {
            "impact_map": {
                "path": str(output_csv),
                "row_count": len(preflight_rows),
                "sha256": sha256(output_csv),
            }
        },
        "operations": {
            "database_writes": 0,
            "network_requests": 0,
            "parser_invocations": 0,
            "source_document_opens": 0,
            "market_feature_builds": 0,
            "financial_feature_builds": 0,
            "membership_rebuilds": 0,
            "portfolio_writes": 0,
        },
        "errors": errors,
        "next_gate": (
            "MATERIALIZE_AND_FREEZE_V3_PANEL_ONCE"
            if not errors
            else "STOP_REPAIR_PREFLIGHT_INPUTS"
        ),
    }
    write_manifest(output_json, manifest)
    print(json.dumps(manifest, indent=2, sort_keys=True))
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
