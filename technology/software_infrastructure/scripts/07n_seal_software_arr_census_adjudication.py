#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parents[2]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from dedicated_parser.contracts import file_sha256  # noqa: E402
from technology.core.config import cfg_get, load_yaml, resolve_path  # noqa: E402
from technology.software_infrastructure.dedicated_parser_baseline import (  # noqa: E402
    open_read_only_database,
)
from technology.software_infrastructure.software_arr_release import (  # noqa: E402
    APPROVED_POLICY_ID,
    APPROVED_RELEASE_ID,
    HUMAN_APPROVED,
    build_arr_policy,
    source_keys,
    utc_timestamp,
    validate_arr_rows,
)
from technology.software_infrastructure.software_metric_review import (  # noqa: E402
    load_csv_rows,
    load_source_evidence,
)
from technology.software_infrastructure.software_parser_hydration import (  # noqa: E402
    atomic_json,
)


DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
SOFTWARE_ROOT = PACKAGE_ROOT / "software_infrastructure"
DEFAULT_WORKBOOK = (
    PROJECT_ROOT
    / "output"
    / "technology_reports"
    / "software_infrastructure"
    / "disclosure_census"
    / "2026-07-30"
    / "arr_adjudication"
    / "software_arr_proposed_canonical_review.csv"
)
DEFAULT_POLICY = (
    SOFTWARE_ROOT
    / "review_policies"
    / "software_arr_policy_v1.json"
)
DEFAULT_APPROVAL = (
    SOFTWARE_ROOT
    / "review_policies"
    / "software_arr_policy_v1_approval.json"
)
DEFAULT_OUTPUT = (
    PROJECT_ROOT
    / "output"
    / "technology_reports"
    / "software_infrastructure"
    / "arr_governance"
    / APPROVED_RELEASE_ID
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Seal the hash-approved 71-row canonical ARR workbook."
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--workbook", type=Path, default=DEFAULT_WORKBOOK)
    parser.add_argument("--policy", type=Path, default=DEFAULT_POLICY)
    parser.add_argument("--approval-file", type=Path, default=DEFAULT_APPROVAL)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--approved-workbook-sha256", required=True)
    parser.add_argument("--reviewer", required=True)
    parser.add_argument("--reviewed-at-utc", default="")
    parser.add_argument("--write", action="store_true")
    return parser.parse_args()


def _artifact(path: Path) -> dict[str, object]:
    return {
        "path": str(path.resolve()),
        "sha256": file_sha256(path),
        "bytes": path.stat().st_size,
    }


def main() -> int:
    args = parse_args()
    expected_hash = args.approved_workbook_sha256.strip().lower()
    if len(expected_hash) != 64:
        raise ValueError("approved-workbook-sha256 must contain 64 hex digits")
    int(expected_hash, 16)
    reviewer = args.reviewer.strip()
    if not reviewer:
        raise ValueError("reviewer is required")
    reviewed_at = args.reviewed_at_utc.strip() or utc_timestamp()
    workbook_path = args.workbook.expanduser().resolve()
    actual_hash = file_sha256(workbook_path)
    if actual_hash != expected_hash:
        raise ValueError(
            "Approved ARR workbook hash mismatch: "
            f"expected={expected_hash}, actual={actual_hash}"
        )
    rows = load_csv_rows(workbook_path)
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    db_path = (
        args.db.expanduser().resolve()
        if args.db
        else resolve_path(
            cfg_get(config, "paths.database_path"),
            base_dir=config_path.parent,
        )
    )
    timeout_sec = float(cfg_get(config, "runtime.sqlite_timeout_sec", 120.0))
    with open_read_only_database(db_path, timeout_sec=timeout_sec) as conn:
        evidence = load_source_evidence(conn, source_keys(rows))
    errors = validate_arr_rows(
        rows,
        source_evidence=evidence,
        expected_count=71,
    )
    if errors:
        raise ValueError(
            "Approved ARR source validation failed: "
            + "; ".join(errors[:10])
        )
    governance = {key: HUMAN_APPROVED for key in source_keys(rows)}
    policy_path = args.policy.expanduser().resolve()
    approval_path = args.approval_file.expanduser().resolve()
    registry_path = (
        SOFTWARE_ROOT
        / "data"
        / "software_infrastructure_specialized_metric_registry.yaml"
    )
    adapter_path = SOFTWARE_ROOT / "dedicated_parser_adapter.py"
    policy = build_arr_policy(
        rows=rows,
        source_evidence=evidence,
        release_id=APPROVED_RELEASE_ID,
        policy_id=APPROVED_POLICY_ID,
        approved_workbook_path=workbook_path,
        approved_workbook_sha256=expected_hash,
        reviewer=reviewer,
        reviewed_at_utc=reviewed_at,
        governance_status_by_key=governance,
        registry_path=registry_path,
        adapter_path=adapter_path,
    )
    approval = {
        "approval_schema_version": "software_arr_approval_v1",
        "release_id": APPROVED_RELEASE_ID,
        "policy_id": APPROVED_POLICY_ID,
        "approved_by": reviewer,
        "approved_at_utc": reviewed_at,
        "approval_source": "direct_user_instruction",
        "approved_workbook_path": str(workbook_path),
        "approved_workbook_sha256": expected_hash,
        "decision_count": len(policy["decisions"]),
        "calibration_eligible_count": 71,
        "measurement_only_flag": 1,
        "production_weight_modified_flag": 0,
    }
    if args.write:
        atomic_json(policy_path, policy)
        atomic_json(approval_path, approval)
    manifest: dict[str, object] = {
        "manifest_version": "software_arr_release_manifest_v1",
        "execution_mode": "write" if args.write else "validate_only",
        "release_id": APPROVED_RELEASE_ID,
        "policy_id": APPROVED_POLICY_ID,
        "approved_workbook_sha256": expected_hash,
        "decision_count": len(policy["decisions"]),
        "source_integrity_status": "PASS",
        "policy_validation_status": "PASS",
        "measurement_only_flag": 1,
        "production_weight_modified_flag": 0,
    }
    if args.write:
        manifest["artifacts"] = {
            "policy": _artifact(policy_path),
            "approval": _artifact(approval_path),
            "approved_workbook": _artifact(workbook_path),
        }
        output_dir = args.output_dir.expanduser().resolve()
        atomic_json(
            output_dir / "software_arr_release_manifest.json",
            manifest,
        )
    print(json.dumps(manifest, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
