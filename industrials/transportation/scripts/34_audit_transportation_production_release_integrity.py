#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from industrials.core.reports import write_text_atomic  # noqa: E402
from industrials.transportation.release_contract import (  # noqa: E402
    MODEL_FAMILY,
    git_source_state,
    required_release_source_paths,
)
from industrials.transportation.selected_feature_history import (  # noqa: E402
    read_json,
    sha256,
)


DEFAULT_RELEASE_NAME = "generic_oos_production_v1"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Audit an immutable transportation production release against "
            "its committed source census, artifact hashes, activation, and "
            "portfolio authorization."
        )
    )
    parser.add_argument("--asof", required=True)
    parser.add_argument("--release-name", default=DEFAULT_RELEASE_NAME)
    parser.add_argument("--release-dir", type=Path, default=None)
    return parser.parse_args()


def validate_reference(
    reference: object,
    *,
    label: str,
    errors: list[str],
) -> None:
    if not isinstance(reference, dict):
        errors.append(label + ": malformed reference")
        return
    path = Path(str(reference.get("path") or ""))
    expected = str(reference.get("sha256") or "").lower()
    if not path.is_file():
        errors.append(label + ": missing " + str(path))
    elif not expected or sha256(path) != expected:
        errors.append(label + ": hash mismatch")


def main() -> int:
    args = parse_args()
    asof = str(args.asof)[:10]
    release_name = str(args.release_name).strip()
    release_dir = (
        args.release_dir.expanduser().resolve()
        if args.release_dir
        else PROJECT_ROOT
        / "output"
        / "industrials"
        / MODEL_FAMILY
        / "releases"
        / asof
        / release_name
    )
    manifest_path = release_dir / "transportation_release_manifest.json"
    errors: list[str] = []
    if not manifest_path.is_file():
        raise FileNotFoundError(manifest_path)
    manifest = read_json(manifest_path)
    if manifest.get("acceptance") != "PASS":
        errors.append("release manifest is not passing")
    if (
        manifest.get("artifact_family")
        != "transportation_immutable_production_release"
    ):
        errors.append("release artifact family is not production")
    if manifest.get("model_family") != MODEL_FAMILY:
        errors.append("release model family mismatch")
    if manifest.get("asof_date") != asof:
        errors.append("release as-of mismatch")
    if manifest.get("release_name") != release_name:
        errors.append("release name mismatch")
    if manifest.get("production_model_promoted") is not True:
        errors.append("production model is not promoted")
    if manifest.get("production_allocation_authorized") is not True:
        errors.append("production allocation is not authorized")

    expected_commit = str(manifest.get("git_commit_sha") or "")
    required_sources = required_release_source_paths(PROJECT_ROOT)
    source_state, source_errors = git_source_state(
        PROJECT_ROOT,
        required_sources,
        expected_commit=expected_commit,
    )
    errors.extend(source_errors)
    validate_reference(
        manifest.get("source_dependencies"),
        label="source_dependencies",
        errors=errors,
    )
    validate_reference(
        manifest.get("portfolio_contract"),
        label="portfolio_contract",
        errors=errors,
    )
    artifacts = manifest.get("artifacts") or {}
    if not isinstance(artifacts, dict):
        errors.append("artifact inventory is malformed")
        artifacts = {}
    for label, item in artifacts.items():
        validate_reference(item, label=str(label), errors=errors)
    if int(manifest.get("artifact_count") or -1) != len(artifacts):
        errors.append("artifact count mismatch")

    source_dependencies_path = Path(
        str((manifest.get("source_dependencies") or {}).get("path") or "")
    )
    if source_dependencies_path.is_file():
        dependencies = read_json(source_dependencies_path)
        if dependencies.get("git_commit_sha") != expected_commit:
            errors.append("source dependency commit mismatch")
        declared = dependencies.get("dependencies") or {}
        if set(declared) != set(required_sources):
            errors.append("source dependency census mismatch")
        for label, item in declared.items():
            validate_reference(
                item,
                label="source:" + str(label),
                errors=errors,
            )

    contract_path = Path(
        str((manifest.get("portfolio_contract") or {}).get("path") or "")
    )
    if contract_path.is_file():
        contract = read_json(contract_path)
        if (
            contract.get("acceptance") != "PASS"
            or contract.get("production_allocation_authorized") is not True
            or abs(float(contract.get("sector_weight_cap") or 0.0) - 0.05)
            > 1e-12
        ):
            errors.append("sealed portfolio contract is not production-ready")

    evidence = manifest.get("evidence") or {}
    if (
        int(evidence.get("portfolio_candidate_rows") or 0) <= 0
        or int(evidence.get("oos_score_valid_rows") or 0) <= 0
        or int(evidence.get("investable_rows") or 0) <= 0
        or not str(evidence.get("lock_id") or "")
    ):
        errors.append("sealed production evidence has no investable OOS rows")

    result = {
        "acceptance": "PASS" if not errors else "FAIL",
        "gate": "TRANSPORTATION_PRODUCTION_RELEASE_INTEGRITY",
        "model_family": MODEL_FAMILY,
        "asof_date": asof,
        "release_name": release_name,
        "release_dir": str(release_dir),
        "git_commit_sha": source_state.get("git_commit_sha", ""),
        "artifact_count": len(artifacts),
        "production_model_promoted": True,
        "production_allocation_authorized": True,
        "errors": errors,
    }
    output_path = (
        release_dir / "transportation_production_release_integrity_audit.json"
    )
    write_text_atomic(
        output_path,
        json.dumps(result, indent=2, sort_keys=True) + "\n",
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
