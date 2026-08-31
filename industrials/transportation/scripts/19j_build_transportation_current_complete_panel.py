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

from industrials.core.config import family_config, load_yaml, resolve_path  # noqa: E402
from industrials.core.reports import write_csv_atomic  # noqa: E402
from industrials.transportation.financial_contract import (  # noqa: E402
    load_metric_registry,
)
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
    verify_artifact,
    write_manifest,
)


CURRENT_PANEL_VERSION = "transportation_current_complete_panel_v2"
DEFAULT_FROZEN_CONTRACT_DIR = (
    PROJECT_ROOT
    / "output"
    / "industrials"
    / "transportation"
    / "historical_features"
    / "v3_conflict_resolved"
)
DEFAULT_OUTPUT_ROOT = (
    PROJECT_ROOT
    / "output"
    / "industrials"
    / "transportation"
    / "current_panels"
)
FROZEN_PANEL_MANIFEST = "transportation_v3_panel_manifest.json"
FROZEN_PANEL_VALIDATION = "transportation_v3_panel_validation.json"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Build one current-only transportation v3 complete panel from an "
            "exact-date PIT snapshot and the sealed reviewed-evidence lineage. "
            "The frozen historical/calibration panel is never modified."
        )
    )
    parser.add_argument("--asof", required=True)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument(
        "--frozen-contract-dir",
        type=Path,
        default=DEFAULT_FROZEN_CONTRACT_DIR,
    )
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    return parser.parse_args()


def _artifact(path: Path, *, row_count: int | None = None) -> dict[str, Any]:
    result: dict[str, Any] = {
        "path": str(path.resolve()),
        "sha256": sha256(path),
    }
    if row_count is not None:
        result["row_count"] = row_count
    return result


def _valid_existing(
    *,
    manifest_path: Path,
    complete_path: Path,
    specialized_path: Path,
    coverage_path: Path,
    asof: str,
    snapshot_hash: str,
    preflight_hash: str,
    frozen_panel_hash: str,
    frozen_validation_hash: str,
    generator_hash: str,
    materializer_hash: str,
) -> bool:
    if not all(
        path.is_file()
        for path in (
            manifest_path,
            complete_path,
            specialized_path,
            coverage_path,
        )
    ):
        return False
    try:
        manifest = read_json(manifest_path)
    except (OSError, ValueError, json.JSONDecodeError):
        return False
    inputs = manifest.get("inputs") or {}
    artifacts = manifest.get("artifacts") or {}
    return (
        manifest.get("acceptance") == "PASS"
        and manifest.get("panel_status") == "CURRENT_ONLY_HASH_SEALED"
        and manifest.get("asof_date") == asof
        and manifest.get("historical_panel_modified") is False
        and str(
            (inputs.get("metric_availability_snapshot") or {}).get("sha256")
            or ""
        )
        == snapshot_hash
        and str((inputs.get("dp8_preflight") or {}).get("sha256") or "")
        == preflight_hash
        and str((inputs.get("frozen_v3_panel_manifest") or {}).get("sha256") or "")
        == frozen_panel_hash
        and str((inputs.get("frozen_v3_panel_validation") or {}).get("sha256") or "")
        == frozen_validation_hash
        and str((manifest.get("generator") or {}).get("sha256") or "")
        == generator_hash
        and str((manifest.get("materializer") or {}).get("sha256") or "")
        == materializer_hash
        and str((artifacts.get("complete_panel") or {}).get("sha256") or "")
        == sha256(complete_path)
        and str(
            (artifacts.get("specialized_discovery_panel") or {}).get(
                "sha256"
            )
            or ""
        )
        == sha256(specialized_path)
        and str((artifacts.get("coverage") or {}).get("sha256") or "")
        == sha256(coverage_path)
    )


def main() -> int:
    args = parse_args()
    asof = args.asof[:10]
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    family = family_config(config, MODEL_FAMILY)
    historical_cfg = family["historical_features"]
    financial_cfg = family["financial"]
    base_dir = config_path.parent
    foundation = resolve_foundation(config_path, args.db)
    historical_root = resolve_path(
        historical_cfg["output_root"],
        base_dir=base_dir,
    )
    snapshot_dir = historical_root / asof
    availability_path = snapshot_dir / "metric_availability.csv"
    if not availability_path.is_file():
        raise FileNotFoundError(
            f"{availability_path}: build the exact-date PIT snapshot first"
        )

    frozen_dir = args.frozen_contract_dir.expanduser().resolve()
    preflight_path = (
        frozen_dir / "transportation_dp8_historical_impact_preflight.json"
    )
    if not preflight_path.is_file():
        raise FileNotFoundError(preflight_path)
    preflight = read_json(preflight_path)
    if (
        preflight.get("acceptance") != "PASS"
        or preflight.get("decision")
        != "GO_ALL_SPECIALIZED_PARTITIONS_ONLY"
    ):
        raise ValueError("the frozen DP8 v3 evidence contract is not passing")
    preflight_inputs = preflight.get("inputs") or {}
    for label, reference in preflight_inputs.items():
        if label in {
            "v2_build_manifest",
            "v2_snapshot_hash_set_sha256",
            "v2_validation_manifest",
        }:
            continue
        verify_artifact(reference, label=f"frozen DP8 input {label}")
    # The v2 build/validation manifests are rolling operational indexes and
    # legitimately change as later snapshots accrue or source snapshots are
    # repaired.  The immutable calibration input is the materialized v3 panel.
    # Anchor current builds to that sealed panel and its original DP8 contract.
    frozen_panel_manifest_path = frozen_dir / FROZEN_PANEL_MANIFEST
    frozen_panel_validation_path = frozen_dir / FROZEN_PANEL_VALIDATION
    frozen_panel_manifest = read_json(frozen_panel_manifest_path)
    frozen_panel_validation = read_json(frozen_panel_validation_path)
    if (
        frozen_panel_manifest.get("acceptance") != "PASS"
        or frozen_panel_manifest.get("panel_status") != "HASH_FROZEN"
        or frozen_panel_manifest.get("asof_date") != preflight.get("asof_date")
        or frozen_panel_manifest.get("last_snapshot_date")
        != preflight.get("last_snapshot_date")
    ):
        raise ValueError("the immutable transportation v3 panel contract is not passing")
    sealed_preflight = (
        (frozen_panel_manifest.get("inputs") or {}).get("dp8_preflight") or {}
    )
    if str(sealed_preflight.get("sha256") or "") != sha256(preflight_path):
        raise ValueError("the immutable v3 panel does not seal the active DP8 preflight")
    frozen_artifacts = frozen_panel_manifest.get("artifacts") or {}
    for label, reference in frozen_artifacts.items():
        verify_artifact(reference, label=f"frozen v3 panel artifact {label}")
    frozen_complete_hash = str(
        (frozen_artifacts.get("complete_panel") or {}).get("sha256") or ""
    )
    if (
        frozen_panel_validation.get("acceptance") != "PASS"
        or frozen_panel_validation.get("panel_status") != "FROZEN"
        or str(
            frozen_panel_validation.get("calibration_input_panel_sha256") or ""
        )
        != frozen_complete_hash
    ):
        raise ValueError("the immutable transportation v3 panel validation is not passing")

    dispositions_path = Path(
        preflight["inputs"]["final_dispositions"]["path"]
    ).resolve()
    coverage_input_path = Path(
        preflight["inputs"]["final_coverage"]["path"]
    ).resolve()
    scope_path = Path(preflight["inputs"]["scope"]["path"]).resolve()
    discovery_registry_path = Path(
        preflight["inputs"]["discovery_registry"]["path"]
    ).resolve()
    _, definitions = load_metric_registry(
        resolve_path(
            financial_cfg["metric_registry"],
            base_dir=base_dir,
        )
    )
    generic_ids = [
        definition.metric_id
        for definition in definitions
        if not definition.specialized
    ]
    scope_rows = read_csv(scope_path)
    coverage_rows = read_csv(coverage_input_path)
    disposition_rows = read_csv(dispositions_path)
    registry_rows = read_csv(discovery_registry_path)
    with read_only_connection(
        foundation.db_path,
        timeout_sec=foundation.timeout_sec,
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

    output_dir = args.output_root.expanduser().resolve() / asof
    specialized_path = (
        output_dir
        / "transportation_current_specialized_discovery_panel.csv.gz"
    )
    complete_path = (
        output_dir / "transportation_current_complete_panel.csv.gz"
    )
    coverage_path = (
        output_dir / "transportation_current_specialized_coverage.csv"
    )
    manifest_path = (
        output_dir / "transportation_current_complete_panel_manifest.json"
    )
    generator_path = Path(__file__).resolve()
    materializer_path = Path(materialize_panels.__code__.co_filename).resolve()
    snapshot_hash = sha256(availability_path)
    preflight_hash = sha256(preflight_path)
    frozen_panel_hash = sha256(frozen_panel_manifest_path)
    frozen_validation_hash = sha256(frozen_panel_validation_path)
    generator_hash = sha256(generator_path)
    materializer_hash = sha256(materializer_path)
    if _valid_existing(
        manifest_path=manifest_path,
        complete_path=complete_path,
        specialized_path=specialized_path,
        coverage_path=coverage_path,
        asof=asof,
        snapshot_hash=snapshot_hash,
        preflight_hash=preflight_hash,
        frozen_panel_hash=frozen_panel_hash,
        frozen_validation_hash=frozen_validation_hash,
        generator_hash=generator_hash,
        materializer_hash=materializer_hash,
    ):
        print(manifest_path.read_text(encoding="utf-8"), end="")
        return 0
    if any(
        path.exists()
        for path in (
            manifest_path,
            complete_path,
            specialized_path,
            coverage_path,
        )
    ):
        raise FileExistsError(
            f"{output_dir}: non-identical current panel exists; "
            "use a new as-of date or remove it after explicit review"
        )

    result = materialize_panels(
        historical_root=historical_root,
        dates=[asof],
        scope_rows=scope_rows,
        coverage_rows=coverage_rows,
        disposition_rows=disposition_rows,
        discovery_registry_rows=registry_rows,
        generic_metric_ids=generic_ids,
        accepted=accepted,
        discovery_path=specialized_path,
        complete_path=complete_path,
        allow_out_of_scope_tickers=True,
    )
    write_csv_atomic(
        coverage_path,
        COVERAGE_FIELDS,
        result["coverage_rows"],
    )
    membership_count = int(result["membership_row_count"])
    expected_specialized = membership_count * len(registry_rows)
    expected_complete = membership_count * (
        len(registry_rows) + len(generic_ids)
    )
    errors: list[str] = []
    if result["discovery_row_count"] != expected_specialized:
        errors.append(
            "specialized row count mismatch="
            f"{result['discovery_row_count']} expected={expected_specialized}"
        )
    if result["complete_row_count"] != expected_complete:
        errors.append(
            "complete row count mismatch="
            f"{result['complete_row_count']} expected={expected_complete}"
        )
    if membership_count <= 0:
        errors.append("current membership count is zero")
    acceptance = "PASS" if not errors else "FAIL"
    manifest = {
        "acceptance": acceptance,
        "gate": "DP16A_BUILD_CURRENT_ONLY_COMPLETE_PANEL",
        "panel_version": CURRENT_PANEL_VERSION,
        "source_panel_contract_version": PANEL_VERSION,
        "panel_status": (
            "CURRENT_ONLY_HASH_SEALED"
            if acceptance == "PASS"
            else "CURRENT_ONLY_NOT_SEALED"
        ),
        "model_family": MODEL_FAMILY,
        "asof_date": asof,
        "membership_row_count": membership_count,
        "current_snapshot_ticker_count": membership_count
        + len(result["out_of_scope_tickers"]),
        "frozen_scope_excluded_ticker_count": len(
            result["out_of_scope_tickers"]
        ),
        "frozen_scope_excluded_tickers": result["out_of_scope_tickers"],
        "scope_expansion_deferred": bool(result["out_of_scope_tickers"]),
        "generic_metric_count": len(generic_ids),
        "specialized_metric_count": len(registry_rows),
        "complete_metric_count": len(generic_ids) + len(registry_rows),
        "accepted_evidence_observation_count": sum(
            len(rows) for rows in accepted.values()
        ),
        "accepted_evidence_ticker_metric_count": len(accepted),
        "historical_panel_modified": False,
        "calibration_input_modified": False,
        "calibration_executed": False,
        "outcomes_accessed": False,
        "production_promotion_authorized": False,
        "inputs": {
            "metric_availability_snapshot": _artifact(
                availability_path,
                row_count=len(read_csv(availability_path)),
            ),
            "dp8_preflight": _artifact(preflight_path),
            "frozen_v3_panel_manifest": _artifact(frozen_panel_manifest_path),
            "frozen_v3_panel_validation": _artifact(frozen_panel_validation_path),
            "scope": _artifact(scope_path, row_count=len(scope_rows)),
            "final_coverage": _artifact(
                coverage_input_path,
                row_count=len(coverage_rows),
            ),
            "final_dispositions": _artifact(
                dispositions_path,
                row_count=len(disposition_rows),
            ),
            "discovery_registry": _artifact(
                discovery_registry_path,
                row_count=len(registry_rows),
            ),
        },
        "artifacts": {
            "specialized_discovery_panel": _artifact(
                specialized_path,
                row_count=result["discovery_row_count"],
            ),
            "complete_panel": _artifact(
                complete_path,
                row_count=result["complete_row_count"],
            ),
            "coverage": _artifact(
                coverage_path,
                row_count=len(result["coverage_rows"]),
            ),
        },
        "generator": {
            "path": str(generator_path),
            "sha256": generator_hash,
        },
        "materializer": {
            "path": str(materializer_path),
            "sha256": materializer_hash,
        },
        "operations": {
            "current_panel_materializations": 1,
            "historical_materializations": 0,
            "calibration_invocations": 0,
            "database_writes": 0,
            "network_requests": 0,
            "parser_invocations": 0,
            "portfolio_writes": 0,
        },
        "errors": errors,
        "next_gate": (
            "EXPORT_OUTCOME_BLIND_CURRENT_MONITORING_SOURCE"
            if acceptance == "PASS"
            else "REVIEW_CURRENT_PANEL_FAILURES"
        ),
    }
    write_manifest(manifest_path, manifest)
    print(json.dumps(manifest, indent=2, sort_keys=True))
    return 0 if acceptance == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
